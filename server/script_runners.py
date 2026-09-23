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
import contextlib
import os
import threading
import traceback

from mlo import (
    run_audit_library, run_auto_tagging, run_format_cues, run_format_lyrics,
    run_grade_library, run_optimize_artist_images, run_optimize_flacs,
    run_process_images, run_optimize_layout,
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
            # resolve_release answers (None, mbid) both when MusicBrainz
            # matched nothing and when the id named a release group with no
            # edition this app can use — two different facts, and the wrong
            # one sends the user looking for a tracklist that exists.
            # (The refusal wording is integrations' own, so the log and the
            # auto-import's `last_error` say the same thing.)
            if release is None:
                why = f"{mbid}: {integrations._NO_EDITION}"
            else:
                why = f"MusicBrainz holds no tracklist for {mbid}"
            log(c(f"{rel}: {why} — no manifest written", Color.YELLOW))
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
    # 20 reports the shape of the music folder and stores that report under
    # <music>/.mlo/data, which is what the Library page warns from; it can
    # also apply what it finds (rename wrong-case names, gather loose audio)
    # under `layout_apply`, which is its force key — the report is written
    # either way, so the run never becomes a no-op.
    20: ("Optimize library layout", run_optimize_layout),
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
    # 16 re-analyses every track, tagged or not. Its OWN flag: script 8's mood
    # stage decides with `force_auto_tag` (it is one stage of the AutoTag pass,
    # and "8 · AutoTag re-run" is what re-runs it), so ticking 16 re-times the
    # MOOD/ENERGY of the library without also re-running the advisory and
    # instrumental work of script 8 — and unticking it leaves that stage alone.
    # The two flags meet in `mlo/moods.apply_mood_tags`, never in one key.
    16: ("force_mood",),
    # 17 re-transforms tracks that already carry a stored transform.
    17: ("force_xlit",),
    # 18 re-submits lyrics for tracks LRCLIB already answers for.
    18: ("force_publish",),
    # 20's apply phase (mlo/layout.py) — the one force key that turns work OFF:
    # the scan always reports, and `layout_apply` is what lets it rename and
    # move. Clearing it is what a caller asks for when it wants the read-only
    # report the script used to be, so the wizard's `{}` and a saved force
    # selection (neither names this key) both leave the library alone.
    20: ("layout_apply",),
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
    "layout": "layout_apply",
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
        # A bare flag means "this script, on/off" for a single run and "the
        # whole chain" for a chain — `_run_chain_locked` applies force once with
        # sid=None, where "this script's own flag" has no single answer. That
        # case used to be a silent no-op (`_FORCE_KEYS.get(None, ())` is
        # empty), so a `run_chain(..., force=True)` forced nothing at all while
        # reporting the run as forced.
        scopes = _FORCE_KEYS.values() if sid is None else (_FORCE_KEYS.get(sid, ()),)
        for keys in scopes:
            for key in keys:
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


# --------------------------------------------------------------------------- #
# The UI header bar: one line, one run painting it
# --------------------------------------------------------------------------- #
# A runner's own bar reaches the UI header through ONE process-wide hook
# (``mlo.stats.progress_hook`` → server/main.py's relay → the WebSocket the bar
# is drawn from), and a script used to REPLACE that hook while it ran so the
# runner's ticks came out labelled as this run's step. Two chains were
# impossible then (one process-wide run lock), so a plain install/restore was
# enough and nothing had to say which run a frame belonged to. Chains over
# disjoint albums may now overlap, so the installed hook is one dispatcher and
# the THREAD a frame arrives on is what identifies its run: a run is registered
# for the whole chain (every runner ticks its bar from the thread that called
# it, and the chain's own step announcements come from that thread too), never
# for one script — a step that never entered its runner still has to move the
# header, and a frame published between two scripts has to stay this run's.
#
# Only the run that has been in flight longest paints the header: the bar is a
# single line, and two albums' numbers interleaved in it would be a lie about
# both. Every run keeps writing its own in-progress ROW, which is keyed by job
# (``job_locks.publish(job=…)``) and therefore never needs guessing.
#
# honey: one run per thread. A chain started from inside another chain's script
# would take that thread's slot (`run_chain` is called by `/api/run`, an import
# and the bulk queue — each from its own thread — so nothing does).
_bars: dict[int, dict] = {}       # chain thread id -> that run's bar context
_bars_lock = threading.Lock()
_bar_base = None                  # the hook installed before the first run claimed the bar


def _bar_dispatch(done, total, desc):
    """Send one runner frame to the run whose thread it arrived on.

    A frame from a thread no run is registered on — a runner that lets a pool
    thread tick a bar shared with it — is DROPPED: with two runs in flight
    there is nothing that says whose it was, and a number painted onto the
    wrong run's bar (or row) is worse than a missing tick.
    """
    with _bars_lock:
        ctx = _bars.get(threading.get_ident())
    emit = (ctx or {}).get("emit")
    if emit is not None:
        emit(done, total, desc)


# Producers that are not a chain itself (the bulk importer's own mirror, a
# download job's ticks) check the INSTALLED hook for this mark and stand down
# rather than paint their numbers into a chain's bar.
_bar_dispatch._mlo_chain = True


def _bar_context():
    """A fresh bar context — one per RUN (see run_chain), not per script."""
    return {"joined": False, "ident": None, "emit": None, "publish": None}


def _bar_join(bar):
    """Put this run on the header bar (once), installing the dispatcher if it
    is the first run in flight.

    A run joins when it starts, not when its first script has a bar of its own:
    its thread is then its own for the whole chain, so the zero frame it claims
    the surfaces with — and the frame a step publishes between two scripts —
    are attributed to it rather than to whichever run happens to be registered.

    The hook the dispatcher replaces is remembered ONCE, by the first run in
    flight, and restored by the last one out. Capturing it per script (what the
    single-run swap did) is what would hand a second, overlapping chain the
    first chain's own wrapper as its "relay": its frames would be re-labelled
    as the other run's step, and once both were done the relay stayed replaced
    by a wrapper whose run had finished.
    """
    global _bar_base
    with _bars_lock:
        if bar.get("joined"):
            return
        if not _bars:
            _bar_base = getattr(mlo_stats, "progress_hook", None)
            mlo_stats.progress_hook = _bar_dispatch
        bar["joined"] = True
        bar["ident"] = threading.get_ident()
        _bars[bar["ident"]] = bar


def _bar_attach(bar, emit, publish):
    """Point this run's slot at the script that is running now.

    *emit* re-labels a runner's own tick as this script's step; *publish* is
    the run's frame writer (`job_locks.publish` with this run's job and the bar
    relay it is entitled to), which is what a frame from the CHAIN itself — a
    step that never entered its runner, the run's opening frame — goes through.
    """
    _bar_join(bar)
    with _bars_lock:
        bar["emit"] = emit
        bar["publish"] = publish


def _bar_release(bar):
    """Take this run off the bar; the last run out puts the relay back.

    Safe on a run that never attached (a headless caller has no bar to give up)
    and safe to call twice."""
    global _bar_base
    with _bars_lock:
        if bar.get("joined") and _bars.get(bar["ident"]) is bar:
            del _bars[bar["ident"]]
        was = bar.get("joined")
        bar["joined"] = False
        if was and not _bars:
            mlo_stats.progress_hook = _bar_base


def _bar_relay(bar):
    """The hook this run may paint the header bar with.

    The run in flight longest owns the bar: the header follows it from its
    first frame to its last instead of switching between two runs, and the
    others get ``_no_bar`` — their frames still reach their own in-progress
    row, which is the surface that names one run at a time. A missing or
    non-callable base (a suite running chains with no relay at all) is no bar
    for anyone, which is also what keeps a frame from being handed back to the
    dispatcher that called it (the recursion this used to swallow).

    A run with NO bar at all — `_run_chain_locked` entered directly, which is
    the shutdown path (a run stopped before it ever claimed a bar) — is the
    same answer, not an error: the frame it is publishing still belongs to its
    own row.
    """
    bar = bar or {}
    with _bars_lock:
        owner, base = next(iter(_bars), None), _bar_base
    if bar.get("joined") and bar.get("ident") == owner and callable(base):
        return base
    return _no_bar


def _bar_frame(bar, done, total, text, job=None, steps=None):
    """One frame from the RUN itself, not from a script's bar.

    A step that never entered its runner (a switched-off feature, an
    unavailable module) and the zero frame a run claims its surfaces with both
    publish this way: through the run's own frame writer once a script has
    given it one — attributed to this run and painted only if it owns the bar —
    and, before that, straight to the registry with the relay this run is
    entitled to (never to the installed hook, which is the dispatcher that
    would look for a script of ours that does not exist yet).

    The in-progress row is written either way: it is keyed by job, so a run
    that does not own the bar still shows its own zero state and its own steps.
    A run with no bar AND no job (`_run_chain_locked` entered directly) has no
    surface left to write into and simply publishes nothing.
    """
    bar = bar or {}
    publish = bar.get("publish")
    if publish is not None:
        publish(done, total, text, steps)
        return
    job_locks.publish(done, total, text, steps, job=job, hook=_bar_relay(bar))


def _no_bar(*_args, **_kwargs):
    """The hook that goes nowhere: this run has no header bar to paint."""
    return None


def _run_with_progress(runner, cfg, label, chain=None, job=None, bar=None):
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

    ponytail: the header bar is ONE process-wide hook, so it is painted by one
    run at a time — see the dispatch block above for how two overlapping chains
    share it without ever showing each other's numbers; the in-progress row is
    per run and always this run's.
    """
    prior = getattr(mlo_stats, "progress_hook", None)
    # The run this script belongs to, resolved HERE (the chain's own context)
    # rather than per frame: the runner's bar is ticked by the pool threads
    # inside it, and those threads are not inside the job. None for a lone
    # script run outside a job (the CLI, the suites).
    job = job_locks.current()
    if not callable(prior) and job is None:
        return runner(cfg)          # a headless caller (CLI/tests): no UI bar
    # A chain hands in its run's bar (run_chain); a lone script (a caller of
    # `run_script` on its own) gets one for the length of this call.
    lone = bar is None
    if lone:
        bar = _bar_context()
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
        """One frame out, to the header bar AND to this run's in-progress row
        (job_locks.publish) — the two surfaces read the same frame or the run
        reads as two different steps depending on where you look. *steps* is
        the chain's whole-script pair, sent beside the fractional position:
        the bar draws the fraction, the readout prints "3/18" with no decimal
        in it. The bar's relay is asked for explicitly (`_bar_relay`), never
        taken from the installed hook: that hook is the dispatcher which called
        this very function, so handing a frame back to it would feed the run
        its own text."""
        job_locks.publish(done, total, text, steps, job=job, hook=_bar_relay(bar))

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

    # The installed hook is the chain dispatcher (marked so a producer that is
    # NOT a chain — the bulk importer's own mirror, a download job's ticks —
    # can tell that a header bar is already speaking for a run and stand down
    # instead of painting its frames into it): this script hands its bar and its
    # frame writer to the run's context, which is what tells two overlapping
    # chains apart and keeps the chain's own step frames this run's.
    _bar_attach(bar, hook, emit)
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
        # A chain releases its bar once, at the end of the RUN (run_chain): the
        # next script of it re-attaches, and the frames the chain publishes
        # between two scripts have to stay this run's.
        if lone:
            _bar_release(bar)


def run_script(sid, cfg, targets=None, force=None, skip_disabled=True, chain=None,
               job=None, bar=None):
    """Run one script against *cfg* and report what happened.

    *chain* ``(index, count)`` is passed by :func:`run_chain` so a step of a
    multi-script run reports against the WHOLE run's bar; a lone caller leaves
    it None and the script's own counts are the bar (see
    :func:`_run_with_progress`). *job* is the run's in-progress row
    (:mod:`server.job_locks`), so the same frames reach it.

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
                "label": label,
                "stats": _run_with_progress(runner, cfg, label, chain, job=job,
                                            bar=bar)}
    except Exception as e:
        traceback.print_exc()
        return {"id": sid, "name": getattr(runner, "__name__", ""),
                "label": label, "error": str(e)}


class RunBusy(RuntimeError):
    """Another run already holds the paths this one needs.

    Scripts mutate the library in place, so two overlapping runs over the SAME
    album (a UI Run All and an import chain, or two imports of one release)
    must never touch it at once: one would re-encode while the other renames
    or deletes. Every entry point — `/api/run`, `imports.finish_album`, the
    bulk queue, the Soulseek importer — funnels through `run_chain`, and the
    gate there is the run's own path claim (:func:`held_paths`): the same
    album-level claim the registry already makes against a delete, a move or a
    tag write. Two chains over DISJOINT albums therefore run at the same time
    — nothing in album B's scripts needs album A to be finished — while the
    same album, and a library-wide Run All, serialize exactly as the
    process-wide lock used to make them.
    """


def held_paths(cfg, targets):
    """The library paths a run is about to touch: its claim, and its gate.

    A scoped run holds its own targets — an import holds the album it is
    finishing, so a second chain over that album queues behind it while a
    chain over another album does not wait at all. An unscoped one — Run All,
    an import chain with no target — reads and rewrites whatever it finds: its
    scripts discover their own albums from the music folder down
    (``folder = config["music_folder"]`` in every sweep — `mlo.grader.
    run_grade_library`, `mlo.loudness`, `mlo.autotag`, the lyric fetch), so it
    holds the LIBRARY ROOT and every album that sweep finds OUTSIDE it.

    Those strays are the point of the second half. The library root is
    ``<music folder>/Artists`` and the naming script files albums under it, but
    nothing stops one living elsewhere in the music folder — filed by hand, by
    another tool, or left behind by an import that failed halfway. A run that
    walks the music folder rewrites those too, and holding the root alone left
    them unlocked while a Run All was on them: a finish press, a bulk import or
    an auto-import chain could hold the very album the sweep was grading and
    both would write it. They are listed from the same walk the run itself
    makes (`mlo.stats._find_albums`), so the claim and the run cannot disagree
    about which albums are in the library. The ``.mlo`` state dirs are never
    part of it (``SKIP_DIRS`` — the walk does not enter them), so a download or
    a trash entry is not "in use" just because a sweep is going and the cap
    prune keeps doing its work.

    The music folder is what an unscoped run holds while the library root does
    not exist: an unscoped run must still hold ONE path, or two library-wide
    runs would have nothing to serialize on and would both walk and rewrite the
    same library.
    """
    scope = targets if targets is not None else cfg.get("targets")
    paths = [os.path.normpath(str(t)) for t in (scope or []) if str(t).strip()]
    if paths:
        return paths
    folder = str(cfg.get("music_folder") or "").strip()
    if not folder or not os.path.isdir(folder):
        return []
    root = library_root(folder)
    if not (root and os.path.isdir(root)):
        return [folder]
    key = os.path.normcase(os.path.abspath(root))
    return [root] + [d for d in mlo_stats._find_albums(folder)
                     if not os.path.normcase(os.path.abspath(d)).startswith(
                         key + os.sep)]


def run_label(ids):
    """The run's name in MAINTAIN → In progress: the first script, plus how
    many follow it when the caller asked for a chain."""
    names = [RUNNERS.get(i, (f"Script {i}", None))[0] for i in ids]
    if not names:
        return "Script run"
    return names[0] if len(names) == 1 else f"{names[0]} + {len(names) - 1} more"


def run_start_frame(ids):
    """The frame a run claims its surfaces with: ``(done, total, text, steps)``.

    Both surfaces (the header bar and the run's own in-progress row) show the
    LAST frame that reached them, so a run has to replace whatever the last
    producer left there with its own zero state the moment it really starts —
    before its first script has built a bar — or the bar reads someone else's
    percentage while this run is still starting.

    It is deliberately the very frame the run's first script publishes for
    itself (see :func:`_run_with_progress`), so claiming here changes nothing
    once that script starts. A run of ONE script keeps the lone script's
    indeterminate frame (``total=0``): a runner that never ticks (AccurateRip,
    Beets) must not sit at "0/1" reading as hung. ``text`` is "" for a run with
    nothing to run — it claims nothing at all.
    """
    total = len(ids)
    if not total:
        return 0, 0, "", None
    label = RUNNERS.get(ids[0], (f"Script {ids[0]}", None))[0]
    if total == 1:
        return 0, 0, label, None
    return 0, total, f"#1/{total} · {label}", (1, total)


def _waiting_on(paths, job):
    """``(path, holder)`` of the first claim that would make this run wait.

    The non-blocking query a client asks (:func:`job_locks.holder`), asked as
    this run — its own claims are never a conflict — so a run that is about to
    queue can say what it is waiting for before it queues. The claim itself is
    taken afterwards, under the caller's wait/timeout rule.
    """
    for path in paths:
        rec = job_locks.holder(path, asker=job)
        if rec:
            return path, rec
    return None


def waiting_text(path, holder):
    """The sentence a queued run shows while it waits: who has the album.

    The positive of :func:`job_locks.refusal` ("… is in use by …"): what someone
    watching MAINTAIN during an import needs is who to wait for, named the same
    way the refusal that would have failed the run names them.
    """
    holder = holder or {}
    who = holder.get("label") or holder.get("kind") or "another job"
    name = os.path.basename(str(path).rstrip("\\/")) or str(path)
    return f"waiting for {who} — {name} is in use"


@contextlib.contextmanager
def claim_paths(paths, *, kind, label, wait=False, timeout=None, job=None,
                bar=None):
    """Hold *paths* for the block, under the run's own wait rule.

    THE seam every run that rewrites an album goes through: a script chain
    (:func:`run_chain`), an import (`server.imports.finish_album` — every path
    of it, so the album is held from the first tag write and not only from the
    first script), an auto-import's background chain. The claim is the gate, so
    the run cannot start while another job holds what it is about to write:

    * ``wait=True`` (imports, adds-to-library) queues, and SAYS so while it
      waits — one frame naming the holder and the album, on the run's own row
      and, when it has one, on the header bar — because a queued import that
      shows nothing at all reads as a press that did nothing.
    * ``wait=False`` (the user's own one-shot: ``/api/run``, the wizard's "Run
      the import chain") is refused at once, with the claim's own sentence
      naming the holder (:class:`RunBusy`; the routes answer 409) — someone is
      at the keyboard, and queueing them behind the very job that is already
      doing this work is what made a press look dead.

    A claim that never happened leaves nothing behind: the row a waiting run
    made for itself is forgotten with the refusal, so MAINTAIN keeps no ghost.
    """
    job = job or job_locks.current() or job_locks.new_job()
    registered = False
    if wait:
        waiting_by = _waiting_on(paths, job)
        if waiting_by is not None:
            wanted, holder = waiting_by
            if job_locks.current() != job:
                # A job of this run's own: it needs a row to be listed in.
                job_locks.register(job, kind=kind, label=label)
                registered = True
            text = waiting_text(wanted, holder)
            if bar is not None:
                _bar_join(bar)
                _bar_frame(bar, 0, 0, text, job=job)
            else:
                job_locks.set_progress(job, 0, 0, text)
    stack = contextlib.ExitStack()
    try:
        # Only the CLAIM is translated here: a PathLocked raised by the body is
        # some other job's collision, not this run's, and stays one.
        held = stack.enter_context(
            job_locks.holding(paths, job=job, kind=kind, label=label,
                              wait=wait, timeout=timeout))
    except job_locks.PathLocked as e:
        if registered:
            job_locks.release(job)
        # The claim's own sentence — "Album is in use by Import Album (job-4) —
        # wait for it to finish, then retry" — names what to wait for, which "a
        # script run is already in progress" could not. RunBusy is the type
        # every caller already answers with 409 (`/api/run`,
        # `/api/import/finish`) or "the chain could not start" (an import).
        raise RunBusy(str(e)) from None
    try:
        yield held
    finally:
        stack.close()


def run_chain(cfg, ids, targets=None, force=None, progress=None, wait=False,
              timeout=None, final=None):
    """Run *ids* in order against a COPY of *cfg*; report after every script.

    ``progress(done, total, label, result)`` is called once per finished
    script. A script that fails is recorded in the returned list and the chain
    carries on — one bad script must never cost the import the rest of the
    pipeline. An id the registry does not know is a failure entry, not a stop.

    *final* is an optional list the chain fills with the folders it ended on.
    A script can MOVE an album (script 14 imports it into the library and
    renames every file), and the chain follows it so the later scripts still
    have an album to work on — this is how a caller that handed in a path
    learns the album is not there any more. The run works on a copy of *cfg*,
    so nothing else can tell it.

    *wait* decides what happens when another chain holds what this one needs:

    * ``wait=False`` (the API's one-shot runs) raises `RunBusy` immediately —
      the caller answers 409 rather than queueing behind a long run.
    * ``wait=True`` (imports) blocks until the album frees, because an import
      must not silently skip its chain: the album is already on disk and the
      user asked for it to be finished. ``timeout`` (default 1 h) bounds the
      wait so a wedged run cannot hold an import forever.

    Serialization follows the TARGETS, not the process (see :func:`held_paths`
    and `RunBusy`): the run claims the paths it works on in
    `server.job_locks` under the same *wait* rule, and that claim IS its gate —
    so a chain scoped to album A never makes a chain scoped to album B wait,
    while two chains over one album (or a library-wide Run All, which holds the
    root) refuse or queue exactly as before. The claim also means a delete, a
    tag write or an organize landing on a folder these scripts are rewriting is
    refused (409) instead of racing them. The claim ends with the run —
    including when it raises or is cancelled.

    A chain is also refused outright while the app is shutting down for an
    auto-update (see :mod:`server.interrupt_recovery`): every script and every
    import chain funnels through here, so this one check is what stops the
    shutdown from STARTING work it would then have to kill.

    And a library-wide run never SWEEPS an album parked for a person (spec
    R163): while an import waits on an answer, the sweep is narrowed to the
    albums that are not parked, with one log line naming what was left alone —
    a run that names its own albums (the person's press, an import's own chain)
    is not filtered (`server.import_autonomy.chain_scope` is the rule).
    """
    from server import interrupt_recovery
    if interrupt_recovery.is_shutting_down():
        raise RunBusy("the app is shutting down for an update — nothing can "
                      "start now; retry when it is back up")
    # A parked album is not swept (spec R163): while an import waits on a
    # person, a library-wide run is narrowed to the albums that are not parked
    # — the run says so — while a run that NAMES its albums (the person's press,
    # an import finishing the album it names) runs as it always did. The scope
    # is read the same way `held_paths` reads it below (`cfg["targets"]` is what
    # `/api/run` sets for a scoped run).
    from server import import_autonomy

    _scope = targets if targets is not None else cfg.get("targets")
    kept, dropped, held_note = import_autonomy.chain_scope(_scope, cfg)
    if held_note:
        log(held_note)
    if dropped and not kept:
        # Everything this sweep could touch is waiting on a person: there is no
        # work it may do, and running the scripts over an empty scope would only
        # report a walk that found nothing.
        return []
    if kept is not _scope:
        targets = kept
    # The claim (and with it the gate) is taken by claim_paths INSIDE the bar
    # block below, so the refusal is translated there — a PathLocked raised by
    # the body is some other job's collision, not this run's, and stays one.
    paths = held_paths(cfg, targets)
    label = run_label(ids)
    bar = _bar_context()
    # The job this run works as: a chain started inside another job (an import
    # finishing an album it already holds) JOINS it, exactly as the claim below
    # would — so what a waiting run writes is that job's own row.
    job = job_locks.current() or job_locks.new_job()
    with contextlib.ExitStack() as stack:
        # The run's bar goes back with the run, however it ends — including on
        # a raise — so the header never keeps a finished run's slot.
        stack.callback(_bar_release, bar)
        # A caller that QUEUES says so while it waits (claim_paths): with
        # nothing published until its first script ran, a waiting import showed
        # no row of its own (MAINTAIN listed the job it was queued behind) and
        # the header kept that job's numbers.
        with claim_paths(paths, kind="scripts", label=label, wait=wait,
                         timeout=timeout, job=job, bar=bar) as job:
            # A run owns the surfaces from the moment it is really running, not
            # from the moment its first script has built a bar: both the header
            # bar and the run's own row are painted by the LAST frame that
            # reached them, so a new run has to replace whatever the last
            # producer left there (the import stages before it, a finished
            # run's report) with ITS OWN zero state — otherwise the bar reads
            # someone else's percentage while this run is still starting, which
            # is what a "half-filled bar at the start of an import chain" was.
            _bar_join(bar)
            done, total, text, steps = run_start_frame(ids)
            if text:
                _bar_frame(bar, done, total, text, job=job, steps=steps)
            return _run_chain_locked(cfg, ids, targets=targets, force=force,
                                     progress=progress, final=final, job=job,
                                     bar=bar)


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


def _claimed_targets(result):
    """The album folders the script that just ran says it moved audio into.

    A script that takes an album's audio out of the folder the chain is pointed
    at (script 14 imports it into the library and renames every file with the
    naming script) knows where it put it, and the folder NAME is not that
    answer: the move renames the files with it, which is exactly what the name
    scan in :func:`_follow_moved_targets` cannot follow. The destination is
    declared in the runner's own stats — ``{"moved_targets": [dirs]}`` — so a
    chain reads it from every runner the same way instead of knowing which
    script happens to move things.

    Only folders that really hold audio NOW are used: a stale path in a
    report must not send the rest of the chain nowhere twice.
    """
    stats = result.get("stats") if isinstance(result, dict) else None
    out = []
    for d in (stats or {}).get("moved_targets") or ():
        try:
            p = os.path.normpath(str(d))
        except Exception:
            continue
        if p and p not in out and os.path.isdir(p) and _has_audio(p):
            out.append(p)
    return out


def _take_claimed(claimed, names, live):
    """The folder a mover says it put ONE vanished target's audio in.

    Only a folder no live target already points at is a candidate — a chain
    over two albums must never hand one of them the other's. A single
    candidate is the answer; with several, only the one holding as many audio
    files as the vanished target had is taken, because anything looser would
    be a guess. "" means the caller keeps its honest "there is nothing to run
    on" warning instead of guessing.
    """
    cands = [d for d in claimed if os.path.normcase(d) not in live]
    if not cands:
        return ""
    if len(cands) == 1:
        return cands[0]
    if names:
        same = [d for d in cands if len(_audio_basenames(d)) == len(names)]
        if len(same) == 1:
            return same[0]
    return ""


def _follow_moved_targets(cfg, audio_names, claimed=(), misses=None,
                          claim_job=None):
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
    A move that ALSO renamed every file cannot be followed that way — beets
    does exactly that on an import, naming each track from its tags — so
    *claimed* (what the script that just ran said it moved the audio into, see
    :func:`_claimed_targets`) is asked FIRST: it is the mover's own answer and
    it costs nothing, where the identity lookup behind it is a walk of the
    whole library that a scoped import (every import) must never pay for. The
    walk is therefore only the fallback for a mover that did not report where
    the audio went — nothing else can answer for a folder a script renamed out
    from under the chain. Without one of the two answers the download folder
    stayed the target and the whole tag-writing tail of the
    chain ran against a folder that no longer holds the album, which is how an
    import ended up looking finished while every later script had written
    nothing. When neither answer exists the target stays put and the chain says
    so out loud instead of printing a cheerful "nothing to do".

    "Vanished" means the target holds no audio any more, not that the
    directory is gone: beets only takes the audio, so the staging folder is
    still there afterwards, holding the covers / .cue / .log it left behind —
    which is exactly the empty-of-audio folder that made the tail a no-op.
    Those files are the album's own, so they are moved into the folder the
    album is in NOW before the chain carries on (mlo.layout.carry_album_files)
    — a cover fetched before the chain ran must not be left one folder behind
    the album it was fetched for.

    The run's CLAIM follows the same move (*claim_job*, see
    :func:`job_locks.move`): the chain holds the folder the audio has left, and
    the album it is still rewriting — tags, covers, the grade — is at its new
    path. Both halves matter: the new folder is claimed before the next script
    writes a byte into it, and the emptied one is let go instead of staying
    "in use" for the rest of the run.
    """
    if cfg.get("targets") is None:
        # A LIBRARY-WIDE run has no targets to follow: every script in it
        # discovers the library for itself, so there is nothing here to
        # re-point. Writing the empty list below would flip every LATER script
        # onto the targeted branch — a config that names no targets is not the
        # same as a config that names none — and the whole tail of the chain
        # would report "nothing to do" while doing nothing at all. That is the
        # failure this function exists to prevent, so it must not be the thing
        # that causes it.
        return
    targets = cfg.get("targets") or []
    live = {os.path.normcase(t) for t in targets}
    out = []
    for t in targets:
        if os.path.isfile(t) or _has_audio(t):
            out.append(t)
            continue
        names = audio_names.get(t) or set()
        if not names:                   # nothing of ours was there to follow
            out.append(t)
            continue
        # One full-library walk per vanished target is the search this does;
        # repeating it for every remaining script costs a library walk per
        # script to re-learn the same answer. A target that resolved nothing
        # once is asked about once (a script that MOVES the album later
        # re-points the chain, and the new folder is not in here).
        if misses is not None and os.path.normcase(t) in misses:
            log(f"WARNING: no audio left in {t} and the album could not be "
                f"found in the library — the scripts after this one have "
                f"nothing to run on")
            out.append(t)
            continue
        moved = _take_claimed(claimed, names,
                              live | {os.path.normcase(p) for p in out})
        if not moved:
            moved = _find_moved_album(names, str(cfg.get("music_folder") or ""))
        if moved:
            log(f"Album moved: {t} → {moved}; the rest of the chain follows it")
            # The CLAIM follows too (server.job_locks.move): the run was holding
            # the folder the audio has just left, and what it is really
            # rewriting is the album — at its new path. Without this the album
            # was unlocked under its new name for the whole tail of the chain
            # (a folder that has just appeared is exactly what a second import,
            # a re-download or a fresh run would claim), while the shell it left
            # stayed "in use" until the run ended. Taken FIRST (the album is
            # never unclaimed in between) and waited for, never stolen: the
            # album is mid-rewrite, so a foreign holder is queued behind.
            if claim_job:
                try:
                    job_locks.move(claim_job, t, moved)
                except job_locks.PathLocked as e:
                    log(f"WARNING: {e} — the album's new folder could not be "
                        f"claimed, so another job may start on it")
            # The album's OWN files travel with it, at the album root: the
            # cover / description / expected-tracklist the steps before and
            # around this mover wrote into the folder the audio just left
            # (mlo.layout.carry_album_files is the one rule, the same one the
            # organizer applies when it renames an album). Without it the album
            # ends up one folder away from its own cover and the grade reports
            # COVER on an album the import had just fetched artwork for.
            try:
                from mlo import layout as _layout
                carried, left = _layout.carry_album_files(
                    t, moved, music_folder=cfg.get("music_folder"), log=log)
            except Exception:
                traceback.print_exc()
                carried, left = [], []
            if left:
                log(f"WARNING: {len(left)} file(s) of {os.path.basename(t)} "
                    f"could not follow the album (" + ", ".join(left[:4])
                    + " already exist in its folder) — left where they are")
            audio_names[moved] = names
            out.append(moved)
        else:
            log(f"WARNING: no audio left in {t} and the album could not be "
                f"found in the library — the scripts after this one have "
                f"nothing to run on")
            if misses is not None:
                misses.add(os.path.normcase(t))
            out.append(t)
    cfg["targets"] = out


def _run_chain_locked(cfg, ids, targets=None, force=None, progress=None,
                      final=None, job=None, bar=None):
    from server import interrupt_recovery
    cfg = dict(cfg)
    if targets is not None:
        cfg["targets"] = [os.path.normpath(str(t)) for t in targets]
    _apply_force(cfg, force)

    results = []
    total = len(ids)
    # Each target's audio, taken before any script runs: the only way to
    # recognise the album again once a script has moved its folder.
    audio_names = {t: _audio_basenames(t) for t in (cfg.get("targets") or [])}
    # Targets whose album could not be found after a script moved it: the
    # lookup is a library walk, so it is answered once per target, not once
    # per remaining script (see _follow_moved_targets).
    misses: set = set()
    for done, sid in enumerate(ids, 1):
        if interrupt_recovery.is_shutting_down():
            # The auto-updater is restarting this container: stop at a SCRIPT
            # BOUNDARY instead of letting the run carry on into Docker's
            # SIGKILL. Uvicorn waits for this background task BEFORE the
            # lifespan teardown, so a chain that ignored the flag held the
            # container open past its stop grace (measured: a 13 s chain kept
            # `docker stop` waiting for it, with the app's own 120 s grace —
            # and its journal — never reached). Nothing is half-written either
            # way (every write is atomic), but this way the run SAYS what it
            # did not do instead of the log simply stopping.
            left = [RUNNERS.get(s, (f"Script {s}", None))[0]
                    for s in ids[done - 1:]]
            log(f"stopping for an update: {len(left)} script(s) not run — "
                + ", ".join(left))
            break
        result = run_script(sid, cfg, chain=(done, total), job=job, bar=bar)
        results.append(result)
        _follow_moved_targets(cfg, audio_names, _claimed_targets(result),
                              misses, claim_job=job)
        label = RUNNERS.get(sid, (f"Script {sid}", None))[0]
        # Every step ends announced, including one that never entered its
        # runner (a switched-off feature, an unavailable module): the header
        # bar and the in-progress row both move to this step, so neither can
        # be left showing a script the run has already left behind. It goes
        # through the RUN's own frame writer (see _bar_frame), which is what
        # keeps the announcement this run's when another chain is in flight.
        _bar_frame(bar, done, total, f"#{done}/{total} · {label}", job=job,
                   steps=(done, total))
        if progress is not None:
            try:
                progress(done, total, label, result)
            except Exception:
                traceback.print_exc()
    if ids and len(results) < total:
        # The run was cut short at a script boundary (the auto-updater above):
        # its last step announce left the bar INSIDE the run, and nothing else
        # is coming, so end the surface rather than leave a frozen fraction on
        # it. The bar fills (done == total, the one state a single-line bar can
        # read as "over") while the readout keeps the honest pair — how many
        # steps ran of how many were asked for — and the text says which.
        ran = len(results)
        _bar_frame(bar, total, total,
                   f"#{ran}/{total} · {run_label(ids)} stopped", job=job,
                   steps=(ran, total))
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
    # Where the chain ended up, for a caller that handed it an album folder:
    # `_follow_moved_targets` re-points `cfg["targets"]` when a script moved it
    # (beets does, on every import), and the copy made at the top of this
    # function means the caller cannot otherwise find out. An import reports
    # the album's real folder from this — its own path was the staging folder
    # it came from, which holds no audio once the chain is done.
    if final is not None:
        final[:] = list(cfg.get("targets") or [])
    return results
