#!/usr/bin/env python3
"""Library codec target + policy contract (mlo.flac / mlo.containers).

The library's audio codec is a setting (Settings and the setup wizard write
`library_codec`), so what must never break is the RULE, not just the encoder
call:

  * every codec's command line carries the configured rate/quality, and
    `library_codec_args` lands verbatim AFTER it (ffmpeg honours the last
    flag, so the user's own wins);
  * the default policy (`lossless_to_lossy`) converts a lossless source to
    the target and never re-encodes a lossy one;
  * a LOSSY source under a LOSSLESS target is refused — under `all` as well,
    with a reason saying why: encoding it again cannot restore a sample;
  * a file already in the target is skipped on EXTENSION AND CODEC, so an
    AAC-in-M4A is not "already FLAC" and an Opus-in-OGG is not Vorbis;
  * the converted original is moved into the app's trash with its origin
    recorded — never deleted, so the lossless master is recoverable;
  * "keep" converts nothing.

No encoder runs and nothing is encoded: `run_tool` is stubbed and the command
lines the pass WOULD run are asserted, so this test needs no toolchain, no
network and no real audio file.

Run:  python tools/test_codec_policy.py
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo import flac as flac_mod
from mlo.config import DEFAULT_CONFIG, normalize_config
from mlo.containers import CODECS, encoder_args, file_codec

FFMPEG = "C:/fake/ffmpeg.exe"
FFPROBE = "C:/fake/ffprobe.exe"
FLAC_EXE = "C:/fake/flac.exe"


class _Proc:
    """What run_tool returns: callers read returncode/stdout/stderr."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class Recorder:
    """The toolchain as the pass sees it: present, never started."""

    def __init__(self):
        self.calls = []

    def detect(self, *_a, **_k):
        return {
            "ffmpeg": {"ffmpeg_exe": FFMPEG, "ffprobe_exe": FFPROBE,
                       "version": "test"},
            "flac": {"flac_exe": FLAC_EXE, "metaflac_exe": "",
                     "version": "1.5.0"},
        }

    def run_tool(self, cmd, **_kwargs):
        """Record the argv and stand in for the tool's output.

        ffprobe answers with a duration and a title (the conversion verifies
        the duration and copies the tags); ffmpeg and flac.exe "write" the
        file they were told to produce. Nothing is encoded.
        """
        self.calls.append(list(cmd))
        exe = str(cmd[0])
        if exe == FFPROBE:
            return _Proc(stdout=json.dumps(
                {"format": {"duration": "1.0", "tags": {"title": "One"}}}))
        out = cmd[cmd.index("-o") + 1] if "-o" in cmd else cmd[-1]
        with open(out, "wb") as f:
            f.write(b"stub" * 16)
        return _Proc()

    def ffmpeg_calls(self):
        return [c for c in self.calls if str(c[0]) == FFMPEG]

    def flac_calls(self):
        return [c for c in self.calls if str(c[0]) == FLAC_EXE]


def _cfg(folder, **overrides):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({
        "music_folder": folder,
        "targets": [folder],
        "auth_username": "",
        "add_seektables": False,
        "force_reencode_flac": False,
        "encoder_tags": {},
    })
    cfg.update(overrides)
    return normalize_config(cfg)


def _write(folder, name, data=b"source-bytes"):
    path = os.path.join(folder, name)
    with open(path, "wb") as f:
        f.write(data)
    return path


def _run(cfg, probe=None):
    """One pass with the toolchain stubbed; returns (recorder, stats)."""
    rec = Recorder()
    real_tools, real_run, real_probe = (flac_mod.detect_all_tools,
                                        flac_mod.run_tool,
                                        flac_mod.file_codec)
    flac_mod.detect_all_tools, flac_mod.run_tool = rec.detect, rec.run_tool
    if probe is not None:
        flac_mod.file_codec = probe
    try:
        # The progress bars go to stderr and the run's log lines to stdout:
        # both are noise here, and the stats dict is what the test asserts on.
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            stats = flac_mod.run_optimize_flacs(cfg)
    finally:
        flac_mod.detect_all_tools, flac_mod.run_tool = real_tools, real_run
        flac_mod.file_codec = real_probe
    return rec, stats


def _probe_with(overrides):
    """file_codec with the codecs no test can produce stubbed.

    Only the entries in *overrides* are faked (an ALAC or Opus stream needs a
    real encoder); every other extension still answers through the real probe,
    so the test cannot drift from the shipping rule.
    """
    def probe(path):
        ext = os.path.splitext(str(path))[1].lower()
        return overrides.get(ext) or file_codec(path)
    return probe


def _names(folder):
    return sorted(os.listdir(folder))


def _trashed(folder):
    """Names in the bin this install's pass writes to (origin manifest aside)."""
    bin_dir = os.path.join(folder, ".mlo", "trash", "default")
    if not os.path.isdir(bin_dir):
        return []
    return sorted(n for n in os.listdir(bin_dir) if n != ".mlo_manifest.json")


# ----------------------------------------------------------------------
# 1. The command line each codec builds. Pure: nothing runs.
# ----------------------------------------------------------------------
cfg = _cfg("C:/music")
for codec, spec in CODECS.items():
    args = encoder_args(codec, cfg)
    assert args[:len(spec["args"])] == spec["args"], (codec, args)
    if spec["lossless"]:
        assert "-b:a" not in args and "-q:a" not in args, (codec, args)
    else:
        flag, default, unit = spec["rate"][0], spec["rate"][1], spec["rate"][4]
        assert args[args.index(flag) + 1] == f"{default}{unit}", (codec, args)

# The configured rate/quality replaces the codec's own default, clamped to the
# range that codec accepts (mp3 stops at 320 kbps, libvorbis at 10).
assert encoder_args("mp3", _cfg("C:/music", library_codec_bitrate=192))[-1] == "192kbps"
assert encoder_args("opus", _cfg("C:/music", library_codec_bitrate=9999))[-1] == "512kbps"
assert encoder_args("ogg", _cfg("C:/music", library_codec_bitrate=8))[-1] == "8"
assert encoder_args("ogg", _cfg("C:/music", library_codec_bitrate=8))[-2] == "-q:a"
assert encoder_args("flac", _cfg("C:/music", library_codec_quality=8))[-2:] == \
    ["-compression_level", "8"]
# ALAC and the PCM targets expose no compression level, so the quality setting
# never reaches their argv.
assert encoder_args("alac", _cfg("C:/music", library_codec_quality=8)) == \
    list(CODECS["alac"]["args"])

# library_codec_args are appended verbatim, last, so a user's own flag wins.
extra = _cfg("C:/music", library_codec="mp3", library_codec_bitrate=192,
             library_codec_args="-b:a 128k -joint_stereo 1")
assert encoder_args("mp3", extra)[4:] == ["-b:a", "192kbps", "-b:a", "128k",
                                          "-joint_stereo", "1"]
cmd = flac_mod.convert_command(FFMPEG, "in.wav", "out.mp3", extra)
assert cmd == [FFMPEG, "-y", "-v", "error", "-nostdin", "-i", "in.wav",
               "-map", "0:a:0", "-c:a", "libmp3lame", "-f", "mp3",
               "-b:a", "192kbps", "-b:a", "128k", "-joint_stereo", "1",
               "out.mp3"], cmd

# The codec an extension cannot answer is probed from the file, and an
# unparsable file falls back to its extension's own answer.
root = tempfile.mkdtemp(prefix="mlo_codec_")
try:
    wav = _write(root, "probe.wav")
    assert file_codec(wav) == "pcm", file_codec(wav)
    assert file_codec(os.path.join(root, "probe.mp3")) == "mp3"
    assert file_codec(os.path.join(root, "probe.m4a")) == "aac"   # fallback
    assert file_codec(os.path.join(root, "probe.xyz")) == ""

    # A file that already matches: extension AND codec.
    assert flac_mod.conversion_verdict(wav, _cfg(root, library_codec="wav"))[0] == "skip"
    assert flac_mod.conversion_verdict(wav, _cfg(root, library_codec="aiff"))[0] == "convert"
    m4a = _write(root, "probe.m4a")
    # .m4a + an AAC codec is NOT "already the lossless target": it is a lossy
    # source, so the default policy leaves it alone and never even considers it
    # (see _candidate_exts) while under "all" the pass refuses it outright.
    default = _cfg(root, library_codec="flac")
    assert "03 track.m4a" not in flac_mod._candidate_exts(default)
    verdict, reason = flac_mod.conversion_verdict(m4a, default)
    assert verdict == "refuse", (verdict, reason)
    assert "cannot restore a sample" in reason, reason
    # ...and "all" refuses it too: it never up-converts, however the policy is
    # set.
    assert flac_mod.conversion_verdict(
        m4a, _cfg(root, library_codec="flac",
                  library_codec_optimize="all")) == (verdict, reason)
    # Lossy -> lossy is the default policy's quiet skip and "all"'s job.
    assert flac_mod.conversion_verdict(
        m4a, _cfg(root, library_codec="mp3"))[0] == "skip"
    assert flac_mod.conversion_verdict(
        m4a, _cfg(root, library_codec="mp3",
                  library_codec_optimize="all"))[0] == "convert"
    # The same container IS the target when the codec agrees.
    assert flac_mod.conversion_verdict(
        m4a, _cfg(root, library_codec="aac"))[0] == "skip"
finally:
    shutil.rmtree(root, ignore_errors=True)

# ----------------------------------------------------------------------
# 2. A lossless source converts to the lossy target under the default policy,
#    the matched/lossy files are left alone, and the original lands in trash.
# ----------------------------------------------------------------------
root = tempfile.mkdtemp(prefix="mlo_codec_")
album = os.path.join(root, "Album")
os.makedirs(album)
try:
    src = _write(album, "01 track.wav")
    already = _write(album, "02 track.mp3")
    lossy = _write(album, "03 track.ogg")

    rec, stats = _run(_cfg(root, library_codec="mp3"))
    calls = rec.ffmpeg_calls()
    assert len(calls) == 1, calls
    # The one command line carries the target's args and the input/output.
    assert calls[0] == [FFMPEG, "-y", "-v", "error", "-nostdin", "-i",
                        src.replace("\\", "/"), "-map", "0:a:0",
                        "-c:a", "libmp3lame", "-f", "mp3", "-b:a", "320kbps",
                        calls[0][-1]], calls[0]
    assert calls[0][-1].endswith(".mp3") and ".conv_" in calls[0][-1], calls[0]
    names = _names(album)
    assert "01 track.mp3" in names, names            # the converted file
    assert "01 track.wav" not in names, names        # the original left
    assert "02 track.mp3" in names and "03 track.ogg" in names, names
    assert stats["modified_count"] == 1, stats
    assert stats["skipped_count"] == 0, stats

    # The original is IN THE TRASH, with the path it came from recorded, so
    # the Trash page can restore it.
    assert _trashed(root) == ["01 track.wav"], _trashed(root)
    with open(os.path.join(root, ".mlo", "trash", "default",
                           ".mlo_manifest.json"), encoding="utf-8") as f:
        entries = json.load(f)["entries"]
    assert entries["01 track.wav"]["origin"].endswith("Album/01 track.wav"), entries

    # ...and the file the pass left alone is byte-for-byte untouched.
    with open(already, "rb") as f:
        assert f.read() == b"source-bytes"
finally:
    shutil.rmtree(root, ignore_errors=True)

# ----------------------------------------------------------------------
# 3. "all" re-encodes a lossy source into a LOSSY target, and a same-container
#    codec change (Opus-in-OGG -> Vorbis) replaces the file in place, with the
#    old encoding still recoverable from the bin.
# ----------------------------------------------------------------------
root = tempfile.mkdtemp(prefix="mlo_codec_")
album = os.path.join(root, "Album")
os.makedirs(album)
try:
    m4a = _write(album, "01 track.m4a")
    ogg = _write(album, "02 track.ogg")
    _write(album, "03 track.mp3")                       # already the target
    cfg = _cfg(root, library_codec="mp3", library_codec_optimize="all")
    rec, stats = _run(cfg, probe=_probe_with({".m4a": "aac"}))
    # Both lossy files are not the target, so "all" converts both — and the
    # file that IS already mp3 is not touched at all.
    calls = rec.ffmpeg_calls()
    assert len(calls) == 2, calls
    assert all(c[-1].endswith(".mp3") for c in calls), calls
    assert stats["modified_count"] == 2, stats
    assert _names(album) == ["01 track.mp3", "02 track.mp3", "03 track.mp3"], \
        _names(album)
    assert _trashed(root) == ["01 track.m4a", "02 track.ogg"], _trashed(root)
    with open(os.path.join(album, "03 track.mp3"), "rb") as f:
        assert f.read() == b"source-bytes"

    # Target OGG, and a .ogg that is really OPUS: same extension, wrong codec
    # — the pass converts it in place rather than reading it as "already the
    # target", and the Opus original is recoverable from the bin.
    root2 = tempfile.mkdtemp(prefix="mlo_codec_")
    album2 = os.path.join(root2, "Album")
    os.makedirs(album2)
    try:
        _write(album2, "03 track.ogg")
        cfg2 = _cfg(root2, library_codec="ogg", library_codec_optimize="all")
        rec2, stats2 = _run(cfg2, probe=_probe_with({".ogg": "opus"}))
        calls2 = rec2.ffmpeg_calls()
        assert len(calls2) == 1, calls2
        cmd2 = calls2[0]
        assert cmd2[cmd2.index("-c:a") + 1] == "libvorbis"
        assert cmd2[cmd2.index("-q:a") + 1] == "6"
        assert cmd2[-1].endswith(".ogg"), cmd2
        assert _names(album2) == ["03 track.ogg"], _names(album2)
        assert _trashed(root2) == ["03 track.ogg"], _trashed(root2)
        assert stats2["modified_count"] == 1, stats2
    finally:
        shutil.rmtree(root2, ignore_errors=True)
finally:
    shutil.rmtree(root, ignore_errors=True)

# ----------------------------------------------------------------------
# 4. "keep" converts nothing — neither the codec value nor the policy.
# ----------------------------------------------------------------------
for overrides in ({"library_codec": "keep"},
                  {"library_codec": "mp3", "library_codec_optimize": "keep"}):
    root = tempfile.mkdtemp(prefix="mlo_codec_")
    album = os.path.join(root, "Album")
    os.makedirs(album)
    try:
        _write(album, "01 track.wav")
        rec, stats = _run(_cfg(root, **overrides))
        assert rec.ffmpeg_calls() == [], rec.ffmpeg_calls()
        assert _names(album) == ["01 track.wav"], _names(album)
        assert _trashed(root) == [], _trashed(root)
        assert stats["modified_count"] == 0, stats
    finally:
        shutil.rmtree(root, ignore_errors=True)

# The FLAC target still runs script 3's own flac.exe pass; a "keep" library is
# not a frozen one (re-compressing a FLAC stays in the same codec).
root = tempfile.mkdtemp(prefix="mlo_codec_")
album = os.path.join(root, "Album")
os.makedirs(album)
try:
    _write(album, "01 track.flac")
    rec, stats = _run(_cfg(root, library_codec="keep"))
    assert rec.ffmpeg_calls() == [], rec.ffmpeg_calls()
    assert len(rec.flac_calls()) == 1, rec.flac_calls()
finally:
    shutil.rmtree(root, ignore_errors=True)

print("ok")
