#!/usr/bin/env python3
"""Verify the .mlo/downloads manager and the library-layout scanner.

Covers GET /api/downloads + POST /api/downloads/{delete,import} and
GET /api/library/layout, plus the .mlo_expected.json sidecar that makes a
PARTIAL import visible on the album page.

Two throwaway music folders are used, one per concern, so the downloads tests
deleting and moving entries can never change what the layout scanner reports.
The real configured music_folder is never listed, deleted or even opened.
Run: python tools/test_downloads_layout.py  (exit 0 pass, 1 fail)
"""
import atexit
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --------------------------------------------------------------------------- #
# hermeticity: app paths resolve through the music folder the moment they are
# first touched, so the scope is redirected to a temp folder BEFORE
# server.main is imported. No state file is ever created (or migrated) in the
# developer's real music folder or its .mlo/data.
# --------------------------------------------------------------------------- #
REAL_MUSIC_FOLDER = ""
try:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        REAL_MUSIC_FOLDER = str((json.load(f) or {}).get("music_folder") or "")
except Exception:
    pass

REDIRECT = tempfile.mkdtemp(prefix="mlo-dl-redirect-")
os.environ["MLO_MUSIC_FOLDER"] = REDIRECT

import mlo  # noqa: E402
import mlo.config as cfgmod  # noqa: E402
import mlo.paths as pathmod  # noqa: E402

_STUB = os.path.join(REDIRECT, "config.json")
with open(_STUB, "w", encoding="utf-8") as f:
    json.dump({"music_folder": REDIRECT}, f)

for _mod in (cfgmod, pathmod):
    _mod.CONFIG_FILE = _STUB
    if getattr(_mod, "LEGACY_DATA_DIR", None) is not None:
        _mod.LEGACY_DATA_DIR = os.path.join(REDIRECT, "legacy")


def _cleanup_redirect():
    os.environ.pop("MLO_MUSIC_FOLDER", None)
    for d in (REDIRECT, MF, MLF):
        shutil.rmtree(d, ignore_errors=True)


atexit.register(_cleanup_redirect)

from server import main as mlo_main  # noqa: E402
from server import library as mlo_library  # noqa: E402

# --------------------------------------------------------------------------- #
# fixture A: a music folder whose .mlo/downloads (one shared queue, where
# slskd writes) is being triaged
# --------------------------------------------------------------------------- #
MF = tempfile.mkdtemp(prefix="mlo-dl-test-")
DOWNLOADS = os.path.join(MF, ".mlo", "downloads")
ARTISTS = os.path.join(MF, "Artists")

# --------------------------------------------------------------------------- #
# fixture B: a music folder holding every layout problem the scanner reports.
# Deliberately SEPARATE from A: the downloads tests delete and move entries in
# their folder, which would otherwise change what the layout scan counts.
# --------------------------------------------------------------------------- #
MLF = tempfile.mkdtemp(prefix="mlo-layout-test-")
LART = os.path.join(MLF, "Artists")

# safety: this suite must never point at the user's real library
REAL = REAL_MUSIC_FOLDER.replace("\\", "/").rstrip("/")
for _t in (MF, MLF):
    if REAL:
        assert not _t.replace("\\", "/").lower().startswith(REAL.lower()), \
            f"temp fixture {_t} sits inside the real music folder {REAL}"


def write(path, size=8):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * size)
    return path


ALBUM_DIR = "Some Album (2010)"
write(os.path.join(DOWNLOADS, ALBUM_DIR, "01 - a.flac"), 1000)
write(os.path.join(DOWNLOADS, ALBUM_DIR, "02 - b.flac"), 2000)
write(os.path.join(DOWNLOADS, ALBUM_DIR, "cover.jpg"), 500)
write(os.path.join(DOWNLOADS, "loose.flac"), 4000)
write(os.path.join(DOWNLOADS, "notes.txt"), 100)
# slskd's in-flight leftovers must not look like finished results
write(os.path.join(DOWNLOADS, ".incomplete", "half.flac"), 700)
write(os.path.join(DOWNLOADS, "big.flac.part"), 300)

# ---- fixture B: library layout problems ------------------------------------ #
LAYOUT_ALBUM = os.path.join(LART, "Artist", "Album")
write(os.path.join(LAYOUT_ALBUM, "01 - ok.flac"))
write(os.path.join(LAYOUT_ALBUM, "cover.jpg"))
write(os.path.join(LAYOUT_ALBUM, "01 - ok.lrc"))
write(os.path.join(LAYOUT_ALBUM, "notes.txt"))              # stray_file
write(os.path.join(LAYOUT_ALBUM, "Extras", "x.flac"))       # unexpected_subfolder
write(os.path.join(LAYOUT_ALBUM, "CD1", "02 - d2.flac"))    # legit: disc dir
write(os.path.join(LART, "Artist", "Empty", "cover.jpg"))    # empty_album
write(os.path.join(LART, "Artist", "loose-in-artist.flac"))  # audio_in_artist
write(os.path.join(LART, "loose-in-artists.flac"))           # audio_in_artists
write(os.path.join(MLF, "loose-at-root.flac"))               # audio_at_root
write(os.path.join(MLF, "Random Rips", "song.flac"))         # unexpected_folder
write(os.path.join(MLF, ".mlo_manifest.json"), 4)            # legacy_state_file


def use(music_folder):
    """Point the app at one fixture folder for the next calls."""
    mlo_main.load_config = lambda: {"music_folder": music_folder}


use(MF)

# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #
FAILED = []


def check(label, fn):
    """Run one labeled check; a failed assertion is reported, not fatal."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - report and keep checking
        FAILED.append(label)
        print(f"FAIL {label}: {type(e).__name__}: {e}")
    else:
        print(f"ok   {label}")


# ---- downloads listing ----------------------------------------------------- #
def test_downloads_listing():
    res = mlo_main.downloads_list()
    assert res["exists"] is True, res
    assert res["folder"] == DOWNLOADS.replace("\\", "/"), res["folder"]
    assert res["music_folder"] == MF.replace("\\", "/"), res["music_folder"]
    names = {e["name"] for e in res["entries"]}
    assert names == {ALBUM_DIR, "loose.flac", "notes.txt", ".incomplete",
                     "big.flac.part"}, names
    assert res["count"] == 5, res["count"]
    assert res["bytes"] == sum(e["bytes"] for e in res["entries"]), res["bytes"]

    by = {e["name"]: e for e in res["entries"]}
    album = by[ALBUM_DIR]
    assert album["dir"] is True and album["album"] is True, album
    assert album["audio"] == 2 and album["images"] == 1, album
    assert album["files"] == 3, album
    # a loose audio FILE is not an album (nothing to move into the library)
    assert by["loose.flac"]["album"] is False, by["loose.flac"]
    assert by["loose.flac"]["audio"] == 1, by["loose.flac"]
    assert by["notes.txt"]["album"] is False and by["notes.txt"]["audio"] == 0, by["notes.txt"]
    # in-flight leftovers are flagged, never presented as results
    assert by[".incomplete"]["partial"] is True, by[".incomplete"]
    assert by["big.flac.part"]["partial"] is True, by["big.flac.part"]
    assert album["partial"] is False, album


def test_downloads_name_guard():
    """Names travel as basenames: anything that could resolve outside the
    downloads dir is refused before a byte is touched."""
    root = os.path.realpath(DOWNLOADS)
    for bad in ("", ".", "..", "../escape", "a/b", "..\\escape", "sub/../../x"):
        assert mlo_main._downloads_name_error(bad, root), f"accepted {bad!r}"
    assert mlo_main._downloads_name_error(ALBUM_DIR, root) is None
    assert mlo_main._downloads_name_error("loose.flac", root) is None

    # and the delete route enforces it rather than trusting the caller
    outside = os.path.join(MF, "keep-me.flac")
    write(outside, 10)
    res = mlo_main.downloads_delete(mlo_main.DownloadsDelete(names=["../keep-me.flac"]))
    assert res["deleted"] == [], res
    assert res["failed"], res
    assert "not a valid entry name" in res["failed"][0]["error"], res
    assert os.path.exists(outside), "escape target was touched"
    assert os.path.getsize(outside) == 10, "escape target was modified"


def test_downloads_delete():
    res = mlo_main.downloads_delete(mlo_main.DownloadsDelete(names=["loose.flac", "nope.flac"]))
    assert res["deleted"] == ["loose.flac"], res
    assert res["freed"] == 4000, res["freed"]
    assert [f["name"] for f in res["failed"]] == ["nope.flac"], res
    assert not os.path.exists(os.path.join(DOWNLOADS, "loose.flac"))


def test_downloads_import():
    res = mlo_main.downloads_import(mlo_main.DownloadsImport(names=[ALBUM_DIR]))
    assert res["failed"] == [], res
    assert [m["name"] for m in res["moved"]] == [ALBUM_DIR], res
    dest = os.path.join(ARTISTS, ALBUM_DIR)
    assert os.path.isdir(dest), dest
    assert os.path.exists(os.path.join(dest, "01 - a.flac")), os.listdir(dest)
    assert not os.path.exists(os.path.join(DOWNLOADS, ALBUM_DIR)), "source left behind"

    # an entry that is gone lands in `failed`, never a 500 for the whole batch
    res2 = mlo_main.downloads_import(mlo_main.DownloadsImport(names=[ALBUM_DIR]))
    assert res2["moved"] == [] and res2["failed"], res2


# ---- library layout -------------------------------------------------------- #
def test_layout_report():
    use(MLF)
    res = mlo_main.library_layout()
    assert res["exists"] is True, res
    assert res["folder"] == MLF.replace("\\", "/"), res["folder"]
    assert res["artists_dir"] == LART.replace("\\", "/"), res["artists_dir"]

    counts = res["counts"]
    # exactly one finding per problem planted in fixture B
    assert counts.get("audio_at_root") == 1, counts
    assert counts.get("unexpected_folder") == 1, counts
    assert counts.get("audio_in_artists") == 1, counts
    assert counts.get("audio_in_artist") == 1, counts
    assert counts.get("empty_album") == 1, counts
    assert counts.get("stray_file") == 1, counts
    assert counts.get("unexpected_subfolder") == 1, counts
    assert counts.get("legacy_state_file") == 1, counts
    assert res["total"] == sum(counts.values()), res
    # only "Album" and "Empty" are album folders; the CD1 disc dir inside
    # "Album" is legitimate and must never be reported
    assert res["albums"] == 2, res["albums"]
    kinds_paths = {(i["kind"], i["path"]) for i in res["issues"]}
    assert ("audio_at_root", "loose-at-root.flac") in kinds_paths, kinds_paths
    assert not any("cd1" in p.lower() for _k, p in kinds_paths), kinds_paths

    # paths in the report are music-folder-relative, never absolute
    assert all(not os.path.isabs(i["path"]) for i in res["issues"]), res["issues"]
    # every finding carries actionable text
    assert all(i["detail"] and i["hint"] for i in res["issues"]), res["issues"]


def test_layout_read_only():
    """The scanner must never move or delete anything."""
    use(MLF)
    before = sorted(
        os.path.join(r, f).replace(MLF, "")
        for r, _d, fs in os.walk(MLF) for f in fs
    )
    mlo_main.library_layout()
    after = sorted(
        os.path.join(r, f).replace(MLF, "")
        for r, _d, fs in os.walk(MLF) for f in fs
    )
    assert before == after, (set(before) ^ set(after))


# ---- expected-tracklist sidecar (partial imports) -------------------------- #
def test_expected_tracks_roundtrip():
    album = LAYOUT_ALBUM
    rows = [
        {"disc": 1, "position": 1, "title": "One", "recording_mbid": "mb-1"},
        {"disc": 1, "position": 2, "title": "Two"},
        {"disc": 1, "position": 3, "title": "Three"},
        {"disc": 1, "position": 0, "title": "junk"},      # dropped: no position
        "not-a-dict",                                     # dropped: wrong type
    ]
    assert pathmod.save_expected_tracks(album, "rel-1", rows) is True
    got = pathmod.load_expected_tracks(album)
    assert got["release_id"] == "rel-1", got
    assert [t["position"] for t in got["tracks"]] == [1, 2, 3], got
    assert got["tracks"][0]["recording_mbid"] == "mb-1", got

    # the manifest is a dotfile, so grading never reads it as library content
    assert pathmod.EXPECTED_TRACKS_FILE.startswith("."), pathmod.EXPECTED_TRACKS_FILE

    # clearing it removes the file, leaving a full album with no manifest
    assert pathmod.save_expected_tracks(album, None, []) is True
    assert not os.path.exists(os.path.join(album, pathmod.EXPECTED_TRACKS_FILE))
    assert pathmod.load_expected_tracks(album)["tracks"] == []
    # a garbled manifest reads as empty rather than raising
    with open(os.path.join(album, pathmod.EXPECTED_TRACKS_FILE), "w", encoding="utf-8") as f:
        f.write("{ not json")
    assert pathmod.load_expected_tracks(album) == {"release_id": None, "tracks": []}
    os.remove(os.path.join(album, pathmod.EXPECTED_TRACKS_FILE))


def test_partial_flags():
    """_add_expected_tracks diffs the release tracklist against the files on
    disk: the absent ones are exactly what the album page greys out."""
    album = LAYOUT_ALBUM
    pathmod.save_expected_tracks(album, "rel-1", [
        {"disc": 1, "position": 1, "title": "One"},
        {"disc": 1, "position": 2, "title": "Two"},
        {"disc": 1, "position": 9, "title": "Never imported"},
    ])
    res = {
        "tracks": [
            {"discnumber": 1, "tracknumber": 1},
            {"discnumber": 1, "tracknumber": 2},
        ]
    }
    mlo_library._add_expected_tracks(res, album)
    assert res["partial"] is True, res
    assert [t["missing"] for t in res["expected_tracks"]] == [False, False, True], \
        res["expected_tracks"]
    assert res["expected_release_id"] == "rel-1", res

    # a full album is not partial
    pathmod.save_expected_tracks(album, "rel-1", [{"disc": 1, "position": 1, "title": "One"}])
    full = {"tracks": [{"discnumber": 1, "tracknumber": 1}]}
    mlo_library._add_expected_tracks(full, album)
    assert full["partial"] is False, full

    # an album with no manifest is untouched: defaults, no findings
    pathmod.save_expected_tracks(album, None, [])
    none = {"tracks": [{"discnumber": 1, "tracknumber": 1}]}
    mlo_library._add_expected_tracks(none, album)
    assert none["partial"] is False and none["expected_tracks"] == [], none

    # untagged files still line up: the library derives (disc, track) from the
    # file name, and so does the diff
    pathmod.save_expected_tracks(album, "rel-1", [{"disc": 1, "position": 4, "title": "Four"}])
    derived = {"tracks": [{"discnumber": None, "tracknumber": 4}]}
    mlo_library._add_expected_tracks(derived, album)
    assert derived["partial"] is False, derived
    pathmod.save_expected_tracks(album, None, [])


check("downloads listing shape + album/partial flags", test_downloads_listing)
check("downloads name guard refuses traversal", test_downloads_name_guard)
check("downloads delete frees bytes, reports misses", test_downloads_delete)
check("downloads import moves into the library", test_downloads_import)
check("layout scanner reports every category", test_layout_report)
check("layout scanner is read-only", test_layout_read_only)
check("expected-tracklist sidecar round trip", test_expected_tracks_roundtrip)
check("partial-import flags match the files on disk", test_partial_flags)

print()
if FAILED:
    print(f"{len(FAILED)} check(s) FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all checks passed")
