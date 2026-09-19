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

REL_MBID = "0b1a2c3d-4e5f-6789-abcd-ef0123456789"
TRACK_MBID = "3e4d5c6b-7a89-0123-cdef-234567890123"

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

T1 = track(ALBUM, "01 - one.flac", {
    "ARTIST": "Credits Artist", "ALBUM": "Credits Album",
    "ALBUMARTIST": "Credits Artist", "MUSICBRAINZ_ALBUMID": REL_MBID})
T2 = track(ALBUM, "02 - two.flac", {
    "ARTIST": "Credits Artist", "ALBUM": "Credits Album",
    "ALBUMARTIST": "Credits Artist", "MUSICBRAINZ_ALBUMID": REL_MBID})
FALLBACK = track(TAGS_ONLY_ALBUM, "01 - tagged.flac", {
    "ARTIST": "Credits Artist", "ALBUM": "No MB Album",
    "PERFORMER": "Dave (double bass)", "COMPOSER": "Eve"})
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


def work(mbid, title):
    return {"type": "performance", "target-type": "work",
            "attributes": ["performance"],
            "work": {"id": mbid, "title": title}}


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
MB_ANSWER[f"release/{REL_MBID}"] = {
    "relations": [credit("a-1", "Alice", "producer")],
    "media": [
        {"tracks": [
            {"recording": {"relations": [
                credit("b-1", "Bob", "engineer"),
                work("w-1", "Credits Song")]}},
            {"recording": {"relations": [
                credit("b-1", "Bob", "engineer"),
                credit("c-1", "Carol", "performer", ["double bass"])]}},
        ]},
    ],
}
ROW_KEYS = {"role", "attributes", "artist", "mbid"}


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
    assert set(roles) == {"engineer", "performer", "producer", "work"}, rows
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
# a track file: its own recording, never the release
# --------------------------------------------------------------------------- #
MB_ANSWER[f"recording/{TRACK_MBID}"] = {
    "relations": [credit("b-1", "Bob", "engineer"),
                  credit("c-1", "Carol", "performer", ["lead vocals"])]}
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


# --------------------------------------------------------------------------- #
# refusals: MB's reason is a 502, the guard is 400, no args is 404
# --------------------------------------------------------------------------- #
def test_mb_refusal_is_502():
    MB_ANSWER[f"release/{REL_MBID}"] = intg.MusicBrainzError(
        "MusicBrainz 503 Service Unavailable after 5 attempts")
    try:
        r = get(album=slash(ALBUM))
    finally:
        MB_ANSWER[f"release/{REL_MBID}"] = {
            "relations": [credit("a-1", "Alice", "producer")], "media": []}
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
        ("track: recording request", test_track_recording_request),
        ("tags: fallback, no request", test_tag_fallback_no_request),
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
