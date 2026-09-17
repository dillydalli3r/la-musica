#!/usr/bin/env python3
"""Cover search (server.integrations) and the lyrics ranking contract.

What this pins, with every HTTP seam stubbed (no network at all):

  * the COV request is built with the app's EXACT header set (the API gate
    401s without it, and an added `Accept` makes it answer an empty stream)
    and with a NON-EMPTY source list even when the saved list is empty —
    COV rejects an empty one with "At least one source must be selected";
  * the streamed JSON lines parse into results carrying the keys the finder
    already reads plus `width`/`height` — taken from the line when the line
    states them, otherwise read from the image's own header by a ranged GET;
  * that probe really parses JPEG (SOFn), PNG (IHDR) and all three WebP
    headers, is memoized (a second call makes no request, even across a
    restart via the disk cache), and can NEVER fail the search: a bad host, an
    erroring fetch and unparseable bytes all leave `width`/`height` null;
  * only the first COVER_PROBE_LIMIT results are probed;
  * the fallback chain (Cover Art Archive by release-group MBID → Deezer →
    iTunes) runs ONLY when the meta-search returns zero or fails, stops at the
    first provider that answers, and the response records which one it was;
  * `resolve_cov_search` lets a per-search `sources`/`country` override the
    saved defaults for that one search, validates ids against the catalogue,
    and never mutates the config;
  * the lyrics built-in chain is the documented ranking, `available_sources()`
    carries a stable 1-based `rank` (and states each provider's caveats), and
    a saved `lyrics_sources` list still wins.

Run:  python tools/test_covers.py
"""
import contextlib
import copy
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo import config as mlo_config
from mlo import lyrics_providers as lp
from server import integrations as intg


# --------------------------------------------------------------------------- #
# Harness — the four HTTP seams, and a private cache/temp space
# --------------------------------------------------------------------------- #
_TMP = tempfile.mkdtemp(prefix="mlo-covers-test-")
# The disk caches belong to a real app run (and would outlive these stubs).
intg._data_cache_dir = lambda name: os.path.join(_TMP, name)
intg._apple_cache_dir = lambda: None
intg._APPLE_MIN_INTERVAL = 0.0
# Host resolution is not the subject here; the SSRF guard has its own tests.
intg._public_host = lambda host: bool(host)

# The catalogue COV answers /api/info with (no network): qobuz/bandcamp off.
CATALOG = {
    "sources": [
        {"id": "itunes", "name": "iTunes", "enabled": True, "color": None,
         "countries": ["us"]},
        {"id": "deezer", "name": "Deezer", "enabled": True, "color": None,
         "countries": ["us"]},
        {"id": "qobuz", "name": "Qobuz", "enabled": False, "color": None,
         "countries": ["us"]},
    ],
    "countries": ["us", "gb"],
    "active_source_limit": 9,
}
intg.cov_catalog = lambda timeout=15.0: copy.deepcopy(CATALOG)


def stub_cov(lines, error=None):
    """Replace the streaming seam; returns the call log."""
    calls = []

    @contextlib.contextmanager
    def fake(body, headers, timeout=60.0):
        calls.append({"body": dict(body), "headers": dict(headers)})
        if error is not None:
            raise error
        yield iter(list(lines))

    intg._cov_stream = fake
    return calls


def stub_json(routes):
    """Replace the JSON seam (CAA / Deezer / iTunes, which share it)."""
    calls = []

    def fake(url, params=None, headers=None, timeout=None, host=None):
        calls.append((url, dict(params or {})))
        for key, payload in routes.items():
            if key in url:
                return payload(params or {}) if callable(payload) else payload
        return None

    intg._advisory_json = fake
    return calls


def stub_probe(images, error=False):
    """Replace the ranged-GET seam; returns the probed URLs."""
    calls = []

    def fake(url, nbytes=intg.COVER_PROBE_BYTES, timeout=10.0):
        calls.append(url)
        if error:
            raise RuntimeError("probe transport exploded")
        return images.get(url, b"")

    intg._probe_get = fake
    return calls


def clear_caches():
    """Memory AND disk — a fresh cache, the way a fresh install starts."""
    import shutil

    intg._GENRE_CACHE.clear()
    shutil.rmtree(os.path.join(_TMP, "genre_cache"), ignore_errors=True)


def cover_lines(count, source="itunes", big="https://img.test/a{}.jpg",
                small="https://img.test/a{}-500.jpg", **extra):
    """*count* streamed cover events, each with its OWN image URL (the probe
    cache is per URL, so identical URLs would collapse into one request)."""
    return [json.dumps({"type": "cover", "source": source,
                        "bigCoverUrl": big.format(i),
                        "smallCoverUrl": small.format(i), **extra,
                        "releaseInfo": {"title": "T", "artist": "A",
                                        "tracks": 12, "url": "https://rel/"}})
            for i in range(count)]


ROW_KEYS = {"source", "small", "big", "title", "artist", "tracks", "url",
            "width", "height"}


# --------------------------------------------------------------------------- #
# Image headers — real bytes, built here (no fixtures on disk)
# --------------------------------------------------------------------------- #
def jpeg(w, h):
    return (b"\xff\xd8"
            + b"\xff\xe0\x00\x10" + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            + b"\xff\xc0\x00\x11\x08" + h.to_bytes(2, "big") + w.to_bytes(2, "big")
            + b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
            + b"\xff\xd9")


def png(w, h):
    return (b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
            + w.to_bytes(4, "big") + h.to_bytes(4, "big")
            + b"\x08\x06\x00\x00\x00")


def webp_vp8x(w, h):
    return (b"RIFF" + (22).to_bytes(4, "little") + b"WEBP" + b"VP8X"
            + (10).to_bytes(4, "little") + b"\x00\x00\x00\x00"
            + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little"))


def webp_vp8(w, h):
    return (b"RIFF" + (30).to_bytes(4, "little") + b"WEBP" + b"VP8 "
            + (18).to_bytes(4, "little") + b"\x00\x00\x00" + b"\x9d\x01\x2a"
            + (w & 0x3FFF).to_bytes(2, "little") + (h & 0x3FFF).to_bytes(2, "little"))


def webp_vp8l(w, h):
    bits = ((w - 1) & 0x3FFF) | (((h - 1) & 0x3FFF) << 14)
    return (b"RIFF" + (21).to_bytes(4, "little") + b"WEBP" + b"VP8L"
            + (5).to_bytes(4, "little") + b"\x2f" + bits.to_bytes(4, "little")
            + b"\x00\x00\x00\x00")


# --------------------------------------------------------------------------- #
# 1) The image-header parser
# --------------------------------------------------------------------------- #
assert intg._image_size(jpeg(48, 64)) == (48, 64), intg._image_size(jpeg(48, 64))
assert intg._image_size(jpeg(3000, 3000)) == (3000, 3000)
assert intg._image_size(png(1920, 1080)) == (1920, 1080), intg._image_size(png(1920, 1080))
assert intg._image_size(webp_vp8x(1400, 900)) == (1400, 900), \
    intg._image_size(webp_vp8x(1400, 900))
assert intg._image_size(webp_vp8(800, 600)) == (800, 600), \
    intg._image_size(webp_vp8(800, 600))
assert intg._image_size(webp_vp8l(1500, 1501)) == (1500, 1501), \
    intg._image_size(webp_vp8l(1500, 1501))
# Nothing to read is not a size, and neither is a truncated header.
assert intg._image_size(b"") is None
assert intg._image_size(b"\xff\xd8") is None
assert intg._image_size(b"\x00" * 64) is None
assert intg._image_size(b"GIF89a" + b"\x00" * 32) is None


# --------------------------------------------------------------------------- #
# 2) The probe: parsed, memoized, and never fatal
# --------------------------------------------------------------------------- #
IMG = "https://img.test/a.jpg"
clear_caches()
probes = stub_probe({IMG: png(1000, 1000)})
assert intg.image_dimensions(IMG) == {"width": 1000, "height": 1000}
assert intg.image_dimensions(IMG) == {"width": 1000, "height": 1000}   # memo
assert len(probes) == 1, probes
# A restart keeps the answer: the disk cache answers, no request is made.
intg._GENRE_CACHE.clear()
assert intg.image_dimensions(IMG) == {"width": 1000, "height": 1000}
assert len(probes) == 1, probes
# ...and it really is on disk under the app's data dir.
assert os.path.isdir(os.path.join(_TMP, "genre_cache")), os.listdir(_TMP)

# Unknown stays unknown, and a failing transport is unknown too — never a
# raise, and never a made-up number.
clear_caches()
probes = stub_probe({})
assert intg.image_dimensions("https://img.test/mystery.png") is None
assert intg.image_dimensions("") is None
assert intg.image_dimensions(None) is None
assert probes == ["https://img.test/mystery.png"], probes
clear_caches()
probes = stub_probe({}, error=True)
assert intg.image_dimensions("https://img.test/boom.jpg") is None
assert len(probes) == 1, probes
# a non-http URL is never fetched
probes = stub_probe({IMG: png(10, 10)})
assert intg.image_dimensions("file:///etc/passwd") is None
assert probes == [], probes


# --------------------------------------------------------------------------- #
# 3) The COV request: the app's headers, a non-empty source list
# --------------------------------------------------------------------------- #
clear_caches()
calls = stub_cov(cover_lines(1))
stub_json({})
stub_probe({IMG: png(1000, 1000)})
CFG = {"cover_sources": [], "cover_country": "us"}
before = copy.deepcopy(CFG)

out = intg.cover_search("Radiohead", "OK Computer", cfg=CFG)
assert (out["provider"], len(out["results"])) == ("cov", 1), out
req = calls[0]
# The saved list is empty → the provider's own enabled list, capped, never
# empty (COV answers 400 "At least one source must be selected" for an empty
# one, and that 400 must not be reachable from this app).
# the provider's own enabled list, in the app's quality order (deezer ranks
# above itunes in COV_SOURCE_PRIORITY) — and never empty
assert req["body"]["sources"] == ["deezer", "itunes"], req["body"]
assert req["body"]["country"] == "us", req["body"]
assert req["body"]["artist"] == "Radiohead", req["body"]
assert req["body"]["album"] == "OK Computer", req["body"]
# Exactly the header set the site's own frontend sends — an added
# `Accept: application/json` makes COV answer 200 with an EMPTY stream.
assert set(req["headers"]) == {"User-Agent", "Referer", "Origin", "X-Session"}, \
    req["headers"]
assert req["headers"]["User-Agent"] == intg.COV_UA, req["headers"]
assert req["headers"]["Referer"] == f"{intg.COV_BASE}/", req["headers"]
assert req["headers"]["Origin"] == intg.COV_BASE, req["headers"]
assert len(req["headers"]["X-Session"]) == 32, req["headers"]
# the same call again carries a FRESH session id
intg.cover_search("Radiohead", "OK Computer", cfg=CFG)
assert calls[1]["headers"]["X-Session"] != req["headers"]["X-Session"]

# A per-search override reaches the request unchanged (and stays there).
intg.cover_search("Radiohead", "OK Computer", sources=["deezer"],
                  country="gb", cfg=CFG)
assert calls[2]["body"]["sources"] == ["deezer"], calls[2]["body"]
assert calls[2]["body"]["country"] == "gb", calls[2]["body"]
# The saved config is not written by a search — not in memory, not on disk.
assert CFG == before, CFG
_saved = []
mlo_config.save_config = lambda cfg: (_saved.append(cfg), True)[1]
intg.cover_search("Radiohead", "OK Computer", sources=["deezer"], cfg=CFG)
assert _saved == [], _saved


# --------------------------------------------------------------------------- #
# 4) Streamed lines → results, with dimensions
# --------------------------------------------------------------------------- #
clear_caches()
lines = [
    '{"type":"source","country":"us","source":"itunes"}',
    '{"type":"count","releaseCount":16,"source":"itunes"}',
    # states its own size → used as-is, no probe
    json.dumps({"type": "cover", "source": "deezer", "width": 1400,
                "height": 1400, "smallCoverUrl": "https://img.test/b-500.jpg",
                "bigCoverUrl": "https://img.test/b.jpg",
                "releaseInfo": {"title": "OK Computer", "artist": "Radiohead",
                                "tracks": 12, "url": "https://www.deezer.com/album/1"}}),
    # states nothing → the image itself is probed
    json.dumps({"type": "cover", "source": "itunes",
                "smallCoverUrl": "https://img.test/a-500.jpg",
                "bigCoverUrl": IMG,
                "releaseInfo": {"title": "OK Computer", "artist": "Radiohead",
                                "tracks": 12,
                                "url": "https://music.apple.com/us/album/1097861387"}}),
    # no cover URL at all → null dimensions, and nothing is fetched for it
    json.dumps({"type": "cover", "source": "itunes",
                "releaseInfo": {"title": "OK Computer"}}),
    "garbage, not json",
    '{"type":"done"}',
]
calls = stub_cov(lines)
stub_json({})
probes = stub_probe({IMG: png(3000, 3000)})
out = intg.cover_search("Radiohead", "OK Computer", cfg=CFG)
assert out["provider"] == "cov", out
rows = out["results"]
assert len(rows) == 3, rows
assert all(set(r) == ROW_KEYS for r in rows), rows
# the finder's own keys are unchanged
assert rows[0] == {"source": "deezer", "small": "https://img.test/b-500.jpg",
                   "big": "https://img.test/b.jpg", "title": "OK Computer",
                   "artist": "Radiohead", "tracks": 12,
                   "url": "https://www.deezer.com/album/1",
                   "width": 1400, "height": 1400}, rows[0]
# probed from the file, not from the URL's own "500x0w" hint
assert (rows[1]["width"], rows[1]["height"]) == (3000, 3000), rows[1]
assert (rows[2]["width"], rows[2]["height"]) == (None, None), rows[2]
assert probes == [IMG], probes

# Only the first COVER_PROBE_LIMIT rows are probed; the rest carry null.
clear_caches()
calls = stub_cov(cover_lines(intg.COVER_PROBE_LIMIT + 2))
stub_json({})
probes = stub_probe(dict.fromkeys(
    [f"https://img.test/a{i}.jpg" for i in range(intg.COVER_PROBE_LIMIT + 2)],
    png(1000, 1000)))
rows = intg.cover_search("Radiohead", "OK Computer", cfg=CFG)["results"]
assert len(probes) == intg.COVER_PROBE_LIMIT, len(probes)
assert rows[intg.COVER_PROBE_LIMIT - 1]["width"] == 1000, rows[-3]
assert rows[-1]["width"] is None and rows[-1]["height"] is None, rows[-1]

# A probe that explodes does not fail the search.
clear_caches()
stub_cov(cover_lines(1))
stub_json({})
stub_probe({IMG: png(1000, 1000)}, error=True)
rows = intg.cover_search("Radiohead", "OK Computer", cfg=CFG)["results"]
assert len(rows) == 1 and rows[0]["width"] is None, rows

# An unparseable body is unknown, not a crash.
clear_caches()
stub_cov(cover_lines(1))
stub_json({})
probes = stub_probe({IMG: b"\x00\x01\x02" * 40})
rows = intg.cover_search("Radiohead", "OK Computer", cfg=CFG)["results"]
assert rows[0]["width"] is None and probes == ["https://img.test/a0.jpg"], rows

# The limit is the stream's own cap (and 1 is honoured).
clear_caches()
stub_cov(cover_lines(5))
stub_json({})
stub_probe({IMG: png(1000, 1000)})
assert len(intg.cover_search("Radiohead", "OK Computer", limit=2, cfg=CFG)["results"]) == 2
assert calls[0]["body"]["sources"], calls[0]["body"]


# --------------------------------------------------------------------------- #
# 5) The fallbacks — only when the meta-search has nothing
# --------------------------------------------------------------------------- #
CAA_RG = "b1392450-e666-3926-a536-22c65f834433"
CAA_IMAGE = "https://coverartarchive.org/release/abc/123.png"
DEEZER_XL = "https://cdn-images.dzcdn.net/images/cover/ok/1000x1000-000000-80-0-0.jpg"
ART100 = "https://is1-ssl.mzstatic.com/image/thumb/Music/abc/100x100bb.jpg"

# (a) the primary answered → no fallback provider is even asked
clear_caches()
stub_cov(cover_lines(1))
calls = stub_json({})
probes = stub_probe({IMG: png(1000, 1000)})
out = intg.cover_search("Radiohead", "OK Computer", cfg=CFG,
                        release_group_mbid=CAA_RG)
assert out["provider"] == "cov" and calls == [], (out, calls)
assert probes == ["https://img.test/a0.jpg"], probes

# (b) zero results, release-group MBID known → Cover Art Archive, front first
clear_caches()
stub_cov([])
calls = stub_json({"coverartarchive.org": {"images": [
    {"front": False, "image": "https://coverartarchive.org/release/abc/back.png",
     "thumbnails": {"large": "https://coverartarchive.org/release/abc/back-500.jpg",
                    "small": "https://coverartarchive.org/release/abc/back-250.jpg"}},
    {"front": True, "image": CAA_IMAGE,
     "thumbnails": {"large": "https://coverartarchive.org/release/abc/123-500.jpg",
                    "small": "https://coverartarchive.org/release/abc/123-250.jpg"}},
]}})
probes = stub_probe({CAA_IMAGE: jpeg(1500, 1500)})
out = intg.cover_search("Radiohead", "OK Computer", cfg=CFG,
                        release_group_mbid=CAA_RG)
assert out["provider"] == "coverartarchive", out
rows = out["results"]
assert len(rows) == 2, rows
assert [r["big"] for r in rows] == [CAA_IMAGE,
                                    "https://coverartarchive.org/release/abc/back.png"]
assert all(set(r) == ROW_KEYS for r in rows), rows
assert rows[0]["source"] == "coverartarchive", rows[0]
assert rows[0]["small"].endswith("123-500.jpg"), rows[0]
assert rows[0]["title"] == "OK Computer" and rows[0]["artist"] == "Radiohead"
assert rows[0]["url"] == f"{intg.CAA_BASE}/release-group/{CAA_RG}", rows[0]
assert (rows[0]["width"], rows[0]["height"]) == (1500, 1500), rows[0]
assert [c[0] for c in calls] == [f"{intg.CAA_BASE}/release-group/{CAA_RG}"], calls
assert probes == [CAA_IMAGE,
                  "https://coverartarchive.org/release/abc/back.png"], probes

# (c) no MBID → Deezer, and the real album's cover sorts ahead of a karaoke one
clear_caches()
stub_cov([])
calls = stub_json({"api.deezer.com": {"data": [
    {"title": "OK Computer", "nb_tracks": 11, "link": "https://www.deezer.com/album/9",
     "artist": {"name": "Molotov Cocktail Piano"},
     "cover_xl": "https://cdn-images.dzcdn.net/images/cover/karaoke/1000x1000.jpg",
     "cover_big": "https://cdn-images.dzcdn.net/images/cover/karaoke/500x500.jpg"},
    {"title": "OK Computer", "nb_tracks": 12, "link": "https://www.deezer.com/album/1",
     "artist": {"name": "Radiohead"}, "cover_xl": DEEZER_XL,
     "cover_big": "https://cdn-images.dzcdn.net/images/cover/ok/500x500-000000-80-0-0.jpg",
     "cover_medium": "https://cdn-images.dzcdn.net/images/cover/ok/250x250.jpg"},
]}})
probes = stub_probe({DEEZER_XL: jpeg(1000, 1000)})
out = intg.cover_search("Radiohead", "OK Computer", cfg=CFG)
assert out["provider"] == "deezer", out
rows = out["results"]
assert [r["artist"] for r in rows] == ["Radiohead", "Molotov Cocktail Piano"], rows
assert rows[0]["source"] == "deezer" and rows[0]["big"] == DEEZER_XL, rows[0]
assert rows[0]["tracks"] == 12, rows[0]
assert (rows[0]["width"], rows[0]["height"]) == (1000, 1000), rows[0]
assert calls[0][0] == f"{intg.DEEZER_API}/search/album", calls
assert "artist:\"Radiohead\"" in calls[0][1]["q"], calls[0][1]

# (d) Deezer empty → iTunes, at its 3000×3000 artwork
clear_caches()
stub_cov([])
calls = stub_json({"api.deezer.com": {"data": []},
                   "itunes.apple.com/search": {"results": [
                       {"collectionName": "OK Computer", "artistName": "Radiohead",
                        "trackCount": 12, "artworkUrl100": ART100,
                        "collectionViewUrl": "https://music.apple.com/us/album/1097861387"},
                       {"collectionName": "MCP Performs Radiohead: OK Computer",
                        "artistName": "Molotov Cocktail Piano", "trackCount": 11,
                        "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/Music/x/100x100bb.jpg"},
                   ]}})
BIG3000 = "https://is1-ssl.mzstatic.com/image/thumb/Music/abc/3000x3000bb.jpg"
probes = stub_probe({BIG3000: png(3000, 3000)})
out = intg.cover_search("Radiohead", "OK Computer", cfg=CFG)
assert out["provider"] == "itunes", out
rows = out["results"]
assert rows[0]["artist"] == "Radiohead", rows
assert rows[0]["small"] == ART100 and rows[0]["big"] == BIG3000, rows[0]
assert rows[0]["title"] == "OK Computer" and rows[0]["tracks"] == 12, rows[0]
assert (rows[0]["width"], rows[0]["height"]) == (3000, 3000), rows[0]
assert [c[0] for c in calls] == [f"{intg.DEEZER_API}/search/album",
                                 "https://itunes.apple.com/search"], calls
assert probes == [BIG3000,
                  "https://is1-ssl.mzstatic.com/image/thumb/Music/x/3000x3000bb.jpg"], probes

# (e) a dead meta-search is a fallback case, not an error
clear_caches()
stub_cov([], error=RuntimeError("COV refused"))
stub_json({"api.deezer.com": {"data": [
    {"title": "OK Computer", "artist": {"name": "Radiohead"}, "nb_tracks": 12,
     "cover_xl": DEEZER_XL, "link": "https://www.deezer.com/album/1"}]}})
stub_probe({DEEZER_XL: png(1000, 1000)})
assert intg.cover_search("Radiohead", "OK Computer", cfg=CFG)["provider"] == "deezer"

# (f) nobody had anything → an EMPTY answer that says so (provider null)
clear_caches()
stub_cov([])
stub_json({})
stub_probe({})
out = intg.cover_search("Nobody", "Nothing", cfg=CFG)
assert out == {"results": [], "provider": None}, out
# ...and a provider that errors is skipped, not fatal
clear_caches()
stub_cov([])


def _boom(*_a, **_k):
    raise RuntimeError("deezer down")


stub_json({"api.deezer.com": _boom})
out = intg.cover_search("Nobody", "Nothing", cfg=CFG)
assert out == {"results": [], "provider": None}, out

# (g) a missing artist+album is still a 400 in the route's eyes
try:
    intg.cover_search("", "", cfg=CFG)
except ValueError:
    pass
else:
    raise AssertionError("cover_search accepted an empty query")


# --------------------------------------------------------------------------- #
# 6) resolve_cov_search: saved defaults, transient overrides, no writes
# --------------------------------------------------------------------------- #
saved = {"cover_sources": ["deezer", "itunes"], "cover_country": "gb"}
frozen = copy.deepcopy(saved)
# the SAVED defaults answer when the caller passes nothing
assert intg.resolve_cov_search(None, None, saved) == (["deezer", "itunes"], "gb")
# a per-search override wins for that one search...
assert intg.resolve_cov_search(["itunes"], "us", saved) == (["itunes"], "us")
assert intg.resolve_cov_search(["itunes"], "us", saved) == (["itunes"], "us")
# ...and leaves the saved defaults exactly as they were
assert saved == frozen, saved
# an empty saved list means "the provider's own enabled list, capped"
assert intg.resolve_cov_search(None, None, {"cover_sources": [],
                                            "cover_country": ""}) == \
    (["deezer", "itunes"], "us")
# an empty override list is "nothing passed", not "search nothing"
assert intg.resolve_cov_search([], None, saved)[0] == ["deezer", "itunes"]
# ids the catalogue does not know (stale saved settings, typos) are dropped,
# and the list can never come back empty
assert intg.resolve_cov_search(["nope", "qobuz"], None, saved) == (["qobuz"], "gb")
assert intg.resolve_cov_search(["nope"], None, saved)[0] == ["deezer", "itunes"]
# the country must be one COV knows
assert intg.resolve_cov_search(None, "zz", saved)[1] == intg.COV_DEFAULT_COUNTRY
assert intg.resolve_cov_search(None, "GB", saved)[1] == "gb"
# ...and the cap is COV's own active-source limit
intg.cov_catalog = lambda timeout=15.0: {**copy.deepcopy(CATALOG),
                                         "active_source_limit": 1}
assert intg.resolve_cov_search(["itunes", "deezer"], None, saved)[0] == ["itunes"]
intg.cov_catalog = lambda timeout=15.0: copy.deepcopy(CATALOG)
# the saved `cover_sources`/`cover_country` keys exist in the shipped config
assert mlo_config.DEFAULT_CONFIG["cover_sources"] == []
assert mlo_config.DEFAULT_CONFIG["cover_country"] == "us"


# --------------------------------------------------------------------------- #
# 7) The lyrics ranking
# --------------------------------------------------------------------------- #
# The documented ranking IS the code's ranking: every provider appears in the
# module docstring, in the order it is tried.
DOC_RANK = []
for line in lp.__doc__.splitlines():
    line = line.strip()
    if line.startswith("* ``") and "``" in line[4:]:
        DOC_RANK.append(line[4:].split("``")[0])
assert DOC_RANK == ["lrclib", "netease", "qq", "kuwo", "kugou", "youtube"], DOC_RANK
assert lp.SOURCES == DOC_RANK, (lp.SOURCES, DOC_RANK)

listed = lp.available_sources()
assert [s["id"] for s in listed] == lp.SOURCES, listed
assert [s["rank"] for s in listed] == [1, 2, 3, 4, 5, 6], listed
assert lp.available_sources() == listed, "the catalogue must be stable"
# each row says what it is good at, and every caveat that exists is stated
for src in listed:
    assert src["notes"].strip() and "synced" in src["notes"].lower(), src
for pid in ("netease", "qq", "kuwo", "kugou"):
    assert "unofficial api" in lp.SOURCE_NOTES[pid].lower(), lp.SOURCE_NOTES[pid]
assert "auto-generated" in lp.SOURCE_NOTES["youtube"].lower(), lp.SOURCE_NOTES["youtube"]
assert "best global coverage" in lp.SOURCE_NOTES["lrclib"], lp.SOURCE_NOTES["lrclib"]
assert "CJK" in lp.SOURCE_NOTES["netease"], lp.SOURCE_NOTES["netease"]

# The built-in order is the default; a saved list still wins, wholesale.
assert lp.provider_order({}) == lp.SOURCES
assert lp.provider_order({"lyrics_sources": []}) == lp.SOURCES
assert lp.provider_order({"lyrics_sources": ["kuwo", "lrclib"]}) == ["kuwo", "lrclib"]
assert lp.provider_order({"lyrics_sources": ["youtube"]}) == ["youtube"]
assert lp.provider_order({"lyrics_sources": ["nope", "kugou"]}) == ["kugou"]
# rank describes the DEFAULT chain, so a user order does not renumber it
assert [s["rank"] for s in lp.available_sources()][:2] == [1, 2]

print("covers + lyrics ranking: all checks passed")

# the scratch cache space goes with the run
shutil.rmtree(_TMP, ignore_errors=True)
