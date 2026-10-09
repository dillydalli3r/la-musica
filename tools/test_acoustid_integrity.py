#!/usr/bin/env python3
"""ACOUSTID_ID integrity: the pair is written by every import path, the
repair completes the owner's CD rip with NO service at all, and a file
nothing can identify is left alone — and still named.

The owner's report is the fixture: a FLAC in a CD-rip album folder, named by
the app's own naming script with the recording MBID and the release group
MBID in the name, carrying a fingerprint from another tagger and no
`ACOUSTID_ID` — the state `mlo.grader` fails with "Missing ACOUSTID_ID (run
Fix AcoustID pairs)". The repair used to ask AcoustID for the recording, and
for a rip the database does not know (or a key that cannot be used) it wrote
NOTHING while the grader kept demanding the id — an id the file already
states, in `MUSICBRAINZ_TRACKID` and in the name this app itself wrote.

What this pins:

* (a) the three import paths write the SAME pair for the same file, and the
  spelling the writer produces is the one the grader accepts (read back
  through the app's own tag layer);
* (b) the repair fixes the owner's exact file — offline: no key, no request,
  fpcalc computes the fingerprint locally and the id comes off the file;
* (c) a half pair whose file names no recording anywhere, with a provider
  that answers nothing, is left UNTOUCHED and still fails honestly with the
  grader's actionable message;
* (d) one selection (an album folder + a track of another album) is
  recalculated in a single run, each file once;
* (e) the SUBMISSION contract end to end: one batched `v2/submit` per
  selection carrying the fingerprint + MusicBrainz recording id each file
  states (fpcalc taken locally when the file carries no fingerprint tag), a
  pair the service already links — or that this app already sent — never
  re-sent, a file naming no recording skipped by name, the service's refusal
  verbatim per track, script 22 reaching the same pass with the same payload,
  and nothing read, fingerprinted or sent while `acoustid_enabled` is off or
  `acoustid_user_key` is missing.

Providers are stubbed (no network) and every file is a real FLAC written
through `mlo.audio`, in a temp folder — the developer's library is never
touched.

Run: python tools/test_acoustid_integrity.py
Exit 0 = pass, 1 = failure.
"""
import atexit
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
# Fixtures: a real FLAC, written through the app's own tag layer
# --------------------------------------------------------------------------- #
FLAC_EXE = None
_DEPS = os.path.join(ROOT, ".dependencies")
if os.path.isdir(_DEPS):
    for _entry in os.listdir(_DEPS):
        if _entry.lower().startswith("flac"):
            _cand = os.path.join(_DEPS, _entry, "flac.exe")
            if os.path.isfile(_cand):
                FLAC_EXE = _cand
                break

WORK = tempfile.mkdtemp(prefix="mlo-acoustid-integrity-")
atexit.register(lambda: shutil.rmtree(WORK, ignore_errors=True))
MF = os.path.join(WORK, "music")

# The owner's identities: the recording MBID the file name carries (the app's
# own naming script writes `[%musicbrainz_trackid%]`) and the release group.
REC = "cd2711d6-2687-44b3-9a0a-3b72a5932a38"
RG = "1305859b-8937-397f-9c33-39f62eb672fb"
ALBUMID = "aaaa1111-2222-3333-4444-555566667777"
ARTISTID = "bbbb1111-2222-3333-4444-555566667777"
OTHER_REC = "d6f1b0c2-1111-2222-3333-444455556666"
LIVE_RG = "99998888-7777-6666-5555-444433332222"

from mlo import acoustid  # noqa: E402
from mlo.audio import AudioFile  # noqa: E402
from mlo.grader import _grade_album  # noqa: E402

FAILURES = []


def check(label, cond, detail=""):
    if not cond:
        FAILURES.append(label)
        print(f"FAIL {label}{f' — {detail}' if detail else ''}")


def make_flac(path, seconds=6):
    """A real (silent) FLAC, so every tag write below goes through the real
    container writer — the fixture recipe tools/test_tag_hygiene.py uses."""
    assert FLAC_EXE, "flac.exe not found under .dependencies"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    wav = path + ".wav"
    with wave.open(wav, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00\x00\x00" * 44100 * seconds)
    subprocess.run([FLAC_EXE, "-s", "-f", "-8", "-o", path, wav],
                   check=True, capture_output=True)
    os.remove(wav)
    return path


def tag(path, **values):
    """Write tags the way the app's own writers do (one save)."""
    af = AudioFile(path)
    af.defer_save(True)
    for name, value in values.items():
        af.set_tag(name, value)
    af.defer_save(False)


def pair(path):
    """(ACOUSTID_ID, ACOUSTID_FINGERPRINT) as the FILE states them, read back
    through the app's own reader — never a variable this test set."""
    af = AudioFile(path)
    return (str(af.get_tag("ACOUSTID_ID") or "").strip(),
            str(af.get_tag("ACOUSTID_FINGERPRINT") or "").strip())


def vorbis_names(path):
    """The raw Vorbis comment names on the file, so the SPELLING the writer
    produced is asserted, not just what `get_tag` maps back."""
    return sorted(k for k in (AudioFile(path).all_tags() or {})
                  if "ACOUST" in k.upper())


ALBUM = os.path.join(MF, "Artists", "Rage Against the Machine",
                     "Rage Against the Machine")
OWNER = os.path.join(ALBUM, f"1-08 Fistful of Steel [{REC}] [{RG}].flac")
BASE = {
    "ALBUMARTIST": "Rage Against the Machine",
    "MUSICBRAINZ_ALBUMARTISTID": ARTISTID,
    "ALBUM": "Rage Against the Machine",
    "MUSICBRAINZ_ALBUMID": ALBUMID,
    "MUSICBRAINZ_RELEASEGROUPID": RG,
    "RELEASETYPE": "Album",
    "DATE": "1992",
    "ORIGINALDATE": "1992",
    "RELEASECOUNTRY": "US",
    "MEDIA": "CD",
    "SOURCE": "CD",
    "ARTIST": "Rage Against the Machine",
    "MUSICBRAINZ_ARTISTID": ARTISTID,
    "DISCNUMBER": "1",
}

# The owner's exact file: the fingerprint another tagger left, no id.
make_flac(OWNER)
tag(OWNER, **dict(BASE, TRACKNUMBER="8", TITLE="Fistful of Steel",
                  MUSICBRAINZ_TRACKID=REC,
                  ACOUSTID_FINGERPRINT="AQADtEmSRQmMkSdSpQ"))

# --------------------------------------------------------------------------- #
# Provider stubs: no network, and a fingerprint that is OURS (so a value read
# back off the file proves which half wrote it)
# --------------------------------------------------------------------------- #
LOCAL_FP = "AQABlocal-taken-by-fpcalc"
SERVICE_CALLS = []


class _R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


_real_fpcalc_path = acoustid.fpcalc_path
_real_run_tool = acoustid.run_tool
_real_lookup = acoustid.lookup


def stub_tools(available=True):
    """fpcalc: present (or not), and printing LOCAL_FP."""
    acoustid.fpcalc_path = lambda cfg=None: (r"C:\stub\fpcalc.exe"
                                            if available else None)
    acoustid.run_tool = lambda *a, **k: _R(
        0, json.dumps({"duration": 30.0, "fingerprint": LOCAL_FP}))


def no_service(*a, **k):
    """Any AcoustID request at all: this test says there is none."""
    SERVICE_CALLS.append(1)
    raise AssertionError("the repair must not ask the service for this file")


def stub_lookup(rows_by_name=None, calls=None, code=None, reason=""):
    """The service seam: a per-file candidate, or a plain answer of nothing."""
    def inner(cfg, path):
        if calls is not None:
            calls.append(os.path.basename(path))
        if code is not None:
            return {"ok": False, "code": code, "reason": reason,
                    "rows": [], "fingerprint": "", "duration": 0.0}
        row = (rows_by_name or {}).get(os.path.basename(path))
        if not row:
            return {"ok": False, "code": acoustid.NO_MATCH,
                    "reason": "AcoustID knows no recording for this track",
                    "rows": [], "fingerprint": LOCAL_FP, "duration": 30.0}
        row = dict(row)
        row.setdefault("fingerprint", LOCAL_FP)
        return {"ok": True, "code": acoustid.OK, "reason": "", "rows": [row],
                "fingerprint": row["fingerprint"], "duration": 30.0}
    return inner


def row(recording_id, rg=RG, score=0.95, title="Fistful of Steel"):
    return {"score": score, "recording_id": recording_id, "title": title,
            "artists": ["Rage Against the Machine"], "release_group_id": rg,
            "release_group_title": "Rage Against the Machine",
            "release_group_type": "Album"}


def grade(path):
    """The grader's own verdict for the album: its AcoustID issues, the way
    the Grading page reads them."""
    res = _grade_album(path, "EMBEDDED",
                       {"music_folder": MF, "grade_check_acoustid": True})
    track_issues = [i for t in (res or {}).get("tracks", [])
                    for i in (t.get("issues") or []) if i.startswith("ACOUSTID")]
    sentences = [s for s in ((res or {}).get("issues") or {})
                 if "ACOUSTID" in s.upper()]
    return track_issues, sentences


CFG = {"music_folder": MF, "targets": [ALBUM],
       "acoustid_enabled": True, "acoustid_api_key": ""}

# --------------------------------------------------------------------------- #
# (b) THE OWNER'S CASE: the grader fails it, the repair fixes it offline
# --------------------------------------------------------------------------- #
print("== the owner's file, before ==")
issues, sentences = grade(ALBUM)
check("the grader reports the owner's failure, named the way the owner saw it",
      issues == ["ACOUSTID_ID"]
      and sentences == ["Missing ACOUSTID_ID (run Fix AcoustID pairs)"],
      f"issues={issues} sentences={sentences}")

stub_tools()
acoustid.lookup = no_service            # no key, no network, no request
stats = acoustid.run_fix_pairs(dict(CFG))
check("the repair completes the owner's pair with no service at all",
      stats["modified_count"] == 1 and stats["error_count"] == 0
      and stats["skipped_count"] == 0,
      f"modified={stats['modified_count']} errors={stats['errors']}")
check("…and never asked AcoustID", SERVICE_CALLS == [])
got_id, got_fp = pair(OWNER)
check("the id written is the recording the file itself states",
      got_id == REC, f"got {got_id!r}")
check("the fingerprint written is the one fpcalc took from the audio",
      got_fp == LOCAL_FP, f"got {got_fp!r}")
check("both halves live under the Vorbis names the writer writes",
      vorbis_names(OWNER) == ["ACOUSTID_FINGERPRINT", "ACOUSTID_ID"],
      f"{vorbis_names(OWNER)}")
issues, sentences = grade(ALBUM)
check("…and the grader accepts the file the owner could not fix",
      issues == [] and sentences == [], f"issues={issues} sentences={sentences}")

# The same repair run again: nothing left to do (idempotent by construction).
stats = acoustid.run_fix_pairs(dict(CFG))
check("a second run leaves the completed pair alone",
      stats["unchanged_count"] == 1 and stats["modified_count"] == 0)

# The recording MBID the app's own naming script put in the NAME is evidence
# too: a file whose tag was stripped is still repairable.
NAMED = os.path.join(ALBUM, f"1-09 Wake Up [{OTHER_REC}] [{RG}].flac")
make_flac(NAMED)
tag(NAMED, **dict(BASE, TRACKNUMBER="9", TITLE="Wake Up",
                  ACOUSTID_FINGERPRINT="AQADtEmSRQmMkSdSpQ"))
check("a file with no MUSICBRAINZ_TRACKID is identified from its own name",
      acoustid._recording_identity(NAMED, AudioFile(NAMED))
      == (OTHER_REC, "the file name"))
acoustid.lookup = no_service
stats = acoustid.run_fix_pairs(dict(CFG, targets=[NAMED]))
check("…and the repair pairs it from that name, still with no service",
      stats["modified_count"] == 1
      and pair(NAMED)[0] == OTHER_REC, f"{pair(NAMED)}")

# A file that states its recording as ACOUSTID_ID alone (the other half
# missing) is completed locally as well — the rule the fixer already had.
ID_ONLY = os.path.join(ALBUM, f"1-10 Bombtrack [{OTHER_REC}] [{RG}].flac")
make_flac(ID_ONLY)
tag(ID_ONLY, **dict(BASE, TRACKNUMBER="10", TITLE="Bombtrack",
                    MUSICBRAINZ_TRACKID=OTHER_REC, ACOUSTID_ID=OTHER_REC))
stats = acoustid.run_fix_pairs(dict(CFG, targets=[ID_ONLY]))
check("an id-only half pair gets its fingerprint from the audio, no service",
      stats["modified_count"] == 1 and pair(ID_ONLY) == (OTHER_REC, LOCAL_FP)
      and SERVICE_CALLS == [], f"{pair(ID_ONLY)}")

# --------------------------------------------------------------------------- #
# (c) NOTHING IDENTIFIES IT: untouched, named, and the grader keeps failing
# --------------------------------------------------------------------------- #
print("== a file nothing can identify ==")
UNKNOWN = os.path.join(ALBUM, "1-11 Freedom.flac")
make_flac(UNKNOWN)
tag(UNKNOWN, **dict(BASE, TRACKNUMBER="11", TITLE="Freedom",
                    ACOUSTID_FINGERPRINT="AQADtEmSRQmMkSdSpQ"))
check("no tag and no name carries a recording id",
      acoustid._recording_identity(UNKNOWN, AudioFile(UNKNOWN)) == ("", ""))
stub_tools()
acoustid.lookup = stub_lookup()          # the provider answers NOTHING
stats = acoustid.run_fix_pairs(dict(CFG, targets=[UNKNOWN]))
check("the provider's silence is a NAMED failure, not a silent skip",
      stats["error_count"] == 1 and stats["modified_count"] == 0
      and stats["errors"]
      and "no recording" in stats["errors"][0], f"{stats}")
check("…and the file is left exactly as it was",
      pair(UNKNOWN) == ("", "AQADtEmSRQmMkSdSpQ"), f"{pair(UNKNOWN)}")
issues, sentences = grade(ALBUM)
check("the grader still fails it, with the actionable message",
      any("ACOUSTID" in i for i in issues)
      and "Missing ACOUSTID_ID (run Fix AcoustID pairs)" in sentences,
      f"issues={issues} sentences={sentences}")

# With no provider at all (no key / no fpcalc) a file carrying nothing is not
# this pass's business — reported as such, never written from a guess.
NO_FPCALC = os.path.join(ALBUM, "1-12 Take the Power Back.flac")
make_flac(NO_FPCALC)
tag(NO_FPCALC, **dict(BASE, TRACKNUMBER="12", TITLE="Take the Power Back"))
stub_tools(available=False)
stats = acoustid.run_fix_pairs(dict(CFG, targets=[NO_FPCALC]))
check("a file with nothing to complete from is skipped, never guessed",
      stats["skipped_count"] == 1 and pair(NO_FPCALC) == ("", ""), f"{stats}")

# --------------------------------------------------------------------------- #
# (a) THE THREE IMPORT PATHS write the same pair for the same file
# --------------------------------------------------------------------------- #
print("== the three import paths ==")
from server import imports  # noqa: E402
from server import script_runners  # noqa: E402

# The same input file, three copies: one per path.
COPIES = {}
for name in ("manual", "auto", "bulk"):
    p = os.path.join(WORK, f"same-input-{name}", os.path.basename(OWNER))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    shutil.copyfile(OWNER, p)
    tag(p, ACOUSTID_ID="", ACOUSTID_FINGERPRINT="AQADtEmSRQmMkSdSpQ",
        MUSICBRAINZ_TRACKID=REC)
    COPIES[name] = p
check("the three copies start as the owner's broken state",
      all(pair(p)[0] == "" for p in COPIES.values()))

SUPPLIED = {
    "release_group_id": RG, "release_group_title": "Rage Against the Machine",
    "release_group_type": "Album", "artists": ["Rage Against the Machine"],
    "score": 0.95, "matched": 1, "total": 1,
    "recordings": [{"path": COPIES["manual"], "recording_id": REC,
                    "fingerprint": LOCAL_FP, "title": "Fistful of Steel",
                    "score": 0.95}],
}

# Path 1 — the wizard's own fingerprint step (manual import): the accepted
# match is written straight from the payload, no fpcalc, no lookup.
calls = []
acoustid.lookup = stub_lookup(calls=calls)
acoustid.fpcalc_path = lambda cfg=None: (_ for _ in ()).throw(
    AssertionError("applying a supplied match must not run fpcalc"))
res = imports.acoustid_match([os.path.dirname(COPIES["manual"])],
                             dict(CFG, targets=None), apply=True,
                             match=SUPPLIED)
check("the wizard's apply writes the accepted pair",
      res["albums"][0]["tagged"] == 1
      and pair(COPIES["manual"]) == (REC, LOCAL_FP),
      f"{res['albums'][0].get('writes')} {pair(COPIES['manual'])}")

# Path 2 — the pipeline every import (auto-import, add to library, the bulk
# queue, the manual finish) ends in: `finish_album`, whose chain runs script
# 21 over the album. The chain's own step is the REAL one; the network-heavy
# siblings are not this test's subject.
stub_tools()
acoustid.lookup = no_service
real_run_chain = script_runners.run_chain
chain_ran = []


def only_fix_pairs(cfg, ids, targets=None, force=None, progress=None,
                   wait=True, **kw):
    chain_ran.append(list(ids))
    return real_run_chain(cfg, [21], targets=targets, force=force,
                          progress=progress, wait=wait, **kw)


script_runners.run_chain = only_fix_pairs
imports.run_cover_step = lambda album_dir, cfg=None: {"source": "stub"}
imports.run_metadata_step = lambda path, cfg=None: {"items": []}
imports.stamp_rym_links = lambda path, cfg=None: {"note": "stub"}
try:
    out = imports.finish_album(os.path.dirname(COPIES["auto"]), dict(CFG))
finally:
    script_runners.run_chain = real_run_chain
check("the import pipeline ran its chain over the album",
      chain_ran and 21 in chain_ran[0], f"{chain_ran}")
check("…and the same pair landed on the file the import was handed",
      pair(COPIES["auto"]) == (REC, LOCAL_FP), f"{pair(COPIES['auto'])}")

# Path 3 — the recalculation the entity menu / a selection runs: the chain's
# own script id over an explicit path list.
stub_tools()
res = script_runners.run_chain(dict(CFG, targets=[COPIES["bulk"]]), [21])
check("the selection recalculation writes the same pair",
      res[0]["stats"]["modified_count"] == 1
      and pair(COPIES["bulk"]) == (REC, LOCAL_FP), f"{pair(COPIES['bulk'])}")

check("all three paths wrote the SAME tags for the same input",
      pair(COPIES["manual"]) == pair(COPIES["auto"]) == pair(COPIES["bulk"])
      == (REC, LOCAL_FP),
      {k: pair(v) for k, v in COPIES.items()})
check("…and the grader accepts every one of them",
      all(grade(os.path.dirname(p))[0] == [] for p in COPIES.values()))

# --------------------------------------------------------------------------- #
# (a2) THE SPELLING BEETS WRITES: every reader agrees the pair is there
# --------------------------------------------------------------------------- #
print("== the tagger's own spelling of the pair ==")
# The import chain's tagger IS beets, and mediafile stores the pair as
# `TXXX:Acoustid Id` / `----:com.apple.iTunes:Acoustid Id` — not the
# `ACOUSTID_ID` this app writes. Every reader must find it either way: the
# owner's report was a step saying "carries no AcoustID tag" for a file the
# app's own import had just tagged, while the grader kept demanding the id.
FFMPEG_EXE = None
if os.path.isdir(_DEPS):  # noqa: F821 (module-level _DEPS above)
    for _entry in os.listdir(_DEPS):
        if _entry.lower().startswith("ffmpeg"):
            _cand2 = os.path.join(_DEPS, _entry, "ffmpeg.exe")
            if os.path.isfile(_cand2):
                FFMPEG_EXE = _cand2
                break


def make_mp3(path):
    """A minimal valid MPEG-1 Layer III stream (test_mb_metadata's recipe —
    no bundled mp3 encoder), with an ID3 tag block to write into."""
    import mutagen.mp3

    frame = bytes.fromhex("ff fb 10 00") + b"\x00" * 100
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(frame * 60)
    audio = mutagen.mp3.MP3(path)
    if audio.tags is None:
        audio.add_tags()
    audio.save()
    return path


def beets_spell(path, kind, id_value=REC, fp_value=LOCAL_FP):
    """The pair under beets/mediafile's spellings, written with the ARBITRARY
    key writer — what an outside tagger leaves on disk."""
    af = AudioFile(path)
    if kind == "mp3":
        af.set_any_tag("TXXX:Acoustid Id", id_value)
        af.set_any_tag("TXXX:Acoustid Fingerprint", fp_value)
    else:
        af.set_any_tag("----:com.apple.iTunes:Acoustid Id", id_value)
        af.set_any_tag("----:com.apple.iTunes:Acoustid Fingerprint", fp_value)


stub_tools()
BEETS_DIR = os.path.join(WORK, "tagger")
MP3 = make_mp3(os.path.join(BEETS_DIR, "01 Song.mp3"))
beets_spell(MP3, "mp3")
check("the reader finds the pair beets writes (mp3)",
      pair(MP3) == (REC, LOCAL_FP), f"{pair(MP3)}")
check("…and names both under their semantic tags",
      vorbis_names(MP3) == ["ACOUSTID_FINGERPRINT", "ACOUSTID_ID"],
      f"{vorbis_names(MP3)}")
check("…so script 21 reads the recording the file states",
      acoustid._recording_identity(MP3, AudioFile(MP3)) == (REC, "ACOUSTID_ID"),
      f"{acoustid._recording_identity(MP3, AudioFile(MP3))}")
check("…and a complete pair is left alone, never re-created",
      acoustid.fix_pair(MP3, CFG)["status"] == "unchanged",
      f"{acoustid.fix_pair(MP3, CFG)}")
check("…and the grader reports no missing half",
      grade(BEETS_DIR)[0] == [], f"{grade(BEETS_DIR)}")

if FFMPEG_EXE:
    M4A = os.path.join(BEETS_DIR, "02 Song.m4a")
    subprocess.run([FFMPEG_EXE, "-v", "quiet", "-y", "-f", "lavfi", "-i",
                    "anullsrc=r=44100:cl=stereo", "-t", "1", "-c:a", "aac",
                    M4A], check=True, capture_output=True)
    beets_spell(M4A, "mp4")
    check("…and the same holds for the MP4 atom spelling",
          pair(M4A) == (REC, LOCAL_FP)
          and acoustid.fix_pair(M4A, CFG)["status"] == "unchanged",
          f"{pair(M4A)} {acoustid.fix_pair(M4A, CFG)}")
else:
    print("  (ffmpeg absent: the MP4 atom spelling is not exercised)")

# This app's own write REPLACES the other spelling — one id on disk, not two.
acoustid.write_tags(MP3, REC, LOCAL_FP, CFG)
_leaked = [k for k in (AudioFile(MP3).all_tags() or {})
           if "ACOUSTID" in str(k).upper()]
check("this app's own write leaves exactly one spelling of each half",
      vorbis_names(MP3) == ["ACOUSTID_FINGERPRINT", "ACOUSTID_ID"],
      f"{vorbis_names(MP3)} / {_leaked}")

# Half a pair in the tagger's spelling: every consumer names the missing half.
HALF = make_mp3(os.path.join(WORK, "tagger-half", "01 Song.mp3"))
beets_spell(HALF, "mp3", fp_value="")
check("a lone beets 'Acoustid Id' reads as an id with no fingerprint",
      pair(HALF) == (REC, ""), f"{pair(HALF)}")
check("…and the grader fails exactly the missing half",
      grade(os.path.dirname(HALF))[0] == ["ACOUSTID_FINGERPRINT"],
      f"{grade(os.path.dirname(HALF))}")
check("…and script 21 completes it from the id the file stated",
      acoustid.fix_pair(HALF, CFG)["status"] == "modified"
      and pair(HALF) == (REC, LOCAL_FP),
      f"{acoustid.fix_pair(HALF, CFG)} {pair(HALF)}")

# --------------------------------------------------------------------------- #
# (d) A SELECTION: several paths in one run, each file exactly once
# --------------------------------------------------------------------------- #
print("== a selection ==")
LIVE = os.path.join(MF, "Artists", "Rage Against the Machine",
                    "Live at the Grand Olympic")
LIVE_RECS = ["11112222-3333-4444-5555-666677778888",
             "22223333-4444-5555-6666-777788889999"]
LIVE_FILES = []
for n, rec in enumerate(LIVE_RECS, 1):
    p = os.path.join(LIVE, f"1-0{n} Live {n} [{rec}] [{LIVE_RG}].flac")
    make_flac(p)
    tag(p, **dict(BASE, ALBUM="Live at the Grand Olympic", TRACKNUMBER=str(n),
                  TITLE=f"Live {n}", MUSICBRAINZ_TRACKID=rec,
                  MUSICBRAINZ_RELEASEGROUPID=LIVE_RG))
    LIVE_FILES.append(p)
stub_tools()
acoustid.lookup = no_service
# An album folder AND a track of ANOTHER album, in one request — the shape a
# mixed selection sends.
res = script_runners.run_chain(
    dict(CFG, targets=[LIVE, OWNER]), [21])
stats = res[0]["stats"]
check("one run covers the whole selection, each file exactly once",
      stats["total_scanned"] == 3 and stats["error_count"] == 0
      and stats["modified_count"] + stats["unchanged_count"] == 3,
      f"{stats}")
check("every file of the selection carries its own pair",
      pair(LIVE_FILES[0])[0] == LIVE_RECS[0]
      and pair(LIVE_FILES[1])[0] == LIVE_RECS[1]
      and pair(OWNER) == (REC, LOCAL_FP),
      f"{[pair(p) for p in LIVE_FILES]}")
check("a folder target and a file inside it are one file, not two",
      stats["total_scanned"] == len({p for p in LIVE_FILES} | {OWNER}))

# --------------------------------------------------------------------------- #
# (e) SUBMISSION: one batched v2/submit, the right payload, nothing re-sent
# --------------------------------------------------------------------------- #
# `_post` is the ONE place a request leaves this module, so stubbing it covers
# both halves of the contract at once — the dedupe question (a lookup) and the
# submission itself — and every form the app builds is captured verbatim.
print("== submission ==")
from mlo.config import DEFAULT_CONFIG  # noqa: E402  (a whole config)

POSTED = []
FP_CALLS = []
# What the "database" already links: fingerprint -> recording ids.
KNOWN = {}
_real_post = acoustid._post


def stub_post(url, form, what):
    POSTED.append({"url": url, "what": what, "form": dict(form)})
    if what == "lookup":
        recs = KNOWN.get(str(form.get("fingerprint") or ""), ())
        return {"ok": True, "code": acoustid.OK, "reason": "",
                "payload": {"status": "ok", "results": [
                    {"id": "track-1", "score": 0.09,
                     "recordings": [{"id": r, "title": "T", "artists": []}]}
                    for r in recs]}}
    if str(form.get("user") or "") == "bad-user-key":
        # The service's own refusal, in its own words (the error body the
        # module turns into a sentence).
        return {"ok": False, "code": acoustid.LOOKUP_FAILED,
                "reason": ("AcoustID submission failed: HTTP 400 - "
                           "invalid user API key (code 8)"),
                "payload": None}
    n = len([k for k in form if k.startswith("fingerprint.")])
    return {"ok": True, "code": acoustid.OK, "reason": "",
            "payload": {"status": "ok", "submissions": [
                {"index": i, "id": 5000 + i, "status": "pending"}
                for i in range(n)]}}


acoustid._post = stub_post
SUB_CFG = dict(DEFAULT_CONFIG, music_folder=MF, targets=None,
               acoustid_enabled=True, acoustid_api_key="app-key",
               acoustid_user_key="user-key")


def counting_tools():
    """fpcalc present, printing a fingerprint that is OURS, and counting its
    own runs — so "nothing was read or fingerprinted" is provable."""
    acoustid.fpcalc_path = lambda cfg=None: r"C:\stub\fpcalc.exe"
    acoustid.run_tool = lambda *a, **k: (
        FP_CALLS.append(1),
        _R(0, json.dumps({"duration": 30.0, "fingerprint": LOCAL_FP})))[1]


def mk(directory, name, **tags):
    p = os.path.join(MF, "Artists", "Submission Fixtures", directory, name)
    make_flac(p)
    tag(p, **dict(BASE, **tags))
    return p


def submits():
    return [p for p in POSTED if p["what"] == "submission"]


def lookups():
    return [p for p in POSTED if p["what"] == "lookup"]


REC_A, REC_B, REC_C, REC_D = (
    "aaaaaaa1-1111-2222-3333-444455556666",
    "bbbbbbb2-1111-2222-3333-444455556666",
    "ccccccc3-1111-2222-3333-444455556666",
    "ddddddd4-1111-2222-3333-444455556666")

# The album: one file carrying the pair (its fingerprint off the tag), one
# carrying NO fingerprint at all (taken from the AUDIO instead — the CD rip
# AcoustID has never heard of), and one naming no recording anywhere.
SUB = "Album of Three"
A = mk(SUB, f"1-01 One [{REC_A}].flac", ALBUM="Album of Three", TRACKNUMBER="1",
       TITLE="One", MUSICBRAINZ_TRACKID=REC_A,
       ACOUSTID_FINGERPRINT="AQADsubmittedpair")
B = mk(SUB, f"1-02 Two [{REC_B}].flac", ALBUM="Album of Three", TRACKNUMBER="2",
       TITLE="Two", MUSICBRAINZ_TRACKID=REC_B)
NOID = mk(SUB, "1-03 Untagged.flac", ALBUM="Album of Three", TRACKNUMBER="3",
          TITLE="Untagged")
check("the untagged fixture names no recording anywhere",
      acoustid._recording_identity(NOID, AudioFile(NOID)) == ("", ""))

# --- one TRACK: the route's own path resolution, and the payload ---------- #
counting_tools()
POSTED.clear()
FP_CALLS.clear()
res = imports.acoustid_submit([A], SUB_CFG)
check("a track path is THAT track, never the album it sits in",
      res["tracks"]["total"] == 1)
check("one track: a dedupe question, then ONE submission",
      res["available"] is True and res["submitted"] == 1
      and len(submits()) == 1 and len(lookups()) == 1
      and submits()[0]["url"] == acoustid.SUBMIT_API_URL)
form = submits()[0]["form"]
check("…carrying the file's own fingerprint, its recording id and its tags",
      form["fingerprint.0"] == "AQADsubmittedpair"
      and form["mbid.0"] == REC_A and form["source.0"] == 1
      and form["client"] == "app-key" and form["user"] == "user-key"
      and form["track.0"] == "One" and "meta" not in form)
check("…and the per-track report is the service's own answer",
      [(r["path"], r["outcome"], r["id"], r["status"]) for r in res["results"]]
      == [(A, acoustid.ACCEPTED, 5000, "pending")]
      and res["tracks"] == {"total": 1, "submitted": 1, "known": 0,
                            "skipped": 0, "failed": 0})

# --- the same press again: idempotent while the service is still importing -- #
POSTED.clear()
res = imports.acoustid_submit([A], SUB_CFG)
check("a second press re-sends nothing, and says why",
      submits() == [] and res["submitted"] == 0 and res["known"] == 1
      and res["results"][0]["outcome"] == acoustid.ALREADY_KNOWN
      and "already submitted" in res["results"][0]["reason"],
      res["results"])

# --- a whole ALBUM: one batch, a local fingerprint, a named skip ---------- #
POSTED.clear()
FP_CALLS.clear()
res = imports.acoustid_submit([os.path.dirname(A)], SUB_CFG)
check("an album goes out as ONE batch of its submittable tracks",
      len(submits()) == 1 and res["tracks"]["total"] == 3
      and res["submitted"] == 1 and res["known"] == 1
      and res["tracks"]["skipped"] == 1
      and len(lookups()) == 1, res["tracks"])
check("the file with no fingerprint tag is fingerprinted from its OWN audio",
      FP_CALLS == [1] and submits()[0]["form"]["fingerprint.0"] == LOCAL_FP
      and submits()[0]["form"]["mbid.0"] == REC_B,
      submits()[0]["form"])
check("the file that names no recording is a named skip, not a request",
      [r["path"] for r in res["results"]
       if r["code"] == acoustid.NO_RECORDING_ID] == [NOID]
      and "MusicBrainz recording" in res["results"][2]["reason"]
      and all(NOID not in p["form"].values() for p in submits()))

# --- a SELECTION with the same pair twice: one entry, not two ------------- #
DUP = "Duplicates"
D1 = mk(DUP, f"2-01 One [{REC_C}].flac", TRACKNUMBER="1", TITLE="One",
        MUSICBRAINZ_TRACKID=REC_C, ACOUSTID_FINGERPRINT="AQADduppair")
D2 = mk(DUP, f"2-01 One (again) [{REC_C}].flac", TRACKNUMBER="1", TITLE="One",
        MUSICBRAINZ_TRACKID=REC_C, ACOUSTID_FINGERPRINT="AQADduppair")
POSTED.clear()
res = imports.acoustid_submit([os.path.dirname(D1)], SUB_CFG)
check("the same fingerprint + recording in one selection is sent ONCE",
      res["tracks"]["total"] == 2 and res["submitted"] == 1
      and len(submits()) == 1
      and len([k for k in submits()[0]["form"] if k.startswith("fingerprint.")]) == 1
      and res["results"][1]["outcome"] == acoustid.ALREADY_KNOWN
      and "twice in this selection" in res["results"][1]["reason"],
      res["results"])

# --- what the SERVICE already links: asked, then not re-sent ------------- #
SRV = "Known Already"
K1 = mk(SRV, f"3-01 Known [{REC_D}].flac", TRACKNUMBER="1", TITLE="Known",
        MUSICBRAINZ_TRACKID=REC_D, ACOUSTID_FINGERPRINT="AQADknownpair")
KNOWN["AQADknownpair"] = (REC_D,)
POSTED.clear()
res = imports.acoustid_submit([K1], SUB_CFG)
check("a pair AcoustID already links is asked about, and never re-sent",
      len(lookups()) == 1 and submits() == [] and res["submitted"] == 0
      and res["known"] == 1
      and res["results"][0]["outcome"] == acoustid.ALREADY_KNOWN
      and REC_D in res["results"][0]["reason"], res["results"])

# --- WHAT THE SERVICE REFUSES: its own sentence, per track --------------- #
BAD = mk("Refused", f"4-01 Refused [{REC_A}].flac", TRACKNUMBER="1",
         TITLE="Refused", MUSICBRAINZ_TRACKID=REC_A,
         ACOUSTID_FINGERPRINT="AQADrefusedpair")
POSTED.clear()
res = imports.acoustid_submit([BAD], dict(SUB_CFG, acoustid_user_key="bad-user-key"))
check("a refused user key comes back in the service's own words",
      res["available"] is True and res["ok"] is False
      and "invalid user API key (code 8)" in res["note"]
      and res["failed"] == 1 and res["submitted"] == 0
      and res["results"][0]["outcome"] == acoustid.REJECTED
      and res["results"][0]["reason"] == res["note"], res["note"])

# --- THE RUN PATH: script 22 reaches the same pass with the same payload -- #
RUN = "Script Target"
S1 = mk(RUN, f"5-01 Scripted [{REC_B}].flac", TRACKNUMBER="1",
        TITLE="Scripted", MUSICBRAINZ_TRACKID=REC_B,
        ACOUSTID_FINGERPRINT="AQADscriptpair")
POSTED.clear()
ran = script_runners.run_chain(dict(SUB_CFG, targets=[os.path.dirname(S1)]), [22])
check("script 22 runs over its targets and submits through the same pass",
      ran[0].get("error") is None and ran[0]["stats"]["submitted"] == 1
      and len(submits()) == 1
      and submits()[0]["form"]["mbid.0"] == REC_B
      and submits()[0]["form"]["fingerprint.0"] == "AQADscriptpair",
      f"{ran[0].get('error')} {ran[0].get('stats')}")
check("…and the run's own counts name what happened",
      (ran[0]["stats"]["total_scanned"], ran[0]["stats"]["already_known"],
       ran[0]["stats"]["error_count"]) == (1, 0, 0), ran[0]["stats"])

# A config that cannot submit at all is a NAMED error from the runner, never a
# silent skip: nothing is read, fingerprinted or sent.
POSTED.clear()
FP_CALLS.clear()
counting_tools()
ran = script_runners.run_script(22, dict(SUB_CFG, acoustid_user_key=""),
                                targets=[os.path.dirname(S1)])
check("a missing user key is the runner's own named error",
      "no user API key" in str(ran.get("error"))
      and "nothing was submitted" in str(ran.get("error")), ran.get("error"))
check("…with nothing read, fingerprinted or sent",
      POSTED == [] and FP_CALLS == [])

# --- the feature switch off means the whole feature is off --------------- #
POSTED.clear()
FP_CALLS.clear()
counting_tools()
res = imports.acoustid_submit([A], dict(SUB_CFG, acoustid_enabled=False))
check("acoustid_enabled off is the named refusal, with no request at all",
      res["available"] is False and res["code"] == acoustid.DISABLED
      and res["note"] == "AcoustID disabled in settings"
      and POSTED == [] and FP_CALLS == [])
# …and a missing USER key is the other half of the same gate.
POSTED.clear()
FP_CALLS.clear()
res = imports.acoustid_submit([A], dict(SUB_CFG, acoustid_user_key=""))
check("no user key is a named refusal, and nothing is read or sent",
      res["available"] is False and res["code"] == acoustid.NO_USER_KEY
      and res["note"] == "no user API key"
      and POSTED == [] and FP_CALLS == [])

# --------------------------------------------------------------------------- #
# The fingerprint memo: fpcalc DECODES the file (up to 120 s of it), and the
# same unchanged track is asked about again and again inside one session — the
# pair fixer, the submitter, the import wizard's own step and a lookup all run
# over the same files minutes apart. Keyed on the file's stat (and the build),
# so a re-download is never served a stale fingerprint.
# --------------------------------------------------------------------------- #
print("== fpcalc runs once per unchanged file ==")

MEMO = mk("Memo Fixtures", "1-01 Memo Track.flac",
          MUSICBRAINZ_TRACKID=REC, TITLE="Memo Track")
BUILD_A = r"C:\stub\fpcalc.exe"
BUILD_B = r"C:\stub\fpcalc-1.6.1.exe"


def _memo_fpcalc(*a, **k):
    """ONE launcher object for the whole case: the memo is keyed on the
    launcher as well as on the file, so swapping the double (a lambda per call)
    would be a different question every time."""
    FP_CALLS.append(1)
    return _R(0, json.dumps({"duration": 30.0, "fingerprint": LOCAL_FP}))


_real_path = acoustid.fpcalc_path
_real_tool = acoustid.run_tool
acoustid.run_tool = _memo_fpcalc
acoustid.fpcalc_path = lambda cfg=None: BUILD_A
FP_CALLS.clear()
try:
    first = acoustid.fingerprint(MEMO, CFG)
    second = acoustid.fingerprint(MEMO, CFG)
    check("one unchanged file is decoded by fpcalc ONCE, however often it is asked",
          len(FP_CALLS) == 1 and first == second
          and first["fingerprint"] == LOCAL_FP,
          f"{FP_CALLS} {first} {second}")

    # The memo is about the FILE, not about the question: a rewritten file (a
    # re-tag, a re-download over the same name) is fingerprinted again.
    tag(MEMO, TITLE="Memo Track (re-tagged, and a longer title)")
    third = acoustid.fingerprint(MEMO, CFG)
    check("...a rewritten file is taken again (the memo is keyed on its stat)",
          len(FP_CALLS) == 2 and third["fingerprint"] == LOCAL_FP, str(FP_CALLS))

    # And it is about THIS build: another fpcalc is a different question and
    # must not reuse the values this one produced.
    acoustid.fpcalc_path = lambda cfg=None: BUILD_B
    acoustid.fingerprint(MEMO, CFG)
    check("...and another fpcalc build is not served the old build's value",
          len(FP_CALLS) == 3, str(FP_CALLS))

    # A missing tool is NOT a fact about the file, so it is never remembered:
    # installing fpcalc has to fix the very next call.
    acoustid.fpcalc_path = lambda cfg=None: None
    gone = acoustid.fingerprint(MEMO, CFG)
    check("...a missing fpcalc is never remembered (installing it fixes the call)",
          gone["code"] == acoustid.NO_FPCALC and len(FP_CALLS) == 3,
          f"{gone} {FP_CALLS}")

    # Back to the first build and the same bytes: the memo answers, and the
    # message the missing tool produced was not it.
    acoustid.fpcalc_path = lambda cfg=None: BUILD_A
    FP_CALLS.clear()
    back = acoustid.fingerprint(MEMO, CFG)
    check("...and the unchanged file is answered from the memo once it is back",
          FP_CALLS == [] and back["ok"] is True
          and back["fingerprint"] == LOCAL_FP,
          f"{FP_CALLS} {back}")
finally:
    acoustid.fpcalc_path = _real_path
    acoustid.run_tool = _real_tool

# --------------------------------------------------------------------------- #
# (a4) AN ALBUM THAT IS A CD IS MATCHED TO A RELEASE GROUP THAT HAS ONE
# --------------------------------------------------------------------------- #
# The fingerprint names the AUDIO, never the pressing: a group whose editions
# are all vinyl or digital is not the thing the user is importing. What the
# album IS — the wizard's own "what is this?" answer (`medium`) or the MEDIA tag
# its files carry — makes it a rule, asked through the cached browse the release
# page already uses, and only of the groups the vote itself would take.
from server import integrations  # noqa: E402

CD_ALBUM = os.path.join(WORK, "cd-album")
RG_CDLESS = "cccccccc-0000-0000-0000-00000000000c"
for _name in ("01 - One.flac", "02 - Two.flac", "03 - Three.flac", "04 - Four.flac"):
    make_flac(os.path.join(CD_ALBUM, _name), seconds=6)

stub_tools()
# The audio alone decides for RG_CDLESS (its 2 tracks score best); RG is the
# runner-up on the same track count, so only the CD rule can move it.
acoustid.lookup = stub_lookup({
    "01 - One.flac": row("rec-1", rg=RG_CDLESS, score=0.95),
    "02 - Two.flac": row("rec-2", rg=RG_CDLESS, score=0.94),
    "03 - Three.flac": row("rec-3", rg=RG, score=0.80),
    "04 - Four.flac": row("rec-4", rg=RG, score=0.79),
})
CD_CFG = dict(CFG, targets=None, acoustid_api_key="stub-key")
BROWSED = []
_real_browse = integrations.release_group_browse


def _browse(gid, *a, **k):
    BROWSED.append(gid)
    formats = ["CD"] if gid == RG else ["Vinyl"]
    return {"releases": [{"formats": formats, "medium": formats[0],
                          "disc_count": 1, "track_count": 3}]}


integrations.release_group_browse = _browse
try:
    cd_row = imports.acoustid_match([CD_ALBUM], CD_CFG, medium="CD")["albums"][0]
    check("a CD album takes the group that HAS a CD edition",
          cd_row["release_group_id"] == RG and cd_row["status"] == "matched",
          str(cd_row))
    check("...asked of the candidates in rank order, and only those",
          BROWSED == [RG_CDLESS, RG], str(BROWSED))
    check("...and the refusal is in the row's own sentence",
          "1 other release group(s) matched the audio without a CD edition"
          in str(cd_row.get("reason")), str(cd_row.get("reason")))

    BROWSED.clear()
    plain = imports.acoustid_match([CD_ALBUM], CD_CFG)["albums"][0]
    check("no medium stated -> the audio decides, exactly as before",
          plain["release_group_id"] == RG_CDLESS and BROWSED == [],
          f"{plain} {BROWSED}")

    # The album's OWN tag answers the same way when the caller says nothing.
    for _name in ("01 - One.flac", "02 - Two.flac", "03 - Three.flac", "04 - Four.flac"):
        tag(os.path.join(CD_ALBUM, _name), MEDIA="CD")
    tagged = imports.acoustid_match([CD_ALBUM], CD_CFG)["albums"][0]
    check("the album's own MEDIA tag makes the same rule",
          tagged["release_group_id"] == RG, str(tagged))

    # A group that matched the audio but has no CD edition anywhere: said in
    # its own words, never dressed up as "AcoustID knows nothing".
    integrations.release_group_browse = lambda gid, *a, **k: {
        "releases": [{"formats": ["Vinyl"], "medium": "Vinyl"}]}
    none_row = imports.acoustid_match([CD_ALBUM], CD_CFG,
                                      medium="CD")["albums"][0]
    check("no CD edition among the matches -> no_match naming the requirement",
          none_row["status"] == "no_match"
          and "none has a CD edition" in str(none_row.get("reason")),
          str(none_row))
finally:
    integrations.release_group_browse = _real_browse

# --------------------------------------------------------------------------- #
# restore + verdict
# --------------------------------------------------------------------------- #
acoustid._post = _real_post
acoustid.fpcalc_path = _real_fpcalc_path
acoustid.run_tool = _real_run_tool
acoustid.lookup = _real_lookup

print()
if FAILURES:
    print(f"{len(FAILURES)} failure(s)")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("OK test_acoustid_integrity")
