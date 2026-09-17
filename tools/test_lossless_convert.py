#!/usr/bin/env python3
"""Lossless-source conversion contract (mlo.flac).

The auto-importer and the Soulseek import button re-containerize whatever
lossless codec a download arrived in into the configured target codec, and
delete the original once the conversion verified. Two things must never
break: an AAC file must NOT be touched (it is lossy — rewriting it as
"lossless" would launder it), and the tags must survive the conversion.

Needs the bundled ffmpeg (.dependencies/ffmpeg*); skips without it.

Run:  python tools/test_lossless_convert.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo import tools as tools_mod

TOOLS = tools_mod.detect_all_tools()
FFMPEG = (TOOLS.get("ffmpeg") or {}).get("ffmpeg_exe")
FFPROBE = (TOOLS.get("ffmpeg") or {}).get("ffprobe_exe")
FLAC = (TOOLS.get("flac") or {}).get("flac_exe")
if not FFMPEG or not FFPROBE or not FLAC:
    print("skip: ffmpeg/flac not installed (run a dependency install first)")
    sys.exit(0)

from mlo.config import DEFAULT_CONFIG
from mlo.flac import convert_album_lossless, target_codec


def _make(path, codec_args, title):
    subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
         "-t", "1", "-metadata", f"title={title}", "-metadata", "artist=Test Artist",
         *codec_args, path],
        check=True, capture_output=True,
    )


def _tags(path):
    """(TITLE, ARTIST) as ffprobe reports them for the converted file.

    Containers spell the keys differently — MP4/WAV report lowercase,
    Vorbis comments (FLAC) uppercase — so compare case-insensitively."""
    import json
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", path],
        check=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout
    fmt = (json.loads(out or "{}").get("format") or {}).get("tags") or {}
    lower = {str(k).lower(): v for k, v in fmt.items()}
    return lower.get("title"), lower.get("artist")


def _cfg(folder, codec, remove_original=True):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({
        "music_folder": folder,
        "targets": [folder],
        "lossless_target_codec": codec,
        "lossless_remove_original": remove_original,
        "optimize_convert_lossless": True,
        "flac_level": 5,
        "add_seektables": False,
        "force_reencode_flac": False,
    })
    return cfg


root = tempfile.mkdtemp(prefix="mlo_convert_")
try:
    # ---- target FLAC: WAV + ALAC converted, AAC untouched -------------------
    album = os.path.join(root, "album")
    os.makedirs(album)
    _make(os.path.join(album, "01 track.wav"), ["-c:a", "pcm_s16le"], "One")
    _make(os.path.join(album, "02 track.m4a"), ["-c:a", "alac"], "Two")
    _make(os.path.join(album, "03 track.m4a"), ["-c:a", "aac", "-b:a", "192k"], "Three")
    _make(os.path.join(album, "04 track.flac"), ["-c:a", "flac"], "Four")

    convert_album_lossless(album, _cfg(album, "flac"))
    names = sorted(os.listdir(album))
    assert "01 track.flac" in names, names
    assert "01 track.wav" not in names, names
    assert "02 track.flac" in names, names          # ALAC -> FLAC
    assert "02 track.m4a" not in names, names
    assert "03 track.m4a" in names, names           # AAC is lossy: left alone
    assert "03 track.flac" not in names, names
    assert "04 track.flac" in names, names
    assert _tags(os.path.join(album, "01 track.flac")) == ("One", "Test Artist"), \
        _tags(os.path.join(album, "01 track.flac"))
    assert _tags(os.path.join(album, "02 track.flac")) == ("Two", "Test Artist"), \
        _tags(os.path.join(album, "02 track.flac"))

    # ---- target ALAC: sources become .m4a, FLAC re-containerized ------------
    album2 = os.path.join(root, "album_alac")
    os.makedirs(album2)
    _make(os.path.join(album2, "05 track.wav"), ["-c:a", "pcm_s16le"], "Five")
    _make(os.path.join(album2, "06 track.flac"), ["-c:a", "flac"], "Six")
    assert target_codec(_cfg(album2, "ALAC")) == "alac"

    convert_album_lossless(album2, _cfg(album2, "alac"))
    names2 = sorted(os.listdir(album2))
    assert "05 track.m4a" in names2 and "05 track.wav" not in names2, names2
    assert "06 track.m4a" in names2 and "06 track.flac" not in names2, names2
    assert _tags(os.path.join(album2, "06 track.m4a")) == ("Six", "Test Artist"), \
        _tags(os.path.join(album2, "06 track.m4a"))

    # ---- the original survives when removal is off --------------------------
    album3 = os.path.join(root, "album_keep")
    os.makedirs(album3)
    _make(os.path.join(album3, "07 track.wav"), ["-c:a", "pcm_s16le"], "Seven")
    convert_album_lossless(album3, _cfg(album3, "flac", remove_original=False))
    names3 = sorted(os.listdir(album3))
    assert "07 track.flac" in names3 and "07 track.wav" in names3, names3
finally:
    shutil.rmtree(root, ignore_errors=True)

print("ok")
