"""Home-page payload: library highlights.

Everything is built from the library itself — stats, recent additions, best
grades, the user's own rated releases, favorites, a random rediscovery shelf,
most-collected artists and albums failing their checks — and the ratings store
for the one shelf that is the caller's own verdict rather than the library's.
Best-effort and TTL-cached: a failing sub-source degrades to a missing shelf
rather than failing the Home page.
"""
import os
import random
import threading
import time

from mlo.grader import printed_pct
from server import job_locks

_lock = threading.Lock()
_cache = {"t": 0.0, "key": None, "data": None}
_TTL = 900.0
# Keys with a background Home rebuild in flight (`_start_home_refresh`), so
# ten pages asking at once still cost ONE walk.
_refreshing: set = set()


def _artist_of(alb, fallback=""):
    meta = alb.get("meta") or {}
    return (str(meta.get("ALBUMARTIST") or "").strip()
            or str(alb.get("album_artist") or "").strip()
            or str(meta.get("ARTIST") or "").strip() or fallback)


def _owned_row(alb, fallback_artist="", reason="", owned=True):
    """One Home shelf row: the LIBRARY's own album row, plus the shelf's reason.

    The shelves draw the shared album card (web/src/components/AlbumCard) — the
    same card the Library grid draws — so the row has to BE the library's row:
    its tracks (the format chip and the play button), `meta` (release country,
    original year, dynamic range), media, grade and audit all come off it, as
    does the framework album's marker. A reduced row would be a second album
    shape for one card to understand, and a Home card that showed less than the
    same album shows in the Library.
    """
    meta = alb.get("meta") or {}
    row = dict(alb)
    row["reason"] = reason
    row["owned"] = owned
    # The card's own artist field (the library grid builds the same one when it
    # flattens the payload): ALBUMARTIST → album_artist → ARTIST.
    row["artist"] = _artist_of(alb, fallback_artist)
    row["mbid"] = (str(meta.get("MUSICBRAINZ_ALBUMID") or "").strip()
                   or str(meta.get("MUSICBRAINZ_RELEASEGROUPID") or "").strip() or None)
    row["mb_kind"] = "rg"
    return row


def _added_at(alb):
    """A row's addition time — the folder's own mtime, the one fact that says
    "just added" for a framework album with no file to read it from."""
    try:
        return os.path.getmtime(alb.get("path") or "")
    except OSError:
        return 0.0


def _recent(albums, limit):
    ordered = sorted(albums, key=_added_at, reverse=True)
    return [_owned_row(a, reason="Recently added") for a in ordered[:limit]]


def _pending(albums, limit):
    """The albums that are added but not downloaded yet, newest first.

    This is the one place a user can see EVERYTHING still waiting: the shelf
    above lists them where they would otherwise be (recent, their artist's
    shelf), but a skeleton that only rides along with other shelves is a
    skeleton a reader has to hunt for.
    """
    waiting = [a for a in albums if a.get("pending")]
    waiting.sort(key=_added_at, reverse=True)
    return [_owned_row(a, reason="Waiting for its audio") for a in waiting[:limit]]


def _top_rated(albums, limit):
    rated = [a for a in albums if a.get("grade_pct") is not None and (a.get("total_checks") or 0) >= 5]
    rated.sort(key=lambda a: (-(a.get("grade_pct") or 0), not a.get("pass")))
    return [_owned_row(a, reason="Best graded") for a in rated[:limit]]


def _favorites(lib, limit, user=""):
    """The signed-in user's favourite ALBUMS.

    Scoped, like every other reader of that table: the sidebar's Favourites
    page shows this user's albums, so a Home shelf built from the default
    scope would be a different person's list beside it — and a cross-user read
    the per-user work exists to prevent.
    """
    try:
        from server import playlists as pl
        favs = (pl.list_favorites(user) or {}).get("albums", [])
    except Exception:
        favs = []
    if not favs:
        return []
    by_path = {os.path.normcase(os.path.normpath(a.get("path") or "")): a
               for a in (alb for ar in lib.get("artists", []) for alb in ar.get("albums", []))}
    out = []
    for p in favs[:limit]:
        alb = by_path.get(os.path.normcase(os.path.normpath(str(p))))
        if alb:
            out.append(_owned_row(alb, reason="Favorite"))
        else:
            name = os.path.basename(str(p).replace("\\", "/"))
            # The library no longer holds this path (the folder was removed or
            # moved): there is no album row to read tags, tracks or a grade
            # from. The row keeps the path the user favourited — and `owned`
            # is what tells the card not to draw a grade, a play button or a
            # link the library cannot answer.
            out.append({
                "path": str(p).replace("\\", "/"),
                "owned": False,
                "artist": "",
                "reason": "Favorite",
                "mbid": None,
                "mb_kind": "rg",
                "tracks": [],
                "cover_file": None,
                "meta": {"ALBUM": name},
            })
    return out


def _top_artists(artists, limit):
    """Most-collected artists, with a representative cover for the card.

    `artist` is the library row's `display_name` (server.library builds it:
    the tag-derived name, or the folder name without its MusicBrainz id) rather
    than the folder's own basename, which is how Home came to draw
    "Radiohead [a74b1b7f-71a5-4011-9441-d0b5e4122711]".

    `has_image` answers for `GET /api/artist/image`, which 404s on a folder
    holding no artist picture: the shelf draws that picture first and the
    cover behind it, and asking for the URL anyway paints the broken-image
    glyph before the fallback replaces it. Read from the directory itself for
    the handful of rows this returns — never a walk, and never a provider.
    """
    rows = []
    for ar in artists:
        name = str(ar.get("display_name") or ar.get("name") or "").strip()
        albs = ar.get("albums") or []
        if not name or not albs:
            continue
        agg = ar.get("aggregate") or {}
        first = sorted(albs, key=lambda a: str(a.get("path") or "").lower())[0]
        row = {
            "path": str(ar.get("path") or "").replace("\\", "/"),
            "artist": name,
            # The artist's MusicBrainz disambiguation comment, straight off the
            # library row the shelf is built from — the same field the artist
            # page and the Library's artist view carry, so every surface that
            # names this artist draws the same parentheses. None when no album
            # states one; no request of its own.
            "disambiguation": str(ar.get("disambiguation") or "").strip() or None,
            "album_count": len(albs),
            "track_count": agg.get("track_count") or 0,
            # The ALBUM rollup's percentage (what the card prints under the
            # name). The artist folder's own verdict is `grade` below — two
            # different questions the shelf draws as two different things.
            "grade_pct": agg.get("grade_pct"),
            "cover_path": first.get("path") or "",
            "cover": first.get("cover_file"),
        }
        # The artist's OWN grade rides along untouched: the library row the
        # shelf is built from already carries it (`mlo.grader.grade_artist`,
        # see server.library), so the dot beside a name on Home is the dot the
        # artist page draws — no second grading pass, no second rule. Rows
        # without one (a caller handing in its own dicts) simply have no dot.
        if "grade" in ar:
            row["grade"] = ar["grade"]
        rows.append(row)
    rows.sort(key=lambda r: (-r["album_count"], r["artist"].lower()))
    rows = rows[:limit]
    # Imported here, at the point of use: only this shelf asks the filesystem
    # anything, and an artistdata that cannot answer must not cost the page.
    from mlo import artistdata
    for r in rows:
        try:
            r["has_image"] = artistdata.has_image(r["path"])
        except Exception:
            r["has_image"] = False
    return rows


def _rated(albums, user, limit):
    """The user's own rated RELEASES, best first.

    The verdict lives in the ratings store, and the store's own rule is what
    keeps this shelf cheap: a rating is refused for any path no page draws
    (`server.ratings.target_missing`), so every stored row has a library album
    behind it and both halves of the join are already in hand — the store's map
    and the album rows. One table read, no walk, no provider.

    UNRATED releases are deliberately not a shelf: "no star yet" is not a
    reason a row is here, the set has no order to give it (it is most of the
    library), and the Rediscover shelf already draws a random slice of owned
    albums for exactly that "you own it and have not looked at it" case.

    `rating` rides on the row in HALF-STARS — the store's own unit, the one
    `GET /api/ratings?scope=album` answers in and web/src/lib/ratings.ts turns
    into the stars a reader sees — so the shelf's order and the stars on its
    cards come from the same number.
    """
    if not albums:
        return []  # nothing to rate: the store read below is not even worth it
    try:
        from server import ratings as store
        got = store.map_for(user=user, scope="album")
    except Exception:
        return []  # an unreadable store loses the shelf, never the whole page
    if not got:
        return []
    # Keyed the way `_favorites` reads the same payload: the library spells a
    # path with forward slashes, a store row is whatever its writer stored.
    by_path = {os.path.normcase(os.path.normpath(p)): int(v)
               for p, v in got.items()}
    rated = []
    for alb in albums:
        half = by_path.get(os.path.normcase(os.path.normpath(str(alb.get("path") or ""))))
        if not half:
            continue  # an unrated album, or a row the caller has no verdict on
        row = _owned_row(alb)
        row["rating"] = half
        rated.append(row)
    # Highest first, then by artist and title so two albums of one value never
    # swap places between builds (the shelf is cached, not re-sorted per view).
    rated.sort(key=lambda r: (-r["rating"], r["artist"].lower(),
                              str((r.get("meta") or {}).get("ALBUM") or "").lower()))
    return rated[:limit]


def _needs_attention(albums, limit):
    """Owned albums that fail at least one check — lowest grade first.

    An album an import is on is left out through the ONE predicate
    (`server.imports.importing_album`, live claim or surviving disk marker): a
    shelf built from a payload that predates the import would otherwise list a
    half-written album as needing attention. Imported lazily for the same
    reason `grade_warning` does — this module must not join the import
    service's import graph at load time.
    """
    try:
        from server import imports
        importing = imports.importing_album
    except Exception:
        def importing(_path):
            return False
    bad = [a for a in albums
           if (a.get("total_checks") or 0) > 0 and not a.get("pass")
           and not importing(a.get("path"))]
    bad.sort(key=lambda a: (a.get("grade_pct") if a.get("grade_pct") is not None else 0.0,
                            -(a.get("total_checks") or 0)))
    return [_owned_row(a, reason="Needs attention") for a in bad[:limit]]


# The most findings the banner names; everything past it is the `more` count a
# reader clicks through to the Library's own Failing filter.
_GRADE_WARNING_MAX = 12
# The grader records an album-wide failure against one of these INSTEAD of a
# file name (mlo.grader's add_issue): "album" for a check that belongs to the
# folder (its cover, its .log), "album-wide" for one that spans every track in
# it (MEDIA, the album tags).
_ALBUM_WHERE = ("album", "album-wide")


def grade_warning(lib):
    """Whether the library passes its grading checks, and — when it does not —
    what fails, specifically: the object both pages' warning strip is drawn
    from (`GET /api/grades/summary`, and `grade_warning` in the Home payload).

    Read off the library payload the pages already hold, and totalled the way
    the Home header totals it: every album's `pass_count` over its
    `total_checks`. `ok` is therefore the grader's own per-album rule (failed
    checks == 0, server.library.build_album) applied to the whole library, so
    the strip can never contradict the percentage printed beside it.

    Three albums are not a finding, and every one of them would be the loudest
    row on the page:
    - a PENDING framework album (the release the user added whose audio has
      not arrived): nothing was graded, because there was nothing to grade,
      and its row's failed check is the empty-folder placeholder — listing it
      would report a pending album as a broken album;
    - an album with NO checks (`total_checks` 0): it PASSES by the rule above
      (0 == 0), which is how an album whose checks are all switched off stops
      disagreeing with the Grade script;
    - an album a LIVE JOB holds (`job_locks.busy`): a chain writes an album
      across its steps, so an album mid-run is deliberately half-written — the
      tag the Auto tagging step is about to write is missing right up until
      that step runs — and a strip that reported it would be describing the
      process, not the library. The album's own row already says "Script run"
      for the same reason; when the claim goes the album is a finding again.
      The albums an import is on right now —
      `server.imports.importing_album`, the ONE predicate (a live import claim,
      or a surviving disk marker after a restart), so both surfaces agree about
      what "being imported" is — are counted in `albums_importing`, which the
      strip prints in one clause: a reader whose failing album left the list
      the moment its chain started is told where it went instead of wondering
      which album the strip lost. It counts the
      FINDINGS left out on that account (graded, failing, mid-import), not
      every claim in the registry — an album that was never a finding has none
      to lose.

    `albums_failing`, `tracks_failing` and the `items` (with their `more`) are
    the findings that are LISTED, and a busy or mid-import album is in none of
    them — `albums_importing` counting the ones the import held back. The
    TOTALS are not: `pass_count`/`total_checks`/`grade_pct` keep counting the
    library's checks exactly as the Home header prints them, busy album
    included — a strip that quietly dropped a running album's checks from the
    sum would contradict the percentage printed beside it.

    `grade_pct` is the one number here the pair of pages prints as a claim, so
    it is kept honest: rounded to one decimal a library failing one check in
    ten thousand read `100.0` beside the album that failed it, and it now
    reads `99.9` — exactly 100 is printed only when `pass_count` equals
    `total_checks`, i.e. by a library with no failed check at all.

    The owner's rule for what a finding is: ONE failing track in an album is
    shown AS THAT TRACK — the album is only the frame around it, and the file
    is the thing to open — while two or more are shown AS THE ALBUM, which
    carries how many tracks fail and the union of their codes, because a dozen
    rows of one album say less than its name. A failure the grader recorded
    against the album itself (no file to name) always makes an album row and
    carries the grader's own sentence as `reason`.
    """
    # The ONE notion of "this album is being imported": server.imports reads
    # `job_locks.holder` and takes the three claim kinds the import queue's own
    # In progress section reads, so the strip and the queue cannot disagree
    # about it. Imported here rather than at module level: `server.imports`
    # drags in the whole chain service, and this module is imported from inside
    # routes that must not join that cycle.
    from server import imports
    albums = [alb for ar in (lib.get("artists") or [])
              for alb in (ar.get("albums") or [])]
    pass_count = sum(int(a.get("pass_count") or 0) for a in albums)
    total_checks = sum(int(a.get("total_checks") or 0) for a in albums)
    items = []
    tracks_failing = 0
    albums_importing = 0
    for alb in albums:
        # See the docstring: neither of these was graded, so neither is wrong.
        if alb.get("pending") or not (alb.get("total_checks") or 0) or alb.get("pass"):
            continue
        # An album an import is on is mid-write by definition — its chain is
        # filling the very tags the grader has just read as missing — so it is
        # not a finding, and it is COUNTED, because a strip that only dropped it
        # would look like it lost an album. The question is asked through the
        # ONE predicate (`server.imports.importing_album`), so a live claim and
        # a surviving disk marker (an import killed mid-way, the app restarted)
        # answer the same way, and the strip and the queue cannot disagree.
        if imports.importing_album(alb.get("path")):
            albums_importing += 1
            continue
        # …and any other live job's album is mid-write rather than wrong. Asked
        # absolutely (job_locks.busy, not holder): the answer is a fact about
        # the album, not about whatever this request happens to be running
        # inside.
        if job_locks.busy(alb.get("path")):
            continue
        bad = [tr for tr in (alb.get("tracks") or []) if tr.get("issues")]
        issues = alb.get("issues") or {}
        sentences = [s for s, where in issues.items()
                     if any(w in _ALBUM_WHERE for w in (where or ()))]
        if not bad and not sentences:
            # A folder failure with no file against it and no album-wide
            # sentence either: the grader keys those by the folder itself (an
            # album folder that holds no audio), and its sentence is the only
            # thing there is to say.
            sentences = list(issues)
        tracks_failing += len(bad)
        meta = alb.get("meta") or {}
        album_path = str(alb.get("path") or "").replace("\\", "/")
        row = {
            "album_path": album_path,
            "artist": _artist_of(alb),
            "album": (str(meta.get("ALBUM") or "").strip()
                      or album_path.rsplit("/", 1)[-1]),
            "grade_pct": alb.get("grade_pct"),
        }
        if len(bad) == 1 and not sentences:
            tr = bad[0]
            items.append(dict(row,
                              kind="track",
                              track_path=str(tr.get("path") or ""),
                              # The tag the player shows, the file name standing
                              # in when it is empty — a row named "" is a track
                              # the reader cannot match to the album's table.
                              title=(str((tr.get("tags") or {}).get("TITLE") or "").strip()
                                     or str(tr.get("file") or "")),
                              codes=sorted(set(tr.get("issues") or ()))))
            continue
        items.append(dict(row,
                          kind="album",
                          failing_tracks=len(bad),
                          codes=sorted({c for tr in bad
                                        for c in (tr.get("issues") or ())}),
                          # EVERY album-wide sentence, not just the first: the
                          # row prints one ("— Missing MEDIA") and the tooltip
                          # next to it names the rest, so a check the strip
                          # cannot print — "Album description missing — fetch
                          # one on the album page", the owner's own ask that the
                          # library say when a description does not exist — is
                          # still spelled out one hover away instead of being
                          # dropped because another sentence came first.
                          **({"reason": sentences[0],
                              "reasons": sentences} if sentences else {})))
    # Worst first: the lowest grade, then the album with the most failing
    # tracks (a single-track row counts as the one track it is).
    items.sort(key=lambda it: (it["grade_pct"] if it["grade_pct"] is not None else 0.0,
                               -(it.get("failing_tracks") or 1)))
    more = max(0, len(items) - _GRADE_WARNING_MAX)
    # Printed BESIDE the findings above, so it obeys the one rule for a printed
    # percentage (mlo.grader.printed_pct): 100 belongs to a library with no
    # failed check at all, and a strip that names a failing album while the
    # line next to it reads "100% of checks pass" contradicts itself in one
    # sentence.
    grade_pct = printed_pct(pass_count, total_checks)
    return {
        "ok": not items,
        "pass_count": pass_count,
        "total_checks": total_checks,
        "grade_pct": grade_pct,
        "albums_failing": len(items),
        # The failing albums an import is holding right now — not listed, and
        # the strip says so in one clause rather than looking short an album.
        "albums_importing": albums_importing,
        "tracks_failing": tracks_failing,
        "items": items[:_GRADE_WARNING_MAX],
        "more": more,
    }


# --------------------------------------------------------------------------- #
# Podcasts
# --------------------------------------------------------------------------- #
# A podcast is not an album by a band: MusicBrainz models it as a SERIES of
# type Podcast whose episodes are release groups linked `part of` it (there is
# no Podcast release-group type — see mlo.naming.DERIVED_RELEASE_TYPES), and
# the app records that series on each episode's files. server.library reads the
# PODCASTSERIES / PODCASTSERIESMBID / PODCASTEPISODE tags into every album
# row's `podcast` block, so everything below is built from a SCAN — the shelf
# and the series page never ask MusicBrainz once.
#
# Episodes are ordered newest first by the episode's own date and then by
# MusicBrainz's episode number: a show that published twice in a day is in the
# right order, and an episode whose date MusicBrainz does not state is placed
# by its number instead of dropping out. The date is the album's DATE tag (an
# episode IS a release, so its own date is the right one), falling back to
# ORIGINALDATE.
def _episode_order(al):
    """(date, episode number) of one episode row — the shelf's own sort key."""
    meta = al.get("meta") or {}
    pod = al.get("podcast") or {}
    try:
        number = int(pod.get("episode") or 0)
    except (TypeError, ValueError):
        number = 0
    return (str(meta.get("DATE") or meta.get("ORIGINALDATE") or ""), number)


def _podcast_groups(albums):
    """The library's episodes grouped by series, newest series first.

    Returns ``[{"series", "series_mbid", "episodes"}, ...]`` with each series'
    episodes newest first. A row whose `podcast` block names no series is not
    an episode — a Broadcast MusicBrainz links to no series — and is left out
    entirely rather than collected under an empty name.
    """
    groups = {}
    for al in albums or []:
        pod = al.get("podcast") or {}
        series = str(pod.get("series") or "").strip()
        if not series:
            continue
        g = groups.setdefault(series, {"series": series,
                                       "series_mbid": pod.get("series_mbid") or None,
                                       "episodes": []})
        # A series re-tagged since (or an episode an older run wrote a bare
        # name for) must not lose its id: the first row that states one wins.
        if not g["series_mbid"] and pod.get("series_mbid"):
            g["series_mbid"] = pod["series_mbid"]
        g["episodes"].append(al)
    out = []
    for g in groups.values():
        g["episodes"].sort(key=_episode_order, reverse=True)
        out.append(g)
    out.sort(key=lambda g: _episode_order(g["episodes"][0]), reverse=True)
    return out


def _podcasts(albums, limit=12):
    """Home's Podcasts shelf: ONE row per series, carrying its newest episode.

    The row IS the newest episode's library row (`_owned_row`), so the shared
    album card draws it, with the three facts a SERIES card needs added: the
    series name (the caption — an episode title alone does not say which show
    it belongs to), its MusicBrainz id, and how many of its episodes the
    library holds. Empty when the library has no podcast at all, which is what
    keeps the shelf off Home for everyone else.
    """
    rows = []
    for g in _podcast_groups(albums)[:limit]:
        row = _owned_row(g["episodes"][0])
        row["podcast_series"] = g["series"]
        row["podcast_series_mbid"] = g["series_mbid"]
        row["podcast_episode_count"] = len(g["episodes"])
        rows.append(row)
    return rows


def podcast_series_payload(cfg, series):
    """ONE podcast series and every episode the library holds, newest first.

    The series page's payload: `series` is the name a shelf row links by (the
    name a reader sees, MusicBrainz's disambiguation included when it stated
    one, so two same-named shows are two pages), each episode is a library
    album row the shared card draws, and None means the library holds no
    episode of it — a 404, not an empty page.
    """
    from server import library as lib_mod

    series = str(series or "").strip()
    if not series:
        return None
    episodes, artist_of = [], {}
    for ar in lib_mod.build_library(cfg).get("artists", []):
        name = str(ar.get("display_name") or ar.get("name") or "")
        for al in ar.get("albums", []):
            if al.get("podcast"):
                episodes.append(al)
                artist_of[al.get("path")] = name
    for g in _podcast_groups(episodes):
        if g["series"] != series:
            continue
        return {
            "series": g["series"],
            "series_mbid": g["series_mbid"],
            "episode_count": len(g["episodes"]),
            "episodes": [_owned_row(e, fallback_artist=artist_of.get(e.get("path"), ""))
                         for e in g["episodes"]],
        }
    return None


# --------------------------------------------------------------------------- #
# The listening shelves (2026-10-17)
#
# Home is about the MUSIC, not the maintenance: genres, the user's own stars,
# moods and what actually gets played. Every shelf is a join of data the app
# already holds — the library tree, the ratings store, the play store — and a
# shelf with nothing real behind it is EMPTY (the page hides it), never padded
# with a low-confidence row to fill the grid.
# --------------------------------------------------------------------------- #
def _track_index(lib):
    """{track path: (album row, track row, artist name)} — one pass."""
    out = {}
    for ar in lib.get("artists") or []:
        artist = str(ar.get("display_name") or ar.get("name") or "")
        for alb in ar.get("albums") or []:
            for tr in alb.get("tracks") or []:
                out[str(tr.get("path") or "")] = (alb, tr, artist)
    return out


def _track_row(album, track, artist="", *, plays=0, rating=None):
    """One track shelf row: what a listener clicks, not a library table row."""
    tags = track.get("tags") or {}
    meta = album.get("meta") or {}
    return {
        "kind": "track",
        "path": str(track.get("path") or "").replace("\\", "/"),
        "file": track.get("file"),
        "title": str(tags.get("TITLE") or track.get("file") or ""),
        "artist": str(tags.get("ARTIST") or artist or ""),
        "album": str(meta.get("ALBUM") or ""),
        "album_path": str(album.get("path") or "").replace("\\", "/"),
        "cover_file": track.get("cover_file") or None,
        "album_cover": album.get("cover_file") or None,
        "duration": (track.get("tech") or {}).get("length"),
        "plays": int(plays or 0),
        "rating": rating,
        "in_library": True,
    }


def _album_plays(albums_by_path, counts):
    """{album path: plays} by summing the plays of the tracks the library
    holds in it. `plays` already stores an `album` column; this join is the
    fallback that also serves a payload built without the store's grouping."""
    out = {}
    for path, alb in albums_by_path.items():
        total = 0
        for tr in alb.get("tracks") or []:
            total += int(counts.get(str(tr.get("path") or ""), 0))
        out[path] = total
    return out


def _listening_shelves(lib, user, limit):
    """The genres, moods, stars and play-count shelves Home draws."""
    from collections import Counter

    from mlo.genres import display_name, split_stored

    try:
        from server import plays as plays_store
        path_plays = plays_store.counts("path", user=user)
        playlist_plays = plays_store.counts("playlist", user=user)
    except Exception:
        path_plays, playlist_plays = {}, {}
    try:
        from server import ratings as store
        track_stars = store.map_for(user=user, scope="track")
        album_stars = store.map_for(user=user, scope="album")
        artist_stars = store.map_for(user=user, scope="artist")
    except Exception:
        track_stars, album_stars, artist_stars = {}, {}, {}

    albums = [alb for ar in (lib.get("artists") or []) for alb in (ar.get("albums") or [])]
    albums_by_path = {str(a.get("path") or "").replace("\\", "/"): a for a in albums}
    index = _track_index(lib)

    # ---- genres ----------------------------------------------------------
    genre_tracks, genre_plays, genre_albums = Counter(), Counter(), {}
    mood_tracks, mood_plays, mood_albums = Counter(), Counter(), {}
    for path, (alb, tr, _artist) in index.items():
        tags = tr.get("tags") or {}
        plays = int(path_plays.get(path, 0))
        names = []
        raw = tags.get("GENRE")
        for value in (raw if isinstance(raw, (list, tuple, set)) else [raw] if raw else []):
            names.extend(split_stored(str(value)))
        seen = {display_name(n) for n in names if n}
        for name in seen:
            genre_tracks[name] += 1
            genre_plays[name] += plays
            genre_albums.setdefault(name, set()).add(str(alb.get("path") or "").replace("\\", "/"))
        mood = str(tags.get("MOOD") or "").strip()
        if mood:
            # MOOD is stored as the classifier's own lowercase word; a lane
            # TITLE is prose ("Melancholic"), while the tag is unchanged.
            mood = mood[:1].upper() + mood[1:]
            mood_tracks[mood] += 1
            mood_plays[mood] += plays
            mood_albums.setdefault(mood, set()).add(str(alb.get("path") or "").replace("\\", "/"))

    def lanes(counter, plays_counter, album_map):
        out = []
        for name, _count in counter.most_common(12):
            paths = [p for p in album_map.get(name, ()) if p in albums_by_path]
            if not paths:
                continue
            paths.sort(key=lambda p: (-plays_counter.get(name, 0),
                                      str((albums_by_path[p].get("meta") or {}).get("ALBUM") or "").lower()))
            out.append({
                "name": name,
                "tracks": int(counter[name]),
                "plays": int(plays_counter.get(name, 0)),
                "albums": [_owned_row(albums_by_path[p], reason=name)
                           for p in paths[:limit]],
            })
        return out[:6]

    genres = lanes(genre_tracks, genre_plays, genre_albums)
    moods = lanes(mood_tracks, mood_plays, mood_albums)

    # ---- play counts -----------------------------------------------------
    played = [(p, int(n)) for p, n in path_plays.items() if p in index and n]
    played.sort(key=lambda pair: (-pair[1], pair[0]))
    top_tracks = []
    for path, n in played[:limit]:
        alb, tr, artist = index[path]
        top_tracks.append(_track_row(alb, tr, artist, plays=n,
                                     rating=track_stars.get(path)))

    album_total = _album_plays(albums_by_path, path_plays)
    top_album_rows = sorted(
        ((p, n) for p, n in album_total.items() if n and p in albums_by_path),
        key=lambda pair: (-pair[1], pair[0]),
    )[:limit]
    top_albums = []
    for path, n in top_album_rows:
        row = _owned_row(albums_by_path[path], reason="Most played")
        row["plays"] = n
        row["rating"] = album_stars.get(path)
        row["reason"] = f"{n} play{'s' if n != 1 else ''}"
        top_albums.append(row)

    # Artist play counts fold their albums' plays through the library's own
    # artist rows — the same fold the artist page's stats do.
    artist_total = Counter()
    artist_row = {}
    for ar in lib.get("artists") or []:
        name = str(ar.get("display_name") or ar.get("name") or "")
        if not name:
            continue
        total = sum(album_total.get(str(alb.get("path") or "").replace("\\", "/"), 0)
                    for alb in ar.get("albums") or [])
        if total:
            artist_total[name] += total
            artist_row.setdefault(name, ar)
    top_artists = []
    for artist, plays in artist_total.most_common(limit):
        ar = artist_row.get(artist) or {}
        albs = ar.get("albums") or []
        first = sorted(albs, key=lambda a: str(a.get("path") or "").lower())[0] if albs else {}
        top_artists.append({
            "kind": "artist",
            "path": str(ar.get("path") or "").replace("\\", "/"),
            "artist": artist,
            "disambiguation": str(ar.get("disambiguation") or "").strip() or None,
            "plays": int(plays),
            "rating": artist_stars.get(str(ar.get("path") or "")),
            "cover_path": first.get("path") or "",
            "cover": first.get("cover_file"),
        })

    top_playlists = []
    if playlist_plays:
        try:
            from server import playlists as pl
            known = {str(p.get("id")): p for p in (pl.list_playlists(user) or [])}
        except Exception:
            known = {}
        for pid, n in sorted(playlist_plays.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]:
            row = known.get(str(pid))
            if not row or not n:
                continue
            top_playlists.append({
                "kind": "playlist",
                "id": row.get("id"),
                "name": row.get("name"),
                "kind_of": row.get("kind"),
                "track_count": row.get("track_count"),
                "plays": int(n),
                "path": f"/playlist/{row.get('id')}",
            })

    # ---- the caller's own stars -----------------------------------------
    rated_albums = []
    for path, stars in album_stars.items():
        alb = albums_by_path.get(path)
        if not alb or not stars:
            continue
        row = _owned_row(alb, reason="Rated")
        row["rating"] = stars
        row["plays"] = album_total.get(path, 0)
        rated_albums.append(row)
    rated_albums.sort(key=lambda r: (-r["rating"], -(r.get("plays") or 0),
                                     str(r.get("artist") or "").lower()))
    rated_tracks = []
    for path, stars in track_stars.items():
        hit = index.get(path)
        if not hit or not stars:
            continue
        alb, tr, artist = hit
        rated_tracks.append(_track_row(alb, tr, artist, plays=path_plays.get(path, 0),
                                       rating=stars))
    rated_tracks.sort(key=lambda r: (-r["rating"], -(r.get("plays") or 0), r["title"].lower()))
    rated_artists = []
    for ar in lib.get("artists") or []:
        path = str(ar.get("path") or "")
        stars = artist_stars.get(path)
        name = str(ar.get("display_name") or ar.get("name") or "")
        if not stars or not name:
            continue
        albs = ar.get("albums") or []
        first = sorted(albs, key=lambda a: str(a.get("path") or "").lower())[0] if albs else {}
        rated_artists.append({
            "kind": "artist", "path": path.replace("\\", "/"), "artist": name,
            "rating": stars, "plays": int(artist_total.get(name, 0)),
            "cover_path": first.get("path") or "", "cover": first.get("cover_file"),
        })
    rated_artists.sort(key=lambda r: (-r["rating"], -(r.get("plays") or 0), r["artist"].lower()))

    return {
        "genres": genres,
        "moods": moods,
        "top_tracks": top_tracks,
        "top_albums": top_albums,
        "top_artists": top_artists,
        "top_playlists": top_playlists,
        "rated_albums": rated_albums[:limit],
        "rated_tracks": rated_tracks[:limit],
        "rated_artists": rated_artists[:limit],
    }


def _rediscover(albums, featured, limit):
    """A random slice of owned albums the other shelves did not name.

    "Rediscover" means "you own it and forgot it": albums whose audio has not
    arrived are left out (they are not listening material yet), and so is
    anything already on another shelf.
    """
    pool = [a for a in albums
            if not a.get("pending")
            and os.path.normcase(os.path.normpath(a.get("path") or "")) not in featured]
    random.shuffle(pool)
    return [_owned_row(a, reason="Rediscover") for a in pool[:limit]]


def build_home(cfg, user=""):
    """Full Home payload for the given config and user (TTL-cached, SWR).

    The cache key carries the user: the payload holds that person's favourites
    and their playlist count, so a shared entry would serve the first caller's
    rows to everyone else for the TTL.

    A payload that is merely OLD is served as it stands and rebuilt behind the
    request (`_refresh_home_async`, single flight), exactly like the library
    tree (R338): this build walks the library and every shelf, and on the
    owner's install the first one after a restart measured **187.5 s** — with
    the Home page's "Loading your library…" in front of it, every restart and
    every Refresh (which drops the memo). No request waits for that again; the
    only blocking build left is a cold process's first ask, and
    `warm_home` (the lifespan's `home-warm` thread) works that one at startup.
    """
    user = str(user or "")
    folder = str(cfg.get("music_folder") or "")
    recent_count = int(cfg.get("home_recent_count", 12) or 12)
    cache_key = (folder, recent_count, user)
    now = time.time()
    with _lock:
        hit = _cache
        if hit["data"] is not None and hit["key"] == cache_key:
            if now - hit["t"] < _TTL:
                return hit["data"]
            _start_home_refresh(cache_key, cfg, user)
            return hit["data"]
    data = _build_home_payload(cfg, user, recent_count)
    with _lock:
        if _cache["key"] == cache_key or _cache["data"] is None:
            _cache.update({"t": now, "key": cache_key, "data": data})
    return data


def _start_home_refresh(cache_key, cfg, user) -> None:
    """Rebuild ONE key's Home payload off the request path (single flight).

    Called with `_lock` held; the thread it starts does the waiting, so the
    caller returns the stale payload immediately. A refresh that fails leaves
    the served payload in place and the next request tries again.
    """
    if cache_key in _refreshing:
        return
    _refreshing.add(cache_key)

    def run():
        try:
            data = _build_home_payload(cfg, str(user or ""),
                                       int(cfg.get("home_recent_count", 12) or 12))
        except BaseException as e:
            print(f"[mlo] home background refresh failed: {e}")
            data = None
        with _lock:
            if data is not None:
                _cache.update({"t": time.time(), "key": cache_key, "data": data})
            _refreshing.discard(cache_key)

    threading.Thread(target=run, daemon=True, name="home-refresh").start()


def warm_home(cfg) -> None:
    """Build the Home payload for every claimed user, before anyone asks.

    One thread, at startup, beside the library warm-up: a process that has
    just started has no Home memo, so the first visit pays the whole build —
    the 187.5 s "Loading your library…" above. The users come from auth's own
    list, so the warmed key is the one that person's page will ask for; an
    install with no users warms the default ("") scope.
    """
    try:
        from server import auth as auth_mod
        users = [str(u).strip() for u in (auth_mod.list_users() or [])]
    except Exception:
        users = []
    for user in [u for u in users if u] or [""]:
        build_home(cfg, user)


def _build_home_payload(cfg, user, recent_count):
    """The shelves themselves — the one place that walks the library for
    Home. `build_home` caches this; `warm_home` fills it at startup.

    Since 2026-10-17 Home is a LISTENER's page: genres, moods, the caller's
    own stars and what actually gets played, built from the library tree, the
    ratings store and the play store. The maintenance shelves (grade strip,
    needs-attention, pending albums, most-collected artists) are gone from it
    — the Library, Grading and the In-progress page are where those belong.
    """
    from server import library as lib_mod

    lib = lib_mod.build_library(cfg)
    artists = lib.get("artists", [])
    albums = [alb for ar in artists for alb in ar.get("albums", [])]

    stats = {
        "artists": len(artists),
        "albums": len(albums),
        "tracks": sum(a.get("track_count") or len(a.get("tracks") or []) for a in albums),
    }
    try:
        from server import playlists as pl
        stats["playlists"] = len(pl.list_playlists(user) or [])
    except Exception:
        stats["playlists"] = 0
    try:
        from server import plays as plays_store
        stats["plays"] = plays_store.count(user=user)
    except Exception:
        stats["plays"] = 0

    limit = max(4, recent_count)
    listening = _listening_shelves(lib, user, limit)
    recent = _recent(albums, recent_count)
    favorites = _favorites(lib, limit, user)
    featured = {os.path.normcase(os.path.normpath(r["path"]))
                for r in recent + favorites}
    data = {
        "stats": stats,
        "recent": recent,
        "favorites": favorites,
        **listening,
        "rediscover": _rediscover(albums, featured, limit),
        # One row per podcast SERIES in the library, its newest episode on it —
        # empty (and so the shelf is not drawn) unless the library holds one.
        "podcasts": _podcasts(albums, recent_count),
    }
    return data


def invalidate():
    # DROPPED, not merely aged: every caller of this is an explicit ask for
    # fresh rows — the Refresh buttons (`_refresh_library_caches`), a settings
    # save, an import that just landed — so the next build is a real one. The
    # TTL path is the other half of the contract and the one that used to hang
    # a page: a payload that is merely OLD is served and rebuilt behind the
    # request (`build_home`), and a cold process's first ask is worked at
    # startup (`warm_home`).
    with _lock:
        _cache["t"] = 0.0
        _cache["data"] = None
    # The "more like this" index reads the same library payload the shelves
    # below are built from, so one invalidation covers both.
    try:
        from server import recommend
        recommend.invalidate()
    except Exception:
        pass
