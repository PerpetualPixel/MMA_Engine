"""Tests for reading a picks video off its screen.

Covers frame sampling (against a synthetic video, using the ffmpeg the
imageio-ffmpeg wheel ships), perceptual-hash de-duplication, the frame
reader's cache and partial-failure behaviour, reading persistence, the
config plumbing, and the pipeline wiring — attribution to the posting
channel, on-screen boards counted as a roundup, pasted cards superseding
screen picks. The vision call and yt-dlp are stubbed: no network, no key.

Run with:  PYTHONPATH=src python -m pytest -q
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import pytest

from mma_engine.aggregate import SourcedPick
from mma_engine.config import Capper, ConfigError, ScreenVideoRef, load_config
from mma_engine.extract import Pick
from mma_engine.pasted_picks import supersede_video_picks
from mma_engine.roundup_slides import SlideFight, ffmpeg_path
from mma_engine.screen_picks import (
    ScreenRead,
    ScreenReader,
    ScreenReading,
    VideoInfo,
    _board_to_fight,
    difference_hash,
    extract_frames,
    fetch_video_info,
    hamming,
    load_reading,
    save_reading,
    unique_frames,
)

FFMPEG = ffmpeg_path()
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="no ffmpeg available")


def pick(a="Jean Silva", b="Jose Miguel Delgado", selection="Jean Silva", **kw) -> Pick:
    fields = dict(
        fighter_a=a,
        fighter_b=b,
        bet_type="moneyline",
        selection=selection,
        fighter=selection,
        confidence=5,
        role="unknown",
        odds_american="",
        stake_units="",
        reasoning="",
    )
    fields.update(kw)
    return Pick(**fields)


BASE_CONFIG = {
    "event": {"name": "Noche UFC"},
    "cappers": [
        {
            "id": "funky_picks",
            "name": "Funky Picks",
            "channel_id": "UCfunky",
            "trust": {"overall": 8.0},
            "aliases": ["Funk Picks"],
        },
        {"id": "mma_guru", "name": "MMA Guru", "trust": {"overall": 6.0}},
    ],
    "videos": [],
}


def write_config(tmp_path: Path, raw: dict) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


# -- frames ----------------------------------------------------------------


def render(directory: Path, name: str, source: str, seconds: int = 1) -> Path:
    path = directory / name
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"{source}=s=320x180:d={seconds}:r=8", "-frames:v", "1", str(path)],
        check=True,
    )
    return path


@needs_ffmpeg
def test_extract_frames_samples_a_still_video_periodically(tmp_path):
    """A video with no cuts at all still yields a frame every sample interval:
    that is the guarantee a fade-in graphic is caught."""
    video = tmp_path / "still.mp4"
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "smptebars=s=320x180:d=10:r=8", "-pix_fmt", "yuv420p", str(video)],
        check=True,
    )
    frames = extract_frames(video, tmp_path / "frames", sample_seconds=2.0, max_frames=50)
    # First frame, then one every ~2s over 10s.
    assert 4 <= len(frames) <= 6
    assert all(f.suffix == ".jpg" for f in frames)


@needs_ffmpeg
def test_extract_frames_respects_the_ceiling(tmp_path):
    video = tmp_path / "still.mp4"
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "testsrc=s=320x180:d=10:r=8", "-pix_fmt", "yuv420p", str(video)],
        check=True,
    )
    frames = extract_frames(video, tmp_path / "frames", sample_seconds=1.0, max_frames=3)
    assert len(frames) == 3


def test_extract_frames_without_ffmpeg_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("mma_engine.screen_picks.ffmpeg_path", lambda: None)
    assert extract_frames(tmp_path / "nope.mp4", tmp_path / "frames") == []


# -- de-duplication --------------------------------------------------------


@needs_ffmpeg
def test_difference_hash_is_stable_and_discriminating(tmp_path):
    bars = render(tmp_path, "bars.png", "smptebars")
    bars_again = render(tmp_path, "bars2.jpg", "smptebars")
    test = render(tmp_path, "test.png", "testsrc")
    h_bars, h_again, h_test = (difference_hash(p) for p in (bars, bars_again, test))
    assert h_bars is not None and h_test is not None
    # Same picture through a different codec: within a few bits.
    assert hamming(h_bars, h_again) <= 4
    # A different picture: far apart.
    assert hamming(h_bars, h_test) > 10


@needs_ffmpeg
def test_unique_frames_drops_repeats_even_after_an_aside(tmp_path):
    """A → B → A: the return to the first picture is a repeat, not a new
    screenshot, because every kept frame is compared against, not only the
    previous one."""
    a = render(tmp_path, "a.png", "smptebars")
    b = render(tmp_path, "b.png", "testsrc")
    a2 = render(tmp_path, "c.jpg", "smptebars")
    assert unique_frames([a, b, a2]) == [a, b]


def test_unique_frames_keeps_what_it_cannot_hash(tmp_path, monkeypatch):
    monkeypatch.setattr("mma_engine.screen_picks.difference_hash", lambda p: None)
    paths = [tmp_path / "x.jpg", tmp_path / "y.jpg"]
    assert unique_frames(paths) == paths


def test_hamming_counts_differing_bits():
    assert hamming(0b1010, 0b0101) == 4
    assert hamming(7, 7) == 0


# -- metadata --------------------------------------------------------------


def test_fetch_video_info_parses_yt_dlp_json(monkeypatch):
    payload = {"id": "0Iggszq1z9M", "title": "Noche UFC picks", "channel": "Funky Picks",
               "channel_id": "UCfunky"}

    def fake_run(command, **kwargs):
        assert "--dump-single-json" in command and "--skip-download" in command
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("mma_engine.screen_picks.subprocess.run", fake_run)
    info = fetch_video_info("https://youtu.be/0Iggszq1z9M?si=abc")
    assert info == VideoInfo(
        video_id="0Iggszq1z9M", url="https://youtu.be/0Iggszq1z9M?si=abc",
        title="Noche UFC picks", channel="Funky Picks", channel_id="UCfunky",
    )


def test_fetch_video_info_passes_cookies_through(monkeypatch):
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"id": "0Iggszq1z9M"}), stderr="")

    monkeypatch.setattr("mma_engine.screen_picks.subprocess.run", fake_run)
    fetch_video_info("https://youtu.be/0Iggszq1z9M", extra_args=["--cookies", "cookies.txt"])
    assert "--cookies" in seen["command"] and "cookies.txt" in seen["command"]


def test_fetch_video_info_fails_open(monkeypatch):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="ERROR: blocked")

    monkeypatch.setattr("mma_engine.screen_picks.subprocess.run", fake_run)
    assert fetch_video_info("https://youtu.be/0Iggszq1z9M") is None
    assert fetch_video_info("not a url") is None


# -- the reader ------------------------------------------------------------


class FakeResponse:
    def __init__(self, parsed, stop_reason="end_turn"):
        self.parsed_output = parsed
        self.stop_reason = stop_reason
        self.stop_details = None


class FakeClient:
    """Stands in for anthropic.Anthropic: one scripted answer per call."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []
        self.messages = self

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(answer)


def frame(directory: Path, name: str, content: bytes) -> Path:
    path = directory / name
    path.write_bytes(content)
    return path


def test_reader_folds_the_same_pick_across_frames_keeping_the_stronger(tmp_path):
    frames = [frame(tmp_path, f"f{i}.jpg", bytes([i]) * 16) for i in range(3)]
    client = FakeClient([
        ScreenRead(event_name="", picks=[], boards=[]),
        ScreenRead(event_name="Noche UFC", picks=[pick(confidence=5)], boards=[]),
        ScreenRead(event_name="", picks=[pick(confidence=8, stake_units="2u")], boards=[]),
    ])
    reader = ScreenReader(client=client, cache_dir=tmp_path / "cache")
    report = reader.read(frames, title="Picks", channel="Funky Picks")

    assert (report.frames, report.read, report.cached, report.failed) == (3, 3, 0, 0)
    assert report.event_name == "Noche UFC"
    assert len(report.picks) == 1
    assert report.picks[0].confidence == 8 and report.picks[0].stake_units == "2u"
    # The video's title and channel reach the prompt, so the reader knows
    # whose picks it is looking at.
    text = client.calls[0]["messages"][0]["content"][1]["text"]
    assert "Picks" in text and "Funky Picks" in text


def test_reader_caches_by_frame_bytes_and_reads_boards(tmp_path):
    same = frame(tmp_path, "a.jpg", b"same-bytes")
    copy = frame(tmp_path, "b.jpg", b"same-bytes")
    board = SlideFight(
        fighter_a="Jean Silva", fighter_b="Jose Miguel Delgado",
        cappers_for_a=["MMA Guru"], cappers_for_b=["Funky Picks"],
        stated_count_a=1, stated_count_b=1,
    )
    client = FakeClient([ScreenRead(event_name="", picks=[], boards=[board])])
    reader = ScreenReader(client=client, cache_dir=tmp_path / "cache")
    report = reader.read([same, copy])

    assert (report.read, report.cached) == (1, 1)
    assert len(client.calls) == 1
    assert len(report.boards) == 2  # one board per frame; merge_roundups folds them
    assert report.boards[0].cappers_for_b == ["Funky Picks"]


def test_reader_keeps_what_it_read_when_the_api_stops(tmp_path):
    frames = [frame(tmp_path, f"f{i}.jpg", bytes([i]) * 16) for i in range(3)]
    client = FakeClient([
        ScreenRead(event_name="", picks=[pick()], boards=[]),
        anthropic.APIConnectionError(request=None),
        ScreenRead(event_name="", picks=[pick(selection="Jose Miguel Delgado")], boards=[]),
    ])
    reader = ScreenReader(client=client, cache_dir=tmp_path / "cache")
    report = reader.read(frames)

    assert report.read == 1 and report.failed == 1
    assert "APIConnectionError" in report.error
    assert [p.selection for p in report.picks] == ["Jean Silva"]
    assert len(client.calls) == 2  # stopped, did not try the third


def test_reader_skips_a_frame_it_cannot_parse_and_carries_on(tmp_path):
    frames = [frame(tmp_path, f"f{i}.jpg", bytes([i]) * 16) for i in range(2)]
    client = FakeClient([
        RuntimeError("no structured output"),
        ScreenRead(event_name="", picks=[pick()], boards=[]),
    ])
    report = ScreenReader(client=client, cache_dir=tmp_path / "cache").read(frames)
    assert (report.read, report.failed) == (1, 1)
    assert len(report.picks) == 1


# -- readings --------------------------------------------------------------


def test_reading_round_trips_and_never_overwrites(tmp_path):
    reading = ScreenReading(
        video_id="0Iggszq1z9M", source_url="https://youtu.be/0Iggszq1z9M",
        title="Picks", channel="Funky Picks", channel_id="UCfunky",
        event_name="Noche UFC", picks=[pick(confidence=7)],
    )
    path = save_reading(tmp_path / "screens", reading)
    assert path is not None and path.name == "0Iggszq1z9M.json"
    loaded = load_reading(tmp_path / "screens", "0Iggszq1z9M")
    assert loaded is not None
    assert loaded.channel_id == "UCfunky" and loaded.picks[0].confidence == 7

    # A second save is a no-op: the file on disk may have been hand-corrected.
    reading.picks = []
    reading.boards = []
    assert save_reading(tmp_path / "screens", reading) is None
    assert save_reading(tmp_path / "screens", ScreenReading("new", "", picks=[])) is None
    assert load_reading(tmp_path / "screens", "0Iggszq1z9M").picks[0].confidence == 7


def test_unreadable_or_empty_reading_is_ignored(tmp_path):
    directory = tmp_path / "screens"
    directory.mkdir()
    (directory / "bad.json").write_text("{not json", encoding="utf-8")
    (directory / "empty.json").write_text(json.dumps({"video_id": "empty", "picks": []}))
    assert load_reading(directory, "bad") is None
    assert load_reading(directory, "empty") is None
    assert load_reading(directory, "missing") is None


# -- config ----------------------------------------------------------------


def test_config_accepts_urls_and_objects_in_screen_videos(tmp_path):
    config = load_config(write_config(tmp_path, {
        **BASE_CONFIG,
        "screen_videos": [
            "https://youtu.be/0Iggszq1z9M",
            {"url": "https://www.youtube.com/watch?v=X8h8G_3by-M", "capper_id": "mma_guru"},
            "https://youtu.be/0Iggszq1z9M",  # listed twice: one entry
        ],
    }))
    assert config.screen_videos == [
        ScreenVideoRef(video_id="0Iggszq1z9M", url="https://youtu.be/0Iggszq1z9M"),
        ScreenVideoRef(
            video_id="X8h8G_3by-M", url="https://www.youtube.com/watch?v=X8h8G_3by-M",
            capper_id="mma_guru",
        ),
    ]
    assert config.settings["screen_picks"]["sample_seconds"] == 8


def test_config_rejects_an_unknown_capper_on_a_screen_video(tmp_path):
    with pytest.raises(ConfigError, match="unknown capper_id"):
        load_config(write_config(tmp_path, {
            **BASE_CONFIG,
            "screen_videos": [{"url": "https://youtu.be/0Iggszq1z9M", "capper_id": "nobody"}],
        }))


def test_retarget_clears_screen_videos(tmp_path, monkeypatch):
    from mma_engine import auto_event

    raw = {
        **BASE_CONFIG,
        "event": {"mode": "auto", "name": "Old Card"},
        "screen_videos": ["https://youtu.be/0Iggszq1z9M"],
        "tracker": {"picks_videos": ["https://youtu.be/X8h8G_3by-M"]},
    }
    path = write_config(tmp_path, raw)
    monkeypatch.setattr(
        auto_event,
        "find_next_event",
        lambda *a, **k: {"name": "New Card", "league": "ufc", "date": "2026-10-01T00:00Z",
                         "fighter_a": "A B", "fighter_b": "C D"},
    )
    assert auto_event.resolve_auto_event(path) is True
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["screen_videos"] == []
    assert after["tracker"]["picks_videos"] == []


# -- supersession ------------------------------------------------------------


def test_a_pasted_card_supersedes_screen_picks_too():
    capper = Capper(id="funky_picks", name="Funky Picks", trust={"overall": 8.0})
    on_screen = SourcedPick(pick=pick(), capper=capper, video_id="v", video_url="", source_kind="screens")
    other = SourcedPick(
        pick=pick(a="Rob Font", b="Jean Matsumoto", selection="Rob Font"),
        capper=capper, video_id="v", video_url="", source_kind="screens",
    )
    pasted = SourcedPick(pick=pick(confidence=9), capper=capper, video_id="p", video_url="", source_kind="pasted")
    kept, dropped = supersede_video_picks([on_screen, other], [pasted])
    assert dropped == 1 and kept == [other]


# -- the pipeline ------------------------------------------------------------


def _install_fakes(monkeypatch, pipeline, *, info, reads):
    """Stub the network edges: yt-dlp metadata, download+frames, vision."""
    monkeypatch.setattr(pipeline, "fetch_video_info", lambda url, proxy="", extra_args=None: info)

    def fake_read_video_screens(
        config, url, video_id, title="", channel="", frames_dir=None, video_file=None,
        fighters=None,
    ):
        from mma_engine.screen_picks import ScreenReport
        report = ScreenReport(frames=len(reads), read=len(reads))
        raw_picks = []
        for read in reads:
            raw_picks.extend(read.picks)
            report.boards.extend(_board_to_fight(b) for b in read.boards)
            report.event_name = report.event_name or read.event_name
        from mma_engine.extract import _dedupe
        report.picks = _dedupe(raw_picks)
        return report

    monkeypatch.setattr(pipeline, "read_video_screens", fake_read_video_screens)


def test_pipeline_attributes_screen_picks_to_the_posting_channel(tmp_path, monkeypatch):
    from mma_engine import pipeline

    monkeypatch.chdir(tmp_path)
    config = load_config(write_config(tmp_path, BASE_CONFIG))
    _install_fakes(
        monkeypatch, pipeline,
        info=VideoInfo(video_id="0Iggszq1z9M", url="https://youtu.be/0Iggszq1z9M",
                       title="Noche UFC picks", channel="Funky Picks", channel_id="UCfunky"),
        reads=[ScreenRead(event_name="Noche UFC",
                          picks=[pick(confidence=8, odds_american="-180", role="favorite")],
                          boards=[])],
    )
    picks, sources = [], []
    event = pipeline.ingest_screen_videos(
        config,
        [ScreenVideoRef(video_id="0Iggszq1z9M", url="https://youtu.be/0Iggszq1z9M")],
        sourced_picks=picks, sources=sources,
    )

    assert event == "Noche UFC"
    (sourced,) = picks
    assert sourced.capper.id == "funky_picks"  # matched on channel id, trust kept
    assert sourced.capper.trust_for("favorite") == 8.0
    assert sourced.source_kind == "screens"
    assert sourced.pick.odds_american == "-180"
    (record,) = sources
    assert record["kind"] == "screens" and record["status"] == "ok"
    assert record["capper"] == "Funky Picks" and record["pick_count"] == 1
    assert record["title"] == "Noche UFC picks"
    # The reading was kept, and carries whose video it was.
    saved = json.loads((tmp_path / "screens" / "0Iggszq1z9M.json").read_text(encoding="utf-8"))
    assert saved["channel_id"] == "UCfunky" and len(saved["picks"]) == 1


def test_pipeline_mints_an_unknown_channel_and_honours_a_pinned_capper(tmp_path, monkeypatch):
    from mma_engine import pipeline

    monkeypatch.chdir(tmp_path)
    config = load_config(write_config(tmp_path, BASE_CONFIG))
    _install_fakes(
        monkeypatch, pipeline,
        info=VideoInfo(video_id="AAAAAAAAAA1", url="u", channel="Octagon Oracle", channel_id="UCnew"),
        reads=[ScreenRead(event_name="", picks=[pick()], boards=[])],
    )
    picks, sources = [], []
    pipeline.ingest_screen_videos(
        config, [ScreenVideoRef(video_id="AAAAAAAAAA1", url="u")], sourced_picks=picks, sources=sources,
    )
    assert picks[0].capper.name == "Octagon Oracle"
    assert picks[0].capper.trust_for("unknown") == 5.0
    assert picks[0].capper.id in config.cappers

    # Pinned by hand: the channel is ignored, the named capper gets the picks.
    picks, sources = [], []
    pipeline.ingest_screen_videos(
        config,
        [ScreenVideoRef(video_id="AAAAAAAAAA2", url="u2", capper_id="mma_guru")],
        sourced_picks=picks, sources=sources,
    )
    assert picks[0].capper.id == "mma_guru"


def test_pipeline_reuses_a_saved_reading_without_touching_the_network(tmp_path, monkeypatch):
    from mma_engine import pipeline

    monkeypatch.chdir(tmp_path)
    config = load_config(write_config(tmp_path, BASE_CONFIG))
    save_reading(tmp_path / "screens", ScreenReading(
        video_id="0Iggszq1z9M", source_url="https://youtu.be/0Iggszq1z9M",
        channel="Funk Picks", picks=[pick(confidence=6)],
    ))

    def boom(*a, **k):
        raise AssertionError("network touched")

    monkeypatch.setattr(pipeline, "fetch_video_info", boom)
    monkeypatch.setattr(pipeline, "read_video_screens", boom)
    picks, sources = [], []
    pipeline.ingest_screen_videos(
        config, [ScreenVideoRef(video_id="0Iggszq1z9M", url="https://youtu.be/0Iggszq1z9M")],
        sourced_picks=picks, sources=sources,
    )
    assert picks[0].capper.id == "funky_picks"  # alias match on the stored channel name
    assert sources[0]["from_reading"] is True


def test_pipeline_counts_on_screen_boards_as_a_roundup(tmp_path, monkeypatch):
    """A tracker-style board on screen is everyone's picks, not the poster's:
    one neutral vote per channel, deferring to a capper already covered."""
    from mma_engine import pipeline

    monkeypatch.chdir(tmp_path)
    config = load_config(write_config(tmp_path, BASE_CONFIG))
    board = SlideFight(
        fighter_a="Jean Silva", fighter_b="Jose Miguel Delgado",
        cappers_for_a=["MMA Guru", "Chisanga MMA"], cappers_for_b=["Funky Picks"],
        stated_count_a=2, stated_count_b=1,
    )
    _install_fakes(
        monkeypatch, pipeline,
        info=VideoInfo(video_id="X8h8G_3by-M", url="u", channel="UFC Predictions Tracker",
                       channel_id="UCtracker"),
        reads=[ScreenRead(event_name="", picks=[], boards=[board])],
    )
    own = SourcedPick(
        pick=pick(confidence=9), capper=config.cappers["funky_picks"],
        video_id="own", video_url="", source_kind="video",
    )
    picks, sources = [own], []
    pipeline.ingest_screen_videos(
        config, [ScreenVideoRef(video_id="X8h8G_3by-M", url="u")], sourced_picks=picks, sources=sources,
    )
    added = [p for p in picks if p.source_kind == "tracker"]
    assert {p.capper.name for p in added} == {"MMA Guru", "Chisanga MMA"}
    assert all(p.pick.confidence == 5 for p in added)
    (record,) = sources
    assert record["board_fights"] == 1 and record["superseded"] == 1
    assert record["pick_count"] == 2


def test_pipeline_reports_a_video_with_nothing_on_screen(tmp_path, monkeypatch):
    from mma_engine import pipeline

    monkeypatch.chdir(tmp_path)
    config = load_config(write_config(tmp_path, BASE_CONFIG))
    _install_fakes(monkeypatch, pipeline, info=None, reads=[ScreenRead(event_name="", picks=[], boards=[])])
    picks, sources = [], []
    pipeline.ingest_screen_videos(
        config, [ScreenVideoRef(video_id="BBBBBBBBBB1", url="u")], sourced_picks=picks, sources=sources,
    )
    assert picks == []
    assert sources[0]["status"] == "no_picks_on_screen"
    assert not (tmp_path / "screens").exists()


def test_pipeline_skip_extraction_makes_no_calls(tmp_path, monkeypatch):
    from mma_engine import pipeline

    monkeypatch.chdir(tmp_path)
    config = load_config(write_config(tmp_path, BASE_CONFIG))

    def boom(*a, **k):
        raise AssertionError("network touched")

    monkeypatch.setattr(pipeline, "fetch_video_info", boom)
    picks, sources = [], []
    pipeline.ingest_screen_videos(
        config, [ScreenVideoRef(video_id="BBBBBBBBBB1", url="u")],
        sourced_picks=picks, sources=sources, skip_extraction=True,
    )
    assert sources[0]["status"] == "extraction_skipped"


def test_remember_screen_videos_is_additive_and_idempotent(tmp_path):
    from mma_engine.pipeline import remember_screen_videos

    path = write_config(tmp_path, {**BASE_CONFIG, "screen_videos": ["https://youtu.be/0Iggszq1z9M"]})
    added = remember_screen_videos(path, [
        ScreenVideoRef(video_id="0Iggszq1z9M", url="https://youtu.be/0Iggszq1z9M"),
        ScreenVideoRef(video_id="X8h8G_3by-M", url="https://youtu.be/X8h8G_3by-M", capper_id="mma_guru"),
        ScreenVideoRef(video_id="captured_frames", url="", capper_id="mma_guru"),
    ])
    assert added == ["X8h8G_3by-M"]
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["screen_videos"] == [
        "https://youtu.be/0Iggszq1z9M",
        {"url": "https://youtu.be/X8h8G_3by-M", "capper_id": "mma_guru"},
    ]
    assert remember_screen_videos(path, [ScreenVideoRef(video_id="X8h8G_3by-M", url="x")]) == []


def test_cli_wires_a_pasted_url_into_the_run(tmp_path, monkeypatch):
    """`--picks-from-video URL --remember-videos` reaches run_pipeline as a
    screen video and lands in config.json."""
    from mma_engine import pipeline

    monkeypatch.chdir(tmp_path)
    path = write_config(tmp_path, BASE_CONFIG)
    seen = {}

    def fake_run_pipeline(config, output_path, **kwargs):
        seen.update(kwargs)
        return {"event": {"name": "x"}, "totals": {"videos": 1, "cappers": 1, "picks": 1, "fights": 1},
                "sources": [{"status": "ok", "capper": "c", "video_id": "v", "kind": "screens", "pick_count": 1}],
                "fights": [{"display": "a vs b", "markets": []}]}

    monkeypatch.setattr(pipeline, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(pipeline, "fetch_event_cards", lambda specs: [])
    code = pipeline.main([
        "--config", str(path), "--output", str(tmp_path / "out.json"), "--no-discover",
        "--picks-from-video", "https://youtu.be/0Iggszq1z9M", "--video-capper", "mma_guru",
        "--remember-videos",
    ])
    assert code == 0
    assert seen["screen_videos"] == [
        ScreenVideoRef(video_id="0Iggszq1z9M", url="https://youtu.be/0Iggszq1z9M", capper_id="mma_guru")
    ]
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["screen_videos"] == [{"url": "https://youtu.be/0Iggszq1z9M", "capper_id": "mma_guru"}]


def test_cli_rejects_a_bad_url_or_unknown_capper(tmp_path, monkeypatch):
    from mma_engine import pipeline

    monkeypatch.chdir(tmp_path)
    path = write_config(tmp_path, BASE_CONFIG)
    assert pipeline.main(["--config", str(path), "--picks-from-video", "nope"]) == 2
    assert pipeline.main([
        "--config", str(path), "--picks-from-video", "https://youtu.be/0Iggszq1z9M",
        "--video-capper", "nobody",
    ]) == 2
    assert pipeline.main(["--config", str(path), "--video-frames", str(tmp_path / "nope")]) == 2


def test_cli_video_frames_attach_to_the_pasted_url(tmp_path, monkeypatch):
    from mma_engine import pipeline

    monkeypatch.chdir(tmp_path)
    path = write_config(tmp_path, BASE_CONFIG)
    seen = {}

    def fake_run_pipeline(config, output_path, **kwargs):
        seen.update(kwargs)
        return {"event": {"name": "x"}, "totals": {"videos": 1, "cappers": 1, "picks": 1, "fights": 1},
                "sources": [{"status": "ok", "capper": "c", "video_id": "v", "pick_count": 1}],
                "fights": [{"display": "a vs b", "markets": []}]}

    monkeypatch.setattr(pipeline, "run_pipeline", fake_run_pipeline)
    shots = tmp_path / "shots"
    shots.mkdir()
    assert pipeline.main([
        "--config", str(path), "--no-discover",
        "--picks-from-video", "https://youtu.be/0Iggszq1z9M", "--video-frames", str(shots),
    ]) == 0
    (ref,) = seen["screen_videos"]
    assert ref.video_id == "0Iggszq1z9M" and ref.frames_dir == str(shots)


def test_cli_video_file_alone_is_keyed_on_the_filename(tmp_path, monkeypatch):
    """A video downloaded by hand needs no URL and no capper: the reading is
    named after the file and its poster is minted from the name."""
    from mma_engine import pipeline

    monkeypatch.chdir(tmp_path)
    path = write_config(tmp_path, BASE_CONFIG)
    seen = {}

    def fake_run_pipeline(config, output_path, **kwargs):
        seen.update(kwargs)
        return {"event": {"name": "x"}, "totals": {"videos": 1, "cappers": 1, "picks": 1, "fights": 1},
                "sources": [{"status": "ok", "capper": "c", "video_id": "v", "pick_count": 1}],
                "fights": [{"display": "a vs b", "markets": []}]}

    monkeypatch.setattr(pipeline, "run_pipeline", fake_run_pipeline)
    clip = tmp_path / "UFC 331 Picks (1).mp4"
    clip.write_bytes(b"not really a video")
    assert pipeline.main([
        "--config", str(path), "--no-discover", "--video-file", str(clip), "--remember-videos",
    ]) == 0
    (ref,) = seen["screen_videos"]
    assert ref.video_id == "local_ufc_331_picks_1" and ref.video_file == str(clip) and ref.url == ""
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["screen_videos"] == [{"video_file": str(clip)}]
    # ...and that entry loads back as the same ref next run.
    assert load_config(path).screen_videos == [ref]

    assert pipeline.main(["--config", str(path), "--video-file", str(tmp_path / "missing.mp4")]) == 2
    assert pipeline.main([
        "--config", str(path), "--video-file", str(clip), "--video-frames", str(tmp_path),
    ]) == 2


@needs_ffmpeg
def test_read_video_screens_cuts_a_local_file_and_leaves_it_alone(tmp_path, monkeypatch):
    from mma_engine import pipeline

    monkeypatch.chdir(tmp_path)
    config = load_config(write_config(tmp_path, BASE_CONFIG))
    clip = tmp_path / "picks.mp4"
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "smptebars=s=320x180:d=6:r=8", "-pix_fmt", "yuv420p", str(clip)],
        check=True,
    )
    monkeypatch.setattr(pipeline, "download_video", lambda *a, **k: (_ for _ in ()).throw(AssertionError("downloaded")))

    class FakeReader:
        def __init__(self, **kwargs):
            pass

        def read(self, frames, title="", channel="", fighters=None):
            from mma_engine.screen_picks import ScreenReport
            return ScreenReport(frames=len(list(frames)), picks=[pick()])

    monkeypatch.setattr(pipeline, "ScreenReader", FakeReader)
    report = pipeline.read_video_screens(config, "", "local_picks", title="picks", video_file=clip)
    assert report.frames >= 1 and len(report.picks) == 1
    assert clip.is_file()  # the user's file is not the run's to delete


def test_pipeline_attributes_a_local_file_by_its_name(tmp_path, monkeypatch):
    from mma_engine import pipeline

    monkeypatch.chdir(tmp_path)
    config = load_config(write_config(tmp_path, BASE_CONFIG))
    clip = tmp_path / "UFC 331 picks.mp4"
    clip.write_bytes(b"x")
    _install_fakes(monkeypatch, pipeline, info=None, reads=[ScreenRead(event_name="", picks=[pick()], boards=[])])
    picks, sources = [], []
    pipeline.ingest_screen_videos(
        config,
        [ScreenVideoRef(video_id="local_ufc_331_picks", url="", video_file=str(clip))],
        sourced_picks=picks, sources=sources,
    )
    assert picks[0].capper.name == "Video: UFC 331 picks"
    assert sources[0]["title"] == "UFC 331 picks"


# -- the download ----------------------------------------------------------


def test_download_video_retries_once_with_cookies_and_a_different_client(tmp_path, monkeypatch):
    """A refused first attempt (YouTube's 403 on the adaptive streams) is
    retried with a progressive format and another player client; the cookie
    flags ride along on both."""
    from mma_engine.roundup_slides import RETRY_EXTRACTOR_ARGS, download_video

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="ERROR: unable to download video data: HTTP Error 403: Forbidden")
        (tmp_path / "vid" / "0Iggszq1z9M.mp4").write_bytes(b"ok")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("mma_engine.roundup_slides.subprocess.run", fake_run)
    got = download_video(
        "https://youtu.be/0Iggszq1z9M", tmp_path / "vid", "0Iggszq1z9M",
        extra_args=["--cookies", "cookies.txt"],
    )
    assert got is not None and got.name == "0Iggszq1z9M.mp4"
    assert len(calls) == 2
    assert all("--cookies" in c for c in calls)
    assert RETRY_EXTRACTOR_ARGS[1] in calls[1] and RETRY_EXTRACTOR_ARGS[1] not in calls[0]
    assert calls[1][calls[1].index("-f") + 1].startswith("b[height<=")


def test_download_video_gives_up_after_the_retry(tmp_path, monkeypatch, caplog):
    from mma_engine.roundup_slides import download_video

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="HTTP Error 403: Forbidden")

    monkeypatch.setattr("mma_engine.roundup_slides.subprocess.run", fake_run)
    with caplog.at_level("WARNING"):
        assert download_video("https://youtu.be/0Iggszq1z9M", tmp_path / "vid", "0Iggszq1z9M") is None
    assert "pip install -U yt-dlp" in caplog.text


def test_ytdlp_extra_args_are_the_configured_cookies(tmp_path, monkeypatch):
    from mma_engine.pipeline import ytdlp_extra_args

    monkeypatch.chdir(tmp_path)
    config = load_config(write_config(tmp_path, {
        **BASE_CONFIG,
        "settings": {"transcript_cookies": {"enabled": True, "file": "cookies.txt"}},
    }))
    assert ytdlp_extra_args(config) == []  # configured, but the export never landed
    (tmp_path / "cookies.txt").write_text("# Netscape HTTP Cookie File\n")
    assert ytdlp_extra_args(config) == ["--cookies", "cookies.txt"]
    config.settings["transcript_cookies"]["enabled"] = False
    assert ytdlp_extra_args(config) == []


# -- method boards, card hints, the paid ceiling ---------------------------


def test_a_method_board_becomes_method_votes_not_moneyline_ones():
    from mma_engine.roundup_slides import slide_fight_to_picks

    board = SlideFight(
        fighter_a="Jean Silva", fighter_b="Jose Miguel Delgado",
        cappers_for_a=["MMA Guru"], cappers_for_b=["Funky Picks"],
        stated_count_a=1, stated_count_b=1, market="ko_tko",
    )
    fight = slide_fight_to_picks(board)
    assert fight.cappers_for_a == [] and fight.cappers_for_b == []
    assert fight.ko_tko_for_a == ["MMA Guru"] and fight.ko_tko_for_b == ["Funky Picks"]
    assert fight.submission_for_a == [] and fight.decision_for_b == []
    # The default is still the moneyline, so every cached read without the
    # field loads as it always did.
    plain = SlideFight.model_validate({
        "fighter_a": "A B", "fighter_b": "C D", "cappers_for_a": ["x"], "cappers_for_b": [],
        "stated_count_a": 0, "stated_count_b": 0,
    })
    assert plain.market == "moneyline" and slide_fight_to_picks(plain).cappers_for_a == ["x"]


def test_pipeline_turns_on_screen_method_boards_into_method_picks(tmp_path, monkeypatch):
    from mma_engine import pipeline

    monkeypatch.chdir(tmp_path)
    config = load_config(write_config(tmp_path, BASE_CONFIG))
    moneyline = SlideFight(
        fighter_a="Jean Silva", fighter_b="Jose Miguel Delgado",
        cappers_for_a=["MMA Guru", "Funky Picks"], cappers_for_b=[],
        stated_count_a=2, stated_count_b=0,
    )
    by_ko = SlideFight(
        fighter_a="Jean Silva", fighter_b="Jose Miguel Delgado",
        cappers_for_a=["MMA Guru"], cappers_for_b=[],
        stated_count_a=1, stated_count_b=0, market="ko_tko",
    )
    _install_fakes(
        monkeypatch, pipeline,
        info=VideoInfo(video_id="X8h8G_3by-M", url="u", channel="Tracker", channel_id="UCt"),
        reads=[ScreenRead(event_name="", picks=[], boards=[moneyline]),
               ScreenRead(event_name="", picks=[], boards=[by_ko])],
    )
    picks, sources = [], []
    pipeline.ingest_screen_videos(
        config, [ScreenVideoRef(video_id="X8h8G_3by-M", url="u")], sourced_picks=picks, sources=sources,
    )
    by_type = {}
    for p in picks:
        by_type.setdefault(p.pick.bet_type, []).append((p.capper.id, p.pick.selection))
    assert sorted(by_type["moneyline"]) == [("funky_picks", "Jean Silva"), ("mma_guru", "Jean Silva")]
    assert by_type["method_of_victory"] == [("mma_guru", "Jean Silva by KO/TKO")]


def test_reader_passes_the_card_as_a_hint_and_keys_the_cache_on_it(tmp_path):
    from mma_engine.screen_picks import READER_VERSION, card_hint, read_key

    frames = [frame(tmp_path, "f.jpg", b"same")]
    client = FakeClient([
        ScreenRead(event_name="", picks=[], boards=[]),
        ScreenRead(event_name="", picks=[], boards=[]),
    ])
    reader = ScreenReader(client=client, cache_dir=tmp_path / "cache")
    reader.read(frames, fighters=["Michael Aswell", "JooSang Yoo"])
    text = client.calls[0]["messages"][0]["content"][1]["text"]
    assert "Fighters on this card: Michael Aswell, JooSang Yoo" in text
    # Same frame, no hint: a different read, not the cached one.
    reader.read(frames)
    assert len(client.calls) == 2
    assert "Fighters on this card" not in client.calls[1]["messages"][0]["content"][1]["text"]
    # And the same hint again is served from the cache.
    reader.read(frames, fighters=["Michael Aswell", "JooSang Yoo"])
    assert len(client.calls) == 2
    assert card_hint(None) == "" and card_hint(["", " "]) == ""
    assert READER_VERSION in read_key(frames[0]) and read_key(frames[0]) != read_key(frames[0], "x")


def test_card_fighter_names_flattens_the_fetched_cards():
    from mma_engine.pipeline import card_fighter_names

    cards = [{"name": "UFC 331", "fights": [
        {"fighter_a": "Joshua Van", "fighter_b": "Alexandre Pantoja"},
        {"fighter_a": "Joshua Van", "fighter_b": ""},
    ]}]
    assert card_fighter_names(cards) == ["Joshua Van", "Alexandre Pantoja"]
    assert card_fighter_names([]) == []


def test_read_video_screens_caps_the_paid_step_not_the_cut(tmp_path, monkeypatch, caplog):
    from mma_engine import pipeline
    from mma_engine.screen_picks import ScreenReport

    monkeypatch.chdir(tmp_path)
    config = load_config(write_config(tmp_path, {
        **BASE_CONFIG, "settings": {"screen_picks": {"max_frames": 3}},
    }))
    many = [tmp_path / f"f{i}.jpg" for i in range(10)]
    for path in many:
        path.write_bytes(b"x")
    monkeypatch.setattr(pipeline, "extract_frames", lambda *a, **k: many)
    monkeypatch.setattr(pipeline, "unique_frames", lambda frames, max_distance=10: list(frames))
    seen = {}

    class FakeReader:
        def __init__(self, **kwargs):
            pass

        def read(self, frames, title="", channel="", fighters=None):
            seen["frames"] = list(frames)
            seen["fighters"] = fighters
            return ScreenReport(frames=len(seen["frames"]))

    monkeypatch.setattr(pipeline, "ScreenReader", FakeReader)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    with caplog.at_level("WARNING"):
        pipeline.read_video_screens(config, "", "local_clip", video_file=clip, fighters=["A B"])
    assert seen["frames"] == many[:3] and seen["fighters"] == ["A B"]
    assert "first 3 of 10" in caplog.text and "max_frames" in caplog.text


def test_totals_count_a_local_video_as_a_video():
    from mma_engine.event_card import _refresh_totals

    payload = {"fights": [{"markets": [{"options": [{"cappers": [
        {"id": "a", "video_url": "", "video_id": "local_clip"},
        {"id": "b", "video_url": "https://youtu.be/x", "video_id": "x"},
    ]}]}]}]}
    _refresh_totals(payload)
    assert payload["totals"] == {"fights": 1, "picks": 2, "cappers": 2, "videos": 2}
