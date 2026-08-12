"""Build the capper roster from a tracker channel's results video.

Channels like @UFCPredictionsTracker publish periodic recaps ranking MMA
prediction channels by correct-pick rate and ROI, usually split into overall /
favorite / underdog categories — the same three axes this project weights on.
This module turns one of those videos into capper entries with *measured* trust
scores instead of hand-guessed ones.

    python -m mma_engine --roster-from https://youtu.be/VIDEO_ID
    python -m mma_engine --roster-from https://youtu.be/VIDEO_ID --apply-roster

The first form writes `roster_proposal.json` for review; the second also merges
the result into `config.json`. Reviewing first is strongly recommended — this is
one model reading one auto-generated transcript full of channel names.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

Category = Literal["overall", "underdog", "favorite", "method"]

# ROI needed to reach the top of the trust scale. At +20% ROI a capper scores
# 10.0, at 0% they sit at the 5.0 neutral mark, at -20% they bottom out.
ROI_AT_MAX_TRUST = 20.0
NEUTRAL_TRUST = 5.0

# The correct-pick rate that counts as break-even, per category. 50% would be
# right for coin-flips, but MMA picking is favorite-dominated: the tracker's
# own tables show the field's 68-75% overall accuracy maps to just 4-10% ROI,
# so treating 50% as neutral inflates accuracy-only cappers far above what
# their profitability supports. These anchors come from those same tables —
# typical break-even points for the odds each category's picks carry (about
# -170 average for an overall slate, -230 for favorites, +150 for dogs, +230
# for method props) — so an accuracy-derived score lands on the same scale as
# an ROI-derived one.
ACCURACY_NEUTRAL = {
    "overall": 62.5,
    "favorite": 70.0,
    "underdog": 40.0,
    "method": 30.0,
}

# Sample-size shrinkage: a 40% ROI over 12 picks is noise, not skill. Scores are
# pulled toward neutral by n / (n + PRIOR_PICKS), so a capper needs roughly this
# many tracked picks before their raw number counts at ~half weight.
PRIOR_PICKS = 50
# Used when the video cites a rate without saying how many picks it covers.
ASSUMED_PICKS = 25


class CategoryStat(BaseModel):
    """One capper's tracked performance in one betting category."""

    category: Category = Field(
        description=(
            "Which slice of their picks this covers: 'overall' for all picks, "
            "'underdog' for their dog picks only, 'favorite' for favorites "
            "only, 'method' for method-of-victory picks."
        )
    )
    roi_percent: float | None = Field(
        default=None,
        description=(
            "Return on investment as a percentage, e.g. 12.5 for +12.5% ROI, "
            "-8.0 for a loss. Null if the video does not state an ROI for this "
            "capper and category."
        ),
    )
    units: float | None = Field(
        default=None,
        description="Net units won (positive) or lost (negative). Null if unstated.",
    )
    correct_percent: float | None = Field(
        default=None,
        description="Percentage of picks that hit, e.g. 61.5. Null if unstated.",
    )
    picks_tracked: int | None = Field(
        default=None,
        description=(
            "How many picks this figure covers. Null if the video does not say. "
            "Used to discount small samples, so report it when stated."
        ),
    )
    rank: int | None = Field(
        default=None,
        description="Their placement in this category's ranking, if given. 1 is best.",
    )


class TrackedCapper(BaseModel):
    """A prediction channel the tracker video reports results for."""

    name: str = Field(description="The channel/capper name as spoken in the video.")
    channel_handle: str = Field(
        description=(
            "Their YouTube @handle if the video states it, e.g. '@FunkyPicks'. "
            "Empty string if never mentioned — do not guess a handle."
        )
    )
    stats: list[CategoryStat] = Field(
        description="Every category this video reports numbers for."
    )
    notes: str = Field(
        description=(
            "One short sentence of context the video gives, e.g. 'best underdog "
            "hitter of the period' or 'on a losing streak'. Empty if none."
        )
    )


class TrackerReport(BaseModel):
    """Everything extractable from one tracker results video."""

    period: str = Field(
        description=(
            "The period the results cover as stated, e.g. 'first 6 months of "
            "2026' or '10 months'. Empty string if not stated."
        )
    )
    cappers: list[TrackedCapper] = Field(
        description="Every prediction channel with reported results."
    )


SYSTEM_PROMPT = """\
You extract MMA prediction-channel performance data from transcripts of \
"predictions tracker" videos — recaps that rank MMA YouTube cappers by how \
profitable and accurate their picks were over a period.

The transcripts are auto-generated, so channel names are frequently mangled. \
Correct them to the real channel name when you are confident of the identity, \
and keep each channel's name spelled identically everywhere it appears.

Report only figures the video actually states. Specifically:

- Record a number only if it is said for that capper and that category. Never \
carry a figure from one category to another, and never compute a number the \
video did not give.
- Leave a field null rather than estimating it. A missing value is fine; an \
invented one corrupts the weighting.
- Distinguish ROI (profitability) from correct-pick percentage (accuracy). They \
are different metrics and the video usually reports both.
- Categories: use 'overall' for all-picks figures, 'underdog' when the figure \
covers only underdog picks, 'favorite' when it covers only favorites, and \
'method' for method-of-victory (KO/sub/decision) figures.
- Include every channel given results, including ones that performed badly.
- Capture the sample size when stated — a rate over a handful of picks is \
treated very differently from one over hundreds.

If the transcript is not a tracker results video, return an empty capper list.\
"""

USER_TEMPLATE = """\
Video: {video_url}

Extract the tracked performance of every MMA prediction channel in this recap.

<transcript>
{transcript}
</transcript>\
"""


def slugify(name: str) -> str:
    """Turn a channel name into a stable config id."""
    cleaned = "".join(c.lower() if c.isalnum() else " " for c in name)
    return "_".join(cleaned.split())[:40] or "capper"


def name_key(name: str) -> str:
    """Normalize a capper name for matching: lowercase, alphanumerics only."""
    return "".join(c.lower() for c in (name or "") if c.isalnum())


def _shrink(raw: float, picks: int) -> float:
    """Pull a raw score toward neutral in proportion to how little data backs it."""
    confidence = picks / (picks + PRIOR_PICKS)
    return round(max(1.0, min(10.0, NEUTRAL_TRUST + (raw - NEUTRAL_TRUST) * confidence)), 1)


def trust_from_totals(totals: dict[str, Any], category: str = "overall") -> float | None:
    """Convert a category's pooled record into a 1-10 trust score.

    ROI is preferred — profitability is what the weighting cares about. Falls
    back to correct-pick rate, measured against the category's break-even
    accuracy (see ACCURACY_NEUTRAL — 70% on favorites is roughly break-even,
    41% on method props is elite). Small samples are pulled toward neutral so
    a hot streak over ten picks cannot mint a 10.0.
    """
    roi = totals.get("roi") or {}
    if roi.get("picks"):
        raw = NEUTRAL_TRUST + (roi["value"] / ROI_AT_MAX_TRUST) * NEUTRAL_TRUST
        return _shrink(raw, roi["picks"])

    correct = totals.get("correct") or {}
    if correct.get("picks"):
        # Break-even accuracy is neutral; every point above adds 0.2 trust.
        neutral_accuracy = ACCURACY_NEUTRAL.get(category, ACCURACY_NEUTRAL["overall"])
        raw = NEUTRAL_TRUST + (correct["value"] - neutral_accuracy) * 0.2
        return _shrink(raw, correct["picks"])
    return None


def stat_to_totals(stat: CategoryStat) -> dict[str, Any]:
    """Represent one video's figure as a pooled record of sample size one video."""
    picks = stat.picks_tracked if stat.picks_tracked else ASSUMED_PICKS
    totals: dict[str, Any] = {}
    if stat.roi_percent is not None:
        totals["roi"] = {"picks": picks, "value": float(stat.roi_percent)}
    if stat.correct_percent is not None:
        totals["correct"] = {"picks": picks, "value": float(stat.correct_percent)}
    return totals


def pool_totals(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Combine two pooled records into a pick-weighted average.

    This is what makes per-event reviews compound: each new card adds its picks
    to the sample, so the trust score converges on the capper's real edge instead
    of swinging with the latest result.
    """
    pooled: dict[str, Any] = {}
    for metric in ("roi", "correct"):
        old, new = existing.get(metric), incoming.get(metric)
        if not old and not new:
            continue
        if not old:
            pooled[metric] = dict(new)
            continue
        if not new:
            pooled[metric] = dict(old)
            continue
        picks = old["picks"] + new["picks"]
        value = (old["value"] * old["picks"] + new["value"] * new["picks"]) / picks
        pooled[metric] = {"picks": picks, "value": round(value, 2)}
    return pooled


def trust_from_record(record: dict[str, Any]) -> dict[str, float]:
    """Derive the per-category trust scores from a capper's pooled record."""
    scores: dict[str, float] = {}
    for category in ("overall", "underdog", "favorite", "method"):
        score = trust_from_totals(record.get(category) or {}, category)
        if score is not None:
            scores[category] = score

    overall = scores.get("overall", NEUTRAL_TRUST)
    return {
        "overall": overall,
        # A capper with no category-specific number inherits their overall score
        # rather than a made-up specialty rating.
        "underdog": scores.get("underdog", overall),
        "favorite": scores.get("favorite", overall),
        "method": scores.get("method", overall),
    }


def build_capper_entry(capper: TrackedCapper, video_id: str = "") -> dict[str, Any]:
    """Turn one tracked capper into a `config.json` capper entry."""
    record = {
        stat.category: stat_to_totals(stat)
        for stat in capper.stats
        if stat_to_totals(stat)
    }
    return {
        "id": slugify(capper.name),
        "name": capper.name,
        "channel_url": (
            f"https://www.youtube.com/{capper.channel_handle.lstrip('/')}"
            if capper.channel_handle
            else ""
        ),
        "discover": bool(capper.channel_handle),
        "trust": trust_from_record(record),
        "tracked": {
            "notes": capper.notes,
            "videos": [video_id] if video_id else [],
            "record": record,
        },
    }


class RosterExtractor:
    """Reads a tracker video's transcript and proposes a capper roster."""

    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        model: str = "claude-opus-5",
        effort: str = "high",
        max_tokens: int = 20000,
    ) -> None:
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens

    def extract(self, transcript: str, video_url: str) -> TrackerReport:
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=self.max_tokens,
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": USER_TEMPLATE.format(
                        video_url=video_url, transcript=transcript
                    ),
                }
            ],
            output_format=TrackerReport,
        )
        if response.stop_reason == "refusal":
            raise RuntimeError(f"Model refused the request: {response.stop_details}")
        if response.parsed_output is None:
            raise RuntimeError(
                f"Model returned no structured output (stop_reason={response.stop_reason})"
            )
        return response.parsed_output


def merge_into_config(
    config_path: Path,
    proposed: list[dict[str, Any]],
    video_id: str = "",
    mode: str = "accumulate",
) -> dict[str, list[str]]:
    """Merge proposed cappers into `config.json`. Returns a per-outcome report.

    Existing cappers are matched by id, by channel URL, or by normalized name —
    including any spellings listed in the entry's optional `aliases` array. The
    aliases exist because tracker transcripts garble channel names ("Bet Sam"
    for BetSlam with Sam, "Live It Larry" for Livid Larry); listing the garbled
    form on the real capper routes its results to them instead of minting a
    duplicate. `channel_url` and the `discover` flag on an existing capper are
    left alone — only the tracked record and the trust scores derived from it
    are touched.

    Two modes:

    - `accumulate` (default) pools this video's numbers with everything already
      recorded, then recomputes trust from the combined sample. This is the mode
      for post-event reviews: every card refines the estimate.
    - `replace` discards the stored record and uses this video alone. This is the
      mode for a long-period recap, which already covers the same picks as the
      per-event videos and would double-count if pooled.

    A video is applied to a capper at most once; re-running the same video is a
    no-op rather than counting its picks twice.
    """
    config = json.loads(config_path.read_text(encoding="utf-8"))
    existing = config.setdefault("cappers", [])

    by_id = {entry.get("id"): entry for entry in existing}
    by_url = {
        entry.get("channel_url", "").rstrip("/").lower(): entry
        for entry in existing
        if entry.get("channel_url")
    }
    by_name: dict[str, dict[str, Any]] = {}
    for entry in existing:
        for spelling in [entry.get("name", "")] + list(entry.get("aliases") or []):
            key = name_key(spelling)
            if key:
                by_name.setdefault(key, entry)

    report: dict[str, list[str]] = {"added": [], "updated": [], "skipped": []}

    for candidate in proposed:
        url_key = candidate.get("channel_url", "").rstrip("/").lower()
        target = (
            by_id.get(candidate["id"])
            or (by_url.get(url_key) if url_key else None)
            or by_name.get(name_key(candidate["name"]))
        )

        if target is None:
            existing.append(candidate)
            by_id[candidate["id"]] = candidate
            if url_key:
                by_url[url_key] = candidate
            by_name.setdefault(name_key(candidate["name"]), candidate)
            report["added"].append(candidate["name"])
            continue

        tracked = target.setdefault("tracked", {"notes": "", "videos": [], "record": {}})
        seen = tracked.setdefault("videos", [])
        if video_id and video_id in seen:
            report["skipped"].append(target.get("name", candidate["name"]))
            continue

        incoming = candidate["tracked"]["record"]
        if mode == "replace":
            tracked["record"] = incoming
            tracked["videos"] = [video_id] if video_id else []
        else:
            stored = tracked.setdefault("record", {})
            for category, totals in incoming.items():
                stored[category] = pool_totals(stored.get(category) or {}, totals)
            if video_id:
                seen.append(video_id)

        tracked["notes"] = candidate["tracked"]["notes"] or tracked.get("notes", "")
        target["trust"] = trust_from_record(tracked["record"])
        report["updated"].append(target.get("name", candidate["name"]))

    config_path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return report
