"""A configurable size cap on the app's transient stores.

The trash bin is the app's own scratch space and it grew without one: it keeps
every album ever removed, and on a real install that was tens of gigabytes of
nothing.

`trash_cap_gb` is the cap, 5 GB by default, 0 = the cap off. A store over its
cap is pruned oldest first until it fits, and what a prune may never take is
what is in use:

  * a path ``server.job_locks`` holds — the registry every import, script run,
    remove and export claims against, so the answer is the same sentence a
    route would be refused with, naming the job that has it;
  * anything the filesystem itself refuses to give up: a file another process
    still has open fails the delete, and that entry is kept and REPORTED
    instead of worked around.

The unit of deletion is the one the app's own trash route uses — a child of a
per-user trash bin (`/api/trash/delete`, the Trash page) — because a deeper
granularity would be a second bookkeeping scheme for the same files.
An entry that is itself in use is skipped whole, never partly deleted.

Trash entries are removed exactly like ``/api/trash/delete`` removes them: the
entry goes first, then its record is dropped from the bin's origin manifest
(`.mlo_manifest.json`, the file the Trash page restores from), so what restore
needs for the entries a prune KEPT is untouched.

The pass runs from this module's own daemon thread, started with the app's other
workers (server/main.py lifespan) and ticking every TICK_SECONDS, so an install
nobody is looking at still holds its cap.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import traceback

from mlo.config import load_config
from mlo.paths import TRASH_MANIFEST_NAME, trash_root
from server import events, job_locks

# The cap, keyed by the store it owns. It is GB in config (a float is fine —
# the Settings row is one decimal).
CAP_KEYS = {"trash": "trash_cap_gb"}

# What the store is called in a log line and a notification, and where a
# client should open when one is clicked (a route web/src/App.tsx mounts).
STORE_LABELS = {"trash": "the trash bin"}
STORE_LINKS = {"trash": "/trash"}

# How often a pass runs. The cap is about disk that fills up over hours, so a
# few minutes is ample; the first pass waits out a settle delay, because a boot
# is exactly when an interrupted import is being recovered and nothing should
# be deleted under.
TICK_SECONDS = 300
SETTLE_SECONDS = 60

_stop = threading.Event()
_worker = None
_lock = threading.Lock()
# The last pass, for a status read and for the tests that drive run_pass.
_last: dict = {}


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def cap_bytes(cfg, store: str) -> int:
    """The configured cap for *store* in bytes; 0 = that store's cap is off.

    A missing, unreadable or non-positive value is off, never "delete
    everything": a config that never mentioned the key (an install whose
    config.json predates it) must not start deleting on upgrade, and a
    hand-edited negative is treated as the 0 the Settings row documents.
    """
    try:
        gb = float((cfg or {}).get(CAP_KEYS[store]) or 0)
    except (TypeError, ValueError):
        return 0
    return int(gb * (1024 ** 3)) if gb > 0 else 0


def size_text(n) -> str:
    """Bytes as the one short form the log line and the notification share
    ("1.2 GB", "840 kB") — every message below names what was freed, so the
    same number is never spelled two ways."""
    n = int(n or 0)
    for unit, step in (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20), ("kB", 1 << 10)):
        if n >= step:
            return f"{n / step:.1f} {unit}"
    return f"{n} B"


# --------------------------------------------------------------------------- #
# The stores on disk
# --------------------------------------------------------------------------- #
def _is_link(path) -> bool:
    """True for symlinks AND the Windows junctions a non-admin account uses
    instead — the test server.main makes, because a junction is not a symlink
    to ``os.path.islink`` yet its realpath resolves just as far away."""
    try:
        return os.path.islink(path) or bool(getattr(os.lstat(path), "st_reparse_tag", 0))
    except OSError:
        return False


def _entry_bytes(path) -> int:
    """Every byte under one entry, links never followed.

    The same walk server.main's ``_dir_stats`` makes, so the size a prune
    reports for an entry is the size the Downloads/Trash pages showed for it —
    one accounting, not two. An unreadable part counts as 0 instead of raising:
    a listing of disk use must never be the reason a pass dies.
    """
    if _is_link(path) or not os.path.isdir(path):
        try:
            return int(os.path.getsize(path))
        except OSError:
            return 0
    total = 0
    for base, dirs, files in os.walk(path, onerror=lambda e: None):
        dirs[:] = [d for d in dirs if not _is_link(os.path.join(base, d))]
        for f in files:
            try:
                total += int(os.path.getsize(os.path.join(base, f)))
            except OSError:
                pass
    return total


def _entry(store: str, root: str, name: str, bin_dir: str = "") -> dict:
    """One deletable unit: its path, its bytes and its own mtime — the age a
    row is sorted by on both pages (a folder's timestamp IS its last write)."""
    path = os.path.join(bin_dir or root, name)
    try:
        mtime = float(os.path.getmtime(path))
    except OSError:
        mtime = 0.0
    return {"store": store, "root": root, "bin": bin_dir or "", "name": name,
            "path": path, "bytes": _entry_bytes(path), "mtime": mtime}


def trash_entries(cfg) -> list:
    """Every candidate in the trash root, across the per-user bins.

    ``<trash>/<user>/<entry>`` is the shape ``mlo.paths.trash_dir`` writes and
    the Trash page reads, so a direct child DIRECTORY of the trash root is a
    bin and its children are the candidates; anything else sitting there (a
    file, a link, a pre-users ``trash/<album>`` an older version left) is this
    store's bytes that the page cannot even show, and is one candidate of its
    own. Either way the manifest is never a candidate — it is the bin's own
    bookkeeping, not an entry.
    """
    root = trash_root((cfg or {}).get("music_folder") or None)
    out = []
    if not root or not os.path.isdir(root):
        return out
    try:
        names = os.listdir(root)
    except OSError:
        return out
    for name in names:
        if name == TRASH_MANIFEST_NAME:
            continue
        path = os.path.join(root, name)
        if os.path.isdir(path) and not _is_link(path):
            try:
                children = os.listdir(path)
            except OSError:
                continue
            for child in children:
                if child == TRASH_MANIFEST_NAME:
                    continue
                out.append(_entry("trash", root, child, bin_dir=path))
        else:
            out.append(_entry("trash", root, name, bin_dir=root))
    return out


# --------------------------------------------------------------------------- #
# What is in use
# --------------------------------------------------------------------------- #
def _in_use(entry) -> str:
    """Why *entry* may not be deleted right now, or "" when nothing known is.

    One question, asked of state the app already trusts: is a job holding the
    path (``server.job_locks`` — the same registry a route is refused against,
    so the answer carries the job's own sentence).

    A file another process holds open is deliberately not guessed at here: the
    delete fails on it, and the caller keeps and reports the entry.
    """
    path = entry["path"]
    holder = job_locks.holder(path)
    if holder:
        return job_locks.refusal(path, holder)
    return ""


# --------------------------------------------------------------------------- #
# Deleting
# --------------------------------------------------------------------------- #
def _remove_entry(path) -> tuple:
    """Delete one entry, returning ``(bytes freed, error)``.

    Size first: after ``rmtree`` the bytes are gone. A link (or a junction) is
    unlinked, never followed — the entry may point outside the store entirely.
    Any failure leaves the entry in place and is reported, never retried into a
    different shape of deletion.
    """
    size = _entry_bytes(path)
    try:
        if _is_link(path):
            try:
                os.remove(path)
            except OSError:
                os.rmdir(path)
            return 0, ""
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
    except OSError as e:
        return 0, str(e) or "delete failed"
    return size, ""


def _forget_trash_records(bin_dir, names) -> None:
    """Drop the origin records of trashed entries that were just deleted.

    The same edit ``server.api_trash._manifest_forget`` makes after
    ``/api/trash/delete``, on the same file and the same schema
    (``{"version": 1, "entries": {name: {origin, at}}}``), because the Trash
    page restores an entry to the path recorded there. A record left behind is
    not dead weight: the next entry to take that name would inherit the stale
    origin and "restore" somewhere it never came from. With nothing left to
    remember the file goes away, exactly as the route leaves it.
    """
    path = os.path.join(bin_dir, TRASH_MANIFEST_NAME)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return  # a bin with no manifest has no records to drop
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return
    for name in names:
        entries.pop(name, None)
    try:
        if entries:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "entries": entries}, f)
            os.replace(tmp, path)
        else:
            os.remove(path)
    except OSError:
        pass  # the bytes are gone either way; the record is the lesser loss


def _prune(store: str, entries: list, cap: int) -> dict:
    """Delete oldest-first from *entries* until the store is under *cap*.

    The cap is on the store's size ON DISK, so the sum is over the live folders
    (a leftover from a previous version counts like anything else) and an entry
    that cannot be deleted stays in it. Ties on age break by path, so two
    entries written in the same second come out in an order a test can name.
    """
    before = sum(int(e["bytes"]) for e in entries)
    out = {"store": store, "cap_bytes": cap, "before_bytes": before,
           "after_bytes": before, "freed_bytes": 0, "removed": [],
           "kept_in_use": [], "failed": []}
    if cap <= 0 or before <= cap:
        return out
    total = before
    forgotten: dict = {}
    for e in sorted(entries, key=lambda x: (x["mtime"], x["path"])):
        if total <= cap:
            break
        reason = _in_use(e)
        if reason:
            out["kept_in_use"].append(
                {"name": e["name"], "root": e["root"], "bytes": int(e["bytes"]),
                 "age_s": int(max(0, time.time() - e["mtime"])), "reason": reason})
            continue
        freed, err = _remove_entry(e["path"])
        if err:
            out["failed"].append({"name": e["name"], "root": e["root"], "reason": err})
            continue
        total -= freed
        out["freed_bytes"] += freed
        out["removed"].append({"name": e["name"], "root": e["root"], "bytes": freed,
                               "age_s": int(max(0, time.time() - e["mtime"]))})
        if e["bin"]:
            forgotten.setdefault(e["bin"], []).append(e["name"])
    for bin_dir, names in forgotten.items():
        _forget_trash_records(bin_dir, names)
    out["after_bytes"] = total
    return out


def prune_trash(cfg=None) -> dict:
    """Bring the trash bin under its cap, oldest first."""
    cfg = load_config() if cfg is None else cfg
    return _prune("trash", trash_entries(cfg), cap_bytes(cfg, "trash"))


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _announce(res: dict) -> None:
    """One log line and one notification per store that changed.

    A prune is a normal, expected action — the app doing what the setting says
    — so the line states the outcome and is not dressed as a warning. Only
    something a prune could NOT delete is reported as a problem, on its own
    line, with the reason the OS gave.
    """
    if not res["removed"] and not res["failed"]:
        return
    label = STORE_LABELS[res["store"]]
    freed = size_text(res["freed_bytes"])
    if res["removed"]:
        line = (f"storage cap: {label} was over its {size_text(res['cap_bytes'])} cap "
                f"— deleted {len(res['removed'])} oldest "
                f"{'entry' if len(res['removed']) == 1 else 'entries'} ({freed}); "
                f"{size_text(res['before_bytes'])} → {size_text(res['after_bytes'])}")
    else:
        line = (f"storage cap: {label} is over its {size_text(res['cap_bytes'])} cap "
                f"and nothing could be deleted from it")
    print(f"[mlo] {line}")
    body = line
    if res["failed"]:
        names = ", ".join(f"{f['name']} ({f['reason']})" for f in res["failed"][:5])
        print(f"[mlo] storage cap: {len(res['failed'])} entr"
              f"{'y' if len(res['failed']) == 1 else 'ies'} in {label} could not be "
              f"deleted: {names}")
        body = (f"{line}; {len(res['failed'])} could not be deleted: {names}")
    try:
        events.emit("storage_pruned",
                    f"Freed {freed}" if res["removed"] else f"{label} could not be pruned",
                    body,
                    data={"link": STORE_LINKS[res["store"]], "store": res["store"],
                          "freed_bytes": res["freed_bytes"],
                          "before_bytes": res["before_bytes"],
                          "after_bytes": res["after_bytes"],
                          "cap_bytes": res["cap_bytes"],
                          "removed": [r["name"] for r in res["removed"]],
                          "kept_in_use": [r["name"] for r in res["kept_in_use"]],
                          "failed": res["failed"]})
    except Exception:
        traceback.print_exc()


def run_pass(cfg=None) -> dict:
    """One pass over the store: prune what is over its cap, announce it.

    A store whose walk or delete raised is reported in place of its result.
    """
    cfg = load_config() if cfg is None else cfg
    out: dict = {}
    for store, fn in (("trash", prune_trash),):
        try:
            res = fn(cfg)
        except Exception as e:
            traceback.print_exc()
            res = {"store": store, "error": str(e) or e.__class__.__name__,
                   "removed": [], "kept_in_use": [], "failed": [],
                   "freed_bytes": 0, "before_bytes": 0, "after_bytes": 0, "cap_bytes": 0}
            out[store] = res
            continue
        out[store] = res
        _announce(res)
    out["at"] = time.time()
    global _last
    _last = out
    return out


# --------------------------------------------------------------------------- #
# The worker
# --------------------------------------------------------------------------- #
def _loop() -> None:
    """Tick forever: a settle delay, then a pass every TICK_SECONDS."""
    if _stop.wait(SETTLE_SECONDS):
        return
    while not _stop.is_set():
        try:
            run_pass()
        except Exception:
            traceback.print_exc()  # one bad pass must not end the worker
        if _stop.wait(TICK_SECONDS):
            return


def start() -> bool:
    """Start the periodic pass; True when this call started it.

    The same shape as the app's other workers and started from the app's
    lifespan, so a server nobody has opened a page on still holds the cap.
    """
    global _worker
    with _lock:
        if _worker and _worker.is_alive():
            return False
        _stop.clear()
        _worker = threading.Thread(target=_loop, name="mlo-cache-caps", daemon=True)
        _worker.start()
    return True


def stop() -> None:
    """Ask the pass to stop at its next idle point."""
    _stop.set()


def status() -> dict:
    """What the last pass did, for a status read (and for the tests)."""
    return {"running": bool(_worker and _worker.is_alive()),
            "tick_seconds": TICK_SECONDS, "last": _last}
