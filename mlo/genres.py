"""Genre formatting: the FAMILY first, the SPECIFIC genres after it.

Two slots, not three. The middle slot of the old parent/main/sub model was a
judgement nobody could make consistently — "is this album Alternative Rock or
Post-Britpop, and which one is the *main* genre?" — so it is gone. What is
left is what can actually be answered:

  * the **family** (the broad head: `rock`, `electronic`, `hip hop`), which is
    derived from the specific genre by :mod:`mlo.genre_vocab` rather than asked
    for, and
  * the **specific** genres, which the sources and the model answer with.

`rock` / `shoegaze` reads as the hierarchy it is: broad first, narrowing. The
family takes the FIRST slot, so `mb_genre_count` = 2 means "the family and one
specific genre", and raising it to 3 allows a second specific genre behind the
family.

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
    "FAMILIES", "canonical", "is_parent", "parent_of", "display_name",
    "split_stored", "iter_names", "normalize_genres", "format_genres", "issues",
]

# Two slots: the family and one specific genre. `mb_genre_count` is the
# user-facing knob and defaults to this same number.
DEFAULT_GENRE_COUNT = 2

# The knob's ceiling. Three is "the family and two specific genres"; past that
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


# Words that stay lowercase inside a capitalized genre name ("Drum and Bass",
# "Woman of the World") — normal title-case practice, and the reason a plain
# str.title() is wrong here.
_SMALL_WORDS = frozenset({
    "and", "or", "of", "the", "in", "on", "a", "an", "with", "for", "to",
})

# Names whose accepted spelling is NOT title case: initialisms and acronyms a
# str.title() would mangle ("Idm", "Uk Garage", "Hi-Nrg", "R&B" → "R&B" is
# already right, but "r&b" would become "R&B" only by luck). Keyed folded.
_ACRONYMS = frozenset({
    "idm", "edm", "ebm", "uk", "us", "usa", "r&b", "rnb", "nrg", "dnb",
    "nwobhm", "dj", "mc", "lo-fi", "ea", "ac", "alt",
})


def _cap_chunk(chunk: str) -> str:
    """One hyphen-separated piece, capitalized (acronyms upper-cased whole)."""
    folded = chunk.casefold()
    if folded in _ACRONYMS:
        return chunk.upper() if folded not in ("r&b", "rnb", "lo-fi") else (
            "R&B" if folded in ("r&b", "rnb") else "Lo-Fi")
    return chunk[:1].upper() + chunk[1:].lower() if chunk else chunk


def display_name(name) -> str:
    """The app's own capitalization of a genre name.

    MusicBrainz publishes its genre names lowercase ("shoegaze", "drum and
    bass") and this app stores what it publishes — but a tag is read by a
    person, and "Shoegaze / Rock" is the form every other tagger writes. The
    rule is title case with three deliberate exceptions: the small connective
    words stay lowercase, known acronyms keep their capitals, and each
    hyphen-separated piece is treated as a word of its own ("post-punk" →
    "Post-Punk", "hi-nrg" → "Hi-NRG").

    Capitalization is display, never identity: every comparison in the app
    (`genre_vocab.canonical`, `is_parent`, `parent_of`, the grader's checks)
    folds case, so "Shoegaze" and "shoegaze" are the same genre to all of it.
    """
    text = _clean(name)
    if not text:
        return ""
    words = []
    for index, word in enumerate(text.split(" ")):
        folded = word.casefold()
        if folded in _ACRONYMS:
            words.append(_cap_chunk(word))
        elif index and folded in _SMALL_WORDS:
            words.append(folded)
        else:
            words.append("-".join(_cap_chunk(p) for p in word.split("-")))
    return " ".join(words)


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


def normalize_genres(names: Iterable, count: int = DEFAULT_GENRE_COUNT,
                     caps: bool = True) -> List[str]:
    """The canonical list for one track: family first, specifics after.

    * every name is resolved to MusicBrainz's spelling when it is a genre
      (`Nonsense` is kept as written, because dropping evidence is worse),
    * duplicates are removed case-insensitively,
    * a name that IS a family is pulled out of the specific list and used as
      the family slot rather than repeated,
    * the family of the first specific genre is derived when none was given,
    * the result is capped at *count*, and the family is placed first — a
      second specific genre yields its slot to the family, never the reverse,
    * a single slot has no room for a family, so it holds the specific genre:
      a family on its own says almost nothing about the music,
    * each name is capitalized for display (`display_name`) unless *caps* is
      off — the stored value is what a person reads, and MusicBrainz's own
      lowercase spelling is a database convention, not a tag convention.
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
        out = specifics[:count]
    elif count == 1:
        # No room for both, and the specific genre is the informative one: a
        # family on its own ("Rock") says almost nothing about the track.
        out = (specifics[:1] or [family])[:1]
    else:
        out = ([family] + specifics[:count - 1])[:count]
    return [display_name(n) for n in out] if caps else out


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
    """The rendered form: `Family / Specific`. Empty list renders empty."""
    return GENRE_ORDER_SEPARATOR.join(normalize_genres(names, count=GENRE_COUNT_MAX))


def issues(values: Iterable, count: int = DEFAULT_GENRE_COUNT) -> List[str]:
    """Structural problems with a track's genre list, for the grader.

    Empty means "in shape": recognized names, no duplicates, at most *count*
    of them, family first.
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
    if family_at and family_at != [0]:
        out.append("family genre must be the first one")
    for name in names:
        if name != _clean(name):
            out.append("spacing")
            break
    return out
