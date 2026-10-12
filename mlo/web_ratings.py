"""Web ratings — what the public thinks of an album, and of a track.

The app already stores ONE rating: ``RATING``, the listener's own stars.
The owner asked for the OTHER kind — the aggregated public score a person
sees on MusicBrainz — and this module is that layer:

  * the SOURCE layer, one parser turning the payload the app would already
    have fetched into ``(value, count, label)`` on the app's own 0-100 scale;
  * the CHOICE, the FIRST source that answered — the configured order IS the
    hierarchy, so the number on the file is a score a reader can open the site
    and check, never a blend no site publishes;
  * the WRITER, which fills the four tags and never replaces a value the file
    already holds unless it is forced to;
  * the SCRIPT runner (id 24), album-scoped like its neighbours.

MusicBrainz is the ONLY source left (2026-10-12, the owner's decision — no
scraped site; see ``mlo.config``). Its rating is read through the same cached,
throttled client as every other MusicBrainz call.

THE SCALE is Picard's 0-100 — the very scale ``RATING`` uses, one half-star =
10 — because that is what a player shows beside the user's own stars and it
is what makes the two comparable at a glance. MusicBrainz publishes on 0-5
(``{"value": 4.35, "votes-count": 31}``, VERIFIED live), converted in ONE
place (``SOURCE_PARSERS``).

THE ALBUM VALUE is written to EVERY track of the album, exactly the way
``ALBUMITUNESADVISORY`` carries an album-wide answer; the TRACK value is the
track's own. Nothing invents a track value from an album one: a track whose
own sources said nothing keeps ALBUMWEBRATING and no WEBRATING, and a reader
that wants "what people think of this song" reads WEBRATING exactly as a
reader that means the release reads ALBUMWEBRATING.

provenance rides beside each value (``WEBRATING_SOURCE``,
``ALBUMWEBRATING_SOURCE``) as the "; "-joined names of the sources that
contributed, the app's own convention for a joined list (``RELEASECOUNTRY``).

WHERE THE ANSWERS COME FROM. Nothing here talks to the network: mlo must not
import server, so the whole HTTP side is an INJECTED fetcher — a single
callable ``fetch(source, kind, ident, cfg)`` returning the raw payload (or
None), supplied by ``server.integrations.web_rating_fetchers()``. That is what
makes the parser and the walk testable with saved fixtures, and it is why a
source that cannot answer costs nothing: it returns None and the walk moves on.
"""
from __future__ import annotations

import math
import os

from .audio import AudioFile
from .config import should_write_audio_tag
from .paths import LIB_AUDIO_EXTS
from .stats import (
    _collect_targets, _find_albums, _make_pbar, _pbar_skip, _pbar_update,
    is_audio_file, new_stats, worker_count,
)
from .ui import print_header, log, c, Color

# --------------------------------------------------------------------------- #
# The scale, and the tag contract
# --------------------------------------------------------------------------- #
# Picard's own, the same one RATING uses (mlo/audio.py) — one half-star = 10.
RATING_MAX = 100
# The scale MusicBrainz publishes on.
SOURCE_SCALE_MAX = 5.0

# The four tags, per level. The VALUE tags hold the integer; the SOURCE tags
# hold the "; "-joined names, like RELEASECOUNTRY.
LEVEL_TAGS = {
    "track": ("WEBRATING", "WEBRATING_SOURCE"),
    "album": ("ALBUMWEBRATING", "ALBUMWEBRATING_SOURCE"),
}
RATING_TAGS = tuple(t for pair in LEVEL_TAGS.values() for t in pair)

# The config key the feature switch lives under (mlo.config.DEFAULT_CONFIG and
# server.script_runners._DISABLED both read it) and the source-order key.
ENABLED_KEY = "web_ratings_enabled"
SOURCES_KEY = "web_ratings_sources"
# The manual override, read straight out of the config like script 15's
# `force_tracklist`: it is not in `_FORCE_KEYS`, so no menu offers it (a web
# change, and this module's owner owns the Python half only) — a person who
# wants a run to replace values by hand sets this key. Fill-only is the
# default and the contract.
FORCE_KEY = "force_web_ratings"

# --------------------------------------------------------------------------- #
# The sources — the built-in RANK, the labels, and what each one really is
# --------------------------------------------------------------------------- #
# A PRIORITY LIST, exactly like GENRE_SOURCES: it fixes the order the sources
# are ASKED in, which is the order their names appear in the SOURCE tag, and
# the order a tie is reported in. The order only decides presentation.
#
#   musicbrainz        the release group's rating for the album, the
#                      recording's own for the track (a WORK's rating is the
#                      fallback — see `parse_musicbrainz_track`).
# MusicBrainz is the ONLY source (2026-10-12, the owner's decision — no scraped
# site; see `mlo.config`). Its rating is read through the same cached, throttled
# client as every other MusicBrainz call.
SOURCES = ["musicbrainz"]

SOURCE_LABELS = {
    "musicbrainz": "MusicBrainz",
}

# Which level each source can answer at. MusicBrainz states a per-track rating
# of its own (the recording — see `parse_musicbrainz_track`), so it may fill
# WEBRATING. When it states nothing for the TRACK, its ALBUM answer is used for
# that track (still attributed to the source); the album's own value is written
# to ALBUMWEBRATING as before.
ALBUM_SOURCES = ("musicbrainz",)
TRACK_SOURCES = ("musicbrainz",)

# One honest line per source for a settings list, exactly like the lyrics and
# genre chains carry: the coverage it is good at, and the caveat it comes with.
SOURCE_NOTES = {
    "musicbrainz": "Open data: the release group's community rating for the "
                   "album, the recording's own for the track. Free, no key, "
                   "MBID-native.",
}


# --------------------------------------------------------------------------- #
# Scale conversion
# --------------------------------------------------------------------------- #
def _to_int(value):
    """A numeric payload value as a float, or None — never a guess."""
    try:
        got = float(value)
    except (TypeError, ValueError):
        return None
    return got


def scale_to_100(value, scale_max=SOURCE_SCALE_MAX):
    """*value* on a 0-*scale_max* scale as the app's integer 0-100 — TRUNCATED
    to the grid, never rounded above the source's own score.

    The ONE conversion every parser calls, so two sources that publish the
    same score on the same scale can never land on two different numbers.
    A value outside the scale is CLAMPED rather than dropped: 4.9/5 is a real
    answer, and a source that publishes 5.5 on a 0-5 scale is stating "as high
    as it goes", not nothing.

    WHY NOT THE NEAREST INTEGER. The app's grid is 0.05 (one 0-100 step), and
    the number a reader sees is that grid value printed to ONE decimal
    (`web/src/lib/ratings.ts` `webText`). Rounding the source UP to the
    nearest grid point puts values that sit just under a tenth's boundary ON
    the boundary, and the second rounding then carries them the rest of the
    way: MEASURED 2026-10-11 on the owner's "Clint Eastwood", a scraper's
    4.54 became 91 = 4.55 and the tag read 4.6, while 4.54 at one decimal is
    4.5 — the rounding the site itself shows. Truncating keeps the display
    equal to the source's own one-decimal reading for EVERY value: the grid
    points are exactly the boundaries a one-decimal rounding can land on, so
    the tag never crosses one the source did not cross. The stored value is
    the source's score rounded DOWN to the app's resolution — 4.54 → 90 →
    "4.5", 4.45 → 89 → "4.5", 4.55 → 91 → "4.6" — never a number above what
    the source published.
    """
    number = _to_int(value)
    if number is None or scale_max <= 0:
        return None
    if number < 0:
        return 0
    scaled = number / float(scale_max) * RATING_MAX
    # The tolerance is for BINARY NOISE, not for the rule: a score that IS on
    # the grid (4.35/5 is exactly 87) computes as 86.99999999999999 in IEEE
    # doubles, and truncating that would land one step low — the same
    # double-rounding defect from the other side (4.35 must read "4.4" on a
    # one-decimal display, and 86 → 4.30 → "4.3" is a wrong number). 1e-9 of a
    # 0-100 point is far below any precision a source publishes.
    return max(0, min(RATING_MAX, math.floor(scaled + 1e-9)))


def _weight(count):
    """A source's own vote count, as parsed from its payload.

    The count the source stated, or 1 when it states none — carried beside the
    value as part of that source's answer (a 3 458-vote average and a single
    vote are different reads of the same payload), never a weight in the
    app's own arithmetic: the CHOICE is by configured order (`choose`).
    """
    number = _to_int(count)
    if number is None or number <= 0:
        return 1
    return int(number)


def _rating_pair(value, count, scale_max=SOURCE_SCALE_MAX):
    """``(value_0_100, count)`` from a raw ``(value, count)`` pair, or None.

    The shared tail of every parser: a payload that states no value is not an
    answer at all (MusicBrainz answers ``{"value": null, "votes-count": 0}``
    for an unrated entity — that is "nobody rated this", not "rated zero").
    The COUNT travels with the value — it is part of the source's own answer
    (a 3 458-vote average and a single vote are different reads of the same
    payload) — but it never decides which source's score the file gets: the
    configured ORDER does (`choose`).
    """
    score = scale_to_100(value, scale_max)
    if score is None:
        return None
    return score, _weight(count)


# --------------------------------------------------------------------------- #
# Per-source payload parsers
# --------------------------------------------------------------------------- #
# Every one of them takes the payload the fetcher returned and answers
# ``(value_0_100, weight, label)`` or None. They are pure: no network, no
# config, no file — which is what lets tools/test_web_ratings.py pin them
# against saved fixtures.

def parse_musicbrainz(payload):
    """A MusicBrainz entity node with ``inc=ratings`` -> the rating.

    VERIFIED live (2026-10-01): ``{"rating": {"value": 4.35, "votes-count":
    31}}`` on a recording, ``{"rating": {"value": 4.75, "votes-count": 105}}``
    on a release group, and ``{"value": null, "votes-count": 0}`` on a work
    nobody has rated — the last one is a miss, not a zero.
    """
    if not isinstance(payload, dict):
        return None
    node = payload
    rating = node.get("rating") if isinstance(node.get("rating"), dict) else None
    if rating is None:
        return None
    # `votes-count` is MusicBrainz's own spelling; `count` is what the older
    # payloads and a couple of its sibling entities state, so it is read when
    # the first is absent — the VALUE is what decides whether this is an
    # answer at all, so the fallback has to be resolved before it is converted.
    count = rating.get("votes-count")
    if count is None:
        count = rating.get("count")
    pair = _rating_pair(rating.get("value"), count)
    if pair is None:
        return None
    return pair[0], pair[1], SOURCE_LABELS["musicbrainz"]


def parse_musicbrainz_track(payload):
    """The RECORDING's own rating, or the WORK's when the recording has none.

    The owner's rule, straight out of the live measurement: MusicBrainz
    states a rating on the recording far more often than on the work it
    performs (measured on The Dark Side of the Moon, 2026-10-01: 4 of 4
    recordings rated, 1 of 4 works rated, most works carrying
    ``{"value": null, "votes-count": 0}``). So the recording's own answer is
    read FIRST and the work is only a fallback — the other way round would
    throw away a rating the app already had.

    *payload* is ``{"recording": node, "work": node|None}: the fetcher asks
    for both in the one request shape the app already uses
    (``inc=ratings+genres+work-rels``, VERIFIED to answer both in one call).
    """
    if not isinstance(payload, dict):
        return None
    own = parse_musicbrainz(payload.get("recording"))
    if own is not None:
        return own
    return parse_musicbrainz(payload.get("work"))


# The one dispatch: source id -> parser. A source absent from here cannot
# answer at all, so adding a source is one entry in SOURCES, one parser and one
# entry here.
SOURCE_PARSERS = {
    "musicbrainz": parse_musicbrainz,
}


def parse_source(source, payload, kind="album"):
    """One source's payload -> ``(value_0_100, weight, label)`` or None.

    The MUSICBRAINZ entry is level-aware: an album payload is a release-group
    node, a track payload is ``{"recording", "work"}`` (see
    ``parse_musicbrainz_track``).
    """
    source = str(source or "").strip().lower()
    parser = SOURCE_PARSERS.get(source)
    if parser is None or payload is None:
        return None
    if kind == "track" and source == "musicbrainz":
        parser = parse_musicbrainz_track
    try:
        return parser(payload)
    except Exception:
        # A parser is pure, but a payload is a stranger's data: a shape that
        # makes one of them raise is that source's miss and nobody else's.
        return None


# --------------------------------------------------------------------------- #
# The choice
# --------------------------------------------------------------------------- #
def choose(answers):
    """The FIRST source's own answer — the order IS the hierarchy, or None.

    ``answers`` are ``((value_0_100, count, label), level)`` pairs in the
    order the sources were asked, each ``level`` "track" or "album".

    Two rules, in this order:

    * a TRACK-level answer beats an album-level one, whoever stated it — the
      value on a track is the track's own score, and an album's score standing
      in for it is a fallback, not a verdict;
    * within one level the ORDER decides, so the configured chain
      (``web_ratings_sources``) is a true hierarchy: the first source that
      answers is the value on the file.

    NOT a mean. A score a person can open and verify beats a blend no site
    publishes.

    Returns ``{"value": int, "sources": [label]}`` or None when no source
    answered. ``sources`` is the one name whose number the file carries — it
    is the provenance of THIS value, and a source that answered for nothing is
    never named.
    """
    first_album = None
    for got, level in answers or ():
        if not got or got[0] is None:
            continue
        if level == "track":
            return {"value": got[0], "sources": [str(got[2] or "").strip()] if got[2] else []}
        if first_album is None:
            first_album = got
    if first_album is None:
        return None
    return {"value": first_album[0],
            "sources": [str(first_album[2] or "").strip()] if first_album[2] else []}


def provider_order(cfg=None):
    """The sources to ask, in order. ``cfg["web_ratings_sources"]`` wins when
    it names any known source (order kept, unknown ids dropped); otherwise the
    built-in rank. Mirrors ``mlo.lyrics_providers.provider_order``, so a config
    a hand edited can never wedge the feature."""
    cfg = cfg or {}
    wanted = cfg.get(SOURCES_KEY)
    if isinstance(wanted, str):
        wanted = [wanted]
    order, seen = [], set()
    for raw in wanted if isinstance(wanted, (list, tuple)) else ():
        source = str(raw or "").strip().lower()
        if source in SOURCE_PARSERS and source not in seen:
            seen.add(source)
            order.append(source)
    return order or [s for s in SOURCES if s in SOURCE_PARSERS]


# --------------------------------------------------------------------------- #
# One entity's rating — the sources, asked in the configured order
# --------------------------------------------------------------------------- #
def _rate(kind, ident, cfg, fetch, album=None):
    """``{"value", "sources"}`` for one entity, or None when nobody answered.

    *fetch* is the injected callable (see the module docstring). The sources
    are asked in the configured order (the hierarchy) and the walk STOPS at
    the first answer that can decide the value — `choose`'s two rules: a
    TRACK-level answer settles a track, and the first answer settles an album
    — so a source below the one that answered is never asked at all. That is
    both the rule and the speed: an album whose MusicBrainz node answers costs
    one request.

    The callable is allowed to raise — this is where a caller's fetcher meets
    a stranger's network — and a raise is that source's miss, not the
    caller's.

    For a TRACK, a source that states nothing for the track itself falls back
    to its ALBUM answer (`album` is that entity's identity, when the caller
    has it) — the owner's rule: track, then album, then artist, per platform.
    An album-level answer does NOT end the walk (a later source may state the
    track's own score), and the level each source actually answered at is
    reported in ``levels``, so a track whose value came from the album says
    so.
    """
    allowed = TRACK_SOURCES if kind == "track" else ALBUM_SOURCES
    order = [s for s in provider_order(cfg) if s in allowed]
    answers, asked, answered, levels = [], [], [], {}
    for source in order:
        asked.append(source)
        level = kind
        try:
            payload = fetch(source, kind, ident, cfg)
        except Exception:
            payload = None
        got = parse_source(source, payload, kind)
        if got is None and kind == "track" and album is not None:
            # This source states nothing for the TRACK: its ALBUM answer is
            # fetched and only an album-wide value can stand in, marked album
            # level. MusicBrainz's album payload carries no per-track rows.
            try:
                payload = fetch(source, "album", album, cfg)
            except Exception:
                payload = None
            got = parse_source(source, payload, "album")
            if got is not None:
                level = "album"
        if got is None:
            continue
        answers.append((got, level))
        answered.append(source)
        levels[source] = level
        if level == kind:
            # The level the caller asked for just answered: nothing below it
            # in the hierarchy can change the value (`choose`).
            break
    result = choose(answers)
    if result is None:
        return None
    result["asked"] = asked
    result["answered"] = answered
    result["levels"] = levels
    return result


def album_rating(artist, album, rg_mbid, cfg, fetch):
    """The ALBUM's web rating — the first source in the chain that answers."""
    return _rate("album", {"artist": str(artist or "").strip(),
                           "album": str(album or "").strip(),
                           "mbid": str(rg_mbid or "").strip()},
                 cfg, fetch)


def track_rating(artist, title, recording_mbid, cfg, fetch, album=None):
    """The TRACK's web rating — the track's own score when any source states
    one, else the first source's ALBUM score for it.

    Every source is asked for the TRACK first (MusicBrainz's recording), and a
    source that states nothing for the track falls back to its ALBUM answer —
    `album` is the album's identity (``{"artist", "album", "mbid"}``) when the
    caller has it. The walk stops at the first TRACK-level answer; an
    album-level one is kept only while nothing better arrives. The level each
    source answered at is on the result's ``levels`` map.
    """
    return _rate("track", {"artist": str(artist or "").strip(),
                           "title": str(title or "").strip(),
                           "mbid": str(recording_mbid or "").strip()},
                 cfg, fetch, album=album)


# --------------------------------------------------------------------------- #
# The writer — fill only, and say what it did
# --------------------------------------------------------------------------- #
def _existing(af, tag):
    try:
        return str(af.get_tag(tag) or "").strip()
    except Exception:
        return ""


def _file_aliases(af):
    """The alias tags on a file as the search walk's input, or {}.

    `mlo.audio.file_aliases` is the one reader of those tags (bare or
    locale-suffixed, any container spelling); a file that will not open, or
    carries none, simply has no second pass.
    """
    try:
        from mlo.audio import file_aliases
        return file_aliases(af.all_tags())
    except Exception:
        return {}


def write_ratings(path, album=None, track=None, cfg=None, force=False):
    """Fill the four tags on ONE file; never replace a value unless *force*.

    The app's fill-only rule (the one every writer but an import's first pass
    obeys — ``server.imports.drop_arrived_values`` is the single documented
    exception): a value already on the file is a fact of the file, and a second
    run of a fetch must not be able to change it. This is what makes "web
    rating" safe to run over a library that a person has hand-corrected.

    *album* / *track* are ``{"value", "sources"}`` as ``aggregate`` returns
    them, or None. Each PAIR is written or skipped as a unit: the value and
    its provenance are one answer, so a file that already holds WEBRATING
    keeps the SOURCE line that belongs to it.

    Every write goes through ``should_write_audio_tag`` (the
    ``audio_tag_writes`` matrix and the family's master switch), so a user who
    turned the family off gets nothing written and an honest ``gated`` note
    instead of a silent no-op.

    Returns ``{"path", "wrote": {tag: value}, "skipped": {tag: why}}`` and
    never raises on a file it cannot open.
    """
    cfg = cfg or {}
    result = {"path": path, "wrote": {}, "skipped": {}, "error": ""}
    try:
        # An OPEN handle is accepted as well as a path: a caller that has just
        # read the file (the runner does, for the identity tags) must not pay a
        # second decode, and a test can hand over its own writer.
        af = path if hasattr(path, "set_tag") else AudioFile(path)
    except Exception as e:      # an unreadable path is reported, not raised
        result["error"] = str(e)
        return result
    if getattr(af, "audio", None) is None:
        result["error"] = str(getattr(af, "error", "") or "unreadable")
        return result

    for kind, answer in (("album", album), ("track", track)):
        value_tag, source_tag = LEVEL_TAGS[kind]
        if not answer:
            result["skipped"][value_tag] = "no answer"
            continue
        current = _existing(af, value_tag)
        if current and not force:
            # A value the file already holds: left exactly as it is, and the
            # pairing rule above is why the SOURCE line is left with it.
            result["skipped"][value_tag] = "already set"
            result["skipped"][source_tag] = "already set"
            continue
        if not should_write_audio_tag(cfg, value_tag, filepath=af.path):
            result["skipped"][value_tag] = "write gate is off"
            result["skipped"][source_tag] = "write gate is off"
            continue
        try:
            ok = af.set_tag(value_tag, str(int(answer["value"])))
        except Exception as e:
            result["skipped"][value_tag] = f"write failed: {e}"
            continue
        if not ok:
            result["skipped"][value_tag] = "write failed"
            continue
        result["wrote"][value_tag] = int(answer["value"])
        names = "; ".join(answer.get("sources") or [])
        if names:
            try:
                if af.set_tag(source_tag, names):
                    result["wrote"][source_tag] = names
            except Exception:
                # The VALUE is what the feature is for; a provenance line that
                # would not write must not undo it — but it is reported.
                result["skipped"][source_tag] = "write failed"
        else:
            result["skipped"][source_tag] = "no sources named"
    return result


# --------------------------------------------------------------------------- #
# Script 24 — the album-scoped runner
# --------------------------------------------------------------------------- #
def _album_identity(rows):
    """``(artist, album, release-group mbid)`` from an album's read tracks.

    Album-level identity is uniform across the files, so the first readable
    track that states each field settles it — the same probe
    ``server.script_runners._album_release_id`` uses, and the same reason: one
    unreadable file must not cost the album its answer.
    """
    artist = album = rg = ""
    for row in rows:
        artist = artist or row["artist"] or row["albumartist"]
        album = album or row["album"]
        rg = rg or row["rg_mbid"]
        if artist and album and rg:
            break
    return artist, album, rg


def run_web_ratings(config, fetch=None):
    """Script 24 — fill every album's web rating, and every track's.

    ALBUM-SCOPED like its neighbours: the album list comes from
    ``config["targets"]`` when a run is scoped and from a walk of the music
    folder otherwise, and the albums are processed by a ThreadPoolExecutor so
    the per-source network waits overlap instead of queueing. One album's
    identity is read ONCE (the release-group id and the names the sources are
    asked by), its ALBUM rating is fetched once and written to every track,
    and each track's own rating is fetched for that track — the album answer
    and every track answer of one album ride the SAME bounded pool, so the
    per-track network waits overlap instead of queueing (MusicBrainz serves
    one request per second; the request COUNT is unchanged, only the waits
    overlap).

    Nothing is invented: a track whose recording has no rating keeps no
    WEBRATING, an album no source answered for keeps no ALBUMWEBRATING, and
    every run says which sources answered and which were skipped and why.
    The FETCHES obey the writer's fill-only rule too: a file that already
    carries WEBRATING is not asked about, and the album is asked about only
    while some row still lacks ALBUMWEBRATING, so re-running the chain over a
    rated library costs no network — unless ``force_web_ratings`` is set.
    *fetch* is the injected network callable; without one the app's own
    fetchers are imported lazily from ``server.integrations`` (mlo must not
    import server at module scope — see the module docstring).
    """
    config = config or {}
    stats = new_stats()
    stats["album_count"] = 0
    stats["track_count"] = 0
    stats["by_source"] = {}
    stats["skipped"] = {}

    print_header("Web ratings")
    if not bool(config.get(ENABLED_KEY, True)):
        log("web ratings are off (web_ratings_enabled)")
        return stats
    if fetch is None:
        try:
            from server.integrations import web_rating_fetchers
            fetch = web_rating_fetchers()
        except Exception as e:
            log(c(f"web rating fetchers unavailable: {e}", Color.RED))
            stats["error_count"] += 1
            stats["errors"].append(str(e))
            return stats

    force = bool(config.get(FORCE_KEY, False))
    order = provider_order(config)
    log("sources: " + (" -> ".join(SOURCE_LABELS[s] for s in order)
                       if order else "none (nothing to ask)"))
    log(f"existing values: {'rewritten (force)' if force else 'kept'}")

    albums = _album_dirs(config)
    if not albums:
        log("No albums found.")
        return stats

    counts = {"ok": 0, "skip": 0, "fail": 0}
    pbar = _make_pbar(total=len(albums), desc="Web ratings", unit="album")

    # THE LANE POLICY — the one mlo.accurip.run_generate_accurip documents. One
    # album is one lane, and the TRACKS inside an album share the budget that
    # lane was given: the rating pool one album may open is the SAME budget
    # DIVIDED among the album lanes, never one width per album, so a Run All
    # over N albums cannot turn into lanes x N requests in flight. The album
    # budget itself is mlo.stats.worker_count's (capped at 8, the ceiling the
    # other network scripts declare). A single-album run — the Import Wizard's
    # case, which never reaches the album pool — has items=1, so `workers` is 1
    # lane and `track_width` hands that whole width to its tracks.
    workers = worker_count(config, maximum=6, items=len(albums))
    track_width = max(1, worker_count(config, maximum=8) // max(1, workers))

    def _one_album(album_dir):
        """Everything one album costs; runs on a worker thread.

        The album's own answer and every track's are fetched on ONE pool of
        `track_width` lanes, and the writes run once it has JOINED — every
        counter is a local here, so the pool never races on shared state, the
        shape mlo.lyrics_fetch uses for its tracks, applied one level up
        because the ALBUM answer is fetched once for the whole folder.

        The overlap is the point: every ask is against MusicBrainz, which serves
        one request per second, so a strictly serial track loop paid that wait
        once per track — only the WAITS overlap. Request COUNT is unchanged —
        still one ask per source per entity. Order is unchanged too: each
        answer is parsed in the configured source order (see ``_rate``), and
        the writes below visit ``rows`` (sorted file names) in order, so a
        ThreadPool completion order can never reach a
        tag.
        """
        out = {"album": album_dir, "album_rating": None, "tracks": 0,
               "wrote": 0, "skipped": 0, "by_source": {}, "errors": [],
               "gated": False}
        try:
            names = sorted(f for f in os.listdir(album_dir)
                           if is_audio_file(f))
        except OSError as e:
            out["errors"].append(f"{album_dir}: {e}")
            return out
        rows = []
        for name in names:
            path = os.path.join(album_dir, name)
            try:
                af = AudioFile(path)
                if getattr(af, "audio", None) is None:
                    continue
                rows.append({
                    "path": path, "af": af,
                    "title": str(af.get_tag("TITLE") or "").strip(),
                    "artist": str(af.get_tag("ARTIST") or "").strip(),
                    "albumartist": str(af.get_tag("ALBUMARTIST") or "").strip(),
                    "album": str(af.get_tag("ALBUM") or "").strip(),
                    "rg_mbid": str(af.get_tag("MUSICBRAINZ_RELEASEGROUPID") or "").strip(),
                    "rec_mbid": str(af.get_tag("MUSICBRAINZ_TRACKID") or "").strip(),
                    # What the file ALREADY holds for the two VALUE tags,
                    # read with `write_ratings`' own `_existing` so the fetch
                    # skips below and the writer can never disagree about what
                    # "already set" means. (The SOURCE tags are not tested
                    # separately: the writer keys the whole pair off the value
                    # tag alone.)
                    "web_rating": _existing(af, "WEBRATING"),
                    "album_web_rating": _existing(af, "ALBUMWEBRATING"),
                    # The alias tags the file carries, in the shape the shared
                    # search walk takes — the second pass's input when the
                    # stored names find nothing (see `_track_answer`).
                    "aliases": _file_aliases(af),
                })
            except Exception as e:
                out["errors"].append(f"{os.path.basename(path)}: {e}")
        if not rows:
            return out
        artist, album, rg = _album_identity(rows)

        # THE FETCHES FOLLOW THE WRITES' OWN FILL-ONLY RULE. A request is
        # worth making only while its answer could still be written:
        # `write_ratings` skips a value already on the file, so a track whose
        # WEBRATING is set can never take a track answer, and the album answer
        # is asked for only while at least one row still lacks
        # ALBUMWEBRATING. *force* bypasses both skips, exactly as it bypasses
        # the writes (R362/R11). The test is `_existing` — the writer's own —
        # so a skip here can never disagree with it and lose a value it would
        # have accepted.
        need_album = force or any(not row["album_web_rating"] for row in rows)

        def _track_answer(row):
            """One track's own rating — its OWN row, nothing shared.

            A row that already carries a WEBRATING makes no request: the
            writer would skip the answer, so no answer is what it is.

            The STORED names are asked first; only when NO source answered for
            them are the track's ALIAS names asked the same way — a platform
            that files the release under the localized name answers only to
            that name. A track that found something pays nothing extra.
            """
            if not force and row["web_rating"]:
                return None
            artist_row = row["artist"] or row["albumartist"]
            around = {"artist": artist, "album": album, "mbid": rg}
            got = track_rating(artist_row, row["title"], row["rec_mbid"],
                               config, fetch, album=around)
            if got is not None:
                return got
            try:
                from mlo.lyrics_providers import _alias_queries
            except Exception:
                return None
            for a_name, t_name, _al, _entity, _query in _alias_queries(
                    artist_row, row["title"], album, row.get("aliases") or {}):
                got = track_rating(a_name, t_name, row["rec_mbid"], config,
                                   fetch, album=around)
                if got is not None:
                    return got
            return None

        def _album_answer():
            """The album's own ask, once for the folder — or no request at all
            when every row already carries ALBUMWEBRATING (unless forced)."""
            if not need_album:
                return None
            return album_rating(artist, album, rg, config, fetch)

        if track_width == 1:
            album_answer = _album_answer()
            answers = [_track_answer(row) for row in rows]
        else:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=track_width) as ex:
                # The album's own ask goes first and on its own lane, while the
                # track asks are already in flight; the answer is awaited once
                # for the whole folder.
                album_fut = ex.submit(_album_answer)
                track_futs = [ex.submit(_track_answer, row) for row in rows]
                album_answer = album_fut.result()
                answers = [fut.result() for fut in track_futs]
        out["album_rating"] = album_answer
        # The writes happen on THIS thread, once the pool has joined, in the
        # sorted-`rows` order — each track writes only its own file, and the
        # counters below are the album's own dict.
        for row, track_answer in zip(rows, answers):
            out["tracks"] += 1
            res = write_ratings(row["af"], album=album_answer,
                                track=track_answer, cfg=config, force=force)
            if res.get("error"):
                out["errors"].append(f"{os.path.basename(row['path'])}: {res['error']}")
                continue
            if res["wrote"]:
                out["wrote"] += 1
            else:
                out["skipped"] += 1
            if any("gate" in str(v) for v in res["skipped"].values()):
                out["gated"] = True
            for source in (album_answer or {}).get("answered", []):
                out["by_source"][source] = out["by_source"].get(source, 0) + 1
        if out["wrote"]:
            _drop_tag_cache([album_dir])
        return out

    results = []
    try:
        if len(albums) == 1 or workers == 1:
            for album_dir in albums:
                results.append(_one_album(album_dir))
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(_one_album, d): d for d in albums}
                for fut in as_completed(futures):
                    try:
                        results.append(fut.result())
                    except Exception as e:   # a worker must never kill the run
                        results.append({"album": futures[fut], "tracks": 0,
                                        "wrote": 0, "skipped": 0,
                                        "by_source": {}, "errors": [str(e)],
                                        "album_rating": None})
    finally:
        try:
            pbar.close()
        except Exception:
            pass

    for out in results:
        # One ALBUM per row, which is the unit the bar and every counter below
        # are in — `total_scanned` is what the progress header and the chain's
        # report read, so a run that touched albums must not report 0.
        stats["total_scanned"] += 1
        for source, count in (out.get("by_source") or {}).items():
            stats["by_source"][source] = stats["by_source"].get(source, 0) + count
        for message in out.get("errors") or []:
            stats["error_count"] += 1
            if len(stats["errors"]) < 25:
                stats["errors"].append(message)
        if out.get("album_rating"):
            stats["album_count"] += 1
        stats["track_count"] += out.get("tracks") or 0
        stats["modified_count"] += out.get("wrote") or 0
        stats["skipped_count"] += out.get("skipped") or 0
        if out.get("gated"):
            stats["skipped"]["write gate"] = stats["skipped"].get("write gate", 0) + 1
        if out.get("wrote"):
            _pbar_update(pbar, counts, "ok")
        elif out.get("errors"):
            # Errors were counted above; a failed album is a "fail" tick.
            pass
        else:
            _pbar_skip(pbar, counts)

    if stats["by_source"]:
        # Per-source counts are TRACKS whose ALBUM answer that source
        # contributed to (the album answer is fetched once and written to the
        # whole folder), so the line sits beside "tracks written" and means the
        # same unit.
        log("sources (tracks carrying their answer): " + " · ".join(
            f"{SOURCE_LABELS.get(s, s)}: {n}"
            for s, n in sorted(stats["by_source"].items(), key=lambda kv: -kv[1])))
    if stats["skipped"]:
        log("skipped: " + " · ".join(f"{k}: {v}" for k, v in sorted(stats["skipped"].items())))
    if not stats["by_source"]:
        log(c("no source answered — nothing written", Color.YELLOW))
    log(c(f"albums rated: {stats['album_count']} · tracks written: "
          f"{stats['modified_count']} · skipped: {stats['skipped_count']} · "
          f"failed: {stats['error_count']}",
          Color.GREEN if not stats["error_count"] else Color.YELLOW))
    return stats


def _drop_tag_cache(folders):
    """Drop the tag cache of the albums this run rewrote — and only those.

    ``/api/run`` (server.api_run._invalidate_run) and the import pipeline
    invalidate the folders a run names, but a script must be correct for every
    entry point (a terminal run has no cache at all). Per-album, exactly like
    ``mlo.taghygiene``: ``invalidate_all`` — the whole library re-parsed on the
    next page — is what a scoped album pass must never charge.
    """
    folders = [f for f in folders if str(f or "").strip()]
    if not folders:
        return
    try:
        from server import tagcache
    except Exception:
        return                      # a terminal run: no server, no cache
    try:
        tagcache.invalidate_album(*folders)
    except Exception:
        pass


def _album_dirs(config):
    """The album folders a run covers: the scoped targets, or the library."""
    folder = str(config.get("music_folder") or "")
    if config.get("targets") is not None:
        files = sorted(_collect_targets(config["targets"], LIB_AUDIO_EXTS))
        return sorted({d for d in (os.path.dirname(f) for f in files)
                       if os.path.isdir(d)})
    if not os.path.isdir(folder):
        return []
    return _find_albums(folder)
