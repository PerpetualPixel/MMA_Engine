"""Read a roundup's picks off its slides, because they were never spoken.

The transcript path (`tracker_picks`) assumes the roundup host reads the
channel names aloud. They don't. A predictions-tracker roundup is a slide
deck: the two fighters at either edge, a dotted line down the middle, and
eighty-odd channel names printed in columns on whichever side they picked —
while the audio says "eighty of eighty-one are on Dyer" and never names one.
Extraction from captions was therefore correct to return nothing, and nothing
is what the consensus got.

So the names come out of the pixels. Two ways in, both landing in the same
`TrackerRoundup` shape the transcript path produces, merged by the same
`merge_roundups` — every fight the deck covers, with the channels on each
side:

    slides from the video      yt-dlp fetches it, ffmpeg cuts a frame at each
                               scene change, one vision call reads each frame
    slides you captured        screenshots dropped in a folder, same reader
                               (--roundup-slides DIR)

ffmpeg comes from the `imageio-ffmpeg` wheel, so a `pip install -r
requirements.txt` is all the setup there is — no system ffmpeg, which matters
on the Windows machine this runs on weekly.

Every frame is cached by the hash of its own bytes, so a run that dies partway
(a spent API balance, most likely) resumes for free instead of re-reading the
deck from the top. Failure anywhere here is non-fatal: no ffmpeg, no yt-dlp,
a video that won't download, a frame the model can't parse — each is logged
and the run continues on whatever the other sources gave it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import anthropic

from pydantic import BaseModel, Field

from .tracker_picks import TrackerFightPicks, TrackerRoundup

log = logging.getLogger(__name__)

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")
MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
# Reading names off a clean, high-contrast slide is closer to OCR than to
# judgement, and a deck runs to dozens of frames — so the slide reader gets
# its own model setting, defaulting cheaper than the extraction model.
DEFAULT_SLIDE_MODEL = "claude-sonnet-5"
# How different a frame must be from the last kept one to count as a new
# slide. Deliberately low: two slides about the same bout (the moneyline
# board, then the method-of-victory board) share their photos and layout and
# score far below an obvious cut, and missing one loses a whole fight's
# attributions. mpdecimate below drops the true duplicates this lets through,
# and an extra frame only costs one cached vision call.
DEFAULT_SCENE_THRESHOLD = 0.15
DEFAULT_MAX_FRAMES = 80
# 720p is enough to read the smallest name on these decks; higher just costs
# download time and image tokens.
DEFAULT_HEIGHT = 720

SYSTEM_PROMPT = """\
You read one slide from an MMA "predictions tracker" roundup and report which \
prediction channels picked which fighter.

The layout is always the same: one fighter's name and photo at the left edge, \
the other's at the right edge, and between them the names of YouTube \
prediction channels, printed in columns. A vertical line of dots usually \
divides the two fighters' supporters.

- A channel's pick is the fighter it is printed nearest to, on that fighter's \
side of the divider. One fighter's supporters are often split across two or \
more columns — those columns are all still that fighter's, as long as they \
sit on that fighter's side of the divider.
- Copy each channel name exactly as printed, including any parenthetical, \
e.g. "MCApicks (Carlos)", "Let's Talk MMA (Brodie)".
- Ignore everything that is not a channel name: the odds tables, the "YouTube \
Predictions" tallies, break-even percentages, and any stars, trophies, or \
colour coding marking a channel as a top predictor or naming a method of \
victory. Those decorate a name; they never change whose side it is on.
- Report every channel you can read. If part of the slide is cut off or \
illegible, report what you can read and leave the rest out — never guess a \
name, and never invent names to match a count printed on the slide.
- Separately, report the tally the slide prints for each fighter (the \
numerator of "YouTube Predictions 80/81", or 0 if the slide prints no such \
number). Copy what is printed; never substitute your own count of the names. \
The two are compared afterwards to detect names that went unread.

If this image is not a fighter-versus-fighter slide with channel names on it \
— an intro, a talking head, a results recap, a title card — return an empty \
fights list.\
"""

USER_TEXT = """\
Read this roundup slide: the two fighters, and which channels picked each.\
"""


class SlideFight(BaseModel):
    """One bout's board: the two fighters and the channels under each."""

    fighter_a: str = Field(description="The fighter named at the LEFT edge of the slide.")
    fighter_b: str = Field(description="The fighter named at the RIGHT edge of the slide.")
    cappers_for_a: list[str] = Field(
        description="Every channel name printed on the left fighter's side."
    )
    cappers_for_b: list[str] = Field(
        description="Every channel name printed on the right fighter's side."
    )
    stated_count_a: int = Field(
        description=(
            "The count the slide itself prints for the left fighter, if any — "
            "the numerator of a 'YouTube Predictions 80/81' style tally. 0 when "
            "the slide prints no such tally. Do not compute it by counting the "
            "names; report only a number printed on the slide."
        )
    )
    stated_count_b: int = Field(
        description="The same printed tally for the right fighter, 0 if absent."
    )


class SlideBoard(BaseModel):
    """What one slide image contains."""

    fights: list[SlideFight] = Field(
        description=(
            "The bout this slide covers, or an empty list if the image is not a "
            "fighter-versus-fighter board with channel names on it."
        )
    )


def board_to_roundup(board: SlideBoard) -> tuple[TrackerRoundup, list[str]]:
    """The board in the shared roundup shape, plus any count discrepancies.

    These slides print their own tally ("YouTube Predictions 80/81"), which
    makes them self-checking: if the reader comes back with 74 names where the
    slide says 80, six channels went unread and the consensus is quietly
    short. That is worth saying out loud rather than discovering never.
    """
    gaps: list[str] = []
    fights: list[TrackerFightPicks] = []
    for fight in board.fights:
        for name, read, stated in (
            (fight.fighter_a, fight.cappers_for_a, fight.stated_count_a),
            (fight.fighter_b, fight.cappers_for_b, fight.stated_count_b),
        ):
            if stated and len(read) != stated:
                gaps.append(
                    f"{name}: read {len(read)} name(s), slide says {stated}"
                )
        fights.append(
            TrackerFightPicks(
                fighter_a=fight.fighter_a,
                fighter_b=fight.fighter_b,
                cappers_for_a=fight.cappers_for_a,
                cappers_for_b=fight.cappers_for_b,
            )
        )
    return TrackerRoundup(event_name="", fights=fights), gaps


@dataclass
class SlideReport:
    """What one deck yielded."""

    roundups: list[TrackerRoundup] = field(default_factory=list)
    frames: int = 0
    read: int = 0
    cached: int = 0
    failed: int = 0
    error: str = ""
    # "Kennedy Nzechukwu: read 41 name(s), slide says 43" — the slide's own
    # printed tally disagreeing with what came back.
    gaps: list[str] = field(default_factory=list)


# -- getting frames --------------------------------------------------------


def ffmpeg_path() -> str | None:
    """The ffmpeg to use: the pip-installed one, then whatever is on PATH.

    `imageio-ffmpeg` ships a static binary, which is the whole reason this
    works on a machine where nobody has installed ffmpeg by hand.
    """
    override = os.environ.get("MMA_FFMPEG", "").strip()
    if override:
        return override
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # not installed, or no binary for this platform
        return shutil.which("ffmpeg")


def download_video(
    url: str,
    dest_dir: Path,
    video_id: str,
    height: int = DEFAULT_HEIGHT,
    proxy: str = "",
    timeout: float = 900.0,
) -> Path | None:
    """Fetch the roundup video itself, video-only and no larger than needed."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(dest_dir.glob(f"{video_id}.*"))
    if existing:
        log.info("  reusing downloaded video %s", existing[0].name)
        return existing[0]

    template = str(dest_dir / f"{video_id}.%(ext)s")
    command = [
        # This interpreter, not whatever "python" happens to mean on PATH:
        # the run is inside a venv and yt-dlp is installed there, not
        # necessarily in the system Python.
        sys.executable,
        "-m",
        "yt_dlp",
        "--quiet",
        "--no-warnings",
        "--no-playlist",
        # Video only: the audio is what the transcript path already read.
        "-f",
        f"bv*[height<={height}]/b[height<={height}]/bv*/b",
        "-o",
        template,
        url,
    ]
    if proxy:
        command[-1:-1] = ["--proxy", proxy]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("  could not run yt-dlp: %s: %s", type(exc).__name__, exc)
        return None
    if result.returncode != 0:
        log.warning(
            "  yt-dlp could not download the roundup video: %s",
            (result.stderr or result.stdout or "").strip()[:300],
        )
        return None

    downloaded = sorted(dest_dir.glob(f"{video_id}.*"))
    return downloaded[0] if downloaded else None


def extract_slides(
    video: Path,
    out_dir: Path,
    scene_threshold: float = DEFAULT_SCENE_THRESHOLD,
    max_frames: int = DEFAULT_MAX_FRAMES,
    timeout: float = 900.0,
) -> list[Path]:
    """One frame per slide change, newest extraction replacing any older one."""
    binary = ffmpeg_path()
    if not binary:
        log.warning(
            "  no ffmpeg available — pip install imageio-ffmpeg (it ships one), "
            "or set MMA_FFMPEG to a binary"
        )
        return []

    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    command = [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video),
        "-vf",
        # A frame wherever the picture changes materially, then drop the
        # near-identical neighbours a transition leaves behind. eq(n,0) is
        # there because a scene score compares against the frame before it,
        # so the opening frame never scores — and on a deck that opens
        # straight onto a fight slide, that is a whole fight missed.
        rf"select='eq(n\,0)+gt(scene,{scene_threshold})',"
        "mpdecimate=hi=64*24:lo=64*12:frac=0.33",
        "-vsync",
        "vfr",
        "-frames:v",
        str(max_frames),
        "-q:v",
        "3",
        str(out_dir / "slide_%04d.jpg"),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("  ffmpeg failed: %s: %s", type(exc).__name__, exc)
        return []
    if result.returncode != 0:
        log.warning("  ffmpeg failed: %s", (result.stderr or "").strip()[:300])
        return []
    return sorted(out_dir.glob("slide_*.jpg"))


def read_directory(directory: Path) -> list[Path]:
    """Slide images captured by hand, in filename order."""
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    )


# -- reading them ----------------------------------------------------------


def image_block(path: Path) -> dict[str, Any]:
    """One image as an API content block."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": MEDIA_TYPES.get(path.suffix.lower(), "image/jpeg"),
            "data": base64.standard_b64encode(path.read_bytes()).decode("ascii"),
        },
    }


def frame_key(path: Path) -> str:
    """Cache key for a frame: the hash of its own bytes.

    Keyed on content rather than name so re-extracting a deck reuses every
    frame that came out the same, and a hand-captured screenshot dropped in
    twice is only ever paid for once.
    """
    return hashlib.sha1(path.read_bytes()).hexdigest()[:16]


class SlideReader:
    """Turns slide images into the roundup shape, one vision call per slide."""

    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        model: str = DEFAULT_SLIDE_MODEL,
        effort: str = "medium",
        max_tokens: int = 8000,
        cache_dir: str | Path = "cache/slides",
        use_cache: bool = True,
    ) -> None:
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.cache_dir = Path(cache_dir)
        self.use_cache = use_cache

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def _read_cache(self, key: str) -> SlideBoard | None:
        if not self.use_cache:
            return None
        path = self._cache_path(key)
        if not path.is_file():
            return None
        try:
            return SlideBoard.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            log.warning("Ignoring unreadable slide cache: %s", path)
            return None

    def _write_cache(self, key: str, parsed: SlideBoard) -> None:
        if not self.use_cache:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_path(key).write_text(
            parsed.model_dump_json(indent=2), encoding="utf-8"
        )

    def _read_one(self, path: Path) -> SlideBoard:
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=self.max_tokens,
            output_config={"effort": self.effort},
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": [image_block(path), {"type": "text", "text": USER_TEXT}],
                }
            ],
            output_format=SlideBoard,
        )
        if response.stop_reason == "refusal":
            raise RuntimeError(f"Model refused the request: {response.stop_details}")
        if response.parsed_output is None:
            raise RuntimeError(
                f"Model returned no structured output (stop_reason={response.stop_reason})"
            )
        return response.parsed_output

    def read(self, paths: Iterable[Path]) -> SlideReport:
        """Read every slide, keeping whatever succeeds.

        A failure part-way through — a spent balance, a rate limit — stops the
        reading but keeps the slides already read: half a deck of attributions
        beats none, and the cache means the next run picks up where this left
        off rather than paying for those slides again.
        """
        report = SlideReport()
        for path in paths:
            report.frames += 1
            key = frame_key(path)
            cached = self._read_cache(key)
            if cached is not None:
                report.cached += 1
                roundup, gaps = board_to_roundup(cached)
                report.roundups.append(roundup)
                report.gaps.extend(gaps)
                continue
            try:
                parsed = self._read_one(path)
            except anthropic.APIError as exc:
                report.failed += 1
                report.error = f"{type(exc).__name__}: {exc}"
                log.warning("  stopped reading slides: %s", report.error)
                break
            except Exception as exc:
                report.failed += 1
                report.error = f"{type(exc).__name__}: {exc}"
                log.warning("  slide %s could not be read: %s", path.name, report.error)
                continue
            self._write_cache(key, parsed)
            report.read += 1
            roundup, gaps = board_to_roundup(parsed)
            report.roundups.append(roundup)
            report.gaps.extend(gaps)
        for gap in report.gaps:
            log.info("  slide count check — %s", gap)
        return report
