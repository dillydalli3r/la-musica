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
import traceback
import asyncio
import filecmp
import inspect
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

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

from mlo import __version__ as APP_VERSION
from mlo import load_config, save_config
from mlo.config import DEFAULT_CONFIG
from mlo import archives as archives_mod
from mlo import cover_choice
from mlo import layout as mlo_layout
from mlo import stats as stats_mod
from mlo import eq as eq_mod

from server import library as lib_mod
from server import mbresolve
from server import playlists as pl_mod
from server import integrations as intg
from server import tagcache
from server import exporter
from server import exportconfigs
from server import api_discovery
from server import api_discover
from server import api_imports
from server import api_lyrics
from server import api_media
from server import api_auth
from server import api_jobs
from server import api_query
from server import api_ratings
from server import api_plays
from server import api_recommend
from server import api_watch
from server import api_queue
from server import api_add
from server import api_choice
from server import api_stack
from server import script_menu
from server import api_storage
from server import api_streaming
from server import api_soulseek
from server import api_youtube
from server import api_rym
from server import api_cookies
from server import auth as auth_mod
from server import events as events_mod
from server import job_locks
from server import discovery
from server import artcache
from server import version as version_mod
from mlo.naming import sanitize_segment
from mlo.paths import (AUDIO_EXTS, SKIP_DIRS, clear_track_covers, downloads_dir,
                       is_video_file, library_root, load_track_covers, mlo_root,
                       move_path, save_track_covers, set_track_covers, trash_dir,
                       trash_path)
from mlo.subproc import tool_path
from server import imports as imports_svc

# Captured at startup — worker threads use run_coroutine_threadsafe against
# this loop to relay script progress over the WebSocket (get_event_loop()
# from a worker thread is unreliable and deprecated).
_MAIN_LOOP = None


@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _MAIN_LOOP
    _MAIN_LOOP = asyncio.get_running_loop()
    # Whatever an interrupted run left behind — the auto-updater stops this
    # container at any moment, so this is the normal case, not an edge one:
    # sweep the temp files our own writers abandoned, reconcile framework
    # albums left half-filled, and report interrupted jobs. Runs BEFORE any
    # worker or request can add more work (server.interrupt_recovery).
    try:
        from server import interrupt_recovery
        interrupt_recovery.startup_recovery(load_config())
        # …and make the NEXT signal (the auto-updater's SIGTERM, or the
        # container's stop) set the shutdown flag immediately instead of at the
        # teardown uvicorn only reaches once the running chain is over, so a
        # run stops at a script boundary and says which scripts it did not run.
        interrupt_recovery.install_signal_grace()
    except Exception as e:
        print(f"[mlo] interrupted-run recovery failed: {e}")
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
    # Artist watches: periodically looks for NEW releases from followed artists
    # (never their back catalogue — see server/artist_watch).
    try:
        from server import artist_watch_worker
        artist_watch_worker.start()
    except Exception as e:
        print(f"[mlo] artist watch worker failed to start: {e}")
    # Soulseek status watcher: pushes a frame the moment the login state,
    # daemon state or port conflict changes (see _soulseek_watch).
    threading.Thread(target=_soulseek_watch, daemon=True).start()
    # Soulseek UPLOADS watcher: announces a peer starting to download from us
    # (see _soulseek_uploads_watch) — nothing else in the app watches uploads.
    threading.Thread(target=_soulseek_uploads_watch, daemon=True).start()
    # Soulseek TRANSFER watcher: pushes the Downloads tab's own rows the moment
    # slskd's byte counts move (see _soulseek_transfers_watch) — the page used
    # to draw those bars from a 3 s poll of its own.
    threading.Thread(target=_soulseek_transfers_watch, daemon=True).start()
    # Downloads the user started from the Soulseek page: the album imports
    # itself (and runs its chain) once it lands, so asking for a download is
    # the only press it needs (see _soulseek_page_downloads_watch).
    threading.Thread(target=_soulseek_page_downloads_watch, daemon=True).start()
    # Size caps: prunes the download/staging/trash caches down to their
    # configured ceilings (see server/cache_caps).
    try:
        from server import cache_caps
        cache_caps.start()
    except Exception as e:
        print(f"[mlo] cache caps worker failed to start: {e}")
    yield
    # Stop taking new work first (the two workers above are the app's own
    # source of new jobs), then the honest part: wait — bounded — for whatever
    # is still running, say what it is waiting for, and record what had to be
    # abandoned so the next start can reconcile it. Every write is atomic, so
    # the worst case for a job that does not finish in time is a repeated
    # album, never a torn file. GRACE_SECONDS stays below the container's stop
    # grace (docker-compose.yml) so this ordered abort always beats SIGKILL.
    try:
        from server import wishes_worker
        wishes_worker.stop()
    except Exception:
        pass
    try:
        from server import artist_watch_worker
        artist_watch_worker.stop()
    except Exception:
        pass
    try:
        from server import cache_caps
        cache_caps.stop()
    except Exception:
        pass
    try:
        from server import interrupt_recovery
        interrupt_recovery.shutdown()
    except Exception as e:
        print(f"[mlo] graceful shutdown failed: {e}")


app = FastAPI(title="la musica API", version=APP_VERSION, lifespan=_lifespan)

# Docker/bootstrap: MLO_MUSIC_FOLDER env seeds music_folder when unset.
_MLO_ENV_FOLDER = os.environ.get("MLO_MUSIC_FOLDER")
if _MLO_ENV_FOLDER:
    _cfg = load_config()
    if not _cfg.get("music_folder"):
        _cfg["music_folder"] = os.path.normpath(_MLO_ENV_FOLDER)
        save_config(_cfg)

# The bind address is what decides whether the login gate applies
# (server/auth.gate_required), and a launcher can bind somewhere the config
# does not mention — the Docker image runs `uvicorn --host 0.0.0.0` while a
# fresh config still says 127.0.0.1, which would leave the published port
# ungated. MLO_SERVER_HOST/MLO_SERVER_PORT therefore seed the same keys the
# launchers pass, so the config, the launcher and the gate can never disagree.
_MLO_ENV_HOST = os.environ.get("MLO_SERVER_HOST")
_MLO_ENV_PORT = os.environ.get("MLO_SERVER_PORT")
if _MLO_ENV_HOST or _MLO_ENV_PORT:
    _cfg = load_config()
    changed = False
    if _MLO_ENV_HOST and str(_cfg.get("server_host") or "") != _MLO_ENV_HOST:
        _cfg["server_host"] = _MLO_ENV_HOST
        changed = True
    if _MLO_ENV_PORT:
        try:
            port = int(_MLO_ENV_PORT)
        except ValueError:
            port = None
        if port and int(_cfg.get("server_port") or 0) != port:
            _cfg["server_port"] = port
            changed = True
    if changed:
        save_config(_cfg)

# The Soulseek listen port, from the same place and for a harder reason:
# docker-compose.yml PUBLISHES this port, and the publish is written as
# `${MLO_SOULSEEK_LISTEN_PORT:-50000}:${MLO_SOULSEEK_LISTEN_PORT:-50000}` —
# one number on both sides, which only stays true if the daemon inside the
# container listens on the number the host publishes. slskd takes its port
# from the config the app writes (`soulseek_listen_port`), so an environment
# that published 51000 while the app still said 50000 gave a share that peers
# could see the size of and never connect to — the exact "it just doesn't
# work" shape of a forward pointing at a closed port. The variable therefore
# SEEDS the config key, like MLO_MUSIC_FOLDER and MLO_SERVER_PORT above it: an
# install in a container cannot be made to disagree with its own publish line.
# (Setting the key in the UI is refused while the pin is present — see
# `/api/config`'s pin check.)
_MLO_ENV_SLSK_PORT = os.environ.get("MLO_SOULSEEK_LISTEN_PORT")
if _MLO_ENV_SLSK_PORT:
    try:
        _slsk_port = int(_MLO_ENV_SLSK_PORT)
    except ValueError:
        _slsk_port = None
    if _slsk_port and 1024 <= _slsk_port <= 65535:
        _cfg = load_config()
        if int(_cfg.get("soulseek_listen_port") or 0) != _slsk_port:
            _cfg["soulseek_listen_port"] = _slsk_port
            save_config(_cfg)
    else:
        print("[mlo] MLO_SOULSEEK_LISTEN_PORT is not a usable port number "
              "(1024-65535) — ignoring it")

# The login gate (v3). Registered BEFORE the CORS middleware on purpose:
# Starlette applies the most recently added middleware outermost, and CORS
# must be the outer one — a 401 has to carry Access-Control-Allow-Origin or
# the desktop/mobile shell (whose origin is not the API's) sees an opaque
# network failure instead of "sign in", and a preflight OPTIONS must be
# answered by CORS rather than rejected by the gate. See server/auth.py for
# what the gate is, and server/api_auth.py for the endpoints that open it.
#
# Handlers that scope their data by user take `request: Request = None` and
# ask `auth_mod.current_user(request)`: FastAPI always injects the request over
# HTTP, and a direct call (a test, or another module's helper) answers with the
# default scope instead of a TypeError.
@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    if request.method == "OPTIONS" or auth_mod.is_public(request.url.path):
        return await call_next(request)
    state = auth_mod.cached_state()
    if state is None:
        # Cache expired: the read touches config.json, so it goes off the
        # event loop. A media seek issues many range requests back to back
        # and only the first of a burst pays this.
        state = await asyncio.to_thread(auth_mod.current_state)
    # The gate is for CLIENT connections. A request from this machine — the
    # host's own browser, a desktop shell, or the host of the container this
    # server runs in, which reaches a published port through the Docker bridge
    # gateway — never has to sign in; see auth.local_addresses. `auth_mode:
    # required` overrides that and asks everyone.
    if not auth_mod.requires_login(request, state):
        return await call_next(request)
    if not state.get("has_password"):
        return JSONResponse(
            {"detail": "this server has no password yet — finish setup to continue",
             "needs_setup": True},
            status_code=428,
        )
    token = auth_mod.token_from_request(request)
    if token and await asyncio.to_thread(auth_mod.valid_session, token):
        return await call_next(request)
    return JSONResponse(
        {"detail": "sign in required", "needs_login": True},
        status_code=401,
    )


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

# The media routes answer CORS for ANY origin, and it has to be here rather than
# in the allow-list above.
#
# A phone's <audio> element is fetched with `crossorigin="anonymous"` (the
# playback visualizer, the equalizer and ReplayGain all read that stream through
# a WebAudio graph, which a tainted element would silently zero out), so the
# response MUST carry Access-Control-Allow-Origin for the origin the media
# loader states. Which origin that is, is WebKit's business and not something
# this server can enumerate: the page's own origin for the desktop shell, the
# custom-scheme origin on iOS, and — because the bytes are pulled by the media
# process rather than the page's fetch stack — possibly nothing at all, which
# arrives as `Origin: null` and matches no allow-list entry. When it does not
# match, WebKit refuses the load and the element errors: the app is completely
# reachable, every API call works, and pressing play still does nothing.
#
# `*` is the safe answer for exactly these two paths and no others:
#   * they are read-only and authenticated by the session TOKEN in the URL
#     (`?token=`), not by the cookie — a cross-origin caller without the token
#     gets a 401 whatever the CORS headers say, and one WITH the token does not
#     need a browser to fetch the bytes;
#   * `*` (as opposed to echoing the caller's origin) means a browser cannot
#     pair the response with credentials, so nothing here can be turned into a
#     credentialed read of a user's library by a page the user happens to visit.
#
# It only ever ADDS the header when the middleware above did not already state
# one, so an allow-listed origin keeps its exact echo and no response ever
# carries two Access-Control-Allow-Origin values (which is itself a CORS
# failure). Registered after CORSMiddleware on purpose: Starlette applies the
# most recently added middleware outermost, so this one sees the finished
# response, headers included.
_MEDIA_CORS_PATHS = ("/api/stream", "/api/videos/stream")


@app.middleware("http")
async def _media_cors(request: Request, call_next):
    response = await call_next(request)
    if (request.url.path in _MEDIA_CORS_PATHS
            and request.headers.get("origin")
            and "access-control-allow-origin" not in response.headers):
        response.headers["access-control-allow-origin"] = "*"
    return response


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
app.include_router(api_auth.router)
app.include_router(api_recommend.router)
app.include_router(api_ratings.router)
app.include_router(api_plays.router)
app.include_router(api_query.router)
app.include_router(api_discover.router)
app.include_router(api_watch.router)
app.include_router(api_queue.router)
app.include_router(api_add.router)
app.include_router(api_choice.router)
app.include_router(api_jobs.router)
app.include_router(api_media.router)
app.include_router(api_stack.router)
app.include_router(script_menu.router)
app.include_router(api_storage.router)
app.include_router(api_streaming.router)
app.include_router(api_soulseek.router)
app.include_router(api_youtube.router)
app.include_router(api_rym.router)
app.include_router(api_cookies.router)

# Script 8 (Auto tagging) never imports a genre: it derives MOOD/ENERGY from
# the audio, cross-references INSTRUMENTAL and derives the album advisory.
# GENRE is written by the import pipeline's genre chain and by manual edits
# only — see mlo.autotag's module docstring.
from mlo import autotag as _autotag  # noqa: E402

# --------------------------------------------------------------------------- #
# Busy library paths (server.job_locks)
# --------------------------------------------------------------------------- #
# Runs, imports and the organizer claim the paths they work on; a route that
# asks for a path another job holds raises PathLocked and is answered 409 with
# the holder named — the same status the script-run routes already use for "a
# run is already in progress", so the UI's error path shows it. Registering it
# once here means the routes themselves only declare which paths they touch
# (`@job_locks.holds(...)`), and never leak the registry's exception type.
@app.exception_handler(job_locks.PathLocked)
def _path_locked(request, exc):
    """409 for a path another job is using (message names that job)."""
    return JSONResponse({"detail": str(exc)}, status_code=409)

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


def _broadcast(payload):
    """Send *payload* to every connected UI socket (no-op with no clients)."""
    loop = _MAIN_LOOP
    if loop is None or loop.is_closed():
        return
    with _progress_lock:
        clients = list(progress_clients)
    for ws in clients:
        try:
            asyncio.run_coroutine_threadsafe(ws.send_json(payload), loop)
        except Exception:
            pass


def _relay(done, total, desc, steps=None):
    """Push one progress frame to every UI socket.

    ``steps`` is the whole-step pair a chained run also knows — "script 3 of
    18" — sent beside the fractional ``done`` the bar is drawn from, so the
    readout can print a whole number while the bar keeps moving inside the
    step that is still running.

    The frame carries WHO it belongs to (``job``/``kind``/``label``, read from
    the job registry's thread-local — see job_locks.frame_identity): with one
    store field the shell could only ever draw one bar, and a run and an export
    on screen together overwrote each other. A producer that owns no job (a
    chain ticking between two scripts) sends no identity, and the client keeps
    drawing those on its single legacy bar.
    """
    try:
        if orig_hook:
            orig_hook(done, total, desc)
    except Exception:
        pass
    payload = {"type": "progress", "done": done, "total": total, "desc": desc}
    if steps:
        payload["steps"] = [int(steps[0]), int(steps[1])]
    job_id, kind, label = job_locks.frame_identity()
    if job_id:
        if not kind or not label:
            # A frame that carried only its id (an older publisher, or one whose
            # identity block ran before its record landed): the registry still
            # knows whose it is, and a labelled bar is the point.
            rec = job_locks.job_record(job_id)
            kind = kind or str(rec.get("kind") or "")
            label = label or str(rec.get("label") or rec.get("kind") or "Job")
        payload["job"] = job_id
        payload["kind"] = kind
        payload["label"] = label
    _broadcast(payload)


stats_mod.progress_hook = _relay
stats_mod.tqdm = None
# A producer that ENDS tells the UI directly (job_locks.release): the client
# takes a bar off the screen on that fact instead of clearing on a timer, which
# is what made a running job's bar blink off between its steps.
job_locks.set_end_hook(_broadcast)


# --------------------------------------------------------------------------- #
# Soulseek status push
# --------------------------------------------------------------------------- #
# The dot on the Soulseek tab is drawn from /api/soulseek/status, which the UI
# only re-fetched on its own timer: a login that landed, a logout, or a slskd
# that died left the dot stale until the next poll (up to 20 seconds) — or
# until a page reload. This watcher re-derives the SAME payload and pushes a
# frame the moment anything in it changes, so the browser repaints the dot
# immediately. One payload definition, so the push can never disagree with a
# refresh.
_SLSK_SIG = None
_SLSK_INTERVAL_S = 2.0


def _slsk_signature(payload):
    return (
        bool(payload.get("installed")), bool(payload.get("running")),
        payload.get("logged_in"), payload.get("error") or "",
        payload.get("conflict") or "", payload.get("account") or "",
    )


def _soulseek_check():
    """One watcher pass: push a frame when the status changed.

    Returns True when a frame was pushed. Split from the loop so the push
    rule itself is testable without a socket or a timer."""
    global _SLSK_SIG
    with _progress_lock:
        if not progress_clients:
            # Nobody to tell: skip the daemon query entirely, and leave the
            # memo alone so the next client gets a fresh comparison.
            return False
    sig = _slsk_signature(soulseek_status_payload())
    if sig == _SLSK_SIG:
        return False
    _SLSK_SIG = sig
    _broadcast({"type": "soulseek"})
    return True


def _soulseek_watch():
    while True:
        try:
            _soulseek_check()
        except Exception:
            pass
        time.sleep(_SLSK_INTERVAL_S)


# --------------------------------------------------------------------------- #
# Nothing in the app ever looked at slskd's UPLOAD tree: what others take from
# us only showed up in the Shares page's own history, when the user went
# looking. This watcher announces a peer that STARTS downloading from us — one
# frame per user, the moment they go from quiet to taking files, and nothing
# more while they are still taking the same ones.
_ULSK_INTERVAL_S = 5.0     # uploads change slowly: one poll per 5 s is plenty
_ULSK_STATE = {}           # {username: active uploads} from the last pass


def _soulseek_uploads_check(cfg=None):
    """One uploads pass: announce every user that just started downloading
    from us. Returns the frames emitted — the loop ignores them, a test does
    not have to.

    Skipped entirely when slskd is not up (and before the config is read):
    there is no upload tree to look at, and a daemon that is not running must
    not cost a request — or a config read — every 5 seconds."""
    from server import soulseek

    global _ULSK_STATE
    if not (soulseek.is_running() or soulseek.web_up()):
        return []
    cfg = cfg or load_config()
    _ULSK_STATE, frames = soulseek.upload_start_frames(
        _ULSK_STATE, soulseek.uploads_state(cfg))
    for f in frames:
        events_mod.emit("upload_started", f"Sharing started: {f['files']} file(s)",
                        f"{f['username']} is downloading from you",
                        {"link": "/soulseek", "username": f["username"],
                         "files": f["files"]}, config=cfg)
    return frames


def _soulseek_uploads_watch():
    while True:
        try:
            _soulseek_uploads_check()
        except Exception:
            pass
        time.sleep(_ULSK_INTERVAL_S)


# --------------------------------------------------------------------------- #
# Live transfer progress push
# --------------------------------------------------------------------------- #
# The Downloads tab's bars were drawn from the page's own 3 s poll of
# /api/soulseek/downloads. Measured on a scratch instance with a real 2 MB/s
# transfer (fake slskd, see the report), the bar moved every 3.02 s and what it
# showed sat on average 1.48 s — p90 2.79 s — behind the bytes slskd had
# already counted, because a poll only ever reports the instant it ran. Bytes
# move continuously, so the reader watches a bar that jumps and then sits
# still. This watcher pushes the SAME rows the route returns the moment they
# change, and the page draws those bars from the frame instead of its timer.
#
# Two cadences, because only moving bytes deserve 2.5 frames a second: while a
# transfer is InProgress (or a job is running) slskd's tree is read every 0.4 s,
# otherwise every 5 s — a queue slskd has not started yet costs the idle one. A
# frame goes out only when something in it changed, and a pass with no UI socket
# open costs no request at all. Measured after this landed, with a real 2 MB/s
# transfer: the bar moved every 0.40 s and showed a value 0.17 s old on average
# (p90 0.31 s), against 3.02 s and 1.48 s for the poll it replaced; 2.5 frames
# of ~370 bytes a second, and 0 frames when nothing moved.
_LIVE_INTERVAL_S = 0.4
_LIVE_IDLE_INTERVAL_S = 5.0
_LIVE = {"sig": None, "ids": frozenset(), "rows": {}}


def _live_transfer_files(tree):
    """Every file in slskd's transfer tree — the rows a bar is drawn from."""
    return [f
            for entry in (tree or [])
            for d in (entry.get("directories") or [])
            for f in (d.get("files") or [])]


def _live_job_rows():
    """Every live job's own progress, as its module publishes it.

    The `progress` block is handed over verbatim: it is the same one
    /api/soulseek/auto serves and the queue rows are built from, so a pushed
    frame can never disagree with a refresh."""
    from server import soulseek_auto
    return [{
        "id": job.get("id"),
        "state": job.get("state"),
        "stage": job.get("stage"),
        "stage_key": job.get("stage_key"),
        "progress": job.get("progress"),
    } for job in soulseek_auto.jobs()]


def _live_signature(files, jobs):
    """What a frame is worth sending for: each transfer's own byte count and
    state, plus each job's state and progress numbers."""
    sig = [(f.get("id"), f.get("bytesTransferred"), f.get("state")) for f in files]
    for job in jobs:
        p = job.get("progress") or {}
        sig.append((job["id"], job["state"], job["stage"], p.get("bytes"),
                    p.get("files_done"), p.get("files_arrived")))
    return tuple(sig)


def _live_transfers_check():
    """One push pass. Returns True when the caller should tick again quickly.

    Fast is for bytes that are actually moving, plus the one pass after
    anything changed — that extra pass is what carries a FINISHED transfer's
    state flip out at 0.4 s instead of waiting out the idle tick. A queue full
    of transfers slskd has not started yet has no bytes to report, so it costs
    the idle cadence; a stalled InProgress one keeps it, because it is the
    thing the page is showing."""
    global _LIVE
    with _progress_lock:
        if not progress_clients:
            # Nobody is watching: no daemon query at all, and the memo is left
            # alone so the next client gets a fresh comparison (the same rule
            # _soulseek_check follows for the status dot).
            return False
    from server import soulseek
    try:
        files = _live_transfer_files(soulseek.downloads_state())
    except Exception:
        # A daemon that is down has no transfers to report. That is not worth
        # a frame of its own — the status watcher already tells the page the
        # daemon went away — and it must not make this loop loud.
        files = []
    jobs = _live_job_rows()
    moving = any("InProgress" in str(f.get("state") or "") for f in files)
    busy = any(j["state"] in ("running", "confirm") for j in jobs)
    sig = _live_signature(files, jobs)
    if sig == _LIVE["sig"]:
        return moving or busy
    ids = frozenset(f.get("id") for f in files)
    rows = {f.get("id"): (f.get("bytesTransferred"), f.get("state")) for f in files}
    # Only the rows that moved, plus one flag for the list changing shape: a
    # slskd tree holds the whole history, and shipping every completed
    # transfer 2.5 times a second would be paid for by a phone on Wi-Fi.
    changed = [f for f in files if _LIVE["rows"].get(f.get("id")) != rows[f.get("id")]]
    resync = ids != _LIVE["ids"]
    _LIVE = {"sig": sig, "ids": ids, "rows": rows}
    _broadcast({"type": "transfers", "files": changed, "jobs": jobs,
                "resync": resync})
    return True


def _soulseek_transfers_watch():
    while True:
        live = False
        try:
            live = _live_transfers_check()
        except Exception:
            pass
        time.sleep(_LIVE_INTERVAL_S if live else _LIVE_IDLE_INTERVAL_S)


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
    # The import wizard's staged album (not in the library yet).
    staged: bool = False


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
    staged: bool = False  # the import wizard's not-yet-imported album


class AssignTagsRequest(BaseModel):
    """Write MB/RYM links to tags. `tracks` maps track path -> {tag: value}."""
    tracks: dict
    staged: bool = False  # the import wizard's not-yet-imported album


class ImportCommit(BaseModel):
    """Store MB/RYM links on an imported album. target_dir = album folder
    name under music_folder, or an absolute path already inside it.

    `rym_artist_link` is the artist-level page the wizard's Links step
    confirmed (`/api/rym/validate` said "artist"): it lands on every track as
    RATEYOURMUSIC_ARTIST in the SAME container write as the album tags, which
    is what keeps that step to one rewrite per track."""
    target_dir: str
    mb_link: Optional[str] = None
    rym_link: Optional[str] = None
    rym_artist_link: Optional[str] = None
    staged: bool = False  # the wizard's album folder, wherever the user put it


class ImportExpected(BaseModel):
    """Record the MusicBrainz release's full tracklist on an imported album.

    An album imported PARTIALLY carries no trace of the tracks that were
    never brought in, so the album page has nothing to grey out. Writing the
    release's own running order at match time is what makes the missing
    tracks visible. `tracks` is [{disc, position, title, recording_mbid}]."""
    target_dir: str
    release_id: Optional[str] = None
    tracks: List[dict] = []
    staged: bool = False  # the wizard's album folder, wherever the user put it


class DownloadsDelete(BaseModel):
    """Basenames of <music>/.mlo/downloads entries to delete permanently."""
    names: List[str] = []


class DownloadsImport(BaseModel):
    """Basenames of <music>/.mlo/downloads entries to move into the library."""
    names: List[str] = []


class StagingRequest(BaseModel):
    """One slskd staging root, and (for delete) the entry inside it.

    `root` is "downloads" or "incomplete" — the two staging folders slskd
    writes to. It is a NAME, never a path: the server resolves it from the
    config, so a client can never aim a delete at an arbitrary directory."""
    root: str = ""
    name: Optional[str] = None


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
@app.get("/api/health")
def health():
    """Liveness, plus the update fields — a client that already polls this can
    show the "newer release" banner without a second request.

    The reply comes from the cached check and NEVER waits on the network: every
    probe of this route gives it a couple of seconds, so a stale cache only
    starts a background refresh for the next poll. See server.version.
    """
    version_mod.refresh_soon()
    return {"status": "ok", **version_mod.cached()}


@app.get("/api/version")
def version_check():
    """What this build is and whether a newer release exists.

    Cached on disk for six hours and never fatal: `source` is `"github"` or
    `"unavailable"`, and an unreachable GitHub answers `latest: null`,
    `update_available: false` rather than an error.
    """
    return version_mod.check()


@app.get("/api/config")
def get_config():
    """The live configuration, minus the one secret in it.

    `auth_password_hash` is a PBKDF2 hash, which is not a password but IS an
    offline-cracking target for whoever holds it — and every client with a
    session (or a `?token=` URL that leaked) can read this endpoint. Nothing
    on the client side needs the value: /api/auth/status reports whether a
    password exists at all.
    """
    cfg = load_config()
    cfg.pop("auth_password_hash", None)
    return cfg


@app.post("/api/config")
def set_config(cfg: dict):
    # The password is not writable through this route: it would let any
    # session replace the credential with a hash of its choosing, and the
    # legitimate path (/api/auth/password) verifies the current password and
    # re-issues the caller's own session. Dropped rather than rejected so a
    # stale client echoing the whole config back does not fail the save.
    cfg = {k: v for k, v in (cfg or {}).items() if k != "auth_password_hash"}
    # The music folder is a path the BACKEND must be able to open — the picker
    # in Settings/wizard sends one, and a path typed by hand or chosen on
    # another machine has to fail here, loudly, rather than be stored and
    # discovered broken at the next start.
    wanted_folder = str(cfg.get("music_folder") or "").strip()
    if wanted_folder and not os.path.isdir(os.path.expanduser(wanted_folder)):
        raise HTTPException(
            400,
            f"the music folder does not exist on this machine: {wanted_folder} "
            f"— pick a folder this server can open")
    # MLO_MUSIC_FOLDER is the Docker/bootstrap source of truth: the folder it
    # names is what the app USES, and the config's own value is rewritten to
    # match on every save (mlo.config._migrate_to_data_dir). So a different
    # folder posted here would be accepted, stored, and silently reverted —
    # say so instead, and name where the pin lives.
    pinned = (os.environ.get("MLO_MUSIC_FOLDER") or "").strip()
    if (wanted_folder and pinned
            and os.path.abspath(os.path.expanduser(wanted_folder))
            != os.path.abspath(os.path.expanduser(pinned))):
        raise HTTPException(
            400,
            f"the music folder is pinned to {pinned} by MLO_MUSIC_FOLDER "
            f"(docker-compose.yml or the environment this server runs in) — "
            f"change it there and restart, or remove the variable to pick one here")
    folder_before = ""
    try:
        folder_before = _music_folder()
    except HTTPException:
        folder_before = ""
    # The Soulseek listen port is pinned the same way, and the failure it
    # prevents is worse than a reverted setting: docker-compose.yml publishes
    # `${MLO_SOULSEEK_LISTEN_PORT:-50000}` in the host's port list while slskd
    # listens on whatever `soulseek_listen_port` says. A port changed HERE while
    # the compose line still names the old one is a forward pointing at a closed
    # port — a share peers can see the size of and never connect to, which is
    # the owner's "clients can detect the number of shared files" report. The
    # environment wins when it is set; say so instead of storing a number that
    # will not survive the next start.
    pinned_port = (os.environ.get("MLO_SOULSEEK_LISTEN_PORT") or "").strip()
    port_before = 0
    try:
        port_before = int(load_config().get("soulseek_listen_port") or 0)
    except (TypeError, ValueError):
        port_before = 0
    if pinned_port and cfg.get("soulseek_listen_port") not in (None, ""):
        try:
            same_port = int(cfg.get("soulseek_listen_port")) == int(pinned_port)
        except (TypeError, ValueError):
            same_port = False
        if not same_port:
            raise HTTPException(
                400,
                f"the Soulseek listen port is pinned to {pinned_port} by "
                f"MLO_SOULSEEK_LISTEN_PORT (docker-compose.yml or the "
                f"environment this server runs in) — change it there and "
                f"restart, or remove the variable to set one here")
    ok = save_config(cfg)
    if not ok:
        reason = getattr(save_config, "last_error", "") or ""
        raise HTTPException(500, f"Failed to save config{(': ' + reason) if reason else ''}")
    # The library folder is baked into slskd's generated config (its share root
    # and its download dir), so a change has to reach a RUNNING daemon — or the
    # share index keeps publishing the tree the library just left. A daemon that
    # is not running picks the new folder up at its next start.
    try:
        if folder_before and _music_folder() != folder_before:
            from server import soulseek as _soulseek
            if _soulseek.is_running():
                _soulseek.restart()
                tagcache.invalidate_all()
    except Exception:
        pass
    # The listen port is baked into slskd's generated config too, and it is the
    # one setting whose staleness is invisible: the daemon keeps serving the old
    # port while every surface in the app says the new one, and peers simply
    # fail to connect. A change therefore reaches a running daemon at once.
    try:
        port_after = int(load_config().get("soulseek_listen_port") or 0)
        if port_before and port_after and port_after != port_before:
            from server import soulseek as _soulseek
            if _soulseek.is_running():
                _soulseek.restart()
    except Exception:
        pass
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
    # A settings save can change the auth gate itself (`auth_mode`,
    # `server_host`, the password hash). The gate's state is cached for a few
    # seconds so the media path does not re-read the config on every range
    # request; refresh it here so turning the gate on/off takes effect on the
    # very next request instead of up to three seconds later.
    try:
        auth_mod.current_state(refresh=True)
    except Exception:
        pass
    return load_config()


@app.get("/api/config/defaults")
def get_config_defaults():
    """Factory defaults — powers the settings UI's reset-to-defaults actions."""
    return DEFAULT_CONFIG


class AiTestRequest(BaseModel):
    """Optional overrides so the wizard can test keys BEFORE they are saved;
    anything omitted comes from the saved config."""
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    effort: Optional[str] = None


@app.post("/api/ai/test")
def ai_test(req: AiTestRequest):
    """One tiny round trip to the configured model.

    The answer to "is this AI setup usable?" — the setup wizard and Settings →
    AI both call it after keys are entered. A provider that refuses is a
    normal 200 payload carrying its own message, never a 500: the message is
    the whole point of the button."""
    from server import ai

    cfg = dict(load_config() or {})
    for key, value in (("ai_base_url", req.base_url), ("ai_api_key", req.api_key),
                       ("ai_model", req.model), ("ai_effort", req.effort)):
        if value is not None:
            cfg[key] = value
    out = {"ok": False, "reply": "", "error": ""}
    try:
        reply = ai.ai_chat(
            cfg,
            "You are a connectivity check. Answer with the single word: ok.",
            "Reply with ok.",
            timeout=30.0,
        )
    except Exception as e:
        out["error"] = str(e)
        return out
    out["ok"] = True
    out["reply"] = (reply or "").strip()[:200]
    return out


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
        # realpath: a symlink inside the library pointing out must not pass.
        ap = os.path.realpath(os.path.normpath(p))
        af = os.path.realpath(os.path.normpath(folder))
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


def _allow_staged(p, staged):
    """The import wizard's opt-in allowance for a path that is NOT in the
    library yet.

    The wizard's per-track steps run on albums the library tree does not list
    — the folder the user pointed it at, a finished download, a staged batch —
    so "inside the music folder" is the wrong test for those calls. It is
    opt-in per request (`staged=true`), and the path still has to EXIST: a
    library-facing call that does not ask for it stays exactly as strict, and
    nothing gains a read of something that is not there.
    """
    return bool(staged) and os.path.exists(p)


def _guard_folder(p, staged=False, what="file", folder=None):
    """The music-folder guard, with the wizard's staged allowance folded in.

    `what` names the thing in the 400 ("file", "folder", "album", "path") and
    `folder` overrides the music folder for the routes that already carry one.
    """
    if _in_music_folder(p, folder or _music_folder()):
        return
    if _allow_staged(p, staged):
        return
    raise HTTPException(400, f"{what} outside music folder")


def _skip_names():
    return {d.lower() for d in SKIP_DIRS}


def _refresh_library_caches():
    """Make the next library/home build re-walk the music folder.

    Both payloads are CACHED — the library tree in `tagcache`'s own entry
    (`get_library`, TTL), Home's for 15 minutes — and Home is built FROM the
    library, so a Refresh that only re-asked answered with the same rows for
    minutes, which reads exactly like a dead button (the reason `/api/home`
    grew its own `?refresh=1`). This is the one place that drops them, shared
    by the Library's and Home's Refresh buttons so the two cannot drift:
    `invalidate_all` clears the tag cache (every album's grade and tech read
    comes from it), the cover cache and the library payload in one go, and the
    identity and recommendation caches go with them.
    """
    tagcache.invalidate_all()
    mbresolve.invalidate()
    from server import recommendations
    recommendations.invalidate()


@app.get("/api/library")
def library(refresh: int = Query(0)):
    """Tag-rich library tree: artists -> albums -> tracks (grade/audit + tags).

    `?refresh=1` is the Library page's Refresh button: it drops the caches the
    payload is built from (`_refresh_library_caches`), so the next build
    re-walks the music folder instead of answering from its TTL entry — what a
    user pressing Refresh after a file was added or a script was run expects.
    """
    if refresh:
        _refresh_library_caches()
    cfg = load_config()
    return lib_mod.build_library(cfg)


@app.get("/api/log/report")
def log_report_route(path: str = Query(...), disc: Optional[int] = Query(None),
                     timeout: int = Query(30, ge=5, le=120)):
    """One rip log in full: Logchecker's own report, this app's checksum
    verdict, and the log's text.

    `path` is the `.log` itself or the album folder that holds it — an album
    with several logs is asked disc by disc (`disc=2` → `CD-2.log`, the pattern
    `grade_album_logs` renames them to), and one with a single log answers
    without a disc. This is the answer to "why did this score 60": the number
    alone never said, and Logchecker's own `Details:` lines are exactly where a
    deduction explains itself (`-10 gap handling` and the like).

    Read-only end to end: nothing is scored into the tags, renamed or stored,
    and the phar runs for this answer alone. The path must live inside the
    music folder, like every other path-taking route.
    """
    from mlo.discs import _disc_expected_name, _disc_pattern_for, log_report

    p = str(path or "").strip()
    root = str(load_config().get("music_folder") or "")
    if not p or not root:
        raise HTTPException(400, "a path inside the music folder is required")
    siblings: List[str] = []
    if os.path.isdir(p):
        # An album folder: pick its log. Several logs are a disc set, and the
        # caller names the one it means — the LIST travels back either way, so
        # the reader can switch discs without asking again.
        try:
            siblings = sorted(f for f in os.listdir(p) if f.lower().endswith(".log"))
        except OSError:
            siblings = []
        if not siblings:
            raise HTTPException(404, "this album holds no .log")
        if disc is not None:
            p = os.path.join(p, _disc_expected_name(_disc_pattern_for(load_config()),
                                                    int(disc), ".log"))
        elif len(siblings) == 1:
            p = os.path.join(p, siblings[0])
        else:
            # Several discs and no disc named: answer with the first, not an
            # error — the payload carries the list, and a viewer that had to
            # guess again would make the reader pick twice.
            p = os.path.join(p, siblings[0])
    if not p.lower().endswith(".log"):
        raise HTTPException(400, "path is not a .log")
    if not os.path.isfile(p):
        raise HTTPException(404, f"no such log: {os.path.basename(p)}")
    if not _in_music_folder(p, root):
        raise HTTPException(400, "log path is outside the music folder")
    out = log_report(p, timeout=int(timeout))
    out["siblings"] = siblings
    return out

@app.get("/api/home")
def home(request: Request, refresh: int = Query(0)):
    """Home page: stats, recent additions, top grades, favorites, a random
    rediscovery shelf, most-collected artists, open wishes and albums failing
    their checks.

    Scoped by the session's user: the shelves carry that person's favourites
    and playlist count, and the cache is keyed on the user for the same reason.

    `?refresh=1` is Home's Refresh button (`_refresh_library_caches`): the
    payload is cached for 15 minutes and is BUILT from the library payload,
    which is cached again under its own key, so refetching the route alone
    returned the same rows for a quarter of an hour — which reads exactly like
    a dead button. One shared drop, the same one `/api/library?refresh=1`
    performs, so the two buttons can never do different things.
    """
    if refresh:
        _refresh_library_caches()
    from server import recommendations
    try:
        return recommendations.build_home(load_config(), auth_mod.current_user(request))
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
        # The shipped default ends the file name with the track's own id, so
        # the sample carries one too — without it the preview showed a path
        # the script could never produce.
        "MUSICBRAINZ_TRACKID": "4f0e7e10-1cf6-4f77-9c48-2c9e9f6f1a11",
        "LABEL": str(sample.get("label") or "American Recordings"),
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


def _fs_roots():
    """Where the folder picker can start: the filesystem root(s)."""
    if os.name == "nt":
        return [f"{ch}:\\" for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                if os.path.isdir(f"{ch}:\\")]
    roots = ["/"]
    home = os.path.expanduser("~")
    if home and home != "/":
        roots.append(home)
    return roots


def _looks_like_library(path):
    """Whether *path* holds audio files directly, so the picker can point at the
    obvious choices. One bounded listing — never a walk."""
    from mlo.paths import LIB_AUDIO_EXTS
    try:
        with os.scandir(path) as it:
            for index, entry in enumerate(it):
                if index > 400:
                    break
                if entry.is_file():
                    ext = os.path.splitext(entry.name)[1].lower()
                    if ext in LIB_AUDIO_EXTS:
                        return True
    except OSError:
        return False
    return False


@app.get("/api/fs/dirs")
def fs_dirs(path: str = Query("")):
    """Subdirectories of a path ON THE SERVER — the music-folder picker.

    The library folder is a path the BACKEND has to be able to open, and no
    browser can hand it one from the client's own machine, so a client that is
    not the server browses the server's filesystem through this route. It lists
    DIRECTORY NAMES only (never file names), flags the ones that already hold
    audio, and reports whether each is writable — the app keeps its state under
    `<music>/.mlo`, so a read-only folder is a trap worth showing. Nothing here
    opens a file. The login gate covers this route like any other.
    """
    raw = str(path or "").strip()
    if raw:
        target = os.path.abspath(os.path.expanduser(raw))
    else:
        # No path: open where the library already is, which is what a picker
        # started from "change the music folder" wants to see.
        try:
            target = _music_folder()
        except HTTPException:
            target = (_fs_roots() or ["/"])[0]
    if not os.path.exists(target):
        raise HTTPException(404, f"no such folder: {target}")
    if not os.path.isdir(target):
        raise HTTPException(400, f"not a folder: {target}")
    dirs = []
    try:
        with os.scandir(target) as it:
            for entry in it:
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue          # a broken link or a race: skip, never fail
                sub = os.path.join(target, entry.name)
                dirs.append({
                    "name": entry.name,
                    "path": sub,
                    "library": _looks_like_library(sub),
                    "writable": os.access(sub, os.W_OK),
                })
    except PermissionError:
        raise HTTPException(403, f"permission denied: {target}")
    except OSError as e:
        raise HTTPException(400, f"could not read {target}: {e}")
    # Plain names first, dot-folders last — `.mlo`, `.Trash-1000` and friends are
    # never what anyone is looking for here.
    dirs.sort(key=lambda d: (d["name"].startswith("."), d["name"].lower()))
    parent = os.path.dirname(target.rstrip(os.sep)) or None
    if parent and os.path.normcase(parent) == os.path.normcase(target):
        parent = None             # a drive root or "/": nothing above it
    return {
        "path": target,
        "parent": parent,
        "roots": _fs_roots(),
        "dirs": dirs,
        # When the folder is pinned by the environment (Docker/compose), the
        # picker can browse but must not pretend a choice would stick — the
        # config's own value is rewritten to the pin on every save.
        "pinned": (os.environ.get("MLO_MUSIC_FOLDER") or "").strip() or None,
    }


@app.get("/api/dependencies")
def dependencies(refresh: int = Query(0)):
    """Installed external tools (the music folder's .mlo/tools + PATH, and the
    app's pre-move .dependencies) vs. the pinned target the installer fetches
    AND the newest release upstream actually has.

    `refresh=1` re-checks GitHub now instead of waiting out the 30-minute TTL.
    Both are answered from a cache and re-fetched by a background thread, so
    this route never waits on GitHub: `checking` says a pass is in flight and
    `upstream_version` stays null until it lands. A failed check degrades a row
    (`upstream_version: null` + `note`) — never the whole table.

    Also starts the auto-update loop: it is a background thread with nothing to
    do until something asks which tools are installed, and this is that call.
    """
    from mlo import fetchdeps
    from mlo.paths import tools_dir
    fetchdeps.ensure_auto_update_worker()
    # The folder this names is the one the page's "open the tools folder" button
    # opens, and the OS opener refuses a path that is not there: an install that
    # has downloaded nothing yet still answers with a real folder. It is inside
    # the music folder, i.e. the app's own directory to create; a music folder
    # that cannot be written to is not an error for a READ (the rows answer
    # either way).
    try:
        os.makedirs(str(tools_dir()), exist_ok=True)
    except OSError:
        pass
    return {"deps_dir": str(tools_dir()),
            **fetchdeps.dependencies_payload(refresh=bool(refresh))}


def _check_source_kind(kind):
    """400 on an unknown `kind` filter (both sources routes validate it)."""
    if not kind:
        return
    from server import sources_health as health_mod

    if kind not in health_mod.KINDS:
        raise HTTPException(400, f"unknown source kind: {kind} "
                                 f"(one of {', '.join(health_mod.KINDS)})")


@app.get("/api/capabilities")
def capabilities_report():
    """What THIS server can do, so the UI can say so before it is asked to.

    Cheap and offline: the answer comes from the cached tool detection and a
    one-time spawn probe (mlo.deps), never from the network. `platform` and
    `python` say which host answered; the per-feature rows are what decides
    whether the UI offers an Install button or names the tool that is missing.
    """
    from mlo.deps import capabilities

    return {"platform": sys.platform,
            "python": sys.version.split()[0],
            **capabilities()}


@app.get("/api/sources/health")
def sources_health(kind: str = Query(None), probe: int = Query(0)):
    """Which external sources work right now — the wizard's and Settings' one
    answer, for every kind at once (`lyrics`, `advisory`, `genre`, `metadata`,
    `links`, `discover`, `credentials`).

    `probe=0` (default) reports the CONFIG only and performs no request at
    all: unconfigured sources are `skipped` (with the config keys they need)
    and the rest `ok`. `probe=1` runs one cheap lookup per configured source
    against the same fixed sample the lyrics providers already probe with —
    in parallel, a few seconds in total — and says what answered.

    The `credentials` rows are not sources: they are the saved logins the
    other kinds need, each asked through its provider's own credential
    endpoint (Discogs `/oauth/identity`, Last.fm `chart.gettoptags`, Spotify
    `POST /api/token`, an AcoustID lookup, slskd's live state, this server's own
    stored password). A source row cannot answer that question — Discogs
    browses anonymously, so its row stays green with a discarded token.
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
    source and a metadata provider, and `discogs`/`lastfm`/`spotify` are also
    credentials), so the row kinds are searched in the payload's own order —
    lyrics, advisory, genre, metadata, links, discover, credentials — unless
    `kind=` picks one. The row always carries its kind, so a caller that sends
    both ids (`kind=id`) is never guessing.
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
    """Install or update external tools, one key at a time.

    A requested tool that is behind gets the NEWEST release its own publisher
    has — GitHub, PyPI or windows.php.net — and the reviewed pin only when
    nothing newer can be seen (see fetchdeps.install_dependency). That is what
    lets one row's Install button do what its Update chip promised; the pin
    stays the target of a first install on a machine that cannot reach the
    check.

    Every result also carries `row`: that tool's Dependencies row as it reads
    AFTER the install, so a pressed row settles in place instead of the page
    needing a second round trip to find out what happened. The other result
    keys (`key`, `name`, `ok`, `version`, `changed`, `error`, `restarted`) are
    unchanged.
    """
    from mlo import fetchdeps
    from server import soulseek as slsk

    # `null` is "every tool" (the page's Install all); an EMPTY list is "the
    # caller found nothing to do" (the wizard's Install missing with nothing
    # missing) and must install nothing. `req.keys or []` collapsed the two,
    # so one press of a button that had nothing to install started a full
    # reinstall of all sixteen tools.
    #
    # "Every tool" means every tool this PLATFORM can install: a Windows-only
    # tool on Linux, or one the host provides as a distro package (flac, ffmpeg
    # — the Docker image ships them), has no download to perform, and counted
    # as failures they turned one press of Install all into twelve of sixteen
    # tools reported broken. The rows carry the same flag (fetchdeps.
    # installable), so the page's per-tool buttons exclude them too; an
    # explicitly requested tool still gets its honest refusal below.
    #
    # It also means only the rows with something TO DO — missing, or behind
    # upstream — the same rule the auto-update pass uses. Re-fetching tools
    # that are already at the newest release downloaded slskd's 118 MB again on
    # every press and changed nothing, which is what made the button look like
    # it was doing something mysterious. (An installer that skipped a download
    # this way reports the version it found: `changed: false`.) To force a
    # fresh copy, delete the tool's folder — the page opens it — and the row
    # reads missing again.
    if req.keys is None:
        try:
            state = {row["key"]: row["state"]
                     for row in fetchdeps.dependency_rows()}
            keys = [k for k in fetchdeps.installable_keys()
                    if state.get(k) in ("missing", "update")]
        except Exception:
            # The row list failed (an unreadable tool folder, a broken config):
            # install what this platform can install rather than refusing the
            # whole press.
            keys = fetchdeps.installable_keys()
    else:
        wanted = set(req.keys)
        keys = [k for k in fetchdeps.DISPLAY_NAMES if k in wanted]

    before = {}
    try:
        before = fetchdeps.installed_versions()
    except Exception:
        before = {}

    # slskd is the one dependency this app RUNS. Windows refuses to replace a
    # file another process is executing, so installing it while the managed
    # daemon is up failed with a bare "used by another process" — which is what
    # "dependency installs are broken" turned out to be: 15 of 16 tools install
    # fine, and that one always failed. Stop it around the install and put it
    # back the way it was, so an update is a single press again.
    slskd_was_running = False
    if "slskd" in keys:
        try:
            slskd_was_running = bool(slsk.is_running())
            if slskd_was_running:
                slsk.stop()
        except Exception:
            slskd_was_running = False

    results = []
    try:
        for key in keys:
            name = fetchdeps.DISPLAY_NAMES[key]
            try:
                version = fetchdeps.install_dependency(key, log=lambda m: None)
                results.append({
                    "key": key,
                    "name": name,
                    "ok": True,
                    "version": version,
                    # The version did not move: either the tool was already at
                    # the newest release or the pin is what upstream has. The
                    # page says so instead of a bare "installed", because a
                    # press that changes nothing and says nothing is what reads
                    # as "the button does not work".
                    "changed": not fetchdeps.same_version(version, before.get(key)),
                })
            except Exception as e:
                results.append({"key": key, "name": name, "ok": False, "error": str(e)})
    finally:
        restarted = False
        if slskd_was_running:
            try:
                slsk.start()
                restarted = True
            except Exception:
                restarted = False
        if restarted:
            for row in results:
                if row["key"] == "slskd":
                    row["restarted"] = True

    try:
        fetchdeps.refresh_tool_cache()
    except Exception:
        pass
    # Each result carries its row as the table would show it NOW, so a row
    # pressed on its own settles in place instead of the page waiting a whole
    # round trip to learn that it worked. One row list for the whole response,
    # after the cache refresh, so every row in it is post-install.
    try:
        rows = {row["key"]: row for row in fetchdeps.dependency_rows()}
    except Exception:
        rows = {}
    for result in results:
        row = rows.get(result["key"])
        if row:
            result["row"] = row
    return {"results": results}


@app.get("/api/album/mbdetect")
def album_mbdetect(path: str = Query(...), staged: bool = Query(False)):
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
    _guard_folder(p, staged, "album")
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
def get_album(path: str = Query(...), staged: bool = Query(False)):
    """Album detail. `path` may be a real folder or an "mb:<release MBID>"
    reference — the app links albums by MusicBrainz ID so pages survive
    reorganization."""
    p = os.path.normpath(mbresolve.resolve_album(path) or path)
    if not os.path.isdir(p):
        raise HTTPException(404, "album not found")
    _guard_folder(p, staged, "album")
    res = lib_mod.build_album(p, load_config())
    if res is None:
        raise HTTPException(404, "no audio files")
    # What an import could not supply for this album, if anything: the same
    # entry the queue's own row and the bell carry (`server.import_autonomy`),
    # in the same shape, so the page says "needs data" with the wizard link
    # instead of leaving the reader to infer it from grading failures. Asked
    # HERE and not inside `build_album`: the library page builds hundreds of
    # albums through that one, and an entry asks the grader a question.
    try:
        from server import import_autonomy
        entry = import_autonomy.for_album(p, load_config())
        if entry:
            res["needs"] = import_autonomy.warning(entry)
    except Exception:
        traceback.print_exc()
    return res


@app.get("/api/podcasts")
def podcast_series(series: str = Query(...)):
    """ONE podcast series and every episode the library holds, newest first.

    A podcast is a MusicBrainz SERIES of type Podcast whose episodes are
    release groups linked `part of` it; the app records that series on each
    episode's files (`mlo.autotag` writes the PODCASTSERIES tags), so this
    answers from the library scan alone — no MusicBrainz request per page view.

    `series` is the NAME a shelf row links by (the name the app stored, with
    MusicBrainz's disambiguation when it stated one: that is what keeps two
    same-named shows apart). A series the library holds no episode of is a
    404, not an empty page — the same rule the album and artist pages follow.
    """
    from server import recommendations
    payload = recommendations.podcast_series_payload(load_config(), series)
    if payload is None:
        raise HTTPException(404, f"no podcast series: {series}")
    return payload


@app.get("/api/artist")
def get_artist(path: str = Query(...)):
    """Artist detail; `path` may be a real folder or an "mb:<artist MBID>"."""
    p = os.path.normpath(mbresolve.resolve_artist(path) or path)
    if not os.path.isdir(p):
        raise HTTPException(404, "artist not found")
    if not _in_music_folder(p, _music_folder()):
        raise HTTPException(400, "artist outside music folder")
    cfg = load_config()
    dir_scan = {}
    albums = lib_mod._find_albums(p, dir_scan)
    direct = [alb for alb in sorted(albums)
              if os.path.dirname(alb).lower() == p.lower()]
    # Framework albums (`server.pending_albums`) hold no audio yet, so the walk
    # above — which looks for audio — never lists them. They are albums the
    # user asked for, so the artist page lists them exactly as the library does
    # (`build_album` answers their folder with the pending row).
    direct += lib_mod.pending_album_dirs(p, dir_scan)
    albums_data = lib_mod.build_albums_parallel(sorted(direct), cfg)
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


def _refuse_locked(path: str) -> None:
    """409 when a job in the registry holds *path* (or a folder above it).

    Reading bytes OUT of a file a script is rewriting is the same race the
    registry already refuses from the write side, seen from the other end: the
    listener gets a torn read, and on Windows the script's temp-then-replace
    fails outright while a stream still holds the file open. Raising
    PathLocked puts this on the app's one handler for it, so the refusal names
    the job and the fix in the registry's own words.
    """
    holder = job_locks.holder(path)
    if holder:
        raise job_locks.PathLocked(path, holder)


@app.get("/api/stream")
def stream(path: str = Query(...), download: int = Query(0)):
    """Stream one library file.

    `download=1` is the offline-download path: the WHOLE file as a single 200
    body that refuses ranges, because the browser's offline cache (Cache
    Storage) rejects a 206 outright — the download button used to fetch this
    same URL and every track failed with "Cache got basic response with bad
    status 206". See server/api_media.py. Without it, a player gets exactly
    what it wants: byte ranges and a 206.

    `download_codec` decides WHAT those downloaded bytes are: `copy` (the
    default) is the file's own codec, any other value re-encodes the track
    into the cache at `download_bitrate` — the library file is never touched,
    and the rendition is served from here rather than from the bulk route
    (see server/api_media.download_rendition). A player is unaffected either
    way: `download` is the only flag that re-encodes.

    A path a job holds right now is refused 409 (both modes) instead of
    streaming a file that is being rewritten; the PLAYER does not change
    otherwise — an unlocked file answers ranges exactly as before, and a stream
    that is already open keeps its handle when a job claims the file (the
    registry guards new reads, it does not cut live ones).
    """
    p = os.path.normpath(mbresolve.resolve_track(path) or path)
    _refuse_locked(p)
    if not os.path.isfile(p):
        raise HTTPException(404, "file not found")
    if not _in_music_folder(p, _music_folder()):
        raise HTTPException(400, "file outside music folder")
    ctype = _CTYPES.get(os.path.splitext(p)[1].lower(), "application/octet-stream")
    if download:
        from server import api_media
        cfg = load_config()
        codec = str(cfg.get("download_codec") or "copy").strip().lower()
        if codec not in ("", "copy"):
            return api_media.download_rendition(p, codec,
                                                cfg.get("download_bitrate") or 0)
        return api_media.full_body_response(p, ctype)
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

    Refused 409 like /api/stream while a job holds the file: a transcode would
    read the very bytes being rewritten.
    """
    p = os.path.normpath(mbresolve.resolve_track(path) or path)
    _refuse_locked(p)
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
        "-i", tool_path(p),
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
_PLAYBACK_META_MAX = 500  # honey: plain-dict LRU; probed once per file version


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
        raise HTTPException(404, "file not found")
    hit = _playback_meta_cache.get(p)
    if hit and hit[0] == mtime:
        _playback_meta_cache.pop(p, None)
        _playback_meta_cache[p] = hit
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
    _playback_meta_cache.pop(p, None)  # refresh recency
    while len(_playback_meta_cache) >= _PLAYBACK_META_MAX:
        _playback_meta_cache.pop(next(iter(_playback_meta_cache)))
    _playback_meta_cache[p] = (mtime, meta)
    return {"path": p.replace("\\", "/"), **meta}


@app.get("/api/tags/registry")
def tags_registry():
    """What this app knows about every tag, in one payload.

    Every field is derived from the code that owns that fact (see
    server.tags_registry): the tag vocabulary, the writer's script name, the
    grade checks, the write gate and the grader's own excess-tag predicate.
    The tag editor, the bulk dialog and the Grading page read this instead of
    each keeping its own hand-written list of labels and checks. Built once
    per process — it reads import-time tables only.
    """
    from server import tags_registry as registry_mod

    return registry_mod.registry()


@app.get("/api/tags")
def get_tags(path: str = Query(...), staged: bool = Query(False)):
    """Read-only tag/lyrics/cover view (tag *writing* was removed — the
    engine's grading/auditing scripts own all tag writes now). Accepts an
    "mb:<recording MBID>" reference as well as a path. `staged` lets the
    import wizard read an album folder that is not in the library yet."""
    from mlo.audio import AudioFile
    p = os.path.normpath(mbresolve.resolve_track(path) or path)
    if not os.path.isfile(p):
        raise HTTPException(404, "file not found")
    _guard_folder(p, staged, "file")
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
        which is a translation.

        Listed, not globbed: `glob` silently yields nothing for a path past
        MAX_PATH on Windows (it stats each joined name), so a deep library
        would lose its translation sidecars with no error anywhere.
        """
        folder, name = os.path.split(p)
        stem = os.path.splitext(name)[0].lower() + "."
        main = os.path.splitext(name)[0].lower() + ".lrc"
        try:
            names = sorted(n for n in os.listdir(folder)
                           if n.lower().startswith(stem) and n.lower().endswith(".lrc"))
        except OSError:
            return None
        for cand in names:
            low = cand.lower()
            # The track's OWN sidecar is the main lyrics file, not a
            # translation — a both-tag-and-sidecar library has it sitting right
            # here, and returning it made the player render the same lyrics
            # twice (once as the song, once as its "translation").
            if low == main:
                continue
            if not low.endswith(XLIT_SIDECAR):
                return _read_text(os.path.join(folder, cand))
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
    so a library that was never run through script 7 still plays level. That
    measurement is BOUNDED (`loudness.PLAYBACK_WAIT_S`): the player installs
    the gain before the track starts, so this request must never sit on a
    multi-second decode — a file that is not measured in time answers unity,
    the decode finishes in the background and its value is cached for the
    next request. That answer is not a verdict, so it also carries `pending`
    (true while the decode is still running: ask again and the gain is there)
    and `album` (false in album mode means the album carries no album gain and
    the track value was used).
    """
    from mlo import loudness

    p = os.path.normpath(mbresolve.resolve_track(path) or path)
    if not os.path.isfile(p):
        raise HTTPException(404, "file not found")
    if not _in_music_folder(p, _music_folder()):
        raise HTTPException(400, "file outside music folder")
    cfg = load_config()
    res = loudness.replaygain_for_path(cfg, p, mode=(mode or None),
                                       wait_s=loudness.PLAYBACK_WAIT_S)
    return {
        "path": p.replace("\\", "/"),
        "gain": res.get("gain"),
        "peak": res.get("peak"),
        "mode": res.get("mode"),
        "source": res.get("source"),
        "analyzed": bool(res.get("analyzed")),
        # `pending`: the on-demand measurement this request started (or joined)
        # is still decoding, so the unity above is TEMPORARY. The player asks
        # again while this is true and, when the value lands, ramps it onto the
        # element that is already playing — an untagged track used to keep that
        # unity for its whole length.
        "pending": bool(res.get("pending")),
        # `album`: the number came from REPLAYGAIN_ALBUM_GAIN. False while
        # `mode` is "album" says the album has no album gain, so per-track
        # values were used — the player reports that instead of implying the
        # album was normalised as an album.
        "album": bool(res.get("album")),
    }


@app.get("/api/cover")
def get_cover(request: Request, album: str = Query(...), file: Optional[str] = Query(None),
              color: int = Query(0), staged: bool = Query(False)):
    """Serve an album's cover art, cached with ETag; ?color=1 returns the
    dominant color instead of the image bytes (UI tinting). Accepts an
    "mb:<release MBID>" album reference, and `staged=1` for the import
    wizard's album — the folder the library does not list yet, which the
    preview <img> therefore has to ask for the same way every other wizard
    call does (without it the folder guard refuses it, and the preview stays
    empty however well the cover was written).

    The response tells caches to REVALIDATE, and the ETag is the byte hash of
    the file: a cover is replaced in place (cover.jpg stays cover.jpg), so
    "fresh for an hour" would keep serving the previous image long after the
    write. An unchanged cover still costs only a 304 — the bytes come from the
    mtime+size-keyed cache below, never a stale entry.
    """
    alb = os.path.normpath(mbresolve.resolve_album(album) or album)
    if not os.path.isdir(alb):
        raise HTTPException(404, "album not found")
    _guard_folder(alb, staged, "album")
    if color:
        c = tagcache.cover_color(alb, file)
        if c is None:
            raise HTTPException(404, "no cover")
        return {"color": c, "album": alb.replace("\\", "/")}
    data, ctype, etag = tagcache.cover_bytes(alb, file)
    if data is None:
        raise HTTPException(404, "no cover")
    from fastapi.responses import Response
    headers = {"Cache-Control": "no-cache", "Accept-Ranges": "bytes"}
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


def _cover_url_bytes(url, artist="", substitute=True):
    """Cover bytes for a caller-supplied URL.

    A provider URL goes through the art cache — its per-host headers are what
    gets past a CDN that refuses the app, its cache is what makes a re-pick
    instant, and `artist` is the identity the fallback is asked about when the
    CDN refuses everybody on this network (the artist-image path, where the
    stand-in is the same artist's picture from another provider). Any other
    public image URL is fetched directly, the way it always was.

    `substitute=False` is what a cover WRITE asks for (`/api/cover/fromurl`,
    the import chain's cover step): the bytes must be the picture the caller
    named — the one the user picked, or the candidate the policy chose for the
    album. Another provider's image standing in for it would be written to the
    library as if it had been picked, which is exactly how a wrong cover lands
    on an album; the caller gets an error instead.
    """
    if artcache.allowed(url):
        data, ctype, _source = artcache.fetch_art(
            url, artist=artist, substitute=substitute)
        if not data:
            raise ValueError("that image could not be fetched — nothing was "
                             "written in its place")
        return data, ctype
    return intg.fetch_image_bytes(url)


@app.post("/api/cover")
@job_locks.holds(lambda album, **_: [album], kind="cover", label="Cover write")
async def upload_cover(album: str = Query(...), file: UploadFile = File(...),
                       track: Optional[str] = Query(None),
                       tracks: Optional[str] = Query(None),
                       staged: bool = Query(False)):
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
    _guard_folder(alb, staged, "album")
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
    tstem = os.path.splitext(os.path.basename(track or ""))[0]
    return re_safe_filename(tstem) or "cover"


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
    out = {"ok": True, "path": dest.replace("\\", "/"), "token": _cover_token(dest)}
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


def _cover_token(path: str):
    """A value that changes whenever a cover file's bytes do — mtime + size.

    The UI puts it in the cover URL (`&v=<token>`), which is the only way a
    REPLACED cover is a different URL: a new image written over cover.jpg
    keeps the same album, the same file name and therefore the same URL, so
    neither the rendered <img> nor any HTTP cache would ever ask for it again.
    """
    try:
        st = os.stat(path)
    except OSError:
        return ""
    return f"{st.st_mtime_ns}-{st.st_size}"


def _cover_metrics(path):
    """Dimensions (+ a below-target warning) for a just-written cover file.

    The same PIL read /api/cover/info does, but from the file: the write has
    already invalidated the byte cache this early in the request.

    The warning is the whole of the enforcement on this path: a cover the USER
    applies — an upload, or a pick from the finder's own list — is written
    whatever its size, and the reply says what is wrong with it. Refusing is
    deliberate only in the AUTONOMOUS path (`server.imports.run_cover_step`),
    which must not put a below-minimum image in the library without anyone
    asking for it; the finder's list shows below-floor rows for a hand apply
    precisely because the user may know better.
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
    # split("/")[-1], not [1]: a missing or malformed Content-Type (an empty
    # string, or a bare "image") has no second part, and indexing it raised
    # IndexError -> a 500 out of /api/cover/fromurl for a remote image that
    # simply did not say what it was.
    ct = (content_type or "").split("/")[-1].strip().lower()
    if ct in ("jpeg", "jpg"):
        return ".jpg"
    if ct in ("png", "webp", "jxl", "bmp"):
        return f".{ct}"
    return ".jpg"


@app.get("/api/cover/search")
async def cover_search(artist: str = Query(""), album: str = Query(""),
                       limit: int = Query(cover_choice.SEARCH_LIMIT, ge=1, le=100),
                       sources: Optional[str] = Query(None),
                       country: Optional[str] = Query(None),
                       release_group_mbid: Optional[str] = Query(None),
                       release_mbid: Optional[str] = Query(None),
                       tracks: Optional[int] = Query(None, ge=1, le=1000)):
    """Album covers for artist/album, RANKED by the one cover policy.

    Sources: covers.musichoarders.xyz (aggregates Apple Music, Deezer, Qobuz,
    Tidal, Discogs, ...) by name, the Cover Art Archive by the identities the
    caller holds — the release's own front cover by `release_mbid`, its release
    group's stand-in by `release_group_mbid` — and, only when those have
    nothing, the keyless Deezer/iTunes fallbacks. `sources` in the reply is the
    per-source report (used / empty / error / skipped, with the reason), so a
    query that found nothing says what was tried and what was never asked.

    The search VERIFIES what it finds: `artist`/`album` (and `tracks`, when the
    caller knows how many the album has) are the identity each row's own
    release is checked against, so a karaoke, tribute or other-album row that
    answers to the same names is rejected rather than ranked — and pinned at
    the bottom of `results` with its rejection, where the finder still offers
    it for a hand apply. `identity` in the reply states what was checked
    against.

    `results` are `mlo.cover_choice`'s ranked candidates — best first, each row
    carrying the image's real pixel size, container and byte count as measured
    from the file, whether it is the release's own cover or a group stand-in,
    the reasons that put it where it is, and `rejected` when it cannot be the
    automatic pick (below the cover target, never measured while that target is
    the minimum, undecodable, an empty answer, another album's release) —
    with `chosen` (the winner) and `notes` alongside, so the finder's first
    row is first for a stated reason.

    `sources` (comma-separated ids) and `country` override the saved defaults
    for this one search — the finder's source picker and region dropdown.
    """
    if not artist.strip() and not album.strip():
        raise HTTPException(400, "artist or album is required")
    src = [s.strip() for s in (sources or "").split(",") if s.strip()] or None
    cfg = load_config()
    try:
        found = await asyncio.to_thread(
            intg.cover_search, artist.strip(), album.strip(), limit,
            60.0, src, country, cfg, (release_group_mbid or "").strip(),
            (release_mbid or "").strip())
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"cover search failed: {e}")
    payload = cover_choice.cover_payload(
        found.get("results") or [], cfg, sources=found.get("sources"),
        provider=found.get("provider"),
        identity={"artist": artist.strip(), "album": album.strip(),
                  "tracks": tracks})
    return {"provider": found.get("provider"),
            "results": payload["candidates"],
            "chosen": payload["chosen"],
            "identity": payload["identity"],
            "notes": payload["notes"],
            "policy": payload["policy"],
            "candidate_count": payload["candidate_count"],
            "rejected_count": payload["rejected_count"]}


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
@job_locks.holds(lambda album, **_: [album], kind="cover", label="Cover write")
async def cover_from_url(album: str = Query(...), url: str = Query(...),
                         track: Optional[str] = Query(None),
                         tracks: Optional[str] = Query(None),
                         staged: bool = Query(False)):
    """Download a cover image from a URL (e.g. a COV search result) and
    store it like an uploaded cover (album cover.*, one per-track sidecar, or
    one image mapped to a whole `tracks=` selection).

    The image at `url` is the image stored: no provider is asked in its place
    (see `_cover_url_bytes`), so a pick that cannot be fetched fails here and
    says so instead of landing some other cover on the album.
    """
    alb = os.path.normpath(album)
    if not os.path.isdir(alb):
        raise HTTPException(404, "album not found")
    _guard_folder(alb, staged, "album")
    stem, selected = _cover_write_target(alb, track, tracks)
    try:
        data, ctype = await asyncio.to_thread(
            _cover_url_bytes, url, substitute=False)
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
    staged: bool = False                # the import wizard's staged album


@app.post("/api/cover/clear")
@job_locks.holds(lambda req: [req.album], kind="cover", label="Cover write")
def cover_clear(req: CoverClearRequest):
    """Drop per-track cover mappings for an album (no `tracks` = all of them).
    The image files stay on disk — clearing a mapping is not deleting art."""
    alb = os.path.normpath(mbresolve.resolve_album(req.album) or req.album)
    if not os.path.isdir(alb):
        raise HTTPException(404, "album not found")
    _guard_folder(alb, req.staged, "album")
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
@job_locks.holds(lambda req: [req.path], kind="tags",
                 label="Video tag write (remux)")
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
    """Grab this artist+title's music video: YouTube first, Soulseek second.

    The YouTube candidate is picked by server/youtube.py (duration window,
    lyric / cover / tribute filtering) and downloaded here. When that half is
    not available — youtube_enabled off, yt-dlp missing, no acceptable
    candidate, or a download it could not deliver — the track is looked for on
    the NETWORK before the answer is "no": a music video nobody put on YouTube
    is often a plain file on a peer's share.

    A Soulseek download takes minutes, so this route does NOT wait for one: it
    searches briefly and QUEUES the transfer as the app's own download (the
    Downloads page shows it from then on) by the same helper the release path
    downloads with (server.soulseek_auto.fetch_video_on_soulseek).

    Returns {ok, file, candidate} for a YouTube download,
    {ok, source: "soulseek", queued, candidate} for a queued one, and
    {ok: false, candidate: null, error} naming BOTH sources when neither has
    this video."""
    from server import soulseek
    from server import soulseek_auto
    from server import youtube

    cfg = load_config()
    artist = str(req.artist or "").strip()
    title = str(req.title or "").strip()
    if not artist or not title:
        raise HTTPException(400, "artist and title are required")
    # YouTube's own gates, and they are only YouTube's: a switch that is off —
    # or a yt-dlp that is not installed — leaves the network as the source it
    # always was, so neither of them ends the request. What they say is kept
    # for the error below, which must not report "not on YouTube" about a
    # lookup the app was never allowed (or able) to make.
    yt_why = ""
    if not youtube.ytdlp_available(cfg):
        yt_why = "yt-dlp is not available — install it under Dependencies"
    elif not youtube.enabled(cfg):
        yt_why = "YouTube downloads are disabled in Settings → Videos"

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

    candidate = None
    if not yt_why:
        candidate = youtube.best_candidate(artist, title,
                                           want_seconds=req.duration,
                                           config=cfg)
        if not candidate:
            yt_why = "no acceptable YouTube match found"
    if candidate:
        try:
            got = youtube.download(candidate["url"], dest, cfg)
        except Exception as e:
            # The upload is there and could not be delivered: that is the same
            # "YouTube cannot serve this one" the network is asked about
            # below, so it does not end the request here.
            yt_why = f"YouTube download failed: {e}"
        else:
            tagcache.invalidate_all()
            return {"ok": True, "file": str(got.get("path") or "").replace("\\", "/"),
                    "candidate": candidate, "container": got.get("container"),
                    "height": got.get("height"), "abr": got.get("abr")}

    # Nothing from YouTube: ask the network for the plain video file and hand
    # the transfer to the app's own download queue. `dest` is deliberately not
    # passed — slskd decides where a transfer lands (under the download dir,
    # where the Downloads page and the importer already look), and waiting for
    # it here would hold this request open for the whole download.
    if not (soulseek.is_running() or soulseek.web_up(cfg)):
        slsk_why = "Soulseek is not running — start slskd to search the network too"
    elif not (soulseek.server_state(cfg) or {}).get("isLoggedIn"):
        # Signed out is not "the network has nothing": slskd is running and
        # cannot search, and saying otherwise would read as "not out there".
        slsk_why = ("Soulseek is not logged in — set your username and password "
                    "in Settings → Soulseek, then restart slskd")
    else:
        try:
            queued = soulseek_auto.fetch_video_on_soulseek(
                artist, title, dest=None, cfg=cfg, seconds=req.duration)
        except Exception as e:
            # slskd's own refusal (an offline peer, a file it will not take)
            # is the user's answer to "why did nothing queue" — the same
            # reading _queue_downloads gives the page's own download routes.
            raise HTTPException(502, f"slskd did not queue the download: {e}")
        if queued:
            return {"ok": True, "source": "soulseek", "queued": True,
                    "candidate": {"user": queued["user"],
                                  "filename": queued["filename"],
                                  "size": queued["size"]}}
        slsk_why = "no Soulseek copy either"
    return {"ok": False, "candidate": None, "error": f"{yt_why}; {slsk_why}"}


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
@job_locks.holds(lambda req: [req.album_path], kind="tags",
                 label="Video tag write (remux)")
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
def cover_info(album: str = Query(...), file: str = Query(None),
               staged: bool = Query(False)):
    """Image details for the album cover (resolution, aspect ratio,
    format, byte size) — powers the album page's Cover info dialog."""
    import io

    alb = os.path.normpath(mbresolve.resolve_album(album) or album)
    if not os.path.isdir(alb):
        raise HTTPException(404, "album not found")
    _guard_folder(alb, staged, "album")
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
@job_locks.holds(lambda req: [req.path], kind="tags", label="Embed lyrics")
def lyrics_embed(req: LyricsEmbedRequest):
    """Write ONLY the embedded LYRICS tag (the last tag-write path the UI
    still needs; everything else is grading-script territory)."""
    from mlo.audio import AudioFile
    p = os.path.normpath(mbresolve.resolve_track(req.path) or req.path)
    if not os.path.isfile(p):
        raise HTTPException(404, "file not found")
    _guard_folder(p, req.staged, "file")
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
def likes_list(request: Request = None):
    """Paths of all liked (hearted) tracks, newest first. Stored paths are
    normalized to forward slashes and MBID-backed rows self-heal after
    reorganization, so they always match the library payload."""
    user = auth_mod.current_user(request)
    return {"paths": [p.replace("\\", "/") for p in pl_mod.list_likes(user)]}


@app.post("/api/likes/toggle")
def likes_toggle(req: LikeToggleRequest, request: Request = None):
    p = os.path.normpath(mbresolve.resolve_track(req.path) or req.path)
    if not os.path.isfile(p):
        raise HTTPException(404, "file not found")
    # A like row for a path outside the library can never match a track again.
    if not _in_music_folder(p, _music_folder()):
        raise HTTPException(400, "file outside music folder")
    p = p.replace("\\", "/")
    try:
        liked = pl_mod.toggle_like(p, mbid=req.mbid, user=auth_mod.current_user(request))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "liked": liked}


class FavoriteToggleRequest(BaseModel):
    kind: str  # "album" | "artist" | "playlist"
    key: str
    mbid: Optional[str] = None  # release/artist MBID — keeps the favorite stable across moves


@app.get("/api/favorites")
def favorites_list(request: Request = None):
    """Favorite albums / artists / playlists, keyed by path (or playlist id),
    newest first — powers the sidebar Favorites section. MBID-backed rows
    self-heal when files move."""
    return pl_mod.list_favorites(auth_mod.current_user(request))


@app.post("/api/favorites/toggle")
def favorites_toggle(req: FavoriteToggleRequest, request: Request = None):
    # album/artist keys are library folders; "playlist" keys are playlist ids,
    # not paths, so only the path-valued kinds get the containment guard.
    if str(req.kind or "").strip().lower() in ("album", "artist"):
        if not _in_music_folder(req.key, _music_folder()):
            raise HTTPException(400, "folder outside music folder")
    try:
        fav = pl_mod.toggle_favorite(req.kind, req.key, mbid=req.mbid,
                                     user=auth_mod.current_user(request))
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
@job_locks.holds(lambda req: req.paths, kind="tags", label="Bulk tag write")
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
    # A GENRE value is a LIST of canonical names, never one joined string (see
    # `_genre_names`); the cap comes from the one setting, read once for the
    # whole batch.
    genre_cap = _autotag.genre_count(load_config())
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
                if name == "GENRE":
                    # One field per genre name: a dialog that sends
                    # "shoegaze; rock" (or a list) must not end up as a single
                    # genre called that. Canonicalized like every other write.
                    names = _genre_names(val, genre_cap)
                    if names:
                        if af.set_tag(name, names):
                            added += 1
                        else:
                            failed += 1
                            errors.append(f"{os.path.basename(rp)}: {name} was not written")
                    elif name in present and af.delete_tag(name):
                        removed += 1
                    continue
                if isinstance(val, (list, tuple)):
                    # A list means several values for one field (three GENREs,
                    # two ARTISTs): set_tag() writes each as a repeated field.
                    # `str(val)` stored the Python repr as one value, which is
                    # the same defect as the GENRE one above.
                    values = [str(x).strip() for x in val if str(x).strip()]
                    if values:
                        if af.set_tag(name, values):
                            added += 1
                        else:
                            failed += 1
                            errors.append(f"{os.path.basename(rp)}: {name} was not written")
                    elif name in present and af.delete_tag(name):
                        removed += 1
                    continue
                v = str(val).strip()
                if not v:
                    if name in present and af.delete_tag(name):
                        removed += 1
                elif af.set_tag(name, v):
                    added += 1
                else:
                    # A refused write (a custom key on a container that cannot
                    # hold it, a read-only file) used to vanish: the dialog
                    # reported ok=true with "0 set, 0 removed" and no error row,
                    # which reads as "nothing to do" instead of "it failed".
                    failed += 1
                    errors.append(f"{os.path.basename(rp)}: {name} was not written")
            # One write for the whole track: a failed flush (read-only file,
            # full disk) means nothing landed, so it is a failed track — the
            # dialog used to report ok=true with counts that never reached
            # the tags.
            if af.defer_save(False) is False:
                failed += 1
                errors.append(f"{os.path.basename(rp)}: {af.error or 'tag write failed'}")
                continue
            tagcache.invalidate_path(p)
        except Exception as e:
            failed += 1
            errors.append(f"{os.path.basename(rp)}: {e}")
    # Tag writes change file sizes and mtimes, which is what the network's
    # file list shows — refresh the share index once the batch is done
    # (debounced: a 500-track batch asks slskd once, a few seconds later).
    if removed or added:
        _refresh_slskd_shares_soon()
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
        targets = [os.path.normpath(t) for t in req.targets]
        folder = cfg.get("music_folder") or ""
        if folder and os.path.isdir(folder):
            for t in targets:
                if not _in_music_folder(t, folder):
                    raise HTTPException(400, f"target outside music folder: {t}")
        cfg["targets"] = targets
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
    _announce_run(req.ids, results, cfg.get("targets"))
    return {"results": results}


def _announce_run(ids, results, targets=None):
    """One notification per finished script run (see server/events.py).

    A UI run is the user's own long job — grading a big library takes minutes,
    and the request that carries it may outlive their attention — so its
    OUTCOME is announced once, on the bus, instead of only in the response.
    Exactly one frame per run: the failed kind when anything errored, the
    grader's own kind for a pure grade run, the plain kind otherwise. A run
    where every script was skipped has no outcome to report (nothing ran), so
    it says nothing.

    A skipped step is not a failure: `run_script` reports it as `skipped`,
    which is the "this feature is switched off" answer, not an error.
    """
    try:
        from server import script_runners
        failed = [r for r in results if r.get("error")]
        ran = [r for r in results if not r.get("error") and not r.get("skipped")]
        if not failed and not ran:
            return
        names = [str(r.get("label") or f"Script {r.get('id')}") for r in results]
        listing = ", ".join(names[:4]) + (f" +{len(names) - 4}" if len(names) > 4 else "")
        # Where the run's subject is: a single album the user graded is that
        # album's page, everything else is the library-wide view.
        targets = [str(t) for t in (targets or []) if str(t).strip()]
        subject = f"/album/{quote(targets[0], safe='')}" if len(targets) == 1 else "/library"
        if failed:
            first = failed[0]
            events_mod.emit(
                "script_failed",
                f"{first.get('label') or 'Script'} failed",
                "; ".join(str(r.get("error"))[:200] for r in failed[:2]),
                {"link": "/in-progress", "ids": list(ids),
                 "failed": [r.get("id") for r in failed]},
            )
        elif list(ids) == [4]:
            dist = (ran[0].get("stats") or {}).get("grade_dist") or {}
            events_mod.emit(
                "grade_done",
                "Grade finished",
                (f"{dist.get('PASS', 0)} passed, {dist.get('FAIL', 0)} failed"
                 if dist else "The library has been graded"),
                {"link": subject, "ids": list(ids), "grade_dist": dist,
                 "album_path": targets[0].replace("\\", "/") if len(targets) == 1 else ""},
            )
        else:
            events_mod.emit(
                "script_done",
                (f"{names[0]} finished" if len(names) == 1
                 else f"{len(ran)} scripts finished"),
                listing,
                {"link": "/in-progress", "ids": list(ids),
                 "ran": [r.get("id") for r in ran]},
            )
    except Exception:
        # A notification must never turn a finished run into a failed request.
        traceback.print_exc()


# --------------------------------------------------------------------------- #
# Export to device (MP3 player / DAP / USB drive)
# --------------------------------------------------------------------------- #
class ExportRequest(BaseModel):
    paths: List[str] = []              # absolute audio file paths to export
    dest: str = ""                     # destination drive root (e.g. "E:\\")
    subfolder: str = "Music"           # created under the drive root
    codec: str = "copy"                # any key of exporter.CODECS
    quality: str = ""                  # a preset key (V0/320/256/q8/…) or a number
    # Which tree an exported track lands in: a key of exporter.STRUCTURES
    # ("" = the shipped one), "custom" for the script below. The exporter is
    # the authority — an unknown key, a bad %field% or a script that names no
    # path comes back as a 400 sentence.
    structure: str = ""
    # The naming script a "custom" structure evaluates (the same %field% /
    # $if() grammar the library's own naming script uses, mlo.naming).
    structure_script: Optional[str] = None
    # Compatibility options. None = use the saved `export_*` config value, so a
    # client that omits a field keeps the user's defaults instead of forcing
    # the built-in one (server.exporter.EXPORT_DEFAULTS holds both).
    embed_covers: Optional[bool] = None
    embed_cover_jpeg_quality: Optional[int] = None
    embed_cover_resolution: Optional[int] = None
    id3v2: Optional[str] = None
    id3v1: Optional[bool] = None
    # "off" | "tags" (write the ReplayGain tags) | "apply" (rewrite the audio).
    replaygain_mode: Optional[str] = None
    eq_profile: Optional[str] = None   # a preset/profile id, "" = no EQ
    # "embedded" (the LYRICS tag) | "lrc" (a .lrc beside the file) | "both" |
    # None = the saved export_lyrics, else the library's own lyrics_format.
    lyrics: Optional[str] = None
    clean_tags: Optional[bool] = None
    playlists: Optional[bool] = None
    sidecars: Optional[bool] = None
    # WHICH files the run writes: the family keys of exporter.FILE_FAMILIES
    # ("" / absent = the saved export_copy_files, else the `sidecars` switch
    # above, else the tracks alone). The exporter is the authority — an unknown
    # family and an EMPTY selection ([] would write an empty folder) both come
    # back as a 400 sentence.
    copy_files: Optional[List[str]] = None
    manifest: Optional[bool] = None
    verify: Optional[bool] = None
    prune: Optional[bool] = None
    workers: Optional[int] = None
    # "server" writes into dest/subfolder (needs a destination the SERVER can
    # see), "zip" stages the same export under the app's data dir and hands
    # back one archive — the only destination a browser can offer its user.
    target: Optional[str] = None


class EqImportRequest(BaseModel):
    name: str = ""
    text: str = ""

class EqAutoEqRequest(BaseModel):
    # The results-relative directory the search returned
    # (`<source>/<rig>/<model>`), not a URL: the server builds the raw GitHub
    # URL itself and refuses anything that is not a plain relative path.
    id: str = ""
    # Which AutoEq form to fetch first — "parametric" (its own parametric
    # output), "graphic" (the band list it is derived from) or "fixed".
    form: str = "parametric"


class ExportConfigRequest(BaseModel):
    name: str = ""
    # The Export page's form, as the page holds it. A dict rather than one field
    # per option on purpose: the form IS the schema, and
    # server.exportconfigs validates it against the exporter's own tables
    # (EXPORT_DEFAULTS, CODECS, STRUCTURES) — a Pydantic copy here would be a
    # second schema to keep in step with the run.
    config: dict = {}


class StructurePreviewRequest(BaseModel):
    script: str = ""
    ext: str = ""      # the codec's produced extension, for the example path


@app.get("/api/export/defaults")
def export_defaults():
    """The saved export form values — what the Export page loads when it opens
    and what its "Save as default" writes back."""
    cfg = load_config()
    out = {}
    for name in ("dest", "subfolder", "codec", "quality", "structure",
                 "structure_script"):
        out[name] = cfg.get("export_" + name, DEFAULT_CONFIG.get("export_" + name, ""))
    for name, default in exporter.EXPORT_DEFAULTS.items():
        out[name] = cfg.get("export_" + name, default)
    # Lyrics are the one option whose shipped default is NOT a constant: with
    # nothing saved it is the library's own `lyrics_format`, so the form opens
    # showing what the app keeps in the library (see exporter.lyrics_mode) and
    # the select always has a value that matches one of its options.
    out["lyrics"] = exporter.lyrics_mode(cfg, {})
    # …and the file selection is resolved the same way (the run's own resolver,
    # not the raw key): a config that still holds only `export_sidecars` opens
    # the form showing the set that switch stands for, so the page always shows
    # the files the next export would actually write.
    out["copy_files"] = list(exporter.copy_files(cfg, {}))
    return out


@app.get("/api/export/configs")
def export_configs_list():
    """Every saved export configuration, newest first.

    Each row carries the equalizer profile it names and whether that profile is
    still there (`eq_missing` / `eq_problem`): a config outlives the profile it
    was saved with, so the list has to be able to mark one as broken before the
    user picks it."""
    return {"configs": exportconfigs.list_configs(_music_folder())}


@app.post("/api/export/configs")
def export_configs_save(req: ExportConfigRequest):
    """Save the Export page's form under a name (or replace that name's config).

    A name that could name a path outside the config folder, an unknown field,
    and a value a run would refuse (an unknown codec, folder structure, target
    or ReplayGain mode) are 400s: a saved config must not be a way to store
    something an export cannot do."""
    try:
        return exportconfigs.save(_music_folder(), req.name, req.config)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/export/configs/{config_id}")
def export_configs_load(config_id: str):
    """One saved config: the form values a run takes, plus the state of the
    equalizer profile it names.

    A config whose profile has been renamed or deleted comes back with
    `eq_missing` true and `eq_problem` filled — the UI says so and the run
    refuses that profile, rather than the export quietly using another curve. A
    config that is not there is a 404 (the UI may be showing a stale list); an
    id that could name another file is a 400."""
    try:
        row = exportconfigs.load(_music_folder(), config_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if row is None:
        raise HTTPException(404, f"no such saved config: {config_id}")
    return row


@app.delete("/api/export/configs/{config_id}")
def export_configs_delete(config_id: str):
    """Drop one saved configuration. Not there = 404; a path-shaped id = 400."""
    try:
        removed = exportconfigs.delete(_music_folder(), config_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not removed:
        raise HTTPException(404, f"no such saved config: {config_id}")
    return {"ok": True, "id": config_id}


@app.get("/api/export/structures")
def export_structures():
    """The folder-structure menu (keys and labels) and the %fields% /
    $functions a custom structure script may use — the Exporter's own tables,
    so the dropdown cannot offer a structure the run would refuse."""
    return exporter.structure_menu()


@app.get("/api/export/files")
def export_files():
    """The file families a run can be asked to copy (keys, labels, hints) — the
    Exporter's own table, so the checkboxes cannot offer a family the run would
    refuse, and the sentence a refused selection comes back with names the same
    vocabulary."""
    return exporter.file_families()


@app.post("/api/export/structure/preview")
def export_structure_preview(req: StructurePreviewRequest):
    """What a user-typed folder structure would write, for one sample track.

    The Export page calls this while the custom structure is being typed: the
    same grammar and the same validation the run itself applies, so a field the
    app does not know is refused there with the server's own sentence instead
    of being discovered at the end of a run."""
    return exporter.preview_structure(req.script, req.ext)


@app.get("/api/export/drives")
def export_drives():
    """Candidate destination drives with free space and bus type."""
    try:
        return {"drives": exporter.list_drives()}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/export/codecs")
def export_codecs():
    """Export codecs with the quality presets and custom-value ranges the
    Export page offers — the server's tables are the single source of truth,
    so adding a codec needs no UI change."""
    return {"codecs": exporter.codec_specs()}


@app.get("/api/export/eq")
def export_eq():
    """The equalizer profiles an export can carry: the built-in presets plus
    everything the user imported, with the filter chain each one renders to."""
    return eq_mod.catalog(_music_folder())


@app.post("/api/export/eq/import")
def export_eq_import(req: EqImportRequest):
    """Store one pasted Equalizer APO / Peace profile and return its row.

    A name that could name a path outside the profile folder, a name that is a
    built-in preset's, or a body past the size cap is a 400 — never a file
    written somewhere else."""
    try:
        return eq_mod.import_profile(_music_folder(), req.name, req.text)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/export/eq/{profile_id}")
def export_eq_delete(profile_id: str):
    """Drop one imported profile. A profile that is not there is a 404 (the UI
    may be showing a stale list); a name that could name another file is a 400."""
    try:
        removed = eq_mod.delete_profile(_music_folder(), profile_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not removed:
        raise HTTPException(404, f"no such profile: {profile_id}")
    return {"ok": True, "id": profile_id}


@app.get("/api/eq/autoeq/search")
def eq_autoeq_search(q: str = Query(""), limit: int = Query(40),
                     refresh: bool = Query(False)):
    """Search AutoEq's measured headphones by name.

    The catalogue itself is the project's INDEX.md, cached beside the profiles
    for a month (mlo.eq.autoeq_index): fetching a megabyte per keystroke is not
    a search box. `refresh=1` re-fetches it — the button the page offers when a
    headphone the user owns is missing — and a failed refresh still answers with
    the cached rows, with the reason in `error`."""
    index = eq_mod.autoeq_index(_music_folder(), refresh=bool(refresh))
    rows = eq_mod.autoeq_search(q, index["rows"], limit=max(1, min(200, int(limit or 40))))
    return {"rows": rows, "models": len(index["rows"]),
            "fetched_at": index["fetched_at"], "error": index["error"]}


@app.post("/api/eq/autoeq/import")
def eq_autoeq_import(req: EqAutoEqRequest):
    """Fetch one AutoEq correction and store it as a profile.

    The row that comes back is the stored profile — the same shape every other
    profile has, so the caller can select it immediately. An id that is not a
    results-relative directory, a fetch that fails, or a file this parser will
    not accept is a 400/502 with the reason; the profile is never stored half
    read."""
    try:
        return eq_mod.autoeq_import(_music_folder(), req.id, req.form)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"AutoEq fetch failed: {e}")


@app.get("/api/export/zip/{zip_id}")
def export_zip_download(zip_id: str):
    """Stream the archive a zip export built.

    An unknown id (a restart, a newer export that replaced it) is a 404 rather
    than an empty file: the client can say the export is gone instead of saving
    a zero-byte download. The response carries the archive's own human name as
    the downloaded file name."""
    path = exporter.zip_path(load_config(), zip_id)
    if not path:
        raise HTTPException(404, "no export archive with that id")
    return FileResponse(path, media_type="application/zip",
                        filename=os.path.basename(path))


@app.delete("/api/export/zip/{zip_id}")
def export_zip_delete(zip_id: str):
    """Drop the built archive early (it is replaced by the next zip export)."""
    if not exporter.drop_zip(load_config(), zip_id):
        raise HTTPException(404, "no export archive with that id")
    return {"ok": True, "id": zip_id}


def _export_claim_paths(req):
    """What one export HOLDS for the whole call: the source tracks, and the
    destination it writes into.

    The sources were always held (an export reads the library files it was
    pointed at, and a script run rewriting them mid-copy is the collision the
    lock exists to prevent). The DESTINATION was not, and that is a real race
    the owner asked about: two exports into one root run `_copy_once` over the
    same names at once, and the second run's `prune` deletes audio the first
    just wrote. A destination outside the library never collides with a script
    run, so holding it costs nothing elsewhere — and two exports to one drive
    now answer 409 instead of fighting."""
    paths = list(getattr(req, "paths", None) or [])
    target = str(getattr(req, "target", "") or "server").strip().lower()
    if target == "server" and str(getattr(req, "dest", "") or "").strip():
        sub = str(getattr(req, "subfolder", "") or "").replace("\\", "/").strip("/")
        paths.append(os.path.join(str(req.dest), *([sub] if sub else [])))
    return paths


@app.post("/api/export/cancel")
def export_cancel():
    """Ask the running export to stop (see exporter.request_cancel).

    Stops at the next file boundary, keeps everything already written, and
    reports `cancelled: true` in that run's result. `cancelled` here answers
    whether a run was in flight at all — a press when nothing is exporting says
    so instead of pretending."""
    stopped = exporter.request_cancel()
    return {"ok": True, "cancelled": stopped}


@app.post("/api/export")
@job_locks.holds(_export_claim_paths, kind="export", label="Export")
def export_run(req: ExportRequest):
    """Copy/transcode the selected tracks onto the target drive. Runs in the
    worker thread pool (sync def) and reports progress via the shared hook,
    so the header progress bar behaves exactly like a library script run.

    The SOURCE tracks and the DESTINATION are held (see _export_claim_paths)."""
    if not req.paths:
        raise HTTPException(400, "no tracks selected")
    target = (req.target or "server").strip().lower()
    if target not in exporter.TARGETS:
        raise HTTPException(400, f"unknown export target: {req.target}")
    dest_root = ""
    if target == "server":
        # A zip export ignores dest (it stages under the app's own data dir),
        # so only the drive target has a destination to check.
        dest_root = os.path.abspath(req.dest)
        if not os.path.isdir(dest_root):
            raise HTTPException(400, f"destination not found: {req.dest}")
    for p in req.paths:
        if not _in_music_folder(p, _music_folder()):
            raise HTTPException(400, f"file outside music folder: {p}")
    if req.codec not in exporter.CODECS:
        raise HTTPException(400, f"unknown codec: {req.codec}")
    cfg = load_config()
    opts = {k: v for k, v in req.model_dump().items()
            if k not in exporter.FORM_FIELDS and v is not None}
    try:
        res = exporter.export_tracks(
            cfg, [os.path.normpath(p) for p in req.paths], dest_root,
            subfolder=req.subfolder, codec=req.codec, quality=req.quality,
            structure=req.structure, structure_script=req.structure_script or "",
            **opts,
        )
    except ValueError as e:      # an unusable destination (inside the library…)
        raise HTTPException(400, str(e))
    if res.get("zip"):
        # The archive is fetched by id, so the client needs the URL to hand the
        # browser (a download, or a link the user can click).
        res["zip"]["url"] = f"/api/export/zip/{res['zip']['id']}"
    # A cancelled run is not a success: it copied what it copied and stopped,
    # and `ok` is what every caller reads as "the export is done".
    ok = res["failed"] == 0 and not res.get("cancelled")
    return {"ok": ok, **res}


# --------------------------------------------------------------------------- #
# Playlists
# --------------------------------------------------------------------------- #
@app.get("/api/playlists")
def playlists_list(request: Request = None):
    return pl_mod.list_playlists(auth_mod.current_user(request))


@app.post("/api/playlists")
def playlists_create(req: PlaylistCreate, request: Request = None):
    if not req.name.strip():
        raise HTTPException(400, "name required")
    user = auth_mod.current_user(request)
    pid = pl_mod.create_playlist(req.name.strip(), req.kind, req.filter, user)
    return pl_mod.get_playlist(pid, user)


@app.get("/api/playlists/{pid}")
def playlists_get(pid: int, request: Request = None):
    pl = pl_mod.get_playlist(pid, auth_mod.current_user(request))
    if pl is None:
        raise HTTPException(404, "playlist not found")
    return pl


@app.patch("/api/playlists/{pid}")
def playlists_rename(pid: int, req: PlaylistUpdate, request: Request = None):
    # only the fields the client actually sent (icon: null CLEARS the icon)
    fields = {k: getattr(req, k) for k in req.model_fields_set}
    user = auth_mod.current_user(request)
    if not pl_mod.update_playlist(pid, fields, user):
        raise HTTPException(404, "playlist not found")
    return pl_mod.get_playlist(pid, user)


@app.delete("/api/playlists/{pid}")
def playlists_delete(pid: int, request: Request = None):
    if not pl_mod.delete_playlist(pid, auth_mod.current_user(request)):
        raise HTTPException(404, "playlist not found")
    return {"ok": True}


@app.post("/api/playlists/{pid}/tracks")
def playlists_add(pid: int, req: PlaylistTracks, request: Request = None):
    user = auth_mod.current_user(request)
    if pl_mod.get_playlist(pid, user) is None:
        raise HTTPException(404, "playlist not found")
    paths = [os.path.normpath(p) for p in req.paths]
    for p in paths:
        if not _in_music_folder(p, _music_folder()):
            raise HTTPException(400, f"file outside music folder: {p}")
    n = pl_mod.add_tracks(pid, paths, req.position, user)
    return {"added": n}


@app.put("/api/playlists/{pid}/tracks")
def playlists_order(pid: int, req: PlaylistTracks, request: Request = None):
    """Full reorder: body paths replace the playlist order entirely."""
    user = auth_mod.current_user(request)
    if pl_mod.get_playlist(pid, user) is None:
        raise HTTPException(404, "playlist not found")
    paths = [os.path.normpath(p) for p in req.paths]
    for p in paths:
        if not _in_music_folder(p, _music_folder()):
            raise HTTPException(400, f"file outside music folder: {p}")
    pl_mod.set_order(pid, paths, user)
    return {"ok": True}


@app.delete("/api/playlists/{pid}/tracks")
def playlists_remove(pid: int, req: PlaylistTracks, request: Request = None):
    user = auth_mod.current_user(request)
    if pl_mod.get_playlist(pid, user) is None:
        raise HTTPException(404, "playlist not found")
    pl_mod.remove_tracks(pid, [os.path.normpath(p) for p in req.paths], user)
    return {"ok": True}


@app.post("/api/playlists/{pid}/filter")
def playlists_filter(pid: int, req: SmartFilter, request: Request = None):
    user = auth_mod.current_user(request)
    pl = pl_mod.get_playlist(pid, user)
    if pl is None:
        raise HTTPException(404, "playlist not found")
    if pl["kind"] != "smart":
        raise HTTPException(400, "not a smart playlist")
    pl_mod.set_smart_filter(pid, req.filter, user)
    return pl_mod.get_playlist(pid, user)


@app.post("/api/playlists/{pid}/evaluate")
def playlists_evaluate(pid: int, request: Request = None):
    user = auth_mod.current_user(request)
    pl = pl_mod.get_playlist(pid, user)
    if pl is None:
        raise HTTPException(404, "playlist not found")
    if pl["kind"] != "smart":
        raise HTTPException(400, "not a smart playlist")
    library = lib_mod.build_library(load_config())
    hits = pl_mod.evaluate_smart(pid, library, user=user)
    return {"paths": hits or []}


@app.get("/api/playlists/{pid}/export")
def playlists_export(pid: int, request: Request = None):
    user = auth_mod.current_user(request)
    content = pl_mod.export_m3u8(pid, user)
    if content is None:
        raise HTTPException(404, "playlist not found")
    pl = pl_mod.get_playlist(pid, user)
    name = re_safe_filename(pl["name"]) or "playlist"
    return PlainTextResponse(
        content,
        media_type="audio/x-mpegurl",
        headers={"Content-Disposition": f'attachment; filename="{name}.m3u8"'},
    )


@app.post("/api/playlists/import")
async def playlists_import(name: str = Query(...), file: UploadFile = File(...),
                           request: Request = None):
    content = (await file.read()).decode("utf-8", errors="replace")
    base = load_config().get("music_folder") or os.getcwd()
    user = auth_mod.current_user(request)
    pid = pl_mod.import_m3u8(name.strip() or file.filename or "imported", content, base, user)
    return pl_mod.get_playlist(pid, user)


def re_safe_filename(name):
    """Older, weaker spelling of the app's ONE filename rule.

    Kept as a name (the routes below and their tests call it) but it is now
    exactly mlo.naming.sanitize_segment: a character that is invalid in a name
    becomes "_", a trailing dot/space becomes "_" instead of hiding until the
    filesystem drops it, and a reserved device name gets a trailing "_". Two
    conventions for one character set is how a path the app WROTE stops being
    a path the app can find again."""
    return sanitize_segment(name)


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
        release = intg.release_lookup(rid)
        # The tracklist's aliases, in ONE browse call (MusicBrainz carries none
        # inside the lookup): the page renders each track's title with the name
        # it is also known by, exactly as the artist and release rows do.
        intg.attach_recording_aliases(release, rid)
        return release
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


# ---- release-group browse (the import wizard's release picker) -------------
@app.get("/api/mb/release-group/{mbid}")
def mb_release_group(mbid: str, limit: int = Query(300), offset: int = Query(0)):
    rid = intg._mbid(mbid)
    if not rid:
        raise HTTPException(400, "invalid MusicBrainz ID or URL")
    try:
        return intg.release_group_browse(rid, max(1, limit), max(0, offset))
    except Exception as e:
        raise HTTPException(502, f"MusicBrainz lookup failed: {e}")


# ---- generic MusicBrainz browser (search + entity pages) -------------------
@app.get("/api/mb/search/fields")
def mb_search_fields():
    """The fields the search box may put in a query, and the syntax to use
    them: MusicBrainz's own index fields per entity kind (field, kind, example,
    whether it takes quotes, and what it means), served from the same dict the
    server's own query builder is written against, so the UI's completion and
    its help can never offer a field the index would not answer."""
    return intg.search_help()


@app.get("/api/mb/search")
def mb_search(q: str = Query(""), type: str = Query("release"),
              limit: int = Query(100), offset: int = Query(0),
              mode: str = Query("free"), primary_type: str = Query(""),
              secondary_type: str = Query(""), artist: str = Query(""),
              year: str = Query(""), label: str = Query(""), catno: str = Query("")):
    """Search MusicBrainz for the in-app browser: type = artist |
    release-group | release | recording; mode = free | catno | barcode
    (catno/barcode only apply to releases); primary_type/secondary_type narrow
    releases and release groups to MusicBrainz release types (Album, EP,
    Single, Soundtrack, Live, ...). artist / year / label / catno are further
    constraints and COMBINE with all of the above — the whole query goes to
    the index, which is the only way to answer "albums by X from 1999 on
    label Y". `q` may be empty when a constraint says enough (a label/year
    browse). Returns {rows, total, offset, next, query}: `next` is the offset
    of the following page (null at the end) and `query` the Lucene query the
    index was asked, so the UI can show it.

    `q` travels to the index AS THE USER WROTE IT — `artist:"Radiohead" AND
    releasegroup:"OK Computer"` is a query the index answers, not a phrase to
    search for, so the app neither escapes nor re-quotes it (see
    `intg.search_query`, the one place that says what the app does to it:
    trim, and AND the constraint boxes on). A query MusicBrainz itself
    refuses answers 400 carrying MusicBrainz's own words, which the browser
    shows verbatim rather than a generic failure.

    `GET /api/mb/search/fields` serves the catalogue of what may go in it."""
    if type not in intg.MB_ENTITIES:
        raise HTTPException(400, "type must be one of " + ", ".join(intg.MB_ENTITIES))
    if mode not in ("free", "catno", "barcode"):
        raise HTTPException(400, "mode must be free, catno or barcode")
    if not (q.strip() or any(x.strip() for x in
                             (primary_type, secondary_type, artist, year, label, catno))):
        raise HTTPException(400, "q or a search constraint is required")
    try:
        return intg.search_mb(type, q, limit, mode, max(0, offset),
                              primary_type=primary_type.strip(),
                              secondary_type=secondary_type.strip(),
                              artist=artist.strip(), year=year.strip(),
                              label=label.strip(), catno=catno.strip())
    except intg.MusicBrainzError as e:
        # MusicBrainz REFUSED this query and said why (its search server names
        # the reason in the body). That is the request's fault, not an outage:
        # 400 with MusicBrainz's own text, never "MusicBrainz search failed".
        if e.status and 400 <= int(e.status) < 500:
            raise HTTPException(400, str(e))
        raise HTTPException(502, f"MusicBrainz search failed: {e}")
    except Exception as e:
        raise HTTPException(502, f"MusicBrainz search failed: {e}")


@app.get("/api/mb/artist/{mbid}")
def mb_artist(mbid: str, limit: int = Query(300), offset: int = Query(0),
              primary_type: str = Query(""), secondary_type: str = Query("")):
    """Artist page: identity + a page of release groups. A type filter is
    answered by the search index (browse cannot filter by type at all), so
    the discography it reports is the whole one, not the loaded window."""
    rid = intg._mbid(mbid)
    if not rid:
        raise HTTPException(400, "invalid MusicBrainz ID or URL")
    try:
        return intg.artist_browse(rid, max(1, limit), max(0, offset),
                                  primary_type.strip(), secondary_type.strip())
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


def _scan_album_tracks(album_dir, lyrics=False):
    """Recursively list audio files in an album folder with tags + tech info.

    `lyrics=True` adds the per-track lyrics state (see `_add_lyrics_state`) —
    the wizard's per-track steps read this payload for an album the library
    tree does not list yet (a staged import)."""
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
    if lyrics:
        _add_lyrics_state(tracks)
    return tracks


def _add_lyrics_state(tracks):
    """Mark scanned tracks with the lyrics they actually have.

    A scan used to carry no lyrics fields, so the import wizard's per-track
    steps hardcoded "no lyrics" and every track of a staged album claimed
    lyrics it already carried. The flags come from `mlo.grader._grade_album`,
    the one place that decides what counts as lyrics (an embedded
    LYRICS/UNSYNCEDLYRICS tag, and an .lrc that holds text and is not shared
    with a same-stem sibling), called per FOLDER because a disc folder is its
    own album to the grader. A folder the grader cannot read leaves the flags
    unset rather than claiming anything.
    """
    from mlo import grader

    cfg = load_config()
    fmt = str(cfg.get("lyrics_format") or "EMBEDDED")
    graded = {}
    for folder in {os.path.dirname(t["path"]) for t in tracks}:
        try:
            res = grader._grade_album(folder, fmt, cfg)
        except Exception:
            continue
        for row in (res or {}).get("tracks") or []:
            graded[os.path.normpath(os.path.join(folder, row.get("file") or ""))] = row
    for t in tracks:
        row = graded.get(os.path.normpath(t["path"])) or {}
        t["lyrics_embedded"] = bool(row.get("lyrics_embedded"))
        t["lyrics_lrc"] = bool(row.get("lyrics_lrc"))
        # The wizard's per-track lyrics step reads the KIND the same way the
        # library payload carries it (mlo.grader stamped both from one read of
        # the two stored texts), and presence is the kind not being null — so a
        # track the wizard shows as "Plain" is "plain" in the library too.
        t["lyrics_kind"] = row.get("lyrics_kind") or None
        t["lyrics_present"] = bool(t["lyrics_kind"])


@app.post("/api/mb/match")
def mb_match(req: MatchRequest):
    """Suggest release track/disc matches for local files in an album."""
    album_dir = os.path.normpath(req.album_path)
    if not os.path.isdir(album_dir):
        raise HTTPException(404, "album not found")
    _guard_folder(album_dir, req.staged, "album")
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
@job_locks.holds(lambda req: list((req.tracks or {}).keys()), kind="tags",
                 label="Write MB links")
def mb_assign(req: AssignTagsRequest):
    """Write per-track MB/RYM link tags. tracks: {path: {TAG: value}}.

    Videos are re-emitted losslessly, so a raw container comes back as a
    same-stem MKV: `container_changed`/`output_paths` name those files."""
    from mlo.audio import AudioFile
    # honey: unbounded dict = unbounded tag writes per request; 500 is far
    # above any wizard batch, raise it if a real flow ever hits the cap.
    if len(req.tracks or {}) > 500:
        raise HTTPException(400, "too many tracks (max 500 per request)")
    # GENRE can be assigned through this route (the wizard's tag step writes
    # it here), so the canonicalization cap comes from mb_genre_count once for
    # the whole request.
    genre_cap = _autotag.genre_count(load_config())
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
        if not _in_music_folder(fp, folder) and not _allow_staged(fp, req.staged):
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
            # GENRE is canonicalized like any other write, but an MKV holds
            # ONE genre string (the batch writer takes a value per key), so
            # the list goes in joined with "; " — the spelling tag_values()
            # reads back as the same names (see _genre_names).
            clean = {}
            for k, v in tag_map.items():
                if str(k).upper() == "GENRE":
                    names = _genre_names(v, genre_cap)
                    if names:
                        clean[k] = "; ".join(names)
                    continue
                if str(v or "").strip():
                    clean[k] = v
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
                if str(k).upper() == "GENRE":
                    # GENRE is the one tag whose value is a LIST: every writer
                    # stores one repeated field per genre, so a value that
                    # arrives as a list, or as one "A; B" string the caller
                    # joined, is split and canonicalized here (an empty one
                    # deletes the tag). `str(v)` wrote the whole thing as a
                    # single genre literally named "['shoegaze', 'rock']".
                    names = _genre_names(v, genre_cap)
                    if names:
                        if not af.set_tag("GENRE", names):
                            errors.append(f"{p} GENRE: {af.error or 'write failed'}")
                    elif not af.delete_tag("GENRE"):
                        errors.append(f"{p} GENRE: {af.error or 'delete failed'}")
                    continue
                if isinstance(v, (list, tuple)):
                    # Several values for one field: set_tag() writes each as a
                    # repeated field, where `str(v)` stored the Python repr as
                    # one value (the GENRE case above, for every other tag).
                    values = [str(x).strip() for x in v if str(x).strip()]
                    if values and not af.set_tag(k, values):
                        errors.append(f"{p} {k}: {af.error or 'write failed'}")
                    elif not values and not af.delete_tag(k):
                        errors.append(f"{p} {k}: {af.error or 'delete failed'}")
                    continue
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
    # Partial success is the norm (one unreadable file must not discard the
    # rest): always 200 with the capped list, never 500 — callers show `errors`.
    return {"ok": not errors, "changed": changed, "errors": errors[:20],
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
    staged: bool = False  # the import wizard's not-yet-imported album


@app.post("/api/lyrics/write")
@job_locks.holds(lambda req: [req.path], kind="lyrics", label="Lyrics write")
def lyrics_write(req: LyricsWriteRequest):
    """Write .lrc sidecar atomically, canonicalized via mlo.lyrics."""
    from mlo.lyrics import _format_for_storage
    p = os.path.normpath(mbresolve.resolve_track(req.path) or req.path)
    if not os.path.isfile(p):
        raise HTTPException(404, "audio file not found")
    _guard_folder(p, req.staged, "file")
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
    # The community-copy rule is the DEFAULT, not a wall: a person submitting
    # their OWN lyrics (a correction, a better sync, a track they recorded)
    # must be able to say so. Set on the second, explicitly-confirmed press.
    force: bool = False


@app.post("/api/lyrics/publish")
async def lyrics_publish(req: LyricsPublishRequest):
    """Submit lyrics to LRCLIB on behalf of a library track.

    The editor sends the exact text it shows; plain vs synced is detected
    from [mm:ss.xx] timestamps so pasting either form just works.

    A recording LRCLIB already answers for is not submitted by default: that
    copy is the community's, and this app only gives the database what it is
    missing (the same rule script 18 enforces, checked with the same
    exact-then-search match script 18 uses, through the app's own LRCLIB
    client). `force: true` is the manual override — the editor offers it on a
    second, explicitly-confirmed press — and then the submission is made and
    LRCLIB's own answer is reported verbatim: "LRCLIB already has this track"
    is the database refusing a duplicate, not a failure of this app."""
    from server.integrations import lrclib_get, lrclib_publish

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
    if artist and track:
        try:
            existing = await asyncio.to_thread(
                lrclib_get, artist, track, album or None, duration)
        except Exception:
            # An unreachable lookup is not an answer: let the submission
            # itself be the thing that reports the network failure.
            existing = None
        if existing and not req.force:
            return {"ok": False, "exists": True,
                    "message": "LRCLIB already has lyrics for this track — "
                               "nothing was published"}
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
    # `forced` says the caller overrode the community-copy rule, so the UI can
    # keep the difference between "submitted" and "submitted against an
    # existing entry" visible after the fact.
    return {"ok": ok, "message": msg, "forced": bool(req.force)}


@app.get("/api/rym/validate")
def rym_validate(url: str = Query(...)):
    """Is this a RateYourMusic link, and WHICH page is it?

    `valid` keeps its old meaning ("a RYM URL") for callers that only accept
    or reject a paste; `kind` is what lets the link editor put an artist page
    in the artist field instead of storing it as the album link — a song page
    written into RATEYOURMUSIC_ALBUM would look resolved forever and block the
    automatic album lookup.
    """
    kind = intg.rym_url_kind(url)
    return {"valid": kind is not None, "kind": kind}


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
# Credits / performers
# --------------------------------------------------------------------------- #
def _credit_files(album_dir, limit=60):
    """The album's audio files, capped.

    Credits are stated per RELEASE, so a box set of a thousand files must not
    cost a thousand tag reads: the MBID and the tag fallback come from the
    first handful of tracks, which carry the same album tags anyway.
    """
    skip = _skip_names()
    out = []
    for root, dirs, files in os.walk(album_dir):
        dirs[:] = [d for d in dirs if d.lower() not in skip]
        for f in sorted(files):
            if is_audio_file(f):
                out.append(os.path.join(root, f))
                if len(out) >= limit:
                    return out
    return out


def _mb_credit_rows(fetch, mbid):
    """MusicBrainz credits, or a 502 carrying MusicBrainz's own reason.

    An unavailable or busy MusicBrainz is MB's answer, not a bug in here, so
    it must never surface as a 500: 502 + the reason is the same report the
    rest of the UI gives for the same outage.
    """
    try:
        return fetch(mbid)
    except Exception as e:
        raise HTTPException(502, f"MusicBrainz credits unavailable: {e}")


@app.get("/api/credits")
def credits(path: str = Query(""), album: str = Query("")):
    """Credits / performers for one track (`path`) or a whole album (`album`).

    MusicBrainz recording `artist-rels` are the answer: each relation states
    the role itself (performer, instrument, vocal, producer, engineer, mix,
    mastering, arranger, composer, lyricist, conductor, remixer…), the
    instrument or vocal part in its attributes, and the person's own name and
    MBID — and one cached RELEASE request covers every track of an album. A
    file with no MusicBrainz ID, or a release MB reports no relation for, is
    answered from the files' own credit tags (`source: "tags"`) so the panel
    is never empty. Rows are `{role, attributes[], artist, mbid}`.
    """
    folder = _music_folder()
    source = "musicbrainz"
    rows, artist, album_name, track_mbid, release_mbid = [], "", "", "", ""
    if path:
        p = os.path.normpath(mbresolve.resolve_track(path) or path)
        if not os.path.isfile(p):
            raise HTTPException(404, "file not found")
        if not _in_music_folder(p, folder):
            raise HTTPException(400, "file outside music folder")
        tags = tagcache.read_track(p, None)[0] or {}
        artist = str(tags.get("ARTIST") or tags.get("ALBUMARTIST") or "").strip()
        album_name = (str(tags.get("ALBUM") or "").strip()
                      or os.path.basename(os.path.dirname(p)))
        track_mbid = intg._mbid(tags.get("MUSICBRAINZ_TRACKID")) or ""
        if track_mbid:
            rows = _mb_credit_rows(intg.recording_credits, track_mbid)
        if not rows:
            source, rows = "tags", intg.credit_rows_from_tags(tags)
    elif album:
        d = os.path.normpath(mbresolve.resolve_album(album) or album)
        if not os.path.isdir(d):
            raise HTTPException(404, "album not found")
        if not _in_music_folder(d, folder):
            raise HTTPException(400, "album outside music folder")
        files = _credit_files(d)
        if not files:
            raise HTTPException(404, "no audio files in this album")
        tagsets = [tagcache.read_track(f, None)[0] or {} for f in files]
        album_name = os.path.basename(d)
        for t in tagsets:
            artist = artist or str(t.get("ALBUMARTIST") or t.get("ARTIST")
                                   or "").strip()
            release_mbid = (release_mbid
                            or intg._mbid(t.get("MUSICBRAINZ_ALBUMID")) or "")
            if artist and release_mbid:
                break
        if release_mbid:
            rows = _mb_credit_rows(intg.release_credits, release_mbid)
        if not rows:
            source = "tags"
            for t in tagsets:
                rows += intg.credit_rows_from_tags(t)
    else:
        raise HTTPException(404, "path or album is required")
    rows = intg.tidy_credit_rows(rows)
    if not rows and not (track_mbid or release_mbid):
        raise HTTPException(404, "no MusicBrainz id and no credit tags on "
                                 + ("this track" if path else "these files"))
    return {"artist": artist, "album": album_name, "rows": rows,
            "source": source,
            "track_mbid" if path else "release_mbid": track_mbid or release_mbid}


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #
def is_audio_file(name):
    """Library tracks: audio + music-video containers (mlo.paths definition,
    so organize / genre import / MB matching treat videos as tracks too)."""
    from mlo.paths import LIB_AUDIO_EXTS
    return os.path.splitext(name)[1].lower() in LIB_AUDIO_EXTS


@app.post("/api/album/remove")
@job_locks.holds(lambda req, **_: [req.path], kind="remove",
                 label="Remove from library")
def album_remove(req: AlbumRemove, request: Request = None):
    """Remove an album from the library by moving it into
    <music_folder>/.mlo/trash/<user>/ (recoverable, nothing is deleted)."""
    cfg = load_config()
    folder = cfg.get("music_folder") or ""
    if not folder or not os.path.isdir(folder):
        raise HTTPException(400, "music_folder not set or not found")
    p = os.path.normpath(req.path)
    if not os.path.isdir(p):
        raise HTTPException(404, "album not found")
    if not _in_music_folder(p, folder):
        raise HTTPException(400, "album outside music folder")
    # One helper for the whole bin: mlo.paths.trash_path picks the collision
    # name, moves the folder and records its origin in the bin's manifest, so
    # this route and the layout script's removal (mlo.layout, script 20) can
    # never disagree about where an entry landed or how it is spelled.
    name = os.path.basename(p) or "album"
    dest = trash_path(p, folder, auth_mod.current_user(request))
    if not dest:
        # trash_path retried every lock/sharing violation away; a player or
        # an importer still holds a file in the album open.
        raise HTTPException(
            500,
            f"could not move {name} to the trash — a file inside it is still "
            f"in use (stop playback and retry)")
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
#
# The trailing bracket groups are the current default script's optional
# ` [label] [release id]` segments (the older shape ended at the braces), and
# a folder with no date or no braces keeps its raw basename — the label is
# cosmetic, so a shape this regex does not know is left alone.
_ALBUM_NAME_RE = re.compile(
    r"^\[Album\]\s*\d{4}-\d{2}-\d{2}\s*-\s*(?:\d{4}-\d{2}-\d{2}\s*-\s*)?"
    r"(.+?)\s*\{[^{}]*\}(?:\s*\[[^\[\]]*\])*\s*$")


def _trash_dir(folder, user=""):
    return os.path.normpath(trash_dir(folder, user))


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
def trash_list(request: Request = None):
    """Contents of <music_folder>/.mlo/trash/<user>, newest entry first.

    Each user has a bin of their own; the empty scope (an unclaimed install,
    and a session older than the users table) reads `.../trash/default` — the
    segment a pre-users bin at `.../trash` was migrated into."""
    folder = load_config().get("music_folder") or ""
    trash = _trash_dir(folder, auth_mod.current_user(request)) if folder else ""
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
def trash_delete(req: TrashDelete = TrashDelete(), request: Request = None):
    """Permanently delete trash entries by basename. Unknown names and
    refused names land in `failed`; nothing else is an error."""
    import shutil
    cfg = load_config()
    folder = cfg.get("music_folder") or ""
    if not folder or not os.path.isdir(folder):
        raise HTTPException(400, "music_folder not set or not found")
    trash = _trash_dir(folder, auth_mod.current_user(request))
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
def trash_restore(req: TrashRestore = TrashRestore(), request: Request = None):
    """Move trash entries back into the library: each returns to the location
    it was trashed from, or — when it has no manifest record — into `dest`.
    Per-entry problems land in `failed`; an unusable `dest` is a 400 for the
    whole request, so a bad request never moves half a batch."""
    cfg = load_config()
    folder = cfg.get("music_folder") or ""
    if not folder or not os.path.isdir(folder):
        raise HTTPException(400, "music_folder not set or not found")
    trash = _trash_dir(folder, auth_mod.current_user(request))
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
def album_scan_tracks(path: str = Query(...), staged: bool = Query(False)):
    """Direct folder scan: audio files with tags (independent of the library).

    Carries the per-track lyrics state as well (`lyrics_embedded`,
    `lyrics_lrc`, `lyrics_present`), because the import wizard's per-track
    steps run on albums the library tree does not list yet — `staged=true` is
    what the wizard sends for exactly that."""
    p = os.path.normpath(path)
    if not os.path.isdir(p):
        raise HTTPException(404, "folder not found")
    _guard_folder(p, staged, "folder")
    return {"path": p.replace("\\", "/"), "tracks": _scan_album_tracks(p, lyrics=True)}


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
@job_locks.holds(lambda req: req.paths, kind="beets", label="Beets tagging")
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
    """A manual search: free text, or a MusicBrainz id.

    `query` is what the search box carries; `mbid` (a recording id — what the
    library stores per track — or a release/release-group id) asks for ONE
    track (or release) by identity instead, which is what a user who is missing
    a single song has. Either may be given; `mbid` wins when it is set."""
    query: str = ""
    mbid: str = ""


class SoulseekDownloadRequest(BaseModel):
    username: str
    files: List[dict]  # [{filename, size}]


@app.get("/api/soulseek/status")
def soulseek_status():
    """Managed slskd availability, running state, login and download dir."""
    return soulseek_status_payload()


def soulseek_status_payload():
    """The status the tab's dot is drawn from (one definition for the route
    and the change watcher below, so the pushed frame can never drift from
    what a refresh would fetch)."""
    from server import soulseek
    cfg = load_config()
    # `live_user` is whoever slskd is signed in as — ours or a foreign
    # instance's (see instance_owner) — so the tab can name the account.
    owns, live_user, conflict = soulseek.instance_owner(cfg)
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
        "conflict_username": live_user,
        "server": server,
        "download_dir": soulseek.download_dir(cfg),
        "web_port": int(cfg.get("soulseek_web_port") or 5030),
        "listen_port": int(cfg.get("soulseek_listen_port") or 50000),
        # saved credentials — the Soulseek tab prefills the login form and
        # slskd auto-connects with them at every start
        "username": str(cfg.get("soulseek_username") or ""),
        # the account slskd is ACTUALLY signed in as (GET /application's
        # user.username, else GET /options' soulseek.username). A saved
        # username and a live one drift apart the moment the login is
        # corrected on slskd's own page, and the tab must show what the
        # network sees, not what the config file remembers.
        "account": live_user or "",
        "password": str(cfg.get("soulseek_password") or ""),
        "has_credentials": bool(str(cfg.get("soulseek_username") or "").strip()
                                and cfg.get("soulseek_password")),
        "autostart": bool(cfg.get("soulseek_autostart", True)),
        "share_dirs": soulseek.share_dirs(cfg),
        # The LISTEN port's real state: whether anything accepts on it here,
        # who holds it, and what the router was actually told (mlo.portmap).
        # A mapping is only reported as made when a router confirmed it, so
        # the tab can say "opened" only when that is true.
        "listen_port_state": soulseek.port_status_payload(cfg),
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
def soulseek_shares(probe: int = 0):
    """Share configuration (la musica settings are the source of truth —
    the slskd yaml is regenerated from them) plus slskd's live scan state.

    `audit` says what other users can actually see right now, and `probe=1`
    additionally pulls slskd's own share index (a large library's index is tens
    of megabytes) and looks for a file that is on disk.

    503 when slskd is down, exactly like the transfer routes below: the daemon's
    own state is part of this answer (`shares_state`, the audit's scan rows), and
    asking a port nothing listens on used to escape as a 500 — `httpx`'s
    ConnectError out of the route, which reads as a broken app rather than as
    "start slskd". The app's own share SETTINGS are not lost by that refusal:
    they live in the config (`dirs`/`exclude` above are read from it, not from
    the daemon), and Settings → Soulseek is where they are edited."""
    from server import soulseek
    cfg = load_config()
    if not (soulseek.is_running() or soulseek.web_up(cfg)):
        raise HTTPException(503, "slskd is not running — start it first")
    return {
        "dirs": soulseek.share_dirs(cfg),
        "exclude": [x.strip("'") for x in soulseek.share_exclude(cfg)],
        "share_library": bool(cfg.get("soulseek_share_library", True)),
        "autostart": bool(cfg.get("soulseek_autostart", True)),
        "slskd": soulseek.shares_state(cfg),
        "audit": soulseek.share_audit(cfg, probe=bool(probe)),
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
    """Ask slskd to rescan its share index (picks up library changes).

    slskd's own reason is republished instead of a bare 500: a rescan it
    refused (409, one is already running) or an API that failed must not look
    like a scan that started."""
    from server import soulseek
    if not (soulseek.is_running() or soulseek.web_up(load_config())):
        raise HTTPException(400, "slskd is not running")
    try:
        soulseek.rescan_shares()
    except soulseek.SlskdError as e:
        raise HTTPException(409, str(e))
    except soulseek.SlskdHTTPError as e:
        raise HTTPException(502, str(e))
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
    """Start a Soulseek search; returns an id to poll for results.

    An MBID (`req.mbid`) is resolved to the queries for ONE track — its artist
    and title, its album, and its own id (`soulseek_auto.mbid_search_queries`)
    — and every one of them is POSTed as its OWN slskd search AT ONCE, the same
    "all at once, poll them in one loop" shape the auto-importer uses: the wall
    time is one window, not one per query. The answer's `id` is those ids
    comma-joined, and `GET /api/soulseek/search/{id}` merges their results, so a
    caller polls ONE key either way. Nothing is added to the wishes or the
    queue: this is a search, and the user downloads what they choose from it."""
    from server import soulseek, soulseek_auto
    if not (soulseek.is_running() or soulseek.web_up(load_config())):
        raise HTTPException(400, "slskd is not running — start it first")
    mbid = str(req.mbid or "").strip()
    if mbid:
        found = soulseek_auto.mbid_search_queries(mbid, load_config())
        if not found.get("ok"):
            raise HTTPException(404, str(found.get("error") or "unknown MusicBrainz id"))
        queries = list(found["queries"])
        ids, errors = soulseek.search_many(queries)
        if not ids:
            raise HTTPException(502, errors[0] if errors else "the search could not start")
        return {"id": ",".join(ids), "ids": ids, "queries": queries,
                "label": found.get("label") or "", "kind": found.get("kind") or "",
                "errors": errors}
    full = req.query.strip()
    if not full:
        raise HTTPException(400, "empty query")
    return {"id": soulseek.search(full), "queries": [full]}


@app.get("/api/soulseek/search/{search_id}")
def soulseek_search_results(search_id: str):
    from server import soulseek
    # adopted slskd (spawned by an earlier run) answers on the web port even
    # though no child handle exists — it must keep serving searches
    if not (soulseek.is_running() or soulseek.web_up(load_config())):
        raise HTTPException(400, "slskd is not running")
    ids = soulseek.search_ids(search_id)
    if not ids:
        raise HTTPException(400, "search id is required")
    if len(ids) > 1:
        # ONE track asked several ways: the results of every search, merged
        # (deduped by peer + file) and complete only when they all are.
        return soulseek.search_results_many(ids)
    return soulseek.search_results(ids[0])


class SoulseekSearchCancelRequest(BaseModel):
    id: str


@app.post("/api/soulseek/search/cancel")
def soulseek_search_cancel(req: SoulseekSearchCancelRequest):
    """Cancel a running search (slskd DELETE /searches/{id}).

    Without this the app is committed to the whole search window plus the
    grace tail; the UI's "stop" only stopped polling. An id that names SEVERAL
    searches (the manual MBID search's comma-joined key) cancels every one of
    them — the user pressed stop on the search, not on one of its queries."""
    from server import soulseek
    if not (soulseek.is_running() or soulseek.web_up(load_config())):
        raise HTTPException(503, "slskd is not running")
    ids = soulseek.search_ids(req.id)
    if not ids:
        raise HTTPException(400, "search id is required")
    cancelled, failed = 0, []
    for sid in ids:
        try:
            soulseek.cancel_search(sid)
            cancelled += 1
        except Exception as e:
            failed.append(f"{sid}: {e}")
    if not cancelled:
        raise HTTPException(502, f"search cancel failed: {'; '.join(failed)}")
    return {"ok": True, "cancelled": cancelled, "ids": ids}


def _clear_settled_in_background():
    """Take the queue's finished rows off the LIST when new work starts — on its
    own thread (see api_queue.clear_settled_queue for what it takes and why),
    because the queue payload asks slskd for its finished downloads and a
    request the user is waiting on must not wait for that.

    The CUT is stamped before the thread starts: only rows that were already
    settled when the request arrived are cleared, so a job that fails while the
    clear is still on its way to the registry is not swallowed by it."""
    import threading
    before = time.time()

    def work():
        try:
            from server import api_queue
            api_queue.clear_settled_queue(before=before)
        except Exception:
            pass
    threading.Thread(target=work, name="mlo-queue-autoclear", daemon=True).start()


def _queue_downloads(soulseek, username, files):
    """Queue files, turning slskd's own refusal into a readable 502.

    slskd answers the enqueue with 500 plus the reason in the body (`User
    <name> appears to be offline`) or with a 201 whose `Failed` list names the
    files the peer would not take. Both are the user's answer — "why did
    nothing queue" — so they must not arrive as a bare "Internal Server Error"
    from this app.
    """
    try:
        out = soulseek.enqueue_download(username, files)
    except soulseek.SlskdError as e:
        raise HTTPException(502, f"slskd did not queue the download: {e}")
    except Exception as e:
        # SlskdHTTPError is an httpx.HTTPStatusError; its str() now carries
        # slskd's own message (see soulseek._error_text).
        raise HTTPException(502, f"slskd did not queue the download: {e}")
    # A download the user just asked for IS a new run: the queue's FINISHED
    # rows come off the list the way the per-section Clear buttons take them
    # (`api_queue.clear_settled_queue`), so the page's own search→download does
    # not push the previous run's Completed/Failed history in front of the work
    # it just started. Live rows stay, and a download that finishes later still
    # shows. After the enqueue, never before: a refused press must not clear
    # anything. Only reached when files were really queued (the callers filter
    # to non-empty lists).
    _clear_settled_in_background()
    return out


def _active_downloads(soulseek, username):
    """The filenames this user ALREADY has queued or downloading in slskd.

    slskd's enqueue is per-user and does not dedupe: asking again for a file a
    live transfer already covers starts a SECOND batch for it, so the album
    comes down twice — and, since the page's own downloads import themselves,
    is imported and chained twice. The identity is slskd's own transfer row:
    the full remote filename, exactly as it appears in the queue, in any state
    that is not finished (`soulseek.finished_transfer`, the same rule the rest
    of the app drops transfers by). No second notion of identity lives here.

    slskd unreachable -> empty: the press is then queued exactly as before,
    because a duplicate is the smaller harm next to refusing a download the
    user asked for (and a route whose daemon is down 503s before this)."""
    active = set()
    try:
        for entry in soulseek.downloads_state() or []:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("username") or "").lower() != str(username or "").lower():
                continue
            for d in entry.get("directories") or []:
                for f in (d or {}).get("files") or []:
                    if not isinstance(f, dict):
                        continue
                    if not soulseek.finished_transfer(f.get("state")):
                        active.add(str(f.get("filename") or ""))
    except Exception:
        return set()
    return active


# --------------------------------------------------------------------------- #
# A download queued from the Soulseek page imports itself
# --------------------------------------------------------------------------- #
# The page's Download button used to be the end of the app's involvement: the
# bytes landed in the download folder and the album waited for somebody to
# press Import, a SECOND decision for an act the user had already taken. The
# page's three download routes now record what was asked for, and the pass
# below imports the album it produced — `_import_one_album` through
# `import_queue`, i.e. exactly what the Import button runs, so the chain, the
# claims and the notifications are the same ones and there is no second import
# pipeline to keep in step.
#
# Only those three routes record an intent: the auto-importer's own downloads
# go through `server.soulseek_auto` and import themselves under their own job,
# so recording there would import the same album twice.
#
# Nothing here decides when a download is done — the pass asks
# `soulseek.ready_albums`, the ONE readiness rule the Import button itself
# works from, so an album whose transfers are still running is simply not ready
# yet and an album a player still holds open is reported by the import exactly
# as a press would report it.
_PAGE_LOCK = threading.Lock()
_PAGE_DOWNLOADS: list = []
_PAGE_INTENTS_NAME = "page_downloads.json"
# Where the records above are kept between runs, and whether they have been
# read back yet. A backend restart (a config save, an update, a crash) between
# the press and the arrival must not silently drop the "a download queued HERE
# is mine" knowledge: the transfer keeps arriving, and an album nobody imports
# is exactly the second press this feature exists to remove.
_PAGE_STORE = {"path": "", "loaded": False}
# How long a recorded intent is worth honouring. An intent is only ever matched
# against the very files it queued (see `_page_download_albums`), so this is a
# bound on a list that must not grow for ever, not a staleness rule — a
# download can sit queued behind a peer overnight.
_PAGE_INTENT_TTL_S = 24 * 3600
_PAGE_IMPORT_INTERVAL_S = 5.0


def _page_store_path(cfg=None):
    """Where the recorded page downloads live between runs.

    Beside the rest of the app's state — `<music folder>/.mlo/data`, the folder
    the notification log is kept in (see mlo.paths.app_data_dir) — and not in a
    store of its own: these are a handful of small records whose only job is to
    outlive the process."""
    from mlo.paths import app_data_dir
    cfg = cfg if isinstance(cfg, dict) else load_config()
    mf = str((cfg or {}).get("music_folder") or "").strip()
    return os.path.join(app_data_dir(mf or None), _PAGE_INTENTS_NAME)


def _read_page_intents(path):
    """The intents a previous run wrote to `path` ([] when there are none).

    Every record is validated on the way in: the file is on the disk a user can
    reach, and a truncated or hand-edited one must not put a malformed record
    into a background pass (a missing size or filename is dropped, not
    defaulted)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    out = []
    for item in (data if isinstance(data, list) else []):
        if not isinstance(item, dict):
            continue
        who = str(item.get("username") or "").strip()
        rows = [{"filename": str((r or {}).get("filename") or ""),
                 "size": int((r or {}).get("size") or 0)}
                for r in (item.get("files") or []) if isinstance(r, dict)]
        rows = [r for r in rows if r["filename"]]
        if not who or not rows:
            continue
        out.append({"username": who, "files": rows,
                    "at": float(item.get("at") or 0)})
    return out


def _load_page_intents():
    """Read the persisted intents back, once per process, on first use.

    Lazily and not at import time: the store lives under the music folder of
    whatever config is live (see `_page_store_path`), which a test or a scoped
    install moves before the first press. Expired records are dropped here, so
    a store left behind by a run that died keeps nothing stale."""
    if _PAGE_STORE["loaded"]:
        return
    _PAGE_STORE["loaded"] = True
    _PAGE_STORE["path"] = _page_store_path()
    cutoff = time.time() - _PAGE_INTENT_TTL_S
    with _PAGE_LOCK:
        _PAGE_DOWNLOADS[:] = [i for i in _read_page_intents(_PAGE_STORE["path"])
                              if i["at"] >= cutoff]


def _save_page_intents():
    """Write the live intents out; a failure never loses the download.

    Best effort on purpose: the pass works from memory exactly as before, so a
    read-only or missing data folder costs the restart survival, not the
    import."""
    path = _PAGE_STORE["path"]
    if not path:
        return
    with _PAGE_LOCK:
        payload = [{"username": i["username"],
                    "files": [dict(r) for r in i["files"]],
                    "at": i["at"]} for i in _PAGE_DOWNLOADS]
    try:
        from mlo import atomic
        os.makedirs(os.path.dirname(path), exist_ok=True)
        atomic.write_bytes(path, json.dumps(payload, ensure_ascii=False)
                           .encode("utf-8"))
    except OSError:
        pass


def _remember_page_download(username, files):
    """Record what the page just queued, so its album imports itself.

    Only the INTENT is kept: which of this peer's files the user asked for.
    Which folder they became is decided later, from where those files really
    landed (`_local_download_candidates`/`_index_download_tree`, the mapping
    the auto-importer itself uses), so nothing here has to know slskd's
    staging layout. It is written out as well (see `_save_page_intents`), so a
    restart mid-download does not forget it."""
    _load_page_intents()
    rows = [{"filename": str((f or {}).get("filename") or ""),
             "size": int((f or {}).get("size") or 0)}
            for f in (files or [])]
    rows = [r for r in rows if r["filename"]]
    if not rows or not str(username or "").strip():
        return
    with _PAGE_LOCK:
        _PAGE_DOWNLOADS.append({"username": str(username), "files": rows,
                                "at": time.time()})
    _save_page_intents()


def _page_intents():
    """The live intent records, oldest first, with anything expired dropped.

    The records themselves are handed back, not copies: the pass marks the
    files an import has taken over ON them and prunes what is left empty."""
    _load_page_intents()
    cutoff = time.time() - _PAGE_INTENT_TTL_S
    with _PAGE_LOCK:
        _PAGE_DOWNLOADS[:] = [i for i in _PAGE_DOWNLOADS if i["at"] >= cutoff]
        return list(_PAGE_DOWNLOADS)


def _page_file_arrived(f, path):
    """True when the bytes on this disk are the file the intent asked for.

    The intent carries the size the press asked for (slskd's/browse's own), so
    a local file of a different size is a different file — or this one, still
    half written — and the album it lies in is a download that has not arrived
    yet."""
    want = int(f.get("size") or 0)
    if not want:
        return True        # the press had no size to compare against
    try:
        return os.path.getsize(path) == want
    except OSError:
        return False       # gone/renamed under us: not present yet


def _page_intent_files(intent, ddir):
    """`[(this intent's file, where it is on disk)]` — arrived files only.

    The lookup is the auto-importer's own, in the same two steps: one index of
    this peer's tree for the leaves this intent queued
    (`_index_download_tree`), then the candidate locations per file
    (`_local_download_candidates`). That is what makes a batch-dir download, an
    older leaf-shaped one and a slskd-sanitised share name all resolve the way
    the pipeline resolves them, instead of this module growing a second idea of
    where a transfer lands. A file that is there at another SIZE is skipped
    (see `_page_file_arrived`), and the next candidate for it is tried."""
    from server import soulseek_auto
    leaves = sorted({os.path.basename(str(f["filename"]).replace("\\", "/"))
                     for f in intent["files"]})
    index = soulseek_auto._index_download_tree(ddir, intent["username"], leaves)
    out = []
    for f in intent["files"]:
        for p in soulseek_auto._local_download_candidates(
                ddir, intent["username"], f["filename"], f["size"], index=index):
            if os.path.isfile(p) and _page_file_arrived(f, p):
                out.append((f, os.path.abspath(p)))
                break
    return out


def _page_download_albums(cfg, ddir):
    """`[(ready folder, [(intent, its files inside it)])]`.

    Only folders that hold a file the user queued from the page are returned:
    an album somebody else is downloading (a wish's job, a bulk run) is not
    this pass's business — those import themselves under their own job, and a
    second import of the same folder is the duplicate this scoping prevents.

    An intent is only ever considered WHOLE: it is the record of what the press
    asked slskd for, and a folder holding only part of that set is a download
    still coming (slskd reports each transfer on its own, so the rest can
    arrive minutes later). Importing it would chain and grade an album from a
    partial set, and the files that land afterwards would arrive into a folder
    nothing is watching any more — `_consume_page_intents` has already spent
    the intent. The whole set at the sizes that were asked for is what makes
    the album ready, and nothing less."""
    from server import soulseek, soulseek_auto
    intents = _page_intents()
    if not intents:
        return []
    try:
        ready = soulseek.ready_albums(cfg)
    except Exception:
        # slskd unreachable or no download dir: nothing can be ready, and the
        # intents stay for the tick that answers.
        return []
    if not ready:
        return []
    where = []
    for intent in intents:
        files = _page_intent_files(intent, ddir)
        if len(files) < len(intent["files"]):
            continue       # part of what was queued is still on its way
        where.append((intent, files))
    out = []
    for root in ready:
        held = [(intent, [fp for fp in files if soulseek_auto._under(fp[1], root)])
                for intent, files in where]
        held = [(intent, files) for intent, files in held if files]
        if held:
            out.append((root, held))
    return out


def _consume_page_intents(held):
    """Take the files an import just took over out of their own intent.

    An intent that queued a whole share loses only the album that just started
    importing; its other albums stay and import when their own folder is
    ready."""
    with _PAGE_LOCK:
        taken = {id(f) for _intent, files in held for f, _local in files}
        for intent, _files in held:
            intent["files"] = [f for f in intent["files"] if id(f) not in taken]
        _PAGE_DOWNLOADS[:] = [i for i in _PAGE_DOWNLOADS if i["files"]]
    _save_page_intents()


def _drop_page_intents(note=""):
    """Forget every recorded page download, saying why ONCE.

    Used when the install does not want imports to run by themselves (see
    `mlo.import_policy.page_download_auto_import`): the album keeps its row in
    the download folder — "ready to import" — and the press that imports it is
    the review those settings asked for. Forgetting is written out too: the
    next run must not pick these records back up."""
    _load_page_intents()
    with _PAGE_LOCK:
        count = len(_PAGE_DOWNLOADS)
        _PAGE_DOWNLOADS.clear()
    _save_page_intents()
    if count and note:
        print(f"[mlo] {count} download(s) queued from the Soulseek page: {note}")


def _page_download_pass():
    """One pass: import the albums the user's own page downloads finished.

    Returns True when an import was started. One album per pass on purpose —
    `import_queue` takes each album all the way through before the next one, so
    the rest are simply taken by the following ticks."""
    from mlo import import_policy
    from server import import_queue, soulseek

    if not _page_intents():
        return False
    cfg = load_config()
    if not import_policy.page_download_auto_import(cfg):
        _drop_page_intents(
            "left in the download folder for you to import — automatic import "
            "is off (import_autonomy = review, or manual_import_enabled off)")
        return False
    if import_queue.running():
        return False      # one import at a time: the next tick takes it
    ddir = soulseek.download_dir(cfg)
    if not ddir or not os.path.isdir(ddir):
        return False
    for root, held in _page_download_albums(cfg, ddir):
        res = import_queue.start(paths=[root])
        if not res.get("ok"):
            # An import run started between the two checks (or the importer is
            # not wired up yet): the album stays where it is and the next tick
            # tries again, with its intent intact.
            continue
        _consume_page_intents(held)
        print(f"[mlo] {os.path.basename(root)}: the download queued from the "
              f"Soulseek page finished — importing it and running its chain")
        return True
    return False


def _soulseek_page_downloads_watch():
    """Import the albums the user's own page downloads produced.

    Its own thread, not a step of the transfer watcher: that one pushes the
    page's live bars and deliberately does nothing while no client is watching
    (`_live_transfers_check`), while a download somebody asked for has to
    finish by itself whether or not a page is open. This loop costs nothing
    while nothing was queued from the page — the first thing a pass does is
    look at an (almost always empty) list."""
    while True:
        try:
            _page_download_pass()
        except Exception:
            traceback.print_exc()
        time.sleep(_PAGE_IMPORT_INTERVAL_S)


@app.post("/api/soulseek/download")
def soulseek_download(req: SoulseekDownloadRequest):
    """Queue files from a user for download into the download dir.

    A download queued HERE is imported by itself once it lands (see the
    page-download section above): the user has already said the album belongs
    in the library by asking for it, and a second press for the same act was
    the queue's own "and now import it".

    A file slskd is already fetching for this user is not asked for a second
    time (see `_active_downloads`): slskd would open a NEW batch for it and
    download the album over again, and the page's own auto-import would import
    and chain it a second time. `skipped` says how many of the asked-for files
    were already on their way."""
    from server import soulseek
    if not (soulseek.is_running() or soulseek.web_up()):
        raise HTTPException(400, "slskd is not running — start it first")
    files = [{"filename": str((f or {}).get("filename") or ""),
              "size": int((f or {}).get("size") or 0)}
             for f in (req.files or [])]
    files = [f for f in files if f["filename"]]
    if not req.username or not files:
        raise HTTPException(400, "username and files required")
    active = _active_downloads(soulseek, req.username)
    queue = [f for f in files if f["filename"] not in active]
    if queue:
        _queue_downloads(soulseek, req.username, queue)
        # Only what THIS press queued: a file a live transfer already covers was
        # not asked for again, and its album is already on its way with the
        # intent of the press that did queue it.
        _remember_page_download(req.username, queue)
    return {"ok": True, "queued": len(queue),
            "skipped": len(files) - len(queue)}


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
    are one call — the UI sends what the user selected. Returns
    {queued, skipped}, and a file slskd is already fetching for this user is
    skipped rather than queued a second time (see `_active_downloads` — the
    same rule /api/soulseek/download-user applies)."""
    from server import soulseek
    if not (soulseek.is_running() or soulseek.web_up()):
        raise HTTPException(503, "slskd is not running — start it first")
    files = [{"filename": str(f.get("filename") or ""), "size": int(f.get("size") or 0)}
             for f in (req.files or []) if str(f.get("filename") or "").strip()]
    if not str(req.username or "").strip() or not files:
        raise HTTPException(400, "username and a non-empty files list are required")
    active = _active_downloads(soulseek, req.username)
    queue = [f for f in files if f["filename"] not in active]
    if queue:
        _queue_downloads(soulseek, req.username, queue)
        _remember_page_download(req.username, queue)
    return {"queued": len(queue), "skipped": len(files) - len(queue)}


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

    active = _active_downloads(soulseek, username)

    queue = [f for f in wanted if f["filename"] not in active]
    if queue:
        _queue_downloads(soulseek, username, queue)
        # Only what was really queued: the files skipped because a transfer for
        # them is already running were not asked for by THIS press, and their
        # album is already on the way (with its own intent from the press that
        # did queue them).
        _remember_page_download(username, queue)
    return {"queued": len(queue), "scanned": scanned,
            "skipped": len(wanted) - len(queue)}


class SoulseekCancelRequest(BaseModel):
    """Transfers to drop in slskd, or QUEUE ROWS to cancel (see the route)."""
    username: str = ""
    transfer_ids: List[str] = []
    # Queue-row ids in the shape GET /api/queue publishes: "pipeline:<key>" for
    # a release still waiting and "job:<id>" for a running one. What the Queue
    # tab's selection sends; `username`/`transfer_ids` keep working untouched.
    ids: Optional[List[str]] = None


@app.post("/api/soulseek/downloads/cancel")
def soulseek_downloads_cancel(req: SoulseekCancelRequest):
    """Drop transfers from slskd's list (per-file or whole-queue cancel), or
    cancel exactly the queue rows named.

    With `ids` (the Queue tab's selection — `cancel_rows`), the call acts on
    those rows and NOTHING else: a WAITING release is dropped from the pipeline
    queue before it ever starts, a RUNNING one is cancelled, and the answer says
    how many of each went (`cancelled`, `ids`) and which of the given ids were
    already gone (`missed`). slskd is not consulted at all for these: a release
    that has not started has no transfers to drop.

    The transfer form is unchanged: `username` + `transfer_ids` drop those
    transfers in slskd, and 400s when either is missing. The underlying DELETE
    carries ?remove=true because a cancelled transfer otherwise stays queued and
    slskd keeps re-requesting the very files the review step just deleted."""
    from server import soulseek, soulseek_auto
    ids = [str(i) for i in (req.ids or []) if str(i).strip()]
    if ids:
        cancelled, missed = soulseek_auto.cancel_rows(ids)
        return {"ok": True, "cancelled": len(cancelled),
                "ids": cancelled, "missed": missed}
    if not (soulseek.is_running() or soulseek.web_up(load_config())):
        raise HTTPException(400, "slskd is not running")
    transfer_ids = [str(t) for t in req.transfer_ids if str(t).strip()]
    if not req.username or not transfer_ids:
        raise HTTPException(400, "username and transfer_ids required")
    try:
        # best effort per transfer: ids already gone must not fail the call
        soulseek.cancel_downloads(req.username, transfer_ids)
    except Exception as e:
        raise HTTPException(502, f"cancel failed: {e}")
    return {"ok": True, "cancelled": len(transfer_ids)}


class SoulseekClearRequest(BaseModel):
    scope: Optional[str] = None
    username: Optional[str] = None
    states: Optional[List[str]] = None


# Scopes the clear route accepts. "failed" lives here rather than in a route
# of its own because a second "Clear failed" button is a filter over the same
# transfer list, not a different operation. "queued" is the odd one out and
# says why in the route: it is not about slskd's transfers at all, it is the
# pipeline's own waiting queue (what a release does while the running ones hold
# the slots), so it needs no daemon and drops nothing that has started.
_CLEAR_SCOPES = ("finished", "failed", "incomplete", "all", "queued")


@app.post("/api/soulseek/downloads/clear")
def soulseek_downloads_clear(req: SoulseekClearRequest):
    """Clear transfers from slskd's history + their local partial bytes, by
    scope (see _CLEAR_SCOPES):

    - `finished` (the default) — the terminal transfers that SUCCEEDED. Their
      bytes stay: a completed download IS the album the app imports.
    - `failed` — the terminal ones that did not succeed (errored, cancelled,
      rejected, timed out…), plus the partials they staged.
    - `incomplete` — everything still in flight: dropped in slskd with
      `?remove=true` and its staged partial deleted (the queue is the only
      record of what is still coming, so this is what the UI must confirm).
    - `all` — finished + failed + incomplete.
    - `queued` — the pipeline's WAITING RELEASES (the Queue tab's "Clear all"):
      every release queued behind the ones already running is dropped before it
      ever starts, and nothing else is touched — a RUNNING release keeps its
      transfers (that is a cancel, one row at a time), and no settled row, no
      library album and no slskd transfer is affected. It needs no daemon, so
      it does not require slskd to be up like the transfer scopes do.

    `username` narrows any scope; `states` still narrows by substring within
    what the scope selected. A request WITHOUT `scope` keeps the old behaviour
    exactly — every finished transfer (failures included), no local deletes —
    and answers with the old {"ok", "cleared"} body.

    Best effort per transfer: one slskd refusal or one undeletable partial is
    reported in `failed`, never a 500."""
    from server import soulseek, soulseek_auto
    legacy = not str(req.scope or "").strip()
    scope = str(req.scope or "finished").strip().lower()
    if scope not in _CLEAR_SCOPES:
        raise HTTPException(400, f"unknown scope {req.scope!r} — expected one of "
                                 f"{', '.join(_CLEAR_SCOPES)}")
    if scope == "queued":
        # Checked BEFORE the daemon check: the waiting queue is this app's own
        # list, and clearing it must work with slskd down.
        got = soulseek_auto.clear_queued()
        return {"ok": True, "cleared": got["cleared"],
                "ids": [f"pipeline:{k}" for k in got["keys"]]}
    if not (soulseek.is_running() or soulseek.web_up(load_config())):
        raise HTTPException(400, "slskd is not running")
    wanted = [str(s).strip().lower() for s in (req.states or []) if str(s).strip()]
    try:
        tree = soulseek.downloads_state()
    except Exception as e:
        raise HTTPException(502, f"downloads lookup failed: {e}")
    ddir = soulseek.download_dir(load_config())

    targets: dict = {}
    for user in tree or []:
        who = str(user.get("username") or "")
        if req.username and who != req.username:
            continue
        for d in (user.get("directories") or []):
            for f in (d.get("files") or []):
                st = str(f.get("state") or "")
                done = soulseek.finished_transfer(st)
                ok = soulseek.successful_transfer(st)
                if legacy:
                    sel = done          # old callers: every finished transfer
                elif scope == "all":
                    sel = True
                elif scope == "finished":
                    sel = done and ok
                elif scope == "failed":
                    sel = done and not ok
                else:                   # incomplete
                    sel = not done
                if not sel or not f.get("id"):
                    continue
                # `states` narrows INSIDE the finished ones, so a client asking
                # for "InProgress" clears nothing instead of killing a live
                # download
                if wanted and not any(w in st.lower() for w in wanted):
                    continue
                targets.setdefault(who, []).append(
                    (str(f["id"]), str(f.get("filename") or ""),
                     int(f.get("size") or 0), done and ok))

    cleared = 0
    files_deleted = 0
    bytes_freed = 0
    failed: list = []
    for who, items in targets.items():
        refused: list = []
        try:
            # one user's failure must not abort the rest (cancel_downloads is a
            # best-effort loop over one DELETE per transfer)
            soulseek.cancel_downloads(who, [t[0] for t in items], failed=refused)
        except Exception as e:
            failed.append({"username": who, "filename": "",
                           "reason": f"could not be dropped in slskd: {e}"})
        cleared += len(items)
        if legacy:
            continue                # old callers never had local bytes touched
        for tid, name, size, complete in items:
            if tid in refused:
                failed.append({"username": who, "filename": name,
                               "reason": "slskd did not confirm the transfer was dropped"})
            if complete:
                continue            # a succeeded transfer's bytes are the album
            res = soulseek.clear_transfer_files(ddir, who, name, size)
            files_deleted += res["files_deleted"]
            bytes_freed += res["bytes_freed"]
            for p in res["problems"]:
                failed.append({"username": who, "filename": name, "reason": p})

    if legacy:
        return {"ok": True, "cleared": cleared}
    return {"cleared": cleared, "files_deleted": files_deleted,
            "bytes_freed": bytes_freed, "failed": failed}


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
        "-i", tool_path(p),
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
    freshly imported albums so grading and the library filters work.

    Both writes honour the same gate every other writer uses
    (should_write_audio_tag): normalize_media_source turns the whole
    MEDIA/SOURCE family off, and audio_tag_writes can disable it per
    filetype — an import used to stamp MEDIA/SOURCE regardless, so a user
    who turned the family off got it back on the next download."""
    from mlo.audio import AudioFile
    from mlo.config import should_write_audio_tag

    cfg = load_config()
    tagged = 0
    for d in album_dirs:
        media = _detect_rip_media(d)
        if not media:
            continue
        for root, _dirs, files in os.walk(d):
            for f in sorted(files):
                if not is_audio_file(f):
                    continue
                path = os.path.join(root, f)
                try:
                    af = AudioFile(path)
                    if af.audio is None:
                        continue
                    if (not str(af.get_tag("MEDIA") or "").strip()
                            and should_write_audio_tag(cfg, "MEDIA", filepath=path)):
                        af.set_tag("MEDIA", media)
                    if (media == "Digital Media"
                            and not str(af.get_tag("SOURCE") or "").strip()
                            and should_write_audio_tag(cfg, "SOURCE", filepath=path)):
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
            traceback.print_exc()
    return stamped


# --------------------------------------------------------------------------- #
# Importing what finished downloading
# --------------------------------------------------------------------------- #
# "Import" is one album's whole trip into the library, and it is deliberately
# ONE function: convert the lossless sources, write MEDIA, stamp the
# MusicBrainz identity, organize with the naming script, then run the
# configured import chain. Every entry point that imports — the classic
# one-click route, the per-album row, the sequential "import all completed"
# runner, and a wish whose download has landed — goes through here, so what an
# album ends up as cannot depend on which button was pressed.
#
# The album folder is claimed in server.job_locks for the whole of that trip
# (convert, tag, organize, chain), so nothing else deletes, moves or retags it
# halfway through: a foreign holder refuses the import with PathLocked (409)
# before any file is touched. With chain_async the background chain TAKES OVER
# that claim before this call returns (see job_locks.in_background below), so
# the album is never unclaimed between the HTTP response and the chain's end.
@job_locks.holds(lambda album, *a, **k: [album], kind="import",
                 label=lambda album, *a, **k: "Import " + (
                     os.path.basename(str(album).rstrip("\\/")) or str(album)))
def _import_one_album(album, cfg, chain_async=True, progress=None):
    """Take ONE downloaded album folder all the way into the library.

    Returns a result dict (`album_root`, counts, `errors`); it never raises,
    because the sequential runner reports per-album failures and must keep
    going with the next album. `chain_async` false runs the import chain
    inline — what "import all completed" needs, since it processes each album
    to completion before touching the next one.
    """
    out = {"path": album, "album_root": album, "converted": 0,
           "media_tagged": 0, "identity_stamped": 0, "organized": False,
           "chain_started": False, "chain": None, "errors": []}
    try:
        # Everything that is not already in the configured library codec
        # (library_codec, WAV/APE/ALAC/… and — under the "all" policy — lossy
        # sources too) is converted before anything is tagged or named, so the
        # naming script and the grader both see the final files.
        from mlo.flac import convert_album_lossless
        out["converted"] = int(
            (convert_album_lossless(album, cfg) or {}).get("modified_count") or 0)
    except Exception as e:
        traceback.print_exc()
        out["errors"].append(f"library codec conversion failed: {e}")
    try:
        # Classify the rip (CD / DVD-Video / Blu-ray / Digital Media) and
        # write the tags BEFORE organizing, so they travel with the files.
        out["media_tagged"] = _tag_media_for_albums([album])
    except Exception as e:
        traceback.print_exc()
        out["errors"].append(f"media tagging failed: {e}")
    try:
        # Canonicalize the MusicBrainz identity (RELEASECOUNTRY etc.) BEFORE
        # the naming script runs, or $releasecountry comes out empty.
        out["identity_stamped"] = _stamp_import_identity([album])
    except Exception as e:
        traceback.print_exc()
        out["errors"].append(f"MusicBrainz identity failed: {e}")
    try:
        # Best-effort: an organize failure must not lose the imported files
        # (they stay in their import folder and can be organized later).
        organized = organize(OrganizeRequest(paths=[album], dry_run=False))
        if isinstance(organized, dict):
            # organize RENAMES the album into the naming-script layout, so the
            # path it was given is stale the moment it returns — the chain has
            # to run on the root organize reports, or every script bails with
            # "album folder not found".
            roots = [r.get("album_root") or r.get("path")
                     for r in (organized.get("results") or [])
                     if isinstance(r, dict)]
            roots = [r for r in roots if r]
            if roots:
                out["album_root"] = roots[0]
        out["organized"] = True
    except Exception as e:
        traceback.print_exc()
        out["errors"].append(f"organize failed: {e}")
    from server import imports as imports_mod
    if not out["organized"]:
        # organize failed, so the album is still where it landed and the path
        # in `album_root` is the only one that exists. Running the chain here
        # would either be a no-op or act on a folder the organizer half-moved;
        # the error is already in `errors` and the caller reports it.
        return out
    if chain_async:
        # Fire-and-forget: the HTTP call returns at once and the chain runs on
        # a background thread (what the one-click route has always done) — but
        # the thread TAKES OVER this job's claim on the album before this call
        # returns (job_locks.in_background gives it a reference of its own).
        # The chain writes tags, covers and lyrics for minutes after the HTTP
        # response, and only claims anything itself at its run_chain step, so
        # without the hand-off the album was unlocked for all of that: a
        # delete, an organize or a bulk tag could land on it mid-chain.
        job_locks.in_background([out["album_root"]], imports_mod.finish_album,
                                out["album_root"], cfg)
        out["chain_started"] = True
    else:
        try:
            out["chain"] = imports_mod.finish_album(out["album_root"], cfg,
                                                    progress=progress)
        except Exception as e:
            traceback.print_exc()
            out["errors"].append(f"import chain failed: {e}")
    return out


def _folder_size(path):
    """`(bytes, file count)` below `path` — the readout the Downloads page
    shows beside an album waiting to be imported."""
    total = 0
    files = 0
    for base, _dirs, names in os.walk(path):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(base, n))
                files += 1
            except OSError:
                continue
    return total, files


def _importable_paths(paths, cfg):
    """Keep the paths that really are albums inside the music folder.

    A client-supplied path must never aim an import (which moves and renames
    files) at an arbitrary directory, so every candidate is resolved and
    checked against the download dir and the library root before use. The
    returned list also drops anything that is not a directory.
    """
    from server import soulseek
    allowed = [os.path.abspath(p) for p in
               (soulseek.download_dir(cfg), str(cfg.get("music_folder") or ""))
               if p]
    out = []
    for p in paths or []:
        if not p:
            continue
        ap = os.path.abspath(str(p))
        if not os.path.isdir(ap):
            continue
        if not any(_in_music_folder(ap, root) for root in allowed if os.path.isdir(root)):
            continue
        out.append(ap)
    return out


def _ready_download_albums():
    """The folders ``POST /api/soulseek/import`` is about to touch.

    ``server.soulseek.ready_albums`` is the list ``import_completed()`` itself
    works from, and each of those albums lands in ``<library>/<its folder
    name>`` (a " (n)" suffix only when that name is taken), so BOTH ends of the
    move are claimed before the body runs. The per-album import that follows
    claims the album again the moment it exists and hands that claim to its
    background chain. An unreadable download dir claims nothing: the route
    reports that in its own words.
    """
    from server import soulseek
    cfg = load_config()
    try:
        albums = list(soulseek.ready_albums(cfg) or [])
    except Exception:
        return []
    root = library_root(cfg.get("music_folder"))
    out = list(albums)
    if root and os.path.isdir(root):
        for src in albums:
            name = os.path.basename(str(src).rstrip("\\/"))
            if name:
                out.append(os.path.join(root, name))
    return out


@app.post("/api/soulseek/import")
@job_locks.holds(_ready_download_albums, kind="import", label="Import downloads")
def soulseek_import():
    """Move completed downloads from the download dir into the library, one
    album folder per shared folder, then immediately organize each imported
    album with the naming script and start its import chain — one click takes
    a download from slskd to a graded-library-ready album folder.

    Albums whose transfers are still running stay in the download dir and are
    reported in `skipped`, so the UI can say "still downloading".

    A folder a player (or slskd) still holds open is not fatal: the albums
    that did move are kept and organized, and the one that could not is
    reported in `failed` as {"path", "reason"} so the UI can name it and tell
    the user to stop playback. The per-album chain runs in the background
    (see `/api/soulseek/import-all` for the "run every album through to the
    end, one after another" behaviour)."""
    from server import soulseek
    try:
        moved = soulseek.import_completed()
    except ValueError as e:
        raise HTTPException(400, str(e))
    skipped = soulseek.last_import_skipped()
    failed = soulseek.last_import_failed()
    cfg = load_config()
    results = [_import_one_album(a, cfg, chain_async=True) for a in moved]
    moved = [r["album_root"] for r in results]
    media_tagged = sum(r["media_tagged"] for r in results)
    converted = sum(r["converted"] for r in results)
    identity_stamped = sum(r["identity_stamped"] for r in results)
    # `organized` must report what actually happened, per album: the UI
    # branches on it ("organize failed: …"), and a batch where one album
    # failed to organize is not a successful batch. The error text is handed
    # back the same way the pre-per-album route handed its own back.
    organize_errors = [e for r in results for e in r["errors"] if e.startswith("organize failed")]
    tagcache.invalidate_all()
    mbresolve.invalidate()
    return {"ok": True, "moved": moved, "skipped": skipped,
            "failed": failed,
            "organized": all(r["organized"] for r in results) if results else False,
            "organize_error": "; ".join(organize_errors) or None,
            "media_tagged": media_tagged, "converted": converted,
            "identity_stamped": identity_stamped,
            "tagging_started": bool(moved),
            "albums": results}


@app.get("/api/soulseek/ready")
def soulseek_ready():
    """Every album sitting in the download dir that finished downloading and
    is waiting to be imported, with its size.

    The Downloads page polls this to offer the per-album Import button and
    the "Import all completed" run; it is the same question
    `import_completed()` asks (see `soulseek.ready_albums`), so an album
    listed here is exactly one the import would move."""
    from server import soulseek
    cfg = load_config()
    try:
        paths = soulseek.ready_albums(cfg)
    except Exception as e:
        raise HTTPException(502, f"could not read the download dir: {e}")
    ddir = soulseek.download_dir(cfg)
    albums = []
    for p in paths:
        size, files = _folder_size(p)
        try:
            rel = os.path.relpath(p, ddir) if ddir else p
        except ValueError:
            rel = p  # different drive (Windows): a relative path does not exist
        albums.append({"path": p, "name": os.path.basename(p.rstrip("\\/")) or p,
                       "rel": rel, "files": files, "bytes": size})
    return {"ok": True, "albums": albums, "download_dir": ddir}


class ImportPathRequest(BaseModel):
    path: str


@app.post("/api/soulseek/import-one")
def soulseek_import_one(req: ImportPathRequest):
    """Import ONE album folder, all the way through, in the background.

    The path must sit inside the download dir or the library — see
    `_importable_paths`; anything else is refused rather than imported."""
    from server import import_queue
    cfg = load_config()
    paths = _importable_paths([req.path], cfg)
    if not paths:
        raise HTTPException(400, "that path is not an album inside your music "
                                 "folder or download folder")
    res = import_queue.start(paths=paths)
    if not res.get("ok"):
        raise HTTPException(409, res.get("error") or "could not start the import")
    return res


@app.post("/api/soulseek/import-all")
def soulseek_import_all():
    """Import every album that finished downloading — one at a time.

    Deliberately NOT one batch: each album is taken through the whole
    pipeline (convert → tag → organize → chain) before the next one starts,
    so a failure half way through leaves the albums after it untouched and
    the UI can name exactly which one broke. Progress lives at
    `/api/soulseek/import-all/status`."""
    from server import import_queue
    res = import_queue.start()
    if not res.get("ok"):
        raise HTTPException(409, res.get("error") or "could not start the import run")
    return res


@app.get("/api/soulseek/import-all/status")
def soulseek_import_all_status():
    from server import import_queue
    return import_queue.status()


@app.post("/api/soulseek/import-all/cancel")
def soulseek_import_all_cancel():
    """Stop after the album currently being imported (never mid-album: a
    half-imported album is worse than a slow one)."""
    from server import import_queue
    return {"ok": import_queue.cancel(), "status": import_queue.status()}


@app.post("/api/wishes/{wid}/import")
def wishes_import(wid: int):
    """Import the download a wish is waiting on.

    A wish can be holding an album that has already landed (the pipeline
    imported it, `album_path` is set) or one that finished downloading while
    auto-import was off. Both cases end in the same place — the album goes
    through `_import_one_album`, exactly like a manual import — and the wish
    is marked imported with the resulting path when it lands, which is also
    what raises the "wish found" notification the other clients show.

    Runs in the background (the chain takes minutes); the caller polls
    `/api/soulseek/import-all/status` or the wish list."""
    from server import import_queue, wishes
    wish = wishes.get_wish(wid)
    if wish is None:
        raise HTTPException(404, "wish not found")
    cfg = load_config()
    candidates = []
    album_path = str(wish.get("album_path") or "").strip()
    if album_path and os.path.isdir(album_path):
        from server import pending_albums
        # A framework album is not an album to import: the folder "Add to
        # library" created holds a marker, a placeholder cover and NO audio, so
        # an import aimed at it would run the chain over nothing and then mark
        # this wish imported — the empty folder would stand in the library as
        # the album that wish was waiting for. Nothing has arrived yet, which is
        # what the 409 below says.
        if not pending_albums.is_placeholder(album_path):
            candidates.append(album_path)
    # Anything in the download dir that names this wish's release: the artist
    # and the title, in either order, is what a peer's folder is called.
    from server import soulseek
    want = {t for t in
            (str(wish.get("artist") or "").lower().split()
             + str(wish.get("title") or "").lower().split()) if len(t) > 2}
    if want:
        for p in soulseek.ready_albums(cfg):
            name = os.path.basename(p.rstrip("\\/")).lower()
            hits = sum(1 for t in want if t in name)
            if hits >= min(2, len(want)):
                candidates.append(p)
    paths = _importable_paths(candidates, cfg)
    if not paths:
        raise HTTPException(
            409,
            "nothing downloaded for this wish yet — search for it (Search now) "
            "or import the album from Downloads once it arrives")

    def _landed(results):
        """Mark the wish imported once its album really is in the library."""
        ok = [r for r in (results or []) if r.get("ok")]
        if not ok:
            return
        for r in ok:
            try:
                wishes.mark_imported(wid, r.get("album_root") or "")
            except Exception:
                traceback.print_exc()
            try:
                events_mod.emit("wish_found", f"Wish imported: {wish.get('artist') or '?'} — {wish.get('title') or '?'}",
                                "The album is in your library now.",
                                {"wish_id": wid, "album_path": r.get("album_root") or ""})
            except Exception:
                pass
            break

    res = import_queue.start(paths=paths, on_done=_landed)
    if not res.get("ok"):
        raise HTTPException(409, res.get("error") or "could not start the import")
    return res


# Wired here, after both sides exist: the queue asks these for what to import
# and for how to import one album, instead of importing server.main (which
# would be a cycle). Inline chains only — the runner's whole contract is that
# one album is finished before the next starts.
from server import import_queue as _import_queue  # noqa: E402
_import_queue.set_importer(lambda path: _import_one_album(path, load_config(), chain_async=False))
_import_queue.set_ready_provider(lambda: __import__(
    "server.soulseek", fromlist=["soulseek"]).ready_albums())
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


# What a browse of this app's OWN account answers with, and why it is read
# locally: the peer network cannot answer it from inside this network. The
# Soulseek server hands every client the address it published for the account —
# this network's own public address — and a router without NAT loopback refuses
# exactly that dial (the port check's `self-connect` row measures the wall).
# slskd's own index is the tree a peer is served, so the answer is the same one
# a browse over the internet would bring back (R297).
LOCAL_SHARE_NOTE = (
    "Read from this app's own share — the same folders and files a peer is "
    "served. Browsing your own account over the peer network needs your router "
    "to reflect its own public address (NAT loopback); this answer does not."
)


def _browse_rows(dirs):
    """The browse answer's own shape: {directory, files:[{filename, size}]}."""
    return [{"directory": str(d.get("directory") or ""),
             "files": [{"filename": str(f.get("filename") or ""),
                        "size": int(f.get("size") or 0)}
                       for f in (d.get("files") or [])]}
            for d in (dirs or [])]


@app.get("/api/soulseek/browse/{username}")
def soulseek_browse(username: str, refresh: int = Query(0)):
    """Every shared folder of a remote user — the manual-pick view: what else
    does this uploader have before queueing individual files?

    Our OWN username is answered from this app's own share (LOCAL_SHARE_NOTE,
    R297): that browse cannot be served over the peer network from inside this
    network, so it is read from the index slskd serves and carries
    `local: true`.

    Otherwise slskd does the browsing over the peer network and we only
    normalize its payload to {username, directories:[…]}: an offline user
    (slskd 404) answers 5xx — an upstream failure carrying slskd's own
    explanation, hence 502. `refresh=1` bypasses the 120 s in-process cache
    (the modal's Refresh button used to re-issue the same cached answer)."""
    from server import soulseek
    if not (soulseek.is_running() or soulseek.web_up(load_config())):
        raise HTTPException(404, "slskd is not running")
    cfg = load_config()
    if soulseek.is_own_username(username, cfg):
        try:
            rows = soulseek.local_browse(cfg, use_cache=not refresh)
        except Exception as e:
            raise HTTPException(502, f"browse failed: {e}")
        return {"username": username, "local": True,
                "note": LOCAL_SHARE_NOTE, "directories": _browse_rows(rows)}
    try:
        dirs = soulseek.browse(username, use_cache=not refresh) or []
    except Exception as e:
        raise HTTPException(502, f"browse failed: {e}")
    return {"username": username, "directories": _browse_rows(dirs)}


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
    """Drop a wish. A FRAMEWORK album is that wish's own folder (it was created
    for it, before the download existed), so it goes with it — a placeholder
    the user no longer wants is not a library album. A folder whose audio has
    already arrived is a real album and stays."""
    from server import pending_albums, wishes
    pending_albums.remove_for_wish(wid)
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
    if req.accept:
        # Answering "yes" RESUMES the job on the lossy copy it found: that is
        # new work starting, so the queue's finished rows come off the list
        # (the same clear the per-section buttons run). A "no" ends the job and
        # clears nothing.
        _clear_settled_in_background()
    return {"ok": True, "accepted": bool(req.accept)}


def _resolve_release(mbid):
    """(release, release_id) for a release OR release-group MBID/URL.

    One resolution path for every caller in this file: the edition is picked
    by the release-choice policy in `mlo.release_choice` (Official above
    promotional/bootleg, the configured medium order with physical before
    digital — the video carriers DVD, Blu-ray, VHS, Video CD and LaserDisc
    named ahead of Digital Media, so a music video on a disc beats the same
    video published as a download — the most complete tracklist, then the
    earliest date; the preferred country and the original-over-reissue rule
    break ties), and its
    reasons are reported next to the pick by `/api/mb/release-choice`. Raises
    502 when MusicBrainz cannot resolve the id at all."""
    try:
        return intg.resolve_release(mbid)
    except Exception as e:
        raise HTTPException(502, f"MusicBrainz release lookup failed: {e}")


@app.post("/api/soulseek/auto")
def soulseek_auto_start(req: SoulseekAutoRequest):
    """Find → verify → download → audit → import a specific MusicBrainz
    release from Soulseek (see server/soulseek_auto.py for the pipeline).

    Accepts a release OR release-group MBID (a group resolves to its best
    edition by the release-choice policy).

    With `soulseek_search_concurrency` releases already running this does NOT
    fail with a 409: the release takes its place in the pipeline's waiting
    queue and the answer says so ({"ok": true, "waiting": true, "position": n,
    "queue_key": …}, no `job`) — it starts by itself when one of the running
    releases finishes, and the Queue tab lists it in its Waiting group until
    then (or until it is cancelled there)."""
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
                     "are skipped while auto_import_avoid_promo is on, and "
                     "editions without a release country while "
                     "auto_import_require_country is on)")
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
    _queue_downloads(soulseek, req.username,
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
    # An UNGRADED log (download or grade timed out) is not a pass: the verdict
    # is about every log in the folder reaching the threshold, and the auto
    # importer already treats an unscorable log as a failure.
    ok = bool(out) and all(
        e["score"] is not None and (e["score"] or 0) >= threshold for e in out
    )
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
    """Library changed (organize/import/remove/tag/run) — refresh the slskd
    share index in the background so the network always sees the current paths.

    This function used to DEFINE `_worker` and never start it, so all of its
    call sites were silent no-ops and the shared file list went stale until
    slskd was restarted by hand. The work now lives in server.soulseek (which
    owns the daemon) and is debounced there."""
    from server import soulseek
    soulseek.refresh_shares_soon()


@app.get("/api/track/download")
def track_download(path: str = Query(...)):
    """Serve the original, untouched audio file as a browser download.

    Same refusal as /api/stream: "untouched" is a promise a job mid-rewrite
    cannot keep, and handing over a half-written file is worse than saying why.
    """
    p = os.path.normpath(mbresolve.resolve_track(path) or path)
    _refuse_locked(p)
    if not os.path.isfile(p):
        raise HTTPException(404, "file not found")
    if not _in_music_folder(p, _music_folder()):
        raise HTTPException(400, "path is outside the music folder")
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
    p = os.path.normpath(mbresolve.resolve_track(path) or path)
    if not os.path.isfile(p):
        raise HTTPException(404, "file not found")
    if not _in_music_folder(p, _music_folder()):
        raise HTTPException(400, "path is outside the music folder")
    bitrate = max(64, min(500, int(bitrate)))
    level = max(0, min(8, int(level)))
    args = [a.format(bitrate=bitrate, level=level) for a in args_tpl]
    # The encode-and-serve step is server.api_media's (it is what the offline
    # download's own rendition uses); this route only owns its codec table and
    # the save-dialog naming.
    from server import api_media
    return api_media.encoded_response(p, args, ext, "application/octet-stream",
                                      attachment=True)


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


def _tag_paths_guard(paths, staged=False):
    """Containment guard (same as /api/tags/bulk and /api/mb/assign): genre
    routes write tags, so paths outside the music folder are refused — unless
    the import wizard asks for its staged album, which is not in it yet."""
    folder = _music_folder()
    for p in paths:
        if not _in_music_folder(p, folder) and not _allow_staged(p, staged):
            raise HTTPException(400, f"path outside music folder: {p}")


def _genre_names(value, cap):
    """The canonical genre list one caller's GENRE value holds.

    A caller may send the value as a list (`["shoegaze", "rock"]`) or as one
    string another surface joined ("Rock; Shoegaze"), and both have to land as
    repeated GENRE fields holding MusicBrainz's own names. `str(v)` stored the
    first as ONE genre literally named "['shoegaze', 'rock']" and the second as
    one named "Rock; Shoegaze" — one field, and a genre no reader recognises.
    `normalize_genres` splits it, resolves each name, derives the family and
    puts it first, capped at *cap*.
    """
    from mlo.genres import normalize_genres
    return normalize_genres(value, cap)


def _write_album_genres(files, names, per_track=None, limit=None):
    """Write GENRE per track: the track's own genres first (MusicBrainz
    recording genres, when the release has them), then the album-level merged
    list, canonicalized and capped through `mlo.genres.normalize_genres`
    (MusicBrainz's own spelling, the family first, at most *limit* names) — so
    the file holds the same list the import and the trimming scripts keep.
    Returns the files written."""
    from mlo.audio import AudioFile
    from mlo.genres import DEFAULT_GENRE_COUNT, normalize_genres
    from server import soulseek_auto

    per_track = per_track or {}
    count = limit or DEFAULT_GENRE_COUNT
    updated = 0
    for p in files:
        try:
            af = AudioFile(p)
            if af.audio is None:
                continue
            merged = normalize_genres(
                list(per_track.get(soulseek_auto._parse_trackno(p)) or [])
                + list(names or []), count)
            # A list, so set_tag writes repeated GENRE fields (one "A; B"
            # string is what makes players show a single genre by that name).
            if merged and af.set_tag("GENRE", merged):
                updated += 1
        except Exception:
            continue
    if updated:
        tagcache.invalidate_all()
        mbresolve.invalidate()
    return updated


class GenreChainImportRequest(BaseModel):
    paths: List[str]  # audio files (or one album dir)
    limit: Optional[int] = None  # defaults to mb_genre_count from settings
    # Which sources to ask. Omitted = every configured source in order (the
    # chain, which every surface now uses); a caller may name a subset, and the
    # wizard's single Import genres button passes none so it runs the
    # configured chain.
    sources: Optional[List[str]] = None
    staged: bool = False  # the import wizard's not-yet-imported album


def _run_genre_chain(paths, limit=None, progress=None, sources=None, staged=False):
    """Import GENRE from EVERY configured genre source, per track.

    The chain runs in the order of `genre_sources` (RateYourMusic → Soulseek
    signals → Discogs/Last.fm/TheAudioDB → Deezer/iTunes → MusicBrainz), merges
    what each source answers, dedupes case-insensitively and caps them at
    `mb_genre_count` — the requested `limit` (the wizard's per-run "Max
    genres") may only LOWER it, through the one helper that reads the setting
    (`mlo.autotag.genre_count`), so a per-run control can never write more
    genres than the user configured.

    It STOPS asking once every track is full: at `mb_genre_count = 2` one good
    specific genre plus its derived family is the whole answer, and the sources
    below the one that supplied it are not asked at all (`asked`,
    `stopped_after`). A source that cannot answer is skipped before any request
    — unconfigured (Discogs/Last.fm/Spotify), known-blocked (RateYourMusic), or
    a documented no-op (Soulseek) — and named in `skipped`. MusicBrainz
    recording genres refine each track when the album names a release.

    The report is what the wizard and the Settings panel render: `per_source`
    with `per_source_counts` (names and tracks per source), `sources`/`levels`
    (per track path, in the order that contributed and the tier that answered),
    `level_counts` (how many tracks an ALBUM/artist-level answer covered), the
    trims, and `notes`. Nothing is ever filled in from a guess.
    `progress(i, total, source)` is the chain's own per-source step, which is
    what the background job (and the websocket relay) reports while it runs.
    """
    from mlo.audio import AudioFile

    cfg = load_config()
    # Validate BEFORE the helper, which swallows a bad value rather than
    # raising (so a direct Python caller cannot be broken by one): the API
    # still answers 400 for a limit that is not a number instead of quietly
    # running with the setting.
    if limit is not None:
        try:
            int(limit)
        except (TypeError, ValueError):
            raise HTTPException(400, "limit must be a number")
    cap = _autotag.genre_count(cfg, limit)
    _tag_paths_guard(paths, staged)
    files = _genre_files(paths)

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

    # The per-source hook ships with the chain itself; a checkout that has not
    # landed it yet still runs, just without the `source i/N` steps.
    hook = {"progress": progress} if progress is not None and \
        "progress" in inspect.signature(intg.genre_chain).parameters else {}
    chain = intg.genre_chain(artist=artist, album=album, release=release,
                             limit=cap, cfg=cfg, files=files, sources=sources,
                             **hook)
    names = chain.get("genres") or []
    # The chain already merged every track's own genres ahead of the
    # release-wide ones and capped them per track — `_write_album_genres`, the
    # ONE writer, is what applies the cap to the file.
    per_track = chain.get("per_track") or {}

    updated = _write_album_genres(files, names, per_track, limit=cap)
    # The cap is a per-track contract the import has to LEAVE BEHIND, not just
    # apply to what it writes: a track that already carried more genres than
    # the setting allows comes down to `mb_genre_count` here, through the one
    # trimmer script 8 and script 10 also use. Counts are extra values / tracks
    # touched, so the caller can say what the cap actually did.
    from mlo.autotag import trim_genres
    trimmed = extra = 0
    trimmed_files = []
    for p in files:
        try:
            af = AudioFile(p)
            if af.audio is None:
                continue
            removed = trim_genres(af, cap)
        except Exception:
            continue
        if removed:
            trimmed += 1
            extra += removed
            trimmed_files.append(os.path.basename(p))
    if trimmed:
        tagcache.invalidate_all()
        mbresolve.invalidate()
    levels = chain.get("levels") or {}
    level_counts = chain.get("level_counts") or {}
    notes = dict(chain.get("notes") or {})
    if trimmed:
        # Reuses the chain's own notes channel (the wizard renders it under the
        # genre step), so the trim is visible where genres are reported.
        notes["genres per track"] = (f"{extra} extra genre(s) trimmed — genres per "
                                     f"track is {cap} (Settings → Import)")
    # An album- or artist-level answer is not a track's own fact, and with two
    # slots it is often the whole answer — so the report says how many tracks
    # it covered rather than leaving the caller to read every `levels` entry.
    wide = int(level_counts.get("album") or 0) + int(level_counts.get("artist") or 0)
    if wide:
        notes["genre level"] = (
            f"{wide} track(s) answered at ALBUM or ARTIST level (their own "
            f"recording states no genre); with genres per track = {cap}, that "
            "is often the whole answer")
    if chain.get("stopped_after"):
        notes["genre sources"] = (
            f"stopped after {chain['stopped_after']}: every track's genres were "
            f"already complete (asked {len(chain.get('asked') or [])} of "
            f"{len(chain.get('order') or [])} configured source(s))")
    return {"updated": updated, "genres": names,
            "per_source": chain.get("per_source") or {},
            "per_source_counts": chain.get("per_source_counts") or {},
            "notes": notes,
            "per_track": bool(per_track),
            "genre_count": cap,
            "trimmed": trimmed,
            "trimmed_files": trimmed_files,
            # The chain files a track's dropped genres under a (disc, position)
            # TUPLE. A tuple key cannot survive FastAPI's jsonable_encoder: it
            # re-encodes the key to a list and then explodes on the unhashable
            # key (`TypeError: unhashable type: 'list'`) — OUTSIDE the handler,
            # so the route answered a plain-text 500 with no detail every time
            # the cap dropped a genre, which is every real MusicBrainz/RYM
            # import. The report therefore uses the app's own "disc:position"
            # key, the same string every other per-track map in the chain
            # already uses.
            "trimmed_genres": {intg._genre_track_key(disc, position): dropped
                               for (disc, position), dropped in
                               (chain.get("per_track_trimmed") or {}).items()},
            # Where each file's genres came from, in the order that
            # contributed, plus the tier that answered (track/album/artist),
            # and what the chain did with the sources it did not need.
            "sources": chain.get("sources") or {},
            "levels": levels,
            "level_counts": level_counts,
            "asked": chain.get("asked") or [],
            "stopped_after": chain.get("stopped_after"),
            "skipped": chain.get("skipped") or {}}


@app.post("/api/genres/import")
def genres_import(req: GenreChainImportRequest):
    """Import GENRE from the given sources (all configured ones by default).

    The wizard's two per-source buttons pass one source each, so MusicBrainz
    and RateYourMusic are asked separately and each reply reports what that
    source wrote."""
    return _run_genre_chain(req.paths, req.limit, sources=req.sources,
                            staged=req.staged)


@app.get("/api/genres/facets")
def genres_facets():
    """GENRE tags across the library, counted and bucketed.

    `genres` is every distinct genre with the number of tracks carrying it
    (descending), which is the "all genres" list; `categories` groups those
    names into the fixed buckets the UI filters by (Metal, Rock, Electronic,
    Hip-Hop, Jazz, Classical, Folk, Soul & Funk, Pop, Other) — a genre lands in
    exactly one bucket, so the counts stay honest. A name is reported in
    MusicBrainz's own spelling (`mlo.genres.canonical`) and a stored value
    another tagger joined ("Rock / Shoegaze") counts as the two genres it
    names, exactly as the writers and the grader read it."""
    from mlo.genres import canonical, split_stored
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
                raw = (tr.get("tags") or {}).get("GENRE")
                for piece in split_stored(raw):
                    text = canonical(piece) or piece
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
    staged: bool = False                        # the wizard's staged album
    force: bool = False                         # re-rate files that already carry 0/1/2

@app.post("/api/mb/advisory/fetch")
def mb_advisory_fetch(req: AdvisoryFetchRequest):
    """Resolve ITUNESADVISORY (0/1) for an album or a MusicBrainz release.

    Tracks are identified by every ISRC their own tag states — or by the
    MusicBrainz recording ID an import already stamped, whose ISRCs
    MusicBrainz supplies — and rated by EVERY applicable source in one pass
    (Deezer and Spotify by ISRC, Apple's explicit-edition album route, Apple's
    exact-title song search), merged by `integrations.merge_advisory`: 1 when
    any source states explicit, else 0 — an unstated advisory is written as 0,
    and `answers` is what shows whether any source actually spoke. A `cleaned`
    Apple entry states nothing and is never written. A provider that STATED a
    value ends the question: it is written as it stands, with that provider's
    provenance, and the AI is not asked to second-guess it — a stated 0 is
    final. When nobody stated anything, `mlo.advisory.decide_advisory` runs the
    ladder (an instrumental is 0, then the configured AI, then
    `advisory_fallback`) and the reported source names the stage that answered.

    An existing valid 0/1/2 is echoed, not re-asked, and the echo carries its
    provenance (`sources[path] = "existing-tag"`, `status[path] = "existing"`)
    so a readout can say the value came from the file and nobody was asked.
    `force: true` is the re-rate: those files are asked and what the sources
    state IS written (the write gate still applies). Only evidence lowers a
    rating — a value invented by `advisory_fallback` never overwrites one — and
    a value equal to the merged one is not rewritten. ALBUMITUNESADVISORY is
    derived from the per-track values for every album folder the call touched
    (script 8's rule, `albums`).

    Returns {updated, values, sources, answers, albums, album_updated, gated,
    status}: `values` maps the file path (paths mode) or "disc:position"
    (release mode) to the merged rating, `sources` maps the same keys to the
    provider that stated it ("existing-tag" for an echoed file value),
    `answers` maps them to what every source said ({source: 0|1}), `status`
    says what happened to each path's value this run — `written`, `unchanged`,
    `existing` or `gated` — so `updated == 0` is never read as a re-rate that
    found nothing, `albums` maps each album folder to the
    ALBUMITUNESADVISORY derived from those values, `album_updated` counts the
    album-tag writes and `gated` the files the ADVISORY write gate refused.
    `skipped` carries the reason (no `updated`, no `values`) when that gate
    refused EVERY file, so a caller never reports a silent no-op as "nothing
    was found"."""
    from server import imports as imports_mod

    if not req.paths and not req.release_mbid:
        raise HTTPException(400, "paths or release_mbid required")
    values = {}
    sources = {}
    answers = {}
    status = {}
    albums = {}
    updated = 0
    album_updated = 0
    gated = 0
    album_gated = 0
    skipped = ""
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
        _tag_paths_guard(req.paths, req.staged)
        result = imports_mod.fetch_advisories(req.paths, load_config(),
                                              force=req.force)
        updated = int(result.get("updated") or 0)
        album_updated = int(result.get("album_updated") or 0)
        gated = int(result.get("gated") or 0)
        album_gated = int(result.get("album_gated") or 0)
        skipped = str(result.get("skipped") or "")
        values.update(result.get("values") or {})
        sources.update(result.get("sources") or {})
        answers.update(result.get("answers") or {})
        status.update(result.get("status") or {})
        albums.update(result.get("albums") or {})
    return {"updated": updated, "values": values, "sources": sources,
            "answers": answers, "status": status, "albums": albums,
            "album_updated": album_updated, "gated": gated,
            "album_gated": album_gated, "skipped": skipped}


class InstrumentalFetchRequest(BaseModel):
    paths: Optional[List[str]] = None           # audio files or album folders
    staged: bool = False                        # the wizard's staged album


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
    _tag_paths_guard(req.paths, req.staged)
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
def metadata_candidates_route(artist: str = Query(""), album_path: str = Query(""),
                              staged: bool = Query(False)):
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
        _guard_folder(album_dir, staged, "album", _music_folder(cfg))
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
            hit = discovery.artist_image(_metadata_artist_name(folder, artist), mbid=artistdata.folder_mbid(folder) or artistdata.folder_mbid(artist), cfg=cfg)
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
                found = discovery.artist_description(name, mbid=artistdata.folder_mbid(folder) or artistdata.folder_mbid(artist), cfg=cfg)
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
@job_locks.holds(lambda req: [] if req.dry_run else req.paths,
                 kind="organize", label="Organize")
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

        # A framework album for this release may already sit where the script
        # names — "Add to library" created that folder before the download
        # existed. Its marker carries the release identity, so an album whose
        # tags say it is the same release lands IN that folder: without this,
        # a name that differs by one segment (the MEDIA spelling the importer
        # detected vs MusicBrainz's) would drop the album beside the
        # placeholder and leave the placeholder pending forever.
        #
        # `script_root` is the name the script gives the album, and the folder
        # is RENAMED onto it below (see the end of this loop): the placeholder's
        # own name comes from the MusicBrainz payload, so keeping it would mean
        # an add-time name outliving the tags — the album sitting in a folder
        # no tag can produce, which grading then reports file by file
        # ("PATH: expected '…'"). The identity, not the name, is what makes the
        # folder the album's: the marker is inside it and travels with it.
        script_root = new_root
        try:
            from server import pending_albums
            adopted = pending_albums.adopt_root(new_root, meta_tags)
        except Exception:
            traceback.print_exc()
            adopted = new_root
        if os.path.normcase(os.path.normpath(adopted)) != \
                os.path.normcase(os.path.normpath(new_root)):
            base = new_root
            new_root = adopted
            moves = [(s, os.path.join(adopted, os.path.relpath(d, base)))
                     for s, d in moves]
            dst_dirs = [os.path.join(adopted, os.path.relpath(d, base))
                        for d in dst_dirs]

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
                            # The app's OWN writers put the album description in
                            # two places — the staging folder of an import and
                            # the album folder `prefetch_content` prepared — and
                            # this sweep is where the two meet. When the file
                            # already sitting at the destination is the SAME
                            # file (same bytes), carrying it again only invents
                            # a "description (2).txt": the library then holds
                            # two copies and the canonical one is what every
                            # reader opens. Identical → drop the source copy,
                            # nothing is lost (the loser is the staging copy of
                            # a file the album already has).
                            if os.path.exists(target) and filecmp.cmp(fpath, target, shallow=False):
                                try:
                                    os.remove(fpath)
                                except OSError:
                                    pass
                                continue
                            # Same rule as the track moves above: a name the
                            # destination already holds is never overwritten.
                            # move_path ends in os.replace, so without this a
                            # sidecar silently ate the file already sitting
                            # there (another track's art, a cover search's
                            # pick) instead of taking a " (2)" name.
                            n = 2
                            base, ext_l = os.path.splitext(os.path.basename(target))
                            while os.path.exists(target) and \
                                    os.path.normcase(os.path.abspath(target)) != \
                                    os.path.normcase(os.path.abspath(fpath)):
                                target = os.path.join(ddir, f"{base} ({n}){ext_l}")
                                n += 1
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

        # The framework album's folder takes the script's name — the album was
        # moved into it above, so this is one rename of a folder that already
        # holds the whole album (marker, manifest, art, audio), not a second
        # move: nothing is left behind and no placeholder is orphaned. Only a
        # folder still carrying its marker is ever renamed
        # (`pending_albums.rename_placeholder`), and only onto a name nothing
        # else occupies: a real album standing there is never touched, and the
        # album then keeps the folder it landed in rather than being merged
        # into someone else's.
        notes = []
        if os.path.normcase(os.path.normpath(new_root)) != \
                os.path.normcase(os.path.normpath(script_root)):
            renamed = ""
            try:
                from server import pending_albums
                renamed = pending_albums.rename_placeholder(new_root, script_root)
            except Exception:
                traceback.print_exc()
            if renamed:
                notes.append("the folder the add created was renamed to the "
                             "name the naming script gives the album")
                new_root = renamed

        # Post-organize cue maintenance: renaming audio underneath cue
        # sheets leaves stale FILE references, and album-named cues moved
        # by the leftovers pass keep names the CD-N grader rejects. Re-run
        # the same evidence-based engine the cue script uses.
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


# --------------------------------------------------------------------------- #
# Unpacked archives: the import path's one staging area for a user's archive
# --------------------------------------------------------------------------- #
# A dropped/picked archive is unpacked into <music>/.mlo/unpacked-<random> and
# its tree then goes through the SAME upload route a folder does (the files are
# already on the server's disk, so they are moved into the album rather than
# uploaded a second time). The state folder is the app's own — the library walk
# prunes it (SKIP_DIRS) — and the prefix is what marks a folder as one the app
# made, which is the only thing a server-side staged path may point into.
_UNPACK_PREFIX = "unpacked-"
# A folder whose wizard went away is swept the next time one is created. Nothing
# the user can see is deleted: the folder holds a copy of an archive's contents
# that was never imported.
_UNPACK_STALE_SECONDS = 24 * 3600


def _unpack_area(folder: str) -> str:
    """The realpath of the app's state folder — where unpacked trees live."""
    return os.path.realpath(mlo_root(folder))


def _sweep_unpacked(folder: str) -> None:
    """Delete unpacked trees a previous wizard run left behind (24h old)."""
    import shutil
    area = _unpack_area(folder)
    now = time.time()
    try:
        entries = os.listdir(area)
    except OSError:
        return
    for name in entries:
        if not name.startswith(_UNPACK_PREFIX):
            continue
        full = os.path.join(area, name)
        try:
            if not os.path.isdir(full) or now - os.path.getmtime(full) < _UNPACK_STALE_SECONDS:
                continue
        except OSError:
            continue
        shutil.rmtree(full, ignore_errors=True)


def _unpack_root(folder: str) -> str:
    """A fresh, app-made folder for one archive's contents."""
    area = _unpack_area(folder)
    os.makedirs(area, exist_ok=True)
    _sweep_unpacked(folder)
    return tempfile.mkdtemp(prefix=_UNPACK_PREFIX, dir=area)


def _staged_paths(blob: Optional[str], folder: str) -> List[tuple]:
    """The unpacked files the wizard kept, from its own JSON list.

    The body carries PATHS, not bytes: an archive the server unpacked needs no
    second upload. Each one comes back as ``(rel path, absolute path)`` — the
    relative half is the file's place inside the folder this app unpacked it
    into, which is what gives the album the structure the archive had (a rip's
    CD1/ folder, its .cue, its .log).

    The one rule that matters is WHERE such a path may point: a folder THIS app
    made under its own state root (<music>/.mlo/unpacked-*), resolved through
    realpath so a link cannot be used to name somebody else's file. Anything
    else is refused outright; the alternative is an upload route that will place
    any file on the server into the library on request.
    """
    if not blob:
        return []
    try:
        items = json.loads(blob)
    except ValueError:
        raise HTTPException(400, "staged is not a JSON list of paths")
    if not isinstance(items, list) or any(not isinstance(i, str) for i in items):
        raise HTTPException(400, "staged is not a JSON list of paths")
    area = _unpack_area(folder)
    out: List[tuple] = []
    for raw in items:
        real = os.path.realpath(os.path.normpath(raw))
        rel = os.path.relpath(real, area).replace("\\", "/")
        top = rel.split("/")[0]
        if rel.startswith("..") or not top.startswith(_UNPACK_PREFIX) or \
                len(rel.split("/")) < 2:
            raise HTTPException(400, "staged file outside the unpack area")
        if os.path.islink(raw) or not os.path.isfile(real):
            raise HTTPException(400, f"staged file is not a plain file: {rel}")
        out.append((rel[len(top) + 1:], real))
    return out


@app.post("/api/import/unpack")
async def import_unpack(
    file: Optional[UploadFile] = File(None),
    path: str = Query(None),
):
    """Unpack ONE archive into the app's staging area and list what came out.

    The wizard sends the user's archive either as bytes (a drop or a pick in the
    browser) or as a PATH (the desktop shell hands over OS paths; the shell and
    the server share a filesystem there). Both land in the same place: a fresh
    folder under <music>/.mlo, unpacked by `mlo.archives.extract`, whose rules
    are the reason an absolute member, a `..` escape, a link or a device node is
    refused BEFORE anything is written — with the reason in this reply, not a
    half-unpacked album in the library.

    The reply is what the wizard shows before it commits: the folder, every
    file in it, and how many of them are audio. `audio: 0` is a FACT the wizard
    turns into "this archive holds no audio" — an archive of images and sheets
    is not an empty album.

    Nothing is imported here: the extracted tree is only staged. It goes into
    the library through the existing upload route (the wizard passes the files
    it kept as `staged`), and `/api/import/unpack/discard` — or the 24h sweep —
    removes what was left.
    """
    cfg = load_config()
    folder = cfg.get("music_folder") or ""
    if not folder or not os.path.isdir(folder):
        raise HTTPException(400, "music_folder not set or not found")
    if file is None and not path:
        raise HTTPException(400, "send either an archive file or an archive path")
    import shutil
    label = ""
    root = ""
    tmp = ""          # the uploaded archive itself — never inside `root`
    try:
        if file is not None:
            label = os.path.basename((file.filename or "archive").replace("\\", "/"))
            if "." not in label:
                raise HTTPException(400, "the uploaded archive has no filename")
        else:
            source_path = os.path.realpath(os.path.normpath(path))
            if not os.path.isfile(source_path):
                raise HTTPException(404, "archive not found")
            label = os.path.basename(source_path)
        if archives_mod.import_kind(label) is None:
            raise HTTPException(
                400, f"{label}: not an archive this app can unpack — it "
                     f"reads {archives_mod.SUPPORTED_ARCHIVES}")
        root = _unpack_root(folder)
        if file is not None:
            fd, tmp = tempfile.mkstemp(
                prefix="incoming-", suffix=os.path.splitext(label)[1],
                dir=os.path.dirname(root))
            with os.fdopen(fd, "wb") as out:
                # A rip archive is hundreds of megabytes: off the event loop,
                # or every other request (and the progress socket) waits for it.
                await asyncio.to_thread(shutil.copyfileobj, file.file, out)
            source = tmp
        else:
            source = source_path

        try:
            await asyncio.to_thread(archives_mod.extract, source, root)
        except archives_mod.ArchiveError as e:
            raise HTTPException(400, str(e))

        audio_exes = tuple(AUDIO_EXTS)
        files_out = []
        audio = 0
        for walk_root, _dirs, names in os.walk(root):
            for name in names:
                full = os.path.join(walk_root, name)
                inside = os.path.relpath(full, root).replace("\\", "/")
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                if inside.lower().endswith(audio_exes):
                    audio += 1
                files_out.append({"relPath": inside,
                                  "path": full.replace("\\", "/"),
                                  "size": size})
        files_out.sort(key=lambda f: f["relPath"].lower())
        return {"ok": True, "label": label, "dir": root.replace("\\", "/"),
                "files": files_out, "unpacked": len(files_out), "audio": audio}
    except Exception:
        if root and os.path.isdir(root):
            shutil.rmtree(root, ignore_errors=True)
        raise
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


@app.post("/api/import/unpack/discard")
async def import_unpack_discard(
    dirs: List[str] = Form(default=[]),
):
    """Remove unpacked trees the wizard has finished with (or given up on).

    Only a folder this app made is ever removed: a name under the state root
    with the app's own prefix, and never a link. Everything else is answered as
    "not removed" rather than obeyed — this route takes paths from the client.
    """
    cfg = load_config()
    folder = cfg.get("music_folder") or ""
    if not folder or not os.path.isdir(folder):
        raise HTTPException(400, "music_folder not set or not found")
    import shutil
    area = _unpack_area(folder)
    removed, skipped = [], []
    for raw in dirs:
        real = os.path.realpath(os.path.normpath(raw))
        rel = os.path.relpath(real, area).replace("\\", "/")
        if rel.startswith("..") or "/" in rel or not rel.startswith(_UNPACK_PREFIX) or \
                not os.path.isdir(real) or os.path.islink(raw):
            skipped.append(str(raw).replace("\\", "/"))
            continue
        shutil.rmtree(real, ignore_errors=True)
        removed.append(rel)
    return {"ok": True, "removed": removed, "skipped": skipped}


def _place_staged(staged, album_path: str) -> None:
    """Move each staged file to its own relative path inside `album_path`.

    The album folder is made if it does not exist. A file a lock holds refuses
    the whole placement with the app's usual sentence — move_path retried every
    sharing violation and never copies blindly, so the source is still whole.
    """
    os.makedirs(album_path, exist_ok=True)
    for rel, src in staged:
        dest = os.path.join(album_path, *rel.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if not move_path(src, dest):
            raise HTTPException(
                500, f"could not place {rel} — a file inside it is "
                     f"still in use (stop playback and retry)")


@app.post("/api/import/upload")
async def import_upload(
    target_dir: str = Query(...),
    files: List[UploadFile] = File(default=[]),
    staged: Optional[str] = Form(None),
):
    """Upload files into a new album directory under the library (Artists).

    Filenames may contain relative subpaths (e.g. "CD1/01 - Intro.flac")
    so whole album folders keep their internal structure. Path traversal
    and absolute paths are rejected.

    `target_dir` is free text from the wizard's album-name field, so only its
    BASENAME is used: `_in_music_folder` alone would accept a name like
    "../Escape" (it resolves to <music>/Escape — inside the music folder, but
    outside Artists/ where the library actually lives).

    ONE song out of an album does not become an album of its own: when the
    whole upload is a single track, `target_dir` names that track (the wizard
    has no album to name for a dropped file) and the file is placed on the
    album it belongs to instead — see `server.imports.import_album_target`,
    which reads the release identity the library already holds and the album
    the file's own tags state. The response's `album_path`/`album_name` are
    the album it actually landed in, and `merged` says the library already
    had it.

    `staged` is the other way in, for files that are ALREADY on this server: an
    archive the wizard unpacked (`POST /api/import/unpack`) or a file the
    desktop shell handed over as a path. A JSON list of absolute paths inside
    the app's own unpack area, moved into the album rather than uploaded again —
    everything after that point is the same code as a byte upload, so an album
    that came out of an archive is grouped, merged and marked partial exactly
    like the folder it was meant to be.
    """
    cfg = load_config()
    folder = cfg.get("music_folder") or ""
    if not folder or not os.path.isdir(folder):
        raise HTTPException(400, "music_folder not set or not found")
    if not files and not staged:
        raise HTTPException(400, "no files to upload")
    raw = os.path.basename(target_dir)
    safe = re_safe_filename(raw) or "Imported"
    # A name of nothing but dots or blanks has no legal spelling: os.path
    # .basename("..") is "..", and joined to the Artists folder that resolves
    # to the MUSIC FOLDER itself, which the containment guard accepts —
    # uploads and ingests would land in the library root. sanitize_segment
    # already rewrites ".." to "__"; this refuses it outright instead of
    # silently inventing a folder for it.
    if not raw.strip(" ."):
        raise HTTPException(400, "invalid album name")
    root = library_root(folder)
    target = os.path.normpath(os.path.join(root, safe))
    if not _in_music_folder(target, folder):
        raise HTTPException(400, "target outside music folder")
    # Staged inside the app's own state folder (<music>/.mlo — the one the
    # library walk prunes, SKIP_DIRS), then placed in one rename: WHICH album
    # these tracks belong to is not known until they are on disk, because the
    # answer is in the files themselves (a single song's own ALBUM/MBID tags,
    # or the release identity the library already holds — see
    # server.imports.import_album_target). A staging folder under Artists/
    # would be listed as an album while the upload is in flight; keeping it in
    # the music folder keeps the placement a same-volume rename.
    try:
        state = mlo_root(folder)
        os.makedirs(state, exist_ok=True)
        stage = tempfile.mkdtemp(prefix="import-", dir=state) if files else ""
    except OSError as e:
        raise HTTPException(500, f"could not stage the upload: {e}")
    staged_files = []  # (rel path as sent, staged absolute path)
    try:
        for f in files:
            name = (f.filename or "file").replace("\\", "/")
            parts = [p for p in name.split("/") if p and p not in (".", "..")]
            if not parts:
                continue
            # reject absolute/escaping paths
            if os.path.isabs(name) or ".." in name.split("/"):
                raise HTTPException(400, f"unsafe filename: {name!r}")
            dest = os.path.join(stage, *parts)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            data = await f.read()
            # Large albums must not stall the event loop (it would freeze the
            # progress websocket and every other request mid-upload).
            await asyncio.to_thread(_write_upload_bytes, dest, data)
            staged_files.append(("/".join(parts), dest))
        # Files the server already holds (an unpacked archive): the same list,
        # with the paths they have inside their own unpacked root, so the album
        # comes out of an archive with the structure the archive gave it.
        staged_files.extend(_staged_paths(staged, folder))
        if not staged_files:
            raise HTTPException(400, "no files to upload")
        album_name, album_path, how = imports_svc.import_album_target(
            [p for rel, p in staged_files if rel.lower().endswith(AUDIO_EXTS)], raw, cfg)
        if not album_path:
            album_path = os.path.normpath(
                os.path.join(root, re_safe_filename(album_name) or safe))
        if not _in_music_folder(album_path, folder):
            raise HTTPException(400, "target outside music folder")
        merged = bool(how == "library" and os.path.isdir(album_path))
        if merged:
            # The library already holds this album: fill it, and never
            # overwrite what it has (its own rip sheets, a same-named track).
            imports_svc.merge_into_album(album_path, [p for _rel, p in staged_files])
        elif os.path.isdir(album_path) or not stage:
            # Either the same album again — the bytes land where they are sent,
            # as they always have (a retried upload replaces its own files) — or
            # files the server already held (an unpacked archive), whose album
            # folder is made here and filled file by file.
            _place_staged(staged_files, album_path)
        else:
            if not move_path(stage, album_path):
                raise HTTPException(
                    500, f"could not import {os.path.basename(album_path)} — a "
                         f"file inside it is still in use (stop playback and "
                         f"retry)")
            stage = ""
        saved = [os.path.join(album_path, *rel.split("/")).replace("\\", "/")
                 for rel, _p in staged_files]
        # A rip that shipped its sheets and only part of its tracks just told
        # us what the album is missing (server.imports.record_sidecar_
        # tracklist): record it here, so ONE track of a CD rip is a partial
        # release from the moment it lands, not a one-track album.
        imports_svc.record_sidecar_tracklist(album_path, cfg)
    finally:
        if stage and os.path.isdir(stage):
            import shutil
            shutil.rmtree(stage, ignore_errors=True)
    tagcache.invalidate_all()
    return {"ok": True, "saved": saved,
            "album_path": album_path.replace("\\", "/"),
            "album_name": os.path.basename(album_path), "merged": merged}


def _write_upload_bytes(dest: str, data: bytes) -> None:
    with open(dest, "wb") as out:
        out.write(data)


@app.post("/api/import/scan")
def import_scan(path: str = Query(...)):
    """Recursively list a folder's files with relative paths (drag-drop or a
    typed/known folder path — there is no in-app folder picker).

    The folder may live anywhere — the follow-up ingest step moves it into
    the library.

    A single FILE is answered as a one-entry listing: a desktop shell hands
    over OS drop PATHS, and "one track dragged out of a folder" is a normal
    thing to drop. The folder it lists itself as its own root then reads as one
    file at the top level, which is exactly what the ingest step's single-track
    rule expects.
    """
    p = os.path.realpath(os.path.normpath(path))
    if os.path.isfile(p):
        try:
            size = os.path.getsize(p)
        except OSError:
            size = 0
        return {"root": p.replace("\\", "/"), "file": True,
                "files": [{"relPath": os.path.basename(p).replace("\\", "/"),
                           "size": size}]}
    if not os.path.isdir(p):
        raise HTTPException(404, "folder not found")
    out = []
    # honey: pre-ingest scan must accept folders outside the library (native
    # picker, downloads); realpath + no-followlinks + entry cap instead of a
    # music-folder guard, which would break the import flow.
    for root, dirs, files in os.walk(p, followlinks=False):
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, p).replace("\\", "/")
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            out.append({"relPath": rel, "size": size})
            if len(out) >= 5000:
                break
        if len(out) >= 5000:
            break
    out.sort(key=lambda x: x["relPath"].lower())
    return {"root": p.replace("\\", "/"), "files": out}


def _ingest_one_file(src, target, cfg, folder):
    """Place ONE file that already sits on this server's disk.

    What a desktop shell's OS drop yields is a path, not bytes, and one track
    dragged out of a folder is the ordinary case. It goes through the SAME
    single-track rule a folder holding one track does
    (`imports.import_album_target`): the track joins the album the library
    already holds, or the album its own tags name, and only a file that answers
    neither becomes a folder named after the wizard's field. The file MOVES —
    an ingest of a path is the user handing the app that file, exactly as
    dropping a folder moves the folder.
    """
    raw = os.path.basename(target or os.path.basename(src))
    name = re_safe_filename(raw)
    if not name or not raw.strip(" ."):
        raise HTTPException(400, "invalid album name")
    _got, album_path, _how = imports_svc.import_album_target([src], raw, cfg)
    if not album_path:
        album_path = os.path.normpath(os.path.join(library_root(folder), name))
    if not _in_music_folder(album_path, folder):
        raise HTTPException(400, "target outside music folder")
    os.makedirs(album_path, exist_ok=True)
    dest_file = os.path.join(album_path, os.path.basename(src))
    if os.path.normcase(os.path.abspath(dest_file)) != os.path.normcase(os.path.abspath(src)):
        if not move_path(src, dest_file):
            raise HTTPException(
                500, f"could not import {os.path.basename(src)} — a file inside "
                     f"it is still in use (stop playback and retry)")
    tagcache.invalidate_all()
    mbresolve.invalidate()
    imports_svc.record_sidecar_tracklist(album_path, cfg)
    return {"ok": True, "path": album_path.replace("\\", "/"),
            "album_name": os.path.basename(album_path), "merged": False}


def _ingest_paths(source, target):
    """The folders ``POST /api/import/ingest`` touches: the album being moved
    and the library folder it lands in.

    `target` is an album NAME, not a path (the body resolves it to
    ``<library>/<target>``), so the claim has to resolve it the same way —
    otherwise the destination of the very move this guard protects would be
    unclaimed, and a same-named album could be organized while it is being
    replaced.
    """
    name = re_safe_filename(os.path.basename(target or os.path.basename(source)))
    folder = str(load_config().get("music_folder") or "")
    paths = [source]
    if folder and name:
        paths.append(os.path.join(library_root(folder), name))
    # A folder holding nothing but one track is that TRACK, and the album it
    # belongs to is its destination (see the upload route): the job lock has
    # to claim the folder the move will really touch, or a same-named album
    # could be organized while the file is being placed in it.
    only = _only_audio(source)
    if len(only) == 1:
        try:
            _n, album_path, _how = imports_svc.import_album_target(
                only, os.path.basename(target or source), load_config())
        except Exception:
            album_path = ""
        if album_path and album_path not in paths:
            paths.append(album_path)
    return paths


def _only_audio(folder):
    """The folder's audio files, or [] — its contents when it holds one
    track and nothing else."""
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    return [os.path.join(folder, f) for f in names
            if f.lower().endswith(AUDIO_EXTS) and os.path.isfile(os.path.join(folder, f))]


@app.post("/api/import/ingest")
@job_locks.holds(_ingest_paths, kind="import", label="Import album")
def import_ingest(source: str = Query(...), target: str = Query(...)):
    """Move an album folder into the library. Same volume it is one rename;
    across devices it is a verified copy followed by the source's removal —
    never a blind copytree + rmtree.

    A folder holding ONE track is not an album: it is placed on the album the
    track belongs to (see `server.imports.import_album_target`), and a folder
    that ships the rip's sheets with only part of its tracks has the sheets'
    tracklist recorded, so the album reads as partial.

    A single FILE is that same case with the file named directly — what a
    desktop shell's OS drop yields (paths, not bytes). It is placed by the one
    single-track rule rather than inventing a one-file album.
    """
    cfg = load_config()
    folder = cfg.get("music_folder") or ""
    if not folder or not os.path.isdir(folder):
        raise HTTPException(400, "music_folder not set or not found")
    src = os.path.normpath(source)
    if os.path.isfile(src):
        return _ingest_one_file(src, target, cfg, folder)
    if not os.path.isdir(src):
        raise HTTPException(404, "source folder not found")
    raw = os.path.basename(target or os.path.basename(src))
    name = re_safe_filename(raw)
    # Nothing but dots/blanks has no legal spelling — see the upload route
    # above: ".." joined to the library root IS the library root.
    if not name or not raw.strip(" ."):
        raise HTTPException(400, "invalid album name")
    root = library_root(folder)
    dest = os.path.normpath(os.path.join(root, name))
    if not _in_music_folder(dest, folder):
        raise HTTPException(400, "target outside music folder")
    if os.path.normcase(os.path.abspath(dest)) == os.path.normcase(os.path.abspath(src)):
        return {"ok": True, "path": dest.replace("\\", "/"), "album_name": name}
    only = _only_audio(src)
    if len(only) == 1:
        got, album_path, how = imports_svc.import_album_target(only, raw, cfg)
        if album_path and os.path.isdir(album_path):
            # The library already holds this album: the track joins it, and
            # the album becomes partial.
            one = os.path.join(album_path, os.path.basename(only[0]))
            if not move_path(only[0], one):
                raise HTTPException(
                    500, f"could not place {os.path.basename(only[0])} into "
                         f"{os.path.basename(album_path)} — a file inside it "
                         f"is still in use (stop playback and retry)")
            try:
                os.rmdir(src)  # the one file left: the folder goes with it
            except OSError:
                pass
            tagcache.invalidate_all()
            mbresolve.invalidate()
            imports_svc.record_sidecar_tracklist(album_path, cfg)
            return {"ok": True, "path": album_path.replace("\\", "/"),
                    "album_name": os.path.basename(album_path), "merged": True}
        if got and re_safe_filename(got) != name:
            # The track's own tags name the album: the folder it is imported
            # into is the album's, not the track's.
            name = re_safe_filename(got)
            dest = os.path.normpath(os.path.join(root, name))
            if not _in_music_folder(dest, folder):
                raise HTTPException(400, "target outside music folder")
    n = 2
    while os.path.exists(dest):
        dest = os.path.normpath(os.path.join(root, f"{name} ({n})"))
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
    imports_svc.record_sidecar_tracklist(dest, cfg)
    return {"ok": True, "path": dest.replace("\\", "/"),
            "album_name": os.path.basename(dest), "merged": False}


@app.post("/api/import/commit")
def import_commit(req: ImportCommit):
    """Store MB/RYM links on every track of a freshly imported album.

    target_dir: album folder name under the library (Artists).
    All three links — the MB release, the album page and (when the wizard
    confirmed it) the artist page — land in ONE pass over the album, one
    container write per track.
    """
    cfg = load_config()
    folder = cfg.get("music_folder") or ""
    if not folder or not os.path.isdir(folder):
        raise HTTPException(400, "music_folder not set or not found")
    target = os.path.normpath(req.target_dir)
    if not os.path.isabs(target):
        target = os.path.normpath(os.path.join(library_root(folder), req.target_dir))
    _guard_folder(target, req.staged, "target", folder)
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
    # The artist page is artist-level, so it goes on every track as
    # RATEYOURMUSIC_ARTIST — and it goes into the SAME per-track map as the two
    # album tags above, so the deferral below still turns the whole step into
    # ONE container write per track. Only a page RYM's own kind check calls an
    # artist is stored: a song or album link here would be a wrong artist link
    # forever (every later import stamps only what no tag already holds), and it
    # is what the wizard's field validation confirmed before sending it.
    artist = (req.rym_artist_link or "").strip()
    if intg.rym_url_kind(artist) == "artist":
        for p in changes:
            changes[p]["RATEYOURMUSIC_ARTIST"] = artist
    # One AudioFile per track, and every tag for that track inside ONE
    # container write. Without the deferral each set_tag costs a whole-file
    # copy + rewrite (mlo/atomic.rewrite_via), and this step stamps up to
    # three tags per track — it is the wizard's "Saving links…", so that
    # difference is the step's whole duration on a real album. The files are
    # independent (rewrite_via writes its own temp beside its target and swaps
    # it in), so they also go through the pool every other per-file pass uses.
    from concurrent.futures import ThreadPoolExecutor
    from mlo.stats import worker_count

    def write_one(p, tag_map):
        af = AudioFile(os.path.normpath(p))
        if af.audio is None:
            return f"{p}: {af.error or 'unreadable'}"
        defer = hasattr(af, "defer_save")
        if defer:
            af.defer_save(True)
        error = None
        try:
            for k, v in tag_map.items():
                # A value already stored is not written again: walking the
                # wizard twice must cost nothing and must not touch the file.
                if str(af.get_tag(k) or "").strip() == str(v).strip():
                    continue
                if not af.set_tag(k, v):
                    error = f"{p} {k}: {af.error}"
                    break
        except Exception as e:
            error = f"{p}: {e}"
        finally:
            # The flush IS the write, so its verdict is the file's verdict —
            # and it has to run on the error path too, or a failed track is
            # left holding changes nothing will save.
            if defer and af.defer_save(False) is False and error is None:
                error = f"{p}: {af.error or 'tag write failed'}"
        if error is None:
            tagcache.invalidate_path(os.path.normpath(p))
        return error

    errors = []
    items = [(p, t) for p, t in changes.items() if t]
    if items:
        workers = worker_count(cfg, default=8, maximum=8, items=len(items))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            errors = [e for e in ex.map(lambda a: write_one(*a), items) if e]
    if errors:
        raise HTTPException(500, "; ".join(errors))
    return {"ok": True, "changed": len(changes)}


@app.post("/api/import/expected")
def import_expected(req: ImportExpected):
    """Record the release's full tracklist on the album folder.

    This is what makes a PARTIAL import legible: the album page diffs the
    files on disk against this list and greys out the ones that never came
    in. Sending an empty `tracks` clears the manifest again (a full import
    leaves nothing behind) — unless the album's own .cue/.log states a
    tracklist that is not complete on disk, in which case THAT is recorded
    (the rip's own answer is better than none)."""
    from mlo.paths import save_expected_tracks
    cfg = load_config()
    folder = cfg.get("music_folder") or ""
    if not folder or not os.path.isdir(folder):
        raise HTTPException(400, "music_folder not set or not found")
    target = os.path.normpath(req.target_dir)
    if not os.path.isabs(target):
        target = os.path.normpath(os.path.join(library_root(folder), req.target_dir))
    _guard_folder(target, req.staged, "target", folder)
    if not os.path.isdir(target):
        raise HTTPException(404, "album not found")
    tracks = req.tracks or []
    if not tracks:
        # An empty request is normally "clear the manifest", but it is ALSO
        # what a release lookup with no tracklist sends. A rip that shipped
        # its .cue/.log knows what the album holds, so that is recorded
        # instead of discarding the only statement of what is missing: it
        # writes only when the folder has no manifest of its own and part of
        # the rip is not there (server.imports.record_sidecar_tracklist).
        recorded = imports_svc.record_sidecar_tracklist(target, cfg)
        if recorded:
            tagcache.invalidate_all()
            return {"ok": True, "tracks": len(recorded["tracks"]),
                    "source": "sidecars"}
    if not save_expected_tracks(target, req.release_id, tracks):
        raise HTTPException(500, "could not write the release tracklist")
    tagcache.invalidate_all()
    return {"ok": True, "tracks": len(tracks)}


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


def _staging_listing(path):
    """One staging folder as the page needs it: totals plus newest-first
    entries, each in `_downloads_entry`'s shape minus its private `_mtime`.

    A missing or unreadable folder is reported as empty and never as an
    error: slskd creates both roots on its own schedule, so the page polls
    this and must not see a failure just because nothing is staged yet."""
    out = {"folder": (os.path.abspath(path) if path else "").replace("\\", "/"),
           "exists": False, "count": 0, "bytes": 0, "entries": []}
    if not path or not os.path.isdir(path):
        return out
    root = os.path.realpath(path)
    try:
        names = os.listdir(root)
    except OSError:
        return out
    entries = [e for e in (_downloads_entry(root, n) for n in names) if e]
    entries.sort(key=lambda e: e["_mtime"], reverse=True)
    for e in entries:
        del e["_mtime"]
    out.update(exists=True, count=len(entries), entries=entries,
               bytes=sum(e["bytes"] for e in entries))
    return out


@app.get("/api/downloads")
def downloads_list():
    """Contents of <music_folder>/.mlo/downloads, newest first.

    This is slskd's staging area: everything it pulls down lands here and
    stays until it is imported into the library or deleted. Enough is reported
    per entry (size, file counts, whether it holds audio) for the page to
    offer Import only where it is meaningful."""
    folder = load_config().get("music_folder") or ""
    out = _staging_listing(_downloads_dir(folder))
    out["music_folder"] = folder.replace("\\", "/")
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
# /api/soulseek/staging — both of slskd's staging folders, by NAME
# --------------------------------------------------------------------------- #
# slskd writes finished files to the download dir and partials to its sibling
# `incomplete` dir (see soulseek._incomplete_dir). The app manages both from
# the Soulseek page, but the client only ever names a root — the path is
# resolved here from the config, so no request can aim a delete elsewhere.
def _staging_root(cfg, root):
    """The configured path of the staging root NAMED `root`, else None.

    Both come from soulseek.download_dir / _incomplete_dir, NOT from
    _downloads_dir(music_folder): slskd is configured with the former pair, so
    a custom `soulseek_download_dir` (and the `incomplete` sibling derived from
    it) is where the bytes actually are — the music-folder-derived path would
    show the page a folder slskd never writes to, or miss one it does."""
    from server import soulseek
    if root == "downloads":
        return soulseek.download_dir(cfg)
    if root == "incomplete":
        return soulseek._incomplete_dir(cfg)
    return None


def _staging_remove(root, name):
    """Delete one entry under *root*. Returns (error, bytes freed).

    Size is measured BEFORE the bytes go, since after rmtree they are gone.
    A symlink is unlinked, never followed: the name guard only cleared the
    link's own segment, so following it would delete a target that may live
    outside the root entirely."""
    import shutil
    p = os.path.join(root, name)
    try:
        if os.path.islink(p):
            os.remove(p)
            return None, 0
        if os.path.isdir(p):
            # _dir_stats reports an unreadable part as 0 rather than raising,
            # so a locked subfolder cannot turn the count into a failure.
            size = _dir_stats(p)[1]
            shutil.rmtree(p)
        else:
            size = os.path.getsize(p)
            os.remove(p)
        return None, size
    except OSError as e:
        return (str(e) or "delete failed"), 0


@app.get("/api/soulseek/staging")
def soulseek_staging():
    """Both slskd staging folders: `downloads` (finished, waiting to be
    imported) and `incomplete` (in-flight partials).

    Each root is reported on its own — a missing one is `exists: false` with
    empty totals, never an error, because slskd creates them on its own
    schedule and the page polls this."""
    cfg = load_config()
    from server import soulseek
    return {"downloads": _staging_listing(soulseek.download_dir(cfg)),
            "incomplete": _staging_listing(soulseek._incomplete_dir(cfg))}


@app.post("/api/soulseek/staging/delete")
def soulseek_staging_delete(req: StagingRequest):
    """Delete ONE entry (file or folder tree) from a named staging root.

    Unlike POST /api/downloads/delete this is a single all-or-nothing action:
    the page deletes what the user picked, so the outcome is either "gone"
    (with the bytes it freed) or a status the UI can explain."""
    cfg = load_config()
    path = _staging_root(cfg, (req.root or "").strip().lower())
    if path is None:
        raise HTTPException(400, "unknown staging root")
    if not os.path.isdir(path):
        raise HTTPException(404, "staging folder not found")
    root = os.path.realpath(path)
    # Same guard the downloads routes use: basename-only, and anything whose
    # realpath leaves the root (traversal, absolute path, symlink) is refused.
    err = _downloads_name_error(req.name, root)
    if err:
        raise HTTPException(400, err)
    if not os.path.lexists(os.path.join(root, req.name)):
        raise HTTPException(404, "entry not found in staging")
    err, freed = _staging_remove(root, req.name)
    if err:
        raise HTTPException(502, err)
    tagcache.invalidate_all()
    _refresh_slskd_shares_soon()
    return {"ok": True, "freed": freed}


@app.post("/api/soulseek/staging/clear")
def soulseek_staging_clear(req: StagingRequest):
    """Empty a named staging root: every entry in it, nothing else.

    One entry that will not delete (a file slskd still holds open is expected
    while a transfer is running) is reported in `failed` and the rest are
    still removed. The root itself is never removed — slskd validates it at
    boot and would refuse to start without it."""
    cfg = load_config()
    path = _staging_root(cfg, (req.root or "").strip().lower())
    if path is None:
        raise HTTPException(400, "unknown staging root")
    if not os.path.isdir(path):
        raise HTTPException(404, "staging folder not found")
    root = os.path.realpath(path)
    try:
        names = os.listdir(root)
    except OSError as e:
        raise HTTPException(502, str(e) or "could not read the staging folder")
    cleared, freed, failed = 0, 0, []
    for name in names:
        err = _downloads_name_error(name, root)
        if err is None and not os.path.lexists(os.path.join(root, name)):
            err = "not found in staging"
        if err is None:
            err, size = _staging_remove(root, name)
            if err is None:
                cleared += 1
                freed += size
        if err:
            failed.append({"name": name, "reason": err})
    if cleared:
        tagcache.invalidate_all()
        _refresh_slskd_shares_soon()
    return {"ok": True, "cleared": cleared, "freed": freed, "failed": failed}


# --------------------------------------------------------------------------- #
# Library layout — is the music folder shaped the way the app expects?
# --------------------------------------------------------------------------- #
# The walk itself lives in mlo.layout — the same one Run All runs as script 20
# — so this route, the panel and the script can never report different numbers.
# The GET route stays a READ-ONLY report: it says what is wrong and where, and
# never moves anything on its own. The fixing half is mlo.layout.apply_fixes,
# reached from here through POST /api/library/layout/apply (the panel's Apply
# fixes) and by script 20 itself, which applies what its scan proved.
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
    description.txt (only audio with no album folder is reported there).

    The rows that CAN be fixed carry what the fix would do; POST
    /api/library/layout/apply is what carries it out."""
    cfg = load_config()
    report = mlo_layout.scan_library(cfg)
    # A manual scan IS a scan: it is what "the last scan" means to the Library
    # page's warning, so it is remembered exactly the way script 20 remembers
    # its own.
    mlo_layout.save_report(cfg, report)
    return report


@app.get("/api/library/layout/report")
def library_layout_report():
    """The layout report the last scan stored (script 20, or this panel's Scan).

    Nothing is walked here: the Library page asks for this on every load, and
    the point of the stored report is that its warning costs no second scan of
    the library. An install that never scanned gets `exists: False` — a
    warning drawn from a scan that did not happen is the one thing this must
    never produce."""
    return mlo_layout.load_report(load_config())


@app.get("/api/grades/summary")
def grades_summary():
    """Whether the library passes its grading checks, and — when it does not —
    what fails, with a link target for every row.

    Read-only: nothing is graded, re-graded or written. It reads the library
    payload the Library page already fetches and says which of its albums and
    tracks came out below their checks (server.recommendations.grade_warning,
    the same object `/api/home` carries as `grade_warning`). The Library page
    has no Home payload, so without this route the two pages would each have
    to count the library themselves and could disagree."""
    from server import recommendations
    return recommendations.grade_warning(lib_mod.build_library(load_config()))


@app.post("/api/library/layout/remove-empty-artist")
def library_layout_remove_empty_artist(req: AlbumRemove, request: Request = None):
    """Move an album-less artist folder into <music>/.mlo/trash/<user>/.

    The one thing the layout panel may act on, and the removal goes through the
    app's own Trash — never shutil.rmtree — so it is recoverable from the Trash
    page like any album the library removed.

    The finding is re-derived HERE, from the folder itself, instead of trusting
    the panel: an artist folder is removable only while mlo.layout's
    `empty_artist` says so — no album folder under it, and no audio anywhere
    beneath. A folder that gained an album since the scan, or that was never
    one of these, is refused, not moved.
    """
    cfg = load_config()
    folder = cfg.get("music_folder") or ""
    if not folder or not os.path.isdir(folder):
        raise HTTPException(400, "music_folder not set or not found")
    p = os.path.normpath(req.path)
    if not os.path.isdir(p):
        raise HTTPException(404, "artist folder not found")
    if not _in_music_folder(p, folder):
        raise HTTPException(400, "artist folder outside music folder")
    lib = library_root(folder)
    # Directly inside <music>/Artists: an album folder is not an artist folder,
    # and nothing above Artists/ is ever removable through this route.
    if not lib or os.path.normcase(os.path.dirname(p)) != os.path.normcase(
            os.path.normpath(lib)):
        raise HTTPException(400, "not an artist folder (must sit in Artists/)")
    if not mlo_layout.empty_artist(p):
        raise HTTPException(
            400, "this artist folder is not an empty artist — it holds an album "
                 "or audio, and this route never moves an artist with music")
    # The same mlo.paths helper mlo.layout's apply phase trashes through, so
    # the panel's "remove" and script 20's automatic removal land an entry in
    # the identical bin with the identical origin recorded.
    dest = trash_path(p, folder, auth_mod.current_user(request))
    if not dest:
        raise HTTPException(
            500,
            f"could not move {os.path.basename(p) or 'artist'} to the trash — a "
            f"file inside it is still in use (stop playback and retry)")
    tagcache.invalidate_all()
    mbresolve.invalidate()
    _refresh_slskd_shares_soon()
    return {"ok": True, "trash": dest.replace("\\", "/")}


@app.post("/api/library/layout/apply")
@job_locks.holds(
    lambda request=None, **_: [library_root(load_config().get("music_folder") or "")],
    kind="layout", label="Optimize library layout")
def library_layout_apply(request: Request = None):
    """Scan the library and SETTLE what the folder itself proves — script 20,
    on demand.

    What the Optimization page's Apply fixes button runs, and the same call
    script 20 makes for itself: names spelled in the wrong letter case are
    renamed to the naming script's spelling, audio that is not in an album
    folder is moved into the one its own tags name, and what is EXCESS goes to
    the app's Trash — a stray file, a folder inside an album that holds no
    audio, an album folder with no audio in it, a foreign root folder holding
    no audio, an album-less artist folder, the old layout's ``.mlo_*``
    leftovers. A foreign folder that HOLDS AUDIO and a hidden folder inside
    ``Artists/`` are reported and left: nothing here can say where their
    contents belong (R185). Every removal's reason is re-derived from the
    folder at the move, so a folder that gained audio since the scan is
    refused. Nothing is ever deleted — the Trash lists every removal and can
    put it back — and no file outside the music folder is touched.

    The whole library, not a target list: this is the panel's action on the
    library it is showing. A targeted run is what the import chain does with
    script 20, where the target IS the album just written.

    Returns the report — the rows left AFTER the fixes, plus `fixes`
    (fixed/failed/skipped, in words) — and stores it, because the panel and the
    Library page's warning read the same numbers by design.

    It runs whether or not ``layout_apply`` is on: that setting is about what a
    SCAN does on its own, and a button reading "Apply fixes" is the user's own
    instruction rather than the scanner's default.
    """
    cfg = load_config()
    folder = cfg.get("music_folder") or ""
    if not folder or not os.path.isdir(folder):
        raise HTTPException(400, "music_folder not set or not found")
    report = mlo_layout.scan_library(cfg)
    mlo_layout.apply_fixes(cfg, report, user=auth_mod.current_user(request))
    mlo_layout.save_report(cfg, report)
    # The library moved under both caches, exactly as a removal does.
    tagcache.invalidate_all()
    mbresolve.invalidate()
    _refresh_slskd_shares_soon()
    return report


# --------------------------------------------------------------------------- #
# Web Push (the transport behind server/events.py)
# --------------------------------------------------------------------------- #
# The device half of the event channel: a browser subscribes here and is then
# woken with the same frames /ws/events carries, with the app closed. The
# routes are thin on purpose — the key material, the encryption and the fan-out
# all live in server/events.py, and the subscription rows in server/auth.py's
# database — so there is exactly one place that knows how a push is sent.
class PushSubscription(BaseModel):
    endpoint: str
    keys: dict
    # What this device asked to be woken for; empty/absent means "everything
    # the server publishes".
    kinds: Optional[List[str]] = None


class PushEndpoint(BaseModel):
    endpoint: str


@app.get("/api/push/status")
def push_status(request: Request):
    """Whether push can be offered here, the key to subscribe with, and how
    many devices this user already has registered.

    A client asks BEFORE it shows the switch: an install whose `cryptography`
    is missing, or a deployment that has no key material, must not be offered a
    button that cannot work (see lib/notify.ts).
    """
    user = auth_mod.current_user(request)
    return {
        "available": bool(events_mod.push_public_key()),
        "public_key": events_mod.push_public_key(),
        "subscriptions": auth_mod.push_subscription_count(user),
    }


@app.post("/api/push/subscribe")
def push_subscribe(body: PushSubscription, request: Request):
    """Register (or refresh) this device.

    The endpoint and the two keys come from the browser's own PushManager; the
    row is scoped to the caller's user, so the news lands on the devices of the
    person who set them up. Re-posting the same endpoint updates it in place —
    that is what the client does on every load (a browser rotates its key
    material, and a stale row would encrypt to a key nobody holds any more).
    """
    endpoint = (body.endpoint or "").strip()
    keys = body.keys or {}
    p256dh = str(keys.get("p256dh") or "").strip()
    auth_key = str(keys.get("auth") or "").strip()
    # A push endpoint is always https (RFC 8030 §5: the push service is reached
    # over TLS), and the keys are a P-256 point and a 16-byte secret. Rejecting
    # the rest here keeps an unusable row out of the database — `_send_one`
    # would only discover it after the next import finished.
    if not endpoint.startswith("https://") or len(endpoint) > 2048:
        raise HTTPException(400, "not a push endpoint")
    if not events_mod.key_size_ok(p256dh, 65) or not events_mod.key_size_ok(auth_key, 16):
        raise HTTPException(400, "not a browser key pair")
    if not events_mod.push_public_key():
        raise HTTPException(503, "push is unavailable on this server")
    user = auth_mod.current_user(request)
    auth_mod.push_subscribe(user, endpoint, p256dh, auth_key, body.kinds)
    return {"ok": True, "subscriptions": auth_mod.push_subscription_count(user)}


@app.post("/api/push/unsubscribe")
def push_unsubscribe(body: PushEndpoint, request: Request):
    """Forget this device (the switch turned off, or a sign-out).

    Scoped to the caller: one user cannot unregister another's device.
    """
    removed = auth_mod.push_unsubscribe(
        (body.endpoint or "").strip(), auth_mod.current_user(request))
    return {"ok": True, "removed": removed}


@app.post("/api/push/test")
def push_test(request: Request):
    """Send one test frame to THIS user's devices.

    The button that lets somebody prove it on their own phone instead of
    believing a settings page. It answers what really happened — how many
    devices took it, how many were dead and how many failed — because "sent"
    with a silent zero is exactly the feedback this feature must not give.
    """
    result = events_mod.send_test(auth_mod.current_user(request))
    if not result.get("available"):
        raise HTTPException(503, "push is unavailable on this server")
    return result


# --------------------------------------------------------------------------- #
# WebSocket + static
# --------------------------------------------------------------------------- #
@app.websocket("/ws/progress")
async def ws_progress(ws: WebSocket):
    """The script-progress relay.

    Gated exactly like /ws/events: the frames carry album and track paths and
    the running step's own text, so an unauthenticated peer that could open
    this socket would read the library's layout and live activity. The token
    rides the query string for the same reason it does on /ws/events (a
    browser WebSocket cannot set an Authorization header, and the shells'
    origin is not the API's).
    """
    token = (ws.query_params.get("token") or "").strip() or (ws.cookies.get("mlo_session") or "")
    state = await asyncio.to_thread(auth_mod.current_state)
    if auth_mod.requires_login(ws, state) and not (token and await asyncio.to_thread(auth_mod.valid_session, token)):
        # the client could then not tell "sign in again" (4401) from "server
        # down" and would retry a stale token forever.
        await ws.accept()
        await ws.close(code=4401)
        return
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


@app.websocket("/ws/events")
async def ws_events(ws: WebSocket):
    """The notification channel: every frame `server.events.emit()` publishes.

    Separate from /ws/progress because the two have different consumers and
    different lifetimes — the progress socket is the page that is running a
    script, this one is every client that wants to know about wishes and
    downloads, which may be a phone in another room. It is also the one route
    where the token travels in the query string: a browser WebSocket cannot
    set an Authorization header, and the desktop/mobile shell's origin is not
    the API's, so neither the header nor a same-site cookie is available.

    `?since=<unix seconds>` replays what the ring still holds, so a client
    that was asleep or reconnecting does not miss the wish that landed while
    it was away.
    """
    token = (ws.query_params.get("token") or "").strip() or (ws.cookies.get("mlo_session") or "")
    state = await asyncio.to_thread(auth_mod.current_state)
    if state.get("required") and not (token and await asyncio.to_thread(auth_mod.valid_session, token)):
        # Accepted first, then closed — see ws_progress: a pre-accept close is
        # an HTTP 403 the browser surfaces as 1006, which would hide the 4401
        # the client's reconnect policy is built on.
        await ws.accept()
        await ws.close(code=4401)
        return
    await ws.accept()
    try:
        since = float(ws.query_params.get("since") or 0.0)
    except (TypeError, ValueError):
        since = 0.0
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    # The replay is snapshotted BEFORE subscribing, and the live queue then
    # skips anything the snapshot already carried: subscribing first sent an
    # event that arrived in between twice (once replayed, once live).
    past = events_mod.recent(since)
    high_water = max([int(e.get("seq") or 0) for e in past] or [0])
    unsubscribe = events_mod.subscribe(queue, asyncio.get_running_loop())
    try:
        for frame in past:
            await ws.send_json(frame)
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=30)
            except asyncio.TimeoutError:
                # Same keep-alive idea as /ws/progress: a send on an idle
                # socket is what detects a peer that went away.
                await ws.send_json({"type": "ping"})
                continue
            if int(payload.get("seq") or 0) <= high_water:
                continue  # already sent as part of the replay
            await ws.send_json(payload)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        unsubscribe()


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
    # Bind where the config says: `server_host` is also what decides whether
    # the login gate applies (see server/auth.gate_required), so a server
    # started from a shell obeys the same rule as one started by the tray.
    _run_cfg = load_config()
    _host = str(_run_cfg.get("server_host") or "127.0.0.1")
    _port = int(_run_cfg.get("server_port") or 8000)
    _warning = auth_mod.gate_warning(_run_cfg)
    if _warning:
        print(f"[mlo] auth: {_warning}")
    if not auth_mod.is_loopback_host(_host):
        print(f"[mlo] listening on {_host}:{_port} — reachable from the network")
    uvicorn.run("server.main:app", host=_host, port=_port, reload=True)