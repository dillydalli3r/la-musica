"""Dynamic Range (in-process) and ReplayGain (rsgain) tag calculation.

New script 7. For every album (folder) it:

  * runs ``rsgain easy`` to write the standard ReplayGain tags
    (REPLAYGAIN_TRACK_GAIN / _TRACK_PEAK / _ALBUM_GAIN / _ALBUM_PEAK), and
  * measures dynamic range IN PROCESS (mlo.dr: ffmpeg decodes the track, numpy
    does the loudness-war block math) and writes DYNAMIC RANGE (per track) and
    ALBUM DYNAMIC RANGE (the album's mean).

Both tags are already required by the grader, so running this script is what
populates them. Tools are optional: the script skips whatever is missing with
a clear message instead of failing — dynamic range needs ffmpeg, and needs the
numpy this app ships with, and says so when either is absent.

The player also uses this module on demand: replaygain_for_path() answers one
track's playback gain from its tags, measuring the file with ffmpeg's EBU R128
filter (and caching the result under <music>/.mlo/data/) when they are missing.
Playback bounds that measurement (``wait_s`` / PLAYBACK_WAIT_S) so a track never
waits on a decode to start; the run finishes in the background and is cached.
"""
import json
import math
import os
import re
import subprocess
import tempfile
import threading
import time

from . import dr
from .audio import AudioFile
from .config import should_write_audio_tag
from .paths import AUDIO_EXTS, app_data_dir
from .stats import (
    new_stats, _make_pbar, _pbar_skip, _pbar_update, _walk_files,
    _collect_targets, _find_albums, is_audio_file, worker_count,
)
from .subproc import run_tool
from .tools import detect_all_tools
from .ui import print_header, log, c, Color
from concurrent.futures import ThreadPoolExecutor, as_completed

RGAIN_TAGS = (
    "REPLAYGAIN_TRACK_GAIN",
    "REPLAYGAIN_TRACK_PEAK",
    "REPLAYGAIN_ALBUM_GAIN",
    "REPLAYGAIN_ALBUM_PEAK",
)


def _album_dirs(config):
    """Album folders to process (from targets, else the whole library)."""
    folder = config["music_folder"]
    if config.get("targets") is not None:
        target_files = _collect_targets(config["targets"], AUDIO_EXTS)
        dirs = sorted({os.path.dirname(f) for f in target_files})
        return [d for d in dirs if os.path.isdir(d)]
    if not os.path.isdir(folder):
        return []
    return _find_albums(folder)


# ----------------------------------------------------------------------
# ReplayGain via rsgain
# ----------------------------------------------------------------------
def _run_rsgain(rsgain_exe, path, skip_existing):
    """Run rsgain easy on an album folder (or the library root).

    THIS IS THE ONE WRITE IN THE ENGINE THAT CANNOT BE MADE ATOMIC, and it is
    named here rather than glossed over: rsgain (TagLib) opens each track and
    rewrites its tags IN PLACE, with no "write somewhere else" mode, so a
    SIGKILL in the middle of it — the container is restarted by the updater at
    arbitrary moments — can leave that ONE track's tag block torn, and the app
    cannot repair a file it can no longer parse. Every other writer in this
    app goes through mlo.atomic (temp file + fsync + one os.replace), and the
    DR half of this script writes through mlo.audio, so rsgain is the only
    exception rather than an example.

    What bounds the damage: rsgain runs one album at a time, so at most one
    file of the album in flight is affected; the album is re-usable from the
    download folder if the track really is destroyed; and the shutdown grace
    (server.interrupt_recovery.GRACE_SECONDS, under the container's stop
    grace) is what normally lets it finish or abort cleanly. There is no
    flag to make it atomic: given a copy it would write the tags to the copy,
    which would then have to be renamed over every original — the same number
    of writes as the conversion pass, and a silent second copy of the library.
    """
    cmd = [rsgain_exe, "easy", "-m", "MAX", "-q"]
    if skip_existing:
        cmd.append("-S")
    cmd.append(path)
    try:
        proc = run_tool(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=3600,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()
            return False, (tail[-1] if tail else f"rc={proc.returncode}")
        return True, ""
    except Exception as e:
        return False, str(e)


def _file_missing_rgain(path):
    """True when the file lacks any of the standard ReplayGain tags."""
    try:
        af = AudioFile(path)
        return any(not af.get_tag(t) for t in RGAIN_TAGS)
    except Exception:
        return True


def _open_album_files(album):
    """{path: AudioFile | None} for the album's audio files, opened once.

    The DR pass asks two questions about the same file — does it still miss a
    DR tag, and then write them — and used to open the container for each, on
    top of the two full decodes (rsgain + dr-meter) the album already pays
    for. One handle answers both; a file that will not open keeps a None
    handle, so the album is still measured and the write pass skips it.
    """
    opened = {}
    try:
        names = sorted(os.listdir(album))
    except OSError:
        return opened
    for f in names:
        if not is_audio_file(f):
            continue
        path = os.path.join(album, f)
        try:
            opened[path] = AudioFile(path)
        except Exception:
            opened[path] = None
    return opened


def _dr_missing_on(af):
    """True when the handle does not hold BOTH DR tags.

    The ALBUM tag is written by the same pass and the grader requires it
    (ALBUM_TAGS → "Missing album tag ALBUM DYNAMIC RANGE" fails the album), so
    requiring only the per-track tag let script 7 skip a whole album that
    could never pass grading — and the skip is by design, so re-running the
    script could not repair it without Force."""
    if af is None:
        return True
    try:
        return not (str(af.get_tag("DYNAMIC RANGE") or "").strip()
                    and str(af.get_tag("ALBUM DYNAMIC RANGE") or "").strip())
    except Exception:
        return True


def _album_needs_dr(opened):
    """Paths in *opened* that still miss a DR tag."""
    return [p for p, af in opened.items() if _dr_missing_on(af)]


def _dr_album(album, ffmpeg_exe, force, write_tags=True, config=None):
    """Measure the album's tracks in process; write the DR tags.

    Returns (files_modified, [(name, reason)] for the files that could not be
    measured at all). A track the meter has no value for — silent, shorter than
    two blocks — is logged by name and simply carries no per-track tag, which
    is a skip and not a failure of the run.
    """
    opened = _open_album_files(album)
    if not opened or not (force or _album_needs_dr(opened)):
        return 0, []

    values = {}
    failures = []
    for path, af in opened.items():
        if af is None:
            # The container would not open, so it cannot be decoded either.
            failures.append((os.path.basename(path), "the file could not be opened"))
            continue
        result = dr.measure_track_detailed(path, ffmpeg_exe)
        if result.dr is None:
            name = os.path.basename(path)
            if result.failed:
                failures.append((name, result.reason))
                log(c(f"      {name}: {result.reason}", Color.YELLOW))
            else:
                log(f"      {name}: {result.reason}")
            continue
        values[path] = result.dr

    album_value = dr.album_dr(list(values.values()))
    if album_value is None:
        log(f"      no dynamic range for {os.path.basename(album)}: "
            f"{len(values)} of {len(opened)} track(s) measured")
    else:
        log(f"      {os.path.basename(album)}: DR {album_value} over "
            f"{len(values)} of {len(opened)} track(s)")
    modified = _write_dr_tags(values, album_value, write_tags=write_tags,
                              config=config, opened=opened)
    return modified, failures


def _write_dr_tags(per_path, album_value, write_tags=True, config=None,
                   opened=None):
    """Write DYNAMIC RANGE + ALBUM DYNAMIC RANGE to the album's files.

    *per_path* maps each track that produced a DR to its value — measured by
    mlo.dr a moment ago, so the value belongs to the file it came from and
    nothing is matched by title or by position in the folder listing any more.
    *opened* is the album's {path: AudioFile} map from _open_album_files: the
    caller's already-open handles, which keep this pass from opening every
    container a second time.
    """
    modified = 0
    for path, dr_value in per_path.items():
        try:
            if opened is not None:
                af = opened.get(path)
                if af is None:
                    continue
            else:
                af = AudioFile(path)
            if not write_tags:
                continue
            if config is not None and not should_write_audio_tag(config, "DYNAMIC RANGE", filepath=path):
                continue
            pending = {}
            if str(af.get_tag("DYNAMIC RANGE") or "").strip() != str(dr_value):
                pending["DYNAMIC RANGE"] = str(dr_value)
            if (album_value is not None
                    and str(af.get_tag("ALBUM DYNAMIC RANGE") or "").strip()
                    != str(album_value)):
                pending["ALBUM DYNAMIC RANGE"] = str(album_value)
            if not pending:
                continue
            if getattr(af, "is_video", False):
                # A video container is rewritten whole on every write — both
                # tags in one ffmpeg pass instead of two remuxes.
                if af.set_video_tags(pending):
                    modified += 1
            else:
                changed = False
                for tag_name, tag_value in pending.items():
                    if af.set_tag(tag_name, tag_value):
                        changed = True
                if changed:
                    modified += 1
        except Exception:
            continue
    return modified


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------
def run_calc_dr_replaygain(config):
    folder = config["music_folder"]
    stats = new_stats()

    if not config.get("dr_replaygain_enabled", True):
        print_header("DR / ReplayGain (skipped - disabled in settings)")
        return stats

    print_header("Dynamic Range & ReplayGain")
    log(f"music folder: {folder}")

    tools = detect_all_tools()
    rsgain = (tools.get("rsgain")
              if config.get("write_replaygain_tags", True) else None)
    ffmpeg = tools.get("ffmpeg")
    write_dr = config.get("write_dynamic_range_tags", True)
    force = config.get("force_dr_replaygain", False)
    skip_existing = config.get("replaygain_skip_existing", True) and not force

    if not rsgain and not (ffmpeg and write_dr):
        log(c("ERROR: neither rsgain nor ffmpeg are installed. "
              "Use Dependencies to download them.", Color.RED))
        return stats

    if rsgain:
        log(f"replaygain: rsgain v{rsgain['version']} · skip-existing="
            f"{'on' if skip_existing else 'off'}")

    # Dynamic range is measured here, in this process, from ffmpeg's decode —
    # so the only two things it can be missing are ffmpeg and the numpy the
    # block math is numpy's. Both are named once, up front, instead of every
    # album quietly coming back "skipped" and the run looking like a success.
    dr_usable = bool(write_dr and ffmpeg and dr.have_numpy())
    if write_dr and not dr_usable:
        why = ("dynamic range needs ffmpeg, which is not installed"
               if not ffmpeg else dr.NUMPY_REASON)
        log(c(f"ERROR: dynamic range unavailable: {why}", Color.RED))
        stats["error_count"] += 1
        stats["errors"].append(("dynamic range", why))

    if dr_usable:
        # The detected version can be absent (an install whose folder name does
        # not carry one) — say "ffmpeg" then, never "vNone".
        version = ffmpeg.get("version")
        log("dynamic range: in process + ffmpeg"
            + (f" v{version}" if version else ""))
    elif write_dr:
        log(c("dynamic range: unavailable (needs ffmpeg and numpy)",
              Color.YELLOW))

    albums = _album_dirs(config)
    if not albums:
        log("No albums found.")
        return stats

    # Snapshot which files are missing ReplayGain tags BEFORE any rsgain
    # pass, so we can later count exactly which files got newly tagged.
    rg_missing = {}
    if rsgain:
        for album in albums:
            rg_missing[album] = [
                os.path.join(album, f)
                for f in sorted(os.listdir(album))
                if is_audio_file(f) and _file_missing_rgain(
                    os.path.join(album, f))
            ]

    # Full-library runs let rsgain scan the whole tree in one go (its album
    # gain is computed per folder anyway), which is much faster than spawning
    # rsgain once per album.
    if rsgain and config.get("targets") is None and os.path.isdir(folder):
        log("running rsgain over the whole library…")
        ok, err = _run_rsgain(rsgain["rsgain_exe"], folder, skip_existing)
        if not ok:
            log(c(f"rsgain failed: {err}", Color.RED))
            stats["error_count"] += 1
            stats["errors"].append(("rsgain", err))
        else:
            # Strip REPLAYGAIN tags for filetypes where it is disabled per-type
            for album, paths in rg_missing.items():
                for p in paths:
                    if not should_write_audio_tag(config, "REPLAYGAIN_TRACK_GAIN", filepath=p):
                        if not _file_missing_rgain(p):
                            # It was missing before but rsgain just wrote it — remove because per-type disabled
                            try:
                                af = AudioFile(p)
                                for tk in ("REPLAYGAIN_TRACK_GAIN", "REPLAYGAIN_TRACK_PEAK", "REPLAYGAIN_ALBUM_GAIN", "REPLAYGAIN_ALBUM_PEAK"):
                                    if af.get_tag(tk):
                                        af.delete_tag(tk)
                            except Exception:
                                pass

    counts = {"ok": 0, "skip": 0, "fail": 0}
    pbar = _make_pbar(len(albums), "DR/ReplayGain", unit="album")

    # Respect worker_limit for the per-album DR/ReplayGain loop (CPU-heavy)
    workers = worker_count(config, default=4, maximum=8, items=len(albums))
    # For a single album, avoid thread overhead
    if len(albums) == 1 or workers == 1:
        for album in sorted(albums):
            album_modified = 0
            album_failed = None
            if rsgain and config.get("targets") is not None:
                ok, err = _run_rsgain(rsgain["rsgain_exe"], album, skip_existing)
                if not ok:
                    album_failed = f"rsgain: {err}"
            if rsgain and album_failed is None:
                # When force is True, rsgain rewrites even if no files were missing — count as modified
                if force and rg_missing.get(album) == []:
                    # Check if album still has audio files (it does if we are here)
                    if any(is_audio_file(f) for f in os.listdir(album)):
                        album_modified += 1
                for path in rg_missing.get(album, []):
                    if not _file_missing_rgain(path):
                        if should_write_audio_tag(config, "REPLAYGAIN_TRACK_GAIN", filepath=path):
                            album_modified += 1
                        else:
                            # Count as not modified but strip if it was written
                            try:
                                af = AudioFile(path)
                                for tk in ("REPLAYGAIN_TRACK_GAIN", "REPLAYGAIN_TRACK_PEAK", "REPLAYGAIN_ALBUM_GAIN", "REPLAYGAIN_ALBUM_PEAK"):
                                    if af.get_tag(tk):
                                        af.delete_tag(tk)
                            except Exception:
                                pass
                    elif force:
                        # Force re-ran but file still missing (e.g., write disabled per-type) -> count as modified attempt
                        if should_write_audio_tag(config, "REPLAYGAIN_TRACK_GAIN", filepath=path):
                            album_modified += 1
            if dr_usable and album_failed is None:
                dr_modified, dr_failures = _dr_album(
                    album, ffmpeg["ffmpeg_exe"], force,
                    write_tags=write_dr, config=config)
                album_modified += dr_modified
                for name, reason in dr_failures:
                    stats["error_count"] += 1
                    stats["errors"].append((name, reason))
            if album_failed:
                stats["total_scanned"] += 1
                stats["error_count"] += 1
                stats["errors"].append((os.path.basename(album), album_failed))
                _pbar_update(pbar, counts, kind="fail")
                continue
            if album_modified:
                stats["total_scanned"] += 1
                stats["modified_count"] += album_modified
                _pbar_update(pbar, counts, kind="ok")
            else:
                stats["skipped_count"] += 1
                _pbar_skip(pbar, counts)
    else:
        def _dr_album_task(album_path):
            amod = 0
            afail = None
            failures = []
            if rsgain and config.get("targets") is not None:
                ok, err = _run_rsgain(rsgain["rsgain_exe"], album_path, skip_existing)
                if not ok:
                    afail = f"rsgain: {err}"
            if rsgain and afail is None:
                for p in rg_missing.get(album_path, []):
                    if not _file_missing_rgain(p):
                        # Only count if REPLAYGAIN allowed for this filetype
                        if should_write_audio_tag(config, "REPLAYGAIN_TRACK_GAIN", filepath=p):
                            amod += 1
                        else:
                            # Strip tags that rsgain wrote but are disabled per-type
                            try:
                                af = AudioFile(p)
                                for tk in ("REPLAYGAIN_TRACK_GAIN", "REPLAYGAIN_TRACK_PEAK", "REPLAYGAIN_ALBUM_GAIN", "REPLAYGAIN_ALBUM_PEAK"):
                                    if af.get_tag(tk):
                                        af.delete_tag(tk)
                            except Exception:
                                pass
            if dr_usable and afail is None:
                dr_modified, failures = _dr_album(
                    album_path, ffmpeg["ffmpeg_exe"], force,
                    write_tags=write_dr, config=config)
                amod += dr_modified
            return album_path, amod, afail, failures

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_dr_album_task, a): a for a in sorted(albums)}
            for fut in as_completed(futures):
                album_path, amod, afail, failures = fut.result()
                for name, reason in failures:
                    stats["error_count"] += 1
                    stats["errors"].append((name, reason))
                if afail:
                    stats["total_scanned"] += 1
                    stats["error_count"] += 1
                    stats["errors"].append((os.path.basename(album_path), afail))
                    _pbar_update(pbar, counts, kind="fail")
                elif amod:
                    stats["total_scanned"] += 1
                    stats["modified_count"] += amod
                    _pbar_update(pbar, counts, kind="ok")
                else:
                    stats["skipped_count"] += 1
                    _pbar_skip(pbar, counts)

    if pbar:
        pbar.close()

    stats["is_grader"] = False
    return stats


# ======================================================================
# On-demand per-file ReplayGain (player-facing)
# ======================================================================
# Batch runs above tag whole folders with rsgain. Playing a track that has
# no tags yet — a fresh import, a file the user copied in, one whose
# per-type ReplayGain writing was disabled — must not play at a different
# loudness than the rest of the library, so this measures the ONE file on
# demand with ffmpeg's EBU R128 filter and caches the result.
#
# ReplayGain 2.0 targets this reference loudness: gain = reference -
# measured_integrated_loudness. A track already at the reference needs no
# correction (0 dB); one measured 3 dB quieter than it gets +3 dB. The
# filter implements the same ITU-R BS.1770 / EBU R128 meter rsgain uses, so
# an on-demand value and a batch-written tag agree (ffmpeg's own ebur128
# summary even prints "TARGET:-23 LUFS" — that is its streaming default, not
# the ReplayGain reference; we override it with this constant).
RG2_REFERENCE_LUFS = -18.0

# ffmpeg summary block (see parse_ebur128). Anchored at line start so the
# per-second progress lines — which carry their own "I:" and "FTPK:"/"TPK:"
# fields — can never be mistaken for the summary values.
EBUR128_I_RE = re.compile(r"^\s*I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", re.MULTILINE)
EBUR128_PEAK_RE = re.compile(r"^\s*Peak:\s*(-?\d+(?:\.\d+)?)\s*dBFS", re.MULTILINE)

_FFMPEG_CACHE = {"exe": None, "checked": False}
# One store rewrites the whole cache file, so the read-modify-write of
# store_analysis is serialized (the player may ask while a scan writes).
_CACHE_LOCK = threading.Lock()


def _ffmpeg_exe():
    """Cached ffmpeg path, or None when the dependency is not installed."""
    if not _FFMPEG_CACHE["checked"]:
        _FFMPEG_CACHE["checked"] = True
        try:
            _FFMPEG_CACHE["exe"] = (detect_all_tools().get("ffmpeg")
                                    or {}).get("ffmpeg_exe")
        except Exception:
            _FFMPEG_CACHE["exe"] = None
    return _FFMPEG_CACHE["exe"]


def _tag_float(v):
    """A ReplayGain tag value ("-3.21 dB", "0.987000") as a float, or None."""
    try:
        return float(str(v).lower().replace("db", "").strip())
    except (TypeError, ValueError):
        return None


def parse_ebur128(text):
    """Parse an ffmpeg ``ebur128=peak=true`` log.

    Returns ``{"lufs": float, "peak": float}`` — integrated loudness in LUFS
    and the true peak as a LINEAR value (``10 ** (dBFS / 20)``), the unit the
    REPLAYGAIN_*_PEAK tags and ReplayGain's clip protection use — or None
    when the log holds no summary block (decoder failure, truncated output).

    Only the block after the last ``Summary:`` counts: every progress line
    prints an ``I:`` field too, and the last of those is not a measurement of
    the whole file.
    """
    if not text:
        return None
    tail = text.rsplit("Summary:", 1)[-1]
    m = EBUR128_I_RE.search(tail)
    p = EBUR128_PEAK_RE.search(tail)
    if not m or not p:
        return None
    return {"lufs": float(m.group(1)),
            "peak": 10.0 ** (float(p.group(1)) / 20.0)}


def _measure_file(path):
    """EBU R128 measurement of one file ({"lufs","peak"}), or None."""
    exe = _ffmpeg_exe()
    if not exe:
        return None
    try:
        proc = run_tool(
            [exe, "-hide_banner", "-nostats", "-nostdin", "-i", path,
             "-map", "0:a:0", "-af", "ebur128=peak=true", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=600,
        )
    except Exception:
        return None
    if proc.returncode:
        # A decode that died halfway still prints a summary of what it read.
        return None
    return parse_ebur128(proc.stderr or "")


def _rg_tags(path):
    """The file's four ReplayGain tags as floats; unreadable -> all None."""
    tags = {"gain_db": None, "peak": None,
            "album_gain_db": None, "album_peak_db": None}
    try:
        af = AudioFile(path)
        tags["gain_db"] = _tag_float(af.get_tag("REPLAYGAIN_TRACK_GAIN"))
        tags["peak"] = _tag_float(af.get_tag("REPLAYGAIN_TRACK_PEAK"))
        tags["album_gain_db"] = _tag_float(af.get_tag("REPLAYGAIN_ALBUM_GAIN"))
        tags["album_peak_db"] = _tag_float(af.get_tag("REPLAYGAIN_ALBUM_PEAK"))
    except Exception:
        pass
    return tags


def analyze_file(path, cfg=None, force=False):
    """ReplayGain for ONE file: its tags when they are complete, else ffmpeg.

    Returns ``{"gain_db", "peak", "lufs", "album_gain_db", "album_peak_db",
    "analyzed", "source"}`` — ``analyzed`` False and ``source`` "tags" when
    all four tags were present (nothing measured), True and "ffmpeg" when the
    file was measured (``lufs`` is then the measured integrated loudness and
    the album fields are None). None when there is nothing to fall back on:
    no decoder or an undecodable file. Never raises.

    ``force`` re-measures even a fully tagged file. ``cfg`` is accepted for
    caller symmetry; no config key changes a measurement.
    """
    if not force:
        tags = _rg_tags(path)
        if None not in tags.values():
            return {"gain_db": tags["gain_db"], "peak": tags["peak"],
                    "lufs": None,
                    "album_gain_db": tags["album_gain_db"],
                    "album_peak_db": tags["album_peak_db"],
                    "analyzed": False, "source": "tags"}
    measured = _measure_file(path)
    if measured is None:
        return None
    return {"gain_db": RG2_REFERENCE_LUFS - measured["lufs"],
            "peak": measured["peak"], "lufs": measured["lufs"],
            "album_gain_db": None, "album_peak_db": None,
            "analyzed": True, "source": "ffmpeg"}


# ----------------------------------------------------------------------
# Cache: <music>/.mlo/data/replaygain.json
# ----------------------------------------------------------------------
def _cache_path(cfg):
    music = (cfg or {}).get("music_folder")
    return os.path.join(app_data_dir(music), "replaygain.json")


def _cache_key(path):
    """Cache key: the absolute path, case-folded as the filesystem does."""
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))


def _read_cache(cfg):
    """The cache dict; a missing or corrupt file reads as empty (never raises)."""
    try:
        with open(_cache_path(cfg), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def cached_analysis(cfg, path):
    """The cached measurement for *path*, or None when absent or stale.

    A size or mtime change means the bytes are not the ones we measured
    (re-tagging, a re-encode, a re-download over the same name).
    """
    entry = _read_cache(cfg).get(_cache_key(path))
    if not isinstance(entry, dict):
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    if entry.get("size") != st.st_size or entry.get("mtime") != st.st_mtime:
        return None
    return entry


def store_analysis(cfg, path, data):
    """Cache one file's analysis, atomically; never raises.

    # ponytail: one JSON file rewritten whole per store — switch to a
    # per-entry file or sqlite only if a big library makes this hurt.
    """
    try:
        st = os.stat(path)
    except OSError:
        return
    entry = {k: (data or {}).get(k)
             for k in ("gain_db", "peak", "lufs", "analyzed", "source")}
    entry.update({"size": st.st_size, "mtime": st.st_mtime, "ts": time.time()})
    target = _cache_path(cfg)
    with _CACHE_LOCK:
        cache = _read_cache(cfg)
        cache[_cache_key(path)] = entry
        tmp = None
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".replaygain_", suffix=".json",
                                       dir=os.path.dirname(target) or ".")
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump(cache, f)
                try:
                    f.flush()
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, target)
        except OSError:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass


# How long a PLAYBACK request may wait for an on-demand measurement before it
# is answered at unity and the decode keeps running in the background (see
# replaygain_for_path's ``wait_s``). The player installs the gain BEFORE the
# track starts, so this bound is the longest a song can be held at the click:
# one round trip is fine, a full-song ffmpeg EBU R128 pass (seconds) is not.
PLAYBACK_WAIT_S = 1.0

# One measurement per file at a time. The player asks for the same path from
# two places at once (the track load that needs the gain before play, and the
# gain readout), and a second ffmpeg decode of the same bytes is pure waste —
# so a request that arrives while a run is going waits on THAT run.
_ANALYZE_LOCK = threading.Lock()
_ANALYZE_RUNS = {}


def _analyze_bounded(cfg, path, wait_s):
    """Measure *path* on demand, waiting at most *wait_s* seconds for it.

    The cached measurement, or None while the decode is still running — that
    run is left to finish (daemon thread) and store its value, so the next
    request is answered from the cache instead of decoding again. Never
    raises: a measurement is playback metadata, never worth losing a request.
    """
    key = _cache_key(path)
    with _ANALYZE_LOCK:
        run = _ANALYZE_RUNS.get(key)
        started = run is None
        if started:
            run = _ANALYZE_RUNS[key] = threading.Event()

    if started:
        def _measure():
            try:
                data = analyze_file(path, cfg)
                if data:
                    store_analysis(cfg, path, data)
            except Exception:
                pass
            finally:
                with _ANALYZE_LOCK:
                    _ANALYZE_RUNS.pop(key, None)
                run.set()

        threading.Thread(target=_measure, name="mlo-replaygain",
                         daemon=True).start()

    if not run.wait(max(0.0, float(wait_s))):
        return None
    return cached_analysis(cfg, path)


def replaygain_for_path(cfg, path, mode=None, preamp_db=None,
                        clip_protection=None, wait_s=None):
    """Playback gain for one track: ``{"gain", "peak", "mode", "source",
    "analyzed"}``.

    ``gain`` is dB to add in the player's gain stage, or None for unity
    (mode "off", or nothing to go on). ``mode`` "album" prefers
    REPLAYGAIN_ALBUM_GAIN and falls back to the track value. Missing tags are
    measured on demand (ffmpeg, via the cache) when
    ``replaygain_analyze_missing`` is on. ``preamp_db`` is added before clip
    protection, which clamps the gain so the resulting peak stays at or below
    0 dBFS and says so in ``source`` ("tags+clamp"). Defaults come from *cfg*
    (``replaygain_mode`` / ``_preamp_db`` / ``_clip_protection``); the
    arguments override them. Never raises.

    ``wait_s`` bounds that on-demand measurement: None (batch callers, and
    the command line) waits for the decode, a number returns unity once it
    runs out — the decode finishes in the background and the value is cached,
    so asking again costs one cache read. Playback passes
    ``PLAYBACK_WAIT_S``: the gain has to be known before the track starts, so
    a request must not sit on a multi-second decode to get it.
    """
    cfg = cfg or {}
    if mode is None:
        mode = cfg.get("replaygain_mode", "track")
    if preamp_db is None:
        preamp_db = cfg.get("replaygain_preamp_db", 0.0)
    if clip_protection is None:
        clip_protection = cfg.get("replaygain_clip_protection", False)

    result = {"gain": None, "peak": None, "mode": mode, "source": None,
              "analyzed": False}
    if mode == "off":
        return result

    tags = _rg_tags(path)
    if mode == "album" and tags["album_gain_db"] is not None:
        gain, peak = tags["album_gain_db"], tags["album_peak_db"]
    else:
        gain, peak = tags["gain_db"], tags["peak"]
    if peak is None:
        peak = tags["peak"]
    source = "tags" if gain is not None else None

    if gain is None and cfg.get("replaygain_analyze_missing"):
        data = cached_analysis(cfg, path)
        if data is None:
            if wait_s is None:
                data = analyze_file(path, cfg)
                if data:
                    store_analysis(cfg, path, data)
            else:
                data = _analyze_bounded(cfg, path, wait_s)
        if data:
            gain = data.get("gain_db")
            peak = data.get("peak")
            source = data.get("source")
            result["analyzed"] = bool(data.get("analyzed"))
    if gain is None:
        return result

    try:
        gain = float(gain) + float(preamp_db or 0.0)
    except (TypeError, ValueError):
        return result
    if clip_protection and peak and peak > 0:
        ceiling = -20.0 * math.log10(peak)
        if gain > ceiling:
            gain = ceiling
            source = f"{source}+clamp"
    result.update({"gain": gain, "peak": peak, "source": source})
    return result
