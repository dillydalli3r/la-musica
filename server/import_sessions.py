"""A manual import the user left unfinished, remembered so it can be resumed.

The wizard holds every step of a manual import in React state and, until this
module existed, kept nothing once the tab was closed: the uploaded album sat in
the library (step 0 already moved the files in) with no record that it was
mid-import, so nothing could offer to finish it. The gap-prompt table
(``server.import_autonomy``) is a different thing — it is raised only *after* a
``finish_album`` run, from the grader, and it is re-derived on read. A session
here is the opposite end: a run that has not reached Finish at all.

One entry per album, in ONE file (``<music>/.mlo/data/import_sessions.json``),
written by the wizard as its step changes and cleared when the album is
finished or the user discards it. The client that reads the list
(``GET /api/import/sessions``) puts a "Continue import" row in the notification
tray; opening it re-enters the wizard on that album at the remembered step.

Every session ALSO stamps the album FOLDER with a mid-import marker
(``mlo.paths.save_importing``, ``.mlo_importing.json``), which is the durable
half of "this album is being imported": it survives a restart and travels with
the chain's own organize/beets rename, so
``server.imports.importing_album`` — the one predicate every grading and
warning surface reads — still knows the album is mid-import after the process
comes back, and a bookmark whose folder was renamed is relocated through the
marker instead of pruned. The file above stays the client-facing list; the
marker is what ties it to the grader.

Nothing here is authoritative about the album — it is just a bookmark. A
session whose folder is gone AND which has no marker anywhere is pruned on read
rather than left to link to nothing.
"""

import json
import os
import time
import traceback

_SESSIONS_NAME = "import_sessions.json"


def _path(cfg=None):
    """Where the sessions live, or None when there is no app data dir."""
    from mlo.paths import app_data_dir

    try:
        d = app_data_dir(str((cfg or {}).get("music_folder") or "") or None)
    except Exception:
        return None
    return os.path.join(d, _SESSIONS_NAME) if d else None


def _key(album_dir):
    """One album, one entry — the same normalization the prompt table uses."""
    return os.path.normpath(str(album_dir)).replace("\\", "/").lower()


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError:
        traceback.print_exc()


def _row(entry):
    """The public shape the client reads, or None for a dead entry."""
    album = str(entry.get("album") or "")
    if not album or not os.path.isdir(album):
        return None
    step = entry.get("step")
    try:
        step = int(step)
    except (TypeError, ValueError):
        step = 1
    return {
        "album": album,
        "album_name": str(entry.get("album_name") or ""),
        "step": max(0, step),
        "staged": bool(entry.get("staged")),
        "at": entry.get("at"),
    }


def _write_marker(album, row):
    """Stamp the album FOLDER with this session, so it survives a restart AND
    a rename the import's own organize/beets step performs.

    The folder is what carries the record: an interrupted chain can move the
    album (script 14 imports and renames it), and the path-keyed table above
    would then point at a folder that is gone and be pruned — leaving nothing
    to resume and an album the grader would report as failing. The marker's
    `album` field is the ORIGINAL path, which is how :func:`sessions`
    relocates the row it belongs to (see `_find_markers`). Never fatal: a
    session whose folder cannot be written is still a central record.
    """
    try:
        from mlo.paths import save_importing
        save_importing(album, {
            "album": str(row.get("album") or album),
            "album_name": str(row.get("album_name") or ""),
            "step": int(row.get("step") or 1),
            "staged": bool(row.get("staged")),
            "session": True,
            "at": row.get("at") or time.time(),
        })
    except Exception:
        traceback.print_exc()


def _clear_marker(album):
    try:
        from mlo.paths import clear_importing
        clear_importing(album)
    except Exception:
        traceback.print_exc()


def _find_markers(cfg=None):
    """Every mid-import marker in the library, keyed by the ORIGINAL album path
    it names: ``{key: (current folder, marker)}``.

    One bounded walk of the library root, run ONLY when a central row's folder
    has gone (the one case a rename creates). The marker travels with the
    folder, so its own ``album`` field still names the path the row was
    written under — that is what relocates the row. A library that holds no
    marker finds none and the walk is the only cost.
    """
    try:
        from mlo.paths import IMPORTING_FILE, library_root
        root = library_root(str((cfg or {}).get("music_folder") or "") or None)
    except Exception:
        return {}
    if not root or not os.path.isdir(root):
        return {}
    out = {}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        if IMPORTING_FILE not in files:
            continue
        try:
            from mlo.paths import load_importing
            info = load_importing(dirpath) or {}
        except Exception:
            continue
        key = _key(info.get("album") or dirpath)
        if key:
            out[key] = (dirpath, info)
    return out


def sessions(cfg=None):
    """Every live unfinished import, newest first. Dead folders are dropped.

    A row whose folder is gone is NOT dropped outright any more: an import
    interrupted by a rename (the chain's own organize/beets step) leaves the
    folder somewhere else with the mid-import marker inside it, and the marker
    names the path the row was written under. `_find_markers` finds it and the
    row is relocated to where the album is NOW, so the tray still offers to
    continue it. Only a folder with no marker anywhere really is gone.
    """
    path = _path(cfg)
    if not path:
        return []
    data = _load(path)
    out, live, missing = [], {}, {}
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        row = _row(entry)
        if row is None:
            missing[key] = entry
            continue
        live[key] = entry
        out.append(row)
    if missing:
        found = _find_markers(cfg)
        for key, entry in missing.items():
            hit = found.get(key)
            if hit is None:
                continue
            folder, marker = hit
            moved = dict(entry)
            moved["album"] = folder.replace("\\", "/")
            moved["step"] = marker.get("step", entry.get("step"))
            moved["at"] = marker.get("at", entry.get("at"))
            row = _row(moved)
            if row is None:
                continue
            live[key] = moved
            out.append(row)
    if len(live) != len(data):
        _save(path, live)
    out.sort(key=lambda r: float(r.get("at") or 0), reverse=True)
    return out


def get(album, cfg=None):
    """This album's live session, or None."""
    if not album:
        return None
    key = _key(album)
    for row in sessions(cfg):
        if _key(row["album"]) == key:
            return row
    return None


def upsert(album, step=1, album_name="", staged=False, cfg=None):
    """Remember (or refresh) this album's unfinished import. Returns the row."""
    path = _path(cfg)
    if not path or not album:
        return None
    album_fwd = os.path.normpath(str(album)).replace("\\", "/")
    row = {
        "album": album_fwd,
        "album_name": str(album_name or "").strip()
                      or (os.path.basename(album_fwd.rstrip("/")) or album_fwd),
        "step": max(0, int(step or 0)),
        "staged": bool(staged),
        "at": time.time(),
    }
    data = _load(path)
    data[_key(album_fwd)] = row
    _save(path, data)
    _write_marker(album_fwd, row)
    return _row(row)


def dismiss(album=None, cfg=None):
    """Forget one album's session, or every session when *album* is None.

    Clears the folder's mid-import marker too: the marker is what makes the
    album read as mid-import after a restart, and a discarded bookmark that
    left it behind would hide a finished-looking album from grading forever.
    """
    path = _path(cfg)
    if not path:
        return
    data = _load(path)
    if album is None:
        for entry in data.values():
            if isinstance(entry, dict) and entry.get("album"):
                _clear_marker(str(entry["album"]))
        # And any marker whose row was already pruned (a folder a rename moved
        # out from under its bookmark): "start over" means no mid-import state
        # is left anywhere, or an album would stay hidden from grading with
        # nothing left to resume it.
        for folder, _marker in _find_markers(cfg).values():
            _clear_marker(folder)
        _save(path, {})
        return
    entry = data.pop(_key(album), None)
    # Clear the marker at BOTH the path the caller named and the path the row
    # remembers: a relocation (R381) can leave the row naming a folder the
    # chain has since renamed back, and a marker left behind is an album hidden
    # from grading with nothing to resume it.
    from mlo.paths import is_importing
    named = [album]
    if isinstance(entry, dict) and entry.get("album"):
        named.append(str(entry["album"]))
    stray = not any(is_importing(p) for p in named)
    for p in named:
        _clear_marker(p)
    if stray:
        # Neither path held the marker, so it has MOVED: the chain's own
        # organize step renamed the folder, and the marker — written inside it —
        # travelled with the rename while still naming the path it was written
        # under. The wizard dismisses with the path it started from, so a
        # FINISHED import used to leave its marker in the album's new folder:
        # the row disappeared from the tray (nothing left to dismiss it), and
        # the library read "importing" until the next restart's sweep. The
        # marker's own `album` field is the only link between the old path and
        # the folder the album is in now, which is what `_find_markers` reads.
        keys = {_key(p) for p in named}
        for key, (folder, _marker) in _find_markers(cfg).items():
            if key in keys:
                _clear_marker(folder)
    _save(path, data)
