"""Tests for auto_event.py: self-retargeting config.json in "auto" mode.

find_next_event and find_tracker_roundup are stubbed out throughout — these
tests are only about what resolve_auto_event / resolve_tracker_roundup do
with what they're told the next event / roundup is.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from mma_engine import auto_event
from mma_engine.discover import DiscoveredVideo


def write_config(tmp_path: Path, event: dict, extra: dict | None = None) -> Path:
    payload = {
        "event": event,
        "settings": {"discovery": {"title_contains": ["stale"], "search": {"queries": ["stale query"]}}},
        "tracker": {"picks_videos": ["https://youtu.be/oldRoundupVid"]},
        "cappers": [],
    }
    if extra:
        payload.update(extra)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def fake_card(name="UFC Fight Night: New Card", league="ufc"):
    return {
        "name": name,
        "league": league,
        "date": "2026-09-10T22:00Z",
        "id": "ufc_fight_night_new_card",
        "fights": [
            {"fighter_a": "Some Prelim", "fighter_b": "Another Prelim", "order": 0},
            {"fighter_a": "Dan Hooker", "fighter_b": "Manuel Parnasse", "order": 1},
        ],
    }


def test_not_auto_mode_is_a_no_op(tmp_path):
    path = write_config(tmp_path, {"name": "Hand-Picked Card", "league": "ufc"})
    before = path.read_text(encoding="utf-8")
    assert auto_event.resolve_auto_event(path) is False
    assert path.read_text(encoding="utf-8") == before


def test_auto_mode_retargets_name_league_date(tmp_path, monkeypatch):
    monkeypatch.setattr(auto_event, "find_next_event", lambda leagues: fake_card())
    path = write_config(tmp_path, {"mode": "auto", "name": "Old Card", "league": "ufc"})

    assert auto_event.resolve_auto_event(path) is True

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["event"] == {
        "mode": "auto",
        "name": "UFC Fight Night: New Card",
        "league": "ufc",
        "date": "2026-09-10T22:00Z",
        "notes": "",
    }


def test_auto_mode_regenerates_discovery_keywords_from_the_main_event(tmp_path, monkeypatch):
    monkeypatch.setattr(auto_event, "find_next_event", lambda leagues: fake_card())
    path = write_config(tmp_path, {"mode": "auto", "name": "Old Card"})

    auto_event.resolve_auto_event(path)

    raw = json.loads(path.read_text(encoding="utf-8"))
    discovery = raw["settings"]["discovery"]
    # The main event is the LAST fight in ESPN's listing order.
    assert discovery["title_contains"] == ["hooker", "parnasse"]
    assert discovery["search"]["queries"] == [
        "UFC Fight Night: New Card predictions",
        "Dan Hooker Manuel Parnasse picks",
        "UFC Fight Night: New Card betting breakdown",
    ]


def test_auto_mode_clears_stale_roundup_url(tmp_path, monkeypatch):
    monkeypatch.setattr(auto_event, "find_next_event", lambda leagues: fake_card())
    path = write_config(tmp_path, {"mode": "auto", "name": "Old Card"})

    auto_event.resolve_auto_event(path)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["tracker"]["picks_videos"] == []


def test_auto_mode_is_a_no_op_when_the_next_event_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(auto_event, "find_next_event", lambda leagues: fake_card())
    path = write_config(tmp_path, {"mode": "auto", "name": "UFC Fight Night: New Card"})
    before = path.read_text(encoding="utf-8")

    assert auto_event.resolve_auto_event(path) is False
    assert path.read_text(encoding="utf-8") == before


def test_auto_mode_leaves_config_alone_when_nothing_upcoming_is_found(tmp_path, monkeypatch):
    monkeypatch.setattr(auto_event, "find_next_event", lambda leagues: None)
    path = write_config(tmp_path, {"mode": "auto", "name": "Old Card"})
    before = path.read_text(encoding="utf-8")

    assert auto_event.resolve_auto_event(path) is False
    assert path.read_text(encoding="utf-8") == before


def test_missing_config_file_is_a_no_op(tmp_path):
    assert auto_event.resolve_auto_event(tmp_path / "nope.json") is False


def test_league_pin_is_passed_through_to_find_next_event(tmp_path, monkeypatch):
    seen = {}

    def fake_find(leagues):
        seen["leagues"] = leagues
        return fake_card(league="pfl")

    monkeypatch.setattr(auto_event, "find_next_event", fake_find)
    path = write_config(tmp_path, {"mode": "auto", "name": "Old Card", "league": "pfl"})

    auto_event.resolve_auto_event(path)

    assert seen["leagues"] == ("pfl",)


# -- resolve_tracker_roundup --------------------------------------------------


def write_tracker_config(tmp_path: Path, tracker: dict, discovery_title_contains=("hooker", "parnasse")) -> Path:
    payload = {
        "event": {"name": "UFC Fight Night: Hooker vs. Parnasse"},
        "settings": {"discovery": {"title_contains": list(discovery_title_contains)}},
        "tracker": tracker,
        "cappers": [],
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def fake_video(video_id="MgD40FZsow0", title="Overview of ALL PREDICTIONS"):
    return DiscoveredVideo(
        video_id=video_id,
        capper_id="_tracker_roundup_scan",
        url=f"https://www.youtube.com/watch?v={video_id}",
        title=title,
        published=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )


def test_auto_discover_off_by_default_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "fake-key")
    monkeypatch.setattr(auto_event, "find_tracker_roundup", lambda **kw: fake_video())
    path = write_tracker_config(tmp_path, {"channel_url": "https://www.youtube.com/@X"})

    assert auto_event.resolve_tracker_roundup(path) is False
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["tracker"].get("picks_videos") is None


def test_auto_discover_finds_and_sets_picks_videos(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "fake-key")
    monkeypatch.setattr(auto_event, "find_tracker_roundup", lambda **kw: fake_video())
    path = write_tracker_config(
        tmp_path, {"channel_url": "https://www.youtube.com/@X", "auto_discover": True}
    )

    assert auto_event.resolve_tracker_roundup(path) is True
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["tracker"]["picks_videos"] == ["https://www.youtube.com/watch?v=MgD40FZsow0"]


def test_auto_discover_does_not_overwrite_an_existing_url(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "fake-key")
    monkeypatch.setattr(auto_event, "find_tracker_roundup", lambda **kw: fake_video())
    path = write_tracker_config(
        tmp_path,
        {
            "channel_url": "https://www.youtube.com/@X",
            "auto_discover": True,
            "picks_videos": ["https://youtu.be/alreadySetXX"],
        },
    )

    assert auto_event.resolve_tracker_roundup(path) is False
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["tracker"]["picks_videos"] == ["https://youtu.be/alreadySetXX"]


def test_auto_discover_needs_a_youtube_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.setattr(auto_event, "find_tracker_roundup", lambda **kw: fake_video())
    path = write_tracker_config(
        tmp_path, {"channel_url": "https://www.youtube.com/@X", "auto_discover": True}
    )

    assert auto_event.resolve_tracker_roundup(path) is False


def test_auto_discover_needs_discovery_keywords(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "fake-key")
    monkeypatch.setattr(auto_event, "find_tracker_roundup", lambda **kw: fake_video())
    path = write_tracker_config(
        tmp_path,
        {"channel_url": "https://www.youtube.com/@X", "auto_discover": True},
        discovery_title_contains=(),
    )

    assert auto_event.resolve_tracker_roundup(path) is False


def test_auto_discover_returns_false_when_nothing_matches(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "fake-key")
    monkeypatch.setattr(auto_event, "find_tracker_roundup", lambda **kw: None)
    path = write_tracker_config(
        tmp_path, {"channel_url": "https://www.youtube.com/@X", "auto_discover": True}
    )

    assert auto_event.resolve_tracker_roundup(path) is False


def test_auto_discover_fails_open_on_error(tmp_path, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("network exploded")

    monkeypatch.setenv("YOUTUBE_API_KEY", "fake-key")
    monkeypatch.setattr(auto_event, "find_tracker_roundup", boom)
    path = write_tracker_config(
        tmp_path, {"channel_url": "https://www.youtube.com/@X", "auto_discover": True}
    )
    before = path.read_text(encoding="utf-8")

    assert auto_event.resolve_tracker_roundup(path) is False
    assert path.read_text(encoding="utf-8") == before
