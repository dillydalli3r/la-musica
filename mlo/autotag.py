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
)

# Per-track slots this stage matches against the release (or the manifest):
# the recording id of the track's own position, and the artist it credits.
_PER_TRACK_TAGS = ("MUSICBRAINZ_TRACKID", "MUSICBRAINZ_ARTISTID")

# release_lookup's own `inc` list, so the album's release comes back with its
# label-info (label + catalog numbers) and the release group's types in ONE
# request that the browser cache then holds for every later album.
_RELEASE_INC = ("artists+recordings+media+release-groups+artist-credits"
                "+genres+labels+isrcs")


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
            tracks[(disc, int(pos))] = {
                "recording_mbid": str(rec.get("id") or ""),
                "artist_mbid": str(artists[0] if artists else ""),
                # The id of this POSITION (distinct from the recording) and
                # the ISRCs MusicBrainz knows for it: `inc` above already
                # fetched both, and they were parsed away — the naming script
                # reads the first, the ISRC tag the second.
                "release_track_mbid": str(trk.get("id") or ""),
                "isrcs": [str(x) for x in (trk.get("isrcs") or [])],
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


def _fill_release_tags(info, config, album_dir):
    """Fill each track's EMPTY MusicBrainz release identity tags.

    Album-level facts the naming script reads per track (label, catalog
    number, country, type, both DATES, medium + the release's own ids) are
    written to EVERY file of the album; the per-track ones (recording id, and
    the credited artist id) come from a POSITION match — the album's own
    `.mlo_expected.json` manifest first (it records the release the wizard
    matched), then the release payload's tracklist. A track with no
    counterpart is left alone and counted.

    A tag that already holds a value is never touched — another pressing's
    label, or ids another tagger wrote, are the album's own business — with
    TWO exceptions. DATE and ORIGINALDATE are SHARPENED to MusicBrainz's
    spelling when the tag holds a coarser form of the same date ("1980" →
    "1980-10-01", see fuller_date): those two name the album folder, so a
    year-only value would otherwise keep it a year forever. RELEASECOUNTRY is
    WIDENED to the release's whole country set when the value it holds is a
    strict subset of it ("US" → "US; CA; XE", see _country_upgrade): the tag
    holds a LIST and a file written before it could would otherwise keep the
    release's first event forever. Returns (written, note) — the album's
    report line, including the "nothing written" cases.
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

    values = []
    for tag, key in _RELEASE_TAGS + _EXTRA_RELEASE_TAGS:
        # RELEASECOUNTRY is the one tag whose value is a LIST — every country
        # the release states, its own first event first. set_tag writes a list
        # as repeated fields (Vorbis comments, an ID3 text list, one MP4 atom
        # per value) and get_tag reads them back "; "-joined, which is the
        # same value the importer's own stamper writes.
        value = (_release_country_codes(release) if tag == "RELEASECOUNTRY"
                 else str(release.get(key) or "").strip())
        if value:
            values.append((tag, value))

    # recording id per (disc, position): the manifest wins — its release_id is
    # the one the album was matched against
    manifest_ids = {t["disc"] * 1000 + t["position"]: t["recording_mbid"]
                    for t in manifest["tracks"] if t.get("recording_mbid")}
    if not values and not release["tracks"] and not manifest_ids:
        return 0, "release tags: release carries none"

    written = 0
    unmatched = 0
    for d in info:
        af = d["af"]
        key = _track_position(af, af.path)
        slot = release["tracks"].get(key) or {}
        track_mbid = (manifest_ids.get(key[0] * 1000 + key[1])
                      or slot.get("recording_mbid") or "")
        if not track_mbid and not slot:
            unmatched += 1
        per_track = [
            ("MUSICBRAINZ_TRACKID", track_mbid),
            ("MUSICBRAINZ_ARTISTID",
             slot.get("artist_mbid") or release["album_artist_mbid"]),
            # The id of this track's POSITION on this release (the recording
            # id above is a different id) and the ISRC of the track: both
            # arrived with the same payload and are filled only where empty,
            # gate included, through the same loop below.
            ("MUSICBRAINZ_RELEASETRACKID", slot.get("release_track_mbid") or ""),
            ("ISRC", (slot.get("isrcs") or [""])[0]),
        ]
        # ONE container rewrite per file. Each set_tag used to save the whole
        # file for itself, so filling twelve tags on a 30 MB track rewrote it
        # twelve times; the flush below is where all of them land. A handle
        # that only implements the get/set contract (a caller's stub) simply
        # writes per tag, like before.
        defer = hasattr(af, "defer_save")
        if defer:
            af.defer_save(True)
        pending = 0
        try:
            for tag, value in values + per_track:
                try:
                    have = str(af.get_tag(tag) or "").strip()
                    if have:
                        # A tag that already holds a value is never overwritten.
                        # Two exceptions. A DATE MusicBrainz spells more
                        # precisely: the album folder is named after it, so a
                        # bare year would otherwise pin the folder there for
                        # good — fuller_date can only add detail. And a
                        # RELEASECOUNTRY holding a strict subset of the
                        # release's countries, which gains the rest —
                        # _country_upgrade only ever widens, so a country the
                        # release does not state survives it.
                        if tag == "RELEASECOUNTRY":
                            value = _country_upgrade(have, value)
                        else:
                            value = fuller_date(have, value)
                        if not value:
                            continue
                    if not str(value or "").strip():
                        # An empty answer is not a value. Writing it produced a
                        # blank tag AND counted as "written" on every run — an
                        # unmatched track, or a release that simply has no
                        # artist/ISRC/status for this file, looked tagged.
                        continue
                    if not should_write_audio_tag(config, tag, filepath=af.path):
                        continue      # the same gate every write here honours
                    if af.set_tag(tag, value):
                        pending += 1
                except Exception:
                    continue
        finally:
            # The one write of this file. A failed flush wrote nothing, so the
            # tags never reached the disk and must not be reported as written;
            # a Ctrl+C between here and the loop's end still flushes what the
            # loop already applied instead of dropping it.
            if defer and af.defer_save(False) is not True:
                pending = 0
        written += pending
    if not written:
        return 0, "release tags: nothing to fill"
    note = f"release tags={written}"
    if unmatched:
        note += f" ({unmatched} track(s) not in the release)"
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
    log("  MUSICBRAINZ release identity: label, catalog number, country, type, "
        "medium + missing MBIDs (release id, release-group id, artist ids, "
        "per-track recording id) filled from the cached release — only where "
        "a tag is EMPTY")
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

        return album, modified, notes, advisory_value, info

    def process_album_full(album):
        """`process_album` plus the mood/genre stages.

        The mood classifier and the genre hook both need the tags the first
        pass already read, so they reuse its parsed handles — a track is
        never opened twice (the librosa decode is the expensive part).
        """
        _, modified, notes, advisory_value, info = process_album(album)
        notes = list(notes or [])

        mood_modified = 0
        genre_trimmed = 0
        for d in info:
            af = d["af"]
            path = af.path
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
                    if not force and has_mood and (not wants_energy or has_energy):
                        continue
                    genre = af.get_tag("GENRE") or ""
                    if moods.apply_mood_tags(af, path, config, genre=genre):
                        mood_modified += 1
                except Exception:
                    continue

        modified = (modified or 0) + mood_modified + genre_trimmed
        if mood_modified:
            notes.append("mood")
        if genre_trimmed:
            notes.append(f"genre trimmed to {genre_cap}")
        return album, modified, notes, advisory_value, info

    counts = {"ok": 0, "skip": 0, "fail": 0}
    pbar = _make_pbar(len(album_dirs), "AutoTag", unit="album")
    workers = worker_count(config, default=8, maximum=8, items=len(album_dirs))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(process_album_full, a): a for a in sorted(album_dirs)}
        for fut in as_completed(futures):
            album = futures[fut]
            try:
                _album, modified, notes, advisory_value, _info = fut.result()
            except Exception as e:
                stats["total_scanned"] += 1
                stats["error_count"] += 1
                stats["errors"].append((os.path.basename(album), str(e)))
                _pbar_update(pbar, counts, kind="fail")
                continue
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
