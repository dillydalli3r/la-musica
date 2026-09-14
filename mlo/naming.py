"""Picard-style file naming script evaluator.

Supports the subset of Picard scripting used by typical naming patterns:
  * %variable% substitution
  * $if(cond, then[, else])   — cond is truthy when non-empty
  * $left(s, n), $num(s, n), $lower(s), $upper(s), $replace(s, a, b)
  * literal text and "/" path separators

Example (the default):
  %albumartist% [%musicbrainz_albumartistid%]/$if(%releasetype%,[%releasetype%] ,)$if(%originaldate%,%originaldate% - ,)$if(%date%,%date% - ,)%album% {$if(%releasecountry%,%releasecountry% - )%media%$if(%catalognumber%, - %catalognumber%)}/%discnumber%-$num(%tracknumber%,2) %title%
"""
import os
import re

DEFAULT_NAMING_SCRIPT = (
    "%albumartist% [%musicbrainz_albumartistid%]/"
    "$if(%releasetype%,[%releasetype%] ,)"
    "$if(%originaldate%,%originaldate% - ,)"
    "$if(%date%,%date% - ,)"
    "%album% {$if(%releasecountry%,%releasecountry% - )%media%$if(%catalognumber%, - %catalognumber%)}/"
    "%discnumber%-$num(%tracknumber%,2) %title%"
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
    """Build the variable map for one track from its tag dict."""
    tags = tags or {}
    date = tags.get("DATE") or ""
    return {
        "albumartist": tags.get("ALBUMARTIST") or tags.get("ARTIST") or "",
        "artist": tags.get("ARTIST") or "",
        "albumartistsort": tags.get("ALBUMARTISTSORT") or "",
        "musicbrainz_albumartistid": tags.get("MUSICBRAINZ_ALBUMARTISTID") or "",
        "musicbrainz_artistid": tags.get("MUSICBRAINZ_ARTISTID") or "",
        "musicbrainz_albumid": tags.get("MUSICBRAINZ_ALBUMID") or "",
        "releasetype": release_type or tags.get("RELEASETYPE") or "",
        "originaldate": tags.get("ORIGINALDATE") or "",
        "date": date,
        "year": (date.split("-")[0] if date else ""),
        "originalyear": (tags.get("ORIGINALDATE") or "").split("-")[0],
        "album": tags.get("ALBUM") or "",
        "releasecountry": tags.get("RELEASECOUNTRY") or "",
        "media": tags.get("MEDIA") or "",
        "catalognumber": tags.get("CATALOGNUMBER") or "",
        "label": tags.get("LABEL") or "",
        "discnumber": _first_part(tags.get("DISCNUMBER")) or "1",
        "disctotal": _first_part(tags.get("DISCTOTAL") or tags.get("TOTALDISCS")) or "",
        "tracknumber": _first_part(tags.get("TRACKNUMBER")) or "",
        "tracktotal": _first_part(tags.get("TRACKTOTAL") or tags.get("TOTALTRACKS")) or "",
        "title": tags.get("TITLE") or "",
        "genre": tags.get("GENRE") or "",
    }