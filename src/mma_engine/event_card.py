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


def card_id(name: str) -> str:
    """A stable id for a card, derived from ESPN's name for it."""
    return "_".join(_normalize_event_name(name).split())[:60] or "card"


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
    leagues: tuple[str, ...] | None = None,
) -> dict[str, Any] | None:
    """The official card for the named event, or None when it can't be found.

    `leagues` restricts which scoreboards are searched — pass ("pfl",) for a
    PFL card so a same-named UFC event can't win the race, and leave it unset
    to try UFC then PFL as before.

    Never raises for network or shape problems — the caller treats None as
    "annotate nothing this run".
    """
    session = session or requests.Session()
    dates = _dates_param()
    for league in leagues or LEAGUES:
        url = ESPN_SCOREBOARD.format(league=league, dates=dates)
        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            card = find_event(response.json(), event_name)
        except (requests.exceptions.RequestException, ValueError) as exc:
            log.warning("Card fetch failed for %s: %s: %s", league, type(exc).__name__, exc)
            continue
        if card and card["fights"]:
            card["league"] = league
            card["id"] = card_id(card["name"])
            log.info(
                "Official card: %s — %d bouts (%s)",
                card["name"], len(card["fights"]), league.upper(),
            )
            return card
    log.warning("No ESPN card found matching %r; consensus left un-annotated", event_name)
    return None


def fetch_event_cards(
    specs: list[dict[str, Any]],
    session: requests.Session | None = None,
    timeout: float = 20.0,
) -> list[dict[str, Any]]:
    """Every configured event's official card, in configured order.

    One run can cover more than one card — a UFC Fight Night and the PFL event
    the same weekend — so the dashboard can offer them side by side rather than
    forcing a choice at config time. A spec that can't be found is logged and
    skipped; the cards that were found still filter the consensus.
    """
    session = session or requests.Session()
    cards: list[dict[str, Any]] = []
    for spec in specs:
        name = (spec.get("name") or "").strip()
        if not name:
            continue
        league = (spec.get("league") or "").strip().lower()
        card = fetch_event_card(
            name,
            session=session,
            timeout=timeout,
            leagues=(league,) if league else None,
        )
        if card:
            # What the config called it, for the dashboard's selector.
            card["label"] = (spec.get("label") or "").strip() or card["name"]
            cards.append(card)
    return cards


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


def _relabel_options(fight: dict[str, Any], old: tuple[str, str], clean: tuple[str, str]) -> None:
    """Carry ESPN's spelling into the option labels, not just the heading.

    The heading is rebuilt from the card, but every option keeps whatever the
    cappers called the fighter — so a bout headed "Lerryan Douglas vs Jamall
    Emmers" was showing "Larion Douglas 89.7%" underneath it. A moneyline
    option is exactly a fighter name, so it can be replaced outright once the
    surnames match; other markets wrap the name in wording of their own ("X by
    KO/TKO"), so there only the name itself is swapped, and only where the
    old spelling actually appears.
    """
    for market in fight.get("markets") or []:
        for option in market.get("options") or []:
            label = option.get("selection", "")
            if not label:
                continue
            if market.get("bet_type") == "moneyline":
                for was, now in zip(old, clean):
                    if _surnames_match(surname(label), surname(was)):
                        option["selection"] = now
                        break
                continue
            for was, now in zip(old, clean):
                if was and was.lower() in label.lower():
                    start = label.lower().index(was.lower())
                    label = label[:start] + now + label[start + len(was):]
            option["selection"] = label


def _option_weight(option: dict[str, Any]) -> float:
    """An option's weight, recomputed from its backers.

    weight = trust x (confidence / 10), summed — the same arithmetic
    aggregate.py does, redone here because merging two entries for one bout
    changes who is backing what.
    """
    return sum(
        float(capper.get("trust", 0.0)) * float(capper.get("confidence", 0)) / 10.0
        for capper in option.get("cappers") or []
    )


def _restat_option(option: dict[str, Any]) -> None:
    """Recount an option from its capper list."""
    cappers = option.get("cappers") or []
    option["weight"] = round(_option_weight(option), 2)
    option["pick_count"] = len(cappers)
    if cappers:
        option["avg_confidence"] = round(
            sum(float(c.get("confidence", 0)) for c in cappers) / len(cappers), 1
        )
    stated = [c for c in cappers if c.get("source", "video") != "tracker"]
    option["stated_pick_count"] = len(stated)
    option["stated_avg_confidence"] = (
        round(sum(float(c.get("confidence", 0)) for c in stated) / len(stated), 1)
        if stated
        else 0.0
    )


def _merge_fight(target: dict[str, Any], extra: dict[str, Any]) -> None:
    """Fold one consensus fight into another for the same bout, in place.

    Both entries are already relabelled to ESPN's spellings, so options line
    up on their labels. A capper appearing on both sides of the merge votes
    once, with their stronger statement — the same rule aggregation applies
    within a fight. Everything downstream of the capper lists (weights,
    counts, consensus shares) is recomputed rather than added up, so the
    merged fight reads exactly as it would have had the two spellings never
    diverged.
    """
    markets = {market.get("bet_type"): market for market in target.get("markets") or []}

    for market in extra.get("markets") or []:
        into = markets.get(market.get("bet_type"))
        if into is None:
            target.setdefault("markets", []).append(market)
            markets[market.get("bet_type")] = market
            continue

        options = {
            (option.get("selection") or "").strip().casefold(): option
            for option in into.get("options") or []
        }
        for option in market.get("options") or []:
            key = (option.get("selection") or "").strip().casefold()
            match = options.get(key)
            if match is None:
                into.setdefault("options", []).append(option)
                options[key] = option
                continue
            by_id = {c.get("id"): c for c in match.get("cappers") or []}
            for capper in option.get("cappers") or []:
                seen = by_id.get(capper.get("id"))
                weight = lambda c: float(c.get("trust", 0)) * float(c.get("confidence", 0))
                if seen is None or weight(capper) > weight(seen):
                    by_id[capper.get("id")] = capper
            match["cappers"] = sorted(
                by_id.values(),
                key=lambda c: (float(c.get("trust", 0)) * float(c.get("confidence", 0)), c.get("name", "")),
                reverse=True,
            )

    for market in target.get("markets") or []:
        for option in market.get("options") or []:
            _restat_option(option)
        total = sum(float(o.get("weight", 0.0)) for o in market.get("options") or [])
        market["total_weight"] = round(total, 2)
        market["pick_count"] = sum(int(o.get("pick_count", 0)) for o in market.get("options") or [])
        for option in market.get("options") or []:
            option["consensus_pct"] = (
                round(float(option["weight"]) / total * 100.0, 1) if total else 0.0
            )
        market["options"] = sorted(
            market.get("options") or [],
            key=lambda o: (o.get("weight", 0), o.get("pick_count", 0)),
            reverse=True,
        )

    target["pick_count"] = sum(
        int(market.get("pick_count", 0)) for market in target.get("markets") or []
    )
    target["capper_count"] = len(
        {
            capper.get("id")
            for market in target.get("markets") or []
            for option in market.get("options") or []
            for capper in option.get("cappers") or []
        }
    )


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
    cards: list[dict[str, Any]] | dict[str, Any] | None,
    previous_fights: list[dict[str, Any]] | None = None,
    previous_card_name: str = "",
) -> None:
    """Reduce the consensus to this run's official card(s), in place.

    Fights matching a bout are stamped with event_id / card_status /
    card_order and kept; everything else is dropped. More than one card can be
    passed — a UFC Fight Night and the PFL event the same weekend — and each
    fight is tagged with the card it belongs to so the dashboard can show them
    separately.

    `previous_fights` is the prior run's payload["fights"], used to catch the
    quiet cancellation: a bout ESPN simply removes from the card (rather than
    marking canceled) was on_card last run and unmatched now — a cancellation
    the reader should see, so it is kept and flagged rather than dropped.

    Two things bound that carry-over, both learned the hard way. It only
    applies within one event: retargeting the engine used to resurrect the
    whole previous card as "cancelled", since every bout was on_card last run
    and unmatched now. And a cancellation carries a stamp of the card it was
    called on (`cancelled_from_card`), because without one the carry-over feeds
    itself — a flagged fight is in the next run's previous payload as
    cancelled, which was enough to flag it again, for ever.
    """
    cards = [cards] if isinstance(cards, dict) else list(cards or [])
    if not cards:
        return

    # key -> the card it was on, taken from the PREVIOUS payload's copy: this
    # run's fight has no stamp of its own yet.
    previously_on_card: dict[tuple[str, str], dict[str, Any]] = {}
    by_id = {card.get("id") or card_id(card["name"]): card for card in cards}
    by_name = {_normalize_event_name(card["name"]): card for card in cards}
    for fight in previous_fights or []:
        status = fight.get("card_status")
        if status not in ("on_card", "cancelled"):
            continue
        # Which card that fight was on: its own stamp when it has one, the
        # caller's word for it on payloads written before cards were tagged.
        event_id = fight.get("event_id")
        if status == "cancelled":
            # The stamp is what proves it was ever on this card, and what
            # stops the flag re-deriving itself run after run.
            home = by_name.get(
                _normalize_event_name(fight.get("cancelled_from_card", ""))
            )
        elif event_id:
            home = by_id.get(event_id)
        else:
            # A payload written before cards were tagged: the caller's word
            # for which card it was built against is all there is.
            home = by_name.get(_normalize_event_name(previous_card_name))
        if home is not None:
            previously_on_card[_card_key(fight)] = home

    matched: dict[tuple[str, int], dict[str, Any]] = {}
    kept: list[dict[str, Any]] = []
    dropped = 0
    merged = 0

    for fight in payload.get("fights") or []:
        bout = card = None
        for candidate in cards:
            bout = _match_card_fight(fight, candidate["fights"])
            if bout is not None:
                card = candidate
                break

        if bout is None:
            home = previously_on_card.get(_card_key(fight))
            if home is not None:
                fight["card_status"] = "cancelled"
                fight["cancelled_from_card"] = home["name"]
                fight["event_id"] = home.get("id") or card_id(home["name"])
                kept.append(fight)
            else:
                dropped += 1
            continue

        this_id = card.get("id") or card_id(card["name"])
        fight["card_status"] = "cancelled" if bout["cancelled"] else "on_card"
        fight["card_order"] = bout["order"]
        fight["card_date"] = bout.get("date")
        fight["event_id"] = this_id
        # Adopt ESPN's clean spellings — the consensus display is built from
        # auto-captions and keeps whatever garble was most common.
        aligned = _surnames_match(
            surname(fight.get("fighter_a", "")), surname(bout["fighter_a"])
        )
        clean_a = bout["fighter_a"] if aligned else bout["fighter_b"]
        clean_b = bout["fighter_b"] if aligned else bout["fighter_a"]
        _relabel_options(
            fight,
            (fight.get("fighter_a", ""), fight.get("fighter_b", "")),
            (clean_a, clean_b),
        )
        fight["fighter_a"], fight["fighter_b"] = clean_a, clean_b
        fight["display"] = f"{clean_a} vs {clean_b}"

        # ESPN says this is one bout, so it is one fight. Two consensus
        # entries reach the same bout when the cappers' spellings diverge far
        # enough to survive the surname canonicalizer ("Schultz" / "Schiltz"),
        # and the card was rendering the bout twice — once with 22 picks and
        # once with 2. Fold the second into the first.
        slot = (this_id, bout["order"])
        existing = matched.get(slot)
        if existing is None:
            matched[slot] = fight
            kept.append(fight)
        else:
            _merge_fight(existing, fight)
            merged += 1

    if merged:
        log.info(
            "Merged %d consensus fight(s) into a bout already matched — the "
            "same fight under two spellings", merged,
        )
    payload["fights"] = kept

    # The rest of each card, so the dashboard shows every bout — a fight
    # nobody picked is still a fight the reader wants to see listed.
    for card in cards:
        this_id = card.get("id") or card_id(card["name"])
        for bout in card["fights"]:
            if (this_id, bout["order"]) in matched:
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
                    "event_id": this_id,
                }
            )

    if dropped:
        log.info(
            "Dropped %d fight(s) not on %s — picks for other events and "
            "transcript garbles",
            dropped,
            " / ".join(card["name"] for card in cards),
        )
    _refresh_totals(payload)

    payload["events"] = [
        {
            "id": card.get("id") or card_id(card["name"]),
            "name": card["name"],
            "label": card.get("label") or card["name"],
            "league": (card.get("league") or "").upper(),
            "date": card.get("date"),
            "bouts": len(card["fights"]),
        }
        for card in cards
    ]
    # The primary card stays where it has always been, so anything reading
    # docs/data.json for a single event keeps working.
    primary = cards[0]
    payload.setdefault("event", {})["card"] = {
        "source": "espn",
        "name": primary["name"],
        "date": primary.get("date"),
        "bouts": len(primary["fights"]),
        "off_card_dropped": dropped,
    }
