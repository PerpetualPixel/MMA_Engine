"""Step 0 (optional) — find each capper's recent videos from their channel.

Instead of pasting eight URLs into `config.json` every week, list the channels
once and let the pipeline pull the latest uploads from each one.

Two ways to do that, chosen automatically:

**YouTube Data API v3 (primary — used when `YOUTUBE_API_KEY` is set).**
The official API. A free key from the Google Cloud console covers this
project's usage thousands of times over (listing one channel's uploads costs
1 quota unit of a 10,000/day allowance). `@handle` URLs resolve via
`channels.list?forHandle=...`; a known `UC...` channel ID skips that call
entirely, because every channel's uploads playlist is its channel ID with the
`UC` prefix swapped for `UU` — a stable, documented YouTube convention.
Uploads then come from `playlistItems.list` with IDs, titles, and publish
dates.

**Channel RSS feed (fallback — no key needed, but now unreliable).**

    https://www.youtube.com/feeds/videos.xml?channel_id=UC...

This endpoint served the same data with no key, but as of August 2026 it
returns 404 for every channel tested — including the largest channels on the
platform, from residential IPs and browsers alike — so YouTube appears to
have discontinued it. The code path is kept in case it returns, and because
its flakiness was already handled: 404s are retried with backoff, and the
`playlist_id=UU...` form of the same endpoint is tried when the `channel_id`
form fails.

Discovery is a convenience, not a requirement: anything listed explicitly in
`config.json`'s `videos` array is always used, and explicit entries win over
discovered ones for the same video.
"""

from __future__ import annotations

import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
FEED_URL_BY_PLAYLIST = "https://www.youtube.com/feeds/videos.xml?playlist_id={playlist_id}"

# YouTube Data API v3 — the official, keyed replacement for the RSS feed.
API_CHANNELS_URL = (
    "https://www.googleapis.com/youtube/v3/channels"
    "?part=contentDetails&forHandle={handle}&key={key}"
)
API_PLAYLIST_ITEMS_URL = (
    "https://www.googleapis.com/youtube/v3/playlistItems"
    "?part=snippet,contentDetails&playlistId={playlist_id}&maxResults=15&key={key}"
)

# An @handle as it appears in a channel URL: youtube.com/@SomeName
_HANDLE_RE = re.compile(r"@([A-Za-z0-9._-]+)")
# The API key travels as a query parameter; keep it out of logs and reports.
_KEY_PARAM_RE = re.compile(r"([?&]key=)[^&\s]+")


def uploads_playlist_id(channel_id: str) -> str:
    """A channel's uploads-playlist ID: its channel ID with UC swapped for UU.

    Every channel has one — this is a stable, documented YouTube convention,
    not something specific to any single channel.
    """
    return "UU" + channel_id[2:] if channel_id.startswith("UC") else channel_id

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}

# A channel ID is always "UC" plus 22 URL-safe characters.
_CHANNEL_ID_RE = re.compile(r"UC[A-Za-z0-9_-]{22}")
# Both of these appear in channel page HTML; either is enough to resolve a handle.
_PAGE_CHANNEL_ID_RES = [
    re.compile(r'"channelId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"'),
    re.compile(r'"externalId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"'),
    re.compile(r'<meta\s+itemprop="identifier"\s+content="(UC[A-Za-z0-9_-]{22})"'),
    re.compile(r'channel/(UC[A-Za-z0-9_-]{22})'),
]

# A plain browser UA; the bare python-requests default is more likely to be served
# a consent interstitial instead of the channel page.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _as_patterns(value: str | list[str] | None) -> list[str]:
    """Normalize a title filter into a lowercase list of substrings.

    Accepts a single string or a list; a video matches if *any* entry appears in
    its title. A list is the practical form for an event, since cappers spell it
    inconsistently ("UFC 320", "UFC320", "Ankalaev vs Pereira").
    """
    if not value:
        return []
    items = [value] if isinstance(value, str) else list(value)
    return [item.strip().lower() for item in items if item and item.strip()]


@dataclass(frozen=True)
class DiscoveredVideo:
    video_id: str
    capper_id: str
    url: str
    title: str
    published: datetime


def parse_channel_id(text: str) -> str | None:
    """Pull a channel ID straight out of a `/channel/UC...` URL, if present."""
    match = _CHANNEL_ID_RE.search(text or "")
    return match.group(0) if match else None


def parse_handle(channel_url: str) -> str | None:
    """Pull the handle out of a `youtube.com/@SomeName` URL, without the @."""
    match = _HANDLE_RE.search(channel_url or "")
    return match.group(1) if match else None


def redact_key(url: str) -> str:
    """Mask the API key query parameter so it never lands in logs or reports."""
    return _KEY_PARAM_RE.sub(r"\1***", url or "")


def parse_playlist_items(payload: dict, capper_id: str) -> list[DiscoveredVideo]:
    """Parse a Data API playlistItems.list response into videos, newest first."""
    videos: list[DiscoveredVideo] = []
    for item in payload.get("items") or []:
        details = item.get("contentDetails") or {}
        snippet = item.get("snippet") or {}
        video_id = (details.get("videoId") or "").strip()
        if not video_id:
            continue

        raw_date = details.get("videoPublishedAt") or snippet.get("publishedAt") or ""
        try:
            published = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        except ValueError:
            continue  # no usable date means we can't apply the lookback window
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)

        videos.append(
            DiscoveredVideo(
                video_id=video_id,
                capper_id=capper_id,
                url=f"https://www.youtube.com/watch?v={video_id}",
                title=(snippet.get("title") or "").strip(),
                published=published,
            )
        )

    videos.sort(key=lambda v: v.published, reverse=True)
    return videos


def parse_channel_id_from_page(html: str) -> str | None:
    """Extract a channel ID from channel-page HTML (for `@handle` URLs)."""
    for pattern in _PAGE_CHANNEL_ID_RES:
        match = pattern.search(html or "")
        if match:
            return match.group(1)
    return None


def parse_feed(xml_text: str, capper_id: str) -> list[DiscoveredVideo]:
    """Parse a YouTube channel RSS feed into videos, newest first."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"Channel feed was not valid XML: {exc}") from exc

    videos: list[DiscoveredVideo] = []
    for entry in root.findall("atom:entry", _NS):
        video_id_el = entry.find("yt:videoId", _NS)
        if video_id_el is None or not (video_id_el.text or "").strip():
            continue
        video_id = video_id_el.text.strip()

        title_el = entry.find("atom:title", _NS)
        title = (title_el.text or "").strip() if title_el is not None else ""

        published_el = entry.find("atom:published", _NS)
        try:
            published = datetime.fromisoformat((published_el.text or "").strip())
        except (AttributeError, ValueError):
            continue  # no usable date means we can't apply the lookback window
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)

        videos.append(
            DiscoveredVideo(
                video_id=video_id,
                capper_id=capper_id,
                url=f"https://www.youtube.com/watch?v={video_id}",
                title=title,
                published=published,
            )
        )

    videos.sort(key=lambda v: v.published, reverse=True)
    return videos


class ChannelDiscovery:
    """Resolves channel URLs and pulls recent uploads from their RSS feeds."""

    def __init__(
        self,
        lookback_days: int = 10,
        max_per_channel: int = 2,
        title_contains: str | list[str] = "",
        cache_path: str | Path = "cache/channels.json",
        timeout: float = 20.0,
        use_cache: bool = True,
        session: requests.Session | None = None,
        max_retries: int = 3,
        retry_backoff: float = 2.0,
        proxies: dict[str, str] | None = None,
        api_key: str = "",
    ) -> None:
        self.api_key = api_key.strip()
        self.lookback_days = lookback_days
        self.max_per_channel = max_per_channel
        self.title_contains = _as_patterns(title_contains)
        self.cache_path = Path(cache_path)
        self.timeout = timeout
        self.use_cache = use_cache
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", _USER_AGENT)
        if proxies:
            self.session.proxies.update(proxies)
        self._channel_ids: dict[str, str] = self._load_cache()

    # -- channel ID cache --------------------------------------------------

    def _load_cache(self) -> dict[str, str]:
        if not self.use_cache or not self.cache_path.is_file():
            return {}
        try:
            return dict(json.loads(self.cache_path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            log.warning("Ignoring unreadable channel cache: %s", self.cache_path)
            return {}

    def _save_cache(self) -> None:
        if not self.use_cache:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(self._channel_ids, indent=2, sort_keys=True), encoding="utf-8"
        )

    # -- retrying HTTP GET ---------------------------------------------------

    def _get_with_retry(self, url: str) -> requests.Response:
        """GET a URL, retrying transient failures with backoff.

        YouTube's RSS surface flakes intermittently (see module docstring) — a
        404 or 5xx here is not reliable evidence the URL is wrong. A connection
        failure (proxy, DNS, timeout) is retried the same way. The final
        attempt's exception propagates so the caller still sees a real error.
        """
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.get(url, timeout=self.timeout)
                response.raise_for_status()
                return response
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                if attempt == self.max_retries:
                    break
                delay = self.retry_backoff * (2 ** (attempt - 1))
                log.info(
                    "  request failed (%s), retry %d/%d in %.0fs: %s",
                    type(exc).__name__,
                    attempt,
                    self.max_retries - 1,
                    delay,
                    redact_key(url),
                )
                time.sleep(delay)
        assert last_exc is not None  # loop always sets it before falling through
        raise last_exc

    # -- resolution --------------------------------------------------------

    def resolve_channel_id(self, channel_url: str, channel_id: str = "") -> str:
        """Return the UC... ID for a channel URL, scraping the page if needed."""
        if channel_id:
            return channel_id

        direct = parse_channel_id(channel_url)
        if direct:
            return direct

        cached = self._channel_ids.get(channel_url)
        if cached:
            return cached

        log.info("Resolving channel ID for %s", channel_url)
        response = self._get_with_retry(channel_url)
        resolved = parse_channel_id_from_page(response.text)
        if not resolved:
            raise ValueError(
                f"Could not find a channel ID on {channel_url}. Open the channel, "
                f"copy its /channel/UC... URL, and set \"channel_id\" in config.json."
            )
        self._channel_ids[channel_url] = resolved
        self._save_cache()
        return resolved

    # -- YouTube Data API path ---------------------------------------------

    def resolve_uploads_playlist(self, capper) -> str:
        """The UU... uploads-playlist ID for a capper, via the Data API if needed.

        A known channel ID converts locally (UC → UU) with no API call; only an
        unresolved @handle costs a channels.list request, and the result is
        cached alongside RSS-era resolutions.
        """
        channel_id = capper.channel_id or parse_channel_id(capper.channel_url)
        if not channel_id:
            channel_id = self._channel_ids.get(capper.channel_url, "")

        if not channel_id:
            handle = parse_handle(capper.channel_url)
            if not handle:
                raise ValueError(
                    f"Could not parse a channel ID or @handle from "
                    f"{capper.channel_url!r}. Use a /channel/UC... or @handle URL."
                )
            log.info("Resolving @%s via the YouTube Data API", handle)
            response = self._get_with_retry(
                API_CHANNELS_URL.format(handle=handle, key=self.api_key)
            )
            items = response.json().get("items") or []
            if not items:
                raise ValueError(
                    f"The YouTube Data API found no channel for @{handle}. "
                    f"Check the channel_url in config.json."
                )
            channel_id = items[0]["id"]
            self._channel_ids[capper.channel_url] = channel_id
            self._save_cache()

        return uploads_playlist_id(channel_id)

    def fetch_playlist_videos_api(
        self, playlist_id: str, capper_id: str
    ) -> list[DiscoveredVideo]:
        response = self._get_with_retry(
            API_PLAYLIST_ITEMS_URL.format(playlist_id=playlist_id, key=self.api_key)
        )
        return parse_playlist_items(response.json(), capper_id)

    # -- RSS fallback path -------------------------------------------------

    def fetch_channel_videos(self, channel_id: str, capper_id: str) -> list[DiscoveredVideo]:
        try:
            response = self._get_with_retry(FEED_URL.format(channel_id=channel_id))
            return parse_feed(response.text, capper_id)
        except requests.exceptions.RequestException as channel_form_error:
            log.info(
                "  channel_id feed form failed after retries, trying the "
                "uploads-playlist form instead"
            )
            try:
                response = self._get_with_retry(
                    FEED_URL_BY_PLAYLIST.format(
                        playlist_id=uploads_playlist_id(channel_id)
                    )
                )
            except requests.exceptions.RequestException:
                # Neither request shape worked — surface the original error,
                # since channel_id is the primary, documented form.
                raise channel_form_error from None
            return parse_feed(response.text, capper_id)

    def _select(self, videos: list[DiscoveredVideo]) -> list[DiscoveredVideo]:
        """Apply the lookback window, the title filter, and the per-channel cap."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
        selected = [v for v in videos if v.published >= cutoff]
        if self.title_contains:
            selected = [
                v
                for v in selected
                if any(pattern in v.title.lower() for pattern in self.title_contains)
            ]
        return selected[: self.max_per_channel]

    def discover(self, cappers: list) -> tuple[list[DiscoveredVideo], list[dict]]:
        """Find recent videos for every capper. Returns (videos, per-channel report).

        A channel that fails (unresolvable handle, network error, bad feed) is
        reported and skipped — it never aborts discovery for the others.
        """
        found: list[DiscoveredVideo] = []
        report: list[dict] = []

        for capper in cappers:
            entry = {"capper_id": capper.id, "capper": capper.name, "status": "ok"}
            if not capper.channel_url and not capper.channel_id:
                entry.update(status="skipped", error="No channel_url configured")
                report.append(entry)
                continue

            try:
                if self.api_key:
                    channel_id = self.resolve_uploads_playlist(capper)
                    videos = self.fetch_playlist_videos_api(channel_id, capper.id)
                else:
                    channel_id = self.resolve_channel_id(
                        capper.channel_url, capper.channel_id
                    )
                    videos = self.fetch_channel_videos(channel_id, capper.id)
            except (requests.exceptions.RequestException, ValueError) as exc:
                error_text = redact_key(f"{type(exc).__name__}: {exc}")
                entry.update(status="failed", error=error_text)
                log.warning("  discovery failed for %s: %s", capper.name, error_text)
                report.append(entry)
                continue
            except Exception as exc:  # never let one channel abort the sweep
                log.exception("Unexpected discovery error for %s", capper.name)
                entry.update(
                    status="failed", error=redact_key(f"{type(exc).__name__}: {exc}")
                )
                report.append(entry)
                continue

            selected = self._select(videos)
            found.extend(selected)
            entry.update(
                channel_id=channel_id,
                feed_videos=len(videos),
                selected=[
                    {
                        "video_id": v.video_id,
                        "title": v.title,
                        "published": v.published.isoformat(),
                    }
                    for v in selected
                ],
            )
            log.info(
                "  %s: %d recent video(s) of %d in feed",
                capper.name,
                len(selected),
                len(videos),
            )
            report.append(entry)

        return found, report
