"""The app's two live sockets and the progress relay behind them.

``/ws/progress`` carries the script-progress frames (a producer pushes through
``_relay``), ``/ws/events`` carries every frame ``server.events.emit()``
publishes. Both are gated by the session token in the query string — a browser
WebSocket cannot set an Authorization header, and the shells' origin is not the
API's — and both answer a stale token with a 4401 close rather than a
pre-accept 403.

This module also owns the frame plumbing: the client set, the end-of-job hook,
and the ``_broadcast`` every producer pushes through (the script runners, and
the watchers in ``server.main``). The event loop that worker threads
relay onto is installed by ``server.main``'s lifespan via ``set_main_loop``.
"""
import asyncio
import threading

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from mlo import stats as stats_mod
from server import auth as auth_mod
from server import events as events_mod
from server import job_locks

# Captured at startup — worker threads use run_coroutine_threadsafe against
# this loop to relay script progress over the WebSocket (get_event_loop()
# from a worker thread is unreliable and deprecated).
_MAIN_LOOP = None


def set_main_loop(loop):
    """Install the loop worker threads relay through (server.main's lifespan)."""
    global _MAIN_LOOP
    _MAIN_LOOP = loop


router = APIRouter()


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


@router.websocket("/ws/progress")
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


@router.websocket("/ws/events")
async def ws_events(ws: WebSocket):
    """The notification channel: every frame `server.events.emit()` publishes.

    Separate from /ws/progress because the two have different consumers and
    different lifetimes — the progress socket is the page that is running a
    script, this one is every client that wants to know about imports and
    downloads, which may be a phone in another room. It is also the one route
    where the token travels in the query string: a browser WebSocket cannot
    set an Authorization header, and the desktop/mobile shell's origin is not
    the API's, so neither the header nor a same-site cookie is available.

    `?since=<unix seconds>` replays what the ring still holds, so a client
    that was asleep or reconnecting does not miss the event that landed while
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
