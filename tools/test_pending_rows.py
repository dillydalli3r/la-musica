#!/usr/bin/env python3
"""The pending marker, through every album-shaped payload the app draws from.

A framework album (`server.pending_albums`: the folder "Add to library" creates
before its audio exists) has to be VISIBLE and VISIBLY unfinished everywhere an
album is listed — the library tree, the artist page, Home, a query's album rows
— and it has to stop saying so the moment the import fills the folder. This
pins the payloads, because that is what every surface draws from:

  * the library row carries `pending` + WHY it waits;
  * the artist page lists it at all (its own walk looks for AUDIO, so a
    framework folder is invisible there unless the payload adds it);
  * a query's album rows carry the marker, and `library.pending` is a field a
    user can filter on;
  * a COMPLETE album carries no marker at all — not false-but-present on the
    wrong row, absent;
  * nothing is playable while pending (no tracks, track_count 0), and the
    complete album's tracks are still there afterwards;
  * the clear path (`pending_albums.clear_if_filled`, what the import runs)
    ends it: the row renders as a normal album again.

The naming preview (`pending_albums.release_tags`) is checked the same way: its
RELEASECOUNTRY carries every country the release states, ";"-joined with
MusicBrainz's own first event first, so the folder the preview names is the
folder the stamped tags name.

No network and no real library: the music folder is a temp directory, the Cover
Art Archive is stubbed at the art cache's own seam, and the release comes from
the same fixture `tools/test_add_to_library.py` uses.

Run: python tools/test_pending_rows.py  (exit 0 pass, 1 fail)
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
# hermeticity: the app's paths resolve through the music folder the moment they
# are first touched, so the scope is redirected BEFORE server.main is imported.
# --------------------------------------------------------------------------- #
REAL_MUSIC_FOLDER = ""
try:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        REAL_MUSIC_FOLDER = str((json.load(f) or {}).get("music_folder") or "")
except Exception:
    pass

REDIRECT = tempfile.mkdtemp(prefix="mlo-pending-redirect-")
MF = tempfile.mkdtemp(prefix="mlo-pending-test-")
os.environ["MLO_MUSIC_FOLDER"] = MF

import mlo.config as cfgmod  # noqa: E402
import mlo.paths as pathmod  # noqa: E402

_STUB = os.path.join(REDIRECT, "config.json")
with open(_STUB, "w", encoding="utf-8") as f:
    json.dump({"music_folder": MF}, f)

for _mod in (cfgmod, pathmod):
    _mod.CONFIG_FILE = _STUB
    if getattr(_mod, "LEGACY_DATA_DIR", None) is not None:
        _mod.LEGACY_DATA_DIR = os.path.join(REDIRECT, "legacy")

atexit.register(lambda: [shutil.rmtree(d, ignore_errors=True)
                         for d in (REDIRECT, MF)])
atexit.register(lambda: os.environ.pop("MLO_MUSIC_FOLDER", None))

REAL = REAL_MUSIC_FOLDER.replace("\\", "/").rstrip("/")
if REAL:
    assert not MF.replace("\\", "/").lower().startswith(REAL.lower()), \
        f"temp fixture {MF} sits inside the real music folder {REAL}"

from fastapi import FastAPI                                    # noqa: E402
from fastapi.testclient import TestClient                      # noqa: E402

from mlo import naming                                         # noqa: E402
from mlo.config import load_config                             # noqa: E402
from server import api_query as aq                             # noqa: E402
from server import artcache, pending_albums, tagcache           # noqa: E402
from mlo import paths as mlo_paths                            # noqa: E402
from server import library as lib_mod                          # noqa: E402
from server import main as mlo_main                            # noqa: E402

FAILED = []


def ok(cond, label, extra=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{f' — {extra}' if extra else ''}")
        FAILED.append(label)


def eq(got, want, label):
    ok(got == want, label, f"got {got!r}, want {want!r}")


# --------------------------------------------------------------------------- #
# stubs: the three network seams the add + pre-fetch touch
# --------------------------------------------------------------------------- #
COVER_PLACEHOLDER = b"\xff\xd8\xff" + b"placeholder-cover" * 8


def fake_fetch_art(url, **kwargs):
    return COVER_PLACEHOLDER, "image/jpeg", "coverartarchive"


artcache.fetch_art = fake_fetch_art

RELEASE = {
    "id": "11111111-1111-1111-1111-111111111111",
    "title": "Pending Album",
    "date": "1997-05-06",
    "originaldate": "1997",
    "country": "GB",
    "status": "Official",
    "label": "Test Label",
    "catalog_number": "CAT-1",
    "release_group_id": "22222222-2222-2222-2222-222222222222",
    "release_type": "album",
    "primary_type": "Album",
    "secondary_types": [],
    "artists": [{"name": "Test Artist", "mbid": "33333333-3333-3333-3333-333333333333"}],
    "medium_count": 1,
    "media": [
        {"disc": 1, "position": 1, "title": "One",
         "recording_mbid": "44444444-4444-4444-4444-444444444444",
         "artist_credit": "Test Artist"},
        {"disc": 1, "position": 2, "title": "Two",
         "recording_mbid": "55555555-5555-5555-5555-555555555555",
         "artist_credit": "Test Artist"},
    ],
}

CFG = {"music_folder": MF, "import_auto_scripts": False,
       "advisory_auto_fetch": False, "instrumental_auto_fetch": False,
       "metadata_auto_fetch": False, "cover_auto_fetch": False,
       "lyrics_format": "EMBEDDED"}


def write_audio(folder, name="1-01 One.flac"):
    """A file the album walk treats as a track (the bytes are never decoded)."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "wb") as fh:
        fh.write(b"fLaC" + b"\x00" * 64)
    return path


def norm(p):
    return os.path.normcase(os.path.normpath(str(p or "")))


def library_rows(cfg):
    """The library payload's album rows, flat, keyed by normalized path."""
    tagcache.invalidate_all()
    payload = lib_mod.build_library(cfg)
    out = {}
    for artist in payload.get("artists", []):
        for alb in artist.get("albums", []):
            out[norm(alb.get("path"))] = alb
    return payload, out


def find(rows, path):
    for row in rows:
        if norm(row.get("path")) == norm(path):
            return row
    return None


# The query goes through the real route (server.api_query), over the same
# library payload — `_config` is the only thing pinned, so the developer's own
# config.json is never read.
app = FastAPI()
app.include_router(aq.router)
client = TestClient(app)
aq._config = lambda: CFG


def album_query(conditions=None):
    body = {"target": "albums", "conditions": conditions or [],
            "limit": 100}
    res = client.post("/api/library/query", json=body)
    if res.status_code != 200:
        raise AssertionError(f"query failed: {res.status_code} {res.text[:200]}")
    return res.json()["items"]


# --------------------------------------------------------------------------- #
# 1. a framework album: the folder exists, the audio does not
# --------------------------------------------------------------------------- #
print("\nthe framework album")
created = pending_albums.create(RELEASE, CFG, prefetch=False)
folder = created["album_path"].replace("/", os.sep)
ok(created["created"] and os.path.isdir(folder), "framework folder created", folder)
reason = (pathmod.load_pending(folder) or {}).get("waiting_for")
eq(reason, "the audio for this release", "the marker says what it waits for")

lib_payload, rows = library_rows(CFG)
row = rows.get(norm(folder))
ok(row is not None, "the library lists it at all")
if row:
    eq(row.get("pending"), True, "library row: pending")
    eq(row.get("pending_reason"), "the audio for this release",
       "library row: why it is pending")
    eq(row.get("track_count"), 0, "library row: 0 tracks — nothing to play")
    eq(row.get("tracks") or [], [], "library row: no playable track")

# The artist page: its own walk looks for AUDIO, so this is the one surface the
# framework folder would silently vanish from.
artist_dir = os.path.dirname(folder)
artist_payload = mlo_main.get_artist(path=artist_dir.replace("\\", "/"))
artist_albums = artist_payload.get("albums") or []
entry = find(artist_albums, folder)
ok(entry is not None, "the artist page lists it", [a.get("path") for a in artist_albums])
if entry:
    eq(entry.get("pending"), True, "artist page: pending")
    eq(entry.get("pending_reason"), "the audio for this release",
       "artist page: why it is pending")

# A query's album rows come from the same tree — and the field is filterable.
query_rows = album_query()
query_row = find(query_rows, folder)
ok(query_row is not None, "the query's album rows list it")
if query_row:
    eq(query_row.get("pending"), True, "query row: pending")
    eq(query_row.get("pending_reason"), "the audio for this release",
       "query row: why it is pending")
filtered = album_query([{"field": "library.pending", "op": "eq", "value": True}])
eq([norm(r.get("path")) for r in filtered], [norm(folder)],
   "library.pending is a field a query can filter on")

# --------------------------------------------------------------------------- #
# 2. a complete album: no marker, on any surface
# --------------------------------------------------------------------------- #
print("\nthe complete album")
done_dir = os.path.join(artist_dir, "Pending Album (done)")
write_audio(done_dir)
lib_payload, rows = library_rows(CFG)
done_row = rows.get(norm(done_dir)) or {}
ok(done_row and not done_row.get("pending"), "library row: no marker")
ok("pending_reason" not in done_row and "wish" not in done_row,
   "library row: the marker's keys are absent, not false", sorted(done_row))
ok((done_row.get("track_count") or 0) > 0, "library row: its tracks are there — it plays")
done_query = album_query([{"field": "library.pending", "op": "eq", "value": True}])
ok(all(norm(r.get("path")) != norm(done_dir) for r in done_query),
   "query: library.pending does not select it")
done_qrow = find(album_query(), done_dir) or {}
ok(not done_qrow.get("pending"), "query row: no marker")

# --------------------------------------------------------------------------- #
# 3. the import fills it: the marker goes, the row renders normally
# --------------------------------------------------------------------------- #
print("\nwhen the audio lands")
write_audio(folder)                       # what the download/import brings
ok(pending_albums.clear_if_filled(folder, CFG),
   "clear_if_filled ends the framework state (the import's own call)")
lib_payload, rows = library_rows(CFG)
after = rows.get(norm(folder)) or {}
ok(after, "the album is still in the library")
ok(not after.get("pending"), "library row: the marker is gone")
ok("pending_reason" not in after and after.get("wish") is None,
   "library row: nothing about the search is left", sorted(after))
ok((after.get("track_count") or 0) > 0, "library row: now it has audio to play")
after_q = find(album_query(), folder) or {}
ok(not after_q.get("pending"), "query row: the marker is gone")

# --------------------------------------------------------------------------- #
# 4. the naming preview's RELEASECOUNTRY: every country, first event first
# --------------------------------------------------------------------------- #
print("\nthe naming preview's country")
tags = pending_albums.release_tags(RELEASE)
eq(tags.get("RELEASECOUNTRY"), "GB",
   "a release with one country previews that code")
# The same release out in three countries: the preview carries all of them,
# ";"-joined exactly as the tags will be stamped (the import's own identity
# stamper writes the same value), so the folder previewed here is the folder
# the tags name.
MULTI = dict(RELEASE, countries=[{"code": "GB", "date": "1997-05-06"},
                                 {"code": "US", "date": "1997-05-20"},
                                 {"code": "XE", "date": "1997-06-01"}])
multi_tags = pending_albums.release_tags(MULTI)
eq(multi_tags.get("RELEASECOUNTRY"), "GB; US; XE",
   "a multi-country release previews every code, '; '-joined")
ok(multi_tags["RELEASECOUNTRY"].split("; ")[0] == RELEASE["country"],
   "…with MusicBrainz's own first event first — what the naming script reads "
   "(mlo.naming._first_multi, spec R33)")
eq(naming.track_variables(multi_tags).get("releasecountry"), "GB",
   "the naming variable is that first code, not the list")
eq(pending_albums.folder_for_release(MULTI, CFG),
   pending_albums.folder_for_release(RELEASE, CFG),
   "…so the folder preview does not move when a release gains countries")

# --------------------------------------------------------------------------- #
# 5. CANCELLING a framework album: the placeholder goes, its audio never does
# --------------------------------------------------------------------------- #
print("\na cancelled framework album")
ended = pending_albums.create(dict(RELEASE, id="99999999-1111-1111-1111-111111111111",
                                   release_group_id="99999999-2222-2222-2222-222222222222",
                                   title="Ended Album"), CFG, prefetch=False)
ended_folder = ended["album_path"].replace("/", os.sep)
ok(os.path.isdir(ended_folder) and bool(pathmod.load_pending(ended_folder)),
   "the framework album and its marker are on disk")
# What cancelling runs (server.api_add's own cancel route, and the sweep):
# `remove_folder` takes the folder the ADD created back.
ok(pending_albums.remove_folder(ended_folder),
   "cancelling removes the folder the add created")
ok(not os.path.isdir(ended_folder), "the folder is gone")
ok(pathmod.load_pending(ended_folder) is None, "and its marker went with it")

# A folder that HOLDS audio is a real album by then (a download landed in it):
# cancelling may never delete it.
kept = pending_albums.create(dict(RELEASE, id="99999999-3333-3333-3333-333333333333",
                                  release_group_id="99999999-4444-4444-4444-444444444444",
                                  title="Landed Album"), CFG, prefetch=False)
kept_folder = kept["album_path"].replace("/", os.sep)
write_audio(kept_folder)
ok(not pending_albums.remove_folder(kept_folder),
   "a folder holding audio is never removed by cancelling")
ok(os.path.isfile(os.path.join(kept_folder, "1-01 One.flac")),
   "and its audio is still there")

print()
if FAILED:
    print(f"FAILED — {len(FAILED)} check(s):")
    for name in FAILED:
        print(f"  - {name}")
    sys.exit(1)
print("PASS — the pending marker rides on every album-shaped payload, and clears")
