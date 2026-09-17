#!/usr/bin/env python3
"""RateYourMusic album/artist link resolution — offline contract.

What this pins, with the HTTP layer stubbed (no network at all):

  * the slug candidates a name produces (accents, `&` → `and`, `The …`,
    punctuation, collapsed dashes);
  * the FIRST candidate that RYM confirms wins — exact slug, then the
    de-`the`-ed one, then RYM's own search page;
  * a Cloudflare interstitial, a 404, and a 200 that landed somewhere else
    are all rejected and fall through — never accepted as a link;
  * a lookup that resolves nothing writes NO link (imports.stamp_rym_links),
    reports "could not resolve" and raises nothing;
  * a link already on the album is never looked up and never overwritten,
    and an unreachable RYM is not an exception;
  * the 30-day cache answers the second call instead of the network.

Run:  python tools/test_rym_links.py
"""
import contextlib
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo import audio as mlo_audio
from server import imports
from server import integrations as intg

BASE = intg.RYM_BASE


# --------------------------------------------------------------------------- #
# HTTP stub — routes on a URL substring, one (status, text) per route
# --------------------------------------------------------------------------- #
class Response:
    def __init__(self, status, text, url):
        self.status_code, self.text, self.url = status, text, url


class FakeHttpx:
    HTTPError = RuntimeError

    def __init__(self, routes, boom=False):
        self.routes, self.boom, self.calls = dict(routes), boom, []

    def get(self, url, params=None, headers=None, timeout=None,
            follow_redirects=None):
        full = url + ("?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
                      if params else "")
        self.calls.append(full)
        if self.boom:
            raise RuntimeError("no connection")
        for key, route in self.routes.items():
            if key in url:
                got = route(url, params or {})
                status, text = got[0], got[1]
                return Response(status, text, got[2] if len(got) > 2 else url)
        return Response(404, "not found", url)


def release_page(artist, album, artist_slug=None):
    slug = artist_slug if artist_slug is not None else intg._rym_slug(artist)
    return (f'<html><head><title>{album} by {artist}</title></head><body>'
            f'<h1 class="album_title">{album}</h1>'
            f'<a href="/artist/{slug}">{artist}</a></body></html>')


def artist_page(artist):
    return f'<html><body><h1 class="artist_name">{artist}</h1></body></html>'


def search_page(*hrefs):
    links = "".join(f'<a href="{h}">{h}</a>' for h in hrefs)
    return f"<html><body><div id='search_results'>{links}</div></body></html>"


def ok(text):
    return lambda url, params: (200, text)


def status(code, text=""):
    return lambda url, params: (code, text)


def elsewhere(text):
    """A 200 that did not stay on the requested path: RYM answers an unknown
    slug by redirecting to search/home, so the page is not the page asked for
    however plausible its content looks."""
    return lambda url, params: (200, text, BASE + "/search?searchterm=x")


def run(routes, boom=False, cache=None):
    """Point integrations at the stub for one scenario; fresh cache dir."""
    intg.httpx = FakeHttpx(routes, boom=boom)
    intg._rym_cache_dir = lambda: cache or tempfile.mkdtemp(prefix="mlo_rym_")
    intg.RYM_MIN_INTERVAL = 0.0
    intg._rym_cookie = lambda cfg=None: ""
    intg._rym_warned = False
    return intg.httpx


_real_httpx = intg.httpx
_real_cache_dir = intg._rym_cache_dir
_real_cookie = intg._rym_cookie
_real_interval = intg.RYM_MIN_INTERVAL
_real_audiofile = mlo_audio.AudioFile

CFG = {"rym_links_auto": True}
try:
    # ----------------------------------------------------------------------- #
    # 1) slugs
    # ----------------------------------------------------------------------- #
    assert intg._rym_slug_candidates("Björk") == ["bjork"], intg._rym_slug("Björk")
    assert intg._rym_slug_candidates("Simon & Garfunkel") == ["simon-and-garfunkel"]
    assert intg._rym_slug_candidates("The Beatles") == ["the-beatles", "beatles"]
    assert intg._rym_slug_candidates("  The   Wall  ") == ["the-wall", "wall"]
    assert intg._rym_slug_candidates("The The") == ["the-the", "the"]
    assert intg._rym_slug_candidates("!!!") == []
    assert intg._rym_slug("Sgt. Pepper's Lonely Hearts Club Band") == \
        "sgt-peppers-lonely-hearts-club-band"
    assert intg._rym_slug("AC/DC") == "ac-dc"
    assert intg._rym_slug("Motörhead — Overkill (Live)") == "motorhead-overkill-live"
    # the album name is what a page must state: entities and accents fold
    assert intg._rym_mentions("<h1>Simon &amp; Garfunkel</h1>", "Simon & Garfunkel")
    assert intg._rym_mentions("<h1>BJORK</h1>", "Björk")
    assert not intg._rym_mentions("<h1>Bjork</h1>", "Björk", "Homogenic")

    # ----------------------------------------------------------------------- #
    # 2) the exact slug wins, and the album page supplies the artist link
    # ----------------------------------------------------------------------- #
    fake = run({
        "/release/album/the-beatles/abbey-road/":
            ok(release_page("The Beatles", "Abbey Road")),
        "/artist/the-beatles": ok(artist_page("The Beatles")),
    })
    got = intg.rym_links("The Beatles", "Abbey Road", cfg=CFG)
    assert got == {"album": f"{BASE}/release/album/the-beatles/abbey-road/",
                   "artist": f"{BASE}/artist/the-beatles", "note": ""}, got
    assert fake.calls == [f"{BASE}/release/album/the-beatles/abbey-road/",
                          f"{BASE}/artist/the-beatles"], fake.calls

    # ----------------------------------------------------------------------- #
    # 3) the de-`the`-ed slug is tried when the exact one is not there, and a
    #    404 is a miss — it never claims RYM is unreachable
    # ----------------------------------------------------------------------- #
    fake = run({
        "/release/album/pink-floyd/the-wall/": status(404),
        "/release/album/pink-floyd/wall/":
            ok(release_page("Pink Floyd", "The Wall")),
        "/artist/pink-floyd": ok(artist_page("Pink Floyd")),
    })
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        got = intg.rym_links("Pink Floyd", "The Wall", cfg=CFG)
    assert got["album"] == f"{BASE}/release/album/pink-floyd/wall/", got
    assert got["note"] == "", got
    assert log.getvalue() == "", log.getvalue()

    # a slug that 200s on a DIFFERENT path (RYM's search/home) is not the page
    # that was asked for, even when its content mentions the artist + album
    fake = run({
        "/release/album/pink-floyd/the-wall/":
            elsewhere(release_page("Pink Floyd", "The Wall")),
        "/release/album/pink-floyd/wall/": ok(release_page("Pink Floyd", "The Wall")),
        "/artist/pink-floyd": ok(artist_page("Pink Floyd")),
    })
    got = intg.rym_links("Pink Floyd", "The Wall", cfg=CFG)
    assert got["album"] == f"{BASE}/release/album/pink-floyd/wall/", got
    assert f"{BASE}/release/album/pink-floyd/the-wall/" in fake.calls, fake.calls
    # the content was right, the PATH was not: that is what rejected it
    page = release_page("Pink Floyd", "The Wall")
    assert intg._rym_mentions(page, "Pink Floyd", "The Wall")
    assert intg._rym_verified("/release/album/pink-floyd/the-wall/", CFG,
                              "Pink Floyd", "The Wall") is None

    # ----------------------------------------------------------------------- #
    # 4) interstitial / 404 → nothing resolved, nothing raised
    # ----------------------------------------------------------------------- #
    fake = run({"/release/album/": ok("<html><title>Just a moment...</title>"),
                "/artist/": ok("<html><title>Just a moment...</title>")})
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        got = intg.rym_links("Rihanna", "Loud", cfg=CFG)
    assert got == {"album": None, "artist": None,
                   "note": "could not resolve on RateYourMusic"}, got
    assert "challenge" in log.getvalue(), log.getvalue()
    assert len(fake.calls) == 1, fake.calls      # blocked is not retried

    fake = run({})          # every candidate 404s
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        got = intg.rym_links("Rihanna", "Loud", cfg=CFG)
    assert got["album"] is None and got["artist"] is None, got
    assert "could not resolve" in got["note"], got
    assert log.getvalue() == "", log.getvalue()

    # ----------------------------------------------------------------------- #
    # 5) RYM's search page is the last fallback, and only a verified hit is
    #    accepted (a cover version on the same query is not)
    # ----------------------------------------------------------------------- #
    fake = run({
        "/search": ok(search_page("/release/album/cover-band/loud-cover/",
                                  "/release/album/rihanna/loud-2/")),
        "/release/album/cover-band/loud-cover/":
            ok(release_page("Cover Band", "Loud")),
        "/release/album/rihanna/loud-2/": ok(release_page("Rihanna", "Loud")),
        "/artist/rihanna": ok(artist_page("Rihanna")),
    }, cache=tempfile.mkdtemp(prefix="mlo_rym_"))
    got = intg.rym_links("Rihanna", "Loud", cfg=CFG)
    assert got == {"album": f"{BASE}/release/album/rihanna/loud-2/",
                   "artist": f"{BASE}/artist/rihanna", "note": ""}, got
    assert any("/search?" in c for c in fake.calls), fake.calls
    assert f"{BASE}/release/album/cover-band/loud-cover/" in fake.calls, fake.calls

    # ----------------------------------------------------------------------- #
    # 6) cache: the second call answers from disk, no request at all
    # ----------------------------------------------------------------------- #
    cache = tempfile.mkdtemp(prefix="mlo_rym_")
    run({"/release/album/rihanna/loud/": ok(release_page("Rihanna", "Loud")),
         "/artist/rihanna": ok(artist_page("Rihanna"))}, cache=cache)
    first = intg.rym_links("Rihanna", "Loud", cfg=CFG)
    sent = list(intg.httpx.calls)
    assert sent, "the first call must hit the network"
    run({}, cache=cache)
    assert intg.rym_links("Rihanna", "Loud", cfg=CFG) == first
    assert intg.httpx.calls == [], intg.httpx.calls

    # ----------------------------------------------------------------------- #
    # 7) unreachable RYM: no link, no exception
    # ----------------------------------------------------------------------- #
    fake = run({}, boom=True)
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        assert intg.rym_links("Rihanna", "Loud", cfg=CFG)["album"] is None
    assert "rateyourmusic" in log.getvalue(), log.getvalue()
    # RYM is not answering: the rest of the ladder is not attempted
    assert len(fake.calls) == 1, fake.calls

    # the gate: rym_links_auto off means no request at all
    fake = run({})
    got = intg.rym_links("Rihanna", "Loud", cfg={"rym_links_auto": False})
    assert got == {"album": None, "artist": None,
                   "note": "automatic lookup is off"}, got
    assert fake.calls == [], fake.calls

    # ----------------------------------------------------------------------- #
    # 8) stamping an album: verified links only, existing links untouched
    # ----------------------------------------------------------------------- #
    class FakeAudio:
        """Tag-reading stand-in for mlo.audio.AudioFile."""

        written = {}

        def __init__(self, path):
            self.path = path
            self.audio = object()
            self.tags = dict(FakeAudio.written.get(path) or {})

        def get_tag(self, name):
            return self.tags.get(name)

        def set_tag(self, name, value):
            self.tags[name] = value
            FakeAudio.written[self.path] = dict(self.tags)
            return True

    root = tempfile.mkdtemp(prefix="mlo_rym_links_")
    album = os.path.join(root, "Loud")
    os.makedirs(album)
    files = []
    for i in (1, 2):
        path = os.path.join(album, f"{i:02d} - track.wav")
        with open(path, "wb") as fh:
            fh.write(b"")
        FakeAudio.written[path] = {"ARTIST": "Rihanna", "ALBUM": "Loud",
                                   "TITLE": f"track {i}"}
        files.append(path)
    mlo_audio.AudioFile = FakeAudio

    run({"/release/album/rihanna/loud/": ok(release_page("Rihanna", "Loud")),
         "/artist/rihanna": ok(artist_page("Rihanna"))})
    out = imports.stamp_rym_links(album, CFG)
    assert out == {"album": f"{BASE}/release/album/rihanna/loud/",
                   "artist": f"{BASE}/artist/rihanna", "note": "",
                   "written": 2}, out
    for path in files:
        assert FakeAudio.written[path]["RATEYOURMUSIC_ALBUM"] == \
            f"{BASE}/release/album/rihanna/loud/", FakeAudio.written[path]
        assert FakeAudio.written[path]["RATEYOURMUSIC_ARTIST"] == \
            f"{BASE}/artist/rihanna", FakeAudio.written[path]

    # both links already there → not one request, nothing rewritten
    run({})
    before = {p: dict(FakeAudio.written[p]) for p in files}
    out = imports.stamp_rym_links(album, CFG)
    assert out["written"] == 0 and out["album"] is None and out["artist"] is None
    assert intg.httpx.calls == [], intg.httpx.calls
    assert {p: FakeAudio.written[p] for p in files} == before

    # only the artist link missing → only that one is resolved, and the
    # album link the user set is not fetched or overwritten
    run({"/release/album/rihanna/loud/": ok(release_page("Rihanna", "Loud")),
         "/artist/rihanna": ok(artist_page("Rihanna"))})
    for path in files:
        FakeAudio.written[path].pop("RATEYOURMUSIC_ARTIST")
    mine = f"{BASE}/release/album/rihanna/loud/?user-edited"
    FakeAudio.written[files[0]]["RATEYOURMUSIC_ALBUM"] = mine
    out = imports.stamp_rym_links(album, CFG)
    assert out["album"] is None and out["artist"] == f"{BASE}/artist/rihanna", out
    assert FakeAudio.written[files[0]]["RATEYOURMUSIC_ALBUM"] == mine
    for path in files:
        assert FakeAudio.written[path]["RATEYOURMUSIC_ARTIST"] == \
            f"{BASE}/artist/rihanna", FakeAudio.written[path]

    # a lookup that finds nothing writes nothing and still returns a note
    for path in files:
        FakeAudio.written[path].pop("RATEYOURMUSIC_ARTIST", None)
        FakeAudio.written[path].pop("RATEYOURMUSIC_ALBUM", None)
    run({}, boom=True)
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        out = imports.stamp_rym_links(album, CFG)
    assert out["written"] == 0 and out["note"].startswith("could not resolve"), out
    for path in files:
        assert "RATEYOURMUSIC_ALBUM" not in FakeAudio.written[path], FakeAudio.written[path]
        assert "RATEYOURMUSIC_ARTIST" not in FakeAudio.written[path], FakeAudio.written[path]
finally:
    intg.httpx = _real_httpx
    intg._rym_cache_dir = _real_cache_dir
    intg._rym_cookie = _real_cookie
    intg.RYM_MIN_INTERVAL = _real_interval
    mlo_audio.AudioFile = _real_audiofile

print("test_rym_links: OK")
