#!/usr/bin/env python
"""Album of the Year: the live-fetch wiring and the pure parsers.

The fixtures below are trimmed copies of REAL page markup (captured
2026-10-10): an album page with its genre row, user score and per-track
ratings; a song page with the song's own score; an artist page; and a search
page. Nothing here reaches the network — `aoty.fetch` is stubbed — so what
this suite proves is the parsing and the resolution, which is everything the
app controls. Whether a live page can be CLEARED is a different question,
answered by `tools/test_cfchallenge.py` (the solver) and by a real cookie.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import aoty                                                     # noqa: E402
from server import integrations as intg                                     # noqa: E402

PASS = 0
FAIL = 0


def ok(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"FAILED: {label}")


# --------------------------------------------------------------------------- #
# Fixtures (trimmed real markup)
# --------------------------------------------------------------------------- #
ALBUM = '''<html><head><title>Radiohead - The Bends</title></head><body>
<div class="albumUserScoreBox"><div class="heading">User Score</div>
<div class="albumUserScore"><a href="#users" title="85.7">86</a></div>
<div class="text numReviews">Based on <a href="/user-reviews/?type=ratings"><strong>13</strong></a>&nbspratings</div></div>
<div class="detailRow"><a href="/genre/2-alternative-rock/">Alternative Rock</a>,
<a href="/genre/638-post-britpop/">Post-Britpop</a><br />
<a href="/genre/99-art-rock/"><div class="secondary">Art Rock</div></a>
<span>/&nbsp;Genre</span></div>
<div class="trackList" id="tracklist"><table class="trackListTable">
<tr><td class="trackNumber">1</td><td class="trackTitle"><a href="/song/6414-planet-telex/">Planet Telex</a><div class="length">4:19</div></td><td class="trackRating"><span class="green-font" title="3905 Ratings">90</span></td></tr>
<tr><td class="trackNumber">2</td><td class="trackTitle"><a href="/song/6415-the-bends/">The Bends</a><div class="length">4:06</div></td><td class="trackRating"><span class="green-font" title="3866 Ratings">89</span></td></tr>
</table></div></body></html>'''

SONG = '''<html><head><title>Radiohead - Just - Song Ratings</title></head><body>
<div class="albumHeadline small"><h1 class="songTitle">Just</h1></div>
<div class="songScoreBox"><div class="heading">User Score</div>
<div class="songScore" title="92.6768">93</div>
<div class="text">Based on <strong>3,023</strong>&nbspratings</div></div></body></html>'''

ARTIST = '''<html><body>
<div class="artistUserScoreBox"><div class="heading">User Score</div>
<div class="artistUserScore"><a href="#users" title="80.4">80</a></div>
<div class="text">Based on <strong>1,000</strong>&nbspratings</div></div>
<div class="detailRow"><a href="/genre/2-alternative-rock/">Alternative Rock</a>
<span>/&nbsp;Genre</span></div></body></html>'''

SEARCH = '''<html><body>
<a href="/album/970-radiohead-the-bends.php">The Bends</a>
<a href="/artist/284-radiohead/">Radiohead</a>
<a href="/song/6420-just/">Just</a>
<a href="/album/9999-radiohead-the-bends-live.php">The Bends Live</a>
</body></html>'''


# --------------------------------------------------------------------------- #
# Parsers
# --------------------------------------------------------------------------- #
album = aoty.parse_album(ALBUM)
ok(album is not None, "the album page parses")
ok(album["genres"] == ["Alternative Rock", "Post-Britpop", "Art Rock"],
   f"album genres: {album['genres']}")
ok(album["rating"] == {"value": 85.7, "count": 13},
   f"album user score: {album['rating']}")
ok([t["title"] for t in album["tracks"]] == ["Planet Telex", "The Bends"],
   "album track list")
ok(album["tracks"][0]["rating"] == {"value": 90.0, "count": 3905},
   f"per-track score, count from the row's own title: {album['tracks'][0]}")
ok(aoty.parse_album("") is None, "an empty page parses to None")
ok(aoty.parse_album("<html><body>no score, no rows</body></html>") is None,
   "a page with nothing parses to None")

song = aoty.parse_song(SONG)
ok(song == {"title": "Just", "rating": {"value": 92.6768, "count": 3023}},
   f"song page: {song}")
ok(aoty.parse_song("<html><body>nothing</body></html>") is None,
   "an empty song page is None")

artist = aoty.parse_artist(ARTIST)
ok(artist["rating"] == {"value": 80.4, "count": 1000},
   f"artist score: {artist['rating']}")
ok(artist["genres"] == ["Alternative Rock"], f"artist genres: {artist['genres']}")

ok(aoty.genres_from_html(ALBUM) == album["genres"],
   "genres_from_html agrees with the album parse")


# --------------------------------------------------------------------------- #
# Resolution through the site's own search (fetch stubbed)
# --------------------------------------------------------------------------- #
class _Stub:
    """`aoty.fetch` replaced: a search page for /search, pages by kind."""

    def __call__(self, path, cfg=None):
        if str(path).startswith("/search"):
            return SEARCH
        if "/album/" in path:
            return ALBUM
        if "/song/" in path:
            return SONG
        if "/artist/" in path:
            return ARTIST
        return None


_real_fetch = aoty.fetch
aoty.fetch = _Stub()
try:
    ok(aoty.resolve("album", "Radiohead", "The Bends") ==
       "/album/970-radiohead-the-bends.php",
       "the album with both name words, not the live namesake")
    ok(aoty.resolve("song", "Just") == "/song/6420-just/",
       "the song page resolves (slug carries the title, not the artist)")
    ok(aoty.resolve("artist", "Radiohead") == "/artist/284-radiohead/",
       "the artist page resolves")
    ok(aoty.resolve("album", "Nobody", "Nothing") is None,
       "no matching link -> None")

    ok(intg.aoty_page("Radiohead", "The Bends", cfg={}, kind="album") == ALBUM,
       "integrations.aoty_page resolves then fetches the album")
    ok(intg.aoty_page("Radiohead", "Just", cfg={}, kind="song") == SONG,
       "…and the song")

    # The ratings fetch contract: the parsed payloads the web-rating parsers read.
    f = intg.web_rating_fetchers()
    album_payload = f("albumoftheyear", "album",
                      {"artist": "Radiohead", "album": "The Bends"}, {})
    ok(isinstance(album_payload, dict) and album_payload["rating"]["value"] == 85.7,
       f"album fetcher payload: {album_payload}")
    track_payload = f("albumoftheyear", "track",
                      {"artist": "Radiohead", "title": "Just"}, {})
    ok(track_payload == {"title": "Just",
                         "rating": {"value": 92.6768, "count": 3023}},
       f"track fetcher payload: {track_payload}")
    artist_payload = f("albumoftheyear", "artist", {"artist": "Radiohead"}, {})
    ok(artist_payload["rating"]["value"] == 80.4,
       f"artist fetcher payload: {artist_payload}")
finally:
    aoty.fetch = _real_fetch

# Without a cookie or a solver there is nothing to fetch with: the genre chain
# skips the source rather than asking it to fail.
skip = intg._genre_source_skip("albumoftheyear", {})
ok(isinstance(skip, str) and "aoty_cookie" in skip,
   f"no cookie/solver -> a skip naming aoty_cookie: {skip}")
ok(intg._genre_source_skip("albumoftheyear",
                           {"cf_solver_url": "http://127.0.0.1:8191"}) is None,
   "a solver makes the source askable without a cookie")

print(f"aoty: {PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
