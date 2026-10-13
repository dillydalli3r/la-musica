#!/usr/bin/env python3
"""The lyrics KIND in the library payload and the wizard's scan — pinned.

A surface can only say whether a track HAS lyrics; the owner asked every one of
them to say WHICH KIND they are (synced/timed or plain) and to show a plain
lyric as the failing state while the user's own ``lyrics_allow_plain`` says
plain is not acceptable. The fact behind that is one payload field:

    lyrics_kind: "synced" | "plain" | null

derived by mlo.lyrics.stored_lyrics_kind from the two stored texts the grader
already reads (the LYRICS/UNSYNCEDLYRICS tag and the ``.lrc`` sidecar) and
stamped by mlo.grader beside ``lyrics_embedded`` / ``lyrics_lrc``.

Pinned here, on a temp library with one track of each shape:

  * a timed ``.lrc``               → "synced"
  * embedded lyrics with timestamps → "synced"
  * embedded lyrics without         → "plain"
  * a timed ``.lrc`` PLUS plain embedded lyrics → "synced" (both sources at
    once IS synced — the player follows the timed one)
  * no lyrics at all                → null

plus the consistency the field is built on (``lyrics_present`` is exactly
``lyrics_kind is not null``, and the two boolean flags agree with it), the same
field on ``GET /api/album/scan-tracks`` (the import wizard's own read of the
same tracks), and the query catalogue entry a Library column sorts/filters by.

Run: python tools/test_lyrics_kind.py
Exit 0 = pass.
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mlo.audio import AudioFile  # noqa: E402
from server import api_query, library as mlo_library  # noqa: E402
from server import main as mlo_main  # noqa: E402  (heavy import)
from fastapi.testclient import TestClient  # noqa: E402

TMP = tempfile.mkdtemp(prefix="mlo-lyrkind-")
MUSIC = os.path.join(TMP, "music")
ALBUM = os.path.join(MUSIC, "Artists", "Kind Artist", "2020 - Kind Album")
os.makedirs(ALBUM)
os.makedirs(os.path.join(MUSIC, ".mlo", "data"))

CFG = {"music_folder": MUSIC}

# A real (if silent) MP3 frame: the album's identity is read from the tracks'
# own tags, and a junk byte string would be an unreadable file.
_MP3_FRAME = bytes([0xFF, 0xFB, 0x90, 0x00]) + b"\x00" * 413

TIMED_LRC = "[00:01.00]first line\n[00:05.00]second line\n"
PLAIN_EMBEDDED = "first line\nsecond line\n"


def write_track(name, tag_lyrics=None, sidecar=None):
    path = os.path.join(ALBUM, name)
    with open(path, "wb") as f:
        f.write(_MP3_FRAME * 40)
    af = AudioFile(path)
    for key, value in {"ALBUMARTIST": "Kind Artist", "ALBUM": "Kind Album",
                       "TITLE": name}.items():
        assert af.set_tag(key, value), (path, key)
    if tag_lyrics is not None:
        assert af.set_lyrics(tag_lyrics), path
    if sidecar is not None:
        with open(os.path.splitext(path)[0] + ".lrc", "w", encoding="utf-8") as fh:
            fh.write(sidecar)
    return path


write_track("1-01 TimedLrc.mp3", sidecar=TIMED_LRC)
write_track("1-02 EmbeddedTimed.mp3", tag_lyrics=TIMED_LRC)
write_track("1-03 EmbeddedPlain.mp3", tag_lyrics=PLAIN_EMBEDDED)
write_track("1-04 Both.mp3", tag_lyrics=PLAIN_EMBEDDED, sidecar=TIMED_LRC)
write_track("1-05 None.mp3")

EXPECTED = {
    "1-01 TimedLrc.mp3": "synced",
    "1-02 EmbeddedTimed.mp3": "synced",
    "1-03 EmbeddedPlain.mp3": "plain",
    "1-04 Both.mp3": "synced",
    "1-05 None.mp3": None,
}

failures = []


def check(name, cond, detail=""):
    if not cond:
        failures.append(f"{name} — {detail}")
        print(f"FAIL {name} — {detail}")


# --------------------------------------------------------------------------- #
# 1) the library payload (`server.library.build_album` — what /api/library and
#    the album page serve)
# --------------------------------------------------------------------------- #
album = mlo_library.build_album(ALBUM, CFG)
check("build_album returns an album", bool(album) and bool(album.get("tracks")),
      repr(album)[:200])
rows = {tr["file"]: tr for tr in (album or {}).get("tracks", [])}
check("every fixture track is in the payload", set(rows) == set(EXPECTED),
      f"{sorted(rows)} != {sorted(EXPECTED)}")

for name, want in EXPECTED.items():
    tr = rows.get(name, {})
    check(f"{name}: lyrics_kind", tr.get("lyrics_kind") == want,
          f"{tr.get('lyrics_kind')!r} != {want!r}")
    # One fact, two fields: presence IS the kind not being null, and the two
    # flags the payload has always carried agree with it — a reader of either
    # cannot see a different story (the timed-.lrc-plus-plain-embedded track
    # has BOTH flags set and is still, correctly, one kind: synced).
    check(f"{name}: lyrics_present follows the kind",
          tr.get("lyrics_present") == (want is not None),
          repr(tr.get("lyrics_present")))
    check(f"{name}: presence matches the payload's own flags",
          bool(tr.get("lyrics_present")) == bool(tr.get("lyrics_embedded")
                                                 or tr.get("lyrics_lrc")),
          f"{tr.get('lyrics_embedded')!r}/{tr.get('lyrics_lrc')!r}")

both = rows.get("1-04 Both.mp3", {})
check("both sources at once: the timed .lrc is credited",
      both.get("lyrics_lrc") is True and both.get("lyrics_embedded") is True
      and both.get("lyrics_kind") == "synced", repr(both)[:200])

# An album with no lyrics at all still carries the field, on every track: no
# reader has to treat "absent" as a case of its own.
check("the field is present (not missing) on every track row",
      all("lyrics_kind" in tr for tr in rows.values()), sorted(rows))

# --------------------------------------------------------------------------- #
# 2) the same field on the import wizard's read of the same folder
#    (GET /api/album/scan-tracks), which the wizard's lyrics step runs on
# --------------------------------------------------------------------------- #
mlo_main.load_config = lambda *a, **k: dict(CFG)
client = TestClient(mlo_main.app)   # no lifespan: no workers, no slskd boot
r = client.get("/api/album/scan-tracks", params={"path": ALBUM})
check("scan-tracks answers", r.status_code == 200, r.text[:200])
scanned = {t["file"]: t for t in (r.json().get("tracks") if r.status_code == 200 else [])}
check("scan-tracks lists the fixture tracks", set(scanned) == set(EXPECTED),
      f"{sorted(scanned)}")
for name, want in EXPECTED.items():
    t = scanned.get(name, {})
    check(f"scan-tracks {name}: lyrics_kind", t.get("lyrics_kind") == want,
          f"{t.get('lyrics_kind')!r} != {want!r}")
    check(f"scan-tracks {name}: lyrics_present follows the kind",
          t.get("lyrics_present") == (want is not None), repr(t.get("lyrics_present")))

# --------------------------------------------------------------------------- #
# 3) the query field the Library's own column sorts/filters by
# --------------------------------------------------------------------------- #
catalogue = {f["field"]: f for g in api_query.catalogue()["groups"] for f in g["fields"]}
entry = catalogue.get("library.lyrics_kind")
check("library.lyrics_kind is in the query catalogue", bool(entry), "missing")
if entry:
    check("…with the two kind values", entry.get("values") == ["synced", "plain"],
          repr(entry.get("values")))
    check("…tracks-only", entry.get("targets") == ["tracks"], repr(entry.get("targets")))

# --------------------------------------------------------------------------- #
# 2b) the grader verdict behind those flags is MEMOIZED per folder, on the
#     folder's own signature: the wizard mounts and re-fetches, and re-opening
#     every file for an answer those files already decide was pure cost. A
#     write in the folder, and every tag-cache invalidation, must still miss.
#     (A stale verdict is worse than a slow scan: the wizard's per-track steps
#     read it as "Plain" / "no lyrics".)
# --------------------------------------------------------------------------- #
from mlo import grader  # noqa: E402
from server import tagcache  # noqa: E402

_real_grade = grader._grade_album
_graded = []


def _counting_grade(folder, fmt, cfg=None):
    _graded.append(os.path.normpath(folder))
    return _real_grade(folder, fmt, cfg)


def _scan():
    return client.get("/api/album/scan-tracks", params={"path": ALBUM})


grader._grade_album = _counting_grade
try:
    tagcache.invalidate_album(ALBUM)
    _graded.clear()
    r = _scan()
    check("the first scan after an invalidation grades the folder",
          r.status_code == 200 and _graded == [os.path.normpath(ALBUM)], f"{_graded}")
    _graded.clear()
    r2 = _scan()
    check("a second scan answers the SAME rows from the memo",
          r2.status_code == 200 and r2.json() == r.json(), r2.text[:200])
    check("…and grades nothing again", _graded == [], f"{_graded}")

    # a write inside the folder misses: the file's own stat is in the signature
    target = os.path.join(ALBUM, "1-05 None.mp3")
    st = os.stat(target)
    os.utime(target, (st.st_atime, st.st_mtime + 5))
    _graded.clear()
    r3 = _scan()
    check("a file rewritten in the folder re-grades it",
          _graded == [os.path.normpath(ALBUM)], f"{_graded}")
    check("…and the rows are still the file's own", r3.json() == r.json())

    # an in-app write drops it outright (the wizard's own lyrics step writes)
    tagcache.invalidate_album(ALBUM)
    _graded.clear()
    _scan()
    check("the tag cache's own invalidation drops the verdict",
          _graded == [os.path.normpath(ALBUM)], f"{_graded}")
    # a per-file invalidation (a tag write to one track) reaches the folder too
    tagcache.invalidate_path(target)
    _graded.clear()
    _scan()
    check("a per-file invalidation drops the folder's verdict",
          _graded == [os.path.normpath(ALBUM)], f"{_graded}")
finally:
    grader._grade_album = _real_grade

# --------------------------------------------------------------------------- #
# 2c) /api/lyrics/auto asks its tracks on `worker_count` lanes, and answers the
#     serial loop's own rows: same result dicts, same order, same counts. The
#     stub fetcher is the instrument — it counts how many fetches are inside it
#     at once, so a one-lane run can never exceed 1.
# --------------------------------------------------------------------------- #
from server import api_lyrics  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

_probe = {"inside": 0, "peak": 0}
_probe_lock = threading.Lock()
_real_load_config = api_lyrics.load_config
_real_fetch_one = api_lyrics.fetch_one


def _stub_fetch_one(path, cfg, replace=False, mark_absent=True):
    with _probe_lock:
        _probe["inside"] += 1
        _probe["peak"] = max(_probe["peak"], _probe["inside"])
    try:
        time.sleep(0.05)
        return {"path": path, "status": "ok", "provider": "stub",
                "provider_label": "Stub", "kind": "synced", "synced": True,
                "wrote": {"embedded": True, "lrc": None},
                "reason": "", "error": ""}
    finally:
        with _probe_lock:
            _probe["inside"] -= 1


def _auto(paths, lanes):
    _probe.update(inside=0, peak=0)
    api_lyrics.load_config = lambda *a, **k: dict(CFG)
    api_lyrics.fetch_one = _stub_fetch_one
    real_wc = api_lyrics.worker_count
    api_lyrics.worker_count = lambda *a, **k: lanes
    try:
        return api_lyrics.lyrics_auto(api_lyrics.LyricsPathsRequest(paths=paths))
    finally:
        api_lyrics.worker_count = real_wc


try:
    LANE_PATHS = [os.path.join(ALBUM, n) for n in sorted(EXPECTED)]
    OUTSIDE = os.path.join(TMP, "outside.mp3")
    with open(OUTSIDE, "wb") as fh:
        fh.write(_MP3_FRAME * 4)
    sent = LANE_PATHS + [OUTSIDE]

    serial = _auto(sent, 1)
    check("one lane never overlaps", _probe["peak"] == 1, repr(_probe))
    laned = _auto(sent, 8)
    check("the lanes really overlap", _probe["peak"] > 1, repr(_probe))
    check("…never more than worker_count", _probe["peak"] <= 8, repr(_probe))

    check("the reply is the serial loop's own, row for row",
          laned == serial, f"{laned} != {serial}")
    check("the counts are the serial loop's own",
          (laned["ok"], laned["skipped"], laned["failed"])
          == (len(LANE_PATHS), 0, 1), str(laned["ok"]) + "/" + str(laned["failed"]))
    check("a rejected path is still reported FIRST, as its own failure",
          laned["results"][0]["path"] == OUTSIDE
          and laned["results"][0]["status"] == "failed", str(laned["results"][0]))
    check("the accepted rows are the paths as SENT, in request order",
          [r["path"] for r in laned["results"][1:]] == LANE_PATHS,
          str([r["path"] for r in laned["results"]]))
finally:
    api_lyrics.load_config = _real_load_config
    api_lyrics.fetch_one = _real_fetch_one

# --------------------------------------------------------------------------- #
# 2d) the no-lyrics answer is settled ONCE for the whole batch, and the strip
#     hears the fetch track by track: the route defers the answer
#     (`mark_absent=False`, exactly like script 13's run), then asks
#     `lyrics_absent` a single time for every track that needed it — the
#     per-track fan-out inside the lanes was the biggest waste of a lyrics
#     step (the module's own comment), and it is why one chunk could hold
#     "0/19" on screen for minutes (the owner's report). Frames: one per
#     completed track, bracketed by 0/N and N/N.
# --------------------------------------------------------------------------- #
from server import instrumental as _inst_mod  # noqa: E402
from server import job_locks as _jl  # noqa: E402

_mark_flags, _absent_batches, _frames, _real_wc2 = [], [], [], api_lyrics.worker_count


def _deferred_fetch(path, cfg, replace=False, mark_absent=True):
    _mark_flags.append(mark_absent)
    return {"path": path, "status": "skipped", "provider": None,
            "provider_label": None, "kind": None, "synced": False,
            "wrote": {"embedded": False, "lrc": None},
            "reason": "no confident match", "error": "", "needs_absent": True}


def _one_absent_pass(paths, cfg=None):
    _absent_batches.append(list(paths))
    return {"updated": 0, "values": {}, "evidence": {}, "reason": {},
            "skipped": "recorded"}


_real_absent_fn = _inst_mod.lyrics_absent
_real_publish_fn = _jl.publish
api_lyrics.load_config = lambda *a, **k: dict(CFG)
api_lyrics.fetch_one = _deferred_fetch
api_lyrics.worker_count = lambda *a, **k: 4
_inst_mod.lyrics_absent = _one_absent_pass
_jl.publish = (lambda done=None, total=None, text="", steps=None, job=None, hook=None:
               _frames.append((done, total)))
try:
    _deferred = api_lyrics.lyrics_auto(api_lyrics.LyricsPathsRequest(paths=LANE_PATHS))
finally:
    api_lyrics.load_config = _real_load_config
    api_lyrics.fetch_one = _real_fetch_one
    api_lyrics.worker_count = _real_wc2
    _inst_mod.lyrics_absent = _real_absent_fn
    _jl.publish = _real_publish_fn

check("the route DEFERS the no-lyrics answer on every track",
      _mark_flags == [False] * len(LANE_PATHS), str(_mark_flags))
check("ONE absent pass for the whole batch, naming every track that needed it",
      len(_absent_batches) == 1 and sorted(_absent_batches[0]) == sorted(LANE_PATHS),
      str(_absent_batches))
check("the deferral's own flag never leaks into the reply rows",
      all("needs_absent" not in r for r in _deferred["results"]),
      str(_deferred["results"][:1]))
check("the strip hears a frame per track, bracketed by 0/N and N/N",
      bool(_frames) and _frames[0] == (0, len(LANE_PATHS))
      and _frames[-1] == (len(LANE_PATHS), len(LANE_PATHS))
      and {d for d, _t in _frames} == set(range(len(LANE_PATHS) + 1)),
      str(_frames))

shutil.rmtree(TMP, ignore_errors=True)

if failures:
    print(f"\nlyrics kind: {len(failures)} check(s) FAILED")
    raise SystemExit(1)
print("lyrics kind: the payload field, the flags it folds and the wizard's "
      "scan — all assertions passed")
