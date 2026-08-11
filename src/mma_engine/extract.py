"""Step 2 — turn a transcript into structured picks with the Claude API.

Uses structured outputs (`output_config.format`) so the model is constrained to
the `VideoPicks` schema — there is no JSON to hand-parse and no retry loop for
malformed output. Long transcripts are chunked; picks from every chunk are
merged and de-duplicated.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from .normalize import fight_key, selection_key

log = logging.getLogger(__name__)

BetType = Literal["moneyline", "method_of_victory", "over_under", "round", "prop"]
Role = Literal["underdog", "favorite", "unknown"]


class Pick(BaseModel):
    """One bet the capper stated in the video."""

    fighter_a: str = Field(description="First fighter in the matchup, full name.")
    fighter_b: str = Field(description="Second fighter in the matchup, full name.")
    bet_type: BetType = Field(description="Which market the bet is on.")
    selection: str = Field(
        description=(
            "The side being bet, written plainly: 'Jon Jones' for a moneyline, "
            "'Jon Jones by KO/TKO' for a method, 'Over 2.5' for a total, "
            "'Jon Jones in round 1' for a round bet."
        )
    )
    fighter: str = Field(
        description=(
            "The fighter this bet is on. Empty string for over/under and any "
            "other bet that is not on a specific fighter."
        )
    )
    confidence: int = Field(
        description=(
            "How strongly the capper backs this pick, 1-10. Base it on their "
            "own language and stake size: a lean or 'small play' is 3-5, a "
            "confident pick is 6-8, a stated best bet or large unit play is 9-10."
        )
    )
    role: Role = Field(
        description=(
            "Whether the capper framed this side as the betting underdog or the "
            "favorite. Use 'unknown' if they never indicate which it is."
        )
    )
    odds_american: str = Field(
        description=(
            "American odds the capper quoted, e.g. '+165' or '-220'. Empty "
            "string if they did not state a price."
        )
    )
    stake_units: str = Field(
        description="Stake the capper stated, e.g. '2u'. Empty string if unstated."
    )
    reasoning: str = Field(
        description=(
            "One or two sentences summarizing the capper's actual stated "
            "rationale. Do not invent reasoning they did not give."
        )
    )


class VideoPicks(BaseModel):
    """Every pick found in a single video (or transcript chunk)."""

    event_name: str = Field(
        description=(
            "The event being previewed, e.g. 'UFC 300'. Empty string if the "
            "transcript never names it."
        )
    )
    picks: list[Pick] = Field(description="All picks stated in this transcript.")


SYSTEM_PROMPT = """\
You extract MMA betting picks from transcripts of YouTube betting-preview videos.

The transcripts are machine-generated, so expect missing punctuation, run-on \
text, and misspelled fighter names. Correct obvious transcription errors in \
fighter names to the real fighter's name when you are confident of the identity.

Extract only bets the capper actually endorses for themselves. Specifically:

- Include picks they state they are betting, leaning toward, or recommending, \
including small plays and parlay legs (record each leg as its own pick).
- Exclude fights they explicitly pass on or stay away from.
- Exclude sides they merely discuss, describe as the public's bet, or name as \
the opinion of someone else.
- Exclude a side they raise and then reject in favor of the other fighter — \
record only the side they land on.

When a capper gives both a moneyline and a method or round bet on the same \
fighter, record each as its own pick with its own confidence.

Set the two fighter fields to the full matchup regardless of which side is bet, \
and keep fighter names spelled consistently across every pick in the video.

Report faithfully. If the transcript is not an MMA betting preview, or contains \
no picks at all, return an empty picks list rather than inferring any.\
"""

USER_TEMPLATE = """\
Capper: {capper_name}
Video: {video_url}{chunk_note}

Extract every betting pick from this transcript.

<transcript>
{transcript}
</transcript>\
"""


@dataclass
class ExtractionResult:
    video_id: str
    picks: list[Pick]
    event_name: str = ""
    ok: bool = True
    error: str = ""
    from_cache: bool = False


def chunk_transcript(text: str, max_chars: int) -> list[str]:
    """Split a transcript into <= max_chars pieces, breaking on word boundaries."""
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            # Back off to the last space so a fighter's name isn't split in half.
            space = text.rfind(" ", start, end)
            if space > start:
                end = space
        chunks.append(text[start:end].strip())
        start = end
    return [chunk for chunk in chunks if chunk]


def _clamp_confidence(value: int) -> int:
    return max(1, min(10, int(value)))


def _dedupe(picks: list[Pick]) -> list[Pick]:
    """Collapse picks repeated across chunks, keeping the highest confidence.

    Cappers often restate a pick in a recap at the end of the video, which lands
    in a different chunk and would otherwise double-count that capper's vote.
    """
    best: dict[tuple[str, str, str], Pick] = {}
    for pick in picks:
        key = (
            fight_key(pick.fighter_a, pick.fighter_b),
            pick.bet_type,
            selection_key(pick.bet_type, pick.selection, pick.fighter),
        )
        existing = best.get(key)
        if existing is None or pick.confidence > existing.confidence:
            best[key] = pick
    return list(best.values())


class PickExtractor:
    """Wraps the Claude API call that turns transcript text into `Pick` objects."""

    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        model: str = "claude-opus-5",
        effort: str = "high",
        max_tokens: int = 20000,
        max_chunk_chars: int = 60000,
        cache_dir: str | Path = "cache/extractions",
        use_cache: bool = True,
    ) -> None:
        # A bare Anthropic() resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN,
        # or an `ant auth login` profile — don't require the env var explicitly.
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.max_chunk_chars = max_chunk_chars
        self.cache_dir = Path(cache_dir)
        self.use_cache = use_cache

    # -- caching -----------------------------------------------------------

    def _cache_path(self, video_id: str) -> Path:
        return self.cache_dir / f"{video_id}.json"

    def _read_cache(self, video_id: str) -> ExtractionResult | None:
        if not self.use_cache:
            return None
        path = self._cache_path(video_id)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            picks = [Pick.model_validate(item) for item in payload["picks"]]
        except Exception:
            log.warning("Ignoring unreadable extraction cache: %s", path)
            return None
        return ExtractionResult(
            video_id=video_id,
            picks=picks,
            event_name=payload.get("event_name", ""),
            from_cache=True,
        )

    def _write_cache(self, result: ExtractionResult) -> None:
        if not self.use_cache:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "video_id": result.video_id,
            "model": self.model,
            "event_name": result.event_name,
            "picks": [pick.model_dump() for pick in result.picks],
        }
        self._cache_path(result.video_id).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # -- extraction --------------------------------------------------------

    def _extract_chunk(
        self, transcript: str, capper_name: str, video_url: str, chunk_note: str
    ) -> VideoPicks:
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=self.max_tokens,
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    # Stable across every video, so cache it once per run.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": USER_TEMPLATE.format(
                        capper_name=capper_name,
                        video_url=video_url,
                        chunk_note=chunk_note,
                        transcript=transcript,
                    ),
                }
            ],
            output_format=VideoPicks,
        )
        if response.stop_reason == "refusal":
            raise RuntimeError(f"Model refused the request: {response.stop_details}")
        if response.parsed_output is None:
            raise RuntimeError(
                f"Model returned no structured output (stop_reason="
                f"{response.stop_reason})"
            )
        return response.parsed_output

    def extract(
        self, video_id: str, transcript: str, capper_name: str, video_url: str
    ) -> ExtractionResult:
        cached = self._read_cache(video_id)
        if cached is not None:
            log.info("[%s] extraction from cache (%d picks)", video_id, len(cached.picks))
            return cached

        chunks = chunk_transcript(transcript, self.max_chunk_chars)
        all_picks: list[Pick] = []
        event_name = ""

        for index, chunk in enumerate(chunks, start=1):
            note = (
                ""
                if len(chunks) == 1
                else f"\n(Part {index} of {len(chunks)} of this video's transcript.)"
            )
            try:
                parsed = self._extract_chunk(chunk, capper_name, video_url, note)
            except anthropic.APIError as exc:
                return ExtractionResult(
                    video_id, picks=[], ok=False, error=f"{type(exc).__name__}: {exc}"
                )
            except RuntimeError as exc:
                return ExtractionResult(video_id, picks=[], ok=False, error=str(exc))
            except Exception as exc:  # one bad video must not abort the run
                log.exception("[%s] unexpected extraction error", video_id)
                return ExtractionResult(
                    video_id,
                    picks=[],
                    ok=False,
                    error=f"Unexpected error: {type(exc).__name__}: {exc}",
                )

            event_name = event_name or parsed.event_name
            for pick in parsed.picks:
                pick.confidence = _clamp_confidence(pick.confidence)
                all_picks.append(pick)

        result = ExtractionResult(
            video_id=video_id, picks=_dedupe(all_picks), event_name=event_name
        )
        log.info(
            "[%s] extracted %d picks across %d chunk(s)",
            video_id,
            len(result.picks),
            len(chunks),
        )
        self._write_cache(result)
        return result
