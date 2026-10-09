#!/usr/bin/env python3
"""Artist image / description fetches (scripts 25 / 26).

The two fetch scripts walk the library's artist folders and fetch ONLY what a
folder is missing; the force flag OVERWRITES. The discovery layer
(server.discovery) and the byte fetcher (server.integrations.fetch_image_bytes)
are STUBBED here, so the pass is proved without a network and without touching
any real library: a scratch music folder, a folder with nothing in it, and a
provider that answers with a known image / text.

Proves, against the REAL runners (server.script_runners.run_fetch_artist_images
/ run_fetch_artist_descriptions, the same functions /api/run calls):
  (a) a MISSING asset is fetched and written;
  (b) an EXISTING one is left byte-identical while force is off;
  (c) force ON overwrites it (with the NEW bytes, not a re-save of the old);
  (d) a disabled feature key fetches nothing, and the provider is never asked.

Run:  python tools/test_artist_fetch.py   (exit 0 pass, 1 fail, 2 cannot run)
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

from server import discovery, integrations  # noqa: E402
from server import script_runners as sr  # noqa: E402

TMP = tempfile.mkdtemp(prefix="mlo-artist-fetch-")
MUSIC = os.path.join(TMP, "music")
ARTISTS = os.path.join(MUSIC, "Artists")
ARTIST = os.path.join(ARTISTS, "Low Artist")
OTHER = os.path.join(ARTISTS, "Other Artist")
for folder in (os.path.join(ARTIST, "Album (2020)"),):
    os.makedirs(folder)
os.makedirs(os.path.join(MUSIC, ".mlo", "data"))
with open(os.path.join(ARTIST, "Album (2020)", "01 - Song.flac"), "wb") as fh:
    fh.write(b"x")

CFG = {"music_folder": MUSIC}
FAILS = []


def check(label, cond, extra=""):
    print(f"{'ok  ' if cond else 'FAIL'} {label}"
          f"{(' — ' + str(extra)) if extra and not cond else ''}")
    if not cond:
        FAILS.append(label)


def image_bytes(colour, w=800, h=800, fmt="JPEG"):
    buf = io.BytesIO()
    ad.Image.new("RGB", (w, h), colour).save(buf, fmt)
    return buf.getvalue()


IMAGE_A = image_bytes((10, 120, 200))     # the first fetch's picture
IMAGE_B = image_bytes((210, 40, 60))      # the re-fetch's, visibly different
FETCH = {"bytes": IMAGE_A}
CALLS = {"image": 0, "description": 0}
TEXT = {"text": "A band from nowhere.\n"}

# The stubbed providers. `fetch_artist_image` does `from server import discovery`
# at call time, so patching the module attributes here is what the runner sees.
def stub_artist_image(name, mbid=None, cfg=None):
    CALLS["image"] += 1
    return {"url": "https://stub.invalid/a.jpg", "source": "stub", "label": "Stub photo"}


def stub_artist_description(name, mbid=None, cfg=None):
    CALLS["description"] += 1
    return {"text": TEXT["text"], "source": "stub",
            "source_url": "https://stub.invalid/a", "title": name}


discovery.artist_image = stub_artist_image
discovery.artist_description = stub_artist_description
integrations.fetch_image_bytes = lambda url, timeout=60.0: (FETCH["bytes"], "image/jpeg")


def read(path):
    with open(path, "rb") as fh:
        return fh.read()


def average_colour(path):
    with ad.Image.open(path) as img:
        r, g, b = img.convert("RGB").resize((1, 1)).getpixel((0, 0))
    return (r, g, b)


def near(got, want, tol=30):
    return all(abs(a - b) <= tol for a, b in zip(got, want))


try:
    # ------------------------------------------------------------------ #
    # (a) a MISSING asset is fetched and written
    # ------------------------------------------------------------------ #
    print("== (a) a missing asset is fetched ==")
    stats = sr.run_fetch_artist_images(dict(CFG))
    img = ad.image_path(ARTIST)
    check("25 writes artist.jpg into the empty artist folder",
          img is not None and os.path.basename(img) == "artist.jpg", img)
    check("25 counts the folder as modified, and it as scanned",
          stats["modified_count"] == 1 and stats["total_scanned"] == 1, stats)
    check("25 reports no error", stats["error_count"] == 0, stats["errors"])

    stats = sr.run_fetch_artist_descriptions(dict(CFG))
    check("26 writes description.txt",
          ad.has_description(ARTIST), repr(ad.read_description(ARTIST)))
    check("26 counts the folder as modified",
          stats["modified_count"] == 1 and stats["total_scanned"] == 1, stats)

    # ------------------------------------------------------------------ #
    # (b) an EXISTING asset is left byte-identical while force is off
    # ------------------------------------------------------------------ #
    print("== (b) an existing asset is left alone (force off) ==")
    before_img = read(img)
    before_desc = read(ad.description_path(ARTIST))
    CALLS["image"] = CALLS["description"] = 0
    FETCH["bytes"] = IMAGE_B          # a re-fetch would store something else

    stats = sr.run_fetch_artist_images(dict(CFG))
    check("25 leaves the stored image byte-identical",
          read(ad.image_path(ARTIST)) == before_img)
    check("25 does not even ask the provider for a present image",
          CALLS["image"] == 0, CALLS)
    check("25 counts the folder as already there",
          stats["unchanged_count"] == 1 and stats["modified_count"] == 0, stats)

    stats = sr.run_fetch_artist_descriptions(dict(CFG))
    check("26 leaves the stored description byte-identical",
          read(ad.description_path(ARTIST)) == before_desc)
    check("26 counts the folder as already there",
          stats["unchanged_count"] == 1 and stats["modified_count"] == 0, stats)

    # ------------------------------------------------------------------ #
    # (c) force ON overwrites
    # ------------------------------------------------------------------ #
    print("== (c) force ON overwrites ==")
    stats = sr.run_fetch_artist_images({**CFG, "force_artist_image": True})
    check("25 force ON rewrites the image", read(ad.image_path(ARTIST)) != before_img)
    check("...with the provider's NEW picture (not a re-save of the old)",
          near(average_colour(ad.image_path(ARTIST)), (210, 40, 60)),
          average_colour(ad.image_path(ARTIST)))
    check("25 force ON reports the write",
          stats["modified_count"] == 1 and stats["unchanged_count"] == 0, stats)

    TEXT["text"] = "A different bio entirely.\n"
    stats = sr.run_fetch_artist_descriptions({**CFG, "force_artist_description": True})
    check("26 force ON rewrites the description",
          read(ad.description_path(ARTIST)) != before_desc
          and "different bio" in ad.read_description(ARTIST),
          repr(ad.read_description(ARTIST)))
    check("26 force ON reports the write", stats["modified_count"] == 1, stats)

    # ------------------------------------------------------------------ #
    # (d) a disabled feature key fetches nothing
    # ------------------------------------------------------------------ #
    print("== (d) the feature switch off means nothing fetched ==")
    # A second artist folder, with nothing in it, appears only now: the runs
    # above must have seen one folder for their counts to mean one.
    os.makedirs(os.path.join(OTHER, "Album"))
    CALLS["image"] = CALLS["description"] = 0
    stats = sr.run_fetch_artist_images({**CFG, "artist_image_enabled": False})
    check("25 with artist_image_enabled off writes nothing",
          not ad.has_image(OTHER) and stats["modified_count"] == 0, (CALLS, stats))
    check("...and never asks a provider", CALLS["image"] == 0, CALLS)

    stats = sr.run_fetch_artist_descriptions({**CFG, "artist_description_enabled": False})
    check("26 with artist_description_enabled off writes nothing",
          not ad.has_description(OTHER) and stats["modified_count"] == 0, (CALLS, stats))
    check("...and never asks a provider", CALLS["description"] == 0, CALLS)

    # ------------------------------------------------------------------ #
    # a TARGETED run touches only the artist the selection sits in
    # ------------------------------------------------------------------ #
    print("== a targeted run is scoped to its selection ==")
    stats = sr.run_fetch_artist_images({**CFG, "targets": [os.path.join(OTHER, "Album")]})
    check("the selected artist's folder is fetched",
          ad.has_image(OTHER) and stats["modified_count"] == 1, stats)
    check("the other artist is not touched again",
          stats["total_scanned"] == 1, stats)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'FAILURES: ' + ', '.join(FAILS) if FAILS else 'all checks passed'}")
raise SystemExit(1 if FAILS else 0)