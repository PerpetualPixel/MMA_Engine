"""Self-retargeting: resolve config.json's event (and tracker roundup) automatically.

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

Once the event is settled, `resolve_tracker_roundup` does the same for
`tracker.picks_videos` — finding this week's pre-event roundup on the
tracker's own channel (see tracker_picks.find_tracker_roundup) instead of
someone pasting a screenshot-read URL in by hand. Also opt-in, via
`tracker.auto_discover: true`.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from .event_card import LEAGUES, find_next_event
from .normalize import surname
from .tracker_picks import find_tracker_roundup

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


def resolve_tracker_roundup(config_path: str | Path) -> bool:
    """Auto-discover this week's tracker roundup, if enabled and none is set.

    Opt-in via `tracker.auto_discover: true` — the same "off unless you ask
    for it" contract as `event.mode: auto`. Only runs when
    `tracker.picks_videos` is empty: a URL already there, whether typed by
    hand or found by an earlier run this week, is left alone rather than
    silently replaced. Reuses `discovery.title_contains` (the current
    event's fighter surnames, kept fresh by `resolve_auto_event` above) as
    the filter, so it costs nothing beyond the `YOUTUBE_API_KEY` capper
    discovery already needs.

    Returns True when a video was found and written. False covers
    everything else — auto-discovery off, a URL already set, no API key, no
    keywords yet, or nothing matched — and never raises: a problem here
    costs this week's auto-discovery, never the run.
    """
    config_path = Path(config_path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False

    tracker = raw.get("tracker") or {}
    if not tracker.get("auto_discover"):
        return False
    if tracker.get("picks_videos"):
        return False

    api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not api_key:
        return False

    title_contains = (raw.get("settings") or {}).get("discovery", {}).get("title_contains") or []
    if isinstance(title_contains, str):
        title_contains = [title_contains]
    if not title_contains:
        return False

    try:
        video = find_tracker_roundup(
            channel_url=tracker.get("channel_url", ""),
            channel_id=tracker.get("channel_id", ""),
            title_contains=list(title_contains),
            api_key=api_key,
        )
    except Exception:
        log.exception("Tracker roundup discovery failed — leaving tracker.picks_videos empty.")
        return False
    if video is None:
        return False

    log.info("Auto-discovered this week's tracker roundup: %s (%s)", video.title, video.url)
    raw.setdefault("tracker", {})["picks_videos"] = [video.url]
    config_path.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return True


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point: `python -m mma_engine.auto_event --config config.json`.

    Exists so weekly.ps1 can retarget and commit+push config.json as its own
    small, fast step BEFORE the video-processing loop — which can run 20-30+
    minutes and is the part that actually fails (a spent API balance, a
    YouTube IP block). Bundling the retarget into that same run's one big
    commit-at-the-end meant an interruption anywhere in the loop left the
    retarget sitting uncommitted, which then blocked the *next* run's
    `git pull --ff-only` with a local-changes conflict. Retargeting (and
    committing) first means an interrupted run downstream never leaves
    config.json in a state the next run can't cleanly pull past.

    Also runs `resolve_tracker_roundup`, for the same reason and at the same
    low cost: finding this week's tracker roundup is one more small API call,
    not another 20-minute pass through the video queue.

    Always exits 0 — like both resolve functions, a problem here should cost
    the run its auto-retarget or auto-discovery, never the run.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description="Retarget config.json to the next event, if event.mode is \"auto\"."
    )
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    args = parser.parse_args(argv)

    retargeted = resolve_auto_event(args.config)
    found_roundup = resolve_tracker_roundup(args.config)
    if not retargeted and not found_roundup:
        print("unchanged")
    else:
        if retargeted:
            print("retargeted")
        if found_roundup:
            print("found_roundup")
    return 0


if __name__ == "__main__":
    sys.exit(main())
