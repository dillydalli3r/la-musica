#!/usr/bin/env python3
"""The library query engine (mlo/query.py + server/api_query.py).

What this pins, over a synthetic library built in-process (no music folder, no
config, no tag reads):

  * every field family matches what it CLAIMS — tag equality/contains/empty
    and regex, rating ranges over halves and "unrated", grade/audit, numeric
    tech compares, the analysis numerics, the library booleans, album and
    artist facts. The expectations are written per family, so a field that
    quietly resolves against the wrong row (a track reading its ALBUM's log
    flag, a rating coerced out of Picard's 0-100 tag, a missing tag counted as
    a value) fails here rather than in the UI.
  * `all` vs `any` precedence, including the case where they MUST differ.
  * sorting (decorated once, blanks last in both directions) and grouping
    (items stamped and contiguous, counts summing to the total).
  * facets: counts, the blank bucket, the `q` filter, and the standalone
    endpoint agreeing with the facet inside a query response.
  * honest `total` for a paginated answer, and `limit: 0` still counting.
  * the catalogue: every tag the registry knows is a field, the ops a field
    advertises are ops the engine can evaluate, and unknown field / op /
    range / regex / group / sort key is a 400.
  * the SMART-PLAYLIST EQUIVALENCE case: the specs the old evaluator's suite
    would have used still select exactly the same paths, and
    `server.playlists.evaluate_smart` answers from the same engine.

There is no tools/test_smart_playlists.py in this tree (checked: no test
anywhere imports evaluate_smart), so the equivalence case lives here.

Run: python tools/test_query.py   (exit 0 pass, 1 fail)
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi import FastAPI                                    # noqa: E402
from fastapi.testclient import TestClient                      # noqa: E402

from mlo import query as q                                     # noqa: E402
from server import api_query as aq                             # noqa: E402
from server import library as lib_mod                          # noqa: E402
from server import playlists as pl                             # noqa: E402
from server.tags_registry import registry                      # noqa: E402

fails = []


def check(ok, label, extra=""):
    if not ok:
        fails.append(f"{label}{(': ' + str(extra)) if extra else ''}")
        print(f"  FAIL {label}" + (f" — {extra}" if extra else ""))


def check_eq(got, want, label):
    check(got == want, label, f"got {got!r}, want {want!r}")


def section(name):
    print(f"\n== {name} ==")


# --------------------------------------------------------------------------- #
# A synthetic library. Deterministic by index, so every expectation below can
# be stated as a rule instead of a fixture dump.
# --------------------------------------------------------------------------- #
GENRES = ("shoegaze", "dream pop", "idm")
NONE_GENRE_EVERY = 7          # every 7th track has no GENRE tag at all


def make_library(albums=6, per_album=4):
    """A payload shaped exactly like server.library.build_library's."""
    artists = {}
    for a in range(albums):
        artist_name = f"Artist {a // 2 + 1}"
        album_name = f"Album {a + 1}"
        tracks = []
        for t in range(per_album):
            n = a * per_album + t + 1
            genre = None if n % NONE_GENRE_EVERY == 0 else GENRES[n % 3]
            video = n % 11 == 0
            unreadable = n % 13 == 0
            tags = {
                "TITLE": f"Track {n}",
                "ARTIST": artist_name,
                "ALBUMARTIST": artist_name,
                "ALBUM": album_name,
                "DATE": f"{2000 + (n % 6)}-0{(n % 9) + 1}-01",
                "GENRE": genre,
                "TRACKNUMBER": f"{t + 1}/{per_album}",
                # Picard's 0-100 scale, on purpose: it must NOT be read as stars.
                "RATING": "80" if n % 5 == 0 else None,
                "MOOD": ("dreamy", "happy", "dark")[n % 3],
                "ENERGY": str(40 + (n % 50)),
                "DYNAMIC RANGE": str(5 + (n % 10)),
                "BPM": str(90 + (n % 80)),
                "INITIALKEY": ("8A", "5B", "11A")[n % 3],
                "MEDIA": "cd" if n % 2 else "vinyl",
                "TITLESORT": None,
            }
            issues = []
            if n % 4 == 0:
                issues.append("COVER")
            if n % 9 == 0:
                issues.append("LYRICS")
            tracks.append({
                "file": f"{t + 1:02d} - Track {n}.flac",
                "path": f"C:/music/{artist_name}/{album_name}/{t + 1:02d} - Track {n}.flac",
                "issues": issues,
                "values": {},
                "lyrics_embedded": n % 3 == 0,
                "lyrics_lrc": n % 5 == 0,
                "unreadable": unreadable,
                "audit": ("REAL", "FAKE", None)[n % 3],
                "log_grade": None,
                "tags": tags,
                "tech": {
                    "bitrate": 128 if n % 3 == 0 else 950,
                    "sample_rate": 44100 if n % 2 else 48000,
                    "bits_per_sample": 16 if n % 2 else 24,
                    "length": 120.0 + (n % 60),
                    "channels": 2,
                    "codec": "FLAC" if n % 3 else "MP3",
                },
                "cover_file": "cover.jpg" if n % 3 else None,
                "discnumber": 1,
                "tracknumber": t + 1,
                "is_video": video,
                "grade_pass": not issues,
                "lyrics_present": bool(n % 3 == 0 or n % 5 == 0),
            })
        album = {
            "path": f"C:/music/{artist_name}/{album_name}",
            "album_artist": artist_name,
            "audit_summary": ("REAL", "FAKE", "Mix", None)[a % 4],
            "media": "cd",
            "track_count": per_album,
            "pass_count": sum(1 for tr in tracks if tr["grade_pass"]),
            "total_checks": per_album,
            "grade_pct": (50.0 if a % 2 else 100.0),
            "has_log": a % 2 == 0,
            "has_cue": a % 3 == 0,
            "cover_file": None if a == 3 else "cover.jpg",
            "cover_ok": a != 3,
            "pending": False,
            "partial": a % 3 == 1,
            "tracks": tracks,
            "issues": {} if a % 2 else {"COVER": ["cover.jpg"]},
            # Album meta as server.library really builds it: the ALBUM_LEVEL
            # tags off the first track — no GENRE, so an album-level genre has
            # to come from the album's tracks.
            "meta": {"ALBUM": album_name, "DATE": f"{2000 + a}",
                     "ALBUMARTIST": artist_name, "MEDIA": "cd"},
        }
        album["pass"] = album["pass_count"] == album["total_checks"]
        artists.setdefault(artist_name, []).append(album)
    out = []
    for artist_name, albums in artists.items():
        track_count = sum(a["track_count"] for a in albums)
        pass_count = sum(a["pass_count"] for a in albums)
        out.append({
            "path": f"C:/music/{artist_name}",
            "name": artist_name,
            "albums": albums,
            "aggregate": {
                "album_count": len(albums),
                "track_count": track_count,
                "pass_count": pass_count,
                "total_checks": sum(a["total_checks"] for a in albums),
                "grade_pct": round(100.0 * pass_count / track_count, 1),
                "audit_summary": ("REAL" if artist_name == "Artist 1" else "Mix"),
            },
        })
    return {"folder": "C:/music", "artists": out}


LIB = make_library()
TRACKS = [tr for art in LIB["artists"] for alb in art["albums"] for tr in alb["tracks"]]
ALBUMS = [alb for art in LIB["artists"] for alb in art["albums"]]

# Rating store: test paths whose store row is 10 (5 stars), 9 (4.5), 8 (4),
# 6 (3), 5 (2.5), 1 (a half) and 0 (unrated). Inside "Album 1" the values run
# 5/4.5/4/3 stars and inside "Album 2" unrated/a half/2.5/unrated, which is what
# lets the scope tests below tell an album's own verdict from its tracks'
# ratings.
HALF = {TRACKS[0]["path"]: 10, TRACKS[1]["path"]: 9, TRACKS[2]["path"]: 8,
        TRACKS[3]["path"]: 6, TRACKS[4]["path"]: 0, TRACKS[5]["path"]: 1,
        # 2.5 stars, on a track whose whole folder carries a 5-star verdict.
        TRACKS[6]["path"]: 5}


def rating_of(path, raw_tag):
    return HALF.get(path)


# Folder-scope rows: the user's verdict on the ENTITY, keyed by that entity's
# folder path and deliberately unrelated to the ratings of what it holds.
#   Album 1: 5 stars (its tracks are 5/4.5/4/3)
#   Album 2: 5 stars (its tracks are unrated/0.5/2.5/unrated — no average of
#            them is anywhere near 5)
#   Album 3: 1.5 stars, though one of its tracks carries a 4-star file tag
#   Artist 1: 1.5 stars, though its tracks are the 5/4.5/4/3/2.5 ones
#   Artist 2: 5 stars, though every row under it is unrated
FOLDER = {
    "album": {
        ALBUMS[0]["path"]: 10,
        ALBUMS[1]["path"]: 10,
        ALBUMS[2]["path"]: 3,
    },
    "artist": {
        LIB["artists"][0]["path"]: 3,
        LIB["artists"][1]["path"]: 10,
    },
}


def folder_of(scope, path):
    return FOLDER.get(scope, {}).get(path)


# The one injected source answers tracks through its call and the two folder
# scopes through its `folder` half — mlo.query's documented plugin point.
rating_of.folder = folder_of


# The rating store is injected into the engine, so its boundary is exercised
# with a stub standing in for server.ratings (another slice's file). The stub
# follows the frozen interface: {path: half-stars} FOR ONE SCOPE, plus from_tag
# (0-100 -> half). Folder rows are keyed by the entity's folder path, exactly as
# the real store keys them.
import types                                                          # noqa: E402

STUB = types.ModuleType("server.ratings")
STUB.map_for = lambda paths=None, user="", scope="track": dict(
    HALF if scope == "track" else FOLDER.get(scope) or {})
STUB.from_tag = lambda value: {"80": 8}.get(str(value))
sys.modules["server.ratings"] = STUB


def half_of(tr):
    """The half-stars the engine will see: the store's row, else the file tag."""
    half = HALF.get(tr["path"])
    if half is None and tr["tags"].get("RATING"):
        half = STUB.from_tag(tr["tags"]["RATING"])
    return half


def album_of(tr):
    for alb in ALBUMS:
        if tr in alb["tracks"]:
            return alb
    return None


def ask(**req):
    req.setdefault("limit", 0)
    return q.run(LIB, req, rating_of=rating_of, bool_fields=aq.bool_fields())


def matched(**req):
    """The paths a request selects (all of them, sorted by path so an
    expectation can be written as a comprehension over the fixture)."""
    req.setdefault("limit", 5000)
    req["sort"] = {"key": "library.path", "dir": 1}
    return [item["path"] for item in ask(**req)["items"]]


def cond(field, op, value=None):
    c = {"field": field, "op": op}
    if value is not None:
        c["value"] = value
    return c


# --------------------------------------------------------------------------- #
section("field families")
# --------------------------------------------------------------------------- #
check_eq(matched(conditions=[cond("tags.GENRE", "eq", "shoegaze")]),
         [tr["path"] for tr in TRACKS if tr["tags"]["GENRE"] == "shoegaze"],
         "tags.GENRE is -> exactly the shoegaze tracks")

check_eq(matched(conditions=[cond("tags.GENRE", "contains", "SHOE")]),
         [tr["path"] for tr in TRACKS if tr["tags"]["GENRE"] == "shoegaze"],
         "tags.GENRE contains is case-insensitive")

check_eq(matched(conditions=[cond("tags.GENRE", "missing")]),
         [tr["path"] for tr in TRACKS if not tr["tags"].get("GENRE")],
         "tags.GENRE is empty -> only the untagged ones")
check_eq(matched(conditions=[cond("tags.GENRE", "present")]),
         [tr["path"] for tr in TRACKS if tr["tags"].get("GENRE")],
         "tags.GENRE is not empty -> the complement")

check_eq(matched(conditions=[cond("tags.GENRE", "matches", "^dream")]),
         [tr["path"] for tr in TRACKS if (tr["tags"]["GENRE"] or "").startswith("dream")],
         "tags.GENRE matches regex")

# A multi-value operand: "is any of" is ONE condition, not an OR of two.
check_eq(matched(conditions=[cond("tags.GENRE", "eq", ["shoegaze", "idm"])]),
         [tr["path"] for tr in TRACKS if tr["tags"]["GENRE"] in ("shoegaze", "idm")],
         "tags.GENRE is any of [..]")

check_eq(matched(conditions=[cond("tags.TRACKNUMBER", "eq", "1/4")]),
         [tr["path"] for tr in TRACKS if tr["tags"]["TRACKNUMBER"] == "1/4"],
         "tags.TRACKNUMBER is -> text comparison of a '3/12' style value")

check_eq(matched(conditions=[cond("tags.DATE", "between", ["2002", "2003"])]),
         [tr["path"] for tr in TRACKS
          if "2002" <= tr["tags"]["DATE"][:4] <= "2003"],
         "tags.DATE between two years")

# ── rating ──────────────────────────────────────────────────────────────────
check_eq(matched(conditions=[cond("rating", "gte", 4)]),
         [tr["path"] for tr in TRACKS if (HALF.get(tr["path"]) or 0) >= 8],
         "rating >= 4 stars reads halves")
check_eq(matched(conditions=[cond("rating", "eq", 4.5)]),
         [tr["path"] for tr in TRACKS if (HALF.get(tr["path"]) or 0) == 9],
         "rating = 4.5 stars (the half)")
check_eq(matched(conditions=[cond("rating", "between", [3, 4])]),
         [tr["path"] for tr in TRACKS if 6 <= (HALF.get(tr["path"]) or 0) <= 8],
         "rating between 3 and 4 inclusive")
check_eq(matched(conditions=[cond("rating", "is_unrated")]),
         [tr["path"] for tr in TRACKS if not HALF.get(tr["path"])],
         "rating is unrated -> no row or 0, including a 0-100 file tag")
check_eq(len(matched(conditions=[cond("rating", "is_rated")])),
         len(TRACKS) - len(matched(conditions=[cond("rating", "is_unrated")])),
         "rating is rated -> the exact complement")
check("tags.RATING" in {f["field"] for g in aq.catalogue()["groups"]
                        for f in g["fields"]},
      "the file RATING tag is still a field of its own (never coerced to stars)")

# ── rating: the album and artist scopes ─────────────────────────────────────
# The two folder fields are the USER's verdict on that entity, from the same
# store and in the same unit as `rating` — never an average or a rollup of the
# tracks' own ratings, which is what the two halves of each pair below prove.
def albums_matched(**req):
    """The album paths a request selects (the `matched` helper, album target)."""
    req.setdefault("limit", 5000)
    req["target"] = "albums"
    req["sort"] = {"key": "album.path", "dir": 1}
    return [item["path"] for item in ask(**req)["items"]]


five_albums = sorted(p for p, half in FOLDER["album"].items() if half >= 8)
check_eq(albums_matched(conditions=[cond("album.rating", "gte", 4)]), five_albums,
         "album.rating >= 4 answers with the albums the user rated 5 stars")
check_eq(five_albums,
         sorted([ALBUMS[0]["path"], ALBUMS[1]["path"]]),
         "…which are the two the fixture gives 5 stars (one of them holds no "
         "track anywhere near that value)")
check_eq(albums_matched(conditions=[cond("album.rating", "is_unrated")]),
         sorted(a["path"] for a in ALBUMS if a["path"] not in FOLDER["album"]),
         "the other albums are unrated, not zero-star")
check_eq(len(albums_matched(conditions=[cond("album.rating", "is_rated")]))
         + len(albums_matched(conditions=[cond("album.rating", "is_unrated")])),
         len(ALBUMS), "album.rating is_rated is the exact complement")

# The album's verdict answers for every row under it, including the track whose
# OWN rating is 2.5 stars — and that same track is NOT among the `rating >= 4`
# rows, which is the difference between the two fields.
low_track = TRACKS[6]["path"]
check_eq(matched(conditions=[cond("album.rating", "gte", 4)]),
         [tr["path"] for alb in ALBUMS if alb["path"] in five_albums
          for tr in alb["tracks"]],
         "a track row answers album.rating with its album's own verdict")
check(low_track in matched(conditions=[cond("album.rating", "gte", 4)])
      and low_track not in matched(conditions=[cond("rating", "gte", 4)]),
      "the 2.5-star track rides its 5-star album's verdict but not its own")
check_eq(matched(conditions=[cond("album.rating", "gte", 4),
                             cond("rating", "gte", 4)]),
         [tr["path"] for alb in ALBUMS if alb["path"] in five_albums
          for tr in alb["tracks"] if (HALF.get(tr["path"]) or 0) >= 8],
         "the two fields combine as two independent conditions")

# The artist mirror: Artist 1's tracks are the 5/4.5/4/3-star ones while the
# artist's own verdict is 1.5 stars, and Artist 2's rows are all unrated under a
# 5-star verdict — so a rollup of the tracks would answer both of these the
# other way round.
artist2 = LIB["artists"][1]
artist2_tracks = [tr["path"] for alb in artist2["albums"] for tr in alb["tracks"]]
check_eq(matched(conditions=[cond("artist.rating", "gte", 4)]), artist2_tracks,
         "artist.rating >= 4 answers with the artist the user rated 5 stars, "
         "even though every row under it is unrated")
check_eq(matched(conditions=[cond("artist.rating", "gte", 4),
                             cond("rating", "gte", 4)]), [],
         "and Artist 1's 4/4.5/5-star tracks are not what artist.rating reports")
check_eq(matched(conditions=[cond("artist.rating", "is_unrated")]),
         [tr["path"] for alb in LIB["artists"][2]["albums"] for tr in alb["tracks"]],
         "the artist with no store row is unrated, and answers so from every row")

check(("album.rating" in {f["field"] for g in aq.catalogue()["groups"]
                          for f in g["fields"]})
      and ("artist.rating" in {f["field"] for g in aq.catalogue()["groups"]
                               for f in g["fields"]}),
      "both folder rating fields are in the catalogue")

# ── grade / audit ───────────────────────────────────────────────────────────
check_eq(matched(conditions=[cond("grade_pass", "eq", True)]),
         [tr["path"] for tr in TRACKS if tr["grade_pass"]],
         "grade_pass is true (JSON true against a Python bool)")
check_eq(matched(conditions=[cond("grade_pass", "eq", False)]),
         [tr["path"] for tr in TRACKS if not tr["grade_pass"]],
         "grade_pass is false")
check_eq(matched(conditions=[cond("issues", "issues_contain", "cover")]),
         [tr["path"] for tr in TRACKS if "COVER" in tr["issues"]],
         "issues has issue COVER (case-insensitive code)")
check_eq(sorted(matched(conditions=[cond("grade_pct", "between", [0, 60])])),
         sorted([tr["path"] for alb in ALBUMS if alb["grade_pct"] == 50.0
                 for tr in alb["tracks"]]),
         "grade_pct is the enclosing album's percentage")
check_eq(matched(conditions=[cond("audit", "eq", "FAKE")]),
         [tr["path"] for tr in TRACKS if tr["audit"] == "FAKE"],
         "audit is FAKE -> the track's OWN verdict")
check_eq(matched(conditions=[cond("audit", "missing")]),
         [tr["path"] for tr in TRACKS if not tr["audit"]],
         "audit is empty -> tracks the audit pass has not verdicted")
check_eq(matched(conditions=[cond("album.audit", "eq", "Mix")]),
         [tr["path"] for alb in ALBUMS if alb["audit_summary"] == "Mix"
          for tr in alb["tracks"]],
         "album.audit reads the album verdict from a track row")

# ── tech ────────────────────────────────────────────────────────────────────
check_eq(matched(conditions=[cond("tech.bitrate", "lt", 320)]),
         [tr["path"] for tr in TRACKS if tr["tech"]["bitrate"] < 320],
         "tech.bitrate below 320 kbps")
check_eq(matched(conditions=[cond("tech.sample_rate", "eq", 44100)]),
         [tr["path"] for tr in TRACKS if tr["tech"]["sample_rate"] == 44100],
         "tech.sample_rate equals")
check_eq(matched(conditions=[cond("tech.length", "between", [130, 140])]),
         [tr["path"] for tr in TRACKS if 130 <= tr["tech"]["length"] <= 140],
         "tech.length range")
check_eq(matched(conditions=[cond("tech.codec", "contains", "flac")]),
         [tr["path"] for tr in TRACKS if tr["tech"]["codec"] == "FLAC"],
         "tech.codec contains (case-insensitive)")

# ── analysis ────────────────────────────────────────────────────────────────
check_eq(matched(conditions=[cond("analysis.DYNAMIC RANGE", "gte", 12)]),
         [tr["path"] for tr in TRACKS if int(tr["tags"]["DYNAMIC RANGE"]) >= 12],
         "analysis.DYNAMIC RANGE numeric compare")
check_eq(matched(conditions=[cond("analysis.BPM", "lt", 120)]),
         [tr["path"] for tr in TRACKS if int(tr["tags"]["BPM"]) < 120],
         "analysis.BPM numeric compare")
check_eq(matched(conditions=[cond("analysis.INITIALKEY", "eq", "8A")]),
         [tr["path"] for tr in TRACKS if tr["tags"]["INITIALKEY"] == "8A"],
         "analysis.INITIALKEY equality")
check_eq(matched(conditions=[cond("analysis.MOOD", "eq", "dreamy")]),
         [tr["path"] for tr in TRACKS if tr["tags"]["MOOD"] == "dreamy"],
         "analysis.MOOD enum equality")
check_eq(matched(conditions=[cond("analysis.ENERGY", "between", [60, 70])]),
         [tr["path"] for tr in TRACKS if 60 <= int(tr["tags"]["ENERGY"]) <= 70],
         "analysis.ENERGY range")

# ── library ─────────────────────────────────────────────────────────────────
check_eq(matched(conditions=[cond("library.lyrics_present", "eq", True)]),
         [tr["path"] for tr in TRACKS if tr["lyrics_present"]],
         "library.lyrics_present")
check_eq(matched(conditions=[cond("library.unreadable", "eq", True)]),
         [tr["path"] for tr in TRACKS if tr["unreadable"]],
         "library.unreadable")
check_eq(matched(conditions=[cond("library.is_video", "eq", True)]),
         [tr["path"] for tr in TRACKS if tr["is_video"]],
         "library.is_video")
check_eq(matched(conditions=[cond("library.title", "contains", "track 1 ")]),
         [tr["path"] for tr in TRACKS if "track 1 " in tr["tags"]["TITLE"].lower()],
         "library.title contains")
check_eq(matched(conditions=[cond("library.tracknumber", "eq", 2)]),
         [tr["path"] for tr in TRACKS if tr["tracknumber"] == 2],
         "library.tracknumber numeric")
shown_cover = {tr["path"]: (tr["cover_file"] or album_of(tr)["cover_file"]) for tr in TRACKS}
check_eq(matched(conditions=[cond("library.cover_file", "missing")]),
         [tr["path"] for tr in TRACKS if not shown_cover[tr["path"]]],
         "library.cover_file is empty -> only tracks with no cover anywhere")
# has_cue is an ALBUM fact; a track row must answer with its album's.
check_eq(matched(conditions=[cond("library.has_cue", "eq", True)]),
         [tr["path"] for alb in ALBUMS if alb["has_cue"] for tr in alb["tracks"]],
         "library.has_cue on a track row reads its album")
check_eq(matched(conditions=[cond("library.has_log", "eq", False)]),
         [tr["path"] for alb in ALBUMS if not alb["has_log"] for tr in alb["tracks"]],
         "library.has_log false on a track row reads its album")

# ── album / artist ──────────────────────────────────────────────────────────
album_genre = q.run(LIB, {"target": "albums", "group": "genre", "limit": 0},
                    rating_of=rating_of)
check_eq(sum(g["count"] for g in album_genre["group_counts"]), len(ALBUMS),
         "an album's genre comes from its tracks (tags.GENRE on an album row)")
check_eq(sorted(g["key"] for g in album_genre["group_counts"] if g["key"]),
         sorted({alb["tracks"][0]["tags"]["GENRE"] for alb in ALBUMS
                 if alb["tracks"][0]["tags"]["GENRE"]}),
         "album-level genre keys are the first track's own genre")
covers = ask(facets=["library.cover_file"])
covered = [tr for tr in TRACKS if shown_cover[tr["path"]]]
check_eq({v["value"]: v["count"] for v in covers["facets"][0]["values"]},
         {"cover.jpg": len(covered)},
         "library.cover_file falls back to the album's cover (the one the row shows)")
check_eq(covers["facets"][0]["missing"], len(TRACKS) - len(covered),
         "only the album with no cover reads as cover-less")

check_eq(matched(conditions=[cond("album.name", "eq", "Album 3")]),
         [tr["path"] for alb in ALBUMS if alb["path"].endswith("/Album 3")
          for tr in alb["tracks"]],
         "album.name equality")
check_eq(matched(conditions=[cond("album.track_count", "gte", 4)]),
         [tr["path"] for tr in TRACKS], "album.track_count covers every track")
check_eq(matched(conditions=[cond("artist.name", "eq", "Artist 2")]),
         [tr["path"] for art in LIB["artists"] if art["name"] == "Artist 2"
          for alb in art["albums"] for tr in alb["tracks"]],
         "artist.name equality")
check_eq(matched(conditions=[cond("artist.album_count", "eq", 3)]),
         [tr["path"] for art in LIB["artists"] if art["aggregate"]["album_count"] == 3
          for alb in art["albums"] for tr in alb["tracks"]],
         "artist.album_count")
check_eq(matched(conditions=[cond("artist.audit", "eq", "Mix")]),
         [tr["path"] for art in LIB["artists"]
          if art["aggregate"]["audit_summary"] == "Mix"
          for alb in art["albums"] for tr in alb["tracks"]],
         "artist.audit is the artist rollup")

# --------------------------------------------------------------------------- #
section("all vs any")
# --------------------------------------------------------------------------- #
narrow = q.run(LIB, {"conditions": [cond("tags.GENRE", "eq", "shoegaze"),
                                    cond("tech.bitrate", "lt", 320)],
                     "match": "all"}, rating_of=rating_of)
wide = q.run(LIB, {"conditions": [cond("tags.GENRE", "eq", "shoegaze"),
                                  cond("tech.bitrate", "lt", 320)],
                   "match": "any"}, rating_of=rating_of)
want_all = [tr["path"] for tr in TRACKS
            if tr["tags"]["GENRE"] == "shoegaze" and tr["tech"]["bitrate"] < 320]
want_any = [tr["path"] for tr in TRACKS
            if tr["tags"]["GENRE"] == "shoegaze" or tr["tech"]["bitrate"] < 320]
check_eq(narrow["total"], len(want_all), "match:all counts the AND")
check_eq(wide["total"], len(want_any), "match:any counts the OR")
check(wide["total"] > narrow["total"],
      "all and any MUST differ on this spec", f"{narrow['total']} vs {wide['total']}")

# An impossible `all` is empty while the same conditions under `any` are not:
# the classic way a broken evaluator shows nothing instead of something.
imp = q.run(LIB, {"conditions": [cond("tags.GENRE", "eq", "shoegaze"),
                                 cond("tags.GENRE", "eq", "idm")],
                  "match": "all"}, rating_of=rating_of)
imp_any = q.run(LIB, {"conditions": [cond("tags.GENRE", "eq", "shoegaze"),
                                     cond("tags.GENRE", "eq", "idm")],
                      "match": "any"}, rating_of=rating_of)
check_eq(imp["total"], 0, "match:all of two exclusive values is empty")
check(imp_any["total"] > 0, "match:any of the same two is not empty")
empty = q.run(LIB, {"conditions": [], "match": "all"}, rating_of=rating_of)
check_eq(empty["total"], len(TRACKS), "an empty rule matches everything")
unknown_op = q.run(LIB, {"conditions": [{"field": "tags.GENRE", "op": "nope",
                                         "value": "x"}]}, rating_of=rating_of)
check_eq(unknown_op["total"], 0, "an unknown op matches nothing (no 500)")

# --------------------------------------------------------------------------- #
section("sort, group, page")
# --------------------------------------------------------------------------- #
by_rating = q.run(LIB, {"conditions": [cond("rating", "is_rated")],
                        "sort": {"key": "rating", "dir": -1}, "limit": 3},
                  rating_of=rating_of)
check_eq([it["path"] for it in by_rating["items"]],
         [TRACKS[0]["path"], TRACKS[1]["path"], TRACKS[2]["path"]],
         "rating desc puts 5, 4.5, 4 stars first")

for direction in (1, -1):
    all_blank = q.run(LIB, {"sort": {"key": "tags.UNSYNCEDLYRICS", "dir": direction},
                            "limit": 1000}, rating_of=rating_of)
    check_eq([it["path"] for it in all_blank["items"]],
             [tr["path"] for tr in TRACKS],
             f"a field the payload does not carry keeps payload order (dir {direction})")

# Blanks last in BOTH directions: MP3 rows have bitrate 128, FLAC 950, and a
# tag with no value must never jump to the top of a descending sort.
desc = q.run(LIB, {"sort": {"key": "tech.bitrate", "dir": -1}},
             rating_of=rating_of)
bitrates = [it["tech"]["bitrate"] for it in desc["items"]]
check_eq(bitrates, sorted(bitrates, reverse=True), "tech.bitrate descending")
mixed = q.run(LIB, {"sort": {"key": "tags.GENRE", "dir": -1}},
              rating_of=rating_of)
last = [it for it in mixed["items"] if not it["tags"].get("GENRE")]
check(bool(last) and all(not it["tags"].get("GENRE")
                         for it in mixed["items"][-len(last):]),
      "an empty value sorts last even descending")

grouped = q.run(LIB, {"group": "artist", "limit": 1000}, rating_of=rating_of)
keys = [it["group"] for it in grouped["items"]]
check_eq(keys, sorted(keys, key=str.lower), "grouped items arrive in group order")
check_eq(sorted(k for k in keys), keys, "every item carries its group key")
check_eq(sum(g["count"] for g in grouped["group_counts"]), grouped["total"],
         "group_counts sums to the total")
check_eq({g["key"] for g in grouped["group_counts"]},
         {art["name"] for art in LIB["artists"]}, "group keys are the artists")
check_eq({g["key"]: g["count"] for g in grouped["group_counts"]},
         {art["name"]: art["aggregate"]["track_count"] for art in LIB["artists"]},
         "artist group counts are honest")

by_year = q.run(LIB, {"group": "year", "limit": 0}, rating_of=rating_of)
check_eq(sum(g["count"] for g in by_year["group_counts"]), len(TRACKS),
         "group:year buckets every track")
by_rating = q.run(LIB, {"group": "rating", "limit": 0}, rating_of=rating_of)
check_eq({g["key"] for g in by_rating["group_counts"]},
         {"Unrated"} | {f"{h / 2:g}" for h in set(HALF.values()) if h},
         "group:rating buckets unrated apart from the stars")

paged = q.run(LIB, {"limit": 2, "offset": 1}, rating_of=rating_of)
check_eq(paged["total"], len(TRACKS), "total is honest for a page")
check_eq(len(paged["items"]), 2, "limit is honoured")
check_eq(paged["items"][0]["path"], TRACKS[1]["path"], "offset is honoured")
zero = q.run(LIB, {"limit": 0}, rating_of=rating_of)
check_eq((zero["items"], zero["total"]), ([], len(TRACKS)),
         "limit 0 returns no items but still counts")

# --------------------------------------------------------------------------- #
section("facets")
# --------------------------------------------------------------------------- #
facet_all = ask(facets=["tags.GENRE"], limit=0)
facet = facet_all["facets"][0]
want_counts = {}
for tr in TRACKS:
    g = tr["tags"].get("GENRE")
    if g:
        want_counts[g] = want_counts.get(g, 0) + 1
check_eq({v["value"]: v["count"] for v in facet["values"]}, want_counts,
         "facet counts equal the fixture")
check_eq(facet["missing"], len([t for t in TRACKS if not t["tags"].get("GENRE")]),
         "the blank bucket counts the untagged rows, not a value")
check_eq(facet["total_values"], len(want_counts), "total_values is the distinct count")
check_eq(sum(v["count"] for v in facet["values"]) + facet["missing"], len(TRACKS),
         "facet counts + missing = the whole set")

filtered = ask(conditions=[cond("tags.GENRE", "eq", "shoegaze")],
               facets=["analysis.MOOD"])
check_eq(sum(v["count"] for v in filtered["facets"][0]["values"]),
         filtered["total"], "a query facet counts only the matched rows")

numeric = ask(facets=["tech.bitrate"])["facets"][0]
check(all(isinstance(v["value"], float) for v in numeric["values"]),
      "an all-numeric facet answers with numbers", numeric["values"][:2])

# --------------------------------------------------------------------------- #
section("API")
# --------------------------------------------------------------------------- #
app = FastAPI()
app.include_router(aq.router)
client = TestClient(app)
lib_mod.build_library = lambda cfg=None: LIB
aq._config = lambda: {}          # never read the developer's config.json


def post(body):
    return client.post("/api/library/query", json=body)


r = post({"conditions": [cond("tags.GENRE", "eq", "shoegaze")], "limit": 5})
check_eq(r.status_code, 200, "POST /api/library/query is 200")
body = r.json()
check_eq(body["total"], len([tr for tr in TRACKS if tr["tags"]["GENRE"] == "shoegaze"]),
         "the endpoint's total matches the engine's")
check("took_ms" in body and body["took_ms"] >= 0, "took_ms is measured")
check(all("artist" in it and "album" in it and "album_path" in it
          for it in body["items"]), "track items carry artist/album/album_path")
check(all(it["artist"] == it["tags"]["ARTIST"] and it["album"] == it["tags"]["ALBUM"]
          and it["album_path"].startswith(it["artist_path"]) for it in body["items"]),
      "the stamped artist/album are the row's own")
check(all("group" not in it for it in body["items"]),
      "no group key is stamped when no group was asked for")

albums = post({"target": "albums", "conditions": [cond("album.year", "gte", 2000)]})
check_eq(albums.json()["total"], len(ALBUMS), "target:albums walks albums")
check(all("tracks" in it for it in albums.json()["items"]),
      "album items are the payload's album rows")

rated_facet = client.get("/api/library/facets", params={"field": "rating"}).json()
unrated = len([tr for tr in TRACKS if not half_of(tr)])
check_eq(rated_facet["missing"], unrated,
         "a rating facet rates through the store; unrated is the blank bucket")
check_eq(sum(v["count"] for v in rated_facet["values"]) + unrated, len(TRACKS),
         "rating facet buckets + unrated = every track")
check_eq({v["value"] for v in rated_facet["values"]},
         {h / 2.0 for h in {half_of(tr) for tr in TRACKS if half_of(tr)}},
         "rating facet values are the stars the store and tags actually hold")
rated_query = post({"conditions": [cond("rating", "gte", 4)], "limit": 0}).json()
check_eq(rated_query["total"], len([tr for tr in TRACKS if (half_of(tr) or 0) >= 8]),
         "the endpoint rates tracks through the store and its file tag")

facets_api = client.get("/api/library/facets", params={"field": "tags.GENRE"})
check_eq(facets_api.status_code, 200, "GET /api/library/facets is 200")
check_eq({v["value"]: v["count"] for v in facets_api.json()["values"]}, want_counts,
         "the facets endpoint agrees with the query response")
check_eq(client.get("/api/library/facets",
                    params={"field": "tags.GENRE", "q": "shoe"}).json()["values"],
         [{"value": "shoegaze", "count": want_counts["shoegaze"]}],
         "the q filter narrows the values")

fields = client.get("/api/library/fields")
check_eq(fields.status_code, 200, "GET /api/library/fields is 200")
groups = {g["id"]: g for g in fields.json()["groups"]}
check_eq(sorted(groups), sorted(["tags", "rating", "grade", "audit", "tech",
                                 "analysis", "library", "album", "artist"]),
         "the catalogue's groups are the contract's")
registry_tags = {f"tags.{t['key']}" for t in registry()["tags"]}
check_eq({f["field"] for f in groups["tags"]["fields"]}, registry_tags,
         "every registry tag is a catalogue field")
check_eq([f["field"] for f in groups["rating"]["fields"]], ["rating"],
         "exactly one rating field")
rating_def = groups["rating"]["fields"][0]
check_eq((rating_def["type"], rating_def["min"], rating_def["max"], rating_def["step"]),
         ("number", 0, 5, 0.5), "rating is 0-5 stars in halves")
check_eq([o["op"] for o in rating_def["ops"]],
         ["eq", "ne", "lt", "gt", "lte", "gte", "between", "is_unrated", "is_rated"],
         "rating advertises is_unrated/is_rated instead of missing/present")
# The folder-scope ratings sit in the album/artist groups and answer with the
# SAME unit and the SAME ops as the track one — one family, three scopes, with
# nothing for a builder to special-case.
for group_id, field in (("album", "album.rating"), ("artist", "artist.rating")):
    by_id = {f["field"]: f for f in groups[group_id]["fields"]}
    check(field in by_id, f"{field} is a field of the {group_id} group")
    entry = by_id.get(field, {})
    check_eq((entry.get("type"), entry.get("min"), entry.get("max"), entry.get("step")),
             ("number", 0, 5, 0.5), f"{field} is 0-5 stars in halves")
    check_eq([o["op"] for o in entry.get("ops") or []],
             [o["op"] for o in rating_def["ops"]],
             f"{field} advertises exactly the ops rating does")
BARE = {"rating", "grade_pct", "grade_pass", "issues", "audit"}
SCOPES = {"tags", "tech", "analysis", "library", "album", "artist"}
check(all(f["field"] in BARE or f["field"].split(".")[0] in SCOPES
          for g in fields.json()["groups"] for f in g["fields"]),
      "every field is either a payload key or dotted with a known scope")
check(all({"field", "label", "type", "ops", "facetable", "sortable", "targets",
           "in_payload"} <= set(f) for g in fields.json()["groups"] for f in g["fields"]),
      "every field carries the catalogue keys the builder reads")
ops = {o["op"] for g in fields.json()["groups"] for f in g["fields"] for o in f["ops"]}
check_eq(sorted(ops - set(q.OPERATORS)), [], "every advertised op is a real op")

# ── refusals ────────────────────────────────────────────────────────────────
for label, payload in (
    ("unknown field", {"conditions": [cond("tags.NOPE", "eq", "x")]}),
    ("unknown op", {"conditions": [cond("tags.GENRE", "sorta")]}),
    ("op not on that field", {"conditions": [cond("tags.GENRE", "issues_contain", "X")]}),
    ("range without two ends", {"conditions": [cond("tech.bitrate", "between", [1])]}),
    ("order op with a list", {"conditions": [cond("tech.bitrate", "lt", [1, 2])]}),
    ("invalid regex", {"conditions": [cond("tags.GENRE", "matches", "([")]}),
    ("unknown target", {"target": "labels"}),
    ("unknown group", {"group": "label"}),
    ("unknown sort key", {"sort": {"key": "tags.NOPE"}}),
    ("bad sort dir", {"sort": {"key": "rating", "dir": 0}}),
    ("bad limit", {"limit": -1}),
    ("facets on an unknown field", {"facets": ["nope.nope"]}),
):
    r = post(payload)
    check(r.status_code == 400 and "detail" in r.json(),
          f"{label} is refused with 400", f"HTTP {r.status_code}")
check_eq(client.get("/api/library/facets", params={"field": "tags.NOPE"}).status_code, 400,
         "the facets endpoint refuses an unknown field")

# --------------------------------------------------------------------------- #
section("smart-playlist equivalence")
# --------------------------------------------------------------------------- #
# The specs the old evaluator's suite would have used, with the paths the old
# implementation selected — written out from the ORIGINAL semantics (tags dict
# -> track payload -> tech, `all`/`any`, base_paths applied first).
SPECS = [
    ({"conditions": [], "match": "all"},
     [tr["path"] for tr in TRACKS]),
    ({"conditions": [{"field": "tags.GENRE", "op": "eq", "value": "shoegaze"}],
      "match": "all"},
     [tr["path"] for tr in TRACKS if tr["tags"].get("GENRE") == "shoegaze"]),
    ({"conditions": [{"field": "tags.GENRE", "op": "contains", "value": "DREAM"}],
      "match": "all"},
     [tr["path"] for tr in TRACKS
      if "dream" in str(tr["tags"].get("GENRE") or "").lower()]),
    ({"conditions": [{"field": "tech.bitrate", "op": "lte", "value": "128"}],
      "match": "all"},
     [tr["path"] for tr in TRACKS if tr["tech"].get("bitrate") <= 128]),
    ({"conditions": [{"field": "tech.length", "op": "gt", "value": 150}],
      "match": "all"},
     [tr["path"] for tr in TRACKS if tr["tech"]["length"] > 150]),
    ({"conditions": [{"field": "grade_pass", "op": "eq", "value": True}], "match": "all"},
     [tr["path"] for tr in TRACKS if tr["grade_pass"]]),
    ({"conditions": [{"field": "tags.GENRE", "op": "missing", "value": None}],
      "match": "all"},
     [tr["path"] for tr in TRACKS if not tr["tags"].get("GENRE")]),
    ({"conditions": [{"field": "tags.GENRE", "op": "present", "value": None}],
      "match": "all"},
     [tr["path"] for tr in TRACKS if tr["tags"].get("GENRE")]),
    ({"conditions": [{"field": "audit", "op": "ne", "value": "REAL"}], "match": "all"},
     [tr["path"] for tr in TRACKS if str(tr.get("audit")) != "REAL"]),
    ({"conditions": [{"field": "tags.DATE", "op": "gte", "value": "2004"}], "match": "all"},
     [tr["path"] for tr in TRACKS if (tr["tags"].get("DATE") or "") >= "2004"]),
    ({"conditions": [{"field": "tags.GENRE", "op": "eq", "value": "shoegaze"},
                     {"field": "tech.bitrate", "op": "lt", "value": 320}],
      "match": "any"},
     [tr["path"] for tr in TRACKS
      if tr["tags"].get("GENRE") == "shoegaze" or tr["tech"]["bitrate"] < 320]),
    ({"conditions": [{"field": "grade_pass", "op": "eq", "value": False},
                     {"field": "tags.GENRE", "op": "eq", "value": "idm"}],
      "match": "all"},
     [tr["path"] for tr in TRACKS
      if not tr["grade_pass"] and tr["tags"].get("GENRE") == "idm"]),
    ({"conditions": [{"field": "tags.MOOD", "op": "present", "value": None}],
      "match": "all"},
     [tr["path"] for tr in TRACKS if str(tr["tags"].get("MOOD") or "").strip()]),
]
for i, (spec, want) in enumerate(SPECS):
    check_eq(q.match_paths(LIB, spec), want, f"engine spec {i} matches the old semantics")

# A False bool is a VALUE: the old evaluator's "is empty" test never matched a
# track that HAS the flag, and the engine still resolves those two fields the
# same way (the browser asks `library.lyrics_present is false` instead).
check_eq(q.match_paths(LIB, {"conditions": [{"field": "lyrics_present",
                                            "op": "missing"}]}), [],
         "a False boolean is not an empty value (unchanged semantics)")

# base_paths: applied before the conditions, order preserved, and a 100k-sized
# base list must not be rescanned per track.
subset = [TRACKS[i]["path"] for i in range(0, len(TRACKS), 2)]
check_eq(q.match_paths(LIB, SPECS[2][0], subset), [p for p in subset
                                                   if p in SPECS[2][1]],
         "base_paths limits the walk before the conditions")

# The playlist store now answers from the engine: same spec, same paths.
pl.get_playlist = lambda pid, user="": {"kind": "smart", "filter": SPECS[9][0]}
check_eq(pl.evaluate_smart(1, LIB), SPECS[9][1],
         "server.playlists.evaluate_smart answers from mlo.query")
pl.get_playlist = lambda pid, user="": {"kind": "manual", "filter": None}
check_eq(pl.evaluate_smart(1, LIB), None, "a manual playlist still answers None")


store_half = aq.rating_source("")
check_eq(store_half(TRACKS[0]["path"], None), 10, "the store's half-stars are read")
check_eq(store_half("C:/music/nowhere.flac", "80"), 8,
         "a file RATING tag is adopted through from_tag, never compared raw")
check_eq(store_half("C:/music/nowhere.flac", None), None, "nothing rated is None")
check_eq(store_half.folder("album", ALBUMS[0]["path"]), 10,
         "the same source answers the ALBUM scope in half-stars")
check_eq(store_half.folder("artist", artist2["path"]), 10,
         "and the ARTIST scope")
check_eq(store_half.folder("album", ALBUMS[3]["path"]), None,
         "an album the user never rated is None, not 0")
check_eq(store_half.folder("artist", "C:/music/Nobody"), None,
         "and a folder the store has no row for is None as well")

folder_query = post({"target": "albums", "limit": 0,
                     "conditions": [cond("album.rating", "gte", 4)]})
check_eq(folder_query.status_code, 200, "album.rating is accepted by the endpoint")
check_eq(folder_query.json()["total"], len(five_albums),
         "the endpoint filters albums by the album rating")
check_eq(post({"target": "albums", "limit": 0,
               "conditions": [cond("album.rating", "is_unrated")]}).json()["total"],
         len(ALBUMS) - len(FOLDER["album"]),
         "…and counts the unrated albums through the same field")
check_eq(post({"limit": 0, "conditions": [cond("artist.rating", "gte", 4)]}).json()["total"],
         len(artist2_tracks),
         "the endpoint filters tracks by their artist's rating")
check_eq(post({"limit": 0, "conditions": [cond("album.rating", "matches", "x")]}).status_code,
         400,
         "an op outside the rating family's own set stays refused on those fields")


check_eq(pl._rating_of("", {"conditions": []}), None,
         "a rule with no rating condition never touches the store")
check(pl._rating_of("", {"conditions": [{"field": "album.rating"}]}) is not None,
      "a rule on an entity rating does pull the store in")
check_eq(q.match_paths(LIB, {"conditions": [{"field": "rating", "op": "gte",
                                             "value": 4}], "match": "all"},
                      rating_of=pl._rating_of("", {"conditions": [{"field": "rating"}]})),
         [tr["path"] for tr in TRACKS if (half_of(tr) or 0) >= 8],
         "the playlist path rates through the store")
check_eq(q.match_paths(LIB, {"conditions": [{"field": "album.rating", "op": "gte",
                                             "value": 4}], "match": "all"},
                      rating_of=pl._rating_of("", {"conditions": [{"field": "album.rating"}]})),
         [tr["path"] for alb in ALBUMS if FOLDER["album"].get(alb["path"], 0) >= 8
          for tr in alb["tracks"]],
         "and a smart playlist filters on the album rating the same way")

# --------------------------------------------------------------------------- #
section("performance (100k tracks)")
# --------------------------------------------------------------------------- #
big = make_library(albums=1000, per_album=100)
count = sum(len(alb["tracks"]) for art in big["artists"] for alb in art["albums"])
check_eq(count, 100000, "the big fixture holds 100k tracks")

started = time.perf_counter()
res = q.run(big, {"conditions": [cond("tags.GENRE", "eq", "shoegaze"),
                                 cond("tech.bitrate", "lt", 320)],
                  "match": "all", "sort": {"key": "tech.bitrate", "dir": -1},
                  "group": "artist", "facets": ["tags.GENRE"], "limit": 200},
            rating_of=rating_of, bool_fields=aq.bool_fields())
full_ms = (time.perf_counter() - started) * 1000.0
print(f"  100k tracks: 2 conditions + facet + group + sort in {full_ms:.0f} ms "
      f"({res['total']} matches, took_ms={res['took_ms']})")
check(full_ms < 15000, "a 100k query finishes in seconds, not minutes", f"{full_ms:.0f} ms")

quarter = make_library(albums=250, per_album=100)
t0 = time.perf_counter()
q.run(quarter, {"conditions": [cond("tags.GENRE", "eq", "shoegaze")], "match": "all"},
      rating_of=rating_of)
small_ms = (time.perf_counter() - t0) * 1000.0
t0 = time.perf_counter()
q.run(big, {"conditions": [cond("tags.GENRE", "eq", "shoegaze")], "match": "all"},
      rating_of=rating_of)
big_ms = (time.perf_counter() - t0) * 1000.0
ratio = big_ms / max(small_ms, 0.001)
print(f"  25k {small_ms:.0f} ms -> 100k {big_ms:.0f} ms (ratio {ratio:.1f}x)")
check(ratio < 12, "the walk scales with the library, not quadratically", f"ratio {ratio:.1f}")

# --------------------------------------------------------------------------- #
section("wiring")
# --------------------------------------------------------------------------- #
# server/main.py's include_router block belongs to the coordinator; this says
# what it must hold rather than failing on someone else's file.
with open(os.path.join(ROOT, "server", "main.py"), encoding="utf-8") as fh:
    main_src = fh.read()
if "api_query.router" not in main_src:
    print("  NOTE server/main.py does not register server.api_query's router yet"
          " — the coordinator adds: from server import api_query /"
          " app.include_router(api_query.router)")
else:
    try:
        from server import main as main_mod
        # The OpenAPI schema is the app's own answer for "what is routed": an
        # included router does not flatten into app.routes on this FastAPI.
        registered = set(main_mod.app.openapi().get("paths") or {})
        check({"/api/library/query", "/api/library/facets", "/api/library/fields"}
              <= registered,
              "the running app registers all three query routes",
              sorted(p for p in registered if "library" in p))
    except Exception as exc:                      # another slice mid-flight
        print(f"  NOTE could not import server.main ({exc}); source check only")

print(f"\n{'PASS' if not fails else 'FAIL'} — {len(fails)} problem(s)")
sys.exit(1 if fails else 0)
