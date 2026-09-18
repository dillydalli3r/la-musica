#!/usr/bin/env python3
"""RateYourMusic album/artist link resolution — offline contract.

What this pins, with the HTTP layer stubbed (no network at all):

  * MusicBrainz is asked FIRST and a rateyourmusic.com url relation it states
    is accepted as the link — for the album (release group) and the artist —
    with no RYM request, and only what MB could not state is scraped;
  * the slug candidates a name produces (accents, `&` → `and`, `The …`,
    punctuation, collapsed dashes);
  * the FIRST candidate that RYM confirms wins — exact slug, then the
    de-`the`-ed one, then RYM's own search page;
  * a Cloudflare interstitial, a 404, and a 200 that landed somewhere else
    are all rejected and fall through — never accepted as a link;
  * a blocked RYM costs ONE probe for the whole process: the ladder is not
    walked, later lookups make no request at all, and the note says what the
    user can do (set rym_cookie, or rely on the MusicBrainz links);
  * a lookup that resolves nothing writes NO link (imports.stamp_rym_links),
    reports "could not resolve" and raises nothing;
  * a link already on the album is never looked up and never overwritten,
    and an unreachable RYM is not an exception;
  * the 30-day cache answers the second call instead of the network;
  * what a pasted URL points at (integrations.rym_url_kind): the release
    album/single/song and /song/ paths, /artist/, a label page ("other"),
    and — the one the UI must refuse — a URL that only MENTIONS
    rateyourmusic.com inside its query string (None).

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


def mb_payload(rym_url="", artist_mbid=""):
    """One MusicBrainz entity payload: its `url-rels` as MB sends them
    (rateyourmusic.com is the "other databases" relation), and the artist
    credit a release group carries."""
    rels = ([{"type": "other databases", "url": {"resource": rym_url}}]
            if rym_url else [])
    credit = [{"artist": {"id": artist_mbid}}] if artist_mbid else []
    return {"relations": rels, "artist-credit": credit}


class FakeMusicBrainz:
    """MusicBrainz stub: `entities` keyed "<entity>/<mbid>", `search` keyed by
    entity name. Every other call raises — a case that expects MB to have
    nothing to say is also a case where MB answers nothing."""

    KEYS = {"artist": "artists", "release-group": "release-groups",
            "release": "releases", "recording": "recordings"}

    def __init__(self, entities=None, search=None):
        self.entities = dict(entities or {})
        self.search = dict(search or {})
        self.calls = []

    def get(self, endpoint, params=None, **kwargs):
        self.calls.append(endpoint)
        if (params or {}).get("query"):
            rows = self.search.get(endpoint)
            if rows is None:
                raise intg.MusicBrainzError(f"nothing stubbed: {endpoint} search")
            return {self.KEYS[endpoint]: list(rows)}
        if endpoint in self.entities:
            return self.entities[endpoint]
        raise intg.MusicBrainzError(f"nothing stubbed: {endpoint}")


def run(routes, boom=False, cache=None, mb=None):
    """Point integrations at the stubs for one scenario; fresh cache dir.

    MusicBrainz defaults to a stub that answers NOTHING, so every case that
    is about the RYM ladder is asked no network at all."""
    intg.httpx = FakeHttpx(routes, boom=boom)
    intg.mb_get_cached = (mb or FakeMusicBrainz()).get
    intg._rym_cache_dir = lambda: cache or tempfile.mkdtemp(prefix="mlo_rym_")
    intg.RYM_MIN_INTERVAL = 0.0
    intg._rym_cookie = lambda cfg=None: ""
    intg._rym_warned = False
    return intg.httpx


_real_httpx = intg.httpx
_real_mb_get_cached = intg.mb_get_cached
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
    # the note is what the user acts on: a blocked RYM says what to do about it
    assert got == {"album": None, "artist": None,
                   "note": intg._RYM_BLOCKED_NOTE}, got
    assert got["note"].startswith("could not resolve"), got
    assert "Cloudflare" in got["note"] and "rym_cookie" in got["note"], got
    assert "challenge" in log.getvalue(), log.getvalue()
    assert len(fake.calls) == 1, fake.calls      # blocked is not retried
    # …and the refusal sticks for the process: the next lookup costs no
    # request at all (RYM used to walk its slug set for every album again)
    assert intg.rym_links("Rihanna", "Loud", cfg=CFG) == got
    assert len(fake.calls) == 1, fake.calls

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
    # ----------------------------------------------------------------------- #
    # 9) what a pasted URL points at — the field the link editor writes to
    # ----------------------------------------------------------------------- #
    assert intg.rym_url_kind(f"{BASE}/release/album/rihanna/loud/") == "album"
    assert intg.rym_url_kind(f"{BASE}/release/single/rihanna/loud/") == "album"
    assert intg.rym_url_kind(f"{BASE}/release/song/rihanna/loud/") == "song"
    assert intg.rym_url_kind(f"{BASE}/song/rihanna/loud/") == "song"
    assert intg.rym_url_kind(f"{BASE}/artist/rihanna") == "artist"
    # a real page that is none of the three link fields
    assert intg.rym_url_kind(f"{BASE}/label/def-jam/") == "other"
    assert intg.rym_url_kind(f"{BASE}/list/user/123/") == "other"
    # the case matters: /release/album/... is an album, /release/song/... a song
    assert intg.rym_url_kind(f"{BASE}/release/album/rihanna/loud/") != \
        intg.rym_url_kind(f"{BASE}/release/song/rihanna/loud/")
    # formatting the user's paste carries: www, a trailing query, case
    assert intg.rym_url_kind("https://www.rateyourmusic.com/artist/rihanna") == "artist"
    assert intg.rym_url_kind(f"{BASE}/artist/rihanna?spotlight=1") == "artist"
    assert intg.rym_url_kind("HTTPS://RATEYOURMUSIC.COM/artist/rihanna") == "artist"
    # NOT rateyourmusic.com: refused, never "other" — and a URL that merely
    # MENTIONS rateyourmusic.com in its query string is still not a RYM URL
    assert intg.rym_url_kind("https://example.com/artist/rihanna") is None
    assert intg.rym_url_kind("https://notrateyourmusic.com/artist/rihanna") is None
    assert intg.rym_url_kind(
        "https://example.com/?u=https://rateyourmusic.com/artist/rihanna") is None
    assert intg.rym_url_kind("rateyourmusic.com/artist/rihanna") is None
    assert intg.rym_url_kind("") is None and intg.rym_url_kind(None) is None
    # ----------------------------------------------------------------------- #
    # 10) MusicBrainz is the FIRST source: the RYM page it states as a url
    #     relation IS the link — no RYM request, no cookie, nothing confirmed.
    #     The MBIDs and URLs are the real ones, verified live.
    # ----------------------------------------------------------------------- #
    NIRVANA_GROUP = "fb3770f6-83fb-32b7-85c4-1f522a92287e"
    NIRVANA = "5b11f4ce-a62d-471e-81fc-a69a8278c7da"
    UNPLUGGED = ("https://rateyourmusic.com/release/album/nirvana/"
                 "mtv_unplugged_in_new_york/")
    fake = run({}, mb=FakeMusicBrainz(entities={
        f"release-group/{NIRVANA_GROUP}": mb_payload(UNPLUGGED, artist_mbid=NIRVANA),
        f"artist/{NIRVANA}": mb_payload(f"{BASE}/artist/nirvana"),
    }))
    got = intg.rym_links("Nirvana", "MTV Unplugged in New York", cfg=CFG,
                         mbid=NIRVANA_GROUP)
    assert got == {"album": UNPLUGGED, "artist": f"{BASE}/artist/nirvana",
                   "note": ""}, got
    assert fake.calls == [], fake.calls          # not one request to RYM

    # the release group is found with MB's own search when the caller has no
    # MBID, and only a hit whose TITLE is this album is used — a same-titled
    # release by someone else never contributes a link
    fake = run({}, mb=FakeMusicBrainz(
        search={"release-group": [
            {"id": "aaaaaaaa-0000-0000-0000-000000000001",
             "title": "MTV Unplugged in New York: Tribute"},
            {"id": NIRVANA_GROUP, "title": "MTV Unplugged in New York"}]},
        entities={
            f"release-group/{NIRVANA_GROUP}": mb_payload(UNPLUGGED,
                                                         artist_mbid=NIRVANA),
            f"artist/{NIRVANA}": mb_payload(f"{BASE}/artist/nirvana")}))
    got = intg.rym_links("Nirvana", "MTV Unplugged in New York", cfg=CFG)
    assert got == {"album": UNPLUGGED, "artist": f"{BASE}/artist/nirvana",
                   "note": ""}, got
    assert fake.calls == [], fake.calls

    # ----------------------------------------------------------------------- #
    # 11) only what MB could not state is scraped: MB gives the album, RYM's
    #     own artist page (cookie configured) gives the artist
    # ----------------------------------------------------------------------- #
    fake = run({"/artist/nirvana": ok(artist_page("Nirvana"))},
               mb=FakeMusicBrainz(entities={
                   f"release-group/{NIRVANA_GROUP}": mb_payload(
                       UNPLUGGED, artist_mbid=NIRVANA)}))
    intg._rym_cookie = lambda cfg=None: "cf_clearance=abc"
    got = intg.rym_links("Nirvana", "MTV Unplugged in New York", cfg=CFG,
                         mbid=NIRVANA_GROUP)
    assert got == {"album": UNPLUGGED, "artist": f"{BASE}/artist/nirvana",
                   "note": ""}, got
    assert fake.calls == [f"{BASE}/artist/nirvana"], fake.calls
finally:
    intg.httpx = _real_httpx
    intg.mb_get_cached = _real_mb_get_cached
    intg._rym_cache_dir = _real_cache_dir
    intg._rym_cookie = _real_cookie
    intg.RYM_MIN_INTERVAL = _real_interval
    mlo_audio.AudioFile = _real_audiofile

print("test_rym_links: OK")
