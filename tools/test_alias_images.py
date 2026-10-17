#!/usr/bin/env python
"""The alias pass for artist images and descriptions (`server/discovery.py`).

Offline and deterministic: every provider is stubbed, so what is proved is the
LADDER — the stored name is asked first, and a name that found nothing is
re-asked under the artist's MusicBrainz aliases, once, with the alias reported
on the hit. Nothing here claims a provider would answer.

WHY: an artist filed under their localized name (`宇多田ヒカル`, not
`Hikaru Utada`) answers only to it, and the name-based image/description
sources otherwise pick whichever act owns the spelling — the same shape the
lyrics and advisory ladders already had.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import discovery  # noqa: E402

PASS = 0
FAIL = 0


def ok(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"FAILED: {label}")


_saved = {name: getattr(discovery, name)
          for name in ("deezer_artist", "audiodb_artist", "audiodb_artist_mbid",
                       "wikipedia_summary", "bandcamp_album_description",
                       "discogs_album_description", "lastfm_album_description",
                       "_alias_names", "source_order")}
try:
    # ---- the artist's alias, from MusicBrainz's own list --------------------
    discovery._alias_names = lambda entity, mbid, cfg, name: (
        ["宇多田ヒカル"] if entity == "artist" and name == "Hikaru Utada" else [])

    # The name-based walk: the stored name answers nothing, the alias does.
    seen = []
    discovery.deezer_artist = lambda name: (
        seen.append(name), {"image": f"photo:{name}"} if name == "宇多田ヒカル" else None)[1]
    discovery.audiodb_artist = lambda name: None
    discovery.audiodb_artist_mbid = lambda mbid: None
    discovery.wikipedia_summary = lambda name: None

    cfg = {"artist_image_sources": ["deezer"]}
    got = discovery.artist_image("Hikaru Utada", mbid="artist-id", cfg=cfg)
    ok(got and got["url"] == "photo:宇多田ヒカル", f"the alias pass finds the photo: {got}")
    ok(seen == ["Hikaru Utada", "宇多田ヒカル"],
       f"the stored name is asked FIRST, then the alias: {seen}")
    ok((got or {}).get("alias_pass", {}).get("query") == "宇多田ヒカル",
       f"…and the hit says which name answered: {got}")

    # A stored name that answers pays no alias pass at all.
    seen.clear()
    discovery.deezer_artist = lambda name: (
        seen.append(name), {"image": f"photo:{name}"})[1]
    got = discovery.artist_image("Hikaru Utada", mbid="artist-id", cfg=cfg)
    ok(seen == ["Hikaru Utada"], f"an answering name is not doubled: {seen}")
    ok("alias_pass" not in got, "…and the hit carries no alias pass")

    # No id -> no alias lookup, and no second pass.
    discovery.deezer_artist = lambda name: None
    ok(discovery.artist_image("Hikaru Utada", cfg=cfg) is None,
       "no mbid -> the ladder stops at the stored name")

    # ---- an album description, by the release group's alias ----------------
    discovery._alias_names = lambda entity, mbid, cfg, name: (
        ["ディープ・リバー"] if entity == "release-group" else [])
    discovery.source_order = lambda cfg, key, default: ["bandcamp"]
    dseen = []
    discovery.bandcamp_album_description = lambda artist, album: (
        dseen.append(album),
        {"text": "A record.", "title": album, "source": "bandcamp"} if album == "ディープ・リバー" else None)[1]
    got = discovery.album_description("Utada Hikaru", "Deep River", mbid="rg-id",
                                      cfg={})
    ok(got and got.get("text") == "A record.", f"the album alias pass answers: {got}")
    ok(dseen == ["Deep River", "ディープ・リバー"],
       f"stored name first, then the alias: {dseen}")
finally:
    for name, value in _saved.items():
        setattr(discovery, name, value)

# --------------------------------------------------------------------------- #
# The streaming services lead the artist-image chain (owner's rule, 2026-10-17)
# --------------------------------------------------------------------------- #
# Spotify is the PRIMARY source and Apple sits after it: Spotify's artist object
# carries a real 640px photo, Apple publishes no artist-photo endpoint at all
# (its entry is the artist's best-known ALBUM artwork). Both are asked only for
# the sources a config lists, and a provider without its credential answers None
# so the walk moves on — a keyless install still gets Deezer's photo.
print()
ok(discovery.IMAGE_SOURCES[0] == "spotify",
   f"Spotify leads the built-in artist-image order ({discovery.IMAGE_SOURCES})")
ok(discovery.IMAGE_SOURCES.index("spotify") < discovery.IMAGE_SOURCES.index("itunes"),
   "…and is preferred over Apple Music")
ok("spotify" in discovery.SOURCES and discovery.SOURCE_LABELS.get("spotify") == "Spotify",
   "Spotify is a catalogue source with its own label")
ok(discovery.spotify_artist_image("Nirvana", cfg={}) is None,
   "without the client id + secret the source answers None (never a guess)")
_saved_hit = discovery._spotify_artist_hit
try:
    discovery._spotify_artist_hit = lambda name, cfg=None, timeout=None: {
        "name": name,
        "images": [{"url": "https://i.scdn.co/640.jpg", "width": 640},
                   {"url": "https://i.scdn.co/300.jpg", "width": 300}],
    }
    ok(discovery.spotify_artist_image("Nirvana", cfg={"spotify_client_id": "x",
                                                      "spotify_client_secret": "y"})
       == "https://i.scdn.co/640.jpg",
       "with a hit it takes the WIDEST image the artist object states")
    got = discovery.artist_image("Nirvana", cfg={"artist_image_sources": ["spotify"],
                                                 "spotify_client_id": "x",
                                                 "spotify_client_secret": "y"})
    ok(got and got.get("source") == "spotify" and got.get("kind") == "photo",
       f"…and the walk returns it as a photo ({got})")
finally:
    discovery._spotify_artist_hit = _saved_hit

print(f"alias_images: {PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)