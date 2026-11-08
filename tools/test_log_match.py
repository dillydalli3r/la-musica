#!/usr/bin/env python3
"""Rip-log matching: one release for a multi-disc set, ranked by evidence.

Pinned here:

  * a folder whose discs sit in CD1/CD2 subfolders is matched as ONE release
    with both medium positions covered — not as two unrelated albums;
  * the same holds when the two logs sit flat in one folder (CD1.log/CD2.log);
  * a release whose track count/durations/tags agree with the logs outranks
    another release MusicBrainz attaches to the same Disc ID;
  * a log's own filename states its disc ("CD2.log"), and "Album (CD1)" /
    "Album [Disc 2]" are disc folders even though the token is decorated;
  * no log / no TOC is an honest empty answer, never a guess.

No network: `releases_for_disc_id` is stubbed with the payload shape
MusicBrainz's /ws/2/discid lookup answers.

Run:  python tools/test_log_match.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mlo.discs import disc_dir_number, disc_id_for_log  # noqa: E402
from server import imports  # noqa: E402
from server import integrations  # noqa: E402

FAILS = []


def ok(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILS.append(label)


def eac_log(rows):
    """An EAC-style log with a TOC table. `rows` = [(start, length), ...]."""
    lines = [
        "Exact Audio Copy V1.6 from 23. October 2020",
        "",
        "TOC of the extracted CD",
        "     Track |   Start  |  Length  | Start sector | End sector",
        "    ---------------------------------------------------------",
    ]
    for i, (start, length) in enumerate(rows, 1):
        lines.append(f"        {i}  | {start} | {length} |          0    |       0")
    lines += ["", "Range status and errors", ""]
    return "\n".join(lines)


TOC1 = [("00:00:00", "03:00:00"), ("03:00:00", "04:00:00")]
TOC2 = [("00:00:00", "02:30:00"), ("02:30:00", "05:00:00")]
LOG1 = eac_log(TOC1)
LOG2 = eac_log(TOC2)


def release(rid, title, artist, date, position, discs, tracks, status="Official"):
    return {
        "release_id": rid,
        "release_group_id": f"rg-{rid}",
        "release_group_title": title,
        "title": title,
        "artist": artist,
        "artist_mbid": "artist-1",
        "date": date,
        "country": "GB",
        "status": status,
        "track_count": len(tracks),
        "disc_position": position,
        "discs": discs,
        "format": "CD",
        "matches_this_disc": True,
        "tracks": [{"position": i + 1, "title": t, "length": ms}
                   for i, (t, ms) in enumerate(tracks)],
    }


WRONG = release("rel-wrong", "Another Album", "Someone Else", "2005",
                1, 2, [("x", 100000), ("y", 100000)], status="Bootleg")
RIGHT_D1 = release("rel-right", "The Album", "The Artist", "1999",
                   1, 2, [("one", 180000), ("two", 240000)])
RIGHT_D2 = release("rel-right", "The Album", "The Artist", "1999",
                   2, 2, [("three", 150000), ("four", 300000)])
OTHER_D2 = release("rel-other", "Other Set", "The Artist", "1999",
                   2, 2, [("three", 150000), ("four", 300000)])

TAGS = {"ALBUM": "The Album", "ALBUMARTIST": "The Artist", "DATE": "1999"}

TMP = tempfile.mkdtemp(prefix="mlo-log-match-")


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


# The real TOC parser hashes the two tables into two distinct Disc IDs; the
# stub answers each with the release rows MusicBrainz would attach.
FLAT = os.path.join(TMP, "flat")
log1 = write(os.path.join(FLAT, "CD1.log"), LOG1)
log2 = write(os.path.join(FLAT, "CD2.log"), LOG2)
id1, _ = disc_id_for_log(log1)
id2, _ = disc_id_for_log(log2)
ok(bool(id1 and id2 and id1 != id2), f"the two TOCs hash to distinct Disc IDs ({id1!r}/{id2!r})")

BY_DISC = {
    id1: [WRONG, RIGHT_D1],
    id2: [RIGHT_D2, OTHER_D2],
}
real_releases = integrations.releases_for_disc_id
integrations.releases_for_disc_id = lambda disc_id: list(BY_DISC.get(disc_id, []))

try:
    print("== two flat logs are ONE release with both discs covered ==")
    got = imports.log_match(FLAT, tags=TAGS)
    best = got.get("best")
    ok(got["ok"] and best is not None, f"the matcher answers a best release ({got.get('reason')!r})")
    ok(best["release"]["release_id"] == "rel-right",
       f"the release whose durations/tags agree wins ({best['release']['release_id']})")
    ok(best["complete"] is True and best["discs_covered"] == [1, 2],
       f"both medium positions are covered ({best.get('discs_covered')})")
    ok(best["confident"] is True, "a complete two-log match is confident")
    ok(len(best["logs"]) == 2, f"both logs are booked to the group ({len(best['logs'])})")
    ok(best["release"]["release_id"] != "rel-wrong",
       "the bootleg with mismatched durations does not win")

    print("== CD1/CD2 subfolders match the same way ==")
    SUB = os.path.join(TMP, "sub")
    write(os.path.join(SUB, "CD1", "CD1.log"), LOG1)
    write(os.path.join(SUB, "CD2", "CD2.log"), LOG2)
    sub = imports.log_match(SUB, tags=TAGS)
    ok(sub["best"] and sub["best"]["release"]["release_id"] == "rel-right",
       f"the disc-subfolder layout groups to one release ({sub['best'] and sub['best']['release']['release_id']})")
    ok(sub["best"]["complete"] is True, "…and the group is complete")

    print("== a log with no TOC is not guessed ==")
    EMPTY = os.path.join(TMP, "empty")
    write(os.path.join(EMPTY, "CD1.log"), "Exact Audio Copy\nno table here\n")
    empty = imports.log_match(EMPTY, tags=TAGS)
    ok(empty["ok"] and empty["best"] is None,
       f"a log with no TOC answers no release ({empty.get('reason')!r})")
    ok(empty["logs"][0]["disc_id"] == "", "…and its disc id is empty")

    print("== no log at all ==")
    NONE = os.path.join(TMP, "none")
    os.makedirs(NONE, exist_ok=True)
    none = imports.log_match(NONE, tags=TAGS)
    ok(not none["ok"] and none["best"] is None and "no rip log" in none["reason"],
       f"a folder without a log says so ({none.get('reason')!r})")
finally:
    integrations.releases_for_disc_id = real_releases

print("== the source folder's disc is read before a flat move ==")
DISC = os.path.join(TMP, "disc-map")
os.makedirs(os.path.join(DISC, "Album CD1"), exist_ok=True)
write(os.path.join(DISC, "Album CD1", "01.flac"), "not audio — the map reads names only")
write(os.path.join(DISC, "Album CD1", "02.flac"), "not audio")
got_map = imports.disc_map(os.path.join(DISC, "Album CD1"))
ok(got_map == {"01.flac": 1, "02.flac": 1},
   f"every file in Album CD1 maps to disc 1 ({got_map})")
os.makedirs(os.path.join(DISC, "Album (CD2)"), exist_ok=True)
write(os.path.join(DISC, "Album (CD2)", "01.flac"), "not audio")
ok(imports.disc_map(os.path.join(DISC, "Album (CD2)")) == {"01.flac": 2},
   "a decorated disc folder maps to disc 2")

print("== the disc vocabulary reads decorated disc folders ==")
ok(disc_dir_number("CD1") == 1, "CD1 is disc 1")
ok(disc_dir_number("Disc 2") == 2, "Disc 2 is disc 2")
ok(disc_dir_number("Album (CD1)") == 1, "Album (CD1) states disc 1")
ok(disc_dir_number("Album [Disc 2]") == 2, "Album [Disc 2] states disc 2")
ok(disc_dir_number("Album (1994)") is None, "a year is not a disc")
ok(disc_dir_number("Extras") is None, "a bonus folder is not a disc")

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s)")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("all log-match checks passed")
