"""Tests for event_card.py: the official-card annotation.

All pure-logic — the ESPN payload is a fixture, never a network call.
"""

from __future__ import annotations

import pytest

from mma_engine.event_card import (
    annotate_consensus,
    find_event,
    parse_card,
)


def espn_event(name="UFC 330: Makhachev vs. Garry", fights=None):
    """A scoreboard event shaped like ESPN's MMA payload — listed
    chronologically with the main event LAST, the way ESPN lists a card
    (confirmed live with UFC 330)."""
    fights = fights if fights is not None else [
        ("Edson Barboza", "Esteban Ribovics", "STATUS_CANCELED"),
        ("Mackenzie Dern", "Jillian Robertson", "STATUS_SCHEDULED"),
        ("Islam Makhachev", "Ian Machado Garry", "STATUS_SCHEDULED"),
    ]
    return {
        "name": name,
        "date": "2026-08-15T22:00Z",
        "competitions": [
            {
                "date": "2026-08-15T22:00Z",
                "status": {"type": {"name": status}},
                "competitors": [
                    {"athlete": {"displayName": a}},
                    {"athlete": {"displayName": b}},
                ],
            }
            for a, b, status in fights
        ],
    }


def consensus_fight(fighter_a, fighter_b, **extra):
    return {
        "fight_id": "x|y",
        "display": f"{fighter_a} vs {fighter_b}",
        "fighter_a": fighter_a,
        "fighter_b": fighter_b,
        "pick_count": 3,
        "capper_count": 2,
        "markets": [{"bet_type": "moneyline", "options": []}],
        **extra,
    }


def test_parse_card_keeps_espn_chronological_order_main_event_last():
    card = parse_card(espn_event())
    orders = {f["fighter_a"]: f["order"] for f in card["fights"]}
    # ESPN lists earliest-first: Barboza opens (0), Makhachev — the main
    # event, listed last — carries the maximum order. The dashboard derives
    # the Main Event banner from that maximum, so getting this backwards
    # crowns the first prelim (the exact bug this pins: the real UFC 330
    # payload listed the title fight at the END of competitions).
    assert orders["Islam Makhachev"] == 2
    assert orders["Edson Barboza"] == 0
    assert card["fights"][0]["cancelled"] is True


def test_find_event_matches_configured_name_within_espn_title():
    scoreboard = {"events": [espn_event("Some Other Card"), espn_event()]}
    card = find_event(scoreboard, "UFC 330")
    assert card is not None
    assert card["name"] == "UFC 330: Makhachev vs. Garry"
    assert find_event(scoreboard, "UFC 999") is None


def test_annotate_marks_on_card_and_corrects_garbled_names():
    # The consensus display carries the transcripts' most common garble
    # ("Makhache"); matching the card must both flag the fight on_card and
    # adopt ESPN's clean spellings.
    payload = {"event": {}, "fights": [consensus_fight("Ian Machado Garry", "Islam Makhache")]}
    annotate_consensus(payload, parse_card(espn_event()))
    fight = payload["fights"][0]
    assert fight["card_status"] == "on_card"
    assert fight["card_order"] == 2
    assert fight["fighter_b"] == "Islam Makhachev"
    assert fight["display"] == "Ian Machado Garry vs Islam Makhachev"


def test_annotate_marks_espn_cancelled_bout():
    payload = {"event": {}, "fights": [consensus_fight("Edson Barboza", "Esteban Ribovic")]}
    annotate_consensus(payload, parse_card(espn_event()))
    assert payload["fights"][0]["card_status"] == "cancelled"


def test_annotate_drops_picks_that_are_not_on_this_card():
    # The DWCS case: a real pick from the same video, for a different event.
    # It is not part of this event, so it does not survive into the payload.
    payload = {"event": {}, "fights": [consensus_fight("Matt Adams", "Anthony Wint")]}
    annotate_consensus(payload, parse_card(espn_event()))
    assert all(f["fighter_a"] != "Matt Adams" for f in payload["fights"])
    assert payload["event"]["card"]["off_card_dropped"] == 1


def test_annotate_drops_the_transcript_garbles_that_invent_fights():
    # Auto-captions turn one bout into a dozen phantom pairings ("Orbai vs
    # Jeremiah Well", "Tyback Oral vs Jeremiah Well"). None are real fights,
    # and none reach the card.
    garbles = [consensus_fight(a, b) for a, b in [
        ("Orbai", "Jeremiah Well"), ("Tyback Oral", "Jeremiah Well"),
        ("Mamedbek Oralbay", "Jeremiah Well"), ("Jose Ochoa", "Unknown"),
    ]]
    payload = {"event": {}, "fights": garbles}
    annotate_consensus(payload, parse_card(espn_event()))
    assert payload["event"]["card"]["off_card_dropped"] == 4
    # What is left is the card itself, nothing else.
    assert len(payload["fights"]) == 3
    assert all(f["card_status"] in ("on_card", "cancelled") for f in payload["fights"])


def test_dropping_off_card_fights_refreshes_the_headline_totals():
    # "459 picks across 61 fights" must describe the card, not everything the
    # transcripts mentioned, or the dashboard's own header contradicts it.
    picked = consensus_fight("Islam Makhachev", "Ian Machado Garry")
    picked["markets"] = [{"bet_type": "moneyline", "options": [
        {"selection": "Islam Makhachev", "cappers": [
            {"id": "a", "video_url": "v1"}, {"id": "b", "video_url": "v2"},
        ]},
    ]}]
    elsewhere = consensus_fight("Matt Adams", "Anthony Wint")
    elsewhere["markets"] = [{"bet_type": "moneyline", "options": [
        {"selection": "Matt Adams", "cappers": [{"id": "c", "video_url": "v3"}]},
    ]}]
    payload = {"event": {}, "totals": {"fights": 2, "picks": 3, "cappers": 3, "videos": 3},
               "fights": [picked, elsewhere]}
    annotate_consensus(payload, parse_card(espn_event()))
    assert payload["totals"] == {"fights": 3, "picks": 2, "cappers": 2, "videos": 2}


def test_annotate_appends_pickless_card_bouts():
    payload = {"event": {}, "fights": [consensus_fight("Islam Makhachev", "Ian Machado Garry")]}
    annotate_consensus(payload, parse_card(espn_event()))
    displays = {f["display"] for f in payload["fights"]}
    assert "Mackenzie Dern vs Jillian Robertson" in displays
    added = next(f for f in payload["fights"] if f["fighter_a"] == "Mackenzie Dern")
    assert added["pick_count"] == 0 and added["markets"] == []
    assert added["card_status"] == "on_card"


def test_annotate_detects_the_quiet_cancellation_via_previous_run():
    # ESPN removed the bout from the card outright (no canceled status left
    # behind). It was on_card last run — that is a cancellation to show, not
    # an off-card pick.
    card = parse_card(espn_event(fights=[("Islam Makhachev", "Ian Machado Garry", "STATUS_SCHEDULED")]))
    gone = consensus_fight("Mackenzie Dern", "Jillian Robertson")
    payload = {"event": {}, "fights": [gone]}
    previous = [consensus_fight("Mackenzie Dern", "Jillian Robertson", card_status="on_card")]
    annotate_consensus(
        payload, card, previous_fights=previous, previous_card_name=card["name"]
    )
    assert payload["fights"][0]["card_status"] == "cancelled"


def test_a_previous_run_for_another_event_does_not_resurrect_its_card():
    """Retargeting the engine used to keep the old card forever: every bout on
    it was on_card last run and unmatched now, so all of them were flagged
    cancelled and kept, out of reach of the off-card filter."""
    card = parse_card(espn_event(fights=[("Islam Makhachev", "Ian Machado Garry", "STATUS_SCHEDULED")]))
    last_week = consensus_fight("Kaik Brito", "Namo Fazil")
    payload = {"event": {}, "fights": [last_week]}
    previous = [consensus_fight("Kaik Brito", "Namo Fazil", card_status="on_card")]

    annotate_consensus(
        payload,
        card,
        previous_fights=previous,
        previous_card_name="Dana White's Contender Series: Season 10, Week 2",
    )
    assert [f["display"] for f in payload["fights"]] == ["Islam Makhachev vs Ian Machado Garry"]


def test_an_unnamed_previous_card_carries_nothing_over():
    """No proof it is the same event, so the unmatched fight is dropped."""
    card = parse_card(espn_event(fights=[("Islam Makhachev", "Ian Machado Garry", "STATUS_SCHEDULED")]))
    payload = {"event": {}, "fights": [consensus_fight("Mackenzie Dern", "Jillian Robertson")]}
    previous = [consensus_fight("Mackenzie Dern", "Jillian Robertson", card_status="on_card")]
    annotate_consensus(payload, card, previous_fights=previous)
    assert all(f["display"] != "Mackenzie Dern vs Jillian Robertson" for f in payload["fights"])


def test_annotate_without_a_card_changes_nothing():
    fight = consensus_fight("Islam Makhachev", "Ian Machado Garry")
    payload = {"event": {}, "fights": [dict(fight)]}
    annotate_consensus(payload, None)
    assert payload["fights"][0] == fight
    assert "card" not in payload["event"]


def test_a_failed_card_fetch_drops_nothing_rather_than_guessing():
    # Fail-open: with no card there is no basis to call a fight off-card, so
    # an ESPN outage must not empty the dashboard.
    payload = {"event": {}, "fights": [
        consensus_fight("Islam Makhachev", "Ian Machado Garry"),
        consensus_fight("Matt Adams", "Anthony Wint"),
    ]}
    annotate_consensus(payload, None)
    assert len(payload["fights"]) == 2


def test_event_payload_carries_card_provenance():
    payload = {"event": {}, "fights": []}
    annotate_consensus(payload, parse_card(espn_event()))
    assert payload["event"]["card"]["source"] == "espn"
    assert payload["event"]["card"]["bouts"] == 3


def test_espn_spelling_reaches_the_option_labels_too():
    """The heading was corrected but the percentages underneath still read
    "Larion Douglas 89.7%" on a bout headed "Lerryan Douglas vs …"."""
    card = parse_card(espn_event(fights=[("Lerryan Douglas", "Jamall Emmers", "STATUS_SCHEDULED")]))
    fight = consensus_fight("Larion Douglas", "Jamall Emmers")
    fight["markets"] = [
        {
            "bet_type": "moneyline",
            "options": [{"selection": "Larion Douglas"}, {"selection": "Jamall Emmers"}],
        },
        {
            "bet_type": "method_of_victory",
            "options": [{"selection": "Larion Douglas by KO/TKO"}],
        },
    ]
    payload = {"event": {}, "fights": [fight]}
    annotate_consensus(payload, card)

    kept = payload["fights"][0]
    assert kept["display"] == "Lerryan Douglas vs Jamall Emmers"
    assert [o["selection"] for o in kept["markets"][0]["options"]] == [
        "Lerryan Douglas",
        "Jamall Emmers",
    ]
    assert kept["markets"][1]["options"][0]["selection"] == "Lerryan Douglas by KO/TKO"


def _capper(capper_id, name, confidence, trust, source="video"):
    return {
        "id": capper_id, "name": name, "confidence": confidence,
        "trust": trust, "role": "unknown", "odds": "", "stake": "",
        "reasoning": "", "source": source, "video_url": "",
    }


def _ml_fight(a, b, selection, cappers):
    fight = consensus_fight(a, b)
    fight["markets"] = [
        {
            "bet_type": "moneyline",
            "label": "Moneyline",
            "total_weight": 0,
            "pick_count": len(cappers),
            "options": [
                {
                    "selection": selection,
                    "consensus_pct": 100.0,
                    "weight": 0,
                    "pick_count": len(cappers),
                    "avg_confidence": 0,
                    "stated_pick_count": len(cappers),
                    "stated_avg_confidence": 0,
                    "cappers": cappers,
                }
            ],
        }
    ]
    return fight


def test_two_spellings_of_one_bout_are_merged_not_listed_twice():
    """The card was rendering the same bout twice — 22 picks under one
    spelling, 2 under another — because both matched the same ESPN bout."""
    card = parse_card(espn_event(fights=[("Jackson McVey", "Wes Schultz", "STATUS_SCHEDULED")]))
    big = _ml_fight("Jackson McVey", "Wes Schultz", "Jackson McVey",
                    [_capper("a", "A", 8, 7.0), _capper("b", "B", 6, 5.0)])
    small = _ml_fight("Jackson McVey", "Wes Schiltz", "Jackson McVey",
                      [_capper("c", "C", 4, 5.0)])
    payload = {"event": {}, "fights": [big, small]}

    annotate_consensus(payload, card)

    assert len(payload["fights"]) == 1
    fight = payload["fights"][0]
    assert fight["display"] == "Jackson McVey vs Wes Schultz"
    option = fight["markets"][0]["options"][0]
    assert [c["id"] for c in option["cappers"]] == ["a", "b", "c"]
    assert option["pick_count"] == 3
    # 5.6 + 3.0 + 2.0, recomputed rather than added from stale totals.
    assert option["weight"] == pytest.approx(10.6)
    assert option["consensus_pct"] == 100.0
    assert fight["pick_count"] == 3


def test_merging_keeps_one_vote_per_capper():
    card = parse_card(espn_event(fights=[("Jackson McVey", "Wes Schultz", "STATUS_SCHEDULED")]))
    strong = _ml_fight("Jackson McVey", "Wes Schultz", "Jackson McVey",
                       [_capper("a", "A", 9, 7.0)])
    weak = _ml_fight("Jackson McVey", "Wes Schiltz", "Jackson McVey",
                     [_capper("a", "A", 3, 7.0)])
    payload = {"event": {}, "fights": [strong, weak]}

    annotate_consensus(payload, card)

    option = payload["fights"][0]["markets"][0]["options"][0]
    assert option["pick_count"] == 1
    assert option["cappers"][0]["confidence"] == 9


def test_a_cancellation_does_not_rederive_itself_forever():
    """A fight flagged cancelled appears in the next run's previous payload as
    cancelled — which used to be enough to flag it again, for ever."""
    card = parse_card(espn_event(fights=[("Islam Makhachev", "Ian Machado Garry", "STATUS_SCHEDULED")]))
    stale = consensus_fight("Kaik Brito", "Namo Fazil")
    payload = {"event": {}, "fights": [stale]}
    # Last run's payload, carrying it as cancelled but with no record of ever
    # having been on this card.
    previous = [consensus_fight("Kaik Brito", "Namo Fazil", card_status="cancelled")]

    annotate_consensus(
        payload, card, previous_fights=previous, previous_card_name=card["name"]
    )
    assert all("Brito" not in f["display"] for f in payload["fights"])


def test_a_cancellation_from_this_card_still_persists():
    card = parse_card(espn_event(fights=[("Islam Makhachev", "Ian Machado Garry", "STATUS_SCHEDULED")]))
    gone = consensus_fight("Mackenzie Dern", "Jillian Robertson")
    payload = {"event": {}, "fights": [gone]}
    previous = [
        dict(
            consensus_fight("Mackenzie Dern", "Jillian Robertson", card_status="cancelled"),
            cancelled_from_card=card["name"],
        )
    ]

    annotate_consensus(
        payload, card, previous_fights=previous, previous_card_name=card["name"]
    )
    kept = next(f for f in payload["fights"] if "Dern" in f["display"])
    assert kept["card_status"] == "cancelled"
    # And it keeps its stamp, so it survives the run after this one too.
    assert kept["cancelled_from_card"] == card["name"]
