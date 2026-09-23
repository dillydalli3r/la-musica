#!/usr/bin/env python3
"""GET /api/storage: honest numbers, quickly, and never a raised exception.

The card on the Home and Dependencies pages is only worth as much as this
answer, so this suite pins the three rules it follows:

  * WHAT IS COUNTED. The library figure is its audio plus the covers and text
    sidecars that belong to it — a stray archive in an album folder and
    anything under a skip-dir (``mlo.paths.SKIP_DIRS``) are not library
    storage and must not inflate it. The app-data, trash and downloads figures
    are about disk, so they count everything they hold; the staging half of a
    transfer is reported apart from the completed one.
  * WHAT HAPPENS WHEN THE OS SAYS NOTHING. A directory that refuses to be
    listed is skipped and NAMED (the walk does not raise), and a volume whose
    size cannot be read answers null — never 0, which would render as "0 bytes
    free".
  * THE ARITHMETIC. used = total - free, percent_used agrees with both, and a
    volume with no numbers at all leaves them all null while the folder
    figures are still answered.

Run:  python tools/test_storage.py
Exit 0 = pass, 2 = skip (fastapi/httpx missing).
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from fastapi.testclient import TestClient
except Exception as e:  # pragma: no cover - a missing extra is a SKIP
    print(f"skip: TestClient unavailable: {e}")
    sys.exit(2)

from server import api_storage  # noqa: E402

# --------------------------------------------------------------------------- #
# The tree: two albums, one stray file, one skip-dir, one unreadable dir
# --------------------------------------------------------------------------- #
ROOT = tempfile.mkdtemp(prefix="mlo_storage_")
MUSIC = os.path.join(ROOT, "Library")
ALBUM_A = os.path.join(MUSIC, "Artists", "Artist One", "Album A")
ALBUM_B = os.path.join(MUSIC, "Artists", "Artist Two", "Album B")
BAD = os.path.join(MUSIC, "Artists", "Unreadable")

# The app's own tools folders (mlo.paths.tools_dir + legacy_tools_dir) are
# whatever the machine has: 1.2 GB of downloaded tools on a dev box, a
# symlinked toolchain on CI (whose links the walk reports as unreadable folders
# — the card would then warn about them), nothing at all in a fresh clone. The
# snapshot reports them and folds them into `app_total`, so the test pins BOTH
# resolvers at a fixture of its own: the same assertions have to answer the same
# thing on every machine.
DEPS = os.path.join(ROOT, "deps")
api_storage.tools_dir = lambda music_folder=None: DEPS
api_storage.tools_dirs = lambda music_folder=None: [DEPS]


def make(path, size):
    """Write *size* bytes at *path* and return the byte count, so the expected
    sums below are written as sums of what was actually written."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\0" * size)
    return size


AUDIO = (make(os.path.join(ALBUM_A, "01 - Song.flac"), 1000)
         + make(os.path.join(ALBUM_A, "02 - Song.flac"), 2000)
         + make(os.path.join(ALBUM_B, "03 - Song.mp3"), 700))
# Library sidecars: an album cover, a playlist the exporter wrote, the album's
# description.
SIDECAR = (make(os.path.join(ALBUM_A, "cover.jpg"), 100)
           + make(os.path.join(ALBUM_A, "Album A.m3u8"), 42)
           + make(os.path.join(ALBUM_A, "description.txt"), 11))
# Not library storage: a stray archive, and anything under a skip-dir.
make(os.path.join(ALBUM_A, "notes.zip"), 9999)
make(os.path.join(ALBUM_A, "__pycache__", "junk.bin"), 5000)
make(os.path.join(ALBUM_A, ".mlo", "hidden.flac"), 4000)
make(os.path.join(BAD, "01 - Lost.flac"), 8888)

APP_DATA = make(os.path.join(MUSIC, ".mlo", "data", "state.db"), 777)
APP_DATA += make(os.path.join(MUSIC, ".mlo", "data", "artcache", "cover.bin"), 222)
TRASH = make(os.path.join(MUSIC, ".mlo", "trash", "default", "old.flac"), 333)
DONE = make(os.path.join(MUSIC, ".mlo", "downloads", "done.flac"), 444)
STAGED = make(os.path.join(MUSIC, ".mlo", "incomplete", "part.flac"), 555)
# The app's tools, outside the music folder (see DEPS above).
DEPS_BYTES = make(os.path.join(DEPS, "ffmpeg", "ffmpeg.exe"), 777)

CFG = {"music_folder": MUSIC, "soulseek_download_dir": ""}


class Unreadable:
    """Make ONE directory refuse to be listed.

    Windows has no chmod that does this, and the case is about the walk's
    OSError branch, not about permissions: the point is that an unreadable
    subdirectory is reported and stepped over instead of taking the whole
    answer down with it.
    """

    def __init__(self, path):
        self.path = os.path.normcase(os.path.abspath(path))
        self.real = None

    def __enter__(self):
        self.real = os.scandir

        def fake(target, *a, **kw):
            if os.path.normcase(os.path.abspath(str(target))) == self.path:
                raise PermissionError(13, "Permission denied", str(target))
            return self.real(target, *a, **kw)

        os.scandir = fake
        return self

    def __exit__(self, *exc):
        os.scandir = self.real
        return False


_REAL_FS = api_storage.read_fs_usage


def fs(total, free):
    """A stubbed volume: `None` means one the OS will not measure."""
    if total is None:
        return {"mount": "Z:\\", "label": "Z:", "type": "network",
                "total_bytes": None, "free_bytes": None, "used_bytes": None,
                "percent_used": None}
    return {"mount": "Z:\\", "label": "Z:", "type": "fixed",
            "total_bytes": total, "free_bytes": free, "used_bytes": total - free,
            "percent_used": round((total - free) * 100 / total, 1)}


# --------------------------------------------------------------------------- #
# 1) The sums, with one directory unreadable
# --------------------------------------------------------------------------- #
api_storage.read_fs_usage = lambda path: fs(1000, 400)
try:
    with Unreadable(BAD):
        snap = api_storage.storage_snapshot(CFG, MUSIC)
finally:
    api_storage.read_fs_usage = _REAL_FS

lib = snap["library"]
assert lib["bytes"] == AUDIO + SIDECAR, lib          # 3853: stray + skip-dirs out
assert lib["files"] == 6, lib                        # 3 tracks + 3 sidecars
assert lib["audio_bytes"] == AUDIO, lib              # 3700
assert lib["sidecar_bytes"] == SIDECAR, lib          # 153
# The skip-dir and the stray file are excluded because they were never added,
# not because another number happened to even out: the walk counted 6 entries
# and the raw sizes above differ from their sum by 18999 bytes.
assert lib["bytes"] != AUDIO + SIDECAR + 9999 + 5000 + 4000 + 8888

assert snap["app_data"]["bytes"] == APP_DATA, snap["app_data"]   # 999, everything
assert snap["app_data"]["files"] == 2, snap["app_data"]
assert snap["trash"]["bytes"] == TRASH, snap["trash"]            # 333
assert snap["downloads"]["bytes"] == DONE + STAGED, snap["downloads"]
assert snap["downloads"]["staging_bytes"] == STAGED, snap["downloads"]
assert snap["downloads"]["staging_files"] == 1, snap["downloads"]
assert len(snap["downloads"]["roots"]) == 2, snap["downloads"]
# The app's own tools, measured where they live (outside the music folder).
assert snap["dependencies"]["bytes"] == DEPS_BYTES, snap["dependencies"]
assert snap["dependencies"]["files"] == 1, snap["dependencies"]
# The app's OWN footprint is one figure: its state, the bin, the transfers and
# its tools. The library above is the user's music and is deliberately not in
# it — that is the whole point of the statistic.
assert snap["app_total"]["bytes"] == (
    APP_DATA + TRASH + DONE + STAGED + DEPS_BYTES), snap["app_total"]
assert snap["app_total"]["files"] == 6, snap["app_total"]   # 2 state, 1 bin, 2 transfers, 1 tool
assert snap["app_total"]["measured"] is True, snap["app_total"]
# ...and a footprint with NOTHING readable says so instead of answering a 0
# that reads as "the app uses no space".
assert api_storage._sum_rows([None, None]) == {"bytes": 0, "files": 0, "measured": False}

# --------------------------------------------------------------------------- #
# 2) The unreadable directory: reported, not raised
# --------------------------------------------------------------------------- #
reasons = {row["path"]: row["reason"] for row in snap["skipped"]}
assert os.path.normcase(BAD) in {os.path.normcase(p) for p in reasons}, snap["skipped"]
bad_reason = [r for p, r in reasons.items() if os.path.normcase(p) == os.path.normcase(BAD)][0]
assert "Permission denied" in bad_reason, bad_reason
assert snap["skipped_count"] == 1, snap["skipped_count"]
# ...and the rest of the library was still measured (the answer is partial, not
# empty): Album B is inside the same tree as the unreadable folder.
assert lib["audio_bytes"] == AUDIO

# --------------------------------------------------------------------------- #
# 2b) A LINK is not an unreadable folder: the walk steps over it on purpose and
#     the figures lose nothing — what a link points at is counted once, at the
#     real file. The bundled tools carry exactly this (the `libjpeg.so` version
#     symlinks), and the card must not warn about them.
# --------------------------------------------------------------------------- #
# The classification is what this suite owns, so it is proven by the WALK's own
# question: `_is_link` is what decides, and whether a real symlink/junction is
# detected is mlo.stats' answer, tested there. Two entries the walk counts
# nothing for anyway (an empty folder, an uncounted extension), so the only
# difference the links can make is in the skip list — which is the point.
LINK_DIR = os.path.join(ALBUM_A, "linked-tree")
LINK_FILE = os.path.join(ALBUM_A, "tool.bin")
os.makedirs(LINK_DIR, exist_ok=True)
make(LINK_FILE, 4096)
base = api_storage.storage_snapshot(CFG, MUSIC)

_REAL_LINK = api_storage._is_link
_FAKE_LINKS = {os.path.normcase(LINK_DIR), os.path.normcase(LINK_FILE)}
api_storage._is_link = lambda entry: os.path.normcase(entry.path) in _FAKE_LINKS
try:
    after = api_storage.storage_snapshot(CFG, MUSIC)
finally:
    api_storage._is_link = _REAL_LINK

assert after["skipped_links"] == 2, after["skipped"]
assert after["skipped_unreadable"] == 0, \
    [r for r in after["skipped"] if r["kind"] != "link"]
assert after["skipped_count"] == 2, after["skipped_count"]
assert {r["kind"] for r in after["skipped"]} == {"link"}, after["skipped"]
assert all("not followed" in r["reason"] for r in after["skipped"]), after["skipped"]
# Same bytes, same file count: nothing double counted through a link and
# nothing lost by not following one.
assert after["library"]["bytes"] == base["library"]["bytes"], \
    (after["library"]["bytes"], base["library"]["bytes"])
assert after["library"]["files"] == base["library"]["files"], after["library"]

# …and where the OS lets a test make a real one, that the walk SEES it is
# confirmed too (Windows needs a privilege for this, CI does not).
try:
    REAL_LINK = os.path.join(ALBUM_A, "01 - Song (link).flac")
    os.symlink(os.path.join(ALBUM_A, "01 - Song.flac"), REAL_LINK)
except (OSError, NotImplementedError, AttributeError) as e:
    print(f"note: no real symlink here ({e}) — the walk's own link test is mlo.stats' case")
else:
    real = api_storage.storage_snapshot(CFG, MUSIC)
    assert real["skipped_links"] == 1 and real["skipped_unreadable"] == 0, real["skipped"]
    assert real["library"]["bytes"] == base["library"]["bytes"], real["library"]

# --------------------------------------------------------------------------- #
# 3) The volume: used = total - free, and the percentage agrees
# --------------------------------------------------------------------------- #
assert snap["total_bytes"] == 1000 and snap["free_bytes"] == 400, snap
assert snap["used_bytes"] == snap["total_bytes"] - snap["free_bytes"]
assert snap["used_bytes"] == 600, snap
assert snap["percent_used"] == round(snap["used_bytes"] * 100 / snap["total_bytes"], 1)
assert snap["percent_used"] == 60.0, snap
assert snap["mount"] and snap["label"] == "Z:" and snap["type"] == "fixed", snap

# --------------------------------------------------------------------------- #
# 4) A volume the OS will not measure stays NULL, never 0 — and the folder
#    figures are still answered.
# --------------------------------------------------------------------------- #
api_storage.read_fs_usage = lambda path: fs(None, None)
try:
    with Unreadable(BAD):
        unknown = api_storage.storage_snapshot(CFG, MUSIC)
finally:
    api_storage.read_fs_usage = _REAL_FS

assert unknown["total_bytes"] is None and unknown["free_bytes"] is None, unknown
assert unknown["used_bytes"] is None and unknown["percent_used"] is None, unknown
assert unknown["total_bytes"] != 0 and unknown["free_bytes"] != 0, unknown
assert unknown["library"]["bytes"] == AUDIO + SIDECAR, unknown["library"]
assert unknown["downloads"]["bytes"] == DONE + STAGED, unknown["downloads"]

# --------------------------------------------------------------------------- #
# 5) read_fs_usage itself: real numbers for a real volume, nulls (not zeros)
#    when the OS refuses. The refusal is faked because a dropped network mount
#    cannot be staged: the drive list reports no size AND disk_usage raises,
#    which is exactly what the two readers do when the share is gone.
# --------------------------------------------------------------------------- #
real = _REAL_FS(ROOT)
assert isinstance(real["total_bytes"], int) and real["total_bytes"] > 0, real
assert isinstance(real["free_bytes"], int) and real["free_bytes"] >= 0, real
assert real["used_bytes"] == real["total_bytes"] - real["free_bytes"], real
assert real["percent_used"] is not None, real

from server import exporter as exporter_mod  # noqa: E402

_real_drives, _real_usage = exporter_mod.list_drives, shutil.disk_usage


def _raise(path):
    raise OSError(59, "unexpected network error")


exporter_mod.list_drives = lambda: [{"letter": "Z:", "root": "Z:\\", "type": "network",
                                     "free": None, "total": None}]
shutil.disk_usage = _raise
try:
    gone = _REAL_FS("Z:\\Music")
finally:
    exporter_mod.list_drives, shutil.disk_usage = _real_drives, _real_usage

assert gone["total_bytes"] is None and gone["free_bytes"] is None, gone
assert gone["used_bytes"] is None and gone["percent_used"] is None, gone
assert gone["type"] == "network" and gone["label"] == "Z:", gone

# --------------------------------------------------------------------------- #
# 6) The route is registered and answers the whole payload (this is what the
#    card polls).
# --------------------------------------------------------------------------- #
api_storage.load_config = lambda: dict(CFG)
api_storage.read_fs_usage = lambda path: fs(1000, 400)
try:
    from server import main as mlo_main

    client = TestClient(mlo_main.app)  # no lifespan: no workers, no slskd boot
    with Unreadable(BAD):
        r = client.get("/api/storage")
finally:
    api_storage.read_fs_usage = _REAL_FS

assert r.status_code == 200, (r.status_code, r.text)
body = r.json()
assert set(body) == {"mount", "label", "type", "total_bytes", "free_bytes",
                     "used_bytes", "percent_used", "library", "app_data",
                     "trash", "downloads", "dependencies", "app_total",
                     "skipped", "skipped_count", "skipped_links",
                     "skipped_unreadable",
                     "scanned_at", "took_ms"}, sorted(body)
assert body["library"]["bytes"] == AUDIO + SIDECAR, body["library"]
assert body["free_bytes"] == 400, body
assert isinstance(body["scanned_at"], int) and body["scanned_at"] > 0, body

# The app's OWN footprint is one figure, and it is the sum of the parts the
# same reply carries (the music is deliberately not in it — that is the point
# of the statistic).
def _n(row, key="bytes"):
    return int((row or {}).get(key) or 0)

assert body["app_total"]["bytes"] == (
    _n(body["app_data"]) + _n(body["trash"]) + _n(body["downloads"])
    + _n(body["dependencies"])), body["app_total"]
assert body["app_total"]["files"] == (
    _n(body["app_data"], "files") + _n(body["trash"], "files")
    + _n(body["downloads"], "files") + _n(body["dependencies"], "files")), body["app_total"]
assert body["app_total"]["measured"] is True, body["app_total"]

shutil.rmtree(ROOT, ignore_errors=True)

print("storage: all assertions passed")
