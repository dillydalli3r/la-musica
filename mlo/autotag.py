"""Automatic tagging: ALBUMITUNESADVISORY + INSTRUMENTAL + MOOD + GENRE.

Script 8 ("Auto Tagging") derives values that would otherwise have to be
filled in by hand:

1) ALBUMITUNESADVISORY from the per-track ITUNESADVISORY (set manually):
       0 = unrated / not explicit, 1 = explicit, 2 = edited / safe.
   Across ALL of the album's tracks (every disc in a multi-disc folder):
       any explicit track (1)      -> 1
       else any edited/safe track (2) -> 2
       else                        -> 0

2) INSTRUMENTAL, cross-referenced from every available source
   (`server.instrumental`: LRCLIB's own `instrumental` flag, Spotify
   audio-features when configured, the track's own name, lyrics evidence):
       any source says instrumental -> 1
       else any source says not instrumental (lyrics count as that) -> 0
       else -> LEFT UNTOUCHED. A track no source can state anything about
       keeps its tag as it is; absence of evidence is never read as
       "instrumental".

3) MOOD from the track's own audio (``mlo.moods``: tempo, energy, brightness,
   dynamics → valence/arousal quadrant), refined by the track's GENRE in
   hybrid mode, plus ENERGY — the 0-100 arousal the verdict was scored from.
   Grading requires the tags, so every track gets them unless the file cannot
   be decoded or librosa is unavailable; video containers are analysed too
   (mlo.moods extracts their audio through ffmpeg), and the GENRE they carry
   is written through the same video tag writer.

4) GENRE is TRIMMED, never imported. This script asks no provider for a
   genre and derives no family of its own: genres come from the import
   pipeline (`server/imports.py`, the genre chain — MusicBrainz and
   RateYourMusic per track, with the configured sources behind them) or from
   a manual tag edit. What the script does enforce is the cap every writer and
   the grader share: a list already carrying more than ``mb_genre_count``
   genres is brought down to it (`trim_genres`), so a pre-existing over-long
   list is fixed by re-running Auto tagging instead of failing grading
   forever.

Albums / tracks that already carry the correct values are skipped unless
the run is forced.
"""
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from .audio import AudioFile
from .config import DEFAULT_CONFIG, should_write_audio_tag
# one genre list policy for every writer: the trimmer, the top-up below and
# the import all keep order, drop case-insensitive repeats and cap the same way
from .genres import GENRE_COUNT_MAX, normalize_genres, split_stored
# the app's RELEASETYPE spelling ("album+live" -> "Album; Live"), shared with
# the organizer and the grader so one release_type reads the same everywhere;
# fuller_date is the same shared rule for the two DATE tags the album folder
# is named after
from .naming import date_is_partial, fuller_date, mb_style_release_type
from .paths import AUDIO_EXTS, load_expected_tracks
from .stats import (
    new_stats, _make_pbar, _pbar_skip, _pbar_update, _collect_targets,
    _find_albums, is_audio_file, worker_count,
)
# the ONE multi-value separator (mlo.tagtext owns it): a writer that has to
# know which values a file already states reads them apart with this, never
# with a second spelling of its own
from .tagtext import split_list
from .ui import print_header, log, c, Color


# ----------------------------------------------------------------------
# ALBUMITUNESADVISORY / INSTRUMENTAL (single-pass per album)
# ----------------------------------------------------------------------
def _album_files(album_dir):
    return sorted(
        os.path.join(album_dir, f)
        for f in os.listdir(album_dir) if is_audio_file(f))


def _derive_advisory(advisories):
    """1 if any explicit advisory, else 2 if any safe, else 0."""
    if any(v == "1" for v in advisories):
        return 1
    if any(v == "2" for v in advisories):
        return 2
    return 0


# INSTRUMENTAL cross-reference (server.instrumental), reached from script 8
# through this one hook. The engine ships no HTTP client of its own and never
# imports the server at module level; when the server is not importable the
# stage simply keeps its lyrics-only behaviour.
def _instrumental_fetch(paths, config):
    """{path: {"value", "answers", "evidence"}} — {} on any failure."""
    if not paths:
        return {}
    try:
        from server.instrumental import detect_instrumental
    except Exception:
        return {}
    try:
        return detect_instrumental(paths, config)
    except Exception:
        return {}


def genre_count(cfg, requested=None):
    """`mb_genre_count` — the one cap every genre writer clamps to.

    *cfg* is a config dict; *requested* is a caller's own limit (the import
    wizard's per-run "Max genres"). A request may only LOWER the cap:
    `mb_genre_count` is the user's setting, so no per-run control can write
    more genres than it, and a hand-edited config file cannot go past
    `GENRE_COUNT_MAX` either. A missing or unparsable value is the shipped
    default, never a literal that drifts from it.
    """
    try:
        cap = int((cfg or {}).get("mb_genre_count") or DEFAULT_CONFIG["mb_genre_count"])
    except (TypeError, ValueError):
        cap = int(DEFAULT_CONFIG["mb_genre_count"])
    cap = max(1, min(GENRE_COUNT_MAX, cap))
    if requested is None:
        return cap
    try:
        want = int(requested)
    except (TypeError, ValueError):
        # Handled by the caller (`server.main` answers 400 for a limit that is
        # not a number); the helper itself falls back to the setting so a
        # direct caller in Python never gets an exception for it.
        return cap
    if want <= 0:
        # 0 is "no per-run limit" in the API, not "one genre": clamping it up
        # to 1 would silently write a single genre for a request that asked
        # for no narrowing at all.
        return cap
    return max(1, min(cap, want))


def trim_genres(af, count):
    """Keep the first `count` genre values, drop the rest. Returns how many
    values were removed (0 = nothing changed, no container write).

    EVERY writer of the tag routes through this one helper (script 8, the
    genre import, script 10), so `mb_genre_count` means the same thing to all
    of them. Values are stored in source-priority order (highest-voted source
    first), so "keep the first N" IS "keep the N best" — the kept list is
    built by the shared `mlo.genres.normalize_genres`, which drops a
    case-insensitive repeat as well as the overflow, so a file carrying
    ["Rock", "rock", "Pop"] leaves here with two genres and one removal
    reported rather than a duplicate the grader fails.
    """
    try:
        count = max(0, int(count))
    except (TypeError, ValueError):
        return 0
    values = af.tag_values("GENRE")
    if not values:
        raw = af.get_tag("GENRE")
        if raw is None:
            return 0
        values = [raw]
    # What the CONTAINER holds, verbatim. The guard below compares against
    # THIS and not against the names split out of it: a file keeping one
    # "; "-joined value has to be rewritten into repeated fields even when the
    # names inside it are already canonical and in order — leaving the join in
    # place is not "nothing to clean", it is the other tagger's spelling.
    stored = [str(v) for v in values]
    if len(values) == 1 and ";" in str(values[0]):
        # ONE stored value that is really a "; "-joined list — the spelling
        # another tagger leaves behind, which tag_values() hands back whole
        # (repeated fields are the app's own spelling). Split it, or the file
        # would read as a single genre called "Rock; Alternative Rock; Indie".
        values = str(values[0]).split(";")
    # Blank repeats are dropped rather than kept: a file carrying ["", "Rock"]
    # must not end up with the empty string as its first genre.
    values = [v for v in (str(v).strip() for v in values) if v]
    # `count == 0` is this helper's "keep none" (delete the tag), which is NOT
    # normalize_genres' own 0 — there 0 means "no cap", for rendering.
    kept = normalize_genres(values, count) if count else []
    # What the tag should hold: repeated fields for a list, one plain value for
    # a single genre — the shapes `set_tag` writes below.
    want = [kept[0]] if len(kept) == 1 else kept
    if want == stored:
        # Nothing to remove, nothing to clean and nothing re-spelled: never
        # rewrite a container for nothing.
        return 0
    if want:
        # A list writes repeated GENRE fields; one value stays a plain string.
        af.set_tag("GENRE", want if len(want) > 1 else want[0])
    else:
        # Nothing survives the cap — an empty GENRE is worse than no GENRE.
        af.delete_tag("GENRE")
    # Never negative: one stored value holding a " / "-joined list is split
    # into more names than it had fields, and "removed" must stay a count.
    return max(0, len(values) - len(kept))


# ----------------------------------------------------------------------
# Album-level MusicBrainz release identity (LABEL / CATALOGNUMBER / …)
# ----------------------------------------------------------------------
# These are ALBUM facts the naming script reads per track, so an album that
# carries a release id must end up with them whichever way it arrived: the
# importer's own stamper (server.soulseek_auto._stamp_mb_tags) covers the
# download it verified, and this stage covers every other album (hand-matched
# in the wizard, or dropped into the library by hand).
#
# tag -> the key of the release payload server.integrations.release_lookup
# returns (the same payload shape the importer's stamper consumes).
_RELEASE_TAGS = (
    ("MUSICBRAINZ_ALBUMID", "id"),
    ("MUSICBRAINZ_RELEASEGROUPID", "release_group_id"),
    ("MUSICBRAINZ_ALBUMARTISTID", "album_artist_mbid"),
    ("LABEL", "label"),
    # the release's FIRST catalog number: a release can carry one per
    # label/pressing and a tag holds a single value
    ("CATALOGNUMBER", "catalog_number"),
    # Every country the release states, not this key's singular value: the
    # value comes from _release_country_codes, which reads the whole event
    # list (`countries`) with MusicBrainz's `country` first. See the country
    # section above — this is the one tag written as a LIST.
    ("RELEASECOUNTRY", "country"),
    ("RELEASETYPE", "release_type"),
    # The two dates the naming script puts in the album folder: the release's
    # own date and the release group's first release. Both are written in
    # FULL (YYYY-MM-DD) whenever MusicBrainz knows the day — a tag holding a
    # bare year is sharpened, never overwritten (see fuller_date).
    ("DATE", "date"),
    ("ORIGINALDATE", "originaldate"),
    ("MEDIA", "medium"),
)

# The two date slots above: the only tags this stage may SHARPEN (year ->
# full date) rather than merely fill when empty.
_DATE_TAGS = ("DATE", "ORIGINALDATE")

# Album facts filled WHENEVER the release is fetched — but deliberately NOT
# part of the prescan completeness test below: putting a slot there would make
# every otherwise-complete album cost one more MusicBrainz request (and get
# rewritten) just to gain a status, and the prescan's contract — an album that
# already carries everything this stage could write is never asked — is what
# the album-date suite pins. They ride along on a request made for another
# reason, exactly like the per-track ids in the loop.
_EXTRA_RELEASE_TAGS = (
    ("RELEASESTATUS", "status"),
    # The rest of the release's own facts: its barcode and ASIN, the language
    # and script its text is in, the licence it is published under and each
    # medium's own title. All of them are single values MusicBrainz states on
    # the release (or, for the disc title, on its medium), all of them have a
    # home in the container's tag system, and none of them names the album
    # folder — so they belong here rather than in the prescan.
    ("BARCODE", "barcode"),
    ("ASIN", "asin"),
    ("SCRIPT", "script"),
    ("LANGUAGE", "language"),
    ("LICENSE", "license"),
)

# Per-track slots this stage matches against the release (or the manifest):
# the recording id of the track's own position, and the artist it credits.
_PER_TRACK_TAGS = ("MUSICBRAINZ_TRACKID", "MUSICBRAINZ_ARTISTID")

# release_lookup's own `inc` list, so the album's release comes back with its
# label-info (label + catalog numbers) and the release group's types in ONE
# request that the browser cache then holds for every later album.
#
# The four relationship includes are what makes the CREDITS reachable: without
# them MusicBrainz answers with the tracklist and nothing about who played,
# produced, engineered or mixed it. `recording-level-rels` is what asks for
# each recording's own relations inside the same release response (one request
# per album, not one per track — the app rate-limits itself to one request a
# second), and `work-rels`/`work-level-rels` bring the work each track
# performs, which is where its composers and lyricists live. `url-rels` is
# what carries the licence relationship.
_RELEASE_INC = ("artists+recordings+media+release-groups+artist-credits"
                "+genres+labels+isrcs+recording-level-rels+artist-rels"
                "+work-rels+work-level-rels+url-rels")


# ----------------------------------------------------------------------
# The rest of the credit table: who played, produced, engineered, mixed
# ----------------------------------------------------------------------
# MusicBrainz keeps a recording's people in its relation list: every entry
# names a relation TYPE (producer, engineer, mix, arranger, conductor,
# remixer, …) and the artist it credits. The app wrote COMPOSER, LYRICIST and
# REMIXER out of that whole table, so a file carried the song but not the
# people who made it — no performer, no producer, no engineer, no mixer.
# Each type below has a tag in the app's own vocabulary (mlo.audio.TAG_MAP,
# Picard's names), and the value is a LIST: several people share one role on
# one track, and they are stored as repeated fields rather than a joined blob.
# "audio director"/"video director" are MusicBrainz's two names for the one
# DIRECTOR credit Picard defines.
_CREDIT_ROLES = (
    ("producer", "PRODUCER"),
    ("engineer", "ENGINEER"),
    ("mix", "MIXER"),
    ("arranger", "ARRANGER"),
    ("DJ-mix", "DJMIXER"),
    ("conductor", "CONDUCTOR"),
    ("remixer", "REMIXER"),
    ("director", "DIRECTOR"),
    ("audio director", "DIRECTOR"),
)

# The performer subtypes. Their relation's ATTRIBUTES name what was performed
# — the instrument, the kind of voice — and that is part of the credit, so it
# goes in the value in Picard's own "Name (instrument)" spelling: the same
# person on guitar and on drums is two different credits, and "guitar (12
# string)" must survive as the instrument it is. The dict's value is what a
# relation with no attribute at all means ("vocal" with no vocal type is
# simply vocals).
_PERFORMER_TYPES = {
    "instrument": "",
    "vocal": "vocals",
    "performing orchestra": "orchestra",
    "concertmaster": "concertmaster",
    "chorus master": "chorus master",
}

# The work's own relation list: the songwriting credits. MusicBrainz states
# them on the WORK (a work has composers and lyricists; a recording has
# performers), which is why they arrive inside the recording's "performance"
# relation to it. WRITER is Picard's "used when uncertain whether composer or
# lyricist".
_WORK_ROLES = (
    ("composer", "COMPOSER"),
    ("lyricist", "LYRICIST"),
    ("writer", "WRITER"),
)

# A work whose type MusicBrainz calls a movement (or a part) is not a work the
# track "is": it is one movement OF a work, and the app has the tags for that
# distinction (MOVEMENT / MOVEMENTNUMBER beside WORK). The same rule the beets
# plugin's work/movement pass applies, so both paths file a movement the same
# way.
_MOVEMENT_WORK_TYPES = ("movement", "part")



# ----------------------------------------------------------------------
# The release's countries — the ONE tag written as a LIST
# ----------------------------------------------------------------------
# MusicBrainz states a release's events (one per country the pressing appeared
# in) AND a singular `country`, which is only the FIRST of them. Every writer
# here used to copy that singular value, so an album out in three countries
# could never say so and every badge showed one code. RELEASECOUNTRY now holds
# the whole set, joined with mlo.tagtext._LIST_SEP when read back, with
# MusicBrainz's own first event FIRST: the naming script reads the first value
# (mlo.naming._first_multi, spec R33), so the album folder is named exactly as
# it was named before this tag could hold a list.
_COUNTRY_SPLIT = re.compile(r"\s*[;,/]\s*")


def _country_codes(value):
    """The country codes a stored RELEASECOUNTRY holds, in order, deduped.

    The app's own writers join repeated values with "; " and other taggers use
    ", " or " / "; the badge reads those same three separators
    (web/src/components/Badges.tsx `releaseCountries`), so one value is the
    same list wherever it is shown.
    """
    codes = []
    for part in _COUNTRY_SPLIT.split(str(value or "")):
        code = part.strip()
        if code and code.upper() not in {c.upper() for c in codes}:
            codes.append(code)
    return codes


def _release_country_codes(release):
    """Every country code *release* states — the value RELEASECOUNTRY is
    written as. [] when the release states none, which writes nothing.

    The singular `country` FIRST, then the rest of `countries` in
    `release_countries`' own (date, name) order: the first value is the code
    MusicBrainz calls the release's country and the one every writer stored
    before this tag could hold a list, so the naming script reads what it
    always read. An event MusicBrainz left without an area code contributes
    nothing — a missing code is absent, never invented.
    """
    codes = _country_codes(release.get("country"))
    for event in release.get("countries") or []:
        code = str((event.get("code") if isinstance(event, dict) else event)
                   or "").strip()
        if code and code.upper() not in {c.upper() for c in codes}:
            codes.append(code)
    return codes


def _country_upgrade(have, codes):
    """The value a RELEASECOUNTRY tag should hold, or "" to leave it alone.

    The ONE tag this stage may WIDEN instead of merely filling: a file tagged
    "US" before this tag could hold a list would otherwise keep one code
    forever while the release states US, CA and XE. The file's value is
    replaced by the release's whole set only when every code it holds is one
    the release states AND the release states at least one more (a strict
    subset). A country the release does NOT state is somebody else's answer and
    keeps its value, and a value that already is the release's set has nothing
    to gain.
    """
    have_codes = _country_codes(have)
    if not have_codes:
        return ""
    stated = {c.upper() for c in codes}
    if any(c.upper() not in stated for c in have_codes):
        return ""                      # never downgrade a value we did not state
    if len(codes) <= len(have_codes):
        return ""                      # already the release's whole set
    return list(codes)


def _artist_credit(rel):
    """(name, MusicBrainz id) of the artist one relation credits."""
    artist = rel.get("artist") or {}
    return (str(artist.get("name") or "").strip(),
            str(artist.get("id") or "").strip())


def _add_credit(out, tag, value):
    """Append one credit to *tag*'s list in *out*, dropping repeats."""
    value = str(value or "").strip()
    if not value:
        return
    values = out.setdefault(tag, [])
    if value not in values:
        values.append(value)


def _relation_credits(relations, roles):
    """{tag: [values]} for one relation list and the role table it answers to.

    A recording's relations are read against _CREDIT_ROLES (the performance
    and production roles), a WORK's against _WORK_ROLES (its songwriters) —
    the two lists hold different relationship types, and reading one with the
    other's table would silently drop every credit on it. Every artist
    relation the table has a tag for becomes one value, in MusicBrainz's own
    order. A person MusicBrainz credits twice — once on the release and once
    on the recording, or with two instruments — is credited once per distinct
    credit: the tag is a LIST of credits, not a tally, and the same pair
    written twice would make every reader show a duplicate.
    """
    out = {}
    for rel in relations or []:
        if not isinstance(rel, dict) or rel.get("target-type") != "artist":
            continue
        name, _mbid = _artist_credit(rel)
        if not name:
            continue
        rtype = str(rel.get("type") or "").strip()
        if rtype in _PERFORMER_TYPES:
            attrs = [str(a).strip() for a in (rel.get("attributes") or [])
                     if str(a).strip()]
            role = " ".join(attrs) or _PERFORMER_TYPES[rtype]
            _add_credit(out, "PERFORMER", f"{name} ({role})" if role else name)
            continue
        for mb_type, tag in roles:
            if rtype == mb_type:
                _add_credit(out, tag, name)
                break
    return out


def _work_credits(recording):
    """The songwriting credits a recording's work states.

    Returns (credits, work_title, work_mbid, movement, movement_number):
    the composer/lyricist/writer credits, the work the track performs (and
    whether MusicBrainz calls that work a movement, in which case its title
    belongs in MOVEMENT rather than WORK), and the per-composer MusicBrainz
    ids Picard keeps in a parallel list to COMPOSER. A recording with no
    performance relation — the common case for a pop song — answers
    ({}, "", "", "", ""), and nothing is written.
    """
    credits = {}
    work, work_mbid, movement, number = "", "", "", ""
    for rel in recording.get("relations") or []:
        if not isinstance(rel, dict) or rel.get("target-type") != "work":
            continue
        if str(rel.get("type") or "").strip() != "performance":
            continue
        node = rel.get("work") or {}
        title = str(node.get("title") or "").strip()
        if not work and title:
            work = title
            work_mbid = str(node.get("id") or "").strip()
            if str(node.get("type") or "").strip().lower() in _MOVEMENT_WORK_TYPES:
                movement, work = title, ""
                number = str(rel.get("number") or "").strip()
        for tag, values in _relation_credits(node.get("relations"),
                                             _WORK_ROLES).items():
            for value in values:
                _add_credit(credits, tag, value)
        # The composer ids ride in the same order as the names above, which is
        # what makes the parallel list usable: a reader resolves the nth id to
        # the nth composer.
        for sub in node.get("relations") or []:
            if not isinstance(sub, dict):
                continue
            if str(sub.get("type") or "").strip() != "composer":
                continue
            _add_credit(credits, "MUSICBRAINZ_COMPOSERID", _artist_credit(sub)[1])
    return credits, work, work_mbid, movement, number


def _release_license(release):
    """The licence URL *release* is published under, or "".

    MusicBrainz keeps a licence as a url relationship with the type "license"
    — on the release for a release-wide licence, on the recording when only
    one track is under it. The first one stated wins, so a file carries the
    licence the release actually names rather than all of them.
    """
    for rel in release.get("relations") or []:
        if not isinstance(rel, dict) or rel.get("target-type") != "url":
            continue
        if str(rel.get("type") or "").strip() != "license":
            continue
        url = (rel.get("url") or {}).get("resource") if isinstance(
            rel.get("url"), dict) else rel.get("url")
        if str(url or "").strip():
            return str(url).strip()
    return ""


def _track_credits(recording):
    """EVERY credit one recording states, as {tag: [values]}.

    The recording's own relation list (performers with their instrument,
    producers, engineers, mixers, arrangers, DJ-mixers, conductors, remixers,
    directors) plus what its WORK states (composers, lyricists, writers, and
    the work itself — its title, its id, and its movement number when
    MusicBrainz calls the work a movement rather than a work the track "is"),
    plus a licence stated for this recording rather than for its release. The
    one reader both writers of these tags use.
    """
    credits = _relation_credits(recording.get("relations"), _CREDIT_ROLES)
    work_credits, work, work_mbid, movement, number = _work_credits(recording)
    for tag, values in work_credits.items():
        for value in values:
            _add_credit(credits, tag, value)
    if work or movement:
        _add_credit(credits, "MUSICBRAINZ_WORKID", work_mbid)
        if movement:
            _add_credit(credits, "MOVEMENT", movement)
            _add_credit(credits, "MOVEMENTNUMBER", number)
        else:
            _add_credit(credits, "WORK", work)
    _add_credit(credits, "LICENSE", _release_license(recording))
    return credits


def _cached_release(mbid):
    """`release_lookup`'s payload for *mbid* over the app's CACHED MB access.

    `mb_get_cached` is the cache the whole server browses through (30-minute
    TTL, single-flight, one request per album at most) — never a fresh
    per-track lookup, and never the uncached `release_lookup`. Returns None
    when there is no release id, the server is not importable (plain CLI), or
    MusicBrainz cannot answer: the caller then writes NOTHING rather than
    inventing a label.
    """
    if not mbid:
        return None
    try:
        # `release_countries` is the server's ONE reader of a release's own
        # events (it names the area and marks the configured preference);
        # importing it here rather than re-parsing them keeps the tag this
        # stage writes and the countries the pages show from ever disagreeing.
        from server.integrations import mb_get_cached, release_countries
    except Exception:
        return None
    try:
        data = mb_get_cached(f"release/{mbid}",
                             {"inc": _RELEASE_INC, "fmt": "json"})
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    rg = data.get("release-group") or {}
    types = [str(rg.get("primary-type") or "").lower()]
    types += [str(s).lower() for s in (rg.get("secondary-types") or [])]
    label = ""
    catalogs = []
    for lab in data.get("label-info") or []:
        if not label:
            label = str(((lab.get("label") or {}).get("name")) or "").strip()
        cn = str(lab.get("catalog-number") or "").strip()
        if cn and cn not in catalogs:
            catalogs.append(cn)
    # The release's own tracks, keyed by (disc, position) — the ONLY key this
    # stage matches on. A title is never compared: two tracks of one album can
    # share a title and a differently-punctuated one must not become a miss.
    tracks = {}
    for medium in data.get("media") or []:
        disc = int(medium.get("position") or 1)
        for trk in medium.get("tracks") or []:
            pos = trk.get("position")
            if not pos:
                continue
            rec = trk.get("recording") or {}
            artists = [ac["artist"]["id"] for ac in trk.get("artist-credit") or []
                       if ac.get("artist")]
            # The people behind the track. ONE release request carried them
            # (see _RELEASE_INC): the recording's own relations hold the
            # performer/producer/engineer/mixer/arranger/DJ-mix/conductor
            # credits, its work relation holds the songwriters and the work
            # itself. Read here, next to the ids, so both writers of these tags
            # — this stage and the beets plugin — read one shape.
            credits = _track_credits(rec)
            tracks[(disc, int(pos))] = {
                "recording_mbid": str(rec.get("id") or ""),
                "artist_mbid": str(artists[0] if artists else ""),
                # The id of this POSITION (distinct from the recording) and
                # the ISRCs MusicBrainz knows for it: `inc` above already
                # fetched both, and they are what the naming script and the
                # ISRC tag read — EVERY one of them, because a recording
                # published in several territories carries an ISRC per
                # territory and the tag is a list.
                "release_track_mbid": str(trk.get("id") or ""),
                "isrcs": [str(x) for x in (rec.get("isrcs")
                                           or trk.get("isrcs") or [])],
                "credits": credits,
            }
    album_artists = [ac["artist"]["id"] for ac in data.get("artist-credit") or []
                     if ac.get("artist")]
    return {
        "id": str(data.get("id") or ""),
        "release_group_id": str(rg.get("id") or ""),
        # the release's artist credit IS the album artist (Picard semantics)
        "album_artist_mbid": str(album_artists[0] if album_artists else ""),
        "tracks": tracks,
        "label": label,
        "catalog_number": catalogs[0] if catalogs else "",
        "country": str(data.get("country") or ""),
        # EVERY country this pressing appeared in, with its date: MusicBrainz's
        # singular `country` above is only the release's FIRST event, so a
        # release out in several countries states one code there and all of
        # them here. The RELEASECOUNTRY tag is written from THIS list.
        "countries": release_countries(data),
        # the release's own status ("Official", "Bootleg", …) — the same
        # field the importer's stamper writes as RELEASESTATUS
        "status": str(data.get("status") or ""),
        # the spelling the app's writers use ("album+live" -> "Album; Live")
        "release_type": mb_style_release_type("+".join(t for t in types if t)),
        # the RELEASE's own date (full when MusicBrainz has the day) — the
        # naming script's %date%, second in the album folder
        "date": str(data.get("date") or ""),
        # the release GROUP's first release date — %originaldate%, first in
        # the album folder
        "originaldate": str(rg.get("first-release-date") or ""),
        # the first medium (CD / Vinyl / Digital Media) — the naming script's
        # %media%
        "medium": next((str(m.get("format") or "")
                        for m in data.get("media") or []), ""),
        # The release's text representation (MusicBrainz keeps language and
        # script together) and its barcode / ASIN / licence — single values
        # the release states, absent from the payload when it states none.
        "language": str((data.get("text-representation") or {})
                        .get("language") or ""),
        "script": str((data.get("text-representation") or {}).get("script") or ""),
        "barcode": str(data.get("barcode") or ""),
        "asin": str(data.get("asin") or ""),
        "license": _release_license(data),
        # Each medium's own title ("Disc 2: The Rarities"), by disc number:
        # DISCSUBTITLE is a per-DISC tag, so the writer picks the entry for
        # the file's own disc rather than repeating disc 1's title everywhere.
        "medium_titles": {int(m.get("position") or 1): str(m.get("title") or "")
                          for m in data.get("media") or []},
    }


def _track_position(af, path):
    """(disc, position) of one file — the key every MB match here uses.

    Its own DISCNUMBER/TRACKNUMBER tags first ("3/12" reads as 3), then the
    naming script's own "D-PP " file-name prefix, which is authoritative when
    the tags are missing (the script wrote that name from the tags).
    """
    def _num(value):
        m = re.match(r"\s*(\d+)", str(value or ""))
        return int(m.group(1)) if m else 0

    disc = _num(af.get_tag("DISCNUMBER")) or 1
    pos = _num(af.get_tag("TRACKNUMBER"))
    if not pos:
        m = re.match(r"(\d+)\s*-\s*(\d+)", os.path.basename(path))
        if m:
            disc, pos = int(m.group(1)), int(m.group(2))
    return disc, pos


def _slot_open(af, tag):
    """Whether *tag* still has something to gain from MusicBrainz: it is
    EMPTY, it is a DATE that stops short of the day ("1980" → the full
    "1980-10-01" the album folder should spell), or it is a RELEASECOUNTRY
    holding a SINGLE code.

    A country is the one slot whose value the release can WIDEN rather than
    merely fill (see _country_upgrade), and a single code cannot be told apart
    from a strict subset of the release's own events without asking about the
    release: "US" alone may be all MusicBrainz states, or the first event of a
    release that is also out in CA and XE. A tag already holding several codes
    is the app's own list form and can gain nothing, so an album carrying one
    is the ONLY country case that costs a request.
    """
    value = str(af.get_tag(tag) or "").strip()
    if not value:
        return True
    if tag in _DATE_TAGS:
        return date_is_partial(value)
    if tag == "RELEASECOUNTRY":
        return len(_country_codes(value)) < 2
    return False


def mb_track_tags(release, slot, disc=1, album_artist_mbid=""):
    """EVERY MusicBrainz value ONE track's file should carry, as (tag, value).

    The album-level values every file of the release repeats (its identity:
    label, catalog number, barcode, country list, type, status, medium, both
    dates, ids — plus the facts no naming script reads: ASIN, language, script,
    licence and this disc's own title), and the per-track ones: the two track
    ids, the artist, EVERY ISRC the recording states, and the credit table —
    each shared role as a LIST, so several performers or producers survive as
    repeated fields rather than one joined blob. Values the release does not
    state are absent, never empty: a release that credits no producer must not
    produce a blank PRODUCER.

    *slot* is the release payload's entry for this file's (disc, position), or
    {} for a track MusicBrainz does not list — the credits then belong to
    nobody and only the album-level values are written. This is the one reader
    of the payload both MusicBrainz paths use (the Auto Tagging stage and the
    beets import plugin), so a field means the same thing on either.
    """
    values = []
    for tag, key in _RELEASE_TAGS:
        # RELEASECOUNTRY is the one tag whose value is a LIST — every country
        # the release states, its own first event first. set_tag writes a list
        # as repeated fields (Vorbis comments, an ID3 text list, one MP4 atom
        # per value) and get_tag reads them back "; "-joined, which is the
        # same value the importer's own stamper writes.
        value = (_release_country_codes(release) if tag == "RELEASECOUNTRY"
                 else str(release.get(key) or "").strip())
        if value:
            values.append((tag, value))
    for tag, key in _EXTRA_RELEASE_TAGS:
        value = str(release.get(key) or "").strip()
        if value:
            values.append((tag, value))
    title = str((release.get("medium_titles") or {}).get(int(disc or 1)) or "")
    if title:
        values.append(("DISCSUBTITLE", title))

    slot = slot or {}
    values.append(("MUSICBRAINZ_TRACKID", slot.get("recording_mbid") or ""))
    values.append(("MUSICBRAINZ_ARTISTID",
                   slot.get("artist_mbid") or album_artist_mbid))
    # The id of this track's POSITION on this release — a different id from
    # the recording id above, and the one beets/Picard write to every file.
    values.append(("MUSICBRAINZ_RELEASETRACKID", slot.get("release_track_mbid") or ""))
    # EVERY ISRC the recording states, as the list it is: one recording is
    # published in several territories under several ISRCs, and the country
    # prefixes are what tell them apart.
    values.append(("ISRC", list(slot.get("isrcs") or [])))
    for tag, credited in (slot.get("credits") or {}).items():
        values.append((tag, list(credited)))
    return values


def write_mb_tags(af, values, config=None, replace=None):
    """Write MusicBrainz-derived values onto ONE open file.

    The one writer both MusicBrainz paths use — this stage and the beets
    import plugin — so a field written by either lands the same way: an EMPTY
    value is skipped (a release that states no ISRC must not produce a blank
    ISRC tag, which every later run would report as "written"), a SCALAR tag
    that already holds a value is KEPT (another pressing's label, or ids
    another tagger wrote, are the album's own business), and every write
    honours the per-tag gates. A LIST is passed through to set_tag, which
    stores it as repeated container fields; a list on a file that already
    states values for that tag COMPLETES them (see _complete_list) instead of
    being cut down to whatever one value the file happened to hold.

    *replace* is the pass's own rule for the tags it may change WITHOUT them
    being empty — ``(tag, have, want) -> value`` (see _fill_release_tags: the
    two dates are sharpened and the country widened). It is asked FIRST, so a
    tag with its own write rule keeps it.

    Returns ``(written, refused)``: how many tags were written, and the
    ``(tag, reason)`` pairs the file's own writer REFUSED — a tag a container
    cannot hold, a read-only file, a full disk. The caller reports them per
    file; swallowing them is what makes a file that was never tagged read
    exactly like a release with nothing to say.
    """
    written = 0
    refused = []
    for tag, value in values:
        try:
            value = _clean_value(value)
            if value is None:
                continue
            have = str(af.get_tag(tag) or "").strip()
            if have:
                wanted = (_clean_value(replace(tag, have, value))
                          if replace is not None else None)
                if wanted is not None:
                    value = wanted
                elif isinstance(value, list):
                    # The answer is a LIST (several performers, several
                    # ISRCs) and the file already states values for this tag:
                    # COMPLETE them rather than leaving the tag alone. The old
                    # blanket ``continue`` cut a release's two engineers down
                    # to whichever single credit the file happened to hold.
                    value = _complete_list(tag, have, value)
                    if value is None:
                        continue
                else:
                    continue
            if not should_write_audio_tag(config, tag, filepath=af.path):
                continue
            if af.set_tag(tag, value):
                written += 1
            else:
                refused.append((tag, str(getattr(af, "error", "") or "refused")))
        except Exception as e:  # noqa: BLE001 — one tag never stops the rest
            refused.append((tag, f"{type(e).__name__}: {e}"))
    return written, refused


def _clean_value(value):
    """The value to store for one tag, or None when there is nothing to store.

    A LIST (several performers, several ISRCs) is trimmed per value and comes
    back as the list; an empty answer — a release that states nothing for this
    tag — is None, because a blank tag is not a value.
    """
    if isinstance(value, (list, tuple, set)):
        values = [str(v).strip() for v in value if str(v).strip()]
        return values or None
    text = str(value or "").strip()
    return text or None


def _complete_list(tag, have, want):
    """*have* plus every value of *want* it does not already state, or None.

    The one rule for a LIST answer landing on a tag that already holds
    values: the file's own values come first, in the order it states them,
    and the answer's values it does not already state follow in
    MusicBrainz's own order. A credit is DATA, not a single slot — a release
    whose track has two engineers must not end up with one because the file
    arrived carrying the other — and the file's own value may be one
    MusicBrainz does not state at all (another pressing's credit), so it is
    never dropped either. Comparison is case-insensitive, the way the genre
    and country lists of this app already compare their own values.

    None means the tag is COMPLETE: nothing may be written, which is what
    keeps a re-run (and the prescan's "nothing to fill") a no-op.

    RELEASECOUNTRY is answered by its own rule (``_country_upgrade``, asked
    through *replace* before this): a country the release does not state keeps
    the value as it is rather than being added to a list it never belonged to,
    so this never completes it.
    """
    if tag == "RELEASECOUNTRY":
        return None
    merged = split_list(have)
    before = len(merged)
    seen = {value.upper() for value in merged}
    for value in want:
        if value.upper() not in seen:
            seen.add(value.upper())
            merged.append(value)
    return merged if len(merged) > before else None


def _mb_replace(tag, have, want):
    """The value a NON-EMPTY MusicBrainz tag should be changed to ("" to keep it).

    Two tags are not merely filled. DATE and ORIGINALDATE are SHARPENED to
    MusicBrainz's spelling when the tag holds a coarser form of the same date
    ("1980" -> "1980-10-01", see fuller_date): those two name the album
    folder, so a year-only value would pin it there for good. RELEASECOUNTRY is
    WIDENED to the release's whole country set when the value it holds is a
    strict subset of it ("US" -> "US; CA; XE", see _country_upgrade). Every
    other tag keeps what it has — another tagger's value is never overwritten.
    """
    if tag == "RELEASECOUNTRY":
        return _country_upgrade(have, want)
    return fuller_date(have, want)


def _fill_release_tags(info, config, album_dir):
    """Fill each track's MusicBrainz tags from the album's release.

    Album-level facts the naming script reads per track (label, catalog
    number, country, type, both DATES, medium + the release's own ids) are
    written to EVERY file of the album; the per-track ones (recording id, and
    the credited artist id) come from a POSITION match — the album's own
    `.mlo_expected.json` manifest first (it records the release the wizard
    matched), then the release payload's tracklist. A track with no
    counterpart is left alone and counted.

    The SAME request also carries everything else MusicBrainz states about the
    release and its tracks (see mb_track_tags): the credit table with each
    shared role as the list it is, the work's songwriters, every ISRC, and the
    release facts no naming script reads (barcode, ASIN, language, script,
    licence, each medium's title). Those ride along on a request made for
    another reason and are never a reason to make one — an album that already
    carries every prescan slot costs nothing, exactly as before.

    A tag that already holds a value is never touched — another pressing's
    label, ids another tagger wrote, are the album's own business — with
    THREE exceptions. DATE and ORIGINALDATE are SHARPENED to MusicBrainz's
    spelling when the tag holds a coarser form of the same date ("1980" →
    "1980-10-01", see fuller_date): those two name the album folder, so a
    year-only value would otherwise keep it a year forever. RELEASECOUNTRY is
    WIDENED to the release's whole country set when the value it holds is a
    strict subset of it ("US" → "US; CA; XE", see _country_upgrade): the tag
    holds a LIST and a file written before it could would otherwise keep the
    release's first event forever. And a LIST answer for any other tag
    COMPLETES what the file states instead of dropping the values it does not
    have (see _complete_list): a track with two engineers must not end up with
    one because the file already carried the other. Returns (written, note) —
    the album's report line, including the "nothing written" cases and every
    tag a file REFUSED (a container that cannot hold it, a failed save), named
    per file.
    """
    manifest = load_expected_tracks(album_dir)
    mbid = ""
    for d in info:
        mbid = str(d["af"].get_tag("MUSICBRAINZ_ALBUMID") or "").strip()
        if mbid:
            break
    if not mbid:
        # the album's own manifest still names the release the wizard matched
        mbid = str(manifest.get("release_id") or "").strip()
    if not mbid:
        # No release id: whatever the tags say is all there is. Never guess.
        return 0, "release tags: no musicbrainz_albumid"

    # Prescan: an album that already carries every tag this stage could write
    # is finished, so it never costs a request. A library-wide run must not
    # ask MusicBrainz about albums that are already complete. Two slots are
    # never "complete" as they stand: a date that stops short of the day
    # (MusicBrainz may spell the same date in full, and the album folder is
    # named after it) and a RELEASECOUNTRY holding one code (it may be the
    # first event of a release out in several countries — the one case where
    # the request is what tells the two apart).
    slots = [tag for tag, _key in _RELEASE_TAGS] + list(_PER_TRACK_TAGS)
    if not any(_slot_open(d["af"], tag) for d in info for tag in slots):
        return 0, "release tags: nothing to fill"

    release = _cached_release(mbid)
    if not release:
        return 0, "release tags: MusicBrainz had no answer"

    # recording id per (disc, position): the manifest wins — its release_id is
    # the one the album was matched against
    manifest_ids = {t["disc"] * 1000 + t["position"]: t["recording_mbid"]
                    for t in manifest["tracks"] if t.get("recording_mbid")}
    if not any(_clean_value(
            _release_country_codes(release) if tag == "RELEASECOUNTRY"
            else str(release.get(key) or "").strip())
            for tag, key in _RELEASE_TAGS + _EXTRA_RELEASE_TAGS) \
            and not release["tracks"] and not manifest_ids:
        # Nothing to write: the release states no identity, no extra fact and
        # no tracklist this album can be matched against.
        return 0, "release tags: release carries none"

    written = 0
    unmatched = 0
    refused = []
    for d in info:
        af = d["af"]
        if getattr(af, "audio", True) is None:
            # A container this app cannot tag at all (mutagen has no writer for
            # it — an .wv, an unreadable file). Named per file: reporting
            # "0 written" without it is exactly the silent answer that hides a
            # file which can never carry the metadata.
            refused.append(f"{os.path.basename(af.path)}: container is not taggable")
            continue
        key = _track_position(af, af.path)
        slot = dict(release["tracks"].get(key) or {})
        track_mbid = (manifest_ids.get(key[0] * 1000 + key[1])
                      or slot.get("recording_mbid") or "")
        if not track_mbid and not slot:
            unmatched += 1
        # The manifest's recording id is the one the album was MATCHED
        # against, so it wins over the release payload's own tracklist.
        slot["recording_mbid"] = track_mbid or slot.get("recording_mbid") or ""
        # Everything this file should carry — the album-level identity, the
        # extras and the whole credit table — from the ONE release request
        # made above.
        per_track = mb_track_tags(release, slot, disc=key[0],
                                  album_artist_mbid=release["album_artist_mbid"])
        # ONE container rewrite per file. Each set_tag used to save the whole
        # file for itself, so filling twelve tags on a 30 MB track rewrote it
        # twelve times; the flush below is where all of them land. A handle
        # that only implements the get/set contract (a caller's stub) simply
        # writes per tag, like before.
        defer = hasattr(af, "defer_save")
        if defer:
            af.defer_save(True)
        try:
            pending, refusals = write_mb_tags(af, per_track, config,
                                              replace=_mb_replace)
        finally:
            # The one write of this file. A failed flush wrote nothing, so the
            # tags never reached the disk and must not be reported as written;
            # a Ctrl+C between here and the loop's end still flushes what the
            # loop already applied instead of dropping it.
            if defer and af.defer_save(False) is not True:
                pending = 0
        written += pending
        name = os.path.basename(af.path)
        if defer and not pending and refusals:
            # A flush that wrote nothing puts every applied tag back in the
            # refused list: the file never changed, so nothing landed.
            refusals = [(tag, "the container write failed")
                        for tag, _reason in refusals]
        refused.extend(f"{name}: {tag} ({reason})" for tag, reason in refusals)
    if not written:
        if refused:
            return 0, "release tags: refused — " + "; ".join(refused[:4]) + (
                f" (+{len(refused) - 4} more)" if len(refused) > 4 else "")
        return 0, "release tags: nothing to fill"
    note = f"release tags={written}"
    if unmatched:
        note += f" ({unmatched} track(s) not in the release)"
    if refused:
        note += " — refused: " + "; ".join(refused[:4]) + (
            f" (+{len(refused) - 4} more)" if len(refused) > 4 else "")
    return written, note


# ----------------------------------------------------------------------
# Script 8 runner
# ----------------------------------------------------------------------
def run_auto_tagging(config):
    folder = config["music_folder"]
    stats = new_stats()

    print_header("Auto Tagging")
    log(f"music folder: {folder}")
    if config.get("auto_advisory", True):
        log("  ALBUMITUNESADVISORY: from per-track ITUNESADVISORY "
            "(any explicit -> 1, else any safe -> 2, else 0)")
    log("  MUSICBRAINZ release identity: label, catalog number, barcode, "
        "country, type, status, language, script, ASIN, licence, medium + "
        "missing MBIDs (release id, release-group id, artist ids, per-track "
        "recording id, release-track id) filled from the cached release — "
        "only where a tag is EMPTY")
    log("  MUSICBRAINZ credits: performers (with their instrument), producers, "
        "engineers, mixers, arrangers, DJ-mixers, conductors, directors, the "
        "work's composers/lyricists/writers and the ISRCs — each role written "
        "as the LIST it is (repeated fields, never a joined blob)")
    log("  RELEASECOUNTRY: every country the release states, \"; \"-joined "
        "(the tag holds a LIST, MusicBrainz's first event first) — a file "
        "holding one of them gains the rest")
    log("  DATE / ORIGINALDATE (the album folder's two dates): filled when "
        "empty, and sharpened to MusicBrainz's full date when the tag holds "
        "only a year or a year-month of the same date")
    if config.get("auto_zero_advisory_for_instrumental", True):
        log("  ITUNESADVISORY: zeroed on instrumentals (auto_zero_advisory_for_instrumental)")
    if config.get("auto_instrumental", True):
        log("  INSTRUMENTAL: " + (
            "cross-referenced (LRCLIB, Spotify, the file's own name, lyrics)"
            if config.get("instrumental_auto_fetch", True) else
            "0 when lyrics present (no-lyrics tracks left untouched)"))
    if config.get("mood_enabled", True):
        log("  MOOD: from the track's audio" + (
            " (refined by GENRE)" if config.get("mood_source", "hybrid") == "hybrid"
            else f" (source: {config.get('mood_source', 'hybrid')})"))
    if config.get("genre_autofill", True):
        log(f"  GENRE: trimmed to {genre_count(config)} — genres are never "
            f"imported by a script (only the import and manual edits write them)")

    force = config.get("force_auto_tag", False)
    do_advisory = config.get("auto_advisory", True)
    do_instrumental = config.get("auto_instrumental", True)
    do_mood = config.get("mood_enabled", True)
    do_genre = config.get("genre_autofill", True)
    # The per-track cap this app's writers keep (`mb_genre_count`, clamped to
    # its own ceiling by the one helper above); grading accepts at most this
    # many genres per track.
    genre_cap = genre_count(config)
    if do_mood:
        from . import moods  # local: keeps librosa discovery out of import time
    # An instrumental has no words to be explicit with, so its advisory is
    # zeroed; a setting can turn that off (see mlo.config).
    do_zero_advisory_for_instrumental = config.get("auto_zero_advisory_for_instrumental", True)

    if config.get("targets") is not None:
        target_files = _collect_targets(config["targets"], AUDIO_EXTS)
        album_dirs = sorted({os.path.dirname(f) for f in target_files})
    else:
        if not os.path.isdir(folder):
            log(c(f"ERROR: folder does not exist: {folder}", Color.RED))
            return stats
        album_dirs = _find_albums(folder)

    if not album_dirs:
        log("No albums found.")
        return stats

    # Per-album MusicBrainz release-tag writes, for the run summary. Albums
    # run on a thread pool, so the count is collected per album here rather
    # than incremented into shared state.
    release_written = []

    def process_album(album):
        files = _album_files(album)
        if not files:
            return album, 0, None, None, []

        # Single pass: load every file once and cache the values needed,
        # instead of re-parsing each file for advisory + instrumental.
        # The handle stays valid across every write below: AudioFile.set_tag()
        # drops its own read caches, so a later get_tag() on this same
        # instance already sees the value just written — re-opening the file
        # per fix (one full container parse each) bought nothing.
        info = []
        for path in files:
            try:
                af = AudioFile(path)
                lyr = af.get_lyrics()
                info.append({
                    "af": af,
                    "advisory": str(af.get_tag("ITUNESADVISORY") or "").strip(),
                    "album_advisory": str(
                        af.get_tag("ALBUMITUNESADVISORY") or "").strip(),
                    "instrumental": str(af.get_tag("INSTRUMENTAL") or "").strip(),
                    "has_lyrics": bool(lyr and str(lyr).strip()) or
                        os.path.exists(os.path.splitext(path)[0] + ".lrc"),
                })
            except Exception:
                continue
        if not info:
            return album, 0, None, None, []

        # ONE container rewrite per file for the WHOLE album pass. Six stages
        # below fill tags (the whitespace fix, the release identity,
        # INSTRUMENTAL, the derived album advisory, the instrumental zero and
        # its re-derivation) and every set_tag used to save the whole
        # container for itself — six whole-file copies of a 30 MB track for
        # six tags, on a library whose script 3 writes `--padding=0` so there
        # is no padding to absorb them. The flush at the bottom is where all
        # of them land at once, exactly as `_fill_release_tags` already does
        # for its own dozen; a handle that only implements get/set (a caller's
        # stub) cannot defer and keeps writing per tag, like before.
        deferred = [d["af"] for d in info if hasattr(d["af"], "defer_save")]
        for af in deferred:
            af.defer_save(True)

        modified = 0
        notes = []
        advisory_value = None

        # Formatting: ensure GENRE has no leading/trailing spaces and
        # ITUNESADVISORY is exactly 0/1/2 without spaces. This is the
        # optimization step for those tags.
        for d in info:
            # GENRE (standard tag, not gated by per-type ADVISORY — but still trim)
            try:
                raw_genre = d["af"].get_tag("GENRE")
                if raw_genre is not None:
                    stripped = str(raw_genre).strip()
                    if str(raw_genre) != stripped:
                        # A stored value another tagger joined is written back
                        # as the names INSIDE it (";" or " / ", see
                        # split_stored) — otherwise trimming the whitespace
                        # would COLLAPSE three genres into one tag named
                        # "A; B; C".
                        value = split_stored(stripped) or stripped
                        if d["af"].set_tag("GENRE", value):
                            modified += 1
            except Exception:
                pass
            # ITUNESADVISORY: trim spaces; keep 0/1/2 only (grading will flag others)
            # Gated by per-filetype ADVISORY
            try:
                raw_adv = d["af"].get_tag("ITUNESADVISORY")
                if raw_adv is not None:
                    stripped = str(raw_adv).strip()
                    if str(raw_adv) != stripped:
                        if not should_write_audio_tag(config, "ITUNESADVISORY", filepath=d["af"].path):
                            continue
                        # Only write trimmed if the trimmed value is valid 0/1/2 or empty
                        # If it's invalid like " 3 ", we still trim to "3" so grading can flag the value, not the spaces
                        if d["af"].set_tag("ITUNESADVISORY", stripped):
                            modified += 1
                            d["advisory"] = stripped
            except Exception:
                pass

        # 0) MusicBrainz release identity (album-level facts + ids). Fill-only,
        # so it runs FIRST — the stages below derive values from what is on the
        # file and must see the tags this one just settled.
        release_modified, release_note = _fill_release_tags(info, config, album)
        release_written.append(release_modified)
        modified += release_modified
        if release_note:
            notes.append(release_note)

        # 1) Fix INSTRUMENTAL first (correct order): the external
        # cross-reference (LRCLIB, Spotify when configured, the track's own
        # name — lyrics evidence is one of its sources too), then the lyrics
        # evidence alone for anything it could not state.
        instrumental_modified = 0
        if do_instrumental:
            detected = {}
            if config.get("instrumental_auto_fetch", True):
                detected = _instrumental_fetch(
                    [d["af"].path for d in info
                     if d["instrumental"] not in ("0", "1")], config)
            for d in info:
                hit = detected.get(os.path.normpath(d["af"].path))
                target = hit.get("value") if hit is not None else None
                if target is None and d["has_lyrics"]:
                    target = 0          # lyrics are evidence of vocals
                if target is None or d["instrumental"] == str(target):
                    continue
                if not should_write_audio_tag(config, "INSTRUMENTAL", filepath=d["af"].path):
                    continue
                if d["af"].set_tag("INSTRUMENTAL", str(target)):
                    modified += 1
                    instrumental_modified += 1
                    d["instrumental"] = str(target)
            if instrumental_modified:
                notes.append("instrumental")

        # 2) Derive ALBUMITUNESADVISORY from current per-track advisories (respect per-type gate)
        advisory_modified = 0
        if do_advisory:
            # Only include advisories for tracks where we can write album advisory, or where advisory is enabled
            advisories_for_derive = [d["advisory"] for d in info]
            advisory_value = _derive_advisory(advisories_for_derive)
            # Check if write needed (filter to writable files)
            need_write = []
            for d in info:
                if d["album_advisory"] != str(advisory_value) and should_write_audio_tag(config, "ALBUMITUNESADVISORY", filepath=d["af"].path):
                    need_write.append(d)
            # When force is True, rewrite even if already correct (count as modified for stats)
            write_list = need_write if not force else [d for d in info if should_write_audio_tag(config, "ALBUMITUNESADVISORY", filepath=d["af"].path)]
            if write_list:
                for d in write_list:
                    if d["af"].set_tag("ALBUMITUNESADVISORY", str(advisory_value)):
                        modified += 1
                        advisory_modified += 1
                        d["album_advisory"] = str(advisory_value)
                if advisory_modified:
                    notes.append(f"advisory={advisory_value}")

        # 3) Auto-zero ITUNESADVISORY for instrumental tracks (must run AFTER instrumental fix + advisory derive)
        zero_modified = 0
        if do_zero_advisory_for_instrumental:
            for d in info:
                is_instrumental = (d["instrumental"] == "1")
                # Only zero if instrumental and advisory is explicit/safe or empty; leave invalid "3" untouched
                if is_instrumental and d["advisory"] in ("1", "2", ""):
                    if not should_write_audio_tag(config, "ITUNESADVISORY", filepath=d["af"].path):
                        continue
                    if d["af"].set_tag("ITUNESADVISORY", "0"):
                        modified += 1
                        zero_modified += 1
                        d["advisory"] = "0"
            if zero_modified:
                notes.append("zero advisory for instrumental")
                # Re-derive album advisory if we zeroed any track (album may need to go from 1/2 -> 0)
                if do_advisory:
                    new_val = _derive_advisory(d["advisory"] for d in info)
                    if new_val != advisory_value:
                        for d in info:
                            if d["album_advisory"] != str(new_val) and should_write_audio_tag(config, "ALBUMITUNESADVISORY", filepath=d["af"].path):
                                if d["af"].set_tag("ALBUMITUNESADVISORY", str(new_val)):
                                    modified += 1
                        advisory_value = new_val
                        # Update note if advisory already appended
                        # Replace last advisory note if present
                        for i, n in enumerate(notes):
                            if n.startswith("advisory="):
                                notes[i] = f"advisory={new_val}"
                                break

        # The album's ONE write per file (see the deferral at the top). A
        # failed flush wrote nothing at all — every tag above was applied in
        # memory only — so it is reported by name instead of being counted as
        # modified, and the next pass over the album fills it again.
        failed = [os.path.basename(af.path) for af in deferred
                  if af.defer_save(False) is not True]
        if failed:
            notes.append("the container write failed: "
                         + ", ".join(failed[:3])
                         + (f" (+{len(failed) - 3} more)"
                            if len(failed) > 3 else ""))
        return album, modified, notes, advisory_value, info

    def mood_genre_for_track(item):
        """The two PER-TRACK stages of Auto tagging: the GENRE cap and MOOD.

        Split out of the album loop so they can run on a pool of their own:
        this is the decode-bound half of the script (librosa, seconds per
        track), and keeping it behind one album's worker meant a run over a
        single album — every import — analysed its tracks strictly one after
        another no matter how many workers the run was allowed. Script 16
        (Mood & Energy) has always pooled per file; this is the same shape.
        """
        album, d = item
        af = d["af"]
        path = af.path
        mood_modified = 0
        genre_trimmed = 0
        if do_genre:
            # GENRE is never IMPORTED here: this script does not ask any
            # provider for a genre, and it does not invent the family of
            # what it finds either. Genres come from the import pipeline
            # (server/imports.py, the genre chain) or from a manual edit —
            # a background pass that silently rewrites a deliberate tag is
            # exactly what the user does not want. What is left is the one
            # job the writer must do on every track: bring a list that
            # exceeds `mb_genre_count` down to it, the same cap the
            # importer, the format pass and the grader use.
            try:
                if trim_genres(af, genre_cap):
                    genre_trimmed += 1
            except Exception:
                pass
        if do_mood:
            try:
                # The decode is the expensive part (librosa, seconds per
                # track), so an already-correct tag short-circuits exactly
                # like the GENRE branch above: re-running Auto tagging, or
                # importing the same album again, must not re-analyse the
                # library for nothing. A track tagged before ENERGY
                # existed is analysed once more to backfill it.
                has_mood = bool(str(af.get_tag("MOOD") or "").strip())
                wants_energy = should_write_audio_tag(
                    config, "ENERGY", filepath=path)
                has_energy = bool(str(af.get_tag("ENERGY") or "").strip())
                if force or not has_mood or (wants_energy and not has_energy):
                    genre = af.get_tag("GENRE") or ""
                    if moods.apply_mood_tags(af, path, config, genre=genre):
                        mood_modified += 1
            except Exception:
                pass
        return album, mood_modified, genre_trimmed

    counts = {"ok": 0, "skip": 0, "fail": 0}
    pbar = _make_pbar(len(album_dirs), "AutoTag", unit="album")
    workers = worker_count(config, default=8, maximum=8, items=len(album_dirs))
    # Stage 1: the album-level work (release identity, instrumental, advisory).
    album_results = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(process_album, a): a for a in sorted(album_dirs)}
        for fut in as_completed(futures):
            album = futures[fut]
            try:
                album_results[album] = fut.result()
            except Exception as e:
                album_results[album] = e
    # Stage 2: every track of every album, one pool — see mood_genre_for_track.
    per_album = {}
    track_work = [(album, d)
                  for album in sorted(album_results)
                  for d in ((album_results[album][4] or [])
                            if not isinstance(album_results[album], Exception) else [])]
    if track_work:
        # Sized by TRACKS, not albums: the work waiting here is one decode per
        # track, and an album count of 1 would otherwise pin a 15-track album
        # to a single lane (the import case, exactly).
        track_workers = worker_count(config, default=8, maximum=8,
                                     items=len(track_work))
        with ThreadPoolExecutor(max_workers=track_workers) as ex:
            futures = {ex.submit(mood_genre_for_track, item): item[0]
                       for item in track_work}
            for fut in as_completed(futures):
                try:
                    album, mood_modified, genre_trimmed = fut.result()
                except Exception:
                    continue
                tally = per_album.setdefault(album, [0, 0])
                tally[0] += mood_modified
                tally[1] += genre_trimmed
    for album in sorted(album_dirs):
        result = album_results.get(album)
        if isinstance(result, Exception) or result is None:
            stats["total_scanned"] += 1
            stats["error_count"] += 1
            stats["errors"].append(
                (os.path.basename(album), str(result or "no result")))
            _pbar_update(pbar, counts, kind="fail")
            continue
        _album, modified, notes, advisory_value, _info = result
        notes = list(notes or [])
        mood_modified, genre_trimmed = per_album.get(album, (0, 0))
        modified = (modified or 0) + mood_modified + genre_trimmed
        if mood_modified:
            notes.append("mood")
        if genre_trimmed:
            notes.append(f"genre trimmed to {genre_cap}")
        if modified:
            stats["total_scanned"] += 1
            stats["modified_count"] += 1
            log(f"  {os.path.basename(_album)} ({', '.join(notes)})")
            _pbar_update(pbar, counts, kind="ok")
        else:
            stats["skipped_count"] += 1
            _pbar_skip(pbar, counts)

    if pbar:
        pbar.close()
    # The runner's own summary: how many release-identity tags this pass
    # filled, or that there was nothing to fill.
    release_total = sum(release_written)
    filled_albums = sum(1 for n in release_written if n)
    stats["release_tags_written"] = release_total
    log("  MusicBrainz release identity: " + (
        f"{release_total} tag(s) filled across {filled_albums} album(s)"
        if release_total else "nothing to fill"))
    stats["is_grader"] = False
    return stats
