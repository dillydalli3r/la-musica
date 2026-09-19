"""Genre formatting: the SPECIFIC genres first, the FAMILY last.

Two slots, not three. The middle slot of the old parent/main/sub model was a
judgement nobody could make consistently — "is this album Alternative Rock or
Post-Britpop, and which one is the *main* genre?" — so it is gone. What is
left is what can actually be answered:

  * the **specific** genres, which the sources and the model answer with, and
  * the **family** (the broad head: `rock`, `electronic`, `hip hop`), which is
    derived from the specific one by :mod:`mlo.genre_vocab` rather than asked
    for.

`rock` / `shoegaze` is a hierarchy; three near-synonyms in a row is not. The
family takes the LAST slot, so `mb_genre_count` = 2 means "one specific genre
and its family", and raising it to 3 allows a second specific genre in front of
the family.

Names are MusicBrainz's own (:mod:`mlo.genre_vocab`): canonical casing, an
alias table for the spellings the sources emit, and a curated genre -> family
table. A name MusicBrainz does not publish is kept verbatim — losing what a
source said would be worse than storing a name the grade check flags — and a
genre with no known family simply has no family slot.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional

from .genre_vocab import FAMILIES, canonical, is_parent, parent_of

__all__ = [
    "DEFAULT_GENRE_COUNT", "GENRE_COUNT_MAX", "GENRE_ORDER_SEPARATOR",
    "FAMILIES", "canonical", "is_parent", "parent_of",
    "split_stored", "iter_names", "normalize_genres", "format_genres", "issues",
]

# Two slots: one specific genre and its family. `mb_genre_count` is the
# user-facing knob and defaults to this same number.
DEFAULT_GENRE_COUNT = 2

# The knob's ceiling. Three is "two specific genres and the family"; past that
# a genre list stops describing the music and starts describing the reviewer.
GENRE_COUNT_MAX = 3

# What separates the slots in the rendered form. Never used as a storage
# separator by anything this app writes (it writes repeated fields) — it is
# read back only for lists another tagger joined that way.
GENRE_ORDER_SEPARATOR = " / "

# Every separator a stored value may carry. A genre name has no "/" or ";" in
# it, so splitting on both is safe, and one file tagged by two tools ends up
# as the names it actually holds.
_SPLIT_RE = re.compile(r"\s*[/;]\s*|\x00")


def _clean(name) -> str:
    return " ".join(str(name or "").split())


def split_stored(value) -> List[str]:
    """The genre names inside one stored value.

    Handles the shapes a file in the wild carries: a repeated field (one name
    per call), a `' / '`-joined list and a `'; '`-joined list — including a
    value that mixes both, which is what tagging the same file with two
    different tools leaves behind.
    """
    text = str(value or "")
    if not text:
        return []
    return [p for p in (_clean(p) for p in _SPLIT_RE.split(text)) if p]


def normalize_genres(names: Iterable, count: int = DEFAULT_GENRE_COUNT) -> List[str]:
    """The canonical list for one track: specifics first, family last.

    * every name is resolved to MusicBrainz's spelling when it is a genre
      (`Nonsense` is kept as written, because dropping evidence is worse),
    * duplicates are removed case-insensitively,
    * a name that IS a family is pulled out of the specific list and used as
      the family slot rather than repeated,
    * the family of the first specific genre is derived when none was given,
    * the result is capped at *count*, and the family is placed last — a
      second specific genre yields its slot to the family, never the reverse.
    """
    count = max(1, int(count or DEFAULT_GENRE_COUNT))
    known: List[str] = []
    unknown: List[str] = []
    family: Optional[str] = None
    seen = set()
    for raw in iter_names(names):
        name = canonical(raw)
        if name is None:
            # Kept verbatim: the grade check will flag it, but silently
            # dropping what a source said would be worse than a flagged name.
            name = _clean(raw)
            if name:
                unknown.append(name)
            continue
        if name in seen:
            continue
        seen.add(name)
        if is_parent(name):
            family = family or name
            continue
        known.append(name)
    # Unrecognised names are kept, but a repeat of one is a duplicate like any
    # other: without adding them to `seen` the same word twice survives, and
    # `issues()` then fails the very list the writer just produced.
    for name in unknown:
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        known.append(name)
    specifics = known
    if family is None:
        # The first recognised specific genre that has a family, not simply
        # the first one: an unrecognised name must not cost the track its
        # family slot.
        for name in specifics:
            found = parent_of(name)
            if found and found not in seen:
                family = found
                break
    if not family:
        return specifics[:count]
    if count == 1:
        # No room for both, and the specific genre is the informative one.
        return (specifics[:1] or [family])[:1]
    return (specifics[:count - 1] + [family])[:count]


def iter_names(names: Iterable) -> List[str]:
    """Every name in *names*, accepting a string, a list, or stored values."""
    if names is None:
        return []
    if isinstance(names, str):
        return split_stored(names)
    if isinstance(names, (list, tuple, set, frozenset)):
        out: List[str] = []
        for item in names:
            if isinstance(item, str) and (GENRE_ORDER_SEPARATOR in item or ";" in item):
                out.extend(split_stored(item))
            else:
                text = _clean(item)
                if text:
                    out.append(text)
        return out
    return split_stored(names)


def format_genres(names: Iterable) -> str:
    """The rendered form: `Specific / Family`. Empty list renders empty."""
    return GENRE_ORDER_SEPARATOR.join(normalize_genres(names, count=GENRE_COUNT_MAX))


def issues(values: Iterable, count: int = DEFAULT_GENRE_COUNT) -> List[str]:
    """Structural problems with a track's genre list, for the grader.

    Empty means "in shape": recognized names, no duplicates, at most *count*
    of them, family last.
    """
    names = iter_names(values)
    out: List[str] = []
    if not names:
        return ["missing"]
    if len(names) > count:
        out.append(f"too many ({len(names)} > {count})")
    if len({n.casefold() for n in names}) != len(names):
        out.append("duplicate")
    for name in names:
        if canonical(name) is None:
            out.append(f"not a known genre: {name}")
    family_at = [i for i, n in enumerate(names) if is_parent(n)]
    if family_at and family_at != [len(names) - 1]:
        out.append("family genre must be the last one")
    for name in names:
        if name != _clean(name):
            out.append("spacing")
            break
    return out
