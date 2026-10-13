#!/usr/bin/env python3
"""One album, ONE folder — the album-level tags name the release, not a file.

The owner's report (2026-10-13): *"sometimes they can split into different
albums / releases in the library"*. Measured on this repo's own fixture
(`tools/make_test_library.py`): the album's first track states
`MEDIA=Digital Media` while its neighbours state nothing, and BOTH the beets
path plugin and the organizer evaluated the naming script from each FILE's own
tags — so one album was written as two library albums,
`… - First Album {Digital Media}` beside `… - First Album`.

The rule this pins (`mlo.naming.ALBUM_LEVEL_TAGS` / `shared_album_tags`): the
folder a naming script produces is a fact about the RELEASE, so the album-level
tokens come from ONE set of values per album — the one MOST of its files state,
"" when none does — everywhere the folder is named or verified: the organizer
(`server.main.organize_albums`), beets' `%mlo_dir` (`server/beets/mloplugin.py`)
and the grade's PATH check (`mlo.grader._naming_mismatch`).

Run:  python tools/test_naming_album_tags.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mlo.naming import (ALBUM_LEVEL_TAGS, DEFAULT_NAMING_SCRIPT, eval_script,  # noqa: E402
                        shared_album_tags, track_variables)

checks = 0
fails = []


def ok(label, cond, detail=""):
    global checks
    checks += 1
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}  {detail}")
        fails.append(label)


def dir_of(tags, album=None):
    """The album FOLDER a naming script produces for one track's tags."""
    v = track_variables(tags, album=album)
    return "/".join(eval_script(DEFAULT_NAMING_SCRIPT, v).split("/")[:-1])


# ---------------------------------------------------------------- the rule ---
TAGS = [
    # The owner's own case: the first file states a medium, the rest state none.
    {"ALBUM": "First Album", "ALBUMARTIST": "Artist Alpha", "TITLE": "Intro",
     "TRACKNUMBER": "1", "DATE": "2020-01-01", "MEDIA": "Digital Media"},
    {"ALBUM": "First Album", "ALBUMARTIST": "Artist Alpha", "TITLE": "Second",
     "TRACKNUMBER": "2", "DATE": "2020-01-01"},
    {"ALBUM": "First Album", "ALBUMARTIST": "Artist Alpha", "TITLE": "Bonus",
     "TRACKNUMBER": "3", "DATE": "2020-01-01"},
]
shared = shared_album_tags(TAGS)
ok("every album-level name is answered, \"\" included",
   set(ALBUM_LEVEL_TAGS) <= set(shared), sorted(set(ALBUM_LEVEL_TAGS) - set(shared)))
ok("the value most of the files state wins (1 of 3 states a medium → kept)",
   shared.get("MEDIA") == "Digital Media", shared.get("MEDIA"))
ok("a name no file states answers empty",
   shared.get("CATALOGNUMBER") == "", shared.get("CATALOGNUMBER"))

# A majority beats a minority, and a tie goes to the earliest file that says
# something — so the answer never depends on the order the files were read in.
wins = shared_album_tags([{"MEDIA": "CD"}, {"MEDIA": "CD"}, {"MEDIA": "Vinyl"}])
ok("the majority's value wins", wins["MEDIA"] == "CD", wins["MEDIA"])
tie = shared_album_tags([{"MEDIA": "CD"}, {"MEDIA": "Vinyl"}])
ok("a tie goes to the earliest file that states one", tie["MEDIA"] == "CD", tie["MEDIA"])
none_ = shared_album_tags([{}, {"ALBUM": "X"}])
ok("a name nothing states is \"\", never one file's value", none_["MEDIA"] == "")
ok("a list value is joined the way the tag layer joins one",
   shared_album_tags([{"LABEL": ["A", "B"]}])["LABEL"] == "A; B",
   shared_album_tags([{"LABEL": ["A", "B"]}])["LABEL"])

# ------------------------------------------------- one album, ONE folder ---
folders_plain = {dir_of(t) for t in TAGS}
folders_shared = {dir_of(t, album=shared) for t in TAGS}
ok("without the rule the album names TWO folders (the bug)",
   len(folders_plain) == 2, sorted(folders_plain))
ok("with the album's own values it names ONE",
   len(folders_shared) == 1, sorted(folders_shared))
ok("…and the folder carries the album's medium, not the silent minority's",
   "{Digital Media}" in next(iter(folders_shared)), next(iter(folders_shared)))

# The file name still answers for its own track: the album's values must not
# flatten the per-track tokens.
def file_of(tags, album=None):
    v = track_variables(tags, album=album)
    return eval_script(DEFAULT_NAMING_SCRIPT, v).split("/")[-1]

ok("per-track tokens stay the file's own (the file names differ)",
   file_of(TAGS[0], album=shared) != file_of(TAGS[1], album=shared)
   and file_of(TAGS[1], album=shared).startswith("1-02"),
   [file_of(t, album=shared) for t in TAGS])
ok("a caller that passes no album keeps the file's own tags (old behaviour)",
   dir_of(TAGS[0]) != dir_of(TAGS[1]))

# --------------------------------------------------------- the plugin side ---
# server/beets/mloplugin.py builds the naming variables from beets' item fields
# and overrides the album-level ones from `_album_shared_tags`. Every name in
# the rule must be covered there, or a tag added to ALBUM_LEVEL_TAGS would
# silently stop overriding in the one mover that runs first.
src = open(os.path.join(ROOT, "server", "beets", "mloplugin.py"), encoding="utf-8").read()
m = re.search(r"_ALBUM_SHARED_VARS = \{(.*?)\n\}", src, re.S)
ok("the plugin declares its album-level override table", bool(m))
covered = set(re.findall(r'"([A-Z_]+)":', m.group(1))) if m else set()
missing = [name for name in ALBUM_LEVEL_TAGS if name not in covered]
ok("every ALBUM_LEVEL_TAG is covered by the plugin's table",
   not missing, f"missing={missing}")
ok("the plugin reads the shared values through the ONE rule",
   "shared_album_tags" in src and "def stash_album_tags" in src
   and "_item_album_tag_values" in src)
ok("…from the ITEMS beets is importing, not from the files on disk",
   "album_tags_for_item(item)" in src and "def on_import_task_start" in src)
_plugin_src = open(os.path.join(ROOT, "server", "beets", "mloplugin.py"),
                   encoding="utf-8").read().replace("\r\n", "\n")
ok("…and it asks the album folder too (the framework album's own record)",
   "_item_album_tag_values(it) for it in items],\n                               folder)"
   in _plugin_src)

# ------------------------------------------------------------- call sites ----
main_src = open(os.path.join(ROOT, "server", "main.py"), encoding="utf-8").read()
ok("the organizer passes the album's values to every track",
   "shared = shared_album_tags([t[\"tags\"] for t in tracks], p)" in main_src
   and "album=shared)" in main_src)
grader_src = open(os.path.join(ROOT, "mlo", "grader.py"), encoding="utf-8").read()
plugin_src = open(os.path.join(ROOT, "server", "beets", "mloplugin.py"),
                  encoding="utf-8").read()
ok("the grade compares against the album's own folder",
   "album_shared = shared_album_tags(" in grader_src and "album=album)" in grader_src)
ok("...through the SAME rule, folder and all",
   "shared_album_tags(\n" in grader_src and "naming_dir)" in grader_src)

# ------------------------------------------- the album the app ADDED ---------
# The owner's report, 2026-10-13: "Cracker Island by Gorillaz imported weirdly.
# It made two folders … the latter of which was tagged correctly, but only has
# one track in it. The former has all the tracks, but isn't imported right."
# The wish's framework album is named from the RELEASE; the auto-import's
# identity stamp can miss a file (locked, read-only — its verdict is per file
# and was silent), and the organizer then named the album from the FILES' thin
# tags: the folder the ADD created was renamed to "<artist>/<year> - <album>
# {<medium>}", the artist lost its id folder, and the file the stamp HAD
# reached was left behind in the old name.
import json
import shutil
import tempfile

from mlo.paths import PENDING_FILE  # noqa: E402

# Short ids on purpose: the album folder below is the naming script's own
# output, and a real UUID pair pushes the scratch path past Windows' 260
# characters (the app carries a long-path prefix for that, a test does not).
RELEASE_ID = "rel00001"
GROUP_ID = "grp00001"
ARTIST_ID = "art00001"
ALBUM = "CI"
ARTIST = "Band"
FOLDER = (f"{ARTIST} [{ARTIST_ID}]"
          f"/[Album] 2023-02-24 - 2023-02-24 - {ALBUM} "
          f"{{JP - CD - P - 1}} [{RELEASE_ID}] [{GROUP_ID}]")
MARKER = {
    "pending": True, "release_id": RELEASE_ID, "release_group_id": GROUP_ID,
    "title": ALBUM, "artist": ARTIST, "artist_id": ARTIST_ID,
    "year": "2023", "date": "2023-02-24", "release_type": "Album",
    "wish_id": 7,
    "release": {"country": "JP", "countries": [{"code": "JP"}],
                "media": ["CD"], "catalog_number": "1",
                "label": "P", "status": "Official",
                "date": "2023-02-24", "originaldate": "2023-02-24"},
    "waiting_for": "the audio for this release",
}


def _thin_mp3(path, title, number):
    """A real, parseable file whose tags state NO release identity — what a
    download whose stamp missed the write leaves behind."""
    frame = bytes.fromhex("ff fb 10 00") + b"\x00" * 100
    with open(path, "wb") as f:
        f.write(frame * 60)
    from mutagen.id3 import ID3, TALB, TPE1, TPE2, TDRC, TRCK, TIT2, TXXX
    tags = ID3()
    tags.add(TIT2(encoding=3, text=title))
    tags.add(TPE1(encoding=3, text=ARTIST))
    tags.add(TPE2(encoding=3, text=ARTIST))
    tags.add(TALB(encoding=3, text=ALBUM))
    tags.add(TDRC(encoding=3, text="2023"))
    tags.add(TRCK(encoding=3, text=str(number)))
    tags.add(TXXX(encoding=3, desc="MEDIA", text="CD"))
    tags.save(path)


def _organize_scratch(with_marker):
    """A scratch library holding the album above, organized for real."""
    from server.main import OrganizeRequest, organize_albums

    root = tempfile.mkdtemp(prefix="mlo-naming-marker-")
    old_env = os.environ.get("MLO_MUSIC_FOLDER")
    os.environ["MLO_MUSIC_FOLDER"] = root
    try:
        from mlo.config import load_config, save_config
        cfg = load_config()
        cfg["music_folder"] = root
        cfg["first_run_done"] = True
        cfg["cover_auto_fetch"] = False
        save_config(cfg)
        album = os.path.join(root, "Artists", *FOLDER.split("/"))
        os.makedirs(album, exist_ok=True)
        for n, title in ((1, "Cracker Island"), (2, "Oil")):
            _thin_mp3(os.path.join(album, f"{n:02d} - {title}.mp3"),
                      title, n)
        if with_marker:
            with open(os.path.join(album, PENDING_FILE), "w", encoding="utf-8") as f:
                json.dump(MARKER, f)
        res = organize_albums(OrganizeRequest(paths=[album], dry_run=False))
        return root, album, res["results"][0]
    finally:
        if old_env is None:
            os.environ.pop("MLO_MUSIC_FOLDER", None)
        else:
            os.environ["MLO_MUSIC_FOLDER"] = old_env


def _folders(root):
    out = []
    for dirpath, dirs, _files in os.walk(os.path.join(root, "Artists")):
        for d in dirs:
            out.append(os.path.relpath(os.path.join(dirpath, d), root)
                       .replace(os.sep, "/"))
    return sorted(out)


# the rule in the library layer, on its own (no server, no config)
from mlo.naming import marker_album_tags, shared_album_tags  # noqa: E402

_mroot = tempfile.mkdtemp(prefix="mlon-")
try:
    with open(os.path.join(_mroot, PENDING_FILE), "w", encoding="utf-8") as f:
        json.dump(MARKER, f)
    _thin = [{"ALBUM": ALBUM, "ALBUMARTIST": ARTIST, "DATE": "2023",
              "MEDIA": "CD"}]
    _plain = shared_album_tags(_thin)
    _filled = shared_album_tags(_thin, _mroot)
    _own = shared_album_tags(
        [dict(_thin[0], MUSICBRAINZ_ALBUMID="someone-elses", DATE="2019")],
        _mroot)
    ok("without a folder the rule answers only what the files state",
       _plain.get("MUSICBRAINZ_ALBUMID") == "" and _plain.get("DATE") == "2023")
    ok("a framework folder's marker fills the names the files leave empty",
       _filled.get("MUSICBRAINZ_ALBUMID") == RELEASE_ID
       and _filled.get("MUSICBRAINZ_ALBUMARTISTID") == ARTIST_ID
       and _filled.get("ORIGINALDATE") == "2023-02-24",
       str(_filled))
    ok("...and IS the album's own answer where the files name no release",
       _filled.get("DATE") == "2023-02-24"
       and _filled.get("RELEASETYPE") == "Album"
       and _filled.get("CATALOGNUMBER") == "1", str(_filled))
    ok("a file that states a release of its own WINS (a re-point)",
       _own.get("MUSICBRAINZ_ALBUMID") == "someone-elses"
       and _own.get("DATE") == "2019" and _own.get("ALBUM") == ALBUM,
       str(_own))
    ok("a folder with no marker answers nothing",
       marker_album_tags(tempfile.mkdtemp(prefix="mlon-plain-")) == {})
finally:
    shutil.rmtree(_mroot, ignore_errors=True)
try:
    root_a, album_a, res_a = _organize_scratch(with_marker=True)
    folders_a = _folders(root_a)
    files_a = os.listdir(album_a)
    shutil.rmtree(root_a, ignore_errors=True)
except Exception as e:      # pragma: no cover - a broken scratch run must show
    print(f"  FAIL the framework-album organize ran  {e!r}")
    fails.append("framework organize")
    folders_a, files_a, res_a = [], [], {}
else:
    ok("a framework album keeps the folder the ADD created",
       os.path.normcase(res_a.get("album_root", "").replace("/", os.sep))
       == os.path.normcase(album_a)
       and folders_a == ["Artists/" + FOLDER.split("/")[0], "Artists/" + FOLDER],
       f"album_root={res_a.get('album_root')!r} folders={folders_a}")
    ok("...and never grows the thin second folder",
       not any("2023 - CI" in f for f in folders_a), folders_a)
    ok("...with every file still inside it",
       sum(1 for f in files_a if f.endswith(".mp3")) == 2, files_a)
    ok("its own manifest and marker stay",
       PENDING_FILE in files_a, files_a)

try:
    root_b, album_b, res_b = _organize_scratch(with_marker=False)
    folders_b = _folders(root_b)
    shutil.rmtree(root_b, ignore_errors=True)
except Exception as e:      # pragma: no cover
    print(f"  FAIL the control organize ran  {e!r}")
    fails.append("control organize")
    folders_b, res_b = [], {}
else:
    ok("the CONTROL: without a marker the same album is still named from "
       "its thin tags",
       any("2023 - CI {CD}" in f for f in folders_b), folders_b)

# the record itself: the marker has to carry what the name needs
ok("the marker keeps the artist id and the release group date",
   "\"artist_id\"" in open(os.path.join(ROOT, "server", "pending_albums.py"),
                                encoding="utf-8").read()
   and "\"originaldate\"" in open(os.path.join(ROOT, "server", "pending_albums.py"),
                                     encoding="utf-8").read())
ok("the organizer asks the marker for the album's own identity",
   "marker_album_tags(p)" in main_src and "marker = marker_album_tags(p)" in main_src)
print()
if fails:
    print(f"FAIL — {len(fails)} problem(s): {fails}")
    sys.exit(1)
print(f"PASS — {checks} checks")