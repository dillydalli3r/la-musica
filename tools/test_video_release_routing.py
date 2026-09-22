#!/usr/bin/env python3
"""A music video is fetched from the network its MEDIUM names.

What this pins, in the order the acquisition pipeline takes the decision:

  * `acquisition_route` — the ONE place the routing rule lives: a VIDEO release
    (its recordings are videos) published as DIGITAL MEDIA goes to YouTube; a
    music video on a DISC (DVD/Blu-ray/VHS/Video CD) keeps the Soulseek path; an
    AUDIO release is untouched, digital medium or not. A payload that cannot
    answer (no medium, no track list) answers "soulseek", which is what every
    release took before this routing existed.
  * the YouTube branch runs INSIDE the auto-import job: no search is sent to
    slskd at all (the search seam is asserted NOT called, and slskd is reported
    DOWN for that job), every track is looked up by artist + title and
    downloaded, the files land in the album folder under the names the rest of
    the pipeline reads a track's place out of (`_parse_trackno` maps them back
    to disc+position), the MB identity/MEDIA/SOURCE tags land, and the album
    finishes through the SAME `_import` — organizer, then
    `imports.finish_album`.
  * the disc and audio cases keep today's path: the search IS run, YouTube is
    never consulted, and a search that finds nothing still ends on the
    Soulseek dead end rather than in silence.
  * a track that cannot be found or downloaded is that TRACK's failure — it is
    counted (`error_count`), named in the log and in the job's note, and the
    rest of the album is still imported; a release with NOTHING found ends the
    way the Soulseek search ends (the wish offer), never as a silent success.
  * the vocabularies know the new spelling: SOURCE "YouTube" is canonical in
    mlo.tagtext, and a wish an auto-import job offers for such a release
    records source "youtube" (server.wishes).

No network, no slskd, no yt-dlp, no ffmpeg: yt-dlp is stubbed, slskd is faked,
the tag layer is an in-memory double and the music folder is a temp directory.

Run:  python tools/test_video_release_routing.py   (exit 0 pass, 1 fail)
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --------------------------------------------------------------------------- #
# hermeticity: the same idiom the import suites use — the stub config file and
# the music-folder redirect are in place BEFORE server.* is imported, so the
# developer's real library is never read or written.
# --------------------------------------------------------------------------- #
REAL_MUSIC_FOLDER = ""
try:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        REAL_MUSIC_FOLDER = str((json.load(f) or {}).get("music_folder") or "")
except Exception:
    pass

REDIRECT = tempfile.mkdtemp(prefix="mlo-vidroute-redirect-")
MF = tempfile.mkdtemp(prefix="mlo-vidroute-music-")
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

import mlo.audio as audio_mod  # noqa: E402
import mlo.flac as flac_mod  # noqa: E402
from mlo import tagtext  # noqa: E402
from server import imports, soulseek, soulseek_auto as auto, wishes  # noqa: E402
from server import main as mlo_main  # noqa: E402
from server import youtube  # noqa: E402

LIB = os.path.join(MF, "Artists")
DD = os.path.join(MF, ".mlo", "downloads")
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
    return os.path.normcase(os.path.abspath(str(p or "")))


# --------------------------------------------------------------------------- #
# the tag layer, as an in-memory double: this suite is about WHERE a release
# comes from and what the pipeline does with the files, not about ffmpeg
# writing MKV metadata (tools/test_video_pipeline.py covers that side).
# --------------------------------------------------------------------------- #
_TAGS = {}


class FakeTrack:
    """The AudioFile surface the stampers use, over a dict.

    `kind` is "video" for the containers the real layer tags through ffmpeg,
    which is what makes `set_video_tags` — the ONE-pass writer — the path
    `_stamp_mb_tags` must take for them."""

    def __init__(self, path):
        self.path = str(path)
        self.error = ""
        self.audio = object()                      # non-None: "readable"
        self.kind = ("video" if self.path.lower().endswith(
            (".mkv", ".webm", ".mov", ".vob", ".avi")) else "flac")
        self.tags = _TAGS.setdefault(norm(self.path), {})
        self.video_writes = 0
        self.tag_writes = 0

    def get_tag(self, name):
        return self.tags.get(str(name).upper())

    def set_tag(self, name, value):
        self.tag_writes += 1
        self.tags[str(name).upper()] = str(value)
        return True

    def set_video_tags(self, mapping):
        self.video_writes += 1
        for k, v in (mapping or {}).items():
            self.tags[str(k).upper()] = str(v)
        return True

    def delete_tag(self, name):
        self.tags.pop(str(name).upper(), None)
        return True


def tags_of(path):
    return _TAGS.get(norm(path), {})


# --------------------------------------------------------------------------- #
# the fixtures: a release whose medium and recordings say what it is
# --------------------------------------------------------------------------- #
TITLES = ["First Video", "Second Video", "Third Video", "Fourth Video"]
MBID = "11111111-2222-3333-4444-555555555555"


def release(medium="Digital Media", tracks=3, video=True, mbid=MBID):
    return {
        "id": mbid,
        "title": "Video Singles",
        "date": "2004-05-01",
        "country": "US",
        "release_group_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "catalog_number": "", "barcode": "", "label": "",
        "status": "Official", "primary_type": "Album", "secondary_types": [],
        "release_type": "album",
        "artists": [{"name": "Test Artist", "mbid": "ffffffff-0000-1111-2222-333333333333"}],
        "medium_formats": [medium],
        "media": [{"position": i + 1, "disc": 1, "title": TITLES[i],
                   "length": 200000, "recording_mbid": f"rec-{i + 1:08d}",
                   "artist_credit": "Test Artist", "video": video}
                  for i in range(tracks)],
    }


# --------------------------------------------------------------------------- #
# the stubs: yt-dlp, slskd, the organizer, the chain
# --------------------------------------------------------------------------- #
CANDIDATES = []          # every best_candidate call
DOWNLOADS = []           # every download call
UNFINDABLE = set()       # titles YouTube has nothing for
SEARCHES = []            # every _search_queries call


def fake_candidate(artist, title, want_seconds=None, config=None):
    CANDIDATES.append({"artist": artist, "title": title, "want": want_seconds})
    if title in UNFINDABLE:
        return None
    return {"id": f"vid{len(CANDIDATES):08d}"[:11],
            "url": f"https://www.youtube.com/watch?v=vid{len(CANDIDATES):08d}"[:50],
            "title": f"{artist} - {title} (Official Video)",
            "duration": want_seconds, "channel": f"{artist} - Topic",
            "height": 1080, "abr": 128}


def fake_download(url, dest_dir, config=None):
    DOWNLOADS.append({"url": url, "dest": dest_dir})
    vid = url.rstrip("/").split("=")[-1] or "vid"
    # yt-dlp's own template: the UPLOAD's title plus the video id, in MKV.
    name = f"{vid} - upload [{vid}].mkv"
    path = os.path.join(dest_dir, name)
    os.makedirs(dest_dir, exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\0" * 4096)
    return {"path": path, "container": "mkv", "height": 1080, "abr": 128,
            "format_id": "137+140"}


def fake_search(slsk_mod, queries, wait_s, usable=None, response_limit=0):
    SEARCHES.append(list(queries))
    return [], [], 0


ORGANIZED = []
FINISHED = []


def fake_organize(req):
    ORGANIZED.append(list(req.paths))
    folder = list(req.paths)[0]
    return {"results": [{"album_root": folder, "path": folder}]}


def fake_finish_album(album_dir, cfg=None, progress=None, force=None, release=None):
    FINISHED.append({"album_dir": album_dir, "release": release})
    return {"chained": True, "scripts": [{"id": "14"}], "errors": [],
            "autonomy": {"missing": {}}}


def install_stubs():
    auto._search_queries = fake_search
    youtube.best_candidate = fake_candidate
    youtube.download = fake_download
    youtube.ytdlp_available = lambda config=None: True
    youtube.enabled = lambda config=None: True
    audio_mod.AudioFile = FakeTrack
    mlo_main.organize = fake_organize
    imports.finish_album = fake_finish_album
    flac_mod.convert_album_lossless = lambda album_dir, cfg=None: {"modified_count": 0}
    auto._account_metadata = lambda album_dir, cfg: {}


def set_slskd(up, logged_in=True):
    soulseek.is_running = lambda *a, **k: up
    soulseek.web_up = lambda *a, **k: up
    soulseek.server_state = lambda *a, **k: {"isLoggedIn": logged_in}


def run_job(rel):
    """Start the release through the REAL entry point and wait for it to settle.

    `start_job` is what "Add to library", the wish worker and the bulk queue
    all call, so the routing is asserted through the path a user's click takes
    — job registry, stages, queue row and all — not by calling _run()."""
    r = auto.start_job(release=rel, queries=["Test Artist Video Singles"],
                       confirm_lossy=False)
    assert r.get("ok"), r
    jid = r["job"]["id"]
    deadline = time.time() + 30
    while time.time() < deadline:
        state = auto.job_state(jid)
        if state["state"] in ("done", "error", "cancelled"):
            return jid, state
        time.sleep(0.02)
    raise AssertionError(f"job {jid} never settled: {auto.job_state(jid)}")


def clear_registry():
    with auto._lock:
        for jid in list(auto._jobs):
            auto._jobs[jid]["state"] = "done"
        auto._jobs.clear()
        del auto._order[:]
    auto._primary = 0


def join_chain():
    """The import chain runs on a daemon thread; wait for it to report."""
    deadline = time.time() + 10
    while time.time() < deadline and not FINISHED:
        time.sleep(0.02)


# --------------------------------------------------------------------------- #
print("\n(1) the routing rule, on the payloads the app really holds")
# --------------------------------------------------------------------------- #
eq(auto.acquisition_route(release("Digital Media")), "youtube",
   "a Digital Media release whose recordings are videos is fetched from YouTube")
eq(auto.acquisition_route(release("DVD")), "soulseek",
   "a music video on a DVD keeps the Soulseek path")
eq(auto.acquisition_route(release("Blu-ray")), "soulseek",
   "a Blu-ray too")
eq(auto.acquisition_route(release("VHS")), "soulseek", "and a VHS")
eq(auto.acquisition_route(release("Video CD")), "soulseek", "and a Video CD")
eq(auto.acquisition_route(release("Digital Media", video=False)), "soulseek",
   "a DIGITAL AUDIO release is untouched (its recordings are not videos)")
eq(auto.acquisition_route(release("CD", video=False)), "soulseek",
   "a CD album is untouched")
eq(auto.acquisition_route(release("Digital Media", 2, True, mbid="x")), "youtube",
   "a two-track video single routes the same way")
eq(auto.acquisition_route({"medium_formats": ["Digital Media"],
                           "media": [{"video": True}]}), "youtube",
   "a payload that nests its tracks under media[].tracks[] answers too")
# nothing to read -> the conservative answer, i.e. exactly today's pipeline
eq(auto.acquisition_route(None), "soulseek", "no release yet -> Soulseek")
eq(auto.acquisition_route({}), "soulseek", "no payload -> Soulseek")
eq(auto.acquisition_route({"id": "x", "media": [{"video": True}]}), "soulseek",
   "a video release whose MEDIUM nobody stated -> Soulseek (no guessing)")
eq(auto.acquisition_route({"id": "x", "medium_formats": ["Digital Media"]}),
   "soulseek", "a Digital Media release with no track list -> Soulseek")
eq([len(auto.video_tracks(release("Digital Media"))), len(auto.video_tracks(release("CD", video=False)))],
   [3, 0], "video_tracks counts the video recordings, and only those")

# --------------------------------------------------------------------------- #
print("\n(2) the vocabularies know the new spelling")
# --------------------------------------------------------------------------- #
ok("YouTube" in tagtext.SOURCE_VALUES, "SOURCE_VALUES carries YouTube",
   tagtext.SOURCE_VALUES)
eq(tagtext.canonical_text("SOURCE", "youtube"), "YouTube",
   "the canonical spelling of a source tag written in any case")
ok("youtube" in wishes.SOURCES, "the wish source list carries youtube",
   wishes.SOURCES)
_wish = wishes.add_wish("99999999-0000-1111-2222-333333333333",
                        title="A Video Single", source="youtube")
eq(str(_wish.get("source") or ""), "youtube", "and a wish records it")
wishes.delete_wish(_wish["id"])

install_stubs()

# --------------------------------------------------------------------------- #
print("\n(3) a DIGITAL video release: YouTube, inside the same job")
# --------------------------------------------------------------------------- #
# slskd is reported DOWN for this one: the YouTube route must not need it.
set_slskd(False)
CANDIDATES.clear()
DOWNLOADS.clear()
SEARCHES.clear()
ORGANIZED.clear()
FINISHED.clear()
rel = release("Digital Media")
jid, state = run_job(rel)
join_chain()

eq(SEARCHES, [], "no search was sent to slskd")
eq(len(CANDIDATES), 3, "one YouTube lookup per track")
eq([c["title"] for c in CANDIDATES], TITLES[:3],
   "each looked up by its own title")
eq([c["artist"] for c in CANDIDATES], ["Test Artist"] * 3,
   "credited to the release's artist")
eq([c["want"] for c in CANDIDATES], [200.0] * 3,
   "with the track's own length, the ±5 s filter")
eq(len(DOWNLOADS), 3, "one download per track")
eq(state["state"], "done", "the job finished")
eq(auto.job_stage(state), "completed", "and the queue row says completed")
result = state.get("result") or {}
ok(result.get("imported"), "the album was imported", result)
eq(int(result.get("error_count") or 0), 0, "with no track missing")

album = str(result.get("album_path") or "")
eq(norm(album), norm(os.path.join(LIB, "Test Artist - Video Singles")),
   "into the library folder named after the release")
files = sorted(os.listdir(album)) if os.path.isdir(album) else []
eq(files, ["1-01 First Video.mkv", "1-02 Second Video.mkv", "1-03 Third Video.mkv"],
   "the downloaded files are named as the pipeline expects them")
# ...which is not a claim about the test's own string: the pipeline's OWN
# reader must map each name back to the track it belongs to.
eq([auto._parse_trackno(os.path.join(album, f)) for f in files],
   [(1, 1), (1, 2), (1, 3)],
   "and _parse_trackno reads disc+position back out of every one")
ok(not any(os.path.isfile(os.path.join(DD, "YouTube", "Test Artist - Video Singles", f))
           for f in files),
   "the staging folder no longer holds them (the import moved the album)")

tags = tags_of(os.path.join(album, "1-01 First Video.mkv"))
eq(tags.get("SOURCE"), "YouTube", "SOURCE says where the album came from")
eq(tags.get("MEDIA"), "Digital Media", "MEDIA is the release's own medium")
eq(tags.get("MUSICBRAINZ_ALBUMID"), MBID, "the release identity is stamped")
eq(tags.get("MUSICBRAINZ_TRACKID"), "rec-00000001", "and the track identity")
eq(tags.get("TRACKNUMBER"), "1", "with the track's number")
eq(tags.get("DISCNUMBER"), "1", "and its disc")
eq(tags.get("TITLE"), "First Video", "and its title")
eq(tags_of(os.path.join(album, "1-03 Third Video.mkv")).get("TITLE"), "Third Video",
   "every track, not just the first")
eq(ORGANIZED, [[album]], "the organize step ran on the imported album")
ok(FINISHED and norm(FINISHED[0]["album_dir"]) == norm(album),
   "and the configured chain (imports.finish_album) ran on it", FINISHED)
eq((FINISHED[0]["release"] or {}).get("id") if FINISHED else "", MBID,
   "carrying the release the job fetched")
ok("YouTube" in " ".join(str(l.get("msg")) for l in state.get("log") or []),
   "the job log says where the album came from")
auto.forget(jid)
clear_registry()

# --------------------------------------------------------------------------- #
print("\n(4) a track that fails is reported; the album still imports")
# --------------------------------------------------------------------------- #
UNFINDABLE.clear()
UNFINDABLE.add("Second Video")
CANDIDATES.clear()
SEARCHES.clear()
FINISHED.clear()
ORGANIZED.clear()
jid, state = run_job(release("Digital Media"))
join_chain()
UNFINDABLE.clear()
eq(SEARCHES, [], "still no Soulseek search")
log = " · ".join(str(l.get("msg")) for l in state.get("log") or [])
ok("Second Video" in log and "no usable YouTube upload found" in log,
   "the missing track is named in the job log", log[-300:])
result = state.get("result") or {}
eq(int(result.get("error_count") or 0), 1, "and counted in the result")
ok("Second Video" in str(result.get("note") or ""),
   "the job's note says a track is missing", result.get("note"))
ok(result.get("imported"), "the two tracks that WERE found are imported", result)
album = str(result.get("album_path") or "")
eq(sorted(os.listdir(album)) if os.path.isdir(album) else [],
   ["1-01 First Video.mkv", "1-03 Third Video.mkv"],
   "and only those are in the album folder")
eq(state["state"], "done", "the job is not a failure — it is a partial album")
auto.forget(jid)
clear_registry()

# --------------------------------------------------------------------------- #
print("\n(5) nothing found at all: the Soulseek dead end, never silence")
# --------------------------------------------------------------------------- #
UNFINDABLE.update(TITLES)
SEARCHES.clear()
ORGANIZED.clear()
jid, state = run_job(release("Digital Media"))
UNFINDABLE.clear()
eq(state["state"], "error", "the job failed rather than reporting success")
err = str((state.get("result") or {}).get("error") or "")
ok("on YouTube" in err, "with a reason that names YouTube", err)
eq(wishes.outcome_of(err), "not_found",
   "classified the way the Soulseek dead end is: not_found, not a transient "
   "outage to re-ask on a timer")
eq(str((state.get("result") or {}).get("outcome") or ""), "not_found",
   "which the settled job's own result carries")
ok(all(t in " ".join(str(l.get("msg")) for l in state.get("log") or [])
       for t in TITLES[:3]), "and every track it looked for, by name")
eq(str(auto.job_stage(state)), "failed", "the queue row is a failed job")
eq(ORGANIZED, [], "nothing was imported")
auto.forget(jid)
clear_registry()

# A background job (confirm_lossy False) never asks a wish to become a wish —
# the same rule the Soulseek path follows. The interactive path offers the
# wish list and ends as a DONE job carrying the wish, which is how "nothing
# found" reaches the user instead of a bare error.
asked = []
_real_ask = auto._ask_to_wish


def fake_ask(rel_, queries, waited, cfg, confirm_lossy, error="", source=""):
    asked.append({"error": error, "source": source, "confirm_lossy": confirm_lossy})
    return {"wished": True, "wish_id": 7, "album_path": None,
            "staging_path": None, "imported": 0, "organized": 0,
            "organize_error": None}


UNFINDABLE.update(TITLES)
auto._ask_to_wish = fake_ask
try:
    jid, state = run_job(release("Digital Media"))
finally:
    auto._ask_to_wish = _real_ask
    UNFINDABLE.clear()
eq(state["state"], "done", "an accepted wish offer settles the job as done")
ok((state.get("result") or {}).get("wished"),
   "carrying the wish, not a pretend download", state.get("result"))
eq([a["source"] for a in asked], ["youtube"],
   "and the offer says it looked on YouTube")
ok(asked and "on YouTube" in asked[0]["error"],
   "with the same reason the failure would have carried", asked)
auto.forget(jid)
clear_registry()

# --------------------------------------------------------------------------- #
print("\n(6) a cancelled YouTube album leaves nothing behind")
# --------------------------------------------------------------------------- #
# One worker and a cancel raised from inside the first download: the tracks
# after it are never started, and what the first one fetched is swept — the
# same treatment the Soulseek path gives a cancelled candidate's bytes.
_real_download = youtube.download
_real_workers = cfgmod.load_config


def cancelling_download(url, dest_dir, config=None):
    got = _real_download(url, dest_dir, config)
    auto.cancel()
    return got


_cfg = cfgmod.load_config()


class _OneWorker(dict):
    """load_config() with worker_limit=1, so the cancel lands between tracks."""


def one_worker_config():
    cfg = dict(_real_workers())
    cfg["worker_limit"] = 1
    return cfg


CANDIDATES.clear()
SEARCHES.clear()
ORGANIZED.clear()
FINISHED.clear()
youtube.download = cancelling_download
cfgmod.load_config = one_worker_config
auto.load_config = one_worker_config
try:
    jid, state = run_job(release("Digital Media", tracks=4))
finally:
    youtube.download = _real_download
    cfgmod.load_config = _real_workers
    auto.load_config = _real_workers
eq(state["state"], "cancelled", "the job reports the cancel")
eq(FINISHED, [], "nothing was imported")
eq(ORGANIZED, [], "and nothing was organized")
ok(not os.path.isdir(os.path.join(DD, "YouTube", "Test Artist - Video Singles")),
   "the staged files were swept with it")
ok(len(CANDIDATES) < 4, "the tracks behind the cancel were never looked up",
   CANDIDATES)
auto.forget(jid)
clear_registry()

# --------------------------------------------------------------------------- #
print("\n(7) a DISC music video and an AUDIO release keep the Soulseek path")
# --------------------------------------------------------------------------- #
install_stubs()
set_slskd(True)
for label, rel in (("a DVD music video", release("DVD")),
                   ("a Blu-ray music video", release("Blu-ray")),
                   ("an audio album", release("CD", video=False)),
                   ("a digital audio album", release("Digital Media", video=False))):
    SEARCHES.clear()
    CANDIDATES.clear()
    DOWNLOADS.clear()
    FINISHED.clear()
    jid, state = run_job(rel)
    ok(SEARCHES, f"{label}: the Soulseek search IS run", SEARCHES)
    eq(CANDIDATES + DOWNLOADS, [], f"{label}: YouTube is never consulted")
    eq(FINISHED, [], f"{label}: nothing is imported from a search that found nothing")
    eq(state["state"], "error", f"{label}: and the job fails honestly")
    err = str((state.get("result") or {}).get("error") or "")
    ok("No candidate folder" in err, f"{label}: with the Soulseek reason", err)
    auto.forget(jid)
    clear_registry()

# --------------------------------------------------------------------------- #
print()
if FAILED:
    print(f"video release routing: {len(FAILED)} FAILED")
    for label in FAILED:
        print(f"  - {label}")
    sys.exit(1)
print("video release routing: all assertions passed")
