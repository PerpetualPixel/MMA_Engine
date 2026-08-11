"""Tests for the pure logic: name normalization and weighted aggregation.

No network and no API key needed — `Pick` objects are constructed directly.
Run with:  PYTHONPATH=src python -m pytest -q
"""

from __future__ import annotations

import pytest

from mma_engine.aggregate import SourcedPick, build_consensus
from mma_engine.config import Capper, extract_video_id
from mma_engine.extract import Pick, chunk_transcript
from mma_engine.normalize import fight_key, method_bucket, selection_key, surname


def make_capper(capper_id: str, overall=5.0, underdog=5.0, favorite=5.0) -> Capper:
    return Capper(
        id=capper_id,
        name=capper_id.replace("_", " ").title(),
        trust={"overall": overall, "underdog": underdog, "favorite": favorite},
    )


def make_pick(
    a="Jon Jones",
    b="Stipe Miocic",
    bet_type="moneyline",
    selection="Jon Jones",
    fighter="Jon Jones",
    confidence=8,
    role="unknown",
) -> Pick:
    return Pick(
        fighter_a=a,
        fighter_b=b,
        bet_type=bet_type,
        selection=selection,
        fighter=fighter,
        confidence=confidence,
        role=role,
        odds_american="",
        stake_units="",
        reasoning="",
    )


def source(pick: Pick, capper: Capper, video_id="vid00000001") -> SourcedPick:
    return SourcedPick(pick=pick, capper=capper, video_id=video_id, video_url="")


# -- normalization ---------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Alexander Volkanovski", "volkanovski"),
        ("Ilía Topuria", "topuria"),
        ("Khabib Nurmagomedov Jr.", "nurmagomedov"),
        ("Marlon Vera", "vera"),
        ("Antonio Rodrigo Nogueira", "nogueira"),
        ("Charles do Bronx", "dobronx"),
        ("", ""),
    ],
)
def test_surname(name, expected):
    assert surname(name) == expected


def test_fight_key_is_order_independent_and_spelling_tolerant():
    a = fight_key("Alexander Volkanovski", "Ilia Topuria")
    b = fight_key("Ilía Topuria", "Volkanovski")
    assert a == b == "topuria|volkanovski"


def test_method_bucket_groups_synonyms():
    assert method_bucket("Jones by KO/TKO") == "ko_tko"
    assert method_bucket("wins by knockout") == "ko_tko"
    assert method_bucket("submission in round 2") == "submission"
    assert method_bucket("rear naked choke") == "submission"
    assert method_bucket("unanimous decision") == "decision"
    assert method_bucket("wins somehow") == "other"


def test_selection_key_distinguishes_markets():
    assert selection_key("moneyline", "Jon Jones", "Jon Jones") == "jones"
    assert (
        selection_key("method_of_victory", "Jones by knockout", "Jon Jones")
        == "jones:ko_tko"
    )
    assert selection_key("over_under", "Over 2.5 rounds") == "over:2.5"
    assert selection_key("over_under", "Under 2.5") == "under:2.5"
    assert selection_key("round", "Jones in round 1", "Jon Jones") == "jones:round1"


def test_extract_video_id_accepts_common_url_shapes():
    for url in [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/live/dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ?feature=share",
        "dQw4w9WgXcQ",
    ]:
        assert extract_video_id(url) == "dQw4w9WgXcQ"


# -- aggregation -----------------------------------------------------------


def test_unanimous_pick_is_full_consensus():
    capper_a, capper_b = make_capper("a"), make_capper("b")
    payload = build_consensus(
        [source(make_pick(), capper_a), source(make_pick(), capper_b)]
    )

    assert payload["totals"] == {"fights": 1, "picks": 2, "cappers": 2, "videos": 1}
    moneyline = payload["fights"][0]["markets"][0]
    assert moneyline["bet_type"] == "moneyline"
    assert len(moneyline["options"]) == 1
    assert moneyline["options"][0]["consensus_pct"] == 100.0
    assert moneyline["options"][0]["pick_count"] == 2


def test_trust_and_confidence_shape_the_split():
    # Trusted capper, max confidence: weight 9.0 * 1.0 = 9.0
    trusted = make_capper("trusted", overall=9.0)
    # Two weak cappers at low confidence: 2 * (5.0 * 0.4) = 4.0
    weak_one, weak_two = make_capper("weak_one"), make_capper("weak_two")

    payload = build_consensus(
        [
            source(make_pick(confidence=10), trusted),
            source(
                make_pick(selection="Stipe Miocic", fighter="Stipe Miocic", confidence=4),
                weak_one,
                video_id="vid00000002",
            ),
            source(
                make_pick(selection="Stipe Miocic", fighter="Stipe Miocic", confidence=4),
                weak_two,
                video_id="vid00000003",
            ),
        ]
    )

    options = payload["fights"][0]["markets"][0]["options"]
    assert [o["selection"] for o in options] == ["Jon Jones", "Stipe Miocic"]
    assert options[0]["weight"] == 9.0
    assert options[1]["weight"] == 4.0
    # 9 / 13 and 4 / 13
    assert options[0]["consensus_pct"] == 69.2
    assert options[1]["consensus_pct"] == 30.8
    assert sum(o["consensus_pct"] for o in options) == pytest.approx(100.0, abs=0.2)


def test_role_selects_the_matching_trust_score():
    specialist = make_capper("dog_hunter", overall=5.0, underdog=9.0, favorite=2.0)
    dog = source(make_pick(confidence=10, role="underdog"), specialist)
    chalk = source(make_pick(confidence=10, role="favorite"), specialist)

    assert dog.weight == pytest.approx(9.0)
    assert chalk.weight == pytest.approx(2.0)


def test_markets_are_kept_separate():
    capper = make_capper("a")
    payload = build_consensus(
        [
            source(make_pick(), capper),
            source(
                make_pick(
                    bet_type="method_of_victory",
                    selection="Jon Jones by KO/TKO",
                ),
                capper,
            ),
            source(
                make_pick(
                    bet_type="over_under",
                    selection="Under 2.5 rounds",
                    fighter="",
                ),
                capper,
            ),
        ]
    )
    labels = [m["bet_type"] for m in payload["fights"][0]["markets"]]
    assert labels == ["moneyline", "method_of_victory", "over_under"]


def test_display_name_prefers_the_fullest_spelling():
    capper_a, capper_b = make_capper("a"), make_capper("b")
    payload = build_consensus(
        [
            source(make_pick(a="Jones", b="Miocic"), capper_a),
            source(make_pick(a="Jon Jones", b="Stipe Miocic"), capper_b),
        ]
    )
    assert payload["fights"][0]["display"] == "Jon Jones vs Stipe Miocic"


def test_min_confidence_filters_low_conviction_picks():
    capper = make_capper("a")
    payload = build_consensus([source(make_pick(confidence=2), capper)], min_confidence=5)
    assert payload["totals"]["picks"] == 0
    assert payload["fights"] == []


def test_fights_are_ordered_by_coverage():
    cappers = [make_capper(f"c{i}") for i in range(3)]
    picks = [source(make_pick(), c) for c in cappers]
    picks.append(
        source(
            make_pick(a="Islam Makhachev", b="Arman Tsarukyan", selection="Islam Makhachev",
                     fighter="Islam Makhachev"),
            cappers[0],
        )
    )
    payload = build_consensus(picks)
    assert payload["fights"][0]["display"] == "Jon Jones vs Stipe Miocic"
    assert payload["fights"][0]["capper_count"] == 3


# -- chunking --------------------------------------------------------------


def test_chunk_transcript_splits_on_word_boundaries():
    text = " ".join(f"word{i}" for i in range(500))
    chunks = chunk_transcript(text, 200)
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)
    # No word is cut in half, so re-joining reproduces the original tokens.
    assert " ".join(chunks).split() == text.split()


def test_chunk_transcript_returns_single_chunk_when_short():
    assert chunk_transcript("short text", 1000) == ["short text"]
