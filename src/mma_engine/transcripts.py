"""Step 1 — pull YouTube transcripts.

YouTube rate-limits aggressive scrapers, so requests are spaced by a randomized
delay and every successful fetch is cached on disk. Re-running the pipeline
after a partial failure only re-requests the videos that are still missing.

Age-restricted uploads (and the occasional PoToken-required video) can't be
read through the anonymous youtube-transcript-api path — YouTube gates their
captions behind a signed-in, 18+ account. When cookies are configured (see
CookieConfig), those specific failures retry once through yt-dlp using your
own cookies, which authenticates the request as you and lets YouTube hand back
the captions it already would in your browser. Everything else stays on the
faster anonymous path.
"""

from __future__ import annotations

import json
import logging
import random
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api import (
    AgeRestricted,
    CouldNotRetrieveTranscript,
    NoTranscriptFound,
    PoTokenRequired,
    TranscriptsDisabled,
    VideoUnavailable,
    YouTubeTranscriptApiException,
)
from youtube_transcript_api.proxies import ProxyConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CookieConfig:
    """Where yt-dlp should read your YouTube cookies from for the authenticated
    fallback. Exactly one source is used; from_browser wins when both are set.

    - from_browser: a local browser profile name yt-dlp can read directly,
      e.g. "chrome", "firefox", "edge", "brave" (--cookies-from-browser).
    - file: path to a Netscape-format cookies.txt exported from a logged-in
      session (--cookies).
    """

    from_browser: str = ""
    file: str = ""

    @property
    def is_configured(self) -> bool:
        return bool(self.from_browser.strip() or self.file.strip())

    def ytdlp_args(self) -> list[str]:
        """The yt-dlp cookie flag for this source, or [] when unconfigured."""
        if self.from_browser.strip():
            return ["--cookies-from-browser", self.from_browser.strip()]
        if self.file.strip():
            return ["--cookies", self.file.strip()]
        return []


def build_cookie_config(settings: dict) -> CookieConfig | None:
    """A CookieConfig from settings.transcript_cookies, or None when the
    fallback is disabled or no source is set. Mirrors proxy.build_proxy_config:
    the single place settings turn into the object the fetcher consumes."""
    cookies = settings.get("transcript_cookies") or {}
    if not cookies.get("enabled"):
        return None
    config = CookieConfig(
        from_browser=str(cookies.get("from_browser") or "").strip(),
        file=str(cookies.get("file") or "").strip(),
    )
    return config if config.is_configured else None


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
        cookie_config: CookieConfig | None = None,
        ytdlp_timeout: float = 120.0,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.languages = languages or ["en"]
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.use_cache = use_cache
        self.cookie_config = cookie_config
        self.ytdlp_timeout = ytdlp_timeout
        self._proxy_config = proxy_config
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
        except (AgeRestricted, PoTokenRequired) as exc:
            # The anonymous endpoint can't read these, but an authenticated
            # request can — retry once through yt-dlp with your cookies. Must
            # be caught before the generic CouldNotRetrieveTranscript below,
            # since both are subclasses of it.
            return self._fetch_authenticated(video_id, reason=type(exc).__name__)
        except CouldNotRetrieveTranscript as exc:
            # Covers IpBlocked / RequestBlocked and any other retrieval failure
            # the cookie fallback wouldn't help with.
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

    # -- authenticated (cookie) fallback -----------------------------------

    def _fetch_authenticated(self, video_id: str, reason: str) -> TranscriptResult:
        """Retry one age-restricted / PoToken-gated video through yt-dlp with
        cookies. Returns the original failure unchanged when no cookies are
        configured, so a run without them behaves exactly as before."""
        if self.cookie_config is None:
            return TranscriptResult(
                video_id,
                ok=False,
                error=(
                    f"{reason}: needs a signed-in account. Enable "
                    "settings.transcript_cookies to read it via yt-dlp."
                ),
            )

        text = self._fetch_via_ytdlp(video_id)
        if not text:
            return TranscriptResult(
                video_id,
                ok=False,
                error=f"{reason}: yt-dlp cookie fallback did not return captions",
            )

        self._write_cache(video_id, text)
        log.info("[%s] transcript fetched via yt-dlp cookies (%d chars)", video_id, len(text))
        return TranscriptResult(video_id, text=text)

    def _ytdlp_command(self, video_id: str, out_template: str) -> list[str]:
        """The yt-dlp invocation for a captions-only, authenticated fetch. Grabs
        both manual and auto-generated subs in the configured languages as
        json3 (the same timed-text shape the primary API returns), never the
        video itself.

        Invoked as `python -m yt_dlp` through the interpreter running the
        pipeline, not the bare `yt-dlp` console script: weekly.ps1 runs the
        pipeline via `.venv\\Scripts\\python.exe -m mma_engine` WITHOUT
        activating the venv, so `.venv\\Scripts` (where `yt-dlp.exe` lives)
        isn't on PATH and a bare `yt-dlp` call fails with FileNotFoundError.
        Going through sys.executable uses the same venv that pip installed
        yt-dlp into, regardless of PATH."""
        cmd = [
            sys.executable, "-m", "yt_dlp",
            *self.cookie_config.ytdlp_args(),
            "--skip-download",
            # --skip-download stops the *download*, not format selection: yt-dlp
            # still resolves its default `bestvideo*+bestaudio/best` selector and
            # aborts with "Requested format is not available" before writing any
            # subtitles when nothing matches. Age-restricted uploads routinely
            # hand back an empty or unselectable format list even with valid
            # cookies, which killed the whole fetch over media we never wanted.
            # This downgrades that to a warning and lets subtitle writing run.
            "--ignore-no-formats-error",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs", ",".join(self.languages),
            "--sub-format", "json3",
            "--no-warnings",
            "-o", out_template,
        ]
        # Exit through the same non-cloud IP as the primary path when a proxy
        # is configured — an age-restricted video fetched from a blocked cloud
        # IP would just fail a different way.
        proxy_url = _proxy_url(self._proxy_config)
        if proxy_url:
            cmd += ["--proxy", proxy_url]
        cmd.append(f"https://www.youtube.com/watch?v={video_id}")
        return cmd

    def _fetch_via_ytdlp(self, video_id: str) -> str | None:
        """Run yt-dlp into a temp dir and parse whatever json3 caption file it
        writes. Returns None (never raises) on any failure — a subprocess
        problem for one video must not abort the run, same posture as the
        network guards in fetch()."""
        with tempfile.TemporaryDirectory() as tmp:
            out_template = str(Path(tmp) / "%(id)s.%(ext)s")
            try:
                subprocess.run(
                    self._ytdlp_command(video_id, out_template),
                    check=True,
                    capture_output=True,
                    timeout=self.ytdlp_timeout,
                )
            except FileNotFoundError:
                # sys.executable missing is essentially impossible, but keep a
                # clear message rather than a bare traceback if it ever happens.
                log.error("Python interpreter not found; cannot run yt-dlp for %s", video_id)
                return None
            except subprocess.TimeoutExpired:
                log.warning("[%s] yt-dlp timed out after %.0fs", video_id, self.ytdlp_timeout)
                return None
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or b"").decode("utf-8", "replace").strip()
                # `python -m yt_dlp` when the package isn't installed exits with
                # "No module named yt_dlp" — call that out specifically so the
                # fix (pip install / rerun weekly.bat) is obvious.
                if "No module named" in stderr and "yt_dlp" in stderr:
                    log.error("yt-dlp is not installed in this environment; run pip install -r requirements.txt")
                elif "DPAPI" in stderr or "Failed to decrypt" in stderr:
                    # Chrome 127+ (and Edge) wrap cookies in App-Bound Encryption
                    # only Chrome itself can undo, so --cookies-from-browser can't
                    # read them on Windows (yt-dlp issue #10927). Point straight at
                    # the working alternative rather than logging a raw stderr line.
                    log.error(
                        "[%s] yt-dlp can't decrypt this browser's cookies (Chrome/Edge "
                        "App-Bound Encryption). Export a cookies.txt and set "
                        "settings.transcript_cookies.file instead of from_browser — see "
                        "README \"Age-restricted videos\".", video_id,
                    )
                else:
                    log.warning("[%s] yt-dlp failed: %s", video_id, stderr.splitlines()[-1] if stderr else exc)
                return None

            # yt-dlp names subs "<id>.<lang>.json3"; a language may resolve to a
            # regional variant (en-US), so match by prefix rather than guessing
            # the exact suffix. Prefer the earliest configured language.
            files = sorted(Path(tmp).glob("*.json3"))
            if not files:
                return None
            chosen = _preferred_sub_file(files, self.languages)
            try:
                return _parse_json3(chosen.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("[%s] could not parse yt-dlp captions: %s", video_id, exc)
                return None


def _proxy_url(proxy_config: ProxyConfig | None) -> str | None:
    """The http proxy URL a ProxyConfig would use, for passing to yt-dlp's
    --proxy. youtube-transcript-api's ProxyConfig exposes this as to_requests_dict()."""
    if proxy_config is None:
        return None
    try:
        proxies = proxy_config.to_requests_dict()
    except Exception:  # pragma: no cover - defensive; shape varies by version
        return None
    return proxies.get("https") or proxies.get("http")


def _preferred_sub_file(files: list[Path], languages: list[str]) -> Path:
    """Pick the caption file whose language prefix appears earliest in the
    requested list; fall back to the first file yt-dlp wrote."""
    for lang in languages:
        base = lang.split("-")[0].lower()
        for path in files:
            # "<id>.<lang>.json3" -> the middle dotted segment is the language.
            parts = path.name.split(".")
            if len(parts) >= 3 and parts[-2].lower().startswith(base):
                return path
    return files[0]


def _parse_json3(raw: str) -> str:
    """Flatten YouTube's json3 timed-text into one space-joined string — the
    same shape the primary snippet-join produces. json3 is a list of `events`,
    each optionally carrying `segs` whose `utf8` fields are the caption text."""
    data = json.loads(raw)
    pieces: list[str] = []
    for event in data.get("events", []):
        for seg in event.get("segs", []) or []:
            piece = (seg.get("utf8") or "").strip()
            if piece:
                pieces.append(piece)
    return " ".join(pieces)
