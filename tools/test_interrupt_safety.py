#!/usr/bin/env python3
"""Interruption safety: what the auto-updater's restart cannot break.

The bundled watchtower service replaces this container's image whenever a new
release appears, so this app is restarted — SIGTERM, then SIGKILL when the stop
grace runs out — at arbitrary moments, in the middle of a script, an import, a
download or a tag write. These checks pin the three defences, in order of
strength, and they are the evidence for the audit table in the module
docstrings of mlo.atomic / server.interrupt_recovery:

1. EVERY WRITER IS ATOMIC — the bytes go into a temp file beside the
   destination, are fsynced, and are moved on with ONE os.replace, so a kill
   at any instant leaves either the old file or the new one. Checked per
   format by injecting the failure exactly at the rename (the original must
   stay byte-identical and parseable), and once for real by killing a
   subprocess in the middle of a repeated large tag write.
2. AN INTERRUPTED RUN IS RECOVERABLE AND SAYS SO — the startup sweep deletes
   this app's own leftover temp files (with a log line naming each), reports
   the download area it will not touch, reconciles a framework album whose
   audio arrived, and reports the jobs a shutdown had to abandon.
3. SHUTDOWN IS HONEST — it refuses to start new work, waits a bounded time
   for what is running while saying what it waits for, and records what it
   could not finish so the next start reconciles exactly those jobs.

The library is a throwaway folder with files the app wrote itself (ffmpeg makes
the audio); nothing here touches a real music folder. The one writer that used to
be unfixable — a third-party tool rewriting a file in place, which rsgain's
``easy`` mode did — now scans instead and the app writes (section 1c), so every
write in this app is atomic and every one of them is checked here.

Run:  python tools/test_interrupt_safety.py
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# BEFORE the app's modules import: every state path this app resolves without
# an explicit music folder (wishes.db, artcache, the config) comes from this
# env var first (mlo.paths.read_music_folder_guess), so the whole run is
# isolated inside the temp library below.
music = tempfile.mkdtemp(prefix="mlo-interrupt-")
os.environ["MLO_MUSIC_FOLDER"] = music

# Pointing the app at a different music folder makes it (by design) rewrite the
# repo-local stub config.json that records which folder owns .mlo/data — the
# same thing every suite in this repo does. Keep the operator's own pointer
# and put it back when this test is done.
_STUB = os.path.join(ROOT, "config.json")
try:
    with open(_STUB, "rb") as _fh:
        _STUB_BEFORE = _fh.read()
except OSError:
    _STUB_BEFORE = None

from mlo import atomic                      # noqa: E402
from mlo.audio import AudioFile             # noqa: E402
from mlo.paths import (app_data_dir, library_root,  # noqa: E402
                       load_pending, save_pending)
from server import interrupt_recovery, job_locks, script_runners  # noqa: E402
import mlo.paths as _paths                                           # noqa: E402

# This test's library IS in the OS temp dir, which the app warns about on
# every resolution (a one-off stderr line per call): the warning is about a
# real deployment mistake, not about this fixture.
_paths._warn_if_temp_folder = lambda _mf: None

FAILED = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{('  — ' + detail) if detail and not ok else ''}")
    if not ok:
        FAILED.append(name)


def section(text):
    print(f"\n{text}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def tools():
    try:
        from mlo.tools import detect_all_tools
        return detect_all_tools().get("ffmpeg") or {}
    except Exception:
        return {}


FFMPEG = tools().get("ffmpeg_exe")
FFPROBE = tools().get("ffprobe_exe")


def duration(path):
    """The track's length in seconds, via ffprobe ('' when unavailable)."""
    if not FFPROBE:
        return ""
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=60)
        return (out.stdout or "").strip()
    except Exception:
        return ""


def make_track(dest, seconds=2, source="sine=frequency=440"):
    if not FFMPEG:
        return False
    try:
        r = subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi",
                            "-i", f"{source}:duration={seconds}", dest],
                           capture_output=True, timeout=300)
    except Exception:
        return False
    return r.returncode == 0 and os.path.isfile(dest)


def read(path):
    with open(path, "rb") as fh:
        return fh.read()


class killed_at_replace:
    """Make the FINAL rename fail — the instant a SIGKILL lands in.

    Patching ``os.replace`` (not one module's copy of it) is what every atomic
    writer in this app funnels through: mlo.atomic.replace_temp, mlo.audio's
    video placement, mlo.images' conversions. The file's bytes are therefore
    complete in the temp at this instant and the original is untouched, which
    is exactly the worst case a kill can leave.
    """

    def __init__(self, exc=RuntimeError("simulated kill at the rename")):
        self.exc = exc

    def __enter__(self):
        self.real = os.replace
        exc = self.exc

        def boom(*_a, **_k):
            raise exc

        os.replace = boom
        return self

    def __exit__(self, *_exc):
        os.replace = self.real
        return False


def interrupted(write_fn):
    """Run *write_fn* with the final rename failing; True when it raised.

    A failed write is answered by the app printing its own traceback (the tag
    editor reports the failure to the user) — that is not what this test is
    about, so stderr is quiet while the failure is injected.
    """
    noise = io.StringIO()
    raised = False
    with killed_at_replace(), contextlib.redirect_stderr(noise):
        try:
            write_fn()
        except BaseException:
            raised = True
    return raised


def leftover_temps(folder):
    out = []
    for name in os.listdir(folder):
        if atomic.is_temp_name(name):
            out.append(name)
    return sorted(out)


# ---------------------------------------------------------------------------
# 1. Every writer is atomic
# ---------------------------------------------------------------------------
album = os.path.join(music, "Artists", "Atomic Artist", "Atomic Album")
os.makedirs(album, exist_ok=True)

FLAC = os.path.join(album, "01 - Track.flac")
MKV = os.path.join(album, "02 - Clip.mkv")
AVI = os.path.join(album, "03 - Raw.avi")

section("1a. a kill at the rename leaves the original file exactly as it was")

if not FFMPEG:
    check("ffmpeg is available to build fixtures", False,
          "no ffmpeg found — the atomicity checks need one real file per format")
else:
    has_audio = make_track(FLAC, 3)
    check("a real FLAC fixture was built", has_audio, FLAC)

    # ---- tag writes, one per format mutagen can write ----------------------
    # The one write in the engine that used to be non-atomic: mutagen's save()
    # rewrites the file it opened, in place.
    formats = [".flac", ".mp3", ".ogg", ".opus", ".m4a", ".aac", ".aiff", ".wav"]
    for ext in formats:
        path = os.path.join(album, f"tag{ext}")
        if not make_track(path, 2):
            check(f"{ext}: fixture built", False, "ffmpeg could not write it")
            continue
        before = read(path)
        before_dur = duration(path)
        af = AudioFile(path)
        if af.audio is None:
            check(f"{ext}: mutagen can read the fixture", False, str(af.error))
            continue
        raised = interrupted(lambda: af.set_tag("ALBUM", "Interrupted write"))
        after = read(path)
        again = AudioFile(path)
        check(f"{ext}: the interrupted tag write touched no byte of the file",
              after == before, f"{len(before)} bytes -> {len(after)}")
        check(f"{ext}: the file still opens and still has its music",
              again.audio is not None and duration(path) == before_dur,
              f"{again.error} dur {before_dur} -> {duration(path)}")
        check(f"{ext}: no temp file was left behind (the writer cleaned up)",
              leftover_temps(album) == [], str(leftover_temps(album)))
        check(f"{ext}: it still writes normally afterwards",
              again.set_tag("ALBUM", "After the kill") and
              AudioFile(path).get_tag("ALBUM") == "After the kill",
              str(again.error))

    # ---- an embedded lyric and an embedded cover go through the same save --
    before = read(FLAC)
    af = AudioFile(FLAC)
    interrupted(lambda: af.set_lyrics("[00:01.00] interrupted"))
    check("embedded lyrics: interrupted write leaves the FLAC byte-identical",
          read(FLAC) == before and AudioFile(FLAC).audio is not None)
    cover = os.path.join(album, "art.jpg")
    img = make_track(cover, 1)  # any file works for the byte-level check
    if img:
        af = AudioFile(FLAC)
        interrupted(lambda: af.add_embedded_picture(cover))
        check("embedded cover: interrupted write leaves the FLAC byte-identical",
              read(FLAC) == before and AudioFile(FLAC).audio is not None)
        os.remove(cover)

    # ---- video containers: .mkv is rewritten in place, others remuxed -----
    make_track(MKV, 2, source="testsrc=duration=2:size=64x64:rate=5")
    make_track(AVI, 2, source="testsrc=duration=2:size=64x64:rate=5")
    if os.path.isfile(MKV):
        before = read(MKV)
        af = AudioFile(MKV)
        interrupted(lambda: af.set_video_tags({"TITLE": "Interrupted"}))
        check("mkv: an interrupted tag rewrite leaves the original byte-identical",
              read(MKV) == before and AudioFile(MKV).audio is not None)
    if os.path.isfile(AVI):
        before = read(AVI)
        before_dur = duration(AVI)
        af = AudioFile(AVI)
        interrupted(lambda: af.set_video_tags({"TITLE": "Interrupted"}))
        check("a non-mkv video: the source is byte-identical after an interruption",
              read(AVI) == before)
        check("a non-mkv video: no half-written .mkv was left at the final name",
              not os.path.exists(os.path.join(album, "03 - Raw.mkv")))
        check("a non-mkv video: the source still probes as before",
              duration(AVI) == before_dur)
    check("only temp files this app owns are left after all that",
          all(atomic.is_temp_name(n) for n in leftover_temps(album)),
          str(leftover_temps(album)))

# ---------------------------------------------------------------------------
# 1b. a REAL kill in the middle of a large write
# ---------------------------------------------------------------------------
section("1b. a real SIGKILL during a large tag write")

BIG = os.path.join(album, "big.flac")
big_built = make_track(BIG, 45, source="anoisesrc=d=45:c=pink") if FFMPEG else False
if not big_built:
    check("a large FLAC fixture was built", False, "needs ffmpeg")
else:
    size = os.path.getsize(BIG)
    before_probe = duration(BIG)
    check("the fixture is large enough to be killed mid-write", size > 2_000_000,
          f"{size} bytes")

    killer = os.path.join(music, "kill_writer.py")
    with open(killer, "w", encoding="utf-8") as fh:
        fh.write(
            "import os, sys, time\n"
            "sys.path.insert(0, os.environ['MLO_ROOT'])\n"
            "from mlo.audio import AudioFile\n"
            "path, start = sys.argv[1], int(sys.argv[2])\n"
            "i = start\n"
            "while True:\n"
            "    af = AudioFile(path)\n"
            "    af.defer_save(True)\n"
            "    af.set_tag('ALBUM', 'Kill test %d' % i)\n"
            "    af.set_tag('COMMENT', 'x' * 400)\n"
            "    af.flush()\n"
            "    i += 1\n")
    env = dict(os.environ, MLO_ROOT=ROOT)
    killed_mid_write = False
    for attempt in range(1, 5):
        proc = subprocess.Popen([sys.executable, killer, BIG, str(attempt)],
                                env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        time.sleep(0.6 + 0.4 * attempt)
        proc.kill()                 # TerminateProcess: no unwinding, like SIGKILL
        proc.wait(timeout=30)
        af = AudioFile(BIG)
        probe = duration(BIG)
        if af.audio is None or probe != before_probe:
            check(f"attempt {attempt}: the file survived the kill intact", False,
                  f"error={af.error} duration {before_probe} -> {probe}")
            break
        check(f"attempt {attempt}: the killed file still opens with its music intact",
              True)
    else:
        killed_mid_write = True
    check("the file is parseable by mutagen after every kill",
          AudioFile(BIG).audio is not None, str(AudioFile(BIG).error))
    check("its tags are whole: the album either kept the old value or holds a"
          " complete new one",
          str(AudioFile(BIG).get_tag("ALBUM") or "") == ""
          or str(AudioFile(BIG).get_tag("ALBUM")).startswith("Kill test"),
          repr(AudioFile(BIG).get_tag("ALBUM")))
    temps = leftover_temps(album)
    check("at most this app's own temp files were left by the kills",
          all(atomic.is_temp_name(n) for n in temps), str(temps))

# ---------------------------------------------------------------------------
# 1c. rsgain is handed a scan, never a file to rewrite
# ---------------------------------------------------------------------------
section("1c. the ReplayGain pass asks rsgain to SCAN, and writes the tags itself")

# `rsgain easy` rewrites a track's tags IN PLACE (TagLib has no other mode), so
# a SIGKILL inside it is the one write no rename can make safe. The batch pass
# must ask for a scan — `custom -s s`, whose printed table is the text rsgain
# would have stored — and put the four tags on the file through mlo.atomic like
# every other writer here. The stub below answers as rsgain does and records
# what it was asked for; a run that regressed to `easy` fails the first check.
from types import SimpleNamespace                                          # noqa: E402
import mlo.loudness as _loudness                                           # noqa: E402
from mlo.config import DEFAULT_CONFIG                                      # noqa: E402
from mlo.tools import detect_all_tools                                     # noqa: E402

rg_album = os.path.join(music, "rg-album")
os.makedirs(rg_album, exist_ok=True)
rg_tracks = [os.path.join(rg_album, f"{i:02d} - Track.flac") for i in (1, 2)]
rg_built = all(os.path.isfile(t) and os.path.getsize(t) > 0 for t in rg_tracks) \
    or all(make_track(t) for t in rg_tracks)
rg_calls = []
_real_run_tool = _loudness.run_tool


def _fake_rsgain(cmd, **_kw):
    rg_calls.append(list(cmd))
    rows = ["Filename\tLoudness (LUFS)\tGain (dB)\tPeak\t Peak (dB)"
            "\tPeak Type\tClipping Adjustment?"]
    for path in cmd[cmd.index("-q") + 1:]:
        rows.append(f"{os.path.basename(path)}\t-20.00\t-2.50\t0.500000"
                    f"\t-6.02\tSample\tN")
    rows.append("Album\t-20.00\t-2.50\t0.500000\t-6.02\tSample\tN")
    return SimpleNamespace(returncode=0, stdout="\n".join(rows) + "\n", stderr="")


if not rg_built:
    check("the ReplayGain fixture could be built (ffmpeg present)", False)
elif not (detect_all_tools().get("rsgain") or {}).get("rsgain_exe"):
    print("note: rsgain is not installed — the scan-only contract was not checked")
else:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(music_folder=music, targets=[rg_album], worker_limit=1,
               dr_replaygain_enabled=True, write_replaygain_tags=True,
               write_dynamic_range_tags=False, replaygain_skip_existing=False,
               force_dr_replaygain=False)
    _loudness.run_tool = _fake_rsgain
    try:
        rg_stats = _loudness.run_calc_dr_replaygain(cfg)
    finally:
        _loudness.run_tool = _real_run_tool
    scans_only = bool(rg_calls) and all(
        "easy" not in c and "-s" in c and c[c.index("-s") + 1] == "s"
        for c in rg_calls)
    check("every rsgain call is a SCAN (`custom -s s`), never `easy`",
          scans_only, str([c[:4] for c in rg_calls]))
    check("the scan is asked for the album's own files, album gain included",
          all("-a" in c and "-O" in c for c in rg_calls)
          and len(rg_calls[0]) - rg_calls[0].index("-q") - 1 == len(rg_tracks),
          str(rg_calls[0] if rg_calls else "no call"))
    values = {os.path.basename(t): str(AudioFile(t).get_tag("REPLAYGAIN_TRACK_GAIN") or "")
              for t in rg_tracks}
    albums = {str(AudioFile(t).get_tag("REPLAYGAIN_ALBUM_GAIN") or "")
              for t in rg_tracks}
    check("the four tags the SCAN reported landed on the files, written by the app",
          all(v == "-2.50 dB" for v in values.values())
          and albums == {"-2.50 dB"},
          f"track={values} album={albums}")
    check("the pass reports the files it wrote", rg_stats.get("modified_count") == 2,
          str(rg_stats))
    temps = leftover_temps(rg_album)
    check("the atomic write left no temp file behind",
          all(atomic.is_temp_name(n) for n in temps), str(temps))

# ---------------------------------------------------------------------------
# 2. An interrupted run is recoverable, and says so
# ---------------------------------------------------------------------------
section("2. the startup sweep reconciles what a killed run left")

staging = os.path.join(music, ".mlo", "downloads", "Half Imported Album")
inflight = os.path.join(music, ".mlo", "incomplete")
data_dir = app_data_dir(music)
os.makedirs(staging, exist_ok=True)
os.makedirs(inflight, exist_ok=True)
os.makedirs(data_dir, exist_ok=True)

# a staging folder an interrupted import never moved in
make_track(os.path.join(staging, "01 - Arrived.flac"), 2) if FFMPEG else None
with open(os.path.join(staging, "notes.txt"), "w", encoding="utf-8") as fh:
    fh.write("user file\n")
# a slskd transfer that resumes by itself — must never be deleted
partial = os.path.join(inflight, "other.album.01.flac.part")
with open(partial, "wb") as fh:
    fh.write(b"\x00" * 2048)

# the app's own leftovers: one abandoned temp in the library, one in the state
# dir, and two files a user could legitimately have written themselves
abandoned = os.path.join(album, ".mlo_tmp_no_meta_cover.jpg")
with open(abandoned, "wb") as fh:
    fh.write(b"\xff\xd8half a cover")
state_temp = os.path.join(data_dir, "import_prompts.json.tmp")
with open(state_temp, "w", encoding="utf-8") as fh:
    fh.write("{}")
user_files = {
    os.path.join(album, "cover.jpg"): b"\xff\xd8a real cover",
    os.path.join(album, "notes.tmp"): b"a user's own scratch file\n",
    os.path.join(album, "back.part"): b"a user's own part file\n",
}
for path, blob in user_files.items():
    with open(path, "wb") as fh:
        fh.write(blob)

# a framework album whose audio HAS arrived (the download landed, the process
# died before the marker was cleared) — and one whose wish is gone
framework = os.path.join(music, "Artists", "Pending Artist", "Pending Album")
os.makedirs(framework, exist_ok=True)
make_track(os.path.join(framework, "01 - Filled.flac"), 2) if FFMPEG else None
save_pending(framework, {"pending": True, "wish_id": 424242,
                         "title": "Filled", "artist": "Pending Artist"})
orphan = os.path.join(music, "Artists", "Pending Artist", "Orphan Album")
os.makedirs(orphan, exist_ok=True)
save_pending(orphan, {"pending": True, "wish_id": 9999999,
                      "title": "Orphan", "artist": "Pending Artist"})

# what a shutdown records when it has to give up on a running job
journal = os.path.join(data_dir, interrupt_recovery.JOURNAL_NAME)
with open(journal, "w", encoding="utf-8") as fh:
    json.dump({"version": 1, "at": "2026-01-01T00:00:00",
               "reason": "container stopped while jobs were running",
               "jobs": [{"kind": "import_queue", "job": "import-queue",
                         "label": "import queue (1/3)", "status": "running",
                         "paths": [staging]}]}, fh)

lines = []
summary = interrupt_recovery.startup_recovery({"music_folder": music},
                                              log=lines.append)
said = "\n".join(lines)

check("it reports what it deleted, by name",
      any("mlo_tmp_no_meta_cover.jpg" in line for line in lines), said)
check("the abandoned library temp is gone", not os.path.exists(abandoned))
check("the abandoned state-dir temp is gone", not os.path.exists(state_temp))
check("a user's cover.jpg is left exactly as it was",
      os.path.exists(os.path.join(album, "cover.jpg"))
      and read(os.path.join(album, "cover.jpg")) == user_files[
          os.path.join(album, "cover.jpg")])
check("a user's own .tmp file in the library is NOT deleted",
      os.path.exists(os.path.join(album, "notes.tmp")))
check("a user's own .part file in the library is NOT deleted",
      os.path.exists(os.path.join(album, "back.part")))
check("the staging folder is reported, not touched",
      os.path.isdir(staging)
      and os.path.exists(os.path.join(staging, "notes.txt"))
      and any(s["kind"] == "staging" for s in summary["staging"]), str(summary["staging"]))
check("the in-flight download is reported, not touched",
      os.path.exists(partial)
      and any(s["kind"] == "incomplete download" for s in summary["staging"]))
check("a framework album that holds audio has its marker cleared",
      load_pending(framework) is None)
check("...and the log says its script chain still has to run",
      any("Pending Album" in line and "chain" in line for line in lines), said)
check("an orphan framework album is reported, not deleted",
      load_pending(orphan) is not None and os.path.isdir(orphan)
      and any("Orphan Album" in line for line in lines), said)
check("the abandoned job from the journal is reported, with the queue row named",
      any("import_queue" in line and "Half Imported Album" in line
          for line in lines), said)
check("...and the journal is cleared, so it is reported once",
      not os.path.exists(journal))

clean = tempfile.mkdtemp(prefix="mlo-interrupt-clean-")
os.makedirs(os.path.join(clean, ".mlo", "data"), exist_ok=True)
clean_log = []
interrupt_recovery.startup_recovery({"music_folder": clean}, log=clean_log.append)
check("a clean start reports nothing to recover",
      any("nothing to recover" in line for line in clean_log),
      "\n".join(clean_log))
shutil.rmtree(clean, ignore_errors=True)

section("2b. the temp-name rule never claims a user's file")
check("a plain .tmp in the library is not a temp the sweep may delete",
      not atomic.is_temp_name("notes.tmp"))
check("a plain .part in the library is not a temp the sweep may delete",
      not atomic.is_temp_name("back.part"))
check("...but the state dir's own .tmp is",
      atomic.is_temp_name("import_prompts.json.tmp", in_state_dir=True))
check("this app's working names are recognised",
      atomic.is_temp_name(".mlo_tmp_opttmp_cover.jpg")
      and atomic.is_temp_name(".mlo_covers_ab12.tmp")
      and atomic.is_temp_name("cover.flac.mlo-tmp-1234-deadbeef"))

# ---------------------------------------------------------------------------
# 3. Shutdown is honest
# ---------------------------------------------------------------------------
section("3. shutdown refuses new work, waits, and records what it abandons")

held = os.path.join(music, "Artists", "Atomic Artist", "Atomic Album")
stop_holding = threading.Event()


def hold_a_job():
    with job_locks.holding([held], kind="scripts", label="Format All"):
        stop_holding.wait(timeout=30)


# a queue row that a kill would leave running, and a real claimed job
real_status = __import__("server.import_queue", fromlist=["x"]).status
q = __import__("server.import_queue", fromlist=["x"])
q.status = lambda: {"state": "running", "done": 1, "total": 3,
                    "current": staging, "started_at": time.time()}
try:
    worker = threading.Thread(target=hold_a_job, daemon=True)
    worker.start()
    for _ in range(100):
        if job_locks.jobs():
            break
        time.sleep(0.05)

    running = interrupt_recovery.running_jobs()
    check("the running script job is seen by the registry it claims in",
          any(j["kind"] == "scripts" for j in running), str(running))
    check("...with the paths it holds", any(held in j["paths"] for j in running),
          str(running))
    check("a running import-queue row is seen too",
          any(j["kind"] == "import_queue" for j in running), str(running))

    during = interrupt_recovery.shutdown(grace=2, log=lines.append)
    said = "\n".join(lines)
    check("the shutdown logs what it is waiting for, by name",
          "waiting for scripts" in said and "Format All" in said, said)
    check("...and says how long it gave the abandoned job",
          "gave up on" in said, said)
    check("it reports the jobs it abandoned",
          len(during["abandoned"]) >= 1, str(during))

    recorded = json.load(open(journal, encoding="utf-8"))
    check("it recorded them for the next start",
          os.path.isfile(journal)
          and all(r["status"] == "running" for r in recorded["jobs"]),
          json.dumps(recorded)[:300])
    check("...naming the paths they held, so recovery can find them",
          any(held in (r.get("paths") or []) for r in recorded["jobs"]),
          json.dumps(recorded)[:300])

    check("new work is refused while shutting down",
          interrupt_recovery.is_shutting_down())
    refused = ""
    try:
        script_runners.run_chain({"music_folder": music}, [1], wait=False)
    except script_runners.RunBusy as exc:
        refused = str(exc)
    check("a script / import chain cannot START during shutdown",
          "shutting down" in refused, refused or "it ran")

    stop_holding.set()
    worker.join(timeout=10)
    time.sleep(0.3)

    # the loop closes: the next start reports exactly what the shutdown wrote
    fresh = []
    rows = interrupt_recovery.startup_recovery({"music_folder": music},
                                               log=fresh.append)
    check("the next start reports the abandoned jobs and clears the journal",
          len(rows["jobs"]) == len(recorded["jobs"]) and not os.path.exists(journal),
          str(rows["jobs"]))

    # -----------------------------------------------------------------
    # A chain that is RUNNING when the shutdown begins must stop at a script
    # boundary — uvicorn waits for the run's background task before the
    # lifespan teardown, so a chain that ignored the flag is what held the
    # container open past its stop grace (measured: `docker stop` waited 96 s
    # for one). The shutdown flag is already set above, so a chain entered
    # here is exactly the chain that was running when the signal landed.
    # -----------------------------------------------------------------
    section("3b. a run in flight stops at a script boundary, and says so")

    started = []
    real_run_script = script_runners.run_script

    def _counting_run_script(sid, cfg, **_kw):
        started.append(sid)
        return {"script": sid, "stats": {}, "targets": []}

    script_runners.run_script = _counting_run_script
    said = io.StringIO()
    try:
        with contextlib.redirect_stdout(said):
            results = script_runners._run_chain_locked(
                {"music_folder": music, "targets": None}, [1, 2, 3])
    finally:
        script_runners.run_script = real_run_script
    out = said.getvalue()
    check("no further script is started once the shutdown has begun",
          started == [], str(started))
    check("the chain says how many scripts it did not run, by name",
          "not run" in out and "Format lyrics" in out, out.strip()[-200:])
    check("...and returns no result for them", results == [], str(results))

    # nothing running: the same shutdown waits zero time and records nothing
    q.status = real_status
    again = interrupt_recovery.shutdown(grace=2, log=lambda _l: None)
    check("with nothing running it records nothing",
          again["abandoned"] == [] and not os.path.exists(journal),
          str(again))
finally:
    q.status = real_status
    interrupt_recovery._shutting_down.clear()
    stop_holding.set()

# ---------------------------------------------------------------------------
shutil.rmtree(music, ignore_errors=True)
if _STUB_BEFORE is not None:
    with open(_STUB, "wb") as _fh:
        _fh.write(_STUB_BEFORE)
print(f"\n{len(FAILED)} failure(s)")
sys.exit(1 if FAILED else 0)
