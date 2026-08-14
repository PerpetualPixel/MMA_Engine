"""Tests for odds.py: live moneyline prices from The Odds API.

All pure-logic — the API payload is a fixture, never a network call. The one
thing exercised against a fake transport is fetch_live_odds' fail-open
contract, which is the behaviour the weekly run depends on.
"""

from __future__ import annotations

import pytest
import requests

from mma_engine.odds import (
    american_to_decimal,
    annotate_odds,
    consensus_price,
    decimal_to_american,
    fetch_live_odds,
    match_event,
    parse_odds_events,
)


def odds_event(home, away, prices, commence_time="2026-08-15T22:00:00Z", event_id="e1"):
    """One event shaped like The Odds API's v4 /odds payload. `prices` maps a
    fighter name to the American line each book hangs on them."""
    books = max((len(v) for v in prices.values()), default=0)
    return {
        "id": event_id,
        "sport_key": "mma_mixed_martial_arts",
        "commence_time": commence_time,
        "home_team": home,
        "away_team": away,
        "bookmakers": [
            {
                "key": f"book{i}",
                "title": f"Book {i}",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": name, "price": lines[i]}
                            for name, lines in prices.items()
                            if i < len(lines)
                        ],
                    }
                ],
            }
            for i in range(books)
        ],
    }


def consensus_fight(fighter_a, fighter_b, **extra):
    return {
        "fight_id": "x|y",
        "display": f"{fighter_a} vs {fighter_b}",
        "fighter_a": fighter_a,
        "fighter_b": fighter_b,
        "markets": [],
        **extra,
    }


# ---------- price arithmetic ----------

@pytest.mark.parametrize(
    "american,decimal",
    [(100, 2.0), (150, 2.5), (-200, 1.5), (-450, 1 + 100 / 450)],
)
def test_american_decimal_roundtrip(american, decimal):
    assert american_to_decimal(american) == pytest.approx(decimal)
    assert decimal_to_american(decimal) == pytest.approx(american, abs=1)


def test_even_money_is_canonicalised_to_plus_100():
    # -100 and +100 are the same price; the decimal form cannot tell them
    # apart, so the round trip settles on the conventional +100.
    assert american_to_decimal(-100) == 2.0
    assert decimal_to_american(2.0) == 100


def test_consensus_price_is_the_median_across_books_not_the_mean():
    # One stale book hanging +400 must not drag the shown price up.
    price = consensus_price([150, 160, 155, 400])
    assert price["american"] == pytest.approx(158, abs=2)
    assert price["books"] == 4


def test_consensus_price_keeps_the_best_number_available():
    price = consensus_price([-200, -180, -150])
    assert price["american"] == -180          # median
    assert price["best_american"] == -150     # longest price on offer


def test_consensus_price_of_nothing_is_none():
    assert consensus_price([]) is None


# ---------- parsing ----------

def test_parse_collects_every_book_for_each_fighter():
    events = parse_odds_events([
        odds_event("Islam Makhachev", "Ian Machado Garry",
                   {"Islam Makhachev": [-450, -430], "Ian Machado Garry": [340, 350]})
    ])
    assert len(events) == 1
    assert events[0]["prices"]["Islam Makhachev"] == [-450, -430]
    assert events[0]["prices"]["Ian Machado Garry"] == [340, 350]


def test_parse_ignores_non_moneyline_markets():
    event = odds_event("Alex Pereira", "Israel Adesanya", {"Alex Pereira": [-150], "Israel Adesanya": [130]})
    event["bookmakers"][0]["markets"].append(
        {"key": "totals", "outcomes": [{"name": "Over", "price": -110}]}
    )
    prices = parse_odds_events([event])[0]["prices"]
    assert set(prices) == {"Alex Pereira", "Israel Adesanya"}


def test_parse_drops_unpriced_and_nonsense_lines():
    # Books emit 0 / null for a market they are not currently pricing, and
    # |price| < 100 is not a real American line.
    event = odds_event("Alex Pereira", "Israel Adesanya", {"Alex Pereira": [-150], "Israel Adesanya": [130]})
    event["bookmakers"][0]["markets"][0]["outcomes"] = [
        {"name": "Alex Pereira", "price": 0},
        {"name": "Israel Adesanya", "price": None},
        {"name": "Someone Unrelated", "price": 50},
    ]
    assert parse_odds_events([event]) == []


def test_parse_survives_a_malformed_event_without_losing_the_good_ones():
    good = odds_event("Alex Pereira", "Israel Adesanya", {"Alex Pereira": [-150], "Israel Adesanya": [130]})
    events = parse_odds_events(["nonsense", {"bookmakers": "wrong type"}, good])
    assert [e["home_team"] for e in events] == ["Alex Pereira"]


def test_parse_of_an_error_body_is_empty_not_an_exception():
    # The API answers a bad key with a JSON object, not a list.
    assert parse_odds_events({"message": "Invalid API key"}) == []


# ---------- matching ----------

def test_match_is_order_independent_because_home_away_is_meaningless_in_mma():
    events = parse_odds_events([
        odds_event("Islam Makhachev", "Ian Machado Garry",
                   {"Islam Makhachev": [-450], "Ian Machado Garry": [340]})
    ])
    fight = consensus_fight("Ian Machado Garry", "Islam Makhachev")
    assert match_event(fight, events) is not None


def test_match_tolerates_the_caption_typos_the_consensus_carries():
    events = parse_odds_events([
        odds_event("Islam Makhachev", "Ian Machado Garry",
                   {"Islam Makhachev": [-450], "Ian Machado Garry": [340]})
    ])
    # "Makhache" is the garble that survives when every capper mangles it.
    assert match_event(consensus_fight("Islam Makhache", "Ian Machado Garry"), events)


def test_match_refuses_a_bout_sharing_only_one_fighter():
    events = parse_odds_events([
        odds_event("Islam Makhachev", "Ian Machado Garry",
                   {"Islam Makhachev": [-450], "Ian Machado Garry": [340]})
    ])
    assert match_event(consensus_fight("Islam Makhachev", "Someone Else"), events) is None


def test_match_prefers_the_earliest_when_a_pairing_appears_twice():
    events = parse_odds_events([
        odds_event("Alex Pereira", "Israel Adesanya", {"Alex Pereira": [-150], "Israel Adesanya": [130]},
                   commence_time="2026-09-20T22:00:00Z", event_id="later"),
        odds_event("Alex Pereira", "Israel Adesanya", {"Alex Pereira": [-160], "Israel Adesanya": [140]},
                   commence_time="2026-08-15T22:00:00Z", event_id="earlier"),
    ])
    assert match_event(consensus_fight("Alex Pereira", "Israel Adesanya"), events)["id"] == "earlier"


# ---------- annotation ----------

def test_annotate_maps_prices_onto_the_fights_own_a_and_b():
    events = parse_odds_events([
        odds_event("Islam Makhachev", "Ian Machado Garry",
                   {"Islam Makhachev": [-450], "Ian Machado Garry": [340]})
    ])
    # fighter_a is the DOG here — the mapping must follow the fight, not the feed.
    payload = {"fights": [consensus_fight("Ian Machado Garry", "Islam Makhachev",
                                          card_status="on_card")]}
    assert annotate_odds(payload, events) == 1
    live = payload["fights"][0]["live_odds"]
    assert live["a"]["american"] == 340
    assert live["b"]["american"] == -450
    assert live["source"] == "the-odds-api"


def test_annotate_skips_off_card_and_cancelled_bouts():
    events = parse_odds_events([
        odds_event("Alex Pereira", "Israel Adesanya", {"Alex Pereira": [-150], "Israel Adesanya": [130]})
    ])
    payload = {"fights": [
        consensus_fight("Alex Pereira", "Israel Adesanya", card_status="off_card"),
        consensus_fight("Alex Pereira", "Israel Adesanya", card_status="cancelled"),
    ]}
    assert annotate_odds(payload, events) == 0
    assert all("live_odds" not in f for f in payload["fights"])


def test_annotate_leaves_unmatched_fights_untouched():
    events = parse_odds_events([
        odds_event("Alex Pereira", "Israel Adesanya", {"Alex Pereira": [-150], "Israel Adesanya": [130]})
    ])
    payload = {"fights": [consensus_fight("Nobody Here", "Someone Else", card_status="on_card")]}
    assert annotate_odds(payload, events) == 0
    assert "live_odds" not in payload["fights"][0]


def test_annotate_records_provenance_on_the_event():
    events = parse_odds_events([
        odds_event("Alex Pereira", "Israel Adesanya", {"Alex Pereira": [-150], "Israel Adesanya": [130]})
    ])
    payload = {"fights": [consensus_fight("Alex Pereira", "Israel Adesanya", card_status="on_card")]}
    annotate_odds(payload, events, fetched_at="2026-08-14T04:00:00+00:00")
    assert payload["event"]["odds"] == {
        "source": "the-odds-api",
        "market": "h2h",
        "fetched_at": "2026-08-14T04:00:00+00:00",
        "fights_priced": 1,
    }


def test_annotate_with_no_events_is_a_no_op_and_stamps_nothing():
    payload = {"fights": [consensus_fight("Alex Pereira", "Israel Adesanya", card_status="on_card")]}
    assert annotate_odds(payload, None) == 0
    assert "odds" not in payload.get("event", {})
    assert "live_odds" not in payload["fights"][0]


def test_annotate_prices_one_side_when_the_feed_only_has_one():
    events = parse_odds_events([
        odds_event("Alex Pereira", "Israel Adesanya", {"Alex Pereira": [-150], "Israel Adesanya": [0]})
    ])
    payload = {"fights": [consensus_fight("Alex Pereira", "Israel Adesanya", card_status="on_card")]}
    assert annotate_odds(payload, events) == 1
    live = payload["fights"][0]["live_odds"]
    assert live["a"]["american"] == -150
    assert live["b"] is None


def test_annotate_refuses_to_price_a_bout_whose_fighters_share_a_surname():
    # normalize.py's known collision. Everywhere else it merely over-groups;
    # here it would hang one side's price on both, showing the favourite's
    # number on the dog. No price beats a confidently wrong one.
    events = parse_odds_events([
        odds_event("Khabib Nurmagomedov", "Umar Nurmagomedov",
                   {"Khabib Nurmagomedov": [-300], "Umar Nurmagomedov": [250]})
    ])
    payload = {"fights": [consensus_fight("Khabib Nurmagomedov", "Umar Nurmagomedov",
                                          card_status="on_card")]}
    assert annotate_odds(payload, events) == 0
    assert "live_odds" not in payload["fights"][0]


# ---------- fail-open contract ----------

class FakeResponse:
    def __init__(self, payload=None, status=200, headers=None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if self._raises:
            raise self._raises
        return self._response


def test_no_api_key_skips_the_request_entirely():
    session = FakeSession()
    assert fetch_live_odds("", session=session) is None
    assert session.calls == []


def test_a_network_failure_returns_none_rather_than_raising():
    session = FakeSession(raises=requests.exceptions.ConnectTimeout("down"))
    assert fetch_live_odds("key", session=session) is None


def test_a_spent_quota_returns_none_rather_than_raising():
    session = FakeSession(FakeResponse(status=401))
    assert fetch_live_odds("key", session=session) is None


def test_a_non_json_body_returns_none_rather_than_raising():
    session = FakeSession(FakeResponse(payload=None))
    assert fetch_live_odds("key", session=session) is None


def test_a_good_response_is_parsed_and_the_key_is_sent_as_a_param():
    payload = [odds_event("Alex Pereira", "Israel Adesanya",
                          {"Alex Pereira": [-150], "Israel Adesanya": [130]})]
    session = FakeSession(FakeResponse(payload, headers={"x-requests-remaining": "497"}))
    events = fetch_live_odds("secret", session=session)
    assert len(events) == 1
    url, params = session.calls[0]
    assert "mma_mixed_martial_arts" in url
    assert params["apiKey"] == "secret"
    assert params["markets"] == "h2h"
    assert params["oddsFormat"] == "american"
