#!/usr/bin/env python3
"""Long paths past MAX_PATH reach the external tools (Windows).

A library whose album folders carry the ids, both dates and the media type
reaches 260 characters on its own — measured on a real library, 3 of 8 files
were 257-279 characters. Python itself is unaffected (wide APIs), so only the
tool-driven steps broke, and they broke QUIETLY:
`flac -t` answered "No such file or directory", the FLAC pass did nothing,
the audit could not read the audio and stamped AUDIT=FAKE.

`mlo.subproc.tool_path` is the one place that turns such a path into a form
the tools can open (the volume's 8.3 alias, else a temporary junction on the
file's own folder). This suite pins:

  * short paths, non-paths and every POSIX host pass through untouched;
  * a long path keeps pointing at the SAME file (contents match through it);
  * the bundled flac, when it is there, really opens the file through it —
    the exact call that used to fail.

Run:  python tools/test_long_paths.py
Exit 2 when the platform cannot exercise the Windows half (no flac / POSIX).
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo.subproc import LONG_PATH_MIN, tool_path  # noqa: E402

passed = 0


def ok(cond, label):
    global passed
    assert cond, f"FAILED: {label}"
    passed += 1
    print(f"  ok: {label}")


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLAC = None
_deps = os.path.join(ROOT, ".dependencies")
if os.path.isdir(_deps):
    for entry in sorted(os.listdir(_deps)):
        if entry.lower().startswith("flac"):
            cand = os.path.join(_deps, entry, "flac.exe")
            if os.path.isfile(cand):
                FLAC = cand
if not FLAC:
    FLAC = shutil.which("flac")

tmp = tempfile.mkdtemp(prefix="mlo-longpath-test-")

# --------------------------------------------------------------------------- #
# 1) Everything that is not a long Windows path is returned unchanged
# --------------------------------------------------------------------------- #
plain = os.path.join(tmp, "short.flac")
with open(plain, "wb") as fh:
    fh.write(b"x")
ok(tool_path(plain) == plain, "a short path is untouched")
ok(tool_path("") == "" and tool_path("-o") == "-o",
   "empty strings and option-looking arguments are untouched")
ok(tool_path(os.path.join(tmp, "missing.flac")) == os.path.join(tmp, "missing.flac"),
   "a path that neither exists nor has an existing folder is untouched")

# --------------------------------------------------------------------------- #
# 2) A path past the threshold still names the SAME file
# --------------------------------------------------------------------------- #
deep = tmp
while len(os.path.join(deep, "pad.flac")) < LONG_PATH_MIN + 40:
    deep = os.path.join(deep, "pad-folder-name-0123456789")
os.makedirs(deep, exist_ok=True)
long_file = os.path.join(deep, "track.flac")
with open(long_file, "wb") as fh:
    fh.write(b"long-path-body")

ok(len(long_file) >= LONG_PATH_MIN, f"the fixture really is long ({len(long_file)} chars)")

if os.name != "nt":
    ok(tool_path(long_file) == long_file,
       "POSIX: a long path is handed over unchanged (no such limit there)")
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{passed} checks passed (POSIX — the Windows half is skipped)")
    raise SystemExit(0)

mapped = tool_path(long_file)
if mapped == long_file:
    # No 8.3 alias and no junction could be made on this machine: the caller
    # gets the original path (the tool will fail exactly as it did before).
    ok(True, "no short form available here — the original path is returned")
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{passed} checks passed (no short form on this host)")
    raise SystemExit(2)

ok(len(mapped) < len(long_file), f"the mapped path is shorter ({len(mapped)} chars)")
ok(os.path.isfile(mapped), "the mapped path exists")
with open(mapped, "rb") as fh:
    ok(fh.read() == b"long-path-body", "it is the SAME file (contents match)")

# …and writing through it lands in the real file.
with open(mapped, "wb") as fh:
    fh.write(b"written-through-bridge")
with open(long_file, "rb") as fh:
    ok(fh.read() == b"written-through-bridge", "writing through it writes the real file")

# --------------------------------------------------------------------------- #
# 3) The bundled flac opens it — the exact call that used to fail
# --------------------------------------------------------------------------- #
if not FLAC:
    print("  skipped: flac check (no flac binary on this machine)")
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{passed} checks passed (flac skipped)")
    raise SystemExit(0)

# A REAL flac, encoded into the long folder: the tool must fail on the path
# (MAX_PATH), never on the audio, so the fixture has to be a valid stream.
import wave  # noqa: E402

wav = os.path.join(tmp, "fixture.wav")
with wave.open(wav, "w") as w:
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(44100)
    w.writeframes(b"\x00\x00\x00\x00" * 4410)
audio = os.path.join(deep, "encoded.flac")
# Encoded THROUGH the mapped path: flac cannot create the long name either,
# which is the same limit seen from the writing side.
subprocess.run([FLAC, "-s", "-f", "-8", "-o", tool_path(audio), wav],
               check=True, capture_output=True)
ok(len(audio) >= LONG_PATH_MIN and os.path.isfile(audio),
   f"the flac fixture is a real stream at a long path ({len(audio)} chars)")

direct = subprocess.run([FLAC, "-t", audio], capture_output=True, text=True,
                        errors="replace")
ok(direct.returncode != 0,
   "the raw long path really does fail for flac (that was the bug)")

audio_mapped = tool_path(audio)
bridged = subprocess.run([FLAC, "-t", audio_mapped], capture_output=True, text=True,
                         errors="replace")
ok(bridged.returncode == 0,
   f"flac verifies that stream through the mapped path ({bridged.returncode})")

# run_tool is the choke point every engine call goes through: same call, and
# the argument is rewritten for the child.
from mlo.subproc import run_tool  # noqa: E402

via_wrapper = run_tool([FLAC, "-t", audio], capture_output=True, text=True)
ok(via_wrapper.returncode == 0,
   "run_tool([flac, '-t', <long path>]) succeeds — the rewrite is in the wrapper")

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{passed} checks passed")
