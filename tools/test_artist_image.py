#!/usr/bin/env python3
"""Artist images: what mlo.grader's artist image check reports, and script 19
(mlo.artistdata.run_optimize_artist_images) as the pass that clears it.

The check judges the DECODED file — its size against the configured ceiling,
its ratio against `artist_image_aspect`, its container, whether it decodes at
all, and whether the pixels were enlarged after this app wrote it — so every
fixture here is a real image written straight to disk, the way a provider, an
older release or a hand-placed file leaves one. Nothing is accepted on the
strength of a file name.

The loop is proved both ways round: an image that fails is fixed by the script
and then passes, and an image that already conforms is left byte for byte
alone on a second run.

Run: python tools/test_artist_image.py  (exit 0 pass, 1 fail, 2 skipped)
"""
import io
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import mlo.artistdata as ad  # noqa: E402

if not ad.HAS_PIL:
    print("SKIP: Pillow not installed")
    raise SystemExit(2)

from mlo.grader import grade_artist  # noqa: E402

TMP = tempfile.mkdtemp(prefix="mlo-artist-image-")
MUSIC = os.path.join(TMP, "music")
ARTIST = os.path.join(MUSIC, "Artists", "Some Artist")
os.makedirs(ARTIST)
os.makedirs(os.path.join(MUSIC, ".mlo", "data"))
# An artist folder is only graded while it holds an album folder — one with
# nothing but the artist's own image and description is the ARTIST_EMPTY case
# (tools/test_artist_grading.py pins that on its own folder). This fixture is
# about the IMAGE, so it holds one.
os.makedirs(os.path.join(ARTIST, "Album (2020)"))
with open(os.path.join(ARTIST, "Album (2020)", "01 - Song.flac"), "wb") as fh:
    fh.write(b"x")
# The description check is not what this file is about; keeping it satisfied
# means every result below is the IMAGE check's own verdict.
with open(os.path.join(ARTIST, "description.txt"), "w", encoding="utf-8") as fh:
    fh.write("A band from nowhere.\n")

# The policy under test: square, 1200 px, cropped, JPEG at the cover quality.
CFG = {"music_folder": MUSIC, "artist_image_crop": True,
       "artist_image_aspect": "1:1", "artist_image_target_size": 1200,
       "cover_crop_enabled": True, "cover_jpeg_quality": 90}

FAILS = []


def check(label, cond, extra=""):
    print(f"{'ok  ' if cond else 'FAIL'} {label}{(' — ' + str(extra)) if extra and not cond else ''}")
    if not cond:
        FAILS.append(label)


def image_bytes(w, h, fmt="JPEG"):
    buf = io.BytesIO()
    ad.Image.new("RGB", (w, h), (10, 120, 200)).save(buf, fmt)
    return buf.getvalue()


def place(w, h, fmt="JPEG", name=None):
    """Write a w×h image straight into the artist folder — no fetch involved.

    Writing a file by hand also replaces the size the app recorded for it (a
    provider write does both), so no fix below is capped by a previous
    fixture's own dimensions."""
    ext = ".jpg" if fmt == "JPEG" else "." + fmt.lower()
    path = os.path.join(ARTIST, name or ("artist" + ext))
    ad.clear_image(ARTIST)
    for other in os.listdir(ARTIST):  # a .webp fixture is not artist.* to clear_image
        if other.lower().startswith("artist."):
            os.remove(os.path.join(ARTIST, other))
    ad._drop_provenance(ARTIST, CFG)
    with open(path, "wb") as fh:
        fh.write(image_bytes(w, h, fmt))
    return path


def size_of(path):
    with ad.Image.open(path) as img:
        return img.size


def codes(res):
    return [i["code"] for i in res["issues"]]


def reason_of(res, code):
    return next((i.get("reason", "") for i in res["issues"] + res["notes"]
                 if i["code"] == code), "")


def read(path):
    with open(path, "rb") as fh:
        return fh.read()


def fix(cfg=None, path=None):
    return ad.optimize_artist_image(ARTIST, cfg or CFG, path=path)


try:
    # ------------------------------------------------------------------ #
    # the policy the check and the fix share
    # ------------------------------------------------------------------ #
    print("== policy ==")
    check("parse_aspect reads the configured forms",
          ad.parse_aspect("1:1") == 1.0 and ad.parse_aspect("4x5") == 0.8
          and ad.parse_aspect("16/9") is not None
          and abs(ad.parse_aspect("16:9") - 16 / 9) < 1e-9,
          [ad.parse_aspect(v) for v in ("1:1", "4x5", "16/9")])
    check("parse_aspect rejects what is not a ratio",
          ad.parse_aspect("") is None and ad.parse_aspect("square") is None
          and ad.parse_aspect("1:0") is None and ad.parse_aspect("1:2:3") is None
          and ad.parse_aspect("1.7778") is None,
          [ad.parse_aspect(v) for v in ("", "square", "1:0", "1:2:3", "1.7778")])
    check("the shipped default is 1:1", ad.DEFAULT_ASPECT == "1:1"
          and ad.parse_aspect(ad.DEFAULT_ASPECT) == 1.0)
    check("image_policy reads the settings",
          ad.image_policy(CFG) == (1.0, ad.ASPECT_TOLERANCE, 1200, 1200),
          ad.image_policy(CFG))
    check("no target size means the documented ceiling",
          ad.image_policy(dict(CFG, artist_image_target_size=0))
          == (1.0, ad.ASPECT_TOLERANCE, 0, ad.DEFAULT_MAX_SIDE),
          ad.image_policy(dict(CFG, artist_image_target_size=0)))
    check("crop off means no aspect is enforced",
          ad.aspect_policy(dict(CFG, artist_image_crop=False)) is None)
    check("a configured 16:9 is what the policy hands over",
          ad.aspect_policy(dict(CFG, artist_image_aspect="16:9")) == 16 / 9)

    # The setting is real: a shipped default, a validator that keeps an
    # unusable saved value out, and a registry entry so MAINTAIN → Checks &
    # scripts shows the codes this check can raise.
    from mlo.config import DEFAULT_CONFIG, normalize_config
    check("artist_image_aspect ships as 1:1",
          DEFAULT_CONFIG["artist_image_aspect"] == "1:1",
          DEFAULT_CONFIG.get("artist_image_aspect"))
    check("a real saved aspect survives normalization",
          normalize_config({"artist_image_aspect": "4:5"})["artist_image_aspect"] == "4:5")
    check("an unusable saved aspect falls back to the shipped default",
          normalize_config({"artist_image_aspect": "square"})["artist_image_aspect"] == "1:1")
    try:
        from server.api_stack import _gate_codes
        claimed = set(_gate_codes().get("grade_check_artist_image", ()))
    except Exception as e:  # fastapi absent: the cross-module claim cannot be read
        claimed = set()
        print(f"  skip: server.api_stack unavailable ({e})")
    if claimed:
        check("the check registry claims every artist image code",
              claimed >= {"ARTIST_IMAGE_MISSING", "ARTIST_IMAGE_CORRUPT",
                          "ARTIST_IMAGE_FORMAT", "ARTIST_IMAGE_OVERSIZED",
                          "ARTIST_IMAGE_ASPECT", "ARTIST_IMAGE_UPSCALED",
                          "ARTIST_IMAGE_UNDERSIZED"},
              sorted(claimed))

    # ------------------------------------------------------------------ #
    # grading: each verdict names the numbers it judged
    # ------------------------------------------------------------------ #
    print("== grading ==")

    place(1200, 1200)
    res = grade_artist(ARTIST, CFG)
    check("a conforming image passes both checks",
          res["pass"] is True and res["issues"] == [] and res["notes"] == [],
          res)

    place(600, 600)  # below the 1200 px target
    res = grade_artist(ARTIST, CFG)
    check("undersized PASSES", res["pass"] is True and codes(res) == [],
          res["issues"])
    check("undersized is reported as a note naming both numbers",
          codes(res) == [] and "ARTIST_IMAGE_UNDERSIZED" in [n["code"] for n in res["notes"]]
          and "600" in reason_of(res, "ARTIST_IMAGE_UNDERSIZED")
          and "1200" in reason_of(res, "ARTIST_IMAGE_UNDERSIZED"),
          res["notes"])

    place(3000, 3000)
    res = grade_artist(ARTIST, CFG)
    check("oversized FAILS", res["pass"] is False
          and codes(res) == ["ARTIST_IMAGE_OVERSIZED"], res["issues"])
    check("oversized names the image's size and the target",
          "3000" in reason_of(res, "ARTIST_IMAGE_OVERSIZED")
          and "1200" in reason_of(res, "ARTIST_IMAGE_OVERSIZED"),
          reason_of(res, "ARTIST_IMAGE_OVERSIZED"))

    place(2500, 2500)
    res = grade_artist(ARTIST, dict(CFG, artist_image_target_size=0))
    check("with no target size the ceiling still fails 2500px",
          codes(res) == ["ARTIST_IMAGE_OVERSIZED"]
          and str(ad.DEFAULT_MAX_SIDE) in reason_of(res, "ARTIST_IMAGE_OVERSIZED")
          and "2500" in reason_of(res, "ARTIST_IMAGE_OVERSIZED"),
          reason_of(res, "ARTIST_IMAGE_OVERSIZED"))

    place(1200, 675)  # 1.778:1, under the size ceiling
    res = grade_artist(ARTIST, CFG)
    check("a wrong aspect FAILS (and nothing else does)",
          codes(res) == ["ARTIST_IMAGE_ASPECT"], res["issues"])
    aspect_reason = reason_of(res, "ARTIST_IMAGE_ASPECT")
    check("the aspect reason names both ratios and the delta",
          "1.778" in aspect_reason and "1:1" in aspect_reason
          and "off" in aspect_reason and "2%" in aspect_reason,
          aspect_reason)
    res_off = grade_artist(ARTIST, dict(CFG, artist_image_crop=False))
    check("with cropping off the same image passes (no aspect enforced)",
          res_off["pass"] is True and codes(res_off) == [], res_off["issues"])

    place(1200, 675)
    res = grade_artist(ARTIST, dict(CFG, artist_image_aspect="16:9"))
    check("the CONFIGURED aspect is what is judged (16:9 passes 1.778:1)",
          res["pass"] is True and codes(res) == [], res["issues"])

    corrupt = place(1200, 1200)
    with open(corrupt, "wb") as fh:
        fh.write(b"not an image at all")
    res = grade_artist(ARTIST, CFG)
    check("undecodable bytes FAIL as corrupt",
          codes(res) == ["ARTIST_IMAGE_CORRUPT"], res["issues"])
    check("the corrupt reason says how many bytes it read",
          f"{os.path.getsize(corrupt)} bytes" in reason_of(res, "ARTIST_IMAGE_CORRUPT"),
          reason_of(res, "ARTIST_IMAGE_CORRUPT"))

    place(1200, 1200)
    open(os.path.join(ARTIST, "artist.jpg"), "wb").close()  # zero bytes
    res = grade_artist(ARTIST, CFG)
    check("a zero-byte artist.jpg is corrupt, not missing",
          codes(res) == ["ARTIST_IMAGE_CORRUPT"]
          and "0 bytes" in reason_of(res, "ARTIST_IMAGE_CORRUPT"),
          res["issues"])

    # an image this app wrote, enlarged afterwards by something else
    ad.clear_image(ARTIST)
    saved = ad.save_image(ARTIST, image_bytes(500, 500, "PNG"), CFG)
    ad.Image.open(io.BytesIO(image_bytes(1000, 1000))).save(saved, "JPEG", quality=90)
    check("the fixture really is twice the size this app stored",
          size_of(saved) == (1000, 1000)
          and ad.recorded_size(ARTIST, CFG) == (500, 500),
          (size_of(saved), ad.recorded_size(ARTIST, CFG)))
    res = grade_artist(ARTIST, CFG)
    check("an image enlarged after the app wrote it is flagged",
          "ARTIST_IMAGE_UPSCALED" in codes(res), res["issues"])
    check("the upscale reason names both sizes and the factor",
          "1000x1000" in reason_of(res, "ARTIST_IMAGE_UPSCALED")
          and "500x500" in reason_of(res, "ARTIST_IMAGE_UPSCALED")
          and "2.00x" in reason_of(res, "ARTIST_IMAGE_UPSCALED"),
          reason_of(res, "ARTIST_IMAGE_UPSCALED"))

    # a real image in a container the library does not read
    webp_ok = True
    try:
        place(1200, 1200, "WEBP", name="artist.webp")
    except Exception as e:  # a Pillow built without WEBP support
        webp_ok = False
        print(f"  skip: WEBP unsupported by this Pillow ({e})")
    if webp_ok:
        res = grade_artist(ARTIST, CFG)
        check("artist.webp is a FORMAT failure, not a missing image",
              codes(res) == ["ARTIST_IMAGE_FORMAT"]
              and "artist.webp" in reason_of(res, "ARTIST_IMAGE_FORMAT"),
              res["issues"])

    # a folder with nothing at all still says MISSING
    ad.clear_image(ARTIST)
    for name in os.listdir(ARTIST):
        if name.lower().startswith("artist."):
            os.remove(os.path.join(ARTIST, name))
    res = grade_artist(ARTIST, CFG)
    check("no image at all is still ARTIST_IMAGE_MISSING",
          codes(res) == ["ARTIST_IMAGE_MISSING"], res["issues"])

    # ------------------------------------------------------------------ #
    # the fix: failing -> run script 19 -> passing
    # ------------------------------------------------------------------ #
    print("== the fix (oversized) ==")
    big = place(3000, 3000)
    res = grade_artist(ARTIST, CFG)
    check("fixture fails before the fix", codes(res) == ["ARTIST_IMAGE_OVERSIZED"],
          res["issues"])
    stats = ad.run_optimize_artist_images(CFG)
    res = grade_artist(ARTIST, CFG)
    check("the script run reports one re-encoded image",
          stats["modified_count"] == 1 and stats["error_count"] == 0, stats)
    check("3000x3000 -> 1200x1200 after the fix", size_of(big) == (1200, 1200),
          size_of(big))
    check("and the check now passes", res["pass"] is True and res["issues"] == [],
          res["issues"])

    first = read(big)
    again = fix()
    check("a second run changes nothing (idempotent)",
          again["changed"] is False and read(big) == first, again)

    print("== the fix (wrong aspect) ==")
    wrong = place(1200, 675)
    check("fixture fails before the fix",
          codes(grade_artist(ARTIST, CFG)) == ["ARTIST_IMAGE_ASPECT"])
    out = fix()
    check("the fix reports before -> after",
          out["changed"] is True and out["before"] == (1200, 675)
          and out["after"] == (675, 675) and "675x675" in out["reason"],
          out)
    res = grade_artist(ARTIST, CFG)
    check("1200x675 -> 675x675 and the aspect check passes",
          size_of(wrong) == (675, 675) and res["pass"] is True
          and codes(res) == [], (size_of(wrong), res["issues"]))

    print("== the fix (a non-square configured aspect) ==")
    cfg169 = dict(CFG, artist_image_aspect="16:9")
    square = place(1000, 1000)
    check("a square image fails the configured 16:9",
          codes(grade_artist(ARTIST, cfg169)) == ["ARTIST_IMAGE_ASPECT"])
    out = fix(cfg169)
    expected = (1000, int(round(1000 / (16 / 9))))
    check("the fix crops to 16:9, not to a square",
          out["changed"] is True and out["after"] == expected, out)
    check("and the 16:9 check passes", grade_artist(ARTIST, cfg169)["pass"] is True
          and size_of(square) == expected, size_of(square))

    print("== the fix (already good, never upscales, corrupt, format) ==")
    good = place(1200, 1200)
    before_bytes = read(good)
    out = fix()
    check("an image that already conforms is left alone",
          out["changed"] is False and read(good) == before_bytes, out)

    small = place(300, 300)
    out = fix()
    check("a 300px image is never upscaled",
          out["changed"] is False and size_of(small) == (300, 300), out)

    corrupt = place(1200, 1200)
    with open(corrupt, "wb") as fh:
        fh.write(b"not an image")
    out = fix(path=corrupt)
    check("a corrupt file is reported and kept, never deleted",
          out["error"] and out["changed"] is False
          and os.path.isfile(corrupt) and read(corrupt) == b"not an image", out)

    if webp_ok:
        webp = place(1200, 1200, "WEBP", name="artist.webp")
        out = fix()
        check("a wrong-container image is converted in place",
              out["changed"] is True
              and os.path.basename(out["path"]) == "artist.jpg"
              and not os.path.isfile(webp)
              and size_of(out["path"]) == (1200, 1200), out)
        check("and the format check passes",
              grade_artist(ARTIST, CFG)["pass"] is True)

    print("== the fix (an enlarged image is brought back to the stored size) ==")
    ad.clear_image(ARTIST)
    saved = ad.save_image(ARTIST, image_bytes(500, 500, "PNG"), CFG)
    ad.Image.open(io.BytesIO(image_bytes(1000, 1000))).save(saved, "JPEG", quality=90)
    check("fixture is flagged as upscaled",
          "ARTIST_IMAGE_UPSCALED" in codes(grade_artist(ARTIST, CFG)))
    out = fix(path=saved)
    check("the fix drops it back to the size the app stored",
          out["changed"] is True and out["after"] == (500, 500)
          and size_of(saved) == (500, 500), out)
    check("and the flag is gone", grade_artist(ARTIST, CFG)["pass"] is True)

    # ------------------------------------------------------------------ #
    # the script's own scope: only artist folders, targets narrow it
    # ------------------------------------------------------------------ #
    print("== script scope ==")
    other = os.path.join(MUSIC, "Artists", "Other Artist")
    os.makedirs(other, exist_ok=True)
    with open(os.path.join(other, "artist.jpg"), "wb") as fh:
        fh.write(image_bytes(3000, 3000))
    place(1200, 1200, name="artist.jpg")
    stats = ad.run_optimize_artist_images(dict(CFG, targets=[ARTIST]))
    check("a targeted run fixes only the folder it was given",
          stats["total_scanned"] == 1 and stats["modified_count"] == 0
          and size_of(os.path.join(other, "artist.jpg")) == (3000, 3000), stats)
    stats = ad.run_optimize_artist_images(CFG)
    check("a whole-library run fixes the other one",
          stats["total_scanned"] == 2 and stats["modified_count"] == 1
          and size_of(os.path.join(other, "artist.jpg")) == (1200, 1200), stats)
    check("album folders do not enter the scan",
          ad.artist_folders(CFG) == sorted([ARTIST, other]), ad.artist_folders(CFG))

    # the fetch and the fix write the same way: a fetched image already conforms
    ad.clear_image(ARTIST)
    fetched = ad.save_image(ARTIST, image_bytes(800, 400), CFG)
    check("the fetch crops to the configured aspect itself",
          size_of(fetched) == (400, 400), size_of(fetched))
    check("so a fetched image passes the check unchanged",
          grade_artist(ARTIST, CFG)["pass"] is True)
    check("and the fix finds nothing to do",
          fix()["changed"] is False)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'FAILURES: ' + ', '.join(FAILS) if FAILS else 'all checks passed'}")
raise SystemExit(1 if FAILS else 0)
