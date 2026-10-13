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

# ------------------------------------------------------------- call sites ----
main_src = open(os.path.join(ROOT, "server", "main.py"), encoding="utf-8").read()
ok("the organizer passes the album's values to every track",
   "shared = shared_album_tags([t[\"tags\"] for t in tracks])" in main_src
   and "album=shared)" in main_src)
grader_src = open(os.path.join(ROOT, "mlo", "grader.py"), encoding="utf-8").read()
ok("the grade compares against the album's own folder",
   "album_shared = shared_album_tags(" in grader_src and "album=album)" in grader_src)

print()
if fails:
    print(f"FAIL — {len(fails)} problem(s): {fails}")
    sys.exit(1)
print(f"PASS — {checks} checks")