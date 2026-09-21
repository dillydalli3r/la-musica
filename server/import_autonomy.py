"""The prompt an import raises when it could not finish an album by itself.

One entry per album, in ONE file (`<music>/.mlo/data/import_prompts.json`), so
a gap is announced exactly once however the album reached the library: the
wizard's finish, the bulk queue, the downloads runner and the Soulseek importer
all end in `server.imports.finish_album`, which raises the prompt here.

Two things happen, and both are the point of the feature:

* an ``import_needs_data`` event goes out on the one bus every client listens
  to (`server.events`) — the notification the user sees, carrying the wizard
  link, and
* the entry is stored for the wizard to list and act on, so a prompt raised
  while no client was open is still there when one opens.

The decided-ness of an album is never stored here: the entry is what
`mlo.import_policy.gaps` said at the end of an import, refreshed by the next
import of the same album (which clears it when there is nothing left to
report), plus whatever the user dismissed by hand.
"""

import json
import os
import time
import traceback

from server import events

_PROMPTS_NAME = "import_prompts.json"


def _path(cfg=None):
    """Where the prompts live, or None when there is no app data dir."""
    from mlo.paths import app_data_dir

    try:
        d = app_data_dir(str((cfg or {}).get("music_folder") or "") or None)
    except Exception:
        return None
    return os.path.join(d, _PROMPTS_NAME) if d else None


def _key(album_dir):
    """One album, one entry — the same normalization the staged review uses."""
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


def _line(entry):
    """One family in the notification body: who is asked, and for what."""
    label = str(entry.get("label") or entry.get("id") or "")
    note = str(entry.get("note") or "")
    if entry.get("state") == "decision":
        return f"{label} — waiting for you ({note})" if note else f"{label} — waiting for you"
    fields = ", ".join(entry.get("fields") or [])
    detail = note or fields
    return f"{label} — no source could supply it ({detail})" if detail \
        else f"{label} — no source could supply it"


def _entry(album_dir, cfg, missing, mode, reason):
    album = os.path.normpath(str(album_dir))
    # One spelling everywhere: the "/" form is what the rest of the app hands a
    # client (`_bulk_one`'s album_path), and the link quotes exactly the string
    # the entry carries so a reader can match one against the other.
    album_fwd = album.replace("\\", "/")
    families = [dict(v) for _, v in sorted(missing.items(), key=lambda kv: _order(kv[0]))]
    return {
        "album": album_fwd,
        "album_name": os.path.basename(album.rstrip("\\/")) or album,
        "at": time.time(),
        "mode": str(mode or "automatic"),
        "reason": str(reason or "missing"),
        "link": _link(album_fwd, families),
        "families": families,
    }


def _order(family_id):
    from mlo.import_policy import order_of

    return order_of(family_id)


def _link(album, families):
    from mlo.import_policy import wizard_link

    return wizard_link(album, [f.get("id") for f in families])


def title(entry):
    """The notification's headline: which album, and that it wants a person."""
    return f"Import needs a decision: {entry.get('album_name') or 'album'}"


def body(entry):
    """What is missing, in the order of the wizard's steps."""
    return "; ".join(_line(f) for f in entry.get("families") or [])


def prompts(cfg=None):
    """Every album waiting on the user, newest first.

    An entry whose album no longer exists is dropped instead of listed: a link
    to a folder that is gone is a dead end, and re-importing the album is what
    raises the prompt again.
    """
    path = _path(cfg)
    if not path or not os.path.isfile(path):
        return []
    data = _load(path)
    live = {k: v for k, v in data.items()
            if isinstance(v, dict) and os.path.isdir(str(v.get("album") or ""))}
    if live != data:
        _save(path, live)
    return sorted((dict(v, id=k) for k, v in live.items()),
                  key=lambda e: float(e.get("at") or 0), reverse=True)


def for_album(album_dir, cfg=None):
    """This album's pending prompt ({} when there is none)."""
    path = _path(cfg)
    if not path or not os.path.isfile(path):
        return {}
    return dict(_load(path).get(_key(album_dir)) or {})


def clear(album_dir, cfg=None):
    """Drop this album's prompt (the album is complete, or the reader is done
    with it). Returns True when an entry was actually removed."""
    path = _path(cfg)
    if not path or not os.path.isfile(path):
        return False
    data = _load(path)
    if data.pop(_key(album_dir), None) is None:
        return False
    _save(path, data)
    return True


def raise_prompt(album_dir, cfg, missing, *, mode="automatic", reason="missing"):
    """Store and announce what this album is still missing.

    One call per finished import: *missing* is `mlo.import_policy.gaps`' own
    answer, so what the notification names is exactly what grading fails. An
    empty *missing* clears the album's entry — the import that resolved the
    gaps is the same call that withdraws the prompt.

    The SAME gap raised again is not a new outcome and does not repeat the
    notification: an album re-imported (a re-run, a script chain that still
    cannot supply the family) says so in the log and in this table, once. Only
    a different set of missing families, a different reason or a different mode
    is news — that is what "one notification per outcome" means here.

    Never raises: an import reports its album, and a prompt that cannot be
    written must not turn a finished import into a failed one.
    """
    try:
        if not missing:
            clear(album_dir, cfg)
            return None
        entry = _entry(album_dir, cfg, missing, mode, reason)
        path = _path(cfg)
        if not path:
            return None
        data = _load(path) if os.path.isfile(path) else {}
        before = data.get(_key(album_dir)) or {}
        same = (_ids(before) == _ids(entry)
                and str(before.get("reason") or "") == entry["reason"]
                and str(before.get("mode") or "") == entry["mode"])
        data[_key(album_dir)] = entry
        _save(path, data)
        if not same:
            events.emit("import_needs_data", title(entry), body(entry),
                        {"link": entry["link"], "album_path": entry["album"],
                         "reason": entry["reason"],
                         "families": [f.get("id") for f in entry["families"]]},
                        config=cfg)
        return entry
    except Exception:
        traceback.print_exc()
        return None


def _ids(entry):
    """The missing family ids of a stored entry, in wizard order — what "the
    same gap" is compared by."""
    return [str(f.get("id") or "") for f in (entry or {}).get("families") or []]
