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
import mlo.paths as paths  # noqa: E402

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
    # The naming script names an artist folder after their MBID ("Slowdive
    # [uuid]", or [...] the 8-char short form); a lookup by NAME must still
    # find it or nothing can store an artist image/description.
    mbid_dir = os.path.join(MUSIC, "Artists", "Mbid Only [a16371b9-2c4f-4d3a-9f0e-2b7a5c1d8e3f]")
    short_dir = os.path.join(MUSIC, "Artists", "Short Name [a16371b9]")
    os.makedirs(mbid_dir)
    os.makedirs(short_dir)
    check("artist_dir finds an MBID-suffixed folder",
          ad.artist_dir(CFG, "Mbid Only") == mbid_dir, ad.artist_dir(CFG, "Mbid Only"))
    check("artist_dir strips the MBID off the queried name",
          ad.artist_dir(CFG, os.path.basename(mbid_dir)) == mbid_dir)
    check("artist_dir finds a short-id folder",
          ad.artist_dir(CFG, "Short Name") == short_dir, ad.artist_dir(CFG, "Short Name"))
    # The same strip has to happen to the QUERIED name: a caller handing back the
    # folder's own name (the UI does) must resolve it even when the id it carries
    # is spelled differently from the one on disk, and when the case differs.
    check("artist_dir strips the queried name's own id form",
          ad.artist_dir(CFG, "Short Name [a16371b9-2c4f-4d3a-9f0e-2b7a5c1d8e3f]") == short_dir,
          ad.artist_dir(CFG, "Short Name [a16371b9-2c4f-4d3a-9f0e-2b7a5c1d8e3f]"))
    check("artist_dir matches a differently-cased MBID folder",
          ad.artist_dir(CFG, "Mbid Only [A16371B9-2C4F-4D3A-9F0E-2B7A5C1D8E3F]") == mbid_dir)
    check("strip_mbid_suffix", ad.strip_mbid_suffix("Slowdive [a16371b9-2c4f-4d3a-9f0e-2b7a5c1d8e3f]") == "Slowdive"
          and ad.strip_mbid_suffix("Slowdive [a16371b9]") == "Slowdive"
          and ad.strip_mbid_suffix("Slowdive") == "Slowdive"
          and ad.strip_mbid_suffix("[Live] Slowdive") == "[Live] Slowdive")
    shutil.rmtree(mbid_dir)
    shutil.rmtree(short_dir)

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
    # The description's provider lives under `description_source`: the entry is
    # shared with the folder's artist image, whose own `source` it must not
    # rename.
    ad.write_description(ALBUM, "A record.", cfg=CFG, source="rym", kind="album")
    prov = ad.read_provenance(ALBUM, cfg=CFG)
    check("album provenance kind", prov.get("kind") == "album", prov)
    check("album provenance source", prov.get("description_source") == "rym", prov)
    check("description did not rename the image source",
          "source" not in prov, prov)
    check("provenance has updated stamp", bool(prov.get("updated")), prov)
    path = os.path.join(MUSIC, ".mlo", "data", "artwork.json")
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    key = ad._norm_key(ALBUM)
    check("artwork.json holds the lowercased folder key", key in raw, sorted(raw))
    check("artwork.json entry matches", raw[key]["description_source"] == "rym", raw[key])
    check("cfg-less read finds the same map",
          ad.read_provenance(ALBUM).get("description_source") == "rym",
          ad.read_provenance(ALBUM))
    ad.delete_description(ALBUM, kind="album", cfg=CFG)
    check("delete_description drops provenance",
          ad.read_provenance(ALBUM, cfg=CFG) == {}, raw)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("\n== a numbered copy is the album's description ==")
# "description (2).txt" is what a file manager leaves when it copies a file
# into a folder that already holds one; the app's own writer REPLACES the file
# atomically, so a copy always arrives from outside. It is still the album's
# description — invisible to a reader that only knows the exact name — and the
# layout scan offers the rename instead of calling the album's own file dead
# weight (mlo.paths.album_sidecar_of, mlo.layout's sidecar_copy finding).
COPY_DIR = os.path.join(TMP, "copy-album")
os.makedirs(COPY_DIR, exist_ok=True)
with io.open(os.path.join(COPY_DIR, "description (2).txt"), "w",
             encoding="utf-8", newline="\n") as fh:
    fh.write("A description a file manager copied.\n")
check("a numbered copy is READ",
      ad.read_description(COPY_DIR).strip() == "A description a file manager copied.",
      ad.description_path(COPY_DIR))
check("…and it is the on-disk name the app resolves to",
      os.path.basename(ad.description_path(COPY_DIR)) == "description (2).txt")
check("…and the family matcher knows the name and its number",
      paths.album_sidecar_of("description (2).TXT") == "description.txt"
      and paths.album_sidecar_copy("description (3).txt") == 3)
# With the canonical file beside it, the canonical one wins — two texts are the
# reader's to sort out, and it must not guess.
with io.open(os.path.join(COPY_DIR, "description.txt"), "w",
             encoding="utf-8", newline="\n") as fh:
    fh.write("The canonical one.\n")
check("the canonical name wins over a copy",
      ad.read_description(COPY_DIR).strip() == "The canonical one.",
      ad.read_description(COPY_DIR))

print(f"\n{'FAILURES: ' + ', '.join(FAILS) if FAILS else 'all checks passed'}")
raise SystemExit(1 if FAILS else 0)
