#!/usr/bin/env python3
"""The audit override is the verdict — for a digital-media track too.

The rule under test: the track editor's REAL / FAKE call is stored in the
`AUDIOAUDITOR_OVERRIDE` tag, it is applied after every derived verdict
(spec R25), and it therefore has to satisfy the AUDIT REQUIREMENT as well.
A DIGITAL-MEDIA album has no rip log to audit, so "Missing AUDIT tag (run
Audit Library)" is exactly the check the owner has to be able to answer by
hand — and answering it per track, from another modal, is not an answer.

Two ends of one rule are pinned here:

  * `mlo.grader._grade_album` — an override of REAL clears the AUDIT issue and
    is reported as the track's verdict; clearing it brings the requirement
    back; an override of FAKE still FAILS (an override is not a bypass).
  * `mlo.audit._write_audit_tag`, the ONE writer of the AUDIT verdict — a run
    that derived its own answer for a file the user has already decided keeps
    the user's value instead of overwriting it, which is what makes the call
    permanent across Audit Library runs.

Local tools only: flac.exe from .dependencies. The suite skips itself (exit 2)
when it is missing. No network.

Run:  python tools/test_audit_override_digital.py
"""
import os
import subprocess
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mlo import audit as audit_mod  # noqa: E402
from mlo.audio import AudioFile  # noqa: E402
from mlo.grader import _grade_album  # noqa: E402

FAILS = []


def ok(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILS.append(label)


def find_flac():
    deps = os.path.join(ROOT, ".dependencies")
    if not os.path.isdir(deps):
        return None
    for entry in os.listdir(deps):
        if entry.lower().startswith("flac"):
            cand = os.path.join(deps, entry, "flac.exe")
            if os.path.isfile(cand):
                return cand
    return None


FLAC_EXE = find_flac()
if not FLAC_EXE:
    print("test_audit_override_digital: SKIP — flac.exe not under .dependencies")
    sys.exit(2)


def make_flac(path):
    wav = path + ".wav"
    with wave.open(wav, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00\x00\x00" * 4410)
    subprocess.run([FLAC_EXE, "-s", "-f", "-8", "-o", path, wav],
                   check=True, capture_output=True)
    os.remove(wav)


def set_tags(path, tags):
    af = AudioFile(path)
    for k, v in tags.items():
        af.set_tag(k, v)


def audit_issues(album, cfg):
    """{issue: files} for one album, filtered to the AUDIT REQUIREMENT.

    Only the two wordings that fail the check — "Missing AUDIT tag" and
    "AUDIT tag is X (not REAL)". The CD path also emits an informational
    "AUDIT readout: nothing established …" when a leg could not be evaluated,
    which is a note about what could not be checked, not a failure, and it is
    deliberately left in place beside a manual override."""
    res = _grade_album(album, "EMBEDDED", cfg)
    return ({k: v for k, v in (res.get("issues") or {}).items()
             if "Missing AUDIT tag" in k or k.startswith("AUDIT tag is ")},
            res.get("tracks") or [])


TMP = tempfile.mkdtemp(prefix="mlo-audit-ovr-")
try:
    music = os.path.join(TMP, "Music", "Artists")
    album = os.path.join(music, "Artist", "Album (2020)")
    os.makedirs(album, exist_ok=True)
    track = os.path.join(album, "1-01 Song.flac")
    make_flac(track)
    # A digital-media track: no .log, no .cue — nothing for AudioAuditor to
    # read, which is the whole reason the manual verdict exists.
    set_tags(track, {"TITLE": "Song", "ARTIST": "Artist", "ALBUMARTIST": "Artist",
                     "ALBUM": "Album", "DATE": "2020", "TRACKNUMBER": "1",
                     "MEDIA": "Digital Media"})
    cfg = {"music_folder": os.path.join(TMP, "Music"), "grade_check_audit": True}

    print("== the requirement, and the override that answers it ==")
    issues, tracks = audit_issues(album, cfg)
    ok(any("Missing AUDIT tag" in k for k in issues),
       "digital media with no AUDIT tag and no override fails the audit check")
    ok(not tracks[0].get("audit_override"),
       "and reports no override")

    set_tags(track, {"AUDIOAUDITOR_OVERRIDE": "REAL"})
    issues, tracks = audit_issues(album, cfg)
    ok(not issues, "override REAL clears the audit issue")
    ok(tracks[0].get("audit") == "REAL" and tracks[0].get("audit_override") == "REAL",
       "and the track's verdict IS the override")

    set_tags(track, {"AUDIOAUDITOR_OVERRIDE": ""})
    issues, _tracks = audit_issues(album, cfg)
    ok(any("Missing AUDIT tag" in k for k in issues),
       "clearing it brings the requirement back")

    set_tags(track, {"AUDIOAUDITOR_OVERRIDE": "FAKE"})
    issues, tracks = audit_issues(album, cfg)
    ok(any("FAKE" in k for k in issues),
       "override FAKE still FAILS — an override is not a bypass")

    set_tags(track, {"AUDIOAUDITOR_OVERRIDE": "REAL"})
    set_tags(track, {"MEDIA": "CD"})
    issues, _tracks = audit_issues(album, cfg)
    ok(not issues,
       "a CD with override REAL is decided by the user, not re-derived")

    print("== the Audit Library run keeps the user's call ==")
    set_tags(track, {"MEDIA": "Digital Media", "AUDIT": ""})
    set_tags(track, {"AUDIOAUDITOR_OVERRIDE": "REAL"})
    changed, _rem, _add, err = audit_mod._write_audit_tag(track, "FAKE")
    ok(changed and not err, "the verdict writer ran")
    ok(str(AudioFile(track).get_tag("AUDIT") or "").strip().upper() == "REAL",
       "a run that derived FAKE writes the user's REAL instead")

    set_tags(track, {"AUDIOAUDITOR_OVERRIDE": ""})
    audit_mod._write_audit_tag(track, "FAKE")
    ok(str(AudioFile(track).get_tag("AUDIT") or "").strip().upper() == "FAKE",
       "with no override the run's own verdict lands as before")
finally:
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)

print()
if FAILS:
    print("FAILED: %d check(s)" % len(FAILS))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("all audit-override checks passed")