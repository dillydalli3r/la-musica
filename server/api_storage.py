"""How much the disk behind the library holds, and what each part of the app
is using of it.

GET /api/storage answers ONE snapshot: the volume's total/free/used, the size
the library itself occupies (its audio plus the covers and text sidecars that
belong to it), the app's own state (``.mlo/data``), the trash bin, and the
downloads/staging pair when this install has them. The Home page and the
Dependencies page render it as one card (``web/src/components/StorageCard.tsx``).

Three rules the answer follows, because a storage readout that lies is worse
than none at all:

  * **It never raises.** An unreadable directory is stepped over and NAMED in
    ``skipped``; a volume the OS will not measure (a network mount that has
    dropped off) answers ``null`` for total/free, which the card shows as
    "unknown" rather than "0 bytes free".
  * **One pass per root, from directory entries.** Sizes come from
    ``entry.stat(follow_symlinks=False)`` — nothing is opened, and no file is
    stat'ed twice, so a big library answers in a second or two.
  * **Every byte count is an int.** No float ever accumulates bytes (a 32-bit
    build cannot hold one exactly past 2**53).

The drive sizes come from ``server.exporter.list_drives()`` — the Export page
reads drive space the same way, so the app has ONE reader for it, not two.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

from mlo.config import load_config
from mlo.paths import (ALL_IMAGE_EXTS, LIB_AUDIO_EXTS, SKIP_DIRS,
                       app_data_dir,
                       downloads_dir, incomplete_dir, library_root, mlo_root,
                       tools_dir, tools_dirs, trash_root)
# The one link test in the tree: the library walks use it too, so a junction
# that would double-count (or loop) is recognised the same way here.
from mlo.stats import _linked_dir

router = APIRouter()

# Pre-lowered once: the walk visits every directory of the library.
_SKIP_LOWER = {d.lower() for d in SKIP_DIRS}

# What counts as the LIBRARY: the audio itself, the covers (an album's
# ``cover.jpg``, a track's sidecar ``01 - Song.jpg``, artist art) and the text
# sidecars the app writes beside a release. Anything else in an album folder —
# an archive, an installer, a downloaded PDF — is not library storage and must
# not inflate the library's figure.
_AUDIO_EXTS = frozenset(LIB_AUDIO_EXTS)
_LIBRARY_EXTS: Dict[str, str] = {e: "audio" for e in _AUDIO_EXTS}
_LIBRARY_EXTS.update({e: "sidecar" for e in
                      (frozenset(ALL_IMAGE_EXTS) | {".txt", ".log", ".cue", ".lrc", ".m3u8"})
                      if e not in _AUDIO_EXTS})

# A share that is going away turns every directory of a walk unreadable; the
# list is capped so the answer stays a card, and ``skipped_count`` says how
# many there were.
MAX_SKIPPED = 100


def _isdir(path) -> bool:
    """os.path.isdir that answers False instead of raising on a path the OS
    cannot even parse (an embedded NUL, a UNC host that is gone)."""
    try:
        return bool(path) and os.path.isdir(path)
    except (OSError, ValueError):
        return False


def _why(exc) -> str:
    """The OS's own words for a failed read ("Permission denied"), or the
    exception's class name when it carries none."""
    return str(getattr(exc, "strerror", None) or exc) or exc.__class__.__name__


def _is_link(entry) -> bool:
    """Whether a scanned entry is a link of any kind.

    A Windows junction (``mklink /J``, a mapped folder) is a directory that
    ``is_symlink`` reports as False, so a walk testing only for symlinks would
    follow one — and one pointing back up its own chain never terminates.
    """
    return _linked_dir(entry)


def read_fs_usage(path) -> Dict[str, Any]:
    """Total/free/used of the volume holding *path*, plus its label and kind.

    Sizes come from ``server.exporter.list_drives()`` (the one drive reader in
    the tree); a path no drive entry covers — a UNC share, a mount point — falls
    back to ``shutil.disk_usage``. A volume the OS will not measure answers
    ``None`` for total/free/used/percent, never 0.

    Never raises. The exporter import is lazy so this module stays importable
    without its audio stack.
    """
    out: Dict[str, Any] = {"mount": None, "label": None, "type": None,
                           "total_bytes": None, "free_bytes": None,
                           "used_bytes": None, "percent_used": None}
    try:
        drive = os.path.splitdrive(os.path.abspath(path))[0]
    except (OSError, ValueError, TypeError):
        drive = ""
    out["mount"] = drive or "/"
    total = free = None
    try:
        from server.exporter import list_drives

        key = os.path.normcase(drive)
        for d in list_drives():
            if os.path.normcase(os.path.splitdrive(str(d.get("root") or ""))[0]) != key:
                continue
            out["mount"] = d.get("root") or out["mount"]
            out["label"] = d.get("letter")
            out["type"] = d.get("type")
            total, free = d.get("total"), d.get("free")
            break
    except Exception:
        pass  # a drive list that cannot be read is "unknown", not an error
    if total is None or free is None:
        try:
            import shutil
            u = shutil.disk_usage(path)
            total, free = int(u.total), int(u.free)
        except (OSError, ValueError):
            total = free = None
    if total is None or free is None:
        return out
    total, free = int(total), int(free)
    used = total - free
    out["total_bytes"] = total
    out["free_bytes"] = free
    out["used_bytes"] = used
    # A ratio is the one figure that may be a float; the bytes above never are.
    out["percent_used"] = round(used * 100 / total, 1) if total > 0 else None
    return out


def scan(root, ext_buckets: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    """One ``os.scandir`` pass over *root*: bytes, files, buckets, skipped.

    *ext_buckets* maps a lower-cased extension to a bucket name (the library
    passes ``_LIBRARY_EXTS``); files whose extension is not in the table are
    not counted. With no table, EVERY regular file counts — the app's state,
    the bin and the downloads tree hold whatever they hold, and each of those
    figures is about disk, not about music.

    Directory links are not followed and a file reached through a link is never
    counted, so nothing is measured twice; both kinds are reported in
    ``skipped``. An unreadable directory is reported and stepped over instead
    of raising: half an answer with the gap named beats no answer.

    Returns ``None`` when *root* does not exist — "this install has no trash
    yet" and "the trash is empty" are different statements.
    """
    if not _isdir(root):
        return None
    out: Dict[str, Any] = {"bytes": 0, "files": 0, "buckets": {}, "skipped": [],
                           "skipped_count": 0, "skipped_links": 0,
                           "skipped_unreadable": 0}

    def skip(path, reason, kind="unreadable"):
        """One entry the walk stepped over — and WHICH KIND of thing it was.

        The two kinds are not the same answer and must not be counted as one:
        an UNREADABLE directory is a gap in the figures (nobody knows what is
        in it), while a LINK is a deliberate skip that costs the figures
        nothing at all — a file reached through a link is counted once, at the
        real file, so not following it is the correct arithmetic rather than a
        missing number. The bundled tools carry both (a `libjpeg.so` version
        symlink is a link; a toolchain unpacked onto a network mount is
        unreadable), and the card says which is which.
        """
        out["skipped_count"] += 1
        if kind == "link":
            out["skipped_links"] += 1
        else:
            out["skipped_unreadable"] += 1
        if len(out["skipped"]) < MAX_SKIPPED:
            out["skipped"].append({"path": path, "reason": reason, "kind": kind})

    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            entries = os.scandir(cur)
        except OSError as e:
            skip(cur, _why(e))
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.lower() in _SKIP_LOWER:
                            continue
                        if _is_link(entry):
                            skip(entry.path, "linked directory — not followed",
                                 kind="link")
                            continue
                        stack.append(entry.path)
                        continue
                    if _is_link(entry):
                        skip(entry.path, "symbolic link — not followed",
                             kind="link")
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue  # a device, socket or other special entry
                    bucket = None
                    if ext_buckets is not None:
                        bucket = ext_buckets.get(os.path.splitext(entry.name)[1].lower())
                        if bucket is None:
                            continue
                    # The size straight off the directory entry: one stat, no
                    # open, and the same entry is never visited twice.
                    size = int(entry.stat(follow_symlinks=False).st_size)
                except OSError as e:
                    skip(entry.path, _why(e))
                    continue
                out["bytes"] += size
                out["files"] += 1
                if bucket:
                    out["buckets"][bucket] = out["buckets"].get(bucket, 0) + size
    return out


class _Skips:
    """The walks' skipped entries, for the answer's single ``skipped`` list —
    and the two KINDS counted apart, because only one of them is a gap: a link
    is not followed on purpose (its target is counted once, where it lives),
    while an unreadable directory is a figure nobody could take."""

    def __init__(self) -> None:
        self.rows: List[Dict[str, str]] = []
        self.count = 0
        self.links = 0
        self.unreadable = 0

    def add(self, result: Optional[Dict[str, Any]]) -> None:
        if not result:
            return
        self.count += result["skipped_count"]
        self.links += result["skipped_links"]
        self.unreadable += result["skipped_unreadable"]
        for row in result["skipped"]:
            if len(self.rows) < MAX_SKIPPED:
                self.rows.append(row)


def _row(path, result: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """One named size row, or None when the folder is not there."""
    if result is None:
        return None
    row: Dict[str, Any] = {"path": path, "bytes": result["bytes"],
                           "files": result["files"]}
    if result["buckets"]:
        row["audio_bytes"] = result["buckets"].get("audio", 0)
        row["sidecar_bytes"] = result["buckets"].get("sidecar", 0)
    return row


def _music_folder(cfg) -> Optional[str]:
    """The configured music folder, or None when it is unset or missing.

    The endpoint answers either way: an install that has not chosen a folder
    yet still has a volume and an app-data folder worth reporting. The import
    is the lazy one every router uses to reach ``server.main``'s resolver
    (it raises HTTPException when nothing is configured).
    """
    try:
        from server.main import _music_folder as resolve
        return resolve(cfg)
    except (HTTPException, ImportError):
        return None


def _download_dirs(cfg, folder) -> List[str]:
    """The download pair transfers really land in: slskd's configured folder
    (``soulseek_download_dir``, else ``<music>/.mlo/downloads``) and the
    staging sibling of it. Both are read out of ``server.soulseek``, which
    derives them, so the card measures the folders the client uses rather than
    a second guess at their names."""
    try:
        from server import soulseek as slsk
        pairs = [slsk.download_dir(cfg), slsk._incomplete_dir(cfg)]
    except Exception:
        pairs = [downloads_dir(folder), incomplete_dir(folder)]
    out: List[str] = []
    for p in pairs:
        if p and p not in out:
            out.append(p)
    return out


def _sum_rows(rows: List[Optional[Dict[str, Any]]]) -> Dict[str, int]:
    """One figure from several sized rows — the app's own footprint.

    A row the walk could not take (a folder that does not exist, a volume the
    OS refused) contributes nothing instead of a zero, and a list where NO row
    was measurable answers 0/0 with `measured: False`: "the app uses nothing"
    and "nothing could be read" are different answers, and only the caller can
    tell the user which one it is looking at.
    """
    present = [r for r in rows if r]
    return {
        "bytes": sum(int(r.get("bytes") or 0) for r in present),
        "files": sum(int(r.get("files") or 0) for r in present),
        "measured": bool(present),
    }


def storage_snapshot(cfg, folder: Optional[str]) -> Dict[str, Any]:
    """Everything GET /api/storage answers, given a resolved music folder."""
    started = time.monotonic()
    skips = _Skips()
    lib = library_root(folder)
    data = app_data_dir(folder)

    lib_row = None
    if lib:
        res = scan(lib, _LIBRARY_EXTS)
        skips.add(res)
        lib_row = _row(lib, res)
    data_res = scan(data)
    skips.add(data_res)
    data_row = _row(data, data_res)
    trash = trash_root(folder)
    trash_res = scan(trash)
    skips.add(trash_res)

    dl_roots = _download_dirs(cfg, folder)
    staging = dl_roots[1] if len(dl_roots) > 1 else None
    dl_rows: List[Dict[str, Any]] = []
    dl_bytes = dl_files = stage_bytes = stage_files = 0
    for p in dl_roots:
        res = scan(p)
        skips.add(res)
        row = _row(p, res)
        if row is None:
            continue
        dl_rows.append(row)
        dl_bytes += row["bytes"]
        dl_files += row["files"]
        if p == staging:
            stage_bytes, stage_files = row["bytes"], row["files"]

    # The app's own tools (ffmpeg, slskd, the analysers) live under the music
    # folder now — <music>/.mlo/tools — and are measured in BOTH places they can
    # be: that folder, and the pre-move <app folder>/.dependencies (see
    # mlo.paths.tools_dirs), because an install that has not updated a tool
    # since the move still keeps it there and it is still the app's footprint.
    # One row, named for the current folder; an install that has downloaded no
    # tool yet has neither folder, which is a null row and contributes nothing
    # rather than a zero.
    deps_rows = []
    for root in tools_dirs(folder):
        res = scan(root)
        skips.add(res)
        deps_rows.append(_row(root, res))
    deps_row = _sum_rows(deps_rows) if any(deps_rows) else None
    if deps_row:
        deps_row["path"] = tools_dir(folder)

    # The volume to report is the one the library lives on; with no library
    # yet (an install before its music folder is set) it is the app's own data
    # folder, which is what the remaining numbers are about anyway.
    anchor = lib if _isdir(lib) else (data if _isdir(data) else None)
    fs = read_fs_usage(anchor or mlo_root(folder) or os.getcwd())

    return {
        "mount": fs["mount"],
        "label": fs["label"],
        "type": fs["type"],
        "total_bytes": fs["total_bytes"],
        "free_bytes": fs["free_bytes"],
        "used_bytes": fs["used_bytes"],
        "percent_used": fs["percent_used"],
        "library": lib_row,
        "app_data": data_row,
        "trash": _row(trash, trash_res),
        "downloads": ({"bytes": dl_bytes, "files": dl_files,
                       "staging_bytes": stage_bytes, "staging_files": stage_files,
                       "roots": dl_rows} if dl_rows else None),
        "dependencies": deps_row,
        # EVERYTHING the app itself occupies: its state, the bin, the
        # transfers and its own tools — the library above is the user's music
        # and deliberately not part of it. One number, because "how much is la
        # musica using" is the question the per-folder rows answer only by
        # addition.
        "app_total": _sum_rows([data_row, _row(trash, trash_res),
                               {"bytes": dl_bytes, "files": dl_files} if dl_rows else None,
                               deps_row]),
        "skipped": skips.rows,
        "skipped_count": skips.count,
        # The split the card speaks in: an unreadable folder is a gap in the
        # figures and warns; a link not followed is the walk being right.
        "skipped_links": skips.links,
        "skipped_unreadable": skips.unreadable,
        "scanned_at": int(time.time()),
        "took_ms": int((time.monotonic() - started) * 1000),
    }


@router.get("/api/storage")
def storage() -> Dict[str, Any]:
    """The volume, the library, the app's own state, the bin and the transfers
    — as one snapshot, with every figure the OS refused to give left null."""
    cfg = load_config()
    return storage_snapshot(cfg, _music_folder(cfg))
