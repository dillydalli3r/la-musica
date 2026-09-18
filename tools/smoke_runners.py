"""Smoke-run the mlo script runners against a synthetic album.

Verifies the Run All pipeline (scripts 1-5, 7, 9-12) executes without
crashing on real (tiny) FLACs. 13 (network lyrics fetch) and 14 (full beets
import) are environment-dependent and only config-checked.

Run:  python tools/smoke_runners.py
Exits 1 when a runner raises OR reports in-run errors, 2 when no flac
encoder is available (nothing to encode fixtures with).
"""
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def find_flac():
    """The bundled flac encoder, wherever .dependencies keeps it.

    The dependency folder is version-stamped ("flac v1.5.0"), so look it up
    by prefix instead of pinning a version that a Dependencies update bumps.
    """
    dep = os.path.join(ROOT, ".dependencies")
    if os.path.isdir(dep):
        for entry in sorted(os.listdir(dep)):
            if entry.lower().startswith("flac"):
                cand = os.path.join(dep, entry, "flac.exe")
                if os.path.isfile(cand):
                    return cand
    return shutil.which("flac")


FLAC = find_flac()


def make_flac(path, title, track):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    wav = path + ".tmp.wav"
    # Broadband (white) noise, not silence, and with decorrelated channels:
    # AudioAuditor correctly flags digital silence as "fake lossless" and
    # identical channels as "fake stereo", which would make the audit runner
    # report errors for a fixture that is fine.
    rnd = random.Random(1234)  # deterministic fixtures
    frames = b"".join(
        struct.pack("<hh", rnd.randint(-8192, 8192), rnd.randint(-8192, 8192))
        for _ in range(44100)
    )
    with wave.open(wav, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(frames)
    subprocess.run([FLAC, "-f", "-s", "--totally-silent", "-o", path, wav], check=True)
    os.remove(wav)
    from mutagen.flac import FLAC as MFLAC

    f = MFLAC(path)
    f["TITLE"] = title
    f["ARTIST"] = "Smoke Artist"
    f["ALBUMARTIST"] = "Smoke Artist"
    f["ALBUM"] = "Smoke Album"
    f["TRACKNUMBER"] = f"{track:02d}"
    f["DISCNUMBER"] = "1"
    f["DATE"] = "2020"
    f["GENRE"] = "Test"
    f["MEDIA"] = "CD"
    f.save()


def main():
    if FLAC is None:
        print("SKIP: no flac.exe found in .dependencies or PATH")
        return 2
    tmp = tempfile.mkdtemp(prefix="mlo_runners_")
    music = os.path.join(tmp, "music")
    album = os.path.join(music, "Smoke Artist", "Smoke Album")
    os.makedirs(album)
    make_flac(os.path.join(album, "1-01 Song A.flac"), "Song A", 1)
    make_flac(os.path.join(album, "1-02 Song B.flac"), "Song B", 2)
    from PIL import Image

    Image.new("RGB", (600, 600), (200, 60, 60)).save(os.path.join(album, "cover.jpg"), "JPEG")

    from mlo.config import DEFAULT_CONFIG
    from mlo.tools import detect_all_tools

    base = {
        **DEFAULT_CONFIG,
        "music_folder": music,
        "targets": [album],
        "lyrics_format": "EMBEDDED",
        "reencode_images": False,
        "audit_thorough": False,
    }

    # The two FLACs are named like a CD rip ("1-01 …"), so script 1 marks them
    # MEDIA=CD — and a CD album is audited on its own evidence: the .log CRCs.
    # Give the fixture a REAL rip log (its Copy CRC lines computed from the
    # actual audio), so this suite exercises the verified path instead of
    # failing the CD gates for a fixture that has no log at all.
    _ff = None
    for _k in ("ffmpeg",):
        _ff = (detect_all_tools().get(_k) or {}).get("ffmpeg_exe")
    if _ff:
        from mlo import discs as _discs

        lines = ["Exact Audio Copy v1.6", "", "Track |  Start  |  Length  | Start sector | End sector",
                 "---------------------------------------------------------"]
        crcs = []
        for _i, _name in enumerate(sorted(f for f in os.listdir(album)
                                          if f.lower().endswith(".flac")), 1):
            _crc = _discs._audio_crc32(_ff, os.path.join(album, _name))
            crcs.append((_i, _crc))
            lines.append(f"  {_i}  | 00:0{_i}.00 | 00:00.10 | 0 | 8")
        lines.append("")
        for _i, _crc in crcs:
            lines += [f"Track  {_i}", f"     Copy CRC {str(_crc).upper()}", ""]
        with open(os.path.join(album, "CD-1.log"), "w", encoding="utf-8") as _fh:
            _fh.write("\n".join(lines))

    from mlo import (run_format_lyrics, run_format_cues, run_optimize_flacs,
                     run_grade_library, run_process_images, run_audit_library,
                     run_auto_tagging, run_format_all)
    from mlo.loudness import run_calc_dr_replaygain
    from mlo.accurip import run_generate_accurip
    from mlo.remux import run_remux_videos
    from mlo.audiometa import run_analyze_audiometa
    from mlo.moods import run_detect_mood_energy

    runners = [
        (1, "lyrics", run_format_lyrics),
        (2, "cues", run_format_cues),
        (3, "flac", run_optimize_flacs),
        (5, "images", run_process_images),
        (9, "accurip", run_generate_accurip),
        (11, "remux", run_remux_videos),
        (6, "audit", run_audit_library),
        (7, "dr", run_calc_dr_replaygain),
        (12, "audiometa", run_analyze_audiometa),
        (16, "mood", run_detect_mood_energy),
        (8, "autotag", run_auto_tagging),
        (4, "grade", run_grade_library),
        (10, "formatall", run_format_all),
    ]
    failures = []
    for sid, name, fn in runners:
        cfg = dict(base)
        cfg["stats"] = {"is_grader": name == "grade"}
        try:
            stats = fn(cfg)
            errs = [e for e in stats.get("errors", []) if e]
            print(f"script {sid:>2} {name:<10} OK   (errors in-run: {len(errs)})")
            for e in list(stats.get("errors", []))[:3]:
                print("      ", e)
            # A runner that reported errors did not do its job: a broken or
            # missing toolchain must fail the suite instead of printing OK.
            if errs:
                failures.append((sid, name, errs[:3]))
        except Exception as e:
            print(f"script {sid:>2} {name:<10} FAIL {type(e).__name__}: {e}")
            failures.append((sid, name, e))

    # format-all force path exercises the new force wiring
    cfg = dict(base)
    cfg["force_lyrics"] = True
    cfg["force_cue"] = True
    cfg["force_accurip"] = True
    cfg["force_auto_tag"] = True
    try:
        force_stats = run_format_all(cfg)
        errs = [e for e in force_stats.get("errors", []) if e]
        if errs:
            print(f"formatall force FAIL (errors in-run: {len(errs)}): {errs[:3]}")
            failures.append((10, "formatall-force", errs[:3]))
        else:
            print("formatall force OK")
    except Exception as e:
        print(f"formatall force FAIL {e}")
        failures.append((10, "formatall-force", e))

    # beets config generation (script 14 config path, no import run)
    try:
        from server.beetscfg import generate_config
        txt = generate_config(dict(base))
        assert "mloplugin" in txt and "write:" in txt
        print("script 14 beets    OK   (config generated)")
    except Exception as e:
        print(f"script 14 beets    FAIL {e}")
        failures.append((14, "beets-cfg", e))

    shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        print(f"RUNNERS_FAILED: {failures}")
        sys.exit(1)
    print("RUNNERS_OK")


if __name__ == "__main__":
    sys.exit(main())
