#!/usr/bin/env python3
"""The art proxy (server.artcache): allowlist, disk cache, fallback chain.

What this pins, with the one HTTP seam stubbed (no network at all):

  * only the allowlisted provider hosts are reachable through the proxy — the
    route would otherwise relay any address the server can see;
  * a cached image is served without a request, and a cached winner is served
    under the ORIGINAL key however the image was actually obtained;
  * a provider that refuses us (403) falls through to the next candidate, and
    the chain is Cover Art Archive → iTunes → Deezer, LAZILY: a tier is only
    asked when every tier before it failed;
  * a total failure is `(None, None, None)` — never an exception — and is
    negatively cached for minutes, so a broken shelf row makes one round of
    requests, not one per render;
  * the per-host header table is applied (contact agent for Wikimedia /
    MusicBrainz / the Cover Art Archive, a browser's own headers for the music
    CDNs, which answer a bot agent with 403);
  * an entry past its TTL is refetched, and the prune keeps the cache under
    its ceiling, dropping the oldest first.

Run:  python tools/test_artcache.py
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import artcache
from server import integrations as intg

_TMP = tempfile.mkdtemp(prefix="mlo-artcache-test-")
# The disk cache belongs to a real app run (and would outlive these stubs).
artcache.cache_dir = lambda music_folder=None: os.path.join(_TMP, "art_cache")
# The genuine HTTP seam — the stubs below replace the module attribute.
_REAL_GET = artcache._get

PRIMARY = "https://cdn-images.dzcdn.net/images/cover/abc123/500x500.jpg"
CAA = "https://coverartarchive.org/release-group/rg-1/front-1200"
ITUNES = "https://is1-ssl.mzstatic.com/image/thumb/x/3000x3000bb.jpg"
DEEZER = "https://cdn-images.dzcdn.net/images/cover/zzz/1000x1000.jpg"
WIKI = "https://upload.wikimedia.org/wikipedia/en/a/ab/Radiohead.jpg"

JPEG = b"\xff\xd8\xff\xe0" + b"cover-bytes"
JPEG3000 = b"\xff\xd8\xff\xe0" + b"apple-artwork"
PNG = b"\x89PNG\r\n\x1a\n" + b"wiki-artwork"


def stub_get(routes):
    """Replace the fetch seam; returns the call log [(url, headers)]."""
    calls = []

    def fake(url, headers, timeout):
        calls.append((url, dict(headers or {})))
        status, data, ctype = routes.get(url, (403, b"", ""))
        if status != 200:
            return status, b"", ""
        return status, data, ctype or artcache._sniff(data)

    artcache._get = fake
    return calls


def reset():
    artcache._FAILS.clear()
    shutil.rmtree(os.path.join(_TMP, "art_cache"), ignore_errors=True)


# --------------------------------------------------------------------------- #
# 1) The allowlist is the trust boundary
# --------------------------------------------------------------------------- #
assert artcache.allowed(PRIMARY)
assert artcache.allowed(WIKI)
assert artcache.allowed(ITUNES)
assert artcache.allowed(CAA)
assert artcache.allowed("https://ia801234.us.archive.org/1/items/x/cover.jpg")
assert artcache.allowed("https://lastfm.freetls.fastly.net/i/u/300x300/abc.jpg")
assert artcache.allowed("https://www.theaudiodb.com/images/media/artist/x.jpg")
# the cover finder's other sources (docs/cover-sources)
assert artcache.allowed("https://static.qobuz.com/images/covers/47/73/5414939937347_600.jpg")
assert artcache.allowed("https://resources.tidal.com/images/7e12f5d8/640x640.jpg")
assert artcache.allowed("https://mvod.itunes.apple.com/itunes-assets/x/100x100bb.jpg")
assert artcache.allowed("https://i.scdn.co/image/ab67616d0000b273")
# …and nothing else is: a look-alike host is not a suffix match, and neither
# is the loopback the SSRF guard exists for.
assert not artcache.allowed("https://dzcdn.net.evil.example/x.jpg")
assert not artcache.allowed("https://evil.example/x.jpg")
assert not artcache.allowed("http://127.0.0.1:8000/api/cover?album=x")
assert not artcache.allowed("http://localhost/x.jpg")
assert not artcache.allowed("file:///etc/passwd")
assert not artcache.allowed("")
assert not artcache.allowed(None)

reset()
calls = stub_get({})
assert artcache.fetch_art("https://evil.example/x.jpg") == (None, None, None)
assert calls == [], calls


# --------------------------------------------------------------------------- #
# 2) Per-host headers: one table, and the two families really are different
# --------------------------------------------------------------------------- #
assert artcache.headers_for(WIKI)["User-Agent"] == intg.USER_AGENT
assert artcache.headers_for(CAA)["User-Agent"] == intg.USER_AGENT
assert artcache.headers_for("https://archive.org/x.jpg")["User-Agent"] == intg.USER_AGENT
assert artcache.headers_for(PRIMARY)["User-Agent"] != intg.USER_AGENT
assert "Mozilla" in artcache.headers_for(PRIMARY)["User-Agent"]
assert artcache.headers_for(PRIMARY)["Referer"] == "https://www.deezer.com/"
assert artcache.headers_for(ITUNES)["User-Agent"].startswith("Mozilla")
assert "Referer" not in artcache.headers_for(ITUNES)
assert artcache.headers_for("https://lastfm.freetls.fastly.net/i/u/x.jpg")["Referer"] == "https://www.last.fm/"

reset()
calls = stub_get({PRIMARY: (200, JPEG, "image/jpeg"), WIKI: (200, PNG, "")})
assert artcache.fetch_art(PRIMARY)[0] == JPEG
assert artcache.fetch_art(WIKI)[2] == "url"
# the header set the table names really was sent, per host
assert calls[0][1]["User-Agent"].startswith("Mozilla")
assert calls[1][1]["User-Agent"] == intg.USER_AGENT
# content type: the response's own, or sniffed from the bytes when it states none
assert artcache._read(artcache._key(WIKI), artcache.cache_dir())[1] == "image/png"


# --------------------------------------------------------------------------- #
# 3) A hit costs no request
# --------------------------------------------------------------------------- #
reset()
calls = stub_get({PRIMARY: (200, JPEG, "image/jpeg")})
first = artcache.fetch_art(PRIMARY)
assert first == (JPEG, "image/jpeg", "url"), first
assert len(calls) == 1
second = artcache.fetch_art(PRIMARY)
assert second == first, second
assert len(calls) == 1, calls


# --------------------------------------------------------------------------- #
# 4) A refusing provider falls through, and the WINNER is cached under the
#    original key — the next view of that row is instant and offline.
# --------------------------------------------------------------------------- #
reset()
calls = stub_get({CAA: (200, JPEG, "image/jpeg")})
data, ctype, source = artcache.fetch_art(
    PRIMARY, artist="Radiohead", album="OK Computer", release_group_mbid="rg-1")
assert data == JPEG and ctype == "image/jpeg", (data, ctype)
assert source == "coverartarchive", source
assert [u for u, _h in calls] == [PRIMARY, CAA], calls
# the second call answers from the cache keyed by the ORIGINAL url
before = len(calls)
assert artcache.fetch_art(PRIMARY, artist="Radiohead", album="OK Computer",
                          release_group_mbid="rg-1")[0] == JPEG
assert len(calls) == before, calls
# …under its own key, so the fallback URL is not what was stored
assert os.path.isfile(os.path.join(artcache.cache_dir(), artcache._key(PRIMARY) + ".bin"))

# The chain is CAA → iTunes → Deezer, and it is LAZY: with the Cover Art
# Archive answering, neither of the other two is even asked for a URL.
reset()
asked = []
artcache._itunes_art_url = lambda a, al, cfg, t: asked.append("itunes") or ITUNES
artcache._deezer_art_url = lambda a, al, cfg, t: asked.append("deezer") or DEEZER
calls = stub_get({CAA: (200, JPEG, "image/jpeg")})
artcache.fetch_art(PRIMARY, artist="Radiohead", album="OK Computer",
                   release_group_mbid="rg-1")
assert asked == [], asked

# With no MBID (and a dead primary) iTunes answers next…
reset()
asked = []
calls = stub_get({ITUNES: (200, JPEG3000, "image/jpeg")})
data, _ctype, source = artcache.fetch_art(PRIMARY, artist="Radiohead", album="OK Computer")
assert asked == ["itunes"], asked
assert source == "itunes" and data == JPEG3000, (source, data)
assert [u for u, _h in calls] == [PRIMARY, ITUNES], calls

# …and Deezer only after it, when iTunes has nothing either.
reset()
asked = []
calls = stub_get({DEEZER: (200, JPEG, "image/jpeg")})
artcache._itunes_art_url = lambda a, al, cfg, t: asked.append("itunes") or ""
data, _ctype, source = artcache.fetch_art(PRIMARY, artist="Radiohead", album="OK Computer")
assert asked == ["itunes", "deezer"], asked
assert source == "deezer", source
assert [u for u, _h in calls] == [PRIMARY, DEEZER], calls


# --------------------------------------------------------------------------- #
# 5) Total failure: None, never a raise — and negatively cached for minutes
# --------------------------------------------------------------------------- #
reset()
calls = stub_get({})                      # everything answers 403
assert artcache.fetch_art(PRIMARY, artist="Radiohead", album="OK Computer") == \
    (None, None, None)
assert (artcache._key(PRIMARY) in artcache._FAILS), artcache._FAILS
before = len(calls)
# inside the window: no request at all, not even the fallback lookup
assert artcache.fetch_art(PRIMARY, artist="Radiohead", album="OK Computer") == \
    (None, None, None)
assert len(calls) == before, calls
# past it, the provider is asked again
artcache._FAILS[artcache._key(PRIMARY)] = time.time() - 1
artcache.fetch_art(PRIMARY, artist="Radiohead", album="OK Computer")
assert len(calls) > before, calls

# A transport failure (None status) is a failure too, and so is an empty body.
reset()


def broken(url, headers, timeout):
    return None, b"", ""


artcache._get = broken
assert artcache.fetch_art(PRIMARY) == (None, None, None)

reset()


def empty(url, headers, timeout):
    return 200, b"", ""


artcache._get = empty
assert artcache.fetch_art(PRIMARY) == (None, None, None)


# The real seam, against a loopback server: a cover comes back whole with the
# headers it was sent, and an oversized body is refused rather than held.
import http.server
import threading


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):                                      # noqa: N802
        seen["headers"] = dict(self.headers)
        if self.path == "/nope":
            self.send_response(404)
            self.end_headers()
            return
        if self.path == "/video":
            body = b"\x00\x00\x00\x18ftypmp42" + b"v" * 64
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = CHUNK * 4 if self.path == "/big" else JPEG
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


seen = {}
CHUNK = b"z" * (intg.IMAGE_MAX_BYTES // 2)
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % srv.server_address[1]
try:
    status, data, ctype = _REAL_GET(base + "/ok", {"User-Agent": intg.USER_AGENT}, 20.0)
    assert (status, data, ctype) == (200, JPEG, "image/jpeg"), (status, data[:16], ctype)
    assert seen["headers"]["User-Agent"] == intg.USER_AGENT
    # 2 × IMAGE_MAX_BYTES: streamed past the cap and dropped, never buffered
    assert _REAL_GET(base + "/big", {}, 20.0) == (200, b"", "")
    # …and a provider that refuses us is a status, not an exception
    assert _REAL_GET(base + "/nope", {}, 20.0)[0] == 404
    # a body that is not an image is a failure too (a video listed as a cover)
    assert _REAL_GET(base + "/video", {}, 20.0) == (200, b"", "")
finally:
    srv.shutdown()
    srv.server_close()


# --------------------------------------------------------------------------- #
# 6) TTL: an entry past its window is refetched
# --------------------------------------------------------------------------- #
reset()
calls = stub_get({PRIMARY: (200, JPEG, "image/jpeg")})
artcache.fetch_art(PRIMARY)
d = artcache.cache_dir()
stale = time.time() - artcache.CACHE_TTL - 60
for suffix in (".bin", ".json"):
    os.utime(os.path.join(d, artcache._key(PRIMARY) + suffix), (stale, stale))
before = len(calls)
assert artcache.fetch_art(PRIMARY)[0] == JPEG
assert len(calls) == before + 1, calls


# --------------------------------------------------------------------------- #
# 7) The prune keeps the cache bounded, oldest first
# --------------------------------------------------------------------------- #
reset()
artcache._CACHE_BYTES, artcache._CACHE_KEEP = 4000, 0.5
try:
    body = b"\xff\xd8\xff\xe0" + b"y" * 1000
    asked = stub_get({})
    last = ""
    for i in range(6):
        url = "https://is1-ssl.mzstatic.com/image/thumb/%d/3000x3000bb.jpg" % i
        stub_get({url: (200, body, "image/jpeg")})
        assert artcache.fetch_art(url)[0] == body
        last = url
    total = sum(os.path.getsize(os.path.join(d, n)) for n in os.listdir(d))
    assert total <= 4000, total
    # the newest entry survived, the oldest went first
    assert os.path.isfile(os.path.join(d, artcache._key(last) + ".bin"))
    assert not os.path.isfile(os.path.join(d, artcache._key(
        "https://is1-ssl.mzstatic.com/image/thumb/0/3000x3000bb.jpg") + ".bin"))
    assert artcache.fetch_art(last)[0] == body      # still a hit, no refetch
finally:
    artcache._CACHE_BYTES, artcache._CACHE_KEEP = 300 * 1024 * 1024, 0.8

print("artcache: all checks passed")
shutil.rmtree(_TMP, ignore_errors=True)
