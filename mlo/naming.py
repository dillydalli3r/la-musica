"""Picard-style file naming script evaluator.

Supports the subset of Picard scripting used by typical naming patterns:
  * %variable% substitution
  * $if(cond, then[, else])   — cond is truthy when non-empty
  * $left(s, n), $num(s, n), $lower(s), $upper(s), $replace(s, a, b)
  * literal text and "/" path separators

MULTI-VALUE RULE (the "does a tag hold a list?" problem):
  RELEASECOUNTRY and LABEL may hold SEVERAL values — beets, Picard and other
  taggers join multi-value fields with "; ", and releases themselves use
  " / " or "+" ("US; GB", "EU / UK", "Walt Disney Records + Universal").
  The naming script must still produce ONE deterministic path, so the FIRST
  non-empty entry wins and the rest are dropped (_first_multi). The rule
  lives here, in track_variables, so the organizer (server.main) and the
  grader's expected-path check evaluate identical paths for identical tags.
  Missing label/country/type/year simply drops its segment — the script
  below is built from $if() conditionals, never from literal brackets that
  could survive as a dangling "[]" (sanitize_path also removes empty []/{}).

Example (the default): the ARTIST folder ends with the artist id, the ALBUM
folder ends with the release id and the release group id, and the FILE name
ends with the track's recording id and the release group id — every level is
identifiable without reading tags:
  Slowdive [a16371b9-…]/$if(%releasetype%,[%releasetype%] ,)$if(%date%,%date% - ,)%album% {…}[%label%] [%musicbrainz_albumid%] [%musicbrainz_releasegroupid%]/1-01 Title [trackid] [releasegroupid].flac
"""
import os
import re
from functools import lru_cache

# Gradeable sentinel: the release type is UNKNOWN (no RELEASETYPE tag and no
# warm MusicBrainz value). The grader substitutes it so the rest of the path
# is still verified while the type token matches whatever the folder
# currently spells there.
UNKNOWN_RELEASE_TYPE = "\x00releasetype\x00"

DEFAULT_NAMING_SCRIPT = (
    # artist folder: name + artist id (a bracket group, like the album
    # folder's release id, so the two levels read alike)
    "%albumartist% [%musicbrainz_albumartistid%]/"
    "$if(%releasetype%,[%releasetype%] ,)"
    # Original (release group) date first, then the release's own — both
    # spelled in full when the tags carry more than a year.
    "$if(%originaldate%,%originaldate% - ,)"
    "$if(%date%,%date% - ,)"
    # country - media - catalog number, each joined only when the one before
    # it is present, so a missing tag never leaves a dangling " - "
    "%album% {$if(%releasecountry%,%releasecountry%)"
    "$if(%media%,$if(%releasecountry%, - ,)%media%)"
    "$if(%catalognumber%,$if(%media%, - ,$if(%releasecountry%, - ,))%catalognumber%)}"
    # label, the RELEASE id and the RELEASE GROUP id, each its own optional
    # bracket group. The release group is the album's own identity across its
    # pressings; the release id names this pressing.
    "$if(%label%, [%label%])"
    "$if(%musicbrainz_albumid%, [%musicbrainz_albumid%])"
    "$if(%musicbrainz_releasegroupid%, [%musicbrainz_releasegroupid%])/"
    # The file name carries the track's own ids: %musicbrainz_trackid% is the
    # RECORDING, and the release group ties the file back to its album even
    # after it is pulled out of the folder. The release id itself is not
    # repeated here — the folder above already names it, and a second copy of
    # a 36-character uuid is exactly the path length this app fights.
    "%discnumber%-$num(%tracknumber%,2) %title%"
    "$if(%musicbrainz_trackid%, [%musicbrainz_trackid%])"
    "$if(%musicbrainz_releasegroupid%, [%musicbrainz_releasegroupid%])"
)

# THE filename rule of the whole app: every character a filesystem refuses in
# a name, plus every ASCII control character. Export, organizer, Soulseek
# writes, playlist files and the grader's expected-path check all come through
# here — a second copy of this set anywhere is how a path the app WROTE stops
# being a path the app can FIND again (the audit then reports "no such file"
# for an album that is sitting right there).
#
# NUL (0x00) is deliberately NOT in the class: the OS refuses it in a path, so
# it can never reach a file, while mlo.grader spells its UNKNOWN_RELEASE_TYPE
# wildcard with it and passes that value through this very substitution — a
# NUL stripped here would silently break the expected-path regex.
_ILLEGAL_RE = re.compile(r'[<>:"/\\|?*\x01-\x1f]')

# A trailing dot or space is invalid on Windows: the filesystem drops it on
# write, so a name that keeps one is a name the app computes but cannot open.
_TRAILING_RE = re.compile(r"[ .]+$")

# Reserved device names, with or without an extension and in any case: Windows
# cannot create CON, NUL, COM1, LPT9, "AUX.mp3" — whatever the folder.
_RESERVED_RE = re.compile(r"(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$")

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def sanitize_segment(name):
    """ONE file or folder name: every invalid character becomes "_".

    The single rule every writer names files with. Illegal characters are
    REPLACED, never dropped, so the mapping is readable and predictable:
    "DECO*27" is "DECO_27", one invalid character is one "_" (a run of three
    is "___"), and the result is a fixed point — sanitize_segment(x) ==
    sanitize_segment(sanitize_segment(x)) — so organizing an already
    organized library is a no-op instead of a rename.

    A "/" here is the character the OS cannot have in a name, never a folder
    boundary: callers that hold a whole relative path want sanitize_path.
    """
    text = _ILLEGAL_RE.sub("_", str(name or ""))
    # Runs of blanks read as one blank (they are legal, just invisible). The
    # trailing dot/space rule below must see the space the source actually
    # ended with, so this only folds runs, it does not trim.
    text = re.sub(r"\s+", " ", text)
    if not text.strip(" "):
        return ""                     # nothing but blanks: not a name at all
    text = text.lstrip(" ")
    # Trailing dot/space, one for one. A segment of nothing but dots cannot
    # survive this either ("." -> "_", ".." -> "__"), which is what keeps a
    # tag value from naming a parent directory.
    text = _TRAILING_RE.sub(lambda m: "_" * len(m.group(0)), text)
    # A reserved device name gets a trailing "_" on its stem: still readable
    # ("AUX.mp3" -> "AUX_.mp3") and no longer reserved, and the result is a
    # fixed point because the guarded name is gone.
    reserved = _RESERVED_RE.match(text)
    if reserved:
        text = reserved.group(1) + "_" + (reserved.group(2) or "")
    return text


def sanitize_path(text):
    """A whole RELATIVE path: every "/"-separated name goes through
    sanitize_segment, and the separators themselves survive — in a naming
    script the "/" IS the folder structure. A "/" that arrives inside a tag
    value becomes "_" before the script is joined (_run substitutes values
    through sanitize_segment), so "AC/DC" names one file, never two levels."""
    # Drop the bracket groups a skipped condition left EMPTY, together with
    # the space that separated them: "Artist [%musicbrainz_albumartistid%]"
    # with no id is the artist's name, not "Artist " — and a trailing blank is
    # an invalid-name character below, so leaving it would spell the shipped
    # artist folder "Artist_".
    text = re.sub(r"\s*\[\s*\]|\s*\{\s*\}", "", text)
    return "/".join(s for s in (sanitize_segment(seg) for seg in text.split("/")) if s)


def name_key(name):
    """The comparison key for ONE file name, shared by everything that has to
    decide whether two names mean the same file.

    A name recorded by another program — a CUE sheet's ``FILE "01. AC/DC -
    Theme.flac"``, a rip log, an .accurip header, an older export — has to
    resolve to the file this app wrote for it (``01. AC_DC - Theme.flac``).
    Both sides go through the SAME rule, so the audit and the grading never
    lose an album to a "no such file" the app invented by renaming it.
    Sanitising BOTH sides is also what keeps this honest for a name that is
    already legal-but-odd on disk: the underscore a name may or may not carry
    stops being a difference.

    The rule runs FIRST and the leaf last: a recorded name carries its "/" as
    a character of the name, so splitting it off as a directory would drop
    everything before the last one ("01. AC/DC - Theme.flac" would key as
    "DC - Theme.flac" and never match the file on disk).
    """
    return os.path.basename(sanitize_segment(name))


def cue_ref_names(ref):
    """The names ONE cue-sheet FILE reference may denote, each keyed through
    name_key — the read side of the same rule.

    A reference is usually a bare leaf ("01 Theme.flac") and sometimes a path
    ("CD1/01 Theme.flac"), whose LEAF is the file in the album folder. But a
    "/" can also be a character OF the name: a sheet that still spells the rip's
    "01. AC/DC - Theme.flac" refers to the file this app wrote as
    "01. AC_DC - Theme.flac", and splitting that reference on the separator
    first would throw away "01. AC" and match nothing. So both the whole
    reference and its leaf are offered, in that order, and every caller accepts
    either one.
    """
    text = str(ref or "")
    names = [name_key(text)]
    leaf = text.replace("\\", "/").rsplit("/", 1)[-1]
    if leaf != text:
        names.append(name_key(leaf))
    return tuple(n for i, n in enumerate(names) if n and n not in names[:i])


def _find_balanced(text, start):
    """Return (content, index_after_close) for the parens starting at `start`
    which must point at '('. Handles nesting; no string literals needed."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1:i], i + 1
    return text[start + 1:], len(text)


def _split_args(argtext):
    """Split on top-level commas."""
    args, depth, cur = [], 0, []
    for ch in argtext:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            args.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    args.append("".join(cur))
    # NOTE: no stripping — trailing spaces in Picard scripts are significant
    # (e.g. "$if(%releasetype%,[%releasetype%] ,)")
    return args


# The $functions below implement, in the order the help lists them. Declared
# as data because a caller that accepts a script TYPED BY THE USER (the
# export's custom folder structure) has to name a typo: an unknown function
# evaluates to "" exactly like an empty tag, so "$iff(...)" silently shortened
# the path instead of reporting itself. Keep this tuple and _func in step.
FUNCTIONS = ("if", "eq", "ne", "not", "and", "or", "left", "right", "num",
             "lower", "upper", "replace")


def _func(name, args, variables):
    a = [_run(tokens, variables) for tokens in args]
    if name == "if":
        if len(a) >= 2:
            cond = a[0].strip()
            if cond and cond != "0":
                return a[1]
            return a[2] if len(a) >= 3 else ""
        return ""
    if name == "eq" and len(a) >= 2:
        return "1" if a[0] == a[1] else ""
    if name == "ne" and len(a) >= 2:
        return "1" if a[0] != a[1] else ""
    if name == "not" and a:
        return "" if (a[0].strip() and a[0].strip() != "0") else "1"
    if name == "and" and len(a) >= 2:
        return a[0] if not (a[0].strip() and a[0].strip() != "0") else a[1]
    if name == "or" and len(a) >= 2:
        return a[0] if (a[0].strip() and a[0].strip() != "0") else a[1]
    if name == "left" and len(a) >= 2:
        try:
            n = max(0, int(float(a[1])))
        except ValueError:
            n = 0
        return a[0][:n]
    if name == "right" and len(a) >= 2:
        try:
            n = max(0, int(float(a[1])))
        except ValueError:
            n = 0
        return a[0][-n:] if n else ""
    if name == "num" and len(a) >= 2:
        try:
            n = int(float(a[1]))
        except ValueError:
            n = 0
        digits = ""
        for ch in a[0]:
            if ch.isdigit():
                digits += ch
            elif digits:
                break
        try:
            return str(int(digits or 0)).zfill(n)
        except ValueError:
            return a[0]
    if name == "lower" and a:
        return a[0].lower()
    if name == "upper" and a:
        return a[0].upper()
    if name == "replace" and len(a) >= 3:
        return a[0].replace(a[1], a[2])
    return ""


@lru_cache(maxsize=32)
def _compile(script):
    """Token list for *script*: literal text, ("var", name), ("func", …, args).

    The script is a constant for a run — every track of a library is named by
    the same one — so it is scanned ONCE per distinct script instead of once
    per character per track (_eval used to re-find every %variable% and every
    balanced paren for each file). Cached by script text; argument bodies are
    compiled too, so a nested $if(...) is not re-scanned either.
    """
    tokens = []
    buf = []

    def flush():
        if buf:
            tokens.append(("lit", "".join(buf)))
            del buf[:]

    i = 0
    n = len(script)
    while i < n:
        ch = script[i]
        if ch == "%":
            j = script.find("%", i + 1)
            if j == -1:
                buf.append(ch)
                i += 1
                continue
            flush()
            tokens.append(("var", script[i + 1:j]))
            i = j + 1
        elif ch == "$":
            m = re.match(r"\$(\w+)\(", script[i:])
            if not m:
                buf.append(ch)
                i += 1
                continue
            name = m.group(1)
            body, end = _find_balanced(script, i + 1 + len(name))
            flush()
            tokens.append(("func", name,
                           tuple(_compile(a) for a in _split_args(body))))
            i = end
        else:
            buf.append(ch)
            i += 1
    flush()
    return tokens


def script_vocabulary(script):
    """(fields, functions) that *script* uses, each in the order it appears.

    ``fields`` are the %variable% names, ``functions`` the $function names —
    what a caller accepting a user-typed script compares against the
    vocabulary it can actually supply (``track_variables``' keys and
    ``FUNCTIONS``). Both are evaluated to "" when unknown, which is the right
    behaviour for a stored script and exactly what a validator has to catch
    before it is stored.
    """
    fields, calls = [], []

    def walk(tokens):
        for token in tokens:
            if token[0] == "var":
                fields.append(token[1])
            elif token[0] == "func":
                calls.append(token[1])
                for body in token[2]:
                    walk(body)

    walk(_compile(str(script or "")))
    return fields, calls


def _run(tokens, variables):
    """Evaluate a compiled token list against *variables*."""
    out = []
    for token in tokens:
        kind = token[0]
        if kind == "lit":
            out.append(token[1])
        elif kind == "var":
            value = str(variables.get(token[1], "") or "")
            # Every substituted TAG VALUE is a name fragment, so it goes
            # through the one rule (mlo.naming.sanitize_segment): a title like
            # "Aerials / Arto" or "AC/DC" must not invent a folder level, and
            # a control character or a reserved device name must not reach the
            # path either. The script's OWN "/" separators (between
            # %variables%) are literal tokens and stay untouched, so the
            # structure the user typed still means structure.
            out.append(sanitize_segment(value))
        else:
            out.append(_func(token[1], token[2], variables))
    return "".join(out)


def _eval(script, variables):
    """Raw evaluation (no sanitization — applied once at the top level)."""
    return _run(_compile(script), variables)


def eval_script(script, variables, shorter_ids=False):
    """Evaluate a Picard-style naming script into a relative path string.

    When shorter_ids is True, full MusicBrainz UUIDs in the output are
    truncated to their first 8 characters.
    """
    text = _eval(script, variables)
    if shorter_ids:
        text = _UUID_RE.sub(lambda m: m.group(0)[:8], text)
    return sanitize_path(text)


def _first_multi(value):
    """First entry of a tag that may hold several values.

    RELEASECOUNTRY and LABEL arrive as lists from other taggers ("US; GB",
    "EU / UK", "Label A + Label B"). The path must not depend on how many
    countries or labels a release has, so the FIRST non-empty entry wins and
    the separators (", " is NOT one — a label may contain a comma) are ";" /
    "+" / " / ". Returns "" when the tag is empty.
    """
    parts = re.split(r"\s*[;+]\s*|\s+/\s+", str(value or ""))
    return next((p.strip() for p in parts if p.strip()), "")


# MusicBrainz release-type vocabulary in MusicBrainz's OWN casing. Three
# spellings are in the wild and all of them must resolve to the same path:
# release_lookup's lowercase "+"-joined form ("album+soundtrack"), the
# "; "-joined MusicBrainz/Picard casing the RELEASETYPE tag carries
# ("Album; Soundtrack"), and a lone lowercase tag ("album").
_PRIMARY_TYPE_CAPS = {"album": "Album", "ep": "EP", "single": "Single",
                      "broadcast": "Broadcast", "other": "Other"}
_SECONDARY_TYPE_CAPS = {
    "compilation": "Compilation", "soundtrack": "Soundtrack",
    "spokenword": "Spokenword", "interview": "Interview",
    "audiobook": "Audiobook", "live": "Live", "remix": "Remix",
    "dj-mix": "DJ-mix", "mixtape/street": "Mixtape/Street", "demo": "Demo",
    "audio drama": "Audio drama", "field recording": "Field recording",
}

# The same vocabulary as plain type NAMES, in MusicBrainz's own lowercase
# spelling — what a release-group TYPE FILTER selects and compares against
# (see server/artist_watch). Derived from the maps above so a type the app can
# name in a folder path is exactly a type a watch may select, and the two can
# never drift apart.
PRIMARY_RELEASE_TYPES = tuple(_PRIMARY_TYPE_CAPS)
SECONDARY_RELEASE_TYPES = tuple(_SECONDARY_TYPE_CAPS)
RELEASE_TYPES = PRIMARY_RELEASE_TYPES + SECONDARY_RELEASE_TYPES


def _type_parts(value):
    """Release types in *value*, whichever of the three spellings it uses."""
    return [p.strip() for p in re.split(r"\s*[+;]\s*", str(value or "")) if p.strip()]


def mb_style_release_type(value):
    """*value* in MusicBrainz's own casing, "; "-joined:
    "album+live" → "Album; Live", "ep" → "EP". Unknown parts pass through."""
    return "; ".join(
        _PRIMARY_TYPE_CAPS.get(p.lower())
        or _SECONDARY_TYPE_CAPS.get(p.lower()) or p
        for p in _type_parts(value))


def lookup_style_release_type(value):
    """*value* in release_lookup's lowercase "+"-joined spelling
    ("Album; Live" → "album+live")."""
    return "+".join(p.lower() for p in _type_parts(value))


# A date tag ("DATE", "ORIGINALDATE") in one of the three spellings
# MusicBrainz, Picard and beets write: a year, a year-month, or a full ISO
# day. Anything else ("circa 1970", a range, a stray value) is not a date
# this app may sharpen and is left exactly as it is.
_ISO_DATE_RE = re.compile(r"\d{4}(?:-\d{2}(?:-\d{2})?)?\Z")


def fuller_date(existing, new):
    """*new* when it spells the SAME date more precisely than *existing*.

    The album folder is named after both date tags ("[Album] 1980-10-01 -
    1997-05-06 - Remain in Light"), so a tag holding only "1980" pins the
    folder to a year even when MusicBrainz knows the day. Filling EMPTY tags
    is not enough for that: the value has to be SHARPENED. This is the one
    write rule that touches a non-empty tag, and it can only ever add
    precision — "1980" → "1980-10-01", "1980-10" → "1980-10-01".

    Returns "" (leave the tag alone) for a value that already carries at
    least as much detail, one that contradicts the new date ("1979" vs
    "1980-10-01"), and any non-ISO spelling of either side.
    """
    have = str(existing or "").strip()
    fresh = str(new or "").strip()
    if not have or len(fresh) <= len(have) or not fresh.startswith(have):
        return ""
    if not _ISO_DATE_RE.match(fresh) or not _ISO_DATE_RE.match(have):
        return ""
    return fresh


def date_is_partial(value):
    """True for a date tag that stops short of the day ("1980", "1980-10").

    Such a value is the one thing MusicBrainz may still be able to sharpen,
    so a caller deciding whether an album is already complete has to treat
    it as unfinished (mlo.autotag's release-tag pass does).
    """
    text = str(value or "").strip()
    return bool(_ISO_DATE_RE.match(text)) and len(text) < 10


def _first_part(value):
    """'1/1' (disc 1 of 1) → '1' — multi-value tags must not inject '/'
    into paths where the sanitizer treats '/' as a folder separator."""
    s = str(value or "")
    if "/" in s:
        head = s.split("/")[0]
        if head.isdigit():
            return head
    return s


def track_variables(tags, release_type=None):
    """Build the variable map for one track from its tag dict.

    Multi-valued tags are reduced to their FIRST entry (_first_multi) so a
    release with several countries or labels produces one deterministic
    path. RELEASECOUNTRY is preferred; beets' own COUNTRY is the fallback
    (some importers stamp only that spelling).

    Two track ids — different things, both usable in a script:
      musicbrainz_trackid      MUSICBRAINZ_TRACKID      the RECORDING id:
                               the same audio on every release that carries it.
      musicbrainz_releasetrackid MUSICBRAINZ_RELEASETRACKID  the id of this
                               track's position ON THIS release (unique per
                               release, so it is the one that names a file
                               unambiguously inside one album).
    """
    tags = tags or {}
    date = tags.get("DATE") or ""
    return {
        "albumartist": tags.get("ALBUMARTIST") or tags.get("ARTIST") or "",
        "artist": tags.get("ARTIST") or "",
        "albumartistsort": tags.get("ALBUMARTISTSORT") or "",
        "musicbrainz_albumartistid": tags.get("MUSICBRAINZ_ALBUMARTISTID") or "",
        "musicbrainz_artistid": tags.get("MUSICBRAINZ_ARTISTID") or "",
        "musicbrainz_albumid": tags.get("MUSICBRAINZ_ALBUMID") or "",
        "musicbrainz_releasegroupid": tags.get("MUSICBRAINZ_RELEASEGROUPID") or "",
        "musicbrainz_trackid": tags.get("MUSICBRAINZ_TRACKID") or "",
        "musicbrainz_releasetrackid": tags.get("MUSICBRAINZ_RELEASETRACKID") or "",
        "releasetype": release_type or tags.get("RELEASETYPE") or "",
        "originaldate": tags.get("ORIGINALDATE") or "",
        "date": date,
        "year": (date.split("-")[0] if date else ""),
        "originalyear": (tags.get("ORIGINALDATE") or "").split("-")[0],
        "album": tags.get("ALBUM") or "",
        "releasecountry": _first_multi(tags.get("RELEASECOUNTRY")
                                       or tags.get("COUNTRY")),
        "media": tags.get("MEDIA") or "",
        "catalognumber": tags.get("CATALOGNUMBER") or "",
        "label": _first_multi(tags.get("LABEL")),
        "discnumber": _first_part(tags.get("DISCNUMBER")) or "1",
        "disctotal": _first_part(tags.get("DISCTOTAL") or tags.get("TOTALDISCS")) or "",
        "tracknumber": _first_part(tags.get("TRACKNUMBER")) or "",
        "tracktotal": _first_part(tags.get("TRACKTOTAL") or tags.get("TOTALTRACKS")) or "",
        "title": tags.get("TITLE") or "",
        "genre": tags.get("GENRE") or "",
    }