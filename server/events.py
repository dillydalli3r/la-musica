"""Server-side events: the one channel every client listens on.

Three things happen in the background that a user wants to hear about even
when the relevant page is not open — a wished-for release showed up on
Soulseek, a download finished, an album is ready to import. Each of those
already runs somewhere deep in a worker thread; what was missing is a place to
*announce* it.

`emit()` is that place. It:

* appends to a small in-memory ring AND to a durable log beside the app state,
  so a client that connects a moment later (or reconnects after a dropped
  socket, a restart, or a night with the app closed) still sees what just
  happened,
* pushes the frame to every live `/ws/events` subscriber,
* hands the same frame to Web Push for every device that subscribed (see the
  "Web Push" section below), and
* returns without blocking — a notification must never be the reason a
  download worker stalls.

Delivery to the OS is mostly the client's business (see `web/src/lib/notify.ts`):
the browser uses the Notification API after asking permission, the desktop and
mobile shells use the Tauri notification plugin. That split stays, because a
live page knows what it was granted and this server does not. What a live page
CANNOT do is reach a client that is closed — a phone in a pocket when an
import finishes overnight — so the devices that asked for it get the frame over
Web Push as well, which is the one transport that wakes an app that is not
running.

Frames are JSON:

    {"type": "event", "event": "wish_found", "title": "…", "body": "…",
     "data": {…}, "at": 1712345678.9}

`data.link` is the SUBJECT of the event as a client route — the thing a
notification about it should open (`/album/<path>`, `/track/<path>`,
`/soulseek`, `/import?album=<path>`, `/in-progress`, `/library`, `/settings`).
The client's tray (web/src/lib/notifications.ts) also derives one from the
entity ids an emitter already publishes, so an emit site that knows its
subject should set `link` and every other one still lands somewhere sensible.
A `data.url` names an outside page instead (the release notes of a newer
version).

Kinds in use: wish_found / wish_failed / wish_not_found (the wish worker — the
last one is a wish whose searches found NOTHING and which therefore stops being
searched; see server/wishes' retry policy), download_done and import_ready (a
settled Soulseek job: in the library, or in the download folder waiting to be
imported), download_failed (a job that gave up — an absent/refused slskd, a
MusicBrainz outage, a verification that failed, or a search that found nothing),
download_done for a finished import run (server/import_queue.py),
import_needs_data (an album an import could not supply a family for — the
import FINISHED, the album is in the library and the gap is a warning on its own
finished row, the bell and the album page; only a review stop's entry really is
waiting on a person, see spec R166), script_done / script_failed and grade_done
(a run of the library scripts, from `/api/run`), update_available (a newer
release exists).
download_started and upload_started are the two "it began" halves of a Soulseek
transfer, announced the moment there is something to watch instead of only at
the end: a download whose first bytes actually moved (the wait in
server/soulseek_auto.py), and a peer starting to take files FROM us (the
uploads watcher in server/main.py). Both are one frame per job/user, never one
per poll.

The OUTCOME kinds — wish_failed, wish_not_found, download_failed and
import_needs_data — are deliberately not switchable off in config: they are the
only word the user gets that something they asked for did not happen, and the
`notify_*` switches cover the "this is nice to know" ones (`notify_wish_found`,
`notify_download_done`, `notify_import_ready` and the two Soulseek start
kinds).

Clients filter by `event`; unknown kinds must be ignored, not fatal, so a
newer client can talk to an older server.
"""

import base64
import json
import os
import queue
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

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

# ── Durable replay ──────────────────────────────────────────────────────────
#
# The ring above is MEMORY: it dies with the process, and a client away for
# more than `_MAX_EVENTS` frames hears nothing about what it missed. Web Push
# covers a closed browser, but a desktop or mobile shell cannot be woken by it
# at all (a Tauri webview has no service worker — see web/src/lib/notify.ts),
# so for those clients the replay on reconnect is the ONLY way a notification
# survives the app being closed. The frames are therefore kept on disk as well,
# beside the push keys, and `recent()` answers from both.
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
    announce a found wish, which is the whole point of the feature.
    """
    key = {
        "wish_found": "notify_wish_found",
        "download_done": "notify_download_done",
        "import_ready": "notify_import_ready",
        "import_started": "notify_import_start",
        "import_done": "notify_import_done",
        "download_started": "notify_soulseek_download_start",
        "upload_started": "notify_soulseek_upload_start",
    }.get(kind)
    if not key:
        return True
    if not isinstance(cfg, dict):
        return True
    return bool(cfg.get(key, True))


# ── Web Push ────────────────────────────────────────────────────────────────
#
# The second transport for the same frames: RFC 8030 (delivery) + RFC 8291
# (aes128gcm encryption) + RFC 8292 (VAPID), so a device that is CLOSED still
# hears that an import finished. It lives next to `emit` because it is one more
# thing publishing a frame has to do — and because the one rule it must obey is
# this module's own: publishing must never fail, never block and never raise
# into the worker that reported an outcome (a dead push service must not fail
# an import).
#
# The key material is generated once and kept in `webpush.json` BESIDE auth.db,
# not in the config: `GET /api/config` hands the whole config to every client
# holding a session (see server/main.py), and the VAPID private key is the
# credential that lets anyone make this install's own subscribers pop up a
# notification. The subscription rows live with the sessions in auth.db
# (server/auth.py, table `push_subscriptions`) because "which devices belong to
# which user" is what that file already answers.
#
# Everything degrades quietly: an install without `cryptography` (an update
# that has not re-run pip yet) reports "push unavailable" on the routes and
# skips the fan-out, rather than breaking the event channel or the API.

# RFC 8030 §5.2: how long a push service may hold the message for a device that
# is offline. A day covers a phone that was off overnight.
_PUSH_TTL_S = 86400
# A push service that has not answered in this long is not going to; the worker
# must not sit on a stalled socket while further events pile up behind it.
_PUSH_TIMEOUT_S = 10
# Frames waiting to be delivered. A full queue DROPS the frame: an event is
# news, not a ledger, and blocking the caller is the one thing forbidden here.
_PUSH_QUEUE_MAX = 256
# The sender thread exits after this long with nothing to send, and the next
# event starts it again — a server with no subscribed device keeps no thread.
_PUSH_IDLE_S = 60.0
# RFC 8291 §4: a single record, and a push service need not accept more than
# 4096 octets (RFC 8030 §7.2). Our payloads are a few hundred bytes.
_PUSH_RECORD_SIZE = 4096
# What a frame may occupy, with room for the record's own overhead. A body that
# grows without bound (a failure carrying a long error list) would otherwise
# produce a record bigger than the `rs` it declares in its own header.
_PUSH_MAX_BYTES = 3500
# VAPID's `sub` claim: how a push service reaches the sender about a problem. A
# self-hosted install has no address to offer, so it names itself here.
_PUSH_SUB = "mailto:la-musica@localhost"

_keys_lock = threading.Lock()
_keys = None  # the VAPID pair, or {} when this install cannot push at all
_signing_key_cache = None  # the same pair as a signing key, built once
_push_lock = threading.Lock()
_push_thread = None
_push_queue = queue.Queue(maxsize=_PUSH_QUEUE_MAX)


def _push_keys_path() -> str:
    from mlo.paths import app_data_dir
    return os.path.join(app_data_dir(), "webpush.json")


def _b64u(raw: bytes) -> str:
    """base64url without padding — the encoding every Web Push field uses."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64u(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def key_size_ok(value: str, size: int) -> bool:
    """Whether `value` is a base64url key of exactly `size` bytes.

    Used by the subscribe route: a subscription whose keys cannot be used must
    be refused while the user is looking at the button, not discovered by the
    fan-out after the next import finished.
    """
    try:
        return len(_unb64u(str(value or ""))) == size
    except Exception:
        return False


def _create_keys() -> dict:
    """A fresh VAPID pair, as `{private, public, sub}` in base64url."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_numbers().private_value.to_bytes(32, "big")
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"private": _b64u(private), "public": _b64u(public), "sub": _PUSH_SUB}


def _load_or_create_keys() -> dict:
    path = _push_keys_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("private") and data.get("public"):
            return {"private": str(data["private"]), "public": str(data["public"]),
                    "sub": str(data.get("sub") or _PUSH_SUB)}
    except FileNotFoundError:
        pass
    except Exception:
        # Unreadable, or not the shape we wrote: replace it. The file is written
        # atomically below, so a half-written one cannot be read at all, and a
        # pair nobody can parse would otherwise disable push until a restart.
        pass
    try:
        keys = _create_keys()
    except Exception:
        return {}  # no cryptography: push stays off, the app carries on
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(keys, fh)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass  # Windows: the file is per-user, under the library's own .mlo
    except Exception:
        # A pair we cannot persist is still usable for this process — it just
        # will not survive a restart, which beats not being able to push.
        pass
    return keys


def push_keys() -> dict:
    """The install's VAPID pair, generated once. `{}` when it cannot push."""
    global _keys
    if _keys is not None:
        return _keys
    with _keys_lock:
        if _keys is None:
            _keys = _load_or_create_keys()
    return _keys


def push_public_key() -> str:
    """The base64url public half a client subscribes with ("" when unavailable)."""
    return str(push_keys().get("public") or "")


def _signing_key(keys: dict):
    global _signing_key_cache
    if _signing_key_cache is None:
        from cryptography.hazmat.primitives.asymmetric import ec
        _signing_key_cache = ec.derive_private_key(
            int.from_bytes(_unb64u(keys["private"]), "big"), ec.SECP256R1())
    return _signing_key_cache


def _encrypt(payload: bytes, ua_public: bytes, auth_secret: bytes, as_key, salt: bytes) -> bytes:
    """One aes128gcm record: RFC 8291 §3.4's key schedule over RFC 8188's framing.

    `as_key` (the application server's ephemeral P-256 key) and `salt` are
    arguments rather than generated here for one reason: RFC 8291's Appendix A
    is a known-answer vector, and the test replays it through this function.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    as_public = as_key.public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint)
    shared = as_key.exchange(ec.ECDH(), ua_key)
    # "WebPush: info" || 0x00 || ua_public || as_public: the trailing NUL is the
    # RFC's, and both public keys are part of the info string — that is what
    # binds the derived key to THIS pair of endpoints.
    ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth_secret,
               info=b"WebPush: info\x00" + ua_public + as_public).derive(shared)
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt,
               info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt,
                 info=b"Content-Encoding: nonce\x00").derive(ikm)
    header = salt + struct.pack(">I", _PUSH_RECORD_SIZE) + bytes([len(as_public)]) + as_public
    # 0x02 ends the plaintext as its padding delimiter — a UA rejects the record
    # without it (RFC 8291 §4).
    return header + AESGCM(cek).encrypt(nonce, payload + b"\x02", None)


def _vapid_header(endpoint: str, keys: dict) -> str:
    """`vapid t=<jwt>, k=<public key>` for one push service (RFC 8292 §3)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils

    parts = urllib.parse.urlsplit(endpoint)
    sign_input = ".".join([
        _b64u(json.dumps({"typ": "JWT", "alg": "ES256"},
                         separators=(",", ":")).encode("utf-8")),
        # `aud` is the push SERVICE's origin — that is who checks the token. A
        # twelve-hour window means a device that reconnects after an outage is
        # not refused for a clock that drifted.
        _b64u(json.dumps({"aud": f"{parts.scheme}://{parts.netloc}",
                          "exp": int(time.time()) + 12 * 3600, "sub": keys["sub"]},
                         separators=(",", ":")).encode("utf-8")),
    ]).encode("ascii")
    der = _signing_key(keys).sign(sign_input, ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(der)
    # JWS ES256 wants the raw r||s pair, not the DER signature OpenSSL returns.
    signature = _b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={sign_input.decode('ascii')}.{signature}, k={keys['public']}"


def _push_post(url: str, body: bytes, headers: dict, timeout: float = _PUSH_TIMEOUT_S):
    """The ONE network seam: POST `body` to a push service, answer
    `(status, bytes)`. Never raises — a push service that is down, refused or
    unreachable answers 0, which the caller counts as a failure — and the test
    replaces this function instead of talking to a real service."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(getattr(response, "status", 0) or 0), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code or 0), b""
    except Exception:
        return 0, b""


def _frame_for_push(payload: dict) -> bytes:
    """The bytes a device is woken with: the frame shape the socket carries,
    minus the ring's own bookkeeping, so web/public/sw.js reads one shape from
    both transports."""
    frame = {
        "type": "event",
        "event": str(payload.get("event") or ""),
        "title": str(payload.get("title") or ""),
        "body": str(payload.get("body") or ""),
        "data": payload.get("data") or {},
    }
    data = json.dumps(frame, separators=(",", ":")).encode("utf-8")
    if len(data) <= _PUSH_MAX_BYTES:
        return data
    # The body is what gives: the title and the click target are what the user
    # acts on, and a body this long is an error list nobody reads on a lock
    # screen anyway.
    over = len(data) - _PUSH_MAX_BYTES
    frame["body"] = frame["body"][: max(0, len(frame["body"]) - over)].rstrip() + "…"
    return json.dumps(frame, separators=(",", ":")).encode("utf-8")


def _send_one(row: dict, body: bytes) -> str:
    """Deliver one encrypted frame to one subscription.

    Answers `"ok"`, `"gone"` (the push service says this endpoint is dead —
    RFC 8030 §7.3/§8.4 make 404 and 410 mean "delete it") or `"error"`.
    """
    keys = push_keys()
    if not keys:
        return "error"
    # A row whose keys are not a P-256 point plus a 16-byte secret can never be
    # delivered to, whatever we do — and a row kept for "error" is retried on
    # every event for the rest of the install's life. `gone` prunes it.
    if not key_size_ok(row.get("p256dh"), 65) or not key_size_ok(row.get("auth"), 16):
        return "gone"
    try:
        from cryptography.hazmat.primitives.asymmetric import ec

        endpoint = str(row.get("endpoint") or "")
        ua_public = _unb64u(str(row.get("p256dh") or ""))
        auth_secret = _unb64u(str(row.get("auth") or ""))
        as_key = ec.generate_private_key(ec.SECP256R1())
        encrypted = _encrypt(body, ua_public, auth_secret, as_key, os.urandom(16))
        headers = {
            "TTL": str(_PUSH_TTL_S),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(encrypted)),
            "Authorization": _vapid_header(endpoint, keys),
            "Urgency": "normal",
        }
    except Exception:
        return "error"
    status, _ = _push_post(endpoint, encrypted, headers)
    if status in (404, 410):
        return "gone"
    return "ok" if 200 <= status < 300 else "error"


def fanout(payload: dict, username: str = None, respect_kinds: bool = True) -> dict:
    """Send one frame to every subscription that asked for its kind.

    `username` narrows it to one person's devices (the test button sends to the
    caller's own). Answers `{sent, gone, failed, subscriptions}`; a subscription
    the push service calls dead is deleted here, which is the only pruning a
    self-hosted install needs — a device that comes back re-subscribes itself.
    """
    from server import auth as auth_mod

    outcome = {"sent": 0, "gone": 0, "failed": 0, "subscriptions": 0}
    try:
        rows = auth_mod.push_subscriptions(
            username=username,
            kind=payload.get("event") if respect_kinds else None)
    except Exception:
        return outcome
    outcome["subscriptions"] = len(rows)
    if not rows:
        return outcome
    body = _frame_for_push(payload)
    for row in rows:
        try:
            result = _send_one(row, body)
            if result == "gone":
                outcome["gone"] += 1
                auth_mod.push_unsubscribe(row.get("endpoint"))
            elif result == "ok":
                outcome["sent"] += 1
            else:
                outcome["failed"] += 1
        except Exception:
            # One bad row must not cost the other devices their frame.
            outcome["failed"] += 1
    return outcome


def _push_worker():
    """Deliver queued frames until the queue has been quiet for a while."""
    global _push_thread
    while True:
        try:
            payload = _push_queue.get(timeout=_PUSH_IDLE_S)
        except queue.Empty:
            with _push_lock:
                _push_thread = None
            return
        try:
            fanout(payload)
        except Exception:
            pass  # fanout swallows its own failures; this is the belt
        finally:
            # Unfinished tasks would make every later join() wait forever —
            # which is exactly what a torn-down worker must never do.
            _push_queue.task_done()


def notify_devices(payload: dict) -> None:
    """Queue `payload` for Web Push. Never raises, never blocks, never waits.

    Called by `emit` for every published frame, so it must be cheap: no
    database read, no socket — the worker thread does all of that. When nothing
    can be pushed (no `cryptography`, no key material) this returns without
    starting anything.
    """
    global _push_thread
    try:
        if not push_public_key():
            return
        with _push_lock:
            if _push_thread is None or not _push_thread.is_alive():
                _push_thread = threading.Thread(
                    target=_push_worker, name="webpush", daemon=True)
                _push_thread.start()
        _push_queue.put_nowait(dict(payload))
    except queue.Full:
        pass  # a backlog of news is still news: drop the frame, keep the app up
    except Exception:
        # Whatever went wrong here, the frame is already in the ring and on the
        # socket — the caller's event must not fail because push did.
        pass


def send_test(username: str = "") -> dict:
    """Send a test frame to one person's devices, synchronously, so the button
    that proves a phone can be reached can tell the truth about what happened.

    It ignores what each device ASKED for: the user pressed this button on one
    of them, and a test that is filtered out by that same device's own kind list
    would prove the opposite of what it promises.
    """
    if not push_keys():
        return {"sent": 0, "gone": 0, "failed": 0, "subscriptions": 0, "available": False}
    result = fanout({
        "type": "event",
        "event": "test",
        "title": "la musica",
        "body": "Push is working — this is a test from your server.",
        "data": {"link": "/settings"},
    }, username=username, respect_kinds=False)
    result["available"] = True
    return result


def emit(kind: str, title: str, body: str = "", data: dict = None, config: dict = None) -> dict:
    """Publish one event. Never raises, never blocks on a subscriber.

    `config` is loaded when the caller has it already (most workers do); the
    switches in it are the user's "do not tell me about this" control — and
    they gate Web Push too, because they decide what is published at all.
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
    # On disk as well as in memory, so a client that was closed (and a desktop
    # or mobile shell that no push service can reach) still finds the frame
    # waiting when its `?since=` asks. Outside the lock: this is file I/O, and
    # the ring above is already the answer for everyone watching right now.
    _log_append(payload)
    for sub_queue, loop in subs:
        try:
            loop.call_soon_threadsafe(_put_nowait, sub_queue, payload)
        except Exception:
            # A subscriber whose loop has gone away is dropped below rather
            # than retried forever.
            with _lock:
                _subscribers.discard((sub_queue, loop))
    # After the socket, always: a device that is AWAY is woken by push, but the
    # clients watching right now must never wait behind a push service. This
    # only queues the frame (see notify_devices) — the send happens on its own
    # thread, and no failure in it can reach the caller.
    notify_devices(payload)
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
