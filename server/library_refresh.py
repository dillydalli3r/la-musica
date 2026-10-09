"""One refresh routine — "tell the truth about the library now".

A REFRESH is two things in one pass, and nothing else in the app does both:

  1. it DROPS the assembled payload caches (`tagcache.invalidate_library_payloads`,
     `mbresolve`, the recommendations), so the next `/api/library`, `/api/home`
     and grades build re-walks the music folder from the files instead of
     answering from a TTL entry — exactly what `?refresh=1` did before this
     module existed; and
  2. it re-runs the FILE-STRUCTURE scan (`mlo.layout.scan_library`) and stores
     its report (`mlo.layout.save_report`), so the Library page's layout
     warning is about the folder as it is NOW — not a scan some earlier press
     happened to leave behind. The stored report is the only thing the warning
     reads (`GET /api/library/layout/report`), so without this half a structure
     problem nothing re-scanned for stayed invisible forever.

FOUR TRIGGERS, one rule each:

  * a MANUAL press — `?refresh=1` on `/api/library` or `/api/home` — is the
    full pass, run inline because the user asked for the truth on that press
    (`server.main._refresh_library_caches` delegates here);
  * APP START — `start()`'s worker runs the full pass once in its own thread,
    so process startup never waits for the walk;
  * AFTER A SCRIPT/IMPORT RUN — `server.script_runners.run_chain` calls
    `mark_stale` on the way out (even on a raise): it is the CHEAP half — the
    payload caches are dropped immediately (the change is known now) while the
    scan is DEBOUNCED a few seconds so a 200-album bulk import, which runs 200
    chains, costs ONE scan and not 200;
  * an INTERVAL — the worker runs the full pass when `now - _last` reaches
    `library_refresh_minutes` (default 5; `0` turns this trigger off entirely).

ANY refresh RESETS THE INTERVAL CLOCK: `_last` is stamped by every trigger
above — the manual press, startup, a script run and the interval pass itself —
so a user (or a run) who just refreshed pushes the next automatic one a full
interval out, instead of the interval firing again seconds later off a stamp
that no longer describes when the library was last checked.

Everything here is best-effort: a scan that cannot run leaves the stored report
in place and says so in the log, and NEVER raises into the request, the worker
or the script run that triggered it. The automatic triggers are all off the
critical path — startup, a request and a script run never wait for a scan.
"""
import threading
import time
import traceback

from mlo.config import load_config
from mlo import layout as mlo_layout

# How long a `mark_stale` waits before its deferred scan runs. A few seconds:
# long enough that the many chains of one bulk import (or the burst of writes
# behind one import) collapse into a single scan, short enough that the
# Library's warning is current about as soon as the run has settled.
DEBOUNCE_S = 4.0

# How often the worker wakes to look for a due scan or a due interval. Small
# enough to fire a debounced scan promptly; the interval trigger only needs to
# be sampled, so this is the debounce's own granularity.
TICK_S = 2.0

# The shipped interval, mirrored from mlo.config's `library_refresh_minutes`
# so this module still works when the key is absent (an older config).
DEFAULT_MINUTES = 5.0

_lock = threading.RLock()
_worker = None
_stop = threading.Event()
# When the library was last refreshed, of ANY kind (the interval clock). 0.0
# means "never in this process".
_last = 0.0
# When a debounced scan is due, and the reason `mark_stale` carried for it.
# 0.0 = no scan pending.
_debounce_at = 0.0
_debounce_reason = ""


def _drop_payload_caches():
    """The cheap half: drop the ASSEMBLED payloads, keep everything keyed on
    the files (the body `server.main._refresh_library_caches` used to hold).

    Lazy imports: this module is imported by `server.main` itself, and the tag
    cache, mbresolve and the recommendations import each other and the app's
    routers — importing them at module load would be a cycle to untangle for no
    gain."""
    from server import tagcache
    tagcache.invalidate_library_payloads()
    from server import mbresolve
    mbresolve.invalidate()
    from server import recommendations
    recommendations.invalidate()


def _whole_library_cfg(cfg):
    """*cfg* for the scan, with any `targets` CLEARED.

    `mlo.layout.scan_library` reads `cfg["targets"]` and confines its walk to
    what it names — a scoped `/api/run` (and the import chain) puts the album
    it is working on into the config. A refresh, though, is about the WHOLE
    library: the report it stores is the one the Library page's warning reads
    as the library's own state, so a scan left scoped to one album would store
    a report about that album while the page presented it as the whole folder.
    The caller's run-scoped copy is never what walks here."""
    scan_cfg = dict(cfg or {})
    scan_cfg.pop("targets", None)
    return scan_cfg


def _scan_and_store(reason, cfg):
    """Run the layout scan over the WHOLE library and store its report.

    ``(ok, findings, note)`` — never raises. A scan that cannot run leaves the
    stored report untouched and reports why; the request, the worker or the
    script run that triggered it is never broken by it."""
    scan_cfg = _whole_library_cfg(cfg)
    try:
        report = mlo_layout.scan_library(scan_cfg)
    except Exception as e:
        return False, None, f"layout scan failed: {e}"
    findings = report.get("total")
    if findings is None:
        findings = len(report.get("issues") or [])
    try:
        path = mlo_layout.save_report(scan_cfg, report)
    except Exception as e:
        return False, findings, f"layout report could not be stored: {e}"
    if not path:
        return False, findings, "layout report could not be stored"
    print(f"[mlo] library refresh ({reason}): layout scan stored "
          f"{int(findings)} finding(s)")
    return True, int(findings), ""


def refresh_now(reason, cfg=None):
    """The FULL pass — drop the payload caches, re-scan the layout, store it.

    Called by the manual Refresh (`?refresh=1`), by the worker at startup, and
    by the worker when the interval is due. Stamps the interval clock and
    supersedes any debounced scan still pending (`mark_stale`'s, whose change
    this pass already covers).

    Returns ``{"ok", "reason", "at", "findings"}`` and NEVER raises: a scan
    that cannot run leaves the stored report in place and says so in the log.
    """
    cfg = cfg if cfg is not None else load_config()
    _drop_payload_caches()
    with _lock:
        global _debounce_at, _debounce_reason
        # A full pass is the deferred scan's own subject, done now.
        _debounce_at = 0.0
        _debounce_reason = ""
    ok, findings, note = _scan_and_store(reason, cfg)
    if not ok:
        print(f"[mlo] library refresh ({reason}): {note}; the stored report "
              f"was left in place")
    global _last
    at = time.time()
    with _lock:
        # Stamped whether or not the scan ran: the payload caches were dropped
        # and the library was re-derived from the files, which is what the
        # interval clock measures.
        _last = at
    return {"ok": ok, "reason": reason, "at": at, "findings": findings}


def mark_stale(reason, cfg=None):
    """The CHEAP half — a change is known NOW, but the scan waits.

    Drops the assembled payload caches (so the next read re-derives the tree)
    and stamps the interval clock immediately, then schedules ONE scan
    `DEBOUNCE_S` from now instead of running it inline. Repeated calls push
    that one pending scan out and coalesce into it, so the many chains of a
    bulk import cost a single scan after the run has settled. Does not raise.

    This is what `server.script_runners.run_chain` calls on the way out."""
    cfg = cfg if cfg is not None else load_config()
    _drop_payload_caches()
    now = time.time()
    global _last, _debounce_at, _debounce_reason
    with _lock:
        _last = now
        _debounce_at = now + DEBOUNCE_S
        _debounce_reason = reason
    return {"ok": True, "reason": reason, "at": now, "findings": None}


def _interval_minutes(cfg):
    """The interval in force for *cfg*, in minutes; 0.0 = the trigger is off."""
    raw = (cfg or {}).get("library_refresh_minutes", DEFAULT_MINUTES)
    try:
        minutes = float(raw)
    except (TypeError, ValueError):
        minutes = float(DEFAULT_MINUTES)
    return minutes if minutes > 0 else 0.0


def _tick(now=None, cfg=None):
    """One worker wake: fire a due debounced scan, then a due interval pass.

    Separated from the loop so it can be driven with an explicit clock and
    config (the tests do exactly that)."""
    global _debounce_at, _debounce_reason, _last
    cfg = cfg if cfg is not None else load_config()
    now = time.time() if now is None else now
    with _lock:
        due = _debounce_at
        reason = _debounce_reason
        pending = due and now >= due
        if pending:
            _debounce_at = 0.0
            _debounce_reason = ""
    if pending:
        _scan_and_store(reason or "a change", cfg)
        with _lock:
            # The deferred scan just established the truth: reset the clock.
            _last = now
    minutes = _interval_minutes(cfg)
    if minutes <= 0:
        return
    with _lock:
        last = _last
    if last and now - last >= minutes * 60.0:
        refresh_now("interval", cfg)


def _loop():
    """Run the startup pass, then wake on a short tick for the two timed
    triggers. One bad pass never ends the worker."""
    try:
        refresh_now("startup")
    except Exception:
        traceback.print_exc()
    while not _stop.wait(TICK_S):
        try:
            _tick()
        except Exception:
            traceback.print_exc()  # one bad wake must not end the worker


def start(cfg=None):
    """Start the worker; True when THIS call started it.

    Idempotent — one worker thread ever, however many times the app's lifespan
    asks. The thread runs `refresh_now("startup")` first and then ticks."""
    global _worker
    with _lock:
        if _worker and _worker.is_alive():
            return False
        _stop.clear()
        _worker = threading.Thread(target=_loop, name="mlo-library-refresh",
                                   daemon=True)
        _worker.start()
    return True


def stop():
    """Ask the worker to stop at its next idle point (app shutdown)."""
    _stop.set()


def last_refresh():
    """The moment of the last refresh of ANY kind (0.0 = never this process)."""
    with _lock:
        return _last


def status():
    """What the last refresh did and the interval in force, for a read (and
    for the tests)."""
    cfg = load_config()
    with _lock:
        pending = _debounce_at - time.time() if _debounce_at else None
        return {"running": bool(_worker and _worker.is_alive()),
                "interval_minutes": _interval_minutes(cfg),
                "last": _last or None,
                "pending_scan_in": max(0.0, pending) if pending is not None else None}