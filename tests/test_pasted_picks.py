"""Tests for pasted picks — cards handed to the engine by hand.

Covers filename-to-capper resolution, the source-line header, the staleness
guard, cache-key stability, and the rule that a pasted card supersedes that
capper's own video. No network and no model call.

Run with:  PYTHONPATH=src python -m pytest -q
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mma_engine.aggregate import SourcedPick
from mma_engine.config import Capper, load_config
from mma_engine.extract import Pick
from mma_engine.pasted_picks import (
    collect_notes,
    has_notes,
    note_id,
    parse_note,
    resolve_capper,
    supersede_video_picks,
)


def capper(id_, name, aliases=()):
    return Capper(id=id_, name=name, trust={"overall": 7.0}, aliases=tuple(aliases))


ROSTER = [
    capper("funky_picks", "Funky Picks", aliases=["Funk Picks"]),
    capper("betslam", "BetSlam with Sam"),
]


def write(directory: Path, name: str, text: str, age_days: float = 0.0) -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    if age_days:
        when = time.time() - age_days * 86400
        os.utime(path, (when, when))
    return path


# -- the source header -----------------------------------------------------


@pytest.mark.parametrize(
    "raw,body,url",
    [
        ("https://patreon.com/posts/1\n\nJones by KO", "Jones by KO", "https://patreon.com/posts/1"),
        ("# https://patreon.com/posts/1\nJones by KO", "Jones by KO", "https://patreon.com/posts/1"),
        ("source: Patreon, Aug 20\nJones by KO", "Jones by KO", "Patreon, Aug 20"),
        ("Jones by KO, 2 units", "Jones by KO, 2 units", ""),
        ("", "", ""),
    ],
)
def test_parse_note_splits_an_optional_source_line(raw, body, url):
    assert parse_note(raw) == (body, url)


def test_a_pick_that_merely_mentions_a_url_keeps_its_first_line():
    body, url = parse_note("Jones by KO — see https://example.com for why")
    assert url == ""
    assert body.startswith("Jones by KO")


# -- naming the file -------------------------------------------------------


@pytest.mark.parametrize(
    "stem,expected",
    [
        ("funky_picks", "funky_picks"),      # the id
        ("Funky Picks", "funky_picks"),      # the name
        ("funky picks", "funky_picks"),      # careless casing/spacing
        ("Funk Picks", "funky_picks"),       # a listed alias
        ("BetSlam with Sam", "betslam"),
        ("Nobody At All", None),
        ("", None),
    ],
)
def test_resolve_capper_by_id_name_or_alias(stem, expected):
    resolved = resolve_capper(stem, ROSTER)
    assert (resolved.id if resolved else None) == expected


# -- collecting the folder -------------------------------------------------


def test_collect_reads_a_named_file(tmp_path):
    write(tmp_path, "funky_picks.txt", "https://patreon.com/posts/1\nJones by KO")
    notes, skipped = collect_notes(tmp_path, ROSTER)
    assert skipped == []
    assert len(notes) == 1
    assert notes[0].capper.id == "funky_picks"
    assert notes[0].text == "Jones by KO"
    assert notes[0].source_url == "https://patreon.com/posts/1"


def test_collect_skips_unknown_names_empty_files_and_the_readme(tmp_path):
    write(tmp_path, "README.md", "instructions")
    write(tmp_path, "nobody.txt", "Jones by KO")
    write(tmp_path, "funky_picks.txt", "   \n")
    write(tmp_path, "notes.pdf", "binary-ish")
    notes, skipped = collect_notes(tmp_path, ROSTER)
    assert notes == []
    assert {row["status"] for row in skipped} == {"unknown_capper", "empty"}
    assert {row["file"] for row in skipped} == {"nobody.txt", "funky_picks.txt"}


def test_a_stale_file_is_skipped_and_reported(tmp_path):
    write(tmp_path, "funky_picks.txt", "Jones by KO", age_days=30)
    notes, skipped = collect_notes(tmp_path, ROSTER, max_age_days=14)
    assert notes == []
    assert skipped[0]["status"] == "stale"
    # The guard can be turned off for a card you know is still current.
    notes, skipped = collect_notes(tmp_path, ROSTER, max_age_days=0)
    assert len(notes) == 1 and skipped == []


def test_a_fresh_file_survives_the_guard(tmp_path):
    write(tmp_path, "funky_picks.txt", "Jones by KO", age_days=3)
    notes, _ = collect_notes(tmp_path, ROSTER, max_age_days=14)
    assert len(notes) == 1


def test_missing_folder_is_not_an_error(tmp_path):
    assert collect_notes(tmp_path / "nope", ROSTER) == ([], [])
    assert has_notes(tmp_path / "nope") is False


def test_has_notes_ignores_the_readme(tmp_path):
    write(tmp_path, "README.md", "instructions")
    assert has_notes(tmp_path) is False
    write(tmp_path, "funky_picks.txt", "Jones by KO")
    assert has_notes(tmp_path) is True


# -- cache key -------------------------------------------------------------


def test_note_id_is_stable_for_the_same_text_and_changes_with_it():
    first = note_id("funky_picks", "Jones by KO")
    assert first == note_id("funky_picks", "Jones by KO")
    assert first != note_id("funky_picks", "Jones by decision")
    assert first != note_id("betslam", "Jones by KO")
    assert first.startswith("paste_funky_picks_")


# -- superseding the teaser video ------------------------------------------


def sourced(capper_, a, b, selection, kind="video"):
    return SourcedPick(
        pick=Pick(
            fighter_a=a,
            fighter_b=b,
            bet_type="moneyline",
            selection=selection,
            fighter=selection,
            confidence=7,
            role="unknown",
            odds_american="",
            stake_units="",
            reasoning="",
        ),
        capper=capper_,
        video_id="vid00000001",
        video_url="",
        source_kind=kind,
    )


def test_a_pasted_card_supersedes_that_cappers_video_for_the_same_fight():
    funky, betslam = ROSTER
    existing = [
        sourced(funky, "Jon Jones", "Tom Aspinall", "Tom Aspinall"),
        sourced(funky, "Ilia Topuria", "Max Holloway", "Ilia Topuria"),
        sourced(betslam, "Jon Jones", "Tom Aspinall", "Jon Jones"),
    ]
    pasted = [sourced(funky, "Jon Jones", "Tom Aspinall", "Jon Jones", kind="pasted")]

    kept, dropped = supersede_video_picks(existing, pasted)
    assert dropped == 1
    # Only Funky's teaser pick for that one fight goes; their other fight and
    # everyone else's picks stay.
    assert [(s.capper.id, s.pick.selection) for s in kept] == [
        ("funky_picks", "Ilia Topuria"),
        ("betslam", "Jon Jones"),
    ]


def test_superseding_leaves_roundup_picks_alone():
    """Roundup lines are handled by their own deferral, not this one."""
    funky = ROSTER[0]
    existing = [sourced(funky, "Jon Jones", "Tom Aspinall", "Jon Jones", kind="tracker")]
    pasted = [sourced(funky, "Jon Jones", "Tom Aspinall", "Jon Jones", kind="pasted")]
    kept, dropped = supersede_video_picks(existing, pasted)
    assert (dropped, len(kept)) == (0, 1)


# -- pipeline wiring -------------------------------------------------------


BASE_CONFIG = {
    "event": {"name": "UFC 300"},
    "cappers": [
        {"id": "funky_picks", "name": "Funky Picks", "trust": {"overall": 7.0}},
    ],
}


def test_pipeline_ingests_a_pasted_card(tmp_path, monkeypatch):
    from mma_engine import pipeline
    from mma_engine.extract import ExtractionResult

    class FakeExtractor:
        def __init__(self, **kwargs):
            pass

        def extract(self, video_id, transcript, capper_name, video_url):
            return ExtractionResult(
                video_id=video_id,
                event_name="UFC 300",
                picks=[
                    Pick(
                        fighter_a="Jon Jones",
                        fighter_b="Tom Aspinall",
                        bet_type="moneyline",
                        selection="Jon Jones",
                        fighter="Jon Jones",
                        confidence=9,
                        role="favorite",
                        odds_american="-150",
                        stake_units="2u",
                        reasoning="Wrestling.",
                    )
                ],
            )

    monkeypatch.setattr(pipeline, "PickExtractor", FakeExtractor)
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({**BASE_CONFIG, "settings": {"pasted_picks": {"dir": "pasted"}}}),
        encoding="utf-8",
    )
    (tmp_path / "pasted").mkdir()
    write(tmp_path / "pasted", "funky_picks.txt", "https://patreon.com/p/1\nJones ML")

    config = load_config(config_path)
    # Their teaser video already gave the other side of the same fight.
    picks = [sourced(ROSTER[0], "Jon Jones", "Tom Aspinall", "Tom Aspinall")]
    sources: list[dict] = []
    pipeline.ingest_pasted_picks(config, sourced_picks=picks, sources=sources)

    assert [(s.capper.id, s.pick.selection, s.source_kind) for s in picks] == [
        ("funky_picks", "Jon Jones", "pasted")
    ]
    assert picks[0].pick.confidence == 9
    assert picks[0].video_url == "https://patreon.com/p/1"
    (record,) = sources
    assert (record["kind"], record["status"], record["pick_count"]) == ("pasted", "ok", 1)


def test_pasted_picks_count_as_stated_confidence():
    """Unlike a roundup line, a pasted pick is the capper's own conviction."""
    from mma_engine.aggregate import build_consensus

    funky = ROSTER[0]
    payload = build_consensus(
        [
            sourced(funky, "Jon Jones", "Tom Aspinall", "Jon Jones", kind="pasted"),
            sourced(ROSTER[1], "Jon Jones", "Tom Aspinall", "Jon Jones", kind="tracker"),
        ]
    )
    option = payload["fights"][0]["markets"][0]["options"][0]
    assert option["pick_count"] == 2
    assert option["stated_pick_count"] == 1
    assert option["stated_avg_confidence"] == 7.0
