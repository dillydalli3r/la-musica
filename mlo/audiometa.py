"""Key & BPM detection (script 12).

For every track it decodes audio with librosa (vendored into
.dependencies via pip, see fetchdeps.PIP_PACKAGES) and writes:

  * BPM        - from dual estimators (tempogram + median beat interval),
                 folded into the 70-180 range and rounded to a whole number,
  * INITIALKEY - from the harmonic chroma correlated against an ensemble of
                 Krumhansl-Schmuckler, Temperley and Albrecht-Shanahan
                 profiles, rendered in musical notation with FLAT spellings
                 ('B♭ min'), Camelot ('8A') or Open Key ('1m').

Skips tracks that already carry both tags unless overwrite/force is set,
and respects the per-filetype audio_tag_writes gates (BPM / INITIALKEY).

The engine is the app's own Rust helper (`rust/`, the `mlo-audio` binary):
one `analyze` call decodes the track once and returns tempo, key and the mood
features, so a decoded track never travels into Python. The librosa
implementations below stay as the fallback for a build without the helper (the
dependencies are optional), and as the reference the helper's parity test
compares against.
"""
import json
import math
import os
import sys

from .audio import AudioFile
from .config import should_write_audio_tag
from .dr import DECODE_TIMEOUT, rust_helper
from .paths import AUDIO_EXTS
from .subproc import run_tool
from .stats import (
    new_stats, _make_pbar, _pbar_skip, _pbar_update, _walk_files,
    _collect_targets, is_audio_file, worker_count, bound_numeric_threads,
)
from .tools import python_pkg_path
from .ui import print_header, log, c, Color
from concurrent.futures import ThreadPoolExecutor, as_completed

# Krumhansl-Schmuckler key profiles (major, minor), indexed from C.
_KS_MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
_KS_MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
# Temperley / Kostka-Payne profiles (from music-cognition corpus studies).
_TEMPERLEY_MAJOR = [0.748, 0.060, 0.488, 0.082, 0.670, 0.460, 0.096, 0.715, 0.104, 0.366, 0.057, 0.400]
_TEMPERLEY_MINOR = [0.712, 0.084, 0.474, 0.618, 0.049, 0.460, 0.105, 0.747, 0.404, 0.067, 0.133, 0.330]
# Albrecht & Shanahan (2013) profiles — the newest of the three.
_ALBRECHT_MAJOR = [0.900, 0.091, 0.211, 0.137, 0.344, 0.382, 0.130, 0.800, 0.099, 0.207, 0.094, 0.306]
_ALBRECHT_MINOR = [0.938, 0.110, 0.268, 0.513, 0.188, 0.418, 0.086, 0.868, 0.450, 0.108, 0.065, 0.317]
# The ensemble: correlating all three against the chroma and averaging the
# scores per (tonic, mode) is markedly more accurate than any single profile.
_KEY_PROFILES = (
    (_KS_MAJOR, _KS_MINOR),
    (_TEMPERLEY_MAJOR, _TEMPERLEY_MINOR),
    (_ALBRECHT_MAJOR, _ALBRECHT_MINOR),
)
# Musical notation uses FLAT spellings (B♭, E♭, A♭…) — the user-facing
# convention. A detected G# is written "A♭".
_PITCHES_FLAT = ["C", "D♭", "D", "E♭", "E", "F", "G♭", "G", "A♭", "A", "B♭", "B"]
# Camelot wheel: each position n covers nB (major) + its relative nA (minor).
# C major = 8B, A minor = 8A, stepping by fifths. Indexed by tonic from C.
_CAMELOT_MAJOR = ["8B", "3B", "10B", "5B", "12B", "7B", "2B", "9B", "4B", "11B", "6B", "1B"]
_CAMELOT_MINOR = ["5A", "12A", "7A", "2A", "9A", "4A", "11A", "6A", "1A", "8A", "3A", "10A"]
# Open Key: 1d = C major, 1m = A minor, same circle of fifths ordering.
_OPENKEY_MAJOR = ["1d", "8d", "3d", "10d", "5d", "12d", "7d", "2d", "9d", "4d", "11d", "6d"]
_OPENKEY_MINOR = ["10m", "5m", "12m", "7m", "2m", "9m", "4m", "11m", "6m", "1m", "8m", "3m"]


# ----------------------------------------------------------------------
# Vendored librosa
# ----------------------------------------------------------------------
_LIBROSA_VERSION = None
_LIBROSA_PROBED = False


def _ensure_librosa():
    """Prepend the vendored librosa folder to sys.path; returns the version
    string or None when the dependency is not installed.

    Answered once per process: the vendored folder the app ships never moves
    while a run is in flight, and the probe is not free — it walks the tools
    folder and reads a .dist-info. mlo.moods asks this from classify(), i.e.
    once per track, where the walk added up to a second of directory I/O per
    thousand tracks on top of work that had not started yet."""
    global _LIBROSA_VERSION, _LIBROSA_PROBED
    if _LIBROSA_PROBED:
        return _LIBROSA_VERSION
    _LIBROSA_PROBED = True
    path = python_pkg_path("librosa")
    if path and path not in sys.path:
        sys.path.insert(0, path)
    try:
        import librosa
        _LIBROSA_VERSION = getattr(librosa, "__version__", "?")
    except Exception:
        _LIBROSA_VERSION = None
    return _LIBROSA_VERSION


def _load_signal(path, sr, max_seconds=None):
    """(y, sr) for one file — librosa, with the bundled ffmpeg as fallback.

    The vendored librosa decodes through soundfile and then audioread, and
    audioread only reaches ffmpeg when one is on PATH — the app's own
    toolchain is not. So .aac (raw ADTS), the MP4/M4A family and the
    music-video containers (all first-class, graded tracks) are decoded with
    the DETECTED ffmpeg instead of being silently skipped. Both routes return
    (None, None) on failure, never raise.

    The fallback decodes ONCE, straight to raw 16-bit PCM on a pipe. It used
    to have ffmpeg write a temp WAV and then have librosa read that WAV back:
    a second full copy of the track — ~160 MB for an hour of audio — through
    the filesystem for every undecodable file, plus a `.decode_*` file left
    behind when a run was killed mid-decode. The samples are the same ones
    either way: the WAV route was int16 (ffmpeg's default for `-f wav`) and
    libsndfile scales int16 by 1/32768, which is exactly what the conversion
    below does. `-ar` already resampled, so there was never a second resample
    to preserve.
    """
    import librosa

    try:
        return librosa.load(path, sr=sr, mono=True, duration=max_seconds)
    except Exception:
        pass

    try:
        from .subproc import run_tool
        from .tools import detect_all_tools
        ffmpeg = (detect_all_tools().get("ffmpeg") or {}).get("ffmpeg_exe")
    except Exception:
        ffmpeg = None
    if not ffmpeg:
        return None, None

    try:
        import numpy as np

        cmd = [ffmpeg, "-y", "-v", "error", "-nostdin", "-threads", "1",
               "-i", path, "-vn"]
        if max_seconds:
            cmd += ["-t", str(max_seconds)]
        cmd += ["-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
        proc = run_tool(cmd, capture_output=True, timeout=30 * 60)
        if proc.returncode != 0 or not proc.stdout:
            return None, None
        raw = proc.stdout
        proc = None
        # int16 -> float32 at libsndfile's own scale (what a temp-WAV read
        # would have produced), then the 2-bytes-per-sample pipe buffer goes.
        y = np.frombuffer(raw, dtype="<i2").astype(np.float32)
        del raw
        y /= np.float32(32768.0)
        return y, sr
    except Exception:
        return None, None


# ----------------------------------------------------------------------
# The Rust helper (rust/, the `mlo-audio` binary)
# ----------------------------------------------------------------------
# The engine for this pass: one `analyze` call spawns ffmpeg, decodes the
# track and returns tempo, key and the mood features as JSON. The discovery is
# `mlo.dr.rust_helper()`'s (a helper built for DYNAMIC RANGE is this one too),
# reused rather than duplicated.
_FFMPEG_PROBED = False
_FFMPEG_EXE = None


def _ffmpeg_exe():
    """The bundled or system ffmpeg, probed once per process."""
    global _FFMPEG_PROBED, _FFMPEG_EXE
    if _FFMPEG_PROBED:
        return _FFMPEG_EXE
    _FFMPEG_PROBED = True
    try:
        from .tools import detect_all_tools
        _FFMPEG_EXE = (detect_all_tools().get("ffmpeg") or {}).get("ffmpeg_exe")
    except Exception:
        _FFMPEG_EXE = None
    return _FFMPEG_EXE


def _analyze_with_rust(path, *, sr=22050, max_seconds=None, trim=False):
    """Parsed `mlo-audio analyze` JSON, or None when the helper cannot be used.

    None means "fall back to librosa": no helper, no ffmpeg, a helper that
    will not start, prints nothing, or prints something that is not its
    contract. A helper that DID run returns its dict — the caller decides what
    a reported `failed` means (the librosa path is the same decode, so a real
    decode failure is not hidden).
    """
    helper = rust_helper()
    if helper is None:
        return None
    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        return None
    argv = [helper, "analyze", "--ffmpeg", str(ffmpeg), "--input", path,
            "--sr", str(int(sr))]
    if max_seconds:
        argv += ["--max-seconds", str(max_seconds)]
    if trim:
        argv += ["--trim"]
    try:
        proc = run_tool(argv, capture_output=True, text=True,
                        timeout=DECODE_TIMEOUT)
    except Exception:
        return None
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return None
    try:
        data = json.loads(lines[-1])
    except ValueError:
        return None
    if not isinstance(data, dict) or "failed" not in data:
        return None
    return data


def _helper_key_bpm(path, min_seconds):
    """(bpm, key) from the helper, or None when it cannot answer.

    None means "run the librosa path". A tuple — even ``(None, None)`` — is a
    real answer: the helper decoded the track but found it too short (or found
    no tempo/key), which is what `detect_key_bpm` returns for those.
    """
    data = _analyze_with_rust(path, sr=22050, trim=True)
    if data is None or data.get("failed"):
        return None
    duration = float((data.get("features") or {}).get("duration") or 0.0)
    if duration < max(1, min_seconds):
        return None, None
    bpm = data.get("bpm")
    key = None
    idx = data.get("key_index")
    if idx is not None:
        key = (int(idx), bool(data.get("key_minor")))
    return (int(round(float(bpm))) if bpm is not None else None), key


def _key_notation(tonic_idx, minor, notation):
    """Render a detected key in the configured notation."""
    tonic = _PITCHES_FLAT[tonic_idx % 12]
    if notation == "camelot":
        return _CAMELOT_MINOR[tonic_idx] if minor else _CAMELOT_MAJOR[tonic_idx]
    if notation == "openkey":
        return _OPENKEY_MINOR[tonic_idx] if minor else _OPENKEY_MAJOR[tonic_idx]
    return f"{tonic} {'min' if minor else 'maj'}"


def _fold_bpm(v):
    """Fold an octave error (half/double time) into the 70-180 DJ range."""
    while v < 70:
        v *= 2
    while v > 180:
        v /= 2
    return v


def _detect_bpm(y, sr, onset=None):
    """BPM from two independent estimators, preferring their agreement.

    1. the autocorrelation tempogram estimate (librosa tempo),
    2. the median of the ACTUAL beat intervals from beat tracking seeded
       with that estimate.

    When they agree within 8% they are averaged (two weak measurements in
    agreement are stronger than either alone); on disagreement the
    beat-interval median wins — it is measured, not interpolated. Both are
    folded into the 70-180 range before combining. Returns None on failure.

    *onset* is the onset envelope of ``y`` when the caller already computed it
    (mlo.moods measures the same signal at the same hop): the mel spectrogram
    behind it is one of the expensive steps of the analysis.
    """
    import numpy as np
    import librosa

    hop = 512
    if onset is None:
        onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    tempo_fn = getattr(librosa.feature, "tempo", None) or getattr(librosa.beat, "tempo", None)
    t_est = None
    if tempo_fn is not None:
        est = tempo_fn(onset_envelope=onset, sr=sr, hop_length=hop, aggregate=np.median)
        est = float(np.atleast_1d(est)[0])
        if np.isfinite(est) and est > 0:
            t_est = est
    t_med = None
    try:
        _, beats = librosa.beat.beat_track(
            onset_envelope=onset, sr=sr, hop_length=hop,
            start_bpm=t_est or 120.0, trim=True)
        if len(beats) >= 4:
            iv = np.diff(beats) * (hop / float(sr))
            med = float(np.median(iv))
            if np.isfinite(med) and med > 0:
                t_med = 60.0 / med
    except Exception:
        t_med = None
    cands = [v for v in (t_est, t_med) if v and np.isfinite(v) and v > 0]
    if not cands:
        return None
    folded = [_fold_bpm(v) for v in cands]
    if len(folded) == 2 and abs(folded[0] - folded[1]) / max(folded) <= 0.08:
        return int(round((folded[0] + folded[1]) / 2))
    return int(round(folded[-1]))


def _detect_key(y, sr, y_harmonic=None):
    """Musical key from the harmonic part of the signal.

    Accuracy comes from four choices:
      * the harmonic component (percussion removed) feeds the chroma,
      * lead-in/lead-out silence is trimmed so quiet noise can't skew it,
      * the chroma is summarized with BOTH the median (robust against
        repeated choruses dominating) and the mean (sensitivity), pooled,
      * three published key profiles (Krumhansl-Schmuckler, Temperley,
        Albrecht-Shanahan) are correlated and their scores averaged.
    Returns (tonic, minor) or None.

    *y_harmonic* is that harmonic component when the caller already separated
    it (mlo.moods does, from the same ``librosa.effects.hpss`` with the same
    margin): harmonic-percussive separation is the single most expensive step
    in the analysis and this used to compute a second identical one.
    """
    import numpy as np
    import librosa

    if y_harmonic is not None:
        y_h = y_harmonic
    else:
        try:
            y_h = librosa.effects.harmonic(y, margin=3.0)
        except Exception:
            y_h = y
    chroma = librosa.feature.chroma_cqt(y=y_h, sr=sr, hop_length=2048)
    chroma = np.asarray(chroma)
    if chroma.size == 0:
        return None
    mean_chroma = chroma.mean(axis=1)
    med_chroma = np.median(chroma, axis=1)
    vec = mean_chroma + med_chroma
    if not np.isfinite(vec).all() or vec.sum() <= 0:
        return None
    vec = vec / vec.sum()

    def _corr(a, b):
        r = float(np.corrcoef(a, b)[0, 1])
        return r if math.isfinite(r) else 0.0

    best = (-2.0, 0, False)
    for idx in range(12):
        rotated = np.roll(vec, -idx)
        for minor in (False, True):
            scores = [_corr(rotated, prof[1 if minor else 0]) for prof in _KEY_PROFILES]
            avg = sum(scores) / len(scores)
            if avg > best[0]:
                best = (avg, idx, minor)
    return best[1], best[2]


def detect_key_bpm(path, min_seconds=10):
    """Analyze one audio file. Returns (bpm:int|None, key:(tonic,minor)|None).

    BPM from dual estimators (tempogram + median beat interval), key from
    the harmonic chroma against an ensemble of published key profiles.
    Returns (None, None) for tracks shorter than *min_seconds* (too short
    for stable estimates).

    The Rust helper answers first when it is present; the librosa path below
    is the fallback (and the reference the parity test compares against).
    """
    got = _helper_key_bpm(path, min_seconds)
    if got is not None:
        return got

    import numpy as np
    import librosa

    sr = 22050
    # The WHOLE track is decoded here, deliberately without mlo.moods'
    # 120 s cap: the key comes from a chroma over the entire signal and the
    # tempo from its full onset envelope, so the tag for a long DJ set or a
    # concert recording is a different number from the one its first two
    # minutes would give. A cap would change tags, not just timing.
    y, _ = _load_signal(path, sr)
    if y is None:
        return None, None
    if y.size < sr * max(1, min_seconds):
        return None, None
    # Trim lead-in/lead-out silence — near-silent padding corrupts both the
    # onset envelope and the chroma statistics.
    try:
        y, _ = librosa.effects.trim(y, top_db=35)
    except Exception:
        pass
    if y.size < sr * max(1, min_seconds):
        return None, None

    try:
        bpm = _detect_bpm(y, sr)
    except Exception:
        bpm = None
    try:
        key = _detect_key(y, sr)
    except Exception:
        key = None

    return bpm, key


# ----------------------------------------------------------------------
# Track selection / tag writing
# ----------------------------------------------------------------------
def _track_paths(config):
    folder = config["music_folder"]
    if config.get("targets") is not None:
        return sorted(_collect_targets(config["targets"], AUDIO_EXTS))
    if not os.path.isdir(folder):
        return []
    return sorted(_walk_files(folder, AUDIO_EXTS))


def _needs_analysis(path, force, overwrite):
    """(needed, af) — whether a tag is missing (or forced), and the handle it
    was decided from.

    The open handle is handed back so the write pass below reuses it: opening
    the file here and again in _write_tags parsed every analysed container
    twice for the same answers."""
    if force:
        return True, None
    try:
        af = AudioFile(path)
        has_bpm = bool(str(af.get_tag("BPM") or "").strip())
        has_key = bool(str(af.get_tag("INITIALKEY") or "").strip())
    except Exception:
        return True, None
    return (overwrite or not (has_bpm and has_key)), af


def _write_tags(path, bpm, key_str, config, af=None):
    """Write BPM/INITIALKEY respecting per-filetype gates. Returns True when
    the file changed. *af* is the already-open handle from _needs_analysis,
    when there is one.

    Both tags are held for ONE flush: set two tags and the container used to
    be rewritten twice for the same run — a full rewrite of the file, for a
    tag that arrived at the same moment as its sibling.
    """
    changed = False
    try:
        af = af or AudioFile(path)
        # A handle that only implements the get/set contract (a caller's stub)
        # writes per tag, like before.
        defer = hasattr(af, "defer_save")
        if defer:
            af.defer_save(True)
        try:
            if bpm is not None and should_write_audio_tag(config, "BPM", filepath=path):
                if str(af.get_tag("BPM") or "").strip() != str(bpm):
                    if af.set_tag("BPM", str(bpm)):
                        changed = True
            if key_str and should_write_audio_tag(config, "INITIALKEY", filepath=path):
                if str(af.get_tag("INITIALKEY") or "").strip() != key_str:
                    if af.set_tag("INITIALKEY", key_str):
                        changed = True
        finally:
            # A failed flush wrote nothing, so the file is not reported as
            # changed; the deferral is always turned off again either way.
            if defer and af.defer_save(False) is not True:
                changed = False
    except Exception:
        return False
    return changed


# ----------------------------------------------------------------------
# Script entry point
# ----------------------------------------------------------------------
def run_analyze_audiometa(config):
    folder = config["music_folder"]
    stats = new_stats()

    if not config.get("audiometa_enabled", True):
        print_header("Key & BPM (skipped - disabled in settings)")
        return stats

    print_header("Key & BPM Detection")
    log(f"music folder: {folder}")

    version = _ensure_librosa()
    if not version:
        log(c("ERROR: librosa is not installed. Use Dependencies to install it.",
              Color.RED))
        stats["error_count"] += 1
        stats["errors"].append(("librosa", "not installed"))
        return stats
    log(f"librosa v{version} · notation={config.get('audiometa_key_notation', 'musical')}")

    force = config.get("force_audiometa", False)
    overwrite = config.get("audiometa_overwrite", False)
    min_seconds = int(config.get("audiometa_min_seconds", 10) or 10)

    # The tag pre-pass opens ONE container per file to ask whether it needs the
    # analysis; it used to run on the runner thread, so the whole library was
    # parsed (and the lanes sat idle) before the first decode started. Every
    # question is about its own file, so it is the same pool, one phase
    # earlier; the handle it hands back is what the write pass reuses.
    tracks = _track_paths(config)
    scan_workers = worker_count(config, maximum=8, items=len(tracks))
    pending = {}
    if tracks:
        with ThreadPoolExecutor(max_workers=scan_workers) as ex:
            for p, (needed, af) in zip(
                    tracks, ex.map(lambda t: _needs_analysis(t, force, overwrite),
                                   tracks)):
                if needed:
                    pending[p] = af
    if not pending:
        log("Nothing to analyze (all tracks already tagged).")
        return stats

    notation = config.get("audiometa_key_notation", "musical")
    paths = sorted(pending)
    workers = worker_count(config, maximum=8, items=len(paths))
    # Whole-file librosa decodes run in *workers* lanes, and each one's numpy
    # is a multi-threaded pool of its own: uncapped, the step occupied
    # workers × cores and the Worker threads setting bounded nothing (R79).
    bound_numeric_threads(config, workers)
    counts = {"ok": 0, "skip": 0, "fail": 0}
    pbar = _make_pbar(len(paths), "Key & BPM", unit="file")

    def _task(path):
        """Decode, measure and write ONE file — all on this file's own lane.

        The write used to happen in _finish, on the runner thread, while the
        pool's lanes waited behind it: every modified file is a full copy of
        the container beside it plus one os.replace (mlo.atomic), and the
        pass wrote them strictly one at a time with every decoder idle.
        Nothing here is shared — each lane holds the one handle from the
        pre-pass, and no other lane touches this path — so the write rides
        along with the analysis exactly as mlo.moods already writes inside
        its lane. Returns ``(path, bpm, key, error, changed)``.
        """
        try:
            bpm, key = detect_key_bpm(path, min_seconds)
        except Exception as e:
            return path, None, None, str(e), False
        if bpm is None and key is None:
            return path, None, None, None, False
        key_str = ""
        if key:
            tonic, minor = key
            key_str = _key_notation(tonic, minor, notation)
        changed = _write_tags(path, bpm, key_str, config,
                              af=pending.get(path))
        return path, bpm, key, None, changed

    def _finish(path, bpm, key, err, changed):
        # Every file this pass looked at is SCANNED, whatever came of it —
        # the run's numbers have to add up (scanned == modified + skipped +
        # errors, README's R10a), and a file that was analysed and needed no
        # tag change was reported as skipped while the scanned counter stayed
        # at zero: a whole 31-track pass read "0 scanned · 0 modified · 31
        # skipped".
        stats["total_scanned"] += 1
        if err is not None:
            stats["error_count"] += 1
            stats["errors"].append((os.path.basename(path), err))
            _pbar_update(pbar, counts, kind="fail")
            return
        if bpm is None and key is None:
            stats["skipped_count"] += 1
            _pbar_skip(pbar, counts)
            return
        if changed:
            stats["modified_count"] += 1
            _pbar_update(pbar, counts, kind="ok")
        else:
            stats["skipped_count"] += 1
            _pbar_skip(pbar, counts)

    if len(paths) == 1 or workers == 1:
        try:
            for path in paths:
                _finish(*_task(path))
        except KeyboardInterrupt:
            raise
        finally:
            if pbar:
                pbar.close()
    else:
        try:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(_task, p): p for p in paths}
                for fut in as_completed(futures):
                    _finish(*fut.result())
        finally:
            if pbar:
                pbar.close()

    stats["is_grader"] = False
    return stats
