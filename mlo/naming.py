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
folder ends with the release id, and the FILE name ends with the track id —
every level is identifiable without reading tags:
  Slowdive [a16371b9-…]/$if(%releasetype%,[%releasetype%] ,)$if(%date%,%date% - ,)%album% {…}[%label%] [%musicbrainz_albumid%]/1-01 Title [trackid].flac
"""
import os
import re

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
    # label then the release id, each its own optional bracket group
    "$if(%label%, [%label%])"
    "$if(%musicbrainz_albumid%, [%musicbrainz_albumid%])/"
    # file name carries the track's own id (%musicbrainz_trackid% = recording)
    "%discnumber%-$num(%tracknumber%,2) %title%"
    "$if(%musicbrainz_trackid%, [%musicbrainz_trackid%])"
)

_ILLEGAL = '<>:"\\|?*'
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def sanitize_path(text):
    """Sanitize each path segment (keeps the script's own '/' separators).
    Illegal filename characters are replaced with '_' — never dropped — so
    a value like "DECO*27" becomes "DECO_27" and every consumer (organizer,
    grader's expected-path check, beets mlo_dir) derives identical paths."""
    # drop empty bracket groups left by skipped conditionals
    text = re.sub(r"\[\s*\]|\{\s*\}", "", text)
    out = []
    for seg in text.split("/"):
        seg = seg.strip()
        seg = "".join("_" if ch in _ILLEGAL else ch for ch in seg)
        seg = re.sub(r"\s+", " ", seg).strip().rstrip(".")
        out.append(seg)
    return "/".join(s for s in out if s)


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


def _func(name, args, variables):
    a = [_eval(x, variables) for x in args]
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


def _eval(script, variables):
    """Raw evaluation (no sanitization — applied once at the top level)."""
    out = []
    i = 0
    n = len(script)
    while i < n:
        ch = script[i]
        if ch == "%":
            j = script.find("%", i + 1)
            if j == -1:
                out.append(ch)
                i += 1
                continue
            name = script[i + 1:j]
            value = str(variables.get(name, "") or "")
            # Tag values must never inject path separators or other illegal
            # characters (a title like "Aerials / Arto" would otherwise split
            # the filename into an unintended subfolder). Illegals become "_"
            # — the same convention as sanitize_path, so the organizer, the
            # grader's expected-path check and the beets mlo_dir field all
            # compute identical paths. The script's OWN "/" separators
            # (between %variables%) are untouched.
            value = re.sub(r'[<>:"/\\|?*]', "_", value)
            out.append(value)
            i = j + 1
        elif ch == "$":
            m = re.match(r"\$(\w+)\(", script[i:])
            if m:
                name = m.group(1)
                body, end = _find_balanced(script, i + 1 + len(name))
                out.append(_func(name, _split_args(body), variables))
                i = end
            else:
                out.append(ch)
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


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