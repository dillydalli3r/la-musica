#!/usr/bin/env python3
"""Credits / performers route (GET /api/credits) — offline contract.

Credits are read STRICTLY from the files' own tags (the owner's rule,
2026-10-18: "all credits in the app should be read strictly from local tags,
no musicbrainz querying every time"), pinned here with the seams stubbed
(tagcache.read_track; no socket is opened):

  * a TRACK and an ALBUM both answer from the files' own credit tags alone —
    `source: "tags"`, ZERO MusicBrainz requests — and Vorbis'
    `"Name (instrument)"` is split back into artist + attribute;
  * every row is `{role, attributes, artist, mbid, source: "file"}`: the
    MusicBrainz MBID column is "" because a tag states a name, not an id;
  * an album unions its tracks' rows and keeps a person who appears on two
    tracks ONCE;
  * every reply carries an `identity` object (the album/track's own tags plus
    the MBIDs those tags state) whose unknown fields are `""` — never absent,
    never `None`;
  * a dead MusicBrainz cannot change any answer: the seam is never called;
  * a path outside the music folder is 400, neither `path` nor `album` 404,
    and an unreadable file is a 200 with no rows.

Run:  python tools/test_credits.py
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
# A file whose OWN tags carry the MusicBrainz vocabulary (roles, ids, the
# release facts), so the FILE rows can be told apart from the live ones and
# de-duplicated against them. Carol appears BOTH here (PERFORMER) and in the
# live recording relations, so the dedupe is exercised by name.
FR_MBID = "11112222-3333-4444-5555-666677778888"
FR = track(ALBUM, "03 - file rows.flac", dict(
    ALBUM_TAGS, TITLE="Three", MUSICBRAINZ_TRACKID=FR_MBID,
    PERFORMER="Carol (lead vocals); Dave (double bass)",
    PRODUCER="Alice", COMPOSER="Frank", WORK="Credits Song",
    ISRC="USRC17607839"))

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


#### --------------------------------------------------------------------------- #
# NO MusicBrainz request is ever made — the seam is a tripwire
# --------------------------------------------------------------------------- #
RELEASE_ANSWER = {}
MB_CALLS = []


def _tripwire(endpoint, params=None, timeout=30.0, retries=5):
    MB_CALLS.append(endpoint)
    raise AssertionError(f"a credits read must not ask MusicBrainz ({endpoint})")


intg.mb_get_cached = _tripwire

ROW_KEYS = {"role", "attributes", "artist", "mbid", "source"}
# every key the reply's `identity` block MUST carry, `""` where the tags
# state nothing — never missing, never `None`.
IDENTITY_KEYS = {
    "title", "artist", "album", "album_artist", "catalog_number", "label",
    "barcode", "date", "original_date", "country", "release_type", "media",
    "track_mbid", "release_mbid", "release_group_mbid", "artist_mbid",
    "recording_mbid", "path",
}


def test_tidy_credit_rows_is_non_destructive():
    # an unusual role and a free-text instrument: normalising may not drop
    # either — it lower-cases the role and folds repeats, nothing else
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
# a track file: its own tags, and not one request
# --------------------------------------------------------------------------- #
# The file carries the roles AND an MBID — the id must not turn into a request.
T1_TAGS = dict(TAGS[T1], MUSICBRAINZ_TRACKID=TRACK_MBID,
               PERFORMER="Carol (lead vocals)", ENGINEER="Bob")
TAGS[T1].update(T1_TAGS)


def test_track_reads_tags_only():
    MB_CALLS.clear()
    r = get(path=slash(T1))
    assert r.status_code == 200, r.text
    body = r.json()
    assert MB_CALLS == [], MB_CALLS
    assert body["source"] == "tags", body
    assert body["track_mbid"] == TRACK_MBID, body
    assert "release_mbid" not in body, body
    # The roles the file states, and the release facts every file carries as
    # rows of their own (label, catalogue number, barcode, ids…) — all of it
    # from the tags, the ids included.
    for row in (
            {"role": "engineer", "attributes": [], "artist": "Bob", "mbid": "",
             "source": "file"},
            {"role": "performer", "attributes": ["lead vocals"],
             "artist": "Carol", "mbid": "", "source": "file"},
            {"role": "musicbrainz recording id", "attributes": [],
             "artist": TRACK_MBID, "mbid": "", "source": "file"}):
        assert row in body["rows"], (row, body)


def test_track_identity_present_and_filled():
    body = get(path=slash(T1)).json()
    ident = body["identity"]
    assert isinstance(ident, dict), body                 # present, never absent
    assert IDENTITY_KEYS <= set(ident), sorted(ident)
    assert all(isinstance(v, str) for v in ident.values()), ident
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
    assert ident["track_mbid"] == ident["recording_mbid"] == TRACK_MBID, ident
    assert ident["track_mbid"] == body["track_mbid"], ident
    assert "release_mbid" not in body, body
    assert ident["release_mbid"] == REL_MBID, ident
    assert ident["artist_mbid"] == ARTIST_MBID, ident
    assert os.path.normpath(ident["path"]) == T1, ident
    assert ident["release_group_mbid"] == "", ident


# --------------------------------------------------------------------------- #
# no MBID: the files' own tags, still not one request
# --------------------------------------------------------------------------- #
def test_tag_fallback_no_request():
    MB_CALLS.clear()
    r = get(path=slash(FALLBACK))
    assert r.status_code == 200, r.text
    body = r.json()
    assert MB_CALLS == [], MB_CALLS
    assert body["source"] == "tags", body
    assert "cached" not in body, body
    assert body["track_mbid"] == "" and "release_mbid" not in body, body
    # Vorbis' "Name (instrument)" comes back apart: artist + attribute — and
    # every row here is FILE-sourced, so each carries `source: "file"`.
    assert {"role": "performer", "attributes": ["double bass"],
            "artist": "Dave", "mbid": "", "source": "file"} in body["rows"], body
    assert {"role": "composer", "attributes": [], "artist": "Eve",
            "mbid": "", "source": "file"} in body["rows"], body


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
    assert [ident[k] for k in ("track_mbid", "release_mbid",
                               "release_group_mbid", "artist_mbid",
                               "recording_mbid")] == [""] * 5, ident
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
# an ALBUM: every file's credit tags, unioned, one row per person
# --------------------------------------------------------------------------- #
def test_album_rows_from_tags():
    other = track(ALBUM, "04 - four.flac",
                  dict(ALBUM_TAGS, TITLE="Four", PERFORMER="Carol (lead vocals)",
                       PRODUCER="Alice"))
    body = get(album=slash(ALBUM)).json()
    rows = body["rows"]
    assert body["source"] == "tags", body
    # The album branch normalises its union through `tidy_credit_rows`, which
    # keeps the four keys a row has always had (see the track branch for the
    # `source` label the panel reads off a single file's rows).
    for row in rows:
        assert set(row) == {"role", "attributes", "artist", "mbid"}, row
        assert isinstance(row["attributes"], list), row
    roles = [row["role"] for row in rows]
    assert roles == sorted(roles), roles
    # Carol is on two files and carries no instrument: ONE row, not two
    carols = [r for r in rows if r["artist"] == "Carol"]
    assert carols == [{"role": "performer", "attributes": ["lead vocals"],
                       "artist": "Carol", "mbid": ""}], carols
    assert {"role": "producer", "attributes": [], "artist": "Alice",
            "mbid": ""} in rows, rows
    # `credit_rows_from_tags` states credit ROLES; the release facts are the
    # identity block's (asserted below), not a second set of rows per file.
    assert not [r for r in rows if r["role"] == "label"], rows


def test_album_identity_present_and_filled():
    body = get(album=slash(ALBUM)).json()
    ident = body["identity"]
    assert isinstance(ident, dict), body
    assert IDENTITY_KEYS <= set(ident), sorted(ident)
    assert all(isinstance(v, str) for v in ident.values()), ident
    assert ident["title"] == ident["album"] == "Credits Album", ident
    assert ident["title"] == body["album"], (ident, body)
    assert ident["artist"] == "Credits Artist", ident
    assert ident["album_artist"] == "Credits Artist", ident
    assert ident["catalog_number"] == "CRED-001", ident
    assert ident["label"] == "Credits Label", ident
    assert ident["barcode"] == "0123456789012", ident
    assert ident["date"] == "1994-03-01", ident
    assert ident["original_date"] == "1990-05-02", ident
    assert ident["country"] == "GB", ident
    assert ident["release_type"] == "Album", ident
    assert ident["media"] == "CD", ident
    assert ident["release_mbid"] == REL_MBID == body["release_mbid"], ident
    assert ident["artist_mbid"] == ARTIST_MBID, ident
    assert os.path.normpath(ident["path"]) == ALBUM, ident
    assert ident["track_mbid"] == ident["recording_mbid"] == "", ident
    assert ident["release_group_mbid"] == "", ident


def test_file_rows_carry_the_whole_mb_vocabulary():
    rows = get(path=slash(FR)).json()["rows"]
    want = [
        ("producer", "Alice"), ("composer", "Frank"), ("work", "Credits Song"),
        ("label", "Credits Label"), ("catalog number", "CRED-001"),
        ("barcode", "0123456789012"), ("release type", "Album"),
        ("original date", "1990-05-02"), ("isrc", "USRC17607839"),
        ("musicbrainz recording id", FR_MBID),
        ("musicbrainz release id", REL_MBID),
    ]
    for role, artist in want:
        assert {"role": role, "attributes": [], "artist": artist,
                "mbid": "", "source": "file"} in rows, (role, rows)
    assert {"role": "performer", "attributes": ["double bass"],
            "artist": "Dave", "mbid": "", "source": "file"} in rows, rows


def test_missing_file_is_200_not_500():
    r = get(path=slash(os.path.join(ALBUM, "does not exist.flac")))
    assert r.status_code == 200, (r.status_code, r.text)
    body = r.json()
    assert body["rows"] == [], body
    assert body["source"] == "tags" and body["track_mbid"] == "", body


# --------------------------------------------------------------------------- #
# refusals: the guard is 400, no args is 404
# --------------------------------------------------------------------------- #
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
        ("tidy: unusual role + free-text attribute kept",
         test_tidy_credit_rows_is_non_destructive),
        ("track: reads the tags, no request", test_track_reads_tags_only),
        ("track: identity present + filled",
         test_track_identity_present_and_filled),
        ("tags: fallback, no request", test_tag_fallback_no_request),
        ("tags: identity offline, track", test_identity_offline_for_tag_only_track),
        ("tags: identity offline, album", test_identity_offline_for_tag_only_album),
        ("album: rows from tags, unioned", test_album_rows_from_tags),
        ("album: identity present + filled", test_album_identity_present_and_filled),
        ("file: the whole MB vocabulary", test_file_rows_carry_the_whole_mb_vocabulary),
        ("file: missing file is 200, not 500", test_missing_file_is_200_not_500),
        ("outside music folder -> 400", test_outside_music_folder_is_400),
        ("no path/album -> 404", test_no_path_is_404),
    ]:
        check(label, fn)
except SystemExit:
    raise
if FAILED:
    print(f"credits: {len(FAILED)} check(s) failed: {', '.join(FAILED)}")
    sys.exit(1)
print("credits: all assertions passed")
