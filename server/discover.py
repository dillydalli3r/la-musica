"""Discover — genre browsing, online recommendations and provider charts.

The four `/api/discover/*` endpoints answer from the app's LIBRARY and from
every external source that can speak about genres:

* **genres** — the genre list. The library's own genres are counted (tracks,
  albums, artists) with the payload's own reading (`mlo.genres`), and the
  sources that publish a genre LIST add their names; the two halves are merged
  case-insensitively, so one genre named by three of them is one row whose
  `sources` lists them.
* **genre** — one page of one genre, per source and per kind, in ONE row shape
  for albums, artists and tracks. A source that fails is reported in `notes`
  (never swallowed); rows from different sources that are the same release are
  merged by MBID (then by normalized artist+title) with the extra sources kept
  in `also_from`.
* **recommended** — online recommendations seeded by the library's own genre
  mix and top artists, or by one genre, with what the library already owns
  dropped and the reason each row was suggested. A `seed_kind` (artist / album
  / track) switches the same endpoint to ONE ENTITY — the page the shelf sits
  on — seeded by that entity's own MusicBrainz id when its tags carry one and
  by artist+title when they do not; an entity shelf is what the page is LIKE,
  so a row the library owns is KEPT and marked (`owned`, `path`) rather than
  dropped, and every source that cannot answer about an entity says why.
* **charts** — what the sources RANK, for one window (all-time / this year /
  this month / this week) and one kind. Unlike the two browse endpoints the
  rows are NOT merged: a chart's rank is its data, so each source's rows keep
  their own order, their own `rank` and their own score, and the page lists
  them source by source in `CHART_ORDER`. A source is asked only for the
  periods its registry row declares — a window it does not publish is
  `unsupported:` in `notes`, never its all-time chart passed off as this
  week's. The library's OWN charts (the user's play history) are a different
  surface: `GET /api/top` (server/api_plays.py), which never mixes a stored
  play count with a provider's ranking.

`SOURCES` below is the ONE place a source is described: its label, what it can
answer for which kind, whether it publishes a genre list or a recommendation
feed, which kinds it recommends for a genre seed (`rec_kinds`) and for ONE
entity (`entity_kinds` — the same list unless its entity answer is not a genre
search), which kinds it charts and for which windows, and the credential it
needs.
The endpoints are driven from it, so a source added there cannot be half-wired —
and `sources_health` reads it so the Sources panel probes exactly the same set.

Every call goes through `server.discovery`'s wrappers (which own the TTL cache
and the per-host throttles), `server.integrations`' cached MusicBrainz access,
or — for RateYourMusic, which has no API — that same module's RYM scrape, so
this module never opens a request of its own.

Honesty rules, which every answer here follows:

* a source that cannot run (no key) is `skipped: no <key>` in `notes`; a call
  that raised is `failed: <reason>` (the provider's own words); a source that
  answered has no note;
* a source that cannot answer a request at all (Deezer files music under 22
  broad genres, TheAudioDB publishes no browse) says so in `notes`, as does a
  chart source asked for a window it does not publish, instead of returning
  rows it cannot stand behind;
* an unknown genre or source is an EMPTY list, not an error — and never a
  guessed row;
* counts only ever come from the library: a source that merely names a genre
  has no track count, so an online-only genre is all zeroes.

Row shape (identical in `genre` and `recommended`, and what every Discover UI
renders; a chart row adds `rank`, `score` and `score_label`):

    kind, title, artist, year, source, source_label, cover_url, page_url,
    mbid, release_group_mbid, path, owned, in_library, tracks, score, reason,
    also_from

`owned` means the library already holds this exact release/artist/track —
matched by MBID first, then by normalized artist+title — and `path` is set only
then, so a row can link into the library. `in_library` is the weaker signal:
the library already holds something from this artist, so the row fills a gap
rather than being a new discovery. `also_from` lists the further sources that
named the same row, beyond its primary `source`. `score` is the provider's OWN
relevance for the row (Last.fm's `match`, Deezer's fans/rank, ListenBrainz's
score) — never a number this module invented, and null when the provider states
none. The providers' scales are not comparable, so it orders rows WITHIN one
source and is not a cross-provider percentage.
"""
from __future__ import annotations

from mlo.genres import canonical, display_name, iter_names
from server import discovery
from server import integrations

# The three row kinds every endpoint speaks; hidden API vocabulary, not a
# config key. The QUERY speaks the plural (`kind=albums`); a ROW carries the
# singular (`"kind": "album"`), which is what the row shape documents.
KINDS = ("albums", "artists", "tracks")
ROW_KIND = {"albums": "album", "artists": "artist", "tracks": "track"}
# A page at most this wide, and never deeper than MAX_OFFSET: the sources'
# own windows are that wide (MusicBrainz pages 100, Deezer charts 100, Apple
# 200) and a browse UI does not walk past it.
MAX_LIMIT = 100
MAX_OFFSET = 200
# Seed sizes for `recommended?seed=library` — a couple of genres and a couple
# of artists, so one loud genre cannot fill the whole shelf.
SEED_GENRES = 3
SEED_ARTISTS = 3
# The ENTITY kinds a shelf can be seeded by instead of a genre list: the page
# it sits on. The query speaks the singular (`seed_kind=artist`); the seeds a
# source is asked about come from that entity's own identity — its MusicBrainz
# id when its tags carry one, else its artist and name (see `entity_seed`).
SEED_KINDS = ("artist", "album", "track")
# How many related artists one source's bridge fans out over when an artist
# similarity has to become rows of ANOTHER kind (Deezer's related list → their
# albums / their top tracks), and how many rows each of them contributes. One
# request per artist: an entity shelf is a starting point, not a crawl, and
# every wrapper underneath is TTL-cached and throttled anyway.
RELATED_FANOUT = 3
RELATED_ROWS = 8
# How many of an ENTITY's own genres MusicBrainz is asked about, how many rows
# each of those tag searches may contribute, and how much of the artist's own
# records the browse contributes. The numbers are deliberately small: a shelf
# is about a dozen rows wide and MusicBrainz's rows LEAD it (registry order), so
# an unbounded tag search would BE the shelf and crowd out the sources that
# state a relationship with the page. MusicBrainz also answers one request per
# second — an entity shelf is a starting point, not a crawl. `RELATED_ROWS // 2`
# is half a bridge: the artist's own records sit BESIDE the genre rows here, not
# instead of them.
ENTITY_GENRES = 2
ENTITY_GENRE_ROWS = 2
ENTITY_BROWSE_ROWS = RELATED_ROWS // 2


def _source(sid, label, note, kinds=(), *, rec_kinds=(), entity_kinds=None,
            genres=False, needs=(), charts=(), chart_periods=()):
    return {"id": sid, "label": label, "note": note, "kinds": tuple(kinds),
            "rec_kinds": tuple(rec_kinds),
            "entity_kinds": tuple(rec_kinds if entity_kinds is None
                                  else entity_kinds),
            "genres": bool(genres),
            "needs": tuple(needs), "charts": tuple(charts),
            "chart_periods": tuple(chart_periods)}


# Registry order IS the preference order: the first source that named a row
# keeps it (`source`), the rest land in `also_from`, and the same order sorts
# the page. `kinds` is what the source can LIST for a genre; `rec_kinds` is
# what it can RECOMMEND for a GENRE seed (ListenBrainz has no genre filter at
# all, so it lists nothing but still recommends through its similar-artists
# feed and charts); `entity_kinds` is what it can say about ONE ENTITY — the
# page a shelf sits on — and only differs where a source's entity answer is
# NOT a genre search: Spotify's genre search serves albums only, while its
# artist-level albums and top tracks answer an album and a track page, and
# neither Apple nor Spotify publishes a related-ARTIST feed at all;
# `charts` is what it can RANK for the Charts page, with the windows it really
# publishes in `chart_periods` — a source is asked for a period it does not
# have ONLY to be reported as unsupported, never to be handed all-time instead.
SOURCES = (
    _source("musicbrainz", "MusicBrainz",
            "Genre (tag) search over release groups, artists and recordings, "
            "and the genre vocabulary itself. It publishes no similar-entity "
            "feed, so an entity shelf reads the entity's own genres and asks "
            "that same search, plus the artist's own records.",
            KINDS, rec_kinds=KINDS, genres=True),
    _source("deezer", "Deezer",
            "Its own genre charts (albums, tracks), artists-by-genre for "
            "Deezer's 22 broad genres, related artists, an artist's albums and "
            "its top tracks — plus its current global chart.",
            KINDS, rec_kinds=KINDS, genres=True,
            charts=KINDS, chart_periods=("all",)),
    _source("itunes", "iTunes",
            "Apple's genreIndex album search, and its most-played songs feed — "
            "a rolling chart Apple refreshes daily and dates nowhere. Its "
            "keyless search also answers an artist's own albums for an entity "
            "shelf; Apple publishes no related-artist feed.",
            ("albums",), rec_kinds=("albums",),
            charts=("tracks",), chart_periods=("all",)),
    _source("audiodb", "TheAudioDB",
            "States the genre and mood of a NAMED artist or album — it "
            "publishes no genre list and no genre browse (its /genres.php "
            "answers 404, verified)."),
    _source("lastfm", "Last.fm",
            "Tag charts: the most-listened albums, artists and tracks under a "
            "tag, its top tag list, similar artists/tracks, and its sitewide "
            "all-time charts.",
            KINDS, rec_kinds=KINDS, genres=True, needs=("lastfm_api_key",),
            charts=KINDS, chart_periods=("all",)),
    _source("listenbrainz", "ListenBrainz",
            "Similar artists (Labs, keyless) and the sitewide most-listened "
            "charts, with this week / this month / this year / all-time "
            "windows. LB Radio itself needs a user token (verified 401), so it "
            "is not wired, and there is no keyless genre filter.",
            (), rec_kinds=KINDS,
            charts=KINDS, chart_periods=("all", "year", "month", "week")),
    _source("discogs", "Discogs",
            "Release browse by style/genre. Anonymous search answers it; a "
            "discogs_token only raises its rate limit.",
            ("albums",), rec_kinds=("albums",)),
    _source("wikidata", "Wikidata",
            "States an entity's genres (P136) for a known artist or release — "
            "a description source, never a list."),
    _source("wikipedia", "Wikipedia",
            "Summarises an artist or an album — a description source, never a "
            "list."),
    _source("spotify", "Spotify",
            "Album search filtered by Spotify's own genre names, and — for an "
            "entity shelf — a NAMED artist's albums and top tracks. Spotify's "
            "related-artists and recommendations endpoints are closed to apps "
            "created after 2024-11-27 (its own announcement), so it states no "
            "similarity.",
            ("albums",), rec_kinds=("albums",),
            entity_kinds=("albums", "tracks"),
            needs=("spotify_client_id", "spotify_client_secret")),
    # RateYourMusic is here for its CHARTS only: it publishes no genre list and
    # no recommendation feed, and its genre reading lives in the import chain
    # (`server.integrations`). It is last in this list on purpose — the registry
    # order is the genre/rec merge order — and first in `CHART_ORDER` below,
    # where the user asked for it as the primary track source.
    _source("rym", "RateYourMusic",
            "Its own user-ranked song charts (all-time and per year), scraped "
            "from the site — RYM has no API and refuses an automated client "
            "without a rym_cookie, so archived snapshots answer behind the live "
            "route. Tracks only; no month or week chart exists.",
            charts=("tracks",), chart_periods=("all", "year")),
)
BY_ID = {spec["id"]: spec for spec in SOURCES}
SOURCE_LABELS = {spec["id"]: spec["label"] for spec in SOURCES}
_ORDER = {spec["id"]: i for i, spec in enumerate(SOURCES)}

# The windows a chart can be asked for, and the order the CHART sources are
# asked in — which is NOT the registry order, because the two list orders mean
# different things. The registry's is the genre/rec merge preference; this one
# is the user's own: RateYourMusic is the PRIMARY track-chart source, so its
# rows lead the page for `kind=tracks` (and it is the only chart source for
# tracks-only windows it publishes). The rest keep the registry's relative
# order, so a source added there cannot fall out of the charts.
CHART_PERIODS = ("all", "year", "month", "week")
CHART_KINDS = KINDS
CHART_FIRST = "rym"
CHART_ORDER = tuple([CHART_FIRST]
                    + [spec["id"] for spec in SOURCES
                       if spec["charts"] and spec["id"] != CHART_FIRST])
_CHART_RANK = {sid: i for i, sid in enumerate(CHART_ORDER)}


class _Skip(Exception):
    """A source cannot answer THIS request — a capability or name mismatch
    ("Deezer has no genre called shoegaze"), not a failure. The caller reports
    it as `skipped:`, where a raised call is `failed:`."""


class _Asked:
    """Who was asked, and what each one said when it could not answer.

    `ids` is the answer's `sources_asked`; `notes` holds ONLY the sources that
    did not answer — `skipped: …` for a missing credential or a capability the
    source does not have, `failed: …` for a call that raised. A source that
    answered has no note, so an empty note map means every asked source
    answered."""

    def __init__(self):
        self.ids = []
        self.notes = {}

    def ask(self, sid):
        if sid not in self.ids:
            self.ids.append(sid)

    def note(self, sid, text):
        self.notes[sid] = text


def catalogue(cfg=None):
    """The registry with what each source can do HERE — the one payload that
    says a source, its capabilities and its credential. `sources_health` builds
    its Discover probe rows from it, so a source added above appears in the
    Sources panel without a second list to keep in step."""
    return {
        "kinds": list(KINDS),
        "chart_periods": list(CHART_PERIODS),
        "sources": [{
            "id": spec["id"], "label": spec["label"], "note": spec["note"],
            "genres": spec["genres"], "kinds": list(spec["kinds"]),
            "rec_kinds": list(spec["rec_kinds"]),
            "entity_kinds": list(spec["entity_kinds"]),
            "needs": list(spec["needs"]),
            "charts": list(spec["charts"]),
            "chart_periods": list(spec["chart_periods"]),
            "missing": missing_keys(spec, cfg), "ready": can_run(spec, cfg),
        } for spec in SOURCES],
    }


def missing_keys(spec, cfg):
    """The config keys this source still needs — [] when it can run."""
    return [key for key in spec["needs"]
            if not str((cfg or {}).get(key) or "").strip()]


def can_run(spec, cfg):
    return not missing_keys(spec, cfg)


def skip_note(spec, cfg):
    """`skipped: no <key>` for a source that cannot run here, else ""."""
    missing = missing_keys(spec, cfg)
    if not missing:
        return ""
    return "skipped: no " + ", no ".join(missing)


def _safe_int(value, default):
    """An int, or *default* for anything unusable (a query argument arriving
    as None, "", a list — the route bounds its own, this only keeps a direct
    caller honest)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _window(limit, offset):
    """A bounded page (defensive clamp; the route refuses a deeper offset)."""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 25
    try:
        offset = int(offset or 0)
    except (TypeError, ValueError):
        offset = 0
    return max(1, min(MAX_LIMIT, limit)), max(0, min(MAX_OFFSET, offset))


def _genre_key(name):
    """The merge key: MusicBrainz's canonical spelling, folded. One genre named
    by three sources with three casings is one genre."""
    text = str(canonical(name) or name or "").strip()
    return " ".join(text.split()).casefold()


def _genre_display(name):
    """The app's spelling of a genre name for the Discover list (the writers'
    Title Case, `mlo.genres.display_name`), so the list reads the same whatever
    a tagger stored or a provider published.

    A name carrying a separator is a PROVIDER's compound label rather than one
    genre name (Deezer files music under "Rap/Hip Hop"), and `display_name`
    would render that "Rap/hip" — it is left exactly as published."""
    text = str(canonical(name) or name or "").strip()
    if "/" in text:
        return text
    return display_name(text) or text


# --------------------------------------------------------------------------- #
# The library, read as Discover needs it
# --------------------------------------------------------------------------- #
def library_view(lib):
    """One pass over the library payload the app already builds.

    `genres` counts the tracks, albums and artists carrying each genre; the
    reading is the payload's OWN (`mlo.genres.iter_names` over each track's
    GENRE tag, then `canonical`/`display_name`), the same one the genre chain,
    the grader and /api/genres/facets use — Discover can therefore never
    disagree with the rest of the app about what a track is filed under.

    `artists`/`albums`/`tracks` are the owned index: MusicBrainz ids and
    normalized artist+title keys, each pointing at the library path the row
    links to. `genres` and `artists` are ordered containers; the rest are
    lookups."""
    genres, genre_artists = {}, {}
    artists, albums, tracks = {}, {}, {}
    artist_mbids, album_mbids, track_mbids = {}, {}, {}

    def genre_names(tags):
        names = []
        for piece in iter_names((tags or {}).get("GENRE")):
            name = _genre_display(piece)
            if name and name not in names:
                names.append(name)
        return names

    for artist in (lib or {}).get("artists") or []:
        folder = str(artist.get("path") or "").replace("\\", "/")
        aname = str(artist.get("name") or "").strip()
        akey = discovery.norm(aname)
        ainfo = artists.get(akey) if akey else None
        if ainfo is None and akey:
            ainfo = {"name": aname, "tracks": 0, "albums": 0, "path": folder}
            artists[akey] = ainfo
        for alb in artist.get("albums") or []:
            meta = alb.get("meta") or {}
            album_artist = str(meta.get("ALBUMARTIST") or meta.get("ARTIST")
                               or aname or "").strip()
            album_name = str(meta.get("ALBUM") or "").strip()
            apath = str(alb.get("path") or "").replace("\\", "/")
            rel_group = str(meta.get("MUSICBRAINZ_RELEASEGROUPID") or "").strip().lower()
            rel_id = str(meta.get("MUSICBRAINZ_ALBUMID") or "").strip().lower()
            titles, album_genres = [], set()
            for tr in alb.get("tracks") or []:
                tags = tr.get("tags") or {}
                tname = str(tags.get("TITLE") or "").strip()
                tartist = str(tags.get("ARTIST") or album_artist or aname).strip()
                if tname:
                    titles.append(tname)
                for name in genre_names(tags):
                    counts = genres.setdefault(name, {"track_count": 0,
                                                      "album_count": 0,
                                                      "artist_count": 0})
                    counts["track_count"] += 1
                    album_genres.add(name)
                    genre_artists.setdefault(name, set()).add(
                        akey or discovery.norm(tartist))
                tkey = (discovery.norm(tartist), discovery.norm(tname))
                if tkey[1] and tkey not in tracks:
                    tmbid = str(tags.get("MUSICBRAINZ_TRACKID") or "").strip().lower()
                    tracks[tkey] = {"path": str(tr.get("path") or "").replace("\\", "/"),
                                    "mbid": tmbid or None, "artist": tartist,
                                    "title": tname, "album": album_name}
                    if tmbid:
                        track_mbids.setdefault(tmbid, tracks[tkey])
                if ainfo is not None:
                    for key in ("MUSICBRAINZ_ALBUMARTISTID", "MUSICBRAINZ_ARTISTID"):
                        vid = str(tags.get(key) or "").strip().lower()
                        if vid:
                            artist_mbids.setdefault(vid, ainfo)
            for name in album_genres:
                genres[name]["album_count"] += 1
            akey_album = (discovery.norm(album_artist), discovery.norm(album_name))
            if ainfo is not None:
                ainfo["albums"] += 1
                ainfo["tracks"] += len(alb.get("tracks") or [])
            if akey_album[1]:
                albums.setdefault(akey_album, {
                    "artist": album_artist, "title": album_name, "path": apath,
                    "tracks": titles, "mbid": rel_group or rel_id or None,
                    "year": str(meta.get("DATE") or "")[:4],
                })
                for mbid in (rel_group, rel_id):
                    if mbid:
                        album_mbids.setdefault(mbid, albums[akey_album])

    for name, buckets in genre_artists.items():
        genres[name]["artist_count"] = len(buckets)
    return {"genres": genres, "artists": artists, "artist_mbids": artist_mbids,
            "albums": albums, "album_mbids": album_mbids,
            "tracks": tracks, "track_mbids": track_mbids}


def _top_genres(index, count):
    """The library's genres by track count (its loudest genres first)."""
    ranked = sorted(index["genres"].items(),
                    key=lambda kv: (-kv[1]["track_count"], kv[0].casefold()))
    return [name for name, _counts in ranked[:max(0, count)]]


def _top_artists(index, count):
    """The library's most-collected artists (by tracks, then by albums)."""
    ranked = sorted(index["artists"].values(),
                    key=lambda a: (-a["tracks"], -a["albums"],
                                   str(a["name"]).casefold()))
    return [a["name"] for a in ranked[:max(0, count)] if a["name"]]


def _holdings(index, row):
    """(owned, in_library, path, track titles) for one online row.

    MBID first — an identity, not a guess — then the normalized artist+title
    pair the whole app matches by. `path` comes from the library entry itself,
    so the UI links to the album/artist/track that IS in the library."""
    kind = row.get("kind")
    mbid = str(row.get("mbid") or "").strip().lower()
    release_group = str(row.get("release_group_mbid") or "").strip().lower()
    artist = discovery.norm(row.get("artist"))
    title = discovery.norm(row.get("title"))

    if kind == "album":
        for key in (release_group, mbid):
            hit = index["album_mbids"].get(key) if key else None
            if hit:
                return True, True, hit["path"], hit["tracks"]
        hit = index["albums"].get((artist, title)) if title else None
        if hit:
            return True, True, hit["path"], hit["tracks"]
        return False, bool(artist) and artist in index["artists"], None, []
    if kind == "track":
        hit = index["track_mbids"].get(mbid) if mbid else None
        if hit is None and title:
            hit = index["tracks"].get((artist, title))
        if hit:
            return True, True, hit["path"], []
        return False, bool(artist) and artist in index["artists"], None, []
    hit = index["artist_mbids"].get(mbid) if mbid else None
    if hit is None and artist:
        hit = index["artists"].get(artist)
    if hit:
        return True, True, hit["path"], []
    return False, False, None, []


# --------------------------------------------------------------------------- #
# Rows: merge, order, and the one public shape
# --------------------------------------------------------------------------- #
def _rank_of(row):
    """The source's own relevance for a row: MusicBrainz's `score`, Deezer's
    `rank`/`fans`, Last.fm's `match`. A source with none keeps insertion
    order."""
    for key in ("score", "popularity", "match"):
        value = row.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def merge_rows(rows):
    """One row per release, however many sources named it.

    Identity is the MBID when there is one and the normalized artist+title
    otherwise; a row with an MBID also registers its name key, so a name-only
    source naming the same album merges into it. The FIRST source in registry
    order keeps the row and is its `source`/`source_label` — registry order IS
    the preference order, so that source's `_reason` is the one the shelf
    shows — and every further source is listed in `also_from`, with a field the
    primary lacks (a cover, a year, an MBID) filled from the duplicate rather
    than lost. A duplicate's reason is taken only when the winner stated none,
    so a row two sources agree on still explains itself."""
    merged, by_mbid, by_name = [], {}, {}
    for row in rows:
        mbid = str(row.get("mbid") or "").strip().lower()
        name = (discovery.norm(row.get("artist")), discovery.norm(row.get("title")))
        hit = by_mbid.get(mbid) if mbid else None
        if hit is None and name[1]:
            hit = by_name.get(name)
        if hit is None:
            hit = dict(row)
            hit["_source"] = row.get("_source")
            hit["_also"] = []
            hit["_rank"] = _rank_of(row)
            merged.append(hit)
        else:
            for key, value in row.items():
                if key.startswith("_"):
                    continue
                if value and not hit.get(key):
                    hit[key] = value
            if not hit.get("_reason") and row.get("_reason"):
                # The winner never stated why; the duplicate's reason is better
                # than an empty line, and it is that source's own words.
                hit["_reason"] = row["_reason"]
            source = row.get("_source")
            if source and source != hit.get("_source") and source not in hit["_also"]:
                hit["_also"].append(source)
        if mbid:
            by_mbid.setdefault(mbid, hit)
        if name[1]:
            by_name.setdefault(name, hit)
    return merged


def sort_rows(rows):
    """Registry order first (a source's own rows keep its ranking), then the
    source's score, then the title."""
    return sorted(rows, key=lambda row: (_ORDER.get(row.get("_source"), len(SOURCES)),
                                         -_rank_of(row),
                                         str(row.get("title") or "").casefold()))


def finalize_row(row, index, reason):
    """The one public row shape (see the module docstring)."""
    kind = str(row.get("kind") or "")
    source = row.get("_source") or ""
    owned, in_library, path, titles = _holdings(index, row)
    return {
        "kind": kind,
        "title": str(row.get("title") or ""),
        "artist": str(row.get("artist") or ""),
        "year": str(row.get("year") or ""),
        "source": source,
        "source_label": SOURCE_LABELS.get(source, source),
        "cover_url": row.get("cover") or None,
        "page_url": row.get("link") or None,
        "mbid": row.get("mbid") or None,
        "release_group_mbid": (row.get("release_group_mbid")
                               or (row.get("mbid") if kind == "album" else None)),
        "path": path if owned else None,
        "owned": bool(owned),
        "in_library": bool(in_library),
        "tracks": list(titles),
        "score": _rank_of(row) or None,
        "reason": reason,
        "also_from": list(row.get("_also") or []),
    }


def _shelf_items(rows, index, limit, reason, drop_owned):
    """The merged shelf: registry order, ONE row per identity, each carrying
    the provider that named it first and the reason it was suggested.

    `drop_owned` is what separates the two shelves this endpoint serves. A
    library-seeded shelf is a shopping list — what the user already has is not
    a recommendation — while an ENTITY shelf (the artist/album/track page) is
    "what this page is like", where an owned row IS an answer: it keeps its
    `owned` flag and its library `path`, so the UI can open it rather than
    offer to add it twice."""
    items = []
    for row in sort_rows(merge_rows(rows)):
        out = finalize_row(row, index, row.get("_reason") or reason)
        if drop_owned and out["owned"]:
            continue
        items.append(out)
        if len(items) >= limit:
            break
    return items


def _nothing_note(cfg, kind, seed_label):
    """The verdict for an empty shelf, as its own sentence in `notes`: a server
    with no usable source for this kind is a different statement from sources
    that answered nothing, and neither may read as a rendering bug."""
    if any(can_run(spec, cfg) for spec in SOURCES if kind in spec["rec_kinds"]):
        return ("no recommendation source had anything to suggest for %s"
                % seed_label)
    return "skipped: no recommendation source is configured for %s" % kind


# --------------------------------------------------------------------------- #
# genres — the library's list and the sources' lists, merged
# --------------------------------------------------------------------------- #
def _adapt(kind, row):
    """A `server.discovery` row as the Discover row shape.

    The provider wrappers predate Discover and each speaks its own dialect —
    Deezer's related artists carry `name`/`image`, Last.fm's `name`, a
    MusicBrainz release group `title`/`cover`/`link` — so this is the ONE place
    that folds them into the shape the endpoints promise. A provider's own
    vocabulary never reaches the API, and a wrapper gaining a key costs nothing
    here."""
    out = dict(row)
    row_kind = ROW_KIND.get(kind, kind)
    out["kind"] = row_kind
    if not out.get("title"):
        out["title"] = row.get("name") or ""
    if not out.get("artist"):
        artist = row.get("artist")
        if isinstance(artist, dict):
            artist = artist.get("name")
        # An artist row's subject IS its name; every other kind needs one.
        out["artist"] = artist or (row.get("name") if row_kind == "artist" else "") or ""
    if not out.get("cover"):
        out["cover"] = row.get("cover") or row.get("image") or row.get("thumbnail")
    if not out.get("link"):
        out["link"] = row.get("link") or row.get("url")
    return out


def _taken(rows, kind, reason=None):
    """One provider answer as Discover rows, each optionally carrying `_reason`."""
    out = []
    for row in rows or []:
        row = _adapt(kind, row)
        if reason:
            row["_reason"] = reason
        out.append(row)
    return out


def _genre_list(sid, cfg):
    """(names, note) from one source that publishes a genre LIST.

    The note is for a PARTIAL answer (MusicBrainz's taxonomy arrives a page at
    a time) — an honest "this is not the whole list" rather than a silent
    truncation."""
    if sid == "musicbrainz":
        got = discovery.musicbrainz_genre_list()
        note = ""
        if not got.get("done"):
            note = ("partial: %d of %s genres — the taxonomy is fetched a page "
                    "at a time, ask again for more"
                    % (got["loaded"], got["count"] or "?"))
        return [row["name"] for row in got["genres"]], note
    if sid == "deezer":
        return [row["name"] for row in discovery.deezer_genre_list()], ""
    if sid == "lastfm":
        # Last.fm's tag chart names moods as well as genres ("chill"); the
        # genre chain drops the same words from free tags, one rule for both.
        return [name for name in discovery.lastfm_top_tags(100, cfg=cfg)
                if str(name).strip().lower() not in discovery.LB_MOOD_WORDS], ""
    raise _Skip("no genre list from %s" % sid)


def genres_payload(cfg=None, scope="all", lib=None):
    """The genre list: the library's own genres, the online lists, or both.

    `scope` filters which half is returned (`library`/`online`/`all`). Counts
    come only from the library — a source that merely NAMES a genre has no
    track count — so an online-only genre carries zeroes and is honest about
    it through `sources`. Rows are ordered library-first (by track count), then
    by how many online sources name them (the genres several publish are the
    ones that matter), then alphabetically — so the list a user actually has
    music under leads, and MusicBrainz's alphabetically-paged taxonomy cannot
    open the list on its most obscure entry."""
    scope = str(scope or "all").strip().lower()
    asked = _Asked()
    rows = {}

    def add(name, sid, counts=None):
        key = _genre_key(name)
        if not key:
            return
        row = rows.get(key)
        if row is None:
            row = {"name": _genre_display(name), "track_count": 0, "album_count": 0,
                   "artist_count": 0, "sources": []}
            rows[key] = row
        if sid not in row["sources"]:
            row["sources"].append(sid)
        if counts:
            for field in ("track_count", "album_count", "artist_count"):
                row[field] += counts.get(field, 0)

    if scope in ("library", "all"):
        index = library_view(_library(cfg, lib))
        for name, counts in index["genres"].items():
            add(name, "library", counts)
    if scope in ("online", "all"):
        for spec in SOURCES:
            if not spec["genres"]:
                continue
            asked.ask(spec["id"])
            if not can_run(spec, cfg):
                asked.note(spec["id"], skip_note(spec, cfg))
                continue
            try:
                names, note = _genre_list(spec["id"], cfg)
            except Exception as e:
                asked.note(spec["id"], "failed: %s" % e)
                continue
            if note:
                asked.note(spec["id"], note)
            for name in names:
                add(name, spec["id"])

    ordered = sorted(rows.values(),
                     key=lambda row: (0 if "library" in row["sources"] else 1,
                                      -len(row["sources"]),
                                      -row["track_count"],
                                      row["name"].casefold()))
    return {"genres": ordered, "sources_asked": asked.ids, "notes": asked.notes}


def _library(cfg, lib):
    """The library payload, built through the app's own cached builder."""
    if lib is not None:
        return lib
    from server import library as library_mod

    return library_mod.build_library(cfg or {})


# --------------------------------------------------------------------------- #
# genre — one page of one genre, per source and per kind
# --------------------------------------------------------------------------- #
def _browse(sid, kind, genre, limit, offset, cfg):
    """One source's rows for a genre+kind, through `server.discovery`.

    Raises `_Skip` when the source cannot answer this genre at all, and
    anything else when the call failed — the caller reports the two
    differently, and neither is swallowed."""
    if sid == "musicbrainz":
        return discovery.musicbrainz_tag_search(kind, genre, limit=limit, offset=offset)
    if sid == "deezer":
        genre_id = discovery.deezer_genre_id(genre)
        if not genre_id:
            raise _Skip('Deezer has no genre called "%s" — its own list is 22 '
                        'broad genres' % genre)
        return discovery.deezer_genre_browse(genre_id, kind, limit=limit, offset=offset)
    if sid == "itunes":
        return discovery.itunes_genre_albums(genre, limit=limit, offset=offset, cfg=cfg)
    if sid == "discogs":
        return discovery.discogs_style_search(genre, limit=limit, offset=offset, cfg=cfg)
    if sid == "lastfm":
        return discovery.lastfm_tag_top(kind, genre, limit=limit, offset=offset, cfg=cfg)
    if sid == "spotify":
        return discovery.spotify_genre_albums(genre, limit=limit, offset=offset, cfg=cfg)
    raise _Skip("no %s list from this source" % kind)


def genre_payload(cfg=None, genre="", kind="albums", source="all", limit=25,
                  offset=0, lib=None):
    """One page of one genre, per source and per kind.

    `source=all` merges every source that can list this kind; one source id
    answers from that source alone. Failures and capability mismatches are
    reported per source in `notes`, every row carries its provenance, and
    `next_offset` is set while any source still states more — the page is
    bounded, so a caller walks it deliberately rather than on faith."""
    genre = str(genre or "").strip()
    kind = str(kind or "").strip().lower()
    source = str(source or "all").strip().lower() or "all"
    limit, offset = _window(limit, offset)
    base = {"genre": genre, "kind": kind, "source": source, "items": [],
            "sources_asked": [], "notes": {}, "next_offset": None}
    if not genre:
        return base
    if source != "all" and source not in BY_ID:
        # An id we do not know is an empty answer that SAYS so, not an error
        # and not somebody else's rows.
        base["notes"] = {source: "unknown source"}
        return base

    specs = [spec for spec in SOURCES if kind in spec["kinds"]]
    if source != "all":
        wanted = BY_ID[source]
        specs = [wanted] if wanted in specs else []
        if not specs:
            # A real source that does not list this kind: the registry's own
            # note says what it does instead of an invented empty answer.
            base["notes"] = {source: "cannot list %s: %s" % (kind, wanted["note"])}
            return base

    asked = _Asked()
    index = library_view(_library(cfg, lib))
    rows, more = [], False
    for spec in specs:
        asked.ask(spec["id"])
        if not can_run(spec, cfg):
            asked.note(spec["id"], skip_note(spec, cfg))
            continue
        try:
            got = _browse(spec["id"], kind, genre, limit, offset, cfg)
        except _Skip as e:
            asked.note(spec["id"], "skipped: %s" % e)
            continue
        except Exception as e:
            asked.note(spec["id"], "failed: %s" % e)
            continue
        total = got.get("total")
        if total is not None and total > offset + limit:
            more = True
        if len(got["rows"]) >= limit:
            more = True
        for row in _taken(got["rows"], kind):
            row["_source"] = spec["id"]
            rows.append(row)

    items = [finalize_row(row, index, "genre: %s" % genre)
             for row in sort_rows(merge_rows(rows))]
    return {"genre": genre, "kind": kind, "source": source, "items": items,
            "sources_asked": asked.ids, "notes": asked.notes,
            "next_offset": (offset + limit) if (more and items) else None}


# --------------------------------------------------------------------------- #
# recommended — online suggestions seeded by the library, by one genre, or by
# ONE ENTITY (the artist/album/track page the shelf sits on)
# --------------------------------------------------------------------------- #
def _recommend_rows(sid, kind, cfg, genres, artists, limit, seed):
    """Rows ONE source recommends for these seeds, each carrying `_reason`.

    What the reason says is the source's own concept of a recommendation: a tag
    chart is "genre: shoegaze (Last.fm tag)", a similar-artist feed is
    "sounds like Slowdive (ListenBrainz)", a discography is "more from Slowdive
    (Deezer)". Raises `_Skip` when the source needs a seed this request does
    not have (a genre it cannot filter by, an artist it cannot resolve)."""
    rows = []

    def take(found, why):
        rows.extend(_taken(found, kind, why))

    if sid in ("musicbrainz", "itunes", "discogs", "spotify"):
        if not genres:
            raise _Skip("needs a genre seed and this request has none")
        for genre in genres:
            take(_browse(sid, kind, genre, limit, 0, cfg)["rows"],
                 "genre: %s (%s)" % (genre, SOURCE_LABELS[sid]))
        return rows

    if sid == "deezer":
        for genre in genres:
            genre_id = discovery.deezer_genre_id(genre)
            if genre_id:
                take(discovery.deezer_genre_browse(genre_id, kind, limit=limit)["rows"],
                     "genre: %s (Deezer chart)" % genre)
        for artist in artists:
            if kind == "artists":
                take(discovery.deezer_related_artists(artist, limit),
                     "sounds like %s (Deezer)" % artist)
            elif kind == "albums":
                take(discovery.deezer_artist_albums(artist, limit, albums_only=True),
                     "more from %s (Deezer)" % artist)
            else:
                take(discovery.deezer_artist_top(artist, limit),
                     "more from %s (Deezer)" % artist)
        if not rows:
            # Nothing came back: say WHICH half of Deezer could not help rather
            # than leaving the caller to guess from an empty list.
            if genres:
                raise _Skip('Deezer has no genre called "%s" — its own list is 22 '
                            'broad genres' % genres[0])
            raise _Skip("no artist seed for Deezer to work from")
        return rows

    if sid == "lastfm":
        for genre in genres:
            take(discovery.lastfm_tag_top(kind, genre, limit=limit, cfg=cfg)["rows"],
                 "genre: %s (Last.fm tag)" % genre)
        if kind == "artists":
            for artist in artists:
                take(discovery.lastfm_similar_artists(artist, limit, cfg=cfg),
                     "sounds like %s (Last.fm)" % artist)
        return rows

    if sid == "listenbrainz":
        if kind == "artists":
            for artist in artists:
                mbid = discovery.resolve_artist_mbid(artist, cfg)
                if not mbid:
                    continue
                take(discovery.listenbrainz_similar_artists(mbid, limit),
                     "sounds like %s (ListenBrainz)" % artist)
            return rows
        if seed != "library":
            # Its charts are sitewide, not genre-filtered, so they are NOT an
            # answer to "more music like this genre" — say so instead.
            raise _Skip("ListenBrainz has no keyless genre filter (LB Radio "
                        "needs a user token)")
        take(discovery.listenbrainz_top_releases(limit=limit) if kind == "albums"
             else discovery.listenbrainz_top_recordings(limit=limit),
             "most listened this month (ListenBrainz)")
        return rows

    raise _Skip("no recommendations from this source")


# What a source that CANNOT answer about an entity says instead: one honest
# sentence each, kept beside the registry so a reader of `notes` learns the
# provider's own limit instead of guessing from an empty list. A source that
# declares no entity capability is never asked at all; anything not named here
# and not handled below falls through to the generic sentence. MusicBrainz and
# Spotify used to be listed here and are implemented instead — a note is for a
# source that HAS no such feed, not for one nobody wrote the call for.
_ENTITY_NOTES = {
    "discogs": "Discogs browses by style, and publishes no similar-entity feed",
}


def entity_seed(seed_kind, mbid="", name="", artist=""):
    """The entity a shelf is built around, as `(seed, why)`.

    A page seeds by IDENTITY first: `mbid` when its tags carry one, else by
    name — an artist by its own name, an album or a track by artist + title.
    `why` is "" when the seed is usable and the honest reason it is not
    otherwise: a page that named nothing cannot be recommended around, and that
    is said rather than answered with somebody else's rows. An artist seed's
    `artist` IS its name — the one name a similar-artist feed and a
    related-artist bridge both need."""
    seed = {"kind": str(seed_kind or "").strip().lower(),
            "mbid": str(mbid or "").strip().lower(),
            "name": str(name or "").strip(),
            "artist": str(artist or "").strip()}
    if seed["kind"] not in SEED_KINDS:
        return seed, ("unknown seed kind %r — an entity shelf is seeded by one "
                      "of: %s" % (seed_kind, ", ".join(SEED_KINDS)))
    if seed["kind"] == "artist":
        seed["artist"] = seed["name"]
        if not seed["name"]:
            return seed, "this page names no artist to seed from"
        return seed, ""
    if not seed["name"]:
        return seed, "this page names no %s to seed from" % seed["kind"]
    if not seed["artist"]:
        return seed, ("this page names no artist for its %s, and every source "
                      "asks for %s rows by that artist"
                      % (seed["kind"], seed["kind"]))
    return seed, ""


def _entity_subject(seed):
    """The entity as a reader says it ("Slowdive", "Slowdive — Souvlaki")."""
    if seed["kind"] == "artist":
        return seed["name"]
    return "%s — %s" % (seed["artist"], seed["name"])


def _entity_basis(seed):
    """What the shelf was built from, in the payload's own words — and WHICH
    form of identity it was built from, because "by name" is a real answer to
    a real page and says the row's reach is only as good as that name."""
    return "%s: %s (%s)" % (seed["kind"], _entity_subject(seed),
                            seed["mbid"] or "by name")


def _entity_rows(sid, kind, cfg, seed, limit):
    """Rows ONE source recommends for ONE entity seed, each carrying `_reason`.

    What a source can say about an entity is NOT what it can say about a genre:
    a similar-artist feed answers an artist page directly, and a related-artist
    list is the bridge to rows of another kind (their albums, their top
    tracks) — never a genre chart or a sitewide chart passed off as "like this
    page". Every row's reason names the provider and the relationship it
    states, and a source with no entity feed at all raises `_Skip` with its own
    sentence instead of returning nothing quietly."""
    rows = []
    by = seed["artist"] or seed["name"]

    def take(found, why):
        rows.extend(_taken(found, kind, why))

    def bridge(names, why_of):
        """One related artist at a time: their albums/tracks, each row saying
        which relationship brought it here."""
        for name in names:
            if kind == "albums":
                found = discovery.deezer_artist_albums(name, RELATED_ROWS,
                                                       albums_only=True)
            elif kind == "tracks":
                found = discovery.deezer_artist_top(name, RELATED_ROWS)
            else:
                found = []
            take(found, why_of(name))

    if sid == "musicbrainz":
        # MusicBrainz's own promise, and the only thing it can honestly say
        # about a page: its tag (= genre) search. The genres are the ENTITY's
        # own, read off MusicBrainz — the artist's for an artist seed, the
        # release group's for an album seed, the recording's for a track seed —
        # falling back to the artist when the entity itself states none (most
        # recordings do, which is why `recording_genres` reads empty as
        # "nothing stated HERE").
        artist_mbid = (seed["mbid"] if seed["kind"] == "artist" else "") \
            or discovery.resolve_artist_mbid(by, cfg)
        if seed["kind"] == "artist":
            genres = integrations.artist_genres(artist_mbid) if artist_mbid else []
        elif seed["kind"] == "album":
            genres = (integrations.release_group_genres(seed["mbid"])
                      if seed["mbid"] else [])
            genres = genres or (integrations.artist_genres(artist_mbid)
                                if artist_mbid else [])
        else:
            genres = (integrations.recording_genres(seed["mbid"])
                      if seed["mbid"] else [])
            genres = genres or (integrations.artist_genres(artist_mbid)
                                if artist_mbid else [])
        genres = [str(g).strip() for g in genres if str(g).strip()][:ENTITY_GENRES]
        if not genres:
            raise _Skip('MusicBrainz states no genres for "%s"' % by)
        for genre in genres:
            found = discovery.musicbrainz_tag_search(kind, genre,
                                                     limit=ENTITY_GENRE_ROWS)["rows"]
            if kind == "artists":
                # The seed is not its own neighbour: a tag search for the
                # entity's genre names it back, and it is dropped by identity
                # and by name — a page carrying no id has only the name.
                want = discovery.norm(by)
                found = [row for row in found
                         if str(row.get("mbid") or "") != artist_mbid
                         and discovery.norm(row.get("title")) != want]
            take(found, "genre: %s (MusicBrainz)" % genre)
        # Then the browse request itself: "more from this artist" is the
        # artist's own records, which is NOT a similarity claim, and its own
        # reason says exactly that. An album shelf reads the release-group
        # browse (`artist_release_groups`, the same request the artist page
        # uses for a discography); a track shelf reads the recordings
        # MusicBrainz files under that artist, because a release group is not
        # a track. Capped like every other bridge — one page of a discography,
        # not a crawl.
        if kind == "albums" and artist_mbid:
            got = integrations.artist_release_groups(artist_mbid,
                                                     limit=ENTITY_BROWSE_ROWS)
            take([discovery.mb_album_row({
                "id": group.get("id"), "title": group.get("title"),
                "artist": by,
                "first_release_date": group.get("first_release_date"),
                "primary_type": group.get("primary_type"),
                "secondary_types": group.get("secondary_types"),
            }) for group in got.get("release_groups") or [] if group.get("id")],
                "more release groups by %s (MusicBrainz)" % by)
        elif kind == "tracks" and artist_mbid:
            take(discovery.musicbrainz_artist_recordings(
                artist_mbid, limit=ENTITY_BROWSE_ROWS)["rows"],
                "more recordings by %s (MusicBrainz)" % by)
        return rows

    if sid == "deezer":
        if kind == "artists":
            take(discovery.deezer_related_artists(by, limit),
                 "sounds like %s (Deezer)" % by)
        else:
            related = [a["name"] for a in
                       discovery.deezer_related_artists(by, RELATED_FANOUT)
                       if a.get("name")]
            bridge(related, lambda name: "more from %s — sounds like %s (Deezer)"
                   % (name, by))
            # The seed's OWN artist, beside the related ones: for an album or a
            # track page the records of the artist being read are the closest
            # answer Deezer states, and the rows the library does not hold are
            # the ones it is actually missing.
            bridge([by], lambda name: "more from %s (Deezer)" % name)
        if not rows:
            raise _Skip('Deezer knows no related artists for "%s"' % by)
        return rows

    if sid == "itunes":
        # An ARTIST shelf is deliberately NOT wired here even though Apple's
        # keyless search can answer `entity=musicArtist`: a search for the
        # page's own name returns the page's artist back plus its homonyms, and
        # once the seed is dropped the row left standing is a different act
        # wearing the same name — the exact thing the album filter below
        # defends against. Apple states no artist similarity anywhere in this
        # API, so the shelf is left to the sources that do.
        if kind != "albums":
            raise _Skip("Apple's search answers album rows about a NAMED "
                        "artist — it publishes no similar-entity feed")
        # Apple has no related feed, so the honest use of its search is the
        # named artist's own records (`term=<artist>&entity=album`). Rows are
        # kept only when Apple's OWN artist field matches the name asked for,
        # so a same-named act cannot ride in on the search term.
        want = discovery.norm(by)
        found = [row for row in discovery.itunes_search_album(by, "", limit=limit + 5)
                 if discovery.norm(row.get("artist")) == want]
        if not found:
            raise _Skip('Apple\'s search matched no album by "%s"' % by)
        take(found, "more from %s (iTunes search)" % by)
        return rows

    if sid == "lastfm":
        if kind == "artists" and seed["kind"] == "artist":
            take(discovery.lastfm_similar_artists(seed["name"], limit, cfg=cfg),
                 "sounds like %s (Last.fm)" % seed["name"])
        elif kind == "tracks" and seed["kind"] == "track":
            take(discovery.lastfm_similar_tracks(seed["artist"], seed["name"],
                                                 limit, cfg=cfg),
                 "sounds like %s (Last.fm)" % seed["name"])
        else:
            raise _Skip("Last.fm's entity feeds are similar ARTISTS and similar "
                        "TRACKS — it has no similar-%s feed" % kind)
        return rows

    if sid == "listenbrainz":
        if kind != "artists":
            raise _Skip("ListenBrainz's Labs feed is similar ARTISTS — it "
                        "publishes no similar-%s feed" % kind)
        # Labs is MBID-native, and a page carrying no id is resolved by NAME
        # through MusicBrainz first (the same resolve the library-seeded shelf
        # uses). A name that resolves to nothing says so, rather than handing
        # back another artist's neighbours.
        mbid = seed["mbid"] if seed["kind"] == "artist" else ""
        mbid = mbid or discovery.resolve_artist_mbid(by, cfg)
        if not mbid:
            raise _Skip('could not resolve "%s" on MusicBrainz, and '
                        "ListenBrainz's feed is MBID-native" % by)
        take(discovery.listenbrainz_similar_artists(mbid, limit),
             "sounds like %s (ListenBrainz)" % by)
        return rows

    if sid == "spotify":
        # Spotify's entity route is NOT a similarity feed — `/related-artists`
        # and `/recommendations` are closed to apps created after 2024-11-27
        # (Spotify's own announcement) — so what it states about a page is
        # "more from the artist NAMED here", the same honest use Deezer's and
        # Apple's own-artist bridges make. An artist shelf is left to the
        # sources that DO publish a related feed (this source is not asked
        # about one: see `entity_kinds`).
        if kind == "albums":
            found = discovery.spotify_artist_albums(by, limit, cfg=cfg)
            if not found:
                raise _Skip('Spotify knows no artist called "%s"' % by)
            take(found, "more from %s (Spotify)" % by)
        elif kind == "tracks":
            found = discovery.spotify_artist_top_tracks(by, limit, cfg=cfg)
            if not found:
                raise _Skip('Spotify knows no artist called "%s"' % by)
            take(found, "more from %s (Spotify)" % by)
        else:
            raise _Skip("Spotify publishes no similar-artist feed — it states "
                        "an artist's own albums and top tracks")
        return rows

    raise _Skip(_ENTITY_NOTES.get(sid) or "no entity recommendations from this source")


def recommended_payload(cfg=None, seed="library", kind="albums", limit=20, lib=None,
                        seed_kind="", seed_mbid="", seed_name="", seed_artist=""):
    """Online recommendations, seeded by the library, by one genre, or by ONE
    entity.

    `seed=library` reads the library's own genre mix and its most-collected
    artists and asks each source for what it is good at; `seed=<genre>` asks
    the genre feeds about that one genre. Everything the library already owns
    is DROPPED — a recommendation is something to add — every row records WHY
    it was suggested, and `basis` names the seed. When nothing can answer,
    `items` is empty and `notes` says which source was skipped or empty; no
    row is ever invented.

    A `seed_kind` (`artist`/`album`/`track`) instead seeds the shelf with the
    ENTITY the page it sits on is about, from `seed_mbid` when the page's tags
    carry one and from `seed_artist`+`seed_name` when they do not (see
    `entity_seed`). An entity shelf KEEPS the rows the library owns — it is a
    statement of what this page is like, so "you already have this one" is an
    answer, and the row carries its library `path` for it — and every source
    that cannot speak about an entity is listed in `notes` with its own
    reason."""
    seed = str(seed or "library").strip() or "library"
    kind = str(kind or "").strip().lower()
    limit = max(1, min(MAX_LIMIT, int(limit or 20)))
    index = library_view(_library(cfg, lib))
    asked = _Asked()

    if str(seed_kind or "").strip():
        entity, why = entity_seed(seed_kind, seed_mbid, seed_name, seed_artist)
        basis = _entity_basis(entity)
        if why:
            # Nothing to seed from is SAID, never answered with somebody else's
            # rows: an empty shelf that explains itself beats a shelf about
            # another page.
            return {"items": [], "sources_asked": [], "basis": basis,
                    "notes": {"recommended": "skipped: %s" % why}}
        rows = []
        for spec in SOURCES:
            if kind not in spec["entity_kinds"]:
                continue
            asked.ask(spec["id"])
            if not can_run(spec, cfg):
                asked.note(spec["id"], skip_note(spec, cfg))
                continue
            try:
                found = _entity_rows(spec["id"], kind, cfg, entity, limit)
            except _Skip as e:
                asked.note(spec["id"], "skipped: %s" % e)
                continue
            except Exception as e:
                # The provider's own words, verbatim: a refusal must never read
                # as "this page has no neighbours".
                asked.note(spec["id"], "failed: %s" % e)
                continue
            for row in found:
                row["_source"] = spec["id"]
                rows.append(row)
        items = _shelf_items(rows, index, limit,
                             "similar to %s" % _entity_subject(entity),
                             drop_owned=False)
        notes = asked.notes
        if not items:
            notes["recommended"] = _nothing_note(cfg, kind, "this %s" % entity["kind"])
        return {"items": items, "sources_asked": asked.ids, "notes": notes,
                "basis": basis}

    if seed.lower() == "library":
        genres = _top_genres(index, SEED_GENRES)
        artists = _top_artists(index, SEED_ARTISTS)
        basis = ("library genres: " + (", ".join(genres) or "none tagged")
                 + " · top artists: " + (", ".join(artists) or "none"))
        if not genres and not artists:
            return {"items": [], "sources_asked": [], "basis": basis,
                    "notes": {"recommended": "skipped: the library has no "
                                             "genres or artists to seed from"}}
    else:
        genres, artists = [seed], []
        basis = "genre: %s" % seed

    rows = []
    for spec in SOURCES:
        if kind not in spec["rec_kinds"]:
            continue
        asked.ask(spec["id"])
        if not can_run(spec, cfg):
            asked.note(spec["id"], skip_note(spec, cfg))
            continue
        try:
            found = _recommend_rows(spec["id"], kind, cfg, genres, artists,
                                    limit, seed)
        except _Skip as e:
            asked.note(spec["id"], "skipped: %s" % e)
            continue
        except Exception as e:
            asked.note(spec["id"], "failed: %s" % e)
            continue
        for row in found:
            row["_source"] = spec["id"]
            rows.append(row)

    items = _shelf_items(rows, index, limit, "genre: %s" % seed, drop_owned=True)
    notes = asked.notes
    if not items:
        notes["recommended"] = _nothing_note(cfg, kind, "this seed")
    return {"items": items, "sources_asked": asked.ids, "notes": notes,
            "basis": basis}


# --------------------------------------------------------------------------- #
# charts — what each provider RANKS, for one window and one kind
# --------------------------------------------------------------------------- #
# The window as a reader says it, for the "unsupported" note.
_PERIOD_WORDS = {"all": "all-time", "year": "yearly", "month": "monthly",
                 "week": "weekly"}

# The non-source key of this payload's `notes`: the verdict on the whole
# request, printed as its own sentence (the same convention `recommended` uses).
NO_CHART_NOTE = "charts"


def _unsupported_note(spec, period):
    """Why a source cannot answer THIS period — said, never mapped to all-time.

    A provider that publishes an all-time chart only must not quietly receive
    "this week" and answer with its all-time list: the user would read a
    week's chart that is not one. So the mismatch is reported with the windows
    the source DOES publish."""
    have = ", ".join(_PERIOD_WORDS.get(p, p) for p in spec["chart_periods"])
    return ("unsupported: %s publishes no %s chart — it charts %s"
            % (spec["label"], _PERIOD_WORDS.get(period, period), have))


def _chart_skip(spec, cfg):
    """WHY a chart source is not asked at all, or "" when it is asked.

    The registry's own rule (`needs`) covers a source with an unset credential.
    RYM's does not, because it has a second route: `integrations`'
    `_genre_source_skip` states that rule once for the whole app (no cookie AND
    no archive fallback is what actually skips it), so the charts reuse it here
    instead of growing a second copy that could disagree."""
    if spec["id"] == CHART_FIRST:
        return integrations._genre_source_skip("rateyourmusic", cfg or {}) or ""
    return skip_note(spec, cfg)


def _chart_rows(sid, kind, period, limit, cfg):
    """One source's chart for one window, through `server.discovery` (or, for
    RYM, `server.integrations`' existing scrape — cookie, throttle, cache and
    archive fallback included). Answers `(rows, note)`: the note is what the
    SOURCE said about its own answer (an archived snapshot), "" otherwise.
    Every provider that FAILED raises, so the caller reports its own words; a
    provider with an empty chart returns []."""
    if sid == "deezer":
        return discovery.deezer_chart(kind, limit=limit), ""
    if sid == "itunes":
        return discovery.itunes_most_played(limit=limit, cfg=cfg), ""
    if sid == "lastfm":
        return discovery.lastfm_chart(kind, limit=limit, cfg=cfg), ""
    if sid == "listenbrainz":
        return discovery.listenbrainz_chart(kind, period, limit=limit), ""
    if sid == CHART_FIRST:
        got = integrations.rym_charts(kind=kind, period=period, limit=limit,
                                      cfg=cfg)
        name = str(got.get("chart") or "")
        rows = got["rows"]
        for row in rows:
            # RYM's OWN ranking is the row's reason: it states no play count,
            # so the chart position is the only score it has, and the chart's
            # name (from the page's own title) says which window it is.
            row["_reason"] = "chart #%s%s" % (
                row.get("rank") or "?",
                (" · %s" % name) if name else " (RateYourMusic)")
        note = ""
        snapshot = got.get("archive") or {}
        if snapshot:
            # RYM answered from the Wayback Machine: the rows are real, but a
            # snapshot can predate the window that was asked for, and that
            # belongs on the chip rather than in a tooltip nobody opens.
            when = integrations._rym_archive_when(snapshot.get("snapshot") or "")
            note = ("answered from an archived snapshot%s on web.archive.org — "
                    "the live site refused" % ((" captured %s" % when) if when else ""))
        return rows, note
    raise _Skip("no charts from this source")


def charts_payload(cfg=None, period="all", kind="tracks", source="all",
                   limit=50, lib=None):
    """What the online sources RANK for one window and one kind.

    The registry drives it: only the sources that declare this `kind` in
    `charts` are asked, in `CHART_ORDER` (RateYourMusic first for tracks), and
    only for the periods they declare in `chart_periods` — a different period is
    `unsupported:` in `notes`, never their all-time chart.

    Rows are NOT merged across sources, deliberately: a chart's RANK is its
    data, and folding two providers' rankings into one row would have to drop
    one of them. So each source's rows appear in its own order, every row
    naming its provider, its rank and its own score, and — through the shared
    row shape — whether the library already holds it (which is what turns a row
    into a link or an Add action). `sources` lists what each source can do here,
    and `notes` says, per source, exactly how it answered: nothing for one that
    answered, `skipped: …` for one that could not run, `unsupported: …` for a
    window it does not publish, `failed: <its own words>` for one that refused.
    """
    period = str(period or "all").strip().lower() or "all"
    kind = str(kind or "tracks").strip().lower() or "tracks"
    source = str(source or "all").strip().lower() or "all"
    limit = max(1, min(MAX_LIMIT, _safe_int(limit, 50)))
    specs = sorted((spec for spec in SOURCES if kind in spec["charts"]),
                   key=lambda spec: _CHART_RANK.get(spec["id"], len(CHART_ORDER)))
    base = {"period": period, "kind": kind, "source": source, "limit": limit,
            "items": [], "sources_asked": [], "notes": {},
            "sources": [{"id": spec["id"], "label": spec["label"],
                         "periods": list(spec["chart_periods"]),
                         "supports_period": period in spec["chart_periods"],
                         "needs": list(spec["needs"]),
                         "missing": missing_keys(spec, cfg),
                         "ready": can_run(spec, cfg)}
                        for spec in specs]}
    if source != "all" and source not in BY_ID:
        # An id we do not know is an empty answer that SAYS so, not an error
        # and not somebody else's rows.
        base["notes"] = {source: "unknown source"}
        return base
    if source != "all":
        wanted = BY_ID[source]
        specs = [spec for spec in specs if spec["id"] == source]
        if not specs:
            base["notes"] = {source: "cannot chart %s: %s" % (kind, wanted["note"])}
            return base

    asked = _Asked()
    index = library_view(_library(cfg, lib))
    items = []
    for spec in specs:
        sid = spec["id"]
        asked.ask(sid)
        if period not in spec["chart_periods"]:
            asked.note(sid, _unsupported_note(spec, period))
            continue
        skip = _chart_skip(spec, cfg)
        if skip:
            asked.note(sid, skip)
            continue
        try:
            found, own_note = _chart_rows(sid, kind, period, limit, cfg)
        except _Skip as e:
            asked.note(sid, "skipped: %s" % e)
            continue
        except Exception as e:
            # The provider's own words, verbatim: a refusal must never read as
            # "nothing is charting".
            asked.note(sid, "failed: %s" % e)
            continue
        if own_note:
            asked.note(sid, own_note)
        for row in found:
            row["_source"] = sid
            out = finalize_row(row, index, str(row.get("_reason")
                                               or ("chart #%s on %s"
                                                   % (row.get("rank") or "?",
                                                      spec["label"]))))
            # The provider's own rank and score, on top of the shared row shape
            # (which every Discover surface renders): "chart #3, 12.4M
            # listeners" is the row's provenance, and it does not fit in a
            # field the shared shape already spends on something else.
            out["rank"] = _safe_int(row.get("rank"), 0)
            out["score"] = row.get("popularity")
            out["score_label"] = str(row.get("popularity_label") or "") or None
            items.append(out)
    notes = asked.notes
    if not items and not notes:
        notes[NO_CHART_NOTE] = ("no chart source had anything to rank for %s"
                                % _PERIOD_WORDS.get(period, period))
    return {"period": period, "kind": kind, "source": source, "limit": limit,
            "items": items, "sources_asked": asked.ids, "notes": notes,
            "sources": base["sources"]}
