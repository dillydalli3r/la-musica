"""Canonical tag-VALUE text: the ONE spelling rule for the tags that have one.

A tag value is written by several hands — the beets import, the import wizard,
scripts 1/8/10/16, the bulk tag editor, the track editor — and read by several
more: the grader's checks, the album summaries the library view builds, the CD
detection in mlo.discs / mlo.audit / mlo.accurip. Two spellings of one answer
("cd" and "CD", "album; live" and "Album; Live") are the same answer to a
person and a DIFFERENT one to every one of those comparisons, so a library can
be at war with itself: the grader fails a value no writer would have produced,
while the CD checks treat a disc as something it is not.

So the tags that HAVE a canonical form are listed here once, with the rule that
produces it, and everyone asks this module:

  * ``AudioFile.set_tag`` / ``set_any_tag`` / ``set_video_tags`` apply it on
    every write, so an import, a wizard write and a manual edit all land
    canonical;
  * script 10 (Format All) re-applies it over an existing library;
  * ``grade_check_tag_case`` and ``grade_check_tag_spaces`` fail what the
    writers would have fixed.

What it deliberately does NOT touch is free text. TITLE, ALBUM, ARTIST,
ALBUMARTIST, LABEL, COMMENT and the lyrics are somebody's words, and their
letter case is part of the value: "AC/DC", "k.d. lang" and "mclusky" must
survive a tag write byte for byte. Only the tags in ``CANONICAL_CASE`` are
looked at at all; every other tag is returned verbatim.

For a tag whose value is a CLOSED vocabulary (MEDIA, RELEASETYPE, MOOD, …) the
match is case-insensitive and the canonical spelling is what is written back;
a value the vocabulary does not KNOW — a mood word a person typed, a release
type MusicBrainz has since added, a SOURCE that is really a video id — is
returned unchanged rather than coerced into a wrong answer. That is what makes
``canonical_value`` idempotent, and idempotence is what lets the same rule run
on every write and again over a whole library.

Pure and dependency-free on purpose: mlo.audio imports this module, and
mlo.audio is imported by mlo.moods, mlo.config, mlo.grader and most of the
engine, so a single import of any of them here would be an import cycle. Only
the standard library is used.
"""
from __future__ import annotations

import re
from typing import Callable, Dict, Optional, Tuple

__all__ = [
    "CANONICAL_CASE",
    "CANONICAL_VALUES",
    "canonical_text",
    "canonical_value",
    "collapse_spacing",
    "has_internal_space_run",
    "is_multiline",
    "spacing_problem",
]

# --------------------------------------------------------------------------- #
# The vocabularies, in the spelling this app stores
# --------------------------------------------------------------------------- #
# MusicBrainz's medium formats — the values the MusicBrainz release lookup
# (server.integrations), the download classifier (server.soulseek_auto) and
# mlo.grader.KNOWN_MEDIA all mean by "media". The grader derives its own set
# from this table, so the value it accepts and the value the writers store can
# never drift apart.
MEDIA_VALUES: Tuple[str, ...] = (
    '12" Vinyl', '10" Vinyl', '7" Vinyl', "8-Track", "Blu-ray", "Blu-spec CD",
    "Cassette", "CD", "CD-R", "Digital Media", "DVD", "DVD-Audio",
    "DVD-Video", "LaserDisc", "Minidisc", "SACD", "SHM-CD", "VHS", "Vinyl",
)

# MusicBrainz release types, primary and secondary, in MusicBrainz's own
# casing (mlo.naming resolves the same vocabulary for the folder path — its
# tables say WHY a type is spelled "DJ-mix"). A release carrying several types
# stores them "; "-joined, which canonical_value handles per part.
RELEASE_TYPE_VALUES: Tuple[str, ...] = (
    # primary
    "Album", "EP", "Single", "Broadcast", "Other",
    # secondary
    "Compilation", "Soundtrack", "Spokenword", "Interview", "Audiobook",
    "Live", "Remix", "DJ-mix", "Mixtape/Street", "Demo", "Audio drama",
    "Field recording",
)

# MusicBrainz release statuses — the same five-plus-two the app's release
# picker names (server.integrations' field help) and the import writes.
RELEASE_STATUS_VALUES: Tuple[str, ...] = (
    "Official", "Promotion", "Bootleg", "Pseudo-Release", "Withdrawn",
    "Expired", "Cancelled",
)

# The mood words mlo.moods' classifier derives, in the capitalisation a TAG is
# stored in — the classifier's own MOODS list is lowercase (it is a label it
# compares internally), while a value a person READS is capitalised the way a
# GENRE is ("Shoegaze", not "shoegaze"). The classifier returns a lowercase
# word and mlo.audio.set_tag writes this spelling, so a whole library told
# apart by case would not survive one pass. A mood a person typed is not in
# the list and is left exactly as typed.
MOOD_VALUES: Tuple[str, ...] = (
    "Happy", "Energetic", "Aggressive", "Sad", "Calm", "Dreamy", "Dark",
    "Party",
)

# AudioAuditor's verdicts. REAL / FAKE are what mlo.audit writes; MIX is what
# foobar2000's AudioAuditor writes for a file it heard both ways ("Fake and
# Real"), and the grader reads it as a verdict that is not REAL
# (mlo.grader's audit_summary handling), so it is part of the vocabulary
# rather than a value to be mangled.
AUDIT_VALUES: Tuple[str, ...] = ("REAL", "FAKE", "MIX")

# Where a rip came from — the two values THIS APP writes: server.main stamps
# "Soulseek" on a download, and the MEDIA/SOURCE pass fills the configured
# digital source, whose shipped default is paths.DEFAULT_DIGITAL_SOURCE
# ("Digital"). SOURCE is otherwise the user's own word — a custom digital
# source, or a video id (server.integrations._YOUTUBE_TAG_KEYS reads SOURCE as
# one) — and nothing outside this list is touched, so a source this table has
# never heard of keeps the spelling its owner gave it.
SOURCE_VALUES: Tuple[str, ...] = ("Soulseek", "Digital")

# tag -> its closed vocabulary, in the canonical spelling. This is what
# server.tags_registry serves as a tag's enum and what the case check reads.
CANONICAL_VALUES: Dict[str, Tuple[str, ...]] = {
    "MEDIA": MEDIA_VALUES,
    "SOURCE": SOURCE_VALUES,
    "RELEASETYPE": RELEASE_TYPE_VALUES,
    "RELEASESTATUS": RELEASE_STATUS_VALUES,
    "AUDIT": AUDIT_VALUES,
    "MOOD": MOOD_VALUES,
}

# --------------------------------------------------------------------------- #
# The rules
# --------------------------------------------------------------------------- #
def _closed(values: Tuple[str, ...]) -> Callable[[str], str]:
    """A rule that rewrites only what *values* names, case-insensitively.

    The lookup is built once, keyed folded, so a value outside the vocabulary
    (a typo, a word a new MusicBrainz release added, a free-typed mood) comes
    back exactly as it went in.
    """
    table = {v.casefold(): v for v in values}
    return lambda part: table.get(part.casefold(), part)


def _upper_code(part: str) -> str:
    """An ISO 3166-1 alpha-2 country code, upper-cased ("us" -> "US").

    The shape is the rule: no table enumerates the country set usefully, and
    every code in it is exactly two ASCII letters. Anything else — a full
    country name, an unknown word — is returned unchanged.
    """
    return part.upper() if len(part) == 2 and part.isascii() and part.isalpha() else part


def _title_code(part: str) -> str:
    """An ISO 15924 script code in its own casing ("latn" -> "Latn").

    Every code in that standard is four ASCII letters with an initial capital,
    so the shape is again the rule; a full name ("Latin") or any other value is
    returned unchanged.
    """
    return part.capitalize() if len(part) == 4 and part.isascii() and part.isalpha() else part


# tag -> how its value is capitalised. Tags absent from this table have no
# canonical form (and are what the free-text rule protects).
CANONICAL_CASE: Dict[str, Callable[[str], str]] = {
    # CLOSED vocabularies: matched case-insensitively, an unknown part kept.
    "MEDIA": _closed(MEDIA_VALUES),
    "SOURCE": _closed(SOURCE_VALUES),
    "RELEASETYPE": _closed(RELEASE_TYPE_VALUES),
    "RELEASESTATUS": _closed(RELEASE_STATUS_VALUES),
    "AUDIT": _closed(AUDIT_VALUES),
    "MOOD": _closed(MOOD_VALUES),
    # CODE-SHAPED values, whose canonical form is the code's own casing.
    "RELEASECOUNTRY": _upper_code,
    "SCRIPT": _title_code,
}

# MLO's own spelling of a multi-value field (mlo.audio joins repeated fields
# with "; " and server.beets.mloplugin writes "; "-joined release types). Only
# this exact separator is treated as a list: a ";" inside a URL is a character,
# not a second value.
_LIST_SEP = "; "

# A run of two or more spaces INSIDE a value. Two is not more separated than
# one and it makes two files' values compare unequal everywhere.
_SPACE_RUN = re.compile(r" {2,}")

# Tags whose value is TEXT rather than a value: their whitespace is content
# (indentation, a blank line between verses) and no spacing or case rule may
# reach inside them. Any value carrying a newline is treated the same way.
_MULTILINE_TAGS = frozenset({"LYRICS", "UNSYNCEDLYRICS", "SYNCLYRICS"})


def _bare_tag(tag) -> str:
    """The tag a stored key names, upper-cased.

    The raw writers spell a tag with a container prefix ("TXXX:MEDIA",
    "----:com.apple.iTunes:MOOD"); the last ":"-separated segment is the name
    either way, so a raw key resolves to the same rule as the semantic one.
    """
    return str(tag).rsplit(":", 1)[-1].strip().upper()


def is_multiline(tag, value) -> bool:
    """Whether *tag*'s value is text rather than a value (see _MULTILINE_TAGS)."""
    text = "" if value is None else str(value)
    if "\n" in text or "\r" in text:
        return True
    return _bare_tag(tag) in _MULTILINE_TAGS


def canonical_value(tag, value):
    """*value* in the canonical spelling for *tag*, or the value unchanged.

    A tag with no rule — TITLE, ALBUM, ARTIST, LABEL, the lyrics, anything not
    in CANONICAL_CASE — is returned exactly as it came in: this is the rule
    that keeps "AC/DC" and "k.d. lang" intact.

    A value holding several answers joined with "; " ("Album; Live") is
    canonicalised per part, and the separator is reproduced as "; ". A part
    that is empty (a stray separator, not a list) leaves the whole value alone.

    Idempotent: the canonical spelling of a canonical value is itself.
    """
    rule = CANONICAL_CASE.get(_bare_tag(tag))
    if rule is None or value is None:
        return value
    text = str(value)
    if _LIST_SEP in text:
        parts = [p.strip() for p in text.split(_LIST_SEP)]
        if all(parts):
            return _LIST_SEP.join(rule(p) for p in parts)
        return value
    return rule(text)


def collapse_spacing(value) -> str:
    """*value* with its spacing made canonical: outer spaces/tabs trimmed and a
    run of internal spaces collapsed to one.

    A multi-line value is returned untouched — the newlines and the indentation
    around them are the text (see is_multiline). This is the fix half of
    spacing_problem, and what mlo.audio.set_tag applies on every write.
    """
    text = str(value)
    if "\n" in text or "\r" in text:
        return text
    return _SPACE_RUN.sub(" ", text.strip(" \t"))


def has_internal_space_run(value) -> bool:
    """Whether a single-line *value* holds a run of two or more inner spaces.

    The value-level half of spacing_problem, exposed on its own because a
    grader that trims each LINE of a multi-line value still wants this rule
    applied per VALUE — and never inside a multi-line value.
    """
    text = "" if value is None else str(value)
    if "\n" in text or "\r" in text:
        return False
    return bool(_SPACE_RUN.search(text))


def spacing_problem(tag, value) -> str:
    """The reason *value*'s whitespace is wrong for *tag*, or "" when it is fine.

    A leading or trailing space/tab, or a run of two or more internal spaces.
    A multi-line value is NEVER judged: the whitespace inside it is the text,
    so LYRICS and every other value carrying a newline always comes back clean.

    The writers and the grader ask this one function — set_tag collapses what
    it reports, script 10 fixes it, grade_check_tag_spaces fails it — so a
    value can never be wrong to one of them and fine to another.
    """
    text = "" if value is None else str(value)
    if not text or is_multiline(tag, text):
        return ""
    if text != text.strip(" \t"):
        return "has leading/trailing spaces"
    if _SPACE_RUN.search(text):
        return "has a run of 2+ internal spaces"
    return ""


def canonical_text(tag, value) -> str:
    """Both halves at once: the canonical spelling AND collapsed spacing.

    What a writer stores for one single-line value — mlo.audio.set_tag applies
    exactly this — so a caller that wants to know whether a value WOULD change
    (script 10, the case check) asks the same question the write would.

    The trim comes FIRST, before the spelling lookup: "  soulseek  " has to
    resolve to the vocabulary entry "Soulseek", and a lookup on the padded
    string would miss it and hand the padding back as an "unknown" value.
    """
    text = "" if value is None else str(value)
    if "\n" in text or "\r" in text:
        return text
    return collapse_spacing(canonical_value(tag, text.strip(" \t")))
