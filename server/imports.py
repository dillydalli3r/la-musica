"""The import service: what happens to an album once it is on disk.

Every import path (the wizard's upload/ingest, the downloads page, the bulk
queue, the Soulseek auto-import) ends in the same place — :func:`finish_album`
runs the configured script chain over the new album folder, and
:func:`bulk_import` is the queue that moves staging folders into the library
first. The Soulseek auto-import additionally verifies the downloaded audio
against the release it was looking for (:func:`acoustid_match`).

Config keys this module owns:

    import_auto_scripts     master switch for the post-import chain
    import_scripts          explicit chain ids ([] = DEFAULT_CHAIN)
    import_bulk_concurrency albums processed at once by bulk_import
    import_acoustid         run the AcoustID release check on an import
    acoustid_*              key / availability of the AcoustID lookup itself

Nothing here raises at a caller: an import that half-worked still reports
every album's outcome.
"""
import os
import re
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from mlo.config import load_config
from mlo.paths import library_root, move_path

from server import script_runners
from server import tagcache

# CUEs → FLACs → videos → lyrics format → fetch lyrics → auto tagging
# (mood/genre/advisory) → images → audit → DR & ReplayGain → AccurateRip →
# key & BPM → beets → format all → grade. Cheap, path-independent work first,
# the slow re-encodes and the library-wide grader last.
DEFAULT_CHAIN = [2, 3, 11, 1, 13, 8, 5, 6, 7, 9, 12, 14, 10, 4]

SCRIPT_ID_MIN, SCRIPT_ID_MAX = 1, 15


# --------------------------------------------------------------------------- #
# The chain
# --------------------------------------------------------------------------- #
def chain_for(cfg=None):
    """The script ids an import runs, in order.

    ``import_auto_scripts`` off is no chain at all. Otherwise an explicit
    ``import_scripts`` list replaces the built-in chain (ids outside 1-15
    dropped, duplicates dropped, order kept); an empty one means DEFAULT_CHAIN.
    """
    cfg = cfg or {}
    if not cfg.get("import_auto_scripts", True):
        return []
    configured = cfg.get("import_scripts")
    if isinstance(configured, str):
        configured = re.split(r"[;,\s]+", configured)
    if configured:
        ids = []
        for raw in configured:
            try:
                sid = int(raw)
            except (TypeError, ValueError):
                continue
            if SCRIPT_ID_MIN <= sid <= SCRIPT_ID_MAX and sid not in ids:
                ids.append(sid)
        return ids
    return list(DEFAULT_CHAIN)


def _invalidate_caches():
    """The album's tags just changed — the tag/name caches are stale.

    Same two caches ``/api/run`` invalidates after a script run; a missing
    module (a stripped backend) is not a reason to fail an import.
    """
    try:
        from server import tagcache
        tagcache.invalidate_all()
    except Exception:
        pass
    try:
        from server import mbresolve
        mbresolve.invalidate()
    except Exception:
        pass


def finish_album(album_dir, cfg=None, progress=None, force=None):
    """Run the configured chain over ONE album folder.

    The single call every import path makes after an album is on disk.
    Returns ``{"path", "scripts", "chain", "errors"}``; ``scripts`` is one
    result per chain id (``server.script_runners`` shape) and ``errors`` a
    flat list for a caller that only wants to know what went wrong. A failing
    script is reported, never raised: the album is already imported.
    """
    cfg = cfg or load_config()
    path = os.path.normpath(str(album_dir))
    out = {"path": path, "scripts": [], "chain": [], "errors": []}
    chain = chain_for(cfg)
    out["chain"] = chain
    if not chain:
        return out                      # auto scripts off / no ids configured
    if not os.path.isdir(path):
        out["errors"].append("album folder not found")
        return out

    try:
        # wait=True: an import must not skip its chain just because a UI run
        # happens to hold the library lock — it queues behind it instead.
        out["scripts"] = script_runners.run_chain(
            cfg, chain, targets=[path], force=force, progress=progress, wait=True)
    except script_runners.RunBusy as e:
        # Only reachable after the (1 h) wait timed out: report it so the
        # caller marks the album unfinished instead of "imported".
        out["errors"] = [str(e)]
        return out
    out["errors"] = [f"script {r.get('id')}: {r['error']}"
                     for r in out["scripts"] if r.get("error")]
    _invalidate_caches()
    return out

# --------------------------------------------------------------------------- #
# AcoustID release check
# --------------------------------------------------------------------------- #
def _album_dir(path):
    """A track path's album folder, or the folder itself."""
    p = os.path.normpath(str(path))
    return p if os.path.isdir(p) else os.path.dirname(p)


def _audio_files(folder):
    """Every audio file under *folder*, dot-dirs pruned.

    Uses the download-side extension set (``server.soulseek_auto``): an import
    can be a folder of raw WAVs/APEs that the chain converts afterwards, and
    ``mlo.paths``'s library sets do not cover those.
    """
    from server.soulseek_auto import _AUDIO_EXTS
    out = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in sorted(files):
            if os.path.splitext(f)[1].lower() in _AUDIO_EXTS:
                out.append(os.path.join(root, f))
    return sorted(out)


_ACOUSTID_ROW = {"release_group_id": None, "release_group_title": None,
                 "release_group_type": None, "artists": [], "score": None,
                 "matched": 0, "total": 0, "recordings": []}


def acoustid_match(paths, cfg=None, progress=None, apply=False):
    """Which release group the audio in these albums really is (AcoustID).

    Album folders or track paths; the tracks of each folder are fingerprinted
    and voted on as one album (see ``mlo.acoustid.match_release``). Returns
    ``{"available", "note", "albums": [{"path", "release_group_id",
    "release_group_title", "release_group_type", "artists", "score",
    "matched", "total", "recordings", "tagged"}]}`` — ``available`` False with
    a human-readable ``note`` when no key/fpcalc is configured, and never an
    exception.

    With *apply* the accepted match is also written into the files
    (`ACOUSTID_ID` + `ACOUSTID_FINGERPRINT`, the tags Picard writes and the
    opt-in grading check reads) and ``tagged`` reports how many went in — the
    wizard passes apply=True when the user accepts the match, which is the
    only moment "this is really that release" is a statement the app can act
    on. Fingerprints come from the lookup, so applying costs no extra fpcalc.
    """
    cfg = cfg or load_config()
    try:
        from mlo import acoustid
    except ImportError as e:                      # pragma: no cover - stripped backend
        return {"available": False, "note": f"acoustid unavailable: {e}", "albums": []}

    albums = []
    for p in paths or []:
        album = _album_dir(p)
        if album and album not in albums:
            albums.append(album)

    if not acoustid.available(cfg):
        return {"available": False,
                "note": acoustid.acoustid_enabled_note(cfg) or "AcoustID unavailable",
                "albums": []}

    rows = []
    for album in albums:
        row = {"path": album, "tagged": 0, **_ACOUSTID_ROW}
        tracks = []
        try:
            tracks = _audio_files(album)[:acoustid.MAX_TRACKS]
            match = acoustid.match_release(cfg, tracks, progress=progress) if tracks else None
        except Exception:
            traceback.print_exc()
            match = None
        if match:
            row.update({k: match.get(k) for k in _ACOUSTID_ROW})
            row["path"] = album
            if apply:
                tagged = 0
                for rec in match.get("recordings") or []:
                    if acoustid.write_tags(rec.get("path"), rec.get("recording_id"),
                                           rec.get("fingerprint"), cfg):
                        tagged += 1
                row["tagged"] = tagged
                if tagged:
                    tagcache.invalidate_all()
        else:
            row["total"] = len(tracks)
        rows.append(row)
    return {"available": True, "note": "", "albums": rows}


def release_group_mismatch(match_row, release_group_id):
    """Warning text when an AcoustID result is another release group, else "".

    Used by the Soulseek auto-import as a *verification* of an already
    accepted download: a different pressing is worth telling the user about,
    never worth throwing the album away over.
    """
    want = str(release_group_id or "").strip()
    got = str((match_row or {}).get("release_group_id") or "").strip()
    if not want or not got or got == want:
        return ""
    title = (match_row or {}).get("release_group_title") or "?"
    return (f"AcoustID matched release group {got} ({title}, "
            f"{(match_row or {}).get('matched')}/{(match_row or {}).get('total')} "
            f"tracks) but the release being imported is {want}")


# --------------------------------------------------------------------------- #
# Identity tags for a release-driven import
# --------------------------------------------------------------------------- #
def _release_for_stamping(release):
    """A release dict with the keys the Soulseek stamper reads.

    Callers hand over what they have (``release_mbid``/``title``/``artists``
    from the wizard or the discovery providers); the stamper wants
    ``id``/``media``/``artists``.
    """
    rel = dict(release or {})
    if not rel.get("id"):
        rel["id"] = rel.get("release_mbid") or ""
    return rel


def _stamp_release(album_dir, release, cfg):
    """Write the release identity into the album's tags.

    Returns ``(written, failed)`` — a file whose tags cannot be written is
    counted rather than silently skipped: a release-driven import that stamped
    nothing would otherwise surface much later as bare grading failures with
    nothing pointing at the stamp step.

    The MusicBrainz ids (+ per-track recording ids) come from
    ``server.soulseek_auto._stamp_mb_tags`` — the same stamper the Soulseek
    import uses, so a bulk import and an auto-import tag identically. On top:
    GENRE from the MusicBrainz genre cascade (track → release → release group →
    artist) and ITUNESADVISORY when the caller supplied one (script 8 then
    derives ALBUMITUNESADVISORY from it).

    Returns the number of files written; a tag failure never fails an import.
    """
    from mlo.audio import AudioFile
    from server import integrations as intg
    from server import soulseek_auto

    rel = _release_for_stamping(release)
    written = 0
    try:
        soulseek_auto._stamp_mb_tags(album_dir, rel)
    except Exception:
        traceback.print_exc()

    genres = {}
    if rel.get("release_group_id") or rel.get("id"):
        try:
            cascade = intg.genre_cascade(rel, limit=cfg.get("mb_genre_count"))
            for track in cascade.get("per_track") or []:
                key = (int(track.get("disc") or 1), int(track.get("position") or 0))
                genres[key] = track.get("genres") or []
        except Exception:
            traceback.print_exc()

    advisory = rel.get("itunesadvisory", rel.get("advisory"))
    advisory = "" if advisory is None else str(advisory).strip()

    failed = 0
    for path in _audio_files(album_dir):
        try:
            af = AudioFile(path)
            if af.audio is None:
                failed += 1
                continue
            tags = {}
            disc, pos = soulseek_auto._parse_trackno(path)
            names = genres.get((disc, pos)) or []
            if names and not str(af.get_tag("GENRE") or "").strip():
                tags["GENRE"] = "; ".join(names)
            if advisory and not str(af.get_tag("ITUNESADVISORY") or "").strip():
                tags["ITUNESADVISORY"] = advisory
            for key, value in tags.items():
                af.set_tag(key, value)
            if tags:
                written += 1
        except Exception:
            failed += 1
            continue
    return written, failed


# --------------------------------------------------------------------------- #
# Bulk queue
# --------------------------------------------------------------------------- #
_job_lock = threading.Lock()
_job = {"id": None, "kind": None, "status": "idle", "started": None,
        "finished": None, "total": 0, "done": 0, "label": "", "items": [],
        "error": None}


def job_state():
    """The bulk job the UI polls: same shape as ``soulseek_auto.job_state()``."""
    with _job_lock:
        return dict(_job, items=[dict(x) for x in _job["items"]])


def _job_update(**fields):
    with _job_lock:
        _job.update(fields)


def _job_note(done, label, item):
    """Live progress for a running job; a no-op for a direct bulk_import call."""
    with _job_lock:
        if _job["status"] != "running":
            return
        _job["done"] = done
        _job["label"] = label
        # the per-script stats of a whole library are noise in a poll payload
        _job["items"].append({k: v for k, v in item.items() if k != "scripts"})


def _stats_hook(done, total, desc):
    """Mirror progress into mlo.stats.progress_hook — the WS relay's source."""
    from mlo import stats as stats_mod
    hook = getattr(stats_mod, "progress_hook", None)
    if callable(hook):
        try:
            hook(done, total, desc)
        except Exception:
            pass


def _inside(path, folder):
    """Whether *path* is *folder* or below it (boundary-aware).

    Same test server.main's music-folder guard applies: ``C:\\Music2`` is not
    inside ``C:\\Music``, and paths on different drives never are.
    """
    try:
        ap = os.path.abspath(os.path.normpath(path))
        af = os.path.abspath(os.path.normpath(folder))
    except (OSError, ValueError, TypeError):
        return False
    if ap == af:
        return True
    if os.path.normcase(os.path.splitdrive(ap)[0]) != os.path.normcase(os.path.splitdrive(af)[0]):
        return False
    try:
        return os.path.normcase(os.path.commonpath([ap, af])) == os.path.normcase(af)
    except ValueError:
        return False


def _unique_dir(parent, name):
    """``<parent>/<name>``, deduplicated with " (2)", " (3)"…"""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(name)).strip().rstrip(".") or "Import"
    dest = os.path.join(parent, safe)
    n = 2
    while os.path.exists(dest):
        dest = os.path.join(parent, f"{safe} ({n})")
        n += 1
    return dest


def _bulk_one(item, cfg):
    """Move one item into the library (when it is not there yet) and finish it.

    Idempotent by construction: a path that already sits inside the library is
    never moved (a second move would duplicate the album as "Album (2)"), it
    just re-runs the chain.
    """
    src = os.path.normpath(str(item.get("path") or ""))
    row = {"path": src, "status": "failed", "album_path": None,
           "error": None, "scripts": []}
    if not src or not os.path.exists(src):
        row["error"] = "path not found"
        return row

    folder = str(cfg.get("music_folder") or "").strip()
    if not folder or not os.path.isdir(folder):
        row["error"] = "music_folder not set or not found"
        return row

    root = library_root(folder)
    # Containment is not enough: the music folder itself, the library root and
    # the app's own .mlo state dir all live inside the music folder, so a
    # caller could queue the whole library (or config/playlists/trash) as if it
    # were an album — and a staging item would then be *moved* into a
    # subdirectory of itself. Album folders BELOW <music>/Artists are of course
    # legitimate, so only the two roots match exactly while the state dir
    # excludes its subtree. Checked before the "has audio?" test so the reason
    # is the real one, not "no audio files".
    def _same(a, b):
        try:
            return os.path.normcase(os.path.abspath(os.path.normpath(a))) == \
                   os.path.normcase(os.path.abspath(os.path.normpath(b)))
        except (OSError, ValueError, TypeError):
            return False

    for guard in (folder, root):
        if guard and _same(src, guard):
            row["error"] = "refusing to import the library root itself"
            return row
    try:
        from mlo.paths import app_data_dir
        # Scoped to THIS cfg's music folder: a bulk run must never treat the
        # config/playlists/trash of some other library as an album.
        state = app_data_dir(folder)
        if state and _inside(src, state):
            row["error"] = "refusing to import a library or app-state folder"
            return row
    except Exception:
        pass

    if os.path.isdir(src) and not _audio_files(src):
        # an empty/stray folder is not an album: importing it would publish a
        # library folder the import did not produce
        row["status"] = "skipped"
        row["error"] = "no audio files"
        return row

    move = item.get("move")
    if move is None:
        # staging paths are moved in; a folder that already IS in the library
        # (a re-run, an album imported by another path) is left where it is
        move = not _inside(src, root)
    if move and _inside(src, root):
        move = False
    album = src
    if move:
        dest = _unique_dir(root, os.path.basename(src.rstrip("\\/")))
        if not move_path(src, dest, log=lambda m: None):
            row["error"] = ("could not move into the library — a file inside "
                            "is still in use (stop playback and retry)")
            return row
        album = dest

    release = item.get("release")
    stamp_error = None
    if release:
        try:
            written, failed = _stamp_release(album, release, cfg)
            if failed:
                stamp_error = (f"release tags written to {written} file(s); "
                               f"{failed} file(s) could not be tagged")
        except Exception as e:
            traceback.print_exc()
            stamp_error = f"release stamping failed: {e}"

    try:
        finished = finish_album(album, cfg)
    except Exception as e:                      # finish_album promises not to raise
        traceback.print_exc()
        finished = {"path": album, "scripts": [], "chain": [], "errors": [str(e)]}

    row["album_path"] = finished["path"].replace("\\", "/")
    row["scripts"] = finished["scripts"]
    row["error"] = "; ".join(filter(None, [stamp_error, *finished["errors"]])) or None
    # "Imported" means the album is in the library. It is only honest to say
    # so when the chain actually ran (or when nothing is configured to run):
    # an album that landed with none of its scripts executed is unfinished,
    # and reporting it as imported is how a silent gap becomes a mystery
    # grading failure later.
    if finished["errors"] and not finished["scripts"]:
        row["status"] = "failed"
        row["error"] = f"imported, but the script chain did not run: {row['error']}"
    else:
        row["status"] = "imported"
    return row


def bulk_import(items, cfg=None, progress=None):
    """Import a queue of albums (staging folders or library folders).

    *items*: ``{"path", "release": <optional MB release dict>, "move": bool}``
    — ``move`` defaults to True for a path outside the library and False for
    one already inside it. Each album is moved into
    ``<music folder>/Artists``, stamped with the release identity when one is
    supplied, then finished with the configured chain. ``import_bulk_concurrency``
    albums run at once (each on its own copy of the config).

    Progress goes to *progress(done, total, label, item_result)* and to
    ``mlo.stats.progress_hook`` (the websocket relay); a running job started by
    :func:`start_bulk` is updated too. Returns ``{"total", "ok", "failed",
    "skipped", "items": [...]}`` with the item rows in input order.

    A row's ``status``: ``imported`` — the album is in the library (a failing
    *script* rides along in ``error``, it does not un-import the album);
    ``skipped`` — nothing to import (no audio files); ``failed`` — it never
    got into the library, and ``error`` says why.
    """
    cfg = cfg or load_config()
    items = [dict(it) for it in (items or [])]
    total = len(items)
    rows = [None] * total
    try:
        concurrency = max(1, int(cfg.get("import_bulk_concurrency") or 1))
    except (TypeError, ValueError):
        concurrency = 1

    def label_for(index):
        name = os.path.basename(str(items[index].get("path") or "").rstrip("\\/"))
        return f"Importing {name}" if name else f"Importing {index + 1}/{total}"

    if total:
        with ThreadPoolExecutor(max_workers=concurrency,
                                thread_name_prefix="mlo-import") as pool:
            futures = {pool.submit(_bulk_one, it, dict(cfg)): i
                       for i, it in enumerate(items)}
            done = 0
            for future in as_completed(futures):
                index = futures[future]
                try:
                    row = future.result()
                except Exception as e:          # _bulk_one reports, never raises
                    traceback.print_exc()
                    row = {"path": str(items[index].get("path") or ""),
                           "status": "failed", "album_path": None,
                           "error": str(e), "scripts": []}
                rows[index] = row
                done += 1
                label = label_for(index)
                _job_note(done, label, row)
                _stats_hook(done, total, label)
                if progress is not None:
                    try:
                        progress(done, total, label, row)
                    except Exception:
                        traceback.print_exc()

    rows = [r for r in rows if r is not None]
    return {
        "total": total,
        "ok": sum(1 for r in rows if r["status"] == "imported"),
        "failed": sum(1 for r in rows if r["status"] == "failed"),
        "skipped": sum(1 for r in rows if r["status"] == "skipped"),
        "items": rows,
    }


def start_bulk(items, cfg=None):
    """Start :func:`bulk_import` on a daemon thread; returns straight away.

    One bulk job at a time: a second start is refused with
    ``{"ok": False, "error": "bulk import already running"}``. Poll
    :func:`job_state` for progress.
    """
    with _job_lock:
        if _job["status"] == "running":
            return {"ok": False, "error": "bulk import already running"}
        _job.update({"id": uuid.uuid4().hex[:12], "kind": "bulk",
                     "status": "running", "started": time.time(),
                     "finished": None, "total": len(items or []), "done": 0,
                     "label": "", "items": [], "error": None})
        job = dict(_job, items=[])

    def run():
        try:
            result = bulk_import(items, cfg or load_config())
            _job_update(status="done", finished=time.time(),
                        done=result["total"], error=None)
        except Exception as e:
            traceback.print_exc()
            _job_update(status="failed", finished=time.time(), error=str(e))

    threading.Thread(target=run, name="mlo-bulk-import", daemon=True).start()
    return {"ok": True, "job": job}
