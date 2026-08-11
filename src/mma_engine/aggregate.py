"""Step 3 — trust-weighted aggregation.

Each pick contributes a weight:

    weight = trust_for(role) * (confidence / 10)

`trust_for(role)` is the capper's underdog score when they framed the bet as a
dog, their favorite score when they framed it as a chalk play, and their overall
score otherwise. A market's consensus percentage for an option is that option's
share of the total weight cast in that market:

    consensus_pct = option_weight / market_total_weight * 100

So a 9.0-trust capper at 10/10 confidence outweighs two 5.0-trust cappers at
4/10 — which is the whole point of tracking trust scores separately.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from .config import Capper
from .extract import Pick
from .normalize import display_name, fight_key, selection_key

BET_TYPE_ORDER = ["moneyline", "method_of_victory", "over_under", "round", "prop"]
BET_TYPE_LABELS = {
    "moneyline": "Moneyline",
    "method_of_victory": "Method of Victory",
    "over_under": "Over / Under",
    "round": "Round",
    "prop": "Prop",
}


@dataclass(frozen=True)
class SourcedPick:
    """A pick tagged with the capper and video it came from."""

    pick: Pick
    capper: Capper
    video_id: str
    video_url: str

    @property
    def weight(self) -> float:
        trust = self.capper.trust_for(self.pick.role)
        return trust * (self.pick.confidence / 10.0)


@dataclass
class _Option:
    """One side of one market, accumulating every capper who backed it."""

    label: str = ""
    sources: list[SourcedPick] = field(default_factory=list)

    @property
    def weight(self) -> float:
        return sum(source.weight for source in self.sources)

    @property
    def avg_confidence(self) -> float:
        if not self.sources:
            return 0.0
        return sum(s.pick.confidence for s in self.sources) / len(self.sources)


def _round(value: float, places: int = 2) -> float:
    return round(value + 0.0, places)


def _option_payload(option: _Option, market_weight: float) -> dict[str, Any]:
    consensus = (option.weight / market_weight * 100.0) if market_weight else 0.0
    supporters = sorted(
        option.sources, key=lambda s: (s.weight, s.capper.name), reverse=True
    )
    return {
        "selection": option.label,
        "consensus_pct": _round(consensus, 1),
        "weight": _round(option.weight),
        "pick_count": len(option.sources),
        "avg_confidence": _round(option.avg_confidence, 1),
        "cappers": [
            {
                "id": s.capper.id,
                "name": s.capper.name,
                "confidence": s.pick.confidence,
                "trust": _round(s.capper.trust_for(s.pick.role)),
                "role": s.pick.role,
                "odds": s.pick.odds_american,
                "stake": s.pick.stake_units,
                "reasoning": s.pick.reasoning,
                "video_url": s.video_url,
            }
            for s in supporters
        ],
    }


def build_consensus(
    sourced_picks: Iterable[SourcedPick],
    event: dict[str, Any] | None = None,
    sources: list[dict[str, Any]] | None = None,
    min_confidence: int = 1,
) -> dict[str, Any]:
    """Aggregate picks into the `data.json` payload the dashboard renders."""
    picks = [s for s in sourced_picks if s.pick.confidence >= min_confidence]

    # fight -> bet_type -> selection_key -> _Option
    grouped: dict[str, dict[str, dict[str, _Option]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    name_variants: dict[str, list[str]] = defaultdict(list)

    for source in picks:
        pick = source.pick
        key = fight_key(pick.fighter_a, pick.fighter_b)
        if not key:
            continue
        name_variants[key].extend([pick.fighter_a, pick.fighter_b])

        market = grouped[key][pick.bet_type]
        option_key = selection_key(pick.bet_type, pick.selection, pick.fighter)
        option = market.get(option_key)
        if option is None:
            option = _Option(label=pick.selection.strip())
            market[option_key] = option
        option.sources.append(source)

    fights: list[dict[str, Any]] = []
    for key, markets in grouped.items():
        names = name_variants[key]
        fighter_a = display_name(names[0::2])
        fighter_b = display_name(names[1::2])

        market_payloads: list[dict[str, Any]] = []
        total_picks = 0
        for bet_type in BET_TYPE_ORDER:
            options = markets.get(bet_type)
            if not options:
                continue
            market_weight = sum(option.weight for option in options.values())
            payloads = sorted(
                (_option_payload(o, market_weight) for o in options.values()),
                key=lambda p: (p["weight"], p["pick_count"]),
                reverse=True,
            )
            total_picks += sum(p["pick_count"] for p in payloads)
            market_payloads.append(
                {
                    "bet_type": bet_type,
                    "label": BET_TYPE_LABELS[bet_type],
                    "total_weight": _round(market_weight),
                    "pick_count": sum(p["pick_count"] for p in payloads),
                    "options": payloads,
                }
            )

        fights.append(
            {
                "fight_id": key,
                "display": f"{fighter_a} vs {fighter_b}".strip(" vs"),
                "fighter_a": fighter_a,
                "fighter_b": fighter_b,
                "pick_count": total_picks,
                "capper_count": len(
                    {
                        s.capper.id
                        for market in markets.values()
                        for option in market.values()
                        for s in option.sources
                    }
                ),
                "markets": market_payloads,
            }
        )

    fights.sort(key=lambda f: (f["capper_count"], f["pick_count"]), reverse=True)

    return {
        # Bump this if the payload shape changes in a way that could break an
        # external consumer (e.g. PerpetualPicks.com) reading docs/data.json.
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": event or {},
        "totals": {
            "fights": len(fights),
            "picks": len(picks),
            "cappers": len({s.capper.id for s in picks}),
            "videos": len({s.video_id for s in picks}),
        },
        "sources": sources or [],
        "fights": fights,
    }
