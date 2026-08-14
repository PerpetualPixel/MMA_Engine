"""Live moneyline prices for the card's bouts, from The Odds API.

The consensus tells you who the cappers like. It never told you what the bet
pays, so a parlay built on the dashboard had to be priced by hand in a
sportsbook app. This module closes that gap: it reads the current h2h
(moneyline) market for the event's bouts and stamps a price onto each fight,
which the dashboard multiplies out into a ticket price.

    fight["live_odds"] = {
        "source": "the-odds-api",
        "fetched_at": "2026-08-14T04:12:00+00:00",
        "commence_time": "2026-08-15T22:00:00Z",
        "a": {"american": -450, "decimal": 1.22, "books": 8, "best_american": -420},
        "b": {"american": 340, "decimal": 4.4, "books": 8, "best_american": 360},
    }

`a` / `b` follow the fight's own fighter_a / fighter_b, so the dashboard never
has to re-match names. The headline price is the MEDIAN across books, for the
same reason the Straights tab medians the cappers' quotes: one book hanging a
stale number shouldn't move the shown price. `best_american` keeps the best
available number alongside it, since a bettor shopping the line wants that one.

Scope, stated plainly: The Odds API carries h2h for MMA, and that is all this
module claims. Method-of-victory, rounds and props are not in the feed, so
those legs keep falling back to the prices cappers quoted in their videos
(see weighted_picks.quoted_odds and the dashboard's optionQuotedOdds). A
dashboard leg therefore knows whether its price is live or quoted, and says so.

Fail-open by design, exactly like event_card: no API key, an unreachable host,
a blown monthly quota or an event the feed doesn't carry all mean "no
live_odds this run" — never a failed weekly run. Prices are a nice-to-have on
top of a consensus that stands on its own.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from statistics import median
from typing import Any

import requests

from .normalize import surname, surnames_match

log = logging.getLogger(__name__)

# The Odds API v4. MMA is one sport key covering UFC and PFL alike, which is
# why event_card sweeps both leagues — the two ends agree on what "MMA" means.
ODDS_API_URL = "https://api.the-odds-api.com/v4/sports/{sport}/odds"
MMA_SPORT_KEY = "mma_mixed_martial_arts"
MONEYLINE_MARKET = "h2h"

# Returned in the response headers; surfaced so a run that quietly burned the
# monthly free-tier allowance says so in the log instead of just going quiet.
QUOTA_HEADERS = ("x-requests-remaining", "x-requests-used")


def american_to_decimal(american: float) -> float:
    return 1 + american / 100 if american > 0 else 1 + 100 / abs(american)


def decimal_to_american(decimal: float) -> int:
    if decimal >= 2:
        return round((decimal - 1) * 100)
    return -round(100 / (decimal - 1)) if decimal > 1 else 0


def _clean_american(value: Any) -> int | None:
    """A usable American price, or None. Books occasionally emit 0/None for a
    market they aren't currently pricing, and |price| < 100 is not a real
    American line."""
    try:
        price = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return price if abs(price) >= 100 else None


def consensus_price(americans: list[int]) -> dict[str, Any] | None:
    """Median price across books, plus the best number on offer.

    "Best" is the longest price: highest decimal payout, whichever side of
    even money it sits on.
    """
    usable = [a for a in americans if a is not None]
    if not usable:
        return None
    decimals = sorted(american_to_decimal(a) for a in usable)
    mid = median(decimals)
    return {
        "american": decimal_to_american(mid),
        "decimal": round(mid, 4),
        "books": len(usable),
        "best_american": decimal_to_american(max(decimals)),
    }


def parse_odds_events(payload: Any) -> list[dict[str, Any]]:
    """The Odds API's /odds response -> one entry per event with per-fighter
    price lists.

    Shape consumed (v4):

        [{"id", "commence_time", "home_team", "away_team",
          "bookmakers": [{"key", "markets": [
              {"key": "h2h", "outcomes": [{"name", "price"}, ...]}]}]}]

    Anything malformed is skipped rather than raised on — a single odd event
    in the feed must not cost the whole run its prices.
    """
    events: list[dict[str, Any]] = []
    if not isinstance(payload, list):
        return events
    for event in payload:
        if not isinstance(event, dict):
            continue
        prices: dict[str, list[int]] = {}
        for bookmaker in event.get("bookmakers") or []:
            if not isinstance(bookmaker, dict):
                continue
            for market in bookmaker.get("markets") or []:
                if not isinstance(market, dict) or market.get("key") != MONEYLINE_MARKET:
                    continue
                for outcome in market.get("outcomes") or []:
                    if not isinstance(outcome, dict):
                        continue
                    name = str(outcome.get("name") or "").strip()
                    price = _clean_american(outcome.get("price"))
                    if name and price is not None:
                        prices.setdefault(name, []).append(price)
        if not prices:
            continue
        events.append(
            {
                "id": event.get("id") or "",
                "commence_time": event.get("commence_time"),
                "home_team": event.get("home_team") or "",
                "away_team": event.get("away_team") or "",
                "prices": prices,
            }
        )
    return events


def _price_for(event: dict[str, Any], fighter: str) -> dict[str, Any] | None:
    """The consensus price this event carries for one fighter, matched on
    surname with the same caption-typo tolerance the card matcher uses."""
    target = surname(fighter)
    if not target:
        return None
    collected: list[int] = []
    for name, prices in event["prices"].items():
        if surnames_match(surname(name), target):
            collected.extend(prices)
    return consensus_price(collected)


def match_event(fight: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The feed event that is this bout, if any.

    Matched on the pair of surnames rather than the event's own naming, since
    the feed names an MMA event by its two fighters and the home/away split
    carries no meaning in MMA. When the same pairing appears twice (a
    rescheduled bout left in the feed), the earliest one wins.
    """
    a, b = surname(fight.get("fighter_a", "")), surname(fight.get("fighter_b", ""))
    if not a or not b:
        return None
    matches = []
    for event in events:
        home, away = surname(event["home_team"]), surname(event["away_team"])
        if (surnames_match(a, home) and surnames_match(b, away)) or (
            surnames_match(a, away) and surnames_match(b, home)
        ):
            matches.append(event)
    if not matches:
        return None
    return sorted(matches, key=lambda e: str(e.get("commence_time") or ""))[0]


def fetch_live_odds(
    api_key: str,
    regions: str = "us",
    session: requests.Session | None = None,
    timeout: float = 20.0,
) -> list[dict[str, Any]] | None:
    """Current MMA moneylines, or None when they can't be had.

    Never raises: the caller treats None as "no prices this run".
    """
    if not api_key:
        log.info("No ODDS_API_KEY set — skipping live odds (quoted prices still shown)")
        return None
    session = session or requests.Session()
    url = ODDS_API_URL.format(sport=MMA_SPORT_KEY)
    params = {
        "apiKey": api_key,
        "regions": regions,
        "markets": MONEYLINE_MARKET,
        "oddsFormat": "american",
    }
    try:
        response = session.get(url, params=params, timeout=timeout)
        response.raise_for_status()
        events = parse_odds_events(response.json())
    except (requests.exceptions.RequestException, ValueError) as exc:
        log.warning("Live odds fetch failed: %s: %s", type(exc).__name__, exc)
        return None
    remaining = response.headers.get("x-requests-remaining")
    if remaining is not None:
        log.info("Live odds: %d events priced (%s API requests left)", len(events), remaining)
    else:
        log.info("Live odds: %d events priced", len(events))
    return events


def annotate_odds(
    payload: dict[str, Any],
    events: list[dict[str, Any]] | None,
    fetched_at: str | None = None,
) -> int:
    """Stamp `live_odds` onto every fight the feed prices, in place.

    Returns how many fights got a price, which the pipeline logs and the
    payload records so the dashboard can say where its numbers came from.
    """
    if not events:
        return 0
    stamp = fetched_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    matched = 0
    for fight in payload.get("fights") or []:
        # A cancelled bout has no market left to price, and is excluded from
        # parlays anyway. (Fights for other events never reach here — the
        # card annotation drops them before this runs.)
        if fight.get("card_status") == "cancelled":
            continue
        # Two fighters reducing to the same surname is normalize.py's known
        # collision. Elsewhere it merely over-groups; here it would map one
        # side's price onto both and show the favourite's number on the dog.
        # A missing price is recoverable, a confidently wrong one is not.
        if surname(fight.get("fighter_a", "")) == surname(fight.get("fighter_b", "")):
            log.warning(
                "Shared surname in %r — skipping live odds rather than risk "
                "mispricing a side", fight.get("display") or fight.get("fight_id"),
            )
            continue
        event = match_event(fight, events)
        if not event:
            continue
        price_a = _price_for(event, fight.get("fighter_a", ""))
        price_b = _price_for(event, fight.get("fighter_b", ""))
        if not price_a and not price_b:
            continue
        fight["live_odds"] = {
            "source": "the-odds-api",
            "fetched_at": stamp,
            "commence_time": event.get("commence_time"),
            "a": price_a,
            "b": price_b,
        }
        matched += 1

    payload.setdefault("event", {})["odds"] = {
        "source": "the-odds-api",
        "market": MONEYLINE_MARKET,
        "fetched_at": stamp,
        "fights_priced": matched,
    }
    return matched
