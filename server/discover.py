"""Discover — genre browsing and online recommendations for the Library app.

The three `/api/discover/*` endpoints answer from the app's LIBRARY and from
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
  dropped and the reason each row was suggested.

`SOURCES` below is the ONE place a source is described: its label, what it can
answer for which kind, whether it publishes a genre list or a recommendation
feed, and the credential it needs. The endpoints are driven from it, so a
source added there cannot be half-wired — and `sources_health` reads it so the
Sources panel probes exactly the same set.

Every call goes through `server.discovery`'s wrappers (which own the TTL cache
and the per-host throttles) or `server.integrations`' cached MusicBrainz
access; this module never opens a request of its own.

Honesty rules, which every answer here follows:

* a source that cannot run (no key) is `skipped: no <key>` in `notes`; a call
  that raised is `failed: <reason>`; a source that answered has no note;
* a source that cannot answer a request at all (Deezer files music under 22
  broad genres, TheAudioDB publishes no browse) says so in `notes` instead of
  returning rows it cannot stand behind;
* an unknown genre or source is an EMPTY list, not an error — and never a
  guessed row;
* counts only ever come from the library: a source that merely names a genre
  has no track count, so an online-only genre is all zeroes.

Row shape (identical in `genre` and `recommended`, and what both UIs render):

    kind, title, artist, year, source, source_label, cover_url, page_url,
    mbid, release_group_mbid, path, owned, in_library, tracks, reason,
    also_from

`owned` means the library already holds this exact release/artist/track —
matched by MBID first, then by normalized artist+title — and `path` is set only
then, so a row can link into the library. `in_library` is the weaker signal:
the library already holds something from this artist, so the row fills a gap
rather than being a new discovery. `also_from` lists the further sources that
named the same row, beyond its primary `source`.
"""
from __future__ import annotations

from mlo.genres import canonical, display_name, iter_names
from server import discovery

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


def _source(sid, label, note, kinds=(), *, rec_kinds=(), genres=False, needs=()):
    return {"id": sid, "label": label, "note": note, "kinds": tuple(kinds),
            "rec_kinds": tuple(rec_kinds), "genres": bool(genres),
            "needs": tuple(needs)}


# Registry order IS the preference order: the first source that named a row
# keeps it (`source`), the rest land in `also_from`, and the same order sorts
# the page. `kinds` is what the source can LIST for a genre; `rec_kinds` is
# what it can RECOMMEND (ListenBrainz has no genre filter at all, so it lists
# nothing but still recommends through its similar-artists feed and charts).
SOURCES = (
    _source("musicbrainz", "MusicBrainz",
            "Genre (tag) search over release groups, artists and recordings, "
            "and the genre vocabulary itself.",
            KINDS, rec_kinds=KINDS, genres=True),
    _source("deezer", "Deezer",
            "Its own genre charts (albums, tracks) and artists-by-genre for "
            "Deezer's 22 broad genres; related artists, an artist's albums and "
            "its top tracks.",
            KINDS, rec_kinds=KINDS, genres=True),
    _source("itunes", "iTunes",
            "Apple's genreIndex album search.",
            ("albums",), rec_kinds=("albums",)),
    _source("audiodb", "TheAudioDB",
            "States the genre and mood of a NAMED artist or album — it "
            "publishes no genre list and no genre browse (its /genres.php "
            "answers 404, verified)."),
    _source("lastfm", "Last.fm",
            "Tag charts: the most-listened albums, artists and tracks under a "
            "tag, its top tag list, and similar artists/tracks.",
            KINDS, rec_kinds=KINDS, genres=True, needs=("lastfm_api_key",)),
    _source("listenbrainz", "ListenBrainz",
            "Similar artists (Labs, keyless) and the sitewide most-listened "
            "charts. LB Radio itself needs a user token (verified 401), so it "
            "is not wired, and there is no keyless genre filter.",
            (), rec_kinds=KINDS),
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
            "Album search filtered by Spotify's own genre names.",
            ("albums",), rec_kinds=("albums",),
            needs=("spotify_client_id", "spotify_client_secret")),
)
BY_ID = {spec["id"]: spec for spec in SOURCES}
SOURCE_LABELS = {spec["id"]: spec["label"] for spec in SOURCES}
_ORDER = {spec["id"]: i for i, spec in enumerate(SOURCES)}


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
        "sources": [{
            "id": spec["id"], "label": spec["label"], "note": spec["note"],
            "genres": spec["genres"], "kinds": list(spec["kinds"]),
            "rec_kinds": list(spec["rec_kinds"]), "needs": list(spec["needs"]),
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
    order keeps the row and is its `source`/`source_label`; every further
    source is listed in `also_from`, and a field the primary lacks (a cover, a
    year, an MBID) is filled from the duplicate rather than lost."""
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
        "reason": reason,
        "also_from": list(row.get("_also") or []),
    }


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
# recommended — online suggestions seeded by the library or by one genre
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


def recommended_payload(cfg=None, seed="library", kind="albums", limit=20, lib=None):
    """Online recommendations, seeded by the library or by one genre.

    `seed=library` reads the library's own genre mix and its most-collected
    artists and asks each source for what it is good at; `seed=<genre>` asks
    the genre feeds about that one genre. Everything the library already owns
    is DROPPED — a recommendation is something to add — every row records WHY
    it was suggested, and `basis` names the seed. When nothing can answer,
    `items` is empty and `notes` says which source was skipped or empty; no
    row is ever invented."""
    seed = str(seed or "library").strip() or "library"
    kind = str(kind or "").strip().lower()
    limit = max(1, min(MAX_LIMIT, int(limit or 20)))
    index = library_view(_library(cfg, lib))
    asked = _Asked()

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

    items = []
    for row in sort_rows(merge_rows(rows)):
        out = finalize_row(row, index, row.get("_reason") or "genre: %s" % seed)
        if out["owned"]:
            continue        # the user already has it: not a recommendation
        items.append(out)
        if len(items) >= limit:
            break

    notes = asked.notes
    if not items:
        answers = [spec for spec in SOURCES if kind in spec["rec_kinds"]]
        if not any(can_run(spec, cfg) for spec in answers):
            notes["recommended"] = ("skipped: no recommendation source is "
                                    "configured for %s" % kind)
        else:
            notes["recommended"] = ("no recommendation source had anything to "
                                    "suggest for this seed")
    return {"items": items, "sources_asked": asked.ids, "notes": notes,
            "basis": basis}
