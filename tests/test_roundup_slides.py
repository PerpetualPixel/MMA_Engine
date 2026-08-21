"""Tests for reading a roundup's slides.

The channel names in a tracker roundup are printed on screen, never spoken,
so these frames are where the attributions actually come from. Covered here:
frame extraction (against a synthetic deck, using the real ffmpeg the
imageio-ffmpeg wheel ships), the content-addressed frame cache, and the
reader's behaviour when the API stops part-way. The vision call itself is
stubbed — no network, no key.

Run with:  PYTHONPATH=src python -m pytest -q
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import anthropic
import pytest

from mma_engine.roundup_slides import (
    SlideBoard,
    SlideFight,
    SlideReader,
    board_to_roundup,
    extract_slides,
    ffmpeg_path,
    frame_key,
    image_block,
    read_directory,
)
from mma_engine.tracker_picks import TrackerFightPicks, TrackerRoundup

FFMPEG = ffmpeg_path()
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="no ffmpeg available")


def build_deck(directory: Path, slides: int = 4, seconds: int = 4) -> Path:
    """A synthetic slide deck: `slides` flat colours, held `seconds` each."""
    # Detailed pictures, not flat colours: a real deck's slides carry photos
    # and columns of text, and scene scoring behaves quite differently on a
    # flat fill than on anything with structure in it.
    sources = [
        "testsrc=s=640x360:d={d}:r=8",
        "smptebars=s=640x360:d={d}:r=8",
        "yuvtestsrc=s=640x360:d={d}:r=8",
        "testsrc2=s=640x360:d={d}:r=8",
        "rgbtestsrc=s=640x360:d={d}:r=8",
        "pal75bars=s=640x360:d={d}:r=8",
    ]
    parts = []
    for index in range(slides):
        part = directory / f"part{index}.mp4"
        subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", sources[index % len(sources)].format(d=seconds),
             "-r", "8", "-pix_fmt", "yuv420p", str(part)],
            check=True,
        )
        parts.append(part)
    listing = directory / "list.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
    deck = directory / "deck.mp4"
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
         "-safe", "0", "-i", str(listing), "-c", "copy", str(deck)],
        cwd=directory, check=True,
    )
    return deck


# -- frames ----------------------------------------------------------------


@needs_ffmpeg
def test_extract_slides_finds_one_frame_per_slide_including_the_first(tmp_path):
    """The opening frame has no predecessor to score against, so it needs
    including explicitly — a deck that opens on a fight slide would otherwise
    lose that whole fight."""
    deck = build_deck(tmp_path, slides=4)
    frames = extract_slides(deck, tmp_path / "frames")
    assert 4 <= len(frames) <= 6
    assert len({frame_key(f) for f in frames}) >= 4


@needs_ffmpeg
def test_extraction_is_deterministic_so_the_frame_cache_holds(tmp_path):
    deck = build_deck(tmp_path, slides=3)
    first = {frame_key(f) for f in extract_slides(deck, tmp_path / "a")}
    second = {frame_key(f) for f in extract_slides(deck, tmp_path / "b")}
    assert first == second


@needs_ffmpeg
def test_max_frames_caps_a_long_deck(tmp_path):
    deck = build_deck(tmp_path, slides=5)
    assert len(extract_slides(deck, tmp_path / "frames", max_frames=2)) == 2


def test_extract_slides_without_ffmpeg_returns_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("mma_engine.roundup_slides.ffmpeg_path", lambda: None)
    assert extract_slides(tmp_path / "nothing.mp4", tmp_path / "frames") == []


def test_read_directory_takes_images_in_order(tmp_path):
    for name in ("b.png", "a.jpg", "notes.txt", "c.webp"):
        (tmp_path / name).write_bytes(b"x")
    assert [p.name for p in read_directory(tmp_path)] == ["a.jpg", "b.png", "c.webp"]
    assert read_directory(tmp_path / "missing") == []


def test_image_block_carries_the_right_media_type(tmp_path):
    path = tmp_path / "slide.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    block = image_block(path)
    assert block["type"] == "image"
    assert block["source"]["media_type"] == "image/png"
    assert block["source"]["type"] == "base64"


def test_frame_key_is_content_addressed(tmp_path):
    (tmp_path / "one.jpg").write_bytes(b"same")
    (tmp_path / "two.jpg").write_bytes(b"same")
    (tmp_path / "three.jpg").write_bytes(b"different")
    assert frame_key(tmp_path / "one.jpg") == frame_key(tmp_path / "two.jpg")
    assert frame_key(tmp_path / "one.jpg") != frame_key(tmp_path / "three.jpg")


# -- reading ---------------------------------------------------------------


def slide(a="Shanelle Dyer", b="Elise Reed", for_a=("Artem MMA",), for_b=("BetSam",),
          stated_a=0, stated_b=0):
    return SlideBoard(
        fights=[
            SlideFight(
                fighter_a=a, fighter_b=b,
                cappers_for_a=list(for_a), cappers_for_b=list(for_b),
                stated_count_a=stated_a, stated_count_b=stated_b,
            )
        ]
    )


class FakeClient:
    """Stands in for the vision call; can be told to fail after N slides."""

    def __init__(self, results, fail_after=None, error=None):
        self.results = list(results)
        self.fail_after = fail_after
        self.error = error
        self.calls = 0
        self.messages = self

    def parse(self, **kwargs):
        if self.fail_after is not None and self.calls >= self.fail_after:
            raise self.error
        parsed = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return type("Response", (), {
            "stop_reason": "end_turn", "stop_details": None, "parsed_output": parsed,
        })()


def frames(tmp_path, count):
    made = []
    for index in range(count):
        path = tmp_path / f"slide_{index}.jpg"
        path.write_bytes(f"frame-{index}".encode())
        made.append(path)
    return made


def test_reader_reads_every_slide(tmp_path):
    client = FakeClient([slide()])
    reader = SlideReader(client=client, cache_dir=tmp_path / "cache")
    report = reader.read(frames(tmp_path, 3))
    assert (report.frames, report.read, report.cached, report.failed) == (3, 3, 0, 0)
    assert len(report.roundups) == 3
    assert client.calls == 3


def test_a_slide_read_once_is_never_paid_for_twice(tmp_path):
    client = FakeClient([slide()])
    cache = tmp_path / "cache"
    paths = frames(tmp_path, 2)
    SlideReader(client=client, cache_dir=cache).read(paths)
    assert client.calls == 2

    again = FakeClient([slide()])
    report = SlideReader(client=again, cache_dir=cache).read(paths)
    assert again.calls == 0
    assert (report.cached, report.read) == (2, 0)
    assert len(report.roundups) == 2


def test_identical_frames_share_one_read(tmp_path):
    """Scene detection emits near-duplicates; byte-identical ones are free."""
    client = FakeClient([slide()])
    first, second = tmp_path / "a.jpg", tmp_path / "b.jpg"
    first.write_bytes(b"same-frame")
    second.write_bytes(b"same-frame")
    report = SlideReader(client=client, cache_dir=tmp_path / "cache").read([first, second])
    assert client.calls == 1
    assert (report.read, report.cached) == (1, 1)


def test_an_api_failure_keeps_the_slides_already_read(tmp_path):
    """A spent balance part-way through a deck should cost the run the rest of
    the deck, not the half it already paid for."""
    error = anthropic.APIError("credit balance too low", request=None, body=None)
    client = FakeClient([slide()], fail_after=2, error=error)
    report = SlideReader(client=client, cache_dir=tmp_path / "cache").read(
        frames(tmp_path, 6)
    )
    assert report.read == 2
    assert len(report.roundups) == 2
    assert "credit balance too low" in report.error


def test_an_unreadable_slide_does_not_stop_the_deck(tmp_path):
    class Flaky(FakeClient):
        def parse(self, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise ValueError("garbled output")
            return type("Response", (), {
                "stop_reason": "end_turn", "stop_details": None,
                "parsed_output": slide(),
            })()

    report = SlideReader(client=Flaky([]), cache_dir=tmp_path / "cache").read(
        frames(tmp_path, 3)
    )
    assert (report.read, report.failed) == (2, 1)


def test_the_slides_own_tally_catches_names_that_went_unread(tmp_path):
    """These boards print "YouTube Predictions 80/81", so a short read is
    detectable rather than silent."""
    board = slide(for_a=("Artem MMA", "BetSam"), for_b=("Kunath",), stated_a=80, stated_b=1)
    roundup, gaps = board_to_roundup(board)
    assert roundup.fights[0].cappers_for_a == ["Artem MMA", "BetSam"]
    assert gaps == ["Shanelle Dyer: read 2 name(s), slide says 80"]

    report = SlideReader(client=FakeClient([board]), cache_dir=tmp_path / "c").read(
        frames(tmp_path, 1)
    )
    assert report.gaps == ["Shanelle Dyer: read 2 name(s), slide says 80"]


def test_a_board_with_no_printed_tally_reports_no_gap():
    _, gaps = board_to_roundup(slide(for_a=("Artem MMA",), for_b=("BetSam",)))
    assert gaps == []


def test_cached_slides_round_trip_through_the_schema(tmp_path):
    cache = tmp_path / "cache"
    paths = frames(tmp_path, 1)
    SlideReader(client=FakeClient([slide()]), cache_dir=cache).read(paths)
    stored = json.loads((cache / f"{frame_key(paths[0])}.json").read_text())
    assert stored["fights"][0]["cappers_for_a"] == ["Artem MMA"]


# -- pipeline wiring -------------------------------------------------------


def test_slides_carry_the_roundup_when_the_transcript_says_nothing(tmp_path, monkeypatch):
    """The normal case for these decks: captions name nobody, slides name
    everyone. A failed transcript must not skip the slides."""
    import json as _json

    from mma_engine import pipeline
    from mma_engine.config import load_config
    from mma_engine.roundup_slides import SlideReport

    config_path = tmp_path / "config.json"
    config_path.write_text(
        _json.dumps(
            {
                "event": {"name": "UFC 300"},
                "cappers": [{"id": "artem_mma", "name": "Artem MMA", "trust": {"overall": 6.2}}],
            }
        ),
        encoding="utf-8",
    )
    config = load_config(config_path)
    monkeypatch.chdir(tmp_path)  # roundups/ is written relative to the cwd

    class DeadFetcher:
        def fetch(self, video_id):
            return type("T", (), {"ok": False, "error": "no captions", "text": ""})()

    monkeypatch.setattr(
        pipeline,
        "read_roundup_slides",
        lambda *a, **k: SlideReport(
            roundups=[slide(for_a=("Artem MMA", "Some New Channel"), for_b=("BetSam",))],
            frames=12, read=12,
        ),
    )

    picks: list = []
    sources: list[dict] = []
    pipeline.ingest_tracker_roundups(
        config,
        ["https://youtu.be/VIDEOIDXX11"],
        fetcher=DeadFetcher(),
        sourced_picks=picks,
        sources=sources,
        apply_cappers=False,
    )

    assert {p.capper.name for p in picks} == {"Artem MMA", "Some New Channel", "BetSam"}
    assert all(p.source_kind == "tracker" for p in picks)
    # The configured capper keeps their real trust; the two unknown channels
    # come in at neutral.
    assert next(p for p in picks if p.capper.id == "artem_mma").capper.trust_for("overall") == 6.2
    (record,) = sources
    assert (record["status"], record["pick_count"], record["slides_read"]) == ("ok", 3, 12)


def test_captured_slides_alone_are_a_roundup(tmp_path, monkeypatch):
    """--roundup-slides with no video URL: the deck is the source."""
    import json as _json

    from mma_engine import pipeline
    from mma_engine.config import load_config
    from mma_engine.roundup_slides import SlideReport

    config_path = tmp_path / "config.json"
    config_path.write_text(
        _json.dumps({"event": {}, "cappers": [{"id": "x", "name": "X"}]}), encoding="utf-8"
    )
    seen = {}

    def fake_read(config, url, video_id, slides_dir=None):
        seen.update(url=url, video_id=video_id, slides_dir=slides_dir)
        return SlideReport(roundups=[slide()], frames=3, read=3)

    monkeypatch.setattr(pipeline, "read_roundup_slides", fake_read)
    monkeypatch.chdir(tmp_path)
    picks: list = []
    sources: list[dict] = []
    pipeline.ingest_tracker_roundups(
        load_config(config_path),
        [],
        fetcher=None,
        sourced_picks=picks,
        sources=sources,
        slides_dir=tmp_path / "shots",
    )
    assert seen == {"url": "", "video_id": "captured_slides", "slides_dir": tmp_path / "shots"}
    assert len(picks) == 2
    assert sources[0]["video_id"] == "captured_slides"
