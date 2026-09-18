#!/usr/bin/env python3
"""CD rip integrity outranks the spectrogram detectors.

The rule under test: a CD rip whose integrity is VERIFIED — by the .log CRCs
of its own rip log, or by a REAL .accurip — passes auditing on that evidence
alone. AudioAuditor's spectral verdict (fake-lossless / MQA / clipping
detectors) may add warning flags, but it can never make a provably intact disc
fail, and a missing AudioAuditor (Linux, Docker, no CUETools) can never leave
such a rip without a verdict.

Local tools only: flac.exe from .dependencies, ffmpeg from .dependencies or
PATH. The suite skips itself when either is missing. No network.

Run:  python tools/test_cd_audit.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mlo import discs as discs_mod  # noqa: E402
from mlo.grader import _grade_album  # noqa: E402
from mlo.naming import DEFAULT_NAMING_SCRIPT  # noqa: E402

FAILS = []


def ok(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILS.append(label)


def flac_exe():
    for root, _dirs, files in os.walk(os.path.join(ROOT, ".dependencies")):
        for f in files:
            if f.lower() == "flac.exe":
                return os.path.join(root, f)
    return shutil.which("flac")


def ffmpeg_exe():
    base = os.path.join(ROOT, ".dependencies")
    for root, _dirs, files in os.walk(base):
        for f in files:
            if f.lower() in ("ffmpeg.exe", "ffmpeg"):
                return os.path.join(root, f)
    return shutil.which("ffmpeg")


FLAC, FFMPEG = flac_exe(), ffmpeg_exe()
if not FLAC:
    print("skipped: no flac encoder available")
    sys.exit(0)

TMP = tempfile.mkdtemp(prefix="mlo-cd-audit-")
ALBUM = os.path.join(TMP, "Artists", "Artist", "Ripped (2020)")
os.makedirs(ALBUM, exist_ok=True)
TRACK = os.path.join(ALBUM, "01 Song.flac")


def build_track():
    wav = os.path.join(TMP, "tone.wav")
    with wave.open(wav, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x01\x00\x02" * 4410)
    subprocess.run([FLAC, "-s", "-f", "-8", "-o", TRACK, wav], check=True,
                   capture_output=True)
    from mutagen.flac import FLAC as MF
    f = MF(TRACK)
    for k, v in (("TITLE", "Song"), ("ARTIST", "Artist"), ("ALBUMARTIST", "Artist"),
                 ("ALBUM", "Album"), ("DATE", "2020"), ("MEDIA", "CD"),
                 ("AUDIT", "FAKE")):
        f[k] = [v]
    f.save()


def write_log(crc):
    with open(os.path.join(ALBUM, "CD-1.log"), "w", encoding="utf-8") as fh:
        fh.write("Exact Audio Copy v1.6\n\n"
                 "Track |  Start  |  Length  | Start sector | End sector\n"
                 "---------------------------------------------------------\n"
                 f"  1  | 00:00.00 | 00:00.10 | 0 | 8\n\n"
                 "Track  1\n"
                 f"     Copy CRC {crc}\n")


def write_accurip(status_line="Accurately ripped"):
    # Canonical form: grading checks the .accurip's own formatting, and that
    # rule is not what this suite is about.
    from mlo.accurip import _canonical_accurip_text

    text = ("[CUETools log; Date: 2026-01-01 00:00:00; Version: 2.1.6]\n"
            "[AccurateRip ID: 00000001-00000001-00000001]\n\n"
            "Track   [  CRC   |   V2   ] Status\n"
            f" 01     [00000001|00000002] ({'V1+V2/Y' if status_line == 'Accurately ripped' else '0/Y'}) {status_line}\n")
    with open(os.path.join(ALBUM, "CD-1.accurip"), "w", encoding="utf-8") as fh:
        fh.write(_canonical_accurip_text(text))


def _cfg(**extra):
    """A full config (so nothing is missing) with every grading check OFF
    except the AUDIT requirement this suite is about: the album's pass/fail
    then reports that one rule and nothing else."""
    from mlo.config import normalize_config

    cfg = dict(normalize_config({"music_folder": TMP}))
    for k in [k for k in cfg if k.startswith("grade_check_")]:
        cfg[k] = False
    cfg["grade_check_audit"] = True
    cfg.update(extra)
    return cfg


CFG = _cfg()

build_track()
real_crc = None
if FFMPEG:
    real_crc = discs_mod._audio_crc32(FFMPEG, TRACK)

print("== a valid EAC log checksum outranks the stored verdict ==")
import mlo.discs as _dm  # noqa: E402

# The log must EXIST for the grader to check its checksum at all; the content
# does not matter here because the check itself is stubbed below.
write_log((f"{real_crc:0>8}".upper() if real_crc else "00000000"))
_real_check = _dm.check_log_checksum
_dm.check_log_checksum = lambda _p: ("ok", None)
try:
    res = _grade_album(ALBUM, "EMBEDDED", _cfg())
    tr = res["tracks"][0]
    ok(tr.get("checksum_status") == "REAL",
       f"the log checksum verdict is REAL ({tr.get('checksum_status')})")
    ok(tr.get("audit") == "REAL" and "AUDIT" not in tr.get("issues", []),
       f"a stored AUDIT=FAKE no longer decides a verified rip "
       f"({tr.get('audit')} / {tr.get('issues')})")
    ok(tr.get("audit_verified") == "log-checksum",
       f"the verdict says WHAT verified it ({tr.get('audit_verified')})")
    ok(res["pass_count"] == res["total_checks"],
       f"the album passes a verified CD rip ({res['pass_count']}/{res['total_checks']})")
finally:
    _dm.check_log_checksum = _real_check

print("== AccurateRip alone is enough ==")
os.remove(os.path.join(ALBUM, "CD-1.log"))
write_accurip()
res = _grade_album(ALBUM, "EMBEDDED", _cfg())
tr = res["tracks"][0]
ok(tr.get("accuraterip_status") == "REAL",
   f"the .accurip verdict is REAL ({tr.get('accuraterip_status')})")
ok(tr.get("audit") == "REAL" and res["pass_count"] == res["total_checks"],
   f"an accurately-ripped disc passes without a valid log checksum "
   f"({res['pass_count']}/{res['total_checks']})")

print("== no verification: the old rule still applies ==")
os.remove(os.path.join(ALBUM, "CD-1.accurip"))
res = _grade_album(ALBUM, "EMBEDDED", _cfg())
tr = res["tracks"][0]
ok("AUDIT" in tr.get("issues", []),
   f"an unverifiable rip still fails the AUDIT requirement ({tr.get('issues')})")
ok(res["pass_count"] < res["total_checks"],
   f"…and the album does not pass ({res['pass_count']}/{res['total_checks']})")
ok(any("AUDIT tag is FAKE" in i or "Missing AUDIT tag" in i
       for i in res["issues"]),
   f"the failure names the tag ({res['issues']})")

print("== script 6 writes the verified verdict itself ==")
# AudioAuditorCLI is a Windows-only .NET tool that is NOT vendored on the CI
# runners / a Linux install: `run_audit_library` refuses to run without it, so
# the sections that drive script 6 end-to-end can only be exercised where it
# exists. The grader-side assertions above are the portable half of the suite.
from mlo.tools import detect_all_tools as _detect_all_tools  # noqa: E402

HAS_AA = bool((_detect_all_tools().get("audioauditor") or {}).get("cli_exe"))
if not HAS_AA:
    print("  skipped: AudioAuditorCLI not installed (Windows-only) — "
          "script 6's own verdict is not exercised here")
elif not FFMPEG or not real_crc:
    print("  skipped: no ffmpeg")
else:
    write_log(f"{real_crc:0>8}".upper())
    from mutagen.flac import FLAC as MF
    from mlo.audit import run_audit_library
    f = MF(TRACK)
    f["AUDIT"] = ["FAKE"]
    f.save()
    # The other CD gates are switched off so this isolates ONE rule: the
    # matching .log CRC vs AudioAuditor's spectral verdict. The fixture is a
    # synthetic tone, so a real AudioAuditor reports "fake lossless" — exactly
    # the disagreement a CD rip must survive.
    run_audit_library({"music_folder": TMP, "audit_verify_cd_checksums": True,
                       "audit_cd_require_both": True, "write_audit_tag": True,
                       "audit_integrity": True, "grade_verbose": False,
                       "audit_require_accuraterip": False,
                       "audit_verify_log_checksum": False,
                       "audit_log_score_threshold": 0})
    verdict = str((MF(TRACK).get("AUDIT") or [""])[0]).strip().upper()
    ok(verdict == "REAL",
       f"a CRC-verified rip is REAL even when AudioAuditor says otherwise "
       f"({verdict})")

print("== a CRC-verified rip needs no verifiable log SHA256 ==")
if not HAS_AA:
    print("  skipped: AudioAuditorCLI not installed (Windows-only)")
elif not FFMPEG or not real_crc:
    print("  skipped: no ffmpeg")
else:
    # The user's rule: the .log's per-track CRCs matching the audio is proof
    # enough — a log WITHOUT a verifiable EAC SHA256 (older EAC, an edited
    # log) must not fail tracks it just proved intact.
    write_log(f"{real_crc:0>8}".upper())
    from mutagen.flac import FLAC as MF

    from mlo.audit import run_audit_library
    f = MF(TRACK)
    f["AUDIT"] = ["FAKE"]
    f.save()
    for state in ("missing", "invalid"):
        _dm.check_log_checksum = lambda _p, s=state: (s, f"stub {s}")
        try:
            run_audit_library({"music_folder": TMP, "audit_verify_cd_checksums": True,
                               "audit_cd_require_both": True, "write_audit_tag": True,
                               "audit_integrity": True, "grade_verbose": False,
                               "audit_require_accuraterip": False,
                               "audit_verify_log_checksum": True,
                               "audit_log_score_threshold": 0})
        finally:
            _dm.check_log_checksum = _real_check
        verdict = str((MF(TRACK).get("AUDIT") or [""])[0]).strip().upper()
        ok(verdict == "REAL",
           f"a log whose checksum is {state} still passes on its matching "
           f"track CRCs ({verdict})")

print("== the viewer trusts the rip's evidence over a stale tag ==")
if not FFMPEG or not real_crc:
    print("  skipped: no ffmpeg")
else:
    # AUDIT is not a graded check on the user's install (grade_check_audit is
    # off), but the library still SHOWS the per-track verdict: an intact CD
    # rip must not render FAKE off a tag written by an older run.
    write_log(f"{real_crc:0>8}".upper())
    from mutagen.flac import FLAC as MF
    f = MF(TRACK)
    f["AUDIT"] = ["FAKE"]
    f.save()
    _dm.check_log_checksum = lambda _p: ("ok", None)
    try:
        res = _grade_album(ALBUM, "EMBEDDED", _cfg(grade_check_audit=False))
    finally:
        _dm.check_log_checksum = _real_check
    tr = res["tracks"][0]
    ok(tr.get("audit") == "REAL",
       f"a verified rip reads REAL in the viewer with the AUDIT check off "
       f"({tr.get('audit')})")

shutil.rmtree(TMP, ignore_errors=True)
print()
if FAILS:
    print("FAILED: %d check(s)" % len(FAILS))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("all CD-audit checks passed")
