#!/usr/bin/env python3
"""Regression tests for .cue sheet renaming.

Covers the failure modes that left cues unrenamed while logs renamed fine:
  1. image-style cue sheets whose FILE entries reference names that no
     longer exist (single FILE for the whole disc) — now matched by
     track count / INDEX start times against the real audio,
  2. case-only renames (cd-1.cue -> CD-1.cue) which os.rename silently
     refuses on Windows,
  3. the run_format_cues re-collect bug where any successful rename made
     the script abort with "No .cue files found." before formatting.

Run: python tools/test_cue_rename.py
Needs the bundled flac.exe (real audio is required for duration evidence).
"""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CFG = {
    "discs_rename_enabled": True,
    "discs_rename_pattern": "CD-{n}",
    "discs_rename_single_fallback": True,
    "discs_toc_tolerance_s": 4.0,
    "discs_toc_unique_margin_s": 4.0,
    "cue_fix_filenames": True,
}


def find_flac():
    dep = os.path.join(ROOT, ".dependencies")
    if os.path.isdir(dep):
        for entry in sorted(os.listdir(dep)):
            if entry.lower().startswith("flac"):
                cand = os.path.join(dep, entry, "flac.exe")
                if os.path.isfile(cand):
                    return cand
    return shutil.which("flac")


def make_flac(flac_exe, path, seconds):
    """Encode a real FLAC of the given duration (so mutagen sees the
    stream length used as INDEX-matching evidence)."""
    rate = 44100
    frames = int(rate * seconds)
    wav = os.path.splitext(path)[0] + ".wav"
    with wave.open(wav, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00\x00\x00" * frames)
    subprocess.run(
        [flac_exe, "--totally-silent", "-f", "-o", path, wav],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    os.remove(wav)


def make_album(base, name, discs):
    """discs: {disc_number: [track durations]} -> D-TT named FLACs."""
    flac = find_flac()
    if flac is None:
        print("SKIP: flac.exe not found (needed for real audio durations)")
        sys.exit(2)
    d = os.path.join(base, name)
    os.makedirs(d)
    for disc, durations in discs.items():
        for i, secs in enumerate(durations, 1):
            make_flac(flac, os.path.join(d, f"{disc}-{i:02d} T.flac"), secs)
    return d


def cue_text(file_ref, durations):
    """Non-canonical image-style cue: CRLF, per-track INDEX starts built
    from the durations, plus a REM line canonicalization must drop."""
    lines = ["REM DISCID 8A0B1234", "REM COMMENT ExactAudioCopy junk",
             f'FILE "{file_ref}" WAVE']
    cum = 0.0
    for i, secs in enumerate(durations, 1):
        mm, ss = int(cum // 60), int(cum % 60)
        lines.append(f"  TRACK {i:02d} AUDIO")
        lines.append(f"    INDEX 01 {mm:02d}:{ss:02d}:00")
        cum += secs
    return "\r\n".join(lines) + "\r\n"


def write_cue(album_dir, name, file_ref, durations):
    path = os.path.join(album_dir, name)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(cue_text(file_ref, durations))
    return path


def test_image_cues_multi_disc(base):
    """Cues named without any disc evidence, FILE entries pointing at a
    nonexistent wav — must land on the right discs via INDEX durations."""
    d = make_album(base, "MultiDisc", {1: [2.0, 30.0], 2: [45.0, 90.0]})
    write_cue(d, "Album.cue", "Whatever.wav", [2.0, 30.0])
    write_cue(d, "Album II.cue", "Whatever.wav", [45.0, 90.0])
    from mlo.discs import rename_cues_for_discs
    notes = rename_cues_for_discs(d, config=CFG)
    names = sorted(os.listdir(d))
    assert "CD-1.cue" in names and "CD-2.cue" in names, (names, notes)
    with open(os.path.join(d, "CD-1.cue"), encoding="utf-8") as fh:
        # INDEX 01 00:02:00 belongs to disc 1 (2s + 30s tracks)
        assert "INDEX 01 00:02:00" in fh.read()
    print("PASS  image-style cues on a multi-disc album -> CD-1/CD-2")


def test_track_count_match(base):
    """Same durations everywhere; only the track count disambiguates."""
    d = make_album(base, "CountMatch", {1: [2.0, 2.0], 2: [2.0, 2.0, 2.0]})
    write_cue(d, "Part One.cue", "img.wav", [2.0, 2.0])
    write_cue(d, "Part Two.cue", "img.wav", [2.0, 2.0, 2.0])
    from mlo.discs import rename_cues_for_discs
    notes = rename_cues_for_discs(d, config=CFG)
    names = sorted(os.listdir(d))
    assert "CD-1.cue" in names and "CD-2.cue" in names, (names, notes)
    print("PASS  track-count evidence assigns ambiguous cues")


def test_case_only_rename(base):
    """cd-1.cue already carries the right name up to case — must still
    become CD-1.cue on a case-insensitive filesystem."""
    d = make_album(base, "CaseOnly", {1: [2.0]})
    write_cue(d, "cd-1.cue", "img.wav", [2.0])
    from mlo.discs import rename_cues_for_discs
    notes = rename_cues_for_discs(d, config=CFG)
    names = sorted(os.listdir(d))
    assert "CD-1.cue" in names, (names, notes)
    print("PASS  case-only rename cd-1.cue -> CD-1.cue")


def test_format_cues_recollect(base):
    """run_format_cues must keep formatting after a rename happened
    (previously it aborted with 'No .cue files found.')."""
    from mlo.cue import run_format_cues
    lib = os.path.join(base, "Lib")
    d = make_album(lib, "Artist - Album", {1: [2.0, 3.0]})
    write_cue(d, "Album.cue", "Whatever.wav", [2.0, 3.0])
    cfg = dict(CFG)
    cfg.update({
        "music_folder": lib,
        "keep_empty_cue_lines": False,
        "keep_other_cue_lines": False,
        "cue_file_type": "WAVE",
        "append_final_newline": False,
        "force_cue": False,
    })
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        run_format_cues(cfg)
    names = sorted(os.listdir(d))
    assert "CD-1.cue" in names, (names, "cue was not renamed")
    with open(os.path.join(d, "CD-1.cue"), encoding="utf-8") as fh:
        content = fh.read()
    assert 'FILE "Whatever.wav" WAVE' in content, content
    assert "REM COMMENT" not in content, "cue was renamed but not formatted"
    print("PASS  run_format_cues formats cues after renaming (re-collect)")


def write_cue_custom(album_dir, name, file_refs):
    """One FILE per track, in the order EAC writes a per-track rip."""
    lines = ["REM DISCID DE0A3310"]
    for i, ref in enumerate(file_refs, 1):
        lines.append(f'FILE "{ref}" WAVE')
        lines.append(f"  TRACK {i:02d} AUDIO")
        lines.append("    INDEX 01 00:00:00")
    path = os.path.join(album_dir, name)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("\r\n".join(lines) + "\r\n")
    return path


def touch_audio(album_dir, *names):
    """Dummy files under real library names: the FILE matcher reads NAMES,
    so a cue-repair test needs the files to exist, not to decode."""
    for n in names:
        with open(os.path.join(album_dir, n), "wb") as fh:
            fh.write(b"fLaC")


def test_disc_aware_track_refs(base):
    """A cue whose FILE lines are track-first ("02 - 36.wav", the track
    titled "36") over the library's own D-TT names: the reference's number is
    a TRACK of the cue's own disc — _track_num_of reads "09 - 36.wav" as disc
    09 / track 36, which left every reference of a real album unrepaired.

    The disc restriction is what decides between two discs of one album
    carrying the same track numbers, and a reference no track of the disc
    explains is still left exactly as written."""
    from mlo.discs import cue_file_refs, fix_cue_filenames

    d = os.path.join(base, "TrackFirst")
    os.makedirs(d)
    touch_audio(d, "1-01 Alpha.flac", "1-02 36.flac", "1-03 Gamma.flac")
    write_cue_custom(d, "CD-1.cue",
                     ["01 - Alpha.wav", "02 - 36.wav", "03 - Gamma.wav",
                      "77 - Nothing.wav"])
    notes = fix_cue_filenames(d, config=CFG)
    refs = cue_file_refs(os.path.join(d, "CD-1.cue"))
    assert refs == ["1-01 Alpha.flac", "1-02 36.flac", "1-03 Gamma.flac",
                    "77 - Nothing.wav"], (notes, refs)
    assert any("77 - Nothing.wav" in n and "left as-is" in n for n in notes), notes
    print("PASS  a track-first reference finds the D-TT track of its disc")

    md = os.path.join(base, "TwoDiscs")
    os.makedirs(md)
    touch_audio(md, "1-01 A.flac", "1-02 B.flac", "1-03 C.flac",
                "2-01 A.flac", "2-02 B.flac", "2-03 C.flac")
    write_cue_custom(md, "CD-1.cue", ["01 - A.wav", "03 - C.wav"])
    write_cue_custom(md, "CD-2.cue", ["03 - C.wav"])
    notes = fix_cue_filenames(md, config=CFG)
    refs1 = cue_file_refs(os.path.join(md, "CD-1.cue"))
    refs2 = cue_file_refs(os.path.join(md, "CD-2.cue"))
    # Track 3 exists on both discs: only the cue's own disc may answer.
    assert refs1 == ["1-01 A.flac", "1-03 C.flac"], (notes, refs1)
    assert refs2 == ["2-03 C.flac"], (notes, refs2)
    print("PASS  the same track number resolves per disc")

    sd = os.path.join(base, "StaleName")
    os.makedirs(sd)
    touch_audio(sd, "1-01 A.flac", "1-02 36 [old].flac",
                "2-01 A.flac", "2-02 36 [old].flac")
    write_cue_custom(sd, "CD-2.cue", ["2-02 36 [new].flac"])
    notes = fix_cue_filenames(sd, config=CFG)
    refs = cue_file_refs(os.path.join(sd, "CD-2.cue"))
    assert refs == ["2-02 36 [old].flac"], (notes, refs)
    print("PASS  a D-TT reference is read as this disc's track")

    im = os.path.join(base, "ImageSheet")
    os.makedirs(im)
    touch_audio(im, "CD-1.flac")
    write_cue_custom(im, "CD-1.cue", ["CDImage.wav"])
    notes = fix_cue_filenames(im, config=CFG)
    refs = cue_file_refs(os.path.join(im, "CD-1.cue"))
    assert refs == ["CD-1.flac"], (notes, refs)
    print("PASS  a single-image sheet names the disc's one file")


def test_converted_source_repoints(base):
    """A lossless conversion renames the audio under the sheet.

    With the rip source kept next to the converted file the referenced name
    still EXISTS, which is why the fix used to skip it: the sheet named a .wav
    the library would never play. And when the folder holds more than one
    candidate the sheet must be left exactly as written."""
    from mlo.discs import cue_file_refs, fix_cue_filenames

    d = make_album(base, "Converted", {1: [2.0]})
    for name, magic in (("img.wav", b"RIFF"), ("img.flac", b"fLaC")):
        with open(os.path.join(d, name), "wb") as fh:
            fh.write(magic)
    write_cue(d, "Album.cue", "img.wav", [2.0])
    notes = fix_cue_filenames(d, config=CFG)
    refs = cue_file_refs(os.path.join(d, "Album.cue"))
    assert refs == ["img.flac"], (notes, refs)
    print("PASS  a converted source is repointed to the file the album holds")

    d2 = make_album(base, "Ambiguous", {1: [2.0]})
    for name in ("img.wav", "img.flac", "img.mp3"):
        with open(os.path.join(d2, name), "wb") as fh:
            fh.write(b"x")
    write_cue(d2, "Album.cue", "img.wav", [2.0])
    notes = fix_cue_filenames(d2, config=CFG)
    refs = cue_file_refs(os.path.join(d2, "Album.cue"))
    assert refs == ["img.wav"], (notes, refs)
    print("PASS  an ambiguous folder leaves the FILE line alone")


def main():
    flac = find_flac()
    if flac is None:
        print("SKIP: no flac.exe found in .dependencies or PATH")
        return 2
    base = tempfile.mkdtemp(prefix="mlo_cue_test_")
    try:
        test_image_cues_multi_disc(base)
        test_track_count_match(base)
        test_case_only_rename(base)
        test_format_cues_recollect(base)
        test_disc_aware_track_refs(base)
        test_converted_source_repoints(base)
        print("All cue-rename tests passed.")
        return 0
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
