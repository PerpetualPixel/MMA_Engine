"""Step 1 — pull YouTube transcripts.

YouTube rate-limits aggressive scrapers, so requests are spaced by a randomized
delay and every successful fetch is cached on disk. Re-running the pipeline
after a partial failure only re-requests the videos that are still missing.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api import (
    CouldNotRetrieveTranscript,
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
    YouTubeTranscriptApiException,
)
from youtube_transcript_api.proxies import ProxyConfig

log = logging.getLogger(__name__)


@dataclass
class TranscriptResult:
    video_id: str
    text: str = ""
    ok: bool = True
    error: str = ""
    from_cache: bool = False

    @property
    def char_count(self) -> int:
        return len(self.text)


class TranscriptFetcher:
    """Fetches transcripts with on-disk caching and polite, jittered delays."""

    def __init__(
        self,
        cache_dir: str | Path = "cache/transcripts",
        languages: list[str] | None = None,
        min_delay: float = 4.0,
        max_delay: float = 12.0,
        use_cache: bool = True,
        proxy_config: ProxyConfig | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.languages = languages or ["en"]
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.use_cache = use_cache
        self._api = YouTubeTranscriptApi(proxy_config=proxy_config)
        self._requests_made = 0

    # -- caching -----------------------------------------------------------

    def _cache_path(self, video_id: str) -> Path:
        return self.cache_dir / f"{video_id}.json"

    def _read_cache(self, video_id: str) -> str | None:
        if not self.use_cache:
            return None
        path = self._cache_path(video_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))["text"]
        except (json.JSONDecodeError, KeyError, OSError):
            log.warning("Ignoring unreadable transcript cache: %s", path)
            return None

    def _write_cache(self, video_id: str, text: str) -> None:
        if not self.use_cache:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {"video_id": video_id, "languages": self.languages, "text": text}
        self._cache_path(video_id).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    # -- fetching ----------------------------------------------------------

    def _sleep_between_requests(self) -> None:
        """Randomized delay before every request after the first."""
        if self._requests_made == 0:
            return
        delay = random.uniform(self.min_delay, self.max_delay)
        log.debug("Sleeping %.1fs before the next YouTube request", delay)
        time.sleep(delay)

    def fetch(self, video_id: str) -> TranscriptResult:
        cached = self._read_cache(video_id)
        if cached is not None:
            log.info("[%s] transcript from cache (%d chars)", video_id, len(cached))
            return TranscriptResult(video_id, text=cached, from_cache=True)

        self._sleep_between_requests()
        self._requests_made += 1

        try:
            fetched = self._api.fetch(video_id, languages=self.languages)
        except (TranscriptsDisabled, NoTranscriptFound) as exc:
            return TranscriptResult(
                video_id, ok=False, error=f"No usable transcript: {type(exc).__name__}"
            )
        except VideoUnavailable as exc:
            return TranscriptResult(
                video_id, ok=False, error=f"Video unavailable: {type(exc).__name__}"
            )
        except CouldNotRetrieveTranscript as exc:
            # Covers IpBlocked / RequestBlocked / AgeRestricted / PoTokenRequired.
            return TranscriptResult(
                video_id, ok=False, error=f"{type(exc).__name__}: {exc}"
            )
        except YouTubeTranscriptApiException as exc:
            return TranscriptResult(
                video_id, ok=False, error=f"{type(exc).__name__}: {exc}"
            )
        except requests.exceptions.RequestException as exc:
            # Proxy/DNS/TLS/timeout failures surface here, below the library's
            # own exception hierarchy. One unreachable video must not abort a
            # 30-video run.
            return TranscriptResult(
                video_id, ok=False, error=f"Network error: {type(exc).__name__}: {exc}"
            )
        except Exception as exc:  # last-resort guard, same reasoning
            log.exception("[%s] unexpected transcript error", video_id)
            return TranscriptResult(
                video_id, ok=False, error=f"Unexpected error: {type(exc).__name__}: {exc}"
            )

        text = " ".join(
            snippet.text.strip() for snippet in fetched if snippet.text.strip()
        )
        if not text:
            return TranscriptResult(video_id, ok=False, error="Transcript was empty")

        self._write_cache(video_id, text)
        log.info("[%s] transcript fetched (%d chars)", video_id, len(text))
        return TranscriptResult(video_id, text=text)
