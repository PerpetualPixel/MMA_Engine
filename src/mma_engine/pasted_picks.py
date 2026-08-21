"""Picks pasted by hand, for cards that never reach YouTube.

Cappers increasingly put their full card behind Patreon and leave a two-play
teaser on YouTube. The pipeline reads the teaser, records two picks where the
capper actually made twelve, and nothing in the run says so — a high-trust
voice quietly shrinks to a fraction of its weight, skewed toward whichever
plays were loud enough to give away.

This is the manual escape hatch. Drop a text file into `pasted/`, named for
the capper it came from, holding text you are entitled to read:

    pasted/funky_picks.txt          # matched by capper id
    pasted/BetSlam with Sam.txt     # or by name, or by any listed alias

Every run reads those files through the same extractor a transcript goes
through, so the picks arrive with real confidence, stated odds, and the
capper's own reasoning — everything a roundup line can't carry. They are
first-class picks and count as stated conviction.

Nothing here fetches from Patreon or anywhere else: the text has to be put in
the file by a person who has access to it. `pasted/` is gitignored for the
same reason — it holds someone else's paid writing and is never ours to
publish.

Two guards, because a hand-managed folder goes stale in a way a feed doesn't:

- A file older than `settings.pasted_picks.max_age_days` is skipped and
  reported, rather than re-injecting last month's card into this week's
  consensus every run until someone notices.
- A pasted card supersedes that capper's own video for any fight it covers.
  The paste is the full card; the video was the teaser.

Extractions are cached against a hash of the text, so re-running costs
nothing and editing the file re-reads it.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .aggregate import SourcedPick
from .config import Capper
from .normalize import fight_key
from .roster import name_key

log = logging.getLogger(__name__)

DEFAULT_DIR = "pasted"
DEFAULT_MAX_AGE_DAYS = 14
TEXT_SUFFIXES = (".txt", ".md")
# An optional first line naming where the text came from, kept as the pick's
# source link so a reader can see it was a paid post rather than a video.
SOURCE_PREFIXES = ("http://", "https://", "source:")


@dataclass(frozen=True)
class PastedNote:
    """One capper's pasted card, ready to extract."""

    capper: Capper
    path: Path
    text: str
    source_url: str
    paste_id: str


def parse_note(raw: str) -> tuple[str, str]:
    """Split an optional source line off the top of a pasted note."""
    lines = raw.lstrip().splitlines()
    if not lines:
        return "", ""
    head = lines[0].strip().lstrip("#").strip()
    lowered = head.lower()
    if any(lowered.startswith(prefix) for prefix in SOURCE_PREFIXES):
        url = head.split(":", 1)[1].strip() if lowered.startswith("source:") else head
        return "\n".join(lines[1:]).strip(), url
    return "\n".join(lines).strip(), ""


def note_id(capper_id: str, text: str) -> str:
    """A cache key for one pasted note.

    Hashed over the text so an unchanged file is free to re-run and an edited
    one is re-read — there is no video id to key on here.
    """
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    return f"paste_{capper_id}_{digest}"


def resolve_capper(stem: str, cappers: Iterable[Capper]) -> Capper | None:
    """The capper a file named `stem` belongs to: by id, name, or alias."""
    wanted = name_key(stem)
    if not wanted:
        return None
    for capper in cappers:
        if capper.id == stem or name_key(capper.id) == wanted:
            return capper
    for capper in cappers:
        for spelling in (capper.name, *capper.aliases):
            if name_key(spelling) == wanted:
                return capper
    return None


def collect_notes(
    directory: Path,
    cappers: Iterable[Capper],
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    now: datetime | None = None,
) -> tuple[list[PastedNote], list[dict[str, Any]]]:
    """Read `pasted/` into notes, plus a report row for every file skipped."""
    cappers = list(cappers)
    notes: list[PastedNote] = []
    skipped: list[dict[str, Any]] = []
    if not directory.is_dir():
        return notes, skipped

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days) if max_age_days > 0 else None

    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if path.stem.lower() == "readme":
            continue

        row = {"path": str(path), "file": path.name}
        capper = resolve_capper(path.stem, cappers)
        if capper is None:
            log.warning(
                "pasted/%s: no capper matches that filename — name the file after "
                "a capper id, name, or alias from config.json", path.name,
            )
            skipped.append({**row, "status": "unknown_capper"})
            continue

        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if cutoff and modified < cutoff:
            log.warning(
                "pasted/%s: last edited %s, older than %d days — skipping so a "
                "stale card can't be re-counted. Touch or update the file to use it.",
                path.name, modified.date().isoformat(), max_age_days,
            )
            skipped.append({**row, "status": "stale", "modified": modified.isoformat()})
            continue

        text, source_url = parse_note(path.read_text(encoding="utf-8", errors="replace"))
        if not text:
            skipped.append({**row, "status": "empty"})
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
    return notes, skipped


def has_notes(directory: Path) -> bool:
    """Whether the folder holds any file worth reading — the cheap check the
    CLI makes before deciding a run has no sources at all."""
    if not directory.is_dir():
        return False
    return any(
        path.is_file()
        and path.suffix.lower() in TEXT_SUFFIXES
        and path.stem.lower() != "readme"
        for path in directory.iterdir()
    )


def supersede_video_picks(
    existing: list[SourcedPick], pasted: list[SourcedPick]
) -> tuple[list[SourcedPick], int]:
    """Drop video picks a pasted card speaks for. Returns (kept, dropped).

    A capper's teaser video and their pasted full card are the same person on
    the same fight; the paste is the authoritative one, and keeping both would
    let a capper vote twice when the two disagree.
    """
    covered = {
        (s.capper.id, fight_key(s.pick.fighter_a, s.pick.fighter_b)) for s in pasted
    }
    kept = [
        s
        for s in existing
        if s.source_kind != "video"
        or (s.capper.id, fight_key(s.pick.fighter_a, s.pick.fighter_b)) not in covered
    ]
    return kept, len(existing) - len(kept)
