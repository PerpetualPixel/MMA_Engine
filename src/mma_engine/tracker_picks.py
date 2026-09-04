"""Ingest a predictions-tracker roundup — one video, every channel's pick.

The tracker channel this repo already trusts for trust scores
(@UFCPredictionsTracker) also publishes a *pre-event* roundup: a video that
walks the card fight by fight and reads out which prediction channels are on
which fighter, often 150+ of them. That single upload is by far the widest
sample available in a week — far wider than the dozen channels whose own
videos this pipeline can find, fetch a transcript for, and extract. Most of
the channels in a roundup have no entry in `config.json` at all, and plenty
never post a video with a readable transcript, so without this module their
opinion never reaches the consensus.

    python -m mma_engine --picks-from-tracker https://youtu.be/VIDEO_ID

or, the normal weekly path, list the URL under `tracker.picks_videos` in
`config.json` and every run ingests it alongside the per-capper videos.

A roundup pick is a thinner thing than a pick from a capper's own video, and
is treated as one:

- It carries no conviction, no price, and no reasoning — the slide says "these
  channels are on Fighter A" and nothing else. So every roundup pick enters at
  one neutral confidence (`settings.tracker_picks.confidence`, 5/10 by
  default) rather than a made-up number, and is tagged `source: "tracker"` in
  the payload so the dashboard can say where it came from.
- A capper's own video always wins. If a channel is both in the roundup and
  had its own video extracted this run, the roundup entry for that fight is
  dropped rather than counted — same vote, lower fidelity.
- Channels with no `config.json` entry are minted at neutral trust (5.0), so
  they count as one unweighted voice each instead of being thrown away.
  `--apply-tracker-cappers` writes them into the config so the id stays
  stable and a later `--roster-from` review can attach a real record to them.

Fail-open, like every other optional stage: a transcript that can't be read,
an extraction that errors, or a video that turns out not to be a roundup at
all costs the run nothing — the per-capper consensus is built exactly as it
would have been.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import anthropic
from pydantic import BaseModel, Field

from .aggregate import SourcedPick
from .config import Capper
from .discover import ChannelDiscovery, DiscoveredVideo
from .extract import Pick, chunk_transcript
from .normalize import display_name, fight_key, surname, within_one_edit
from .roster import NEUTRAL_TRUST, name_key, slugify

log = logging.getLogger(__name__)

# A tally entry states no conviction at all, so it enters mid-scale: half the
# weight of a stated best bet, twice that of a throwaway lean.
DEFAULT_CONFIDENCE = 5
# Roundups are name-dense — a single chunk can hold hundreds of channel names,
# and the output is one line per name. Chunks are kept well under the
# per-video default so no single response has to carry the whole card.
DEFAULT_CHUNK_CHARS = 12000


class TrackerFightPicks(BaseModel):
    """One fight from the roundup and the channels on each side."""

    fighter_a: str = Field(description="First fighter in the matchup, full name.")
    fighter_b: str = Field(description="Second fighter in the matchup, full name.")
    cappers_for_a: list[str] = Field(
        description=(
            "Names of the prediction channels the video says picked fighter_a. "
            "One entry per channel, spelled as the channel is known."
        )
    )
    cappers_for_b: list[str] = Field(
        description="Names of the prediction channels the video says picked fighter_b."
    )


class TrackerRoundup(BaseModel):
    """Every fight covered by one roundup video (or transcript chunk)."""

    event_name: str = Field(
        description=(
            "The event being covered, e.g. 'UFC 300'. Empty string if the "
            "transcript never names it."
        )
    )
    fights: list[TrackerFightPicks] = Field(
        description="Each fight the transcript reports channel picks for."
    )


SYSTEM_PROMPT = """\
You read transcripts of MMA "predictions tracker" roundups — videos that go \
through an upcoming card fight by fight and report which YouTube prediction \
channels picked which fighter.

The transcripts are machine-generated, so channel names and fighter names are \
frequently mangled. Correct them to the real name when you are confident of \
the identity, and keep every name spelled identically everywhere it appears.

Record only what the video actually attributes:

- List a channel under a fighter only when the video says that channel picked \
that fighter. Never split a count into invented names: if the host says "32 \
channels are on Silva" without naming them, record only the names actually \
spoken.
- A channel belongs to exactly one side of a fight. If the host corrects \
themselves, keep the side they land on.
- Ignore the host's own opinion, the betting odds, and any discussion of how \
the fight might play out. This is an attribution task, not a picks-analysis \
task.
- Include every fight the transcript covers, even ones with only a couple of \
named channels.

If the transcript is not a roundup of other channels' picks — for example a \
single capper's own predictions video, or a results recap of how channels \
scored — return an empty fights list rather than inferring attributions.\
"""

USER_TEMPLATE = """\
Video: {video_url}{chunk_note}

Extract, per fight, which prediction channels picked which fighter.

<transcript>
{transcript}
</transcript>\
"""


@dataclass
class RoundupResult:
    """The outcome of reading one roundup video."""

    video_id: str
    fights: list[TrackerFightPicks] = field(default_factory=list)
    event_name: str = ""
    ok: bool = True
    error: str = ""
    from_cache: bool = False

    @property
    def attribution_count(self) -> int:
        return sum(len(f.cappers_for_a) + len(f.cappers_for_b) for f in self.fights)


def merge_roundups(parsed: Iterable[TrackerRoundup]) -> list[TrackerFightPicks]:
    """Fold every chunk's fights into one list, one vote per channel per fight.

    Chunk boundaries cut a card mid-fight and roundups recap themselves, so the
    same fight arrives more than once — sometimes with the two fighters in the
    other order. Sides are therefore keyed by surname rather than by position.
    A channel the chunks disagree about (side A here, side B there) is dropped
    for that fight: a garbled attribution is worth less than no attribution.
    """
    fights: dict[str, dict[str, Any]] = {}
    conflicts = 0

    for roundup in parsed:
        for fight in roundup.fights:
            surname_a, surname_b = surname(fight.fighter_a), surname(fight.fighter_b)
            if not surname_a or not surname_b or surname_a == surname_b:
                continue
            key = fight_key(fight.fighter_a, fight.fighter_b)
            entry = fights.setdefault(
                key,
                {
                    "names": defaultdict(list),
                    "votes": {},
                    "labels": {},
                    "dropped": set(),
                    # Whichever fighter the first chunk named first stays
                    # fighter_a, so the merged fight reads the way the video
                    # said it rather than in surname-alphabetical order.
                    "primary": surname_a,
                },
            )
            sides = (
                (surname_a, fight.fighter_a, fight.cappers_for_a),
                (surname_b, fight.fighter_b, fight.cappers_for_b),
            )
            for side, spelling, cappers in sides:
                entry["names"][side].append(spelling)
                for capper in cappers:
                    key_c = name_key(capper)
                    if not key_c:
                        continue
                    previous = entry["votes"].get(key_c)
                    if previous is None:
                        entry["votes"][key_c] = side
                        entry["labels"][key_c] = capper.strip()
                    elif previous != side:
                        entry["dropped"].add(key_c)

    merged: list[TrackerFightPicks] = []
    for key, entry in fights.items():
        sides = sorted(entry["names"], key=lambda s: (s != entry["primary"], s))
        if len(sides) != 2:
            continue
        conflicts += len(entry["dropped"])
        buckets: dict[str, list[str]] = {sides[0]: [], sides[1]: []}
        for key_c, side in entry["votes"].items():
            if key_c in entry["dropped"]:
                continue
            buckets[side].append(entry["labels"][key_c])
        merged.append(
            TrackerFightPicks(
                fighter_a=display_name(entry["names"][sides[0]]),
                fighter_b=display_name(entry["names"][sides[1]]),
                cappers_for_a=sorted(buckets[sides[0]]),
                cappers_for_b=sorted(buckets[sides[1]]),
            )
        )

    if conflicts:
        log.info(
            "Dropped %d roundup attribution(s) the transcript put on both sides "
            "of the same fight", conflicts,
        )
    merged.sort(key=lambda f: len(f.cappers_for_a) + len(f.cappers_for_b), reverse=True)
    return merged


class RoundupExtractor:
    """Wraps the Claude API call that turns a roundup transcript into picks.

    Deliberately mirrors `extract.PickExtractor` — same chunking, same caching
    contract, same "one bad video never aborts the run" error handling — so
    the two extraction paths behave identically from the pipeline's side.
    """

    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        model: str = "claude-opus-5",
        effort: str = "high",
        max_tokens: int = 20000,
        max_chunk_chars: int = DEFAULT_CHUNK_CHARS,
        cache_dir: str | Path = "cache/tracker_picks",
        use_cache: bool = True,
    ) -> None:
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.max_chunk_chars = max_chunk_chars
        self.cache_dir = Path(cache_dir)
        self.use_cache = use_cache

    # -- caching -----------------------------------------------------------

    def _cache_path(self, video_id: str) -> Path:
        return self.cache_dir / f"{video_id}.json"

    def _read_cache(self, video_id: str) -> RoundupResult | None:
        if not self.use_cache:
            return None
        path = self._cache_path(video_id)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            fights = [TrackerFightPicks.model_validate(item) for item in payload["fights"]]
        except Exception:
            log.warning("Ignoring unreadable roundup cache: %s", path)
            return None
        return RoundupResult(
            video_id=video_id,
            fights=fights,
            event_name=payload.get("event_name", ""),
            from_cache=True,
        )

    def _write_cache(self, result: RoundupResult) -> None:
        if not self.use_cache:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "video_id": result.video_id,
            "model": self.model,
            "event_name": result.event_name,
            "fights": [fight.model_dump() for fight in result.fights],
        }
        self._cache_path(result.video_id).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # -- extraction --------------------------------------------------------

    def _extract_chunk(
        self, transcript: str, video_url: str, chunk_note: str
    ) -> TrackerRoundup:
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=self.max_tokens,
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": USER_TEMPLATE.format(
                        video_url=video_url,
                        chunk_note=chunk_note,
                        transcript=transcript,
                    ),
                }
            ],
            output_format=TrackerRoundup,
        )
        if response.stop_reason == "refusal":
            raise RuntimeError(f"Model refused the request: {response.stop_details}")
        if response.parsed_output is None:
            raise RuntimeError(
                f"Model returned no structured output (stop_reason="
                f"{response.stop_reason})"
            )
        return response.parsed_output

    def extract(self, video_id: str, transcript: str, video_url: str) -> RoundupResult:
        cached = self._read_cache(video_id)
        if cached is not None:
            log.info(
                "[%s] roundup from cache (%d fights, %d attributions)",
                video_id,
                len(cached.fights),
                cached.attribution_count,
            )
            return cached

        chunks = chunk_transcript(transcript, self.max_chunk_chars)
        parsed_chunks: list[TrackerRoundup] = []
        event_name = ""

        for index, chunk in enumerate(chunks, start=1):
            note = (
                ""
                if len(chunks) == 1
                else f"\n(Part {index} of {len(chunks)} of this video's transcript.)"
            )
            try:
                parsed = self._extract_chunk(chunk, video_url, note)
            except anthropic.APIError as exc:
                return _failed(video_id, f"{type(exc).__name__}: {exc}", parsed_chunks)
            except RuntimeError as exc:
                return _failed(video_id, str(exc), parsed_chunks)
            except Exception as exc:  # one bad video must not abort the run
                log.exception("[%s] unexpected roundup extraction error", video_id)
                return _failed(
                    video_id, f"Unexpected error: {type(exc).__name__}: {exc}", parsed_chunks
                )
            event_name = event_name or parsed.event_name
            parsed_chunks.append(parsed)

        result = RoundupResult(
            video_id=video_id,
            fights=merge_roundups(parsed_chunks),
            event_name=event_name,
        )
        log.info(
            "[%s] roundup: %d fights, %d channel attributions across %d chunk(s)",
            video_id,
            len(result.fights),
            result.attribution_count,
            len(chunks),
        )
        self._write_cache(result)
        return result


def _failed(
    video_id: str, error: str, parsed_chunks: list[TrackerRoundup]
) -> RoundupResult:
    """A failed extraction — keeping whatever whole chunks already succeeded.

    A roundup is a long transcript read in several passes, and the failure is
    usually a spent API quota partway through. The chunks that did come back
    are complete fights' worth of attributions, so they are kept and the run
    continues with a partial roundup rather than none. Not cached: the next
    run should try for the rest.
    """
    partial = merge_roundups(parsed_chunks)
    if partial:
        log.warning(
            "[%s] roundup extraction stopped early (%s) — keeping %d fight(s) "
            "from the chunks that completed", video_id, error, len(partial),
        )
        return RoundupResult(video_id=video_id, fights=partial, ok=True, error=error)
    return RoundupResult(video_id=video_id, fights=[], ok=False, error=error)


# -- finding this week's roundup automatically ------------------------------

# A dedicated id, distinct from any real capper — this "channel" only ever
# exists for the duration of one discovery call and is never itself a source
# of picks.
TRACKER_SCAN_ID = "_tracker_roundup_scan"


def find_tracker_roundup(
    channel_url: str,
    channel_id: str,
    title_contains: list[str],
    api_key: str,
    lookback_days: int = 21,
    discovery: ChannelDiscovery | None = None,
) -> DiscoveredVideo | None:
    """The tracker channel's most recent upload matching this week's event.

    Reuses `ChannelDiscovery` exactly as capper discovery does — same API
    call, same RSS fallback, same legacy `/c/Name` page-scrape — pointed at
    one channel (the tracker's) instead of the whole roster, filtered by the
    same `discovery.title_contains` keywords the main event search already
    relies on. A roundup posts a little further ahead of the card than most
    capper previews, hence the wider default lookback.

    None on anything that doesn't resolve to a match: no channel configured,
    no keywords to filter by, no API key, nothing found, or a network
    problem — the caller treats that exactly like "nothing to auto-discover
    this week" and leaves `tracker.picks_videos` for a person to fill in.
    """
    if not channel_url and not channel_id:
        return None
    if not title_contains:
        return None

    scan_target = Capper(
        id=TRACKER_SCAN_ID, name="Tracker roundup", channel_url=channel_url, channel_id=channel_id
    )
    channel_discovery = discovery or ChannelDiscovery(
        lookback_days=lookback_days,
        max_per_channel=1,
        title_contains=title_contains,
        api_key=api_key,
    )
    found, _report = channel_discovery.discover([scan_target])
    if not found:
        return None
    return max(found, key=lambda v: v.published)


# -- boards read elsewhere -------------------------------------------------

READINGS_DIR = "roundups"


def load_readings(directory: Path) -> dict[str, tuple[str, TrackerRoundup]]:
    """Roundup boards already transcribed, keyed by video id.

    A deck that has been read once does not need reading again — not by the
    vision pass, not by anyone. Dropping the transcription in `roundups/` as

        {"video_id": "...", "source_url": "...", "event_name": "...",
         "fights": [{"fighter_a": "...", "fighter_b": "...",
                     "cappers_for_a": [...], "cappers_for_b": [...]}]}

    makes it a first-class source: merged with whatever the transcript and the
    slides give, free to re-run, and reviewable in a diff. It is also the way
    in when the video can't be downloaded and the API can't be called — the
    board is public information, and someone who has read it can just write it
    down.
    """
    readings: dict[str, tuple[str, TrackerRoundup]] = {}
    if not directory.is_dir():
        return readings

    for path in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            roundup = TrackerRoundup(
                event_name=raw.get("event_name", ""),
                fights=[
                    TrackerFightPicks.model_validate(fight)
                    for fight in raw.get("fights") or []
                ],
            )
        except Exception as exc:
            log.warning("Ignoring unreadable roundup reading %s: %s", path, exc)
            continue
        if not roundup.fights:
            continue
        video_id = str(raw.get("video_id") or path.stem)
        readings[video_id] = (str(raw.get("source_url") or ""), roundup)
        log.info(
            "Roundup board read from %s: %d fight(s)", path.name, len(roundup.fights)
        )
    return readings


def save_reading(
    directory: Path,
    video_id: str,
    source_url: str,
    fights: list[TrackerFightPicks],
    event_name: str = "",
) -> Path | None:
    """Write a board that was just read to `roundups/`, so it is read once.

    Reading a deck costs a vision call per slide. Once it is read, the result
    is plain facts about a public video — worth keeping in the repo, where the
    next run gets it for free, a diff shows exactly which channel was recorded
    on which side, and a name the reader garbled can be corrected by hand.
    Never overwrites: a file already there was either written by an earlier
    run or corrected by a person, and both beat re-deriving it.
    """
    if not fights:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{video_id}.json"
    if path.exists():
        return None
    payload = {
        "video_id": video_id,
        "source_url": source_url,
        "event_name": event_name,
        "fights": [fight.model_dump() for fight in fights],
    }
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    log.info("Wrote %s — %d fight(s), read once and free from here on", path, len(fights))
    return path


# -- attribution -----------------------------------------------------------


class CapperDirectory:
    """Resolves a channel name spoken in a roundup to a capper to weight it by.

    Configured cappers are matched by name or by any of their `aliases` (the
    same alias list `--roster-from` uses to route garbled tracker spellings to
    the right channel), with one edit of slack on longer names for the usual
    caption noise. Anything still unmatched is minted at neutral trust rather
    than dropped — an unknown channel is one unweighted voice, which is what
    it deserves, and is exactly how it would score with an empty record.
    """

    def __init__(self, cappers: Iterable[Capper]) -> None:
        self._known: dict[str, Capper] = {}
        self._ids: set[str] = set()
        for capper in cappers:
            self._ids.add(capper.id)
            for spelling in (capper.name, *capper.aliases):
                key = name_key(spelling)
                if key:
                    self._known.setdefault(key, capper)
        self._minted: dict[str, Capper] = {}

    @property
    def minted(self) -> list[Capper]:
        """Cappers invented for this run, in first-seen order."""
        return list(self._minted.values())

    def _lookup(self, key: str) -> Capper | None:
        exact = self._known.get(key)
        if exact is not None:
            return exact
        # Caption slack, but only where a single edit can't turn one real
        # channel into another: "mmaguru" and "mmagurus" are the same channel,
        # "pick" and "picks" as whole names are not worth guessing about.
        if len(key) < 8:
            return None
        for candidate, capper in self._known.items():
            if len(candidate) >= 8 and within_one_edit(key, candidate):
                return capper
        return None

    def resolve(self, name: str) -> tuple[Capper, bool] | None:
        """(capper, was_minted) for a spoken channel name, or None if unusable."""
        key = name_key(name)
        if not key:
            return None
        known = self._lookup(key)
        if known is not None:
            return known, False
        minted = self._minted.get(key)
        if minted is None:
            minted = Capper(
                id=self._mint_id(name),
                name=name.strip(),
                discover=False,
                trust={"overall": NEUTRAL_TRUST},
            )
            self._minted[key] = minted
            self._ids.add(minted.id)
        return minted, True

    def _mint_id(self, name: str) -> str:
        """A config id for a channel we have never seen, distinct from every
        configured one — a roundup name that slugifies onto an existing id is
        a different channel, not that capper, or it would have matched above."""
        base = f"tracker_{slugify(name)}"
        candidate, suffix = base, 2
        while candidate in self._ids:
            candidate = f"{base}_{suffix}"
            suffix += 1
        return candidate


@dataclass
class IngestStats:
    """What one roundup contributed, for the run log and the source record."""

    picks: int = 0
    matched: int = 0
    minted: int = 0
    superseded: int = 0
    unusable: int = 0

    @property
    def cappers(self) -> int:
        return self.matched + self.minted


def to_sourced_picks(
    fights: Iterable[TrackerFightPicks],
    directory: CapperDirectory,
    video_id: str,
    video_url: str,
    confidence: int = DEFAULT_CONFIDENCE,
    already_covered: frozenset[tuple[str, str]] = frozenset(),
) -> tuple[list[SourcedPick], IngestStats]:
    """Turn merged roundup fights into weighable picks.

    `already_covered` holds (capper_id, fight_key) pairs a capper's own video
    already spoke for this run; those roundup entries are skipped so one
    channel is one vote per fight, cast by the richer source.
    """
    picks: list[SourcedPick] = []
    stats = IngestStats()
    # matched/minted count channels, not picks: one channel picking eight
    # fights is one channel.
    matched_ids: set[str] = set()
    minted_ids: set[str] = set()

    for fight in fights:
        key = fight_key(fight.fighter_a, fight.fighter_b)
        sides = (
            (fight.fighter_a, fight.cappers_for_a),
            (fight.fighter_b, fight.cappers_for_b),
        )
        for fighter, names in sides:
            for name in names:
                resolved = directory.resolve(name)
                if resolved is None:
                    stats.unusable += 1
                    continue
                capper, was_minted = resolved
                if (capper.id, key) in already_covered:
                    stats.superseded += 1
                    continue
                (minted_ids if was_minted else matched_ids).add(capper.id)
                picks.append(
                    SourcedPick(
                        pick=Pick(
                            fighter_a=fight.fighter_a,
                            fighter_b=fight.fighter_b,
                            bet_type="moneyline",
                            selection=fighter,
                            fighter=fighter,
                            confidence=confidence,
                            # A tally says who, never dog-or-chalk, so these
                            # weight by the capper's overall score.
                            role="unknown",
                            odds_american="",
                            stake_units="",
                            reasoning="",
                        ),
                        capper=capper,
                        video_id=video_id,
                        video_url=video_url,
                        source_kind="tracker",
                    )
                )
                stats.picks += 1

    stats.matched, stats.minted = len(matched_ids), len(minted_ids)
    return picks, stats


def capper_entry(capper: Capper) -> dict[str, Any]:
    """A `config.json` entry for a channel first seen in a roundup."""
    return {
        "id": capper.id,
        "name": capper.name,
        "channel_url": "",
        # No channel URL means discovery has nothing to sweep; a later
        # --roster-from review or a hand-added URL can turn this on.
        "discover": False,
        "trust": {
            "overall": NEUTRAL_TRUST,
            "underdog": NEUTRAL_TRUST,
            "favorite": NEUTRAL_TRUST,
            "method": NEUTRAL_TRUST,
        },
        "tracked": {
            "notes": "First seen in a predictions-tracker roundup; no tracked record yet.",
            "videos": [],
            "record": {},
        },
    }


def merge_new_cappers(config_path: Path, cappers: Iterable[Capper]) -> list[str]:
    """Append roundup-discovered channels to `config.json`. Returns names added.

    Strictly additive: an entry that already exists — by id or by any name or
    alias it lists — is left exactly as it is. Trust scores earned from
    tracked results are never overwritten by this path.
    """
    config = json.loads(config_path.read_text(encoding="utf-8"))
    existing = config.setdefault("cappers", [])

    ids = {entry.get("id") for entry in existing}
    names: set[str] = set()
    for entry in existing:
        for spelling in [entry.get("name", "")] + list(entry.get("aliases") or []):
            key = name_key(spelling)
            if key:
                names.add(key)

    added: list[str] = []
    for capper in cappers:
        if capper.id in ids or name_key(capper.name) in names:
            continue
        existing.append(capper_entry(capper))
        ids.add(capper.id)
        names.add(name_key(capper.name))
        added.append(capper.name)

    if added:
        config_path.write_text(
            json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return added
