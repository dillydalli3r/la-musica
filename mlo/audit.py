"""Audio integrity auditing via the AudioAuditor CLI.

Wraps AudioAuditorCLI (https://github.com/Angel2mp3/AudioAuditor), a .NET
tool that detects fake lossless files (frequency cutoffs / upsampled
lossy sources), clipping, MQA encoding, fake stereo, excessive silence
and AI-generated audio. The CLI ships as a single self-contained exe and
is auto-downloaded into .dependencies like the encoder toolchain.

Files are fed to the CLI in batches via stdin (one path per line, its
documented bulk mode) and results are read back as one JSON array per
batch with `analyze --json`.

Outputs written to tags:
  AUDIT      REAL / FAKE - on every audited file. Files that already
             carry a REAL or FAKE verdict are skipped (force audit
             overrides this), like the ENCODER markers for optimization.
  LOG_GRADE  0-100 rip-log score (AudioAuditor/cambia) - written to the
             tracks of MEDIA=CD releases only, one score per disc, with
             logs/cues deterministically named CD-N.log / CD-N.cue
             first (see discs.py).
"""
import json
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor

from .audio import AudioFile
from .config import should_write_audio_tag
from .paths import AUDIO_EXTS, DEPS_DIR, app_data_dir
from .stats import (
    new_stats, _make_pbar, _pbar_update, _collect_targets, _walk_files,
    _diff_bytes, worker_count,
)
from .subproc import run_tool
from .tools import detect_all_tools
from .ui import print_header, log, c, Color

# The extensions the CD passes walk. paths.AUDIO_EXTS is the engine's set
# (mlo.stats.is_audio_file also accepts music-video containers, which are never
# CD-DA tracks); this tuple used to be typed out at every gate and had already
# drifted — one copy listed .mp4, so a video container could be listed as a CD
# track while `files` (built from AUDIO_EXTS) never held it.
CD_AUDIO_EXTS = tuple(AUDIO_EXTS)

# Statuses reported by the CLI: Valid (real lossless), Fake (frequency
# cutoff / transcoded), Unknown (could not classify / decode errors),
# Corrupt, Optimized (MQA). Anything unknown-but-decoded is treated as
# a warning rather than a failure.
STATUS_REAL = "Valid"

# How many paths to pipe per CLI invocation. The CLI accepts up to 50k
# paths per run; smaller batches give the GUI a usable progress bar.
BATCH_SIZE = 250

# Map per-file boolean flags to issue labels for the summary.
FLAG_KEYS = (
    ("hasClipping", "clipping"),
    ("isMqa", "MQA"),
    ("isAiGenerated", "AI-generated"),
    ("isFakeStereo", "fake stereo"),
    ("hasExcessiveSilence", "excessive silence"),
    ("hasScaledClipping", "scaled clipping"),
)


# Detector toggles: config key -> CLI --no-* flag. Default on; a False
# setting appends the flag, disabling that detector. Silenced detectors
# produce no warning flags, so a "warn" verdict from them disappears.
# audit_scaled_clipping is filtered in Python (no CLI flag) so loud masters
# can hide just scaled clipping without losing normal clipping detection.
DETECTOR_NO_FLAGS = {
    "audit_clipping": "--no-clipping",
    "audit_mqa": "--no-mqa",
    "audit_ai": "--no-ai",
    "audit_fake_stereo": "--no-fake-stereo",
    "audit_silence": "--no-silence",
    "audit_dynamic_range": "--no-dynamic-range",
    "audit_true_peak": "--no-true-peak",
    "audit_lufs": "--no-lufs",
    "audit_bpm": "--no-bpm",
}


def verify_integrity(filepath, ffmpeg_exe=None, flac_exe=None):
    """Verify audio file integrity like foobar2000's Verify Integrity.

    For FLAC: runs `flac -t` (test) which checks frame CRCs and stream integrity.
    For all types: runs `ffmpeg -v error -i file -f null -` to catch decoding errors,
    truncated files, and sync errors (similar to foobar2000's decoder check).

    Returns (ok: bool, error: str | None). True when no errors detected.
    """
    ext = os.path.splitext(filepath)[1].lower()
    # Try flac -t for FLAC files (most thorough for FLAC)
    if ext == ".flac" and flac_exe and os.path.isfile(flac_exe):
        try:
            proc = run_tool([flac_exe, "-t", filepath],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", timeout=60)
            # flac -t already reads the whole stream, verifies every frame CRC
            # and the MD5 in the STREAMINFO — a second full `ffmpeg -f null`
            # decode of the same file is a whole extra pass for nothing.
            if proc.returncode == 0:
                return True, None
            err = (proc.stderr or proc.stdout or "").strip().splitlines()
            err = err[-1] if err else f"flac -t rc={proc.returncode}"
            return False, err[:200]
        except subprocess.TimeoutExpired:
            return False, "flac -t timeout"
        except Exception as e:
            return False, str(e)[:200]

    # ffmpeg check for all audio types (including FLAC as second check)
    if ffmpeg_exe and os.path.isfile(ffmpeg_exe):
        try:
            proc = run_tool([ffmpeg_exe, "-v", "error", "-i", filepath, "-f", "null", "-"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", timeout=60)
            err = (proc.stderr or "").strip()
            if proc.returncode != 0:
                return False, (err.splitlines()[0] if err else f"ffmpeg rc={proc.returncode}")[:200]
            if err:
                # Filter false positives like "error correction" / "error concealment" which are not decode errors
                low = err.lower()
                if "error" in low and "error correction" not in low and "error concealment" not in low and "error resilience" not in low:
                    return False, err.splitlines()[0][:200]
                # Also catch "invalid", "corrupt", "truncated" etc.
                if any(k in low for k in ("invalid", "corrupt", "truncated", "sync error", "crc mismatch")):
                    return False, err.splitlines()[0][:200]
            return True, None
        except subprocess.TimeoutExpired:
            return False, "ffmpeg timeout"
        except Exception as e:
            return False, str(e)[:200]

    # Fallback: try mutagen load to at least verify the file can be parsed
    try:
        af = AudioFile(filepath)
        if af.audio is None:
            return False, af.error or "unreadable"
        return True, None
    except Exception as e:
        return False, str(e)[:200]


def _audit_batch(cli, paths, config):
    """Run one AudioAuditorCLI analyze batch; returns parsed items."""
    cmd = [
        cli, "analyze", "--json",
        "--no-fun", "--no-tips", "--no-update-check", "--no-config",
    ]
    if config.get("audit_thorough", False):
        cmd.append("--thorough")
    else:
        cmd.append("--fast")

    cutoff = config.get("audit_cutoff_allow", 0)
    if cutoff:
        try:
            cmd += ["--cutoff-allow", str(int(cutoff))]
        except (ValueError, TypeError):
            pass

    for key, flag in DETECTOR_NO_FLAGS.items():
        if not config.get(key, True):
            cmd.append(flag)

    # AudioAuditorCLI 2.0.0 scan/analyze currently hangs for many files
    # (even a single 1-sec test flac times out after 60s with --json --fast).
    # Fall back to per-file `info` (which still works) when the batch times out.
    batch_timeout = int(config.get("audit_batch_timeout_s", 30) or 30)
    per_file_timeout = int(config.get("audit_per_file_timeout_s", 30) or 30)
    try:
        proc = run_tool(
            cmd,
            input="\n".join(paths) + "\n",
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=batch_timeout,
        )
    except subprocess.TimeoutExpired:
        # Fallback: `info` per file (the 2.0.0 batch scan hangs). One process
        # per file is I/O bound, so they run in parallel; `info` takes none of
        # the analyze flags — no detector toggles, no cutoff allowance, no
        # metrics — so these items carry the STATUS alone, and the caller must
        # not read a missing flag list as "no detector found anything".
        log(c(f"AudioAuditor batch timed out after {batch_timeout}s, falling back to per-file info (2.0.0 scan hang; status-only results, detector config not applied)", Color.YELLOW))
        import re as _re

        def _info_one(p):
            try:
                proc2 = run_tool([cli, "info", p], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=per_file_timeout)
                if proc2.returncode == 0 and proc2.stdout:
                    # `info` always succeeds and prints `Status: REAL/FAKE`.
                    m = _re.search(r"Status:\s*(REAL|FAKE)", proc2.stdout, _re.IGNORECASE)
                    status = ("Valid" if m.group(1).upper() == "REAL" else "Fake") if m else "Unknown"
                    return {"filePath": p, "fileName": os.path.basename(p), "status": status, "errorMessage": "", "statusOnly": True}
                return {"filePath": p, "fileName": os.path.basename(p), "status": "Unknown",
                        "errorMessage": (proc2.stderr or "")[:200], "statusOnly": True}
            except Exception as e:
                return {"filePath": p, "fileName": os.path.basename(p), "status": "Unknown",
                        "errorMessage": str(e)[:200], "statusOnly": True}

        workers = worker_count(config, default=8, maximum=16, items=len(paths))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            return list(ex.map(_info_one, paths))

    if not (proc.stdout or "").strip():
        err = (proc.stderr or "").strip()
        if "No supported audio files" in err:
            return []
        raise RuntimeError(f"AudioAuditorCLI failed (rc={proc.returncode}): "
                           f"{err[:200] or 'no output'}")

    try:
        items = json.loads(proc.stdout)
    except ValueError as e:
        raise RuntimeError(f"unparseable AudioAuditorCLI JSON output: {e}")

    if not isinstance(items, list):
        raise RuntimeError("unexpected AudioAuditorCLI output shape")
    return items


def _classify(item, config=None):
    """Return (severity, label): 'fail' | 'warn' | 'ok' and a reason."""
    status = str(item.get("status", "")).strip()
    err = str(item.get("errorMessage") or "").strip()

    if status == STATUS_REAL:
        flags = [label for key, label in FLAG_KEYS if item.get(key)]
        if config is not None and not config.get("audit_scaled_clipping", True):
            flags = [f for f in flags if f != "scaled clipping"]
        if flags:
            return "warn", ", ".join(flags)
        return "ok", ""
    if status == "Fake":
        cutoff = item.get("effectiveFrequency")
        detail = f"cutoff {cutoff} Hz" if cutoff else "spectral cutoff"
        return "fail", f"fake lossless ({detail})"
    if status == "Corrupt":
        return "fail", f"corrupt: {err or 'unreadable'}"
    if status == "Optimized":
        return "fail", "MQA (lossy 'optimized')"
    if status == "Unknown":
        # AudioAuditor could not answer: a timeout, a decode error, a file it
        # has no classifier for. That is not evidence of a fake rip, and it
        # must not become a verdict (see _audit_tag_value) — it is reported as
        # a warning and left for a later run with a working tool.
        if err:
            return "warn", f"unverified: {err}"
        return "warn", "unclassified"

    return "warn", f"status {status or '?'}"


def _audit_file_line(item):
    """Compact per-file console line with the key metrics."""
    parts = [
        f"{item.get('sampleRate', '?')} Hz",
        f"{item.get('bitsPerSample', '?')}-bit",
        f"{item.get('channels', '?')}ch",
        f"{item.get('actualBitrate', '?')} kbps",
    ]
    cutoff = item.get("effectiveFrequency")
    if cutoff:
        parts.append(f"cutoff {cutoff} Hz")
    rg = item.get("replayGain") if item.get("hasReplayGain") else None
    if rg is not None:
        parts.append(f"RG {rg} dB")
    return "  ".join(str(p) for p in parts)


def _audit_tag_value(severity, cli_status):
    """The AUDIT tag value for a file, or None when the tool said nothing.

    REAL when the CLI confirms genuine lossless (status Valid), FAKE for a
    confirmed failure (fake lossless, corrupt, MQA). "Unknown" — a timeout, a
    decode error, a file the CLI cannot classify — is NOT a verdict: writing
    FAKE for it made the non-answer permanent, because every later run skips a
    file that already carries a verdict. Returning None leaves the tag alone
    so a run with a working tool can still decide it, and grading reports the
    missing tag honestly.
    """
    if cli_status == STATUS_REAL:
        return "REAL"
    if cli_status in ("Fake", "Corrupt", "Optimized"):
        return "FAKE"
    return None


def _read_and_normalize_audit(path, write_tags=True, config=None):
    """Read the AUDIT verdict and fix legacy mixed-case values (Real -> REAL)
    in a single file open. Returns (verdict, changed)."""
    try:
        af = AudioFile(path)
        raw = str(af.get_tag("AUDIT") or "").strip()
        v = raw.upper()
        changed = False
        if write_tags and raw and v in ("REAL", "FAKE") and raw != v:
            # Respect per-filetype AUDIT toggle
            if config is None or should_write_audio_tag(config, "AUDIT", filepath=path):
                changed = bool(af.set_tag("AUDIT", v))
        return (v if v in ("REAL", "FAKE") else None), changed
    except Exception:
        return None, False


# ----------------------------------------------------------------------
# Verdict evidence: which file a stored AUDIT verdict was decided for
# ----------------------------------------------------------------------
# "REAL" is a statement about the BYTES AudioAuditor read. Replace or
# re-encode the file and the tag still says REAL about audio nothing audited —
# and the next run SKIPS it on that stale verdict. The size and mtime each
# verdict was written for live in <music>/.mlo/data/audit_evidence.json (the
# same place and shape as the on-demand ReplayGain cache, mlo.loudness) and a
# file whose bytes no longer match is audited again instead of skipped.
#
# ponytail: one JSON file rewritten once per run, entries dropped only when
# their file is gone; move it to sqlite if a huge library ever makes that hurt.
EVIDENCE_NAME = "audit_evidence.json"
_EVIDENCE = {}


def _ev_key(path):
    """Evidence key: canonical path, the spelling the CD maps also use."""
    try:
        return os.path.normcase(os.path.realpath(path))
    except OSError:
        return os.path.normcase(path)


def _file_stamp(path):
    """[size, mtime_ns] of *path*, or None when it cannot be stat'ed."""
    try:
        st = os.stat(path)
        return [st.st_size, st.st_mtime_ns]
    except OSError:
        return None


def _evidence_path(config):
    """<music>/.mlo/data/audit_evidence.json, or "" when there is no folder."""
    try:
        return os.path.join(app_data_dir(config.get("music_folder")),
                            EVIDENCE_NAME)
    except Exception:
        return ""


def _load_evidence(config):
    """The stored {path: [size, mtime_ns]} map; unreadable reads as empty.

    Entries whose file is gone are dropped here: the map is per library, and a
    deleted album must not leave its stamps behind forever."""
    path = _evidence_path(config)
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if isinstance(v, list) and os.path.exists(k)}


def _save_evidence(config, evidence):
    """Atomic write of the evidence map; never raises."""
    path = _evidence_path(config)
    if not path:
        return
    tmp = None
    try:
        d = os.path.dirname(path)
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".audit_evidence_", suffix=".json",
                                   dir=d)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(evidence, f)
            try:
                f.flush()
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    except OSError:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def _write_audit_tag(path, value):
    """Write the AUDIT tag when it differs. Returns
    (changed: bool, b_rem: int, b_add: int, error: str | None)."""
    try:
        before = os.path.getsize(path)
    except OSError as e:
        return False, 0, 0, f"stat: {e}"

    try:
        af = AudioFile(path)
        if af.audio is None:
            return False, 0, 0, f"load: {af.error}"

        cur = af.get_tag("AUDIT")
        cur_clean = str(cur).strip() if cur is not None else ""
        if cur_clean.lower() == value.lower():
            # The file already carries this verdict and this run just re-read
            # it against these bytes: renew the evidence so the next run may
            # skip it (a stale stamp was the reason this file was audited).
            _EVIDENCE[_ev_key(path)] = _file_stamp(path)
            return False, 0, 0, None

        if not af.set_tag("AUDIT", value):
            return False, 0, 0, f"write: {af.error}"

        after = os.path.getsize(path)
        b_rem, b_add = _diff_bytes(before, after)
        _EVIDENCE[_ev_key(path)] = _file_stamp(path)
        return True, b_rem, b_add, None
    except Exception as e:
        return False, 0, 0, str(e)


def run_audit_library(config):
    folder = config["music_folder"]
    thorough = config.get("audit_thorough", False)

    stats = new_stats()
    stats["is_grader"] = True
    stats["grade_dist"] = {"PASS": 0, "FAIL": 0}

    print_header("Audio Auditor (AudioAuditorCLI)")
    log(f"music folder: {folder} · thorough={thorough} · writes AUDIT tags")

    tools = detect_all_tools()
    aa = tools.get("audioauditor")
    cli = aa["cli_exe"] if aa else None
    if cli:
        log(f"cli: {cli} · v{aa['version']}")
    else:
        # AudioAuditor is Windows-only and optional EVIDENCE: the .log CRC,
        # the .accurip and the integrity test are the checks that decide a CD,
        # and they need no AudioAuditor. Returning here skipped the whole
        # audit — CD verification, rip-log grades, AccurateRip — for a missing
        # extra tool. The spectral pass below is skipped instead, and files it
        # would have decided simply keep no verdict (see _audit_tag_value).
        log(c("WARNING: AudioAuditorCLI not found in the .dependencies folder "
              "- the spectral pass is skipped, the CD checks below still run.",
              Color.YELLOW))
        log(f"Expected a folder like: {os.path.join(DEPS_DIR, 'AudioAuditor v2.0.0')}")
        log("Use Dependencies (GUI sidebar → MANAGE) to download it.")
        stats["errors"].append(("AudioAuditorCLI",
                                "not found - spectral audit skipped"))

    if not os.path.isdir(folder):
        log(c(f"ERROR: folder does not exist: {folder}", Color.RED))
        return stats

    targets = config.get("targets")
    files = _collect_targets(targets, AUDIO_EXTS)
    if targets is None:
        files = sorted(_walk_files(folder, AUDIO_EXTS))
    if not files:
        log("No audio files found.")
        return stats

    force = config.get("force_audit", False)
    verbose = config.get("grade_verbose", True)

    # The verdict-evidence map for this run, loaded BEFORE anything writes a
    # verdict (the CD phases below write some) and saved once at the end.
    _EVIDENCE.clear()
    _EVIDENCE.update(_load_evidence(config))

    # ------------------------------------------------------------------
    # One MEDIA=CD pass for the whole run. Four separate phases (CD checksum
    # verification, rip-log candidates, AccurateRip candidates, CD format)
    # each used to re-list every album folder and re-open every file's tags to
    # ask the same question — four full tag reads per album on a CD library.
    # The map answers all four, and cd_files (the album-level set) and
    # cd_files_flagged (the per-file MEDIA=CD set) both fall out of it.
    # ------------------------------------------------------------------
    by_album_files = {}
    for p in files:
        by_album_files.setdefault(os.path.dirname(p), []).append(p)

    # The library's own spelling of each file, keyed by canonical path.
    # AudioAuditor reports its own spelling (8.3 short names on Windows, case
    # variants), and a DISC NUMBER can only be read off a name we know — a
    # short name has no "1-01" prefix, which silently turned the per-disc
    # .accurip lookup into the album-wide one it replaced.
    canon_files = {_ev_key(p): p for p in files}

    cd_album_map = {}
    cd_files_flagged = set()
    for album_dir, paths in by_album_files.items():
        is_cd = False
        for pp in paths:
            try:
                af_tmp = AudioFile(pp)
                if af_tmp.audio is None:
                    continue
                if str(af_tmp.get_tag("MEDIA") or "").strip() == "CD":
                    cd_files_flagged.add(pp)
                    is_cd = True
            except Exception:
                continue
        cd_album_map[album_dir] = is_cd

    # ------------------------------------------------------------------
    # CD rip verification — the .log CRC is the AUTHORITATIVE integrity source
    # for MEDIA=CD: a rip whose printed CRCs match the audio is REAL even when
    # AudioAuditor's spectral read disagrees (a synthetic tone, an unusual
    # master — AudioAuditor is not a fact). `audit_cd_require_both` decides
    # only whether AudioAuditor is ALSO run over CD files, where its warnings
    # are kept and its verdict decides for a disc neither the .log nor a REAL
    # .accurip could verify. Files that cannot be verified get NO verdict at
    # all (grading fails them). AudioAuditor is otherwise never run on CD
    # rips; it is reserved for every other release type.
    # ------------------------------------------------------------------
    # Default True, matching mlo.config.DEFAULT_CONFIG: a partial cfg must
    # not audit a CD more leniently than the shipped app does.
    require_both = bool(config.get("audit_cd_require_both", True))
    cd_files = set()
    unverified_cd = {}
    checksum_verified = {}
    checksum_verified_canon = {}
    # Album dir -> {disc number (None when the name carries none): verdict}.
    # Parsing is a text read, so this runs even when the log CRC cannot be
    # computed. Per DISC, not per album: the album-wide version marked every
    # track REAL as soon as ANY .accurip of the album verified, so a second
    # disc of a repack inherited the first disc's verdict.
    ar_disc_verdicts = {}

    def _album_accurip_verdicts(album_dir):
        cached = ar_disc_verdicts.get(album_dir)
        if cached is not None:
            return cached
        from .accurip import parse_accurip_status
        from .discs import _log_name_disc
        verdicts = {}
        try:
            names = sorted(os.listdir(album_dir))
        except OSError:
            names = []
        for name in names:
            if not name.lower().endswith(".accurip"):
                continue
            try:
                with open(os.path.join(album_dir, name), "r",
                          encoding="utf-8", errors="replace") as fh:
                    status, _detail = parse_accurip_status(fh.read())
            except OSError:
                continue
            if status not in ("REAL", "FAKE"):
                continue
            ok = status == "REAL"
            key = _log_name_disc(name)
            verdicts[key] = verdicts[key] and ok if key in verdicts else ok
        ar_disc_verdicts[album_dir] = verdicts
        return verdicts

    def _disc_accurip_verified(album_dir, path):
        """The .accurip verdict for the disc *path* itself belongs to.

        A numbered log (CD-2.accurip) is looked up by the file's own D-TT
        number, so disc 2 can no longer pass on disc 1's verdict. A track
        whose name carries no disc number — a plain "01 Title.flac", or an
        album with no D-TT structure at all — falls back to the album-wide
        answer, which is exactly the one .accurip such an album has.
        """
        verdicts = _album_accurip_verdicts(album_dir)
        if not verdicts:
            return False
        from .discs import disc_of_filename
        disc_n = disc_of_filename(os.path.basename(path))
        if disc_n is not None and disc_n in verdicts:
            return verdicts[disc_n] is True
        if disc_n is None:
            return all(verdicts.values())
        return False
    # Whether a CD could be verified at all on this machine, and whether
    # CUETools could make the .accurip in the first place: both decide later
    # whether "not verified" is evidence or just a missing tool.
    from .accurip import resolve_arcue_exe
    ffmpeg_exe_for_cd = (tools.get("ffmpeg") or {}).get("ffmpeg_exe")
    ar_generator = bool(resolve_arcue_exe(tools)) and bool(ffmpeg_exe_for_cd)
    cd_verify_ran = False
    cd_files = {p for a, paths in by_album_files.items()
                if cd_album_map.get(a) for p in paths}
    # Canonical spelling of the same set: AudioAuditor echoes the path in its
    # own form (case, separators, 8.3), so membership is tested canonically.
    cd_canon = {_ev_key(p) for p in cd_files}
    if config.get("audit_verify_cd_checksums", True):
        # Reuse already-detected tools to avoid redundant GitHub cache lookup
        if not ffmpeg_exe_for_cd:
            log(c("WARNING: ffmpeg not found - CD checksum verification "
                  "unavailable.", Color.YELLOW))
            stats["errors"].append(("CD checksum", "ffmpeg not found"))
        else:
            cd_verify_ran = True
            from .discs import verify_album_checksums
            from concurrent.futures import as_completed

            cd_albums = {a: ps for a, ps in by_album_files.items()
                         if cd_album_map.get(a)}
            cw = worker_count(config, default=4, maximum=8,
                              items=len(cd_albums))
            # A bar for the CRC pass: it decodes every track of every CD, and
            # without one the header sat frozen for the whole phase.
            crc_counts = {"ok": 0, "skip": 0, "fail": 0}
            crc_pbar = _make_pbar(len(cd_albums), "CD checksums", unit="album")
            with ThreadPoolExecutor(max_workers=cw) as ex:
                futures = {
                    ex.submit(verify_album_checksums, ffmpeg_exe_for_cd,
                              album, paths, config): album
                    for album, paths in cd_albums.items()
                }
                for fut in as_completed(futures):
                    album = futures[fut]
                    try:
                        res, unver = fut.result()
                    except Exception as e:
                        stats["errors"].append((os.path.basename(album), f"checksum verify: {e}"))
                        _pbar_update(crc_pbar, crc_counts, kind="fail")
                        continue
                    _pbar_update(crc_pbar, crc_counts)
                    for path, verdict in res.items():
                        # The .log CRC is written as soon as it is known: a
                        # verified rip must carry REAL even when AudioAuditor
                        # is missing (it is a Windows-only tool) or its
                        # spectrogram detectors disagree. AA can only ADD
                        # warning flags to a file the log could not verify.
                        if config.get("write_audit_tag", True) and should_write_audio_tag(config, "AUDIT", filepath=path):
                            changed, b_rem, b_add, err = _write_audit_tag(path, verdict)
                            if err:
                                stats["errors"].append((os.path.basename(path), err))
                            elif changed:
                                stats["modified_count"] += 1
                                stats["total_bytes_removed"] += b_rem
                                stats["total_bytes_added"] += b_add
                        checksum_verified[path] = verdict
                        checksum_verified_canon[_ev_key(path)] = verdict
                    unverified_cd.update(unver)
            if crc_pbar:
                crc_pbar.close()

            if cd_files:
                n_ar = sum(1 for a in cd_albums
                           if any(_album_accurip_verdicts(a).values()))
                if n_ar:
                    log(f"AccurateRip: {n_ar} album(s) verified — those discs "
                        f"are REAL on that evidence alone")
                n_real = sum(1 for v in checksum_verified.values()
                             if v == "REAL")
                n_fake = len(checksum_verified) - n_real
                if require_both:
                    log(f"CD checksums: {n_real} REAL, {n_fake} FAKE "
                        f"({len(unverified_cd)} unverified of "
                        f"{len(cd_files)} CD track(s) — will be combined with AudioAuditor)")
                else:
                    log(f"CD checksums: {n_real} REAL, {n_fake} FAKE "
                        f"({len(unverified_cd)} unverified of "
                        f"{len(cd_files)} CD track(s) - .log CRC is the only "
                        f"audit source for MEDIA=CD)")
                if n_fake:
                    for p in sorted(checksum_verified):
                        if checksum_verified[p] == "FAKE":
                            try:
                                rel = os.path.relpath(p, folder)
                            except ValueError:
                                rel = os.path.join(os.path.basename(os.path.dirname(p)), os.path.basename(p))
                            log(f"  {c('✕', Color.RED)} "
                                 f"{rel} "
                                 f"{c('FAKE (CRC mismatch vs .log)', Color.RED)}")
                if unverified_cd and verbose:
                    for p in sorted(unverified_cd)[:20]:
                        log(f"  {c('–', Color.GREY)} {os.path.basename(p)} "
                            f"{c(unverified_cd[p], Color.GREY)}")

    # ------------------------------------------------------------------
    # Integrity check (foobar2000 Verify Integrity style) — optional but on
    # by default. Uses `flac -t` for FLAC and `ffmpeg -v error` for all
    # types to catch truncated files, frame CRC mismatches, and sync errors.
    # Failures here make the final AUDIT FAKE, just like a fake lossless
    # detection, and are all configurable via Settings → Audit.
    # ------------------------------------------------------------------
    integrity_failed = {}
    if config.get("audit_integrity", True):
        ffmpeg_exe = (tools.get("ffmpeg") or {}).get("ffmpeg_exe")
        flac_exe = (tools.get("flac") or {}).get("flac_exe")
        if not flac_exe and not ffmpeg_exe:
            # Without a decoder this pass could only re-parse each file's tags
            # and then report "all files passed verification" — a claim about
            # audio nothing tested. Nothing is checked, and the log says so.
            log(c(f"Integrity: no flac/ffmpeg available - {len(files)} file(s) "
                  f"NOT verified (no decoder to test them with)", Color.YELLOW))
            stats["errors"].append(("Integrity", "no flac/ffmpeg to verify with"))
        else:
            # Only verify files that would be audited anyway (respect force/skip later)
            # But run it now so we can fail fast and avoid an expensive AudioAuditor run
            # on a file that is already corrupt.
            def _check_one(p):
                ok, err = verify_integrity(p, ffmpeg_exe, flac_exe)
                return p, ok, err

            cw = worker_count(config, default=8, maximum=16, items=len(files))
            with ThreadPoolExecutor(max_workers=cw) as ex:
                futs = {ex.submit(_check_one, p): p for p in files}
                from concurrent.futures import as_completed as _as_comp
                for fut in _as_comp(futs):
                    p, ok, err = fut.result()
                    if not ok:
                        integrity_failed[p] = err or "integrity check failed"

            if integrity_failed:
                n_fail = len(integrity_failed)
                log(c(f"Integrity: {n_fail} file(s) failed verification (foobar2000 style) — will be AUDIT=FAKE", Color.RED))
                for p in sorted(integrity_failed)[:10]:
                    try:
                        rel = os.path.relpath(p, folder)
                    except ValueError:
                        rel = os.path.basename(p)
                    log(f"  {c('✕', Color.RED)} {rel} {c(integrity_failed[p][:80], Color.RED)}")
                if n_fail > 10:
                    log(f"  … and {n_fail - 10} more")
            else:
                log("Integrity: all files passed verification")

    # A CD rip's verdict comes from its OWN verification, never from the
    # spectrogram detectors: the .log CRC (written above, as soon as it is
    # known) or a REAL .accurip. `audit_cd_require_both` only decides whether
    # AudioAuditor is ALSO run over MEDIA=CD — its verdict can add warning
    # flags, and decides only for a disc neither source could verify.

    # Skip files that already carry a REAL/FAKE verdict (normalizing
    # legacy mixed-case values) unless the audit is forced.
    # When require_both, CD files are NOT skipped — they need the second source.
    todo = files
    skipped = 0
    stale = 0
    if not force:
        workers = worker_count(config, default=8, maximum=8, items=len(files))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(
                lambda path: _read_and_normalize_audit(
                    path, config.get("write_audit_tag", True), config
                ),
                files,
            ))
        todo = []
        for path, (verdict, changed) in zip(files, results):
            if verdict is not None:
                # CD files are re-checked even when a tag exists: they carry
                # the log/AccurateRip verdict, which outranks the stored one —
                # but only when one of those checks can actually run.
                if path in cd_files and (require_both or cd_verify_ran):
                    todo.append(path)
                    continue
                # A verdict is only trusted for the bytes it was written for.
                # A re-encoded or replaced file keeps the old tag, and this
                # stamp check is what stops that stale REAL from being skipped
                # forever; a file with no recorded stamp (audited before the
                # evidence existed) is audited once more and then has one.
                stamp = _file_stamp(path)
                if stamp is not None and _EVIDENCE.get(_ev_key(path)) == stamp:
                    skipped += 1
                    if changed:
                        stats["modified_count"] += 1
                    continue
                stale += 1
                todo.append(path)
            else:
                todo.append(path)
        if skipped:
            # counted as SKIPPED, not as a warning or a failure: the reported
            # pass rate is Real/total, and a skipped file never was a warning.
            stats["skipped_count"] += skipped
            log(f"skipping {skipped} file(s) already carrying an AUDIT verdict "
                f"for the file's current bytes (force audit overrides)")
        if stale:
            log(c(f"{stale} file(s) carry an AUDIT verdict with no matching "
                  f"evidence (replaced, re-encoded, or audited before the "
                  f"evidence was recorded) - auditing them again",
                  Color.YELLOW))

    if not require_both:
        # Only skip CD files that were successfully verified via log CRC.
        # Unverified CD files (no .log, no CRC, or log missing) still need
        # AudioAuditor — otherwise they'd never be audited and grading would
        # always fail them for missing AUDIT.
        todo = [p for p in todo if p not in checksum_verified]
        if cd_files:
            n_unverified = len(unverified_cd)
            n_verified = len(checksum_verified)
            log(f"CD rips ({len(cd_files)} track(s)) are verified via .log "
                f"checksums only - AudioAuditor not applied to MEDIA=CD "
                f"({n_verified} verified, {n_unverified} unverified will be audited).")
    else:
        # require_both: CD files stay in todo even when checksum-verified (the
        # AA run is what adds warning flags); unverified ones stay too — for
        # them AudioAuditor is the only verdict there is.
        if cd_files:
            log(f"CD rips ({len(cd_files)} track(s)): the .log CRC / .accurip "
                f"decides the verdict; AudioAuditor adds warnings only.")

    # Integrity failures that were skipped due to already having an AUDIT tag
    # still need to be handled — if a file is corrupt, its AUDIT must be FAKE
    # even if it already says REAL. Respect per-filetype toggle.
    if config.get("audit_integrity", True) and integrity_failed:
        for p, err in list(integrity_failed.items()):
            if p not in todo and p in files and should_write_audio_tag(config, "AUDIT", filepath=p):
                todo.append(p)

    if cli is None:
        # No spectral tool. Everything the .log CRC, the .accurip and the
        # integrity test proved is already written above; the files this pass
        # would have decided keep NO verdict, so a later run with the tool
        # installed can still decide them. Integrity failures are evidence and
        # are written here, because the loop below will not run.
        if config.get("audit_integrity", True):
            for p, err in integrity_failed.items():
                if p not in files or not should_write_audio_tag(
                        config, "AUDIT", filepath=p):
                    continue
                changed, b_rem, b_add, tag_err = _write_audit_tag(p, "FAKE")
                if tag_err:
                    stats["errors"].append((os.path.basename(p), tag_err))
                elif changed:
                    stats["modified_count"] += 1
                    stats["total_bytes_removed"] += b_rem
                    stats["total_bytes_added"] += b_add
        log(c(f"AudioAuditorCLI not installed: {len(todo)} file(s) are left "
              f"without an AUDIT verdict (no spectral evidence to write)",
              Color.YELLOW))
        todo = []

    log(f"auditing {len(todo)} file(s) · fast scan "
        f"{'off (--thorough)' if thorough else 'on'}")
    todo_paths = set(todo)

    counts = {"ok": 0, "skip": 0, "fail": 0}
    status_counts = {"Real": 0, "Fake": 0, "Unknown": 0,
                     "Corrupt": 0, "Optimized": 0}
    issue_counts = {}
    flagged = []
    warned = 0
    pbar = _make_pbar(len(todo), "Auditing", unit="file")
    batch_size = int(config.get("audit_batch_size", BATCH_SIZE) or BATCH_SIZE)
    batch_size = max(50, min(500, batch_size))
    # Track per-file severity/status for later unscorable-log handling
    file_severity_map = {}
    file_status_map = {}

    for start in range(0, len(todo), batch_size):
        batch = todo[start:start + batch_size]
        try:
            items = _audit_batch(cli, batch, config)
        except Exception as e:
            stats["total_scanned"] += len(batch)
            stats["error_count"] += len(batch)
            stats["errors"].append((f"batch {start // batch_size + 1} "
                                     f"({len(batch)} files)", str(e)))
            log(c(f"Audit batch failed: {e}", Color.RED))
            counts["fail"] += len(batch)
            if pbar is not None:
                try:
                    pbar.update(len(batch))
                    pbar.set_postfix(ok=counts["ok"], skip=counts["skip"], fail=counts["fail"])
                except Exception:
                    pass
            continue

        # Files the CLI silently dropped (unsupported/renamed). Paths
        # are compared via realpath so 8.3 short names don't cause
        # phantom misses when the CLI echoes back the long form (or
        # vice versa).
        def canon(p):
            if not p:
                return ""
            try:
                return os.path.normcase(os.path.realpath(p))
            except OSError:
                return os.path.normcase(p)

        missing = {canon(p) for p in batch}
        canon_failed = {canon(k): v for k, v in integrity_failed.items()} if config.get("audit_integrity", True) else {}
        for item in items:
            path = item.get("filePath") or ""
            missing.discard(canon(path))
            severity, reason = _classify(item, config)
            cli_status = str(item.get("status", "")).strip()

            stats["total_scanned"] += 1
            try:
                rel = os.path.relpath(path, folder) if path else item.get(
                    "fileName", "?")
            except ValueError:
                rel = os.path.basename(path) if path else item.get(
                    "fileName", "?")

            # CLI verdict drives the status counts; warnings (clipping
            # flags etc. on an otherwise Valid file) are tracked apart.
            skey = {"Valid": "Real", "Fake": "Fake", "Corrupt": "Corrupt",
                     "Optimized": "Optimized"}.get(cli_status, "Unknown")
            status_counts[skey] += 1
            # Track for unscorable-log FAIL handling later
            file_severity_map[canon(path)] = severity
            file_status_map[canon(path)] = skey

            # Base AA tag value before CD combination (None = no verdict: the
            # CLI could not decide, and a non-answer must not become FAKE).
            tag_value = _audit_tag_value(severity, cli_status)

            # When bothrequired, the final AUDIT is the AND of the two sources.
            # checksum must be REAL and AA must be Valid/REAL; otherwise FAKE.
            # .log CRC is authoritative, so an unverified log also means FAKE.
            # Preserve warning flags (Valid+clipping etc.) when both are REAL.
            # Every CD file enters this branch, not only the ones the CRC pass
            # reached: with no ffmpeg to decode them the CRC map is empty, and
            # gating on it left AudioAuditor's spectral read as the whole
            # verdict for a MEDIA=CD library.
            if require_both and canon(path) in cd_canon:
                # Look every CD verdict up by its canonical path: the tool
                # reports its own spelling of the same file (case, separator,
                # 8.3 name), and a raw string compare silently missed it —
                # which left the AA verdict standing on its own.
                _ck = canon(path)
                chk = checksum_verified_canon.get(_ck)
                orig_severity = severity
                orig_reason = reason
                # Integrity first: a rip whose .log CRC verifies is REAL, and
                # so is one whose .accurip verifies. The CRC is authoritative
                # for a CD — the user's rule, and what makes a synthetic-tone
                # fixture survive a real AudioAuditor's "fake lossless"
                # verdict. `audit_cd_require_both` decides only whether AA is
                # ALSO run over these files (its warnings are kept, and it
                # decides when neither source verified); it is not a veto, and
                # the branch that read it as one was unreachable — `chk !=
                # "REAL"` is the exact negation of this test.
                _known = canon_files.get(_ck, path)
                _album_dir = os.path.dirname(_known)
                if chk == "REAL" or _disc_accurip_verified(_album_dir, _known):
                    tag_value = "REAL"
                    severity = "warn" if orig_severity == "warn" else "ok"
                    reason = orig_reason if severity == "warn" else ""
                elif cd_verify_ran or _album_accurip_verdicts(_album_dir) or ar_generator:
                    tag_value = "FAKE"
                    severity = "fail"
                    reason = f"CD log not REAL ({unverified_cd.get(path, chk or 'no CRC')})"
                else:
                    # Nothing on this machine could verify a CD: no ffmpeg for
                    # the .log CRC, no .accurip to read and no CUETools to make
                    # one. "Cannot check" is not "did not match" — keep whatever
                    # AudioAuditor itself said (often no verdict at all) instead
                    # of writing a FAKE that every later run then skips.
                    pass
                # Ensure status counts reflect the AA side already counted;
                # the final tag is what grading will use.

            # Integrity check — use canon for Windows 8.3 / case variant safety
            if canon(path) in canon_failed and should_write_audio_tag(config, "AUDIT", filepath=path):
                tag_value = "FAKE"
                severity = "fail"
                reason = f"integrity check failed: {canon_failed[canon(path)]}"

            # Persist the verdict into the file's AUDIT tag (respects
            # per-filetype). No value means no evidence: the tag is left
            # exactly as it was, so a later run (with the tool installed, or
            # the timeout past) can still decide the file.
            if (tag_value is not None and config.get("write_audit_tag", True)
                    and should_write_audio_tag(config, "AUDIT", filepath=path)):
                changed, b_rem, b_add, tag_err = _write_audit_tag(
                    path, tag_value)
            else:
                changed, b_rem, b_add, tag_err = False, 0, 0, None
            if tag_err:
                log(c(f"    [tag warn] {os.path.basename(rel)}: {tag_err}",
                      Color.YELLOW))
            elif changed:
                stats["modified_count"] += 1
                stats["total_bytes_removed"] += b_rem
                stats["total_bytes_added"] += b_add

            # The metrics line is AudioAuditor's own reading: the `info`
            # fallback carries the status only, and printing "? Hz ?-bit" as
            # if it were a measurement is worse than saying nothing.
            metrics = "" if item.get("statusOnly") else _audit_file_line(item)

            if severity == "ok":
                if verbose:
                    log(f"{c('✓', Color.GREEN)} {rel}")
            elif severity == "warn":
                # A warning is neither a skip nor a failure: it is counted on
                # its own (stats["audit_warned"] below) so the reported pass
                # rate stays Real/total.
                warned += 1
                issue_counts[reason] = issue_counts.get(reason, 0) + 1
                log(f"{c('!', Color.YELLOW)} {rel}  "
                    f"{c(reason, Color.YELLOW)}")
                if verbose and metrics:
                    log(f"    {metrics}")
            else:
                stats["grade_dist"]["FAIL"] += 1
                stats["errors"].append((rel, reason))
                base_reason = reason.split(" (")[0]
                issue_counts[base_reason] = issue_counts.get(base_reason, 0) + 1
                flagged.append((rel, reason))
                log(f"{c('✕', Color.RED)} {rel}  {c(reason, Color.RED)}")
                if metrics:
                    log(f"    {metrics}")

        # Files the CLI silently dropped (unsupported/renamed).
        for gone in missing:
            stats["total_scanned"] += 1
            stats["skipped_count"] += 1
            status_counts["Unknown"] += 1
            try:
                rel_gone = os.path.relpath(gone, folder)
            except ValueError:
                rel_gone = os.path.basename(gone)
            log(f"{c('–', Color.GREY)} {rel_gone} "
                f"{c('(no audit result)', Color.GREY)}")

        if pbar is not None:
            try:
                pbar.update(len(batch))
            except Exception:
                pass

    if pbar:
        pbar.close()

    # Rip-log grading for MEDIA=CD albums: rename logs/cues to CD-N and
    # write each disc's 0-100 cambia score to its tracks' LOG_GRADE.
    # Parallelized across albums - each disc scores in its own CLI process,
    # so thread workers scale instead of running one album at a time.
    album_dirs = sorted(by_album_files)
    # Pre-filter to albums that could be CD rips (contain .log and MEDIA==CD) to avoid
    # spawning thousands of no-op threads for non-CD libraries (5k albums = 5k futures).
    # The MEDIA answer comes from the single pass above, not from re-opening tags.
    cd_candidate_dirs = []
    for d in album_dirs:
        try:
            if not any(f.lower().endswith(".log") for f in os.listdir(d)):
                continue
        except OSError:
            continue
        if cd_album_map.get(d):
            cd_candidate_dirs.append(d)
    log_scores = {}
    log_notes = []
    log_unscorable = {}   # album dir -> disc numbers whose .log could not be scored
    from .discs import grade_album_logs, logchecker_available
    from concurrent.futures import as_completed
    # A missing scorer and an unscorable log are different things: with
    # Logchecker/PHP not installed nothing judged the logs at all, and the
    # gate below must not read that as "every CD log of this library is
    # broken" (it used to write AUDIT=FAKE for the whole CD library).
    scorer_present = logchecker_available()

    def _grade_one(album_dir):
        if not os.path.isdir(album_dir):
            return album_dir, {}, [], []
        scores, notes, unscorable = grade_album_logs(
            cli, album_dir, force=force,
            write_tags=config.get("write_log_grade", True),
            config=config,
            log_fn=(lambda m: log(f"  {m}")) if verbose else None)
        return album_dir, scores, notes, unscorable

    if cd_candidate_dirs:
        workers = worker_count(config, default=8, maximum=8, items=len(cd_candidate_dirs))
        # One tick per album: scoring spawns a PHP process per disc, and the UI
        # header follows this bar.
        log_counts = {"ok": 0, "skip": 0, "fail": 0}
        log_pbar = _make_pbar(len(cd_candidate_dirs), "Rip-log grades", unit="album")
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(_grade_one, d) for d in cd_candidate_dirs]
            for fut in as_completed(futures):
                album_dir, scores, notes, unscorable = fut.result()
                _pbar_update(log_pbar, log_counts, kind="ok" if scores else "skip")
                if scores:
                    log_scores[album_dir] = scores
                if unscorable:
                    log_unscorable[album_dir] = unscorable
                log_notes.extend(f"{os.path.basename(album_dir)}: {n}" for n in notes)
        if log_pbar:
            log_pbar.close()

    if log_scores:
        log("")
        log("Rip-log grades (LOG_GRADE):")
        for album_dir, scores in log_scores.items():
            parts = " · ".join(f"CD-{d} = {s}/100"
                               for d, s in sorted(scores.items()))
            log(f"  {os.path.basename(album_dir)}: {parts}")
    if log_notes:
        for n in log_notes[:20]:
            log(c(f"  [log] {n}", Color.YELLOW))
        if len(log_notes) > 20:
            log(f"  … and {len(log_notes) - 20} more.")

    # Audit FAIL on unscorable logs — user request: every CD .log must be gradeable
    def _canon2(p):
        try:
            return os.path.normcase(os.path.realpath(p))
        except OSError:
            return os.path.normcase(p)

    # The one "this file failed" path shared by every CD gate below. Each gate
    # used to carry its own copy of the count adjustment (status_counts,
    # warned, skipped_count, issue counts, grade_dist, flagged) and they had
    # already drifted — some moved the warn count, some did not, and two could
    # double-count a file an earlier gate had failed. Returns False when the
    # file was already a failure, so a second reason is recorded without
    # re-counting it.
    def mark_fake(fp, reason, issue=None):
        nonlocal warned
        canon_fp = _canon2(fp)
        key = issue or reason
        if (file_status_map.get(canon_fp) == "Fake"
                and file_severity_map.get(canon_fp) == "fail"):
            issue_counts[key] = issue_counts.get(key, 0) + 1
            return False
        try:
            rel = os.path.relpath(fp, folder)
        except ValueError:
            rel = os.path.basename(fp)
        prev_status = file_status_map.get(canon_fp)
        prev_sev = file_severity_map.get(canon_fp)
        if canon_fp in file_status_map:
            if prev_status == "Real":
                status_counts["Real"] = max(0, status_counts.get("Real", 0) - 1)
                status_counts["Fake"] = status_counts.get("Fake", 0) + 1
                if prev_sev == "warn":
                    warned = max(0, warned - 1)
            elif prev_status == "Unknown":
                status_counts["Unknown"] = max(0, status_counts.get("Unknown", 0) - 1)
                status_counts["Fake"] = status_counts.get("Fake", 0) + 1
            elif prev_status not in ("Fake", "Corrupt", "Optimized"):
                status_counts[prev_status] = max(0, status_counts.get(prev_status, 0) - 1)
                status_counts["Fake"] = status_counts.get("Fake", 0) + 1
        else:
            # Skipped (it already carried an AUDIT tag): it was counted as
            # skipped, not as a result, and it becomes a failure now.
            if stats.get("skipped_count", 0) > 0:
                stats["skipped_count"] = max(0, stats["skipped_count"] - 1)
            stats["total_scanned"] = stats.get("total_scanned", 0) + 1
            status_counts["Fake"] = status_counts.get("Fake", 0) + 1
        flagged.append((rel, reason))
        issue_counts[key] = issue_counts.get(key, 0) + 1
        stats["grade_dist"]["FAIL"] = stats["grade_dist"].get("FAIL", 0) + 1
        file_status_map[canon_fp] = "Fake"
        file_severity_map[canon_fp] = "fail"
        _write_fake(fp)
        return True

    def _write_fake(fp):
        """Persist AUDIT=FAKE for a gate verdict (respects per-filetype)."""
        if config.get("write_audit_tag", True) and should_write_audio_tag(
                config, "AUDIT", filepath=fp):
            try:
                _write_audit_tag(fp, "FAKE")
            except Exception:
                pass

    if config.get("audit_fail_on_unscorable_log", True) and cd_candidate_dirs:
        # Without a scorer only the MISSING .log still fails: an absent file is
        # evidence no scoring tool can change, while "the log is there but
        # could not be scored" is a property of the missing scorer. Treating
        # both as failures wrote AUDIT=FAKE for every CD track of a library
        # that merely has no PHP installed.
        scorer_needed = not scorer_present
        if scorer_needed:
            log(c("Unscorable-log failures are limited to discs with NO .log: "
                  "Logchecker/PHP is not installed, so the logs that are there "
                  "could not be scored (install it from Dependencies and re-run)",
                  Color.YELLOW))
            stats["errors"].append(("LOG_GRADE", "Logchecker/PHP not installed - logs not scored"))
        if log_unscorable:
            # Per-disc, straight from grade_album_logs. The disc numbers used
            # to be recovered by regexing its free-text notes ("disc 2: could
            # not score"), which mixed the album name with a disc number and
            # could fail tracks of the wrong disc.
            from .discs import (album_discs as _ad_unsc, _disc_pattern_for as _pat_unsc,
                                _disc_expected_name as _exp_unsc)
            pat_unsc = _pat_unsc(config)
            failed_discs = {}
            for d, disc_list in log_unscorable.items():
                for disc_n in disc_list:
                    if scorer_needed and os.path.isfile(
                            os.path.join(d, _exp_unsc(pat_unsc, disc_n, ".log"))):
                        continue
                    failed_discs.setdefault(d, []).append(disc_n)
            if failed_discs:
                total_discs = sum(len(v) for v in failed_discs.values())
                log(c(f"Audit FAIL on unscorable logs: {total_discs} disc(s) in {len(failed_discs)} CD album(s) have .log that could not be graded — marking only those disc(s) as failed (per-disc, audit_fail_on_unscorable_log on)", Color.RED))
                for d, disc_list in failed_discs.items():
                    try:
                        discs_here = _ad_unsc(d)
                    except Exception:
                        discs_here = {}
                    for disc_n in disc_list:
                        for fp in discs_here.get(disc_n, ()):
                            if fp not in files:
                                continue
                            # The log's GRADE measures how much of the rip the log
                            # documents, not whether the audio is the audio that was
                            # ripped: a track whose .log CRC just matched the file is
                            # intact whatever the score says.
                            if checksum_verified_canon.get(_canon2(fp)) == "REAL":
                                continue
                            mark_fake(fp, "unscorable .log (LOG_GRADE missing)",
                                      "unscorable .log")

    # Audit FAIL on invalid .log SHA256 checksum (EAC 1.0b1+ logs)
    # --------------------------------------------------------------
    if config.get("audit_verify_log_checksum", True) and cd_candidate_dirs:
        from .discs import check_log_checksum, album_discs as _ad_chk, _disc_pattern_for as _pat_chk, _disc_expected_name as _exp_chk
        checksum_failed = {}  # album_dir -> list of (log_path, detail)
        for d in cd_candidate_dirs:
            try:
                discs_here = _ad_chk(d)
            except Exception:
                discs_here = {}
            pat = _pat_chk(config)
            logs_to_check = []
            if discs_here:
                for disc_n in discs_here:
                    lp = os.path.join(d, _exp_chk(pat, disc_n, ".log"))
                    if os.path.isfile(lp):
                        logs_to_check.append((lp, discs_here[disc_n]))
                    else:
                        # Fallback orphan present but not at expected name
                        pass
                # Also include any extra .log not at expected pattern (orphan)
                # Try to map orphan to a specific disc via explicit disc number in filename or TOC, otherwise only for single-disc albums
                try:
                    for f in os.listdir(d):
                        if f.lower().endswith(".log"):
                            full = os.path.join(d, f)
                            if full not in [x[0] for x in logs_to_check]:
                                # Attempt disc-specific mapping for orphan
                                from .discs import _log_name_disc as _lnd_orphan, parse_log_toc_seconds as _plts, read_log_text as _rlt
                                orphan_disc = _lnd_orphan(f)
                                if orphan_disc and orphan_disc in discs_here:
                                    logs_to_check.append((full, discs_here[orphan_disc]))
                                elif len(discs_here) == 1:
                                    # Single-disc: orphan belongs to the sole disc
                                    sole = next(iter(discs_here.values()))
                                    logs_to_check.append((full, sole))
                                else:
                                    # Multi-disc orphan: try TOC duration matching to find unique disc (reuse discs.py logic)
                                    try:
                                        toc = _plts(_rlt(full))
                                        if toc > 0:
                                            from .discs import _audio_seconds as _asec2
                                            candidates = []
                                            toc_tol = float(config.get("discs_toc_tolerance_s", 4.0)) if config else 4.0
                                            for dn2, tlist in discs_here.items():
                                                if dn2 in [dn for dn, _ in logs_to_check]:
                                                    continue
                                                secs = _asec2(tlist)
                                                if secs and abs(secs - toc) <= toc_tol:
                                                    candidates.append((dn2, tlist))
                                            if len(candidates) == 1:
                                                logs_to_check.append((full, candidates[0][1]))
                                                continue
                                    except Exception:
                                        pass
                                    # If still ambiguous on multi-disc, skip orphan to avoid marking whole album — will be handled as unscorable disc instead
                                    continue
                except OSError:
                    pass
            else:
                try:
                    for f in os.listdir(d):
                        if f.lower().endswith(".log"):
                            full = os.path.join(d, f)
                            all_tr = [os.path.join(d, xf) for xf in os.listdir(d) if xf.lower().endswith(CD_AUDIO_EXTS)]
                            logs_to_check.append((full, all_tr))
                except OSError:
                    continue
            for lp, trs in logs_to_check:
                state, detail = check_log_checksum(lp)
                # When audit_verify_log_checksum is required (True by default), both
                # 'invalid' and 'missing' EAC checksums must fail auditing – a CD rip
                # without a verifiable SHA256 cannot be considered accurately ripped.
                # 'unsupported' (XLD/non-EAC) has no checksum concept and stays PASS.
                # 'ok' (valid) stays PASS.
                if state == "invalid":
                    checksum_failed.setdefault(d, []).append((lp, detail or "invalid SHA256"))
                elif state == "missing":
                    # EAC log claims no checksum line but should have one (required)
                    checksum_failed.setdefault(d, []).append((lp, detail or "missing Log checksum"))
                # 'ok', 'unsupported', None are passes
        if checksum_failed:
            log(c(f"Audit FAIL on log checksum: {sum(len(v) for v in checksum_failed.values())} log(s) in {len(checksum_failed)} CD album(s) have invalid/missing SHA256 checksum — marking their disc(s) as failed (audit_verify_log_checksum on, required)", Color.RED))
            for d, lst in checksum_failed.items():
                for lp, detail in lst:
                    # Determine the affected tracks FOR THIS LOG — per disc, not
                    # for the album. This loop used to sit outside `lst`, so on a
                    # multi-disc album only the LAST bad log's disc was ever
                    # marked (and `affected` could be a stale list from the
                    # previous log).
                    try:
                        discs_here = _ad_chk(d)
                        affected = None
                        if discs_here:
                            pat = _pat_chk(config)
                            for dn, trs in discs_here.items():
                                exp = os.path.join(d, _exp_chk(pat, dn, ".log"))
                                if os.path.normcase(exp) == os.path.normcase(lp):
                                    affected = trs
                                    break
                        if affected is None:
                            # Orphan: try explicit disc number in filename, else only for single-disc
                            from .discs import _log_name_disc as _lnd2b
                            dn_orph = _lnd2b(os.path.basename(lp))
                            if discs_here and dn_orph and dn_orph in discs_here:
                                affected = discs_here[dn_orph]
                            elif discs_here and len(discs_here) == 1:
                                affected = next(iter(discs_here.values()))
                            elif not discs_here:
                                affected = [os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(CD_AUDIO_EXTS)]
                            else:
                                # Multi-disc orphan with no explicit mapping — skip to avoid whole-album false fail
                                continue
                    except OSError:
                        continue
                    for fp in affected:
                        if fp not in files:
                            continue
                        # A track whose .log CRC was verified against the audio is
                        # intact by its own evidence: the log's per-track checksum
                        # matched, so the missing/unverifiable SHA256 of the LOG
                        # FILE says nothing about the audio. Same rule the
                        # AccurateRip gate below applies.
                        if checksum_verified_canon.get(_canon2(fp)) == "REAL":
                            continue
                        mark_fake(
                            fp,
                            f"log checksum invalid ({os.path.basename(lp)}: {detail or 'mismatch'})",
                            "log checksum invalid")

    # Audit FAIL on AccurateRip mismatch (any track not accurately ripped)
    # ONLY via .accurip files (CUETools) — log data is never consulted for
    # AccurateRip per user: "The CUETOOLS cli tool should be the only tool
    # used to generate accurip files, data from log files isn't related to it
    # whatsoever."  Missing .accurip or any 'No match' => audit FAIL.
    # Per user 2026-08-25: this must affect AUDIT only (grading is tagging-only)
    # and must run for ALL CD albums, not just those with a .log.
    # --------------------------------------------------------------
    # Candidate list for AccurateRip: ALL CD albums (MEDIA==CD), regardless of
    # .log presence — the same one pass that decided MEDIA for the checksum
    # phase above, not a second tag-reading walk of every album.
    cd_accurip_candidate_dirs = [d for d in album_dirs if cd_album_map.get(d)]
    if config.get("audit_require_accuraterip", True) and cd_accurip_candidate_dirs:
        from .discs import album_discs as _ad_ar, _disc_pattern_for as _pat_ar, _disc_expected_name as _exp_ar
        try:
            from .accurip import parse_accurip_status as _parse_ar_audit
        except Exception:
            _parse_ar_audit = None
        # Per-track AccurateRip: only the tracks that actually mismatch fail, not the whole album
        ar_failed_per_file = {}  # file path -> reason
        ar_files_seen = 0
        for d in cd_accurip_candidate_dirs:
            try:
                discs_here = _ad_ar(d)
            except Exception:
                discs_here = {}
            pat = _pat_ar(config)
            if not discs_here:
                try:
                    aud = [os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(CD_AUDIO_EXTS)]
                    if aud:
                        discs_here = {1: aud}
                except OSError:
                    discs_here = {}
            if not discs_here:
                continue
            all_accurips = [os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(".accurip")]
            for disc_n, trs in sorted(discs_here.items()):
                expected = os.path.join(d, _exp_ar(pat, disc_n, ".accurip"))
                ap = expected if os.path.isfile(expected) else None
                if ap is None and len(discs_here) == 1 and all_accurips:
                    ap = sorted(all_accurips)[0]
                if ap is None or not os.path.isfile(ap):
                    # Missing file → every track on this disc fails (per-track,
                    # but all) — only when something COULD have written one.
                    # Without CUETools (or the ffmpeg that transports to WAV)
                    # this is a property of the machine, not of the rip, and
                    # failing the whole CD library for a missing dependency was
                    # the missing-tool-reads-as-failure bug.
                    if ar_generator:
                        for fp in trs:
                            if fp in files:
                                ar_failed_per_file[fp] = "Missing .accurip file (CUETools)"
                    continue
                ar_files_seen += 1
                try:
                    txt_ar = open(ap, "r", encoding="utf-8", errors="replace").read()
                except OSError as e:
                    for fp in trs:
                        if fp in files:
                            ar_failed_per_file[fp] = f"cannot read .accurip: {e}"
                    continue
                # Use per-track parser for accurate per-track verdicts
                try:
                    from .accurip import parse_accurip_per_track as _parse_per
                    per_track = _parse_per(txt_ar)
                except Exception:
                    per_track = {}
                if _parse_ar_audit is not None:
                    overall_st, overall_detail = _parse_ar_audit(txt_ar)
                else:
                    low = txt_ar.lower()
                    if "no match" in low:
                        overall_st = "FAKE"
                    elif "accurately ripped" in low:
                        overall_st = "REAL"
                    elif txt_ar.strip():
                        overall_st = "REAL"
                    else:
                        overall_st = "NONE"
                    overall_detail = None
                # If overall is NONE/FAKE but per_track empty, fall back to overall
                if not per_track:
                    if overall_st == "FAKE":
                        for fp in trs:
                            if fp in files:
                                ar_failed_per_file[fp] = overall_detail or "AccurateRip mismatch in .accurip (No match)"
                    elif overall_st == "NONE":
                        for fp in trs:
                            if fp in files:
                                ar_failed_per_file[fp] = overall_detail or "Missing/unscorable AccurateRip in .accurip"
                    continue
                # Per-track: map each file's track number to its status
                for fp in trs:
                    if fp not in files:
                        continue
                    # Determine track number for this file
                    try:
                        from .discs import _track_num_of as _tn2, _file_track_number as _ftn2
                        tn = _tn2(fp)
                        if tn is None:
                            tn = _ftn2(os.path.basename(fp))
                        if tn is None:
                            continue
                        st = per_track.get(int(tn))
                        if st is None:
                            # Track not in .accurip table → treat as not present → fail if required
                            st = "NONE"
                        if st == "FAKE":
                            ar_failed_per_file[fp] = "AccurateRip No match in .accurip"
                        elif st == "NONE":
                            ar_failed_per_file[fp] = "Track not present in AccurateRip database"
                        # REAL → pass, do not add
                    except Exception:
                        continue
        # A rip whose .log CRC verified is intact by its own evidence:
        # AccurateRip not knowing that pressing — an absent .accurip, a
        # "not present in database" track — is not evidence against it.
        for _fp in [p for p in ar_failed_per_file
                    if checksum_verified.get(p) == "REAL"]:
            ar_failed_per_file.pop(_fp, None)
        if ar_failed_per_file:
            # Group by album for log header
            by_album = {}
            for fp, reason in ar_failed_per_file.items():
                d = os.path.dirname(fp)
                by_album.setdefault(d, []).append((fp, reason))
            log(c(f"Audit FAIL on AccurateRip: {len(ar_failed_per_file)} track(s) in {len(by_album)} CD album(s) not accurately ripped — marking only those tracks as failed (per-track, audit_require_accuraterip on, .accurip only via CUETools)", Color.RED))
            for fp, reason in ar_failed_per_file.items():
                if fp not in files:
                    continue
                mark_fake(fp,
                          f"not accurately ripped ({os.path.basename(fp)}: {reason})",
                          "not accurately ripped")
        elif not ar_generator and not ar_files_seen:
            log(c("AccurateRip check skipped: CUETools (or ffmpeg for its WAV "
                  "transport) is not installed, so no .accurip can exist or be "
                  "made — no verdict written from its absence", Color.YELLOW))
            stats["errors"].append(("AccurateRip",
                                    "CUETools/ffmpeg not installed - .accurip not checked"))

    # Audit FAIL on Logchecker score below threshold
    # --------------------------------------------------------------
    if int(config.get("audit_log_score_threshold", 100) or 0) > 0 and cd_candidate_dirs and log_scores:
        try:
            thr_a = int(config.get("audit_log_score_threshold", 100) or 0)
            thr_a = max(0, min(100, thr_a))
        except Exception:
            thr_a = 0
        if thr_a > 0:
            from .discs import album_discs as _ad_thr
            thr_failed = {}  # album_dir -> list disc nums
            for d, scores in list(log_scores.items()):
                for disc_n, sc in scores.items():
                    try:
                        if int(sc) < thr_a:
                            thr_failed.setdefault(d, []).append((disc_n, sc))
                    except Exception:
                        continue
            if thr_failed:
                log(c(f"Audit FAIL on log score threshold: {sum(len(v) for v in thr_failed.values())} disc(s) in {len(thr_failed)} CD album(s) below {thr_a}/100 — marking their disc(s) as failed (audit_log_score_threshold on)", Color.RED))
                for d, lst in thr_failed.items():
                    for disc_n, sc in lst:
                        # The disc numbers here come from grade_album_logs, i.e.
                        # from the same album_discs map, so the tracks of the
                        # disc are known. A disc this map cannot resolve is left
                        # alone rather than marking the whole album on a guess.
                        try:
                            affected = (_ad_thr(d) or {}).get(disc_n) or []
                        except Exception:
                            affected = []
                        for fp in affected:
                            if fp not in files:
                                continue
                            # Same rule as the other log gates: a track whose
                            # .log CRC matched the audio is intact, whatever
                            # the log's completeness score says about the log.
                            if checksum_verified_canon.get(_canon2(fp)) == "REAL":
                                continue
                            mark_fake(fp, f"log score {sc} below threshold {thr_a}",
                                      f"log score < {thr_a}")


    # Audit FAIL on CD not 16-bit 44.1 kHz (true CD-DA) — independent of the
    # CRC check and of whether AudioAuditor ran.
    # --------------------------------------------------------------
    if config.get("audit_check_cd_format", True):
        # The per-file MEDIA=CD set came out of the one pass at the top of this
        # function, so this no longer re-opens every file's tags to ask again
        # (it used to be the fourth full tag read per album of the run).
        cd_format_failed = {}
        # This gate opens every CD track, so it advances the UI bar like the
        # other long phases; without it the header sat on the previous phase.
        fmt_counts = {"ok": 0, "skip": 0, "fail": 0}
        fmt_pbar = _make_pbar(len(cd_files_flagged), "CD format", unit="file")
        for fp in sorted(cd_files_flagged):
            _pbar_update(fmt_pbar, fmt_counts)
            if fp not in files:
                continue
            try:
                af2 = AudioFile(fp)
                if af2.audio is None or not hasattr(af2.audio, "info") or af2.audio.info is None:
                    continue
                info2 = af2.audio.info
                bits2 = getattr(info2, "bits_per_sample", None)
                if bits2 is None:
                    bits2 = getattr(info2, "bits", None)
                rate2 = getattr(info2, "sample_rate", None)
                if rate2 is None:
                    continue
                is_ok2 = True
                detail2 = ""
                if bits2 is not None and bits2 != 16:
                    is_ok2 = False
                    detail2 = f"{bits2}-bit"
                if rate2 != 44100:
                    is_ok2 = False
                    detail2 = f"{detail2} {rate2}Hz".strip() if detail2 else f"{rate2}Hz"
                if not is_ok2:
                    cd_format_failed[fp] = detail2 or "not 16/44.1"
            except Exception:
                continue
        if fmt_pbar:
            fmt_pbar.close()
        if cd_format_failed:
            log(c(f"Audit FAIL on CD format: {len(cd_format_failed)} CD track(s) not 16-bit 44.1 kHz — marking as failed (audit_check_cd_format on)", Color.RED))
            for fp, detail2 in cd_format_failed.items():
                mark_fake(fp, f"not 16-bit 44.1 kHz ({detail2})",
                          "not 16-bit 44.1 kHz")


    # .log CRC verdicts never pass through the AudioAuditor loop above, so
    # count them for the summary — otherwise a CRC-only CD library reports
    # "Real 0 · Fake 0". Anything the loop already counted is in
    # file_status_map and is skipped (no double counting).
    if not require_both and checksum_verified:
        for p, verdict in checksum_verified.items():
            if p in todo_paths or _canon2(p) in file_status_map:
                continue
            stats["total_scanned"] += 1
            if verdict == "REAL":
                status_counts["Real"] += 1
            else:
                status_counts["Fake"] += 1
                stats["grade_dist"]["FAIL"] = stats["grade_dist"].get("FAIL", 0) + 1

    # Every AUDIT verdict this run wrote is bound to the file's size/mtime,
    # so the next run may skip a file without trusting a tag written for
    # different bytes.
    _save_evidence(config, _EVIDENCE)

    stats["grade_dist"]["PASS"] = status_counts["Real"]
    # The pass rate is REAL out of everything examined. It used to be
    # "Real - warned", which mixed a warning count into a file count and
    # under-reported the rate by the number of flagged-but-clean files.
    stats["summary_pass"] = status_counts["Real"]
    stats["summary_total"] = stats["total_scanned"]
    stats["issue_counts"] = issue_counts
    stats["audit_status_counts"] = status_counts
    stats["audit_warned"] = warned
    stats["audit_flagged"] = len(flagged)
    stats["audit_log_scores"] = {
        os.path.basename(d): s for d, s in log_scores.items()}

    log("")
    log("Audit summary: "
        + " · ".join(f"{k} {v}" for k, v in status_counts.items()))
    if skipped:
        log(f"  {skipped} file(s) already audited were skipped.")
    if warned:
        log(f"  {warned} file(s) came back as warnings: detector flags on a "
            f"real file (clipping / MQA / silence / AI markers), or a status "
            f"the tool could not decide - the latter carry NO verdict.")

    return stats
