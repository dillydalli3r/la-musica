"""Two configurable size caps on the app's transient stores.

Both stores are the app's own scratch space and both grew without one: a
Soulseek job that is cancelled, a candidate that loses the race or an import
that fails leaves its bytes in the download/staging folders (`soulseek_
clear_downloads` only removes the copy an import actually landed), and the
trash bin keeps every album ever removed. On a real install that was tens of
gigabytes of nothing.

`soulseek_cache_cap_gb` and `trash_cap_gb` are two INDEPENDENT caps, 5 GB each
by default, 0 = that store's cap off. A store over its cap is pruned oldest
first until it fits, and what a prune may never take is what is in use:

  * a path ``server.job_locks`` holds — the registry every import, script run,
    remove and export claims against, so the answer is the same sentence a
    route would be refused with, naming the job that has it;
  * an entry belonging to an slskd transfer that is still running, read from
    slskd's own tree (``soulseek.downloads_state()``) — the same evidence
    ``soulseek.import_completed`` refuses to move an unfinished album on, so a
    folder this app will not import yet is a folder a prune will not delete;
  * anything the filesystem itself refuses to give up: a file slskd still has
    open fails the delete, and that entry is kept and REPORTED instead of
    worked around.

The unit of deletion is the one the app's own staging routes use — a top-level
entry of a staging root (`/api/soulseek/staging/delete`, the Downloads page) and
a child of a per-user trash bin (`/api/trash/delete`, the Trash page) — because
a deeper granularity would be a second bookkeeping scheme for the same files.
An entry that is itself in use is skipped whole, never partly deleted.

Trash entries are removed exactly like ``/api/trash/delete`` removes them: the
entry goes first, then its record is dropped from the bin's origin manifest
(`.mlo_manifest.json`, the file the Trash page restores from), so what restore
needs for the entries a prune KEPT is untouched.

The pass runs from this module's own daemon thread, started with the app's other
workers (server/main.py lifespan) and ticking every TICK_SECONDS, so an install
nobody is looking at still holds its caps.
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

# The two caps, keyed by the store each one owns. Both are GB in config (a
# float is fine — the Settings row is one decimal) and they are never summed:
# one store filling up must not eat the other's room.
CAP_KEYS = {"soulseek": "soulseek_cache_cap_gb", "trash": "trash_cap_gb"}

# What each store is called in a log line and a notification, and where a
# client should open when one is clicked (a route web/src/App.tsx mounts).
STORE_LABELS = {"soulseek": "the Soulseek download folder", "trash": "the trash bin"}
STORE_LINKS = {"soulseek": "/soulseek", "trash": "/trash"}

# How often a pass runs. The caps are about disk that fills up over hours, so a
# few minutes is ample; the first pass waits out a settle delay, because a boot
# is exactly when an interrupted import is being recovered and slskd is coming
# up with its transfers — neither is something to delete under.
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
def download_roots(cfg) -> list:
    """The folders slskd writes into, in the order the card reports them.

    Read out of ``server.soulseek``, which derives them from the config, rather
    than from the music folder: a custom ``soulseek_download_dir`` (and the
    ``incomplete`` sibling derived from it) is where the bytes really are, so
    the cap holds over the folders the client uses and not a second guess at
    their names. The paths module is the fallback for an install where soulseek
    cannot be imported at all.
    """
    try:
        from server import soulseek
        roots = [soulseek.download_dir(cfg), soulseek._incomplete_dir(cfg)]
    except Exception:
        from mlo.paths import downloads_dir, incomplete_dir
        folder = (cfg or {}).get("music_folder") or None
        roots = [downloads_dir(folder), incomplete_dir(folder)]
    out: list = []
    for root in roots:
        if root and root not in out:
            out.append(root)
    return out


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


def soulseek_entries(cfg) -> list:
    """Every candidate in the download/staging pair.

    A top-level child of either root, which is the unit the app already lists
    and deletes (``/api/soulseek/staging``), dot-entries excepted: ``.incomplete``
    is slskd's own staging tree on an install that has not migrated, and the
    importer steps over every dot-dir for the same reason — they are not results
    an entry-level action may take. A dot-dir that holds a running transfer is
    still PROTECTED by the in-use check below; it is just never a candidate.
    """
    out = []
    for root in download_roots(cfg):
        try:
            names = os.listdir(root)
        except OSError:
            continue  # slskd creates both roots on its own schedule
        for name in names:
            if name.startswith("."):
                continue
            out.append(_entry("soulseek", root, name))
    return out


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
def running_transfer_names(cfg) -> tuple:
    """What a transfer that is STILL RUNNING owns, from slskd's own tree:
    ``(folders, files)``, both lower-cased.

    ``folders`` are the peer's username — the first path segment under either
    staging root in the layout slskd writes today (see
    ``soulseek.DESTINATION_SUBDIR``) — and the leaf of the remote folder it is
    writing into (what ``soulseek._pending_album_folders`` matches an album
    against). ``files`` are the transfer's own file names, which only ever
    protect a LOOSE candidate: a directory candidate under a staging root is
    already named by the username or the remote folder slskd writes through,
    while a partial dropped straight in the root has nothing but its name.

    An unreachable slskd answers with two empty sets: guessing "nothing is
    running" is what the entry-level delete cannot prove, so it keeps whatever
    the filesystem refuses to give up, and the transfer's own bytes simply hold
    the cap until the transfer is done.
    """
    try:
        from server import soulseek
        from server.soulseek_auto import _remote_rel
        tree = soulseek.downloads_state(cfg) or []
    except Exception:
        return set(), set()
    folders, files = set(), set()
    for user in tree:
        if not isinstance(user, dict):
            continue
        who = str(user.get("username") or "").strip().lower()
        for d in user.get("directories") or []:
            if not isinstance(d, dict):
                continue
            for f in d.get("files") or []:
                if not isinstance(f, dict):
                    continue
                try:
                    if soulseek.finished_transfer(f.get("state")):
                        continue
                except Exception:
                    continue
                if who:
                    folders.add(who)
                parts = [p for p in _remote_rel(str(f.get("filename") or "")).replace("\\", "/").split("/") if p]
                if not parts:
                    continue
                files.add(parts[-1].strip().lower())
                if len(parts) > 1:
                    folders.add(parts[-2].strip().lower())
    folders.discard("")
    files.discard("")
    return folders, files


# One sentence for both halves of "a transfer is using this": what the user
# needs to know is that the app is not the thing holding the bytes.
TRANSFER_IN_USE = "an slskd transfer is still running in it"


def _in_use(entry, folders, files) -> str:
    """Why *entry* may not be deleted right now, or "" when nothing known is.

    Two questions, both asked of state the app already trusts: is a job holding
    the path (``server.job_locks`` — the same registry a route is refused
    against, so the answer carries the job's own sentence), and is a running
    slskd transfer writing in it. The second is a NAME question, answered the
    way ``soulseek.import_completed`` decides an album is not ready yet: the
    entry's own name, then every directory name inside it, against the peer and
    remote-folder names a running transfer owns. Files are only matched when
    the candidate IS a file — a leftover loose partial in a staging root — so
    two candidates of one release sharing a track name cannot make the app keep
    a folder forever.

    A file another process holds open is deliberately not guessed at here: the
    delete fails on it, and the caller keeps and reports the entry.
    """
    path = entry["path"]
    holder = job_locks.holder(path)
    if holder:
        return job_locks.refusal(path, holder)
    if not folders and not files:
        return ""
    base = os.path.basename(path).strip().lower()
    if base in folders:
        return TRANSFER_IN_USE
    if not os.path.isdir(path) or _is_link(path):
        return TRANSFER_IN_USE if base in files else ""
    for root, dirs, _names in os.walk(path, onerror=lambda e: None):
        dirs[:] = [d for d in dirs if not _is_link(os.path.join(root, d))]
        for name in dirs:
            if name.strip().lower() in folders:
                return TRANSFER_IN_USE
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

    The same edit ``server.main._manifest_forget`` makes after
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


def _prune(store: str, entries: list, cap: int, folders: set = frozenset(),
           files: set = frozenset()) -> dict:
    """Delete oldest-first from *entries* until the store is under *cap*.

    The cap is on the store's size ON DISK, so the sum is over the live folders
    (a leftover from a previous version counts like anything else) and an entry
    that cannot be deleted stays in it. Ties on age break by path, so two
    entries written in the same second come out in an order a test can name.
    *folders* / *files* are what a running slskd transfer owns (see
    :func:`running_transfer_names`); the trash bin passes neither.
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
        reason = _in_use(e, folders, files)
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


def prune_soulseek_cache(cfg=None) -> dict:
    """Bring the Soulseek download/staging cache under its cap, oldest first."""
    cfg = load_config() if cfg is None else cfg
    folders, files = running_transfer_names(cfg)
    return _prune("soulseek", soulseek_entries(cfg),
                  cap_bytes(cfg, "soulseek"), folders, files)


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
    """One pass over both stores: prune what is over its cap, announce it.

    Neither store can take the other down: a store whose walk or delete raised
    is reported in place of its result and the other one still runs.
    """
    cfg = load_config() if cfg is None else cfg
    out: dict = {}
    for store, fn in (("soulseek", prune_soulseek_cache), ("trash", prune_trash)):
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

    The same shape as the other workers (server/wishes_worker, server/
    artist_watch_worker) and started from the app's lifespan, so a server
    nobody has opened a page on still holds the caps.
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
