#!/usr/bin/env python3
"""Export-to-device contract (server.exporter).

The Export page is the one place the app writes OUTSIDE the library, so the
rules it must never break are pinned here: a selection exports into the
configured layout (the shipped one is the library's own tree — ALBUMARTIST /
Album / "1-01 Title", disc number included so a two-disc album cannot collide
on one name — and a structure the user typed is a naming script evaluated by
the same grammar), the compatibility options
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
from mlo import naming                   # noqa: E402
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


# ------------------------------------------------------- the file selection
# WHAT an export copies is one table (server.exporter.FILE_FAMILIES) and one
# classifier: a non-audio sibling's family is what the menu offers, what the
# copy pass writes and what the run reports it left behind — so the audit and
# the copy pass cannot disagree about a file.
for _key, _spec in exporter.FILE_FAMILIES.items():
    assert _spec["label"] and _spec["hint"], (_key, _spec)
# Every family the extension table names is a family the menu offers…
_kinds = {family for family, _why in exporter._EXTRA_REASONS.values()}
assert _kinds <= set(exporter.FILE_FAMILIES), sorted(_kinds - set(exporter.FILE_FAMILIES))
# …and the menu is what the page renders, key for key.
assert [f["v"] for f in exporter.file_families()["families"]] == list(exporter.FILE_FAMILIES)
# A run nobody asked anything of writes the tracks alone (today's behaviour),
# and the `sidecars` switch it replaced resolves to the set it always copied.
assert exporter.copy_files({}, {}) == ("audio",)
assert exporter.copy_files({}, {"sidecars": True}) == exporter.LEGACY_SIDECAR_FAMILIES
assert set(exporter.LEGACY_SIDECAR_FAMILIES) <= set(exporter.FILE_FAMILIES)
assert "audio" in exporter.LEGACY_SIDECAR_FAMILIES
assert exporter.copy_files({}, {"copy_files": ["cover", "audio", "audio"]}) == ("audio", "cover")
# A saved selection beats the switch it replaced, and a per-run one beats both.
assert exporter.copy_files({"export_copy_files": ["cue"], "export_sidecars": True}, {}) == ("cue",)
assert exporter.copy_files({"export_copy_files": ["cue"]}, {"copy_files": ["text"]}) == ("text",)
# An empty selection and an unknown family are refused with a sentence — before
# anything is written, so a refused run leaves no folder behind.
assert exporter.copy_files_error(["audio"]) == ""
assert "unknown file famil" in exporter.copy_files_error(["audio", "covers"])
assert "at least one" in exporter.copy_files_error([])
assert "at least one" in exporter.copy_files_error("")
# …and the comma-separated spelling a hand-edited config may hold is the same
# selection (config.json is user-editable).
assert exporter.copy_files({}, {"copy_files": "audio, cover"}) == ("audio", "cover")


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

    def all_tags(self):
        return dict(self._tags)


def _rel(structure, tags, music_folder=None, ext=".mp3", disc="", script=""):
    src = os.path.join(LIB, "Artist One", "Album A", "03 - Song.flac")
    return exporter._target_relpath(src, _Tags(tags), structure, music_folder or "",
                                    ext, disc, script)


def listing(root):
    """Every file under *root*, with "/" separators, sorted."""
    return sorted(os.path.join(base, f).replace("\\", "/")
                  for base, _dirs, files in os.walk(root) for f in files)


# The layout each structure promises. The shipped one is a naming script, so it
# writes the LIBRARY's own file name — disc number first, "1-03 Song", the way
# mlo.naming's default does — and takes ALBUMARTIST (never the track's own
# ARTIST). The hand-built ones keep the " - " spelling a saved export_structure
# pins, and take the disc prefix only for a multi-disc album.
TAGGED = {"TITLE": "Song", "ARTIST": "Artist One", "ALBUMARTIST": "Artist One",
          "ALBUM": "Album A", "TRACKNUMBER": "3"}
assert _rel(exporter.DEFAULT_STRUCTURE, TAGGED) == \
    os.path.join("Artist One", "Album A", "1-03 Song.mp3")
assert _rel("album", TAGGED) == os.path.join("Album A", "03 - Song.mp3")
assert _rel("flat", TAGGED) == "Artist One - 03 - Song.mp3"
assert _rel("mirror", TAGGED, LIB) == os.path.join("Artist One", "Album A", "03 - Song.mp3")
assert _rel("album", TAGGED, None, ".mp3", "2-") == \
    os.path.join("Album A", "2-03 - Song.mp3")
# ALBUMARTIST, not ARTIST: a compilation is ONE folder, not one per track.
COMPILATION = dict(TAGGED, ARTIST="Guest Singer", ALBUMARTIST="Various Artists")
assert _rel(exporter.DEFAULT_STRUCTURE, COMPILATION).split(os.sep)[0] == "Various Artists"
# A file with no ALBUMARTIST falls back to ARTIST, exactly as track_variables
# does for the library's own script.
assert _rel(exporter.DEFAULT_STRUCTURE, {k: v for k, v in TAGGED.items()
                                         if k != "ALBUMARTIST"}).split(os.sep)[0] == "Artist One"
# The disc number is written for a SINGLE-disc album too ("1-"), and a two-disc
# album's files can never collide on one name.
assert _rel(exporter.DEFAULT_STRUCTURE, dict(TAGGED, DISCNUMBER="2")) == \
    os.path.join("Artist One", "Album A", "2-03 Song.mp3")
# A custom structure is the same grammar: whatever the user typed, exactly.
assert _rel("custom", TAGGED, None, ".mp3", "", "%album%/%artist% - %title%") == \
    os.path.join("Album A", "Artist One - Song.mp3")
assert _rel("custom", TAGGED, None, ".mp3", "", "%albumartist%/$left(%title%,2)%tracknumber%") == \
    os.path.join("Artist One", "So3.mp3")
# An untagged file still lands in a sensible place instead of the export root:
# the folders it already sits in and its own file name stand in for the tags.
assert _rel(exporter.DEFAULT_STRUCTURE, {}) == \
    os.path.join("Artist One", "Album A", "1-00 03 - Song.mp3")

# ------------------------------------------------------- invalid characters
# The app's ONE filename rule, through the real path builder: a character a
# filesystem refuses becomes "_" in the name the run writes — never dropped,
# never transliterated, one "_" per character. The default config is what an
# export uses, so this is what a run writes without being asked for anything.
for bad, title in (("<", "A<B"), (">", "A>B"), (":", "A:B"), ('"', 'A"B'),
                   ("|", "A|B"), ("?", "A?B"), ("*", "A*B"), ("\\", "A\\B"),
                   ("\x01", "A\x01B"), ("\t", "A\tB")):
    rel = _rel(exporter.DEFAULT_STRUCTURE, dict(TAGGED, TITLE=title))
    assert rel == os.path.join("Artist One", "Album A", "1-03 A_B.mp3"), (bad, rel)
# A run of three becomes "___": the mapping is one "_" per invalid character,
# not a collapse.
assert _rel(exporter.DEFAULT_STRUCTURE, dict(TAGGED, TITLE="A***B")).endswith("A___B.mp3")
# A trailing dot or space is invalid on Windows (the filesystem drops it, so
# the name the app computed would not be the name on disk) — replaced too.
assert _rel(exporter.DEFAULT_STRUCTURE, dict(TAGGED, TITLE="Vol. 1.")).endswith("Vol. 1_.mp3")
assert _rel(exporter.DEFAULT_STRUCTURE, dict(TAGGED, TITLE="Track ")).endswith("Track_.mp3")
# A reserved device name is not a name, with or without an extension: the
# album folder and the artist folder are guarded as whole names.
assert _rel("album", dict(TAGGED, ALBUM="CON")) == os.path.join("CON_", "03 - Song.mp3")
assert _rel("album", dict(TAGGED, ALBUM="AUX.mp3")) == os.path.join("AUX_.mp3", "03 - Song.mp3")
assert _rel(exporter.DEFAULT_STRUCTURE, dict(TAGGED, ALBUMARTIST="NUL", ALBUM="COM1")) == \
    os.path.join("NUL_", "COM1_", "1-03 Song.mp3")
# The subfolder under the drive root is one NAME too, so it is named by the
# same rule (and can never be a traversal or a reserved name).
assert exporter.safe_subfolder("CON") == "CON_"
assert exporter.safe_subfolder("Music.") == "Music_"
# "/" is STRUCTURE in a template but a character of a TAG VALUE: a title of
# "AC/DC" names one file inside one album folder, never a level of its own.
assert _rel("custom", dict(TAGGED, TITLE="AC/DC"), None, ".mp3", "",
            "%albumartist%/%title%") == os.path.join("Artist One", "AC_DC.mp3")
assert _rel(exporter.DEFAULT_STRUCTURE, dict(TAGGED, TITLE="AC/DC", ALBUM="AC/DC")) == \
    os.path.join("Artist One", "AC_DC", "1-03 AC_DC.mp3")
assert _rel("album", dict(TAGGED, TITLE="AC/DC", ALBUM="AC/DC")) == \
    os.path.join("AC_DC", "03 - AC_DC.mp3")
assert _rel("flat", dict(TAGGED, TITLE="AC/DC", ALBUMARTIST="AC/DC")) == "AC_DC - 03 - AC_DC.mp3"

# Idempotent: sanitising a name the rule already produced changes nothing, so
# organizing or exporting an album a second time is a no-op rather than a
# rename. (Asserted on the whole relative path, the unit a writer works in.)
for _text in ("Artist One/Album A/1-03 AC_DC.mp3", "CON_/AUX_.mp3/1-03 A_B.mp3",
              "NUL_/COM1_/1-03 Vol. 1_.mp3"):
    assert naming.sanitize_path(_text) == _text
    for _seg in _text.split("/"):
        assert naming.sanitize_segment(_seg) == _seg, _seg
        assert naming.sanitize_segment(naming.sanitize_segment(_seg)) == _seg, _seg

# What the page offers and what a run accepts are the same table, and the menu
# carries the vocabulary a custom script is written in.
_menu = exporter.structure_menu()
assert [s["v"] for s in _menu["structures"]] == list(exporter.STRUCTURES)
assert all(s["label"] for s in _menu["structures"])
assert "albumartist" in _menu["fields"] and "discnumber" in _menu["fields"]
assert "num" in _menu["functions"]
# A structure that cannot name a path is refused with a sentence — a bad field,
# a bad function, an empty script and an unknown key all fail BEFORE any run.
for bad in ("%nope%/%title%", "$iff(%title%,%title%,x)/%title%", "", "   "):
    why = exporter.structure_error("custom", bad)
    assert why and "custom folder structure" in why, (bad, why)
assert "unknown folder structure" in exporter.structure_error("artist_album", "")
assert exporter.structure_error(exporter.DEFAULT_STRUCTURE, "") == ""
assert exporter.structure_error("mirror", "") == ""
# …and starting a run with one is a ValueError, never a half-written tree.
for structure, script in (("custom", "%nope%"), ("artist_album", "")):
    try:
        exporter.export_tracks({}, [], DEST, structure=structure, structure_script=script)
    except ValueError as e:
        assert "folder structure" in str(e), str(e)
    else:
        raise AssertionError(f"{structure!r} must be refused")
# The preview shows the sample track's path through the same evaluator the run
# uses (and the codec's extension), or the refusal sentence.
_pv = exporter.preview_structure("$upper(%albumartist%)/%album%/%title%", ".flac")
assert _pv["ok"] and _pv["path"] == "SYSTEM OF A DOWN/Toxicity/Psycho.flac", _pv
assert exporter.preview_structure("%nope%")["ok"] is False
assert exporter.preview_structure("")["ok"] is False


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
    # The login gate reads the config through server.auth's own import (not the
    # `load_config` alias patched above), so it is switched off HERE rather than
    # left to whatever auth state the machine happens to have.
    from server import auth as _auth_mod
    _auth_mod.requires_login = lambda request, state: False
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
    # The shipped structure is the library's own: the disc number comes first
    # even on a single-disc album ("1-01 Track 1"), and the " - " the old
    # preset used is gone (that is the library's spelling, not a choice here).
    # An export carries AUDIO: the cover travels EMBEDDED (asserted below) and
    # the cover.jpg / description.txt / .lrc of the library stay there. The
    # album playlist IS here because this run asked for playlists=True; without
    # it (the default) the album export writes no .m3u8 at all.
    assert sorted(os.listdir(album_dir)) == [
        "1-01 Track 1.mp3", "1-02 Track 2.mp3", "Album A.m3u8"], os.listdir(album_dir)
    assert stats["sidecars"] == 0, stats
    assert stats["playlists"] == 4, stats   # 3 album playlists + all.m3u8
    # Nothing was left behind in silence: the report names every non-audio file
    # beside the selection with the reason it did not travel.
    assert {r["name"] for r in stats["excluded"]} == {
        "01 - One.lrc", "cover.jpg", "description.txt"}, stats["excluded"]
    # …classified by the family the file belongs to (the key the file
    # selection offers): the cover, the album's own description, the .lrc
    assert stats["excluded_counts"] == {"cover": 1, "description": 1, "lyrics": 1}, \
        stats["excluded_counts"]
    assert stats["excluded_note"] and "not exported" in stats["excluded_note"]
    assert sorted(os.listdir(os.path.join(DEST, "Music", "Artist One", "Album B"))) == [
        "1-01 Track 1.mp3", "2-01 Track 1.mp3", "Album B.m3u8"]

    exported = os.path.join(album_dir, "1-01 Track 1.mp3")
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
    assert playlist.startswith("#EXTM3U\n#EXTINF:") and "Artist One/Album A/1-01 Track 1.mp3" in playlist
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
    copy_of_one = os.path.join(DEST2, "Music", "Artist One", "Album A", "1-01 Track 1.flac")
    copy_af = AudioFile(copy_of_one)
    assert copy_af.get_tag("TITLE") == "Track 1"
    assert copy_af.embedded_pictures(), "the copied FLAC must carry the album cover"

    # ------------------------------------------------------- custom structure
    # A custom structure is a naming script too, so the tree it writes is
    # exactly what it says — and the page's own preview runs the same
    # evaluator (exporter.preview_structure) the run does.
    custom_dest = os.path.join(ROOT, "DestCustom")
    os.makedirs(custom_dest)
    custom_script = "$upper(%albumartist%)/%album%/%discnumber%-%tracknumber% %title%"
    custom = exporter.export_tracks(CFG, [one, two, d1, d2], custom_dest,
                                    structure="custom", structure_script=custom_script,
                                    embed_covers=False, playlists=False, verify=True,
                                    sidecars=False)
    assert custom["failed"] == 0, custom["errors"]
    root_slash = custom_dest.replace("\\", "/")
    assert listing(custom_dest) == [
        f"{root_slash}/Music/ARTIST ONE/Album A/1-1 Track 1.flac",
        f"{root_slash}/Music/ARTIST ONE/Album A/1-2 Track 2.flac",
        # the two discs keep their own numbers: nothing collides on one name
        f"{root_slash}/Music/ARTIST ONE/Album B/1-1 Track 1.flac",
        f"{root_slash}/Music/ARTIST ONE/Album B/2-1 Track 1.flac",
    ], listing(custom_dest)

    # A script that names nothing at all for a REAL track is refused before
    # anything is written, even though the sample the preview uses has the tag
    # (an empty %field% drops its own segment, so only a path with nothing left
    # in it is refused).
    try:
        exporter.export_tracks(CFG, [one], custom_dest, structure="custom",
                               structure_script="%genre%")
    except ValueError as e:
        assert "produced no path" in str(e), str(e)
    else:
        raise AssertionError("a structure naming no path must be refused")

    stale = os.path.join(DEST2, "Music", "Artist One", "Album A", "99 - Stale.flac")
    shutil.copy2(two, stale)
    pruned = exporter.export_tracks(CFG, [one], DEST2, codec="copy", embed_covers=False,
                                    playlists=False, prune=True, verify=True)
    assert pruned["pruned"] == 2, (pruned["pruned"], pruned["pruned_files"])
    assert not os.path.exists(stale)

    # ----------------------------------------- hostile names through the run
    # A REAL export of tags a filesystem cannot spell. This is the same path
    # builder the checks above use, driven end to end: what lands on the device
    # comes from mlo.naming's one rule, so a "/" inside a TITLE names one file
    # (never a second directory level), a "|" in the artist one folder, a
    # reserved device name is spelled so Windows can hold it — and every TAG
    # keeps exactly what the source said, because the library's data is the
    # truth and only the NAME on disk is sanitised.
    HOST_LIB = os.path.join(ROOT, "HostileLib")
    HOST_DEST = os.path.join(ROOT, "HostileDest")
    os.makedirs(HOST_DEST)
    HOST_SRC = os.path.join(HOST_LIB, "Album")
    HOST_CASES = [
        # artist, album, title, track, expected path under the export root
        ("AC/DC", "CON", "AC/DC", "1", "AC_DC/CON_/1-01 AC_DC.flac"),
        ("AC/DC", "CON", "Bad:Name?*", "2", "AC_DC/CON_/1-02 Bad_Name__.flac"),
        # the tag writer trims the trailing blank (tag hygiene), so what the
        # rule sees is "Trail." — and the trailing dot still cannot survive
        ("AC/DC", "AUX.mp3", "Trail. ", "3", "AC_DC/AUX_.mp3/1-03 Trail_.flac"),
        # a control character in a tag (the rip's own encoding damage) is part
        # of the invalid set too, and it must not reach a name either
        ("AC/DC", "CON", "Bell\x01X", "4", "AC_DC/CON_/1-04 Bell_X.flac"),
    ]
    host_paths = []
    for artist, album, title, track, _rel_want in HOST_CASES:
        host_paths.append(make(
            os.path.join(HOST_SRC, f"{track} - t.flac"), 0.4, 520,
            {"TITLE": title, "ARTIST": artist, "ALBUMARTIST": artist,
             "ALBUM": album, "TRACKNUMBER": track}))
    hostile_run = exporter.export_tracks(CFG, host_paths, HOST_DEST, codec="copy",
                                         playlists=False, verify=True, workers=2)
    assert hostile_run["failed"] == 0, hostile_run["errors"]
    host_root = HOST_DEST.replace("\\", "/")
    assert listing(HOST_DEST) == sorted(
        f"{host_root}/Music/{want}" for *_x, want in HOST_CASES), listing(HOST_DEST)
    # No written name carries a character Windows refuses, a control character,
    # a trailing dot/space or a reserved device name — the rule is what the
    # acceptance list is, checked on what the RUN actually wrote.
    _RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} \
        | {f"lpt{i}" for i in range(1, 10)}
    for rel in (want for *_x, want in HOST_CASES):
        for seg in rel.split("/"):
            assert not any(c in seg for c in '<>:"/\\|?*'), seg
            assert not any(ord(c) < 32 for c in seg), seg
            assert seg == seg.rstrip(" ."), seg
            assert os.path.splitext(seg)[0].lower() not in _RESERVED, seg
    # AC/DC is ONE file in ONE album folder: the tag made no directory level.
    assert len([p for p in listing(HOST_DEST) if "AC_DC" in p]) == 4, listing(HOST_DEST)
    assert f"{host_root}/Music/AC_DC/CON_/1-01 AC_DC.flac" in listing(HOST_DEST)
    assert f"{host_root}/Music/AC_DC/AUX_.mp3/1-03 Trail_.flac" in listing(HOST_DEST)
    # ...and the TAG is untouched: the file on disk is "AC_DC", the title in it
    # is "AC/DC" (the app audits and grades on the tag, never on the name).
    moved = AudioFile(os.path.join(HOST_DEST, "Music", "AC_DC", "CON_", "1-01 AC_DC.flac"))
    assert moved.get_tag("TITLE") == "AC/DC", moved.all_tags()
    assert moved.get_tag("ALBUM") == "CON" and moved.get_tag("ARTIST") == "AC/DC"
    # Idempotent at the ALBUM level: a second run under the same rule renames
    # nothing and re-copies nothing — "_"-vs-invalid is not a difference.
    again_host = exporter.export_tracks(CFG, host_paths, HOST_DEST, codec="copy",
                                        playlists=False, verify=True, workers=2)
    assert (again_host["exported"], again_host["skipped"], again_host["failed"]) == (0, 4, 0), again_host
    assert listing(HOST_DEST) == sorted(
        f"{host_root}/Music/{want}" for *_x, want in HOST_CASES), listing(HOST_DEST)

    # ------------------------------------- extra files are audited, never lost
    # A library album carries more than audio. An export carries the AUDIO, so
    # every one of these stays in the library — and the run reports each of
    # them, by name and classification, instead of dropping them in silence.
    EX_LIB = os.path.join(LIB, "Artist One", "Extras Album")
    EX_DEST = os.path.join(ROOT, "ExtrasDest")
    os.makedirs(EX_DEST)
    ex_track = make(os.path.join(EX_LIB, "1-01 Extras.flac"), 0.4, 440,
                    album_tags("Extras Album", "1"))
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
                    "color=c=blue:s=600x600", "-frames:v", "1",
                    os.path.join(EX_LIB, "cover.jpg")], check=True, capture_output=True)
    for extra_name in ("Album.accurip", "Album.log", "Album.cue", "notes.txt",
                       "Artist.jpg", "Album.m3u8", "release.nfo", "Album.md5",
                       "Album.sfv", "Thumbs.db", "liner.bak", "description.txt"):
        with open(os.path.join(EX_LIB, extra_name), "w", encoding="utf-8") as f:
            f.write("x\n")
    os.makedirs(os.path.join(EX_LIB, "Scans"), exist_ok=True)
    ex_run = exporter.export_tracks(CFG, [ex_track], EX_DEST, codec="copy",
                                    embed_covers=True, verify=True)
    assert ex_run["failed"] == 0, ex_run["errors"]
    # Only the audio travelled — no .accurip/.log/.cue/.txt/.jpg/.m3u8/.nfo/
    # .md5/.sfv/Thumbs.db/.bak and no stray subfolder in the exported tree
    # (the default selection is the tracks alone).
    assert listing(EX_DEST) == [
        f"{EX_DEST.replace(chr(92), '/')}/Music/Artist One/Extras Album/1-01 Track 1.flac"
    ], listing(EX_DEST)
    # The cover still reaches the device — EMBEDDED in the file it belongs to.
    assert AudioFile(os.path.join(EX_DEST, "Music", "Artist One", "Extras Album",
                                  "1-01 Track 1.flac")).embedded_pictures(), \
        "the cover must travel embedded"
    # Every extra is reported, named and given the FAMILY it belongs to — the
    # family the menu offers and the copy pass writes (FILE_FAMILIES), never a
    # second vocabulary of its own.
    reported = {r["name"]: r for r in ex_run["excluded"]}
    assert set(reported) == {
        "Album.accurip", "Album.log", "Album.cue", "notes.txt", "Artist.jpg",
        "Album.m3u8", "release.nfo", "Album.md5", "Album.sfv", "Thumbs.db",
        "liner.bak", "cover.jpg", "description.txt", "Scans/"}, sorted(reported)
    for name in ("Album.accurip", "Album.log"):
        assert reported[name]["kind"] == "log", reported[name]
    for name in ("Album.md5", "Album.sfv"):
        assert reported[name]["kind"] == "checksum", reported[name]
    assert reported["Album.cue"]["kind"] == "cue", reported["Album.cue"]
    for name in ("notes.txt", "release.nfo"):
        assert reported[name]["kind"] == "text", reported[name]
    assert reported["description.txt"]["kind"] == "description", reported["description.txt"]
    assert reported["Album.m3u8"]["kind"] == "playlist", reported["Album.m3u8"]
    for name in ("cover.jpg", "Artist.jpg"):
        assert reported[name]["kind"] == "cover", reported[name]
    for name in ("Thumbs.db", "liner.bak"):
        assert reported[name]["kind"] == "other", reported[name]
    assert reported["Scans/"]["dir"] is True and reported["Scans/"]["kind"] == "other"
    assert all(r["reason"] for r in ex_run["excluded"]), ex_run["excluded"]
    assert all(r["album"] == "Artist One/Extras Album" for r in ex_run["excluded"])
    assert ex_run["excluded_total"] == 14, ex_run["excluded_total"]
    assert ex_run["excluded_counts"] == {"log": 2, "checksum": 2, "cue": 1, "text": 2,
                                         "description": 1, "playlist": 1, "cover": 2,
                                         "other": 3}, ex_run["excluded_counts"]
    assert "14 non-audio file(s) not exported" in ex_run["excluded_note"], ex_run["excluded_note"]
    assert ex_run["copy_files"] == ["audio"], ex_run["copy_files"]

    # The two switches still exist for a device that wants the old behaviour:
    # asking for them copies the sidecars and writes the album playlists again.
    EX_OPT = os.path.join(ROOT, "ExtrasOptIn")
    os.makedirs(EX_OPT)
    opt_in = exporter.export_tracks(CFG, [ex_track], EX_OPT, codec="copy",
                                    sidecars=True, playlists=True, verify=True)
    assert opt_in["failed"] == 0, opt_in["errors"]
    opt_files = sorted(os.path.basename(p) for p in listing(EX_OPT))
    assert "cover.jpg" in opt_files and "Album.cue" in opt_files \
        and "Album.accurip" in opt_files, opt_files
    assert "Extras Album.m3u8" in opt_files, opt_files
    assert opt_in["sidecars"] and opt_in["playlists"], opt_in
    # The classic switch resolves to its own family set through the same code
    # path, and copies what it always copied: the cover and the description,
    # the .lrc/.cue/.log/.accurip — never the .md5/.sfv/.nfo/Thumbs.db, never a
    # stray subfolder.
    assert opt_in["copy_files"] == list(exporter.LEGACY_SIDECAR_FAMILIES), opt_in["copy_files"]
    assert "notes.txt" not in opt_files and "release.nfo" not in opt_files \
        and "Album.md5" not in opt_files and "Thumbs.db" not in opt_files, opt_files

    # …and WHAT a run writes is the caller's selection, family by family: asked
    # for the artwork, the notes and the album's description, it writes exactly
    # those beside the tracks and reports the rest as left behind.
    EX_PICK = os.path.join(ROOT, "ExtrasPicked")
    os.makedirs(EX_PICK)
    picked = exporter.export_tracks(CFG, [ex_track], EX_PICK, codec="copy",
                                    copy_files=["audio", "cover", "text", "description"],
                                    embed_covers=False, verify=True)
    assert picked["failed"] == 0, picked["errors"]
    assert picked["copy_files"] == ["audio", "cover", "description", "text"], picked["copy_files"]
    assert sorted(os.path.basename(p) for p in listing(EX_PICK)) == [
        "1-01 Track 1.flac", "Artist.jpg", "cover.jpg", "description.txt",
        "notes.txt", "release.nfo"], listing(EX_PICK)
    assert picked["sidecars"] == 5, picked["sidecars"]
    assert {r["name"] for r in picked["excluded"]} == {
        "Album.accurip", "Album.log", "Album.cue", "Album.m3u8", "Album.md5",
        "Album.sfv", "Thumbs.db", "liner.bak", "Scans/"}, picked["excluded"]

    # …and "anything else" is honest about what it means: the files this app
    # cannot classify travel, a stray SUBFOLDER is still only reported — the
    # promise `extra_files` makes, and the copy pass keeps it.
    EX_OTHER = os.path.join(ROOT, "ExtrasOther")
    os.makedirs(EX_OTHER)
    others = exporter.export_tracks(CFG, [ex_track], EX_OTHER, codec="copy",
                                    copy_files=["audio", "other"], verify=True)
    assert others["failed"] == 0, others["errors"]
    assert sorted(os.path.basename(p) for p in listing(EX_OTHER)) == [
        "1-01 Track 1.flac", "Thumbs.db", "liner.bak"], listing(EX_OTHER)
    scan_row = next(r for r in others["excluded"] if r["name"] == "Scans/")
    assert scan_row["dir"] is True and scan_row["kind"] == "other", scan_row

    # A selection that writes no tracks is still coherent — the families it was
    # asked for land in the album folder and nothing else does (sync mode, which
    # would delete the destination's audio, is refused instead: see below).
    EX_FILES = os.path.join(ROOT, "ExtrasFilesOnly")
    os.makedirs(EX_FILES)
    files_only = exporter.export_tracks(CFG, [ex_track], EX_FILES, codec="copy",
                                        copy_files=["cue", "log"], verify=True)
    assert files_only["failed"] == 0, files_only["errors"]
    assert sorted(os.path.basename(p) for p in listing(EX_FILES)) == \
        ["Album.accurip", "Album.cue", "Album.log"], listing(EX_FILES)
    assert files_only["exported"] == 0, files_only

    # An EMPTY selection is refused with a sentence, before the destination is
    # touched: an export that copies nothing must not write an empty folder and
    # report success. An unknown family is refused the same way.
    EX_NONE = os.path.join(ROOT, "ExtrasNone")
    os.makedirs(EX_NONE)
    for nothing in ([], "", ["audio", "covers"]):
        try:
            exporter.export_tracks(CFG, [ex_track], EX_NONE, codec="copy",
                                   copy_files=nothing)
            raise AssertionError(f"{nothing!r} must be refused")
        except ValueError as e:
            assert "famil" in str(e) or "at least one" in str(e), e
    assert listing(EX_NONE) == [], listing(EX_NONE)
    # …and sync mode (which deletes audio this run did not write) with no tracks
    # in the selection is refused rather than emptying the destination.
    try:
        exporter.export_tracks(CFG, [ex_track], EX_NONE, codec="copy",
                               copy_files=["cover"], prune=True)
        raise AssertionError("sync with no tracks must be refused")
    except ValueError as e:
        assert "sync mode" in str(e), e

    # A PLAYLIST export is a different writer and still writes its .m3u8 (the
    # rule above is about ALBUM exports, and a blunt "never write .m3u8" would
    # have broken this).
    from server import playlists as pl_mod
    _pl_tmp = tempfile.mkdtemp(prefix="mlo_export_pl_")
    _real_db = pl_mod.db_path
    pl_mod.db_path = lambda: os.path.join(_pl_tmp, "playlists.db")
    try:
        pid = pl_mod.create_playlist("Export check")
        pl_mod.add_tracks(pid, [ex_track])
        body = pl_mod.export_m3u8(pid)
        assert body.startswith("#EXTM3U"), body
        assert ex_track.replace("\\", "/") in body, body
    finally:
        pl_mod.db_path = _real_db
        shutil.rmtree(_pl_tmp, ignore_errors=True)

    # ------------------------------------------ the audit still finds the file
    # The other half of the rule: a cue sheet that still names the file the way
    # the RIP spelled it ("1-01 AC/DC.flac") must resolve to the file this app
    # WROTE ("1-01 AC_DC.flac"). Both sides go through naming.name_key, so the
    # app's own renaming never reads as a missing file.
    from mlo.discs import _norm_name
    from mlo.grader import _grade_album
    assert _norm_name("1-01 AC/DC.flac") == _norm_name("1-01 AC_DC.flac")
    assert _norm_name("1-01 AC_DC.flac") == _norm_name("1-01 AC_DC.flac")
    AUD_LIB = os.path.join(ROOT, "AuditLib", "Artists", "Artist One", "Audit Album")
    os.makedirs(AUD_LIB)
    aud_track = make(os.path.join(AUD_LIB, "1-01 AC_DC.flac"), 0.4, 700,
                     album_tags("Audit Album", "1"))

    def _cue(ref):
        with open(os.path.join(AUD_LIB, "Audit Album.cue"), "w",
                  encoding="utf-8") as f:
            f.write(f'FILE "{ref}" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n')

    AUD_CFG = {"music_folder": os.path.join(ROOT, "AuditLib"), "grade_check_cue_files": True}
    _cue("1-01 AC/DC.flac")
    graded = str(_grade_album(AUD_LIB, "EMBEDDED", AUD_CFG))
    assert "CUE references a file the album does not have" not in graded, graded
    # ...and the check is not vacuous: a reference to a file that really is not
    # there is still reported.
    _cue("1-99 Nothing Here.flac")
    graded = str(_grade_album(AUD_LIB, "EMBEDDED", AUD_CFG))
    assert "CUE references a file the album does not have" in graded, graded
    os.remove(os.path.join(AUD_LIB, "Audit Album.cue"))


    # ------------------------------------------------------------ refusal
    try:
        exporter.export_tracks(CFG, [one], LIB, codec="copy")
    except ValueError as e:
        assert "music folder" in str(e)
    else:
        raise AssertionError("exporting into the music folder must be refused")
finally:
    shutil.rmtree(ROOT, ignore_errors=True)

print("ok  export: shipped/custom structures + disc numbers, ID3v2.3, embedded "
      "art, ReplayGain, playlists, the file selection (family by family, "
      "classic sidecar set, empty refused), idempotent re-run, copy-with-art, "
      "prune, library-destination refusal")
