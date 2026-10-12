"""Genre formatting: the SPECIFIC genres a source stated, and the hierarchy
above them derived — never written.

One rule, and everything else follows from it: **a GENRE tag holds the names a
source actually stated, and nothing else.** `Pop; Art Pop` was this app
asserting a level nobody published — "Pop" was derived from "Art Pop" and
written into the file as if a source had said it. That is also why the first
slot had to mean "the parent", why a list's meaning depended on its order, and
why a track whose only genre was `Pop` looked like a parent with no child.

What replaced it:

  * `normalize_genres` caps, resolves and dedupes the names, and drops every
    name another member of the SAME list sits under — the parent slot old
    files still carry, wherever the source put it (`_drop_derived_parents`).
    Nothing is ever added.
  * the hierarchy is a VIEW: `genre_vocab.path_of`/`parent_of` answer what sits
    above a name (`Electronic › House › Deep House`), so the levels a file
    shows can be corrected without rewriting the file, and a name that is a
    parent in one place and a genre in another (`House`) needs no special case
    — the tag stores what the track is, the view says where it sits.
  * `family_of` derives the head of a list for the callers that want one broad
    word (browse facets, marking a chip), from the names and never from a
    position.

Names are MusicBrainz's own (:mod:`mlo.genre_vocab`): canonical casing, an
alias table for the spellings the sources emit, and a curated parent table. A
name MusicBrainz does not publish is kept verbatim — losing what a source said
would be worse than storing a name the grade check flags — and a genre with no
known parent simply sits at the top of its own chain.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional

from .genre_vocab import (FAMILIES, canonical, is_parent, parent_of, path_of,
                          top_of)

__all__ = [
    "DEFAULT_GENRE_COUNT", "GENRE_COUNT_MAX", "GENRE_ORDER_SEPARATOR",
    "FAMILIES", "canonical", "is_parent", "parent_of", "path_of", "top_of",
    "display_name", "family_of", "split_stored", "iter_names",
    "normalize_genres", "format_genres", "issues",
]

# How many specific genres a track may hold. `mb_genre_count` is the
# user-facing knob and defaults to this same number.
DEFAULT_GENRE_COUNT = 2

# The knob's ceiling. Three specific genres is already a lot; past that a genre
# list stops describing the music and starts describing the reviewer.
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


def _drop_derived_parents(names):
    """*names* without any name another member of the list sits under.

    The shape this removes is the one THIS APP used to write: a family stored
    beside its own child, so `Pop; Art Pop` is really `Art Pop` — the `Pop`
    was derived, never stated, and a track whose only genre was `Pop` looked
    like a parent with no child. The rule is position-independent on purpose:
    a name is dropped whenever it is a STRICT ANCESTOR of another name in the
    SAME list, wherever the source put it, so `Pop; Electropop` and
    `Electropop; Pop` both answer `Electropop` and no head survives beside its
    own child by source order. A name no other member sits under keeps its
    place exactly as it came, so an unrelated pair (`Rock` + `Jazz`) and a
    lone head are untouched.
    """
    names = list(names)
    if len(names) < 2:
        return names
    keys = [canonical(n) or _clean(n) for n in names]
    kept = []
    for index, key in enumerate(keys):
        others = keys[:index] + keys[index + 1:]
        if key and any(key in path_of(other)[:-1] for other in others):
            continue
        kept.append(names[index])
    return kept


def family_of(names: Iterable) -> str:
    """The HEAD the list sits under, display-cased — "" when none is known.

    Derived from the names, never from their order: the first name with a
    known chain wins, and a list of nothing but heads answers with the first
    head. This is what a browse facet, a chip's colour or a "broad genre"
    line asks for; the tag itself never holds it (`normalize_genres`).
    """
    for name in iter_names(names):
        head = top_of(name)
        if head:
            return display_name(head)
    return ""


def normalize_genres(names: Iterable, count: int = DEFAULT_GENRE_COUNT,
                     caps: bool = True) -> List[str]:
    """The canonical list for one track: the SPECIFIC genres, as stated.

    * every name is resolved to MusicBrainz's spelling when it is a genre
      (`Nonsense` is kept as written, because dropping evidence is worse),
    * duplicates are removed case-insensitively,
    * a name another member of the SAME list sits under is dropped — the
      parent slot this app wrote before the parent became a derived view,
      wherever the source put it (`_drop_derived_parents`),
    * the result is capped at *count*,
    * each name is capitalized for display (`display_name`) unless *caps* is
      off — the stored value is what a person reads, and MusicBrainz's own
      lowercase spelling is a database convention, not a tag convention.

    Nothing is ever ADDED: the hierarchy above these names is
    `genre_vocab.path_of`, which callers derive when they want it.
    """
    count = max(1, int(count or DEFAULT_GENRE_COUNT))
    known: List[str] = []
    unknown: List[str] = []
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
    out = _drop_derived_parents(known)[:count]
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
    """The rendered form: one name per slot, joined by " / ". Empty renders empty.

    Only a display shape — the STORED form is one field per name (see
    `normalize_genres`), and nothing reads a meaning into the order.
    """
    return GENRE_ORDER_SEPARATOR.join(normalize_genres(names, count=GENRE_COUNT_MAX))


def issues(values: Iterable, count: int = DEFAULT_GENRE_COUNT) -> List[str]:
    """Structural problems with a track's genre list, for the grader.

    Empty means "in shape": recognized names, no duplicates, at most *count*
    of them, single-spaced. There is no ORDER rule any more — a GENRE tag
    holds specific names, and the level above them is derived, not stored
    (`mlo.genres` and `mlo.genre_vocab.path_of`), so position means nothing.
    What is still a defect is a PARENT name stored beside its own child,
    anywhere in the list: the family slot this app used to write, which the
    canonical pass removes.
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
    for name in names:
        if name != _clean(name):
            out.append("spacing")
            break
    if len(_drop_derived_parents([canonical(n) or _clean(n) for n in names])) < len(names):
        # The file still carries a parent name beside its own child (anywhere
        # in the list) — the family slot this app used to write. It is a
        # derived value standing in for a fact, and Format All's canonical
        # pass removes it (the same predicate, so the writer and the check
        # cannot disagree).
        out.append("derived parent genre stored")
    return out
