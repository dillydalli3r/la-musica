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
import of the same album (which clears it when there is nothing left to report)
AND re-derived when the prompts are READ (`missing_now`) — a family filled
after the import (the wizard step the prompt links to, a cover search, a lyrics
fetch) withdraws the prompt without another import ever running — plus whatever
the user dismissed by hand.
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

    Two kinds of entry are dropped instead of listed, and both are the same
    statement — the condition the prompt announced no longer holds:

    * an album whose folder no longer exists: a link to a folder that is gone
      is a dead end, and re-importing the album is what raises the prompt
      again, and
    * an album whose missing families have been supplied SINCE the import that
      reported them (`missing_now`): the wizard's own step the prompt links to,
      the album page's cover search, a lyrics fetch or another import can all
      fill a family without going near this table, and a prompt for what is no
      longer missing is exactly the stale row this view must not show. The
      entry is dropped here rather than at the next import of the album, which
      may never come.

    A prompt whose gap cannot be re-derived (the folder is unreadable, the
    grader raised) is KEPT: silence is not the same as answered.
    """
    path = _path(cfg)
    if not path or not os.path.isfile(path):
        return []
    data = _load(path)
    live = {}
    for k, v in data.items():
        if not isinstance(v, dict) or not os.path.isdir(str(v.get("album") or "")):
            continue
        named = _ids(v)
        now = missing_now(str(v.get("album") or ""), cfg) if named else None
        if now is not None and not (set(named) & now):
            continue                    # every family it named is supplied
        live[k] = v
    if live != data:
        _save(path, live)
    return sorted((dict(v, id=k) for k, v in live.items()),
                  key=lambda e: float(e.get("at") or 0), reverse=True)


# The re-derivation's memo. The caller is a route the UI POLLS (the queue view,
# every few seconds while something is moving) and a re-derivation runs the
# grader on a real album, so the answer is remembered — but a remembered answer
# must never outlive its reason. `_signature` is that reason: the album's own
# directory stamp. A cover written beside the tracks moves the directory's
# mtime, a tag written into a track moves the file's, a sidecar added or
# removed moves both the count and the directory — so the family the user just
# supplied is re-derived on the very next poll. The TTL is the backstop for
# everything OUTSIDE the album (a settings change that turns a check off): no
# memo is trusted longer than this, however still the files are.
_VERIFY_TTL_S = 60.0
_verified = {}      # (album key, mode, review families) -> (signature, ids, at)

# The checks the re-derivation switches OFF: none of them can produce a FAMILY
# code (`mlo.import_policy.FAMILIES` is the family vocabulary, and `gaps` folds
# nothing else into a family), so they cannot change the answer — and they are
# the expensive half of a grade: decoding a CD to verify its rip-log CRCs, an
# external audio audit, the library index an expected-track list needs. A route
# the UI polls must not pay for them.
_VERIFY_OFF = ("grade_check_crc", "grade_check_audit", "grade_check_log_checksum",
               "grade_check_cd_log", "grade_check_cd_cue", "grade_check_cd_format",
               "grade_check_log_grade", "grade_check_expected_tracks")


def _signature(album_dir):
    """What a CHANGE to the album looks like, without reading one byte of it:
    the directory's own mtime, how many entries it holds, the newest entry's
    mtime and their total size. One listdir and one stat per entry."""
    try:
        entries = []
        with os.scandir(str(album_dir)) as it:
            for e in it:
                st = e.stat()
                entries.append((st.st_mtime_ns, st.st_size))
        dir_mtime = os.stat(str(album_dir)).st_mtime_ns
    except OSError:
        return None
    return (dir_mtime, len(entries),
            max((m for m, _ in entries), default=0),
            sum(size for _, size in entries))


def _forget_verified(album_dir):
    """Drop this album's memo, whatever config it was derived under."""
    key = _key(album_dir)
    for k in [k for k in _verified if k[0] == key]:
        _verified.pop(k, None)


def _memo_key(album_dir, cfg):
    """One album under ONE policy: the mode and the families the user keeps for
    themselves are the two things `effective_config` reads, so they are what a
    memoized answer is only valid for."""
    from mlo import import_policy

    return (_key(album_dir), str(import_policy.mode(cfg or {})),
            tuple(import_policy.review_families(cfg or {})))


def missing_now(album_dir, cfg=None):
    """The family ids this album is missing RIGHT NOW (a set), or None when the
    question cannot be answered.

    `mlo.import_policy.gaps` is the ONE source of what "missing" means — the
    same call `imports.finish_album` makes at the end of an import, so a prompt
    withdrawn here and a gap that would be reported there are the same
    statement — asked with the effective config an import runs under and the
    checks in _VERIFY_OFF switched off. Memoised per album (see _signature).
    """
    from mlo import import_policy
    from mlo.stats import is_audio_file

    try:
        if not any(is_audio_file(f) for f in os.listdir(str(album_dir))):
            # A folder with no audio cannot be graded at all: `gaps` would
            # answer with the advisory alone (the one family the grader does
            # not read off the tracks) and every OTHER family would read as
            # supplied — the opposite of the truth. Nothing was filled here,
            # so nothing is withdrawn.
            return None
    except OSError:
        return None

    eff = dict(import_policy.effective_config(cfg or {}))
    for name in _VERIFY_OFF:
        eff[name] = False
    key = _memo_key(album_dir, cfg)
    sig = _signature(album_dir)
    now = time.time()
    hit = _verified.get(key)
    if hit and hit[0] == sig and now - hit[2] < _VERIFY_TTL_S:
        return set(hit[1])
    try:
        ids = set(import_policy.gaps(str(album_dir), eff))
    except Exception:
        traceback.print_exc()
        return None
    _verified[key] = (sig, tuple(sorted(ids)), now)
    return ids


def for_album(album_dir, cfg=None):
    """This album's pending prompt ({} when there is none)."""
    path = _path(cfg)
    if not path or not os.path.isfile(path):
        return {}
    return dict(_load(path).get(_key(album_dir)) or {})


def clear(album_dir, cfg=None):
    """Drop this album's prompt (the album is complete, or the reader is done
    with it). Returns True when an entry was actually removed."""
    _forget_verified(album_dir)
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
        # The next READ re-derives this album's gaps (see `prompts`): what the
        # import reported a moment ago is not an answer worth caching, and a
        # prompt whose families are already supplied must not sit listed for a
        # memo window just because this call is what put it there.
        _forget_verified(album_dir)
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
