#!/usr/bin/env python3
"""On-demand per-file ReplayGain contract (mlo.loudness).

The player asks for one track's gain while a batch run may be tagging a
whole folder — so the on-demand path must (a) trust the four tags when they
are all there, (b) measure with ffmpeg's EBU R128 filter when they are not,
(c) never raise, and (d) cache a measurement so replaying a track costs one
stat(). The batch shell-out paths (rsgain / simple-dr-meter) are untouched
and untested here: they need the tools installed.

Run:  python tools/test_replaygain.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo import loudness
from mlo.config import DEFAULT_CONFIG

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
# True peak comes back LINEAR (the unit of the REPLAYGAIN_*_PEAK tags):
# 10 ** (-18.1 / 20) = 0.1245
assert abs(parsed["peak"] - 10 ** (-18.1 / 20)) < 1e-12, parsed
assert round(parsed["peak"], 4) == 0.1245, parsed

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

    # ---- replaygain_for_path: mode / preamp / album / clip ---------------
    _FakeAudioFile.tags = {
        "REPLAYGAIN_TRACK_GAIN": "-3.21 dB",
        "REPLAYGAIN_TRACK_PEAK": "0.987",
        "REPLAYGAIN_ALBUM_GAIN": "-2.50 dB",
        "REPLAYGAIN_ALBUM_PEAK": "1.002",
    }
    cfg = dict(DEFAULT_CONFIG)
    cfg["music_folder"] = tempfile.gettempdir()
    cfg["replaygain_analyze_missing"] = False  # no measuring in these cases

    off = loudness.replaygain_for_path(cfg, "t.flac", mode="off")
    assert off == {"gain": None, "peak": None, "mode": "off",
                   "source": None, "analyzed": False}, off

    track = loudness.replaygain_for_path(cfg, "t.flac", clip_protection=False)
    assert track["gain"] == -3.21 and track["mode"] == "track", track
    assert track["source"] == "tags" and track["analyzed"] is False, track

    album = loudness.replaygain_for_path(cfg, "t.flac", mode="album",
                                         clip_protection=False)
    assert album["gain"] == -2.50, album       # album gain wins in album mode
    assert album["peak"] == 1.002, album

    pre = loudness.replaygain_for_path(cfg, "t.flac", preamp_db=3.0,
                                       clip_protection=False)
    assert abs(pre["gain"] - (-0.21)) < 1e-9, pre

    # Album mode falls back to the track value when the album tags are gone.
    _FakeAudioFile.tags = {"REPLAYGAIN_TRACK_GAIN": "-3.21 dB",
                           "REPLAYGAIN_TRACK_PEAK": "0.5"}
    fallback = loudness.replaygain_for_path(cfg, "t.flac", mode="album",
                                            clip_protection=False)
    assert fallback["gain"] == -3.21 and fallback["peak"] == 0.5, fallback

    # Clip protection clamps to the ceiling for the peak: 0 dBFS is the
    # loudest a peak of 0.5 can be scaled to, i.e. -20*log10(0.5) = +6.02 dB
    # of headroom. A +6 dB gain is still under it, so it is left alone...
    _FakeAudioFile.tags = {"REPLAYGAIN_TRACK_GAIN": "6.00 dB",
                           "REPLAYGAIN_TRACK_PEAK": "0.5"}
    under = loudness.replaygain_for_path(cfg, "t.flac", preamp_db=0.0)
    assert abs(under["gain"] - 6.0) < 1e-9, under
    assert under["source"] == "tags", under

    # ...while a hot master that IS above the ceiling gets cut to it, and the
    # returned source says so. (Peak 2.0: -20*log10(2.0) = -6.0206 dB.)
    _FakeAudioFile.tags = {"REPLAYGAIN_TRACK_GAIN": "6.00 dB",
                           "REPLAYGAIN_TRACK_PEAK": "2.0"}
    clamped = loudness.replaygain_for_path(cfg, "t.flac", preamp_db=0.0)
    assert abs(clamped["gain"] - (-20 * __import__("math").log10(2.0))) < 1e-9, clamped
    assert abs(clamped["gain"] - (-6.0206)) < 1e-4, clamped
    assert clamped["source"] == "tags+clamp", clamped

    # Clip protection off leaves the same hot gain alone.
    hot = loudness.replaygain_for_path(cfg, "t.flac", clip_protection=False)
    assert hot["gain"] == 6.0 and hot["source"] == "tags", hot

    # A tagless file with analyze_missing off has nothing to report.
    _FakeAudioFile.tags = {}
    assert loudness.replaygain_for_path(cfg, "t.flac")["gain"] is None
finally:
    loudness.AudioFile = _real_af
    loudness._FFMPEG_CACHE.update(_real_ffmpeg)


# ----------------------------------------------------------------------
# Cache: <music>/.mlo/data/replaygain.json, invalidated by size/mtime.
# ----------------------------------------------------------------------
root = tempfile.mkdtemp(prefix="mlo_rg_")
try:
    cache_cfg = dict(DEFAULT_CONFIG)
    cache_cfg["music_folder"] = root
    cache_file = os.path.join(root, ".mlo", "data", "replaygain.json")
    track_path = os.path.join(root, "Artist", "Album", "01 track.flac")
    os.makedirs(os.path.dirname(track_path))
    with open(track_path, "wb") as f:
        f.write(b"x" * 32)

    assert loudness.cached_analysis(cache_cfg, track_path) is None
    loudness.store_analysis(cache_cfg, track_path, {
        "gain_db": 1.5, "peak": 0.75, "lufs": -19.5,
        "analyzed": True, "source": "ffmpeg",
        "album_gain_db": None, "album_peak_db": None,
    })
    assert os.path.isfile(cache_file), cache_file
    entry = loudness.cached_analysis(cache_cfg, track_path)
    assert entry["gain_db"] == 1.5 and entry["peak"] == 0.75, entry
    assert entry["lufs"] == -19.5 and entry["source"] == "ffmpeg", entry
    assert entry["analyzed"] is True, entry
    assert entry["size"] == 32 and entry["mtime"] == os.stat(track_path).st_mtime, entry
    assert entry["ts"] > 0, entry
    assert "album_gain_db" not in entry, entry   # stored shape is the spec's

    # Rewritten file (size changed) is a different file: re-measure.
    with open(track_path, "ab") as f:
        f.write(b"yy")
    assert loudness.cached_analysis(cache_cfg, track_path) is None

    # Same bytes, newer mtime (re-tag, re-download) also invalidates.
    loudness.store_analysis(cache_cfg, track_path, {
        "gain_db": -2.0, "peak": 0.9, "lufs": -16.0,
        "analyzed": True, "source": "ffmpeg"})
    entry = loudness.cached_analysis(cache_cfg, track_path)
    assert entry["gain_db"] == -2.0, entry
    later = entry["mtime"] + 10
    os.utime(track_path, (later, later))
    assert loudness.cached_analysis(cache_cfg, track_path) is None

    # A corrupt cache file reads as empty and the next store starts fresh.
    with open(cache_file, "w", encoding="utf-8") as f:
        f.write('{"Artist\\\\Album": {"gain_db"')
    assert loudness.cached_analysis(cache_cfg, track_path) is None
    loudness.store_analysis(cache_cfg, track_path, {
        "gain_db": 0.25, "peak": 1.0, "lufs": -18.25,
        "analyzed": True, "source": "ffmpeg"})
    entry = loudness.cached_analysis(cache_cfg, track_path)
    assert entry["gain_db"] == 0.25, entry
    assert len(list(os.listdir(os.path.dirname(cache_file)))) == 1, \
        os.listdir(os.path.dirname(cache_file))   # no temp litter

    # A path that does not exist is simply not cached (never raises).
    loudness.store_analysis(cache_cfg, os.path.join(root, "gone.flac"), entry)
    assert loudness.cached_analysis(cache_cfg, os.path.join(root, "gone.flac")) is None

    # ---- analyze_missing wiring: measure once, then serve from the cache --
    # (A fresh file: the one above still has a valid cache entry.)
    track2 = os.path.join(root, "Artist", "Album", "02 track.flac")
    with open(track2, "wb") as f:
        f.write(b"z" * 16)
    calls = []
    _real_analyze = loudness.analyze_file
    loudness.analyze_file = lambda p, cfg=None, force=False: (
        calls.append(p) or {"gain_db": 4.0, "peak": 0.5, "lufs": -22.0,
                            "album_gain_db": None, "album_peak_db": None,
                            "analyzed": True, "source": "ffmpeg"})
    try:
        cfg2 = dict(DEFAULT_CONFIG)
        cfg2["music_folder"] = root
        first = loudness.replaygain_for_path(cfg2, track2,
                                             mode="track", preamp_db=0.0,
                                             clip_protection=False)
        assert first == {"gain": 4.0, "peak": 0.5, "mode": "track",
                         "source": "ffmpeg", "analyzed": True}, first
        second = loudness.replaygain_for_path(cfg2, track2,
                                              mode="track", preamp_db=0.0,
                                              clip_protection=False)
        assert second == first, second
        assert calls == [track2], calls   # second call hit the cache
    finally:
        loudness.analyze_file = _real_analyze
finally:
    shutil.rmtree(root, ignore_errors=True)


# ----------------------------------------------------------------------
# Real decoder (optional): a tagless FLAC measured end to end. Skipped when
# ffmpeg is not installed — nothing here needs the network or rsgain.
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
    finally:
        shutil.rmtree(root, ignore_errors=True)

print("ok")
