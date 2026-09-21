#!/usr/bin/env python3
"""Dynamic range (script 7) measured in process: the loudness-war formulas.

Script 7 used to run a downloaded copy of simple-dr-meter through whatever
Python interpreter it found and parse that tool's ``dr.txt`` log. The
measurement is a page of arithmetic (mlo/dr.py), so it runs here instead — and
this suite pins the arithmetic where it can be checked by hand, then against
real decodes, then through the album loop that writes the tags.

What is pinned:

  * the block math from a hand-computed block set — the second-highest peak,
    the loudest-20% RMS rule and the rounding — so a change in any of those
    shows up as a wrong number, not as a run that merely "still works";
  * the rules that must never produce a value: fewer than two blocks, and a
    silent track (no DR at all — 0 would drag every album average down);
  * a 60-second synthetic stereo file whose DR is fixed BY CONSTRUCTION
    (peak 0.5 against an RMS of 0.3 measures 20*log10(0.5/0.3) = 4.44 dB), in
    stereo, in mono and at 96 kHz, which only comes out right if the decode
    resamples to the 44.1 kHz the meter measures at;
  * an album run through run_calc_dr_replaygain() that writes DYNAMIC RANGE per
    track and ALBUM DYNAMIC RANGE per album, skips the tracks that have no DR,
    and skips the whole album on a second run (skip-existing).

The fixtures are WAV files synthesized here with numpy (one 1 ms 1 kHz burst
per block sets the peak, a sustained sine sets the block RMS), FLAC-encoded by
the bundled ffmpeg, so the expected number is arithmetic rather than a
recording of today's behaviour. The album fixture is three tracks — two
measurable, one silent — plus a second album, so the album loop runs both its
threaded and its sequential branch; ReplayGain writes are off in that config so
rsgain is never involved and the run stays offline. The user's library is never
touched, and nothing here reaches the network.

Run:  python tools/test_dynamic_range.py
"""
import math
import os
import shutil
import subprocess
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mlo import dr  # noqa: E402
from mlo.audio import AudioFile  # noqa: E402
from mlo.config import DEFAULT_CONFIG  # noqa: E402
from mlo.loudness import run_calc_dr_replaygain  # noqa: E402
from mlo.paths import library_root  # noqa: E402
from mlo.tools import detect_all_tools  # noqa: E402

passed = 0
skipped = []

BLOCK_SECONDS = 3
RATE = 44100
BLOCKS = 20                      # 20 three-second blocks = 60 seconds
LOUD, QUIET, BURST_AMP = 0.3, 0.1, 0.5
# Every block carries a 1 ms burst at 0.5 (the peak) over a sustained sine whose
# amplitude is the block's RMS — 0.3 in the four loudest blocks, 0.1 elsewhere.
# So the second-highest peak is 0.5, the loudest-20% RMS (4 of 20 blocks) is
# 0.3, and the DR is fixed by construction.
EXPECTED_DR = 20 * math.log10(BURST_AMP / LOUD)          # 4.437 dB -> 4


def ok(cond, label):
    global passed
    assert cond, f"FAILED: {label}"
    passed += 1
    print(f"  ok: {label}")


def skip(label):
    skipped.append(label)
    print(f"  skip: {label}")


def ffmpeg_exe():
    """The bundled or system ffmpeg, or None."""
    return (detect_all_tools().get("ffmpeg") or {}).get("ffmpeg_exe")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def make_wav(path, channels=2, rate=RATE, loud=LOUD, silent=False,
             blocks=BLOCKS):
    """A WAV whose DR is fixed by construction (silence when *silent*)."""
    import numpy as np

    per_block = int(BLOCK_SECONDS * rate)
    total = blocks * per_block if not silent else 4 * per_block
    t = np.arange(total) / rate
    if silent:
        samples = np.zeros(total)
    else:
        amp = np.where((np.arange(total) // per_block) < 4, loud, QUIET)
        samples = amp * np.sin(2 * np.pi * 1000 * t)
        burst = np.arange(max(1, int(0.001 * rate))) / rate
        wave_part = BURST_AMP * np.sin(2 * np.pi * 1000 * burst)
        for start in range(0, total, per_block):
            samples[start:start + len(wave_part)] = wave_part
    scaled = np.round(samples * 32767).astype("<i2")
    frames = (np.repeat(scaled[:, None], channels, axis=1).tobytes()
              if channels > 1 else scaled.tobytes())
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return path


def to_flac(exe, src, dst):
    """Re-encode a fixture as FLAC (lossless, so the DR survives untouched)."""
    subprocess.run([exe, "-v", "error", "-y", "-i", src, "-ac", "2",
                    "-c:a", "flac", dst], check=True, capture_output=True)
    return dst


# --------------------------------------------------------------------------- #
# The arithmetic, from a block set computed by hand
# --------------------------------------------------------------------------- #
def check_block_math(tmp):
    import numpy as np

    # 10 blocks: the second-highest peak is 0.7, the two loudest blocks (20% of
    # 10) have RMS 0.6 and 0.5, so DR = 20*log10(0.7 / sqrt((0.6^2+0.5^2)/2)).
    peaks = np.array([[0.8], [0.7], [0.6], [0.5], [0.4],
                      [0.3], [0.2], [0.1], [0.05], [0.01]], dtype="<f4")
    rms = np.array([[0.6], [0.5], [0.45], [0.4], [0.35],
                    [0.3], [0.25], [0.2], [0.15], [0.1]], dtype="<f4")
    wanted = 20 * math.log10(0.7 / math.sqrt((0.6 ** 2 + 0.5 ** 2) / 2))
    got, reason = dr.value_from_blocks(peaks, rms)
    ok(got == int(round(wanted)),
       f"the block math follows the meter (peak 0.8/0.7, top-20% RMS 0.6/0.5 "
       f"-> DR {wanted:.2f} -> {got}, reason {reason!r})")

    # The 20% is of the block count and never rounds down to nothing: a 4-block
    # track has floor(0.8) = 0 loud blocks by percentile alone, and the meter
    # keeps one anyway, so this is DR 6.02 rather than no value.
    four_peaks = np.ones((4, 1), dtype="<f4") * 0.5
    four_rms = np.array([[0.25], [0.2], [0.15], [0.1]], dtype="<f4")
    wanted4 = 20 * math.log10(0.5 / 0.25)
    got4, reason4 = dr.value_from_blocks(four_peaks, four_rms)
    ok(got4 == int(round(wanted4)),
       f"4 blocks take their top 20% as ONE block (max(1, floor(0.8))), so DR "
       f"is {got4} (hand value {wanted4:.2f}, reason {reason4!r})")

    # One block is not a measurement, and neither is a silent track: both are
    # "no DR", which is not the same as DR 0.
    one, why = dr.value_from_blocks(four_peaks[:1], four_rms[:1])
    ok(one is None, f"a single block has no DR (got {one!r}, {why!r})")

    silent_peaks = np.zeros((6, 2), dtype="<f4")
    silent_rms = np.zeros((6, 2), dtype="<f4")
    quiet, why = dr.value_from_blocks(silent_peaks, silent_rms)
    ok(quiet is None and "silent" in why,
       f"a silent track has NO DR, not DR 0 (got {quiet!r}, reason {why!r})")


def check_album_values():
    cases = [
        ([], None, "no tracks at all"),
        ([None, None], None, "every track silent/short"),
        ([3], 3, "one track"),
        ([10, 10, 11, 12], 11, "mean 10.75"),
        ([4, 5], 4, "half rounds to even, as the meter's numpy.round does"),
        ([4, None, 5], 4, "the empty track contributes nothing, it is not a 0"),
    ]
    for values, wanted, label in cases:
        got = dr.album_dr(values)
        ok(got == wanted, f"album_dr({values}) == {wanted} ({label})")


# --------------------------------------------------------------------------- #
# Real decodes of files whose DR is fixed by construction
# --------------------------------------------------------------------------- #
def check_built_files(tmp, exe):
    stereo = make_wav(os.path.join(tmp, "stereo.wav"))
    got = dr.measure_track(stereo, exe)
    ok(got is not None and abs(got - EXPECTED_DR) <= 1,
       f"a 60 s synthetic stereo file measures DR {got}, fixed by construction "
       f"at {EXPECTED_DR:.2f} (peak {BURST_AMP} vs loud-block RMS {LOUD})")

    mono = make_wav(os.path.join(tmp, "mono.wav"), channels=1)
    got = dr.measure_track(mono, exe)
    ok(got is not None and abs(got - EXPECTED_DR) <= 1,
       f"the same material in MONO measures DR {got} (one channel, no "
       f"de-interleave guesswork)")

    hi = make_wav(os.path.join(tmp, "hi.wav"), rate=96000)
    got = dr.measure_track(hi, exe)
    ok(got is not None and abs(got - EXPECTED_DR) <= 1,
       f"the same material at 96 kHz measures DR {got}: the decode resampled "
       f"to the meter's 44.1 kHz (the blocks are 3 s of THAT rate)")

    silent = make_wav(os.path.join(tmp, "silent.wav"), silent=True)
    result = dr.measure_track_detailed(silent, exe)
    ok(result.dr is None and not result.failed and "silent" in result.reason,
       f"a silent file has no DR and is not a failure (got {result.dr!r}, "
       f"{result.reason!r})")

    short = make_wav(os.path.join(tmp, "short.wav"), blocks=1)   # 3 s = 1 block
    result = dr.measure_track_detailed(short, exe)
    ok(result.dr is None and not result.failed and "shorter" in result.reason,
       f"a 3 s file is one block, so no DR (got {result.dr!r}, "
       f"{result.reason!r})")

    broken = os.path.join(tmp, "broken.flac")
    with open(broken, "wb") as fh:
        fh.write(b"this is not a flac stream" * 64)
    result = dr.measure_track_detailed(broken, exe)
    ok(result.dr is None and result.failed and result.reason,
       f"a file that cannot be decoded reports WHY instead of dropping out "
       f"silently ({result.reason!r})")


# --------------------------------------------------------------------------- #
# The album loop: DYNAMIC RANGE / ALBUM DYNAMIC RANGE through script 7
# --------------------------------------------------------------------------- #
def _album_fixture(tmp, exe, lib, name, specs):
    """A folder of FLAC tracks built from the analytic WAVs in *specs*."""
    album = os.path.join(library_root(lib), "A", name)
    os.makedirs(album)
    raw = os.path.join(tmp, "raw")
    os.makedirs(raw, exist_ok=True)
    paths = []
    for index, (label, kwargs) in enumerate(specs, 1):
        src = make_wav(os.path.join(raw, f"{name}-{index}.wav"), **kwargs)
        paths.append(to_flac(exe, src,
                             os.path.join(album, f"{index:02d} - {label}.flac")))
    return album, paths


def _cfg(lib, **over):
    config = dict(DEFAULT_CONFIG)
    config.update({
        "music_folder": lib,
        "targets": None,
        "dr_replaygain_enabled": True,
        "write_dynamic_range_tags": True,
        "write_replaygain_tags": False,     # only the DR half runs here
        "force_dr_replaygain": False,
        "replaygain_skip_existing": True,
    }, **over)
    return config


def _dr_tags(paths):
    return [(str(AudioFile(p).get_tag("DYNAMIC RANGE") or ""),
             str(AudioFile(p).get_tag("ALBUM DYNAMIC RANGE") or ""))
            for p in paths]


def check_album_loop(tmp, exe):
    lib = os.path.join(tmp, "library")
    _album_a, a_paths = _album_fixture(tmp, exe, lib, "Album A", [
        ("Loud", {}),                                   # construction: DR 4.44
        ("Quieter", {"loud": 0.2812}),                  # construction: DR 5.00
        ("Silent", {"silent": True}),
    ])
    _album_b, b_paths = _album_fixture(tmp, exe, lib, "Album B", [
        ("Loud", {}),
        ("Quieter", {"loud": 0.2812}),
    ])
    # worker_limit 4 with two albums is the threaded branch of the album loop;
    # 1 is the sequential one, and a single target is the single-album one.
    config = _cfg(lib, worker_limit=4)

    stats = run_calc_dr_replaygain(config)
    dr_a = _dr_tags(a_paths)
    dr_b = _dr_tags(b_paths)

    ok(abs(int(dr_a[0][0]) - EXPECTED_DR) <= 1 and abs(int(dr_b[0][0]) - EXPECTED_DR) <= 1,
       f"both albums measured their loud track at DR{dr_a[0][0]}/DR{dr_b[0][0]} "
       f"with 4 workers per album (construction {EXPECTED_DR:.2f})")
    ok(dr_a[1][0] and dr_a[1][0] != dr_a[0][0],
       f"the quieter track is its own measurement, DR{dr_a[1][0]} "
       f"(not DR{dr_a[0][0]} of the track above it)")
    ok(dr_a[2][0] == "" and dr_a[2][1] == "",
       "the silent track gets NO tags at all — nothing is invented for it")
    for album_dr in {dr_a[0][1], dr_a[1][1], dr_b[0][1], dr_b[1][1]}:
        want = int(round((int(dr_a[0][0]) + int(dr_a[1][0])) / 2))
        ok(album_dr == str(want),
           f"ALBUM DYNAMIC RANGE {album_dr} is the mean of the measured tracks "
           f"({dr_a[0][0]} + {dr_a[1][0]})/2 -> {want}")
    ok(stats["modified_count"] == 4 and stats["error_count"] == 0,
       f"the run reports 4 modified files and no errors "
       f"(modified={stats['modified_count']}, errors={stats['error_count']}, "
       f"albums={stats['total_scanned']})")

    again = run_calc_dr_replaygain(config)
    ok(again["skipped_count"] == 2 and again["error_count"] == 0,
       f"a second run skips both albums whose tags are already there "
       f"(skipped={again['skipped_count']}, errors={again['error_count']})")

    once = run_calc_dr_replaygain(
        _cfg(lib, worker_limit=1, force_dr_replaygain=True,
             targets=[os.path.dirname(a_paths[0])]))
    ok(once["error_count"] == 0 and _dr_tags(a_paths) == dr_a,
       f"one targeted album through the sequential branch re-measures without "
       f"error and leaves the same values (errors={once['error_count']})")


def check_undecodable_track(tmp, exe):
    """One unreadable file must be REPORTED, not dropped in silence."""
    lib = os.path.join(tmp, "brokenlib")
    album, paths = _album_fixture(tmp, exe, lib, "Album", [("Loud", {})])
    broken = os.path.join(album, "02 - Broken.flac")
    with open(broken, "wb") as fh:
        fh.write(b"this is not a flac stream" * 128)

    stats = run_calc_dr_replaygain(_cfg(lib, worker_limit=1, targets=[album]))
    errors = dict(stats["errors"])
    tags = _dr_tags(paths)
    ok(stats["error_count"] == 1 and "02 - Broken.flac" in errors,
       f"the unreadable track is reported by NAME and counted "
       f"(errors={errors})")
    ok(tags[0][0] and abs(int(tags[0][0]) - EXPECTED_DR) <= 1,
       f"the album's other track is still measured, DR{tags[0][0]} "
       f"(one bad file neither sinks the album nor hides itself)")


def check_without_numpy(tmp, exe):
    """A build with no numpy must SAY so, not come back with silent skips."""
    lib = os.path.join(tmp, "nolib")
    album, paths = _album_fixture(tmp, exe, lib, "Album", [("Loud", {})])
    config = _cfg(lib, worker_limit=1)

    real_np = dr.np
    dr.np = None
    try:
        stats = run_calc_dr_replaygain(config)
        reason = dict(stats["errors"]).get("dynamic range", "")
        tagged = str(AudioFile(paths[0]).get_tag("DYNAMIC RANGE") or "")
    finally:
        dr.np = real_np

    ok(reason == dr.NUMPY_REASON and stats["error_count"] >= 1,
       f"without numpy the run reports {reason!r} as the reason and counts an "
       f"error ({stats['error_count']})")
    ok(tagged == "",
       f"and writes no DYNAMIC RANGE tag rather than a value it cannot "
       f"measure (got {tagged!r})")
    ok(dr.measure_track(paths[0], exe) is not None,
       "with numpy back, the same file measures normally (the check above "
       "tested the missing dependency, not a broken fixture)")


def main():
    print("Dynamic range (in-process) — formulas, edge cases and the album loop")

    if not dr.have_numpy():
        print("  skip: numpy is not importable, so the meter cannot run here")
        print("\n0 check(s) passed, 0 failed, 1 skipped")
        return 0

    exe = ffmpeg_exe()
    tmp = tempfile.mkdtemp(prefix="mlo_dr_test_")
    checks = [
        ("block math", lambda: check_block_math(tmp)),
        ("album values", check_album_values),
    ]
    if exe:
        checks.append(("built files", lambda: check_built_files(tmp, exe)))
        checks.append(("without numpy", lambda: check_without_numpy(tmp, exe)))
        checks.append(("unreadable track", lambda: check_undecodable_track(tmp, exe)))
        checks.append(("album loop", lambda: check_album_loop(tmp, exe)))
    else:
        skip("no ffmpeg: the decode-backed checks need it")

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

    print(f"\n{passed} check(s) passed, {bad} failed"
          + (f", {len(skipped)} skipped" if skipped else ""))
    for s in skipped:
        print(f"  skipped: {s}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
