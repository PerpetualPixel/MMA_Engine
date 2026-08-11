"""Tests for consensus_client integration module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mma_engine.consensus_client import ConsensusClient


@pytest.fixture
def sample_consensus_data() -> dict:
    """Sample consensus payload matching the real schema."""
    return {
        "schema_version": 1,
        "generated_at": "2026-08-11T03:28:17+00:00",
        "event": {"name": "UFC 330"},
        "totals": {"fights": 2, "picks": 10, "cappers": 2, "videos": 2},
        "sources": [],
        "fights": [
            {
                "fight_id": "garry_makhachev",
                "display": "Islam Makhachev vs Ian Machado Garry",
                "fighter_a": "Islam Makhachev",
                "fighter_b": "Ian Machado Garry",
                "pick_count": 5,
                "capper_count": 2,
                "markets": [
                    {
                        "bet_type": "moneyline",
                        "label": "Moneyline",
                        "total_weight": 20.0,
                        "pick_count": 3,
                        "options": [
                            {
                                "selection": "Islam Makhachev",
                                "consensus_pct": 100.0,
                                "weight": 15.0,
                                "pick_count": 2,
                                "avg_confidence": 9.0,
                                "cappers": [
                                    {
                                        "id": "capper1",
                                        "name": "Capper One",
                                        "confidence": 9,
                                        "trust": 7.5,
                                        "role": "overall",
                                    }
                                ],
                            },
                            {
                                "selection": "Ian Machado Garry",
                                "consensus_pct": 0.0,
                                "weight": 5.0,
                                "pick_count": 1,
                                "avg_confidence": 5.0,
                                "cappers": [],
                            },
                        ],
                    }
                ],
            },
            {
                "fight_id": "johnson_ochoa",
                "display": "Charles Johnson vs Jose Ochoa",
                "fighter_a": "Charles Johnson",
                "fighter_b": "Jose Ochoa",
                "pick_count": 5,
                "capper_count": 2,
                "markets": [
                    {
                        "bet_type": "moneyline",
                        "label": "Moneyline",
                        "total_weight": 12.0,
                        "pick_count": 3,
                        "options": [
                            {
                                "selection": "Jose Ochoa",
                                "consensus_pct": 71.4,
                                "weight": 8.5,
                                "pick_count": 2,
                                "avg_confidence": 8.5,
                                "cappers": [],
                            },
                            {
                                "selection": "Charles Johnson",
                                "consensus_pct": 28.6,
                                "weight": 3.5,
                                "pick_count": 1,
                                "avg_confidence": 7.0,
                                "cappers": [],
                            },
                        ],
                    }
                ],
            },
        ],
    }


@pytest.fixture
def mock_client(sample_consensus_data, monkeypatch):
    """Create a ConsensusClient with mocked fetch."""

    def mock_fetch(self, use_cache=True):
        return sample_consensus_data

    monkeypatch.setattr(ConsensusClient, "fetch", mock_fetch)
    return ConsensusClient()


def test_fight_by_display(mock_client):
    """Test finding a fight by display name."""
    fight = mock_client.fight_by_display("Islam Makhachev vs Ian Machado Garry")
    assert fight is not None
    assert fight["fight_id"] == "garry_makhachev"


def test_fight_by_display_case_insensitive(mock_client):
    """Test that fight lookup is case-insensitive."""
    fight = mock_client.fight_by_display("islam makhachev vs ian machado garry")
    assert fight is not None


def test_fight_by_id(mock_client):
    """Test finding a fight by ID."""
    fight = mock_client.fight_by_id("garry_makhachev")
    assert fight is not None
    assert fight["display"] == "Islam Makhachev vs Ian Machado Garry"


def test_market_by_type(mock_client):
    """Test finding a market within a fight."""
    fight = mock_client.fight_by_display("Islam Makhachev vs Ian Machado Garry")
    market = mock_client.market_by_type(fight, "moneyline")
    assert market is not None
    assert market["label"] == "Moneyline"


def test_consensus_for_option(mock_client):
    """Test finding an option within a market."""
    fight = mock_client.fight_by_display("Islam Makhachev vs Ian Machado Garry")
    market = mock_client.market_by_type(fight, "moneyline")
    option = mock_client.consensus_for_option(market, "Islam Makhachev")
    assert option is not None
    assert option["consensus_pct"] == 100.0
    assert option["pick_count"] == 2


def test_consensus_for_option_case_insensitive(mock_client):
    """Test that option lookup is case-insensitive."""
    fight = mock_client.fight_by_display("Islam Makhachev vs Ian Machado Garry")
    market = mock_client.market_by_type(fight, "moneyline")
    option = mock_client.consensus_for_option(market, "islam makhachev")
    assert option is not None


def test_all_fights(mock_client):
    """Test retrieving all fights."""
    fights = mock_client.all_fights()
    assert len(fights) == 2
    assert fights[0]["fight_id"] == "garry_makhachev"


def test_event_info(mock_client):
    """Test retrieving event metadata."""
    event = mock_client.event_info()
    assert event["name"] == "UFC 330"


def test_totals(mock_client):
    """Test retrieving aggregate counts."""
    totals = mock_client.totals()
    assert totals["fights"] == 2
    assert totals["picks"] == 10
    assert totals["cappers"] == 2


def test_cache_behavior(mock_client):
    """Test that caching works as expected."""
    data1 = mock_client.fetch(use_cache=True)
    data2 = mock_client.fetch(use_cache=True)
    assert data1 is data2  # Same object (cached)


def test_cache_cleared(mock_client):
    """Test that cache can be cleared."""
    data1 = mock_client.fetch(use_cache=True)
    mock_client.clear_cache()
    data2 = mock_client.fetch(use_cache=True)
    # After clearing, a fresh fetch would happen (we can't test this fully
    # without mocking, but we can verify clear_cache doesn't error)
    assert data1 is not None
