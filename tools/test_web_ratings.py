#!/usr/bin/env python3
"""Web ratings (`mlo/web_ratings.py`, script 24) — the whole chain, OFFLINE.

What this pins, and what it refuses to guess:

  * the SCALE conversion: every source publishes on its own scale (MusicBrainz
    0-5, RateYourMusic 0.5-5 whose maximum is what matters, Album of the Year
    0-100) and all of them land on the app's 0-100 Picard scale — the same one
    RATING uses — through ONE helper, with halves rounded UP (a 4.125/5 that
    read 82 because the digit above it was even is not a rule anyone can
    predict from the page).
  * the PARSERS, against payloads SAVED FROM THE LIVE SOURCES (nothing here
    touches the network): the MusicBrainz rating node as
    `release-group/<mbid>?inc=ratings` really answers it, the recording node
    with its work fallback, the RateYourMusic release page's schema.org
    microdata and its 2026 component (the exact markup of a real page), and
    Album of the Year's user score as BOTH the parsed dict the live fetcher
    returns and the saved page. A payload that states no rating —
    MusicBrainz's `{"value": null, "votes-count": 0}`, an AOTY answer with no
    user score — is a MISS and contributes nothing, never a zero.
  * the CHOICE: the configured order IS the hierarchy, so the value is the
    FIRST source that answered — a source below it is not even asked — and a
    TRACK-level answer beats an album-level one whoever stated it. Never a
    mean: the file carries a score a reader can open the site and check.
  * the WRITER: fill-only. A value already on the file survives a second run
    byte for byte unless the run is forced, and a switched-off family writes
    nothing. Proven on a REAL FLAC (tags read back off disk with mutagen) and
    on the runner itself.
  * the RUNNER (script 24): album-scoped, one album answer written to every
    track of the folder, and a track whose own sources said nothing falling
    back to a source's ALBUM answer (recorded at album level in `levels`). Its
    network is an INJECTED fetcher, so this suite can prove the whole run
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
# The conversion TRUNCATES to the app's grid (0.05 on the 0-5 scale): the tag
# never holds a number ABOVE what the source published, because the number a
# reader sees is that grid value printed to one decimal and a rounding UP would
# carry it past the tenth's boundary the source itself rounded to. The grid
# points ARE those boundaries, so truncating makes the displayed one decimal
# equal the source's own (R358/R358e): 4.54 -> 90 -> "4.5" (not 91 -> "4.6"),
# 4.45 -> 89 -> "4.5", 4.35 -> 87 -> "4.4" — the last one is exactly ON the
# grid, and the tolerance in `scale_to_100` is what keeps binary noise from
# truncating it to 86 ("4.3", a wrong number).
eq(wr.scale_to_100(5), 100, "5/5 is 100")
eq(wr.scale_to_100(0), 0, "0/5 is 0")
eq(wr.scale_to_100(4.35), 87, "4.35/5 is 87 (on the grid, not 86 from float noise)")
eq(wr.scale_to_100(4.18), 83, "4.18/5 is 83 (the grid value at or below it)")
eq(wr.scale_to_100(2.5), 50, "2.5/5 is 50")
eq(wr.scale_to_100(4.125), 82, "82.5 truncates to 82 — a source's 4.125 reads "
                              "\"4.1\" at one decimal, and 83 would read \"4.2\"")
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
eq(wr.parse_rateyourmusic(RYM_HTML), (83, 49366, "RateYourMusic"),
   "the RYM page HTML -> 83 (its own 4.18 truncated to the grid)")
eq(wr.parse_rateyourmusic({"rating": {"value": 4.18, "count": 49366}}),
   (83, 49366, "RateYourMusic"),
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
eq(wr.SOURCES, ["rateyourmusic", "albumoftheyear", "musicbrainz"],
   "the three shipped sources, in their built-in rank")
eq(list(wr.ALBUM_SOURCES), ["rateyourmusic", "albumoftheyear", "musicbrainz"],
   "all three answer for an album")
eq(list(wr.TRACK_SOURCES), ["rateyourmusic", "albumoftheyear", "musicbrainz"],
   "…and all three answer for a track (an album answer may stand in)")

eq(wr.parse_aoty(AOTY_HTML), (76, 152, "Album of the Year"),
   "AOTY: the user score box read at AOTY's own precision (the anchor's exact "
   "76.7), truncated to the app's grid — 77 would display \"3.9\" where the "
   "site's own 76.7 is 3.8")
eq(wr.parse_aoty(AOTY_CRITICS_ONLY), None,
   "…and the CRITIC score is never read as the user score")
eq(wr.parse_aoty(""), None, "an empty page is a miss")
# The LIVE shape the fetcher returns: the parsed dict from
# `server/aoty.parse_album` ({"genres", "rating", "tracks"}) carries the same
# user score as the saved page above.
eq(wr.parse_aoty({"genres": ["Alt-Country"],
                  "rating": {"value": 76.7, "count": 152},
                  "tracks": []}),
   (76, 152, "Album of the Year"),
   "the parsed AOTY album dict -> the same 76 (76.7 truncated)")
eq(wr.parse_aoty({"genres": ["Alt-Country"], "rating": None, "tracks": []}),
   None, "an AOTY answer with no user score is a miss")
eq(wr.parse_aoty_track({"title": "This Tornado Loves You",
                        "rating": {"value": 81.2, "count": 40}}),
   (81, 40, "Album of the Year"),
   "an AOTY track dict reads that SONG's own score")
eq(wr.parse_aoty_track({"title": "This Tornado Loves You", "rating": None}),
   None, "…and a song with no score is a miss")
eq(wr.parse_source("albumoftheyear",
                   {"rating": {"value": 81.2, "count": 40}}, "track"),
   (81, 40, "Album of the Year"),
   "the one dispatch is level-aware for AOTY too")
eq(wr.parse_source("musicbrainz", MB_ALBUM, "album"), (95, 105, "MusicBrainz"),
   "the one dispatch reads a MusicBrainz album payload")
eq(wr.parse_source("musicbrainz", MB_TRACK_OWN, "track"), (84, 18, "MusicBrainz"),
   "…and is level-aware for a track")
eq(wr.parse_source("albumoftheyear", AOTY_HTML, "album"),
   (76, 152, "Album of the Year"),
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

# The RATING chain has NO archive route: `rym_song_rating` reads the song page
# LIVE, even when `rym_archive_fallback` is set — that key belongs to the links
# and charts features (`discover.py`). So a live site that answers nothing is a
# MISS, and the archived-snapshot reader is never reached.
_ORIG_FETCH = intg._rym_archive_fetch
_arch_calls = []


def _forbidden_archive_fetch(url, params=None):
    """Any archive read is a contract violation on this path."""
    _arch_calls.append(url)
    return RYM_SONG_PAGE, url, True


intg._rym_archive_fetch = _forbidden_archive_fetch
_served.update(pages={}, asked=[], default=None)
intg._rym_get = _fake_rym_get
try:
    no_archive = intg.rym_song_rating("Radiohead", "Paranoid Android",
                                      {"rym_archive_fallback": True})
finally:
    intg._rym_archive_fetch = _ORIG_FETCH
    intg._rym_get = _ORIG_RYM_GET
eq(no_archive, None, "with the live site unanswered the song is a MISS")
eq(_arch_calls, [], "…and the rating chain never reaches for the archive")
eq(_served["asked"],
   ["/song/radiohead/paranoid-android/",
    "/song/radiohead/paranoid-android-1/"],
   "…after walking the two LIVE candidates, then giving up")

print("== the choice: the order IS the hierarchy ==")
# RYM answers the track at TRACK level, so it IS the value — and MusicBrainz,
# below it in the configured chain, is never even asked. The numbers are the
# owner's own live read of "Clint Eastwood" (2026-10-11): RYM 4.54 from 3 458
# votes, AOTY's row 95 from 1 394, MB's recording 83 from 20 — whose
# vote-weighted mean is 92, i.e. the file read 4.6 while the site said 4.54.
# A score a person can open and check beats a blend no site publishes.
_asked = []


def _clint_fetch(source, kind, ident, cfg):
    _asked.append((source, kind))
    if source == "rateyourmusic":
        return {"rating": {"value": 4.54, "count": 3458}}
    raise AssertionError(f"{source} must not be asked once RYM answered ({kind})")


clint = wr.track_rating("Gorillaz", "Clint Eastwood", "rec-mbid", {},
                        _clint_fetch,
                        album={"artist": "Gorillaz", "album": "Gorillaz",
                               "mbid": "rg-mbid"})
eq(clint["value"], 90, "RYM's own 4.54 -> 90 (truncated to the grid), which "
                        "prints \"4.5\" — the site's own one-decimal reading "
                        "(91 would print 4.6, the double rounding the owner saw)")
eq(clint["sources"], ["RateYourMusic"],
   "…and only the source whose number the file carries is named")
eq(clint["asked"], ["rateyourmusic"],
   "…and the walk STOPS there: no lower source is asked at all")
eq(clint["levels"], {"rateyourmusic": "track"}, "…at track level")
eq(_asked, [("rateyourmusic", "track")], "exactly one request was made")


def _rym_mb_fetch(source, kind, ident, cfg):
    """Two track-level sources, no socket: RYM's song page, MB's recording."""
    if source == "rateyourmusic":
        return {"rating": {"value": 4.67, "count": 17654}}   # the song page
    if source == "musicbrainz":
        return MB_TRACK_OWN                                  # 4.2/5, 18 votes
    return None


track = wr.track_rating("Radiohead", "Paranoid Android", "rec-mbid", {},
                        _rym_mb_fetch)
eq(track["sources"], ["RateYourMusic"],
   "a track's WEBRATING_SOURCE names the ONE source whose number it carries")
eq(track["value"], 93, "…RYM's own song score (4.67 -> 93)")
eq(track["asked"], ["rateyourmusic"],
   "…and MusicBrainz, below it in the chain, was not asked")
eq(track["levels"], {"rateyourmusic": "track"},
   "…and the answer is at TRACK level")

# A source that states NOTHING for the track itself falls back to its ALBUM
# answer — still that source's data, marked `album` in `levels` — but an
# ALBUM-level answer does NOT end the walk: a source further down may still
# state the track's OWN score, and that one wins.
def _album_fallback_fetch(source, kind, ident, cfg):
    """RYM does not answer this track at all; AOTY only answers for the ALBUM."""
    if source == "albumoftheyear":
        if kind == "album":
            return {"rating": {"value": 90, "count": 10}}
        return None
    if source == "rateyourmusic":
        return None                    # no song page, no release page either
    if source == "musicbrainz":
        return MB_TRACK_OWN            # 4.2/5, 18 votes
    return None


fb = wr.track_rating("Radiohead", "A Song Rym Does Not Have", "rec-mbid", {},
                     _album_fallback_fetch,
                     album={"artist": "Radiohead", "album": "In Rainbows",
                            "mbid": "rg-mbid"})
eq(fb["asked"], ["rateyourmusic", "albumoftheyear", "musicbrainz"],
   "a track nobody above states is asked all the way down the chain")
eq(fb["levels"], {"albumoftheyear": "album", "musicbrainz": "track"},
   "…the album-level answer is kept while the walk continues")
eq(fb["value"], 84,
   "…and MusicBrainz's TRACK answer beats AOTY's ALBUM answer from above")
eq(fb["sources"], ["MusicBrainz"], "…so the file carries its number and name")

# …and when NO source states the track, the first ALBUM-level answer stands.
def _album_only_fetch(source, kind, ident, cfg):
    """Only AOTY answers, and only for the ALBUM — nobody states the track."""
    if source == "albumoftheyear" and kind == "album":
        return {"rating": {"value": 90, "count": 10}}
    return None


none_row = wr.track_rating("Radiohead", "A Song Aoty Has Not Rated", "rec-mbid",
                           {}, _album_only_fetch,
                           album={"artist": "Radiohead", "album": "In Rainbows",
                                  "mbid": "rg-mbid"})
eq(none_row["levels"], {"albumoftheyear": "album"},
   "no track-level answer from anyone -> the album answer stands, marked `album`")
eq(none_row["value"], 90, "…and its number is the file's")
eq(none_row["sources"], ["Album of the Year"], "…attributed to that source")

# …and when the source's album payload carries the TRACK's OWN row, that row is
# the track's answer, at TRACK level: AOTY states every track's score on the
# album page, so a missing song page (the common case — the site has no page
# for most tracks) must not demote the track to the album's score.
def _album_rows_fetch(source, kind, ident, cfg):
    """AOTY answers the ALBUM payload, which carries a row per track."""
    if source == "albumoftheyear":
        if kind == "album":
            return {"rating": {"value": 90, "count": 10},
                    "tracks": [{"title": "Paranoid Android",
                                "rating": {"value": 96, "count": 37}},
                               {"title": "Karma Police",
                                "rating": {"value": 94, "count": 40}}]}
        return None
    if source == "rateyourmusic":
        return None
    if source == "musicbrainz":
        return {"recording": {"rating": {"value": None, "votes-count": 0}},
                "work": {"rating": {"value": None, "votes-count": 0}}}
    return None


row = wr.track_rating("Radiohead", "Paranoid Android", "rec-mbid", {},
                      _album_rows_fetch,
                      album={"artist": "Radiohead", "album": "In Rainbows",
                             "mbid": "rg-mbid"})
eq(wr.track_row("albumoftheyear",
                {"tracks": [{"title": "Karma  Police!",
                             "rating": {"value": 94, "count": 40}}]},
                "Karma Police")[0], 94,
   "a row matches on the folded title (punctuation and case are not the song)")
eq(wr.track_row("musicbrainz", {"tracks": [{"title": "Karma Police",
                                            "rating": {"value": 94}}]},
                "Karma Police"), None,
   "no other source has per-track rows in its album payload")
eq(row["levels"]["albumoftheyear"], "track",
   "AOTY's own row answers at TRACK level, not the album's")
eq(row["value"], 96, "…so the file carries 96, the row's own score")

print("== the choice function ==")
eq(wr.choose([((84, 31, "MusicBrainz"), "track")]),
   {"value": 84, "sources": ["MusicBrainz"]},
   "one answer is that answer, whatever its count")
eq(wr.choose([((90, 10, "Album of the Year"), "album"),
              ((84, 18, "MusicBrainz"), "track")]),
   {"value": 84, "sources": ["MusicBrainz"]},
   "a TRACK-level answer beats an album-level one stated above it")
eq(wr.choose([((90, 10, "Album of the Year"), "album"),
              ((95, 105, "MusicBrainz"), "album")]),
   {"value": 90, "sources": ["Album of the Year"]},
   "two album-level answers: the FIRST in the configured order wins")
eq(wr.choose([((4, 1, "A"), "album")]), {"value": 4, "sources": ["A"]},
   "a low count is not a weaker answer — the order decides, never the votes")
eq(wr.choose([]), None, "no answers at all chooses None")
eq(wr.choose([(None, "album")]), None, "…and a missing entry is skipped")

print("== the source order ==")
eq(wr.provider_order({}), [s for s in wr.SOURCES if s in wr.SOURCE_PARSERS],
   "no saved list = the built-in rank (minus any source without a parser)")
eq(wr.provider_order({"web_ratings_sources": ["albumoftheyear",
                                              "musicbrainz"]}),
   ["albumoftheyear", "musicbrainz"], "a saved order wins, as written")
eq(wr.provider_order({"web_ratings_sources": ["albumoftheyear", "bogus",
                                              "musicbrainz"]}),
   ["albumoftheyear", "musicbrainz"], "an unknown id is dropped, not fatal")
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
    res = wr.write_ratings(track, album={"value": 10, "sources": ["MusicBrainz"]},
                           track={"value": 10, "sources": ["MusicBrainz"]},
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
    res = wr.write_ratings(track, album={"value": 10, "sources": ["MusicBrainz"]},
                           track={"value": 10, "sources": ["MusicBrainz"]},
                           cfg={}, force=True)
    eq(res["wrote"].get("WEBRATING"), 10, "a forced run replaces the value")
    f = FLAC(track)
    eq(f["WEBRATING"], ["10"], "…on disk")
    eq(f["WEBRATING_SOURCE"], ["MusicBrainz"], "…and its provenance")

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
    eq(f["WEBRATING"], ["95"],
       "…and its WEBRATING falls back to that same source's ALBUM answer")
    eq(f["WEBRATING_SOURCE"], ["MusicBrainz"],
       "…attributed to the source that answered, at album level")

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
    # answer lands first — a completion-ordered choice would write MB's 84 and
    # name MusicBrainz; the contract says the CONFIGURED order decides, and MB
    # (below RYM) is not even asked. Every tag on disk must be byte-identical
    # between the two runs.
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
        """RYM answers LAST, deliberately; MB answers at once."""
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
       ["RateYourMusic"],
       "…and the label is the CONFIGURED order's answer, never the fastest one")
    ok(all(FLAC(os.path.join(conc_dir, n)).get("WEBRATING") == ["93"]
           for n in names),
       "…and every track carries RYM's own 93, though MusicBrainz answered first")
    shutil.rmtree(base, ignore_errors=True)

    for d in (tmp, library):
        shutil.rmtree(d, ignore_errors=True)

print(f"\n{'PASS' if not failed else 'FAIL'} — {passed} ok, {failed} failed")
sys.exit(1 if failed else 0)
