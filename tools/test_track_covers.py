#!/usr/bin/env python3
"""Per-track cover manifests: one image file, many tracks (phase 3b).

Pins the storage contract in mlo/paths.py, the two consumers that resolve art
through it (mlo/grader.py per-track grading + extra-artwork predicate,
server/library.py `_enrich_track`), the HTTP surface in server/main.py
(`tracks=` on the two write routes, `/api/cover/clear`, the
width/height/megapixels/warning response) and the organize() manifest rewrite.

Everything runs in throwaway folders; the configured music folder is never
touched.

Run: python tools/test_track_covers.py
Exit 0 = pass, 1 = failure, 2 = skip (no flac.exe / no Pillow).
"""
import asyncio
import atexit
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
# hermeticity: importing server.main runs module-level database _init()s that
# resolve their path through the configured music folder — the live install.
# Redirect before that import (same trick as tools/test_trash_api.py).
# --------------------------------------------------------------------------- #
REAL_MUSIC_FOLDER = ""
try:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        REAL_MUSIC_FOLDER = str((json.load(f) or {}).get("music_folder") or "")
except Exception:
    pass

REDIRECT = tempfile.mkdtemp(prefix="mlo-covers-redirect-")
os.environ["MLO_MUSIC_FOLDER"] = REDIRECT

import mlo.config as cfgmod  # noqa: E402
import mlo.paths as pathmod  # noqa: E402

_STUB = os.path.join(REDIRECT, "config.json")
with open(_STUB, "w", encoding="utf-8") as f:
    json.dump({"music_folder": REDIRECT}, f)
for _mod in (cfgmod, pathmod):
    _mod.CONFIG_FILE = _STUB
    if getattr(_mod, "LEGACY_DATA_DIR", None) is not None:
        _mod.LEGACY_DATA_DIR = os.path.join(REDIRECT, "legacy")


TMP = None


def _cleanup():
    os.environ.pop("MLO_MUSIC_FOLDER", None)
    shutil.rmtree(REDIRECT, ignore_errors=True)
    if TMP:
        shutil.rmtree(TMP, ignore_errors=True)


TMP = tempfile.mkdtemp(prefix="mlo-covers-")
atexit.register(_cleanup)

from mlo.config import DEFAULT_CONFIG  # noqa: E402
from mlo.paths import (TRACK_COVERS_FILE, clear_track_covers,  # noqa: E402
                       get_sidecar_cover_path, get_track_cover,
                       load_track_covers, save_track_covers, set_track_covers)

MUSIC = os.path.join(TMP, "music")

FAILED = []


def check(label, fn):
    """Run one labelled check; a failed assertion is reported, not fatal."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - report and keep checking
        FAILED.append(label)
        print(f"FAIL {label}: {type(e).__name__}: {e}")
    else:
        print(f"ok   {label}")


def album(name):
    d = os.path.join(MUSIC, "Artist", name)
    os.makedirs(d, exist_ok=True)
    return d


def image(path, size=(1200, 1200), fmt=None):
    """A real image file of the given pixel size (Pillow is a hard need)."""
    from PIL import Image

    fmt = fmt or ("PNG" if os.path.splitext(path)[1].lower() == ".png" else "JPEG")
    Image.new("RGB", size, (20, 40, 60)).save(path, fmt)
    return path


def image_bytes(size=(828, 828), fmt="PNG"):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, (20, 40, 60)).save(buf, fmt)
    return buf.getvalue()


def images_in(folder):
    return sorted(f for f in os.listdir(folder)
                  if f.lower().endswith((".jpg", ".jpeg", ".png", ".jxl")))


# --------------------------------------------------------------------------- #
# prerequisites: real audio (grading + organize) and Pillow (image fixtures)
# --------------------------------------------------------------------------- #
def find_flac():
    dep = os.path.join(ROOT, ".dependencies")
    if os.path.isdir(dep):
        for entry in sorted(os.listdir(dep)):
            if entry.lower().startswith("flac"):
                cand = os.path.join(dep, entry, "flac.exe")
                if os.path.isfile(cand):
                    return cand
    return shutil.which("flac")


FLAC = find_flac()
try:
    from PIL import Image  # noqa: F401
    HAS_PIL = True
except Exception:
    HAS_PIL = False


def make_flac(path, title, track):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    wav = path + ".tmp.wav"
    with wave.open(wav, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00\x00\x00" * 44100)
    subprocess.run([FLAC, "-f", "-s", "--totally-silent", "-o", path, wav], check=True)
    os.remove(wav)
    from mutagen.flac import FLAC as MutagenFLAC

    f = MutagenFLAC(path)
    f["TITLE"] = title
    f["ARTIST"] = "Cover Artist"
    f["ALBUMARTIST"] = "Cover Artist"
    f["ALBUM"] = "Cover Album"
    f["TRACKNUMBER"] = f"{track:02d}"
    f["DISCNUMBER"] = "1"
    f["TRACKTOTAL"] = "2"
    f["DISCTOTAL"] = "1"
    f["DATE"] = "2020"
    f["MEDIA"] = "CD"
    f["CATALOGNUMBER"] = "COV-001"
    f["LABEL"] = "Cover Records"
    f.save()


# --------------------------------------------------------------------------- #
# 1. storage contract — one image, many tracks
# --------------------------------------------------------------------------- #
print("== manifest storage ==")
A = album("Storage")


def t_shared_image():
    image(os.path.join(A, "07 - A.jpg"))
    m = set_track_covers(A, ["07 - A.flac", "08 - B.flac"], "07 - A.jpg")
    assert m == {"07 - A.flac": "07 - A.jpg", "08 - B.flac": "07 - A.jpg"}, m
    assert load_track_covers(A) == m, load_track_covers(A)
    # exactly ONE image file in the folder, both tracks resolve to it
    assert images_in(A) == ["07 - A.jpg"], images_in(A)
    seven = get_track_cover(A, "07 - A.flac")
    eight = get_track_cover(A, "08 - B.flac")
    assert seven == eight == os.path.join(A, "07 - A.jpg"), (seven, eight)


def t_lookup_is_case_insensitive():
    assert get_track_cover(A, "08 - b.FLAC") == os.path.join(A, "07 - A.jpg")


def t_second_pair_does_not_copy():
    image(os.path.join(A, "09 - C.png"))
    before = hashlib.md5(open(os.path.join(A, "07 - A.jpg"), "rb").read()).hexdigest()
    set_track_covers(A, ["09 - C.flac"], "09 - C.png")
    assert images_in(A) == ["07 - A.jpg", "09 - C.png"], images_in(A)
    after = hashlib.md5(open(os.path.join(A, "07 - A.jpg"), "rb").read()).hexdigest()
    assert before == after, "the first image was rewritten"
    m = load_track_covers(A)
    assert m["09 - C.flac"] == "09 - C.png" and len(m) == 3, m


def t_missing_image_is_refused():
    before = load_track_covers(A)
    m = set_track_covers(A, ["10 - D.flac"], "10 - D.jpg")  # no such file
    assert m == before, m
    assert "10 - D.flac" not in load_track_covers(A)
    assert get_track_cover(A, "10 - D.flac") is None


check("two tracks share one image file", t_shared_image)
check("track lookup is case-insensitive", t_lookup_is_case_insensitive)
check("a second assignment never copies the image", t_second_pair_does_not_copy)
check("a missing image is refused, not recorded", t_missing_image_is_refused)

# --------------------------------------------------------------------------- #
# 2. resolution: manifest -> stem probe -> None
# --------------------------------------------------------------------------- #
print("== resolution order ==")
B = album("Fallback")
image(os.path.join(B, "01 - Song.jpg"))


def t_stem_probe_without_manifest():
    got = get_track_cover(B, "01 - Song.flac")
    assert got == os.path.join(B, "01 - Song.jpg"), got
    assert got == get_sidecar_cover_path(B, "01 - Song.flac")
    assert load_track_covers(B) == {}, "no manifest expected"


def t_none_without_art():
    assert get_track_cover(B, "02 - Bare.flac") is None


def t_dangling_entry_falls_back():
    image(os.path.join(B, "03 - X.png"))
    save_track_covers(B, {"03 - X.flac": "gone.jpg"})
    got = get_track_cover(B, "03 - X.flac")
    assert got == os.path.join(B, "03 - X.png"), got
    # ...and the undecodable entry is still there: resolution ignores it, it is
    # not silently rewritten behind the caller's back.
    assert load_track_covers(B) == {"03 - X.flac": "gone.jpg"}


check("stem probe still resolves without a manifest", t_stem_probe_without_manifest)
check("no art at all resolves to None", t_none_without_art)
check("a dangling entry falls back to the stem probe", t_dangling_entry_falls_back)

# --------------------------------------------------------------------------- #
# 3. empty mapping removes the file
# --------------------------------------------------------------------------- #
print("== cleanup ==")
C = album("Empty")
image(os.path.join(C, "05 - E.jpg"))


def t_empty_mapping_deletes_file():
    set_track_covers(C, ["05 - E.flac", "06 - F.flac"], "05 - E.jpg")
    assert os.path.isfile(os.path.join(C, TRACK_COVERS_FILE))
    save_track_covers(C, {})
    assert not os.path.exists(os.path.join(C, TRACK_COVERS_FILE)), "manifest left behind"
    assert load_track_covers(C) == {}
    # clearing the last entry is the same thing
    set_track_covers(C, ["05 - E.flac"], "05 - E.jpg")
    clear_track_covers(C, ["05 - e.FLAC"])
    assert not os.path.exists(os.path.join(C, TRACK_COVERS_FILE)), "manifest left behind"
    assert os.path.isfile(os.path.join(C, "05 - E.jpg")), "the image must survive"


def t_clear_single_track_keeps_others():
    set_track_covers(C, ["05 - E.flac", "06 - F.flac"], "05 - E.jpg")
    out = clear_track_covers(C, ["06 - F.flac"])
    assert out == {"05 - E.flac": "05 - E.jpg"}, out
    assert load_track_covers(C) == out


check("an empty mapping removes the manifest file", t_empty_mapping_deletes_file)
check("clearing one track keeps the others", t_clear_single_track_keeps_others)

if FLAC is None:
    print("SKIP: no flac.exe found in .dependencies or PATH")
    sys.exit(2)
if not HAS_PIL:
    print("SKIP: Pillow is not installed")
    sys.exit(2)

# --------------------------------------------------------------------------- #
# 4. HTTP surface
# --------------------------------------------------------------------------- #
print("== endpoints ==")
from fastapi import HTTPException  # noqa: E402
from server import main as srv  # noqa: E402

CFG = {"music_folder": MUSIC, "cover_target_size": 1200, "naming_script": ""}
srv.load_config = lambda: dict(CFG)

REAL = REAL_MUSIC_FOLDER.replace("\\", "/").rstrip("/").lower()
# Only meaningful when there IS a configured music folder to stay away from: a
# checkout without config.json (CI) reads "" here, and every path starts with
# "" — which would fail a fixture that is in fact hermetic.
if REAL:
    assert not MUSIC.replace("\\", "/").lower().startswith(REAL), \
        f"temp fixture {MUSIC} sits inside the real music folder"


class Upload:
    """Minimal stand-in for fastapi's UploadFile (filename + async read)."""

    def __init__(self, data, filename="image.png"):
        self.data = data
        self.filename = filename

    async def read(self):
        return self.data


def expect_400(fn, label):
    try:
        fn()
    except HTTPException as e:
        assert e.status_code == 400, f"{label}: {e.status_code} {e.detail}"
        return
    raise AssertionError(f"{label}: expected HTTPException(400)")


H = album("Http")
for n in ("07 - A.flac", "08 - B.flac", "09 - C.flac"):
    make_flac(os.path.join(H, n), f"Song {n[0:2]}", int(n[:2]))
image(os.path.join(H, "cover.jpg"))


def t_upload_tracks_writes_once():
    res = asyncio.run(srv.upload_cover(
        album=H, file=Upload(image_bytes()), track=None,
        tracks="07 - A.flac,08 - B.flac"))
    assert res["ok"] is True, res
    assert res["path"].endswith("/07 - A.png"), res["path"]
    assert res["width"] == 828 and res["height"] == 828, res
    assert res["megapixels"] == round(828 * 828 / 1_000_000, 2), res
    assert res["below_target"] is True and res["target"] == 1200, res
    assert res["warning"] == (
        "828×828 is below the minimum 1200×1200 — grading will flag this cover"), res
    # ONE new image file, named after the first selected track
    assert images_in(H) == ["07 - A.png", "cover.jpg"], images_in(H)
    m = load_track_covers(H)
    assert m == {"07 - A.flac": "07 - A.png", "08 - B.flac": "07 - A.png"}, m
    assert get_track_cover(H, "09 - C.flac") is None


def t_upload_tracks_rejects_bad_selection():
    expect_400(lambda: asyncio.run(srv.upload_cover(
        album=H, file=Upload(image_bytes()), track=None, tracks="07 - A.flac,nope.flac")),
        "unknown track")
    expect_400(lambda: asyncio.run(srv.upload_cover(
        album=H, file=Upload(image_bytes()), track=None, tracks=",")),
        "empty selection")
    expect_400(lambda: asyncio.run(srv.upload_cover(
        album=H, file=Upload(b"definitely not an image"), track=None, tracks=None)),
        "non-image upload")


def t_upload_single_track_unchanged():
    res = asyncio.run(srv.upload_cover(
        album=H, file=Upload(image_bytes((1200, 1200))), track="09 - C.flac",
        tracks=None))
    assert res["ok"] is True and res["path"].endswith("/09 - C.png"), res
    assert res["warning"] is None, res
    assert res["width"] == 1200 and res["height"] == 1200, res
    assert os.path.isfile(os.path.join(H, "09 - C.png"))
    assert "09 - C.flac" not in load_track_covers(H), "single-track writes no map"


def t_upload_album_cover_unchanged():
    res = asyncio.run(srv.upload_cover(
        album=H, file=Upload(image_bytes((1200, 1200)), filename="front.png"),
        track=None, tracks=None))
    assert res["ok"] is True and res["path"].endswith("/cover.png"), res


def t_upload_format_pillow_cannot_read():
    # JXL/HEIC covers reach the app as plain bytes; a Pillow build without the
    # plugin must not turn them into a 400.
    jxl = b"\xff\x0a" + b"\x00" * 64
    res = asyncio.run(srv.upload_cover(
        album=H, file=Upload(jxl, filename="art.jxl"), track=None, tracks=None))
    assert res["ok"] is True and res["path"].endswith("/cover.jxl"), res
    assert res["width"] is None and res["warning"] is None, res


def t_fromurl_tracks():
    orig = srv.intg.fetch_image_bytes
    srv.intg.fetch_image_bytes = lambda url: (image_bytes(), "image/png")
    try:
        res = asyncio.run(srv.cover_from_url(
            album=H, url="http://example.invalid/x.png", track=None,
            tracks="08 - B.flac,09 - C.flac"))
    finally:
        srv.intg.fetch_image_bytes = orig
    assert res["ok"] is True and res["path"].endswith("/08 - B.png"), res
    assert res["below_target"] is True and res["target"] == 1200, res
    assert res["warning"] == (
        "828×828 is below the minimum 1200×1200 — grading will flag this cover"), res
    m = load_track_covers(H)
    assert m["08 - B.flac"] == "08 - B.png" and m["09 - C.flac"] == "08 - B.png", m
    assert images_in(H) == ["07 - A.png", "08 - B.png", "09 - C.png", "cover.jpg",
                            "cover.jxl", "cover.png"], images_in(H)


def t_clear_endpoint():
    req = srv.CoverClearRequest(album=H, tracks=["08 - B.flac"])
    assert srv.cover_clear(req) == {"ok": True}
    m = load_track_covers(H)
    assert "08 - B.flac" not in m and m["07 - A.flac"] == "07 - A.png", m
    assert os.path.isfile(os.path.join(H, "08 - B.png")), "clearing must not delete art"
    # no tracks -> drop them all and remove the manifest, image still on disk
    assert srv.cover_clear(srv.CoverClearRequest(album=H)) == {"ok": True}
    assert load_track_covers(H) == {}
    assert not os.path.exists(os.path.join(H, TRACK_COVERS_FILE))
    assert os.path.isfile(os.path.join(H, "07 - A.png")), "clearing must not delete art"


check("POST /api/cover?tracks= writes once + warns", t_upload_tracks_writes_once)
check("POST /api/cover rejects empty/unknown selections", t_upload_tracks_rejects_bad_selection)
check("POST /api/cover?track= behaviour is unchanged", t_upload_single_track_unchanged)
check("POST /api/cover without a track writes cover.*", t_upload_album_cover_unchanged)
check("an image format Pillow cannot decode still uploads", t_upload_format_pillow_cannot_read)
check("POST /api/cover/fromurl accepts tracks=", t_fromurl_tracks)
check("POST /api/cover/clear drops entries, keeps the image", t_clear_endpoint)

# --------------------------------------------------------------------------- #
# 5. consumers: grading + the library payload
# --------------------------------------------------------------------------- #
print("== consumers ==")
from mlo.grader import _grade_album  # noqa: E402
from server import library as lib_mod  # noqa: E402

G = os.path.join(MUSIC, "Artists", "Cover Artist", "2020 - Cover Album")
os.makedirs(G, exist_ok=True)
make_flac(os.path.join(G, "07 - A.flac"), "Song A", 7)
make_flac(os.path.join(G, "08 - B.flac"), "Song B", 8)
image(os.path.join(G, "cover.jpg"))
SHARED = image(os.path.join(G, "tracks 7 and 8.jpg"))  # matches NO track stem
GRADE_CFG = dict(DEFAULT_CONFIG, music_folder=MUSIC, lyrics_format="EMBEDDED")


def _issues(res):
    return set(res["issues"] if isinstance(res["issues"], dict) else [])


def _sidecar_issues(res):
    return {i for i in _issues(res) if i.startswith("Sidecar cover")}


def t_grader_accepts_shared_image():
    set_track_covers(G, ["07 - A.flac", "08 - B.flac"], "tracks 7 and 8.jpg")
    res = _grade_album(G, "EMBEDDED", GRADE_CFG)
    assert not _sidecar_issues(res), _sidecar_issues(res)
    assert not any(i.startswith("Extra artwork") for i in _issues(res)), _issues(res)
    by_file = {t["file"]: t for t in res["tracks"]}
    for name in ("07 - A.flac", "08 - B.flac"):
        tr = by_file[name]
        assert tr["sidecar_cover_file"] == "tracks 7 and 8.jpg", (name, tr["sidecar_cover_file"])
        assert tr["sidecar_cover"] == os.path.join(G, "tracks 7 and 8.jpg"), tr["sidecar_cover"]


def t_grader_without_manifest_sees_a_stray():
    clear_track_covers(G)
    assert os.path.isfile(SHARED), "clearing must not delete the image"
    res = _grade_album(G, "EMBEDDED", GRADE_CFG)
    assert any(i.startswith("Extra artwork") for i in _issues(res)), _issues(res)
    by_file = {t["file"]: t for t in res["tracks"]}
    assert by_file["07 - A.flac"]["sidecar_cover_file"] is None
    assert by_file["08 - B.flac"]["sidecar_cover_file"] is None


def t_library_enrich_uses_manifest():
    set_track_covers(G, ["07 - A.flac", "08 - B.flac"], "tracks 7 and 8.jpg")
    tr = {"file": "08 - B.flac"}
    lib_mod._enrich_track(tr, G)
    assert tr["cover_file"] == "tracks 7 and 8.jpg", tr.get("cover_file")
    bare = {"file": "09 - Missing.flac"}
    lib_mod._enrich_track(bare, G)
    assert bare["cover_file"] is None and "cover_file" in bare, bare


def t_cover_check_is_aspect_ratio_not_crop():
    """grade_check_cover_crop gates an ASPECT-RATIO (squareness) test — there
    is no crop detection in the grader, and the issue text says so. The
    toggle must switch it off for the album cover and for sidecar covers."""
    from mlo.grader import _cover_image_ok

    ratio_cfg = dict(GRADE_CFG, cover_enforce_size=False,
                     cover_resize_enabled=False, cover_force_exact_size=False,
                     cover_enforce_square=True)
    wide = image(os.path.join(G, "wide.png"), (1200, 900))
    assert not _cover_image_ok(wide, ratio_cfg), "a non-square image fails"
    assert _cover_image_ok(wide, dict(ratio_cfg, grade_check_cover_crop=False)), \
        "grade_check_cover_crop=False stops grading the aspect ratio (sidecar path)"

    ratio_dir = os.path.join(MUSIC, "Artists", "Cover Artist", "Ratio Album")
    make_flac(os.path.join(ratio_dir, "01 - A.flac"), "Song A", 1)
    image(os.path.join(ratio_dir, "cover.jpg"), (1200, 900))
    issues = _issues(_grade_album(ratio_dir, "EMBEDDED", ratio_cfg))
    assert any("aspect ratio" in i and "not square" in i for i in issues), issues
    assert not any("crop" in i for i in issues), issues
    issues = _issues(_grade_album(ratio_dir, "EMBEDDED",
                                  dict(ratio_cfg, grade_check_cover_crop=False)))
    assert not any("aspect ratio" in i for i in issues), issues


check("grading accepts one image shared by two tracks", t_grader_accepts_shared_image)
check("without the manifest the same image is stray artwork",
      t_grader_without_manifest_sees_a_stray)
check("library _enrich_track resolves through the manifest", t_library_enrich_uses_manifest)
check("the cover check grades the ASPECT RATIO, not a crop heuristic",
      t_cover_check_is_aspect_ratio_not_crop)

# --------------------------------------------------------------------------- #
# 6. organize(): the manifest follows the album, stale entries are dropped
# --------------------------------------------------------------------------- #
print("== organize ==")
OLD = os.path.join(MUSIC, "Organize Artist", "Old Folder")
os.makedirs(OLD, exist_ok=True)
make_flac(os.path.join(OLD, "01 - Song A.flac"), "Song A", 1)
make_flac(os.path.join(OLD, "02 - Song B.flac"), "Song B", 2)
image(os.path.join(OLD, "01 - Song A.jpg"))
save_track_covers(OLD, {
    "01 - Song A.flac": "01 - Song A.jpg",
    "02 - Song B.flac": "01 - Song A.jpg",
    "03 - Gone.flac": "02 - Ghost.jpg",       # both sides missing -> dropped
})


def t_organize_rewrites_manifest():
    res = srv.organize(type("R", (), {"paths": [OLD], "dry_run": False}))["results"][0]
    assert res.get("ok"), res
    new_root = res["album_root"].replace("/", os.sep)
    assert not os.path.isdir(OLD), "old album dir still exists"
    m = load_track_covers(new_root)
    flacs = sorted(f for f in os.listdir(new_root) if f.lower().endswith(".flac"))
    jpgs = sorted(f for f in os.listdir(new_root) if f.lower().endswith(".jpg"))
    assert len(flacs) == 2 and len(jpgs) == 1, (flacs, jpgs)
    assert m == {flacs[0]: jpgs[0], flacs[1]: jpgs[0]}, (m, flacs, jpgs)
    assert "03 - Gone.flac" not in m, m
    assert get_track_cover(new_root, flacs[1]) == os.path.join(new_root, jpgs[0])
    assert os.path.isfile(os.path.join(new_root, TRACK_COVERS_FILE))


check("organize follows the sidecar rename and drops stale entries",
      t_organize_rewrites_manifest)

print()
if FAILED:
    print(f"FAILED {len(FAILED)}: {', '.join(FAILED)}")
    sys.exit(1)
print("All track-cover checks passed.")
