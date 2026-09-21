"""Everything this app knows about a tag, derived from the code that owns it.

The same fact about one tag used to be written down in four places that could
drift apart: the write matrix (``mlo.config.audio_tag_writes``), the grader's
check list (``mlo.grader``), the tag vocabulary (``mlo.audio.TAG_MAP``) and the
web UI's own hand-kept labels — the track page's field list, the bulk dialog's
removable chips, the Grading page's check rows. Genre made it visible: the
model changed in one place and the editors kept their own idea of it.

So this module owns NO tag facts of its own beyond two editorial ones — the
human label and the one-line meaning — and derives everything else:

  key / spellings   ``mlo.audio.TAG_MAP`` (plus the encoder markers the
                    Optimize script writes), with every file-side spelling the
                    tag API can emit built from the container specs.
  family            ``mlo.config._TAG_TO_FAMILY`` for the write-gate family;
                    the display family is the table below (it is the one thing
                    no other module states).
  writer            the script table (``mlo.cli.SCRIPTS``) — never a raw "8".
  graded_by         ``mlo.grader``'s own tables (``PER_TRACK_TAGS``,
                    ``ALBUM_TAGS``, ``TAG_PRESENCE_CHECKS``) plus ``TAG_CHECKS``
                    below, for the tags graded by a named check rather than a
                    list; every key is verified against ``DEFAULT_CONFIG`` at
                    build time, so a renamed check fails loudly here instead of
                    silently vanishing from the UI.
  write_gate        ``mlo.config.should_write_audio_tag`` asked per filetype,
                    with the master switches PROBED out of it (see
                    ``_family_switches``) rather than copied from its body.
  excess            ``mlo.grader.tag_key_allowed`` — the same predicate the
                    Optimize / Format All strip passes apply. The registry
                    ships its allow-list (``TAG_ALLOWLIST``, the grader's own)
                    plus every spelling it accepts per tag, so the client asks
                    the same question instead of re-implementing it.

``GET /api/tags/registry`` serves the whole thing, built once and cached.
"""
from __future__ import annotations

import re
import threading

from mlo.audio import TAG_MAP, _mp4_specs, _mp3_specs
from mlo.cli import SCRIPT_LABELS
from mlo.config import (AUDIO_TAG_TYPES, DEFAULT_CONFIG, _LYRICS_PREFIXES,
                        _audio_tag_family, should_write_audio_tag)
from mlo.grader import (ALBUM_TAGS, KNOWN_MEDIA, PER_TRACK_TAGS, TAG_ALLOWLIST,
                        TAG_PRESENCE_CHECKS, tag_key_allowed)

# --------------------------------------------------------------------------- #
# The display families — the one grouping no other module states, so it lives
# here and every page reads it instead of keeping its own field list.
# --------------------------------------------------------------------------- #
FAMILIES = (
    ("identity", "Identity"),
    ("release", "Release"),
    ("audio", "Audio"),
    ("lyrics", "Lyrics"),
    ("provenance", "Provenance"),
    # What the LISTENER said, not what the app measured or concluded: a rating
    # is an opinion, and grouping it with any measurement would imply it can be
    # graded, recomputed or overwritten by a pass. Nothing does.
    ("opinion", "Your opinion"),
)

TAG_FAMILY = {
    # What names the track, its people and what it is.
    "TITLE": "identity", "ARTIST": "identity", "ALBUMARTIST": "identity",
    "ALBUMARTISTSORT": "identity", "ARTISTSORT": "identity",
    "TITLESORT": "identity", "TRACKNUMBER": "identity",
    "DISCNUMBER": "identity", "COMPILATION": "identity", "GENRE": "identity",
    "WORK": "identity", "MOVEMENT": "identity",
    "MOVEMENTNUMBER": "identity", "COMPOSER": "identity",
    "LYRICIST": "identity", "REMIXER": "identity", "COMMENT": "identity",
    "COPYRIGHT": "identity", "ITUNESADVISORY": "identity",
    # The release the track came from, its identifiers and its medium.
    "ALBUM": "release", "DATE": "release", "ORIGINALDATE": "release",
    "ORIGINALYEAR": "release", "RELEASETYPE": "release",
    "RELEASESTATUS": "release", "RELEASECOUNTRY": "release",
    "CATALOGNUMBER": "release", "BARCODE": "release", "SCRIPT": "release",
    "LABEL": "release", "TRACKTOTAL": "release", "DISCTOTAL": "release",
    "ISRC": "release", "LICENSE": "release", "MEDIA": "release",
    "SOURCE": "release", "ALBUMITUNESADVISORY": "release",
    "MUSICBRAINZ_ALBUMID": "release",
    "MUSICBRAINZ_ALBUMARTISTID": "release",
    "MUSICBRAINZ_ARTISTID": "release",
    "MUSICBRAINZ_TRACKID": "release",
    "MUSICBRAINZ_RELEASEID": "release",
    "MUSICBRAINZ_RELEASEGROUPID": "release",
    "MUSICBRAINZ_RELEASETRACKID": "release",
    "MUSICBRAINZ_WORKID": "release",
    "RATEYOURMUSIC_ALBUM": "release", "RATEYOURMUSIC_TRACK": "release",
    "RATEYOURMUSIC_ARTIST": "release",
    # What the analysis passes measured on the audio itself.
    "MOOD": "audio", "ENERGY": "audio", "BPM": "audio", "INITIALKEY": "audio",
    "DYNAMIC RANGE": "audio", "ALBUM DYNAMIC RANGE": "audio",
    "REPLAYGAIN_TRACK_GAIN": "audio", "REPLAYGAIN_TRACK_PEAK": "audio",
    "REPLAYGAIN_ALBUM_GAIN": "audio", "REPLAYGAIN_ALBUM_PEAK": "audio",
    "INSTRUMENTAL": "audio", "ACOUSTID_ID": "audio",
    "ACOUSTID_FINGERPRINT": "audio",
    # The lyrics and their language transforms.
    "LYRICS": "lyrics", "UNSYNCEDLYRICS": "lyrics",
    "TRANSLATION": "lyrics", "TRANSLITERATION": "lyrics",
    # What this app's own audited pipeline concluded about the file.
    "AUDIT": "provenance", "AUDIOAUDITOR_OVERRIDE": "provenance",
    "INTEGRITY": "provenance", "AUDIO_MD5": "provenance",
    "LOG_CRC": "provenance", "LOG_GRADE": "provenance",
    "ENCODER_PROGRAM": "provenance", "ENCODER_QUALITY": "provenance",
    "ENCODER_VERSION": "provenance",
    # The listener's own stars — see FAMILIES above.
    "RATING": "opinion",
}

# A tag added to TAG_MAP without a family above lands here instead of in a
# wrong group: a new tag is visibly unclassified rather than silently misfiled.
FAMILY_OTHER = "other"

# --------------------------------------------------------------------------- #
# Labels and meanings — the only hand-written tag prose left, so a label
# exists once and every page reads it from here.
# --------------------------------------------------------------------------- #
TAG_INFO = {
    "TITLE": ("Title", "The track's own name."),
    "ARTIST": ("Artist", "The performing artist credited on this track."),
    "ALBUMARTIST": ("Album artist", "Who the release is filed under — one value across the album."),
    "ALBUMARTISTSORT": ("Album artist sort", "Sort spelling of the album artist (The Beatles → Beatles)."),
    "ARTISTSORT": ("Artist sort", "Sort spelling of the track artist."),
    "TITLESORT": ("Title sort", "Sort spelling of the title, articles moved to the end."),
    "TRACKNUMBER": ("Track #", "Position on the disc; part of the file name the naming script computes."),
    "DISCNUMBER": ("Disc #", "Which disc of a multi-disc release — required only when the album has several."),
    "COMPILATION": ("Compilation", "1 on a various-artists release, so players group it as one album."),
    "GENRE": ("Genre", "A LIST: the specific genres first, the family last. Names are MusicBrainz spellings."),
    "WORK": ("Work", "The classical composition this track performs."),
    "MOVEMENT": ("Movement", "Movement title within the work."),
    "MOVEMENTNUMBER": ("Movement #", "Movement position within the work."),
    "COMPOSER": ("Composer", "Who wrote the music (the performer is ARTIST)."),
    "LYRICIST": ("Lyricist", "Who wrote the words."),
    "REMIXER": ("Remixer", "Who remixed this version."),
    "COMMENT": ("Comment", "Free-text note on the file."),
    "COPYRIGHT": ("Copyright", "The release's copyright line."),
    "ITUNESADVISORY": ("Advisory", "Content rating: 0 clean, 1 explicit, 2 cleaned."),
    "ALBUM": ("Album", "The release's title, one value across its tracks."),
    "DATE": ("Date", "Release date (YYYY, YYYY-MM or YYYY-MM-DD); names the album folder."),
    "ORIGINALDATE": ("Original date", "When the recording was first released, when that differs from DATE."),
    "ORIGINALYEAR": ("Original year", "Four-digit year of that original release."),
    "RELEASETYPE": ("Release type", "Album / single / EP / live… — the folder layout the organizer writes."),
    "RELEASESTATUS": ("Release status", "Official / promotion / bootleg…"),
    "RELEASECOUNTRY": ("Country", "Country of the release."),
    "CATALOGNUMBER": ("Catalog #", "Label catalogue number of the release."),
    "BARCODE": ("Barcode", "The release's barcode."),
    "SCRIPT": ("Script", "Writing system of the release's language (Latn, Jpan, …)."),
    "LABEL": ("Label", "Record label that published the release."),
    "TRACKTOTAL": ("Track total", "How many tracks the release holds."),
    "DISCTOTAL": ("Disc total", "How many discs the release holds."),
    "ISRC": ("ISRC", "The recording's ISRC — how the advisory sources identify a track."),
    "LICENSE": ("License", "Licence the release is published under."),
    "MEDIA": ("Media", "Medium of the release (CD, vinyl, digital media…) — decides which CD checks apply."),
    "SOURCE": ("Source", "Where this rip came from; required when MEDIA is digital media."),
    "ALBUMITUNESADVISORY": ("Album advisory", "The strictest per-track advisory, repeated on every track of the album."),
    "MUSICBRAINZ_ALBUMID": ("MusicBrainz release", "The release's MusicBrainz id — what the import, the cover search and the album page resolve."),
    "MUSICBRAINZ_ALBUMARTISTID": ("MB album artist id", "MusicBrainz artist id of the album artist."),
    "MUSICBRAINZ_ARTISTID": ("MB track artist id", "MusicBrainz artist id of the track artist."),
    "MUSICBRAINZ_TRACKID": ("MB recording id", "MusicBrainz recording id of this track."),
    "MUSICBRAINZ_RELEASEID": ("MB release id", "MusicBrainz release id (the older of the two release spellings)."),
    "MUSICBRAINZ_RELEASEGROUPID": ("MB release group id", "The release group the album belongs to."),
    "MUSICBRAINZ_RELEASETRACKID": ("MB release track id", "This track's own position id on this release."),
    "MUSICBRAINZ_WORKID": ("MB work id", "MusicBrainz work id the classical tags sit beside."),
    "RATEYOURMUSIC_ALBUM": ("RYM album link", "RateYourMusic release page URL."),
    "RATEYOURMUSIC_TRACK": ("RYM track link", "RateYourMusic track page URL."),
    "RATEYOURMUSIC_ARTIST": ("RYM artist link", "RateYourMusic artist page URL."),
    "MOOD": ("Mood", "The mood word the classifier derived from the track's own audio."),
    "ENERGY": ("Energy", "0-100 arousal the MOOD verdict was scored from."),
    "BPM": ("BPM", "Tempo in beats per minute."),
    "INITIALKEY": ("Initial key", "The musical key the track sits in."),
    "DYNAMIC RANGE": ("Dynamic range", "Per-track DR measured by simple-dr-meter."),
    "ALBUM DYNAMIC RANGE": ("Album dynamic range", "One DR value for the whole release."),
    "REPLAYGAIN_TRACK_GAIN": ("ReplayGain track gain", "Track loudness gain in dB, from rsgain."),
    "REPLAYGAIN_TRACK_PEAK": ("ReplayGain track peak", "Peak sample of the track, for clipping-safe replay."),
    "REPLAYGAIN_ALBUM_GAIN": ("ReplayGain album gain", "Album-wide gain, so tracks keep their relative loudness."),
    "REPLAYGAIN_ALBUM_PEAK": ("ReplayGain album peak", "Peak sample of the album."),
    "INSTRUMENTAL": ("Instrumental", "1 when no vocals are present; a 1 next to lyrics fails grading."),
    "ACOUSTID_ID": ("AcoustID id", "AcoustID identifier of the recording."),
    "ACOUSTID_FINGERPRINT": ("AcoustID fingerprint", "Chromaprint fingerprint the AcoustID id was matched from."),
    "LYRICS": ("Lyrics", "The lyrics text, embedded in the file (timed or plain)."),
    "UNSYNCEDLYRICS": ("Unsynced lyrics", "Lyrics kept as plain text rather than the canonical LYRICS field."),
    "TRANSLATION": ("Translation", "Translated lyrics; the language lives in the tag name (TRANSLATION-EN)."),
    "TRANSLITERATION": ("Transliteration", "Romanised lyrics; the language lives in the tag name (TRANSLITERATION-JA-Latn)."),
    "AUDIT": ("Audit verdict", "AudioAuditor's REAL / FAKE verdict on the file's audio."),
    "AUDIOAUDITOR_OVERRIDE": ("Audit override", "A hand-set REAL / FAKE that wins over every derived verdict."),
    "INTEGRITY": ("Integrity verdict", "OK / FAIL from the audio integrity test."),
    "AUDIO_MD5": ("Audio MD5", "Legacy integrity checksum; nothing writes it any more."),
    "LOG_CRC": ("Log CRC", "OK / MISMATCH against the rip log's own per-track CRC."),
    "LOG_GRADE": ("Log score", "0-100 score the rip log earned."),
    "ENCODER_PROGRAM": ("Encoder program", "Which encoder produced the file (FLAC only)."),
    "ENCODER_QUALITY": ("Encoder quality", "Encoder setting the file was produced with."),
    "ENCODER_VERSION": ("Encoder version", "Version of that encoder."),
    "RATING": ("Rating", "Your own stars — 0-5 with halves — stored in the file as Picard's RATING, "
                          "0-100 (one half-star = 10). An opinion, so nothing grades it; the app keeps "
                          "its own copy and heals it after a rename."),
}

# --------------------------------------------------------------------------- #
# Writers, named through the script table so a label never drifts from the id.
# --------------------------------------------------------------------------- #
def _script(n: int) -> str:
    return f"{SCRIPT_LABELS[n]} ({n})"


# Beets (script 14) is the tagger that stamps standard MusicBrainz metadata; the
# import wizard writes the same names through server.integrations.
_RELEASE_WRITER = f"{_script(14)} · import"
_AUDIO_WRITER = f"{_script(7)}"
_AUTOTAG = _script(8)
_MOODS = _script(16)
_LYRICS = _script(13)

TAG_WRITER = {
    "MEDIA": _script(1) + " · media/source normalization",
    "SOURCE": _script(1) + " · media/source normalization",
    "ITUNESADVISORY": f"{_AUTOTAG} · advisory fetch",
    "ALBUMITUNESADVISORY": _AUTOTAG,
    "INSTRUMENTAL": f"{_AUTOTAG} · instrumental fetch",
    "GENRE": f"{_AUTOTAG} · genre import · {_script(10)} trims",
    "MOOD": f"{_AUTOTAG} · {_MOODS}",
    "ENERGY": f"{_AUTOTAG} · {_MOODS}",
    "BPM": _script(12),
    "INITIALKEY": _script(12),
    "DYNAMIC RANGE": _AUDIO_WRITER,
    "ALBUM DYNAMIC RANGE": _AUDIO_WRITER,
    "REPLAYGAIN_TRACK_GAIN": _AUDIO_WRITER,
    "REPLAYGAIN_TRACK_PEAK": _AUDIO_WRITER,
    "REPLAYGAIN_ALBUM_GAIN": _AUDIO_WRITER,
    "REPLAYGAIN_ALBUM_PEAK": _AUDIO_WRITER,
    "AUDIT": _script(6),
    "LOG_GRADE": _script(6),
    "LOG_CRC": _script(6),
    "INTEGRITY": _script(6),
    "AUDIO_MD5": "nothing — legacy, read only",
    "AUDIOAUDITOR_OVERRIDE": "the track editor (manual) — beats every verdict",
    "LYRICS": f"{_LYRICS} · lyrics editor",
    "UNSYNCEDLYRICS": _LYRICS,
    "TRANSLATION": _script(17),
    "TRANSLITERATION": _script(17),
    "ACOUSTID_ID": _RELEASE_WRITER + " (fingerprint match)",
    "ACOUSTID_FINGERPRINT": _RELEASE_WRITER + " (fingerprint match)",
    "ENCODER_PROGRAM": _script(3),
    "ENCODER_QUALITY": _script(3),
    "ENCODER_VERSION": _script(3),
    # The ratings API (server.ratings) is the only writer, and it writes on the
    # click itself — no script pass touches an opinion.
    "RATING": "the ratings API (server.ratings) — the star you clicked",
}
DEFAULT_WRITER = _RELEASE_WRITER

# --------------------------------------------------------------------------- #
# Closed value sets, taken from the code that validates or produces them.
# --------------------------------------------------------------------------- #
# A value outside these is the anomaly the track page marks. Values that
# ARE a closed set in the code are read from it; the rest are stated here
# beside the check that owns them (grader's 0/1/2 advisory rule, mlo.audit's
# REAL/FAKE, the seven-plus-one mood words in mlo.moods).
_TAG_ENUM = {
    "ITUNESADVISORY": ("0", "1", "2"),
    "ALBUMITUNESADVISORY": ("0", "1", "2"),
    "INSTRUMENTAL": ("0", "1"),
    "AUDIT": ("REAL", "FAKE"),
    "AUDIOAUDITOR_OVERRIDE": ("REAL", "FAKE"),
    "INTEGRITY": ("OK", "FAIL"),
    "LOG_CRC": ("OK", "MISMATCH"),
}

# Open value RANGES — a scale rather than a closed set of answers, so a client
# can mark a value outside it as the anomaly without owning the scale. The
# RATING scale is Picard's own (server.ratings converts it to half-stars); a
# tag with no entry here states its range in its meaning line instead.
_TAG_RANGE = {
    "RATING": (0, 100),
}


def _range_for(tag: str):
    lo_hi = _TAG_RANGE.get(tag)
    return [lo_hi[0], lo_hi[1]] if lo_hi else None


def _enum_for(tag: str):
    if tag in _TAG_ENUM:
        return list(_TAG_ENUM[tag])
    if tag == "MEDIA":
        # The grader's own table, which its consistency check tests
        # case-insensitively — the client folds before comparing, as it does
        # for every enum value.
        return sorted(KNOWN_MEDIA)
    if tag == "MOOD":
        # mlo.moods owns the mood words; imported here because the module
        # pulls the audio analysis stack in with it.
        from mlo.moods import MOODS
        return list(MOODS)
    # GENRE is deliberately absent: the vocabulary is 2 200 MusicBrainz names
    # (see web/src/lib/genres.ts's same decision) and the grader already names
    # a bad one in its own GENRE_VOCAB issue — the client marks that verdict
    # instead of re-deriving it.
    return None


# --------------------------------------------------------------------------- #
# Checks: the named grade_check_* / grade_include_* keys, and which tags they
# grade. The tag side is derived from the grader's tables; the check side is
# read straight out of DEFAULT_CONFIG, so a check that exists there but not on
# the Grading page shows up instead of disappearing.
# --------------------------------------------------------------------------- #
CHECK_PREFIXES = ("grade_check_", "grade_include_")

# Tags graded by a named check that no grader TABLE names (the rest come from
# PER_TRACK_TAGS / ALBUM_TAGS / TAG_PRESENCE_CHECKS). Verified against
# DEFAULT_CONFIG at build time.
TAG_CHECKS = {
    "GENRE": ("grade_check_genre_count", "grade_check_genre_order",
              "grade_check_genre_vocab"),
    "INITIALKEY": ("grade_check_key_bpm",),
    "BPM": ("grade_check_key_bpm",),
    "ACOUSTID_ID": ("grade_check_acoustid",),
    "ACOUSTID_FINGERPRINT": ("grade_check_acoustid",),
    "AUDIT": ("grade_check_audit",),
    "AUDIOAUDITOR_OVERRIDE": ("grade_check_audit",),
    "LOG_GRADE": ("grade_check_log_grade",),
    "INSTRUMENTAL": ("grade_check_instrumental",),
    "LYRICS": ("grade_check_lyrics", "grade_check_lyrics_format"),
    "UNSYNCEDLYRICS": ("grade_check_lyrics",),
    "TRANSLATION": ("grade_check_xlit_translation",),
    "TRANSLITERATION": ("grade_check_xlit_transliteration",),
    "MUSICBRAINZ_ALBUMID": ("grade_check_mb_links",),
    "RATEYOURMUSIC_ALBUM": ("grade_check_rym_links",),
    "MEDIA": ("grade_check_media",),
    "SOURCE": ("grade_check_source",),
    "AUDIO_MD5": ("grade_check_excess_tags",),
    "INTEGRITY": ("grade_check_excess_tags",),
    "LOG_CRC": ("grade_check_excess_tags",),
    "ENCODER_PROGRAM": ("grade_check_encoder",),
    "ENCODER_QUALITY": ("grade_check_encoder",),
    "ENCODER_VERSION": ("grade_check_encoder",),
}

# Per-track issue CODES the grader can report against a tag — what the track
# page marks a value with. The presence halves come from the grader's tables;
# these are the ones a specific check appends on its own.
TAG_ISSUE_CODES = {
    "GENRE": ("GENRE_COUNT", "GENRE_ORDER", "GENRE_VOCAB"),
    "INSTRUMENTAL": ("LYRICS",),
    "LYRICS": ("XLIT_MISSING", "XLIT_UNNEEDED"),
    "TRANSLATION": ("XLIT_MISSING", "XLIT_UNNEEDED"),
    "TRANSLITERATION": ("XLIT_MISSING", "XLIT_UNNEEDED"),
    "MUSICBRAINZ_ALBUMID": ("MB_LINK",),
    "RATEYOURMUSIC_ALBUM": ("RYM_LINK",),
    "ACOUSTID_ID": ("ACOUSTID_ID",),
    "ACOUSTID_FINGERPRINT": ("ACOUSTID_FINGERPRINT",),
    "AUDIT": ("AUDIT",),
    "LOG_GRADE": ("LOG_GRADE",),
    "MEDIA": ("MEDIA",),
    "SOURCE": ("SOURCE",),
}

# Check labels, read by every page that lists checks. A key DEFAULT_CONFIG has
# and this table does not is labelled from its own name (see _humanize), so a
# new check is visible on the Grading page the moment it exists.
CHECK_LABELS = {
    "grade_check_unreadable": "Unreadable files",
    "grade_check_missing_tags": "Required tags",
    "grade_check_album_tags": "Album-level tags",
    "grade_check_mood": "Mood tag present",
    "grade_check_energy": "Energy tag present",
    "grade_check_genre": "Genre tag present",
    "grade_check_genre_count": "Genre count per track",
    "grade_check_genre_order": "Genre order (family last)",
    "grade_check_genre_vocab": "Genre vocabulary",
    "grade_check_replaygain": "ReplayGain tags present",
    "grade_check_encoder": "Encoder identity",
    "grade_check_naming": "Naming script match",
    "grade_check_filename_case": "Path capitalization",
    "grade_check_ext_case": "Lowercase extensions",
    "grade_check_key_bpm": "Key & BPM",
    "grade_check_acoustid": "AcoustID tags present",
    "grade_check_excess_tags": "Excess tags",
    "grade_check_media": "Media type",
    "grade_check_source": "Source tag",
    "grade_check_instrumental": "Instrumental consistency",
    "grade_check_disallowed": "Disallowed file types",
    "grade_check_extra_images": "Stray images",
    "grade_check_empty_folders": "Empty folders",
    "grade_check_expected_tracks": "Release tracklist manifest",
    "grade_check_album_description": "Album description stored",
    "grade_check_raw_video": "Raw videos",
    "grade_check_lossless_source": "Lossless sources",
    "grade_check_disc_naming": "Disc rip-sheet naming",
    "grade_check_cd_log": "CD — .log present",
    "grade_check_cd_cue": "CD — .cue present",
    "grade_check_cd_format": "CD — lossless format",
    "grade_check_crc": "CRC checksums",
    "grade_check_artist_image": "Artist image stored",
    "grade_check_artist_description": "Artist description stored",
    "grade_check_audit": "Require audit tag",
    "grade_check_log_checksum": "Log checksum valid",
    "grade_check_accuraterip": "AccurateRip verified (audit only)",
    "grade_check_log_grade": "Log grade present & in range",
    "grade_check_mb_links": "MusicBrainz release link",
    "grade_check_rym_links": "RateYourMusic release link",
    "grade_check_cover": "Cover art",
    "grade_check_cover_crop": "Cover aspect ratio (squareness)",
    "grade_check_sidecar_cover": "Per-track sidecar covers",
    "grade_check_tag_spaces": "Tags — no padding",
    "grade_check_tag_blank_lines": "Tags — no blank lines",
    "grade_check_lyrics_spaces": "Lyrics — no padding",
    "grade_check_lyrics_blank_lines": "Lyrics — blank line rules",
    "grade_check_lyrics_zero": "Lyrics — zero timestamp rule",
    "grade_check_lyrics_format": "Lyrics — canonical formatting",
    "grade_check_cue_spaces": "CUE — no padding",
    "grade_check_cue_blank_lines": "CUE — no blank lines",
    "grade_check_cue_format": "CUE — canonical formatting",
    "grade_check_accurip_format": ".accurip — canonical formatting",
    "grade_check_cue_files": "CUE — referenced files exist",
    "grade_check_lyrics": "Lyrics present",
    "grade_check_lyrics_lang_tags": "Transform language tags",
    "grade_check_xlit_transliteration": "Transliteration — needed, never extra",
    "grade_check_xlit_translation": "Translation — needed, never extra",
    "grade_include_music": "Audio tracks",
    "grade_include_cover": "Cover art",
    "grade_include_description": "Album description",
    "grade_include_cue": "CUE sheets",
    "grade_include_log": "Log files",
    "grade_include_lrc": "LRC lyrics",
    "grade_include_accurip": "AccurateRip files",
    "grade_include_video": "Remuxed videos",
    "grade_include_other": "Other files",
}

_ACRONYMS = {"mb": "MB", "rym": "RYM", "cue": "CUE", "cd": "CD", "lrc": "LRC",
             "id3": "ID3", "bpm": "BPM", "dr": "DR"}


def _humanize(key: str) -> str:
    """A label for a config key no table names — 'grade_check_foo_bar' →
    'Foo bar', so a new check is never invisible for want of a label."""
    tail = key
    for prefix in CHECK_PREFIXES:
        if tail.startswith(prefix):
            tail = tail[len(prefix):]
            break
    words = [w for w in tail.split("_") if w]
    return " ".join(_ACRONYMS.get(w, w.capitalize()) for w in words) or key


# --------------------------------------------------------------------------- #
# Derivations
# --------------------------------------------------------------------------- #
def _tag_spellings():
    """key -> the file-side spellings the tag API can emit for it.

    Built from TAG_MAP's container specs (the vorbis name, each ID3 frame /
    TXXX description / UFID owner, each MP4 atom / freeform name) and filtered
    through ``tag_key_allowed``: what ships is what the grader's own predicate
    accepts, so a client testing membership asks the same question a strip pass
    does."""
    out = {}
    for name, spec in TAG_MAP.items():
        forms = {str(spec.get("flac") or "")}
        for frame, desc in _mp3_specs(spec):
            forms.add(f"{frame}:{desc}" if desc else str(frame))
        for atom in _mp4_specs(spec):
            if isinstance(atom, tuple) and atom[0] == "freeform":
                forms.add(f"----:{atom[1]}:{atom[2]}")
            elif not isinstance(atom, tuple):
                forms.add(str(atom))
        out[name] = sorted(f for f in forms if f and tag_key_allowed(f))
    return out


def _norm_spelling(form: str) -> str:
    """A file-side spelling folded the way mlo.grader compares names.

    Mirrors ``mlo.grader._tag_key_norm`` (drop case, spaces and underscores)
    plus the wrapper rule of ``tag_key_allowed``: the name inside a
    "TXXX:desc" / "----:vendor:name" wrapper is what gets compared."""
    text = str(form)
    if ":" in text:
        text = text.rsplit(":", 1)[-1]
    return re.sub(r"[\s_]+", "", text).upper()


def _encoder_spellings(key: str):
    """The ENCODER_* markers: a vorbis name plus the TXXX / freeform atom a
    beets-tagged MP4 or an older rip carries."""
    forms = {key, f"TXXX:{key}", f"----:com.apple.iTunes:{key}"}
    return sorted(f for f in forms if tag_key_allowed(f))


_family_switches_cache = {}


def _family_switches(tag: str, family: str):
    """The DEFAULT_CONFIG switches that gate *family*'s writes.

    ``should_write_audio_tag`` keeps its family→master-switch map inside the
    function body, so the registry PROBES it instead of copying it: turn one
    boolean key off, ask again, and a switch that flipped the answer is one.
    A family gated by a COMBINATION (INSTRUMENTAL needs either of two switches
    on) reports none — the per-filetype answer below still comes straight from
    the function."""
    if not family:
        return []
    if family in _family_switches_cache:
        return _family_switches_cache[family]
    switches = []
    for key, value in DEFAULT_CONFIG.items():
        if not isinstance(value, bool):
            continue
        trial = dict(DEFAULT_CONFIG)
        trial[key] = False
        if not should_write_audio_tag(trial, tag, filetype="flac"):
            switches.append(key)
    _family_switches_cache[family] = sorted(switches)
    return _family_switches_cache[family]


def _write_gate(tag: str):
    family = _audio_tag_family(tag)
    return {
        "family": family,
        # Asked, not assumed: the same call every writer makes, per filetype,
        # with the shipped defaults.
        "filetypes": {ft: bool(should_write_audio_tag(DEFAULT_CONFIG, tag,
                                                     filetype=ft))
                      for ft in AUDIO_TAG_TYPES},
        "switches": _family_switches(tag, family),
    }


def _graded_by(tag: str):
    """The check keys that grade *tag*, from the grader's own tables."""
    checks = set(TAG_CHECKS.get(tag, ()))
    presence = TAG_PRESENCE_CHECKS.get(tag)
    if presence:
        checks.add(presence[0])
    elif tag in PER_TRACK_TAGS:
        # Everything else per-track is the generic required-tags sweep.
        checks.add("grade_check_missing_tags")
    if tag in ALBUM_TAGS:
        checks.add("grade_check_album_tags")
    return sorted(checks)


def _issue_codes(tag: str):
    codes = set(TAG_ISSUE_CODES.get(tag, ()))
    presence = TAG_PRESENCE_CHECKS.get(tag)
    if presence:
        codes.add(presence[1])
    if tag in PER_TRACK_TAGS:
        # The generic sweep appends the tag's own name as its issue code.
        codes.add(tag)
    if tag in ALBUM_TAGS:
        codes.add(tag)
    return sorted(codes)


def _checks():
    """Every grade_* key DEFAULT_CONFIG holds, in its own order."""
    out = []
    for key, value in DEFAULT_CONFIG.items():
        if not str(key).startswith(CHECK_PREFIXES):
            continue
        out.append({
            "key": key,
            "label": CHECK_LABELS.get(key) or _humanize(key),
            "default": bool(value),
        })
    return out


def _assert_known(built):
    """Fail loudly when a derivation points at something that moved.

    A renamed check, or a tag claim naming a key DEFAULT_CONFIG does not hold,
    is a bug in this file — not a page that quietly stops showing a row."""
    config_keys = set(DEFAULT_CONFIG)
    for tag in built["tags"]:
        for key in tag["graded_by"]:
            if key not in config_keys:
                raise ValueError(
                    f"tags_registry: {tag['key']} claims check {key!r}, "
                    f"which DEFAULT_CONFIG does not define")
    unlabelled = [c["key"] for c in built["checks"]
                  if c["key"] not in CHECK_LABELS]
    if unlabelled:
        # Not fatal: the label falls back to the key's own words. Worth a
        # raised eyebrow in a test, not a broken server.
        built["checks_unlabelled"] = unlabelled


def _build():
    spellings = {k: _tag_spellings()[k] for k in TAG_MAP}
    for key in ("ENCODER_PROGRAM", "ENCODER_QUALITY", "ENCODER_VERSION"):
        spellings[key] = _encoder_spellings(key)

    # Reverse index: an emitted spelling -> its canonical key, keyed the way
    # the grader compares names (mlo.grader._tag_key_norm: case, spaces and
    # underscores dropped, a TXXX:/freeform wrapper unwrapped). So a file that
    # stores the encoder marker as "encoder_quality" resolves to the same tag
    # as the "ENCODER_QUALITY" this app writes.
    aliases = {}
    for key, forms in spellings.items():
        for form in forms:
            aliases.setdefault(_norm_spelling(form), key)

    tags = []
    for key, forms in spellings.items():
        label, meaning = TAG_INFO.get(key, (None, ""))
        tags.append({
            "key": key,
            "label": label or _humanize(key),
            "meaning": meaning,
            "family": TAG_FAMILY.get(key, FAMILY_OTHER),
            "writer": TAG_WRITER.get(key, DEFAULT_WRITER),
            "graded_by": _graded_by(key),
            "issue_codes": _issue_codes(key),
            "write_gate": _write_gate(key),
            "enum": _enum_for(key),
            # The open scale (RATING's 0-100), or None for a tag whose values
            # are words or free text.
            "range": _range_for(key),
        })

    checks = _checks()
    by_tag = {}
    for tag in tags:
        for key in tag["graded_by"]:
            by_tag.setdefault(key, []).append(tag["key"])
    for check in checks:
        check["tags"] = sorted(by_tag.get(check["key"], ()))

    built = {
        "families": [{"id": fid, "label": flabel} for fid, flabel in FAMILIES],
        "tags": tags,
        "aliases": aliases,
        # The grader's OWN allow-list (mlo.grader.TAG_ALLOWLIST, the set its
        # excess check and the strip passes both test), plus the lyrics-
        # transform prefixes whose language lives in the tag name. A client
        # tests excess against these rather than re-deriving the predicate.
        "allowed": sorted(TAG_ALLOWLIST),
        "allowed_prefixes": list(_LYRICS_PREFIXES),
        "checks": checks,
    }
    _assert_known(built)
    return built


_cache = None
_cache_lock = threading.Lock()


def registry():
    """The registry, built once per process (it reads only import-time tables)."""
    global _cache
    if _cache is None:
        with _cache_lock:
            if _cache is None:
                _cache = _build()
    return _cache
