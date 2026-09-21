#!/usr/bin/env python3
"""The album-cover preview: the URL the UI's <img> builds, and what the server
answers for it (server.main's cover routes + server.tagcache's byte cache).

What this pins, over a temp library with a real (if silent) album and a second
album OUTSIDE the music folder — the import wizard's staged album:

  * a staged album's cover is served ONLY when the request carries the wizard's
    `staged=true` allowance: without it the folder guard answers 400. The
    preview URL the UI builds therefore has to carry it, or the "<img>" is
    empty even though the cover was written fine;
  * the bytes served are the bytes on DISK, straight after the same request
    wrote them — never a stale cover from the in-process cache (the cache is
    keyed by the file's mtime+size, so a rewrite is a miss);
  * every cover write reports a `token` (the written file's mtime+size) that
    CHANGES when the bytes change, which is what lets the client turn a
    replaced cover into a different URL instead of reusing the cached one. That
    token is carried in the URL's `v` query and must not disturb the answer;
  * the ETag follows the bytes (a rewrite is a new ETag), If-None-Match is
    honoured against it, and the response tells an HTTP cache to REVALIDATE
    (`no-cache`) — so a cover whose file name did not change can never be
    served stale, and an unchanged one still costs a 304;
  * the album payload names the freshly written cover, so the page's own cover
    slot has something to render after the write.

Run:  python tools/test_cover_preview.py
Exit 0 = pass, 2 = skip (no fastapi/httpx, so no TestClient).
"""
import os
import shutil
import struct
import sys
import tempfile
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    from fastapi.testclient import TestClient
except Exception as e:                                   # pragma: no cover
    print(f"SKIP: TestClient unavailable ({e})")
    raise SystemExit(2)

from server import main as mlo_main                      # noqa: E402  (heavy)
from server import tagcache                              # noqa: E402

# --------------------------------------------------------------------------- #
# Temp library: one album inside the music folder (the album page's case) and
# one OUTSIDE it (the import wizard's staged case — the one that used to stay
# blank, because the preview URL did not carry the staged allowance).
# --------------------------------------------------------------------------- #
TMP = tempfile.mkdtemp(prefix="mlo-cover-preview-")
MUSIC = os.path.join(TMP, "music")
ALBUM = os.path.join(MUSIC, "Artists", "Preview Artist", "2020 - Preview Album")
OUTSIDE = os.path.join(TMP, "downloads", "Preview Album")
for d in (ALBUM, OUTSIDE, os.path.join(MUSIC, ".mlo", "data")):
    os.makedirs(d)

CFG = {"music_folder": MUSIC, "cover_target_size": 8,
       "cover_resize_enabled": False, "cover_crop_enabled": False,
       "cover_jpeg_quality": 90}
mlo_main.load_config = lambda *a, **k: dict(CFG)

# A real (if silent) MP3 frame sequence: an album with no audio file is not an
# album at all, and /api/album would 404 instead of naming its cover.
_MP3_FRAME = bytes([0xFF, 0xFB, 0x90, 0x00]) + b"\x00" * 413
for _alb in (ALBUM, OUTSIDE):
    with open(os.path.join(_alb, "01 - Song.mp3"), "wb") as f:
        f.write(_MP3_FRAME * 40)

client = TestClient(mlo_main.app)


def png(w, h, rgb):
    """A valid PNG of one colour, built by hand — no Pillow needed either to
    make it or to upload it (the writer sniffs the container magic)."""
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag, body):
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def upload(alb, data, staged=False, name="upload.png"):
    return client.post("/api/cover", params={"album": alb, "staged": str(bool(staged)).lower()},
                       files={"file": (name, data, "image/png")})


def fetch(alb, file=None, staged=False, params=None, headers=None):
    q = {"album": alb}
    if file:
        q["file"] = file
    if staged:
        q["staged"] = "true"
    q.update(params or {})
    return client.get("/api/cover", params=q, headers=headers)


def on_disk(path):
    with open(path, "rb") as f:
        return f.read()


def cover_file(alb, staged=False):
    r = client.get("/api/album", params={"path": alb, "staged": str(bool(staged)).lower()})
    assert r.status_code == 200, (alb, r.status_code, r.text)
    return r.json()["cover_file"]


# --------------------------------------------------------------------------- #
# 1) The staged album: the cover IS served, but only for a request that asks
#    for the wizard's allowance — which is what the preview URL must carry.
# --------------------------------------------------------------------------- #
assert cover_file(OUTSIDE, staged=True) is None, "the staged album starts without art"
res = upload(OUTSIDE, png(2, 2, (200, 30, 30)), staged=True)
assert res.status_code == 200, res.text
body = res.json()
written = os.path.abspath(body["path"].replace("/", os.sep))
assert os.path.isfile(written), written
assert os.path.dirname(written) == os.path.abspath(OUTSIDE), written
name = os.path.basename(written)

# The wizard writes the cover with staged=true and the preview must read it
# back the same way: that is exactly the pair that used to disagree.
served = fetch(OUTSIDE, name, staged=True)
assert served.status_code == 200, (served.status_code, served.text)
assert served.content == on_disk(written), "the just-written cover is not what is served"
assert served.headers["content-type"].startswith("image/"), served.headers
# …and a library-facing call stays exactly as strict as it was: the fix is the
# client asking for the staged album, never a relaxed guard.
strict = fetch(OUTSIDE, name, staged=False)
assert strict.status_code == 400, (strict.status_code, strict.text)
assert "outside music folder" in strict.text

# --------------------------------------------------------------------------- #
# 2) The write reports a token that changes with the bytes, and the URL's own
#    `v` (where the client puts it) never changes what is served.
# --------------------------------------------------------------------------- #
assert body.get("token"), body
token1, etag1 = body["token"], served.headers["etag"]
assert token1 == f"{os.stat(written).st_mtime_ns}-{os.stat(written).st_size}", token1

res2 = upload(OUTSIDE, png(3, 3, (20, 90, 210)), staged=True)
assert res2.status_code == 200, res2.text
body2 = res2.json()
written2 = os.path.abspath(body2["path"].replace("/", os.sep))
assert written2 == written, (written2, written)      # same name, new bytes
new_bytes = on_disk(written2)
assert new_bytes, written2
assert body2["token"] != token1, (body2["token"], token1)

# The fresh token: the newly written bytes.
fresh = fetch(OUTSIDE, name, staged=True, params={"v": body2["token"]})
assert fresh.status_code == 200, fresh.text
assert fresh.content == new_bytes, "the replaced cover did not come back"
assert fresh.headers["etag"] != etag1, (fresh.headers.get("etag"), etag1)
# The STALE token (a page still holding the previous one) must not resurrect
# the old image: the origin is the only authority, and it has the new bytes.
stale = fetch(OUTSIDE, name, staged=True, params={"v": token1})
assert stale.status_code == 200, (stale.status_code, stale.text)
assert stale.content == new_bytes, "a stale token served the old cover"

# --------------------------------------------------------------------------- #
# 3) Revalidate, and the ETag is the answer: an unchanged cover is a 304, a
#    changed one is the new bytes — never a cached image outliving its file.
# --------------------------------------------------------------------------- #
assert "no-cache" in fresh.headers.get("cache-control", ""), fresh.headers
again = fetch(OUTSIDE, name, staged=True, headers={"If-None-Match": fresh.headers["etag"]})
assert again.status_code == 304, (again.status_code, again.text)
assert again.content == b"", again.content
old = fetch(OUTSIDE, name, staged=True, headers={"If-None-Match": etag1})
assert old.status_code == 200 and old.content == new_bytes, (old.status_code, old.content)

# --------------------------------------------------------------------------- #
# 4) An album inside the music folder (the album page's case): same story, and
#    the payload the page renders from names the file that was just written.
# --------------------------------------------------------------------------- #
assert cover_file(ALBUM) is None
res3 = upload(ALBUM, png(2, 2, (10, 200, 10)))
assert res3.status_code == 200, res3.text
album_cover = os.path.basename(res3.json()["path"].replace("/", os.sep))
assert cover_file(ALBUM) == album_cover, (cover_file(ALBUM), album_cover)
inb = fetch(ALBUM, params={"v": res3.json()["token"]})
assert inb.status_code == 200, inb.text
assert inb.content == on_disk(os.path.join(ALBUM, album_cover))
assert fetch(ALBUM).status_code == 200        # no file= : the album cover itself

res4 = upload(ALBUM, png(4, 4, (240, 240, 20)))
assert res4.status_code == 200, res4.text
after = fetch(ALBUM)
assert after.status_code == 200, after.text
assert after.content == on_disk(os.path.join(ALBUM, album_cover))
assert after.content != inb.content, "the in-process cache served the replaced cover"
assert res4.json()["token"] != res3.json()["token"]
# …and the shared byte cache agrees with the disk, not with its own past.
cached = tagcache.cover_bytes(ALBUM)
assert cached[0] == after.content, "tagcache still holds the replaced cover"
assert cached[2] == after.headers["etag"].strip('"'), (cached[2], after.headers["etag"])

print("cover preview: all checks passed")
shutil.rmtree(TMP, ignore_errors=True)
