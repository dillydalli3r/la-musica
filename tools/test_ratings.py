#!/usr/bin/env python3
"""Ratings: half-star precision, rename healing, RATING-tag interop.

Everything runs against a throwaway database and throwaway files under the
system temp dir. The container writer AND the cached tag reader are stubbed
through ONE in-memory tag store, and the library index and the config are
stubbed too, so no audio file is read or written, no real state db is touched
and nothing goes to the network.

Run: python tools/test_ratings.py   (exit 0 pass, 1 fail)
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import mlo                                            # noqa: E402
from mlo.config import DEFAULT_CONFIG                 # noqa: E402
from mlo.config import should_write_audio_tag         # noqa: E402
from mlo.grader import TAG_ALLOWLIST, tag_key_allowed  # noqa: E402
from server import mbresolve                          # noqa: E402
from server import ratings                            # noqa: E402
from server import tagcache                           # noqa: E402

fails = []


def check(ok, label, extra=""):
    if not ok:
        fails.append(f"{label}{(': ' + str(extra)) if extra else ''}")


tmp = tempfile.mkdtemp(prefix="mlo_ratings_")
ratings.db_path = lambda: os.path.join(tmp, "ratings.db")

# ── fixtures: one in-memory tag store behind both the writer and the reader ──
TAGS = {}      # normcased path -> {tag: value}
FAIL = set()   # normcased paths whose container refuses a write
CALLS = []     # ("set"|"delete", path, value) — what actually reached the file


def key(path):
    """The tag store's key: the tag cache keys by the normalized path, and the
    API hands forward-slashed ones, so the fake must fold them the same way."""
    return os.path.normcase(os.path.normpath(str(path)))


def make_file(name):
    """A real (empty) file, so os.path.isfile answers like the app's do."""
    p = os.path.join(tmp, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"\0")
    return p


class FakeAudio:
    """Stand-in for mlo.audio.AudioFile over TAGS."""

    def __init__(self, path):
        self.path = path
        self.key = key(path)
        self.error = None
        self.audio = object()  # "the container loaded"

    def get_tag(self, name):
        return TAGS.get(self.key, {}).get(name)

    def set_tag(self, name, value):
        if self.key in FAIL:
            self.error = "read-only file"
            return False
        TAGS.setdefault(self.key, {})[name] = value
        CALLS.append(("set", self.path, value))
        return True

    def delete_tag(self, name):
        if self.key in FAIL:
            self.error = "read-only file"
            return False
        TAGS.get(self.key, {}).pop(name, None)
        CALLS.append(("delete", self.path, None))
        return True

    def defer_save(self, on=True):
        if self.key in FAIL:
            self.error = "read-only file"
            return False
        return True

    def tag_values(self, name):
        val = TAGS.get(self.key, {}).get(name)
        return [val] if val not in (None, "") else []

    def all_tags(self):
        return dict(TAGS.get(self.key, {}))


def fake_read_track(path, tag_list=None):
    tags = TAGS.get(key(path), {})
    if tag_list is not None:
        tags = {t: tags.get(t) for t in tag_list}
    return dict(tags), {}


import mlo.audio                                       # noqa: E402
mlo.audio.AudioFile = FakeAudio
tagcache.read_track = fake_read_track
mlo.load_config = lambda: {"write_rating_tags": True}

INDEX = {"tracks": {}, "tracks_bypath": {}, "albums": {}, "artists": {}}
mbresolve.get_index = lambda cfg=None, force=False: INDEX
mbresolve.track_mbid_for = lambda path: INDEX["tracks_bypath"].get(
    str(path).replace("\\", "/"), "")

from server import api_ratings                         # noqa: E402
from fastapi import HTTPException                      # noqa: E402

api_ratings.load_config = lambda: {"write_rating_tags": True}

CFG = {"write_rating_tags": True}
a = make_file("Album/01 - One.flac")
b = make_file("Album/02 - Two.flac")
api_a = a.replace("\\", "/")

# ── 1. set / get / clear round-trips ────────────────────────────────────────
check(ratings.map_for() == {}, "nothing rated on a fresh database", ratings.map_for())
check(ratings.counts() == {str(i): 0 for i in range(1, 11)},
      "every bucket 1-10 is present and empty", ratings.counts())

check(ratings.set_rating(a, 7) == 7, "set_rating stores 7 half-stars")
check(ratings.value(a) == 7, "the store reads it back", ratings.value(a))
check(ratings.map_for() == {api_a: 7},
      "the map is keyed by the forward-slashed library path", ratings.map_for())
check(ratings.counts()["7"] == 1, "the count follows", ratings.counts())

ratings.set_rating(a, 0, user="")
check(ratings.value(a) == 0, "0 clears the value")
check(ratings.map_for() == {}, "and the row is gone from the map", ratings.map_for())
check(ratings.counts()["7"] == 0, "and from the counts", ratings.counts())

# ── 2. half-star values survive, unit conversion is exact ───────────────────
for i in range(1, 11):
    ratings.set_rating(a, i)
    check(ratings.value(a) == i, f"{i} half-stars survive", ratings.value(a))
    check(ratings.map_for()[api_a] == i, f"{i} half-stars in the map")
    check(ratings.to_tag(i) == i * 10, f"{i} half-stars is {i * 10} in the tag")
    check(ratings.from_tag(i * 10) == i, f"{i * 10} in a tag is {i} half-stars")
ratings.set_rating(a, 0)

check(ratings.from_tag("80") == 8, "Picard's 80 is four stars")
check(ratings.from_tag(65) == 7, "an off-scale 65 lands on the nearest half-star")
check(ratings.from_tag(None) == 0 and ratings.from_tag("") == 0
      and ratings.from_tag("junk") == 0, "an unreadable tag is unrated")
check(ratings.to_tag(0) == 0, "clearing writes 0")

# junk is refused with ValueError (the endpoints answer 400), numbers clamp
for bad in (None, "", "abc", [], {}, True, 3.5, "1.5"):
    try:
        ratings.parse_half_stars(bad)
        check(False, f"{bad!r} is refused as a rating")
    except ValueError:
        pass
check(ratings.parse_half_stars(12) == 10, "12 clamps to 10")
check(ratings.parse_half_stars(-3) == 0, "-3 clamps to 0")
check(ratings.parse_half_stars("10") == 10, "a numeric string is accepted")
check(ratings.parse_half_stars(6.0) == 6, "a whole float is accepted")

# ── 3. a bulk set reports per-path failures ─────────────────────────────────
gone = os.path.join(tmp, "Album", "missing.flac")
res = ratings.bulk_rate([a, gone], 4, cfg=CFG)
check(res["updated"] == 1, "the file that is there was rated", res)
check([f["path"] for f in res["failed"]] == [gone.replace("\\", "/")],
      "the vanished path is the failure", res["failed"])
check(res["failed"][0]["error"] == ratings.MISSING_FILE,
      "and says why", res["failed"])
check(ratings.value(a) == 4, "the good path really took the rating")
check(res["tags_written"] == 1, "its tag was written too", res)
check(TAGS[key(a)]["RATING"] == "40", "as 0-100", TAGS)

# ── 4. the tag write produces 0-100 and clearing removes it ─────────────────
CALLS.clear()
TAGS.pop(key(b), None)
out = ratings.write_tag(b, 7, CFG)
check(out["written"] and out["rating100"] == 70 and not out["error"],
      "a write produces the 0-100 value", out)
check(TAGS[key(b)]["RATING"] == "70", "and the file carries it", TAGS)
check([c for c in CALLS if c[0] == "set" and c[1] == b] == [("set", b, "70")],
      "through one container write", CALLS)

CALLS.clear()
out = ratings.write_tag(b, 7, CFG)
check(out["written"] is False and not CALLS,
      "a file that already says it is not rewritten", (out, CALLS))

out = ratings.write_tag(b, 0, CFG)
check(out["written"] and out["rating100"] == 0, "clearing writes nothing", out)
check("RATING" not in TAGS.get(key(b), {}), "and removes the tag", TAGS)
check([c for c in CALLS if c[0] == "delete"] == [("delete", b, None)],
      "by deleting it", CALLS)

# the write gate: off, the file is untouched but the rating still stands
CALLS.clear()
out = ratings.write_tag(b, 5, {"write_rating_tags": False})
check(out["skipped"] and not out["written"] and not CALLS,
      "write_rating_tags off leaves the file alone", (out, CALLS))
check(DEFAULT_CONFIG["write_rating_tags"] is True, "the key defaults on")
check(should_write_audio_tag({"write_rating_tags": False}, "RATING",
                             filetype="flac") is False,
      "the master switch reaches the tag gate")
check(should_write_audio_tag(DEFAULT_CONFIG, "RATING", filetype="flac") is True,
      "and the shipped default allows it")

# a container that refuses the write must never cost the user the rating
FAIL.add(key(b))
res = ratings.bulk_rate([b], 8, cfg=CFG)
check(res["updated"] == 1, "the rating is stored anyway", res)
check(ratings.value(b) == 8, "in the database", ratings.value(b))
check([f["path"] for f in res["tags_failed"]] == [b.replace("\\", "/")],
      "with the tag failure reported per path", res["tags_failed"])
check(res["tags_failed"][0]["error"] == "read-only file",
      "naming the container's own error", res["tags_failed"])
one = ratings.rate(b, 3, cfg=CFG)
check(one["rating"] == 3 and one["tag"]["error"] == "read-only file",
      "a single PUT reports it too", one)
check(ratings.value(b) == 3, "and the rating still moved", ratings.value(b))
FAIL.discard(key(b))
ratings.set_rating(b, 0)
ratings.set_rating(a, 0)          # section 3 rated it; the store starts clean

# ── 5. adoption: a RATING tag with no row supplies the initial value ────────
TAGS[key(a)] = {"RATING": "80"}
check(ratings.map_for() == {},
      "the unfiltered map opens no file, so nothing is adopted yet",
      ratings.map_for())
check(ratings.map_for(paths=[a]) == {api_a: 8},
      "the requested-path read adopts the file's own RATING",
      ratings.map_for(paths=[a]))
check(ratings.value(a) == 8, "and records it as a row", ratings.value(a))
check(api_a in ratings.map_for(),
      "so the next full map already carries it", ratings.map_for())

# a stored value always wins over the file's
ratings.set_rating(a, 3)
check(ratings.map_for(paths=[a]) == {api_a: 3},
      "a DB value is never overwritten by the file's tag", ratings.map_for(paths=[a]))
check(ratings.adopt(a) == 0, "adopt() reports nothing to adopt", ratings.value(a))
check(TAGS[key(a)]["RATING"] == "80", "the file's tag is left as it was")
ratings.set_rating(a, 0)
check(ratings.map_for(paths=[a]) == {api_a: 8},
      "cleared, the file's own tag supplies the value again",
      ratings.map_for(paths=[a]))
ratings.set_rating(a, 0)          # leave the store empty for the rename below

# ── 6. a rename heals by MusicBrainz recording id ───────────────────────────
MBID = "11111111-2222-3333-4444-555555555555"
old = os.path.join(tmp, "Old", "01 - One.flac")     # never on disk
new = make_file("New/01 - One.flac")
api_old = old.replace("\\", "/")
TAGS.pop(key(old), None)
TAGS[key(new)] = {}
ratings.set_rating(old, 9, mbid=MBID)
check(ratings.map_for() == {api_old: 9},
      "a rating is NOT dropped while the move is still unknown to the index",
      ratings.map_for())

INDEX["tracks"][MBID] = new.replace("\\", "/")
INDEX["tracks_bypath"][new.replace("\\", "/")] = MBID
api_new = new.replace("\\", "/")
check(ratings.map_for(paths=[old]) == {api_old: 9},
      "a caller still holding the stale path keeps the rating",
      ratings.map_for(paths=[old]))
check(ratings.map_for() == {api_new: 9},
      "the rating follows the file to its new path", ratings.map_for())
check(ratings.value(new) == 9, "and the row was re-pointed", ratings.value(new))

# ── 7. keys are the library payload's own path form ─────────────────────────
check(all("\\" not in k for k in ratings.map_for()),
      "no map key carries a backslash", ratings.map_for())
check(api_new in ratings.map_for(),
      "the key is track['path'] exactly", ratings.map_for())

# ── 8. the endpoints' own contract ──────────────────────────────────────────
put = api_ratings.ratings_put(api_ratings.RatingPut(path=a, rating=7))
check(put["ok"] and put["path"] == api_a and put["rating"] == 7,
      "PUT answers ok/path/rating", put)
check(put["tag"]["rating100"] == 70, "and reports the tag", put["tag"])
try:
    api_ratings.ratings_put(api_ratings.RatingPut(path=gone, rating=7))
    check(False, "a PUT for a file that is not there is refused")
except HTTPException as e:
    check(e.status_code == 404, "a missing file is a 404", e.status_code)
try:
    api_ratings.ratings_put(api_ratings.RatingPut(path=a, rating="junk"))
    check(False, "a PUT with junk is refused")
except HTTPException as e:
    check(e.status_code == 400, "junk is a 400", e.status_code)

got = api_ratings.ratings_get(paths=[a])
check(got["ratings"] == {api_a: 7}, "GET ?paths= answers those paths", got)
check(set(got["counts"]) == {str(i) for i in range(1, 11)},
      "and always the ten buckets", got["counts"])
bulk = api_ratings.ratings_bulk(api_ratings.RatingBulk(paths=[a, gone], rating=6))
check(bulk["updated"] == 1 and len(bulk["failed"]) == 1,
      "bulk keeps the per-path failure", bulk)

# ── 9. the optimizer's own strip pass keeps RATING ──────────────────────────
check(tag_key_allowed("RATING"), "the excess predicate accepts RATING")
check("RATING" in TAG_ALLOWLIST, "RATING is in the shared allow-list",
      sorted(k for k in TAG_ALLOWLIST if k.startswith("RAT")))

from mlo import format_all                              # noqa: E402

strip_path = make_file("Album/03 - Three.flac")
TAGS[key(strip_path)] = {"RATING": "70", "TITLE": "Three", "VENDOR_JUNK": "x"}
deleted = []
strip_af = FakeAudio(strip_path)
_realdel = strip_af.delete_tag


def _delete(name):
    deleted.append(name)
    return _realdel(name)


strip_af.delete_tag = _delete
format_all._format_audio_tags(strip_path, {"strip_unknown_tags": True,
                                           "audio_tag_writes": {}}, af=strip_af)
check("VENDOR_JUNK" in deleted, "the strip pass still removes junk", deleted)
check("RATING" not in deleted, "and leaves the rating alone", deleted)
check("RATING" in TAGS.get(key(strip_path), {}), "the tag survives the pass",
      TAGS.get(key(strip_path)))

# ── done ────────────────────────────────────────────────────────────────────
shutil.rmtree(tmp, ignore_errors=True)

if fails:
    print("ratings: FAILED")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("ratings: all assertions passed")
sys.exit(0)
