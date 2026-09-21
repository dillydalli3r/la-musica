"""The 20 library scripts, in one place every caller shares.

Extracted from ``server/main.py``'s ``RUNNERS`` table so the import pipeline
(:mod:`server.imports`), the bulk queue and the Soulseek importer run exactly
the same scripts with exactly the same force-flag semantics as ``/api/run`` —
``RunRequest``'s docstring there is the contract (a *supplied* force dict is
authoritative and complete; an omitted one falls back to the saved Settings).

Every runner takes the whole config dict and honours ``config["targets"]``
(the folders a run is scoped to), which is how one album is finished without
touching the rest of the library.

No script ever raises out of here: a failing script comes back as
``{"id", "error"}`` so a chain can carry on and the import is never lost.
"""
import os
import threading
import traceback

from mlo import (
    run_audit_library, run_auto_tagging, run_format_cues, run_format_lyrics,
    run_grade_library, run_optimize_artist_images, run_optimize_flacs,
    run_process_images, run_scan_layout,
)
from mlo import stats as mlo_stats
from mlo.loudness import run_calc_dr_replaygain
from mlo.paths import (SKIP_DIRS, library_root, load_expected_tracks,
                       prune_empty_dirs, save_expected_tracks)
from mlo.ui import Color, c, log, print_header
from server import job_locks


def _optional(module, name):
    """An entry point whose module may be absent (a stripped checkout).

    Same ImportError guard ``/api/run`` used, so importing this module never
    fails over a script the install does not have.
    """
    try:
        return getattr(__import__(module, fromlist=[name]), name)
    except ImportError:
        return None


# --------------------------------------------------------------------------- #
# Script 15 — Release tracklist (.mlo_expected.json)
# --------------------------------------------------------------------------- #
# Album-level identity tags are uniform across an album's tracks, so a handful
# of files settle it and one unreadable file cannot cost the album its
# manifest (the same bound server.imports._album_mbids uses).
_TRACKLIST_PROBE_FILES = 5


def _album_release_id(album_dir):
    """The album's MusicBrainz id, else its release-group id, else ""."""
    from mlo.audio import AudioFile

    try:
        names = sorted(f for f in os.listdir(album_dir)
                       if mlo_stats.is_audio_file(f))
    except OSError:
        return ""
    album_id = rg_id = ""
    for name in names[:_TRACKLIST_PROBE_FILES]:
        try:
            af = AudioFile(os.path.join(album_dir, name))
            if getattr(af, "audio", None) is None:
                continue
            album_id = album_id or str(af.get_tag("MUSICBRAINZ_ALBUMID") or "").strip()
            rg_id = rg_id or str(af.get_tag("MUSICBRAINZ_RELEASEGROUPID") or "").strip()
        except Exception:
            continue
        if album_id:
            break
    return album_id or rg_id


def run_release_tracklist(config):
    """Script 15 — write each album's release tracklist manifest.

    Grading REQUIRES ``.mlo_expected.json`` (grade_check_expected_tracks): the
    files on disk only describe themselves, so nothing else can say whether a
    partially imported album was meant to hold more tracks. An import records
    the manifest from the release it imported; an album that arrived any other
    way (beets, a hand-placed rip, a library from before this existed) carries
    only the MusicBrainz id in its tags — so the tracklist is fetched from
    that id, through the app's own MusicBrainz access (server.integrations;
    no new HTTP path, and its 1 req/s etiquette means a whole-library run
    costs one rate-limited request per album).

    No id on the album means NO manifest: one line says so. A fabricated
    tracklist would fail the album forever for tracks that release never had.
    A manifest already present is left alone unless the force flag is set.
    """
    from mlo.grader import _relpath_guard
    from mlo.paths import EXPECTED_TRACKS_FILE
    from server import integrations

    config = config or {}
    stats = mlo_stats.new_stats()
    print_header("Release tracklist (.mlo_expected.json)")

    folder = str(config.get("music_folder") or "")
    force = bool(config.get("force_tracklist", False))
    log(f"music folder: {folder} · "
        f"existing manifests: {'rewritten (force)' if force else 'kept'}")

    if config.get("targets") is not None:
        files = mlo_stats._collect_targets(config["targets"], mlo_stats.LIB_AUDIO_EXTS)
        albums = [d for d in sorted({os.path.dirname(f) for f in files})
                  if os.path.isdir(d)]
    elif os.path.isdir(folder):
        albums = mlo_stats._find_albums(folder)
    else:
        albums = []
    if not albums:
        log("No albums found.")
        return stats

    counts = {"ok": 0, "skip": 0, "fail": 0}
    pbar = mlo_stats._make_pbar(len(albums), "Release tracklist", unit="album")
    for album in albums:
        rel = _relpath_guard(album, folder or album)
        if not force and load_expected_tracks(album)["tracks"]:
            stats["skipped_count"] += 1
            mlo_stats._pbar_skip(pbar, counts)
            continue

        mbid = _album_release_id(album)
        if not mbid:
            log(f"{rel}: no MUSICBRAINZ_ALBUMID (or RELEASEGROUPID) on its "
                f"tracks — no manifest written")
            stats["skipped_count"] += 1
            mlo_stats._pbar_skip(pbar, counts)
            continue

        try:
            release, release_id = integrations.resolve_release(mbid)
        except Exception as e:
            log(c(f"{rel}: MusicBrainz lookup failed — {e}", Color.YELLOW))
            stats["error_count"] += 1
            stats["errors"].append((album, str(e)))
            mlo_stats._pbar_update(pbar, counts, kind="fail")
            continue

        tracks = (release or {}).get("media") or []
        if not tracks:
            log(f"{rel}: MusicBrainz holds no tracklist for {mbid} — "
                f"no manifest written")
            stats["skipped_count"] += 1
            mlo_stats._pbar_skip(pbar, counts)
            continue

        rid = release_id or mbid
        if save_expected_tracks(album, rid, tracks):
            log(f"{rel}: {len(tracks)} track(s) recorded from release {rid}")
            stats["modified_count"] += 1
            mlo_stats._pbar_update(pbar, counts)
        else:
            log(c(f"{rel}: could not write {EXPECTED_TRACKS_FILE}", Color.YELLOW))
            stats["error_count"] += 1
            stats["errors"].append((album, "manifest write failed"))
            mlo_stats._pbar_update(pbar, counts, kind="fail")

    if pbar:
        pbar.close()
    log(c(f"release tracklist: {stats['modified_count']} written · "
          f"{stats['skipped_count']} skipped · "
          f"{stats['error_count']} failed", Color.GREEN if not stats["error_count"]
          else Color.YELLOW))
    return stats


# id -> (label, runner). Labels are the ones web/src/lib/scripts.ts renders,
# id for id; ids and names must stay in step with README.md and the frozen
# EXPECTED_SCRIPTS in tools/test_script_menus.py.
RUNNERS: dict[int, tuple[str, "callable"]] = {
    1: ("Format lyrics", run_format_lyrics),
    2: ("Format CUEs", run_format_cues),
    3: ("Optimize FLACs", run_optimize_flacs),
    4: ("Grade", run_grade_library),
    5: ("Process images", run_process_images),
    6: ("Audit library", run_audit_library),
    7: ("DR & ReplayGain", run_calc_dr_replaygain),
    8: ("Auto Tagging", run_auto_tagging),
    9: ("AccurateRip", _optional("mlo.accurip", "run_generate_accurip")),
    10: ("Format all", _optional("mlo.format_all", "run_format_all")),
    11: ("Remux videos (MKV)", _optional("mlo.remux", "run_remux_videos")),
    12: ("Key & BPM", _optional("mlo.audiometa", "run_analyze_audiometa")),
    13: ("Fetch lyrics", _optional("mlo.lyrics_fetch", "run_fetch_lyrics")),
    14: ("Beets tagging", _optional("server.beetscfg", "run_beets_tagging")),
    15: ("Release tracklist", run_release_tracklist),
    16: ("Mood & Energy", _optional("mlo.moods", "run_detect_mood_energy")),
    # 17 is the AI pass: it reads the lyrics script 13 stored and writes
    # TRANSLITERATION-*/TRANSLATION-* tags (and sidecars). No AI configured =
    # one log line, no failure.
    17: ("Lyrics transliterate (AI)", _optional("mlo.lyrics_xlit", "run_lyrics_xlit")),
    # 18 gives back: this library's lyrics go to LRCLIB for recordings the
    # database does not have yet. Off = `lrclib_auto_publish` is off.
    18: ("Publish lyrics (LRCLIB)", _optional("mlo.lyrics_publish", "run_publish_lyrics")),
    # 19 re-fits the artist images already in the library to the configured
    # aspect/size (mlo.artistdata's own runner — the same policy the fetch and
    # the grading check use).
    19: ("Optimize artist images", run_optimize_artist_images),
    # 20 is the ONLY read-only script: it reports the shape of the music
    # folder and stores that report under <music>/.mlo/data, which is what the
    # Library page warns from. No force flag — there is nothing to overwrite.
    20: ("Scan library layout", run_scan_layout),
    # 21 completes an AcoustID PAIR a file only half carries. That pair is a
    # grading check of its own (grade_check_acoustid), and no other script
    # could clear it, so this is the fixer the Grading page's failure points
    # at. No force flag: its subject IS the incomplete pair, so a file holding
    # both halves is deliberately left alone.
    21: ("Fix AcoustID pairs", _optional("mlo.acoustid", "run_fix_pairs")),
}

# The config key a script's own force flag lives under. `force` may be keyed by
# script id ("5") or by these UI names ("images") — both reach the same key.
_FORCE_KEYS = {
    1: ("force_lyrics",),
    2: ("force_cue",),
    3: ("force_reencode_flac",),
    5: ("force_reencode_images",),
    6: ("force_audit",),
    7: ("force_dr_replaygain",),
    8: ("force_auto_tag",),
    9: ("force_accurip",),
    # Format all re-runs whatever is forced above; forcing it forces them.
    10: ("force_accurip", "force_cue", "force_lyrics", "force_auto_tag"),
    12: ("force_audiometa",),
    13: ("force_lyrics",),
    # 15 rewrites a manifest that already exists (otherwise it is left alone).
    15: ("force_tracklist",),
    # 16 re-analyses every track, tagged or not — the same flag script 8's
    # mood stage answers to.
    16: ("force_mood",),
    # 17 re-transforms tracks that already carry a stored transform.
    17: ("force_xlit",),
    # 18 re-submits lyrics for tracks LRCLIB already answers for.
    18: ("force_publish",),
}
_FORCE_ALIASES = {
    "lyrics": "force_lyrics",
    "cue": "force_cue",
    "flac": "force_reencode_flac",
    "images": "force_reencode_images",
    "audit": "force_audit",
    "dr": "force_dr_replaygain",
    "autotag": "force_auto_tag",
    "accurip": "force_accurip",
    "audiometa": "force_audiometa",
    "tracklist": "force_tracklist",
    "mood": "force_mood",
    "xlit": "force_xlit",
    "publish": "force_publish",
}
# Scripts whose feature has its own on/off switch: with it off the runner is a
# no-op at best and a crash at worst, so a chain skips them instead. A tuple
# means the script runs while ANY of the switches is on (17 does
# transliteration, translation, or both).
_DISABLED = {
    7: "dr_replaygain_enabled",
    12: "audiometa_enabled",
    16: "mood_enabled",
    17: ("lyrics_xlit_enabled", "lyrics_translate_enabled"),
    18: "lrclib_auto_publish",
    # 21 writes ACOUSTID_* tags, and the switch is the app's own answer to
    # "should AcoustID do anything here" — the same one its lookup helper
    # refuses on. Without it a chain would keep fingerprinting files for a
    # feature the user turned off (the runner refuses too; this is what makes
    # the run report say WHY it did nothing).
    21: "acoustid_enabled",
}


def _apply_force(cfg, force, sid=None):
    """Copy a supplied force selection into the cfg force flags.

    *sid* scopes the write to one script (a single run_script call); None
    applies every entry (one chain). A supplied dict is authoritative AND
    complete: every force flag it does not mention is turned OFF first, so a
    UI that sends only the checked keys cannot leave a saved-on force flag
    forcing a script the user just unchecked. A bare True/False turns this
    script's own flag on/off.
    """
    if force is None:
        return
    if isinstance(force, bool):
        for key in _FORCE_KEYS.get(sid, ()):
            cfg[key] = force
        return
    # Authoritative dict: clear first, then apply what was supplied.
    if sid is None:
        for keys in _FORCE_KEYS.values():
            for key in keys:
                cfg[key] = False
    else:
        for key in _FORCE_KEYS.get(sid, ()):
            cfg[key] = False
    for raw, value in force.items():
        alias = _FORCE_ALIASES.get(str(raw).strip().lower())
        if alias is not None:
            if sid is None or alias in _FORCE_KEYS.get(sid, ()):
                cfg[alias] = bool(value)
            continue
        try:
            ids = [int(raw)]
        except (TypeError, ValueError):
            continue
        if sid is not None and sid not in ids:
            continue
        for i in ids:
            for key in _FORCE_KEYS.get(i, ()):
                cfg[key] = bool(value)


def _run_with_progress(runner, cfg, label, chain=None):
    """Run *runner* with the UI's progress bar pointed at this script.

    Every runner builds its own tqdm-style bar, and that bar is what the UI
    header follows (``mlo.stats.progress_hook`` → ``server/main.py``'s relay →
    the WebSocket the front-end draws). Two of them have no bar at all
    (AccurateRip, Beets tagging) and the rest return early WITHOUT creating
    one whenever the album holds nothing for them (no CUE file, no video,
    every track already key/BPM-tagged, no image to optimize) — their runtime
    then showed the previous script's numbers, or a bar frozen on the last
    file, with no way to tell a slow script from a stuck one.

    So the script owns the header for its whole run: the runner's own ticks
    are forwarded (under the script's UI label, so the header names the script
    the way the menus do rather than the runner's internal "FLAC"/"Grading"),
    it is announced before the work starts, and completed afterwards when the
    runner reported nothing on its own.

    *chain* is ``(index, count)`` — this script's 1-based place in a multi-step
    run, and how many steps that run has. It is what makes the header honest
    for Run All: ``done`` becomes "finished scripts + this script's own
    fraction" and ``total`` the scripts in the run, so the numbers run from
    0 to count across the whole chain. Without it each script reported ITS OWN
    counts and its own completion, which is exactly what a chain must not do:
    every script boundary reset the numbers to zero and then declared the bar
    finished (`done >= total`), so the header flipped to indeterminate and the
    front-end cleared it while the run was still going.

    ponytail: one process-wide hook swapped for the duration of the call —
    script runs are serialized by RUN_LOCK, so nothing else can observe the
    swap, and the original hook is restored in `finally` even on a raise.
    """
    prior = getattr(mlo_stats, "progress_hook", None)
    if not callable(prior):
        return runner(cfg)          # a headless caller (CLI/tests): no UI bar
    index, count = chain or (0, 0)
    chained = count > 1
    # How far into its own slice this script has been seen to be. Monotonic:
    # a runner that draws two bars in a row (albums, then files) restarts its
    # counts at the second one, and a chain bar that steps backwards reads as
    # a stall.
    span = [0.0]
    # The last (done, total) the runner reported on its own, for the
    # single-script completion below. Two slots instead of the list of ones
    # this used to append per file — that list only ever answered "did the
    # runner tick at all" and grew once per file.
    last = [0, 0]

    def emit(done, total, text, steps=None):
        """One frame out. *steps* is the chain's whole-script pair, sent
        beside the fractional position: the bar draws the fraction, the
        readout prints "3/18" with no decimal in it. A hook that takes only
        the three-argument frame (a test double, a curl-era relay) still gets
        its numbers."""
        frames = [(done, total, text)] if steps is None else [
            (done, total, text, steps), (done, total, text)]
        for frame in frames:
            try:
                prior(*frame)
                return
            except TypeError:
                continue
            except Exception:
                return

    def hook(done, total, detail):
        last[0], last[1] = done, total
        # The runner's own description when it has one, behind the step's
        # position in the run: "…· Beets tagging — MusicBrainz lookup". A long
        # step that prints nothing (beets' lookup phase) then still shows the
        # user WHERE in the run they are instead of only a step name.
        text = f"#{index}/{count} · {detail}" if detail and chained else label
        if not chained:
            emit(done, total, text)
            return
        try:
            frac = float(done) / float(total) if total else span[0]
        except (TypeError, ValueError, ZeroDivisionError):
            frac = span[0]
        span[0] = frac = max(span[0], min(1.0, frac))
        # The bar follows the fraction; the readout prints #index/count, so a
        # half-finished step never reads as "1.9 of 18".
        emit(index - 1 + frac, count, text, (index, count))

    mlo_stats.progress_hook = hook
    try:
        if chained:
            # Claim the bar at this script's slice — determinate from the very
            # first frame, so the header never falls back to the sweep between
            # two steps of a run that is still going.
            emit(index - 1, count, f"#{index}/{count} · {label}", (index, count))
        else:
            # Claim the bar before the first file. Announced with NO total on
            # purpose: a runner that never ticks (AccurateRip's CUETools pass,
            # Beets' single `beet import`) would otherwise sit at "0/1" — an
            # empty bar for the whole run, which reads as hung. With total 0 the
            # header draws the indeterminate sweep instead, and the first real
            # tick (or the finally below) turns it into live numbers.
            emit(0, 0, label)
        return runner(cfg)
    finally:
        if chained:
            # This script is done: its whole slice is behind us whatever it
            # reported on its own (a no-op script still consumed a step).
            emit(index, count, f"#{index}/{count} · {label}", (index, count))
        elif not last[1] or last[0] < last[1]:
            # Whatever this runner reported, it is over: either it never
            # opened a bar (AccurateRip, Beets), or its own bar is still short
            # of its total (a run that stopped early, a sub-bar left open).
            # Both must finish the header rather than leave it looking hung.
            emit(1, 1, label)
        mlo_stats.progress_hook = prior


def run_script(sid, cfg, targets=None, force=None, skip_disabled=True, chain=None):
    """Run one script against *cfg* and report what happened.

    *chain* ``(index, count)`` is passed by :func:`run_chain` so a step of a
    multi-script run reports against the WHOLE run's bar; a lone caller leaves
    it None and the script's own counts are the bar (see
    :func:`_run_with_progress`).

    Returns ``{"id", "name", "label", "stats"}``, ``{"id", ..., "skipped": True,
    "reason"}`` for a script whose feature is switched off, or ``{"id",
    "error"}`` — this function never raises.
    """
    if targets is not None:
        cfg["targets"] = [os.path.normpath(str(t)) for t in targets]
    _apply_force(cfg, force, sid)

    label, runner = RUNNERS.get(sid, (None, None))
    if runner is None:
        return {"id": sid, "error": f"runner {sid} not available"}

    gate = _DISABLED.get(sid)
    if skip_disabled and gate:
        keys = gate if isinstance(gate, tuple) else (gate,)
        if not any(cfg.get(k, True) for k in keys):
            joined = " and ".join(keys)
            return {"id": sid, "name": getattr(runner, "__name__", ""), "label": label,
                    "skipped": True,
                    "reason": f"{joined} {'are' if len(keys) > 1 else 'is'} off"}
    try:
        return {"id": sid, "name": getattr(runner, "__name__", ""),
                "label": label, "stats": _run_with_progress(runner, cfg, label, chain)}
    except Exception as e:
        traceback.print_exc()
        return {"id": sid, "name": getattr(runner, "__name__", ""),
                "label": label, "error": str(e)}


class RunBusy(RuntimeError):
    """Another run holds the library lock.

    Scripts mutate the library in place, so two overlapping runs (a UI Run
    All and an import chain, or two imports) must never touch the same album
    at once: one would re-encode while the other renames or deletes. Every
    entry point — `/api/run`, `imports.finish_album`, the bulk queue, the
    Soulseek importer — funnels through `run_chain`, which is where the lock
    lives.
    """


# One run at a time, process-wide.
RUN_LOCK = threading.Lock()


def held_paths(cfg, targets):
    """The library paths a run is about to touch, for the lock registry.

    A scoped run holds its own targets. An unscoped one — Run All, an import
    chain with no target — reads and rewrites whatever it finds, so it holds
    the library root itself: that is the claim a delete, a move or a tag write
    is refused against while the scripts are running.
    """
    scope = targets if targets is not None else cfg.get("targets")
    paths = [os.path.normpath(str(t)) for t in (scope or []) if str(t).strip()]
    if paths:
        return paths
    folder = str(cfg.get("music_folder") or "").strip()
    root = library_root(folder) if folder else None
    return [root] if root and os.path.isdir(root) else []


def run_label(ids):
    """The run's name in MAINTAIN → In progress: the first script, plus how
    many follow it when the caller asked for a chain."""
    names = [RUNNERS.get(i, (f"Script {i}", None))[0] for i in ids]
    if not names:
        return "Script run"
    return names[0] if len(names) == 1 else f"{names[0]} + {len(names) - 1} more"


def _job_progress(job, progress):
    """Forward each finished script to the caller AND to the lock registry, so
    the in-progress list shows which step of the run is going."""
    def report(done, total, label, result):
        job_locks.set_progress(job, done, total, label)
        if progress is not None:
            progress(done, total, label, result)
    return report


def run_chain(cfg, ids, targets=None, force=None, progress=None, wait=False,
              timeout=None):
    """Run *ids* in order against a COPY of *cfg*; report after every script.

    ``progress(done, total, label, result)`` is called once per finished
    script. A script that fails is recorded in the returned list and the chain
    carries on — one bad script must never cost the import the rest of the
    pipeline. An id the registry does not know is a failure entry, not a stop.

    *wait* decides what happens when another chain holds the lock:

    * ``wait=False`` (the API's one-shot runs) raises `RunBusy` immediately —
      the caller answers 409 rather than queueing behind a long run.
    * ``wait=True`` (imports) blocks until the lock frees, because an import
      must not silently skip its chain: the album is already on disk and the
      user asked for it to be finished. ``timeout`` (default 1 h) bounds the
      wait so a wedged run cannot hold an import forever.

    The run claims the paths it works on in `server.job_locks` under the same
    *wait* rule: a delete, a tag write or an organize landing on a folder these
    scripts are rewriting is refused (409) instead of racing them. The claim
    ends with the run — including when it raises or is cancelled.

    A chain is also refused outright while the app is shutting down for an
    auto-update (see :mod:`server.interrupt_recovery`): every script and every
    import chain funnels through here, so this one check is what stops the
    shutdown from STARTING work it would then have to kill.
    """
    from server import interrupt_recovery
    if interrupt_recovery.is_shutting_down():
        raise RunBusy("the app is shutting down for an update — nothing can "
                      "start now; retry when it is back up")
    acquired = RUN_LOCK.acquire(blocking=False) if not wait else \
        RUN_LOCK.acquire(timeout=3600 if timeout is None else timeout)
    if not acquired:
        raise RunBusy("a script run is already in progress")
    try:
        with job_locks.holding(held_paths(cfg, targets), kind="scripts",
                               label=run_label(ids), wait=wait,
                               timeout=timeout) as job:
            return _run_chain_locked(cfg, ids, targets=targets, force=force,
                                     progress=_job_progress(job, progress))
    finally:
        RUN_LOCK.release()


def _prune_empty_target_dirs(cfg):
    """Remove the folders the run emptied, from each target up to the library.

    An organize or a removal takes the last file out of a disc or album folder
    and leaves the empty shell behind, so after the scripts have run the
    target is swept: its own subtree first (the disc folder that just went
    empty), then the folders above it that are now empty too. Only the run's
    OWN targets are swept, and only below the music folder — the root itself
    and everything outside the library is never a candidate — so a chain that
    ran no scripts cannot walk (let alone empty) the whole library.

    Best-effort by design: this is housekeeping AFTER the scripts, and a
    permission error must never turn a finished run into a failed one.
    Returns the removed paths, deepest first."""
    targets = cfg.get("targets") or []
    mf = str(cfg.get("music_folder") or "").strip()
    if not mf or not targets:
        return []
    prefix = os.path.normcase(os.path.abspath(mf)) + os.sep
    removed = []
    for raw in targets:
        d = os.path.abspath(str(raw))
        if not os.path.isdir(d):
            d = os.path.dirname(d)
        if not os.path.normcase(d).startswith(prefix):
            continue
        removed += prune_empty_dirs(d)
        # The target's own chain, deepest first: a folder still holding a file
        # (or an unremovable child) stops the climb, because nothing above a
        # non-empty folder can be empty either.
        while os.path.normcase(d).startswith(prefix):
            if os.path.basename(d).startswith(".") or os.path.basename(d) in SKIP_DIRS:
                break
            try:
                os.rmdir(d)
            except OSError:
                break
            removed.append(d)
            d = os.path.dirname(d)
    return removed


def _audio_basenames(folder):
    """Lower-cased audio file names directly inside *folder* (a set)."""
    try:
        return {f.name.lower() for f in os.scandir(folder)
                if f.is_file() and mlo_stats.is_audio_file(f.name)}
    except OSError:
        return set()


def _has_audio(folder):
    """True when *folder* holds at least one audio file directly inside it.

    The chain only ever asks this as a yes/no (a target counts as "vanished"
    once no audio is left in it), and it asks it for every target after every
    script — so this stops at the first hit instead of listing the whole
    directory: an album folder holds more covers, .cue and .log than tracks,
    and the audio files are usually the first entries scandir hands over.
    """
    try:
        for f in os.scandir(folder):
            if f.is_file() and mlo_stats.is_audio_file(f.name):
                return True
    except OSError:
        pass                # gone, unreadable, or not a directory at all
    return False


def _find_moved_album(names, music_folder):
    """The library folder that holds *names* among its audio, else "".

    A subset, not equality: a folder that already held files of the same
    names (a second rip of the same album, a re-import) leaves the moved
    tracks beside them as "… (2).flac", so the album still has to be
    recognised by the names it brought with it.
    """
    if not names or not music_folder or not os.path.isdir(music_folder):
        return ""
    skip = {d.lower() for d in SKIP_DIRS}
    for dirpath, dirs, files in os.walk(music_folder):
        dirs[:] = [d for d in dirs
                   if not d.startswith(".") and d.lower() not in skip]
        found = {f.lower() for f in files if mlo_stats.is_audio_file(f)}
        if names <= found:
            return dirpath
    return ""


def _follow_moved_targets(cfg, audio_names):
    """Re-point the chain at an album a script moved, and never lose it silently.

    Script 14 (beets) rewrites the tags and applies the naming script, so an
    album whose folder was not already canonical comes out somewhere else —
    and the rest of the chain is scoped to the path the import started with.
    A vanished target then made every later script a no-op: Format all said
    "No files found to format.", Grade said "No albums found.", both with
    zero stats and no error, so the album was left unformatted and ungraded
    as if the scripts had run and found nothing to do.

    Identity, not name: an album's audio file names travel with it, so the
    folder that now holds the vanished target's whole audio set is the album.
    A move that also renamed every file cannot be followed that way — then
    the target stays put and the chain says so out loud instead of printing a
    cheerful "nothing to do".

    "Vanished" means the target holds no audio any more, not that the
    directory is gone: beets only takes the audio, so the staging folder is
    still there afterwards, holding the covers / .cue / .log it left behind —
    which is exactly the empty-of-audio folder that made the tail a no-op.
    """
    targets = cfg.get("targets") or []
    out = []
    for t in targets:
        if os.path.isfile(t) or _has_audio(t):
            out.append(t)
            continue
        names = audio_names.get(t) or set()
        if not names:                   # nothing of ours was there to follow
            out.append(t)
            continue
        moved = _find_moved_album(names, str(cfg.get("music_folder") or ""))
        if moved:
            log(f"Album moved: {t} → {moved}; the rest of the chain follows it")
            audio_names[moved] = names
            out.append(moved)
        else:
            log(f"WARNING: no audio left in {t} and the album could not be "
                f"found in the library — the scripts after this one have "
                f"nothing to run on")
            out.append(t)
    cfg["targets"] = out


def _run_chain_locked(cfg, ids, targets=None, force=None, progress=None):
    cfg = dict(cfg)
    if targets is not None:
        cfg["targets"] = [os.path.normpath(str(t)) for t in targets]
    _apply_force(cfg, force)

    results = []
    total = len(ids)
    # Each target's audio, taken before any script runs: the only way to
    # recognise the album again once a script has moved its folder.
    audio_names = {t: _audio_basenames(t) for t in (cfg.get("targets") or [])}
    for done, sid in enumerate(ids, 1):
        result = run_script(sid, cfg, chain=(done, total))
        results.append(result)
        _follow_moved_targets(cfg, audio_names)
        if progress is not None:
            label = RUNNERS.get(sid, (f"Script {sid}", None))[0]
            try:
                progress(done, total, label, result)
            except Exception:
                traceback.print_exc()
    if ids:
        try:
            removed = _prune_empty_target_dirs(cfg)
        except Exception:
            traceback.print_exc()
            removed = []
        if removed:
            log(f"Removed {len(removed)} empty folder(s) left by the run: "
                + ", ".join(removed))
        # The run is over: whatever it did to tags, filenames or folders, the
        # Soulseek network is still serving its boot-time view of them until
        # slskd re-indexes. Debounced there, so a chain of scripts asks once.
        try:
            from server import soulseek
            soulseek.refresh_shares_soon()
        except Exception:
            traceback.print_exc()
    return results
