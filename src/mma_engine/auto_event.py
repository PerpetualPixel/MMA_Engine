"""Self-retargeting: resolve config.json's event automatically each run.

Every retarget so far in this project's life has been the same manual chore:
find the next card's name, edit "event", rewrite discovery.title_contains and
search.queries to the new fighters, and clear tracker.picks_videos so a stale
roundup URL doesn't burn a video download and a vision pass on last week's
deck. Set "event" to {"mode": "auto"} and this module does that chore itself,
before config.json is even loaded — the pipeline always runs against whatever
ESPN says is next, rolling over to the following week's card on its own once
one event's date has passed.

A name typed by hand (no "mode") is left completely alone: auto mode is
opt-in, so a Contender Series week (or any card that wants a hand-picked
match rather than "whatever's soonest") keeps working exactly as before.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .event_card import LEAGUES, find_next_event
from .normalize import surname

log = logging.getLogger(__name__)


def _search_queries(event_name: str, fighter_a: str, fighter_b: str) -> list[str]:
    return [
        f"{event_name} predictions",
        f"{fighter_a} {fighter_b} picks",
        f"{event_name} betting breakdown",
    ]


def resolve_auto_event(config_path: str | Path) -> bool:
    """Retarget config.json in place if its event is set to auto mode.

    Returns True when the file was rewritten (a new event was resolved).
    False covers every other case — no auto mode, no upcoming event found
    (network trouble, or ESPN has nothing posted yet), or the soonest event
    is the same one config.json already names, so there is nothing to do.
    Never raises: like the rest of the ESPN integration, a problem here
    costs the run its auto-retarget, not the run itself.
    """
    config_path = Path(config_path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False

    event = raw.get("event") or {}
    if (event.get("mode") or "").strip().lower() != "auto":
        return False

    league = (event.get("league") or "").strip().lower()
    leagues = (league,) if league else LEAGUES
    try:
        card = find_next_event(leagues=leagues)
    except Exception:
        log.exception("Auto event resolution failed — leaving config.json as-is.")
        return False
    if card is None:
        return False
    if card["name"] == event.get("name"):
        return False  # same event as last run; nothing to retarget

    fights = card.get("fights") or []
    # ESPN lists a card chronologically, main event last (see event_card.py).
    main_event = fights[-1] if fights else None
    fighter_a = main_event["fighter_a"] if main_event else ""
    fighter_b = main_event["fighter_b"] if main_event else ""

    log.info("Auto-retargeting from %r to %r", event.get("name") or "(none)", card["name"])

    raw["event"] = {
        "mode": "auto",
        "name": card["name"],
        "league": card["league"],
        "date": card.get("date") or "",
        "notes": event.get("notes", ""),
    }

    settings: dict[str, Any] = raw.setdefault("settings", {})
    discovery: dict[str, Any] = settings.setdefault("discovery", {})
    if fighter_a and fighter_b:
        discovery["title_contains"] = sorted(
            {surname(fighter_a).lower(), surname(fighter_b).lower()}
        )
        discovery.setdefault("search", {})["queries"] = _search_queries(
            card["name"], fighter_a, fighter_b
        )

    tracker: dict[str, Any] = raw.setdefault("tracker", {})
    if tracker.get("picks_videos"):
        log.info("Clearing tracker.picks_videos — that roundup was for the old event.")
    tracker["picks_videos"] = []

    config_path.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return True
