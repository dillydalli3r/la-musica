"""Lyrics routes — the provider chain and its per-track auto-import.

Script 13 writes lyrics for a whole library; this router exposes the same
engine call for one track (the track page's "Auto-import lyrics" button) so a
click and a script run produce byte-identical files. Provider order and the
plain-vs-synced policy come from the config the settings page edits.
"""
import os
import time
from concurrent.futures import (ThreadPoolExecutor, as_completed,
                                TimeoutError as FuturesTimeoutError)

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from mlo import load_config
from mlo.lyrics_fetch import fetch_one
from mlo.lyrics_providers import (SOURCES, SOURCE_LABELS, SOURCE_NOTES,
                                 Deadline, available_sources, provider_order)
from mlo.stats import worker_count
from server import tagcache
from server import job_locks

router = APIRouter(tags=["lyrics"])


class LyricsPathsRequest(BaseModel):
    """A batch of tracks for the lyrics routes: the fetch, the
    transliteration/translation pass and the LRCLIB publish all take the same
    shape, so one button and one script cannot mean different things."""
    paths: list[str] = []
    force: bool = False
    staged: bool = False  # the import wizard's not-yet-imported album


# Each path runs the whole provider chain (up to six providers, each with
# retries), and the handler is synchronous, so it occupies an API worker
# thread. Cap the batch: a whole-album or whole-library fetch belongs to
# script 13 (`POST /api/run`), which reports progress and can be cancelled.
MAX_PATHS = 100

# The budget ONE PRESS is allowed to spend, in seconds. Measured, a single
# track's unlimited walk is the whole chain with retries (six providers, up to
# 51 s of retries each) plus a 240 s YouTube caption pass, and the alias pass
# repeats all of it — while the reply, and the strip the user is watching, wait
# for EVERY lane. The owner's report, 2026-10-13: "i feel like the app can get
# 'stuck' importing lyrics" — the strip sat at "Fetching lyrics — 1/3…"
# indefinitely, and because the job is only released in this route's `finally`,
# a walk that never returned left the bar on screen through a page reload.
# The wizard's client gives up at 120 s (`api.lyricsAuto`), so the server must
# answer before that: a track stops walking at `PER_TRACK_BUDGET` (its row then
# names the sources left, `mlo.lyrics_providers.Deadline`), and the batch stops
# WAITING at `BATCH_BUDGET` whatever a lane is still doing — the lanes left are
# reported as timed out and keep their own provider timeouts, so nothing hangs.
# honey: fixed budgets, not a setting — a knob here would only move the stall.
# Revisit if the client's own timeout changes or a provider is added whose
# normal answer takes tens of seconds.
PER_TRACK_BUDGET = 45.0
BATCH_BUDGET = 75.0


def _batch_paths(paths, cfg, staged):
    """Split a batch into the tracks it may touch and the ones it may not.

    ONE guard for all three lyrics routes, because they write to the same
    files: inside the music folder, or the wizard's explicit staged allowance
    for an album the library does not list yet. A path that fails either is
    reported as that path's own failure, so one bad entry never costs the rest
    of the batch its work.

    Returns ``(resolved, rejected)`` — `resolved` as ``(as_sent, full)`` pairs
    in request order (a reply quotes the path the caller sent, so a client can
    key its own rows by it), `rejected` as ``(path, error)`` pairs carrying the
    words the fetch has always used.
    """
    from server.main import _allow_staged, _in_music_folder, _music_folder

    folder = _music_folder(cfg)
    resolved, rejected = [], []
    for path in paths:
        full = os.path.normpath(path)
        if not folder or not (_in_music_folder(full, folder)
                              or _allow_staged(full, staged)):
            rejected.append((path, "path outside music folder"))
        elif not os.path.isfile(full):
            rejected.append((path, "file not found"))
        else:
            resolved.append((path, full))
    return resolved, rejected


@router.get("/api/lyrics/providers")
def lyrics_providers():
    """Selectable lyrics sources, the saved order, and the plain-lyrics policy."""
    cfg = load_config()
    return {
        "sources": available_sources(),
        "default_order": list(SOURCES),
        "order": provider_order(cfg),
        "saved": cfg.get("lyrics_sources") or [],
        "allow_plain": bool(cfg.get("lyrics_allow_plain", False)),
        "search_aliases": bool(cfg.get("lyrics_search_aliases", True)),
        "labels": dict(SOURCE_LABELS),
        "notes": dict(SOURCE_NOTES),
    }


@router.get("/api/lyrics/providers/probe")
def lyrics_provider_probe(source: str = Query(...)):
    """Test ONE provider against the fixed sample track (Settings / wizard).

    One cheap lookup, no writes: `{id, kind, status: ok|skipped|fail, detail,
    ms}`. `skipped` means this machine cannot run the provider (yt-dlp missing,
    nothing to probe, the host refusing us), `fail` that it ran and had no
    lyrics for the sample. The sample is the same for every provider, so two
    runs are comparable."""
    from mlo.lyrics_providers import probe_source
    if source not in SOURCES:
        raise HTTPException(404, f"unknown lyrics source: {source}")
    return probe_source(source, load_config())


@router.post("/api/lyrics/auto")
@job_locks.holds(lambda req: req.paths, kind="lyrics", label="Fetch lyrics")
def lyrics_auto(req: LyricsPathsRequest):
    """Auto-import lyrics for one or more tracks through the provider chain.

    Falls back provider by provider (LRCLIB → NetEase → Kugou → QQ Music →
    Kuwo → YouTube captions by default), then writes
    per `lyrics_format` exactly like script 13. A source's answer WITH
    timestamps wins; untimed text is written only when no source states any,
    and the track's result says which happened (`kind`). A track
    that already has lyrics is left alone unless *force* is set — this route
    is the ONE place that replaces stored words, because a person asked for
    those tracks (the bulk script never does) — and an INSTRUMENTAL track is
    never touched, force or not.

    The manual counterpart of the chain's own script 13 — `fetch_one` is what
    both call — so the tags and the sidecar a click writes are the ones an
    import writes.
    """
    cfg = load_config()
    if len(req.paths) > MAX_PATHS:
        raise HTTPException(
            413, f"too many paths in one request ({len(req.paths)} > {MAX_PATHS}) — "
                 f"run script 13 (Fetch lyrics) for a whole album or library")
    results = []
    resolved, rejected = _batch_paths(req.paths, cfg, req.staged)
    for path, error in rejected:
        results.append({"path": path, "status": "failed", "error": error})
    total = len(resolved)
    # ONE TRACK PER LANE (`worker_count`, the one knob every multi-file runner
    # reads), because each track's fetch is its OWN question — its own tags
    # decide what is searched and what is written — and the lanes only overlap
    # the WAITS. Politeness is the provider layer's, not this loop's:
    # `mlo.lyrics_providers._request` spaces requests PER HOST (its lock guards
    # the timestamp bookkeeping alone, never the request), so two lanes asking
    # lrclib queue behind each other while a lane asking NetEase does not wait
    # for them. Writes are per file (one track, one handle), and the rows are
    # reassembled in REQUEST ORDER, so the reply is the serial loop's own,
    # row for row.
    #
    # The job is what makes the fetch VISIBLE while it runs: a frame per
    # completed track reaches the header bar and the wizard's own strip over
    # the progress socket, and the In-progress page lists the row meanwhile.
    # The wizard's counted bar used to advance only when a whole chunk of
    # eight came back, so one slow lane held the readout at "0/19" for minutes
    # (the report this fixes).
    #
    # The press HOLDS the tracks it was given (the decorator above): a second
    # press — or the import chain's own script 13 on the same album — is
    # refused with the holder's name instead of running two fetches over the
    # same files. The job id is that hold's OWN row, so the frames below land
    # on the row the In-progress page already shows.
    workers = worker_count(cfg, maximum=8, items=total)
    job = job_locks.current() or job_locks.new_job()
    fetched: list = [None] * total
    ex = None
    try:
        if total:
            job_locks.publish(0, total, f"Fetching lyrics — 0/{total}…", job=job)
            ex = ThreadPoolExecutor(max_workers=workers)
            # One budget per LANE, from the moment it starts: the walk stops
            # between providers and names the ones it never reached, so a slow
            # track answers "stopped after 45s — the sources left were …"
            # instead of holding the batch (and its bar) open.
            futures = {
                ex.submit(fetch_one, full, cfg, replace=bool(req.force),
                          mark_absent=False,
                          deadline=Deadline(PER_TRACK_BUDGET)): i
                for i, (_as_sent, full) in enumerate(resolved)
            }
            done = 0
            finished = set()
            try:
                for fut in as_completed(futures, timeout=BATCH_BUDGET):
                    finished.add(fut)
                    i = futures[fut]
                    try:
                        fetched[i] = fut.result()
                    except Exception as e:
                        # fetch_one absorbs its own failures; this is belt and
                        # braces so one broken lane cannot strand the reply.
                        fetched[i] = {"path": resolved[i][1], "status": "failed",
                                      "error": str(e)}
                    done += 1
                    job_locks.publish(done, total,
                                      f"Fetching lyrics — {done}/{total}…", job=job)
            except FuturesTimeoutError:
                # The BACKSTOP — a lane inside a write (a video lyric remux has
                # an hour-long ffmpeg timeout) is not the walk the per-track
                # budget bounds, so the batch stops WAITING here and says so.
                # Those lanes keep their own provider/timeout bounds; nothing is
                # written twice (each track is one file, one handle).
                late = [i for fut, i in futures.items() if fut not in finished]
                for i in late:
                    fetched[i] = {
                        "path": resolved[i][1], "status": "failed",
                        "error": (f"did not finish within {int(BATCH_BUDGET)}s "
                                  f"— the fetch is still running in the "
                                  f"background; press Fetch lyrics again for "
                                  f"what is left"),
                    }
                done = total
                job_locks.publish(total, total,
                                  f"Fetching lyrics — {total}/{total} "
                                  f"({len(late)} still running)…", job=job)
            # The no-lyrics answer runs ONCE for the whole batch, never once
            # per track: `lyrics_absent` cross-references LRCLIB, Spotify and
            # the AI per track, and the per-track fan-out inside the lanes was
            # measured as the single biggest waste of a lyrics step (the
            # module's own comment; script 13's run defers it exactly this
            # way, and the same values land on the same files either way).
            missing = [i for i, r in enumerate(fetched)
                       if r and r.get("needs_absent")]
            if missing:
                job_locks.publish(total, total,
                                  "Marking the tracks nothing had as instrumental…",
                                  job=job)
                from mlo.lyrics_fetch import _apply_absent
                from server import instrumental as inst

                try:
                    marked = inst.lyrics_absent([resolved[i][1] for i in missing], cfg)
                except Exception:
                    marked = {}
                for i in missing:
                    # `needs_absent` is the deferral's own flag, never part of
                    # the reply row — the chain pops it the same way.
                    fetched[i].pop("needs_absent", None)
                    _apply_absent(fetched[i], resolved[i][1], marked or {})
    finally:
        if ex is not None:
            # `wait=False`: the reply must not wait for a lane whose own bound
            # is longer than the batch's (the video-write case the backstop
            # above reports). Those threads keep running — each is bounded by
            # its provider's own timeouts and every write it makes is atomic —
            # and the JOB is not released here: the decorator's own hold owns
            # that row (releasing it twice would drop the row while the claim
            # is still standing).
            ex.shutdown(wait=False)
    for (path, _full), res in zip(resolved, fetched):
        res["path"] = path
        results.append(res)
    if any(r.get("status") == "ok" for r in results):
        tagcache.invalidate_album(*{os.path.dirname(p) for _, p in resolved})
    return {
        "results": results,
        "order": provider_order(cfg),
        "ok": sum(1 for r in results if r.get("status") == "ok"),
        "skipped": sum(1 for r in results if r.get("status") == "skipped"),
        "failed": sum(1 for r in results if r.get("status") == "failed"),
    }


@router.post("/api/lyrics/xlit")
def lyrics_xlit(req: LyricsPathsRequest):
    """Transliterate and translate the lyrics these tracks already carry.

    Script 17's own runner (`run_lyrics_xlit`), aimed at a selection: the same
    entry point the post-import chain calls, so a click here and an import
    there write the same `TRANSLITERATION-<lang>` / `TRANSLATION-<lang>` tags
    and the same `.romaji.lrc` / `.<lang>.lrc` sidecars — the tags always, the
    sidecars only for the LRC/BOTH lyrics formats (`lyrics_format`,
    `lyrics_xlit_sidecars`), which is the rule the runner and the grader share.

    The runner's own numbers come back unchanged (files modified, its error
    lines), plus `note` when it had nothing to work with — both switches off in
    Settings, or no AI configured — so a button that wrote nothing says why
    instead of looking like it did.
    """
    from mlo.lyrics_xlit import ai_ready, run_lyrics_xlit

    cfg = load_config()
    if len(req.paths) > MAX_PATHS:
        raise HTTPException(
            413, f"too many paths in one request ({len(req.paths)} > {MAX_PATHS}) — "
                 f"run script 17 (Lyrics transliterate) for a whole album or library")
    resolved, rejected = _batch_paths(req.paths, cfg, req.staged)
    errors = [f"{os.path.basename(p)}: {e}" for p, e in rejected]
    note = ""
    if not (cfg.get("lyrics_xlit_enabled", True) or cfg.get("lyrics_translate_enabled", True)):
        note = ("Transliteration and translation are both off in "
                "Settings → Lyrics")
    elif not ai_ready(cfg):
        note = ("AI is not configured — set the base URL and model in "
                "Settings → AI (script 17 has the same requirement)")
    stats = None
    if resolved:
        run_cfg = dict(cfg)
        run_cfg["targets"] = [full for _, full in resolved]
        if req.force:
            run_cfg["force_xlit"] = True
        stats = run_lyrics_xlit(run_cfg)
        errors += [str(e) for e in (stats.get("errors") or [])]
        if stats.get("modified_count"):
            tagcache.invalidate_album(*{os.path.dirname(p) for _, p in resolved})
    return {
        "stats": stats,
        "paths": [path for path, _ in resolved],
        "ok": int((stats or {}).get("modified_count") or 0),
        "skipped": int((stats or {}).get("unchanged_count") or 0),
        "failed": len(errors),
        "errors": errors,
        "note": note,
    }


class LyricsOffsetRequest(BaseModel):
    """A lyric offset for ONE track, in milliseconds.

    A DELTA, not an absolute position: the control sends the step the user
    pressed (its buttons move a tenth of a second), and the server applies it
    to what the file holds at that moment. A caller that instead sent "the
    lyrics are now 300 ms late" would need to know where the file started, and
    two surfaces open on one track (the sidebar and the fullscreen player both
    offer the control) would each overwrite the other's arithmetic with their
    own idea of the origin.
    """
    path: str
    delta_ms: int
    staged: bool = False  # the import wizard's not-yet-imported album


# The app's own sanity bound on one press: a shift longer than an hour is a
# caller bug, not an offset, and the route must refuse it BEFORE it rewrites a
# file — a wrong 3,600,000 ms stamp is unreadable in every player.
MAX_OFFSET_MS = 3600000


@router.post("/api/lyrics/offset")
@job_locks.holds(lambda req: [req.path], kind="lyrics", label="Lyric offset")
def lyrics_offset(req: LyricsOffsetRequest):
    """Move every timestamp in this track's lyrics by `delta_ms` and save it.

    The one lyric edit the PLAYER offers (the sidebar pane's and the fullscreen
    pane's shared offset control): the reader hears that the lines are early or
    late and nudges them until they land, which is only useful if the correction
    outlives the session — so the shift is written where the lyrics live
    (`mlo.lyrics.shift_stored_lyrics`: the `.lrc` beside the track and/or its
    `LYRICS` tag, whichever holds them) and the reply carries the text that was
    stored, so both surfaces render the file's own copy rather than their own
    arithmetic.

    Untimed lines are left exactly as they were — an unsynced lyric has no sync
    to move, and giving it one would invent timings nobody wrote.
    """
    from mlo.lyrics import shift_stored_lyrics

    if abs(req.delta_ms) > MAX_OFFSET_MS:
        raise HTTPException(
            400, f"offset out of range (±{MAX_OFFSET_MS // 60000} minutes)")
    cfg = load_config()
    resolved, rejected = _batch_paths([req.path], cfg, req.staged)
    if rejected:
        _, error = rejected[0]
        raise HTTPException(404 if error == "file not found" else 403, error)
    full = resolved[0][1]
    text, targets = shift_stored_lyrics(full, req.delta_ms, cfg)
    if text is None:
        raise HTTPException(404, "this track has no lyrics to shift")
    tagcache.invalidate_path(full)
    return {"ok": True, "lrc": text, "targets": targets}


@router.post("/api/lyrics/remove")
@job_locks.holds(lambda req: req.paths, kind="lyrics", label="Remove lyrics")
def lyrics_remove(req: LyricsPathsRequest):
    """Remove the lyrics these tracks store — the lyrics step's Remove button.

    Three places per track, all of them through the one implementation
    (`mlo.lyrics.remove_stored_lyrics`): the LYRICS tag, the `.lrc` sidecars
    named after the track itself (`.lrc` / `.romaji.lrc` / `.<lang>.lrc`) and
    the TRANSLITERATION-* / TRANSLATION-* tags. The fetch only ever fills, so
    without this a wrong or stale text could only leave a file by hand.

    A track that held none of the three is answered `empty` — nothing was
    wrong with the press — and a path outside the folder is answered the same
    way the other lyrics routes answer it, as that path's own failure.
    """
    from mlo.lyrics import remove_stored_lyrics

    cfg = load_config()
    if len(req.paths) > MAX_PATHS:
        raise HTTPException(
            413, f"too many paths in one request ({len(req.paths)} > {MAX_PATHS})")
    resolved, rejected = _batch_paths(req.paths, cfg, req.staged)
    results = [{"path": p, "status": "failed", "error": e} for p, e in rejected]
    for as_sent, full in resolved:
        try:
            gone = remove_stored_lyrics(full)
            results.append({"path": as_sent,
                            "status": "ok" if gone["removed"] else "empty",
                            **gone})
            if gone["removed"]:
                tagcache.invalidate_path(full)
        except Exception as e:
            results.append({"path": as_sent, "status": "failed", "error": str(e)})
    return {
        "results": results,
        "removed": sum(1 for r in results if r.get("removed")),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "paths": [p for p, _ in resolved],
    }


@router.get("/api/lyrics/find")
def lyrics_find(artist: str = Query(""), title: str = Query(""),
                album: str = Query(""), duration: float = Query(0.0)):
    """Look lyrics up in the chain without writing anything (the lyrics
    manager's "search online" box).

    The box carries no MusicBrainz ids (the file's own are not in the query),
    so the alias pass asks MusicBrainz by NAME — the same names the chain
    searches with, tried only when the typed ones find nothing and only while
    `lyrics_search_aliases` is on. The hit says which kind it is (`synced` or
    `plain`: untimed text is an answer, the weaker one) and, when the second
    pass is what answered, which name and entity found it (`alias_pass`).
    Nothing is ever written here."""
    from mlo.lyrics_providers import fetch_lyrics

    cfg = load_config()
    if not title:
        raise HTTPException(400, "title is required")
    search_aliases = None
    if cfg.get("lyrics_search_aliases", True):
        try:
            from server.integrations import search_aliases
        except Exception:
            search_aliases = None

    def _aliases():
        """The names the chain's second pass searches with.

        Asked for only when the typed names found nothing (the chain calls
        this lazily), so an ordinary search costs no MusicBrainz lookup. The
        slots are the ones a lyrics query is built from; the entities are the
        MusicBrainz ones their aliases come from."""
        out = {}
        for slot, entity, value in (("artist", "artist", artist),
                                    ("title", "recording", title),
                                    ("album", "release-group", album)):
            found = search_aliases(entity, "", cfg, value) if value else []
            if found:
                out[slot] = found
        return out

    hit = fetch_lyrics(cfg, artist, title, album or None, duration or None,
                       aliases=_aliases if search_aliases else None)
    if not hit:
        return {"found": False, "order": provider_order(cfg)}
    return {"found": True, "order": provider_order(cfg), **hit}
