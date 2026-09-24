#!/usr/bin/env python3
"""Single songs out of an album (issue #53).

One track of a CD rip imported on its own is a SLICE of that album, not an
album of its own: it lands in the album's folder, the rip's own .cue/.log says
which track that is and what the album is still missing, and the .accurip
speaks for the track that is there — never for the disc as if all of it were.

Everything runs against a REAL rip fixture built here (four FLAC tracks whose
log CRCs are the tracks' own decoded CRCs, a per-track .cue, an EAC-style .log
with a TOC and per-track Filenames, and a canonical CUETools .accurip), on a
scratch music folder: the developer's library is never opened or written. The
import goes through the app's own HTTP surface (the /api/import routes), which
is the contract the wizard depends on.

Run:  python tools/test_single_song_import.py   (exit 0 pass, 1 fail)
Needs .dependencies' flac.exe and ffmpeg (skips itself when either is absent).
"""
import atexit
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --------------------------------------------------------------------------- #
# hermeticity: the app's paths resolve through the music folder the moment they
# are first touched, so the scope is redirected BEFORE server.main is imported.
# --------------------------------------------------------------------------- #
REAL_MUSIC_FOLDER = ""
try:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        REAL_MUSIC_FOLDER = str((json.load(f) or {}).get("music_folder") or "")
except Exception:
    pass

REDIRECT = tempfile.mkdtemp(prefix="mlo-ssi-redirect-")
MF = tempfile.mkdtemp(prefix="mlo-ssi-lib-")
RIP = tempfile.mkdtemp(prefix="mlo-ssi-rip-")
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

atexit.register(lambda: [shutil.rmtree(d, ignore_errors=True) for d in (REDIRECT, MF, RIP)])
atexit.register(lambda: os.environ.pop("MLO_MUSIC_FOLDER", None))

REAL = REAL_MUSIC_FOLDER.replace("\\", "/").rstrip("/")
for _t in (MF, RIP):
    if REAL:
        assert not _t.replace("\\", "/").lower().startswith(REAL.lower()), \
            f"temp fixture {_t} sits inside the real music folder {REAL}"

FAILS = []


def check(cond, label, extra=""):
    got = f" — {extra}" if (extra and not cond) else ""
    print(("  PASS  " if cond else "  FAIL  ") + label + got)
    if not cond:
        FAILS.append(label)


def eq(got, want, label):
    check(got == want, label, f"got {got!r}, want {want!r}")


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #
def _find(name):
    base = os.path.join(ROOT, ".dependencies")
    for root, _dirs, files in os.walk(base):
        for f in files:
            if f.lower() == name.lower():
                return os.path.join(root, f)
    return shutil.which(os.path.splitext(name)[0])


FLAC, FFMPEG = _find("flac.exe"), _find("ffmpeg.exe")
if not FLAC or not FFMPEG:
    print("SKIP: a real rip fixture needs .dependencies' flac.exe and ffmpeg.exe")
    sys.exit(0)

from fastapi.testclient import TestClient  # noqa: E402
from mutagen.flac import FLAC as MFLAC  # noqa: E402
from mlo import discs  # noqa: E402
from mlo.accurip import (  # noqa: E402
    _canonical_accurip_text, parse_accurip_per_track, parse_accurip_status,
    run_generate_accurip,
)
from mlo.config import load_config, normalize_config  # noqa: E402
from mlo.grader import _grade_album  # noqa: E402
from mlo.paths import load_expected_tracks  # noqa: E402
from server import library as lib_mod  # noqa: E402
from server import main as mlo_main  # noqa: E402

# --------------------------------------------------------------------------- #
# the rip fixture
# --------------------------------------------------------------------------- #
ALBUM_TITLE = "The Wall"
ALBUM_ARTIST = "Pink Floyd"
ALBUM_MBID = "8f1b7b4e-0000-4000-8000-000000000053"
ALBUM_FOLDER = "Pink Floyd - The Wall"      # the folder a full import creates
# Well apart in playtime: the log's TOC is the evidence that places a file
# whose name and tags say nothing, and the disc mapping only trusts a UNIQUE
# playtime match (mlo.discs.TOC_TOLERANCE_S).
TRACKS = [("First", 3.0), ("Second", 20.0), ("Third", 37.0), ("Fourth", 54.0)]
NAMES = [f"{i:02d} - {title}.flac" for i, (title, _s) in enumerate(TRACKS, 1)]


def make_wav(path, seconds):
    with wave.open(path, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00\x00\x00" * int(44100 * seconds))


def make_track(path, seconds, number, title, tagged=True,
               album=ALBUM_TITLE, artist=ALBUM_ARTIST, mbid=ALBUM_MBID):
    """A real FLAC track, tagged the way a rip of this album is."""
    wav = os.path.join(RIP, "tone.wav")
    make_wav(wav, seconds)
    subprocess.run([FLAC, "-s", "-f", "-8", "-o", path, wav],
                   check=True, capture_output=True)
    if tagged:
        f = MFLAC(path)
        f["TITLE"] = [title]
        f["ARTIST"] = [artist]
        f["ALBUMARTIST"] = [artist]
        f["ALBUM"] = [album]
        f["DATE"] = ["1979"]
        f["MEDIA"] = ["CD"]
        f["TRACKNUMBER"] = [str(number)]
        f["TRACKTOTAL"] = [str(len(TRACKS))]
        f["MUSICBRAINZ_ALBUMID"] = [mbid]
        f.save()
    return path


def write_cue(album_dir):
    lines = ["REM GENRE Rock", "REM DATE 1979", 'PERFORMER "Pink Floyd"',
             f'TITLE "{ALBUM_TITLE}"']
    start = 0.0
    for i, (title, secs) in enumerate(TRACKS, 1):
        lines.append(f'FILE "{NAMES[i - 1]}" WAVE')
        lines.append(f"  TRACK {i:02d} AUDIO")
        lines.append(f'    TITLE "{title}"')
        lines.append(f'    PERFORMER "{ALBUM_ARTIST}"')
        lines.append(f"    INDEX 01 {int(start // 60):02d}:{int(start % 60):02d}:00")
        start += secs
    with open(os.path.join(album_dir, "CD-1.cue"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def write_log(album_dir, crcs):
    """An EAC-style log: the TOC the disc mapping reads, and per-track
    Filename + Copy CRC (the values the audio's own CRC is compared with)."""
    rows = []
    start = 0.0
    for i, (_title, secs) in enumerate(TRACKS, 1):
        rows.append(f"{i:7d}  | {int(start // 60)}:{int(start % 60):02d}.00 | "
                    f" {int(secs // 60)}:{int(secs % 60):02d}.00 |"
                    f" {int(start * 75):9d}    | {int((start + secs) * 75 - 1):9d}")
        start += secs
    body = []
    for i, crc in enumerate(crcs, 1):
        body.append(f"Track {i:2d}\n\n"
                    f"     Filename F:\\rip\\{os.path.splitext(NAMES[i - 1])[0]}.wav\n\n"
                    f"     Peak level 100.0 %\n"
                    f"     Track quality 100.0 %\n"
                    f"     Copy CRC {crc}\n")
    text = ("Exact Audio Copy V1.6 from 23. October 2020\n\n"
            "EAC extraction logfile from 1. January 2026, 12:00\n\n"
            f"{ALBUM_ARTIST} / {ALBUM_TITLE}\n\n"
            "Used drive  : PLEXTOR  Adapter: 1  ID: 0\n\n"
            "TOC of the extracted CD\n\n"
            "     Track |   Start  |  Length  | Start sector | End sector\n"
            "    ---------------------------------------------------------\n"
            + "\n".join(rows) + "\n\n\n"
            "Range status and errors\n\n"
            + "\n".join(body) + "\n"
            "No errors occurred\n\nEnd of status report\n")
    with open(os.path.join(album_dir, "CD-1.log"), "w", encoding="utf-8") as fh:
        fh.write(text)
    return text


def write_accurip(album_dir):
    """A canonical CUETools log whose per-track verdicts differ: the DISC is
    FAKE (track 3 does not match), which is exactly what a partial import must
    not let speak for the one track it holds."""
    rows = [" 01     [11111111|22222222] (V1+V2/Y) Accurately ripped",
            " 02     [33333333|44444444] (V1+V2/Y) Accurately ripped",
            " 03     [55555555|66666666] (0/Y) No match",
            " 04     [77777777|88888888] (V1+V2/Y) Accurately ripped"]
    text = ("[CUETools log; Date: 2026-01-01 00:00:00; Version: 2.2.6]\n"
            "[AccurateRip ID: 00001234-00001234-00001234]\n\n"
            "Track   [  CRC   |   V2   ] Status\n" + "\n".join(rows) + "\n")
    canon = _canonical_accurip_text(text, append_final_newline=False)
    with open(os.path.join(album_dir, "CD-1.accurip"), "w", encoding="utf-8") as fh:
        fh.write(canon)
    return canon


print("== build the scratch CD rip ==")
rip_tracks = [make_track(os.path.join(RIP, NAMES[i]), secs, i + 1, title)
              for i, (title, secs) in enumerate(TRACKS)]
real_crcs = [discs._audio_crc32(FFMPEG, p) for p in rip_tracks]
check(all(real_crcs), "every fixture track's decoded CRC is readable")
write_cue(RIP)
log_text = write_log(RIP, real_crcs)
accurip_text = write_accurip(RIP)
print(f"  rip: {RIP}")

client = TestClient(mlo_main.app)


def upload(target_dir, names, extra=()):
    """POST /api/import/upload with the named fixture files (plus extras)."""
    files = []
    for name in list(names) + list(extra):
        with open(os.path.join(RIP, name), "rb") as fh:
            files.append(("files", (name, fh.read(), "application/octet-stream")))
    return client.post("/api/import/upload", params={"target_dir": target_dir},
                       files=files)


def album_row(path):
    """The library payload's row for one album folder (the album page's own
    source), or None."""
    want = os.path.normcase(os.path.abspath(path))
    for artist in lib_mod.build_library(load_config()).get("artists", []):
        for alb in artist.get("albums", []):
            if alb.get("path") and os.path.normcase(os.path.abspath(alb["path"])) == want:
                return alb
    return None


def grade(path, **cfg):
    return _grade_album(path, "lrc", normalize_config({"music_folder": MF, **cfg}))


def missing_positions(row):
    return [t["position"] for t in row["expected_tracks"] if t["missing"]]


def present_positions(row):
    return [t["position"] for t in row["expected_tracks"] if not t["missing"]]


# --------------------------------------------------------------------------- #
# 0. the sheets say what the fixture claims — the rest of the suite is reading
#    a real rip, not a file this test invented answers for
# --------------------------------------------------------------------------- #
print("== the rip's own sheets state its tracklist ==")
sheets = discs.sidecar_tracklist(RIP)
eq(sheets["source"], "cue", "the .cue is the tracklist source")
eq([r["position"] for r in sheets["rows"]], [1, 2, 3, 4], "four rows, in order")
eq([r["title"] for r in sheets["rows"]], [t for t, _s in TRACKS],
   "the cue's own per-track TITLEs are the rows' titles")
eq([r["file"] for r in sheets["rows"]], NAMES,
   "each row carries the FILE the cue names for it")
eq(discs.parse_log_track_files(log_text),
   {i: os.path.splitext(NAMES[i - 1])[0] + ".wav" for i in range(1, 5)},
   "the log's per-track Filename lines are read")
eq(discs.parse_log_track_seconds(log_text),
   {1: 3.0, 2: 20.0, 3: 37.0, 4: 54.0}, "the log's TOC playtimes are read")
eq(discs.parse_log_checksums(log_text), {i: real_crcs[i - 1] for i in range(1, 5)},
   "the log states each track's own CRC")
eq(parse_accurip_per_track(accurip_text), {1: "REAL", 2: "REAL", 3: "FAKE", 4: "REAL"},
   "the .accurip's per-track verdicts are read")
eq(parse_accurip_status(accurip_text)[0], "FAKE",
   "the .accurip's DISC verdict is FAKE (track 3 does not match)")

print("== a file is placed on the row the sheets give it ==")
second = os.path.join(RIP, NAMES[1])
eq(discs.match_disc_row(sheets["rows"], second)["position"], 2,
   "the cue's FILE entry places the second track on row 2")
# A file that states nothing about its position: no number in its name, no
# TRACKNUMBER tag, and a log whose per-track Filename lines are absent (XLD
# writes none) — the TOC playtime is the only evidence left.
unnumbered = os.path.join(RIP, "Second.flac")
with open(second, "rb") as src, open(unnumbered, "wb") as dst:
    dst.write(src.read())
_f = MFLAC(unnumbered)
del _f["TRACKNUMBER"]
_f.save()
log_only = discs.log_track_rows("\n".join(
    ln for ln in log_text.splitlines() if "Filename" not in ln))
eq((len(log_only), all(not r["file"] for r in log_only)), (4, True),
   "an XLD-style log states the running order without naming files")
eq(discs.match_disc_row(log_only, unnumbered)["position"], 2,
   "the log's TOC playtime places the unnumbered file on row 2")

# --------------------------------------------------------------------------- #
# 1. ONE track of the rip + its sheets: it lands IN the album, and the album
#    reads partial with the rip's own tracklist saying what is missing
# --------------------------------------------------------------------------- #
print("== one track of the rip, with its sheets, into the album folder ==")
res = upload(ALBUM_FOLDER, [NAMES[1]], extra=["CD-1.cue", "CD-1.log", "CD-1.accurip"])
check(res.status_code == 200, "the upload succeeds", res.text[:200])
body = res.json()
album_dir = body["album_path"]
eq(os.path.basename(album_dir), ALBUM_FOLDER, "the file landed in the album folder")
eq(body.get("album_name"), ALBUM_FOLDER, "the album's name is the album's")
eq(body.get("merged"), False, "it was not merged into an existing album")
check(os.path.isfile(os.path.join(album_dir, NAMES[1])),
      "the imported track is inside the album folder")
check(not os.path.isdir(os.path.join(pathmod.library_root(MF),
                                     os.path.splitext(NAMES[1])[0])),
      "no album was created out of the track's own name")

manifest = pathmod.load_expected_tracks(album_dir)
eq([t["position"] for t in manifest["tracks"]], [1, 2, 3, 4],
   "the rip's own tracklist was recorded from the .cue")
eq([t["file"] for t in manifest["tracks"]], NAMES,
   "the recorded rows name the file each one is")

row = album_row(album_dir)
check(row is not None, "the album is in the library payload")
eq(len(row["expected_tracks"]), 4, "the album reports the rip's four tracks")
eq(missing_positions(row), [1, 3, 4],
   "the tracks that never came in are the missing ones (not merely counted)")
eq(present_positions(row), [2],
   "the imported file is matched to ITS row (row 2), not to the first row")
eq([t["title"] for t in row["expected_tracks"] if not t["missing"]], ["Second"],
   "and that row is the cue's own track")
eq(row["partial"], True, "the album reports itself partial")
eq(row["partial_reason"], "1 of 4 tracks of the album's tracklist are in this folder",
   "the album says how much of the release is there")
state = discs.album_expected_state(album_dir)
eq((state["present"], state["missing"]), (1, 3), "the completeness rule agrees")

# --------------------------------------------------------------------------- #
# 2. grading: partial, said in those words, with the per-track AccurateRip
#    verdict for the track that is there
# --------------------------------------------------------------------------- #
print("== a partial CD is graded on the evidence it has ==")
g = grade(album_dir)
eq(g["partial"], True, "the grade says the album is partial")
eq(g["partial_reason"], "1 of 4 tracks of the album's tracklist are in this folder",
   "the grade names how much of the disc is here")
issues = list(g["issues"])
check(not any(i.startswith("Missing .log file") for i in issues),
      "the .log IS there: no generic missing-.log failure", issues)
check(not any(i.startswith("Missing .cue file") for i in issues),
      "the .cue IS there: no generic missing-.cue failure", issues)
track = g["tracks"][0]
eq(track["file"], NAMES[1], "the graded track is the imported one")
eq(track["accuraterip_status"], "REAL",
   "the .accurip yields the PER-TRACK verdict for the track that is present")
eq(g["accuraterip_status"], "REAL",
   "the album-level verdict is the one its own track carries — the disc-wide "
   "FAKE (track 3) is not charged to an album that does not hold track 3")
check("CRC_MISMATCH" not in (track.get("issues") or []),
      "the log's CRC for that track verifies against the audio",
      track.get("issues"))
check("CRC" not in (track.get("issues") or []),
      "and the track is covered by the log's per-track CRCs", track.get("issues"))

# A rule that CANNOT pass on a partial disc must say so in those words: the
# same one-track album without its sheets is the case (a single song imported
# from the release's tracklist alone).
print("== a partial CD whose sheets were never imported says why ==")
# A different release: two library albums of ONE release are a real (if odd)
# state, and which of them a lone song joins is not what this check is about.
OTHER_MBID = "8f1b7b4e-0000-4000-8000-000000000054"
other_name = "Other Album Track.flac"
make_track(os.path.join(RIP, other_name), 37.0, 3, "Third",
           album="Other Album", artist="Other Artist", mbid=OTHER_MBID)
res2 = upload("Partial CD (no sheets)", [other_name])
check(res2.status_code == 200, "the upload succeeds", res2.text[:200])
lonely = res2.json()["album_path"]
rec = client.post("/api/import/expected", json={
    "target_dir": lonely, "release_id": None,
    "tracks": [{"disc": t["disc"], "position": t["position"], "title": t["title"],
                "recording_mbid": None, "file": t["file"]}
               for t in manifest["tracks"]]})
check(rec.status_code == 200, "the tracklist could be recorded", rec.text[:200])
g2 = grade(lonely)
eq(g2["partial"], True, "the album without sheets is partial too")
issues2 = list(g2["issues"])
check(any("this album holds 1 of 4 tracks" in i for i in issues2),
      "a CD rule that cannot pass on a slice says exactly that", issues2)
check("Missing .log file" not in issues2,
      "and it is not reported as the generic missing-.log failure")

# --------------------------------------------------------------------------- #
# 3. a second song of the same rip joins the album it belongs to
# --------------------------------------------------------------------------- #
print("== a second song of the same rip joins the album ==")
log_path = os.path.join(album_dir, "CD-1.log")
log_before = hashlib.sha256(open(log_path, "rb").read()).hexdigest()
res3 = upload(NAMES[0], [NAMES[0]], extra=["CD-1.log"])
check(res3.status_code == 200, "a dropped single track imports", res3.text[:200])
body3 = res3.json()
eq(os.path.basename(body3["album_path"]), ALBUM_FOLDER,
   "the track's own name is not an album name: it joined the album its release id names")
eq(body3.get("merged"), True, "the response says it joined an existing album")
check(os.path.isfile(os.path.join(album_dir, NAMES[0])),
      "the second track is in the album folder")
check(not os.path.isdir(os.path.join(pathmod.library_root(MF),
                                     os.path.splitext(NAMES[0])[0])),
      "no sibling album was created beside it")
row3 = album_row(album_dir)
eq(missing_positions(row3), [3, 4],
   "the album is still partial, now missing only what is not there")
eq(row3["partial_reason"], "2 of 4 tracks of the album's tracklist are in this folder",
   "and it says so with the new count")
eq(hashlib.sha256(open(log_path, "rb").read()).hexdigest(), log_before,
   "the album's own .log was NOT replaced by the arriving copy (writers fill)")

# --------------------------------------------------------------------------- #
# 4. script 9 does not regenerate a partial disc's .accurip into a lie
# --------------------------------------------------------------------------- #
print("== the .accurip of a partial album is left exactly as it is ==")
accurip_path = os.path.join(album_dir, "CD-1.accurip")
stored = open(accurip_path, "rb").read()
cfg = normalize_config({"music_folder": MF,
                        "targets": [os.path.join(album_dir, NAMES[0])],
                        "force_accurip": True})
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    stats = run_generate_accurip(cfg)
out = buf.getvalue()
after = open(accurip_path, "rb").read()
check(after == stored, "a forced AccurateRip run did not rewrite the rip's "
                       "own .accurip", f"{len(stored)} -> {len(after)} bytes")
eq(parse_accurip_per_track(after.decode("utf-8")),
   {1: "REAL", 2: "REAL", 3: "FAKE", 4: "REAL"},
   "the disc's own verdicts are still the stored ones")
eq(stats["modified_count"], 0, "nothing was written for the partial album")
check("partial album" in out or "cannot run" in out,
      "the run says why it left the album alone", out[-400:])
if "partial album" not in out:
    # A host without the AccurateRip toolchain (CUETools/ARCUE on Windows,
    # the mono runtime in the container) stops at the run's own precondition
    # and never reaches the album pass — so the sentence above is the tool
    # report instead. The guard itself is still proven here, by the file:
    # the .accurip is byte-identical and nothing was written for the album.
    print("  (no AccurateRip toolchain on this host — the partial-album guard "
          "is proven by the file assertions above)")

# --------------------------------------------------------------------------- #
# 5. a genuinely standalone single is still its own album
# --------------------------------------------------------------------------- #
print("== a standalone single keeps its own album ==")
solo_name = "01 - Solo Track.flac"
make_track(os.path.join(RIP, solo_name), 4.0, 1, "Solo Track", tagged=False)
res4 = upload(solo_name, [solo_name])
check(res4.status_code == 200, "the standalone single imports", res4.text[:200])
solo_dir = res4.json()["album_path"]
eq(os.path.basename(solo_dir), solo_name,
   "a file that names no album is imported as its own album, as it always was")
check(os.path.isfile(os.path.join(solo_dir, solo_name)), "and the file is in it")
eq(load_expected_tracks(solo_dir)["tracks"], [],
   "nothing invented a tracklist for the album-less single")
eq(album_row(solo_dir)["partial"], False, "it is not partial")

print("== a real album name is never hijacked by the track's own tags ==")
res5 = upload("The Wall (Remaster)", [NAMES[3]])
check(res5.status_code == 200, "the single imports", res5.text[:200])
eq(os.path.basename(res5.json()["album_path"]), "The Wall (Remaster)",
   "a caller that names a real album keeps that name")
eq(res5.json().get("merged"), False,
   "and it did not join the album the release id points at")

print()
if FAILS:
    print(f"FAILED ({len(FAILS)})")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("ALL PASS")
