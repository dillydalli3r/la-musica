"""Genre formatting: the specific genres a source stated, in the source's own
order.

Two rules, and everything else follows from them:

  * **a GENRE tag holds the names a source actually stated, and nothing else**;
  * **the ORDER is the source's own rank** — MusicBrainz publishes each
    entity's genres by vote count (its most-voted first), and the app's
    default is the TOP TWO of that list (`mb_genre_count`). A file stores them
    in exactly that order, so `art pop, alternative pop` is the ranking, not an
    alphabetized set.

The PARENT idea is gone (the owner's call, 2026-10-12): it is genuinely
debatable what should sit above a name — `industrial rock`'s parent could be
`industrial` or `rock`, and no data settles it. So nothing derives one any
more: `normalize_genres` does not drop a name another member sits under, the
grader does not grade an order or a parent, and no surface prints a "level
above" beside a name. (`mlo.genre_vocab` still answers `parent_of`/`top_of`
for the two internal BROWSE aids that bucket names — the artist recommender and
the keyword fallback's broad family — but this module no longer re-exports
them: no tag, no check and no row depends on them.)

What `normalize_genres` does, in order:

  * resolves each name to MusicBrainz's spelling when it is one (`Nonsense` is
    kept as written, because dropping evidence is worse);
  * removes case-insensitive repeats;
  * caps at `mb_genre_count` — the FIRST names win, which is the source's rank;
  * capitalizes for display (`display_name`).

Nothing is ever added, reordered, or removed for being someone's parent.

Names are MusicBrainz's own (:mod:`mlo.genre_vocab`): canonical casing and an
alias table for the spellings the sources emit. A name MusicBrainz does not
publish is kept verbatim — losing what a source said would be worse than
storing a name the grade check flags.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional

from .genre_vocab import FAMILIES, canonical

__all__ = [
    "DEFAULT_GENRE_COUNT", "GENRE_COUNT_MAX", "GENRE_ORDER_SEPARATOR",
    "FAMILIES", "canonical",
    "display_name", "split_stored", "iter_names",
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
    (`genre_vocab.canonical`, the grader's checks) folds case, so "Shoegaze"
    and "shoegaze" are the same genre to all of it.
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
    """The canonical list for one track: the SPECIFIC genres, as stated.

    * every name is resolved to MusicBrainz's spelling when it is a genre
      (`Nonsense` is kept as written, because dropping evidence is worse),
    * duplicates are removed case-insensitively,
    * the result is capped at *count* — the FIRST names win, because the
      caller's order IS the rank (MusicBrainz publishes an entity's genres by
      vote count, most-voted first, and `mb_genre_count` defaults to 2: the
      top two genres that site lists),
    * each name is capitalized for display (`display_name`) unless *caps* is
      off — the stored value is what a person reads, and MusicBrainz's own
      lowercase spelling is a database convention, not a tag convention.

    The ORDER of *names* is KEPT, never canonicalized: it is the ranking the
    source published, and the parent idea that once justified a canonical
    order is gone (2026-10-12). Nothing is added or removed for being a parent.
    """
    count = max(1, int(count or DEFAULT_GENRE_COUNT))
    known: List[str] = []
    seen = set()
    for raw in iter_names(names):
        name = canonical(raw)
        if name is None:
            # Kept verbatim: the grade check will flag it, but silently
            # dropping what a source said would be worse than a flagged name.
            name = _clean(raw)
        if not name:
            continue
        # Deduped case-insensitively, and IN PLACE: collecting the recognised
        # names first and appending the unrecognised ones after reordered the
        # list, so the cap then kept a name the source stated LATER over the
        # first one it stated (`["Nonsense", "rock"]` at `mb_genre_count = 1`
        # stored "Rock" and dropped "Nonsense") - and the order IS the rank.
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        known.append(name)
    out = known[:count]
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
    `normalize_genres`) — and the ORDER is the list's own (the source's rank),
    untouched here: rendering must never reorder what the file stores.
    """
    return GENRE_ORDER_SEPARATOR.join(normalize_genres(names, count=GENRE_COUNT_MAX))


def issues(values: Iterable, count: int = DEFAULT_GENRE_COUNT) -> List[str]:
    """Structural problems with a track's genre list, for the grader.

    Empty means "in shape": recognized names, no duplicates, at most *count*
    of them, single-spaced. POSITION carries no defect any more: the order is
    the source's own rank (MusicBrainz publishes an entity's genres by vote
    count and `mb_genre_count` takes the top N of that list), so no grader can
    know what another order should have been — any order passes. Neither does
    a PARENT: what sits above a name is not something any data settles (the
    owner's call, 2026-10-12), so a name another member would once have sat
    under is just another name. What is left is what a reader can see is
    wrong: a repeat, an unknown spelling, spacing, and too many names.
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
    return out
