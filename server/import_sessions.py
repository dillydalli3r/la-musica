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

Nothing here is authoritative about the album — it is just a bookmark. A
session whose folder is gone is pruned on read rather than left to link to
nothing.
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


def sessions(cfg=None):
    """Every live unfinished import, newest first. Dead folders are dropped."""
    path = _path(cfg)
    if not path:
        return []
    data = _load(path)
    out, live = [], {}
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        row = _row(entry)
        if row is None:
            continue
        live[key] = entry
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
    return _row(row)


def dismiss(album=None, cfg=None):
    """Forget one album's session, or every session when *album* is None."""
    path = _path(cfg)
    if not path:
        return
    data = _load(path)
    if album is None:
        _save(path, {})
        return
    data.pop(_key(album), None)
    _save(path, data)
