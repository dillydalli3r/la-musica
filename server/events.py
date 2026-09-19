"""Server-side events: the one channel every client listens on.

Three things happen in the background that a user wants to hear about even
when the relevant page is not open — a wished-for release showed up on
Soulseek, a download finished, an album is ready to import. Each of those
already runs somewhere deep in a worker thread; what was missing is a place to
*announce* it.

`emit()` is that place. It:

* appends to a small in-memory ring, so a client that connects a moment later
  (or reconnects after a dropped socket) still sees what just happened,
* pushes the frame to every live `/ws/events` subscriber, and
* returns without blocking — a notification must never be the reason a
  download worker stalls.

Delivery to the OS is the client's business (see `web/src/lib/notify.ts`):
the browser uses the Notification API after asking permission, the desktop and
mobile shells use the Tauri notification plugin. That split is deliberate —
a web page can only raise a notification from a live page, and this server
cannot know what any client has been granted.

Frames are JSON:

    {"type": "event", "event": "wish_found", "title": "…", "body": "…",
     "data": {…}, "at": 1712345678.9}

Clients filter by `event`; unknown kinds must be ignored, not fatal, so a
newer client can talk to an older server.
"""

import json
import threading
import time

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
# than the client's stored value, i.e. silently dropped — a wish found after a
# restart would never be announced. A clock-seeded number keeps the protocol's
# "strictly newer" rule true across restarts, and doubles as the timestamp the
# replay window (`?since=`) is expressed in.
_seq = int(time.time() * 1000)


def _notify_configured(kind: str, cfg: dict) -> bool:
    """Is this event kind switched on in the config?

    Defaults are True: a server whose config predates these keys must still
    announce a found wish, which is the whole point of the feature.
    """
    key = {
        "wish_found": "notify_wish_found",
        "download_done": "notify_download_done",
        "import_ready": "notify_import_ready",
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
    for queue, loop in subs:
        try:
            loop.call_soon_threadsafe(_put_nowait, queue, payload)
        except Exception:
            # A subscriber whose loop has gone away is dropped below rather
            # than retried forever.
            with _lock:
                _subscribers.discard((queue, loop))
    return payload


def _put_nowait(queue, payload):
    try:
        queue.put_nowait(payload)
    except Exception:
        pass


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
    """Events newer than `since` (unix seconds), oldest first."""
    with _lock:
        out = [e for e in _events if float(e.get("at") or 0) > float(since or 0)]
    return out[-limit:]


def subscribe_count() -> int:
    with _lock:
        return len(_subscribers)


def _frame(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"))
