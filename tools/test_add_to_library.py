#!/usr/bin/env python3
"""Verify "Add to library": the framework album, its wish, the pending library
row, the cleanup when the import fills it, and the cancel.

No network at all: MusicBrainz (the route's resolvers), the Cover Art Archive
(stubbed at the art cache's own seam) and the import chain's remote steps are
stubbed, and the music folder is a temp directory — the developer's real
library is never opened or written.

Run: python tools/test_add_to_library.py  (exit 0 pass, 1 fail)
"""
import atexit
import hashlib
import inspect
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

REDIRECT = tempfile.mkdtemp(prefix="mlo-add-redirect-")
MF = tempfile.mkdtemp(prefix="mlo-add-test-")
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
for _t in (MF,):
    if REAL:
        assert not _t.replace("\\", "/").lower().startswith(REAL.lower()), \
            f"temp fixture {_t} sits inside the real music folder {REAL}"

from mlo import grader as grader_mod  # noqa: E402
from mlo.config import load_config  # noqa: E402
from server import artcache, imports, pending_albums, wishes  # noqa: E402
from server import integrations as intg_mod  # noqa: E402
from server import library as lib_mod  # noqa: E402
from server import main as mlo_main  # noqa: E402  (organize + the DELETE route)

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
# stubs: the network's three seams (release lookup, cover fetch, chain steps)
# --------------------------------------------------------------------------- #
COVER_PLACEHOLDER = b"\xff\xd8\xff" + b"placeholder-cover" * 8
COVER_IMPORTED = b"\xff\xd8\xff" + b"real-cover-the-import-wrote" * 8

cover_calls = []


def fake_fetch_art(url, **kwargs):
    cover_calls.append(url)
    return COVER_PLACEHOLDER, "image/jpeg", "coverartarchive"


artcache.fetch_art = fake_fetch_art

release = {
    "id": "11111111-1111-1111-1111-111111111111",
    "title": "Test Album",
    "date": "1997-05-06",
    "originaldate": "1997",
    "country": "GB",
    "status": "Official",
    "medium": "CD",
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


def release_variant(n, title):
    """The same test release as a DIFFERENT one: each case below needs an album
    of its own, or the second add correctly reports "already in the library"
    and returns no wish at all."""
    rel = dict(release)
    rel["id"] = str(n) * 8 + "-1111-1111-1111-111111111111"
    rel["release_group_id"] = str(n) * 8 + "-2222-2222-2222-222222222222"
    rel["title"] = title
    # its own artist too: each case must own its folder (the artist folder is
    # pruned when the framework album is cancelled and nothing else is in it)
    rel["artists"] = [{"name": f"Test Artist {n}", "mbid": str(n) * 8 + "-3333-3333-3333-333333333333"}]
    rel["media"] = [dict(t) for t in release["media"]]
    return rel


# The import chain, with every remote step replaced: nothing here may touch the
# network, and `run_cover_step` records what the folder looked like when the
# chain reached its cover step.
CHAIN_CFG = {"music_folder": MF, "import_auto_scripts": False,
             "advisory_auto_fetch": False, "instrumental_auto_fetch": False,
             "metadata_auto_fetch": False, "cover_auto_fetch": False,
             "lyrics_format": "EMBEDDED"}

imports.stamp_rym_links = lambda path, cfg=None: {"note": "stub", "album": "", "artist": ""}
imports.run_metadata_step = lambda path, cfg=None: {"items": []}
cover_step_saw = []


def fake_cover_step(album_dir, cfg=None):
    """What the chain's own cover step would do: it runs only when the album
    has NO cover, and it writes the release's real artwork."""
    cover_step_saw.append(os.path.exists(os.path.join(album_dir, "cover.jpg")))
    with open(os.path.join(album_dir, "cover.jpg"), "wb") as fh:
        fh.write(COVER_IMPORTED)
    return {"source": "stub"}


imports.run_cover_step = fake_cover_step

# The ADD-time pre-fetch (`pending_albums.create` → `imports.prefetch_album`)
# runs the import chain's own steps early — the metadata step and the cover
# search. The artist image / description candidates are stubbed by
# `imports.run_metadata_step` above; the two seams still left are the links
# resolver and the cover finder, and neither may be reached for real here.
intg_mod.rym_links = lambda artist="", album="", cfg=None, mbid=None: {
    "album": "https://rateyourmusic.com/release/album/a/b/",
    "artist": "https://rateyourmusic.com/artist/a", "note": "stub"}
cover_searches = []


def fake_cover_search(artist, album, limit=40, timeout=60.0, sources=None,
                      country=None, cfg=None, release_group_mbid="",
                      release_mbid=""):
    """What the cover finder answers offline: one release-group stand-in, so the
    add-time staging has a real candidate to rank and mark."""
    cover_searches.append({"artist": artist, "album": album,
                           "release_group_mbid": release_group_mbid,
                           "release_mbid": release_mbid})
    return {"provider": "coverartarchive", "sources": [
        {"id": "coverartarchive", "status": "used", "count": 1}],
        "results": [{
            "source": "coverartarchive", "small": None,
            "big": "https://coverartarchive.org/release-group/rg/front",
            "title": "Test Album", "artist": "Test Artist", "tracks": 2,
            "url": "https://coverartarchive.org/release-group/rg",
            "width": 1200, "height": 1200, "format": "jpeg", "bytes": 200000,
            "front": True, "kind": "front", "release_cover": False, "rank": 0}]}


intg_mod.cover_search = fake_cover_search


def add_audio(album_dir):
    """A file the album walk treats as a track (the bytes are never decoded)."""
    path = os.path.join(album_dir, "1-01 One.flac")
    with open(path, "wb") as fh:
        fh.write(b"fLaC" + b"\x00" * 64)
    return path


def find_album(payload, album_path):
    want = os.path.normcase(os.path.normpath(album_path))
    for artist in payload.get("artists", []):
        for alb in artist.get("albums", []):
            if os.path.normcase(os.path.normpath(str(alb.get("path") or ""))) == want:
                return artist, alb
    return None, None


def fresh_library(cfg):
    from server import tagcache
    tagcache.invalidate_all()
    return lib_mod.build_library(cfg)


def finish(album_dir, cfg):
    """Finish one album through the import chain's own entry point.

    `defer_tagging` is the auto-import's staged mode while this ticket was
    written and is being retired by the import-chain owner, so the call is made
    the way the module currently offers it — CHAIN_CFG configures no chain, so
    no chain runs either way.
    """
    return imports.finish_album(album_dir, cfg, **_FINISH_KW)


def read_bytes(path):
    """The file's bytes, or b"" when it is not there."""
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return b""


# The import-chain owner is retiring `defer_tagging`; until then it is the mode
# a staged album is finished in, so it is passed only while it exists.
_FINISH_KW = ({"defer_tagging": True}
              if "defer_tagging" in inspect.signature(imports.finish_album).parameters
              else {})


# --------------------------------------------------------------------------- #
# 1. the framework album + its wish
# --------------------------------------------------------------------------- #
print("\nframework album")
wishes.delete_wish(0)                                    # no-op, schema warm-up
cfg = load_config()
row = pending_albums.create(release, cfg)
folder = row["album_path"].replace("/", os.sep)
ok(row["created"] and os.path.isdir(folder), "folder created", folder)
ok(cover_calls and cover_calls[0].startswith("https://coverartarchive.org/release-group/"),
   "the release-group cover was asked for through the art cache",
   cover_calls[:1])

manifest = pathmod.load_expected_tracks(folder)
eq(manifest["release_id"], release["id"], "manifest records the release id")
eq([(t["disc"], t["position"], t["title"]) for t in manifest["tracks"]],
   [(1, 1, "One"), (1, 2, "Two")], "manifest holds the release's own tracklist")

marker = pathmod.load_pending(folder)
ok(bool(marker and marker.get("pending")), "pending marker written")
eq(marker.get("waiting_for"), "a verified Soulseek download", "marker says what it waits for")
eq(marker.get("release_group_id"), release["release_group_id"], "marker carries the release group")
cover_name = (marker.get("cover") or {}).get("file")
eq(cover_name, "cover.jpg", "the placeholder cover is the album's cover")
with open(os.path.join(folder, cover_name), "rb") as fh:
    eq(hashlib.sha1(fh.read()).hexdigest(), (marker.get("cover") or {}).get("sha1"),
       "the marker records the placeholder's own bytes")

wish = wishes.get_wish(row["wish_id"])
eq(wish["source"], "musicbrainz", "the wish is labelled as a MusicBrainz add")
eq(wish["release_mbid"], release["id"], "the wish is keyed by the release id")
eq(os.path.normcase(wish["album_path"]), os.path.normcase(folder),
   "the wish points at the framework folder")
ok(wish["pending"] is True, "the wish reports itself pending")
eq(wishes.add_wish(release["id"])["id"], wish["id"], "adding again reuses the same wish")

# --------------------------------------------------------------------------- #
# 2. the library lists it — pending, with its track list, no playable track
# --------------------------------------------------------------------------- #
print("\nlibrary payload")
payload = fresh_library(cfg)
artist_row, album_row = find_album(payload, folder)
ok(album_row is not None, "the pending album is in the library payload")
eq(album_row["pending"], True, "listed as pending")
eq(album_row["track_count"], 0, "no track is counted as present")
eq(album_row["tracks"], [], "no track is playable yet")
eq([t["title"] for t in album_row["expected_tracks"]], ["One", "Two"],
   "the track list comes from the release manifest")
ok(all(t["missing"] for t in album_row["expected_tracks"]), "every track is still missing")
eq(album_row["partial"], True, "the album is partial, not complete")
eq(album_row["pass"], False, "a pending album is not a PASS")
eq(album_row["grade_pct"], None, "and it states no grade percentage")
eq(album_row["cover_file"], cover_name, "the placeholder cover is the album's cover")
eq(album_row["meta"]["ALBUM"], "Test Album", "title from the release")
eq(album_row["meta"]["ALBUMARTIST"], "Test Artist", "artist from the release")
eq(album_row["meta"]["DATE"], "1997-05-06", "date from the release")
eq(album_row["issues"], {}, "a placeholder is not reported as a broken album")
eq(artist_row["aggregate"]["track_count"], 0, "the artist rollup counts no phantom track")
eq(artist_row["aggregate"]["album_count"], 1, "the artist rollup does count the pending album")

# the framework folder is NOT an "empty folder" grading failure either
eq(grader_mod._find_empty_folders(MF, {}), [], "the placeholder is not an empty-folder failure")

# nothing pending is ever "already in your library"
ok(release["id"] not in wishes.owned_mbids(cfg), "a pending album is not owned evidence")
ok(release["release_group_id"] not in wishes.owned_mbids(cfg),
   "nor is its release group")

# --------------------------------------------------------------------------- #
# 3. the import that fills it: marker gone, OUR cover gone, the chain's kept
# --------------------------------------------------------------------------- #
print("\nimport fills the framework album")
add_audio(folder)
tagcache_mod = __import__("server.tagcache", fromlist=["tagcache"])
tagcache_mod.invalidate_all()

# The chain's own two calls, first, because they are the ordering guarantee: the
# placeholder MUST go before the cover step (that step counts any cover as the
# album's own) and the marker must NOT go while the chain still owes the album
# work.
ok(pending_albums.drop_placeholder_cover(folder),
   "the chain's pre-cover-step call drops the placeholder")
ok(not os.path.exists(os.path.join(folder, cover_name)), "and the file is gone from disk")
eq(pending_albums.clear_if_filled(folder, CHAIN_CFG, chained=False), False,
   "a configured chain that has NOT run leaves the album pending")
ok(bool(pathmod.load_pending(folder)), "so the library still reports it pending")

out = finish(folder, CHAIN_CFG)
eq(out["errors"], [], "the stubbed import finished without errors")
ok(not os.path.exists(os.path.join(folder, pathmod.PENDING_FILE)),
   "the marker is gone once the import has finished the album")
ok(all(saw is False for saw in cover_step_saw),
   "the chain's cover step never saw a cover (the placeholder went first)",
   cover_step_saw)
cover_bytes = read_bytes(os.path.join(folder, "cover.jpg"))
ok(cover_bytes != COVER_PLACEHOLDER,
   "the placeholder image is not what the album is left with")
ok(not cover_bytes or cover_bytes == COVER_IMPORTED,
   "the album's cover is one the import itself wrote", cover_bytes[:12])

payload = fresh_library(CHAIN_CFG)
_artist_row, album_row = find_album(payload, folder)
eq(album_row["pending"], False, "the album is a normal album again")
eq(album_row["track_count"], 1, "its track is counted")
eq(wishes.get_wish(wish["id"])["pending"], False, "and the wish reports itself no longer pending")
# ...and adding that same release again creates nothing at all
again = pending_albums.create(release, cfg)
ok(again["existing"] and not again["created"], "an album already in the library is not re-created")

# --------------------------------------------------------------------------- #
# 4. a cover the import (or the user) wrote is never deleted
# --------------------------------------------------------------------------- #
print("\na foreign cover survives the cleanup")
row2 = pending_albums.create(release_variant(9, "Second Add"), cfg)
folder2 = row2["album_path"].replace("/", os.sep)
FOREIGN = b"\x89PNG\r\n\x1a\n" + b"the-import-wrote-this" * 4
with open(os.path.join(folder2, "cover.jpg"), "wb") as fh:
    fh.write(FOREIGN)
add_audio(folder2)
out2 = finish(folder2, CHAIN_CFG)
ok(not os.path.exists(os.path.join(folder2, pathmod.PENDING_FILE)),
   "the pending marker still goes")
# The invariant: our placeholder is never what remains, and a cover someone else
# wrote is never the file the cleanup deleted. Whether the chain's own cover
# step then rewrites it depends on the import chain's configuration, so both
# outcomes are legitimate here.
cover2 = read_bytes(os.path.join(folder2, "cover.jpg"))
ok(cover2 in (FOREIGN, COVER_IMPORTED),
   "a cover replaced before the cleanup is never deleted", cover2[:12])
ok(cover2 != COVER_PLACEHOLDER,
   "and the placeholder image is not what is left behind")
ok(out2["errors"] == [], "the second import finished without errors")

# --------------------------------------------------------------------------- #
# 5. cancelling takes the folder with it
# --------------------------------------------------------------------------- #
print("\ncancel")
row3 = pending_albums.create(release_variant(8, "Third Add"), cfg)
folder3 = row3["album_path"].replace("/", os.sep)
artist_dir = os.path.dirname(folder3)
ok(os.path.isdir(folder3), "the third framework folder exists")
res = mlo_main.wishes_delete(row3["wish_id"])
ok(res.get("ok") is True, "the wish was deleted")
ok(not os.path.isdir(folder3), "the framework folder went with it")
ok(not os.path.isdir(artist_dir), "the emptied artist folder was pruned")
ok(wishes.get_wish(row3["wish_id"]) is None, "the wish row is gone")

# there is only ONE artist now (the two filled albums), and no pending row left
payload = fresh_library(CHAIN_CFG)
pending_left = [a for ar in payload["artists"] for a in ar["albums"] if a.get("pending")]
eq(pending_left, [], "no pending album is left behind")

# --------------------------------------------------------------------------- #
# 6. the import that lands on a DIFFERENT name still fills the framework folder
# --------------------------------------------------------------------------- #
print("\norganize adopts the framework album")
import struct  # noqa: E402  (a real WAV the organizer can tag and read)


def write_tagged_wav(path, tags):
    """A real WAV carrying ID3 tags: organize evaluates the naming script from
    the audio's own tags, so adoption cannot be tested with a fake file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    frames = b"\x00\x00" * 800
    with open(path, "wb") as fh:
        fh.write(b"RIFF" + struct.pack("<I", 36 + len(frames)) + b"WAVEfmt "
                 + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 16000, 2, 16)
                 + b"data" + struct.pack("<I", len(frames)) + frames)
    from mlo.audio import AudioFile
    af = AudioFile(path)
    for key, value in tags.items():
        af.set_tag(key, value)
    return path


adopt = release_variant(6, "Adopt Add")
row6 = pending_albums.create(adopt, cfg)
folder6 = row6["album_path"].replace("/", os.sep)
ok(os.path.isdir(folder6), "the framework album exists before the download")

staging = os.path.join(MF, ".mlo", "downloads", "a-peer",
                       "Test Artist 6 - Adopt Add")
write_tagged_wav(os.path.join(staging, "1-01 One.wav"), {
    "ALBUM": adopt["title"],
    "ALBUMARTIST": "Test Artist 6",
    "ARTIST": "Test Artist 6",
    "MUSICBRAINZ_ALBUMARTISTID": adopt["artists"][0]["mbid"],
    "MUSICBRAINZ_ARTISTID": adopt["artists"][0]["mbid"],
    "MUSICBRAINZ_ALBUMID": adopt["id"],
    "MUSICBRAINZ_RELEASEGROUPID": adopt["release_group_id"],
    "RELEASETYPE": "Album",
    "DATE": adopt["date"],
    "ORIGINALDATE": adopt["originaldate"],
    # Deliberately a DIFFERENT medium from MusicBrainz's own "CD": the naming
    # script then names another folder than the framework album's, which is
    # exactly the case adoption exists for.
    "MEDIA": "Vinyl",
    "RELEASECOUNTRY": adopt["country"],
    "CATALOGNUMBER": adopt["catalog_number"],
    "LABEL": adopt["label"],
    "DISCNUMBER": "1",
    "TRACKNUMBER": "1",
    "TITLE": "One",
})
org = mlo_main.organize(mlo_main.OrganizeRequest(paths=[staging], dry_run=False))
res_row = (org.get("results") or [{}])[0]
landed = os.path.normcase(os.path.normpath(str(res_row.get("album_root") or "")))
eq(landed, os.path.normcase(os.path.normpath(folder6)),
   "the organizer landed the album IN the framework folder")
ok(any(f.lower().endswith(".wav") for f in os.listdir(folder6)),
   "the downloaded track is in the framework folder now",
   os.listdir(folder6))
ok(not any(f.lower().endswith(".wav") for f in os.listdir(staging))
   if os.path.isdir(staging) else True,
   "and left the staging folder")
ok(bool(pathmod.load_pending(folder6)),
   "the marker is still there until the chain finishes the album")
eq(res_row.get("errors") or [], [], "the organizer reported no error")

# --------------------------------------------------------------------------- #
# 7. the HTTP route, end to end, with MusicBrainz stubbed
# --------------------------------------------------------------------------- #
print("\nPOST /api/library/add")
try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except Exception as e:                                            # pragma: no cover
    print(f"  SKIP  TestClient unavailable: {e}")
else:
    from server import api_add, integrations as intg, wishes_worker

    # Its own release: the albums above are in the library by now, and the
    # route is supposed to CREATE a framework album, not report one it found.
    route_release = release_variant(7, "Route Add")
    intg.auto_import_targets = lambda mbid, kind=None, mode="best", types=None, limit=None: (
        [{"mbid": route_release["id"], "title": route_release["title"]}], [])
    intg.resolve_release = lambda mbid: (route_release, route_release["id"])
    triggered = []
    wishes_worker.trigger = lambda wid=None: triggered.append(wid) or {"ok": True}

    app = FastAPI()
    app.include_router(api_add.router)
    client = TestClient(app)

    r = client.post("/api/library/add", json={
        "mbid": route_release["release_group_id"], "kind": "release_group",
        "mode": "best"})
    eq(r.status_code, 200, "the route answers 200")
    body = r.json()
    albums = body.get("albums") or []
    eq(len(albums), 1, "one album was added")
    route_folder = str(albums[0]["album_path"]).replace("/", os.sep)
    ok(albums[0]["created"] and os.path.isdir(route_folder),
       "the route created the framework folder on disk", route_folder)
    ok(bool(pathmod.load_pending(route_folder)), "and marked it pending")
    # The add STARTS the search it just recorded, on the worker's OWN pass (no
    # wish id: the pass reads the store and searches what is due). Waiting for
    # the loop's next tick was up to two minutes of nothing happening, with the
    # album sitting in the library saying it was waiting for a download.
    eq(triggered, [None], "a plain add starts the search for what it recorded")
    eq(body.get("note"), "Soulseek is searching for them now.",
       "and the reply says the search started")
    eq(wishes.get_wish(albums[0]["wish_id"])["source"], "musicbrainz",
       "the wish is on the existing queue, labelled MusicBrainz")

    # The SAME release added again is the same album and the same wish: one
    # folder on disk and one row in the store, so there is one thing to search.
    again = client.post("/api/library/add", json={
        "mbid": route_release["release_group_id"], "kind": "release_group",
        "mode": "best"})
    again_albums = again.json().get("albums") or []
    eq([a["wish_id"] for a in again_albums], [albums[0]["wish_id"]],
       "adding the same release again reuses its one wish")
    eq(len([w for w in wishes.list_wishes()
            if w["release_mbid"] == route_release["id"]]), 1,
       "and the store still holds one row for it")
    triggered.clear()

    # The same pressing under the OTHER id: a wish saved from an album link
    # carries the release GROUP id (that is what the link holds), while an add
    # resolves the edition and would key its own row by the RELEASE id. Two
    # rows for one pressing are two jobs, both downloading the same album.
    cross = release_variant(0, "Cross Id Add")
    group_wish = wishes.add_wish(cross["release_group_id"], title="Cross Id Add",
                                 artist="Test Artist 0", source="soulseek")
    intg.auto_import_targets = lambda mbid, kind=None, mode="best", types=None, limit=None: (
        [{"mbid": cross["id"], "title": cross["title"]}], [])
    intg.resolve_release = lambda mbid: (cross, cross["id"])
    cross_add = client.post("/api/library/add",
                            json={"mbid": cross["id"], "kind": "release"})
    eq([a["wish_id"] for a in cross_add.json().get("albums") or []], [group_wish["id"]],
       "an add keyed by the release reuses the wish saved by its release group")
    eq([w["id"] for w in wishes.list_wishes()
        if cross["id"] in (w["release_mbid"], (w.get("release") or {}).get("id"))
        or w["release_mbid"] == cross["release_group_id"]], [group_wish["id"]],
       "and the store still holds ONE row for that pressing")
    triggered.clear()
    intg.auto_import_targets = lambda mbid, kind=None, mode="best", types=None, limit=None: (
        [{"mbid": route_release["id"], "title": route_release["title"]}], [])
    intg.resolve_release = lambda mbid: (route_release, route_release["id"])

    down_release = release_variant(2, "Download Add")
    intg.auto_import_targets = lambda mbid, kind=None, mode="best", types=None, limit=None: (
        [{"mbid": down_release["id"], "title": down_release["title"]}], [])
    intg.resolve_release = lambda mbid: (down_release, down_release["id"])
    down = client.post("/api/library/add", json={
        "mbid": down_release["release_group_id"], "kind": "release_group",
        "mode": "best", "download": True})
    eq(down.status_code, 200, "the download ask answers 200")
    down_albums = down.json().get("albums") or []
    eq(len(down_albums), 1, "the download ask added its own album")
    eq(triggered, [None], "download=True starts it the same way (one start, one queue)")
    eq(down.json().get("note"), "Soulseek is searching for them now.",
       "and the reply says the search started")
    triggered.clear()
    intg.auto_import_targets = lambda mbid, kind=None, mode="best", types=None, limit=None: (
        [{"mbid": route_release["id"], "title": route_release["title"]}], [])
    intg.resolve_release = lambda mbid: (route_release, route_release["id"])

    bad = client.post("/api/library/add", json={"mbid": "", "kind": "release"})
    eq(bad.status_code, 400, "a blank id is refused with 400")

    # a recording + the release it belongs to (the release page's track rows)
    fifth = release_variant(5, "Recording Add")
    intg.resolve_release = lambda mbid: (fifth, fifth["id"])
    rec = client.post("/api/library/add", json={
        "mbid": "99999999-9999-9999-9999-999999999999", "kind": "recording",
        "release_mbid": fifth["id"]})
    eq(rec.status_code, 200, "a recording resolves to its release")
    rec_albums = rec.json().get("albums") or []
    eq(len(rec_albums), 1, "one album for the recording's release")
    ok(os.path.isdir(str(rec_albums[0]["album_path"]).replace("/", os.sep)),
       "and it created the framework folder")
    ok(not (rec.json().get("errors") or []), "no error row for the recording add")

    # A recording whose release the library ALREADY holds adds nothing: the
    # pipeline refuses to download an album it has, so the framework album this
    # would create is one nothing can ever fill.
    owned = release_variant(6, "Adopt Add")      # in the library, MBIDs stamped
    intg.resolve_release = lambda mbid: (owned, owned["id"])
    owned_rec = client.post("/api/library/add", json={
        "mbid": "99999999-9999-9999-9999-999999999997", "kind": "recording",
        "release_mbid": owned["id"]})
    eq(owned_rec.status_code, 200, "a recording on an owned release answers 200")
    eq(owned_rec.json().get("albums"), [], "and adds nothing")
    eq([s.get("reason") for s in owned_rec.json().get("skipped") or []],
       ["already in the library"], "saying the library already holds it")

    # an edition picked inside a release GROUP is the one that is added
    seventh = release_variant(4, "Chosen Edition")
    intg.resolve_release = lambda mbid: (seventh, seventh["id"])
    policy_calls = []
    real_targets = intg.auto_import_targets
    intg.auto_import_targets = lambda mbid, kind=None, mode="best", limit=None: (
        policy_calls.append(mbid) or real_targets(mbid, kind, mode, limit=limit))
    pick = client.post("/api/library/add", json={
        "mbid": seventh["release_group_id"], "kind": "release_group",
        "mode": "best", "release_mbid": seventh["id"]})
    eq(pick.status_code, 200, "the picked edition answers 200")
    pick_albums = pick.json().get("albums") or []
    eq([a["release_id"] for a in pick_albums], [seventh["id"]],
       "the picked edition is what was added, not the policy's")
    eq(policy_calls, [], "the policy was not consulted for a picked edition")

    # a pick from ANOTHER group is refused with a reason, never added
    other = release_variant(3, "Other Group Edition")
    intg.resolve_release = lambda mbid: (other, other["id"])
    stale = client.post("/api/library/add", json={
        "mbid": seventh["release_group_id"], "kind": "release_group",
        "release_mbid": other["id"]})
    eq(stale.status_code, 200, "a mismatched pick answers 200")
    eq(stale.json().get("albums"), [], "and adds nothing")
    eq([s.get("reason") for s in stale.json().get("skipped") or []],
       ["that release is not part of this release group"],
       "with the reason stated")
    intg.auto_import_targets = real_targets

    cancelled = client.post("/api/library/add/cancel",
                            json={"album_path": albums[0]["album_path"]})
    eq(cancelled.status_code, 200, "the cancel route answers 200")
    ok(not os.path.isdir(route_folder), "the route's cancel removed the folder")
    ok(wishes.get_wish(albums[0]["wish_id"]) is None, "and deleted the wish")

# --------------------------------------------------------------------------- #
# 8. the album's PAGE: a framework album answers with a payload, not "not found"
# --------------------------------------------------------------------------- #
print("\nthe pending album's page")
pending_album = another = pending_albums.create(release_variant(2, "Page Add"), cfg)
page_folder = another["album_path"].replace("/", os.sep)
payload_row = lib_mod.build_album(page_folder, cfg)
ok(payload_row is not None, "build_album answers for a folder with no audio")
eq(payload_row["pending"], True, "and says the album is pending")
eq(payload_row["tracks"], [], "with no playable track")
eq(payload_row["track_count"], 0, "and no phantom track counted")
eq(payload_row["grade_pct"], None, "nothing was graded, so no grade is claimed")
eq(payload_row["issues"], {}, "and the empty folder is not reported as a broken album")
eq([t["title"] for t in payload_row["expected_tracks"]], ["One", "Two"],
   "the page carries the release's own tracklist")
ok(all(t["missing"] for t in payload_row["expected_tracks"]), "every track still missing")
ok(bool(payload_row["cover_file"]), "the placeholder cover is the album's cover")
eq(payload_row["path"], page_folder.replace(os.sep, "/"), "path is the folder the page asked for")
# the wish filling it: what the page says instead of an empty tracklist
wish_state = payload_row.get("wish") or {}
eq(wish_state.get("id"), another["wish_id"], "the page carries the wish that fills it")
ok(wish_state.get("status") in ("wanted", "searching"), wish_state)
eq(wish_state.get("attempts"), 0, "with the attempts the queue has spent")
ok("due_at" in wish_state and "reason" in wish_state,
   "and when it is next searched, plus the queue's own reason", wish_state)
# the content the ADD pre-fetched, recorded on the marker
prefetched = payload_row.get("prefetched") or {}
ok("cover_candidates" in prefetched and "links" in prefetched,
   "the marker records what the add pre-fetched", prefetched)
eq(cover_searches[-1]["release_mbid"], release_variant(2, "x")["id"],
   "the pre-fetch asked the cover finder for THIS release's own cover")
# ...and the page's route takes the pending branch rather than 404ing: the
# not-found empty state is for a folder that is not there, never for one the
# app itself created.
served = mlo_main.get_album(path=page_folder)
eq(served["pending"], True, "GET /api/album answers for the framework album")
ok((served.get("wish") or {}).get("id") == another["wish_id"],
   "and carries its wish, so the page can say what is happening to it")
try:
    mlo_main.get_album(path=os.path.join(MF, "no-such-album"))
    ok(False, "an unknown folder still 404s")
except Exception as e:
    ok("404" in str(e), "an unknown folder still 404s", e)

print()
if FAILED:
    print(f"FAILED ({len(FAILED)}): " + ", ".join(FAILED))
    sys.exit(1)
print("All Add-to-library checks passed.")
sys.exit(0)
