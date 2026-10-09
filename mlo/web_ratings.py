"""Web ratings — what the public thinks of an album, and of a track.

The app already stores ONE rating: ``RATING``, the listener's own stars.
The owner asked for the OTHER kind — the aggregated public score a person
sees on MusicBrainz, RateYourMusic, Album of the Year or Discogs — and this
module is that layer:

  * the SOURCE layer, one parser per provider turning the payload the app
    would already have fetched into ``(value, weight, label)`` on the app's
    own 0-100 scale;
  * the NORMALISER, a weighted mean over the sources that answered;
  * the WRITER, which fills the four tags and never replaces a value the file
    already holds unless it is forced to;
  * the SCRIPT runner (id 24), album-scoped like its neighbours.

THE SCALE is Picard's 0-100 — the very scale ``RATING`` uses, one half-star =
10 — because that is what a player shows beside the user's own stars and it
is what makes the two comparable at a glance. Every source here publishes on
a different scale and each is converted in ONE place (``SOURCE_PARSERS``):

  * MusicBrainz 0-5 (``{"value": 4.35, "votes-count": 31}``, VERIFIED live)
  * RateYourMusic 0.5-5 (``<meta itemprop="ratingValue" content="4.18" />``,
    VERIFIED live against a real release page, and the
    ``page_section_main_info_music_rating_value`` component a 2026 page serves
    instead — see ``rym_rating_from_html``; a release page answers for the
    album, a SONG page for the track)
  * Discogs 0-5 (``community.rating = {"average": 4.72, "count": 3809}``,
    VERIFIED live)
  * Album of the Year 0-100 — see the note on that source below.

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
None), supplied by ``server.integrations.web_rating_fetchers()`` and by
``server.discovery`` for the Discogs release. That is what makes the parsers
and the aggregation testable with saved fixtures, and it is why a source that
cannot answer costs nothing: it returns None and the mean is taken over the
sources that did.

ALBUM OF THE YEAR IS READ FROM AN ARCHIVED CAPTURE, deliberately. Its site
answers Cloudflare's JS challenge to every automated client this project can
run — 403 to a plain HTTP client, to HEADLESS and to HEADED Chromium, and to a
reader proxy (all measured 2026-10-01) — so the live page is not asked at all;
``server.integrations.aoty_album_page`` resolves the album's own page through
the Wayback index and replays the newest capture, the same route the RYM
readers take when there is no cookie. The page's markup this parser reads was
taken from exactly such a capture, so the parse is pinned to a real payload
rather than guessed: a source whose payload could not be pinned would not ship
at all.
"""
from __future__ import annotations

import os
import re

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
# The scale every source but Album of the Year publishes on.
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
# the order a tie is reported in. The VALUE is not a pick-the-first answer —
# every source that answers contributes to the weighted mean — so the order
# only decides presentation.
#
#   a. rateyourmusic   the release page's average (0.5-5) and its vote count
#                      for the album, the SONG page's own average and count
#                      for the track (RYM rates both).
#                      FIRST because it is the score this app's owner already
#                      reads, and because its album answer is the one the
#                      genre chain also wants (one page read, two answers).
#   b. musicbrainz     the release group's rating for the album, the
#                      recording's own for the track (a WORK's rating is the
#                      fallback — see `parse_musicbrainz_track`).
#   c. albumoftheyear  the album's user score.
#   d. discogs         the release's `community.rating`, when a token is set.
SOURCES = ["rateyourmusic", "musicbrainz", "albumoftheyear", "discogs"]

SOURCE_LABELS = {
    "rateyourmusic": "RateYourMusic",
    "musicbrainz": "MusicBrainz",
    "albumoftheyear": "Album of the Year",
    "discogs": "Discogs",
}

# Which level each source can answer at. Two of them have a per-track
# statement of their own — MusicBrainz rates the recording, and RateYourMusic
# keeps a separate page (and a separate community average) per SONG — so both
# may fill WEBRATING. Album of the Year and Discogs rate a RELEASE only: their
# answers are album-level and are written to ALBUMWEBRATING, never promoted to
# a track.
ALBUM_SOURCES = ("rateyourmusic", "musicbrainz", "albumoftheyear", "discogs")
TRACK_SOURCES = ("rateyourmusic", "musicbrainz")

# One honest line per source for a settings list, exactly like the lyrics and
# genre chains carry: the coverage it is good at, and the caveat it comes with.
SOURCE_NOTES = {
    "rateyourmusic": "The release page's community average (0.5-5) and its "
                     "vote count for the album, the song page's own for the "
                     "track. Live pages need a rym_cookie; without one the "
                     "archived snapshot of the same page answers (when one "
                     "exists).",
    "musicbrainz": "Open data: the release group's community rating for the "
                   "album, the recording's own for the track. Free, no key, "
                   "MBID-native.",
    "albumoftheyear": "The album's user score (0-100) and its genre list. Its "
                      "site refuses every automated client this project can "
                      "run, so the page is read from the newest archived "
                      "capture of it (Wayback) — nothing is guessed.",
    "discogs": "The release's community rating (0-5) and vote count. Needs a "
               "discogs_token in Settings -> Discovery.",
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


def _round_half_up(number):
    """Nearest integer, halves rounded UP — the one rounding rule here.

    Python's own ``round`` rounds halves to the nearest EVEN number, so
    ``round(82.5)`` is 82 and ``round(83.5)`` is 84 — a scale conversion whose
    answer depends on the parity of the digit above it is not a rule a person
    can predict from a page that shows 4.125/5. Every conversion and the
    aggregation itself go through this, so 82.5 always reads 83.
    """
    return int(number + 0.5) if number >= 0 else -int(-number + 0.5)


def scale_to_100(value, scale_max=SOURCE_SCALE_MAX):
    """*value* on a 0-*scale_max* scale as the app's integer 0-100.

    The ONE conversion every parser calls, so two sources that publish the
    same score on the same scale can never land on two different numbers.
    A value outside the scale is CLAMPED rather than dropped: 4.9/5 is a real
    answer that rounds to 98, and a source that publishes 5.5 on a 0-5 scale
    is stating "as high as it goes", not nothing.
    """
    number = _to_int(value)
    if number is None or scale_max <= 0:
        return None
    if number < 0:
        return 0
    return max(0, min(RATING_MAX, _round_half_up(number / float(scale_max)
                                                 * RATING_MAX)))


def _weight(count):
    """The aggregation weight a source's own vote count earns.

    The source's own count when it states one — a score 49 366 people voted
    on outranks one with a single vote — and 1 when it states none, so a
    source that answers without a count still counts exactly once.
    """
    number = _to_int(count)
    if number is None or number <= 0:
        return 1
    return int(number)


def _rating_pair(value, count, scale_max=SOURCE_SCALE_MAX):
    """``(value_0_100, weight)`` from a raw ``(value, count)`` pair, or None.

    The shared tail of every parser: a payload that states no value is not an
    answer at all (MusicBrainz answers ``{"value": null, "votes-count": 0}``
    for an unrated entity — that is "nobody rated this", not "rated zero").
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


# The RYM release page states its community average as schema.org microdata —
# VERIFIED live on a real release page: <div itemprop="aggregateRating"
# itemscope itemtype="http://schema.org/AggregateRating"> <meta
# itemprop="ratingValue" content="4.18" /> <meta itemprop="bestRating"
# content="5.0" /> <meta itemprop="worstRating" content="0.5" /> <meta
# itemprop="ratingCount" content="49366" />. The visual twin
# (<span class="avg_rating">4.18</span>) is read as the fallback, because it is
# the same number on a page whose microdata is missing.
_RYM_VALUE_RE = re.compile(
    r'itemprop="ratingValue"[^>]*content="([0-9]+(?:\.[0-9]+)?)"', re.I)
_RYM_COUNT_RE = re.compile(
    r'itemprop="ratingCount"[^>]*content="([0-9]+)"', re.I)
_RYM_AVG_RE = re.compile(
    r'class="avg_rating"[^>]*>\s*([0-9]+(?:\.[0-9]+)?)\s*<', re.I)
# RYM's 2026 design states the same number in its own component, with NO
# microdata at all — the release page's rating and a SONG page's rating are the
# same component. VERIFIED on the archived song page of Radiohead's "Paranoid
# Android" (web.archive.org/web/20260814042836id_/…/song/radiohead/
# paranoid-android/), which carries neither an `itemprop` nor an `avg_rating`
# span:
#   <div class="page_section_main_info_music_rating_value has_tip">
#     <div class="page_section_main_info_music_rating_value_rating">
#       <img alt="rating bolded" alt="bold star" class="metadata-star-bold" /> 4.67
#     </div>
#     <div class="page_section_main_info_music_rating_value_number">
#       17,654
#       ratings
#     </div>
#   </div>
# The star image's tags are skipped before the value is read, so a digit inside
# an attribute can never be mistaken for the score.
_RYM_SECTION_VALUE_RE = re.compile(
    r'page_section_main_info_music_rating_value_rating[^>]*>\s*'
    r'(?:<[^>]*>\s*)*([0-9]+(?:\.[0-9]+)?)', re.I)
_RYM_SECTION_COUNT_RE = re.compile(
    r'page_section_main_info_music_rating_value_number[^>]*>\s*'
    r'([0-9][0-9,]*)\s*ratings', re.I)


def rym_rating_from_html(html):
    """``{"value", "count"}`` for a RYM release OR song page, or None.

    Called by ``server.integrations._rym_album_answer`` for a release page and
    by ``server.integrations.rym_song_rating`` for a song page — so a rating
    read from a LIVE page and one read from an ARCHIVED snapshot go through the
    same parser, whichever design the page is in.

    TWO designs are accepted, in this order: the schema.org microdata a 2021
    page publishes, the visual ``avg_rating`` twin that sits beside it, and the
    ``page_section_main_info_music_rating_value`` component the 2026 design
    uses instead (see above). The microdata is tried first so a page carrying
    both is read exactly as it always was. The scale is RYM's own
    worst-to-best 0.5-5, whose maximum is what the conversion needs (a 0.5 floor
    would only matter to a reader that rescales the bottom).
    """
    text = html or ""
    if not text:
        return None
    match = (_RYM_VALUE_RE.search(text) or _RYM_AVG_RE.search(text)
             or _RYM_SECTION_VALUE_RE.search(text))
    if not match:
        return None
    value = _to_int(match.group(1))
    if value is None:
        return None
    count = 0
    found = _RYM_COUNT_RE.search(text) or _RYM_SECTION_COUNT_RE.search(text)
    if found:
        count = int(found.group(1).replace(",", ""))
    return {"value": value, "count": count}


def parse_rateyourmusic(payload):
    """A RYM answer (or a release/song page's HTML) -> its community average.

    Two shapes are accepted because the routes that produce them differ: the
    app's own scrape answers with a parsed dict carrying ``{"rating": {"value",
    "count"}}`` (``_rym_album_answer`` for a release, ``rym_song_rating`` for a
    song), while a raw page body is parsed here. Both end on RYM's 0.5-5
    average, converted by the one scale helper.
    """
    if isinstance(payload, dict):
        rating = payload.get("rating")
        if not isinstance(rating, dict):
            return None
        pair = _rating_pair(rating.get("value"), rating.get("count"))
    elif isinstance(payload, str):
        parsed = rym_rating_from_html(payload)
        if not parsed:
            return None
        pair = _rating_pair(parsed.get("value"), parsed.get("count"))
    else:
        return None
    if pair is None:
        return None
    return pair[0], pair[1], SOURCE_LABELS["rateyourmusic"]


# Album of the Year: the album page's USER SCORE, 0-100 (AOTY's own scale, so
# no conversion — the identity case of `scale_to_100`). The page states it
# twice and both are needed: the anchor's `title` attribute is the exact
# average and its text is the rounded integer AOTY displays.
# VERIFIED against a real page (Neko Case, Middle Cyclone — the archived
# capture this project's AOTY reader replays):
#   <div class="albumUserScoreBox"><div class="heading">User Score</div>
#   <div class="albumUserScore"><a href="#users" title="76.7">77</a>…
#   <div class="text numReviews">Based on <a href="…/user-reviews/?type=ratings">
#   <strong>152</strong>&nbspratings</a></div>
# The critic score (`class="albumCriticScore"`, "Based on <strong>27</strong>
# reviews") is a DIFFERENT number and is deliberately never read: the owner
# asked for the USER score, and reading the critic anchor by mistake would
# write a number nobody asked for under a name that says "web rating".
_AOTY_USER_RE = re.compile(
    r'class="albumUserScore"[^>]*>\s*<a[^>]*title="([0-9]+(?:\.[0-9]+)?)"[^>]*>'
    r'\s*([0-9]+)\s*<', re.I)
# `&nbspratings` is AOTY's own unclosed entity (`&nbsp` + "ratings") — matched
# verbatim, because the safe-looking `&nbsp;ratings` is not what the page says.
_AOTY_COUNT_RE = re.compile(
    r'Based on\s*<a[^>]*>\s*<strong>([0-9,]+)</strong>\s*&nbspratings', re.I)


def parse_aoty(html):
    """An AOTY album page -> its user score, on the app's own scale.

    The score is already 0-100, so this is the one source whose conversion is
    the identity — it still goes through `_rating_pair` with an explicit
    ``scale_max=100``, because "which scale is this" belongs to the parser and
    a second conversion rule would be a second place to be wrong.

    Returns None when the page states no user score at all (a page whose
    ratings are hidden, or markup this parser does not know) — never a zero.
    """
    text = html or ""
    if not text:
        return None
    match = _AOTY_USER_RE.search(text)
    if not match:
        return None
    count = 0
    found = _AOTY_COUNT_RE.search(text)
    if found:
        count = int(found.group(1).replace(",", ""))
    pair = _rating_pair(match.group(1) or match.group(2), count,
                        RATING_MAX)
    if pair is None:
        return None
    return pair[0], pair[1], SOURCE_LABELS["albumoftheyear"]


def parse_discogs(payload):
    """A Discogs release detail -> its ``community.rating`` average.

    VERIFIED live (2026-10-01, release 1174296 — Radiohead, In Rainbows):
    ``"community": {"rating": {"count": 3809, "average": 4.72}}``. The scale
    is 0-5. ``count`` is the number of people who rated the release, which is
    also the release's weight. A release nobody rated carries
    ``{"count": 0, "average": 0.0}`` — an average of zero with no votes is
    "nobody rated this", and the value is dropped rather than written as 0.
    """
    if not isinstance(payload, dict):
        return None
    community = payload.get("community") if isinstance(payload.get("community"), dict) else None
    rating = (community or {}).get("rating")
    if not isinstance(rating, dict):
        return None
    count = _to_int(rating.get("count")) or 0
    if count <= 0:
        return None
    pair = _rating_pair(rating.get("average"), count)
    if pair is None:
        return None
    return pair[0], pair[1], SOURCE_LABELS["discogs"]


# The one dispatch: source id -> parser. A source absent from here cannot
# answer at all, so adding a source is one entry in SOURCES, one parser and one
# entry here.
SOURCE_PARSERS = {
    "rateyourmusic": parse_rateyourmusic,
    "musicbrainz": parse_musicbrainz,
    "albumoftheyear": parse_aoty,
    "discogs": parse_discogs,
}


def parse_source(source, payload, kind="album"):
    """One source's payload -> ``(value_0_100, weight, label)`` or None.

    The MUSICBRAINZ entry is level-aware: an album payload is a release-group
    node, a track payload is ``{"recording", "work"}`` (see
    ``parse_musicbrainz_track``). RATEYOURMUSIC needs no branch here: the
    fetcher already answers a track with the SONG page's own rating and an
    album with the release page's, and ``parse_rateyourmusic`` reads both
    shapes. Album of the Year and Discogs rate a release, so they answer at
    album level only.
    """
    parser = SOURCE_PARSERS.get(str(source or "").strip().lower())
    if parser is None or payload is None:
        return None
    if source == "musicbrainz" and kind == "track":
        parser = parse_musicbrainz_track
    try:
        return parser(payload)
    except Exception:
        # A parser is pure, but a payload is a stranger's data: a shape that
        # makes one of them raise is that source's miss and nobody else's.
        return None


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def aggregate(answers):
    """The weighted mean of the answers that came back, or None.

    ``answers`` are ``(value_0_100, weight, label)`` triples (or None, which
    each source contributes when it has nothing to say). The mean is over
    VOTES, not sources: a score 49 366 people voted on carries 49 366 times
    the weight of one with a single vote, which is the owner's rule and the
    only one that makes two hundreds-of-votes sources agree on a number
    instead of on an average of averages.

    Returns ``{"value": int, "sources": [label, ...]}`` — the labels in the
    order they were asked (the configured order), deduped — or None when no
    source answered.
    """
    kept = [a for a in (answers or []) if a and a[0] is not None]
    if not kept:
        return None
    weight = sum(_weight(a[1]) for a in kept)
    if weight <= 0:
        return None
    total = sum(float(a[0]) * _weight(a[1]) for a in kept)
    value = max(0, min(RATING_MAX, _round_half_up(total / weight)))
    labels = []
    for _value, _w, label in kept:
        text = str(label or "").strip()
        if text and text not in labels:
            labels.append(text)
    return {"value": value, "sources": labels}


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
def _rate(kind, ident, cfg, fetch):
    """``{"value", "sources"}`` for one entity, or None when nobody answered.

    *fetch* is the injected callable (see the module docstring). It is called
    for every source in the configured order and its payload parsed by the one
    table above. It is allowed to raise — this is where a caller's fetcher
    meets a stranger's network — and a raise is that source's miss, counted in
    ``answered``/``missed`` rather than thrown at the caller.
    """
    allowed = TRACK_SOURCES if kind == "track" else ALBUM_SOURCES
    order = [s for s in provider_order(cfg) if s in allowed]
    answers, answered = [], []
    for source in order:
        try:
            payload = fetch(source, kind, ident, cfg)
        except Exception:
            payload = None
        got = parse_source(source, payload, kind)
        if got is None:
            continue
        answers.append(got)
        answered.append(source)
    result = aggregate(answers)
    if result is None:
        return None
    result["asked"] = order
    result["answered"] = answered
    return result


def album_rating(artist, album, rg_mbid, cfg, fetch):
    """The ALBUM's aggregated web rating, or None.

    The identity is the release-group MBID when the file states one (an
    identity, never a title guess) and the artist/album names otherwise —
    which is all RYM and Discogs can search by.
    """
    return _rate("album", {"artist": str(artist or "").strip(),
                           "album": str(album or "").strip(),
                           "mbid": str(rg_mbid or "").strip()},
                 cfg, fetch)


def track_rating(artist, title, recording_mbid, cfg, fetch):
    """The TRACK's aggregated web rating, or None.

    Track-level sources only (MusicBrainz's recording rating and RYM's SONG
    page): a release-wide score is an ALBUM answer and is never dressed up as
    a track's (see the module docstring).
    """
    return _rate("track", {"artist": str(artist or "").strip(),
                           "title": str(title or "").strip(),
                           "mbid": str(recording_mbid or "").strip()},
                 cfg, fetch)


# --------------------------------------------------------------------------- #
# The writer — fill only, and say what it did
# --------------------------------------------------------------------------- #
def _existing(af, tag):
    try:
        return str(af.get_tag(tag) or "").strip()
    except Exception:
        return ""


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
    and every track answer of one album ride the SAME bounded pool, so a
    track's RYM wait no longer blocks the next track's MusicBrainz ask (the
    two hosts have separate 1 req/s locks; only the waits overlap, the request
    COUNT per host is unchanged).

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

        The overlap is the point: MusicBrainz (1 req/s) and RYM/Wayback
        (1 req/s) are separate hosts with separate locks, so a strictly serial
        track loop let one track's RYM wait block the next track's MB ask and
        vice versa. Per-host request COUNT is unchanged — still one ask per
        source per entity — only the WAITS overlap. Order is unchanged too:
        each answer is parsed and aggregated in the configured source order
        (see ``_rate``), and the writes below visit ``rows`` (sorted file
        names) in order, so a ThreadPool completion order can never reach a
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
            """
            if not force and row["web_rating"]:
                return None
            return track_rating(row["artist"] or row["albumartist"],
                                row["title"], row["rec_mbid"],
                                config, fetch)

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
