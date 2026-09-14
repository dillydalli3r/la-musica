"""Dynamic Range (simple-dr-meter) and ReplayGain (rsgain) tag calculation.

New script 7. For every album (folder) it:

  * runs ``rsgain easy`` to write the standard ReplayGain tags
    (REPLAYGAIN_TRACK_GAIN / _TRACK_PEAK / _ALBUM_GAIN / _ALBUM_PEAK), and
  * runs simple-dr-meter (a Python script; needs ffmpeg + numpy) to write
    DYNAMIC RANGE (per track) and ALBUM DYNAMIC RANGE tags parsed from the
    ``dr.txt`` log it produces.

Both tags are already required by the grader, so running this script is what
populates them. Tools are optional: the script skips whatever is missing with
a clear message instead of failing.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

from .audio import AudioFile
from .config import should_write_audio_tag
from .paths import AUDIO_EXTS
from .stats import (
    new_stats, _make_pbar, _pbar_skip, _pbar_update, _walk_files,
    _collect_targets, _find_albums, is_audio_file, worker_count,
)
from .subproc import run_tool
from .tools import detect_all_tools, simple_dr_meter_path
from .ui import print_header, log, c, Color
from concurrent.futures import ThreadPoolExecutor, as_completed

# Track row: "DR12      -0.15 dB   -11.21 dB      3:30 05-Spiders"
# Columns: DR, Peak (val unit), RMS (val unit), Duration, Track label.
# simple-dr-meter labels each row with the TRACK NUMBER and TITLE from the
# file's tags (e.g. "05-Spiders"), NOT the filename - so we key by track
# number and match files via their TRACKNUMBER tag.
TRACK_ROW_RE = re.compile(
    r"^\s*DR(\d{1,2})\s+(?:\S+\s+){5}(\d+)\s*-\s*(.*?)\s*$"
)
# Album: "Official DR value: DR11"
OFFICIAL_DR_RE = re.compile(
    r"Official DR value:\s*DR(\d{1,2})", re.IGNORECASE
)

RGAIN_TAGS = (
    "REPLAYGAIN_TRACK_GAIN",
    "REPLAYGAIN_TRACK_PEAK",
    "REPLAYGAIN_ALBUM_GAIN",
    "REPLAYGAIN_ALBUM_PEAK",
)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _find_python():
    """A Python interpreter able to run simple-dr-meter, or None."""
    if not getattr(sys, "frozen", False):
        return sys.executable
    for cand in ("py", "python"):
        p = shutil.which(cand)
        if p:
            return p
    return None


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
    """Run rsgain easy on an album folder (or the library root)."""
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


# ----------------------------------------------------------------------
# Dynamic Range via simple-dr-meter
# ----------------------------------------------------------------------
def _run_dr_meter(script_path, ffmpeg_dir, album, workdir):
    """Run simple-dr-meter on an album; returns path to dr.txt or None."""
    python = _find_python()
    if not python:
        return None
    env = dict(os.environ)
    env["PATH"] = ffmpeg_dir + os.pathsep + env.get("PATH", "")
    dr_path = os.path.join(album, "dr.txt")
    # simple-dr-meter refuses to overwrite an existing dr.txt log; drop any
    # stale one so re-runs work.
    try:
        if os.path.exists(dr_path):
            os.remove(dr_path)
    except OSError:
        return None
    try:
        proc = run_tool(
            [python, script_path, album],
            cwd=workdir, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=3600,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()
            log(c(f"      dr-meter: {(tail[-1] if tail else 'failed')}",
                  Color.YELLOW))
            return None
        return dr_path if os.path.isfile(dr_path) else None
    except Exception as e:
        log(c(f"      dr-meter error: {e}", Color.YELLOW))
        return None


def _parse_dr_file(dr_path):
    """Parse dr.txt -> ({track_number: dr}, {title_lower: dr}, album_dr)."""
    per_track = {}
    per_title = {}
    album_dr = None
    try:
        # Try chardet if available for cp1252/latin1 titles, fallback to utf-8
        enc = "utf-8"
        try:
            import chardet as _ch
            with open(dr_path, "rb") as _fb:
                raw = _fb.read(65536)
                det = _ch.detect(raw)
                if det and det.get("encoding") and det.get("confidence", 0) > 0.5:
                    enc = det["encoding"]
        except Exception:
            pass
        with open(dr_path, "r", encoding=enc, errors="replace") as f:
            for line in f:
                m = TRACK_ROW_RE.match(line)
                if m:
                    dr = int(m.group(1))
                    try:
                        per_track[int(m.group(2))] = dr
                    except ValueError:
                        pass
                    title = m.group(3).strip().lower()
                    if title:
                        per_title[title] = dr
                    continue
                m2 = OFFICIAL_DR_RE.search(line)
                if m2:
                    album_dr = int(m2.group(1))
    except OSError:
        pass
    return per_track, per_title, album_dr


def _raw_tag(af, name):
    """Read a raw tag value (case-insensitive) that isn't in TAG_MAP."""
    try:
        for k, v in af.all_tags().items():
            if str(k).lower() == name.lower():
                return str(v).strip()
    except Exception:
        pass
    return ""


def _write_dr_tags(album, per_track, per_title, album_dr, write_tags=True, config=None):
    """Write DYNAMIC RANGE + ALBUM DYNAMIC RANGE to the album's files.

    Rows in dr.txt are keyed by TRACK NUMBER + TITLE; files are matched by
    their TRACKNUMBER tag first, falling back to the TITLE tag.
    """
    modified = 0
    for f in sorted(os.listdir(album)):
        if not is_audio_file(f):
            continue
        path = os.path.join(album, f)
        try:
            af = AudioFile(path)
            raw_tn = _raw_tag(af, "TRACKNUMBER")
            num = int(raw_tn) if raw_tn.isdigit() else None
            raw_title = _raw_tag(af, "TITLE").lower()
        except Exception:
            continue
        dr = per_track.get(num) if num is not None else None
        if dr is None and raw_title:
            dr = per_title.get(raw_title)
        if dr is None:
            continue
        try:
            if not write_tags:
                continue
            if config is not None and not should_write_audio_tag(config, "DYNAMIC RANGE", filepath=path):
                continue
            changed = False
            if str(af.get_tag("DYNAMIC RANGE") or "").strip() != str(dr):
                if af.set_tag("DYNAMIC RANGE", str(dr)):
                    changed = True
            if (album_dr is not None
                    and str(af.get_tag("ALBUM DYNAMIC RANGE") or "").strip()
                    != str(album_dr)):
                if af.set_tag("ALBUM DYNAMIC RANGE", str(album_dr)):
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
    dr_script = (simple_dr_meter_path()
                 if config.get("write_dynamic_range_tags", True) else None)
    force = config.get("force_dr_replaygain", False)
    skip_existing = config.get("replaygain_skip_existing", True) and not force

    if not rsgain and not (dr_script and ffmpeg):
        log(c("ERROR: neither rsgain nor simple-dr-meter+ffmpeg are installed. "
              "Use Dependencies to download them.", Color.RED))
        return stats

    if rsgain:
        log(f"replaygain: rsgain v{rsgain['version']} · skip-existing="
            f"{'on' if skip_existing else 'off'}")
    if (dr_script and ffmpeg
            and config.get("write_dynamic_range_tags", True)):
        log(f"dynamic range: simple-dr-meter + ffmpeg v{ffmpeg['version']}")
    else:
        log(c("dynamic range: unavailable (need simple-dr-meter + ffmpeg "
              "+ numpy in the Python that runs it)", Color.YELLOW))

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

    workdir = tempfile.mkdtemp(prefix="mlo_dr_")
    # Respect worker_limit for the per-album DR/ReplayGain loop (CPU-heavy)
    workers = worker_count(config, default=4, maximum=8, items=len(albums))
    # For a single album, avoid thread overhead
    if len(albums) == 1 or workers == 1:
        try:
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
                if dr_script and ffmpeg and album_failed is None:
                    audio_files = [f for f in os.listdir(album) if is_audio_file(f)]
                    if audio_files and (force or any(_file_missing_dr(
                            os.path.join(album, f)) for f in audio_files)):
                        dr_path = _run_dr_meter(
                            dr_script, os.path.dirname(ffmpeg["ffmpeg_exe"]),
                            album, workdir)
                        if dr_path:
                            per_track, per_title, album_dr = _parse_dr_file(dr_path)
                            album_modified += _write_dr_tags(
                                album, per_track, per_title, album_dr,
                                write_tags=config.get("write_dynamic_range_tags", True),
                                config=config,
                            )
                            try:
                                os.remove(dr_path)
                            except OSError:
                                pass
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
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    else:
        def _dr_album_task(album_path):
            amod = 0
            afail = None
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
            if dr_script and ffmpeg and afail is None:
                try:
                    audio_files = [f for f in os.listdir(album_path) if is_audio_file(f)]
                except OSError:
                    audio_files = []
                if audio_files and (force or any(_file_missing_dr(os.path.join(album_path, f)) for f in audio_files)):
                    dr_path = _run_dr_meter(dr_script, os.path.dirname(ffmpeg["ffmpeg_exe"]), album_path, workdir)
                    if dr_path:
                        per_track, per_title, album_dr = _parse_dr_file(dr_path)
                        amod += _write_dr_tags(album_path, per_track, per_title, album_dr, write_tags=config.get("write_dynamic_range_tags", True), config=config)
                        try:
                            os.remove(dr_path)
                        except OSError:
                            pass
            return album_path, amod, afail

        try:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(_dr_album_task, a): a for a in sorted(albums)}
                for fut in as_completed(futures):
                    album_path, amod, afail = fut.result()
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
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    if pbar:
        pbar.close()

    stats["is_grader"] = False
    return stats


def _file_missing_dr(path):
    try:
        af = AudioFile(path)
        return not (str(af.get_tag("DYNAMIC RANGE") or "").strip())
    except Exception:
        return True
