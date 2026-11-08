"""Online discovery: provider charts, genre browse and library-seeded shelves.

The owner removed online discovery in 2.1.0 (spec R443). This module restores
it with the streaming services as the preferred evidence — Apple Music's
keyless charts and genre index first, Deezer as the keyless fallback, and
Last.fm / ListenBrainz / Spotify where a key is configured. Amazon Music has
no public catalogue API, so it is not offered as a source; the app says which
service answered rather than pretending they are interchangeable.

Routes:

* ``GET /api/discover/charts`` — what one provider ranks now;
* ``GET /api/discover/genres`` — the genre list the browse page offers;
* ``GET /api/discover/genre`` — top songs/albums for ONE genre (Apple's
  genreIndex, with Last.fm's tag chart as the keyed fallback);
* ``GET /api/discover/recommended`` — a library-seeded online shelf, with
  owned rows dropped server-side.

Every route is gated by ``discovery_enabled`` (the same switch the metadata
providers already honour) and never raises a provider's failure as a 500: a
refusal is an empty shelf plus the provider's own words in ``reason``.
"""
from collections import Counter

from fastapi import APIRouter, Query

from mlo.config import load_config
from mlo.genre_vocab import FAMILIES
from server import discovery

router = APIRouter(tags=["discover"])

KINDS = ("tracks", "albums", "artists")
CHART_SOURCES = ("auto", "itunes", "deezer", "lastfm", "listenbrainz")


def _enabled(cfg):
    return bool((cfg or {}).get("discovery_enabled", True))


def _disabled():
    return {"ok": False, "rows": [], "source": None,
            "reason": "online discovery is off (Settings → Discovery)"}


def _norm(value):
    try:
        return discovery.norm(value)
    except Exception:
        return str(value or "").strip().casefold()


def _owned_index(cfg):
    """(tracks, albums, artists) the library already holds, normalized."""
    from server import library as lib_mod

    tracks, albums, artists = set(), set(), set()
    try:
        lib = lib_mod.build_library(cfg)
    except Exception:
        return tracks, albums, artists
    for artist in (lib or {}).get("artists") or []:
        aname = artist.get("display_name") or artist.get("name") or ""
        if aname:
            artists.add(_norm(aname))
        for album in artist.get("albums") or []:
            meta = album.get("meta") or {}
            album_artist = meta.get("ALBUMARTIST") or meta.get("ARTIST") or aname
            if meta.get("ALBUM"):
                albums.add((_norm(meta.get("ALBUM")), _norm(album_artist)))
            for track in album.get("tracks") or []:
                tags = track.get("tags") or {}
                if tags.get("TITLE"):
                    tracks.add((_norm(tags.get("TITLE")),
                                _norm(tags.get("ARTIST") or album_artist)))
    return tracks, albums, artists


def _drop_owned(rows, kind, cfg):
    """Remove rows the library already has, by the same title+artist key the
    library's own search uses."""
    if not rows:
        return rows
    tracks, albums, artists = _owned_index(cfg)
    out = []
    for row in rows:
        title, artist = _norm(row.get("title")), _norm(row.get("artist"))
        if kind == "track" and (title, artist) in tracks:
            continue
        if kind == "album" and (title, artist) in albums:
            continue
        if kind == "artist" and (title or artist) in artists:
            continue
        out.append(row)
    return out


def _library_genre(cfg):
    """The genre the library listens to most — the "For you" shelf's seed."""
    from server import library as lib_mod
    from mlo.genres import split_stored

    counts = Counter()
    try:
        lib = lib_mod.build_library(cfg)
    except Exception:
        return "pop"
    for artist in (lib or {}).get("artists") or []:
        for album in artist.get("albums") or []:
            for track in album.get("tracks") or []:
                for name in split_stored((track.get("tags") or {}).get("GENRE")):
                    counts[name.casefold()] += 1
    if not counts:
        return "pop"
    return counts.most_common(1)[0][0]


def _chart(cfg, source, kind, period, limit):
    """(rows, source) for one chart request; raises ProviderRefused."""
    source = str(source or "auto").strip().lower()
    if source == "auto":
        source = "itunes" if kind == "tracks" else "deezer"
    if source == "itunes":
        if kind != "tracks":
            raise discovery.ProviderRefused(
                "Apple's keyless chart is songs only — choose Deezer for albums/artists")
        return discovery.itunes_most_played(limit, cfg=cfg), "itunes"
    if source == "deezer":
        return discovery.deezer_chart(kind, limit), "deezer"
    if source == "lastfm":
        return discovery.lastfm_chart(kind, limit, cfg=cfg), "lastfm"
    if source == "listenbrainz":
        return discovery.listenbrainz_chart(kind, period, limit), "listenbrainz"
    raise discovery.ProviderRefused(f"unknown chart source {source!r}")


@router.get("/api/discover/charts")
def discover_charts(
    period: str = Query("all", description="all|week|month|year (ListenBrainz)"),
    kind: str = Query("tracks", description="tracks|albums|artists"),
    source: str = Query("auto", description="auto|itunes|deezer|lastfm|listenbrainz"),
    limit: int = Query(50, ge=1, le=200),
):
    """One provider's current chart, in its own order."""
    cfg = load_config()
    if not _enabled(cfg):
        return _disabled()
    kind = (kind or "tracks").strip().lower()
    if kind not in KINDS:
        return {"ok": False, "rows": [], "reason": f"kind must be one of {', '.join(KINDS)}"}
    try:
        rows, used = _chart(cfg, source, kind, period, limit)
    except discovery.ProviderRefused as e:
        return {"ok": False, "rows": [], "source": source, "kind": kind,
                "period": period, "reason": str(e)}
    return {"ok": True, "rows": rows, "source": used, "kind": kind,
            "period": period, "reason": ""}


@router.get("/api/discover/genres")
def discover_genres():
    """The broad genres the browse page offers, in the vocabulary's order."""
    return {
        "ok": True,
        "genres": [{"name": name, "label": name.title()} for name in FAMILIES],
    }


@router.get("/api/discover/genre")
def discover_genre(
    genre: str = Query("pop"),
    kind: str = Query("tracks", description="tracks|albums|artists"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Top songs (default) or albums for ONE genre.

    Apple's genreIndex is the preferred, keyless answer; a genre Apple states
    nothing for falls through to Last.fm's tag chart when an API key is set.
    """
    cfg = load_config()
    if not _enabled(cfg):
        return _disabled()
    genre = str(genre or "").strip()
    kind = (kind or "tracks").strip().lower()
    if not genre:
        return {"ok": False, "rows": [], "genre": genre, "kind": kind,
                "reason": "a genre is required"}
    if kind not in KINDS:
        return {"ok": False, "rows": [], "genre": genre, "kind": kind,
                "reason": f"kind must be one of {', '.join(KINDS)}"}
    try:
        if kind == "tracks":
            got = discovery.itunes_genre_songs(genre, limit, offset, cfg=cfg)
            rows, source = got.get("rows") or [], "itunes"
            if not rows and str(cfg.get("lastfm_api_key") or "").strip():
                rows = (discovery.lastfm_tag_top("tracks", genre, limit, offset,
                                                 cfg=cfg).get("rows") or [])
                source = "lastfm"
        elif kind == "albums":
            got = discovery.itunes_genre_albums(genre, limit, offset, cfg=cfg)
            rows, source = got.get("rows") or [], "itunes"
            if not rows and str(cfg.get("lastfm_api_key") or "").strip():
                rows = (discovery.lastfm_tag_top("albums", genre, limit, offset,
                                                 cfg=cfg).get("rows") or [])
                source = "lastfm"
        else:
            rows, source = [], "none"
            if str(cfg.get("lastfm_api_key") or "").strip():
                rows = (discovery.lastfm_tag_top("artists", genre, limit, offset,
                                                 cfg=cfg).get("rows") or [])
                source = "lastfm"
    except discovery.ProviderRefused as e:
        return {"ok": False, "rows": [], "genre": genre, "kind": kind,
                "reason": str(e)}
    return {"ok": True, "rows": rows, "genre": genre, "kind": kind,
            "source": source, "offset": offset, "reason": ""}


@router.get("/api/discover/recommended")
def discover_recommended(
    seed: str = Query("library", description="library|genre|artist"),
    kind: str = Query("albums", description="tracks|albums|artists"),
    genre: str = Query("", description="override the library's top genre"),
    artist: str = Query("", description="seed artist for seed=artist"),
    limit: int = Query(12, ge=1, le=50),
):
    """An online shelf seeded by the library (its most-listened genre) or by a
    named artist/genre, with rows the library already owns removed."""
    cfg = load_config()
    if not _enabled(cfg):
        return _disabled()
    kind = (kind or "albums").strip().lower()
    if kind not in KINDS:
        return {"ok": False, "rows": [], "reason": f"kind must be one of {', '.join(KINDS)}"}
    seed = (seed or "library").strip().lower()
    if seed not in ("library", "genre", "artist"):
        return {"ok": False, "rows": [], "reason": "seed must be library, genre or artist"}
    want = max(limit, 12) * 2
    try:
        if seed == "artist" and artist.strip():
            # Spotify first when its client credentials are configured (the
            # owner's preference order), Deezer's keyless top-tracks as the
            # fallback. `spotify_artist_top_tracks` answers [] with no key, so
            # an unconfigured install costs no request.
            rows = discovery.spotify_artist_top_tracks(
                artist.strip(), min(50, want), cfg=cfg)
            source = "spotify"
            if not rows:
                rows = discovery.deezer_artist_top(artist.strip(), want)
                source = "deezer"
            basis = f"tracks by {artist.strip()}"
        else:
            picked = genre.strip() or _library_genre(cfg)
            if kind == "tracks":
                rows = discovery.itunes_genre_songs(picked, want, 0, cfg=cfg).get("rows") or []
                source = "itunes"
            elif kind == "albums":
                rows = discovery.itunes_genre_albums(picked, want, 0, cfg=cfg).get("rows") or []
                source = "itunes"
            else:
                rows = discovery.deezer_chart("artists", want)
                source = "deezer"
            basis = f"your library's {picked} listening"
    except discovery.ProviderRefused as e:
        return {"ok": False, "rows": [], "source": None, "seed": seed,
                "reason": str(e)}
    rows = _drop_owned(rows, kind[:-1] if kind != "artists" else "artist", cfg)
    return {"ok": True, "rows": rows[:limit], "source": source, "seed": seed,
            "kind": kind, "basis": basis, "reason": ""}
