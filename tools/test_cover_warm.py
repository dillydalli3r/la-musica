#!/usr/bin/env python3
"""The row-thumb WARMER (server.artcache.warm_thumbs).

`GET /api/cover?w=160` is what every library row and the player bar ask for,
and on a cold library each first request pays one decode+encode. The warmer
fills that same disk cache in the background when the library tree comes up, so
a first visit reads what a former pass (or a former run) already encoded.

What this pins, in order:

  * a pass encodes the thumbs for the covers it was given — the files really
    appear where `cover_thumb` writes them;
  * a SECOND pass over the same tree encodes NOTHING (every entry it would
    write is already there, proven by the files' mtimes, and the summary says
    so) — the pass is idempotent, so restarting the app is not a re-encode;
  * the pass is CAPPED: with a cap below the album count it touches exactly
    the cap and reports the rest, and never walks the whole tree;
  * a WALL BUDGET bounds a slow pass too — a decode that drags must not make
    the pass run past its budget;
  * covers that need no thumb are skipped, not failed: an album with no cover
    file, and a master already at or below the asked width (`cover_thumb`
    serves those as their own bytes and writes no entry — warming them would
    re-read the master every start);
  * an undecodable cover counts as failed, and never raises out of the pass.

The tree shapes the library actually builds are accepted: the dict
`server.library.build_library` returns, a bare iterable of album rows, and a
bare iterable of (folder, file) pairs.

Run: python tools/test_cover_warm.py     (exit 0 pass, 1 fail, 2 skip)
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from PIL import Image
except Exception as e:  # pragma: no cover - a missing Pillow is a SKIP
    print(f"skip: Pillow unavailable: {e}")
    sys.exit(2)

from server import artcache  # noqa: E402

FAILED = []


def ok(cond, label, extra=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{f' — {extra}' if extra else ''}")
        FAILED.append(label)


def eq(got, want, label):
    ok(got == want, label, f"got {got!r}, want {want!r}")


_TMP = tempfile.mkdtemp(prefix="mlo-cover-warm-test-")
# The real thumb dir belongs to a running app; this pass must not touch it.
artcache.thumb_dir = lambda music_folder=None: os.path.join(_TMP, "thumbs")


def _album(name, side, fmt="JPEG"):
    """A scratch album folder holding one real image named cover.jpg."""
    d = os.path.join(_TMP, "music", name)
    os.makedirs(d, exist_ok=True)
    Image.new("RGB", (side, side), (side % 255, 90, 140)).save(
        os.path.join(d, "cover.jpg"), fmt, quality=90)
    return d


def _thumb_of(folder):
    return artcache.thumb_path(os.path.join(folder, "cover.jpg"), artcache.WARM_WIDTH)


def _tree(*folders):
    return {"artists": [{"albums": [{"path": f, "cover_file": "cover.jpg"}
                                    for f in folders]}]}


BIG1, BIG2, BIG3 = _album("big1", 800), _album("big2", 700), _album("big3", 1200)
SMALL = _album("small", 100)                 # master at/below w: no thumb ever
NOCover = os.path.join(_TMP, "music", "nocover")
os.makedirs(NOCover, exist_ok=True)
BROKEN = os.path.join(_TMP, "music", "broken")
os.makedirs(BROKEN, exist_ok=True)
with open(os.path.join(BROKEN, "cover.jpg"), "wb") as fh:
    fh.write(b"this is not an image at all")

try:
    # ----------------------------------------------------------------- #
    # 1) A pass warms the covers it was given
    # ----------------------------------------------------------------- #
    print("1) a pass encodes the row thumbs it was given")
    tree = _tree(BIG1, BIG2, BIG3)
    stats = artcache.warm_thumbs(tree, workers=2)
    eq(stats["warmed"], 3, "three covers warmed")
    eq(stats["failed"], 0, "no failures")
    eq(stats["capped"], 0, "nothing capped")
    print(f"       summary: {stats}")
    for f in (BIG1, BIG2, BIG3):
        dest = _thumb_of(f)
        ok(bool(dest) and os.path.isfile(dest), f"thumb written for {os.path.basename(f)}")
    # …and it is the real, shrunk bytes, not the master
    blob = open(_thumb_of(BIG1), "rb").read()
    ok(0 < len(blob) < os.path.getsize(os.path.join(BIG1, "cover.jpg")),
       "thumb is smaller than its master", f"{len(blob)} bytes")

    # ----------------------------------------------------------------- #
    # 2) A second pass decodes NOTHING
    # ----------------------------------------------------------------- #
    print("2) a second pass is a no-op (nothing re-encoded)")
    before = {f: os.stat(_thumb_of(f)).st_mtime_ns for f in (BIG1, BIG2, BIG3)}
    again = artcache.warm_thumbs(tree, workers=2)
    eq(again["warmed"], 0, "second pass warmed nothing")
    eq(again["skipped"], 3, "all three skipped as already present")
    after = {f: os.stat(_thumb_of(f)).st_mtime_ns for f in (BIG1, BIG2, BIG3)}
    eq(after, before, "thumb files were not rewritten (mtimes unchanged)")
    print(f"       summary: {again}")

    # ----------------------------------------------------------------- #
    # 3) The pass is CAPPED
    # ----------------------------------------------------------------- #
    print("3) the pass is capped and returns")
    shutil.rmtree(artcache.thumb_dir(), ignore_errors=True)
    capped = artcache.warm_thumbs(tree, cap=2, workers=2)
    eq(capped["warmed"], 2, "cap of 2 warmed exactly 2")
    eq(capped["capped"], 1, "the third album is reported as not reached")
    eq(sum(1 for f in (BIG1, BIG2, BIG3) if os.path.isfile(_thumb_of(f))), 2,
       "exactly two thumbs on disk")

    # ----------------------------------------------------------------- #
    # 3b) …and a WALL BUDGET bounds a slow pass too
    # ----------------------------------------------------------------- #
    print("3b) a wall budget bounds a pass whose decodes drag")
    shutil.rmtree(artcache.thumb_dir(), ignore_errors=True)
    real_cover_thumb = artcache.cover_thumb

    def slow(path, w):
        time.sleep(0.4)
        return real_cover_thumb(path, w)

    artcache.cover_thumb = slow
    try:
        t0 = time.monotonic()
        slowstats = artcache.warm_thumbs(tree, workers=1, budget=0.4)
        elapsed = time.monotonic() - t0
    finally:
        artcache.cover_thumb = real_cover_thumb
    ok(elapsed < 2.0, "pass returned near its budget, not the full tree",
       f"took {elapsed:.2f}s")
    ok(slowstats["capped"] >= 1, "the budget left albums unreached",
       str(slowstats))
    print(f"       took {elapsed:.2f}s, summary: {slowstats}")

    # ----------------------------------------------------------------- #
    # 4) Covers that need no thumb are SKIPPED, not failed
    # ----------------------------------------------------------------- #
    print("4) no-cover and already-small albums are skipped")
    shutil.rmtree(artcache.thumb_dir(), ignore_errors=True)
    mixed = {"artists": [{"albums": [
        {"path": BIG1, "cover_file": "cover.jpg"},
        {"path": NOCover, "cover_file": ""},
        {"path": SMALL, "cover_file": "cover.jpg"},
    ]}]}
    mstats = artcache.warm_thumbs(mixed, workers=2)
    eq(mstats["warmed"], 1, "only the big cover warmed")
    eq(mstats["skipped"], 2, "no-cover and at-or-below-w skipped")
    eq(mstats["failed"], 0, "neither counted as a failure")
    ok(not os.path.isfile(_thumb_of(SMALL)),
       "an at-or-below-w master got no thumb entry")
    print(f"       summary: {mstats}")

    # ----------------------------------------------------------------- #
    # 5) An undecodable cover fails without raising
    # ----------------------------------------------------------------- #
    print("5) an undecodable cover is a failure, never an exception")
    shutil.rmtree(artcache.thumb_dir(), ignore_errors=True)
    bstats = artcache.warm_thumbs([(BROKEN, "cover.jpg")])
    eq(bstats["failed"], 1, "the broken cover counted as failed")
    eq(bstats["warmed"], 0, "and warmed nothing")

    # ----------------------------------------------------------------- #
    # 6) The row/pair shapes the callers pass are all accepted
    # ----------------------------------------------------------------- #
    print("6) rows and (folder, file) pairs are accepted as well as the tree")
    shutil.rmtree(artcache.thumb_dir(), ignore_errors=True)
    rows = [{"path": BIG1, "cover_file": "cover.jpg"},
            {"path": BIG2, "cover_file": "cover.jpg"}]
    eq(artcache.warm_thumbs(rows)["warmed"], 2, "album rows warmed")
    shutil.rmtree(artcache.thumb_dir(), ignore_errors=True)
    eq(artcache.warm_thumbs([(BIG1, "cover.jpg")])["warmed"], 1,
       "(folder, file) pairs warmed")

    # ----------------------------------------------------------------- #
    # 7) Several widths in one pass (the library draws rows at 160 and the
    #    default grid at 320, so the warmer fills both), and the cap still
    #    counts (cover, width) pairs, not albums
    # ----------------------------------------------------------------- #
    print("7) a pass warms more than one width, cap counts per width")
    shutil.rmtree(artcache.thumb_dir(), ignore_errors=True)
    multi = artcache.warm_thumbs(tree, w=(160, 320), workers=2)
    eq(multi["warmed"], 6, "three albums × two widths warmed")
    for width in (160, 320):
        for f in (BIG1, BIG2, BIG3):
            dest = artcache.thumb_path(os.path.join(f, "cover.jpg"), width)
            ok(bool(dest) and os.path.isfile(dest),
               f"thumb at w={width} for {os.path.basename(f)}")
    eq(artcache.warm_thumbs(tree, w=(160, 320), workers=2)["warmed"], 0,
       "a second multi-width pass decodes nothing")
    shutil.rmtree(artcache.thumb_dir(), ignore_errors=True)
    eq(artcache.warm_thumbs(tree, w=(160, 320), cap=3, workers=2)["warmed"], 3,
       "cap bounds (cover, width) pairs, not albums")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

if FAILED:
    print(f"FAILED ({len(FAILED)}): " + ", ".join(FAILED))
    sys.exit(1)
print("cover warm: all checks passed")
sys.exit(0)