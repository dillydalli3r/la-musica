#!/usr/bin/env python3
"""Regression tests for mlo.paths.move_path.

The incident this guards: shutil.move() silently degraded a same-volume album
rename that hit WinError 32 (a file inside still open by slskd) into
copytree() + rmtree(), so the album ended up duplicated in the library AND in
the download folder. Every case below pins one guarantee of the fix; the
lock cases need a Windows-style sharing violation, so they are skipped with a
message on a platform that cannot block a rename with an open handle.

Run: python tools/test_fs_move.py   (exit 0 = pass)
"""
import os
import shutil
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mlo import paths


def tree_bytes(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            total += os.path.getsize(os.path.join(root, name))
    return total


def make_album(root, name="Album", tracks=3, size=4096):
    d = os.path.join(root, name)
    os.makedirs(d)
    for i in range(tracks):
        with open(os.path.join(d, "%02d track.flac" % i), "wb") as fh:
            fh.write(b"A" * size)
    return d


def album_names(tracks=3):
    return sorted("%02d track.flac" % i for i in range(tracks))


def write_file(path, size):
    with open(path, "wb") as fh:
        fh.write(b"B" * size)
    return path


def lockable_here():
    """Whether an open handle on a child file blocks the parent-dir rename.

    Windows denies it (sharing violation / access denied) — that is the real
    failure mode of the incident. POSIX allows it, so the lock cases skip.
    """
    tmp = tempfile.mkdtemp(prefix="mlo_move_probe_")
    probe = make_album(tmp, "probe", tracks=1)
    fh = open(os.path.join(probe, "00 track.flac"), "r+b")
    try:
        os.rename(probe, probe + "_moved")
    except OSError as exc:
        print("  lock probe: rename blocked (winerror=%s errno=%s)"
              % (getattr(exc, "winerror", None), exc.errno))
        return True
    finally:
        fh.close()
        if os.path.isdir(probe + "_moved"):
            os.rename(probe + "_moved", probe)
        shutil.rmtree(tmp, ignore_errors=True)
    print("  lock probe: this platform renames with an open handle - lock cases skipped")
    return False


def cross_device_once(src):
    """Stand in for a move across volumes: the helper's own rename reports
    winerror 17 for the first attempt at *src*, then behaves normally."""
    real = paths._replace
    state = {"raised": False, "calls": 0}

    def fake(s, d):
        state["calls"] += 1
        if not state["raised"] and os.path.abspath(s) == os.path.abspath(src):
            state["raised"] = True
            raise OSError(18, "cross-device link", str(s), 17, None)
        return real(s, d)

    paths._replace = fake
    return state, real


def test_file_move(tmp):
    src = write_file(os.path.join(tmp, "one.flac"), 1024)
    os.makedirs(os.path.join(tmp, "dest"))
    dst = os.path.join(tmp, "dest", "one.flac")
    assert paths.move_path(src, dst), "same-volume file move failed"
    assert os.path.isfile(dst) and os.path.getsize(dst) == 1024, "destination wrong"
    assert not os.path.exists(src), "source file left behind"


def test_dir_move(tmp):
    src = make_album(tmp)
    got = tree_bytes(src)
    os.makedirs(os.path.join(tmp, "dest"))
    dst = os.path.join(tmp, "dest", "Album")
    assert paths.move_path(src, dst), "same-volume directory move failed"
    assert sorted(os.listdir(dst)) == album_names(), "destination contents wrong"
    assert not os.path.exists(src), "source directory left behind"
    assert tree_bytes(tmp) == got, "bytes doubled -> the directory was copied, not renamed"


def test_locked_child_dir_move(tmp):
    src = make_album(tmp, "Locked")
    got = tree_bytes(src)
    os.makedirs(os.path.join(tmp, "dest"))
    dst = os.path.join(tmp, "dest", "Locked")
    handle = open(os.path.join(src, "00 track.flac"), "r+b")
    logs = []
    result = {}

    def work():
        result["ok"] = paths.move_path(src, dst, attempts=40, delay=0.1, log=logs.append)

    t = threading.Thread(target=work)
    t.start()
    time.sleep(0.4)
    assert not os.path.exists(dst), ("destination appeared while the rename was still "
                                     "blocked -> the move degraded into a copy")
    time.sleep(0.6)
    handle.close()
    t.join(30)
    assert not t.is_alive(), "move never finished after the lock was released"
    assert result.get("ok") is True, "locked move returned %r" % (result.get("ok"),)
    assert not os.path.exists(src), "source directory left behind"
    assert tree_bytes(tmp) == got, "bytes doubled -> the album was copied, not renamed"
    assert len(logs) == 1, "expected one retry log, got %r" % (logs,)


def test_locked_past_attempts(tmp):
    src = make_album(tmp, "Stuck")
    got = tree_bytes(src)
    os.makedirs(os.path.join(tmp, "dest"))
    dst = os.path.join(tmp, "dest", "Stuck")
    logs = []
    handle = open(os.path.join(src, "01 track.flac"), "r+b")
    try:
        assert paths.move_path(src, dst, attempts=3, delay=0.05, log=logs.append) is False, \
            "move reported success while the source was still locked"
    finally:
        handle.close()
    assert not os.path.exists(dst), "destination touched even though the move failed"
    assert sorted(os.listdir(src)) == album_names(), "source damaged by the failed move"
    assert tree_bytes(src) == got, "source bytes changed"
    assert len(logs) == 2, "expected one retry log and one give-up log, got %r" % (logs,)


def test_cross_volume_file(tmp):
    src = write_file(os.path.join(tmp, "one.flac"), 2048)
    os.makedirs(os.path.join(tmp, "dest"))
    dst = os.path.join(tmp, "dest", "one.flac")
    state, real = cross_device_once(src)
    try:
        assert paths.move_path(src, dst), "cross-volume file move failed"
    finally:
        paths._replace = real
    assert state["calls"] >= 2, "the copy path never placed the file"
    assert os.path.getsize(dst) == 2048, "destination size wrong"
    assert not os.path.exists(src), "source file left behind"
    leftovers = [n for n in os.listdir(os.path.dirname(dst)) if ".mlo-tmp-" in n]
    assert not leftovers, "temporary copy left behind: %r" % (leftovers,)
    assert tree_bytes(tmp) == 2048, "bytes doubled -> the source copy was not removed"


def test_cross_volume_dir(tmp):
    src = make_album(tmp, "Album")
    got = tree_bytes(src)
    os.makedirs(os.path.join(tmp, "dest"))
    dst = os.path.join(tmp, "dest", "Album")
    state, real = cross_device_once(src)
    try:
        assert paths.move_path(src, dst), "cross-volume directory move failed"
    finally:
        paths._replace = real
    assert state["calls"] >= 2, "the tree copy never happened"
    assert sorted(os.listdir(dst)) == album_names(), "destination contents wrong"
    assert not os.path.exists(src), "source tree left behind"
    assert tree_bytes(tmp) == got, "bytes doubled -> the source tree was not removed"


def test_failed_copy_keeps_source(tmp):
    src = write_file(os.path.join(tmp, "one.flac"), 4096)
    dest_dir = os.path.join(tmp, "dest")
    os.makedirs(dest_dir)
    dst = os.path.join(dest_dir, "one.flac")
    real_replace, real_copy2 = paths._replace, paths.shutil.copy2

    def always_cross(s, d):
        raise OSError(18, "cross-device link", str(s), 17, None)

    def short_copy(s, d, **kw):
        shutil.copyfile(s, d)
        with open(d, "r+b") as fh:
            fh.truncate(10)
        return d

    paths._replace = always_cross
    paths.shutil.copy2 = short_copy
    try:
        assert paths.move_path(src, dst, attempts=2, delay=0.01) is False, \
            "size-verifying copy reported success with a truncated destination"
    finally:
        paths._replace = real_replace
        paths.shutil.copy2 = real_copy2
    assert os.path.isfile(src) and os.path.getsize(src) == 4096, \
        "a failed copy deleted or damaged the source"
    assert not os.path.exists(dst), "a failed copy left a destination behind"
    assert os.listdir(dest_dir) == [], "a failed copy left a partial file behind"


def main():
    locked_cases = ("locked child file released after ~1s", "locked past every attempt")
    cases = [
        ("same-volume file move", test_file_move),
        ("same-volume directory move", test_dir_move),
        ("locked child file released after ~1s", test_locked_child_dir_move),
        ("locked past every attempt", test_locked_past_attempts),
        ("cross-volume file move", test_cross_volume_file),
        ("cross-volume directory move", test_cross_volume_dir),
        ("failed copy never deletes the source", test_failed_copy_keeps_source),
    ]
    failed = 0
    for name, fn in cases:
        print("-", name)
        if name in locked_cases and not LOCKABLE:
            print("  SKIP: platform cannot block a rename with an open handle")
            continue
        tmp = tempfile.mkdtemp(prefix="mlo_move_")
        try:
            fn(tmp)
            print("  ok")
        except AssertionError as exc:
            failed += 1
            print("  FAIL:", exc)
        except Exception as exc:  # noqa: BLE001 - report and keep going
            failed += 1
            print("  ERROR: %s: %s" % (type(exc).__name__, exc))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if failed:
        print("FAILED:", failed)
        return 1
    print("all move_path cases passed")
    return 0


LOCKABLE = lockable_here()

if __name__ == "__main__":
    sys.exit(main())
