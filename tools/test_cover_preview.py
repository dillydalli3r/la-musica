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
    slot has something to render after the write;
  * the FINDER's own write on that staged album (the wizard's cover step): an
    album whose folder arrived carrying another album's `cover.jpg` — a peer's
    sidecar no import step clears — keeps showing it UNLESS the request carries
    the wizard's staged allowance, and with it the pick lands on `cover.jpg` and
    is what the preview serves.

Run:  python tools/test_cover_preview.py
Exit 0 = pass, 2 = skip (no fastapi/httpx, so no TestClient).
"""
import base64
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

# A cover write to a LIBRARY album also queues script 5 for that album
# (server.main._schedule_cover_process, spec R56f): background work that holds
# the album's job_locks claim while it runs. Section 4 writes the same album
# twice in a row, so the second write would be refused 409 by the first write's
# own follow-up run ("… is in use by Process images"). What this suite pins is
# the preview URL and the bytes it answers, so the follow-up is stubbed out
# here; that it fires AND holds the album is `tools/test_track_covers.py`'s
# check (and `tools/test_job_locks.py` stubs it for the same reason).
mlo_main._schedule_cover_process = lambda alb: False

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

# --------------------------------------------------------------------------- #
# 5) The finder's own write on the wizard's staged album — the pick must become
#    what the preview serves.
#
#    The album folder arrived with ANOTHER album's cover.jpg (a peer's sidecar:
#    nothing in the import clears a cover FILE — `imports.drop_arrived_values`
#    drops embedded art only — and `run_cover_step` leaves an album that has art
#    alone). Replacing it is the user's own pick, and the request the finder
#    builds must carry the wizard's staged allowance the way every other cover
#    call the wizard makes does: without it the write is refused and the panel
#    below keeps showing the arrived art, however well the row was picked.
#
#    The two JPEGs are inlined so the check needs no Pillow, and both land as
#    cover.jpg — the same in-place replacement the album page does.
# --------------------------------------------------------------------------- #
FOREIGN = os.path.join(TMP, "downloads", "Another Album")
os.makedirs(FOREIGN)
FOREIGN_JPG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsK"
    "CwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRT/2wBDAQMEBAUEBQkFBQkUDQsNFBQUFBQU"
    "FBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBT/wAARCAAIAAgDASIA"
    "AhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQA"
    "AAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3"
    "ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWm"
    "p6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEA"
    "AwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSEx"
    "BhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElK"
    "U1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3"
    "uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD4Pooo"
    "r+9T4o//2Q==")
PICK_JPG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsK"
    "CwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRT/2wBDAQMEBAUEBQkFBQkUDQsNFBQUFBQU"
    "FBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBT/wAARCAAIAAgDASIA"
    "AhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQA"
    "AAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3"
    "ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWm"
    "p6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEA"
    "AwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSEx"
    "BhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElK"
    "U1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3"
    "uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwCpRRRX"
    "85H8VH//2Q==")
assert FOREIGN_JPG != PICK_JPG
with open(os.path.join(FOREIGN, "cover.jpg"), "wb") as f:
    f.write(FOREIGN_JPG)
with open(os.path.join(FOREIGN, "01 - Song.mp3"), "wb") as f:
    f.write(_MP3_FRAME * 40)

# The finder's candidate: a URL the server fetches through the one seam the
# other suites stub (a provider CDN is not reachable from a test).
def cover_info_file(alb):
    r = client.get("/api/cover/info", params={"album": alb, "staged": "true"})
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()["file"]


# 5a) What the panel shows BEFORE anything is applied: the arrived cover. That
#     file IS the album's cover — no import step cleared it, and the cover step
#     leaves an album that already has art alone.
assert cover_file(FOREIGN, staged=True) == "cover.jpg"
arrived = fetch(FOREIGN, "cover.jpg", staged=True)
assert arrived.content == FOREIGN_JPG, "the fixture album starts on another album's art"
assert cover_info_file(FOREIGN) == "cover.jpg"

_orig_fetch = mlo_main.intg.fetch_image_bytes
mlo_main.intg.fetch_image_bytes = lambda url, *a, **k: (PICK_JPG, "image/jpeg")
try:
    # 5b) The finder's write WITHOUT the wizard's allowance — the request the
    #     modal used to send: refused, and the panel keeps the arrived art.
    #     This refusal IS the bug: every "Use this cover" in the wizard's cover
    #     step was answered with this 400, so the pick never reached the folder.
    res5 = client.post("/api/cover/fromurl", params={
        "album": FOREIGN, "url": "http://covers.invalid/pick.jpg",
        "artist": "Some Artist", "title": "Some Album"})
    assert res5.status_code == 400, (res5.status_code, res5.text)
    assert "outside music folder" in res5.text, res5.text
    assert fetch(FOREIGN, "cover.jpg", staged=True).content == FOREIGN_JPG, (
        "the preview changed under a refused write")
    assert not os.path.exists(os.path.join(FOREIGN, "cover.png"))

    # 5c) The same write WITH the wizard's own flag — what the finder sends now
    #     — lands, and IS what the preview serves.
    res6 = client.post("/api/cover/fromurl", params={
        "album": FOREIGN, "url": "http://covers.invalid/pick.jpg",
        "artist": "Some Artist", "title": "Some Album", "staged": "true"})
    assert res6.status_code == 200, (res6.status_code, res6.text)
    landed = os.path.abspath(res6.json()["path"].replace("/", os.sep))
    assert os.path.basename(landed) == "cover.jpg", landed   # replaced in place
    served5 = fetch(FOREIGN, "cover.jpg", staged=True)
    # The writer compresses on the way in (`_compress_cover_bytes`), so "the
    # pick" is the file the write reports — what matters is that the preview
    # serves THAT and not the art the album arrived with.
    assert served5.content == on_disk(landed), "the applied pick is not what the preview serves"
    assert served5.content != FOREIGN_JPG, "the arrived cover is still what the preview serves"
    assert served5.headers["etag"] != arrived.headers["etag"], (
        "the replaced cover reports the same ETag")
    assert cover_info_file(FOREIGN) == "cover.jpg"
finally:
    mlo_main.intg.fetch_image_bytes = _orig_fetch

# --------------------------------------------------------------------------- #
# 6) The SIZED cover — what the surfaces that draw a small one ask for.
#
#    The player bar draws 74 px, the fullscreen picture 448: both used to
#    fetch the master (a 1200-3000 px JPEG, 0.3-3 MB) and let the browser
#    shrink it, which is the round trip the owner measured as "about a second"
#    between pressing play and the artwork. `?w=` serves the file shrunk to
#    that width, encoded ONCE and then read from disk by every later request —
#    which is what these checks pin, because a re-encode per request or a
#    validator that forces a round trip on every play would both bring the
#    latency straight back. The master path (no `w`) must stay exactly as it
#    was: the offline warm stores the full-size file, and nothing here may
#    change what it gets.
# --------------------------------------------------------------------------- #
try:
    from PIL import Image  # noqa: F401 — the SHRINK is Pillow's, server-side
except Exception:
    print("cover preview: sized-cover checks SKIPPED (no Pillow)")
else:
    from io import BytesIO

    from server import artcache

    SIZED = os.path.join(MUSIC, "Artists", "Preview Artist", "2001 - Sized Album")
    os.makedirs(SIZED, exist_ok=True)
    with open(os.path.join(SIZED, "01 - Song.mp3"), "wb") as f:
        f.write(_MP3_FRAME * 40)
    # 900x600, drawn as the bar draws it: the shrink has to keep the SHAPE
    # (the same crop) and land on the asked width.
    big = os.path.join(SIZED, "cover.jpg")
    Image.new("RGB", (900, 600), (12, 90, 200)).save(big, "JPEG", quality=95)
    # The thumbnail cache lives in THIS album's own data dir (the live
    # install's music folder must never be written to by a test).
    thumbs = os.path.join(MUSIC, ".mlo", "data", artcache.thumb_dir().rsplit(os.sep, 1)[-1])
    artcache.thumb_dir = lambda *a, **k: thumbs

    master = fetch(SIZED)
    assert master.status_code == 200, master.text
    assert "no-cache" in master.headers["cache-control"], master.headers
    master_bytes = master.content

    thumb = fetch(SIZED, params={"w": "160"})
    assert thumb.status_code == 200, thumb.text
    assert thumb.headers["content-type"].startswith("image/"), thumb.headers
    drawn = Image.open(BytesIO(thumb.content))
    assert drawn.size == (160, 107), drawn.size          # 900x600 shrunk, same crop
    assert len(thumb.content) < len(master_bytes) // 4, (
        len(thumb.content), len(master_bytes))
    # A sized answer may be KEPT by the browser — that is what makes a repeat
    # play (and a queue row, and the fullscreen pane) cost no round trip at
    # all. The token a cover WRITE reports still lands on a different URL
    # (`api.coverUrl`'s `v`), so an in-app replacement is still refetched.
    assert "max-age=" in thumb.headers["cache-control"], thumb.headers
    assert thumb.headers["etag"] != master.headers["etag"], "the thumb reuses the master's ETag"
    revalidated = fetch(SIZED, params={"w": "160"},
                        headers={"If-None-Match": thumb.headers["etag"]})
    assert revalidated.status_code == 304, (revalidated.status_code, revalidated.text)
    assert revalidated.content == b"", revalidated.content

    def thumb_entries():
        return {n: (os.stat(os.path.join(thumbs, n)).st_mtime_ns,
                    os.stat(os.path.join(thumbs, n)).st_size)
                for n in os.listdir(thumbs) if n.endswith(".bin")}

    first = thumb_entries()
    assert len(first) == 1, first                     # one width asked, one entry
    for _ in range(3):
        again_bytes = fetch(SIZED, params={"w": "160"})
        assert again_bytes.content == thumb.content, "the cached thumbnail changed"
    assert thumb_entries() == first, (
        "the thumbnail was re-encoded (the cache entry was rewritten) per request")

    # A second width is a second entry, and a width BETWEEN the steps lands on
    # the same bucket — otherwise every pixel count the UI computes would be a
    # new encode and the surfaces would stop sharing bytes.
    assert fetch(SIZED, params={"w": "500"}).content == fetch(SIZED, params={"w": "400"}).content
    assert len(thumb_entries()) == 2, thumb_entries()

    # A cover replaced IN PLACE is a new entry, never a stale hit: the key is
    # the file's own stat, not the URL.
    Image.new("RGB", (900, 600), (230, 40, 40)).save(big, "JPEG", quality=95)
    os.utime(big, ns=(0, 0))                          # a write inside one mtime tick
    replaced = fetch(SIZED, params={"w": "160"})
    assert replaced.status_code == 200, replaced.text
    assert replaced.content != thumb.content, "the shrunk cover outlived its file"
    assert replaced.headers["etag"] != thumb.headers["etag"], (
        "the replaced cover reports the same ETag")

    # An image already at or below the asked width is served as ITS OWN bytes:
    # no upscale, no re-encode, no second cache entry — a 700 px cover asked
    # for at 640 stays that 700 px cover.
    small_album_cover = os.path.join(ALBUM, cover_file(ALBUM))
    small_asked = fetch(ALBUM, cover_file(ALBUM), params={"w": "1200"})
    assert small_asked.status_code == 200, small_asked.text
    assert small_asked.content == on_disk(small_album_cover), (
        "a cover below the asked width was re-encoded")

print("cover preview: all checks passed")
shutil.rmtree(TMP, ignore_errors=True)
