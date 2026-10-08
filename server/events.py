"""Server-side events: the one channel every client listens on.

Three things happen in the background that a user wants to hear about even
when the relevant page is not open — a download finished, an album is ready to
import, a run ended. Each of those already runs somewhere deep in a worker
thread; what was missing is a place to *announce* it.

`emit()` is that place. It:

* appends to a small in-memory ring AND to a durable log beside the app state,
  so a client that connects a moment later (or reconnects after a dropped
  socket, a restart, or a night with the app closed) still sees what just
  happened,
* pushes the frame to every live `/ws/events` subscriber, and
* returns without blocking — a notification must never be the reason a
  download worker stalls.

Delivery to the OS is mostly the client's business (see `web/src/lib/notify.ts`):
the browser uses the Notification API after asking permission, the desktop and
mobile shells use the Tauri notification plugin. That split stays, because a
live page knows what it was granted and this server does not.

Frames are JSON:

    {"type": "event", "event": "import_done", "title": "…", "body": "…",
     "data": {…}, "at": 1712345678.9}

`data.link` is the SUBJECT of the event as a client route — the thing a
notification about it should open (`/album/<path>`, `/track/<path>`,
`/import?album=<path>`, `/in-progress`, `/library`, `/settings`).
The client's tray (web/src/lib/notifications.ts) also derives one from the
entity ids an emitter already publishes, so an emit site that knows its
subject should set `link` and every other one still lands somewhere sensible.
A `data.url` names an outside page instead (the release notes of a newer
version).

Kinds in use: download_done and import_ready (a settled acquisition: in the
library, or in the download folder waiting to be imported), download_failed (a
job that gave up — a MusicBrainz outage, a verification that failed, or a
search that found nothing), download_done for a finished import run (the import
pipeline), import_needs_data (an album an import could not supply a family for
— the import FINISHED, the album is in the library and the gap is a warning on
its own finished row, the bell and the album page; only a review stop's entry
really is waiting on a person, see spec R166), script_done / script_failed and
grade_done (a run of the library scripts, from `/api/run`), update_available (a
newer release exists).

The OUTCOME kinds — download_failed and import_needs_data — are deliberately
not switchable off in config: they are the only word the user gets that
something they asked for did not happen, and the `notify_*` switches cover the
"this is nice to know" ones (`notify_download_done`, `notify_import_ready` and
the import start/done kinds).

Clients filter by `event`; unknown kinds must be ignored, not fatal, so a
newer client can talk to an older server.
"""

import json
import os
import threading
import time
import traceback

# Ring size. Big enough that a client which reconnects after a hiccup is
# caught up, small enough that a long-idle server does not hoard memory.
_MAX_EVENTS = 100

# Subscribers: a set of (asyncio.Queue, loop) pairs — see subscribe().
_subscribers = set()
_lock = threading.RLock()
_events = []
# Event numbers are MILLISECONDS SINCE THE EPOCH, not a counter from zero: a
# client persists the last number it saw so a reconnect can replay what it
# missed, and the backend restarts on every config save / dependency install.
# A counter that restarted at 1 would make every post-restart event look older
# than the client's stored value, i.e. silently dropped — an import finished
# after a restart would never be announced. A clock-seeded number keeps the protocol's
# "strictly newer" rule true across restarts, and doubles as the timestamp the
# replay window (`?since=`) is expressed in.
_seq = int(time.time() * 1000)

# ── Durable replay ──────────────────────────────────────────────────────────
#
# The ring above is MEMORY: it dies with the process, and a client away for
# more than `_MAX_EVENTS` frames hears nothing about what it missed. The replay
# on reconnect is therefore the only way a notification survives the app being
# closed or the server restarting, so the frames are kept on disk as well, and
# `recent()` answers from both.
_EVENT_LOG = "events.jsonl"
# Frames kept when the file is rewritten, and the size that triggers it. The
# log is a catch-up window, not a ledger: 400 frames is days of a busy install,
# and the rewrite keeps the file (and the read a reconnect pays for) small.
_LOG_KEEP = 400
_LOG_MAX_BYTES = 512 * 1024


def _event_log_path() -> str:
    from mlo.paths import app_data_dir
    return os.path.join(app_data_dir(), _EVENT_LOG)


def _log_append(payload: dict) -> None:
    """Append one frame to the durable log. Never raises (see emit).

    The append and the compaction it may trigger share `_lock`: the compaction
    is a read-modify-write of the whole file, so a frame appended between its
    read and its `os.replace` would be rewritten away — silently, and only
    under concurrent emitters. `emit` calls this after releasing the lock and
    holds nothing itself, so what the lock covers here is file I/O alone."""
    try:
        path = _event_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
            if os.path.getsize(path) > _LOG_MAX_BYTES:
                _log_compact(path)
    except Exception:
        pass


def _log_compact(path: str) -> None:
    """Rewrite the log with just its newest frames, atomically.

    A half-written log read by a client mid-rewrite would lose frames it has
    not seen yet, so the tail is written beside the log and moved over it."""
    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.writelines(lines[-_LOG_KEEP:])
    os.replace(tmp, path)


def _log_frames(since: float) -> list:
    """Frames in the durable log newer than `since`, oldest first."""
    try:
        with open(_event_log_path(), encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            frame = json.loads(line)
        except ValueError:
            # A torn last line (a crash mid-write) is one lost frame, not a
            # reason to answer the client with nothing.
            continue
        if float(frame.get("at") or 0) > float(since or 0):
            out.append(frame)
    return out


def _notify_configured(kind: str, cfg: dict) -> bool:
    """Is this event kind switched on in the config?

    Defaults are True: a server whose config predates these keys must still
    announce a finished import.
    """
    key = {
        "download_done": "notify_download_done",
        "import_ready": "notify_import_ready",
        "import_started": "notify_import_start",
        "import_done": "notify_import_done",
    }.get(kind)
    if not key:
        return True
    if not isinstance(cfg, dict):
        return True
    return bool(cfg.get(key, True))


def emit(kind: str, title: str, body: str = "", data: dict = None, config: dict = None) -> dict:
    """Publish one event. Never raises, never blocks on a subscriber.

    `config` is loaded when the caller has it already (most workers do); the
    switches in it are the user's "do not tell me about this" control.
    """
    payload = {
        "type": "event",
        "event": str(kind or ""),
        "title": str(title or ""),
        "body": str(body or ""),
        "data": data or {},
        "at": time.time(),
    }
    try:
        if config is None:
            from mlo.config import load_config
            config = load_config()
        if not _notify_configured(payload["event"], config):
            return payload
    except Exception:
        # A config that cannot be read must not silence the event; the
        # switches are a preference, not a permission.
        pass
    global _seq
    with _lock:
        _seq += 1
        payload["seq"] = _seq
        _events.append(payload)
        del _events[:-_MAX_EVENTS]
        subs = list(_subscribers)
    # On disk as well as in memory, so a client that was closed (or a desktop
    # or mobile shell) still finds the frame waiting when its `?since=` asks.
    # Outside the lock: this is file I/O, and the ring above is already the
    # answer for everyone watching right now.
    _log_append(payload)
    for sub_queue, loop in subs:
        try:
            loop.call_soon_threadsafe(_put_nowait, sub_queue, payload)
        except Exception:
            # A subscriber whose loop has gone away is dropped below rather
            # than retried forever.
            with _lock:
                _subscribers.discard((sub_queue, loop))
    return payload


def _put_nowait(queue, payload):
    try:
        queue.put_nowait(payload)
    except Exception:
        pass


# ── the library-changed signal ──────────────────────────────────────────────
#
# One outcome that is a SIGNAL rather than a record: "the library changed under
# you, drop what you derived from it". Every mutating request that can touch the
# library publishes it — a tag write, a cover, a rename, an import step, a trash
# move — and so does the background rebuild of the assembled tree (`server
# .tagcache`), because that tree is served STALE-WHILE-REVALIDATE: the refetch a
# write immediately triggers is answered with the PRE-write rows, and this second
# frame is what makes the page ask again once the fresh tree is in hand.
#
# The client treats it as silent (no tray entry, no OS notification — see
# web/src/lib/notify.ts): a tag write must not fill the notification panel. It
# exists only so `onAppEvent` listeners can invalidate.
_LIBRARY_CHANGED = "library_changed"
# A burst — one wizard step, one import, one chain — must cost ONE refetch: the
# emit is coalesced, so the last write of the burst is the only frame.
_LIB_CHANGED_DELAY = 0.35
_lib_changed_lock = threading.Lock()
_lib_changed_timer = None

# Mutating paths that cannot have changed the library: credentials and sessions,
# the config UI's own writes, and installed-tool, export/EQ, cookie and
# library-query state. Everything else a POST/PUT/PATCH/DELETE touches is
# treated as a library write — a loud list would have to be maintained forever,
# and a forgotten route is a page that quietly goes stale.
_NOT_LIBRARY_WRITES = (
    "/api/auth", "/login", "/logout", "/password", "/revoke-all", "/setup",
    "/users", "/api/config", "/api/dependencies", "/api/export",
    "/api/eq", "/api/cookies", "/api/rym/cookies",
    "/api/ai/test", "/api/open-folder", "/api/naming/preview",
    "/api/library/query", "/api/import/scripts/preview",
    "/api/import/prompts/dismiss", "/api/import/sessions",
    "/api/stack",
)


def affects_library(path: str) -> bool:
    """Whether a mutating request on *path* can have changed the library."""
    p = str(path or "")
    return not any(p == pre or p.startswith(pre + "/") for pre in _NOT_LIBRARY_WRITES)


def note_library_write(path: str = "") -> None:
    """Announce that the library changed, coalesced into one frame per burst.

    Called by the HTTP middleware after a successful mutating request and by
    the library rebuild when it lands. Safe from any thread; never raises.
    """
    global _lib_changed_timer
    try:
        with _lib_changed_lock:
            if _lib_changed_timer is not None:
                _lib_changed_timer.cancel()
            timer = threading.Timer(_LIB_CHANGED_DELAY, _fire_library_changed,
                                    args=(str(path or ""),))
            timer.daemon = True
            _lib_changed_timer = timer
            timer.start()
    except Exception:
        traceback.print_exc()


def _fire_library_changed(path):
    global _lib_changed_timer
    with _lib_changed_lock:
        _lib_changed_timer = None
    try:
        emit(_LIBRARY_CHANGED, "", "", {"path": path} if path else {})
    except Exception:
        traceback.print_exc()


def subscribe(queue, loop):
    """Register an asyncio.Queue to receive frames. Returns an unsubscribe
    callable; the WebSocket route must call it in a `finally` block."""
    with _lock:
        _subscribers.add((queue, loop))

    def _unsubscribe():
        with _lock:
            _subscribers.discard((queue, loop))

    return _unsubscribe


def recent(since: float = 0.0, limit: int = _MAX_EVENTS):
    """Events newer than `since` (unix seconds), oldest first.

    Answered from the memory ring AND the durable log: the ring is everything a
    client might have missed within this process's life, the log is what
    survives a restart or an app that was closed for days. Frames are deduped on
    `seq` (both can hold the same one) and the newest `limit` are returned —
    a client always wants the tail, never the whole history."""
    with _lock:
        out = [e for e in _events if float(e.get("at") or 0) > float(since or 0)]
    if len(out) < limit:
        seen = {int(e.get("seq") or 0) for e in out}
        extra = [f for f in _log_frames(since) if int(f.get("seq") or 0) not in seen]
        if extra:
            out = sorted(out + extra,
                         key=lambda e: (float(e.get("at") or 0), int(e.get("seq") or 0)))
    return out[-limit:]


def subscribe_count() -> int:
    with _lock:
        return len(_subscribers)


def _frame(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"))
