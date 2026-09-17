#!/usr/bin/env python3
"""Verification for `GET /api/videos/thumb` — the scrub-preview frames.

The route has to answer with a real JPEG for any time the scrubber asks for,
refuse anything that is not a library video, and NEVER render the same frame
twice while the user drags. This pins exactly that:

  * a JPEG (magic bytes + content type) at t=0 and at a mid-point,
  * a path outside the music folder, and a non-video file inside it, are
    refused,
  * the second identical request is served from the cache — the cached file
    is not rewritten (same mtime, same bytes), which is what "no re-encode"
    means,
  * a negative or absurd `t` is clamped (the negative one lands on t=0's own
    frame, i.e. the same cache entry).

Offline: the fixture is a 2-second testsrc clip rendered by the app's own
ffmpeg. Without ffmpeg there is nothing to verify, so the test exits 2.

Run:  python tools/test_thumbs.py
"""
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --------------------------------------------------------------------------- #
# hermeticity: redirect every app path into a temp folder BEFORE server.main is
# imported (the app resolves its music folder through mlo.paths/config, and
# MLO_MUSIC_FOLDER is what seeds it). Nothing is written into the developer's
# real music folder.
# --------------------------------------------------------------------------- #
REAL_MUSIC_FOLDER = ""
try:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        REAL_MUSIC_FOLDER = str((json.load(f) or {}).get("music_folder") or "")
except Exception:
    pass

REDIRECT = tempfile.mkdtemp(prefix="mlo-thumb-redirect-")
os.environ["MLO_MUSIC_FOLDER"] = REDIRECT

import mlo.config as cfgmod  # noqa: E402
import mlo.paths as pathmod  # noqa: E402

_STUB = os.path.join(REDIRECT, "config.json")
with open(_STUB, "w", encoding="utf-8") as f:
    json.dump({"music_folder": REDIRECT}, f)

for _mod in (cfgmod, pathmod):
    _mod.CONFIG_FILE = _STUB
    if getattr(_mod, "LEGACY_DATA_DIR", None) is not None:
        _mod.LEGACY_DATA_DIR = os.path.join(REDIRECT, "legacy")

_REAL = REAL_MUSIC_FOLDER.replace("\\", "/").rstrip("/")
if _REAL:
    assert not REDIRECT.replace("\\", "/").lower().startswith(_REAL.lower()), \
        f"temp fixture {REDIRECT} sits inside the real music folder {_REAL}"


def _cleanup():
    os.environ.pop("MLO_MUSIC_FOLDER", None)
    shutil.rmtree(REDIRECT, ignore_errors=True)


atexit.register(_cleanup)

from mlo.tools import detect_all_tools  # noqa: E402

_T = detect_all_tools().get("ffmpeg") or {}
FFMPEG = _T.get("ffmpeg_exe")
FFPROBE = _T.get("ffprobe_exe")
if not FFMPEG:
    print("SKIP: no ffmpeg detected — nothing to render a fixture with "
          "(install it under Dependencies)")
    sys.exit(2)

passed = 0


def ok(cond, label):
    global passed
    assert cond, f"FAILED: {label}"
    passed += 1
    print(f"  ok: {label}")


# --------------------------------------------------------------------------- #
# fixture: a 2-second 160x120 clip inside the (redirected) music folder
# --------------------------------------------------------------------------- #
CLIP = os.path.join(REDIRECT, "clip.mp4")
gen = subprocess.run(
    [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
     "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=2",
     "-pix_fmt", "yuv420p", "-c:v", "libx264", CLIP],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
if gen.returncode != 0 or not os.path.isfile(CLIP):
    print("SKIP: ffmpeg could not render the fixture: "
          + gen.stderr.decode("utf-8", "replace").strip()[:300])
    sys.exit(2)

NOTES = os.path.join(REDIRECT, "notes.txt")
with open(NOTES, "w", encoding="utf-8") as f:
    f.write("not a video\n")

OUTSIDE = tempfile.mkdtemp(prefix="mlo-thumb-outside-")
atexit.register(lambda: shutil.rmtree(OUTSIDE, ignore_errors=True))
OUTSIDE_VIDEO = os.path.join(OUTSIDE, "outside.mp4")
shutil.copyfile(CLIP, OUTSIDE_VIDEO)

from fastapi.testclient import TestClient  # noqa: E402

from server import main as mlo_main  # noqa: E402  (heavy import)

_client = TestClient(mlo_main.app)   # no lifespan: no workers, no autostart

CACHE = os.path.join(REDIRECT, ".mlo", "data", "thumbs")


def thumb(t=0, w=160, path=CLIP):
    return _client.get("/api/videos/thumb", params={"path": path, "t": t, "w": w})


def cache_files():
    if not os.path.isdir(CACHE):
        return []
    return sorted(os.listdir(CACHE))


def jpeg(res):
    return res.status_code == 200 and res.content[:3] == b"\xff\xd8\xff"


print("== frames ==")
r0 = thumb(t=0)
ok(jpeg(r0), f"t=0 answers a JPEG (status {r0.status_code}, "
             f"{len(r0.content)} bytes)")
ok(r0.headers.get("content-type") == "image/jpeg",
   f"content type is image/jpeg (got {r0.headers.get('content-type')})")
ok("max-age" in (r0.headers.get("cache-control") or ""),
   f"the frame is privately cacheable (got {r0.headers.get('cache-control')})")

r1 = thumb(t=1)
ok(jpeg(r1), f"a mid-point (t=1) answers a JPEG (status {r1.status_code})")
ok(r1.content != r0.content, "a different second is a different frame")

r0b = thumb(t=0)
ok(r0b.content == r0.content, "the same request answers the same bytes")

print("== caching ==")
files = cache_files()
ok(len(files) >= 2, f"the frames landed in the cache dir (got {files})")
target = [f for f in files if os.path.getsize(os.path.join(CACHE, f)) == len(r0.content)]
ok(len(target) == 1, f"the t=0 frame is one cache entry (got {len(target)})")
fp = os.path.join(CACHE, target[0])
before = os.stat(fp).st_mtime_ns
t0 = time.time()
r0c = thumb(t=0)
first_ms = (time.time() - t0) * 1000
after = os.stat(fp).st_mtime_ns
ok(after == before,
   "the second identical request did not rewrite the cached frame (no re-encode)")
ok(r0c.content == r0.content, "and it served the same bytes")
print(f"  (cached request took {first_ms:.0f} ms)")

# the cache is quantized per WHOLE second: 1.4s and 1.0s are the same frame
q1 = thumb(t=1.0)
n_after_one = len(cache_files())
q2 = thumb(t=1.4)
ok(q2.content == q1.content,
   "two requests inside one second are the same frame (per-second bucket)")
ok(len(cache_files()) == n_after_one, "…and the second one added no cache entry")

print("== guards ==")
outside = thumb(path=OUTSIDE_VIDEO)
ok(outside.status_code == 400,
   f"a video outside the music folder is refused (got {outside.status_code})")
ok(thumb(path=NOTES).status_code == 400,
   f"a non-video file inside the music folder is refused (got 400)")
missing = thumb(path=os.path.join(REDIRECT, "nope.mp4"))
ok(missing.status_code == 404,
   f"a missing file is 404 (got {missing.status_code})")

print("== clamping ==")
neg = thumb(t=-5)
ok(jpeg(neg), f"a negative t still answers a frame (status {neg.status_code})")
ok(neg.content == r0.content, "a negative t is clamped to t=0 (same frame)")

huge = thumb(t=99999)
ok(jpeg(huge), f"an absurd t still answers a frame (status {huge.status_code})")
if FFPROBE:
    at_end = thumb(t=2)
    ok(huge.content == at_end.content,
       "an absurd t is clamped to the file's duration (same frame as t=end)")
else:
    print("  (no ffprobe: duration unknown, the end clamp cannot be checked)")

zero_width = _client.get("/api/videos/thumb",
                         params={"path": CLIP, "t": 0, "w": -3})
ok(jpeg(zero_width), "a nonsense width still answers a frame")
ok(all(os.path.getsize(os.path.join(CACHE, f)) > 0 for f in cache_files()),
   "no truncated file is left in the cache")

print("== cache pruning ==")
import server.thumbs as thumbs  # noqa: E402

_scratch = tempfile.mkdtemp(prefix="mlo-thumb-prune-")
atexit.register(lambda: shutil.rmtree(_scratch, ignore_errors=True))
for _i in range(4):
    _fp = os.path.join(_scratch, "pad-%d.jpg" % _i)
    with open(_fp, "wb") as _fh:
        _fh.write(b"x" * 1024)
    os.utime(_fp, (1000 + _i, 1000 + _i))        # pad-0 is the oldest
_ceiling = thumbs._CACHE_BYTES
thumbs._CACHE_BYTES = 2048                        # 4 KB of frames fit neither
thumbs._prune(_scratch)
thumbs._CACHE_BYTES = _ceiling
ok(sorted(os.listdir(_scratch)) == ["pad-3.jpg"],
   f"the oldest frames are dropped until the cache fits "
   f"({sorted(os.listdir(_scratch))})")

print(f"\nAll {passed} checks passed.")
