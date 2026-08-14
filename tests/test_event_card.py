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
    annotate_consensus(payload, card, previous_fights=previous)
    assert payload["fights"][0]["card_status"] == "cancelled"


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
