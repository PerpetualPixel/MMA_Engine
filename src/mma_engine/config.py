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
    "discovery": {
        # Pull each capper's recent uploads from their channel RSS feed instead
        # of pasting video URLs by hand every week.
        "enabled": False,
        "lookback_days": 10,
        "max_videos_per_channel": 2,
        # Optional substring the video title must contain, e.g. "UFC 300".
        # Useful when a channel posts non-betting content in the same window.
        "title_contains": "",
    },
    "pasted_picks": {
        # Cards pasted by hand into `pasted/`, for cappers who now put their
        # full card behind a paywall and leave a teaser on YouTube (see
        # mma_engine.pasted_picks). Nothing is fetched — a person puts the
        # text in the file. Enabled here means "read the folder if it exists".
        "enabled": True,
        "dir": "pasted",
        # A hand-managed folder goes stale silently, so a file untouched for
        # this long is skipped and reported rather than re-counted every week.
        # 0 disables the guard.
        "max_age_days": 14,
    },
    "tracker_picks": {
        # Ingest the predictions tracker's pre-event roundup — one video that
        # reports which of 150+ channels picked which fighter (see
        # mma_engine.tracker_picks). The URLs live under `tracker.picks_videos`
        # in config.json; this block only says how they are read. Enabled here
        # means "use them if any are listed".
        "enabled": True,
        # Confidence every roundup pick enters at. A tally states no
        # conviction, so it sits mid-scale rather than pretending to one.
        "confidence": 5,
        # Roundup transcripts are name-dense and the response is one line per
        # name, so they are chunked smaller than a normal picks video.
        "max_chunk_chars": 12000,
    },
    "live_odds": {
        # Current moneyline prices from The Odds API, stamped onto each bout so
        # the dashboard can price a parlay instead of just ranking it. Costs
        # one API request per run (free tier allows 500/month). Enabled here
        # only means "use it if a key is present" — with no ODDS_API_KEY the
        # fetch is skipped and the run is exactly as it was before.
        "enabled": True,
        # Bookmaker regions to average over: us, us2, uk, eu, au (comma-joined).
        "regions": "us",
    },
    "proxy": {
        # Route YouTube requests through a proxy — required for the pipeline to
        # run unattended on GitHub Actions' shared runners, since YouTube blocks
        # their cloud IP ranges outright. Credentials come from environment
        # variables (see mma_engine.proxy), never from this file.
        "enabled": False,
        "provider": "webshare",  # "webshare" or "generic"
    },
    "transcript_cookies": {
        # A signed-in-account fallback for the videos youtube-transcript-api
        # can't read anonymously — chiefly age-restricted uploads (YouTube
        # gates their captions behind an 18+ account) and the occasional
        # PoToken-required video. When enabled, those specific failures retry
        # once through yt-dlp using your own cookies, which authenticates the
        # request as you and lets YouTube serve the captions it already would
        # in your browser. Every other video still uses the faster anonymous
        # path; disabled by default, so a run with no cookies configured
        # behaves exactly as before.
        "enabled": False,
        # Read cookies straight from a local browser profile, e.g. "chrome",
        # "firefox", "edge", "brave" (yt-dlp --cookies-from-browser). Empty to
        # use a cookies.txt file instead.
        "from_browser": "",
        # Path to a Netscape-format cookies.txt exported from a logged-in
        # session (yt-dlp --cookies). Empty to use from_browser instead. If
        # both are set, from_browser wins.
        "file": "",
    },
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
    # Optional UC... ID. Set it to skip handle resolution, or leave it blank and
    # let discovery scrape it from the channel page once and cache it.
    channel_id: str = ""
    # Whether channel discovery pulls this capper's uploads.
    discover: bool = True
    trust: dict[str, float] = field(default_factory=dict)
    # Other spellings this channel is known by — chiefly the garbles tracker
    # transcripts produce ("Bet Sam" for BetSlam with Sam). Roster merging and
    # roundup attribution both match against these, so a mangled name reaches
    # the real capper instead of minting a duplicate.
    aliases: tuple[str, ...] = ()

    def trust_for(self, role: str) -> float:
        """Trust score for how this capper framed the bet.

        `role` is "underdog" / "favorite" as reported by the extractor, or
        "method" for method-of-victory picks (the aggregation passes it for
        that market regardless of dog/chalk framing); anything else
        (including "unknown") falls back to the overall score.
        """
        overall = float(self.trust.get("overall", 5.0))
        if role in ("underdog", "favorite", "method"):
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
    # The `tracker` block: the results channel the trust scores come from, plus
    # any pre-event roundup videos to ingest picks from.
    tracker: dict[str, Any] = field(default_factory=dict)

    @property
    def tracker_picks_videos(self) -> list[str]:
        """Roundup video URLs to ingest every run, from `tracker.picks_videos`."""
        return [url for url in (self.tracker.get("picks_videos") or []) if url]

    def capper(self, capper_id: str) -> Capper:
        try:
            return self.cappers[capper_id]
        except KeyError:
            raise ConfigError(f"Unknown capper_id {capper_id!r}") from None

    @property
    def discoverable_cappers(self) -> list[Capper]:
        """Cappers whose channels should be swept for recent uploads."""
        return [
            capper
            for capper in self.cappers.values()
            if capper.discover and (capper.channel_url or capper.channel_id)
        ]


def load_config(path: str | Path = "config.json") -> Config:
    """Read, validate, and normalize `config.json`."""
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"Config file not found: {config_path}")

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{config_path} is not valid JSON: {exc}") from exc

    raw_settings = raw.get("settings") or {}
    settings = {**DEFAULT_SETTINGS, **raw_settings}
    # `discovery` is nested, so merge it key-by-key rather than letting a partial
    # block from config.json drop the defaults for the keys it omits.
    settings["discovery"] = {
        **DEFAULT_SETTINGS["discovery"],
        **(raw_settings.get("discovery") or {}),
    }
    settings["proxy"] = {
        **DEFAULT_SETTINGS["proxy"],
        **(raw_settings.get("proxy") or {}),
    }
    settings["transcript_cookies"] = {
        **DEFAULT_SETTINGS["transcript_cookies"],
        **(raw_settings.get("transcript_cookies") or {}),
    }
    settings["live_odds"] = {
        **DEFAULT_SETTINGS["live_odds"],
        **(raw_settings.get("live_odds") or {}),
    }
    settings["pasted_picks"] = {
        **DEFAULT_SETTINGS["pasted_picks"],
        **(raw_settings.get("pasted_picks") or {}),
    }
    settings["tracker_picks"] = {
        **DEFAULT_SETTINGS["tracker_picks"],
        **(raw_settings.get("tracker_picks") or {}),
    }
    # Environment wins over the file so CI can override without a commit.
    if os.environ.get("MMA_MODEL"):
        settings["model"] = os.environ["MMA_MODEL"]
    if os.environ.get("MMA_EFFORT"):
        settings["effort"] = os.environ["MMA_EFFORT"]
    if os.environ.get("MMA_PROXY_ENABLED"):
        # Lets GitHub Actions turn proxying on for its cloud runners without
        # forcing it on for local runs, which don't need it and may not have
        # proxy credentials configured.
        settings["proxy"]["enabled"] = os.environ["MMA_PROXY_ENABLED"].strip().lower() in (
            "1",
            "true",
            "yes",
        )
    # Same rationale as the proxy env overrides: cookies are a secret, so CI
    # writes the exported cookies.txt to a path and points the pipeline at it
    # via env rather than committing it. Setting the file path alone is enough
    # to turn the fallback on.
    if os.environ.get("MMA_TRANSCRIPT_COOKIES_FILE"):
        settings["transcript_cookies"]["file"] = os.environ["MMA_TRANSCRIPT_COOKIES_FILE"].strip()
        settings["transcript_cookies"]["enabled"] = True
    if os.environ.get("MMA_TRANSCRIPT_COOKIES_ENABLED"):
        settings["transcript_cookies"]["enabled"] = os.environ[
            "MMA_TRANSCRIPT_COOKIES_ENABLED"
        ].strip().lower() in ("1", "true", "yes")
    # Lets a run opt out of the odds request without editing config.json —
    # useful when the free-tier allowance is nearly spent and the consensus
    # matters more than the prices.
    if os.environ.get("MMA_LIVE_ODDS_ENABLED"):
        settings["live_odds"]["enabled"] = os.environ[
            "MMA_LIVE_ODDS_ENABLED"
        ].strip().lower() in ("1", "true", "yes")

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
            channel_id=entry.get("channel_id", ""),
            discover=bool(entry.get("discover", True)),
            trust=dict(entry.get("trust") or {}),
            aliases=tuple(entry.get("aliases") or ()),
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
        tracker=dict(raw.get("tracker") or {}),
    )
