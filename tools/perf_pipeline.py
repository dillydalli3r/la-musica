#!/usr/bin/env python3
"""Wall-clock harness for the library pipeline: per-SCRIPT, per-PHASE.

Builds a scratch library (real FLACs, real covers, real CUE/log sidecars) under
the scratch music folder and then runs each script through the app's OWN runner
(`server.script_runners.run_script` with no targets — a deliberate library-wide
run, exactly what Run All is), timing every one of them. The import pipeline's
own local steps (the library-codec conversion the import runs before anything
else) are timed in a second phase against a staged album.

It prints a table and writes a machine-readable report next to it, plus a
manifest of the library afterwards so two runs' OUTPUTS can be compared
byte-for-byte — a change that speeds a script up must not have changed what it
wrote. Each script's row also carries `http` = `Nc/Mr`: httpx clients
CONSTRUCTED and requests SENT during that script (counted by patching
`httpx.Client.__init__`/`send`), which is how the shared-client change is
measured — a run that builds one client for 120 requests prints `1c/120r`.

The measurement used for issue #53's before/after (12 albums x 5 tracks, 2 s
each, default script set — 13 and 18 are network-only and skipped):

    MLO_MUSIC_FOLDER=F:/tmp/mlo-perf python tools/perf_pipeline.py \
        --albums 12 --tracks 5 --build yes --json tools/perf_before.json
    MLO_MUSIC_FOLDER=F:/tmp/mlo-perf python tools/perf_pipeline.py \
        --albums 12 --tracks 5 --build yes --json tools/perf_after.json

`--build yes` always, so both runs measure the SAME freshly built library
(the chain renames and retags it, so a re-run over the leftovers measures a
different input). Networks scripts: `--skip 13,18` is the default; add 14 to
drop beets' own import of the whole scratch library (its cost is beets',
and on a cold beets DB it dwarfs the local passes).

    python tools/perf_pipeline.py --seconds 12 --skip 13,14,18   # real decodes

    python tools/perf_pipeline.py --albums 12 --tracks 5
    python tools/perf_pipeline.py --no-build --json tools/perf_after.json

Environment: MLO_MUSIC_FOLDER (default F:/tmp/mlo-perf) is the scratch scope;
the harness never touches the owner's real library.
"""
import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import time
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SCRATCH = os.environ.get("MLO_MUSIC_FOLDER") or "F:/tmp/mlo-perf"
os.environ["MLO_MUSIC_FOLDER"] = SCRATCH

# The chain's own order, from the app — never a second copy of it.
from mlo.config import DEFAULT_RUN_ALL_ORDER, normalize_config  # noqa: E402
from server import script_runners  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def find_flac():
    dep = os.path.join(ROOT, ".dependencies")
    if os.path.isdir(dep):
        for entry in sorted(os.listdir(dep)):
            if entry.lower().startswith("flac"):
                for base, _dirs, files in os.walk(os.path.join(dep, entry)):
                    for f in files:
                        if f.lower() == "flac.exe":
                            return os.path.join(base, f)
    return shutil.which("flac")


FLAC = find_flac()


def make_noise_wav(path, seconds=2.0, seed=1234):
    """Broadband, decorrelated stereo noise: real audio for DR/audit/AccurateRip.

    Silence is flagged "fake lossless" and identical channels "fake stereo" by
    the audit, so a silent fixture would measure the error paths instead.
    """
    import random

    rnd = random.Random(seed)
    rate = 44100
    frames = bytearray()
    for _ in range(int(rate * seconds)):
        frames += struct.pack("<hh", rnd.randint(-9000, 9000),
                              rnd.randint(-9000, 9000))
    with wave.open(path, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


BAD_LYRICS = "[00:01.00]wrong line\n[00:02.00]another wrong line\n"
GOOD_LYRICS = ("[00:01.50]first synced line\n[00:03.00]second synced line\n"
               "[00:05.25]third synced line\n")


def tag_flac(path, tags):
    from mutagen.flac import FLAC as MFLAC

    f = MFLAC(path)
    for k, v in tags.items():
        f[k] = v
    f.save()


def write_cover(path, w=900, h=820):
    from PIL import Image

    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(0, w, 4):
            v = (x * 7 + y * 3) % 256
            for dx in range(4):
                if x + dx < w:
                    px[x + dx, y] = (v, (v * 3) % 256, (v * 5) % 256)
    img.save(path, "JPEG", quality=97)


CUE = """REM GENRE {genre}
REM DATE {year}
PERFORMER "{artist}"
TITLE "{album}"
FILE "{first}" WAVE
  TRACK 01 AUDIO
    TITLE "{t1}"
    PERFORMER "{artist}"
    INDEX 01 00:00:00
"""

EAC_LOG = """Exact Audio Copy V1.6 from 23. October 2020

EAC extraction logfile from 1. January {year}

{artist} / {album}

Used drive  : PIONEER BD-RW   BDR-209D   Adapter: 1  ID: 0

Track |   Start  |  Length  | Start sector | End sector
---------------------------------------------------------
    1  |  0:00.00 |  3:00.00 |         0    |   13500

Range status and errors
Selected range
     Filename {artist} - {album}.wav

Peak level 100.0 %
     Range quality 100.0 %
     Test CRC 00000000
     Copy CRC 00000000
     Copy OK

No errors occurred

AccurateRip summary
Track  1  accurately ripped (confidence 5)  [00000000]  (AR v2)

End of status report
"""


def build_library(music, albums, tracks, seconds=2.0):
    lib = os.path.join(music, "Artists")
    if os.path.isdir(lib):
        shutil.rmtree(lib, ignore_errors=True)
    os.makedirs(lib, exist_ok=True)
    wav = os.path.join(music, ".perf-tone.wav")
    os.makedirs(music, exist_ok=True)
    make_noise_wav(wav, seconds=seconds)
    for a in range(albums):
        artist = f"Perf Artist {a // 3 + 1:02d}"
        year = 1990 + (a % 30)
        album = f"{year} - Album {a:03d}"
        adir = os.path.join(lib, artist, album)
        os.makedirs(adir, exist_ok=True)
        is_cd = (a % 3 == 0)
        first = None
        for t in range(tracks):
            fpath = os.path.join(adir, f"{t + 1:02d} - Track {t + 1:02d}.flac")
            subprocess.run([FLAC, "-f", "-s", "--totally-silent", "-o", fpath, wav],
                           check=True)
            if first is None:
                first = os.path.basename(fpath)
            tags = {
                "ARTIST": artist,
                "ALBUMARTIST": artist,
                "ALBUM": album.split(" - ", 1)[1],
                "TITLE": f"Track {t + 1:02d}",
                "TRACKNUMBER": f"{t + 1:02d}",
                "TRACKTOTAL": str(tracks),
                "DATE": str(year),
                "MEDIA": "CD" if is_cd else "Digital Media",
                "SOURCE": "" if is_cd else "Digital",
                "GENRE": ["Rock", "Jazz", "Pop", "Ambient"][a % 4],
            }
            if a % 2 == 0 and t == 0:
                # An embedded lyric the formatter has to clean, and the sidecar
                # it is compared against.
                tags["LYRICS"] = BAD_LYRICS
                with open(os.path.join(adir, os.path.splitext(os.path.basename(fpath))[0]
                                       + ".lrc"), "w", encoding="utf-8") as fh:
                    fh.write(GOOD_LYRICS)
            tag_flac(fpath, tags)
        write_cover(os.path.join(adir, "cover.jpg"))
        if is_cd:
            with open(os.path.join(adir, "album.cue"), "w", encoding="utf-8") as fh:
                fh.write(CUE.format(genre="Rock", year=year, artist=artist,
                                    album=album.split(" - ", 1)[1], first=first,
                                    t1="Track 01"))
            with open(os.path.join(adir, f"{artist} - {album}.log"), "w",
                      encoding="utf-8") as fh:
                fh.write(EAC_LOG.format(year=year, artist=artist,
                                        album=album.split(" - ", 1)[1]))
        else:
            # A second image per non-CD album: the image pass has more than one
            # file to decide about.
            write_cover(os.path.join(adir, "back.jpg"), 600, 600)
    os.remove(wav)
    return lib


def build_staged(music, tracks=4):
    """A staged (downloaded) album for the import phase: WAVs need converting."""
    staged = os.path.join(music, "downloads", "Perf Staged Album")
    if os.path.isdir(staged):
        shutil.rmtree(staged, ignore_errors=True)
    os.makedirs(staged, exist_ok=True)
    wav = os.path.join(music, ".perf-tone.wav")
    make_noise_wav(wav, seconds=1.0)
    for t in range(tracks):
        dst = os.path.join(staged, f"{t + 1:02d} - Staged {t + 1:02d}.wav")
        shutil.copyfile(wav, dst)
    write_cover(staged + os.sep + "cover.jpg", 500, 500)
    os.remove(wav)
    return staged


# --------------------------------------------------------------------------- #
# Manifest: what the run WROTE, byte for byte
# --------------------------------------------------------------------------- #
def manifest(root):
    out = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in {".mlo", ".mlo_data", "__pycache__"}]
        for f in sorted(files):
            p = os.path.join(base, f)
            rel = os.path.relpath(p, root).replace("\\", "/")
            try:
                with open(p, "rb") as fh:
                    out[rel] = hashlib.sha1(fh.read()).hexdigest()
            except OSError as e:
                out[rel] = f"unreadable: {e}"
    return out


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #
def _count_http():
    """Count httpx client CONSTRUCTIONS and requests process-wide.

    The pipeline's provider calls are the one place where a process-wide
    resource shows up in a script's wall clock: `httpx.get` builds (and closes)
    a client per call, and a client's construction loads a TLS context — so
    "clients built" is the number that says whether the app reuses one.
    Returns (counts, stop); call counts.clear() before each script.
    """
    import httpx

    counts = {"clients": 0, "requests": 0}
    real_init = httpx.Client.__init__
    real_send = httpx.Client.send

    def init(self, *a, **kw):
        counts["clients"] += 1
        return real_init(self, *a, **kw)

    def send(self, *a, **kw):
        counts["requests"] += 1
        return real_send(self, *a, **kw)

    httpx.Client.__init__ = init
    httpx.Client.send = send

    def stop():
        httpx.Client.__init__ = real_init
        httpx.Client.send = real_send

    return counts, stop


def run_chain(ids, cfg, music):
    """Time each script through the app's own runner. Returns [(sid, label, secs, ...)]."""
    from mlo.paths import library_root

    httpx_counts, stop_http = _count_http()
    rows = []
    try:
        for sid in ids:
            label = script_runners.RUNNERS.get(sid, (f"script {sid}", None))[0]
            httpx_counts.update({"clients": 0, "requests": 0})
            t0 = time.perf_counter()
            res = script_runners.run_script(sid, cfg, targets=None)
            secs = time.perf_counter() - t0
            stats = res.get("stats") or {}
            note = ""
            if res.get("error"):
                note = "ERROR: " + str(res["error"])[:120]
            elif res.get("skipped"):
                note = "skipped: " + str(res.get("reason") or "")
            rows.append({"id": sid, "label": label, "seconds": secs,
                         "modified": stats.get("modified_count"),
                         "errors": stats.get("error_count"),
                         "http_clients": httpx_counts["clients"],
                         "http_requests": httpx_counts["requests"],
                         "note": note})
            print(f"    {sid:>2} {label:<28} {secs:8.2f}s  "
                  f"http={httpx_counts['clients']}c/{httpx_counts['requests']}r  "
                  f"{note or ''}", flush=True)
    finally:
        stop_http()
    return rows


def run_import_phase(cfg, music, tracks):
    """The import pipeline's own local steps, timed on a freshly staged album."""
    from mlo import flac as mlo_flac

    rows = []
    staged = build_staged(music, tracks=tracks)
    steps = [
        ("import: convert staged album to the library codec",
         lambda: mlo_flac.convert_album_lossless(staged, cfg)),
    ]
    for label, fn in steps:
        t0 = time.perf_counter()
        try:
            fn()
        except Exception as e:  # a step that raises is a finding, not a crash
            rows.append({"step": label, "seconds": time.perf_counter() - t0,
                         "note": f"raised: {e}"})
            continue
        rows.append({"step": label, "seconds": time.perf_counter() - t0,
                     "note": ""})
        print(f"    {label:<44} {rows[-1]['seconds']:8.2f}s", flush=True)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--music", default=SCRATCH)
    ap.add_argument("--albums", type=int, default=12)
    ap.add_argument("--tracks", type=int, default=5)
    ap.add_argument("--seconds", type=float, default=2.0,
                    help="length of every fixture track: 2 s keeps a run short, "
                         ">= 12 s makes DR/Key&BPM/Mood do their real decode "
                         "(a 3 s DR block needs 6 s of audio)")
    ap.add_argument("--only", default="")
    ap.add_argument("--skip", default="13,18")
    ap.add_argument("--build", default="auto", choices=("auto", "yes", "no"),
                    help="build the scratch library first (auto = when missing)")
    ap.add_argument("--json", default="")
    ap.add_argument("--import-phase", action="store_true", default=True)
    args = ap.parse_args()

    music = args.music
    if args.build == "yes" or (args.build == "auto"
                               and not os.path.isdir(os.path.join(music, "Artists"))):
        print(f"building scratch library: {args.albums} albums x {args.tracks} tracks "
              f"under {music}", flush=True)
        t0 = time.perf_counter()
        build_library(music, args.albums, args.tracks, seconds=args.seconds)
        print(f"  built in {time.perf_counter() - t0:.2f}s", flush=True)

    cfg = normalize_config({
        "music_folder": music,
        "first_run_done": True,
        # The harness measures the SHIPPED behaviour, so it turns nothing on
        # that ships off: no force flags, no review screens, no play-state
        # bookkeeping to a server that is not running.
        "targets": None,
    })
    cfg.pop("targets", None)

    ids = [int(x) for x in args.only.split(",") if x.strip()] or list(DEFAULT_RUN_ALL_ORDER)
    skip = {int(x) for x in args.skip.split(",") if x.strip()}
    ids = [s for s in ids if s not in skip]
    print(f"\nchain: {ids}  (skipping {sorted(skip)})", flush=True)

    report = {"music": music, "albums": args.albums, "tracks": args.tracks,
              "seconds": args.seconds, "skipped": sorted(skip),
              "scripts": [], "import_steps": [],
              "started": time.strftime("%Y-%m-%dT%H:%M:%S")}

    t_all = time.perf_counter()
    print("\nphase: chain", flush=True)
    report["scripts"] = run_chain(ids, cfg, music)
    report["chain_seconds"] = time.perf_counter() - t_all

    lib = os.path.join(music, "Artists")
    report["manifest"] = manifest(lib) if os.path.isdir(lib) else {}
    report["manifest_sha1"] = hashlib.sha1(
        json.dumps(report["manifest"], sort_keys=True).encode()).hexdigest()

    print("\nphase: import", flush=True)
    t_imp = time.perf_counter()
    report["import_steps"] = run_import_phase(cfg, music, args.tracks)
    report["import_seconds"] = time.perf_counter() - t_imp

    print(f"\n{'script':<34}{'seconds':>10}{'http':>14}")
    print("-" * 58)
    for r in report["scripts"]:
        http = (f"{r.get('http_clients', 0)}c/{r.get('http_requests', 0)}r"
                if r.get("http_requests") or r.get("http_clients") else "")
        print(f"{str(r['id']) + ' ' + r['label']:<34}{r['seconds']:>10.2f}{http:>14}")
    for r in report["import_steps"]:
        print(f"{r['step'][:33]:<34}{r['seconds']:>10.2f}")
    print("-" * 58)
    total = sum(r["seconds"] for r in report["scripts"]) + sum(
        r["seconds"] for r in report["import_steps"])
    print(f"{'TOTAL':<34}{total:>10.2f}")
    # 13 (lyrics fetch) and 18 (LRCLIB publish) only do anything over the
    # network, and 14 is beets' own import of the whole library (its cost is
    # beets', and it dominates a cold beets DB) — named separately so a change
    # to the local passes is not buried under them.
    net = {13, 14, 18}
    local = sum(r["seconds"] for r in report["scripts"] if r["id"] not in net)
    report["local_seconds"] = local
    report["network_seconds"] = sum(r["seconds"] for r in report["scripts"]
                                    if r["id"] in net)
    print(f"{'TOTAL (local, no 13/14/18)':<34}{local:>10.2f}")
    print(f"\nmanifest sha1: {report['manifest_sha1']}  "
          f"({len(report['manifest'])} files)")

    out = args.json or os.path.join(ROOT, "tools", "perf_pipeline.json")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(report, fh, indent=1, sort_keys=True)
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
