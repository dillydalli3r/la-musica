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
from mlo.config import load_config
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
       # the RateYourMusic link step is a network lookup: this suite runs
       # offline (tools/test_rym_links.py covers it with stubbed HTTP)
       "rym_links_auto": False}

# --------------------------------------------------------------------------- #
# The chain a config describes
# --------------------------------------------------------------------------- #
assert imports.DEFAULT_CHAIN == [2, 3, 11, 1, 13, 18, 8, 5, 6, 7, 9, 12, 14, 15, 10, 4], \
    imports.DEFAULT_CHAIN
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
assert sorted(script_runners.RUNNERS) == list(range(1, 19)), sorted(script_runners.RUNNERS)
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
empty = imports.finish_album(album, CFG)
assert empty == {"path": os.path.normpath(album), "scripts": [], "chain": [], "errors": []}, empty
missing = imports.finish_album(os.path.join(ROOT, "nope"), {"import_scripts": [4]})
assert missing["chain"] == [4] and missing["errors"] == ["album folder not found"], missing

# --------------------------------------------------------------------------- #
# defer_tagging: the auto-import stages and places, and does NOT tag
# --------------------------------------------------------------------------- #
# A non-empty chain, both auto fetches on: the default call must run all
# three, the deferred one none of them — while the RYM stamp, the metadata
# step and the cover step run either way.
DF_CFG = {"music_folder": MF, "import_scripts": [4, 3],
          "advisory_auto_fetch": True, "instrumental_auto_fetch": True,
          "rym_links_auto": True}
assert imports.chain_for(DF_CFG) == [4, 3], imports.chain_for(DF_CFG)

defer_album = staging_album("Defer Album")
DF_PATH = os.path.normpath(defer_album)
_seen = {}
_real_steps = {n: getattr(imports, n) for n in
               ("stamp_rym_links", "fetch_advisories", "fetch_instrumentals",
                "run_metadata_step", "run_cover_step")}
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
    script_runners.run_chain = _spy("chain", [])

    staged = imports.finish_album(defer_album, DF_CFG, defer_tagging=True)
    deferred_seen = {k: len(v) for k, v in _seen.items()}
    deferred_args = {k: list(v) for k, v in _seen.items()}
    _seen.clear()
    full = imports.finish_album(defer_album, DF_CFG)
    default_seen = {k: len(v) for k, v in _seen.items()}
    default_args = {k: list(v) for k, v in _seen.items()}
finally:
    for _n, _fn in _real_steps.items():
        setattr(imports, _n, _fn)
    script_runners.run_chain = _real_run_chain

# the three staging steps ran, on the album, exactly once
assert deferred_seen == {"rym": 1, "metadata": 1, "cover": 1}, deferred_seen
assert deferred_args["rym"][0][0] == (DF_PATH, DF_CFG), deferred_args["rym"]
assert deferred_args["metadata"][0][0] == (DF_PATH, DF_CFG), deferred_args["metadata"]
# the tag-writing work did NOT: no advisory, no instrumental, no chain
assert "advisory" not in deferred_seen, deferred_seen
assert "instrumental" not in deferred_seen, deferred_seen
assert "chain" not in deferred_seen, deferred_seen

# the returned dict is still the documented one, with an EMPTY chain/scripts
assert staged["path"] == DF_PATH, staged
assert staged["chain"] == [] and staged["scripts"] == [], staged
assert staged["errors"] == [], staged
for key in ("path", "scripts", "chain", "errors"):
    assert key in staged, (key, staged)

# …and the DEFAULT call (no flag) keeps today's behaviour: chain + both fetches
assert full["chain"] == [4, 3], full
assert default_seen == {"rym": 1, "metadata": 1, "cover": 1,
                        "advisory": 1, "instrumental": 1, "chain": 1}, default_seen
assert default_args["chain"][0][0][1] == [4, 3], default_args["chain"]
assert default_args["chain"][0][1]["targets"] == [DF_PATH], default_args["chain"]
assert full["scripts"] == [], full

# the auto-import call site is the reason the flag exists: it must pass it
from server import soulseek_auto as _auto

_auto_calls = []
_real_finish_album = imports.finish_album


def _capture(album_dir, cfg=None, progress=None, force=None, *,
             defer_tagging=False):
    _auto_calls.append((os.path.normpath(album_dir), defer_tagging))
    return {"path": os.path.normpath(album_dir), "scripts": [], "chain": [],
            "errors": []}


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
assert _auto_calls == [(DF_PATH, True)], _auto_calls

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
    # the Soulseek download check: a mismatch is a WARNING in the job log
    from server import soulseek_auto
    soulseek_auto._job["log"] = []
    _acoustid.available = lambda cfg=None: True

    def _match(paths, cfg=None, progress=None):
        return {"available": True, "note": "", "albums": [{
            "path": paths[0], "release_group_id": "rg-OTHER",
            "release_group_title": "Other Pressing", "matched": 9, "total": 9}]}

    _real_match = imports.acoustid_match
    imports.acoustid_match = _match
    try:
        soulseek_auto._verify_acoustid(os.path.normpath(album),
                                       {"release_group_id": "rg-WANT"}, {"import_acoustid": True})
    finally:
        imports.acoustid_match = _real_match
finally:
    _acoustid.fpcalc_path, _acoustid.available = _real_fpcalc, _real_available

msgs = " | ".join(line["msg"] for line in soulseek_auto.job_state()["log"])
assert "WARNING" in msgs and "rg-OTHER" in msgs and "rg-WANT" in msgs, msgs
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
    assert _written["01 - track.wav"]["GENRE"] == "Shoegaze; Noise Pop", _written["01 - track.wav"]

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

    # Nobody states a rating -> the tag stays ABSENT. A missing advisory means
    # "unrated"; writing 0 would claim the audio is clean.
    for tags in _written.values():
        tags.pop("ITUNESADVISORY")
    _intg.resolve_advisory_route = lambda **kw: {
        "value": None, "source": None, "checked": ["deezer-isrc", "apple-album"]}
    adv = imports.fetch_advisories([_stamp_lib], CFG)
    assert adv["updated"] == 0 and adv["values"] == {} and adv["sources"] == {}, adv
    for tags in _written.values():
        assert "ITUNESADVISORY" not in tags, tags
finally:
    _audio.AudioFile = _real_audiofile
    _intg.genre_chain = _real_chain
    _intg.resolve_advisory_route = _real_resolve_advisory

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
