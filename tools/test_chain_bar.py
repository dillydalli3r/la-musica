#!/usr/bin/env python3
"""The wizard's progress strip, driven by the frames a REAL chain publishes.

The wizard reports every action to one strip (web/src/pages/ImportWizard.tsx's
ActionBar), and two things can disagree in it: the action the user started, and
the relay frame the engine publishes over the websocket the header bar draws.
This is the contract that keeps them from disagreeing:

  * a run claims both surfaces with its own zero state the moment it really
    starts (`server/script_runners.run_start_frame`), and every frame of a
    chained run carries the whole-step pair `steps`, while the frames an IMPORT
    publishes before the chain is a bare done/total with NO pair;
  * so the strip draws the run's own frames, and while an import stage is still
    what is running it says so under ITS OWN name — a stage's "4/8" is never
    drawn as the chain's progress, which is what a half-filled bar at the start
    of a chain was;
  * and the Finish step's own bar draws the same numbers, because both read one
    source.

The first half runs a real chain on a scratch album and records every frame the
relay would have received, in order. The second half hands those frames to
tools/check_chain_bar.mjs, which serves the REAL page on a scratch port (8011+,
never the owner's 8000), presses "Run all", pushes the frames into
the store the app draws from and asserts the label, readout and bar a user
would see after each one.

No network, no real scripts: the two chain steps are stub runners that drive a
bar, exactly like a real runner does.

Run:  python tools/test_chain_bar.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo import stats as _stats                   # noqa: E402
from mlo.stats import _HookPbar                   # noqa: E402
from server import job_locks as _jl               # noqa: E402
from server import script_runners                 # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = tempfile.mkdtemp(prefix="mlo_chain_bar_")
MF = os.path.join(ROOT, "music")
ALBUM = os.path.join(MF, "Artists", "Chain Bar")
os.makedirs(ALBUM, exist_ok=True)
with open(os.path.join(ALBUM, "01 - Track.flac"), "w", encoding="utf-8") as fh:
    fh.write("not really audio")

TICKS = 2
frames = []


def record(done, total, desc, steps=None):
    """Stands in for the WebSocket relay the header bar — and the wizard's
    strip — is drawn from: every frame, in order, exactly as a client receives
    it. *steps* defaults because `job_locks.publish` also serves hooks that
    take only the 3-argument frame."""
    frames.append({"done": done, "total": total, "desc": desc,
                   "steps": list(steps) if steps else None})


def ticking(cfg):
    """A script that drives its own bar, like every real runner does."""
    pbar = _HookPbar(TICKS, "Step")
    for _ in range(TICKS):
        pbar.update(1)
        time.sleep(0.01)
    return {"modified_count": 0}


real_runners = dict(script_runners.RUNNERS)
real_hook = getattr(_stats, "progress_hook", None)
_stats.progress_hook = record
try:
    # What is on screen the moment the wizard's chain button is pressed: an
    # import's stage frame — a bare 4/8, no step pair — still running before
    # the chain starts. This is the frame the strip must never draw as the
    # chain's own, and the second one is a stage that keeps publishing AFTER
    # the action started (the same publisher, one step further): that one is
    # the stage's own to name, never the chain's.
    _jl.publish(4, 8, "Previous album — organizing")
    _jl.publish(5, 8, "Previous album — organizing")
    assert len(frames) == 2 and all(f["steps"] is None for f in frames), frames
    stage, later_stage = dict(frames[0]), dict(frames[1])
    frames.clear()
    script_runners.RUNNERS[3] = ("Optimize FLACs", ticking)
    script_runners.RUNNERS[5] = ("Process images", ticking)
    results = script_runners.run_chain({"music_folder": MF}, [3, 5], targets=[ALBUM])
finally:
    _stats.progress_hook = real_hook
    script_runners.RUNNERS.clear()
    script_runners.RUNNERS.update(real_runners)

assert [r.get("id") for r in results] == [3, 5], results
# The run's own zero state is the FIRST thing the surfaces are told, and from
# there every frame of a chained run carries the pair: the readout can never
# show the stage's 4/8, and the pair never restarts at a stage's numbers.
first = frames[0]
assert (first["done"], first["total"]) == (0, 2) and first["steps"] == [1, 2], first
assert "#1/2" in first["desc"], first
assert all(f["steps"] is not None for f in frames), frames
assert frames[-1]["steps"] == [2, 2], frames
assert not any((f["done"], f["total"]) == (4, 8) for f in frames), frames
assert frames[-1]["total"] == 2 and frames[-1]["done"] == 2, frames[-1]

payload_path = os.path.join(ROOT, "chain-bar-frames.json")
with open(payload_path, "w", encoding="utf-8") as fh:
    json.dump({"stage": stage, "later_stage": later_stage, "frames": frames,
               # The sentence the claim itself raises when another job already
               # holds the album — what `/api/import/finish` answers 409 with,
               # and what the step's own line has to read as a refusal.
               "busy": _jl.refusal(ALBUM, {"label": "Import Album", "job": "job-4"})}, fh)

# The strip itself: node + Vite serve the real page on a scratch port, and the
# frames above are pushed into the store it draws from. Exit 2 is this repo's
# "the tooling is not installed" (the exit-2 convention every tools/check_*.mjs follows).
ui = subprocess.run(["node", os.path.join("tools", "check_chain_bar.mjs"), payload_path],
                    cwd=REPO, capture_output=True, text=True)
if ui.returncode == 0:
    print(ui.stdout.strip())
elif ui.returncode == 2:
    print("SKIPPED  the wizard's progress strip check (node, web/node_modules or "
          "playwright missing): " + (ui.stderr.strip().splitlines() or [""])[0])
else:
    raise AssertionError("the wizard's progress strip check failed:\n"
                         + ui.stdout + ui.stderr)

shutil.rmtree(ROOT, ignore_errors=True)
print(f"chain bar: the run's own {len(frames)} frames are what the strip draws "
      f"({TICKS} ticks per script over 2 scripts)")

# --------------------------------------------------------------------------- #
# The LANES: an album-lane step at a time, a free-lane step beside the spine
# --------------------------------------------------------------------------- #
# The scheduler's own contract (mlo.scripts.SCRIPT_LANES + SCRIPT_DEPS), proved
# with runners that could not pass if the chain were still a straight line: two
# steps that each wait for the other's START only both finish when they ran at
# the same time, and two album-lane steps that record their own intervals can
# never overlap.
import threading                                                     # noqa: E402

LANE_ROOTS = tempfile.mkdtemp(prefix="mlo_chain_lanes_")
LANE_ALBUM = os.path.join(LANE_ROOTS, "Artists", "Lane Album")
os.makedirs(LANE_ALBUM, exist_ok=True)
with open(os.path.join(LANE_ALBUM, "01 - Track.flac"), "w", encoding="utf-8") as fh:
    fh.write("not really audio")

lane_frames = []


def lane_record(done, total, desc, steps=None):
    lane_frames.append({"desc": desc, "steps": list(steps) if steps else None})


real_runners = dict(script_runners.RUNNERS)
real_hook = getattr(_stats, "progress_hook", None)
_stats.progress_hook = lane_record
try:
    # 25 is a REAL free-lane script (Fetch artist images); 3 and 5 are real
    # album-lane scripts. Their bodies are replaced — the lanes are what is
    # under test, not what the scripts fetch.
    spine_started = threading.Event()
    free_started = threading.Event()
    intervals = []
    lock = threading.Lock()

    def spine_one(cfg):
        with lock:
            if len(intervals) == 0:
                intervals.append([time.time(), None])
        spine_started.set()
        # If the free lane were serialized behind the spine this would time out
        # and the assert below would say so, instead of hanging the suite.
        assert free_started.wait(10), "the free-lane step never ran beside the spine"
        with lock:
            intervals[0][1] = time.time()
        return {"modified_count": 0}

    def spine_two(cfg):
        start = time.time()
        time.sleep(0.05)
        with lock:
            intervals.append([start, time.time()])
        return {"modified_count": 0}

    def free_step(cfg):
        free_started.set()
        assert spine_started.wait(10), "the spine never started its step"
        return {"modified_count": 0}

    script_runners.RUNNERS[3] = ("Optimize FLACs", spine_one)
    script_runners.RUNNERS[5] = ("Process images", spine_two)
    script_runners.RUNNERS[25] = ("Fetch artist images", free_step)
    results = script_runners.run_chain({"music_folder": LANE_ROOTS}, [3, 5, 25],
                                       targets=[LANE_ALBUM])
finally:
    _stats.progress_hook = real_hook
    script_runners.RUNNERS.clear()
    script_runners.RUNNERS.update(real_runners)

assert [r.get("id") for r in results] == [3, 5, 25], results
assert intervals and all(end is not None for _s, end in intervals), intervals
assert intervals[0][1] <= intervals[1][0], \
    f"two album-lane steps overlapped: {intervals}"
# The free step was announced on the LOG, never as a frame: the bar and the row
# speak for the spine, and a side step's label there would read as the run's
# own position (a frame whose text names the free script would have to be
# "#n/3 · Fetch artist images").
assert not any("Fetch artist images" in f["desc"] for f in lane_frames), lane_frames
assert any("Optimize FLACs" in f["desc"] for f in lane_frames), lane_frames
assert lane_frames[-1]["steps"] == [3, 3], lane_frames[-1]
shutil.rmtree(LANE_ROOTS, ignore_errors=True)
print("chain lanes: the free-lane step ran beside the spine (no album-lane "
      "step overlapped another), and the spine's frames are the strip's")
