"""Tests for tracker-derived capper trust scores.

Covers the arithmetic that turns tracked ROI into a weight, the pooling that
makes post-event reviews compound, and the config merge. No network — the model
call itself is not exercised here.

Run with:  PYTHONPATH=src python -m pytest -q
"""

from __future__ import annotations

import json

import pytest

from mma_engine.roster import (
    NEUTRAL_TRUST,
    CategoryStat,
    TrackedCapper,
    build_capper_entry,
    merge_into_config,
    pool_totals,
    slugify,
    stat_to_totals,
    trust_from_record,
    trust_from_totals,
)


def totals(roi=None, correct=None, picks=1000):
    """A pooled record with a large sample, so shrinkage is negligible."""
    out = {}
    if roi is not None:
        out["roi"] = {"picks": picks, "value": roi}
    if correct is not None:
        out["correct"] = {"picks": picks, "value": correct}
    return out


# -- trust arithmetic ------------------------------------------------------


def test_zero_roi_is_neutral():
    assert trust_from_totals(totals(roi=0.0)) == pytest.approx(NEUTRAL_TRUST, abs=0.2)


def test_trust_rises_with_roi_and_caps_at_ten():
    scale = [trust_from_totals(totals(roi=r)) for r in (-20, -10, 0, 10, 20, 60)]
    assert scale == sorted(scale), "trust must be monotonic in ROI"
    assert scale[-1] == 10.0, "a huge ROI is capped, not extrapolated"
    assert 1.0 <= scale[0] <= 2.0, "a deeply negative ROI bottoms out near 1"


def test_roi_is_preferred_over_correct_percent():
    # Profitable but sub-50% accurate — betting dogs. ROI should win.
    both = totals(roi=15.0, correct=42.0)
    assert trust_from_totals(both) == trust_from_totals(totals(roi=15.0))
    assert trust_from_totals(both) > NEUTRAL_TRUST


def test_correct_percent_used_when_no_roi():
    assert trust_from_totals(totals(correct=50.0)) == pytest.approx(
        NEUTRAL_TRUST, abs=0.2
    )
    assert trust_from_totals(totals(correct=65.0)) > NEUTRAL_TRUST
    assert trust_from_totals(totals(correct=35.0)) < NEUTRAL_TRUST


def test_no_usable_metric_returns_none():
    assert trust_from_totals({}) is None
    assert trust_from_totals({"roi": {"picks": 0, "value": 40.0}}) is None


def test_small_samples_are_pulled_toward_neutral():
    """A hot streak over a few picks must not mint a top score."""
    tiny = trust_from_totals(totals(roi=40.0, picks=8))
    large = trust_from_totals(totals(roi=40.0, picks=800))

    assert tiny < large
    assert abs(tiny - NEUTRAL_TRUST) < abs(large - NEUTRAL_TRUST)
    assert tiny < 7.0, "8 picks at +40% ROI should not read as elite"
    assert large > 9.0, "800 picks at +40% ROI should"


def test_shrinkage_is_symmetric_for_losses():
    tiny = trust_from_totals(totals(roi=-40.0, picks=8))
    assert NEUTRAL_TRUST - 2.0 < tiny < NEUTRAL_TRUST


# -- pooling (what makes post-event reviews compound) ----------------------


def test_pooling_is_pick_weighted_not_a_plain_average():
    # 100 picks at +10% then 20 picks at -20% -> (1000 - 400) / 120 = +5%
    pooled = pool_totals(
        {"roi": {"picks": 100, "value": 10.0}},
        {"roi": {"picks": 20, "value": -20.0}},
    )
    assert pooled["roi"]["picks"] == 120
    assert pooled["roi"]["value"] == pytest.approx(5.0)


def test_pooling_handles_a_missing_side():
    existing = {"roi": {"picks": 50, "value": 8.0}}
    assert pool_totals(existing, {}) == existing
    assert pool_totals({}, existing) == existing


def test_pooling_keeps_metrics_separate():
    pooled = pool_totals(
        {"roi": {"picks": 10, "value": 10.0}},
        {"correct": {"picks": 10, "value": 60.0}},
    )
    assert pooled["roi"] == {"picks": 10, "value": 10.0}
    assert pooled["correct"] == {"picks": 10, "value": 60.0}


def test_repeated_events_converge_rather_than_swing():
    """Each event nudges the estimate; one bad card cannot tank a long record."""
    record = {"roi": {"picks": 400, "value": 12.0}}
    after_bad_card = pool_totals(record, {"roi": {"picks": 6, "value": -100.0}})

    before = trust_from_totals(record)
    after = trust_from_totals(after_bad_card)
    assert after < before
    assert before - after < 1.0, "a 6-pick card must not swing trust by a full point"


# -- entry construction ----------------------------------------------------


def stat(category, **kwargs) -> CategoryStat:
    return CategoryStat(category=category, **kwargs)


def test_build_entry_maps_categories_to_trust():
    capper = TrackedCapper(
        name="Funkybunch MMA",
        channel_handle="@FunkyPicks",
        stats=[
            stat("overall", roi_percent=10.0, picks_tracked=500),
            stat("underdog", roi_percent=20.0, picks_tracked=500),
            stat("favorite", roi_percent=-5.0, picks_tracked=500),
        ],
        notes="Best underdog hitter of the period.",
    )
    entry = build_capper_entry(capper, video_id="abc12345678")

    assert entry["id"] == "funkybunch_mma"
    assert entry["channel_url"] == "https://www.youtube.com/@FunkyPicks"
    assert entry["discover"] is True
    assert entry["trust"]["underdog"] > entry["trust"]["overall"]
    assert entry["trust"]["favorite"] < entry["trust"]["overall"]
    assert entry["tracked"]["videos"] == ["abc12345678"]


def test_missing_category_inherits_overall_not_a_guess():
    capper = TrackedCapper(
        name="Solo Stat",
        channel_handle="",
        stats=[stat("overall", roi_percent=8.0, picks_tracked=300)],
        notes="",
    )
    trust = build_capper_entry(capper)["trust"]
    assert trust["underdog"] == trust["overall"] == trust["favorite"]


def test_capper_without_handle_is_not_discoverable():
    capper = TrackedCapper(name="No Handle", channel_handle="", stats=[], notes="")
    entry = build_capper_entry(capper)
    assert entry["discover"] is False
    assert entry["channel_url"] == ""
    assert entry["trust"]["overall"] == NEUTRAL_TRUST


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Funkybunch MMA", "funkybunch_mma"),
        ("We Want Picks (Jacob)", "we_want_picks_jacob"),
        ("TonyHasDiedMMA", "tonyhasdiedmma"),
        ("!!!", "capper"),
    ],
)
def test_slugify(name, expected):
    assert slugify(name) == expected


def test_stat_to_totals_skips_empty_stats():
    assert stat_to_totals(stat("overall")) == {}


def test_trust_from_record_defaults_to_neutral():
    assert trust_from_record({}) == {
        "overall": NEUTRAL_TRUST,
        "underdog": NEUTRAL_TRUST,
        "favorite": NEUTRAL_TRUST,
    }


# -- config merge ----------------------------------------------------------


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "cappers": [
                    {
                        "id": "funky_picks",
                        "name": "Funkybunch MMA",
                        "channel_url": "https://www.youtube.com/@FunkyPicks",
                        "discover": True,
                        "trust": {"overall": 7.5, "underdog": 9.0, "favorite": 9.0},
                    }
                ]
            }
        )
    )
    return path


def entry(name, capper_id, url, roi, picks=400):
    return {
        "id": capper_id,
        "name": name,
        "channel_url": url,
        "discover": True,
        "trust": {"overall": 0, "underdog": 0, "favorite": 0},
        "tracked": {
            "notes": "",
            "videos": [],
            "record": {"overall": {"roi": {"picks": picks, "value": roi}}},
        },
    }


def test_merge_matches_existing_capper_by_channel_url(config_file):
    # A different id but the same channel must update, not duplicate.
    proposed = [
        entry("Funky Bunch", "funky_bunch", "https://www.youtube.com/@FunkyPicks", 10.0)
    ]
    report = merge_into_config(config_file, proposed, video_id="vid00000001")

    config = json.loads(config_file.read_text())
    assert len(config["cappers"]) == 1
    assert report["updated"] == ["Funkybunch MMA"]
    assert config["cappers"][0]["trust"]["overall"] > NEUTRAL_TRUST


def test_merge_adds_unknown_cappers(config_file):
    proposed = [entry("New Guy", "new_guy", "https://www.youtube.com/@NewGuy", 5.0)]
    report = merge_into_config(config_file, proposed, video_id="vid00000001")

    config = json.loads(config_file.read_text())
    assert report["added"] == ["New Guy"]
    assert {c["id"] for c in config["cappers"]} == {"funky_picks", "new_guy"}


def test_merge_preserves_channel_url_and_discover_flag(config_file):
    config = json.loads(config_file.read_text())
    config["cappers"][0]["discover"] = False
    config_file.write_text(json.dumps(config))

    merge_into_config(
        config_file,
        [entry("Funkybunch MMA", "funky_picks", "", 10.0)],
        video_id="vid00000001",
    )

    updated = json.loads(config_file.read_text())["cappers"][0]
    assert updated["discover"] is False, "a deliberate opt-out must survive a refresh"
    assert updated["channel_url"] == "https://www.youtube.com/@FunkyPicks"


def test_applying_the_same_video_twice_is_a_noop(config_file):
    proposed = [entry("Funkybunch MMA", "funky_picks", "", 10.0)]

    merge_into_config(config_file, proposed, video_id="vid00000001")
    first = json.loads(config_file.read_text())["cappers"][0]

    report = merge_into_config(config_file, proposed, video_id="vid00000001")
    second = json.loads(config_file.read_text())["cappers"][0]

    assert report["skipped"] == ["Funkybunch MMA"]
    assert first["trust"] == second["trust"]
    assert second["tracked"]["record"]["overall"]["roi"]["picks"] == 400


def test_accumulate_pools_successive_events(config_file):
    merge_into_config(
        config_file,
        [entry("Funkybunch MMA", "funky_picks", "", 10.0, picks=100)],
        video_id="event0000001",
    )
    merge_into_config(
        config_file,
        [entry("Funkybunch MMA", "funky_picks", "", -20.0, picks=20)],
        video_id="event0000002",
    )

    record = json.loads(config_file.read_text())["cappers"][0]["tracked"]
    assert record["videos"] == ["event0000001", "event0000002"]
    assert record["record"]["overall"]["roi"]["picks"] == 120
    assert record["record"]["overall"]["roi"]["value"] == pytest.approx(5.0)


def test_replace_mode_discards_prior_record(config_file):
    merge_into_config(
        config_file,
        [entry("Funkybunch MMA", "funky_picks", "", 10.0, picks=100)],
        video_id="event0000001",
    )
    merge_into_config(
        config_file,
        [entry("Funkybunch MMA", "funky_picks", "", -20.0, picks=20)],
        video_id="recap0000001",
        mode="replace",
    )

    record = json.loads(config_file.read_text())["cappers"][0]["tracked"]
    assert record["videos"] == ["recap0000001"]
    assert record["record"]["overall"]["roi"] == {"picks": 20, "value": -20.0}


def test_merge_matches_by_normalized_name_without_url():
    # The tracker transcript gives no handle, and spacing differs from the
    # configured name — normalized-name matching must still route it.
    import pytest as _pytest  # local to keep top imports untouched
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps({
            "cappers": [{
                "id": "funky_picks",
                "name": "Funkybunch MMA",
                "channel_url": "https://www.youtube.com/@FunkyPicks",
                "discover": True,
                "trust": {"overall": 7.5, "underdog": 9.0, "favorite": 9.0},
            }]
        }))
        proposed = [entry("Funky Bunch MMA", "funky_bunch_mma", "", 10.0)]
        report = merge_into_config(path, proposed, video_id="vid00000001")

        config = json.loads(path.read_text())
        assert len(config["cappers"]) == 1
        assert report["updated"] == ["Funkybunch MMA"]


def test_merge_matches_by_alias():
    # "Bet Sam" is how captions mangle "BetSlam with Sam" — an alias on the
    # real capper routes the tracked record to them instead of duplicating.
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps({
            "cappers": [{
                "id": "betslam_sam",
                "name": "BetSlam with Sam",
                "channel_url": "https://www.youtube.com/@BetSlamWithSam",
                "discover": True,
                "trust": {"overall": 7.5, "underdog": 9.0, "favorite": 9.0},
                "aliases": ["Bet Sam", "Bet Slam with Sam"],
            }]
        }))
        proposed = [entry("Bet Sam", "bet_sam", "", 12.0)]
        report = merge_into_config(path, proposed, video_id="vid00000001")

        config = json.loads(path.read_text())
        assert len(config["cappers"]) == 1
        assert report["updated"] == ["BetSlam with Sam"]
        # The configured name survives; only trust/record change.
        assert config["cappers"][0]["name"] == "BetSlam with Sam"
        assert config["cappers"][0]["trust"]["overall"] > NEUTRAL_TRUST
