#!/usr/bin/env python3
"""A music video is fetched from the network its MEDIUM names — and from the
other one when that network comes up empty.

What this pins, in the order the acquisition pipeline takes the decision:

  * `acquisition_route` — the ONE place the routing rule lives: a VIDEO release
    (its recordings are videos) published as DIGITAL MEDIA or WEB goes to
    YouTube FIRST; a music video on a DISC (DVD/Blu-ray/VHS/Video CD) searches
    Soulseek first; an AUDIO release is untouched, digital medium or not, and a
    payload that cannot answer (no medium, no track list) answers "soulseek",
    which is what every release took before this routing existed. "Web" is the
    same medium as "Digital Media" (mlo.tagtext.MEDIA_VALUES and
    soulseek_auto._is_digital agree on both spellings).
  * the YouTube branch runs INSIDE the auto-import job: no search is sent to
    slskd at all (the search seam is asserted NOT called, and slskd is reported
    DOWN for that job), every track is looked up by artist + title and
    downloaded, the files land in the album folder under the names the rest of
    the pipeline reads a track's place out of (`_parse_trackno` maps them back
    to disc+position), the MB identity/MEDIA/SOURCE tags land (MEDIA from the
    medium MusicBrainz states, SOURCE per file), and the album finishes through
    the SAME `_import` — organizer, then `imports.finish_album`.
  * a DISC music video whose Soulseek search comes back with nothing is not
    reported as not found: YouTube is tried per track, and an album it serves
    imports and settles the job as done (that is the "physical media first,
    the other network second" half of issue #53).
  * a Web release with YouTube switched off (or yt-dlp missing) is not a
    failure before any fetch either: it falls through to the Soulseek search,
    which is the attempt it has always deserved.
  * the NETWORK's fallback is really attempted: an unreachable or signed-out
    slskd does not stand in for a search. The per-track miss then reports that
    state as its reason — never "no copy", which is a claim about a search
    nobody ran.
  * the audio cases keep today's path: the search IS run, YouTube is never
    consulted, and a search that finds nothing still ends on the Soulseek dead
    end rather than in silence.
  * a track YouTube has no usable upload for falls back to the NETWORK
    (`soulseek_auto.fetch_video_on_soulseek`): one search of the track's own
    artist + title, the best of the peer copies that are really that track
    (video container + artist + title in the file name, fastest peer first),
    queued and waited out, then staged under the track's own name so the same
    import stamps it. A peer copy that is NOT the track (another artist,
    another song, an audio container) is not a fallback, and a track NEITHER
    source serves is reported naming both.
  * the grab route (`POST /api/videos/download-youtube`) does not wait for a
    transfer: it searches briefly, queues the file as the app's own download
    and answers `{ok, source: "soulseek", queued: true, candidate}`; a request
    where neither source has the video answers `ok: false` with a reason that
    names both, and an unreachable slskd is reported as that (not as "no
    copy").
  * a track that cannot be found or downloaded is that TRACK's failure — it is
    counted (`error_count`), named in the log and in the job's note, and the
    rest of the album is still imported; a release with NOTHING found ends the
    way the Soulseek search ends (the wish offer), never as a silent success.
  * the vocabularies know the new spelling: SOURCE "YouTube" and MEDIA "Web"
    are canonical in mlo.tagtext, and a wish an auto-import job offers for such
    a release records source "youtube" (server.wishes).

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
# What the fake Soulseek search answers with, in slskd's response shape (see
# soulseek.search_results). Empty is "the network has nothing", which is what
# every test above this one wants; the fallback sections below fill it.
SLSK_FILES = []          # [{user, file, size, duration, speed}]
ENQUEUED = []            # every enqueue_download call
ARRIVED = []             # every _wait_for_files call


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


def fake_search(slsk_mod, queries, wait_s, usable=None, response_limit=0,
cancel_check=None):
    SEARCHES.append(list(queries))
    if not SLSK_FILES:
        return [], [], 0
    made = [{"username": f["user"], "file": f["file"], "size": int(f["size"]),
             "duration": f.get("duration"), "slot": True,
             "ext": os.path.splitext(f["file"])[1].lstrip("."),
             "speed": int(f.get("speed") or 2 * 1024 * 1024), "queue": 0}
            for f in SLSK_FILES]
    return [(queries[0], {"state": "Completed", "isComplete": True,
                          "responses": made})], [], 0


def slsk_video(title, artist="Test Artist", ext=".mkv", size=16384,
               duration=200.0, user="peer1", speed=2 * 1024 * 1024):
    """One peer's copy of a music video, as a search response describes it."""
    return {"user": user, "size": size, "duration": duration, "speed": speed,
            "file": f"@@{user}\\Music Videos\\{artist} - {title}{ext}"}


def fake_enqueue_download(username, files, cfg=None):
    ENQUEUED.append({"username": username, "files": list(files)})
    return True


def fake_wait_for_files(slsk_mod, ddir, username, wanted, timeout_s,
                        cancel_check=None, phase="download",
                        queue_budget_s=None, on_start=None):
    """slskd having delivered the transfer: the file lands where it would,
    under the download dir, and the wait returns `{remote: local}`."""
    ARRIVED.append({"username": username,
                    "files": [w["filename"] for w in wanted]})
    got = {}
    for w in wanted:
        leaf = str(w["filename"]).replace("\\", "/").rsplit("/", 1)[-1]
        path = os.path.join(ddir, str(username), leaf)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"\0" * int(w.get("size") or 0))
        got[w["filename"]] = path
    return got


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
    # The fallback's own two seams: enqueueing the transfer, and waiting for
    # it. The real ones are httpx against slskd and a poll of the download
    # tree — neither is this suite's subject (see
    # tools/test_soulseek_candidates.py for those).
    soulseek.enqueue_download = fake_enqueue_download
    auto._wait_for_files = fake_wait_for_files
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
eq(auto.acquisition_route(release("Web")), "youtube",
   "a Web release is the same medium as Digital Media: YouTube first")
eq(auto._is_digital(release("Web")), True,
   "the digital test knows both spellings of a download-only release")
eq(auto._is_digital({"medium_formats": ["Web", "Digital Media"], "media": []}), True,
   "and a payload stating either one is digital")
eq(auto._is_digital({"medium_formats": ["Web", "CD"]}), False,
   "…while a Web release bundled with a disc is not")
eq(auto.acquisition_route(release("Web", video=False)), "soulseek",
   "an AUDIO release published as Web is untouched like any other")
eq(auto._release_medium(release("Web")), "Web",
   "and the album says the medium MusicBrainz states, not 'Digital Media'")
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
ok("Web" in tagtext.MEDIA_VALUES, "MEDIA_VALUES knows the Web spelling",
   tagtext.MEDIA_VALUES)
eq(tagtext.canonical_text("MEDIA", "web"), "Web",
   "and canonicalises it like every other medium")
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
# slskd is DOWN for this one (see section 3): the network's fallback is asked
# ANYWAY — a state read taken when the album started is not a search, and it is
# what the per-track miss reports, rather than "no copy", which would be a
# claim about a search nobody ran.
UNFINDABLE.clear()
UNFINDABLE.add("Second Video")
CANDIDATES.clear()
SEARCHES.clear()
FINISHED.clear()
ORGANIZED.clear()
jid, state = run_job(release("Digital Media"))
join_chain()
UNFINDABLE.clear()
eq(SEARCHES, [["Test Artist Second Video"]],
   "the network WAS asked for the track YouTube missed, slskd down or not")
log = " · ".join(str(l.get("msg")) for l in state.get("log") or [])
ok("Second Video" in log and "no usable YouTube upload found" in log,
   "the missing track is named in the job log", log[-300:])
ok("Soulseek is not running" in log,
   "and the miss says WHY — the unreachable slskd, never a network that "
   "answered 'nothing'", log[-300:])
ok("no Soulseek copy either" not in log,
   "…which is a claim about a search that could not run", log[-300:])
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
print("\n(7) an AUDIO release is never routed to YouTube")
# --------------------------------------------------------------------------- #
# The routing rule only ever moves a VIDEO release: an album whose recordings
# are audio keeps the search it always had, even a digital one, and YouTube is
# never consulted for it.
install_stubs()
set_slskd(True)
for label, rel in (("an audio album", release("CD", video=False)),
                   ("a digital audio album", release("Digital Media", video=False)),
                   ("an audio album published as Web", release("Web", video=False))):
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
print("\n(8) a track YouTube does not have comes from Soulseek")
# --------------------------------------------------------------------------- #
# One track YouTube has nothing for, and TWO peers offering exactly that video
# — named after the track, in a container the library supports. The album still
# imports (two tracks from YouTube, one from the network), the fastest peer is
# the one asked, and the job says where the third track came from.
install_stubs()
set_slskd(True)
UNFINDABLE.clear()
UNFINDABLE.add("Second Video")
CANDIDATES.clear()
DOWNLOADS.clear()
SEARCHES.clear()
ENQUEUED.clear()
ARRIVED.clear()
ORGANIZED.clear()
FINISHED.clear()
SLSK_FILES[:] = [slsk_video("Second Video", user="peer1", speed=512 * 1024),
                 slsk_video("Second Video", user="peer0", speed=4 * 1024 * 1024)]
jid, state = run_job(release("Digital Media"))
join_chain()
UNFINDABLE.clear()
SLSK_FILES[:] = []
eq(SEARCHES, [["Test Artist Second Video"]],
   "the network was searched ONCE, for the track YouTube missed, by its own "
   "artist and title")
eq(len(DOWNLOADS), 2, "YouTube served the other two")
eq(len(ENQUEUED), 1, "ONE transfer was queued")
eq(ENQUEUED[0]["username"], "peer0",
   "from the FASTEST peer that has it, not the first the search listed")
eq([f["filename"] for f in ENQUEUED[0]["files"]],
   ["@@peer0\\Music Videos\\Test Artist - Second Video.mkv"],
   "the remote FILE itself — a music video is one file, not a folder")
eq(len(ARRIVED), 1, "and the transfer was waited out")
ok(state["state"] == "done", "the job finished", str(state.get("error") or ""))
result = state.get("result") or {}
eq(int(result.get("error_count") or 0), 0, "with every track present")
album = str(result.get("album_path") or "")
eq(sorted(os.listdir(album)) if os.path.isdir(album) else [],
   ["1-01 First Video.mkv", "1-02 Second Video.mkv", "1-03 Third Video.mkv"],
   "the network's file is named like the tracks YouTube delivered")
eq(tags_of(os.path.join(album, "1-02 Second Video.mkv")).get("TITLE"),
   "Second Video", "and stamped as that track")
log = " · ".join(str(l.get("msg")) for l in state.get("log") or [])
ok("1-02 Second Video.mkv — from peer0" in log,
   "the job log says which peer it came from", log[-400:])
auto.forget(jid)
clear_registry()

# A peer copy that is NOT this track (another artist, another song) is not a
# fallback: the album's tracks are its own recordings, so the miss stands.
UNFINDABLE.add("Second Video")
SLSK_FILES[:] = [slsk_video("Second Video", artist="Some Cover Band"),
                 slsk_video("Another Song", user="peer3"),
                 slsk_video("Second Video", ext=".txt")]
SEARCHES.clear()
ENQUEUED.clear()
ARRIVED.clear()
CANDIDATES.clear()
jid, state = run_job(release("Digital Media"))
UNFINDABLE.clear()
SLSK_FILES[:] = []
eq(SEARCHES, [["Test Artist Second Video"]], "the search ran")
eq(ENQUEUED, [], "and NOTHING was queued for it")
eq(ARRIVED, [], "nothing was waited for either")
result = state.get("result") or {}
eq(int(result.get("error_count") or 0), 1, "the track is still missing")
ok("Second Video" in str(result.get("note") or "")
   and "no usable YouTube upload found" in str(result.get("note") or "")
   and "no Soulseek copy either" in str(result.get("note") or ""),
   "and the line names BOTH sources", result.get("note"))
auto.forget(jid)
clear_registry()

# --------------------------------------------------------------------------- #
print("\n(9) neither source has it: the dead end names both")
# --------------------------------------------------------------------------- #
# Every track missing from YouTube AND nothing usable on the network (slskd is
# UP here, so the searches really run): the job ends on the app's dead end,
# which now names both sources and still reads as `not_found` to the wish
# policy — a release nobody has is not a transient outage.
install_stubs()
set_slskd(True)
UNFINDABLE.update(TITLES)
SLSK_FILES[:] = [slsk_video("First Video", artist="Some Cover Band")]
SEARCHES.clear()
ENQUEUED.clear()
ARRIVED.clear()
ORGANIZED.clear()
FINISHED.clear()
jid, state = run_job(release("Digital Media"))
UNFINDABLE.clear()
SLSK_FILES[:] = []
eq(state["state"], "error", "the job failed rather than reporting success")
err = str((state.get("result") or {}).get("error") or "")
ok("on YouTube or Soulseek" in err, "the dead end names BOTH sources", err)
eq(wishes.outcome_of(err), "not_found",
   "and still classifies as not_found, not as a transient outage")
eq(len(SEARCHES), 3, "every track was looked for on the network")
eq(ENQUEUED, [], "nothing was queued")
eq(ARRIVED, [], "and nothing was waited for")
eq(ORGANIZED, [], "nothing was imported")
log = " · ".join(str(l.get("msg")) for l in state.get("log") or [])
eq(log.count("no Soulseek copy either"), 3,
   "each track's line says what the network did not have either")
auto.forget(jid)
clear_registry()

# --------------------------------------------------------------------------- #
print("\n(10) the grab route queues what YouTube does not have")
# --------------------------------------------------------------------------- #
# The route must not hold a request open for a transfer that takes minutes: it
# searches briefly, queues the file as the app's own download and answers with
# what it queued.
install_stubs()
set_slskd(True)
FOLDER = os.path.join(LIB, "Video Grab Album")
os.makedirs(FOLDER, exist_ok=True)
UNFINDABLE.clear()
UNFINDABLE.add("Third Video")
SLSK_FILES[:] = [slsk_video("Third Video", user="peer2")]
ENQUEUED.clear()
ARRIVED.clear()
SEARCHES.clear()
DOWNLOADS.clear()
r = mlo_main.videos_download_youtube(mlo_main.YoutubeDownloadRequest(
    path=FOLDER, artist="Test Artist", title="Third Video", duration=200))
ok(r.get("ok") is True, "the route answered ok", r)
eq(r.get("source"), "soulseek", "naming the network as the source")
eq(r.get("queued"), True, "with a QUEUED transfer")
ok(not r.get("file"), "and no local file — the bytes are still coming", r)
eq((r.get("candidate") or {}).get("filename"), "Test Artist - Third Video.mkv",
   "the answer names the file it queued")
eq(len(ENQUEUED), 1, "one transfer was queued")
eq(ENQUEUED[0]["username"], "peer2", "from the peer the search named")
eq(ARRIVED, [], "and the ROUTE did not wait for it")
eq(SEARCHES, [["Test Artist Third Video"]], "the search was the track's own")

# Neither source has it: the reason names BOTH.
ENQUEUED.clear()
SEARCHES.clear()
SLSK_FILES[:] = []
r = mlo_main.videos_download_youtube(mlo_main.YoutubeDownloadRequest(
    path=FOLDER, artist="Test Artist", title="Third Video", duration=200))
eq(r.get("ok"), False, "nothing anywhere is not ok")
eq(r.get("candidate"), None, "with no candidate")
err = str(r.get("error") or "")
ok("YouTube" in err and "Soulseek" in err,
   "and a reason that names both sources", err)
eq(SEARCHES, [["Test Artist Third Video"]], "the network was asked")

# An unreachable slskd is SAID, not hidden behind "no copy" — and nothing is
# searched for on a network the app cannot reach.
set_slskd(False)
SEARCHES.clear()
r = mlo_main.videos_download_youtube(mlo_main.YoutubeDownloadRequest(
    path=FOLDER, artist="Test Artist", title="Third Video", duration=200))
eq(r.get("ok"), False, "the answer is still a failure")
ok("Soulseek is not running" in str(r.get("error") or ""),
   "with the REAL reason (slskd is not running)", r)
eq(SEARCHES, [], "and no search was sent to a network that is not there")
set_slskd(True)
UNFINDABLE.clear()

# --------------------------------------------------------------------------- #
print("\n(11) a DISC music video searches Soulseek first, then YouTube")
# --------------------------------------------------------------------------- #
# Issue #53: a music video on physical media is the release worth having, so the
# DISC is searched for first (it is a folder like any pressing) — and a disc
# nobody shares is still a video somebody put on YouTube. The search finding
# nothing must therefore fall back to YouTube per track, and the file must say
# which network produced it.
install_stubs()
set_slskd(True)
SLSK_FILES[:] = []
UNFINDABLE.clear()
CANDIDATES.clear()
DOWNLOADS.clear()
SEARCHES.clear()
ORGANIZED.clear()
FINISHED.clear()
# WHICH network was asked first, in order: both seams write one word into this
# trace, so "Soulseek first, YouTube after it" is asserted as the sequence the
# job really took, not inferred from two counters.
TRACE = []
_real_candidate = youtube.best_candidate
_real_search = auto._search_queries


def traced_candidate(artist, title, want_seconds=None, config=None):
    TRACE.append("youtube")
    return _real_candidate(artist, title, want_seconds, config)


def traced_search(slsk_mod, queries, wait_s, usable=None, response_limit=0,
cancel_check=None):
    TRACE.append("soulseek")
    return _real_search(slsk_mod, queries, wait_s, usable, response_limit)


youtube.best_candidate = traced_candidate
auto._search_queries = traced_search
try:
    jid, state = run_job(release("DVD"))
finally:
    youtube.best_candidate = _real_candidate
    auto._search_queries = _real_search
join_chain()
eq(TRACE[:1], ["soulseek"], "the Soulseek search runs FIRST (the disc is a folder)")
ok(SEARCHES and len(SEARCHES) == 1, "one search window of the release's own templates",
   SEARCHES)
eq(TRACE.count("soulseek"), 1,
   "the network is asked once, before YouTube — not the per-track fallback")
eq(TRACE[1:], ["youtube"] * 3, "and YouTube is then asked for every track of the disc")
eq(len(CANDIDATES), 3, "…once per track, in the release's own order")
eq([c["title"] for c in CANDIDATES], TITLES[:3], "each looked up by its own title")
eq(len(DOWNLOADS), 3, "each of which yt-dlp delivers")
eq(state["state"], "done", "so the album is fetched from the second network")
result = state.get("result") or {}
eq(int(result.get("error_count") or 0), 0, "with nothing missing")
album = str(result.get("album_path") or "")
eq(sorted(os.listdir(album)) if os.path.isdir(album) else [],
   ["1-01 First Video.mkv", "1-02 Second Video.mkv", "1-03 Third Video.mkv"],
   "named exactly as the tracks YouTube delivers are")
first = tags_of(os.path.join(album, "1-01 First Video.mkv"))
eq(first.get("SOURCE"), "YouTube", "the file is reported as fetched from YouTube")
eq(first.get("MEDIA"), "DVD",
   "and MEDIA is the disc the release is pressed on, not 'Digital Media'")
eq(first.get("TITLE"), "First Video", "with the track's own title")
eq(first.get("MUSICBRAINZ_ALBUMID"), MBID, "and the release's identity")
log = " · ".join(str(l.get("msg")) for l in state.get("log") or [])
ok("trying YouTube" in log,
   "the log says the other network is being tried after the empty search",
   log[-400:])
ok("No candidate folder" not in log,
   "and the search's dead end is never reached", log[-400:])
auto.forget(jid)
clear_registry()

# A Blu-ray where the network HAS one of the three: the album still imports,
# and each FILE says which network served it — the per-file origin the job log
# reports is written into the tags too.
UNFINDABLE.clear()
UNFINDABLE.add("Second Video")
SLSK_FILES[:] = [slsk_video("Second Video", user="peer0", speed=4 * 1024 * 1024)]
CANDIDATES.clear()
DOWNLOADS.clear()
SEARCHES.clear()
ENQUEUED.clear()
ARRIVED.clear()
ORGANIZED.clear()
FINISHED.clear()
jid, state = run_job(release("Blu-ray"))
join_chain()
UNFINDABLE.clear()
SLSK_FILES[:] = []
eq(state["state"], "done", "a mixed album finishes")
result = state.get("result") or {}
album = str(result.get("album_path") or "")
eq(sorted(os.listdir(album)) if os.path.isdir(album) else [],
   ["1-01 First Video.mkv", "1-02 Second Video.mkv", "1-03 Third Video.mkv"],
   "with every track of the disc")
eq(len(DOWNLOADS), 2, "YouTube served the two it had")
eq(len(ENQUEUED), 1, "and the network was queued for the one it did not")
eq(tags_of(os.path.join(album, "1-01 First Video.mkv")).get("SOURCE"), "YouTube",
   "the YouTube downloads say YouTube")
eq(tags_of(os.path.join(album, "1-02 Second Video.mkv")).get("SOURCE"), "Soulseek",
   "and the peer's file says Soulseek — per file, not one word for the album")
eq(tags_of(os.path.join(album, "1-03 Third Video.mkv")).get("SOURCE"), "YouTube",
   "…so a mixed album reports which network produced WHICH file")
eq(tags_of(os.path.join(album, "1-02 Second Video.mkv")).get("MEDIA"), "Blu-ray",
   "and MEDIA stays the disc the release is")
auto.forget(jid)
clear_registry()

# --------------------------------------------------------------------------- #
print("\n(12) a Web release with YouTube switched off is still searched")
# --------------------------------------------------------------------------- #
# The route names which network is tried FIRST, never the only one that may be
# tried: with YouTube disabled the release must fall through to the Soulseek
# search (the attempt it has always deserved) instead of raising before any
# fetch — and with nothing on the network either, it ends on the SEARCH's own
# reason, not on a YouTube refusal.
install_stubs()
set_slskd(True)
_real_enabled = youtube.enabled
youtube.enabled = lambda config=None: False
try:
    CANDIDATES.clear()
    SEARCHES.clear()
    ORGANIZED.clear()
    FINISHED.clear()
    jid, state = run_job(release("Web"))
finally:
    youtube.enabled = _real_enabled
eq(CANDIDATES, [], "YouTube is never consulted while it is switched off")
ok(SEARCHES, "but the Soulseek search IS run", SEARCHES)
eq(state["state"], "error", "the job ends on the search's dead end")
err = str((state.get("result") or {}).get("error") or "")
ok("No candidate folder" in err,
   "with the SEARCH's own reason, not a YouTube refusal", err)
eq(ORGANIZED, [], "nothing was imported")
log = " · ".join(str(l.get("msg")) for l in state.get("log") or [])
ok("disabled in Settings" in log,
   "and the log says why YouTube was skipped", log[-400:])
auto.forget(jid)
clear_registry()

# --------------------------------------------------------------------------- #
print("\n(13) a Web release goes to YouTube, and says Web as its medium")
# --------------------------------------------------------------------------- #
install_stubs()
set_slskd(True)
UNFINDABLE.clear()
CANDIDATES.clear()
DOWNLOADS.clear()
SEARCHES.clear()
ORGANIZED.clear()
FINISHED.clear()
jid, state = run_job(release("Web"))
join_chain()
eq(SEARCHES, [], "a Web release is not searched for on the network first")
eq(len(CANDIDATES), 3, "it is fetched from YouTube, one lookup per track")
eq(state["state"], "done", "and imports")
result = state.get("result") or {}
album = str(result.get("album_path") or "")
eq(tags_of(os.path.join(album, "1-01 First Video.mkv")).get("MEDIA"), "Web",
   "MEDIA is the medium MusicBrainz states — Web, not 'Digital Media'")
eq(tags_of(os.path.join(album, "1-01 First Video.mkv")).get("SOURCE"), "YouTube",
   "and SOURCE where it came from")
auto.forget(jid)
clear_registry()

# --------------------------------------------------------------------------- #
print("\n(14) neither network has the DISC: the search's dead end stands")
# --------------------------------------------------------------------------- #
# The fallback is not an excuse to report a success: when YouTube has nothing
# either, the job ends exactly as a Soulseek search that found nothing does.
install_stubs()
set_slskd(True)
UNFINDABLE.update(TITLES)
SLSK_FILES[:] = []
CANDIDATES.clear()
SEARCHES.clear()
ORGANIZED.clear()
FINISHED.clear()
jid, state = run_job(release("Video CD"))
UNFINDABLE.clear()
eq(state["state"], "error", "the job failed rather than reporting success")
err = str((state.get("result") or {}).get("error") or "")
ok("No candidate folder" in err,
   "with the Soulseek search's own reason (that is where the job gave up)", err)
ok(SEARCHES, "the search ran first", SEARCHES)
eq(len(CANDIDATES), 3, "and YouTube was asked for every track before giving up")
eq(ORGANIZED, [], "nothing was imported")
eq(FINISHED, [], "and no chain ran")
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
