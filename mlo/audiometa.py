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
"""
import math
import os
import sys
import tempfile

from .audio import AudioFile
from .config import should_write_audio_tag
from .paths import AUDIO_EXTS
from .stats import (
    new_stats, _make_pbar, _pbar_skip, _pbar_update, _walk_files,
    _collect_targets, is_audio_file, worker_count,
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
def _ensure_librosa():
    """Prepend the vendored librosa folder to sys.path; returns the version
    string or None when the dependency is not installed."""
    path = python_pkg_path("librosa")
    if path and path not in sys.path:
        sys.path.insert(0, path)
    try:
        import librosa
        return getattr(librosa, "__version__", "?")
    except Exception:
        return None


def _load_signal(path, sr, max_seconds=None):
    """(y, sr) for one file — librosa, with the bundled ffmpeg as fallback.

    The vendored librosa decodes through soundfile and then audioread, and
    audioread only reaches ffmpeg when one is on PATH — the app's own
    toolchain is not. So .aac (raw ADTS), the MP4/M4A family and the
    music-video containers (all first-class, graded tracks) are decoded with
    the DETECTED ffmpeg instead of being silently skipped. Both routes return
    (None, None) on failure, never raise.
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

    fd, tmp = tempfile.mkstemp(suffix=".wav", prefix=".decode_")
    os.close(fd)
    try:
        cmd = [ffmpeg, "-y", "-v", "error", "-nostdin", "-threads", "1",
               "-i", path, "-vn"]
        if max_seconds:
            cmd += ["-t", str(max_seconds)]
        cmd += ["-ac", "1", "-ar", str(sr), "-f", "wav", tmp]
        proc = run_tool(cmd, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=30 * 60)
        if proc.returncode != 0:
            return None, None
        return librosa.load(tmp, sr=sr, mono=True)
    except Exception:
        return None, None
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


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
    """
    import numpy as np
    import librosa

    sr = 22050
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

    pending = {}
    for p in _track_paths(config):
        needed, af = _needs_analysis(p, force, overwrite)
        if needed:
            pending[p] = af
    if not pending:
        log("Nothing to analyze (all tracks already tagged).")
        return stats

    notation = config.get("audiometa_key_notation", "musical")
    paths = sorted(pending)
    workers = worker_count(config, default=4, maximum=8, items=len(paths))
    counts = {"ok": 0, "skip": 0, "fail": 0}
    pbar = _make_pbar(len(paths), "Key & BPM", unit="file")

    def _task(path):
        try:
            bpm, key = detect_key_bpm(path, min_seconds)
        except Exception as e:
            return path, None, None, str(e)
        return path, bpm, key, None

    def _finish(path, bpm, key, err):
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
        key_str = ""
        if key:
            tonic, minor = key
            key_str = _key_notation(tonic, minor, notation)
        if _write_tags(path, bpm, key_str, config, af=pending.get(path)):
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
