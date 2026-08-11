"""Step 5 — turn the consensus into a weighted picks feed for the website.

Reads the `docs/data.json` payload the pipeline just built and distills it
into `docs/picks.json`: one entry per market, naming the consensus selection
and scoring how strongly the cappers back it. PerpetualPicks.com (or any
other consumer) reads that one file — no Python required on the website side.

Each market's top option gets a 0–10 strength score:

    conviction = consensus_pct / 100          # how one-sided the market is
    backing    = min(1.0, weight / 20.0)      # how much trust-weight is behind it
    strength   = 10 * conviction * (0.5 + 0.5 * backing)

Conviction alone can mislead — one capper picking unopposed is "100%
consensus" — so backing scales the score by the absolute trust weight cast.
20+ weight (roughly three high-trust cappers at high confidence) counts as
fully backed. Strength maps to a tier and a suggested stake:

    strength >= 7.5  ->  "strong"  (2.0 units)
    strength >= 5.0  ->  "lean"    (1.0 unit)
    otherwise        ->  "pass"    (0 units)

Each pick also carries `comments`: the backing cappers' own reasoning for the
consensus selection, verbatim from the extraction step, so the website can
show WHY the cappers like the pick, not just that they do. Additive field —
schema_version stays 1; consumers that only read the original fields are
unaffected.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

FULL_BACKING_WEIGHT = 20.0
STRONG_THRESHOLD = 7.5
LEAN_THRESHOLD = 5.0
UNITS = {"strong": 2.0, "lean": 1.0, "pass": 0.0}


def _strength(consensus_pct: float, weight: float) -> float:
    conviction = consensus_pct / 100.0
    backing = min(1.0, weight / FULL_BACKING_WEIGHT)
    return round(10.0 * conviction * (0.5 + 0.5 * backing), 1)


def _tier(strength: float) -> str:
    if strength >= STRONG_THRESHOLD:
        return "strong"
    if strength >= LEAN_THRESHOLD:
        return "lean"
    return "pass"


def _comments(option: dict[str, Any]) -> list[dict[str, Any]]:
    """The backing cappers' reasoning, ordered by trust so the most
    trusted voice reads first. Cappers whose extraction produced no
    reasoning text are skipped rather than emitted as empty bullets."""
    backers = sorted(
        option.get("cappers", []),
        key=lambda c: c.get("trust", 0.0),
        reverse=True,
    )
    return [
        {
            "capper": c.get("name") or c.get("id") or "Unknown capper",
            "comment": c["reasoning"].strip(),
            "confidence": c.get("confidence"),
        }
        for c in backers
        if c.get("reasoning", "").strip()
    ]


def build_picks(consensus: dict[str, Any]) -> dict[str, Any]:
    """Distill a consensus payload into the weighted picks feed."""
    picks: list[dict[str, Any]] = []

    for fight in consensus.get("fights", []):
        for market in fight.get("markets", []):
            options = market.get("options", [])
            if not options:
                continue
            top = max(options, key=lambda o: (o["weight"], o["pick_count"]))
            strength = _strength(top["consensus_pct"], top["weight"])
            tier = _tier(strength)
            picks.append(
                {
                    "fight_id": fight["fight_id"],
                    "fight": fight["display"],
                    "market": market["bet_type"],
                    "market_label": market["label"],
                    "selection": top["selection"],
                    "consensus_pct": top["consensus_pct"],
                    "weight": top["weight"],
                    "pick_count": top["pick_count"],
                    "avg_confidence": top["avg_confidence"],
                    "strength": strength,
                    "tier": tier,
                    "suggested_units": UNITS[tier],
                    "comments": _comments(top),
                }
            )

    picks.sort(key=lambda p: (p["strength"], p["weight"]), reverse=True)

    return {
        # Bump this if the feed shape changes in a way that could break
        # PerpetualPicks.com reading docs/picks.json.
        "schema_version": 1,
        "generated_at": consensus.get("generated_at"),
        "event": consensus.get("event", {}),
        "source": "MMA_Engine trust-weighted capper consensus",
        "totals": {
            "picks": len(picks),
            "strong": sum(1 for p in picks if p["tier"] == "strong"),
            "lean": sum(1 for p in picks if p["tier"] == "lean"),
            "pass": sum(1 for p in picks if p["tier"] == "pass"),
        },
        "picks": picks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the weighted picks feed from the consensus payload."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("docs/data.json"),
        help="Consensus payload built by the pipeline (default: docs/data.json)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/picks.json"),
        help="Where to write the picks feed (default: docs/picks.json)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    consensus = json.loads(args.input.read_text(encoding="utf-8"))
    feed = build_picks(consensus)
    args.output.write_text(
        json.dumps(feed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    totals = feed["totals"]
    log.info(
        "Wrote %s: %d picks (%d strong, %d lean, %d pass)",
        args.output,
        totals["picks"],
        totals["strong"],
        totals["lean"],
        totals["pass"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
