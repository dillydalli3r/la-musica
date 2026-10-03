#!/usr/bin/env python3
"""Web ratings (`mlo/web_ratings.py`, script 24) — the whole chain, OFFLINE.

What this pins, and what it refuses to guess:

  * the SCALE conversion: every source publishes on its own scale (MusicBrainz
    0-5, RateYourMusic 0.5-5 whose maximum is what matters, Discogs 0-5) and
    all of them land on the app's 0-100 Picard scale — the same one RATING
    uses — through ONE helper, with halves rounded UP (a 4.125/5 that read 82
    because the digit above it was even is not a rule anyone can predict from
    the page).
  * the PARSERS, against payloads SAVED FROM THE LIVE SOURCES (nothing here
    touches the network): the MusicBrainz rating node as
    `release-group/<mbid>?inc=ratings` really answers it, the recording node
    with its work fallback, the RateYourMusic release page's schema.org
    microdata (the exact markup of a real page), and Discogs' `community
    .rating`. A payload that states no rating — MusicBrainz's
    `{"value": null, "votes-count": 0}`, Discogs' `{"count": 0}` — is a MISS
    and contributes nothing, never a zero.
  * the AGGREGATION: a vote-count-weighted mean over the sources that answered,
    a source with no count counting once, and None when nobody answered.
  * the WRITER: fill-only. A value already on the file survives a second run
    byte for byte unless the run is forced, and a switched-off family writes
    nothing. Proven on a REAL FLAC (tags read back off disk with mutagen) and
    on the runner itself.
  * the RUNNER (script 24): album-scoped, one album answer written to every
    track of the folder, a track whose own sources said nothing keeping
    ALBUMWEBRATING and no WEBRATING — nothing is invented from an album value.
    Its network is an INJECTED fetcher, so this suite can prove the whole run
    without a socket.

Run:  python tools/test_web_ratings.py   (exit 0 = pass, 1 = failure, 2 = skip)
"""
import os
import shutil
import subprocess
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlo import web_ratings as wr                                     # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLAC_EXE = None
_deps = os.path.join(ROOT, ".dependencies")
if os.path.isdir(_deps):
    for entry in os.listdir(_deps):
        if entry.lower().startswith("flac"):
            cand = os.path.join(_deps, entry, "flac.exe")
            if os.path.isfile(cand):
                FLAC_EXE = cand
                break
if FLAC_EXE is None:
    FLAC_EXE = shutil.which("flac") or None

passed = 0
failed = 0


def ok(cond, label):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}")


def eq(got, want, label):
    ok(got == want, f"{label}  (got {got!r}, want {want!r})")


# --------------------------------------------------------------------------- #
# The fixtures — payloads SAVED from the live sources (2026-10-01)
# --------------------------------------------------------------------------- #
# MusicBrainz, `release-group/f5093c06-…?inc=ratings` (The Dark Side of the
# Moon) — the album-level shape the owner asked for, trimmed to what a parser
# reads.
MB_ALBUM = {"id": "f5093c06-23e3-404f-aeaa-40f72885ee3a",
            "title": "The Dark Side of the Moon",
            "rating": {"value": 4.75, "votes-count": 105},
            "genres": [{"name": "art rock"}, {"name": "progressive rock"}]}
# `recording/…?inc=ratings+genres+work-rels`, two real tracks: one the
# recording itself is rated for, one where only the WORK is.
MB_TRACK_OWN = {"recording": {"rating": {"value": 4.2, "votes-count": 18}},
                "work": {"rating": {"value": None, "votes-count": 0}}}
MB_TRACK_WORK_ONLY = {"recording": {"rating": {"value": None, "votes-count": 0}},
                      "work": {"rating": {"value": 5, "votes-count": 1}}}
# What an entity nobody rated answers — a MISS, not a zero.
MB_UNRATED = {"rating": {"value": None, "votes-count": 0}}

# RateYourMusic: the byte-for-byte microdata of a real release page (In
# Rainbows, from the archived capture this project's RYM ladder reads).
RYM_HTML = (
    '<div class="release_page" itemprop="mainEntity" itemscope '
    'itemtype="http://schema.org/MusicAlbum">\n'
    '     <div itemprop="aggregateRating" itemscope '
    'itemtype="http://schema.org/AggregateRating">\n'
    '     <meta itemprop="ratingValue" content="4.18" />\n'
    '     <meta itemprop="bestRating" content="5.0" />\n'
    '     <meta itemprop="worstRating" content="0.5" />\n'
    '     <meta itemprop="ratingCount" content="49366" /><meta '
    'itemprop="reviewCount" content="755" /></div>\n'
    '   <meta content="In Rainbows" itemprop="name" />\n'
)
# …and the visual twin, for a page whose microdata is missing.
RYM_HTML_AVG_ONLY = ('<th class="info_hdr">RYM Rating</td><td colspan="2">'
                     '<span class="avg_rating" >\n 3.77\n </span>'
                     '<span class="max_rating">/ <span>5.0</span>')

# RYM's 2026 design: the same two numbers with NO microdata at all, in the
# component the current pages serve. This is the block read out of the archived
# SONG page of Radiohead's "Paranoid Android"
# (web.archive.org/web/20260814042836id_/…/song/radiohead/paranoid-android/),
# where the star image's `alt`/`class` attributes sit between the value's
# opening tag and the number itself.
RYM_SONG_HTML = (
    '<div class="page_section_main_info_music_rating_main">\n'
    '   <div data-tiptip="&lt;b&gt;Bolded&lt;/b&gt;: In the top 10,000 songs '
    'of all time."\n'
    '      class="page_section_main_info_music_rating_value has_tip">\n'
    '      <div class="page_section_main_info_music_rating_value_rating">\n'
    '          <img alt="rating bolded" alt="bold star" '
    'class="metadata-star-bold" /> 4.67\n'
    '         \n'
    '      </div>\n'
    '      <div class="page_section_main_info_music_rating_value_number">\n'
    '         17,654 \n'
    '         \n'
    '         ratings\n'
    '      </div>\n'
    '   </div>\n'
    '</div>\n'
)
# The same component on a RELEASE page — the album aggregate a 2026 capture
# carries instead of the microdata above.
RYM_ALBUM_HTML_NEW = (
    '<div class="page_section_main_info_music_rating_main">\n'
    '   <div class="page_section_main_info_music_rating_value has_tip">\n'
    '      <div class="page_section_main_info_music_rating_value_rating">\n'
    '          <img alt="rating bolded" class="metadata-star-bold" /> 4.23\n'
    '      </div>\n'
    '      <div class="page_section_main_info_music_rating_value_number">\n'
    '         66,965\n'
    '         ratings\n'
    '      </div>\n'
    '   </div>\n'
    '</div>\n'
)

# Whole SONG pages, as the identity check sees them: the page's own `<title>`
# (what it says it IS), the rating component, and the album track-list widget a
# real page carries. That widget names EVERY song of the record — and the
# archived Paranoid Android page names "Let Down" in exactly this way — which
# is why the check reads the page's own title, not the page's whole text.
RYM_TRACKLIST_WIDGET = (
    '<ul class="tracklist">\n'
    '<li><div class="tracklist_line"><span class="tracklist_num">2</span>'
    '<span class="tracklist_title"><a class="song bolded" '
    'href="/song/radiohead/paranoid-android/"><span class="rendered_text">'
    'Paranoid Android</span></a></span></div></li>\n'
    '<li><div class="tracklist_line"><span class="tracklist_num">5</span>'
    '<span class="tracklist_title"><a class="song" '
    'href="/song/radiohead/let-down/"><span class="rendered_text">Let Down'
    '</span></a></span></div></li>\n'
    '</ul>\n')
# A different song by the SAME artist, naming the asked title in its own
# track list — the shape a bare-slug hit serves when RYM's entry for the asked
# title carries a `-1` suffix.
RYM_SONG_PAGE_OTHER = (
    '<title>Radiohead - Let Down - Lyrics and ratings - Rate Your Music</title>'
    '\n<a href="/artist/radiohead">Radiohead</a>\n'
    + RYM_TRACKLIST_WIDGET +
    '<div class="page_section_main_info_music_rating_value_rating">\n'
    '   4.42\n</div>\n'
    '<div class="page_section_main_info_music_rating_value_number">\n'
    '   9,001 ratings\n</div>\n')
RYM_SONG_PAGE = ('<title>Radiohead - Paranoid Android - Lyrics and ratings - '
                 'Rate Your Music</title>\n'
                 '<a href="/artist/radiohead">Radiohead</a>\n'
                 + RYM_TRACKLIST_WIDGET + RYM_SONG_HTML)
RYM_SONG_PAGE_SHA = (
    '<title>Radiohead - Subterranean Homesick Alien - Lyrics and ratings - '
    'Rate Your Music</title>\n'
    '<a href="/artist/radiohead">Radiohead</a>\n' + RYM_SONG_HTML)
# A page that IS the song but states no community average yet.
RYM_SONG_PAGE_UNRATED = ('<title>Radiohead - Paranoid Android - Lyrics and '
                         'ratings - Rate Your Music</title>\n'
                         '<a href="/artist/radiohead">Radiohead</a>\n')

# Discogs, `releases/1174296` (Radiohead, In Rainbows) — the live document.
DISCOGS = {"id": 1174296, "title": "In Rainbows",
           "genres": ["Electronic", "Rock"],
           "styles": ["Alternative Rock", "Art Rock"],
           "community": {"have": 38869, "want": 19012,
                         "rating": {"count": 3809, "average": 4.72}}}
DISCOGS_UNRATED = {"community": {"rating": {"count": 0, "average": 0.0}}}


# Album of the Year, the album page of Neko Case's Middle Cyclone — the exact
# markup of a real capture (the score box, and the Details row that carries the
# genre anchors). The page states the user score twice: the exact average in
# the anchor's `title`, the rounded integer a visitor sees as its text.
AOTY_HTML = (
    '<div class="albumTopBox"><div class="albumCriticScoreBox">'
    '<div class="heading">Critic Score</div>'
    '<div class="albumCriticScore"><a href="#critics" title="78.0537">78</a>'
    '<div class="ratingBar green"><div class="green" style="width:78%;">'
    '</div></div></div><div class="text numReviews">Based on <strong>27'
    '</strong> reviews</div></div><div class="albumUserScoreBox">'
    '<div class="heading">User Score</div>'
    '<div class="albumUserScore"><a href="#users" title="76.7">77</a>'
    '<div class="ratingBar green"><div class="green" style="width:77%;">'
    '</div></div></div><div class="text numReviews">Based on <a href='
    '"/album/1-neko-case-middle-cyclone/user-reviews/?type=ratings">'
    '<strong>152</strong>&nbspratings</a></div></div>'
    '<div class="albumTopBox info"><div class="sectionHeading">'
    '<h2>Details</h2></div>'
    '<div class="detailRow"><a href="/genre/17-alt-country/">Alt-Country</a>, '
    '<a href="/genre/37-singer-songwriter/">Singer-Songwriter</a><br />'
    '<a href="/genre/70-americana/"><div class="secondary">Americana</div></a>'
    ', <a href="/genre/301-field-recordings/"><div class="secondary">'
    'Field Recordings</div></a> <span>/&nbsp;Genre</span></div></div>'
)
# The same page with the user score hidden — the critic number must NOT be
# picked up as the answer.
AOTY_CRITICS_ONLY = (
    '<div class="albumCriticScore"><a href="#critics" title="78.0537">78</a>'
    '</div><div class="text numReviews">Based on <strong>27</strong> reviews'
    '</div>'
)

print("== scale conversion ==")
# The one conversion, on every scale a source publishes. 4.18/5 = 83.6 -> 84;
# 4.75/5 = 95; 2.5/5 = 50; 4.125/5 = 82.5 -> 83 (halves UP, never to even).
eq(wr.scale_to_100(5), 100, "5/5 is 100")
eq(wr.scale_to_100(0), 0, "0/5 is 0")
eq(wr.scale_to_100(4.35), 87, "4.35/5 is 87")
eq(wr.scale_to_100(4.18), 84, "4.18/5 is 84")
eq(wr.scale_to_100(2.5), 50, "2.5/5 is 50")
eq(wr.scale_to_100(4.125), 83, "a half rounds UP (82.5 -> 83, never 82)")
eq(wr.scale_to_100(5.5), 100, "above the scale clamps to 100, it is not dropped")
eq(wr.scale_to_100(None), None, "no value is no conversion")
eq(wr.scale_to_100("nonsense"), None, "a non-number is no conversion")
eq(wr.scale_to_100(82, 100), 82, "the 0-100 scale passes through unchanged")

print("== the per-source parsers (saved payloads, no network) ==")
eq(wr.parse_musicbrainz(MB_ALBUM), (95, 105, "MusicBrainz"),
   "MusicBrainz album: 4.75/5 with 105 votes")
eq(wr.parse_musicbrainz(MB_UNRATED), None,
   "an unrated MusicBrainz entity is a MISS, not a zero")
eq(wr.parse_musicbrainz({"rating": {"value": 4.2, "count": 18}}),
   (84, 18, "MusicBrainz"), "the older `count` spelling is read too")
eq(wr.parse_musicbrainz_track(MB_TRACK_OWN), (84, 18, "MusicBrainz"),
   "a track uses the RECORDING's own rating")
eq(wr.parse_musicbrainz_track(MB_TRACK_WORK_ONLY), (100, 1, "MusicBrainz"),
   "…and the WORK's only when the recording states none")
eq(wr.parse_musicbrainz_track({"recording": MB_UNRATED, "work": None}), None,
   "neither rated is a miss")

eq(wr.rym_rating_from_html(RYM_HTML), {"value": 4.18, "count": 49366},
   "the RYM page's schema.org microdata parses")
eq(wr.rym_rating_from_html(RYM_HTML_AVG_ONLY), {"value": 3.77, "count": 0},
   "…and the visual avg_rating is the fallback, with no count stated")
eq(wr.rym_rating_from_html("<html><body>nothing</body></html>"), None,
   "a page that states no rating parses to None")
eq(wr.parse_rateyourmusic(RYM_HTML), (84, 49366, "RateYourMusic"),
   "the RYM page HTML -> 84, weighted by its 49 366 votes")
eq(wr.parse_rateyourmusic({"rating": {"value": 4.18, "count": 49366}}),
   (84, 49366, "RateYourMusic"),
   "…and the parsed answer the genre ladder hands over gives the same number")
eq(wr.parse_rateyourmusic({"genres": ["art rock"]}), None,
   "an answer with no rating sub-dict is a miss")

# The 2026 design — no microdata at all, the value and its count in RYM's own
# component. Both levels are this same shape.
eq(wr.rym_rating_from_html(RYM_SONG_HTML), {"value": 4.67, "count": 17654},
   "the 2026 SONG markup parses to its value and its ratings count")
eq(wr.rym_rating_from_html(RYM_ALBUM_HTML_NEW), {"value": 4.23, "count": 66965},
   "…and the same component on a release page (the album aggregate)")
eq(wr.rym_rating_from_html(RYM_HTML + RYM_SONG_HTML),
   {"value": 4.18, "count": 49366},
   "the schema.org microdata still wins when a page carries both designs")
eq(wr.rym_rating_from_html('<div class="page_section_main_info_music_rating_'
                           'value_rating"> 4.50 </div>'),
   {"value": 4.5, "count": 0},
   "a component with no count is a value with no votes, never a miss")
eq(wr.parse_rateyourmusic(RYM_SONG_HTML), (93, 17654, "RateYourMusic"),
   "a song page -> 93, weighted by its own 17 654 votes")
eq(wr.parse_rateyourmusic({"rating": {"value": 4.67, "count": 17654}}),
   (93, 17654, "RateYourMusic"),
   "…and the song answer the fetcher hands over reads identically")
eq(wr.parse_source("rateyourmusic", RYM_SONG_HTML, "track"),
   (93, 17654, "RateYourMusic"),
   "the one dispatch reads a TRACK's RYM payload (no level branch needed)")
ok("rateyourmusic" in wr.TRACK_SOURCES, "RateYourMusic is a track-level source")

eq(wr.parse_discogs(DISCOGS), (94, 3809, "Discogs"),
   "Discogs: 4.72/5 over 3 809 votes")
eq(wr.parse_discogs(DISCOGS_UNRATED), None,
   "a Discogs release nobody rated is a miss, never a 0")
eq(wr.parse_aoty(AOTY_HTML), (77, 152, "Album of the Year"),
   "AOTY: the user score box, 0-100 as published, weighted by its ratings")
eq(wr.parse_aoty(AOTY_CRITICS_ONLY), None,
   "…and the CRITIC score is never read as the user score")
eq(wr.parse_aoty(""), None, "an empty page is a miss")
eq(wr.parse_source("musicbrainz", MB_ALBUM, "album"), (95, 105, "MusicBrainz"),
   "the one dispatch reads a MusicBrainz album payload")
eq(wr.parse_source("musicbrainz", MB_TRACK_OWN, "track"), (84, 18, "MusicBrainz"),
   "…and is level-aware for a track")
eq(wr.parse_source("albumoftheyear", AOTY_HTML, "album"),
   (77, 152, "Album of the Year"),
   "…and reads an AOTY page through the same dispatch")
eq(wr.parse_source("albumoftheyear", "2024-06: not an album page", "album"), None,
   "a payload with no user score contributes nothing")

# The AOTY GENRE side is the same page parsed by the genre provider layer, so
# its own parser is pinned to the same saved markup.
from server import integrations as intg                                  # noqa: E402
eq(intg.aoty_genres_from_html(AOTY_HTML),
   ["Alt-Country", "Singer-Songwriter", "Americana", "Field Recordings"],
   "the AOTY Details row's genre anchors, headline names first")
eq(intg.aoty_genres_from_html("<html><body>no genres here</body></html>"), [],
   "…and a page without them yields no names")
eq(intg.aoty_genres_from_html(""), [], "…and neither does an empty page")

print("== the RYM song source (slug, identity check, fallback order) ==")
# The ONE slug generator both segments use, on real RYM song titles. Each
# spelling below was read off rateyourmusic.com: apostrophes are DELETED
# ("Lady Godiva's" → `lady-godivas-…`, "Ain't" → `aint-…`) and `&` becomes
# "and" ("Earth, Wind & Fire" → `earth-wind-and-fire`) — as on the artist and
# release pages — while every other run of punctuation/space collapses to ONE
# dash. This is the spelling a /song/ URL carries; a wrong guess is a MISS,
# never a wrong answer (the page is checked before it is read).
eq(intg._rym_slug("Paranoid Android"), "paranoid-android", "a plain title")
eq(intg._rym_slug("Subterranean Homesick Alien"),
   "subterranean-homesick-alien", "…a long one (RYM's page adds the `-1`)")
eq(intg._rym_slug("Exit Music (For a Film)"), "exit-music-for-a-film",
   "parentheses collapse together with the spaces around them")
eq(intg._rym_slug("Lady Godiva's Operation"), "lady-godivas-operation",
   "an apostrophe is DELETED, not turned into a dash")
eq(intg._rym_slug("Ain't It Funny"), "aint-it-funny", "…and again")
eq(intg._rym_slug("Sweet / I Thought You Wanted to Dance"),
   "sweet-i-thought-you-wanted-to-dance",
   "a slash collapses into the run around it, not into two dashes")
eq(intg._rym_slug("Rock & Roll"), "rock-and-roll", "`&` is RYM's own `and`")
eq(intg._rym_slug("Björk — Jóga"), "bjork-joga",
   "diacritics fold and an em dash leaves no stray dash")

eq(intg._rym_song_paths("Radiohead", "Paranoid Android"),
   ["/song/radiohead/paranoid-android/",
    "/song/radiohead/paranoid-android-1/"],
   "the ladder is the bare slug, then ONE numbered spelling — no `-2`")
eq(intg.RYM_SONG_TRIES, 2,
   "…and that bound is the module's own, deliberately small budget")
eq(intg._rym_song_paths("Earth, Wind & Fire", "Kalimba Story"),
   ["/song/earth-wind-and-fire/kalimba-story/",
    "/song/earth-wind-and-fire/kalimba-story-1/"],
   "the artist segment is the artist-page spelling")
eq(intg._rym_song_paths("", "Paranoid Android"), [],
   "no artist = no path, so no request is ever made")

# The identity check reads the page's OWN `<title>`, never the whole page: a
# song page's track-list widget names every song on the record (the archived
# "Paranoid Android" page names "Let Down", and vice versa), so a whole-text
# check would accept a sibling track's page.
eq(intg._rym_song_matches(RYM_SONG_PAGE, "Radiohead", "Paranoid Android"), True,
   "the page's own title is the song that was asked for")
eq(intg._rym_song_matches(RYM_SONG_PAGE, "Radiohead", "Let Down"), False,
   "…and a title the page only LISTS is not accepted as its own")
eq(intg._rym_song_matches(RYM_SONG_PAGE_OTHER, "Radiohead", "Paranoid Android"),
   False,
   "a sibling track's page is a miss, even though its track list names ours")
eq(intg._rym_song_matches("<html><body>no title</body></html>", "A", "B"), False,
   "a page with no <title> at all is never verified")

# The live route, with the module's own GET replaced by saved pages: the whole
# ladder runs — the identity check included — with no socket. `_served`
# records what it would have fetched, in order.
_ORIG_RYM_GET = intg._rym_get
_served = {"pages": {}, "asked": [], "default": None}


def _fake_rym_get(path, params=None, cfg=None, expect=None):
    _served["asked"].append(path)
    return _served["pages"].get(path, _served["default"])


def song_from(pages, artist="Radiohead", title="Paranoid Android", default=None):
    """`rym_song_rating` over saved pages, with the live site's GET replaced."""
    _served.update(pages=pages, asked=[], default=default)
    intg._rym_get = _fake_rym_get
    try:
        return intg.rym_song_rating(artist, title,
                                    {"rym_archive_fallback": False})
    finally:
        intg._rym_get = _ORIG_RYM_GET


eq(song_from({"/song/radiohead/paranoid-android/": RYM_SONG_PAGE}),
   {"rating": {"value": 4.67, "count": 17654}},
   "a VERIFIED song page answers with its own value and count")
eq(_served["asked"], ["/song/radiohead/paranoid-android/"],
   "…and the walk stops at the FIRST hit (no -1/-2 request is spent)")

eq(song_from({}, default=RYM_SONG_PAGE_OTHER), None,
   "a page that IS another song is a MISS, never its score")
eq(_served["asked"],
   ["/song/radiohead/paranoid-android/",
    "/song/radiohead/paranoid-android-1/"],
   "…after every slug was tried (two, the whole budget, then it gives up)")

eq(song_from({"/song/radiohead/subterranean-homesick-alien/":
              RYM_SONG_PAGE_OTHER,
              "/song/radiohead/subterranean-homesick-alien-1/":
              RYM_SONG_PAGE_SHA},
             title="Subterranean Homesick Alien"),
   {"rating": {"value": 4.67, "count": 17654}},
   "a bare slug serving another song falls through to the `-1` page")
eq(_served["asked"],
   ["/song/radiohead/subterranean-homesick-alien/",
    "/song/radiohead/subterranean-homesick-alien-1/"],
   "…and stops there — the `-2` candidate is never asked")

eq(song_from({"/song/radiohead/paranoid-android/": RYM_SONG_PAGE_UNRATED}), None,
   "the right page with no community average is a miss, never a zero")
eq(_served["asked"], ["/song/radiohead/paranoid-android/"],
   "…and it is still the first VERIFIED hit, so the walk stops")

eq(song_from({}, artist="", title="Paranoid Android"), None,
   "a track with no artist is a miss")
eq(_served["asked"], [], "…with ZERO requests")

# With the archive permitted and no `rym_cookie`, the live site is not asked at
# all — the snapshot answers, under the same identity check. The REAL archive
# leg runs (`_rym_archive_newest` included); only the two seams are replaced,
# so this process reads and writes nothing outside itself.
_ORIG_FETCH = intg._rym_archive_fetch
_ORIG_READ, _ORIG_WRITE = intg._rym_cache_read, intg._rym_cache_write
_arch_urls = []
_served.update(pages={}, asked=[], default=None)
intg._rym_get = _fake_rym_get
intg._rym_cache_read = lambda key, ttl: None
intg._rym_cache_write = lambda key, text: None


def _fake_archive_fetch(url, params=None):
    """One Wayback request: the bare slug IS this song, nothing else is."""
    _arch_urls.append(url)
    if url.endswith("/song/radiohead/paranoid-android/"):
        return RYM_SONG_PAGE, url.replace("/2id_/", "/20260814042836id_/"), True
    return None, url, True


intg._rym_archive_fetch = _fake_archive_fetch
try:
    archived = intg.rym_song_rating("Radiohead", "Paranoid Android",
                                    {"rym_archive_fallback": True})
finally:
    intg._rym_archive_fetch = _ORIG_FETCH
    intg._rym_cache_read, intg._rym_cache_write = _ORIG_READ, _ORIG_WRITE
    intg._rym_get = _ORIG_RYM_GET
eq(archived, {"rating": {"value": 4.67, "count": 17654}},
   "with no cookie the archived copy of the song page answers")
eq(_served["asked"], [], "…and the live site is never asked")
eq(_arch_urls, ["https://web.archive.org/web/2id_/https://rateyourmusic.com"
                "/song/radiohead/paranoid-android/"],
   "…ONE newest-capture request, for the bare slug")

# A song RYM has no capture for is the common case on an album nobody rates
# per track, so its cost is bounded deliberately: the two candidates, ONE
# newest-capture request each — never the release readers' indexed-capture
# ladder (the bare `2id_` URLs below are what pins that).
_arch_urls = []
intg._rym_archive_fetch = _fake_archive_fetch
intg._rym_cache_read = lambda key, ttl: None
intg._rym_cache_write = lambda key, text: None
try:
    missing = intg.rym_song_rating("Radiohead", "Airbag",
                                   {"rym_archive_fallback": True})
finally:
    intg._rym_archive_fetch = _ORIG_FETCH
    intg._rym_cache_read, intg._rym_cache_write = _ORIG_READ, _ORIG_WRITE
eq(missing, None, "a song the archive holds no capture of is a miss")
eq(_arch_urls, ["https://web.archive.org/web/2id_/https://rateyourmusic.com"
                "/song/radiohead/airbag/",
                "https://web.archive.org/web/2id_/https://rateyourmusic.com"
                "/song/radiohead/airbag-1/"],
   "…costing the bare slug and ONE numbered spelling, newest capture only")

print("== a track's aggregate over RYM and MusicBrainz ==")


def _rym_mb_fetch(source, kind, ident, cfg):
    """Two track-level sources, no socket: RYM's song page, MB's recording."""
    if source == "rateyourmusic":
        return {"rating": {"value": 4.67, "count": 17654}}   # the song page
    if source == "musicbrainz":
        return MB_TRACK_OWN                                  # 4.2/5, 18 votes
    return None


track = wr.track_rating("Radiohead", "Paranoid Android", "rec-mbid", {},
                        _rym_mb_fetch)
eq(track["sources"], ["RateYourMusic", "MusicBrainz"],
   "a track's WEBRATING_SOURCE can now read RateYourMusic; MusicBrainz")
eq(track["value"], 93,
   "…and the vote-weighted mean is RYM's own 93 (17 654 votes vs 18)")

print("== aggregation ==")
# 87 and 84 over 31 and 49 366 votes: the big one decides the number.
agg = wr.aggregate([(87, 31, "MusicBrainz"), (84, 49366, "RateYourMusic")])
eq(agg["value"], 84, "the mean is weighted by votes, not by source count")
eq(agg["sources"], ["MusicBrainz", "RateYourMusic"],
   "…and the contributing sources are named, in the order asked")
eq(wr.aggregate([(90, None, "A"), (80, "", "B")])["value"], 85,
   "a source with no count counts exactly ONCE (90+80)/2")
eq(wr.aggregate([(50, 1, "A"), (51, 1, "B")])["value"], 51,
   "a .5 tie rounds UP, so any two reads of the same answers agree")
eq(wr.aggregate([None, None]), None, "no answers at all aggregates to None")
eq(wr.aggregate([]), None, "…and so does an empty list")
eq(wr.aggregate([(120, 1, "A"), (200, 1, "B")])["value"], 100,
   "the mean is clamped into 0-100")

print("== the source order ==")
eq(wr.provider_order({}), [s for s in wr.SOURCES if s in wr.SOURCE_PARSERS],
   "no saved list = the built-in rank (minus any source without a parser)")
eq(wr.provider_order({"web_ratings_sources": ["discogs", "musicbrainz"]}),
   ["discogs", "musicbrainz"], "a saved order wins, as written")
eq(wr.provider_order({"web_ratings_sources": ["discogs", "bogus", "musicbrainz"]}),
   ["discogs", "musicbrainz"], "an unknown id is dropped, not fatal")
eq(wr.provider_order({"web_ratings_sources": ["bogus"]}),
   [s for s in wr.SOURCES if s in wr.SOURCE_PARSERS],
   "a list naming nothing real falls back to the built-in rank")

print("== the writer (fill only) ==")
if FLAC_EXE is None:
    print("  skip: flac.exe not found under .dependencies or on PATH")
else:
    from mutagen.flac import FLAC

    def make_flac(path):
        wav = path + ".wav"
        with wave.open(wav, "w") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(b"\x00\x00\x00\x00" * 4410)
        subprocess.run([FLAC_EXE, "-s", "-f", "-8", "-o", path, wav],
                       check=True, capture_output=True)
        os.remove(wav)

    tmp = tempfile.mkdtemp(prefix="mlo_web_ratings_")
    track = os.path.join(tmp, "01 Song.flac")
    make_flac(track)
    f = FLAC(track)
    f["TITLE"] = ["Song"]
    f["ARTIST"] = ["Artist"]
    f["ALBUM"] = ["Album"]
    f.save()

    ALBUM_ANSWER = {"value": 90, "sources": ["MusicBrainz", "RateYourMusic"]}
    TRACK_ANSWER = {"value": 80, "sources": ["MusicBrainz"]}

    res = wr.write_ratings(track, album=ALBUM_ANSWER, track=TRACK_ANSWER,
                           cfg={})
    ok(res["wrote"].get("WEBRATING") == 80, "WEBRATING is written")
    ok(res["wrote"].get("ALBUMWEBRATING") == 90, "ALBUMWEBRATING is written")
    ok(res["wrote"].get("WEBRATING_SOURCE") == "MusicBrainz",
       "the track's sources are written beside it")
    ok(res["wrote"].get("ALBUMWEBRATING_SOURCE") == "MusicBrainz; RateYourMusic",
       "the album's sources are \"; \"-joined")
    f = FLAC(track)
    eq(f["WEBRATING"], ["80"], "the value is on disk as the integer")
    eq(f["ALBUMWEBRATING"], ["90"], "…and so is the album value")
    eq(f["ALBUMWEBRATING_SOURCE"], ["MusicBrainz; RateYourMusic"],
       "…and its joined provenance")

    # A SECOND run must not touch a value the file already holds.
    res = wr.write_ratings(track, album={"value": 10, "sources": ["Discogs"]},
                           track={"value": 10, "sources": ["Discogs"]},
                           cfg={})
    ok(not res["wrote"], "a second run writes nothing")
    eq(res["skipped"].get("WEBRATING"), "already set",
       "…and says the value was already set")
    f = FLAC(track)
    eq(f["WEBRATING"], ["80"], "the existing WEBRATING is untouched")
    eq(f["ALBUMWEBRATING"], ["90"], "the existing ALBUMWEBRATING is untouched")
    eq(f["ALBUMWEBRATING_SOURCE"], ["MusicBrainz; RateYourMusic"],
       "the provenance that belongs to the kept value is untouched too")

    # Forced, the same call replaces them.
    res = wr.write_ratings(track, album={"value": 10, "sources": ["Discogs"]},
                           track={"value": 10, "sources": ["Discogs"]},
                           cfg={}, force=True)
    eq(res["wrote"].get("WEBRATING"), 10, "a forced run replaces the value")
    f = FLAC(track)
    eq(f["WEBRATING"], ["10"], "…on disk")
    eq(f["WEBRATING_SOURCE"], ["Discogs"], "…and its provenance")

    # The feature switch is the family's master switch, and it really gates.
    res = wr.write_ratings(track, album=ALBUM_ANSWER, track=TRACK_ANSWER,
                           cfg={"web_ratings_enabled": False}, force=True)
    ok(not res["wrote"], "with web_ratings_enabled off nothing is written")
    eq(res["skipped"].get("WEBRATING"), "write gate is off",
       "…and the skip says the gate, not 'no answer'")
    f = FLAC(track)
    eq(f["WEBRATING"], ["10"], "the file is left exactly as it was")

    # No answer is not an erasure: a missing track value writes nothing.
    res = wr.write_ratings(track, album=None, track=None, cfg={})
    eq(res["skipped"].get("WEBRATING"), "no answer",
       "an entity no source answered for is reported as no answer")

    print("== the runner (script 24), with an injected fetcher ==")

    def make_album(folder, tracks):
        os.makedirs(folder, exist_ok=True)
        for i, (title, rec_mbid) in enumerate(tracks, 1):
            path = os.path.join(folder, f"{i:02d} {title}.flac")
            make_flac(path)
            g = FLAC(path)
            g["TITLE"] = [title]
            g["ARTIST"] = ["Artist"]
            g["ALBUMARTIST"] = ["Artist"]
            g["ALBUM"] = [folder_name]
            g["MUSICBRAINZ_RELEASEGROUPID"] = [rg]
            if rec_mbid:
                g["MUSICBRAINZ_TRACKID"] = [rec_mbid]
            g.save()
        return folder

    calls = []

    def fake_fetch(source, kind, ident, cfg):
        """A fetcher with the app's contract and NO socket."""
        calls.append((source, kind, dict(ident)))
        if source != "musicbrainz" or not ident.get("mbid"):
            return None
        if kind == "track":
            return MB_TRACK_OWN
        return MB_ALBUM

    library = tempfile.mkdtemp(prefix="mlo_web_ratings_lib_")
    rg = "f5093c06-23e3-404f-aeaa-40f72885ee3a"
    folder_name = "The Dark Side of the Moon"
    album_dir = make_album(os.path.join(library, "Pink Floyd", folder_name),
                           [("Speak to Me", "bef3fddb-5aca-49f5-b2fd-d56a23268d63"),
                            ("Breathe", None)])
    # A second album, so the per-album identity is really per album.
    rg2 = "11111111-2222-3333-4444-555555555555"
    folder_name = "Another Album"
    rg = rg2
    album_dir2 = make_album(os.path.join(library, "Artist", folder_name),
                            [("Only Song", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")])

    stats = wr.run_web_ratings({"targets": [album_dir, album_dir2],
                                "web_ratings_sources": ["musicbrainz"],
                                "worker_limit": 2},
                               fetch=fake_fetch)
    ok(stats["album_count"] == 2, "both albums got an album rating")
    ok(stats["modified_count"] == 3, "all three tracks were written")
    ok(stats["error_count"] == 0, f"no errors (got {stats['errors']})")
    # `by_source` counts the TRACKS whose ALBUM answer a source contributed to
    # (the number the log prints beside "tracks written"), not the albums: the
    # album answer is fetched once and lands on every track, so "MusicBrainz: 3"
    # reads as "three tracks carry a MusicBrainz-sourced score".
    eq(stats["by_source"], {"musicbrainz": 3},
       "the per-source count is the tracks it answered for")

    f = FLAC(os.path.join(album_dir, "01 Speak to Me.flac"))
    eq(f["ALBUMWEBRATING"], ["95"], "the album's own score lands on the track")
    eq(f["ALBUMWEBRATING_SOURCE"], ["MusicBrainz"], "…with its provenance")
    eq(f["WEBRATING"], ["84"], "…and the track's own score beside it")

    f = FLAC(os.path.join(album_dir, "02 Breathe.flac"))
    eq(f["ALBUMWEBRATING"], ["95"],
       "a track whose OWN sources said nothing still carries the album's")
    ok("WEBRATING" not in f, "…and gets NO WEBRATING — nothing is invented")
    ok("WEBRATING_SOURCE" not in f, "…and no provenance for a value it lacks")

    # A second run over the same library changes nothing (fill-only, end to
    # end), and a source that never answers costs nothing.
    before = {}
    for root, _dirs, files in os.walk(library):
        for name in files:
            if name.endswith(".flac"):
                before[os.path.join(root, name)] = os.path.getmtime(
                    os.path.join(root, name))
    stats2 = wr.run_web_ratings({"targets": [album_dir, album_dir2],
                                 "web_ratings_sources": ["musicbrainz"],
                                 "worker_limit": 2}, fetch=fake_fetch)
    eq(stats2["modified_count"], 0, "a second run writes nothing at all")
    eq(stats2["skipped_count"], 3, "…and books every track as skipped")
    for path, stamp in before.items():
        eq(os.path.getmtime(path), stamp, f"{os.path.basename(path)} untouched")

    # …and a run that does not list rateyourmusic never asks it: the track
    # source is opt-in through `web_ratings_sources`, exactly like every other.
    ok(not any(src == "rateyourmusic" for src, _k, _i in calls),
       "a run without rateyourmusic in web_ratings_sources asks it ZERO times")

    # THE FETCHES OBEY THE WRITES' OWN FILL-ONLY RULE. A second run over an
    # album whose tags are already on disk costs ZERO requests and still books
    # the album and its tracks; a forced run asks again.
    print("== a second run costs no requests (fill-only fetches) ==")
    fill_base = tempfile.mkdtemp(prefix="mlo_web_ratings_fill_")
    folder_name = "Rated Album"
    rg = "99999999-8888-7777-6666-555555555555"
    fill_dir = make_album(os.path.join(fill_base, "Artist", folder_name),
                          [("One", "rec-one"), ("Two", "rec-two")])
    fetch_log = []

    def tracking_fetch(source, kind, ident, cfg):
        fetch_log.append((source, kind))
        if source != "musicbrainz" or not ident.get("mbid"):
            return None
        return MB_ALBUM if kind == "album" else MB_TRACK_OWN

    fill_cfg = {"targets": [fill_dir], "web_ratings_sources": ["musicbrainz"],
                "worker_limit": 2}
    first = wr.run_web_ratings(fill_cfg, fetch=tracking_fetch)
    eq(first["modified_count"], 2, "the first run writes both tracks")
    ok(len(fetch_log) > 0, "…and asks the network")
    fetch_log.clear()
    second = wr.run_web_ratings(fill_cfg, fetch=tracking_fetch)
    eq(len(fetch_log), 0, "a second run makes ZERO fetch calls")
    eq(second["modified_count"], 0, "…writes nothing")
    eq(second["track_count"], 2, "…and still accounts for both tracks")
    eq(second["total_scanned"], 1, "…and for the album")
    eq(second["skipped_count"], 2, "…and books both tracks as skipped")
    fetch_log.clear()
    forced = wr.run_web_ratings(dict(fill_cfg, force_web_ratings=True),
                                fetch=tracking_fetch)
    ok(len(fetch_log) > 0, "a forced run fetches again")
    eq(forced["modified_count"], 2, "…and rewrites both tracks")
    shutil.rmtree(fill_base, ignore_errors=True)

    # The feature switch stops the run before a single request is made.
    asked = len(calls)

    def counting_fetch(*args, **kwargs):
        raise AssertionError("a switched-off run must not call the fetcher")

    stats3 = wr.run_web_ratings({"targets": [album_dir],
                                 "web_ratings_enabled": False,
                                 "web_ratings_sources": ["musicbrainz"]},
                                fetch=counting_fetch)
    eq(stats3["modified_count"], 0, "web_ratings_enabled off writes nothing")
    ok(len(calls) == asked, "…and asks no source")

    # The per-track pool must not let a COMPLETION order reach a tag. Two runs
    # over the same album content: one pinned to one lane per album (so the
    # track asks are serial, `track_width` 1), one with a wide track pool. The
    # injected fetcher makes RYM the slow one, so within a track the fast MB
    # answer lands first — a completion-ordered aggregation would title the
    # provenance "MusicBrainz; RateYourMusic"; the contract says the CONFIGURED
    # order. Every tag on disk must be byte-identical between the two runs.
    import threading
    import time as _time

    def make_same_album(base, name, tracks):
        folder = os.path.join(base, name)
        os.makedirs(folder)
        for i, (title, rec) in enumerate(tracks, 1):
            path = os.path.join(folder, f"{i:02d} {title}.flac")
            make_flac(path)
            g = FLAC(path)
            g["TITLE"] = [title]
            g["ARTIST"] = ["Artist"]
            g["ALBUMARTIST"] = ["Artist"]
            g["ALBUM"] = [name]
            g["MUSICBRAINZ_RELEASEGROUPID"] = [
                "f5093c06-23e3-404f-aeaa-40f72885ee3a"]
            g["MUSICBRAINZ_TRACKID"] = [rec]
            g.save()
        return folder

    def slow_fetch(source, kind, ident, cfg):
        """MB answers at once; RYM answers LAST, deliberately."""
        if source == "rateyourmusic":
            _time.sleep(0.08 if kind == "album" else 0.05)
            if kind == "album":
                return {"rating": {"value": 4.18, "count": 49366}}
            return {"rating": {"value": 4.67, "count": 17654}}
        if source == "musicbrainz":
            return MB_ALBUM if kind == "album" else MB_TRACK_OWN
        return None

    base = tempfile.mkdtemp(prefix="mlo_web_ratings_race_")
    race_tracks = [("Paranoid Android", "rec-1"), ("Let Down", "rec-2"),
                   ("Karma Police", "rec-3")]
    serial_dir = make_same_album(base, "Serial", race_tracks)
    conc_dir = make_same_album(base, "Concurrent", race_tracks)
    sources = ["rateyourmusic", "musicbrainz"]
    seen_threads = set()

    def threaded_fetch(source, kind, ident, cfg):
        seen_threads.add(threading.get_ident())
        return slow_fetch(source, kind, ident, cfg)

    s_serial = wr.run_web_ratings(
        {"targets": [serial_dir], "web_ratings_sources": sources,
         "worker_limit": 1}, fetch=slow_fetch)
    seen_threads.clear()
    s_conc = wr.run_web_ratings(
        {"targets": [conc_dir], "web_ratings_sources": sources,
         "worker_limit": 8}, fetch=threaded_fetch)
    ok(len(seen_threads) > 1,
       "the wide run really fanned the album and its tracks over >1 thread "
       f"(saw {len(seen_threads)})")
    eq(s_conc["modified_count"], 3, "the wide run wrote every track")
    eq(s_serial["modified_count"], 3, "the pinned run wrote every track")

    tags = ("WEBRATING", "WEBRATING_SOURCE", "ALBUMWEBRATING",
            "ALBUMWEBRATING_SOURCE")
    names = sorted(f for f in os.listdir(serial_dir) if f.endswith(".flac"))
    eq(sorted(f for f in os.listdir(conc_dir) if f.endswith(".flac")), names,
       "both runs wrote the same track files")
    for name in names:
        a = FLAC(os.path.join(serial_dir, name))
        b = FLAC(os.path.join(conc_dir, name))
        for tag in tags:
            eq(list(b.get(tag) or []), list(a.get(tag) or []),
               f"{name}: {tag} is byte-identical to the serial run")
    eq(list(FLAC(os.path.join(conc_dir, names[0])).get("WEBRATING_SOURCE")),
       ["RateYourMusic; MusicBrainz"],
       "…and the labels are the CONFIGURED order, never the completion order")
    ok(all(FLAC(os.path.join(conc_dir, n)).get("WEBRATING")
           for n in names),
       "…and every track carries a WEBRATING from both sources")
    shutil.rmtree(base, ignore_errors=True)

    for d in (tmp, library):
        shutil.rmtree(d, ignore_errors=True)

print(f"\n{'PASS' if not failed else 'FAIL'} — {passed} ok, {failed} failed")
sys.exit(1 if failed else 0)
