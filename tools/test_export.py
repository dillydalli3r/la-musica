#!/usr/bin/env python3
"""Export-to-device contract (server.exporter).

The Export page is the one place the app writes OUTSIDE the library, so the
rules it must never break are pinned here: a selection exports into the
configured layout (multi-disc albums included, whose file names carry the
disc prefix or two discs collide on "01 - Intro"), the compatibility options
actually reach the files (ID3v2.3, embedded art at the requested quality and
resolution cap, ReplayGain tags), a re-run is idempotent for transcodes as
well as copies, sync mode prunes, and exporting back into the music folder is
refused outright.

Needs ffmpeg (bundled at .dependencies/ffmpeg*); skips without it.

Run:  python tools/test_export.py
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
if not FFMPEG:
    print("skip: ffmpeg not installed (run a dependency install first)")
    sys.exit(0)

from mlo.audio import AudioFile          # noqa: E402
from server import exporter              # noqa: E402

# ---------------------------------------------------------------- codec table

# Every preset the UI offers must expand to real ffmpeg arguments, and a plain
# number must still work for the codecs that offer a custom value.
for name in exporter.CODECS:
    for preset in exporter.CODECS[name].get("presets", []):
        assert exporter._codec_args(name, preset["v"]), (name, preset)
assert exporter._codec_args("alac", "") == ["-c:a", "alac"]
assert exporter._codec_args("wav", "24") == ["-c:a", "pcm_s24le"]
assert exporter._codec_args("aiff", "16") == ["-c:a", "pcm_s16be"]
assert exporter._codec_args("mp3", "213") == ["-c:a", "libmp3lame", "-b:a", "213k"]
# …clamped to the codec's range, so a typo cannot reach ffmpeg
assert exporter._codec_args("mp3", "9999") == ["-c:a", "libmp3lame", "-b:a", "320k"]
assert exporter._codec_args("vorbis", "7") == ["-c:a", "libvorbis", "-q:a", "7"]
# …and an unusable value falls back to the codec's own default preset
assert exporter._codec_args("mp3", "") == exporter._codec_args("mp3", "V2")
assert exporter._codec_args("flac", "nonsense") == exporter._codec_args("flac", "8")
# The page renders this table; the copy codec has no knobs at all.
assert exporter.codec_specs()["mp3"]["presets"][0]["v"] == "V0"
assert exporter.codec_specs()["copy"]["presets"] == []

# The subfolder is appended to the destination drive: a path or a traversal
# there would write outside the device, so it collapses to ONE folder name.
for bad in ("../../etc", "Music/../x", "..\\..\\evil", "A/B", "  /Music/ "):
    out = exporter.safe_subfolder(bad)
    assert out and "/" not in out and "\\" not in out and ".." not in out, (bad, out)
assert exporter.safe_subfolder("") == "Music"


# ---------------------------------------------------------------- fixtures

ROOT = tempfile.mkdtemp(prefix="mlo_export_test_")
LIB = os.path.join(ROOT, "Library")
DEST = os.path.join(ROOT, "Dest")
os.makedirs(DEST)
os.makedirs(os.path.join(LIB, "Artist One", "Album A"), exist_ok=True)


class _Tags:
    """Just enough of AudioFile for the path builder (tag reads only)."""

    def __init__(self, tags):
        self._tags = tags

    def get_tag(self, name):
        return self._tags.get(name)


def _rel(structure, tags, music_folder=None, ext=".mp3", disc=""):
    src = os.path.join(LIB, "Artist One", "Album A", "03 - Song.flac")
    return exporter._target_relpath(src, _Tags(tags), structure, music_folder or "", ext, disc)


# The layout each structure promises, and the disc prefix that keeps a
# multi-disc album's two "03 - Song" files apart.
TAGGED = {"TITLE": "Song", "ARTIST": "Artist One", "ALBUMARTIST": "Artist One",
          "ALBUM": "Album A", "TRACKNUMBER": "3"}
assert _rel("artist_album", TAGGED) == os.path.join("Artist One", "Album A", "03 - Song.mp3")
assert _rel("album", TAGGED) == os.path.join("Album A", "03 - Song.mp3")
assert _rel("flat", TAGGED) == "Artist One - 03 - Song.mp3"
assert _rel("mirror", TAGGED, LIB) == os.path.join("Artist One", "Album A", "03 - Song.mp3")
assert _rel("artist_album", TAGGED, None, ".mp3", "2-") == \
    os.path.join("Artist One", "Album A", "2-03 - Song.mp3")
# An untagged file still lands in a sensible place instead of the export root.
assert _rel("artist_album", {}) == os.path.join("Artist One", "Album A", "00 - 03 - Song.mp3")


def make(path, seconds=1.0, freq=440, tags=None, codec=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    codec = codec or {"flac": "flac", "m4a": "aac"}[os.path.splitext(path)[1].lstrip(".")]
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
                    f"sine=frequency={freq}:duration={seconds}", "-c:a", codec, path],
                   check=True, capture_output=True)
    af = AudioFile(path)
    af.defer_save(True)
    for key, value in (tags or {}).items():
        af.set_tag(key, value)
    af.defer_save(False)
    return path


def album_tags(album, track="1", disc=None):
    tags = {"TITLE": f"Track {track}", "ARTIST": "Artist One",
            "ALBUMARTIST": "Artist One", "ALBUM": album, "TRACKNUMBER": track}
    if disc:
        tags["DISCNUMBER"] = disc
    return tags


try:
    one = make(os.path.join(LIB, "Artist One", "Album A", "01 - One.flac"), 1.0, 440,
               album_tags("Album A"))
    two = make(os.path.join(LIB, "Artist One", "Album A", "02 - Two.flac"), 0.8, 660,
               album_tags("Album A", "2"))
    # A real two-disc album: the file names must carry the disc prefix or both
    # discs land on "01 - Track 1.flac" and one of them is silently lost.
    d1 = make(os.path.join(LIB, "Artist One", "Album B", "1-01 - Track 1.flac"), 0.5, 300,
              album_tags("Album B", "1", "1"))
    d2 = make(os.path.join(LIB, "Artist One", "Album B", "2-01 - Track 1.flac"), 0.5, 320,
              album_tags("Album B", "1", "2"))
    # An MP4 source, because its tags read back differently ("1/0" for the track
    # number) — the idempotency check compares the identity a player shows.
    m4a = make(os.path.join(LIB, "Artist Two", "Album C", "01 - One.m4a"), 0.5, 380,
               album_tags("Album C"))

    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
                    "color=c=red:s=900x900", "-frames:v", "1",
                    os.path.join(LIB, "Artist One", "Album A", "cover.jpg")],
                   check=True, capture_output=True)
    with open(os.path.join(LIB, "Artist One", "Album A", "01 - One.lrc"), "w",
              encoding="utf-8") as f:
        f.write("[00:01.00] Track 1\n")
    with open(os.path.join(LIB, "Artist One", "Album A", "description.txt"), "w",
              encoding="utf-8") as f:
        f.write("An album.\n")

    CFG = {"music_folder": LIB, "embed_cover_jpeg_quality": 85,
           "embed_cover_resolution": 400, "jpeg_progressive": True}
    TRACKS = [one, two, d1, d2, m4a]
    OPTS = dict(embed_covers=True, replaygain=True, playlists=True, verify=True,
                workers=4)

    # ------------------------------------------------------------ transcode
    stats = exporter.export_tracks(CFG, TRACKS, DEST, codec="mp3", quality="V2", **OPTS)
    assert stats["failed"] == 0, stats["errors"]
    assert stats["exported"] == 5 and stats["verified"] == 5, stats
    # The lossy source is called out rather than silently re-encoded.
    assert stats["warnings"] and "m4a" in stats["warnings"][0], stats["warnings"]

    album_dir = os.path.join(DEST, "Music", "Artist One", "Album A")
    assert sorted(os.listdir(album_dir)) == [
        "01 - One.lrc", "01 - Track 1.mp3", "02 - Track 2.mp3", "Album A.m3u8",
        "cover.jpg", "description.txt"], os.listdir(album_dir)
    assert sorted(os.listdir(os.path.join(DEST, "Music", "Artist One", "Album B"))) == [
        "1-01 - Track 1.mp3", "2-01 - Track 1.mp3", "Album B.m3u8"]

    exported = os.path.join(album_dir, "01 - Track 1.mp3")
    af = AudioFile(exported)
    assert af.get_tag("TITLE") == "Track 1" and af.get_tag("ALBUM") == "Album A"
    # ReplayGain: track and album gain, both written from the same measurement.
    assert "dB" in str(af.get_tag("REPLAYGAIN_TRACK_GAIN")), af.all_tags()
    assert "dB" in str(af.get_tag("REPLAYGAIN_ALBUM_GAIN")), af.all_tags()
    # Cover art, embedded and re-encoded to the requested cap.
    pics = af.embedded_pictures()
    assert pics and pics[0][0] == "image/jpeg", pics
    # ID3v2.3 is the point of the option: older players do not read v2.4.
    with open(exported, "rb") as f:
        head = f.read(4)
    assert head[:3] == b"ID3" and head[3] == 3, head
    # Playlists: relative paths, EXTINF with the duration, both levels present.
    playlist = open(os.path.join(DEST, "Music", "all.m3u8"), encoding="utf-8").read()
    assert playlist.startswith("#EXTM3U\n#EXTINF:") and "Artist One/Album A/01 - Track 1.mp3" in playlist
    assert os.path.isfile(os.path.join(DEST, "Music", "Artist One", "Album A", "Album A.m3u8"))

    # ------------------------------------------------------------ idempotent
    again = exporter.export_tracks(CFG, TRACKS, DEST, codec="mp3", quality="V2", **OPTS)
    assert (again["exported"], again["skipped"], again["failed"]) == (0, 5, 0), again

    # A CHANGED CBR preset must be reported, not silently swallowed as "already
    # exported": the destination holds 128 kbps and the run asks for 320.
    switched = exporter.export_tracks(CFG, [one], DEST, codec="mp3", quality="320",
                                      embed_covers=False, playlists=False,
                                      verify=True, replaygain=False)
    assert (switched["exported"], switched["skipped"], switched["failed"]) == (0, 0, 1), switched

    # ------------------------------------------------------------ copy path
    DEST2 = os.path.join(ROOT, "DestCopy")
    os.makedirs(DEST2)
    copied = exporter.export_tracks(CFG, [one, two], DEST2, codec="copy",
                                    embed_covers=True, playlists=False, verify=True,
                                    workers=2)
    assert copied["failed"] == 0, copied["errors"]
    copy_of_one = os.path.join(DEST2, "Music", "Artist One", "Album A", "01 - Track 1.flac")
    copy_af = AudioFile(copy_of_one)
    assert copy_af.get_tag("TITLE") == "Track 1"
    assert copy_af.embedded_pictures(), "the copied FLAC must carry the album cover"

    # ------------------------------------------------------------ sync mode
    stale = os.path.join(DEST2, "Music", "Artist One", "Album A", "99 - Stale.flac")
    shutil.copy2(two, stale)
    pruned = exporter.export_tracks(CFG, [one], DEST2, codec="copy", embed_covers=False,
                                    playlists=False, prune=True, verify=True)
    assert pruned["pruned"] == 2, (pruned["pruned"], pruned["pruned_files"])
    assert not os.path.exists(stale)

    # ------------------------------------------------------------ refusal
    try:
        exporter.export_tracks(CFG, [one], LIB, codec="copy")
    except ValueError as e:
        assert "music folder" in str(e)
    else:
        raise AssertionError("exporting into the music folder must be refused")
finally:
    shutil.rmtree(ROOT, ignore_errors=True)

print("ok  export: layout + disc prefixes, ID3v2.3, embedded art, ReplayGain, "
      "playlists, idempotent re-run, copy-with-art, prune, library-destination refusal")
