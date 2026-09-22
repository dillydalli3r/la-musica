#!/usr/bin/env python3
"""The .accurip round trip: CUETools writes the file, the audit and the grader read it.

Every claim below is made about a REAL run of `mlo.accurip.run_generate_accurip`
over a synthetic CD album (two 16-bit/44.1 kHz WAVs tagged MEDIA=CD), driven by
the real CUETools binary from .dependencies:

  * a CD album with NO cue and NO log still gets its CD-{n}.accurip — the
    synthesized-cue path used to be refused, so exactly the albums whose
    sidecars are missing could never be verified;
  * a cue that names files this disc does not hold does not derail the run
    (ArCueDotNet's own recovery gives up as soon as the folder holds more
    audio than the cue names);
  * the file is canonical — LF, trimmed lines, no trailing blank line — and
    `_canonical_accurip_text` is idempotent on it, which is the rule grading
    compares the stored file against;
  * the parsers read what CUETools actually wrote: the per-track CRC table
    describes THIS disc's audio, and the verdict is the one the log states;
  * a verdict table in a layout `_TRACK_AR_RE` did not know (a single CRC, as
    pre-2.1.4 CUETools prints) still yields per-track verdicts, and a
    "disk not present in database" log says so instead of "no track status";
  * a legacy `.accurip` name with no disc number is renamed onto the disc its
    own CRC table describes, and never onto a disc it might equally describe.

A synthetic pressing is not in the AccurateRip database, so its verdict is
NONE: that is the honest answer for it, and the assertions check the log
against the audio rather than against the database. No network is required.

Local tools only: ffmpeg and the CUETools ARCUE binary, both from
.dependencies; the suite skips itself when either is missing.

Run:  python tools/test_accurip_roundtrip.py
"""
import contextlib
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mlo.accurip import (  # noqa: E402
    _canonical_accurip_text,
    parse_accurip_per_track,
    parse_accurip_status,
    parse_accurip_track_crcs,
    resolve_arcue_exe,
    run_generate_accurip,
)
from mlo.audio import AudioFile  # noqa: E402
from mlo import discs as discs_mod  # noqa: E402
from mlo.config import DEFAULT_CONFIG  # noqa: E402

FAILS = []


def ok(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILS.append(label)


def ffmpeg_exe():
    base = os.path.join(ROOT, ".dependencies")
    for root, _dirs, files in os.walk(base):
        for f in files:
            if f.lower() in ("ffmpeg.exe", "ffmpeg"):
                return os.path.join(root, f)
    return shutil.which("ffmpeg")


FFMPEG = ffmpeg_exe()
try:
    ARCUE = resolve_arcue_exe()
except Exception:
    ARCUE = None

if not FFMPEG or not ARCUE:
    print("skipped: no ffmpeg and/or CUETools ARCUE available "
          f"(ffmpeg={FFMPEG}, arcue={ARCUE})")
    sys.exit(0)

TMP = tempfile.mkdtemp(prefix="mlo-accurip-")
ALBUM = os.path.join(TMP, "Artists", "Artist", "Ripped (2020)")
os.makedirs(ALBUM, exist_ok=True)


def writetrack(path, freq, seconds=2.0):
    """A 16-bit/44.1 kHz stereo WAV tagged MEDIA=CD, via ffmpeg + mlo.audio."""
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency={freq}:duration={seconds}:sample_rate=44100",
                    "-ac", "2", "-sample_fmt", "s16", "-acodec", "pcm_s16le", path],
                   capture_output=True, check=True)
    AudioFile(path).set_tag("MEDIA", "CD")


def cfg(**extra):
    c = dict(DEFAULT_CONFIG)
    c["music_folder"] = ALBUM
    c["targets"] = None
    c.update(extra)
    return c


def write_cue(path, refs):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for i, ref in enumerate(refs, 1):
            fh.write(f'FILE "{ref}" WAVE\n  TRACK {i:02d} AUDIO\n    INDEX 01 00:00:00\n')


def run(c, album=None):
    """run_generate_accurip on `album`, with its output captured for the
    failure messages (the run is chatty by design)."""
    if album is not None:
        c = dict(c, music_folder=album)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        stats = run_generate_accurip(c)
    return stats, buf.getvalue()


def crc(path):
    return discs_mod._audio_crc32(FFMPEG, path)


T1 = os.path.join(ALBUM, "01 Track 1.wav")
T2 = os.path.join(ALBUM, "02 Track 2.wav")
CUE = os.path.join(ALBUM, "CD-1.cue")
ACCURIP = os.path.join(ALBUM, "CD-1.accurip")
writetrack(T1, 440)
writetrack(T2, 880)
write_cue(CUE, ["01 Track 1.wav", "02 Track 2.wav"])
CRC1, CRC2 = crc(T1), crc(T2)

print("== a CD album generates CD-1.accurip through the real CUETools ==")
stats, out = run(cfg())
ok(stats["modified_count"] == 1 and stats["error_count"] == 0,
   f"one .accurip written, no errors ({stats['modified_count']}, "
   f"{stats['error_count']}: {stats['errors']})")
if not os.path.isfile(ACCURIP):
    print("  FAIL no CD-1.accurip was written:\n" + out)
    FAILS.append("no CD-1.accurip was written")
    shutil.rmtree(TMP, ignore_errors=True)
    print("\nFAILED: %d check(s)" % len(FAILS))
    sys.exit(1)

with open(ACCURIP, "r", encoding="utf-8", newline="") as fh:
    text = fh.read()
print("  --- CD-1.accurip (first 20 lines) ---")
for line in text.splitlines()[:20]:
    print("  | " + line)
print("  --- end ---")

ok(text.startswith("[CUETools log;"),
   f"the file is a CUETools log ({text.splitlines()[0][:60]!r})")
ok("\r" not in text and not text.endswith("\n"),
   f"LF only, and no trailing newline as the canonical rule requires "
   f"(crlf={'\\r' in text}, ends_with_lf={text.endswith(chr(10))})")
canon = _canonical_accurip_text(text, append_final_newline=False)
ok(canon == text and _canonical_accurip_text(canon, append_final_newline=False) == canon,
   "the stored bytes ARE the canonical form, and the canonicaliser is "
   "idempotent on them (what grading compares)")

table = parse_accurip_track_crcs(text)
ok(table.get(1) == CRC1 and table.get(2) == CRC2,
   f"the log's per-track CRC32 describes THIS disc's audio "
   f"({table} vs {CRC1}/{CRC2})")

# The grader's own verdict on the file script 9 just wrote: the two must not
# disagree about a file that is exactly what CUETools produced.
from mlo.grader import _grade_sidecars  # noqa: E402

rows = [r for r in _grade_sidecars(ALBUM, sorted(os.listdir(ALBUM)), cfg())
        if r.get("type") == "accurip"]
ok(rows and all(r.get("ok") for r in rows),
   f"grading accepts the generated .accurip ({rows})")

status, detail = parse_accurip_status(text)
per = parse_accurip_per_track(text)
ok(status in ("REAL", "FAKE", "NONE"),
   f"the verdict parses ({status}: {detail})")
ok(not per or status in ("REAL", "FAKE"),
   f"a per-track table never comes with a NONE verdict ({status}, {per})")
if "not present in database" in text:
    # What a synthetic pressing gets: nothing was claimed, and the file says so
    # rather than reading as an unparsable log.
    ok(status == "NONE" and per == {}
       and "not present in database" in (detail or ""),
       f"a pressing outside the database is NONE, with that reason "
       f"({status}: {detail})")
    print("  (AccurateRip has no record of this synthetic pressing — the "
          "offline half of the round trip is what is asserted here)")

print("== no cue and no log: the album still generates (synthesized cue) ==")
os.remove(ACCURIP)
os.remove(CUE)
stats, out = run(cfg())
ok(stats["modified_count"] == 1 and stats["error_count"] == 0 and os.path.isfile(ACCURIP),
   f"CD-1.accurip is written for a CD album with neither cue nor log "
   f"({stats['modified_count']}, {stats['error_count']}: {stats['errors']})")
if os.path.isfile(ACCURIP):
    with open(ACCURIP, "r", encoding="utf-8") as fh:
        table2 = parse_accurip_track_crcs(fh.read())
    ok(table2.get(1) == CRC1 and table2.get(2) == CRC2,
       f"…and it verifies the same two tracks ({table2})")

print("== a cue naming files this disc does not hold does not derail the run ==")
# The cue a failed rename leaves behind: stale names for audio that is not in
# the album. ArCueDotNet cannot find them, and with more audio in the folder
# than the cue names it refuses outright ("unable to locate the audio files").
os.remove(ACCURIP)
write_cue(CUE, ["01.flac", "02.flac"])
writetrack(os.path.join(ALBUM, "03 Decoy.wav"), 1320, seconds=3.0)
stats, out = run(cfg(force_accurip=True))
ok(stats["error_count"] == 0 and stats["modified_count"] == 1,
   f"the disc is still verified through a stale cue ({stats['modified_count']}, "
   f"{stats['error_count']}: {stats['errors']})")
if os.path.isfile(ACCURIP):
    with open(ACCURIP, "r", encoding="utf-8") as fh:
        table3 = parse_accurip_track_crcs(fh.read())
    ok(CRC1 in table3.values() and CRC2 in table3.values(),
       f"…and the log covers this disc's own audio ({table3})")
os.remove(CUE)
os.remove(os.path.join(ALBUM, "03 Decoy.wav"))

print("== an image rip's cue keeps its layout through a stale FILE reference ==")
# One audio file per disc, and a cue naming the file it was ripped from: the
# TRACK/INDEX lines ARE the disc's TOC, so the reference has to be repointed —
# a synthesized one-track cue would describe a disc this is not.
IMG = os.path.join(TMP, "Artists", "Artist", "Image Rip (2018)")
os.makedirs(IMG, exist_ok=True)
IMG_WAV = os.path.join(IMG, "Album (2018).wav")
writetrack(IMG_WAV, 660, seconds=4.0)
with open(os.path.join(IMG, "CD-1.cue"), "w", encoding="utf-8", newline="\n") as fh:
    fh.write('FILE "Album.ape" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n'
             '  TRACK 02 AUDIO\n    INDEX 01 00:02:00\n')
stats, out = run(cfg(), album=IMG)
img_log = os.path.join(IMG, "CD-1.accurip")
ok(stats["error_count"] == 0 and stats["modified_count"] == 1
   and os.path.isfile(img_log),
   f"the image rip generates through its own cue ({stats['modified_count']}, "
   f"{stats['error_count']}: {stats['errors']})")
if os.path.isfile(img_log):
    with open(img_log, "r", encoding="utf-8") as fh:
        img_table = parse_accurip_track_crcs(fh.read())
    ok(sorted(img_table) == [1, 2],
       f"…with the cue's two tracks, not one synthesized track "
       f"({sorted(img_table)})")

print("== the same file under the other canonical settings ==")
# Grading compares the stored file against _canonical_accurip_text with the
# USER's flags, so the writer must honour them too: append_final_newline and
# keep_empty_accurip_lines change the expected bytes, not just the call.
stats, out = run(cfg(force_accurip=True, append_final_newline=True,
                     keep_empty_accurip_lines=True))
with open(ACCURIP, "r", encoding="utf-8", newline="") as fh:
    variant = fh.read()
ok(variant == _canonical_accurip_text(variant, keep_empty_lines=True,
                                      append_final_newline=True)
   and variant.endswith("\n"),
   "with append_final_newline + keep_empty_accurip_lines the stored file is "
   f"still exactly what grading expects (ends_with_lf={variant.endswith(chr(10))})")

print("== the parsers read the log shapes CUETools writes ==")
# 2.1.4+: two CRCs per track, and an "Offsetted by" block (alternate
# pressings) that must not decide the primary verdict.
modern = (
    "[CUETools log; Date: 4/11/2012 10:48:23 AM; Version: 2.1.4]\n"
    "[CTDB TOCID: JsQ7P1MwgnFfqPpu5OJ.ZJOkl5M-] found.\n"
    "Track | CTDB Status\n"
    "  1   | (100/100) Accurately ripped\n"
    "[AccurateRip ID: 001a2e35-00e484f6-7d0f930b] found.\n"
    "Track   [  CRC   |   V2   ] Status\n"
    " 01     [f95b16c0|d0f748ba] (10+02/34) Accurately ripped\n"
    " 02     [5eef1118|0f0c8580] (10+02/34) No match\n"
    "Offsetted by 640:\n"
    " 01     [c32a7a37] (12/34) Accurately ripped\n"
    "\n"
    "Track Peak [ CRC32  ] [W/O NULL]\n"
    " --    99.0 [DECC2519] [00000000]\n"
    " 01    99.0 [40EF0FF6] [00000000]\n"
)
st, _d = parse_accurip_status(modern)
ok(st == "FAKE" and parse_accurip_per_track(modern) == {1: "REAL", 2: "FAKE"},
   f"a 2.1.4 log: one track's No match is FAKE, per track "
   f"({st}, {parse_accurip_per_track(modern)})")

# 2.0.9/2.1.2 print ONE CRC per track and a differently spaced header; the
# verdicts are the same verdicts.
legacy = (
    "[CUETools log; Date: 16.11.2010 13:42:06; Version: 2.0.9]\n"
    "[AccurateRip ID: 00147ff7-00b45cbb-9b0c680b] found.\n"
    "Track [ CRC ] Status\n"
    " 01 [c6e0170a] (1/1) Accurately ripped\n"
    " 02 [bef24d7e] (1/1) Accurately ripped\n"
    "Track Peak [ CRC32  ] [W/O NULL]\n"
    " 01    99.0 [C38AD947] [0137FA96]\n"
    " 02    99.0 [C600DDA6] [4DC16D3E]\n"
)
st, _d = parse_accurip_status(legacy)
ok(st == "REAL" and parse_accurip_per_track(legacy) == {1: "REAL", 2: "REAL"},
   f"an older log with one CRC per track still reads per track "
   f"({st}, {parse_accurip_per_track(legacy)})")

# A disc whose pressing is not in the database at all: the ID line says so,
# and that sentence is the reason there is no table.
dbmiss = (
    "[CUETools log; Date: 1/1/2020 1:00:00 AM; Version: 2.2.6]\n"
    "[CTDB TOCID: yswuv9NTJI.eOFBtxQEvCfg3Mbo-] disk not present in database.\n"
    "[AccurateRip ID: 000001c2-000004b1-06000402] disk not present in database.\n"
    "\n"
    "Track Peak [ CRC32  ] [W/O NULL]\n"
    " --    8.8 [5FB7CE9D] [100EB82B]\n"
    " 01    8.8 [C38AD947] [0137FA96]\n"
)
st, detail = parse_accurip_status(dbmiss)
ok(st == "NONE" and "not present in database" in (detail or "")
   and parse_accurip_per_track(dbmiss) == {},
   f"a database miss is NONE and says why ({st}: {detail})")

print("== a legacy .accurip name is renamed onto the disc it describes ==")
TWO = os.path.join(TMP, "Artists", "Artist", "Two Discs (2021)")
os.makedirs(TWO, exist_ok=True)
D2T1 = os.path.join(TWO, "2-01 Track.wav")
D2T2 = os.path.join(TWO, "2-02 Track.wav")
writetrack(os.path.join(TWO, "1-01 Track.wav"), 500)
writetrack(os.path.join(TWO, "1-02 Track.wav"), 600)
writetrack(D2T1, 700)
writetrack(D2T2, 800)
stats, out = run(cfg(), album=TWO)
ok(stats["modified_count"] == 2 and stats["error_count"] == 0,
   f"both discs' .accurip files are written ({stats['modified_count']}: "
   f"{stats['errors']})")
legacy_name = os.path.join(TWO, "App.accurip")
os.replace(os.path.join(TWO, "CD-2.accurip"), legacy_name)
notes = discs_mod.rename_accurip_for_discs(
    TWO, discs_mod.album_discs(TWO), config=cfg(), log_fn=lambda _m: None)
renamed = os.path.join(TWO, "CD-2.accurip")
ok(renamed in [os.path.join(TWO, n) for n, _ in notes] or os.path.isfile(renamed),
   f"a name with no disc number ('App.accurip') is claimed by the disc whose "
   f"CRC table it holds ({notes})")
if os.path.isfile(renamed):
    with open(renamed, "r", encoding="utf-8") as fh:
        moved = parse_accurip_track_crcs(fh.read())
    ok(moved.get(1) == crc(D2T1) and moved.get(2) == crc(D2T2),
       f"…and it is disc 2's, not disc 1's ({moved})")

print("== an ambiguous legacy name is left alone ==")
# Identical audio on both discs: the table matches either, so no rename may
# claim one — renaming onto the wrong disc hands grading the wrong verdict.
AMB = os.path.join(TMP, "Artists", "Artist", "Ambiguous (2022)")
os.makedirs(AMB, exist_ok=True)
for disc in (1, 2):
    for n, freq in ((1, 500), (2, 600)):
        writetrack(os.path.join(AMB, f"{disc}-{n:02d} Track.wav"), freq)
stats, out = run(cfg(), album=AMB)
os.remove(os.path.join(AMB, "CD-2.accurip"))
os.replace(os.path.join(AMB, "CD-1.accurip"), os.path.join(AMB, "App.accurip"))
notes = discs_mod.rename_accurip_for_discs(
    AMB, discs_mod.album_discs(AMB), config=cfg(force_accurip=False),
    log_fn=lambda _m: None)
ok(notes == [] and os.path.isfile(os.path.join(AMB, "App.accurip")),
   f"a legacy name that matches both discs is not renamed ({notes})")

print("== a scoped run verifies ONE album and the rest stays byte for byte ==")
# What the import chain does the moment an album lands: script 9 with that
# album as its target. The whole point of the scoped run is that a library of
# hundreds of albums costs ONE album's verification — the other albums must
# come out of it exactly as they went in.
SCOPE_A = os.path.join(TMP, "Artists", "Scoped", "Album A (2024)")
SCOPE_B = os.path.join(TMP, "Artists", "Scoped", "Album B (2024)")
for _dir, _freq in ((SCOPE_A, 300), (SCOPE_B, 1000)):
    os.makedirs(_dir, exist_ok=True)
    for _n in (1, 2):
        writetrack(os.path.join(_dir, f"{_n:02d} Track {_n}.wav"), _freq + _n)
    write_cue(os.path.join(_dir, "CD-1.cue"),
              ["01 Track 1.wav", "02 Track 2.wav"])


def snapshot(root):
    """{music-folder-relative path: sha256} — the bytes of everything below."""
    out = {}
    for base, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(base, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root).replace("\\", "/")] = \
                    hashlib.sha256(fh.read()).hexdigest()
    return out


TARGET_PREFIX = os.path.relpath(SCOPE_A, TMP).replace("\\", "/") + "/"
both = snapshot(TMP)
outside = {p: h for p, h in both.items()
           if not p.startswith(TARGET_PREFIX) and not p.startswith(".mlo/")}

stats, out = run(cfg(music_folder=TMP, targets=[SCOPE_A]))
after = snapshot(TMP)
outside_after = {p: h for p, h in after.items()
                 if not p.startswith(TARGET_PREFIX) and not p.startswith(".mlo/")}

ok(stats["modified_count"] == 1 and stats["error_count"] == 0,
   f"the target album is the one that was verified ({stats['modified_count']}, "
   f"{stats['error_count']}: {stats['errors']})")
ok(os.path.isfile(os.path.join(SCOPE_A, "CD-1.accurip")),
   "…and its CD-1.accurip is on disk")
ok(outside == outside_after,
   "no file outside the target album changed: the run's writes are inside the "
   f"album it was given ({sorted(set(outside_after) ^ set(outside))[:5]})")

b_folder = os.path.relpath(SCOPE_B, TMP).replace("\\", "/") + "/"
ok({p[len(b_folder):]: h for p, h in both.items() if p.startswith(b_folder)}
   == {p[len(b_folder):]: h for p, h in after.items() if p.startswith(b_folder)},
   "the neighbouring album is byte for byte identical (hash of every file, "
   "before vs after)")

# …and the untargeted run is still the whole library: the album the scoped run
# left alone is verified by it (the scoped run is a narrowing, never a change
# of what a Run All covers).
stats, out = run(cfg(music_folder=TMP, force_accurip=False))
ok(os.path.isfile(os.path.join(SCOPE_B, "CD-1.accurip")),
   "an untargeted run is the whole library again — it verified the album the "
   "scoped run never looked at")
ok(stats["modified_count"] >= 1 and stats["error_count"] == 0,
   f"…and it reports the work it did ({stats['modified_count']} written, "
   f"{stats['skipped_count']} already current, {stats['errors']})")

print("== a missing tool is named, with where to install it ==")
import mlo.accurip as accurip_mod  # noqa: E402
import mlo.tools as tools_mod  # noqa: E402

_real_detect = tools_mod.detect_all_tools
_real_resolve = accurip_mod.resolve_arcue_exe
try:
    tools_mod.detect_all_tools = lambda: {"ffmpeg": {}, "cuetools": {}}
    accurip_mod.resolve_arcue_exe = lambda _tools=None: None
    stats, out = run(cfg())
finally:
    tools_mod.detect_all_tools = _real_detect
    accurip_mod.resolve_arcue_exe = _real_resolve
ok(stats["modified_count"] == 0 and stats["error_count"] == 1,
   f"a run with no tools writes nothing and reports the failure "
   f"({stats['modified_count']}, {stats['error_count']})")
ok("ffmpeg" in out and "CUETools" in out and "Dependencies" in out,
   "the failure names BOTH missing tools and points at the Dependencies page "
   f"({[l for l in out.splitlines() if 'missing' in l or 'Dependencies' in l]})")

shutil.rmtree(TMP, ignore_errors=True)
print()
if FAILS:
    print("FAILED: %d check(s)" % len(FAILS))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("all .accurip round-trip checks passed")
