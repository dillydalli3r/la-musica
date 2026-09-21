"""Discover routes — genre browsing, online recommendations and provider charts.

The four `/api/discover/*` endpoints the Discover pages read: the genre list,
one genre's page per source and kind, recommendations seeded by the library, by
one genre or by ONE entity (the artist/album/track page a shelf sits on), and
the online charts for one window. The registry of sources, the library reading
and the merge all live in `server.discover`; the plain
functions below validate the query, bound the page and hand the payload back
unchanged, and the route handlers are only their wiring — the same split
`api_discovery.ensure_artist_album_metadata` uses, so the surface is testable
without an app (and without FastAPI's `Query` defaults standing in for the real
arguments).

They are a router of their own (like the discovery/lyrics/import surfaces) so
the provider layer stays testable on its own; `server/main.py` only includes it.
"""
from fastapi import APIRouter, HTTPException, Query

from mlo import load_config
from server import discover

router = APIRouter(tags=["discover"])

_SCOPES = ("library", "online", "all")


def _int_arg(value, default):
    """A query argument as an int, or *default* for anything unusable.

    FastAPI hands a real int to the route; a direct caller sees the parameter's
    own default, so this keeps the plain functions callable on their own."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _str_arg(value, default=""):
    """A query argument as a string, or *default* when a caller left it out."""
    return value.strip() if isinstance(value, str) else default


def genres_list(scope="all", cfg=None):
    """The genre list, from the library, the online sources, or both.

    `scope=library|online|all` picks the half. A genre named by several sources
    is ONE row whose `sources` lists them and whose counts come from the library
    (a source that only names a genre has no counts). Sources that could not run
    are reported in `notes` (`skipped: no <key>`, `failed: <reason>`, or
    `partial: …` for MusicBrainz's page-at-a-time taxonomy) — never swallowed.
    """
    scope = _str_arg(scope, "all").lower() or "all"
    if scope not in _SCOPES:
        raise HTTPException(400, "scope must be one of: " + ", ".join(_SCOPES))
    return discover.genres_payload(cfg if cfg is not None else load_config(),
                                   scope=scope)


def genre_page(genre="", kind="albums", source="all", limit=25, offset=0, cfg=None):
    """One page of one genre, per source and per kind.

    `source=all` merges every source that can list this `kind` (albums,
    artists, tracks) into ONE row shape; a single source id answers from that
    source alone. Rows carry their provenance, rows naming the same release are
    merged (MBID first, then normalized artist+title) with the extra sources in
    `also_from`, and `notes` reports every source that was skipped or failed.
    An unknown genre or source is an empty `items` list, never an error.
    """
    kind = _str_arg(kind, "albums").lower() or "albums"
    if kind not in discover.KINDS:
        raise HTTPException(400, "kind must be one of: " + ", ".join(discover.KINDS))
    offset = _int_arg(offset, 0)
    if offset < 0 or offset > discover.MAX_OFFSET:
        # The sources' own windows are bounded; a deeper offset would silently
        # repeat rows, so the page is refused instead.
        raise HTTPException(400, "offset must be between 0 and %d" % discover.MAX_OFFSET)
    return discover.genre_payload(cfg if cfg is not None else load_config(),
                                  genre=_str_arg(genre), kind=kind,
                                  source=_str_arg(source, "all").lower() or "all",
                                  limit=_int_arg(limit, 25), offset=offset)


def recommended_list(seed="library", kind="albums", limit=20, cfg=None,
                     seed_kind="", seed_mbid="", seed_name="", seed_artist=""):
    """Online recommendations, seeded by the library, by one genre, or by ONE
    entity.

    `seed=library` uses the library's own genre mix and its most-collected
    artists; `seed=<genre name>` asks the genre feeds about that one genre.
    What the library already owns is dropped, every row records why it was
    suggested (`reason`) and `basis` names the seed. With nothing to suggest —
    or no source configured — `items` is empty and `notes` says so.

    `seed_kind=artist|album|track` (with `seed_mbid`, else `seed_name` and
    `seed_artist`) seeds the shelf with ONE entity — the page it sits on —
    and keeps the rows the library already owns, each marked and carrying its
    library `path`. Anything other than a seed kind the registry knows is a
    400: a shelf about the wrong page would be worse than a stated refusal.
    """
    kind = _str_arg(kind, "albums").lower() or "albums"
    if kind not in discover.KINDS:
        raise HTTPException(400, "kind must be one of: " + ", ".join(discover.KINDS))
    seed_kind = _str_arg(seed_kind, "").lower()
    if seed_kind and seed_kind not in discover.SEED_KINDS:
        raise HTTPException(400, "seed_kind must be one of: "
                            + ", ".join(discover.SEED_KINDS))
    return discover.recommended_payload(cfg if cfg is not None else load_config(),
                                        seed=_str_arg(seed, "library") or "library",
                                        kind=kind, limit=_int_arg(limit, 20),
                                        seed_kind=seed_kind,
                                        seed_mbid=_str_arg(seed_mbid),
                                        seed_name=_str_arg(seed_name),
                                        seed_artist=_str_arg(seed_artist))


def charts_list(period="all", kind="tracks", source="all", limit=50, cfg=None):
    """What the online sources rank for one window and one kind.

    `period=all|year|month|week` picks the window and `kind=tracks|albums|
    artists` the rows; `source=all` asks every source that charts that kind, in
    the registry's chart order (RateYourMusic first for tracks), and a single
    source id answers from that one alone. A source asked for a window it does
    not publish is reported as `unsupported:` in `notes` — never handed its
    all-time chart — and every outcome is a note (`skipped:`/`failed:`), so an
    empty page always says which of the two it is. Charts are NOT merged across
    sources (a rank is per source); the library's own charts live at
    `/api/top` (server/api_plays.py).
    """
    period = _str_arg(period, "all").lower() or "all"
    if period not in discover.CHART_PERIODS:
        raise HTTPException(400, "period must be one of: "
                            + ", ".join(discover.CHART_PERIODS))
    kind = _str_arg(kind, "tracks").lower() or "tracks"
    if kind not in discover.CHART_KINDS:
        raise HTTPException(400, "kind must be one of: "
                            + ", ".join(discover.CHART_KINDS))
    return discover.charts_payload(cfg if cfg is not None else load_config(),
                                   period=period, kind=kind,
                                   source=_str_arg(source, "all").lower() or "all",
                                   limit=_int_arg(limit, 50))


@router.get("/api/discover/genres")
def discover_genres(scope: str = Query("all")):
    """The genre list (see `genres_list`)."""
    return genres_list(scope=scope)


@router.get("/api/discover/charts")
def discover_charts(period: str = Query("all"), kind: str = Query("tracks"),
                    source: str = Query("all"), limit: int = Query(50)):
    """The online charts for one window (see `charts_list`)."""
    return charts_list(period=period, kind=kind, source=source, limit=limit)


@router.get("/api/discover/genre")
def discover_genre(genre: str = Query(""), kind: str = Query("albums"),
                   source: str = Query("all"), limit: int = Query(25),
                   offset: int = Query(0)):
    """One page of one genre (see `genre_page`)."""
    return genre_page(genre=genre, kind=kind, source=source, limit=limit,
                      offset=offset)


@router.get("/api/discover/recommended")
def discover_recommended(seed: str = Query("library"), kind: str = Query("albums"),
                         limit: int = Query(20), seed_kind: str = Query(""),
                         seed_mbid: str = Query(""), seed_name: str = Query(""),
                         seed_artist: str = Query("")):
    """Online recommendations (see `recommended_list`)."""
    return recommended_list(seed=seed, kind=kind, limit=limit,
                            seed_kind=seed_kind, seed_mbid=seed_mbid,
                            seed_name=seed_name, seed_artist=seed_artist)
