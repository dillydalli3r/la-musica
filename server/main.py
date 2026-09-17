"""
FastAPI backend for la musica v2 — localhost:8000.

Wraps the mlo/* engine as REST + WebSocket for the React frontend:
library (tag-rich, sortable), grading/auditing, tag editing, playback
streaming, playlists (manual + smart, .m3u8), MusicBrainz/LRCLIB/RYM
integrations, and album import.
"""
import os
import re
import sys
import asyncio
import glob
import json
import threading
import time
import pathlib
import tempfile
from contextlib import asynccontextmanager
from typing import List, Optional
from urllib.parse import quote

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

from mlo import __version__ as APP_VERSION
from mlo import load_config, save_config
from mlo.config import DEFAULT_CONFIG
from mlo import stats as stats_mod

from server import library as lib_mod
from server import mbresolve
from server import playlists as pl_mod
from server import integrations as intg
from server import tagcache
from server import exporter
from server import api_discovery
from server import api_imports
from server import api_lyrics
from server import discovery
from server import artcache
from mlo.paths import (SKIP_DIRS, clear_track_covers, downloads_dir,
                       is_video_file, library_root, load_track_covers, move_path,
                       save_track_covers, set_track_covers, trash_dir)

# Captured at startup — worker threads use run_coroutine_threadsafe against
# this loop to relay script progress over the WebSocket (get_event_loop()
# from a worker thread is unreliable and deprecated).
_MAIN_LOOP = None


@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _MAIN_LOOP
    _MAIN_LOOP = asyncio.get_running_loop()
    # Optionally bring up the managed slskd process with the backend.
    if load_config().get("soulseek_autostart", False):
        def _autostart_slskd():
            try:
                from server import soulseek
                ok, msg = soulseek.start()
                if ok:
                    soulseek.wait_until_ready()
                else:
                    print(f"[mlo] slskd autostart skipped: {msg}")
            except Exception as e:
                print(f"[mlo] slskd autostart failed: {e}")
        threading.Thread(target=_autostart_slskd, daemon=True).start()
    # Wishlist worker: periodically re-searches Soulseek for saved releases.
    try:
        from server import wishes_worker
        wishes_worker.start()
    except Exception as e:
        print(f"[mlo] wishes worker failed to start: {e}")
    yield
    try:
        from server import wishes_worker
        wishes_worker.stop()
    except Exception:
        pass


app = FastAPI(title="la musica API", version=APP_VERSION, lifespan=_lifespan)

# Docker/bootstrap: MLO_MUSIC_FOLDER env seeds music_folder when unset.
_MLO_ENV_FOLDER = os.environ.get("MLO_MUSIC_FOLDER")
if _MLO_ENV_FOLDER:
    _cfg = load_config()
    if not _cfg.get("music_folder"):
        _cfg["music_folder"] = os.path.normpath(_MLO_ENV_FOLDER)
        save_config(_cfg)

app.add_middleware(
    CORSMiddleware,
    # tauri.localhost is the Windows WebView2 form of the tauri:// origin.
    allow_origins=[
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:3000", "http://localhost:1420",
        "tauri://localhost", "http://tauri.localhost", "https://tauri.localhost",
    ],
    # Dev servers pick arbitrary ports; any localhost origin may talk to the
    # local backend. This also keeps <audio crossorigin> media loads working,
    # which the playback visualizer's WebAudio graph requires.
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# The /api/library payload is large (every track's tags + grading details);
# gzip cuts it ~10x for a cheap first-paint win on big libraries.
app.add_middleware(GZipMiddleware, minimum_size=1024)

# Feature routers live in their own modules (discovery/artwork/lyrics/import)
# so each provider layer stays independently testable; main.py only wires
# them. They are included here, right after the middleware, before the rest
# of the app's own routes.
app.include_router(api_discovery.router)
app.include_router(api_imports.router)
app.include_router(api_lyrics.router)

# Script 8 (Auto tagging) writes MOOD from the audio itself and fills a
# missing GENRE through a provider hook its caller supplies — the engine
# never imports this layer. Registering the discovery chain here means a
# library-wide Auto tagging run gets genres exactly like the import pipeline.
from mlo import autotag as _autotag  # noqa: E402
_autotag.set_genre_lookup(discovery.genre_lookup)

# --------------------------------------------------------------------------- #
# Progress relay (WebSocket + original hook)
# --------------------------------------------------------------------------- #
progress_clients: set[WebSocket] = set()
# Guards progress_clients: the engine's progress hook runs on worker threads
# while the event loop adds/discards sockets, so every read or write of the
# set happens under this lock (an unguarded list() can raise RuntimeError:
# "Set changed size during iteration" straight into the engine).
_progress_lock = threading.Lock()

orig_hook = stats_mod.progress_hook


def _relay(done, total, desc):
    try:
        if orig_hook:
            orig_hook(done, total, desc)
    except Exception:
        pass
    loop = _MAIN_LOOP
    if loop is None or loop.is_closed():
        return
    with _progress_lock:
        clients = list(progress_clients)
    for ws in clients:
        try:
            asyncio.run_coroutine_threadsafe(
                ws.send_json({"done": done, "total": total, "desc": desc}), loop
            )
        except Exception:
            pass


stats_mod.progress_hook = _relay
stats_mod.tqdm = None


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class RunRequest(BaseModel):
    ids: List[int]
    targets: Optional[List[str]] = None
    force: Optional[dict] = None


class LyricsEmbedRequest(BaseModel):
    path: str
    lyrics: str


class PlaylistCreate(BaseModel):
    name: str
    kind: str = "manual"
    filter: Optional[dict] = None


class PlaylistUpdate(BaseModel):
    """Partial playlist update (currently: rename)."""
    name: str | None = None


class PlaylistTracks(BaseModel):
    paths: List[str]
    position: Optional[int] = None


class SmartFilter(BaseModel):
    filter: dict


class MatchRequest(BaseModel):
    album_path: str
    release_id: str


class AssignTagsRequest(BaseModel):
    """Write MB/RYM links to tags. `tracks` maps track path -> {tag: value}."""
    tracks: dict


class ImportCommit(BaseModel):
    """Store MB/RYM links on an imported album. target_dir = album folder
    name under music_folder, or an absolute path already inside it."""
    target_dir: str
    mb_link: Optional[str] = None
    rym_link: Optional[str] = None


class ImportExpected(BaseModel):
    """Record the MusicBrainz release's full tracklist on an imported album.

    An album imported PARTIALLY carries no trace of the tracks that were
    never brought in, so the album page has nothing to grey out. Writing the
    release's own running order at match time is what makes the missing
    tracks visible. `tracks` is [{disc, position, title, recording_mbid}]."""
    target_dir: str
    release_id: Optional[str] = None
    tracks: List[dict] = []


class DownloadsDelete(BaseModel):
    """Basenames of <music>/.mlo/downloads entries to delete permanently."""
    names: List[str] = []


class DownloadsImport(BaseModel):
    """Basenames of <music>/.mlo/downloads entries to move into the library."""
    names: List[str] = []


class AlbumRemove(BaseModel):
    path: str


class TrashDelete(BaseModel):
    """Basenames of trash-bin entries to delete permanently."""
    names: List[str] = []


class TrashRestore(BaseModel):
    """Basenames to move back out of the bin. `dest` is the fallback folder
    for entries whose original location was never recorded."""
    names: List[str] = []
    dest: Optional[str] = None


# --------------------------------------------------------------------------- #
# Health / config
# --------------------------------------------------------------------------- #
@app.post("/api/shutdown")
def shutdown_backend():
    """Stop the backend process itself.

    Only honored when this backend was spawned by a launcher that set
    MLO_ALLOW_SHUTDOWN=1 (the tray app / desktop shell), so they can stop
    even backends they didn't spawn (e.g. after a restart of the shell).
    """
    if os.environ.get("MLO_ALLOW_SHUTDOWN") != "1":
        raise HTTPException(403, "shutdown not enabled for this backend")

    def _die():
        time.sleep(0.3)
        os._exit(0)

    threading.Thread(target=_die, daemon=True).start()
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"status": "ok", "version": APP_VERSION}


@app.get("/api/config")
def get_config():
    return load_config()


@app.post("/api/config")
def set_config(cfg: dict):
    ok = save_config(cfg)
    if not ok:
        raise HTTPException(500, "Failed to save config")
    # A settings change can alter what the recommendation shelf and the
    # discovery chains return (source order, counts, providers on/off), and
    # the library payload carries grading results that depend on the grader
    # toggles — drop both caches so the next read reflects the new config
    # instead of up to 15 minutes of stale rows.
    try:
        from server import recommendations
        recommendations.invalidate()
    except Exception:
        pass
    tagcache.invalidate_all()
    return load_config()


@app.get("/api/config/defaults")
def get_config_defaults():
    """Factory defaults — powers the settings UI's reset-to-defaults actions."""
    return DEFAULT_CONFIG


# --------------------------------------------------------------------------- #
# Library
# --------------------------------------------------------------------------- #
def _music_folder(cfg=None):
    cfg = cfg or load_config()
    folder = cfg.get("music_folder") or ""
    if not folder or not os.path.isdir(folder):
        raise HTTPException(400, "music_folder not set or not found")
    return folder


def _in_music_folder(p, folder):
    """Path-containment guard shared by every path-taking endpoint.

    Compares normalized absolute paths at directory boundaries so a
    sibling like C:\\Music2 is never treated as being inside C:\\Music.
    """
    try:
        ap = os.path.abspath(os.path.normpath(p))
        af = os.path.abspath(os.path.normpath(folder))
    except (OSError, ValueError, TypeError):
        return False
    if ap == af:
        return True
    # Paths on different drives (C: vs F:, or two UNC shares) have no common
    # ancestor at all; commonpath raises ValueError there, which means
    # "outside" — not a 500 out of every path-taking endpoint.
    if os.path.normcase(os.path.splitdrive(ap)[0]) != os.path.normcase(os.path.splitdrive(af)[0]):
        return False
    try:
        common = os.path.commonpath([ap, af])
    except (OSError, ValueError, TypeError):
        return False
    return os.path.normcase(common) == os.path.normcase(af)


def _skip_names():
    return {d.lower() for d in SKIP_DIRS}


@app.get("/api/library")
def library():
    """Tag-rich library tree: artists -> albums -> tracks (grade/audit + tags)."""
    cfg = load_config()
    return lib_mod.build_library(cfg)


@app.get("/api/home")
def home():
    """Home page: stats, recent additions, top grades and MusicBrainz-backed
    album recommendations derived from the library's own taste."""
    from server import recommendations
    try:
        return recommendations.build_home(load_config())
    except Exception as e:
        raise HTTPException(502, f"home payload failed: {e}")


@app.post("/api/naming/preview")
def naming_preview(req: dict):
    """Evaluate a naming script against a sample track so Settings can show
    what the folder structure would look like before running Organize."""
    from server.naming import DEFAULT_NAMING_SCRIPT, eval_script, track_variables
    script = str(req.get("script") or "").strip() or DEFAULT_NAMING_SCRIPT
    shorter = bool(req.get("short_folder_names"))
    sample = req.get("sample") or {}
    tags = {
        "ALBUMARTIST": str(sample.get("albumartist") or "System of a Down"),
        "ARTIST": str(sample.get("artist") or "System of a Down"),
        "ALBUM": str(sample.get("album") or "Toxicity"),
        "DATE": str(sample.get("date") or "2001-09-04"),
        "ORIGINALDATE": str(sample.get("originaldate") or "2001-08-27"),
        "RELEASETYPE": str(sample.get("releasetype") or "album"),
        "RELEASECOUNTRY": str(sample.get("releasecountry") or "US"),
        "MEDIA": str(sample.get("media") or "CD"),
        "CATALOGNUMBER": str(sample.get("catalognumber") or "CK 62240"),
        "DISCNUMBER": "1", "TRACKNUMBER": "4", "TITLE": str(sample.get("title") or "Psycho"),
        "MUSICBRAINZ_ALBUMID": "f8a44d0f-8241-3bdd-9988-413f28606650",
        "MUSICBRAINZ_ALBUMARTISTID": "cc0b7089-5d5c-4c2e-a48f-7b9c3e5d1a2b",
    }
    try:
        path = eval_script(script, track_variables(tags, tags["RELEASETYPE"]), shorter_ids=shorter)
        return {"path": path, "ok": True}
    except Exception as e:
        return {"path": None, "ok": False, "error": str(e)}


@app.post("/api/open-folder")
def open_folder(req: AlbumRemove):
    """Reveal a folder in the OS file manager (Windows/macOS/Linux)."""
    import subprocess
    import sys as _sys
    p = os.path.normpath(req.path)
    if not os.path.isdir(p):
        raise HTTPException(404, "folder not found")
    if not _in_music_folder(p, _music_folder()):
        raise HTTPException(400, "folder outside music folder")
    try:
        if _sys.platform == "win32":
            os.startfile(p)  # type: ignore[attr-defined]
        elif _sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
    except Exception as e:
        raise HTTPException(500, f"could not open folder: {e}")
    return {"ok": True}


@app.get("/api/dependencies")
def dependencies():
    """Installed external tools (.dependencies + PATH) vs. the pinned
    versions the scripts expect, with optional GitHub 'latest' check."""
    from mlo.tools import detect_all_tools, DEPS_DIR
    from mlo.fetchdeps import DISPLAY_NAMES, installed_versions, latest_versions
    tools = detect_all_tools()
    installed = installed_versions()
    latest = {}
    try:
        latest = latest_versions()
    except Exception:
        pass
    out = []
    for key, name in DISPLAY_NAMES.items():
        info = tools.get(key) or {}
        exe = next((v for k, v in info.items() if k.endswith("_exe") and v), None)
        ver = info.get("version")
        iv = installed.get(key)
        lv = latest.get(key)
        present = bool(iv or info)
        if not present:
            state = "missing"
        elif lv and (iv or ver) and lv != (iv or ver):
            state = "update"
        else:
            state = "ok"
        out.append({
            "key": key,
            "name": name,
            "installed_version": iv,
            "latest_version": lv,
            "detected_version": ver,
            "path": exe,
            "state": state,
        })
    return {"deps_dir": str(DEPS_DIR), "tools": out}


def _check_source_kind(kind):
    """400 on an unknown `kind` filter (both sources routes validate it)."""
    if not kind:
        return
    from server import sources_health as health_mod

    if kind not in health_mod.KINDS:
        raise HTTPException(400, f"unknown source kind: {kind} "
                                 f"(one of {', '.join(health_mod.KINDS)})")


@app.get("/api/sources/health")
def sources_health(kind: str = Query(None), probe: int = Query(0)):
    """Which external sources work right now — the wizard's and Settings' one
    answer, for all four kinds at once (`lyrics`, `advisory`, `genre`,
    `metadata`).

    `probe=0` (default) reports the CONFIG only and performs no request at
    all: unconfigured sources are `skipped` (with the config keys they need)
    and the rest `ok`. `probe=1` runs one cheap lookup per configured source
    against the same fixed sample the lyrics providers already probe with —
    in parallel, a few seconds in total — and says what answered.
    """
    from server import sources_health as health_mod

    _check_source_kind(kind)
    return health_mod.health_payload(load_config(), kind=kind, probe=bool(probe))


@app.get("/api/sources/health/{source_id}")
def sources_health_source(source_id: str, kind: str = Query(None),
                          probe: int = Query(1)):
    """One source's row — same shape, for a per-source Test button.

    Probes by default here: asking about ONE source is a deliberate test.

    The row is returned BARE — the same object `/api/sources/health` puts in
    `sources`, so a per-source Test button reads one shape.

    An id can belong to two kinds (`deezer` and `itunes` are both a genre
    source and a metadata provider), so the row kinds are searched in the
    payload's own order — lyrics, advisory, genre, metadata — unless `kind=`
    picks one. The row always carries its kind, so a caller that sends both
    ids (`kind=id`) is never guessing.
    """
    from server import sources_health as health_mod

    _check_source_kind(kind)
    if source_id not in health_mod.source_ids():
        raise HTTPException(404, f"unknown source: {source_id}")
    payload = health_mod.health_payload(load_config(), kind=kind,
                                        probe=bool(probe))
    row = next((r for r in payload["sources"] if r["id"] == source_id), None)
    if row is None:
        raise HTTPException(404, f"{source_id} is not a {kind} source")
    return row


@app.get("/api/videos/thumb")
def videos_thumb(path: str = Query(...), t: float = Query(0.0),
                 w: int = Query(320)):
    """One JPEG frame of a library video at *t* seconds (scrub preview).

    Seeking happens before `-i` (keyframe seek) and the frame is cached under
    `<music>/.mlo/data/thumbs/`, keyed by path+mtime+width+whole second of
    *t* — so dragging the scrubber re-encodes nothing. *t* is clamped to the
    file's length when ffprobe knows it, and *w* to a sane range.
    """
    from server import thumbs

    p = os.path.normpath(mbresolve.resolve_track(path) or path)
    if not os.path.isfile(p):
        raise HTTPException(404, "file not found")
    if not _in_music_folder(p, _music_folder()):
        raise HTTPException(400, "file outside music folder")
    if not is_video_file(p):
        raise HTTPException(400, "not a video file")
    try:
        fp = thumbs.thumb_file(p, t=t, w=w)
    except thumbs.ThumbError as e:
        raise HTTPException(e.status, str(e))
    # Private, long-lived: the key already carries the file's mtime, so a
    # changed file is a different URL as far as any cache is concerned.
    return FileResponse(fp, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})


class DepsInstallRequest(BaseModel):
    keys: Optional[List[str]] = None


@app.post("/api/dependencies/install")
def dependencies_install(req: DepsInstallRequest):
    """Install/update external tools from their pinned GitHub releases."""
    from mlo import fetchdeps
    wanted = set(req.keys or [])
    results = []
    for key, name in fetchdeps.DISPLAY_NAMES.items():
        if wanted and key not in wanted:
            continue
        try:
            fetchdeps.install_dependency(key, log=lambda m: None)
            results.append({"key": key, "name": name, "ok": True})
        except Exception as e:
            results.append({"key": key, "name": name, "ok": False, "error": str(e)})
    try:
        fetchdeps.refresh_tool_cache()
    except Exception:
        pass
    return {"results": results}


@app.get("/api/album/mbdetect")
def album_mbdetect(path: str = Query(...)):
    """Live scan: find a MusicBrainz release ID in ANY track tag.

    Scans every audio file's raw tags case-insensitively for a key
    containing 'musicbrainz' + 'album' (catches MUSICBRAINZ_ALBUMID and
    variants written by other taggers), and also checks the mapped
    MUSICBRAINZ_ALBUMID / MUSICBRAINZ_RELEASEGROUPID tags directly.
    """
    import re
    from mlo.audio import AudioFile

    p = os.path.normpath(path)
    if not os.path.isdir(p):
        raise HTTPException(404, "album not found")
    if not _in_music_folder(p, _music_folder()):
        raise HTTPException(400, "album outside music folder")
    uuid_re = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
    # Recursive: some libraries nest the album folder inside a folder of the
    # same name, so scan subdirectories too. Album tags are uniform across
    # tracks, so checking a handful of audio files is enough — this keeps the
    # scan instant even on huge folders.
    scanned = 0
    MAX_SCAN = 12
    skip = _skip_names()
    for root, dirs, files in os.walk(p):
        dirs[:] = [d for d in dirs if d.lower() not in skip]
        for f in sorted(files):
            if not is_audio_file(f):
                continue
            scanned += 1
            if scanned > MAX_SCAN:
                return {"mbid": None, "truncated": True}
            af = AudioFile(os.path.join(root, f))
            if af.audio is None:
                continue
            for key in ("MUSICBRAINZ_ALBUMID", "MUSICBRAINZ_RELEASEGROUPID"):
                v = af.get_tag(key)
                m = uuid_re.search(str(v)) if v else None
                if m:
                    rel = os.path.relpath(os.path.join(root, f), p)
                    return {"mbid": m.group(0).lower(), "key": key, "track": rel}
            try:
                for k, v in (af.all_tags() or {}).items():
                    kl = str(k).lower()
                    if "musicbrainz" in kl and "album" in kl and "artist" not in kl:
                        m = uuid_re.search(str(v)) if v else None
                        if m:
                            rel = os.path.relpath(os.path.join(root, f), p)
                            return {"mbid": m.group(0).lower(), "key": k, "track": rel}
            except Exception:
                continue
    return {"mbid": None, "scanned": scanned}


@app.get("/api/album")
def get_album(path: str = Query(...)):
    """Album detail. `path` may be a real folder or an "mb:<release MBID>"
    reference — the app links albums by MusicBrainz ID so pages survive
    reorganization."""
    p = os.path.normpath(mbresolve.resolve_album(path) or path)
    if not os.path.isdir(p):
        raise HTTPException(404, "album not found")
    if not _in_music_folder(p, _music_folder()):
        raise HTTPException(400, "album outside music folder")
    res = lib_mod.build_album(p, load_config())
    if res is None:
        raise HTTPException(404, "no audio files")
    return res


@app.get("/api/artist")
def get_artist(path: str = Query(...)):
    """Artist detail; `path` may be a real folder or an "mb:<artist MBID>"."""
    p = os.path.normpath(mbresolve.resolve_artist(path) or path)
    if not os.path.isdir(p):
        raise HTTPException(404, "artist not found")
    if not _in_music_folder(p, _music_folder()):
        raise HTTPException(400, "artist outside music folder")
    cfg = load_config()
    albums = lib_mod._find_albums(p)
    direct = [alb for alb in sorted(albums)
              if os.path.dirname(alb).lower() == p.lower()]
    albums_data = lib_mod.build_albums_parallel(direct, cfg)
    # Display name: the tag-derived artist (folders carry an MBID suffix).
    display_name = next((a.get("album_artist") for a in albums_data
                         if a.get("album_artist")), None)
    # Artwork the app itself stores for the artist (see mlo/artistdata):
    # the image, the description, and the artist-level grade that watches
    # for both. A failure here must not take the whole artist page down.
    image_file = None
    description = ""
    provenance = {}
    try:
        from mlo import artistdata
        description = artistdata.read_description(p) or ""
        image_file = os.path.basename(artistdata.image_path(p) or "") or None
        provenance = artistdata.read_provenance(p)
    except Exception:
        pass
    try:
        from mlo import grader
        grade = grader.grade_artist(p, cfg)
    except Exception as e:
        grade = {"error": str(e)}
    return {
        "path": p.replace("\\", "/"),
        "name": os.path.basename(p),
        "display_name": display_name,
        "albums": albums_data,
        "aggregate": lib_mod._aggregate_albums(albums_data),
        "artwork": {
            "image": bool(image_file),
            "image_file": image_file,
            "image_url": (f"/api/artist/image?artist={quote(p.replace(chr(92), '/'))}"
                          if image_file else None),
            "description": description.strip() or None,
            "description_source": provenance.get("description_source") or provenance.get("source"),
            "description_url": provenance.get("description_source_url"),
            "provenance": provenance,
            # May the app fetch these on the user's behalf? (Settings →
            # Artist images & descriptions.) The UI greys its Fetch actions
            # when off rather than letting the request 403.
            "auto_image": bool(cfg.get("artist_image_enabled", True)),
            "auto_description": bool(cfg.get("artist_description_enabled", True)),
        },
        "grade": grade,
    }


# --------------------------------------------------------------------------- #
# Streaming / tags
# --------------------------------------------------------------------------- #
_CTYPES = {
    ".flac": "audio/flac", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
    # Music videos: <video> elements need the video media types; matroska
    # plays in Chromium-based webviews (WebView2 / Tauri) and browsers.
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".ogg": "audio/ogg",
    ".opus": "audio/ogg", ".wav": "audio/wav", ".aac": "audio/aac",
    ".mkv": "video/x-matroska", ".webm": "video/webm", ".mov": "video/quicktime",
    ".avi": "video/x-msvideo", ".wmv": "video/x-ms-wmv", ".flv": "video/x-flv",
    ".mpg": "video/mpeg", ".mpeg": "video/mpeg", ".vob": "video/mpeg",
    ".m2v": "video/mpeg", ".ts": "video/mp2t", ".m2ts": "video/mp2t",
    ".mts": "video/mp2t", ".3gp": "video/3gpp", ".ogv": "video/ogg",
    ".mka": "audio/x-matroska",
}


@app.get("/api/stream")
def stream(path: str = Query(...)):
    p = os.path.normpath(mbresolve.resolve_track(path) or path)
    if not os.path.isfile(p):
        raise HTTPException(404, "file not found")
    if not _in_music_folder(p, _music_folder()):
        raise HTTPException(400, "file outside music folder")
    ctype = _CTYPES.get(os.path.splitext(p)[1].lower(), "application/octet-stream")
    # ponytail: unknown extensions stream as octet-stream; add explicit
    # mapping above when a supported player format is missing.
    return FileResponse(p, media_type=ctype, headers={"Accept-Ranges": "bytes"})


@app.get("/api/videos/stream")
def videos_stream(path: str = Query(...), transcode: int = Query(0)):
    """Playable stream for a library music video.

    ?transcode=0 (default) serves the file bytes as-is (seekable, exact
    quality). Codecs browsers cannot decode (MPEG-2 in VOB/MPG/M2TS/AVI,
    VC-1, ...) fail in the <video> element — the player then retries with
    ?transcode=1, which pipes the file through ffmpeg into a fragmented
    MP4 (H.264/AAC) browsers always play. Transcoded streams are not
    seekable; the picture/sound are identical in content. Files the
    browser decodes natively never touch ffmpeg.
    """
    p = os.path.normpath(mbresolve.resolve_track(path) or path)
    if not os.path.isfile(p):
        raise HTTPException(404, "file not found")
    if not _in_music_folder(p, _music_folder()):
        raise HTTPException(400, "file outside music folder")
    ext = os.path.splitext(p)[1].lower()

    if not transcode and ext in _NATIVE_VIDEO_EXTS:
        ctype = _CTYPES.get(ext, "application/octet-stream")
        return FileResponse(p, media_type=ctype, headers={"Accept-Ranges": "bytes"})

    from mlo.tools import detect_all_tools

    ffmpeg = (detect_all_tools().get("ffmpeg") or {}).get("ffmpeg_exe")
    if not ffmpeg:
        raise HTTPException(503, "ffmpeg not installed — install it under Dependencies for video playback")
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", p,
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-vf", "scale=-2:min(720\\,ih)",
        "-c:a", "aac", "-b:a", "160k", "-ac", "2",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "pipe:1",
    ]
    try:
        import subprocess

        # CREATE_NO_WINDOW: a piped ffmpeg still allocates a console on
        # Windows unless suppressed — one flashed open per video otherwise.
        # stderr goes to DEVNULL: a PIPE never drained blocks ffmpeg once
        # full, hanging the stream.
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=0x08000000 if os.name == "nt" else 0)
    except Exception as e:
        raise HTTPException(500, f"ffmpeg failed to start: {e}")

    from starlette.responses import StreamingResponse

    def _gen():
        try:
            if proc.stdout is None:
                return
            while True:
                chunk = proc.stdout.read(256 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            if proc.poll() is None:
                proc.kill()
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass

    return StreamingResponse(_gen(), media_type="video/mp4")


# Video/audio codecs Chromium-based webviews (WebView2/Tauri, Chrome, Firefox)
# decode natively. Deliberately conservative: AC-3/E-AC-3/DTS decode in
# Edge/WebView2 but NOT in plain Chrome (video plays with no sound and no
# error), so those transcode to AAC; MPEG-4 Part 2 / VC-1 / WMV fail hard.
_NATIVE_VIDEO_CODECS = {"h264", "vp8", "vp9", "av1", "hevc", "theora"}
_NATIVE_AUDIO_CODECS = {"aac", "mp3", "opus", "vorbis", "flac", "alac",
                        "pcm_u8", "pcm_s16le", "pcm_s24le", "pcm_s16be",
                        "pcm_f32le"}
_playback_meta_cache: dict = {}


@app.get("/api/videos/meta")
def videos_meta(path: str = Query(...)):
    """Playback decision for a music video: can the browser decode the file
    natively (container + codec probe) and how long is it (seconds).

    The player uses `native` to pick the stream URL upfront instead of
    guessing and retrying on error — this also catches the silent case
    (decodable video, undecodable audio) that never raises an error event.
    `duration` is ffprobe's container length, which fragmented-MP4 live
    transcodes cannot carry (the element reports Infinity there). Cached
    per path+mtime; probes run once per file version."""
    from mlo.remux import _stream_info
    from mlo.tools import detect_all_tools

    p = os.path.normpath(mbresolve.resolve_track(path) or path)
    if not os.path.isfile(p):
        raise HTTPException(404, "file not found")
    if not _in_music_folder(p, _music_folder()):
        raise HTTPException(400, "file outside music folder")
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        mtime = 0.0
    hit = _playback_meta_cache.get(p)
    if hit and hit[0] == mtime:
        return {"path": p.replace("\\", "/"), **hit[1]}

    ext = os.path.splitext(p)[1].lower()
    ffprobe = (detect_all_tools().get("ffmpeg") or {}).get("ffprobe_exe")
    info = _stream_info(p, ffprobe) if ffprobe else None
    vcodec = info[0] if info else None
    # Only the FIRST audio track matters: videos_stream maps 0:a:0, so the
    # other tracks never reach the browser.
    acodecs = info[1] if info else []
    duration = info[3] if info else None
    if info is None:
        native, reason = False, "unreadable by ffprobe — live transcode"
    elif ext not in _NATIVE_VIDEO_EXTS:
        native, reason = False, f"{ext} container is always transcoded"
    elif (vcodec or "").lower() not in _NATIVE_VIDEO_CODECS:
        native, reason = False, f"{vcodec or 'unknown'} video codec"
    elif acodecs and (acodecs[0] or "").lower() not in _NATIVE_AUDIO_CODECS:
        native, reason = False, f"{acodecs[0]} audio codec"
    else:
        native, reason = True, None
    meta = {"native": native, "reason": reason, "duration": duration,
            "video_codec": vcodec, "audio_codecs": acodecs}
    _playback_meta_cache[p] = (mtime, meta)
    return {"path": p.replace("\\", "/"), **meta}


@app.get("/api/tags")
def get_tags(path: str = Query(...)):
    """Read-only tag/lyrics/cover view (tag *writing* was removed — the
    engine's grading/auditing scripts own all tag writes now). Accepts an
    "mb:<recording MBID>" reference as well as a path."""
    from mlo.audio import AudioFile
    p = os.path.normpath(mbresolve.resolve_track(path) or path)
    if not os.path.isfile(p):
        raise HTTPException(404, "file not found")
    if not _in_music_folder(p, _music_folder()):
        raise HTTPException(400, "file outside music folder")
    af = AudioFile(p)
    if af.audio is None:
        raise HTTPException(500, af.error or "unreadable")
    tags, tech = tagcache.read_track(p)
    if not tags:
        tags = af.all_tags() or {}
    try:
        lyr = af.get_lyrics()
    except Exception:
        lyr = None
    lyrics_source = "embedded" if lyr else None
    if not lyr:
        # fall back to the .lrc sidecar so lyrics_format=LRC libraries still
        # show lyrics in the player and editor
        lrc_path = os.path.splitext(p)[0] + ".lrc"
        if os.path.isfile(lrc_path):
            try:
                with open(lrc_path, "r", encoding="utf-8", errors="replace") as fh:
                    lyr = fh.read()
                lyrics_source = "sidecar"
            except Exception:
                pass

    # Stored lyric transforms: embedded TRANSLITERATION / TRANSLATION tags
    # first, then the .romaji.lrc / .<lang>.lrc sidecars. Stored data only —
    # the player renders these directly.
    from mlo.lyrics import XLIT_SIDECAR, _same_essence, needs_transliteration

    def _read_text(sp):
        if not os.path.isfile(sp):
            return None
        try:
            with open(sp, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except Exception:
            return None

    def _translation_sidecar():
        """First "<stem>.<lang>.lrc" next to the track. "<stem>.lrc" is the
        main lyrics file and "<stem>.romaji.lrc" the romanization, neither of
        which is a translation."""
        stem = os.path.splitext(p)[0]
        for cand in sorted(glob.glob(glob.escape(stem) + ".*.lrc")):
            if not cand.lower().endswith(XLIT_SIDECAR):
                return _read_text(cand)
        return None

    # Language-specific transform tags (TRANSLITERATION-JA-LATN,
    # TRANSLATION-EN, …) read first; the bare legacy names still read.
    xlit = str(af.get_lyrics_transform("TRANSLITERATION") or "").strip() \
        or _read_text(os.path.splitext(p)[0] + XLIT_SIDECAR) or None
    trans = str(af.get_lyrics_transform("TRANSLATION") or "").strip() \
        or _translation_sidecar() or None
    # Suppress REDUNDANT transforms — romanizing lyrics that are already in
    # Latin script and "translating" lyrics into themselves produce the
    # useless sub-lines the sidebar used to render under e.g. English songs.
    _lyr_text = str(lyr or "")
    if xlit is not None and not needs_transliteration(_lyr_text):
        xlit = None
    if trans is not None and _same_essence(trans, _lyr_text):
        trans = None
    cover = None
    try:
        alb = os.path.dirname(p)
        for cand in ("cover.jpg", "cover.jpeg", "cover.png", "cover.jxl", "cover.webp", "cover.bmp"):
            if os.path.isfile(os.path.join(alb, cand)):
                cover = os.path.join(alb, cand).replace("\\", "/")
                break
    except Exception:
        pass
    return {"path": p.replace("\\", "/"), "tags": tags, "lyrics": lyr,
            "lyrics_source": lyrics_source, "cover": cover, "tech": tech,
            "lyrics_xlit": xlit, "lyrics_trans": trans}


@app.get("/api/replaygain")
def get_replaygain(path: str = Query(...), mode: str = Query("")):
    """ReplayGain for one track: the gain the player should apply, in dB.

    The player applies this in its WebAudio gain stage so loudness stays even
    between tracks — it is playback metadata, deliberately not surfaced as a
    column. `mode` (track/album/off) overrides the saved `replaygain_mode`
    for one request; album mode prefers REPLAYGAIN_ALBUM_GAIN and falls back
    to the track value. A file whose tags carry no ReplayGain is measured on
    the spot (ffmpeg EBU R128) and cached under `.mlo/data/replaygain.json`,
    so a library that was never run through script 7 still plays level.
    """
    from mlo import loudness

    p = os.path.normpath(mbresolve.resolve_track(path) or path)
    if not os.path.isfile(p):
        raise HTTPException(404, "file not found")
    if not _in_music_folder(p, _music_folder()):
        raise HTTPException(400, "file outside music folder")
    cfg = load_config()
    res = loudness.replaygain_for_path(cfg, p, mode=(mode or None))
    return {
        "path": p.replace("\\", "/"),
        "gain": res.get("gain"),
        "peak": res.get("peak"),
        "mode": res.get("mode"),
        "source": res.get("source"),
        "analyzed": bool(res.get("analyzed")),
    }


@app.get("/api/cover")
def get_cover(request: Request, album: str = Query(...), file: Optional[str] = Query(None),
              color: int = Query(0)):
    """Serve an album's cover art, cached with ETag; ?color=1 returns the
    dominant color instead of the image bytes (UI tinting). Accepts an
    "mb:<release MBID>" album reference."""
    alb = os.path.normpath(mbresolve.resolve_album(album) or album)
    if not os.path.isdir(alb):
        raise HTTPException(404, "album not found")
    if not _in_music_folder(alb, _music_folder()):
        raise HTTPException(400, "album outside music folder")
    if color:
        c = tagcache.cover_color(alb, file)
        if c is None:
            raise HTTPException(404, "no cover")
        return {"color": c, "album": alb.replace("\\", "/")}
    data, ctype, etag = tagcache.cover_bytes(alb, file)
    if data is None:
        raise HTTPException(404, "no cover")
    from fastapi.responses import Response
    headers = {"Cache-Control": "public, max-age=3600", "Accept-Ranges": "bytes"}
    if etag:
        headers["ETag"] = f'"{etag}"'
        inm = request.headers.get("if-none-match")
        if inm and inm.strip('"') == etag:
            return Response(content=b"", status_code=304, headers=headers)
    return Response(content=data, media_type=ctype, headers=headers)


@app.get("/api/art")
async def proxy_art(url: str = Query(...), artist: str = Query(""),
                    album: str = Query(""), rg: str = Query("")):
    """Serve provider artwork through the app instead of hotlinking it.

    The UI is handed CDN URLs (Deezer, iTunes, the Cover Art Archive, …) by
    every recommendation row, cover-search result and artist-image candidate.
    This route fetches one of those — cached for a month under
    `<music>/.mlo/data/art_cache`, and fallen back to a provider that answers
    when the CDN refuses us (`server/artcache.py`) — so a shelf renders the
    same whether or not the network can reach the provider directly.

    `artist`/`album`/`rg` (a release-group MBID) are the identity the fallback
    is asked about; without them only the original URL is tried. Only
    allowlisted provider hosts are accepted, and a total failure answers 404,
    which is what keeps the UI's placeholder.
    """
    data, ctype, source = await asyncio.to_thread(
        artcache.fetch_art, url, artist=artist.strip(), album=album.strip(),
        release_group_mbid=rg.strip())
    if not data:
        raise HTTPException(404, "no artwork")
    return Response(content=data, media_type=ctype or "image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400",
                             "X-Art-Source": source or "url"})


def _cover_url_bytes(url, artist="", album="", rg=""):
    """Cover bytes for a caller-supplied URL.

    A provider URL goes through the art cache — its per-host headers are what
    gets past a CDN that refuses the app, and its fallback is what answers at
    all when the CDN refuses everybody on this network. Any other public image
    URL is fetched directly, the way it always was.
    """
    if artcache.allowed(url):
        data, ctype, _source = artcache.fetch_art(
            url, artist=artist, album=album, release_group_mbid=rg)
        if not data:
            raise ValueError("that image could not be fetched")
        return data, ctype
    return intg.fetch_image_bytes(url)


@app.post("/api/cover")
async def upload_cover(album: str = Query(...), file: UploadFile = File(...),
                       track: Optional[str] = Query(None),
                       tracks: Optional[str] = Query(None)):
    """Upload cover art.

    No `track`/`tracks` replaces the album's cover.*; `track=<audio filename>`
    writes a per-track sidecar named after the track stem (e.g.
    '01 - Song.jpg'); `tracks=a.flac,b.flac` writes ONE image — the sidecar of
    the first selected track — and maps every selected track to it, so tracks
    can share art without the file being duplicated.
    """
    alb = os.path.normpath(album)
    if not os.path.isdir(alb):
        raise HTTPException(404, "album not found")
    if not _in_music_folder(alb, _music_folder()):
        raise HTTPException(400, "album outside music folder")
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".jxl", ".webp", ".bmp"):
        ext = ".jpg"
    stem, selected = _cover_write_target(alb, track, tracks)
    data = await file.read()
    if not _is_image_bytes(data):
        raise HTTPException(400, "uploaded file is not a readable image")
    res = _write_cover_bytes(alb, stem, ext, data)
    if selected:
        set_track_covers(alb, selected, os.path.basename(res["path"]))
    return res


def _cover_stem(track):
    """Sidecar stem for a track filename: '01 - Song.flac' -> '01 - Song'."""
    tstem = os.path.splitext(os.path.basename(track or ""))[0].strip()
    return re_safe_filename(tstem).strip().rstrip(".") or "cover"


def _resolve_selected_tracks(alb, tracks):
    """(resolved, unknown) album filenames for a comma-separated selection.

    Names are matched case-insensitively against the album folder so the
    caller can reject an unknown one instead of recording a map entry that
    names a file which is not there.
    """
    names = [os.path.basename(t.strip()) for t in str(tracks or "").split(",") if t.strip()]
    try:
        listing = {f.lower(): f for f in os.listdir(alb)}
    except OSError:
        listing = {}
    resolved, unknown = [], []
    for n in names:
        real = listing.get(n.lower())
        if real and os.path.isfile(os.path.join(alb, real)):
            resolved.append(real)
        else:
            unknown.append(n)
    return resolved, unknown


def _cover_write_target(alb, track, tracks):
    """(stem, selected tracks) for a cover write: a multi-track selection
    wins, then a single `track`, then the album cover."""
    if str(tracks or "").strip():
        selected, unknown = _resolve_selected_tracks(alb, tracks)
        if unknown:
            raise HTTPException(400, f"unknown track(s) in album: {', '.join(unknown)}")
        if not selected:
            raise HTTPException(400, "no tracks selected")
        return _cover_stem(selected[0]), selected
    if track:
        return _cover_stem(track), []
    return "cover", []


def _image_magic_ext(data):
    """Image extension from magic numbers, or None when the bytes carry no
    recognised image container signature."""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"\xff\x0a" or data[:12] == b"\x00\x00\x00\x0cJXL ":
        return ".jxl"
    if data[:2] == b"BM":
        return ".bmp"
    return None


def _is_image_bytes(data):
    """Whether upload bytes are an image.

    Pillow reading it counts, and so does a recognised image container magic:
    JXL/HEIC/AVIF need plugins Pillow may not carry, and such a file is still
    the cover the user meant to upload.
    """
    if not data:
        return False
    if _image_magic_ext(data):
        return True
    try:
        from PIL import Image
    except Exception:
        return True
    import io
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.size
        return True
    except Exception:
        return False


def _compress_cover_bytes(data: bytes, ext: str):
    """Downscale / crop / re-encode freshly added cover art.

    Returns (bytes, ext, info). A cover from the finder arrives at full CDN
    resolution (3000px+ and several MB) and an upload at whatever the user
    happened to have, while the library's own convention is
    `cover_target_size`, cropped square, JPEG at `cover_jpeg_quality`. Doing
    that the moment the image lands means a downloaded or uploaded cover is
    already library-conformant instead of sitting oversized until script 5
    happens to run.

    Best effort by design: anything PIL cannot read (or any encode failure)
    returns the original bytes untouched, so a stored cover is never lost or
    corrupted by this.
    """
    info = {"compressed": False, "original_bytes": len(data),
            "original_width": None, "original_height": None}
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    try:
        target = int(cfg.get("cover_target_size", 1200) or 0)
    except (TypeError, ValueError):
        target = 1200
    resize = bool(cfg.get("cover_resize_enabled", True))
    crop = bool(cfg.get("cover_crop_enabled", True))
    try:
        quality = max(70, min(100, int(cfg.get("cover_jpeg_quality", 90) or 90)))
    except (TypeError, ValueError):
        quality = 90
    # JXL is left alone: Pillow cannot write it without the plugin, and the
    # image script re-encodes it later.
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".bmp"):
        return data, ext, info
    try:
        import io as _io
        from PIL import Image

        with Image.open(_io.BytesIO(data)) as img:
            img.load()
            ow, oh = img.size
            info["original_width"], info["original_height"] = ow, oh
            has_alpha = (img.mode in ("RGBA", "LA")
                         or (img.mode == "P" and "transparency" in img.info))
            out = img
            # Crop BEFORE resizing: the crop decides which pixels survive, so
            # scaling first would throw away resolution the crop should keep.
            if crop and ow != oh:
                side = min(ow, oh)
                left, top = (ow - side) // 2, (oh - side) // 2
                out = out.crop((left, top, left + side, top + side))
            w, h = out.size
            if resize and target > 0 and max(w, h) > target:
                scale = target / max(w, h)
                out = out.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                                 Image.LANCZOS)
            nw, nh = out.size
            buf = _io.BytesIO()
            if has_alpha:
                out.convert("RGBA").save(buf, format="PNG", optimize=True)
                new_ext = ".png"
            else:
                out.convert("RGB").save(buf, format="JPEG", quality=quality,
                                        optimize=True, progressive=True)
                new_ext = ".jpg"
            new_data = buf.getvalue()
            info["width"], info["height"] = nw, nh
            # Keep the re-encode only when it helps: it must not grow a file
            # (a small already-optimised PNG can) and must not be a no-op.
            if (nw, nh) != (ow, oh) or len(new_data) < len(data):
                info["compressed"] = True
                info["bytes"] = len(new_data)
                return new_data, new_ext, info
            info.pop("width", None)
            info.pop("height", None)
    except Exception:
        info.pop("width", None)
        info.pop("height", None)
    return data, ext, info


def _write_cover_bytes(alb: str, stem: str, ext: str, data: bytes):
    """Atomically write cover bytes as <stem><ext> into the album folder,
    bust the cover cache, and report the written image's dimensions.

    The image is compressed first (see _compress_cover_bytes), so a cover that
    was just downloaded or uploaded is stored at the library's own size and
    encoding rather than at whatever the source served."""
    orig_ext = ext
    info = {}
    try:
        data, ext, info = _compress_cover_bytes(data, ext)
    except Exception:
        info = {}
    dest = os.path.join(alb, f"{stem}{ext}")
    fd, tmp = tempfile.mkstemp(prefix=".cover_tmp_", suffix=ext, dir=alb)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            try:
                f.flush()
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp, dest)
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise HTTPException(500, str(e))
    # A re-encode can change the extension (PNG -> JPG). Remove exactly the
    # file this write superseded, so the album does not keep a stale duplicate
    # of the same stem in the old format.
    if ext != orig_ext:
        _drop_stale_cover(alb, f"{stem}{orig_ext}")
    tagcache.invalidate_all()
    mbresolve.invalidate()
    out = {"ok": True, "path": dest.replace("\\", "/")}
    out.update(_cover_metrics(dest))
    for k, v in info.items():
        out.setdefault(k, v)
    return out


_COVER_EXTS = (".jpg", ".jpeg", ".png", ".jxl", ".webp", ".bmp")


def _drop_stale_cover(alb: str, stale: str):
    """Remove one cover file that a re-encode has just superseded.

    Deliberately narrow: it removes exactly `stale` and nothing else. Only the
    file whose extension THIS write changed is superseded — an album may
    legitimately hold cover.png next to cover.jxl, and sweeping "other cover
    formats" would delete art the user still has.
    """
    try:
        path = os.path.join(alb, stale)
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def _cover_metrics(path):
    """Dimensions (+ a below-target warning) for a just-written cover file.

    The same PIL read /api/cover/info does, but from the file: the write has
    already invalidated the byte cache this early in the request.
    """
    out = {"width": None, "height": None, "megapixels": None, "warning": None}
    try:
        from PIL import Image
        with Image.open(path) as img:
            w, h = img.size
    except Exception:
        return out
    if not (w and h):
        return out
    out["width"], out["height"] = w, h
    out["megapixels"] = round(w * h / 1_000_000, 2)
    try:
        target = int(load_config().get("cover_target_size", 1200) or 1200)
    except Exception:
        target = 1200
    out["target"] = target
    if 0 < target and min(w, h) < target:
        # The minimum IS the cover target the grader checks, so this is the
        # same number that will fail the album later — say that, rather than
        # only describing the image.
        out["below_target"] = True
        out["warning"] = (f"{w}×{h} is below the minimum {target}×{target} "
                          f"— grading will flag this cover")
    else:
        out["below_target"] = False
    return out


def _sniff_image_ext(data: bytes, content_type: str) -> str:
    """File-extension for image bytes, from magic numbers, then the
    Content-Type, defaulting to .jpg (the common cover-art case)."""
    magic = _image_magic_ext(data)
    if magic:
        return magic
    ct = (content_type or "").split("/")[1].strip().lower()
    if ct in ("jpeg", "jpg"):
        return ".jpg"
    if ct in ("png", "webp", "jxl", "bmp"):
        return f".{ct}"
    return ".jpg"


@app.get("/api/cover/search")
async def cover_search(artist: str = Query(""), album: str = Query(""),
                       limit: int = Query(40, ge=1, le=100),
                       sources: Optional[str] = Query(None),
                       country: Optional[str] = Query(None),
                       release_group_mbid: Optional[str] = Query(None)):
    """Search covers.musichoarders.xyz (aggregates Apple Music, Deezer,
    Qobuz, Tidal, Discogs, ...) for album covers matching artist/album, and
    fall back to the Cover Art Archive / Deezer / iTunes when it has nothing.

    `results` rows carry `width`/`height` — the image's real pixel size, probed
    from the file for the first results and `null` when unknown (never a
    guess). `provider` names who answered: "cov", a fallback id, or null when
    nobody had anything, so an empty result is never silent.

    `sources` (comma-separated ids) and `country` override the saved defaults
    for this one search — the finder's source picker and region dropdown.
    """
    if not artist.strip() and not album.strip():
        raise HTTPException(400, "artist or album is required")
    src = [s.strip() for s in (sources or "").split(",") if s.strip()] or None
    try:
        return await asyncio.to_thread(
            intg.cover_search, artist.strip(), album.strip(), limit,
            60.0, src, country, None, (release_group_mbid or "").strip())
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"cover search failed: {e}")


@app.get("/api/cover/sources")
def cover_sources():
    """Selectable cover sources + regions, and the saved defaults.

    `default_sources` / `default_country` are what Settings holds, so the
    finder opens on exactly the values a run without overrides would use."""
    cat = intg.cov_catalog()
    cfg = load_config()
    chosen, ctry = intg.resolve_cov_search(None, None, cfg)
    return {
        "sources": cat["sources"],
        "countries": cat["countries"],
        "active_source_limit": cat["active_source_limit"],
        "default_sources": chosen,
        "default_country": ctry,
        "saved_sources": [str(s) for s in (cfg.get("cover_sources") or [])],
        "saved_country": str(cfg.get("cover_country") or intg.COV_DEFAULT_COUNTRY),
    }


@app.post("/api/cover/fromurl")
async def cover_from_url(album: str = Query(...), url: str = Query(...),
                         track: Optional[str] = Query(None),
                         tracks: Optional[str] = Query(None),
                         artist: str = "", title: str = "", rg: str = ""):
    """Download a cover image from a URL (e.g. a COV search result) and
    store it like an uploaded cover (album cover.*, one per-track sidecar, or
    one image mapped to a whole `tracks=` selection).

    `artist`/`title`/`rg` are only used for provider URLs, where they are what
    the fallback is asked about when the CDN itself refuses us.
    """
    alb = os.path.normpath(album)
    if not os.path.isdir(alb):
        raise HTTPException(404, "album not found")
    if not _in_music_folder(alb, _music_folder()):
        raise HTTPException(400, "album outside music folder")
    stem, selected = _cover_write_target(alb, track, tracks)
    try:
        data, ctype = await asyncio.to_thread(
            _cover_url_bytes, url, artist.strip(), title.strip(), rg.strip())
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"cover download failed: {e}")
    if not data:
        raise HTTPException(502, "empty image response")
    res = _write_cover_bytes(alb, stem, _sniff_image_ext(data, ctype), data)
    if selected:
        set_track_covers(alb, selected, os.path.basename(res["path"]))
    return res


class CoverClearRequest(BaseModel):
    album: str
    tracks: Optional[List[str]] = None  # None/empty = every per-track entry


@app.post("/api/cover/clear")
def cover_clear(req: CoverClearRequest):
    """Drop per-track cover mappings for an album (no `tracks` = all of them).
    The image files stay on disk — clearing a mapping is not deleting art."""
    alb = os.path.normpath(mbresolve.resolve_album(req.album) or req.album)
    if not os.path.isdir(alb):
        raise HTTPException(404, "album not found")
    if not _in_music_folder(alb, _music_folder()):
        raise HTTPException(400, "album outside music folder")
    names = [os.path.basename(str(t)) for t in (req.tracks or []) if str(t).strip()]
    clear_track_covers(alb, names or None)
    tagcache.invalidate_all()
    return {"ok": True}


@app.get("/api/videos/scan")
def videos_scan(path: str = Query(None)):
    """List video files (non-audio containers) under an album or the whole
    library, with codec/duration info from ffprobe, so the UI can offer
    one-click remuxing."""
    from mlo.remux import VIDEO_EXTS, _stream_info
    from mlo.tools import detect_all_tools

    folder = _music_folder()
    root = os.path.normpath(path) if path else folder
    if not os.path.isdir(root):
        raise HTTPException(404, "folder not found")
    if not _in_music_folder(root, folder):
        raise HTTPException(400, "folder outside music folder")
    ffprobe = (detect_all_tools().get("ffmpeg") or {}).get("ffprobe_exe")
    skip = _skip_names()
    out = []
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d.lower() not in skip]
        for f in sorted(files):
            if os.path.splitext(f)[1].lower() not in VIDEO_EXTS:
                continue
            full = os.path.join(r, f)
            info = _stream_info(full, ffprobe) if ffprobe else None
            out.append({
                "path": full.replace("\\", "/"),
                "album": r.replace("\\", "/"),
                "file": f,
                "size": os.path.getsize(full),
                "video_codec": info[0] if info else None,
                "audio_codecs": info[1] if info else [],
                "duration": info[3] if info else None,
                # Legacy field name (UI compatibility): with the MKV remux
                # every stream copies, so any probeable file is remuxable.
                "mp4_safe": info is not None,
            })
    return {"videos": out}


@app.get("/api/videos/subtitles")
def videos_subtitles(path: str = Query(...)):
    """Subtitle sources for a video: streams muxed into the container plus
    external .srt/.vtt sidecars next to the file."""
    from server import subtitles as sub_mod

    p = os.path.normpath(path)
    if not os.path.isfile(p):
        raise HTTPException(404, "video not found")
    if not _in_music_folder(p, _music_folder()):
        raise HTTPException(400, "path outside music folder")
    try:
        return sub_mod.list_subtitles(p)
    except Exception as e:
        raise HTTPException(500, f"subtitle probe failed: {e}")


@app.get("/api/videos/subtitle")
def videos_subtitle(path: str = Query(...), n: int = Query(None), sidecar: str = Query(None)):
    """One subtitle as WebVTT (extracted / converted on demand)."""
    from server import subtitles as sub_mod

    p = os.path.normpath(path)
    if not os.path.isfile(p):
        raise HTTPException(404, "video not found")
    if not _in_music_folder(p, _music_folder()):
        raise HTTPException(400, "path outside music folder")
    try:
        data = sub_mod.vtt_for(p, muxed_n=n, sidecar=sidecar)
        return Response(content=data, media_type="text/vtt; charset=utf-8")
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"subtitle extraction failed: {e}")


class VideoTagRequest(BaseModel):
    """Tag a music video. Video containers are rewritten with a stream
    copy (bit-exact video/audio/captions); non-MKV sources come back as
    same-stem MKVs, so the response carries the final path."""
    path: str
    tags: dict


@app.post("/api/videos/tag")
def videos_tag(req: VideoTagRequest):
    """Write tags into a music video (TITLE/ARTIST/ALBUM/DISCNUMBER/...).

    Works for every library video container: MKV is rewritten in place,
    every other container (MP4/M4V included) is remuxed losslessly by
    ffmpeg into a same-stem Matroska with the new metadata — never
    re-encoded, captions and every audio stream preserved — so the
    response carries `container_changed`/`output_path`. This is the
    endpoint behind the downloads review flow ("tag this VOB as a music
    video")."""
    from mlo.audio import AudioFile
    from mlo.paths import LIB_VIDEO_EXTS

    p = os.path.normpath(req.path)
    if not os.path.isfile(p):
        raise HTTPException(404, "file not found")
    if not (os.path.splitext(p)[1].lower() in LIB_VIDEO_EXTS):
        raise HTTPException(400, "not a video file — use the tag editor for audio")
    if not _in_music_folder(p, _music_folder()):
        raise HTTPException(400, "file outside music folder")
    af = AudioFile(p)
    if af.audio is None:
        raise HTTPException(500, af.error or "unreadable video")
    ok = af.set_video_tags(req.tags or {})
    if not ok:
        raise HTTPException(500, af.error or "tag write failed")
    final = (af.tag_output_path or p).replace("\\", "/")
    tagcache.invalidate_path(os.path.normpath(final))
    tagcache.invalidate_path(p)
    mbresolve.invalidate()
    tech = getattr(af, "tech", {}) or {}
    # A tag write can re-emit the file in another container (a raw VOB/AVI/…
    # becomes a same-stem MKV) — the UI tells the user, so say which file now
    # holds the data and whether the container changed.
    return {"ok": True, "path": final, "renamed": os.path.normcase(final) != os.path.normcase(p),
            "container_changed": bool(af.container_changed),
            "output_path": final, "tech": tech}


class YoutubeDownloadRequest(BaseModel):
    """Grab a music video from YouTube for an album.

    `path` is the album folder to drop it into (or a track path whose folder
    is used); without it the file lands in the app's downloads dir for
    review. `duration` is the expected length in seconds — the candidate
    search uses it to reject live/tribute/cover uploads."""
    path: Optional[str] = None
    artist: str
    title: str
    duration: Optional[int] = None


@app.post("/api/videos/download-youtube")
def videos_download_youtube(req: YoutubeDownloadRequest):
    """Search YouTube for this artist+title and download the best match.

    The candidate is picked by server/youtube.py (duration window, lyric /
    cover / tribute filtering); with youtube_enabled off, yt-dlp missing or
    no acceptable candidate, the answer is {ok: false, candidate: null} with
    a reason rather than a half-download. Returns {ok, file, candidate}."""
    from server import youtube

    cfg = load_config()
    artist = str(req.artist or "").strip()
    title = str(req.title or "").strip()
    if not artist or not title:
        raise HTTPException(400, "artist and title are required")
    if not youtube.ytdlp_available(cfg):
        return {"ok": False, "candidate": None,
                "error": "yt-dlp is not available — install it under Dependencies"}

    dest = ""
    if req.path:
        p = os.path.normpath(req.path)
        dest = p if os.path.isdir(p) else os.path.dirname(p)
        if not os.path.isdir(dest):
            raise HTTPException(404, "destination folder not found")
        if not _in_music_folder(dest, _music_folder(cfg)):
            raise HTTPException(400, "destination outside music folder")
    else:
        from mlo.paths import downloads_dir
        dest = downloads_dir(str(cfg.get("music_folder") or "") or None) or ""
        if not dest:
            raise HTTPException(400, "music folder is not configured")
        os.makedirs(dest, exist_ok=True)

    candidate = youtube.best_candidate(artist, title, want_seconds=req.duration,
                                       config=cfg)
    if not candidate:
        return {"ok": False, "candidate": None,
                "error": "no acceptable YouTube match found"}
    try:
        got = youtube.download(candidate["url"], dest, cfg)
    except Exception as e:
        raise HTTPException(502, f"YouTube download failed: {e}")
    tagcache.invalidate_all()
    return {"ok": True, "file": str(got.get("path") or "").replace("\\", "/"),
            "candidate": candidate, "container": got.get("container"),
            "height": got.get("height"), "abr": got.get("abr")}


class VideoMatchAssignment(BaseModel):
    """One video -> track assignment (the MusicBrainz matching step)."""
    path: str
    title: str
    tracknumber: Optional[int] = None
    discnumber: Optional[int] = None


class VideoMatchRequest(BaseModel):
    """Assign MusicBrainz track identities to an album's music videos."""
    album_path: str
    assignments: List[VideoMatchAssignment]


@app.post("/api/videos/match")
def videos_match(req: VideoMatchRequest):
    """Write TITLE/DISCNUMBER/TRACKNUMBER onto an album's music videos.

    The reverse of /api/mb/match: the user (or the import flow) pairs each
    video file with a release track, and this writes those identities so the
    video sorts and grades with the album. Only tags that would actually
    change are written — every video write is a full lossless remux, so a
    no-op assignment must not rewrite the file. Returns {updated}; a video
    re-emitted in another container is named in `output_paths`."""
    from mlo.audio import AudioFile
    from mlo.paths import LIB_VIDEO_EXTS

    album = os.path.normpath(req.album_path)
    if not os.path.isdir(album):
        raise HTTPException(404, "album not found")
    if not _in_music_folder(album, _music_folder()):
        raise HTTPException(400, "album outside music folder")
    if not req.assignments:
        raise HTTPException(400, "assignments are required")

    folder = _music_folder()
    updated = 0
    errors = []
    # Video writes are full lossless rewrites, so a raw container (VOB/AVI/…)
    # comes back as a same-stem MKV: report the files that were re-emitted.
    swapped = []
    for a in req.assignments:
        p = os.path.normpath(a.path)
        if not os.path.isfile(p):
            errors.append(f"{a.path}: not found")
            continue
        if os.path.splitext(p)[1].lower() not in LIB_VIDEO_EXTS:
            errors.append(f"{os.path.basename(p)}: not a video file")
            continue
        if not _in_music_folder(p, folder):
            errors.append(f"{os.path.basename(p)}: outside music folder")
            continue
        af = AudioFile(p)
        if af.audio is None:
            errors.append(f"{os.path.basename(p)}: {af.error or 'unreadable'}")
            continue
        want = {"TITLE": str(a.title or "").strip()}
        if a.tracknumber is not None:
            want["TRACKNUMBER"] = str(a.tracknumber)
        if a.discnumber is not None:
            want["DISCNUMBER"] = str(a.discnumber)
        tags = {k: v for k, v in want.items()
                if v and str(af.get_tag(k) or "").strip() != v}
        if not tags:
            continue
        if not af.set_video_tags(tags):
            errors.append(f"{os.path.basename(p)}: {af.error or 'tag write failed'}")
            continue
        updated += 1
        final = (af.tag_output_path or p).replace("\\", "/")
        if af.container_changed:
            swapped.append(final)
        tagcache.invalidate_path(os.path.normpath(final))
        tagcache.invalidate_path(p)
    if updated:
        tagcache.invalidate_all()
        mbresolve.invalidate()
    if errors and not updated:
        raise HTTPException(500, "; ".join(errors))
    return {"updated": updated, "errors": errors,
            "container_changed": bool(swapped), "output_paths": swapped}


@app.get("/api/cover/info")
def cover_info(album: str = Query(...), file: str = Query(None)):
    """Image details for the album cover (resolution, aspect ratio,
    format, byte size) — powers the album page's Cover info dialog."""
    import io

    alb = os.path.normpath(mbresolve.resolve_album(album) or album)
    if not os.path.isdir(alb):
        raise HTTPException(404, "album not found")
    if not _in_music_folder(alb, _music_folder()):
        raise HTTPException(400, "album outside music folder")
    data, ctype, _etag = tagcache.cover_bytes(alb, file)
    if data is None:
        raise HTTPException(404, "no cover")
    # Resolve the on-disk path for the file name (cache is keyed by stat).
    p = None
    if file:
        cand = os.path.normpath(os.path.join(alb, os.path.basename(file)))
        if os.path.isfile(cand):
            p = cand
    if p is None:
        for cand in ("cover.jpg", "cover.jpeg", "cover.png", "cover.jxl", "cover.webp", "cover.bmp"):
            full = os.path.join(alb, cand)
            if os.path.isfile(full):
                p = full
                break
    info = {"file": os.path.basename(p) if p else None,
            "format": (ctype or "image").split("/")[-1].upper(),
            "bytes": len(data),
            "megapixels": None, "aspect": None, "aspect_label": None,
            "width": None, "height": None}
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        w, h = img.size
        info["width"], info["height"] = w, h
        info["format"] = img.format or info["format"]
        if w and h:
            info["megapixels"] = round(w * h / 1_000_000, 2)
            from math import gcd
            g = gcd(w, h) or 1
            rw, rh = w // g, h // g
            if rw > 40 or rh > 40:
                # huge coprime pairs read badly — show a decimal ratio too
                info["aspect"] = f"{rw}:{rh}"
                info["aspect_label"] = f"{w / h:.2f}:1"
            else:
                info["aspect"] = f"{rw}:{rh}"
                info["aspect_label"] = f"{rw}:{rh}"
    except Exception:
        pass
    return info


@app.post("/api/lyrics/embed")
def lyrics_embed(req: LyricsEmbedRequest):
    """Write ONLY the embedded LYRICS tag (the last tag-write path the UI
    still needs; everything else is grading-script territory)."""
    from mlo.audio import AudioFile
    p = os.path.normpath(mbresolve.resolve_track(req.path) or req.path)
    if not os.path.isfile(p):
        raise HTTPException(404, "file not found")
    if not _in_music_folder(p, _music_folder()):
        raise HTTPException(400, "file outside music folder")
    af = AudioFile(p)
    if af.audio is None:
        raise HTTPException(500, af.error or "unreadable")
    if not af.set_lyrics(req.lyrics or ""):
        raise HTTPException(500, af.error or "lyrics write failed")
    tagcache.invalidate_path(p)
    return {"ok": True}


class LikeToggleRequest(BaseModel):
    path: str
    mbid: Optional[str] = None  # MusicBrainz recording ID — keeps the like alive across moves


@app.get("/api/likes")
def likes_list():
    """Paths of all liked (hearted) tracks, newest first. Stored paths are
    normalized to forward slashes and MBID-backed rows self-heal after
    reorganization, so they always match the library payload."""
    return {"paths": [p.replace("\\", "/") for p in pl_mod.list_likes()]}


@app.post("/api/likes/toggle")
def likes_toggle(req: LikeToggleRequest):
    p = os.path.normpath(mbresolve.resolve_track(req.path) or req.path)
    if not os.path.isfile(p):
        raise HTTPException(404, "file not found")
    # A like row for a path outside the library can never match a track again.
    if not _in_music_folder(p, _music_folder()):
        raise HTTPException(400, "file outside music folder")
    p = p.replace("\\", "/")
    try:
        liked = pl_mod.toggle_like(p, mbid=req.mbid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "liked": liked}


class FavoriteToggleRequest(BaseModel):
    kind: str  # "album" | "artist" | "playlist"
    key: str
    mbid: Optional[str] = None  # release/artist MBID — keeps the favorite stable across moves


@app.get("/api/favorites")
def favorites_list():
    """Favorite albums / artists / playlists, keyed by path (or playlist id),
    newest first — powers the sidebar Favorites section. MBID-backed rows
    self-heal when files move."""
    return pl_mod.list_favorites()


@app.post("/api/favorites/toggle")
def favorites_toggle(req: FavoriteToggleRequest):
    # album/artist keys are library folders; "playlist" keys are playlist ids,
    # not paths, so only the path-valued kinds get the containment guard.
    if str(req.kind or "").strip().lower() in ("album", "artist"):
        if not _in_music_folder(req.key, _music_folder()):
            raise HTTPException(400, "folder outside music folder")
    try:
        fav = pl_mod.toggle_favorite(req.kind, req.key, mbid=req.mbid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "fav": fav}


class LyricsWordsyncRequest(BaseModel):
    path: str = ""
    text: str = ""


@app.post("/api/lyrics/wordsync")
def lyrics_wordsync(req: LyricsWordsyncRequest):
    """Deterministic line→word ELRC for one track.

    Reads the stored lyrics — the LYRICS tag first, then the .lrc sidecar —
    unless the caller already has the text, then spreads each line's timings
    across its words at `lrc_sync_level`. Pure text arithmetic: the same
    builder script 1 uses, no model involved.
    """
    from mlo.lyrics import elrc_word_sync
    text = str(req.text or "").strip()
    if not text:
        if not str(req.path or "").strip():
            raise HTTPException(400, "no path or lyrics text provided")
        from mlo.audio import AudioFile
        p = os.path.normpath(mbresolve.resolve_track(req.path) or req.path)
        if not os.path.isfile(p):
            raise HTTPException(404, "file not found")
        if not _in_music_folder(p, _music_folder()):
            raise HTTPException(400, "file outside music folder")
        af = AudioFile(p)
        if af.audio is None:
            raise HTTPException(500, af.error or "unreadable")
        text = (af.get_lyrics() or "").strip()
        if not text:
            lrc_path = os.path.splitext(p)[0] + ".lrc"
            if os.path.isfile(lrc_path):
                try:
                    with open(lrc_path, "r", encoding="utf-8", errors="replace") as fh:
                        text = fh.read().strip()
                except Exception:
                    text = ""
    if not text:
        raise HTTPException(400, "track has no lyrics to sync")
    level = str(load_config().get("lrc_sync_level") or "LINE").lower()
    return {"lrc": elrc_word_sync(text, level=level)}


class BulkTagsRequest(BaseModel):
    paths: List[str]
    remove: List[str] = []
    set: dict = {}


@app.post("/api/tags/bulk")
def tags_bulk(req: BulkTagsRequest):
    """Bulk tag surgery across the given tracks: delete the named tags and
   /or set tags to values (an empty value deletes that tag instead). Built
    for the library UI's batch tools; per-tag auditable counts come back."""
    from mlo.audio import AudioFile
    paths = [p for p in (req.paths or []) if str(p).strip()]
    sets = {str(k).strip().upper(): v for k, v in (req.set or {}).items() if str(k).strip()}
    removes = [str(t).strip().upper() for t in (req.remove or []) if str(t).strip()]
    if not paths:
        raise HTTPException(400, "no tracks given")
    if not removes and not sets:
        raise HTTPException(400, "nothing to do — pick tags to remove or set")
    removed = added = failed = 0
    errors: list = []
    for rp in paths:
        try:
            p = os.path.normpath(mbresolve.resolve_track(rp) or rp)
            if not os.path.isfile(p) or not _in_music_folder(p, _music_folder()):
                failed += 1
                errors.append(f"{os.path.basename(rp)}: not found in library")
                continue
            af = AudioFile(p)
            if af.audio is None:
                failed += 1
                errors.append(f"{os.path.basename(rp)}: {af.error or 'unreadable'}")
                continue
            # existence via all_tags(): get_tag only knows the standard
            # TAG_MAP names and would miss custom/unknown keys entirely
            present = {str(k).upper() for k in (af.all_tags() or {})}
            af.defer_save(True)
            for name in removes:
                if name in present and af.delete_tag(name):
                    removed += 1
            for name, val in sets.items():
                v = str(val).strip()
                if not v:
                    if name in present and af.delete_tag(name):
                        removed += 1
                elif af.set_tag(name, v):
                    added += 1
            af.defer_save(False)
            tagcache.invalidate_path(p)
        except Exception as e:
            failed += 1
            errors.append(f"{os.path.basename(rp)}: {e}")
    return {"ok": failed == 0, "removed": removed, "added": added,
            "failed": failed, "errors": errors[:20]}


# --------------------------------------------------------------------------- #
# Run scripts
# --------------------------------------------------------------------------- #
# Scripts mutate the library in place, so two overlapping runs (double-clicked
# Run, or an import-triggered chain landing on a UI run) would fight over the
# same files. The lock itself lives in server.script_runners, because the
# import pipeline and the bulk queue run the same scripts; a second caller
# gets 409 instead of queueing.
@app.post("/api/run")
def run_scripts(req: RunRequest):
    from server import script_runners
    try:
        return _run_scripts(req)
    except script_runners.RunBusy as e:
        raise HTTPException(409, str(e))


def _run_scripts(req: RunRequest):
    """Run the requested scripts (ids in request order) against the library.

    The id → runner registry lives in `server.script_runners` so the import
    pipeline, the bulk queue and the Soulseek auto-importer run the exact same
    scripts; this handler is the HTTP shell around it (target scoping, force
    semantics, cache invalidation, progress).
    """
    from server import script_runners

    cfg = load_config()
    if req.targets:
        cfg["targets"] = [os.path.normpath(t) for t in req.targets]
    # Per-script options default from saved config; the request can override.
    # A *supplied* force dict is authoritative and complete (see
    # script_runners._apply_force): the UI's one-shot Force switch sends every
    # checked key, so an unchecked key must turn the force off rather than
    # fall back to a saved-on config value.
    f = req.force or {}
    if req.force is not None:
        # image-option overrides (subset of run_process_images knobs)
        for key in ("rename_to_cover", "reencode_to_jxl", "images_convert_to_jpeg",
                    "images_convert_lossless_to_png", "convert_jxl_back", "remove_alpha",
                    "jpeg_progressive", "cover_resize_enabled", "cover_crop_enabled",
                    "cover_target_size", "cover_jpeg_quality", "jpegxl_effort",
                    "jpegxl_distance", "png_optimization_level"):
            if key in f:
                cfg[key] = f[key]

    for i in req.ids:
        if script_runners.RUNNERS.get(i, (None, None))[1] is None:
            raise HTTPException(400, f"runner {i} not available")

    results = script_runners.run_chain(
        cfg, list(req.ids), targets=None, force=req.force if req.force is not None else None,
    )
    tagcache.invalidate_all()
    mbresolve.invalidate()
    return {"results": results}


# --------------------------------------------------------------------------- #
# Export to device (MP3 player / DAP / USB drive)
# --------------------------------------------------------------------------- #
class ExportRequest(BaseModel):
    paths: List[str] = []              # absolute audio file paths to export
    dest: str                          # destination drive root (e.g. "E:\\")
    subfolder: str = "Music"           # created under the drive root
    codec: str = "copy"                # copy | flac | mp3 | aac | opus | vorbis
    quality: str = ""                  # codec-specific (V0/320/256/q8/…)
    structure: str = "artist_album"    # artist_album | flat | mirror


@app.get("/api/export/drives")
def export_drives():
    """Candidate destination drives with free space and bus type."""
    try:
        return {"drives": exporter.list_drives()}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/export/codecs")
def export_codecs():
    """Available export codecs + their quality choices (drives the UI)."""
    return {"codecs": {k: v.get("label", k) for k, v in exporter.CODECS.items()}}


@app.post("/api/export")
def export_run(req: ExportRequest):
    """Copy/transcode the selected tracks onto the target drive. Runs in the
    worker thread pool (sync def) and reports progress via the shared hook,
    so the header progress bar behaves exactly like a library script run."""
    if not req.paths:
        raise HTTPException(400, "no tracks selected")
    dest_root = os.path.abspath(req.dest)
    if not os.path.isdir(dest_root):
        raise HTTPException(400, f"destination not found: {req.dest}")
    for p in req.paths:
        if not _in_music_folder(p, _music_folder()):
            raise HTTPException(400, f"file outside music folder: {p}")
    if req.codec not in exporter.CODECS:
        raise HTTPException(400, f"unknown codec: {req.codec}")
    cfg = load_config()
    res = exporter.export_tracks(
        cfg, [os.path.normpath(p) for p in req.paths], dest_root,
        subfolder=req.subfolder, codec=req.codec, quality=req.quality,
        structure=req.structure,
    )
    return {"ok": res["failed"] == 0, **res}


# --------------------------------------------------------------------------- #
# Playlists
# --------------------------------------------------------------------------- #
@app.get("/api/playlists")
def playlists_list():
    return pl_mod.list_playlists()


@app.post("/api/playlists")
def playlists_create(req: PlaylistCreate):
    if not req.name.strip():
        raise HTTPException(400, "name required")
    pid = pl_mod.create_playlist(req.name.strip(), req.kind, req.filter)
    return pl_mod.get_playlist(pid)


@app.get("/api/playlists/{pid}")
def playlists_get(pid: int):
    pl = pl_mod.get_playlist(pid)
    if pl is None:
        raise HTTPException(404, "playlist not found")
    return pl


@app.patch("/api/playlists/{pid}")
def playlists_rename(pid: int, req: PlaylistUpdate):
    # only the fields the client actually sent (icon: null CLEARS the icon)
    fields = {k: getattr(req, k) for k in req.model_fields_set}
    if not pl_mod.update_playlist(pid, fields):
        raise HTTPException(404, "playlist not found")
    return pl_mod.get_playlist(pid)


@app.delete("/api/playlists/{pid}")
def playlists_delete(pid: int):
    if not pl_mod.delete_playlist(pid):
        raise HTTPException(404, "playlist not found")
    return {"ok": True}


@app.post("/api/playlists/{pid}/tracks")
def playlists_add(pid: int, req: PlaylistTracks):
    if pl_mod.get_playlist(pid) is None:
        raise HTTPException(404, "playlist not found")
    paths = [os.path.normpath(p) for p in req.paths]
    for p in paths:
        if not _in_music_folder(p, _music_folder()):
            raise HTTPException(400, f"file outside music folder: {p}")
    n = pl_mod.add_tracks(pid, paths, req.position)
    return {"added": n}


@app.put("/api/playlists/{pid}/tracks")
def playlists_order(pid: int, req: PlaylistTracks):
    """Full reorder: body paths replace the playlist order entirely."""
    if pl_mod.get_playlist(pid) is None:
        raise HTTPException(404, "playlist not found")
    paths = [os.path.normpath(p) for p in req.paths]
    for p in paths:
        if not _in_music_folder(p, _music_folder()):
            raise HTTPException(400, f"file outside music folder: {p}")
    pl_mod.set_order(pid, paths)
    return {"ok": True}


@app.delete("/api/playlists/{pid}/tracks")
def playlists_remove(pid: int, req: PlaylistTracks):
    if pl_mod.get_playlist(pid) is None:
        raise HTTPException(404, "playlist not found")
    pl_mod.remove_tracks(pid, [os.path.normpath(p) for p in req.paths])
    return {"ok": True}


@app.post("/api/playlists/{pid}/filter")
def playlists_filter(pid: int, req: SmartFilter):
    pl = pl_mod.get_playlist(pid)
    if pl is None:
        raise HTTPException(404, "playlist not found")
    if pl["kind"] != "smart":
        raise HTTPException(400, "not a smart playlist")
    pl_mod.set_smart_filter(pid, req.filter)
    return pl_mod.get_playlist(pid)


@app.post("/api/playlists/{pid}/evaluate")
def playlists_evaluate(pid: int):
    pl = pl_mod.get_playlist(pid)
    if pl is None:
        raise HTTPException(404, "playlist not found")
    if pl["kind"] != "smart":
        raise HTTPException(400, "not a smart playlist")
    library = lib_mod.build_library(load_config())
    hits = pl_mod.evaluate_smart(pid, library)
    return {"paths": hits or []}


@app.get("/api/playlists/{pid}/export")
def playlists_export(pid: int):
    content = pl_mod.export_m3u8(pid)
    if content is None:
        raise HTTPException(404, "playlist not found")
    pl = pl_mod.get_playlist(pid)
    name = re_safe_filename(pl["name"]) or "playlist"
    return PlainTextResponse(
        content,
        media_type="audio/x-mpegurl",
        headers={"Content-Disposition": f'attachment; filename="{name}.m3u8"'},
    )


@app.post("/api/playlists/import")
async def playlists_import(name: str = Query(...), file: UploadFile = File(...)):
    content = (await file.read()).decode("utf-8", errors="replace")
    base = load_config().get("music_folder") or os.getcwd()
    pid = pl_mod.import_m3u8(name.strip() or file.filename or "imported", content, base)
    return pl_mod.get_playlist(pid)


def re_safe_filename(name):
    import re
    return re.sub(r'[\\/*?:"<>|]', "_", name)


# --------------------------------------------------------------------------- #
# MusicBrainz / LRCLIB / RYM
# --------------------------------------------------------------------------- #
@app.get("/api/mb/release")
def mb_release_query(mbid: str = Query(...)):
    """Release lookup by ID or full URL (query param — URLs contain slashes
    and cannot travel inside the path segment)."""
    rid = intg._mbid(mbid)
    if not rid:
        raise HTTPException(400, "invalid MusicBrainz ID or URL")
    try:
        return intg.release_lookup(rid)
    except Exception as e:
        raise HTTPException(502, f"MusicBrainz lookup failed: {e}")


@app.get("/api/mb/release-genres")
def mb_release_genres_query(mbid: str = Query(...), limit: Optional[int] = Query(None)):
    rid = intg._mbid(mbid)
    if not rid:
        raise HTTPException(400, "invalid MusicBrainz ID or URL")
    try:
        release = intg.release_lookup(rid)
        return intg.genre_cascade(release, limit=max(0, int(limit)) if limit else None)
    except Exception as e:
        raise HTTPException(502, f"MusicBrainz genre lookup failed: {e}")


# ---- generic MusicBrainz browser (search + entity pages) -------------------
@app.get("/api/mb/search")
def mb_search(q: str = Query(..., min_length=1), type: str = Query("release"),
              limit: int = Query(100), offset: int = Query(0),
              mode: str = Query("free"), primary_type: str = Query(""),
              secondary_type: str = Query("")):
    """Search MusicBrainz for the in-app browser: type = artist |
    release-group | release | recording; mode = free | catno | barcode
    (catno/barcode only apply to releases); primary_type/secondary_type narrow
    releases and release groups to MusicBrainz release types (Album, EP,
    Single, Soundtrack, Live, ...). Returns {rows, total} — searches page 100
    rows at a time via offset."""
    if type not in intg.MB_ENTITIES:
        raise HTTPException(400, "type must be one of " + ", ".join(intg.MB_ENTITIES))
    if mode not in ("free", "catno", "barcode"):
        raise HTTPException(400, "mode must be free, catno or barcode")
    try:
        return intg.search_mb(type, q, limit, mode, max(0, offset),
                              primary_type=primary_type.strip(),
                              secondary_type=secondary_type.strip())
    except Exception as e:
        raise HTTPException(502, f"MusicBrainz search failed: {e}")


@app.get("/api/mb/artist/{mbid}")
def mb_artist(mbid: str, limit: int = Query(300), offset: int = Query(0)):
    rid = intg._mbid(mbid)
    if not rid:
        raise HTTPException(400, "invalid MusicBrainz ID or URL")
    try:
        return intg.artist_browse(rid, max(1, limit), max(0, offset))
    except Exception as e:
        raise HTTPException(502, f"MusicBrainz lookup failed: {e}")


@app.get("/api/mb/release-group/{mbid}")
def mb_release_group(mbid: str, limit: int = Query(300), offset: int = Query(0)):
    rid = intg._mbid(mbid)
    if not rid:
        raise HTTPException(400, "invalid MusicBrainz ID or URL")
    try:
        return intg.release_group_browse(rid, max(1, limit), max(0, offset))
    except Exception as e:
        raise HTTPException(502, f"MusicBrainz lookup failed: {e}")


@app.get("/api/mb/recording/{mbid}")
def mb_recording(mbid: str, limit: int = Query(300), offset: int = Query(0)):
    rid = intg._mbid(mbid)
    if not rid:
        raise HTTPException(400, "invalid MusicBrainz ID or URL")
    try:
        return intg.recording_browse(rid, max(1, limit), max(0, offset))
    except Exception as e:
        raise HTTPException(502, f"MusicBrainz lookup failed: {e}")


@app.get("/api/mb/detect/{mbid}")
def mb_detect(mbid: str):
    """Identify which MusicBrainz entity kind a bare MBID belongs to, so the
    browser can route pasted IDs without the user choosing a type."""
    rid = intg._mbid(mbid)
    if not rid:
        raise HTTPException(400, "invalid MusicBrainz ID or URL")
    try:
        return intg.detect_mbid(rid)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(502, f"MusicBrainz lookup failed: {e}")


@app.get("/api/mb/search/releases")
def mb_search_releases(q: str = Query(..., min_length=1), limit: int = 10,
                        mode: str = Query("release")):
    """Search MusicBrainz releases: mode = release | track | catno | barcode."""
    if mode not in ("release", "track", "catno", "barcode"):
        raise HTTPException(400, "mode must be release, track, catno or barcode")
    try:
        return intg.search_releases(q, limit, mode)
    except Exception as e:
        raise HTTPException(502, f"MusicBrainz search failed: {e}")


@app.get("/api/mb/search/artists")
def mb_search_artists(q: str = Query(..., min_length=1), limit: int = 5):
    try:
        return intg.search_artists(q, limit)
    except Exception as e:
        raise HTTPException(502, f"MusicBrainz search failed: {e}")


def _scan_album_tracks(album_dir):
    """Recursively list audio files in an album folder with tags + tech info."""
    tracks = []
    skip = _skip_names()
    for root, dirs, files in os.walk(album_dir):
        dirs[:] = [d for d in dirs if d.lower() not in skip]
        for f in sorted(files):
            if not is_audio_file(f):
                continue
            path = os.path.join(root, f)
            tags, tech = tagcache.read_track(path, None)
            duration = float(tech.get("length") or 0) or None
            tn = lib_mod._parse_num(tags.get("TRACKNUMBER"))
            dn = lib_mod._parse_num(tags.get("DISCNUMBER"))
            tracks.append({
                "path": path.replace("\\", "/"),
                "file": os.path.relpath(path, album_dir).replace("\\", "/"),
                "tracknumber": tn,
                "discnumber": dn,
                "title": tags.get("TITLE"),
                "artist": tags.get("ARTIST"),
                "duration": duration,
                "tags": tags,
                "tech": tech,
            })
    return tracks


@app.post("/api/mb/match")
def mb_match(req: MatchRequest):
    """Suggest release track/disc matches for local files in an album."""
    album_dir = os.path.normpath(req.album_path)
    if not os.path.isdir(album_dir):
        raise HTTPException(404, "album not found")
    if not _in_music_folder(album_dir, _music_folder()):
        raise HTTPException(400, "album outside music folder")
    rid = intg._mbid(req.release_id)
    if not rid:
        raise HTTPException(400, "invalid release ID")
    try:
        release = intg.release_lookup(rid)
    except Exception as e:
        raise HTTPException(502, f"MusicBrainz lookup failed: {e}")

    from mlo.audio import AudioFile
    local_tracks = _scan_album_tracks(album_dir)
    for lt in local_tracks:
        lt.pop("tags", None)
        lt.pop("tech", None)
    return {"release": release, "suggestions": intg.match_tracks(local_tracks, release)}


@app.post("/api/mb/assign")
def mb_assign(req: AssignTagsRequest):
    """Write per-track MB/RYM link tags. tracks: {path: {TAG: value}}.

    Videos are re-emitted losslessly, so a raw container comes back as a
    same-stem MKV: `container_changed`/`output_paths` name those files."""
    from mlo.audio import AudioFile
    errors = []
    changed = 0
    folder = _music_folder()
    # Video writes are full lossless rewrites, so a raw container (VOB/AVI/…)
    # comes back as a same-stem MKV: report the files that were re-emitted.
    swapped = []
    for p, tag_map in req.tracks.items():
        fp = os.path.normpath(p)
        if not os.path.isfile(fp):
            errors.append(f"{p}: not found")
            continue
        if not _in_music_folder(fp, folder):
            errors.append(f"{p}: outside music folder")
            continue
        af = AudioFile(fp)
        if af.audio is None:
            errors.append(f"{p}: {af.error or 'unreadable'}")
            continue
        # Advisory values: only 0/1/2 exist (0 clean, 1 clean/explicit-clean,
        # 2 explicit) and a blank means "delete the tag" — an invalid value
        # written here would only surface as a grading failure later.
        bad = [k for k, v in tag_map.items()
               if k.upper() in ("ITUNESADVISORY", "ALBUMITUNESADVISORY")
               and str(v or "").strip() not in ("", "0", "1", "2")]
        if bad:
            errors.append(f"{p}: {', '.join(sorted(bad))} must be 0, 1, 2 or empty")
            continue
        if getattr(af, "is_video", False):
            # Video containers: batch all tags into ONE lossless ffmpeg
            # rewrite (a per-tag rewrite remuxes the whole file each time).
            clean = {k: v for k, v in tag_map.items() if str(v or "").strip()}
            deletes = [k for k, v in tag_map.items() if not str(v or "").strip()]
            if deletes and not clean:
                errors.append(f"{p}: video containers cannot delete tags — overwrite instead")
                continue
            if clean and not af.set_video_tags(clean):
                errors.append(f"{p}: {af.error or 'tag write failed'}")
                continue
            if af.container_changed:
                swapped.append((af.tag_output_path or fp).replace("\\", "/"))
            changed += 1
            tagcache.invalidate_path(fp)
            continue
        af.defer_save(True)
        for k, v in tag_map.items():
            try:
                if v is None or str(v) == "":
                    if not af.delete_tag(k):
                        errors.append(f"{p} {k}: {af.error or 'delete failed'}")
                elif not af.set_tag(k, str(v)):
                    errors.append(f"{p} {k}: {af.error or 'write failed'}")
            except Exception as e:
                errors.append(f"{p} {k}: {e}")
        af.defer_save(False)
        changed += 1
        tagcache.invalidate_path(fp)
    if errors:
        raise HTTPException(500, "; ".join(errors))
    return {"ok": True, "changed": changed,
            "container_changed": bool(swapped), "output_paths": swapped}


def _lyrics_record(hit, artist="", track="", album=None, duration=None):
    """A provider-chain hit in the record shape the lyrics UI already consumes.

    The album/library batch download, the lyrics manager's search list and the
    viewer all read `id` / `plainLyrics` / `syncedLyrics` / `artist` / `track`
    (the LRCLIB record shape). Keeping it means those callers get the whole
    fallback chain — LRCLIB → NetEase → Kugou → QQ Music → Kuwo → YouTube
    captions, see `mlo.lyrics_providers.SOURCES` — without changing, and
    `provider` / `provider_label` ride along for anything that wants to show
    where the lyrics came from.
    """
    if not hit:
        return None
    synced = (hit.get("synced") or "").strip()
    plain = (hit.get("plain") or "").strip()
    if not synced and not plain:
        return None
    import zlib
    key = f"{hit.get('provider')}|{hit.get('matched_artist')}|{hit.get('matched_title')}"
    return {
        # Stable positive id: the manager modal dedupes candidates by it.
        "id": zlib.crc32(key.encode("utf-8")) & 0x7FFFFFFF,
        "trackName": hit.get("matched_title") or track,
        "artistName": hit.get("matched_artist") or artist,
        "albumName": hit.get("matched_album") or album,
        "duration": hit.get("duration") or duration,
        "instrumental": bool(hit.get("instrumental")),
        "plainLyrics": plain or None,
        "syncedLyrics": synced or None,
        "provider": hit.get("provider"),
        "provider_label": hit.get("provider_label") or hit.get("provider"),
        # The UI's older field names (kept for the search list rendering).
        "artist": hit.get("matched_artist") or artist,
        "track": hit.get("matched_title") or track,
    }


@app.get("/api/lyrics/search")
async def lyrics_search(
    artist: str = Query(...), track: str = Query(...),
    album: str = Query(None), duration: int = Query(None),
):
    """Lyrics candidates from the configured provider chain.

    At most one row (the chain returns its best hit); the list shape is what
    the editor's candidate picker expects, and a miss is an empty list rather
    than a 404 so a search can just say "nothing found".
    """
    from mlo.lyrics_providers import fetch_lyrics
    try:
        hit = await asyncio.to_thread(
            fetch_lyrics, load_config(), artist, track, album, duration)
    except Exception as e:
        raise HTTPException(502, f"lyrics search failed: {e}")
    record = _lyrics_record(hit, artist, track, album, duration)
    return [record] if record else []


@app.get("/api/lyrics/get")
async def lyrics_get(
    artist: str = Query(""), track: str = Query(...),
    album: str = Query(None), duration: int = Query(None),
):
    """The best lyrics hit for a track, through the chain (with fallbacks)."""
    from mlo.lyrics_providers import fetch_lyrics
    try:
        hit = await asyncio.to_thread(
            fetch_lyrics, load_config(), artist, track, album, duration)
    except Exception as e:
        raise HTTPException(502, str(e))
    record = _lyrics_record(hit, artist, track, album, duration)
    if record is None:
        return JSONResponse({"found": False}, status_code=404)
    return {"found": True, **record}


class LyricsWriteRequest(BaseModel):
    path: str
    lrc: str = ""
    source: str = "lrclib"


@app.post("/api/lyrics/write")
def lyrics_write(req: LyricsWriteRequest):
    """Write .lrc sidecar atomically, canonicalized via mlo.lyrics."""
    from mlo.lyrics import _format_for_storage
    p = os.path.normpath(mbresolve.resolve_track(req.path) or req.path)
    if not os.path.isfile(p):
        raise HTTPException(404, "audio file not found")
    if not _in_music_folder(p, _music_folder()):
        raise HTTPException(400, "file outside music folder")
    lrc_path = os.path.splitext(p)[0] + ".lrc"
    cfg = load_config()
    try:
        final = _format_for_storage(req.lrc, cfg, optimize=True, is_for_lrc=True)
    except Exception:
        final = req.lrc
    fd, tmp = tempfile.mkstemp(prefix=".lrc_tmp_", suffix=".lrc", dir=os.path.dirname(p) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(final)
            try:
                f.flush()
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp, lrc_path)
    except Exception as e:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise HTTPException(500, str(e))
    tagcache.invalidate_path(p)
    return {"ok": True, "lrc": lrc_path.replace("\\", "/")}


class LyricsPublishRequest(BaseModel):
    """LRCLIB submission. `path` (optional) identifies a library track whose
    tags/duration seed the request when explicit fields are missing."""
    path: Optional[str] = None
    artist: str = ""
    track: str = ""
    album: str = ""
    duration: Optional[int] = None
    plain: str = ""
    synced: str = ""


@app.post("/api/lyrics/publish")
async def lyrics_publish(req: LyricsPublishRequest):
    """Submit lyrics to LRCLIB on behalf of a library track.

    The editor sends the exact text it shows; plain vs synced is detected
    from [mm:ss.xx] timestamps so pasting either form just works."""
    from server.integrations import lrclib_publish

    artist, track, album = req.artist.strip(), req.track.strip(), req.album.strip()
    duration = req.duration
    if req.path:
        p = os.path.normpath(mbresolve.resolve_track(req.path) or req.path)
        if os.path.isfile(p):
            tags, tech = tagcache.read_track(p, ("ARTIST", "ALBUMARTIST", "TITLE", "ALBUM"))
            artist = artist or (tags.get("ARTIST") or tags.get("ALBUMARTIST") or "").strip()
            track = track or (tags.get("TITLE") or "").strip()
            album = album or (tags.get("ALBUM") or "").strip()
            if not duration:
                try:
                    duration = int(round(float(tech.get("length") or 0))) or None
                except (TypeError, ValueError):
                    duration = None
    synced = req.synced if req.synced.strip() else None
    plain = req.plain if req.plain.strip() else None
    if synced is None and plain is None and req.path:
        # Fall back to the text already stored on the track.
        from mlo.audio import AudioFile
        af = AudioFile(p)
        if af.audio is not None:
            stored = af.get_lyrics()
            if stored:
                synced = stored
    ok, msg = await asyncio.to_thread(
        lrclib_publish, artist, track, album, duration, plain, synced)
    return {"ok": ok, "message": msg}


@app.get("/api/rym/validate")
def rym_validate(url: str = Query(...)):
    return {"valid": intg.parse_rym_album_url(url) is not None}


@app.get("/api/rym/resolve")
def rym_resolve(artist: str = Query(""), album: str = Query("")):
    """Verified RateYourMusic links for an album, for the link editor.

    Same resolution the import uses (rym_links_auto gates it): each link is
    None unless RYM itself confirmed that page, and `note` says why nothing
    was found — "could not resolve" is the normal "paste the URL yourself"
    answer, so it is a 200 here, never an error.
    """
    return intg.rym_links(artist, album, cfg=load_config())


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #
def is_audio_file(name):
    """Library tracks: audio + music-video containers (mlo.paths definition,
    so organize / genre import / MB matching treat videos as tracks too)."""
    from mlo.paths import LIB_AUDIO_EXTS
    return os.path.splitext(name)[1].lower() in LIB_AUDIO_EXTS


@app.post("/api/album/remove")
def album_remove(req: AlbumRemove):
    """Remove an album from the library by moving it into
    <music_folder>/.mlo/trash/ (recoverable, nothing is deleted)."""
    cfg = load_config()
    folder = cfg.get("music_folder") or ""
    if not folder or not os.path.isdir(folder):
        raise HTTPException(400, "music_folder not set or not found")
    p = os.path.normpath(req.path)
    if not os.path.isdir(p):
        raise HTTPException(404, "album not found")
    if not _in_music_folder(p, folder):
        raise HTTPException(400, "album outside music folder")
    trash = os.path.normpath(trash_dir(folder))
    os.makedirs(trash, exist_ok=True)
    name = os.path.basename(p) or "album"
    dest = os.path.normpath(os.path.join(trash, name))
    n = 2
    while os.path.exists(dest):
        dest = os.path.normpath(os.path.join(trash, f"{name} ({n})"))
        n += 1
    if not move_path(p, dest):
        # move_path already retried the sharing violation away; a player or
        # an importer still holds a file in the album open.
        raise HTTPException(
            500,
            f"could not move {name} to the trash — a file inside it is still "
            f"in use (stop playback and retry)")
    entries = _manifest_read(trash)
    entries[os.path.basename(dest)] = {
        "origin": p.replace("\\", "/"),
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    _manifest_write(trash, entries)
    tagcache.invalidate_all()
    mbresolve.invalidate()
    _refresh_slskd_shares_soon()
    return {"ok": True, "trash": dest.replace("\\", "/")}


# --------------------------------------------------------------------------- #
# Trash bin (the other end of POST /api/album/remove)
# --------------------------------------------------------------------------- #
# "[Album] 2010-12-15 - 2010-12-15 - Aimai Elegy {JP - CD - XECJ-1011}" -> the
# title is whatever sits between the date prefix and the brace suffix. Only
# structural decoration is dropped; nothing is ever synthesised.
_ALBUM_NAME_RE = re.compile(
    r"^\[Album\]\s*\d{4}-\d{2}-\d{2}\s*-\s*(?:\d{4}-\d{2}-\d{2}\s*-\s*)?(.+?)\s*\{[^{}]*\}\s*$")


def _trash_dir(folder):
    return os.path.normpath(trash_dir(folder))


# Origin manifest: lives INSIDE the bin (it is part of the data and must
# travel with it), so it is never listed, deleted or restored — see
# _trash_name_error.
_TRASH_MANIFEST = ".mlo_manifest.json"


def _manifest_path(trash):
    return os.path.join(trash, _TRASH_MANIFEST)


def _manifest_read(trash):
    """{entry name: {"origin": ..., "at": ...}}; a missing or corrupt
    manifest reads as empty — entries trashed before this file existed are a
    normal state, not an error."""
    try:
        with open(_manifest_path(trash), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    entries = data.get("entries") if isinstance(data, dict) else None
    return entries if isinstance(entries, dict) else {}


def _manifest_write(trash, entries):
    """Whole-file rewrite via temp + os.replace: a crash mid-write can never
    leave a half-written manifest behind."""
    tmp = _manifest_path(trash) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "entries": entries}, f)
    os.replace(tmp, _manifest_path(trash))


def _manifest_forget(trash, names):
    """Drop `names` from the manifest, atomically. Records for entries that no
    longer exist are not just dead weight: a later entry that never went
    through the move endpoint (dropped in by hand, or created by the dedupe
    suffix) would inherit the stale origin and 'restore' somewhere it never
    came from. With nothing left to remember, the file goes away entirely —
    the bin carries no bookkeeping it cannot back up."""
    entries = _manifest_read(trash)
    for n in names:
        entries.pop(n, None)
    if entries:
        _manifest_write(trash, entries)
        return
    try:
        os.remove(_manifest_path(trash))
    except OSError:
        pass


def _trash_label(name):
    """Display name for a trashed entry: the album title when the folder
    follows the app naming convention, otherwise the raw basename."""
    m = _ALBUM_NAME_RE.match(name)
    return (m.group(1).strip() if m and m.group(1).strip() else name)


# How many files of a trashed entry the listing spells out. The UI says
# "showing 200 of N"; file_count always reports the true total.
_TRASH_FILES_CAP = 200


def _is_link(path):
    """True for symlinks AND the Windows junctions a non-admin account has to
    use instead — os.path.islink misses the latter, yet their realpath
    resolves just as far away, which is the whole point of skipping them."""
    try:
        return os.path.islink(path) or bool(getattr(os.lstat(path), "st_reparse_tag", 0))
    except OSError:
        return False


def _dir_stats(path, collect=None):
    """(audio track count, recursive bytes) for a folder. Unreadable parts
    count as 0/0 rather than raising — a listing must never 500.

    `collect`, when a list, is filled with (rel, size) for every file — the
    same walk, so the file listing never pays for a second one. rel is
    forward-slashed and relative to `path`; unreadable files are still listed,
    with size 0, exactly like the size accounting above ignores them."""
    tracks = 0
    total = 0
    for root, dirs, files in os.walk(path, onerror=lambda e: None):
        dirs[:] = [d for d in dirs if not _is_link(os.path.join(root, d))]
        for f in files:
            full = os.path.join(root, f)
            try:
                size = os.path.getsize(full)
            except OSError:
                if collect is not None:
                    collect.append((os.path.relpath(full, path).replace("\\", "/"), 0))
                continue
            if collect is not None:
                collect.append((os.path.relpath(full, path).replace("\\", "/"), size))
            total += size
            if is_audio_file(f):
                tracks += 1
    return tracks, total


def _trash_entry(trash, name, root):
    """One listing row, or None when the child is not a plain folder/file
    inside the bin — symlinks, junctions and devices resolve elsewhere (or
    nowhere), and listing them would both lie about the path and let the walk
    escape the trash dir."""
    if name == _TRASH_MANIFEST:
        return None
    p = os.path.join(trash, name)
    if os.path.islink(p) or not _in_music_folder(os.path.realpath(p), root):
        return None
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        mtime = 0
    if os.path.isdir(p):
        found = []
        tracks, size = _dir_stats(p, found)
        # cover_bytes() is the same search GET /api/cover uses, so the bin
        # agrees with the album view about what counts as a cover.
        cover = tagcache.cover_bytes(p)[0] is not None
        kind = "album"
    elif os.path.isfile(p):
        kind, cover, tracks = "file", False, 1 if is_audio_file(name) else 0
        try:
            size = os.path.getsize(p)
        except OSError:
            size = 0
        found = [(name, size)]
    else:
        return None
    found.sort()
    return {
        "name": name,
        "path": p.replace("\\", "/"),
        "kind": kind,
        "label": _trash_label(name),
        "tracks": tracks,
        "bytes": size,
        "trashed_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(mtime)),
        "cover": cover,
        # sorted by rel; file_count is the true total, files may be capped
        "file_count": len(found),
        "files": [{"name": rel.rsplit("/", 1)[-1], "rel": rel, "bytes": n}
                  for rel, n in found[:_TRASH_FILES_CAP]],
        # sort key only; stripped before the response leaves
        "_mtime": mtime,
    }


@app.get("/api/trash")
def trash_list():
    """Contents of <music_folder>/.mlo/trash, newest entry first."""
    folder = load_config().get("music_folder") or ""
    trash = _trash_dir(folder) if folder else ""
    out = {"folder": trash.replace("\\", "/"), "exists": False,
           "count": 0, "bytes": 0, "entries": [],
           "music_folder": folder.replace("\\", "/")}
    if not trash or not os.path.isdir(trash):
        return out
    try:
        names = os.listdir(trash)
    except OSError:
        return out
    origins = {}
    for n, rec in _manifest_read(trash).items():
        origin = rec.get("origin") if isinstance(rec, dict) else None
        if isinstance(origin, str) and origin:
            origins[n] = origin
    entries = [e for e in (_trash_entry(trash, n, os.path.realpath(trash))
                           for n in names) if e]
    for e in entries:
        e["origin"] = origins.get(e["name"])
    entries.sort(key=lambda e: e["_mtime"], reverse=True)
    for e in entries:
        del e["_mtime"]
    out.update(exists=True, count=len(entries), entries=entries,
               bytes=sum(e["bytes"] for e in entries))
    return out


def _trash_name_error(name, root):
    """Why `name` may not be deleted from or restored out of the trash dir,
    or None when fine.

    Names travel as basenames over the API, so anything that is not a single
    plain path segment — or that resolves (symlinks included) outside the
    trash dir — is refused before a single byte is touched.
    """
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        return "not a valid trash entry name"
    if name == _TRASH_MANIFEST:
        return "reserved trash file"
    try:
        real = os.path.realpath(os.path.join(root, name))
    except (OSError, ValueError):
        return "unresolvable path"
    if os.path.dirname(real) != root:
        return "outside the trash folder"
    return None


@app.post("/api/trash/delete")
def trash_delete(req: TrashDelete = TrashDelete()):
    """Permanently delete trash entries by basename. Unknown names and
    refused names land in `failed`; nothing else is an error."""
    import shutil
    cfg = load_config()
    folder = cfg.get("music_folder") or ""
    if not folder or not os.path.isdir(folder):
        raise HTTPException(400, "music_folder not set or not found")
    trash = _trash_dir(folder)
    if not os.path.isdir(trash):
        raise HTTPException(404, "trash folder not found")
    root = os.path.realpath(trash)
    deleted, failed, freed = [], [], 0
    for name in req.names:
        err = _trash_name_error(name, root)
        p = os.path.join(trash, name) if err is None else ""
        if err is None and not os.path.lexists(p):
            err = "not found in trash"
        if err is None:
            try:
                # Size first: once rmtree has run the bytes are unrecoverable.
                if os.path.islink(p):
                    # realpath() already proved the target is inside the bin;
                    # unlink the link itself, never what it points at.
                    size = 0
                    os.remove(p)
                elif os.path.isdir(p):
                    size = _dir_stats(p)[1]
                    shutil.rmtree(p)
                else:
                    size = os.path.getsize(p)
                    os.remove(p)
                freed += size
            except OSError as e:
                err = str(e) or "delete failed"
        if err:
            failed.append({"name": name, "error": err})
        else:
            deleted.append(name)
    if deleted:
        # The entries are gone for good, so their origin records go too.
        _manifest_forget(trash, deleted)
        # Same invalidation the move endpoint does: the library, MB cache and
        # slskd shares all still describe the deleted files.
        tagcache.invalidate_all()
        mbresolve.invalidate()
        _refresh_slskd_shares_soon()
    return {"deleted": deleted, "failed": failed, "freed": freed}


@app.post("/api/trash/restore")
def trash_restore(req: TrashRestore = TrashRestore()):
    """Move trash entries back into the library: each returns to the location
    it was trashed from, or — when it has no manifest record — into `dest`.
    Per-entry problems land in `failed`; an unusable `dest` is a 400 for the
    whole request, so a bad request never moves half a batch."""
    cfg = load_config()
    folder = cfg.get("music_folder") or ""
    if not folder or not os.path.isdir(folder):
        raise HTTPException(400, "music_folder not set or not found")
    trash = _trash_dir(folder)
    if not os.path.isdir(trash):
        raise HTTPException(404, "trash folder not found")
    dest = ""
    if req.dest is not None:
        dest = os.path.normpath(req.dest)
        if not os.path.isdir(dest) or not _in_music_folder(dest, folder):
            raise HTTPException(400, "dest must be an existing folder inside the music folder")
    root = os.path.realpath(trash)
    origins = _manifest_read(trash)
    restored, failed = [], []
    for name in req.names:
        err = _trash_name_error(name, root)
        src = os.path.join(trash, name)
        if err is None and not os.path.lexists(src):
            err = "not found in trash"
        target = ""
        if err is None:
            rec = origins.get(name)
            origin = rec.get("origin") if isinstance(rec, dict) else None
            if isinstance(origin, str) and origin.strip():
                if not _in_music_folder(origin, folder):
                    err = "original location is outside the music folder"
                else:
                    target = os.path.normpath(origin)
            elif dest:
                target = os.path.join(dest, name)
            else:
                err = "original location unknown — pass dest"
        if err is None and os.path.lexists(target):
            # Never overwrite: the occupant there is somebody's data too.
            err = f"already exists: {target.replace(chr(92), '/')}"
        if err is None:
            try:
                # The artist folder is often gone by now; recreate it.
                parent = os.path.dirname(target)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                if not move_path(src, target):
                    err = "move failed — a file inside is still in use"
            except OSError as e:
                err = str(e) or "restore failed"
        if err:
            failed.append({"name": name, "error": err})
        else:
            origins.pop(name, None)
            restored.append({"name": name, "to": target.replace("\\", "/")})
    if restored:
        # Same invalidation the move endpoint does — the library just gained
        # albums back, and the manifest just lost rows.
        _manifest_write(trash, origins)
        tagcache.invalidate_all()
        mbresolve.invalidate()
        _refresh_slskd_shares_soon()
    return {"restored": restored, "failed": failed}


@app.get("/api/album/scan-tracks")
def album_scan_tracks(path: str = Query(...)):
    """Direct folder scan: audio files with tags (independent of the library)."""
    p = os.path.normpath(path)
    if not os.path.isdir(p):
        raise HTTPException(404, "folder not found")
    if not _in_music_folder(p, _music_folder()):
        raise HTTPException(400, "folder outside music folder")
    return {"path": p.replace("\\", "/"), "tracks": _scan_album_tracks(p)}


class OrganizeRequest(BaseModel):
    paths: List[str]
    dry_run: bool = False


class BeetsImportRequest(BaseModel):
    paths: List[str]


@app.get("/api/beets/status")
def beets_status():
    """Vendored-beets availability and the config that would be generated."""
    from server import beetscfg
    version = beetscfg.beets_available()
    cfg = load_config()
    return {
        "installed": bool(version),
        "version": version,
        "db": beetscfg.db_path(),
        "config": beetscfg.generate_config(cfg),
    }


@app.post("/api/beets/install")
def beets_install():
    """Vendor beets into .dependencies (pip-managed, like other tools)."""
    from mlo import fetchdeps
    fetchdeps.install_dependency("beets", log=lambda m: None)
    return {"ok": True, "version": fetchdeps.PINNED["beets"]["version"]}


@app.post("/api/beets/import")
def beets_import(req: BeetsImportRequest):
    """Tag albums with managed beets (MusicBrainz match + Picard-parity
    plugin: locale aliases, work/movement, release-type caps), then
    optionally re-organize file placement with the naming script."""
    from server import beetscfg
    folder = _music_folder()
    if not folder or not os.path.isdir(folder):
        raise HTTPException(400, "music_folder not set or not found")
    valid = []
    for p in req.paths:
        np = os.path.normpath(p)
        if not os.path.isdir(np):
            raise HTTPException(404, f"album not found: {p}")
        if not _in_music_folder(np, folder):
            raise HTTPException(400, f"album outside music folder: {p}")
        valid.append(np)
    cutoff = time.time()
    ok, output = beetscfg.run_beets_import(valid)
    if not ok:
        raise HTTPException(500, output[-2000:])
    tagcache.invalidate_all()
    mbresolve.invalidate()
    organized = None
    if load_config().get("beets_organize_after", False):
        # beets already placed files per the naming script; organize only the
        # album folders this import touched (fresh mtimes under music folder)
        # so stale-file failures on the pre-import paths are impossible.
        from mlo.stats import _find_albums
        fresh = [d for d in _find_albums(folder)
                 if os.path.getmtime(d) >= cutoff - 1]
        if fresh:
            res = organize(OrganizeRequest(paths=fresh, dry_run=False))
            organized = res.get("results")
    return {"ok": True, "output": output[-4000:], "organized": organized}


class SoulseekSearchRequest(BaseModel):
    query: str


class SoulseekDownloadRequest(BaseModel):
    username: str
    files: List[dict]  # [{filename, size}]


@app.get("/api/soulseek/status")
def soulseek_status():
    """Managed slskd availability, running state, login and download dir."""
    from server import soulseek
    cfg = load_config()
    owns, foreign_user, conflict = soulseek.instance_owner(cfg)
    running = soulseek.is_running() or owns
    logged_in = None
    server = None
    if running:
        try:
            server = soulseek.server_state()
            logged_in = bool(server and server.get("isLoggedIn"))
        except Exception:
            logged_in = False
    return {
        "installed": soulseek.slskd_installed(),
        "running": running,
        "logged_in": logged_in,
        # the daemon's own words for a failed login (INVALIDPASS, empty
        # credentials, a port it could not bind). Without it the UI can only
        # guess "not logged in" and show a generic cooldown hint.
        "error": soulseek.login_error(cfg) if running and logged_in is False else None,
        # set when slskd's web port is held by ANOTHER app's slskd (default
        # port 5030 is shared). The UI must explain that instead of the
        # misleading "running, not logged in".
        "conflict": conflict or None,
        "conflict_username": foreign_user,
        "server": server,
        "download_dir": soulseek.download_dir(cfg),
        "web_port": int(cfg.get("soulseek_web_port") or 5030),
        "listen_port": int(cfg.get("soulseek_listen_port") or 50000),
        # saved credentials — the Soulseek tab prefills the login form and
        # slskd auto-connects with them at every start
        "username": str(cfg.get("soulseek_username") or ""),
        "password": str(cfg.get("soulseek_password") or ""),
        "has_credentials": bool(str(cfg.get("soulseek_username") or "").strip()
                                and cfg.get("soulseek_password")),
        "autostart": bool(cfg.get("soulseek_autostart", True)),
        "share_dirs": soulseek.share_dirs(cfg),
    }


@app.post("/api/soulseek/start")
def soulseek_start():
    """Spawn slskd and return quickly — the UI polls /status (1s) for the
    web API / network login instead of this request blocking for the whole
    boot (first boot re-scans the whole shared library, which takes a while).

    Saved credentials (from a previous login) make slskd connect to the
    Soulseek network automatically — no login form needed."""
    from server import soulseek
    ok, msg = soulseek.start()
    if not ok:
        raise HTTPException(400, msg)
    ready = soulseek.wait_until_ready(timeout=6.0)
    cfg = load_config()
    return {
        "ok": True,
        "ready": ready,
        "message": msg,
        "has_credentials": bool(str(cfg.get("soulseek_username") or "").strip()
                                and cfg.get("soulseek_password")),
    }


@app.post("/api/soulseek/restart")
def soulseek_restart():
    """Restart slskd (e.g. to apply new ports) and wait until it answers."""
    from server import soulseek
    if not (soulseek.is_running() or soulseek.web_up(load_config())):
        raise HTTPException(400, "slskd is not running")
    if not soulseek.restart():
        raise HTTPException(504, "slskd did not become ready in time")
    return {"ok": True}


@app.get("/api/soulseek/shares")
def soulseek_shares():
    """Share configuration (la musica settings are the source of truth —
    the slskd yaml is regenerated from them) plus slskd's live scan state."""
    from server import soulseek
    cfg = load_config()
    return {
        "dirs": soulseek.share_dirs(cfg),
        "exclude": [x.strip("'") for x in soulseek.share_exclude(cfg)],
        "share_library": bool(cfg.get("soulseek_share_library", True)),
        "autostart": bool(cfg.get("soulseek_autostart", True)),
        "slskd": soulseek.shares_state(cfg),
    }


class SoulseekSharesRequest(BaseModel):
    dirs: List[str] = []
    autostart: Optional[bool] = None
    apply: bool = True


@app.post("/api/soulseek/shares")
def soulseek_shares_update(req: SoulseekSharesRequest):
    """Save the shared-folder list (and autostart flag). Restarting slskd
    re-indexes the shares — share changes only apply after a restart."""
    from server import soulseek
    cfg = load_config()
    dirs = sorted({os.path.normpath(str(d).strip()) for d in req.dirs if str(d).strip()})
    for d in dirs:
        if not os.path.isdir(d):
            raise HTTPException(400, f"not a folder: {d}")
    cfg["soulseek_share_dirs"] = dirs
    if req.autostart is not None:
        cfg["soulseek_autostart"] = req.autostart
    save_config(cfg)
    restarted = False
    if req.apply and (soulseek.is_running() or soulseek.web_up(cfg)):
        restarted = soulseek.restart()
        if not restarted:
            raise HTTPException(504, "slskd did not become ready in time")
    return {"ok": True, "dirs": dirs, "autostart": cfg["soulseek_autostart"],
            "restarted": restarted}


@app.post("/api/soulseek/shares/rescan")
def soulseek_shares_rescan():
    """Ask slskd to rescan its share index (picks up library changes)."""
    from server import soulseek
    if not (soulseek.is_running() or soulseek.web_up(load_config())):
        raise HTTPException(400, "slskd is not running")
    soulseek.rescan_shares()
    return {"ok": True}


@app.post("/api/soulseek/stop")
def soulseek_stop():
    from server import soulseek
    stopped = soulseek.stop()
    if not stopped:
        return {"ok": True, "message": "not running"}
    return {"ok": True, "message": "stopped"}


@app.post("/api/soulseek/search")
def soulseek_search(req: SoulseekSearchRequest):
    """Start a Soulseek search; returns an id to poll for results."""
    from server import soulseek
    if not (soulseek.is_running() or soulseek.web_up(load_config())):
        raise HTTPException(400, "slskd is not running — start it first")
    if not req.query.strip():
        raise HTTPException(400, "empty query")
    return {"id": soulseek.search(req.query.strip())}


@app.get("/api/soulseek/search/{search_id}")
def soulseek_search_results(search_id: str):
    from server import soulseek
    # adopted slskd (spawned by an earlier run) answers on the web port even
    # though no child handle exists — it must keep serving searches
    if not (soulseek.is_running() or soulseek.web_up(load_config())):
        raise HTTPException(400, "slskd is not running")
    return soulseek.search_results(search_id)


class SoulseekSearchCancelRequest(BaseModel):
    id: str


@app.post("/api/soulseek/search/cancel")
def soulseek_search_cancel(req: SoulseekSearchCancelRequest):
    """Cancel a running search (slskd DELETE /searches/{id}).

    Without this the app is committed to the whole search window plus the
    grace tail; the UI's "stop" only stopped polling."""
    from server import soulseek
    if not (soulseek.is_running() or soulseek.web_up(load_config())):
        raise HTTPException(503, "slskd is not running")
    sid = str(req.id or "").strip()
    if not sid:
        raise HTTPException(400, "search id is required")
    try:
        soulseek.cancel_search(sid)
    except Exception as e:
        raise HTTPException(502, f"search cancel failed: {e}")
    return {"ok": True}


@app.post("/api/soulseek/download")
def soulseek_download(req: SoulseekDownloadRequest):
    """Queue files from a user for download into the download dir."""
    from server import soulseek
    if not (soulseek.is_running() or soulseek.web_up()):
        raise HTTPException(400, "slskd is not running — start it first")
    if not req.username or not req.files:
        raise HTTPException(400, "username and files required")
    soulseek.enqueue_download(req.username, req.files)
    return {"ok": True, "queued": len(req.files)}


@app.get("/api/soulseek/downloads")
def soulseek_downloads():
    """Download transfer tree (per user / directory / file with state).

    503 when slskd is down: an empty list would read as "nothing queued"
    while the queue is really unreachable."""
    from server import soulseek
    if not (soulseek.is_running() or soulseek.web_up(load_config())):
        raise HTTPException(503, "slskd is not running — start it first")
    return {"downloads": soulseek.downloads_state()}


class SoulseekBulkDownloadRequest(BaseModel):
    """Queue an arbitrary file list from ONE user (multi-folder / whole-share)."""
    username: str
    files: List[dict]  # [{filename, size}]


class SoulseekUserDownloadRequest(BaseModel):
    """Queue every shared file of ONE user (optionally under one folder)."""
    username: str
    folder: Optional[str] = None


@app.post("/api/soulseek/download-bulk")
def soulseek_download_bulk(req: SoulseekBulkDownloadRequest):
    """Queue a list of files from one user in a single POST.

    slskd's enqueue route is per-user, so several folders (or a whole share)
    are one call — the UI sends what the user selected. Returns {queued}."""
    from server import soulseek
    if not (soulseek.is_running() or soulseek.web_up()):
        raise HTTPException(503, "slskd is not running — start it first")
    files = [{"filename": str(f.get("filename") or ""), "size": int(f.get("size") or 0)}
             for f in (req.files or []) if str(f.get("filename") or "").strip()]
    if not str(req.username or "").strip() or not files:
        raise HTTPException(400, "username and a non-empty files list are required")
    soulseek.enqueue_download(req.username, files)
    return {"queued": len(files)}


@app.post("/api/soulseek/download-user")
def soulseek_download_user(req: SoulseekUserDownloadRequest):
    """Queue a remote user's whole share, or one folder of it.

    The share tree is browsed first (that is where the file list comes from),
    then every file is queued in ONE per-user call. Returns
    {queued, scanned, skipped} — `scanned` is every file the share listed,
    `skipped` those left out because a transfer for them is already queued
    or downloading, so pressing the button twice does not double-queue."""
    from server import soulseek
    if not (soulseek.is_running() or soulseek.web_up()):
        raise HTTPException(503, "slskd is not running — start it first")
    username = str(req.username or "").strip()
    if not username:
        raise HTTPException(400, "username is required")
    try:
        dirs = soulseek.browse(username, use_cache=False) or []
    except Exception as e:
        raise HTTPException(502, f"browse failed: {e}")

    folder = str(req.folder or "").strip().replace("\\", "/").rstrip("/")
    wanted = []
    scanned = 0
    for d in dirs:
        dpath = str(d.get("directory") or "")
        if folder and not (dpath.replace("\\", "/").rstrip("/").lower() == folder.lower()
                           or dpath.replace("\\", "/").rstrip("/").lower().startswith(folder.lower() + "/")):
            continue
        for f in d.get("files") or []:
            name = str(f.get("filename") or "").strip()
            if not name:
                continue
            scanned += 1
            wanted.append({"filename": name, "size": int(f.get("size") or 0)})
    if not wanted:
        raise HTTPException(404, "no files to queue (folder not found in the share?)")

    active = set()
    try:
        for entry in soulseek.downloads_state() or []:
            if str(entry.get("username") or "").lower() != username.lower():
                continue
            for d in entry.get("directories") or []:
                for f in d.get("files") or []:
                    if not soulseek.finished_transfer(f.get("state")):
                        active.add(str(f.get("filename") or ""))
    except Exception:
        active = set()

    queue = [f for f in wanted if f["filename"] not in active]
    if queue:
        soulseek.enqueue_download(username, queue)
    return {"queued": len(queue), "scanned": scanned,
            "skipped": len(wanted) - len(queue)}


class SoulseekCancelRequest(BaseModel):
    username: str
    transfer_ids: List[str]


@app.post("/api/soulseek/downloads/cancel")
def soulseek_downloads_cancel(req: SoulseekCancelRequest):
    """Drop transfers from slskd's list (per-file or whole-queue cancel).
    The underlying DELETE carries ?remove=true because a cancelled transfer
    otherwise stays queued and slskd keeps re-requesting the very files the
    review step just deleted."""
    from server import soulseek
    if not (soulseek.is_running() or soulseek.web_up(load_config())):
        raise HTTPException(400, "slskd is not running")
    ids = [str(t) for t in req.transfer_ids if str(t).strip()]
    try:
        # best effort per transfer: ids already gone must not fail the call
        soulseek.cancel_downloads(req.username, ids)
    except Exception as e:
        raise HTTPException(502, f"cancel failed: {e}")
    return {"ok": True, "cancelled": len(ids)}


class SoulseekClearRequest(BaseModel):
    username: Optional[str] = None
    states: Optional[List[str]] = None


@app.post("/api/soulseek/downloads/clear")
def soulseek_downloads_clear(req: SoulseekClearRequest):
    """Clear FINISHED transfers from the history — optionally for one user,
    optionally narrowed to some of the finished states. In-progress and
    queued transfers are never touched: the queue is the only record of what
    is still coming, and clearing it mid-download throws away bytes already
    on disk. The response counts the finished transfers handed to slskd's
    per-transfer DELETE (best effort: one already gone still counts)."""
    from server import soulseek
    if not (soulseek.is_running() or soulseek.web_up(load_config())):
        raise HTTPException(400, "slskd is not running")
    wanted = [str(s).strip().lower() for s in (req.states or []) if str(s).strip()]
    try:
        tree = soulseek.downloads_state()
    except Exception as e:
        raise HTTPException(502, f"downloads lookup failed: {e}")

    targets = {}
    for user in tree or []:
        who = str(user.get("username") or "")
        if req.username and who != req.username:
            continue
        for d in (user.get("directories") or []):
            for f in (d.get("files") or []):
                st = str(f.get("state") or "")
                # `states` only narrows WITHIN the finished ones, so a client
                # asking for "InProgress" clears nothing instead of killing a
                # live download
                if not soulseek.finished_transfer(st):
                    continue
                if wanted and not any(w in st.lower() for w in wanted):
                    continue
                if f.get("id"):
                    targets.setdefault(who, []).append(str(f["id"]))

    cleared = 0
    for who, tids in targets.items():
        # one user's failure must not abort the rest (cancel_downloads is a
        # best-effort loop over one DELETE per transfer)
        try:
            soulseek.cancel_downloads(who, tids)
        except Exception:
            continue
        cleared += len(tids)
    return {"ok": True, "cleared": cleared}


@app.get("/api/soulseek/uploads")
def soulseek_uploads():
    """Upload transfer tree — the shared-history view (per user / file).

    503 when slskd is down (an empty list would read as "nothing shared yet")."""
    from server import soulseek
    if not (soulseek.is_running() or soulseek.web_up(load_config())):
        raise HTTPException(503, "slskd is not running — start it first")
    return {"uploads": soulseek.uploads_state()}


def _review_file_info(p, ffprobe=None):
    """Tech + tags for one completed download file (review panel row)."""
    from mlo.audio import AudioFile
    from mlo.paths import LIB_VIDEO_EXTS

    name = os.path.basename(p)
    ext = os.path.splitext(name)[1].lower()
    is_video = ext in LIB_VIDEO_EXTS
    af = AudioFile(p)
    tags = af.all_tags() if af.audio is not None else {}
    tech = getattr(af, "tech", None)
    if not tech and af.audio is not None:
        tech = tagcache.read_track(p)[1]
    return {
        "path": p.replace("\\", "/"),
        "file": name,
        "ext": ext.lstrip(".").upper(),
        "is_video": is_video,
        "size": os.path.getsize(p),
        "mtime": os.path.getmtime(p),
        "tags": {k: v for k, v in (tags or {}).items()
                 if k in ("TITLE", "ARTIST", "ALBUM", "DISCNUMBER", "TRACKNUMBER", "DATE", "GENRE")},
        "tech": tech or {},
    }


@app.get("/api/soulseek/review")
def soulseek_review():
    """Completed downloads on disk, ready for the review workflow:
    preview locally, tag them (VOB → tagged MKV), then import."""
    from mlo.paths import AUDIO_EXTS, LIB_VIDEO_EXTS

    from server import soulseek

    cfg = load_config()
    ddir = soulseek.download_dir(cfg)
    out = []
    if os.path.isdir(ddir):
        wanted = set(AUDIO_EXTS) | set(LIB_VIDEO_EXTS) | {".log", ".cue", ".jpg", ".jpeg", ".png", ".pdf", ".txt"}
        for root, dirs, files in os.walk(ddir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in sorted(files):
                if os.path.splitext(f)[1].lower() not in wanted:
                    continue
                p = os.path.join(root, f)
                try:
                    entry = _review_file_info(p)
                except Exception:
                    continue
                entry["user"] = os.path.relpath(root, ddir).split(os.sep)[0]
                out.append(entry)
    return {"dir": ddir.replace("\\", "/"), "files": out}


@app.get("/api/soulseek/local-file")
def soulseek_local_file(path: str = Query(...)):
    """Stream a completed download straight from the download dir for
    in-app preview (guarded to the download dir — the library stream
    endpoint only serves the music folder)."""
    from server import soulseek

    p = os.path.normpath(path)
    ddir = os.path.abspath(soulseek.download_dir(load_config()))
    if not _in_music_folder(p, ddir):
        raise HTTPException(400, "path outside the download dir")
    if not os.path.isfile(p):
        raise HTTPException(404, "file not found")
    ctype = _CTYPES.get(os.path.splitext(p)[1].lower(), "application/octet-stream")
    return FileResponse(p, media_type=ctype, headers={"Accept-Ranges": "bytes"})


# Containers Chromium can decode in a <video> element. Everything else —
# DVD-Video VOB (MPEG-PS), Blu-ray M2TS, MPEG-2 in AVI — is previewed
# through the live transcode endpoint below.
_NATIVE_VIDEO_EXTS = {".mp4", ".m4v", ".webm", ".mkv", ".mov", ".ogv", ".3gp", ".3g2"}


@app.get("/api/soulseek/preview-stream")
def soulseek_preview_stream(path: str = Query(...), native: int = Query(0)):
    """Playable preview of a downloaded video. ?native=1 streams the file
    as-is; the default pipes it through ffmpeg into a fragmented MP4
    (H.264/AAC, 480p) so browsers can play DVD/Blu-ray rips they cannot
    decode. Preview only — the stream never touches the file on disk;
    import/remux always uses the original bytes."""
    import subprocess
    from server import soulseek
    from mlo.tools import detect_all_tools

    p = os.path.normpath(path)
    ddir = os.path.abspath(soulseek.download_dir(load_config()))
    if not _in_music_folder(p, ddir):
        raise HTTPException(400, "path outside the download dir")
    if not os.path.isfile(p):
        raise HTTPException(404, "file not found")
    ext = os.path.splitext(p)[1].lower()

    if native or ext in _NATIVE_VIDEO_EXTS:
        ctype = _CTYPES.get(ext, "application/octet-stream")
        return FileResponse(p, media_type=ctype, headers={"Accept-Ranges": "bytes"})

    ffmpeg = (detect_all_tools().get("ffmpeg") or {}).get("ffmpeg_exe")
    if not ffmpeg:
        raise HTTPException(503, "ffmpeg not installed — install it under Dependencies for video previews")
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", p,
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "27",
        "-vf", "scale=-2:min(480\\,ih)",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "pipe:1",
    ]
    try:
        # CREATE_NO_WINDOW — same reason as videos_stream above.
        # stderr DEVNULL: undrained PIPE blocks ffmpeg, hangs stream.
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=0x08000000 if os.name == "nt" else 0)
    except Exception as e:
        raise HTTPException(500, f"ffmpeg failed to start: {e}")

    from starlette.responses import StreamingResponse
    from mlo.subproc import _kill_tree

    def _gen():
        try:
            if proc.stdout is None:
                return
            while True:
                chunk = proc.stdout.read(256 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            # Runs on client disconnect, read error and normal end alike.
            # ffmpeg holds the previewed file open for as long as it lives,
            # and a killed-but-unreaped child keeps that handle — so the
            # import move that follows a preview hits a sharing violation.
            # Kill the tree, then WAIT for it to actually die.
            try:
                if proc.poll() is None:
                    _kill_tree(proc)
                if proc.stdout is not None:
                    proc.stdout.close()
            except Exception:
                pass
            finally:
                try:
                    proc.wait()
                except Exception:
                    pass

    return StreamingResponse(_gen(), media_type="video/mp4")


class LocalFileDeleteRequest(BaseModel):
    path: str


@app.post("/api/soulseek/local-file/delete")
def soulseek_local_file_delete(req: LocalFileDeleteRequest):
    """Discard a previewed download (removes the file, not the library)."""
    from server import soulseek

    p = os.path.normpath(req.path)
    ddir = os.path.abspath(soulseek.download_dir(load_config()))
    if not _in_music_folder(p, ddir):
        raise HTTPException(400, "path outside the download dir")
    if os.path.isfile(p):
        os.remove(p)
        try:
            for root, dirs, files in os.walk(ddir, topdown=False):
                if os.path.abspath(root).startswith(os.path.abspath(ddir)) and not os.listdir(root) and root != ddir:
                    os.rmdir(root)
        except OSError:
            pass
    return {"ok": True}


# DVD / Blu-ray disc-image markers used to classify a downloaded rip.
_DVD_EXTS = {".vob", ".ifo", ".bup", ".vro"}
_BD_EXTS = {".m2ts", ".mts"}
_VIDEO_EXTS = {".vob", ".mpg", ".mpeg", ".m2v", ".ts", ".m2ts", ".mts",
               ".mkv", ".mp4", ".avi", ".wmv", ".mov", ".webm", ".flv"}


def _detect_rip_media(album_dir):
    """Classify a downloaded rip so grading gets the right MEDIA tag:
    DVD/Blu-ray disc images, verified CD rips (log+cue), or digital media."""
    exts = set()
    has_log = has_cue = False
    for _root, _dirs, files in os.walk(album_dir):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            exts.add(ext)
            if ext == ".log":
                has_log = True
            elif ext == ".cue":
                has_cue = True
    if exts & _DVD_EXTS:
        return "DVD-Video"
    if exts & _BD_EXTS:
        return "Blu-ray"
    if has_log and has_cue:
        return "CD"
    if exts & _VIDEO_EXTS:
        # a plain digital video rip (no disc image)
        return "Digital Media"
    if any(e in exts for e in (".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aac")):
        return "Digital Media"
    return None


def _tag_media_for_albums(album_dirs):
    """Write MEDIA (and SOURCE for digital rips) on every audio file of the
    freshly imported albums so grading and the library filters work."""
    from mlo.audio import AudioFile

    tagged = 0
    for d in album_dirs:
        media = _detect_rip_media(d)
        if not media:
            continue
        for root, _dirs, files in os.walk(d):
            for f in sorted(files):
                if not is_audio_file(f):
                    continue
                try:
                    af = AudioFile(os.path.join(root, f))
                    if af.audio is None:
                        continue
                    if not str(af.get_tag("MEDIA") or "").strip():
                        af.set_tag("MEDIA", media)
                    if media == "Digital Media" and not str(af.get_tag("SOURCE") or "").strip():
                        af.set_tag("SOURCE", "Soulseek")
                    tagged += 1
                except Exception:
                    continue
    return tagged


def _stamp_import_identity(album_dirs):
    """Canonicalize the MusicBrainz identity of hand-downloaded albums.

    The auto-import path stamps the release it verified; a folder downloaded
    by hand carries whatever the uploader tagged — often another pressing's
    IDs, and never RELEASECOUNTRY/RELEASESTATUS/RELEASETYPE (the naming
    script's $releasecountry reads RELEASECOUNTRY). When such an album names
    a MusicBrainz release, resolve it and reuse the auto-import stamping.
    Best effort: a MusicBrainz outage never fails the import."""
    from mlo.audio import AudioFile
    from server import integrations as intg
    from server import soulseek_auto

    stamped = 0
    for d in album_dirs:
        mbid = ""
        for root, _dirs, files in os.walk(d):
            for f in sorted(files):
                if not is_audio_file(f):
                    continue
                try:
                    mbid = str(AudioFile(os.path.join(root, f)
                                         ).get_tag("MUSICBRAINZ_ALBUMID") or "").strip()
                except Exception:
                    mbid = ""
                break
            if mbid:
                break
        if not mbid:
            continue
        try:
            release = intg.release_lookup(mbid)
            stamped += soulseek_auto._stamp_mb_tags(d, release)
        except Exception:
            import traceback
            traceback.print_exc()
    return stamped


@app.post("/api/soulseek/import")
def soulseek_import():
    """Move completed downloads from the download dir into the library,
    one album folder per shared folder, then immediately organize each
    imported album with the naming script — one click takes a download
    from slskd to a graded-library-ready album folder.

    Albums whose transfers are still running stay in the download dir and
    are reported in `skipped`, so the UI can say "still downloading".

    A folder a player (or slskd) still holds open is not fatal: the albums
    that did move are kept and organized, and the one that could not is
    reported in `failed` as {"path", "reason"} so the UI can name it and
    tell the user to stop playback.
    """
    from server import soulseek
    try:
        moved = soulseek.import_completed()
    except ValueError as e:
        raise HTTPException(400, str(e))
    skipped = soulseek.last_import_skipped()
    failed = soulseek.last_import_failed()
    media_tagged = 0
    converted = 0
    identity_stamped = 0
    if moved:
        # Lossless sources (WAV/APE/ALAC...) become the configured lossless
        # codec before anything is tagged or named, so the naming script and
        # the grader both see the final files.
        try:
            from mlo.flac import convert_album_lossless
            for album in moved:
                converted += int((convert_album_lossless(album, load_config())
                                  or {}).get("modified_count") or 0)
        except Exception:
            import traceback
            traceback.print_exc()
        # Classify each rip (CD / DVD-Video / Blu-ray / Digital Media) and
        # write the tags BEFORE organizing, so they travel with the files.
        try:
            media_tagged = _tag_media_for_albums(moved)
        except Exception:
            import traceback
            traceback.print_exc()
        # Then canonicalize the MusicBrainz identity (RELEASECOUNTRY etc.)
        # BEFORE the naming script runs, or $releasecountry comes out empty.
        try:
            identity_stamped = _stamp_import_identity(moved)
        except Exception:
            import traceback
            traceback.print_exc()
        try:
            # Best-effort: organize failures must not lose the imported files
            # (they stay in their import folders and can be organized later).
            organize(OrganizeRequest(paths=moved, dry_run=False))
        except Exception as e:
            import traceback
            traceback.print_exc()
            tagcache.invalidate_all()
            mbresolve.invalidate()
            return {"ok": True, "moved": moved, "skipped": skipped,
                    "failed": failed,
                    "organized": False,
                    "organize_error": str(e), "media_tagged": media_tagged,
                    "converted": converted, "tagging_started": False,
                    "identity_stamped": identity_stamped}
        # Fire-and-forget: the configured import script chain per album
        # (CUEs → FLACs → videos → lyrics → mood/genre → images → audit →
        # DR & ReplayGain → AccurateRip → key & BPM → beets → format all →
        # grade) on a background thread, so the HTTP call returns at once.
        from server import imports as imports_mod
        for album in moved:
            threading.Thread(
                target=imports_mod.finish_album,
                args=(album, load_config()),
                daemon=True,
            ).start()
    tagcache.invalidate_all()
    mbresolve.invalidate()
    return {"ok": True, "moved": moved, "skipped": skipped,
            "failed": failed,
            "organized": bool(moved),
            "media_tagged": media_tagged, "converted": converted,
            "identity_stamped": identity_stamped,
            "tagging_started": bool(moved)}


@app.get("/api/soulseek/user/{username}")
def soulseek_user(username: str):
    """Remote user profile info (speed, slots, shared file count)."""
    from server import soulseek
    if not soulseek.is_running():
        raise HTTPException(400, "slskd is not running")
    try:
        return soulseek.user_info(username)
    except Exception as e:
        raise HTTPException(502, f"user info failed: {e}")


@app.get("/api/soulseek/browse/{username}")
def soulseek_browse(username: str, refresh: int = Query(0)):
    """Every shared folder of a remote user — the manual-pick view: what else
    does this uploader have before queueing individual files?

    We only normalize slskd's payload to {username, directories:[…]}; slskd
    does the browsing over the peer network, so an offline user (slskd 404)
    or our OWN username (slskd cannot connect to itself) answers 5xx. That is
    an upstream failure carrying slskd's own explanation, hence 502.
    `refresh=1` bypasses the 120 s in-process cache (the modal's Refresh
    button used to re-issue the same cached answer)."""
    from server import soulseek
    if not (soulseek.is_running() or soulseek.web_up(load_config())):
        raise HTTPException(404, "slskd is not running")
    try:
        dirs = soulseek.browse(username, use_cache=not refresh) or []
    except Exception as e:
        raise HTTPException(502, f"browse failed: {e}")
    return {"username": username,
            "directories": [{"directory": str(d.get("directory") or ""),
                             "files": [{"filename": str(f.get("filename") or ""),
                                        "size": int(f.get("size") or 0)}
                                       for f in (d.get("files") or [])]}
                            for d in dirs]}


class SoulseekMessageRequest(BaseModel):
    message: str


@app.get("/api/soulseek/messages")
def soulseek_messages():
    """Conversations with their unread counts, unread first then alphabetical.

    slskd hands the list back unordered and carries no last-message preview,
    so this sort is the only ordering the UI gets (a preview line would cost
    one request per conversation)."""
    from server import soulseek
    if not (soulseek.is_running() or soulseek.web_up(load_config())):
        raise HTTPException(400, "slskd is not running")
    try:
        convs = soulseek.conversations() or []
    except Exception as e:
        raise HTTPException(502, f"conversation list failed: {e}")
    out = [{"username": str(c.get("username") or ""),
            "is_active": bool(c.get("isActive", True)),
            "unread": int(c.get("unAcknowledgedMessageCount") or 0)}
           for c in convs]
    out.sort(key=lambda c: (not c["unread"], c["username"].lower()))
    return {"ok": True, "unread": sum(c["unread"] for c in out),
            "conversations": out}


@app.get("/api/soulseek/messages/{username}")
def soulseek_conversation(username: str):
    """One conversation's messages, oldest first (slskd's own order).

    FastAPI decodes {username} on the way in (Soulseek usernames contain
    spaces) and the wrapper re-encodes it for slskd. slskd answers 404 for a
    conversation it does not know — the wrapper maps that to None, which is
    NOT an upstream failure: an existing conversation with no messages comes
    back as an empty list."""
    from server import soulseek
    if not (soulseek.is_running() or soulseek.web_up(load_config())):
        raise HTTPException(400, "slskd is not running")
    try:
        msgs = soulseek.messages(username)
    except Exception as e:
        raise HTTPException(502, f"conversation lookup failed: {e}")
    if msgs is None:
        raise HTTPException(404, f"no conversation with {username}")
    return {"ok": True, "username": username,
            "messages": [{"id": int(m.get("id") or 0),
                          "direction": str(m.get("direction") or ""),
                          "message": str(m.get("message") or ""),
                          "timestamp": str(m.get("timestamp") or ""),
                          "acknowledged": bool(m.get("isAcknowledged")),
                          "replayed": bool(m.get("wasReplayed"))}
                         for m in msgs]}


@app.post("/api/soulseek/messages/{username}")
def soulseek_send_message(username: str, req: SoulseekMessageRequest):
    """Send a private message; sending to an unknown user creates the
    conversation server-side.

    slskd answers 201 when the message went out and 200 when the peer
    blacklisted/ignored us — both are HTTP successes, so the status is what
    separates "sent" from "silently dropped" for the UI."""
    from server import soulseek
    if not (soulseek.is_running() or soulseek.web_up(load_config())):
        raise HTTPException(400, "slskd is not running")
    message = str(req.message or "").strip()
    if not message:
        raise HTTPException(400, "empty message")
    try:
        status = soulseek.send_message(username, message)
    except Exception as e:
        raise HTTPException(502, f"send failed: {e}")
    return {"ok": True, "sent": int(status) == 201}


@app.post("/api/soulseek/messages/{username}/read")
def soulseek_messages_read(username: str):
    """Acknowledge every message of a conversation (clears its unread count).

    slskd answers 404 for a conversation it does not know; the wrapper maps
    that to False and the route reports it as an ordinary `acknowledged:
    false` — a thread that vanished between two polls is not an upstream
    failure worth an error toast in the UI."""
    from server import soulseek
    if not (soulseek.is_running() or soulseek.web_up(load_config())):
        raise HTTPException(400, "slskd is not running")
    try:
        acked = soulseek.acknowledge_conversation(username)
    except Exception as e:
        raise HTTPException(502, f"acknowledge failed: {e}")
    return {"ok": True, "acknowledged": bool(acked)}


@app.delete("/api/soulseek/messages/{username}")
def soulseek_messages_close(username: str):
    """Close (hide) a conversation — slskd answers 204, which the wrapper
    turns into True. An unknown (or already closed) conversation comes back
    as False and is reported as `closed: false`, not as an error."""
    from server import soulseek
    if not (soulseek.is_running() or soulseek.web_up(load_config())):
        raise HTTPException(400, "slskd is not running")
    try:
        closed = soulseek.close_conversation(username)
    except Exception as e:
        raise HTTPException(502, f"close failed: {e}")
    return {"ok": True, "closed": bool(closed)}


class SoulseekAutoRequest(BaseModel):
    release_mbid: Optional[str] = None
    # Manual overrides: custom query templates for this run, or an exact
    # user/folder (from a manual search) to download without searching.
    queries: Optional[List[str]] = None
    username: Optional[str] = None
    target_dir: Optional[str] = None


# --------------------------------------------------------------------------- #
# Wishes — save MusicBrainz releases now, auto-fill them from Soulseek later
# --------------------------------------------------------------------------- #
class WishAddRequest(BaseModel):
    release_mbid: str
    title: Optional[str] = None
    artist: Optional[str] = None
    year: Optional[str] = None
    note: Optional[str] = None
    target_dir: Optional[str] = None
    queries: Optional[List[str]] = None


class WishUpdateRequest(BaseModel):
    note: Optional[str] = None
    target_dir: Optional[str] = None
    status: Optional[str] = None
    queries: Optional[List[str]] = None


@app.get("/api/wishes")
def wishes_list():
    """Saved releases + the background worker's status and recent log."""
    from server import wishes, wishes_worker
    return {"wishes": wishes.list_wishes(), "worker": wishes_worker.status(),
            "log": wishes.read_log(60)}


@app.post("/api/wishes")
def wishes_add(req: WishAddRequest):
    """Save a MusicBrainz release to the wishlist without downloading it.
    Missing display fields are filled from MusicBrainz."""
    from server import wishes
    title, artist, year = req.title, req.artist, req.year
    if not title:
        try:
            from server import integrations as intg
            rel = intg.release_lookup(req.release_mbid)
            title = rel.get("title")
            artist = artist or ((rel.get("artists") or [{}])[0].get("name"))
            year = year or str(rel.get("date") or "")[:4]
        except Exception:
            pass
    w = wishes.add_wish(req.release_mbid, title=title or "", artist=artist or "",
                        year=year or "", note=req.note or "",
                        target_dir=req.target_dir or "", queries=req.queries)
    return {"ok": True, "wish": w}


@app.patch("/api/wishes/{wid}")
def wishes_update(wid: int, req: WishUpdateRequest):
    from server import wishes
    w = wishes.update_wish(wid, req.model_dump(exclude_unset=True))
    if w is None:
        raise HTTPException(404, "wish not found")
    return {"ok": True, "wish": w}


@app.delete("/api/wishes/{wid}")
def wishes_delete(wid: int):
    from server import wishes
    return {"ok": wishes.delete_wish(wid)}


@app.post("/api/wishes/{wid}/search")
def wishes_search(wid: int):
    """Search Soulseek for one wish right now."""
    from server import wishes, wishes_worker
    if wishes.get_wish(wid) is None:
        raise HTTPException(404, "wish not found")
    return wishes_worker.trigger(wid)


@app.post("/api/wishes/search-all")
def wishes_search_all():
    """Run a full wishes cycle now (every due wish, newest attempted first)."""
    from server import wishes_worker
    return wishes_worker.trigger()


@app.post("/api/wishes/reconcile")
def wishes_reconcile():
    """Flip wishes already present in the library (manual download) to
    imported. Returns how many were resolved."""
    from server import wishes
    n = wishes.reconcile_with_library()
    return {"ok": True, "resolved": n}


class SoulseekTestLogRequest(BaseModel):
    username: str
    files: List[dict]  # [{filename, size}] — the .log entries of one folder


@app.get("/api/soulseek/auto")
def soulseek_auto_status():
    """Current auto-import job state (poll this from the Soulseek page)."""
    from server import soulseek_auto
    return soulseek_auto.job_state()


@app.post("/api/soulseek/auto/cancel")
def soulseek_auto_cancel():
    from server import soulseek_auto
    return {"ok": soulseek_auto.cancel()}


class SoulseekAutoConfirmRequest(BaseModel):
    accept: bool


@app.post("/api/soulseek/auto/confirm")
def soulseek_auto_confirm(req: SoulseekAutoConfirmRequest):
    """Answer the running auto-import's pending prompt.

    A job parks in state `confirm` rather than deciding for the user: it found
    only lossy copies, or it found no usable folder at all and the release
    could be handed to the wishes list instead. Both questions are answered
    here because the job waits on one event either way — which question was
    asked is `job_state()["confirm"]["reason"]`. This releases the job:
    accept=true takes the lossy copy (or adds the wish),
    accept=false ends the job without doing either."""
    from server import soulseek_auto
    ok = soulseek_auto.confirm(req.accept)
    if not ok:
        raise HTTPException(409, "no confirmation is pending")
    return {"ok": True, "accepted": bool(req.accept)}


def _resolve_release(mbid):
    """(release, release_id) for a release OR release-group MBID/URL.

    One resolution path for every caller in this file: the edition is picked
    by the auto-import policy (Official first, CD → Digital Media → … by
    medium, earliest date breaking ties, promotional/bootleg editions never
    while auto_import_avoid_promo is on). Raises 502 when MusicBrainz cannot
    resolve the id at all."""
    try:
        return intg.resolve_release(mbid)
    except Exception as e:
        raise HTTPException(502, f"MusicBrainz release lookup failed: {e}")


@app.post("/api/soulseek/auto")
def soulseek_auto_start(req: SoulseekAutoRequest):
    """Find → verify → download → audit → import a specific MusicBrainz
    release from Soulseek (see server/soulseek_auto.py for the pipeline).

    Accepts a release OR release-group MBID (a group resolves to its best
    edition by the release-choice policy)."""
    from server import soulseek_auto
    if not req.release_mbid and not (req.username and req.target_dir):
        raise HTTPException(400, "release_mbid or username+target_dir required")
    release = None
    release_mbid = req.release_mbid
    if release_mbid:
        release, release_mbid = _resolve_release(release_mbid)
        if not release_mbid:
            raise HTTPException(
                502, "MusicBrainz release not found, or it has no edition "
                     "eligible for auto-import (promotional/bootleg editions "
                     "are skipped while auto_import_avoid_promo is on)")
    r = soulseek_auto.start_job(release_mbid=release_mbid, release=release,
                                queries=req.queries, username=req.username,
                                target_dir=req.target_dir,
                                # the user asked for this release by hand: a
                                # lossy-only match is offered, never silent
                                confirm_lossy=True)
    if not r.get("ok"):
        raise HTTPException(409, r.get("error", "job refused"))
    return r


class MBAutoImportRequest(BaseModel):
    """Bulk auto-import: a release, a release group, or a whole artist.

    kind "auto" detects the entity from the ID; mode "best" queues one release
    per release group (the edition chosen by the auto-import policy), "all"
    queues every edition of a release group."""
    mbid: str
    kind: Optional[str] = None   # auto | release | release_group | artist
    mode: Optional[str] = None   # best | all


# How long the auto-import route may spend resolving an ID inline before it
# queues the item unresolved and lets the JOB do the lookup. MusicBrainz is
# throttled to 1 req/s, so a quick answer is a bonus — never a reason to hold
# the request open (a 503ing MusicBrainz used to sit here for minutes with the
# page's Auto-import button disabled, i.e. "nothing happens").
_MB_QUICK_RESOLVE_S = 3.0


def _quick(fn, seconds):
    """`fn()`'s result, or None when it did not answer within `seconds`.

    Runs on a daemon thread so a resolver stuck on a MusicBrainz outage cannot
    hold the request; the abandoned thread finishes on its own and only ever
    writes its own box (a late answer is simply not used)."""
    box = {}
    t = threading.Thread(target=lambda: box.setdefault("v", fn()), daemon=True)
    t.start()
    t.join(seconds)
    return box.get("v")


@app.post("/api/mb/auto-import")
def mb_auto_import(req: MBAutoImportRequest):
    """Queue Soulseek auto-import jobs for a release, a release group, or an
    artist's whole discography (one job at a time; the rest wait in the
    pipeline's queue). Groups already in the library are skipped.

    mode "all" only widens a RELEASE GROUP (every edition instead of the best
    one); for an artist it stays one release per release group, because
    downloading every pressing of a discography is never what "download the
    artist" means.

    The request resolves WHICH releases to queue in at most
    `_MB_QUICK_RESOLVE_S` seconds: an ID that does not resolve inside that
    budget is queued anyway (status "queued (resolving)") and the job looks it
    up — the button must come back whatever MusicBrainz is doing."""
    from server import soulseek_auto

    mbid = intg._mbid(req.mbid)
    if not mbid:
        raise HTTPException(400, "a MusicBrainz ID or URL is required")
    mode = (req.mode or "best").strip().lower()
    if mode not in ("best", "all"):
        raise HTTPException(400, "mode must be 'best' or 'all'")
    kind = (req.kind or "auto").strip().lower()
    if kind not in ("auto", "release", "release_group", "artist"):
        raise HTTPException(400, "kind must be release, release_group or artist")

    # kind "auto" needs a lookup of its own; the job does it when this does
    # not answer in time, so an unresolved kind is queued, never dropped.
    resolved = _quick(lambda: intg.auto_import_targets(mbid, kind, mode),
                      _MB_QUICK_RESOLVE_S)
    deferred = resolved is None
    targets, skipped = ([{"mbid": mbid, "title": ""}], []) if deferred else resolved

    items = []
    for t in targets:
        # kind/mode ride along ONLY for a deferred item: the ones the quick
        # pass resolved are concrete releases, and the job must not resolve
        # them a second time (a release id is not a release group).
        depth = soulseek_auto.enqueue(release_mbid=t["mbid"],
                                      kind=kind if deferred else None,
                                      mode=mode if deferred else None)
        items.append({
            "mbid": t["mbid"], "title": t.get("title") or "",
            "status": ("queued (resolving)" if deferred
                       else "queued" if depth else "running"),
        })
    return {"queued": len(items), "items": items, "skipped": skipped}


@app.post("/api/soulseek/test-log")
def soulseek_test_log(req: SoulseekTestLogRequest):
    """Download ONLY a folder's .log file(s), grade them with Logchecker,
    then clean up — a quality preview before committing to the album.

    Returns {logs: [{file, score, checksum, detail}], ok} where ok means
    every log reached the configured grade_log_score_threshold (100)."""
    from server import soulseek
    from mlo.discs import score_disc_log

    if not (soulseek.is_running() or soulseek.web_up()):
        raise HTTPException(400, "slskd is not running — start it first")
    logs = [f for f in req.files
            if str(f.get("filename") or "").lower().endswith(".log")]
    if not logs:
        raise HTTPException(400, "this folder has no .log files")

    cfg = load_config()
    ddir = soulseek.download_dir(cfg)
    # req.username is unvalidated client input used as a path segment; without
    # this check a username of "../.." aims the empty-folder cleanup below at
    # directories anywhere up the tree.
    user = str(req.username or "").strip()
    if (not user or user != req.username or user != os.path.basename(user)
            or user in (".", "..")):
        raise HTTPException(400, "invalid username")
    upath = os.path.join(ddir, user)
    if not _in_music_folder(upath, ddir):
        raise HTTPException(400, "username escapes the download dir")
    soulseek.enqueue_download(req.username,
                              [{"filename": f["filename"], "size": f.get("size") or 0}
                               for f in logs])
    from server.soulseek_auto import _wait_for_files, _remote_rel
    got = _wait_for_files(soulseek, ddir, req.username, logs, timeout_s=180)

    threshold = int(cfg.get("grade_log_score_threshold", 100) or 100)
    out = []
    for f in logs:
        local = got.get(f["filename"])
        if not local:
            out.append({"file": os.path.basename(f["filename"]), "score": None,
                        "checksum": None, "detail": "download timed out"})
            continue
        score = score_disc_log(local)
        from mlo.discs import check_log_checksum
        state, detail = check_log_checksum(local)
        out.append({"file": os.path.basename(f["filename"]), "score": score,
                    "checksum": state, "detail": detail})
        try:
            os.remove(local)
        except OSError:
            pass
    # drop now-empty user folders left by the log test
    try:
        for root, dirs, files in os.walk(upath, topdown=False):
            if not os.listdir(root):
                os.rmdir(root)
    except OSError:
        pass
    scored = [e for e in out if e["score"] is not None]
    ok = bool(scored) and all((e["score"] or 0) >= threshold for e in scored)
    return {"ok": ok, "threshold": threshold, "logs": out}


@app.post("/api/soulseek/shares/refresh")
def soulseek_shares_refresh():
    """Restart slskd so the share index picks up added/removed/moved files."""
    from server import soulseek
    if not (soulseek.is_running() or soulseek.web_up(load_config())):
        return {"ok": False, "message": "slskd is not running"}
    ok = soulseek.restart()
    return {"ok": ok, "message": "share index refreshed" if ok else "restart failed"}


class SoulseekLoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/soulseek/login")
def soulseek_login(req: SoulseekLoginRequest):
    """Save Soulseek credentials, restart slskd with them, and wait for the
    network login. The Soulseek server registers brand-new usernames on
    first login, so the same call covers signing in AND creating an
    account; failure means the credentials were rejected (wrong password
    for an existing account)."""
    from server import soulseek

    cfg = load_config()
    _ours, _who, conflict = soulseek.instance_owner(cfg)
    if conflict:
        return {"ok": False, "logged_in": False,
                "message": f"{conflict} — only one slskd can run at a time, so "
                           f"stop the other app's slskd first"}
    username = req.username.strip()
    if not username or not req.password:
        raise HTTPException(400, "username and password are required")
    same = (username == str(cfg.get("soulseek_username") or "").strip()
            and req.password == str(cfg.get("soulseek_password") or ""))
    if not same:
        cfg["soulseek_username"] = username
        cfg["soulseek_password"] = req.password
        save_config(cfg)

    # When the submitted credentials MATCH the saved ones and slskd is
    # already up, do NOT restart: restarting aborts slskd's own reconnect
    # attempts and resets the Soulseek server's cooldown — which is what
    # made stop → start → login loops stop working. Just wait for login.
    if not same or not (soulseek.is_running() or soulseek.web_up(cfg)):
        soulseek.restart()
    deadline = time.time() + 40.0
    logged_in = False
    while time.time() < deadline:
        try:
            state = soulseek.server_state() or {}
        except Exception:
            state = {}
        logged_in = bool(state.get("isLoggedIn"))
        if logged_in:
            break
        time.sleep(1.0)
    if logged_in:
        _refresh_slskd_shares_soon()
        return {"ok": True, "logged_in": True,
                "message": f"Logged in to Soulseek as {username}"}
    # the daemon's verdict beats any guess this endpoint could make
    detail = soulseek.login_error()
    return {"ok": False, "logged_in": False,
            "message": detail or (
                "The Soulseek server did not accept these credentials "
                "— if this username already exists, the password may "
                "be wrong; otherwise try again in a minute (new "
                "accounts can take a moment to register)")}


def _refresh_slskd_shares_soon():
    """Library changed (organize/import/remove) — refresh the slskd share
    index in the background so the network always sees the current paths."""
    def _worker():
        try:
            from server import soulseek
            if soulseek.is_running() or soulseek.web_up(load_config()):
                time.sleep(3.0)  # debounce bursts of organize calls
                soulseek.restart()
        except Exception:
            import traceback
            traceback.print_exc()
    threading.Thread(target=_worker, name="mlo-share-refresh", daemon=True).start()


@app.get("/api/track/download")
def track_download(path: str = Query(...)):
    """Serve the original, untouched audio file as a browser download."""
    p = os.path.normpath(path)
    if not _in_music_folder(p, _music_folder()):
        raise HTTPException(400, "path is outside the music folder")
    if not os.path.isfile(p):
        raise HTTPException(404, "file not found")
    return FileResponse(p, media_type="application/octet-stream",
                        filename=os.path.basename(p))


_EXPORT_CODECS = {
    # codec: (ext, lossless, ffmpeg args template)
    "flac": (".flac", True, ["-c:a", "flac", "-compression_level", "{level}"]),
    "alac": (".m4a", True, ["-c:a", "alac"]),
    "wav": (".wav", True, ["-c:a", "pcm_s16le"]),
    "mp3": (".mp3", False, ["-c:a", "libmp3lame", "-b:a", "{bitrate}k"]),
    "aac": (".m4a", False, ["-c:a", "aac", "-b:a", "{bitrate}k"]),
    "opus": (".opus", False, ["-c:a", "libopus", "-b:a", "{bitrate}k", "-vbr", "on"]),
}


@app.get("/api/track/export")
def track_export(path: str = Query(...), codec: str = Query("flac"),
                 bitrate: int = Query(320), level: int = Query(5)):
    """Transcode a library track to the requested codec/bitrate and serve it
    as a download. Lossless (flac/alac/wav) ignores the bitrate; lossy
    codecs take 64–500 kbps."""
    codec = codec.lower().strip()
    if codec not in _EXPORT_CODECS:
        raise HTTPException(400, f"unsupported codec: {codec}")
    ext, _lossless, args_tpl = _EXPORT_CODECS[codec]
    p = os.path.normpath(path)
    if not _in_music_folder(p, _music_folder()):
        raise HTTPException(400, "path is outside the music folder")
    if not os.path.isfile(p):
        raise HTTPException(404, "file not found")

    from mlo.tools import detect_all_tools
    ffmpeg = (detect_all_tools().get("ffmpeg") or {}).get("ffmpeg_exe")
    if not ffmpeg:
        raise HTTPException(500, "ffmpeg is not installed")

    bitrate = max(64, min(500, int(bitrate)))
    level = max(0, min(8, int(level)))
    args = [a.format(bitrate=bitrate, level=level) for a in args_tpl]

    tmpdir = tempfile.mkdtemp(prefix="mlo_export_")
    out = os.path.join(tmpdir, os.path.splitext(os.path.basename(p))[0] + ext)
    from mlo.subproc import run_tool
    import subprocess
    try:
        proc = run_tool([ffmpeg, "-y", "-v", "error", "-i", p] + args +
                        ["-map_metadata", "0", out],
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                        text=True, errors="replace", timeout=1800)
        if proc.returncode != 0 or not os.path.isfile(out) or os.path.getsize(out) == 0:
            raise HTTPException(500, f"transcode failed: {(proc.stderr or '')[:200]}")
    except HTTPException:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    except Exception as e:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(500, f"transcode failed: {e}")

    from starlette.background import BackgroundTask

    def _cleanup():
        try:
            os.remove(out)
            os.rmdir(tmpdir)
        except OSError:
            pass

    return FileResponse(out, media_type="application/octet-stream",
                        filename=os.path.basename(out),
                        background=BackgroundTask(_cleanup))


class GenreImportRequest(BaseModel):
    paths: List[str]  # audio files (or one album dir)
    count: Optional[int] = None  # defaults to mb_genre_count from settings


def _genre_files(paths):
    """Audio files under the given paths (each may be a file or an album
    folder). Raises 400 when the paths hold no audio."""
    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, fs in os.walk(p):
                files += [os.path.join(root, f) for f in sorted(fs)
                          if is_audio_file(f)]
        elif is_audio_file(p):
            files.append(p)
    if not files:
        raise HTTPException(400, "no audio files found")
    return files


def _tag_paths_guard(paths):
    """Containment guard (same as /api/tags/bulk and /api/mb/assign): genre
    routes write tags, so paths outside the music folder are refused."""
    folder = _music_folder()
    for p in paths:
        if not _in_music_folder(p, folder):
            raise HTTPException(400, f"path outside music folder: {p}")


def _write_album_genres(files, names, per_track=None, limit=None):
    """Write GENRE per track: the track's own genres first (MusicBrainz
    recording genres, when the release has them), then the album-level merged
    list, deduped Title-Case and capped. Returns the files written."""
    from mlo.audio import AudioFile
    from server import soulseek_auto

    per_track = per_track or {}
    updated = 0
    for p in files:
        try:
            af = AudioFile(p)
            if af.audio is None:
                continue
            merged = []
            for name in list(per_track.get(soulseek_auto._parse_trackno(p)) or []) + list(names):
                text = str(name).strip().title()
                if text and text.lower() not in {g.lower() for g in merged}:
                    merged.append(text)
            if limit:
                merged = merged[:limit]
            if merged and af.set_tag("GENRE", "; ".join(merged)):
                updated += 1
        except Exception:
            continue
    if updated:
        tagcache.invalidate_all()
        mbresolve.invalidate()
    return updated


@app.post("/api/mb/genres")
def mb_genres_import(req: GenreImportRequest):
    """Import genres from MUSICBRAINZ onto the given tracks.

    The release (MUSICBRAINZ_ALBUMID) or release group (RELEASEGROUPID) on the
    first track identifies the entity; per-track recording genres win over the
    release's list, and the count follows Settings → Import (`mb_genre_count`,
    default 3). Other sources are deliberately not consulted here — use
    /api/genres/import for the full chain."""
    from mlo.audio import AudioFile

    cfg = load_config()
    n = max(1, min(10, int(req.count or cfg.get("mb_genre_count", 3) or 3)))
    _tag_paths_guard(req.paths)
    files = _genre_files(req.paths)

    probe = AudioFile(files[0])
    mbid = str(probe.get_tag("MUSICBRAINZ_ALBUMID") or "").strip()
    rgid = str(probe.get_tag("MUSICBRAINZ_RELEASEGROUPID") or "").strip()
    if not mbid and not rgid:
        raise HTTPException(400, "no MusicBrainz album/release-group ID on the track — import & link first")

    release = None
    if mbid:
        try:
            release = intg.release_lookup(mbid)
        except Exception as e:
            raise HTTPException(502, f"MusicBrainz lookup failed: {e}")

    artist = str(probe.get_tag("ALBUMARTIST") or probe.get_tag("ARTIST") or "").strip()
    album = str(probe.get_tag("ALBUM") or "").strip()
    chain = intg.genre_chain(
        artist=artist, album=album, limit=n, cfg=cfg, sources=["musicbrainz"],
        release=release or {"id": "", "release_group_id": rgid, "genres": [],
                            "artists": [], "media": []},
    )
    names = chain.get("genres") or []
    track_genres = {}
    for t in (release or {}).get("media") or []:
        g = t.get("genres") or []
        if g:
            track_genres[(int(t.get("disc") or 1), int(t.get("position") or 0))] = g

    updated = _write_album_genres(files, names, track_genres, limit=n)
    return {"ok": True, "updated": updated, "genres": names,
            "per_source": chain.get("per_source") or {},
            "per_track": bool(track_genres)}


class GenreChainImportRequest(BaseModel):
    paths: List[str]  # audio files (or one album dir)
    limit: Optional[int] = None  # defaults to mb_genre_count from settings


@app.post("/api/genres/import")
def genres_import(req: GenreChainImportRequest):
    """Import GENRE from EVERY configured genre source, per track.

    The chain runs in the order of `genre_sources` (RateYourMusic → Soulseek
    signals → Discogs/Last.fm/TheAudioDB → Deezer/iTunes → MusicBrainz),
    merges what each source answers, dedupes case-insensitively, Title-Cases
    the names and caps them at `limit`/`mb_genre_count` (default 3). Sources
    that cannot answer are reported in `notes` (a blocked RYM, a Discogs or
    Last.fm source without its token/key) — nothing is filled in from a guess.
    MusicBrainz recording genres refine each track when the album names a
    release. Returns {updated, per_source, notes, genres}."""
    from mlo.audio import AudioFile

    cfg = load_config()
    _tag_paths_guard(req.paths)
    files = _genre_files(req.paths)

    probe = AudioFile(files[0])
    mbid = str(probe.get_tag("MUSICBRAINZ_ALBUMID") or "").strip()
    artist = str(probe.get_tag("ALBUMARTIST") or probe.get_tag("ARTIST") or "").strip()
    album = str(probe.get_tag("ALBUM") or "").strip()
    release = None
    if mbid:
        try:
            release = intg.release_lookup(mbid)
        except Exception:
            release = None

    try:
        limit = int(req.limit) if req.limit else None
    except (TypeError, ValueError):
        raise HTTPException(400, "limit must be a number")
    chain = intg.genre_chain(artist=artist, album=album, release=release,
                             limit=limit, cfg=cfg, files=files)
    names = chain.get("genres") or []
    # The chain already merged every track's own genres ahead of the
    # release-wide ones and capped them per track — `_write_album_genres`, the
    # ONE writer, is what applies the cap to the file.
    per_track = chain.get("per_track") or {}

    cap = limit or int(cfg.get("mb_genre_count") or 3)
    updated = _write_album_genres(files, names, per_track, limit=cap)
    return {"updated": updated, "genres": names,
            "per_source": chain.get("per_source") or {},
            "notes": chain.get("notes") or {},
            "per_track": bool(per_track),
            # Where each file's genres came from, in the order that
            # contributed, plus the tier that answered (track/album/artist).
            "sources": chain.get("sources") or {},
            "levels": chain.get("levels") or {}}


@app.get("/api/genres/facets")
def genres_facets():
    """GENRE tags across the library, counted and bucketed.

    `genres` is every distinct genre with the number of tracks carrying it
    (descending), which is the "all genres" list; `categories` groups those
    names into the fixed buckets the UI filters by (Metal, Rock, Electronic,
    Hip-Hop, Jazz, Classical, Folk, Soul & Funk, Pop, Other) — a genre lands in
    exactly one bucket, so the counts stay honest."""
    from server import library as lib_mod

    cfg = load_config()
    try:
        lib = lib_mod.build_library(cfg)
    except Exception as e:
        raise HTTPException(502, f"library scan failed: {e}")
    counts = {}
    for artist in lib.get("artists", []):
        for alb in artist.get("albums", []):
            for tr in alb.get("tracks", []):
                raw = str((tr.get("tags") or {}).get("GENRE") or "")
                for name in re.split(r"[;,]", raw):
                    text = name.strip().title()
                    if text:
                        counts[text] = counts.get(text, 0) + 1
    genres = [{"name": n, "count": c}
              for n, c in sorted(counts.items(), key=lambda x: (-x[1], x[0].lower()))]
    buckets = {}
    for row in genres:
        buckets.setdefault(intg.genre_category(row["name"]), []).append(row["name"])
    order = [c for c, _keys in intg.GENRE_CATEGORIES] + [intg.OTHER_CATEGORY]
    categories = [{"name": name, "genres": buckets[name]}
                  for name in order if buckets.get(name)]
    return {"genres": genres, "categories": categories}


class AdvisoryFetchRequest(BaseModel):
    paths: Optional[List[str]] = None           # audio files or album folders
    release_mbid: Optional[str] = None          # a MusicBrainz release id/URL

@app.post("/api/mb/advisory/fetch")
def mb_advisory_fetch(req: AdvisoryFetchRequest):
    """Resolve ITUNESADVISORY (0/1) for an album or a MusicBrainz release.

    Tracks are identified by their ISRC tag — or by the MusicBrainz recording
    ID an import already stamped, whose ISRCs MusicBrainz supplies — and rated
    by EVERY applicable source in one pass (Deezer and Spotify by ISRC, Apple's
    explicit-edition album route, Apple's exact-title song search), merged by
    `integrations.merge_advisory`: 1 when any source states explicit, else 0 —
    an unstated advisory is written as 0, and `answers` is what shows whether
    any source actually spoke. A `cleaned` Apple entry states nothing and is
    never written, an existing valid 0/1/2 is never overwritten, and a value
    equal to the merged one is not rewritten.

    Returns {updated, values, sources, answers}: `values` maps the file path
    (paths mode) or "disc:position" (release mode) to the merged rating,
    `sources` maps the same keys to the provider that stated it, and `answers`
    maps them to what every source said ({source: 0|1})."""
    from server import imports as imports_mod

    if not req.paths and not req.release_mbid:
        raise HTTPException(400, "paths or release_mbid required")
    values = {}
    sources = {}
    answers = {}
    updated = 0
    if req.release_mbid:
        rid = intg._mbid(req.release_mbid)
        if not rid:
            raise HTTPException(400, "invalid MusicBrainz release ID")
        try:
            values.update(intg.release_advisories(rid, sources=sources,
                                                 answers=answers))
        except Exception as e:
            raise HTTPException(502, f"MusicBrainz release lookup failed: {e}")
    if req.paths:
        _tag_paths_guard(req.paths)
        result = imports_mod.fetch_advisories(req.paths, load_config())
        updated = int(result.get("updated") or 0)
        values.update(result.get("values") or {})
        sources.update(result.get("sources") or {})
        answers.update(result.get("answers") or {})
    return {"updated": updated, "values": values, "sources": sources,
            "answers": answers}


class InstrumentalFetchRequest(BaseModel):
    paths: Optional[List[str]] = None           # audio files or album folders


@app.post("/api/instrumental/fetch")
def instrumental_fetch(req: InstrumentalFetchRequest):
    """Resolve and write INSTRUMENTAL (0/1) for these albums / tracks.

    Every source is cross-referenced by `server.instrumental`: LRCLIB's own
    `instrumental` flag by artist/title/album/duration, Spotify
    audio-features when credentials are configured, the file's own name
    ("... (Instrumental)") and lyrics evidence. They are merged by one rule —
    any source saying instrumental → 1, else any source saying not
    instrumental → 0, else NO value and no write (absence of evidence is never
    recorded as a value, and a variant title is never read as the track). A
    file that already carries 0/1 is left alone: the user's edit wins.

    Returns {updated, values, evidence}: `values` maps the file path to the
    merged value and `evidence` maps it to what each source said
    ({source: 0|1}) — that is what the UI shows next to the tag."""
    from server import imports as imports_mod

    if not req.paths:
        raise HTTPException(400, "paths required")
    _tag_paths_guard(req.paths)
    result = imports_mod.fetch_instrumentals(req.paths, load_config())
    return {"updated": int(result.get("updated") or 0),
            "values": result.get("values") or {},
            "evidence": result.get("evidence") or {}}


def _metadata_album_identity(album_dir, artist=""):
    """(artist, album) for a metadata request, from the folder's own tags."""
    tracks = _scan_album_tracks(album_dir) or []
    tags = (tracks[0].get("tags") if tracks else None) or {}
    album = str(tags.get("ALBUM") or os.path.basename(os.path.normpath(album_dir))).strip()
    return (artist or str(tags.get("ALBUMARTIST") or tags.get("ARTIST") or "").strip(),
            album)


@app.get("/api/metadata/candidates")
def metadata_candidates_route(artist: str = Query(""), album_path: str = Query("")):
    """Image / description candidates for an artist or an album folder.

    Read-only: nothing is written. The list is composed by
    `server.discovery.metadata_candidates` from the reliable providers (Deezer
    photo, TheAudioDB, Apple artwork, Wikipedia) plus the configured image
    chain's own pick, and the first description each configured description
    source can give. With `metadata_review` on, an album also carries the
    candidates the import chain STAGED for it (`staged`). The UI's pick is
    written through POST /api/metadata/apply."""
    from server import discovery, imports as imports_mod

    cfg = load_config()
    if not artist and not album_path:
        raise HTTPException(400, "artist or album_path required")
    album_dir = ""
    album = ""
    if album_path:
        album_dir = os.path.normpath(album_path)
        if not os.path.isdir(album_dir):
            raise HTTPException(404, "album not found")
        if not _in_music_folder(album_dir, _music_folder(cfg)):
            raise HTTPException(400, "album outside music folder")
        artist, album = _metadata_album_identity(album_dir, artist)
    try:
        candidates = discovery.metadata_candidates(artist, album, cfg=cfg)
    except Exception as e:
        raise HTTPException(502, f"metadata lookup failed: {e}")
    candidates["artist"] = artist
    candidates["album"] = album
    candidates["staged"] = imports_mod.staged_metadata(album_dir, cfg) if album_dir else {}
    return candidates


class MetadataApplyRequest(BaseModel):
    """Apply one metadata choice (the user's pick, or an automatic one)."""
    kind: str                                   # artist_image | artist_description | album_description
    artist: Optional[str] = None                # artist name or folder path
    album_path: Optional[str] = None            # album folder (album_description)
    image_url: Optional[str] = None             # artist_image source
    description: Optional[str] = None           # artist_description / album_description text


@app.post("/api/metadata/apply")
def metadata_apply(req: MetadataApplyRequest):
    """Save one metadata choice: artist image (from a candidate URL), artist
    description or album description (supplied text, or the best candidate
    when the body carries none). Clears the album's staged review entry so a
    reviewed album leaves the queue."""
    from mlo import artistdata
    from server import discovery, imports as imports_mod

    cfg = load_config()
    kind = (req.kind or "").strip().lower()
    album_dir = ""
    if req.album_path:
        album_dir = os.path.normpath(req.album_path)
        if not os.path.isdir(album_dir):
            raise HTTPException(404, "album not found")
        if not _in_music_folder(album_dir, _music_folder(cfg)):
            raise HTTPException(400, "album outside music folder")

    if kind == "artist_image":
        artist = (req.artist or "").strip()
        if not artist:
            raise HTTPException(400, "artist is required")
        folder = _metadata_artist_folder(artist, cfg)
        url = (req.image_url or "").strip()
        source = "manual"
        label = None
        if not url:
            hit = discovery.artist_image(_metadata_artist_name(folder, artist), cfg=cfg)
            if not hit or not hit.get("url"):
                raise HTTPException(404, "no artist image found in any configured source")
            url, source, label = hit["url"], hit.get("source") or "auto", hit.get("label")
        try:
            # A provider URL goes through the art cache: its per-host headers
            # and its fallback are what make a CDN that refuses the app still
            # yield the artist image (see `_cover_url_bytes`).
            data, _ctype = _cover_url_bytes(url, artist)
        except Exception as e:
            raise HTTPException(502, f"image download failed: {e}")
        path = artistdata.save_image(folder, data, cfg, source=source,
                                     source_url=url, kind="artist", label=label)
        if not path:
            raise HTTPException(400, "not a usable image")
        saved = path

    elif kind in ("artist_description", "album_description"):
        text = str(req.description or "").strip()
        source = source_url = title = None
        if kind == "artist_description":
            artist = (req.artist or "").strip()
            if not artist:
                raise HTTPException(400, "artist is required")
            folder = _metadata_artist_folder(artist, cfg)
            name = _metadata_artist_name(folder, artist)
            if not text:
                found = discovery.artist_description(name, cfg=cfg)
                if not found:
                    raise HTTPException(404, "no description found in any configured source")
                text, source = found["text"], found.get("source")
                source_url, title = found.get("source_url"), found.get("title")
            saved = artistdata.write_description(folder, text, cfg=cfg, source=source,
                                                 source_url=source_url, kind="artist")
        else:
            if not album_dir:
                raise HTTPException(400, "album_path is required")
            artist, album = _metadata_album_identity(album_dir, (req.artist or "").strip())
            if not text:
                found = discovery.album_description(artist, album, cfg=cfg)
                if not found:
                    raise HTTPException(404, "no description found in any configured source")
                text, source = found["text"], found.get("source")
                source_url, title = found.get("source_url"), found.get("title")
            saved = artistdata.write_description(album_dir, text, cfg=cfg, source=source,
                                                 source_url=source_url, kind="album")
        if not saved:
            raise HTTPException(400, "description is empty")
        if source:
            target = folder if kind == "artist_description" else album_dir
            artistdata.write_provenance(target, {
                "description_source": source,
                "description_source_url": source_url,
                "description_title": title}, kind="artist" if kind == "artist_description" else "album",
                cfg=cfg)
    else:
        raise HTTPException(400, "kind must be artist_image, artist_description or album_description")

    if album_dir:
        imports_mod.stage_metadata(album_dir, None, cfg)
    tagcache.invalidate_all()
    return {"ok": True, "saved": str(saved).replace("\\", "/")}


def _metadata_artist_folder(artist, cfg):
    """An artist folder from a path (inside the music folder) or a name —
    the same resolution the artist-page routes use."""
    from server.api_discovery import _artist_folder
    return _artist_folder(artist, cfg)


def _metadata_artist_name(folder, artist=""):
    """The artist's *name* for provider lookups, given a library folder."""
    from server.api_discovery import _artist_name
    return _artist_name(folder, artist)


def _rewrite_track_covers(old_root, new_root, renames):
    """Follow the per-track cover map through a move.

    Both keys (tracks) and values (images) are album filenames: they are
    rewritten through *renames* (old basename -> new basename) and an entry
    whose track or image is not in the new album root is dropped, rather than
    left pointing at a file that no longer exists."""
    mapping = load_track_covers(new_root) or load_track_covers(old_root)
    if not mapping:
        return
    try:
        present = {f.lower() for f in os.listdir(new_root)
                   if os.path.isfile(os.path.join(new_root, f))}
    except OSError:
        return
    lowered = {k.lower(): v for k, v in renames.items()}
    out = {}
    for track, image in mapping.items():
        track = lowered.get(track.lower(), track)
        image = lowered.get(image.lower(), image)
        if track.lower() in present and image.lower() in present:
            out[track] = image
    save_track_covers(new_root, out)


@app.post("/api/organize")
def organize(req: OrganizeRequest):
    """Rename/move albums according to the configured naming script.

    For each album: evaluate the script per track, move the audio files,
    move same-stem sidecars (.lrc/.cue/...) next to their track, move
    leftover album files (cover art etc.) to the new album root, and prune
    emptied folders. Nothing leaves the music folder.
    """
    from server.naming import DEFAULT_NAMING_SCRIPT, eval_script, track_variables

    cfg = load_config()
    folder = cfg.get("music_folder") or ""
    if not folder or not os.path.isdir(folder):
        raise HTTPException(400, "music_folder not set or not found")
    script = (cfg.get("naming_script") or "").strip() or DEFAULT_NAMING_SCRIPT
    shorter = bool(cfg.get("short_folder_names", False))

    results = []
    for album_dir in req.paths:
        p = os.path.normpath(mbresolve.resolve_album(album_dir) or album_dir)
        if not os.path.isdir(p):
            results.append({"path": album_dir, "error": "album not found"})
            continue
        if not _in_music_folder(p, folder):
            results.append({"path": album_dir, "error": "album outside music folder"})
            continue
        tracks = _scan_album_tracks(p)
        if not tracks:
            results.append({"path": album_dir, "error": "no audio files found"})
            continue

        # album-level tags from the first tagged track
        meta_tags = next((t["tags"] for t in tracks if t["tags"].get("TITLE") or t["tags"].get("ALBUM")), tracks[0]["tags"])
        release_type = meta_tags.get("RELEASETYPE")
        if not release_type and meta_tags.get("MUSICBRAINZ_ALBUMID"):
            try:
                rel = intg.release_lookup(meta_tags["MUSICBRAINZ_ALBUMID"])
                release_type = rel.get("release_type")
            except Exception:
                release_type = None

        moves = []  # (src, dst)
        dst_dirs = []  # target dir of every track (moved or in place)
        errors = []
        for t in tracks:
            try:
                vars_ = track_variables(t["tags"], release_type=release_type)
                rel = eval_script(script, vars_, shorter_ids=shorter)
            except Exception as e:
                errors.append(f"{t['file']}: script error: {e}")
                continue
            if not rel:
                errors.append(f"{t['file']}: script evaluated to empty path")
                continue
            # The script defines the path without the file extension — the
            # source extension is re-appended LOWERCASE (beets keeps the
            # source ext; this app additionally normalizes its case).
            rel += os.path.splitext(t["path"])[1].lower()
            # The library itself is <music folder>/Artists (contract A): the
            # script only names the path INSIDE it, so every album lands under
            # Artists/ and the music-folder root stays the Soulseek share root.
            dst = os.path.normpath(os.path.join(library_root(folder), rel))
            if not _in_music_folder(dst, folder):
                errors.append(f"{t['file']}: destination outside music folder")
                continue
            src = os.path.normpath(t["path"])
            if os.path.normcase(src) == os.path.normcase(dst):
                if src != dst:
                    # case-only difference (TOXICITY.FLAC -> toxicity.flac or
                    # wrong capitalization): still queued — os.rename handles
                    # same-file case renames on case-insensitive filesystems.
                    moves.append((src, dst))
                dst_dirs.append(os.path.dirname(dst))
                continue
            stem, ext = os.path.splitext(dst)
            n = 2
            while os.path.exists(dst) and os.path.normcase(dst) != os.path.normcase(src):
                dst = f"{stem} ({n}){ext}"
                n += 1
            moves.append((src, dst))
            dst_dirs.append(os.path.dirname(dst))

        # The album root is the common parent of EVERY track's target dir.
        # Deriving it from the first move alone collapses the whole album
        # into a disc subfolder when a single track (a hidden track like
        # "Arto" in its own "1-14 Aerials" disc folder) happens to move.
        # Even when every track is already in place the sweep below still
        # runs: stray files in subfolders must come along to the album root.
        try:
            new_root = os.path.commonpath(dst_dirs)
        except ValueError:
            new_root = os.path.dirname(moves[0][1]) if moves else p

        # companion sidecars: same stem as an audio file, different extension.
        # Matches exact stems ("04 - Psycho.jpg") AND extended stems
        # ("04 - Psycho.cover.jpg" / "04 - Psycho - front.jpg").
        sidecar_moves = []
        for src, dst in moves:
            sdir, sname = os.path.split(src)
            sstem = os.path.splitext(sname)[0].lower()
            ddir, dname = os.path.split(dst)
            dstem = os.path.splitext(dname)[0]
            try:
                for f in os.listdir(sdir):
                    fpath = os.path.join(sdir, f)
                    fstem, fext = os.path.splitext(f)
                    if f.lower() == sname.lower() or os.path.isdir(fpath):
                        continue
                    if fext.lower() not in (".flac", ".mp3", ".m4a", ".mp4", ".ogg", ".opus", ".wav", ".aac"):
                        fstem_l = fstem.lower()
                        if fstem_l == sstem or fstem_l.startswith(sstem + "."):
                            target = os.path.join(ddir, dstem + fext.lower())
                            if fpath == target:
                                continue  # already in place (exact)
                            sidecar_moves.append((fpath, target))
            except OSError:
                pass

        if req.dry_run:
            results.append({
                "path": album_dir,
                "dry_run": True,
                "album_root": new_root.replace("\\", "/"),
                "moves": [{"from": s.replace("\\", "/"), "to": d.replace("\\", "/")} for s, d in moves],
                "sidecars": [{"from": s.replace("\\", "/"), "to": d.replace("\\", "/")} for s, d in sidecar_moves],
                "errors": errors,
            })
            continue

        moved = 0
        for src, dst in moves + sidecar_moves:
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                # Case-only renames (capitalization / extension case) go
                # through move_path too: it uses os.replace, which renames a
                # file onto its own name modulo case.
                if not move_path(src, dst):
                    errors.append(f"{os.path.basename(src)}: a file inside is "
                                  f"still in use — stop playback and retry")
                    continue
                moved += 1
            except Exception as e:
                errors.append(f"{os.path.basename(src)}: {e}")

        # leftovers (cover art, logs, anything else) -> new album root.
        # Audio files are never leftovers — their placement is owned by the
        # moves list above (a compliant track that stays put must not be
        # swept into a disc subfolder or deduped onto itself).
        leftovers = 0
        for root, dirs, files in os.walk(p):
            for f in list(files):
                if is_audio_file(f):
                    continue
                fpath = os.path.join(root, f)
                if not os.path.exists(fpath):
                    continue
                # extensions are normalized to lowercase on every move
                fbase, fext = os.path.splitext(f)
                dst = os.path.join(new_root, fbase + fext.lower())
                # Already sitting in the album root: moving it onto itself
                # must be skipped, or the dedupe below renames it " (2)".
                if os.path.normcase(os.path.abspath(fpath)) == os.path.normcase(os.path.abspath(dst)):
                    continue
                n = 2
                while os.path.exists(dst) and os.path.normcase(os.path.abspath(dst)) != os.path.normcase(os.path.abspath(fpath)):
                    dst = os.path.join(new_root, f"{fbase} ({n}){fext.lower()}")
                    n += 1
                try:
                    os.makedirs(new_root, exist_ok=True)
                    if not move_path(fpath, dst):
                        errors.append(f"{f}: a file inside is still in use — "
                                      f"stop playback and retry")
                        continue
                    leftovers += 1
                except Exception as e:
                    errors.append(f"{f}: {e}")

        # Per-track cover manifest: its keys and values are filenames in the
        # album folder, so a sidecar renamed with its track (or a track that
        # moved) has to be followed here too. The manifest file itself was
        # swept to the new album root by the leftovers pass above.
        try:
            cover_renames = {os.path.basename(s): os.path.basename(d)
                             for s, d in moves + sidecar_moves}
            _rewrite_track_covers(p, new_root, cover_renames)
        except Exception as e:
            errors.append(f"cover manifest: {e}")

        # prune emptied folders: first any empty subfolders left inside the
        # old album (deepest first — a swept "scans/" folder must not keep
        # the album dir alive), then the album chain up to music_folder
        pruned = 0
        for root, dirs, _files in os.walk(p, topdown=False):
            for d in dirs:
                full = os.path.join(root, d)
                try:
                    if not os.listdir(full):
                        os.rmdir(full)
                        pruned += 1
                except OSError:
                    pass
        cursor = p
        while os.path.abspath(cursor).lower() != os.path.abspath(folder).lower():
            try:
                if os.listdir(cursor):
                    break
                os.rmdir(cursor)
                pruned += 1
                cursor = os.path.dirname(cursor)
            except OSError:
                break

        # Post-organize cue maintenance: renaming audio underneath cue
        # sheets leaves stale FILE references, and album-named cues moved
        # by the leftovers pass keep names the CD-N grader rejects. Re-run
        # the same evidence-based engine the cue script uses.
        notes = []
        try:
            from mlo.discs import fix_cue_filenames, rename_cues_for_discs
            for old, new in rename_cues_for_discs(new_root, config=cfg):
                notes.append(f"cue renamed: {old} -> {new}")
            notes.extend(fix_cue_filenames(new_root, config=cfg))
        except Exception as e:
            errors.append(f"cue maintenance: {e}")

        results.append({
            "path": album_dir,
            "ok": True,
            "moved": moved,
            "leftovers": leftovers,
            "album_root": new_root.replace("\\", "/"),
            "pruned": pruned,
            "notes": notes,
            "errors": errors,
        })
    tagcache.invalidate_all()
    mbresolve.invalidate()
    if any(r.get("moved") for r in results):
        _refresh_slskd_shares_soon()
    return {"results": results}


@app.post("/api/import/upload")
async def import_upload(
    target_dir: str = Query(...),
    files: List[UploadFile] = File(...),
):
    """Upload files into a new album directory under the library (Artists).

    Filenames may contain relative subpaths (e.g. "CD1/01 - Intro.flac")
    so whole album folders keep their internal structure. Path traversal
    and absolute paths are rejected.

    `target_dir` is free text from the wizard's album-name field, so only its
    BASENAME is used: `_in_music_folder` alone would accept a name like
    "../Escape" (it resolves to <music>/Escape — inside the music folder, but
    outside Artists/ where the library actually lives).
    """
    cfg = load_config()
    folder = cfg.get("music_folder") or ""
    if not folder or not os.path.isdir(folder):
        raise HTTPException(400, "music_folder not set or not found")
    safe = re_safe_filename(os.path.basename(target_dir)) or "Imported"
    target = os.path.normpath(os.path.join(library_root(folder), safe))
    if not _in_music_folder(target, folder):
        raise HTTPException(400, "target outside music folder")
    os.makedirs(target, exist_ok=True)
    saved = []
    for f in files:
        name = (f.filename or "file").replace("\\", "/")
        parts = [p for p in name.split("/") if p and p not in (".", "..")]
        if not parts:
            continue
        # reject absolute/escaping paths
        if os.path.isabs(name) or ".." in name.split("/"):
            raise HTTPException(400, f"unsafe filename: {name!r}")
        dest = os.path.join(target, *parts)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        data = await f.read()
        # Large albums must not stall the event loop (it would freeze the
        # progress websocket and every other request mid-upload).
        await asyncio.to_thread(_write_upload_bytes, dest, data)
        saved.append(dest.replace("\\", "/"))
    return {"ok": True, "saved": saved, "album_path": target.replace("\\", "/")}


def _write_upload_bytes(dest: str, data: bytes) -> None:
    with open(dest, "wb") as out:
        out.write(data)


@app.post("/api/import/scan")
def import_scan(path: str = Query(...)):
    """Recursively list a folder's files with relative paths (drag-drop or a
    typed/known folder path — there is no in-app folder picker).

    The folder may live anywhere — the follow-up ingest step moves it into
    the library.
    """
    p = os.path.normpath(path)
    if not os.path.isdir(p):
        raise HTTPException(404, "folder not found")
    out = []
    for root, dirs, files in os.walk(p):
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, p).replace("\\", "/")
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            out.append({"relPath": rel, "size": size})
    out.sort(key=lambda x: x["relPath"].lower())
    return {"root": p.replace("\\", "/"), "files": out}


@app.post("/api/import/ingest")
def import_ingest(source: str = Query(...), target: str = Query(...)):
    """Move an album folder into the library. Same volume it is one rename;
    across devices it is a verified copy followed by the source's removal —
    never a blind copytree + rmtree."""
    cfg = load_config()
    folder = cfg.get("music_folder") or ""
    if not folder or not os.path.isdir(folder):
        raise HTTPException(400, "music_folder not set or not found")
    src = os.path.normpath(source)
    if not os.path.isdir(src):
        raise HTTPException(404, "source folder not found")
    name = re_safe_filename(os.path.basename(target or os.path.basename(src)))
    if not name:
        raise HTTPException(400, "invalid target name")
    dest = os.path.normpath(os.path.join(library_root(folder), name))
    if not _in_music_folder(dest, folder):
        raise HTTPException(400, "target outside music folder")
    if os.path.normcase(os.path.abspath(dest)) == os.path.normcase(os.path.abspath(src)):
        return {"ok": True, "path": dest.replace("\\", "/")}
    n = 2
    while os.path.exists(dest):
        dest = os.path.normpath(os.path.join(library_root(folder), f"{name} ({n})"))
        n += 1
    if not move_path(src, dest):
        # move_path retried every lock/sharing violation and refuses to copy
        # blindly: whatever failed, the source is still complete.
        raise HTTPException(
            500,
            f"could not import {name} — a file inside it is still in use "
            f"(stop playback and retry)")
    tagcache.invalidate_all()
    mbresolve.invalidate()
    return {"ok": True, "path": dest.replace("\\", "/")}


@app.post("/api/import/commit")
def import_commit(req: ImportCommit):
    """Store MB/RYM links on every track of a freshly imported album.

    target_dir: album folder name under the library (Artists).
    """
    cfg = load_config()
    folder = cfg.get("music_folder") or ""
    if not folder or not os.path.isdir(folder):
        raise HTTPException(400, "music_folder not set or not found")
    target = os.path.normpath(req.target_dir)
    if not os.path.isabs(target):
        target = os.path.normpath(os.path.join(library_root(folder), req.target_dir))
    if not _in_music_folder(target, folder):
        raise HTTPException(400, "target outside music folder")
    if not os.path.isdir(target):
        raise HTTPException(404, "album not found")
    from mlo.audio import AudioFile
    changes = {}
    for root, _dirs, files in os.walk(target):
        for f in files:
            if not is_audio_file(f):
                continue
            changes[os.path.join(root, f).replace("\\", "/")] = {}
    mb = intg._mbid(req.mb_link)
    if mb:
        for p in changes:
            changes[p]["MUSICBRAINZ_ALBUMID"] = mb
    rym = intg.parse_rym_album_url(req.rym_link)
    if rym:
        for p in changes:
            changes[p]["RATEYOURMUSIC_ALBUM"] = rym
    errors = []
    for p, tag_map in changes.items():
        if not tag_map:
            continue
        af = AudioFile(os.path.normpath(p))
        if af.audio is None:
            errors.append(f"{p}: {af.error or 'unreadable'}")
            continue
        for k, v in tag_map.items():
            if not af.set_tag(k, v):
                errors.append(f"{p} {k}: {af.error}")
        tagcache.invalidate_path(os.path.normpath(p))
    if errors:
        raise HTTPException(500, "; ".join(errors))
    return {"ok": True, "changed": len(changes)}


@app.post("/api/import/expected")
def import_expected(req: ImportExpected):
    """Record the release's full tracklist on the album folder.

    This is what makes a PARTIAL import legible: the album page diffs the
    files on disk against this list and greys out the ones that never came
    in. Sending an empty `tracks` clears the manifest again (a full import
    leaves nothing behind)."""
    from mlo.paths import save_expected_tracks
    cfg = load_config()
    folder = cfg.get("music_folder") or ""
    if not folder or not os.path.isdir(folder):
        raise HTTPException(400, "music_folder not set or not found")
    target = os.path.normpath(req.target_dir)
    if not os.path.isabs(target):
        target = os.path.normpath(os.path.join(library_root(folder), req.target_dir))
    if not _in_music_folder(target, folder):
        raise HTTPException(400, "target outside music folder")
    if not os.path.isdir(target):
        raise HTTPException(404, "album not found")
    if not save_expected_tracks(target, req.release_id, req.tracks):
        raise HTTPException(500, "could not write the release tracklist")
    tagcache.invalidate_all()
    return {"ok": True, "tracks": len(req.tracks or [])}


# --------------------------------------------------------------------------- #
# .mlo/downloads — slskd's staging area
# --------------------------------------------------------------------------- #
def _downloads_dir(folder):
    """<music>/.mlo/downloads, or None when no music folder is set."""
    if not folder:
        return None
    try:
        return downloads_dir(folder)
    except Exception:
        return None


def _downloads_name_error(name, root):
    """Why `name` may not be acted on inside the downloads dir, or None.

    Names travel as basenames over the API, so anything that is not a single
    plain path segment — or that resolves (symlinks included) outside the
    directory — is refused before a byte is touched."""
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        return "not a valid entry name"
    try:
        real = os.path.realpath(os.path.join(root, name))
    except (OSError, ValueError):
        return "unresolvable path"
    if os.path.dirname(real) != root:
        return "outside the downloads folder"
    return None


def _downloads_scan(path):
    """(files, bytes, audio, images) under *path*, skipping hidden app dirs."""
    from mlo.paths import LIB_AUDIO_EXTS, LIB_VIDEO_EXTS, IMAGE_EXTS
    files = size = audio = images = 0
    for root, dirs, names in os.walk(path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in names:
            files += 1
            try:
                size += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
            ext = os.path.splitext(f)[1].lower()
            if ext in LIB_AUDIO_EXTS or ext in LIB_VIDEO_EXTS:
                audio += 1
            elif ext in IMAGE_EXTS:
                images += 1
    return files, size, audio, images


def _downloads_entry(root, name):
    """One downloads entry: size, file counts and whether it is an album.

    `album` is what the page keys the Import button off — a folder holding
    audio. `partial` marks slskd's own in-flight leftovers, which must never
    look like something safe to import or delete by accident."""
    from mlo.paths import LIB_AUDIO_EXTS, LIB_VIDEO_EXTS, IMAGE_EXTS
    p = os.path.join(root, name)
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        return None
    # In-flight transfers and scratch files are dot/underscore-prefixed or
    # carry a partial suffix; they are not results to act on.
    partial = name.startswith((".", "_")) or \
        name.lower().endswith((".part", ".tmp", ".!ut", ".downloading"))
    if os.path.isdir(p) and not os.path.islink(p):
        files, size, audio, images = _downloads_scan(p)
        return {"name": name, "dir": True, "bytes": size, "files": files,
                "audio": audio, "images": images, "album": audio > 0,
                "partial": partial, "_mtime": mtime}
    try:
        size = os.path.getsize(p)
    except OSError:
        size = 0
    ext = os.path.splitext(name)[1].lower()
    return {"name": name, "dir": False, "bytes": size, "files": 1,
            "audio": 1 if (ext in LIB_AUDIO_EXTS or ext in LIB_VIDEO_EXTS) else 0,
            "images": 1 if ext in IMAGE_EXTS else 0,
            "album": False, "partial": partial, "_mtime": mtime}


@app.get("/api/downloads")
def downloads_list():
    """Contents of <music_folder>/.mlo/downloads, newest first.

    This is slskd's staging area: everything it pulls down lands here and
    stays until it is imported into the library or deleted. Enough is reported
    per entry (size, file counts, whether it holds audio) for the page to
    offer Import only where it is meaningful."""
    folder = load_config().get("music_folder") or ""
    ddir = _downloads_dir(folder)
    out = {"folder": (ddir or "").replace("\\", "/"), "exists": False,
           "count": 0, "bytes": 0, "entries": [],
           "music_folder": folder.replace("\\", "/")}
    if not ddir or not os.path.isdir(ddir):
        return out
    root = os.path.realpath(ddir)
    try:
        names = os.listdir(ddir)
    except OSError:
        return out
    entries = [e for e in (_downloads_entry(root, n) for n in names) if e]
    entries.sort(key=lambda e: e["_mtime"], reverse=True)
    for e in entries:
        del e["_mtime"]
    out.update(exists=True, count=len(entries), entries=entries,
               bytes=sum(e["bytes"] for e in entries))
    return out


@app.post("/api/downloads/delete")
def downloads_delete(req: DownloadsDelete = DownloadsDelete()):
    """Permanently delete downloads entries by basename. Unknown and refused
    names land in `failed`; nothing else is an error."""
    import shutil
    folder = load_config().get("music_folder") or ""
    ddir = _downloads_dir(folder)
    if not ddir or not os.path.isdir(ddir):
        raise HTTPException(404, "downloads folder not found")
    root = os.path.realpath(ddir)
    deleted, failed, freed = [], [], 0
    for name in req.names:
        err = _downloads_name_error(name, root)
        p = os.path.join(ddir, name) if err is None else ""
        if err is None and not os.path.lexists(p):
            err = "not found in downloads"
        if err is None:
            try:
                # Size first: once rmtree has run the bytes are unrecoverable.
                if os.path.islink(p):
                    size = 0
                    os.remove(p)
                elif os.path.isdir(p):
                    size = _dir_stats(p)[1]
                    shutil.rmtree(p)
                else:
                    size = os.path.getsize(p)
                    os.remove(p)
                freed += size
            except OSError as e:
                err = str(e) or "delete failed"
        if err:
            failed.append({"name": name, "error": err})
        else:
            deleted.append(name)
    if deleted:
        tagcache.invalidate_all()
        _refresh_slskd_shares_soon()
    return {"deleted": deleted, "failed": failed, "freed": freed}


@app.post("/api/downloads/import")
def downloads_import(req: DownloadsImport = DownloadsImport()):
    """Move downloads entries into the library as albums, clearing them from
    the staging area.

    Each entry keeps its name (deduplicated with " (2)" etc.) and is verified
    to sit inside the downloads dir first. A per-entry failure lands in
    `failed`; the rest of the batch still moves."""
    folder = load_config().get("music_folder") or ""
    ddir = _downloads_dir(folder)
    if not ddir or not os.path.isdir(ddir):
        raise HTTPException(404, "downloads folder not found")
    if not folder or not os.path.isdir(folder):
        raise HTTPException(400, "music_folder not set or not found")
    root = os.path.realpath(ddir)
    lib = library_root(folder)
    moved, failed = [], []
    for name in req.names:
        err = _downloads_name_error(name, root)
        src = os.path.join(ddir, name) if err is None else ""
        if err is None and not os.path.lexists(src):
            err = "not found in downloads"
        if err is None:
            safe = re_safe_filename(os.path.basename(name)) or "Download"
            dest = os.path.normpath(os.path.join(lib, safe))
            if not _in_music_folder(dest, folder):
                err = "target outside music folder"
            else:
                n = 2
                while err is None and os.path.exists(dest):
                    dest = os.path.normpath(os.path.join(lib, f"{safe} ({n})"))
                    n += 1
                if err is None and not move_path(src, dest):
                    # move_path retried every lock/sharing violation and
                    # refuses to copy blindly: the source stays complete.
                    err = ("could not import — a file inside it is still in "
                           "use (stop playback and retry)")
                elif err is None:
                    moved.append({"name": name, "path": dest.replace("\\", "/")})
        if err:
            failed.append({"name": name, "error": str(err)})
    if moved:
        tagcache.invalidate_all()
        mbresolve.invalidate()
        _refresh_slskd_shares_soon()
    return {"moved": moved, "failed": failed}


# --------------------------------------------------------------------------- #
# Library layout — is the music folder shaped the way the app expects?
# --------------------------------------------------------------------------- #
# The canonical library is <music>/Artists/<Artist>/<Album>/<files>. Everything
# else that holds audio, or that sits where no audio belongs, is reported here.
# This is a READ-ONLY report: it says what is wrong and where, and never moves
# anything on its own.
_LAYOUT_ALBUM_SIDECARS = {".lrc", ".cue", ".log", ".accurip"}
# Folders allowed directly in the music folder. `.mlo*` covers the app's own
# state dirs; everything else is reported.
_LAYOUT_ROOT_ALLOWED = {".mlo"}
# Disc folders are the one nesting the app creates and understands.
_LAYOUT_DISC_RE = re.compile(r"^(cd|disc|disk)\s*\d+$", re.I)


def _layout_is_audio(name):
    from mlo.paths import LIB_AUDIO_EXTS, LIB_VIDEO_EXTS
    ext = os.path.splitext(name)[1].lower()
    return ext in LIB_AUDIO_EXTS or ext in LIB_VIDEO_EXTS


def _layout_list(d):
    try:
        return os.listdir(d)
    except OSError:
        return []


def _layout_issue(kind, path, folder, detail, hint):
    """One report row. `path` is always music-folder-relative for display, so
    the UI never has to know the machine's absolute layout."""
    try:
        rel = os.path.relpath(path, folder)
    except ValueError:
        rel = path
    return {"kind": kind, "path": rel.replace("\\", "/"),
            "abs": path.replace("\\", "/"), "detail": detail, "hint": hint}


def _layout_counts(issues):
    counts = {}
    for i in issues:
        counts[i["kind"]] = counts.get(i["kind"], 0) + 1
    return counts


def _layout_has_audio(d):
    """Whether any audio sits directly in *d* or one/two levels down (the only
    nesting the layout uses: disc folders inside an album)."""
    for root, dirs, names in os.walk(d):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        if root.count(os.sep) - d.count(os.sep) > 2:
            dirs[:] = []
            continue
        if any(_layout_is_audio(f) for f in names):
            return True
    return False


def _layout_case_only(expected, actual):
    """Whether two names are the SAME name in different letter case.

    Case-only is the only kind of name mismatch the layout scanner reports.
    Anything else — a different album, a different title — is a naming
    problem and belongs to the grader's PATH check; repeating it here would
    only give the user two rows for one thing, and a guess about paths is
    what the scanner must never make.
    """
    return (bool(expected) and expected != actual
            and expected.casefold() == actual.casefold())


def _layout_case_issues(artist, album, album_dir, folder, script, seen):
    """`wrong_case` rows for one album folder, or [] when there is nothing
    trustworthy to compare against.

    The expected names come from the naming script the ORGANIZER applies —
    the same `naming_script` the grader evaluates — run over the album's own
    tags. That is the whole reason this is reportable at all: Windows is
    case-insensitive, so "abbey road" and "Abbey Road" open the same folder
    and nothing in the normal file API will ever admit the difference. What
    it does NOT do is lie about the spelling: `os.listdir` returns the name
    exactly as it is STORED on disk, and that stored spelling is what every
    comparison below reads. So a case-only mismatch shows up here precisely
    because the scanner looks at the raw directory entries instead of asking
    the OS whether two paths are "the same file" — it would always say yes.

    Tags are read from ONE audio file per album (the first one, through the
    tag cache), because the artist and album segments are album properties
    and the script's last segment is only compared against the file those
    tags came from. Any other file in the album is left alone: its expected
    name needs its own tags, and without them a comparison would be a guess.
    Unreadable tags, an empty tag dict or a script that evaluates to nothing
    all end in silence — a false "wrong case" here would push the user into
    renaming music to a name that is not actually correct.

    Cost ceiling: this reads tags for one file per album on every full scan.
    If scanning a large library ever gets slow, the cheap win is a prefilter
    — only read tags for albums whose folder or file names do not already
    contain the tag spelling — or a cached layout snapshot; both are more
    machinery than a read-only report currently earns.

    `seen` holds the absolute paths this scan already reported, so an artist
    folder shared by ten albums produces one row, not ten.
    """
    from mlo.naming import eval_script, track_variables

    src = None
    for f in _layout_list(album_dir):
        if _layout_is_audio(f):
            src = f
            break
    if src is None:
        return []
    try:
        tags = tagcache.read_track(os.path.join(album_dir, src))[0] or {}
    except Exception:
        return []
    release_type = str(tags.get("RELEASETYPE") or "").strip() or None
    expected = eval_script(script, track_variables(tags, release_type=release_type))
    if not expected:
        return []
    segs = [s for s in expected.replace("\\", "/").split("/") if s]
    if not segs:
        return []

    rows = []

    def add(path, expected_name, actual_name, what):
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            return
        seen.add(key)
        rows.append(_layout_issue(
            "wrong_case", path, folder,
            "%s \u201c%s\u201d differs from the naming script\u2019s \u201c%s\u201d "
            "in letter case only" % (what, actual_name, expected_name),
            "run Organize \u2014 it rewrites this to the script\u2019s exact "
            "casing (nothing is renamed by this scan)"))

    # The script's leading segments are directories — segment 0 is the artist
    # folder, segment 1 the album folder — and the last one is the file name
    # WITHOUT its extension (beets-style: the extension belongs to the file,
    # so it is appended from the file on disk, exactly as the grader does).
    # A shorter script simply names fewer things, and zip then compares only
    # what it does name.
    for seg, actual_name, path, what in zip(
            segs[:-1],
            (artist, album),
            (os.path.dirname(album_dir), album_dir),
            ("artist folder", "album folder")):
        if _layout_case_only(seg, actual_name):
            add(path, seg, actual_name, what)
    expected_file = segs[-1] + os.path.splitext(src)[1]
    if _layout_case_only(expected_file, src):
        add(os.path.join(album_dir, src), expected_file, src, "file")
    return rows


@app.get("/api/library/layout")
def library_layout():
    """Scan the whole music folder for misplaced files, unexpected folders and
    names spelled in the wrong letter case.

    Read-only. Checks the music-folder root, <music>/Artists, each artist
    folder, each album folder and the tree's depth — reporting every place the
    canonical `<music>/Artists/<Artist>/<Album>/<files>` shape is not met.

    What the app itself stores counts as expected, never as a stray: the
    album's description.txt (mlo.paths.ALBUM_SIDECAR_NAMES) next to the
    cover art, and inside an artist folder its artist.jpg / artist.png and
    description.txt (only audio with no album folder is reported there)."""
    from mlo.naming import DEFAULT_NAMING_SCRIPT
    from mlo.paths import ALBUM_SIDECAR_NAMES, IMAGE_EXTS
    cfg = load_config()
    folder = cfg.get("music_folder") or ""
    # The canonical spellings the `wrong_case` check compares against: the
    # same script (same fallback) the organizer renames with and the grader
    # grades against.
    naming_script = (str(cfg.get("naming_script") or "").strip()
                     or DEFAULT_NAMING_SCRIPT)
    out = {"folder": folder.replace("\\", "/"), "artists_dir": "",
           "exists": False, "issues": [], "counts": {}, "total": 0,
           "albums": 0, "artists": 0, "audio_files": 0}
    if not folder or not os.path.isdir(folder):
        return out
    lib = library_root(folder)
    out["exists"] = True
    out["artists_dir"] = (lib or "").replace("\\", "/")
    issues = []
    # Paths already reported as wrong_case — one artist folder serves all of
    # its albums, and the user does not need that row ten times.
    case_seen = set()

    # ---- 1. the music-folder root -----------------------------------------
    # Only Artists/ and the app's own .mlo state dirs belong here. A loose
    # audio file at the root is the classic "dropped it in the wrong place",
    # and a foreign folder is either a manual rip dump or a stray copy.
    for name in _layout_list(folder):
        p = os.path.join(folder, name)
        if os.path.isdir(p):
            if name in _LAYOUT_ROOT_ALLOWED or name.startswith(".mlo"):
                continue
            if lib and os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(lib)):
                continue
            holds = _layout_has_audio(p)
            issues.append(_layout_issue(
                "unexpected_folder", p, folder,
                "folder in the music folder root%s" % (" holding audio" if holds else ""),
                "the library lives in Artists/ — move anything real into "
                "Artists/<Artist>/<Album>/"))
        elif _layout_is_audio(name):
            issues.append(_layout_issue(
                "audio_at_root", p, folder, "audio file in the music folder root",
                "move it into Artists/<Artist>/<Album>/ (or import it) so "
                "grading and the organizer can see it"))
        elif name.startswith(".mlo_"):
            issues.append(_layout_issue(
                "legacy_state_file", p, folder,
                "leftover from the old .mlo_data layout",
                "safe to delete once the migration has been confirmed"))

    if not lib or not os.path.isdir(lib):
        out.update(issues=issues, counts=_layout_counts(issues), total=len(issues))
        return out

    # ---- 2. <music>/Artists ------------------------------------------------
    artist_names = _layout_list(lib)
    for name in artist_names:
        p = os.path.join(lib, name)
        if not os.path.isdir(p):
            if _layout_is_audio(name):
                issues.append(_layout_issue(
                    "audio_in_artists", p, folder,
                    "audio file directly in Artists/ (no artist or album folder)",
                    "move it into Artists/<Artist>/<Album>/"))
            else:
                issues.append(_layout_issue(
                    "stray_in_artists", p, folder,
                    "non-audio file directly in Artists/",
                    "delete it, or move it into the album it belongs to"))
            continue
        if name.startswith("."):
            issues.append(_layout_issue(
                "hidden_folder", p, folder, "hidden folder inside Artists/",
                "hidden folders are not library content — move or delete it"))
            continue

        # ---- 3. <music>/Artists/<Artist> ----------------------------------
        for an in _layout_list(p):
            ap = os.path.join(p, an)
            if not os.path.isdir(ap):
                if _layout_is_audio(an):
                    issues.append(_layout_issue(
                        "audio_in_artist", ap, folder,
                        "audio file directly in the artist folder \u201c%s\u201d "
                        "(no album folder)" % name,
                        "give it an album folder: Artists/<Artist>/<Album>/"))
                # Any other file in an artist folder is the artist's own
                # content (artist.jpg / artist.png / description.txt written
                # by mlo.artistdata) — expected, so nothing to report.
                continue
            out["albums"] += 1
            if not _layout_has_audio(ap):
                issues.append(_layout_issue(
                    "empty_album", ap, folder,
                    "album folder \u201c%s / %s\u201d holds no audio" % (name, an),
                    "remove it, or fill it \u2014 an empty album grades as an error"))
            # Letter-case drift: the folder or file is in the right PLACE but
            # spells its name the way the filesystem let somebody type it,
            # not the way the naming script spells it. Reported next to the
            # shape problems because from here it is the same kind of answer:
            # "this is not the canonical library yet".
            issues.extend(_layout_case_issues(
                name, an, ap, folder, naming_script, case_seen))

            # ---- 4. inside an album: strays and unexpected subfolders ------
            for f in _layout_list(ap):
                fp = os.path.join(ap, f)
                if os.path.isdir(fp):
                    if not _LAYOUT_DISC_RE.match(f):
                        issues.append(_layout_issue(
                            "unexpected_subfolder", fp, folder,
                            "folder \u201c%s\u201d inside album \u201c%s / %s\u201d"
                            % (f, name, an),
                            "only disc folders (CD1, Disc 2, \u2026) belong "
                            "inside an album"))
                    continue
                ext = os.path.splitext(f)[1].lower()
                if _layout_is_audio(f):
                    out["audio_files"] += 1
                elif ext and ext in _LAYOUT_ALBUM_SIDECARS:
                    pass                      # .lrc/.cue/.log/.accurip: expected
                elif ext in IMAGE_EXTS:
                    pass                      # cover art: expected
                elif f.lower() in ALBUM_SIDECAR_NAMES:
                    pass                      # album description.txt: expected
                elif f.startswith("."):
                    pass                      # the app's own manifests
                else:
                    issues.append(_layout_issue(
                        "stray_file", fp, folder,
                        "file \u201c%s\u201d is not audio, artwork or a known "
                        "sidecar" % f,
                        "delete it if it is junk (nfo/db/txt) — it is dead "
                        "weight in the library"))

    out["artists"] = sum(1 for n in artist_names
                         if os.path.isdir(os.path.join(lib, n)) and not n.startswith("."))
    out.update(issues=issues, counts=_layout_counts(issues), total=len(issues))
    return out


# --------------------------------------------------------------------------- #
# WebSocket + static
# --------------------------------------------------------------------------- #
@app.websocket("/ws/progress")
async def ws_progress(ws: WebSocket):
    await ws.accept()
    with _progress_lock:
        progress_clients.add(ws)
    try:
        while True:
            try:
                # receive() detects dead peers (ping does not), so stale
                # sockets get pruned instead of accumulating forever.
                await asyncio.wait_for(ws.receive(), timeout=30)
            except asyncio.TimeoutError:
                try:
                    await ws.send_json({"type": "ping"})
                except Exception:
                    break
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        with _progress_lock:
            progress_clients.discard(ws)


WEB_DIST = ROOT / "web" / "dist"
if WEB_DIST.is_dir():
    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        """Serve the built SPA: real files as-is, everything else falls back
        to index.html so client-side routes (/settings, /album/...) work."""
        if full_path.startswith(("api/", "ws")):
            raise HTTPException(404)
        # full_path is attacker-controlled: a ".." segment would otherwise read
        # any file on disk (e.g. /../../server/tagcache.py, or the config file
        # holding the Soulseek credentials).
        if ".." in full_path.replace("\\", "/").split("/"):
            raise HTTPException(404)
        file = WEB_DIST / full_path
        # Belt and braces: even without a ".." segment (absolute/UNC input on
        # Windows) the resolved target must still sit inside the built SPA.
        try:
            resolved = file.resolve()
        except (OSError, ValueError):
            resolved = None
        if (resolved is not None and resolved.is_file()
                and _in_music_folder(resolved, WEB_DIST.resolve())):
            # hashed asset filenames change per build; etag revalidation is enough
            return FileResponse(resolved)
        if full_path.startswith("assets/"):
            # a missing hashed bundle must 404 — the fallback would answer with
            # index.html, i.e. HTML handed to the browser as JavaScript/CSS.
            raise HTTPException(404)
        # index.html must revalidate so an app update is picked up immediately
        return FileResponse(WEB_DIST / "index.html", headers={"Cache-Control": "no-cache"})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server.main:app", host="127.0.0.1", port=8000, reload=True)