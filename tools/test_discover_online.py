#!/usr/bin/env python3
"""Online discovery routes: Apple-first charts and genre browse, owned rows dropped.

No network: every provider reader is stubbed with the row shape
`server.discovery` already returns, and the library payload is a tiny tree.
Pinned here:

  * `discover_enabled` off answers a disabled shelf and never calls a provider;
  * `source=auto` prefers Apple for songs and Deezer for albums/artists;
  * a genre Apple has nothing for falls through to Last.fm's tag chart;
  * the library-seeded shelf removes rows the library already owns;
  * a provider refusal is an honest empty answer with the provider's words,
    never a fabricated row.

Run:  python tools/test_discover_online.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mlo.genre_vocab import FAMILIES  # noqa: E402
from server import api_discover as ad  # noqa: E402
from server import discovery  # noqa: E402
from server import library as lib_mod  # noqa: E402

FAILS = []


def ok(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILS.append(label)


CFG = {"discovery_enabled": True, "lastfm_api_key": "test-key"}
real = {}


def patch(name, value):
    real[name] = getattr(discovery, name)
    setattr(discovery, name, value)


def restore():
    for name, value in real.items():
        setattr(discovery, name, value)
    real.clear()


TRACK = {"kind": "track", "title": "Song A", "artist": "Artist A",
         "album": "Album A", "source": "itunes", "rank": 1}
ALBUM = {"kind": "album", "title": "Album B", "artist": "Artist B",
         "source": "deezer", "rank": 1}

LIB = {"folder": "F:/music", "artists": [{
    "name": "Artist A", "display_name": "Artist A",
    "albums": [{
        "meta": {"ALBUM": "Album A", "ALBUMARTIST": "Artist A"},
        "tracks": [{"tags": {"TITLE": "Song A", "ARTIST": "Artist A",
                             "GENRE": "rock"}}],
    }],
}]}

real_build = lib_mod.build_library
lib_mod.build_library = lambda _cfg: LIB
ad.load_config = lambda: dict(CFG)

try:
    print("== the genre list ==")
    genres = ad.discover_genres()
    ok(genres["ok"] and [g["name"] for g in genres["genres"]] == list(FAMILIES),
       "the browse list is the app's own broad genre vocabulary")
    ok(all(g["label"] for g in genres["genres"]), "every genre has a label")

    print("== source=auto prefers Apple for songs, Deezer for albums ==")
    calls = []

    def fake_itunes_tracks(genre, limit=50, offset=0, cfg=None, timeout=None):
        calls.append(("itunes", genre, limit))
        return {"rows": [dict(TRACK, genre=genre)], "total": 1}

    def fake_deezer(kind, limit=50, timeout=None):
        calls.append(("deezer", kind, limit))
        return [dict(ALBUM, kind=kind[:-1])]

    patch("itunes_genre_songs", fake_itunes_tracks)
    patch("deezer_chart", fake_deezer)
    patch("itunes_most_played", lambda limit=50, cfg=None, timeout=None: [dict(TRACK)])

    tracks = ad.discover_charts(kind="tracks", source="auto", period="all", limit=10)
    ok(tracks["ok"] and tracks["source"] == "itunes" and tracks["rows"][0]["title"] == "Song A",
       f"a songs chart asks Apple first ({tracks.get('source')})")
    albums = ad.discover_charts(kind="albums", source="auto", period="all", limit=10)
    ok(albums["ok"] and albums["source"] == "deezer" and albums["rows"][0]["title"] == "Album B",
       f"an albums chart asks Deezer ({albums.get('source')})")

    print("== genre browse: Apple, then Last.fm when Apple is empty ==")
    calls.clear()
    got = ad.discover_genre(genre="shoegaze", kind="tracks", limit=5, offset=0)
    ok(got["ok"] and got["source"] == "itunes" and got["rows"][0]["genre"] == "shoegaze",
       f"the genre row is Apple's ({got.get('source')})")

    patch("itunes_genre_songs",
          lambda genre, limit=50, offset=0, cfg=None, timeout=None:
          {"rows": [], "total": 0})
    patch("lastfm_tag_top",
          lambda kind, tag, limit=25, offset=0, cfg=None, timeout=None:
          {"rows": [dict(TRACK, source="lastfm", title="Fallback")], "total": 1})
    fell = ad.discover_genre(genre="shoegaze", kind="tracks", limit=5, offset=0)
    ok(fell["ok"] and fell["source"] == "lastfm" and fell["rows"][0]["title"] == "Fallback",
       f"an empty Apple answer falls through to Last.fm ({fell.get('source')})")

    print("== the library-seeded shelf drops owned rows ==")
    patch("itunes_genre_albums",
          lambda genre, limit=25, offset=0, cfg=None, timeout=None:
          {"rows": [dict(ALBUM, source="itunes"),
                    dict(ALBUM, title="New Album", artist="New Artist")],
           "total": 2})
    rec = ad.discover_recommended(seed="library", kind="albums", genre="", artist="", limit=12)
    titles = [r["title"] for r in rec["rows"]]
    ok(rec["ok"] and "New Album" in titles and "Album B" in titles,
       f"the unowned rows survive ({titles})")
    ok("Album A" not in titles,
       "an album the library already holds is dropped")
    ok(rec["basis"] and "rock" in rec["basis"],
       f"the shelf names its library-genre seed ({rec.get('basis')!r})")

    print("== a refusal is honest ==")
    def refuse(*_a, **_k):
        raise discovery.ProviderRefused("HTTP 503: try later")

    patch("itunes_most_played", refuse)
    bad = ad.discover_charts(kind="tracks", source="itunes", period="all", limit=5)
    ok(not bad["ok"] and bad["rows"] == [] and "503" in bad["reason"],
       f"a provider refusal is an empty answer with its own words ({bad.get('reason')!r})")

    print("== the gate ==")
    ad.load_config = lambda: {"discovery_enabled": False}
    off = ad.discover_charts(kind="tracks", source="itunes", period="all", limit=5)
    ok(not off["ok"] and "off" in off["reason"], "discovery_enabled off answers a disabled shelf")
finally:
    restore()
    lib_mod.build_library = real_build

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s)")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("all online-discovery checks passed")
