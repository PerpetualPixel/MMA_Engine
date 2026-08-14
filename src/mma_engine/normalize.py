"""Name and selection normalization.

Different cappers write the same fight a dozen ways ("Volkanovski vs Topuria",
"Ilia Topuria vs. Alexander Volkanovski", "Topuria/Volk"). Grouping picks for a
consensus needs a stable key per fight and per bet selection, so everything is
reduced to accent-free, punctuation-free surnames before comparison.

Known limitation: two fighters sharing a surname on the same card would collide.
Rare enough to accept; correct it by hand in `config.json` naming if it happens.
"""

from __future__ import annotations

import re
import unicodedata

# Particles that belong to the surname rather than being the surname itself.
_SURNAME_PARTICLES = {
    "da", "de", "del", "della", "der", "di", "do", "dos", "du",
    "la", "le", "san", "santa", "st", "van", "von",
}
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}

_METHOD_BUCKETS: list[tuple[str, tuple[str, ...]]] = [
    ("ko_tko", ("ko", "tko", "knockout", "knock out", "stoppage", "strikes", "punches")),
    ("submission", ("submission", "sub", "tap", "choke", "armbar", "kimura", "guillotine")),
    ("decision", ("decision", "dec", "cards", "judges", "points")),
]


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_name(name: str) -> str:
    """Lowercase, de-accent, and strip punctuation from a fighter's name."""
    cleaned = strip_accents(name or "").lower()
    cleaned = re.sub(r"[^a-z0-9\s]", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def surname(name: str) -> str:
    """Reduce a fighter name to the token used for matching across cappers."""
    tokens = [t for t in normalize_name(name).split() if t]
    while len(tokens) > 1 and tokens[-1] in _SUFFIXES:
        tokens.pop()
    if not tokens:
        return ""
    if len(tokens) >= 2 and tokens[-2] in _SURNAME_PARTICLES:
        return f"{tokens[-2]}{tokens[-1]}"
    return tokens[-1]


def within_one_edit(a: str, b: str) -> bool:
    """True when two strings differ by at most one edit (swap/insert/delete).

    The tolerance every caption-typo comparison in the pipeline runs on:
    aggregate.py's surname canonicalizer, event_card.py's card matching, and
    odds.py's book matching all need the same "makhache" ~ "makhachev" slack.
    """
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) <= 1
    shorter, longer = (a, b) if len(a) < len(b) else (b, a)
    i = j = edits = 0
    while i < len(shorter) and j < len(longer):
        if shorter[i] == longer[j]:
            i += 1
        else:
            edits += 1
            if edits > 1:
                return False
        j += 1
    return True


def surnames_match(a: str, b: str) -> bool:
    """Caption-typo-tolerant surname equality: exact for short names, one edit
    of slack for long ones ("makhache" ~ "makhachev")."""
    if not a or not b:
        return False
    if a == b:
        return True
    return min(len(a), len(b)) >= 5 and within_one_edit(a, b)


def fight_key(fighter_a: str, fighter_b: str, canon=None) -> str:
    """Order-independent key identifying a matchup.

    `canon` optionally maps a surname to its canonical spelling, so caption
    typos that differ between videos ("makhache" / "makhachev") still land on
    the same key. See `aggregate.surname_canonicalizer`.
    """
    canon = canon or (lambda s: s)
    parts = sorted(canon(p) for p in (surname(fighter_a), surname(fighter_b)) if p)
    return "|".join(parts)


def method_bucket(text: str) -> str:
    """Classify a method-of-victory phrase into ko_tko / submission / decision."""
    lowered = normalize_name(text)
    for bucket, keywords in _METHOD_BUCKETS:
        if any(keyword in lowered for keyword in keywords):
            return bucket
    return "other"


def _number_in(text: str) -> str:
    match = re.search(r"\d+(?:\.\d+)?", text or "")
    return match.group(0) if match else ""


def selection_key(bet_type: str, selection: str, fighter: str = "", canon=None) -> str:
    """Stable key for the *side* of a bet, so identical picks group together."""
    canon = canon or (lambda s: s)
    selection = selection or ""
    fighter_part = canon(surname(fighter) or surname(selection))

    if bet_type == "moneyline":
        return fighter_part

    if bet_type == "method_of_victory":
        return f"{fighter_part}:{method_bucket(selection)}"

    if bet_type == "over_under":
        lowered = normalize_name(selection)
        side = "over" if "over" in lowered else "under" if "under" in lowered else "?"
        return f"{side}:{_number_in(selection)}"

    if bet_type == "round":
        return f"{fighter_part}:round{_number_in(selection)}"

    # Free-form props: fall back to the normalized text itself.
    return normalize_name(selection)


def display_name(candidates: list[str]) -> str:
    """Pick the most complete spelling of a name seen across cappers."""
    ranked = sorted(
        (c.strip() for c in candidates if c and c.strip()),
        key=lambda c: (len(c.split()), len(c)),
        reverse=True,
    )
    return ranked[0] if ranked else ""
