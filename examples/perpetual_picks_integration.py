"""Example: integrate MMA_Engine consensus into PerpetualPicks algorithm.

This shows how to blend the trust-weighted consensus picks with your own
algorithm's predictions for better accuracy.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add src to path if running from examples/
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mma_engine.consensus_client import ConsensusClient


def get_consensus_pick(fight_display: str, bet_type: str = "moneyline") -> dict | None:
    """Get the consensus pick for a specific fight and market.

    Args:
        fight_display: Fight name (e.g. "Islam Makhachev vs Ian Machado Garry")
        bet_type: Market type (default "moneyline")

    Returns:
        Dict with: selection, consensus_pct, weight, pick_count, avg_confidence, cappers
        Or None if the fight/market not found.
    """
    client = ConsensusClient()
    fight = client.fight_by_display(fight_display)
    if not fight:
        return None

    market = client.market_by_type(fight, bet_type)
    if not market or not market.get("options"):
        return None

    # Return the top option (highest consensus)
    return max(market["options"], key=lambda o: o["consensus_pct"])


def blend_predictions(
    your_prediction: float,
    your_confidence: float,
    consensus_pct: float,
    consensus_weight: float,
    blend_ratio: float = 0.5,
) -> tuple[float, str]:
    """Blend your algorithm's prediction with the consensus.

    Args:
        your_prediction: Your algorithm's confidence (0-100)
        your_confidence: How confident you are in your prediction (0-10)
        consensus_pct: Consensus percentage from MMA_Engine (0-100)
        consensus_weight: Total weight backing the consensus (higher = more reliable)
        blend_ratio: How much to weight consensus (0-1). 0.5 = 50/50 blend

    Returns:
        (final_confidence, recommendation)
    """
    # Normalize consensus weight to a 0-10 confidence scale
    # (rough heuristic: every 10 weight units = +1 confidence)
    consensus_confidence = min(10.0, consensus_weight / 10.0)

    # Blend predictions
    blended = (
        your_prediction * your_confidence * (1 - blend_ratio)
        + consensus_pct * consensus_confidence * blend_ratio
    ) / (your_confidence * (1 - blend_ratio) + consensus_confidence * blend_ratio)

    # Recommend based on agreement
    agreement = abs(your_prediction - consensus_pct)
    if agreement < 10:
        recommendation = "Strong agreement — high confidence"
    elif agreement < 20:
        recommendation = "Moderate agreement — proceed with caution"
    else:
        recommendation = "Disagreement — investigate further"

    return blended, recommendation


# Example usage
if __name__ == "__main__":
    print("=== MMA_Engine Consensus Integration Example ===\n")

    # Fetch all fights and show the consensus
    client = ConsensusClient()
    data = client.fetch()

    print(f"Event: {client.event_info().get('name')}")
    print(f"Generated: {data.get('generated_at')}")
    print(f"Picks: {client.totals().get('picks')} across {client.totals().get('fights')} fights\n")

    # Example: blend a hypothetical prediction with the consensus
    print("=== Example: Blend Your Prediction ===\n")

    fight_name = "Islam Makhachev vs Ian Machado Garry"
    consensus = get_consensus_pick(fight_name)

    if consensus:
        print(f"Fight: {fight_name}")
        print(f"Consensus: {consensus['selection']} at {consensus['consensus_pct']}%")
        print(f"  Supporting cappers: {consensus['pick_count']}")
        print(f"  Average confidence: {consensus['avg_confidence']}/10")
        print(f"  Total weight: {consensus['weight']}\n")

        # Hypothetical: your algorithm thinks Islam at 65%
        your_pick_pct = 65.0
        your_confidence = 7.0

        blended, recommendation = blend_predictions(
            your_prediction=your_pick_pct,
            your_confidence=your_confidence,
            consensus_pct=consensus["consensus_pct"],
            consensus_weight=consensus["weight"],
            blend_ratio=0.4,  # 40% consensus, 60% your algorithm
        )

        print(f"Your prediction: {your_pick_pct}% (confidence {your_confidence}/10)")
        print(f"Blended result: {blended:.1f}%")
        print(f"Recommendation: {recommendation}")
    else:
        print(f"Fight '{fight_name}' not in current consensus (may not have been posted yet)")

    # Show all available fights
    print("\n=== All Available Fights ===\n")
    for fight in client.all_fights()[:5]:
        moneyline = client.market_by_type(fight, "moneyline")
        if moneyline and moneyline.get("options"):
            top = moneyline["options"][0]
            print(f"{fight['display']:45} → {top['selection']:25} {top['consensus_pct']:5.1f}%")
