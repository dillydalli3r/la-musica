"""Library grader: per-album tag/lyrics/cover compliance reports."""
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from .audio import AudioFile, TAG_MAP
from .config import DEFAULT_CONFIG, should_write_audio_tag
from .lyrics import (
    _lrc_for, _canonical_lyrics, format_lyrics_text, has_lyrics_text,
    text_meets_sync_level,
)
# The genre list rules and the lyric-transform need rule each live in ONE
# module the writers already use (mlo.genres for the import / scripts 8, 10,
# mlo.lyrics_xlit for script 17), so the grader can never disagree with what
# those produce: it asks the same functions.
from .genres import issues as genre_issues
# The vocabulary predicate the genre writers canonicalize with, so the
# grade_check_genre_vocab check asks the SAME question of a name they do.
from .genres import canonical as genre_canonical, iter_names
# …and their capitalization, for the GENRE half of grade_check_tag_case:
# GENRE is an open multi-value tag whose canonical form is PER NAME
# (mlo.genres.display_name), so it has no entry in mlo.tagtext.CANONICAL_CASE
# and the value rule that lives there cannot answer for it.
from .genres import display_name as genre_display_name
# The tag CANONICAL-VALUE rule (spelling + spacing) the writers apply on every
# write (mlo.audio), so grade_check_tag_case / grade_check_tag_spaces can only
# ever fail what those writers would have fixed — and the case check is the
# SAME function, not a second opinion about what canonical means.
from .tagtext import MEDIA_VALUES as _MEDIA_VALUES, canonical_value, spacing_problem
from .lyrics_xlit import (
    XLIT_SIDECAR, dominant_script, primary_translation_lang, xlit_needs,
)
from .cue import canonical_cue_text
from .naming import (DEFAULT_NAMING_SCRIPT, UNKNOWN_RELEASE_TYPE,
                     cue_ref_names, lookup_style_release_type,
                     mb_style_release_type, name_key)
from .paths import (ALBUM_SIDECAR_NAMES, AUDIO_EXTS, IMAGE_EXTS,
                    LIB_AUDIO_EXTS, LIB_VIDEO_EXTS, get_track_cover,
                    library_root, load_expected_tracks, load_pending,
                    load_track_covers)
from .stats import (
    new_stats, _make_pbar, _pbar_skip, _pbar_update, is_audio_file,
    _find_albums, _clean_set, _summarize_values, _collect_targets,
    worker_count, WALK_EMPTY, WALK_FILES, WALK_MATCHED,
)
from .deps import HAS_PIL, Image
from .ui import print_header, log, c, Color, print_separator, _short_val

# Media formats the library understands (MusicBrainz-style MEDIA values).
# Everything outside CD / Digital Media in this set is graded like any other
# release — the CUE/LOG/AccurateRip expectations are already gated on
# _is_cd(media_summary) — while unknown values still fail grading.
#
# Folded from mlo.tagtext.MEDIA_VALUES, the table the writers canonicalize
# against: the value this grader accepts is exactly the value a writer stores,
# so a media the app can write can never be "unrecognized".
KNOWN_MEDIA = frozenset(v.lower() for v in _MEDIA_VALUES)


def _is_cd(media_summary):
    """Whether the album's MEDIA summary is a CD-DA release.

    MEDIA is user-written tag data, so the test is case-insensitive:
    MEDIA=cd is the same medium as CD (the unknown-value check against
    KNOWN_MEDIA lowercases too, so "cd" is accepted as known either way).
    """
    return str(media_summary or "").strip().lower() == "cd"


def _library_codec_spec(cfg):
    """mlo.containers' spec of the configured `library_codec` target, or None
    when the setting names no target ("keep") or the module is unavailable."""
    try:
        from .containers import CODECS
        from .flac import target_codec
    except Exception:
        return None
    return CODECS.get(target_codec(cfg or {}))


def _uncompressed_source_target(cfg):
    """The codec script 3 converts uncompressed sources to, or None.

    None means the pass will never convert one — either the library is kept
    as it is, or the target IS one of those containers (WAV/AIFF are PCM
    targets too) — which is what lets the grading check that flags stray
    WAV/AIFF files stand down instead of failing such an album forever.
    """
    try:
        from .flac import LOSSLESS_SOURCE_EXTS as src_exts
    except Exception:
        return "FLAC"
    spec = _library_codec_spec(cfg)
    if spec is None or spec["ext"] in src_exts:
        return None
    return spec["codec"].upper()


def _lossy_target_file(path, cfg):
    """Whether *path* is a file in the configured LOSSY library target.

    The CD-DA 16-bit/44.1 kHz check describes the master a rip produced; once
    the library has been converted to a lossy target those bytes are gone, so
    the check cannot judge such a file any more (Opus resamples to 48 kHz by
    design, and a CD album the user chose to convert must not fail over it).
    """
    spec = _library_codec_spec(cfg)
    if spec is None or spec["lossless"]:
        return False
    return os.path.splitext(str(path))[1].lower() == spec["ext"]


def _is_digital(media_summary):
    """Whether the album's MEDIA summary is Digital Media (case-insensitive
    for the same reason as _is_cd)."""
    return str(media_summary or "").strip().lower() == "digital media"


# Cover dimensions, keyed by (path, size, mtime_ns) — see
# _get_cover_dimensions. Bounded like mlo.discs._CRC_MEMO: a whole-library
# grade drops the map instead of retaining one entry per cover file forever.
_COVER_DIMS_MEMO = {}
_COVER_DIMS_MEMO_MAX = 4096


def _get_cover_dimensions(cover_path):
    """Cover dimensions of *cover_path*, memoised per (path, size, mtime).

    The dimension read is the expensive half of a cover check (Pillow decodes
    the image; JXL shells out to jxlinfo) and the same file is asked for it
    repeatedly: a per-track sidecar shared by several tracks, and the failure
    detail that re-reads the image a check has just opened. A rewritten cover
    carries a new stamp, so it is read again — the key is what makes that safe.
    """
    key = None
    try:
        st = os.stat(cover_path)
        key = (os.path.normcase(os.path.abspath(cover_path)),
               st.st_size, st.st_mtime_ns)
        cached = _COVER_DIMS_MEMO.get(key)
        if cached is not None:
            return cached
    except OSError:
        key = None
    dims = _read_cover_dimensions(cover_path)
    if key is not None:
        # A grade of a whole library keeps one entry per distinct cover: the
        # map is dropped rather than grown once it is bigger than any album.
        if len(_COVER_DIMS_MEMO) >= _COVER_DIMS_MEMO_MAX:
            _COVER_DIMS_MEMO.clear()
        _COVER_DIMS_MEMO[key] = dims
    return dims


def _read_cover_dimensions(cover_path):
    """Get cover dimensions, handling JXL via jxlinfo when Pillow lacks JXL support."""
    ext = os.path.splitext(cover_path)[1].lower()
    if ext == ".jxl":
        # Try Pillow first (may have JXL plugin)
        if HAS_PIL:
            try:
                with Image.open(cover_path) as im:
                    return im.size
            except Exception:
                pass
        # Fallback to jxlinfo (from libjxl)
        try:
            from .tools import detect_all_tools
            tools = detect_all_tools()
            jxl_info = tools.get("libjxl") or {}
            # Prefer explicit dir, fallback to cjxl_exe dirname
            jxl_dir = jxl_info.get("dir") or (os.path.dirname(jxl_info["cjxl_exe"]) if jxl_info.get("cjxl_exe") else None)
            candidates = []
            if jxl_dir:
                candidates.append(os.path.join(jxl_dir, "jxlinfo.exe"))
                # Some installs place it one level up or in bin subdir
                candidates.append(os.path.join(os.path.dirname(jxl_dir), "jxlinfo.exe"))
                candidates.append(os.path.join(jxl_dir, "bin", "jxlinfo.exe"))
            # Also try PATH
            import shutil
            which = shutil.which("jxlinfo") or shutil.which("jxlinfo.exe")
            if which:
                candidates.append(which)
            for jxlinfo in candidates:
                if jxlinfo and os.path.isfile(jxlinfo):
                    from .subproc import run_tool
                    proc = run_tool([jxlinfo, cover_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=5)
                    if proc.stdout:
                        m = re.search(r"(\d+)x(\d+)", proc.stdout)
                        if m:
                            return (int(m.group(1)), int(m.group(2)))
                    break
        except Exception:
            pass
        return (None, None)
    else:
        if not HAS_PIL:
            return (None, None)
        try:
            with Image.open(cover_path) as im:
                return im.size
        except Exception:
            return (None, None)

PER_TRACK_TAGS = [
    # Identity: the tags that make a file a usable library track at all.
    # Absence of any of these fails grade_check_missing_tags (the naming
    # script's path check only covers them indirectly, and not at all when
    # grade_check_naming is off or no music_folder is set).
    "TITLE",
    "ARTIST",
    "ALBUM",
    "ALBUMARTIST",
    "DATE",
    "TRACKNUMBER",
    "DISCNUMBER",     # only when the album really has several discs
    "GENRE",
    "MOOD",
    "ENERGY",
    "ITUNESADVISORY",
    "REPLAYGAIN_TRACK_GAIN",
    "REPLAYGAIN_TRACK_PEAK",
    "REPLAYGAIN_ALBUM_GAIN",
    "REPLAYGAIN_ALBUM_PEAK",
    "DYNAMIC RANGE",
    "INSTRUMENTAL",
]

# Tags whose PRESENCE is graded by a toggle of its own instead of the
# generic grade_check_missing_tags sweep, so one feature can be required
# without the whole sweep: mood/genre/energy are auto-filled by script 8
# (and the grader is what says a track may not ship without them),
# ReplayGain is the opt-in loudness family (see REPLAYGAIN_TAGS).
# Value: (config key, per-track issue code). Every entry defaults ON.
TAG_PRESENCE_CHECKS = {
    "GENRE": ("grade_check_genre", "GENRE_MISSING"),
    "MOOD": ("grade_check_mood", "MOOD_MISSING"),
    # ENERGY (0-100, written next to MOOD by script 8). Readable/writable on
    # every graded container including video, which the mood writer covers.
    "ENERGY": ("grade_check_energy", "ENERGY_MISSING"),
    "REPLAYGAIN_TRACK_GAIN": ("grade_check_replaygain", "REPLAYGAIN_TRACK_GAIN"),
    "REPLAYGAIN_TRACK_PEAK": ("grade_check_replaygain", "REPLAYGAIN_TRACK_PEAK"),
    "REPLAYGAIN_ALBUM_GAIN": ("grade_check_replaygain", "REPLAYGAIN_ALBUM_GAIN"),
    "REPLAYGAIN_ALBUM_PEAK": ("grade_check_replaygain", "REPLAYGAIN_ALBUM_PEAK"),
}

# The ReplayGain family is OPT-IN, like the AcoustID pair below: a file
# carrying at least one of the four tags must carry the complete set, while a
# file with none of them is never graded for them at all — the player can
# measure loudness on the fly, so a library that never ran script 7 must not
# be failed for the absence. grade_check_missing_tags never reaches these:
# grade_check_replaygain is the only path that grades them, so enabling both
# toggles cannot double-penalize a half-written set.
REPLAYGAIN_TAGS = (
    "REPLAYGAIN_TRACK_GAIN",
    "REPLAYGAIN_TRACK_PEAK",
    "REPLAYGAIN_ALBUM_GAIN",
    "REPLAYGAIN_ALBUM_PEAK",
)

# Checks that only ever apply to audio-only containers. No engine script
# writes ReplayGain / DR / AUDIT / Key&BPM into video files (the loudness,
# audit and audiometa scripts walk AUDIO_EXTS), so music videos are graded
# on what applies to them — tags, links, names — and never on these.
VIDEO_SKIP_TAGS = {
    "REPLAYGAIN_TRACK_GAIN", "REPLAYGAIN_TRACK_PEAK",
    "REPLAYGAIN_ALBUM_GAIN", "REPLAYGAIN_ALBUM_PEAK",
    "DYNAMIC RANGE",
}


ALBUM_TAGS = [
    "ALBUMITUNESADVISORY",
    "ALBUM DYNAMIC RANGE",
]


# Tags beets (mediafile) writes that the optimizer's TAG_MAP has no entry
# for: its own vorbis spellings of shared fields, MusicBrainz identifiers,
# and plugin extras. Beets-tagged files are first-class citizens, so none of
# these count as excess — only tags neither this repo's scripts nor beets
# would have written (vendor/ripper junk) are flagged by the excess check.
BEETS_TAGS = {
    # beets' vorbis spellings of shared fields
    "TRACK", "DISC", "CATALOGNUM", "COMPILATION", "ALBUMSORT",
    "ALBUMTYPE", "ALBUMSTATUS", "COMPOSERSORT", "COUNTRY", "SCRIPT",
    "LANGUAGE", "ASIN", "BARCODE", "YEAR", "DISCSUBTITLE", "PERFORMER",
    "MOVEMENTTOTAL",
    # MusicBrainz identifiers (beets / Picard)
    "MUSICBRAINZ_TRACKID", "MUSICBRAINZ_ALBUMID", "MUSICBRAINZ_ARTISTID",
    "MUSICBRAINZ_ALBUMARTISTID", "MUSICBRAINZ_RELEASEGROUPID",
    "MUSICBRAINZ_WORKID", "MUSICBRAINZ_RELEASETRACKID",
    "MUSICBRAINZ_ORIGINALALBUMID", "MUSICBRAINZ_ORIGINALARTISTID",
    "MUSICBRAINZ_TRMID",
    # Picard / beets release metadata + per-track credits — standard
    # MusicBrainz tagging output (releasestatus, totaltracks, the multi-
    # artist "artists" tag, producer/mixer/engineer roles, composer IDs…),
    # seen on essentially every properly tagged file
    "RELEASESTATUS", "TOTALTRACKS", "TOTALDISCS", "ARTISTS",
    "MUSICBRAINZ_COMPOSERID", "MUSICBRAINZ_CONDUCTORID",
    "MUSICBRAINZ_LYRICISTID", "MUSICBRAINZ_REMIXERID",
    "PRODUCER", "MIXER", "ENGINEER", "CONDUCTOR", "DJMIXER", "ARRANGER",
    "WRITER", "PUBLISHER", "MOOD",
    # beets plugins / third-party tooling it ships
    "ACOUSTID_ID", "ACOUSTID_FINGERPRINT",
    "REPLAYGAIN_REFERENCE_LOUDNESS",
    # Picard's own spellings of the same release metadata (TXXX:MusicBrainz
    # Album Type / Album Status / Disc Id / Album Release Country and their
    # MP4 freeform equivalents, plus the "use sort names" fields). Picard is
    # the documented interchange partner, so its standard output must never
    # count as an excess tag.
    "MUSICBRAINZ_ALBUMTYPE", "MUSICBRAINZ_ALBUMSTATUS",
    "MUSICBRAINZ_DISCID", "MUSICBRAINZ_ALBUMRELEASECOUNTRY",
    "MUSICBRAINZ_RELEASECOUNTRY", "MUSICBRAINZ_ALBUMARTISTSORT",
    "ARTISTSORT", "ALBUMARTISTSORT", "TITLESORT",
}

# ID3 frames beets or the encoder write that have no TAG_MAP entry: TCMP =
# compilation, TSOC = composer sort, TSSE/TENC = encoder identity (the MP3
# counterpart of the ENCODER_* vorbis tags above).
BEETS_ID3_FRAMES = {"TCMP", "TSOC", "TSSE", "TENC"}


def _tag_key_norm(key):
    """Uppercase key with spaces/underscores removed, so the file-side
    spellings of a name ("MusicBrainz Album Id", MUSICBRAINZ_ALBUM_ID,
    musicbrainz_albumid) all compare equal to the allowlist entry."""
    return re.sub(r"[\s_]+", "", str(key)).upper()


# Every tag name this app (TAG_MAP), its encoder markers, and beets/Picard
# (BEETS_TAGS) may write — normalized with _tag_key_norm. THE single source
# of truth: the excess-tag grade below and the Optimize/Format All strip pass
# (mlo.format_all, which imports tag_key_allowed) must agree, or a strip
# leaves a tag the grader flags, or deletes one it requires.
TAG_ALLOWLIST = frozenset(
    _tag_key_norm(k) for k in (
        *TAG_MAP, "ENCODER_PROGRAM", "ENCODER_QUALITY", "ENCODER_VERSION",
        *BEETS_TAGS))


def tag_key_allowed(key):
    """Whether *key* — in any file-side spelling the tag API emits (vorbis
    name, "TXXX:desc", "----:com.apple.iTunes:desc", a raw ID3 frame id such
    as "PRIV:owner", or a language-suffixed lyrics transform) — is part of
    the vocabulary this app and beets write. Anything else is junk a vendor
    or ripper left behind."""
    k = str(key)
    ku = k.upper()
    if _tag_key_norm(k) in TAG_ALLOWLIST:
        return True
    if ku.startswith(("TRANSLATION-", "TRANSLITERATION-")):
        return True
    if ku.startswith("TXXX:"):
        # ID3 freeform frames: allowed when the frame's description names a
        # tag the script or beets writes.
        return _tag_key_norm(k.split(":", 1)[1]) in TAG_ALLOWLIST
    if ku.startswith("----:"):
        # MP4 freeform atoms ("----:com.apple.iTunes:Name"): same rule on
        # the sub-name.
        return _tag_key_norm(k.rsplit(":", 1)[-1]) in TAG_ALLOWLIST
    return ku.split(":", 1)[0] in BEETS_ID3_FRAMES


def _tag_value(af, name):
    """Value of *name* through the tag API, falling back to a normalized
    scan of every tag (a file may store a spelling TAG_MAP does not name —
    e.g. an MP4 freeform atom written by another tagger — and the app's own
    tags must never be lost to a spelling difference alone)."""
    val = af.get_tag(name)
    if val not in (None, ""):
        return val
    want = _tag_key_norm(name)
    for k, v in (af.all_tags() or {}).items():
        if _tag_key_norm(k).endswith(want):
            return v
    return val


COVER_NAMES = {"cover.jpg", "cover.jpeg", "cover.png", "cover.jxl"}

# Enhanced LRC word-level timestamps: <mm:ss.xx> or <mm:ss.xxx>
# Mirrors WORD_TS_RE in lyrics.py
WORD_TS_RE = re.compile(r"<(\d{1,2}):(\d{1,2})(?:\.(\d+))?>")
TIMESTAMP_RE_GRADE = re.compile(r"\[(\d{1,2}):(\d{1,2})(?:\.(\d+))?\]")


def summarize_audits(values):
    """Collapse per-track AUDIT values into one album-level verdict.

    FAKE wins, a uniform REAL passes through, anything else is 'Mix'.
    None when empty. Case-insensitive (legacy mixed-case tags).
    """
    vals = {str(v).strip().upper() for v in values if v and str(v).strip()}
    if not vals:
        return None
    if "FAKE" in vals:
        return "FAKE"
    if vals == {"REAL"}:
        return "REAL"
    return "Mix"


def _grade_lyrics_present(embedded, lrc, lyrics_format):
    fmt = str(lyrics_format).upper()

    if fmt == "LRC":
        return lrc
    if fmt == "BOTH":
        return embedded and lrc

    return embedded


def _sidecar_ok(path):
    """Whether the sidecar file exists AND actually holds lyric text.

    An empty file left behind by an aborted run is not a stored transform —
    the same rule the lyrics-presence check above applies to a .lrc sidecar.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return has_lyrics_text(fh.read())
    except OSError:
        return False


def _xlit_stored(af, ap, kind):
    """Language detail of the stored *kind* transforms on this file.

    One entry per non-empty tag — the language suffix it carries, or "" for
    the bare legacy name — plus the accepted sidecars on disk, read exactly
    the way script 17 writes them (``TRANSLITERATION-JA-LATN`` + the
    ``.romaji.lrc`` sidecar, ``TRANSLATION-EN`` + ``<stem>.en.lrc``). Both
    storage places count, so a transform that lives in the sidecar alone is
    present; and the language the name carries is what makes a per-language
    check possible — a translation stored under the WRONG language is a
    mismatch the grader has to be able to name, not a silent pass.
    """
    found = set()
    for key, val in (af.all_tags() or {}).items():
        k = str(key).upper().rsplit(":", 1)[-1]
        if not str(val or "").strip():
            continue
        if k == kind:
            found.add("")
        elif k.startswith(kind + "-") and len(k) > len(kind) + 1:
            found.add(k[len(kind) + 1:].lower())
    base = os.path.splitext(ap)[0]
    if kind == "TRANSLITERATION":
        # One sidecar, one spelling, no language in its name.
        if _sidecar_ok(base + XLIT_SIDECAR):
            found.add("")
        return found
    # Translations: every <stem>.<lang>.lrc next to the file. The plain
    # <stem>.lrc is the lyrics sidecar, not a translation, and .romaji.lrc
    # belongs to the transliteration pass — neither names a target language.
    stem = os.path.basename(base).lower()
    try:
        entries = os.listdir(os.path.dirname(base) or ".")
    except OSError:
        entries = []
    for entry in entries:
        low = entry.lower()
        if not low.startswith(stem + ".") or not low.endswith(".lrc") \
                or low == stem + ".lrc" or low.endswith(XLIT_SIDECAR):
            continue
        if _sidecar_ok(os.path.join(os.path.dirname(base), entry)):
            found.add(low[len(stem) + 1:-len(".lrc")])
    return found


def _zero_target_allows_grader(cfg, is_for_lrc: bool) -> bool:
    """Whether zero-timestamp check should apply for LRC sidecar vs embedded tag."""
    try:
        target = str(cfg.get("lrc_zero_timestamp_target", "BOTH")).upper()
    except Exception:
        target = "BOTH"
    if target == "LRC":
        return is_for_lrc
    if target == "EMBEDDED":
        return not is_for_lrc
    return True


def _lyrics_formatted(text, cfg, is_for_lrc=False, check_spaces=True,
                      check_blank_lines=True):
    """True when the lyrics already match the configured formatting
    (timestamps, metadata stripping, blank collapse, no trailing blanks).

    Idempotency check against the raw text: running the Lyrics formatter
    must not change it (so a stray trailing newline, CRLF, or timestamp
    precision drift is caught too). When enhanced LRC is enabled, word-level
    <mm:ss.xx> timestamps are also validated for correct precision/formatting.
    Respects lrc_zero_timestamp_target and blank mode.

    *check_spaces* / *check_blank_lines* mirror grade_check_lyrics_spaces
    and grade_check_lyrics_blank_lines: a disabled toggle must not fail the
    album for that difference (the CUE check relaxes its comparison the same
    way). Both default to the strict canonical comparison.
    """
    if not text or not str(text).strip():
        return True
    raw = str(text)
    try:
        eff_zero = bool(cfg.get("lrc_add_zero_timestamp", False)) and _zero_target_allows_grader(cfg, is_for_lrc)
        expected = _canonical_lyrics(
            format_lyrics_text(
                raw,
                precision=int(cfg.get("lrc_timestamp_precision", 2) or 2),
                strip_metadata=cfg.get("lrc_strip_metadata", True),
                collapse_blank_lines=cfg.get("lrc_collapse_blank_lines", True),
                lrc_enhanced_enabled=bool(cfg.get("lrc_enhanced_enabled", True)),
                lrc_enhanced_word_sync=bool(cfg.get("lrc_enhanced_word_sync", True)),
                lrc_extended_enabled=bool(cfg.get("lrc_extended_enabled", True)),
                lrc_add_zero_timestamp=eff_zero,
                lrc_zero_timestamp_blank=bool(cfg.get("lrc_zero_timestamp_blank", False)),
            ),
            append_final_newline=cfg.get("append_final_newline", False),
        )
    except Exception:
        return True
    if raw != expected:
        if check_spaces and check_blank_lines:
            return False
        # A disabled toggle ignores its difference instead of failing the
        # album for it (same idea as _cue_formatted's space mode).
        def _relaxed(s):
            lines = str(s).split("\n")
            if not check_spaces:
                lines = [ln.strip() for ln in lines]
            if not check_blank_lines:
                lines = [ln for ln in lines if ln.strip()]
            return lines
        if _relaxed(raw) != _relaxed(expected):
            return False
    # Zero-timestamp compatibility: when enabled for this target, first lyric line must be [00:00.00]
    if bool(cfg.get("lrc_add_zero_timestamp", False)) and _zero_target_allows_grader(cfg, is_for_lrc):
        try:
            if not _lyrics_zero_timestamp_ok(raw, cfg, is_for_lrc=is_for_lrc):
                return False
        except Exception:
            pass
    # Enhanced LRC validity: word timestamps must be in order and correctly formatted
    # Only check when enhanced is enabled; otherwise they are plain text.
    if cfg.get("lrc_enhanced_enabled", True) and cfg.get("lrc_enhanced_word_sync", True):
        try:
            if not _lyrics_word_timestamps_valid(raw, cfg):
                return False
        except Exception:
            pass
    return True


# Two timestamps on the SAME line ("[00:00.00][00:45.53]text") break
# ESLyrics on foobar2000. Must not span a newline (that is the legitimate
# "[00:00.00]" empty marker line followed by the next line), so use a
# space/tab-only separator.
_MERGED_TS_RE = re.compile(
    r"\[\d{1,2}:\d{2}(?:\.\d+)?\][ \t]*\[\d{1,2}:\d{2}"
)


def _lyrics_merged_timestamps(text, cfg=None):
    """True when a line carries two adjacent line-level timestamps.
    Word-level <mm:ss.xx> timestamps are NOT considered merged (enhanced LRC).
    When extended LRC is disabled, merged timestamps are not flagged.
    """
    # Respect extended flag: when extended disabled, don't flag merged
    if cfg is not None and not cfg.get("lrc_extended_enabled", True):
        return False
    raw = str(text or "")
    # Strip word-level timestamps before checking so "<00:12.00>[00:13.00]" or
    # "[00:12.00] <00:12.34>" is not flagged. Also ensures extended handling
    # where word timestamps appear inline does not trigger false positive.
    # Replace with a placeholder word to break adjacency: "[00:12.00] <00:12.34> [00:13.00]"
    # should NOT be considered merged; removing to "" would leave "[00:12.00]  [00:13.00]" which still matches
    # _MERGED_TS_RE via space-only separator, so use " word " placeholder.
    try:
        cleaned = WORD_TS_RE.sub(" word ", raw)
    except Exception:
        cleaned = re.sub(r"<\d{1,2}:\d{1,2}(?:\.\d+)?>", " word ", raw)
    return bool(_MERGED_TS_RE.search(cleaned))


def _lyrics_word_timestamps_valid(text, cfg):
    """Check enhanced LRC word timestamps are in order and correctly formatted.

    When lrc_enhanced_enabled is False, always True (treated as plain text).
    When lrc_enhanced_word_sync is False, also skip.
    Checks:
    - Each <mm:ss.xx> must be zero-padded and match configured precision
    - Seconds <60
    - Word timestamps within a line are monotonically non-decreasing
    - First word timestamp on a line is >= preceding line timestamp (if any)
    """
    if not cfg.get("lrc_enhanced_enabled", True):
        return True
    if not cfg.get("lrc_enhanced_word_sync", True):
        return True
    if "<" not in str(text or ""):
        return True
    precision = 3 if int(cfg.get("lrc_timestamp_precision", 2) or 2) == 3 else 2
    correct_re = re.compile(r"<\d{2}:\d{2}\.\d{" + str(precision) + r"}>")
    for line in str(text or "").splitlines():
        matches = list(WORD_TS_RE.finditer(line))
        if not matches:
            continue
        # Formatting check: each raw word timestamp must be correctly zero-padded
        for m in matches:
            raw_ts = m.group(0)
            if not correct_re.fullmatch(raw_ts):
                return False
            try:
                secs = int(m.group(2))
                if secs >= 60:
                    return False
                mins = int(m.group(1))
                if mins < 0 or mins > 99:
                    # Allow 0-99, but still check overall validity; >99 will be caught by formatting
                    pass
            except (ValueError, TypeError):
                return False
        # Ordering check within the line
        times = []
        for m in matches:
            mins = int(m.group(1))
            secs = int(m.group(2))
            ms = m.group(3)
            try:
                ms_ms = int(ms[:3].ljust(3, "0")[:3]) if ms else 0
            except ValueError:
                ms_ms = 0
            total_ms = (mins * 60 + secs) * 1000 + ms_ms
            unit_ms = 10 ** (3 - precision)
            total_ms = ((total_ms + unit_ms // 2) // unit_ms) * unit_ms
            times.append(total_ms)
        for i in range(1, len(times)):
            if times[i] < times[i-1]:
                return False
        # Ensure first word timestamp >= preceding line timestamp on same line
        line_ts_matches = list(TIMESTAMP_RE_GRADE.finditer(line))
        if line_ts_matches and matches:
            first_word_pos = matches[0].start()
            relevant = [lm for lm in line_ts_matches if lm.start() < first_word_pos]
            if relevant:
                last = relevant[-1]
                lm = int(last.group(1))
                ls = int(last.group(2))
                lms = last.group(3)
                try:
                    lms_ms = int(lms[:3].ljust(3, "0")[:3]) if lms else 0
                except ValueError:
                    lms_ms = 0
                l_total = (lm * 60 + ls) * 1000 + lms_ms
                unit = 10 ** (3 - precision)
                l_total = ((l_total + unit // 2) // unit) * unit
                if times[0] < l_total:
                    return False
    return True


def _lyrics_zero_timestamp_ok(text, cfg, is_for_lrc=False):
    """True when the first lyric line matches the zero timestamp expectation.

    When lrc_add_zero_timestamp is False or target doesn't allow this type,
    always True. Otherwise the first lyric's handling depends on
    lrc_zero_timestamp_blank: when True it must be exactly bare [00:00.00],
    when False it must be tight [00:00.00]Text (zero timestamp on same line as first lyric).
    """
    if not cfg.get("lrc_add_zero_timestamp", False):
        return True
    if not _zero_target_allows_grader(cfg, is_for_lrc):
        return True
    if not text or not str(text).strip():
        return True
    try:
        precision = 3 if int(cfg.get("lrc_timestamp_precision", 2) or 2) == 3 else 2
    except Exception:
        precision = 2
    zero_ts = f"[00:00.{'0' * precision}]"
    # Find first non-blank, non-metadata line
    for ln in str(text).splitlines():
        s = ln.strip()
        if not s:
            continue
        low = s.lower()
        is_meta = (low.startswith("[ar:") or low.startswith("[ti:") or
                   low.startswith("[al:") or low.startswith("[by:") or
                   low.startswith("[au:") or low.startswith("[la:") or
                   low.startswith("[offset:") or low.startswith("[length:") or
                   low.startswith("[re:") or low.startswith("[ve:"))
        if is_meta and "<" not in s:
            continue
        # First lyric line found — check per blank setting
        if cfg.get("lrc_zero_timestamp_blank", False):
            return s == zero_ts
        else:
            # Tight: must start with zero_ts and have text after (not just bare)
            return s.startswith(zero_ts) and len(s) > len(zero_ts) and s[len(zero_ts):].strip() != ""
    return True  # no lyric lines found — nothing to enforce


def _cue_formatted(path, cfg):
    """True when a cue sheet is already in canonical form (LF, no BOM,
    quoted FILE lines with the configured type, no trailing whitespace,
    normalized DISCID/track/index)."""
    try:
        with open(path, "rb") as f:
            raw = f.read(4096)
    except OSError:
        return False
    if b"\x00" in raw:
        return True  # not really a cue; do not penalize
    if raw.startswith(b"\xef\xbb\xbf"):
        return False  # UTF-8 BOM would be stripped
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            content = f.read()
    except UnicodeDecodeError:
        try:
            with open(path, "r", encoding="latin-1", newline="") as f:
                content = f.read()
        except OSError:
            return False
    except OSError:
        return False
    canonical = canonical_cue_text(
        content,
        keep_empty_lines=cfg.get("keep_empty_cue_lines", False) or not cfg.get("grade_check_cue_blank_lines", True),
        keep_other_lines=cfg.get("keep_other_cue_lines", False),
        file_type=cfg.get("cue_file_type", "WAVE"),
        append_final_newline=cfg.get("append_final_newline", False),
    )
    if cfg.get("grade_check_cue_spaces", True):
        return canonical == content
    # Space check disabled: ignore leading/trailing whitespace per line
    # (blank-line collapsing and every other normalization still apply).
    return [ln.strip() for ln in canonical.split("\n")] == \
        [ln.strip() for ln in content.split("\n")]


# Non-audio files that the viewer can show alongside the tracks.
SIDECAR_EXTS = (".cue", ".log", ".lrc", ".accurip", ".jxl", ".jpg", ".jpeg", ".png")
SIDECAR_TYPES = {
    ".cue": "cue", ".log": "log", ".lrc": "lrc", ".accurip": "accurip",
    ".jxl": "image", ".jpg": "image", ".jpeg": "image", ".png": "image",
}

# Category -> config key deciding whether files of that kind are allowed.
# 'other' is opt-in (extra files fail grading by default). Videos — remuxed
# MKV and raw VOB/AVI/... — are their own allowed-by-default category; raw
# videos still fail the dedicated un-remuxed-video check. 'description' is
# the app's own description.txt: it may not be called a stray file.
CATEGORY_INCLUDE_KEYS = {
    "music": "grade_include_music",
    "cover": "grade_include_cover",
    "cue": "grade_include_cue",
    "log": "grade_include_log",
    "lrc": "grade_include_lrc",
    "accurip": "grade_include_accurip",
    "video": "grade_include_video",
    "description": "grade_include_description",
    "other": "grade_include_other",
}


_VIDEO_EXTS_CACHE = None


def _video_exts():
    """Every video container extension the remuxer (script 11) accepts.
    Lazy import keeps the grader import light and avoids import cycles."""
    global _VIDEO_EXTS_CACHE
    if _VIDEO_EXTS_CACHE is None:
        try:
            from .remux import VIDEO_EXTS

            _VIDEO_EXTS_CACHE = VIDEO_EXTS
        except Exception:
            _VIDEO_EXTS_CACHE = (".mkv",)
    return _VIDEO_EXTS_CACHE


def _video_category_exts():
    """Extensions classified as the 'video' file category: every library
    video container (LIB_VIDEO_EXTS — MP4/M4V included) plus the exotic
    containers only the remuxer accepts."""
    return LIB_VIDEO_EXTS + tuple(
        e for e in _video_exts() if e not in LIB_VIDEO_EXTS)


def _classify_file(f):
    """Category of a filename: music / cover / cue / log / lrc / accurip /
    video / description / other."""
    low = f.lower()
    if low.endswith(_video_category_exts()):
        # Video containers FIRST: LIB_AUDIO_EXTS is AUDIO_EXTS +
        # LIB_VIDEO_EXTS, so the music branch below would swallow every
        # music video and grade_include_video would never gate anything.
        # Remuxed (MKV) and raw (VOB/AVI/WMV/...) videos are their own
        # allowed-by-default category, so a raw VOB only fails the dedicated
        # un-remuxed-video check — never the generic disallowed-files check.
        return "video"
    if low.endswith(LIB_AUDIO_EXTS):
        return "music"
    if low.endswith(IMAGE_EXTS):
        return "cover"
    if low.endswith(".cue"):
        return "cue"
    if low.endswith(".log"):
        return "log"
    if low.endswith(".lrc"):
        return "lrc"
    if low.endswith(".accurip"):
        return "accurip"
    if low in ALBUM_SIDECAR_NAMES:
        # The album/artist description the app writes itself — a legitimate
        # library file, allowed by default like the cover art next to it.
        return "description"
    return "other"


def _is_video_file(f):
    """Video-container tracks (music videos) are library tracks but are
    never CD-DA: the CD-only checks (rip-log score, CRC coverage, 16/44.1
    format, AccurateRip requirement) must not apply to them. Uses the
    library's canonical video set (paths.LIB_VIDEO_EXTS) — the same one the
    per-track tag checks use — so AAC/M4A audio rips keep every CD check.
    """
    return os.path.splitext(f or "")[1].lower() in LIB_VIDEO_EXTS


def _category_allowed(cfg, category):
    """Whether files of a category are allowed under the current grading
    configuration (other = opt-in)."""
    key = CATEGORY_INCLUDE_KEYS[category]
    return bool(cfg.get(key, category != "other"))


# Windows hidden/system names that must never fail a folder.
HIDDEN_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}


def _skip_grading_file(full):
    """True when a folder entry should be ignored by grading: subdirectories
    and hidden/system/OS files (desktop.ini, Thumbs.db, dotfiles)."""
    if os.path.isdir(full):
        return True
    name = os.path.basename(full)
    if name.lower() in HIDDEN_NAMES or name.startswith("."):
        return True
    try:
        attr = os.stat(full).st_file_attributes
        if attr is not None and (attr & 0x2 or attr & 0x4):  # HIDDEN | SYSTEM
            return True
    except (OSError, AttributeError):
        pass
    return False


def _disallowed_files(album_dir, all_files, cfg):
    """Files in an album folder whose category is not allowed (subdirs and
    hidden/system files are ignored)."""
    out = []
    for f in sorted(all_files):
        full = os.path.join(album_dir, f)
        if _skip_grading_file(full):
            continue
        if not _category_allowed(cfg, _classify_file(f)):
            out.append(f)
    return out


def _extra_images(album_dir, all_files, audio_files):
    """Image files that belong to no track and no album slot: neither the
    album cover (cover.*) nor a per-track sidecar whose stem matches a track
    ("01 - Song.jpg", extended stems like "01 - Song.front.jpg" count too —
    the same convention the organizer's sidecar pass follows), nor an image
    listed in the per-track cover manifest (a shared image is named after one
    track only, so its stem says nothing about the other tracks that use it),
    nor the artist's own artist.jpg / artist.png (stored at artist level by
    mlo.artistdata — never stray album art)."""
    from .artistdata import ARTIST_IMAGE_STEMS
    track_stems = {os.path.splitext(f)[0].lower() for f in audio_files}
    mapped = {v.lower() for v in load_track_covers(album_dir).values()}
    out = []
    for f in sorted(all_files):
        low = f.lower()
        if not low.endswith(IMAGE_EXTS) or low in COVER_NAMES:
            continue
        if os.path.splitext(low)[0] in ARTIST_IMAGE_STEMS:
            continue
        full = os.path.join(album_dir, f)
        if _skip_grading_file(full):
            continue
        if low in mapped:
            continue
        stem = os.path.splitext(f)[0].lower()
        if stem in track_stems or any(
            stem.startswith(s + ".") for s in track_stems
        ):
            continue
        out.append(f)
    return out


def _log_file_ok(path):
    """A .log passes when it is non-empty text (a usable rip log)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(8192)
    except OSError:
        return False
    return bool(content and content.strip())


def _get_cover_target_size(ext, config):
    """Per-format cover target size for grader (mirrors images helper)."""
    if config is None:
        config = {}
    ext = (ext or "").lower()
    try:
        global_size = int(config.get("cover_target_size", 0) or 0)
    except (TypeError, ValueError):
        global_size = 0
    global_size = max(0, min(4000, global_size))
    per_size = 0
    try:
        if ext in (".jpg", ".jpeg"):
            per_size = int(config.get("cover_jpeg_target_size", 0) or 0)
        elif ext == ".png":
            per_size = int(config.get("cover_png_target_size", 0) or 0)
        elif ext == ".jxl":
            per_size = int(config.get("cover_jxl_target_size", 0) or 0)
    except (TypeError, ValueError):
        per_size = 0
    if per_size > 0:
        return max(0, min(4000, per_size))
    return global_size


def _cover_image_ok(path, config):
    """Validate a cover image against size + ASPECT-RATIO checks.

    Checks:
    * file exists and >0 bytes (always)
    * if cover_enforce_size and cover_resize_enabled and target_size>0,
      dimensions must be exactly target_size x target_size (1px tolerance)
    * if cover_enforce_square and grade_check_cover_crop, the ASPECT RATIO
      must be within threshold (abs(width/height -1) <= threshold) — this is
      a squareness test, not crop detection (see the album-cover check)

    Handles per-format target sizes. When Pillow is unavailable or the
    file can't be opened, falls back to existence/size check to avoid
    false failures. Keeps existing behavior when enforcement disabled.
    """
    # Basic existence/size check always required
    try:
        if not os.path.exists(path):
            return False
        if os.path.getsize(path) <= 0:
            return False
    except OSError:
        return False
    if config is None:
        config = {}
    ext = os.path.splitext(path)[1].lower()
    target = _get_cover_target_size(ext, config)
    # If no enforcement enabled, just existence/size suffices (preserve
    # backwards compatibility) — but force_exact implies both when resize is on
    enforce_size = bool(config.get("cover_enforce_size", False))
    enforce_square = bool(config.get("cover_enforce_square", False))
    resize_enabled = bool(config.get("cover_resize_enabled", False))
    force_exact = bool(config.get("cover_force_exact_size", False))
    if force_exact and resize_enabled and target > 0:
        # Force exact implies both size and square must be enforced
        enforce_size = True
        enforce_square = True
    if not force_exact:
        # grade_check_cover_crop is the toggle for the ASPECT-RATIO part of
        # the check (see the album-cover check below) — a sidecar cover must
        # not be graded for squareness when that check is switched off.
        # force_exact_size is an explicit "exactly target×target" request and
        # keeps implying it.
        enforce_square = enforce_square and bool(
            config.get("grade_check_cover_crop", True))
    if not enforce_size and not enforce_square:
        return True
    # Need Pillow to inspect dimensions; without it we cannot verify but should not silently pass stringent checks.
    # Emit a warning and treat as passed to avoid false failures, but log once per run.
    if not HAS_PIL:
        try:
            # Log only once per process to avoid spam
            if not hasattr(_cover_image_ok, "_warned_no_pil"):
                from .ui import log, c, Color
                log(c("WARNING: Pillow not installed — cover size/square checks skipped (install Pillow for strict grading).", Color.YELLOW))
                _cover_image_ok._warned_no_pil = True
        except Exception:
            pass
        return True
    try:
        with Image.open(path) as img:
            # Header read only: this check judges the pixel dimensions, and
            # Pillow's lazy open answers size from the header — load()
            # decoded every cover in the library in full to measure it, and
            # the failure it could raise was swallowed right here anyway.
            w, h = img.size
            if w <= 0 or h <= 0:
                return False
            # Size enforcement — a cover LARGER than the target fails: the
            # image pass downscales it. An undersized one is accepted, because
            # that same pass never upscales (resampling up invents pixels), so
            # failing it would be a permanent FAIL nothing can clear.
            if enforce_size and resize_enabled and target > 0:
                try:
                    tol = int(config.get("grader_cover_size_tolerance_px", 0) or 0)
                    tol = max(0, min(5, tol))
                except Exception:
                    tol = 0
                if w - target > tol or h - target > tol:
                    return False
            # Square enforcement (force_exact => strict)
            if enforce_square:
                if force_exact:
                    try:
                        thr = float(config.get("grader_strict_square_threshold", 0.0) or 0.0)
                    except (TypeError, ValueError):
                        thr = 0.0
                    thr = max(0.0, min(0.05, thr))
                else:
                    try:
                        thr = float(config.get("cover_crop_threshold", 0.0) or 0.0)
                    except (TypeError, ValueError):
                        thr = 0.0
                    thr = max(0.0, min(0.5, thr))
                # When square enforcement is on, aspect must be within threshold
                # The spec mentions cover_enforce_square and (cover_crop_enabled or cover_enforce_square)
                # So we check regardless of crop_enabled if enforce_square true
                ratio = w / h if h != 0 else 1.0
                deviation = abs(ratio - 1.0)
                if deviation > thr:
                    return False
            return True
    except Exception:
        # Pillow couldn't open (e.g. JXL without plugin) — try jxlinfo
        # dimensions before giving up; only pass when truly unreadable.
        try:
            jw, jh = _get_cover_dimensions(path)
            if jw and jh and enforce_size and resize_enabled and target > 0:
                try:
                    tol = int(config.get("grader_cover_size_tolerance_px", 0) or 0)
                    tol = max(0, min(5, tol))
                except Exception:
                    tol = 0
                if jw - target > tol or jh - target > tol:
                    return False
            if jw and jh and enforce_square:
                ratio = jw / jh if jh else 1.0
                try:
                    thr = float(config.get("cover_crop_threshold", 0.0) or 0.0)
                except (TypeError, ValueError):
                    thr = 0.0
                if abs(ratio - 1.0) > max(0.0, min(0.5, thr)):
                    return False
            if jw and jh:
                return True
        except Exception:
            pass
        # Enforcement is on and no reader could state the dimensions: that is
        # an unreadable/corrupt cover, not a pass (the album-cover path has
        # always failed it — the two must agree).
        return not (enforce_size or enforce_square)


def _grade_sidecars(album_dir, all_files, cfg):
    """Per-file grades for non-audio files shown in the library viewer.

    Returns a list of dicts: {file, type, ok, detail}. These are
    informational rows; they do not change the album's pass/fail (the
    album-level checks below cover compliance).
    """
    sidecars = []
    for f in sorted(all_files):
        full = os.path.join(album_dir, f)
        if _skip_grading_file(full):
            continue
        category = _classify_file(f)
        if category == "music":
            continue
        if category == "cue":
            ok = _cue_formatted(full, cfg)
            detail = "formatted" if ok else "needs formatting"
        elif category == "lrc":
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    lrc_text = fh.read()
                ok = _lyrics_formatted(lrc_text, cfg, is_for_lrc=True) and \
                    not _lyrics_merged_timestamps(lrc_text, cfg)
                # Enhanced LRC validity: word timestamps must be in order / correctly formatted
                if ok and cfg.get("lrc_enhanced_enabled", True) and cfg.get("lrc_enhanced_word_sync", True):
                    if not _lyrics_word_timestamps_valid(lrc_text, cfg):
                        ok = False
            except OSError:
                ok = False
            detail = "formatted" if ok else "needs formatting"
        elif category == "log":
            ok = _log_file_ok(full)
            detail = "present" if ok else "empty"
        elif category == "accurip":
            # .accurip must be a valid CUETools log *and* correctly trimmed per user spec:
            # each line stripped of leading/trailing spaces/tabs, only outer blank lines removed.
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    acc_text = fh.read()
                if not acc_text or not acc_text.strip():
                    ok = False
                elif "[CUETools log;" not in acc_text:
                    ok = False
                else:
                    try:
                        from mlo.accurip import _canonical_accurip_text, parse_accurip_status as _parse_ar_side
                        # Check canonical formatting (trim each line, trim outer blanks only)
                        # No extra blank line at bottom — like .cue's rstrip(), final LF only if append_final_newline
                        # Respects keep_empty_accurip_lines (like keep_empty_cue_lines)
                        append = bool(cfg.get("append_final_newline", False))
                        keep_empty = bool(cfg.get("keep_empty_accurip_lines", False))
                        canonical = _canonical_accurip_text(acc_text, keep_empty_lines=keep_empty, append_final_newline=append)
                        if acc_text != canonical:
                            ok = False
                        else:
                            # A canonical CUETools log is a GOOD file whatever
                            # its verdict, so NONE counts: a pressing that is
                            # not in the AccurateRip database ends its log
                            # "disk not present in database" with only the
                            # Track Peak table, and the generator writes
                            # exactly that. Failing it here marked a file the
                            # user could never fix as "needs formatting"; the
                            # verdict itself is the AccurateRip leg's business
                            # (`_cd_legs` / mlo.audit name a DB miss by name).
                            st, _ = _parse_ar_side(acc_text)
                            ok = st in ("REAL", "FAKE", "NONE")
                    except Exception:
                        ok = "[CUETools log;" in acc_text
            except OSError:
                ok = False
            detail = "formatted" if ok else "needs formatting"
        elif category == "cover":
            ok = _cover_image_ok(full, cfg)
            if ok:
                detail = "present"
            else:
                # Differentiate empty vs dimension mismatch for UI clarity
                try:
                    exists = os.path.exists(full) and os.path.getsize(full) > 0
                except OSError:
                    exists = False
                if not exists:
                    detail = "empty"
                else:
                    # Check which enforcement failed for more helpful detail
                    ext = os.path.splitext(full)[1].lower()
                    tgt = _get_cover_target_size(ext, cfg)
                    enforce_size = bool(cfg.get("cover_enforce_size", False)) and bool(cfg.get("cover_resize_enabled", False)) and tgt > 0
                    enforce_square = (bool(cfg.get("cover_enforce_square", False))
                                      and bool(cfg.get("grade_check_cover_crop", True)))
                    # Try to inspect image for specific reason
                    try:
                        if HAS_PIL:
                            with Image.open(full) as _im:
                                _w, _h = _im.size
                                if enforce_size and (abs(_w - tgt) > 1 or abs(_h - tgt) > 1):
                                    detail = f"wrong size {_w}x{_h} (need {tgt}x{tgt})"
                                elif enforce_square:
                                    thr = float(cfg.get("cover_crop_threshold", 0.0) or 0.0)
                                    thr = max(0.0, min(0.5, thr))
                                    ratio = _w / _h if _h else 1.0
                                    if abs(ratio - 1.0) > thr:
                                        detail = f"aspect ratio {_w}x{_h} not square"
                                    else:
                                        detail = "needs resize"
                                else:
                                    detail = "needs resize"
                        else:
                            detail = "needs resize"
                    except Exception:
                        detail = "needs resize"
        else:
            ok = _category_allowed(cfg, category)
            detail = "allowed" if ok else "disallowed type"
        sidecars.append({
            "file": f, "type": category,
            "ok": ok, "detail": detail,
        })
    return sidecars


def _norm_path_case(p):
    """Separator + case normalization so a case-only difference is recognised
    on BOTH platforms.

    `os.path.normcase` reads like the portable answer and is not one: it
    lowercases on Windows and is the identity on POSIX, so on Linux a path
    differing only in letter case fell through to the "path" verdict — a full
    naming failure — instead of "case", which meant the PATH_CASE check and
    its switch did not exist for a Docker or Linux install at all. Folding
    the case here makes the comparison mean the same thing everywhere."""
    return str(p or "").replace("/", os.sep).replace("\\", os.sep).casefold()


def _fs_cased_dir(path, base):
    """Re-spell *path* with the letter case the filesystem actually stores.

    Only needed because the PATH_CASE grade compares a path against the
    naming script, and on a case-insensitive filesystem (Windows) that
    comparison is only as good as the casing of the string the caller handed
    us: grading `.../Artists/Artist/2020 - Album` for a folder the disk
    stores as `2020 - ALBUM` returned an exact match, so the wrong case the
    user has to fix (organize rewrites it) vanished into the caller's own
    spelling — a path served from the library payload, healed from a stale
    cache, or typed into an API call. File names never had that hole (they
    come from os.listdir); directory segments are walked here for the same
    reason: the listing is the only source of the stored case.

    Segments below *base* are matched case-insensitively against the listing
    one level at a time (one listdir per level, once per album, and only when
    the naming check is enabled). A segment with no counterpart — an 8.3
    short name (`DILLYD~1`), a junction/symlink target, an unreadable parent
    — keeps the caller's spelling rather than resolving to a path that may
    not address the same folder, and so does a path that is not below *base*.
    """
    if not base:
        return path
    try:
        rel = os.path.relpath(path, base)
    except ValueError:  # different drives (Windows): no shared base
        return path
    if os.path.isabs(rel) or rel.startswith(os.pardir):
        return path
    cur = base
    for seg in rel.split(os.sep):
        try:
            names = os.listdir(cur)
        except OSError:
            return path
        match = next((n for n in names
                      if os.path.normcase(n) == os.path.normcase(seg)), None)
        if match is None:
            return path
        cur = os.path.join(cur, match)
    return cur


def _mb_release_type(mbid):
    """Release type MusicBrainz already knows for *mbid* — never a new call.

    The organizer resolves a missing RELEASETYPE live (server.main organize)
    and writes the value into the folder; grading must reproduce that path
    WITHOUT depending on the network. So only the ALREADY-WARM in-process
    MusicBrainz cache (server.integrations._BROWSE_CACHE, filled by the
    app's own MB traffic) is consulted: a warm hit returns the same
    lowercase "+"-joined spelling release_lookup produces, a miss returns
    None and the caller grades the missing RELEASETYPE tag instead of
    silently re-fetching. Lazy import keeps the plain CLI importable.
    """
    try:
        from server.integrations import _BROWSE_CACHE, _BROWSE_LOCK
    except Exception:
        return None
    endpoint = f"release/{mbid}"
    data = None
    try:
        with _BROWSE_LOCK:
            for (ep, _params), hit in _BROWSE_CACHE.items():
                if ep == endpoint and isinstance(hit, tuple) and hit[1]:
                    data = hit[1]
                    break
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    rg = data.get("release-group") or {}
    primary = str(rg.get("primary-type") or "").strip().lower()
    if not primary:
        return None
    secondary = [str(s).strip().lower() for s in (rg.get("secondary-types") or [])]
    return "+".join([primary] + [s for s in secondary if s]) or None


def _release_type_candidates(value):
    """Spellings the naming script may have been evaluated with.

    The organizer passes the RELEASETYPE tag VERBATIM and only falls back to
    release_lookup's lowercase "+" form when the tag is absent, so the raw
    value comes first and decides. The other spellings of the SAME type keep
    a folder laid out by a differently-spelled tagger (beets' capped
    "Album; Live" vs the wizard's "album+live") from failing the path check.
    An UNKNOWN type also tries the no-type layout, so an album organized
    before the tag existed still matches.
    """
    if value == UNKNOWN_RELEASE_TYPE:
        return [UNKNOWN_RELEASE_TYPE, ""]
    out = []
    for v in (value, mb_style_release_type(value),
              lookup_style_release_type(value)):
        if v and v not in out:
            out.append(v)
    return out or [""]


def _naming_mismatch(ap, folder, script, release_type, tags):
    """Expected-relative-path comparison for grade_check_naming.

    *folder* is the MUSIC folder; the paths are compared against the library
    root inside it (<music>/Artists) — the same base the organizer's
    organize() uses — so an album sitting in the music folder root is a
    mismatch, not a false pass.

    The naming script defines the target path WITHOUT the file extension
    (beets-style — the extension is preserved from the source file), so the
    extension is appended before comparing. Returns (kind, expected) where
    kind is "ok" (exact match), "case" (matches except for LETTER CASE —
    graded by grade_check_filename_case) or "path" (no match at all —
    graded by grade_check_naming). Both full and 8-char-truncated
    MusicBrainz IDs are accepted so the short_folder_names setting can't
    produce false failures.

    Every equivalent spelling of the release type is tried (see
    _release_type_candidates) and the BEST verdict wins: an exact match
    beats a case-only match beats a mismatch, so a differently-spelled tag
    can never turn a passing path into a failure. When *release_type* is
    UNKNOWN_RELEASE_TYPE the type token is matched as a wildcard — the tag
    is gone and MusicBrainz was never consulted, so the folder is checked on
    everything the tags DO say; the missing tag is reported separately, so
    this stays a visible defect rather than a silent pass.
    """
    from mlo.naming import eval_script, track_variables

    base = library_root(folder) if folder else folder
    actual = os.path.relpath(ap, base)
    ext = os.path.splitext(ap)[1]
    actual_slash = str(actual).replace(os.sep, "/").replace("\\", "/")

    def _seps(p):
        # separator-normalized but CASE-SENSITIVE (exact compare)
        return str(p).replace("/", os.sep).replace("\\", os.sep)

    def _match(value):
        variables = track_variables(tags or {}, release_type=value)
        full = eval_script(script, variables, shorter_ids=False) + ext
        short = eval_script(script, variables, shorter_ids=True) + ext
        shown = (full or short).replace(UNKNOWN_RELEASE_TYPE, "?")
        if value == UNKNOWN_RELEASE_TYPE:
            patterns = [_wildcard_re(e) for e in (full, short) if e]
            if any(re.fullmatch(p, actual_slash) for p in patterns):
                return ("ok", None)
            if any(re.fullmatch(p, actual_slash, re.IGNORECASE) for p in patterns):
                return ("case", shown)
            return ("path", shown)
        for e in (full, short):
            if e and _seps(e) == _seps(actual):
                return ("ok", None)
        for e in (full, short):
            if e and _norm_path_case(e) == _norm_path_case(actual):
                return ("case", full or short)
        return ("path", full or short)

    best_kind, best_expected = "path", ""
    for value in _release_type_candidates(release_type):
        kind, expected = _match(value)
        if kind == "ok":
            return (kind, None)
        if not best_expected:
            best_expected = expected
        if kind == "case" and best_kind != "case":
            best_kind, best_expected = kind, expected
    return (best_kind, best_expected)


def _wildcard_re(expected):
    """Regex for an expected path carrying UNKNOWN_RELEASE_TYPE: the sentinel
    stands for whatever token the folder currently spells there (empty
    included, so an album organized before the tag existed still matches)."""
    return "[^/\\\\]*".join(re.escape(p) for p in str(expected).split(UNKNOWN_RELEASE_TYPE))


def _multi_disc_album(album_dir, all_files):
    """Evidence in the FOLDER that the album really holds more than one disc.

    DISCNUMBER is a multi-disc tag: beets writes it only for multi-disc
    releases and the naming script defaults the variable to 1, so requiring
    the tag on every album would fail every single-disc release. Evidence is
    the D-TT (disc-track) filename convention or a per-disc split of the
    audio — both are album-wide properties, so one track settles it. The tag
    half of the evidence (DISCTOTAL / TOTALDISCS) is read from the track the
    caller has ALREADY parsed (see _multi_disc_from_tags): probing for it here
    opened the album's first file a second time, once per album, for a value
    that loop reads anyway.
    """
    try:
        from .discs import album_discs, disc_of_filename

        if len(album_discs(album_dir) or {}) > 1:
            return True
        if any((disc_of_filename(f) or 1) > 1 for f in all_files):
            return True
    except Exception:
        pass
    return False


def _multi_disc_from_tags(af):
    """Whether an already-parsed track states a disc total above 1."""
    for k in ("DISCTOTAL", "TOTALDISCS"):
        v = str(_tag_value(af, k) or "").strip()
        if v.isdigit() and int(v) > 1:
            return True
    return False


def _audio_format_info(af):
    """(bit depth, sample rate) of an already-parsed AudioFile, or
    (None, None) when the format doesn't report them. mutagen names bit
    depth inconsistently — some formats expose bits_per_sample, others bits.
    """
    info = getattr(getattr(af, "audio", None), "info", None)
    if info is None:
        return (None, None)
    bits = getattr(info, "bits_per_sample", None)
    if bits is None:
        bits = getattr(info, "bits", None)
    return (bits, getattr(info, "sample_rate", None))


def _ambiguous_lrc_stems(filenames):
    """Stems shared by more than one graded file in one album folder.

    `_lrc_for` drops only the LAST extension, so "01 Song.flac", "01 Song.mp3"
    and "01 Song.mp4" in the same folder all resolve to the same
    "01 Song.lrc" — one real sidecar would be credited to every one of them.
    A sidecar that belongs to several tracks belongs to none, so these stems
    get no lrc credit at all (their embedded lyrics still count).
    `normcase` compares stems the way this OS's filesystem does.
    """
    counts = {}
    for name in filenames:
        stem = os.path.normcase(os.path.splitext(name)[0])
        counts[stem] = counts.get(stem, 0) + 1
    return {stem for stem, n in counts.items() if n > 1}


def _grade_album(album_dir, lyrics_format, cfg=None):
    if cfg is None:
        cfg = {}
    try:
        all_files = os.listdir(album_dir)
    except PermissionError as e:
        log(f"Permission denied reading album: {album_dir} ({e})", Color.YELLOW)
        return {"error": True, "path": album_dir, "error_detail": f"Permission denied: {e}"}
    except OSError as e:
        log(f"Cannot list album directory: {album_dir} ({e})", Color.YELLOW)
        return {"error": True, "path": album_dir, "error_detail": str(e)}
    files = sorted(f for f in all_files if is_audio_file(f))
    audio_paths = [os.path.join(album_dir, f) for f in files]

    if not audio_paths:
        return None

    total_checks = 0
    failed_checks = 0

    tracks = []
    issues = {}
    # Informational lines for this album: they say what was NOT checked,
    # never that something failed. The artist grade has carried the same
    # channel since the image checks (_artist_image_issues).
    notes = []

    album_tag_values = {}
    media_values = []
    # MEDIA values of the album's VIDEO tracks only (see the media summary):
    # read when no audio track states one, never pooled with them.
    video_media_values = []
    source_values = []
    album_artist = None
    # Whether any track states the album's MusicBrainz release id — the
    # manifest check below only applies where script 15 could have written one.
    album_has_mbid = False

    # Path-grading setup (grade_check_naming): the naming script is evaluated
    # per track exactly like the organizer does. RELEASETYPE comes from tags;
    # when the tag is missing it is resolved from the MusicBrainz release the
    # same way organize does (see _mb_release_type), so an organized folder
    # matches its own naming script.
    music_folder = str(cfg.get("music_folder") or "").strip()
    naming_script = (str(cfg.get("naming_script") or "").strip() or DEFAULT_NAMING_SCRIPT)
    try:
        naming_check = (
            cfg.get("grade_check_naming", True)
            and bool(music_folder)
            and os.path.commonpath([os.path.normpath(album_dir), os.path.normpath(music_folder)])
            == os.path.normpath(music_folder)
        )
    except ValueError:
        naming_check = False
    album_release_type = None

    # Grade the case the DISK stores, not the case this caller spelled: on a
    # case-insensitive filesystem a canonically-spelled path for a folder
    # stored as "2020 - ALBUM" used to pass the PATH_CASE check (see
    # _fs_cased_dir). Only the directory segments need it — the file name
    # already comes from os.listdir.
    naming_dir = _fs_cased_dir(album_dir, library_root(music_folder)) if naming_check else album_dir

    lyrics_present_count = 0
    lyrics_expected_count = 0
    instrumental_count = 0
    # Bit depth / sample rate per audio path, captured from the mutagen info
    # parsed in the loop below — the CD format pass reads it from here
    # instead of re-opening every file.
    format_by_path = {}

    def add_issue(field, where="album"):
        issues.setdefault(field, set()).add(where)

    def unavailable(check, exc, where="album"):
        """Record a check that could not run — count it AND fail it.

        A bare `except Exception: pass` silently REMOVED a check from the
        grade (and, when the counter sat inside the try, from the
        denominator too), so an album could grade PASS without the check
        ever applying. An exception now costs the check like any other
        failure and names itself in the issue list.
        """
        nonlocal total_checks, failed_checks
        total_checks += 1
        failed_checks += 1
        add_issue(f"{check} could not be evaluated: {exc}", where)

    cover_file = None
    for f in all_files:
        if f.lower() in COVER_NAMES:
            cover_file = f
            break

    has_log = any(f.lower().endswith(".log") for f in all_files)
    has_cue = any(f.lower().endswith(".cue") for f in all_files)
    # A .log only counts as a rip log when it actually holds text — an empty
    # file left by an aborted run must not satisfy grade_check_cd_log.
    usable_logs = [f for f in all_files if f.lower().endswith(".log")
                   and _log_file_ok(os.path.join(album_dir, f))]
    # DISCNUMBER is only required of multi-disc albums (see _multi_disc_album),
    # and only the missing-tag sweep grades it — skip the probe otherwise. The
    # folder evidence is free; the tag evidence is settled inside the track
    # loop below, on the file that loop has already parsed.
    multi_disc = (_multi_disc_album(album_dir, all_files)
                  if cfg.get("grade_check_missing_tags", True) else False)

    # One .lrc cannot be credited to several same-stem tracks (see
    # _ambiguous_lrc_stems) — computed once for the whole folder.
    ambiguous_lrc_stems = _ambiguous_lrc_stems(files)
    # CD tracks whose AUDIT requirement is decided after the .log/.accurip
    # verification runs (see the deferred resolution further down).
    deferred_audit = {}

    for ap in audio_paths:
        af = AudioFile(ap)
        basename = os.path.basename(ap)
        if not multi_disc:
            # The album-wide DISCNUMBER requirement (see _multi_disc_album):
            # any track stating a disc total settles it, and this file is
            # already open — the probe must not re-open one per album.
            multi_disc = _multi_disc_from_tags(af)

        track = {
            "file": basename,
            "issues": [],
            "values": {},
            "lyrics_embedded": False,
            "lyrics_lrc": False,
            "unreadable": False,
            "audit": None,
            "log_grade": None,
        }

        if af.audio is None:
            track["unreadable"] = True
            if cfg.get("grade_check_unreadable", True):
                add_issue("Unreadable audio file", basename)
                track["issues"].append("UNREADABLE")
                total_checks += 1
                failed_checks += 1
            # Same gates as the graded loop below: an unreadable track fails
            # exactly the checks that would have been graded on a readable
            # one, so disabling a check also stops counting it here.
            for t in PER_TRACK_TAGS:
                if not should_write_audio_tag(cfg, t, filepath=ap):
                    continue
                if t in REPLAYGAIN_TAGS:
                    # Opt-in family: an unreadable file carries no RG tag, so
                    # the family is not graded at all (see REPLAYGAIN_TAGS).
                    continue
                gate, _code = TAG_PRESENCE_CHECKS.get(
                    t, ("grade_check_missing_tags", t))
                if not cfg.get(gate, True):
                    continue
                if t == "DISCNUMBER" and not multi_disc:
                    continue
                total_checks += 1
                failed_checks += 1
                add_issue(f"Missing {t} (unreadable file)", basename)

            tracks.append(track)
            continue

        # Required per-track tags (skip if per-filetype disabled).
        is_video_track = os.path.splitext(ap)[1].lower() in LIB_VIDEO_EXTS
        format_by_path[ap] = _audio_format_info(af)
        # ReplayGain is opt-in per file: with none of the four tags present
        # there is nothing to grade (and nothing is counted), so an
        # untouched library is not penalized for the family at all.
        grade_replaygain = (
            bool(cfg.get("grade_check_replaygain", True))
            and any(str(af.get_tag(t) or "").strip() for t in REPLAYGAIN_TAGS)
        )
        for t in PER_TRACK_TAGS:
            if is_video_track and t in VIDEO_SKIP_TAGS:
                # Video containers: ReplayGain / DR never apply (no engine
                # script writes them there) — record, don't grade.
                track["values"][t] = af.get_tag(t)
                continue
            if t in REPLAYGAIN_TAGS and not grade_replaygain:
                # Not supposed to be graded for its filetype this run —
                # record the value, don't grade or count it.
                track["values"][t] = af.get_tag(t)
                continue
            if not should_write_audio_tag(cfg, t, filepath=ap):
                # Track not supposed to have this tag for its filetype — don't grade it
                track["values"][t] = af.get_tag(t)
                continue
            # GENRE / MOOD / ENERGY / ReplayGain answer to their own toggle
            # (see TAG_PRESENCE_CHECKS), everything else to missing_tags. A
            # switched-off presence check is neither graded nor COUNTED: it
            # used to stay in the denominator (and in pass_count, since the
            # value was recorded regardless), so turning a check off still
            # moved grade_pct and the check count the UI shows.
            presence_gate, presence_code = TAG_PRESENCE_CHECKS.get(
                t, ("grade_check_missing_tags", t))
            presence_graded = cfg.get(presence_gate, True)
            if presence_graded:
                total_checks += 1
            val = af.get_tag(t)
            track["values"][t] = val

            if val is None or str(val).strip() == "":
                # ITUNESADVISORY is optional: absence means "unrated", not a
                # defect — nothing auto-fills 0 anymore, so a missing tag
                # must not fail the check (a PRESENT value is still validated
                # as 0/1/2 below).
                if t == "DISCNUMBER" and not multi_disc:
                    # Single-disc releases legitimately carry no DISCNUMBER.
                    pass
                elif t != "ITUNESADVISORY" and presence_graded:
                    failed_checks += 1
                    add_issue(f"Missing {t}", basename)
                    track["issues"].append(presence_code)
            elif t == "ITUNESADVISORY":
                raw = str(val)
                stripped = raw.strip()
                if raw != stripped or stripped not in ("0", "1", "2"):
                    if cfg.get("grade_check_missing_tags", True):
                        failed_checks += 1
                        add_issue(f"ITUNESADVISORY must be 0/1/2 without spaces (found {raw!r})", basename)
                        track["issues"].append(t)
            elif t == "GENRE":
                raw = str(val)
                if cfg.get("grade_check_tag_spaces", True):
                    # GENRE arrives here as the whole "; "-joined list, so its
                    # leading/trailing answer is the whole-string trim, and an
                    # internal run of spaces is the same near-miss every other
                    # tag is graded on (the rule lives in mlo.tagtext).
                    why = spacing_problem(t, raw)
                    if not why and raw != raw.strip():
                        why = "has leading/trailing spaces"
                    if why:
                        failed_checks += 1
                        add_issue(f"GENRE {why} ({raw!r})", basename)
                        track["issues"].append(t)

            # Configurable: check tags for blank lines (spaces already handled for GENRE above)
            # Only check per-line trailing/leading spaces for non-GENRE/ITUNESADVISORY when enabled
            if cfg.get("grade_check_tag_spaces", True) and t not in ("GENRE", "ITUNESADVISORY"):
                raw_all = str(val) if val is not None else ""
                # Leading/trailing is judged per LINE — a value carrying
                # newlines still has untrimmed lines — while a run of 2+ inner
                # spaces is a per-VALUE near-miss that script 10 collapses.
                # Both halves come from mlo.tagtext.spacing_problem, so grading
                # can never fail a value a writer would have stored as it is.
                why = spacing_problem(t, raw_all)
                if not why and raw_all and any(ln != ln.strip(" \t") for ln in raw_all.splitlines()):
                    why = "has leading/trailing spaces"
                if why:
                    failed_checks += 1
                    add_issue(f"{t} {why} ({raw_all!r})", basename)
                    track["issues"].append(t)
            if cfg.get("grade_check_tag_blank_lines", True):
                # LYRICS is exempt here: the lyrics formatter intentionally
                # keeps single middle blank lines (section separators) and
                # their blank/spacing handling is owned by the dedicated
                # grade_check_lyrics_* checks below, which mirror it.
                if t not in ("LYRICS", "UNSYNCEDLYRICS", "SYNCLYRICS"):
                    raw_blank = str(val) if val is not None else ""
                    # Any blank/whitespace-only line anywhere (including leading/trailing outer blanks)
                    # mirrors Format All's removal of ALL blank lines, not just middle
                    if "\n" in raw_blank and any(not line.strip() for line in raw_blank.splitlines()):
                        failed_checks += 1
                        add_issue(f"{t} has blank lines", basename)
                        track["issues"].append(t)

        # Genre COUNT (grade_check_genre_count) — a different question from
        # the presence check above: a track may hold AT MOST the configured
        # number of GENRE values, the same cap the import and the trimming
        # scripts apply (mb_genre_count, Settings → Import). Only an OVERFLOW
        # fails: one specific genre is a complete answer, so there is no
        # exact-count quota and nothing is ever topped up to fill a slot (the
        # slot would be filler, and the family is derived by the writers
        # anyway — see mlo.genres). A filetype the genre write gate excludes
        # is never failed for a genre this app was told never to write, and
        # with the toggle off nothing is counted — same rule as every other
        # check.
        if cfg.get("grade_check_genre_count", True) \
                and should_write_audio_tag(cfg, "GENRE", filepath=ap):
            total_checks += 1
            # The fallback is DEFAULT_CONFIG's value, never a literal: the
            # shipped default must not drift from the one place it lives. A
            # partial or hand-edited config must not break a whole grade, so an
            # unparsable value falls back to the same default instead of
            # raising.
            try:
                want = int(cfg.get("mb_genre_count") or DEFAULT_CONFIG["mb_genre_count"])
            except (TypeError, ValueError):
                want = int(DEFAULT_CONFIG["mb_genre_count"])
            # tag_values reads the repeated fields back piece by piece; a file
            # another tagger wrote as ONE "Rock; Pop" value names two genres,
            # and the trimming scripts read it the same way — counting it as
            # one would fail a track they would leave alone.
            values = [v for v in (str(x).strip() for x in af.tag_values("GENRE")) if v]
            if len(values) == 1 and ";" in values[0]:
                values = [p.strip() for p in values[0].split(";") if p.strip()]
            have = len(values)
            if have > want:
                failed_checks += 1
                add_issue(f"Genre count: {have} genres, at most {want} allowed",
                          basename)
                track["issues"].append("GENRE_COUNT")

        # Genre ORDER (grade_check_genre_order) — the hierarchy half of the
        # same contract: the FAMILY, if present, comes FIRST and the specific
        # genres follow it ("rock / shoegaze / dream pop"). The rules
        # themselves live in mlo.genres, which the import and scripts 8/10
        # also apply, so the grader cannot fail a list those would leave alone.
        #
        # Only the order and duplicate lines are reported here. The count and
        # the vocabulary belong to their own checks — a disabled check must not
        # reappear under another code — and the spaces line is already the
        # GENRE presence check's.
        if cfg.get("grade_check_genre_order", True) \
                and should_write_audio_tag(cfg, "GENRE", filepath=ap):
            total_checks += 1
            try:
                want = int(cfg.get("mb_genre_count") or DEFAULT_CONFIG["mb_genre_count"])
            except (TypeError, ValueError):
                want = int(DEFAULT_CONFIG["mb_genre_count"])
            values = [v for v in (str(x).strip() for x in af.tag_values("GENRE")) if v]
            if len(values) == 1 and ";" in values[0]:
                values = [p.strip() for p in values[0].split(";") if p.strip()]
            order_issues = [
                msg for msg in genre_issues(values, want)
                # Absence is the PRESENCE check's code (GENRE_MISSING), and a
                # switched-off presence check must not reappear here.
                if msg != "missing"
                and not msg.startswith("too many")
                and not msg.startswith("not a known genre")
                and not msg.startswith("Genre has ")
            ]
            if order_issues:
                failed_checks += 1
                for msg in order_issues:
                    add_issue(msg, basename)
                track["issues"].append("GENRE_ORDER")

        # Genre VOCABULARY (grade_check_genre_vocab) — every stored name must
        # be a MusicBrainz genre (mlo.genres.canonical). The app's writers only
        # ever store names MusicBrainz publishes, so an unknown one is a
        # source's own spelling that nothing canonicalized; the report names
        # each of them rather than accepting it silently. The tag is never
        # rewritten from here — the writers own that (mlo.genres.normalize_genres).
        if cfg.get("grade_check_genre_vocab", True) \
                and should_write_audio_tag(cfg, "GENRE", filepath=ap):
            total_checks += 1
            # Read through the SAME splitter the order check uses
            # (mlo.genres.split_stored): a file whose single value is the
            # rendered "Shoegaze / Rock" holds two names, and testing it as
            # one would fail this check while the order check passed the same
            # file — two checks, two readings of one list.
            values = iter_names(af.tag_values("GENRE"))
            unknown = [n for n in values if genre_canonical(n) is None]
            if unknown:
                failed_checks += 1
                for name in unknown:
                    add_issue(f"Not a MusicBrainz genre: {name}", basename)
                track["issues"].append("GENRE_VOCAB")

        # Key & BPM (script 12 output) — required when the check is on.
        # Excess tags: anything NEITHER this pipeline's scripts NOR beets
        # would have written — the optimizer's strip pass would remove every
        # key outside the shared vocabulary. Their presence counts against
        # grading so unoptimized files surface, and the report names EVERY one
        # of them (the tag names are what the user acts on) plus the script
        # that removes it. Gated on `strip_unknown_tags` as well: with the
        # strip pass switched off nothing in the pipeline can clear an excess
        # tag, so demanding it would be a permanent FAIL.
        if cfg.get("grade_check_excess_tags", True) \
                and cfg.get("strip_unknown_tags", True) and not is_video_track:
            # Script vocabulary: TAG_MAP keys, the encoder identity tags,
            # beets/mediafile's own spellings and Picard's — the SAME
            # predicate the strip passes apply (mlo.containers for Optimize
            # FLACs, mlo.format_all for Format all), so a strip can never
            # leave a tag that is then flagged, or delete one the grader
            # requires (every graded name is in TAG_MAP — see
            # tools/test_grading_paths.py). Language-specific lyrics
            # transforms (TRANSLATION-EN, TRANSLITERATION-JA-LATN, …) and the
            # app's own AUDIOAUDITOR_OVERRIDE are first-class, not excess.
            _extra = sorted({str(_k) for _k in af.all_tags().keys()
                             if not tag_key_allowed(_k)})
            if _extra:
                total_checks += 1
                failed_checks += 1
                add_issue("Excess tags: " + ", ".join(_extra)
                          + " (run Optimize FLACs (script 3) or Format all "
                            "(script 10) to strip them)", basename)
                track["issues"].append("TAGS")

        if cfg.get("grade_check_key_bpm", True) and not is_video_track:
            for t in ("INITIALKEY", "BPM"):
                if not should_write_audio_tag(cfg, t, filepath=ap):
                    continue
                total_checks += 1
                val = af.get_tag(t)
                track["values"][t] = val
                if val is None or str(val).strip() == "":
                    failed_checks += 1
                    add_issue(f"Missing {t}", basename)
                    track["issues"].append(t)
                    continue
                # INITIALKEY must match the configured notation's exact
                # format. Musical notation is standardized on FLAT spellings
                # ("B♭ min") — a sharp spelling ("A# min") is pre-standard
                # output and fails so stale tags surface.
                if t == "INITIALKEY":
                    notation = str(cfg.get("audiometa_key_notation", "musical")).lower()
                    v = str(val).strip()
                    if notation == "camelot":
                        ok = re.fullmatch(r"\d{1,2}[AB]", v) is not None
                    elif notation == "openkey":
                        ok = re.fullmatch(r"\d{1,2}[dm]", v) is not None
                    else:
                        ok = re.fullmatch(r"[A-G]♭? (maj|min)", v) is not None
                    if not ok:
                        failed_checks += 1
                        if notation == "musical":
                            add_issue(f"INITIALKEY '{v}' not standard flat notation "
                                      f"(expected e.g. 'B♭ min')", basename)
                        else:
                            add_issue(f"INITIALKEY '{v}' not {notation} notation", basename)
                        track["issues"].append(t)

        # AcoustID fingerprint pair (opt-in feature): a file carrying one
        # half must carry both — mlo.acoustid writes ID and fingerprint
        # together. A file with neither is never graded, so a library that
        # does not use the feature can never fail here.
        if cfg.get("grade_check_acoustid", True) and not is_video_track:
            _a_id = _a_fp = ""
            for _k, _v in (af.all_tags() or {}).items():
                # TXXX:ACOUSTID_ID / ----:com.apple.iTunes:acoustid_id both
                # reduce to the semantic name under _tag_key_norm.
                _name = _tag_key_norm(str(_k).rsplit(":", 1)[-1])
                if _name == "ACOUSTIDID":
                    _a_id = str(_v or "").strip()
                elif _name == "ACOUSTIDFINGERPRINT":
                    _a_fp = str(_v or "").strip()
            if _a_id or _a_fp:
                total_checks += 1
                if not (_a_id and _a_fp):
                    _missing = ("ACOUSTID_FINGERPRINT" if _a_id
                                else "ACOUSTID_ID")
                    failed_checks += 1
                    # The half pair is the one thing here no other check can
                    # fix, and the pass that completes it is a step of the
                    # chain — named the way the neighbours name their actions
                    # ("run Audit Library", "run organize"). The tag that is
                    # missing is still named, which is what a reader searches
                    # the tags for.
                    add_issue(f"Missing {_missing} (run Fix AcoustID pairs)",
                              basename)
                    track["issues"].append(_missing)

        # File/folder names must match the naming script (the same script the
        # organizer applies), relative to the music folder — exact match for
        # PATH, and letter-case separately for PATH_CASE.
        if naming_check:
            try:
                tags_map = af.all_tags() or {}
            except Exception:
                tags_map = {}
            if album_release_type is None:
                # Tags first; MusicBrainz only from an already-warm client.
                # Resolved once per album — the value is sticky (the release
                # type is an album property, not a per-track one).
                album_release_type = str(tags_map.get("RELEASETYPE") or "").strip()
                if not album_release_type and tags_map.get("MUSICBRAINZ_ALBUMID"):
                    album_release_type = _mb_release_type(
                        tags_map["MUSICBRAINZ_ALBUMID"]) or ""
                if not album_release_type and "releasetype" in naming_script.lower():
                    # Genuinely absent (no tag, nothing warm): grade the
                    # missing TAG, not a path that depends on the network.
                    # The type token is matched as a wildcard so the rest of
                    # the layout is still verified.
                    album_release_type = UNKNOWN_RELEASE_TYPE
                    total_checks += 1
                    failed_checks += 1
                    add_issue("Missing RELEASETYPE tag (the naming script "
                              "writes it into the path)", basename)
            kind, expected = _naming_mismatch(
                os.path.join(naming_dir, basename),
                music_folder, naming_script, album_release_type, tags_map)
            if kind == "path" and cfg.get("grade_check_naming", True):
                total_checks += 1
                failed_checks += 1
                # The same pointer the case-only twin below carries: the
                # expected path above IS the current naming script's own
                # evaluation of this file's tags (see _naming_mismatch), so
                # the way to satisfy it is the album's own Organize action —
                # never a hand rename, which would drift again the next time
                # MusicBrainz sharpens a date.
                add_issue(f"PATH: expected '{expected}' (run organize)", basename)
                track["issues"].append("PATH")
            elif kind == "case" and cfg.get("grade_check_filename_case", True):
                total_checks += 1
                failed_checks += 1
                add_issue(f"PATH CASE: expected '{expected}' (run organize)", basename)
                track["issues"].append("PATH_CASE")

        # Additional check for *all* tags in the file (including TITLE, ALBUM, etc.) for leading/trailing spaces and blank lines
        if cfg.get("grade_check_tag_spaces", True) or cfg.get("grade_check_tag_blank_lines", True):
            try:
                all_tags = af.all_tags()
                for tag_key, tag_val in all_tags.items():
                    # Skip already checked per-track tags to avoid double counting
                    if tag_key in PER_TRACK_TAGS or tag_key in ALBUM_TAGS:
                        continue
                    # Skip some internal tags that are not user-visible
                    if tag_key in ("ENCODER_PROGRAM", "ENCODER_QUALITY", "ENCODER_VERSION", "AUDIT", "LOG_GRADE"):
                        continue
                    # Lyrics tags are graded by the dedicated grade_check_lyrics_*
                    # checks below, which mirror the lyrics formatter (it keeps
                    # single middle blank lines by design) — the generic tag
                    # hygiene rule must not fail them.
                    if tag_key.upper() in ("LYRICS", "UNSYNCEDLYRICS", "SYNCLYRICS"):
                        continue
                    raw = str(tag_val) if tag_val is not None else ""
                    # Per-line spaces check (not whole-string strip which flags trailing \n)
                    if cfg.get("grade_check_tag_spaces", True):
                        total_checks += 1
                        why = spacing_problem(tag_key, raw)
                        if not why and raw and any(ln != ln.strip(" \t") for ln in raw.splitlines()):
                            why = "has leading/trailing spaces"
                        if why:
                            failed_checks += 1
                            add_issue(f"{tag_key} {why} ({raw!r})", basename)
                            track["issues"].append(tag_key)
                    if cfg.get("grade_check_tag_blank_lines", True):
                        total_checks += 1
                        if "\n" in raw and any(not line.strip() for line in raw.splitlines()):
                            failed_checks += 1
                            add_issue(f"{tag_key} has blank lines", basename)
                            track["issues"].append(tag_key)
            except Exception as e:
                unavailable("Tag hygiene sweep (spaces/blank lines)", e, basename)

        # Tag VALUE CASE (grade_check_tag_case) — a tag whose value has a
        # canonical spelling (mlo.tagtext.CANONICAL_CASE: MEDIA, SOURCE,
        # RELEASETYPE, RELEASESTATUS, AUDIT, RELEASECOUNTRY, SCRIPT, MOOD) must
        # be stored that way. ONE check per track: how many tags a track got
        # wrong does not change what the track costs, exactly like the path
        # case check. The comparison is the write rule itself
        # (mlo.tagtext.canonical_value), so grading can only ever fail a value
        # those writers would have rewritten — a vocabulary's unknown value
        # comes back unchanged and passes.
        #
        # GENRE is the one tag that rule cannot answer for: it is an OPEN
        # multi-value tag, and its canonical form is per NAME (a writer
        # capitalizes each name on its own, mlo.genres.display_name) rather
        # than per value, so it is deliberately absent from CANONICAL_CASE and
        # is asked of the genre writers' own function here. Without it a
        # library could carry "metal; alternative metal" — a real defect no
        # other check reports, because the vocabulary and order rules fold
        # case — while every writer would have stored "Metal; Alternative
        # Metal".
        #
        # Free text is never looked at: TITLE, ALBUM, ARTIST, ALBUMARTIST,
        # LABEL, COMMENT and the lyrics have no canonical form, which is what
        # keeps "AC/DC" and "k.d. lang" out of this check. The tags a filetype
        # is not supposed to carry are skipped with every other write gate.
        if cfg.get("grade_check_tag_case", True):
            try:
                total_checks += 1
                wrong = []
                for tag_key, tag_val in (af.all_tags() or {}).items():
                    if tag_val is None:
                        continue
                    raw_val = str(tag_val)
                    fixed = str(canonical_value(tag_key, raw_val))
                    if fixed == raw_val:
                        # No canonical form (free text), or already canonical.
                        continue
                    if not should_write_audio_tag(cfg, tag_key, filepath=ap):
                        continue
                    wrong.append(f"{tag_key} {raw_val!r} → {fixed!r}")
                # Read through the app's own reader for the tag (iter_names =
                # mlo.genres.split_stored), so a list another tagger joined
                # into one value is judged name by name like a repeated field.
                # An unrecognised name is compared with the spelling the
                # writer would still give it: display_name, not the verbatim
                # name the vocabulary check already reports.
                genre_wrong = []
                if should_write_audio_tag(cfg, "GENRE", filepath=ap):
                    for name in iter_names(af.tag_values("GENRE")):
                        expected = genre_display_name(
                            genre_canonical(name) or name)
                        if name != expected:
                            genre_wrong.append(f"GENRE {name!r} → {expected!r}")
                if wrong or genre_wrong:
                    failed_checks += 1
                    if wrong:
                        add_issue("Tag case: " + ", ".join(wrong)
                                  + " (run Format all (script 10))", basename)
                    if genre_wrong:
                        add_issue("Genre case: " + ", ".join(genre_wrong)
                                  + " (run Format all (script 10))", basename)
                    track["issues"].append("TAG_CASE")
                    if genre_wrong:
                        track["issues"].append("GENRE_CASE")
            except Exception as e:
                unavailable("Tag case check", e, basename)

        # ENCODER marker tags — per-format, only when that field is enabled.
        # For FLAC (the only audio type the app re-encodes), check PROGRAM/QUALITY/VERSION.
        # PROGRAM is off by default since v1.4.2, but when turned on per format grading must require it.
        if cfg.get("grade_check_encoder", True):
            try:
                # FLAC is the only audio container the app re-encodes, and
                # this loop only ever sees audio_paths — the image branches
                # that used to live here were unreachable (cover encoder
                # markers are graded separately, below).
                if os.path.splitext(ap)[1].lower() == ".flac":
                    enc_cfg = (cfg.get("encoder_tags") or {}).get("flac", {}) if cfg else {}
                    for field in ("ENCODER_PROGRAM", "ENCODER_QUALITY", "ENCODER_VERSION"):
                        # Default: PROGRAM off, QUALITY/VERSION on
                        default_on = False if field == "ENCODER_PROGRAM" else True
                        if not enc_cfg.get(field, default_on):
                            continue
                        # Read the tag via the underlying mutagen object (PROGRAM not in TAG_MAP for FLAC)
                        val = None
                        try:
                            if af.audio is not None and hasattr(af.audio, "get"):
                                # FLAC Vorbis via mutagen.flac.FLAC — keys are lower-case in storage
                                # Do case-insensitive lookup
                                raw = None
                                # Try direct lower and upper
                                for k in (field, field.lower(), field.upper()):
                                    if k in af.audio:
                                        try:
                                            raw = af.audio.get(k, [None])[0]
                                        except Exception:
                                            raw = None
                                        if raw is not None:
                                            break
                                # Fallback via get_tag for other containers
                                if raw is None:
                                    raw = af.get_tag(field)
                                val = str(raw).strip() if raw is not None else None
                            else:
                                val = af.get_tag(field)
                        except Exception:
                            val = None
                        total_checks += 1
                        if not val:
                            failed_checks += 1
                            add_issue(f"Missing {field} (re-optimize)", basename)
                            track["issues"].append(field)
            except Exception as e:
                unavailable("Encoder identity check", e, basename)

        # Artist for the library view (first track that has one). Keys are
        # matched case-insensitively: Picard writes lowercase Vorbis
        # comments while other taggers use uppercase.
        if album_artist is None:
            try:
                raw = {str(k).lower(): v for k, v in af.all_tags().items()}
                for k in ("albumartist", "tpe2", "aart",
                          "artist", "tpe1", "\xa9art"):
                    v = str(raw.get(k) or "").strip()
                    if v:
                        album_artist = v
                        break
            except Exception:
                album_artist = None

        # Album-wide tag values (only for enabled filetypes). Music videos are
        # left out of the pool: no engine script writes these into a video
        # container (ALBUM DYNAMIC RANGE is in VIDEO_SKIP_TAGS for exactly that
        # reason) and should_write_audio_tag cannot say so — an MKV has no
        # audio-tag filetype, so every per-type gate answers True for it. A
        # pooled bonus video used to fail the album with "missing on some
        # tracks", and a video-only album then held no album-tag value at all:
        # the pool stays empty and the album-tags check skips the folder
        # instead of failing it for a tag nothing writes there.
        for t in ALBUM_TAGS:
            if is_video_track or not should_write_audio_tag(cfg, t, filepath=ap):
                continue
            v = af.get_tag(t)
            album_tag_values.setdefault(t, set()).add(
                str(v).strip() if v is not None else ""
            )

        # MEDIA / SOURCE values.
        media_val = af.get_tag("MEDIA")
        source_val = af.get_tag("SOURCE")

        media_clean = str(media_val).strip() if media_val is not None else ""
        source_clean = str(source_val).strip() if source_val is not None else ""

        track["values"]["MEDIA"] = media_clean or None
        track["values"]["SOURCE"] = source_clean or None

        # AudioAuditor verdict persisted by the Audit Library script:
        # required on every track of every media type, REAL to pass (skipped if AUDIT disabled for this filetype).
        audit_val = af.get_tag("AUDIT")
        audit_clean = str(audit_val).strip() if audit_val is not None else ""
        track["audit"] = audit_clean or None
        if (
            should_write_audio_tag(cfg, "AUDIT", filepath=ap)
            # Default OFF, matching mlo.config.DEFAULT_CONFIG: a partial cfg
            # (tests, library-grade helpers) must not auto-fail an
            # unaudited library.
            and cfg.get("grade_check_audit", False)
            and not is_video_track
        ):
            if _is_cd(media_clean):
                # A CD rip's integrity is decided by its own verification
                # (.log CRC / .accurip), which is only read AFTER this loop —
                # so this verdict is DEFERRED. AudioAuditor's spectrogram
                # verdict must never be the last word on a disc whose rip is
                # provably intact.
                deferred_audit[ap] = (basename, audit_clean)
            else:
                total_checks += 1
                if not audit_clean:
                    failed_checks += 1
                    add_issue("Missing AUDIT tag (run Audit Library)", basename)
                    track["issues"].append("AUDIT")
                elif audit_clean.upper() != "REAL":
                    failed_checks += 1
                    add_issue(f"AUDIT tag is {audit_clean.upper()} (not REAL)",
                              basename)
                    track["issues"].append("AUDIT")

        # MusicBrainz / RateYourMusic identity links — required for a PASS.
        # Exactly two links are graded: the MusicBrainz RELEASE (falling
        # back to its release group) and the RateYourMusic release-group
        # page. Artist / recording / per-track RYM links stay optional —
        # they power extra buttons but are not graded.
        mb_release = str(
            af.get_tag("MUSICBRAINZ_ALBUMID") or af.get_tag("MUSICBRAINZ_RELEASEGROUPID") or ""
        ).strip()
        if mb_release:
            album_has_mbid = True
        rym_release = str(af.get_tag("RATEYOURMUSIC_ALBUM") or "").strip()
        if should_write_audio_tag(cfg, "MUSICBRAINZ_ALBUMID", filepath=ap) and cfg.get(
            "grade_check_mb_links", True
        ):
            total_checks += 1
            if not mb_release:
                failed_checks += 1
                add_issue("Missing MusicBrainz release link (import from MusicBrainz)", basename)
                track["issues"].append("MB_LINK")
        if should_write_audio_tag(cfg, "RATEYOURMUSIC_ALBUM", filepath=ap) and cfg.get(
            "grade_check_rym_links", True
        ):
            total_checks += 1
            if not rym_release:
                failed_checks += 1
                add_issue("Missing RateYourMusic release link", basename)
                track["issues"].append("RYM_LINK")

        # Rip-log score (MEDIA=CD releases only, checked once MEDIA is
        # known - read here, graded in the CD section below).
        lg_val = af.get_tag("LOG_GRADE")
        track["log_grade"] = (
            str(lg_val).strip() if lg_val is not None
            and str(lg_val).strip() else None)

        # The album's MEDIA/SOURCE summary describes the RELEASE medium —
        # bonus music videos on a disc don't change it, so they stay out of
        # the consistency pool (a CD + music-video disc is not INCONSISTENT).
        # A video-only album (music videos, no audio track) has no audio value
        # to describe it with, and the video writer stamps the same MEDIA tag:
        # these values are held aside for the album check below, which reads
        # them only when no audio track contributed one.
        if media_clean:
            if is_video_track:
                video_media_values.append(media_clean)
            else:
                media_values.append(media_clean)

        if source_clean and not is_video_track:
            source_values.append(source_clean)

        # Lyrics status. A sidecar only counts when it actually holds text:
        # an empty .lrc left behind by an aborted run used to make a
        # lyric-less track report (and grade as) having lyrics, and content
        # that is only metadata or bare timestamps is no better. A sidecar
        # shared with a same-stem sibling is credited to nobody.
        lyr = af.get_lyrics()
        embedded = bool(lyr and str(lyr).strip())
        lrc = False
        if os.path.normcase(os.path.splitext(basename)[0]) not in ambiguous_lrc_stems:
            try:
                with open(_lrc_for(ap), "r", encoding="utf-8", errors="replace") as _f:
                    lrc = has_lyrics_text(_f.read())
            except OSError:
                lrc = False

        track["lyrics_embedded"] = embedded
        track["lyrics_lrc"] = lrc

        inst = af.get_tag("INSTRUMENTAL")
        inst_val = str(inst).strip() if inst is not None else None
        track["values"]["INSTRUMENTAL"] = inst_val

        # Manual AudioAuditor override. Read here (where the file's tags are
        # already open) so it rides the track payload; it is APPLIED much
        # later, after every derived REAL/FAKE verdict, so it wins over them.
        # Read through the tag API with a normalized fallback: the tag is the
        # app's own (TAG_MAP + the allowlist below), and a file that stores
        # it under another spelling must not silently lose its override.
        _ov = str(_tag_value(af, "AUDIOAUDITOR_OVERRIDE") or "").strip().upper()
        track["values"]["AUDIOAUDITOR_OVERRIDE"] = _ov if _ov in ("REAL", "FAKE") else None

        if inst_val == "1":
            instrumental_count += 1
            if should_write_audio_tag(cfg, "INSTRUMENTAL", filepath=ap) and cfg.get("grade_check_instrumental", True):
                total_checks += 1
                if embedded or lrc:
                    failed_checks += 1
                    add_issue("INSTRUMENTAL=1 but lyrics present", basename)
                    track["issues"].append("LYRICS")

        elif inst_val == "0":
            if should_write_audio_tag(cfg, "INSTRUMENTAL", filepath=ap):
                # Track expectation for stats regardless
                _has_lyr = _grade_lyrics_present(embedded, lrc, lyrics_format)
                if _has_lyr:
                    lyrics_present_count += 1
                lyrics_expected_count += 1
                if cfg.get("grade_check_lyrics", True):
                    total_checks += 1
                    if not _has_lyr:
                        failed_checks += 1
                        add_issue(f"Missing lyrics ({lyrics_format.upper()})", basename)
                        track["issues"].append("LYRICS")

        # Lyrics FORMATTING compliance (only when lyrics are present):
        # the stored text must already be in the canonical form the Lyrics
        # script would produce, and never carry merged timestamps.
        # Skip if LYRICS disabled for this filetype.
        # Configurable via grade_check_lyrics_* and grade_check_lyrics_zero/crop
        if embedded or lrc:
            if not should_write_audio_tag(cfg, "LYRICS", filepath=ap):
                pass
            elif not cfg.get("grade_check_lyrics_format", True):
                pass
            elif not cfg.get("grade_check_lyrics_spaces", True) and not cfg.get("grade_check_lyrics_blank_lines", True) and not cfg.get("grade_check_lyrics_zero", True):
                # All lyrics checks disabled
                pass
            else:
                total_checks += 1
                lyr_text = str(lyr) if embedded else None
                lrc_text = None
                if lrc:
                    try:
                        with open(_lrc_for(ap), "r", encoding="utf-8",
                                  errors="replace") as _f:
                            lrc_text = _f.read()
                    except OSError:
                        lrc_text = None
                fmt_ok = True
                # Check formatting (trailing/leading spaces, blank lines) if enabled
                if cfg.get("grade_check_lyrics_spaces", True) or cfg.get("grade_check_lyrics_blank_lines", True):
                    # Each toggle gates its own comparison: turning off just
                    # one must not still fail the album for that difference.
                    _ly_spaces = cfg.get("grade_check_lyrics_spaces", True)
                    _ly_blanks = cfg.get("grade_check_lyrics_blank_lines", True)
                    if lyr_text and not _lyrics_formatted(
                            lyr_text, cfg, is_for_lrc=False,
                            check_spaces=_ly_spaces, check_blank_lines=_ly_blanks):
                        fmt_ok = False
                    if lrc_text and not _lyrics_formatted(
                            lrc_text, cfg, is_for_lrc=True,
                            check_spaces=_ly_spaces, check_blank_lines=_ly_blanks):
                        fmt_ok = False
                # Zero timestamp check if enabled
                if cfg.get("grade_check_lyrics_zero", True):
                    if lyr_text and not _lyrics_zero_timestamp_ok(lyr_text, cfg, is_for_lrc=False):
                        fmt_ok = False
                    if lrc_text and not _lyrics_zero_timestamp_ok(lrc_text, cfg, is_for_lrc=True):
                        fmt_ok = False
                if (lyr_text and _lyrics_merged_timestamps(lyr_text, cfg)) or \
                   (lrc_text and _lyrics_merged_timestamps(lrc_text, cfg)):
                    fmt_ok = False
                # Enhanced LRC word timestamp validity (order / formatting)
                if cfg.get("lrc_enhanced_enabled", True) and cfg.get("lrc_enhanced_word_sync", True):
                    if lyr_text and not _lyrics_word_timestamps_valid(lyr_text, cfg):
                        fmt_ok = False
                    if lrc_text and not _lyrics_word_timestamps_valid(lrc_text, cfg):
                        fmt_ok = False
                    # Sync-level REQUIREMENT: synced lyrics must carry at
                    # least the configured granularity (LINE default —
                    # plain line tags; WORD — per-word tags; SYLLABLE — glued per-syllable tags).
                    _level = cfg.get("lrc_sync_level", "LINE")
                    if lyr_text and TIMESTAMP_RE_GRADE.search(lyr_text) \
                            and not text_meets_sync_level(lyr_text, _level):
                        fmt_ok = False
                    if lrc_text and TIMESTAMP_RE_GRADE.search(lrc_text) \
                            and not text_meets_sync_level(lrc_text, _level):
                        fmt_ok = False
                # Unsynced lyrics fail — plain text with no [mm:ss.xx] is not
                # synced — UNLESS the user opted into plain lyrics
                # (lyrics_allow_plain). The provider chain is then allowed to
                # store an untimed answer, and no script can invent the
                # missing timestamps: grading what the pipeline can produce.
                if not cfg.get("lyrics_allow_plain", False):
                    if lyr_text and not TIMESTAMP_RE_GRADE.search(lyr_text):
                        fmt_ok = False
                    if lrc_text and not TIMESTAMP_RE_GRADE.search(lrc_text):
                        fmt_ok = False
                if not fmt_ok:
                    failed_checks += 1
                    add_issue("Lyrics not optimally formatted "
                              "(run Lyrics script)", basename)
                    track["issues"].append("LYRICS")

        # Transform tags must carry their language — TRANSLATION-EN,
        # TRANSLITERATION-JA-LATN — never the bare legacy names, so the
        # stored language is auditable (and gradeable) per file.
        if cfg.get("grade_check_lyrics_lang_tags", True):
            _bare_transforms = sorted(
                str(k).upper() for k in (af.all_tags() or {})
                if str(k).upper() in ("TRANSLATION", "TRANSLITERATION")
            )
            if _bare_transforms:
                total_checks += 1
                failed_checks += 1
                add_issue(
                    f"{', '.join(_bare_transforms)} tag lacks language detail "
                    "(expected e.g. TRANSLATION-EN, TRANSLITERATION-JA-LATN)",
                    basename)
                track["issues"].append("LYRICS")

        # Lyric TRANSFORMS that should and should not be there
        # (grade_check_xlit_transliteration / grade_check_xlit_translation):
        # script 17's output is auditable only if a transform that tells the
        # reader nothing new fails like a missing one. Both sides ask
        # mlo.lyrics_xlit.xlit_needs, so grading can never disagree with what
        # the script would write for the same track. An instrumental carries no
        # lyrics by definition — the app's own marker excludes it either way.
        xlit_text = str(lyr or "") if embedded else ""
        if not xlit_text.strip() and lrc:
            try:
                with open(_lrc_for(ap), "r", encoding="utf-8",
                          errors="replace") as _f:
                    xlit_text = _f.read()
            except OSError:
                xlit_text = ""
        xlit_text = xlit_text.strip()
        if xlit_text and inst_val != "1" \
                and should_write_audio_tag(cfg, "LYRICS", filepath=ap):
            need = xlit_needs(xlit_text, cfg, af.get_tag("LANGUAGE"))
            reader = primary_translation_lang(cfg)
            srclatin = dominant_script(xlit_text) == "latin"

            if cfg.get("grade_check_xlit_transliteration", True):
                total_checks += 1
                have = _xlit_stored(af, ap, "TRANSLITERATION")
                if need["transliteration"]:
                    if not have:
                        failed_checks += 1
                        add_issue("TRANSLITERATION missing for non-Latin lyrics "
                                  "(run Lyrics xlit/translate)", basename)
                        track["issues"].append("XLIT_MISSING")
                elif have:
                    failed_checks += 1
                    add_issue(
                        "TRANSLITERATION present but the lyrics are "
                        + ("already Latin script" if srclatin
                           else f"already in the reader's script ({reader})"),
                        basename)
                    track["issues"].append("XLIT_UNNEEDED")

            if cfg.get("grade_check_xlit_translation", True):
                total_checks += 1
                have = _xlit_stored(af, ap, "TRANSLATION")
                if need["translation"]:
                    missing = [l for l in need["langs"] if l not in have]
                    if missing:
                        failed_checks += 1
                        if have:
                            # A translation under another language is a
                            # mismatch, not a pass: the message names both
                            # sides so the fix is unambiguous (the tag to
                            # write, and the one that is there instead).
                            stored = ", ".join(
                                "TRANSLATION-" + l.upper() for l in sorted(have) if l
                            ) or "the bare TRANSLATION tag"
                            add_issue(
                                "TRANSLATION-" + "/".join(l.upper() for l in missing)
                                + f" missing for non-{reader} lyrics "
                                + f"(stored: {stored})", basename)
                        else:
                            add_issue(
                                f"TRANSLATION missing for non-{reader} lyrics",
                                basename)
                        track["issues"].append("XLIT_MISSING")
                elif have:
                    failed_checks += 1
                    add_issue("TRANSLATION present but the lyrics are already in "
                              f"the reader's language ({reader})", basename)
                    track["issues"].append("XLIT_UNNEEDED")

        # Per-track cover — a manifest entry (one image shared by several
        # tracks, e.g. 7 and 8) or a same-stem sidecar for this track.
        # Graded with the same cover checks as the album cover.* (size, square, etc.)
        # when such an image exists for this track. Uses the same config keys
        # (cover_target_size, cover_enforce_size/square, cover_crop_threshold, etc.)
        # Shared image => graded per referencing track, same verdict each time.
        try:
            sidecar = get_track_cover(album_dir, basename)
            track["sidecar_cover"] = sidecar
            track["sidecar_cover_file"] = os.path.basename(sidecar) if sidecar else None
            if sidecar and cfg.get("grade_check_sidecar_cover", True):
                total_checks += 1
                # Validate the sidecar image with the same cover checks
                if not _cover_image_ok(sidecar, cfg):
                    failed_checks += 1
                    # Differentiate missing vs dimension mismatch for UI
                    try:
                        exists = os.path.exists(sidecar) and os.path.getsize(sidecar) > 0
                    except OSError:
                        exists = False
                    if not exists:
                        detail = "empty"
                    else:
                        ext_sc = os.path.splitext(sidecar)[1].lower()
                        tgt_sc = _get_cover_target_size(ext_sc, cfg)
                        enforce_size_sc = bool(cfg.get("cover_enforce_size", False)) and bool(cfg.get("cover_resize_enabled", False)) and tgt_sc > 0
                        enforce_square_sc = bool(cfg.get("cover_enforce_square", False)) and bool(cfg.get("grade_check_cover_crop", True))
                        try:
                            # Dimensions come from the memoised reader: the
                            # same shared image is validated once per
                            # referencing track, and this detail text used to
                            # re-open the file for every one of them.
                            _w_sc, _h_sc = _get_cover_dimensions(sidecar)
                            if _w_sc is None or _h_sc is None:
                                detail = "unreadable"
                            elif enforce_size_sc and (abs(_w_sc - tgt_sc) > 1 or abs(_h_sc - tgt_sc) > 1):
                                detail = f"wrong size {_w_sc}x{_h_sc} (need {tgt_sc}x{tgt_sc})"
                            elif enforce_square_sc:
                                thr_sc = float(cfg.get("cover_crop_threshold", 0.0) or 0.0)
                                thr_sc = max(0.0, min(0.5, thr_sc))
                                ratio_sc = _w_sc / _h_sc if _h_sc else 1.0
                                if abs(ratio_sc - 1.0) > thr_sc:
                                    detail = f"aspect ratio {_w_sc}x{_h_sc} not square"
                                else:
                                    detail = "needs resize"
                            else:
                                detail = "needs resize"
                        except Exception:
                            detail = "needs resize"
                    add_issue(f"Sidecar cover {os.path.basename(sidecar)} {detail}", basename)
                    track["issues"].append("COVER")
        except Exception:
            track["sidecar_cover"] = None
            track["sidecar_cover_file"] = None

        tracks.append(track)

    # CD passes below look a track up by its full path; build the index once
    # instead of scanning the track list per file.
    track_by_path = {os.path.join(album_dir, tr["file"]): tr for tr in tracks}

    # MEDIA consistency (skip if MEDIA_SOURCE disabled for all tracks).
    any_media_enabled = any(
        should_write_audio_tag(cfg, "MEDIA", filepath=os.path.join(album_dir, tr["file"]))
        for tr in tracks if not tr.get("unreadable")
    )
    # MEDIA summary: the audio tracks' values, or — when the folder holds only
    # music videos — the video tracks' own MEDIA tags, which is the release
    # medium every file in the folder states. Without the fallback those
    # albums graded "Missing MEDIA" while every file carried it.
    media_summary = _summarize_values(media_values or video_media_values)
    if any_media_enabled and cfg.get("grade_check_media", True):
        total_checks += 1
        if media_summary is None:
            failed_checks += 1
            add_issue("Missing MEDIA", "album-wide")
        elif media_summary == "INCONSISTENT":
            failed_checks += 1
            add_issue("MEDIA inconsistent across tracks", "album-wide")
    elif not any_media_enabled:
        # No enabled tracks — treat as unknown but not failing
        media_summary = None

    digital = _is_digital(media_summary)

    # SOURCE policy per track (skipped if MEDIA_SOURCE disabled for this filetype).
    if cfg.get("grade_check_source", True):
        for tr in tracks:
            if tr.get("unreadable"):
                continue
            # Resolve full path for per-type check
            tr_path = os.path.join(album_dir, tr["file"])
            if not should_write_audio_tag(cfg, "SOURCE", filepath=tr_path):
                continue
            total_checks += 1
            src = tr["values"].get("SOURCE")

            if digital:
                if not src:
                    failed_checks += 1
                    add_issue("Missing SOURCE (required for Digital Media)", tr["file"])
                    tr["issues"].append("SOURCE")
            else:
                if src:
                    failed_checks += 1
                    add_issue("SOURCE present but MEDIA is not Digital Media", tr["file"])
                    tr["issues"].append("SOURCE")

    # SOURCE consistency for Digital Media (only if at least one track enables MEDIA_SOURCE).
    if digital and cfg.get("grade_check_source", True):
        # Filter to only enabled filetypes
        enabled_sources = []
        any_enabled = False
        for tr in tracks:
            if tr.get("unreadable"):
                continue
            tr_path = os.path.join(album_dir, tr["file"])
            if should_write_audio_tag(cfg, "SOURCE", filepath=tr_path):
                any_enabled = True
                v = tr["values"].get("SOURCE")
                if v:
                    enabled_sources.append(v)
        if any_enabled:
            total_checks += 1
            clean_sources = _clean_set(enabled_sources)

            if len(clean_sources) > 1:
                failed_checks += 1
                add_issue("SOURCE inconsistent across album", "album-wide")

    # Album-wide tag consistency (skip if no enabled tracks for this tag).
    if cfg.get("grade_check_album_tags", True):
        for t in ALBUM_TAGS:
            vals = album_tag_values.get(t)
            if vals is None:
                # No enabled filetypes for this tag — skip grading
                continue
            total_checks += 1
            clean = {x for x in vals if x}

            if not clean:
                failed_checks += 1
                add_issue(f"Missing album tag {t}", "album-wide")
                for tr in tracks:
                    tr["issues"].append(t)
            elif "" in vals:
                failed_checks += 1
                add_issue(f"Album tag {t} missing on some tracks", "album-wide")
                for tr in tracks:
                    tr["issues"].append(t)
            elif len(clean) > 1:
                failed_checks += 1
                add_issue(f"Album tag {t} inconsistent", "album-wide")
                for tr in tracks:
                    tr["issues"].append(t)
            # surface the album tag value per track so detail views can
            # render it (e.g. ALBUM DYNAMIC RANGE as its own row)
            album_val = next(iter(clean)) if len(clean) == 1 else ("" if clean else None)
            for tr in tracks:
                tr["values"][t] = album_val

    # Media-specific file requirements.
    if _is_cd(media_summary):
        if cfg.get("grade_check_cd_log", True):
            total_checks += 1
            if not has_log:
                failed_checks += 1
                add_issue("Missing .log file", "album")
            elif not usable_logs:
                # A file that merely ENDS in .log (empty, or whitespace only)
                # is not a rip log: it cannot carry a CRC or a LOG_GRADE.
                failed_checks += 1
                add_issue("Rip .log is empty/unreadable — not a usable rip log",
                          "album")

        if cfg.get("grade_check_cd_cue", True):
            total_checks += 1
            if not has_cue:
                failed_checks += 1
                add_issue("Missing .cue file", "album")

        # CD rip naming: .log/.cue must match discs_rename_pattern (default
        # CD-{n} → CD-1 … CD-11) — the deterministic scheme from discs.py.
        # If autorename left a file under its original name (ambiguous case,
        # or discs_rename disabled) the album fails here. .log CONTENTS are
        # never modified — only the filename is changed.
        if cfg.get("grade_check_disc_naming", True):
            try:
                from .discs import _disc_pattern_for as _pat, _is_expected_disc_file as _is_exp
                pat = _pat(cfg)
                bad_logs = [f for f in all_files
                            if f.lower().endswith(".log")
                            and not _is_exp(f, pat, ".log")]
                bad_cues = [f for f in all_files
                            if f.lower().endswith(".cue")
                            and not _is_exp(f, pat, ".cue")]
                # Counted at the end: a check that throws is counted by
                # unavailable() instead, so it is counted exactly once either
                # way and can never vanish from the grade.
                total_checks += 1
                if bad_logs or bad_cues:
                    failed_checks += 1
                    detail = ", ".join(bad_logs + bad_cues)
                    add_issue(f"CD rip sheets not named {pat} (found: {detail}) — "
                              f"enable Settings → CD Rips → Auto-Rename or "
                              f"rename manually to {pat.replace('{n}', '1')}.log", "album")
            except Exception as e:
                unavailable("CD rip sheet naming check", e)

        # CD releases must carry the rip-log score on every track (skipped if
        # LOG_GRADE disabled for this filetype). Digital Media has no log.
        if _is_cd(media_summary) and cfg.get("grade_check_log_grade", True):
            for tr in tracks:
                if tr.get("unreadable"):
                    continue
                if _is_video_file(tr.get("file")):
                    continue
                tr_path = os.path.join(album_dir, tr["file"])
                if not should_write_audio_tag(cfg, "LOG_GRADE", filepath=tr_path):
                    continue
                total_checks += 1
                lg = tr.get("log_grade")
                if lg is None:
                    failed_checks += 1
                    add_issue("Missing LOG_GRADE tag (run Audit Library)",
                              tr["file"])
                    tr["issues"].append("LOG_GRADE")
                elif not lg.isdigit() or not (0 <= int(lg) <= 100):
                    failed_checks += 1
                    add_issue(f"LOG_GRADE not 0-100: {lg}", tr["file"])
                    tr["issues"].append("LOG_GRADE")
                else:
                    try:
                        # 100 = the shipped default (mlo.config); 0 is a
                        # deliberate "no threshold", never a partial-cfg fallback.
                        thresh = int(cfg.get("grade_log_score_threshold", 100) or 0)
                        thresh = max(0, min(100, thresh))
                    except Exception:
                        thresh = 0
                    if thresh > 0 and int(lg) < thresh:
                        failed_checks += 1
                        add_issue(f"LOG_GRADE {lg} below threshold {thresh} (Logchecker score too low)", tr["file"])
                        tr["issues"].append("LOG_GRADE")

        # CD integrity: every track must be covered by a per-track CRC in
        # the rip log(s). The .log checksum is the ONLY audit source for
        # CD rips — an album whose log is missing, unreadable, or lacks a
        # CRC for any of its tracks can never grade PASS.
        if cfg.get("grade_check_crc", True):
            try:
                from .discs import parse_log_checksums, read_log_text, \
                    album_discs as _album_discs, disc_of_filename, \
                    _file_track_number, _disc_pattern_for as _pat2, \
                    _disc_expected_name as _exp_name
                log_paths = [os.path.join(album_dir, f)
                             for f in all_files
                             if f.lower().endswith(".log")]
                # Per-DISC checksum maps, keyed by the log each checksum came
                # from: a multi-disc rip has one .log per disc, and merging
                # them into one flat map (then guessing the disc back from
                # the RENAME pattern) reported every track as uncovered as
                # soon as auto-rename was off or the logs were named
                # differently. The disc is now read from the log's own file
                # name, never from a required naming scheme.
                disc_pattern = _pat2(cfg)
                per_disc_crc = {}
                unmapped_crc = {}

                def _disc_for_log(name):
                    low = os.path.basename(name).lower()
                    for n in range(1, 100):
                        try:
                            if low == _exp_name(disc_pattern, n, ".log").lower():
                                return n
                        except Exception:
                            break
                    try:
                        from .discs import _log_name_disc
                        return _log_name_disc(name)
                    except Exception:
                        return None

                for lp in sorted(log_paths):
                    got = parse_log_checksums(read_log_text(lp))
                    if not got:
                        continue
                    d = _disc_for_log(lp)
                    if d:
                        per_disc_crc.setdefault(d, {}).update(got)
                    else:
                        unmapped_crc.update(got)
                if not per_disc_crc and not unmapped_crc:
                    total_checks += 1
                    failed_checks += 1
                    add_issue("Rip .log has no per-track CRC checksums "
                              "(cannot verify CD integrity)", "album")
                else:
                    discs_map = _album_discs(album_dir)
                    disc_by_path = {p: d for d, paths in (discs_map or {}).items()
                                    for p in paths}
                    single_log = len(log_paths) <= 1
                    # (path, the CRC the log states for it) for the value
                    # pass below, filled while the mapping is already resolved
                    # here — the disc-to-log attribution must not be guessed
                    # twice.
                    stated = []
                    for ap in audio_paths:
                        tr_track = track_by_path.get(ap)
                        if tr_track is None or tr_track.get("unreadable"):
                            continue
                        if _is_video_file(tr_track.get("file")):
                            continue
                        # Use _track_num_of for D-TT like 1-01 -> 01, not disc number
                        try:
                            from .discs import _track_num_of
                            tn = _track_num_of(ap)
                            if tn is None:
                                tn = _file_track_number(ap)
                        except Exception:
                            tn = _file_track_number(ap)
                        d = (disc_by_path.get(ap)
                             or disc_of_filename(os.path.basename(ap)) or 1)
                        crcs = per_disc_crc.get(d)
                        if crcs is None and single_log:
                            # Exactly one log covers the album's tracks,
                            # whichever disc its name claims — a single-disc
                            # rip whose log name carries no disc number.
                            crcs = next(iter(per_disc_crc.values()), {}) or unmapped_crc
                        elif crcs is None and unmapped_crc and not per_disc_crc:
                            # Every log failed to state its disc (unusual
                            # names): their checksums are all there is, and
                            # nothing can be attributed to the wrong disc
                            # because no disc was attributable at all.
                            crcs = unmapped_crc
                        covered = tn is not None and tn in (crcs or {})
                        if covered:
                            stated.append((ap, (crcs or {}).get(tn)))
                        total_checks += 1
                        if not covered:
                            failed_checks += 1
                            add_issue("Track not covered by .log CRC "
                                      "(unverifiable CD rip)", tr_track["file"])
                            tr_track["issues"].append("CRC")

                    # ---- the CRC VALUES, not just their coverage ----------
                    # A log states a CRC-32 per track and it must equal the
                    # CRC-32 of that track's own decoded PCM. Coverage alone
                    # let a log from a DIFFERENT rip (or audio altered after
                    # the rip) grade PASS, while the check has always been
                    # advertised as "checksums must match the audio". Tracks
                    # the log cannot be compared against — a lossy encode,
                    # which can never reproduce the WAV CRC — are not
                    # verifiable here and are left to the coverage verdict.
                    # ponytail: decoding is the cost of proof; the memo in
                    # discs._audio_crc32 means an album grades on the same
                    # decode the audit pass already paid for.
                    try:
                        from .discs import _audio_crc32 as _crc_of, \
                            LOSSLESS_CRC_EXTS as _crc_exts
                        from .tools import detect_all_tools as _detect
                        ffmpeg_exe = (_detect().get("ffmpeg") or {}).get("ffmpeg_exe")
                        pairs = [(ap, want) for ap, want in stated
                                 if want and ffmpeg_exe
                                 and os.path.splitext(ap)[1].lower() in _crc_exts]
                        if pairs:
                            # Decoded SERIALLY: this runs inside the run's own
                            # album pool, so a four-way pool here multiplied
                            # that by four (up to 64 ffmpeg decodes at once on
                            # a 16-way grade) and the decodes spent their time
                            # queueing for CPU. The album pool is what keeps
                            # the cores busy; this pass only has to be right.
                            actuals = [_crc_of(ffmpeg_exe, ap) for ap, _ in pairs]
                            for (ap, want), actual in zip(pairs, actuals):
                                if actual is None:
                                    # undecodable: the coverage check speaks
                                    # for this track, not a fabricated verdict
                                    continue
                                tr_track = track_by_path.get(ap)
                                if tr_track is None:
                                    continue
                                total_checks += 1
                                if str(actual).upper() != str(want).upper():
                                    failed_checks += 1
                                    add_issue(
                                        f"Log CRC {str(want).upper()} does not match "
                                        f"the track's audio CRC {actual} — the rip "
                                        f"does not match its own log",
                                        tr_track["file"])
                                    tr_track["issues"].append("CRC_MISMATCH")
                    except Exception as e:
                        unavailable("CD rip-log CRC value check", e)
            except Exception as e:
                unavailable("CD rip-log CRC coverage check", e)

        # CD format: must be 16-bit 44.1 kHz (CD-DA) — helps detect fake rips from hi-res upsampled sources
        if cfg.get("grade_check_cd_format", True) and _is_cd(media_summary):
            try:
                for ap in audio_paths:
                    tr_track = track_by_path.get(ap)
                    if tr_track is None or tr_track.get("unreadable"):
                        continue
                    if _is_video_file(tr_track.get("file")):
                        continue
                    if _lossy_target_file(ap, cfg):
                        # This album was deliberately converted to the
                        # configured lossy target: the CD-DA stream the check
                        # describes is gone, so there is nothing left to judge
                        # (see _lossy_target_file).
                        continue
                    # Format info captured when the file was parsed for its
                    # tags — no second AudioFile() per track.
                    bits, rate = format_by_path.get(ap) or (None, None)
                    if rate is None:
                        # No rate info: cannot verify, skip
                        continue
                    # CD must be 16/44.1. Not every format reports bit depth
                    # (MP3/MP4 usually don't) — then the rate is the only check.
                    is_ok = True
                    detail = ""
                    if bits is not None and bits != 16:
                        is_ok = False
                        detail = f"{bits}-bit"
                    if rate != 44100:
                        is_ok = False
                        detail = f"{detail} {rate}Hz".strip() if detail else f"{rate}Hz"
                    total_checks += 1
                    if not is_ok:
                        failed_checks += 1
                        add_issue(f"CD must be 16-bit 44.1 kHz (found {detail or 'unknown format'})", tr_track["file"])
                        tr_track["issues"].append("CD_FORMAT")
            except Exception as e:
                unavailable("CD 16-bit/44.1 kHz format check", e)

        # Viewer columns for CD log checksum / AccurateRip — REAL/NONE/FAKE
        # CHECKSUM: derived from rip .log's SHA256 (EAC), AccurateRip is ONLY via .accurip (CUETools)
        # Per user: "ACCURATERIP values in the column for it and for all auditing / grading
        # purposes should ONLY be pulled from .accurip files."  No log fallback.
        checksum_status = "NONE"
        accuraterip_status = "NONE"
        # Per-disc checksum map for correct per-track column (fixes multi-disc bug where one bad log marked whole album FAKE)
        per_disc_checksum_map = {}
        try:
            from .discs import check_log_checksum as _check_csum_v
            csum_logs_v = [os.path.join(album_dir, f) for f in all_files if f.lower().endswith(".log")]
            # Build per-disc checksum status
            from .discs import _disc_pattern_for as _pat_csum, _disc_expected_name as _exp_csum, _log_name_disc as _lnd_csum, album_discs as _ad_csum
            try:
                _pat_c = _pat_csum(cfg)
            except Exception:
                _pat_c = "CD-{n}"
            try:
                _discs_for_csum = _ad_csum(album_dir)
            except Exception:
                _discs_for_csum = {}
            has_csum = False
            has_invalid = False
            has_unverified = False
            for lp in sorted(csum_logs_v):
                state, _det = _check_csum_v(lp)
                # Determine disc for this log file
                disc_for_log = None
                base_log = os.path.basename(lp)
                # Try pattern expected name match
                for cand_n in range(1, 20):
                    try:
                        if os.path.normcase(base_log) == os.path.normcase(_exp_csum(_pat_c, cand_n, ".log")):
                            disc_for_log = cand_n
                            break
                    except Exception:
                        continue
                if disc_for_log is None:
                    # Fallback to explicit disc number in filename
                    try:
                        dn = _lnd_csum(base_log)
                        if dn and dn in (_discs_for_csum or {}):
                            disc_for_log = dn
                    except Exception:
                        pass
                if disc_for_log is None:
                    # Single-disc case: treat as disc 1
                    if _discs_for_csum and len(_discs_for_csum) == 1:
                        disc_for_log = next(iter(_discs_for_csum.keys()))
                    elif not _discs_for_csum:
                        disc_for_log = 1
                    else:
                        # Multi-disc orphan with no mapping — conservatively mark as not mapped (skip per-disc, but still affect album aggregate)
                        disc_for_log = None
                # Map state to per-disc
                if disc_for_log is not None:
                    if state == "ok":
                        per_disc_checksum_map[disc_for_log] = "REAL"
                    elif state == "unverified":
                        # The log carries a checksum that NOTHING could check
                        # (no EAC log checker, so Logchecker's own 'checksum_ok'
                        # is untested — see mlo.discs.check_log_checksum). Not a
                        # pass and not a failure: the column says UNVERIFIED, so
                        # a doctored log can never read REAL, and an honest
                        # install without the helper is not accused of anything.
                        per_disc_checksum_map[disc_for_log] = "UNVERIFIED"
                    elif state == "invalid":
                        per_disc_checksum_map[disc_for_log] = "FAKE"
                    elif state == "missing":
                        # A log checksum is never required (spec R27): an absent
                        # line is not evidence against the rip, so the disc
                        # reads NONE here exactly like a 0.99-era EAC or XLD log
                        # that never wrote one. mlo.audit logs the case by name.
                        per_disc_checksum_map[disc_for_log] = "NONE"
                    elif state == "unsupported":
                        per_disc_checksum_map[disc_for_log] = "NONE"
                    elif state is None:
                        # Error / not found — leave as NONE
                        pass
                # Album aggregate still needed for _realtime fallback. Only a
                # checksum that failed to verify is a failure; an absent one is
                # not (spec R27), so it leaves the aggregate at NONE.
                if state == "ok":
                    has_csum = True
                elif state == "unverified":
                    has_unverified = True
                elif state == "invalid":
                    has_invalid = True
                    has_csum = True
            if has_invalid:
                checksum_status = "FAKE"
            elif has_unverified:
                # A claimed checksum nothing verified outranks a plain NONE:
                # the log is not evidence either way, and saying REAL here is
                # exactly the claim that let a modified log through.
                checksum_status = "UNVERIFIED"
            elif has_csum:
                checksum_status = "REAL"
            else:
                checksum_status = "NONE"
            # If multi-disc and per-disc map shows only one disc FAKE, album stays FAKE (conservative) but per-track will be per-disc below
            # For accurate per-disc, keep map as is
            # AccurateRip viewer: ONLY via .accurip files (CUETools), per user request
            # CD-{n}.accurip scheme participates in disc rename; each disc's file is CD-N.accurip
            accurip_files = [os.path.join(album_dir, f) for f in all_files if f.lower().endswith(".accurip")]
            has_ar = False
            has_mismatch = False
            # Per-track map disc_n -> {track_num: status}
            per_disc_ar_map = {}
            if accurip_files:
                try:
                    from .accurip import parse_accurip_status as _parse_ar, parse_accurip_per_track as _parse_ar_per
                except Exception:
                    _parse_ar = None
                    _parse_ar_per = None
                for ap in sorted(accurip_files):
                    try:
                        txt_ar = open(ap, "r", encoding="utf-8", errors="replace").read()
                    except OSError:
                        continue
                    if _parse_ar is not None:
                        st, _detail = _parse_ar(txt_ar)
                        if st == "REAL":
                            has_ar = True
                        elif st == "FAKE":
                            has_mismatch = True
                            has_ar = True
                        else:  # NONE
                            # Do not set has_ar for NONE – keeps album NONE if no REAL/FAKE
                            pass
                        # Build per-disc per-track map for per-track column
                        if _parse_ar_per is not None:
                            try:
                                # Determine disc number from filename CD-{n}.accurip
                                base = os.path.basename(ap)
                                disc_n = 1
                                try:
                                    from .discs import _disc_pattern_for as _pat_tmp, _disc_expected_name as _exp_tmp
                                    # Try to parse disc number from filename
                                    import re as _re2
                                    m_disc = _re2.search(r"(\d+)", base)
                                    if m_disc:
                                        # Prefer pattern-based expected names
                                        for cand_n in range(1, 20):
                                            if os.path.normcase(base) == os.path.normcase(_exp_tmp(_pat_tmp(cfg), cand_n, ".accurip")):
                                                disc_n = cand_n
                                                break
                                        else:
                                            # Fallback to first integer found
                                            disc_n = int(m_disc.group(1))
                                except Exception:
                                    pass
                                per = _parse_ar_per(txt_ar)
                                if per:
                                    per_disc_ar_map[disc_n] = per
                            except Exception:
                                pass
                        if has_mismatch:
                            # Continue to build per_disc maps even if overall FAKE – need all discs for per-track
                            pass
                    else:
                        low_ar = txt_ar.lower()
                        if "accurately ripped" in low_ar and "no match" not in low_ar:
                            has_ar = True
                        elif "no match" in low_ar:
                            has_mismatch = True
                            has_ar = True
                # No fallback to .log – strictly .accurip
            # if no .accurip files, stays NONE
            if has_mismatch:
                accuraterip_status = "FAKE"
            elif has_ar:
                accuraterip_status = "REAL"
            else:
                accuraterip_status = "NONE"
            # Store for viewer columns (album-level and per-track)
            # Album disc mapping — scanned once for the whole album; both
            # per-track lookups below used to re-list the folder per track.
            try:
                from .discs import album_discs as _ad_tracks
                discs_all_tracks = _ad_tracks(album_dir)
            except Exception:
                discs_all_tracks = {}
            # Per-disc checksum (fixes whole-album FAKE when only one disc's log is bad) and per-track accuraterip
            for tr in tracks:
                # Per-disc checksum lookup — only that disc's tracks show FAKE
                try:
                    from .discs import disc_of_filename as _dof_c
                    base_c = tr["file"]
                    disc_n_c = _dof_c(base_c)
                    if disc_n_c is None:
                        try:
                            discs_all_c = discs_all_tracks
                            if discs_all_c:
                                full_c = os.path.join(album_dir, base_c)
                                for dn_c, tlist_c in discs_all_c.items():
                                    if any(os.path.normcase(os.path.join(album_dir, os.path.basename(p))) == os.path.normcase(full_c) or os.path.basename(p).lower() == base_c.lower() for p in tlist_c):
                                        disc_n_c = dn_c
                                        break
                        except Exception:
                            pass
                        if disc_n_c is None:
                            disc_n_c = 1
                    if disc_n_c in per_disc_checksum_map:
                        tr["checksum_status"] = per_disc_checksum_map[disc_n_c]
                    else:
                        tr["checksum_status"] = checksum_status
                except Exception:
                    tr["checksum_status"] = checksum_status
                # Resolve per-track AccurateRip: disc + track number lookup
                try:
                    from .discs import disc_of_filename as _dof, _track_num_of as _tnof, _file_track_number as _ftn
                    # Determine disc for this track file
                    base = tr["file"]
                    disc_n = _dof(base)
                    if disc_n is None:
                        # Fallback: infer from album discs mapping
                        try:
                            discs_all = discs_all_tracks
                            if discs_all:
                                # Find which disc contains this file path
                                full = os.path.join(album_dir, base)
                                for dn, tlist in discs_all.items():
                                    if any(os.path.normcase(os.path.join(album_dir, os.path.basename(p))) == os.path.normcase(full) or os.path.basename(p).lower() == base.lower() for p in tlist):
                                        disc_n = dn
                                        break
                        except Exception:
                            pass
                        if disc_n is None:
                            disc_n = 1
                    # Determine track number
                    full_path = os.path.join(album_dir, base)
                    tn = _tnof(full_path)
                    if tn is None:
                        tn = _ftn(base)
                    if tn is not None and disc_n in per_disc_ar_map:
                        per_map = per_disc_ar_map[disc_n]
                        # Track numbers in .accurip are 1-based without disc prefix (01..N per disc)
                        # For disc N, track 1 corresponds to per_map[1]
                        st = per_map.get(int(tn))
                        if st:
                            tr["accuraterip_status"] = st
                        else:
                            # Track not in map (e.g., not present in DB) → NONE
                            tr["accuraterip_status"] = "NONE"
                            # Keep has_* for album already computed
                            continue
                    else:
                        # Fallback: if per-track map not available or track not found, use album status
                        # (e.g., missing .accurip → NONE, album FAKE/REAL otherwise)
                        # For missing disc file, per_disc_ar_map won't have entry → falls through to album
                        if has_mismatch:
                            tr["accuraterip_status"] = "FAKE" if accuraterip_status == "FAKE" else accuraterip_status
                        elif has_ar:
                            tr["accuraterip_status"] = "REAL" if accuraterip_status == "REAL" else accuraterip_status
                        else:
                            tr["accuraterip_status"] = "NONE"
                        continue
                except Exception:
                    tr["accuraterip_status"] = accuraterip_status
                # The per-track AUDIT readout is decided in one place below
                # (the three legs), so nothing is marked FAKE here: marking a
                # leg NONE as FAKE is what made "nobody could check this
                # pressing" read exactly like "your rip is bad".
            # ---- the three legs of a MEDIA=CD verdict ----------------------
            # Script 6 writes a CD's verdict from exactly three legs
            # (mlo.audit.run_audit_library — spec R21), so the readout below
            # asks the SAME question: the rip log's score, the disc's
            # checksums, and AccurateRip. Reading the library by a different
            # rule is how a disc could show REAL while the run that stamped it
            # decided otherwise.
            def _cd_legs(tr):
                """{leg name: 'ok' | 'fail' | 'missing'} for one CD track.

                A leg whose setting is off is absent (the same escape hatch
                mlo.audit applies), and a leg nothing established is
                'missing' — reported by name, never counted as a failure:
                "we could not check" is not "your rip is bad".
                """
                legs = {}
                try:
                    threshold = int(cfg.get("audit_log_score_threshold", 100) or 0)
                except (TypeError, ValueError):
                    threshold = 0
                if threshold > 0:
                    grade = str(tr.get("log_grade") or "").strip()
                    if not (grade.isdigit() and 0 <= int(grade) <= 100):
                        legs["log-score"] = "missing"
                    else:
                        legs["log-score"] = ("fail" if int(grade) < threshold
                                             else "ok")
                states = []
                if cfg.get("audit_verify_log_checksum", True):
                    # Only a state this half actually reached counts: REAL (the
                    # log's SHA256 verified) or FAKE (it did not). NONE is "this
                    # log carries no checksum" — XLD, an EAC log older than 1.0,
                    # or a line that was never written — and per spec R30 that is
                    # not a missing leg to charge the album for. Reading it as
                    # missing is what failed a 2008 rip whose log was written by
                    # a version that had no checksum to write.
                    csum_state = str(tr.get("checksum_status") or "")
                    log_state = {"REAL": "ok", "FAKE": "fail"}.get(csum_state)
                    if log_state:
                        states.append(log_state)
                    elif csum_state == "UNVERIFIED":
                        # The log claims a checksum that NOTHING could verify
                        # (no EAC log checker — mlo.discs.check_log_checksum).
                        # Neither evidence for the rip nor against it, so the
                        # leg reads 'missing' (the docstring's "we could not
                        # check"): an honest install is never charged for a
                        # helper it has not installed, and the CRCs below still
                        # decide the leg when they can.
                        states.append("missing")
                if cfg.get("grade_check_crc", True):
                    codes = set(tr.get("issues") or ())
                    states.append("fail" if "CRC_MISMATCH" in codes
                                  else ("missing" if "CRC" in codes else "ok"))
                if states:
                    legs["checksums"] = ("fail" if "fail" in states
                                         else ("missing" if "missing" in states
                                               else "ok"))
                if cfg.get("audit_require_accuraterip", True):
                    legs["accuraterip"] = {"REAL": "ok", "FAKE": "fail"}.get(
                        str(tr.get("accuraterip_status") or ""), "missing")
                return legs

            def _cd_leg_reason(name, tr):
                """Why a leg failed, in the words mlo.audit's verdict uses."""
                if name == "accuraterip":
                    if str(tr.get("accuraterip_status") or "") == "NONE":
                        return ("the .accurip holds no AccurateRip verdict — "
                                "the pressing is not in the database, so "
                                "nothing was checked")
                    return "AccurateRip reports the track does not match"
                if name == "checksums":
                    return ("the rip log's checksum does not match the audio")
                return "the rip log's score is below audit_log_score_threshold"

            # ---- the rip's own evidence decides the CD's readout -----------
            # All three legs passing reads REAL; a FAILING leg reads FAKE and
            # names itself; a MISSING leg does not lower a stamped verdict on
            # its own (it is named in the album's issues instead) and, for a
            # track carrying NO stamped verdict, R26's reading still applies —
            # its own verified evidence (a verifying .log checksum or a REAL
            # .accurip) is what a user sees for a disc script 6 has not
            # stamped yet.
            cd_legs_missing = {}
            for tr in tracks:
                if not _is_cd(media_summary) or _is_video_file(tr.get("file")):
                    continue
                legs = _cd_legs(tr)
                tr["audit_legs"] = legs
                failed = sorted(n for n, state in legs.items() if state == "fail")
                missing = sorted(n for n, state in legs.items() if state == "missing")
                # The verdict the file actually carries: `audit` is the
                # stored tag as read, and nothing has rewritten it yet.
                stored_tag = str(tr.get("audit") or "").strip()
                if failed:
                    tr["audit"] = "FAKE"
                    tr["audit_verified"] = "; ".join(
                        _cd_leg_reason(n, tr) for n in failed)
                elif legs and not missing:
                    tr["audit"] = "REAL"
                    tr["audit_verified"] = ("all three sources: "
                                            + ", ".join(sorted(legs)))
                elif not stored_tag and (tr.get("checksum_status") == "REAL"
                                         or tr.get("accuraterip_status") == "REAL"):
                    tr["audit"] = "REAL"
                    tr["audit_verified"] = (
                        "log-checksum" if tr.get("checksum_status") == "REAL"
                        else "accuraterip")
                for name in missing:
                    cd_legs_missing.setdefault(name, []).append(tr.get("file"))
                    if tr.get("audit_legs_missing") is None:
                        tr["audit_legs_missing"] = missing
            # A leg nothing established is a FAILED CHECK for the album, and it
            # is charged exactly once however many legs and tracks it covers.
            # It used to cost nothing, on the reading that the artefact behind
            # each leg already had a graded check of its own — which is what
            # let an album list "nothing established the CD verdict's
            # 'accuraterip' leg" as a problem to fix and STILL grade PASS,
            # drawing a green dot beside a problem it had just named. An issue
            # the verdict does not charge is a verdict that lies, so the
            # readout below is a check of its own: every CD leg has evidence.
            # The AccurateRip leg is the ONE exception, and it is not for its
            # own sake: a pressing the database has never seen reads exactly
            # like a disc with no .accurip at all, and no code path can tell
            # the two apart. A missing FILE is the app's to fix (script 9
            # writes it); a missing DATABASE ENTRY is not, and failing an
            # album for it failed the rip for what the network does not know.
            # It is reported in `notes` instead — said plainly as NOT CHECKED,
            # which is what it is — so nothing is hidden and nothing is claimed.
            if [n for n in cd_legs_missing if n != "accuraterip"]:
                total_checks += 1
                failed_checks += 1
            # The missing legs are named ONCE for the album, per leg, so the
            # readout says which artefact is absent rather than only that one
            # is.
            _missing_wording = {
                "log-score": "no LOG_GRADE tag scores the rip log",
                "checksums": ("the rip log states no CRC for these tracks, so "
                              "nothing was compared with the audio"),
                "accuraterip": ("no .accurip verdict verifies this disc — a "
                                "pressing that is not in the AccurateRip "
                                "database reads the same as a disc with no "
                                ".accurip"),
            }
            for name in sorted(cd_legs_missing):
                subject = (f"the CD verdict's '{name}' evidence for "
                           f"{len(cd_legs_missing[name])} track(s)")
                reason = _missing_wording.get(name, "no evidence")
                if name == "accuraterip":
                    notes.append(
                        f"AUDIT not checked: nothing established {subject}: "
                        f"{reason} — the database simply may not know this "
                        f"pressing, and a disc that could not be checked is "
                        f"not a bad rip")
                else:
                    add_issue(
                        f"AUDIT readout: nothing established {subject}: "
                        f"{reason} — the stored verdict is shown as it is, "
                        f"not guessed (we could not check, which is not the "
                        f"same as a bad rip)", "album")
            # ---- CD verification resolves the deferred AUDIT requirement --
            # A rip whose .log CRC verifies, or whose .accurip verifies, is
            # REAL on that evidence alone: the stored tag (AudioAuditor's
            # spectral verdict, or a stale FAKE from an earlier run) is
            # corrected, and the requirement is satisfied.
            for ap, (basename, stored_tag) in deferred_audit.items():
                tr = track_by_path.get(ap)
                if tr is None:
                    continue
                total_checks += 1
                if (tr.get("checksum_status") == "REAL"
                        or tr.get("accuraterip_status") == "REAL"):
                    tr["audit"] = "REAL"
                    # What proved it: the rip log's own EAC checksum, or the
                    # .accurip's AccurateRip verdict. (The per-track CRC match
                    # is proven by script 6, which is what writes the tag.)
                    tr["audit_verified"] = ("log-checksum" if tr.get("checksum_status") == "REAL"
                                            else "accuraterip")
                    if str(tr.get("values", {}).get("AUDIT") or "").strip().upper() != "REAL":
                        tr["values"]["AUDIT"] = "REAL"
                    continue
                failed_checks += 1
                # Which leg the verdict is missing, in the same words the
                # readout and mlo.audit's run log use: a disc nobody could
                # check must not read like a disc that failed.
                _legs = tr.get("audit_legs") or _cd_legs(tr)
                _short = ", ".join(sorted(
                    n for n, state in _legs.items() if state in ("fail", "missing")
                )) or "verifiable evidence (no check could be evaluated)"
                if not stored_tag:
                    add_issue(f"Missing AUDIT tag (run Audit Library) — the CD "
                              f"verdict needs: {_short}", basename)
                else:
                    add_issue(f"AUDIT tag is {stored_tag.upper()} (not REAL) — "
                              f"the CD verdict needs: {_short}", basename)
                tr["issues"].append("AUDIT")

            # ---- manual override wins over every derived verdict ----------
            # AudioAuditor's REAL/FAKE is EVIDENCE, not a fact: a user who has
            # verified a rip by other means (a second drive, a different tool,
            # a known-good source) must be able to say so and have it stick.
            # The AUDIOAUDITOR_OVERRIDE tag is applied LAST, after every
            # derived path above, and both the per-track statuses and the
            # album-level ALL-REAL gates are made to agree with it — so a
            # forced re-audit reproduces the override instead of erasing it.
            for tr in tracks:
                ov = str(tr.get("values", {}).get("AUDIOAUDITOR_OVERRIDE") or "").strip().upper()
                if ov not in ("REAL", "FAKE"):
                    continue
                tr["audit"] = ov
                tr["audit_override"] = ov
                tr["accuraterip_status"] = ov
                if ov == "REAL":
                    tr["checksum_status"] = "REAL"
            # ---- the rip log's own checksums, GRADED ----------------------
            # A log whose EAC SHA256 does not verify — or that claims none
            # while one is required — describes a rip nobody can prove, so the
            # album FAILS here. This verdict used to reach only the AUDIT
            # column and the audit requirement (grade_check_audit, off by
            # default): a provably bad log could grade PASS. The per-track
            # statuses are read AFTER the manual override above, so a forced
            # AUDIOAUDITOR_OVERRIDE=REAL still wins over the derived verdict.
            # 'unsupported' (XLD, older EAC logs with no checksum concept)
            # stays NONE and passes — nothing claimed, nothing refuted.
            if cfg.get("grade_check_log_checksum", True):
                try:
                    suspect = [tr for tr in tracks
                               if not _is_video_file(tr.get("file"))
                               and tr.get("checksum_status") == "FAKE"]
                    total_checks += 1
                    if suspect:
                        failed_checks += 1
                        names = ", ".join(sorted({str(tr.get("file") or "") for tr in suspect}))
                        add_issue("Rip .log checksum does not verify "
                                  f"(EAC SHA256 invalid or missing) — {names}",
                                  "album")
                        for tr in suspect:
                            tr["issues"].append("LOG_CHECKSUM")
                except Exception as e:
                    unavailable("Rip .log checksum check", e)
        except Exception as e:
            # Never a silent pass: this block resolves the CD's deferred AUDIT
            # requirement, applies the manual override and grades the log's own
            # checksums. A bare `pass` here removed all three from the grade at
            # once — the album kept its PASS with the CD's integrity unproven.
            unavailable("CD integrity verification", e)

        # Grading: AccurateRip is AUDIT-only per user request — grading is reserved to tagging.
        # Do NOT fail grading on missing/FAKE accurip; only auditing fails. Viewer column still shows REAL/NONE/FAKE.

    elif _is_digital(media_summary):
        # SOURCE requirements already checked per-track.
        pass

    else:
        # Vinyl / SACD / DVD / Blu-ray / Cassette etc. — valid media, no CUE
        # or LOG expectations; only genuinely unknown values fail.
        if cfg.get("grade_check_media", True):
            total_checks += 1
            if (
                media_summary is not None
                and media_summary != "INCONSISTENT"
                and media_summary.strip().lower() not in KNOWN_MEDIA
            ):
                failed_checks += 1
                add_issue(f"Unrecognized MEDIA value: {media_summary}", "album-wide")

    # Cover check — also builds a UI-friendly cover_detail string that
    # surfaces enforcement failures (e.g. "cover.jpg (wrong size 500x500 → 1000x1000)"
    # or "MISSING (wrong size)") so the library tree's COVER column is
    # never silently "cover.jpg" when the image would fail grading.
    cover_detail = cover_file or "MISSING"
    cover_ok = True
    if cfg.get("grade_check_cover", True):
        total_checks += 1
    if not cover_file:
        if cfg.get("grade_check_cover", True):
            failed_checks += 1
            cover_ok = False
            add_issue("Missing cover image", "album")
        if cfg.get("cover_enforce_size") and cfg.get("cover_resize_enabled"):
            try:
                tgt = _get_cover_target_size("", cfg)
            except Exception:
                tgt = 0
            if tgt > 0:
                cover_detail = f"MISSING (need {tgt}x{tgt})"
    else:
        cover_path = os.path.join(album_dir, cover_file)
        ext_cov = os.path.splitext(cover_file)[1].lower()
        target_cov = _get_cover_target_size(ext_cov, cfg)
        size_failed = False
        square_failed = False
        size_info = ""
        under_info = ""
        # Force exact: when cover_force_exact_size is on, it implies both size and square
        # must be exactly target×target, regardless of the separate enforce toggles.
        force_exact = bool(cfg.get("cover_force_exact_size", False))
        enforce_size = bool(cfg.get("cover_enforce_size", False))
        enforce_square = bool(cfg.get("cover_enforce_square", False))
        if force_exact and cfg.get("cover_resize_enabled", False) and target_cov > 0:
            enforce_size = True
            enforce_square = True
        # Cache dimensions once (handles JXL via jxlinfo) — and read them
        # whenever a reader is available, not only for the enforcement checks:
        # this is also what proves the file grade_check_cover just accepted is
        # an IMAGE. With resize/enforcement off nothing else opened it, so a
        # 0-byte or truncated cover.jpg satisfied the check (and silently
        # covered for a missing cover image).
        w = h = None
        cover_read_error = False
        if HAS_PIL or cover_path.lower().endswith(".jxl"):
            w, h = _get_cover_dimensions(cover_path)
            if w is None or h is None:
                cover_read_error = True
        # The enforcement passes below fail an unreadable cover themselves (the
        # size pass names it, the aspect-ratio pass names it), so this counts
        # its own check only when neither of them will run.
        if cover_read_error and cfg.get("grade_check_cover", True) and not (
            (enforce_size and cfg.get("cover_resize_enabled", False)
             and target_cov > 0)
            or (enforce_square and cfg.get("grade_check_cover_crop", True))
        ):
            total_checks += 1
            failed_checks += 1
            cover_ok = False
            add_issue("Cover image unreadable/corrupt", "album")
        # Size enforcement: require exact target_size x target_size (configurable tolerance)
        if enforce_size and cfg.get("cover_resize_enabled", False) and target_cov > 0 and cfg.get("grade_check_cover", True):
            total_checks += 1
            try:
                tol = int(cfg.get("grader_cover_size_tolerance_px", 0) or 0)
                tol = max(0, min(5, tol))
            except Exception:
                tol = 0
            if w is not None and h is not None:
                if w - target_cov > tol or h - target_cov > tol:
                    failed_checks += 1
                    size_failed = True
                    cover_ok = False
                    size_info = f"{w}x{h} → {target_cov}x{target_cov}"
                    add_issue(f"Cover image too large {w}x{h} (need {target_cov}x{target_cov})", "album")
                elif w < target_cov - tol or h < target_cov - tol:
                    # Undersized: the image pass never upscales, so the target
                    # is unreachable for this file and failing it would be
                    # permanent. Report it, do not fail it.
                    under_info = f"{w}x{h}, below the {target_cov}px target (never upscaled)"
            elif cover_read_error:
                failed_checks += 1
                size_failed = True
                cover_ok = False
                add_issue(f"Cover image unreadable/corrupt (need {target_cov}x{target_cov})", "album")
        # Aspect-ratio (squareness) enforcement — NOT crop detection: there
        # is no crop heuristic here, only |w/h - 1| <= threshold, so the
        # issue text says so. grade_check_cover_crop keeps gating it.
        # (force_exact uses the strict threshold from config)
        if enforce_square and cfg.get("grade_check_cover", True) and cfg.get("grade_check_cover_crop", True):
            if force_exact:
                try:
                    thr_cov = float(cfg.get("grader_strict_square_threshold", 0.0) or 0.0)
                    thr_cov = max(0.0, min(0.05, thr_cov))
                except (TypeError, ValueError):
                    thr_cov = 0.0
            else:
                try:
                    thr_cov = float(cfg.get("cover_crop_threshold", 0.0) or 0.0)
                except (TypeError, ValueError):
                    thr_cov = 0.0
                thr_cov = max(0.0, min(0.5, thr_cov))
            try:
                if w is not None and h is not None:
                    ratio = w / h if h else 1.0
                    if abs(ratio - 1.0) > thr_cov:
                        failed_checks += 1
                        square_failed = True
                        cover_ok = False
                        add_issue(f"Cover aspect ratio {w}x{h} not square "
                                  f"(threshold {thr_cov:.0%})", "album")
                        if not size_info:
                            size_info = f"{w}x{h} not square"
                elif cover_read_error:
                    failed_checks += 1
                    square_failed = True
                    cover_ok = False
                    add_issue("Cover image unreadable/corrupt (aspect ratio "
                              "unverifiable)", "album")
                # Counted once, at the end: an exception is counted by
                # unavailable() instead (never silently dropped, never double).
                total_checks += 1
            except Exception as e:
                unavailable("Cover aspect-ratio check", e)
        if size_failed or square_failed:
            if size_failed and square_failed:
                cover_detail = f"{cover_file} (wrong size, not square {size_info})"
            elif size_failed:
                cover_detail = f"{cover_file} (wrong size {size_info})"
            elif square_failed:
                cover_detail = f"{cover_file} (not square {size_info})"
            else:
                cover_detail = f"{cover_file} (needs resize)"
        elif cover_read_error:
            # Nothing could decode this file — say so instead of leaving the
            # detail looking like a healthy "cover.jpg".
            cover_detail = f"{cover_file} (unreadable)"
        elif under_info:
            cover_detail = f"{cover_file} ({under_info})"
        else:
            cover_detail = cover_file
        # ENCODER for cover image per-format (only when that field is enabled)
        # Gated like the audio check (grade_check_encoder) and on
        # image processing: script 5 is the only writer of those markers,
        # so with it off the check would fail an album nothing can fix.
        try:
            cov_ext = os.path.splitext(cover_file)[1].lower() if cover_file else ""
            cov_enc_key = None
            if cov_ext in (".jpg", ".jpeg"):
                cov_enc_key = "jpeg"
            elif cov_ext == ".png":
                cov_enc_key = "png"
            elif cov_ext == ".jxl":
                cov_enc_key = "jxl"
            if cov_enc_key and cover_file \
                    and cfg.get("grade_check_encoder", True) \
                    and cfg.get("reencode_images", True):
                cov_enc_cfg = (cfg.get("encoder_tags") or {}).get(cov_enc_key, {})
                for field in ("ENCODER_PROGRAM", "ENCODER_QUALITY", "ENCODER_VERSION"):
                    default_on = False if field == "ENCODER_PROGRAM" else True
                    if not cov_enc_cfg.get(field, default_on):
                        continue
                    # Check presence via appropriate reader
                    has_enc = False
                    try:
                        if cov_enc_key == "jpeg":
                            from .containers import _read_jpeg_xmp_tags
                            q, v, _ = _read_jpeg_xmp_tags(cover_path)
                            if field == "ENCODER_PROGRAM":
                                # Check XMP for program (parse raw)
                                try:
                                    with open(cover_path, "rb") as f:
                                        has_enc = b"ENCODER_PROGRAM" in f.read()
                                except Exception:
                                    has_enc = False
                            elif field == "ENCODER_QUALITY":
                                has_enc = q is not None and str(q).strip() != ""
                            else:  # VERSION
                                has_enc = v is not None and str(v).strip() != ""
                        elif cov_enc_key == "png":
                            from .containers import _read_png_text
                            txt = _read_png_text(cover_path)
                            has_enc = field in txt and str(txt[field]).strip() != ""
                        elif cov_enc_key == "jxl":
                            from .containers import _read_jxl_tags
                            q, v, _ = _read_jxl_tags(cover_path)
                            if field == "ENCODER_PROGRAM":
                                try:
                                    with open(cover_path, "rb") as f:
                                        has_enc = b"ENCODER_PROGRAM" in f.read()
                                except Exception:
                                    has_enc = False
                            elif field == "ENCODER_QUALITY":
                                has_enc = q is not None
                            else:
                                has_enc = v is not None
                    except Exception:
                        has_enc = False
                    total_checks += 1
                    if not has_enc:
                        failed_checks += 1
                        add_issue(f"Cover missing {field} (re-optimize)", "album")
                        cover_ok = False
        except Exception as e:
            # The cover's ENCODER_* markers are a graded check like the audio
            # tags' (grade_check_encoder) — a failure to read them must cost
            # the album the check instead of vanishing into a bare `pass`.
            unavailable("Cover encoder identity check", e)
    # Cover failure makes every track fail as well (per request: track/album fail)
    if not cover_ok:
        for tr in tracks:
            if "COVER" not in tr["issues"]:
                tr["issues"].append("COVER")

    # CUE sheet FORMATTING compliance (when a cue exists): every cue must
    # already be in the canonical form the CUE formatter would produce.
    cue_files = sorted(
        os.path.join(album_dir, f) for f in all_files
        if f.lower().endswith(".cue")
    )
    if has_cue and cfg.get("grade_check_cue_format", True):
        total_checks += 1
        cue_ok = True
        for cue_path in cue_files:
            if not _cue_formatted(cue_path, cfg):
                cue_ok = False
                break
        if not cue_ok:
            failed_checks += 1
            add_issue("CUE sheet not optimally formatted "
                      "(run CUE Sheets script)", "album")

    # ...and its FILE lines must name files this album actually holds. The
    # formatter carries the name through verbatim, so a converted (wav→flac)
    # or renamed album used to keep a sheet pointing at a file that is not
    # there — playable nowhere, invisible to every other check.
    if has_cue and cfg.get("grade_check_cue_files", True):
        from .discs import cue_file_refs
        # Both sides through the shared name rule (naming.name_key, which
        # cue_ref_names applies): a sheet that still names a file the way the
        # rip wrote it ("01. AC/DC - Theme.flac") refers to the file this app
        # WROTE for it ("01. AC_DC - Theme.flac"). Without that the app's own
        # renaming reads as a missing file and fails the album for it.
        on_disk = {name_key(f).lower() for f in all_files}
        missing = []
        for cue_path in cue_files:
            for ref in cue_file_refs(cue_path):
                names = cue_ref_names(ref)
                if names and not any(n.lower() in on_disk for n in names):
                    missing.append(f"{os.path.basename(cue_path)}: {ref}")
        total_checks += 1
        if missing:
            failed_checks += 1
            add_issue("CUE references a file the album does not have "
                      f"({', '.join(missing[:3])}"
                      f"{', …' if len(missing) > 3 else ''}) — run CUE Sheets",
                      "album")

    # .accurip FORMATTING compliance (when .accurip exists): trim each line, outer blanks only
    # Per user: delete leading/trailing spaces per line, only outer blanks. Counts towards grading.
    if cfg.get("grade_check_accurip_format", True) and any(
            f.lower().endswith(".accurip") for f in all_files):
        # Only check formatting if the file is a valid CUETools log; synthetic files already fail via sidecar check
        # Gated like every other graded check (grade_check_accurip_format):
        # it used to run whenever an .accurip existed, so no toggle could
        # stop it counting against the album.
        total_checks += 1
        accum_ok = True
        for f in all_files:
            if not f.lower().endswith(".accurip"):
                continue
            full = os.path.join(album_dir, f)
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    txt = fh.read()
                from mlo.accurip import _canonical_accurip_text
                append = bool(cfg.get("append_final_newline", False))
                keep_empty = bool(cfg.get("keep_empty_accurip_lines", False))
                canonical = _canonical_accurip_text(txt, keep_empty_lines=keep_empty, append_final_newline=append)
                if txt != canonical:
                    accum_ok = False
                    break
            except OSError:
                accum_ok = False
                break
        if not accum_ok:
            failed_checks += 1
            add_issue(".accurip not optimally formatted (run Format All or Generate AccurateRip)", "album")

    # Strict file-type check: any file whose category is not allowed
    # (e.g. an unclassified .txt/.pdf/.m3u when 'other' is off) fails the
    # album. Categories are toggled in Settings -> Grading.
    if cfg.get("grade_check_disallowed", True):
        disallowed = _disallowed_files(album_dir, all_files, cfg)
        total_checks += 1
        if disallowed:
            failed_checks += 1
            shown = ", ".join(disallowed[:6])
            if len(disallowed) > 6:
                shown += f" (+{len(disallowed) - 6} more)"
            add_issue(f"Disallowed file types: {shown}", "album")

    # Extra artwork check: images that are neither the album cover (cover.*)
    # nor a per-track sidecar ("01 - Song.jpg") are strays — they must move
    # with the album (organize sweeps them to the album root) or be removed.
    if cfg.get("grade_check_extra_images", True):
        extra_imgs = _extra_images(album_dir, all_files, files)
        total_checks += 1
        if extra_imgs:
            failed_checks += 1
            shown = ", ".join(extra_imgs[:4])
            if len(extra_imgs) > 4:
                shown += f" (+{len(extra_imgs) - 4} more)"
            add_issue(f"Extra artwork not tied to any track: {shown}", "album")

    # Album description (the album page's "fetch description" feature):
    # <album>/description.txt must exist and be non-blank. Lazy import — the
    # grader is imported from contexts that must not pull in the image stack.
    if cfg.get("grade_check_album_description", True):
        from .artistdata import has_description as _has_description
        total_checks += 1
        if not _has_description(album_dir):
            failed_checks += 1
            add_issue("Album description missing — fetch one on the album page",
                      "album")

    # File extensions must be lowercase ("01 - Song.FLAC" fails). organize
    # lowercases every extension it touches.
    if cfg.get("grade_check_ext_case", True):
        bad_ext = [f for f in sorted(all_files)
                   if os.path.splitext(f)[1] != os.path.splitext(f)[1].lower()]
        total_checks += 1
        if bad_ext:
            failed_checks += 1
            shown = ", ".join(bad_ext[:6])
            if len(bad_ext) > 6:
                shown += f" (+{len(bad_ext) - 6} more)"
            add_issue(f"File extension not lowercase: {shown}", "album")

    # Raw, un-remuxed video files (VOB/AVI/WMV/TS/...) fail grading — script
    # 11 normalizes them to MKV with every stream copied bit-exact. MKV is
    # the remux TARGET and MP4/M4V play and tag natively (script 11 leaves
    # them alone unless video_process_mp4 is on), so neither is flagged.
    if cfg.get("grade_check_raw_video", True):
        try:
            from .remux import REMUX_GATED_EXTS, VIDEO_EXTS as _ALL_VIDEO_EXTS
            _RAW_VIDEO_EXTS = tuple(
                e for e in _ALL_VIDEO_EXTS
                if e != ".mkv" and e not in REMUX_GATED_EXTS)
        except Exception:
            _RAW_VIDEO_EXTS = ()
        raw_videos = [f for f in sorted(all_files)
                      if _RAW_VIDEO_EXTS and f.lower().endswith(_RAW_VIDEO_EXTS)]
        total_checks += 1
        if raw_videos:
            failed_checks += 1
            shown = ", ".join(raw_videos[:4])
            if len(raw_videos) > 4:
                shown += f" (+{len(raw_videos) - 4} more)"
            add_issue(f"Un-remuxed video file(s): {shown} (run Remux)", "album")

    # Lossless but uncompressed sources (WAV/AIFF/APE/WV/SHN) fail grading —
    # script 3 converts them to the library codec target. The check is skipped
    # when that pass will never touch them: the target IS one of those
    # containers (a WAV/AIFF library keeps them by design) or the user chose
    # to keep the library as it is. Failing those albums would be a permanent
    # verdict about a decision, not a defect, and nothing could ever fix it.
    _src_target = _uncompressed_source_target(cfg)
    if cfg.get("grade_check_lossless_source", True) and _src_target:
        try:
            from .flac import LOSSLESS_SOURCE_EXTS as _LOSSLESS_SRC_EXTS
        except Exception:
            _LOSSLESS_SRC_EXTS = ()
        uncompressed = [f for f in sorted(all_files)
                        if _LOSSLESS_SRC_EXTS and f.lower().endswith(_LOSSLESS_SRC_EXTS)]
        total_checks += 1
        if uncompressed:
            failed_checks += 1
            shown = ", ".join(uncompressed[:4])
            if len(uncompressed) > 4:
                shown += f" (+{len(uncompressed) - 4} more)"
            add_issue(f"Uncompressed lossless file(s): {shown} "
                      f"(script 3 converts them to {_src_target})", "album")

    # Every album must carry the MusicBrainz release's own tracklist manifest
    # (.mlo_expected.json, written at import and by script 15). The files on
    # disk only describe themselves, so without the manifest a PARTIAL import
    # (3 tracks of 12) is indistinguishable from a complete album and grades
    # PASS. An album that HAS the manifest is never failed by this check —
    # its tracklist is diffed against the files as before (see
    # server.library._add_expected_tracks → `expected_tracks` / `partial`).
    # Only an album carrying a MusicBrainz release id is graded on it: script
    # 15 writes NO manifest without one (it will not fabricate a tracklist for
    # a release it cannot name), so requiring one there failed the album with
    # nothing the user could run to clear it — the manifest is unattainable by
    # design, and the missing release link is what that album is graded on.
    if cfg.get("grade_check_expected_tracks", True) and album_has_mbid:
        total_checks += 1
        if not load_expected_tracks(album_dir)["tracks"]:
            failed_checks += 1
            add_issue(EXPECTED_TRACKS_MISSING, "album")

    pass_count = max(0, total_checks - failed_checks)

    # Ensure viewer columns have values even for non-CD albums
    try:
        if "checksum_status" not in locals():
            checksum_status = "NONE"
        if "accuraterip_status" not in locals():
            accuraterip_status = "NONE"
        # For non-CD, ensure per-track values exist
        if not _is_cd(media_summary):
            for tr in tracks:
                if "checksum_status" not in tr:
                    tr["checksum_status"] = "NONE"
                if "accuraterip_status" not in tr:
                    tr["accuraterip_status"] = "NONE"
    except Exception:
        checksum_status = "NONE"
        accuraterip_status = "NONE"

    # Per-file grades for the non-audio files shown in the viewer (only
    # computed when the viewer toggle is enabled, to avoid extra I/O).
    sidecars = []
    if cfg.get("show_sidecar_files", False):
        sidecars = _grade_sidecars(album_dir, all_files, cfg)

    # Album-level audit summary — REAL-TIME (not just stored tags).
    # Per user: app must calculate AUDIT from current factors, not just read AUDIT tag.
    # If .accurip is missing or FAKE, AUDIT is FAKE immediately, even if tag still says REAL.
    # This covers the "removed .accurip file still says REAL" bug.
    def _realtime_audit_for_album():
        # Per-track lightweight checks — only that track's AUDIT becomes FAKE, but for album summary we still return FAKE if any track fails
        # This fixes the bug where one bad track's checksum/accurip made whole album's tracks show FAKE via uniform status; now per-track is accurate,
        # but album summary remains FAKE if any track is FAKE (so folder still indicates issue, but per-track column shows which track)
        # Music-video tracks are never CD-DA — CD-only audit checks exclude them.
        cd_tracks = [tr for tr in tracks if not _is_video_file(tr.get("file"))]
        try:
            if _is_cd(media_summary) and cfg.get("audit_require_accuraterip", True) and cfg.get("grade_check_accuraterip", True):
                # Per-track AccurateRip: a NONE/FAKE track makes the album
                # FAKE — unless that track's rip is verified by its own .log
                # CRC, which stands on its own evidence.
                for tr in cd_tracks:
                    if tr.get("checksum_status") == "REAL":
                        continue
                    if tr.get("accuraterip_status") in ("NONE", "FAKE"):
                        return "FAKE"
        except Exception:
            pass
        # Check log checksum per-track (per-disc)
        try:
            if _is_cd(media_summary) and cfg.get("audit_verify_log_checksum", True) and cfg.get("grade_check_log_checksum", True):
                for tr in tracks:
                    if tr.get("checksum_status") == "FAKE":
                        return "FAKE"
        except Exception:
            pass
        # Check CD format (lightweight) — per-track already
        try:
            if _is_cd(media_summary) and cfg.get("audit_check_cd_format", True):
                # CD must be 16/44.1 — if any track failed CD_FORMAT grading, audit is FAKE
                for tr in tracks:
                    if "CD_FORMAT" in tr.get("issues", []):
                        return "FAKE"
        except Exception:
            pass
        # Fall back to stored AUDIT tags (covers AudioAuditor, integrity, etc.) — per-track
        # If any track's stored AUDIT is FAKE, album is FAKE
        tag_summary = summarize_audits(tr["audit"] for tr in tracks)
        if tag_summary == "FAKE":
            return "FAKE"
        # For REAL, require all per-track checks to be REAL as well (not just album aggregate)
        try:
            # A track verified by EITHER source (its .log CRC or its
            # .accurip) counts as accurately ripped for the album verdict.
            all_ar_real = all(
                tr.get("accuraterip_status") == "REAL"
                or tr.get("checksum_status") == "REAL"
                for tr in cd_tracks
            ) if _is_cd(media_summary) and cfg.get("audit_require_accuraterip", True) and cfg.get("grade_check_accuraterip", True) else True
            all_csum_ok = all(tr.get("checksum_status") != "FAKE" for tr in tracks) if _is_cd(media_summary) and cfg.get("audit_verify_log_checksum", True) and cfg.get("grade_check_log_checksum", True) else True
        except Exception:
            all_ar_real = accuraterip_status == "REAL"
            all_csum_ok = checksum_status != "FAKE"
        if tag_summary == "REAL" and all_ar_real and all_csum_ok:
            return "REAL"
        # If no AUDIT tag yet, but lightweight checks passed, consider REAL for display
        # (audit hasn't been run, but files would pass)
        if tag_summary is None and all_ar_real and all_csum_ok:
            # Need to ensure accuraterip is REAL when required
            if _is_cd(media_summary) and cfg.get("audit_require_accuraterip", True) and cfg.get("grade_check_accuraterip", True):
                if all_ar_real:
                    return "REAL"
            else:
                return "REAL"
        if tag_summary is None and _is_cd(media_summary):
            # No accurip and no tag → treat as not yet audited, but per user should be FAKE
            # Only when audit_require_accuraterip is on and any track is NONE
            if cfg.get("audit_require_accuraterip", True) and cfg.get("grade_check_accuraterip", True):
                for tr in cd_tracks:
                    if tr.get("accuraterip_status") == "NONE":
                        return "FAKE"
        return tag_summary

    audit_summary = _realtime_audit_for_album()

    return {
        "path": album_dir,
        "album_artist": album_artist,
        "audit_summary": audit_summary,
        "media": media_summary or "(unknown)",
        "source_summary": _summarize_values(source_values),
        "track_count": len(audio_paths),
        "pass_count": pass_count,
        "total_checks": total_checks,
        "cover_file": cover_file,
        "cover_detail": cover_detail,
        "cover_ok": cover_ok,
        "has_log": has_log,
        "has_cue": has_cue,
        "checksum_status": checksum_status if 'checksum_status' in locals() else "NONE",
        "accuraterip_status": accuraterip_status if 'accuraterip_status' in locals() else "NONE",
        "lyrics_present": lyrics_present_count,
        "lyrics_expected": lyrics_expected_count,
        "instrumental_count": instrumental_count,
        "tracks": tracks,
        "sidecars": sidecars,
        "album_values": {
            t: _summarize_values(album_tag_values.get(t, set()))
            for t in ALBUM_TAGS
        },
        "issues": {k: sorted(v, key=str.lower) for k, v in issues.items()},
        "notes": sorted(set(notes), key=str.lower),
    }


# Artist-level checks (the artist folder, not an album): the image and the
# text the artist page fetches into the library. Order is display order; the
# labels are what the UI shows for each row.
ARTIST_CHECKS = [
    {"key": "grade_check_artist_image", "label": "Artist image",
     "description": "The artist folder must hold an artist.jpg / artist.png "
                    "whose size, aspect and format match the configured "
                    "artist-image policy."},
    {"key": "grade_check_artist_description", "label": "Artist description",
     "description": "The artist folder must hold a non-blank description.txt."},
]

# Issue codes per artist check, and the artwork key each one reports on. The
# image check raises several: its size, its shape, its container and its
# decodability are four different verdicts on one file, and each one names the
# numbers it judged. ARTIST_IMAGE_UNDERSIZED is the informational one — the
# result carries it in `notes`, never in `issues`, because nothing here upscales
# and failing an image below the target would fail the folder forever.
_ARTIST_CHECK_ISSUES = {
    "grade_check_artist_image": (("ARTIST_IMAGE_MISSING", "ARTIST_IMAGE_CORRUPT",
                                  "ARTIST_IMAGE_FORMAT", "ARTIST_IMAGE_OVERSIZED",
                                  "ARTIST_IMAGE_ASPECT", "ARTIST_IMAGE_UPSCALED",
                                  "ARTIST_IMAGE_UNDERSIZED"), "image"),
    "grade_check_artist_description": (("ARTIST_DESCRIPTION_MISSING",),
                                       "description"),
}


def _artist_image_issues(folder, image_file, cfg, where):
    """(issues, notes) for an artist folder's image.

    Judged on the decoded file — Pillow reads the pixels, never the name or the
    suffix — and every reason names the numbers behind it. An image this app
    wrote is also compared with the size it recorded writing it: anything larger
    on disk had pixels invented for it afterwards, which is worth saying because
    a resized-up photo looks detailed at a glance.

    The policy (aspect, tolerance, target, ceiling) comes from mlo.artistdata, so
    this check and script 19, which fixes what it reports, cannot judge by
    different numbers."""
    from .artistdata import (ARTIST_IMAGE_EXTS, DEFAULT_ASPECT,
                             aspect_deviation, decode_size, image_policy,
                             recorded_size, unsupported_image)

    label = "Artist image"

    def issue(code, reason):
        return {"code": code, "label": label, "where": where, "reason": reason}

    if not image_file:
        other = unsupported_image(folder)
        if other:
            exts = " / ".join(ARTIST_IMAGE_EXTS)
            return ([issue("ARTIST_IMAGE_FORMAT",
                           f"{os.path.basename(other)} is not an artist image "
                           f"this app reads ({exts}) — script 19 converts it")], [])
        return ([issue("ARTIST_IMAGE_MISSING",
                       "no artist.jpg / artist.png in the folder — fetch one from "
                       "the artist page (script 19 can only re-fit an image that "
                       "is there)")], [])

    name = os.path.basename(image_file)
    size = decode_size(image_file)
    if size is None:
        try:
            nbytes = os.path.getsize(image_file)
        except OSError:
            nbytes = 0
        return ([issue("ARTIST_IMAGE_CORRUPT",
                       f"{name} does not decode ({nbytes} bytes) — re-fetch it; "
                       f"script 19 cannot repair bytes that are not an image")], [])

    width, height = size
    longest = max(width, height)
    aspect, tolerance, target, max_side = image_policy(cfg)
    issues, notes = [], []

    if longest > max_side:
        expected = (f"the configured artist_image_target_size {target}px"
                    if target > 0 else
                    f"the {max_side}px artist-image ceiling "
                    f"(artist_image_target_size is 0 = keep the native size)")
        issues.append(issue(
            "ARTIST_IMAGE_OVERSIZED",
            f"{width}x{height}: longest side {longest}px, {longest - max_side}px "
            f"over {expected} (script 19 downscales it)"))
    elif target > 0 and longest < target:
        notes.append(issue(
            "ARTIST_IMAGE_UNDERSIZED",
            f"{width}x{height} is below the configured {target}px target "
            f"({target - longest}px short) — accepted, and never upscaled: an "
            f"enlarged photo would be invented detail"))

    if aspect:
        deviation = aspect_deviation(size, aspect)
        if deviation > tolerance:
            configured = str(cfg.get("artist_image_aspect") or DEFAULT_ASPECT)
            issues.append(issue(
                "ARTIST_IMAGE_ASPECT",
                f"{width}x{height} is {width / float(height):.3f}:1, not the "
                f"configured {configured} ({aspect:.3f}:1) — {deviation:.1%} off, "
                f"tolerance {tolerance:.0%} — script 19 crops it to {configured}"))

    recorded = recorded_size(folder, cfg)
    if recorded and longest > max(recorded):
        issues.append(issue(
            "ARTIST_IMAGE_UPSCALED",
            f"{width}x{height} on disk, {recorded[0]}x{recorded[1]} when this app "
            f"wrote it — {longest / float(max(recorded)):.2f}x larger, so its "
            f"extra detail is interpolated or came from outside the app (script 19 "
            f"restores the stored size)"))

    return issues, notes


def grade_artist(artist_dir, cfg=None) -> dict:
    """Grade an artist folder on the things that apply to it: its image, its
    description (ARTIST_CHECKS), and that it holds an album at all. Album-level
    checks — tags, logs, covers — never run here.

    The image check is judged on the decoded file (the configured aspect and
    size, the format, decodability, and whether the pixels were enlarged after
    this app wrote them); *issues* fail it, *notes* inform without failing —
    an undersized image is perfectly acceptable here. Every issue carries a
    `reason` naming the numbers behind it.

    *pct* is 100 with both checks disabled: nothing graded is nothing failed,
    so *pass* is True there (the album rule reports the same 100% for an album
    whose checks are all switched off). Two folder-level verdicts never go
    through that arithmetic — an unreadable/absent folder
    (ARTIST_FOLDER_MISSING) and a folder holding no album at all
    (ARTIST_EMPTY) each report one issue and invent no checks, because neither
    is a folder this app can grade whatever the settings say.
    """
    cfg = cfg or {}
    folder = str(artist_dir or "")
    where = os.path.basename(folder.replace("\\", "/").rstrip("/")) or folder
    out = {
        "path": folder,
        "checks": 0,
        "pass_count": 0,
        "failed_checks": 0,
        "pct": 0.0,
        "pass": False,
        "issues": [],
        "notes": [],
        "artwork": {"image": False, "image_file": None, "description": False},
    }
    if not folder or not os.path.isdir(folder):
        out["issues"].append({
            "code": "ARTIST_FOLDER_MISSING", "label": "Artist folder",
            "where": where,
        })
        return out

    from .artistdata import has_description, image_path
    # The album-folder question is mlo.layout's (the scanner reports the same
    # folder as `empty_artist`), so the grade and the scan answer it alike.
    from .layout import artist_album_folders
    image_file = image_path(folder)
    out["artwork"] = {
        "image": bool(image_file),
        "image_file": image_file,
        "description": has_description(folder),
    }

    # An artist folder holding no album folder at all is not a graded artist:
    # its image and description are the artist's own cover, nothing under it
    # can be graded as music, and the library still lists it as an artist. It
    # fails the way an absent folder does — one issue, no checks invented —
    # because the artefact checks describe a folder that can hold an album, and
    # reporting image/description grades for one that cannot would be a score
    # for the wrong question. `mlo.layout` reports the same folder as
    # `empty_artist`, and the removal the panel offers goes through the Trash.
    if not artist_album_folders(folder):
        out["issues"].append({
            "code": "ARTIST_EMPTY", "label": "Artist albums", "where": where,
            "reason": "no album folder in this artist folder — nothing here is "
                      "an album, so there is nothing to grade as music. Add "
                      "one of the artist's albums, or remove the folder to the "
                      "Trash (Optimize → Library layout → remove)",
        })
        return out

    for check in ARTIST_CHECKS:
        if not cfg.get(check["key"], True):
            # Toggle off: the artefact is neither required nor counted.
            continue
        out["checks"] += 1
        codes, art_key = _ARTIST_CHECK_ISSUES[check["key"]]
        if check["key"] == "grade_check_artist_image":
            found, notes = _artist_image_issues(folder, image_file, cfg, where)
            out["notes"].extend(notes)
        elif out["artwork"][art_key]:
            found = []
        else:
            found = [{"code": codes[0], "label": check["label"], "where": where,
                      "reason": "no description.txt (or it is blank)"}]
        if found:
            out["failed_checks"] += 1
            out["issues"].extend(found)

    out["pass_count"] = out["checks"] - out["failed_checks"]
    # Nothing graded is nothing failed, exactly like an album with every check
    # switched off (format_grade_report reads it as 100%): a 0 here contradicted
    # that and showed a passing artist folder as "0%".
    out["pct"] = (100.0 if not out["checks"]
                  else 100.0 * out["pass_count"] / out["checks"])
    out["pass"] = out["failed_checks"] == 0
    return out


def format_grade_report(res, lyrics_format, track_file=None):
    """
    Build [(text, style), ...] lines for a grade result, for the GUI
    grade-details dialog. Styles: None, "bold", "red", "green", "muted".
    Pass track_file to limit the report to a single track.
    """
    lines = []

    if "error" in res:
        lines.append((f"Error grading: {res.get('path')}", "red"))
        return lines

    ok = res["pass_count"] == res["total_checks"]
    failed = res["total_checks"] - res["pass_count"]

    lines.append((
        f"Grade: {'PASS' if ok else 'FAIL'} ({100.0 if ok else 0.0:.0f}%) | "
        f"Checks: {res['pass_count']}/{res['total_checks']} | "
        f"Failed: {failed} | Tracks: {res['track_count']}",
        "green" if ok else "red",
    ))
    lines.append((
        f"Media: {res['media']} | "
        f"Source: {res['source_summary'] or 'MISSING'} | "
        f"Cover: {res['cover_file'] or 'MISSING'} | "
        f"Log: {'yes' if res['has_log'] else 'no'} | "
        f"Cue: {'yes' if res['has_cue'] else 'no'}",
        None,
    ))

    if res.get("audit_summary"):
        audit = res["audit_summary"]
        lines.append((
            f"Audio audit: {audit}",
            "green" if audit == "REAL"
            else ("red" if audit in ("FAKE", "Mix") else None),
        ))
    if res.get("media") == "CD":
        grades = sorted({
            tr.get("log_grade") for tr in res["tracks"]
            if tr.get("log_grade")})
        lines.append((
            "Rip-log grades (LOG_GRADE): "
            + (" ".join(f"{g}/100" for g in grades) if grades
               else "MISSING"),
            "green" if grades else "red",
        ))

    album_tag_parts = []
    for t in ALBUM_TAGS:
        val = res["album_values"].get(t)
        album_tag_parts.append(f"{t}={val if val else 'MISSING'}")
    lines.append(("Album tags: " + " | ".join(album_tag_parts), None))

    lines.append((
        f"Lyrics: required {str(lyrics_format).upper()}; "
        f"present {res['lyrics_present']}/{res['lyrics_expected']}; "
        f"instrumental {res['instrumental_count']}",
        None,
    ))

    if res["issues"]:
        for field, where in sorted(res["issues"].items()):
            if len(where) == 1 and where[0] in ("album", "album-wide"):
                lines.append((f"  - {field}", "red"))
            elif len(where) <= 5:
                lines.append((f"  - {field}: {', '.join(where)}", "red"))
            else:
                preview = ", ".join(where[:5])
                lines.append((f"  - {field}: {preview}, +{len(where) - 5} more", "red"))
    else:
        lines.append(("  - no problems", "green"))

    lines.append(("Tracks:", "bold"))

    for i, tr in enumerate(res["tracks"], 1):
        if track_file and os.path.join(res["path"], tr["file"]) != track_file:
            continue

        v = tr["values"]
        lyr = []
        if tr["lyrics_embedded"]:
            lyr.append("EMB")
        if tr["lyrics_lrc"]:
            lyr.append("LRC")
        lyr_state = "+".join(lyr) if lyr else "NONE"

        lines.append((f"  {i:02d}. {tr['file']}", "bold"))
        lines.append((
            f"      GENRE={_short_val(v.get('GENRE'), 18)} | "
            f"MOOD={_short_val(v.get('MOOD'), 12)} | "
            f"ADVISORY={_short_val(v.get('ITUNESADVISORY'), 8)} | "
            f"DR={_short_val(v.get('DYNAMIC RANGE'), 6)} | "
            f"INST={_short_val(v.get('INSTRUMENTAL'), 4)}",
            None,
        ))
        lines.append((
            f"      RG_TRACK={_short_val(v.get('REPLAYGAIN_TRACK_GAIN'), 10)} / "
            f"{_short_val(v.get('REPLAYGAIN_TRACK_PEAK'), 8)} | "
            f"RG_ALBUM={_short_val(v.get('REPLAYGAIN_ALBUM_GAIN'), 10)} / "
            f"{_short_val(v.get('REPLAYGAIN_ALBUM_PEAK'), 8)}",
            None,
        ))
        lines.append((
            f"      MEDIA={_short_val(v.get('MEDIA'), 14)} | "
            f"SOURCE={_short_val(v.get('SOURCE'), 14)} | "
            f"LYRICS={lyr_state} | "
            f"AUDIT={tr.get('audit') or '—'} | "
            f"LOG_GRADE={tr.get('log_grade') or '—'}",
            None,
        ))

        if tr["issues"]:
            lines.append((f"      Issues: {', '.join(tr['issues'])}", "red"))
        elif track_file:
            lines.append(("      No issues", "green"))

        if track_file:
            break

    return lines


def _relpath_guard(path, base):
    """os.path.relpath that never raises on cross-drive paths (Windows)."""
    try:
        return os.path.relpath(path, base)
    except ValueError:
        return os.path.basename(path)


# Issue code for a library folder that holds nothing beneath it at all. The
# label the UI shows for it is "Empty folder"; the row's `where` is the
# folder's path relative to the music folder.
EMPTY_FOLDER = "EMPTY_FOLDER"

# Issue code for an album folder with no .mlo_expected.json: the release's own
# tracklist is missing, so nothing can say whether the album is complete.
# Album-wide like the other album checks (see grade_check_expected_tracks).
EXPECTED_TRACKS_MISSING = "EXPECTED_TRACKS_MISSING"


def _find_empty_folders(root, dirs_out):
    """Directories under *root* that hold no audio track anywhere beneath them.

    Albums are derived from audio files (_find_albums), so such a folder never
    becomes one and would otherwise be invisible to grading. The scan is the
    album walk's OWN directory scan (*dirs_out*, filled by _walk_files), so the
    library is walked once and not twice; hidden dirs and SKIP_DIRS (the app's
    own state, .git, the recycle bin) are pruned by that walk, and the music
    folder root itself is never reported — an empty music folder is not a
    folder that needs cleaning up.

    A folder that still holds part of the album — the cover slot or one of the
    sidecars the scripts write next to the audio — is reported too: its audio
    is gone, which is a broken album rather than a folder to leave alone. A
    folder holding only unrelated files (a Scans/*.jpg subfolder) is nobody's
    album and stays out of the report.
    """
    has_audio = dict(dirs_out)
    # Deepest first: one file makes its whole chain of ancestors non-empty, and
    # a folder holding audio stays reported-free whatever its children hold.
    for dirpath in sorted(has_audio, key=len, reverse=True):
        v = has_audio[dirpath]
        if v > WALK_EMPTY:
            parent = os.path.dirname(dirpath)
            if parent in has_audio and has_audio[parent] < v:
                has_audio[parent] = v
    root_norm = os.path.normpath(root)
    out = []
    for d, v in has_audio.items():
        if v == WALK_MATCHED or os.path.normpath(d) == root_norm:
            continue
        if any(part.startswith(".")
               for part in os.path.relpath(d, root).split(os.sep)):
            # The album walk itself does not prune a dot-dir, but reporting the
            # app's own hidden folders as empty albums would bury the real ones.
            continue
        if load_pending(d):
            # A framework album: the folder "Add to library" created before its
            # audio arrived. It is deliberately audio-less and the library scan
            # lists it as pending, so it is not a broken album — failing it here
            # would fail the very album the import is about to fill.
            continue
        if v == WALK_FILES:
            # Files, but none of them audio: only a folder still holding part
            # of the album (the cover slot or a rip/lyrics sidecar) is a broken
            # album — a Scans/*.jpg subfolder or a stray notes.txt is nobody's
            # album and is left alone.
            try:
                names = os.listdir(d)
            except OSError:
                continue
            if not any(_is_album_marker(n) for n in names):
                continue
        out.append(d)
    return sorted(out)


def _is_album_marker(name):
    """A file that says this folder was an album: the cover slot itself or one
    of the rip/lyrics sidecars the scripts write next to the audio."""
    low = name.lower()
    return low in COVER_NAMES or low.endswith(
        (".cue", ".log", ".lrc", ".accurip"))


def _empty_folder_result(folder, folder_root):
    """Grade-style row for an empty folder: one failed check, zero tracks.

    Same shape as _grade_album's dict so the run's summary loop totals it like
    any other row — that is what makes it count against grading.
    """
    rel = _relpath_guard(folder, folder_root)
    return {
        "path": folder,
        "where": rel,
        "album_artist": "",
        "audit_summary": "",
        "media": "",
        "source_summary": "",
        "track_count": 0,
        "pass_count": 0,
        "total_checks": 1,
        "cover_file": "",
        "cover_detail": "",
        "cover_ok": False,
        "has_log": False,
        "has_cue": False,
        "checksum_status": "NONE",
        "accuraterip_status": "NONE",
        "lyrics_present": 0,
        "lyrics_expected": 0,
        "instrumental_count": 0,
        "tracks": [],
        "sidecars": [],
        "album_values": {t: "" for t in ALBUM_TAGS},
        "issues": {EMPTY_FOLDER: [rel]},
        # Same shape as a graded album: a reader of `notes` never has to
        # treat "absent" as a case of its own.
        "notes": [],
    }


def printed_pct(pass_count, total_checks):
    """The percentage a surface may PRINT for one score. None with no checks.

    ONE rule for every surface that prints one (the library header, an album
    row, the grading strip), because they sit beside each other: a library of
    10 281 checks with one failed is 99.99 %, which `round(..., 1)` turns into
    `100.0` — a row that says Fail beside a number that says everything passed,
    and the strip that names the failing album calls the same library 100 %
    perfect. 100 is printed only by a library with nothing failed; a shortfall
    prints 99.9, which is all the precision a percentage is read at.
    """
    total = int(total_checks or 0)
    if not total:
        return None
    passed = int(pass_count or 0)
    pct = round(100.0 * passed / total, 1)
    return 99.9 if passed < total and pct >= 100.0 else pct


def run_grade_library(config):
    folder = config["music_folder"]
    lyrics_format = config.get("lyrics_format", "EMBEDDED").upper()
    verbose = config.get("grade_verbose", True)

    stats = new_stats()
    stats["is_grader"] = True
    stats["grade_dist"] = {"PASS": 0, "FAIL": 0}

    print_header("Library Grader")
    log(f"music folder: {folder} · lyrics format: {lyrics_format}")
    log(
        f"criteria: per-track {', '.join(PER_TRACK_TAGS)} | "
        f"album {', '.join(ALBUM_TAGS)} | media/source rule | "
        f"CD log+cue | cover jpg/jpeg/png/jxl | "
        f"INST=1 no lyrics | INST=0 lyrics required"
    )
    try:
        _th = int(config.get("grade_log_score_threshold", 100) or 0)
        if _th > 0:
            log(f"  CD log threshold: {_th}/100 (via Logchecker) — LOG_GRADE < {_th} fails grading")
    except Exception:
        pass

    if not os.path.isdir(folder):
        log(c(f"ERROR: folder does not exist: {folder}", Color.RED))
        return stats

    if config.get("targets") is not None:
        # Targeted run: derive albums from the explicit targets only — do
        # NOT walk the whole library first (costly on large trees).
        target_files = _collect_targets(config["targets"], AUDIO_EXTS)
        albums = sorted({os.path.dirname(f) for f in target_files})
        # No library-wide walk on a targeted run, so no empty-folder sweep
        # either: the user graded a selection, not the tree.
        empty_folders = []
    else:
        # One walk feeds both: _find_albums fills the directory scan that the
        # empty-folder sweep reads, so a grade no longer walks the library
        # twice. The folders with no audio below them never become albums and
        # grading would skip them silently — collected before the no-albums
        # exit so a library of nothing but empty folders still reports them.
        dir_scan = {}
        albums = _find_albums(folder, dir_scan)
        empty_folders = (_find_empty_folders(folder, dir_scan)
                         if config.get("grade_check_empty_folders", True) else [])

    if not albums and not empty_folders:
        log("No albums found.")
        return stats

    results = [_empty_folder_result(d, folder) for d in empty_folders]
    # Each empty folder is one graded row: keep "graded N" in step with
    # grade_dist, otherwise the failure rate can exceed 100%.
    stats["total_scanned"] += len(empty_folders)
    counts = {"ok": 0, "skip": 0, "fail": 0}
    workers = worker_count(config, default=16, maximum=16, items=len(albums))
    # A caller's own sink for what this run graded (spec R155). It is a PRIVATE
    # key the caller puts on the very config it hands in — the import does, so
    # its gap report can reuse the grade this step just paid for instead of
    # grading the same album again — and it exists only when a caller asked:
    # with no sink nothing here changes, and no run's own payload grows.
    sink = config.get("_grade_sink")
    if not isinstance(sink, dict):
        sink = None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_grade_album, a, lyrics_format, config): a
                   for a in albums}
        pbar = _make_pbar(len(futures), "Grading", unit="album")

        for fut in as_completed(futures):
            album = futures[fut]

            try:
                result = fut.result()
            except Exception as e:
                stats["total_scanned"] += 1
                stats["error_count"] += 1
                stats["errors"].append((album, str(e)))
                _pbar_update(pbar, counts, kind="fail")
                continue

            if result is None:
                stats["skipped_count"] += 1
                _pbar_skip(pbar, counts)
                continue

            if isinstance(result, dict) and result.get("error"):
                stats["total_scanned"] += 1
                stats["error_count"] += 1
                stats["errors"].append((result.get("path", album), result.get("error_detail", "unknown error")))
                _pbar_update(pbar, counts, kind="fail")
                continue
            stats["total_scanned"] += 1
            results.append(result)
            if sink is not None:
                sink[os.path.normcase(os.path.normpath(str(album)))] = result
            _pbar_update(pbar, counts, kind="ok")

        if pbar:
            pbar.close()

    results.sort(key=lambda r: _relpath_guard(r.get("path", ""), folder).lower())

    summary_pass = 0
    summary_total = 0
    issue_counts = {}
    audit_only_failed = 0

    for result in results:
        failed_checks = result["total_checks"] - result["pass_count"]
        passed = failed_checks == 0
        # Binary grading: an album is 100% only when every check passes.
        grade = "PASS" if passed else "FAIL"
        # ...and the library view we do not build here badges an album FAIL
        # when its AUDIT is FAKE/Mix (web/src/lib/status.ts). That verdict stays
        # out of this run's PASS — grading is reserved to tagging, per the
        # AccurateRip rule — but it is counted, so the run never reports a
        # library as entirely fine while the UI shows rows in red.
        audit_txt = str(result.get("audit_summary") or "").strip()
        audit_bad = audit_txt.upper() in ("FAKE", "MIX")
        if audit_bad and passed:
            audit_only_failed += 1

        stats["grade_dist"][grade] += 1
        summary_pass += result["pass_count"]
        summary_total += result["total_checks"]

        for field in result["issues"]:
            issue_counts[field] = issue_counts.get(field, 0) + 1

        rel = _relpath_guard(result["path"], folder)

        grade_color = Color.GREEN if passed else Color.RED

        log(
            f"{c('✓' if passed else '✕', grade_color)} {rel}  "
            f"{c(grade, grade_color)} {result['pass_count']}/{result['total_checks']} · "
            f"{result['track_count']} tr · {result['media'] or 'no media'} · "
            f"src {result['source_summary'] or '—'} · "
            f"{result['cover_file'] or 'no cover'} · "
            f"log {'✓' if result['has_log'] else '–'} "
            f"cue {'✓' if result['has_cue'] else '–'} · "
            f"lyrics {result['lyrics_present']}/{result['lyrics_expected']}"
            # The row itself says why the library view will show this album in
            # red even though the grade above is a PASS.
            f"{f' · audit {audit_txt.upper()}' if audit_bad else ''}"
        )

        missing_tags = [
            t for t in ALBUM_TAGS if not result["album_values"].get(t)
        ]
        # An empty folder carries no tags to be missing (and no tracks below).
        if missing_tags and result["track_count"]:
            log(c(f"    missing album tags: {', '.join(missing_tags)}",
                  Color.YELLOW))

        if result["issues"]:
            log(c(f"    issues: {', '.join(result['issues'])}", Color.RED))

        if verbose and result["tracks"]:
            log("Tracks:")

            for i, tr in enumerate(result["tracks"], 1):
                v = tr["values"]

                lyr = []
                if tr["lyrics_embedded"]:
                    lyr.append("EMB")
                if tr["lyrics_lrc"]:
                    lyr.append("LRC")
                lyr_state = "+".join(lyr) if lyr else "NONE"

                log(f"  {i:02d}. {tr['file']}")
                log(
                    f"      GENRE={_short_val(v.get('GENRE'), 18)} | "
                    f"MOOD={_short_val(v.get('MOOD'), 12)} | "
                    f"ADVISORY={_short_val(v.get('ITUNESADVISORY'), 8)} | "
                    f"DR={_short_val(v.get('DYNAMIC RANGE'), 6)} | "
                    f"INST={_short_val(v.get('INSTRUMENTAL'), 4)}"
                )
                log(
                    f"      RG_TRACK={_short_val(v.get('REPLAYGAIN_TRACK_GAIN'), 10)} / "
                    f"{_short_val(v.get('REPLAYGAIN_TRACK_PEAK'), 8)} | "
                    f"RG_ALBUM={_short_val(v.get('REPLAYGAIN_ALBUM_GAIN'), 10)} / "
                    f"{_short_val(v.get('REPLAYGAIN_ALBUM_PEAK'), 8)}"
                )
                log(
                    f"      MEDIA={_short_val(v.get('MEDIA'), 14)} | "
                    f"SOURCE={_short_val(v.get('SOURCE'), 14)} | "
                    f"LYRICS={lyr_state}"
                )

                if tr["issues"]:
                    log(
                        c(
                            f"      Issues: {', '.join(tr['issues'])}",
                            Color.RED,
                        )
                    )

    stats["summary_pass"] = summary_pass
    stats["summary_total"] = summary_total
    stats["albums_passed"] = stats["grade_dist"].get("PASS", 0)
    stats["albums_failed"] = stats["grade_dist"].get("FAIL", 0)
    # Albums that pass every graded check but are shown as FAIL by the library
    # because their audit came back FAKE/Mix: the run's PASS count is grade-only
    # (see the loop), so it names them here instead of reading as "all fine".
    stats["albums_audit_failed"] = audit_only_failed
    stats["issue_counts"] = issue_counts

    if audit_only_failed:
        log(c(f"{audit_only_failed} album(s) pass grading with a FAKE/Mix audit "
              f"— the library badges them FAIL (Audit column)", Color.YELLOW))

    return stats


# --------------------------------------------------------------------------- #
# Check gates — read-only introspection, for MAINTAIN → Check stack
# --------------------------------------------------------------------------- #
# The config keys this module consults to gate a check. Read out of this
# file's OWN source instead of being typed out a second time: server/api_stack
# and tools/test_check_stack.py compare this set against the keys
# mlo.config.DEFAULT_CONFIG defines, so a gate that is read here but settable
# nowhere (or a check nothing reads any more) is REPORTED rather than hiding —
# which is the only meaning "every workflow uses the latest checks" can have in
# code. Grading logic itself does not change here; nothing below is consulted
# by a grade.

CHECK_PREFIXES = ("grade_check_", "grade_include_")


def check_gates():
    """Sorted ``grade_check_*`` / ``grade_include_*`` keys this module reads.

    A string constant anywhere in the module counts — a config lookup, a
    table value, a docstring, a comment naming the check. Prose is a claim:
    a name mentioned here is one this module says it owns, so a stale mention
    is something the stack test reports instead of a silent no-op.
    """
    import ast
    with open(__file__, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=__file__)
    gates = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            # the prefixes themselves are not keys
            if (node.value.startswith(CHECK_PREFIXES)
                    and node.value not in CHECK_PREFIXES):
                gates.add(node.value)
    return sorted(gates)
