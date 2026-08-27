"""Tests for channel discovery — feed parsing, filtering, and merge behavior.

No network: RSS and channel-page HTML are supplied as fixtures, and the HTTP
session is stubbed. Run with:  PYTHONPATH=src python -m pytest -q
"""

from __future__ import annotations

import json

from datetime import datetime, timedelta, timezone

import pytest
import requests

from mma_engine.config import Capper
from mma_engine.discover import (
    parse_duration,
    parse_search_results,
    ChannelDiscovery,
    parse_channel_id,
    parse_channel_id_from_page,
    parse_feed,
    uploads_playlist_id,
)

CHANNEL_ID = "UCpSQhfFzpZ9COp_WiKSUEkQ"


def feed_xml(entries: list[tuple[str, str, str]]) -> str:
    """Build a YouTube-shaped channel feed from (video_id, title, published)."""
    items = "\n".join(
        f"""  <entry>
    <id>yt:video:{vid}</id>
    <yt:videoId>{vid}</yt:videoId>
    <yt:channelId>{CHANNEL_ID}</yt:channelId>
    <title>{title}</title>
    <published>{published}</published>
    <updated>{published}</updated>
  </entry>"""
        for vid, title, published in entries
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns="http://www.w3.org/2005/Atom">
  <yt:channelId>{CHANNEL_ID}</yt:channelId>
  <title>Artem MMA</title>
{items}
</feed>"""


def iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class StubResponse:
    def __init__(self, text: str, error: Exception | None = None):
        self.text = text
        self._error = error

    def raise_for_status(self):
        if self._error:
            raise self._error

    def json(self):
        import json

        return json.loads(self.text)


class StubSession:
    """Maps URL substrings to responses; records what was requested."""

    def __init__(self, routes: dict[str, StubResponse | Exception]):
        self.routes = routes
        self.headers: dict[str, str] = {}
        self.requested: list[str] = []

    def get(self, url: str, timeout: float = 0):
        self.requested.append(url)
        for fragment, result in self.routes.items():
            if fragment in url:
                if isinstance(result, Exception):
                    raise result
                return result
        raise requests.exceptions.ConnectionError(f"no stub route for {url}")


def make_discovery(routes, **kwargs) -> ChannelDiscovery:
    kwargs.setdefault("use_cache", False)
    # Single attempt by default so failure-path tests stay fast; tests that
    # specifically exercise retrying set max_retries themselves.
    kwargs.setdefault("max_retries", 1)
    kwargs.setdefault("retry_backoff", 0.0)
    return ChannelDiscovery(session=StubSession(routes), **kwargs)


# -- feed parsing ----------------------------------------------------------


def test_parse_feed_reads_id_title_and_date():
    xml = feed_xml([("dQw4w9WgXcQ", "UFC 317 Picks", "2026-08-09T18:00:00+00:00")])
    videos = parse_feed(xml, "artem_mma")

    assert len(videos) == 1
    video = videos[0]
    assert video.video_id == "dQw4w9WgXcQ"
    assert video.title == "UFC 317 Picks"
    assert video.capper_id == "artem_mma"
    assert video.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert video.published == datetime(2026, 8, 9, 18, tzinfo=timezone.utc)


def test_parse_feed_sorts_newest_first():
    xml = feed_xml(
        [
            ("aaaaaaaaaaa", "Older", iso(5)),
            ("bbbbbbbbbbb", "Newest", iso(1)),
            ("ccccccccccc", "Middle", iso(3)),
        ]
    )
    assert [v.title for v in parse_feed(xml, "c")] == ["Newest", "Middle", "Older"]


def test_parse_feed_skips_entries_missing_id_or_date():
    xml = f"""<?xml version="1.0"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns="http://www.w3.org/2005/Atom">
  <entry><title>No video id</title><published>{iso(1)}</published></entry>
  <entry><yt:videoId>ddddddddddd</yt:videoId><title>No date</title></entry>
  <entry><yt:videoId>eeeeeeeeeee</yt:videoId><title>Good</title>
    <published>{iso(1)}</published></entry>
</feed>"""
    videos = parse_feed(xml, "c")
    assert [v.video_id for v in videos] == ["eeeeeeeeeee"]


def test_parse_feed_rejects_malformed_xml():
    with pytest.raises(ValueError):
        parse_feed("<html><body>truncated response", "c")


def test_parse_feed_returns_nothing_for_well_formed_non_feed():
    # An HTML error page can still parse as XML; it just has no entries.
    assert parse_feed("<html><body>404 Not Found</body></html>", "c") == []


def test_parse_feed_handles_empty_feed():
    assert parse_feed(feed_xml([]), "c") == []


# -- channel ID resolution -------------------------------------------------


def test_parse_channel_id_from_channel_url():
    assert (
        parse_channel_id(f"https://www.youtube.com/channel/{CHANNEL_ID}") == CHANNEL_ID
    )
    assert parse_channel_id("https://www.youtube.com/@FunkyPicks") is None


@pytest.mark.parametrize(
    "html",
    [
        f'{{"channelId":"{CHANNEL_ID}","title":"x"}}',
        f'{{"externalId":"{CHANNEL_ID}"}}',
        f'<meta itemprop="identifier" content="{CHANNEL_ID}">',
        f'<link rel="canonical" href="https://www.youtube.com/channel/{CHANNEL_ID}">',
    ],
)
def test_parse_channel_id_from_page_shapes(html):
    assert parse_channel_id_from_page(html) == CHANNEL_ID


def test_parse_channel_id_from_page_returns_none_when_absent():
    assert parse_channel_id_from_page("<html><body>nothing</body></html>") is None


def test_resolve_channel_id_prefers_configured_id_over_network():
    discovery = make_discovery({})
    assert discovery.resolve_channel_id("https://youtube.com/@x", CHANNEL_ID) == CHANNEL_ID
    assert discovery.session.requested == []  # no page fetch needed


def test_resolve_channel_id_scrapes_handle_pages():
    discovery = make_discovery(
        {"@FunkyPicks": StubResponse(f'"channelId":"{CHANNEL_ID}"')}
    )
    resolved = discovery.resolve_channel_id("https://www.youtube.com/@FunkyPicks")
    assert resolved == CHANNEL_ID


def test_resolve_channel_id_raises_actionable_error():
    discovery = make_discovery({"@Ghost": StubResponse("<html>consent wall</html>")})
    with pytest.raises(ValueError, match="channel_id"):
        discovery.resolve_channel_id("https://www.youtube.com/@Ghost")


# -- selection rules -------------------------------------------------------


def capper(capper_id="artem_mma", **kwargs) -> Capper:
    return Capper(
        id=capper_id,
        name="Artem MMA",
        channel_url=f"https://www.youtube.com/channel/{CHANNEL_ID}",
        **kwargs,
    )


def test_lookback_window_excludes_old_uploads():
    xml = feed_xml(
        [("aaaaaaaaaaa", "Recent", iso(2)), ("bbbbbbbbbbb", "Stale", iso(40))]
    )
    discovery = make_discovery({"videos.xml": StubResponse(xml)}, lookback_days=10)
    videos, report = discovery.discover([capper()])

    assert [v.title for v in videos] == ["Recent"]
    assert report[0]["status"] == "ok"
    assert report[0]["feed_videos"] == 2


def test_max_per_channel_caps_results():
    xml = feed_xml([(f"vid{i:07d}xxx", f"Video {i}", iso(i)) for i in range(1, 6)])
    discovery = make_discovery({"videos.xml": StubResponse(xml)}, max_per_channel=2)
    videos, _ = discovery.discover([capper()])

    # Newest first, so the two most recent survive the cap.
    assert [v.title for v in videos] == ["Video 1", "Video 2"]


def test_title_filter_is_case_insensitive():
    xml = feed_xml(
        [
            ("aaaaaaaaaaa", "UFC 317 Full Card Picks", iso(1)),
            ("bbbbbbbbbbb", "Weekly mailbag", iso(1)),
        ]
    )
    discovery = make_discovery(
        {"videos.xml": StubResponse(xml)}, title_contains="ufc 317"
    )
    videos, _ = discovery.discover([capper()])
    assert [v.video_id for v in videos] == ["aaaaaaaaaaa"]


def test_title_filter_accepts_multiple_spellings():
    # Cappers write the same event inconsistently; any pattern matching is enough.
    xml = feed_xml(
        [
            ("aaaaaaaaaaa", "UFC 320 Full Card Breakdown", iso(1)),
            ("bbbbbbbbbbb", "UFC320 Best Bets", iso(2)),
            ("ccccccccccc", "UFC 319 Recap", iso(3)),
            ("ddddddddddd", "Random vlog", iso(4)),
        ]
    )
    discovery = make_discovery(
        {"videos.xml": StubResponse(xml)},
        title_contains=["ufc 320", "ufc320"],
        max_per_channel=5,
    )
    videos, _ = discovery.discover([capper()])
    assert [v.video_id for v in videos] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]


def test_empty_title_filter_keeps_everything_recent():
    xml = feed_xml([("aaaaaaaaaaa", "Anything", iso(1))])
    for empty in ("", [], None):
        discovery = make_discovery(
            {"videos.xml": StubResponse(xml)}, title_contains=empty
        )
        videos, _ = discovery.discover([capper()])
        assert len(videos) == 1, f"{empty!r} should not filter anything out"


# -- resilience ------------------------------------------------------------


def test_one_failing_channel_does_not_stop_the_others():
    good_xml = feed_xml([("aaaaaaaaaaa", "Picks", iso(1))])
    discovery = make_discovery(
        {
            f"channel_id={CHANNEL_ID}": StubResponse(good_xml),
            "@Broken": requests.exceptions.ConnectionError("boom"),
        }
    )
    broken = Capper(
        id="broken", name="Broken", channel_url="https://www.youtube.com/@Broken"
    )
    videos, report = discovery.discover([capper(), broken])

    assert [v.video_id for v in videos] == ["aaaaaaaaaaa"]
    statuses = {entry["capper_id"]: entry["status"] for entry in report}
    assert statuses == {"artem_mma": "ok", "broken": "failed"}
    assert "ConnectionError" in next(
        e["error"] for e in report if e["capper_id"] == "broken"
    )


def test_capper_without_channel_is_reported_not_crashed():
    discovery = make_discovery({})
    channel_less = Capper(id="x", name="No Channel")
    videos, report = discovery.discover([channel_less])
    assert videos == []
    assert report[0]["status"] == "skipped"


def test_resolve_videos_merges_explicit_and_discovered(tmp_path, monkeypatch):
    """Explicit config entries survive; discovery adds to them without duplicating."""
    import json

    from mma_engine import pipeline
    from mma_engine.config import load_config
    from mma_engine.discover import DiscoveredVideo

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "event": {"name": "UFC 320"},
                "settings": {"discovery": {"enabled": True}},
                "cappers": [
                    {
                        "id": "artem_mma",
                        "name": "Artem MMA",
                        "channel_url": f"https://www.youtube.com/channel/{CHANNEL_ID}",
                        "trust": {"overall": 7.5},
                    }
                ],
                "videos": [
                    {"capper_id": "artem_mma", "url": "https://youtu.be/pinnedaaaaa"}
                ],
            }
        )
    )
    config = load_config(config_path)

    fake = [
        # Same video the user pinned by hand — must not be added twice.
        DiscoveredVideo(
            "pinnedaaaaa", "artem_mma", "https://youtu.be/pinnedaaaaa", "dupe",
            datetime.now(timezone.utc),
        ),
        DiscoveredVideo(
            "freshbbbbbb", "artem_mma", "https://youtu.be/freshbbbbbb", "UFC 320 Picks",
            datetime.now(timezone.utc),
        ),
    ]
    monkeypatch.setattr(
        pipeline.ChannelDiscovery,
        "discover",
        lambda self, cappers: (fake, [{"capper_id": "artem_mma", "status": "ok"}]),
    )

    videos, report = pipeline.resolve_videos(config)

    assert [v.video_id for v in videos] == ["pinnedaaaaa", "freshbbbbbb"]
    assert report[0]["status"] == "ok"
    # The pinned entry keeps its original attribution, not the discovered title.
    assert videos[0].title == ""

    summary = pipeline._summarize_discovery(config, videos, report)
    assert "freshbbbbbb" in summary and "Artem MMA" in summary


def test_resolve_videos_skips_discovery_when_disabled(tmp_path):
    import json

    from mma_engine import pipeline
    from mma_engine.config import load_config

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "settings": {"discovery": {"enabled": False}},
                "cappers": [{"id": "a", "name": "A", "trust": {}}],
                "videos": [{"capper_id": "a", "url": "https://youtu.be/onlymineaaa"}],
            }
        )
    )
    videos, report = pipeline.resolve_videos(load_config(config_path))
    assert [v.video_id for v in videos] == ["onlymineaaa"]
    assert report == []


class FlakyThenOkSession:
    """Fails the first `fail_times` calls to a given URL, then serves it.

    Models exactly what happened against real YouTube: a 404 that clears on
    retry, not a persistently broken URL.
    """

    def __init__(self, fragment: str, fail_times: int, ok_response: StubResponse):
        self.fragment = fragment
        self.fail_times = fail_times
        self.ok_response = ok_response
        self.headers: dict[str, str] = {}
        self.calls = 0

    def get(self, url: str, timeout: float = 0):
        if self.fragment in url:
            self.calls += 1
            if self.calls <= self.fail_times:
                raise requests.exceptions.HTTPError("404 Client Error: Not Found")
            return self.ok_response
        raise requests.exceptions.ConnectionError(f"no stub route for {url}")


def test_transient_feed_404_recovers_on_retry(monkeypatch):
    monkeypatch.setattr("mma_engine.discover.time.sleep", lambda _seconds: None)
    xml = feed_xml([("aaaaaaaaaaa", "Picks", iso(1))])
    session = FlakyThenOkSession("videos.xml", fail_times=2, ok_response=StubResponse(xml))
    discovery = ChannelDiscovery(
        session=session, use_cache=False, max_retries=3, retry_backoff=1.0
    )

    videos, report = discovery.discover([capper()])

    assert session.calls == 3, "must have retried, not given up after the first 404"
    assert [v.video_id for v in videos] == ["aaaaaaaaaaa"]
    assert report[0]["status"] == "ok"


def test_retries_are_exhausted_before_reporting_failure(monkeypatch):
    """When BOTH the channel_id and uploads-playlist forms are broken, each
    exhausts its own retries independently — not more, not fewer."""
    monkeypatch.setattr("mma_engine.discover.time.sleep", lambda _seconds: None)
    session = FlakyThenOkSession(
        "videos.xml", fail_times=99, ok_response=StubResponse("unreachable")
    )
    discovery = ChannelDiscovery(
        session=session, use_cache=False, max_retries=3, retry_backoff=1.0
    )

    videos, report = discovery.discover([capper()])

    assert session.calls == 6, (
        "must not retry forever, and must exhaust both the channel_id form "
        "(3 attempts) and the uploads-playlist fallback (3 more) before giving up"
    )
    assert videos == []
    assert report[0]["status"] == "failed"


def test_retry_backoff_is_exponential(monkeypatch):
    """The backoff schedule restarts for the uploads-playlist fallback — it is
    a fresh set of retries, not a continuation of the first form's."""
    delays: list[float] = []
    monkeypatch.setattr("mma_engine.discover.time.sleep", delays.append)
    session = FlakyThenOkSession(
        "videos.xml", fail_times=99, ok_response=StubResponse("unreachable")
    )
    discovery = ChannelDiscovery(
        session=session, use_cache=False, max_retries=4, retry_backoff=2.0
    )

    discovery.discover([capper()])

    assert delays == [2.0, 4.0, 8.0, 2.0, 4.0, 8.0]


def test_uploads_playlist_id_swaps_uc_for_uu():
    assert uploads_playlist_id(CHANNEL_ID) == "UU" + CHANNEL_ID[2:]
    assert uploads_playlist_id(CHANNEL_ID).startswith("UU")


class ChannelFormFailsPlaylistFormWorksSession:
    """channel_id=... always errors; playlist_id=... always succeeds.

    Models what would happen if only the channel_id request shape is broken.
    """

    def __init__(self, ok_response: StubResponse):
        self.ok_response = ok_response
        self.headers: dict[str, str] = {}
        self.channel_id_calls = 0
        self.playlist_id_calls = 0

    def get(self, url: str, timeout: float = 0):
        if "channel_id=" in url:
            self.channel_id_calls += 1
            raise requests.exceptions.HTTPError("404 Client Error: Not Found")
        if "playlist_id=" in url:
            self.playlist_id_calls += 1
            return self.ok_response
        raise requests.exceptions.ConnectionError(f"no stub route for {url}")


def test_falls_back_to_uploads_playlist_when_channel_id_form_fails(monkeypatch):
    monkeypatch.setattr("mma_engine.discover.time.sleep", lambda _seconds: None)
    xml = feed_xml([("aaaaaaaaaaa", "Picks", iso(1))])
    session = ChannelFormFailsPlaylistFormWorksSession(StubResponse(xml))
    discovery = ChannelDiscovery(session=session, use_cache=False, max_retries=2)

    videos, report = discovery.discover([capper()])

    assert session.channel_id_calls == 2, "must exhaust retries before falling back"
    assert session.playlist_id_calls == 1, "the fallback request must use playlist_id="
    assert [v.video_id for v in videos] == ["aaaaaaaaaaa"]
    assert report[0]["status"] == "ok"


def test_reports_the_channel_id_error_when_both_forms_fail(monkeypatch):
    monkeypatch.setattr("mma_engine.discover.time.sleep", lambda _seconds: None)

    class BothFailSession:
        headers: dict[str, str] = {}

        def get(self, url, timeout=0):
            if "channel_id=" in url:
                raise requests.exceptions.HTTPError("404 channel_id form")
            if "playlist_id=" in url:
                raise requests.exceptions.HTTPError("404 playlist_id form")
            raise requests.exceptions.ConnectionError("unexpected url")

    discovery = ChannelDiscovery(session=BothFailSession(), use_cache=False, max_retries=1)
    videos, report = discovery.discover([capper()])

    assert videos == []
    assert report[0]["status"] == "failed"
    # The reported error is the primary (channel_id) form's, not the fallback's —
    # channel_id is the documented, expected-to-work shape.
    assert "channel_id form" in report[0]["error"]


def test_http_error_on_feed_is_captured():
    discovery = make_discovery(
        {
            "videos.xml": StubResponse(
                "", error=requests.exceptions.HTTPError("429 Too Many Requests")
            )
        }
    )
    videos, report = discovery.discover([capper()])
    assert videos == []
    assert report[0]["status"] == "failed"
    assert "429" in report[0]["error"]


# -- YouTube Data API path -------------------------------------------------


import json as _json

from mma_engine.discover import parse_handle, parse_playlist_items, redact_key


def api_playlist_json(entries: list[tuple[str, str, str]]) -> str:
    """Build a Data API playlistItems.list response from (id, title, published)."""
    return _json.dumps(
        {
            "items": [
                {
                    "snippet": {"title": title, "publishedAt": published},
                    "contentDetails": {"videoId": vid, "videoPublishedAt": published},
                }
                for vid, title, published in entries
            ]
        }
    )


def test_parse_handle():
    assert parse_handle("https://www.youtube.com/@FunkyPicks") == "FunkyPicks"
    assert parse_handle("https://youtube.com/@Some.Name_1/videos") == "Some.Name_1"
    assert parse_handle("https://www.youtube.com/channel/UCabc") is None
    assert parse_handle("") is None


def test_redact_key_masks_only_the_key():
    url = "https://www.googleapis.com/youtube/v3/channels?part=x&key=AIzaSecret123&other=1"
    redacted = redact_key(url)
    assert "AIzaSecret123" not in redacted
    assert "key=***" in redacted
    assert "part=x" in redacted


def test_parse_playlist_items_reads_id_title_and_date():
    payload = _json.loads(
        api_playlist_json([("dQw4w9WgXcQ", "UFC 320 Picks", "2026-08-09T18:00:00Z")])
    )
    videos = parse_playlist_items(payload, "artem_mma")

    assert len(videos) == 1
    video = videos[0]
    assert video.video_id == "dQw4w9WgXcQ"
    assert video.title == "UFC 320 Picks"
    assert video.capper_id == "artem_mma"
    assert video.published == datetime(2026, 8, 9, 18, tzinfo=timezone.utc)


def test_parse_playlist_items_skips_broken_entries_and_sorts():
    payload = {
        "items": [
            {"snippet": {"title": "No id"}, "contentDetails": {}},
            {
                "snippet": {"title": "Older", "publishedAt": iso(5)},
                "contentDetails": {"videoId": "aaaaaaaaaaa", "videoPublishedAt": iso(5)},
            },
            {
                "snippet": {"title": "Newest", "publishedAt": iso(1)},
                "contentDetails": {"videoId": "bbbbbbbbbbb", "videoPublishedAt": iso(1)},
            },
        ]
    }
    videos = parse_playlist_items(payload, "c")
    assert [v.title for v in videos] == ["Newest", "Older"]


def test_api_discovery_with_pinned_channel_id_skips_channels_lookup():
    # A known UC... ID converts to the UU... uploads playlist locally — the only
    # API call should be playlistItems.list.
    routes = {
        "playlistItems": StubResponse(
            api_playlist_json([("aaaaaaaaaaa", "UFC 320 Picks", iso(1))])
        )
    }
    discovery = make_discovery(routes, api_key="AIzaTest")
    videos, report = discovery.discover([capper(channel_id=CHANNEL_ID)])

    assert [v.video_id for v in videos] == ["aaaaaaaaaaa"]
    assert report[0]["status"] == "ok"
    session = discovery.session
    assert len(session.requested) == 1
    assert "playlistId=UU" + CHANNEL_ID[2:] in session.requested[0]


def test_api_discovery_resolves_handle_via_channels_endpoint():
    routes = {
        "youtube/v3/channels": StubResponse(
            _json.dumps({"items": [{"id": CHANNEL_ID}]})
        ),
        "playlistItems": StubResponse(
            api_playlist_json([("bbbbbbbbbbb", "UFC 320 Best Bets", iso(2))])
        ),
    }
    discovery = make_discovery(routes, api_key="AIzaTest")
    handle_capper = Capper(
        id="funky_picks",
        name="Funkybunch MMA",
        channel_url="https://www.youtube.com/@FunkyPicks",
    )
    videos, report = discovery.discover([handle_capper])

    assert [v.video_id for v in videos] == ["bbbbbbbbbbb"]
    assert report[0]["status"] == "ok"
    assert any("forHandle=FunkyPicks" in url for url in discovery.session.requested)


def test_api_discovery_unknown_handle_is_isolated_failure():
    routes = {
        "youtube/v3/channels": StubResponse(_json.dumps({"items": []})),
    }
    discovery = make_discovery(routes, api_key="AIzaTest")
    ghost = Capper(id="g", name="Ghost", channel_url="https://www.youtube.com/@Ghost")
    videos, report = discovery.discover([ghost])

    assert videos == []
    assert report[0]["status"] == "failed"
    assert "@Ghost" in report[0]["error"]


def test_api_discovery_error_report_never_leaks_the_key():
    routes = {
        "playlistItems": requests.exceptions.HTTPError(
            "403 Client Error for url: https://www.googleapis.com/youtube/v3/"
            "playlistItems?part=snippet&key=AIzaSecret123"
        )
    }
    discovery = make_discovery(routes, api_key="AIzaSecret123")
    videos, report = discovery.discover([capper(channel_id=CHANNEL_ID)])

    assert report[0]["status"] == "failed"
    assert "AIzaSecret123" not in report[0]["error"]


def test_api_discovery_applies_same_selection_filters():
    routes = {
        "playlistItems": StubResponse(
            api_playlist_json(
                [
                    ("aaaaaaaaaaa", "UFC 320 Full Card", iso(1)),
                    ("bbbbbbbbbbb", "Weekly mailbag", iso(2)),
                    ("ccccccccccc", "UFC 320 Best Bets", iso(40)),
                ]
            )
        )
    }
    discovery = make_discovery(
        routes, api_key="AIzaTest", lookback_days=10, title_contains=["ufc 320"]
    )
    videos, _ = discovery.discover([capper(channel_id=CHANNEL_ID)])

    # The mailbag fails the title filter; the 40-day-old video fails lookback.
    assert [v.video_id for v in videos] == ["aaaaaaaaaaa"]


def test_zero_matches_reports_recent_titles_for_diagnosis():
    # Feed reads fine but nothing passes the title filter — the report should
    # show what was there, so the user can fix title_contains.
    xml = feed_xml(
        [
            ("aaaaaaaaaaa", "UFC 999 Full Card", iso(1)),
            ("bbbbbbbbbbb", "Weekly mailbag", iso(2)),
        ]
    )
    discovery = make_discovery(
        {"videos.xml": StubResponse(xml)}, title_contains=["ufc 320"]
    )
    videos, report = discovery.discover([capper()])

    assert videos == []
    assert report[0]["status"] == "ok"
    titles = "\n".join(report[0]["recent_titles"])
    assert "UFC 999 Full Card" in titles
    assert "Weekly mailbag" in titles


def test_successful_match_does_not_report_recent_titles():
    xml = feed_xml([("aaaaaaaaaaa", "UFC 320 Picks", iso(1))])
    discovery = make_discovery(
        {"videos.xml": StubResponse(xml)}, title_contains=["ufc 320"]
    )
    _, report = discovery.discover([capper()])
    assert "recent_titles" not in report[0]


# -- open search across YouTube --------------------------------------------


def search_payload(items: list[tuple[str, str, str, str, float]]) -> str:
    """A search.list response: (video_id, title, channel_id, channel_title, age)."""
    import json as _json

    return _json.dumps(
        {
            "items": [
                {
                    "id": {"videoId": video_id},
                    "snippet": {
                        "title": title,
                        "channelId": channel_id,
                        "channelTitle": channel_title,
                        "publishedAt": iso(age),
                    },
                }
                for video_id, title, channel_id, channel_title, age in items
            ]
        }
    )


def durations_payload(pairs: list[tuple[str, str]]) -> str:
    import json as _json

    return _json.dumps(
        {"items": [{"id": vid, "contentDetails": {"duration": d}} for vid, d in pairs]}
    )


@pytest.mark.parametrize(
    "iso_text,seconds",
    [("PT12M30S", 750), ("PT1H2M3S", 3723), ("PT45S", 45), ("P1DT2H", 93600), ("", 0), ("nonsense", 0)],
)
def test_parse_duration(iso_text, seconds):
    assert parse_duration(iso_text) == seconds


def test_parse_search_results_carries_the_channel():
    videos = parse_search_results(
        json.loads(search_payload([("vid00000001", "UFC picks", "UC1", "Some MMA", 1)]))
    )
    assert len(videos) == 1
    assert videos[0].channel_id == "UC1"
    assert videos[0].channel_title == "Some MMA"
    # Who that channel is in this project's terms is the pipeline's business.
    assert videos[0].capper_id == ""


def search_discovery(items, durations, **kwargs):
    routes = {
        "/search?": StubResponse(search_payload(items)),
        "/videos?": StubResponse(durations_payload(durations)),
    }
    kwargs.setdefault("api_key", "test-key")
    kwargs.setdefault("lookback_days", 14)
    kwargs.setdefault("title_contains", ["nurmagomedov"])
    return make_discovery(routes, **kwargs)


def test_search_keeps_prediction_videos_about_this_event():
    discovery = search_discovery(
        [
            ("vid00000001", "Nurmagomedov vs Song PREDICTIONS", "UC1", "A MMA", 1),
            ("vid00000002", "Nurmagomedov weigh-in live stream", "UC2", "B MMA", 1),
            ("vid00000003", "UFC 999 picks and parlays", "UC3", "C MMA", 1),
        ],
        [("vid00000001", "PT15M"), ("vid00000002", "PT2H"), ("vid00000003", "PT20M")],
    )
    videos, report = discovery.search(["nurmagomedov predictions"])

    # The weigh-in mentions the event but isn't a pick; the other picks video
    # isn't this event.
    assert [v.video_id for v in videos] == ["vid00000001"]
    assert report[0]["returned"] == 3 and report[0]["kept"] == 1


def test_search_drops_shorts_and_clips():
    discovery = search_discovery(
        [
            ("vid00000001", "Nurmagomedov predictions", "UC1", "A MMA", 1),
            ("vid00000002", "Nurmagomedov pick #shorts", "UC2", "B MMA", 1),
        ],
        [("vid00000001", "PT11M"), ("vid00000002", "PT47S")],
    )
    videos, _ = discovery.search(["q"], min_duration_seconds=180)
    assert [v.video_id for v in videos] == ["vid00000001"]


def test_search_caps_per_channel_and_overall():
    items = [
        (f"vid0000000{i}", "Nurmagomedov predictions", f"UC{i // 2}", f"Chan {i // 2}", i * 0.1)
        for i in range(1, 7)
    ]
    discovery = search_discovery(items, [(v, "PT10M") for v, *_ in items])
    videos, _ = discovery.search(["q"], max_per_channel=1, max_results=2)
    assert len(videos) == 2
    assert len({v.channel_id for v in videos}) == 2


def test_search_without_a_key_is_skipped_not_fatal():
    discovery = search_discovery([], [], api_key="")
    videos, report = discovery.search(["q"])
    assert videos == []
    assert report[0]["status"] == "skipped"


def test_a_failing_query_does_not_stop_the_others():
    routes = {
        "q=bad": requests.exceptions.ConnectionError("boom"),
        "/search?": StubResponse(
            search_payload([("vid00000001", "Nurmagomedov predictions", "UC1", "A", 1)])
        ),
        "/videos?": StubResponse(durations_payload([("vid00000001", "PT10M")])),
    }
    discovery = make_discovery(
        routes, api_key="k", lookback_days=14, title_contains=["nurmagomedov"]
    )
    videos, report = discovery.search(["bad", "good"])
    assert [v.video_id for v in videos] == ["vid00000001"]
    assert [entry["status"] for entry in report if "query" in entry][:2] == ["failed", "ok"]


# -- who a searched-up video belongs to ------------------------------------


def searched(channel_id="UC_NEW", channel_title="Fresh MMA Picks"):
    from mma_engine.discover import DiscoveredVideo

    return DiscoveredVideo(
        video_id="vid00000001",
        capper_id="",
        url="https://www.youtube.com/watch?v=vid00000001",
        title="Nurmagomedov vs Song predictions",
        published=datetime.now(timezone.utc),
        channel_id=channel_id,
        channel_title=channel_title,
    )


def config_with(cappers, tmp_path):
    from mma_engine.config import load_config

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"event": {"name": "X"}, "cappers": cappers}), encoding="utf-8"
    )
    return load_config(path)


def test_a_searched_video_matches_its_configured_capper_by_channel_id(tmp_path):
    from mma_engine.pipeline import capper_for_channel

    config = config_with(
        [{"id": "artem_mma", "name": "Artem MMA", "channel_id": "UC_ARTEM",
          "trust": {"overall": 6.2}}],
        tmp_path,
    )
    capper = capper_for_channel(config, searched(channel_id="UC_ARTEM", channel_title="Renamed Channel"))
    # Matched on the id, so a renamed channel keeps its earned trust.
    assert (capper.id, capper.trust_for("overall")) == ("artem_mma", 6.2)


def test_a_searched_video_matches_by_name_when_the_id_is_unknown(tmp_path):
    from mma_engine.pipeline import capper_for_channel

    config = config_with(
        [{"id": "funky_picks", "name": "Funky Picks", "trust": {"overall": 7.1}}], tmp_path
    )
    capper = capper_for_channel(config, searched(channel_id="UC_OTHER", channel_title="funky picks"))
    assert capper.id == "funky_picks"


def test_an_unknown_channel_is_minted_at_neutral_trust(tmp_path):
    from mma_engine.pipeline import capper_for_channel

    config = config_with([{"id": "a", "name": "A"}], tmp_path)
    capper = capper_for_channel(config, searched())

    assert capper.id == "yt_fresh_mma_picks"
    assert capper.name == "Fresh MMA Picks"
    assert capper.trust_for("overall") == 5.0
    # Not swept weekly off the back of one video — that stays a person's call.
    assert capper.discover is False
    # And it is in the config for the rest of the run, so the pipeline can
    # look it up by id when the pick is attributed.
    assert config.cappers["yt_fresh_mma_picks"] is capper
