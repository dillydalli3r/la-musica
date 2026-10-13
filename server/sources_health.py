"""Which external sources work right now — one payload for the setup wizard.

Six kinds of source, one row shape: the lyrics providers, the advisory
(ITUNESADVISORY) routes, the genre sources, the metadata (artist image)
providers, the /api/discover/* sources and the credentials those sources
need. Every row says what it needs, whether that is configured here, and —
when asked to probe — what actually answered.

`probe=False` (the default) answers from the config alone: NO request leaves
this machine, unconfigured rows are `skipped` and the rest are `ok`
("configured"). That is what a settings page can afford on every open.

`probe=True` runs ONE cheap lookup per configured source against the same fixed
sample the lyrics providers already probe with (Radiohead / Creep / Pablo
Honey), in parallel, and reports what came back:

  * `ok`      — it answered ("synced lyrics, 38 lines", "6 genres",
                "artist photo 1000x1000"),
  * `skipped` — it cannot run here at all (no key, no yt-dlp),
  * `fail`    — it ran and had nothing, or raised.

A source can still run without a `needs` entry when something else stands in
for it. The entry stays in `needs` (the panel prompts for it) and
`optional_needs` says which of `needs` the source can run without.

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

# `discover` is the /api/discover/* surface: its sources are the ones
# `server.discover`'s registry declares, probed with what each can actually
# answer (a genre list, a genre browse, a recommendation feed).
#
# `credentials` is the odd one out and is LAST for that reason: these rows are
# not sources, they are the logins the sources need. A source row answers "can
# this source do its job here", which is NOT the same question — Discogs
# browses anonymously, so its row stays green with a discarded token, and
# Spotify is simply skipped without a client secret, so a REJECTED secret read
# as "not configured". These rows ask the provider's own credential endpoint
# instead; see server/credential_checks.
KINDS = ("lyrics", "advisory", "genre", "metadata", "discover",
         "credentials")

# The ask order of the advisory routes — `resolve_advisory_route`'s own order.
_ADVISORY_ORDER = ["deezer-isrc", "spotify-isrc", "apple-album", "itunes-song",
                   "youtube-age"]
_ADVISORY_LABELS = {
    "deezer-isrc": "Deezer (ISRC)",
    "spotify-isrc": "Spotify (ISRC)",
    "apple-album": "Apple (album editions)",
    "itunes-song": "iTunes (song search)",
    "youtube-age": "YouTube (age gate)",
}

# What each advisory route states — one line per route, taken from its own
# probe below (the field it reads and what it needs to run), never a claim the
# probe does not make.
_ADVISORY_PROVIDES = {
    "deezer-isrc": "the sample ISRC's Deezer track and its explicit flag — "
                   "keyless, asked first.",
    "spotify-isrc": "the sample ISRC's Spotify explicit flag; needs client "
                    "credentials, and a track Spotify does not have is its own "
                    "answer.",
    "apple-album": "the sample album's Apple edition and the "
                   "collectionExplicitness it states.",
    "itunes-song": "Apple's song search for the track, and its "
                   "trackExplicitness.",
    "youtube-age": "a track's YouTube video age gate; needs the video's id and "
                   "yt-dlp.",
}

# Genre source labels — the spellings the settings UI already shows.
_GENRE_LABELS = {
    "musicbrainz": "MusicBrainz",
}

# What each genre source actually contributes — one line, the settings tray
# shows it under the source's name so a tick reads as a choice rather than a
# name. Each line is the source's own paragraph above
# `server.integrations.GENRE_SOURCES` compressed to its point; the ORDER of
# the rows (and so of these lines) is that registry's priority list, and the
# rows carry `rank` — the 1-based position — for the same reason.
_GENRE_PROVIDES = {
    "musicbrainz": "each recording's own genres, else the release group's, "
                   "else the artist's — curated, and the app's identity anchor",
}

# What each credential row asks — one line, the check's own purpose, so the
# wizard's Keys step can say what pasting a key buys without a second list to
# keep in step with `server.credential_checks`.
_CREDENTIAL_PROVIDES = {
    "discogs": "proves the token with GET /oauth/identity — who it belongs to.",
    "lastfm": "proves the key with chart.gettoptags — a chart, so nothing else "
              "has to exist.",
    "lastfm-scrobble": "proves the SAVED session with an AUTHENTICATED read "
                       "(user.getInfo) — Last.fm's own words when it refuses, "
                       "and a blank key/secret/session is skipped, never a pass.",
    "spotify": "proves the pair with the same client-credentials grant the "
               "sources themselves need.",
    "musicbrainz": "proves the saved login with GET /oauth2/userinfo (renewing "
                   "the access token when it is due) — the account every "
                   "submission would be made by, or MusicBrainz's own refusal.",
    "acoustid": "proves the application key with one lookup on a probe "
                "fingerprint.",
    "acoustid-user": "proves the user key with a real probe SUBMISSION — the "
                     "one lookup a user key can answer.",
    "ai": "one tiny /chat/completions round trip with the key as a Bearer — "
          "can cost money.",
    "login": "this server's own gate: is anyone being asked to sign in, and "
             "would the stored claim let them.",
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


def _why(prefix, reason):
    """*prefix* with the provider's own refusal appended, when there is one.

    A keyed source that answers nothing and a keyed source that was REFUSED
    both leave an empty result behind; the provider's sentence is what tells
    them apart ("check the credentials" sends the user looking for a key that
    is already there)."""
    return f"{prefix} — {reason}" if reason else prefix


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
    from server import integrations as intg

    if pid == "deezer-isrc":
        hit = intg._deezer_advisory(SAMPLE_ISRC)
        if hit is None:
            return "fail", "no advisory data for the sample ISRC"
        return "ok", "explicit" if hit[0] else "clean (not explicit)"

    if pid == "spotify-isrc":
        started = time.time()
        hit = intg._spotify_advisory(SAMPLE_ISRC, cfg)
        if hit is None:
            return "fail", _why("no Spotify track for the sample ISRC — check "
                                "the credentials",
                                intg.spotify_last_error(started))
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
    from server import integrations as intg

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

    return "skipped", "unknown source"


# --------------------------------------------------------------------------- #
# Discover (/api/discover/*) — genre lists, genre browse, recommendations
# --------------------------------------------------------------------------- #
# One probe per registry source, each asking the thing that source is FOR, so
# the panel's answer means "this source can do its job here" rather than "a
# request did not raise". The registry note travels on the row (`notes`), which
# is what explains that TheAudioDB/Wikidata/Wikipedia are description and
# verification sources — they publish no genre list at all.
def _probe_discover(pid, cfg):
    from server import discovery
    from server import integrations as intg

    if pid == "musicbrainz":
        got = discovery.musicbrainz_genre_list(pages=1)
        detail = _count_detail([row["name"] for row in got["genres"]])
        if not detail:
            return "fail", "MusicBrainz stated no genres"
        if not got.get("done"):
            return "ok", "%s (partial: %d of %s)" % (detail, got["loaded"],
                                                     got["count"] or "?")
        return "ok", detail

    if pid == "deezer":
        detail = _count_detail([row["name"] for row in discovery.deezer_genre_list()])
        return ("ok", detail) if detail else ("fail", "no Deezer genre list")

    if pid == "itunes":
        got = discovery.itunes_genre_albums("shoegaze", limit=1)
        rows = got.get("rows") or []
        return ("ok", "%s for a genre search" % _count_detail(
            [r.get("title") for r in rows], "albums")) if rows \
            else ("fail", "no Apple genre album")

    if pid == "audiodb":
        row = discovery.audiodb_artist(SAMPLE_ARTIST) or {}
        parts = [p for p in (row.get("genre"), row.get("mood")) if p]
        return ("ok", " · ".join(parts) + " (states a named artist's genre, "
                                          "no list endpoint)") if parts \
            else ("fail", "TheAudioDB stated no genre for the sample artist")

    if pid == "lastfm":
        started = time.time()
        got = discovery.lastfm_tag_top("albums", "shoegaze", limit=1, cfg=cfg)
        detail = _count_detail([r.get("title") for r in got.get("rows") or []],
                               "albums")
        if detail:
            return "ok", "%s under the sample tag" % detail
        return "fail", _why("no Last.fm albums for the sample tag",
                            discovery.lastfm_last_error(started))

    if pid == "listenbrainz":
        mbid = _artist_mbid(cfg)
        if not mbid:
            return "fail", "could not resolve the sample artist on MusicBrainz"
        rows = discovery.listenbrainz_similar_artists(mbid, limit=1) or []
        return ("ok", _count_detail([r.get("title") for r in rows],
                                    "artists") + " similar to the sample") if rows \
            else ("fail", "no ListenBrainz similar artists")

    if pid == "discogs":
        started = time.time()
        got = discovery.discogs_style_search("Shoegaze", limit=1, cfg=cfg)
        rows = got.get("rows") or []
        if rows:
            return "ok", "%s under the sample style" % _count_detail(
                [r.get("title") for r in rows], "releases")
        got_http = discovery.last_http_error("api.discogs.com")
        reason = ""
        if got_http and float(got_http.get("at") or 0) >= started \
                and got_http.get("status"):
            reason = (f"Discogs answered HTTP {got_http['status']} "
                      f"{got_http['body']}").strip()
        return "fail", _why("no Discogs release for the sample style", reason)

    if pid == "wikidata":
        got = discovery.wikidata_genres(term="%s %s" % (SAMPLE_ARTIST, SAMPLE_ALBUM))
        detail = _count_detail((got or {}).get("genres"))
        return ("ok", "%s (description source, never a list)" % detail) if detail \
            else ("fail", "Wikidata stated no genres")

    if pid == "wikipedia":
        summary = discovery.wikipedia_summary(SAMPLE_ARTIST) or {}
        return ("ok", "article summary (description source, never a list)") \
            if summary.get("extract") or summary.get("description") \
            else ("fail", "no Wikipedia summary")

    if pid == "spotify":
        started = time.time()
        got = discovery.spotify_genre_albums("rock", limit=1, cfg=cfg)
        rows = got.get("rows") or []
        if rows:
            return "ok", "%s for a genre search" % _count_detail(
                [r.get("title") for r in rows], "albums")
        return "fail", _why("no Spotify genre album",
                            intg.spotify_last_error(started))

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

    if pid == "wikidata":
        url = discovery.wikidata_artist_image(SAMPLE_ARTIST, cfg=cfg)
        return ("ok", _image_detail("P18 image, Wikimedia Commons", url)) if url \
            else ("fail", "no Wikidata image (P18) for the sample artist")

    if pid == "discogs":
        if not str((cfg or {}).get("discogs_token") or "").strip():
            return "skipped", "needs a Discogs token (Settings → Sources)"
        url = discovery.discogs_artist_image(SAMPLE_ARTIST, cfg=cfg)
        return ("ok", _image_detail("artist image", url)) if url \
            else ("fail", "no Discogs artist image")

    if pid == "lastfm":
        if not str((cfg or {}).get("lastfm_api_key") or "").strip():
            return "skipped", "needs a Last.fm API key (Settings → Sources)"
        url = discovery.lastfm_artist_image(SAMPLE_ARTIST, cfg=cfg)
        return ("ok", _image_detail("artist photo", url)) if url \
            else ("fail", "no Last.fm artist photo")

    return "skipped", "unknown source"


# --------------------------------------------------------------------------- #
# Credentials — is the saved login accepted by the provider?
# --------------------------------------------------------------------------- #
def _probe_credentials(cid, cfg):
    """One credential's own verdict, from `server.credential_checks`.

    These rows are the only ones that may answer `fail` for a reason that is
    NOT about the sample: a refused token is refused whatever album is asked
    about, which is what makes them worth asking separately from the source
    that uses the credential."""
    from server import credential_checks

    return credential_checks.check(cid, cfg)


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
        # (1-based, the registry's own order), `notes` and `provides` all come
        # straight from the registry so the wizard reads them from ONE payload:
        # `notes` is the capability text the settings list shows and
        # `provides` is the same sentence for the wizard's Keys step, which
        # asks every kind of row "what do you provide to the app".
        specs.append({"id": pid, "kind": "lyrics", "label": src["label"],
                      "synced": True, "rank": src.get("rank"),
                      "notes": src.get("notes") or "",
                      "provides": src.get("notes") or "",
                      "needs": ["yt-dlp"] if pid == "youtube" else [],
                      "probe": lambda cfg, p=pid: _probe_lyrics(p, cfg)})

    for pid in _ADVISORY_ORDER:
        needs = {"spotify-isrc": ["spotify_client_id", "spotify_client_secret"],
                 "youtube-age": ["yt-dlp"]}.get(pid, [])
        specs.append({"id": pid, "kind": "advisory",
                      "label": _ADVISORY_LABELS[pid], "needs": list(needs),
                      "provides": _ADVISORY_PROVIDES.get(pid, ""),
                      "probe": lambda cfg, p=pid: _probe_advisory(p, cfg)})

    # The genre rows ARE the chain: `rank` is the registry's own 1-based
    # position (what the tray shows as the priority order it saves back) and
    # `provides` is what that source contributes. Both come from this one
    # place, so a source reordered above GENRE_SOURCES renumbers itself here.
    for rank, pid in enumerate(intg.GENRE_SOURCES, 1):
        specs.append({"id": pid, "kind": "genre", "rank": rank,
                      "provides": _GENRE_PROVIDES.get(pid, ""),
                      "label": _GENRE_LABELS.get(pid, pid), "needs": [],
                      "probe": lambda cfg, p=pid: _probe_genre(p, cfg)})

    for pid in discovery.IMAGE_SOURCES:
        specs.append({"id": pid, "kind": "metadata",
                      "label": discovery.SOURCE_LABELS.get(pid, pid),
                      # The two keyed providers say what they need, so the
                      # Sources panel prompts for the credential instead of
                      # showing a source that always fails.
                      "needs": (["lastfm_api_key"] if pid == "lastfm"
                                else ["discogs_token"] if pid == "discogs" else []),
                      "provides": discovery.SOURCE_NOTES.get(pid, ""),
                      "probe": lambda cfg, p=pid: _probe_metadata(p, cfg)})

    # Discover: `server.discover` IS the registry for the /api/discover/*
    # surface, so its sources are read from there — a source added to that
    # registry appears in this panel (and in the wizard) without a second list
    # to keep in step. `notes` is the source's own capability text.
    from server import discover as discover_mod

    for spec in discover_mod.SOURCES:
        specs.append({"id": spec["id"], "kind": "discover",
                      "label": spec["label"], "needs": list(spec["needs"]),
                      "notes": spec["note"], "provides": spec["note"],
                      "probe": lambda cfg, p=spec["id"]: _probe_discover(p, cfg)})

    # Credentials: the logins every row above depends on, each asked through
    # its provider's own credential endpoint (server.credential_checks). The
    # registry lives there rather than here because the checks belong beside
    # the request builders they exercise, and because a credential is not a
    # source: `needs` is what a person must paste, and an unset key is a
    # `needs <key>` row rather than silence.
    from server import credential_checks

    for spec in credential_checks.CREDENTIALS:
        specs.append({"id": spec["id"], "kind": "credentials",
                      "label": spec["label"], "needs": list(spec["needs"]),
                      "free": bool(spec["free"]),
                      "provides": _CREDENTIAL_PROVIDES.get(spec["id"], ""),
                      "probe": lambda cfg, p=spec["id"]: _probe_credentials(p, cfg)})

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


def _optional_keys(spec, cfg):
    """The `needs` entries whose absence does NOT stop this source — [] for
    most of them.

    A source is normally unrunnable without its key, which is why `needs` is
    also what the panel prompts for. A spec may name the exceptions in
    `optional`; the key stays in `needs` (the panel still prompts for it) and
    only the STATUS/`probe` decision consults this. `optional_gate` names the
    key that decides a GATED entry; a spec without one is unconditional.
    """
    out = list(spec.get("optional") or ())
    gate = str(spec.get("optional_gate") or "")
    if gate and not bool((cfg or {}).get(gate)):
        out = []
    return out


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
        optional = _optional_keys(spec, cfg)
        # What actually STOPS the source: a missing key that has no second
        # route. A missing key named in `optional` leaves the source runnable,
        # so it is `ok` and IS probed, while `configured` stays False so the
        # panel still prompts for it.
        blocking = [k for k in missing if k not in optional]
        if not missing:
            detail = "configured"
        elif blocking:
            detail = "needs " + ", ".join(blocking)
        else:
            detail = spec.get("optional_detail") or ("runs without "
                                                     + ", ".join(missing))
        row = {"id": spec["id"], "kind": spec["kind"], "label": spec["label"],
               "free": bool(spec.get("free", True)), "needs": list(spec["needs"]),
               "configured": not missing,
               "status": "skipped" if blocking else "ok",
               "detail": detail,
               "ms": 0}
        if optional:
            row["optional_needs"] = list(optional)
        if spec.get("synced"):
            row["synced"] = True
        if spec.get("rank") is not None:
            row["rank"] = spec["rank"]
        if spec.get("provides"):
            row["provides"] = spec["provides"]
        if spec.get("notes"):
            row["notes"] = spec["notes"]
        rows.append(row)
        if probe and not blocking:
            pending.append((row, spec))

    if pending:
        # MusicBrainz is reached through the sample artist's MBID and
        # rate-limits per host: resolve it ONCE, up here, so the parallel
        # probes below cannot race each other into a 503.
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
