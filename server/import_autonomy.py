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

A second kind of entry lives in the same table, raised by the video remux
instead of by an import: a DISC STRUCTURE (a DVD ``VIDEO_TS`` or Blu-ray
``BDMV`` rip, ``mlo.videodisc``) whose main feature the app refuses to pick —
two titles within a few percent, a playlist it cannot read, an ``.iso``.
:func:`raise_video_prompt` stores it in the same shape (one "family" whose note
names the candidates and their durations), so the same bell, the same API and
the same queue row carry it, and :func:`prompts` re-derives it from the
structure itself: the question is open while the app would still refuse to pick
(`_video_still_needs`).

A WAIT is not a WARNING, and the difference is what the album does while an
entry stands. A **review stop** (`reason == "stopped"`) and a **video prompt**
(``kind == VIDEO_KIND``) are waits: a person is answering, or a disc structure
is about to be remuxed, and a script chain that rewrites the folder in the
meantime half-writes the very release they are working on. :func:`chain_scope`
is that guarantee — the library-wide chain run (`server.script_runners.run_chain`,
the one seam all of them pass) asks it which albums it may touch, so a sweep of
the library leaves a waiting album exactly as it is and says which ones it left
alone, while a run that names its albums (the person's own press, an import
finishing the album it names) is not filtered.

Everything else an entry stands for — a family no source could supply
(`reason == "missing"`) — is a WARNING, not a wait. The import ran to its end:
the album is in the library, the release's row is FINISHED, the chain that would
finish it has run, and the gap is announced on the one bell, shown on the
finished queue row with the wizard link and the dismiss, and re-derived on read
like any other entry. Nothing is held, nothing waits for the user, and no run
skips the album because of it (R166).
"""

import json
import os
import time
import traceback

from server import events

_PROMPTS_NAME = "import_prompts.json"

# The one "family" a VIDEO prompt carries. It is not a wizard step (no wizard
# step decides which title of a disc rip is the feature), but it is the same
# row shape, so the notification body, GET /api/import/prompts and the queue's
# "Needs you" row render it without a second vocabulary. `kind` is what tells
# the two prompt kinds apart in the stored table.
#
# honey: reusing the family row (rather than a second prompt table) is what
# keeps one bell, one API and one queue row for "a person must decide this".
# The cost is that `prompts` has to branch on `kind` for its re-derivation —
# a family prompt is re-derived from the grader, this one from the structure
# itself (`_video_still_needs`). A real wizard step for it would let the
# branch go, if the UI ever grows one.
VIDEO_KIND = "video"
VIDEO_FAMILY = "video_disc"
VIDEO_LABEL = "Main feature"


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
    """The notification's headline: which album, and what it wants.

    A family no source could supply is NOT a decision anyone is waiting on:
    the import ran to its end, the album is in the library, and the gap is a
    warning — so the headline says what the album needs instead of "needs a
    decision", which reads as a held import (and was one, while a prompt
    parked the album; see `parked_keys`). A review stop keeps that wording:
    there a person really is mid-decision and the chain has not run.
    """
    name = entry.get("album_name") or "album"
    if entry.get("kind") == VIDEO_KIND:
        return f"Which title is the main feature: {name}"
    if str(entry.get("reason") or "") == "stopped":
        return f"Import needs a decision: {name}"
    return f"{name} — needs extra data"


def body(entry):
    """What is missing, in the order of the wizard's steps."""
    return "; ".join(_line(f) for f in entry.get("families") or [])


def warning(entry):
    """One entry, in the shape EVERY surface carries it (the ONE vocabulary).

    The queue's row (`server.api_queue`), the album page's own banner
    (`GET /api/album`) and the wizard's prompt banner all read this, so a gap
    can never be described one way in the list and another on the page it
    links to. ``families``/``labels`` are the wizard's own ids and labels in
    its own order (the ids are what its ``?missing=`` parameter takes),
    ``link`` is the wizard opened AT this album and at the step that decides
    the first of them, ``detail`` is the sentence the notification body is
    made of, and ``reason``/``mode`` say which kind of entry it is: a
    ``"stopped"`` import is WAITING for the answer (review mode), anything
    else is a warning on an album that already landed (R166).
    """
    families = [f for f in (entry.get("families") or []) if isinstance(f, dict)]
    return {
        "families": [str(f.get("id") or "") for f in families],
        "labels": [str(f.get("label") or "") for f in families],
        "link": str(entry.get("link") or ""),
        "detail": body(entry),
        "reason": str(entry.get("reason") or ""),
        "mode": str(entry.get("mode") or ""),
        "waiting": _awaits_answer(entry),
    }


def _live(entry, cfg):
    """Whether a STORED entry still stands — the ONE test `prompts` and
    `for_album` share, so an entry the list would drop is an entry the album
    page cannot find either.

    An album whose folder is gone no longer stands (a link to a folder that is
    gone is a dead end, and re-importing the album is what raises the prompt
    again); a video prompt stands while the app would still refuse to pick; a
    family gap stands while any of the families it named is still missing
    (`missing_now`). A gap whose answer cannot be re-derived (the folder is
    unreadable, the grader raised) STANDS: silence is not the same as answered.
    """
    if not isinstance(entry, dict):
        return False
    if not os.path.isdir(str(entry.get("album") or "")):
        return False
    if entry.get("kind") == VIDEO_KIND:
        return _video_still_needs(str(entry.get("album") or ""), cfg)
    named = _ids(entry)
    now = missing_now(str(entry.get("album") or ""), cfg) if named else None
    return not (now is not None and not (set(named) & now))


def prompts(cfg=None):
    """Every album with an OPEN entry, newest first.

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
    grader raised) is KEPT: silence is not the same as answered. `_live` is
    that whole test, shared with `for_album`.
    """
    path = _path(cfg)
    if not path or not os.path.isfile(path):
        return []
    data = _load(path)
    live = {k: v for k, v in data.items() if _live(v, cfg)}
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
# external audio audit, the library index an expected-track list needs, and
# the FLAC stream MD5's own reference check (one decode per audio no audit has
# already verified). A route the UI polls must not pay for them.
_VERIFY_OFF = ("grade_check_crc", "grade_check_audit", "grade_check_log_checksum",
               "grade_check_cd_log", "grade_check_cd_cue", "grade_check_cd_format",
               "grade_check_log_grade", "grade_check_expected_tracks",
               "grade_check_flac_md5")


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
    _video_asked.pop(key, None)


# The video prompts' own memo, in the same shape and for the same reason as
# `_verified` above: the answer costs an ffprobe of the structure's streams and
# `prompts()` is polled. What a remembered answer is valid for is the STRUCTURE
# itself (`_structure_stamp`) — not the folder's mtime, which Windows may not
# have updated yet when the streams a remux consumed are deleted.
_VIDEO_TTL_S = 60.0
_video_asked = {}   # album key -> (structure stamp, still_open, at)


def _structure_stamp(disc):
    """What a disc structure IS, without probing one byte of media: the kind,
    the structure, and every title's key, streams and their sizes/mtimes. Two
    calls that see the same stamp would pick the same feature."""
    rows = []
    for t in disc.titles:
        streams = []
        for s in t.streams:
            try:
                e = os.stat(s)
                streams.append((os.path.normcase(s), e.st_size, e.st_mtime_ns))
            except OSError:
                streams.append((os.path.normcase(s), None, None))
        rows.append((t.key, tuple(streams), t.note))
    return (disc.kind, os.path.normcase(disc.structure), tuple(rows))


def _probe():
    """``path -> seconds | None`` from the app's own ffprobe, or None when the
    toolchain is missing — mlo.videodisc then refuses rather than guessing."""
    from mlo.remux import _duration_probe
    from mlo.tools import detect_all_tools

    exe = (detect_all_tools().get("ffmpeg") or {}).get("ffprobe_exe")
    return _duration_probe(exe) if exe else None


def _video_still_needs(folder, cfg=None):
    """Whether *folder* still holds a disc structure the app cannot pick from.

    That is exactly what a video prompt announces, so it is what the prompt is
    re-derived from: a folder that is no longer a disc structure (its streams
    were remuxed and removed, or the image was mounted) answers False, and so
    does a structure whose main feature CAN be picked now (the ambiguous title
    is gone). An answer that cannot be derived at all keeps the prompt —
    silence is not the same as answered, the rule `missing_now` follows too.
    """
    from mlo import videodisc

    key = _key(folder)
    probe = _probe()
    try:
        # The probe is what fills a DVD title's duration (a Blu-ray's comes
        # from its playlist), and without durations there is nothing to
        # compare — so recognition and the pick are asked together.
        disc = (videodisc.recognize(folder, probe)
                or videodisc.disc_image(folder))
    except Exception:
        traceback.print_exc()
        return True
    if disc is None:
        _video_asked.pop(key, None)
        return False
    stamp = _structure_stamp(disc)
    now = time.time()
    hit = _video_asked.get(key)
    if hit and hit[0] == stamp and now - hit[2] < _VIDEO_TTL_S:
        return hit[1]
    try:
        still = videodisc.pick(disc, probe)[0] is None
    except Exception:
        traceback.print_exc()
        still = True
    _video_asked[key] = (stamp, still, now)
    return still


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
    """This album's open entry ({} when there is none).

    Reads ITS entry and asks `_live` about it — the same test the list uses, so
    the two can never disagree — without re-deriving every other album's gap.
    The album page asks this on every load (`GET /api/album`'s `needs`), and an
    album page must not pay a grade for each of the library's other open gaps;
    `prompts` still walks them all for the views that show them.
    """
    if not album_dir:
        return {}
    path = _path(cfg)
    if not path or not os.path.isfile(path):
        return {}
    entry = _load(path).get(_key(album_dir))
    if not _live(entry, cfg):
        return {}
    return dict(entry)


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


# --------------------------------------------------------------------------- #
# A WAIT is not a WARNING
# --------------------------------------------------------------------------- #
def _awaits_answer(entry):
    """Whether this entry is a WAIT — an album a run must leave alone — rather
    than a WARNING on an album that is already finished.

    Two entries are waits. A **video prompt** (`kind == VIDEO_KIND`) stands for
    a disc structure the app could not pick a feature from: the remux it
    belongs to has not happened, so its files are about to change. A **review
    stop** (`reason == "stopped"`) is a person mid-decision in the wizard, with
    the chain not yet run — the half-answered album a sweep must not rewrite.

    A family no source could supply (`reason == "missing"`) is NOT a wait: the
    import ran to its end, the album is in the library and graded like any
    other, and what is left is a warning the queue shows on its finished row
    and the bell announces. Holding such an album back from a library-wide run
    was the app treating a warning as a lock — and it did exactly that until
    the owner hit it: an album imported with a cover missing was skipped by
    Run All with no way to tell it apart from one still being written.
    """
    if not entry:
        return False
    return (entry.get("kind") == VIDEO_KIND
            or str(entry.get("reason") or "") == "stopped")


def parked_entry(album_dir, cfg=None):
    """The prompt standing for ONE album, when it is a WAIT ({} otherwise).

    `prompts()` is the ONE definition of "the gap is still open" — it drops an
    entry whose folder is gone and one whose families have since been supplied
    — so this asks that and forms no second opinion, then asks
    `_awaits_answer` whether the open gap is a wait or a warning. Cheap for the
    caller: one file read plus the re-derivation the list already memoises.
    """
    if not album_dir:
        return {}
    key = _key(album_dir)
    for entry in prompts(cfg):
        if str(entry.get("id") or "") == key:
            return entry if _awaits_answer(entry) else {}
    return {}


def parked_keys(cfg=None):
    """Every album a run must not touch right now, in the store's own key shape.

    Only the WAITS (see `_awaits_answer`): a finished import that is short of a
    family is a normal library album with a warning on it, and `chain_scope`
    below is what would otherwise skip it.
    """
    return frozenset(str(e.get("id") or "") for e in prompts(cfg)
                     if _awaits_answer(e))


def chain_scope(targets, cfg=None):
    """``(kept, dropped, note)`` — the albums a chain run may touch.

    A chain over an album whose import is WAITING on a person is how a
    half-answered release gets half-written: the person is mid-decision in the
    wizard (a review stop), or a disc structure is about to be remuxed. So a
    run that DISCOVERS its own albums — the library-wide sweep (`Run All`, a
    chain with no targets: `server.script_runners.run_chain` collects the whole
    library and every script walks it) — is narrowed to the albums that are not
    waiting, and ``note`` says which were left alone: a skip nobody is told
    about is the same as a silent overwrite.

    What "waiting" means is `parked_keys`' own answer, and it is deliberately
    NOT "has a prompt": an album a finished import reported a missing family
    for is in the library, graded and ordinary — the gap is a warning on its
    queue row and in the bell, not a lock. Skipping it made a warning read as
    an unfinished import to anyone watching a Run All (R166).

    *targets* is the run's own scope (``None``/empty = the sweep), and targets
    come back UNCHANGED whenever nothing is waiting, so a sweep stays a sweep
    and an album-scoped run stays scoped.

    A run that NAMES its albums is deliberately not filtered, whoever started
    it. `/api/import/finish` and the wizard's ticked scripts are the person's
    own press — the way a park gets resolved — and an import (`finish_album`,
    every path of it) is finishing the album it names: its own steps run first
    and rewrite that album either way (`drop_arrived_values` empties the
    families the import is about to decide), so filtering only its chain would
    leave the album emptied and not refilled — the half-write this rule exists
    to prevent. What an import never does is touch an album it was not asked
    about: its chain is scoped to its own folder (R89), and a same-release
    re-download is refused before any of this (R89's identity check).
    """
    parked = parked_keys(cfg)
    if not parked:
        return targets, [], ""
    scope = [str(t) for t in (targets or []) if str(t).strip()]
    if scope:
        return targets, [], ""
    # A sweep. Its scope is the library itself, so what it may not touch is
    # every album inside it that is WAITING — the rest of the library is still
    # swept, which is the difference between "this album is being answered" and
    # "the run does nothing".
    from mlo.stats import _find_albums

    folder = str((cfg or {}).get("music_folder") or "").strip()
    albums = _find_albums(folder) if folder and os.path.isdir(folder) else []
    kept = [a for a in albums if _key(a) not in parked]
    dropped = [a for a in albums if _key(a) in parked]
    if not dropped:
        return targets, [], ""
    note = (f"{len(dropped)} album(s) are waiting on you — left untouched by "
            f"this run: " + ", ".join(os.path.basename(os.path.normpath(d)) or d
                                      for d in dropped[:5])
            + (" …" if len(dropped) > 5 else ""))
    return kept, dropped, note


def raise_prompt(album_dir, cfg, missing, *, mode="automatic", reason="missing"):
    """Store and announce what this album is still missing.

    One call per finished import: *missing* is `mlo.import_policy.gaps`' own
    answer, so what the notification names is exactly what grading fails. An
    empty *missing* clears the album's entry — the import that resolved the
    gaps is the same call that withdraws the prompt.

    What it raises is a WARNING, not a hold: the import has already finished and
    the album is in the library, so nothing about the album waits for the user
    (`_awaits_answer` is the line, and a family gap is on the warning side of
    it). The gap is announced once, listed on the album's own finished queue row
    with the wizard link, and re-derived on read until it is supplied.

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


def _video_note(candidates, reason):
    """The prompt's body: why the app will not choose, and what it could see."""
    listing = "; ".join(str(c).strip() for c in (candidates or []) if str(c).strip())
    if not listing:
        return str(reason or "the disc structure could not be read")
    return f"{reason} — candidates: {listing}" if reason else f"candidates: {listing}"


def raise_video_prompt(folder, cfg, *, candidates=(), reason="", mode="automatic"):
    """Store and announce "which title is the main feature?" for *folder*.

    Raised by script 11 (``mlo.remux``) when a disc structure (VIDEO_TS /
    BDMV) holds more than one plausible feature — or none it can read — and it
    refuses to guess. Same table, same bus and same row shape as
    :func:`raise_prompt`, so the notification bell, ``GET /api/import/prompts``
    and the Soulseek queue's "Needs you" row all carry the question: the
    single family's ``note`` names the candidates and their durations, which is
    what the reader needs to answer it.

    The SAME question (same reason, same candidates) is not repeated on the
    next run: only a changed answer is news. Never raises — a remux that cannot
    write a prompt still reports its own log line.
    """
    try:
        album = os.path.normpath(str(folder))
        note = _video_note(candidates, reason)
        entry = {
            "album": album.replace("\\", "/"),
            "album_name": os.path.basename(album.rstrip("\\/")) or album,
            "at": time.time(),
            "mode": str(mode or "automatic"),
            "reason": str(reason or "video"),
            "kind": VIDEO_KIND,
            # No wizard step decides this one, so the link is the wizard at the
            # album (the album's own page) rather than a family's step.
            "link": _link(album.replace("\\", "/"), []),
            "families": [{
                "id": VIDEO_FAMILY,
                "label": VIDEO_LABEL,
                "state": "decision",
                "note": note,
                "fields": [],
                "codes": [],
            }],
        }
        path = _path(cfg)
        if not path:
            return None
        data = _load(path) if os.path.isfile(path) else {}
        before = data.get(_key(album)) or {}
        same = (before.get("kind") == VIDEO_KIND
                and str(before.get("reason") or "") == entry["reason"]
                and _video_note_of(before) == note)
        data[_key(album)] = entry
        _save(path, data)
        # The next read re-derives whether the question is still open; this
        # call is what just stored the answer it was raised from.
        _forget_verified(album)
        if not same:
            events.emit("import_needs_data", title(entry), body(entry),
                        {"link": entry["link"], "album_path": entry["album"],
                         "reason": entry["reason"],
                         "families": [VIDEO_FAMILY]},
                        config=cfg)
        return entry
    except Exception:
        traceback.print_exc()
        return None


def _video_note_of(entry):
    """The stored body of a video entry — what "the same question" is compared
    by (the candidates and the reason, not the moment it was asked)."""
    for f in (entry or {}).get("families") or []:
        if str(f.get("id") or "") == VIDEO_FAMILY:
            return str(f.get("note") or "")
    return ""
