#!/usr/bin/env python3
"""The FLAC STREAMINFO MD5 — used, trusted and verified by every verb that
touches a FLAC's audio (grade / audit / tag / optimize).

A FLAC's STREAMINFO carries the MD5 of its DECODED audio. It is the one
identity a tag write cannot move (mlo.discs' CRC memo, mlo.accurip's evidence
and mlo.audit's verdict evidence are all filed under it), and it is a CLAIM
about the audio: only a decode says whether it is true, and a file that states
none (the all-zero field) has nothing to compare at all — "unknown", which is
not "fine".

Three REAL fixtures are built here with the vendored encoder (a 2-second tone
encoded by flac.exe), and each is pinned against every verb:

  * honest.flac     — states the MD5 of its own audio.
  * tampered.flac   — the same bytes with the STREAMINFO MD5 replaced by
                      0xdeadbeef… (same audio, a header that lies about it).
  * nomd5.flac      — the same bytes with the STREAMINFO MD5 zeroed (the
                      documented "no digest" state).

How each fixture is made (reproducible by hand):

    ffmpeg/`wave` writes a 2 s 440 Hz stereo 16-bit tone;
    `flac -s -f -8 -o honest.flac tone.wav` encodes it;
    `python -c "d=bytearray(open('honest.flac','rb').read()); d[26:42]=b'\\xde\\xad\\xbe\\xef'*4;
                open('tampered.flac','wb').write(bytes(d))"`
      — the MD5 is the LAST 16 bytes of the 34-byte STREAMINFO block, which
      starts 8 bytes into the file ("fLaC" + the 4-byte block header), so the
      field is bytes 26..42;
    the same script with b"\\x00"*16 writes the no-MD5 fixture.

Everything runs offline in a temp folder; the AudioAuditor spectral pass is
deliberately absent (the audit supports that — it is optional Windows-only
evidence) so the integrity/MD5 answers this suite pins are deterministic.

Run:  python tools/test_flac_md5.py   (exit 0 pass, 1 fail, 2 skip)
"""
import hashlib
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import mlo.audit as mlo_audit  # noqa: E402
import mlo.flac as mlo_flac  # noqa: E402
import mlo.grader as mlo_grader  # noqa: E402
from mlo.audio import AudioFile  # noqa: E402
from mlo.config import DEFAULT_CONFIG, normalize_config  # noqa: E402
from mlo.flac import (  # noqa: E402
    MD5_ABSENT, MD5_MISMATCH, MD5_OK, decoded_md5, stream_md5_state,
)
from mlo.tools import detect_all_tools  # noqa: E402

FAILED = []
SKIPPED = []
PASSED = [0]


def check(label, cond, detail=""):
    if cond:
        PASSED[0] += 1
        print(f"  ok   {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ""))


def flat(values):
    """The grader's `issues` values are lists of where-names (and a check may
    report a list of them), so join whatever is there as words."""
    out = []
    for v in values or ():
        out.extend(v if isinstance(v, (list, tuple)) else [v])
    return ", ".join(str(x) for x in out)


def skip(label):
    SKIPPED.append(label)
    print(f"  skip {label}")


TOOLS = detect_all_tools()
FLAC_EXE = (TOOLS.get("flac") or {}).get("flac_exe")
METAFLAC_EXE = (TOOLS.get("flac") or {}).get("metaflac_exe")
FFMPEG_EXE = (TOOLS.get("ffmpeg") or {}).get("ffmpeg_exe")

TAMPER_BYTES = b"\xde\xad\xbe\xef" * 4
TAMPER_HEX = TAMPER_BYTES.hex()


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def write_tone(path, seconds=2.0, freq=440.0, rate=44100, channels=2,
               amp=12000):
    """A real WAV: *seconds* of a sine at *freq*, 16-bit stereo."""
    frames = bytearray()
    for i in range(int(rate * seconds)):
        sample = struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / rate)))
        frames += sample * channels
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


def encode_flac(wav, out, extra=()):
    """The vendored reference encoder — a REAL FLAC, not a header stub."""
    subprocess.run([FLAC_EXE, "-s", "-f", "-8", *extra, "-o", out, wav],
                   check=True, capture_output=True)
    return out


def retamper(src, dst, md5_bytes):
    """*src* -> *dst* with the STREAMINFO MD5 written to *md5_bytes*.

    The MD5 is STREAMINFO's last 16 bytes; STREAMINFO is the first block,
    whose 34 bytes start at offset 8 ("fLaC" + a 1-byte block header + its
    3-byte length) — so the field is 8 + 18 = 26 through 42.
    """
    with open(src, "rb") as fh:
        data = bytearray(fh.read())
    assert data[:4] == b"fLaC", "not a FLAC"
    assert data[4] & 0x7F == 0, "the first block is not STREAMINFO"
    data[26:42] = md5_bytes
    with open(dst, "wb") as fh:
        fh.write(bytes(data))
    return dst


def file_sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def tag_count(path):
    return len(AudioFile(path).all_tags() or {})


def build_fixtures(root):
    """(fixtures dict, source wav) — three real FLACs plus the tone they came
    from, all under *root*."""
    fx = os.path.join(root, "fixtures")
    os.makedirs(fx, exist_ok=True)
    wav = write_tone(os.path.join(fx, "tone.wav"))
    honest = encode_flac(wav, os.path.join(fx, "honest.flac"))
    tampered = retamper(honest, os.path.join(fx, "tampered.flac"), TAMPER_BYTES)
    nomd5 = retamper(honest, os.path.join(fx, "nomd5.flac"), b"\x00" * 16)
    return {"honest": honest, "tampered": tampered, "nomd5": nomd5,
            "wav": wav, "dir": fx}


def stated(path):
    r = subprocess.run([METAFLAC_EXE, "--show-md5sum", path],
                       capture_output=True, text=True)
    return r.stdout.strip().lower()


def run_metaflac(*args):
    return subprocess.run([METAFLAC_EXE, *args], capture_output=True, text=True)


def unjudging_flac(root, real):
    """A path to a flac that encodes WITHOUT judging the input's header.

    Which is what CI's build did: it encoded a file whose STREAMINFO MD5 was
    not the digest of its own audio and exited 0, where this machine's flac
    says "MD5sum of input is different from MD5sum of output" and fails. That
    difference is what let a tampered file be rewritten in CI while every
    suite was green here, so the guard is exercised against both: this shim
    forwards every argument but `-V` to the real tool, hides its output and
    exits 0.

    A `.cmd` on Windows, a shebang script elsewhere — the same two shapes the
    app itself has to run on. None when there is no flac to wrap.
    """
    if not real or not os.path.isfile(real):
        return None
    body = ("import subprocess, sys\n"
            f"REAL = {real!r}\n"
            "args = [a for a in sys.argv[1:] if a != '-V']\n"
            "subprocess.run([REAL] + args, stdout=subprocess.DEVNULL,\n"
            "               stderr=subprocess.DEVNULL)\n"
            "sys.exit(0)\n")
    if os.name == "nt":
        script = os.path.join(root, "flac-unjudging.py")
        with open(script, "w", newline="\n") as fh:
            fh.write(body)
        path = os.path.join(root, "flac-unjudging.cmd")
        with open(path, "w", newline="\r\n") as fh:
            fh.write("@echo off\r\n"
                     f"\"{sys.executable}\" \"{script}\" %*\r\n"
                     "exit /b %ERRORLEVEL%\r\n")
        return path
    path = os.path.join(root, "flac-unjudging")
    with open(path, "w", newline="\n") as fh:
        fh.write("#!" + sys.executable + "\n" + body)
    os.chmod(path, 0o755)
    return path


def reference_test(path):
    """`flac -t` — the reference check, verbatim: (rc, the tool's whole
    output). The whole output, because the tool says two things at once for a
    stream with no MD5 ("cannot check MD5 signature …" then "ok"), and a caller
    that reads only the last line reads that as a pass."""
    r = subprocess.run([FLAC_EXE, "-t", path], capture_output=True, text=True)
    return r.returncode, f"{r.stdout or ''}\n{r.stderr or ''}"


# --------------------------------------------------------------------------- #
# 1. The fixtures, as the reference decoder sees them
# --------------------------------------------------------------------------- #
def check_fixtures(root):
    print("\n== the three fixtures (built by the vendored encoder) ==")
    fx = build_fixtures(root)
    check("flac.exe is present (a real encoder to build with)", bool(FLAC_EXE),
          "no flac.exe under .dependencies")
    if not FLAC_EXE:
        return fx

    digests = {name: stated(fx[name]) for name in ("honest", "tampered", "nomd5")}
    check("honest.flac states the digest of its own audio",
          digests["honest"] == subprocess.run(
              [METAFLAC_EXE, "--show-md5sum", fx["honest"]],
              capture_output=True, text=True).stdout.strip().lower()
          and len(digests["honest"]) == 32, digests["honest"])
    check("tampered.flac's header really holds the bytes this suite wrote "
          "(bytes 26..42 of the file)", digests["tampered"] == TAMPER_HEX,
          f"header says {digests['tampered']}, suite wrote {TAMPER_HEX}")
    check("nomd5.flac's header really states NO MD5",
          digests["nomd5"] == "0" * 32, digests["nomd5"])
    check("the tampering changed the file and not its size",
          os.path.getsize(fx["tampered"]) == os.path.getsize(fx["honest"]),
          f"{os.path.getsize(fx['tampered'])} vs {os.path.getsize(fx['honest'])}")

    rc, out = reference_test(fx["honest"])
    check("flac -t: the honest fixture passes", rc == 0 and "ok" in out,
          f"rc={rc} {out[-200:]!r}")
    rc, out = reference_test(fx["tampered"])
    check("flac -t: the tampered fixture FAILS with an MD5 mismatch",
          rc != 0 and "MD5 signature mismatch" in out, f"rc={rc} {out[-200:]!r}")
    rc, out = reference_test(fx["nomd5"])
    check("flac -t: the no-MD5 fixture is ACCEPTED (rc=0) and says it cannot "
          "check the MD5 — which is why rc=0 must not be read as 'verified'",
          rc == 0 and "cannot check MD5 signature" in out, f"rc={rc} {out[-200:]!r}")
    return fx


# --------------------------------------------------------------------------- #
# 2. The app's own reading of the three states
# --------------------------------------------------------------------------- #
def check_states(fx):
    print("\n== mlo.flac.stream_md5_state / mlo.audit.verify_integrity ==")
    if not (FLAC_EXE and FFMPEG_EXE):
        skip("no flac/ffmpeg: the states need a decoder")
        return
    states = {name: stream_md5_state(fx[name], FLAC_EXE, FFMPEG_EXE)
              for name in ("honest", "tampered", "nomd5")}
    check("honest -> ok, and the digest it states is the one it holds",
          states["honest"][0] == MD5_OK
          and states["honest"][2] == stated(fx["honest"]),
          str(states["honest"]))
    check("tampered -> md5-mismatch (named, not a generic failure)",
          states["tampered"][0] == MD5_MISMATCH, str(states["tampered"]))
    check("nomd5 -> md5-absent (unknown), never 'ok'",
          states["nomd5"][0] == MD5_ABSENT, str(states["nomd5"]))
    check("a non-FLAC is not this question (no false 'absent')",
          stream_md5_state(fx["wav"], FLAC_EXE, FFMPEG_EXE)[0] == "",
          str(stream_md5_state(fx["wav"], FLAC_EXE, FFMPEG_EXE)))

    ok, err = mlo_audit.verify_integrity(fx["honest"], FFMPEG_EXE, FLAC_EXE)
    check("verify_integrity: honest passes", ok and err is None, f"{ok} {err}")
    ok, err = mlo_audit.verify_integrity(fx["tampered"], FFMPEG_EXE, FLAC_EXE)
    check("verify_integrity: tampered FAILS and names the FLAC MD5",
          (not ok) and "FLAC MD5 mismatch" in str(err), f"{ok} {err!r}")
    ok, err = mlo_audit.verify_integrity(fx["nomd5"], FFMPEG_EXE, FLAC_EXE)
    check("verify_integrity: no MD5 is not a failure (it is 'unknown')",
          ok and err is None, f"{ok} {err!r}")

    # The ffmpeg fallback (no flac.exe) must reach the same three answers
    # rather than trusting the header: it decodes and hashes the samples.
    st = stream_md5_state(fx["tampered"], None, FFMPEG_EXE)
    check("without flac.exe the same verdict comes from decoding with ffmpeg "
          "(the header is never taken on its word)",
          st[0] == MD5_MISMATCH, str(st))

    # …and the digest of a decoded file IS the digest it states (what makes
    # the whole comparison meaningful): 16-bit, and the 24-bit representation
    # is FLAC's own (three bytes a sample, not padded to four).
    digest, err = decoded_md5(fx["honest"], FFMPEG_EXE, 16)
    check("ffmpeg's decoded digest == the stated STREAMINFO MD5",
          digest == stated(fx["honest"]), f"{digest} vs {stated(fx['honest'])} {err}")


# --------------------------------------------------------------------------- #
# 3. Tagging: the audio (and so the digest) is untouched by every writer
# --------------------------------------------------------------------------- #
def check_tag_writes(fx, root):
    print("\n== writing tags leaves the audio byte-for-byte the same ==")
    if not FLAC_EXE:
        skip("no flac.exe: cannot measure the digest")
        return
    work = os.path.join(root, "tags")
    os.makedirs(work, exist_ok=True)

    def proof(label, path, write, adds_tags=True):
        before = stated(path)
        before_sha = file_sha(path)
        before_digest = decoded_md5(path, FFMPEG_EXE, 16)[0] if FFMPEG_EXE else None
        rc_before, _out = reference_test(path)
        n_before = tag_count(path)
        write(path)
        after_n = tag_count(path)
        after = stated(path)
        after_sha = file_sha(path)
        after_digest = decoded_md5(path, FFMPEG_EXE, 16)[0] if FFMPEG_EXE else None
        check(f"{label}: the file was rewritten", before_sha != after_sha,
              "nothing changed — the proof would be vacuous")
        check(f"{label}: the stated STREAMINFO MD5 is IDENTICAL",
              before == after, f"{before} -> {after}")
        if before_digest:
            check(f"{label}: the DECODED audio hashes identically",
                  before_digest == after_digest,
                  f"{before_digest} -> {after_digest}")
        # The reference verdict is PRESERVED — unchanged, not "passing": a tag
        # write cannot move the digest, so a header that lies keeps lying (that
        # is the audit's and the grade's to catch) and an honest one stays
        # honest.
        rc, out = reference_test(path)
        check(f"{label}: flac -t reaches the same verdict as before the write",
              rc == rc_before, f"rc={rc_before} -> {rc} {out[-160:]!r}")
        if adds_tags:
            check(f"{label}: the tag write landed ({n_before} -> {after_n} tags)",
                  after_n > n_before, f"{n_before} -> {after_n}")

    for name in ("honest", "tampered", "nomd5"):
        src = fx[name]
        single = os.path.join(work, f"{name}-single.flac")
        bulk = os.path.join(work, f"{name}-bulk.flac")
        beets = os.path.join(work, f"{name}-beets.flac")
        for dst in (single, bulk, beets):
            shutil.copyfile(src, dst)

        def one(path):
            af = AudioFile(path)
            assert af.set_tag("COMMENT", "written by the single-tag path"), af.error

        def many(path):
            af = AudioFile(path)
            af.defer_save(True)
            for i, key in enumerate(("COMMENT", "MOOD", "ENERGY")):
                af.set_tag(key, str(i))
            af.defer_save(False)

        def via_beets(path):
            # beets writes through mutagen's mediafile, which for FLAC is
            # mutagen.flac.FLAC.save() — the writer itself, not this app's
            # wrapper, is what the proof has to cover.
            from mutagen.flac import FLAC as MF
            f = MF(path)
            f["TITLE"] = ["Written by beets"]
            f.save()

        proof(f"{name} · single tag write (AudioFile)", single, one)
        proof(f"{name} · bulk write (defer_save)", bulk, many)
        proof(f"{name} · beets/mediafile (mutagen FLAC.save)", beets, via_beets)

    # The FLAC optimizer's own metadata operations (block stripping and
    # seektable edits — the metaflac calls script 3 makes when it does NOT
    # re-encode) must leave the digest alone as well: they rewrite the file.
    def metaflac_blocks(path):
        run_metaflac("--dont-use-padding", "--remove",
                     "--block-type=CUESHEET,APPLICATION,PADDING", path)
        run_metaflac("--add-seekpoint=10s", path)
        run_metaflac("--remove", "--block-type=SEEKTABLE", path)

    strip = os.path.join(work, "honest-metaflac.flac")
    shutil.copyfile(fx["honest"], strip)
    proof("honest · metaflac block/seektable edits (script 3, no re-encode)",
          strip, metaflac_blocks, adds_tags=False)


def check_chain_scripts(fx, root):
    print("\n== the chain's own tag writers (scripts 1 and 10) ==")
    if not FLAC_EXE:
        skip("no flac.exe: cannot measure the digest")
        return
    from mlo.lyrics import run_format_lyrics
    from mlo.format_all import run_format_all

    lib = os.path.join(root, "chain")
    album = os.path.join(lib, "Artists", "Artist", "2020 - Album")
    os.makedirs(album, exist_ok=True)
    paths = {}
    for name in ("honest", "tampered", "nomd5"):
        p = os.path.join(album, f"{name} - Track.flac")
        shutil.copyfile(fx[name], p)
        # Something for the writers to FIX, written the way THOSE scripts are
        # about — by another tagger, through mutagen: this app's own writers
        # canonicalize on the way in, so a value only this app writes is
        # already canonical and the pass would (correctly) touch nothing.
        # A legacy mixed-case MEDIA is script 1's own normalization job and a
        # padded TITLE is script 10's.
        from mutagen.flac import FLAC as MF
        f = MF(p)
        f["MEDIA"] = ["cd"]
        f["TITLE"] = ["  Track  "]
        f.save()
        paths[name] = p

    cfg = dict(DEFAULT_CONFIG)
    cfg.update({"music_folder": lib, "targets": None, "auth_username": "",
                "grade_verbose": False, "worker_limit": 1})
    cfg = normalize_config(cfg)

    before = {n: (stated(p), file_sha(p), tag_count(p), reference_test(p)[0])
              for n, p in paths.items()}
    stats1 = run_format_lyrics(dict(cfg))
    stats10 = run_format_all(dict(cfg))
    wrote = int(stats1.get("modified_count", 0)) + int(stats10.get("modified_count", 0))
    check("scripts 1 + 10 really rewrote the fixtures (the proof is not "
          "vacuous)", wrote >= 1, f"modified_count={wrote}")
    for name, p in paths.items():
        after = (stated(p), file_sha(p), tag_count(p), reference_test(p)[0])
        check(f"{name}: scripts 1/10 left the stated MD5 identical",
              before[name][0] == after[0], f"{before[name][0]} -> {after[0]}")
        check(f"{name}: scripts 1/10 rewrote the file (tags/bytes changed)",
              before[name][1] != after[1] or before[name][2] != after[2],
              f"{before[name]} -> {after}")
        check(f"{name}: the reference verdict is unchanged by the chain",
              before[name][3] == after[3],
              f"rc={before[name][3]} -> {after[3]}")
    return paths


# --------------------------------------------------------------------------- #
# 4. Optimizing: the conversion must not change the audio, or the original stays
# --------------------------------------------------------------------------- #
def opt_cfg(lib, **over):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({
        "music_folder": lib,
        "targets": [lib],
        "auth_username": "",
        "library_codec": "flac",
        "force_reencode_flac": False,
        "add_seektables": False,
        "encoder_tags": {},
        "lossless_remove_original": False,
        "grade_verbose": False,
        "worker_limit": 1,
    })
    cfg.update(over)
    return normalize_config(cfg)


def check_optimize(fx, root):
    print("\n== script 3 (optimize): audio identity across the re-encode ==")
    if not FLAC_EXE:
        skip("no flac.exe: script 3 needs it")
        return
    lib = os.path.join(root, "opt")
    os.makedirs(lib, exist_ok=True)
    paths = {}
    for name in ("honest", "tampered", "nomd5"):
        p = os.path.join(lib, f"{name}.flac")
        shutil.copyfile(fx[name], p)
        paths[name] = p

    before = {n: (stated(p), file_sha(p)) for n, p in paths.items()}
    stats = mlo_flac.run_optimize_flacs(opt_cfg(lib, force_reencode_flac=True))

    # honest: re-encoded, same audio, same digest.
    p = paths["honest"]
    check("honest: the re-encode happened",
          file_sha(p) != before["honest"][1], "the file was not rewritten")
    check("honest: the re-encoded file states the SAME digest (the audio is "
          "the audio)", stated(p) == before["honest"][0],
          f"{before['honest'][0]} -> {stated(p)}")
    rc, line = reference_test(p)
    check("honest: flac -t passes on the re-encoded file", rc == 0, f"{line!r}")

    # tampered: the conversion FAILS, nothing is replaced, and the run SAYS so.
    p = paths["tampered"]
    check("tampered: the original was kept byte-for-byte",
          file_sha(p) == before["tampered"][1],
          f"the file changed: {before['tampered'][1]} -> {file_sha(p)}")
    check("tampered: the file still states the wrong digest (nothing was "
          "quietly 'fixed')", stated(p) == TAMPER_HEX, stated(p))
    errs = [str(e) for e in stats.get("errors", [])]
    check("tampered: the run reported it as an ERROR (not a silent skip)",
          any("tampered.flac" in e and "FLAC MD5 mismatch" in e for e in errs),
          str(errs))
    check("tampered: the run counts exactly one failure and modifies only the "
          "other two files (the failure is not a modification)",
          stats.get("error_count") == 1 and stats.get("modified_count") == 2,
          f"errors={stats.get('error_count')} modified={stats.get('modified_count')}")

    # The refusal must not rest on the TOOL's own wording. A flac build that
    # encodes without judging the input's header — CI's did exactly that —
    # rewrote this file and reported success: the damage laundered into a file
    # whose header finally matched its audio. So the worker runs again,
    # directly, against a shim of that build (no -V, no complaint, exit 0),
    # and what is under test is the app's own header comparison.
    shim = unjudging_flac(root, FLAC_EXE)
    if not shim:
        skip("no flac.exe to shim: the header guard cannot be exercised")
    else:
        p = os.path.join(lib, "tampered_unjudged.flac")
        shutil.copyfile(fx["tampered"], p)
        before_shim = (stated(p), file_sha(p))
        _name, ok, info, _b_rem, _b_add = mlo_flac._optimize_flac(
            (shim, METAFLAC_EXE, p, 5, False, "", True, True, opt_cfg(lib)))
        check("tampered, against a tool that does not judge the header (no -V): "
              "the original is STILL kept byte-for-byte",
              file_sha(p) == before_shim[1],
              f"the file changed: {before_shim[1]} -> {file_sha(p)}")
        check("tampered, that tool: the file still states the wrong digest "
              "(nothing was quietly 'fixed')", stated(p) == TAMPER_HEX,
              stated(p))
        check("tampered, that tool: the refusal comes from the HEADER "
              "comparison and names it, and the run is not a success",
              (not ok) and "FLAC MD5 mismatch" in str(info),
              f"ok={ok} info={info!r}")

    # nomd5: an unknown source digest is not a licence to skip the check — the
    # re-encode decodes both sides (flac -V), and the file it writes states the
    # digest of the audio it really holds.
    p = paths["nomd5"]
    check("nomd5: the re-encode happened (an absent digest does not skip it)",
          file_sha(p) != before["nomd5"][1], "the file was not rewritten")
    check("nomd5: the re-encoded file now states a REAL digest",
          stated(p) not in ("", "0" * 32), stated(p))
    check("nomd5: and that digest is the digest of its decoded audio",
          stated(p) == decoded_md5(p, FFMPEG_EXE, 16)[0],
          f"{stated(p)} vs {decoded_md5(p, FFMPEG_EXE, 16)}")


def check_conversion(fx, root):
    print("\n== script 3 (conversion): the converted file's MD5 vs the "
          "source's decoded MD5 ==")
    if not (FLAC_EXE and FFMPEG_EXE):
        skip("no flac/ffmpeg: the conversion needs both")
        return
    lib = os.path.join(root, "conv")
    os.makedirs(lib, exist_ok=True)
    wav = write_tone(os.path.join(lib, "01 - Source.wav"), freq=440.0)

    # (a) a real conversion: the output must state the SOURCE's decoded digest.
    stats = mlo_flac.run_optimize_flacs(
        opt_cfg(lib, library_codec="flac", lossless_remove_original=False))
    out = os.path.join(lib, "01 - Source.flac")
    check("a WAV source converts to FLAC", os.path.isfile(out), sorted(os.listdir(lib)))
    if os.path.isfile(out):
        src_digest, err = decoded_md5(wav, FFMPEG_EXE, 16)
        check("the converted file's stated MD5 == the source's DECODED MD5",
              stated(out) == src_digest, f"{stated(out)} vs {src_digest} {err}")
        check("…and the reference decoder agrees its digest is its audio",
              stream_md5_state(out, FLAC_EXE, FFMPEG_EXE)[0] == MD5_OK,
              str(stream_md5_state(out, FLAC_EXE, FFMPEG_EXE)))
        check("…and the run counted a clean conversion (no errors, one "
              "modified file)",
              stats.get("error_count", 0) == 0
              and stats.get("modified_count", 0) == 1, str(stats))

    # (b) an encoder that hands back DIFFERENT audio: the conversion must fail,
    # the source must stay, and the run must report it. The "encoder" is a real
    # FLAC of another tone, so the mismatch is a real audio difference.
    lib2 = os.path.join(root, "conv_bad")
    os.makedirs(lib2, exist_ok=True)
    wav2 = write_tone(os.path.join(lib2, "01 - Source.wav"), freq=440.0)
    # The "wrong" audio lives OUTSIDE the album folder: it is a stand-in the
    # lying encoder hands back, not a second source for the pass to convert.
    other = os.path.join(root, "other")
    os.makedirs(other, exist_ok=True)
    wrong = encode_flac(write_tone(os.path.join(other, "other.wav"), freq=660.0,
                                   seconds=2.0),
                        os.path.join(other, "other.flac"))
    wav2_sha = file_sha(wav2)

    real_run_tool = mlo_flac.run_tool

    def lying_encoder(cmd, *a, **kw):
        """Hand back a FLAC of OTHER audio where the encode would run."""
        if (str(cmd[0]) == FFMPEG_EXE and "-y" in [str(c) for c in cmd]
                and "md5" not in [str(c) for c in cmd]):
            shutil.copyfile(wrong, cmd[-1])
            class _P:
                returncode = 0
                stdout = ""
                stderr = ""
            return _P()
        return real_run_tool(cmd, *a, **kw)

    lines = []
    real_log = mlo_flac.log
    mlo_flac.run_tool = lying_encoder
    mlo_flac.log = lambda msg, *a, **k: lines.append(str(msg))
    try:
        bad_stats = mlo_flac.run_optimize_flacs(
            opt_cfg(lib2, library_codec="flac", lossless_remove_original=True))
    finally:
        mlo_flac.run_tool = real_run_tool
        mlo_flac.log = real_log

    check("a conversion that changed the audio did NOT produce a file",
          not os.path.exists(os.path.join(lib2, "01 - Source.flac")),
          sorted(os.listdir(lib2)))
    check("the original source is exactly where it was, byte for byte",
          os.path.isfile(wav2) and file_sha(wav2) == wav2_sha,
          sorted(os.listdir(lib2)))
    errs = [str(e) for e in bad_stats.get("errors", [])]
    check("the failure reached the run's own error surface",
          any("01 - Source.wav" in e for e in errs), str(errs))
    check("and it was reported as a FAILURE, not counted as a by-design skip",
          bad_stats.get("error_count") == 1
          and not any(str(e).startswith("skipped") for e in errs),
          f"errors={errs} count={bad_stats.get('error_count')} "
          f"skipped={bad_stats.get('skipped_count')}")
    check("the run log names the file and the reason",
          any("FLAC MD5 mismatch" in ln for ln in lines), str(lines))


# --------------------------------------------------------------------------- #
# 5. Script 6 (audit): the verdict per fixture
# --------------------------------------------------------------------------- #
def audit_cfg(lib, **over):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({
        "music_folder": lib,
        "targets": [lib],
        "auth_username": "",
        "audit_integrity": True,
        "write_audit_tag": True,
        "audit_verify_cd_checksums": False,
        "audit_cd_require_both": False,
        "audit_require_accuraterip": False,
        "audit_verify_log_checksum": False,
        "grade_verbose": False,
        "worker_limit": 1,
    })
    cfg.update(over)
    return normalize_config(cfg)


def run_audit_without_spectral(lib, **over):
    """Script 6 with the Windows-only spectral tool absent — the audit's own
    supported mode (its integrity/CD checks run without it), which is what
    makes the MD5 verdicts here deterministic and offline. Returns
    (tags per file, log lines, stats)."""
    real_detect = mlo_audit.detect_all_tools

    def without_aa():
        tools = dict(real_detect())
        tools.pop("audioauditor", None)
        return tools

    lines = []
    real_log = mlo_audit.log
    mlo_audit.detect_all_tools = without_aa
    mlo_audit.log = lambda msg, *a, **k: lines.append(str(msg))
    try:
        stats = mlo_audit.run_audit_library(audit_cfg(lib, **over))
    finally:
        mlo_audit.detect_all_tools = real_detect
        mlo_audit.log = real_log
    return lines, stats


def check_audit(fx, root):
    print("\n== script 6 (audit): INTEGRITY / AUDIT per fixture ==")
    if not FLAC_EXE:
        skip("no flac.exe: the audit's integrity test needs it")
        return
    lib = os.path.join(root, "audit")
    os.makedirs(lib, exist_ok=True)
    paths = {}
    for name in ("honest", "tampered", "nomd5"):
        p = os.path.join(lib, f"{name}.flac")
        shutil.copyfile(fx[name], p)
        paths[name] = p

    lines, stats = run_audit_without_spectral(lib)
    tags = {n: (str(AudioFile(p).get_tag("INTEGRITY") or ""),
                str(AudioFile(p).get_tag("AUDIT") or ""))
            for n, p in paths.items()}
    check("honest: INTEGRITY=OK (its stated digest verified against its audio)",
          tags["honest"][0] == "OK", str(tags))
    check("tampered: INTEGRITY=FAIL — its own code, distinct from unknown",
          tags["tampered"][0] == "FAIL", str(tags))
    check("tampered: the AUDIT verdict is FAKE (a failing verdict, not a skip)",
          tags["tampered"][1] == "FAKE", str(tags))
    check("nomd5: INTEGRITY=UNKNOWN — reported as unknown, never as verified",
          tags["nomd5"][0] == "UNKNOWN", str(tags))

    joined = "\n".join(lines)
    check("the run log names the MD5 mismatch as such",
          "FLAC MD5 mismatch" in joined, joined[-1500:])
    check("…and names the no-MD5 files as unknown (a warning, not a failure)",
          "state NO MD5" in joined and "UNVERIFIED" in joined, joined[-1500:])
    check("the audit did not pass the no-MD5 file off as verified",
          "nomd5.flac" in joined, joined[-1500:])

    # Re-running over settled bytes must not re-decode, and must leave the
    # tags exactly as they are (the INTEGRITY write is a no-op when the file
    # already says it).
    before = {n: stated(p) for n, p in paths.items()}
    decodes = []
    real_verify = mlo_audit.verify_integrity

    def counting_verify(*a, **kw):
        decodes.append(a[0])
        return real_verify(*a, **kw)

    mlo_audit.verify_integrity = counting_verify
    try:
        lines2, _ = run_audit_without_spectral(lib)
    finally:
        mlo_audit.verify_integrity = real_verify
    check("a second audit re-decodes ONLY the file whose verdict is a FAIL "
          "(a pass is remembered, a failure is re-established)",
          decodes == [paths["tampered"]], str(decodes))
    check("…and the files' audio is untouched by it",
          all(stated(p) == before[n] for n, p in paths.items()),
          str({n: (before[n], stated(p)) for n, p in paths.items()}))

    # A file whose digest is TAMPERED (so its audio no longer matches what its
    # header claims) must be re-audited rather than covered by the stored
    # verdict: the identity the record is filed under IS that digest.
    retamper(paths["honest"], paths["honest"], b"\x01" * 16)
    lines3, _ = run_audit_without_spectral(lib)
    check("tampering a verified file's digest invalidates its stored verdict "
          "(it is re-decoded, not trusted)",
          str(AudioFile(paths["honest"]).get_tag("INTEGRITY") or "") == "FAIL",
          str(AudioFile(paths["honest"]).get_tag("INTEGRITY")))
    return stats


# --------------------------------------------------------------------------- #
# 6. Script 4 (grade): the finding a user sees, and who it fails
# --------------------------------------------------------------------------- #
def grade_cfg(lib, **over):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({
        "music_folder": lib,
        "auth_username": "",
        "grade_verbose": False,
        "grade_check_flac_md5": True,
        # Everything else this suite's fixtures would trip for reasons it does
        # not set up (they are synthetic folders with no cover, no naming-script
        # layout and a handful of tags) is switched off, so what is measured is
        # the MD5 check and nothing else.
        "grade_check_naming": False,
        "grade_check_missing_tags": False,
        "grade_check_album_tags": False,
        "grade_check_lyrics": False,
        "grade_check_lyrics_format": False,
        "grade_check_instrumental": False,
        "grade_check_cover": False,
        "grade_check_sidecar_cover": False,
        "grade_check_encoder": False,
        "grade_check_media": False,
        "grade_check_source": False,
        "grade_check_unreadable": False,
        "grade_check_disallowed": False,
        "grade_check_ext_case": False,
        "grade_check_filename_case": False,
        "grade_check_excess_tags": False,
        "grade_check_audit": False,
        "grade_check_mb_links": False,
        "grade_check_rym_links": False,
        "grade_check_mood": False,
        "grade_check_energy": False,
        "grade_check_genre": False,
        "grade_check_genre_count": False,
        "grade_check_genre_order": False,
        "grade_check_replaygain": False,
        "grade_check_acoustid": False,
        "grade_check_album_description": False,
        "grade_check_tag_case": False,
        "grade_check_tag_spaces": False,
        "grade_check_expected_tracks": False,
        "grade_check_raw_video": False,
        "grade_check_lossless_source": False,
        "grade_check_disc_naming": False,
        "grade_check_cd_log": False,
        "grade_check_cd_cue": False,
        "grade_check_cd_format": False,
        "grade_check_crc": False,
        "grade_check_log_checksum": False,
        "grade_check_accuraterip": False,
        "grade_check_log_grade": False,
        "grade_check_artist_image": False,
        "grade_check_artist_description": False,
        "grade_check_key_bpm": False,
    })
    cfg.update(over)
    return normalize_config(cfg)


def check_grading(fx, root):
    print("\n== script 4 (grade): the FLAC MD5 finding, per fixture ==")
    if not FLAC_EXE:
        skip("no flac.exe: grading verifies with it")
        return
    lib = os.path.join(root, "grade")
    album = os.path.join(lib, "Album")
    os.makedirs(album, exist_ok=True)
    paths = {}
    for i, name in enumerate(("honest", "tampered", "nomd5"), 1):
        p = os.path.join(album, f"0{i} - {name}.flac")
        shutil.copyfile(fx[name], p)
        AudioFile(p).set_tag("TITLE", name)
        paths[name] = p

    cfg = grade_cfg(lib)
    res = mlo_grader._grade_album(album, "EMBEDDED", dict(cfg))
    issues = res["issues"]
    by_track = {os.path.basename(tr["file"]): tr for tr in res["tracks"]}

    has_mismatch = [k for k in issues if "FLAC MD5 mismatch" in k]
    has_absent = [k for k in issues if "FLAC MD5 absent" in k]
    check("the tampered file's finding names the mismatch",
          bool(has_mismatch)
          and "02 - tampered.flac" in ", ".join(flat(issues[k]) for k in has_mismatch),
          str(issues))
    check("the no-MD5 file's finding names the absence of a digest",
          bool(has_absent)
          and "03 - nomd5.flac" in ", ".join(flat(issues[k]) for k in has_absent),
          str(issues))
    check("the honest file gets NO FLAC MD5 finding",
          not any("01 - honest.flac" in ", ".join(flat(issues[k]))
                  for k in has_mismatch + has_absent), str(issues))
    check("the tampered file carries the FLAC_MD5 issue code",
          "FLAC_MD5" in by_track["02 - tampered.flac"]["issues"],
          str(by_track["02 - tampered.flac"]["issues"]))
    check("the no-MD5 file carries FLAC_MD5_ABSENT and NOT FLAC_MD5",
          "FLAC_MD5_ABSENT" in by_track["03 - nomd5.flac"]["issues"]
          and "FLAC_MD5" not in by_track["03 - nomd5.flac"]["issues"],
          str(by_track["03 - nomd5.flac"]["issues"]))
    check("a mismatching file cannot grade PASS",
          res["pass_count"] != res["total_checks"],
          f"{res['pass_count']}/{res['total_checks']}")

    # The same album with the check OFF: the bad file passes again — the
    # toggle is what decides, and nothing else in the grade noticed.
    off = mlo_grader._grade_album(album, "EMBEDDED",
                                  dict(cfg, grade_check_flac_md5=False))
    check("with grade_check_flac_md5 off, no FLAC MD5 finding is raised",
          not any("FLAC MD5" in k for k in off["issues"]), str(off["issues"]))
    check("…and the album passes (the finding was the only thing failing it)",
          off["pass_count"] == off["total_checks"],
          f"{off['pass_count']}/{off['total_checks']} {off['issues']}")

    # A good file must not start failing because of this check: the honest
    # fixture on its own still grades clean.
    good_album = os.path.join(root, "grade_good", "Album")
    os.makedirs(good_album, exist_ok=True)
    good = os.path.join(good_album, "01 - honest.flac")
    shutil.copyfile(fx["honest"], good)
    AudioFile(good).set_tag("TITLE", "honest")
    good_res = mlo_grader._grade_album(good_album, "EMBEDDED",
                                       dict(grade_cfg(root)))
    check("an honest FLAC still grades clean with the check ON",
          good_res["pass_count"] == good_res["total_checks"],
          f"{good_res['pass_count']}/{good_res['total_checks']} "
          f"{good_res['issues']}")

    # The check is wired like every other one: a key in DEFAULT_CONFIG that the
    # grader's own gate scan finds (server/api_stack reads the same set).
    gates = set(mlo_grader.check_gates())
    check("grade_check_flac_md5 is a real gate the grader reads",
          "grade_check_flac_md5" in gates, sorted(gates)[:5])
    check("…and it ships ON, like every other check (strict = the defaults)",
          DEFAULT_CONFIG.get("grade_check_flac_md5") is True,
          str(DEFAULT_CONFIG.get("grade_check_flac_md5")))
    return res


# --------------------------------------------------------------------------- #
# 7. The tag vocabulary written by script 6 is the one the pages validate
# --------------------------------------------------------------------------- #
def check_vocabulary():
    print("\n== the INTEGRITY vocabulary (registry / tag enum) ==")
    try:
        from server.tags_registry import registry
        reg = registry()
    except Exception as e:
        skip(f"tags_registry unavailable: {e}")
        return
    row = next((t for t in reg["tags"] if t["key"] == "INTEGRITY"), None)
    check("INTEGRITY is a registered tag", row is not None, "")
    if row:
        check("its closed value set is OK / FAIL / UNKNOWN",
              list(row["enum"] or []) == ["OK", "FAIL", "UNKNOWN"],
              str(row["enum"]))
        check("script 6 is still named as its writer",
              "6" in str(row["writer"]), str(row["writer"]))
        check("grade_check_flac_md5 is named as one of its graders",
              "grade_check_flac_md5" in (row["graded_by"] or []),
              str(row["graded_by"]))
        check("the issue codes it can raise are declared for the track page",
              {"FLAC_MD5", "FLAC_MD5_ABSENT"} <= set(row["issue_codes"] or []),
              str(row["issue_codes"]))
    keys = {c["key"] for c in reg["checks"]}
    check("the new check is a registry check with a label",
          "grade_check_flac_md5" in keys, sorted(keys)[:3])


# --------------------------------------------------------------------------- #
def main():
    print("FLAC STREAMINFO MD5 — grade / audit / tag / optimize")
    tmp = tempfile.mkdtemp(prefix="mlo-md5-")
    try:
        if not FLAC_EXE or not METAFLAC_EXE:
            print("SKIP: no vendored flac/metaflac under .dependencies")
            return 2
        fx = check_fixtures(tmp)
        check_states(fx)
        check_tag_writes(fx, tmp)
        check_chain_scripts(fx, tmp)
        check_optimize(fx, tmp)
        check_conversion(fx, tmp)
        check_audit(fx, tmp)
        check_grading(fx, tmp)
        check_vocabulary()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{PASSED[0]} check(s) passed, {len(FAILED)} failed"
          + (f", {len(SKIPPED)} skipped" if SKIPPED else ""))
    for label in FAILED:
        print(f"  FAILED: {label}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
