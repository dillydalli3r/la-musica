"""Discovery routes — the provider catalogue, artist artwork and artist/album
descriptions, all served through `server.discovery`.

These live in their own router (rather than inline in `server/main.py`) so the
provider layer, its caches and its fallback chains stay testable on their own;
`main.py` only includes the router. Every handler validates the paths it takes
against the music folder exactly like `main.py` does.
"""
import os
import re
import traceback

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel

from mlo import artistdata, load_config
from server import artcache
from server import discovery
from server import integrations as intg
from server import tagcache

router = APIRouter(tags=["discovery"])

# An artist folder that is nothing but an MBID ("Artists/<uuid>") has no name
# to give a provider; _artist_name falls back to the album tags then.
_MBID_ONLY_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


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


class AlbumMetadataRequest(BaseModel):
    path: str = ""
    # Which of artist_image / artist_description / album_description to run;
    # empty = all three. A caller with a progress bar drives them one by one.
    items: list = []
    force: bool = False
    staged: bool = False  # the import wizard's not-yet-imported album


def _guard(path, cfg=None, staged=False):
    """Path containment, mirroring main.py's shared guard — including the
    import wizard's opt-in `staged` allowance for its own album folder, which
    is not in the library yet."""
    from server.main import _allow_staged, _in_music_folder, _music_folder
    folder = _music_folder(cfg)
    if not folder:
        raise HTTPException(400, "music folder is not configured")
    if not path or not (_in_music_folder(path, folder)
                        or _allow_staged(path, staged)):
        raise HTTPException(400, "path outside music folder")


def _artist_folder(artist, cfg):
    """Resolve the API's `artist` parameter.

    Three shapes are accepted because three callers exist: the artist page
    sends the folder path it already has (folder names carry MBID suffixes, so
    a name lookup would miss), its links carry `mb:<artist MBID>` — the form
    the album page links with, so on that route every provider lookup used to
    search for the literal string "mb:…" and find nothing — and a hand-written
    call may send a plain artist name. A path must sit inside the music folder;
    an MBID resolves through the same index `main.py`'s artist route uses; a
    name is resolved under <music>/Artists by `mlo.artistdata`, which refuses
    separators — the containment guard for the name form.
    """
    if not artist or not artist.strip():
        raise HTTPException(400, "artist is required")
    candidate = os.path.normpath(artist)
    looks_like_path = os.path.isabs(artist) or any(sep in artist for sep in ("/", "\\"))
    if looks_like_path and os.path.isdir(candidate):
        _guard(candidate, cfg)
        return candidate
    if not looks_like_path and artist.strip().lower().startswith("mb:"):
        from server import mbresolve
        resolved = mbresolve.resolve_artist(artist.strip())
        if resolved and os.path.isdir(resolved):
            _guard(resolved, cfg)
            return os.path.normpath(resolved)
    folder = artistdata.artist_dir(cfg, artist)
    if not folder:
        raise HTTPException(404, f"artist folder not found: {artist}")
    return folder


def _artist_name(folder, artist=""):
    """The artist's *name* for provider lookups, given a library folder path.

    Folder names carry an MBID suffix ("Slowdive [a16371b9-…]"), so a raw path
    cannot be handed to Deezer/Wikipedia/TheAudioDB. Strip the suffix; when the
    folder is nothing but an MBID, read the name from the album tags inside.
    An `mb:<MBID>` that no library folder owns falls back to MusicBrainz, so a
    provider call still gets a real name instead of the raw reference.
    """
    ref = next((str(v).strip() for v in (folder, artist)
                if str(v or "").strip().lower().startswith("mb:")), "")
    if ref:
        try:
            payload = intg.mb_get_cached(f"artist/{ref[3:].strip()}", {"fmt": "json"})
            name = str((payload or {}).get("name") or "").strip()
            if name:
                return name
        except Exception:
            pass
    name = os.path.basename(str(folder or artist or "").rstrip("\\/"))
    name = artistdata.strip_mbid_suffix(name)
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


# --------------------------------------------------------------------------- #
# The metadata step: artist image + artist/album descriptions
# --------------------------------------------------------------------------- #
# The three things an import has to find and account for. ONE implementation,
# called by the auto-import chain (server.soulseek_auto) and by the import
# menu's own route below: a second one would drift from this within a day.
META_ITEMS = ("artist_image", "artist_description", "album_description")

META_LABELS = {
    "artist_image": "Artist image",
    "artist_description": "Artist description",
    "album_description": "Album description",
}


def meta_line(item, res):
    """One log line for one item's result: what happened, and from where."""
    detail = f" — {res.get('detail')}" if res.get("detail") else ""
    return f"{META_LABELS.get(item, item)}: {res.get('state')}{detail}"


def _meta(state, source=None, detail=None):
    return {"state": state, "source": source, "detail": detail}


def ensure_artist_album_metadata(album_dir, cfg=None, force=False, progress=None,
                                 items=None):
    """Fetch and store an album's artist image and artist/album descriptions.

    THE step both the auto-import chain and the import menu's route call
    (``server.soulseek_auto``, ``POST /api/album/metadata/fetch``): one place
    decides what "already there" means, which provider answers, and what the
    honest outcome was.

    Returns one entry per requested item (``artist_image``,
    ``artist_description``, ``album_description``), each
    ``{"state", "source", "detail"}`` with state:

      ``present``    already stored — nothing was fetched (``force`` refetches)
      ``disabled``   switched off in Settings, so nothing was fetched
      ``not-found``  no folder or provider could answer; nothing was written
      ``fetched``    a provider answered and the content was stored
      ``error``      the fetch or the write failed; ``detail`` says why

    ``progress`` is called ``progress(done, total, label)`` (the house progress
    convention), ``items`` restricts the run to some of the three so a caller
    with a progress bar can drive them one at a time. Never raises: an import
    must not fail because a provider is down."""
    cfg = cfg or load_config()
    album_dir = os.path.normpath(str(album_dir or ""))
    wanted = [i for i in (items or META_ITEMS) if i in META_ITEMS]
    if not os.path.isdir(album_dir):
        return {i: _meta("error", detail="album folder not found") for i in wanted}

    artist, album = _album_identity(album_dir)
    folder = artistdata.artist_dir(cfg, artist)
    if not artist and folder:
        # Tags carry nothing (a folder nobody tagged): the artist folder's own
        # name is the artist, MBID suffix stripped.
        artist = _artist_name(folder)

    out = {}
    fetched = False
    for done, item in enumerate(wanted, 1):
        if progress:
            try:
                progress(done, len(wanted), META_LABELS[item])
            except Exception:
                pass
        if item == "artist_image":
            out[item] = _artist_image_item(folder, artist, cfg, force)
        elif item == "artist_description":
            out[item] = _artist_description_item(folder, artist, cfg, force)
        else:
            out[item] = _album_description_item(album_dir, artist, album, cfg, force)
        fetched = fetched or out[item]["state"] == "fetched"
    if fetched:
        # The album's folder and the artist's folder both just changed.
        tagcache.invalidate_all()
    return out


def _artist_image_item(folder, artist, cfg, force):
    """Artist photo: what is stored, or the best one a provider has."""
    if not force and folder and artistdata.has_image(folder):
        # "present — deezer": who stored it is part of accounting for it.
        return _meta("present", source=artistdata.read_provenance(folder).get("source"),
                     detail=os.path.basename(artistdata.image_path(folder) or "") or None)
    if not cfg.get("artist_image_enabled", True):
        return _meta("disabled", detail="automatic artist images are off in Settings")
    if not folder or not artist:
        return _meta("not-found", detail="no artist folder in the library yet")
    source = None
    try:
        hit = discovery.artist_image(artist, mbid=artistdata.folder_mbid(folder) or artistdata.folder_mbid(artist), cfg=cfg) or {}
        url = hit.get("url")
        if not url:
            return _meta("not-found", detail="no source had an image")
        source = hit.get("source")
        # Same fetcher the artist page's own Fetch button uses, so the
        # automatic pick and the manual one can never disagree.
        data, _ctype, _src = artcache.fetch_art(url, artist=artist, cfg=cfg)
        if not data:
            return _meta("not-found", source=source, detail="the image could not be downloaded")
        path = artistdata.save_image(folder, data, cfg, source=source or "auto",
                                     source_url=url, kind="artist",
                                     label=hit.get("label"))
        if not path:
            return _meta("error", source=source, detail="what the source served is not a usable image")
        return _meta("fetched", source=source, detail=os.path.basename(path))
    except Exception as e:
        traceback.print_exc()
        return _meta("error", source=source, detail=str(e))


def _artist_description_item(folder, artist, cfg, force):
    if not force and folder and artistdata.has_description(folder):
        return _meta("present", source=_stored_description_source(folder))
    if not cfg.get("artist_description_enabled", True):
        return _meta("disabled", detail="automatic artist descriptions are off in Settings")
    if not folder or not artist:
        return _meta("not-found", detail="no artist folder in the library yet")
    try:
        found = discovery.artist_description(artist, mbid=artistdata.folder_mbid(folder) or artistdata.folder_mbid(artist), cfg=cfg) or {}
    except Exception as e:
        traceback.print_exc()
        return _meta("error", detail=str(e))
    text = str(found.get("text") or "").strip()
    if not text:
        return _meta("not-found", detail="no source had a description")
    return _store_description(folder, text, found, cfg, kind="artist")


def _album_description_item(album_dir, artist, album, cfg, force):
    if not force and artistdata.has_description(album_dir):
        return _meta("present", source=_stored_description_source(album_dir))
    if not cfg.get("album_description_enabled", True):
        return _meta("disabled", detail="automatic album descriptions are off in Settings")
    if not album:
        return _meta("not-found", detail="no album name to look up")
    try:
        found = discovery.album_description(artist, album, cfg=cfg) or {}
    except Exception as e:
        traceback.print_exc()
        return _meta("error", detail=str(e))
    text = str(found.get("text") or "").strip()
    if not text:
        return _meta("not-found", detail="no source had a description")
    return _store_description(album_dir, text, found, cfg, kind="album")


def _stored_description_source(folder):
    """Which provider a stored description came from (None when unrecorded)."""
    prov = artistdata.read_provenance(folder)
    return prov.get("description_source") or prov.get("source")


def _store_description(folder, text, found, cfg, kind):
    """Write a description plus its provenance; the ``_meta`` result for it."""
    source = found.get("source")
    path = artistdata.write_description(folder, text, cfg=cfg, source=source,
                                       source_url=found.get("source_url"), kind=kind)
    if not path:
        return _meta("error", source=source, detail="the description could not be written")
    artistdata.write_provenance(folder, {
        "description_source": source,
        "description_source_url": found.get("source_url"),
        "description_title": found.get("title")}, kind=kind, cfg=cfg)
    return _meta("fetched", source=source, detail=found.get("title"))


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #
@router.get("/api/discovery/sources")
def discovery_sources():
    """The provider catalogue the Settings order pickers read.

    Only the sources that still have a feature behind them are reported: the
    artist-image and description chains (Settings → *Artist images &
    descriptions*, and the genre picker's own list).
    """
    cfg = load_config()
    cat = discovery.sources_catalog()
    return {
        **cat,
        "enabled": bool(cfg.get("discovery_enabled", True)),
        "saved": {
            "artist_image_sources": cfg.get("artist_image_sources") or [],
            "description_sources": cfg.get("description_sources") or [],
        },
    }


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
    mbid = artistdata.folder_mbid(artist)
    auto = discovery.artist_image(name, mbid=mbid, cfg=cfg)
    if auto:
        add(auto.get("url"), auto.get("source"), auto.get("label") or "Automatic pick")
    db = discovery.audiodb_artist_mbid(mbid) if mbid else None
    if not db:
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
        auto = discovery.artist_image(_artist_name(folder, req.artist), mbid=artistdata.folder_mbid(folder) or artistdata.folder_mbid(req.artist), cfg=cfg)
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
        found = discovery.artist_description(_artist_name(folder, req.artist), mbid=artistdata.folder_mbid(folder) or artistdata.folder_mbid(req.artist), cfg=cfg)
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


@router.post("/api/album/metadata/fetch")
def album_metadata_fetch(req: AlbumMetadataRequest):
    """Find and account for an album's artist image and descriptions.

    The import menu's own way into the auto-import's metadata step
    (``ensure_artist_album_metadata`` above) — same callable, so the menu can
    never do something the unattended import would not. ``items`` runs a
    subset (one call per item keeps a progress bar honest), ``force`` refetches
    what is already stored.

    A switched-off feature is reported per item as ``disabled`` instead of
    refused with a 403: one call accounts for all three, and one toggle being
    off must not hide the state of the others."""
    cfg = load_config()
    if not req.path:
        raise HTTPException(400, "path is required")
    album_dir = os.path.normpath(req.path)
    _guard(album_dir, cfg, staged=req.staged)
    if not os.path.isdir(album_dir):
        raise HTTPException(404, "album not found")
    items = ensure_artist_album_metadata(album_dir, cfg, force=req.force,
                                         items=[str(i) for i in req.items] or None)
    return {"ok": True, "path": album_dir.replace("\\", "/"), "items": items}


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
