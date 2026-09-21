#!/usr/bin/env python3
"""Ratings: half-star precision, rename healing, RATING-tag interop.

Covers the three scopes — a track (which also carries the file's RATING tag),
an album folder and an artist folder (which live in the store alone and store
independently of each other and of the tracks), their per-scope counts, the
refusal of a folder the library does not know, and the migration of a database
written before scopes existed.

Everything runs against a throwaway database and throwaway files under the
system temp dir. The container writer AND the cached tag reader are stubbed
through ONE in-memory tag store, and the library payload, index and config are
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

INDEX = {"tracks": {}, "tracks_bypath": {}, "albums": {}, "albums_bypath": {},
         "artists": {}, "artists_bypath": {}}
mbresolve.get_index = lambda cfg=None, force=False: INDEX
mbresolve.track_mbid_for = lambda path: INDEX["tracks_bypath"].get(
    str(path).replace("\\", "/"), "")

# The folder scopes ask the library PAYLOAD which folders are albums/artists, so
# the payload is stubbed to one artist holding one album — the same one real
# files are made under below, so paths and payload agree.
from server import library as library_mod              # noqa: E402

ent_track = make_file("Artist/Album 1/01 - One.flac")
ALBUM_DIR = os.path.dirname(ent_track)
ARTIST_DIR = os.path.dirname(ALBUM_DIR)
api_ent_track = ent_track.replace("\\", "/")
api_album_dir = ALBUM_DIR.replace("\\", "/")
api_artist_dir = ARTIST_DIR.replace("\\", "/")
library_mod.build_library = lambda cfg=None: {
    "folder": tmp.replace("\\", "/"),
    "artists": [{"path": api_artist_dir, "name": "Artist",
                 "albums": [{"path": api_album_dir, "tracks": []}]}],
}

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
ratings.set_rating(a, 0)          # sections 6 and 8 left track rows behind
ratings.set_rating(b, 0)
ratings.set_rating(new, 0)

# ── 10. the track scope, unchanged: 4.5 stars is 9 and tags the file 90 ─────
c = make_file("Album/04 - Four.flac")
d = make_file("Album/05 - Five.flac")
TAGS[key(c)] = {}
CALLS.clear()
out = ratings.rate(c, 9, cfg=CFG)
check(out["rating"] == 9 and out["tag"]["written"] and out["tag"]["rating100"] == 90,
      "a 4.5-star track write stores 9 and reports the tag value", out)
check(TAGS[key(c)]["RATING"] == "90", "the file really carries 90", TAGS[key(c)])
TAGS[key(d)] = {}
out = ratings.rate(d, 9, cfg={"write_rating_tags": False})
check(out["rating"] == 9 and out["tag"]["skipped"] and not out["tag"]["written"],
      "with write_rating_tags off the store still takes it", out)
check("RATING" not in TAGS[key(d)], "and the file is left alone", TAGS[key(d)])
ratings.set_rating(c, 0)
ratings.set_rating(d, 0)

# ── 11. album and artist ratings: three independent scopes, one store ───────
check(ratings.target_missing("track", a, CFG) == "",
      "a file that is there can take a track rating")
check(ratings.target_missing("album", ALBUM_DIR, CFG) == "",
      "an album folder the payload knows can take an album rating")
check(ratings.target_missing("artist", ARTIST_DIR, CFG) == "",
      "and its artist folder an artist rating")
check(ratings.target_missing("album", os.path.join(tmp, "Nope"), CFG)
      == ratings.MISSING["album"],
      "a folder no album row names is refused with that scope's reason",
      ratings.target_missing("album", os.path.join(tmp, "Nope"), CFG))
check(ratings.target_missing("artist", ALBUM_DIR, CFG) == ratings.MISSING["artist"],
      "an ALBUM folder is not an artist folder")
check(ratings.target_missing("track", ALBUM_DIR, CFG) == ratings.MISSING_FILE,
      "and a folder is never a track")
try:
    ratings.parse_scope("playlist")
    check(False, "a scope nobody has is refused")
except ValueError as e:
    check("scope" in str(e), "a scope nobody has is refused by name", e)
check(ratings.parse_scope("") == "track" and ratings.parse_scope(None) == "track",
      "an absent scope is the track scope (today's callers)")
check(ratings.parse_scope("Album") == "album", "and the word is case-insensitive")

ratings.set_rating(ALBUM_DIR, 10, scope="album")
ratings.set_rating(ARTIST_DIR, 6, scope="artist")
ratings.set_rating(ent_track, 5)
check(ratings.value(ALBUM_DIR, scope="album") == 10, "the album keeps its 5 stars")
check(ratings.value(ARTIST_DIR, scope="artist") == 6, "the artist keeps its 3")
check(ratings.value(ent_track) == 5, "the track keeps its 2.5")
check(ratings.map_for(scope="album") == {api_album_dir: 10},
      "the album map carries the album's own verdict", ratings.map_for(scope="album"))
check(ratings.map_for(scope="artist") == {api_artist_dir: 6},
      "the artist map carries the artist's", ratings.map_for(scope="artist"))
check(ratings.map_for() == {api_ent_track: 5},
      "and neither leaks into the track map", ratings.map_for())
check(ratings.value(ALBUM_DIR, scope="artist") == 0
      and ratings.value(ARTIST_DIR, scope="album") == 0,
      "an album row is not an artist row even for the same folder tree")
check(ratings.counts()["10"] == 0 and ratings.counts(scope="album")["10"] == 1,
      "the counts are per scope", (ratings.counts(), ratings.counts(scope="album")))
check(ratings.counts(scope="artist")["6"] == 1, "the artist count is its own",
      ratings.counts(scope="artist"))

# A folder rating never touches a file: there is no tag for a folder.
CALLS.clear()
out = ratings.rate(ALBUM_DIR, 10, scope="album", cfg=CFG)
check(out["tag"] is None, "an album rating reports no tag write", out)
check(out["rating"] == 10 and not CALLS, "and nothing reached the filesystem", CALLS)
CALLS.clear()
out = ratings.rate(ARTIST_DIR, 0, scope="artist", cfg=CFG)
check(out["tag"] is None and out["rating"] == 0 and not CALLS,
      "clearing an artist rating writes nothing either", (out, CALLS))

# Clearing is per scope: each verdict leaves the other two standing.
check(ratings.map_for(scope="artist") == {}, "the artist is now unrated",
      ratings.map_for(scope="artist"))
check(ratings.value(ALBUM_DIR, scope="album") == 10
      and ratings.value(ent_track) == 5,
      "while the album and the track it holds are untouched")
ratings.set_rating(ALBUM_DIR, 0, scope="album")
check(ratings.map_for(scope="album") == {} and ratings.value(ent_track) == 5,
      "clearing the album leaves the track alone", ratings.map_for(scope="album"))
check(ratings.counts(scope="album")["10"] == 0, "and empties its bucket")

# A folder rating heals across a move on the payload's own release id — the same
# self-repair a track row gets from its recording id (section 6).
MBID_ALBUM = "aaaaaaaa-1111-2222-3333-444444444444"
INDEX["albums_bypath"][api_album_dir] = MBID_ALBUM
check(ratings.entity_mbid("album", api_album_dir, CFG) == MBID_ALBUM,
      "a folder's release id comes out of the library payload")
check(ratings.entity_mbid("artist", api_artist_dir, CFG) == "",
      "and is empty when the payload carries no id for it")
heal_new = os.path.dirname(make_file("Other/New Album/01 - One.flac")).replace("\\", "/")
heal_old = os.path.join(tmp, "Other", "Old Album")     # never on disk
INDEX["albums"][MBID_ALBUM] = heal_new
ratings.set_rating(heal_old, 8, mbid=MBID_ALBUM, scope="album")
check(ratings.map_for(scope="album", paths=[heal_old])
      == {heal_old.replace("\\", "/"): 8},
      "a page still holding the stale folder path keeps its value",
      ratings.map_for(scope="album", paths=[heal_old]))
check(ratings.map_for(scope="album") == {heal_new: 8},
      "an album row whose folder moved is answered under its current path",
      ratings.map_for(scope="album"))
check(ratings.value(heal_new, scope="album") == 8, "and the row was re-pointed")

# ── 12. the endpoints, per scope ────────────────────────────────────────────
put = api_ratings.ratings_put(api_ratings.RatingPut(path=ALBUM_DIR, rating=10, scope="album"))
check(put["ok"] and put["scope"] == "album" and put["rating"] == 10 and put["tag"] is None,
      "PUT takes a scope and answers which one it stored", put)
check(put["path"] == api_album_dir, "echoing the forward-slashed folder path", put)
put_artist = api_ratings.ratings_put(
    api_ratings.RatingPut(path=ARTIST_DIR, rating=4, scope="artist"))
check(put_artist["scope"] == "artist" and put_artist["rating"] == 4,
      "the artist scope stores what it was given", put_artist)
try:
    api_ratings.ratings_put(api_ratings.RatingPut(path=ent_track, rating=4, scope="album"))
    check(False, "a PUT for a file that is not an album folder is refused")
except HTTPException as e:
    check(e.status_code == 404 and e.detail == ratings.MISSING["album"],
          "a non-album path is a 404 naming the reason", (e.status_code, e.detail))
try:
    api_ratings.ratings_put(api_ratings.RatingPut(path=ALBUM_DIR, rating=4, scope="genre"))
    check(False, "a PUT with an unknown scope is refused")
except HTTPException as e:
    check(e.status_code == 400, "an unknown scope is a 400", e.status_code)
put_track = api_ratings.ratings_put(api_ratings.RatingPut(path=ent_track, rating=9))
check(put_track["scope"] == "track" and put_track["tag"]["rating100"] == 90,
      "a PUT without a scope is still a track rating with its tag", put_track)

got_album = api_ratings.ratings_get(paths=None, scope="album")
check(got_album["scope"] == "album"
      and got_album["ratings"] == {api_album_dir: 10, heal_new: 8},
      "GET ?scope=album answers the album map, and only album rows", got_album)
check(set(got_album["counts"]) == {str(i) for i in range(1, 11)},
      "with the same ten buckets", got_album["counts"])
check(got_album["counts"]["10"] == 1 and got_album["counts"]["8"] == 1,
      "counting the album rows and nothing else", got_album["counts"])
got_artist = api_ratings.ratings_get(scope="artist", paths=[ARTIST_DIR])
check(got_artist["ratings"] == {api_artist_dir: 4},
      "and ?scope=artist&paths= answers just that folder", got_artist)
check(api_ratings.ratings_get(scope="album", paths=[a])["ratings"] == {},
      "a track path in an album request answers nothing", a)
bulk = api_ratings.ratings_bulk(api_ratings.RatingBulk(paths=[ent_track], rating=7))
check(bulk["scope"] == "track" and bulk["updated"] == 1,
      "bulk stays the track call", bulk)

# ── 13. a database written before scopes existed ────────────────────────────
import sqlite3                                         # noqa: E402

legacy_dir = tempfile.mkdtemp(prefix="mlo_ratings_legacy_")
legacy_db = os.path.join(legacy_dir, "ratings.db")
con = sqlite3.connect(legacy_db)
con.executescript("""
    CREATE TABLE ratings (
        user TEXT NOT NULL DEFAULT '',
        path TEXT NOT NULL,
        rating INTEGER NOT NULL,
        mbid TEXT,
        updated REAL NOT NULL,
        PRIMARY KEY (user, path)
    );
    CREATE INDEX ratings_by_mbid ON ratings (user, mbid);
    INSERT INTO ratings VALUES ('', 'C:/music/Old/01.flac', 7, 'rec-1', 1.0);
    INSERT INTO ratings VALUES ('someone', 'C:/music/Old/02.flac', 4, NULL, 2.0);
    """)
con.commit()
con.close()

main_db = ratings.db_path
ratings.db_path = lambda: legacy_db
ratings._initialized = False      # force the schema pass on the next connection
check(ratings.map_for() == {"C:/music/Old/01.flac": 7},
      "a row written before scopes existed reads back as a track rating",
      ratings.map_for())
check(ratings.counts()["7"] == 1 and ratings.counts("someone")["4"] == 1,
      "with its value and its user intact")
check(ratings.map_for(scope="album") == {},
      "and it is NOT an album rating", ratings.map_for(scope="album"))
check(ratings.set_rating("C:/music/Old", 3, scope="album") == 3,
      "a folder rating can now share a path with a migrated track row")
con = sqlite3.connect(legacy_db)
tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
info = list(con.execute("PRAGMA table_info(ratings)"))
con.close()
check("ratings_legacy" not in tables, "the pre-scope table is gone", tables)
check([r[1] for r in info][:3] == ["user", "scope", "path"],
      "the new table leads with the key", [r[1] for r in info])
check({r[1] for r in info if r[5]} == {"user", "scope", "path"},
      "keyed by (user, scope, path), so one path can be both", info)
ratings.db_path = main_db
ratings._initialized = False
check(ratings.value(ent_track) == 7 and ratings.value(ALBUM_DIR, scope="album") == 10,
      "the main database answers unchanged after that",
      (ratings.value(ent_track), ratings.value(ALBUM_DIR, scope="album")))
shutil.rmtree(legacy_dir, ignore_errors=True)

# ── done ────────────────────────────────────────────────────────────────────
shutil.rmtree(tmp, ignore_errors=True)

if fails:
    print("ratings: FAILED")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("ratings: all assertions passed")
sys.exit(0)
