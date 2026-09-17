"""Discovery routes — recommendations, catalogue search, artist artwork and
artist/album descriptions, all served through `server.discovery`.

These live in their own router (rather than inline in `server/main.py`) so the
provider layer, its caches and its fallback chains stay testable on their own;
`main.py` only includes the router. Every handler validates the paths it takes
against the music folder exactly like `main.py` does.
"""
import os
import re

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel

from mlo import artistdata, load_config
from server import artcache
from server import discovery
from server import integrations as intg
from server import library as lib_mod
from server import tagcache

router = APIRouter(tags=["discovery"])

# An artist folder that is nothing but an MBID ("Artists/<uuid>") has no name
# to give a provider; _artist_name falls back to the album tags then.
_MBID_ONLY_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


class WishRequest(BaseModel):
    artist: str = ""
    title: str = ""
    year: str = ""
    note: str = ""
    mbid: str = ""


class ImageRequest(BaseModel):
    artist: str = ""
    url: str = ""
    source: str = ""


class TextRequest(BaseModel):
    artist: str = ""
    text: str = ""


class AlbumTextRequest(BaseModel):
    path: str = ""
    artist: str = ""
    album: str = ""
    text: str = ""


def _guard(path, cfg=None):
    """Path containment, mirroring main.py's shared guard."""
    from server.main import _in_music_folder, _music_folder
    folder = _music_folder(cfg)
    if not folder:
        raise HTTPException(400, "music folder is not configured")
    if not path or not _in_music_folder(path, folder):
        raise HTTPException(400, "path outside music folder")


def _artist_folder(artist, cfg):
    """Resolve the API's `artist` parameter.

    Two shapes are accepted because two callers exist: the artist page sends
    the folder path it already has (folder names carry MBID suffixes, so a
    name lookup would miss), while a hand-written call may send a plain
    artist name. A path must sit inside the music folder; a name is resolved
    under <music>/Artists by `mlo.artistdata`, which refuses separators — the
    containment guard for the name form.
    """
    if not artist or not artist.strip():
        raise HTTPException(400, "artist is required")
    candidate = os.path.normpath(artist)
    looks_like_path = os.path.isabs(artist) or any(sep in artist for sep in ("/", "\\"))
    if looks_like_path and os.path.isdir(candidate):
        _guard(candidate, cfg)
        return candidate
    folder = artistdata.artist_dir(cfg, artist)
    if not folder:
        raise HTTPException(404, f"artist folder not found: {artist}")
    return folder


def _artist_name(folder, artist=""):
    """The artist's *name* for provider lookups, given a library folder path.

    Folder names carry an MBID suffix ("Slowdive [a16371b9-…]"), so a raw path
    cannot be handed to Deezer/Wikipedia/TheAudioDB. Strip the suffix; when the
    folder is nothing but an MBID, read the name from the album tags inside.
    """
    name = os.path.basename(str(folder or artist or "").rstrip("\\/"))
    name = re.sub(r"\s*[\[(][0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}[\])]\s*$",
                  "", name, flags=re.I).strip()
    if name and not _MBID_ONLY_RE.match(name):
        return name
    try:
        from mlo.audio import AudioFile
        from mlo.stats import is_audio_file
        for entry in sorted(os.listdir(folder)):
            sub = os.path.join(folder, entry)
            if not os.path.isdir(sub):
                continue
            for f in sorted(os.listdir(sub)):
                if not is_audio_file(f):
                    continue
                af = AudioFile(os.path.join(sub, f))
                tag = (af.get_tag("ALBUMARTIST") or af.get_tag("ARTIST") or "").strip()
                if tag:
                    return tag
    except Exception:
        pass
    return name


def _feature_gate(cfg, key, what):
    """Refuse an *automatic* fetch when its feature is switched off.

    Manual flows (an explicit URL, an upload, edited text, Clear) stay
    available: the settings toggle governs the app fetching on your behalf,
    not your own hand.
    """
    if not cfg.get(key, True):
        raise HTTPException(403, f"{what} is turned off in Settings")


def _owned_album_keys(cfg):
    """{normalized "artist|title": album path} for the current library.

    Uses the same 5-second library cache the rest of the app reads, so a
    "more like this" request never walks the tree itself. A library that
    fails to build degrades to "nothing owned", which only means suggestions
    may repeat an album you already have.
    """
    try:
        payload = tagcache.get_library(
            lib_mod.library_cache_key(cfg),
            lambda: lib_mod.build_library(cfg),
        )
    except Exception:
        return {}
    keys = {}
    for artist in (payload or {}).get("artists") or []:
        for album in artist.get("albums") or []:
            name = album.get("album_artist") or album.get("artist") or artist.get("name")
            key = f"{discovery._norm(name)}|{discovery._norm(album.get('title'))}"
            keys[key] = album.get("path")
    return keys


def _effective_cfg(cfg, override_source=None):
    """Config copy that forces one search source (used by the MusicBrainz
    page's "MusicBrainz only" mode and its discovery-first default)."""
    if not override_source:
        return cfg
    forced = dict(cfg)
    forced["discovery_search_sources"] = [override_source]
    if override_source == "musicbrainz":
        forced["mb_search_source"] = "musicbrainz"
    return forced


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #
@router.get("/api/discovery/sources")
def discovery_sources():
    cfg = load_config()
    cat = discovery.sources_catalog()
    return {
        **cat,
        "enabled": bool(cfg.get("discovery_enabled", True)),
        "saved": {
            "discovery_rec_sources": cfg.get("discovery_rec_sources") or [],
            "discovery_search_sources": cfg.get("discovery_search_sources") or [],
            "artist_image_sources": cfg.get("artist_image_sources") or [],
            "description_sources": cfg.get("description_sources") or [],
        },
        "mb_search_source": cfg.get("mb_search_source", "auto"),
    }


@router.get("/api/discovery/search")
def discovery_search(q: str = Query(""), type: str = Query("album"),
                     limit: int = Query(25, ge=1, le=100),
                     artist: str = Query(""), album: str = Query(""),
                     source: str = Query("")):
    """Catalogue search for the MusicBrainz browser and the global search box.

    `source` forces one provider ("musicbrainz" keeps the old behaviour);
    otherwise the configured `mb_search_source` decides: discovery-first with
    a MusicBrainz fallback (auto), discovery only, or MusicBrainz only.
    """
    cfg = load_config()
    mode = source or cfg.get("mb_search_source", "auto")
    if mode == "musicbrainz":
        cfg = _effective_cfg(cfg, "musicbrainz")
    rows = []
    if type == "artist":
        rows = discovery.search_artists(q, limit=limit, cfg=cfg)
    else:
        rows = discovery.search_albums(q, limit=limit, cfg=cfg,
                                       artist=artist, album=album or q)
        owned = _owned_album_keys(cfg)
        for row in rows:
            row["owned_path"] = owned.get(
                f"{discovery._norm(row.get('artist'))}|{discovery._norm(row.get('title'))}")
    return {"query": q, "type": type, "mode": mode, "rows": rows}


@router.get("/api/discovery/album")
def discovery_album(deezer_id: int = Query(0), artist: str = Query(""),
                    album: str = Query(""), resolve: int = Query(0)):
    """Full album detail from the discovery providers, with the MusicBrainz
    release-group resolved on demand (`resolve=1`) so the page can offer
    "add to wishes" without a second round trip."""
    detail = discovery.deezer_album(deezer_id) if deezer_id else None
    if not detail and (artist or album):
        hits = discovery.deezer_search_album(f'artist:"{artist}" album:"{album}"', limit=3) \
            if artist else []
        match = next((h for h in hits if discovery._norm(h["title"]) == discovery._norm(album)), None) \
            or (hits[0] if hits else None)
        if match:
            detail = discovery.deezer_album(match["deezer_id"])
    if not detail:
        raise HTTPException(404, "album not found in the discovery providers")
    if resolve:
        rg = discovery.resolve_release_group(detail.get("artist"), detail.get("title"))
        if rg:
            detail["mbid"] = rg.get("mbid")
            detail["mb_release_group"] = rg
    return detail


@router.get("/api/discovery/similar")
def discovery_similar(kind: str = Query("album"), artist: str = Query(""),
                      title: str = Query(""), album: str = Query(""),
                      mbid: str = Query(""), limit: int = Query(12, ge=1, le=40)):
    """\"More like this\" rows for an album, track or artist page.

    Rows the library already owns are flagged (`owned_path`) rather than
    dropped, so the UI can link straight to the album it already has.
    """
    cfg = load_config()
    artist = artist.strip()
    if not artist:
        raise HTTPException(400, "artist is required")
    owned = _owned_album_keys(cfg)
    if kind == "track":
        rows = discovery.similar_tracks(artist, title or None, limit=limit, cfg=cfg)
    elif kind == "artist":
        rows = [{**r, "kind": "artist"} for r in
                discovery.similar_artists(artist, mbid=mbid or None, limit=limit, cfg=cfg)]
    else:
        rows = discovery.similar_albums(artist, album or title or None, mbid=mbid or None,
                                        limit=limit, cfg=cfg)
    for row in rows:
        key = f"{discovery._norm(row.get('artist'))}|{discovery._norm(row.get('title'))}"
        row["owned_path"] = owned.get(key)
    return {"kind": kind, "artist": artist, "rows": rows}


@router.get("/api/discovery/popular")
def discovery_popular(limit: int = Query(12, ge=1, le=40),
                      range: str = Query("week")):
    """What the world is listening to right now (ListenBrainz sitewide)."""
    cfg = load_config()
    rows = discovery.popular_albums(limit=limit, cfg=cfg, range_=range)
    owned = _owned_album_keys(cfg)
    for row in rows:
        row["owned_path"] = owned.get(
            f"{discovery._norm(row.get('artist'))}|{discovery._norm(row.get('title'))}")
    return {"rows": rows, "range": range}


@router.post("/api/discovery/wish")
def discovery_wish(req: WishRequest):
    """Turn a discovery row into a wish — the bridge from "popular album"
    to something the Soulseek worker can actually download.

    A row without a MusicBrainz id (Deezer/iTunes rows never carry one) is
    resolved against MusicBrainz first; if nothing matches there is nothing
    to download by identity, so the caller gets a clear 404 instead of a wish
    that can never fill.
    """
    from server import wishes
    cfg = load_config()
    artist = req.artist.strip()
    title = req.title.strip()
    if not title:
        raise HTTPException(400, "title is required")
    mbid = req.mbid.strip()
    resolved = None
    if not mbid:
        resolved = discovery.resolve_release_group(artist, title, cfg)
        if not resolved or not resolved.get("mbid"):
            raise HTTPException(404, "no MusicBrainz release found for this album")
        mbid = resolved["mbid"]
    wish = wishes.add_wish(
        mbid,
        title=title,
        artist=artist or (resolved or {}).get("artist", ""),
        year=req.year or (resolved or {}).get("year", ""),
        note=req.note or "Added from discovery",
    )
    return {"ok": True, "wish": wish, "resolved": resolved}


# --------------------------------------------------------------------------- #
# Artist artwork + descriptions
# --------------------------------------------------------------------------- #
def _artist_image_payload(folder, artist):
    path = artistdata.image_path(folder)
    provenance = artistdata.read_provenance(folder)
    return {
        "present": bool(path),
        "file": os.path.basename(path) if path else None,
        "url": f"/api/artist/image?artist={artist}" if path else None,
        "source": provenance.get("source"),
        "label": provenance.get("label"),
        "source_url": provenance.get("source_url"),
        "fetched": provenance.get("fetched"),
        "updated": provenance.get("updated"),
    }


def _artist_text_payload(folder):
    text = artistdata.read_description(folder)
    provenance = artistdata.read_provenance(folder)
    return {
        "present": bool(text.strip()),
        "text": text or None,
        "source": provenance.get("description_source") or provenance.get("source"),
        "source_url": provenance.get("description_source_url") or provenance.get("source_url"),
        "fetched": provenance.get("description_fetched") or provenance.get("fetched"),
    }


@router.get("/api/artist/artwork")
def artist_artwork(artist: str = Query(...)):
    """Everything the artist page needs about stored artwork: the image, the
    description, where each came from, and the artist's own grade."""
    cfg = load_config()
    folder = _artist_folder(artist, cfg)
    payload = {
        "artist": artist,
        "path": folder.replace("\\", "/"),
        "image": _artist_image_payload(folder, artist),
        "description": _artist_text_payload(folder),
        "provenance": artistdata.read_provenance(folder),
        # Whether the app may fetch on the user's behalf (Settings →
        # Artist images & descriptions). The UI greys its Fetch buttons when
        # this is off instead of letting the request 403.
        "auto": {
            "image": bool(cfg.get("artist_image_enabled", True)),
            "description": bool(cfg.get("artist_description_enabled", True)),
        },
    }
    try:
        from mlo import grader
        payload["grade"] = grader.grade_artist(folder, cfg)
    except Exception as e:
        payload["grade"] = {"error": str(e)}
    return payload


@router.get("/api/artist/image")
def artist_image(artist: str = Query(...)):
    """The stored artist image bytes (404 when none has been saved)."""
    cfg = load_config()
    folder = _artist_folder(artist, cfg)
    path = artistdata.image_path(folder)
    if not path:
        raise HTTPException(404, "no artist image")
    return _file_response(path)


@router.get("/api/artist/image/candidates")
def artist_image_candidates(artist: str = Query(...)):
    """Candidate images for the picker, from every configured source.

    The automatic fetch takes the first one; the UI shows them all so a bad
    automatic pick can be swapped for another provider's without a manual
    upload.
    """
    cfg = load_config()
    rows = []
    seen = set()

    def add(url, source, label, kind="photo"):
        if not url or url in seen:
            return
        seen.add(url)
        rows.append({"url": url, "source": source, "label": label, "kind": kind})

    name = _artist_name(artist)
    auto = discovery.artist_image(name, cfg=cfg)
    if auto:
        add(auto.get("url"), auto.get("source"), auto.get("label") or "Automatic pick")
    db = discovery.audiodb_artist(name)
    if db:
        for key, label in (("thumb", "TheAudioDB thumb"), ("banner", "TheAudioDB banner"),
                           ("wide_thumb", "TheAudioDB wide thumb"),
                           ("fanart", "TheAudioDB fanart")):
            add(db.get(key), "audiodb", label)
    dz = discovery.deezer_artist(name)
    if dz:
        add(dz.get("image"), "deezer", "Deezer artist photo")
    it = discovery.itunes_artist_artwork(name)
    if it:
        add(it, "itunes", "Apple album artwork")
    summary = discovery.wikipedia_summary(name)
    if summary:
        add(summary.get("image"), "wikipedia", "Wikipedia lead image")
    return {"artist": artist, "name": name, "rows": rows}


@router.post("/api/artist/image")
def artist_image_save(req: ImageRequest):
    """Save an artist image: an explicit candidate *url*, or the automatic
    pick when no url is given (which the settings toggle can switch off)."""
    cfg = load_config()
    folder = _artist_folder(req.artist, cfg)
    url = req.url.strip()
    source = req.source.strip()
    label = None
    if not url:
        _feature_gate(cfg, "artist_image_enabled", "Automatic artist images")
        auto = discovery.artist_image(_artist_name(folder, req.artist), cfg=cfg)
        if not auto:
            raise HTTPException(404, "no artist image found in any configured source")
        url, source, label = auto["url"], auto["source"], auto.get("label")
    try:
        data, _ctype, _src = artcache.fetch_art(url, cfg=cfg)
        if not data:
            raise RuntimeError("no source could provide the image")
    except Exception as e:
        raise HTTPException(502, f"image download failed: {e}")
    path = artistdata.save_image(folder, data, cfg, source=source or "manual",
                                 source_url=url, kind="artist", label=label)
    if not path:
        raise HTTPException(400, "not a usable image")
    tagcache.invalidate_all()
    return {"ok": True, "file": os.path.basename(path), "source": source or "manual",
            "source_url": url,
            "image": _artist_image_payload(folder, req.artist)}


@router.post("/api/artist/image/upload")
async def artist_image_upload(artist: str = Form(...), file: UploadFile = File(...)):
    """Manual artist image upload — the path offered when auto-fetch fails."""
    cfg = load_config()
    folder = _artist_folder(artist, cfg)
    data = await file.read()
    path = artistdata.save_image(folder, data, cfg, source="upload",
                                 source_url=None, kind="artist", label="Uploaded")
    if not path:
        raise HTTPException(400, "not a usable image")
    tagcache.invalidate_all()
    return {"ok": True, "file": os.path.basename(path),
            "image": _artist_image_payload(folder, artist)}


@router.delete("/api/artist/image")
def artist_image_clear(artist: str = Query(...)):
    cfg = load_config()
    folder = _artist_folder(artist, cfg)
    removed = artistdata.clear_image(folder)
    tagcache.invalidate_all()
    return {"ok": removed}


@router.post("/api/artist/description")
def artist_description_save(req: TextRequest):
    """Store the artist description — the supplied text, or fetched from the
    configured description sources when the body carries none."""
    cfg = load_config()
    folder = _artist_folder(req.artist, cfg)
    text = (req.text or "").strip()
    source = source_url = title = None
    if not text:
        _feature_gate(cfg, "artist_description_enabled", "Automatic artist descriptions")
        found = discovery.artist_description(_artist_name(folder, req.artist), cfg=cfg)
        if not found:
            raise HTTPException(404, "no description found in any configured source")
        text = found["text"]
        source, source_url, title = found.get("source"), found.get("source_url"), found.get("title")
    path = artistdata.write_description(folder, text, cfg=cfg, source=source,
                                        source_url=source_url, kind="artist")
    if not path:
        raise HTTPException(400, "description is empty")
    if source:
        artistdata.write_provenance(folder, {"description_source": source,
                                             "description_source_url": source_url,
                                             "description_title": title}, kind="artist", cfg=cfg)
    tagcache.invalidate_all()
    return {"ok": True, "source": source, "text": text,
            "description": _artist_text_payload(folder)}


@router.delete("/api/artist/description")
def artist_description_clear(artist: str = Query(...)):
    cfg = load_config()
    folder = _artist_folder(artist, cfg)
    removed = artistdata.delete_description(folder, kind="artist", cfg=cfg)
    tagcache.invalidate_all()
    return {"ok": removed}


@router.post("/api/album/description")
def album_description_save(req: AlbumTextRequest):
    """Store an album description in the album folder (`description.txt`),
    fetched from the configured sources when the body carries no text."""
    cfg = load_config()
    if not req.path:
        raise HTTPException(400, "path is required")
    album_dir = os.path.normpath(req.path)
    _guard(album_dir)
    if not os.path.isdir(album_dir):
        raise HTTPException(404, "album not found")
    text = (req.text or "").strip()
    artist, album = req.artist.strip(), req.album.strip()
    source = source_url = title = None
    if not text:
        _feature_gate(cfg, "album_description_enabled", "Automatic album descriptions")
        if not artist or not album:
            artist, album = _album_identity(album_dir, artist, album)
        found = discovery.album_description(artist, album, cfg=cfg)
        if not found:
            raise HTTPException(404, "no description found in any configured source")
        text = found["text"]
        source, source_url, title = found.get("source"), found.get("source_url"), found.get("title")
    path = artistdata.write_description(album_dir, text, cfg=cfg, source=source,
                                        source_url=source_url, kind="album")
    if not path:
        raise HTTPException(400, "description is empty")
    if source:
        artistdata.write_provenance(album_dir, {"description_source": source,
                                                "description_source_url": source_url,
                                                "description_title": title},
                                    kind="album", cfg=cfg)
    tagcache.invalidate_all()
    return {"ok": True, "source": source, "text": text,
            "description": {"present": True, "text": text, "source": source,
                            "source_url": source_url}}


@router.delete("/api/album/description")
def album_description_clear(path: str = Query(...)):
    cfg = load_config()
    album_dir = os.path.normpath(path)
    _guard(album_dir)
    removed = artistdata.delete_description(album_dir, kind="album", cfg=cfg)
    tagcache.invalidate_all()
    return {"ok": removed}


def _album_identity(album_dir, artist="", album=""):
    """Artist + album for a folder, read from its files when the caller did
    not supply them (the album page already has them; the API does not
    require it)."""
    from mlo.audio import AudioFile
    from mlo.stats import is_audio_file
    if artist and album:
        return artist, album
    try:
        names = sorted(os.listdir(album_dir))
    except OSError:
        return artist, album
    for name in names:
        if not is_audio_file(name):
            continue
        try:
            af = AudioFile(os.path.join(album_dir, name))
            artist = artist or (af.get_tag("ALBUMARTIST") or af.get_tag("ARTIST") or "")
            album = album or (af.get_tag("ALBUM") or "")
        except Exception:
            continue
        if artist and album:
            break
    if not album:
        album = os.path.basename(album_dir)
    return artist, album


def _file_response(path):
    from fastapi.responses import FileResponse
    ext = os.path.splitext(path)[1].lower()
    ctype = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".png": "image/png"}.get(ext, "application/octet-stream")
    return FileResponse(path, media_type=ctype)
