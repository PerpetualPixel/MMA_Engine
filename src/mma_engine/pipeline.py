"""Pipeline orchestration and CLI entry point.

    python -m mma_engine --config config.json --output docs/data.json

Each stage is independently importable (`transcripts`, `extract`, `aggregate`),
so the GitHub Action just calls this module and commits the output file.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .aggregate import SourcedPick, build_consensus
from .auto_event import resolve_auto_event
from .config import (
    Capper,
    Config,
    ConfigError,
    ScreenVideoRef,
    VideoRef,
    extract_video_id,
    load_config,
    local_video_id,
)
from .discover import ChannelDiscovery, DiscoveredVideo
from .event_card import annotate_consensus, fetch_event_cards
from .normalize import fight_key, surname, surnames_match as _surnames_match
from .odds import annotate_odds, fetch_live_odds
from .pasted_picks import (
    PastedNote,
    collect_notes,
    has_notes,
    note_id,
    parse_note,
    supersede_video_picks,
)
from .extract import PickExtractor
from .proxy import ProxyConfigError, build_proxy_config, build_requests_proxies
from .roster import (
    NEUTRAL_TRUST,
    RosterExtractor,
    build_capper_entry,
    merge_into_config,
    name_key,
    slugify,
)
from .roundup_slides import (
    SlideReader,
    SlideReport,
    download_video,
    extract_slides,
    read_directory,
)
from .screen_picks import (
    ScreenReader,
    ScreenReading,
    ScreenReport,
    boards_as_roundup,
    extract_frames,
    fetch_video_info,
    load_reading as load_screen_reading,
    save_reading as save_screen_reading,
    unique_frames,
)
from .tracker_picks import (
    CapperDirectory,
    RoundupExtractor,
    RoundupResult,
    load_readings,
    save_reading,
    TrackerRoundup,
    merge_new_cappers,
    merge_roundups,
    to_sourced_picks,
)
from .transcripts import TranscriptFetcher, build_cookie_config

log = logging.getLogger("mma_engine")


def resolve_videos(config: Config) -> tuple[list[VideoRef], list[dict]]:
    """Combine explicitly configured videos with channel-discovered ones.

    Explicit entries in `config.json` always win: if the same video is both
    listed by hand and found via discovery, the hand-listed entry is kept (it
    may carry a title or a deliberate capper attribution).
    """
    videos = list(config.videos)
    discovery_settings = config.settings["discovery"]
    if not discovery_settings.get("enabled"):
        return videos, []

    cappers = config.discoverable_cappers
    if not cappers:
        log.warning("Discovery is enabled but no capper has a channel configured.")
        return videos, []

    api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if api_key:
        log.info(
            "Discovering recent uploads for %d channel(s) via the YouTube Data API",
            len(cappers),
        )
    else:
        log.warning(
            "YOUTUBE_API_KEY is not set — falling back to YouTube's RSS feeds, "
            "which appear to be discontinued (404 for every channel as of Aug "
            "2026). Get a free key: see README.md \"Channel discovery\"."
        )
        log.info("Discovering recent uploads for %d channel(s)", len(cappers))
    discovery = ChannelDiscovery(
        lookback_days=int(discovery_settings["lookback_days"]),
        max_per_channel=int(discovery_settings["max_videos_per_channel"]),
        title_contains=discovery_settings.get("title_contains", ""),
        use_cache=bool(config.settings["use_cache"]),
        proxies=build_requests_proxies(config.settings),
        api_key=api_key,
    )
    discovered, report = discovery.discover(cappers)

    # Then anyone else on YouTube who covered this event.
    search_settings = discovery_settings["search"]
    if search_settings["enabled"] and search_settings["queries"]:
        log.info(
            "Searching YouTube for this event's predictions (%d quer%s)",
            len(search_settings["queries"]),
            "y" if len(search_settings["queries"]) == 1 else "ies",
        )
        searched, search_report = discovery.search(
            list(search_settings["queries"]),
            max_results=int(search_settings["max_results"]),
            max_per_channel=int(search_settings["max_per_channel"]),
            min_duration_seconds=int(search_settings["min_duration_seconds"]),
            require_prediction_terms=bool(search_settings["require_prediction_terms"]),
        )
        report.extend(search_report)
        known = {v.capper_id for v in discovered}
        for video in searched:
            capper = capper_for_channel(config, video)
            discovered.append(
                DiscoveredVideo(
                    video_id=video.video_id,
                    capper_id=capper.id,
                    url=video.url,
                    title=video.title,
                    published=video.published,
                    channel_id=video.channel_id,
                    channel_title=video.channel_title,
                )
            )
        minted = sum(1 for v in searched if v.channel_id and v.channel_id not in known)
        log.info(
            "  search added %d video(s) from channels not in config.json", minted
        )

    seen = {video.video_id for video in videos}
    for item in discovered:
        if item.video_id in seen:
            continue
        seen.add(item.video_id)
        videos.append(
            VideoRef(
                video_id=item.video_id,
                capper_id=item.capper_id,
                url=item.url,
                title=item.title,
            )
        )
    log.info("Discovery added %d video(s); %d total to process", len(videos) - len(config.videos), len(videos))
    return videos, report


def capper_for_channel(config: Config, video: DiscoveredVideo) -> Capper:
    """The capper a searched-up video belongs to, minting one if need be.

    Open search finds whoever posted, which is the point of it — most of them
    have no `config.json` entry and no tracked record. Those are minted at
    neutral trust and count as one unweighted voice each, the same treatment a
    channel first seen in a roundup gets. A channel that *is* configured keeps
    its earned trust, matched on its channel id first (exact, and immune to a
    renamed channel) and then on its name or aliases.
    """
    channel_id = (video.channel_id or "").strip()
    if channel_id:
        for capper in config.cappers.values():
            if capper.channel_id == channel_id or channel_id in (capper.channel_url or ""):
                return capper

    wanted = name_key(video.channel_title)
    if wanted:
        for capper in config.cappers.values():
            for spelling in (capper.name, *capper.aliases):
                if name_key(spelling) == wanted:
                    return capper

    base = f"yt_{slugify(video.channel_title or channel_id or 'channel')}"
    capper_id, suffix = base, 2
    while capper_id in config.cappers:
        capper_id, suffix = f"{base}_{suffix}", suffix + 1
    minted = Capper(
        id=capper_id,
        name=video.channel_title or channel_id or "Unknown channel",
        channel_url=f"https://www.youtube.com/channel/{channel_id}" if channel_id else "",
        channel_id=channel_id,
        # Found by search this once; sweeping their uploads every week is a
        # decision for a person to make, not a side effect of one video.
        discover=False,
        trust={"overall": NEUTRAL_TRUST},
    )
    config.cappers[capper_id] = minted
    return minted


def ytdlp_extra_args(config: Config) -> list[str]:
    """The yt-dlp flags every video download here should carry: your cookies.

    The same `settings.transcript_cookies` the age-restricted caption
    fallback uses. Signed in, YouTube serves the download through the
    clients it gives a real viewer, which is the difference between a 403
    and a video on more days than not. Empty when unconfigured or when the
    cookie file is missing, in which case the download simply goes
    anonymous as before.
    """
    cookies = build_cookie_config(config.settings)
    if cookies is None:
        return []
    missing = cookies.missing_file()
    if missing:
        log.warning("  cookie file %s not found — downloading without cookies", missing)
        return []
    return cookies.ytdlp_args()


def read_video_screens(
    config: Config,
    url: str,
    video_id: str,
    title: str = "",
    channel: str = "",
    frames_dir: Path | None = None,
    video_file: Path | None = None,
) -> ScreenReport:
    """Every unique frame of a video, read for the picks printed on it.

    Frames come from the video itself — downloaded, or a file already on
    disk — cut at every scene change and at least every few seconds, then
    de-duplicated by perceptual hash so each screenshot is read once; or
    from a folder of screenshots captured by hand when neither will do.
    """
    settings = config.settings["screen_picks"]
    cache_root = Path("cache")

    if frames_dir is not None:
        frames = read_directory(frames_dir)
        if not frames:
            log.warning("  no screenshots found in %s", frames_dir)
            return ScreenReport(error=f"no images in {frames_dir}")
        log.info("  %d screenshot(s) from %s", len(frames), frames_dir)
    else:
        if video_file is not None:
            if not video_file.is_file():
                log.warning("  no such video file: %s", video_file)
                return ScreenReport(error=f"no such file {video_file}")
            log.info("  using the video file %s", video_file)
            video = video_file
        else:
            proxies = build_requests_proxies(config.settings)
            video = download_video(
                url,
                cache_root / "screen_video",
                video_id=video_id,
                height=int(settings["video_height"]),
                proxy=proxies.get("https", "") if proxies else "",
                extra_args=ytdlp_extra_args(config),
            )
            if video is None:
                return ScreenReport(error="video download failed")
        frames = extract_frames(
            video,
            cache_root / "screens" / "frames" / video_id,
            scene_threshold=float(settings["scene_threshold"]),
            sample_seconds=float(settings["sample_seconds"]),
            max_frames=int(settings["max_frames"]),
        )
        # Only a download of ours is disposable; a file the user handed over
        # is theirs.
        if video_file is None and not bool(settings["keep_video"]):
            video.unlink(missing_ok=True)
        if not frames:
            return ScreenReport(error="no frames extracted")
        log.info("  %d frame(s) cut from the video", len(frames))

    total = len(frames)
    frames = unique_frames(frames, max_distance=int(settings["max_distance"]))
    log.info("  %d unique screenshot(s) after dropping %d near-duplicate(s)", len(frames), total - len(frames))

    reader = ScreenReader(
        model=str(settings["model"]),
        effort=str(settings["effort"]),
        cache_dir=cache_root / "screens",
        use_cache=bool(config.settings["use_cache"]),
    )
    report = reader.read(frames, title=title, channel=channel)
    log.info(
        "  read %d screenshot(s) (%d from cache, %d failed): %d pick(s), %d board(s)",
        report.read + report.cached, report.cached, report.failed,
        len(report.picks), len(report.boards),
    )
    return report


def capper_for_screen_video(
    config: Config, video_id: str, channel_id: str, channel_title: str, title: str = ""
) -> Capper:
    """Whose picks a screen-read video carries: the channel that posted it.

    Same matching as open search — channel id first, then name or alias,
    else minted at neutral trust. A video whose metadata could not be read
    at all gets a capper named for the video, so its picks still count as
    one unweighted voice rather than vanishing; pin it with --video-capper
    or a capper_id in config.json's screen_videos to do better.
    """
    if channel_id or channel_title:
        return capper_for_channel(
            config,
            DiscoveredVideo(
                video_id=video_id,
                capper_id="",
                url="",
                title="",
                published=datetime.now(timezone.utc),
                channel_id=channel_id,
                channel_title=channel_title,
            ),
        )
    capper_id = f"yt_video_{slugify(video_id)}"
    if capper_id in config.cappers:
        return config.cappers[capper_id]
    minted = Capper(
        id=capper_id,
        name=f"Video: {title}" if title else f"YouTube video {video_id}",
        discover=False,
        trust={"overall": NEUTRAL_TRUST},
    )
    config.cappers[capper_id] = minted
    return minted


def ingest_screen_videos(
    config: Config,
    videos: list[ScreenVideoRef],
    sourced_picks: list[SourcedPick],
    sources: list[dict[str, Any]],
    skip_extraction: bool = False,
) -> str:
    """Add the picks printed on each listed video's screen, in place.

    A capper's on-screen picks are their own picks — full confidence where
    the graphic states one, real odds, tagged `screens` so the dashboard can
    say where they came from. A tracker-style board found on screen is
    counted as a roundup instead: one neutral vote per channel. Returns the
    event name a reading states, if any. Fails open throughout.
    """
    settings = config.settings["screen_picks"]
    if not settings["enabled"] or not videos:
        return ""

    readings_dir = Path(settings["readings_dir"])
    proxies = build_requests_proxies(config.settings)
    proxy = proxies.get("https", "") if proxies else ""
    directory = CapperDirectory(config.cappers.values())
    event_name = ""

    for ref in videos:
        video_id = ref.video_id
        frames_dir = Path(ref.frames_dir) if ref.frames_dir else None
        video_file = Path(ref.video_file) if ref.video_file else None
        log.info("Screen picks — %s", video_id)
        record: dict[str, Any] = {
            "video_id": video_id,
            "url": ref.url,
            "capper_id": ref.capper_id,
            "capper": "",
            "title": "",
            "kind": "screens",
            "status": "ok",
            "pick_count": 0,
        }

        reading = load_screen_reading(readings_dir, video_id)
        if reading is not None:
            log.info("  reading from %s/%s.json — no download, no API call", readings_dir, video_id)
            record["from_reading"] = True
        else:
            if skip_extraction:
                record["status"] = "extraction_skipped"
                sources.append(record)
                continue
            info = (
                fetch_video_info(ref.url, proxy=proxy, extra_args=ytdlp_extra_args(config))
                if ref.url
                else None
            )
            title = info.title if info else (video_file.stem if video_file else "")
            channel = info.channel if info else ""
            channel_id = info.channel_id if info else ""
            if info is None and ref.url:
                log.warning("  could not read the video's title/channel — attributing by id")
            report = read_video_screens(
                config, ref.url, video_id, title=title, channel=channel,
                frames_dir=frames_dir, video_file=video_file,
            )
            record.update(
                frames=report.frames, frames_read=report.read + report.cached
            )
            if report.error:
                record["error"] = report.error
            if not report.picks and not report.boards:
                record["status"] = "no_picks_on_screen" if report.frames else "no_frames"
                record["capper"] = channel or f"YouTube video {video_id}"
                record["title"] = title
                sources.append(record)
                log.warning("  nothing readable came off the screen (%s)", report.error or "no picks printed")
                continue
            reading = ScreenReading(
                video_id=video_id,
                source_url=ref.url,
                title=title,
                channel=channel,
                channel_id=channel_id,
                event_name=report.event_name,
                picks=report.picks,
                boards=report.boards,
            )
            if not report.error:
                # Only a complete reading is worth keeping: a partial one
                # (the API stopped part-way) should be finished next run.
                save_screen_reading(readings_dir, reading)

        # The capper's own picks, attributed to whoever posted the video.
        capper = (
            config.capper(ref.capper_id)
            if ref.capper_id
            else capper_for_screen_video(
                config, video_id, reading.channel_id, reading.channel, title=reading.title
            )
        )
        record.update(capper_id=capper.id, capper=capper.name, title=reading.title)
        for pick in reading.picks:
            sourced_picks.append(
                SourcedPick(
                    pick=pick,
                    capper=capper,
                    video_id=video_id,
                    video_url=ref.url,
                    source_kind="screens",
                )
            )
        record["pick_count"] = len(reading.picks)
        event_name = event_name or reading.event_name

        # Boards on screen are a roundup, counted as one: one neutral vote
        # per channel, deferring to any capper already covered this run.
        if reading.boards:
            covered = frozenset(
                (s.capper.id, fight_key(s.pick.fighter_a, s.pick.fighter_b))
                for s in sourced_picks
            )
            fights = merge_roundups([boards_as_roundup(reading.boards, reading.event_name)])
            picks, stats = to_sourced_picks(
                fights,
                directory,
                video_id=video_id,
                video_url=ref.url,
                confidence=int(config.settings["tracker_picks"]["confidence"]),
                already_covered=covered,
            )
            sourced_picks.extend(picks)
            record.update(
                board_fights=len(fights),
                board_picks=stats.picks,
                capper_count=stats.cappers,
                new_cappers=stats.minted,
                superseded=stats.superseded,
            )
            record["pick_count"] += stats.picks

        sources.append(record)
        log.info(
            "  %d pick(s) for %s%s",
            len(reading.picks), capper.name,
            f", plus {record.get('board_picks', 0)} roundup vote(s) from {record.get('board_fights', 0)} board(s)"
            if reading.boards else "",
        )
    return event_name


def remember_screen_videos(config_path: Path, videos: list[ScreenVideoRef]) -> list[str]:
    """Append videos to `config.json`'s screen_videos so every later run reads
    them too (free, from `screens/`). Returns the ids actually added."""
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    existing = raw.setdefault("screen_videos", [])
    known: set[str] = set()
    for entry in existing:
        source = entry if isinstance(entry, str) else (entry.get("url") or entry.get("video_id") or "")
        if not source and isinstance(entry, dict) and entry.get("video_file"):
            known.add(local_video_id(entry["video_file"]))
            continue
        try:
            known.add(extract_video_id(source))
        except ConfigError:
            continue
    added: list[str] = []
    for ref in videos:
        if ref.video_id in known or not (ref.url or ref.video_file):
            continue
        entry: dict[str, Any] = {}
        if ref.url:
            entry["url"] = ref.url
        if ref.video_file:
            # The file stays where it is; once read, screens/<id>.json is
            # what later runs actually use, so the path only matters until then.
            entry["video_file"] = ref.video_file
        if ref.capper_id:
            entry["capper_id"] = ref.capper_id
        existing.append(entry)
        known.add(ref.video_id)
        added.append(ref.video_id)
    if added:
        config_path.write_text(
            json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return added


def ingest_pasted_picks(
    config: Config,
    sourced_picks: list[SourcedPick],
    sources: list[dict[str, Any]],
    extra_notes: list[tuple[str, Path]] | None = None,
    skip_extraction: bool = False,
) -> None:
    """Read `pasted/` — cards pasted by hand from paywalled posts — in place.

    Runs after the videos so a pasted card can supersede the same capper's
    teaser video, and before the roundup so the roundup defers to both.
    """
    settings = config.settings["pasted_picks"]
    if not settings["enabled"] and not extra_notes:
        return

    directory = Path(settings["dir"])
    notes, skipped = collect_notes(
        directory,
        config.cappers.values(),
        max_age_days=int(settings["max_age_days"]),
    ) if settings["enabled"] else ([], [])

    # --picks-from-text CAPPER_ID=FILE: a one-off paste that doesn't live in
    # the folder, and isn't subject to its staleness guard.
    for capper_id, path in extra_notes or []:
        try:
            capper = config.capper(capper_id)
        except ConfigError as exc:
            log.error("%s", exc)
            continue
        if not path.is_file():
            log.error("No such pasted picks file: %s", path)
            continue
        text, source_url = parse_note(path.read_text(encoding="utf-8", errors="replace"))
        if not text:
            log.warning("%s is empty — nothing to extract", path)
            continue
        notes.append(
            PastedNote(
                capper=capper,
                path=path,
                text=text,
                source_url=source_url,
                paste_id=note_id(capper.id, text),
            )
        )

    for row in skipped:
        sources.append(
            {
                "video_id": "",
                "url": row["path"],
                "capper_id": "",
                "capper": f"Pasted: {row['file']}",
                "title": "",
                "kind": "pasted",
                "status": row["status"],
                "pick_count": 0,
            }
        )
    if not notes:
        return

    extractor = (
        None
        if skip_extraction
        else PickExtractor(
            model=config.settings["model"],
            effort=config.settings["effort"],
            max_tokens=int(config.settings["max_tokens"]),
            max_chunk_chars=int(config.settings["max_transcript_chars"]),
            use_cache=bool(config.settings["use_cache"]),
        )
    )

    added: list[SourcedPick] = []
    for note in notes:
        log.info("Pasted picks — %s (%s)", note.capper.name, note.path.name)
        record: dict[str, Any] = {
            "video_id": note.paste_id,
            "url": note.source_url,
            "capper_id": note.capper.id,
            "capper": note.capper.name,
            "title": note.path.name,
            "kind": "pasted",
            "status": "ok",
            "pick_count": 0,
            "transcript_chars": len(note.text),
        }
        if extractor is None:
            record["status"] = "extraction_skipped"
            sources.append(record)
            continue

        extraction = extractor.extract(
            video_id=note.paste_id,
            transcript=note.text,
            capper_name=note.capper.name,
            video_url=note.source_url,
        )
        if not extraction.ok:
            record.update(status="extraction_failed", error=extraction.error)
            log.warning("  extraction failed: %s", extraction.error)
            sources.append(record)
            continue

        record["pick_count"] = len(extraction.picks)
        sources.append(record)
        log.info("  %d picks", len(extraction.picks))
        for pick in extraction.picks:
            added.append(
                SourcedPick(
                    pick=pick,
                    capper=note.capper,
                    video_id=note.paste_id,
                    video_url=note.source_url,
                    source_kind="pasted",
                )
            )

    if not added:
        return
    kept, dropped = supersede_video_picks(sourced_picks, added)
    if dropped:
        log.info(
            "Dropped %d video pick(s) the pasted card(s) supersede — the paste "
            "is the full card, the video was the teaser", dropped,
        )
    sourced_picks[:] = kept + added


def read_roundup_slides(
    config: Config,
    url: str,
    video_id: str,
    slides_dir: Path | None = None,
) -> SlideReport:
    """The picks printed on a roundup's slides, read with vision.

    The channel names in these decks are on-screen text the host never says
    aloud, so this — not the transcript — is where the roundup's attributions
    actually live. Frames come from the video itself, or from screenshots
    captured by hand when the download won't work.
    """
    settings = config.settings["tracker_picks"]
    cache_root = Path("cache")

    if slides_dir is not None:
        frames = read_directory(slides_dir)
        if not frames:
            log.warning("  no slide images found in %s", slides_dir)
            return SlideReport(error=f"no images in {slides_dir}")
        log.info("  %d captured slide(s) from %s", len(frames), slides_dir)
    else:
        proxies = build_requests_proxies(config.settings)
        video = download_video(
            url,
            cache_root / "roundup_video",
            video_id=video_id,
            height=int(settings["video_height"]),
            proxy=proxies.get("https", "") if proxies else "",
            extra_args=ytdlp_extra_args(config),
        )
        if video is None:
            return SlideReport(error="video download failed")
        frames = extract_slides(
            video,
            cache_root / "slides" / "frames" / video_id,
            scene_threshold=float(settings["scene_threshold"]),
            max_frames=int(settings["max_frames"]),
        )
        if not bool(settings["keep_video"]):
            video.unlink(missing_ok=True)
        if not frames:
            return SlideReport(error="no frames extracted")
        log.info("  %d slide(s) cut from the video", len(frames))

    reader = SlideReader(
        model=str(settings["slide_model"]),
        effort=str(settings["slide_effort"]),
        cache_dir=cache_root / "slides",
        use_cache=bool(config.settings["use_cache"]),
    )
    report = reader.read(frames)
    log.info(
        "  read %d slide(s) (%d from cache, %d failed)",
        report.read + report.cached, report.cached, report.failed,
    )
    return report


def ingest_tracker_roundups(
    config: Config,
    urls: list[str],
    fetcher: TranscriptFetcher,
    sourced_picks: list[SourcedPick],
    sources: list[dict[str, Any]],
    apply_cappers: bool = False,
    skip_extraction: bool = False,
    slides_dir: Path | None = None,
    board_odds: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Add every channel's pick from the tracker's roundup video(s), in place.

    Two readings of the same deck, merged: the transcript (which rarely names
    a channel — the host says "eighty of eighty-one are on Dyer" and moves on)
    and the slides themselves, where the names are actually printed. Returns
    the event name the roundup states, if any. Fails open throughout: a
    roundup that can't be read costs the run nothing but its own picks.
    """
    settings = config.settings["tracker_picks"]
    if (not urls and slides_dir is None) or not settings["enabled"]:
        return ""
    read_slides = bool(settings["read_slides"]) and not skip_extraction

    directory = CapperDirectory(config.cappers.values())
    # One channel is one vote per fight, cast by the richest source: a capper
    # whose own video (or pasted card) this run already covers a fight doesn't
    # also get counted off the roundup slide.
    covered = frozenset(
        (s.capper.id, fight_key(s.pick.fighter_a, s.pick.fighter_b))
        for s in sourced_picks
    )
    extractor = (
        None
        if skip_extraction
        else RoundupExtractor(
            model=config.settings["model"],
            effort=config.settings["effort"],
            max_tokens=int(config.settings["max_tokens"]),
            max_chunk_chars=int(settings["max_chunk_chars"]),
            use_cache=bool(config.settings["use_cache"]),
        )
    )

    event_name = ""
    # Captured slides with no roundup URL configured are a roundup in their
    # own right: the deck is the source, the video was only ever how we got
    # at it.
    # Boards already transcribed into roundups/ — free, and the only source
    # that works with no video download and no API call at all.
    readings_dir = Path(settings["readings_dir"])
    readings = load_readings(readings_dir)
    entries = list(urls)
    if not entries and (slides_dir is not None or readings):
        entries = [""]
    for url in entries:
        if url:
            try:
                video_id = extract_video_id(url)
            except ConfigError as exc:
                log.warning("Skipping tracker roundup: %s", exc)
                continue
        else:
            video_id = "captured_slides"

        log.info("Tracker roundup — %s", video_id)
        record: dict[str, Any] = {
            "video_id": video_id,
            "url": url,
            "capper_id": "",
            "capper": "Predictions tracker roundup",
            "title": "",
            "kind": "tracker_roundup",
            "status": "ok",
            "pick_count": 0,
        }

        collected: list[TrackerRoundup] = []
        event_from_video = ""

        transcribed = readings.pop(video_id, None)
        if transcribed is None and not url and readings:
            # Slides-only run: take whatever boards are on disk.
            for stored_id, stored in list(readings.items()):
                readings.pop(stored_id)
                collected.append(stored[1])
                record["reading_fights"] = record.get("reading_fights", 0) + len(
                    stored[1].fights
                )
        elif transcribed is not None:
            collected.append(transcribed[1])
            record["reading_fights"] = len(transcribed[1].fights)

        # The transcript: cheap, occasionally carries a name the host reads
        # out, and never the whole board. Its failure is not this roundup's
        # failure — the slides are the real source.
        if url:
            transcript = fetcher.fetch(video_id)
            if not transcript.ok:
                record.update(status="transcript_failed", error=transcript.error)
                log.warning("  transcript failed: %s", transcript.error)
            elif extractor is None:
                record["status"] = "extraction_skipped"
            else:
                record["transcript_chars"] = transcript.char_count
                spoken = extractor.extract(video_id, transcript.text, url)
                if spoken.ok:
                    event_from_video = spoken.event_name
                    collected.append(
                        TrackerRoundup(
                            event_name=spoken.event_name, fights=spoken.fights
                        )
                    )
                    if spoken.error:
                        # Partial: some chunks came back, then the API stopped.
                        record["error"] = spoken.error
                else:
                    record["error"] = spoken.error
                    log.warning("  transcript extraction failed: %s", spoken.error)

        # The slides, where the channel names are actually printed — unless a
        # transcribed board already covers this deck, in which case there is
        # nothing to pay a vision pass for.
        if read_slides and transcribed is None:
            slides = read_roundup_slides(config, url, video_id, slides_dir=slides_dir)
            collected.extend(slides.roundups)
            record.update(
                slides_read=slides.read + slides.cached, slide_frames=slides.frames
            )
            if slides.error:
                record["slides_error"] = slides.error

        if not collected:
            record["status"] = record.get("status") or "no_sources"
            if record["status"] == "ok":
                record["status"] = "empty"
            sources.append(record)
            continue

        record["status"] = "ok"
        fights = merge_roundups(collected)
        if transcribed is None and fights:
            # Keep what the slides cost money to read.
            save_reading(readings_dir, video_id, url, fights, event_from_video)
        result = RoundupResult(
            video_id=video_id, fights=fights, event_name=event_from_video
        )

        picks, stats = to_sourced_picks(
            result.fights,
            directory,
            video_id=video_id,
            video_url=url,
            confidence=int(settings["confidence"]),
            already_covered=covered,
        )
        sourced_picks.extend(picks)
        # The boards print prices as well as names, and for method bets they
        # are the only prices the engine has — the live feed is moneyline
        # only. Collected per fight so run_pipeline can stamp them on the
        # payload the same way live odds are stamped on.
        if board_odds is not None:
            for board in result.fights:
                printed = {
                    side: board.odds(side).model_dump(exclude_defaults=True)
                    for side in ("a", "b")
                }
                if not any(printed.values()):
                    continue
                board_odds[fight_key(board.fighter_a, board.fighter_b)] = {
                    "fighter_a": board.fighter_a,
                    "fighter_b": board.fighter_b,
                    **printed,
                    "source": "tracker board",
                    "video_id": video_id,
                }
        event_name = event_name or result.event_name
        record.update(
            pick_count=stats.picks,
            capper_count=stats.cappers,
            new_cappers=stats.minted,
            superseded=stats.superseded,
            fights=len(result.fights),
        )
        sources.append(record)
        log.info(
            "  %d picks from %d channels (%d already in config, %d new); "
            "%d deferred to the capper's own video or pasted card",
            stats.picks, stats.cappers, stats.matched, stats.minted, stats.superseded,
        )

    if apply_cappers and directory.minted:
        added = merge_new_cappers(config.path, directory.minted)
        log.info("Added %d roundup channel(s) to %s", len(added), config.path)
    return event_name


def run_pipeline(
    config: Config,
    output_path: Path,
    skip_extraction: bool = False,
    videos: list[VideoRef] | None = None,
    discovery_report: list[dict] | None = None,
    roundup_urls: list[str] | None = None,
    apply_tracker_cappers: bool = False,
    pasted_notes: list[tuple[str, Path]] | None = None,
    slides_dir: Path | None = None,
    screen_videos: list[ScreenVideoRef] | None = None,
) -> dict[str, Any]:
    """Fetch transcripts, extract picks, aggregate, and write the payload."""
    settings = config.settings
    videos = config.videos if videos is None else videos

    fetcher = TranscriptFetcher(
        languages=settings["transcript_languages"],
        min_delay=float(settings["min_delay_seconds"]),
        max_delay=float(settings["max_delay_seconds"]),
        use_cache=bool(settings["use_cache"]),
        proxy_config=build_proxy_config(settings),
        cookie_config=build_cookie_config(settings),
    )
    extractor = (
        None
        if skip_extraction
        else PickExtractor(
            model=settings["model"],
            effort=settings["effort"],
            max_tokens=int(settings["max_tokens"]),
            max_chunk_chars=int(settings["max_transcript_chars"]),
            use_cache=bool(settings["use_cache"]),
        )
    )

    sourced_picks: list[SourcedPick] = []
    sources: list[dict[str, Any]] = []
    event_name = config.event.get("name", "")

    for index, video in enumerate(videos, start=1):
        capper = config.capper(video.capper_id)
        log.info("[%d/%d] %s — %s", index, len(videos), capper.name, video.video_id)

        record: dict[str, Any] = {
            "video_id": video.video_id,
            "url": video.url,
            "capper_id": capper.id,
            "capper": capper.name,
            "title": video.title,
            "status": "ok",
            "pick_count": 0,
        }

        transcript = fetcher.fetch(video.video_id)
        if not transcript.ok:
            record.update(status="transcript_failed", error=transcript.error)
            log.warning("  transcript failed: %s", transcript.error)
            sources.append(record)
            continue
        record["transcript_chars"] = transcript.char_count

        if extractor is None:
            record["status"] = "extraction_skipped"
            sources.append(record)
            continue

        extraction = extractor.extract(
            video_id=video.video_id,
            transcript=transcript.text,
            capper_name=capper.name,
            video_url=video.url,
        )
        if not extraction.ok:
            record.update(status="extraction_failed", error=extraction.error)
            log.warning("  extraction failed: %s", extraction.error)
            sources.append(record)
            continue

        event_name = event_name or extraction.event_name
        record["pick_count"] = len(extraction.picks)
        sources.append(record)

        for pick in extraction.picks:
            sourced_picks.append(
                SourcedPick(
                    pick=pick,
                    capper=capper,
                    video_id=video.video_id,
                    video_url=video.url,
                )
            )

    # Videos read off their screen — a URL pasted in, every unique frame
    # read for the picks printed on it. These are the capper's own picks,
    # so they land before the pasted cards (which supersede them) and the
    # roundup (which defers to them).
    screen_event = ingest_screen_videos(
        config,
        config.screen_videos if screen_videos is None else screen_videos,
        sourced_picks=sourced_picks,
        sources=sources,
        skip_extraction=skip_extraction,
    )
    event_name = event_name or screen_event

    # Cards pasted by hand from paywalled posts, for cappers whose YouTube
    # upload is only a teaser these days.
    ingest_pasted_picks(
        config,
        sourced_picks=sourced_picks,
        sources=sources,
        extra_notes=pasted_notes,
        skip_extraction=skip_extraction,
    )

    # The tracker's pre-event roundup: one video carrying every channel's pick,
    # including the many channels this pipeline can't read a video for. Runs
    # last so a capper's own picks are already in hand and their roundup entry
    # for the same fight can defer to them.
    board_odds: dict[str, dict[str, Any]] = {}
    roundup_event = ingest_tracker_roundups(
        config,
        config.tracker_picks_videos if roundup_urls is None else roundup_urls,
        fetcher=fetcher,
        sourced_picks=sourced_picks,
        sources=sources,
        apply_cappers=apply_tracker_cappers,
        skip_extraction=skip_extraction,
        slides_dir=slides_dir,
        board_odds=board_odds,
    )
    event_name = event_name or roundup_event

    event = {**config.event, "name": event_name}
    payload = build_consensus(
        sourced_picks,
        event=event,
        sources=sources,
        min_confidence=int(settings["min_confidence"]),
    )
    if discovery_report:
        payload["discovery"] = discovery_report

    # Pin the consensus to the event's official card (ESPN): anything not on
    # the card is dropped, cancelled bouts get flagged rather than silently
    # vanishing, and garbled fighter spellings are corrected.
    # The previous run's payload is what detects a quiet cancellation — a
    # bout ESPN removes from the card outright was on_card last run and
    # unmatched now. Fail-open: no card, no annotation, pipeline continues.
    previous: dict[str, Any] = {}
    if output_path.is_file():
        try:
            previous = json.loads(output_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    specs = config.event_specs or [{"name": event.get("name") or "", "league": "", "label": ""}]
    cards = fetch_event_cards(specs)
    if not cards:
        log.warning(
            "No ESPN card found for %s — consensus left un-annotated, so nothing "
            "is filtered this run",
            " / ".join(spec["name"] for spec in specs) or "(unnamed event)",
        )
    annotate_consensus(
        payload,
        cards,
        previous_fights=previous.get("fights") or [],
        # Which card that payload was built for — the carry-over is only valid
        # inside one event, and this is what says so.
        previous_card_name=((previous.get("event") or {}).get("card") or {}).get(
            "name", ""
        ),
    )

    # Current moneyline prices, so the dashboard can price a parlay rather
    # than only rank it. Runs after the card annotation so it only ever
    # prices this event's bouts, and fails open the same way: no key, no
    # network, or a spent quota simply means no prices this run.
    odds_settings = settings["live_odds"]
    if odds_settings["enabled"]:
        priced = annotate_odds(
            payload,
            fetch_live_odds(
                os.environ.get("ODDS_API_KEY", "").strip(),
                regions=str(odds_settings["regions"]),
            ),
        )
        if priced:
            log.info("Live moneylines attached to %d bouts", priced)

    # The board's own prices, per fight. Kept separate from live_odds rather
    # than merged into it: these are the tracker's snapshot from whenever the
    # deck was built, and the dashboard labels them as such. They are the only
    # prices the engine has for method bets, which the live feed doesn't carry.
    if board_odds:
        stamped = 0
        for fight in payload.get("fights") or []:
            board = board_odds.get(fight_key(fight["fighter_a"], fight["fighter_b"]))
            if not board:
                continue
            # The card annotation may have corrected a spelling, and with it
            # which fighter is "a"; match on surname rather than position.
            flip = not _surnames_match(surname(fight["fighter_a"]), surname(board["fighter_a"]))
            fight["board_odds"] = {
                "source": board["source"],
                "video_id": board["video_id"],
                "a": board["b" if flip else "a"],
                "b": board["a" if flip else "b"],
            }
            stamped += 1
        log.info("Board prices attached to %d bout(s)", stamped)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    log.info("Wrote %s", output_path)
    return payload


def run_roster(
    config: Config,
    video_url: str,
    mode: str,
    apply_changes: bool,
    proposal_path: Path,
) -> int:
    """Extract a capper roster from a tracker results video."""
    settings = config.settings
    video_id = extract_video_id(video_url)

    fetcher = TranscriptFetcher(
        languages=settings["transcript_languages"],
        min_delay=float(settings["min_delay_seconds"]),
        max_delay=float(settings["max_delay_seconds"]),
        use_cache=bool(settings["use_cache"]),
        proxy_config=build_proxy_config(settings),
        cookie_config=build_cookie_config(settings),
    )
    transcript = fetcher.fetch(video_id)
    if not transcript.ok:
        log.error("Could not read that video's transcript: %s", transcript.error)
        return 1

    extractor = RosterExtractor(
        model=settings["model"],
        effort=settings["effort"],
        max_tokens=int(settings["max_tokens"]),
    )
    try:
        report = extractor.extract(transcript.text, video_url)
    except Exception as exc:
        log.error("Roster extraction failed: %s: %s", type(exc).__name__, exc)
        return 1

    if not report.cappers:
        log.error(
            "No capper results found in that video. Check it is a tracker results "
            "video rather than a picks video."
        )
        return 1

    entries = [build_capper_entry(capper, video_id) for capper in report.cappers]
    proposal = {
        "source_video": video_url,
        "video_id": video_id,
        "period": report.period,
        "mode": mode,
        "cappers": entries,
    }
    proposal_path.write_text(
        json.dumps(proposal, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    print(f"\nTracked period: {report.period or '(not stated)'}")
    print(f"Cappers found:  {len(entries)}\n")
    print(f"{'Capper':<28}{'overall':>9}{'underdog':>10}{'favorite':>10}")
    print("-" * 57)
    for entry in sorted(entries, key=lambda e: e["trust"]["overall"], reverse=True):
        trust = entry["trust"]
        print(
            f"{entry['name'][:27]:<28}{trust['overall']:>9}"
            f"{trust['underdog']:>10}{trust['favorite']:>10}"
        )
    print(f"\nProposal written to {proposal_path}")

    if not apply_changes:
        print("Review it, then re-run with --apply-roster to merge into config.json.")
        return 0

    result = merge_into_config(config.path, entries, video_id=video_id, mode=mode)
    print(f"\nMerged into {config.path} ({mode} mode):")
    for outcome in ("added", "updated", "skipped"):
        if result[outcome]:
            print(f"  {outcome}: {', '.join(result[outcome])}")
    if result["skipped"]:
        print("  (skipped = this video was already applied to that capper)")
    return 0


def _summarize_discovery(
    config: Config, videos: list[VideoRef], report: list[dict]
) -> str:
    """Human-readable dry run of what discovery found, for `--discover-only`."""
    lines = ["", f"Discovered {len(videos)} video(s) to process:"]
    for video in videos:
        capper = config.capper(video.capper_id)
        lines.append(f"  {capper.name:<24} {video.video_id}  {video.title}")
    searches = [entry for entry in report if "query" in entry]
    if searches:
        lines.append("")
        lines.append("YouTube search:")
        for entry in searches:
            detail = ", ".join(
                f"{key} {value}"
                for key, value in entry.items()
                if key not in ("query", "status")
            )
            lines.append(f"  {entry['query']}: {entry['status']}{' — ' + detail if detail else ''}")
    failures = [entry for entry in report if entry["status"] != "ok"]
    if failures:
        lines.append("")
        lines.append(f"{len(failures)} source(s) could not be read:")
        for entry in failures:
            label = entry.get("capper") or entry.get("query") or "(unknown)"
            lines.append(f"  - {label}: {entry.get('error', entry['status'])}")
    filtered_out = [entry for entry in report if entry.get("recent_titles")]
    if filtered_out:
        lines.append("")
        lines.append(
            "Channels whose recent uploads all failed the filters "
            "(lookback_days / title_contains):"
        )
        for entry in filtered_out:
            lines.append(f"  {entry['capper']}:")
            for title in entry["recent_titles"]:
                lines.append(f"    {title}")
    if not videos:
        lines.append(
            "  (nothing) — widen settings.discovery.lookback_days, adjust "
            "title_contains to match the titles above, or list videos by hand."
        )
    return "\n".join(lines)


def _summarize(payload: dict[str, Any]) -> str:
    totals = payload["totals"]
    lines = [
        "",
        f"Event:   {payload['event'].get('name') or '(unnamed)'}",
        f"Videos:  {totals['videos']} contributing / {len(payload['sources'])} listed",
        f"Cappers: {totals['cappers']}",
        f"Picks:   {totals['picks']} across {totals['fights']} fights",
    ]
    pasted = [
        s
        for s in payload["sources"]
        if s.get("kind") == "pasted" and s["status"] == "ok"
    ]
    if pasted:
        lines.append(
            f"Pasted:  {sum(p.get('pick_count', 0) for p in pasted)} picks hand-fed "
            f"from {len(pasted)} card(s): {', '.join(p['capper'] for p in pasted)}"
        )
    screens = [
        s
        for s in payload["sources"]
        if s.get("kind") == "screens" and s["status"] == "ok"
    ]
    if screens:
        lines.append(
            f"Screens: {sum(p.get('pick_count', 0) for p in screens)} picks read off "
            f"{len(screens)} video(s): {', '.join(p['capper'] for p in screens)}"
        )
    roundups = [
        s
        for s in payload["sources"]
        if s.get("kind") == "tracker_roundup" and s["status"] == "ok"
    ]
    if roundups:
        lines.append(
            f"Roundup: {sum(r.get('capper_count', 0) for r in roundups)} channels "
            f"read off {len(roundups)} tracker video(s), "
            f"{sum(r.get('new_cappers', 0) for r in roundups)} of them new"
        )
    failures = [s for s in payload["sources"] if s["status"] != "ok"]
    if failures:
        lines.append(f"Skipped: {len(failures)} video(s)")
        for source in failures:
            lines.append(
                f"  - {source['capper']} {source['video_id']}: "
                f"{source['status']} ({source.get('error', '')})"
            )
    if payload["fights"]:
        lines.append("\nTop consensus:")
        for fight in payload["fights"][:5]:
            moneyline = next(
                (m for m in fight["markets"] if m["bet_type"] == "moneyline"), None
            )
            if not moneyline or not moneyline["options"]:
                continue
            top = moneyline["options"][0]
            lines.append(
                f"  {fight['display']}: {top['selection']} "
                f"{top['consensus_pct']}% ({top['pick_count']} picks)"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mma_engine",
        description="Build an MMA betting consensus report from YouTube transcripts.",
    )
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument(
        "--output",
        default="docs/data.json",
        help="Where to write the consensus payload (default: docs/data.json)",
    )
    parser.add_argument(
        "--skip-extraction",
        action="store_true",
        help="Fetch and cache transcripts only; make no Claude API calls.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Ignore cached transcripts and extractions; re-fetch everything.",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Force channel discovery on, regardless of settings.discovery.enabled.",
    )
    parser.add_argument(
        "--no-discover",
        action="store_true",
        help="Force channel discovery off; use only the videos listed in config.json.",
    )
    parser.add_argument(
        "--no-search",
        action="store_true",
        help=(
            "Skip the open YouTube search for this run — the configured capper "
            "channels only. Every video search finds is a paid extraction, so "
            "this is the cheap run."
        ),
    )
    parser.add_argument(
        "--discover-only",
        action="store_true",
        help=(
            "List the videos discovery would process, then exit. No transcripts "
            "are fetched and no Claude API calls are made."
        ),
    )
    pasted_group = parser.add_argument_group("pasted picks (paywalled cards)")
    pasted_group.add_argument(
        "--picks-from-text",
        metavar="CAPPER_ID=FILE",
        action="append",
        default=None,
        help=(
            "Extract picks from a text file you pasted yourself, attributed to "
            "CAPPER_ID. Repeatable. For the weekly rhythm, drop files into "
            "pasted/ named after the capper instead — no flag needed."
        ),
    )
    pasted_group.add_argument(
        "--no-pasted-picks",
        action="store_true",
        help="Skip the pasted/ folder for this run.",
    )
    roundup_group = parser.add_argument_group("tracker roundups (everyone's picks)")
    roundup_group.add_argument(
        "--picks-from-tracker",
        metavar="VIDEO_URL",
        action="append",
        default=None,
        help=(
            "Ingest a predictions-tracker roundup — one video reporting which "
            "channels picked which fighter. Repeatable; overrides "
            "tracker.picks_videos in config.json for this run."
        ),
    )
    roundup_group.add_argument(
        "--no-tracker-picks",
        action="store_true",
        help="Skip the roundup videos listed in config.json for this run.",
    )
    roundup_group.add_argument(
        "--roundup-slides",
        metavar="DIR",
        default=None,
        help=(
            "Read roundup slides you captured yourself (screenshots) from DIR "
            "instead of downloading the video. Use when yt-dlp can't fetch it."
        ),
    )
    roundup_group.add_argument(
        "--no-roundup-slides",
        action="store_true",
        help=(
            "Skip reading the roundup's slides — transcript only. Much cheaper, "
            "and usually returns almost nothing: the names are printed, not said."
        ),
    )
    roundup_group.add_argument(
        "--apply-tracker-cappers",
        action="store_true",
        help=(
            "Write channels first seen in a roundup into config.json at neutral "
            "trust, so their ids stay stable across runs."
        ),
    )
    screen_group = parser.add_argument_group("screen picks (any video, read off its frames)")
    screen_group.add_argument(
        "--picks-from-video",
        metavar="VIDEO_URL",
        action="append",
        default=None,
        help=(
            "Paste a YouTube URL: the video is downloaded, every unique frame "
            "is screenshotted and read for the picks printed on it, and they "
            "are ingested for the channel that posted it. Repeatable; adds to "
            "screen_videos in config.json for this run."
        ),
    )
    screen_group.add_argument(
        "--video-capper",
        metavar="CAPPER_ID",
        default="",
        help=(
            "Attribute the --picks-from-video / --video-frames picks to this "
            "capper instead of the video's own channel."
        ),
    )
    screen_group.add_argument(
        "--video-file",
        metavar="PATH",
        default=None,
        help=(
            "Read a video you downloaded yourself instead of fetching it: the "
            "frames are cut straight from this file (never deleted). Pair with "
            "--picks-from-video URL to say whose video it is, or use it alone."
        ),
    )
    screen_group.add_argument(
        "--video-frames",
        metavar="DIR",
        default=None,
        help=(
            "Read screenshots you captured yourself from DIR instead of "
            "downloading the video. Pair with one --picks-from-video for the "
            "attribution, or with --video-capper on its own."
        ),
    )
    screen_group.add_argument(
        "--remember-videos",
        action="store_true",
        help=(
            "Write the --picks-from-video URLs into config.json's screen_videos "
            "so every later run keeps their picks (read for free from screens/)."
        ),
    )
    screen_group.add_argument(
        "--no-screen-videos",
        action="store_true",
        help="Skip the screen_videos listed in config.json for this run.",
    )
    roster_group = parser.add_argument_group("capper roster (tracker videos)")
    roster_group.add_argument(
        "--roster-from",
        metavar="VIDEO_URL",
        help=(
            "Extract capper results from a predictions-tracker video and derive "
            "trust scores from them. Writes a proposal for review."
        ),
    )
    roster_group.add_argument(
        "--roster-mode",
        choices=["accumulate", "replace"],
        default="accumulate",
        help=(
            "accumulate (default): pool with previously recorded results — use "
            "for post-event reviews. replace: use this video alone — use for a "
            "long-period recap, which would double-count if pooled."
        ),
    )
    roster_group.add_argument(
        "--apply-roster",
        action="store_true",
        help="Merge the extracted roster into config.json instead of only proposing it.",
    )
    roster_group.add_argument(
        "--roster-output",
        default="roster_proposal.json",
        help="Where to write the roster proposal (default: roster_proposal.json).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    load_dotenv()

    try:
        if resolve_auto_event(args.config):
            log.info("Retargeted config.json to the next event (event.mode = \"auto\").")
        config = load_config(args.config)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    if args.no_cache:
        config.settings["use_cache"] = False

    try:
        if args.roster_from:
            return run_roster(
                config,
                video_url=args.roster_from,
                mode=args.roster_mode,
                apply_changes=args.apply_roster,
                proposal_path=Path(args.roster_output),
            )

        if args.discover or args.discover_only:
            config.settings["discovery"]["enabled"] = True
        if args.no_discover:
            config.settings["discovery"]["enabled"] = False
        if args.no_search:
            config.settings["discovery"]["search"]["enabled"] = False

        videos, discovery_report = resolve_videos(config)
    except ProxyConfigError as exc:
        log.error("%s", exc)
        return 2

    if args.discover_only:
        print(_summarize_discovery(config, videos, discovery_report))
        return 0 if videos else 1

    if args.no_pasted_picks:
        config.settings["pasted_picks"]["enabled"] = False
    pasted_notes: list[tuple[str, Path]] = []
    for pair in args.picks_from_text or []:
        capper_id, _, file_path = pair.partition("=")
        if not capper_id or not file_path:
            log.error(
                "--picks-from-text wants CAPPER_ID=FILE, e.g. "
                "--picks-from-text funky_picks=pasted/funky_picks.txt (got %r)", pair,
            )
            return 2
        pasted_notes.append((capper_id, Path(file_path)))

    # None here means "whatever config.json lists"; the flags either replace
    # that list or empty it.
    roundup_urls = [] if args.no_tracker_picks else args.picks_from_tracker
    effective_roundups = (
        config.tracker_picks_videos if roundup_urls is None else roundup_urls
    )
    if not config.settings["tracker_picks"]["enabled"]:
        effective_roundups = []
    if args.no_roundup_slides:
        config.settings["tracker_picks"]["read_slides"] = False
    slides_dir = Path(args.roundup_slides) if args.roundup_slides else None

    # Videos to read off their screen: config.json's list, plus whatever was
    # pasted on the command line for this run.
    screen_videos: list[ScreenVideoRef] = [] if args.no_screen_videos else list(config.screen_videos)
    pasted_videos: list[ScreenVideoRef] = []
    if args.video_capper and args.video_capper not in config.cappers:
        log.error("--video-capper %r is not a capper id in %s", args.video_capper, config.path)
        return 2
    for url in args.picks_from_video or []:
        try:
            video_id = extract_video_id(url)
        except ConfigError as exc:
            log.error("%s", exc)
            return 2
        pasted_videos.append(
            ScreenVideoRef(video_id=video_id, url=url, capper_id=args.video_capper)
        )
    if args.video_file and args.video_frames:
        log.error("--video-file and --video-frames are two ways in; use one")
        return 2
    local = args.video_file or args.video_frames
    if local:
        # A file or a folder of screenshots stands in for one video's
        # download. A URL alongside says whose video it is (and keys the
        # reading); without one the reading is keyed on the file's name and
        # attributed to --video-capper, or minted from the name.
        if len(pasted_videos) > 1:
            log.error("--video-file / --video-frames read one video; give them one --picks-from-video")
            return 2
        if args.video_file and not Path(args.video_file).is_file():
            log.error("No such video file: %s", args.video_file)
            return 2
        if args.video_frames and not Path(args.video_frames).is_dir():
            log.error("No such screenshots folder: %s", args.video_frames)
            return 2
        if pasted_videos:
            ref = pasted_videos[0]
            video_id, url = ref.video_id, ref.url
        else:
            video_id = local_video_id(local) if args.video_file else "captured_frames"
            url = ""
        pasted_videos = [
            ScreenVideoRef(
                video_id=video_id, url=url, capper_id=args.video_capper,
                frames_dir=args.video_frames or "", video_file=args.video_file or "",
            )
        ]
    # A URL pasted on the command line replaces its config.json entry for
    # this run, so a --video-frames folder or --video-capper override wins.
    pasted_ids = {ref.video_id for ref in pasted_videos}
    screen_videos = [ref for ref in screen_videos if ref.video_id not in pasted_ids] + pasted_videos
    if not config.settings["screen_picks"]["enabled"] and pasted_videos:
        config.settings["screen_picks"]["enabled"] = True
    if args.remember_videos and pasted_videos:
        added = remember_screen_videos(config.path, pasted_videos)
        if added:
            log.info("Remembered %d video(s) in %s screen_videos", len(added), config.path)

    pasted_settings = config.settings["pasted_picks"]
    has_pasted = bool(pasted_notes) or (
        pasted_settings["enabled"] and has_notes(Path(pasted_settings["dir"]))
    )

    # A roundup on its own is a perfectly good run: it carries every channel's
    # pick without needing a single per-capper video. So is a folder of pasted
    # cards.
    has_screens = bool(screen_videos) and config.settings["screen_picks"]["enabled"]
    if not videos and not effective_roundups and not has_pasted and slides_dir is None and not has_screens:
        log.error(
            "No videos to process. Either add entries to the \"videos\" array in %s "
            "(e.g. {\"capper_id\": \"artem_mma\", \"url\": \"https://youtu.be/...\"}), "
            "or enable settings.discovery to pull them from the capper channels. "
            "A predictions-tracker roundup works on its own too "
            "(--picks-from-tracker https://youtu.be/...), as does a pasted "
            "card in pasted/, or any picks video read off its screen "
            "(--picks-from-video https://youtu.be/...).",
            config.path,
        )
        return 2

    try:
        payload = run_pipeline(
            config,
            Path(args.output),
            skip_extraction=args.skip_extraction,
            videos=videos,
            discovery_report=discovery_report,
            roundup_urls=effective_roundups,
            apply_tracker_cappers=args.apply_tracker_cappers,
            pasted_notes=pasted_notes,
            slides_dir=slides_dir,
            screen_videos=screen_videos,
        )
    except ProxyConfigError as exc:
        log.error("%s", exc)
        return 2
    print(_summarize(payload))

    # Every video failing is a real failure, not an empty report.
    if payload["sources"] and all(s["status"] != "ok" for s in payload["sources"]):
        log.error("Every video failed — see the errors above.")
        return 1

    # A consensus with no fights in it is not a thin week, it is a wiped
    # dashboard: weekly.ps1 publishes whatever this writes, and with the
    # tracker roundup as the only source there is no second source left to
    # cover for it when the deck can't be found or read. Fail instead, so
    # the last good docs/data.json stays live.
    if not payload["fights"]:
        log.error(
            "The consensus came out empty — no fights survived. Nothing worth "
            "publishing, so this is a failure rather than a result. Usually "
            "this means the tracker roundup wasn't found or couldn't be read "
            "(check tracker.picks_videos and the roundup log lines above), or "
            "the ESPN card filter dropped everything because config.json names "
            "a different event than the picks cover."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
