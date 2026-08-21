"""Tests for roundup ingestion — one tracker video, every channel's pick.

Covers the chunk merge (including the reversed-order and both-sides cases),
channel attribution against the configured roster, and the config merge. No
network and no model call: the extractor's API call itself is not exercised
here, only everything either side of it.

Run with:  PYTHONPATH=src python -m pytest -q
"""

from __future__ import annotations

import json

import pytest

from mma_engine.config import Capper, load_config
from mma_engine.normalize import fight_key
from mma_engine.tracker_picks import (
    CapperDirectory,
    TrackerFightPicks,
    TrackerRoundup,
    merge_new_cappers,
    merge_roundups,
    to_sourced_picks,
)


def roundup(*fights, event_name="UFC 300"):
    return TrackerRoundup(event_name=event_name, fights=list(fights))


def fight(a, b, for_a=(), for_b=()):
    return TrackerFightPicks(
        fighter_a=a, fighter_b=b, cappers_for_a=list(for_a), cappers_for_b=list(for_b)
    )


def capper(id_, name, overall=5.0, aliases=()):
    return Capper(id=id_, name=name, trust={"overall": overall}, aliases=tuple(aliases))


# -- merging chunks --------------------------------------------------------


def test_merge_unions_the_same_fight_across_chunks():
    merged = merge_roundups(
        [
            roundup(fight("Jon Jones", "Tom Aspinall", ["Funky Picks"], ["MMA Guru"])),
            roundup(fight("Jon Jones", "Tom Aspinall", ["Chisanga MMA"], [])),
        ]
    )
    assert len(merged) == 1
    assert merged[0].cappers_for_a == ["Chisanga MMA", "Funky Picks"]
    assert merged[0].cappers_for_b == ["MMA Guru"]


def test_merge_aligns_sides_by_fighter_not_position():
    """A later chunk naming the pair in the other order must not flip votes."""
    merged = merge_roundups(
        [
            roundup(fight("Jon Jones", "Tom Aspinall", ["Funky Picks"], [])),
            roundup(fight("Tom Aspinall", "Jon Jones", ["MMA Guru"], [])),
        ]
    )
    assert len(merged) == 1
    # The first chunk's orientation is kept, and the flipped chunk's vote
    # follows its fighter rather than its position.
    assert merged[0].fighter_a == "Jon Jones"
    assert merged[0].cappers_for_a == ["Funky Picks"]
    assert merged[0].cappers_for_b == ["MMA Guru"]


def test_merge_drops_a_channel_the_chunks_put_on_both_sides():
    merged = merge_roundups(
        [
            roundup(fight("Jon Jones", "Tom Aspinall", ["Funky Picks"], ["MMA Guru"])),
            roundup(fight("Jon Jones", "Tom Aspinall", [], ["Funky Picks"])),
        ]
    )
    assert merged[0].cappers_for_a == []
    assert merged[0].cappers_for_b == ["MMA Guru"]


def test_merge_counts_one_vote_per_channel_however_often_it_repeats():
    merged = merge_roundups(
        [
            roundup(fight("Jon Jones", "Tom Aspinall", ["Funky Picks"], [])),
            roundup(fight("Jon Jones", "Tom Aspinall", ["funky picks"], [])),
            roundup(fight("Jon Jones", "Tom Aspinall", ["FunkyPicks"], [])),
        ]
    )
    assert merged[0].cappers_for_a == ["Funky Picks"]


def test_merge_skips_unusable_matchups():
    merged = merge_roundups(
        [
            roundup(
                fight("Jon Jones", "", ["Funky Picks"], []),
                fight("Jones", "Jones", ["MMA Guru"], []),
            )
        ]
    )
    assert merged == []


# -- attribution -----------------------------------------------------------


def directory():
    return CapperDirectory(
        [
            capper("funky_picks", "Funky Picks", overall=8.0),
            capper("betslam", "BetSlam with Sam", overall=7.0, aliases=["Bet Sam"]),
        ]
    )


def test_configured_channel_resolves_to_its_trust():
    resolved, minted = directory().resolve("Funky Picks")
    assert (resolved.id, minted) == ("funky_picks", False)
    assert resolved.trust_for("unknown") == 8.0


def test_alias_routes_a_garbled_name_to_the_real_capper():
    resolved, minted = directory().resolve("bet sam")
    assert (resolved.id, minted) == ("betslam", False)


def test_caption_typo_within_one_edit_still_matches():
    resolved, minted = directory().resolve("Funky Pick")
    assert (resolved.id, minted) == ("funky_picks", False)


def test_unknown_channel_is_minted_at_neutral_trust():
    directory_ = directory()
    resolved, minted = directory_.resolve("Chisanga MMA")
    assert minted is True
    assert resolved.trust_for("overall") == 5.0
    assert resolved.discover is False
    # Same name later in the video is the same capper, not a second one.
    again, _ = directory_.resolve("chisanga mma")
    assert again.id == resolved.id
    assert [c.id for c in directory_.minted] == [resolved.id]


def test_minted_ids_never_collide_with_a_configured_one():
    directory_ = CapperDirectory([capper("tracker_mma_guru", "Somebody Else")])
    resolved, _ = directory_.resolve("MMA Guru")
    assert resolved.id != "tracker_mma_guru"


def test_a_nameless_entry_is_unusable_rather_than_minted():
    assert directory().resolve("   ") is None


# -- conversion to picks ---------------------------------------------------


def picks_for(fights, covered=frozenset(), confidence=5):
    return to_sourced_picks(
        fights,
        directory(),
        video_id="VID",
        video_url="https://youtu.be/VID",
        confidence=confidence,
        already_covered=covered,
    )


def test_each_attribution_becomes_one_moneyline_pick():
    picks, stats = picks_for(
        [fight("Jon Jones", "Tom Aspinall", ["Funky Picks"], ["Chisanga MMA"])]
    )
    assert stats.picks == 2
    assert (stats.matched, stats.minted) == (1, 1)
    jones = next(p for p in picks if p.capper.id == "funky_picks")
    assert jones.pick.bet_type == "moneyline"
    assert jones.pick.selection == "Jon Jones"
    assert jones.pick.fighter == "Jon Jones"
    assert jones.pick.confidence == 5
    assert jones.pick.role == "unknown"
    assert jones.pick.reasoning == ""
    assert jones.source_kind == "tracker"
    # 8.0 trust at the neutral 5/10 confidence.
    assert jones.weight == pytest.approx(4.0)


def test_a_cappers_own_video_supersedes_their_roundup_line():
    key = fight_key("Jon Jones", "Tom Aspinall")
    picks, stats = picks_for(
        [fight("Jon Jones", "Tom Aspinall", ["Funky Picks"], ["Chisanga MMA"])],
        covered=frozenset({("funky_picks", key)}),
    )
    assert stats.superseded == 1
    assert [p.capper.id for p in picks] != []
    assert "funky_picks" not in {p.capper.id for p in picks}


def test_superseding_is_per_fight_not_per_capper():
    covered = frozenset({("funky_picks", fight_key("Jon Jones", "Tom Aspinall"))})
    picks, stats = picks_for(
        [
            fight("Jon Jones", "Tom Aspinall", ["Funky Picks"], []),
            fight("Ilia Topuria", "Max Holloway", ["Funky Picks"], []),
        ],
        covered=covered,
    )
    assert stats.superseded == 1
    assert [p.pick.selection for p in picks] == ["Ilia Topuria"]


def test_channel_counts_are_channels_not_picks():
    _, stats = picks_for(
        [
            fight("Jon Jones", "Tom Aspinall", ["Funky Picks"], []),
            fight("Ilia Topuria", "Max Holloway", ["Funky Picks"], ["Chisanga MMA"]),
        ]
    )
    assert stats.picks == 3
    assert (stats.matched, stats.minted) == (1, 1)


def test_confidence_is_configurable():
    picks, _ = picks_for(
        [fight("Jon Jones", "Tom Aspinall", ["Funky Picks"], [])], confidence=3
    )
    assert picks[0].pick.confidence == 3
    assert picks[0].weight == pytest.approx(2.4)


# -- config merge ----------------------------------------------------------


BASE_CONFIG = {
    "event": {"name": "UFC 300"},
    "cappers": [
        {
            "id": "funky_picks",
            "name": "Funky Picks",
            "trust": {"overall": 8.0, "underdog": 8.5},
            "aliases": ["Funk Picks"],
            "tracked": {"videos": ["abc"], "record": {"overall": {}}},
        }
    ],
}


def write_config(tmp_path, data=None):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data or BASE_CONFIG, indent=2), encoding="utf-8")
    return path


def test_merge_new_cappers_appends_unknown_channels(tmp_path):
    path = write_config(tmp_path)
    added = merge_new_cappers(path, [capper("tracker_chisanga_mma", "Chisanga MMA")])
    assert added == ["Chisanga MMA"]

    entries = json.loads(path.read_text(encoding="utf-8"))["cappers"]
    new = next(e for e in entries if e["id"] == "tracker_chisanga_mma")
    assert new["trust"]["overall"] == 5.0
    assert new["discover"] is False
    assert new["tracked"]["record"] == {}


def test_merge_new_cappers_leaves_existing_entries_untouched(tmp_path):
    path = write_config(tmp_path)
    added = merge_new_cappers(
        path,
        [
            capper("tracker_funky_picks", "Funky Picks"),
            capper("tracker_funk_picks", "Funk Picks"),  # a listed alias
        ],
    )
    assert added == []
    assert json.loads(path.read_text(encoding="utf-8")) == BASE_CONFIG


def test_pipeline_ingests_a_roundup_into_the_sourced_picks(tmp_path, monkeypatch):
    """The wiring: transcript in, weighable picks and a source record out."""
    from mma_engine import pipeline
    from mma_engine.aggregate import SourcedPick
    from mma_engine.extract import Pick
    from mma_engine.tracker_picks import RoundupResult

    class FakeFetcher:
        def fetch(self, video_id):
            return type("T", (), {"ok": True, "error": "", "text": "…", "char_count": 3})()

    class FakeExtractor:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def extract(self, video_id, transcript, video_url):
            return RoundupResult(
                video_id=video_id,
                event_name="UFC 300",
                fights=[
                    fight(
                        "Jon Jones",
                        "Tom Aspinall",
                        ["Funky Picks", "Chisanga MMA"],
                        ["MMA Guru"],
                    )
                ],
            )

    monkeypatch.setattr(pipeline, "RoundupExtractor", FakeExtractor)
    # A read board is written to roundups/ relative to the cwd; keep the run
    # inside the tmp dir rather than the working tree.
    monkeypatch.chdir(tmp_path)
    config = load_config(
        write_config(tmp_path, {**BASE_CONFIG, "tracker": {"picks_videos": []}})
    )

    # Funky Picks already spoke for this fight in their own video this run.
    own = SourcedPick(
        pick=Pick(
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
        ),
        capper=config.cappers["funky_picks"],
        video_id="OWNVIDEOID1",
        video_url="https://youtu.be/OWNVIDEOID1",
    )
    picks, sources = [own], []

    event_name = pipeline.ingest_tracker_roundups(
        config,
        ["https://youtu.be/VIDEOIDXX11"],
        fetcher=FakeFetcher(),
        sourced_picks=picks,
        sources=sources,
        apply_cappers=True,
    )

    assert event_name == "UFC 300"
    added = [p for p in picks if p.source_kind == "tracker"]
    assert {p.capper.name for p in added} == {"Chisanga MMA", "MMA Guru"}
    assert all(p.pick.confidence == 5 for p in added)

    (record,) = sources
    assert record["kind"] == "tracker_roundup"
    assert (record["status"], record["pick_count"], record["superseded"]) == ("ok", 2, 1)

    # --apply-tracker-cappers persisted the two new channels, and left the
    # configured one exactly as it was.
    entries = json.loads(config.path.read_text(encoding="utf-8"))["cappers"]
    assert {e["name"] for e in entries} == {"Funky Picks", "Chisanga MMA", "MMA Guru"}
    assert entries[0] == BASE_CONFIG["cappers"][0]


def test_config_exposes_aliases_and_roundup_videos(tmp_path):
    path = write_config(
        tmp_path,
        {**BASE_CONFIG, "tracker": {"picks_videos": ["https://youtu.be/VIDEOIDXX11", ""]}},
    )
    config = load_config(path)
    assert config.cappers["funky_picks"].aliases == ("Funk Picks",)
    assert config.tracker_picks_videos == ["https://youtu.be/VIDEOIDXX11"]
    assert config.settings["tracker_picks"]["confidence"] == 5
