#!/usr/bin/env python3
"""Verification for `server.sources_health` — the setup wizard's one answer.

Three things are easy to get wrong here and all three cost the user a wizard
step: a keyed source that claims to be ready, a broken source that takes the
endpoint down with it, and a "cheap" status call that turns out to hit six
APIs every time Settings opens. This pins:

  * a keyed source without its key is `skipped` + `configured: false`, and the
    row names the key to ask for,
  * an unconfigured source is never probed (no request, even with probe=1),
  * a probe that raises is a `fail` row with a detail, never an exception,
  * `probe=False` calls nothing at all,
  * every registered source appears exactly once, with the documented keys.

Offline by construction: every network seam is stubbed, and the real registry
is only ever asked for its CONFIGURATION (probe=False) or with stubbed probes.

Run:  python tools/test_sources_health.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import mlo.lyrics_providers as lyrics  # noqa: E402
from mlo.lyrics_providers import SOURCES as LYRICS_SOURCES  # noqa: E402
from server import credential_checks as cc  # noqa: E402
from server import discover  # noqa: E402
from server import discovery  # noqa: E402
from server import integrations as intg  # noqa: E402
from server import sources_health as sh  # noqa: E402

passed = 0


def ok(cond, label):
    global passed
    assert cond, f"FAILED: {label}"
    passed += 1
    print(f"  ok: {label}")


ROW_KEYS = {"id", "kind", "label", "free", "needs", "configured", "status",
            "detail", "ms"}

CALLS = []


def stub(name, ret):
    """A network seam replacement that records every call."""
    def fn(*args, **kwargs):
        CALLS.append(name)
        return ret
    return fn


def calls(name):
    return CALLS.count(name)


def row(payload, source_id):
    return next(r for r in payload["sources"] if r["id"] == source_id)


# --------------------------------------------------------------------------- #
# the whole registry, configuration only
# --------------------------------------------------------------------------- #
print("== registry ==")
health = sh.health_payload(cfg={}, probe=False)
rows = health["sources"]
ids = [r["id"] for r in rows]

# The RYM link row shares its id with the genre source that reads the same
# cookie — the id may appear twice, the (kind, id) pair may not.
expected = (list(LYRICS_SOURCES) + sorted(intg.ADVISORY_SOURCES)
            + list(intg.GENRE_SOURCES) + list(discovery.IMAGE_SOURCES)
            + ["rateyourmusic"] + [spec["id"] for spec in discover.SOURCES]
            + list(cc.credential_ids()))
ok(sorted(ids) == sorted(expected),
   f"every registered source is reported once ({len(ids)} rows, "
   f"expected {len(expected)})")
ok(len(ids) == len(set((r["kind"], r["id"]) for r in rows)),
   "no (kind, id) pair is repeated — a shared id carries its own kind")

ok(all(set(r) - {"synced", "rank", "notes", "provides"} == ROW_KEYS
       for r in rows),
   "every row carries exactly the documented keys")
ok(all(r["kind"] in sh.KINDS for r in rows),
   f"every row declares a known kind (got {sorted({r['kind'] for r in rows})})")
ok(all(isinstance(r["needs"], list)
       and all(isinstance(n, str) for n in r["needs"]) for r in rows),
   "`needs` is a list of requirement names")
# Every SOURCE is free to use; the credential rows are the exception the flag
# exists for (an AI key can be a paid one), and only those.
ok(all(r["free"] is True for r in rows if r["kind"] != "credentials")
   and [r["id"] for r in rows if r["kind"] == "credentials" and not r["free"]]
   == ["ai"],
   "every source is free, and the one credential that can cost money says so")
ok(all(r["status"] in ("ok", "skipped", "fail") for r in rows),
   "`status` is one of ok|skipped|fail")
ok(all(isinstance(r["ms"], int) and r["ms"] >= 0 for r in rows),
   "`ms` is a non-negative int")
ok(all(isinstance(r["detail"], str) and r["detail"] for r in rows),
   "every row explains itself")
ok(all(("synced" in r) == (r["kind"] == "lyrics") for r in rows),
   "`synced` is on the lyrics rows only")
link_rows = [r for r in rows if r["kind"] == "links"]
ok(len(link_rows) == 1 and link_rows[0]["id"] == "rateyourmusic"
   and link_rows[0]["needs"] == ["rym_cookie"],
   f"the one link row is RateYourMusic and asks for the cookie "
   f"({[r['label'] for r in link_rows]})")
lyrics_rows = [r for r in rows if r["kind"] == "lyrics"]
ok([r.get("rank") for r in lyrics_rows] == [1, 2, 3, 4, 5, 6],
   f"the lyrics rows carry the registry's 1-based rank in order "
   f"({[r.get('rank') for r in lyrics_rows]})")
ok(all(isinstance(r.get("notes"), str) and r["notes"] for r in lyrics_rows),
   "…and each one's own notes")
ok(all("rank" not in r for r in rows if r["kind"] not in ("lyrics", "genre"))
   and all("notes" not in r for r in rows if r["kind"] not in ("lyrics", "discover")),
   "no other kind borrows the lyrics-only or discover-only keys")

# `provides` is on EVERY row of EVERY kind — the wizard's Keys step renders
# one line per provider saying what it gives the app, so a kind that quietly
# skipped it would leave an unexplained row there.
ok(all(isinstance(r.get("provides"), str) and r["provides"].strip()
       for r in rows),
   f"every row of every kind says what it provides "
   f"({sorted({r['kind'] for r in rows if not r.get('provides')})} silent)")
for kind in sh.KINDS:
    got = sh.health_payload(cfg={}, kind=kind)["sources"]
    ok(all(r.get("provides", "").strip() for r in got),
       f"kind={kind} rows all carry their own `provides` ({len(got)})")

# The genre rows ARE the chain the wizard asks, and the tray saves: the ids
# follow `server.integrations.GENRE_SOURCES` in its own order, `rank` says the
# 1-based position (so the list the user sees top-to-bottom IS the priority
# order that gets saved back) and `provides` is the one line under each name.
genre_rows = [r for r in rows if r["kind"] == "genre"]
ok([r["id"] for r in genre_rows] == list(intg.GENRE_SOURCES),
   f"the genre rows ARE GENRE_SOURCES, in registry order "
   f"({[r['id'] for r in genre_rows]})")
ok([r.get("rank") for r in genre_rows] == list(range(1, len(genre_rows) + 1)),
   f"…ranked 1..N in that same order "
   f"({[r.get('rank') for r in genre_rows]})")
ok(all(isinstance(r.get("provides"), str) and r["provides"].strip()
       for r in genre_rows),
   "…each with a non-empty line saying what it contributes")
ok([r["provides"] for r in genre_rows]
   == [sh._GENRE_PROVIDES[pid] for pid in intg.GENRE_SOURCES],
   "…and each line is that source's own entry, not a neighbour's")
ok(all(r["label"] != r["id"] for r in rows),
   "every row has a real label, never the raw source id (the picker reads it)")
ok(all(r["label"] for r in rows), "no row has an empty label")
ok(health["checked_at"].endswith("+00:00"),
   f"`checked_at` is an ISO UTC stamp ({health['checked_at']})")

disc_rows = [r for r in rows if r["kind"] == "discover"]
ok([r["id"] for r in disc_rows] == [spec["id"] for spec in discover.SOURCES],
   f"the Discover rows ARE the /api/discover registry, in its own order "
   f"({[r['id'] for r in disc_rows]})")
ok(all(r.get("notes") == spec["note"] for r, spec in zip(disc_rows, discover.SOURCES)),
   "…each carrying what that source can answer (the panel shows it)")
ok(row({"sources": disc_rows}, "lastfm")["needs"] == ["lastfm_api_key"]
   and row({"sources": disc_rows}, "spotify")["needs"]
   == ["spotify_client_id", "spotify_client_secret"]
   and row({"sources": disc_rows}, "musicbrainz")["configured"] is True,
   "…with the credential the DISCOVER source itself needs")

# The credentials kind is the one that answers a DIFFERENT question from the
# source rows: is the saved login accepted. `discogs`, `lastfm` and `spotify`
# deliberately appear here AND as a source — Discogs browses anonymously, so
# its source row says nothing about the token, and this one is the answer.
cred_rows = [r for r in rows if r["kind"] == "credentials"]
ok([r["id"] for r in cred_rows] == list(cc.credential_ids()),
   f"the credential rows ARE server.credential_checks' registry, in its own "
   f"order ({[r['id'] for r in cred_rows]})")
for cid, needs in (("discogs", ["discogs_token"]),
                   ("lastfm", ["lastfm_api_key"]),
                   ("spotify", ["spotify_client_id", "spotify_client_secret"]),
                   ("acoustid", ["acoustid_api_key"]),
                   ("soulseek", ["soulseek_username", "soulseek_password"]),
                   # The AI key is NOT a need: a local LM Studio answers with
                   # no key at all, so requiring one would call a working
                   # setup broken. Its absence is reported by the check.
                   ("ai", ["ai_base_url", "ai_model"])):
    got = next(r for r in cred_rows if r["id"] == cid)
    ok(got["needs"] == needs, f"credential {cid} names what to paste ({got['needs']})")
    ok(got["configured"] is False and got["status"] == "skipped"
       and all(k in got["detail"] for k in needs),
       f"credential {cid} is skipped, naming what is missing ({got['detail']})")
saved = next(r for r in cred_rows if r["id"] == "login")
ok(saved["needs"] == [] and saved["configured"] is True,
   "the login row has nothing to paste, so it is never 'missing' a key")

print("== kind filter ==")
for kind in sh.KINDS:
    got = sh.health_payload(cfg={}, kind=kind)["sources"]
    ok(got and all(r["kind"] == kind for r in got),
       f"kind={kind} returns only {kind} rows ({len(got)})")
ok(len(sh.health_payload(cfg={})["sources"]) == len(rows),
   "no kind returns everything")

# --------------------------------------------------------------------------- #
# configuration state (no request may leave the machine for this)
# --------------------------------------------------------------------------- #
print("== configuration state ==")
for sid, needs in (("spotify-isrc", ["spotify_client_id", "spotify_client_secret"]),
                   ("discogs-parental", ["discogs_token"]),
                   ("lastfm", ["lastfm_api_key"]),
                   ("discogs", ["discogs_token"]),
                   ("rateyourmusic", ["rym_cookie"])):
    r = row(health, sid)
    ok(r["needs"] == needs, f"{sid} names the config keys it needs ({r['needs']})")
    ok(r["configured"] is False and r["status"] == "skipped",
       f"{sid} is skipped while unconfigured ({r['status']})")
    ok(all(k in r["detail"] for k in needs),
       f"{sid}'s detail names what to configure ({r['detail']})")

keyless = ["lrclib", "deezer-isrc", "apple-album", "musicbrainz", "deezer"]
ok(all(row(health, sid)["configured"] is True
       and row(health, sid)["status"] == "ok" for sid in keyless),
   f"a keyless source is configured by default ({keyless})")
ok(all(row(health, sid)["detail"] == "configured" for sid in keyless),
   "and says exactly that")

configured_cfg = {"spotify_client_id": "id", "spotify_client_secret": "secret",
                  "discogs_token": "tok", "lastfm_api_key": "key",
                  "rym_cookie": "cf=1"}
keyed = sh.health_payload(cfg=configured_cfg)["sources"]
ok(all(row({"sources": keyed}, sid)["configured"] is True
       for sid in ("spotify-isrc", "discogs-parental", "lastfm", "discogs",
                   "rateyourmusic")),
   "setting the keys flips every keyed source to configured")

print("== probe=False is free ==")
intg._advisory_json = stub("advisory_json", None)
intg.mb_get = stub("mb_get", {})
intg._deezer_advisory = stub("deezer_advisory", None)
intg._spotify_advisory = stub("spotify_advisory", None)
intg._apple_album_advisory = stub("apple_advisory", None)
intg._itunes_song_advisory = stub("itunes_advisory", None)
intg.rym_genres = stub("rym_genres", None)
intg._audiodb_genre_names = stub("audiodb_genres", [])
discovery._json = stub("discovery_json", None)
discovery.deezer_artist = stub("deezer_artist", None)
discovery.wikipedia_summary = stub("wikipedia_summary", None)
discovery.listenbrainz_genre_tags = stub("listenbrainz_genres", None)
discovery.album_genres = stub("discovery_album_genres", [])
discovery.itunes_search_album = stub("itunes_album", [])
discovery.wikidata_genres = stub("wikidata_genres", None)
discovery.lastfm_artist_genres = stub("lastfm_genres", [])
discovery.discogs_album_genres = stub("discogs_genres", [])
discovery.discogs_parental_advisory = stub("discogs_parental", None)
discovery._discogs_release = stub("discogs_release", None)
discovery.itunes_artist_artwork = stub("itunes_artwork", None)
discovery.audiodb_artist = stub("audiodb_artist", None)
discovery.resolve_artist_mbid = stub("resolve_artist_mbid", "")
discovery.musicbrainz_genre_list = stub("mb_genre_list", {"genres": [], "count": 0,
                                                          "loaded": 0, "done": True})
discovery.deezer_genre_list = stub("deezer_genre_list", [])
discovery.itunes_genre_albums = stub("itunes_genre_albums", {"rows": [], "total": None})
discovery.discogs_style_search = stub("discogs_style", {"rows": [], "total": None})
discovery.lastfm_tag_top = stub("lastfm_tag_top", {"rows": [], "total": None})
discovery.listenbrainz_similar_artists = stub("lb_similar", [])
discovery.spotify_genre_albums = stub("spotify_genre", {"rows": [], "total": None})
# The Discover registry's RateYourMusic row is a CHARTS source, probed through
# the scrape in `server.integrations` — the seam below keeps this file's
# "probe=False is free" and "no call leaves the machine" guarantees.
intg.rym_charts = stub("rym_charts", {"rows": [], "total": None, "chart": ""})
# The credentials rows have their own probe seam; the login row has nothing to
# paste, so it is always asked and would otherwise read this machine's own
# auth database on every probe=True call in this file.
cc.check = stub("credential_check", ("ok", "stubbed credential"))

CALLS.clear()
sh.health_payload(cfg={}, probe=False)
sh.health_payload(cfg=configured_cfg, probe=False)
ok(not CALLS,
   f"probe=False performs no source call at all (got {sorted(set(CALLS))})")

print("== an unconfigured source is not probed ==")
lyrics.probe_source = stub("lyrics_probe", {"id": "x", "status": "ok",
                                           "detail": "synced lyrics, 3 lines"})
CALLS.clear()
sh.health_payload(cfg={}, probe=True)
for sid, seam in (("spotify-isrc", "spotify_advisory"),
                  ("discogs-parental", "discogs_release"),
                  ("lastfm", "lastfm_genres"),
                  ("discogs", "discogs_genres"),
                  ("rateyourmusic", "rym_genres")):
    ok(calls(seam) == 0,
       f"{sid} was never asked without its key (probe=1, {seam} calls: "
       f"{calls(seam)})")
ok(calls("lyrics_probe") >= 5,
   f"the configured lyrics providers WERE probed ({calls('lyrics_probe')} calls)")
ok(calls("deezer_artist") >= 1,
   f"the configured metadata source WAS probed ({calls('deezer_artist')} calls)")
# Only the login row has nothing to paste, so it is the ONE credential asked
# with an empty config; the six keyed rows never reach the provider.
ok(calls("credential_check") == 1,
   f"only the login credential was probed with an empty config "
   f"({calls('credential_check')} credential probes)")

print("== the RYM link probe ==")
saved_rym_links, saved_failures = intg.rym_links, intg._rym_failures
try:
    def _links(album=None, artist=None, note=""):
        # *a/**k on purpose: the probe calls this positionally, and named
        # parameters would shadow the captured values.
        return lambda *a, **k: {"album": album, "artist": artist, "note": note}

    intg.rym_links = _links("https://rateyourmusic.com/release/album/radiohead/pablo-honey/",
                            "https://rateyourmusic.com/artist/radiohead")
    st, detail = sh._probe_links("rateyourmusic", {"rym_cookie": "cf=1"})
    ok(st == "ok" and "album" in detail and "artist" in detail,
       f"both links found is an ok row ({detail})")

    intg.rym_links = _links(note="could not resolve RateYourMusic links")
    st, detail = sh._probe_links("rateyourmusic", {"rym_cookie": "cf=1"})
    ok(st == "fail" and "could not resolve" in detail,
       f"a verified miss is a fail carrying RYM's own note ({detail})")

    def _refused(artist="", album="", cfg=None, mbid=None):
        intg._rym_failures += 1
        return {"album": None, "artist": None, "note": ""}

    intg.rym_links = _refused
    st, detail = sh._probe_links("rateyourmusic", {"rym_cookie": "cf=1"})
    ok(st == "fail" and "Cookie" in detail,
       f"RYM refusing the client says how to get past it ({detail})")

    intg.rym_links = _links(artist="https://rateyourmusic.com/artist/radiohead")
    st, detail = sh._probe_links("rateyourmusic", {"rym_cookie": "cf=1"})
    ok(st == "ok" and "artist" in detail and "album" not in detail,
       f"one half of the pair is still an ok row ({detail})")

    ok(sh._probe_links("deezer", {})[1] == "unknown source",
       "an id this probe does not own is skipped, never guessed at")
finally:
    intg.rym_links, intg._rym_failures = saved_rym_links, saved_failures

print("== a rate-limited source says so ==")


class _HTTP503(Exception):
    class response:                     # noqa: N801 - what httpx exposes
        status_code = 503


sh._ARTIST_MBID.clear()
discovery.resolve_artist_mbid = stub("resolve_artist_mbid",
                                    "a74b1b7f-71a5-4011-9441-d0b5e4122711")


def _mb_503(*args, **kwargs):
    CALLS.append("mb_get_503")
    raise _HTTP503("503")


intg.mb_get = _mb_503
mb = row(sh.health_payload(cfg={}, probe=True), "musicbrainz")
ok(mb["status"] == "fail" and "503" in mb["detail"],
   f"a 503 from MusicBrainz is a fail naming the status ({mb['detail']})")
intg.mb_get = stub("mb_get", {"genres": [{"name": "rock"}]})
mb = row(sh.health_payload(cfg={}, probe=True), "musicbrainz")
ok(mb["status"] == "ok" and mb["detail"] == "1 genre",
   f"a real answer is ok with its genre count ({mb['detail']})")
sh._ARTIST_MBID.clear()

# --------------------------------------------------------------------------- #
# probe outcomes, with the probe seam replaced by stubs
# --------------------------------------------------------------------------- #
print("== probe outcomes ==")
REAL_SPECS = sh._specs


def fake_specs(kind=None):
    def boom(cfg):
        raise RuntimeError("boom")
    specs = [
        {"id": "ok-source", "kind": "lyrics", "label": "OK", "needs": [],
         "synced": True, "probe": lambda cfg: ("ok", "synced lyrics, 12 lines")},
        {"id": "bad-source", "kind": "genre", "label": "Bad", "needs": [],
         "probe": boom},
        {"id": "keyed-source", "kind": "advisory", "label": "Keyed",
         "needs": ["discogs_token"], "probe": boom},
        {"id": "ytdlp-source", "kind": "metadata", "label": "Tool",
         "needs": ["yt-dlp"],
         "probe": lambda cfg: ("skipped", "needs a track's YouTube id")},
    ]
    return [s for s in specs if kind is None or s["kind"] == kind]


sh._specs = fake_specs
try:
    out = sh.health_payload(cfg={}, probe=True)["sources"]
    by_id = {r["id"]: r for r in out}
    ok(by_id["ok-source"]["status"] == "ok"
       and by_id["ok-source"]["detail"] == "synced lyrics, 12 lines",
       f"a working probe is ok with its own detail ({by_id['ok-source']['detail']})")
    ok(by_id["ok-source"]["ms"] >= 0 and by_id["ok-source"]["configured"] is True,
       "…and it carries a measured ms")
    bad = by_id["bad-source"]
    ok(bad["status"] == "fail" and "boom" in bad["detail"],
       f"a raising probe is a fail row naming the error ({bad['detail']})")
    keyed = by_id["keyed-source"]
    ok(keyed["status"] == "skipped" and keyed["configured"] is False
       and "discogs_token" in keyed["detail"],
       f"an unconfigured source is skipped, never probed ({keyed['detail']})")
    tool = by_id["ytdlp-source"]
    ok(tool["status"] in ("ok", "skipped") and "yt-dlp" in tool["needs"],
       f"a tool requirement is named in needs ({tool['needs']})")
    # the whole payload survives a probe that raises, per kind
    for kind in ("lyrics", "genre"):
        one = sh.health_payload(cfg={}, kind=kind, probe=True)["sources"]
        ok(len(one) == 1, f"kind={kind} still returns its one row")
finally:
    sh._specs = REAL_SPECS

print("== the discover probes ==")
discovery.deezer_genre_list = lambda timeout=None: [{"id": 152, "name": "Rock"},
                                                   {"id": 132, "name": "Pop"}]
st, detail = sh._probe_discover("deezer", {})
ok(st == "ok" and detail == "2 genres",
   f"the Discover probe asks what that source is FOR ({detail})")
discovery.musicbrainz_genre_list = lambda pages=2: {
    "genres": [{"name": "rock", "id": "x"}], "count": 1, "loaded": 1, "done": True}
st, detail = sh._probe_discover("musicbrainz", {})
ok(st == "ok" and detail == "1 genre",
   f"…and reports MusicBrainz's own taxonomy ({detail})")
discovery.audiodb_artist = lambda name, timeout=None: {"genre": "Rock",
                                                       "mood": "Melancholic"}
st, detail = sh._probe_discover("audiodb", {})
ok(st == "ok" and "no list endpoint" in detail,
   f"a verification source says what it can do instead ({detail})")
ok(sh._probe_discover("nope", {}) == ("skipped", "unknown source"),
   "an id this probe does not own is skipped, never guessed at")

print("== registry order ==")
ok(sh.source_ids() == [r["id"] for r in rows],
   "source_ids() mirrors the payload's own order (routes use it to validate)")

print(f"\nAll {passed} checks passed.")
