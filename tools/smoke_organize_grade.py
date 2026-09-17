"""Functional smoke test: organize sweeps every file, extra images fail grading.

Builds a synthetic album (real FLACs via .dependencies/flac), runs the
organize endpoint logic with a sandboxed music folder, then the grader.

Run:  python tools/smoke_organize_grade.py
Exits 2 when no flac encoder is available.
"""
import os
import shutil
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


def make_flac(path, title, track, artist="Smoke Artist", album="Smoke Album"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    wav = path + ".tmp.wav"
    with wave.open(wav, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00\x00\x00" * 44100)  # 1 s of silence
    subprocess.run([FLAC, "-f", "-s", "--totally-silent", "-o", path, wav], check=True)
    os.remove(wav)
    from mutagen.flac import FLAC as MutagenFLAC

    f = MutagenFLAC(path)
    f["TITLE"] = title
    f["ARTIST"] = artist
    f["ALBUMARTIST"] = artist
    f["ALBUM"] = album
    f["TRACKNUMBER"] = f"{track:02d}"
    f["DISCNUMBER"] = "1"
    f["TRACKTOTAL"] = "2"
    f["DISCTOTAL"] = "1"
    f["DATE"] = "2020"
    f["MEDIA"] = "CD"
    f["CATALOGNUMBER"] = "SMOKE-001"
    f["LABEL"] = "Smoke Records"
    f.save()


def main():
    if FLAC is None:
        print("SKIP: no flac.exe found in .dependencies or PATH")
        return 2
    from server import main as srv

    tmp = tempfile.mkdtemp(prefix="mlo_smoke_")
    music = os.path.join(tmp, "music")
    album = os.path.join(music, "Smoke Artist", "Old Folder")
    os.makedirs(album)

    a = os.path.join(album, "01 - Song A.flac")
    b = os.path.join(album, "02 - Song B.flac")
    make_flac(a, "Song A", 1)
    make_flac(b, "Song B", 2)

    # strays: root extra image, subfolder image, txt, per-track sidecars
    with open(os.path.join(album, "front.jpg"), "wb") as f:
        f.write(b"\xff\xd8\xff\xe0JFIF-smoke")
    scans = os.path.join(album, "scans")
    os.makedirs(scans)
    with open(os.path.join(scans, "back.jpg"), "wb") as f:
        f.write(b"\xff\xd8\xff\xe0JFIF-smoke")
    with open(os.path.join(album, "notes.txt"), "w") as f:
        f.write("stray")
    with open(os.path.join(album, "01 - Song A.lrc"), "w") as f:
        f.write("[00:01.00] hi")

    srv.load_config = lambda: {"music_folder": music, "naming_script": "", "short_folder_names": False}
    from server.mbresolve import invalidate

    invalidate()

    # ---- organize: everything must move, nothing left behind -------------
    res = srv.organize(type("R", (), {"paths": [album], "dry_run": False}))["results"][0]
    assert res.get("ok"), res
    print("organize #1:", {k: res[k] for k in ("moved", "leftovers", "pruned")})
    new_root = res["album_root"].replace("/", os.sep)
    assert not os.path.isdir(album), f"old album dir still exists: {album}"
    listing = sorted(
        os.path.relpath(os.path.join(r, f), new_root).replace("\\", "/")
        for r, _, fs in os.walk(new_root)
        for f in fs
    )
    print("new album contents:")
    for x in listing:
        print("   ", x)
    names = {os.path.basename(x) for x in listing}
    assert any(x.endswith(".flac") for x in listing) and len([x for x in listing if x.endswith(".flac")]) == 2
    assert "notes.txt" in names, f"notes.txt not swept: {listing}"
    assert "front.jpg" in names, f"front.jpg not swept: {listing}"
    assert "back.jpg" in names, f"scans/back.jpg not swept to album root: {listing}"
    assert "1-01 Song A.lrc" in names, f"track sidecar lrc lost: {listing}"
    leftovers_all = [x for x in listing if x.endswith((".jpg", ".txt"))]
    assert not any("/" in x for x in leftovers_all), "leftovers were not flattened to album root"

    # ---- grading: extra images + txt must FAIL the album ------------------
    from mlo.grader import _grade_album

    g = _grade_album(new_root, "EMBEDDED", {"music_folder": music, "lyrics_format": "EMBEDDED"})
    issues = {i for i in g["issues"]}
    print("grade issues:", sorted(issues))
    assert any(i.startswith("Extra artwork not tied to any track") for i in issues), issues
    assert any(i.startswith("Disallowed file types") for i in issues), issues
    assert not g["pass_count"] == g["total_checks"], "album should FAIL"

    # ---- clean the strays; every remaining image is a track sidecar -------
    os.remove(os.path.join(new_root, "notes.txt"))
    os.remove(os.path.join(new_root, "front.jpg"))
    os.rename(os.path.join(new_root, "back.jpg"), os.path.join(new_root, "1-01 Song A.cover.jpg"))
    g2 = _grade_album(new_root, "EMBEDDED", {"music_folder": music, "lyrics_format": "EMBEDDED"})
    issues2 = {i for i in g2["issues"]}
    print("grade issues after cleanup:", sorted(issues2) or "none")
    assert not any(i.startswith("Extra artwork") for i in issues2), issues2

    # ---- organize with nothing to move must still sweep strays ------------
    sub = os.path.join(new_root, "extra junk")
    os.makedirs(sub, exist_ok=True)
    with open(os.path.join(sub, "stray.txt"), "w") as f:
        f.write("x")
    res2 = srv.organize(type("R", (), {"paths": [new_root], "dry_run": False}))["results"][0]
    print("organize #2:", {k: res2.get(k) for k in ("ok", "moved", "leftovers", "pruned")})
    assert res2.get("ok"), res2
    assert res2.get("leftovers", 0) == 1, res2
    assert os.path.isfile(os.path.join(new_root, "stray.txt")), "stray not swept on a no-move album"
    assert not os.path.isdir(sub), "emptied subfolder not pruned"

    # ---- naming variables expose label ------------------------------------
    from mlo.naming import track_variables

    v = track_variables({"LABEL": "Smoke Records", "MEDIA": "CD"})
    assert v["label"] == "Smoke Records" and v["media"] == "CD"

    # ---- TAG_MAP round-trips LABEL ----------------------------------------
    from mlo.audio import AudioFile

    moved_flac = os.path.join(new_root, "1-01 Song A.flac")
    af = AudioFile(moved_flac)
    assert af.get_tag("LABEL") == "Smoke Records", af.get_tag("LABEL")
    assert af.get_tag("CATALOGNUMBER") == "SMOKE-001"
    assert af.get_tag("MEDIA") == "CD"

    shutil.rmtree(tmp, ignore_errors=True)
    print("SMOKE_OK")


if __name__ == "__main__":
    sys.exit(main())
