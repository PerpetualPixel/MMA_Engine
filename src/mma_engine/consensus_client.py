"""Fetch and parse the live consensus data for external integration.

Use this to pull the latest consensus from https://perpetualpixel.github.io/MMA_Engine/
and integrate it into external systems like PerpetualPicks.com.

    from mma_engine.consensus_client import ConsensusClient

    client = ConsensusClient()
    data = client.fetch()

    # Find a fight
    fight = client.fight_by_display("Islam Makhachev vs Ian Machado Garry")
    if fight:
        moneyline = client.market_by_type(fight, "moneyline")
        consensus = client.consensus_for_option(moneyline, "Islam Makhachev")
        print(f"{consensus['selection']}: {consensus['consensus_pct']}% ({consensus['pick_count']} picks)")

    # Or iterate all fights and markets
    for fight in data["fights"]:
        print(f"{fight['display']} ({fight['pick_count']} picks)")
        for market in fight["markets"]:
            print(f"  {market['label']}: {len(market['options'])} options")
"""

from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger(__name__)

CONSENSUS_URL = "https://perpetualpixel.github.io/MMA_Engine/docs/data.json"


class ConsensusClient:
    """Fetch and query the live consensus data."""

    def __init__(self, url: str = CONSENSUS_URL, timeout: int = 10) -> None:
        self.url = url
        self.timeout = timeout
        self._cache: dict[str, Any] | None = None

    def fetch(self, use_cache: bool = True) -> dict[str, Any]:
        """Fetch the latest consensus payload.

        Args:
            use_cache: If True, return cached data if available (default True).
                       Set to False to always fetch fresh.

        Returns:
            The full consensus payload: schema_version, generated_at, event,
            totals, sources, fights, discovery (if available).

        Raises:
            requests.RequestException: If the fetch fails.
        """
        if use_cache and self._cache is not None:
            return self._cache

        response = requests.get(self.url, timeout=self.timeout)
        response.raise_for_status()
        self._cache = response.json()
        return self._cache

    def fight_by_display(self, display: str) -> dict[str, Any] | None:
        """Find a fight by its display string (e.g. 'Fighter A vs Fighter B')."""
        data = self.fetch()
        for fight in data.get("fights", []):
            if fight["display"].lower() == display.lower():
                return fight
        return None

    def fight_by_id(self, fight_id: str) -> dict[str, Any] | None:
        """Find a fight by its ID (surname-sorted key)."""
        data = self.fetch()
        for fight in data.get("fights", []):
            if fight["fight_id"] == fight_id:
                return fight
        return None

    def market_by_type(
        self, fight: dict[str, Any], bet_type: str
    ) -> dict[str, Any] | None:
        """Find a market within a fight by type (e.g. 'moneyline', 'method_of_victory')."""
        for market in fight.get("markets", []):
            if market["bet_type"] == bet_type:
                return market
        return None

    def consensus_for_option(
        self, market: dict[str, Any], selection: str
    ) -> dict[str, Any] | None:
        """Find an option within a market by selection name.

        Returns the full option dict with consensus_pct, weight, pick_count, cappers, etc.
        """
        for option in market.get("options", []):
            if option["selection"].lower() == selection.lower():
                return option
        return None

    def all_fights(self) -> list[dict[str, Any]]:
        """Return all fights from the latest consensus."""
        data = self.fetch()
        return data.get("fights", [])

    def event_info(self) -> dict[str, Any]:
        """Return event metadata (name, date, etc.)."""
        data = self.fetch()
        return data.get("event", {})

    def totals(self) -> dict[str, Any]:
        """Return aggregate counts: fights, picks, cappers, videos."""
        data = self.fetch()
        return data.get("totals", {})

    def clear_cache(self) -> None:
        """Clear the cached data so the next fetch hits the network."""
        self._cache = None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    client = ConsensusClient()
    data = client.fetch()

    print(f"Event: {client.event_info().get('name')}")
    print(f"Totals: {client.totals()}\n")

    for fight in client.all_fights()[:3]:
        print(f"{fight['display']}")
        for market in fight["markets"]:
            print(f"  {market['label']}:")
            for option in market["options"]:
                pct = option["consensus_pct"]
                picks = option["pick_count"]
                print(f"    {option['selection']:30} {pct:6.1f}% ({picks:2d} picks)")
        print()
