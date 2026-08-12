"""Tests for the weighted picks feed generator."""

from __future__ import annotations

from mma_engine.weighted_picks import _strength, _tier, build_picks


def _consensus(fights: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "generated_at": "2026-08-11T03:28:17+00:00",
        "event": {"name": "UFC 330"},
        "fights": fights,
    }


def _fight(fight_id: str, display: str, options: list[dict]) -> dict:
    return {
        "fight_id": fight_id,
        "display": display,
        "markets": [
            {
                "bet_type": "moneyline",
                "label": "Moneyline",
                "options": options,
            }
        ],
    }


def _option(selection: str, pct: float, weight: float, picks: int = 1) -> dict:
    return {
        "selection": selection,
        "consensus_pct": pct,
        "weight": weight,
        "pick_count": picks,
        "avg_confidence": 7.0,
    }


def test_strength_unanimous_fully_backed():
    """100% consensus with 20+ weight scores a perfect 10."""
    assert _strength(100.0, 20.0) == 10.0
    assert _strength(100.0, 50.0) == 10.0


def test_strength_unanimous_thin_backing():
    """One unopposed capper is 100% consensus but scores modestly."""
    assert _strength(100.0, 2.0) == 5.5


def test_strength_split_market():
    """A 50/50 market never scores above the pass line."""
    assert _strength(50.0, 40.0) == 5.0


def test_tier_boundaries():
    assert _tier(7.5) == "strong"
    assert _tier(7.4) == "lean"
    assert _tier(5.0) == "lean"
    assert _tier(4.9) == "pass"


def test_build_picks_takes_top_option():
    consensus = _consensus(
        [
            _fight(
                "a|b",
                "Fighter A vs Fighter B",
                [
                    _option("Fighter B", 30.0, 6.0, picks=1),
                    _option("Fighter A", 70.0, 14.0, picks=3),
                ],
            )
        ]
    )
    feed = build_picks(consensus)
    assert feed["totals"]["picks"] == 1
    pick = feed["picks"][0]
    assert pick["selection"] == "Fighter A"
    assert pick["fight"] == "Fighter A vs Fighter B"
    assert pick["market"] == "moneyline"
    assert pick["suggested_units"] in (0.0, 1.0, 2.0)


def test_build_picks_sorted_by_strength():
    consensus = _consensus(
        [
            _fight("a|b", "A vs B", [_option("A", 60.0, 5.0)]),
            _fight("c|d", "C vs D", [_option("C", 100.0, 25.0)]),
        ]
    )
    feed = build_picks(consensus)
    assert [p["fight"] for p in feed["picks"]] == ["C vs D", "A vs B"]
    assert feed["picks"][0]["tier"] == "strong"


def test_build_picks_skips_empty_markets():
    consensus = _consensus(
        [
            {
                "fight_id": "a|b",
                "display": "A vs B",
                "markets": [
                    {"bet_type": "moneyline", "label": "Moneyline", "options": []}
                ],
            }
        ]
    )
    feed = build_picks(consensus)
    assert feed["totals"]["picks"] == 0
    assert feed["picks"] == []


def test_build_picks_carries_capper_comments():
    """Backing cappers' reasoning rides along, trust-ordered, empties skipped."""
    option = _option("Fighter A", 100.0, 14.0, picks=3)
    option["cappers"] = [
        {"id": "low", "name": "Low Trust", "trust": 2.0, "confidence": 5, "reasoning": "Cardio edge."},
        {"id": "high", "name": "High Trust", "trust": 9.0, "confidence": 8, "reasoning": "Better everywhere."},
        {"id": "mute", "name": "No Comment", "trust": 7.0, "confidence": 6, "reasoning": "  "},
    ]
    feed = build_picks(_consensus([_fight("a|b", "A vs B", [option])]))
    comments = feed["picks"][0]["comments"]
    assert [c["capper"] for c in comments] == ["High Trust", "Low Trust"]
    assert comments[0] == {"capper": "High Trust", "comment": "Better everywhere.", "confidence": 8}


def test_build_picks_without_cappers_has_empty_comments():
    feed = build_picks(_consensus([_fight("a|b", "A vs B", [_option("A", 60.0, 5.0)])]))
    assert feed["picks"][0]["comments"] == []


def test_feed_metadata_carried_over():
    feed = build_picks(_consensus([]))
    assert feed["schema_version"] == 1
    assert feed["generated_at"] == "2026-08-11T03:28:17+00:00"
    assert feed["event"]["name"] == "UFC 330"
    assert feed["totals"] == {"picks": 0, "strong": 0, "lean": 0, "pass": 0}


def test_build_picks_passes_card_status_through():
    fights = [
        {**_fight("a|b", "A vs B", [_option("A", 100.0, 12.0)]), "card_status": "cancelled"},
        _fight("c|d", "C vs D", [_option("C", 100.0, 12.0)]),
    ]
    picks = build_picks(_consensus(fights))["picks"]
    by_fight = {p["fight_id"]: p for p in picks}
    # The website reads this to banner cancelled fights; a fight with no
    # annotation (no card fetched that run) simply omits the field.
    assert by_fight["a|b"]["card_status"] == "cancelled"
    assert "card_status" not in by_fight["c|d"]
