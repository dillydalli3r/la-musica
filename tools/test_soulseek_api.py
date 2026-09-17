#!/usr/bin/env python3
"""Verify the slskd REST calls match slskd's real routes/DTOs.

Downloads silently did nothing because enqueue posted to a non-existent
`/downloads` route with the wrong body. These asserts pin the contract.

Run:  python tools/test_soulseek_api.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import soulseek

calls = []


def fake_request(method, path, json_body=None, timeout=30.0):
    calls.append((method, path, json_body))
    return {"id": "x"} if method == "POST" and path == "/searches" else None


soulseek._request = fake_request

soulseek.enqueue_download("Some User", [{"filename": "a\\b.flac", "size": 123}])
method, path, body = calls[-1]
assert method == "POST", method
assert path == "/transfers/downloads/Some%20User", path
assert body == [{"filename": "a\\b.flac", "size": 123}], body

soulseek.rescan_shares()
assert calls[-1][:2] == ("PUT", "/shares"), calls[-1][:2]

# cancelling a rejected transfer: DELETE /transfers/downloads/{user}/{id}
# with ?remove=true, or slskd keeps re-queueing the files the auto-importer
# just deleted.
guid = "0b1a2c3d-4e5f-6789-abcd-ef0123456789"
soulseek.cancel_downloads("Some User", [guid, None])
method, path, body = calls[-1]
assert method == "DELETE", method
assert path == f"/transfers/downloads/Some%20User/{guid}?remove=true", path

soulseek.search("q", timeout_ms=45000)
method, path, body = calls[-1]
# MILLISECONDS: slskd hands the value to Soulseek.NET as ms, so a
# seconds-style number (45) ends the search in ~milliseconds and returns
# nothing — measured against slskd 0.26.
assert body.get("searchTimeout") == 45000, body
assert "timeout" not in body, body
soulseek.search("q")
assert "searchTimeout" not in calls[-1][2], calls[-1][2]

# slskd's option section is `directories` (Options.DirectoriesOptions). The
# old `dirs:` spelling was silently ignored, so slskd saved downloads into
# its own default folder while the app waited — forever — for files under
# its configured download dir (this was the auto-import hang).
import os
import tempfile

_root = tempfile.mkdtemp(prefix="mlo_slskd_cfg_")
_cfg = {
    "music_folder": os.path.join(_root, "music"),
    "soulseek_download_dir": os.path.join(_root, "downloads"),
    "soulseek_username": "",
    "soulseek_share_library": False,
}
text, _key = soulseek.generate_yaml(_cfg)
assert "\ndirectories:" in text, text
assert "\ndirs:" not in text, text
assert "  downloads:" in text, text
assert "  incomplete:" in text, text

# ...and both directories must exist before slskd boots, or it refuses to
# start (DirectoryExists validator) and the app reports "did not become ready".
_downloads = soulseek._ensure_dirs(_cfg)
assert os.path.isdir(_downloads), _downloads
# ...and the staging dir is the SIBLING `incomplete`, NOT a `.incomplete`
# child of the download dir: partials must never live in the folder the app
# lists, imports from and shares.
_inc = soulseek._incomplete_dir(_cfg)
assert _inc == os.path.join(os.path.dirname(_downloads), "incomplete"), _inc
assert os.path.isdir(_inc), _inc
assert not os.path.isdir(os.path.join(_downloads, ".incomplete")), _downloads

# slskd serializes SearchStates as bitwise flags joined with commas;
# 'Completed, TimedOut' must count as done or every search polls forever.
for done in ("Completed", "Completed, TimedOut", "TimedOut",
             "Completed, ResponseLimitReached", "Cancelled",
             "Errored", "FileLimitReached", "InProgress, FileLimitReached",
             "Completed, Errored"):
    assert soulseek.is_search_done(done), done
for pending in ("InProgress", "", None, "None"):
    assert not soulseek.is_search_done(pending), pending
assert soulseek.is_search_done({"state": "InProgress", "isComplete": True})
assert not soulseek.is_search_done({"state": "InProgress", "isComplete": False})

# ...but an Errored search is a failure, not an empty result set: the
# auto-importer must report it instead of "no candidate folders".
assert soulseek.search_error("Errored") == "Errored"
assert soulseek.search_error("Completed, Errored") == "Errored"
assert soulseek.search_error({"state": "Errored", "isComplete": True}) == "Errored"
assert soulseek.search_error("FileLimitReached") == ""
assert soulseek.search_error({"state": "Completed"}) == ""
assert soulseek.search_error(None) == ""

# browsing a remote user's shares is what the auto-importer's manual-folder
# picker and GET /api/soulseek/browse/{username} rely on. browse() hands the
# name to httpx raw, and httpx percent-encodes the space on the wire, so the
# route slskd actually receives is /users/Some%20User/browse.
import httpx

assert soulseek.browse("Some User") == []
method, path, body = calls[-1]
assert method == "GET", method
assert body is None, body
assert (str(httpx.Request("GET", "http://x" + path).url)
        == "http://x/users/Some%20User/browse"), path

# /api/soulseek/downloads/clear must drop only FINISHED transfers: filtering the
# transfer tree it gets from downloads_state() with finished_transfer() is the
# whole decision, and cancelling an InProgress/Queued transfer would delete the
# partial file the auto-importer is still waiting on.
tree = [{
    "username": "Some User",
    "directories": [{
        "directory": "Music\\Album",
        "files": [
            {"id": "1", "filename": "a.flac", "size": 1, "state": "Completed, Succeeded"},
            {"id": "2", "filename": "b.flac", "size": 1, "state": "Errored"},
            {"id": "3", "filename": "c.flac", "size": 1, "state": "InProgress"},
            {"id": "4", "filename": "d.flac", "size": 1, "state": "Queued"},
        ],
    }],
}]
_files = [f for d in tree[0]["directories"] for f in d["files"]]
_clear = [f["id"] for f in _files if soulseek.finished_transfer(f["state"])]
assert _clear == ["1", "2"], _clear
assert [f["id"] for f in _files if f["id"] not in _clear] == ["3", "4"], _files

# every state the clear endpoint promises to remove (substring = slskd's
# comma-joined flag strings), and none of the ones it must leave alone.
for st in (*soulseek._FINISHED_STATES, "Completed, Succeeded", "Completed, Errored",
           "Completed, Cancelled"):
    assert soulseek.finished_transfer(st), st
for st in ("InProgress", "Queued", "Requested", "Initializing", "None", "", None):
    assert not soulseek.finished_transfer(st), st

# --------------------------------------------------------------------------- #
# import_completed: slskd's REAL completed layout, the legacy per-user layout,
# and the "still downloading" guard (contract F)
# --------------------------------------------------------------------------- #
import json
import shutil
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    with open(os.path.join(REPO, "config.json"), encoding="utf-8") as _f:
        _REAL_MF = str((json.load(_f) or {}).get("music_folder") or "")
except Exception:
    _REAL_MF = ""

LAYOUT = tempfile.mkdtemp(prefix="mlo_import_layout_")
MF = os.path.join(LAYOUT, "music")
DD = os.path.join(LAYOUT, "downloads")
# safety: this suite never uses the developer's live library as a fixture
assert not _REAL_MF or not os.path.normcase(os.path.abspath(LAYOUT)).startswith(
    os.path.normcase(os.path.abspath(_REAL_MF))), (LAYOUT, _REAL_MF)

ART = os.path.join(MF, "Artists")   # library root every import lands in
ICFG = {"music_folder": MF, "soulseek_download_dir": DD}
os.makedirs(MF)


def put(rel, size=1024):
    p = os.path.join(DD, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"\0" * size)
    return p


# The config this app generates sets no destination pattern, so slskd's
# default ${SOURCE_DIRECTORY} applies: a completed transfer lands at
# <downloads>/<leaf of the remote folder>/<file> — NOT <user>/<leaf>/<file>.
put("Album With Discs/01 - a.flac")
put("Album With Discs/rip.log")
put("Album With Discs/CD2/02 - b.flac")       # disc subfolder stays inside the album
put("Another Album [FLAC]/01 - c.flac")
put("Still Downloading/02 - e.flac")          # a live transfer's folder
put("loose-in-root.flac")                     # bare file in the download root
# legacy per-user layout (older builds / the per-user download option): the
# user folder holds no album file of its own, so its CHILD is the album
put("Some User/Legacy Album/01 - d.flac")
put("Some User/scan.jpg")                     # leftover -> "Soulseek Some User"
# a folder that holds a loose file of its own IS one album, even when it also
# has album-bearing subfolders (rule 1 before rule 2: one album, moved whole)
put("Peer Singles/stray.flac")
put("Peer Singles/Album Two/01 - f.flac")
# never touched: no album file anywhere below, so there is nothing to import
put("junk/scans/cover.jpg")
put("junk/notes.txt")
# slskd's partials live in a dot dir and must never be walked or moved
put(".incomplete/Some User/Music/Album/09 - partial.flac")

# slskd's transfer tree: `filename` is the REMOTE path; the local album folder
# is its leaf (same folder name slskd writes the completed files into).
_real_downloads_state = soulseek.downloads_state
soulseek.downloads_state = lambda cfg=None: [{
    "username": "Some User",
    "directories": [
        {"directory": "Music\\Still Downloading",
         "files": [{"id": "1", "filename": "Music\\Still Downloading\\02 - e.flac",
                    "size": 1, "state": "InProgress"}]},
        # a FINISHED transfer must not hold its album back
        {"directory": "Music\\Album With Discs",
         "files": [{"id": "2", "filename": "Music\\Album With Discs\\01 - a.flac",
                    "size": 1, "state": "Completed, Succeeded"}]},
    ],
}]
try:
    moved = soulseek.import_completed(ICFG)
    skipped = soulseek.last_import_skipped()
finally:
    soulseek.downloads_state = _real_downloads_state

assert isinstance(moved, list), moved              # shape every caller unpacks
assert skipped == [os.path.join(DD, "Still Downloading")], skipped

# real layout: ONE album folder, moved whole (disc subfolder and log included)
album = os.path.join(ART, "Album With Discs")
assert album in moved, moved
assert os.path.isfile(os.path.join(album, "01 - a.flac"))
assert os.path.isfile(os.path.join(album, "rip.log"))
assert os.path.isfile(os.path.join(album, "CD2", "02 - b.flac"))
assert not os.path.exists(os.path.join(DD, "Album With Discs"))
assert os.path.join(ART, "Another Album [FLAC]") in moved, moved

# legacy per-user layout: the CHILD is the album, the user is not
legacy = os.path.join(ART, "Legacy Album")
assert legacy in moved, moved
assert os.path.isfile(os.path.join(legacy, "01 - d.flac"))
user_album = os.path.join(ART, "Soulseek Some User")
assert user_album in moved and os.path.isfile(os.path.join(user_album, "scan.jpg"))
assert not os.path.exists(os.path.join(DD, "Some User"))

# a folder holding a file of its own is ONE album even with album-bearing
# subfolders (rule 1 wins) — nothing is split off it
peers = os.path.join(ART, "Peer Singles")
assert peers in moved, moved
assert os.path.isfile(os.path.join(peers, "stray.flac"))
assert os.path.isfile(os.path.join(peers, "Album Two", "01 - f.flac"))
assert not os.path.exists(os.path.join(ART, "Album Two")), moved

# loose files directly in the download root -> one Soulseek album
assert os.path.join(ART, "Soulseek") in moved, moved
assert os.path.isfile(os.path.join(ART, "Soulseek", "loose-in-root.flac"))

# the import lands in the library root (<music>/Artists) — never the music
# folder root, which is shared to the network
assert all(os.path.normcase(os.path.abspath(p)).startswith(
    os.path.normcase(os.path.abspath(ART))) for p in moved), moved
assert not os.path.exists(os.path.join(MF, "Still Downloading"))
assert os.path.join(MF, "Still Downloading") not in moved, moved

# untouched: the live transfer's folder, the junk-only folder, the partials
assert os.path.isfile(os.path.join(DD, "Still Downloading", "02 - e.flac"))
assert os.path.isfile(os.path.join(DD, "junk", "notes.txt"))
assert os.path.isfile(os.path.join(DD, "junk", "scans", "cover.jpg"))
assert os.path.isfile(os.path.join(DD, ".incomplete", "Some User", "Music",
                                   "Album", "09 - partial.flac"))

# once the transfers finish, the same folder imports normally
soulseek.downloads_state = lambda cfg=None: []
moved2 = soulseek.import_completed(ICFG)
assert soulseek.last_import_skipped() == []
assert os.path.join(ART, "Still Downloading") in moved2, moved2
assert os.path.isfile(os.path.join(ART, "Still Downloading", "02 - e.flac"))
assert soulseek.import_completed(ICFG) == []        # nothing left to import
soulseek.downloads_state = _real_downloads_state

# --------------------------------------------------------------------------- #
# library root = <music folder>/Artists (contract A): organize() renames INTO
# it and /api/import/ingest resolves the album folder under it
# --------------------------------------------------------------------------- #
REDIRECT = tempfile.mkdtemp(prefix="mlo-import-root-redirect-")
# server.main's module-level state (playlists/wishes DBs) resolves through
# app_data_dir() — redirect the scope BEFORE importing it, so no file is ever
# created in the developer's real music folder.
os.environ["MLO_MUSIC_FOLDER"] = REDIRECT
import mlo.config as _cfgmod
import mlo.paths as _pathmod

_STUB = os.path.join(REDIRECT, "config.json")
with open(_STUB, "w", encoding="utf-8") as f:
    json.dump({"music_folder": REDIRECT}, f)
for _mod in (_cfgmod, _pathmod):
    _mod.CONFIG_FILE = _STUB
    if getattr(_mod, "LEGACY_DATA_DIR", None) is not None:
        _mod.LEGACY_DATA_DIR = os.path.join(REDIRECT, "legacy")

from server import main as mlo_main  # noqa: E402  (heavy import)

MF2 = tempfile.mkdtemp(prefix="mlo-import-root-")
assert not _REAL_MF or not os.path.normcase(os.path.abspath(MF2)).startswith(
    os.path.normcase(os.path.abspath(_REAL_MF))), (MF2, _REAL_MF)
mlo_main.load_config = lambda: {"music_folder": MF2, "naming_script": "",
                                "short_folder_names": False}

import server.naming as _naming  # noqa: E402

SRC_ALBUM = os.path.join(MF2, "Unsorted", "Some Album")
os.makedirs(SRC_ALBUM)
with open(os.path.join(SRC_ALBUM, "01 - a.flac"), "wb") as f:
    f.write(b"\0" * 4096)
with open(os.path.join(SRC_ALBUM, "cover.jpg"), "wb") as f:
    f.write(b"x")

# the naming script only names the path INSIDE Artists/ — patched to a fixed
# relative path so the check is about the base organize() joins it onto
_scan, _eval, _refresh = (mlo_main._scan_album_tracks, _naming.eval_script,
                          mlo_main._refresh_slskd_shares_soon)
mlo_main._scan_album_tracks = lambda p: [{
    "path": os.path.join(p, "01 - a.flac"), "file": "01 - a.flac",
    "tags": {"TITLE": "Track", "ARTIST": "Some Artist", "ALBUM": "Some Album"},
}]
_naming.eval_script = lambda script, vars_, shorter_ids=False: "Some Artist/Some Album/01 - Track"
mlo_main._refresh_slskd_shares_soon = lambda: None
try:
    org = mlo_main.organize(mlo_main.OrganizeRequest(paths=[SRC_ALBUM], dry_run=False))
finally:
    (mlo_main._scan_album_tracks, _naming.eval_script,
     mlo_main._refresh_slskd_shares_soon) = _scan, _eval, _refresh

_r = org["results"][0]
assert _r.get("ok"), _r
assert not _r["errors"], _r["errors"]
ROOT = os.path.join(MF2, "Artists", "Some Artist", "Some Album")
assert os.path.normcase(_r["album_root"]) == os.path.normcase(ROOT.replace("\\", "/")), _r["album_root"]
assert os.path.isfile(os.path.join(ROOT, "01 - Track.flac")), _r
assert os.path.isfile(os.path.join(ROOT, "cover.jpg"))       # leftover swept along
assert not os.path.exists(os.path.join(MF2, "Some Artist")), "album landed in the music-folder root"

# ingest: the album folder is created under Artists/
SRC_ING = os.path.join(REDIRECT, "incoming", "Ingested Album")
os.makedirs(SRC_ING)
with open(os.path.join(SRC_ING, "01 - x.flac"), "wb") as f:
    f.write(b"\0" * 2048)
ing = mlo_main.import_ingest(source=SRC_ING, target="Ingested Album")
DEST = os.path.join(MF2, "Artists", "Ingested Album")
assert os.path.normcase(ing["path"]) == os.path.normcase(DEST.replace("\\", "/")), ing
assert os.path.isfile(os.path.join(DEST, "01 - x.flac"))
assert not os.path.exists(SRC_ING)

# ...and a name collision is deduped inside Artists/ too
os.makedirs(SRC_ING)
with open(os.path.join(SRC_ING, "02 - y.flac"), "wb") as f:
    f.write(b"\0" * 2048)
ing2 = mlo_main.import_ingest(source=SRC_ING, target="Ingested Album")
assert os.path.normcase(ing2["path"]) == os.path.normcase(
    (DEST + " (2)").replace("\\", "/")), ing2

# --------------------------------------------------------------------------- #
# U3: transfer progress fields, junk folders, .incomplete pruning, locked move
# --------------------------------------------------------------------------- #
# _user_transfers must hand the progress payload (contract §2) its metrics
# WITHOUT changing what it filters: same user, same pending set, same states.
_UT_TREE = [{
    "username": "Some User",
    "directories": [{"directory": "Music\\Album", "files": [
        {"id": "1", "filename": "Music\\Album\\01.flac", "state": "InProgress",
         "bytesTransferred": 250, "size": 1000, "percentComplete": 25.0,
         "averageSpeed": 4096.5, "remainingTime": "00:01:30"},
        {"id": "2", "filename": "Music\\Album\\02.flac", "state": "Queued",
         "bytesTransferred": 300, "size": 600},     # slskd reported no metrics
        {"id": "3", "filename": "Music\\Album\\03.flac",
         "state": "Completed, Succeeded", "bytesTransferred": 700, "size": 700,
         "percentComplete": 100.0, "averageSpeed": 0, "remainingTime": "1.02:03:04"},
        {"id": "4", "filename": "Music\\Album\\04.flac", "state": "InProgress",
         "bytesTransferred": 150, "size": 100, "percentComplete": 150.0},
    ]}, {"directory": "Music\\Other", "files": [
        {"id": "5", "filename": "Music\\Other\\05.flac", "state": "InProgress",
         "bytesTransferred": 1, "size": 2},
    ]}],
}, {"username": "Other User", "directories": [{"directory": "x", "files": [
    {"id": "6", "filename": "Music\\Album\\01.flac", "state": "InProgress",
     "bytesTransferred": 1, "size": 2},
]}]}]


class _FakeSlsk:
    @staticmethod
    def downloads_state(cfg=None):
        return _UT_TREE


_UT_PENDING = {"Music\\Album\\01.flac", "Music\\Album\\02.flac",
               "Music\\Album\\03.flac", "Music\\Album\\04.flac"}
_rows = {t["id"]: t for t in
         soulseek._user_transfers(_FakeSlsk, "Some User", _UT_PENDING)}
assert set(_rows) == {"1", "2", "3", "4"}, _rows      # other user/other dir gone
for _rec in _rows.values():
    assert set(_rec) == {"id", "filename", "state", "bytes", "size", "percent",
                         "speed", "remaining"}, _rec
assert (_rows["1"]["bytes"], _rows["1"]["size"], _rows["1"]["percent"],
        _rows["1"]["speed"], _rows["1"]["remaining"]) == (250, 1000, 25, 4096.5, 90)
# percentComplete missing -> bytes/size; speed/remaining missing -> 0.0/None
assert (_rows["2"]["percent"], _rows["2"]["speed"], _rows["2"]["remaining"]) == (50, 0.0, None)
# slskd writes a TimeSpan past a day as "d.hh:mm:ss"
assert _rows["3"]["remaining"] == 86400 + 2 * 3600 + 3 * 60 + 4, _rows["3"]
assert _rows["4"]["percent"] == 100, _rows["4"]        # clamped, never > 100
assert [t["id"] for t in
        soulseek._user_transfers(_FakeSlsk, "Some User", _UT_PENDING,
                                 states=("InProgress",))] == ["1", "4"]

# ONE pooled client for every slskd call (the 1-3 s polling must not
# re-handshake per tick), rebuilt when the API key or base URL changes
_c1 = soulseek._http_client()
assert soulseek._http_client() is _c1, "a fresh client per request"
soulseek._proc["api_key"] = "rotated-key"          # slskd mints one per boot
try:
    assert soulseek._http_client() is not _c1, "stale API key reused"
finally:
    soulseek._proc["api_key"] = None
soulseek._close_client()
assert soulseek._http_client() is not _c1, "closed client handed out again"
soulseek._close_client()

# the move seam really is U1's mlo.paths.move_path (and its log is wired up)
import mlo.paths as _mpath

_seen_move = {}
_mp_orig = _mpath.move_path


def _spy_move(src, dst, *, attempts=40, delay=0.5, log=None):
    _seen_move.update(src=src, dst=dst, got_log=callable(log))
    return False


_mpath.move_path = _spy_move
try:
    _ok, _why = soulseek._move("srcX", "dstY")
finally:
    _mpath.move_path = _mp_orig
assert _seen_move == {"src": "srcX", "dst": "dstY", "got_log": True}, _seen_move
assert _ok is False and isinstance(_why, str) and _why, (_ok, _why)

# --------------------------------------------------------------------------- #
# import_completed: junk stays out of the library, empty staging trees go,
# a locked move is reported instead of raising
# --------------------------------------------------------------------------- #
L3 = tempfile.mkdtemp(prefix="mlo_u3_import_")
MF3 = os.path.join(L3, "music")
DD3 = os.path.join(L3, "downloads")
IFG = {"music_folder": MF3, "soulseek_download_dir": DD3}
os.makedirs(MF3)


def put3(rel, data=b"\0" * 64):
    p = os.path.join(DD3, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return p


put3("Real Album/01 - a.flac")
# the leftover from the live report: one peer's log + cue, no audio anywhere
LOGONLY = os.path.join(DD3, "[CDP 7243 8 29750 2 1] [USA, Rem 2000]")
put3("[CDP 7243 8 29750 2 1] [USA, Rem 2000]/soulseek.log")
put3("[CDP 7243 8 29750 2 1] [USA, Rem 2000]/soulseek.cue")
# slskd's staging leftovers: empty trees vs one still holding a partial file
os.makedirs(os.path.join(DD3, ".incomplete", "peerA", "Music", "Album"))
os.makedirs(os.path.join(DD3, ".incomplete", "peerB"))
put3(".incomplete/peerC/Music/Album/09 - partial.flac")

_real_state = soulseek.downloads_state
soulseek.downloads_state = lambda cfg=None: []
try:
    moved3 = soulseek.import_completed(IFG)
    leftovers = soulseek.last_import_leftovers()
    failed3 = soulseek.last_import_failed()
finally:
    soulseek.downloads_state = _real_state

assert moved3 == [os.path.join(MF3, "Artists", "Real Album")], moved3
assert failed3 == [], failed3
assert leftovers == [LOGONLY], leftovers
# left in place — never published as an album named after the peer's folder
assert os.path.isfile(os.path.join(LOGONLY, "soulseek.log"))
assert not os.path.exists(os.path.join(MF3, "Artists", os.path.basename(LOGONLY)))
# empty trees pruned, a tree holding a partial file untouched, root kept
assert not os.path.exists(os.path.join(DD3, ".incomplete", "peerA")), "empty tree kept"
assert not os.path.exists(os.path.join(DD3, ".incomplete", "peerB")), "empty tree kept"
assert os.path.isdir(os.path.join(DD3, ".incomplete"))
assert os.path.isfile(os.path.join(DD3, ".incomplete", "peerC", "Music",
                                   "Album", "09 - partial.flac"))

# a file slskd still holds open: move_path gives up, the album stays put, the
# returned list omits it, and the reason reaches the caller — no exception
put3("Locked Album/01 - a.flac")
LOCKED = os.path.join(DD3, "Locked Album")
_REAL_MOVE = soulseek._move
_REASON = ("move did not complete: [WinError 32] The process cannot access "
           "the file because it is being used by another process")
soulseek._move = lambda src, dst: (False, _REASON)
try:
    soulseek.downloads_state = lambda cfg=None: []
    moved4 = soulseek.import_completed(IFG)
    failed4 = soulseek.last_import_failed()
finally:
    soulseek._move = _REAL_MOVE
    soulseek.downloads_state = _real_state

assert moved4 == [], moved4
assert LOCKED not in moved4 and os.path.isfile(os.path.join(LOCKED, "01 - a.flac"))
assert [f["path"] for f in failed4] == [LOCKED], failed4
assert failed4[0]["reason"] == _REASON, failed4
assert not os.path.exists(os.path.join(MF3, "Artists", "Locked Album"))

# the still-downloading guard keys off slskd's REAL layout: the local album
# folder is `<downloads>/<leaf of the remote folder>`, so the leaf is what
# must match (case-insensitively) — and only for a live transfer
_LEAF = "Wish You Were Here (1975) - U.S. - CD (Capitol)"
soulseek.downloads_state = lambda cfg=None: [
    {"username": "a-peer", "directories": [{"directory": "Music\\" + _LEAF, "files": [
        {"id": "9", "filename": "Music\\" + _LEAF + "\\02 - t.flac",
         "state": "InProgress"}]}]},
    {"username": "b-peer", "directories": [{"directory": "x", "files": [
        {"id": "10", "filename": "Music\\Finished Album\\01.flac",
         "state": "Completed, Succeeded"}]}]},
]
try:
    _pend = soulseek._pending_album_folders(IFG)
finally:
    soulseek.downloads_state = _real_state
assert _pend == {_LEAF.lower()}, _pend

# --------------------------------------------------------------------------- #
# Soulseek private messaging (contract §0/§1): slskd faked at the request
# layer — the six conversation wrappers answer through `_request` (the seam
# the rest of this file uses), send_message through the pooled client, the one
# call whose meaning is the raw status. No slskd, no network.
# --------------------------------------------------------------------------- #
class _FakeSlskd:
    """Canned slskd responses on the `_request` seam, one per call."""

    def __init__(self, *responses):
        self.queued = list(responses)
        self.calls = []

    def __call__(self, method, path, json_body=None, timeout=30.0, **kw):
        self.calls.append((method, path, json_body))
        status, payload = self.queued.pop(0)
        if status >= 400:
            req = httpx.Request(method, "http://slskd" + path)
            raise httpx.HTTPStatusError(
                f"HTTP {status}", request=req,
                response=httpx.Response(status, request=req))
        return payload


class _FakeStatus:
    """A response as `_request_status` sees it: the status, no body."""

    def __init__(self, status_code):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("POST", "http://slskd/conversations")
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=req,
                response=httpx.Response(self.status_code, request=req))


class _FakeClient:
    """Pooled httpx client stand-in, one queued status per request."""

    def __init__(self, *statuses):
        self.queued = list(statuses)
        self.calls = []

    def request(self, method, path, json=None, headers=None, timeout=None, **kw):
        self.calls.append((method, path, json))
        return _FakeStatus(self.queued.pop(0))


_real_request = soulseek._request
_real_http_client = soulseek._http_client


def slskd(*responses):
    """Queue (status, payload) pairs for the next `_request` calls."""
    fake = _FakeSlskd(*responses)
    soulseek._request = fake
    return fake


def slskd_send(*statuses):
    """Queue raw statuses for the next pooled-client (send_message) calls."""
    client = _FakeClient(*statuses)
    soulseek._http_client = lambda *a, **k: client
    return client


SPACED = "Some User"
_ENC = "/conversations/Some%20User"

# percent-encoding: Soulseek usernames contain spaces and slskd's route is
# [UrlEncoded], so every wrapper must quote the name into the PATH. A raw
# space (or quote_plus' "+") targets a different resource and 404s upstream.
_c_conv = slskd((200, [{"username": SPACED, "isActive": True,
                        "unAcknowledgedMessageCount": 2}]))
assert soulseek.conversations()[0]["username"] == SPACED
_c_one = slskd((200, {"username": SPACED, "messages": []}))
assert soulseek.conversation(SPACED)["username"] == SPACED
_c_msgs = slskd((200, [{"id": 1, "message": "hi"}]))
assert soulseek.messages(SPACED) == [{"id": 1, "message": "hi"}]
_c_send = slskd_send(201)
assert soulseek.send_message(SPACED, "hi") == 201
_c_ackc = slskd((200, None))
assert soulseek.acknowledge_conversation(SPACED) is True
_c_ackm = slskd((200, None))
assert soulseek.acknowledge_message(SPACED, 7) is True
_c_close = slskd((204, None))
assert soulseek.close_conversation(SPACED) is True
for _client, _meth, _route in ((_c_conv, "GET", "/conversations"),
                               (_c_one, "GET", _ENC),
                               (_c_msgs, "GET", _ENC + "/messages"),
                               (_c_send, "POST", _ENC),
                               (_c_ackc, "PUT", _ENC),
                               (_c_ackm, "PUT", _ENC + "/7"),
                               (_c_close, "DELETE", _ENC)):
    _got_meth, _path, _body = _client.calls[-1]
    assert _got_meth == _meth, (_got_meth, _meth, _path)
    assert _path.split("?")[0] == _route, (_path, _route)
    assert " " not in _path and "+" not in _path, _path

# the SEND body is a bare JSON string — httpx serializes json="hello there" to
# `"hello there"`; slskd's POST expects exactly that, not {"message": ...}
_c_send = slskd_send(201)
assert soulseek.send_message(SPACED, "hello there") == 201
assert _c_send.calls[-1][2] == "hello there", _c_send.calls[-1]
assert not isinstance(_c_send.calls[-1][2], dict), _c_send.calls[-1]

# 201 = sent, 200 = the peer blacklisted/ignored us: two DISTINCT statuses, so
# the route can report sent: false without guessing
slskd_send(201)
assert soulseek.send_message(SPACED, "m") == 201
slskd_send(200)
assert soulseek.send_message(SPACED, "m") == 200

# 404 = the conversation does not exist upstream -> None/False, never a raise
# (messages() answers None, not [], so the caller can tell it from an empty
# thread — that is what turns GET /messages/{u} into a 404 instead of {})
slskd((404, None))
assert soulseek.conversation("ghost user") is None
slskd((404, None))
assert soulseek.messages("ghost user") is None
slskd((404, None))
assert soulseek.acknowledge_conversation("ghost user") is False
slskd((404, None))
assert soulseek.acknowledge_message("ghost user", 3) is False
slskd((404, None))
assert soulseek.close_conversation("ghost user") is False

# any other upstream error still propagates (the routes turn it into a 502)
for _call in (lambda: soulseek.conversations(),
              lambda: soulseek.conversation(SPACED),
              lambda: soulseek.messages(SPACED),
              lambda: soulseek.acknowledge_conversation(SPACED),
              lambda: soulseek.acknowledge_message(SPACED, 1),
              lambda: soulseek.close_conversation(SPACED)):
    slskd((500, {"error": "boom"}))
    try:
        _call()
    except httpx.HTTPStatusError as exc:
        assert exc.response.status_code == 500, exc.response
    else:
        raise AssertionError(f"{_call} swallowed a 500")
slskd_send(500)
try:
    soulseek.send_message(SPACED, "m")
except httpx.HTTPStatusError as exc:
    assert exc.response.status_code == 500, exc.response
else:
    raise AssertionError("send_message swallowed a 500")
soulseek._request = _real_request
soulseek._http_client = _real_http_client

# --------------------------------------------------------------------------- #
# Messaging ROUTES (§2) through FastAPI's TestClient with the wrapper layer
# patched; no `with` block, so no lifespan and no slskd boot.
# --------------------------------------------------------------------------- #
from fastapi.testclient import TestClient  # noqa: E402

_WRAPPER_NAMES = ("is_running", "web_up", "conversations", "conversation",
                  "messages", "send_message", "acknowledge_conversation",
                  "close_conversation")
_real_wrappers = {n: getattr(soulseek, n) for n in _WRAPPER_NAMES}
soulseek.is_running = lambda: True
soulseek.web_up = lambda *a, **k: True
_client = TestClient(mlo_main.app)

# list: unread total = sum of unAcknowledgedMessageCount, order unread-first
# then alphabetical (slskd supplies no order at all — this raw order is
# neither alphabetical nor unread-first, so dropping the sort fails here)
_RAW_CONVS = [
    {"username": "zoe", "isActive": True, "unAcknowledgedMessageCount": 0},
    {"username": "alice", "isActive": True, "unAcknowledgedMessageCount": 3},
    {"username": "bob", "isActive": False, "unAcknowledgedMessageCount": 0},
    {"username": "charlie", "isActive": True, "unAcknowledgedMessageCount": 2},
]
soulseek.conversations = lambda *a, **k: _RAW_CONVS
r = _client.get("/api/soulseek/messages")
assert r.status_code == 200, r.text
data = r.json()
assert data["ok"] is True, data
assert data["unread"] == 5, data                 # 3 + 2 + 0 + 0
assert [c["username"] for c in data["conversations"]] == [
    "alice", "charlie", "bob", "zoe"], data
for _c in data["conversations"]:
    assert set(_c) == {"username", "is_active", "unread"}, _c
assert {c["username"]: (c["is_active"], c["unread"]) for c in data["conversations"]} == {
    "alice": (True, 3), "charlie": (True, 2),
    "bob": (False, 0), "zoe": (True, 0)}, data

# thread: oldest-first (slskd's own order, passed through as-is) with the
# frozen field names; the username is percent-encoded on the wire and FastAPI
# decodes it back to the spaced name
_RAW_MSGS = [
    {"id": 1, "direction": "In", "message": "first",
     "timestamp": "2026-01-01T10:00:00Z", "isAcknowledged": False,
     "wasReplayed": False},
    {"id": 2, "direction": "Out", "message": "second",
     "timestamp": "2026-01-01T10:05:00Z", "isAcknowledged": True,
     "wasReplayed": True},
]
_seen_user = []


def _fake_messages(username, *a, **k):
    _seen_user.append(username)
    return list(_RAW_MSGS)


soulseek.messages = _fake_messages
r = _client.get("/api/soulseek/messages/Some%20User")
assert r.status_code == 200, r.text
data = r.json()
assert data["ok"] is True and data["username"] == SPACED, data
assert _seen_user[-1] == SPACED, _seen_user
assert [m["id"] for m in data["messages"]] == [1, 2], data
assert data["messages"][0] == {
    "id": 1, "direction": "In", "message": "first",
    "timestamp": "2026-01-01T10:00:00Z", "acknowledged": False,
    "replayed": False}, data["messages"][0]
assert data["messages"][1]["acknowledged"] is True, data["messages"][1]
assert data["messages"][1]["replayed"] is True, data["messages"][1]

# no such conversation upstream (messages() answers None) -> 404, not an
# empty thread
soulseek.messages = lambda *a, **k: None
r = _client.get("/api/soulseek/messages/Some%20User")
assert r.status_code == 404, r.text

# send: 400 on an empty/whitespace draft (never reaches slskd), sent: true for
# 201 and sent: false for a 200 (blacklisted/ignored)
_sent = []


def _fake_send(username, message, *a, **k):
    _sent.append((username, message))
    return _fake_send.status


_fake_send.status = 201
soulseek.send_message = _fake_send
r = _client.post("/api/soulseek/messages/Some%20User", json={"message": "hi there"})
assert r.status_code == 200 and r.json() == {"ok": True, "sent": True}, r.text
assert _sent == [(SPACED, "hi there")], _sent
_fake_send.status = 200                      # slskd took it but blacklisted us
r = _client.post("/api/soulseek/messages/Some%20User", json={"message": "hi"})
assert r.status_code == 200 and r.json() == {"ok": True, "sent": False}, r.text
for _blank in ("", "   ", "\n\t "):
    r = _client.post("/api/soulseek/messages/Some%20User", json={"message": _blank})
    assert r.status_code == 400, (_blank, r.status_code, r.text)
assert len(_sent) == 2, _sent                 # blanks never reached the wrapper

# read / close: frozen shapes, both truth values
soulseek.acknowledge_conversation = lambda username, *a, **k: True
r = _client.post("/api/soulseek/messages/Some%20User/read")
assert r.status_code == 200 and r.json() == {"ok": True, "acknowledged": True}, r.text
soulseek.acknowledge_conversation = lambda username, *a, **k: False
assert _client.post("/api/soulseek/messages/Some%20User/read").json() == {
    "ok": True, "acknowledged": False}
soulseek.close_conversation = lambda username, *a, **k: True
r = _client.delete("/api/soulseek/messages/Some%20User")
assert r.status_code == 200 and r.json() == {"ok": True, "closed": True}, r.text
soulseek.close_conversation = lambda username, *a, **k: False
assert _client.delete("/api/soulseek/messages/Some%20User").json() == {
    "ok": True, "closed": False}

# slskd down: every messaging route answers 400 "slskd is not running", like
# the neighbouring soulseek routes do
soulseek.is_running = lambda: False
soulseek.web_up = lambda *a, **k: False
for _meth, _url, _body in (("get", "/api/soulseek/messages", None),
                           ("get", "/api/soulseek/messages/alice", None),
                           ("post", "/api/soulseek/messages/alice", {"message": "hi"}),
                           ("post", "/api/soulseek/messages/alice/read", None),
                           ("delete", "/api/soulseek/messages/alice", None)):
    _call = getattr(_client, _meth)
    r = _call(_url, json=_body) if _body is not None else _call(_url)
    assert r.status_code == 400, (_meth, _url, r.status_code, r.text)
    assert "not running" in r.json()["detail"], (_meth, _url, r.text)

for _name, _orig in _real_wrappers.items():
    setattr(soulseek, _name, _orig)

shutil.rmtree(L3, ignore_errors=True)

os.environ.pop("MLO_MUSIC_FOLDER", None)
shutil.rmtree(LAYOUT, ignore_errors=True)
shutil.rmtree(MF2, ignore_errors=True)
shutil.rmtree(REDIRECT, ignore_errors=True)
assert not os.path.exists(LAYOUT) and not os.path.exists(MF2)

print("ok")
