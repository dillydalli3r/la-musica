#!/usr/bin/env python3
"""Cover search (server.integrations) and the lyrics ranking contract.

What this pins, with every HTTP seam stubbed (no network at all):

  * the COV request is built with the app's EXACT header set (the API gate
    401s without it, and an added `Accept` makes it answer an empty stream)
    and with a NON-EMPTY source list even when the saved list is empty —
    COV rejects an empty one with "At least one source must be selected";
  * the streamed JSON lines parse into results carrying the keys the finder
    already reads plus what the candidate was MEASURED from: `width`/`height`
    (taken from the line when the line states them, otherwise read from the
    image's own header by a ranged GET), the container those bytes really are
    (`format`) and how many bytes the URL answered with (`bytes`), the
    provider's own front/back labelling and its rank in that provider's order;
  * that probe really parses JPEG (SOFn), PNG (IHDR) and all three WebP
    headers, is memoized (a second call makes no request, even across a
    restart via the disk cache), and can NEVER fail the search: a bad host, an
    erroring fetch and unparseable bytes all leave `width`/`height` null;
  * only the first COVER_PROBE_LIMIT results are probed;
  * the fallback chain (Cover Art Archive by release-group MBID → Deezer →
    iTunes) runs ONLY when the meta-search returns zero or fails, stops at the
    first provider that answers, and the response records which one it was;
  * the response also carries `sources` — one row per source that was asked
    (used / empty / error / skipped, with the reason), which is what the cover
    policy turns into the pick's own notes;
  * `resolve_cov_search` lets a per-search `sources`/`country` override the
    saved defaults for that one search, validates ids against the catalogue,
    and never mutates the config;
  * the import cover step (`run_cover_step`) honours the shipped defaults:
    `cover_review` ON (the default) STAGES the RANKED candidates — best first
    by `mlo.cover_choice`, each row with the reasons that put it there, the
    winner recorded as `chosen`, the finder's own per-source report as `notes`
    — in the metadata review file and writes no cover at all; OFF writes the
    WINNER through the cover page's own writer, and writes nothing (with a
    note saying why) when no candidate reaches the cover target; both modes
    ask for the same candidate set, the meta-search AND the identity reads the
    tags support, and the other keys of a pre-existing staged entry survive;
    `cover_auto_fetch` OFF fetches, stages and writes nothing; an album that
    already has art is the same no-op in all three modes;
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
                small="https://img.test/a{}-500.jpg", release=None, **extra):
    """*count* streamed cover events, each with its OWN image URL (the probe
    cache is per URL, so identical URLs would collapse into one request).

    `release` is the event's `releaseInfo` — what the source says the cover's
    own release is (COV really answers with the matched release's title, artist
    and track count, and `mlo.cover_choice` checks them against the album being
    covered). The default states NOTHING about the release, so a test that is
    not about the identity check is not silently testing it; the flows that are
    pass the album they were found for.
    """
    info = dict(release or {"title": None, "artist": None, "tracks": None,
                            "url": "https://rel/"})
    return [json.dumps({"type": "cover", "source": source,
                        "bigCoverUrl": big.format(i),
                        "smallCoverUrl": small.format(i), **extra,
                        "releaseInfo": info})
            for i in range(count)]


# Every row a provider answers with: the finder's own keys plus what the
# candidate was measured from (`mlo.cover_choice` reads all of them).
ROW_KEYS = {"source", "small", "big", "title", "artist", "tracks", "url",
            "width", "height", "format", "bytes", "front", "kind",
            "release_cover", "rank"}


def release_of(artist, album, tracks=None):
    """A `releaseInfo` block for `cover_lines`: the release the SOURCE says the
    cover belongs to. Real rows carry the album they were found for; a test
    that wants a karaoke or tribute answer passes that release's names instead.
    """
    return {"title": album, "artist": artist, "tracks": tracks,
            "url": "https://rel/"}


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
SIZE_PNG = {"width": 1000, "height": 1000, "format": "png",
            "bytes": len(png(1000, 1000))}
assert intg.image_dimensions(IMG) == SIZE_PNG, intg.image_dimensions(IMG)
assert intg.image_dimensions(IMG) == SIZE_PNG                        # memo
assert len(probes) == 1, probes
# A restart keeps the answer: the disk cache answers, no request is made.
intg._GENRE_CACHE.clear()
assert intg.image_dimensions(IMG) == SIZE_PNG
assert len(probes) == 1, probes
# The container is read from the bytes, never from the URL: a JPEG served from
# a ".png" path says jpeg, and bytes that are not one of the three say "".
assert intg._image_format(jpeg(10, 10)) == "jpeg"
assert intg._image_format(webp_vp8x(10, 10)) == "webp"
assert intg._image_format(b"GIF89a" + b"\x00" * 32) == ""
clear_caches()
stub_probe({IMG: jpeg(1200, 1200)})
got = intg.image_dimensions(IMG)
assert got["format"] == "jpeg" and got["bytes"] == len(jpeg(1200, 1200)), got
from mlo import cover_choice as _cc  # noqa: E402
assert _cc.url_size_hint("https://is1-ssl.mzstatic.com/image/thumb/Music/abc/3000x3000bb.jpg") == 3000
assert _cc.url_size_hint("https://img.test/a.jpg") is None
# ...and it really is on disk under the app's data dir.
assert os.path.isdir(os.path.join(_TMP, "genre_cache")), os.listdir(_TMP)

# An EMPTY answer is a real answer (0 bytes, no container) — which the cover
# policy rejects out loud, and the difference between that and a probe that
# could not run at all (unknown: no answer either way, never a made-up number).
clear_caches()
probes = stub_probe({})
assert intg.image_dimensions("https://img.test/mystery.png") == {
    "format": "", "bytes": 0}, intg.image_dimensions("https://img.test/mystery.png")
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
                   "width": 1400, "height": 1400,
                   # a metadata-only row: nothing probed it, so no container,
                   # no byte count — and COV stated no type for it
                   # the probe for its container answered nothing here (the
                   # stub has no bytes for this URL), which does not reject a
                   # row whose size the provider itself stated
                   "format": "", "bytes": 0, "front": None, "kind": None,
                   "release_cover": None, "rank": 0}, rows[0]
# ...and its rank is the position COV listed it in (its own relevance order)
assert [r["rank"] for r in rows] == [0, 1, 2], rows
# probed from the file, not from the URL's own "500x0w" hint
assert (rows[1]["width"], rows[1]["height"]) == (3000, 3000), rows[1]
assert (rows[2]["width"], rows[2]["height"]) == (None, None), rows[2]
# The row that STATED its own size is probed too — for the container the policy
# judges it on (and the probe's own reading wins when it has one; this row's
# probe answered nothing, so its own 1400 survives).
assert sorted(probes) == sorted(["https://img.test/b.jpg", IMG]), probes

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

# (a) the primary answered → the album's own Cover Art Archive reference is
# still read (that group image is what the candidates are compared against and
# what the policy prefers), while no name-based fallback provider is asked.
clear_caches()
stub_cov(cover_lines(1))
calls = stub_json({"coverartarchive.org": {"images": []}})
probes = stub_probe({IMG: png(1000, 1000)})
out = intg.cover_search("Radiohead", "OK Computer", cfg=CFG,
                        release_group_mbid=CAA_RG)
assert out["provider"] == "cov", out
assert [c[0] for c in calls] == [f"{intg.CAA_BASE}/release-group/{CAA_RG}"], calls
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
# Sorted, not ordered: the dimensions of several rows are probed in a pool,
# so which probe lands in the recorder first is a scheduling detail. What
# matters — and what the rows above assert — is that each row got ITS OWN
# size back.
assert sorted(probes) == sorted([CAA_IMAGE,
                                 "https://coverartarchive.org/release/abc/back.png"]), probes

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
assert sorted(probes) == sorted([BIG3000,
                                 "https://is1-ssl.mzstatic.com/image/thumb/Music/x/3000x3000bb.jpg"]), probes

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
assert out["results"] == [] and out["provider"] is None, out
# ...and every source that was asked (or never asked) says so: the report the
# cover policy turns into the pick's own notes.
assert [(s["id"], s["status"]) for s in out["sources"]][:2] == [
    ("covers.musichoarders.xyz", "empty"), ("coverartarchive", "skipped")], out["sources"]
assert any("no release id" in (s.get("detail") or "") for s in out["sources"]), out["sources"]
assert any(s["id"] == "deezer" and s["status"] == "empty" for s in out["sources"]), out["sources"]
assert any(s["id"] == "itunes" and s["status"] == "empty" for s in out["sources"]), out["sources"]
# ...and a provider that errors is skipped, not fatal
clear_caches()
stub_cov([])


def _boom(*_a, **_k):
    raise RuntimeError("deezer down")


stub_json({"api.deezer.com": _boom})
out = intg.cover_search("Nobody", "Nothing", cfg=CFG)
assert out["results"] == [] and out["provider"] is None, out
assert any(s["id"] == "deezer" and s["status"] == "error" for s in out["sources"]), out["sources"]

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
# 7) The import cover step: `cover_review` on stages the candidates
# --------------------------------------------------------------------------- #
# `run_cover_step` identifies an album by its own tags and hands the review
# file what the finder returned. Building real tagged audio here would test
# mutagen, so the tags come from a table; the HTTP seams above are the ones
# the finder actually uses.
from mlo import audio as mlo_audio
from server import imports as imp

MUSIC = os.path.join(_TMP, "music")
REVIEW_FILE = os.path.join(MUSIC, ".mlo", "data", "metadata_review.json")
# The no-op shape: nothing wanted or nothing to do — the same answer for an
# album that already has art and for a switch that is off.
NOOP = {"fetched": False, "applied": {}, "source": None, "note": "",
        "staged": False, "candidates": 0, "choice": None, "notes": []}

_TAGS = {}


class FakeAudioFile:
    """`mlo.audio.AudioFile`, minus mutagen: the step reads artist/album/the
    release-group id straight off the tags."""

    def __init__(self, path):
        self.path = path
        self.audio = object()          # not None → the tags are read
        self.tags = _TAGS.get(os.path.normpath(str(path)), {})

    def get_tag(self, name):
        return self.tags.get(name)


mlo_audio.AudioFile = FakeAudioFile


def album(name, artist="Radiohead", title="OK Computer", rg="", cover=False,
          album_id=""):
    """A folder with one audio file carrying these TAGS, and maybe a cover."""
    path = os.path.join(MUSIC, "Artists", name)
    os.makedirs(path, exist_ok=True)
    track = os.path.join(path, "01 - Airbag.flac")
    open(track, "wb").close()
    _TAGS[os.path.normpath(track)] = {"ALBUMARTIST": artist, "ALBUM": title,
                                      "MUSICBRAINZ_RELEASEGROUPID": rg,
                                      "MUSICBRAINZ_ALBUMID": album_id}
    if cover:
        open(os.path.join(path, "cover.jpg"), "wb").close()
    return path


# The shipped defaults, straight from the config schema: on, and an explicit
# "off" round-trips instead of being defaulted back on.
assert mlo_config.normalize_config({})["cover_review"] is True
assert mlo_config.normalize_config({})["cover_auto_fetch"] is True
assert mlo_config.normalize_config({"cover_review": False})["cover_review"] is False
assert mlo_config.DEFAULT_CONFIG["cover_review"] is True
assert mlo_config.DEFAULT_CONFIG["cover_auto_fetch"] is True
# a review is a pick-one screen, so the step asks for a screenful
assert imp.COVER_REVIEW_LIMIT == 12

REVIEW_ON = {"music_folder": MUSIC, "cover_review": True}
# A pass-through recorder around the finder: the review limit is applied a
# second time to the staged rows, so only the CALL shows which limit the step
# actually asked the provider for.
_real_search = intg.cover_search
asked = []


def recording_search(artist, album, **kw):
    asked.append(kw)
    return _real_search(artist, album, **kw)


intg.cover_search = recording_search

staged_album = album("Radiohead/OK Computer", album_id="rel-1")
clear_caches()
stub_probe({})
calls = stub_cov(cover_lines(20, width=1200, height=1200,
                             release=release_of("Radiohead", "OK Computer", 12)))
stub_json({})
out = imp.run_cover_step(staged_album, REVIEW_ON)
# staged, NOT fetched, and the count is the review limit — not the finder's
# own default of 40, and not the single hit the auto-apply used to take.
assert (out["staged"], out["fetched"], out["candidates"]) == (True, False, 12), out
assert out["source"] == "cov" and "12 cover" in out["note"], out
assert out["choice"] and out["choice"]["big"] == "https://img.test/a0.jpg", out["choice"]
assert "best: itunes" in out["note"], out["note"]
assert set(out) == set(NOOP), out
assert asked[0]["limit"] == imp.COVER_REVIEW_LIMIT, asked
# The album's own release id (rel-1, from its tags) is an IDENTITY: the Cover
# Art Archive was asked for THIS release's own cover, found none, and said so —
# and the name-based fallbacks were never asked, because the meta-search
# answered. Every one of those facts is in the notes the picker shows.
assert any(n.startswith("coverartarchive: answered with no covers") for n in out["notes"]), out["notes"]
assert any(n.startswith("deezer: skipped") for n in out["notes"]), out["notes"]
assert len(calls) == 1, calls
assert calls[0]["body"]["artist"] == "Radiohead", calls[0]["body"]
assert calls[0]["body"]["album"] == "OK Computer", calls[0]["body"]
# no cover file: the step wrote nothing at all
assert os.listdir(staged_album) == ["01 - Airbag.flac"], os.listdir(staged_album)

# The candidates live in the metadata step's own review file, under the
# album's key, with that step's keys left alone.
review = json.load(open(REVIEW_FILE, encoding="utf-8"))
covers = review[imp._review_key(staged_album)]["covers"]
assert set(covers) == {"artist", "album", "album_id", "release_group",
                       "identity", "provider", "staged_at", "results", "chosen",
                       "notes", "policy"}, covers
assert covers["artist"] == "Radiohead" and covers["album"] == "OK Computer"
# what the candidates were checked against is recorded with them
assert covers["identity"] == {"artist": "Radiohead", "album": "OK Computer",
                              "tracks": None}, covers["identity"]
assert covers["release_group"] == "" and covers["provider"] == "cov"
# `album_id` is the release the album's tags name — the second identity the
# lookup can fall back on when the import chain has moved the album.
assert covers["album_id"] == "rel-1", covers["album_id"]
assert len(covers["staged_at"]) == 20 and covers["staged_at"].endswith("Z"), \
    covers["staged_at"]
# the provider's rows, ranked best first by the ONE cover policy: every row
# carries what it was measured from plus the reasons that put it there, and the
# winner is recorded separately so the picker opens ON the pick.
assert len(covers["results"]) == 12, len(covers["results"])
first = covers["results"][0]
assert {k: first[k] for k in ("source", "small", "big", "title", "artist",
                              "tracks", "url", "width", "height")} == {
    "source": "itunes", "small": "https://img.test/a0-500.jpg",
    "big": "https://img.test/a0.jpg", "title": "OK Computer",
    "artist": "Radiohead", "tracks": 12,
    "url": "https://rel/", "width": 1200, "height": 1200}, first
assert first["rejected"] is None and first["score"] > 0, first
assert first["reasons"] and "1200px cover target" in " ".join(first["reasons"]), first
assert covers["chosen"]["big"] == "https://img.test/a0.jpg", covers["chosen"]
assert covers["policy"]["minimum"] == 1200, covers["policy"]
# identical rows tie on every rule, so the provider's own order (the position
# it listed them in) keeps them in the order it sent them
assert [r["big"] for r in covers["results"]] == \
    [f"https://img.test/a{i}.jpg" for i in range(12)], covers["results"]

# The import chain relocates the album (beets/organize), so the record must be
# findable from a path the import never saw: `staged_metadata` falls back to
# the album folder name and then to the album's own MusicBrainz ids, read from
# the tags of whichever folder the page is looking at.
_moved = album("Someone Else/OK Computer (2017 remaster)", artist="Radiohead",
               title="OK Computer", album_id="rel-1")
assert imp.staged_metadata(_moved, REVIEW_ON)["covers"]["provider"] == "cov", \
    "moved album lost its staged covers"
assert imp.staged_metadata(staged_album, REVIEW_ON) == imp.staged_metadata(_moved, REVIEW_ON), \
    "the moved lookup returned a different record"

# A name search answers with karaoke, tribute and 8-bit releases too — the
# album's own identity is what tells them apart, and it reaches the pick from
# the album folder (its tags) through `cover_candidates` into the policy. Here
# EVERY row is another artist's release: the review says there is nothing to
# pick rather than offering the wrong album's art as the best of what answered,
# and each row keeps its own rejection for the manual list.
karaoke_album = album("Radiohead/Kid A", title="Kid A")
clear_caches()
stub_cov(cover_lines(3, width=1400, height=1400,
                     release=release_of("Vitamin String Quartet",
                                        "Strung Out On Kid A", 10)))
stub_json({})
out = imp.run_cover_step(karaoke_album, REVIEW_ON)
assert (out["staged"], out["candidates"], out["choice"]) == (True, 3, None), out
assert "no cover to pick" in out["note"], out["note"]
karaoke_entry = imp.staged_metadata(karaoke_album, REVIEW_ON)["covers"]
assert karaoke_entry["identity"] == {"artist": "Radiohead", "album": "Kid A",
                                     "tracks": None}, karaoke_entry["identity"]
assert all(r["rejected"] for r in karaoke_entry["results"]), karaoke_entry["results"]
assert all("a different artist's release" in r["rejected"]
           for r in karaoke_entry["results"]), karaoke_entry["results"]
# Nothing was written for the album either — a staged review never writes.
assert os.listdir(karaoke_album) == ["01 - Airbag.flac"], os.listdir(karaoke_album)

# The same query with the real album among the rows: the karaoke row is from
# the PREFERRED source at the same size, so without the identity check it is
# the pick — with it, the album's own cover is, and the loser says why.
mixed_album = album("Radiohead/Amnesiac", title="Amnesiac")
clear_caches()
stub_cov(cover_lines(1, source="qobuz", width=1200, height=1200,
                     big="https://img.test/k{}.jpg",
                     release=release_of("Molotov Cocktail Piano",
                                        "MCP Performs Radiohead: Amnesiac", 11))
         + cover_lines(1, source="itunes", width=1200, height=1200,
                       big="https://img.test/real{}.jpg",
                       release=release_of("Radiohead", "Amnesiac", 11)))
stub_json({})
out = imp.run_cover_step(mixed_album, REVIEW_ON)
assert out["choice"] and out["choice"]["big"] == "https://img.test/real0.jpg", out
mixed_entry = imp.staged_metadata(mixed_album, REVIEW_ON)["covers"]
assert out["source"] == "cov" and out["candidates"] == 2, out
assert [r["source"] for r in mixed_entry["results"] if not r["rejected"]] == ["itunes"], \
    mixed_entry["results"]
assert mixed_entry["results"][-1]["rejected"] and \
    "different artist" in mixed_entry["results"][-1]["rejected"], mixed_entry["results"]

# The album's own TRACKLIST is the third fact a row is checked against: the
# recorded release manifest (what the add path and the import write) says 12,
# so the 23-track reissue ranks below the 12-track row of the same name.
manifest_album = album("Radiohead/Hail to the Thief", title="Hail to the Thief")
from mlo.paths import save_expected_tracks  # noqa: E402
assert save_expected_tracks(manifest_album, "rel-htt",
                            [{"disc": 1, "position": i, "title": f"T{i}"}
                             for i in range(1, 13)]) is True
clear_caches()
stub_cov(cover_lines(1, source="qobuz", width=1200, height=1200,
                     big="https://img.test/reissue{}.jpg",
                     release=release_of("Radiohead",
                                        "Hail to the Thief (Collector's Edition)", 23))
         + cover_lines(1, source="itunes", width=1200, height=1200,
                       big="https://img.test/htt{}.jpg",
                       release=release_of("Radiohead", "Hail to the Thief", 12)))
stub_json({})
out = imp.run_cover_step(manifest_album, REVIEW_ON)
assert out["choice"] and out["choice"]["big"] == "https://img.test/htt0.jpg", out
htt_entry = imp.staged_metadata(manifest_album, REVIEW_ON)["covers"]
assert htt_entry["identity"]["tracks"] == 12, htt_entry["identity"]
assert "23 track(s)" in " ".join(htt_entry["results"][-1]["reasons"]), \
    htt_entry["results"][-1]["reasons"]

# The DEFAULT is the review: a config that says nothing about it stages too.
default_album = album("Blur/Think Tank", artist="Blur", title="Think Tank")
clear_caches()
stub_cov(cover_lines(3, width=1000, height=1000,
                     release=release_of("Blur", "Think Tank")))
stub_json({})
out = imp.run_cover_step(default_album, {"music_folder": MUSIC})
assert (out["staged"], out["fetched"], out["candidates"]) == (True, False, 3), out

# A row with no image URL cannot be applied at all (`POST /api/cover/fromurl`
# takes a URL), so the policy REJECTS it — and it is still reported, with the
# reason, rather than being dropped in silence. The usable rows are unaffected.
nowrite_album = album("Muse/Origin of Symmetry", artist="Muse",
                      title="Origin of Symmetry")
clear_caches()
lines = cover_lines(3, width=1400, height=1400,
                    release=release_of("Muse", "Origin of Symmetry"))
lines.append(json.dumps({"type": "cover", "source": "itunes",
                         "releaseInfo": {"title": "Origin of Symmetry",
                                          "artist": "Muse"}}))
stub_cov(lines)
stub_json({})
out = imp.run_cover_step(nowrite_album, REVIEW_ON)
assert (out["staged"], out["candidates"]) == (True, 4), out
staged = imp.staged_metadata(nowrite_album, REVIEW_ON)["covers"]["results"]
assert [r["big"] for r in staged if not r["rejected"]] == \
    [f"https://img.test/a{i}.jpg" for i in range(3)], staged
assert staged[-1]["big"] is None and "no image URL" in staged[-1]["rejected"], staged[-1]

# A release-group id in the tags asks the Cover Art Archive BY IDENTITY (the
# stand-in, by id — no name guessing) AND the meta-search by name: both carry
# candidates the other does not, and the policy ranks them together. The
# identity read runs only when the name search had nothing, so here the CAA
# answers and the fallback chain supplies the rows.
rg_album = album("Radiohead/Amnesiac", title="Amnesiac", rg=CAA_RG)
clear_caches()
cov_calls = stub_cov([])
jcalls = stub_json({"coverartarchive.org": {"images": [
    {"front": i == 0, "types": ["Front"] if i == 0 else ["Back"],
     "image": f"https://coverartarchive.org/release/rg/{i}.png",
     "thumbnails": {"large": f"https://coverartarchive.org/release/rg/{i}-500.jpg"}}
    for i in range(15)]}})
stub_probe({f"https://coverartarchive.org/release/rg/{i}.png": jpeg(1500, 1500)
            for i in range(15)})
out = imp.run_cover_step(rg_album, REVIEW_ON)
assert (out["staged"], out["candidates"], out["source"]) == \
    (True, 12, "coverartarchive"), out
# the meta-search WAS asked (by name) and had nothing — which is why the
# identity read ran at all: an empty answer is a fallback case, never a
# silent one
assert len(cov_calls) == 1 and cov_calls[0]["body"]["artist"] == "Radiohead", cov_calls
assert [c[0] for c in jcalls] == [f"{intg.CAA_BASE}/release-group/{CAA_RG}"], jcalls
entry = imp.staged_metadata(rg_album, REVIEW_ON)["covers"]
assert entry["release_group"] == CAA_RG and len(entry["results"]) == 12, entry
# the group's images ARE the album's own art — the reference the policy
# prefers — and the policy says so in the row's own reasons
assert entry["results"][0]["release_cover"] is False, entry["results"][0]
assert entry["results"][0]["kind"] == "front", entry["results"][0]
assert "release group's front cover" in " ".join(entry["results"][0]["reasons"]), entry["results"][0]

# The metadata step may have staged this very album: its keys survive.
imp.stage_metadata(staged_album, {"artist": "Radiohead",
                                  "album": "OK Computer",
                                  "candidates": [{"url": "https://artist.test/x.jpg"}]},
                   REVIEW_ON)
clear_caches()
stub_cov(cover_lines(4, width=1000, height=1000,
                     release=release_of("Radiohead", "OK Computer", 12)))
stub_json({})
out = imp.run_cover_step(staged_album, REVIEW_ON)
entry = imp.staged_metadata(staged_album, REVIEW_ON)
assert entry["candidates"] == [{"url": "https://artist.test/x.jpg"}], entry
assert entry["artist"] == "Radiohead" and entry["album"] == "OK Computer", entry
assert len(entry["covers"]["results"]) == 4, entry


# --------------------------------------------------------------------------- #
# 8) `cover_review` off: the best hit is written here; the two no-ops
# --------------------------------------------------------------------------- #
# The step reaches back into `server.main` for the cover page's own writer
# (download + normalise + store), so that is what gets stubbed here.
from server import main as srv_main

fetched = []
written = []


def fake_url_bytes(url, artist="", album="", rg=""):
    fetched.append({"url": url, "artist": artist, "album": album, "rg": rg})
    return png(600, 600), "image/png"


def fake_write_cover(album_dir, stem, ext, data):
    written.append({"album_dir": album_dir, "stem": stem, "ext": ext,
                    "data": data})
    return {"path": os.path.join(album_dir, stem + ext)}


srv_main._cover_url_bytes = fake_url_bytes
srv_main._write_cover_bytes = fake_write_cover
srv_main._sniff_image_ext = lambda data, ctype=None: ".png"

staged_before = copy.deepcopy(imp.staged_metadata(staged_album, REVIEW_ON))
clear_caches()
calls = stub_cov(cover_lines(5, width=1400, height=1400,
                             release=release_of("Radiohead", "OK Computer", 12)))
stub_json({})
out = imp.run_cover_step(staged_album, {"music_folder": MUSIC,
                                        "cover_review": False})
assert (out["fetched"], out["staged"], out["candidates"]) == (True, False, 5), out
assert out["source"] == "cov" and out["note"].startswith("cover fetched"), out
# no review: the SAME candidate set is ranked (the pick must be the best of what
# exists, not whatever answered first) and the winner is written — so both modes
# ask for the same limit.
assert asked[-1]["limit"] == imp.COVER_REVIEW_LIMIT, asked
assert out["applied"] == {"cover": os.path.join(staged_album, "cover.png")}, out
assert out["choice"]["big"] == "https://img.test/a0.jpg", out["choice"]
assert "best of 5 candidate(s)" in out["note"], out["note"]
# the winner went through the writer, with the album's identity
assert fetched == [{"url": "https://img.test/a0.jpg", "artist": "Radiohead",
                    "album": "OK Computer", "rg": ""}], fetched
assert [w["album_dir"] for w in written] == [staged_album], written
assert written[0]["stem"] == "cover" and written[0]["data"] == png(600, 600), written
# the file is the writer's, and the staged set an earlier step left is untouched
assert not os.path.exists(os.path.join(staged_album, "cover.png"))
assert imp.staged_metadata(staged_album, REVIEW_ON) == staged_before, \
    imp.staged_metadata(staged_album, REVIEW_ON)

# Every candidate is below the cover target (the floor the write path and the
# grader both call the minimum): the step writes NOTHING and says why, rather
# than quietly taking the smallest image the internet had. The candidates are
# still returned, so a user can pick one by hand.
clear_caches()
calls = stub_cov(cover_lines(3, width=800, height=800,
                             release=release_of("Muse", "Origin of Symmetry")))
stub_json({})
fetched.clear()
out = imp.run_cover_step(nowrite_album, {"music_folder": MUSIC,
                                         "cover_review": False})
assert (out["fetched"], out["applied"]) == (False, {}), out
assert "no candidate could be used" in out["note"], out["note"]
assert "below the minimum 1200×1200" in out["note"], out["note"]
assert fetched == [], fetched
assert out["candidates"] == 3 and out["choice"] is None, out

# ...and the floor holds at the WRITE too, not only where the winner was picked:
# a chosen candidate below the library's minimum — or one whose size was never
# measured while that minimum is set — is never downloaded and never stored,
# whatever a future change to the ranking decides. The policy already refuses
# both kinds, so this is the step's own guard against that ranking being wrong;
# a stubbed candidate set stands in for the mistake, and the note names it.
real_candidates = imp.cover_candidates
gate_album = album("Muse/Absolution", artist="Muse", title="Absolution")


def stub_candidates(chosen):
    def fake(album_dir, cfg=None):
        return {"chosen": chosen, "candidates": [chosen], "candidate_count": 1,
                "notes": [], "provider": "cov", "artist": "Muse",
                "album": "Absolution", "album_id": "", "release_group": "",
                "policy": {}, "identity": {}}

    imp.cover_candidates = fake


fetched.clear()
written.clear()
stub_candidates({"source": "itunes", "big": "https://img.test/small.jpg",
                 "width": 800, "height": 800, "reasons": ["the best of what answered"]})
out = imp.run_cover_step(gate_album, {"music_folder": MUSIC, "cover_review": False})
assert (out["fetched"], out["applied"]) == (False, {}), out
assert fetched == [] and written == [], (fetched, written)
assert "below the minimum 1200×1200" in out["note"], out["note"]

stub_candidates({"source": "itunes", "big": "https://img.test/unmeasured.jpg",
                 "width": None, "height": None, "reasons": []})
out = imp.run_cover_step(gate_album, {"music_folder": MUSIC, "cover_review": False})
assert (out["fetched"], out["applied"]) == (False, {}), out
assert fetched == [] and written == [], (fetched, written)
assert "size never measured" in out["note"], out["note"]

# A candidate that DOES clear the floor still goes through the writer: the gate
# is the floor, not a new refusal of everything.
stub_candidates({"source": "itunes", "big": "https://img.test/at_target.jpg",
                 "width": 1200, "height": 1200, "reasons": ["at the target"]})
out = imp.run_cover_step(gate_album, {"music_folder": MUSIC, "cover_review": False})
imp.cover_candidates = real_candidates
assert out["fetched"] is True and fetched[-1]["url"] == "https://img.test/at_target.jpg", out
assert written[-1]["album_dir"] == gate_album, written[-1]

# `cover_auto_fetch` off: nothing is fetched, staged or written, with either
# value of cover_review — the finder is not even asked.
off_album = album("Portishead/Dummy", artist="Portishead", title="Dummy")
clear_caches()
calls = stub_cov(cover_lines(3, width=1000, height=1000))
stub_json({})
wrote = (len(fetched), len(written))
for off_cfg in ({"music_folder": MUSIC, "cover_auto_fetch": False},
                {"music_folder": MUSIC, "cover_auto_fetch": False,
                 "cover_review": True},
                {"music_folder": MUSIC, "cover_auto_fetch": False,
                 "cover_review": False}):
    assert imp.run_cover_step(off_album, off_cfg) == NOOP, off_cfg
assert calls == [] and (len(fetched), len(written)) == wrote, (calls, fetched, written)
assert imp.staged_metadata(off_album, REVIEW_ON) == {}

# An album that already has art: the same no-op in all three modes, and still
# no staged entry (a covered album's staged set is not a thing the UI reads).
covered = album("Air/Moon Safari", artist="Air", title="Moon Safari", cover=True)
clear_caches()
calls = stub_cov(cover_lines(3, width=1000, height=1000))
stub_json({})
results = [imp.run_cover_step(covered, mode) for mode in (
    {"music_folder": MUSIC},
    {"music_folder": MUSIC, "cover_review": False},
    {"music_folder": MUSIC, "cover_auto_fetch": False})]
assert results == [NOOP] * 3, results
assert calls == [] and (len(fetched), len(written)) == wrote, (calls, fetched, written)
assert imp.staged_metadata(covered, REVIEW_ON) == {}
assert sorted(os.listdir(covered)) == ["01 - Airbag.flac", "cover.jpg"], \
    sorted(os.listdir(covered))

# Nobody has anything: a note that says so — never an exception, never a
# staged entry with no candidates in it.
empty_album = album("Nobody/Nothing", artist="Nobody", title="Nothing")
clear_caches()
calls = stub_cov([])
jcalls = stub_json({})
out = imp.run_cover_step(empty_album, REVIEW_ON)
assert (out["staged"], out["fetched"], out["candidates"]) == (False, False, 0), out
assert "no cover" in out["note"], out
assert len(calls) == 1 and jcalls, (calls, jcalls)   # the fallbacks found none either
assert imp.staged_metadata(empty_album, REVIEW_ON) == {}


# --------------------------------------------------------------------------- #
# 9) The lyrics ranking
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
