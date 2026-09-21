#!/usr/bin/env python3
"""The configured script chain runs on EVERY acquisition path (user ask (d)).

What this pins, path by path:

  * the Soulseek auto-importer — a download the pipeline fetched by itself
  * the bulk queue and the sequential "import everything downloaded" queue
    (the one-click import route and the per-row Import button both end there)
  * an "Add to library" framework album the download lands in (organize adopts
    the folder the naming script created before the audio existed)
  * a watch-queued release (the watch's wish fills that same framework album)
  * a manual drag-and-drop import (the wizard's finish)

Every one of them must end in `imports.finish_album` running the CONFIGURED
chain exactly ONCE, on the folder the album actually ended at — the one the
organizer (and any chain script that renames) left it at. On top of that:

  * a path whose chain is switched off REPORTS that, instead of looking like an
    import whose scripts all ran;
  * a partially-found album still gets its whole chain, and its gaps still get
    the ONE prompt;
  * a chain that moves/renames the album mid-run does not leave the bookkeeping
    (the framework album's pending marker, the prompt's link) on the stale path;
  * the per-album result reaches the surfaces that report an import (the queue
    run's rows, the wish notification).

No network, no slskd, no real scripts: the chain's remote steps and
`script_runners.run_chain` itself are stubbed, and the music folder is a temp
directory. Run: python tools/test_chain_after_acquire.py   (exit 0 pass, 1 fail)
"""
import atexit
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --------------------------------------------------------------------------- #
# hermeticity: same idiom as tools/test_add_to_library.py — the config file and
# the app-state redirect are in place BEFORE server.* is imported, so the
# developer's real library is never read or written.
# --------------------------------------------------------------------------- #
REAL_MUSIC_FOLDER = ""
try:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        REAL_MUSIC_FOLDER = str((json.load(f) or {}).get("music_folder") or "")
except Exception:
    pass

REDIRECT = tempfile.mkdtemp(prefix="mlo-chain-redirect-")
MF = tempfile.mkdtemp(prefix="mlo-chain-music-")
# the download dir the pipeline really uses: inside the music folder (organize
# refuses a source outside it — the library's own guard)
DD = os.path.join(MF, ".mlo", "downloads")
os.makedirs(DD, exist_ok=True)
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
if REAL:
    assert not MF.replace("\\", "/").lower().startswith(REAL.lower()), \
        f"the temp fixture {MF} sits inside the real music folder {REAL}"

from mlo import flac as flac_mod  # noqa: E402
from server import artist_watch, import_autonomy, import_queue, imports  # noqa: E402
from server import main as mlo_main  # noqa: E402
from server import pending_albums, script_runners, soulseek, soulseek_auto  # noqa: E402
from server import wishes, wishes_worker  # noqa: E402
from server import events  # noqa: E402

LIB = os.path.join(MF, "Artists")
FAILED = []


def ok(cond, label, extra=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{f' — {extra}' if extra else ''}")
        FAILED.append(label)


def eq(got, want, label):
    ok(got == want, label, f"got {got!r}, want {want!r}")


def norm(p):
    return os.path.normpath(str(p or ""))


def fwd(p):
    return norm(p).replace("\\", "/")


# --------------------------------------------------------------------------- #
# the chain every path below runs: the Run All order, minus the ONE script it
# declares library-wide. The point of the suite is that an album gets the same
# scripts however it arrived, so the scripts Run All runs are asserted HERE, in
# the import's own terms: 16 Mood & Energy, 17 Lyrics transliterate (AI) and 19
# Optimize artist images used to run only from the menu, and a fourth script
# joining the run order must not go missing the same way.
# --------------------------------------------------------------------------- #
print("\n(0) the chain an import runs")
eq(imports.DEFAULT_CHAIN,
   [sid for sid in cfgmod.DEFAULT_RUN_ALL_ORDER
    if sid not in imports.LIBRARY_WIDE_SCRIPTS],
   "an import runs the Run All order, minus the library-wide scripts it declares")
eq([sid for sid in cfgmod.DEFAULT_RUN_ALL_ORDER if sid not in imports.DEFAULT_CHAIN],
   list(imports.LIBRARY_WIDE_SCRIPTS),
   "and the only script it leaves out is a declared one")
eq(imports.DEFAULT_CHAIN, imports.chain_for({}),
   "which is what an import with nothing configured runs")


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def make_wav(path, seconds=0.05):
    """A real (tiny) WAV — the grader reads the album's own audio."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0" * int(8000 * seconds))
    return path


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


def album_of(name, tracks=2, root=None):
    folder = os.path.join(root or os.path.join(REDIRECT, "staging"), name)
    for i in range(1, tracks + 1):
        make_wav(os.path.join(folder, f"{i:02d} - track.wav"))
    return folder


def release(n, title, media="CD"):
    """A MusicBrainz release shape, one per case (each needs its own folder)."""
    return {
        "id": str(n) * 8 + "-1111-1111-1111-111111111111",
        "title": title,
        "date": "2001-03-04",
        "originaldate": "2001",
        "country": "GB",
        "status": "Official",
        "medium": media,
        "label": "Test Label",
        "catalog_number": f"CAT-{n}",
        "release_group_id": str(n) * 8 + "-2222-2222-2222-222222222222",
        "release_type": "album",
        "primary_type": "Album",
        "secondary_types": [],
        "artists": [{"name": f"Test Artist {n}",
                     "mbid": str(n) * 8 + "-3333-3333-3333-333333333333"}],
        "medium_count": 1,
        "media": [
            {"disc": 1, "position": 1, "title": "One",
             "recording_mbid": str(n) * 8 + "-4444-4444-4444-444444444444",
             "artist_credit": f"Test Artist {n}"},
            {"disc": 1, "position": 2, "title": "Two",
             "recording_mbid": str(n) * 8 + "-5555-5555-5555-555555555555",
             "artist_credit": f"Test Artist {n}"},
        ],
    }


# The import chain, configured but never really run: `run_chain` is the seam
# this suite watches, and the remote steps (RYM, advisories, metadata, cover)
# are stubbed so nothing here touches the network.
CFG = {"music_folder": MF, "import_auto_scripts": True, "import_scripts": [4],
       "import_bulk_concurrency": 1, "rym_links_auto": False,
       "advisory_auto_fetch": False, "instrumental_auto_fetch": False,
       "metadata_auto_fetch": False, "cover_auto_fetch": False,
       "import_autonomy": "automatic", "import_review_families": [],
       "soulseek_download_dir": DD, "wishes_auto_import": True}
CFG_OFF = dict(CFG, import_auto_scripts=False)

imports.stamp_rym_links = lambda path, cfg=None: {
    "album": "", "artist": "", "note": "", "written": 0}
imports.fetch_advisories = lambda paths, cfg=None, progress=None: {
    "updated": 0, "values": {}}
imports.fetch_instrumentals = lambda paths, cfg=None: {
    "updated": 0, "values": [], "evidence": {}}
imports.run_metadata_step = lambda album_dir, cfg=None: {
    "note": "", "staged": False, "applied": {}}
# the cover/metadata steps answer to their OWN keys, not to the chain switch:
# a check below (and tools/test_add_to_library.py) leans on that
STAGING_CALLS = []


def _stub_cover_step(album_dir, cfg=None):
    STAGING_CALLS.append(norm(album_dir))
    return {"note": "", "staged": False, "fetched": 0, "source": ""}


imports.run_cover_step = _stub_cover_step

# What the chain did, and where: the point of the whole suite.
CHAIN_CALLS = []
CHAIN_MOVE = None       # callable(folder) — emulates beets/organize moving it
CHAIN_RAISE = None      # exception instance — emulates a chain that cannot start


def stub_run_chain(cfg, ids, targets=None, force=None, progress=None,
                   wait=False, timeout=None):
    folder = norm((targets or [""])[0])
    entry = {"ids": list(ids), "target": folder, "wait": bool(wait),
             "force": force, "chain": None}
    CHAIN_CALLS.append(entry)
    if CHAIN_RAISE is not None:
        raise CHAIN_RAISE
    results = []
    for i, sid in enumerate(ids, 1):
        results.append({"id": sid, "label": f"Script {sid}", "stats": {},
                        "error": None})
        if progress is not None:
            progress(i, len(ids), f"Script {sid}", results[-1])
    entry["chain"] = [r["id"] for r in results]
    if CHAIN_MOVE is not None:
        CHAIN_MOVE(folder)
    entry["returned"] = len(results)
    return results


script_runners.run_chain = stub_run_chain

# finish_album itself stays the real one — it is what every path must reach —
# but its result is recorded so a check can read `chained`/`chain_off`/`note`.
FINISH_RESULTS = []
_real_finish_album = imports.finish_album


def finish_recording(album_dir, cfg=None, progress=None, force=None, **kwargs):
    res = _real_finish_album(album_dir, cfg, progress=progress, force=force,
                             **kwargs)
    FINISH_RESULTS.append(res)
    return res


imports.finish_album = finish_recording


def calls_since(mark):
    return CHAIN_CALLS[mark:]


def results_since(mark):
    return FINISH_RESULTS[mark:]


def join_chain_threads(timeout=60):
    """The auto-importer stages in a daemon thread; wait for it to finish."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        live = [t for t in threading.enumerate()
                if t.name == "mlo-soulseek-import-chain"]
        if not live and time.time() > deadline - timeout + 0.3:
            return True
        for t in live:
            t.join(1.0)
        if not live:
            return True
    return False


# --------------------------------------------------------------------------- #
# (a) the Soulseek auto-importer
# --------------------------------------------------------------------------- #
print("\n(a) the Soulseek auto-importer")


def check_auto_download():
    src = os.path.join(DD, "Peer Album")
    make_wav(os.path.join(src, "01 - one.wav"))
    rel = release(1, "Auto Album")
    final = os.path.join(LIB, "Test Artist 1", "Auto Album (2001)")
    saves = {name: getattr(soulseek_auto, name) for name in
             ("_stamp_mb_tags", "_stamp_media", "_verify_acoustid",
              "_account_metadata")}
    real_convert = flac_mod.convert_album_lossless
    real_organize = mlo_main.organize

    def organize(req):
        """The organizer RENAMES the album into the naming-script layout — the
        path the chain must run on is the root it reports, not the one it was
        handed."""
        os.makedirs(os.path.dirname(final), exist_ok=True)
        shutil.move(req.paths[0], final)
        return {"results": [{"album_root": final, "path": final}]}

    soulseek_auto._stamp_mb_tags = lambda album_dir, release: 0
    soulseek_auto._stamp_media = lambda album_dir, media, cfg: (0, [])
    soulseek_auto._verify_acoustid = lambda album_dir, release, cfg: None
    soulseek_auto._account_metadata = lambda album_dir, cfg: {}
    flac_mod.convert_album_lossless = lambda album_dir, cfg: {"modified_count": 0}
    mlo_main.organize = organize
    mark, rmark = len(CHAIN_CALLS), len(FINISH_RESULTS)
    try:
        res = soulseek_auto._import(src, rel, CFG, "CD")
    finally:
        for name, fn in saves.items():
            setattr(soulseek_auto, name, fn)
        flac_mod.convert_album_lossless = real_convert
        mlo_main.organize = real_organize
    join_chain_threads()

    eq(res.get("album_path"), final, "the download landed in the organizer's folder")
    calls = calls_since(mark)
    eq(len(calls), 1, "the configured chain ran exactly once")
    if calls:
        eq(calls[0]["target"], norm(final),
           "and on the FINAL folder the organizer left the album at")
        eq(calls[0]["ids"], imports.chain_for(CFG), "with the configured ids")
        eq(calls[0]["wait"], True, "waiting for the library lock, as an import must")
    done = results_since(rmark)
    eq(len(done), 1, "the auto-import called finish_album exactly once")
    if done:
        ok(done[0]["chained"] is True, "and its result says the chain ran",
           done[0])
        ok(done[0]["note"].startswith("the script chain ran"),
           "with one honest line about it", done[0]["note"])


check_auto_download()

# --------------------------------------------------------------------------- #
# (b) the queue: bulk, the one-click route and the sequential runner
# --------------------------------------------------------------------------- #
print("\n(b) the bulk queue and the import queue")


def check_bulk_and_queue():
    src = album_of("Bulk Album")
    mark = len(CHAIN_CALLS)
    out = imports.bulk_import([{"path": src}], CFG)
    row = out["items"][0]
    eq(row["status"], "imported", "the bulk row reports the album imported")
    eq(row["chained"], True, "and carries the chain's own verdict on the row")
    ok(str(row.get("note") or "").startswith("the script chain ran"),
       "with the line the bulk queue reports", row.get("note"))
    calls = calls_since(mark)
    eq(len(calls), 1, "the chain ran exactly once for it")
    if calls:
        eq(calls[0]["target"], norm(row["album_path"].replace("/", os.sep)),
           "on the album's final (moved-into-the-library) folder")

    # the one-click "Import" route and the sequential queue runner both call
    # main._import_one_album; the queue's own rows are what the queue view reads
    album = album_of("Queued Album")
    final = os.path.join(LIB, "Test Artist 9", "Queued Album (2001)")
    real_organize = mlo_main.organize
    saves = {n: getattr(mlo_main, n) for n in
             ("_tag_media_for_albums", "_stamp_import_identity")}
    real_convert = flac_mod.convert_album_lossless

    def organize(req):
        os.makedirs(os.path.dirname(final), exist_ok=True)
        shutil.move(req.paths[0], final)
        return {"results": [{"album_root": final}]}

    mlo_main.organize = organize
    mlo_main._tag_media_for_albums = lambda albums: 0
    mlo_main._stamp_import_identity = lambda albums: 0
    flac_mod.convert_album_lossless = lambda album_dir, cfg: {"modified_count": 0}
    mark = len(CHAIN_CALLS)
    try:
        res = mlo_main._import_one_album(album, CFG, chain_async=False)
    finally:
        mlo_main.organize = real_organize
        for name, fn in saves.items():
            setattr(mlo_main, name, fn)
        flac_mod.convert_album_lossless = real_convert
    eq(res.get("album_root"), final, "the one-click import reports the organized root")
    calls = calls_since(mark)
    eq(len(calls), 1, "which is where it ran the chain, exactly once")
    if calls:
        eq(calls[0]["target"], norm(final), "on the final folder")
    ok(bool(res.get("chain") and res["chain"]["chained"]),
       "and returns the chain's result", res.get("chain"))

    # the sequential runner: what it keeps per album is what the queue view shows
    importer = lambda path: mlo_main._import_one_album(path, CFG, chain_async=False)
    album2 = album_of("Queue Row Album")
    final2 = os.path.join(LIB, "Test Artist 9", "Queue Row Album (2001)")
    mlo_main.organize = lambda req: _organize_to(req, final2)
    flac_mod.convert_album_lossless = lambda album_dir, cfg: {"modified_count": 0}
    mark = len(CHAIN_CALLS)
    import_queue.set_importer(importer)
    try:
        started = import_queue.start(paths=[album2])
        ok(started.get("ok") is True, "the sequential run starts", started)
        deadline = time.time() + 60
        while import_queue.running() and time.time() < deadline:
            time.sleep(0.05)
        st = import_queue.status()
    finally:
        import_queue.set_importer(lambda p: {})
        mlo_main.organize = real_organize
        flac_mod.convert_album_lossless = real_convert
    eq(len(calls_since(mark)), 1, "the queue run's album got the chain once")
    entry = (st.get("results") or [{}])[0]
    eq(entry.get("chained"), True, "and the queue row says the chain ran")
    ok(str(entry.get("note") or "").startswith("the script chain ran"),
       "with the line the queue view reports", entry.get("note"))
    eq([s.get("id") for s in (entry.get("scripts") or [])], [4],
       "and which scripts it ran")
    ok(entry.get("album_root") == final2, "on the final folder", entry)


def _organize_to(req, final):
    os.makedirs(os.path.dirname(final), exist_ok=True)
    shutil.move(req.paths[0], final)
    return {"results": [{"album_root": final}]}


check_bulk_and_queue()

# --------------------------------------------------------------------------- #
# (c) an "Add to library" framework album the download lands in
# --------------------------------------------------------------------------- #
print("\n(c) the framework album organize adopts")


def check_framework_adoption():
    from server import artcache

    real_fetch = artcache.fetch_art
    artcache.fetch_art = lambda url, **kw: (b"\xff\xd8\xff" + b"placeholder" * 4,
                                            "image/jpeg", "coverartarchive")
    try:
        row = pending_albums.create(release(3, "Adopt Add"), CFG)
    finally:
        artcache.fetch_art = real_fetch
    folder = norm(row["album_path"])
    ok(os.path.isdir(folder), "the framework album exists before the download")
    ok(bool(pathmod.load_pending(folder)), "and is pending")

    # the download arrives somewhere else and organize adopts the framework
    adopt = release(3, "Adopt Add")
    staging = os.path.join(DD, "a-peer", "Test Artist 3 - Adopt Add")
    write_tagged_wav(os.path.join(staging, "1-01 One.wav"), {
        "ALBUM": adopt["title"],
        "ALBUMARTIST": "Test Artist 3", "ARTIST": "Test Artist 3",
        "MUSICBRAINZ_ALBUMARTISTID": adopt["artists"][0]["mbid"],
        "MUSICBRAINZ_ARTISTID": adopt["artists"][0]["mbid"],
        "MUSICBRAINZ_ALBUMID": adopt["id"],
        "MUSICBRAINZ_RELEASEGROUPID": adopt["release_group_id"],
        "RELEASETYPE": "Album", "DATE": adopt["date"],
        "ORIGINALDATE": adopt["originaldate"],
        # a DIFFERENT medium from MusicBrainz's own "CD": the naming script then
        # names another folder than the framework album's — the case adoption
        # exists for
        "MEDIA": "Vinyl", "RELEASECOUNTRY": adopt["country"],
        "CATALOGNUMBER": adopt["catalog_number"], "LABEL": adopt["label"],
        "DISCNUMBER": "1", "TRACKNUMBER": "1", "TITLE": "One",
    })
    org = mlo_main.organize(mlo_main.OrganizeRequest(paths=[staging], dry_run=False))
    landed = norm((org.get("results") or [{}])[0].get("album_root") or "")
    eq(landed, folder, "the organizer landed the album IN the framework folder")
    ok(bool(pathmod.load_pending(folder)),
       "the marker is still there while the chain has not run")

    # the auto-importer's post-download seam, on the adopted folder
    mark, rmark = len(CHAIN_CALLS), len(FINISH_RESULTS)
    soulseek_auto._start_import_chain(folder, CFG)
    join_chain_threads()
    calls = calls_since(mark)
    eq(len(calls), 1, "the chain ran exactly once on the adopted folder")
    if calls:
        eq(calls[0]["target"], folder, "on the framework album's own folder")
    done = results_since(rmark)
    eq(len(done), 1, "finish_album was called once, with (folder, cfg) and nothing else")
    if done:
        ok(done[0]["chained"] is True, "its result says the chain ran", done[0])
    ok(not pathmod.load_pending(folder),
       "and only THEN is the framework album no longer pending")


check_framework_adoption()

# --------------------------------------------------------------------------- #
# (d) a watch-queued release
# --------------------------------------------------------------------------- #
print("\n(d) a release a watch queued")


def check_watch_queued():
    from server import artcache

    triggered = []
    real_trigger = wishes_worker.trigger
    wishes_worker.trigger = lambda wid=None: triggered.append(wid) or {"ok": True}
    real_fetch = artcache.fetch_art
    artcache.fetch_art = lambda url, **kw: (b"\xff\xd8\xff" + b"placeholder" * 4,
                                            "image/jpeg", "coverartarchive")
    try:
        row = artist_watch.queue_release(release(4, "Watched Album"), CFG)
    finally:
        wishes_worker.trigger = real_trigger
        artcache.fetch_art = real_fetch
    folder = norm(row["album_path"])
    ok(os.path.isdir(folder) and row.get("wish_id"),
       "the watch queued a framework album + wish through the one Add-to-library seam",
       row)
    eq(triggered, [row["wish_id"]], "and the existing wish worker is searching it")

    # the download lands in the framework album, and the job's post-download
    # seam runs the chain
    make_wav(os.path.join(folder, "01 - One.wav"))
    mark, rmark = len(CHAIN_CALLS), len(FINISH_RESULTS)
    soulseek_auto._start_import_chain(folder, CFG)
    join_chain_threads()
    eq(len(calls_since(mark)), 1, "the watched album's chain ran exactly once")
    if calls_since(mark):
        eq(calls_since(mark)[0]["target"], folder, "on the folder the audio landed in")
    done = results_since(rmark)
    ok(bool(done) and done[0]["chained"] is True,
       "with the chain's result reported", done[-1] if done else None)
    ok(not pathmod.load_pending(folder), "and the pending marker is gone")

    # …and the wish that filled it reports the chain in its notification
    emitted = []
    real_emit = events.emit
    events.emit = lambda kind, title, body, data=None, **kw: emitted.append(
        {"kind": kind, "title": title, "body": body, "data": data or {}})
    saves = {n: getattr(soulseek, n) for n in ("is_running", "web_up", "server_state")}
    real_start = soulseek_auto.start_job
    real_state = soulseek_auto.job_state
    from server import integrations as intg
    real_resolve = intg.resolve_release
    rel = release(4, "Watched Album")
    soulseek.is_running = lambda: True
    soulseek.server_state = lambda cfg=None: {"isLoggedIn": True}
    soulseek_auto.start_job = lambda **kw: {"ok": True, "job": {"id": 4242}}
    soulseek_auto.job_state = lambda jid=None: {
        "state": "done",
        "result": {"album_path": folder, "imported": True,
                   "chain": done[-1] if done else {}}}
    intg.resolve_release = lambda mbid: (rel, rel["id"])
    try:
        wish = wishes.get_wish(row["wish_id"])
        outcome = wishes_worker._run_one(wish, CFG)
    finally:
        events.emit = real_emit
        for name, fn in saves.items():
            setattr(soulseek, name, fn)
        soulseek_auto.start_job = real_start
        soulseek_auto.job_state = real_state
        intg.resolve_release = real_resolve
    eq(outcome, "imported", "the wish settled as imported")
    found = [e for e in emitted if e["kind"] == "wish_found"]
    eq(len(found), 1, "and announced it once")
    if found:
        ok("the script chain ran" in str(found[0]["body"]),
           "with what the chain did in the notification body", found[0]["body"])
        ok(str(found[0]["data"].get("chain") or "").startswith("the script chain ran"),
           "and on the notification payload", found[0]["data"])


check_watch_queued()

# --------------------------------------------------------------------------- #
# (e) a manual drag-and-drop import
# --------------------------------------------------------------------------- #
print("\n(e) a manual (drag-and-drop) import")


def check_manual_import():
    # the wizard's finish route is exactly this one call
    folder = album_of("Dropped Album")
    mark, rmark = len(CHAIN_CALLS), len(FINISH_RESULTS)
    res = imports.finish_album(folder, CFG)
    eq(len(calls_since(mark)), 1, "the chain ran exactly once on the dropped album")
    if calls_since(mark):
        eq(calls_since(mark)[0]["target"], norm(folder), "on the folder it was handed")
    eq(results_since(rmark) and results_since(rmark)[0]["chained"], True,
       "and the result reports the chain ran")


check_manual_import()

# --------------------------------------------------------------------------- #
# (f) a switched-off chain is reported, not hidden
# --------------------------------------------------------------------------- #
print("\n(f) the chain switched off in config")


def check_chain_off():
    folder = album_of("No Chain Album")
    mark = len(CHAIN_CALLS)
    smark = len(STAGING_CALLS)
    res = imports.finish_album(folder, CFG_OFF)
    eq(len(calls_since(mark)), 0, "no chain was run — import_auto_scripts is off")
    eq(STAGING_CALLS[smark:], [norm(folder)],
       "while the cover/metadata staging still ran: those are their own switches")
    eq(res["chain_off"], True, "and the result says so")
    eq(res["chained"], False, "it does not claim the chain ran")
    ok("import_auto_scripts is off" in res["note"],
       "the note names the switch", res["note"])
    eq(imports.chain_summary(res), res["note"], "chain_summary is that same line")

    out = imports.bulk_import([{"path": album_of("No Chain Bulk")}], CFG_OFF)
    row = out["items"][0]
    eq(row["status"], "imported", "a bulk row with the chain off still imports")
    eq(row["chain_off"], True, "and says the chain was off")
    ok("import_auto_scripts is off" in str(row.get("note") or ""),
       "in the row's own words", row.get("note"))

    # a framework album whose chain is off ends its pending state (no chain is
    # coming to end it later) — and the result says why
    from server import artcache
    real_fetch = artcache.fetch_art
    artcache.fetch_art = lambda url, **kw: (b"\xff\xd8\xff" + b"placeholder" * 4,
                                            "image/jpeg", "coverartarchive")
    try:
        row = pending_albums.create(release(6, "No Chain Pending"), CFG_OFF)
    finally:
        artcache.fetch_art = real_fetch
    folder = norm(row["album_path"])
    make_wav(os.path.join(folder, "01 - One.wav"))
    res = imports.finish_album(folder, CFG_OFF)
    ok(not pathmod.load_pending(folder),
       "a chain-off import still ends the framework album's pending state")
    ok("import_auto_scripts is off" in res["note"],
       "while saying the chain did not run", res["note"])


check_chain_off()

# --------------------------------------------------------------------------- #
# (g) a partially-found album: chain runs, gaps still get ONE prompt
# --------------------------------------------------------------------------- #
print("\n(g) a partially-found album")


def check_partial_album():
    folder = album_of("Partial Album")
    import_autonomy.clear(folder, CFG)
    mark = len(CHAIN_CALLS)
    res = imports.finish_album(folder, CFG)
    eq(len(calls_since(mark)), 1, "the whole chain still ran for it")
    eq(res["chained"], True, "and says so")
    ok(bool(res["autonomy"]["missing"]),
       "while reporting what no source supplied", res.get("autonomy"))
    ok(res["autonomy"]["prompt"] is not None, "raising the one prompt")
    mine = [p for p in import_autonomy.prompts(CFG) if p["album"] == fwd(folder)]
    eq(len(mine), 1, "which the wizard lists once for this album")
    if mine:
        eq(mine[0]["mode"], "automatic", "in the import's own mode")
        ok("step=" in str(mine[0]["link"]), "with a wizard link", mine[0]["link"])

    # importing it again refreshes that ONE entry rather than stacking another
    finish_recording(folder, CFG)
    mine = [p for p in import_autonomy.prompts(CFG) if p["album"] == fwd(folder)]
    eq(len(mine), 1, "a second import keeps it at one prompt per album")
    import_autonomy.clear(folder, CFG)


check_partial_album()

# --------------------------------------------------------------------------- #
# (h) the marker rules when the chain did not run
# --------------------------------------------------------------------------- #
print("\n(h) a chain that could not run")


def check_chain_busy():
    from server import artcache
    real_fetch = artcache.fetch_art
    artcache.fetch_art = lambda url, **kw: (b"\xff\xd8\xff" + b"placeholder" * 4,
                                            "image/jpeg", "coverartarchive")
    try:
        row = pending_albums.create(release(7, "Busy Chain"), CFG)
    finally:
        artcache.fetch_art = real_fetch
    folder = norm(row["album_path"])
    make_wav(os.path.join(folder, "01 - One.wav"))
    global CHAIN_RAISE
    CHAIN_RAISE = script_runners.RunBusy("the library lock is held by a UI run")
    try:
        res = imports.finish_album(folder, CFG)
    finally:
        CHAIN_RAISE = None
    eq(res["chained"], False, "a chain that could not start is not 'chained'")
    eq(res["chain_off"], False, "and it is not 'off' either")
    ok(res["errors"], "the reason is reported", res)
    ok(bool(pathmod.load_pending(folder)),
       "the framework album STAYS pending: a configured chain has not run")


check_chain_busy()

# --------------------------------------------------------------------------- #
# (i) a chain that moves the album mid-run
# --------------------------------------------------------------------------- #
print("\n(i) the album moves (and its extension changes) mid-chain")


def check_midchain_move():
    from server import artcache, mbresolve
    real_fetch = artcache.fetch_art
    artcache.fetch_art = lambda url, **kw: (b"\xff\xd8\xff" + b"placeholder" * 4,
                                            "image/jpeg", "coverartarchive")
    try:
        row = pending_albums.create(release(8, "Moved Mid"), CFG)
    finally:
        artcache.fetch_art = real_fetch
    before = norm(row["album_path"])
    # the album's own MusicBrainz tags: they are what `_resolve_moved_album`
    # follows when the chain has renamed the folder out from under the import
    # (an album the auto path imported carries them — the importer stamps them
    # before the chain runs)
    rel = release(8, "Moved Mid")
    write_tagged_wav(os.path.join(before, "01 - One.wav"), {
        "ALBUM": rel["title"], "ALBUMARTIST": "Test Artist 8",
        "MUSICBRAINZ_ALBUMID": rel["id"],
        "MUSICBRAINZ_RELEASEGROUPID": rel["release_group_id"],
    })
    after = os.path.join(LIB, "Test Artist 8", "Moved Mid (2001)")

    def move(folder):
        """What script 14 (beets, `move: yes`) does: renames the album folder —
        and the converter changes the extension inside it."""
        if not os.path.isdir(folder):
            return
        os.makedirs(os.path.dirname(after), exist_ok=True)
        shutil.move(folder, after)
        wav = os.path.join(after, "01 - One.wav")
        if os.path.isfile(wav):
            with wave.open(wav) as w:
                frames = w.readframes(w.getnframes())
                params = w.getparams()
            os.remove(wav)
            with wave.open(os.path.join(after, "01 - One.flac"), "wb") as w:
                w.setnchannels(params.nchannels)
                w.setsampwidth(params.sampwidth)
                w.setframerate(params.framerate)
                w.writeframes(frames)

    global CHAIN_MOVE
    real_heal = mbresolve.heal_row
    mbresolve.heal_row = lambda kind, path, ref: after
    mark = len(CHAIN_CALLS)
    CHAIN_MOVE = move
    try:
        res = imports.finish_album(before, CFG)
    finally:
        CHAIN_MOVE = None
        mbresolve.heal_row = real_heal
    calls = calls_since(mark)
    eq(len(calls), 1, "the chain ran exactly once, and was NOT re-run on a stale path")
    if calls:
        eq(calls[0]["target"], before,
           "it was handed the folder as it was when the run started")
    eq(res["path"], norm(after),
       "and the import reports the folder the album ended at")
    ok(not os.path.isdir(before), "the folder it was handed is gone")
    ok(not pathmod.load_pending(after),
       "the pending marker (which moved WITH the album) is cleared there")
    ok(not pathmod.load_pending(before),
       "and nothing was left pending behind on the stale path")
    prompt = import_autonomy.for_album(after, CFG)
    if res.get("autonomy", {}).get("prompt"):
        eq(prompt.get("album"), fwd(after),
           "the prompt it raised points at the FINAL folder")
    else:
        ok(True, "no prompt was needed for the moved album")
    import_autonomy.clear(after, CFG)


check_midchain_move()

# --------------------------------------------------------------------------- #
print()
if FAILED:
    print(f"chain after acquire: {len(FAILED)} FAILED")
    for label in FAILED:
        print(f"  - {label}")
    sys.exit(1)
print("chain after acquire: all assertions passed")
