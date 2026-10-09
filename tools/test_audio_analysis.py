#!/usr/bin/env python3
"""The Rust analysis engine: tempo, key and the mood features.

`mlo.audiometa.detect_key_bpm` and `mlo.moods.classify` used to be two librosa
passes — together 11 of a 20-second local chain (`tools/perf_after_local.json`).
The engine is now the app's own zero-dependency helper (`rust/`, the
`mlo-audio` binary): one `analyze` call decodes the track once and prints tempo,
key and the mood features as a single JSON line.

This suite pins the MUSICAL answer on inputs whose answer is known by
construction — a click track at a fixed tempo, triads/scales at fixed keys —
proves the engine is deterministic (two runs byte-identical), and, when librosa
is importable, reports how far the two engines drift. Drift is REPORTED, never
failed: the owner accepts that BPM / INITIALKEY / MOOD / ENERGY may move a
little, and this test only fails when the musical answer itself is wrong.

Run:  python tools/test_audio_analysis.py
Exit: 0 all checks passed, 1 a check failed, 2 the engine or ffmpeg is missing.
"""
import array
import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mlo import audiometa, dr  # noqa: E402
from mlo.tools import detect_all_tools  # noqa: E402

SR = 22050
PASSED = 0
SKIPPED = []

# A tempo and four keys whose answer is exact by construction. The tolerances
# are the task's own: the tempo may be off by at most 3 %, the key must match.
TEMPO_BPM = 128.0
TEMPO_TOLERANCE = 0.03
KEYS = [
    ("C major", "C", False),
    ("A minor", "A", True),
    ("F major", "F", False),
    ("E minor", "E", True),
]
# Semitone offsets of the natural/harmonic scale each mode runs through.
MAJOR_SCALE = (0, 2, 4, 5, 7, 9, 11, 12)
MINOR_SCALE = (0, 2, 3, 5, 7, 8, 10, 12)
NOTE_HZ = {
    "C": 261.63, "C#": 277.18, "D": 293.66, "D#": 311.13, "E": 329.63,
    "F": 349.23, "F#": 369.99, "G": 392.00, "G#": 415.30, "A": 440.00,
    "A#": 466.16, "B": 493.88,
}
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def ok(cond, label):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok: {label}")
    else:
        raise AssertionError(label)


def skip(label):
    SKIPPED.append(label)
    print(f"  skip: {label}")


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #
def resolve_engine():
    """The `mlo-audio` binary, building it once when only the source is here.

    The build is deliberately attempted only when `mlo.dr.rust_helper()` finds
    nothing: a checked-out tree with an old binary still runs, and a tree with
    no binary gets one. Failure to build is what exit code 2 reports.
    """
    path = dr.rust_helper()
    if path:
        return path
    cargo = shutil.which("cargo")
    if not cargo:
        return None
    print("  building rust/ (cargo build --release)…")
    try:
        subprocess.run(
            [cargo, "build", "--release", "--manifest-path",
             os.path.join(ROOT, "rust", "Cargo.toml")],
            capture_output=True, text=True, timeout=600)
    except Exception:
        return None
    dr._RUST_PROBED = False           # the probe cached "absent" before the build
    dr._RUST_PATH = None
    return dr.rust_helper()


def ffmpeg_exe():
    """The bundled or system ffmpeg, or None."""
    return (detect_all_tools().get("ffmpeg") or {}).get("ffmpeg_exe")


def run_engine(binary, exe, path, extra=()):
    """(stdout line, parsed dict) from `mlo-audio analyze`."""
    argv = [binary, "analyze", "--ffmpeg", exe, "--input", path, *extra]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=600)
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if proc.returncode != 0 or not lines:
        raise AssertionError(
            f"engine failed on {os.path.basename(path)} "
            f"(rc={proc.returncode}, stderr={proc.stderr.strip()[:200]!r})")
    return lines[-1], json.loads(lines[-1])


# --------------------------------------------------------------------------- #
# Fixtures, pure stdlib
# --------------------------------------------------------------------------- #
def write_wav(path, frames, sr=SR):
    """Mono 16-bit PCM WAV from a float iterable."""
    data = array.array("h", (max(-32767, min(32767, int(v * 32767))) for v in frames))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(data.tobytes())
    return path


def click_track(bpm, seconds=20.0):
    """A decaying noise burst on every beat: a tempo exact by construction."""
    period = 60.0 / bpm
    rng = random.Random(3)
    n = int(SR * seconds)
    beat = int(period * SR)
    out = [0.0] * n
    i = 0
    while i < n:
        for k in range(min(300, n - i)):
            out[i + k] += 0.8 * math.exp(-k / 40.0) * rng.uniform(-1, 1)
        i += beat
    return out


def tonal_track(root, minor, seconds=12.0):
    """A triad over its bass root plus the mode's scale: a key by construction.

    The tonic and the triad dominate, so the chroma has an unambiguous answer
    (any key detector that cannot name a held C-E-G with a C bass is broken).
    """
    idx = NOTE_NAMES.index(root)
    intervals = (0, 3, 7) if minor else (0, 4, 7)
    bass = NOTE_HZ[root] / 2.0
    tones = [bass * 2 ** (k / 12.0) for k in intervals]
    scale = MINOR_SCALE if minor else MAJOR_SCALE
    n = int(SR * seconds)
    for i in range(n):
        t = i / SR
        v = sum(0.28 * math.sin(2 * math.pi * tones[j] * t) * (1.0 - 0.25 * j)
                for j in range(3))
        v += 0.18 * math.sin(2 * math.pi * bass * t)
        step = int(t * 2) % 8
        f = NOTE_HZ[root] * 2 ** (scale[step] / 12.0)
        v += 0.20 * math.sin(2 * math.pi * f * t)
        yield v * 0.6


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def check_tempo(tmp, exe, binary):
    """Every click track's tempo must come back within 3 % of its own BPM."""
    for bpm in (90.0, TEMPO_BPM, 150.0):
        path = write_wav(os.path.join(tmp, f"click{int(bpm)}.wav"),
                         click_track(bpm))
        _raw, data = run_engine(binary, exe, path)
        got = data.get("bpm")
        ok(got is not None and abs(got - bpm) / bpm <= TEMPO_TOLERANCE,
           f"{int(bpm)} BPM click reads {got} (±{TEMPO_TOLERANCE:.0%})")


def check_key(tmp, exe, binary):
    """Every triad/scale must name its own key, tonic and mode both."""
    for label, root, minor in KEYS:
        path = write_wav(os.path.join(tmp, f"key_{root}_{'m' if minor else 'M'}.wav"),
                         tonal_track(root, minor))
        _raw, data = run_engine(binary, exe, path)
        want_idx = NOTE_NAMES.index(root)
        got_idx, got_minor = data.get("key_index"), data.get("key_minor")
        ok(got_idx == want_idx and bool(got_minor) == minor,
           f"{label} reads {data.get('key')!r} (got idx={got_idx}, minor={got_minor})")


def check_determinism(tmp, exe, binary):
    """The engine is deterministic: the same file gives the same JSON line."""
    path = write_wav(os.path.join(tmp, "det.wav"), click_track(TEMPO_BPM))
    first, _ = run_engine(binary, exe, path)
    second, _ = run_engine(binary, exe, path)
    ok(first == second, f"two runs agree byte-for-byte ({first[:80]}…)")


def check_mood_features(tmp, exe, binary):
    """The mood feature dict is the shape `mlo.moods._score` consumes."""
    path = write_wav(os.path.join(tmp, "mood.wav"), tonal_track("C", False))
    _raw, data = run_engine(binary, exe, path)
    feats = data.get("features") or {}
    need = {"duration", "rms_db", "dynamic_range_db", "onset_rate",
            "centroid_hz", "percussive_ratio", "tempo", "key"}
    ok(need <= set(feats),
       f"features carry every key _score/_verdict reads ({sorted(feats)})")
    ok(feats.get("key") in ("major", "minor"), f"feature key is the mode word ({feats.get('key')!r})")
    ok(data.get("mood") in ["happy", "energetic", "aggressive", "sad", "calm",
                            "dreamy", "dark", "party"],
       f"mood is one of mlo.moods.MOODS ({data.get('mood')!r})")


def check_librosa_drift(tmp, exe, binary):
    """Report the two engines' deltas on the same fixtures; never fail on them.

    librosa is the engine that produced every tag until now, so it is the
    reference for the DIRECTION of the drift the owner accepted. The musical
    answer is asserted in check_tempo/check_key; here the deltas are printed
    and, when the engine and the reference disagree on the musical answer, the
    reference loses noisily but the run stays green.
    """
    if audiometa._ensure_librosa() is None:
        skip("librosa not installed: the engine-drift report is skipped")
        return
    note = audiometa._load_signal
    click = write_wav(os.path.join(tmp, "drift-click.wav"), click_track(TEMPO_BPM))
    y, sr = note(click, SR)
    py_bpm = audiometa._detect_bpm(y, sr) if y is not None else None
    _raw, data = run_engine(binary, exe, click)
    print(f"  tempo: rust={data.get('bpm')}  librosa={py_bpm} "
          f"(Δ={_delta(data.get('bpm'), py_bpm)})")
    ok(data.get("bpm") is not None
       and abs(data["bpm"] - TEMPO_BPM) / TEMPO_BPM <= TEMPO_TOLERANCE,
       f"the engine still has the musical answer (librosa={py_bpm})")

    for label, root, minor in KEYS:
        path = write_wav(os.path.join(tmp, f"drift-{root}.wav"),
                         tonal_track(root, minor))
        y, sr = note(path, SR)
        py_key = audiometa._detect_key(y, sr) if y is not None else None
        _raw, data = run_engine(binary, exe, path)
        print(f"  key {label}: rust={(data.get('key_index'), data.get('key_minor'))}  "
              f"librosa={py_key}")
        ok(data.get("key_index") == NOTE_NAMES.index(root)
           and bool(data.get("key_minor")) == minor,
           f"the engine still has the musical answer (librosa={py_key})")


def _delta(a, b):
    if a is None or b is None:
        return "n/a"
    return f"{abs(a - b):+.1f}"


# --------------------------------------------------------------------------- #
def main():
    print("Rust analysis engine (mlo-audio analyze) — tempo, key, mood features")

    exe = ffmpeg_exe()
    if not exe:
        print("  the engine decodes through ffmpeg and there is none here")
        return 2
    binary = resolve_engine()
    if not binary:
        print("  mlo-audio is not built and could not be built "
              "(cargo missing or the build failed)")
        return 2
    print(f"  engine: {binary}")

    tmp = tempfile.mkdtemp(prefix="mlo_analysis_test_")
    checks = [
        ("tempo", lambda: check_tempo(tmp, exe, binary)),
        ("key", lambda: check_key(tmp, exe, binary)),
        ("features", lambda: check_mood_features(tmp, exe, binary)),
        ("determinism", lambda: check_determinism(tmp, exe, binary)),
        ("librosa drift", lambda: check_librosa_drift(tmp, exe, binary)),
    ]
    bad = 0
    for label, fn in checks:
        print(f"\n{label}")
        try:
            fn()
        except AssertionError as e:
            bad += 1
            print(f"  FAIL {e}")
        except Exception as e:                 # a broken fixture is not a pass
            bad += 1
            import traceback
            print(f"  ERROR {type(e).__name__}: {e}")
            traceback.print_exc()
    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{PASSED} check(s) passed, {bad} failed"
          + (f", {len(SKIPPED)} skipped" if SKIPPED else ""))
    for s in SKIPPED:
        print(f"  skipped: {s}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())