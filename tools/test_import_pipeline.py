#!/usr/bin/env python3
"""Import pipeline contract (server.imports + server.script_runners).

What an import must guarantee end to end:

  * the chain is the configured one — built-in by default, an explicit
    `import_scripts` list when set (junk ids dropped), nothing at all with
    `import_auto_scripts` off;
  * a script that blows up is reported and the chain carries on — one bad
    script may never cost the rest of the import;
  * the bulk queue moves staging albums into `<music>/Artists`, reports
    ok/failed/skipped, re-runs without duplicating an album that is already in
    the library, and is pollable through job_state();
  * AcoustID says WHY it is unusable instead of pretending the audio matched;
  * progress reaches both the caller's callback and mlo.stats.progress_hook
    (the websocket relay's source);
  * an import that arrives carrying a peer's lyrics, genre, rating and art
    REPLACES all four with what it finds (the drop, then the writers), asserted
    end to end through `finish_album` for the lyric fetch (script 13), the
    release's genres and the advisory ladder;
  * a config that switches one of those three families OFF (a saved chain
    without script 13, `genre_autofill` off, `advisory_auto_fetch` off) says so
    in the import's own result and its one-line note instead of quietly
    importing without it — and the shipped defaults skip none of them.

No network, no real scripts: the chain is switched off or
monkeypatched everywhere a real run would happen.

Run:  python tools/test_import_pipeline.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo import paths as mlo_paths
from mlo.config import DEFAULT_RUN_ALL_ORDER, load_config
from server import imports, script_runners

ROOT = tempfile.mkdtemp(prefix="mlo_import_pipeline_")
MF = os.path.join(ROOT, "music")
STAGING = os.path.join(ROOT, "staging")


def make_wav(path, seconds=0.05):
    """A real (tiny) WAV — the album scanner must see actual audio."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0" * int(8000 * seconds))
    return path


def staging_album(name, tracks=2):
    for i in range(1, tracks + 1):
        make_wav(os.path.join(STAGING, name, f"{i:02d} - track.wav"))
    return os.path.join(STAGING, name)


os.makedirs(MF)
LIB = os.path.join(MF, "Artists")
# chain off: these albums must be moved and reported, not processed
CFG = {"music_folder": MF, "import_auto_scripts": False, "import_scripts": [],
       "import_bulk_concurrency": 2, "advisory_auto_fetch": True,
       # the network steps an import reaches whatever the chain does (the
       # artist image / descriptions and the cover art): this suite runs
       # offline, and tools/test_add_to_library.py covers them stubbed
       "metadata_auto_fetch": False, "cover_auto_fetch": False,
       # the RateYourMusic link step is a network lookup too: this suite runs
       # offline (tools/test_rym_links.py covers it with stubbed HTTP)
       "rym_links_auto": False}

# --------------------------------------------------------------------------- #
# The chain a config describes
# --------------------------------------------------------------------------- #
# Path-changing scripts first (11 videos → 3 FLACs → 14 beets, which moves),
# then the sidecar namers that must see final audio names (15 manifest → 2
# CUEs → 1 lyrics format), then content, then 10 Format all, then 4 Grade.
# The chain IS the Run All order — one list, in `mlo.config` — minus the
# library-wide scripts it declares (`LIBRARY_WIDE_SCRIPTS`): a script added to
# Run All can never be silently missing from the import path again. Three were
# (16 Mood & Energy, 17 Lyrics transliterate (AI), 19 Optimize artist images:
# Run All ran them, an import never did), which is what this assertion now
# catches — and nothing is left out today: 20 (Optimize library layout) was the one
# declared exception while its runner ignored `targets` and re-walked the whole
# library per album, and it now scopes itself to the album it is handed.
assert imports.DEFAULT_CHAIN == [sid for sid in DEFAULT_RUN_ALL_ORDER
                                 if sid not in imports.LIBRARY_WIDE_SCRIPTS], \
    imports.DEFAULT_CHAIN
assert imports.LIBRARY_WIDE_SCRIPTS == (), imports.LIBRARY_WIDE_SCRIPTS
assert set(DEFAULT_RUN_ALL_ORDER) - set(imports.DEFAULT_CHAIN) == set(), \
    set(DEFAULT_RUN_ALL_ORDER) - set(imports.DEFAULT_CHAIN)
# 20 fixes the album's layout inside the chain: after beets (14) has put the
# folder where the naming script wants it, before the grade (4) reads it.
assert 20 in imports.DEFAULT_CHAIN, imports.DEFAULT_CHAIN
assert imports.DEFAULT_CHAIN.index(14) < imports.DEFAULT_CHAIN.index(20) \
    < imports.DEFAULT_CHAIN.index(4), imports.DEFAULT_CHAIN
for _sid in (16, 17, 19):
    assert _sid in imports.DEFAULT_CHAIN, \
        f"script {_sid} must be reached by an import, not only by Run All"
assert imports.chain_for({}) == imports.DEFAULT_CHAIN
assert imports.chain_for({"import_auto_scripts": True, "import_scripts": []}) \
    == imports.DEFAULT_CHAIN
# an explicit list replaces it: junk ids dropped, duplicates dropped, order kept
# (17 is a real script since the AI transforms returned, so it is KEPT; 99 / 0
# / -3 are the junk)
assert imports.chain_for({"import_scripts": [4, 99, 4, 0, 17, -3, 3]}) == [4, 17, 3]
# the settings field can be the human-typed "4, 3;3" form
assert imports.chain_for({"import_scripts": "4, 3;3"}) == [4, 3]
assert imports.chain_for({"import_auto_scripts": False, "import_scripts": [1]}) == []
assert imports.chain_for({"import_auto_scripts": False}) == []

# --------------------------------------------------------------------------- #
# Registry + one script
# --------------------------------------------------------------------------- #
assert sorted(script_runners.RUNNERS) == list(range(1, 25)), sorted(script_runners.RUNNERS)
assert script_runners.RUNNERS[2][0] == "Format CUEs", script_runners.RUNNERS[2]
assert script_runners.RUNNERS[2][1].__name__ == "run_format_cues", script_runners.RUNNERS[2]
assert all(label for label, _ in script_runners.RUNNERS.values())

# an unknown id is a result, not a raise
assert script_runners.run_script(99, dict(CFG))["error"]
assert script_runners.run_script(0, dict(CFG))["id"] == 0

_REAL_RUNNERS = dict(script_runners.RUNNERS)
_calls = []


def _boom(cfg):
    raise RuntimeError("no ffmpeg on this machine")


def _fine(cfg):
    _calls.append(list(cfg.get("targets") or []))
    return {"modified_count": 1}


script_runners.RUNNERS.update({3: ("Optimize FLACs", _boom), 5: ("Process images", _fine)})
try:
    # a supplied force dict is authoritative — on OR off
    cfg = {"music_folder": MF, "force_reencode_images": False}
    assert script_runners.run_script(5, cfg, force={"5": True})["stats"] == {"modified_count": 1}
    assert cfg["force_reencode_images"] is True, cfg
    cfg = {"music_folder": MF, "force_reencode_images": True}
    assert script_runners.run_script(5, cfg, force={"images": False})["stats"] == {"modified_count": 1}
    assert cfg["force_reencode_images"] is False, cfg
    cfg = {"music_folder": MF, "force_reencode_images": True}
    script_runners.run_script(5, cfg, force=None)
    assert cfg["force_reencode_images"] is True, cfg      # saved Settings survive

    # the chain continues past a failure and reports both
    seen = []
    _calls.clear()
    album = staging_album("Chain Album")
    chain_cfg = {"music_folder": MF}
    results = script_runners.run_chain(
        chain_cfg, [3, 5], targets=[album], force=None,
        progress=lambda done, total, label, res: seen.append((done, total, label, res)))
    assert [r["id"] for r in results] == [3, 5], results
    assert "no ffmpeg on this machine" in results[0]["error"], results[0]
    assert results[1]["stats"] == {"modified_count": 1}, results[1]
    assert _calls == [[os.path.normpath(album)]], _calls
    assert [s[:3] for s in seen] == [(1, 2, "Optimize FLACs"), (2, 2, "Process images")], seen
    # run_chain works on a copy: the caller's cfg keeps whatever it had
    assert "targets" not in chain_cfg, chain_cfg
finally:
    script_runners.RUNNERS.clear()
    script_runners.RUNNERS.update(_REAL_RUNNERS)

# --------------------------------------------------------------------------- #
# finish_album: the single post-import call
# --------------------------------------------------------------------------- #
# With no chain configured the result must SAY so — `chain_off` plus one honest
# line — rather than look like an import whose scripts all ran.
empty = imports.finish_album(album, CFG)
assert empty["path"] == os.path.normpath(album), empty
assert empty["scripts"] == [] and empty["chain"] == [] and empty["errors"] == [], empty
assert empty["chain_off"] is True and empty["chained"] is False, empty
assert "import_auto_scripts is off" in empty["note"], empty
assert imports.chain_summary(empty) == empty["note"], empty
missing = imports.finish_album(os.path.join(ROOT, "nope"), {"import_scripts": [4]})
assert missing["chain"] == [4] and missing["errors"] == ["album folder not found"], missing
assert missing["chain_off"] is False and missing["chained"] is False, missing
# a result that says nothing about a chain claims nothing about one
assert imports.chain_summary({"path": album}) == "", imports.chain_summary({"path": album})

# --------------------------------------------------------------------------- #
# EVERY path runs the chain on the album — the auto-import included
# --------------------------------------------------------------------------- #
# There is no "stage and place, no tagging" flavour any more: that flavour is
# what left auto-downloaded files without their optimization/tagging pass. The
# same call stages the album (RYM links, metadata, cover) and then runs the
# configured chain over the folder the organizer left it at.
DF_CFG = {"music_folder": MF, "import_scripts": [4, 3],
          "advisory_auto_fetch": True, "instrumental_auto_fetch": True,
          "rym_links_auto": True}
assert imports.chain_for(DF_CFG) == [4, 3], imports.chain_for(DF_CFG)

defer_album = staging_album("Defer Album")
DF_PATH = os.path.normpath(defer_album)
_seen = {}
_real_steps = {n: getattr(imports, n) for n in
               ("stamp_rym_links", "fetch_advisories", "fetch_instrumentals",
                "run_metadata_step", "run_cover_step", "_stamp_release")}
_real_run_chain = script_runners.run_chain


def _spy(name, value):
    def fn(*a, **k):
        _seen.setdefault(name, []).append((a, k))
        return value
    return fn


try:
    imports.stamp_rym_links = _spy("rym", {"album": None, "artist": None,
                                           "note": "", "written": 0})
    imports.fetch_advisories = _spy("advisory", {})
    imports.fetch_instrumentals = _spy("instrumental", {})
    imports.run_metadata_step = _spy("metadata", {})
    imports.run_cover_step = _spy("cover", {})
    imports._stamp_release = _spy("genres", (0, 0))
    script_runners.run_chain = _spy("chain", [])

    full = imports.finish_album(defer_album, DF_CFG)
    default_seen = {k: len(v) for k, v in _seen.items()}
    default_args = {k: list(v) for k, v in _seen.items()}
finally:
    for _n, _fn in _real_steps.items():
        setattr(imports, _n, _fn)
    script_runners.run_chain = _real_run_chain

# the staging steps, both tag-writing fetches AND the chain: once each. The
# genres family is NOT among them here, and that is the contract: its one
# action is release-driven, and this fixture's files carry no MusicBrainz
# identity to resolve a release from (tools/test_autonomous_import.py's album
# has one and asserts the step runs there).
assert default_seen == {"rym": 1, "metadata": 1, "cover": 1,
                        "advisory": 1, "instrumental": 1, "chain": 1}, default_seen
assert default_args["rym"][0][0] == (DF_PATH, DF_CFG), default_args["rym"]
assert default_args["metadata"][0][0] == (DF_PATH, DF_CFG), default_args["metadata"]
assert default_args["chain"][0][0][1] == [4, 3], default_args["chain"]
assert default_args["chain"][0][1]["targets"] == [DF_PATH], default_args["chain"]
assert full["chain"] == [4, 3] and full["chained"] is True, full
assert full["chain_off"] is False and full["scripts"] == [], full
# the chain was configured but reported no results: the note says exactly that
# — and, since this saved chain does not carry script 13, the lyrics it was
# therefore configured not to fetch ride along (see the skip case below)
assert full["note"] == ("the script chain did not run — " + imports.SKIPPED_LYRICS), full
assert full["skipped_families"] == [imports.SKIPPED_LYRICS], full["skipped_families"]
assert imports.chain_summary(full) == full["note"], full

# --------------------------------------------------------------------------- #
# AcoustID: unusable says why, and says nothing about matching
# --------------------------------------------------------------------------- #
res = imports.acoustid_match([album], {"acoustid_enabled": False})
assert res["available"] is False and res["albums"] == [], res
assert res["note"] == "AcoustID disabled in settings", res
res = imports.acoustid_match([album], {"acoustid_enabled": True, "acoustid_api_key": ""})
assert res["available"] is False and res["note"] == "no API key", res

# a key but no fpcalc is "unavailable" too — never a lookup, never a crash
from mlo import acoustid as _acoustid
_real_fpcalc, _real_available = _acoustid.fpcalc_path, _acoustid.available
_acoustid.fpcalc_path = lambda cfg=None: None
try:
    res = imports.acoustid_match([album], {"acoustid_enabled": True,
                                           "acoustid_api_key": "k"})
    assert res["available"] is False and res["note"] == "fpcalc not installed", res
finally:
    _acoustid.fpcalc_path, _acoustid.available = _real_fpcalc, _real_available

# ...and the earlier conflict pass is the one that named both release groups
assert imports.release_group_mismatch(
    {"release_group_id": "rg-OTHER", "release_group_title": "Other Pressing",
     "matched": 9, "total": 9}, "rg-WANT").find("rg-OTHER") >= 0
assert imports.release_group_mismatch(
    {"release_group_id": "rg-OTHER", "conflicts": [
        {"kind": "artist", "reason": "the audio is by A but the tags say B"}]},
    "rg-OTHER").strip().startswith("the audio is by A")
# a mismatch is a warning string, never an exception
assert imports.release_group_mismatch({"release_group_id": "abc"}, "abc") == ""
assert "abc" in imports.release_group_mismatch({"release_group_id": "abc"}, "xyz")
assert imports.release_group_mismatch({}, "xyz") == ""

# --------------------------------------------------------------------------- #
# Bulk queue: staging -> library, idempotent, pollable
# --------------------------------------------------------------------------- #
a1, a2 = staging_album("Album One"), staging_album("Album Two")
progress_rows, hook_rows = [], []

from mlo import stats as _stats
_prev_hook = getattr(_stats, "progress_hook", None)


def _record_hook(done, total, desc, steps=None):
    """The relay the header bar — and the wizard's progress strip — is drawn
    from. Four arguments: a chained run's frames carry the whole-step pair, and
    a hook that takes only three loses it silently (`job_locks.publish` retries
    the 3-argument frame on a TypeError)."""
    hook_rows.append((done, total, desc, steps))


_stats.progress_hook = _record_hook
try:
    out = imports.bulk_import(
        [{"path": a1}, {"path": a2}], CFG,
        progress=lambda done, total, label, row: progress_rows.append((done, total, label, row)))
finally:
    _stats.progress_hook = _prev_hook

assert out["total"] == 2 and out["ok"] == 2 and out["failed"] == 0, out
assert out["skipped"] == 0, out
assert [r["status"] for r in out["items"]] == ["imported", "imported"], out["items"]
assert [r["path"] for r in out["items"]] == [os.path.normpath(a1), os.path.normpath(a2)]
for row, src in zip(out["items"], (a1, a2)):
    assert os.path.isdir(row["album_path"]), row
    assert row["album_path"] == os.path.join(LIB, os.path.basename(src)).replace("\\", "/"), row
    assert not os.path.exists(src), f"staging folder left behind: {src}"
    assert len(os.listdir(row["album_path"])) == 2, row
assert sorted(os.listdir(LIB)) == ["Album One", "Album Two"], os.listdir(LIB)
assert len(progress_rows) == 2 and sorted(r[0] for r in progress_rows) == [1, 2], progress_rows
assert all(r[1] == 2 for r in progress_rows), progress_rows
assert all(r[3]["status"] == "imported" for r in progress_rows), progress_rows
# The hook was DRIVEN — and by WHAT is the check, never by how many frames
# arrived. `bulk_import` runs `import_bulk_concurrency` albums at once (2
# here), and the chain's pre-work announces itself phase by phase
# (`imports._phase`: what an import shows while its chain is still starting),
# so the two albums' frames interleave: every phase is announced ONCE PER
# ALBUM, and each phase's first appearance comes after the phase before it —
# the order the import works in. A row COUNT is satisfied by any producer that
# happens to publish as many frames (and broken by one that publishes one
# more), which is not what this line is here to say.
# The metadata and cover steps announce themselves together now: they write
# FILES (the review record, the art) while the four tag steps write TAGS, so
# the pair runs beside them — started after the genre step (which settles the
# identity its lookup reads) and joined before the chain. One frame for the
# pair, because that is what it is: two steps on one worker, in their own
# order (`imports._files_step`).
# A phase frame says what this import is DOING, so an import with nothing to do
# publishes none: this fixture's config runs no chain and switches every network
# step off (links, metadata, cover art, advisories, instrumentals), and the
# albums carry no MusicBrainz identity for the release-identity or genre steps
# to work from — so there is no step to announce, and the strip must not be told
# that there is (the owner's report: a step that is a no-op for this album, or a
# family that is switched off, must not be published at all). The exact
# sequences a fixture that DOES have work publishes are pinned in the phase
# section at the end of this file.
PHASE_TEXTS = ("Looking up links…", "Stamping the release's identity…",
               "Fetching genres…", "Settling the digital release…",
               "Fetching metadata and cover art…", "Fetching advisories…",
               "Checking instrumentals…")
phase_rows = [row for row in hook_rows if row[2] in PHASE_TEXTS]
assert phase_rows == [], phase_rows

# re-running on the album that is already in the library is a no-op move
again = imports.bulk_import([{"path": out["items"][0]["album_path"]}], CFG)
assert again["ok"] == 1 and again["items"][0]["status"] == "imported", again
assert again["items"][0]["album_path"] == out["items"][0]["album_path"], again
assert sorted(os.listdir(LIB)) == ["Album One", "Album Two"], os.listdir(LIB)
assert len(os.listdir(out["items"][0]["album_path"])) == 2, "duplicate bytes"
# an explicit move on a library path must not duplicate the album either
moved_again = imports.bulk_import([{"path": out["items"][0]["album_path"], "move": True}], CFG)
assert moved_again["items"][0]["album_path"] == out["items"][0]["album_path"], moved_again
assert sorted(os.listdir(LIB)) == ["Album One", "Album Two"], os.listdir(LIB)

# nothing to import -> skipped, not a bogus album folder
empty_dir = os.path.join(STAGING, "Empty Folder")
os.makedirs(empty_dir)
skipped = imports.bulk_import([{"path": empty_dir}, {"path": os.path.join(ROOT, "ghost")}], CFG)
assert skipped["ok"] == 0 and skipped["skipped"] == 1 and skipped["failed"] == 1, skipped
assert skipped["items"][0]["status"] == "skipped", skipped["items"][0]
assert skipped["items"][1]["error"] == "path not found", skipped["items"][1]

# the library root itself and the app's own state dir are refused: moving
# either into <music>/Artists would take the whole library (or its config,
# playlists and trash) with it
state_dir = os.path.join(MF, ".mlo", "data")
os.makedirs(state_dir, exist_ok=True)
with open(os.path.join(state_dir, "config.json"), "w", encoding="utf-8") as fh:
    fh.write("{}")
protected = imports.bulk_import(
    [{"path": CFG["music_folder"]}, {"path": state_dir}], CFG)
assert protected["ok"] == 0 and protected["failed"] == 2, protected
assert all("refusing" in (r["error"] or "") for r in protected["items"]), protected["items"]
assert os.path.isdir(LIB), "the library must be untouched by the refused items"
assert sorted(os.listdir(LIB)) == ["Album One", "Album Two"], os.listdir(LIB)

# --------------------------------------------------------------------------- #
# start_bulk + job_state: MANY at once, each answering for itself
# --------------------------------------------------------------------------- #
# The single-slot rule ("bulk import already running") is gone: a batch started
# from the Library's Import button must not wait behind the wizard's queue, or
# the reverse. What stays exclusive is the ALBUM (`_bulk_one` refuses a path an
# import already chains — pinned at the end of this file), and the pipelines
# every job runs share ONE budget, so three batches are not three machines.
assert imports.job_state()["status"] == "idle", imports.job_state()

gate = threading.Event()
_real_finish = imports.finish_album


def _slow_finish(album_dir, cfg=None, progress=None, force=None, **kwargs):
    # The seam takes whatever the importer passes (`release` today, whatever it
    # is tomorrow): a fake that fixed the signature would turn a NEW keyword
    # into a TypeError inside the bulk thread — which surfaces as the row never
    # reaching "imported", not as a missing argument — and only when the thread
    # happens to call it while this stub is installed. The other seam stubs
    # (`test_job_locks`, `test_chain_after_acquire`) take `**kwargs` for the
    # same reason.
    gate.wait(10)
    return {"path": os.path.normpath(album_dir), "scripts": [], "chain": [], "errors": []}


def _wait_job(job_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = imports.job_state(job_id)
        if state["status"] != "running":
            return state
        time.sleep(0.05)
    return imports.job_state(job_id)


job_album = staging_album("Job Album")
other_album = staging_album("Other Job Album")
imports.finish_album = _slow_finish
try:
    first = imports.start_bulk([{"path": job_album}], CFG)
    assert first["ok"] is True, first
    assert first["job"]["status"] == "running" and first["job"]["kind"] == "bulk", first
    assert first["job"]["total"] == 1 and first["job"]["id"], first
    second = imports.start_bulk([{"path": other_album}], CFG)
    assert second["ok"] is True, second
    assert second["job"]["id"] != first["job"]["id"], (first, second)
    # Every payload says what else is running — the surfaces that start a batch
    # away from the wizard name that ("N other batches running").
    assert {j["id"] for j in second["job"]["jobs"]} >= {first["job"]["id"], second["job"]["id"]}, second["job"]
    # Each job answers for its own albums while the other is still going, and an
    # id nobody started answers the idle shape the route always had.
    assert imports.job_state(first["job"]["id"])["total"] == 1, imports.job_state(first["job"]["id"])
    assert imports.job_state(second["job"]["id"])["total"] == 1, imports.job_state(second["job"]["id"])
    assert imports.job_state("nonexistent")["status"] == "idle", imports.job_state("nonexistent")
    # The pool is the PROCESS's, not the job's: the same width hands back the
    # same pool, which is what makes the concurrency a budget and not a
    # multiplier.
    assert imports._import_pool(4) is imports._import_pool(4)
finally:
    gate.set()
    imports.finish_album = _real_finish

state = _wait_job(first["job"]["id"])
assert state["status"] == "done", state
assert state["done"] == 1 and state["total"] == 1, state
assert state["finished"] and state["error"] is None, state
assert [i["status"] for i in state["items"]] == ["imported"], state
assert state["items"][0]["album_path"].endswith("Job Album"), state["items"][0]
assert os.path.isdir(os.path.join(LIB, "Job Album")), os.listdir(LIB)

second_state = _wait_job(second["job"]["id"])
assert second_state["status"] == "done", second_state
assert [i["status"] for i in second_state["items"]] == ["imported"], second_state
assert second_state["items"][0]["album_path"].endswith("Other Job Album"), second_state["items"][0]
assert os.path.isdir(os.path.join(LIB, "Other Job Album")), os.listdir(LIB)
# The id-less poll (the shape the route answered before ids existed) is one of
# the two, never a third: nothing invented a job.
assert imports.job_state()["id"] in {first["job"]["id"], second["job"]["id"]}, imports.job_state()

# --------------------------------------------------------------------------- #
# The QUEUE: `import_bulk_concurrency` at once, the rest waiting their turn
# --------------------------------------------------------------------------- #
# Not a free-for-all and not one at a time: the process has a budget, and what
# it does not admit WAITS — each album with a row that says which it is
# ("queued" until a worker picks it up, "running" while it is imported). The
# width is the payload's own `concurrency`, so a surface can say "4 at once, 9
# queued" without guessing.
queue_cfg = dict(CFG, import_bulk_concurrency=1)
queued_albums = [staging_album(f"Queue Album {n}") for n in (1, 2, 3)]
gate.clear()
imports.finish_album = _slow_finish
try:
    job = imports.start_bulk([{"path": p} for p in queued_albums], queue_cfg)
    assert job["ok"] is True and job["job"]["total"] == 3, job
    assert job["job"]["concurrency"] == 1, job["job"]
    assert all(r["status"] in ("queued", "running") for r in job["job"]["items"]), job["job"]["items"]
    deadline = time.time() + 10
    while time.time() < deadline:
        now = imports.job_state(job["job"]["id"])
        if now["running"] == 1 and now["queued"] == 2:
            break
        time.sleep(0.05)
    now = imports.job_state(job["job"]["id"])
    assert now["running"] == 1, now
    assert now["queued"] == 2, now
    assert [r["status"] for r in now["items"]].count("running") == 1, now["items"]
    assert now["total"] == 3 and now["done"] == 0, now
finally:
    gate.set()
    imports.finish_album = _real_finish
queue_state = _wait_job(job["job"]["id"])
assert queue_state["status"] == "done" and queue_state["done"] == 3, queue_state
assert [i["status"] for i in queue_state["items"]] == ["imported"] * 3, queue_state
assert queue_state["running"] == 0 and queue_state["queued"] == 0, queue_state
assert os.path.isdir(os.path.join(LIB, "Queue Album 1")), os.listdir(LIB)

# --------------------------------------------------------------------------- #
# A pinned MusicBrainz link: the release the owner asked for, and no guessing
# --------------------------------------------------------------------------- #
# `POST /api/import/bulk` items may carry `mbid` — the Library's Import dialog
# collects a MusicBrainz link (or a release-group link) for every album whose
# files name no release, so the pipeline imports the release the user pointed
# at instead of matching by name. The run resolves it (through the shared,
# rate-limited MusicBrainz client) and stamps THAT release; a link MusicBrainz
# cannot answer for is the row's own reason — the album is never imported as
# something else.
from server import integrations as _intg
_real_resolve = _intg.resolve_release
_real_stamp = imports._stamp_release
PIN_ID, PIN_GROUP = "33333333-3333-3333-3333-333333333333", "44444444-4444-4444-4444-444444444444"
PIN_LINK = f"https://musicbrainz.org/release/{PIN_ID}"
stamped = []


def _fake_resolve(mbid):
    stamped.append(("resolved", mbid))
    return {"id": PIN_ID, "title": "Pinned Album", "release_group_id": PIN_GROUP,
            "media": [{"disc": 1, "position": 1, "title": "Song",
                       "recording_mbid": None}],
            "artists": []}


def _record_stamp(album, release, cfg, **kw):
    stamped.append(("stamped", release.get("id")))
    return (1, 0)


_intg.resolve_release = _fake_resolve
imports._stamp_release = _record_stamp
try:
    pinned = staging_album("Pinned Album")
    pinned_out = imports.bulk_import([{"path": pinned, "mbid": PIN_LINK}], CFG)
finally:
    _intg.resolve_release = _real_resolve
    imports._stamp_release = _real_stamp
assert pinned_out["ok"] == 1 and pinned_out["items"][0]["status"] == "imported", pinned_out
# resolved ONCE from the item's link, and every stamp used that release
# (an import stamps it twice by design: the identity pass and the genres)
assert stamped[0] == ("resolved", PIN_LINK), stamped
assert {v for k, v in stamped if k == "stamped"} == {PIN_ID}, stamped
assert os.path.isdir(os.path.join(LIB, "Pinned Album")), os.listdir(LIB)

# …and a link that cannot be resolved fails THAT row, naming MusicBrainz: an
# import must not quietly fall back to a name match the user did not ask for.
def _bad_resolve(mbid):
    raise RuntimeError("MusicBrainz is busy, try again")


_intg.resolve_release = _bad_resolve
try:
    unpinned = staging_album("Unresolvable Album")
    bad_out = imports.bulk_import([{"path": unpinned, "mbid": "https://musicbrainz.org/release/nope"}], CFG)
finally:
    _intg.resolve_release = _real_resolve
assert bad_out["failed"] == 1 and "MusicBrainz" in str(bad_out["items"][0]["error"]), bad_out
assert not os.path.isdir(os.path.join(LIB, "Unresolvable Album")), os.listdir(LIB)

# --------------------------------------------------------------------------- #
# The routes the wizard calls
# --------------------------------------------------------------------------- #
from server import api_imports
from fastapi import HTTPException

preview = api_imports.import_scripts_preview(api_imports.PreviewRequest())
assert preview["count"] == len(preview["chain"]), preview
assert set(preview["labels"]) == set(preview["chain"]), preview
assert all(preview["labels"][sid] for sid in preview["chain"]), preview
assert preview["chain"] == imports.chain_for(load_config()), preview

# The music-folder guard: the handlers import it from server.main INSIDE the
# call (main imports this module to mount the router). A stub stands in for
# main here so this suite stays runnable while that file is being worked on.
import types

_stub = types.ModuleType("server.main")
_stub._music_folder = lambda cfg=None: MF
_stub._allow_staged = lambda p, staged=False: bool(staged) and os.path.exists(p)
_stub._in_music_folder = lambda p, folder: os.path.normcase(
    os.path.abspath(os.path.normpath(p))).startswith(
        os.path.normcase(os.path.abspath(os.path.normpath(folder)).rstrip(os.sep) + os.sep)) or \
    os.path.normcase(os.path.abspath(os.path.normpath(p))) == \
    os.path.normcase(os.path.abspath(os.path.normpath(folder)))
_real_main = sys.modules.get("server.main")
sys.modules["server.main"] = _stub
try:
    try:
        api_imports.import_acoustid(api_imports.AcoustidRequest(paths=[ROOT]))
    except HTTPException as e:
        assert e.status_code == 400 and "outside music folder" in e.detail, e
    else:
        raise AssertionError("a path outside the music folder was accepted")
    inside = api_imports.import_acoustid(
        api_imports.AcoustidRequest(paths=[os.path.join(LIB, "Album One")]))
    assert inside["available"] is False and inside["note"], inside
    # The supplied-match form of the same route: its recordings are path-guarded
    # like everything else this route touches.
    try:
        api_imports.import_acoustid(api_imports.AcoustidRequest(
            paths=[os.path.join(LIB, "Album One")], apply=True,
            match={"release_group_id": "rg", "recordings": [
                {"path": os.path.join(ROOT, "x.flac"), "recording_id": "r",
                 "fingerprint": "AQAB"}]}))
    except HTTPException as e:
        assert e.status_code == 400 and "outside music folder" in e.detail, e
    else:
        raise AssertionError("a supplied match wrote outside the music folder")

    # Submitting to AcoustID is outward-facing: no confirm, no call at all.
    _submits = []
    _real_submit = imports.acoustid_submit
    imports.acoustid_submit = lambda paths, cfg=None: (
        _submits.append(list(paths)) or {"stub": True})
    try:
        try:
            api_imports.import_acoustid_submit(api_imports.AcoustidSubmitRequest(
                paths=[os.path.join(LIB, "Album One")]))
        except HTTPException as e:
            assert e.status_code == 400 and "confirm" in e.detail, e
        else:
            raise AssertionError("an unconfirmed AcoustID submission went ahead")
        assert _submits == [], _submits
        got = api_imports.import_acoustid_submit(api_imports.AcoustidSubmitRequest(
            paths=[os.path.join(LIB, "Album One")], confirm=True))
        assert got == {"stub": True}, got
        assert _submits == [[os.path.join(LIB, "Album One")]], _submits
        try:
            api_imports.import_acoustid_submit(api_imports.AcoustidSubmitRequest(
                paths=[os.path.join(ROOT, "x")], confirm=True))
        except HTTPException as e:
            assert e.status_code == 400 and "outside music folder" in e.detail, e
        else:
            raise AssertionError("submit accepted a path outside the music folder")
    finally:
        imports.acoustid_submit = _real_submit
    try:
        api_imports.import_finish(api_imports.FinishRequest(paths=[os.path.join(ROOT, "x")]))
    except HTTPException as e:
        assert e.status_code == 400, e
    else:
        raise AssertionError("finish accepted a path outside the music folder")
    try:
        api_imports.import_bulk(api_imports.BulkRequest(
            items=[api_imports.BulkItem(path=os.path.join(ROOT, "x"))]))
    except HTTPException as e:
        assert e.status_code == 400, e
    else:
        raise AssertionError("bulk accepted a path outside the music folder")
    # the preview route only guards the paths it is given
    assert api_imports.import_scripts_preview(
        api_imports.PreviewRequest(paths=[os.path.join(LIB, "Album One")]))["count"] >= 0

    # A fresh install has NO music folder configured yet, and the wizard asks
    # for this preview on mount: an empty path list guards nothing and must
    # answer, not 400 (the CI suite runs without any config on disk).
    def _no_folder(cfg=None):
        raise HTTPException(400, "music_folder not set or not found")

    _stub._music_folder = _no_folder
    fresh = api_imports.import_scripts_preview(api_imports.PreviewRequest())
    assert fresh["count"] == len(fresh["chain"]) and fresh["count"] > 0, fresh
    try:
        api_imports.import_scripts_preview(api_imports.PreviewRequest(paths=[ROOT]))
    except HTTPException as e:
        assert e.status_code == 400, e
    else:
        raise AssertionError("paths were accepted with no music folder configured")
finally:
    if _real_main is None:
        sys.modules.pop("server.main", None)
    else:
        sys.modules["server.main"] = _real_main

# --------------------------------------------------------------------------- #
# Release stamping: MB ids + GENRE cascade + advisory in ONE pass
# --------------------------------------------------------------------------- #
from mlo import audio as _audio
from server import integrations as _intg

_stamp_album = staging_album("Stamp Album")
_written = {}


class _FakeAudio:
    """Stands in for mlo.audio.AudioFile: tag writes accumulate per FILE.

    (Two passes walk the album — MB ids, then genres/advisory — and each
    opens its own AudioFile, so the writes have to be keyed by path, not by
    instance.)
    """

    def __init__(self, path):
        self.path = path
        self.audio = object()
        _written.setdefault(os.path.basename(path), {})

    @property
    def tags(self):
        return _written[os.path.basename(self.path)]

    def get_tag(self, key):
        return self.tags.get(key, "")

    def set_tag(self, key, value):
        self.tags[key] = value
        return True


def _joined(value):
    """A tag value the way a READER sees it.

    A writer may hand over a LIST (mlo.audio.set_tag stores repeated fields and
    get_tag joins them with "; ") or an already joined string — the same value
    either way, which is what this asserts.
    """
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v) for v in value)
    return str(value or "")


_real_audiofile, _real_chain = _audio.AudioFile, _intg.genre_chain
_real_resolve_advisory = _intg.resolve_advisory_route
_audio.AudioFile = _FakeAudio
# `_stamp_release` asks the full per-track chain; here it answers with the
# per-track map that chain returns (keyed by disc:position), so the stamping
# test stays offline.
_intg.genre_chain = lambda **kwargs: {
    "per_track": {(1, 1): ["Shoegaze", "Noise Pop"], (1, 2): ["Shoegaze"]},
    "sources": {}, "levels": {}}
try:
    stamped = imports.bulk_import([{
        "path": _stamp_album,
        "release": {"release_mbid": "rel-1", "release_group_id": "rg-1",
                    "title": "Stamp", "artists": [{"name": "A", "mbid": "art-1"}],
                    "advisory": 1,
                    # The pressing's own events: the singular `country` is only
                    # the FIRST of them, so the stamped tag has to come from
                    # this list (issue #18).
                    "country": "GB",
                    "countries": [{"code": "GB", "date": "1994-05-06"},
                                  {"code": "US", "date": "1994-05-20"},
                                  {"code": "XE", "date": "1994-06-01"}],
                    "media": [{"disc": 1, "position": 1, "title": "One", "recording_mbid": "rec-1"},
                              {"disc": 1, "position": 2, "title": "Two", "recording_mbid": "rec-2"}]},
    }], CFG)

    assert stamped["ok"] == 1, stamped
    assert sorted(_written) == ["01 - track.wav", "02 - track.wav"], _written
    for name, tags in _written.items():
        assert tags["MUSICBRAINZ_ALBUMID"] == "rel-1", tags
        assert tags["MUSICBRAINZ_RELEASEGROUPID"] == "rg-1", tags
        assert tags["MUSICBRAINZ_ARTISTID"] == "art-1", tags
        # the release payload's own "advisory" is NOT echoed into the tag: a
        # rating only comes from a lookup that actually states one
        assert "ITUNESADVISORY" not in tags, tags
        assert tags["GENRE"], tags
    assert _written["01 - track.wav"]["MUSICBRAINZ_TRACKID"] == "rec-1", _written["01 - track.wav"]
    assert _written["02 - track.wav"]["MUSICBRAINZ_TRACKID"] == "rec-2", _written["02 - track.wav"]
    # The release's whole country list is stamped, not its singular `country`
    # (MusicBrainz's first event): every country the pressing came out in,
    # ";"-joined, first value first — the same value mlo.autotag's own pass
    # writes and the same one the album badge reads.
    for _name, tags in _written.items():
        assert _joined(tags.get("RELEASECOUNTRY")) == "GB; US; XE", (_name, tags)
    # The list goes in as REPEATED GENRE fields, not one "; "-joined value:
    # the grader counts the values a file carries, so a joined string would
    # read as one genre and fail the per-track count check. The names are
    # canonicalised to MusicBrainz's own spelling and the FAMILY is derived
    # into the FIRST slot — the stub above answers "Shoegaze / Noise Pop", and
    # what lands is `rock` followed by the specific `shoegaze` (mb_genre_count
    # = 2, so the second specific genre yields the family's slot).
    assert _written["01 - track.wav"]["GENRE"] == ["Rock", "Shoegaze"], _written["01 - track.wav"]
    assert _written["02 - track.wav"]["GENRE"] == ["Rock", "Shoegaze"], _written["02 - track.wav"]

    # A stated rating IS written, for every track — and the provider that
    # stated it is reported back per track.
    _stamp_lib = stamped["items"][0]["album_path"]
    _intg.resolve_advisory_route = lambda **kw: {
        "value": 1, "source": "deezer-isrc", "checked": ["deezer-isrc"]}
    adv = imports.fetch_advisories([_stamp_lib], CFG)
    assert adv["updated"] == 2, adv
    assert set(adv["sources"].values()) == {"deezer-isrc"}, adv
    for tags in _written.values():
        assert tags["ITUNESADVISORY"] == "1", tags

    # Nobody states a rating -> the LADDER decides (mlo.advisory): with no AI
    # configured and no lyrics in these silent test files, the last resort is
    # `advisory_fallback` (0 by default), and the stage that wrote it is
    # reported per track. What the providers said stays empty: no source spoke.
    for tags in _written.values():
        tags.pop("ITUNESADVISORY")
    _intg.resolve_advisory_route = lambda **kw: {
        "value": None, "source": None, "checked": ["deezer-isrc", "apple-album"]}
    adv = imports.fetch_advisories([_stamp_lib], CFG)
    assert adv["updated"] == 2, adv
    assert set(adv["sources"].values()) == {"fallback"}, adv
    assert adv["answers"] == {}, adv
    for tags in _written.values():
        assert tags["ITUNESADVISORY"] == "0", tags
    # With the fallback switched off nothing is written at all — an unrated
    # track stays unrated rather than claiming to be clean.
    for tags in _written.values():
        tags.pop("ITUNESADVISORY")
    adv = imports.fetch_advisories([_stamp_lib], dict(CFG, advisory_fallback="none"))
    assert adv["updated"] == 0 and adv["values"] == {}, adv
    for tags in _written.values():
        assert "ITUNESADVISORY" not in tags, tags
finally:
    _audio.AudioFile = _real_audiofile
    _intg.genre_chain = _real_chain
    _intg.resolve_advisory_route = _real_resolve_advisory

# --------------------------------------------------------------------------- #
# What an import does NOT keep: the peer's lyric, genre, advisory and art
# --------------------------------------------------------------------------- #
# `finish_album`'s first pass over an album that just landed is
# `imports.drop_arrived_values`: the four families an import decides for itself
# are the IMPORT's, and every writer for them FILLS an empty slot rather than
# replacing a full one (a user's own write needs that — see the last case
# here), so the values a download arrived with have to go before those writers
# run. Real (if tiny) FLAC files are tagged here rather than a stand-in
# AudioFile, because what this pins is what the app's own tag layer does to a
# file: mutagen, `mlo.audio`, and the writers of all four families.
import struct

from mlo import format_all as _format_all
from mlo import lyrics_fetch as _lyrics_fetch

ARRIVED_ROOT = tempfile.mkdtemp(prefix="mlo_import_arrived_")


def png_rgb(rgb):
    """A real 1×1 PNG in that colour. Pillow opens these, so the cover pass'
    preparation is genuinely exercised instead of skipped as unreadable."""
    import zlib

    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00" + bytes(rgb))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat)
            + chunk(b"IEND", b""))


PEER_ART = png_rgb((255, 0, 0))
ALBUM_ART = png_rgb((0, 0, 255))


def peer_flac(folder, lyric=None, advisory="0"):
    """One track carrying what a peer's download carries: its own GENRE,
    ITUNESADVISORY, LYRICS and embedded art.

    The container is the FLAC marker plus a STREAMINFO block and no frames —
    mutagen tags it and `mlo.audio` reads it exactly like any other FLAC, and
    nothing in this suite decodes audio (the same reason `make_wav` above
    writes a WAV nobody listens to).
    """
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "01 - Track.flac")
    streaminfo = struct.pack(">HH", 4096, 4096) + b"\x00\x00\x00" + b"\x00\x00\x00"
    streaminfo += ((44100 << 44) | (1 << 41) | (15 << 36) | 0).to_bytes(8, "big")
    streaminfo += b"\x00" * 16
    with open(path, "wb") as fh:
        fh.write(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big")
                 + streaminfo)

    from mutagen.flac import FLAC, Picture

    f = FLAC(path)
    f["TITLE"] = "Track One"
    f["ARTIST"] = "Test Artist"
    f["ALBUMARTIST"] = "Test Artist"
    f["ALBUM"] = "Peer Album"
    f["TRACKNUMBER"] = "1"
    f["GENRE"] = "Peer Genre"
    f["ITUNESADVISORY"] = advisory
    if lyric is not None:
        f["LYRICS"] = lyric
    pic = Picture()
    pic.type = 3
    pic.mime = "image/png"
    pic.data = PEER_ART
    f.add_picture(pic)
    f.save()
    return path


def arrived(path):
    """The file through the app's own tag layer — every assertion below is
    about what a reader of the finished album sees."""
    from mlo.audio import AudioFile

    af = AudioFile(path)
    assert af.audio is not None, f"unreadable fixture {path}"
    return af


ARRIVED_CFG = {"music_folder": ARRIVED_ROOT, "advisory_auto_fetch": True,
               "cover_auto_fetch": True, "genre_autofill": True}
APP_NOTES = {"id": "rel-1", "release_group_id": "rg-1", "title": "Peer Album",
             "artists": [{"name": "Test Artist", "mbid": "art-1"}],
             "media": [{"disc": 1, "position": 1, "title": "Track One",
                        "recording_mbid": "rec-1"}]}
IMPORTED_LYRIC = "[00:04.00]the lyric the import fetched"

_real_genre_chain, _real_advisory_route = _intg.genre_chain, _intg.resolve_advisory_route
_real_fetch_lyrics = _lyrics_fetch.fetch_lyrics
# The three remote seams of the import's own writers, answered locally: the
# release's genre chain, the advisory providers, and the lyrics service.
_intg.genre_chain = lambda **kw: {"per_track": {(1, 1): ["Shoegaze"]},
                                  "sources": {}, "levels": {}}
# 1 states explicit — and the fixtures arrive rated 0, so a value that was NOT
# replaced shows up as a 0 rather than as a coincidence.
_intg.resolve_advisory_route = lambda **kw: {"value": 1, "source": "deezer-isrc",
                                             "checked": ["deezer-isrc"]}
_lyrics_fetch.fetch_lyrics = lambda *a, **kw: {
    "provider": "lrclib", "provider_label": "LRCLIB",
    "synced": IMPORTED_LYRIC, "plain": "the lyric the import fetched"}

try:
    # ---- defaults: all four arrived values go, the import's four land -------
    album = os.path.join(ARRIVED_ROOT, "Peer Album")
    track = peer_flac(album, lyric="peer lyric, plain")
    sidecar = os.path.join(album, "01 - Track.lrc")
    with open(sidecar, "w", encoding="utf-8") as fh:
        fh.write("peer lyric, from the sidecar it arrived with")
    af = arrived(track)
    assert af.has_tag("GENRE") and af.get_tag("ITUNESADVISORY") == "0", af.all_tags()
    assert af.get_lyrics() and af.embedded_pictures(), af.all_tags()

    dropped = imports.drop_arrived_values(album, ARRIVED_CFG, [13])
    assert dropped == {"checked": 1, "failed": 0, "lyrics": 1, "genre": 1,
                       "advisory": 1, "cover": 1, "alias": 0}, dropped
    af = arrived(track)
    assert not af.has_tag("GENRE"), af.all_tags()
    assert not af.has_tag("ITUNESADVISORY"), af.all_tags()
    assert not (af.get_lyrics() or "").strip(), af.get_lyrics()
    assert af.embedded_pictures() == [], af.embedded_pictures()
    assert not os.path.exists(sidecar), "the .lrc it arrived with is gone too"

    # The import's own writers, in `finish_album`'s own order: the lyrics step
    # (script 13's per-track core, which the batch runner and the API share),
    # the release's genres, then the advisory pipeline.
    fetched = _lyrics_fetch.fetch_one(track, ARRIVED_CFG)
    assert fetched["status"] == "ok", fetched
    written, failed = imports._stamp_release(album, APP_NOTES, ARRIVED_CFG)
    assert (written, failed) == (1, 0), (written, failed)
    adv = imports.fetch_advisories([album], ARRIVED_CFG)
    assert adv["updated"] == 1, adv
    af = arrived(track)
    assert "the lyric the import fetched" in (af.get_lyrics() or ""), af.get_lyrics()
    # A READER sees the repeated GENRE fields joined ("; "), whatever the writer
    # handed over — the release's genres, and not the peer's.
    assert af.get_tag("GENRE") == "Rock; Shoegaze", af.get_tag("GENRE")
    assert af.get_tag("ITUNESADVISORY") == "1", af.get_tag("ITUNESADVISORY")

    # …and the album's own cover is what the files carry afterwards: script 10's
    # pass is the import's embedder, `embed_covers` ON replacing whatever art is
    # there with the cover the album owns (the shipped OFF is that same pass
    # leaving audio with no art at all, which is what the drop above already
    # left behind).
    cover = os.path.join(album, "cover.png")
    with open(cover, "wb") as fh:
        fh.write(ALBUM_ART)
    prepped = _format_all._prepare_embedded_cover(album, ARRIVED_CFG)
    assert prepped, "the album's own cover is what the pass embeds"
    assert _format_all._format_embedded_covers(
        track, {**ARRIVED_CFG, "embed_covers": True}, {})[1] is True
    pics = arrived(track).embedded_pictures()
    assert len(pics) == 1 and pics[0][1] == prepped[0], [len(b) for _m, b in pics]
    assert pics[0][1] != PEER_ART, "the peer's art never comes back"

    # ---- import_keep_synced_lyrics ON: a SYNCED lyric survives … -----------
    synced = "[00:12.30]peer lyric, synced"
    kept = os.path.join(ARRIVED_ROOT, "Synced Album")
    kept_track = peer_flac(kept, lyric=synced)
    kept_sidecar = os.path.join(kept, "01 - Track.lrc")
    with open(kept_sidecar, "w", encoding="utf-8") as fh:
        fh.write(synced)
    keep_cfg = dict(ARRIVED_CFG, import_keep_synced_lyrics=True)
    dropped = imports.drop_arrived_values(kept, keep_cfg, [13])
    assert dropped["lyrics"] == 0 and dropped["genre"] == 1, dropped
    af = arrived(kept_track)
    assert af.get_lyrics() == synced, af.get_lyrics()
    assert os.path.isfile(kept_sidecar), "the synced sidecar stays too"
    # …and the fetch skips that file for exactly that reason …
    fetched = _lyrics_fetch.fetch_one(kept_track, keep_cfg)
    assert (fetched["status"], fetched["reason"]) == ("skipped", "lyrics already present"), fetched
    # …while the other three families are replaced whatever the switch says.
    assert not af.has_tag("GENRE") and not af.has_tag("ITUNESADVISORY"), af.all_tags()
    assert af.embedded_pictures() == [], af.embedded_pictures()
    # A PLAIN lyric is not work nobody could reproduce — it is what every
    # provider answers with, so the same switch does not keep it.
    plain = os.path.join(ARRIVED_ROOT, "Plain Album")
    plain_track = peer_flac(plain, lyric="peer lyric, plain")
    dropped = imports.drop_arrived_values(plain, keep_cfg, [13])
    assert dropped["lyrics"] == 1, dropped
    assert not (arrived(plain_track).get_lyrics() or "").strip()

    # ---- the wiring: `finish_album` itself drops before its own steps ------
    # The one call every import path makes, with the chain switched off so
    # nothing here depends on a script running. What it proves is WHERE the
    # drop sits: the peer's genre is gone and the release's has already landed
    # from the genres step below it, the art that came with the download is
    # gone, and the two families this chain-less run does not decide (lyrics,
    # advisory) still hold what the album arrived with.
    wired = os.path.join(ARRIVED_ROOT, "Wired Album")
    wired_track = peer_flac(wired, lyric="peer lyric, plain")
    wired_cfg = {"music_folder": ARRIVED_ROOT, "import_auto_scripts": False,
                 "import_scripts": [], "rym_links_auto": False,
                 "metadata_auto_fetch": False, "instrumental_auto_fetch": False,
                 "genre_autofill": True, "cover_auto_fetch": True,
                 "advisory_auto_fetch": True}
    _real_candidates = imports.cover_candidates
    imports.cover_candidates = lambda *a, **kw: None      # no cover search here
    try:
        res = imports.finish_album(wired, wired_cfg, release=APP_NOTES)
    finally:
        imports.cover_candidates = _real_candidates
    af = arrived(wired_track)
    assert res["dropped"] == {"checked": 1, "failed": 0, "lyrics": 0, "genre": 1,
                              "advisory": 0, "cover": 1, "alias": 0}, res["dropped"]
    assert af.get_tag("GENRE") == "Rock; Shoegaze", af.get_tag("GENRE")
    assert af.embedded_pictures() == [], af.embedded_pictures()
    assert "peer lyric" in (af.get_lyrics() or ""), af.get_lyrics()
    assert af.get_tag("ITUNESADVISORY") == "0", af.get_tag("ITUNESADVISORY")

    # ---- no drop, no replacement: the writers are still fill-only ----------
    # The half of the contract the import must NOT change: the wizard's own
    # steps and every /run write into empty slots, and a value already there —
    # the user's own, or a peer's on an album nobody imported — is theirs.
    manual = os.path.join(ARRIVED_ROOT, "Manual Album")
    manual_track = peer_flac(manual, lyric="peer lyric, plain")
    imports._stamp_release(manual, APP_NOTES, ARRIVED_CFG)
    echoes = imports.fetch_advisories([manual], ARRIVED_CFG)
    af = arrived(manual_track)
    assert af.get_tag("GENRE") == "Peer Genre", af.get_tag("GENRE")
    assert echoes["updated"] == 0 and af.get_tag("ITUNESADVISORY") == "0", echoes
    assert set(echoes["sources"].values()) == {"existing-tag"}, echoes["sources"]
    assert "peer lyric" in (af.get_lyrics() or ""), af.get_lyrics()
    assert len(af.embedded_pictures()) == 1, af.embedded_pictures()

    # ---- what the import is NOT going to decide, it does not empty ---------
    # A family the user kept, and a chain with no lyrics step (script 13 is the
    # fetcher — nothing would replace what the drop removed). Both are read off
    # the same switches the steps themselves are gated on.
    held = os.path.join(ARRIVED_ROOT, "Held Album")
    held_track = peer_flac(held, lyric="peer lyric, plain")
    dropped = imports.drop_arrived_values(
        held, dict(ARRIVED_CFG, import_review_families=["genres", "lyrics"]), [13])
    af = arrived(held_track)
    assert dropped["lyrics"] == 0 and dropped["genre"] == 0, dropped
    assert af.has_tag("GENRE") and (af.get_lyrics() or "").strip(), af.all_tags()
    assert dropped["cover"] == 1 and dropped["advisory"] == 1, dropped

    noch = os.path.join(ARRIVED_ROOT, "No Chain Album")
    noch_track = peer_flac(noch, lyric="peer lyric, plain")
    dropped = imports.drop_arrived_values(noch, ARRIVED_CFG, [])
    af = arrived(noch_track)
    # The chain decides the lyrics, so a chain without it keeps them — while the
    # genre and the cover still go (their steps run on every path) and the
    # advisory does NOT (its step only runs when a chain will read it).
    assert dropped["lyrics"] == 0 and dropped["advisory"] == 0, dropped
    assert dropped["genre"] == 1 and dropped["cover"] == 1, dropped
    assert (af.get_lyrics() or "").strip() and af.embedded_pictures() == [], af.all_tags()

    # ---- END TO END: the import FINDS all three and writes them over --------
    # One WHOLE import — the chain included — on an album that arrived carrying
    # the peer's genre, rating and lyric: after `finish_album` the files hold
    # what the import found, family by family (script 13's own runner for the
    # lyrics, `_stamp_release` for the release's genres, `fetch_advisories` for
    # the rating). The chain is faked at ONE seam — `run_chain` — because no
    # real script may touch this suite's fixtures, and script 13 is run FOR REAL
    # inside it: the point is that the import's own chain reaches the fetcher,
    # not that a script runner works.
    e2e = os.path.join(ARRIVED_ROOT, "End To End Album")
    e2e_track = peer_flac(e2e, lyric="peer lyric, plain")
    e2e_sidecar = os.path.join(e2e, "01 - Track.lrc")
    with open(e2e_sidecar, "w", encoding="utf-8") as fh:
        fh.write("peer lyric, from the sidecar it arrived with")
    # The cover family is off here on purpose: this case is about the three
    # families the ask names, and an on cover step would search the network.
    e2e_cfg = {"music_folder": ARRIVED_ROOT, "import_scripts": [13],
               "advisory_auto_fetch": True, "instrumental_auto_fetch": False,
               "genre_autofill": True, "cover_auto_fetch": False,
               "rym_links_auto": False, "metadata_auto_fetch": False}
    _real_chain_fn = script_runners.run_chain
    _chain_ids = []

    def _fake_chain(cfg, ids, targets=None, force=None, progress=None,
                    wait=True, final=None):
        """The one seam: script 13 runs its real runner, everything else is a
        row without work (no other script may touch these fixtures)."""
        _chain_ids.append(list(ids))
        rows = []
        for sid in ids:
            if sid == 13:
                rows.append({"id": 13, "label": "Fetch lyrics",
                             "stats": _lyrics_fetch.run_fetch_lyrics(
                                 dict(cfg, targets=list(targets or [])))})
            else:
                rows.append({"id": sid, "label": f"script {sid}", "stats": {}})
        return rows

    script_runners.run_chain = _fake_chain
    try:
        e2e_res = imports.finish_album(e2e, e2e_cfg, release=APP_NOTES)
    finally:
        script_runners.run_chain = _real_chain_fn
    # the chain the import ran carried the lyrics step, and nothing was
    # configured away — the default shape of an import
    assert _chain_ids == [[13]], _chain_ids
    assert e2e_res["chained"] is True and e2e_res["chain"] == [13], e2e_res
    assert e2e_res["skipped_families"] == [], e2e_res
    # WHAT ARRIVED went first, all three families of it (the cover switch is
    # off, so the peer's art is left where it is — nobody claimed that family)
    assert e2e_res["dropped"] == {"checked": 1, "failed": 0, "lyrics": 1,
                                  "genre": 1, "advisory": 1, "cover": 0,
                                  "alias": 0}, e2e_res["dropped"]
    af = arrived(e2e_track)
    # …and each one is now the IMPORT's: the fetched lyric (the sidecar it
    # arrived with was replaced with the embedded tag the format asks for), the
    # release's genres, the sources' rating.
    assert "the lyric the import fetched" in (af.get_lyrics() or ""), af.get_lyrics()
    assert "peer lyric" not in (af.get_lyrics() or ""), af.get_lyrics()
    assert not os.path.exists(e2e_sidecar), "the arrived .lrc is gone, not kept"
    assert af.get_tag("GENRE") == "Rock; Shoegaze", af.get_tag("GENRE")
    assert af.get_tag("ITUNESADVISORY") == "1", af.get_tag("ITUNESADVISORY")

    # ---- a CONFIGURED skip is REPORTED, never silent ------------------------
    # The three families a config can switch off on its own: a saved chain
    # without script 13, `genre_autofill` off, `advisory_auto_fetch` off. The
    # grader never requires a tag whose writer is off, so there is no gap and no
    # prompt — the import's own report is the only place this can appear, and
    # nothing is overruled to close it: the album keeps exactly what it arrived
    # with, because the writers that would have replaced it are the ones off.
    sk = os.path.join(ARRIVED_ROOT, "Skipped Album")
    sk_track = peer_flac(sk, lyric="peer lyric, plain")
    sk_cfg = dict(e2e_cfg, import_scripts=[4], genre_autofill=False,
                  advisory_auto_fetch=False)
    script_runners.run_chain = _fake_chain
    try:
        sk_res = imports.finish_album(sk, sk_cfg, release=APP_NOTES)
    finally:
        script_runners.run_chain = _real_chain_fn
    assert sk_res["skipped_families"] == [imports.SKIPPED_GENRES,
                                          imports.SKIPPED_LYRICS,
                                          imports.SKIPPED_ADVISORY], sk_res["skipped_families"]
    # the one honest line every surface prints carries them all — a queue row,
    # a notification and the wizard's Finish line are the same wording
    assert sk_res["note"].startswith("the script chain ran 1 script"), sk_res["note"]
    for _reason in sk_res["skipped_families"]:
        assert _reason in sk_res["note"], sk_res["note"]
    assert imports.chain_summary(sk_res) == sk_res["note"], sk_res
    af = arrived(sk_track)
    assert af.get_tag("GENRE") == "Peer Genre", af.get_tag("GENRE")
    assert af.get_tag("ITUNESADVISORY") == "0", af.get_tag("ITUNESADVISORY")
    assert "peer lyric" in (af.get_lyrics() or ""), af.get_lyrics()
finally:
    _intg.genre_chain = _real_genre_chain
    _intg.resolve_advisory_route = _real_advisory_route
    _lyrics_fetch.fetch_lyrics = _real_fetch_lyrics
    shutil.rmtree(ARRIVED_ROOT, ignore_errors=True)

# --------------------------------------------------------------------------- #
# The SHIPPED defaults reach all three families
# --------------------------------------------------------------------------- #
# All of the above is only worth anything if an untouched install can fetch each
# family: the default chain carries the lyric fetch (13), both families' own
# switches ship on, and `import_scripts` ships empty (i.e. DEFAULT_CHAIN). The
# skip report above is for a config that CHANGED one of those, never for a
# fresh one.
from mlo.config import DEFAULT_CONFIG  # noqa: E402  (read with the defaults above)

assert 13 in imports.DEFAULT_CHAIN, imports.DEFAULT_CHAIN
assert DEFAULT_CONFIG["import_scripts"] == [], DEFAULT_CONFIG["import_scripts"]
assert DEFAULT_CONFIG["genre_autofill"] is True, DEFAULT_CONFIG["genre_autofill"]
assert DEFAULT_CONFIG["advisory_auto_fetch"] is True, DEFAULT_CONFIG["advisory_auto_fetch"]
# …which is exactly a config no import reports a skip for
assert imports._skipped_families(imports.DEFAULT_CHAIN, DEFAULT_CONFIG, ()) == [], \
    imports._skipped_families(imports.DEFAULT_CHAIN, DEFAULT_CONFIG, ())

# --------------------------------------------------------------------------- #
# The mover's report is where the album is WHEN THE SCRIPT RETURNS, and the
# album's own files travel with it
# --------------------------------------------------------------------------- #
# beets moves the album into the library and THIS runner then renames it again
# (`beets_organize_after`): beets' own %mlo_dir spelling and MLO's naming script
# differ by more than the extension case — measured on a real Creep EP import,
# beets wrote "…Creep {GB - 7243 8 80234 2 9} [Parlophone] [<release id>]" and
# the organize step renamed it to "…Creep {GB - CD - 7243 8 80234 2 9}
# [Parlophone] [<release id>] [<group id>]". A `moved_targets` computed before
# that step therefore named a folder that no longer existed by the time the
# runner returned, the chain dropped the claim as stale, and every script after
# it ran against the emptied staging folder ("no audio left in …", Format all
# and Grade both reporting 0).
import contextlib  # noqa: E402
import io  # noqa: E402

from server import beetscfg as _beetscfg  # noqa: E402
from server import main as _mlo_main  # noqa: E402

MOVE_MF = tempfile.mkdtemp(prefix="mlo_import_pipeline_move_")
MOVE_LIB = os.path.join(MOVE_MF, "Artists")
MOVE_STAGING = os.path.join(MOVE_LIB, "Creep")
BEETS_DIR = os.path.join(MOVE_LIB, "Radiohead", "Creep [beets spelling]")
ORGANIZED = os.path.join(MOVE_LIB, "Radiohead", "[EP] Creep [naming script]")
for _i in (1, 2):
    make_wav(os.path.join(MOVE_STAGING, f"{_i:02d} - track.wav"))
# what the steps BEFORE the chain (and the chain's own earlier scripts) leave in
# the album folder: the cover the autonomous step fetched, the description
# beside it, the expected-tracklist manifest
for _name in ("cover.jpg", "description.txt", ".mlo_expected.json"):
    with open(os.path.join(MOVE_STAGING, _name), "w", encoding="utf-8") as _f:
        _f.write(_name)


def _fake_beets_import(paths, cfg=None, timeout=None, on_line=None):
    """What `beet import` does: move the audio into the library under the
    naming script's beets spelling, leaving everything else behind."""
    os.makedirs(BEETS_DIR, exist_ok=True)
    for _f in sorted(os.listdir(MOVE_STAGING)):
        if _f.lower().endswith(".wav"):
            shutil.move(os.path.join(MOVE_STAGING, _f),
                        os.path.join(BEETS_DIR, "1-" + _f))
    return True, ""


def _fake_organize(req):
    """The runner's own `organize after beets`: the album moves AGAIN."""
    rows = []
    for p in req.paths:
        if os.path.normcase(os.path.normpath(p)) == \
                os.path.normcase(os.path.normpath(BEETS_DIR)):
            shutil.move(BEETS_DIR, ORGANIZED)
            rows.append({"path": p, "ok": True, "moved": 2, "leftovers": 0,
                         "album_root": ORGANIZED.replace("\\", "/"),
                         "pruned": 0, "notes": [], "errors": []})
    return {"results": rows}


_after_14 = []
def _after_mover(cfg):
    _after_14.append(list(cfg.get("targets") or []))
    return {"modified_count": 0}


_real_beets_import = _beetscfg.run_beets_import
_real_organize = _mlo_main.organize
_real_available = _beetscfg.beets_available
script_runners.RUNNERS[5] = ("Process images", _after_mover)
_move_log = io.StringIO()
final = []
try:
    _beetscfg.run_beets_import = _fake_beets_import
    _mlo_main.organize = _fake_organize
    _beetscfg.beets_available = lambda: "stub"
    with contextlib.redirect_stdout(_move_log):
        results = script_runners.run_chain({"music_folder": MOVE_MF}, [14, 5],
                                           targets=[MOVE_STAGING], final=final)
finally:
    _beetscfg.run_beets_import = _real_beets_import
    _mlo_main.organize = _real_organize
    _beetscfg.beets_available = _real_available
    script_runners.RUNNERS.clear()
    script_runners.RUNNERS.update(_REAL_RUNNERS)

assert results[0]["id"] == 14 and not results[0].get("error"), results[0]
assert results[0]["stats"]["modified_count"] == 1, results[0]["stats"]
assert results[0]["stats"]["moved_targets"] == [ORGANIZED], \
    f"the mover reports the folder the album is in when it returns: {results[0]['stats']}"
assert [os.path.normcase(p) for p in final] == [os.path.normcase(ORGANIZED)], final
assert [os.path.normcase(p) for p in _after_14[0]] == [os.path.normcase(ORGANIZED)], \
    f"the script after the mover ran on that folder: {_after_14}"
assert "no audio left" not in _move_log.getvalue(), _move_log.getvalue()
# the album's own files travelled with it — cover, description, manifest
assert sorted(os.listdir(ORGANIZED)) == [
    ".mlo_expected.json", "1-01 - track.wav", "1-02 - track.wav", "cover.jpg",
    "description.txt"], sorted(os.listdir(ORGANIZED))
# …and the staging folder is gone, not an audio-less shell holding somebody's
# cover art (which is what the scan reports as a broken album)
assert not os.path.exists(MOVE_STAGING), sorted(os.listdir(MOVE_LIB))
shutil.rmtree(MOVE_MF, ignore_errors=True)

shutil.rmtree(ROOT, ignore_errors=True)
print("import pipeline: all assertions passed")

# --------------------------------------------------------------------------- #
# A digital release's own three answers (server.imports.settle_digital_import)
# --------------------------------------------------------------------------- #
# What the owner's grading messages were about: a folder of audio that arrived
# with no SOURCE, no album description and lyrics a script cannot repair.
# Before this step a manual import landed all three as grading failures —
# "Missing SOURCE (required for Digital Media)", "Lyrics not optimally
# formatted (run Lyrics script)" and "Album description missing — fetch one on
# the album page". What is asserted here is the SETTLE, the one entry point
# both the pipeline and the wizard's Finish call:
#
#   * SOURCE asks (never invents) when nothing states one, and is written to
#     every track that lacks it once the release or the user answers;
#   * an untimed lyric — and one the formatter cannot canonicalise — is removed
#     when the chain will fetch, and NOT touched when it will not (the honest
#     report is the family's own skip there);
#   * the album description comes from the import's own metadata step, whose
#     "nothing found" answer is reported rather than left to the grade.
print("== the digital release's own answers ==")

DIG_MF = os.path.join(ROOT, "digital_music")
DIG_LIB = os.path.join(DIG_MF, "Artists")
DIG_ALBUM = os.path.join(DIG_LIB, "Digital Album")
os.makedirs(DIG_ALBUM)
DIG_CFG = {"music_folder": DIG_MF, "import_auto_scripts": False,
           "import_scripts": [], "advisory_auto_fetch": False,
           "metadata_auto_fetch": False, "cover_auto_fetch": False,
           "rym_links_auto": False, "instrumental_auto_fetch": False}
DIG_PLAIN = "Setting sun on the neon drift\na chrome horizon"
DIG_MERGED = "[00:00.00][00:45.53]Setting  sun  on the neon drift\n[00:46.86]Against her skin"


FLAC_EXE = None
_deps = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     ".dependencies")
if os.path.isdir(_deps):
    for _entry in sorted(os.listdir(_deps)):
        if _entry.lower().startswith("flac"):
            _cand = os.path.join(_deps, _entry, "flac.exe")
            if os.path.isfile(_cand):
                FLAC_EXE = _cand
                break
if FLAC_EXE is None:
    FLAC_EXE = shutil.which("flac")
assert FLAC_EXE, "flac.exe not found — the digital-release case needs real FLACs"


def _dig_flac(name, tags):
    path = os.path.join(DIG_ALBUM, name)
    wav = path + ".wav"
    with wave.open(wav, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00\x00\x00" * 4410)
    subprocess.run([FLAC_EXE, "-s", "-f", "-8", "-o", path, wav],
                   check=True, capture_output=True)
    os.remove(wav)
    from mutagen.flac import FLAC
    f = FLAC(path)
    for k, v in tags.items():
        f[k] = [v]
    f.save()
    return path


_dig_files = [
    _dig_flac("1-01 - plain.flac", {
        "TITLE": "Plain", "ARTIST": "Digital Artist", "ALBUMARTIST": "Digital Artist",
        "ALBUM": "Digital Album", "TRACKNUMBER": "1", "DISCNUMBER": "1",
        "MEDIA": "Digital Media", "INSTRUMENTAL": "0", "LYRICS": DIG_PLAIN}),
    _dig_flac("1-02 - merged.flac", {
        "TITLE": "Merged", "ARTIST": "Digital Artist", "ALBUMARTIST": "Digital Artist",
        "ALBUM": "Digital Album", "TRACKNUMBER": "2", "DISCNUMBER": "1",
        "MEDIA": "Digital Media", "INSTRUMENTAL": "0", "LYRICS": DIG_MERGED}),
]


def _dig_issues():
    from mlo.grader import _grade_album
    res = _grade_album(DIG_ALBUM, "EMBEDDED", DIG_CFG) or {}
    return res.get("issues") or {}


def _lyric_issues(issues=None):
    """Every lyrics-format finding, whatever its wording.

    The message NAMES its reason now ("not optimally formatted (run Lyrics
    script) — …" for what the formatter repairs, "cannot be repaired by a
    script: …" for the conditions no formatter can invent timing for), so a
    case asserts the FINDING, not one of the two spellings.
    """
    found = issues if issues is not None else _dig_issues()
    return [m for m in found
            if m.startswith("Lyrics not optimally formatted")
            or m.startswith("Lyrics cannot be repaired by a script")]


_before = _dig_issues()
assert "Missing SOURCE (required for Digital Media)" in _before, sorted(_before)
assert _lyric_issues(_before), sorted(_before)
assert "Album description missing — fetch one on the album page" in _before, sorted(_before)

# the suggestion: nothing states a source, so it is ASKED, with the config's
# own default for the field — and nothing is written
_sug = imports.stamp_album_source(DIG_ALBUM, DIG_CFG, dry=True)
assert _sug["state"] == "asked" and _sug["value"] == "" and _sug["default"], _sug
assert _sug["missing"] == 2 and _sug["written"] == 0, _sug
assert _dig_issues().get("Missing SOURCE (required for Digital Media)"), "a dry ask writes nothing"

# the release's own store URL is evidence the pipeline MAY write
_derived = imports.stamp_album_source(
    DIG_ALBUM, DIG_CFG, release={"id": "", "urls": ["https://qobuz.com/album/x"]})
assert _derived["state"] == "written" and _derived["value"] == "Qobuz", _derived
assert _derived["from"] == "release" and _derived["written"] == 2, _derived
assert "Missing SOURCE (required for Digital Media)" not in _dig_issues(), _dig_issues()

# the user's own answer fills what the release did not
from mutagen.flac import FLAC as _MFLAC
for _p in _dig_files:
    _f = _MFLAC(_p)
    del _f["SOURCE"]
    _f.save()
_answered = imports.stamp_album_source(DIG_ALBUM, DIG_CFG, value="Bandcamp")
assert _answered["state"] == "written" and _answered["value"] == "Bandcamp", _answered
assert all(_MFLAC(p).get("SOURCE") == ["Bandcamp"] for p in _dig_files)

# the lyrics: the formatter cannot repair either lyric, and the fetch will run
assert imports.settle_digital_lyrics(DIG_ALBUM, DIG_CFG, chain=[1, 13], dry=True) \
    ["state"] == "would-clean"
_no_fetch = imports.settle_digital_lyrics(DIG_ALBUM, DIG_CFG, chain=[1])
assert _no_fetch["state"] == "no-fetch" and _no_fetch["dropped"] == 0, _no_fetch
assert _lyric_issues(), "a chain without the fetch leaves the lyrics alone"
_cleaned = imports.settle_digital_lyrics(DIG_ALBUM, DIG_CFG, chain=[1, 13])
assert _cleaned["state"] == "cleaned" and _cleaned["dropped"] == 2, _cleaned
assert _cleaned["unformatted"] == 1, _cleaned  # the merged one the formatter cannot fix
assert not _lyric_issues(), _dig_issues()
# …and with plain lyrics allowed by the install, nothing is theirs to remove
from mutagen.flac import FLAC as _MFLAC2
_MFLAC2(_dig_files[0])["LYRICS"] = DIG_PLAIN
_MFLAC2(_dig_files[0]).save()
_allowed = imports.settle_digital_lyrics(DIG_ALBUM, dict(DIG_CFG, lyrics_allow_plain=True),
                                         chain=[1, 13])
assert _allowed["state"] == "allow-plain" and _allowed["dropped"] == 0, _allowed

# the description: the import's OWN metadata step, and its honest answer when
# nothing is found (no provider reachable / nothing to find)
from server import discovery as _dig_discovery
_real_album_description = _dig_discovery.album_description
try:
    _dig_discovery.album_description = lambda *a, **k: {}
    _meta_cfg = dict(DIG_CFG, metadata_auto_fetch=True)
    _nope = imports.settle_digital_import(DIG_ALBUM, _meta_cfg, chain=[1, 13], metadata=True)
    assert _nope["metadata"]["applied"]["album_description"] is None, _nope["metadata"]
    assert "Album description missing — fetch one on the album page" in _dig_issues(), \
        "an album nothing can describe still grades as missing — and the settle says so"
    _dig_discovery.album_description = lambda *a, **k: {
        "text": "A digital release described by its own source.",
        "source": "wikipedia", "source_url": "https://en.wikipedia.org/wiki/x"}
    _got = imports.settle_digital_import(DIG_ALBUM, _meta_cfg, chain=[1, 13], metadata=True)
    assert _got["metadata"]["applied"]["album_description"], _got["metadata"]
    assert "Album description missing — fetch one on the album page" not in _dig_issues(), \
        _dig_issues()
finally:
    _dig_discovery.album_description = _real_album_description
for _probe in ("Missing SOURCE (required for Digital Media)",
               "Album description missing — fetch one on the album page"):
    assert _probe not in _dig_issues(), (_probe, _dig_issues())
assert not _lyric_issues(), _dig_issues()

# the whole thing again through the import itself: a chain-less import still
# settles both halves and reports them (there is no wizard-only path)
_res = imports.finish_album(DIG_ALBUM, DIG_CFG)
assert _res["settled"]["source"]["state"] in ("present", "written", "asked"), _res["settled"]
assert _res["settled"]["lyrics"]["state"] == "no-fetch", _res["settled"]
shutil.rmtree(DIG_MF, ignore_errors=True)

# --------------------------------------------------------------------------- #
# A release-driven import fills the album's OWN identity (the owner's ask)
# --------------------------------------------------------------------------- #
# "IF a specific release is being imported (not just download best behavior),
# the app should auto fill relevant info on it (like the bitrate, country and
# mediatype like how other albums have). If a best downloader behavior /
# auto-import download finishes and verifies a download of a good release,
# right after that it should also auto-fill this info."
#
# The three fields an album's readout shows, and where each one comes from:
#
#   country      RELEASECOUNTRY — MusicBrainz's own release events;
#   mediatype    MEDIA          — the medium MusicBrainz states for the release
#                                 (a pressing can be Vinyl, SACD, SHM-CD, Web…);
#   bitrate      NOT a tag at all: the format/bitrate half of the card and of
#                the album page is the audio's own tech (`server.tagcache` reads
#                mutage/ffprobe: codec, bitrate, depth, rate), which every
#                reader has the moment a file is readable — so the import has
#                nothing to write for it and this suite asserts the READOUT.
#
# What an import must therefore do is write the RELEASE's identity, from the
# release payload it already holds (the edition the user picked, or the pressing
# the job downloaded and verified) — no MusicBrainz request of its own — and
# fill only: a medium, a country or an id the file already carries is the user's.
print("== a release's own identity, at import time ==")

import json  # noqa: E402

from server import library as _lib  # noqa: E402

IDENT_MF = os.path.join(ROOT, "identity_music")
os.makedirs(IDENT_MF, exist_ok=True)
IDENT_CFG = {"music_folder": IDENT_MF, "import_auto_scripts": False,
             "import_scripts": [], "advisory_auto_fetch": False,
             "metadata_auto_fetch": False, "cover_auto_fetch": False,
             "rym_links_auto": False, "instrumental_auto_fetch": False,
             "genre_autofill": False}


def ident_release(n, media, country, catalog="", label="Sire",
                  countries=None, date="1980-10-08"):
    """A MusicBrainz release payload in the shape `release_lookup` returns."""
    return {
        "id": f"{n:08d}-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "title": "Remain in Light",
        "date": date,
        "originaldate": "1980",
        "country": country,
        "countries": countries if countries is not None
                     else [{"code": country, "date": date}],
        "status": "Official",
        "medium": media,
        "medium_formats": [media],
        "label": label,
        "catalog_number": catalog or f"CAT-{n}",
        "barcode": f"00000000000{n}",
        "release_group_id": f"{n:08d}-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "release_type": "album",
        "primary_type": "Album",
        "secondary_types": [],
        "artists": [{"name": "Talking Heads",
                     "mbid": "a94a7155-c79d-4409-9fcf-220cb0e4dc3a"}],
        "medium_count": 1,
        "media": [{"disc": 1, "position": i, "title": f"Track {i}",
                   "recording_mbid": f"{n:08d}-cccc-4ccc-8ccc-cccccccccc{i:02d}"}
                  for i in (1, 2)],
    }


def ident_album(name, track_tags=()):
    """A real two-track FLAC album (the app's own tag layer reads it back)."""
    folder = os.path.join(IDENT_MF, "Artists", "Talking Heads", name)
    os.makedirs(folder, exist_ok=True)
    paths = []
    for i in (1, 2):
        path = os.path.join(folder, f"1-0{i} - Track {i}.flac")
        wav = path + ".wav"
        with wave.open(wav, "w") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(b"\x00\x00\x00\x00" * (44100 * 2))
        subprocess.run([FLAC_EXE, "-s", "-f", "-8", "-o", path, wav],
                       check=True, capture_output=True)
        os.remove(wav)
        from mutagen.flac import FLAC
        f = FLAC(path)
        for k, v in (("TITLE", f"Track {i}"), ("ARTIST", "Talking Heads"),
                     ("ALBUMARTIST", "Talking Heads"), ("ALBUM", "Remain in Light"),
                     ("TRACKNUMBER", str(i)), ("DISCNUMBER", "1")):
            f[k] = [v]
        for tag, value in (track_tags if i == 1 else ()):
            f[tag] = [value]
        f.save()
        paths.append(path)
    return folder, paths


def ident_tags(path):
    from mlo.audio import AudioFile
    af = AudioFile(path)
    assert af.audio is not None, f"unreadable fixture {path}"
    return {t: str(af.get_tag(t) or "") for t in
            ("MEDIA", "RELEASECOUNTRY", "CATALOGNUMBER", "LABEL", "RELEASESTATUS",
             "DATE", "MUSICBRAINZ_ALBUMID")}


# ---- (1) the specific release the import is FOR ------------------------------
# The edition the user picked: the medium, the country and the release's own
# catalogue number land while the album is still the folder that arrived.
LATEST = ident_release(3, "SHM-CD", "JP", catalog="WPCR-13292")
album_dir, album_files = ident_album("Remain in Light (2009)")
res = imports.finish_album(album_dir, IDENT_CFG, release=LATEST)
assert res["release_identity"] == {"written": 2, "skipped": 0, "failed": 0}, \
    res["release_identity"]
got = ident_tags(album_files[0])
assert got["MEDIA"] == "SHM-CD", got
assert got["RELEASECOUNTRY"] == "JP", got
assert got["CATALOGNUMBER"] == "WPCR-13292" and got["LABEL"] == "Sire", got
assert got["RELEASESTATUS"] == "Official" and got["DATE"] == "1980-10-08", got
assert got["MUSICBRAINZ_ALBUMID"] == LATEST["id"], got
assert ident_tags(album_files[1]) == got, "every track of the album, not just one"
# …and the album READOUT is what the reader sees: the pressing, then the audio's
# own format/bitrate (the encoding half is measured, never written).
row = _lib.build_album(album_dir, dict(IDENT_CFG, music_folder=IDENT_MF))
assert row["media"] == "SHM-CD", row["media"]
assert row["meta"]["RELEASECOUNTRY"] == "JP" and row["meta"]["MEDIA"] == "SHM-CD", row["meta"]
tech = row["tracks"][0]["tech"]
assert tech.get("codec") == "FLAC" and tech.get("bitrate") and \
    tech.get("bits_per_sample") == 16 and tech.get("sample_rate") == 44100, tech

# A SECOND import of the same album writes nothing at all: every slot is filled
# (the fill is not a rewrite), and it costs no MusicBrainz request either —
# the release is in hand.
again = imports.finish_album(album_dir, IDENT_CFG, release=LATEST)
assert again["release_identity"] == {"written": 0, "skipped": 2, "failed": 0}, \
    again["release_identity"]

# ---- (2) a value that is already there is the USER's -------------------------
# The shipped MEDIA default and a catalogue number the user typed are kept, the
# release fills only what is empty — the app's writer rule, which is what makes
# an import safe on an album somebody has already edited.
KEPT = ident_release(4, "Vinyl", "DE", catalog="SHOULD-NOT-LAND")
kept_dir, kept_files = ident_album("Remain in Light (kept)",
                                   track_tags=(("MEDIA", "CD"),
                                               ("CATALOGNUMBER", "MINE"),
                                               ("RELEASECOUNTRY", "FR"),
                                               ("LABEL", "My Label")))
kept = imports.finish_album(kept_dir, IDENT_CFG, release=KEPT)
kept_tags = ident_tags(kept_files[0])
# BOTH files gain a slot (the status, the date and the release id), while every
# value already there is untouched — "written" counts files, as the genre stamp
# beside it does.
assert kept["release_identity"]["written"] == 2, kept["release_identity"]
assert kept_tags["MEDIA"] == "CD", kept_tags
assert kept_tags["CATALOGNUMBER"] == "MINE", kept_tags
assert kept_tags["LABEL"] == "My Label", kept_tags
# RELEASECOUNTRY is the one release tag the app WIDENS rather than fills — a
# country the release does not state keeps its value, and nothing here may
# replace one country with another (see mlo.autotag._country_upgrade).
assert kept_tags["RELEASECOUNTRY"] == "FR", kept_tags
# the slots nobody had still land, so "kept" is not "left alone entirely"
assert kept_tags["RELEASESTATUS"] == "Official" and kept_tags["DATE"] == "1980-10-08", kept_tags

# …and the SAME rule holds for the country the release DOES state, as the
# server writes it: widening, never replacing (mlo.autotag's own rule).
WIDE = ident_release(5, "CD", "US", countries=[{"code": "US"}, {"code": "CA"}])
wide_dir, wide_files = ident_album("Remain in Light (wide)",
                                   track_tags=(("RELEASECOUNTRY", "US"),))
imports.finish_album(wide_dir, IDENT_CFG, release=WIDE)
assert ident_tags(wide_files[0])["RELEASECOUNTRY"] == "US; CA", \
    ident_tags(wide_files[0])

# ---- (3) the pressing that LANDED, not the one that was asked for ------------
# The add recorded a framework album for the US CD ("Add to library"), the walk
# landed a different pressing, and the import is handed THE RELEASE IT FETCHED
# (`finish_album(release=…)`). The album's own
# record still names the asked-for edition — the framework marker and the
# release manifest — so a writer reading those instead of the payload in hand
# would stamp the wrong pressing.
ASKED = ident_release(6, "CD", "US", catalog="ASKED-CAT")
LANDED = ident_release(7, "12\" Vinyl", "GB", catalog="LANDED-CAT")
landed_dir, landed_files = ident_album("Remain in Light (landed)")
mlo_paths.save_pending(landed_dir, {
    "pending": True, "release_id": ASKED["id"], "release_group_id": ASKED["release_group_id"],
    "title": ASKED["title"], "artist": "Talking Heads", "year": "1980",
    "date": ASKED["date"], "release_type": "Album"})
mlo_paths.save_expected_tracks(landed_dir, ASKED["id"], [
    {"disc": 1, "position": i, "title": f"Track {i}",
     "recording_mbid": f"asked-rec-{i}"} for i in (1, 2)])
landed = imports.finish_album(landed_dir, IDENT_CFG, release=LANDED)
landed_tags = ident_tags(landed_files[0])
assert landed["release_identity"] == {"written": 2, "skipped": 0, "failed": 0}, \
    landed["release_identity"]
assert landed_tags["MEDIA"] == "12\" Vinyl", landed_tags
assert landed_tags["RELEASECOUNTRY"] == "GB", landed_tags
assert landed_tags["CATALOGNUMBER"] == "LANDED-CAT", landed_tags
assert landed_tags["MUSICBRAINZ_ALBUMID"] == LANDED["id"], landed_tags
assert ASKED["id"] not in json.dumps(landed_tags), \
    f"the asked-for pressing must not appear anywhere: {landed_tags}"
# the marker is the framework album's, and the import clears it as it does on
# every path — the point here is only that it was never the SOURCE of the facts
assert not mlo_paths.load_pending(landed_dir), \
    "the framework marker is cleared by the import that filled the album"
shutil.rmtree(IDENT_MF, ignore_errors=True)
print("release identity: all assertions passed")

# --------------------------------------------------------------------------- #
# The phases an import publishes: what THIS album is having done to it
# --------------------------------------------------------------------------- #
print("== the import's published phases ==")
# The owner's ask: the step display must make sense and be up to date. A phase
# frame is the import's readout BEFORE its first script, so it names work that
# is really about to happen to THIS album — a step that is a no-op for it (a CD
# rip has nothing digital to settle), a family that is switched off (links,
# metadata, cover art, advisories, instrumentals) or a decision the album states
# nothing for (no release identity to stamp, no genre source to ask) is not
# announced at all. Asserted as the EXACT sequence, in order, because "which
# frames" is the whole contract — a count is satisfied by any producer that
# happens to publish as many.
PHASE_MF = os.path.join(ROOT, "phase_music")
os.makedirs(PHASE_MF, exist_ok=True)
PHASE_CFG = {"music_folder": PHASE_MF, "import_auto_scripts": True,
             "import_scripts": [4], "advisory_auto_fetch": True,
             "instrumental_auto_fetch": True, "metadata_auto_fetch": False,
             "cover_auto_fetch": False, "rym_links_auto": False,
             "genre_autofill": True, "import_autonomy": "automatic"}
PHASE_RELEASE = {"id": "phase-rel-1", "release_group_id": "phase-rg-1",
                 "title": "Phase Album", "date": "1996-06-11",
                 "medium_formats": ["Digital Media"],
                 "artists": [{"name": "Phase Artist", "mbid": "phase-art-1"}],
                 "media": [{"disc": 1, "position": 1, "title": "Track One",
                            "recording_mbid": "phase-rec-1", "video": False}]}
_phase_rows = []
_prev_hook = getattr(_stats, "progress_hook", None)


def _phase_hook(done, total, desc, steps=None):
    _phase_rows.append((desc, done, total, steps))


# The two writers the chain's own steps would reach are stubbed: this suite
# runs offline, and what is under test is WHICH phase is announced, not what the
# fetchers do (tools/test_import_pipeline.py's own advisory section covers them
# then, with the same stubs).
_saved_fetch_adv, _saved_fetch_inst = imports.fetch_advisories, imports.fetch_instrumentals
imports.fetch_advisories = lambda paths, cfg=None: {"updated": 0}
imports.fetch_instrumentals = lambda paths, cfg=None: {"written": 0}
# …and the chain itself, so no script really runs here: the fixture's chain is
# what makes the advisory and instrumental phases real (they are gated on a
# chain that reads what they write), and a grade run would take a minute of this
# suite's time without telling it anything about phases.
_saved_chain_fn_phases = script_runners.run_chain
script_runners.run_chain = lambda cfg, ids, **kw: [
    {"id": sid, "label": f"Script {sid}", "stats": {}, "error": None}
    for sid in ids]


def phase_album(name):
    for i in (1, 2):
        make_wav(os.path.join(PHASE_MF, name, f"{i:02d} - track.wav"))
    return os.path.join(PHASE_MF, name)


def published_phases(album, cfg, release=None):
    """The PHASE frames one import published, in order.

    The chain's own frames ("Grade", "#2/4 Key & BPM", …) travel on the same
    hook, so only the vocabulary `imports._phase` publishes is read back:
    `PHASE_TEXTS` is that list, one entry per step in `_finish_album`.
    """
    _phase_rows.clear()
    _stats.progress_hook = _phase_hook
    try:
        imports.finish_album(album, dict(cfg), release=release)
    finally:
        _stats.progress_hook = _prev_hook
    return [row[0] for row in _phase_rows if row[0] in PHASE_TEXTS]


try:
    # (1) an album with a release and a genre source: exactly the steps it
    #     really has, in the order they run — and nothing else (every other
    #     family is off in PHASE_CFG, and this fixture's tracks state no MEDIA,
    #     so the digital-release settling has nothing to settle either).
    _got = published_phases(phase_album("Phase One"), PHASE_CFG, PHASE_RELEASE)
    assert _got == ["Stamping the release's identity…", "Fetching genres…",
                    "Fetching advisories…", "Checking instrumentals…"], _got
    # (2) …and a GENRE-LESS config skips the genre frame (the owner's example),
    #     keeping every other step exactly where it was.
    _got = published_phases(phase_album("Phase Two"),
                            dict(PHASE_CFG, genre_autofill=False), PHASE_RELEASE)
    assert _got == ["Stamping the release's identity…",
                    "Fetching advisories…", "Checking instrumentals…"], _got
    # (3) an album the app knows nothing about: no release, no MusicBrainz
    #     identity, no chain — no step to announce at all.
    _got = published_phases(phase_album("Phase Three"),
                            dict(PHASE_CFG, import_auto_scripts=False,
                                 import_scripts=[]))
    assert _got == [], _got
    # (4) …and a phase frame says only WHERE the import is: no count and no step
    #     pair, so the strip draws an indeterminate bar under the phase's name
    #     rather than a percentage of something. The pair belongs to a chain
    #     that has actually started (tools/test_chain_bar.py pins both halves).
    _phase_rows.clear()
    _stats.progress_hook = _phase_hook
    try:
        imports.finish_album(phase_album("Phase Four"), dict(PHASE_CFG),
                             release=PHASE_RELEASE)
    finally:
        _stats.progress_hook = _prev_hook
    _phase_frames = [row for row in _phase_rows if row[0] in PHASE_TEXTS]
    assert _phase_frames, _phase_rows
    assert all(row[1] == 0 and row[2] == 0 and row[3] is None
               for row in _phase_frames), _phase_frames
finally:
    imports.fetch_advisories, imports.fetch_instrumentals = _saved_fetch_adv, _saved_fetch_inst
    script_runners.run_chain = _saved_chain_fn_phases
    shutil.rmtree(PHASE_MF, ignore_errors=True)
print("phases: all assertions passed")

# --------------------------------------------------------------------------- #
# The import's own completion frame names the ALBUM, not the folder
# --------------------------------------------------------------------------- #
# `imports._announce_import` is the ONE entry/exit every import path announces
# through, and it used to name the album by `os.path.basename(path)`: a
# downloaded folder is a peer's spelling, a bare UUID or the title alone, so the
# frame named nothing a user could place. It reads the album's own tags now.
print("== the import's own notices ==")
from server import events as _events  # noqa: E402
from mlo.audio import AudioFile as _AudioFile  # noqa: E402

NOTICE_DIR = os.path.join(ROOT, "notice_music")
os.makedirs(NOTICE_DIR, exist_ok=True)
# A real tagged FLAC under a folder named like a bare UUID — the shape a
# downloaded folder really has once the app has named nothing.
_notice_folder = os.path.join(NOTICE_DIR, "5be1a1e2-0f0f-4a1b-9c3d-deadbeef0000")
notice_track = peer_flac(_notice_folder)
from mutagen.flac import FLAC as _NoticeFLAC  # noqa: E402

_notice_file = _NoticeFLAC(notice_track)
_notice_file["DATE"] = "1996-06-11"
_notice_file.save()
_notice_said = []
_real_emit_fn = _events.emit
_events.emit = lambda kind, title, body, data=None, **kw: _notice_said.append(
    (kind, title, body, data))
try:
    imports._announce_import("import_started", _notice_folder, cfg=IDENT_CFG)
    imports._announce_import("import_done", _notice_folder,
                             {"chained": True, "scripts": [1], "errors": [],
                              "chain": [1]}, cfg=IDENT_CFG)
finally:
    _events.emit = _real_emit_fn
assert [row[0] for row in _notice_said] == ["import_started", "import_done"], _notice_said
assert _notice_said[0][1] == "Importing Test Artist — Peer Album (1996)", _notice_said[0]
assert _notice_said[1][1] == "Imported Test Artist — Peer Album (1996)", _notice_said[1]
# …and the identity helper is what both read: the folder is a bare UUID here, so
# a by-the-folder name would be that UUID.
assert imports.album_identity_label(_notice_folder) == \
    "Test Artist — Peer Album (1996)", imports.album_identity_label(_notice_folder)
shutil.rmtree(NOTICE_DIR, ignore_errors=True)
print("import notices: all assertions passed")

# --------------------------------------------------------------------------- #
# The arrived ALIAS tags are the import's to replace (issue #59)
# --------------------------------------------------------------------------- #
print("== the arrived aliases ==")
# `mlo.autotag` writes TITLEALIAS / ARTISTALIAS / ALBUMALIAS from the release
# the import holds, and it COMPLETES a list rather than replacing it — so the
# peer's spellings have to go first, exactly like the other four families an
# import decides. The family is the release-identity stamp's ("links"): a user
# who kept that family for review keeps the arrived aliases too.
alias_dir = os.path.join(ROOT, "alias_music")
os.makedirs(alias_dir, exist_ok=True)
# The same real-FLAC fixture the arrived-values section builds, so the tags are
# read back through the app's own tag layer (`arrived`) rather than through a
# second vocabulary of this test's own.
alias_track = peer_flac(alias_dir)
from mutagen.flac import FLAC as _FLAC

_alias_file = _FLAC(alias_track)
_alias_file["TITLEALIAS"] = "The Peer's Spelling"
_alias_file["TITLEALIAS-JA"] = "ピアの綴り"
_alias_file["ARTISTALIAS"] = "Peer Alias"
_alias_file.save()
alias_cfg = {"music_folder": alias_dir, "advisory_auto_fetch": False,
             "cover_auto_fetch": False, "genre_autofill": True}
alias_dropped = imports.drop_arrived_values(alias_dir, alias_cfg, [])
# The GENRE the peer shipped goes with them (the import decides that family) and
# the advisory/cover families are switched off here, so their values stay — the
# count of the family under test is the one that matters.
assert alias_dropped == {"checked": 1, "failed": 0, "lyrics": 0, "genre": 1,
                         "advisory": 0, "cover": 0, "alias": 1}, alias_dropped
_af = arrived(alias_track)
assert not _af.has_tag("TITLEALIAS"), _af.all_tags()
assert _af.get_tag("TITLEALIAS-JA") is None, _af.all_tags()
assert not _af.has_tag("ARTISTALIAS"), _af.all_tags()
# …and the family a user kept for review keeps them: nothing is dropped.
_alias_file = _FLAC(alias_track)
_alias_file["TITLEALIAS"] = "The Peer's Spelling"
_alias_file.save()
kept_dropped = imports.drop_arrived_values(
    alias_dir, dict(alias_cfg, import_review_families=["links"]), [])
assert kept_dropped["alias"] == 0, kept_dropped
assert arrived(alias_track).has_tag("TITLEALIAS"), "a kept family is not emptied"
shutil.rmtree(alias_dir, ignore_errors=True)
print("aliases: all assertions passed")

# --------------------------------------------------------------------------- #
# ONE chain per album, whoever asks second (the owner's "scripts run twice")
# --------------------------------------------------------------------------- #
print("== a second import of an album that is already being imported ==")
# The owner's report: "the scripts in auto-importing seem to be run twice". Two
# autonomous paths can reach ONE album (a download's own finish and a page
# download), and the second must not queue behind the
# first and then run the whole pipeline again — that is the repeat, and both
# halves of it (the six look-ups and every script) are what the album's second
# import is refused for. The rule lives in `finish_album` and is about the
# ALBUM: the only caller allowed to chain an album another import holds is the
# one holding it (this job's own nested steps).
import threading as _threading

from server import job_locks as _job_locks

DOUBLE_MF = os.path.join(ROOT, "double_music")
os.makedirs(DOUBLE_MF, exist_ok=True)
DOUBLE_CFG = {"music_folder": DOUBLE_MF, "import_auto_scripts": True,
              "import_scripts": [4], "advisory_auto_fetch": False,
              "instrumental_auto_fetch": False, "metadata_auto_fetch": False,
              "cover_auto_fetch": False, "rym_links_auto": False,
              "genre_autofill": False, "import_autonomy": "automatic"}
double_album = os.path.join(DOUBLE_MF, "Double Album")
for _i in (1, 2):
    make_wav(os.path.join(double_album, f"{_i:02d} - track.wav"))

DOUBLE_CHAINS = []
DOUBLE_GATE = _threading.Event()
_saved_chain_fn = script_runners.run_chain


def _blocking_chain(cfg, ids, targets=None, force=None, progress=None,
                    wait=False, timeout=None, final=None):
    DOUBLE_CHAINS.append(list(ids))
    if len(DOUBLE_CHAINS) == 1:
        # hold the first chain open: the second caller must meet a RUNNING
        # import, which is exactly the moment the rule is about
        DOUBLE_GATE.wait(20)
    if final is not None:
        final[:] = list(targets or [])
    return [{"id": sid, "label": f"Script {sid}", "stats": {}, "error": None}
            for sid in ids]


script_runners.run_chain = _blocking_chain
first = {}


def _first_import():
    with _job_locks.holding([double_album], kind="import", label="First import"):
        first["res"] = imports.finish_album(double_album, dict(DOUBLE_CFG))


_thread = _threading.Thread(target=_first_import, name="mlo-double-first",
                            daemon=True)
_thread.start()
_deadline = time.time() + 30
while time.time() < _deadline and not DOUBLE_CHAINS:
    time.sleep(0.02)
assert DOUBLE_CHAINS, "the first import never reached its chain"
try:
    # (1) a SECOND AUTONOMOUS IMPORT with a job of its own: it must answer the
    #     album's state instead of queueing behind the first and then running
    #     the whole pipeline again. (A caller with NO job identity deliberately
    #     still waits and then runs — an import must never skip its chain; that
    #     contract is `tools/test_job_locks.py`'s "a queued import says what it
    #     waits for".)
    with _job_locks.holding([], kind="import", label="Second import"):
        second = imports.finish_album(double_album, dict(DOUBLE_CFG))
    assert second.get("already_importing") is True, second
    assert second.get("chained") is False and second.get("scripts") == [], second
    assert "already importing this album" in str(second.get("note")), second
    assert len(DOUBLE_CHAINS) == 1, \
        f"a second import must not start a chain of its own: {DOUBLE_CHAINS}"
    # (2) …and the BULK queue (the wizard's finish, the page's import) is the
    #     other path that used to "just re-run the chain" over a library album:
    #     its row for an album an import is on reports that instead.
    bulk = imports.bulk_import([{"path": double_album}], dict(DOUBLE_CFG))
    assert bulk["total"] == 1 and bulk["items"][0]["status"] == "failed", bulk
    assert bulk["items"][0].get("already_importing") is True, bulk["items"][0]
    assert "already importing this album" in str(bulk["items"][0]["error"]), \
        bulk["items"][0]
    assert len(DOUBLE_CHAINS) == 1, DOUBLE_CHAINS
finally:
    DOUBLE_GATE.set()
    _thread.join(30)
    script_runners.run_chain = _saved_chain_fn
assert first["res"].get("chained") is True, first.get("res")
assert len(DOUBLE_CHAINS) == 1, DOUBLE_CHAINS
# …and once nothing holds the album, the same bulk row DOES run the chain: the
# refusal is about an import that is running, never about the album's history.
_idle = imports.bulk_import([{"path": double_album, "move": False}],
                            dict(DOUBLE_CFG))
assert _idle["items"][0].get("chained") is True, _idle["items"][0]
shutil.rmtree(DOUBLE_MF, ignore_errors=True)
print("one chain per album: all assertions passed")

