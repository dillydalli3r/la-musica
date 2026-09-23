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

print("== a log that predates EAC checksums is not missing one ==")
def write_log_header(header, crc="00000000"):
    """A rip log with the EAC version the header names and NO checksum line:
    what the version rule reads is the header, never the log's date."""
    with open(os.path.join(ALBUM, "CD-1.log"), "w", encoding="utf-8") as fh:
        fh.write(header + "\n\n"
                 "Track |  Start  |  Length  | Start sector | End sector\n"
                 "---------------------------------------------------------\n"
                 "  1  | 00:00.00 | 00:00.10 | 0 | 8\n\n"
                 "Track  1\n"
                 f"     Copy CRC {crc}\n")


# Spec §3: "XLD and older EAC logs pass (nothing claimed, nothing refuted)".
# EAC only began writing the Rijndael log checksum in 1.0, so a 0.99-era log
# (this suite's fixture is a real 2008 one) cannot be missing a line its
# version never wrote — and treating it as 'missing' failed the whole disc.
write_log_header("Exact Audio Copy V0.99 prebeta 4 from 23. January 2008")
state, detail = discs_mod.check_log_checksum(os.path.join(ALBUM, "CD-1.log"))
ok(state == "unsupported",
   f"an EAC 0.99 log is 'unsupported', not 'missing' ({state}: {detail})")
res = _grade_album(ALBUM, "EMBEDDED", _cfg(grade_check_log_checksum=True))
tr = res["tracks"][0]
ok(tr.get("checksum_status") == "NONE" and "LOG_CHECKSUM" not in tr.get("issues", []),
   f"…so the disc does not fail grading on it ({tr.get('checksum_status')} / "
   f"{tr.get('issues')})")

# A MODERN log whose checksum line is gone is still read as 'missing' — the
# reader names the case — but a log checksum is never REQUIRED (spec R27), so
# its absence is not evidence against the rip and grading does not charge it.
# Only a checksum the log CARRIES, and that does not verify, fails the disc.
write_log_header("Exact Audio Copy V1.6 from 23. October 2020")
state, detail = discs_mod.check_log_checksum(os.path.join(ALBUM, "CD-1.log"))
ok(state == "missing",
   f"an EAC 1.6 log with no checksum line still reads 'missing' ({state}: {detail})")
res = _grade_album(ALBUM, "EMBEDDED", _cfg(grade_check_log_checksum=True))
tr = res["tracks"][0]
ok(tr.get("checksum_status") == "NONE" and "LOG_CHECKSUM" not in tr.get("issues", []),
   f"…but it is not required, so grading does not charge it "
   f"({tr.get('checksum_status')} / {tr.get('issues')})")

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
    # …and the album is one check short on the LEG THE APP CAN FIX: this rip
    # carries no LOG_GRADE, so the score leg has nothing behind it and charges
    # (script 6 scores the log). The AccurateRip leg no longer does — a
    # pressing the database has never seen reads exactly like a disc with no
    # .accurip and nothing can tell the two apart — so it is said as NOT
    # CHECKED in `notes` and charged to nobody: the same missing evidence,
    # reported for what it is, one leg apart.
    ok(res["pass_count"] == res["total_checks"] - 1
       and any("'log-score' evidence" in i for i in res["issues"])
       and any("'accuraterip'" in n and "not checked" in n
               for n in res.get("notes") or [])
       and not any("'accuraterip' evidence" in i for i in res["issues"]),
       f"a rip verified by its log is REAL and the album passes, with the "
       f"unverifiable AccurateRip leg said as not checked "
       f"({res['pass_count']}/{res['total_checks']}, "
       f"issues={list(res['issues'])[:2]}, notes={len(res.get('notes') or [])})")
finally:
    _dm.check_log_checksum = _real_check

print("== AccurateRip alone is enough ==")
os.remove(os.path.join(ALBUM, "CD-1.log"))
write_accurip()
res = _grade_album(ALBUM, "EMBEDDED", _cfg())
tr = res["tracks"][0]
ok(tr.get("accuraterip_status") == "REAL",
   f"the .accurip verdict is REAL ({tr.get('accuraterip_status')})")
ok(res["pass_count"] == res["total_checks"] - 1
   and any("'log-score' evidence" in i for i in res["issues"])
   and not any("'checksums' evidence" in i for i in res["issues"]),
   f"an accurately-ripped disc reads REAL, and the album is one check short "
   f"while the score leg has no LOG_GRADE — the .log's absence is NOT charged "
   f"to the checksums evidence (R27) "
   f"({res['pass_count']}/{res['total_checks']}, {res['issues']})")

print("== every leg established: the album passes ==")
# The other side of the rule above. With the .accurip in place AND the log's
# checksum verifying, each required leg has evidence, nothing is listed, and
# the album is a pass — which is what running script 9 over a disc the
# AccurateRip database knows clears the readout the case above names.
write_log((f"{real_crc:0>8}".upper() if real_crc else "00000000"))
_dm.check_log_checksum = lambda _p: ("ok", None)
try:
    res = _grade_album(ALBUM, "EMBEDDED", _cfg(audit_log_score_threshold=0))
finally:
    _dm.check_log_checksum = _real_check
ok(res["pass_count"] == res["total_checks"] and not res["issues"],
   f"a disc whose every required leg has evidence is a pass "
   f"({res['pass_count']}/{res['total_checks']}, {res['issues']})")

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
    import mlo.audit as _audit_mod
    from mlo.audit import run_audit_library
    f = MF(TRACK)
    f["AUDIT"] = ["FAKE"]
    f.save()
    # The other CD gates are switched off so this isolates ONE rule: the
    # matching .log CRC vs AudioAuditor's spectral verdict. The fixture is a
    # synthetic tone, so a real AudioAuditor reports "fake lossless" — exactly
    # the disagreement a CD rip must survive: the verdict stays REAL and the
    # disagreement is recorded as a WARNING (never as a verdict of its own).
    _aa_lines = []
    _real_audit_log = _audit_mod.log
    _audit_mod.log = lambda msg, *a, **k: _aa_lines.append(str(msg))
    try:
        run_audit_library({"music_folder": TMP, "audit_verify_cd_checksums": True,
                           "audit_cd_require_both": True, "write_audit_tag": True,
                           "audit_integrity": True, "grade_verbose": False,
                           "audit_require_accuraterip": False,
                           "audit_verify_log_checksum": False,
                           "audit_log_score_threshold": 0})
    finally:
        _audit_mod.log = _real_audit_log
    verdict = str((MF(TRACK).get("AUDIT") or [""])[0]).strip().upper()
    ok(verdict == "REAL",
       f"a CRC-verified rip is REAL even when AudioAuditor says otherwise "
       f"({verdict})")
    ok(any("AudioAuditor reports" in l and "warning only" in l for l in _aa_lines),
       f"…and the disagreement is recorded as a warning, not a verdict "
       f"({[l for l in _aa_lines if 'AudioAuditor reports' in l]})")

print("== the rip log's own SHA256 is one of the three legs ==")
if not HAS_AA:
    print("  skipped: AudioAuditorCLI not installed (Windows-only)")
elif not FFMPEG or not real_crc:
    print("  skipped: no ffmpeg")
else:
    from mutagen.flac import FLAC as MF

    def audit_once(**over):
        """Run script 6 over the fixture and return (AUDIT tag, log lines).

        The tag is cleared first, so "no verdict written" is distinguishable
        from a FAKE: every row below asserts WHICH of the two a run produced.
        Only the keys a row is about are overridden — the three legs have
        their own settings and a verdict that is always REAL would prove
        nothing about them.
        """
        cfg = {"music_folder": TMP, "audit_verify_cd_checksums": True,
               "audit_cd_require_both": True, "write_audit_tag": True,
               "audit_integrity": False, "grade_verbose": False,
               "audit_require_accuraterip": False,
               "audit_verify_log_checksum": True,
               "audit_log_score_threshold": 0}
        cfg.update(over)
        f = MF(TRACK)
        if "AUDIT" in f:
            del f["AUDIT"]
        f.save()
        lines = []
        _real_audit_log = _audit_mod.log
        _audit_mod.log = lambda msg, *a, **k: lines.append(str(msg))
        try:
            run_audit_library(cfg)
        finally:
            _audit_mod.log = _real_audit_log
        tag = MF(TRACK).get("AUDIT")
        return (str(tag[0]).strip().upper() if tag else ""), lines

    write_log(f"{real_crc:0>8}".upper())
    # A log that contradicts itself about its own bytes — a checksum that does
    # not verify — cannot be trusted about the CRCs it prints, so this leg
    # fails even though the printed CRC matches the audio. It used to be
    # exempted on the spot by that very match, which is how a doctored log
    # still produced AUDIT=REAL.
    _dm.check_log_checksum = lambda _p: ("invalid", "stub invalid")
    try:
        verdict, _lines = audit_once()
    finally:
        _dm.check_log_checksum = _real_check
    ok(verdict == "FAKE",
       f"a log whose SHA256 does not verify fails the checksums leg even on "
       f"matching CRCs ({verdict!r})")

    # …and an ABSENT checksum is the opposite case (R27): a log that carries
    # none claims nothing, so it is not required and the disc is decided by
    # its CRCs. The run still names the case in its log.
    _dm.check_log_checksum = lambda _p: ("missing", "stub missing")
    try:
        verdict, lines = audit_once()
    finally:
        _dm.check_log_checksum = _real_check
    ok(verdict == "REAL",
       f"a log that carries no checksum is judged on its matching CRCs "
       f"({verdict!r}, {[l for l in lines if 'not required' in l]})")

    # …and the pre-1.0-EAC exemption still holds, with the REAL reader (no
    # stub): EAC only began writing the log checksum in 1.0, so a 0.99-era log
    # claims nothing and refutes nothing — the leg is decided by the CRCs.
    write_log_header("Exact Audio Copy V0.99 prebeta 4 from 23. January 2008",
                     f"{real_crc:0>8}".upper())
    verdict, _lines = audit_once()
    ok(verdict == "REAL",
       f"a log that predates EAC checksums is REAL on its matching CRCs "
       f"({verdict!r})")

print("== the CD verdict is the AND of its three legs ==")
# One row per combination (Requirements 3): the rip log's SCORE
# (audit_log_score_threshold / Logchecker), the disc's CHECKSUMS (the .log's
# Copy CRC against the audio, plus the log's own SHA256), and ACCURATERIP (the
# .accurip). REAL only when all three pass; any leg failing is FAKE. The legs
# are driven with the suite's own stubs: grade_album_logs for the score, the
# log's Copy CRC + check_log_checksum for the checksums, and the .accurip
# itself for AccurateRip.
if not HAS_AA:
    print("  skipped: AudioAuditorCLI not installed (Windows-only)")
elif not FFMPEG or not real_crc:
    print("  skipped: no ffmpeg")
else:
    _real_grade_logs = _dm.grade_album_logs

    _SCORE = [True]

    def score():
        """A Logchecker score for every disc, so a row can put the score leg
        above or below the threshold without a scorer on the machine."""
        return ({1: (100 if _SCORE[0] else 40)}, [], [])

    def set_score(ok):
        _SCORE[0] = ok

    _dm.grade_album_logs = lambda *a, **k: score()

    def row(score_ok, crc_ok, ar, crc=None):
        """(expected verdict, what the row drove) for one combination."""
        set_score(score_ok)
        write_log((f"{real_crc:0>8}".upper() if crc_ok else "00000000")
                  if crc is None else crc)
        _dm.check_log_checksum = (
            (lambda _p: ("ok", None)) if crc_ok
            else (lambda _p: ("invalid", "stub invalid")))
        ap = os.path.join(ALBUM, "CD-1.accurip")
        if os.path.isfile(ap):
            os.remove(ap)
        if ar is not None:
            write_accurip("Accurately ripped" if ar else "No match")
        verdict, lines = audit_once(audit_require_accuraterip=True,
                                    audit_verify_log_checksum=True,
                                    audit_log_score_threshold=100)
        return verdict, lines

    try:
        # Each row is (score, checksums, AccurateRip) -> the ONE combination
        # that reaches REAL is all three passing.
        for score_ok, crc_ok, ar in ((True, True, True),
                                     (False, True, True),
                                     (True, False, True),
                                     (True, True, False),
                                     (False, False, True),
                                     (False, True, False),
                                     (True, False, False),
                                     (False, False, False)):
            verdict, _lines = row(score_ok, crc_ok, ar)
            want = "REAL" if (score_ok and crc_ok and ar) else "FAKE"
            ok(verdict == want,
               f"score={'100' if score_ok else '40'}, CRCs="
               f"{'match' if crc_ok else 'mismatch'}, "
               f"AccurateRip={'pass' if ar else 'fail'} → {want} "
               f"({verdict!r})")
        # The .accurip's verdict is read from the FILE, and a "No match" is a
        # leg failure even when the .log CRC just matched the audio. With no
        # .accurip AND no CUETools to make one the leg cannot be evaluated at
        # all — "cannot check" is not "did not match", so the file keeps NO
        # verdict and the run names the leg it could not evaluate.
        set_score(True)
        write_log(f"{real_crc:0>8}".upper())
        os.remove(os.path.join(ALBUM, "CD-1.accurip"))
        _dm.check_log_checksum = lambda _p: ("ok", None)
        import mlo.accurip as _accurip_mod

        _real_arcue = _accurip_mod.resolve_arcue_exe
        _accurip_mod.resolve_arcue_exe = lambda _tools: None
        try:
            verdict, lines = audit_once(audit_require_accuraterip=True,
                                        audit_verify_log_checksum=True,
                                        audit_log_score_threshold=100)
        finally:
            _accurip_mod.resolve_arcue_exe = _real_arcue
        ok(verdict == "",
           f"a CD whose AccurateRip leg cannot be evaluated keeps NO verdict "
           f"({verdict!r})")
        ok(any("missing 'accuraterip' evidence" in l for l in lines),
           f"…and the run names the leg that is missing "
           f"({[l for l in lines if 'missing' in l]})")
    finally:
        _dm.grade_album_logs = _real_grade_logs
        _dm.check_log_checksum = _real_check

print("== the viewer trusts the rip's evidence over a stale tag ==")
if not FFMPEG or not real_crc:
    print("  skipped: no ffmpeg")
else:
    # AUDIT is not a graded check on the user's install (grade_check_audit is
    # off), but the library still SHOWS the per-track verdict: an intact CD
    # rip must not render FAKE off a tag written by an older run.
    write_log(f"{real_crc:0>8}".upper())
    from mutagen.flac import FLAC as MF

    def _grade_now(with_tag):
        f = MF(TRACK)
        if with_tag:
            f["AUDIT"] = ["FAKE"]
        elif "AUDIT" in f:
            del f["AUDIT"]
        f.save()
        _dm.check_log_checksum = lambda _p: ("ok", None)
        try:
            # audit_log_score_threshold 0: the suite's own script-6 runs above
            # wrote LOG_GRADE=0 for this synthetic log, and the score leg is
            # not what these two cases are about (they are about the
            # checksum/evidence reading).
            return _grade_album(ALBUM, "EMBEDDED",
                                _cfg(grade_check_audit=False,
                                     audit_log_score_threshold=0))
        finally:
            _dm.check_log_checksum = _real_check

    # R26's own case: the track carries NO stamped verdict and its rip log
    # verifies, so the readout is REAL on that evidence alone.
    tr = _grade_now(with_tag=False)["tracks"][0]
    ok(tr.get("audit") == "REAL",
       f"an UNSTAMPED verified rip reads REAL in the viewer with the AUDIT "
       f"check off ({tr.get('audit')}, {tr.get('audit_verified')})")

    # …and the aligned case: the verdict needs all three legs, so a stamped
    # tag is never overruled into REAL by one leg, and the readout NAMES the
    # legs nothing established instead (here: no LOG_GRADE, no .accurip — this
    # fixture has neither, and "we could not check" must not read as REAL).
    tr = _grade_now(with_tag=True)["tracks"][0]
    ok(tr.get("audit") != "REAL"
       and sorted(tr.get("audit_legs_missing") or []) == ["accuraterip"],
       f"a stamped CD with a leg nothing established is not overruled into "
       f"REAL, and the readout names the missing legs "
       f"({tr.get('audit')}, {tr.get('audit_legs')})")

print("== the phar's checksum word is only evidence when it validated ==")
# Logchecker does not compute the log's SHA256 itself: it shells out to the
# pypi `eac-logchecker` script and prints `Checksum: checksum_ok` either way.
# On the owner's container the helper was missing, so a real EAC 1.3 log with
# one digit of "Peak level" changed came back `Score 100` / `checksum_ok` and
# the app recorded it as verified. The fallback reads the phar's own notice and
# the helper on PATH now — the second is what holds for a log the phar cannot
# even parse, which prints no notice at all.
with open(os.path.join(ALBUM, "CD-1.log"), "w", encoding="utf-8") as fh:
    fh.write("Exact Audio Copy V1.3 from 2. September 2016\n\n"
             "Track |  Start  |  Length  | Start sector | End sector\n"
             "---------------------------------------------------------\n"
             "  1  | 00:00.00 | 00:00.10 | 0 | 8\n\n"
             "Track  1\n     Copy CRC 00000000\n\n"
             "==== Log checksum "
             + "0" * 64 + " ====\n")
CHECKSUM_LOG = os.path.join(ALBUM, "CD-1.log")

_saved_csum = (discs_mod.HAS_EAC_CHECKER, discs_mod.eac_logchecker,
               discs_mod.run_tool, discs_mod._eac_helper_on_path)
try:
    # No verifier in-process, so the phar fallback is the path under test.
    discs_mod.HAS_EAC_CHECKER = False
    discs_mod.eac_logchecker = None

    def _phar(text):
        def fake(argv, **kw):
            class P:
                returncode = 0
                stdout = text
                stderr = ""
            return P()
        return fake

    NOTICE = ("Ripper  : EAC\nVersion : 1.3\nLanguage: en\nScore   : 100\n"
              "Checksum: checksum_ok\nDetails :\n"
              "    [Notice] Could not find EAC logchecker, checksum not validated.\n")

    discs_mod.run_tool = _phar(NOTICE)
    discs_mod._eac_helper_on_path = lambda path=None: False
    state, detail = discs_mod.check_log_checksum(CHECKSUM_LOG)
    ok(state == "unverified",
       f"a 'checksum_ok' the phar could not validate reads 'unverified', never "
       f"'ok' ({state}: {detail})")

    discs_mod._eac_helper_on_path = lambda path=None: True
    # …and with the helper present AND the phar not saying it failed to
    # validate, its word stands: `ok`. (The notice above is decisive on its
    # own — the phar saying it did not validate is not overruled by PATH.)
    discs_mod.run_tool = _phar(
        "Ripper  : EAC\nVersion : 1.3\nLanguage: en\nScore   : 100\n"
        "Checksum: checksum_ok\nDetails :\n"
        "    [Notice] Log checksum verified.\n")
    state, _detail = discs_mod.check_log_checksum(CHECKSUM_LOG)
    ok(state == "ok",
       f"the same word WITH the helper on PATH is a pass ({state})")

    discs_mod.run_tool = _phar(NOTICE.replace("checksum_ok", "checksum_invalid"))
    state, _detail = discs_mod.check_log_checksum(CHECKSUM_LOG)
    ok(state == "invalid", f"a checksum the phar refuses is 'invalid' ({state})")

    discs_mod.run_tool = _phar("Ripper  : unknown\nVersion : \nScore   : 0\n"
                               "Checksum: checksum_ok\nDetails :\n"
                               "    Unknown log file, could not determine ripper.\n")
    state, _detail = discs_mod.check_log_checksum(CHECKSUM_LOG)
    ok(state == "unverified",
       f"a log the phar cannot parse is 'unverified', not 'ok' ({state})")
finally:
    (discs_mod.HAS_EAC_CHECKER, discs_mod.eac_logchecker, discs_mod.run_tool,
     discs_mod._eac_helper_on_path) = _saved_csum

shutil.rmtree(TMP, ignore_errors=True)

print()
if FAILS:
    print("FAILED: %d check(s)" % len(FAILS))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("all CD-audit checks passed")
