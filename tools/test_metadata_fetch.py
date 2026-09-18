#!/usr/bin/env python3
"""The import menu's provider step, offline (server.api_discovery +
server.main's scan route).

Two surfaces the import wizard reads, both pinned here against a temp library
and stubbed providers — no network, no real app run:

  * ``ensure_artist_album_metadata`` (POST /api/album/metadata/fetch): one
    entry per item with the honest state — ``fetched`` (and the file/description
    actually written), ``present`` on the second call with NOTHING rewritten,
    ``disabled`` when the item's feature is off while its siblings still run,
    ``not-found`` when no provider answers, ``error`` for the one item whose
    provider blew up;
  * ``GET /api/album/scan-tracks``: the lyrics flags the wizard's per-track
    steps read, computed by the same detection the grader uses — embedded
    lyrics, an .lrc sidecar, neither — and absent from a plain scan.

The temp library is <tmp>/music/Artists/<Artist [mbid]>/<Album>, so the real
music folder is never touched and the artist-folder lookup is exercised with
the naming script's MBID suffix.

Run: python tools/test_metadata_fetch.py
Exit 0 = pass, 2 = skip (no Pillow, so no artist image can be stored).
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mlo import artistdata  # noqa: E402
from mlo.audio import AudioFile  # noqa: E402
from server import api_discovery, artcache, discovery  # noqa: E402
from server import main as mlo_main  # noqa: E402  (heavy import)
from fastapi.testclient import TestClient  # noqa: E402

if not artistdata.HAS_PIL:
    print("SKIP: Pillow not installed")
    raise SystemExit(2)

TMP = tempfile.mkdtemp(prefix="mlo-metadata-")
MUSIC = os.path.join(TMP, "music")
MBID = "a16371b9-2c4f-4d3a-9f0e-2b7a5c1d8e3f"
# The naming script's folder shape: a name plus the artist's MBID, so the
# step's own `artist_dir` lookup is what has to strip the suffix.
ARTIST = os.path.join(MUSIC, "Artists", f"Meta Artist [{MBID}]")
ALBUM = os.path.join(ARTIST, "2020 - Meta Album")
OUTSIDE = tempfile.mkdtemp(prefix="mlo-metadata-outside-")
os.makedirs(ALBUM)
os.makedirs(os.path.join(MUSIC, ".mlo", "data"))

CFG = {"music_folder": MUSIC, "artist_image_crop": False,
       "artist_image_target_size": 0, "cover_jpeg_quality": 90}

# A real (if silent) MP3 frame sequence: the album's identity is read from the
# tracks' tags, and a junk byte string would be an unreadable file.
_MP3_FRAME = bytes([0xFF, 0xFB, 0x90, 0x00]) + b"\x00" * 413


def write_mp3(path, tags):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(_MP3_FRAME * 40)
    af = AudioFile(path)
    for key, value in tags.items():
        assert af.set_tag(key, value), (path, key)
    return path


TRACK = write_mp3(os.path.join(ALBUM, "1-01 Song.mp3"),
                  {"ALBUMARTIST": "Meta Artist", "ALBUM": "Meta Album",
                   "TITLE": "Song"})

mlo_main.load_config = lambda *a, **k: dict(CFG)
api_discovery.load_config = lambda *a, **k: dict(CFG)
client = TestClient(mlo_main.app)   # no lifespan: no workers, no slskd boot

# --------------------------------------------------------------------------- #
# Stubbed providers — each call is logged, so "asked again" is testable
# --------------------------------------------------------------------------- #
CALLS = []
ARTIST_IMAGE = {"url": "https://providers.test/artist.jpg", "source": "deezer",
                "label": "Meta Artist"}
ARTIST_TEXT = {"text": "A band from nowhere.", "source": "wikipedia",
               "source_url": "https://providers.test/bio", "title": "Meta Artist"}
ALBUM_TEXT = {"text": "A record of note.", "source": "rym",
              "source_url": "https://providers.test/album", "title": "Meta Album"}

_image_buf = io.BytesIO()
artistdata.Image.new("RGB", (80, 80), (12, 34, 56)).save(_image_buf, "PNG")


def _img_bytes():
    return _image_buf.getvalue()


def stub_providers(artist_image=ARTIST_IMAGE, artist_description=ARTIST_TEXT,
                   album_description=ALBUM_TEXT):
    """Answer like the three provider chains do (None = nothing found)."""

    def call(what, value, *args):
        CALLS.append((what,) + args)
        if isinstance(value, Exception):
            raise value
        return value

    discovery.artist_image = lambda name, mbid=None, cfg=None: call(
        "artist_image", artist_image, name)
    discovery.artist_description = lambda name, mbid=None, cfg=None: call(
        "artist_description", artist_description, name)
    discovery.album_description = lambda artist, album, mbid=None, cfg=None: call(
        "album_description", album_description, artist, album)


def stub_fetch_art():
    """The download step: the bytes the image provider's URL serves."""
    artcache.fetch_art = lambda url, artist=None, cfg=None: (_img_bytes(),
                                                             "image/png", url)


def reset():
    """Forget everything the last step stored: files, provenance, call log."""
    del CALLS[:]
    artistdata.clear_image(ARTIST)
    artistdata.delete_description(ARTIST, kind="artist", cfg=CFG)
    artistdata.delete_description(ALBUM, kind="album", cfg=CFG)


stub_providers()
stub_fetch_art()

# --------------------------------------------------------------------------- #
# 1) Enabled + providers answering: fetched, source recorded, files written
# --------------------------------------------------------------------------- #
reset()
out = api_discovery.ensure_artist_album_metadata(ALBUM, CFG)
assert set(out) == set(api_discovery.META_ITEMS), out
assert {i: r["state"] for i, r in out.items()} == {
    "artist_image": "fetched", "artist_description": "fetched",
    "album_description": "fetched"}, out
# The item that answered says WHICH provider answered.
assert out["artist_image"]["source"] == "deezer", out["artist_image"]
assert out["artist_description"]["source"] == "wikipedia", out["artist_description"]
assert out["album_description"]["source"] == "rym", out["album_description"]
# ... and every provider was asked with the artist's NAME, MBID suffix gone.
assert CALLS == [("artist_image", "Meta Artist"),
                 ("artist_description", "Meta Artist"),
                 ("album_description", "Meta Artist", "Meta Album")], CALLS

# The bytes landed: a real artist image, in the MBID-suffixed folder, whose
# pixels are the ones the provider served (the store re-encodes, so the file is
# not byte-identical to the response).
assert artistdata.has_image(ARTIST), os.listdir(ARTIST)
with artistdata.Image.open(artistdata.image_path(ARTIST)) as img:
    assert img.size == (80, 80), img.size            # the served size
    got_rgb = img.convert("RGB").resize((1, 1)).getpixel((0, 0))
    # JPEG is lossy, so compare the average colour rather than a raw pixel.
    assert all(abs(got - want) <= 8 for got, want in zip(got_rgb, (12, 34, 56))), got_rgb
prov = artistdata.read_provenance(ARTIST, cfg=CFG)
assert prov.get("source") == "deezer", prov
assert prov.get("source_url") == ARTIST_IMAGE["url"], prov
# The descriptions landed with their own provenance — the text's provider must
# not rename the IMAGE's `source` in the entry the two share.
assert artistdata.read_description(ALBUM) == "A record of note.\n", \
    repr(artistdata.read_description(ALBUM))
album_prov = artistdata.read_provenance(ALBUM, cfg=CFG)
assert album_prov.get("description_source") == "rym", album_prov
assert album_prov.get("kind") == "album", album_prov
assert artistdata.read_description(ARTIST) == "A band from nowhere.\n"
artist_prov = artistdata.read_provenance(ARTIST, cfg=CFG)
assert artist_prov.get("description_source") == "wikipedia", artist_prov
assert artist_prov.get("source") == "deezer", artist_prov   # the image's own

# --------------------------------------------------------------------------- #
# 2) Second call: nothing is fetched and NOTHING is rewritten
# --------------------------------------------------------------------------- #
IMAGE_PATH = artistdata.image_path(ARTIST)
ARTIST_DESC = artistdata.description_path(ARTIST)
ALBUM_DESC = artistdata.description_path(ALBUM)
before = {p: (os.path.getmtime(p), os.path.getsize(p)) for p in
          (IMAGE_PATH, ARTIST_DESC, ALBUM_DESC)}
before_bytes = {p: open(p, "rb").read() for p in before}
del CALLS[:]
again = api_discovery.ensure_artist_album_metadata(ALBUM, CFG)
assert {i: r["state"] for i, r in again.items()} == {
    "artist_image": "present", "artist_description": "present",
    "album_description": "present"}, again
# "present — deezer": who stored it is part of accounting for it.
assert again["artist_image"]["source"] == "deezer", again["artist_image"]
assert again["artist_description"]["source"] == "wikipedia", again["artist_description"]
assert again["album_description"]["source"] == "rym", again["album_description"]
assert CALLS == [], CALLS          # not one provider was asked again
after = {p: (os.path.getmtime(p), os.path.getsize(p)) for p in before}
assert after == before, (before, after)
assert {p: open(p, "rb").read() for p in before} == before_bytes

# --------------------------------------------------------------------------- #
# 3) One feature off: that item is `disabled`, its siblings still run
# --------------------------------------------------------------------------- #
reset()
stub_providers()
off = dict(CFG, artist_description_enabled=False)
part = api_discovery.ensure_artist_album_metadata(ALBUM, off)
assert part["artist_description"]["state"] == "disabled", part
assert "Settings" in str(part["artist_description"]["detail"]), part
# The other two still did their work ...
assert part["artist_image"]["state"] == "fetched", part
assert part["album_description"]["state"] == "fetched", part
assert artistdata.has_image(ARTIST) and artistdata.has_description(ALBUM)
# ... and the switched-off one was not even asked, nor written.
assert [c[0] for c in CALLS] == ["artist_image", "album_description"], CALLS
assert artistdata.read_description(ARTIST) == "", "no artist description was stored"
# A switched-off artist IMAGE stays off too, without touching the text items.
reset()
img_off = dict(CFG, artist_image_enabled=False)
part = api_discovery.ensure_artist_album_metadata(ALBUM, img_off)
assert part["artist_image"]["state"] == "disabled", part
assert not artistdata.has_image(ARTIST)
assert part["artist_description"]["state"] == "fetched", part
assert part["album_description"]["state"] == "fetched", part

# --------------------------------------------------------------------------- #
# 4) Providers answering nothing: not-found, never fetched, nothing written
# --------------------------------------------------------------------------- #
reset()
stub_providers(artist_image=None, artist_description=None, album_description=None)
empty = api_discovery.ensure_artist_album_metadata(ALBUM, CFG)
assert {i: r["state"] for i, r in empty.items()} == {
    "artist_image": "not-found", "artist_description": "not-found",
    "album_description": "not-found"}, empty
assert len(CALLS) == 3, CALLS      # all three were asked ...
assert not artistdata.has_image(ARTIST) and not artistdata.has_description(ARTIST)
assert not artistdata.has_description(ALBUM), "nothing may be invented"

# --------------------------------------------------------------------------- #
# 5) A provider that raises: `error` for that item only
# --------------------------------------------------------------------------- #
reset()
stub_providers(album_description=RuntimeError("provider exploded"))
buf = io.StringIO()
with contextlib.redirect_stderr(buf):
    raised = api_discovery.ensure_artist_album_metadata(ALBUM, CFG)   # never raises
assert raised["album_description"]["state"] == "error", raised
assert "provider exploded" in str(raised["album_description"]["detail"]), raised
# The failure is reported, not swallowed: the traceback reaches the log.
assert "provider exploded" in buf.getvalue(), buf.getvalue()
assert not artistdata.has_description(ALBUM)
assert raised["artist_image"]["state"] == "fetched", raised
assert raised["artist_description"]["state"] == "fetched", raised
assert artistdata.has_image(ARTIST) and artistdata.has_description(ARTIST)

# --------------------------------------------------------------------------- #
# 6) The route: the same step, guarded like every other path route
# --------------------------------------------------------------------------- #
reset()
stub_providers()
r = client.post("/api/album/metadata/fetch", json={"path": ALBUM})
assert r.status_code == 200, r.text
body = r.json()
assert body["ok"] is True and set(body["items"]) == set(api_discovery.META_ITEMS), body
assert {i: v["state"] for i, v in body["items"].items()} == {
    "artist_image": "fetched", "artist_description": "fetched",
    "album_description": "fetched"}, body["items"]
assert os.path.isfile(os.path.join(ALBUM, "description.txt")), os.listdir(ALBUM)
# A second POST is the wizard's re-run: everything is already there.
r = client.post("/api/album/metadata/fetch", json={"path": ALBUM})
assert all(v["state"] == "present" for v in r.json()["items"].values()), r.text
# Outside the music folder -> 400, and no provider is consulted.
outside_track = write_mp3(os.path.join(OUTSIDE, "1-01 Out.mp3"),
                          {"ALBUMARTIST": "Meta Artist", "ALBUM": "Meta Album"})
del CALLS[:]
r = client.post("/api/album/metadata/fetch", json={"path": OUTSIDE})
assert r.status_code == 400, r.text
assert r.json()["detail"] == "path outside music folder", r.text
assert CALLS == [], CALLS
# A missing folder is a 404, not a step that reports three errors.
r = client.post("/api/album/metadata/fetch", json={"path": os.path.join(MUSIC, "nope")})
assert r.status_code == 404, r.text

# --------------------------------------------------------------------------- #
# 7) scan-tracks: the lyrics state the wizard's per-track steps read
# --------------------------------------------------------------------------- #
# Three tracks in one album: embedded lyrics, only a sidecar, neither. The
# flags must come from the same detection the grader uses.
LYRIC_ALBUM = os.path.join(MUSIC, "Artists", "Lyric Artist", "2020 - Lyric Album")
embedded = write_mp3(os.path.join(LYRIC_ALBUM, "1-01 Embedded.mp3"),
                     {"ALBUMARTIST": "Lyric Artist", "ALBUM": "Lyric Album"})
AudioFile(embedded).set_lyrics("first line\nsecond line")
sidecar = write_mp3(os.path.join(LYRIC_ALBUM, "1-02 Sidecar.mp3"),
                    {"ALBUMARTIST": "Lyric Artist", "ALBUM": "Lyric Album"})
with open(os.path.splitext(sidecar)[0] + ".lrc", "w", encoding="utf-8") as fh:
    fh.write("sidecar line one\nsidecar line two\n")
write_mp3(os.path.join(LYRIC_ALBUM, "1-03 Bare.mp3"),
          {"ALBUMARTIST": "Lyric Artist", "ALBUM": "Lyric Album"})

r = client.get("/api/album/scan-tracks", params={"path": LYRIC_ALBUM})
assert r.status_code == 200, r.text
flags = {t["file"]: (t["lyrics_embedded"], t["lyrics_lrc"], t["lyrics_present"])
         for t in r.json()["tracks"]}
assert flags == {"1-01 Embedded.mp3": (True, False, True),
                 "1-02 Sidecar.mp3": (False, True, True),
                 "1-03 Bare.mp3": (False, False, False)}, flags
# The scan is otherwise unchanged: the wizard still gets its tags and tech.
assert r.json()["tracks"][0]["tags"].get("ALBUM") == "Lyric Album", r.json()["tracks"][0]

# The default scan carries no lyrics fields at all: the detection costs a
# grader pass per folder, so it is opt-in.
plain = mlo_main._scan_album_tracks(LYRIC_ALBUM)
assert plain and all("lyrics_present" not in t for t in plain), plain
assert all("lyrics_embedded" not in t and "lyrics_lrc" not in t for t in plain), plain

# Same containment guard as the metadata route.
r = client.get("/api/album/scan-tracks", params={"path": OUTSIDE})
assert r.status_code == 400, r.text
assert r.json()["detail"] == "folder outside music folder", r.text

shutil.rmtree(TMP, ignore_errors=True)
shutil.rmtree(OUTSIDE, ignore_errors=True)
print("metadata fetch + scan tracks: all assertions passed")
