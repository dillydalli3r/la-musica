"""Ad-hoc library queries — one engine, one field catalogue.

GET  /api/library/fields   the ONE catalogue: every field the library payload
                           holds, grouped for a builder UI. The smart-playlist
                           rule editor renders this too, so a field is defined
                           once and can never drift between the two.
POST /api/library/query    conditions + sort + group + facets, answered by
                           ``mlo.query`` against the SAME payload the library
                           page renders (``server.library.build_library`` is
                           TTL-cached, so a query re-reads no tags).
GET  /api/library/facets   value counts for one field, one walk, no per-row
                           tag read.

Nothing here evaluates a condition: ``mlo.query`` owns the operator semantics,
so a saved smart playlist and an ad-hoc query answer identically. This module
owns the CATALOGUE (what a client may ask for) and turns a request that names
something that does not exist into a 400 instead of a silently empty answer.
"""
from __future__ import annotations

import re
import threading
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from fastapi import APIRouter, HTTPException, Query, Request

from mlo import query as query_mod
from server import library as lib_mod
from server.auth import current_user
from server.tags_registry import registry

router = APIRouter()

# --------------------------------------------------------------------------- #
# The catalogue
# --------------------------------------------------------------------------- #
# Group order is the order a builder renders: what the user browses by first.
GROUPS: Tuple[Tuple[str, str], ...] = (
    ("tags", "Tags"),
    ("rating", "Rating"),
    ("grade", "Grade"),
    ("audit", "Audit"),
    ("tech", "Technical"),
    ("analysis", "Audio analysis"),
    ("library", "Library"),
    ("album", "Album"),
    ("artist", "Artist"),
)

ALL_TARGETS = ("tracks", "albums")

# Tag keys whose values are numbers on disk. Everything else with no closed
# value set is text; a registry enum makes it an enum with those options.
_NUMERIC_TAGS = frozenset({
    "BPM", "DYNAMIC RANGE", "ALBUM DYNAMIC RANGE", "ENERGY", "ORIGINALYEAR",
    "REPLAYGAIN_TRACK_GAIN", "REPLAYGAIN_TRACK_PEAK",
    "REPLAYGAIN_ALBUM_GAIN", "REPLAYGAIN_ALBUM_PEAK",
    "RATEYOURMUSIC_ALBUM", "RATEYOURMUSIC_TRACK", "RATEYOURMUSIC_ARTIST",
})

# Dates: text (an ISO date and a bare year are both text), but they order and
# range correctly — a year range is one of the few queries a music library is
# browsed by, so `between` must be offered here.
_ORDINAL_TEXT = frozenset({"DATE", "ORIGINALDATE"})
_ORDINAL_OPS = ("lt", "gt", "lte", "gte", "between")

# Ops the original smart-playlist evaluator accepted for ANY field. They stay
# accepted here: a stored rule must keep evaluating even where the catalogue
# does not offer the op for that field.
LEGACY_OPS = frozenset({"eq", "ne", "lt", "gt", "lte", "gte", "contains",
                        "missing", "present"})

# The analysis fields, read out of the payload's own tags (script 7 / 8 write
# them): key -> (label, type, unit).
_ANALYSIS = (
    ("DYNAMIC RANGE", "Dynamic range", "number", "dB"),
    ("BPM", "BPM", "number", None),
    ("INITIALKEY", "Initial key", "text", None),
    ("MOOD", "Mood", "enum", None),
    ("ENERGY", "Energy", "number", None),
)

_TECH = (
    ("bitrate", "Bitrate", "number", "kbps", True),
    ("sample_rate", "Sample rate", "number", "Hz", True),
    ("bits_per_sample", "Bit depth", "number", "bits", True),
    ("length", "Length", "number", "s", False),
    ("channels", "Channels", "number", None, True),
    ("codec", "Codec", "text", None, True),
)


def _field(name: str, label: str, kind: str, *, targets=ALL_TARGETS,
           values: Optional[Sequence[Any]] = None, unit: Optional[str] = None,
           vmin: Optional[float] = None, vmax: Optional[float] = None,
           step: Optional[float] = None, facetable: bool = False,
           sortable: bool = True, in_payload: bool = True, hint: str = "",
           extra_ops: Sequence[str] = (), ops: Optional[Sequence[str]] = None) -> dict:
    """One catalogue entry.

    ``ops`` replaces the type's own op list for a field whose ops are its own
    thing (issue codes); otherwise the type decides and ``extra_ops`` adds."""
    out = {
        "field": name,
        "label": label,
        "type": kind,
        "ops": query_mod.ops_for(kind, extra_ops) if ops is None
               else [{"op": o, "label": query_mod.OPERATORS[o]} for o in ops],
        "facetable": bool(facetable),
        "sortable": bool(sortable),
        "targets": list(targets),
        "in_payload": bool(in_payload),
    }
    if values:
        out["values"] = [str(v) for v in values]
    elif kind == "bool":
        # The two values the engine compares for a boolean field. Stated here
        # so a value control renders a select instead of a free-text box the
        # user has to spell right.
        out["values"] = ["true", "false"]
    if unit:
        out["unit"] = unit
    if vmin is not None:
        out["min"] = vmin
    if vmax is not None:
        out["max"] = vmax
    if step is not None:
        out["step"] = step
    if hint:
        out["hint"] = hint
    return out


def _tag_fields() -> List[dict]:
    """Every tag the registry knows, as ``tags.<KEY>``.

    The registry is the tag source (labels, meanings, closed value sets, and
    the family the tag page files it under); this adds only the two facts the
    registry cannot state — the type a VALUE has, and whether the library
    payload actually carries the tag yet. ``in_payload`` is read from
    ``server.library.TRACK_TAGS``, the list the payload builder reads, so a
    tag nothing reads is visible as such instead of quietly matching
    nothing."""
    out = []
    carried = set(lib_mod.TRACK_TAGS)
    for tag in registry()["tags"]:
        key = tag["key"]
        if tag["enum"]:
            kind = "enum"
        elif key in _NUMERIC_TAGS:
            kind = "number"
        else:
            kind = "text"
        extra = _ORDINAL_OPS if key in _ORDINAL_TEXT else ()
        out.append(_field(
            f"tags.{key}", tag["label"], kind,
            values=tag["enum"], facetable=kind == "enum",
            in_payload=key in carried, hint=tag["meaning"],
            extra_ops=extra,
        ))
    return out


# The one op set every rating field shares: the comparisons, the range, and the
# two emptiness tests that ARE a rating's own (is_unrated/is_rated) — so the
# generic number ops (missing/present) are not offered on top. Shared by the
# track field and the two entity fields: they are one family in three scopes
# and must never advertise different answers.
_RATING_OPS = ("eq", "ne", "lt", "gt", "lte", "gte", "between",
               "is_unrated", "is_rated")


def _rating_field() -> dict:
    """The track rating field. Stars (halves), from the rating store the server
    layer injects — never from the file's RATING tag, which is Picard's 0-100
    scale."""
    # Explicit ops: is_unrated/is_rated ARE this field's emptiness test.
    return _field("rating", "Rating", "number", targets=("tracks",),
                  vmin=0, vmax=5, step=0.5, facetable=True, ops=_RATING_OPS,
                  hint="Stars (halves), 0-5. Unrated tracks carry no value.")


def _entity_rating_field(scope: str, label: str, hint: str) -> dict:
    """One folder-scope rating field (``album.rating`` / ``artist.rating``).

    The USER's verdict on that entity, out of the same store and in the same
    unit and ops as ``rating`` — the average of the tracks' ratings is a
    different number and is NOT what this field holds. Answerable from a track
    row too (as ``album.audit`` is): a track row reports the verdict on the
    album or artist it sits under."""
    return _field(f"{scope}.rating", label, "number", vmin=0, vmax=5, step=0.5,
                  facetable=True, ops=_RATING_OPS, hint=hint)


def _grade_fields() -> List[dict]:
    return [
        _field("grade_pct", "Grade %", "number", vmin=0, vmax=100,
               facetable=False, sortable=True,
               hint="Album grade percentage (a track has no checks of its own)."),
        _field("grade_pass", "Grade pass", "bool", facetable=True,
               hint="The row passed every enabled check."),
        _field("issues", "Issues", "text", sortable=False,
               ops=("issues_contain", "missing", "present"),
               hint="Issue codes the grader reported for this row."),
    ]


def _audit_field() -> dict:
    return _field("audit", "Audit verdict", "enum", facetable=True,
                  values=("REAL", "FAKE", "Mix"),
                  hint="This row's own audio-audit verdict.")


def _tech_fields() -> List[dict]:
    return [
        _field(f"tech.{key}", label, kind, targets=("tracks",), unit=unit,
               facetable=facet, sortable=True)
        for key, label, kind, unit, facet in _TECH
    ]


def _analysis_fields() -> List[dict]:
    values = {t["key"]: t["enum"] for t in registry()["tags"]}
    carried = set(lib_mod.TRACK_TAGS)
    out = []
    for key, label, kind, unit in _ANALYSIS:
        out.append(_field(f"analysis.{key}", label, kind, targets=("tracks",),
                          unit=unit, values=values.get(key),
                          facetable=kind == "enum", in_payload=key in carried))
    return out


def _library_fields() -> List[dict]:
    return [
        _field("library.path", "Path", "text", facetable=False),
        _field("library.file", "File name", "text", targets=("tracks",),
               facetable=False),
        _field("library.title", "Title", "text", targets=("tracks",),
               facetable=False),
        _field("library.tracknumber", "Track number", "number",
               targets=("tracks",)),
        _field("library.discnumber", "Disc number", "number",
               targets=("tracks",), facetable=True),
        _field("library.is_video", "Music video", "bool", targets=("tracks",),
               facetable=True),
        _field("library.unreadable", "Unreadable", "bool", targets=("tracks",),
               facetable=True),
        _field("library.lyrics_present", "Lyrics", "bool", facetable=True,
               hint="Any lyrics — embedded or a sidecar .lrc."),
        _field("library.lyrics_embedded", "Lyrics embedded", "bool",
               targets=("tracks",), facetable=True),
        _field("library.lyrics_lrc", "Lyrics sidecar", "bool",
               targets=("tracks",), facetable=True),
        _field("library.has_log", "Rip log", "bool", facetable=True,
               hint="Album fact: the folder holds a rip log."),
        _field("library.has_cue", "Cue sheet", "bool", facetable=True,
               hint="Album fact: the folder holds a cue sheet."),
        _field("library.cover_file", "Cover file", "text", facetable=False,
               hint="File name of the cover the row shows, if any."),
        _field("library.pending", "Placeholder album", "bool",
               targets=("albums",), facetable=True,
               hint="Added to the library, audio not imported yet."),
        _field("library.partial", "Partial import", "bool",
               targets=("albums",), facetable=True,
               hint="The release's tracklist is not all on disk."),
    ]


def _album_fields() -> List[dict]:
    return [
        _field("album.name", "Album folder", "text", facetable=False),
        _field("album.path", "Album path", "text", facetable=False),
        _field("album.album_artist", "Album artist", "text", facetable=False),
        _field("album.year", "Year", "number", facetable=True,
               extra_ops=_ORDINAL_OPS, hint="Year out of the album's DATE."),
        _field("album.genre", "Album genre", "text", facetable=False),
        _field("album.media", "Media", "enum", facetable=True,
               values=_enum_of("MEDIA")),
        _field("album.track_count", "Track count", "number"),
        _entity_rating_field(
            "album", "Album rating",
            "Stars (halves), 0-5: your own verdict on the album, not the "
            "average of its tracks' ratings."),
        _field("album.audit", "Album audit", "enum", facetable=True,
               values=("REAL", "FAKE", "Mix"),
               hint="The album's verdict, readable from a track row too."),
    ]


def _artist_fields() -> List[dict]:
    return [
        _field("artist.name", "Artist folder", "text", facetable=False),
        _field("artist.path", "Artist path", "text", facetable=False),
        _field("artist.album_count", "Album count", "number"),
        _field("artist.track_count", "Track count", "number"),
        _field("artist.grade_pct", "Artist grade %", "number", vmin=0, vmax=100,
               hint="Rollup of the artist's albums."),
        _entity_rating_field(
            "artist", "Artist rating",
            "Stars (halves), 0-5: your own verdict on the artist, not an "
            "average of their albums or tracks."),
        _field("artist.audit", "Artist audit", "enum", facetable=True,
               values=("REAL", "FAKE", "Mix"),
               hint="Rollup of the artist's albums."),
    ]


def _enum_of(tag_key: str) -> Optional[Sequence[str]]:
    for tag in registry()["tags"]:
        if tag["key"] == tag_key and tag["enum"]:
            return tag["enum"]
    return None


# group id -> the fields it holds. GROUPS decides the order and the labels;
# this decides what each one contains, so neither is restated.
_GROUP_FIELDS = {
    "tags": _tag_fields,
    "rating": lambda: [_rating_field()],
    "grade": _grade_fields,
    "audit": lambda: [_audit_field()],
    "tech": _tech_fields,
    "analysis": _analysis_fields,
    "library": _library_fields,
    "album": _album_fields,
    "artist": _artist_fields,
}

# A group with no builder (or a builder with no group) is a typo in one of the
# two tables, and it must fail at import rather than at the first request.
if set(dict(GROUPS)) != set(_GROUP_FIELDS):
    raise ValueError("api_query: GROUPS and _GROUP_FIELDS must name the same groups")


_catalogue: Optional[dict] = None
_catalogue_lock = threading.RLock()  # _index() re-enters through catalogue()


def catalogue() -> dict:
    """The field catalogue, built once per process (it reads only import-time
    tables: the tag registry and the payload's own track tag list)."""
    global _catalogue
    if _catalogue is None:
        with _catalogue_lock:
            if _catalogue is None:
                _catalogue = {"groups": [
                    {"id": gid, "label": label,
                     "fields": _GROUP_FIELDS[gid]()}
                    for gid, label in GROUPS
                ]}
    return _catalogue


def _index() -> Dict[str, dict]:
    """field name -> catalogue entry (built with the catalogue, cached with it)."""
    with _catalogue_lock:
        if _derived is None:
            _derive()
        return _derived["index"]


_derived: Optional[Dict[str, Any]] = None


def _derive() -> Dict[str, Any]:
    """Everything the catalogue implies, computed once: the field index, the
    bare names a saved rule may hold, and the boolean fields."""
    global _derived
    index = {f["field"]: f for g in catalogue()["groups"] for f in g["fields"]}
    bare = {f.rsplit(".", 1)[-1] for f in index}
    bools = {f for f, e in index.items() if e["type"] == "bool"}
    bools |= {f for f in bare if index.get(f, {}).get("type") == "bool"}
    _derived = {"index": index, "bare": bare, "bool": bools}
    return _derived


def _config() -> dict:
    from mlo import load_config
    return load_config()


def _bare_names() -> Set[str]:
    """The bare names a saved rule may hold: a tag's own key (``GENRE``), a
    tech key, or a payload key a field ends in (``grade_pass``). The original
    evaluator resolved any bare key against the track, so these must stay
    answerable even though the catalogue spells them ``tags.GENRE``."""
    _index()
    return _derived["bare"]


def bool_fields() -> Set[str]:
    """The catalogue's boolean fields, dotted AND bare (a saved rule says
    ``is_video``, the catalogue spells it ``library.is_video``). One
    definition, read by both the query endpoint and the playlist evaluator."""
    _index()
    return _derived["bool"]


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #
def _bad(detail: str):
    return HTTPException(400, detail)


def _known(field: str) -> bool:
    return field in _index() or field in _bare_names() or field in query_mod.ALIASES


def _check_field(field: Any) -> str:
    name = str(field or "")
    if not name or not _known(name):
        raise _bad(f"unknown field: {name or '(empty)'}")
    return name


def _check_op(field: str, op: Any) -> str:
    name = str(op or "eq")
    if name not in query_mod.OPERATORS:
        raise _bad(f"unknown op: {name}")
    entry = _index().get(field)
    advertised = {o["op"] for o in entry["ops"]} if entry else set()
    if name not in advertised and name not in LEGACY_OPS:
        raise _bad(f"op {name!r} does not apply to field {field!r}")
    return name


def _check_value(op: str, value: Any) -> None:
    seq = isinstance(value, (list, tuple))
    if op == "between":
        if not seq or len(value) != 2:
            raise _bad("op 'between' needs [min, max]")
        return
    if seq and op in ("lt", "gt", "lte", "gte"):
        raise _bad(f"op {op!r} needs a single value")
    if op == "matches":
        for pattern in (value if seq else [value]):
            try:
                re.compile(str(pattern))
            except re.error as exc:
                raise _bad(f"invalid regex: {exc}")


def _validated(body: dict) -> dict:
    """Validate a query body, or raise 400 naming what is wrong.

    A client that has drifted from the catalogue is told, not quietly given an
    empty list."""
    if not isinstance(body, dict):
        raise _bad("body must be a JSON object")
    target = body.get("target") or "tracks"
    if target not in query_mod.TARGETS:
        raise _bad(f"unknown target: {target}")
    match = body.get("match") or "all"
    if match not in ("all", "any"):
        raise _bad(f"unknown match: {match}")
    conditions = body.get("conditions") or []
    if not isinstance(conditions, list):
        raise _bad("conditions must be a list")
    for cond in conditions:
        if not isinstance(cond, dict):
            raise _bad("each condition must be an object")
        field = _check_field(cond.get("field"))
        op = _check_op(field, cond.get("op"))
        _check_value(op, cond.get("value"))
    group = body.get("group")
    if group not in (None, "") and group not in query_mod.GROUP_KEYS:
        raise _bad(f"unknown group: {group}")
    sort = body.get("sort") or {}
    if not isinstance(sort, dict):
        raise _bad("sort must be an object")
    if sort.get("key"):
        for key in (sort["key"] if isinstance(sort["key"], list) else [sort["key"]]):
            if key not in query_mod.GROUP_KEYS and not _known(str(key)):
                raise _bad(f"unknown sort key: {key}")
    if sort.get("dir") is not None:
        try:
            if int(sort["dir"]) not in (1, -1):
                raise ValueError
        except (TypeError, ValueError):
            raise _bad("sort.dir must be 1 or -1")
    for key in ("limit", "offset", "facet_limit"):
        if body.get(key) is not None:
            try:
                if int(body[key]) < 0:
                    raise ValueError
            except (TypeError, ValueError):
                raise _bad(f"{key} must be a non-negative integer")
    facets = query_mod.facet_fields(body)
    for field in facets:
        _check_field(field)
    paths = body.get("paths")
    if paths is not None and not isinstance(paths, list):
        raise _bad("paths must be a list")
    return body


# --------------------------------------------------------------------------- #
# Rating store
# --------------------------------------------------------------------------- #
def rating_source(user: str):
    """``rating_of(path, raw_tag) -> half-stars`` for the engine, with the
    folder scopes attached as ``rating_of.folder(scope, path)``.

    The store is read ONCE per scope and only when a condition actually names a
    rating field (the engine calls this per row, the first call pays). Truth
    order is the store's own: a DB row wins, else the file's RATING tag
    converted out of Picard's 0-100 scale, else unrated. A folder has no tag,
    so a folder row is the whole truth there.

    The folder half is an attribute of the same callable because the engine
    needs one fact a track lookup does not carry: which KIND of entity the path
    names (``mlo.query``'s module docstring states the contract). A source
    without it makes every album/artist rating field read unrated."""
    state: Dict[str, Any] = {}

    def store():
        if "mod" not in state:
            try:
                from server import ratings as ratings_mod
            except Exception:
                # The ratings slice not landed yet: nothing is rated, which is
                # what the payload alone can prove.
                ratings_mod = None
            state["mod"] = ratings_mod
        return state["mod"]

    def map_of(scope: str) -> Dict[str, int]:
        key = f"map:{scope}"
        if key not in state:
            mod = store()
            try:
                state[key] = (mod.map_for(user=user, scope=scope) if mod else {}) or {}
            except Exception:
                state[key] = {}
        return state[key]

    def rating_of(path: str, raw_tag):
        mod = store()
        if mod is None:
            return None
        half = map_of("track").get(path)
        if half is None and raw_tag not in (None, ""):
            try:
                half = mod.from_tag(raw_tag)
            except Exception:
                half = None
        return half

    def folder(scope: str, path: str):
        """The user's stored verdict on an album/artist folder (None when
        unrated). No tag fallback exists for a folder, and the map carries
        half-stars already — which is the unit the engine asked for."""
        if store() is None:
            return None
        return map_of(scope).get(path)

    rating_of.folder = folder
    return rating_of


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.get("/api/library/fields")
def library_fields():
    """The ONE field catalogue: what the query builder renders, what the
    smart-playlist rule editor renders, and what this API accepts."""
    return catalogue()


@router.post("/api/library/query")
def library_query(body: dict, request: Request):
    """Conditions + sort + group + facets over the cached library payload."""
    req = _validated(body or {})
    cfg = _config()
    library = lib_mod.build_library(cfg)
    return query_mod.run(library, req,
                         rating_of=rating_source(current_user(request)),
                         bool_fields=bool_fields())


@router.get("/api/library/facets")
def library_facets(request: Request, field: str,
                   limit: int = query_mod.FACET_LIMIT,
                   q: Optional[str] = Query(None), target: str = "tracks"):
    """Value counts for one field over the whole library.

    One walk of the cached payload; no tag is re-read (``build_library`` is
    TTL-cached), and the rows are streamed, not collected."""
    name = _check_field(field)
    if target not in query_mod.TARGETS:
        raise _bad(f"unknown target: {target}")
    library = lib_mod.build_library(_config())
    rows = (row for row, _payload in query_mod.walk(library, target))
    out = query_mod.facet(rows, name, limit=max(0, int(limit)), q=q,
                          rating_of=rating_source(current_user(request)))
    out["target"] = target
    return out
