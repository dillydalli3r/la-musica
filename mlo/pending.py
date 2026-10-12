"""Tag writes a held file denied, kept so the change lands later instead of
being lost.

The owner's report is the whole reason this module exists: rating a track
while it plays failed the tag write, because the app's OWN player held the file
open in a way that denies ``os.replace`` for the length of the playback (see
``mlo.atomic.open_shared_read`` — the streaming routes now open the file with
``FILE_SHARE_DELETE``, so the rename lands and this is the fallback rather than
the everyday path).

A write can still be refused by something this app does not control — a
scanner, an antivirus pass, a sibling process, a read-only destination. A flat
refusal then THREW THE USER'S EDIT AWAY. This module keeps it instead: the op
the write was making is recorded in a small JSON queue under the app's state
dir, and a drain replays it through ``mlo.audio.AudioFile`` when the file is
free. So the answer to a denied write is "queued, it will land", not "nothing
was written".

The queue is deliberately simple: one file, one lock, rewritten atomically
(``mlo.atomic.write_bytes``), keyed by path so repeated denials of the same
file COALESCE into the newest op for each field. A drain drops an entry whose
file has vanished, keeps one whose file is still held (with an attempt count
and a bounded window), and says what it did. Nothing here is a UI control —
the app drains it on its own, at startup and on an interval.
"""
import json
import os
import threading
import time

from . import atomic

# One JSON file beside the app's other state (mlo.paths.app_data_dir), never in
# the library: the queue is the app's own bookkeeping and must not be mistaken
# for a user's file — nor swept by the library's temp rules.
QUEUE_NAME = "pending_tag_writes.json"
VERSION = 1

# How often the lifespan's worker tries again, and how long a change is held
# before it is given up on. The try loop is short (a held file usually frees in
# milliseconds to minutes); the window is the backstop for a file that will
# NEVER take the write (a read-only destination), so the queue cannot grow for
# ever. MAX_ATTEMPTS is the other bound — a later attempt is only made after
# the next interval, so this is roughly a day at the interval below.
INTERVAL_S = 30.0
MAX_ATTEMPTS = 240
DROP_AFTER_S = 24 * 60 * 60

_lock = threading.RLock()
_local = threading.local()
# A drain applies its entries OUTSIDE `_lock` (the replay writes files), so two
# concurrent drains — the interval worker and an enqueue's immediate attempt —
# could both pick the same entry up. This is the one drain at a time gate.
_drain_gate = threading.Lock()

# The op keys the drain knows how to replay, and the AudioFile call each makes.
_OP_KINDS = ("set_tag", "delete_tag", "set_lyrics", "delete_lyrics",
             "set_video_tags")


def queue_path() -> str:
    """Where the queue lives: the app's own state dir (see mlo.paths)."""
    from .paths import app_data_dir
    return os.path.join(app_data_dir(), QUEUE_NAME)


# --------------------------------------------------------------------------- #
# Reading and writing the queue
# --------------------------------------------------------------------------- #
def _read() -> dict:
    try:
        with open(queue_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"version": VERSION, "entries": {}}
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return {"version": VERSION, "entries": {}}
    data.setdefault("version", VERSION)
    return data


def _write(data: dict) -> None:
    blob = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
    target = queue_path()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    atomic.write_bytes(target, blob)


def _key(path) -> str:
    return os.path.normpath(str(path))


def _op_key(op: dict) -> str:
    """What makes two ops the SAME field: kind + name. A newer op under one key
    replaces the older one, so a file ends up carrying the user's LATEST value
    rather than a pile of every value they passed through."""
    kind = str(op.get("op") or "")
    if kind == "set_lyrics":
        return "set_lyrics"
    if kind == "set_video_tags":
        tags = op.get("tags") or {}
        return "set_video_tags:" + ",".join(sorted(str(k) for k in tags))
    return f"{kind}:{str(op.get('name') or '').upper()}"


def pending_entries() -> list:
    """The owed changes, oldest first — for the UI and for tests."""
    with _lock:
        data = _read()
    out = []
    for path, entry in data["entries"].items():
        out.append({"path": path, "ops": list(entry.get("ops") or []),
                    "attempts": int(entry.get("attempts") or 0),
                    "first": float(entry.get("first") or 0)})
    out.sort(key=lambda e: e["first"])
    return out


def pending_count() -> int:
    with _lock:
        return len(_read()["entries"])


def in_drain() -> bool:
    """Whether the CURRENT thread is a drain applying its own ops — an enqueue
    from inside it would re-record the very change it is trying to write."""
    return bool(getattr(_local, "active", False))


# --------------------------------------------------------------------------- #
# Recording a change
# --------------------------------------------------------------------------- #
def enqueue(path, ops, *, now=None) -> int:
    """Record *ops* (a list of op dicts) as owed for *path*; returns the number
    of ops now owed for it, or 0 when there was nothing to record.

    Coalescing: an op that names the same field as one already owed replaces it
    (the file must end on the newest value), and new fields are appended.
    """
    clean = []
    for op in ops or []:
        if isinstance(op, dict) and str(op.get("op") or "") in _OP_KINDS:
            clean.append(op)
    if not clean:
        return 0
    now = time.time() if now is None else float(now)
    key = _key(path)
    with _lock:
        data = _read()
        entry = data["entries"].get(key) or {
            "ops": [], "attempts": 0, "first": now, "next_try": now}
        merged = list(entry.get("ops") or [])
        for op in clean:
            okey = _op_key(op)
            merged = [m for m in merged if _op_key(m) != okey]
            merged.append(op)
        entry["ops"] = merged
        # A fresh denial of the file RE-ARMS the intent: the newest value must
        # land, so its attempt count and window start over rather than
        # inheriting an older op's exhausted budget (see MAX_ATTEMPTS).
        entry["attempts"] = 0
        entry["first"] = now
        # The enqueue itself is the signal: allow the one fire-and-forget drain
        # that follows it to try immediately.
        entry["next_try"] = now
        data["entries"][key] = entry
        _write(data)
    _attempt_soon()
    return len(merged)


def queue_held_write(path, ops, exc):
    """The error a held-file tag write reports: record *ops* for *path* (unless
    a drain is applying its own copy) and return a PermissionError carrying the
    same diagnosis but the truth about the change.

    Returns *exc* unchanged when nothing could be recorded — the honest answer
    is then still "nothing was written".
    """
    if in_drain():
        return exc
    try:
        if enqueue(path, ops):
            # Rebuild the message from the ORIGINAL denial, not from *exc* —
            # *exc* may already be a composed held-file message ("nothing was
            # written"), and nesting it would print both claims at once.
            cause = getattr(exc, "__cause__", None) or exc
            return atomic.held_replace_error(path, cause, queued=True)
    except Exception:
        pass
    return exc


# --------------------------------------------------------------------------- #
# Draining it
# --------------------------------------------------------------------------- #
def _apply(af, op: dict) -> None:
    """Replay one op through *af*; raise when the file refused it."""
    kind = op.get("op")
    if kind == "set_tag":
        ok = af.set_tag(op.get("name"), op.get("value"))
    elif kind == "delete_tag":
        ok = af.delete_tag(op.get("name"))
    elif kind == "set_lyrics":
        ok = af.set_lyrics(op.get("text"))
    elif kind == "delete_lyrics":
        ok = af.delete_lyrics()
    elif kind == "set_video_tags":
        ok = af.set_video_tags(op.get("tags") or {})
    else:
        return
    if not ok:
        raise OSError(getattr(af, "error", None) or f"{kind} failed")


def _apply_entry(path: str, ops: list) -> None:
    """Apply an entry's ops through one AudioFile, in ONE flush."""
    from .audio import AudioFile

    af = AudioFile(path)
    if af.audio is None:
        raise OSError(af.error or "the file could not be opened")
    af.defer_save(True)
    for op in ops:
        _apply(af, op)
    if af.flush() is False:
        raise OSError(af.error or "the tag write did not land")


def drain(*, log=None, limit: int = 32, now=None) -> dict:
    """Try every due owed change once; returns a summary of what happened.

    * Dropped — the file is gone (an op for a deleted file is stale, not
      failed).
    * Written — the ops replayed and the file now carries them.
    * Held — the file refused again; the entry is kept with its attempt count
      bumped, and is given up on (dropped) at ``MAX_ATTEMPTS`` or after
      ``DROP_AFTER_S``.

    ``log(line)`` gets one summary line per drain (the default prints it) and
    is silent when there was nothing due. A drain runs on its OWN thread's
    marks, so its replays never re-enqueue themselves.
    """
    now = time.time() if now is None else float(now)
    if not _drain_gate.acquire(blocking=False):
        return {"written": 0, "held": 0, "dropped": 0, "busy": True}
    written = dropped = held = 0
    try:
        with _lock:
            data = _read()
            entries = dict(data["entries"])
            due = [(k, e) for k, e in entries.items()
                   if float(e.get("next_try") or 0) <= now]
            due.sort(key=lambda kv: float(kv[1].get("first") or 0))
            due = due[:max(1, int(limit))]
        _local.active = True
        try:
            for key, entry in due:
                ops = list(entry.get("ops") or [])
                attempts = int(entry.get("attempts") or 0)
                first = float(entry.get("first") or now)
                if not ops:
                    _remove(key)
                    dropped += 1
                    continue
                if not os.path.isfile(key):
                    _remove(key)
                    dropped += 1
                    continue
                try:
                    _apply_entry(key, ops)
                except Exception:
                    attempts += 1
                    if attempts >= MAX_ATTEMPTS or (now - first) >= DROP_AFTER_S:
                        _remove(key)
                        dropped += 1
                    else:
                        _bump(key, attempts, now + INTERVAL_S)
                        held += 1
                else:
                    _remove(key)
                    written += 1
        finally:
            _local.active = False
    finally:
        _drain_gate.release()
    summary = {"written": written, "held": held, "dropped": dropped}
    if written or held or dropped:
        line = (f"[mlo] pending tag writes: {written} written, "
                f"{held} still held, {dropped} dropped")
        if log is None:
            print(line)
        else:
            try:
                log(line)
            except Exception:
                pass
    return summary


def _remove(key: str) -> None:
    with _lock:
        data = _read()
        if key in data["entries"]:
            del data["entries"][key]
            _write(data)


def _bump(key: str, attempts: int, next_try: float) -> None:
    with _lock:
        data = _read()
        entry = data["entries"].get(key)
        if entry is None:
            return
        entry["attempts"] = int(attempts)
        entry["next_try"] = float(next_try)
        _write(data)


# --------------------------------------------------------------------------- #
# The worker the server's lifespan runs
# --------------------------------------------------------------------------- #
_worker_lock = threading.Lock()
_worker_thread = None
_stop = threading.Event()
_soon_lock = threading.Lock()
_soon_busy = False


def _attempt_soon() -> None:
    """One fire-and-forget drain right after an enqueue, so a file that was
    freed between the denial and the recording does not wait a whole interval.
    Never blocks the request: a thread with not much to do.

    One at a time — a burst of edits must not spawn a thread per edit (and the
    drain's own gate would make all but the first return at once anyway)."""
    global _soon_busy
    if in_drain():
        return
    with _soon_lock:
        if _soon_busy:
            return
        _soon_busy = True

    def _run():
        global _soon_busy
        try:
            drain()
        except Exception:
            pass
        finally:
            with _soon_lock:
                _soon_busy = False

    threading.Thread(target=_run, daemon=True,
                     name="mlo-pending-tag-writes").start()


def start_worker() -> None:
    """Start the interval drain if it is not already running (idempotent)."""
    global _worker_thread
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _stop.clear()
        _worker_thread = threading.Thread(target=_run, daemon=True,
                                          name="mlo-pending-drain")
        _worker_thread.start()


def _run() -> None:
    # Once at startup, then on the interval — see the lifespan's call.
    while not _stop.is_set():
        try:
            drain()
        except Exception:
            pass
        _stop.wait(INTERVAL_S)


def stop() -> None:
    """Ask the interval worker to stop (a shutdown; the queue is on disk, so
    the next start drains what is left)."""
    _stop.set()