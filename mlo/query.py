"""One query engine behind smart playlists and the library browser.

The condition language (``{"conditions": [{field, op, value}], "match":
"all"|"any"}``) was born in ``server.playlists`` for saved smart playlists.
It lives here now, verbatim, so a saved playlist and an ad-hoc browser query
can never disagree about what matches: both compile their condition list
through :func:`compile_spec` and walk the library through :func:`walk`, and
two rows match if and only if the same predicate says so.

Layering
--------
This module never imports ``server``. Everything it needs from the app the
caller injects:

``rating_of(path, raw_tag) -> half-stars | None``
    The rating store (``server.ratings``). Half-stars are its unit (0-10,
    0/absent = unrated); stars are what the catalogue advertises, so the
    engine halves them. Without a source every track is unrated — which is
    all the payload alone can prove. ``raw_tag`` is the file's own RATING,
    which only the track scope has: a folder carries no tag.

    The same source answers the FOLDER scopes through an optional attribute,
    ``rating_of.folder(scope, path) -> half-stars | None``, where scope is
    "album" or "artist" and path is that entity's folder — the store's own
    folder rows, keyed by the paths the app already uses for those entities.
    The engine needs one fact there that a track lookup does not carry: which
    KIND of entity the path names. A source without the attribute answers
    every album/artist rating field as unrated, which is all such a store can
    prove.

``bool_fields``
    The catalogue's boolean field names, so ``true``/``"1"``/``"yes"`` from a
    client compare equal to the payload's Python bool.

Field vocabulary
----------------
``tags.<KEY>``, ``tech.<KEY>``, ``analysis.<KEY>``, ``library.<KEY>``,
``album.<KEY>``, ``artist.<KEY>``, plus the payload's own bare keys
(``grade_pass``, ``audit``, …) and the bare sort aliases the API contract
names (``album``, ``artist``, ``title``, ``year``, ``genre``, ``rating``,
``path``). A bare key resolves against the track exactly as the original
evaluator did — tags first, then the track payload, then ``tech`` — so a
saved smart playlist keeps its meaning; the aliases only add names that
resolved to nothing before.

``rating``, ``album.rating`` and ``artist.rating`` are one family: the same
unit (stars, halves) and the same ops over the three scopes a rating can name
— the track's own verdict, the album's, the artist's. An album's rating is the
user's verdict on the ALBUM, never the average of its tracks' ratings.
"""
from __future__ import annotations

import os
import re
import time
from collections import Counter
from typing import Any, Callable, Dict, Iterable, Iterator, List, NamedTuple, Optional, Sequence, Tuple

TARGETS = ("tracks", "albums")

# --------------------------------------------------------------------------- #
# Operands
# --------------------------------------------------------------------------- #
def as_num(v) -> Optional[float]:
    """The value as a float, or None when it is not number-like."""
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _pair(a, b):
    """Both operands as numbers when both are numeric, else as text.

    A client sends ``value`` as a string while tag/tech values can be
    numeric, so comparing the raw operands raises TypeError
    (``2020 < "1"``)."""
    na, nb = as_num(a), as_num(b)
    if na is not None and nb is not None:
        return na, nb
    return ("" if a is None else str(a)), ("" if b is None else str(b))


def _ord_cmp(op):
    def run(a, b):
        if a is None or b is None:
            return False
        x, y = _pair(a, b)
        return op(x, y)
    return run


def _is_seq(v) -> bool:
    """A multi-value operand: the builder sends a list for "is any of" and
    for a range, and never for a single comparison."""
    return isinstance(v, (list, tuple))


def _eq1(a, b) -> bool:
    x, y = _pair(a, b)
    return x == y


def _eq(a, b) -> bool:
    if a is None:
        return False
    if _is_seq(b):
        return any(_eq1(a, v) for v in b)
    return _eq1(a, b)


def _ne(a, b) -> bool:
    if _is_seq(b):
        return not any(_eq1(a, v) for v in b)
    x, y = _pair(a, b)
    return x != y


def _contains(a, b) -> bool:
    if a is None or b is None:
        return False
    if _is_seq(b):
        return any(_contains(a, v) for v in b)
    return str(b).lower() in str(a).lower()


def _leading_year(v) -> Optional[str]:
    """The 4-digit year a value starts with ('2003-04-01' -> '2003'), else None."""
    text = str(v).strip()
    return text[:4] if len(text) >= 4 and text[:4].isdigit() else None


def _between(a, b) -> bool:
    """Inclusive range — ONE condition, because a range row split into
    ``gte`` + ``lte`` turns into "everything" under ``match: "any"``.

    Numeric when every operand is number-like, else text. The one exception is
    a range whose BOTH bounds are bare years against a value that starts with a
    year: that compares years, so "2002..2003" includes "2003-04-01" instead of
    silently dropping most of its own end year."""
    if a is None or not _is_seq(b) or len(b) != 2:
        return False
    lo, hi = b
    if lo is None or hi is None:
        return False
    na, nlo, nhi = as_num(a), as_num(lo), as_num(hi)
    if na is not None and nlo is not None and nhi is not None:
        return nlo <= na <= nhi
    year = _leading_year(a)
    if (year is not None and len(str(lo).strip()) == 4
            and len(str(hi).strip()) == 4):
        ylo, yhi = _leading_year(lo), _leading_year(hi)
        if ylo is not None and yhi is not None:
            return ylo <= year <= yhi
    x, y, z = str(a), str(lo), str(hi)
    return y <= x <= z


_REGEX_MAX = 256
_regex_cache: Dict[str, Any] = {}


def _matches(a, b) -> bool:
    """Case-insensitive regex search. A pattern that does not compile is a
    client bug the API refuses with 400; here it simply never matches, so a
    bad saved playlist shows an empty list instead of a 500."""
    if a is None or b is None:
        return False
    if _is_seq(b):
        return any(_matches(a, v) for v in b)
    pattern = str(b)
    if len(pattern) > _REGEX_MAX:
        return False
    rx = _regex_cache.get(pattern)
    if rx is None:
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error:
            return False
        if len(_regex_cache) >= _REGEX_MAX:
            _regex_cache.clear()
        _regex_cache[pattern] = rx
    return rx.search(str(a)) is not None


def _missing(a, b) -> bool:
    return a is None or str(a).strip() == ""


def _present(a, b) -> bool:
    return a is not None and str(a).strip() != ""


def _issues_contain(a, b) -> bool:
    """*a* is the row's own issue-code list (see :func:`_row_issues`)."""
    if not a:
        return False
    if _is_seq(b):
        return any(_issues_contain(a, v) for v in b)
    want = str(b).strip().upper()
    if not want:
        return False
    return any(want in str(c).upper() for c in a)


_OPS: Dict[str, Callable[[Any, Any], bool]] = {
    "eq": _eq,
    "ne": _ne,
    "lt": _ord_cmp(lambda x, y: x < y),
    "gt": _ord_cmp(lambda x, y: x > y),
    "lte": _ord_cmp(lambda x, y: x <= y),
    "gte": _ord_cmp(lambda x, y: x >= y),
    "contains": _contains,
    "missing": _missing,
    "present": _present,
    "between": _between,
    "matches": _matches,
    "issues_contain": _issues_contain,
}

# The operator catalogue: the ONE vocabulary of condition ids, with the label
# the query builder and the smart-playlist editor both render.
OPERATORS: Dict[str, str] = {
    "eq": "is",
    "ne": "is not",
    "lt": "is less than",
    "gt": "is more than",
    "lte": "is at most",
    "gte": "is at least",
    "between": "is between",
    "contains": "contains",
    "matches": "matches regex",
    "missing": "is empty",
    "present": "is not empty",
    "issues_contain": "has issue",
    "is_unrated": "is unrated",
    "is_rated": "is rated",
}

# Which ops a field advertises, by its declared type. The evaluator accepts
# every op for every field (a saved smart playlist's op must keep working);
# this decides only what the builder OFFERS.
OPS_FOR_TYPE: Dict[str, Tuple[str, ...]] = {
    "text": ("eq", "ne", "contains", "matches", "missing", "present"),
    "number": ("eq", "ne", "lt", "gt", "lte", "gte", "between",
               "missing", "present"),
    "enum": ("eq", "ne", "missing", "present"),
    "bool": ("eq", "ne"),
    "issues": ("issues_contain", "missing", "present"),
    "rating": ("eq", "ne", "lt", "gt", "lte", "gte", "between",
               "is_unrated", "is_rated"),
}


def ops_for(kind: str, extra: Sequence[str] = ()) -> List[Dict[str, str]]:
    """The ``ops`` array a field carries: [{"op","label"}, …].

    Extra ids (a date field's range, say) keep the label lookup in one place."""
    ids = tuple(OPS_FOR_TYPE.get(kind, OPS_FOR_TYPE["text"])) + tuple(extra)
    return [{"op": o, "label": OPERATORS[o]} for o in ids]


# --------------------------------------------------------------------------- #
# Rows — one walk over the library payload feeds everything
# --------------------------------------------------------------------------- #
class Row(NamedTuple):
    """A row being queried: what field resolution reads.

    ``track`` is None for an album-target row. Keeping the three payload dicts
    by reference is the point — nothing is copied per row, so a 100k-track
    walk allocates three pointers per row instead of a merged record."""
    track: Optional[dict]
    album: Optional[dict]
    artist: Optional[dict]

    @property
    def payload(self) -> dict:
        """The dict the library page renders for this row."""
        return self.track if self.track is not None else (self.album or {})


_EMPTY: dict = {}


def walk(library, target: str = "tracks", paths: Optional[Iterable[str]] = None
         ) -> Iterator[Tuple[Row, dict]]:
    """Every row in the payload, once, in payload order.

    ``paths`` limits the walk to a set of track paths (a smart playlist
    applied to a subset); the set is built once, so a 100k base list is not
    scanned per track."""
    keep = None if paths is None else {str(p) for p in paths}
    for artist in library.get("artists") or ():
        for album in artist.get("albums") or ():
            if target == "albums":
                yield Row(None, album, artist), album
                continue
            for track in album.get("tracks") or ():
                if keep is not None and track.get("path") not in keep:
                    continue
                yield Row(track, album, artist), track


def _norm_path(p) -> str:
    return str(p or "").replace("\\", "/")


def _base(path) -> str:
    return os.path.basename(str(path or "").rstrip("/\\"))


# --------------------------------------------------------------------------- #
# Field resolution
# --------------------------------------------------------------------------- #
def _tags_of(row: Row) -> dict:
    """A track's own tags; an album's ``meta`` with ``album_values`` under it.

    Tracks read ONLY their own tags — that is what the smart-playlist
    evaluator always did, and an album-wide fallback would quietly widen a
    saved playlist's matches."""
    if row.track is not None:
        return row.track.get("tags") or _EMPTY
    album = row.album or _EMPTY
    return album.get("meta") or _EMPTY


def _album_tag(row: Row, key: str):
    """A tag as an ALBUM fact: the album's own meta, its graded album values,
    else its first track's tags.

    The payload's album meta is itself read from the album's first readable
    track (``server.library._album_meta``), so the third step is the same fact
    for the tags the meta does not carry — without it a genre filter on albums
    would match nothing while every one of their tracks is tagged."""
    album = row.album or _EMPTY
    value = (album.get("meta") or _EMPTY).get(key)
    if value is None:
        value = (album.get("album_values") or _EMPTY).get(key)
    if value is None:
        for track in album.get("tracks") or ():
            value = (track.get("tags") or _EMPTY).get(key)
            if value is not None:
                break
    return value


def _tech_of(row: Row) -> dict:
    if row.track is None:
        return _EMPTY
    return row.track.get("tech") or _EMPTY


def _track_or_empty(row: Row) -> dict:
    return row.track or _EMPTY


def _row_issues(row: Row):
    """The row's own issue codes. A track carries a list; an album carries
    ``{code: [files]}``. Nothing is folded across levels: "has COVER" on an
    album is the album's verdict, not each of its tracks'."""
    if row.track is not None:
        return row.track.get("issues") or ()
    return tuple((row.album or _EMPTY).get("issues") or ())


def _audit_of(row: Row):
    """The row's OWN audit verdict.

    No cross-level fallback: a track answers with its own verdict and an album
    with its summary, which is what the original evaluator resolved and what
    the track page shows. The album's verdict is ``album.audit`` (readable from
    a track row too), the artist rollup ``artist.audit`` — one name per fact."""
    if row.track is not None:
        return row.track.get("audit")
    return (row.album or _EMPTY).get("audit_summary")


def _year_of(value):
    """The year out of a DATE/ORIGINALDATE value: '2020-05-01' -> '2020'.
    Anything that does not start with a year stays as it is, so junk shows up
    as junk instead of an empty bucket."""
    text = str(value or "").strip()
    return text[:4] if len(text) >= 4 and text[:4].isdigit() else text


def _basename(path, folder):
    """Folder name of a library path — the same name the payload's artist row
    carries under ``name``, derived for albums (which have none)."""
    return folder or _base(path)


def _library_value(row: Row, key: str):
    """``library.<KEY>``: the payload's own per-row facts."""
    track = _track_or_empty(row)
    album = row.album or _EMPTY
    if key == "path":
        return _norm_path(track.get("path") if row.track is not None
                          else album.get("path"))
    if key == "file":
        return track.get("file")
    if key == "title":
        return _tags_of(row).get("TITLE") if row.track is not None else None
    if key == "tracknumber":
        return track.get("tracknumber") if row.track is not None else None
    if key == "discnumber":
        return track.get("discnumber") if row.track is not None else None
    if key == "is_video":
        return bool(track.get("is_video")) if row.track is not None else None
    if key == "unreadable":
        return bool(track.get("unreadable")) if row.track is not None else None
    if key == "lyrics_present":
        if row.track is not None:
            return bool(track.get("lyrics_present"))
        # The album row's own lyrics_present is a COUNT of its tracks.
        return bool(album.get("lyrics_present"))
    if key == "lyrics_embedded":
        return bool(track.get("lyrics_embedded")) if row.track is not None else None
    if key == "lyrics_lrc":
        return bool(track.get("lyrics_lrc")) if row.track is not None else None
    if key == "has_log":
        return bool(album.get("has_log")) if album else None
    if key == "has_cue":
        return bool(album.get("has_cue")) if album else None
    if key == "cover_file":
        # The cover this row SHOWS: a sidecar next to the track, else the
        # album's own. Reading only the track's field would report every track
        # of a normally-covered album as cover-less.
        own = track.get("cover_file") if row.track is not None else None
        return own or album.get("cover_file") or None
    if key == "pending":
        return bool(album.get("pending")) if album else None
    if key == "partial":
        return bool(album.get("partial")) if album else None
    return None


def _album_value(row: Row, key: str):
    album = row.album or _EMPTY
    if key == "name":
        return _basename(album.get("path"), album.get("name")) if album else None
    if key == "path":
        return _norm_path(album.get("path")) if album else None
    if key == "album_artist":
        if album.get("album_artist"):
            return album.get("album_artist")
        return _album_tag(row, "ALBUMARTIST") or _album_tag(row, "ARTIST")
    if key == "year":
        return _year_of(_album_tag(row, "DATE") or _album_tag(row, "ORIGINALDATE")) or None
    if key == "genre":
        return _album_tag(row, "GENRE")
    if key == "media":
        return _album_tag(row, "MEDIA")
    if key == "track_count":
        return album.get("track_count")
    if key == "audit":
        return album.get("audit_summary") if album else None
    if key == "has_log":
        return bool(album.get("has_log")) if album else None
    if key == "has_cue":
        return bool(album.get("has_cue")) if album else None
    if key == "pending":
        return bool(album.get("pending")) if album else None
    if key == "partial":
        return bool(album.get("partial")) if album else None
    return None


def _artist_value(row: Row, key: str):
    artist = row.artist or _EMPTY
    agg = artist.get("aggregate") or _EMPTY
    if key == "name":
        return _basename(artist.get("path"), artist.get("name")) if artist else None
    if key == "path":
        return _norm_path(artist.get("path")) if artist else None
    if key == "album_count":
        return agg.get("album_count")
    if key == "track_count":
        return agg.get("track_count")
    if key == "grade_pct":
        return agg.get("grade_pct")
    if key == "audit":
        return agg.get("audit_summary")
    return None


def _grade_pct(row: Row):
    """Grade percentage — the ALBUM's, for both targets: a track has no checks
    of its own, and the album row's own ``grade_pct`` is that same number."""
    return (row.album or _EMPTY).get("grade_pct")


# Bare keys that name a field of their own rather than a payload key. They
# are the sort keys the API contract names ("sort":{"key":"album"}).
ALIASES = {
    "album": "tags.ALBUM",
    "artist": "tags.ARTIST",
    "title": "tags.TITLE",
    "genre": "tags.GENRE",
    "year": "tags.DATE",
    "path": "library.path",
    "rating": "rating",
    "is_video": "library.is_video",
    "unreadable": "library.unreadable",
    "lyrics_present": "library.lyrics_present",
    "has_log": "library.has_log",
    "has_cue": "library.has_cue",
    "grade_pct": "grade_pct",
    "grade_pass": "grade_pass",
    "audit": "audit",
    "issues": "issues",
}

_RATING_HALF = 2.0

# The three fields that ARE a rating — one unit (stars, halves), one op family,
# three scopes. The album/artist two resolve through the store's folder half.
# Public: `server.playlists` reads it to know whether a smart playlist's spec
# needs the store at all.
RATING_FIELDS = ("rating", "album.rating", "artist.rating")
_ENTITY_RATING_FIELDS = RATING_FIELDS[1:]


def _rating_getter(rating_of: Optional[Callable]):
    def get(row: Row):
        if row.track is None or rating_of is None:
            return None
        raw = (row.track.get("tags") or _EMPTY).get("RATING")
        half = rating_of(_norm_path(row.track.get("path")), raw)
        if not half:
            return None
        return float(half) / _RATING_HALF
    return get


def _entity_rating_getter(scope: str, rating_of: Optional[Callable]):
    """``album.rating`` / ``artist.rating`` — the user's verdict on the ENTITY.

    Deliberately NOT the average of the tracks' own ratings: that is a
    different number, drawn beside this one and labelled, and the two must
    never be confusable. Same unit and the same op semantics as ``rating``
    (stars, halves; unrated is no value at all), read from the folder half of
    the injected store — which is asked once per row and answers the rows
    where the row has no such entity at all with None."""
    folder_of = getattr(rating_of, "folder", None)
    if folder_of is None:
        # A source that predates the folder scopes (or a store with no folder
        # rows): nothing is rated there, which is all it can prove.
        return lambda row: None

    def get(row: Row):
        entity = row.album if scope == "album" else row.artist
        path = _norm_path((entity or _EMPTY).get("path"))
        if not path:
            return None
        half = folder_of(scope, path)
        if not half:
            return None
        return float(half) / _RATING_HALF
    return get


def field_getter(field: str, rating_of: Optional[Callable] = None
                 ) -> Callable[[Row], Any]:
    """Compile *field* into a ``row -> value`` callable, once per request.

    Unknown fields resolve to None (the row simply never matches), which is
    what the original evaluator did; the API rejects them for ad-hoc queries
    before this is ever reached."""
    name = ALIASES.get(field, field)
    if name == "rating":
        return _rating_getter(rating_of)
    if name in _ENTITY_RATING_FIELDS:
        return _entity_rating_getter(name.split(".", 1)[0], rating_of)
    scope, _, key = name.partition(".")
    if scope == "tags":
        if not key:
            return lambda row: None
        def tags_get(row: Row):
            if row.track is not None:
                return (row.track.get("tags") or _EMPTY).get(key)
            return _album_tag(row, key)
        return tags_get
    if scope == "tech":
        if not key:
            return lambda row: None
        return lambda row: _tech_of(row).get(key)
    if scope == "analysis":
        if not key:
            return lambda row: None
        def analysis_get(row: Row):
            v = (_track_or_empty(row).get("analysis") or _EMPTY).get(key)
            if v is None:
                v = (_track_or_empty(row).get("tags") or _EMPTY).get(key)
            return v
        return analysis_get
    if scope == "library":
        return lambda row: _library_value(row, key)
    if scope == "album":
        return lambda row: _album_value(row, key)
    if scope == "artist":
        return lambda row: _artist_value(row, key)
    if name == "grade_pass":
        return lambda row: (bool(row.track.get("grade_pass"))
                            if row.track is not None
                            else bool((row.album or _EMPTY).get("pass")))
    if name == "grade_pct":
        return _grade_pct
    if name == "audit":
        return _audit_of
    if name == "issues":
        return _row_issues
    if name == "path":
        return lambda row: _norm_path(row.payload.get("path"))

    # A bare key: the payload's own key first, exactly as the original
    # evaluator resolved it (a two-level lookup, not a merged record).
    def bare(row: Row):
        if row.track is None:
            return None
        tags = row.track.get("tags") or _EMPTY
        if field in tags:
            return tags[field]
        if field in row.track:
            return row.track.get(field)
        return (row.track.get("tech") or _EMPTY).get(field)
    return bare


# --------------------------------------------------------------------------- #
# Conditions
# --------------------------------------------------------------------------- #
def _as_bool_text(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    text = str(v).strip().lower()
    return "true" if text in ("true", "1", "yes", "y", "on") else "false"


_RATING_STATE = {
    "is_unrated": lambda stars: stars is None,
    "is_rated": lambda stars: stars is not None,
}


def compile_conditions(conditions: Sequence[dict], target: str = "tracks",
                       rating_of: Optional[Callable] = None,
                       bool_fields: Iterable[str] = ()) -> List[Callable[[Row], bool]]:
    """Compile the condition list into one predicate per condition."""
    bools = frozenset(bool_fields)
    out: List[Callable[[Row], bool]] = []
    for cond in conditions or ():
        field = str(cond.get("field") or "")
        op = str(cond.get("op") or "eq")
        value = cond.get("value")
        get = field_getter(field, rating_of)
        if ALIASES.get(field, field) in RATING_FIELDS and op in _RATING_STATE:
            state = _RATING_STATE[op]
            out.append(lambda row, get=get, state=state: state(get(row)))
            continue
        fn = _OPS.get(op)
        if fn is None:
            # Unknown op: matches nothing, as it always has. The API rejects
            # it up front for ad-hoc queries.
            out.append(lambda row: False)
            continue
        if field in bools and op in ("eq", "ne"):
            # A JSON `true` renders as "True" while the payload's bool renders
            # as "False": coerce both sides, but ONLY for a comparison — an
            # emptiness test must still see the value as it is (a False bool is
            # a value, not an empty one).
            out.append(lambda row, get=get, fn=fn, v=value: fn(
                _as_bool_text(get(row)), _as_bool_text(v)))
        else:
            out.append(lambda row, get=get, fn=fn, v=value: fn(get(row), v))
    return out


def compile_spec(spec: dict, target: str = "tracks",
                 rating_of: Optional[Callable] = None,
                 bool_fields: Iterable[str] = ()) -> Callable[[Row], bool]:
    """One predicate for a whole filter spec (``all``/``any``), as the
    smart-playlist evaluator and the browser query both need it.

    An empty condition list matches everything (an empty rule is "all your
    music"), which is what smart playlists already did."""
    predicates = compile_conditions((spec or {}).get("conditions") or [],
                                    target, rating_of, bool_fields)
    match_all = (spec or {}).get("match", "all") != "any"
    if not predicates:
        return lambda row: True
    if match_all:
        return lambda row: all(p(row) for p in predicates)
    return lambda row: any(p(row) for p in predicates)


# --------------------------------------------------------------------------- #
# Grouping
# --------------------------------------------------------------------------- #
GROUP_KEYS = ("artist", "album", "genre", "year", "rating")

_UNRATED = "Unrated"


def _group_artist(row: Row):
    tags = _tags_of(row)
    for key in ("ARTIST", "ALBUMARTIST"):
        v = tags.get(key) if row.track is not None else _album_tag(row, key)
        if v:
            return str(v)
    album = row.album or _EMPTY
    if album.get("album_artist"):
        return str(album["album_artist"])
    return _basename((row.artist or _EMPTY).get("path"), (row.artist or _EMPTY).get("name"))


def _group_album(row: Row):
    if row.track is not None:
        v = (row.track.get("tags") or _EMPTY).get("ALBUM")
        if v:
            return str(v)
        if (row.album or _EMPTY).get("meta"):
            v = (row.album or _EMPTY)["meta"].get("ALBUM")
            if v:
                return str(v)
    return _basename((row.album or _EMPTY).get("path"), (row.album or _EMPTY).get("name"))


def _group_rating(rating_of: Optional[Callable]):
    get = _rating_getter(rating_of)

    def key(row: Row):
        stars = get(row)
        if stars is None:
            return _UNRATED
        return f"{stars:g}"
    return key


def group_getter(name: str, target: str = "tracks",
                 rating_of: Optional[Callable] = None) -> Optional[Callable[[Row], str]]:
    """Compile a group key into ``row -> key`` ("" when the field is empty)."""
    if name == "artist":
        return _group_artist
    if name == "album":
        return _group_album
    if name == "rating":
        return _group_rating(rating_of)
    if name == "genre":
        get = field_getter("tags.GENRE")
    elif name == "year":
        base = field_getter("tags.DATE")
        album_base = field_getter("album.year")

        def get(row: Row):
            v = base(row)
            if v is None or str(v).strip() == "":
                v = album_base(row)
            return _year_of(v)
    else:
        return None
    return lambda row: ("" if get(row) is None else str(get(row)))


# --------------------------------------------------------------------------- #
# Sorting
# --------------------------------------------------------------------------- #
def sort_getter(key: str, target: str = "tracks",
                rating_of: Optional[Callable] = None) -> Callable[[Row], Any]:
    if key in GROUP_KEYS:
        g = group_getter(key, target, rating_of)
        if g is not None:
            return g
    return field_getter(key, rating_of)


def _sort_tuple(value) -> tuple:
    """A total order over a field whose values mix numbers, text and gaps:
    blanks last, numbers before text, case-insensitive for text."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return (1, 0.0, "")
    n = as_num(value)
    if n is not None:
        return (0, n, "")
    return (0, 0.0, str(value).lower())


def order(rows: List[Row], keys: Optional[Sequence[str]] = None,
          desc: bool = False, rating_of: Optional[Callable] = None,
          group: Optional[str] = None, target: str = "tracks") -> List[Row]:
    """Sort in place-stable fashion, cheaply.

    Rows are decorated once with their sort tuple (one field read per row, not
    one per comparison), and an empty value sorts last whichever direction was
    asked for — a table sorted by bitrate descending has no reason to put
    "no bitrate" on top."""
    getters = [sort_getter(k, target, rating_of) for k in (keys or ())]
    decorated = []
    for row in rows:
        tup = tuple(_sort_tuple(g(row)) for g in getters)
        decorated.append((tup, row))
    if getters:
        decorated.sort(key=lambda pair: pair[0], reverse=bool(desc))
        # Blanks last in both directions: a stable pass over the rare rows.
        decorated.sort(key=lambda pair: 0 if pair[0][0][0] == 0 else 1)
    return [row for _, row in decorated]


def order_by_group(rows: List[Row], get: Callable[[Row], str]) -> List[Row]:
    """Grouped order: group key ascending, payload order inside a group (the
    sort is stable), so the rows a client folds together are contiguous."""
    decorated = [(get(r), r) for r in rows]
    decorated.sort(key=lambda pair: str(pair[0]).lower())
    return [row for _, row in decorated]


# --------------------------------------------------------------------------- #
# Facets and group counts
# --------------------------------------------------------------------------- #
def bucket_key(value) -> Optional[str]:
    """A facet bucket: the value as text, None for blank/absent."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip()
    return text or None


def count_values(rows: Iterable[Row], get: Callable[[Row], Any]) -> Tuple[Counter, int]:
    """One pass over *rows*: value buckets + the blank count."""
    counts: Counter = Counter()
    missing = 0
    for row in rows:
        key = bucket_key(get(row))
        if key is None:
            missing += 1
        else:
            counts[key] += 1
    return counts, missing


def facet_values(counts: Counter, limit: int = 50, q: Optional[str] = None) -> dict:
    """The values array a facet answers with.

    A facet whose every bucket is numeric answers with numbers (so the client
    compares, not string-matches); a facet that mixes numbers and text stays
    text, where string comparison is the only honest one."""
    numeric = bool(counts) and all(as_num(k) is not None for k in counts)
    rows = [{"value": float(k) if numeric else k, "count": c}
            for k, c in counts.items()]
    if q:
        needle = str(q).strip().lower()
        rows = [v for v in rows if needle in str(v["value"]).lower()]
    # Count desc, then value asc: a stable, meaningful top-N.
    rows.sort(key=lambda v: (-v["count"], str(v["value"]).lower()))
    return {"values": rows[:limit], "total_values": len(rows)}


def facet(rows: Iterable[Row], field: str, limit: int = 50,
          q: Optional[str] = None, rating_of: Optional[Callable] = None) -> dict:
    """Value counts for one field over the rows given.

    Blank values are counted separately (``missing``) rather than as a bucket,
    so a value picker lists values and the client can still say "42 rows have
    none"."""
    counts, missing = count_values(rows, field_getter(field, rating_of))
    out = facet_values(counts, limit, q)
    return {"field": field, "values": out["values"],
            "total_values": out["total_values"], "missing": missing}


# --------------------------------------------------------------------------- #
# The query
# --------------------------------------------------------------------------- #
DEFAULT_LIMIT = 200
MAX_LIMIT = 10000
FACET_LIMIT = 50


def run(library, req: dict, rating_of: Optional[Callable] = None,
        bool_fields: Iterable[str] = ()) -> dict:
    """Answer one browser query against the library payload.

    ONE walk: every condition, the group key and the facet counters are read
    off the same row in the same pass, so nothing is re-derived per condition
    and the payload is never indexed by path."""
    started = time.perf_counter()
    req = req or {}
    target = "albums" if str(req.get("target") or "tracks") == "albums" else "tracks"
    spec = {"conditions": req.get("conditions") or [],
            "match": req.get("match") or "all"}
    group = req.get("group") or None
    if group not in GROUP_KEYS:
        group = None
    predicate = compile_spec(spec, target, rating_of, bool_fields)
    group_of = group_getter(group, target, rating_of) if group else None

    facets = [f for f in facet_fields(req) if f]
    facet_getters = [(f, field_getter(f, rating_of)) for f in facets]
    facet_counts: Dict[str, Counter] = {f: Counter() for f in facets}
    facet_missing: Dict[str, int] = {f: 0 for f in facets}
    group_counter: Counter = Counter()

    rows: List[Row] = []
    for row, _payload in walk(library, target, req.get("paths")):
        if not predicate(row):
            continue
        rows.append(row)
        for field, get in facet_getters:
            key = bucket_key(get(row))
            if key is None:
                facet_missing[field] += 1
            else:
                facet_counts[field][key] += 1
        if group_of is not None:
            group_counter[group_of(row)] += 1

    total = len(rows)
    sort_spec = req.get("sort") or {}
    keys = sort_spec.get("key")
    if keys:
        keys = [keys] if isinstance(keys, str) else list(keys)
        rows = order(rows, keys, int(sort_spec.get("dir") or 1) < 0,
                     rating_of, target=target)
    elif group_of is not None:
        rows = order_by_group(rows, group_of)

    limit = req.get("limit")
    limit = DEFAULT_LIMIT if limit is None else int(limit)
    limit = max(0, min(limit, MAX_LIMIT))
    offset = max(0, int(req.get("offset") or 0))
    page = rows[offset:offset + limit] if limit else []

    items = []
    for row in page:
        item = dict(row.payload)
        artist = row.artist or _EMPTY
        album = row.album or _EMPTY
        item["artist"] = _basename(artist.get("path"), artist.get("name"))
        item["artist_path"] = _norm_path(artist.get("path"))
        item["album"] = _basename(album.get("path"), album.get("name"))
        item["album_path"] = _norm_path(album.get("path"))
        if group_of is not None:
            item["group"] = group_of(row)
        items.append(item)

    facet_limit = max(0, int(req.get("facet_limit") or FACET_LIMIT))
    out_facets = []
    for field in facets:
        values = facet_values(facet_counts[field], facet_limit)
        out_facets.append({"field": field, "values": values["values"],
                           "total_values": values["total_values"],
                           "missing": facet_missing[field]})

    out = {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "group_counts": ([{"key": k, "count": c} for k, c in group_counter.items()]
                         if group_of is not None else []),
        "facets": out_facets,
        "took_ms": round((time.perf_counter() - started) * 1000.0, 2),
    }
    out["group_counts"].sort(key=lambda g: (-g["count"], str(g["key"]).lower()))
    return out



def facet_fields(req: dict) -> List[str]:
    raw = (req or {}).get("facets")
    if raw is None:
        raw = (req or {}).get("facet")
    if not raw:
        return []
    if isinstance(raw, str):
        return [raw]
    return [str(f) for f in raw]


# --------------------------------------------------------------------------- #
# Smart playlists
# --------------------------------------------------------------------------- #
def match_paths(library, spec: dict, base_paths: Optional[Iterable[str]] = None,
                rating_of: Optional[Callable] = None,
                bool_fields: Iterable[str] = ()) -> List[str]:
    """The paths a smart playlist's spec selects, in payload order.

    This is the original ``server.playlists.evaluate_smart`` body — same walk,
    same ``base_paths`` filter applied before the conditions, same order — now
    the shared implementation."""
    predicate = compile_spec(spec or {}, "tracks", rating_of, bool_fields)
    hits: List[str] = []
    for row, _payload in walk(library, "tracks", base_paths):
        if predicate(row):
            hits.append(row.track.get("path"))
    return hits
