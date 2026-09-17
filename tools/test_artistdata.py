#!/usr/bin/env python3
"""Artist artwork + descriptions stored inside the library (mlo/artistdata.py).

Temp library only: <tmp>/music/Artists/<Artist>/... with its own
<tmp>/music/.mlo/data, so the configured music folder is never touched.

Run: python tools/test_artistdata.py
Exit 0 = pass, 1 = failure, 2 = skip (no Pillow).
"""
import io
import json
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

TMP = tempfile.mkdtemp(prefix="mlo-artistdata-")
MUSIC = os.path.join(TMP, "music")
ARTIST = os.path.join(MUSIC, "Artists", "Some Artist")
ALBUM = os.path.join(ARTIST, "2001 - An Album")
os.makedirs(ALBUM)
os.makedirs(os.path.join(MUSIC, ".mlo", "data"))
CFG = {"music_folder": MUSIC, "artist_image_crop": False,
       "artist_image_target_size": 0, "cover_jpeg_quality": 90}

FAILS = []


def check(label, cond, extra=""):
    print(f"{'ok  ' if cond else 'FAIL'} {label}{(' — ' + str(extra)) if extra and not cond else ''}")
    if not cond:
        FAILS.append(label)


def png_bytes(w, h, color=(10, 120, 200)):
    img = ad.Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def size_of(path):
    with ad.Image.open(path) as img:
        return img.size


try:
    # --- artist_dir: case-insensitive resolve, refuses separators ------------
    got = ad.artist_dir(CFG, "some artist")
    check("artist_dir case-insensitive", got == ARTIST, got)
    check("artist_dir unknown -> None", ad.artist_dir(CFG, "Nobody") is None)
    check("artist_dir refuses '/'", ad.artist_dir(CFG, "Some/Artist") is None)
    check("artist_dir refuses '..'", ad.artist_dir(CFG, "..") is None)
    check("artist_dir refuses 'a\\\\b'", ad.artist_dir(CFG, "Some\\Artist") is None)

    # --- save_image: crop 2:1 -> square, then target 400 ---------------------
    cfg400 = dict(CFG, artist_image_crop=True, artist_image_target_size=400)
    p = ad.save_image(ARTIST, png_bytes(800, 400), cfg400, source="deezer",
                      source_url="https://example.test/a", title="Some Artist")
    check("save_image returns artist.jpg", p is not None and p.endswith("artist.jpg"), p)
    check("crop+target -> 400x400", size_of(p) == (400, 400), size_of(p))
    check("has_image", ad.has_image(ARTIST) and ad.image_path(ARTIST) == p)
    with ad.Image.open(p) as img:
        check("jpeg output format", img.format == "JPEG", img.format)

    # --- native size kept when target is 0 (no upscale of a 300px source) ----
    p = ad.save_image(ARTIST, png_bytes(300, 300), CFG, source="deezer")
    check("target 0 keeps native 300x300", size_of(p) == (300, 300), size_of(p))
    p = ad.save_image(ARTIST, png_bytes(300, 300), cfg400, source="deezer")
    check("small source is never upscaled", size_of(p) == (300, 300), size_of(p))

    # --- crop off keeps the 2:1 aspect --------------------------------------
    p = ad.save_image(ARTIST, png_bytes(600, 300), CFG, source="deezer")
    check("crop off keeps aspect", size_of(p) == (600, 300), size_of(p))

    # --- png variant replaces the jpg, and vice versa ------------------------
    p = ad.save_image(ARTIST, png_bytes(300, 300), CFG, fmt="png", source="deezer")
    check("fmt=png writes artist.png", os.path.basename(p) == "artist.png"
          and os.path.isfile(os.path.join(ARTIST, "artist.png")), p)
    check("saving png removes artist.jpg",
          not os.path.isfile(os.path.join(ARTIST, "artist.jpg")))
    p = ad.save_image(ARTIST, png_bytes(300, 300), CFG, source="deezer")
    check("saving jpg replaces artist.png",
          os.path.basename(p) == "artist.jpg"
          and not os.path.isfile(os.path.join(ARTIST, "artist.png")), p)
    check("only one artist image on disk",
          len([f for f in os.listdir(ARTIST) if f.lower().startswith("artist.")]) == 1)
    prov_artist = ad.read_provenance(ARTIST, cfg=CFG)
    check("image provenance kind/source",
          prov_artist.get("kind") == "artist"
          and prov_artist.get("source") == "deezer", prov_artist)
    check("image provenance keeps fetched + updated",
          bool(prov_artist.get("fetched")) and bool(prov_artist.get("updated")), prov_artist)

    # --- junk bytes rejected, folder absent -> None --------------------------
    check("non-image bytes rejected", ad.save_image(ARTIST, b"not an image", CFG) is None)
    check("missing folder -> None",
          ad.save_image(os.path.join(ARTIST, "nope"), png_bytes(10, 10), CFG) is None)
    check("clear_image", ad.clear_image(ARTIST) and not ad.has_image(ARTIST))

    # --- segment_artist_image -------------------------------------------------
    check("segment unknown artist -> None",
          ad.segment_artist_image(CFG, "Nobody", png_bytes(50, 50)) is None)
    p = ad.segment_artist_image(CFG, "some artist", png_bytes(50, 50), source="lastfm")
    check("segment saves into the resolved folder",
          p is not None and os.path.dirname(p) == ARTIST, p)
    ad.clear_image(ARTIST)

    # --- descriptions ---------------------------------------------------------
    check("write_description rejects blank",
          ad.write_description(ARTIST, "   \n\n \t ") == "")
    desc = ad.write_description(ARTIST, "Line one  \n\n\n\nLine two\n\n",
                                cfg=CFG, source="lastfm",
                                source_url="https://example.test/bio", kind="artist")
    check("write_description returns path", desc.endswith("description.txt"), desc)
    check("description_path", ad.description_path(ARTIST) == desc)
    check("description normalized",
          ad.read_description(ARTIST) == "Line one\n\nLine two\n",
          repr(ad.read_description(ARTIST)))
    check("has_description", ad.has_description(ARTIST))
    check("blank write kept the old text",
          ad.read_description(ARTIST) == "Line one\n\nLine two\n")
    check("delete_description", ad.delete_description(ARTIST, cfg=CFG)
          and not ad.has_description(ARTIST))
    check("read_description on missing -> ''", ad.read_description(ARTIST) == "")
    check("delete_description twice -> False", ad.delete_description(ARTIST, cfg=CFG) is False)

    # --- provenance -----------------------------------------------------------
    ad.write_description(ALBUM, "A record.", cfg=CFG, source="rym", kind="album")
    prov = ad.read_provenance(ALBUM, cfg=CFG)
    check("album provenance kind", prov.get("kind") == "album", prov)
    check("album provenance source", prov.get("source") == "rym", prov)
    check("provenance has updated stamp", bool(prov.get("updated")), prov)
    path = os.path.join(MUSIC, ".mlo", "data", "artwork.json")
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    key = ad._norm_key(ALBUM)
    check("artwork.json holds the lowercased folder key", key in raw, sorted(raw))
    check("artwork.json entry matches", raw[key]["source"] == "rym", raw[key])
    check("cfg-less read finds the same map",
          ad.read_provenance(ALBUM).get("source") == "rym",
          ad.read_provenance(ALBUM))
    ad.delete_description(ALBUM, kind="album", cfg=CFG)
    check("delete_description drops provenance",
          ad.read_provenance(ALBUM, cfg=CFG) == {}, raw)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'FAILURES: ' + ', '.join(FAILS) if FAILS else 'all checks passed'}")
raise SystemExit(1 if FAILS else 0)
