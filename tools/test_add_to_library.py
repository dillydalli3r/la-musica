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
import threading
import time as real_time

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
# THE PRESSING, while the audio is still on its way (the owner's ask: an album
# being imported must not read as a blank cell). The identity the ADD resolved
# is on the wish, so the tile's medium/country/catalogue readout is filled from
# what the framework album already stores — and these are the same values the
# import then writes into the files (`server.imports._stamp_release_identity`),
# so the tile does not change its mind when the download lands.
eq(album_row["meta"]["MEDIA"], "CD", "the medium the release is pressed on")
eq(album_row["media"], "CD", "and the row's own medium field carries it")
eq(album_row["meta"]["RELEASECOUNTRY"], "GB", "the country the release came out in")
eq(album_row["meta"]["CATALOGNUMBER"], "CAT-1", "the release's catalogue number")
eq(album_row["meta"]["LABEL"], "Test Label", "and the label that put it out")
eq(album_row["meta"]["RELEASESTATUS"], "Official", "with its MusicBrainz status")
# the facts the RELEASE does not state stay empty rather than being guessed
eq(album_row["meta"]["ITUNESADVISORY"], None, "nothing invents a rating")
ok((wish.get("release") or {}).get("media") == ["CD"],
   "the wish carries the identity block the row read", wish.get("release"))
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
# placeholder cover must NOT go before the cover step (that step treats our
# placeholder as no cover of the album's own and fetches over it — deleting it
# first is how an album whose every candidate the floor refuses ended up with no
# cover at all), and the marker must NOT go while the chain still owes the album
# work.
ok(not pending_albums.drop_placeholder_cover(folder),
   "the placeholder cover is NOT dropped on its own — it is the folder's last cover")
ok(os.path.exists(os.path.join(folder, cover_name)),
   "so the album still HAS a cover at that point", cover_name)
eq(pending_albums.clear_if_filled(folder, CHAIN_CFG, chained=False), False,
   "a configured chain that has NOT run leaves the album pending")
ok(bool(pathmod.load_pending(folder)), "so the library still reports it pending")

out = finish(folder, CHAIN_CFG)
eq(out["errors"], [], "the stubbed import finished without errors")
ok(not os.path.exists(os.path.join(folder, pathmod.PENDING_FILE)),
   "the marker is gone once the import has finished the album")
ok(all(saw is True for saw in cover_step_saw),
   "the chain reached its cover step with the placeholder still in the folder",
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
# 6. the import that lands on a DIFFERENT name fills the framework folder AND
#    the folder takes the name the naming script gives the TAGS
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
# The name those TAGS give the album — the add named its folder from the
# MusicBrainz payload, whose medium is "CD" while the download is a Vinyl rip,
# so the two disagree by construction. The tags win: a folder named from the
# payload is a folder NO tag can produce, and grading checks every file against
# the script's own answer (issue #48's "PATH: expected '<…>'").
expected = os.path.normcase(os.path.normpath(
    pending_albums.folder_for_release(dict(adopt, medium="Vinyl"), cfg) or ""))
eq(landed, expected, "the album landed in the folder the naming script names")
ok(not os.path.isdir(folder6) or
   os.path.normcase(os.path.normpath(folder6)) == landed,
   "so the name the ADD gave the folder is gone", folder6)
ok(any(f.lower().endswith(".wav") for f in os.listdir(landed)),
   "the downloaded track is in that folder now", os.listdir(landed))
ok(not any(f.lower().endswith(".wav") for f in os.listdir(staging))
   if os.path.isdir(staging) else True,
   "and left the staging folder")
ok(bool(pathmod.load_pending(landed)),
   "the marker travelled with the folder: still the framework album, now at "
   "the name the tags give it")
ok(not pathmod.load_pending(folder6),
   "and nothing is left pending under the add-time name")
eq(os.path.normcase(str((wishes.get_wish(row6["wish_id"]) or {}).get("album_path")
                        or "")),
   landed, "the wish that created it follows the folder")
eq(res_row.get("errors") or [], [], "the organizer reported no error")

# ...and when the import's own tags name a folder that is NOT even beside the
# framework album (a different artist credit, a naming script edited in
# between), the placeholder is STILL the destination: the release's own wish
# knows where its album is, so `adopt_root` asks it. Otherwise the album lands
# beside a placeholder that then stands empty in the library for ever — one
# release, two albums on the shelf.
far = release_variant(9, "Far Adopt")
row9 = pending_albums.create(far, cfg)
folder9 = row9["album_path"].replace("/", os.sep)
ok(os.path.isdir(folder9), "the far-away case has its framework album")
far_staging = os.path.join(MF, ".mlo", "downloads", "a-peer",
                           "Other Artist Nine - Far Adopt")
write_tagged_wav(os.path.join(far_staging, "1-01 One.wav"), {
    "ALBUM": far["title"],
    # A DIFFERENT artist name: organize names another artist folder entirely,
    # so the placeholder is not in the folder it is about to write into.
    "ALBUMARTIST": "Other Artist Nine",
    "ARTIST": "Other Artist Nine",
    "MUSICBRAINZ_ALBUMARTISTID": far["artists"][0]["mbid"],
    "MUSICBRAINZ_ARTISTID": far["artists"][0]["mbid"],
    "MUSICBRAINZ_ALBUMID": far["id"],
    "MUSICBRAINZ_RELEASEGROUPID": far["release_group_id"],
    "RELEASETYPE": "Album",
    "DATE": far["date"],
    "ORIGINALDATE": far["originaldate"],
    "MEDIA": "CD",
    "RELEASECOUNTRY": far["country"],
    "CATALOGNUMBER": far["catalog_number"],
    "LABEL": far["label"],
    "DISCNUMBER": "1",
    "TRACKNUMBER": "1",
    "TITLE": "One",
})
org2 = mlo_main.organize(mlo_main.OrganizeRequest(paths=[far_staging], dry_run=False))
res2 = (org2.get("results") or [{}])[0]
landed2 = os.path.normcase(os.path.normpath(str(res2.get("album_root") or "")))
# …and it lands there under the name the SCRIPT gives those tags: the tags
# credit "Other Artist Nine", so that is the artist folder it belongs in — the
# placeholder's own (payload-named) folder is renamed onto it, marker and all.
far_tags = dict(far, medium="CD",
                artists=[{"name": "Other Artist Nine",
                          "mbid": far["artists"][0]["mbid"]}])
expected2 = os.path.normcase(os.path.normpath(
    pending_albums.folder_for_release(far_tags, cfg) or ""))
eq(landed2, expected2,
   "an import whose tags name another artist folder lands at the script's own name")
ok(not os.path.isdir(folder9),
   "so the placeholder's payload-named folder is gone", folder9)
ok(any(f.lower().endswith(".wav") for f in os.listdir(landed2)),
   "and its track is in there", os.listdir(landed2))
ok(bool(pathmod.load_pending(landed2)), "with the marker")
eq(res2.get("errors") or [], [], "with no error reported")

# An import that was never adopted (aimed at a folder of its own — the wizard,
# the Downloads panel) and COMPLETES somewhere else still ends the placeholder:
# the import's own end matches the release identity, not the folder name, so
# the empty album cannot survive beside the album it was created for.
stray = release_variant(8, "Stray Add")
rowS = pending_albums.create(stray, cfg)
folderS = rowS["album_path"].replace("/", os.sep)
ok(os.path.isdir(folderS), "the stray case has its framework album")
arrived = os.path.join(MF, "Artists", "Test Artist 8", "Stray Add (imported)")
write_tagged_wav(os.path.join(arrived, "1-01 One.wav"), {
    "ALBUM": stray["title"],
    "ALBUMARTIST": "Test Artist 8",
    "ARTIST": "Test Artist 8",
    "MUSICBRAINZ_ALBUMARTISTID": stray["artists"][0]["mbid"],
    "MUSICBRAINZ_ARTISTID": stray["artists"][0]["mbid"],
    "MUSICBRAINZ_ALBUMID": stray["id"],
    "MUSICBRAINZ_RELEASEGROUPID": stray["release_group_id"],
    "RELEASETYPE": "Album",
    "DATE": stray["date"],
    "ORIGINALDATE": stray["originaldate"],
    "MEDIA": "CD",
    "RELEASECOUNTRY": stray["country"],
    "CATALOGNUMBER": stray["catalog_number"],
    "LABEL": stray["label"],
    "DISCNUMBER": "1",
    "TRACKNUMBER": "1",
    "TITLE": "One",
})
eq(pending_albums.clear_if_filled(arrived, cfg, chained=True), False,
   "the folder the import landed in is not a framework album itself")
ok(not os.path.isdir(folderS),
   "yet the placeholder standing for ITS release is gone", folderS)
eq([d for d in pending_albums._scan_pending(MF)
    if int((pathmod.load_pending(d) or {}).get("wish_id") or 0) == int(rowS["wish_id"])],
   [], "and no framework folder is left for that release anywhere")
eq(os.path.normcase(str((wishes.get_wish(rowS["wish_id"]) or {}).get("album_path") or "")),
   os.path.normcase(arrived),
   "the wish now names the album that really arrived")

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

    # The real policy, captured BEFORE the stubs below replace it: this section
    # ends by putting it back, and a "real" captured after the first stub would
    # restore a stub (the later cases here exercise the policy itself).
    real_targets = intg.auto_import_targets
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

# --------------------------------------------------------------------------- #
# 9. the deferred add, the re-add of a wish that had ENDED, a release the
#    library already has, and the add that only NAMES its release
# --------------------------------------------------------------------------- #
print("\nthe deferred add, the re-add, and the add by name")
try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except Exception as e:                                            # pragma: no cover
    print(f"  SKIP  TestClient unavailable: {e}")
else:
    from server import api_add, api_queue
    from server import integrations as intg
    from server import wishes_worker

    add_app = FastAPI()
    add_app.include_router(api_add.router)
    add_client = TestClient(add_app)
    queue_app = FastAPI()
    queue_app.include_router(api_queue.router)
    queue_client = TestClient(queue_app)

    def rows_for(wish_id, section="queued"):
        payload = queue_client.get("/api/queue").json()
        return [r for r in payload["sections"][section]
                if r.get("wish_id") == wish_id]

    def until(pred, timeout=30.0):
        end = real_time.monotonic() + timeout
        while real_time.monotonic() < end:
            if pred():
                return True
            real_time.sleep(0.05)
        return pred()

    real_targets = intg.auto_import_targets
    real_resolve = intg.resolve_release
    real_trigger = wishes_worker.trigger
    real_owned = wishes.owned_mbids
    real_search = intg.search_mb

    def unique_release(tag, title, artist):
        """A test release whose ids are REAL UUIDs — `release_variant`'s own
        ids grow past eight characters for n > 9, and the route PARSES what it
        is given (`integrations._mbid`), so a longer first segment is not an id
        at all (the policy's "already in the library" check then misses, which
        is exactly what this case is about)."""
        rel = release_variant(1, title)
        rel["id"] = f"{tag}-1111-1111-1111-111111111111"
        rel["release_group_id"] = f"{tag}-2222-2222-2222-222222222222"
        rel["artists"] = [{"name": artist,
                           "mbid": f"{tag}-3333-3333-3333-333333333333"}]
        return rel

    # ---- the reply does not wait for MusicBrainz -------------------------- #
    slow = unique_release("0a0a0a0a", "Deferred Add", "Test Artist 11")
    lookup_started = threading.Event()
    lookup_may_finish = threading.Event()
    kicks = []

    def slow_targets(mbid, kind=None, mode="best", types=None, limit=None):
        """MusicBrainz takes as long as it takes: the reply must not be
        waiting on it, which is the whole point of the deferred add."""
        lookup_started.set()
        lookup_may_finish.wait(30)
        return [{"mbid": slow["id"], "title": slow["title"]}], []

    intg.auto_import_targets = slow_targets
    intg.resolve_release = lambda mbid: (slow, slow["id"])
    wishes_worker.trigger = lambda wid=None: kicks.append(wid) or {"ok": True}

    began = real_time.monotonic()
    replied = add_client.post("/api/library/add", json={
        "mbid": slow["release_group_id"], "kind": "release_group",
        "title": slow["title"], "artist": "Test Artist 11", "year": "1997"})
    took = real_time.monotonic() - began
    eq(replied.status_code, 200, "a deferred add answers 200")
    body = replied.json()
    ok(took < 1.0, "in well under a second, whatever MusicBrainz is doing",
       f"{took:.2f}s")
    eq(body.get("background"), True, "the reply says the work goes on without it")
    eq(body.get("resolving"), True, "and that the release is still being resolved")
    eq(body.get("note"), ("Added to your library — MusicBrainz is still being "
                          "asked what this release is, and the search starts as "
                          "it answers."),
       "in the server's own words about that state")
    albums = body.get("albums") or []
    eq(len(albums), 1, "the framework album is in the reply")
    eq(albums[0].get("resolving"), True, "carrying the live flag")
    eq(albums[0].get("created"), True, "and the fact that it created it")
    d_wish = albums[0]["wish_id"]
    d_folder = str(albums[0]["album_path"]).replace("/", os.sep)
    ok(os.path.isdir(d_folder), "the folder is on disk when the reply is written",
       d_folder)
    ok(bool(pathmod.load_pending(d_folder)), "marked pending")
    eq(kicks, [], "nothing is searching yet: MusicBrainz has not named the release")
    ok(lookup_started.wait(10), "the lookup runs on its own thread")
    open_rows = rows_for(d_wish)
    eq(len(open_rows), 1, "the deferred add has one row on the queue")
    eq(open_rows[0]["stage"], "searching_musicbrainz",
       "saying MusicBrainz is being asked, not that a download is coming")
    eq(open_rows[0]["pending"], True, "as a framework album")
    ok(open_rows[0]["cancelable"], "that can still be taken back off the queue",
       open_rows[0])

    # ---- the resolution lands: ONE album, and the search starts ----------- #
    lookup_may_finish.set()
    ok(until(lambda: not pending_albums.is_resolving(d_folder)),
       "the resolving flag is cleared when the resolution lands")
    ok(until(lambda: bool(kicks)), "and the search for what it recorded is started")
    wish_row = wishes.get_wish(d_wish)
    landed = str((wish_row or {}).get("album_path") or "").replace("/", os.sep)
    ok(os.path.isdir(landed), "the wish points at the album it recorded", landed)
    eq([t["title"] for t in (pathmod.load_expected_tracks(landed) or {}).get("tracks", [])],
       ["One", "Two"], "the release's own tracklist is on the album now")
    marked = [d for d in pending_albums._scan_pending(MF)
              if int((pathmod.load_pending(d) or {}).get("wish_id") or 0) == int(d_wish)]
    ok(len(marked) == 1, "exactly one framework folder stands for the add", marked)
    eq(os.path.normcase(marked[0]) if marked else "", os.path.normcase(landed),
       "and it is the one its wish names")
    settled = rows_for(d_wish)
    eq(len(settled), 1, "still one queue row for it")
    ok(settled and settled[0]["stage"] in ("queued", "searching"),
       "now the ordinary wish row", settled[0]["stage"] if settled else None)

    # ---- a wish that ENDED, re-added, is searched again -------------------- #
    again = unique_release("0b0b0b0b", "Readded Album", "Test Artist 12")
    intg.auto_import_targets = lambda mbid, kind=None, mode="best", types=None, limit=None: (
        [{"mbid": again["id"], "title": again["title"]}], [])
    intg.resolve_release = lambda mbid: (again, again["id"])
    first = add_client.post("/api/library/add",
                            json={"mbid": again["release_group_id"],
                                  "kind": "release_group"})
    a_wish = (first.json().get("albums") or [{}])[0].get("wish_id")
    ok(bool(a_wish), "the re-add case recorded its wish", first.json())
    wishes.mark_not_found(a_wish, "nothing usable found on the network")
    ended = wishes.get_wish(a_wish)
    eq(ended["status"], "not_found", "the wish ENDED (nothing was found)")
    eq(wishes.due_at(ended, load_config()), float("inf"),
       "and no pass will ever search it again — that is what terminal means")
    kicks.clear()
    second = add_client.post("/api/library/add",
                             json={"mbid": again["release_group_id"],
                                   "kind": "release_group"})
    eq(second.status_code, 200, "re-adding it answers 200")
    live = wishes.get_wish(a_wish)
    eq(live["status"], "wanted", "the terminal verdict is cleared")
    eq(live["not_found"], 0, "its empty-search counter is reset")
    ok(wishes.due_at(live, load_config()) <= real_time.time() + 1,
       "…and it is due NOW, so the worker's next pass searches it")
    eq(second.json().get("note"), "Soulseek is searching for them now.",
       "and the promise the reply makes is one the queue will keep")
    eq([w["id"] for w in wishes.list_wishes()
        if w["release_mbid"] == again["id"]], [a_wish], "one wish, not two")
    eq(kicks, [None], "and the pass that searches it is started")

    # ---- a release the library already has -------------------------------- #
    owned_rel = unique_release("0d0d0d0d", "Owned Add", "Test Artist 14")
    owned_folder = os.path.join(MF, "Artists", "Test Artist 14", "Owned Add")
    os.makedirs(owned_folder, exist_ok=True)
    add_audio(owned_folder)                    # a real album, audio on disk
    wishes.owned_mbids = lambda cfg=None: {
        owned_rel["release_group_id"].lower(): owned_folder}
    intg.auto_import_targets = real_targets    # the policy's own owned check
    intg.resolve_release = lambda mbid: (owned_rel, owned_rel["id"])
    kicks.clear()
    owned_post = add_client.post("/api/library/add",
                                 json={"mbid": owned_rel["release_group_id"],
                                       "kind": "release_group"})
    eq(owned_post.status_code, 200, "adding a release the library has answers 200")
    owned_body = owned_post.json()
    eq(owned_body.get("albums"), [], "and creates no framework album")
    eq(owned_body.get("note"), "It is already in your library.",
       "saying exactly that, instead of promising a search")
    eq([s.get("reason") for s in owned_body.get("skipped") or []],
       ["already in the library"], "with the policy's own reason")
    eq(kicks, [], "and nothing is queued for an album that is already here")
    eq([d for d in pending_albums._scan_pending(MF)
        if str((pathmod.load_pending(d) or {}).get("release_group_id") or "").lower()
        == owned_rel["release_group_id"].lower()], [],
       "no framework folder was left behind for it")
    wishes.owned_mbids = real_owned

    # ---- a lookup that FAILS lands somewhere, and says why ---------------- #
    broke = unique_release("0c0c0c0c", "Broken Lookup", "Test Artist 13")

    def broken_targets(mbid, kind=None, mode="best", types=None, limit=None):
        raise RuntimeError("MusicBrainz did not answer")

    intg.auto_import_targets = broken_targets
    intg.resolve_release = lambda mbid: (broke, broke["id"])
    bad_post = add_client.post("/api/library/add", json={
        "mbid": broke["release_group_id"], "kind": "release_group",
        "title": broke["title"], "artist": "Test Artist 13", "year": "1997"})
    eq(bad_post.status_code, 200, "an add whose lookup fails still answers")
    bad_album = (bad_post.json().get("albums") or [{}])[0]
    bad_folder = str(bad_album.get("album_path") or "").replace("/", os.sep)
    bad_wish = bad_album.get("wish_id")
    ok(os.path.isdir(bad_folder),
       "its framework album is there while the lookup runs")
    ok(until(lambda: not os.path.isdir(bad_folder)),
       "and is taken down when the lookup fails (nothing would ever fill it)")
    failed_wish = wishes.get_wish(bad_wish)
    eq(failed_wish["status"], "wanted",
       "the request itself stays on the queue, so the search can still be run")
    # The reason is written one step AFTER the placeholder folder comes down
    # (server.api_add: `remove_for_wish`, then the library look-up, then
    # `mark_wanted`), and the placeholder's removal is what the wait above
    # polls for — so reading the row at this instant raced the write and lost
    # on a loaded machine. The claim is the same one; the read waits for it.
    ok(until(lambda: "MusicBrainz" in str(
            (wishes.get_wish(bad_wish) or {}).get("last_error") or "")),
       "with the reason recorded on it",
       (wishes.get_wish(bad_wish) or {}).get("last_error"))
    bad_rows = rows_for(bad_wish)
    eq(len(bad_rows), 1, "the queue still has its row")
    ok("MusicBrainz" in str(bad_rows[0].get("reason") or ""),
       "saying what went wrong", bad_rows[0].get("reason"))
    intg.auto_import_targets = real_targets
    intg.resolve_release = real_resolve

    # ---- an add that only NAMES its release -------------------------------- #
    named = unique_release("0e0e0e0e", "Named Album", "Test Artist 15")
    intg.search_mb = lambda entity, query, **kw: (
        {"rows": [{"id": named["release_group_id"], "score": 100, "title": query,
                   "first_release_date": "1997"}], "total": 1, "offset": 0,
         "next": None, "query": query} if entity == "release-group" else
        {"rows": [], "total": 0, "offset": 0, "next": None, "query": query})
    intg.auto_import_targets = lambda mbid, kind=None, mode="best", types=None, limit=None: (
        [{"mbid": named["id"], "title": named["title"]}], [])
    intg.resolve_release = lambda mbid: (named, named["id"])
    hit = add_client.post("/api/library/add", json={
        "mbid": "", "kind": "album", "title": named["title"],
        "artist": "Test Artist 15", "year": "1997", "source": "Deezer",
        "page_url": "https://example.invalid/album/15"})
    eq(hit.status_code, 200, "an add that names its release answers 200")
    hit_body = hit.json()
    eq(hit_body.get("matched"), True, "and says MusicBrainz matched it")
    hit_albums = hit_body.get("albums") or []
    eq(len(hit_albums), 1, "the add went through as an ordinary one")
    eq(hit_albums[0].get("release_group_id"), named["release_group_id"],
       "for the entity MusicBrainz answered with")
    # A MATCHED name-only add continues exactly as an id-given one, and with a
    # title and an artist in hand that is the DEFERRED path: its resolution
    # runs on a daemon thread and starts the search when it lands
    # (`_create_all` → `wishes_worker.trigger`). Waiting for that kick here is
    # what keeps the NEXT case's count its own — the thread's kick is not
    # ordered against this request's reply, and the case below clears `kicks`
    # and then asserts exactly one entry, which a straggler would break.
    ok(until(lambda: bool(kicks)), "and its own resolution starts the search")
    kicks.clear()

    # …and with nothing to match, the NAME is what is recorded: no id is
    # invented, and nothing claims MusicBrainz matched it.
    intg.search_mb = lambda entity, query, **kw: {
        "rows": [], "total": 0, "offset": 0, "next": None, "query": query}
    kicks.clear()
    gap = add_client.post("/api/library/add", json={
        "mbid": "", "kind": "album", "title": "No Such Album 16",
        "artist": "No Such Artist 16", "source": "Deezer",
        "page_url": "https://example.invalid/album/16"})
    eq(gap.status_code, 200, "an add MusicBrainz cannot match answers 200")
    gap_body = gap.json()
    eq(gap_body.get("matched"), False, "saying it was not matched")
    eq(gap_body.get("by_name"), True, "that it is keyed by name instead")
    ok(bool(gap_body.get("wish_id")), "on a wish the queue can search", gap_body)
    eq(gap_body.get("albums"), [],
       "with no framework album (there is no id that could ever tie one to it)")
    gap_wish = wishes.get_wish(gap_body["wish_id"])
    ok(str(gap_wish["release_mbid"]).startswith("name:"),
       "the wish is keyed by the NAME", gap_wish["release_mbid"])
    eq(gap_wish["source"], "soulseek",
       "recorded as a by-name request, never as a MusicBrainz match")
    eq(gap_wish["artist"], "No Such Artist 16", "with the artist the row gave")
    eq(gap_wish["title"], "No Such Album 16", "and its album")
    ok("Deezer" in str(gap_wish["note"]), "and where the row came from",
       gap_wish["note"])
    eq(kicks, [None], "and the search for it is started")
    gap_rows = rows_for(gap_body["wish_id"])
    eq(len(gap_rows), 1, "the by-name wish has a row on the queue")
    eq(gap_rows[0]["source"], "Soulseek", "labelled as the by-name request it is")
    eq(gap_rows[0]["pending"], False, "with no framework album claiming otherwise")
    eq(gap_rows[0]["stage"], "queued", "and no MusicBrainz step it never had")
    eq(gap_body.get("note"), "MusicBrainz has no match for it — it is on the "
                             "queue to be searched by name.",
       "the reply says which of the two happened")

    # …and nothing to search BY is refused rather than recorded as an empty wish
    empty = add_client.post("/api/library/add", json={"mbid": "", "kind": "album"})
    eq(empty.status_code, 400, "an add with neither artist nor title is refused")

    # ---- an add records the group's ranked editions (spec R150/R169) ------- #
    # The walk is what makes a scarce release findable: without the list the
    # search stops at the one edition the add resolved, and a pressing whose
    # folders hold nothing usable ENDS the release instead of moving on to the
    # next — which is what the artist watch's path always did and this route
    # never did. So the add has to hand the wish the list
    # `integrations.group_targets` resolved: best first, catalog numbers
    # included (that is what R169 dedupes by, and what the queue row's badge
    # reports the position of).
    walked = unique_release("0f0f0f0f", "Walked Album", "Test Artist 17")
    group_rows = [
        {"mbid": walked["id"], "title": walked["title"], "score": 900,
         "catalog_numbers": ["WALK-1"]},
        {"mbid": walked["release_group_id"], "title": "Walked Album (JP)",
         "score": 800, "catalog_numbers": ["WALK-1J"]},
    ]
    intg.auto_import_targets = lambda mbid, kind=None, mode="best", types=None, limit=None: (
        [{"mbid": walked["id"], "title": walked["title"],
          "release_group_id": walked["release_group_id"],
          "candidates": group_rows}], [])
    intg.resolve_release = lambda mbid: (walked, walked["id"])
    walk_run = add_client.post("/api/library/add", json={
        "mbid": walked["id"], "kind": "release_group", "title": walked["title"],
        "artist": "Test Artist 17", "year": "1999"})
    eq(walk_run.status_code, 200, "a release-group add answers 200")
    walk_wish_id = (walk_run.json().get("albums") or [{}])[0].get("wish_id")
    ok(bool(walk_wish_id) and until(lambda: bool(wishes.get_wish(walk_wish_id))),
       "and its wish is on the queue", walk_wish_id)
    # The wish exists from the REQUEST (create_from_request names it from the
    # title it was given); the ranked list arrives with the resolution, which
    # runs on a daemon thread — so wait for the walk itself, not the row.
    ok(until(lambda: wishes.walk_length(wishes.get_wish(walk_wish_id), cfg) == 2),
       "the add records the group's ranked editions on the wish",
       (wishes.get_wish(walk_wish_id) or {}).get("candidates"))
    stored = (wishes.get_wish(walk_wish_id) or {}).get("candidates") or []
    eq([r.get("mbid") for r in stored],
       [walked["id"], walked["release_group_id"]],
       "best first, exactly as the policy ranked them")
    eq([r.get("catalog_numbers") for r in stored], [["WALK-1"], ["WALK-1J"]],
       "with the catalog numbers the walk dedupes by")
    walk_row = rows_for(walk_wish_id)
    eq(len(walk_row), 1, "the walk is still ONE row of the queue")
    walk = walk_row[0].get("walk") or {}
    eq((walk.get("total"), walk.get("label")), (2, "Release 1 of 2"),
       "and the row states the walk's size and position in the shared wording")
    intg.auto_import_targets = real_targets
    intg.resolve_release = real_resolve

    intg.search_mb = real_search
    intg.auto_import_targets = real_targets
    intg.resolve_release = real_resolve
    wishes_worker.trigger = real_trigger

# --------------------------------------------------------------------------- #
# 10. the state that leaves an empty album behind, and the sweep that heals it
# --------------------------------------------------------------------------- #
print("\nframework albums nothing would ever fill")
from server import interrupt_recovery  # noqa: E402

# A wish whose release the library "has" — where the folder proving it is an
# AUDIO-LESS framework album (the library listed the placeholder the add itself
# created). Marking the wish imported on that evidence is what left a request
# terminal with an empty folder standing behind it for ever.
ph = dict(release_variant(1, "Reconcile Add"))
ph["id"] = "0f1f1f1f-1111-1111-1111-111111111111"
ph["release_group_id"] = "0f1f1f1f-2222-2222-2222-222222222222"
ph["artists"] = [{"name": "Test Artist Ph", "mbid": "0f1f1f1f-3333-3333-3333-333333333333"}]
ph_row = pending_albums.create(ph, cfg)
ph_folder = ph_row["album_path"].replace("/", os.sep)
real_owned = wishes.owned_mbids
wishes.owned_mbids = lambda cfg=None: {ph["id"].lower(): ph_folder}
eq(wishes.reconcile_with_library(cfg), 0,
   "reconciliation refuses a folder that holds no audio")
ok(wishes.get_wish(ph_row["wish_id"])["status"] != "imported",
   "so the wish stays open for the search that will fill it")
add_audio(ph_folder)                              # the audio arrives
eq(wishes.reconcile_with_library(cfg), 1, "…and resolves it once the audio is there")
eq(wishes.get_wish(ph_row["wish_id"])["status"], "imported", "…as imported")

# An ENDED wish whose framework album is still empty: nothing searches a
# terminal wish again by itself, so the album would stand there for good. The
# startup sweep re-arms the request instead — the search is what was missing.
dead = dict(release_variant(1, "Ended Add"))
dead["id"] = "0f2f2f2f-1111-1111-1111-111111111111"
dead["release_group_id"] = "0f2f2f2f-2222-2222-2222-222222222222"
dead["artists"] = [{"name": "Test Artist Dead", "mbid": "0f2f2f2f-3333-3333-3333-333333333333"}]
dead_row = pending_albums.create(dead, cfg)
dead_folder = dead_row["album_path"].replace("/", os.sep)
wishes.mark_not_found(dead_row["wish_id"], "nothing usable found")
ok(wishes.is_terminal(wishes.get_wish(dead_row["wish_id"]), cfg),
   "the ended wish is terminal, so no pass would search it again")
sweep = interrupt_recovery.startup_recovery(cfg, log=lambda _t: None)
rearmed = [r for r in sweep.get("pending") or [] if r.get("kind") == "pending_rearmed"
           and os.path.normcase(r.get("folder") or "") == os.path.normcase(dead_folder)]
ok(len(rearmed) == 1, "the sweep re-arms the wish whose album is still empty",
   sweep.get("pending"))
eq(wishes.get_wish(dead_row["wish_id"])["status"], "wanted",
   "so the search for it runs again")
ok(os.path.isdir(dead_folder), "and its framework album stays as its placeholder")

# …and the other end of the same state: a placeholder whose album IS in the
# library (real audio, elsewhere) is taken down, so the library cannot show an
# empty album beside the one it is for.
dup = dict(release_variant(1, "Dup Add"))
dup["id"] = "0f3f3f3f-1111-1111-1111-111111111111"
dup["release_group_id"] = "0f3f3f3f-2222-2222-2222-222222222222"
dup["artists"] = [{"name": "Test Artist Dup", "mbid": "0f3f3f3f-3333-3333-3333-333333333333"}]
dup_row = pending_albums.create(dup, cfg)
dup_folder = dup_row["album_path"].replace("/", os.sep)
dup_real = os.path.join(MF, "Artists", "Test Artist Dup", "Dup Add (real)")
os.makedirs(dup_real, exist_ok=True)
add_audio(dup_real)
wishes.mark_not_found(dup_row["wish_id"], "nothing usable found")
wishes.owned_mbids = lambda cfg=None: {dup["id"].lower(): dup_real}
sweep2 = interrupt_recovery.startup_recovery(cfg, log=lambda _t: None)
removed = [r for r in sweep2.get("pending") or [] if r.get("kind") == "pending_duplicate"
           and os.path.normcase(r.get("folder") or "") == os.path.normcase(dup_folder)]
ok(len(removed) == 1, "the sweep removes the placeholder whose album is really here",
   sweep2.get("pending"))
ok(not os.path.isdir(dup_folder), "the empty album is gone from the library")
eq(os.path.normcase(str((wishes.get_wish(dup_row["wish_id"]) or {}).get("album_path") or "")),
   os.path.normcase(dup_real), "and its wish names the album that really arrived")
wishes.owned_mbids = real_owned

print()
if FAILED:
    print(f"FAILED ({len(FAILED)}): " + ", ".join(FAILED))
    sys.exit(1)
print("All Add-to-library checks passed.")
sys.exit(0)
