"""Discover routes — genre browsing and online recommendations.

The three `/api/discover/*` endpoints the Discover pages read: the genre list,
one genre's page per source and kind, and recommendations seeded by the library
or by one genre. The registry of sources, the library reading and the merge all
live in `server.discover`; the plain functions below validate the query, bound
the page and hand the payload back unchanged, and the route handlers are only
their wiring — the same split `api_discovery.ensure_artist_album_metadata` uses,
so the surface is testable without an app (and without FastAPI's `Query`
defaults standing in for the real arguments).

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


def recommended_list(seed="library", kind="albums", limit=20, cfg=None):
    """Online recommendations, seeded by the library or by one genre.

    `seed=library` uses the library's own genre mix and its most-collected
    artists; `seed=<genre name>` asks the genre feeds about that one genre.
    What the library already owns is dropped, every row records why it was
    suggested (`reason`) and `basis` names the seed. With nothing to suggest —
    or no source configured — `items` is empty and `notes` says so.
    """
    kind = _str_arg(kind, "albums").lower() or "albums"
    if kind not in discover.KINDS:
        raise HTTPException(400, "kind must be one of: " + ", ".join(discover.KINDS))
    return discover.recommended_payload(cfg if cfg is not None else load_config(),
                                        seed=_str_arg(seed, "library") or "library",
                                        kind=kind, limit=_int_arg(limit, 20))


@router.get("/api/discover/genres")
def discover_genres(scope: str = Query("all")):
    """The genre list (see `genres_list`)."""
    return genres_list(scope=scope)


@router.get("/api/discover/genre")
def discover_genre(genre: str = Query(""), kind: str = Query("albums"),
                   source: str = Query("all"), limit: int = Query(25),
                   offset: int = Query(0)):
    """One page of one genre (see `genre_page`)."""
    return genre_page(genre=genre, kind=kind, source=source, limit=limit,
                      offset=offset)


@router.get("/api/discover/recommended")
def discover_recommended(seed: str = Query("library"), kind: str = Query("albums"),
                         limit: int = Query(20)):
    """Online recommendations (see `recommended_list`)."""
    return recommended_list(seed=seed, kind=kind, limit=limit)
