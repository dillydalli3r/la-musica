#!/usr/bin/env python3
"""The caches that stop a locally hosted page from re-reading the library
(issue #70): `server/tagindex.py`, `tagcache.get_library`'s single flight and
`tagcache.cached_payload`'s page memo.

These are the only places the app answers a page from a cache whose correctness
is an ARGUMENT about invalidation, and the argument is what this suite pins:

  * a matching entry is a HIT — the builder does not run a second time;
  * a file that changed is read again (its own stat is the identity);
  * a file that is gone drops out of the payload, and an album that is gone
    loses its row (`tagindex.prune`);
  * a settings change cannot reach an entry computed under other settings
    (the whole config is part of the key);
  * the app's own writes are never served stale — every `invalidate_*` hook
    that the write paths already call drops BOTH layers;
  * a payload written by ANOTHER BUILD is not served at all: the index stamp
    carries the app version beside the payload's shape, so an upgrade that
    changes a grading rule re-grades instead of answering with the old verdict
    (the HDCD case: 4.8.0's stored "Unrecognized MEDIA value: HDCD" kept
    showing on Home and the Library page until the album's own files moved);
  * the first paint of several pages at once builds the library ONCE, and a
    builder that fails does not wedge the other waiters.

The tag/grade CONTENT of a payload is every other suite's business; this one
only needs files to exist, so it builds tiny stub files (a folder listing and a
file's stat are all an identity is made of). Nothing here touches the real
music folder: the scope is redirected to a temp one before `server.main` is
imported, exactly as `tools/test_trash_api.py` does.

Run: python tools/test_tagindex.py   (exit 0 pass, 1 fail)
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# The app's own state resolves through app_data_dir(music_folder) the moment it
# is first touched; redirect it BEFORE server.main is imported so no state file
# is created (or migrated) in the developer's real music folder.
MUSIC = tempfile.mkdtemp(prefix="mlo_tagindex_")
os.environ["MLO_MUSIC_FOLDER"] = MUSIC

import mlo.config as cfgmod  # noqa: E402
import mlo.paths as pathmod  # noqa: E402

_STUB_CONFIG = os.path.join(MUSIC, "config.json")
with open(_STUB_CONFIG, "w", encoding="utf-8") as _f:
    json.dump({"music_folder": MUSIC}, _f)
for _mod in (cfgmod, pathmod):
    _mod.CONFIG_FILE = _STUB_CONFIG

from server import library as lib_mod  # noqa: E402
from server import main as mlo_main  # noqa: E402
from server import tagcache, tagindex  # noqa: E402

FAILED = []


def check(label, cond, detail=""):
    if cond:
        print(f"ok   {label}")
    else:
        FAILED.append(label)
        print(f"FAIL {label}" + (f" — {detail}" if detail else ""))


def section(name):
    print(f"\n== {name} ==")


def write(path, size=64, data=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data if data is not None else b"stub" * max(1, size // 4))
    return path


# --------------------------------------------------------------------------- #
# The fixture: one album of two stub tracks, and a config of our own.
# --------------------------------------------------------------------------- #
ARTIST = os.path.join(MUSIC, "Artists", "Artist One")
ALBUM = os.path.join(ARTIST, "Album One")
TRACK_1 = write(os.path.join(ALBUM, "01 - One.flac"))
TRACK_2 = write(os.path.join(ALBUM, "02 - Two.flac"))

CFG = dict(mlo_main.load_config())
CFG["music_folder"] = MUSIC
mlo_main.load_config = lambda: dict(CFG)

# Count the real builds: `build_album` resolves `_build_album` through the
# module, so wrapping it observes every path that reaches the disk.
BUILDS = {"n": 0}
_REAL_BUILD = lib_mod._build_album


def _counted_build(album_dir, cfg, light=False):
    BUILDS["n"] += 1
    return _REAL_BUILD(album_dir, cfg, light)


lib_mod._build_album = _counted_build


def rows():
    """How many album rows the index holds, read from the file itself."""
    db = tagindex.db_file(CFG)
    if not db or not os.path.isfile(db):
        return -1
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute("SELECT count(*) FROM album_payload").fetchone()[0])
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 1) A hit is a hit, and it is on disk
# --------------------------------------------------------------------------- #
section("index: store, then serve without rebuilding")
tagcache.invalidate_all()
before = BUILDS["n"]
p1 = lib_mod.build_album(ALBUM, CFG, light=True)
built_once = BUILDS["n"]
check("the first read builds", built_once == before + 1, BUILDS["n"])
check("the row is on disk", rows() >= 1, rows())
p2 = lib_mod.build_album(ALBUM, CFG, light=True)
check("the second read does not build", BUILDS["n"] == built_once, BUILDS["n"])
check("and answers the same payload", p2 == p1)

# --------------------------------------------------------------------------- #
# 2) A changed file is read again — the folder's own stats are the identity
# --------------------------------------------------------------------------- #
section("index: a changed file is re-read")
write(os.path.join(ALBUM, "01 - One.jpg"), 32)              # a sidecar cover
p3 = lib_mod.build_album(ALBUM, CFG, light=True)
check("adding a file to the folder misses", BUILDS["n"] == built_once + 1, BUILDS["n"])
check("the fresh payload carries it",
      (p3.get("tracks") or [{}])[0].get("cover_file") == "01 - One.jpg",
      p3.get("tracks", [{}])[0].get("cover_file"))

built_once = BUILDS["n"]
with open(TRACK_1, "ab") as fh:                             # content, not name
    fh.write(b"more bytes")
p4 = lib_mod.build_album(ALBUM, CFG, light=True)
check("a rewritten file misses", BUILDS["n"] == built_once + 1, BUILDS["n"])
check("and the payload is still the album", len(p4.get("tracks") or []) == 2)

# --------------------------------------------------------------------------- #
# 3) A deleted file drops out; a deleted album loses its row
# --------------------------------------------------------------------------- #
section("index: deletions drop out")
built_once = BUILDS["n"]
os.remove(os.path.join(ALBUM, "01 - One.jpg"))
p5 = lib_mod.build_album(ALBUM, CFG, light=True)
check("a removed file misses", BUILDS["n"] == built_once + 1, BUILDS["n"])
check("and is not in the payload",
      (p5.get("tracks") or [{}])[0].get("cover_file") is None,
      p5.get("tracks", [{}])[0].get("cover_file"))

GONE = os.path.join(ARTIST, "Album Gone")
write(os.path.join(GONE, "01 - x.flac"))
tagcache.invalidate_all()
lib_mod.build_album(GONE, CFG, light=True)
lib_mod.build_album(ALBUM, CFG, light=True)
check("two albums, two rows", rows() == 2, rows())
shutil.rmtree(GONE)
tagindex.prune(CFG)
check("a pruned album loses its row", rows() == 1, rows())
kept = BUILDS["n"]
lib_mod.build_album(ALBUM, CFG, light=True)
check("the album that is still there keeps its row and its payload",
      BUILDS["n"] == kept and rows() == 1, (BUILDS["n"] - kept, rows()))

# The growth bound: entries written under a config the user has since changed
# are unreachable but still on disk, so rows nothing has read in a year go.
_conn = sqlite3.connect(tagindex.db_file(CFG))
_conn.execute("UPDATE album_payload SET built_at = ?",
              (time.time() - 400 * 24 * 3600.0,))
_conn.commit()
_conn.close()
tagindex.prune(CFG)
check("a year-old row is swept", rows() == 0, rows())

# --------------------------------------------------------------------------- #
# 4) A settings change cannot reach an entry computed under other settings
# --------------------------------------------------------------------------- #
section("index: the config is part of the key")
built_once = BUILDS["n"]
CFG2 = dict(CFG)
CFG2["grade_check_missing_tags"] = not CFG.get("grade_check_missing_tags", True)
p6 = lib_mod.build_album(ALBUM, CFG2, light=True)
check("another setting builds instead of hitting", BUILDS["n"] == built_once + 1)
check("and the payload is the other setting's", p6 != p5)
p7 = lib_mod.build_album(ALBUM, CFG2, light=True)
check("that entry is cached in its turn", BUILDS["n"] == built_once + 1, BUILDS["n"])
p8 = lib_mod.build_album(ALBUM, CFG, light=True)
check("the original setting is still its own entry",
      BUILDS["n"] == built_once + 1 and p8 == p5, BUILDS["n"])

# --------------------------------------------------------------------------- #
# 5) The page memo is dropped by the write hooks
# --------------------------------------------------------------------------- #
section("pages: a write hook is never served stale")
tagcache.invalidate_all()
route_builds = BUILDS["n"]
page1 = mlo_main.get_album(path=ALBUM)
check("the album page builds once", BUILDS["n"] == route_builds + 1, BUILDS["n"])
check("and is served from the memo next time",
      (mlo_main.get_album(path=ALBUM), BUILDS["n"] == route_builds + 1)[1],
      BUILDS["n"])

# The write itself, and the hook every tag write/rename fires.
NEW_TRACK = write(os.path.join(ALBUM, "03 - Three.flac"))
tagcache.invalidate_path(NEW_TRACK)
page2 = mlo_main.get_album(path=ALBUM)
check("invalidate_path drops the page AND the index row",
      len(page2.get("tracks") or []) == 3 and page1 != page2,
      [len(page1.get("tracks") or []), len(page2.get("tracks") or [])])

# ...and the scoped invalidation an import fires.
os.remove(NEW_TRACK)
tagcache.invalidate_album(ALBUM)
page3 = mlo_main.get_album(path=ALBUM)
check("invalidate_album drops it too", len(page3.get("tracks") or []) == 2,
      len(page3.get("tracks") or []))

# ...and the artist page, whose payload is its albums'.
artist1 = mlo_main.get_artist(path=ARTIST)
artist_builds = BUILDS["n"]
check("the artist page is memoized",
      (mlo_main.get_artist(path=ARTIST), BUILDS["n"] == artist_builds)[1],
      BUILDS["n"])
write(os.path.join(ALBUM, "03 - Three.flac"))
tagcache.invalidate_all()
artist2 = mlo_main.get_artist(path=ARTIST)
check("invalidate_all drops the artist page too", artist1 != artist2)
check("and it rebuilt", BUILDS["n"] > artist_builds)

# --------------------------------------------------------------------------- #
# 5b) The same thing with a REAL write, through the app's own writer: a tag
#     write moves the file's stat (so the index misses by itself) and fires the
#     hook the write paths call (so the page memo goes). The app's tag routes
#     are `tagcache.invalidate_path(file)`; this is that pair end to end.
# --------------------------------------------------------------------------- #
section("pages: a real tag write is never served stale")
FLAC_EXE = ""
for _root, _dirs, _files in os.walk(os.path.join(ROOT, ".dependencies")):
    if "flac.exe" in _files:
        FLAC_EXE = os.path.join(_root, "flac.exe")
        break
if not FLAC_EXE:
    print("note: no flac.exe under .dependencies — the real-write case is skipped")
else:
    import subprocess
    import wave

    real_album = os.path.join(ARTIST, "Real Album")
    os.makedirs(real_album, exist_ok=True)
    real_track = os.path.join(real_album, "01 - Real.flac")
    wav = real_track + ".wav"
    with wave.open(wav, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00\x00\x00" * 22050)
    subprocess.run([FLAC_EXE, "-s", "-f", "-8", "-o", real_track, wav], check=True,
                   capture_output=True)
    os.remove(wav)

    tagcache.invalidate_all()
    before_page = mlo_main.get_album(path=real_album)
    _first = (before_page.get("tracks") or [{}])[0]
    check("the real album page has no title yet",
          not str((_first.get("tags") or {}).get("TITLE") or "").strip(),
          (_first.get("tags") or {}).get("TITLE"))

    from mlo.audio import AudioFile
    wrote = AudioFile(real_track).set_tag("TITLE", "Written By The App")
    check("the tag write itself succeeded", bool(wrote))
    tagcache.invalidate_path(real_track)     # what the tag routes fire

    after_page = mlo_main.get_album(path=real_album)
    _first = (after_page.get("tracks") or [{}])[0]
    check("the write is on the page the next time it is asked for",
          (_first.get("tags") or {}).get("TITLE") == "Written By The App",
          (_first.get("tags") or {}).get("TITLE"))

# --------------------------------------------------------------------------- #
# 6) An index that cannot be used is not a broken page
# --------------------------------------------------------------------------- #
section("index: an unusable index falls back to building")
tagcache.invalidate_all()
_db = tagindex.db_file(CFG)
with open(_db, "wb") as _fh:                 # a corrupt file, not a database
    _fh.write(b"this is not a sqlite database" * 100)
tagindex._MEM.clear()
tagindex._conn, tagindex._conn_path = None, None
tagcache.invalidate_all()                    # clears rows, reopens the file
broken = BUILDS["n"]
p_ok = lib_mod.build_album(ALBUM, CFG, light=True)
check("a corrupt index still answers", bool(p_ok.get("tracks") or []), p_ok.get("tracks"))
check("by building (no exception, no crash)", BUILDS["n"] == broken + 1, BUILDS["n"])
check("and the next read still works", lib_mod.build_album(ALBUM, CFG, light=True) == p_ok)

# --------------------------------------------------------------------------- #
# 7) One library build for everybody waiting
# --------------------------------------------------------------------------- #
section("library: single flight")
CALLS = {"n": 0}
KING = threading.Lock()


def slow_builder():
    with KING:
        CALLS["n"] += 1
    time.sleep(0.3)
    return {"folder": MUSIC, "artists": []}


BARRIER = threading.Barrier(6)
OUT = [None] * 6


def worker(i):
    BARRIER.wait()
    OUT[i] = tagcache.get_library(("single-flight-test", (), 0, False), slow_builder)


threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
t0 = time.time()
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=30)
elapsed = time.time() - t0
check("six concurrent first paints build ONCE", CALLS["n"] == 1, CALLS["n"])
check("all of them got the payload",
      all(o == {"folder": MUSIC, "artists": []} for o in OUT), OUT)
check("and none of them waited for a queue of builds", elapsed < 2.0, elapsed)

# A builder that fails must release the flight, or every later caller waits for
# a result that is never coming.
tagcache.invalidate_all()


def boom():
    raise RuntimeError("builder failed")


try:
    tagcache.get_library(("single-flight-fail", (), 0, False), boom)
    check("a failing builder raises to its caller", False)
except RuntimeError:
    check("a failing builder raises to its caller", True)
else:  # pragma: no cover
    pass
good = tagcache.get_library(("single-flight-fail", (), 0, False),
                            lambda: {"folder": "after-failure", "artists": []})
check("and the next caller builds instead of waiting",
      good == {"folder": "after-failure", "artists": []}, good)
tagcache.invalidate_all()

# --------------------------------------------------------------------------- #
# 7b) A tree that is merely OLD is served, and re-derived BEHIND the request.
#     This is the difference between "the library page took 14.5 s once a
#     minute" and a page load that never waits for a walk it did not ask for.
#     The TTL is patched rather than slept through: the behaviour is the
#     contract, not the number.
# --------------------------------------------------------------------------- #
section("library: an expired tree is served, not waited for")
SWR = {"n": 0}
SWR_KEY = ("swr-test", (), 0, False)


def swr_builder():
    SWR["n"] += 1
    return {"folder": MUSIC, "artists": [], "build": SWR["n"]}


check("the first paint builds it", swr_builder.__name__ and
      tagcache.get_library(SWR_KEY, swr_builder).get("build") == 1, SWR["n"])
_real_ttl = tagcache._LIB_TTL
tagcache._LIB_TTL = 0.05
try:
    time.sleep(0.1)
    t0 = time.time()
    stale = tagcache.get_library(SWR_KEY, swr_builder)
    served = time.time() - t0
    check("an expired tree answers at once", served < 0.1, served)
    check("with the tree that was already there", stale.get("build") == 1, stale)
    for _ in range(250):
        if SWR["n"] >= 2:
            break
        time.sleep(0.02)
    check("and the refresh runs behind the request", SWR["n"] >= 2, SWR["n"])
    fresh = tagcache.get_library(SWR_KEY, swr_builder)
    check("the next request is the refreshed tree", fresh.get("build") == 2, fresh)
finally:
    tagcache._LIB_TTL = _real_ttl
    tagcache.invalidate_all()

# ...and neither does an in-app WRITE: `invalidate_album` is what an import,
# a tag write and a script run fire, and the page that follows it must be
# served from memory while the tree is re-derived.
section("library: an in-app write refreshes behind the page")
DIRTY = {"n": 0}
DIRTY_KEY = ("dirty-test", (), 0, False)


def dirty_builder():
    DIRTY["n"] += 1
    return {"folder": MUSIC, "artists": [], "build": DIRTY["n"]}


tagcache.get_library(DIRTY_KEY, dirty_builder)
tagcache.invalidate_album(ALBUM)
t0 = time.time()
after_write = tagcache.get_library(DIRTY_KEY, dirty_builder)
waited = time.time() - t0
check("a write does not make the next page wait", waited < 0.1, waited)
check("it is served the tree from memory", after_write.get("build") == 1, after_write)
for _ in range(250):
    if DIRTY["n"] >= 2:
        break
    time.sleep(0.02)
check("and the refresh behind it lands", DIRTY["n"] >= 2, DIRTY["n"])
check("so the request after that is the fresh tree",
      tagcache.get_library(DIRTY_KEY, dirty_builder).get("build") == 2)
tagcache.invalidate_all()

# --------------------------------------------------------------------------- #
# 8) The library-state stamp is PER ALBUM. One album's audit run must not make
#    every other album's row unreachable — that is what turned the library page
#    after an import into a whole-library re-read (14.5 s measured on a
#    170-file install whose warm reads are 40 ms).
# --------------------------------------------------------------------------- #
section("index: the state stamp is one album's own")
import mlo.audit as auditmod  # noqa: E402

OTHER_ARTIST = os.path.join(MUSIC, "Artists", "Artist Two")
OTHER = os.path.join(OTHER_ARTIST, "Album Two")
OTHER_TRACK = write(os.path.join(OTHER, "01 - Other.flac"))
EVIDENCE = auditmod._evidence_path(CFG)
os.makedirs(os.path.dirname(EVIDENCE), exist_ok=True)
with open(EVIDENCE, "w", encoding="utf-8") as fh:
    json.dump({}, fh)
sig_before = tagindex.dir_signature(ALBUM, CFG)
sig_other_before = tagindex.dir_signature(OTHER, CFG)
with open(EVIDENCE, "w", encoding="utf-8") as fh:
    json.dump({OTHER_TRACK: [1, 2, True, "x", "ok"]}, fh)
check("another album's audit evidence leaves this album's identity alone",
      tagindex.dir_signature(ALBUM, CFG) == sig_before)
check("and it does change the album it is about",
      tagindex.dir_signature(OTHER, CFG) != sig_other_before)
with open(EVIDENCE, "w", encoding="utf-8") as fh:
    json.dump({TRACK_1: [1, 2, True, "x", "ok"]}, fh)
check("this album's own evidence changes it",
      tagindex.dir_signature(ALBUM, CFG) != sig_before)

# --------------------------------------------------------------------------- #
# 10) A payload belongs to the BUILD that wrote it
# --------------------------------------------------------------------------- #
# The owner's case: an upgrade changes the grading RULES (the HDCD fix, whose
# 4.8.0 payload kept reporting "Unrecognized MEDIA value: HDCD" on Home and the
# Library page because the album's own files never moved). The index stamp
# therefore carries the app version beside the payload's shape, and a build
# that finds a row from another one drops it and re-grades.
section("index: another build's payload is not served")
tagcache.invalidate_all()
BUILDS["n"] = 0
lib_mod.build_album(ALBUM, CFG, light=True)
check("this build's payload is stored and served", BUILDS["n"] == 1, BUILDS["n"])
_real_stamp = tagindex.payload_stamp
tagindex.payload_stamp = lambda: _real_stamp() + "+other"
try:
    tagindex._MEM.clear()
    tagindex._conn, tagindex._conn_path = None, None
    BUILDS["n"] = 0
    again = lib_mod.build_album(ALBUM, CFG, light=True)
    check("a row stamped by another build is dropped, and the album is built again",
          BUILDS["n"] == 1, BUILDS["n"])
    check("…and the freshly built payload is the one served afterwards",
          lib_mod.build_album(ALBUM, CFG, light=True) == again and BUILDS["n"] == 1,
          BUILDS["n"])
finally:
    tagindex.payload_stamp = _real_stamp
    tagindex._MEM.clear()
    tagindex._conn, tagindex._conn_path = None, None

# --------------------------------------------------------------------------- #
print(f"\n{'FAILED: ' + ', '.join(FAILED) if FAILED else 'all checks passed'}")
os.environ.pop("MLO_MUSIC_FOLDER", None)
sys.exit(1 if FAILED else 0)
