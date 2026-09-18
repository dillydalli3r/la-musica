"""Which external sources work right now — one payload for the setup wizard.

Five kinds of source, one row shape: the lyrics providers, the advisory
(ITUNESADVISORY) routes, the genre sources, the metadata (artist image)
providers and the LINK sources (the RateYourMusic album/artist links an import
stamps). Every row says what it needs, whether that is configured here, and —
when asked to probe — what actually answered.

`probe=False` (the default) answers from the config alone: NO request leaves
this machine, unconfigured rows are `skipped` and the rest are `ok`
("configured"). That is what a settings page can afford on every open.

`probe=True` runs ONE cheap lookup per configured source against the same fixed
sample the lyrics providers already probe with (Radiohead / Creep / Pablo
Honey), in parallel, and reports what came back:

  * `ok`      — it answered ("synced lyrics, 38 lines", "6 genres",
                "artist photo 1000x1000"),
  * `skipped` — it cannot run here at all (no key, no yt-dlp, RYM refusing us),
  * `fail`    — it ran and had nothing, or raised.

Nothing here ever raises or writes anything: a broken source is a row, not a
500. `mlo.lyrics_providers.probe_source` is reused for the lyrics providers so
there is exactly one sample and one set of probe rules.
"""
import concurrent.futures
import re
import time
from datetime import datetime, timezone

# The one fixed sample every probe uses, so two runs are comparable. Artist,
# album, track and duration are `mlo.lyrics_providers.PROBE_SAMPLE`'s; the ISRC
# is what Deezer/Apple/Spotify file that track under (verified live), and is
# what the two ISRC advisory routes are asked about.
SAMPLE_ARTIST = "Radiohead"
SAMPLE_TRACK = "Creep"
SAMPLE_ALBUM = "Pablo Honey"
SAMPLE_ISRC = "GBAYE9200070"

# RYM links are their own row rather than a second reading of the genre
# source that shares the cookie: the genre probe asks for genres, this one
# resolves the sample album's links, which is the thing the import writes.
KINDS = ("lyrics", "advisory", "genre", "metadata", "links")

# The ask order of the advisory routes — `resolve_advisory_route`'s own order.
_ADVISORY_ORDER = ["deezer-isrc", "spotify-isrc", "apple-album", "itunes-song",
                   "discogs-parental", "youtube-age"]
_ADVISORY_LABELS = {
    "deezer-isrc": "Deezer (ISRC)",
    "spotify-isrc": "Spotify (ISRC)",
    "apple-album": "Apple (album editions)",
    "itunes-song": "iTunes (song search)",
    "discogs-parental": "Discogs (parental advisory)",
    "youtube-age": "YouTube (age gate)",
}

# Genre source labels — the spellings the settings UI already shows.
_GENRE_LABELS = {
    "rateyourmusic": "RateYourMusic",
    "listenbrainz": "ListenBrainz",
    "musicbrainz": "MusicBrainz",
    "itunes": "iTunes",
    "wikidata": "Wikidata",
    "lastfm": "Last.fm",
    "discogs": "Discogs",
    "theaudiodb": "TheAudioDB",
    "bandcamp": "Bandcamp",
    "deezer": "Deezer",
    "spotify": "Spotify",
}

# A probe returns (status, detail) and is wrapped by `_timed`, which is the
# only place that turns a raised exception into a `fail` row.
def _ms(seconds):
    return int(seconds * 1000)


def _count_detail(names, label="genres"):
    """("6 genres" | "") for a list of genre names."""
    names = [str(n).strip() for n in (names or []) if str(n or "").strip()]
    if not names:
        return ""
    return "%d %s" % (len(names), label[:-1] if len(names) == 1 else label)


def _image_detail(label, url):
    """*label* plus the pixel size when the provider bakes it into the URL."""
    match = re.search(r"(\d{2,5})x(\d{2,5})", str(url or ""))
    return "%s %sx%s" % (label, match.group(1), match.group(2)) if match \
        else label


# --------------------------------------------------------------------------- #
# Lyrics — the providers' own probe, unchanged
# --------------------------------------------------------------------------- #
def _probe_lyrics(pid, cfg):
    from mlo.lyrics_providers import probe_source

    got = probe_source(pid, cfg) or {}
    return got.get("status") or "fail", str(got.get("detail") or "")


# --------------------------------------------------------------------------- #
# Advisory (ITUNESADVISORY) routes
# --------------------------------------------------------------------------- #
def _probe_advisory(pid, cfg):
    from server import discovery
    from server import integrations as intg

    if pid == "deezer-isrc":
        hit = intg._deezer_advisory(SAMPLE_ISRC)
        if hit is None:
            return "fail", "no advisory data for the sample ISRC"
        return "ok", "explicit" if hit[0] else "clean (not explicit)"

    if pid == "spotify-isrc":
        hit = intg._spotify_advisory(SAMPLE_ISRC, cfg)
        if hit is None:
            return "fail", ("no Spotify track for the sample ISRC — check the "
                            "credentials")
        return "ok", "explicit" if hit[0] else "clean (not explicit)"

    if pid == "apple-album":
        hit = intg._apple_album_advisory(SAMPLE_ARTIST, SAMPLE_ALBUM,
                                         title=SAMPLE_TRACK, cfg=cfg)
        if hit is None:
            return "fail", "no Apple edition rated the sample track"
        return "ok", "explicit" if hit[0] else "clean (not explicit)"

    if pid == "itunes-song":
        hit = intg._itunes_song_advisory(SAMPLE_TRACK, SAMPLE_ARTIST)
        if hit is not None:
            return "ok", "explicit" if hit[0] else "clean (not explicit)"
        # Apple ranking a cover/remix above the studio track is a fact about
        # the sample, not a broken source: only an EMPTY search means the
        # source itself is not answering. (The same request is already in the
        # advisory cache, so this costs nothing.)
        raw = intg._advisory_json(
            intg._ITUNES_LOOKUP + "/search",
            {"term": " ".join((SAMPLE_ARTIST, SAMPLE_TRACK)),
             "entity": "song", "limit": 10}) or {}
        if raw.get("resultCount"):
            return "ok", "search answered, no rated match for the sample"
        return "fail", "Apple's song search answered nothing"

    if pid == "discogs-parental":
        # The release lookup is the token's real test: a bad or missing token
        # answers nothing here, while a hit with no flag is a working source.
        row = discovery._discogs_release(SAMPLE_ARTIST, SAMPLE_ALBUM, cfg=cfg)
        if not row:
            return "fail", "no Discogs release matched — check discogs_token"
        flagged = discovery.discogs_parental_advisory(SAMPLE_ARTIST,
                                                      SAMPLE_ALBUM, cfg)
        if flagged:
            return "ok", "parental advisory flagged on the sample's edition"
        return "ok", "release found, no advisory flag"

    if pid == "youtube-age":
        # Like the captions provider: there is nothing to probe without a
        # track's own video id, and nothing here ever searches YouTube.
        return "skipped", "needs a track's YouTube id"

    return "skipped", "unknown source"


# --------------------------------------------------------------------------- #
# Genre sources
# --------------------------------------------------------------------------- #
_ARTIST_MBID = {}


def _artist_mbid(cfg):
    """The sample artist's MusicBrainz MBID, resolved once per process."""
    if "mbid" not in _ARTIST_MBID:
        from server import discovery

        try:
            _ARTIST_MBID["mbid"] = discovery.resolve_artist_mbid(SAMPLE_ARTIST,
                                                                 cfg) or ""
        except Exception:
            _ARTIST_MBID["mbid"] = ""
    return _ARTIST_MBID["mbid"]


def _probe_genre(pid, cfg):
    from server import discovery
    from server import integrations as intg

    if pid == "rateyourmusic":
        before = intg._rym_failures
        data = intg.rym_genres(SAMPLE_ARTIST, SAMPLE_ALBUM)
        if intg._rym_failures != before:
            return "skipped", ("RYM refused the request (403/challenge) — "
                               "refresh rym_cookie")
        detail = _count_detail((data or {}).get("genres"))
        return ("ok", detail) if detail else ("fail", "no RYM genres for the sample")

    if pid == "listenbrainz":
        mbid = _artist_mbid(cfg)
        if not mbid:
            return "fail", "could not resolve the sample artist on MusicBrainz"
        got = discovery.listenbrainz_genre_tags(mbid, "artist")
        if not got:
            return "fail", "no ListenBrainz tags for the sample artist"
        names = (got.get("genres") or []) + (got.get("tags") or [])
        detail = _count_detail(names)
        return ("ok", detail) if detail else ("fail", "no ListenBrainz genres")

    if pid == "musicbrainz":
        mbid = _artist_mbid(cfg)
        if not mbid:
            return "fail", "could not resolve the sample artist on MusicBrainz"
        # `artist_genres` swallows every failure into [], which would report a
        # rate-limited MusicBrainz as a source with no genres. Ask the
        # transport directly, so a 429/503 is a fail that says so.
        try:
            data = intg.mb_get("artist/%s" % mbid, {"inc": "genres", "fmt": "json"})
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status:
                return "fail", "MusicBrainz answered HTTP %s" % status
            return "fail", "MusicBrainz did not answer (%s)" % type(e).__name__
        detail = _count_detail(intg._genres(data or {}))
        return ("ok", detail) if detail \
            else ("fail", "no MusicBrainz genres for the sample artist")

    if pid == "itunes":
        rows = discovery.itunes_search_album(SAMPLE_ARTIST, SAMPLE_ALBUM, limit=1)
        detail = _count_detail([r.get("genre") for r in (rows or [])])
        return ("ok", detail) if detail else ("fail", "no iTunes album genre")

    if pid == "wikidata":
        # Wikidata is reached by a search TERM, and "artist album" is only one
        # phrasing of it (the genre chain's). The album's own title is the
        # second, cheapest try before calling the source dead.
        detail = ""
        for term in (" ".join((SAMPLE_ARTIST, SAMPLE_ALBUM)), SAMPLE_ALBUM):
            got = discovery.wikidata_genres(term=term)
            detail = _count_detail((got or {}).get("genres"))
            if detail:
                break
        return ("ok", detail) if detail else ("fail", "no Wikidata genres")

    if pid == "lastfm":
        detail = _count_detail(discovery.lastfm_artist_genres(SAMPLE_ARTIST, cfg))
        return ("ok", detail) if detail else ("fail", "no Last.fm tags")

    if pid == "discogs":
        detail = _count_detail(discovery.discogs_album_genres(SAMPLE_ARTIST,
                                                             SAMPLE_ALBUM, cfg))
        return ("ok", detail) if detail else ("fail", "no Discogs genres")

    if pid == "theaudiodb":
        detail = _count_detail(intg._audiodb_genre_names(SAMPLE_ARTIST,
                                                         SAMPLE_ALBUM))
        return ("ok", detail) if detail else ("fail", "no TheAudioDB genres")

    if pid == "bandcamp":
        detail = _count_detail((intg.bandcamp_album(SAMPLE_ARTIST,
                                                    SAMPLE_ALBUM) or {}).get("genres"))
        return ("ok", detail) if detail else ("fail", "no Bandcamp tags")

    if pid == "spotify":
        detail = _count_detail(discovery.spotify_artist_genres(SAMPLE_ARTIST, cfg))
        return ("ok", detail) if detail else ("fail", "no Spotify genres")

    if pid == "deezer":
        detail = _count_detail(discovery.album_genres(SAMPLE_ARTIST, SAMPLE_ALBUM,
                                                      cfg=cfg))
        return ("ok", detail) if detail else ("fail", "no Deezer genres")

    return "skipped", "unknown source"


# --------------------------------------------------------------------------- #
# Metadata (artist image) providers
# --------------------------------------------------------------------------- #
def _probe_metadata(pid, cfg):
    from server import discovery

    if pid == "deezer":
        row = discovery.deezer_artist(SAMPLE_ARTIST) or {}
        url = row.get("image")
        return ("ok", _image_detail("artist photo", url)) if url \
            else ("fail", "no Deezer artist photo")

    if pid == "audiodb":
        row = discovery.audiodb_artist(SAMPLE_ARTIST) or {}
        for key, label in (("thumb", "artist thumb"), ("banner", "artist banner"),
                           ("fanart", "artist fanart")):
            if row.get(key):
                return "ok", _image_detail(label, row[key])
        return "fail", "no TheAudioDB artist image"

    if pid == "itunes":
        url = discovery.itunes_artist_artwork(SAMPLE_ARTIST)
        return ("ok", _image_detail("album artwork (no artist photo API)", url)) \
            if url else ("fail", "no Apple artwork for the sample artist")

    if pid == "wikipedia":
        summary = discovery.wikipedia_summary(SAMPLE_ARTIST) or {}
        url = summary.get("image")
        return ("ok", _image_detail("lead image", url)) if url \
            else ("fail", "no Wikipedia lead image")

    return "skipped", "unknown source"


# --------------------------------------------------------------------------- #
# Link sources — the RYM album/artist links an import stamps
# --------------------------------------------------------------------------- #
_RYM_LABELS = {"rateyourmusic": "RateYourMusic links (album + artist)"}


def _probe_links(pid, cfg):
    from server import integrations as intg

    if pid != "rateyourmusic":
        return "skipped", "unknown source"
    # MusicBrainz states the RYM page for well-known releases, so a missing
    # cookie is not automatically a failure — the note says which half of the
    # ladder answered (or that RYM refused the client).
    fails_before = intg._rym_failures
    got = intg.rym_links(SAMPLE_ARTIST, SAMPLE_ALBUM, cfg) or {}
    album, artist = got.get("album"), got.get("artist")
    note = str(got.get("note") or "").strip()
    if album or artist:
        parts = [x for x in (("album" if album else ""),
                             ("artist" if artist else "")) if x]
        return "ok", " · ".join(parts) + " link resolved" + (f" · {note}" if note else "")
    if intg._rym_failures > fails_before:
        return "fail", note or ("RYM did not answer — paste a logged-in "
                                "Cookie header to get past Cloudflare")
    return "fail", note or "no link could be verified"


# --------------------------------------------------------------------------- #
# The registry: one spec per source, in a stable order
# --------------------------------------------------------------------------- #
def _specs(kind=None):
    """Every source as a spec, in registry order, filtered to *kind*."""
    from mlo.lyrics_providers import available_sources
    from server import discovery
    from server import integrations as intg

    specs = []
    for src in available_sources():
        pid = src["id"]
        # All six providers are keyless: the captions one needs yt-dlp
        # *installed*, which is what the wizard has to check for it. `rank`
        # (1-based, the registry's own order) and `notes` come straight from
        # the registry so the wizard reads them from ONE payload.
        specs.append({"id": pid, "kind": "lyrics", "label": src["label"],
                      "synced": True, "rank": src.get("rank"),
                      "notes": src.get("notes") or "",
                      "needs": ["yt-dlp"] if pid == "youtube" else [],
                      "probe": lambda cfg, p=pid: _probe_lyrics(p, cfg)})

    for pid in _ADVISORY_ORDER:
        needs = {"spotify-isrc": ["spotify_client_id", "spotify_client_secret"],
                 "discogs-parental": ["discogs_token"],
                 "youtube-age": ["yt-dlp"]}.get(pid, [])
        specs.append({"id": pid, "kind": "advisory",
                      "label": _ADVISORY_LABELS[pid], "needs": list(needs),
                      "probe": lambda cfg, p=pid: _probe_advisory(p, cfg)})

    for pid in intg.GENRE_SOURCES:
        needs = {"lastfm": ["lastfm_api_key"], "discogs": ["discogs_token"],
                 "rateyourmusic": ["rym_cookie"],
                 "spotify": ["spotify_client_id", "spotify_client_secret"],
                 }.get(pid, [])
        specs.append({"id": pid, "kind": "genre",
                      "label": _GENRE_LABELS.get(pid, pid), "needs": list(needs),
                      "probe": lambda cfg, p=pid: _probe_genre(p, cfg)})

    for pid in discovery.IMAGE_SOURCES:
        specs.append({"id": pid, "kind": "metadata",
                      "label": discovery.SOURCE_LABELS.get(pid, pid),
                      "needs": [],
                      "probe": lambda cfg, p=pid: _probe_metadata(p, cfg)})

    # Links: the RYM pair an import writes onto the album's tracks. It needs
    # the same cookie the genre row prompts for — that is the point: the
    # wizard shows one place to paste it and one button that proves it works.
    for pid in ("rateyourmusic",):
        specs.append({"id": pid, "kind": "links",
                      "label": _RYM_LABELS[pid], "needs": ["rym_cookie"],
                      "probe": lambda cfg, p=pid: _probe_links(p, cfg)})

    return [s for s in specs if kind is None or s["kind"] == kind]


# Requirements that are an INSTALLED tool rather than a config key. They live
# in the same `needs` list (that is what the wizard has to fix), but they are
# checked by looking for the tool.
_TOOLS = ("yt-dlp",)


def _missing(spec, cfg):
    """The config keys / tools this spec still needs — [] when ready."""
    cfg = cfg or {}
    out = [k for k in spec["needs"]
           if k not in _TOOLS and not str(cfg.get(k) or "").strip()]
    return out + [t for t in spec["needs"]
                  if t in _TOOLS and not _tool_available(t)]


def _tool_available(tool):
    if tool != "yt-dlp":
        return False
    from mlo.lyrics_providers import _ytdlp_exe

    return bool(_ytdlp_exe())


def _timed(fn, cfg):
    """(status, detail, ms) for one probe — never raises."""
    started = time.time()
    try:
        status, detail = fn(cfg)
    except Exception as e:
        status, detail = "fail", "raised: %s" % e
    return str(status), str(detail or ""), _ms(time.time() - started)


def health_payload(cfg=None, kind=None, probe=False):
    """Every source with its configuration state, and its live state on request.

    `kind` filters to one of KINDS (None = all). `probe=True` runs the real
    lookups — in parallel, so the slowest source decides the wall time — and
    only for sources that are configured: an unconfigured one cannot answer
    here, so it is `skipped` without a request.
    """
    if cfg is None:
        from mlo.config import load_config

        cfg = load_config() or {}
    specs = _specs(kind)

    rows, pending = [], []
    for spec in specs:
        missing = _missing(spec, cfg)
        row = {"id": spec["id"], "kind": spec["kind"], "label": spec["label"],
               "free": True, "needs": list(spec["needs"]),
               "configured": not missing, "status": "ok" if not missing else "skipped",
               "detail": "configured" if not missing
                         else "needs " + ", ".join(missing),
               "ms": 0}
        if spec.get("synced"):
            row["synced"] = True
        if spec.get("rank") is not None:
            row["rank"] = spec["rank"]
        if spec.get("notes"):
            row["notes"] = spec["notes"]
        rows.append(row)
        if probe and not missing:
            pending.append((row, spec))

    if pending:
        # Two genre sources are reached through the sample artist's MBID, and
        # MusicBrainz rate-limits per host: resolve it ONCE, up here, so the
        # parallel probes below cannot race each other into a 503.
        if any(spec["id"] in ("listenbrainz", "musicbrainz")
               for _row, spec in pending):
            _artist_mbid(cfg)
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            futures = [(row, pool.submit(_timed, spec["probe"], cfg))
                       for row, spec in pending]
            for row, future in futures:
                status, detail, ms = future.result()
                row.update(status=status, detail=detail, ms=ms)

    return {"sources": rows,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def source_ids(kind=None):
    """Every source id this module reports, for validation and tests.

    An id may appear twice with different kinds (`deezer` and `itunes` are
    both a genre source and a metadata provider), so route validation asks
    this and the ROW carries the kind."""
    return [spec["id"] for spec in _specs(kind)]
