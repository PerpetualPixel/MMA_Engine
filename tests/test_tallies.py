"""Tests for the spoken-tally path (headcounts with no channel names)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mma_engine.tallies import (
    SOURCE_KIND,
    TallyError,
    TallyFight,
    fight_payload,
    load_tallies,
    merge_tallies,
)


def fight(**overrides):
    base = {
        "fighter_a": "Brendan Allen",
        "fighter_b": "Christian Leroy Duncan",
        "moneyline": {"channels": 73, "counts": {"a": 44}},
        "method": {
            "channels": 48,
            "counts": {"a": {"submission": 16, "decision": 14}, "b": {"ko_tko": 18}},
        },
    }
    base.update(overrides)
    return base


def test_one_sided_moneyline_puts_the_rest_on_the_other_fighter():
    parsed = TallyFight(fight(), "t")
    assert parsed.moneyline == {"a": 44, "b": 29}


def test_both_sides_stated_must_sum_to_the_channel_count():
    with pytest.raises(TallyError, match="sums to 70"):
        TallyFight(fight(moneyline={"channels": 73, "counts": {"a": 44, "b": 26}}), "t")


def test_a_count_above_the_field_is_rejected():
    with pytest.raises(TallyError, match="more than the"):
        TallyFight(fight(moneyline={"channels": 40, "counts": {"a": 44}}), "t")


def test_method_split_must_sum_to_its_own_total():
    # The host states the method total separately from the winner total, and a
    # split that misses it is a mis-transcription, not a smaller sample.
    broken = fight(
        method={
            "channels": 48,
            "counts": {"a": {"submission": 16, "decision": 14}, "b": {"ko_tko": 17}},
        }
    )
    with pytest.raises(TallyError, match="sums to 47"):
        TallyFight(broken, "t")


def test_unknown_method_is_rejected_rather_than_dropped():
    with pytest.raises(TallyError, match="unknown method"):
        TallyFight(fight(method={"channels": 1, "counts": {"a": {"dq": 1}}}), "t")


def test_panel_backing_cannot_exceed_the_panel():
    with pytest.raises(TallyError, match="more than the panel holds"):
        TallyFight(fight(sharps={"a": {"predictors": [9, 8]}}), "t")


def test_zero_of_a_panel_is_a_real_reading():
    # "not one of the seven best dog pickers is on Duncan" is the single most
    # informative thing the roundup said about the main event.
    parsed = TallyFight(fight(sharps={"b": {"underdogs": [0, 7]}}), "t")
    assert parsed.sharps["b"] == {"underdogs": [0, 7]}


def test_payload_matches_the_shape_build_consensus_emits():
    payload = fight_payload(TallyFight(fight(), "t"), confidence=5)
    assert payload["fight_id"] == "allen|duncan"
    assert payload["source"] == SOURCE_KIND
    # The widest market is the headcount: a channel that named a method also
    # named a winner, so the two markets must not be summed into it.
    assert payload["capper_count"] == 73
    ml = next(m for m in payload["markets"] if m["bet_type"] == "moneyline")
    lead = ml["options"][0]
    assert lead["selection"] == "Brendan Allen"
    assert lead["pick_count"] == 44
    assert lead["consensus_pct"] == 60.3
    # One neutral-trust voice at the roundup's placeholder confidence, so the
    # number is on the same scale as an attributed fight's.
    assert lead["weight"] == 110.0
    # Nothing was stated, and there is nobody to name.
    assert lead["stated_pick_count"] == 0
    assert lead["cappers"] == []


def test_method_options_are_labelled_the_way_attributed_ones_are():
    payload = fight_payload(TallyFight(fight(), "t"), confidence=5)
    mov = next(m for m in payload["markets"] if m["bet_type"] == "method_of_victory")
    assert [o["selection"] for o in mov["options"]] == [
        "Christian Leroy Duncan by KO/TKO",
        "Brendan Allen by Submission",
        "Brendan Allen by Decision",
    ]


def test_a_fight_with_no_markets_is_not_emitted():
    bare = {"fighter_a": "A Smith", "fighter_b": "B Jones"}
    assert fight_payload(TallyFight(bare, "t"), confidence=5) is None


def write_tally(tmp_path: Path, **overrides) -> Path:
    body = {"event_name": "UFC Fight Night: Allen vs. Duncan", "fights": [fight(**overrides)]}
    path = tmp_path / "card.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_load_skips_a_file_that_does_not_reconcile(tmp_path, caplog):
    write_tally(tmp_path, moneyline={"channels": 73, "counts": {"a": 44, "b": 26}})
    assert load_tallies(tmp_path) == []
    assert "Skipping tally" in caplog.text


def test_load_tolerates_a_missing_directory():
    assert load_tallies(Path("no/such/dir")) == []


def test_merge_adds_the_fight_and_its_board_prices(tmp_path):
    write_tally(
        tmp_path,
        odds={"a": {"moneyline": "-127", "submission": "+290"}, "b": {"moneyline": "+106"}},
    )
    payload = {"fights": [], "totals": {}}
    board_odds: dict = {}
    added = merge_tallies(payload, load_tallies(tmp_path), confidence=5, board_odds=board_odds)
    assert added == 1
    assert payload["totals"]["channels_tallied"] == 73
    assert payload["totals"]["fights"] == 1
    assert board_odds["allen|duncan"]["a"]["submission"] == "+290"


def test_a_fight_the_named_pipeline_already_covered_is_left_alone(tmp_path):
    write_tally(tmp_path)
    payload = {
        "fights": [
            {
                "fighter_a": "Brendan Allen",
                "fighter_b": "Christian Duncan",
                "markets": [],
            }
        ],
        "totals": {},
    }
    # A headcount and a roster of the same channels are the same votes; the
    # roster is the better reading, so the tally defers instead of adding.
    assert merge_tallies(payload, load_tallies(tmp_path), confidence=5) == 0
    assert len(payload["fights"]) == 1


def test_card_order_rides_along_so_a_tally_only_run_sorts_chronologically(tmp_path):
    write_tally(tmp_path, card_order=11)
    payload = {"fights": [], "totals": {}}
    merge_tallies(payload, load_tallies(tmp_path), confidence=5)
    assert payload["fights"][0]["card_order"] == 11
