"""Ingest a predictions-tracker roundup that was only ever *spoken*.

The tracker's pre-event roundup is normally read off its slides, where the
channel names are printed (see `mma_engine.roundup_slides`). Sometimes the
deck isn't available — the video can't be downloaded, the upload is
audio-led, or all that reaches this machine is a transcript someone pasted
in. What the host says is a tally and never a roster:

    "52 channels out of 70 are on Kareckaite, for 74%."

That is a real, wide sample — far wider than the dozen channels whose own
videos the pipeline can fetch — and throwing it away leaves the dashboard
blank for the week. But it cannot go through the normal path, because every
stage after extraction is keyed on *who* cast a vote: the trust weighting,
the one-vote-per-capper rule, the "via tracker" rows in the names overlay.
Inventing 52 names to fill a count is explicitly forbidden (see the rule in
`tracker_picks.SYSTEM_PROMPT`) and would poison the trust scores besides.

So a tally is ingested as what it is: a headcount with no names behind it.

- A tally file lives in `tallies/<slug>.json` and is written by hand from the
  transcript. It holds, per fight, the moneyline split, the method-of-victory
  split, the prices the host read out, and the tracker's own best-predictor
  panel counts.
- It is merged into the payload *after* `build_consensus`, as finished market
  options carrying `pick_count`, `consensus_pct` and `weight` but an empty
  `cappers` list. The dashboard already has a word for an option nobody
  stated a confidence for — `isCounted()` — and renders these the same way,
  minus the names button there are no names for.
- Every channel in a tally counts as one neutral-trust voice at the same
  placeholder confidence a roundup pick gets, so a tallied fight's numbers
  are directly comparable to a fight read off the board where most channels
  were unknown anyway.
- A fight the attributed pipeline already covers wins. The tally is the
  fallback, never an addition on top: counting both would double-count the
  same channels.

Each file is checked against itself on load. The host states a total ("47
channels gave a method") and then the split, so the split must sum to the
total; a file where it doesn't is reported and skipped rather than published,
because a mis-transcribed count is indistinguishable from a real one once it
reaches the dashboard.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable

from .aggregate import BET_TYPE_LABELS, _round
from .normalize import fight_key
from .roster import NEUTRAL_TRUST
from .tracker_picks import METHOD_LABELS, METHODS

log = logging.getLogger(__name__)

SIDES = ("a", "b")

# What a tally option says about itself in the payload, so a reader of
# data.json (and PerpetualPicks.com) can tell a headcount from a roster.
SOURCE_KIND = "tally"


class TallyError(ValueError):
    """A tally file whose own numbers don't reconcile."""


def _as_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TallyError(f"{where}: expected a whole number, got {value!r}")
    if value < 0:
        raise TallyError(f"{where}: counts cannot be negative ({value})")
    return value


def _panel(raw: Any, where: str) -> dict[str, list[int]]:
    """The tracker's own best-predictor panels, as {name: [backing, size]}.

    The host reports these as "six of the eight best predictors are on
    Harris" — a separate, much narrower signal than the full field, and the
    one the dashboard shows alongside the headcount.
    """
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        raise TallyError(f"{where}: expected an object of panel counts")
    panels: dict[str, list[int]] = {}
    for name, pair in raw.items():
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise TallyError(f"{where}.{name}: expected [backing, panel_size]")
        backing = _as_int(pair[0], f"{where}.{name}[0]")
        size = _as_int(pair[1], f"{where}.{name}[1]")
        if size <= 0:
            raise TallyError(f"{where}.{name}: panel size must be positive")
        if backing > size:
            raise TallyError(
                f"{where}.{name}: {backing} of {size} is more than the panel holds"
            )
        panels[name] = [backing, size]
    return panels


class TallyFight:
    """One fight's headcounts, validated against the totals the host stated."""

    def __init__(self, raw: dict[str, Any], where: str) -> None:
        self.fighter_a = str(raw.get("fighter_a") or "").strip()
        self.fighter_b = str(raw.get("fighter_b") or "").strip()
        if not self.fighter_a or not self.fighter_b:
            raise TallyError(f"{where}: both fighters must be named")
        self.where = f"{where} ({self.fighter_a} vs {self.fighter_b})"

        self.notes = [str(n) for n in (raw.get("notes") or [])]
        # Where the bout sits on the night, 0 = first prelim. The ESPN card
        # annotation normally supplies this (and overwrites it if it runs);
        # carrying it here is what lets the dashboard sort a tally-only run
        # earliest-to-latest, with the main event last.
        order = raw.get("card_order")
        self.card_order = (
            _as_int(order, f"{where} card_order") if order is not None else None
        )
        self.odds: dict[str, dict[str, str]] = {
            side: {
                str(k): str(v)
                for k, v in ((raw.get("odds") or {}).get(side) or {}).items()
                if str(v).strip()
            }
            for side in SIDES
        }
        self.sharps: dict[str, dict[str, list[int]]] = {
            side: _panel(
                (raw.get("sharps") or {}).get(side), f"{self.where} sharps.{side}"
            )
            for side in SIDES
        }

        self.moneyline = self._moneyline(raw.get("moneyline") or {})
        self.method = self._method(raw.get("method") or {})
        self.method_sharps = _panel(
            (raw.get("method_sharps") or {}).get("counts"),
            f"{self.where} method_sharps.counts",
        )

    # -- parsing ---------------------------------------------------------
    def _moneyline(self, raw: dict[str, Any]) -> dict[str, int] | None:
        if not raw:
            return None
        where = f"{self.where} moneyline"
        channels = _as_int(raw.get("channels"), f"{where}.channels")
        counts = raw.get("counts") or {}
        stated = {
            side: _as_int(counts[side], f"{where}.counts.{side}")
            for side in SIDES
            if side in counts
        }
        if not stated:
            raise TallyError(f"{where}: no side has a count")
        # The host usually states only the leading side's count against the
        # field ("52 of 70"). A two-way market splits the rest onto the other
        # fighter — arithmetic on two numbers he did say, not a guess.
        if len(stated) == 1:
            side = next(iter(stated))
            other = "b" if side == "a" else "a"
            stated[other] = channels - stated[side]
            if stated[other] < 0:
                raise TallyError(
                    f"{where}: {stated[side]} picks is more than the "
                    f"{channels} channels that voted"
                )
        total = sum(stated.values())
        if total != channels:
            raise TallyError(
                f"{where}: the split sums to {total}, "
                f"but {channels} channels were counted"
            )
        return stated

    def _method(self, raw: dict[str, Any]) -> dict[str, dict[str, int]] | None:
        if not raw:
            return None
        where = f"{self.where} method"
        channels = _as_int(raw.get("channels"), f"{where}.channels")
        counts = raw.get("counts") or {}
        split: dict[str, dict[str, int]] = {}
        for side in SIDES:
            side_counts = counts.get(side) or {}
            unknown = set(side_counts) - set(METHODS)
            if unknown:
                raise TallyError(
                    f"{where}.counts.{side}: unknown method(s) "
                    f"{', '.join(sorted(unknown))}"
                )
            split[side] = {
                method: _as_int(side_counts[method], f"{where}.counts.{side}.{method}")
                for method in METHODS
                if side_counts.get(method)
            }
        total = sum(n for side in split.values() for n in side.values())
        if total != channels:
            raise TallyError(
                f"{where}: the split sums to {total}, "
                f"but {channels} channels were counted"
            )
        return split

    # -- payload ---------------------------------------------------------
    @property
    def key(self) -> str:
        return fight_key(self.fighter_a, self.fighter_b)

    @property
    def display(self) -> str:
        return f"{self.fighter_a} vs {self.fighter_b}"

    def fighter(self, side: str) -> str:
        return self.fighter_a if side == "a" else self.fighter_b

    def board_odds(self) -> dict[str, Any] | None:
        if not any(self.odds.values()):
            return None
        return {
            "fighter_a": self.fighter_a,
            "fighter_b": self.fighter_b,
            "a": dict(self.odds["a"]),
            "b": dict(self.odds["b"]),
            "source": "tracker tally",
        }


def _option(
    selection: str,
    picks: int,
    market_picks: int,
    confidence: int,
    sharp: dict[str, list[int]] | None = None,
) -> dict[str, Any]:
    """One market option, priced in the same units `build_consensus` uses.

    Every channel behind a tally is one unknown voice: neutral trust at the
    roundup's placeholder confidence, which is exactly the weight an
    unrecognised channel gets when its name *is* on the board. So a tallied
    fight's weight is on the same scale as an attributed one's, and
    `optionStrength` on the dashboard ranks them together.
    """
    weight = picks * NEUTRAL_TRUST * (confidence / 10.0)
    option = {
        "selection": selection,
        "consensus_pct": _round(picks / market_picks * 100.0, 1) if market_picks else 0.0,
        "weight": _round(weight),
        "pick_count": picks,
        "avg_confidence": _round(float(confidence), 1),
        # Nobody stated anything: a tally is a headcount. This is the field
        # the dashboard's isCounted() reads to say so.
        "stated_pick_count": 0,
        "stated_avg_confidence": 0.0,
        # There are no names. Deliberately empty rather than absent, so every
        # consumer that iterates `option.cappers` keeps working.
        "cappers": [],
        "source": SOURCE_KIND,
    }
    if sharp:
        option["sharp"] = sharp
    return option


def fight_payload(fight: TallyFight, confidence: int) -> dict[str, Any] | None:
    """Turn one tally fight into the payload shape `build_consensus` emits."""
    markets: list[dict[str, Any]] = []

    if fight.moneyline:
        total = sum(fight.moneyline.values())
        options = [
            _option(
                fight.fighter(side),
                picks,
                total,
                confidence,
                sharp=fight.sharps.get(side),
            )
            for side, picks in fight.moneyline.items()
            if picks
        ]
        if options:
            markets.append(_market("moneyline", options))

    if fight.method:
        total = sum(n for side in fight.method.values() for n in side.values())
        options = []
        for side in SIDES:
            for method, picks in fight.method[side].items():
                sharp = fight.method_sharps.get(f"{side}_{method}")
                options.append(
                    _option(
                        f"{fight.fighter(side)} by {METHOD_LABELS[method]}",
                        picks,
                        total,
                        confidence,
                        sharp={"method": sharp} if sharp else None,
                    )
                )
        if options:
            markets.append(_market("method_of_victory", options))

    if not markets:
        return None

    # The widest market is the honest headline count: a channel that gave a
    # method also gave a winner, so summing the markets would count it twice.
    channels = max(market["pick_count"] for market in markets)
    return {
        "fight_id": fight.key,
        "display": fight.display,
        "fighter_a": fight.fighter_a,
        "fighter_b": fight.fighter_b,
        "pick_count": sum(market["pick_count"] for market in markets),
        "capper_count": channels,
        "markets": markets,
        "source": SOURCE_KIND,
        **({"card_order": fight.card_order} if fight.card_order is not None else {}),
        **({"notes": fight.notes} if fight.notes else {}),
    }


def _market(bet_type: str, options: list[dict[str, Any]]) -> dict[str, Any]:
    options.sort(key=lambda o: (o["weight"], o["pick_count"]), reverse=True)
    return {
        "bet_type": bet_type,
        "label": BET_TYPE_LABELS[bet_type],
        "total_weight": _round(sum(o["weight"] for o in options)),
        "pick_count": sum(o["pick_count"] for o in options),
        "options": options,
        "source": SOURCE_KIND,
    }


class TallyFile:
    """One hand-written tally, loaded and reconciled."""

    def __init__(self, path: Path, raw: dict[str, Any]) -> None:
        self.path = path
        self.event_name = str(raw.get("event_name") or "").strip()
        self.source_url = str(raw.get("source_url") or "").strip()
        self.notes = str(raw.get("notes") or "").strip()
        self.fights = [
            TallyFight(entry, f"{path.name} fight {index + 1}")
            for index, entry in enumerate(raw.get("fights") or [])
        ]


def load_tallies(directory: Path) -> list[TallyFile]:
    """Every readable tally in `directory`, newest filename last.

    Fail-open per file, like every other optional stage: a tally that doesn't
    reconcile is logged and skipped, and the rest of the run is unaffected.
    """
    if not directory.is_dir():
        return []
    loaded: list[TallyFile] = []
    for path in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Skipping tally %s: %s", path, exc)
            continue
        try:
            loaded.append(TallyFile(path, raw))
        except TallyError as exc:
            log.error("Skipping tally %s: %s", path, exc)
    return loaded


def merge_tallies(
    payload: dict[str, Any],
    tallies: Iterable[TallyFile],
    confidence: int = 5,
    board_odds: dict[str, dict[str, Any]] | None = None,
) -> int:
    """Add tallied fights to `payload`, in place. Returns how many were added.

    Only fights the attributed pipeline missed are added: a headcount and a
    roster of the same channels are the same votes, and the roster is the
    better reading of them. Matching is on the surname-sorted fight key, the
    same key `build_consensus` groups on.
    """
    fights = payload.setdefault("fights", [])
    covered = {
        fight_key(f.get("fighter_a", ""), f.get("fighter_b", "")) for f in fights
    }
    added = 0
    names: list[str] = []
    for tally in tallies:
        for fight in tally.fights:
            if fight.key in covered:
                log.info(
                    "Tally skipped for %s — already covered by named picks",
                    fight.display,
                )
                continue
            entry = fight_payload(fight, confidence)
            if entry is None:
                continue
            entry["tally_source"] = {
                "file": tally.path.name,
                "url": tally.source_url,
                "event_name": tally.event_name,
            }
            fights.append(entry)
            covered.add(fight.key)
            added += 1
            names.append(entry["display"])
            if board_odds is not None:
                board = fight.board_odds()
                if board:
                    board.setdefault("video_id", "")
                    board_odds.setdefault(fight.key, board)

    if not added:
        return 0

    fights.sort(key=lambda f: (f.get("capper_count", 0), f.get("pick_count", 0)), reverse=True)
    totals = payload.setdefault("totals", {})
    totals["fights"] = len(fights)
    totals["picks"] = sum(f.get("pick_count", 0) for f in fights)
    # `cappers` counts identities, and a tally has none; the channels behind
    # it are reported separately rather than inflating a roster count the
    # names overlay can't back up.
    totals["channels_tallied"] = max(
        (f.get("capper_count", 0) for f in fights if f.get("source") == SOURCE_KIND),
        default=0,
    )
    log.info("Tallied %d fight(s) with no named attributions: %s", added, ", ".join(names))
    return added
