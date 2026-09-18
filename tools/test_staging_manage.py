#!/usr/bin/env python3
"""Verify the slskd staging manager: GET /api/soulseek/staging and the two
POST routes that empty it.

Covers exactly what the Soulseek page relies on:

  * both roots (downloads + incomplete) listed independently, newest first,
    with the /api/downloads entry shape minus `_mtime`, and a missing root
    reported as `exists: false` with zero totals instead of an error,
  * POST .../delete removing one entry (tree or file) with the bytes it freed,
    404 for a miss, 400 for an unknown root, and 400 — entry untouched — for
    every name that would resolve outside the root (traversal, absolute path,
    backslash, symlink out),
  * POST .../clear taking the whole root, one locked entry landing in `failed`
    while the rest still clear, the root directory surviving, and the OTHER
    root untouched.

The staging roots are read from `soulseek_download_dir` (and its sibling
`incomplete`), NOT from the music folder: this suite points them at a temp
directory and plants a decoy in the music-folder-derived default so a route
that reads the wrong one cannot pass.

Offline and hermetic: no slskd, no network, nothing written outside temp.
Run:  python tools/test_staging_manage.py   (exit 0 pass, 1 fail)
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
# hermeticity: app paths resolve through the music folder the moment they are
# first touched, so the scope is redirected to a temp folder BEFORE
# server.main is imported. Nothing is written into the developer's real music
# folder.
# --------------------------------------------------------------------------- #
REAL_MUSIC_FOLDER = ""
try:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        REAL_MUSIC_FOLDER = str((json.load(f) or {}).get("music_folder") or "")
except Exception:
    pass

REDIRECT = tempfile.mkdtemp(prefix="mlo-staging-redirect-")
os.environ["MLO_MUSIC_FOLDER"] = REDIRECT

import mlo.config as cfgmod  # noqa: E402
import mlo.paths as pathmod  # noqa: E402

_STUB = os.path.join(REDIRECT, "config.json")
with open(_STUB, "w", encoding="utf-8") as f:
    json.dump({"music_folder": REDIRECT}, f)

for _mod in (cfgmod, pathmod):
    _mod.CONFIG_FILE = _STUB
    if getattr(_mod, "LEGACY_DATA_DIR", None) is not None:
        _mod.LEGACY_DATA_DIR = os.path.join(REDIRECT, "legacy")

# --------------------------------------------------------------------------- #
# fixtures: a temp staging pair + the music folder
# --------------------------------------------------------------------------- #
MUSIC = os.path.join(REDIRECT, "music")
STAGE = tempfile.mkdtemp(prefix="mlo-staging-test-")
DOWNLOADS = os.path.join(STAGE, "downloads")
INCOMPLETE = os.path.join(STAGE, "incomplete")
# where slskd would land if the routes derived the path from the music folder
DECOY = os.path.join(MUSIC, ".mlo", "downloads")

_REAL = REAL_MUSIC_FOLDER.replace("\\", "/").rstrip("/")
if _REAL:
    for _t in (MUSIC, STAGE):
        assert not _t.replace("\\", "/").lower().startswith(_REAL.lower()), \
            f"temp fixture {_t} sits inside the real music folder {_REAL}"


def _cleanup():
    os.environ.pop("MLO_MUSIC_FOLDER", None)
    for d in (REDIRECT, STAGE):
        shutil.rmtree(d, ignore_errors=True)


atexit.register(_cleanup)

from fastapi.testclient import TestClient  # noqa: E402

from server import main as mlo_main  # noqa: E402  (heavy import)

mlo_main.load_config = lambda: {"music_folder": MUSIC,
                                "soulseek_download_dir": DOWNLOADS}

_client = TestClient(mlo_main.app)   # no lifespan: no slskd boot


def write(path, size=8):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * size)
    return path


def get_staging():
    r = _client.get("/api/soulseek/staging")
    assert r.status_code == 200, r.text
    return r.json()


def delete(root, name):
    return _client.post("/api/soulseek/staging/delete",
                        json={"root": root, "name": name})


def clear(root):
    return _client.post("/api/soulseek/staging/clear", json={"root": root})


ALBUM = "Some Album (2010)"
IN_FLIGHT = ".in-flight"

# slskd's incomplete dir is the configured download dir's sibling, never a
# `_downloads_dir(music_folder)` path — that default is what the decoy below
# occupies.
assert mlo_main._staging_root(mlo_main.load_config(), "incomplete") == INCOMPLETE
assert mlo_main._staging_root(mlo_main.load_config(), "downloads") == DOWNLOADS

write(os.path.join(DOWNLOADS, ALBUM, "01 - a.flac"), 1000)
write(os.path.join(DOWNLOADS, ALBUM, "02 - b.flac"), 2000)
write(os.path.join(DOWNLOADS, "loose.flac"), 4000)
write(os.path.join(DOWNLOADS, IN_FLIGHT, "half.flac"), 700)
write(os.path.join(INCOMPLETE, "Other (2020)", "a.mp3"), 1000)
write(os.path.join(INCOMPLETE, "half.tmp"), 10)
# a file the music-folder-derived default holds: it must never show up in the
# staging listing
write(os.path.join(DECOY, "slskd-never-writes-here.flac"), 999999)
# deterministic newest-first order (dir mtimes are set AFTER their contents)
for _p, _t in ((os.path.join(DOWNLOADS, "loose.flac"), 1000),
               (os.path.join(DOWNLOADS, ALBUM), 2000),
               (os.path.join(DOWNLOADS, IN_FLIGHT), 3000),
               (os.path.join(INCOMPLETE, "half.tmp"), 1000),
               (os.path.join(INCOMPLETE, "Other (2020)"), 2000)):
    os.utime(_p, (_t, _t))


def _open_file_undeletable():
    """True when this OS refuses to unlink a file another handle still holds
    open (Windows), i.e. a stuck entry can be staged deterministically."""
    probe = os.path.join(STAGE, "probe-open")
    with open(probe, "wb") as f:
        f.write(b"x")
        try:
            os.remove(probe)
        except OSError:
            return True
    return False


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #
FAILED = []


def check(label, fn):
    """Run one labeled check; a failed assertion is reported, not fatal."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - report and keep checking
        FAILED.append(label)
        print(f"FAIL {label}: {type(e).__name__}: {e}")
    else:
        print(f"ok   {label}")


# ---- listing --------------------------------------------------------------- #
def test_listing_shape():
    res = get_staging()
    for key, root in (("downloads", DOWNLOADS), ("incomplete", INCOMPLETE)):
        d = res[key]
        assert d["exists"] is True, d
        assert d["folder"] == os.path.abspath(root).replace("\\", "/"), d["folder"]
        assert d["count"] == len(d["entries"]), d
        assert d["bytes"] == sum(e["bytes"] for e in d["entries"]), d
        # `_mtime` is the server's private ordering key, never the page's
        assert all("_mtime" not in e for e in d["entries"]), d["entries"]

    dl = res["downloads"]
    assert [e["name"] for e in dl["entries"]] == [IN_FLIGHT, ALBUM, "loose.flac"], \
        dl["entries"]
    by = {e["name"]: e for e in dl["entries"]}
    assert dl["count"] == 3 and dl["bytes"] == 7700, dl
    assert "slskd-never-writes-here.flac" not in by, \
        "the music-folder-derived default was listed instead of soulseek_download_dir"

    album = by[ALBUM]
    assert album["dir"] is True and album["album"] is True, album
    assert album["files"] == 2 and album["audio"] == 2, album
    assert album["images"] == 0 and album["bytes"] == 3000, album
    assert album["partial"] is False, album

    loose = by["loose.flac"]
    assert loose["dir"] is False and loose["files"] == 1, loose
    assert loose["audio"] == 1 and loose["bytes"] == 4000, loose
    # a loose audio file is not an album: there is nothing to import
    assert loose["album"] is False and loose["partial"] is False, loose

    # dot-prefixed name = slskd's own in-flight leftover, never a result
    assert by[IN_FLIGHT]["partial"] is True, by[IN_FLIGHT]
    assert by[IN_FLIGHT]["album"] is True, by[IN_FLIGHT]

    inc = res["incomplete"]
    assert [e["name"] for e in inc["entries"]] == ["Other (2020)", "half.tmp"], \
        inc["entries"]
    assert inc["count"] == 2 and inc["bytes"] == 1010, inc
    assert {e["name"] for e in inc["entries"]}.isdisjoint(by), \
        "the two roots were not listed independently"
    # `.tmp` suffix is in-flight too, same as a dot prefix
    assert inc["entries"][1]["partial"] is True, inc["entries"][1]


# ---- delete ---------------------------------------------------------------- #
def test_delete_entries():
    r = delete("downloads", ALBUM)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "freed": 3000}, r.text
    assert not os.path.exists(os.path.join(DOWNLOADS, ALBUM))

    r = delete("incomplete", "Other (2020)")
    assert r.status_code == 200, r.text
    assert r.json()["freed"] == 1000, r.text
    assert not os.path.exists(os.path.join(INCOMPLETE, "Other (2020)"))

    # a lone file frees its own bytes; the root name is trimmed and lowercased
    r = delete(" Downloads ", "loose.flac")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "freed": 4000}, r.text
    assert not os.path.exists(os.path.join(DOWNLOADS, "loose.flac"))

    # a miss is a 404, not a 500
    assert delete("downloads", "nope.flac").status_code == 404
    # an unknown root is refused before any path is resolved
    for bad_root in ("elsewhere", "", "downloads/../incomplete", "."):
        r = delete(bad_root, "x")
        assert r.status_code == 400, (bad_root, r.status_code, r.text)
        assert r.json()["detail"] == "unknown staging root", r.text


def test_delete_name_guard():
    """Names travel as basenames: anything that could resolve outside the root
    is refused, and nothing on disk is touched."""
    outside = write(os.path.join(STAGE, "escape.flac"), 10)
    before = sorted(os.listdir(DOWNLOADS))
    for bad in ("../escape", "..\\escape", "./escape", "sub/dir",
                os.path.join(STAGE, "escape.flac"), "", ".", ".."):
        r = delete("downloads", bad)
        assert r.status_code == 400, (bad, r.status_code, r.text)
        assert r.json()["detail"], r.text
        assert os.path.isfile(outside) and os.path.getsize(outside) == 10, \
            f"escape target touched by {bad!r}"
        assert sorted(os.listdir(DOWNLOADS)) == before, (bad, os.listdir(DOWNLOADS))
    assert delete("downloads", "../escape").json()["detail"] == \
        "not a valid entry name"


def test_delete_symlink_escape():
    target = tempfile.mkdtemp(prefix="mlo-staging-outside-")
    atexit.register(lambda: shutil.rmtree(target, ignore_errors=True))
    write(os.path.join(target, "keep.flac"), 21)
    link = os.path.join(DOWNLOADS, "link-out")
    try:
        os.symlink(target, link)
    except OSError as e:
        print(f"     SKIP symlink case: cannot create one here ({e.__class__.__name__})")
        return
    r = delete("downloads", "link-out")
    assert r.status_code == 400, (r.status_code, r.text)
    assert r.json()["detail"] == "outside the downloads folder", r.text
    assert os.path.isfile(os.path.join(target, "keep.flac")), "the link target was deleted"
    assert os.path.lexists(link), "the link itself was removed"


# ---- clear ----------------------------------------------------------------- #
def test_clear():
    shutil.rmtree(DOWNLOADS, ignore_errors=True)
    write(os.path.join(DOWNLOADS, "a.flac"), 100)
    write(os.path.join(DOWNLOADS, "b (2011)", "x.flac"), 250)
    write(os.path.join(INCOMPLETE, "keeper.flac"), 77)

    r = clear("downloads")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"ok": True, "cleared": 2, "freed": 350, "failed": []}, body
    assert os.path.isdir(DOWNLOADS), "the root itself was removed"
    assert os.listdir(DOWNLOADS) == [], os.listdir(DOWNLOADS)
    assert os.path.isfile(os.path.join(INCOMPLETE, "keeper.flac")), \
        "clear touched the other root"

    # an empty root is 0 cleared, not an error
    empty = clear("downloads")
    assert empty.status_code == 200, empty.text
    assert empty.json() == {"ok": True, "cleared": 0, "freed": 0, "failed": []}, \
        empty.json()


def test_clear_reports_undeletable():
    """One entry that will not delete is reported and the rest still go."""
    if not _open_file_undeletable():
        print("     SKIP locked-entry case: this OS unlinks an open file (POSIX)")
        return
    shutil.rmtree(DOWNLOADS, ignore_errors=True)
    write(os.path.join(DOWNLOADS, "one.flac"), 11)
    write(os.path.join(DOWNLOADS, "two.flac"), 22)
    locked = write(os.path.join(DOWNLOADS, "held.flac"), 44)

    with open(locked, "rb"):
        r = clear("downloads")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cleared"] == 2 and body["freed"] == 33, body
    assert [f["name"] for f in body["failed"]] == ["held.flac"], body
    assert body["failed"][0]["reason"], body
    assert os.path.isfile(locked), "a reported failure is still on disk"
    assert not os.path.exists(os.path.join(DOWNLOADS, "one.flac")), \
        "clear aborted on the first failure"
    assert not os.path.exists(os.path.join(DOWNLOADS, "two.flac")), \
        "clear aborted on the first failure"
    assert os.path.isdir(DOWNLOADS), "the root itself was removed"


# ---- missing roots --------------------------------------------------------- #
def test_missing_roots():
    shutil.rmtree(INCOMPLETE, ignore_errors=True)
    res = get_staging()
    inc = res["incomplete"]
    assert inc["exists"] is False, inc
    assert inc["count"] == 0 and inc["bytes"] == 0 and inc["entries"] == [], inc
    assert inc["folder"].replace("\\", "/").endswith("/incomplete"), inc["folder"]
    assert res["downloads"]["exists"] is True, res["downloads"]

    assert delete("incomplete", "x").status_code == 404
    assert clear("incomplete").status_code == 404


# ---- neighbours ------------------------------------------------------------ #
def test_downloads_route_unchanged():
    """One guard: the pre-existing downloads route still answers its shape."""
    r = _client.get("/api/downloads")
    assert r.status_code == 200, r.text
    d = r.json()
    assert {"folder", "exists", "count", "bytes", "entries"} <= set(d), sorted(d)
    assert d["folder"] == DECOY.replace("\\", "/"), d["folder"]
    assert d["exists"] is True and d["count"] == 1, d
    assert [e["name"] for e in d["entries"]] == ["slskd-never-writes-here.flac"], \
        d["entries"]


check("staging listing: shape, totals, flags, newest first", test_listing_shape)
check("staging delete: frees bytes, 404 miss, 400 unknown root", test_delete_entries)
check("staging delete: name guard leaves the disk untouched", test_delete_name_guard)
check("staging delete: symlink out of the root is refused", test_delete_symlink_escape)
check("staging clear: empties one root, keeps the other + the root", test_clear)
check("staging clear: undeletable entry fails alone", test_clear_reports_undeletable)
check("staging: a missing root is empty, not an error", test_missing_roots)
check("regression: GET /api/downloads still answers its shape",
      test_downloads_route_unchanged)

print()
if FAILED:
    print(f"{len(FAILED)} check(s) FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all checks passed")
