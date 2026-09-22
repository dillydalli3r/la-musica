"""Configuration persisted to config.json next to the application."""
import copy
import json
import os
import tempfile

from .paths import (CONFIG_FILE, DEFAULT_DIGITAL_SOURCE, app_data_dir, downloads_dir,
                    legacy_state_dirs, read_music_folder_guess, trash_dir,
                    trash_root)
from .naming import (DEFAULT_NAMING_SCRIPT, PRIMARY_RELEASE_TYPES,
                     SECONDARY_RELEASE_TYPES)
# The genre-list ceiling, so `mb_genre_count`'s validated range below and the
# value mlo.genres enforces can never drift apart: one number, one home.
from .genres import GENRE_COUNT_MAX
from .ui import c, Color

# Shipped default naming scripts from BEFORE the current one. A config that
# saved the old default still pins the old layout — normalize_config swaps a
# stored legacy default for the current one, so the new default takes effect
# without ever touching a script the user actually edited.
#   [0] pre-2.4.0 layout ("Artist [id]/Album {country - media - cat}/01 - Title")
#   [1] the 2.4.0 default, retired when the track id moved into the file name
#       — Settings writes the script into config.json, so every install that
#       never edited it holds this string verbatim
LEGACY_DEFAULT_NAMING_SCRIPTS = (
    "%albumartist% [%musicbrainz_albumartistid%]/$if(%releasetype%,[%releasetype%] ,)"
    "$if(%originaldate%,%originaldate% - ,)$if(%date%,%date% - ,)"
    "%album% {$if(%releasecountry%,%releasecountry% - )%media%$if(%catalognumber%, - %catalognumber%)}/"
    "%discnumber%-$num(%tracknumber%,2) %title%",
    "%albumartist% [%musicbrainz_albumartistid%]/%album%"
    "$if(%releasetype%,$if(%year%, (%releasetype%, %year%), (%releasetype%)),"
    "$if(%year%, (%year%),))"
    "$if(%label%, [%label%])$if(%releasecountry%, [%releasecountry%])/"
    "%discnumber%-$num(%tracknumber%,2) %title%",
    #   [2] the dated default, retired when the media type moved back into
    #       the album folder's brace group ("{US - CD - 6095-2}")
    "%albumartist% [%musicbrainz_albumartistid%]/$if(%releasetype%,[%releasetype%] ,)"
    "$if(%originaldate%,%originaldate% - ,)$if(%date%,%date% - ,)"
    "%album% {$if(%releasecountry%,%releasecountry%)"
    "$if(%catalognumber%,$if(%releasecountry%, - ,)%catalognumber%)}"
    "$if(%label%, [%label%])$if(%musicbrainz_albumid%, [%musicbrainz_albumid%])/"
    "%discnumber%-$num(%tracknumber%,2) %title%$if(%musicbrainz_trackid%, [%musicbrainz_trackid%])",
    #   [3] the release-id-only default, retired when the release GROUP id
    #       joined the album folder and the file name (a track that leaves its
    #       folder still names the album it came from)
    "%albumartist% [%musicbrainz_albumartistid%]/$if(%releasetype%,[%releasetype%] ,)"
    "$if(%originaldate%,%originaldate% - ,)$if(%date%,%date% - ,)"
    "%album% {$if(%releasecountry%,%releasecountry%)"
    "$if(%media%,$if(%releasecountry%, - ,)%media%)"
    "$if(%catalognumber%,$if(%media%, - ,$if(%releasecountry%, - ,))%catalognumber%)}"
    "$if(%label%, [%label%])$if(%musicbrainz_albumid%, [%musicbrainz_albumid%])/"
    "%discnumber%-$num(%tracknumber%,2) %title%$if(%musicbrainz_trackid%, [%musicbrainz_trackid%])",
)

# The genre chains previous releases shipped (Settings writes the whole list
# into the config, so an untouched install holds a copy of one of them). Both
# are swapped for the current default; a list the user actually edited is
# theirs and is kept. Kept next to the naming-script legacy default: same
# migration idea, same file.
#   [0] the pre-per-track chain (one album-level pass, Soulseek included)
#   [1] the first per-track chain (before Bandcamp/Spotify, Wikidata before
#       TheAudioDB/Last.fm's own tier)
#   [2] the two-source chain, MusicBrainz before RateYourMusic. The shipped
#       default is RYM first (the order `server.integrations.GENRE_SOURCES`
#       documents), and an untouched install holds a byte-for-byte copy of
#       this list, so it follows the new order instead of keeping the old one
#       as if it had been chosen.
LEGACY_DEFAULT_GENRE_SOURCES = (
    [
        "rateyourmusic", "soulseek", "discogs", "lastfm", "theaudiodb",
        "musicbrainz", "deezer", "itunes",
    ],
    [
        "rateyourmusic", "listenbrainz", "musicbrainz", "itunes", "wikidata",
        "lastfm", "discogs", "theaudiodb", "deezer",
    ],
    [
        "musicbrainz", "rateyourmusic",
    ],
    #   [3] the two-source chain, RateYourMusic before MusicBrainz — the
    #       default until the owner asked for every source back, so an install
    #       that never touched the list follows the new default too
    [
        "rateyourmusic", "musicbrainz",
    ],
)

# The auto-import query templates previous releases shipped. Settings writes
# the whole list into the config, so an untouched install holds a copy — and
# would keep searching with three broad templates after the default narrowed
# to the catalog number alone. A list the user actually edited is theirs and is
# kept; the exact old defaults are swapped for the current ones.
LEGACY_DEFAULT_CD_QUERIES = (
    ["catalognumber", "artist album catalognumber", "artist album"],
)

# Genres-per-track defaults this app shipped BEFORE the current one (2: one
# specific genre and its family). A config still holding one of these was
# never a decision — Settings carried the shipped default — so it follows the
# new value, the same rule the naming script, the genre-source order and the
# query templates above already use. A number the user actually chose is any
# other value and is kept. (3 was the shipped default up to 2.8.x, back when
# the slots were parent / main / sub.)
LEGACY_DEFAULT_GENRE_COUNTS = (3,)
LEGACY_DEFAULT_DIGITAL_QUERIES = (
    ["artist album year", "artist album"],
)

# Run All order — PATH-CHANGING SCRIPTS FIRST (11 videos → 3 FLACs → 14
# beets, whose generated config sets `move: yes`), then the sidecar namers
# that must see final audio names (15 manifest → 2 CUEs → 1 lyrics format),
# then content: 13 fetch lyrics → 18 publish → 17 AI transforms, 8 auto
# tagging (mood/genre/advisory), 5 images → 19 artist images (the two image
# passes together: covers then the artwork stored beside them), 6 audit, 7 DR
# & ReplayGain, 9 AccurateRip, 12 key & BPM, 16 mood & energy, then 10
# Format all (the canonical trim), 20 the layout report on the resulting
# tree, 21 the AcoustID pair fix whose tag writes grading reads, and 4 Grade
# last.
# The order this replaced ran CUEs before the converters — a cue could name
# "….wav" for an album that had become FLAC — and beets fourth-from-last, so
# the album was moved after images/audit/DR had been computed for paths that
# no longer existed. 15 stays right after 14: it reads the release id beets
# matched. 20 sits next to 4 because both are read-outs of the finished
# library: 10 has just made its final passes, so a layout report that ran
# earlier would describe names the run itself was about to change — and 21
# belongs there for the same reason, since a pair it completes is one of the
# things 4 grades.
# Keep in step with web/src/lib/scripts.ts (tests/test_script_menus).
DEFAULT_RUN_ALL_ORDER = [11, 3, 14, 15, 2, 1, 13, 18, 17, 8, 5, 19, 6, 7, 9, 12, 16, 10, 20, 21, 4]

# The genre-source order that shipped before the two-source default: recognizing
# it lets normalize_config treat it as "never customized" (see below).
LEGACY_GENRE_SOURCES = [
    "rateyourmusic", "listenbrainz", "musicbrainz", "itunes", "lastfm",
    "theaudiodb", "wikidata", "bandcamp", "discogs", "deezer", "spotify",
]

# Audio tag families that can be toggled per filetype.
# Each family groups related TAG_MAP keys that are written together.
AUDIO_TAG_FAMILIES = [
    "AUDIT",          # AUDIT (Audit Library)
    "LOG_GRADE",      # LOG_GRADE (disc rip log scores)
    "REPLAYGAIN",     # REPLAYGAIN_TRACK/ALBUM_GAIN/PEAK (4 tags via rsgain)
    "DYNAMIC_RANGE",  # DYNAMIC RANGE + ALBUM DYNAMIC RANGE (in-process, mlo/dr)
    "MEDIA_SOURCE",   # MEDIA + SOURCE (Digital Media normalization)
    "INSTRUMENTAL",   # INSTRUMENTAL (lyrics presence)
    "ADVISORY",       # ITUNESADVISORY + ALBUMITUNESADVISORY
    "LYRICS",         # embedded LYRICS tag (and .lrc sidecar)
    "BPM",            # BPM (Key & BPM analysis)
    "INITIALKEY",     # INITIALKEY (Key & BPM analysis)
    "GENRE",          # GENRE (import / auto tagging, one per configured count)
    "MOOD",           # MOOD (audio/provider classification, script 8/16)
    "ENERGY",         # ENERGY (0-100, audio analysis, written with MOOD)
    "RATING",         # RATING (0-100 Picard scale, the listener's own stars)
]
AUDIO_TAG_TYPES = ["flac", "mp3", "mp4", "ogg", "opus", "aac"]

# Map individual tag names to their family for per-type checks.
_TAG_TO_FAMILY = {
    "AUDIT": "AUDIT",
    "LOG_GRADE": "LOG_GRADE",
    "REPLAYGAIN_TRACK_GAIN": "REPLAYGAIN",
    "REPLAYGAIN_TRACK_PEAK": "REPLAYGAIN",
    "REPLAYGAIN_ALBUM_GAIN": "REPLAYGAIN",
    "REPLAYGAIN_ALBUM_PEAK": "REPLAYGAIN",
    "DYNAMIC RANGE": "DYNAMIC_RANGE",
    "ALBUM DYNAMIC RANGE": "DYNAMIC_RANGE",
    "MEDIA": "MEDIA_SOURCE",
    "SOURCE": "MEDIA_SOURCE",
    "INSTRUMENTAL": "INSTRUMENTAL",
    "ITUNESADVISORY": "ADVISORY",
    "ALBUMITUNESADVISORY": "ADVISORY",
    "LYRICS": "LYRICS",
    "UNSYNCEDLYRICS": "LYRICS",
    "BPM": "BPM",
    "INITIALKEY": "INITIALKEY",
    "GENRE": "GENRE",
    "MOOD": "MOOD",
    "ENERGY": "ENERGY",
    "RATING": "RATING",
    # integrity tags follow AUDIT family (written alongside audit when present)
    "AUDIO_MD5": "AUDIT",
    "INTEGRITY": "AUDIT",
    "LOG_CRC": "LOG_GRADE",
}

# Lyrics transforms carry their language in the tag name
# (TRANSLATION-DE, TRANSLITERATION-JA), so they are matched by prefix: every
# one of them is a lyrics tag and follows the LYRICS family.
_LYRICS_PREFIXES = ("TRANSLATION-", "TRANSLITERATION-")


def _audio_tag_family(tag_name):
    name = str(tag_name).upper()
    if name.startswith(_LYRICS_PREFIXES):
        return "LYRICS"
    return _TAG_TO_FAMILY.get(name)


# The two ADVISORY tags answer to DIFFERENT switches, because different things
# write them: `ALBUMITUNESADVISORY` is script 8's derivation — "Auto Album
# Advisory", derived from the per-track values — while `ITUNESADVISORY` is what
# the advisory FETCH resolves from the providers (script 8 only ever zeroes it
# for an instrumental). One family switch for both meant that turning the
# derivation off silently disabled the explicit "Fetch advisory rating" action,
# and the refused run replied exactly like "no provider knew this track".
_TAG_WRITE_SWITCH = {"ITUNESADVISORY": "advisory_auto_fetch"}


def _ext_to_audio_type(ext):
    ext = (ext or "").lower().lstrip(".")
    if ext == "flac":
        return "flac"
    if ext == "mp3":
        return "mp3"
    if ext in ("m4a", "mp4", "aac"):
        # aac files use mp4 container in mutagen; keep separate for aac
        return "aac" if ext == "aac" else "mp4"
    if ext == "ogg":
        return "ogg"
    if ext == "opus":
        return "opus"
    if ext in ("wav", "aif", "aiff"):
        # WAVE/AIFF carry ID3 frames exactly like MP3 (mutagen exposes `.tags`
        # as an ID3 object for both), so the per-filetype tag switches that
        # apply to ID3 files apply to them.
        return "mp3"
    return None

def should_write_audio_tag(config, tag_name, filepath=None, filetype=None):
    """Whether an audio tag may be written for the given file.

    Checks the global master switch for the tag's family (e.g. write_audit_tag)
    AND the per-filetype `audio_tag_writes` override. If no per-type entry
    exists the default is True (backward compatible).
    Unknown tags or filetypes always return True.
    """
    if config is None:
        return True
    family = _audio_tag_family(tag_name)
    if not family:
        return True
    # Global master switches (map family -> config key).
    family_global = {
        "AUDIT": "write_audit_tag",
        "LOG_GRADE": "write_log_grade",
        "REPLAYGAIN": "write_replaygain_tags",
        "DYNAMIC_RANGE": "write_dynamic_range_tags",
        "MEDIA_SOURCE": "normalize_media_source",
        "INSTRUMENTAL": None,  # gated by two keys; handle below
        "ADVISORY": "auto_advisory",
        "GENRE": "genre_autofill",
        "MOOD": "mood_enabled",
        # RATING is the user's own star rating (server.ratings): the API writes
        # it the moment a star is clicked, so its master switch is what turns
        # writing the TAG off while keeping the rating in the app.
        "RATING": "write_rating_tags",
        # ENERGY is written by the same analysis pass as MOOD, so it answers
        # to the same switch; the grader requires it when it is on.
        "ENERGY": "mood_enabled",
        "LYRICS": None,  # lyrics_format gates this separately
    }
    # A tag whose own writer has a switch of its own wins over its family's
    # (see _TAG_WRITE_SWITCH).
    gkey = _TAG_WRITE_SWITCH.get(str(tag_name).upper()) or family_global.get(family)
    if gkey is not None and not config.get(gkey, True):
        return False
    # INSTRUMENTAL has two globals; require at least one path to be enabled.
    # For per-type we still respect the individual caller: fix_instrumental
    # vs auto_instrumental are checked at call sites, so here just check
    # that at least one is enabled when family is INSTRUMENTAL.
    if family == "INSTRUMENTAL":
        if not config.get("fix_instrumental_from_lyrics", True) and not config.get("auto_instrumental", True):
            return False
    # Resolve filetype
    if not filetype:
        if filepath:
            ext = os.path.splitext(filepath)[1]
            filetype = _ext_to_audio_type(ext)
        else:
            return True
    if not filetype:
        return True
    # Per-filetype override
    per = config.get("audio_tag_writes") or {}
    # Normalize filetype alias: aac -> aac, mp4 stays mp4
    ft = per.get(filetype)
    if not isinstance(ft, dict):
        # Also try ext-based fallback for mp4/m4a
        if filetype == "mp4":
            ft = per.get("mp4") or per.get("m4a")
        if not isinstance(ft, dict):
            return True
    # If family not in ft, default True
    if family not in ft:
        return True
    return bool(ft.get(family, True))

# The grading keys 3.7.0 turned ON in DEFAULT_CONFIG, where grading shipped
# strict: the audit-tag requirement (an unaudited album used to pass) and the
# "other" file category (an unclassified file used to fail the album). They
# are named HERE because they are the entire difference between the Strict and
# Balanced presets — `server.api_stack.BALANCED_OFF` is this list, so the two
# stay one name apart instead of two hand-kept copies of the same sixty
# booleans, and a test can show Strict IS the shipped defaults rather than a
# second opinion about them.
STRICT_DEFAULT_KEYS = ("grade_check_audit", "grade_include_other")

DEFAULT_CONFIG = {
    # The first-run wizard supplies this; never ship a developer-specific
    # library path in the application defaults.
    "music_folder": "",

    # FLAC optimizer (script 3) — the compression level it re-encodes at is
    # `library_codec_quality`, next to the codec target it belongs to.
    "add_seektables": False,
    "force_reencode_flac": False,
    "flac_preserve_picture": False,
    "flac_no_padding": True,

    # Images — global
    "jpegxl_effort": 10,
    "jpegxl_distance": 0.0,
    "images_jpeg_quality": 100,
    "reencode_images": True,
    "reencode_to_jxl": False,
    "convert_jxl_back": True,
    "images_convert_to_jpeg": True,
    "images_convert_lossless_to_png": False,
    "rename_to_cover": True,
    "remove_alpha": True,
    "jpeg_progressive": True,
    "png_optimization_level": 6,
    "force_reencode_images": False,
    # Cover art — resize / crop — defaults enforce exactly 1200x1200 90% JPEG
    # (the canonical embedded-art size requested by the user).
    "cover_resize_enabled": True,
    "cover_target_size": 1200,
    "cover_crop_enabled": True,
    "cover_crop_threshold": 0.0,
    "cover_force_exact_size": True,
    "cover_enforce_size": True,
    "cover_enforce_square": True,
    "cover_jpeg_enabled": True,
    "cover_png_enabled": True,
    "cover_jxl_enabled": True,
    "cover_jpeg_target_size": 0,
    "cover_png_target_size": 0,
    "cover_jxl_target_size": 0,
    # Cover FINDER (covers.musichoarders.xyz meta-search): the SAVED DEFAULTS
    # for which region (`cover_country`) and which sources (`cover_sources`) a
    # search uses. Both are DEFAULTS ONLY: the finder may override either for
    # one search (its region dropdown and source picker), and a search never
    # writes back here — only "Save as default" in the finder does.
    # An empty `cover_sources` means "use the provider's own enabled list in
    # the app's quality order, capped at the provider's active-source limit";
    # it is never sent downstream empty (the API rejects that).
    "cover_country": "us",
    "cover_sources": [],
    # Album covers re-encode to 90% quality; other images keep max quality.
    "cover_jpeg_quality": 90,
    # Artist artwork & text pulled from the discovery providers and stored in
    # the library itself — Artists/<Artist>/artist.jpg, description.txt and
    # artist.json (provenance: provider, source URL, fetch time). Artist
    # images have no minimum resolution by default; they are only cropped to
    # `artist_image_aspect` and re-encoded at the cover JPEG quality.
    # A target size of 0 keeps the provider's native size.
    "artist_image_enabled": True,
    "artist_image_sources": [],       # ordered provider ids; [] = built-in order
    "artist_image_crop": True,
    # The shape every artist image is stored in, "width:height" (1:1 is the
    # shipped square). Read by the fetch (mlo.artistdata), by the artist image
    # grading check and by script 19, so the audit and the pass that fixes it
    # judge by the same number. Only enforced while `artist_image_crop` is on.
    "artist_image_aspect": "1:1",
    "artist_image_target_size": 0,
    "artist_description_enabled": True,
    "album_description_enabled": True,
    "description_sources": [],        # ordered provider ids; [] = built-in order
    # Store the provider's FULL prose — the whole Wikipedia article, not its
    # lead paragraph — as the description. OFF keeps the short summary form.
    # Default ON: a lead paragraph is a teaser, and the UI has Read more for
    # the long form.
    "description_full": True,

    # Embedded cover art in audio files (applied by script 10 and the FLAC
    # optimizer). Default OFF: optimization REMOVES embedded art — covers
    # live on disk as cover.* / per-track sidecars, and every player reads
    # those. When ON, the album cover is embedded into every track instead.
    "embed_covers": False,
    # JPEG quality of the embedded art — only applies when the embedded
    # image is a JPEG; PNG/lossless embeds ignore it.
    "embed_cover_jpeg_quality": 90,
    # Max resolution (longest side, px) of the embedded art. 0 keeps the
    # cover file's own size. The aspect ratio is always preserved.
    "embed_cover_resolution": 1200,

    # Lyrics / CUE — including Enhanced/Extended LRC (new in v1.2.0)
    "optimize_lrc": True,
    "optimize_embedded_lyrics": True,
    "lyrics_format": "EMBEDDED",
    "lrc_timestamp_precision": 2,
    "lrc_strip_metadata": True,
    "lrc_collapse_blank_lines": True,
    "lrc_enhanced_enabled": True,
    "lrc_enhanced_word_sync": True,
    # Required (and targeted) sync granularity of synced lyrics.
    # LINE is the default: plain [mm:ss.xx] line timestamps.
    "lrc_sync_level": "LINE",
    "lrc_extended_enabled": True,
    "lrc_add_zero_timestamp": False,
    "lrc_zero_timestamp_blank": False,
    # Where the zero timestamp is added when enabled: EMBEDDED, LRC, or BOTH.
    # Now works for both standard and enhanced LRCs (per request).
    # When enabled, it always adds a blank "[00:00.00]" as the first line.
    "lrc_zero_timestamp_target": "BOTH",
    # Disabled by default to preserve the byte-exact no-final-newline mode.
    "append_final_newline": False,
    "keep_empty_cue_lines": False,
    "keep_other_cue_lines": False,
    "keep_empty_accurip_lines": False,
    "cue_file_type": "WAVE",

    # MEDIA / SOURCE normalization
    "normalize_media_source": True,
    "digital_media_source_value": DEFAULT_DIGITAL_SOURCE,
    # When True, empty SOURCE on Digital Media is filled with the value above;
    # when False (default, per request), empty stays empty.
    "fill_empty_source": False,
    # Strip SOURCE on CD rips — CD must never carry SOURCE (per user request, on by default)
    "strip_source_on_cd": True,

    # CD rips (MEDIA=CD): deterministic CD-N renaming of .log/.cue and
    # conservative CUE FILE-name correction. Both are content-derived —
    # nothing is renamed when the evidence is ambiguous. The single-fallback
    # ensures a lone .cue/.log in a single-disc album still becomes CD-1.
    "discs_rename_enabled": True,
    "discs_rename_pattern": "CD-{n}",
    "discs_rename_single_fallback": True,
    "discs_toc_tolerance_s": 4.0,
    "discs_toc_unique_margin_s": 4.0,
    "cue_fix_filenames": True,

    # Naming script (Picard-style). Organize, the grader's path check and the
    # beets plugin all read this; it was previously undeclared, so defaults
    # and validation now cover it. `short_folder_names` truncates the
    # MusicBrainz IDs in folder names to 8 chars.
    "naming_script": DEFAULT_NAMING_SCRIPT,
    "short_folder_names": False,
    # Script 20 (library layout) FIXES what it finds instead of only reporting
    # it: a name spelled in the wrong letter case is renamed to the script's
    # spelling, audio sitting outside any album folder is moved into the one
    # its own tags name, and an artist folder with no album under it goes to
    # the Trash. OFF is the read-only report it used to be. The one-shot force
    # flag reaches this same key, so a single run can skip the fixing without
    # anything being saved.
    "layout_apply": True,

    # Force flags used by the Run All order / individual runs — re-format even
    # when a file already looks canonical.
    "force_lyrics": False,
    "force_cue": False,
    # 15 rewrites an .mlo_expected.json that is already there (see the script
    # 15 runner); without the flag an existing manifest is left alone.
    "force_tracklist": False,

    # Music-file tag writes
    "fix_instrumental_from_lyrics": True,
    "write_audit_tag": True,
    "write_log_grade": True,
    "write_replaygain_tags": True,
    "write_dynamic_range_tags": True,
    # Rating a track also writes RATING (0-100, Picard's scale) into the file,
    # and clearing a rating removes the tag. Off, ratings still live in the
    # app's own database — only the tag write stops, which is what a library
    # whose ratings belong to someone else's player wants.
    "write_rating_tags": True,

    # Grading ships STRICT: a fresh install grades every file it holds with
    # every check, and the two keys this release moved are named below so the
    # Strict and Balanced presets stay one list apart instead of two hand-kept
    # copies of the same sixty booleans (server/api_stack.py:BALANCED_OFF).
    "grade_verbose": True,
    # What file categories are allowed when grading an album folder. A
    # folder with files of a disallowed category fails grading. Every
    # category ships allowed: the grade is meant to see the whole folder, and
    # a category left out is the one thing grading never looks at.
    "grade_include_music": True,
    "grade_include_cover": True,
    "grade_include_cue": True,
    "grade_include_log": True,
    "grade_include_lrc": True,
    "grade_include_accurip": True,
    # An unclassified file (.txt/.pdf/.m3u) is allowed, which is what the
    # disallowed-files check reads: with this ON nothing else grades such a
    # file, so the layout scan's `stray_file` (script 20) is where it is still
    # reported. OFF is what makes one fail the album (grade_check_disallowed).
    "grade_include_other": True,
    # Remuxed music videos (MKV sidecars from script 11) are allowed by default.
    "grade_include_video": True,
    # Configurable strict checks for grading (all on by default, per request)
    # These make trailing/leading spaces, blank lines, cover aspect ratio
    # (squareness) and zero timestamps count as failures for the relevant
    # file types.
    "grade_check_tag_spaces": True,
    # Tag VALUES with a canonical spelling must be stored that way (mlo.tagtext:
    # "cd" -> "CD", "album; live" -> "Album; Live"). Its own toggle because it
    # is its own kind of near-miss: not whitespace, a spelling.
    "grade_check_tag_case": True,
    "grade_check_lyrics_spaces": True,
    "grade_check_cue_spaces": True,
    # Cover ASPECT RATIO (squareness) — |w/h - 1| <= cover_crop_threshold.
    # NOT crop detection: there is no crop heuristic (kept key name).
    "grade_check_cover_crop": True,
    "grade_check_lyrics_zero": True,
    "grade_check_tag_blank_lines": True,
    "grade_check_lyrics_blank_lines": True,
    "grade_check_cue_blank_lines": True,
    "grader_cover_size_tolerance_px": 0,
    "grader_strict_square_threshold": 0.0,
    "grade_log_score_threshold": 100,
    "grade_check_log_checksum": True,
    "grade_check_accuraterip": True,
    # Comprehensive grading toggles — every check can be disabled individually
    "grade_check_unreadable": True,
    "grade_check_missing_tags": True,
    "grade_check_encoder": True,
    # AUDIT must read REAL. It ships ON (it used to ship off, so an unaudited
    # library was never failed): the verdict it grades is the rip's OWN
    # evidence — the .log's CRC vs the decoded PCM, then AccurateRip — never
    # AudioAuditor's spectral opinion, so this cannot fail a disc that
    # verified itself, only one nothing verified. Turn off 'Require audit tag'
    # to grade an unaudited library (that is the Balanced preset's answer).
    "grade_check_audit": True,
    "grade_check_instrumental": True,
    "grade_check_lyrics": True,
    "grade_check_lyrics_format": True,
    # Transform tags must carry their language (TRANSLATION-EN, not TRANSLATION)
    "grade_check_lyrics_lang_tags": True,
    "grade_check_sidecar_cover": True,
    "grade_check_media": True,
    "grade_check_source": True,
    "grade_check_album_tags": True,
    "grade_check_cd_log": True,
    "grade_check_cd_cue": True,
    "grade_check_disc_naming": True,
    "grade_check_log_grade": True,
    "grade_check_crc": True,
    "grade_check_cd_format": True,
    "grade_check_cover": True,
    "grade_check_cue_format": True,
    # ...and that the sheet's FILE lines name files the album actually has
    # (a converted or renamed album used to keep "….wav" forever).
    "grade_check_cue_files": True,
    # .accurip canonical FORMAT (line/blank-line shape). Every other graded
    # check can be switched off; this one ran whenever the file existed.
    "grade_check_accurip_format": True,
    "grade_check_disallowed": True,
    # A library folder with nothing at all beneath it (no file anywhere in
    # its subtree) counts against grading as an EMPTY_FOLDER failure. Albums
    # are derived from audio files, so without this walk such a folder is
    # invisible to grading (the "no audio files" skip) — a half-finished
    # import or a stale folder would never show up as a problem.
    "grade_check_empty_folders": True,
    # Every album must carry the MusicBrainz release's own tracklist
    # (.mlo_expected.json, written by the import and by script 15). Files on
    # disk only describe themselves, so without the manifest a partially
    # imported album (3 of 12 tracks) is indistinguishable from a complete
    # one and would grade PASS — the release's tracklist is the only source
    # that knows what is missing. A manifest that IS present is diffed
    # against the files as before (this check never fails such an album).
    "grade_check_expected_tracks": True,
    # Images that are neither the album cover (cover.*) nor a per-track
    # sidecar ("01 - Song.jpg") fail grading — strays must move with the
    # album (organize sweeps them to the album root) or be removed.
    "grade_check_extra_images": True,
    # File paths must match the configured naming script (per-track relative
    # path from the music folder). Both full and 8-char-truncated MusicBrainz
    # IDs are accepted so the short_folder_names setting can't cause false fails.
    "grade_check_naming": True,
    # Filenames / folder names must match the naming script's CAPITALIZATION
    # exactly — a path that differs only in letter case (TOXICITY vs Toxicity)
    # fails; organize applies the canonical casing.
    "grade_check_filename_case": True,
    # File extensions must be LOWERCASE ("01 - Song.FLAC" fails). organize
    # lowercases every extension it touches.
    "grade_check_ext_case": True,
    # INITIALKEY + BPM tags (written by script 12, Key & BPM) are required.
    "grade_check_key_bpm": True,
    # Excess tags: any key the optimizer's strip pass would remove (outside
    # TAG_MAP + encoder identity tags) fails grading — run Optimize to strip.
    "grade_check_excess_tags": True,
    # Raw, un-remuxed video files (VOB/AVI/WMV/TS...) fail grading — run
    # script 11 to normalize them to MKV. Remuxed MKV/MP4 videos are fine.
    "grade_check_raw_video": True,
    "grade_check_mb_links": True,   # MusicBrainz release (or group) link required
    "grade_check_rym_links": True,  # RateYourMusic release link required
    # Lossless but uncompressed sources (WAV/AIFF/APE/WV/SHN) fail grading —
    # script 3 converts them to FLAC.
    "grade_check_lossless_source": True,
    # Mood & genre (script 8): both are computed/filled automatically, and a
    # track missing either fails grading — the whole point is that no track
    # ships without them.
    "grade_check_mood": True,
    "grade_check_energy": True,   # ENERGY (0-100), written next to MOOD by script 8
    "grade_check_genre": True,
    # Genre COUNT per track: a track may hold AT MOST mb_genre_count values,
    # the same cap the import and the trimming scripts apply (mb_genre_count,
    # Settings → Import). Only an overflow fails — a track with one specific
    # genre is a complete answer (the family is optional), so there is no
    # exact-count quota and nothing is ever topped up to fill a slot.
    "grade_check_genre_count": True,
    # Genre ORDER: the list is a hierarchy with the FAMILY FIRST
    # ("rock / shoegaze / dream pop"). Fails a list whose family sits in a
    # later slot or repeats; the names themselves are graded by
    # grade_check_genre_vocab below (see mlo/genres.py).
    "grade_check_genre_order": True,
    # Genre VOCABULARY: every stored name must be a MusicBrainz genre
    # (mlo.genres.canonical). The app's own writers only ever store names
    # MusicBrainz publishes, so a name it does not know is either a source's
    # own spelling that nothing canonicalized or a typo — the report names it
    # instead of silently accepting it. Off makes grading accept any name (the
    # tag is kept either way: dropping what a source said would be worse).
    "grade_check_genre_vocab": True,
    # Lyric transforms that should not be there, and the ones that should.
    # A track whose lyrics are already in the reader's own script must NOT
    # carry a TRANSLITERATION tag, and a track that needs one must — the
    # same rule for TRANSLATION, against `lyrics_translation_langs`. This is
    # what makes script 17's output auditable instead of merely present.
    "grade_check_xlit_transliteration": True,
    "grade_check_xlit_translation": True,
    # ReplayGain tags: opt-in like AcoustID — graded only when the file
    # already carries at least one of the four REPLAYGAIN_* tags, and then
    # the whole set is required. A library that never ran script 7 is never
    # failed for their absence (the player measures on the fly instead);
    # a half-written set is a real defect.
    "grade_check_replaygain": True,
    # AcoustID identity tags: pair check, never fires on files carrying
    # neither ACOUSTID_ID nor ACOUSTID_FINGERPRINT.
    "grade_check_acoustid": True,
    # The album description sidecar (description.txt) is a legitimate part of
    # an album folder — allowed as a file category by default.
    "grade_include_description": True,
    # Text and artwork stored inside the library: the album's description.txt,
    # the artist's artist.jpg and the artist's description.txt.
    "grade_check_album_description": True,
    "grade_check_artist_image": True,
    "grade_check_artist_description": True,

    # Audio audit (AudioAuditor CLI): full-track detectors (silence, DR,
    # true peak, LUFS, BPM) instead of the fast scan; force re-audits files
    # that already carry an AUDIT verdict. Detector toggles map to the CLI's
    # --no-* flags (default on) and --cutoff-allow sets the frequency-cutoff
    # threshold for fake detection (0 = CLI default).
    "audit_thorough": True,
    "force_audit": False,
    "audit_cutoff_allow": 0,
    # For MEDIA=CD rips, verify tracks against the CRC-32 checksums printed
    # in the .log and write AUDIT=REAL/FAKE from that (authoritative over
    # AudioAuditor for those files: a synthetic-tone fixture survives a real
    # AudioAuditor's "fake lossless" verdict). audit_cd_require_both (default
    # on) additionally runs AudioAuditor over CD files — its warnings are
    # kept, and it decides for a disc neither the .log CRC nor a REAL
    # .accurip could verify.
    "audit_verify_cd_checksums": True,
    "audit_cd_require_both": True,
    "audit_integrity": True,
    "audit_fail_on_unscorable_log": True,
    "audit_verify_log_checksum": True,
    "audit_require_accuraterip": True,
    "audit_log_score_threshold": 100,
    "audit_check_cd_format": True,
    "audit_batch_size": 250,
    "audit_batch_timeout_s": 30,
    "audit_per_file_timeout_s": 30,
    "audit_clipping": True,
    "audit_scaled_clipping": True,
    "audit_mqa": True,
    "audit_ai": True,
    "audit_fake_stereo": True,
    "audit_silence": True,
    "audit_dynamic_range": True,
    "audit_true_peak": True,
    "audit_lufs": True,
    "audit_bpm": True,
    "write_accurip_files": True,
    "force_accurip": False,

    # Encoder marker tags written per file type (ENCODER_PROGRAM /
    # ENCODER_QUALITY / ENCODER_VERSION) — ENCODER_PROGRAM off by default
    # (legacy, not needed for optimization gating), but can be re-enabled per
    # format via Settings → Encoder Tags. QUALITY/VERSION remain on (they gate
    # re-optimization: higher effort or newer version).
    "encoder_tags": {
        "flac": {"ENCODER_PROGRAM": False, "ENCODER_QUALITY": True, "ENCODER_VERSION": True},
        "jpeg": {"ENCODER_PROGRAM": False, "ENCODER_QUALITY": True, "ENCODER_VERSION": True},
        "png": {"ENCODER_PROGRAM": False, "ENCODER_QUALITY": True, "ENCODER_VERSION": True},
        "jxl": {"ENCODER_PROGRAM": False, "ENCODER_QUALITY": True, "ENCODER_VERSION": True},
    },
    # Per-filetype audio tag writes — which semantic tag families each audio
    # container receives. All True by default; ANDed with the global master
    # switches above (write_audit_tag etc.). Organized by filetype for
    # predictable, fine-grained control without crowding the UI.
    "audio_tag_writes": {
        "flac": {k: True for k in AUDIO_TAG_FAMILIES},
        "mp3": {k: True for k in AUDIO_TAG_FAMILIES},
        "mp4": {k: True for k in AUDIO_TAG_FAMILIES},
        "ogg": {k: True for k in AUDIO_TAG_FAMILIES},
        "opus": {k: True for k in AUDIO_TAG_FAMILIES},
        "aac": {k: True for k in AUDIO_TAG_FAMILIES},
    },

    # DR / ReplayGain (script 7): in-process DR + rsgain.
    "dr_replaygain_enabled": True,
    "replaygain_skip_existing": True,
    "force_dr_replaygain": False,
    # Playback gain. "track" applies REPLAYGAIN_TRACK_GAIN, "album" prefers
    # REPLAYGAIN_ALBUM_GAIN (falling back to the track value), "off" plays at
    # unity. A track whose file carries no ReplayGain tags is analysed on
    # demand (ffmpeg EBU R128, cached) instead of silently playing loud.
    "replaygain_mode": "track",
    "replaygain_preamp_db": 0.0,
    "replaygain_analyze_missing": True,
    # Peak-aware limiting: clamp the applied gain so the track's peak cannot
    # clip when it is known.
    "replaygain_clip_protection": True,

    # Video remux (script 11): every video container -> MKV with the video
    # copied bit-exact and every audio stream re-encoded to FLAC (lossless
    # from the decoded source, compression level video_flac_level). Caption /
    # subtitle streams are always copied and verified — never removed. When
    # the muxer refuses the video codec, the video falls back to H.264
    # (gated by video_reencode_incompatible). The fallback re-encodes the only
    # copy lossily, so it is OFF by default: an incompatible video is reported
    # instead of quietly losing a generation. The original (e.g. the VOB) is
    # deleted after a verified remux; a stray original whose same-stem MKV
    # already exists is duration-verified and then removed as well.
    "video_reencode_incompatible": False,
    # Lossy source audio (AC3/DTS/AAC…) is copied into the MKV instead of
    # being re-encoded to FLAC: re-encoding lossy audio cannot restore a
    # sample and inflates the file. Lossless source audio (PCM/FLAC/TrueHD)
    # is still converted to FLAC. Turn off to force FLAC for every stream.
    "video_lossy_audio_copy": True,
    "video_crf": 18,
    "video_preset": "medium",
    "video_flac_level": 8,
    "video_remove_original": True,
    "video_process_mp4": False,

    # Music videos from YouTube (server/youtube.py, yt-dlp). 0 max height =
    # whatever the source offers (best).
    "youtube_enabled": True,
    "youtube_max_height": 0,
    # Cookies for yt-dlp: the user's own browser session, which is the only
    # thing that opens an age-gated, members-only or rate-limited video —
    # YouTube answers "Sign in to confirm your age" without one, and a fresh
    # IP gets throttled. `none` is the default because a cookie jar is a
    # credential and sending one is the user's decision, never a default.
    # `file` reads the ONE jar the app owns (<music>/.mlo/data/cookies.txt,
    # written by Settings → Videos; see server/api_youtube.py) and `browser`
    # lets yt-dlp read the browser's own store. Both yt-dlp paths
    # (server/youtube.py: the importable module and the vendored binary)
    # honour whichever is set.
    "youtube_cookies_mode": "none",
    "youtube_cookies_browser": "chrome",

    # Library codec target (script 3 and every import). The library's audio
    # format is a SETTING, not an assumption: `library_codec` names what the
    # whole library ends up as, and script 3 converts the files that are not
    # there yet. Values, extensions and encoder arguments all come from
    # mlo.containers.CODECS (and web/src/lib/codecMeta.ts renders the same
    # list in Settings / the setup wizard):
    #   flac   .flac  lossless (the shipped default: what metaflac, the
    #                 ENCODER identity tags and `flac -t` verification are
    #                 built around)
    #   alac   .m4a   compressed lossless, for Apple-centric libraries
    #   wav    .wav   uncompressed lossless (PCM)
    #   aiff   .aiff  uncompressed lossless (PCM)
    #   mp3    .mp3   lossy, CBR
    #   aac    .m4a   lossy, CBR
    #   ogg    .ogg   lossy (Vorbis)
    #   opus   .opus  lossy
    #   keep          never convert anything
    "library_codec": "flac",
    # Lossy rate/quality for the codec above: kbps for the CBR targets
    # (mp3/aac/opus), libvorbis' own 0-10 quality scale for ogg (-q:a 6 ≈
    # 192 kbps VBR). 0 = the codec's own shipped default (mp3 320, aac 256,
    # ogg 6, opus 128 kbps — see mlo.containers.CODECS). Clamped to the
    # codec's usable range, and ignored by a lossless target, which has no
    # bitrate setting.
    "library_codec_bitrate": 0,
    # Lossless compression level: FLAC -0..-8, used both by the ffmpeg
    # conversion and by script 3's own flac.exe re-encode. ALAC and the PCM
    # targets expose no such control and ignore it.
    "library_codec_quality": 5,
    # Extra encoder arguments, appended verbatim: flac.exe options when the
    # target is FLAC, ffmpeg options for every other target (added after the
    # quality/bitrate flags above, so a user's own flag wins). The app does
    # NOT validate them — a bad flag fails the encode, which is reported per
    # file — it only strips control characters and caps the length.
    "library_codec_args": "",
    # What the optimisation pass may DO with the target:
    #   lossless_to_lossy (default) — a LOSSLESS source is converted to the
    #     target (that is the lossless -> lossy transcode the name refers to
    #     when the target is lossy, and a lossless re-container otherwise).
    #     A LOSSY source is never re-encoded: lossy -> lossless cannot
    #     restore a sample, and lossy -> lossy is a generation loss.
    #   all — additionally re-encode lossy sources to the target. The one
    #     thing it still refuses is a LOSSY source under a LOSSLESS target,
    #     which can only lose quality.
    #   keep — never convert; only the existing format/naming work runs
    #     (script 3's own FLAC re-compression still does, since it stays in
    #     the same codec).
    "library_codec_optimize": "lossless_to_lossy",
    # Whether the converted ORIGINAL is taken out of the library afterwards.
    # It is moved into the app's trash bin (<music>/.mlo/trash/, with its
    # origin recorded), never deleted, so the lossless master can be
    # restored from the Trash page.
    "lossless_remove_original": True,

    # Auto Tagging (script 8)
    "auto_advisory": True,
    "auto_instrumental": True,
    # ON by default: a track with no words cannot be explicit, so an
    # instrumental's advisory is 0. Turn it off to leave an instrumental's
    # advisory exactly as it is (an explicit instrumental stays explicit).
    "auto_zero_advisory_for_instrumental": True,
    # Fetch INSTRUMENTAL (0/1) from the external sources during auto tagging + import.
    "instrumental_auto_fetch": True,
    "force_auto_tag": False,

    # Key & BPM analysis (script 12): librosa-backed BPM + initial key.
    "audiometa_enabled": True,
    "audiometa_overwrite": False,
    "audiometa_min_seconds": 10,
    "audiometa_key_notation": "musical",  # musical | camelot | openkey
    "force_audiometa": False,

    # Mood & Energy detection (script 16). MOOD/ENERGY are already derived by
    # script 8 (Auto tagging) as part of its pass; script 16 is the same
    # classifier on its own, and this flag re-analyses every track whether or
    # not it already carries the tags.
    "force_mood": False,

    # Lyrics sources, tried in order until one has the song — each provider
    # falls back to the next, and every one of them answers with TIMESTAMPS.
    # Empty = the built-in order (LRCLIB, NetEase, Kugou, QQ Music, Kuwo,
    # YouTube captions); see Settings → Lyrics.
    "lyrics_sources": [],
    # Accept plain (unsynced) lyrics when no provider has a synced version.
    # Off (the default) means synced or nothing: the chain ships timestamps
    # only, and a provider answer without them is treated as no answer.
    "lyrics_allow_plain": False,
    # YouTube captions through yt-dlp, for tracks that carry a video id (tag or
    # the "[<id>]" the video download leaves in the file name). Never searches
    # YouTube on its own. Off = that provider is simply not in the chain.
    "lyrics_youtube_captions": True,

    # AI-assisted lyric transforms (script 17). Any OpenAI-compatible
    # /chat/completions endpoint works (OpenAI, OpenRouter, LM Studio,
    # llama.cpp, Google Gemini's OpenAI-compatible endpoint). The app never
    # needs it: with no base URL + model the script logs one line and returns.
    "ai_base_url": "",
    "ai_api_key": "",
    "ai_model": "",
    # Reasoning effort for AI calls: HIGH is the default — maximum thinking
    # budget for transliteration/translation quality; MINIMAL disables
    # thinking entirely for speed; MAX asks for the provider's own ceiling
    # (server.ai.ai_chat drops to HIGH if the endpoint refuses the word).
    "ai_effort": "high",
    # AI genre inference — the one AI feature that runs during importing and
    # tagging. The model is given the genres the configured sources (RateYourMusic
    # and MusicBrainz first, then the rest) already answered with (plus whatever the other configured sources know)
    # and returns at most `mb_genre_count - 1` SPECIFIC genres, most specific
    # first: the family is not its to answer — the app derives it
    # (mlo.genre_vocab.parent_of) and puts it first, see mlo.genres. On by
    # default *when an AI endpoint is configured* — with no base URL/model the
    # import simply uses the source list as-is.
    "ai_genre_inference": True,
    # Thinking budget for the inference. HIGH is the default: the model is
    # told to reason about the ranking and to look up anything the fetched
    # list does not cover before answering, which is what makes the specific
    # genres worth having over the source order. MINIMAL answers from the
    # fetched list alone, for a fast import on a big backlog; MAX asks for
    # the provider's highest thinking budget.
    "ai_genre_effort": "high",
    # Let the model consult its own knowledge of the artist/album beyond the
    # genres it was handed (rather than re-ranking only what it was given).
    # Off makes the answer strictly a re-ranking of the fetched list.
    "ai_genre_research": True,
    # Script 17 — persistent lyric transforms. Enabled, so Run All writes
    # TRANSLITERATION / TRANSLATION tags (and sidecars) for every track that
    # needs them; answers are disk-cached, so re-runs only pay for new or
    # changed lyrics.
    "lyrics_xlit_enabled": True,
    "lyrics_translate_enabled": True,
    # Target languages for translation, comma separated ("en,de"). The first
    # language is the reader's own script for the romanization rule and names
    # the TRANSLATION tag; every language also gets its own "<stem>.<lang>.lrc"
    # sidecar.
    "lyrics_translation_langs": "en",
    # Also write "<stem>.romaji.lrc" / "<stem>.<lang>.lrc" next to the audio
    # (tags-only lyrics formats never write stray sidecars).
    "lyrics_xlit_sidecars": True,
    "force_xlit": False,

    # Auto-publish to LRCLIB (script 18): this library's own lyrics are
    # submitted for tracks the community database does not answer for yet.
    # Outward-facing and public, so it is a setting; a track LRCLIB already
    # has is never touched (force_publish re-submits anyway).
    "lrclib_auto_publish": True,
    "force_publish": False,

    # Managed beets tagging (Picard parity).
    "locale": "en",
    "beets_translations": True,
    "beets_work_movement": True,
    "beets_release_type_caps": True,
    # Default True: beets only relocates AUDIO files; the follow-up organize
    # applies the naming script to filenames and gathers sidecars / covers
    # into the final album folder, which grading requires.
    "beets_organize_after": True,

    # Import autonomy — how much of an import the app decides on its own.
    # "automatic" (the default) runs the configured chain end to end, fetches
    # everything the configured sources can answer, and only comes back to the
    # user for what nothing could supply: the album is imported either way, and
    # whatever it is still missing is reported as ONE prompt — a notification
    # plus an entry the wizard lists — naming the families and the fields, and
    # linking to the album at the step where each decision is made. "review" is
    # the wizard's own behaviour applied to the pipeline instead: the import
    # stops before the first step that needs a decision (a family the album
    # still lacks) and hands the album over rather than deciding past it. What
    # "missing" means is `mlo.grader`'s own checks — this adds no second
    # completeness opinion.
    "import_autonomy": "automatic",
    # Families the USER decides even in automatic mode: "links", "cover",
    # "genres", "lyrics", "advisory" (the wizard's own steps). A family named
    # here is never auto-decided — the cover step stages candidates instead of
    # writing the first hit, the links and advisory fetches are skipped, the
    # lyrics script drops out of the chain — and the album's prompt names it as
    # awaiting a decision instead of as unsourced. Empty = the app decides
    # everything any configured source can answer, which is the point of
    # automatic mode.
    "import_review_families": [],

    # The two switches over the whole acquisition surface. Both default on,
    # which is how the app has always behaved. `auto_acquisition_enabled` is
    # the MASTER switch for everything the app does on its own: the wishes
    # worker searching the queue, an artist watch queueing a new release, and
    # an "Add to library" request starting a download. Off, the request is
    # still RECORDED — the wish, the watch's row, the framework album — and
    # reported honestly ("nothing searched: automatic acquisition is off"),
    # but nothing is searched or downloaded until the user acts on it (the
    # wish's own Search now, the wizard, the Soulseek page). It is not the
    # same switch as `wishes_auto_import`, which keeps searching and only
    # stops the download. `manual_import_enabled` is the switch over the
    # user-driven import path: the wizard and every POST /api/import/* route.
    # Off, those routes answer 409 naming this setting instead of importing,
    # so a user can hand the whole importer over to the automatic pipeline (or
    # stop imports entirely) without anything importing behind their back.
    "auto_acquisition_enabled": True,
    "manual_import_enabled": True,

    # Import — drag & drop, the import wizard and the Soulseek pipelines all
    # run the same script chain, so a freshly imported album leaves the
    # pipeline complete instead of half-tagged. Empty `import_scripts` = the
    # built-in chain: 2 CUEs → 3 FLACs → 11 videos → 1 lyrics format →
    # 13 fetch lyrics → 8 auto tagging (mood/genre/advisory) → 5 images →
    # 6 audit → 7 DR & ReplayGain → 9 AccurateRip → 12 key & BPM → 14 beets →
    # 10 format all → 4 grade. An explicit list of script ids replaces it;
    # `import_auto_scripts` off disables the chain entirely (the wizard still
    # writes MusicBrainz tags, lyrics and covers itself).
    "import_auto_scripts": True,
    "import_scripts": [],
    # Albums processed at the same time when several are imported at once.
    "import_bulk_concurrency": 2,
    # AcoustID release matching during import: fingerprint each track with
    # chromaprint's fpcalc and ask AcoustID which MusicBrainz recording the
    # audio actually is, then offer the release that contains them. Falls back
    # to the existing title/artist search when fpcalc is missing, no key is
    # configured, or nothing matches.
    "import_acoustid": True,
    "acoustid_enabled": True,
    "acoustid_api_key": "",
    # SUBMITTING to AcoustID is a second credential, not a second app key: the
    # application key (`acoustid_api_key`) can only look up, and the user key is
    # the one acoustid.org shows a signed-in person. It is what
    # POST /api/import/acoustid/submit publishes with, and it is never needed
    # for matching.
    "acoustid_user_key": "",
    # The override mlo/acoustid.py already honours; without a default (and so
    # without a Settings field) the documented escape hatch could not be set.
    "acoustid_fpcalc_path": "",
    "acoustid_min_score": 0.75,

    # Soulseek via managed slskd (shares = the library folder <music>/Artists).
    "soulseek_username": "",
    "soulseek_password": "",
    "soulseek_description": "",
    "soulseek_listen_port": 50000,
    "soulseek_web_port": 5030,
    "soulseek_up_limit": 0,
    "soulseek_down_limit": 0,
    # slskd transfer slots (concurrent transfers) and speed limits in KiB/s.
    # A SPEED limit of 0 is "unlimited" (emitted as slskd's int.MaxValue); a
    # SLOT count of 0 is not a number of slots — slskd refuses a count below 1,
    # so a blank/0 slots value is left to slskd's own default.
    #
    # What these slots are FOR: the app runs `soulseek_search_concurrency`
    # releases at once, each downloading up to `soulseek_candidate_slots`
    # candidates, and the app's own enqueueing is what holds those two limits
    # (see server.soulseek_auto). slskd then takes the transfers they produce
    # off the network, and it can only have this many in flight — so the
    # shipped default is exactly that product, 3 × 3 = 9. A config with fewer
    # slots than its other two settings need still gets the guarantee: the
    # per-release width is narrowed to fit (`_batch_width`), so the app never
    # asks slskd for more than it will serve.
    "soulseek_download_slots": 9,
    "soulseek_upload_slots": 2,
    "soulseek_upload_limit_kib": 0,
    "soulseek_download_limit_kib": 0,
    # slskd's HTTPS listener binds an extra port (5031) with a self-signed
    # cert by default. The app talks plain HTTP to the loopback port, so the
    # second listener is disabled unless explicitly wanted.
    "soulseek_web_https": False,
    # Ask the router to open the listen port (UPnP IGD, then NAT-PMP) so peers
    # can reach this client without a manual port-forward. slskd has no such
    # option — upstream closed the request unimplemented — so the mapping is
    # made by the app itself (mlo/portmap.py). On by default because an
    # unreachable listen port is what makes a client look offline to the
    # network; it is a no-op when no gateway answers, and the status says so.
    "soulseek_upnp": True,
    # After a downloaded release imports successfully, delete the copy that was
    # downloaded — the library now holds the album and the download folder is
    # only a staging area. ON by default: the alternative is a second full copy
    # of everything you acquire. It is only ever the folder the job itself
    # downloaded into, only inside the configured download dir, and only after
    # the import reported success — a failed import keeps its files so it can
    # be retried without downloading them again.
    "soulseek_clear_downloads": True,
    "soulseek_download_dir": "",
    # ON by default: the Soulseek client should be up whenever the app is.
    "soulseek_autostart": True,
    # Share the library with the network on the configured listen port.
    "soulseek_share_library": True,
    # Auto-import (MusicBrainz release → Soulseek). Each template is a
    # space-separated list of release fields: artist album year date country
    # catalognumber barcode label.
    # Query templates per release kind. A PHYSICAL release — CD, vinyl,
    # cassette, SACD, SHM-CD, CD-R, Blu-spec CD: every medium mlo.tagtext
    # .MEDIA_VALUES names except Digital Media — is searched by the traits that
    # identify THAT pressing: the catalog number and the barcode are what rip
    # folders carry, and neither of them asks the network for every other
    # pressing of the same album the way artist/title does. A pressing with
    # neither falls back to its label and country. Digital Media has no
    # pressing trait at all, so it keeps the broader `artist album year`
    # wording. Add templates (Settings → Auto-import) to widen a search again.
    # `soulseek_auto_cd_queries` is the key the physical one grew out of: a CD
    # is a physical release, so a config that still sets that key keeps using
    # its templates (its own shipped default is superseded — it named the
    # catalog number alone, which the physical default already covers).
    "soulseek_auto_physical_queries": ["catalognumber", "barcode"],
    "soulseek_auto_cd_queries": ["catalognumber"],
    "soulseek_auto_digital_queries": ["artist album year"],
    # Every disc's .log must score at least this (Logchecker 0-100) before
    # the full album is downloaded.
    "soulseek_auto_log_min_score": 100,
    # Fraction of the release track list a candidate folder must contain.
    "soulseek_auto_complete_ratio": 1.0,
    # Every candidate the search turns up is tried in score order (there is no
    # rejection cap: a rejected peer costs only its own attempt, and stopping
    # early throws away candidates that would have verified).
    # Quiet seconds before slskd ends a search with too few replies to score.
    "soulseek_auto_search_wait": 10,
    # Peers that must answer before the search is scored instead of waiting on
    # slskd's quiet timer: a popular album never goes quiet, and slskd only
    # hands back its responses once a search has ENDED — this is what stops a
    # good copy from sitting behind a full search window. Measured through this
    # client: a popular query with a 10 s quiet window and no limit became
    # readable after 32 s; with a 5-response limit after 0.5 s, with 40 after
    # ~11 s (peers arrive in a burst, then trickle). 15 keeps the wait to a
    # few seconds while still scoring fifteen whole folders.
    "soulseek_auto_response_limit": 15,
    # How many releases the auto-importer works on AT THE SAME TIME — the
    # wishes worker fills up to this many wishes in one pass, a bulk
    # "download all" run keeps this many jobs in flight, and a release over the
    # ceiling WAITS in the queue (it keeps its place, it is cancellable there,
    # and it starts by itself when one of the running ones finishes) instead of
    # being refused. Searching is mostly waiting on the network, so one album
    # at a time left the page showing a queue that only ever moved one item.
    "soulseek_search_concurrency": 3,
    # How many candidate downloads of ONE release run at the same time — three
    # peers of one album transfer side by side, the first that verifies good
    # becomes the import and the others are cancelled and swept, and the NEXT
    # candidate is only asked for when one of them lands or fails. Enforced by
    # the app's own enqueueing; `soulseek_download_slots` is only the outer
    # ceiling slskd puts on the transfers it produces (see that key).
    "soulseek_candidate_slots": 3,
    # Park an interactive job that found no usable folder and ask the user
    # whether to add the release to the wishes list, instead of failing the job
    # outright: a rare album is worth watching for, and the background wishes
    # worker keeps searching with the queries the job already used. The
    # background path itself never asks (a wish must not be turned into a wish).
    "soulseek_auto_wish_prompt": True,
    # Release-choice policy (mlo.release_choice — the ONE policy every
    # acquisition path ranks editions with: "Add to library", the bulk
    # auto-import, the wish worker and the artist watch). It prefers
    # status=Official, never auto-picks a Promotion / Bootleg / Pseudo-Release
    # while avoid-promo is on, requires a RELEASECOUNTRY while require-country
    # is on, and ranks the rest by the caller's release-group TYPE filter
    # first, then medium, then completeness, then how close the edition's own
    # date is to the group's original release date, then the two keys below.
    # Country matters because it is the one trait that proves the edition was
    # actually sold somewhere: a country-less MusicBrainz release is usually an
    # unsorted import, and the folder-name query templates are built around it.
    "auto_import_avoid_promo": True,
    "auto_import_require_country": True,
    # Best medium first, by MusicBrainz format name. CD leads (the pressings
    # rips and the folder templates are built for), the other PHYSICAL media
    # follow, and digital is last: a digital edition carries no catalog number
    # and no pressing to match against, so it is the edition a Soulseek folder
    # matches least reliably. A format the list does not name ranks after every
    # configured one.
    "auto_import_medium_order": ["CD", "Vinyl", "Cassette", "Other", "Digital Media"],
    # The release country preferred among otherwise EQUAL editions — a
    # tie-breaker, never a filter. An ISO 3166-1 alpha-2 code, the spelling
    # MusicBrainz publishes on the release ("US", "GB", "XW" for worldwide),
    # compared case-insensitively; blank (the default) states no preference.
    # It can never outrank status, medium, completeness or the date rule.
    "prefer_release_country": "",
    # Prefer the explicit/original edition over a clean or edited one, where
    # MusicBrainz says so in the release title or its disambiguation comment: a
    # clean edition may carry altered audio, so it is taken only when nothing
    # else is on offer. Off = a clean edition ranks on the other rules like any
    # other edition.
    "prefer_original_edition": True,
    # A COMPRESSED derivative of a disc (a BDRip/DVDRip/x264 re-encode) sorts
    # below the disc's own streams — a remux, a full disc — in the
    # release-choice policy (mlo/release_choice.py) and decides which file the
    # disc-folder rule keeps when a VIDEO_TS/BDMV structure sits beside a
    # re-encode of the same feature. OFF = the tier scores nothing, so the
    # other eight tiers decide exactly as they did before the rule existed.
    "prefer_disc_streams": True,
    # Covers: fetch and write cover art during import when the album has none.
    # On by default — a downloaded album without a cover grades as incomplete,
    # and the finder's provider chain (Cover Art Archive → Deezer → Apple) can
    # fill it in while the import is still running.
    "cover_auto_fetch": True,
    # With cover_auto_fetch on, the finder's winner is WRITTEN during the
    # import (the artist/album's own MusicBrainz release-group cover ranks
    # first as the reference, so the album's real art is normally what lands).
    # On = stage the candidates for the user to pick instead, which is what a
    # library that cannot tolerate a wrong pressing chooses — the album then
    # waits with a "Needs you" row until someone decides.
    "cover_review": False,
    # A disc's own streams beat a compressed derivative of them: when MusicBrainz
    # offers both a remux/full-disc edition and a BDRip/DVDRip of the same
    # group, the disc is taken (and a folder holding a disc structure plus a
    # 700 MB re-encode keeps the disc's feature, not the re-encode). On by
    # default — a re-encode cannot be undone — and off leaves the choice to
    # the rest of the policy, exactly as before this rule existed.
    # Explicit shared folders (empty = the library folder <music>/Artists).
    "soulseek_share_dirs": [],
    # Extra share filters — substrings/paths slskd must NOT share.
    "soulseek_share_exclude": [],

    # Wishes — MusicBrainz releases saved to the library WITHOUT downloading.
    # A background worker re-searches Soulseek for each wish on an interval
    # and auto-imports the release the moment a verified match appears.
    "wishes_enabled": True,
    "wishes_interval_hours": 6,
    # The retry policy (one place: server/wishes' "Retry policy" section).
    # TRANSIENT failures — a refused/absent slskd, a MusicBrainz outage, a
    # failed verification — are retried with backoff: the wait doubles per
    # attempt, up to `wishes_max_attempts` attempts (0 = retry forever).
    # A search that found NOTHING is not a failure to try harder: it spends a
    # not-found attempt instead, and after `wishes_not_found_attempts` empty
    # searches (0 = never give up) the wish ends 'not_found' — terminal and
    # announced once, with the queue's retry button as the way back.
    # Both ends record why, and neither is retried by the timer again.
    # SHIPPED AS "KEEP LOOKING": a release the user asked for keeps being
    # searched on its interval until it is found or the user cancels it. The
    # network is not a fixed catalogue — a share that is offline today is
    # online next week, and stopping after three quiet searches threw away
    # requests the user had already made. A finite cap is still there for
    # anyone who wants one.
    "wishes_max_attempts": 0,       # 0 = retry forever
    "wishes_not_found_attempts": 0,  # empty searches before 'not_found' (0 = never)
    "wishes_retry_backoff_minutes": 30,  # extra wait per retry, doubled (0 = off)
    "wishes_auto_import": True,

    # Artist watches — follow an artist and add what it releases from NOW on.
    # The whole point is what a watch refuses to do: a fresh watch never
    # enumerates a back catalogue, so "new" is a release group whose FIRST
    # release date is after the watch was created (an undated group is never
    # new), and one cycle queues at most `artist_watch_max_per_cycle` albums
    # whatever the watch's own policy is ("backfill" only widens the date rule,
    # per watch, on purpose). The worker that runs the cycles is
    # server/artist_watch_worker; the policy itself is server/artist_watch.
    "artist_watch_enabled": True,
    # How long between two checks of the SAME artist (a watch has its own
    # timer; the worker ticks far more often than this).
    "artist_watch_interval_hours": 24,
    # The hard per-cycle cap — the anti-dump rule. One album is the shipped
    # default: a watch that queued a hundred at once is the discography dump
    # this feature exists to avoid.
    "artist_watch_max_per_cycle": 1,
    # Which release-group TYPES a watch may queue. MusicBrainz's own type names
    # (lowercase) — the vocabulary is mlo.naming.PRIMARY_RELEASE_TYPES /
    # SECONDARY_RELEASE_TYPES, and the rule is server.artist_watch's: a
    # secondary type is a QUALIFIER and decides the match (a live album is
    # Album + Live, so "album" alone must not queue it and ticking "live"
    # must), otherwise the group's primary type has to be selected. Album + EP
    # is the shipped default, so a watch sweeps up new studio records and EPs
    # and leaves the live records, compilations and scores alone. A watch may
    # override the set per artist.
    "artist_watch_types": ["album", "ep"],
    # Queue a matched release into the library automatically. Off means a watch
    # only reports what it found (the notification is the whole output), which
    # is what a user who wants to choose the edition by hand wants.
    "artist_watch_auto_add": True,

    # Genres imported per release/track (top voted first). Sources are tried
    # in this order and merged, and EVERY source is asked for EVERY track
    # (server.integrations._genre_source_answers). The registry of every
    # source the app can ask is `server.integrations.GENRE_SOURCES`; this is
    # the SHIPPED default (what an empty saved list falls back to) and it
    # follows that list's own order — RateYourMusic first, MusicBrainz
    # second — with the rationale for each position documented there and in
    # `server/integrations.py` above GENRE_SOURCES. `soulseek` is deliberately
    # absent (peers advertise folders, not genres). An install that never
    # touched the Settings list follows this change: the previous two-source
    # default is in `LEGACY_DEFAULT_GENRE_SOURCES` (see normalize_config).
    # GENRES PER TRACK — one knob for three places, so they can never
    # disagree: how many genres an import writes onto a track (highest-voted
    # source first), how many a track may KEEP (script 8's auto tagging, the
    # genre import and script 10's canonical pass all trim the rest off), and
    # the most grading accepts (`grade_check_genre_count`). Default 2: the
    # family and one specific genre ("rock / shoegaze"). The family is DERIVED
    # (mlo.genre_vocab.parent_of), never asked of a model and never invented,
    # and it takes the FIRST slot — so 3 means "the family and two specific
    # genres", not a third synonym. Raising it past 3 would say less about the
    # music, not more (GENRE_COUNT_MAX), and a merged "Rock; Alternative Rock;
    # Indie; Shoegaze; Post-Rock" list helps no one.
    "mb_genre_count": 2,
    # Genre sources, in priority order — EVERY source the app knows, in the
    # order the user asked for: RateYourMusic first (what the release page
    # itself says), then MusicBrainz (open data, keyless, the identity
    # anchor), then the rest of the registry in its documented order. The
    # chain is a priority list that STOPS once a track's list is complete
    # (`_genre_complete`), so shipping all of them costs nothing on a release
    # the first two can answer and is what makes a rare pressing still get a
    # genre. `soulseek` is the one source deliberately absent (peers advertise
    # folders, not genres). Every position's rationale is documented above
    # `server.integrations.GENRE_SOURCES`; `tools/test_genres.py` asserts the
    # two lists are equal.
    "genre_sources": [
        "rateyourmusic", "musicbrainz", "listenbrainz", "itunes", "lastfm",
        "theaudiodb", "wikidata", "bandcamp", "discogs", "deezer", "spotify",
    ],
    # Optional keys for the genre sources that need one. Left empty the source
    # is skipped instead of guessed (Discogs' search endpoint requires a
    # token; Last.fm requires an API key). RYM answers only a real browser
    # session: paste the Cookie header of a logged-in rateyourmusic.com tab
    # (it carries Cloudflare's cf_clearance). Empty, RYM's live pages are not
    # asked at all while the archived-snapshot fallback below is on — a
    # refused request would latch the source off for a few minutes anyway
    # (`integrations._rym_blocked`) — so the genre chain either reads a
    # snapshot or falls through to the next source. Deezer/iTunes/TheAudioDB/
    # MusicBrainz are keyless.
    "discogs_token": "",
    "lastfm_api_key": "",
    "rym_cookie": "",
    # Read RateYourMusic's genre pages from the Wayback Machine when the live
    # site will not serve them: no `rym_cookie`, or one RYM refused to honour.
    # An archived copy is RYM's OWN data — the same page, read by the same
    # scrapers — so the answer still comes from that source, and the report
    # says which snapshot answered and how old it is. Off, and with no cookie,
    # RYM contributes nothing, exactly as it did before this key existed.
    # Ships ON because it is the only route that works on an install nobody
    # has pasted a cookie into, and it costs one politely throttled request per
    # album (1 req/s, and cached for 30 days like every other RYM page) — the
    # fallback is skipped entirely when the live page can answer.
    "rym_archive_fallback": True,
    # Auto-resolve RateYourMusic album + artist links during import; off =
    # links are only ever set by hand in the link editor.
    "rym_links_auto": True,
    # Advisory (ITUNESADVISORY) auto-fetch on import: EVERY applicable source
    # is asked in one pass and cross-referenced — Deezer by ISRC, Spotify by
    # ISRC when configured below, Apple's explicit-edition album route and
    # Apple's song search — merged so an explicit statement anywhere wins and
    # anything else is 0. MusicBrainz supplies the ISRCs when the file has
    # none. An existing 0/1/2 is never overwritten by a ROUTINE pass (the
    # user's edit wins); an explicit re-rate (`force`, the wizard/settings
    # "re-check" action) asks the providers again and rewrites only with
    # evidence — the invented `advisory_fallback` never overwrites a stored
    # rating.
    "advisory_auto_fetch": True,
    # What happens when NO source states an advisory — the ladder in
    # `mlo.advisory`: an instrumental is 0 (`auto_zero_advisory_for_instrumental`),
    # then the configured AI provider judges the lyrics when this is on,
    # then the multilingual word scan runs when `advisory_lyrics_scan` is on,
    # and `advisory_fallback` is the last resort: "0" (not explicit, the
    # shipped default), "2" (clean edition) or "none" (write nothing at all,
    # leaving the track unrated).
    "advisory_ai_classify": True,
    "advisory_lyrics_scan": True,
    "advisory_fallback": "0",
    # Optional Spotify Web API credentials (client-credentials flow). Used by
    # the advisory cross-reference (the ISRC search) and by instrumental
    # detection (audio-features `instrumentalness`). Empty = both are skipped
    # entirely; it is never required and its absence can never fail an import.
    "spotify_client_id": "",
    "spotify_client_secret": "",
    # Artist image / artist description / album description auto-fetch on
    # import. With metadata_review on, candidates are staged and only written
    # when the user applies one.
    "metadata_auto_fetch": True,
    "metadata_review": False,
    # Mood & genre tagging (script 8, Auto tagging). MOOD is derived from the
    # track's audio (librosa features: tempo, energy, brightness, dynamics)
    # and, in hybrid mode, cross-checked against provider metadata; GENRE is
    # filled from the providers whenever the tags carry none. Both are graded.
    "mood_enabled": True,
    "genre_autofill": True,
    "mood_source": "hybrid",   # CHOICES: audio | provider | hybrid
    # Script 3/10 removes tags outside the canonical set while optimizing.
    "strip_unknown_tags": True,

    # Home — the library highlight shelves on the sidebar's Home section.
    "home_recent_count": 12,
    # Discovery — the external music APIs behind artist artwork and
    # descriptions (Deezer, ListenBrainz, iTunes, TheAudioDB, Wikipedia).
    # Empty source lists = the built-in order; every feature walks its list and
    # falls back to the next provider, and MusicBrainz stays the final fallback
    # so results keep their MBIDs.
    "discovery_enabled": True,
    "discovery_timeout_s": 8,

    # Misc
    "auto_advance": True,
    # 0 means automatic. A positive value caps every module's worker pool,
    # which is useful on slower disks or shared machines.
    "worker_limit": 0,
    # How many tracks an offline download fetches at once — the browser's own
    # download queue and the bulk transfer endpoint both read it, so one key
    # describes the whole path. A local server can stream several FLACs in
    # parallel; more than a handful mostly thrashes the disk and the network.
    "download_concurrency": 3,
    "run_all_order": list(DEFAULT_RUN_ALL_ORDER),

    # Export to device (Export page). Each key is the SAVED DEFAULT behind one
    # control on that page: the page loads them when it opens and its "Save as
    # default" writes them back, so a DAP's own settings survive a restart.
    # Every one is still overridable per export — the request carries the
    # values the form showed. Which codecs/structures exist is
    # server.exporter's tables, not a list here: an unknown value falls back
    # to the default instead of being reset by the config loader.
    "export_dest": "",
    "export_subfolder": "Music",
    "export_codec": "copy",
    "export_quality": "",
    "export_structure": "artist_album",
    # Embed the album cover into every exported file. Separate keys from the
    # library's embed_covers / embed_cover_* on purpose: exporting to a player
    # must never decide what the library's own files keep. Quality applies to
    # JPEG embeds, resolution caps the longest side (0 = the cover's own size).
    "export_embed_covers": True,
    "export_embed_cover_jpeg_quality": 90,
    "export_embed_cover_resolution": 1200,
    # ID3 write options for MP3 exports: "2.3" is what older players and car
    # stereos read (mutagen writes 2.4 by default), and an ID3v1 chunk is for
    # players that read nothing else. Only applies where the export writes
    # tags — a plain byte-for-byte copy stays byte-for-byte.
    "export_id3v2": "2.3",
    "export_id3v1": False,
    # What an exported file does about loudness. "off" leaves the audio alone,
    # "tags" measures each exported track with ffmpeg's EBU R128 meter (the
    # same meter script 7 writes tags from) and stores ReplayGain 2.0 track +
    # album tags, so a player that honours them plays the export at the
    # library's loudness, and "apply" bakes that same gain into the samples —
    # for a player that honours nothing. An applied export carries no
    # REPLAYGAIN_* tags: the gain is in the audio, and a player applying the
    # tags on top would correct it twice.
    "export_replaygain_mode": "off",
    # An Equalizer APO / Peace profile applied while transcoding (see mlo.eq):
    # a built-in preset id or an imported profile's, "" = no EQ. The profile's
    # preamp and filters are rendered into the same ffmpeg filter chain as the
    # ReplayGain gain above.
    "export_eq_profile": "",
    # Where an export goes: "server" (a drive/folder this machine can see, the
    # drive picker on the Export page) or "zip" (staged in the app's data dir
    # and handed back as one archive — the only destination a browser can offer
    # the user of a different machine).
    "export_target": "server",
    # Write only the canonical tag set on transcodes instead of letting the
    # source's leftover frames ride along beside it.
    "export_clean_tags": True,
    # .m3u8 playlists next to the exported albums (and one for the whole
    # export) — what a DAP needs to show album order.
    "export_playlists": True,
    # Mirror cover.*/description.txt/artist image/.lrc/.cue/.log next to the
    # exported audio.
    "export_sidecars": True,
    # Write checksums.sha256 (sha256<2 spaces>relative path, what `sha256sum -c`
    # reads back) at the export root, so a copy to a card can be proven intact.
    "export_manifest": False,
    # Re-open every written file and prove it parses (and has the source's
    # duration) before the export reports success.
    "export_verify": True,
    # Sync mode: delete audio files under the export root this run did not
    # write. OFF by default — an export never deletes anything unless the user
    # explicitly asked for a mirror of the selection.
    "export_prune": False,
    # Parallel transcode/copy workers; 0 = automatic (half the cores, max 8).
    "export_workers": 0,

    # ── Accounts, remote access and notifications (v3) ────────────────────
    # The gate that protects everything but /api/health, /api/auth/* and the
    # static shell. "auto" is the shipped rule: ON whenever the server is
    # reachable from anywhere but this machine's loopback (`server_host` is
    # not a loopback address), OFF for loopback — a single-user desktop
    # install keeps working with no password to type. "required" gates
    # loopback too (two people sharing a machine, a kiosk); "off" only ever
    # applies to loopback and is for a machine with no other user.
    "auth_mode": "auto",
    # Optional label shown on the login screen ("who am I signing in as").
    # The password is the credential; there is no user database.
    "auth_username": "",
    # PBKDF2-HMAC-SHA256 — "pbkdf2$<iterations>$<salt-hex>$<hash-hex>".
    # Never the password itself. Written by /api/auth/password, read by
    # server/auth.py; an unparsable value is treated as "no password set".
    "auth_password_hash": "",
    # How long a login lasts. Sessions live in .mlo/data/auth.db, so
    # restarting the server does not sign every client out.
    "auth_session_days": 30,
    # Where the server binds. A non-loopback address (0.0.0.0, a LAN IP) is
    # what makes the gate mandatory under `auth_mode: auto`.
    "server_host": "127.0.0.1",
    "server_port": 8000,
    # The address clients should dial, e.g. "http://musicbox.lan:8000" or a
    # Tailscale name. Empty means "wherever this page was served from", which
    # is right for the browser and the desktop shell.
    "server_public_url": "",
    # Desktop/mobile/web notifications for the events the app already has:
    # a wish found on Soulseek, a download finished, an album ready to
    # import — plus the two "it began" halves of a Soulseek transfer, a
    # download whose first bytes moved and a peer taking files from us. Each
    # client asks for its own OS permission; these switches are the
    # server-side half (what gets published at all).
    "notify_wish_found": True,
    "notify_download_done": True,
    "notify_import_ready": True,
    # The import phase itself: started when the chain picks the album up, done
    # when it has been over it (the chain's own one-line summary rides along).
    "notify_import_start": True,
    "notify_import_done": True,
    "notify_soulseek_download_start": True,
    "notify_soulseek_upload_start": True,
    # UI language for the web app and the client shells. English is the
    # shipped language and the fallback for every key a locale does not
    # translate (web/src/locales). The library's own tag language — the one
    # that decides whether lyrics need translating — is
    # `lyrics_translation_langs`.
    "ui_locale": "en",

    # First run / updates
    "first_run_done": False,
    # Install tools that are missing or behind their upstream release without
    # waiting for the Dependencies page. OFF by default: it downloads binaries
    # on its own schedule, which is a decision the user makes, not one the app
    # makes for them. The worker reads this every pass, so switching it off
    # stops the next pass immediately.
    "dependencies_auto_update": False,
    # Sidecar files (cue/log/lrc/accurip) shown as extra rows in library views
    "show_sidecar_files": False,
}


_BOOL_KEYS = {
    key for key, value in DEFAULT_CONFIG.items() if isinstance(value, bool)
}
_INT_RANGES = {
    "video_crf": (0, 51),
    "soulseek_listen_port": (1024, 65535),
    "soulseek_web_port": (1024, 65535),
    "soulseek_up_limit": (0, 100000),
    "soulseek_down_limit": (0, 100000),
    "soulseek_download_slots": (1, 20),
    "soulseek_upload_slots": (0, 20),
    "soulseek_upload_limit_kib": (0, 1000000),
    "soulseek_download_limit_kib": (0, 1000000),
    "youtube_max_height": (0, 4320),
    "video_flac_level": (0, 8),
    "jpegxl_effort": (1, 10),
    "lrc_timestamp_precision": (2, 3),
    "png_optimization_level": (0, 6),
    "audit_cutoff_allow": (0, 24000),
    "audit_batch_size": (50, 500),
    "audit_batch_timeout_s": (10, 120),
    "audit_per_file_timeout_s": (10, 60),
    "images_jpeg_quality": (70, 100),
    "cover_jpeg_quality": (70, 100),
    "embed_cover_jpeg_quality": (60, 100),
    "embed_cover_resolution": (0, 4000),
    "grader_cover_size_tolerance_px": (0, 5),
    "grade_log_score_threshold": (0, 100),
    "audit_log_score_threshold": (0, 100),
    "worker_limit": (0, 64),
    "download_concurrency": (1, 8),
    "cover_target_size": (0, 4000),
    "cover_jpeg_target_size": (0, 4000),
    "cover_png_target_size": (0, 4000),
    "cover_jxl_target_size": (0, 4000),
    "soulseek_auto_log_min_score": (0, 100),
    "soulseek_auto_search_wait": (2, 300),
    "soulseek_auto_response_limit": (5, 500),
    "soulseek_search_concurrency": (1, 8),
    "soulseek_candidate_slots": (1, 20),
    "wishes_interval_hours": (1, 168),
    "wishes_max_attempts": (0, 1000),
    # The not-found budget and the retry backoff's step (see the wishes block
    # in DEFAULT_CONFIG): both 0 = off, and a backoff longer than a day is
    # pointless because retry_delay caps there anyway.
    "wishes_not_found_attempts": (0, 1000),
    "wishes_retry_backoff_minutes": (0, 1440),
    # A watch may not be checked more than hourly (MusicBrainz etiquette), and
    # a longer gap than a month is not a watch any more. The per-cycle cap's
    # floor is 1: a watch that may queue nothing would never add anything.
    "artist_watch_interval_hours": (1, 720),
    "artist_watch_max_per_cycle": (1, 50),
    "home_recent_count": (4, 60),
    "artist_image_target_size": (0, 4000),
    "discovery_timeout_s": (3, 30),
    "import_bulk_concurrency": (1, 8),
    "mb_genre_count": (1, GENRE_COUNT_MAX),
    "export_embed_cover_jpeg_quality": (1, 100),
    "export_embed_cover_resolution": (0, 8000),
    "export_workers": (0, 64),
    "server_port": (1, 65535),
    "auth_session_days": (1, 3650),
    # The library codec target's rate and compression level. The bitrate is
    # clamped per codec when it is USED (mlo.containers.encoder_args — ogg
    # takes 0-10, mp3 up to 320 kbps), so this is the widest legal span; 0
    # means "the codec's own shipped default".
    "library_codec_bitrate": (0, 512),
    "library_codec_quality": (0, 8),
}
_CHOICES = {
    "lyrics_format": {"EMBEDDED", "LRC", "BOTH"},
    # Import autonomy: the whole chain, or the wizard's stop-at-each-step
    # behaviour applied to the pipeline (see DEFAULT_CONFIG).
    "import_autonomy": {"automatic", "review"},
    "auth_mode": {"auto", "required", "off"},
    "advisory_fallback": {"0", "2", "none"},
    "ai_genre_effort": {"minimal", "low", "medium", "high", "max"},
    "lrc_zero_timestamp_target": {"EMBEDDED", "LRC", "BOTH"},
    # Sync granularity required of (and targeted for) synced lyrics:
    # SYLLABLE = glued per-syllable ELRC tags, WORD = per-word ELRC tags,
    # LINE = plain [mm:ss.xx] line timestamps only.
    "lrc_sync_level": {"SYLLABLE", "WORD", "LINE"},
    "ai_effort": {"minimal", "low", "medium", "high", "max"},
    "cue_file_type": {"WAVE", "MP3"},
    "audiometa_key_notation": {"musical", "camelot", "openkey"},
    "video_preset": {"ultrafast", "superfast", "veryfast", "faster", "fast",
                     "medium", "slow", "slower", "veryslow"},
    "mood_source": {"audio", "provider", "hybrid"},
    "replaygain_mode": {"track", "album", "off"},
    # The export's own loudness/equalizer/destination choices (see
    # DEFAULT_CONFIG): a stored typo falls back to the shipped default rather
    # than reaching ffmpeg or the exporter as an unknown mode.
    "export_replaygain_mode": {"off", "tags", "apply"},
    "export_target": {"server", "zip"},
    # The library's audio codec target and what the optimisation pass may do
    # with it — the values mlo.containers.CODECS and mlo.flac define.
    "library_codec": {"flac", "alac", "wav", "aiff", "mp3", "aac", "ogg",
                      "opus", "keep"},
    "library_codec_optimize": {"all", "lossless_to_lossy", "keep"},
    # How yt-dlp gets the user's cookies, and which browser's store it reads
    # in browser mode. The browser list is yt-dlp's own (server/youtube.py
    # holds the same tuple as COOKIES_BROWSERS, so the validator, the settings
    # field and the module cannot drift apart).
    "youtube_cookies_mode": {"none", "file", "browser"},
    "youtube_cookies_browser": {"chrome", "chromium", "edge", "firefox",
                                "brave", "opera", "safari", "vivaldi",
                                "whale"},
}


def _as_bool(value, default):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off", ""}:
            return False
    return default


def normalize_config(user=None) -> dict:
    """Return a validated, deep-copied configuration.

    Configuration files are user-editable, so malformed values must not leak
    into the GUI as truthy strings or invalid encoder arguments.
    """
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if isinstance(user, dict):
        # Never persist transient 'targets' (GUI selection) to disk
        user = {k: v for k, v in user.items() if k != "targets"}
        # Deep-merge nested dicts instead of shallow overwrite
        for k in ("encoder_tags", "audio_tag_writes"):
            if k in user and isinstance(user[k], dict) and isinstance(cfg[k], dict):
                merged = copy.deepcopy(cfg[k])
                merged.update(user[k])
                user[k] = merged
        cfg.update(user)


    # The codec settings used to be split across three keys: the FLAC-only
    # `flac_level`, the lossless target `lossless_target_codec` and the on/off
    # `optimize_convert_lossless`. They are one target now (`library_codec`
    # with its quality/bitrate/args and `library_codec_optimize`), so a saved
    # install is carried over: a level or codec the user actually CHOSE is a
    # decision and is kept, while the old shipped defaults were never a
    # decision (the same rule the LEGACY_* rewrites below follow) and move to
    # the new ones. Runs before validation, so a migrated value is normalized
    # like any other.
    saved = user if isinstance(user, dict) else {}

    # The locale that decides what names and aliases are WRITTEN in used to be
    # the beets import's own key (`beets_locale`). It is the app's ONE locale
    # now — the MusicBrainz pages, the beets import and the Soulseek alias
    # searches all read `locale` — so a saved value is carried over and the old
    # key dropped. A value here is a DECISION, which is why it is carried
    # whatever it says: "en" was the old shipped default and is the new one, so
    # there is no default to tell apart from a choice.
    if "locale" not in saved and str((saved or {}).get("beets_locale") or "").strip():
        cfg["locale"] = saved["beets_locale"]
    cfg.pop("beets_locale", None)
    if "library_codec_quality" not in saved:
        try:
            level = int(saved.get("flac_level"))
        except (TypeError, ValueError):
            level = None
        # 8 was the old shipped default: an install still holding it never
        # chose it and follows the new default (5).
        if level is not None and level != 8:
            cfg["library_codec_quality"] = level
    if "library_codec" not in saved and str(saved.get("lossless_target_codec") or "").strip():
        cfg["library_codec"] = saved["lossless_target_codec"]
    if "library_codec_optimize" not in saved and saved.get("optimize_convert_lossless") is False:
        cfg["library_codec_optimize"] = "keep"
    cfg.pop("flac_level", None)
    cfg.pop("lossless_target_codec", None)
    cfg.pop("optimize_convert_lossless", None)

    # An export's ReplayGain used to be one boolean (export_replaygain). It is
    # a three-way choice now (export_replaygain_mode) because "write the tags"
    # and "rewrite the audio" are different jobs: a player that honours tags
    # applies them, a player that honours nothing needs the gain in the
    # samples. A saved true becomes "tags" — precisely what it used to do —
    # and a saved false becomes "off".
    if "export_replaygain_mode" not in saved and "export_replaygain" in saved:
        cfg["export_replaygain_mode"] = (
            "tags" if _as_bool(saved.get("export_replaygain"), False) else "off")
    cfg.pop("export_replaygain", None)

    for key in _BOOL_KEYS:
        cfg[key] = _as_bool(cfg.get(key), DEFAULT_CONFIG.get(key, False))

    for key, (low, high) in _INT_RANGES.items():
        try:
            value = int(cfg.get(key, DEFAULT_CONFIG[key]))
        except (TypeError, ValueError):
            value = DEFAULT_CONFIG[key]
        cfg[key] = max(low, min(high, value))

    for key, choices in _CHOICES.items():
        raw = str(cfg.get(key, DEFAULT_CONFIG[key]))
        canonical = {c.upper(): c for c in choices}
        cfg[key] = canonical.get(raw.upper(), DEFAULT_CONFIG[key])

    folder = cfg.get("music_folder", "")
    cfg["music_folder"] = folder.strip() if isinstance(folder, str) else ""

    source = cfg.get("digital_media_source_value", DEFAULT_DIGITAL_SOURCE)
    cfg["digital_media_source_value"] = (
        str(source).strip() or DEFAULT_DIGITAL_SOURCE
    )

    # The preferred release country: a bare ISO 3166-1 alpha-2 code (the
    # spelling MusicBrainz publishes on the release) compared case-
    # insensitively. Kept upper-cased and short; anything unrecognized simply
    # never matches, which is exactly what the default (blank = no preference)
    # already means.
    cfg["prefer_release_country"] = (
        str(cfg.get("prefer_release_country") or "").strip().upper()[:12]
    )

    # `library_codec_args` reaches the encoder verbatim (mlo.containers.
    # codec_extra_args splits it on whitespace), so the app does not validate
    # its content — only its shape: control characters and newlines become
    # spaces (a hand-edited config must not smuggle a NUL into an argv) and
    # the field is length-capped.
    codec_args = "".join(ch if ch.isprintable() else " "
                         for ch in str(cfg.get("library_codec_args") or ""))
    cfg["library_codec_args"] = " ".join(codec_args.split())[:200]

    # `last_update_check` / `update_check_interval_days` belonged to an
    # app-update checker that no longer exists; nothing reads them, so they
    # are not written back (the app's update story is the dependency check in
    # mlo/fetchdeps.py).
    cfg.pop("last_update_check", None)
    cfg.pop("update_check_interval_days", None)

    try:
        thr = float(cfg.get("cover_crop_threshold", 0.05))
        cfg["cover_crop_threshold"] = max(0.0, min(0.5, thr))
    except (TypeError, ValueError):
        cfg["cover_crop_threshold"] = 0.05

    # The artist image's configured aspect ("W:H"). Validated with the parser the
    # fetch, the audit and script 19 all read, so a hand-edited config cannot
    # wedge the grader into failing every artist image for a shape nothing can
    # parse — an unusable value falls back to the shipped default.
    from .artistdata import parse_aspect
    if parse_aspect(cfg.get("artist_image_aspect")) is None:
        cfg["artist_image_aspect"] = DEFAULT_CONFIG["artist_image_aspect"]

    for k, default, lo, hi in (
        ("discs_toc_tolerance_s", 4.0, 0.5, 10.0),
        ("discs_toc_unique_margin_s", 4.0, 0.5, 10.0),
        ("jpegxl_distance", 0.0, 0.0, 2.0),
        ("grader_strict_square_threshold", 0.005, 0.0, 0.05),
        ("acoustid_min_score", 0.75, 0.0, 1.0),
        ("replaygain_preamp_db", 0.0, -24.0, 24.0),
    ):
        try:
            v = float(cfg.get(k, default))
            cfg[k] = max(lo, min(hi, v))
        except (TypeError, ValueError):
            cfg[k] = default

    for k in ("cover_target_size", "cover_jpeg_target_size",
              "cover_png_target_size", "cover_jxl_target_size"):
        try:
            cfg[k] = int(cfg.get(k, DEFAULT_CONFIG[k]) or 0)
        except (TypeError, ValueError):
            cfg[k] = DEFAULT_CONFIG[k]
        cfg[k] = max(0, min(4000, cfg[k]))

    # Discs rename pattern: must contain {n}, reasonable length, no path sep
    pat = cfg.get("discs_rename_pattern", "CD-{n}")
    if not isinstance(pat, str) or "{n}" not in pat:
        pat = "CD-{n}"
    # Sanitize: no directory separators, no null, limit length
    pat = pat.replace("/", "").replace("\\", "").strip()[:32] or "CD-{n}"
    if "{n}" not in pat:
        pat = "CD-{n}"
    cfg["discs_rename_pattern"] = pat

    try:
        ratio = float(cfg.get("soulseek_auto_complete_ratio", 1.0))
        cfg["soulseek_auto_complete_ratio"] = max(0.5, min(1.0, ratio))
    except (TypeError, ValueError):
        cfg["soulseek_auto_complete_ratio"] = 1.0
    for k in ("soulseek_auto_physical_queries", "soulseek_auto_cd_queries",
              "soulseek_auto_digital_queries"):
        v = cfg.get(k)
        if isinstance(v, str):
            # the settings UI edits templates as one ";"-separated line
            v = [t for t in v.split(";") if t.strip()]
        if not isinstance(v, list):
            v = list(DEFAULT_CONFIG[k])
        clean = [str(t).strip()[:120] for t in v if str(t).strip()]
        cfg[k] = clean[:6] or list(DEFAULT_CONFIG[k])

    # Release-choice medium order: unknown labels are kept (slskd/MB may add
    # formats), an empty list falls back to the default.
    v = cfg.get("auto_import_medium_order")
    if isinstance(v, str):
        v = [t for t in v.replace("\n", ";").split(";") if t.strip()]
    if not isinstance(v, (list, tuple)):
        v = []
    cfg["auto_import_medium_order"] = (
        [str(t).strip() for t in v if str(t).strip()][:12]
        or list(DEFAULT_CONFIG["auto_import_medium_order"])
    )
    v = cfg.get("genre_sources")
    if isinstance(v, str):
        v = [t for t in v.replace("\n", ";").split(";") if t.strip()]
    if not isinstance(v, (list, tuple)):
        v = []
    saved = [str(t).strip().lower() for t in v if str(t).strip()][:16]
    # The shipped order used to be all eleven sources. A saved list that is
    # byte-for-byte that old default was never a user decision, so it follows
    # the new default (RateYourMusic + MusicBrainz); a list the user actually
    # edited is kept exactly as saved.
    if saved == LEGACY_GENRE_SOURCES:
        saved = []
    # An untouched install holds a copy of one of the OLD shipped defaults,
    # which is not a choice: swap it for the current one (a customised list is
    # kept as written).
    if saved in LEGACY_DEFAULT_GENRE_SOURCES:
        saved = []
    cfg["genre_sources"] = saved or list(DEFAULT_CONFIG["genre_sources"])

    # Release-group types a watch may queue: a CLOSED vocabulary (MusicBrainz's
    # own names, compared case-insensitively), so an unknown name is dropped
    # rather than kept as a filter that could never match anything — and a list
    # left with nothing selectable falls back to the shipped default (album+EP)
    # instead of leaving every watch type-less.
    v = cfg.get("artist_watch_types")
    if isinstance(v, str):
        v = [t for t in v.replace("\n", ";").split(";") if t.strip()]
    if not isinstance(v, (list, tuple)):
        v = []
    known = {t.lower() for t in (PRIMARY_RELEASE_TYPES + SECONDARY_RELEASE_TYPES)}
    picked = []
    for t in v:
        name = str(t).strip().lower()
        if name in known and name not in picked:
            picked.append(name)
    cfg["artist_watch_types"] = (picked[:len(known)]
                                 or list(DEFAULT_CONFIG["artist_watch_types"]))

    # An untouched install holds the old shipped genres-per-track count (see
    # LEGACY_DEFAULT_GENRE_COUNTS) and follows the new one.
    if cfg.get("mb_genre_count") in LEGACY_DEFAULT_GENRE_COUNTS:
        cfg["mb_genre_count"] = DEFAULT_CONFIG["mb_genre_count"]

    # The instrumental advisory zero-fill shipped OFF and is ON now. An install
    # whose saved config carries the old default never chose it (the settings
    # form wrote the shipped value on the first save), so it follows the new
    # one — the same rule the naming script and the genre defaults use. A
    # config that genuinely wants the old behavior sets it off again.
    if cfg.get("auto_zero_advisory_for_instrumental") is False:
        cfg["auto_zero_advisory_for_instrumental"] = True

    # Auto-import query templates: the old shipped defaults narrow to the
    # catalog-number-only (CD) / artist-album-year (digital) wording. The
    # physical key is newer than any of them, so the lists a physical release
    # WAS searched with before it existed — a CD by the cd key, every other
    # pressing by the digital one, which is exactly the fall-through the
    # physical key ends — are no more a decision about it than the others are:
    # a config holding one of them follows the shipped default.
    legacy_physical = (["catalognumber"], *LEGACY_DEFAULT_DIGITAL_QUERIES,
                       *LEGACY_DEFAULT_CD_QUERIES)
    for key, legacy in (("soulseek_auto_cd_queries", LEGACY_DEFAULT_CD_QUERIES),
                        ("soulseek_auto_digital_queries", LEGACY_DEFAULT_DIGITAL_QUERIES),
                        ("soulseek_auto_physical_queries", legacy_physical)):
        stored = cfg.get(key)
        if isinstance(stored, str):
            stored = [t for t in stored.replace("\n", ";").split(";") if t.strip()]
        if isinstance(stored, (list, tuple)) and [str(t).strip() for t in stored] in [
                [str(t) for t in old] for old in legacy]:
            stored = list(DEFAULT_CONFIG[key])
        if isinstance(stored, (list, tuple)):
            cfg[key] = [str(t).strip() for t in stored if str(t).strip()]

    # Keys that no longer drive anything (the recommendation shelves, the
    # catalogue-search source order, the MusicBrainz browser's search mode):
    # dropped here so a saved config stops carrying them around.
    for dead in ("home_recommendations", "home_rec_count", "home_popular_count",
                 "home_rec_source", "discovery_rec_sources",
                 "discovery_search_sources", "mb_search_source",
                 "soulseek_auto_max_attempts"):
        cfg.pop(dead, None)

    # `auth_username` is a display name AND, on an install with no users row,
    # the name its claim logs in under — which makes it a path segment
    # (`trash/<user>/`) and a SQLite key. A value that is not one plain segment
    # is blanked rather than honoured: a hand-edited config must not be able to
    # name a user `..` and hand that session the app's own data directory.
    from .paths import user_segment
    name = str(cfg.get("auth_username") or "").strip()
    if name and user_segment(name) != name:
        cfg["auth_username"] = ""

    # Shared-folder lists (Settings → Soulseek, Soulseek → Sharing). Accept a
    # ";"/newline separated string from hand-edited config files.
    for k in ("soulseek_share_dirs", "soulseek_share_exclude"):
        v = cfg.get(k)
        if isinstance(v, str):
            v = [t for t in v.replace("\n", ";").split(";") if t.strip()]
        if not isinstance(v, (list, tuple)):
            v = []
        cfg[k] = [str(t).strip() for t in v if str(t).strip()][:64]

    # Provider preference lists (discovery, lyrics, artist images, artwork
    # text). Empty means "use the built-in order"; unknown ids are dropped so
    # a hand-edited config can never wedge a feature.
    for k in ("lyrics_sources", "artist_image_sources", "description_sources"):
        v = cfg.get(k)
        if isinstance(v, str):
            v = [t for t in v.replace("\n", ";").split(";") if t.strip()]
        if not isinstance(v, (list, tuple)):
            v = []
        cfg[k] = [str(t).strip().lower() for t in v if str(t).strip()][:16]

    # Import script chain: script ids, order preserved, duplicates dropped.
    scripts = cfg.get("import_scripts")
    if isinstance(scripts, str):
        scripts = scripts.replace(";", ",").replace("\n", ",").split(",")
    if not isinstance(scripts, (list, tuple)):
        scripts = []
    chain = []
    for item in scripts:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value not in chain:
            chain.append(value)
    cfg["import_scripts"] = chain[:32]

    # Import autonomy: which families the user decides by hand. The names are
    # validated against mlo.import_policy's own registry (this list is the one
    # place the pipeline and the wizard both read), so a typo in a hand-edited
    # config cannot make the pipeline review a family that does not exist —
    # and the stored order is the wizard's, so "first" always means first step.
    from .import_policy import configured_families
    cfg["import_review_families"] = list(configured_families(cfg))

    script = cfg.get("naming_script")
    if not isinstance(script, str) or not script.strip():
        cfg["naming_script"] = DEFAULT_NAMING_SCRIPT
    else:
        script = script.strip()[:2000]
        # migration: a stored PRE-2.4.0 shipped default is not a custom script
        if script in LEGACY_DEFAULT_NAMING_SCRIPTS:
            script = DEFAULT_NAMING_SCRIPT
        cfg["naming_script"] = script

    default_tags = DEFAULT_CONFIG["encoder_tags"]
    user_tags = cfg.get("encoder_tags") if isinstance(cfg.get("encoder_tags"), dict) else {}
    merged_tags = {}
    for file_type, fields in default_tags.items():
        merged_tags[file_type] = dict(fields)
        values = user_tags.get(file_type)
        if isinstance(values, dict):
            for field in fields:
                merged_tags[file_type][field] = _as_bool(
                    values.get(field), fields[field]
                )
    cfg["encoder_tags"] = merged_tags

    # Audio tag writes per filetype
    default_audio = DEFAULT_CONFIG.get("audio_tag_writes", {})
    user_audio = cfg.get("audio_tag_writes") if isinstance(cfg.get("audio_tag_writes"), dict) else {}
    merged_audio = {}
    for ftype in AUDIO_TAG_TYPES:
        base = default_audio.get(ftype, {k: True for k in AUDIO_TAG_FAMILIES})
        merged_audio[ftype] = dict(base)
        vals = user_audio.get(ftype)
        if isinstance(vals, dict):
            for fam in AUDIO_TAG_FAMILIES:
                if fam in vals:
                    merged_audio[ftype][fam] = _as_bool(vals.get(fam), base.get(fam, True))
    # alias: if user used "m4a" key, fold into mp4
    if isinstance(user_audio.get("m4a"), dict):
        for fam in AUDIO_TAG_FAMILIES:
            if fam in user_audio["m4a"]:
                merged_audio["mp4"][fam] = _as_bool(user_audio["m4a"].get(fam), merged_audio["mp4"].get(fam, True))
    cfg["audio_tag_writes"] = merged_audio

    order = cfg.get("run_all_order", DEFAULT_RUN_ALL_ORDER)
    clean_order = []
    if isinstance(order, (list, tuple)):
        for value in order:
            try:
                script_id = int(value)
            except (TypeError, ValueError):
                continue
            # A saved order keeps scripts 1..14 in the user's sequence. The
            # ids added later (15 tracklist, 16 mood, 17 AI transforms) are
            # shed and re-inserted at their canonical anchors below: a saved
            # "15" predates the tracklist script (it was the lyrics
            # xlit/translate script when the id last moved), and the anchor
            # rule puts every later id where the pipeline wants it.
            #
            # 20 (layout scan) and 21 (AcoustID pairs) are KEPT instead: they
            # are the newest ids and have never meant anything else, so a saved
            # order that holds one holds the user's own position for it, and
            # shedding it would silently undo that. An order written before
            # they existed simply has neither and gets them from the same
            # anchor rule below.
            if ((1 <= script_id <= 14 or script_id in (20, 21))
                    and script_id not in clean_order):
                clean_order.append(script_id)
    # Migrate legacy sequential default [1..8] to systematic pipeline
    if clean_order == [1, 2, 3, 4, 5, 6, 7, 8] and clean_order != list(DEFAULT_RUN_ALL_ORDER):
        clean_order = list(DEFAULT_RUN_ALL_ORDER)
    # Migrate old 8-item order without AccurateRip to new 10-item order
    if clean_order == [1, 2, 8, 3, 5, 6, 4, 7]:
        clean_order = list(DEFAULT_RUN_ALL_ORDER)
    # Ensure new Format All (10) is present at end for existing installs that had old 9-item order
    if 10 not in clean_order and clean_order == [1, 2, 8, 3, 5, 9, 6, 4, 7]:
        clean_order.append(10)
    # Also handle case where user had old 9 at end: [1,2,8,3,5,6,4,7,9] -> migrate to new order with 10 at end
    if clean_order == [1, 2, 8, 3, 5, 6, 4, 7, 9]:
        clean_order = [1, 2, 8, 3, 5, 9, 6, 4, 7, 10]

    # Scripts added later join existing pipelines at sensible positions:
    #   14 beets tagging   — right after the remux (writes tags, places files)
    #   13 lyric fetching  — after autotag (needs final ARTIST/TITLE), before grade
    #   12 Key & BPM       — after 13 (grading wants its tags)
    #   15 tracklist       — after beets (needs its MusicBrainz match)
    def _insert_script(order, sid, anchors):
        if sid in order:
            return
        for anchor in anchors:
            if anchor in order:
                order.insert(order.index(anchor) + 1, sid)
                return
        order.append(sid)

    if clean_order:
        _insert_script(clean_order, 14, [11])
        _insert_script(clean_order, 13, [8, 14, 11])
        _insert_script(clean_order, 12, [13, 8])
        _insert_script(clean_order, 15, [14, 12, 13, 8])
        # 16 mood & energy — with the other librosa pass (12), after the
        # tagging that gives its genre prior something to work with
        _insert_script(clean_order, 16, [12, 13, 8])
        # 18 publish — right after the lyrics it submits (13); 17 AI
        # transforms — after the lyrics it reads, before the analysis passes
        # and grading
        _insert_script(clean_order, 18, [13, 12, 16])
        _insert_script(clean_order, 17, [18, 13, 12, 16])
        # 19 artist images — with script 5's image pass, whose policy it shares
        _insert_script(clean_order, 19, [5, 8, 16])
        # 20 layout scan — right before grading, so the report describes the
        # names 10 (Format all, the canonical trim) has just settled instead
        # of the ones a saved order was still about to rewrite
        _insert_script(clean_order, 20, [10, 16, 12])
        # 21 AcoustID pairs — after 20 and so also before the grader, which is
        # the script that reports the incomplete pair it completes
        _insert_script(clean_order, 21, [20, 10, 16, 12])
    cfg["run_all_order"] = clean_order or list(DEFAULT_RUN_ALL_ORDER)
    return cfg


def active_config_file():
    """Where the live configuration lives: <music folder>/.mlo/data/config.json
    once a music folder is known, the repo-local config.json otherwise."""
    mf = read_music_folder_guess()
    if mf:
        cand = os.path.join(app_data_dir(mf), "config.json")
        if os.path.isfile(cand):
            return cand
    return CONFIG_FILE


_MIGRATED = False


def _migrate_to_data_dir():
    """One-time move of ALL app state into the new layout:
    <music folder>/.mlo/data (config.json plus everything from the legacy
    state dirs: beets library + config, playlists/likes database, slskd.yaml
    and any other app-written state) and the transient dirs into
    <music folder>/.mlo —
    .mlo_downloads -> .mlo/downloads (incl. .incomplete/) and .mlo_trash ->
    .mlo/trash (incl. .mlo_manifest.json). The old top-level .mlo_data is one
    of the legacy sources and is removed once drained. Existing files are
    moved, never clobbered — except an app-created EMPTY database at the
    destination (see _is_empty_db) — and a stub config.json stays behind at
    the legacy path so the music folder can still be located on the next
    run."""
    global _MIGRATED
    if _MIGRATED:
        return
    _MIGRATED = True
    import shutil

    mf = read_music_folder_guess()
    if not mf or not os.path.isdir(mf):
        return
    d = app_data_dir(mf)
    if os.path.abspath(d) == os.path.abspath(os.path.dirname(CONFIG_FILE)):
        return
    try:
        os.makedirs(d, exist_ok=True)
        # Every dir that may hold pre-migration state, most authoritative
        # first: this scope's Data/.mlo_data, then a previous music folder's
        # (nobody rewrote the stub), then repo-local server/data. Never
        # clobbered.
        # A source that belongs to the PREVIOUS music folder is another
        # install's state — a scope handed in through MLO_MUSIC_FOLDER or a
        # stale stub. COPY those, never move: a throwaway scope (a test, a
        # probe run) that is deleted afterwards must not be able to strand
        # the real install's config, playlists or beets library.
        from .paths import previous_state_dirs

        foreign = {os.path.normcase(os.path.abspath(p))
                   for p in previous_state_dirs(mf)}
        for src in legacy_state_dirs(mf):
            _move_state_dir(src, d,
                            copy=os.path.normcase(os.path.abspath(src)) in foreign)
        # Transient dirs keep their contents (downloads' .incomplete/ and the
        # bin's .mlo_manifest.json simply travel as entries).
        _move_state_dir(os.path.join(mf, ".mlo_downloads"), downloads_dir(mf))
        _move_state_dir(os.path.join(mf, ".mlo_trash"), trash_dir(mf))
        new_cfg = os.path.join(d, "config.json")
        if os.path.isfile(CONFIG_FILE) and not os.path.exists(new_cfg):
            # Copy, not move: the stub path is rewritten right below anyway,
            # and a scope that is thrown away afterwards must not take the
            # only copy of the settings with it.
            shutil.copy2(CONFIG_FILE, new_cfg)
        # A carried-over config may still point at its old location (the very
        # bug this fixes): keep the live value aligned with the new folder.
        if os.path.isfile(new_cfg):
            try:
                with open(new_cfg, "r", encoding="utf-8") as f:
                    moved = json.load(f)
                if moved.get("music_folder") != mf:
                    moved["music_folder"] = mf
                    with open(new_cfg, "w", encoding="utf-8") as f:
                        json.dump(moved, f, indent=2, sort_keys=True)
                        f.write("\n")
            except Exception:
                pass
        # stub so read_music_folder_guess() keeps resolving after the move
        _write_stub(mf)
    except Exception as e:
        print(f"WARNING: state migration to {d} failed: {e}")


_TRASH_ADOPTED = False


def _adopt_legacy_trash():
    """Move a bin written before users existed into the `default` scope.

    `<music>/.mlo/trash` used to BE the bin; it is now the PARENT of the
    per-user bins (`<music>/.mlo/trash/<user>`). Without this an upgraded
    install opens an empty bin while every deleted file is still on disk one
    level up — the worst version of this bug, because the page says there is
    nothing to restore and nothing looks broken. Runs once per process, lists
    the entries BEFORE creating the destination, and never clobbers.
    """
    global _TRASH_ADOPTED
    if _TRASH_ADOPTED:
        return
    _TRASH_ADOPTED = True
    import shutil

    try:
        mf = read_music_folder_guess()
        if not mf or not os.path.isdir(mf):
            return
        dst = trash_dir(mf)
        src = os.path.dirname(dst) if dst else None
        # A bin that is already per-user (or absent) has nothing to adopt.
        if not src or os.path.isdir(dst) or not os.path.isdir(src):
            return
        entries = [n for n in os.listdir(src) if n != os.path.basename(dst)]
        if not entries:
            return
        os.makedirs(dst, exist_ok=True)
        for name in entries:
            s, d = os.path.join(src, name), os.path.join(dst, name)
            if os.path.exists(d):
                continue
            shutil.move(s, d)
    except Exception as e:
        print(c(f"WARNING: could not move the old bin into the default scope: {e}",
                Color.YELLOW))


def load_config() -> dict:
    _migrate_to_data_dir()
    _adopt_legacy_trash()
    path = active_config_file()
    user = None
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                user = json.load(f)
        except Exception as e:
            # honey: corrupt config still boots on defaults; cause goes to the
            # log (and stays out of the UI) instead of vanishing silently.
            print(c(f"WARNING: ignoring corrupt config {path}: {e}", Color.YELLOW))
            user = None
    return normalize_config(user)


def _is_empty_db(path):
    """True when *path* is a SQLite file with no row in any of its tables.

    Safety net for the no-clobber rule: an empty database can already sit at
    the destination when the migration runs (an older build opened its
    playlists/wishes database during startup, or an interrupted run left a
    freshly created file behind). Treating that as 'existing data' would
    skip the user's real database and orphan it in the old folder — an empty
    app-created DB never outranks pre-migration state."""
    if not str(path).lower().endswith(".db") or not os.path.isfile(path):
        return False
    try:
        import sqlite3
        con = sqlite3.connect(path, timeout=5)
        try:
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            return not any(con.execute(f'SELECT 1 FROM "{t}" LIMIT 1').fetchone()
                           for t in tables)
        finally:
            con.close()
    except Exception:
        return False


def _merge_state_dir(src, dst, copy=False):
    """Fill *dst* in from *src*, recursive, never overwriting a file.

    Used for the directory entries of a state move: a plain "the destination
    exists, skip it whole" is wrong for a tree, because an interrupted move
    (or a copy that was killed) leaves a partial one there. Files keep the
    no-clobber rule — only what is missing is filled in — so a merge can
    never replace live data, only complete it. Emptied source directories are
    removed as the recursion unwinds, which is what lets the caller's final
    ``os.rmdir(src)`` succeed.
    """
    import shutil

    try:
        os.makedirs(dst, exist_ok=True)
        for name in os.listdir(src):
            s, d = os.path.join(src, name), os.path.join(dst, name)
            if os.path.isdir(s):
                _merge_state_dir(s, d, copy)
                continue
            if os.path.exists(d):
                continue
            if copy:
                shutil.copy2(s, d)
            else:
                shutil.move(s, d)
        try:
            os.rmdir(src)
        except OSError:
            pass
    except Exception as e:
        print(f"WARNING: could not merge app state: {e}")


def _move_state_dir(src, dst, copy=False):
    """Move (or copy) app state files from one dir to another (never clobber).

    Used by the legacy-state migration, by the transient dirs
    (.mlo_downloads/.mlo_trash) and by a music-folder change, so playlists,
    the beets DB, wishes, slskd.yaml and the download/trash contents follow
    the library. ``copy=True`` leaves the source untouched — used when the
    source belongs to a DIFFERENT install (a scope set by MLO_MUSIC_FOLDER),
    where moving would strand that install. The one exception to no-clobber:
    an empty database the app itself created at the destination (see
    _is_empty_db). Returns True when a move was attempted.
    """
    import shutil

    if not src or not dst:
        return False
    if os.path.abspath(src) == os.path.abspath(dst):
        return False
    if not os.path.isdir(src):
        return False
    try:
        os.makedirs(dst, exist_ok=True)
        for name in os.listdir(src):
            if name == "tray.lock":  # runtime lock, not data
                continue
            s, d = os.path.join(src, name), os.path.join(dst, name)
            if os.path.isdir(s):
                # A directory at the destination is MERGED into, never skipped
                # whole: an interrupted move/copy leaves a partial (or empty)
                # tree there, and skipping it stranded the rest of the data —
                # the half-written .beets library stayed half-written and the
                # app opened an empty library. Files still never clobber
                # files, so live data is safe; the merge only fills gaps.
                _merge_state_dir(s, d, copy)
                continue
            if os.path.exists(d) and not (os.path.isfile(s) and _is_empty_db(d)):
                continue
            if copy:
                shutil.copy2(s, d)
            else:
                shutil.move(s, d)
        if not copy:
            try:
                os.rmdir(src)  # only when nothing was left behind (tray.lock, skipped)
            except OSError:
                pass
        return True
    except Exception as e:
        print(f"WARNING: could not move app state: {e}")
        return False


def _write_stub(music_folder):
    """Legacy-path config.json: records the folder that owns the .mlo/data
    dir so read_music_folder_guess() keeps resolving on the next run."""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"music_folder": music_folder}, f, indent=2)
            f.write("\n")
    except OSError:
        pass


def save_config(cfg: dict) -> bool:
    """Validate and atomically replace the persisted configuration."""
    global _MIGRATED
    try:
        # Merge onto the live config: callers may POST a partial dict (e.g. the
        # Soulseek port form sends two keys), and an omitted key must keep its
        # saved value instead of reverting to the factory default.
        current = load_config()
        current.update(cfg or {})
        # Drop keys outside the schema (obsolete/typo writes); 'targets' is
        # transient GUI selection and is stripped by normalize_config anyway.
        normalized = normalize_config(
            {k: v for k, v in current.items() if k in DEFAULT_CONFIG}
        )
        prev_mf = read_music_folder_guess() or ""
        new_mf = str(normalized.get("music_folder") or "").strip()
        # ALL app state lives in <music folder>/.mlo (data/, downloads/,
        # trash/): a folder change must carry all of it along, or the new
        # folder starts empty and the old one is orphaned.
        if (new_mf and prev_mf and os.path.isdir(new_mf)
                and os.path.abspath(new_mf) != os.path.abspath(prev_mf)):
            d = app_data_dir(new_mf)
            # the previous folder's own state dir first, then every older
            # layout it may still hold (Data, server/data)
            for src in (app_data_dir(prev_mf), *legacy_state_dirs(prev_mf)):
                _move_state_dir(src, d)
            _move_state_dir(downloads_dir(prev_mf), downloads_dir(new_mf))
            # The bin's ROOT, not one scope's bin: trash_dir() is
            # `<...>/trash/<user>` now, so moving it would carry the `default`
            # scope and strand every named user's bin in the old folder —
            # their Trash page would read an empty bin while the albums sat on
            # disk one layout up.
            _move_state_dir(trash_root(prev_mf), trash_root(new_mf))
            _write_stub(new_mf)
        path = active_config_file()
        directory = os.path.dirname(path) or "."
        fd, temp_path = tempfile.mkstemp(
            prefix=".mlo_config_", suffix=".json", dir=directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump(normalized, f, indent=2, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        # a save that introduces/changes the music folder triggers the move
        _MIGRATED = False
        _migrate_to_data_dir()
        return True
    except Exception as e:
        # honey: caller shows this after "Failed to save config" (+ disk-full /
        # perms cause instead of a bare False).
        print(c(f"ERROR: Could not save config: {e}", Color.RED))
        save_config.last_error = str(e)
        return False
