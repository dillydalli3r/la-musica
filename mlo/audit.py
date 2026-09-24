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
  INTEGRITY  OK / FAIL / UNKNOWN - on every FLAC whose stream the integrity
             test decoded. The verdict of the container's OWN statement about
             its audio (STREAMINFO's MD5): OK when the audio hashes to the
             digest the stream states, FAIL when it does not (corrupt or
             dishonestly written — the AUDIT verdict is FAKE for those too),
             UNKNOWN when the stream states none (all-zero = unknown: the
             audio decoded, nothing verifies it). Written only when it says
             something other than what the file already carries.
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
from .paths import AUDIO_EXTS, DEPS_DIR, app_data_dir, is_video_file
from .stats import (
    new_stats, _make_pbar, _pbar_update, _collect_targets, _walk_files,
    _diff_bytes, worker_count,
)
from .subproc import run_tool
from .tagtext import canonical_text, canonical_value
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

    For FLAC: runs `flac -t` (test) which checks frame CRCs and stream integrity
    — and answers the question only this container can ask: whether the audio
    is what its STREAMINFO MD5 says it is. The three answers are kept apart
    (see mlo.flac.stream_md5_state): a MISMATCH is a failure ("the digest the
    stream states is not the digest of its audio" — a corrupt or dishonestly
    written file), a stream that states NO MD5 is not (it decoded, nothing
    compares it — recorded as "unknown" and reported as a warning, never read
    as "verified"), and everything else is the plain decode/CRC check.
    For all types: runs `ffmpeg -v error -i file -f null -` to catch decoding errors,
    truncated files, and sync errors (similar to foobar2000's decoder check).

    Returns (ok: bool, error: str | None). True when no errors detected.
    """
    ext = os.path.splitext(filepath)[1].lower()
    # Try flac -t for FLAC files (most thorough for FLAC)
    if ext == ".flac" and (flac_exe or ffmpeg_exe):
        from .flac import (MD5_ABSENT, MD5_FAILED, MD5_MISMATCH, MD5_OK,
                          MD5_UNKNOWN, md5_finding, stream_md5_state)
        state, detail, _digest = stream_md5_state(filepath, flac_exe,
                                                 ffmpeg_exe)
        if state != MD5_UNKNOWN:
            _INTEGRITY_STATE[_ev_key(filepath)] = state
            if state in (MD5_OK, MD5_ABSENT):
                # ABSENT is a clean decode with nothing to compare (all-zero
                # STREAMINFO MD5): not a failure, and not a verification
                # either — the INTEGRITY tag says UNKNOWN and the run log
                # names it.
                return True, None
            if state == MD5_MISMATCH:
                return False, md5_finding(MD5_MISMATCH, detail)
            if state == MD5_FAILED:
                return False, str(detail)[:200]
            return False, f"flac -t: {detail or 'could not verify'}"[:200]
        # UNKNOWN: nothing could establish it (a bit depth with no comparable
        # representation, or no flac.exe and no ffmpeg) — the plain decode
        # below is all that is left, and it catches a corrupt stream, not a
        # header that lies about it.

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


def _read_audit_tags(path, write_tags=True, config=None):
    """(media_is_cd, verdict, changed) for one file, from ONE container read.

    The MEDIA=CD pass at the top of the run and the verdict pass below ask the
    same file two different questions, and each used to open — and parse — the
    container for itself, on top of the open the AUDIT write pays: three parses
    of every file, every run. server.tagcache is the stat-keyed read cache the
    API serves tracks through, so the second question is answered from the
    first parse. Its key carries the file's mtime, so the one write made here
    (normalizing a legacy mixed-case verdict, Real -> REAL) makes the next read
    read again rather than serve what the write just replaced.

    Imported lazily: mlo must not import server at module level (the CLI runs
    the scripts without a server), and a build that has no server simply reads
    the file directly.
    """
    raw_media = raw_audit = ""
    got = None
    try:
        from server import tagcache
        got = tagcache.read_track(path, ["MEDIA", "AUDIT"])[0]
    except Exception:
        got = None
    if got is None:
        try:
            af = AudioFile(path)
            if af.audio is None:
                return False, None, False
            raw_media = str(af.get_tag("MEDIA") or "")
            raw_audit = str(af.get_tag("AUDIT") or "").strip()
        except Exception:
            return False, None, False
    else:
        raw_media = str(got.get("MEDIA") or "")
        raw_audit = str(got.get("AUDIT") or "").strip()

    # Through the canonical spelling (mlo.tagtext), so a "cd" a different
    # tagger wrote is the same MEDIA this audit expects.
    is_cd = canonical_text("MEDIA", raw_media) == "CD"
    # mlo.tagtext owns the spelling rule; an unknown value (a word that is
    # neither verdict) comes back unchanged and is reported as no verdict,
    # exactly as the old .upper() did.
    v = str(canonical_value("AUDIT", raw_audit))
    changed = False
    if write_tags and raw_audit and v in ("REAL", "FAKE") and raw_audit != v:
        # Respect per-filetype AUDIT toggle
        if config is None or should_write_audio_tag(config, "AUDIT", filepath=path):
            changed = _write_audit_value(path, v)
    return is_cd, (v if v in ("REAL", "FAKE") else None), changed


def _write_audit_value(path, value):
    """Write one normalized AUDIT value; True when the file changed."""
    try:
        af = AudioFile(path)
        return bool(af.set_tag("AUDIT", value))
    except Exception:
        return False


# ----------------------------------------------------------------------
# Verdict evidence: which file a stored AUDIT verdict was decided for
# ----------------------------------------------------------------------
# "REAL" is a statement about the BYTES AudioAuditor read. Replace or
# re-encode the file and the tag still says REAL about audio nothing audited —
# and the next run SKIPS it on that stale verdict. What each verdict was
# written for lives in <music>/.mlo/data/audit_evidence.json (the same place
# and shape as the on-demand ReplayGain cache, mlo.loudness), and a file the
# record no longer describes is audited again instead of skipped.
#
# A record is [size, mtime_ns, verified, identity]: the size and mtime it was
# written for, the integrity test's own answer for that audio (True/False, or
# None when the run never tested it), and the audio identity a tag write
# cannot move. All four are read through _evidence_state, which is the only
# thing that decides whether a stored verdict is trusted — see there for why
# the identity has to be part of it.
#
# ponytail: one JSON file rewritten once per run, entries dropped only when
# their file is gone; move it to sqlite if a huge library ever makes that hurt.
EVIDENCE_NAME = "audit_evidence.json"
_EVIDENCE = {}
# Which files THIS run verified as intact (canonical path -> bool). It rides
# in the evidence record's third element (see _record_evidence).
_INTEGRITY_PASSED = {}
# The stream-MD5 answer THIS run got for each file it decoded (canonical path
# -> the state mlo.flac.stream_md5_state names: "ok" / "md5-mismatch" /
# "md5-absent" / "error"). It rides in the record's fifth element, so a later
# run — and the grader, which must not pay for the same decode — can tell a
# verified digest from an absent one from a mismatched one, and the INTEGRITY
# tag is written from it.
_INTEGRITY_STATE = {}
# "the caller did not say": distinguishes "keep what the record has" from a
# remembered None.
_UNSET = object()


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


def _record_evidence(path, verified=_UNSET, identity=None, state=None):
    """Store the evidence record for *path*:
    [size, mtime_ns, verified, identity, state].

    *verified* is the integrity test's own answer for these bytes (True/False)
    when this run ran it over the file, and None when it did not — the config
    had the test off, or the file was skipped before it could run. None is not
    a failure and not a pass: it means "nothing established this", and a later
    run verifies a file whose record does not say True rather than trusting a
    verdict nobody ever decoded (which is also how a record written by a
    version that had no third element behaves).

    The fourth element is the AUDIO identity (see :func:`_audio_identity`):
    the half of the record that a tag write cannot move. A caller that has
    just read it passes it in rather than paying for the same header twice.

    The fifth is WHAT the test found (mlo.flac's stream-MD5 vocabulary), so a
    later reader — the grader, or a run that only has to write the INTEGRITY
    tag — learns "verified" apart from "the stream states no MD5 at all"
    without decoding the file again. Absent from an older record, and "" for a
    file whose container states no digest (every non-FLAC).
    """
    stamp = _file_stamp(path)
    if stamp is None:
        return
    key = _ev_key(path)
    if verified is _UNSET:
        verified = _INTEGRITY_PASSED.get(key)
        if verified is None:
            rec = _EVIDENCE.get(key)
            if (isinstance(rec, (list, tuple)) and len(rec) >= 3
                    and _record_stamp(rec) == stamp):
                # This run did not run the test over the file (it was settled
                # already), and the file has not moved since: the answer the
                # record carries is still the answer for these bytes, so a
                # later write does not have to forget it.
                verified = rec[2]
    if identity is None:
        identity = _audio_identity(path)
    if state is None:
        state = _INTEGRITY_STATE.get(key, "")
        if not state:
            rec = _EVIDENCE.get(key)
            if (isinstance(rec, (list, tuple)) and len(rec) >= 5
                    and _record_stamp(rec) == stamp):
                state = str(rec[4] or "")
    _EVIDENCE[key] = [stamp[0], stamp[1], verified, identity, state]


def _record_stamp(rec):
    """The [size, mtime_ns] a stored record was written for, or None."""
    try:
        return [int(rec[0]), int(rec[1])]
    except (TypeError, ValueError, IndexError):
        return None


def _audio_identity(path):
    """*path*'s audio identity, or "" when its container cannot state one.

    The FLAC STREAMINFO MD5 that mlo.discs' CRC memo and mlo.accurip already
    key their own audio evidence on: a tag write leaves it, a re-encode or a
    different file changes it. Imported lazily (mlo.accurip imports this
    module back) and only asked of a .flac, the one container that states a
    stream identity here — a file of any other kind would pay an open (an
    ffprobe, for a video) to be told nothing.
    """
    if os.path.splitext(str(path))[1].lower() != ".flac":
        return ""
    try:
        from .accurip import _audio_identity as identity_of
        return identity_of(path) or ""
    except Exception:
        return ""


def _evidence_state(path):
    """(current, verified, state) for one file's stored evidence.

    *current* — the record describes THIS file's audio: either the size/mtime
    stamp matches, or the record's audio identity does. The stamp is the cheap
    half; the identity is the half that survives a tag write, which is what a
    re-audit after a script chain actually needs — the chain writes tags to
    every file it touches, so by the next run every stamp has moved while the
    audio has not, and a stamp-only test re-decodes the whole library to learn
    what the last run already knew (mlo.discs and mlo.accurip key their own
    audio evidence on the identity for exactly this reason).

    *verified* — the record also carries the integrity test's answer for those
    bytes, and it was True. A container that states no identity (an mp3's tags
    move its stamp and nothing else) is trusted on its stamp alone, which is
    what it was before this element existed.

    *state* — WHAT the test found (mlo.flac's stream-MD5 vocabulary: "ok" /
    "md5-mismatch" / "md5-absent" / "error"), "" when the record predates that
    element or the file states no digest. It is what the INTEGRITY tag and the
    grader's finding are written from, so neither has to decode the file the
    audit already decoded.
    """
    key = _ev_key(path)
    rec = _EVIDENCE.get(key)
    stamp = _file_stamp(path)
    if stamp is None or not isinstance(rec, (list, tuple)) or len(rec) < 2:
        return False, False, ""
    verified = bool(len(rec) >= 3 and rec[2])
    state = str(rec[4] or "") if len(rec) >= 5 else ""
    if _record_stamp(rec) == stamp:
        return True, verified, state
    identity = str(rec[3]) if len(rec) >= 4 else ""
    if identity and identity == _audio_identity(path):
        # The same audio under a new stamp: the tags were rewritten, the
        # samples were not. Re-filed under the current stamp so the rest of
        # this run asks the cheap question.
        _record_evidence(path, verified, identity, state)
        return True, verified, state
    return False, False, ""


def _evidence_path(config):
    """<music>/.mlo/data/audit_evidence.json, or "" when there is no folder."""
    try:
        return os.path.join(app_data_dir(config.get("music_folder")),
                            EVIDENCE_NAME)
    except Exception:
        return ""


def _load_evidence(config):
    """The stored {path: [size, mtime_ns, verified, identity, state]} map;
    unreadable reads as empty.

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


# The INTEGRITY tag's vocabulary: the FLAC stream MD5's verdict, in the three
# states a stream can be in. Unknown is NOT a pass — it is the all-zero field
# saying "no digest here", which is why it is written rather than left off.
INTEGRITY_TAG_VALUES = {"ok": "OK", "md5-mismatch": "FAIL", "error": "FAIL",
                        "md5-absent": "UNKNOWN"}


def _write_integrity_value(path, state, config):
    """Write the FLAC stream identity's verdict when the file does not say it.

    OK / FAIL / UNKNOWN from mlo.flac's own vocabulary (see
    INTEGRITY_TAG_VALUES). FLAC only: no other container states an MD5 of its
    audio, so there is nothing for the tag to be about. Gated exactly like the
    AUDIT verdict it belongs to (write_audit_tag, and the per-filetype AUDIT
    family, which mlo.config maps INTEGRITY into) and a no-op when the file
    already carries the verdict — a re-audit of a settled library must not
    rewrite a byte. Used for the files no AUDIT-write phase opened; a file that
    DOES get a verdict has both tags written in one rewrite (_write_audit_tag).
    """
    value = INTEGRITY_TAG_VALUES.get(state or "")
    if not value:
        return False
    if (not config.get("audit_integrity", True)
            or not config.get("write_audit_tag", True)
            or not should_write_audio_tag(config, "INTEGRITY", filepath=path)):
        return False
    try:
        af = AudioFile(path)
        if af.audio is None:
            return False
        cur = str(af.get_tag("INTEGRITY") or "").strip().upper()
        _INTEGRITY_WRITTEN.add(_ev_key(path))
        if cur == value:
            return False
        if not af.set_tag("INTEGRITY", value):
            return False
        # The tag write moved the stamp: re-file the record under it, so the
        # next phase of THIS run (and the next run) still sees the verdict the
        # integrity test just established for these bytes.
        _record_evidence(path)
        return True
    except Exception:
        return False


def _pending_integrity(path):
    """The INTEGRITY value THIS run established for *path*, "" when none."""
    return INTEGRITY_TAG_VALUES.get(_INTEGRITY_STATE.get(_ev_key(path), "") or "",
                                    "")


def _write_audit_tag(path, value):
    """Write the AUDIT tag — and, through the same container rewrite, the
    INTEGRITY verdict this run established for the file. Returns
    (changed: bool, b_rem: int, b_add: int, error: str | None).

    Both tags are this app's own verdicts about the same audio, and a second
    open to add the second one was a whole extra file rewrite per audited file
    (mlo.audio saves per call, and with flac_no_padding a save rewrites the
    whole stream) — so the two go out together, in one deferred save.
    """
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
        need_audit = cur_clean.lower() != value.lower()
        wanted = _pending_integrity(path)
        writable = bool(wanted) and _verdict_write_allowed(path)
        need_integrity = writable and (
            str(af.get_tag("INTEGRITY") or "").strip().upper() != wanted)
        if writable:
            # Whatever this open decides, the INTEGRITY tag is settled for
            # this file: the fill pass at the end of the run must not open it
            # again to find that out.
            _INTEGRITY_WRITTEN.add(_ev_key(path))
        if not need_audit and not need_integrity:
            # The file already carries this verdict and this run just re-read
            # it against these bytes: renew the evidence so the next run may
            # skip it (a stale stamp was the reason this file was audited).
            _record_evidence(path)
            return False, 0, 0, None

        af.defer_save(True)
        ok = True
        if need_audit:
            ok = bool(af.set_tag("AUDIT", value)) and ok
        if need_integrity:
            ok = bool(af.set_tag("INTEGRITY", wanted)) and ok
        af.defer_save(False)
        if not ok:
            return False, 0, 0, f"write: {af.error}"

        after = os.path.getsize(path)
        b_rem, b_add = _diff_bytes(before, after)
        _record_evidence(path)
        return True, b_rem, b_add, None
    except Exception as e:
        return False, 0, 0, str(e)


def _verdict_write_allowed(path):
    """Whether this run may write the file's verdict tags at all.

    The gates every verdict write shares: `write_audit_tag` and the integrity
    test's own switch, plus the per-filetype AUDIT family (which mlo.config
    maps INTEGRITY into). One function, so a tag can never go out through a
    gate the other write path ignores.

    Read from _RUN_CONFIG — the config of the run in progress. The AUDIT write
    path is reached from the verdict phases (the CD legs, the spectral loop),
    none of which carries the config down to :func:`_write_audit_tag`.
    """
    return bool(_RUN_CONFIG.get("write_audit_tag", True)
                and _RUN_CONFIG.get("audit_integrity", True)
                and should_write_audio_tag(_RUN_CONFIG, "INTEGRITY",
                                           filepath=path))


# The config of the run in progress (see _verdict_write_allowed), and the set
# of files whose INTEGRITY tag this run has already settled — a file opened for
# its AUDIT verdict carries it out in the same rewrite, and the fill pass at
# the end of the run must not open it again to find that out.
_RUN_CONFIG: dict = {}
_INTEGRITY_WRITTEN: set = set()


# ----------------------------------------------------------------------
# What an earlier audit already established about a file's audio
# ----------------------------------------------------------------------
# The grader (script 4) asks the same question the audit does — does this
# stream's audio hash to the MD5 its STREAMINFO states — and must not pay for
# a decode the audit has already paid for. These two functions are the whole
# contract: `recorded_integrity` reads the answer (and only when the record
# still describes THESE bytes, the same rule the audit skips a file on), and
# `note_integrity` files back what the grader itself decoded, so the next
# reader gets the same answer for free.
_EVIDENCE_LOCK = __import__("threading").Lock()
_EVIDENCE_LOADED = False
_EVIDENCE_DIRTY = False


def _ensure_evidence(config):
    """Load the map once per process (the audit's own run also does this)."""
    global _EVIDENCE_LOADED
    with _EVIDENCE_LOCK:
        if _EVIDENCE_LOADED:
            return
        _EVIDENCE.clear()
        if (config or {}).get("music_folder"):
            _EVIDENCE.update(_load_evidence(config))
        _EVIDENCE_LOADED = True


def recorded_integrity(path, config=None, stated=""):
    """(state, "") for *path* when an audit's answer still describes its audio.

    *state* is mlo.flac's stream-MD5 vocabulary; ("", "") means nothing on
    record describes these bytes (never audited, replaced, or re-encoded) and
    the caller has to verify them itself. A record whose stamp has moved is
    matched on the stream identity, which is what survives a tag write — the
    same test the audit's own skip uses, so the two can never disagree about
    whether a stored answer is still this file's.

    *stated* (the digest the file states NOW, hex) makes the claim explicit:
    a record filed under a different digest is not an answer about this
    header, whatever its stamp says.
    """
    try:
        _ensure_evidence(config or {})
        current, _verified, state = _evidence_state(path)
        if not current:
            return "", ""
        if stated:
            rec = _EVIDENCE.get(_ev_key(path)) or []
            identity = str(rec[3]) if len(rec) >= 4 else ""
            if identity and identity != f"flac:{int(stated, 16)}":
                return "", ""
        return state, ""
    except Exception:
        return "", ""


def note_integrity(path, state, config=None):
    """File what THIS caller (the grader) decoded, for the next reader.

    Recorded exactly like the audit records its own answer — the stamp, the
    audio identity and the state — so a later audit or grade over the same
    audio does not decode the file again, and a tag write in between does not
    lose the answer. Saved on the way out of the caller's pass (see
    save_evidence), never per file.
    """
    global _EVIDENCE_DIRTY
    try:
        _ensure_evidence(config or {})
        key = _ev_key(path)
        _INTEGRITY_STATE[key] = state
        from .flac import MD5_ABSENT, MD5_OK
        _INTEGRITY_PASSED[key] = state in (MD5_OK, MD5_ABSENT)
        _record_evidence(path)
        with _EVIDENCE_LOCK:
            _EVIDENCE_DIRTY = True
    except Exception:
        pass


def save_evidence(config=None):
    """Write the evidence map if anything recorded since the last save."""
    global _EVIDENCE_DIRTY
    if not (config or {}).get("music_folder"):
        return
    with _EVIDENCE_LOCK:
        if not _EVIDENCE_DIRTY:
            return
        _EVIDENCE_DIRTY = False
    try:
        _save_evidence(config or {}, _EVIDENCE)
    except Exception:
        pass


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
    global _EVIDENCE_LOADED, _EVIDENCE_DIRTY
    _EVIDENCE.clear()
    _EVIDENCE.update(_load_evidence(config))
    with _EVIDENCE_LOCK:
        _EVIDENCE_LOADED = True
        _EVIDENCE_DIRTY = False
    _INTEGRITY_PASSED.clear()
    _INTEGRITY_STATE.clear()
    _INTEGRITY_WRITTEN.clear()
    _RUN_CONFIG.clear()
    _RUN_CONFIG.update(config)

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

    # ONE tag read per file, answering BOTH questions this run asks of the
    # same container: whether the track is MEDIA=CD (below) and whether it
    # already carries an AUDIT verdict (the pass further down, which also
    # normalizes a legacy mixed-case value). Keyed by path:
    # {path: (is_cd, verdict, changed)}.
    read_workers = worker_count(config, default=8, maximum=16, items=len(files))
    with ThreadPoolExecutor(max_workers=read_workers) as pool:
        file_reads = dict(zip(files, pool.map(
            lambda p: _read_audit_tags(p, config.get("write_audit_tag", True),
                                       config),
            files)))

    cd_album_map = {}
    cd_files_flagged = set()
    for album_dir, paths in by_album_files.items():
        is_cd = False
        for pp in paths:
            # Through the canonical spelling (mlo.tagtext), so a "cd" a
            # different tagger wrote is the same MEDIA this audit expects.
            if file_reads.get(pp, (False, None, False))[0]:
                cd_files_flagged.add(pp)
                is_cd = True
        cd_album_map[album_dir] = is_cd

    # ------------------------------------------------------------------
    # CD rip verification — a MEDIA=CD verdict is the AND of three legs,
    # every one of them the rip's OWN evidence:
    #
    #   1. the rip log's score (`audit_log_score_threshold`, written per disc
    #      by Logchecker as LOG_GRADE),
    #   2. the disc's checksums — the .log's per-track `Copy CRC` against the
    #      decoded PCM, and the .log's own EAC SHA256 (audit_verify_log_checksum),
    #   3. AccurateRip (audit_require_accuraterip), read from the .accurip.
    #
    # Each leg is recorded as it is established (cd_legs) and the phase at the
    # end of the CD section is the ONLY place that turns them into an AUDIT
    # tag. A leg a machine cannot evaluate leaves the file without a verdict
    # and is reported by name; a leg the user switched off is not required.
    #
    # AudioAuditor's spectral detectors have no vote on a CD in either
    # direction: its verdict used to decide for a disc neither the .log nor a
    # REAL .accurip could verify, and a "fake lossless" read of a synthetic
    # tone could outvote a log that had just proved the disc intact. It stays
    # EVIDENCE — a disagreement is reported as a warning — and it remains the
    # verdict for every other release type, where there is nothing else to go
    # on. `audit_cd_require_both` only decides whether it is run over
    # MEDIA=CD at all.
    # ------------------------------------------------------------------
    # Default True, matching mlo.config.DEFAULT_CONFIG: a partial cfg must
    # not audit a CD more leniently than the shipped app does.
    require_both = bool(config.get("audit_cd_require_both", True))
    cd_files = set()
    unverified_cd = {}
    checksum_verified = {}
    # The three legs of a MEDIA=CD verdict, keyed by canonical path: the gate
    # that establishes a leg records it here, and the verdict phase at the end
    # of the CD section is the one place that reads them. A leg whose key is
    # absent was switched off by config; "unknown" is a leg nothing on this
    # machine could evaluate — reported, never guessed.
    cd_legs = {}

    def set_leg(path, name, state, why=""):
        """Record one leg of one track's CD verdict.

        A definite failure outranks "unknown", which outranks "ok": a track
        whose log cannot be scored AND whose log checksum is wrong is a
        failure, not an incomplete answer. Two gates may feed one leg (the
        per-track CRCs and the log's own SHA256 are both "the disc's
        checksums"), so the stronger state is what survives.
        """
        legs = cd_legs.setdefault(_ev_key(path), {})
        rank = {"ok": 0, "unknown": 1, "fail": 2}
        current = legs.get(name)
        if current is None or rank[state] >= rank[current[0]]:
            legs[name] = (state, why if state != "ok" else "")
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
            # The leg is UN-EVALUATED, not absent: the user did not switch it
            # off, this machine just cannot decode the audio to compare it with
            # the rip log's CRCs. Recorded so the verdict phase reports the
            # missing leg by name instead of deciding a CD on two legs.
            for _p in cd_files:
                set_leg(_p, "checksums", "unknown",
                        "no ffmpeg to decode the audio and compare it with the "
                        "rip log's CRCs")
        else:
            cd_verify_ran = True
            from .discs import verify_album_checksums
            from concurrent.futures import as_completed

            cd_albums = {a: ps for a, ps in by_album_files.items()
                         if cd_album_map.get(a)}
            cw = worker_count(config, default=4, maximum=8,
                              items=len(cd_albums))
            # One album's share of that budget: the pool below runs *cw*
            # albums at once, and each of them may start this many decoders —
            # so the run's total stays cw (2 with worker_limit=2), instead of
            # cw × a constant 4.
            per_album = max(1, cw // max(1, min(len(cd_albums), cw)))
            # A bar for the CRC pass: it decodes every track of every CD, and
            # without one the header sat frozen for the whole phase.
            crc_counts = {"ok": 0, "skip": 0, "fail": 0}
            crc_pbar = _make_pbar(len(cd_albums), "CD checksums", unit="album")
            with ThreadPoolExecutor(max_workers=cw) as ex:
                futures = {
                    ex.submit(verify_album_checksums, ffmpeg_exe_for_cd,
                              album, paths, config, per_album): album
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
                        # The .log CRC is the FIRST of the three legs a CD
                        # verdict is the AND of, not the verdict itself. It
                        # used to be written as AUDIT=REAL on its own, which is
                        # how a rip whose log scored 40/100 and whose .accurip
                        # said "No match" could still be tagged REAL.
                        checksum_verified[path] = verdict
                        set_leg(path, "checksums",
                                "ok" if verdict == "REAL" else "fail",
                                "" if verdict == "REAL" else
                                "the rip log's CRC does not match the track's "
                                "audio")
                    for path, why in unver.items():
                        # Not a mismatch: nothing decoded the audio to compare
                        # against. Recorded as an un-evaluated leg — it is the
                        # missing half of the evidence, not evidence against
                        # the rip.
                        set_leg(path, "checksums", "unknown", why)
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

    # Skip files that already carry a REAL/FAKE verdict (a legacy mixed-case
    # value was normalized by the read above) unless the audit is forced.
    # When require_both, CD files are NOT skipped — they need the second source.
    #
    # This runs BEFORE the integrity pass on purpose. That pass decodes every
    # file it is handed, and a file whose stored verdict was written for
    # exactly these bytes (the evidence stamp below) has nothing left for it
    # to find: re-decoding it on every run is what made a re-audit of an
    # unchanged library cost one full decode per file and report nothing. What
    # the pass IS for is the files this run is about to audit — fail fast on a
    # corrupt one before the spectral run, and write FAKE for a failure a
    # stored verdict missed — and those are exactly `todo`.
    todo = files
    skipped = 0
    stale = 0
    if not force:
        # Whether this run is even supposed to decode the audio (see the
        # integrity pass below): with the test off, nothing about a file's
        # bytes can be established, so a stored verdict is trusted on its
        # stamp alone — exactly what it was before, and nothing is claimed.
        integrity_on = bool(config.get("audit_integrity", True))
        todo = []
        for path in files:
            _, verdict, changed = file_reads.get(path, (False, None, False))
            if verdict is not None:
                # CD files are re-checked even when a tag exists: they carry
                # the log/AccurateRip verdict, which outranks the stored one —
                # but only when one of those checks can actually run.
                if path in cd_files and (require_both or cd_verify_ran):
                    todo.append(path)
                    continue
                # A verdict is only trusted for the audio it was written for:
                # the record's stamp (replaced or re-encoded files move it) or
                # — since a script chain writes tags to every file it touches,
                # which moves the stamp and nothing else — its audio identity.
                # A file with no matching record (audited before the evidence
                # existed, or changed since) is audited once more and then has
                # one.
                #
                # With the integrity test on, those samples must ALSO have
                # been decoded and passed once: a verdict recorded by a run
                # that never ran that test (the setting was off, or the
                # version that wrote it had no such element) is re-established
                # here, once, and settles from then on.
                current, verified, rec_state = _evidence_state(path)
                if current and (not integrity_on or verified):
                    skipped += 1
                    if changed:
                        stats["modified_count"] += 1
                    # The decode was not paid for again, but its VERDICT is
                    # still this file's — the INTEGRITY tag is filled from the
                    # record for a library audited before that tag existed, so
                    # the file carries what the audit knows without a rewrite
                    # per run (a no-op once it already says it).
                    if (integrity_on and rec_state
                            and _write_integrity_value(path, rec_state, config)):
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
            log(c(f"Integrity: no flac/ffmpeg available - {len(todo)} file(s) "
                  f"NOT verified (no decoder to test them with)", Color.YELLOW))
            stats["errors"].append(("Integrity", "no flac/ffmpeg to verify with"))
        elif todo:
            # Only the files this run is actually about to audit: fail fast
            # and avoid an expensive AudioAuditor run on a file that is
            # already corrupt. A file the verdict/stamp pass above left out of
            # `todo` was verified for exactly these bytes already (that is
            # what `todo` means here), and `flac -t`/`ffmpeg -f null` decode
            # the whole file — re-decoding it proved nothing and cost a full
            # decode per file on every re-audit of an unchanged library.
            to_verify = []
            from .flac import MD5_ABSENT
            for p in todo:
                current, verified, _state = _evidence_state(p)
                if not (current and verified):
                    to_verify.append(p)
            untouched = len(todo) - len(to_verify)
            if untouched:
                log(f"Integrity: {untouched} file(s) already verified intact "
                    f"for their audio - not decoded again")

            def _check_one(p):
                ok, err = verify_integrity(p, ffmpeg_exe, flac_exe)
                return p, ok, err

            cw = worker_count(config, default=8, maximum=16,
                              items=len(to_verify))
            with ThreadPoolExecutor(max_workers=cw) as ex:
                futs = {ex.submit(_check_one, p): p for p in to_verify}
                from concurrent.futures import as_completed as _as_comp
                for fut in _as_comp(futs):
                    p, ok, err = fut.result()
                    # The answer belongs to these bytes: recorded so the next
                    # run over them does not decode the file again, and
                    # recorded either way — a FAIL must be re-established, not
                    # remembered as a pass.
                    _INTEGRITY_PASSED[_ev_key(p)] = bool(ok)
                    _record_evidence(p)
                    if not ok:
                        integrity_failed[p] = err or "integrity check failed"
                    # The container's own verdict on its stream MD5 (FLAC
                    # only) is written with the file's AUDIT tag — one
                    # container rewrite for both verdicts (_write_audit_tag);
                    # files no verdict phase opens get theirs from the fill
                    # pass at the end of the run.

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
            elif todo:
                # The count is the pass's own list, which is now only the
                # files this run audits — a file skipped on its evidence is
                # not "verified" by this pass and must not be counted as if it
                # were.
                log(f"Integrity: all {len(todo)} file(s) to be audited passed "
                    f"verification")

            # A stream that states NO MD5 decoded, but nothing verified it:
            # all-zero STREAMINFO MD5 means "unknown", not "fine", and reading
            # flac -t's rc=0 as a pass is exactly the silent acceptance this
            # report exists to end. Named per file, counted apart from the
            # failures (they are not failures) and apart from the passes.
            md5_absent = [p for p in to_verify
                          if _INTEGRITY_STATE.get(_ev_key(p)) == MD5_ABSENT]
            if md5_absent:
                log(c(f"Integrity: {len(md5_absent)} FLAC(s) state NO MD5 "
                      f"(STREAMINFO all zero) — decoded without error, but "
                      f"their audio is UNVERIFIED (INTEGRITY=UNKNOWN):",
                      Color.YELLOW))
                for p in sorted(md5_absent)[:10]:
                    try:
                        rel = os.path.relpath(p, folder)
                    except ValueError:
                        rel = os.path.basename(p)
                    log(f"  {c('?', Color.YELLOW)} {rel}")
                if len(md5_absent) > 10:
                    log(f"  … and {len(md5_absent) - 10} more")

    # A CD rip's verdict comes from its OWN verification, never from the
    # spectrogram detectors: the .log CRC (written above, as soon as it is
    # known) or a REAL .accurip. `audit_cd_require_both` only decides whether
    # AudioAuditor is ALSO run over MEDIA=CD — its verdict can add warning
    # flags, and decides only for a disc neither source could verify.

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

            # A MEDIA=CD verdict is NEVER AudioAuditor's to make, in either
            # direction. The three legs recorded by the gates around this loop
            # decide it, in the verdict phase at the end of the CD section: a
            # spectral "fake lossless" read of a synthetic tone cannot unmake a
            # rip whose own evidence checks out, and no spectral "Valid" can
            # stand in for evidence that is missing. What the tool has to say
            # is kept as EVIDENCE — a disagreement with a disc the log or the
            # .accurip verified is reported as a warning below, never written
            # as a verdict — and for every other release type it remains the
            # verdict, because there is nothing else to go on there.
            if canon(path) in cd_canon:
                tag_value = None
                if severity == "fail":
                    severity = "warn"
                    aa_status = cli_status or "a problem"
                    reason = (f"AudioAuditor reports {aa_status}, the rip's "
                              f"own evidence decides a CD (warning only)")

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
                            # The log's GRADE measures how much of the rip the
                            # log documents, not whether the audio is the audio
                            # that was ripped — so this is ONE leg of the CD
                            # verdict (the log's own score), never a short
                            # circuit past the other two. It used to be skipped
                            # for a track its .log CRC had just verified, which
                            # is how a disc with no gradeable log could still
                            # be tagged REAL.
                            set_leg(fp, "log-score", "fail",
                                    "the .log could not be graded "
                                    "(LOG_GRADE missing)")
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
                # A log checksum is never REQUIRED (spec R30). A log that never
                # carried one — XLD, or EAC older than 1.0 — claims nothing, so
                # nothing is refuted and the disc is judged on its per-track
                # CRCs alone. What IS required is that a checksum the log does
                # carry verifies: 'invalid' is the rip log contradicting itself
                # about its own bytes, and that fails the disc.
                if state == "invalid":
                    checksum_failed.setdefault(d, []).append((lp, detail or "invalid SHA256"))
                elif state == "missing":
                    # EAC writes the line from 1.0 on, so an absent line in a
                    # 1.x log means it was edited after EAC signed it. Reported
                    # — the run log names it — but never charged: "present ones
                    # must match, absent ones are not required" is the rule, and
                    # a charge here is the whole check required under another
                    # name. mlo.discs.check_log_checksum keeps the distinction so
                    # the viewer can still say which case a log is in.
                    log(c(f"WARNING: {os.path.basename(lp)} carries no 'Log "
                          f"checksum' line (EAC writes one from 1.0) — not "
                          f"required, so the disc is judged on its CRCs",
                          Color.YELLOW))
                elif state == "unverified":
                    # It carries one, and nothing could check it: Logchecker
                    # prints `checksum_ok` whether or not it found its pypi EAC
                    # helper, so a modified log reads identical to a clean one
                    # there. Named, never charged — the CRCs below decide the
                    # leg, and installing eac-logchecker closes the gap.
                    log(c(f"WARNING: {os.path.basename(lp)} carries a log "
                          f"checksum that was NOT verified ({detail}) — the "
                          f"disc is judged on its CRCs; install the EAC log "
                          f"checker to verify the log itself", Color.YELLOW))
                # 'ok' establishes the leg. 'unsupported' / 'missing' / None
                # leave it to the per-track CRCs.
                #
                # The log's own SHA256 is the second half of the disc's
                # "checksums" leg: a log that cannot be trusted about ITSELF
                # cannot be trusted about the CRCs it prints.
                for fp in trs:
                    if state == "ok":
                        set_leg(fp, "checksums", "ok")
                    elif state == "invalid":
                        set_leg(fp, "checksums", "fail",
                                "the rip log's EAC SHA256 does not verify")
        if checksum_failed:
            log(c(f"Audit FAIL on log checksum: {sum(len(v) for v in checksum_failed.values())} log(s) in {len(checksum_failed)} CD album(s) carry an invalid SHA256 checksum — marking their disc(s) as failed (the log's own checksum must verify when it is present)", Color.RED))
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
                        # No exemption for a track whose .log CRC matched: the
                        # verdict is the AND of the three legs, and a log that
                        # cannot be trusted about itself is exactly what this
                        # leg records (set_leg above).
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
                    for fp in trs:
                        if fp not in files:
                            continue
                        if ar_generator:
                            ar_failed_per_file[fp] = "Missing .accurip file (CUETools)"
                            set_leg(fp, "accuraterip", "fail",
                                    "no .accurip file for the disc — nothing "
                                    "was checked (run AccurateRip (script 9))")
                        else:
                            # No file and no tool that could make one: the leg
                            # is un-evaluated, so the file keeps no verdict and
                            # the run names this leg as the missing one.
                            set_leg(fp, "accuraterip", "unknown",
                                    "no .accurip and no CUETools to make one")
                    continue
                ar_files_seen += 1
                try:
                    txt_ar = open(ap, "r", encoding="utf-8", errors="replace").read()
                except OSError as e:
                    for fp in trs:
                        if fp in files:
                            ar_failed_per_file[fp] = f"cannot read .accurip: {e}"
                            set_leg(fp, "accuraterip", "fail",
                                    f"the .accurip cannot be read: {e}")
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
                                set_leg(fp, "accuraterip", "fail",
                                        "the .accurip reports No match")
                    elif overall_st == "NONE":
                        for fp in trs:
                            if fp in files:
                                ar_failed_per_file[fp] = overall_detail or "Missing/unscorable AccurateRip in .accurip"
                                # A database MISS is not a mismatch: this
                                # pressing is not in AccurateRip at all, so
                                # the leg cannot pass and nothing was refuted.
                                # The verdict is still not REAL (the rule
                                # needs AccurateRip to pass) but the reason
                                # says which of the two it is, so the user
                                # reads "nobody could check this disc" and
                                # not "your rip is bad".
                                set_leg(fp, "accuraterip", "fail",
                                        "the .accurip holds no AccurateRip "
                                        "verdict — this pressing is not in the "
                                        "database, so nothing was checked")
                    else:
                        for fp in trs:
                            if fp in files:
                                set_leg(fp, "accuraterip", "ok")
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
                            set_leg(fp, "accuraterip", "fail",
                                    "AccurateRip reports No match for the track")
                        elif st == "NONE":
                            ar_failed_per_file[fp] = "Track not present in AccurateRip database"
                            set_leg(fp, "accuraterip", "fail",
                                    "the track is not in the AccurateRip "
                                    "database — nothing was checked for it")
                        else:
                            set_leg(fp, "accuraterip", "ok")
                        # REAL → pass, do not add
                    except Exception:
                        continue
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

    # ---- the third leg: the rip log's own SCORE ---------------------------
    # Logchecker scores each disc's rip log 0-100 (written to LOG_GRADE).
    # `audit_log_score_threshold` at 0 leaves the leg unrequired; past it the
    # score must reach the threshold. A disc whose log could not be scored at
    # all is an UN-EVALUATED leg — reported by name, never guessed: writing
    # FAKE would blame the rip for a missing scorer or a log nobody graded,
    # and the gate above is what fails a log that is missing or ungradeable.
    try:
        thr_a = int(config.get("audit_log_score_threshold", 100) or 0)
        thr_a = max(0, min(100, thr_a))
    except Exception:
        thr_a = 0
    if thr_a > 0 and cd_candidate_dirs:
        from .discs import album_discs as _ad_thr

        def _discs_of(album_dir):
            """{disc number: [tracks]} for one CD album, with the single-disc
            fallback the other CD phases use: an album whose files carry no
            disc number still has one disc, and its one .log still scores."""
            try:
                discs_here = _ad_thr(album_dir) or {}
            except Exception:
                discs_here = {}
            if not discs_here:
                try:
                    aud = [os.path.join(album_dir, f)
                           for f in os.listdir(album_dir)
                           if f.lower().endswith(CD_AUDIO_EXTS)]
                except OSError:
                    aud = []
                if aud:
                    discs_here = {1: aud}
            return discs_here

        def _stored_log_grade(trs):
            """The 0-100 LOG_GRADE the tracks of one disc already carry.

            `grade_album_logs` SKIPS a disc whose tracks all hold a valid
            LOG_GRADE (no reason to re-score the same log), so an empty score
            map means either "this disc was already graded" or "nothing could
            score it" — and the FILE says which, the same way the scorer itself
            trusts a stored grade. Returns None unless every track carries the
            same 0-100 value, so a half-written grade is never read as a score.
            """
            values = set()
            for p in trs:
                try:
                    val = str(AudioFile(p).get_tag("LOG_GRADE") or "").strip()
                except Exception:
                    return None
                if not (val.isdigit() and 0 <= int(val) <= 100):
                    return None
                values.add(val)
            return int(values.pop()) if len(values) == 1 else None

        log_ok, log_below, log_unscored = {}, {}, {}
        for d in cd_candidate_dirs:
            scores = log_scores.get(d) or {}
            unscorable = set(log_unscorable.get(d) or ())
            for disc_n, trs in sorted(_discs_of(d).items()):
                sc = None if disc_n in unscorable else scores.get(disc_n)
                if sc is None and disc_n not in unscorable:
                    sc = _stored_log_grade(trs)
                if not isinstance(sc, int):
                    log_unscored.setdefault(d, []).append((disc_n, trs, sc))
                elif sc < thr_a:
                    log_below.setdefault(d, []).append((disc_n, sc, trs))
                else:
                    log_ok.setdefault(d, []).append((disc_n, sc, trs))

        total_below = sum(len(v) for v in log_below.values())
        if total_below:
            log(c(f"Audit FAIL on log score threshold: {total_below} disc(s) in {len(log_below)} CD album(s) below {thr_a}/100 — marking their disc(s) as failed (audit_log_score_threshold on)", Color.RED))
        for d, lst in log_below.items():
            for disc_n, sc, trs in lst:
                for fp in trs:
                    if fp not in files:
                        continue
                    set_leg(fp, "log-score", "fail",
                            f"the rip log scores {sc}/100, below the "
                            f"{thr_a}/100 threshold")
                    mark_fake(fp, f"log score {sc} below threshold {thr_a}",
                              f"log score < {thr_a}")
        for d, lst in log_ok.items():
            for disc_n, sc, trs in lst:
                for fp in trs:
                    if fp in files:
                        set_leg(fp, "log-score", "ok")
        # Nothing here scored this disc: say which of the two reasons it is, so
        # the missing leg the verdict phase reports is actionable.
        for d, lst in log_unscored.items():
            for disc_n, trs, sc in lst:
                if sc is None and scorer_present:
                    why = ("no rip-log score: the tracks carry no LOG_GRADE and "
                           "Logchecker did not produce one for this .log")
                elif sc is None:
                    why = ("no rip-log score: the tracks carry no LOG_GRADE and "
                           "Logchecker/PHP is not installed, so none could be "
                           "produced")
                else:
                    why = f"the rip log's score is not a number ({sc!r})"
                for fp in trs:
                    if fp in files:
                        set_leg(fp, "log-score", "unknown", why)


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


    # ---- the one place a MEDIA=CD verdict is written ------------------------
    # REAL exactly when the three legs all pass: the rip log scores at/above
    # `audit_log_score_threshold`, the disc's checksums match the audio (the
    # per-track CRCs, and the log's own EAC SHA256 when
    # `audit_verify_log_checksum` is on), and AccurateRip passes
    # (`audit_require_accuraterip`). Each leg is required only while its own
    # setting requires it — switching one off is an escape hatch, and it
    # removes that leg rather than weakening the other two.
    #
    # One leg failing is a FAKE: the evidence is against the rip. One leg the
    # machine could not evaluate (no ffmpeg to decode, no .accurip and no
    # CUETools to make one, no Logchecker to score the log) leaves the file
    # WITHOUT a verdict and names the missing leg in the run log — writing
    # REAL would claim evidence nobody produced, and writing FAKE would blame
    # the rip for a tool that is not installed.
    #
    # AudioAuditor has no vote here at all: its read is kept as evidence (a
    # disagreement is reported as a warning by the audit loop above) and it
    # decides every release type that is not a CD.
    cd_verdict_unresolved = []
    cd_verdict_failed = {}
    for fp in sorted(cd_files):
        if is_video_file(fp):
            continue
        if not should_write_audio_tag(config, "AUDIT", filepath=fp):
            continue
        legs = cd_legs.get(_ev_key(fp))
        if not legs:
            # Every leg switched off: nothing says anything about a CD, so the
            # tag it already carries is left exactly as it was.
            continue
        failed = sorted(name for name, (state, _why) in legs.items()
                        if state == "fail")
        if failed:
            verdict = "FAKE"
            for name in failed:
                why, count = cd_verdict_failed.get(name, (legs[name][1], 0))
                cd_verdict_failed[name] = (why, count + 1)
        elif any(state == "unknown" for state, _why in legs.values()):
            cd_verdict_unresolved.append((fp, legs))
            continue
        else:
            verdict = "REAL"
        if not config.get("write_audit_tag", True):
            continue
        changed, b_rem, b_add, err = _write_audit_tag(fp, verdict)
        if err:
            stats["errors"].append((os.path.basename(fp), err))
        elif changed:
            stats["modified_count"] += 1
            stats["total_bytes_removed"] += b_rem
            stats["total_bytes_added"] += b_add

    if cd_verdict_failed:
        # Which leg cost the verdict, by name and reason: a CD is FAKE because
        # of one line of its own evidence, and the run log has to say which —
        # "the disc is not in the AccurateRip database" and "the .log CRC does
        # not match the audio" are different problems for the user.
        log(c("CD verdict FAKE — the evidence that failed:", Color.RED))
        for name in sorted(cd_verdict_failed):
            why, count = cd_verdict_failed[name]
            log(c(f"  {name} ({count} track(s)): {why}", Color.RED))

    if cd_verdict_unresolved:
        by_leg = {}
        for fp, legs in cd_verdict_unresolved:
            for name, (state, why) in legs.items():
                if state == "unknown":
                    by_leg.setdefault(name, (why, []))[1].append(fp)
        log(c(f"CD evidence incomplete: {len(cd_verdict_unresolved)} CD "
              f"track(s) keep no AUDIT verdict — a verdict needs evidence, and "
              f"none of it could be evaluated here:", Color.YELLOW))
        for name in sorted(by_leg):
            why, paths = by_leg[name]
            log(c(f"  missing '{name}' evidence ({len(paths)} track(s)): {why}",
                  Color.YELLOW))
        stats["errors"].append((
            "CD verdict",
            "incomplete evidence — no verdict written for "
            + ", ".join(sorted(by_leg))))

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

    # ---- the INTEGRITY tag for everything no verdict phase opened --------
    # A file whose AUDIT verdict this run wrote already carries its INTEGRITY
    # value out of that same rewrite, and a file no phase opened (a library
    # with no spectral tool, a file AudioAuditor could not decide, a CD whose
    # verdict its own evidence gave) gets it here: the verdict the integrity
    # test established for these bytes, OK / FAIL / UNKNOWN, one open per file
    # that still needs it and none for the ones that have it.
    filled = 0
    for p in files:
        key = _ev_key(p)
        if not _INTEGRITY_STATE.get(key) or key in _INTEGRITY_WRITTEN:
            continue
        if _write_integrity_value(p, _INTEGRITY_STATE[key], config):
            filled += 1
    if filled:
        stats["modified_count"] += filled
        log(f"Integrity: wrote an INTEGRITY verdict into {filled} file(s)")

    # Every AUDIT verdict this run wrote is bound to the file's size/mtime,
    # so the next run may skip a file without trusting a tag written for
    # different bytes.
    _save_evidence(config, _EVIDENCE)
    _EVIDENCE_DIRTY = False

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
