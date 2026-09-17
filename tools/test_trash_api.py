#!/usr/bin/env python3
"""Verify the trash bin API (GET /api/trash, POST /api/trash/delete,
POST /api/trash/restore, POST /api/album/remove).

Everything runs against a throwaway music folder under the system temp dir;
the real configured music_folder is never listed, deleted or even opened.
Run: python tools/test_trash_api.py  (exit 0 pass, 1 fail)
"""
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --------------------------------------------------------------------------- #
# hermeticity: the app's playlists/wishes databases resolve their path
# through app_data_dir() — i.e. the live install — the moment they are first
# touched. The scope is redirected to a temp folder BEFORE server.main is
# imported, so no state file is ever created (or migrated) in the developer's
# real music folder or its .mlo/data.
# --------------------------------------------------------------------------- #
REAL_MUSIC_FOLDER = ""
try:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        REAL_MUSIC_FOLDER = str((json.load(f) or {}).get("music_folder") or "")
except Exception:
    pass

REDIRECT = tempfile.mkdtemp(prefix="mlo-trash-redirect-")
os.environ["MLO_MUSIC_FOLDER"] = REDIRECT

import mlo  # noqa: E402
import mlo.config as cfgmod  # noqa: E402
import mlo.paths as pathmod  # noqa: E402

_STUB = os.path.join(REDIRECT, "config.json")
with open(_STUB, "w", encoding="utf-8") as f:
    json.dump({"music_folder": REDIRECT}, f)

for _mod in (cfgmod, pathmod):
    _mod.CONFIG_FILE = _STUB
    if getattr(_mod, "LEGACY_DATA_DIR", None) is not None:
        _mod.LEGACY_DATA_DIR = os.path.join(REDIRECT, "legacy")


def _cleanup_redirect():
    os.environ.pop("MLO_MUSIC_FOLDER", None)
    shutil.rmtree(REDIRECT, ignore_errors=True)


atexit.register(_cleanup_redirect)

from fastapi import HTTPException  # noqa: E402
from server import main as mlo_main  # noqa: E402  (heavy import, only for this)

# --------------------------------------------------------------------------- #
# fixture: a temp music folder with a .mlo/trash inside it
# --------------------------------------------------------------------------- #
MF = tempfile.mkdtemp(prefix="mlo-trash-test-")
TRASH = os.path.join(MF, ".mlo", "trash")
ALBUM = "[Album] 2010-12-15 - 2010-12-15 - Aimai Elegy {JP - CD - XECJ-1011}"
LOOSE = "Loose (2)"          # dedupe suffix, no [Album] convention
STRAY = "stray.flac"         # a bare file dumped in the bin

ALBUM_BYTES = 1000 + 2000 + 3000 + 500 + 100   # 3 flac (one nested) + cover + notes
LOOSE_BYTES = 4000


def write(rel, size):
    p = os.path.join(TRASH, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"x" * size)
    return p


os.makedirs(TRASH)
album_path = write(os.path.join(ALBUM, "01 - a.flac"), 1000)
write(os.path.join(ALBUM, "02 - b.flac"), 2000)
write(os.path.join(ALBUM, "disc 2", "01 - c.flac"), 3000)
write(os.path.join(ALBUM, "cover.jpg"), 500)
write(os.path.join(ALBUM, "notes.txt"), 100)
loose_path = write(os.path.join(LOOSE, "only.flac"), LOOSE_BYTES)
write(os.path.join(LOOSE, "rip.log"), 0)
stray_path = write(STRAY, 700)
outside_path = os.path.join(MF, "outside.txt")          # escape target
with open(outside_path, "wb") as f:
    f.write(b"do not delete me")

# deterministic ordering: stray newest, then Loose, then the album.
# Stamp the bin entries themselves — a trash row's timestamp is its own mtime.
now = time.time()
os.utime(os.path.join(TRASH, ALBUM), (now - 300, now - 300))
os.utime(os.path.join(TRASH, LOOSE), (now - 200, now - 200))
os.utime(stray_path, (now - 100, now - 100))

mlo_main.load_config = lambda: {"music_folder": MF}

# safety: this suite must never point at the user's real library
REAL = REAL_MUSIC_FOLDER.replace("\\", "/").rstrip("/")
if REAL:
    assert not MF.replace("\\", "/").lower().startswith(REAL.lower()), \
        f"temp fixture {MF} sits inside the real music folder {REAL}"

# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #
FAILED = []
COVER = {}


def check(label, fn):
    """Run one labeled check; a failed assertion is reported, not fatal."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - report and keep checking
        FAILED.append(label)
        print(f"FAIL {label}: {type(e).__name__}: {e}")
    else:
        print(f"ok   {label}")


def listing():
    res = mlo_main.trash_list()
    COVER["listing"] = res
    return res


def test_listing_shape():
    res = listing()
    assert res["exists"] is True, res["exists"]
    assert res["folder"] == TRASH.replace("\\", "/"), res["folder"]
    assert res["music_folder"] == MF.replace("\\", "/"), res["music_folder"]
    assert res["count"] == len(res["entries"]) == 3, res
    assert res["bytes"] == ALBUM_BYTES + LOOSE_BYTES + 700, res["bytes"]
    assert [e["name"] for e in res["entries"]] == [STRAY, LOOSE, ALBUM], res["entries"]
    times = [e["trashed_at"] for e in res["entries"]]
    assert times == sorted(times, reverse=True), times
    for e in res["entries"]:
        assert set(e) == {"name", "path", "kind", "label", "tracks", "bytes",
                          "trashed_at", "cover", "origin", "file_count",
                          "files"}, sorted(e)
        # entries trashed before the manifest existed simply have no origin
        assert e["origin"] is None, e
        assert e["path"] == os.path.join(TRASH, e["name"]).replace("\\", "/"), e
        assert time.strptime(e["trashed_at"], "%Y-%m-%dT%H:%M:%S"), e["trashed_at"]
    # newest entry (the stray file) really is the most recently modified one
    assert COVER["listing"]["entries"][0]["name"] == STRAY
    assert time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 100)) == times[0], times[0]


def test_track_and_byte_counts():
    res = listing()
    album = [e for e in res["entries"] if e["name"] == ALBUM][0]
    assert album["kind"] == "album", album["kind"]
    assert album["tracks"] == 3, album["tracks"]          # recursive: disc 2 counted
    assert album["bytes"] == ALBUM_BYTES, album["bytes"]  # recursive, cover + notes too
    assert album["cover"] is True, album
    loose = [e for e in res["entries"] if e["name"] == LOOSE][0]
    assert loose["tracks"] == 1 and loose["bytes"] == LOOSE_BYTES, loose
    assert loose["cover"] is False, loose
    stray = [e for e in res["entries"] if e["name"] == STRAY][0]
    assert stray["kind"] == "file", stray
    assert stray["tracks"] == 1 and stray["bytes"] == 700, stray
    assert stray["cover"] is False, stray


def test_label_parsed_from_convention_name():
    res = listing()
    album = [e for e in res["entries"] if e["name"] == ALBUM][0]
    assert album["label"] == "Aimai Elegy", album["label"]


def test_label_falls_back_to_raw_name():
    res = listing()
    loose = [e for e in res["entries"] if e["name"] == LOOSE][0]
    assert loose["label"] == LOOSE, loose["label"]
    stray = [e for e in res["entries"] if e["name"] == STRAY][0]
    assert stray["label"] == STRAY, stray["label"]


def test_files_recursive_rel_and_sorted():
    res = listing()
    album = [e for e in res["entries"] if e["name"] == ALBUM][0]
    assert [f["rel"] for f in album["files"]] == [
        "01 - a.flac", "02 - b.flac", "cover.jpg",
        "disc 2/01 - c.flac", "notes.txt"], album["files"]
    assert album["file_count"] == len(album["files"]) == 5, album["file_count"]
    assert album["files"][0] == {"name": "01 - a.flac", "rel": "01 - a.flac",
                                 "bytes": 1000}, album["files"][0]
    nested = [f for f in album["files"] if f["rel"] == "disc 2/01 - c.flac"][0]
    assert nested["name"] == "01 - c.flac", nested
    assert nested["bytes"] == 3000, nested
    # every rel is forward-slashed, relative and carries no leading slash
    for f in album["files"]:
        assert not f["rel"].startswith("/") and "\\" not in f["rel"], f
        assert set(f) == {"name", "rel", "bytes"}, sorted(f)

    loose = [e for e in res["entries"] if e["name"] == LOOSE][0]
    assert [f["rel"] for f in loose["files"]] == ["only.flac", "rip.log"], loose["files"]
    assert [f["bytes"] for f in loose["files"]] == [LOOSE_BYTES, 0], loose["files"]
    assert loose["file_count"] == 2, loose

    stray = [e for e in res["entries"] if e["name"] == STRAY][0]
    assert stray["files"] == [{"name": STRAY, "rel": STRAY, "bytes": 700}], stray["files"]
    assert stray["file_count"] == 1, stray


def test_files_capped_but_file_count_true():
    entry = "big"
    for i in range(205):
        write(os.path.join(entry, f"{i:03d}.flac"), 10)
    try:
        row = [e for e in listing()["entries"] if e["name"] == entry][0]
        assert mlo_main._TRASH_FILES_CAP == 200, mlo_main._TRASH_FILES_CAP
        assert row["file_count"] == 205, row["file_count"]
        assert len(row["files"]) == 200, len(row["files"])
        rels = [f["rel"] for f in row["files"]]
        assert rels == sorted(rels), rels[:3]
        # the cap keeps the head of the sorted list, so the UI can page on
        assert rels[0] == "000.flac" and rels[-1] == "199.flac", (rels[0], rels[-1])
        assert row["tracks"] == 205 and row["bytes"] == 2050, row
    finally:
        shutil.rmtree(os.path.join(TRASH, entry), ignore_errors=True)


def test_linked_subdir_not_walked():
    """A directory link inside an entry is skipped by the file walk too: its
    files are neither listed nor counted, so a listing never escapes the bin."""
    entry = "linkholder"
    write(os.path.join(entry, "01 - real.flac"), 50)
    link = os.path.join(TRASH, entry, "linked")
    if not make_dir_link(link, MF):
        shutil.rmtree(os.path.join(TRASH, entry), ignore_errors=True)
        print("skip nested-link file check (no symlink/junction privilege)")
        return
    try:
        row = [e for e in listing()["entries"] if e["name"] == entry][0]
        assert [f["rel"] for f in row["files"]] == ["01 - real.flac"], row["files"]
        assert row["file_count"] == 1, row["file_count"]
        assert row["tracks"] == 1 and row["bytes"] == 50, row
    finally:
        drop_link(link)
        shutil.rmtree(os.path.join(TRASH, entry), ignore_errors=True)


LINK = os.path.join(TRASH, "escape-link")


def make_dir_link(link, target):
    """Directory link at `link` -> `target`: a symlink when the OS grants the
    privilege, else the junction — the privilege-free form Windows offers.
    Returns False when neither can be created here."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                           capture_output=True, text=True)
        return r.returncode == 0 and os.path.exists(link)
    return False


def drop_link(link):
    try:
        os.remove(link)
    except OSError:
        os.rmdir(link)


def make_escape_link():
    return make_dir_link(LINK, MF)


def drop_escape_link():
    drop_link(LINK)


def test_symlink_out_of_trash_hidden_from_listing():
    if not make_escape_link():
        print("skip escape-link listing check (no symlink/junction privilege)")
        return
    try:
        names = [e["name"] for e in listing()["entries"]]
        assert "escape-link" not in names, names
        assert listing()["count"] == 3, listing()["count"]
    finally:
        drop_escape_link()


def test_symlink_escape_refused():
    if not make_escape_link():
        print("skip escape-link delete check (no symlink/junction privilege)")
        return
    try:
        res = mlo_main.trash_delete(mlo_main.TrashDelete(names=["escape-link"]))
        assert res["deleted"] == [], res
        assert len(res["failed"]) == 1, res
        assert "outside the trash folder" in res["failed"][0]["error"], res
        assert os.path.exists(outside_path), "escaped deletion removed outside.txt"
        assert os.path.isdir(MF), "escaped deletion removed the music folder"
    finally:
        drop_escape_link()


def test_delete_stray_file():
    res = mlo_main.trash_delete(mlo_main.TrashDelete(names=[STRAY]))
    assert res["deleted"] == [STRAY], res
    assert res["failed"] == [], res
    assert res["freed"] == 700, res["freed"]
    assert not os.path.exists(stray_path), "file still on disk"
    assert listing()["count"] == 2


def test_delete_deduped_entry():
    assert LOOSE in [e["name"] for e in listing()["entries"]]
    res = mlo_main.trash_delete(mlo_main.TrashDelete(names=[LOOSE]))
    assert res["deleted"] == [LOOSE], res
    assert res["freed"] == LOOSE_BYTES, res["freed"]
    assert not os.path.exists(loose_path), "deduped dir still on disk"
    assert listing()["count"] == 1


def test_delete_invalidates_caches():
    calls = []
    mlo_main.tagcache.invalidate_all = lambda: calls.append("tags")
    mlo_main.mbresolve.invalidate = lambda: calls.append("mb")
    mlo_main._refresh_slskd_shares_soon = lambda: calls.append("shares")
    try:
        # a refused name must not invalidate anything
        res = mlo_main.trash_delete(mlo_main.TrashDelete(names=["..", "nope"]))
        assert res["deleted"] == [], res
        assert calls == [], calls
        res = mlo_main.trash_delete(mlo_main.TrashDelete(names=[ALBUM]))
        assert res["deleted"] == [ALBUM], res
        assert res["freed"] == ALBUM_BYTES, res["freed"]
        assert calls == ["tags", "mb", "shares"], calls
    finally:
        from server import mbresolve, tagcache
        mlo_main.tagcache.invalidate_all = tagcache.invalidate_all
        mlo_main.mbresolve.invalidate = mbresolve.invalidate
        mlo_main._refresh_slskd_shares_soon = _real_refresh


def test_album_gone_but_trash_survives():
    assert not os.path.exists(album_path), "album dir still on disk"
    assert os.path.isdir(TRASH), "the trash dir itself was deleted"
    assert os.path.isdir(MF), "the music folder was deleted"
    res = listing()
    assert res["exists"] is True and res["count"] == 0 and res["bytes"] == 0, res
    assert res["entries"] == [], res


def test_missing_entry_fails():
    res = mlo_main.trash_delete(mlo_main.TrashDelete(names=["ghost"]))
    assert res["deleted"] == [], res
    assert res["freed"] == 0, res
    assert len(res["failed"]) == 1 and res["failed"][0]["name"] == "ghost", res
    assert res["failed"][0]["error"], res


def test_empty_request_is_not_an_error():
    res = mlo_main.trash_delete(mlo_main.TrashDelete())
    assert res == {"deleted": [], "failed": [], "freed": 0}, res
    res = mlo_main.trash_delete(mlo_main.TrashDelete(names=[]))
    assert res == {"deleted": [], "failed": [], "freed": 0}, res
    res = mlo_main.trash_delete()          # no body at all
    assert res == {"deleted": [], "failed": [], "freed": 0}, res


def test_escape_attempts_refused():
    # a real entry sits in the bin while the escapes are attempted, so
    # "nothing was removed" is a claim about something that could be removed
    keeper = write(os.path.join("keeper", "01 - keep.flac"), 900)
    attempts = ["../outside.txt", "..\\outside.txt", "/abs", "a/b",
                ".", "..", "", "..\\..\\outside.txt", ALBUM + "/../outside.txt"]
    res = mlo_main.trash_delete(mlo_main.TrashDelete(names=attempts))
    assert res["deleted"] == [], res
    assert res["freed"] == 0, res
    assert [f["name"] for f in res["failed"]] == attempts, res
    for f in res["failed"]:
        assert f["error"], f
    assert os.path.exists(outside_path), "escape attempt deleted outside.txt"
    assert os.path.exists(keeper), "escape attempt deleted a real entry"
    assert os.path.isdir(TRASH), "escape attempt deleted the trash dir"
    assert os.path.isdir(MF), "escape attempt deleted the music folder"
    assert listing()["count"] == 1, listing()


def test_missing_bin():
    empty = tempfile.mkdtemp(prefix="mlo-trash-empty-")
    try:
        mlo_main.load_config = lambda: {"music_folder": empty}
        res = mlo_main.trash_list()
        assert res["exists"] is False, res
        assert res["entries"] == [] and res["count"] == 0 and res["bytes"] == 0, res
        assert res["folder"] == os.path.join(empty, ".mlo", "trash").replace("\\", "/"), res
        try:
            mlo_main.trash_delete(mlo_main.TrashDelete(names=["x"]))
        except HTTPException as e:
            assert e.status_code in (400, 404), e.status_code
        else:
            raise AssertionError("deleting from a missing bin should not report success")
        try:
            mlo_main.trash_restore(mlo_main.TrashRestore(names=["x"]))
        except HTTPException as e:
            assert e.status_code in (400, 404), e.status_code
        else:
            raise AssertionError("restoring from a missing bin should not report success")
        # unset music_folder must be just as quiet on the read path
        mlo_main.load_config = lambda: {"music_folder": ""}
        res = mlo_main.trash_list()
        assert res["exists"] is False and res["entries"] == [], res
        assert res["music_folder"] == "", res
    finally:
        mlo_main.load_config = lambda: {"music_folder": MF}
        shutil.rmtree(empty, ignore_errors=True)


# --------------------------------------------------------------------------- #
# origin manifest + restore (POST /api/album/remove round trip)
# --------------------------------------------------------------------------- #
ORIG_ALBUM = "[Album] 2001-08-27 - 2001-09-04 - Toxicity {US - CD - CK 62240}"
ORIG_DIR = os.path.join(MF, "Artists", "Toxicity Artist", ORIG_ALBUM)
ORIG_DIR_API = ORIG_DIR.replace("\\", "/")


def with_cache_spies(fn):
    """Run fn(calls) with the three cache hooks replaced by recorders: the
    real slskd hook would spawn a daemon thread against the temp fixture."""
    calls = []
    mlo_main.tagcache.invalidate_all = lambda: calls.append("tags")
    mlo_main.mbresolve.invalidate = lambda: calls.append("mb")
    mlo_main._refresh_slskd_shares_soon = lambda: calls.append("shares")
    try:
        fn(calls)
    finally:
        from server import mbresolve, tagcache
        mlo_main.tagcache.invalidate_all = tagcache.invalidate_all
        mlo_main.mbresolve.invalidate = mbresolve.invalidate
        mlo_main._refresh_slskd_shares_soon = _real_refresh


def make_album(album_dir, size=1234):
    """A one-track album in the library; returns the track path."""
    os.makedirs(album_dir, exist_ok=True)
    p = os.path.join(album_dir, "01 - x.flac")
    with open(p, "wb") as f:
        f.write(b"x" * size)
    return p


def manifest():
    """The raw manifest on disk — the API deliberately never exposes it."""
    with open(mlo_main._manifest_path(TRASH), "r", encoding="utf-8") as f:
        return json.load(f)


def trash_names():
    return [e["name"] for e in listing()["entries"]]


def test_move_records_origin():
    make_album(ORIG_DIR)
    with_cache_spies(lambda _: mlo_main.album_remove(mlo_main.AlbumRemove(path=ORIG_DIR)))
    assert not os.path.exists(ORIG_DIR), "album still in the library"
    assert os.path.isfile(os.path.join(TRASH, ORIG_ALBUM, "01 - x.flac")), "album not in the bin"
    res = listing()
    row = [e for e in res["entries"] if e["name"] == ORIG_ALBUM]
    assert len(row) == 1, trash_names()
    assert row[0]["origin"] == ORIG_DIR_API, row[0]
    rec = manifest()["entries"][ORIG_ALBUM]
    assert rec["origin"] == ORIG_DIR_API, rec
    assert time.strptime(rec["at"], "%Y-%m-%dT%H:%M:%S"), rec["at"]
    # the artist folder goes away with it: restoring has to rebuild it
    shutil.rmtree(os.path.join(MF, "Artists"))


def test_manifest_never_listed_or_touched():
    res = listing()
    assert mlo_main._TRASH_MANIFEST not in trash_names(), trash_names()
    assert res["count"] == len(res["entries"]), res
    assert res["bytes"] == sum(e["bytes"] for e in res["entries"]), res
    d = mlo_main.trash_delete(mlo_main.TrashDelete(names=[mlo_main._TRASH_MANIFEST]))
    assert d["deleted"] == [] and len(d["failed"]) == 1, d
    assert d["failed"][0]["error"], d
    r = mlo_main.trash_restore(mlo_main.TrashRestore(names=[mlo_main._TRASH_MANIFEST]))
    assert r["restored"] == [] and len(r["failed"]) == 1, r
    r = mlo_main.trash_restore(mlo_main.TrashRestore(names=[mlo_main._TRASH_MANIFEST],
                                                     dest=MF))
    assert r["restored"] == [], r
    assert os.path.isfile(mlo_main._manifest_path(TRASH)), "manifest was removed"
    assert manifest()["entries"], "manifest was emptied"


def test_dedup_move_records_right_origin():
    make_album(ORIG_DIR, size=99)
    with_cache_spies(lambda _: mlo_main.album_remove(mlo_main.AlbumRemove(path=ORIG_DIR)))
    dedup = ORIG_ALBUM + " (2)"
    assert os.path.isfile(os.path.join(TRASH, dedup, "01 - x.flac")), trash_names()
    row = [e for e in listing()["entries"] if e["name"] == dedup]
    assert len(row) == 1, trash_names()
    assert row[0]["origin"] == ORIG_DIR_API, row[0]
    entries = manifest()["entries"]
    assert entries[dedup]["origin"] == ORIG_DIR_API, entries
    assert entries[ORIG_ALBUM]["origin"] == ORIG_DIR_API, entries


def test_restore_returns_to_original_path():
    def body(calls):
        res = mlo_main.trash_restore(mlo_main.TrashRestore(names=[ORIG_ALBUM]))
        assert res == {"restored": [{"name": ORIG_ALBUM, "to": ORIG_DIR_API}],
                       "failed": []}, res
        assert calls == ["tags", "mb", "shares"], calls
    with_cache_spies(body)
    assert os.path.isfile(os.path.join(ORIG_DIR, "01 - x.flac")), "album not back on disk"
    assert os.path.isdir(os.path.dirname(ORIG_DIR)), "artist folder not recreated"
    assert not os.path.exists(os.path.join(TRASH, ORIG_ALBUM)), "bin copy still there"
    assert ORIG_ALBUM not in trash_names(), trash_names()
    assert ORIG_ALBUM not in manifest()["entries"], manifest()


def test_occupied_destination_refused():
    dedup = ORIG_ALBUM + " (2)"
    occupant = os.path.join(ORIG_DIR, "01 - x.flac")
    before = open(occupant, "rb").read()
    res = mlo_main.trash_restore(mlo_main.TrashRestore(names=[dedup]))
    assert res["restored"] == [], res
    assert len(res["failed"]) == 1, res
    assert res["failed"][0]["name"] == dedup, res
    assert "already exists" in res["failed"][0]["error"], res
    assert ORIG_DIR_API in res["failed"][0]["error"], res["failed"][0]
    assert os.path.isfile(os.path.join(TRASH, dedup, "01 - x.flac")), "bin entry was moved"
    assert open(occupant, "rb").read() == before, "the occupant was overwritten"
    assert dedup in manifest()["entries"], manifest()


def test_dedup_entry_restores_to_its_origin():
    dedup = ORIG_ALBUM + " (2)"
    shutil.rmtree(ORIG_DIR)               # the occupant is gone, origin is free again
    res = mlo_main.trash_restore(mlo_main.TrashRestore(names=[dedup]))
    assert res == {"restored": [{"name": dedup, "to": ORIG_DIR_API}], "failed": []}, res
    assert os.path.isfile(os.path.join(ORIG_DIR, "01 - x.flac")), "deduped entry not restored"
    assert dedup not in trash_names(), trash_names()
    assert dedup not in manifest()["entries"], manifest()
    assert manifest()["entries"] == {}, manifest()
    shutil.rmtree(os.path.join(MF, "Artists"))


LEGACY = "legacy-album"       # in the bin with no manifest record at all


def test_unknown_origin_needs_dest():
    write(os.path.join(LEGACY, "01 - legacy.flac"), 111)
    row = [e for e in listing()["entries"] if e["name"] == LEGACY][0]
    assert row["origin"] is None, row
    res = mlo_main.trash_restore(mlo_main.TrashRestore(names=[LEGACY]))
    assert res["restored"] == [], res
    assert len(res["failed"]) == 1, res
    assert res["failed"][0]["error"] == "original location unknown — pass dest", res
    assert os.path.isfile(os.path.join(TRASH, LEGACY, "01 - legacy.flac")), "entry moved"
    assert not os.path.exists(os.path.join(MF, LEGACY)), "entry escaped into the library"
    # the same entry restores once a destination is supplied
    target = os.path.join(MF, "Restored")
    os.makedirs(target, exist_ok=True)
    res = mlo_main.trash_restore(mlo_main.TrashRestore(names=[LEGACY], dest=target))
    to = os.path.join(target, LEGACY).replace("\\", "/")
    assert res == {"restored": [{"name": LEGACY, "to": to}], "failed": []}, res
    assert os.path.isfile(os.path.join(target, LEGACY, "01 - legacy.flac")), res
    assert LEGACY not in trash_names(), trash_names()
    assert LEGACY not in manifest()["entries"], manifest()


def test_bad_dest_is_400_and_moves_nothing():
    entry = "destless"
    write(os.path.join(entry, "01 - x.flac"), 5)
    outside = tempfile.mkdtemp(prefix="mlo-trash-outside-")
    try:
        for bad in (outside, os.path.join(MF, "no-such-folder"), "", "relative-ish"):
            try:
                mlo_main.trash_restore(mlo_main.TrashRestore(names=[entry], dest=bad))
            except HTTPException as e:
                assert e.status_code == 400, (bad, e.status_code)
            else:
                raise AssertionError(f"dest {bad!r} should have been refused")
        assert os.path.isfile(os.path.join(TRASH, entry, "01 - x.flac")), "entry moved"
        assert entry in trash_names(), trash_names()
        assert os.listdir(outside) == [], os.listdir(outside)
    finally:
        shutil.rmtree(outside, ignore_errors=True)
        shutil.rmtree(os.path.join(TRASH, entry), ignore_errors=True)


STALE_ALBUM = "[Album] 1999-01-01 - 1999-01-01 - Stale Entry {US - CD - X}"
STALE_DIR = os.path.join(MF, "Artists", "Stale Artist", STALE_ALBUM)


def test_refused_delete_keeps_manifest():
    """A delete that removed nothing must leave the manifest exactly alone —
    a refused name, an unknown name and the manifest itself included."""
    assert mlo_main._manifest_read(TRASH) == {}, mlo_main._manifest_read(TRASH)
    make_album(STALE_DIR)
    with_cache_spies(lambda _: mlo_main.album_remove(mlo_main.AlbumRemove(path=STALE_DIR)))
    before = mlo_main._manifest_read(TRASH)
    assert list(before) == [STALE_ALBUM], before

    def body(calls):
        res = mlo_main.trash_delete(mlo_main.TrashDelete(
            names=["..", "ghost", mlo_main._TRASH_MANIFEST, STALE_ALBUM + "/evil"]))
        assert res["deleted"] == [] and res["freed"] == 0, res
        assert [f["name"] for f in res["failed"]] == [
            "..", "ghost", mlo_main._TRASH_MANIFEST, STALE_ALBUM + "/evil"], res
        assert calls == [], calls

    with_cache_spies(body)
    assert mlo_main._manifest_read(TRASH) == before, mlo_main._manifest_read(TRASH)
    assert os.path.isfile(mlo_main._manifest_path(TRASH)), "manifest file vanished"
    assert STALE_ALBUM in trash_names(), trash_names()


def test_delete_prunes_manifest_and_stale_origin():
    """Deleting an entry drops its record; a later entry with the same name —
    one that never went through the move endpoint — must not inherit it."""
    assert list(mlo_main._manifest_read(TRASH)) == [STALE_ALBUM], mlo_main._manifest_read(TRASH)

    def body(calls):
        res = mlo_main.trash_delete(mlo_main.TrashDelete(names=[STALE_ALBUM]))
        assert res["deleted"] == [STALE_ALBUM] and res["failed"] == [], res
        assert res["freed"] == 1234, res["freed"]
        assert calls == ["tags", "mb", "shares"], calls

    with_cache_spies(body)
    assert STALE_ALBUM not in trash_names(), trash_names()
    assert mlo_main._manifest_read(TRASH) == {}, mlo_main._manifest_read(TRASH)
    # nothing left to remember: the bin keeps no manifest at all
    assert not os.path.exists(mlo_main._manifest_path(TRASH)), "empty manifest left behind"

    # the imposter: same name, dropped straight into the bin
    write(os.path.join(STALE_ALBUM, "01 - imposter.flac"), 7)
    try:
        row = [e for e in listing()["entries"] if e["name"] == STALE_ALBUM][0]
        assert row["origin"] is None, row
        res = mlo_main.trash_restore(mlo_main.TrashRestore(names=[STALE_ALBUM]))
        assert res["restored"] == [], res
        assert res["failed"][0]["error"] == "original location unknown — pass dest", res
        assert os.path.isdir(os.path.join(TRASH, STALE_ALBUM)), "entry left the bin"
        assert not os.path.exists(mlo_main._manifest_path(TRASH)), "failed restore wrote a manifest"
    finally:
        shutil.rmtree(os.path.join(TRASH, STALE_ALBUM), ignore_errors=True)
        shutil.rmtree(os.path.join(MF, "Artists"), ignore_errors=True)


_real_refresh = mlo_main._refresh_slskd_shares_soon

# state-mutating checks: order matters
for label, fn in [
    ("listing shape (count/bytes/newest-first/fields)", test_listing_shape),
    ("recursive track + byte counting, cover flag", test_track_and_byte_counts),
    ("label parsed from [Album] convention name", test_label_parsed_from_convention_name),
    ("label falls back to raw name", test_label_falls_back_to_raw_name),
    ("files listed recursively with rel, sorted", test_files_recursive_rel_and_sorted),
    ("files capped, file_count true", test_files_capped_but_file_count_true),
    ("symlinked subdir inside an entry not walked", test_linked_subdir_not_walked),
    ("symlinked dir hidden from listing", test_symlink_out_of_trash_hidden_from_listing),
    ("symlink out of trash refused", test_symlink_escape_refused),
    ("delete file entry + freed", test_delete_stray_file),
    ("delete deduped 'name (2)' entry", test_delete_deduped_entry),
    ("caches invalidated only after a real delete", test_delete_invalidates_caches),
    ("album deleted, trash + music folder survive", test_album_gone_but_trash_survives),
    ("missing entry -> failed, not an error", test_missing_entry_fails),
    ("empty request -> empty result", test_empty_request_is_not_an_error),
    ("escape attempts refused, nothing removed", test_escape_attempts_refused),
    ("missing bin -> exists:false", test_missing_bin),
    ("move records the origin in the manifest", test_move_records_origin),
    ("manifest hidden, undeletable and unrestorable", test_manifest_never_listed_or_touched),
    ("deduped move records the right origin", test_dedup_move_records_right_origin),
    ("restore returns to the exact original path", test_restore_returns_to_original_path),
    ("occupied destination refused, nothing moved", test_occupied_destination_refused),
    ("deduped entry restores to its origin", test_dedup_entry_restores_to_its_origin),
    ("unknown origin fails without dest, restores with one", test_unknown_origin_needs_dest),
    ("bad dest -> 400, nothing moved", test_bad_dest_is_400_and_moves_nothing),
    ("refused delete leaves the manifest untouched", test_refused_delete_keeps_manifest),
    ("delete prunes manifest, no stale origin reused", test_delete_prunes_manifest_and_stale_origin),
]:
    check(label, fn)

shutil.rmtree(MF, ignore_errors=True)
assert not os.path.exists(MF), MF

if FAILED:
    print(f"trash api: {len(FAILED)} check(s) failed: {', '.join(FAILED)}")
    sys.exit(1)
print("trash api: all assertions passed")
