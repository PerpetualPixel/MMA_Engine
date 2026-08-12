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
    """A scoreboard event shaped like ESPN's MMA payload — main event FIRST,
    the way ESPN lists a card."""
    fights = fights if fights is not None else [
        ("Islam Makhachev", "Ian Machado Garry", "STATUS_SCHEDULED"),
        ("Mackenzie Dern", "Jillian Robertson", "STATUS_SCHEDULED"),
        ("Edson Barboza", "Esteban Ribovics", "STATUS_CANCELED"),
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


def test_parse_card_orders_chronologically_main_event_last():
    card = parse_card(espn_event())
    orders = {f["fighter_a"]: f["order"] for f in card["fights"]}
    # ESPN listed Makhachev first (main event) — chronologically he is last.
    assert orders["Islam Makhachev"] == 2
    assert orders["Edson Barboza"] == 0
    assert card["fights"][2]["cancelled"] is True


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


def test_annotate_flags_off_card_picks_instead_of_letting_them_impersonate():
    # The DWCS case: a real pick from the same video, for a different event.
    payload = {"event": {}, "fights": [consensus_fight("Matt Adams", "Anthony Wint")]}
    annotate_consensus(payload, parse_card(espn_event()))
    assert payload["fights"][0]["card_status"] == "off_card"
    assert "card_order" not in payload["fights"][0]


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


def test_event_payload_carries_card_provenance():
    payload = {"event": {}, "fights": []}
    annotate_consensus(payload, parse_card(espn_event()))
    assert payload["event"]["card"]["source"] == "espn"
    assert payload["event"]["card"]["bouts"] == 3
