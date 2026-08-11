"""Tests for channel discovery — feed parsing, filtering, and merge behavior.

No network: RSS and channel-page HTML are supplied as fixtures, and the HTTP
session is stubbed. Run with:  PYTHONPATH=src python -m pytest -q
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import requests

from mma_engine.config import Capper
from mma_engine.discover import (
    ChannelDiscovery,
    parse_channel_id,
    parse_channel_id_from_page,
    parse_feed,
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
