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
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .aggregate import SourcedPick, build_consensus
from .config import Config, ConfigError, VideoRef, extract_video_id, load_config
from .discover import ChannelDiscovery
from .event_card import annotate_consensus, fetch_event_card
from .odds import annotate_odds, fetch_live_odds
from .extract import PickExtractor
from .proxy import ProxyConfigError, build_proxy_config, build_requests_proxies
from .roster import RosterExtractor, build_capper_entry, merge_into_config
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


def run_pipeline(
    config: Config,
    output_path: Path,
    skip_extraction: bool = False,
    videos: list[VideoRef] | None = None,
    discovery_report: list[dict] | None = None,
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
    previous_fights: list[dict[str, Any]] = []
    if output_path.is_file():
        try:
            previous_fights = json.loads(output_path.read_text(encoding="utf-8")).get(
                "fights", []
            )
        except (json.JSONDecodeError, OSError):
            pass
    card = fetch_event_card(event.get("name") or "")
    annotate_consensus(payload, card, previous_fights)

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
    failures = [entry for entry in report if entry["status"] != "ok"]
    if failures:
        lines.append("")
        lines.append(f"{len(failures)} channel(s) could not be read:")
        for entry in failures:
            lines.append(f"  - {entry['capper']}: {entry.get('error', entry['status'])}")
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
        "--discover-only",
        action="store_true",
        help=(
            "List the videos discovery would process, then exit. No transcripts "
            "are fetched and no Claude API calls are made."
        ),
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

        videos, discovery_report = resolve_videos(config)
    except ProxyConfigError as exc:
        log.error("%s", exc)
        return 2

    if args.discover_only:
        print(_summarize_discovery(config, videos, discovery_report))
        return 0 if videos else 1

    if not videos:
        log.error(
            "No videos to process. Either add entries to the \"videos\" array in %s "
            "(e.g. {\"capper_id\": \"artem_mma\", \"url\": \"https://youtu.be/...\"}), "
            "or enable settings.discovery to pull them from the capper channels.",
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
        )
    except ProxyConfigError as exc:
        log.error("%s", exc)
        return 2
    print(_summarize(payload))

    # Every video failing is a real failure, not an empty report.
    if payload["sources"] and all(s["status"] != "ok" for s in payload["sources"]):
        log.error("Every video failed — see the errors above.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
