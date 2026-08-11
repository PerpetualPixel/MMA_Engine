"""Loading and validation of `config.json`.

The config is the only thing that changes week to week: update `event`, drop the
new video URLs into `videos`, and the rest of the pipeline reads from here.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Any of these forms is accepted in config.json's "videos[].url":
#   https://www.youtube.com/watch?v=VIDEOID
#   https://youtu.be/VIDEOID
#   https://www.youtube.com/live/VIDEOID
#   https://www.youtube.com/shorts/VIDEOID
#   https://www.youtube.com/embed/VIDEOID
#   VIDEOID
_VIDEO_ID = r"[A-Za-z0-9_-]{11}"
_URL_PATTERNS = [
    re.compile(rf"(?:v=|/v/)({_VIDEO_ID})"),
    re.compile(rf"youtu\.be/({_VIDEO_ID})"),
    re.compile(rf"youtube\.com/(?:live|shorts|embed)/({_VIDEO_ID})"),
    re.compile(rf"^({_VIDEO_ID})$"),
]

DEFAULT_SETTINGS: dict[str, Any] = {
    "model": "claude-opus-5",
    "effort": "high",
    "max_tokens": 20000,
    "transcript_languages": ["en"],
    "min_delay_seconds": 4.0,
    "max_delay_seconds": 12.0,
    "max_transcript_chars": 60000,
    "min_confidence": 1,
    "use_cache": True,
}


class ConfigError(ValueError):
    """Raised when `config.json` is missing required data or is malformed."""


def extract_video_id(url_or_id: str) -> str:
    """Pull the 11-character YouTube video ID out of a URL (or pass one through)."""
    candidate = (url_or_id or "").strip()
    for pattern in _URL_PATTERNS:
        match = pattern.search(candidate)
        if match:
            return match.group(1)
    raise ConfigError(f"Could not parse a YouTube video ID from {url_or_id!r}")


@dataclass(frozen=True)
class Capper:
    id: str
    name: str
    channel_url: str = ""
    trust: dict[str, float] = field(default_factory=dict)

    def trust_for(self, role: str) -> float:
        """Trust score for how this capper framed the bet.

        `role` is "underdog" / "favorite" as reported by the extractor; anything
        else (including "unknown") falls back to the overall score.
        """
        overall = float(self.trust.get("overall", 5.0))
        if role in ("underdog", "favorite"):
            return float(self.trust.get(role, overall))
        return overall


@dataclass(frozen=True)
class VideoRef:
    video_id: str
    capper_id: str
    url: str
    title: str = ""


@dataclass(frozen=True)
class Config:
    event: dict[str, Any]
    settings: dict[str, Any]
    cappers: dict[str, Capper]
    videos: list[VideoRef]
    path: Path

    def capper(self, capper_id: str) -> Capper:
        try:
            return self.cappers[capper_id]
        except KeyError:
            raise ConfigError(f"Unknown capper_id {capper_id!r}") from None


def load_config(path: str | Path = "config.json") -> Config:
    """Read, validate, and normalize `config.json`."""
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"Config file not found: {config_path}")

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{config_path} is not valid JSON: {exc}") from exc

    settings = {**DEFAULT_SETTINGS, **(raw.get("settings") or {})}
    # Environment wins over the file so CI can override without a commit.
    if os.environ.get("MMA_MODEL"):
        settings["model"] = os.environ["MMA_MODEL"]
    if os.environ.get("MMA_EFFORT"):
        settings["effort"] = os.environ["MMA_EFFORT"]

    if settings["min_delay_seconds"] > settings["max_delay_seconds"]:
        raise ConfigError("settings.min_delay_seconds exceeds max_delay_seconds")

    cappers: dict[str, Capper] = {}
    for entry in raw.get("cappers") or []:
        if not entry.get("id") or not entry.get("name"):
            raise ConfigError(f"Capper entries need an 'id' and a 'name': {entry!r}")
        if entry["id"] in cappers:
            raise ConfigError(f"Duplicate capper id {entry['id']!r}")
        cappers[entry["id"]] = Capper(
            id=entry["id"],
            name=entry["name"],
            channel_url=entry.get("channel_url", ""),
            trust=dict(entry.get("trust") or {}),
        )
    if not cappers:
        raise ConfigError("config.json defines no cappers")

    videos: list[VideoRef] = []
    seen: set[str] = set()
    for entry in raw.get("videos") or []:
        source = entry.get("url") or entry.get("video_id") or ""
        video_id = extract_video_id(source)
        capper_id = entry.get("capper_id")
        if capper_id not in cappers:
            raise ConfigError(
                f"Video {source!r} references unknown capper_id {capper_id!r}"
            )
        if video_id in seen:
            continue  # same video listed twice — one vote, not two
        seen.add(video_id)
        videos.append(
            VideoRef(
                video_id=video_id,
                capper_id=capper_id,
                url=entry.get("url") or f"https://youtu.be/{video_id}",
                title=entry.get("title", ""),
            )
        )

    return Config(
        event=dict(raw.get("event") or {}),
        settings=settings,
        cappers=cappers,
        videos=videos,
        path=config_path,
    )
