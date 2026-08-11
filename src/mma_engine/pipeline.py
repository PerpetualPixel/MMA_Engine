"""Pipeline orchestration and CLI entry point.

    python -m mma_engine --config config.json --output docs/data.json

Each stage is independently importable (`transcripts`, `extract`, `aggregate`),
so the GitHub Action just calls this module and commits the output file.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .aggregate import SourcedPick, build_consensus
from .config import Config, ConfigError, load_config
from .extract import PickExtractor
from .transcripts import TranscriptFetcher

log = logging.getLogger("mma_engine")


def run_pipeline(
    config: Config,
    output_path: Path,
    skip_extraction: bool = False,
) -> dict[str, Any]:
    """Fetch transcripts, extract picks, aggregate, and write the payload."""
    settings = config.settings

    fetcher = TranscriptFetcher(
        languages=settings["transcript_languages"],
        min_delay=float(settings["min_delay_seconds"]),
        max_delay=float(settings["max_delay_seconds"]),
        use_cache=bool(settings["use_cache"]),
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

    for index, video in enumerate(config.videos, start=1):
        capper = config.capper(video.capper_id)
        log.info(
            "[%d/%d] %s — %s", index, len(config.videos), capper.name, video.video_id
        )

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

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    log.info("Wrote %s", output_path)
    return payload


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

    if not config.videos:
        log.error(
            "No videos configured. Add this week's entries to the \"videos\" array "
            "in %s, e.g. {\"capper_id\": \"artem_mma\", \"url\": \"https://youtu.be/...\"}",
            config.path,
        )
        return 2

    if args.no_cache:
        config.settings["use_cache"] = False

    payload = run_pipeline(
        config, Path(args.output), skip_extraction=args.skip_extraction
    )
    print(_summarize(payload))

    # Every video failing is a real failure, not an empty report.
    if payload["sources"] and all(s["status"] != "ok" for s in payload["sources"]):
        log.error("Every video failed — see the errors above.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
