"""The 14 library scripts, in one place every caller shares.

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
    run_grade_library, run_optimize_flacs, run_process_images,
)
from mlo.loudness import run_calc_dr_replaygain


def _optional(module, name):
    """An entry point whose module may be absent (a stripped checkout).

    Same ImportError guard ``/api/run`` used, so importing this module never
    fails over a script the install does not have.
    """
    try:
        return getattr(__import__(module, fromlist=[name]), name)
    except ImportError:
        return None


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
    8: ("Auto tagging", run_auto_tagging),
    9: ("AccurateRip", _optional("mlo.accurip", "run_generate_accurip")),
    10: ("Format all", _optional("mlo.format_all", "run_format_all")),
    11: ("Remux videos (MKV)", _optional("mlo.remux", "run_remux_videos")),
    12: ("Key & BPM", _optional("mlo.audiometa", "run_analyze_audiometa")),
    13: ("Fetch lyrics", _optional("mlo.lyrics_fetch", "run_fetch_lyrics")),
    14: ("Beets tagging", _optional("server.beetscfg", "run_beets_tagging")),
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
}
# Scripts whose feature has its own on/off switch: with it off the runner is a
# no-op at best and a crash at worst, so a chain skips them instead.
_DISABLED = {
    7: "dr_replaygain_enabled",
    12: "audiometa_enabled",
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


def run_script(sid, cfg, targets=None, force=None, skip_disabled=True):
    """Run one script against *cfg* and report what happened.

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
    if skip_disabled and gate and not cfg.get(gate, True):
        return {"id": sid, "name": getattr(runner, "__name__", ""), "label": label,
                "skipped": True, "reason": f"{gate} is off"}
    try:
        return {"id": sid, "name": getattr(runner, "__name__", ""),
                "label": label, "stats": runner(cfg)}
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
    """
    acquired = RUN_LOCK.acquire(blocking=False) if not wait else \
        RUN_LOCK.acquire(timeout=3600 if timeout is None else timeout)
    if not acquired:
        raise RunBusy("a script run is already in progress")
    try:
        return _run_chain_locked(cfg, ids, targets=targets, force=force, progress=progress)
    finally:
        RUN_LOCK.release()


def _run_chain_locked(cfg, ids, targets=None, force=None, progress=None):
    cfg = dict(cfg)
    if targets is not None:
        cfg["targets"] = [os.path.normpath(str(t)) for t in targets]
    _apply_force(cfg, force)

    results = []
    total = len(ids)
    for done, sid in enumerate(ids, 1):
        result = run_script(sid, cfg)
        results.append(result)
        if progress is not None:
            label = RUNNERS.get(sid, (f"Script {sid}", None))[0]
            try:
                progress(done, total, label, result)
            except Exception:
                traceback.print_exc()
    return results
