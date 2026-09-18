"""Read a picks video off its screen — every unique frame, then the picks on it.

    python -m mma_engine --picks-from-video https://youtu.be/VIDEO_ID

Paste any YouTube URL and this turns the video into picks the dashboard can
weigh, without needing a transcript: the video is downloaded (yt-dlp, video
only, 720p), one frame is cut wherever the picture changes and at least
every few seconds in between (ffmpeg), near-identical frames are dropped so
each screenshot is unique, and each surviving frame goes to one vision call
that reports whatever picks it shows.

Two kinds of video land here, and the reader tells them apart per frame:

- A capper's own video, with picks printed on screen — a pick card, a best
  bets slide, a bet slip, a lower third saying "Silva ML -180, 2u". Those
  come back as `Pick` objects (the same schema the transcript extractor
  produces), attributed to the channel that posted the video, and carry
  whatever conviction the graphic states: a stake, a "best bet" label, a
  price.
- A tracker-style roundup board — two fighters, and channel names printed on
  whichever side they picked. Those come back as boards, and are counted
  exactly as `--picks-from-tracker` would count them: one neutral vote per
  channel, tagged `via tracker`.

So the URL is the whole input. Whose video it is comes from the video's own
metadata (yt-dlp `--dump-single-json`), matched against `config.json` by
channel id, then name or alias, and minted at neutral trust when unknown —
or set by hand with `--video-capper`.

Frames are cached by the hash of their bytes (`cache/screens/`), so a run
that dies part-way resumes for free, and a finished reading is written to
`screens/<video_id>.json`, where every later run reuses it without a
download or an API call, and a garbled name can be fixed by hand. Failure
anywhere here is non-fatal: no yt-dlp, no ffmpeg, a video that won't
download, a frame the model can't parse — each is logged and the run
continues on whatever the other sources gave it.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import anthropic
from pydantic import BaseModel, Field

from .config import extract_video_id
from .extract import Pick, _clamp_confidence, _dedupe
from .roundup_slides import (
    DEFAULT_HEIGHT,
    DEFAULT_SLIDE_MODEL,
    SlideFight,
    ffmpeg_path,
    frame_key,
    image_block,
)
from .tracker_picks import TrackerFightPicks, TrackerRoundup

log = logging.getLogger(__name__)

# A capper's video is mostly a talking head with cuts between camera angles,
# so scene detection alone would either miss a graphic that fades in (no
# cut) or fire on every camera change. Frames are therefore taken at every
# real cut AND at least every `sample_seconds`, and the near-duplicates that
# leaves behind are dropped by perceptual hash below.
DEFAULT_SCENE_THRESHOLD = 0.3
DEFAULT_SAMPLE_SECONDS = 8.0
DEFAULT_MAX_FRAMES = 150
# Hamming distance on a 64-bit difference hash under which two frames are
# the same picture. 10 folds the same slide across a subtle zoom or a
# presenter moving in the corner; two different pick cards score 25+.
DEFAULT_MAX_DISTANCE = 10
# What a pick enters at when the graphic shows a side and nothing about how
# sure the capper is — the same neutral middle a roundup line gets.
DEFAULT_CONFIDENCE = 5
DEFAULT_READINGS_DIR = "screens"

SYSTEM_PROMPT = """\
You read one frame captured from a YouTube MMA betting video and report the \
betting picks printed on it.

Two kinds of frame carry picks; everything else carries none.

1. A capper's own picks, shown as on-screen graphics: a pick card or best-bets \
slide listing fights and the side taken, a bet slip, a lower-third banner \
("Silva ML -180, 2u"), a card graphic with the picked fighter highlighted or \
ticked, or a "lock of the night" panel. For each pick the frame shows, report \
the matchup, the market, the side, and whatever the graphic states about \
conviction: a stake in units, a label such as "best bet", "lock", "lean" or \
"small play", and the price quoted. Rate confidence from those signals only — \
a lean or small play is 3-5, a plain pick with no signal is 5, a confident or \
sized play is 6-8, a stated best bet or lock or 3+ unit play is 9-10. Set the \
role from the price when one is printed (a plus price is the underdog, a \
minus price the favorite), otherwise "unknown". Give one pick per line the \
graphic shows: a moneyline and a method bet on the same fighter are two picks.

2. A predictions-tracker roundup board: one fighter at the left edge, the \
other at the right, and YouTube channel names printed in columns on whichever \
side each channel picked, often with a "YouTube Predictions 80/81" tally. \
Report those as a board — the two fighters, every channel name on each side, \
copied exactly as printed, and the printed tally (0 if none) — never as picks.

Rules:
- Report only what is printed on this frame. Never infer a pick from a \
fighter's photo, an odds table, a tale of the tape, a fight poster, or the \
video's title; those name the fight, not the side taken.
- An odds board showing both fighters' prices with neither one marked as the \
pick is not a pick.
- Copy fighter names as printed and keep them consistent; write the matchup \
as the two full names regardless of which side is picked.
- If the frame is a talking head, a sponsor card, an intro, a highlight clip \
or anything else with no picks printed on it, return empty lists.\
"""

USER_TEMPLATE = """\
Video: {title}
Channel: {channel}

Report every betting pick printed on this frame.\
"""


class ScreenRead(BaseModel):
    """What one frame shows: the capper's own picks, or a roundup board."""

    event_name: str = Field(
        description="The event named on the frame, e.g. 'UFC 300'. Empty if none is printed."
    )
    picks: list[Pick] = Field(
        description=(
            "The capper's own picks printed on this frame. Empty unless the "
            "frame states which side the capper is taking."
        )
    )
    boards: list[SlideFight] = Field(
        description=(
            "Tracker-style boards on this frame: two fighters with prediction "
            "channel names printed on each side. Empty for every other frame."
        )
    )


@dataclass(frozen=True)
class VideoInfo:
    """What YouTube says about a video: enough to attribute its picks."""

    video_id: str
    url: str
    title: str = ""
    channel: str = ""
    channel_id: str = ""


@dataclass
class ScreenReading:
    """A finished reading of one video, in the shape `screens/` stores."""

    video_id: str
    source_url: str
    title: str = ""
    channel: str = ""
    channel_id: str = ""
    event_name: str = ""
    picks: list[Pick] = field(default_factory=list)
    boards: list[TrackerFightPicks] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "source_url": self.source_url,
            "title": self.title,
            "channel": self.channel,
            "channel_id": self.channel_id,
            "event_name": self.event_name,
            "picks": [pick.model_dump() for pick in self.picks],
            "boards": [board.model_dump() for board in self.boards],
        }

    @classmethod
    def from_payload(cls, raw: dict[str, Any]) -> "ScreenReading":
        return cls(
            video_id=str(raw.get("video_id") or ""),
            source_url=str(raw.get("source_url") or ""),
            title=str(raw.get("title") or ""),
            channel=str(raw.get("channel") or ""),
            channel_id=str(raw.get("channel_id") or ""),
            event_name=str(raw.get("event_name") or ""),
            picks=[Pick.model_validate(item) for item in raw.get("picks") or []],
            boards=[
                TrackerFightPicks.model_validate(item) for item in raw.get("boards") or []
            ],
        )

    @property
    def empty(self) -> bool:
        return not self.picks and not self.boards


@dataclass
class ScreenReport:
    """What reading one video's frames yielded."""

    picks: list[Pick] = field(default_factory=list)
    boards: list[TrackerFightPicks] = field(default_factory=list)
    event_name: str = ""
    frames: int = 0
    read: int = 0
    cached: int = 0
    failed: int = 0
    error: str = ""


# -- what the video is -----------------------------------------------------


def fetch_video_info(
    url: str, proxy: str = "", timeout: float = 120.0, extra_args: list[str] | None = None
) -> VideoInfo | None:
    """Title and channel for a URL, from yt-dlp's metadata call. None on failure.

    `extra_args` (the configured cookies) go straight to yt-dlp.
    """
    try:
        video_id = extract_video_id(url)
    except Exception:
        return None
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--quiet",
        "--no-warnings",
        "--no-playlist",
        *(extra_args or []),
        "--skip-download",
        "--dump-single-json",
        url,
    ]
    if proxy:
        command[-1:-1] = ["--proxy", proxy]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("  could not run yt-dlp for metadata: %s: %s", type(exc).__name__, exc)
        return None
    if result.returncode != 0:
        log.warning(
            "  yt-dlp could not read the video's metadata: %s",
            (result.stderr or result.stdout or "").strip()[:300],
        )
        return None
    try:
        raw = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None
    return VideoInfo(
        video_id=str(raw.get("id") or video_id),
        url=url,
        title=str(raw.get("title") or ""),
        channel=str(raw.get("channel") or raw.get("uploader") or ""),
        channel_id=str(raw.get("channel_id") or raw.get("uploader_id") or ""),
    )


# -- getting the frames ----------------------------------------------------


def extract_frames(
    video: Path,
    out_dir: Path,
    scene_threshold: float = DEFAULT_SCENE_THRESHOLD,
    sample_seconds: float = DEFAULT_SAMPLE_SECONDS,
    max_frames: int = DEFAULT_MAX_FRAMES,
    timeout: float = 900.0,
) -> list[Path]:
    """A frame at every cut, and at least one every `sample_seconds`.

    A pick card that fades in over a talking head never registers as a scene
    change, so a pure scene cut misses it; the periodic sample is the
    guarantee that no graphic stays up for long without being captured.
    """
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

    interval = max(0.5, float(sample_seconds))
    select = (
        rf"select='eq(n\,0)+gt(scene\,{scene_threshold})"
        rf"+gte(t-prev_selected_t\,{interval})'"
    )
    command = [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video),
        "-vf",
        select,
        "-vsync",
        "vfr",
        "-frames:v",
        str(max_frames),
        "-q:v",
        "3",
        str(out_dir / "frame_%04d.jpg"),
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
    return sorted(out_dir.glob("frame_*.jpg"))


def difference_hash(path: Path, timeout: float = 60.0) -> int | None:
    """A 64-bit perceptual hash of one image, via ffmpeg (no Pillow needed).

    The image is shrunk to 9x8 greyscale and each bit records whether a
    pixel is brighter than its right-hand neighbour — the classic dHash.
    Small shifts, compression noise and a presenter blinking in the corner
    leave it nearly unchanged; a different slide flips half the bits.
    """
    binary = ffmpeg_path()
    if not binary:
        return None
    command = [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-vf",
        "scale=9:8:flags=area,format=gray",
        "-frames:v",
        "1",
        "-f",
        "rawvideo",
        "-",
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    pixels = result.stdout
    if result.returncode != 0 or len(pixels) < 72:
        return None
    value = 0
    for row in range(8):
        for col in range(8):
            left = pixels[row * 9 + col]
            right = pixels[row * 9 + col + 1]
            value = (value << 1) | (1 if left > right else 0)
    return value


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def unique_frames(
    paths: Iterable[Path], max_distance: int = DEFAULT_MAX_DISTANCE
) -> list[Path]:
    """Drop every frame that looks like one already kept.

    Compared against every kept frame, not just the previous one: a video
    that cuts back to the same pick card after a talking-head aside shows
    the card twice, and it only needs reading once. A frame that can't be
    hashed is kept — paying for one extra read beats losing a pick.
    """
    kept: list[Path] = []
    hashes: list[int] = []
    for path in paths:
        digest = difference_hash(path)
        if digest is None:
            kept.append(path)
            continue
        if any(hamming(digest, seen) <= max_distance for seen in hashes):
            continue
        kept.append(path)
        hashes.append(digest)
    return kept


# -- reading them ----------------------------------------------------------


def _board_to_fight(board: SlideFight) -> TrackerFightPicks:
    return TrackerFightPicks(
        fighter_a=board.fighter_a,
        fighter_b=board.fighter_b,
        cappers_for_a=board.cappers_for_a,
        cappers_for_b=board.cappers_for_b,
    )


class ScreenReader:
    """Turns frames into picks, one vision call per unique frame."""

    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        model: str = DEFAULT_SLIDE_MODEL,
        effort: str = "medium",
        max_tokens: int = 8000,
        cache_dir: str | Path = "cache/screens",
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

    def _read_cache(self, key: str) -> ScreenRead | None:
        if not self.use_cache:
            return None
        path = self._cache_path(key)
        if not path.is_file():
            return None
        try:
            return ScreenRead.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            log.warning("Ignoring unreadable frame cache: %s", path)
            return None

    def _write_cache(self, key: str, parsed: ScreenRead) -> None:
        if not self.use_cache:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_path(key).write_text(parsed.model_dump_json(indent=2), encoding="utf-8")

    def _read_one(self, path: Path, title: str, channel: str) -> ScreenRead:
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
                    "content": [
                        image_block(path),
                        {
                            "type": "text",
                            "text": USER_TEMPLATE.format(
                                title=title or "(unknown)", channel=channel or "(unknown)"
                            ),
                        },
                    ],
                }
            ],
            output_format=ScreenRead,
        )
        if response.stop_reason == "refusal":
            raise RuntimeError(f"Model refused the request: {response.stop_details}")
        if response.parsed_output is None:
            raise RuntimeError(
                f"Model returned no structured output (stop_reason={response.stop_reason})"
            )
        return response.parsed_output

    def read(self, paths: Iterable[Path], title: str = "", channel: str = "") -> ScreenReport:
        """Read every frame, keeping whatever succeeds.

        An API failure part-way (a spent balance, a rate limit) stops the
        reading but keeps what was read: the cache means the next run picks
        up from there rather than paying for those frames again. Picks the
        same graphic shows on several frames are folded into one, keeping
        the highest confidence, exactly as a transcript's chunks are.
        """
        report = ScreenReport()
        raw_picks: list[Pick] = []
        for path in paths:
            report.frames += 1
            key = frame_key(path)
            parsed = self._read_cache(key)
            if parsed is not None:
                report.cached += 1
            else:
                try:
                    parsed = self._read_one(path, title, channel)
                except anthropic.APIError as exc:
                    report.failed += 1
                    report.error = f"{type(exc).__name__}: {exc}"
                    log.warning("  stopped reading frames: %s", report.error)
                    break
                except Exception as exc:
                    report.failed += 1
                    report.error = f"{type(exc).__name__}: {exc}"
                    log.warning("  frame %s could not be read: %s", path.name, report.error)
                    continue
                self._write_cache(key, parsed)
                report.read += 1
            report.event_name = report.event_name or parsed.event_name
            for pick in parsed.picks:
                pick.confidence = _clamp_confidence(pick.confidence)
                raw_picks.append(pick)
            report.boards.extend(_board_to_fight(board) for board in parsed.boards)
        report.picks = _dedupe(raw_picks)
        return report


# -- keeping what was read -------------------------------------------------


def load_reading(directory: Path, video_id: str) -> ScreenReading | None:
    """A video already read, from `screens/<video_id>.json`, or None."""
    path = directory / f"{video_id}.json"
    if not path.is_file():
        return None
    try:
        reading = ScreenReading.from_payload(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:
        log.warning("Ignoring unreadable screen reading %s: %s", path, exc)
        return None
    if reading.empty:
        return None
    reading.video_id = reading.video_id or video_id
    return reading


def save_reading(directory: Path, reading: ScreenReading) -> Path | None:
    """Write a finished reading so the next run gets it for free.

    Never overwrites: a file already there was written by an earlier run or
    corrected by a person, and both beat re-deriving it.
    """
    if reading.empty:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{reading.video_id}.json"
    if path.exists():
        return None
    path.write_text(
        json.dumps(reading.to_payload(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    log.info(
        "Wrote %s — %d pick(s), %d board(s); read once and free from here on",
        path, len(reading.picks), len(reading.boards),
    )
    return path


def boards_as_roundup(boards: Iterable[TrackerFightPicks], event_name: str = "") -> TrackerRoundup:
    """Boards read off a screen, in the shape the tracker path merges."""
    return TrackerRoundup(event_name=event_name, fights=list(boards))
