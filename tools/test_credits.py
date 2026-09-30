#!/usr/bin/env python3
"""Credits / performers route (GET /api/credits) — offline contract.

The panel's promise, pinned here with BOTH seams stubbed (integrations.
mb_get_cached and tagcache.read_track; no socket is opened):

  * an ALBUM dir costs exactly ONE MusicBrainz request (`release/<id>` with
    the relations included) however many tracks it holds, answers
    `release_mbid`, groups rows by role and keeps a player
    who appears on two tracks ONCE;
  * a track FILE asks `recording/<mbid>` and answers `track_mbid` (never a
    `release_mbid`);
  * a file whose tags carry PERFORMER/COMPOSER but no MBID is answered from
    the files' own tags (`source: "tags"`) with ZERO MB requests, and Vorbis'
    `"Name (instrument)"` is split back into artist + attribute;
  * a work relation surfaces as its own `{role: "work", …}` row;
  * `rows` merge the RELEASE's relations, every RECORDING's and the relations
    of the WORK each recording names — `tidy_credit_rows` only normalises
    (lower-case role, dedupe), so no role and no attribute is ever dropped;
  * every reply carries an `identity` object (the album/track's own tags plus
    the MBIDs the route resolved) whose unknown fields are `""` — never
    absent, never `None`;
  * every row is exactly `{role, attributes, artist, mbid}`;
  * MusicBrainz refusing is 502 carrying MB's own reason — never a 500 — and
    a path outside the music folder is 400, neither `path` nor `album` 404.

Run:  python tools/test_credits.py
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --------------------------------------------------------------------------- #
# hermeticity: seed the music folder BEFORE server.main is imported, and point
# mlo.config at a stub config file so the app never reads the developer's real
# library or writes into its .mlo/data.
# --------------------------------------------------------------------------- #
REDIRECT = tempfile.mkdtemp(prefix="mlo-credits-redirect-")
os.environ["MLO_MUSIC_FOLDER"] = REDIRECT

import mlo.config as cfgmod  # noqa: E402
import mlo.paths as pathmod  # noqa: E402

_STUB_CFG = os.path.join(REDIRECT, "config.json")
with open(_STUB_CFG, "w", encoding="utf-8") as f:
    json.dump({"music_folder": REDIRECT}, f)

cfgmod.CONFIG_FILE = _STUB_CFG
pathmod.CONFIG_FILE = _STUB_CFG

import server.integrations as intg  # noqa: E402
import server.tagcache as tagcache  # noqa: E402
from server import main as mlo_main  # noqa: E402  (heavy import)
from fastapi.testclient import TestClient  # noqa: E402

MF = tempfile.mkdtemp(prefix="mlo-credits-music-")
OUTSIDE = tempfile.mkdtemp(prefix="mlo-credits-outside-")
ALBUM = os.path.join(MF, "Artists", "Credits Artist", "Credits Album")
TAGS_ONLY_ALBUM = os.path.join(MF, "Artists", "Credits Artist", "No MB Album")
BARE_ALBUM = os.path.join(MF, "Artists", "Credits Artist", "Bare Album")

REL_MBID = "0b1a2c3d-4e5f-6789-abcd-ef0123456789"
TRACK_MBID = "3e4d5c6b-7a89-0123-cdef-234567890123"
ARTIST_MBID = "7f8e9d0c-1b2a-3456-7890-abcdef012345"

mlo_main.load_config = lambda: {"music_folder": MF}
_client = TestClient(mlo_main.app)


def track(album_dir, name, tags):
    os.makedirs(album_dir, exist_ok=True)
    p = os.path.normpath(os.path.join(album_dir, name))
    with open(p, "wb") as fh:
        fh.write(b"")                      # only its PATH and tags matter here
    TAGS[p] = dict(tags)
    return p


# --------------------------------------------------------------------------- #
# the two stub seams
# --------------------------------------------------------------------------- #
TAGS = {}

# the album facts EVERY file of the album carries (a real album's tags), so an
# album request's `identity` reads the same values whichever file it takes them
# from — and a track request's identity states the same album facts.
ALBUM_TAGS = {
    "ARTIST": "Credits Artist", "ALBUM": "Credits Album",
    "ALBUMARTIST": "Credits Artist", "MUSICBRAINZ_ALBUMID": REL_MBID,
    "MUSICBRAINZ_ARTISTID": ARTIST_MBID,
    "CATALOGNUMBER": "CRED-001", "LABEL": "Credits Label",
    "BARCODE": "0123456789012", "DATE": "1994-03-01",
    "ORIGINALDATE": "1990-05-02", "RELEASECOUNTRY": "GB",
    "RELEASETYPE": "Album", "MEDIA": "CD"}

T1 = track(ALBUM, "01 - one.flac", dict(ALBUM_TAGS, TITLE="One"))
T2 = track(ALBUM, "02 - two.flac", dict(ALBUM_TAGS, TITLE="Two"))
FALLBACK = track(TAGS_ONLY_ALBUM, "01 - tagged.flac", {
    "ARTIST": "Credits Artist", "ALBUM": "No MB Album",
    "PERFORMER": "Dave (double bass)", "COMPOSER": "Eve"})
# no MusicBrainz id AND no credit tag on this album's file: the identity is
# still there, and that is no reason to answer 404 about it.
BARE = track(BARE_ALBUM, "01 - bare.flac", {
    "ARTIST": "Bare Artist", "ALBUM": "Bare Album", "TITLE": "Bare One"})
LOOSE = track(OUTSIDE, "loose.flac", {})

_real_read_track = tagcache.read_track
tagcache.read_track = \
    lambda path, tag_list=None: (dict(TAGS.get(os.path.normpath(path)) or {}), {})

MB_CALLS = []
MB_ANSWER = {}

_real_mb_get_cached = intg.mb_get_cached


def _fake_mb_get_cached(endpoint, params=None, timeout=30.0, retries=5):
    """The MB seam: record the endpoint, answer (or refuse) from MB_ANSWER."""
    MB_CALLS.append(endpoint)
    answer = MB_ANSWER.get(endpoint)
    if isinstance(answer, Exception):
        raise answer
    return answer


intg.mb_get_cached = _fake_mb_get_cached


def credit(mbid, name, kind, attributes=()):
    """One MusicBrainz artist-relation, the shape mb_get returns."""
    return {"type": kind, "target-type": "artist",
            "attributes": list(attributes),
            "artist": {"name": name, "id": mbid}}


def work(mbid, title, relations=()):
    """One work relation, carrying the WORK's own relations the way MB's
    `work-level-rels` inlines them into the very same response."""
    return {"type": "performance", "target-type": "work",
            "attributes": ["performance"],
            "work": {"id": mbid, "title": title,
                     "relations": [dict(r) for r in relations]}}


# the WORK's own relations: `composer` and `lyricist` — two roles neither the
# release nor any recording states on its own, so the album request can only
# show them by merging the works its recordings name.
WORK_RELATIONS = [credit("d-1", "Frank", "composer"),
                  credit("e-1", "Gina", "lyricist")]


def get(**params):
    return _client.get("/api/credits", params=params)


def slash(p):
    return p.replace("\\", "/")


FAILED = []


def check(label, fn):
    """Run one labeled case; a failed assertion is reported, not fatal."""
    try:
        fn()
    except AssertionError as e:
        FAILED.append(label)
        print(f"FAIL {label}: {e}")
    else:
        print(f"ok   {label}")


# --------------------------------------------------------------------------- #
# an album: ONE release request, grouped + de-duplicated rows
# --------------------------------------------------------------------------- #
RELEASE_ANSWER = {
    "relations": [credit("a-1", "Alice", "producer"),
                  # an unusual role and a free-text instrument: `tidy_credit_rows`
                  # normalises but drops neither, so both must reach the reply
                  credit("f-1", "Hana", "Miscellaneous Support"),
                  credit("g-1", "Ivan", "performer", ["ney", "bendir"])],
    "media": [
        {"tracks": [
            {"recording": {"relations": [
                credit("b-1", "Bob", "engineer"),
                work("w-1", "Credits Song", WORK_RELATIONS)]}},
            {"recording": {"relations": [
                credit("b-1", "Bob", "engineer"),
                credit("c-1", "Carol", "performer", ["double bass"]),
                # the SAME work, named by a second track: its own relations
                # arrive twice and must still be ONE row each
                work("w-1", "Credits Song", WORK_RELATIONS)]}},
        ]},
    ],
}
MB_ANSWER[f"release/{REL_MBID}"] = RELEASE_ANSWER
ROW_KEYS = {"role", "attributes", "artist", "mbid"}
# every key the reply's `identity` block MUST carry, `""` where the tags and
# the MBIDs the route resolved state nothing — never missing, never `None`.
IDENTITY_KEYS = {
    "title", "artist", "album", "album_artist", "catalog_number", "label",
    "barcode", "date", "original_date", "country", "release_type", "media",
    "track_mbid", "release_mbid", "release_group_mbid", "artist_mbid",
    "recording_mbid", "path",
}


def test_album_one_request_and_rows():
    MB_CALLS.clear()
    r = get(album=slash(ALBUM))
    assert r.status_code == 200, r.text
    body = r.json()
    # one request for the whole album, of the release itself
    assert MB_CALLS == [f"release/{REL_MBID}"], MB_CALLS
    assert body["release_mbid"] == REL_MBID, body
    assert "track_mbid" not in body, body
    # `cached` used to answer "did this come from MusicBrainz?" — which is what
    # `source` already says and the UI already renders. Gone rather than kept
    # as a second name for the same fact.
    assert "cached" not in body, body
    assert body["source"] == "musicbrainz", body
    assert body["artist"] == "Credits Artist" and body["album"] == "Credits Album", body


def test_rows_shape_grouped_and_deduped():
    body = get(album=slash(ALBUM)).json()
    rows = body["rows"]
    assert rows, body
    for row in rows:
        assert set(row) == ROW_KEYS, row                          # no extras
        assert isinstance(row["attributes"], list), row
    # the role groups it, in role order
    roles = [row["role"] for row in rows]
    assert roles == sorted(roles), roles
    assert set(roles) == {"composer", "engineer", "lyricist",
                          "miscellaneous support", "performer", "producer",
                          "work"}, rows
    # an engineer on TWO tracks is one row
    bobs = [row for row in rows if row["artist"] == "Bob"]
    assert bobs == [{"role": "engineer", "attributes": [], "artist": "Bob",
                     "mbid": "b-1"}], bobs
    # the instrument rides in attributes; the release-level relation survives
    assert {"role": "performer", "attributes": ["double bass"],
            "artist": "Carol", "mbid": "c-1"} in rows, rows
    assert {"role": "producer", "attributes": [], "artist": "Alice",
            "mbid": "a-1"} in rows, rows


def test_work_relation_is_a_work_row():
    rows = get(album=slash(ALBUM)).json()["rows"]
    works = [row for row in rows if row["role"] == "work"]
    assert works == [{"role": "work", "attributes": ["performance"],
                      "artist": "Credits Song", "mbid": "w-1"}], works


# --------------------------------------------------------------------------- #
# the identity block: always an object, tag-carried, "" where unknown
# --------------------------------------------------------------------------- #
def test_album_identity_present_and_filled():
    body = get(album=slash(ALBUM)).json()
    ident = body["identity"]
    assert isinstance(ident, dict), body                 # present, never absent
    assert IDENTITY_KEYS <= set(ident), sorted(ident)
    # one string per field, so an unknown field is "" and never None
    assert all(isinstance(v, str) for v in ident.values()), ident
    # the album's own name heads the panel (the files' ALBUM tag here), so
    # `title` and `album` state the same string
    assert ident["title"] == ident["album"] == "Credits Album", ident
    assert ident["title"] == body["album"], (ident, body)
    assert ident["artist"] == "Credits Artist", ident
    assert ident["album_artist"] == "Credits Artist", ident
    # every album fact the fixture's tags state
    assert ident["catalog_number"] == "CRED-001", ident
    assert ident["label"] == "Credits Label", ident
    assert ident["barcode"] == "0123456789012", ident
    assert ident["date"] == "1994-03-01", ident
    assert ident["original_date"] == "1990-05-02", ident
    assert ident["country"] == "GB", ident
    assert ident["release_type"] == "Album", ident
    assert ident["media"] == "CD", ident
    # the ids the route resolved, and the folder the panel was opened on
    assert ident["release_mbid"] == REL_MBID == body["release_mbid"], ident
    assert ident["artist_mbid"] == ARTIST_MBID, ident
    assert os.path.normpath(ident["path"]) == ALBUM, ident
    # an album has no single recording, and no file states a release group
    assert ident["track_mbid"] == ident["recording_mbid"] == "", ident
    assert ident["release_group_mbid"] == "", ident


# the two roles only the WORK states — the release's own relations and every
# recording's were left without them, so a reply can only show them by having
# merged the works its recordings name.
WORK_ROWS = [{"role": "composer", "attributes": [], "artist": "Frank",
              "mbid": "d-1"},
             {"role": "lyricist", "attributes": [], "artist": "Gina",
              "mbid": "e-1"}]


def test_work_relations_surface_on_album_and_track():
    album_rows = get(album=slash(ALBUM)).json()["rows"]
    for row in WORK_ROWS:
        assert row in album_rows, album_rows
    track_rows = get(path=slash(T1)).json()["rows"]
    for row in WORK_ROWS:
        assert row in track_rows, track_rows
    # BOTH tracks name that one work, so its relations arrive twice and each
    # person is still exactly ONE row (the existing dedupe)
    for row in WORK_ROWS:
        found = [r for r in album_rows if r["artist"] == row["artist"]]
        assert found == [row], (row, found)


def test_tidy_credit_rows_is_non_destructive():
    # an unusual role and a free-text instrument, both stated by the release
    # MB_ANSWER answers with: normalising may not drop either
    rows = get(album=slash(ALBUM)).json()["rows"]
    unusual = [r for r in rows if r["role"] == "miscellaneous support"]
    assert unusual == [{"role": "miscellaneous support", "attributes": [],
                        "artist": "Hana", "mbid": "f-1"}], unusual
    free_text = [r for r in rows if r["artist"] == "Ivan"]
    assert free_text == [{"role": "performer", "attributes": ["ney", "bendir"],
                         "artist": "Ivan", "mbid": "g-1"}], free_text
    # and tidy itself only normalises: lower-case role, one row per repeat,
    # nothing else touched
    raw = [{"role": "Miscellaneous Support", "attributes": [], "artist": "Hana",
            "mbid": "f-1"},
           {"role": "performer", "attributes": ["ney", "bendir"],
            "artist": "Ivan", "mbid": "g-1"},
           {"role": "performer", "attributes": ["ney", "bendir"],
            "artist": "Ivan", "mbid": "g-1"}]
    assert intg.tidy_credit_rows(raw) == [
        {"role": "miscellaneous support", "attributes": [], "artist": "Hana",
         "mbid": "f-1"},
        {"role": "performer", "attributes": ["ney", "bendir"], "artist": "Ivan",
         "mbid": "g-1"}], intg.tidy_credit_rows(raw)


# --------------------------------------------------------------------------- #
# a track file: its own recording, never the release
# --------------------------------------------------------------------------- #
MB_ANSWER[f"recording/{TRACK_MBID}"] = {
    "relations": [credit("b-1", "Bob", "engineer"),
                  credit("c-1", "Carol", "performer", ["lead vocals"]),
                  # the recording names the same WORK the album's track does:
                  # its composer and lyricist come from the work, not here
                  work("w-1", "Credits Song", WORK_RELATIONS)]}
TAGS[T1]["MUSICBRAINZ_TRACKID"] = TRACK_MBID


def test_track_recording_request():
    MB_CALLS.clear()
    r = get(path=slash(T1))
    assert r.status_code == 200, r.text
    body = r.json()
    assert MB_CALLS == [f"recording/{TRACK_MBID}"], MB_CALLS
    assert body["track_mbid"] == TRACK_MBID, body
    assert "release_mbid" not in body, body
    # `cached` was a second name for `source`; it is gone (and the UI never
    # read it), so the source label is the whole contract here.
    assert body["source"] == "musicbrainz" and "cached" not in body, body
    assert {"role": "performer", "attributes": ["lead vocals"],
            "artist": "Carol", "mbid": "c-1"} in body["rows"], body


def test_track_identity_present_and_filled():
    body = get(path=slash(T1)).json()
    ident = body["identity"]
    assert isinstance(ident, dict), body                 # present, never absent
    assert IDENTITY_KEYS <= set(ident), sorted(ident)
    assert all(isinstance(v, str) for v in ident.values()), ident
    # the file's OWN TITLE, and the album facts its tags carry
    assert ident["title"] == "One", ident
    assert ident["artist"] == "Credits Artist", ident
    assert ident["album"] == "Credits Album", ident
    assert ident["album_artist"] == "Credits Artist", ident
    assert ident["catalog_number"] == "CRED-001", ident
    assert ident["label"] == "Credits Label", ident
    assert ident["barcode"] == "0123456789012", ident
    assert ident["date"] == "1994-03-01", ident
    assert ident["original_date"] == "1990-05-02", ident
    assert ident["country"] == "GB", ident
    assert ident["release_type"] == "Album", ident
    assert ident["media"] == "CD", ident
    # the recording id, under both names the panel reads it by, and the ids
    # the route resolved
    assert ident["track_mbid"] == ident["recording_mbid"] == TRACK_MBID, ident
    # A track's reply names its own recording at the top level (`track_mbid`);
    # the release it sits on is named by the album ID tag, which the identity
    # block is the only place to carry — the body has no `release_mbid` key on
    # this branch, exactly as the album branch has no `track_mbid`.
    assert ident["track_mbid"] == body["track_mbid"], ident
    assert "release_mbid" not in body, body
    assert ident["release_mbid"] == REL_MBID, ident
    assert ident["artist_mbid"] == ARTIST_MBID, ident
    assert os.path.normpath(ident["path"]) == T1, ident
    # nothing states a release-group id: unknown is "", never None
    assert ident["release_group_mbid"] == "", ident


# --------------------------------------------------------------------------- #
# no MBID: the files' own tags, and not one request
# --------------------------------------------------------------------------- #
def test_tag_fallback_no_request():
    MB_CALLS.clear()
    r = get(path=slash(FALLBACK))
    assert r.status_code == 200, r.text
    body = r.json()
    assert MB_CALLS == [], MB_CALLS                     # the fallback is offline
    assert body["source"] == "tags", body
    assert "cached" not in body, body
    assert body["track_mbid"] == "" and "release_mbid" not in body, body
    # Vorbis' "Name (instrument)" comes back apart: artist + attribute
    assert {"role": "performer", "attributes": ["double bass"],
            "artist": "Dave", "mbid": ""} in body["rows"], body
    assert {"role": "composer", "attributes": [], "artist": "Eve",
            "mbid": ""} in body["rows"], body


def test_identity_offline_for_tag_only_track():
    body = get(path=slash(FALLBACK)).json()
    assert body["source"] == "tags", body
    ident = body["identity"]
    assert isinstance(ident, dict), body                 # present, never absent
    assert IDENTITY_KEYS <= set(ident), sorted(ident)
    assert all(isinstance(v, str) for v in ident.values()), ident
    assert ident["artist"] == "Credits Artist", ident
    assert ident["album"] == "No MB Album", ident
    assert os.path.normpath(ident["path"]) == FALLBACK, ident
    # no MusicBrainz id anywhere, and still no None and no missing key
    assert [ident[k] for k in ("track_mbid", "release_mbid",
                               "release_group_mbid", "artist_mbid",
                               "recording_mbid")] == [""] * 5, ident
    # a fact no tag states is "" rather than absent
    assert ident["date"] == "" and ident["catalog_number"] == "", ident


def test_identity_offline_for_tag_only_album():
    MB_CALLS.clear()
    body = get(album=slash(BARE_ALBUM)).json()
    # no MB id and no credit tag on the file: an EMPTY row list is an answer
    # here, not a 404 — and the header still names the album the tags state
    assert body["source"] == "tags" and body["rows"] == [], body
    assert MB_CALLS == [], MB_CALLS
    ident = body["identity"]
    assert isinstance(ident, dict), body                 # present, never absent
    assert IDENTITY_KEYS <= set(ident), sorted(ident)
    assert all(isinstance(v, str) for v in ident.values()), ident
    assert ident["title"] == ident["album"] == "Bare Album", ident
    assert ident["artist"] == "Bare Artist", ident
    assert [ident[k] for k in ("track_mbid", "release_mbid",
                               "release_group_mbid", "artist_mbid",
                               "recording_mbid")] == [""] * 5, ident


# --------------------------------------------------------------------------- #
# refusals: MB's reason is a 502, the guard is 400, no args is 404
# --------------------------------------------------------------------------- #
def test_mb_refusal_is_502():
    MB_ANSWER[f"release/{REL_MBID}"] = intg.MusicBrainzError(
        "MusicBrainz 503 Service Unavailable after 5 attempts")
    try:
        r = get(album=slash(ALBUM))
    finally:
        MB_ANSWER[f"release/{REL_MBID}"] = RELEASE_ANSWER
    assert r.status_code == 502, (r.status_code, r.text)
    assert "503" in r.json()["detail"], r.text          # MB's own reason
    assert "MusicBrainz credits unavailable" in r.json()["detail"], r.text


def test_outside_music_folder_is_400():
    r = get(path=slash(LOOSE))
    assert r.status_code == 400, (r.status_code, r.text)
    assert "outside music folder" in r.json()["detail"], r.text


def test_no_path_is_404():
    r = _client.get("/api/credits")
    assert r.status_code == 404, (r.status_code, r.text)
    assert "path or album is required" in r.json()["detail"], r.text


try:
    for label, fn in [
        ("album: one release request", test_album_one_request_and_rows),
        ("album: rows shape/grouped/deduped", test_rows_shape_grouped_and_deduped),
        ("album: work relation", test_work_relation_is_a_work_row),
        ("album: identity present + filled",
         test_album_identity_present_and_filled),
        ("album+track: work's own roles merged",
         test_work_relations_surface_on_album_and_track),
        ("tidy: unusual role + free-text attribute kept",
         test_tidy_credit_rows_is_non_destructive),
        ("track: recording request", test_track_recording_request),
        ("track: identity present + filled",
         test_track_identity_present_and_filled),
        ("tags: fallback, no request", test_tag_fallback_no_request),
        ("tags: identity offline, track",
         test_identity_offline_for_tag_only_track),
        ("tags: identity offline, empty rows is an answer",
         test_identity_offline_for_tag_only_album),
        ("mb 503 -> 502", test_mb_refusal_is_502),
        ("outside music folder -> 400", test_outside_music_folder_is_400),
        ("no path/album -> 404", test_no_path_is_404),
    ]:
        check(label, fn)
finally:
    tagcache.read_track = _real_read_track
    intg.mb_get_cached = _real_mb_get_cached
    os.environ.pop("MLO_MUSIC_FOLDER", None)
    shutil.rmtree(MF, ignore_errors=True)
    shutil.rmtree(OUTSIDE, ignore_errors=True)
    shutil.rmtree(REDIRECT, ignore_errors=True)

if FAILED:
    print(f"credits: {len(FAILED)} check(s) failed: {', '.join(FAILED)}")
    sys.exit(1)
print("credits: all assertions passed")
