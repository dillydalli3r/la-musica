"""Trash bin — the recoverable other end of POST /api/album/remove.

``mlo.paths.trash_path`` does the move; what lives here is the bin's own
surface: listing what it holds (with the size the Home card shows), deleting an
entry for good, and putting one back where it came from. The origin manifest
lives INSIDE the bin (``.mlo_manifest.json``), so it travels with the data —
see the banner comment below for the entry-name shape it records.
"""
import json
import os
import re
import time
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from mlo.paths import move_path, trash_dir
from server import auth as auth_mod
from server import mbresolve, tagcache
from server.api_common import (_in_music_folder,
                               is_audio_file,
                               load_config)


router = APIRouter(tags=["trash"])


class TrashDelete(BaseModel):
    """Basenames of trash-bin entries to delete permanently."""
    names: List[str] = []


class TrashRestore(BaseModel):
    """Basenames to move back out of the bin. `dest` is the fallback folder
    for entries whose original location was never recorded."""
    names: List[str] = []
    dest: Optional[str] = None


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


@router.get("/api/trash")
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


@router.post("/api/trash/delete")
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
        # Same invalidation the move endpoint does: the library and MB cache
        # still describe the deleted files.
        tagcache.invalidate_album(trash)
        mbresolve.invalidate()
    return {"deleted": deleted, "failed": failed, "freed": freed}


@router.post("/api/trash/restore")
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
    restored_roots = set()
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
            restored_roots.add(target if os.path.isdir(target) else os.path.dirname(target))
    if restored:
        # Same invalidation the move endpoint does — the library just gained
        # albums back, and the manifest just lost rows.
        _manifest_write(trash, origins)
        tagcache.invalidate_album(*restored_roots)
        mbresolve.invalidate()
    return {"restored": restored, "failed": failed}
