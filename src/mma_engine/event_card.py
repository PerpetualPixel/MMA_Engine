"""The event's official fight card, read live from ESPN's MMA scoreboard.

The consensus is built from whatever the cappers talked about, which is not
the same thing as the event: prediction videos routinely cover side content
(a Contender Series card, next week's Fight Night) in the same upload, and a
transcript garble invents pairings that were never fights at all. ESPN's
scoreboard carries the real card — every bout, in order, with per-fight
status — so it decides which fights survive into the payload:

    card_status: "on_card"   — matched to a bout on this event's card
                 "cancelled" — was on the card and is no longer scheduled
                               (ESPN marks it canceled, or it vanished from
                               the card between runs)

Anything else is dropped outright. The card is the event, so a fight that is
not on it does not belong in the event's payload — not in the dashboard, not
in picks.json, not in a parlay. The alternative (keeping them under an
"other events" heading) put dozens of caption-mangled non-fights next to the
real card and made the reader do the filtering.

On-card fights also gain `card_order` (0 = earliest bout of the night — ESPN
lists the main event first, so chronological order is the reverse of listing
order) and have their fighter names corrected to ESPN's clean spellings,
which fixes the auto-caption garbles ("Islam Makhache") that survive
aggregation when every capper's transcript mangles a name the same way.

Bouts nobody picked are appended as pickless fights, so the dashboard shows
the whole card rather than only the fights that happened to get coverage.

Fail-open by design: if ESPN is unreachable or the event can't be found, the
payload is returned un-annotated (no card_status anywhere) and NOTHING is
dropped — the pipeline logs and moves on. A missing scoreboard must never
cost the weekly run, and a run with no card has no basis on which to call a
fight off-card, so it keeps everything rather than guessing.

The same host (site.web.api.espn.com) is what PerpetualCode's worker already
uses for UFC event matching and MMA settlement, so the two ends of the
pipeline read the same source of truth.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from .normalize import surname, surnames_match as _surnames_match

log = logging.getLogger(__name__)

ESPN_SCOREBOARD = (
    "https://site.web.api.espn.com/apis/site/v2/sports/mma/{league}/scoreboard"
    "?dates={dates}"
)
# UFC first: it is the config default and by far the common case. PFL rides
# along because The Odds API's MMA market (and so the site) blends both.
LEAGUES = ("ufc", "pfl")
# A card is fetched a few days back (an event that just happened is still the
# one being reviewed) through several weeks out (cards firm up early).
LOOKBACK_DAYS = 4
LOOKAHEAD_DAYS = 45

CANCELLED_STATUSES = {"STATUS_CANCELED", "STATUS_CANCELLED", "STATUS_POSTPONED"}


def _normalize_event_name(name: str) -> str:
    return " ".join("".join(c.lower() if c.isalnum() else " " for c in (name or "")).split())


def _dates_param(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    fmt = lambda d: d.strftime("%Y%m%d")  # noqa: E731
    return f"{fmt(now - timedelta(days=LOOKBACK_DAYS))}-{fmt(now + timedelta(days=LOOKAHEAD_DAYS))}"


def parse_card(event: dict[str, Any]) -> dict[str, Any]:
    """One ESPN scoreboard event -> the card shape the annotator consumes.

    ESPN's MMA scoreboard lists a card chronologically — earliest bout first,
    main event LAST (confirmed live with UFC 330: the title fight named in
    the event's own title sat at the end of the competitions array). So
    `order` is simply the listing position: 0 = first bout of the night, the
    maximum = the main event.
    """
    competitions = event.get("competitions") or []
    fights = []
    for position, competition in enumerate(competitions):
        competitors = competition.get("competitors") or []
        names = [
            (c.get("athlete") or {}).get("displayName") or ""
            for c in competitors[:2]
        ]
        if len([n for n in names if n]) < 2:
            continue
        status_name = (
            ((competition.get("status") or {}).get("type") or {}).get("name") or ""
        )
        fights.append(
            {
                "fighter_a": names[0],
                "fighter_b": names[1],
                "order": position,
                "date": competition.get("date") or event.get("date"),
                "cancelled": status_name in CANCELLED_STATUSES,
            }
        )
    return {
        "name": event.get("name") or "",
        "date": event.get("date"),
        "fights": fights,
    }


def find_event(scoreboard: dict[str, Any], event_name: str) -> dict[str, Any] | None:
    """The scoreboard event whose name contains the configured event name."""
    wanted = _normalize_event_name(event_name)
    if not wanted:
        return None
    for event in scoreboard.get("events") or []:
        if wanted in _normalize_event_name(event.get("name")):
            return parse_card(event)
    return None


def fetch_event_card(
    event_name: str,
    session: requests.Session | None = None,
    timeout: float = 20.0,
) -> dict[str, Any] | None:
    """The official card for the named event, or None when it can't be found.

    Never raises for network or shape problems — the caller treats None as
    "annotate nothing this run".
    """
    session = session or requests.Session()
    dates = _dates_param()
    for league in LEAGUES:
        url = ESPN_SCOREBOARD.format(league=league, dates=dates)
        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            card = find_event(response.json(), event_name)
        except (requests.exceptions.RequestException, ValueError) as exc:
            log.warning("Card fetch failed for %s: %s: %s", league, type(exc).__name__, exc)
            continue
        if card and card["fights"]:
            log.info(
                "Official card: %s — %d bouts", card["name"], len(card["fights"])
            )
            return card
    log.warning("No ESPN card found matching %r; consensus left un-annotated", event_name)
    return None


def _card_key(fight: dict[str, Any]) -> tuple[str, str]:
    pair = sorted([surname(fight["fighter_a"]), surname(fight["fighter_b"])])
    return (pair[0], pair[1])


def _match_card_fight(fight: dict[str, Any], card_fights: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The card bout this consensus fight is, typo-tolerantly, if any."""
    a = surname(fight.get("fighter_a", ""))
    b = surname(fight.get("fighter_b", ""))
    for bout in card_fights:
        ca, cb = surname(bout["fighter_a"]), surname(bout["fighter_b"])
        if (_surnames_match(a, ca) and _surnames_match(b, cb)) or (
            _surnames_match(a, cb) and _surnames_match(b, ca)
        ):
            return bout
    return None


def _refresh_totals(payload: dict[str, Any]) -> None:
    """Recount the headline totals over the fights that survived the card
    filter, so "21 cappers across 61 fights" describes the payload the reader
    is actually looking at rather than everything the transcripts mentioned."""
    fights = payload.get("fights") or []
    cappers: set[str] = set()
    videos: set[str] = set()
    picks = 0
    for fight in fights:
        for market in fight.get("markets") or []:
            for option in market.get("options") or []:
                for capper in option.get("cappers") or []:
                    picks += 1
                    if capper.get("id"):
                        cappers.add(capper["id"])
                    if capper.get("video_url"):
                        videos.add(capper["video_url"])
    payload.setdefault("totals", {}).update(
        fights=len(fights), picks=picks, cappers=len(cappers), videos=len(videos)
    )


def annotate_consensus(
    payload: dict[str, Any],
    card: dict[str, Any] | None,
    previous_fights: list[dict[str, Any]] | None = None,
    previous_card_name: str = "",
) -> None:
    """Reduce the consensus to this event's card, in place.

    Fights matching a bout on the card are stamped with card_status /
    card_order and kept; everything else is dropped. `previous_fights` is the
    prior run's payload["fights"], used to catch the quiet cancellation: a
    bout ESPN simply removes from the card (rather than marking canceled) was
    on_card last run and unmatched now — that is a cancellation the user
    should see, so it is kept and flagged rather than dropped with the rest.

    That carry-over only makes sense within one event. `previous_card_name` is
    the card the prior payload was built for, and the bouts are carried over
    only when it is the same card as this run's. Retargeting the engine to the
    next event used to resurrect the whole previous card as "cancelled" —
    every bout was on_card last run and unmatched now, so a Contender Series
    card sat on the Fight Night dashboard indefinitely, immune to the off-card
    filter that exists to prevent exactly that. An unnamed previous card is
    treated as a different one: without proof it is the same event, dropping
    an unmatched fight is the safe direction.
    """
    if not card:
        return

    previously_on_card: set[tuple[str, str]] = set()
    same_card = bool(previous_card_name) and _normalize_event_name(
        previous_card_name
    ) == _normalize_event_name(card["name"])
    if same_card:
        for fight in previous_fights or []:
            if fight.get("card_status") in ("on_card", "cancelled"):
                previously_on_card.add(_card_key(fight))
    elif previous_fights:
        log.info(
            "Previous payload was for %r, not %r — its bouts are a different "
            "event's and are not carried over",
            previous_card_name or "(un-annotated)", card["name"],
        )

    matched_orders: set[int] = set()
    kept: list[dict[str, Any]] = []
    dropped = 0
    for fight in payload.get("fights") or []:
        bout = _match_card_fight(fight, card["fights"])
        if bout is None:
            if _card_key(fight) in previously_on_card:
                fight["card_status"] = "cancelled"
                kept.append(fight)
            else:
                dropped += 1
            continue
        kept.append(fight)
        matched_orders.add(bout["order"])
        fight["card_status"] = "cancelled" if bout["cancelled"] else "on_card"
        fight["card_order"] = bout["order"]
        fight["card_date"] = bout.get("date")
        # Adopt ESPN's clean spellings — the consensus display is built from
        # auto-captions and keeps whatever garble was most common.
        aligned = _surnames_match(
            surname(fight.get("fighter_a", "")), surname(bout["fighter_a"])
        )
        clean_a = bout["fighter_a"] if aligned else bout["fighter_b"]
        clean_b = bout["fighter_b"] if aligned else bout["fighter_a"]
        fight["fighter_a"], fight["fighter_b"] = clean_a, clean_b
        fight["display"] = f"{clean_a} vs {clean_b}"

    payload["fights"] = kept

    # The rest of the card, so the dashboard shows every bout — a fight
    # nobody picked is still a fight the reader wants to see listed.
    for bout in card["fights"]:
        if bout["order"] in matched_orders:
            continue
        kept.append(
            {
                "fight_id": "|".join(_card_key(bout)),
                "display": f"{bout['fighter_a']} vs {bout['fighter_b']}",
                "fighter_a": bout["fighter_a"],
                "fighter_b": bout["fighter_b"],
                "pick_count": 0,
                "capper_count": 0,
                "markets": [],
                "card_status": "cancelled" if bout["cancelled"] else "on_card",
                "card_order": bout["order"],
                "card_date": bout.get("date"),
            }
        )

    if dropped:
        log.info(
            "Dropped %d fight(s) not on %s — picks for other events and "
            "transcript garbles", dropped, card["name"],
        )
    _refresh_totals(payload)

    payload.setdefault("event", {})["card"] = {
        "source": "espn",
        "name": card["name"],
        "date": card.get("date"),
        "bouts": len(card["fights"]),
        "off_card_dropped": dropped,
    }
