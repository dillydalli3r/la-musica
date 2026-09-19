"""Genre formatting: parent / main / sub, in that order, three by default.

The app's genre contract in one place, because five consumers have to agree
on it — the AI inference that produces the list, the import that writes it,
script 8 (auto tagging) and script 10 (format all) that keep it trimmed, and
the grader that fails a track whose list does not follow the shape.

Canonical storage is the repeated `GENRE` field, in hierarchy order:

    GENRE = Rock
    GENRE = Alternative Rock
    GENRE = Post-Britpop

`format_genres` renders that as one string for the UI and for log lines.
Storage stays a plain multi-value tag because every player understands it;
the " / " join is a *display* convention, so a track whose own tags carry a
single `"Rock / Alternative Rock / Post-Britpop"` value is read back as three
genres (`split_stored`) rather than one oddly named one — the same tolerance
`trim_genres` already applies to the `"; "` spelling.

The hierarchy is enforced only as far as it can be checked cheaply: the first
slot must be a recognised broad genre when it is one this vocabulary knows,
slots must be non-empty and distinct. Ordering is deliberately NOT re-derived
from the strings — a model (or a human) ranks "Post-Britpop" under
"Alternative Rock" in a way no word list will reproduce, and second-guessing
it would rewrite correct data.

`mlo/genres.py` imports nothing from the server: the AI call, the MB/RYM
lookups and the provider chain all live above this module, which is pure
string policy.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

# Canonical storage count. `mb_genre_count` is the user-facing knob and
# defaults to this same number; this constant is what code falls back to when
# a partial config has no value at all.
DEFAULT_GENRE_COUNT = 3

# What separates the slots in the rendered form. Never used as a storage
# separator by anything this app writes (it writes repeated fields) — it is
# read back only for lists another tagger joined that way.
GENRE_ORDER_SEPARATOR = " / "

# Broad genres, used to (a) recognise a parent slot and (b) lift a legacy flat
# list into shape when nothing better is available. Kept as the *family* head
# of each branch of the genre tree; a subgenre's own name is not here.
GENRE_PARENTS = (
    "rock", "pop", "hip hop", "hip-hop", "rap", "electronic", "dance", "metal",
    "punk", "jazz", "blues", "soul", "funk", "r&b", "rhythm and blues",
    "reggae", "ska", "country", "folk", "world", "latin", "classical",
    "ambient", "experimental", "industrial", "gospel", "christian",
    "soundtrack", "stage & screen", "spoken word", "comedy", "audiobook",
    "easy listening", "new age", "children's", "holiday", "instrumental",
)


def _clean(name) -> str:
    return " ".join(str(name or "").split())


def is_parent_genre(name: str) -> bool:
    """True when `name` is a broad genre this vocabulary knows.

    Used to check the FIRST slot. An unknown head (a language- or region-
    specific family, "Kwaito") is not a failure — it is simply not something
    this list can vouch for, and the grader reports the *count* and duplicate
    rules separately so an unknown parent never fails a correct track.
    """
    return _clean(name).casefold() in _PARENT_SET


def family_of(name: str) -> Optional[str]:
    """The canonical broad genre a name belongs to, when it is one verbatim.

    Only the head of the vocabulary is resolvable without a full genre graph;
    everything else returns None, which callers treat as "no opinion" rather
    than "not a genre".
    """
    key = _clean(name).casefold()
    if key in _PARENT_SET:
        # Hand back the vocabulary's own spelling ("rock" -> "Rock").
        for parent in GENRE_PARENTS:
            if parent == key:
                return parent.title() if parent not in ("r&b",) else "R&B"
    return None


_PARENT_SET = {g.casefold() for g in GENRE_PARENTS}
# "hip-hop" is the same family as "hip hop" for slot purposes.
_PARENT_SET.add("hip hop")


def split_stored(value) -> List[str]:
    """The genre names inside one stored value.

    Repeated-field tags hand back one name per call, so a single value is the
    normal case; a value another tagger joined is split on ";" or " / " — and
    both spellings are also what this app renders, so reading its own output
    back through here is lossless.
    """
    text = _clean(value)
    if not text:
        return []
    if GENRE_ORDER_SEPARATOR in text:
        parts = text.split(GENRE_ORDER_SEPARATOR)
    elif ";" in text:
        parts = text.split(";")
    else:
        return [text]
    return [p for p in (_clean(p) for p in parts) if p]


def normalize_genres(names: Iterable, count: int = DEFAULT_GENRE_COUNT) -> List[str]:
    """The canonical list: trimmed, de-duplicated (case-insensitively), capped.

    Order is preserved — it IS the hierarchy, so sorting here would undo the
    inference the list came from. Duplicates collapse on the casefolded name;
    a blank entry is dropped rather than stored.
    """
    try:
        count = max(0, int(count))
    except (TypeError, ValueError):
        count = DEFAULT_GENRE_COUNT
    out: List[str] = []
    seen = set()
    for raw in names or []:
        for name in split_stored(raw):
            key = name.casefold()
            if not name or key in seen:
                continue
            seen.add(key)
            out.append(name)
    return out[:count] if count else out


def format_genres(names: Iterable) -> str:
    """The rendered form: `Parent / Main / Sub`. Empty list renders empty."""
    return GENRE_ORDER_SEPARATOR.join(normalize_genres(names, count=0))


def order_hierarchy(names: Iterable, count: int = DEFAULT_GENRE_COUNT) -> List[str]:
    """`normalize_genres` plus "the parent goes first".

    Only used on lists the app did NOT rank itself (a legacy flat GENRE tag,
    a source dump): if no slot is a known broad genre, the order is left
    exactly as found — inventing a hierarchy is worse than keeping none.
    """
    values = normalize_genres(names, count=count)
    if not values or is_parent_genre(values[0]):
        return values
    for i, name in enumerate(values[1:], start=1):
        if is_parent_genre(name):
            return [values[i]] + values[:i] + values[i + 1:]
    return values


def issues(values: Iterable, count: int = DEFAULT_GENRE_COUNT) -> List[str]:
    """Structural problems with a track's genre list, for the grader.

    Returns human-readable strings; empty means the list follows the contract.
    Deliberately structural only: the count, blank slots, duplicates and a
    parent that sits in the wrong slot are all checkable without a genre
    database, and each maps to a distinct fix ("run the inference", "drop the
    duplicate") rather than a vague "wrong genre".

    Mis-ordering is only reported when a LATER slot is a broad genre this
    vocabulary knows (so "Alternative Rock / Rock / Post-Britpop" is caught)
    — an unknown head ("Kwaito") is never a failure, because a genre list
    that cannot vouch for a name must not invent a defect.
    """
    names = [n for n in (split_stored(v) for v in (values or [])) for n in n]
    out: List[str] = []
    try:
        want = max(0, int(count))
    except (TypeError, ValueError):
        want = DEFAULT_GENRE_COUNT
    if want and len(names) != want:
        shown = "no genre" if not names else (
            f"{len(names)} genre" + ("" if len(names) == 1 else "s"))
        out.append(f"Genre count: {shown}, {want} expected")
    folded = [n.casefold() for n in names]
    has_dupes = len(set(folded)) != len(folded)
    if has_dupes:
        dupes = sorted({n for n in names if folded.count(n.casefold()) > 1})
        out.append("Genre slots repeat: " + ", ".join(dupes))
    # A repeated parent trips this too, and the duplicate line says it better.
    if len(names) > 1 and not has_dupes:
        misplaced = [n for n in names[1:] if is_parent_genre(n)]
        if misplaced:
            out.append(
                "Genre order: the parent genre must come first (found "
                + ", ".join(repr(n) for n in misplaced) + " after "
                + repr(names[0]) + ")")
    if any(n.strip() != n for n in names):
        out.append("Genre has leading/trailing spaces")
    return out
