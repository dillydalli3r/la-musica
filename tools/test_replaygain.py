#!/usr/bin/env python3
"""ReplayGain measurement contract (mlo.loudness) — what script 7 uses.

Script 7 (Calculate DR & ReplayGain) tags a library by shelling out to rsgain;
the parts of mlo.loudness it leans on are pinned here:

  * parse_ebur128 reads ffmpeg's ebur128/astats log into integrated loudness
    (LUFS) and a LINEAR peak — the unit the REPLAYGAIN_*_PEAK tags are written
    in. The peak is the SAMPLE peak, not the true peak: rsgain writes sample
    peaks, so a true peak here disagreed with the file's own tag by up to 30%.
    The fixture is a 45°-phase sine at fs/4, whose samples all land on ±0.7071
    of its amplitude: sample peak 11585/32768 = 0.3535, true peak 0.5.
  * analyze_file answers ONE file: its four tags when they are all there (no
    decode), else a fresh ffmpeg measurement, else None. Never raises; a file
    with nothing to measure — silent, undecodable — answers None, not a number.
    The gain rsgain itself reports is the oracle for the rest, when rsgain is
    installed.

Run:  python tools/test_replaygain.py
"""
import array
import os
import shutil
import subprocess
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo import loudness
from mlo.tools import detect_all_tools

# ----------------------------------------------------------------------
# A real ffmpeg 7 ebur128 log (1 kHz lavfi sine, 3 s), verbatim: progress
# lines each carry their own "I:"/"TPK:" fields that must NOT be read as the
# measurement — only the Summary block is.
# ----------------------------------------------------------------------
EBUR128_LOG = """\
[Parsed_ebur128_0 @ 000001fb6e8e8c00] t: 1.699977   TARGET:-23 LUFS    M: -21.1 S:-120.7     I: -21.1 LUFS       LRA:   0.0 LU  FTPK: -18.1 dBFS  TPK: -18.1 dBFS
[Parsed_ebur128_0 @ 000001fb6e8e8c00] t: 2.999977   TARGET:-23 LUFS    M: -21.1 S: -21.1     I: -21.1 LUFS       LRA:  20.0 LU  FTPK: -18.1 dBFS  TPK: -18.1 dBFS
[Parsed_ebur128_0 @ 000001fb6e8e8c00] Summary:

  Integrated loudness:
    I:         -21.1 LUFS
    Threshold: -31.1 LUFS

  Loudness range:
    LRA:        20.0 LU
    Threshold: -41.1 LUFS
    LRA low:   -41.1 LUFS
    LRA high:  -21.1 LUFS

  True peak:
    Peak:      -18.1 dBFS
[out#0/null @ 000001fb6e865b00] video:0KiB audio:258KiB subtitle:0KiB other streams:0KiB global headers:0KiB muxing overhead: unknown
size=N/A time=00:00:03.00 bitrate=N/A speed= 308x elapsed=0:00:00.00
"""

parsed = loudness.parse_ebur128(EBUR128_LOG)
assert parsed is not None, parsed
assert parsed["lufs"] == -21.1, parsed
# The peak comes back LINEAR (the unit of the REPLAYGAIN_*_PEAK tags):
# 10 ** (-18.1 / 20) = 0.1245
assert abs(parsed["peak"] - 10 ** (-18.1 / 20)) < 1e-12, parsed
assert round(parsed["peak"], 4) == 0.1245, parsed

# The astats lines that ride along with the ebur128 summary carry the same
# peak to 1e-6 dB where the summary rounds it to 0.1 dBFS, so they win. The
# value is 20*log10(11585/32768) = -9.031078, the real peak of a 16-bit file
# whose loudest sample is 11585 — a 45°-phase sine at fs/4, 0.70710678 of its
# amplitude, so the tag that peak belongs in reads 0.353546.
ASTATS_TAIL = """
[Parsed_astats_1 @ 000002143c8942c0] Channel: 1
[Parsed_astats_1 @ 000002143c8942c0] Peak level dB: -9.031078
[Parsed_astats_1 @ 000002143c8942c0] Channel: 2
[Parsed_astats_1 @ 000002143c8942c0] Peak level dB: -12.000000
[Parsed_astats_1 @ 000002143c8942c0] Overall
[Parsed_astats_1 @ 000002143c8942c0] Peak level dB: -9.031078
"""
exact = loudness.parse_ebur128(EBUR128_LOG + ASTATS_TAIL)
assert exact is not None, exact
assert abs(exact["peak"] - 10 ** (-9.031078 / 20)) < 1e-12, exact
assert abs(exact["peak"] - 11585 / 32768) < 1e-6, exact
assert exact["lufs"] == -21.1, exact
# ...and a silent channel's "-inf" is not a measurement, so the ebur128
# summary line is the fallback rather than a peak of 0.
assert abs(loudness.parse_ebur128(
    EBUR128_LOG + "\n[Parsed_astats_1 @ 0] Peak level dB: -inf\n")["peak"]
    - 10 ** (-18.1 / 20)) < 1e-12

# A log with progress lines only (decoder died) is not a measurement, and a
# negative/zero peak must not be clamped away by the parser.
assert loudness.parse_ebur128(EBUR128_LOG.split("Summary:")[0]) is None
assert loudness.parse_ebur128("") is None
assert loudness.parse_ebur128("ffmpeg version 7.0") is None
assert loudness.RG2_REFERENCE_LUFS == -18.0


# ----------------------------------------------------------------------
# Tag path: a file whose four tags are already there is never decoded.
# ----------------------------------------------------------------------
class _FakeAudioFile:
    """Stands in for mlo.audio.AudioFile (no real tagged file needed)."""
    tags = {}

    def __init__(self, path):
        pass

    def get_tag(self, name):
        return self.tags.get(name)


_real_af = loudness.AudioFile
_real_ffmpeg = dict(loudness._FFMPEG_CACHE)
loudness.AudioFile = _FakeAudioFile
# No decoder during the tag-path checks: anything that tries to measure
# returns None, so a wrong branch is visible instead of merely slow.
loudness._FFMPEG_CACHE.update({"exe": None, "checked": True})
try:
    _FakeAudioFile.tags = {
        "REPLAYGAIN_TRACK_GAIN": "-3.21 dB",
        "REPLAYGAIN_TRACK_PEAK": "0.987",
        "REPLAYGAIN_ALBUM_GAIN": "-2.50 dB",
        "REPLAYGAIN_ALBUM_PEAK": "1.002",
    }
    data = loudness.analyze_file("some/track.flac")
    assert data["gain_db"] == -3.21, data
    assert data["peak"] == 0.987, data
    assert data["album_gain_db"] == -2.50, data
    assert data["album_peak_db"] == 1.002, data
    assert data["analyzed"] is False and data["source"] == "tags", data
    assert data["lufs"] is None, data

    # One tag missing -> nothing usable in the tag set -> measure (None
    # here because the decoder is stubbed out), never a half-tag answer.
    _FakeAudioFile.tags = {"REPLAYGAIN_TRACK_GAIN": "-3.21 dB"}
    assert loudness.analyze_file("some/track.flac") is None

    # A file with no tags at all and no decoder has nothing to answer: the
    # caller gets None, never a fabricated unity gain.
    _FakeAudioFile.tags = {}
    assert loudness.analyze_file("some/track.flac") is None
finally:
    loudness.AudioFile = _real_af
    loudness._FFMPEG_CACHE.update(_real_ffmpeg)


# ----------------------------------------------------------------------
# Real decoder (optional): the peak metric, the files with no measurement,
# and — the reference — the gain and peak rsgain reports for the same file.
# Skipped when ffmpeg is not installed; nothing here needs the network.
# ----------------------------------------------------------------------
ffmpeg = loudness._ffmpeg_exe()
if not ffmpeg:
    print("note: ffmpeg not installed — live ebur128 measurement skipped")
else:
    root = tempfile.mkdtemp(prefix="mlo_rg_ff_")
    try:
        tone = os.path.join(root, "tone.flac")
        subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi",
                        "-i", "sine=frequency=1000:duration=3", tone],
                       check=True)
        live = loudness.analyze_file(tone)
        assert live is not None, live
        assert live["source"] == "ffmpeg" and live["analyzed"] is True, live
        assert -40.0 < live["lufs"] < 0.0, live
        assert live["gain_db"] == loudness.RG2_REFERENCE_LUFS - live["lufs"], live
        assert 0.0 < live["peak"] <= 1.0, live

        # An undecodable file returns None instead of raising.
        bad = os.path.join(root, "broken.flac")
        with open(bad, "wb") as f:
            f.write(b"not audio" * 100)
        assert loudness.analyze_file(bad) is None

        # A silent file has nothing to measure, so it gets NO number: the
        # ffmpeg summary is "-inf" LUFS and "-inf" peak, and neither is a
        # gain. (A 0.0 dB gain would play silence at unity, which is right
        # only by accident and wrong the moment the file is not silent.)
        silent = os.path.join(root, "silent.wav")
        with wave.open(silent, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(b"\x00\x00" * (44100 * 3))
        assert loudness.analyze_file(silent) is None, loudness.analyze_file(silent)

        # The peak metric. A 45°-phase sine at fs/4 is [S, S, -S, -S] at
        # 44.1 kHz: every sample sits at 0.7071 of the sine's amplitude, so
        # the SAMPLE peak is S/32768 = 0.35355 and the TRUE peak is 0.5.
        # Measuring 0.5 here means peak=true came back (the bug), 0.35355
        # means the sample peak — the number in the REPLAYGAIN_*_PEAK tag.
        S = 11585
        inter_sample = os.path.join(root, "inter-sample.wav")
        samples = array.array("h", [S, S, -S, -S] * (44100 * 3 // 4))
        if sys.byteorder != "little":
            samples.byteswap()
        with wave.open(inter_sample, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(samples.tobytes())
        peak = loudness.analyze_file(inter_sample)
        assert peak is not None, peak
        assert abs(peak["peak"] - S / 32768) <= 1e-4, peak
        assert peak["peak"] < 0.45, (
            "the true peak of this fixture is 0.5, so anything near it is the "
            "wrong metric: %r" % (peak,))

        # The oracle: rsgain scans the same file (-s s writes nothing) and
        # prints the gain it would tag it with and the sample peak it would
        # store. Both have to match.
        rsgain = (detect_all_tools().get("rsgain") or {}).get("rsgain_exe")
        if not rsgain:
            print("note: rsgain not installed — gain/peak cross-check skipped")
        else:
            scan = subprocess.run([rsgain, "custom", "-s", "s", "-O", "-q",
                                   inter_sample],
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
            lines = [l for l in (scan.stdout or "").splitlines() if l.strip()]
            assert len(lines) == 2, (scan.returncode, scan.stdout, scan.stderr)
            _name, rs_lufs, rs_gain, rs_peak = lines[1].split("\t")[:4]
            assert abs(peak["gain_db"] - float(rs_gain)) <= 0.1, (peak, rs_gain)
            assert abs(peak["peak"] - float(rs_peak)) <= 1e-4, (peak, rs_peak)
            assert float(rs_gain) == loudness.RG2_REFERENCE_LUFS - float(rs_lufs), lines[1]
    finally:
        shutil.rmtree(root, ignore_errors=True)

print("ok")
