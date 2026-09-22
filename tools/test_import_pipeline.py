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
    (the websocket relay's source).

No network, no slskd, no real scripts: the chain is switched off or
monkeypatched everywhere a real run would happen.

Run:  python tools/test_import_pipeline.py
"""
import os
import shutil
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
# catches — and nothing is left out today: 20 (Scan library layout) was the one
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
assert sorted(script_runners.RUNNERS) == list(range(1, 22)), sorted(script_runners.RUNNERS)
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
assert full["note"] == "the script chain did not run", full
assert imports.chain_summary(full) == full["note"], full

# the auto-import's own seam hands the album to the SAME call, with nothing
# deferred — this is the path the user's downloads take
from server import soulseek_auto as _auto

_auto_calls = []
_real_finish_album = imports.finish_album


def _capture(album_dir, cfg=None, progress=None, force=None, **kwargs):
    _auto_calls.append((os.path.normpath(album_dir), dict(kwargs)))
    return {"path": os.path.normpath(album_dir), "scripts": [], "chain": [4],
            "errors": [], "chained": True, "chain_off": False,
            "note": "the script chain ran 1 script"}


imports.finish_album = _capture
try:
    _auto._start_import_chain(DF_PATH, DF_CFG)
    for t in threading.enumerate():          # it stages in a daemon thread
        if t.name == "mlo-soulseek-import-chain":
            t.join(30)
finally:
    imports.finish_album = _real_finish_album
_deadline = time.time() + 10
while not _auto_calls and time.time() < _deadline:
    time.sleep(0.01)
assert _auto_calls == [(DF_PATH, {"release": None})], _auto_calls
# a second call carries the release the auto-import just resolved: that is what
# lets finish_album fetch genres without looking the identity up again
_auto_calls.clear()
imports.finish_album = _capture
try:
    _auto._start_import_chain(DF_PATH, DF_CFG, {"id": "rel-1"})
    for t in threading.enumerate():
        if t.name == "mlo-soulseek-import-chain":
            t.join(30)
finally:
    imports.finish_album = _real_finish_album
_deadline = time.time() + 10
while not _auto_calls and time.time() < _deadline:
    time.sleep(0.01)
assert _auto_calls == [(DF_PATH, {"release": {"id": "rel-1"}})], _auto_calls

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
    # the Soulseek download check: a conflict is a WARNING in the job log, and
    # the release the job matched goes in as the cross-check candidate
    from server import soulseek_auto
    soulseek_auto._job["log"] = []
    _acoustid.available = lambda cfg=None: True
    SEEN = {}

    def _match(paths, cfg=None, progress=None, expect=None):
        SEEN["expect"] = expect
        return {"available": True, "note": "", "ok": True, "code": "conflict",
                "albums": [{
                    "path": paths[0], "release_group_id": "rg-OTHER",
                    "release_group_title": "Other Pressing", "matched": 9,
                    "total": 9, "status": "matched", "conflict": True,
                    "conflicts": [{"kind": "release_group",
                                   "reason": "the audio is release group rg-OTHER "
                                             "but the tags say rg-WANT"}]}]}

    WANT = {"release_group_id": "rg-WANT"}
    _real_match = imports.acoustid_match
    imports.acoustid_match = _match
    try:
        soulseek_auto._verify_acoustid(os.path.normpath(album), WANT,
                                       {"import_acoustid": True})
    finally:
        imports.acoustid_match = _real_match
    assert SEEN["expect"] == WANT, SEEN
    msgs_conflict = " | ".join(line["msg"] for line in soulseek_auto.job_state()["log"])

    # a check that could not RUN says so: never dressed up as "no match"
    def _error_match(paths, cfg=None, progress=None, expect=None):
        return {"available": True, "note": "AcoustID lookup timed out after 30s",
                "ok": False, "code": "lookup_failed",
                "albums": [{"path": paths[0], "status": "error",
                            "code": "lookup_failed", "reason": "AcoustID lookup "
                            "timed out after 30s", "matched": 0, "total": 4}]}

    soulseek_auto._job["log"] = []
    imports.acoustid_match = _error_match
    try:
        soulseek_auto._verify_acoustid(os.path.normpath(album), WANT,
                                       {"import_acoustid": True})
    finally:
        imports.acoustid_match = _real_match
finally:
    _acoustid.fpcalc_path, _acoustid.available = _real_fpcalc, _real_available

msgs = " | ".join(line["msg"] for line in soulseek_auto.job_state()["log"])
assert "WARNING" in msgs and "could not answer" in msgs, msgs
assert "timed out" in msgs and "unverified, not rejected" in msgs, msgs
assert "no release group matched" not in msgs, msgs
assert "rg-OTHER" not in msgs, msgs
# the conflict pass named both release groups, as a warning
assert "WARNING" in msgs_conflict and "rg-OTHER" in msgs_conflict, msgs_conflict
assert "rg-WANT" in msgs_conflict and "pressing or edition" in msgs_conflict, msgs_conflict

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
_stats.progress_hook = lambda done, total, desc: hook_rows.append((done, total, desc))
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
assert len(hook_rows) == 2, hook_rows

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
# start_bulk + job_state: one at a time, and it settles
# --------------------------------------------------------------------------- #
assert imports.job_state()["status"] == "idle", imports.job_state()

gate = threading.Event()
_real_finish = imports.finish_album


def _slow_finish(album_dir, cfg=None, progress=None, force=None):
    gate.wait(10)
    return {"path": os.path.normpath(album_dir), "scripts": [], "chain": [], "errors": []}


job_album = staging_album("Job Album")
imports.finish_album = _slow_finish
try:
    started = imports.start_bulk([{"path": job_album}], CFG)
    assert started["ok"] is True, started
    assert started["job"]["status"] == "running" and started["job"]["kind"] == "bulk", started
    assert started["job"]["total"] == 1 and started["job"]["id"], started
    refused = imports.start_bulk([{"path": job_album}], CFG)
    assert refused == {"ok": False, "error": "bulk import already running"}, refused
finally:
    gate.set()
    imports.finish_album = _real_finish

deadline = time.time() + 20
while time.time() < deadline and imports.job_state()["status"] == "running":
    time.sleep(0.05)
state = imports.job_state()
assert state["status"] == "done", state
assert state["done"] == 1 and state["total"] == 1, state
assert state["finished"] and state["error"] is None, state
assert [i["status"] for i in state["items"]] == ["imported"], state
assert state["items"][0]["album_path"].endswith("Job Album"), state["items"][0]
assert os.path.isdir(os.path.join(LIB, "Job Album")), os.listdir(LIB)

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
    assert adv["answers"] == {} and adv["hits"] == {}, adv
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
                       "advisory": 1, "cover": 1}, dropped
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
                              "advisory": 0, "cover": 1}, res["dropped"]
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
finally:
    _intg.genre_chain = _real_genre_chain
    _intg.resolve_advisory_route = _real_advisory_route
    _lyrics_fetch.fetch_lyrics = _real_fetch_lyrics
    shutil.rmtree(ARRIVED_ROOT, ignore_errors=True)

# --------------------------------------------------------------------------- #
# soulseek.import_completed(finish=...): the chain is opt-in per album
# --------------------------------------------------------------------------- #
from server import soulseek as _slsk

SL_FILES = tempfile.mkdtemp(prefix="mlo_import_pipeline_slsk_")
SL_MF, SL_DD = os.path.join(SL_FILES, "music"), os.path.join(SL_FILES, "downloads")
os.makedirs(SL_MF)


def put_flac(rel):
    """soulseek.py classifies an import by library extensions (.flac, not
    .wav) — nothing decodes it here, the chain is off."""
    p = os.path.join(SL_DD, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"fLaC" + b"\0" * 64)
    return p


put_flac(os.path.join("Peer Album", "01 - a.flac"))
SL_CFG = {"music_folder": SL_MF, "soulseek_download_dir": SL_DD,
          "import_auto_scripts": False, "import_scripts": [],
          "rym_links_auto": False}      # no network in this suite
_real_downloads_state = _slsk.downloads_state
_slsk.downloads_state = lambda cfg=None: []
try:
    quiet = _slsk.import_completed(SL_CFG)                     # default: unchanged
    assert _slsk.last_import_scripts() == [], _slsk.last_import_scripts()
    assert os.path.isdir(os.path.join(SL_MF, "Artists", "Peer Album")), os.listdir(SL_MF)
    put_flac(os.path.join("Second Album", "01 - b.flac"))
    finished = _slsk.import_completed(SL_CFG, finish=True)
finally:
    _slsk.downloads_state = _real_downloads_state

assert finished == [os.path.join(SL_MF, "Artists", "Second Album")], finished
rows = _slsk.last_import_scripts()
assert len(rows) == 1 and rows[0]["path"] == finished[0], rows
assert rows[0]["chain"] == [] and rows[0]["scripts"] == [] and rows[0]["errors"] == [], rows

shutil.rmtree(ROOT, ignore_errors=True)
shutil.rmtree(SL_FILES, ignore_errors=True)
print("import pipeline: all assertions passed")
