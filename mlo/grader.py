"""Library grader: per-album tag/lyrics/cover compliance reports."""
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from .audio import AudioFile
from .config import should_write_audio_tag
from .lyrics import (
    _lrc_for, _canonical_lyrics, format_lyrics_text,
    sync_level_of, text_meets_sync_level,
)
from .lyrics_xlit import (
    XLIT_SIDECAR, ai_ready, needs_translation, needs_transliteration,
    primary_translation_lang,
)
from .cue import canonical_cue_text
from .naming import DEFAULT_NAMING_SCRIPT
from .paths import AUDIO_EXTS, IMAGE_EXTS, LIB_AUDIO_EXTS, LIB_VIDEO_EXTS, get_sidecar_cover_path
from .stats import (
    new_stats, _make_pbar, _pbar_skip, _pbar_update, is_audio_file,
    _find_albums, _clean_set, _summarize_values, _collect_targets,
    worker_count,
)
from .deps import HAS_PIL, Image
from .ui import print_header, log, c, Color, print_separator, _short_val

# Media formats the library understands (MusicBrainz-style MEDIA values).
# Everything outside CD / Digital Media in this set is graded like any other
# release — the CUE/LOG/AccurateRip expectations are already gated on
# media_summary == "CD" — while unknown values still fail grading.
KNOWN_MEDIA = {
    "cd", "cd-r", "digital media", "vinyl", '12" vinyl', '10" vinyl', '7" vinyl',
    "sacd", "dvd", "dvd-video", "dvd-audio", "blu-ray", "blu-spec cd", "shm-cd",
    "cassette", "minidisc", "8-track", "vhs", "laserdisc",
}


def _get_cover_dimensions(cover_path):
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
    "GENRE",
    "ITUNESADVISORY",
    "REPLAYGAIN_TRACK_GAIN",
    "REPLAYGAIN_TRACK_PEAK",
    "REPLAYGAIN_ALBUM_GAIN",
    "REPLAYGAIN_ALBUM_PEAK",
    "DYNAMIC RANGE",
    "INSTRUMENTAL",
]

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


def _lyrics_formatted(text, cfg, is_for_lrc=False):
    """True when the lyrics already match the configured formatting
    (timestamps, metadata stripping, blank collapse, no trailing blanks).

    Idempotency check against the raw text: running the Lyrics formatter
    must not change it (so a stray trailing newline, CRLF, or timestamp
    precision drift is caught too). When enhanced LRC is enabled, word-level
    <mm:ss.xx> timestamps are also validated for correct precision/formatting.
    Respects lrc_zero_timestamp_target and blank mode.
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


# Alias for task description naming
def _lyrics_enhanced_valid(text, cfg):
    return _lyrics_word_timestamps_valid(text, cfg)


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
# videos still fail the dedicated un-remuxed-video check.
CATEGORY_INCLUDE_KEYS = {
    "music": "grade_include_music",
    "cover": "grade_include_cover",
    "cue": "grade_include_cue",
    "log": "grade_include_log",
    "lrc": "grade_include_lrc",
    "accurip": "grade_include_accurip",
    "video": "grade_include_video",
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


def _classify_file(f):
    """Category of a filename: music / cover / cue / log / lrc / accurip / video / other."""
    low = f.lower()
    if low.endswith(LIB_AUDIO_EXTS):
        return "music"
    if low.endswith(_video_exts()):
        # Remuxed (MKV) and raw (VOB/AVI/WMV/...) videos are their own
        # allowed-by-default category, so a raw VOB only fails the dedicated
        # un-remuxed-video check — never the generic disallowed-files check.
        return "video"
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
    return "other"


def _is_video_file(f):
    """Video-container tracks (music videos) are library tracks but are
    never CD-DA: the CD-only checks (rip-log score, CRC coverage, 16/44.1
    format, AccurateRip requirement) must not apply to them."""
    return os.path.splitext(f or "")[1].lower() in (".mp4", ".m4a", ".aac")


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
    the same convention the organizer's sidecar pass follows)."""
    track_stems = {os.path.splitext(f)[0].lower() for f in audio_files}
    out = []
    for f in sorted(all_files):
        low = f.lower()
        if not low.endswith(IMAGE_EXTS) or low in COVER_NAMES:
            continue
        full = os.path.join(album_dir, f)
        if _skip_grading_file(full):
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


def _image_file_ok(path, config=None):
    """An image sidecar passes when it is a real, non-empty file.

    When *config* is provided and cover enforcement is enabled,
    delegates to _cover_image_ok for dimension/square checks.
    """
    if config is not None:
        # Treat any image as cover for the generic check; _cover_image_ok
        # will handle the non-enforced case by just checking size >0
        try:
            return _cover_image_ok(path, config)
        except NameError:
            pass
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


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
    """Validate a cover image against size/square enforcement.

    Checks:
    * file exists and >0 bytes (always)
    * if cover_enforce_size and cover_resize_enabled and target_size>0,
      dimensions must be exactly target_size x target_size (1px tolerance)
    * if cover_enforce_square, aspect ratio must be within threshold
      (abs(width/height -1) <= threshold)

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
            try:
                img.load()
            except Exception:
                pass
            w, h = img.size
            if w <= 0 or h <= 0:
                return False
            # Size enforcement — dimensions must match the resolution
            # target exactly (within tolerance) in BOTH directions: an
            # undersized cover fails just like an oversized one.
            if enforce_size and resize_enabled and target > 0:
                try:
                    tol = int(config.get("grader_cover_size_tolerance_px", 0) or 0)
                    tol = max(0, min(5, tol))
                except Exception:
                    tol = 0
                if abs(w - target) > tol or abs(h - target) > tol:
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
                if abs(jw - target) > tol or abs(jh - target) > tol:
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
        return True


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
                ok = _lyrics_formatted(lrc_text, cfg) and \
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
                            st, _ = _parse_ar_side(acc_text)
                            ok = st in ("REAL", "FAKE")
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
                    enforce_square = bool(cfg.get("cover_enforce_square", False))
                    # Try to inspect image for specific reason
                    try:
                        if HAS_PIL:
                            with Image.open(full) as _im:
                                _w, _h = _im.size
                                if enforce_size and (abs(_w - tgt) > 1 or abs(_h - tgt) > 1):
                                    detail = f"wrong size {_w}x{_h} (need {tgt}x{tgt})"
                                elif enforce_square:
                                    thr = float(cfg.get("cover_crop_threshold", 0.05) or 0.05)
                                    thr = max(0.0, min(0.5, thr))
                                    ratio = _w / _h if _h else 1.0
                                    if abs(ratio - 1.0) > thr:
                                        detail = f"not square {_w}x{_h}"
                                    else:
                                        detail = "needs resize/crop"
                                else:
                                    detail = "needs resize/crop"
                        else:
                            detail = "needs resize/crop"
                    except Exception:
                        detail = "needs resize/crop"
        else:
            ok = _category_allowed(cfg, category)
            detail = "allowed" if ok else "disallowed type"
        sidecars.append({
            "file": f, "type": category,
            "ok": ok, "detail": detail,
        })
    return sidecars


def _norm_path_case(p):
    """Separator + case normalization so path comparisons work on both
    Windows (case-insensitive, backslashes) and POSIX."""
    return os.path.normcase(str(p or "").replace("/", os.sep).replace("\\", os.sep))


def _naming_mismatch(ap, folder, script, release_type, tags):
    """Expected-relative-path comparison for grade_check_naming.

    The naming script defines the target path WITHOUT the file extension
    (beets-style — the extension is preserved from the source file), so the
    extension is appended before comparing. Returns (kind, expected) where
    kind is "ok" (exact match), "case" (matches except for LETTER CASE —
    graded by grade_check_filename_case) or "path" (no match at all —
    graded by grade_check_naming). Both full and 8-char-truncated
    MusicBrainz IDs are accepted so the short_folder_names setting can't
    produce false failures.
    """
    from mlo.naming import eval_script, track_variables

    actual = os.path.relpath(ap, folder)
    variables = track_variables(tags or {}, release_type=release_type)
    ext = os.path.splitext(ap)[1]
    expected_full = eval_script(script, variables, shorter_ids=False) + ext
    expected_short = eval_script(script, variables, shorter_ids=True) + ext

    def _seps(p):
        # separator-normalized but CASE-SENSITIVE (exact compare)
        return str(p).replace("/", os.sep).replace("\\", os.sep)

    for exp in (expected_full, expected_short):
        if exp and _seps(exp) == _seps(actual):
            return ("ok", None)
    for exp in (expected_full, expected_short):
        if exp and _norm_path_case(exp) == _norm_path_case(actual):
            return ("case", expected_full or expected_short)
    return ("path", expected_full or expected_short)


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

    album_tag_values = {}
    media_values = []
    source_values = []
    album_artist = None

    # Path-grading setup (grade_check_naming): the naming script is evaluated
    # per track exactly like the organizer does, minus the network MB lookup —
    # RELEASETYPE comes from tags only.
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

    lyrics_present_count = 0
    lyrics_expected_count = 0
    instrumental_count = 0

    def add_issue(field, where="album"):
        issues.setdefault(field, set()).add(where)

    cover_file = None
    for f in all_files:
        if f.lower() in COVER_NAMES:
            cover_file = f
            break

    has_log = any(f.lower().endswith(".log") for f in all_files)
    has_cue = any(f.lower().endswith(".cue") for f in all_files)

    for ap in audio_paths:
        af = AudioFile(ap)
        basename = os.path.basename(ap)

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
            if cfg.get("grade_check_missing_tags", True):
                for t in PER_TRACK_TAGS:
                    if not should_write_audio_tag(cfg, t, filepath=ap):
                        continue
                    total_checks += 1
                    failed_checks += 1

            tracks.append(track)
            continue

        # Required per-track tags (skip if per-filetype disabled).
        is_video_track = os.path.splitext(ap)[1].lower() in LIB_VIDEO_EXTS
        for t in PER_TRACK_TAGS:
            if is_video_track and t in VIDEO_SKIP_TAGS:
                # Video containers: ReplayGain / DR never apply (no engine
                # script writes them there) — record, don't grade.
                track["values"][t] = af.get_tag(t)
                continue
            if not should_write_audio_tag(cfg, t, filepath=ap):
                # Track not supposed to have this tag for its filetype — don't grade it
                track["values"][t] = af.get_tag(t)
                continue
            total_checks += 1
            val = af.get_tag(t)
            track["values"][t] = val

            if val is None or str(val).strip() == "":
                # ITUNESADVISORY is optional: absence means "unrated", not a
                # defect — nothing auto-fills 0 anymore, so a missing tag
                # must not fail the check (a PRESENT value is still validated
                # as 0/1/2 below).
                if cfg.get("grade_check_missing_tags", True) and t != "ITUNESADVISORY":
                    failed_checks += 1
                    add_issue(f"Missing {t}", basename)
                    track["issues"].append(t)
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
                if raw != raw.strip():
                    if cfg.get("grade_check_tag_spaces", True):
                        failed_checks += 1
                        add_issue(f"GENRE has leading/trailing spaces ({raw!r})", basename)
                        track["issues"].append(t)

            # Configurable: check tags for blank lines (spaces already handled for GENRE above)
            # Only check per-line trailing/leading spaces for non-GENRE/ITUNESADVISORY when enabled
            if cfg.get("grade_check_tag_spaces", True) and t not in ("GENRE", "ITUNESADVISORY"):
                raw_all = str(val) if val is not None else ""
                # Per-line check: any line with leading/trailing spaces/tabs (not newlines)
                if raw_all and any(ln != ln.strip(" \t") for ln in raw_all.splitlines()):
                    failed_checks += 1
                    add_issue(f"{t} has leading/trailing spaces ({raw_all!r})", basename)
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

        # Key & BPM (script 12 output) — required when the check is on.
        # Excess tags: anything NEITHER this pipeline's scripts NOR beets
        # would have written — the optimizer's strip pass would remove every
        # key outside the shared vocabulary. Their presence counts against
        # grading so unoptimized files surface.
        if cfg.get("grade_check_excess_tags", True) and not is_video_track:
            from .audio import TAG_MAP as _TAG_MAP
            # Script vocabulary: TAG_MAP keys (compared with spaces/
            # underscores normalized away, so "DYNAMIC RANGE" and file-side
            # spellings like "DYNAMIC_RANGE" match), the encoder identity
            # tags, and beets/mediafile's own spellings (MUSICBRAINZ_* ids,
            # TRACK/DISC/CATALOGNUM, …) — beets-tagged files are first-class
            # citizens, only junk like vendor tags counts as excess.
            _allowed = {_tag_key_norm(k) for k in _TAG_MAP} | {_tag_key_norm(k) for k in (
                "ENCODER_PROGRAM", "ENCODER_QUALITY", "ENCODER_VERSION",
                *BEETS_TAGS)}
            # Language-specific lyrics transforms (TRANSLATION-EN,
            # TRANSLITERATION-JA-LATN, …) are first-class tags, not excess.
            _allowed_prefixes = ("TRANSLATION-", "TRANSLITERATION-")
            _extra = []
            for _k in af.all_tags().keys():
                _k = str(_k)
                _ku = _k.upper()
                if _tag_key_norm(_k) in _allowed:
                    continue
                if _ku.startswith(_allowed_prefixes):
                    continue
                if _ku.startswith("TXXX:"):
                    # ID3 freeform frames: allowed when the frame's
                    # description names a tag the script or beets writes.
                    if _tag_key_norm(_k.split(":", 1)[1]) in _allowed:
                        continue
                if _ku.startswith("----:"):
                    # MP4 freeform atoms ("----:com.apple.iTunes:Name"):
                    # same rule on the sub-name.
                    if _tag_key_norm(_k.rsplit(":", 1)[-1]) in _allowed:
                        continue
                if _ku in BEETS_ID3_FRAMES:
                    continue
                _extra.append(_k)
            _extra = sorted(set(_extra))
            if _extra:
                total_checks += 1
                failed_checks += 1
                shown = ", ".join(_extra[:6]) + ("…" if len(_extra) > 6 else "")
                add_issue(f"Excess tags: {shown}", basename)
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

        # File/folder names must match the naming script (the same script the
        # organizer applies), relative to the music folder — exact match for
        # PATH, and letter-case separately for PATH_CASE.
        if naming_check:
            try:
                tags_map = af.all_tags() or {}
            except Exception:
                tags_map = {}
            if album_release_type is None and tags_map.get("RELEASETYPE"):
                album_release_type = tags_map.get("RELEASETYPE")
            kind, expected = _naming_mismatch(
                ap, music_folder, naming_script,
                tags_map.get("RELEASETYPE") or album_release_type,
                tags_map,
            )
            if kind == "path" and cfg.get("grade_check_naming", True):
                total_checks += 1
                failed_checks += 1
                add_issue(f"PATH: expected '{expected}'", basename)
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
                        if raw and any(ln != ln.strip(" \t") for ln in raw.splitlines()):
                            failed_checks += 1
                            add_issue(f"{tag_key} has leading/trailing spaces ({raw!r})", basename)
                            track["issues"].append(tag_key)
                    if cfg.get("grade_check_tag_blank_lines", True):
                        total_checks += 1
                        if "\n" in raw and any(not line.strip() for line in raw.splitlines()):
                            failed_checks += 1
                            add_issue(f"{tag_key} has blank lines", basename)
                            track["issues"].append(tag_key)
            except Exception:
                pass

        # ENCODER marker tags — per-format, only when that field is enabled.
        # For FLAC (the only audio type the app re-encodes), check PROGRAM/QUALITY/VERSION.
        # PROGRAM is off by default since v1.4.2, but when turned on per format grading must require it.
        if cfg.get("grade_check_encoder", True):
            try:
                ext_enc = os.path.splitext(ap)[1].lower()
                enc_key = None
                if ext_enc == ".flac":
                    enc_key = "flac"
                elif ext_enc in (".jpg", ".jpeg"):
                    enc_key = "jpeg"
                elif ext_enc == ".png":
                    enc_key = "png"
                elif ext_enc == ".jxl":
                    enc_key = "jxl"
                if enc_key:
                    enc_cfg = (cfg.get("encoder_tags") or {}).get(enc_key, {}) if cfg else {}
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
            except Exception:
                pass

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

        # Album-wide tag values (only for enabled filetypes).
        for t in ALBUM_TAGS:
            if not should_write_audio_tag(cfg, t, filepath=ap):
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
            and cfg.get("grade_check_audit", True)
            and not is_video_track
        ):
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
        if media_clean and not is_video_track:
            media_values.append(media_clean)

        if source_clean and not is_video_track:
            source_values.append(source_clean)

        # Lyrics status.
        lyr = af.get_lyrics()
        embedded = bool(lyr and str(lyr).strip())
        lrc = os.path.exists(_lrc_for(ap))

        track["lyrics_embedded"] = embedded
        track["lyrics_lrc"] = lrc

        inst = af.get_tag("INSTRUMENTAL")
        inst_val = str(inst).strip() if inst is not None else None
        track["values"]["INSTRUMENTAL"] = inst_val

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
                    if lyr_text and not _lyrics_formatted(lyr_text, cfg, is_for_lrc=False):
                        fmt_ok = False
                    if lrc_text and not _lyrics_formatted(lrc_text, cfg, is_for_lrc=True):
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
                # Unsynced lyrics must fail — plain text without any [mm:ss.xx] is not synced
                if lyr_text and not TIMESTAMP_RE_GRADE.search(lyr_text):
                    fmt_ok = False
                if lrc_text and not TIMESTAMP_RE_GRADE.search(lrc_text):
                    fmt_ok = False
                if not fmt_ok:
                    failed_checks += 1
                    add_issue("Lyrics not optimally formatted "
                              "(run Lyrics script)", basename)
                    track["issues"].append("LYRICS")

        # Script 15 outputs: transliteration + translation, graded per track.
        # Gated on the features being enabled AND AI being configured in
        # Settings → AI, so a library without lyric transforms configured is
        # never penalized. Latin-script lyrics are their own transliteration
        # (romanizing Spanish would be a no-op) and always pass the xlit check.
        # Transliteration / translation compliance — graded only when the
        # transforms are actually NEEDED for the configured reader locale
        # (script 15's own rule): non-Latin lyrics in a script the reader
        # doesn't read need romanization; lyrics whose script differs from
        # the locale's script need a translation. Same-script pairs (French
        # → en) can't be told apart from the target language offline, so
        # they are never demanded here.
        _xlit_src = None
        if (embedded or lrc) and ai_ready(cfg):
            _xlit_src = str(lyr) if embedded else None
            if _xlit_src is None and lrc:
                try:
                    with open(_lrc_for(ap), "r", encoding="utf-8",
                              errors="replace") as _f:
                        _xlit_src = _f.read()
                except OSError:
                    _xlit_src = None
        if _xlit_src and cfg.get("grade_check_xlit", True) \
                and cfg.get("lyrics_xlit_enabled", True) \
                and needs_transliteration(_xlit_src, cfg):
            total_checks += 1
            xlit_text = str(af.get_lyrics_transform("TRANSLITERATION") or "").strip()
            if not xlit_text:
                _xlit_side = os.path.splitext(ap)[0] + XLIT_SIDECAR
                if os.path.isfile(_xlit_side):
                    try:
                        with open(_xlit_side, "r", encoding="utf-8", errors="replace") as _f:
                            xlit_text = _f.read().strip()
                    except OSError:
                        xlit_text = ""
            if not xlit_text:
                failed_checks += 1
                add_issue("No transliteration for non-Latin lyrics "
                          "(run Lyrics Translate script)", basename)
                track["issues"].append("LYRICS")
            elif cfg.get("lrc_enhanced_enabled", True) \
                    and cfg.get("lrc_enhanced_word_sync", True) \
                    and (sync_level_of(xlit_text) < sync_level_of(_xlit_src)
                         or not text_meets_sync_level(xlit_text, cfg.get("lrc_sync_level", "LINE"))):
                # the source lyrics are syllable/word-synced — the
                # romanization must be too, at least as fine-grained, or
                # karaoke dies at the romanized line
                failed_checks += 1
                add_issue("Transliteration not syllable-synced "
                          "(force re-run Lyrics Translate script)", basename)
                track["issues"].append("LYRICS")
        if _xlit_src and cfg.get("grade_check_trans", True) \
                and cfg.get("lyrics_translate_enabled", True) \
                and needs_translation(_xlit_src, cfg):
            lang = primary_translation_lang(cfg)
            total_checks += 1
            trans_text = str(af.get_lyrics_transform("TRANSLATION", lang) or "").strip()
            if not trans_text:
                _trans_side = os.path.splitext(ap)[0] + f".{lang}.lrc"
                if os.path.isfile(_trans_side):
                    try:
                        with open(_trans_side, "r", encoding="utf-8", errors="replace") as _f:
                            trans_text = _f.read().strip()
                    except OSError:
                        trans_text = ""
            if not trans_text:
                failed_checks += 1
                add_issue(f"No {lang} translation (run Lyrics Translate script)",
                          basename)
                track["issues"].append("LYRICS")
            elif cfg.get("lrc_enhanced_enabled", True) \
                    and cfg.get("lrc_enhanced_word_sync", True) \
                    and (sync_level_of(trans_text) < sync_level_of(_xlit_src)
                         or not text_meets_sync_level(trans_text, cfg.get("lrc_sync_level", "LINE"))):
                failed_checks += 1
                add_issue(f"{lang} translation not syllable-synced "
                          "(force re-run Lyrics Translate script)", basename)
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
                _want = primary_translation_lang(cfg).upper()
                add_issue(
                    f"{', '.join(_bare_transforms)} tag lacks language detail "
                    f"(expected e.g. TRANSLATION-{_want}, TRANSLITERATION-JA-LATN)",
                    basename)
                track["issues"].append("LYRICS")

        # Sidecar track cover (e.g. "01 - Song.flac" → "01 - Song.jpg" in same folder)
        # Graded with the same cover checks as the album cover.* (size, square, etc.)
        # when such a sidecar exists for this track. Uses the same config keys
        # (cover_target_size, cover_enforce_size/square, cover_crop_threshold, etc.)
        try:
            sidecar = get_sidecar_cover_path(album_dir, basename)
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
                        enforce_square_sc = bool(cfg.get("cover_enforce_square", False))
                        try:
                            if HAS_PIL:
                                with Image.open(sidecar) as _im_sc:
                                    _w_sc, _h_sc = _im_sc.size
                                    if enforce_size_sc and (abs(_w_sc - tgt_sc) > 1 or abs(_h_sc - tgt_sc) > 1):
                                        detail = f"wrong size {_w_sc}x{_h_sc} (need {tgt_sc}x{tgt_sc})"
                                    elif enforce_square_sc:
                                        thr_sc = float(cfg.get("cover_crop_threshold", 0.05) or 0.05)
                                        thr_sc = max(0.0, min(0.5, thr_sc))
                                        ratio_sc = _w_sc / _h_sc if _h_sc else 1.0
                                        if abs(ratio_sc - 1.0) > thr_sc:
                                            detail = f"not square {_w_sc}x{_h_sc}"
                                        else:
                                            detail = "needs resize/crop"
                                    else:
                                        detail = "needs resize/crop"
                            else:
                                detail = "needs resize/crop"
                        except Exception:
                            detail = "needs resize/crop"
                    add_issue(f"Sidecar cover {os.path.basename(sidecar)} {detail}", basename)
                    track["issues"].append("COVER")
        except Exception:
            track["sidecar_cover"] = None
            track["sidecar_cover_file"] = None

        tracks.append(track)

    # MEDIA consistency (skip if MEDIA_SOURCE disabled for all tracks).
    any_media_enabled = any(
        should_write_audio_tag(cfg, "MEDIA", filepath=os.path.join(album_dir, tr["file"]))
        for tr in tracks if not tr.get("unreadable")
    )
    media_summary = _summarize_values(media_values)
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

    digital = media_summary == "Digital Media"

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
    if media_summary == "CD":
        if cfg.get("grade_check_cd_log", True):
            total_checks += 1
            if not has_log:
                failed_checks += 1
                add_issue("Missing .log file", "album")

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
                total_checks += 1
                if bad_logs or bad_cues:
                    failed_checks += 1
                    detail = ", ".join(bad_logs + bad_cues)
                    add_issue(f"CD rip sheets not named {pat} (found: {detail}) — "
                              f"enable Settings → CD Rips → Auto-Rename or "
                              f"rename manually to {pat.replace('{n}', '1')}.log", "album")
            except Exception:
                pass

        # CD releases must carry the rip-log score on every track (skipped if
        # LOG_GRADE disabled for this filetype). Digital Media has no log.
        if media_summary == "CD" and cfg.get("grade_check_log_grade", True):
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
                        thresh = int(cfg.get("grade_log_score_threshold", 0) or 0)
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
                crc_map = {}
                for lp in sorted(log_paths):
                    crc_map.update(parse_log_checksums(read_log_text(lp)))
                if not crc_map:
                    total_checks += 1
                    failed_checks += 1
                    add_issue("Rip .log has no per-track CRC checksums "
                              "(cannot verify CD integrity)", "album")
                else:
                    discs_map = _album_discs(album_dir)
                    multi = bool(discs_map)
                    for ap in audio_paths:
                        tr_track = next(
                            (t for t in tracks
                             if t.get("unreadable") is False
                             and os.path.join(album_dir, t["file"]) == ap),
                            None)
                        if tr_track is None:
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
                        covered = tn is not None and tn in crc_map
                        if multi:
                            d = disc_of_filename(os.path.basename(ap)) or 1
                            dlog = os.path.join(album_dir, _exp_name(_pat2(cfg), d, ".log"))
                            if not os.path.isfile(dlog):
                                covered = False
                        total_checks += 1
                        if not covered:
                            failed_checks += 1
                            add_issue("Track not covered by .log CRC "
                                      "(unverifiable CD rip)", tr_track["file"])
                            tr_track["issues"].append("CRC")
            except Exception:
                pass

        # CD format: must be 16-bit 44.1 kHz (CD-DA) — helps detect fake rips from hi-res upsampled sources
        if cfg.get("grade_check_cd_format", True) and media_summary == "CD":
            try:
                for ap in audio_paths:
                    tr_track = next(
                        (t for t in tracks
                         if t.get("unreadable") is False
                         and os.path.join(album_dir, t["file"]) == ap),
                        None)
                    if tr_track is None:
                        continue
                    if _is_video_file(tr_track.get("file")):
                        continue
                    # Get audio format details via mutagen
                    try:
                        af_fmt = AudioFile(ap)
                        if af_fmt.audio is None or not hasattr(af_fmt.audio, "info") or af_fmt.audio.info is None:
                            continue
                        info = af_fmt.audio.info
                        # mutagen FLAC/OGG: bits_per_sample + sample_rate; MP3: sample_rate; MP4: sample_rate/bits
                        bits = getattr(info, "bits_per_sample", None)
                        if bits is None:
                            bits = getattr(info, "bits_per_sample", None)  # fallback
                            # For some formats, bits may be in different attr
                            if bits is None:
                                bits = getattr(info, "bits", None)
                        rate = getattr(info, "sample_rate", None)
                        if rate is None:
                            rate = getattr(info, "sample_rate", None)
                        # Only check when we can determine both; CD must be 16/44.1
                        # For MP3/MP4 where bits not available, check rate only
                        is_ok = True
                        detail = ""
                        if bits is not None and bits != 16:
                            is_ok = False
                            detail = f"{bits}-bit"
                        if rate is not None and rate != 44100:
                            is_ok = False
                            detail = f"{detail} {rate}Hz".strip() if detail else f"{rate}Hz"
                        elif rate is None:
                            # No rate info: cannot verify, skip
                            continue
                        total_checks += 1
                        if not is_ok:
                            failed_checks += 1
                            add_issue(f"CD must be 16-bit 44.1 kHz (found {detail or 'unknown format'})", tr_track["file"])
                            tr_track["issues"].append("CD_FORMAT")
                    except Exception:
                        continue
            except Exception:
                pass

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
                    elif state == "invalid":
                        per_disc_checksum_map[disc_for_log] = "FAKE"
                    elif state == "missing":
                        # Missing checksum is FAKE when verify required, else NONE for viewer — respect config
                        if cfg.get("audit_verify_log_checksum", True):
                            per_disc_checksum_map[disc_for_log] = "FAKE"
                        else:
                            per_disc_checksum_map[disc_for_log] = "NONE"
                    elif state == "unsupported":
                        per_disc_checksum_map[disc_for_log] = "NONE"
                    elif state is None:
                        # Error / not found — leave as NONE
                        pass
                # Album aggregate still needed for _realtime fallback — missing counts as invalid when required
                if state == "ok":
                    has_csum = True
                elif state == "invalid":
                    has_invalid = True
                    has_csum = True
                elif state == "missing" and cfg.get("audit_verify_log_checksum", True):
                    has_invalid = True
                    has_csum = True
            if has_invalid:
                checksum_status = "FAKE"
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
            # Per-disc checksum (fixes whole-album FAKE when only one disc's log is bad) and per-track accuraterip
            for tr in tracks:
                # Per-disc checksum lookup — only that disc's tracks show FAKE
                try:
                    from .discs import disc_of_filename as _dof_c, _track_num_of as _tnof_c, album_discs as _ad_c2
                    base_c = tr["file"]
                    disc_n_c = _dof_c(base_c)
                    if disc_n_c is None:
                        try:
                            discs_all_c = _ad_c2(album_dir)
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
                    from .discs import disc_of_filename as _dof, _track_num_of as _tnof, _file_track_number as _ftn, album_discs as _ad2
                    # Determine disc for this track file
                    base = tr["file"]
                    disc_n = _dof(base)
                    if disc_n is None:
                        # Fallback: infer from album discs mapping
                        try:
                            discs_all = _ad2(album_dir)
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
                # Per-track realtime audit for viewer highlighting: missing/FAKE accurip makes track AUDIT FAKE
                # when audit_require_accuraterip is required (True by default, per user request).
                # This ensures library viewer highlights tracks red like their album when .accurip is missing,
                # even though the stored AUDIT tag may still be REAL until the next Audit Library run.
                try:
                    if _is_video_file(tr.get("file")):
                        pass
                    else:
                        if media_summary == "CD" and cfg.get("audit_require_accuraterip", True) and cfg.get("grade_check_accuraterip", True):
                            if tr.get("accuraterip_status") in ("NONE", "FAKE"):
                                tr["audit"] = "FAKE"
                        if media_summary == "CD" and cfg.get("audit_verify_log_checksum", True) and cfg.get("grade_check_log_checksum", True):
                            if tr.get("checksum_status") == "FAKE":
                                tr["audit"] = "FAKE"
                except Exception:
                    pass
            # Second pass for tracks where accurip_status was set via per_map continue path: ensure audit override there too
            for tr in tracks:
                if _is_video_file(tr.get("file")):
                    continue
                try:
                    if media_summary == "CD" and cfg.get("audit_require_accuraterip", True) and cfg.get("grade_check_accuraterip", True):
                        if tr.get("accuraterip_status") in ("NONE", "FAKE"):
                            if tr.get("audit") != "FAKE":
                                tr["audit"] = "FAKE"
                    if media_summary == "CD" and cfg.get("audit_verify_log_checksum", True) and cfg.get("grade_check_log_checksum", True):
                        if tr.get("checksum_status") == "FAKE" and tr.get("audit") != "FAKE":
                            tr["audit"] = "FAKE"
                except Exception:
                    pass
        except Exception:
            pass

        # Grading: AccurateRip is AUDIT-only per user request — grading is reserved to tagging.
        # Do NOT fail grading on missing/FAKE accurip; only auditing fails. Viewer column still shows REAL/NONE/FAKE.

    elif media_summary == "Digital Media":
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
        # Force exact: when cover_force_exact_size is on, it implies both size and square
        # must be exactly target×target, regardless of the separate enforce toggles.
        force_exact = bool(cfg.get("cover_force_exact_size", False))
        enforce_size = bool(cfg.get("cover_enforce_size", False))
        enforce_square = bool(cfg.get("cover_enforce_square", False))
        if force_exact and cfg.get("cover_resize_enabled", False) and target_cov > 0:
            enforce_size = True
            enforce_square = True
        # Cache dimensions once (handles JXL via jxlinfo)
        w = h = None
        cover_read_error = False
        if (HAS_PIL or cover_path.lower().endswith(".jxl")) and (enforce_size or enforce_square):
            w, h = _get_cover_dimensions(cover_path)
            if w is None or h is None:
                cover_read_error = True
        # Size enforcement: require exact target_size x target_size (configurable tolerance)
        if enforce_size and cfg.get("cover_resize_enabled", False) and target_cov > 0 and cfg.get("grade_check_cover", True):
            total_checks += 1
            try:
                tol = int(cfg.get("grader_cover_size_tolerance_px", 0) or 0)
                tol = max(0, min(5, tol))
            except Exception:
                tol = 0
            if w is not None and h is not None:
                if abs(w - target_cov) > tol or abs(h - target_cov) > tol:
                    failed_checks += 1
                    size_failed = True
                    cover_ok = False
                    size_info = f"{w}x{h} → {target_cov}x{target_cov}"
                    add_issue(f"Cover image wrong size {w}x{h} (need {target_cov}x{target_cov})", "album")
            elif cover_read_error:
                failed_checks += 1
                size_failed = True
                cover_ok = False
                add_issue(f"Cover image unreadable/corrupt (need {target_cov}x{target_cov})", "album")
        # Square enforcement: aspect within threshold (force_exact uses strict threshold from config)
        if enforce_square and cfg.get("grade_check_cover", True) and cfg.get("grade_check_cover_crop", True):
            total_checks += 1
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
                        add_issue(f"Cover image not square {w}x{h} (threshold {thr_cov:.0%})", "album")
                        if not size_info:
                            size_info = f"{w}x{h} not square"
                elif cover_read_error:
                    failed_checks += 1
                    square_failed = True
                    cover_ok = False
                    add_issue("Cover image unreadable/corrupt (not square)", "album")
            except Exception:
                pass
        if size_failed or square_failed:
            if size_failed and square_failed:
                cover_detail = f"{cover_file} (wrong size, not square {size_info})"
            elif size_failed:
                cover_detail = f"{cover_file} (wrong size {size_info})"
            elif square_failed:
                cover_detail = f"{cover_file} (not square {size_info})"
            else:
                cover_detail = f"{cover_file} (needs resize/crop)"
        else:
            cover_detail = cover_file
        # ENCODER for cover image per-format (only when that field is enabled)
        try:
            cov_ext = os.path.splitext(cover_file)[1].lower() if cover_file else ""
            cov_enc_key = None
            if cov_ext in (".jpg", ".jpeg"):
                cov_enc_key = "jpeg"
            elif cov_ext == ".png":
                cov_enc_key = "png"
            elif cov_ext == ".jxl":
                cov_enc_key = "jxl"
            if cov_enc_key and cover_file:
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
        except Exception:
            pass
    # Cover failure makes every track fail as well (per request: track/album fail)
    if not cover_ok:
        for tr in tracks:
            if "COVER" not in tr["issues"]:
                tr["issues"].append("COVER")

    # CUE sheet FORMATTING compliance (when a cue exists): every cue must
    # already be in the canonical form the CUE formatter would produce.
    if has_cue and cfg.get("grade_check_cue_format", True):
        cue_files = sorted(
            os.path.join(album_dir, f) for f in all_files
            if f.lower().endswith(".cue")
        )
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

    # .accurip FORMATTING compliance (when .accurip exists): trim each line, outer blanks only
    # Per user: delete leading/trailing spaces per line, only outer blanks. Counts towards grading.
    if any(f.lower().endswith(".accurip") for f in all_files):
        # Only check formatting if the file is a valid CUETools log; synthetic files already fail via sidecar check
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
    # 11 normalizes them to MKV with every stream copied bit-exact. The
    # remuxed containers themselves (MKV/MP4) are not flagged here.
    if cfg.get("grade_check_raw_video", True):
        try:
            from .remux import VIDEO_EXTS as _ALL_VIDEO_EXTS
            _RAW_VIDEO_EXTS = tuple(e for e in _ALL_VIDEO_EXTS if e != ".mkv")
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
    # script 3 converts them to FLAC losslessly.
    if cfg.get("grade_check_lossless_source", True):
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
            add_issue(f"Uncompressed lossless file(s): {shown} (convert to FLAC)", "album")

    pass_count = max(0, total_checks - failed_checks)

    # Ensure viewer columns have values even for non-CD albums
    try:
        if "checksum_status" not in locals():
            checksum_status = "NONE"
        if "accuraterip_status" not in locals():
            accuraterip_status = "NONE"
        # For non-CD, ensure per-track values exist
        if media_summary != "CD":
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
            if media_summary == "CD" and cfg.get("audit_require_accuraterip", True) and cfg.get("grade_check_accuraterip", True):
                # Check per-track accuraterip: if any track is NONE/FAKE, album audit is FAKE, but per-track already set correctly above
                for tr in cd_tracks:
                    if tr.get("accuraterip_status") in ("NONE", "FAKE"):
                        return "FAKE"
        except Exception:
            pass
        # Check log checksum per-track (per-disc)
        try:
            if media_summary == "CD" and cfg.get("audit_verify_log_checksum", True) and cfg.get("grade_check_log_checksum", True):
                for tr in tracks:
                    if tr.get("checksum_status") == "FAKE":
                        return "FAKE"
        except Exception:
            pass
        # Check CD format (lightweight) — per-track already
        try:
            if media_summary == "CD" and cfg.get("audit_check_cd_format", True):
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
            all_ar_real = all(tr.get("accuraterip_status") == "REAL" for tr in cd_tracks) if media_summary == "CD" and cfg.get("audit_require_accuraterip", True) and cfg.get("grade_check_accuraterip", True) else True
            all_csum_ok = all(tr.get("checksum_status") != "FAKE" for tr in tracks) if media_summary == "CD" and cfg.get("audit_verify_log_checksum", True) and cfg.get("grade_check_log_checksum", True) else True
        except Exception:
            all_ar_real = accuraterip_status == "REAL"
            all_csum_ok = checksum_status != "FAKE"
        if tag_summary == "REAL" and all_ar_real and all_csum_ok:
            return "REAL"
        # If no AUDIT tag yet, but lightweight checks passed, consider REAL for display
        # (audit hasn't been run, but files would pass)
        if tag_summary is None and all_ar_real and all_csum_ok:
            # Need to ensure accuraterip is REAL when required
            if media_summary == "CD" and cfg.get("audit_require_accuraterip", True) and cfg.get("grade_check_accuraterip", True):
                if all_ar_real:
                    return "REAL"
            else:
                return "REAL"
        if tag_summary is None and media_summary == "CD":
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
    }


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
        _th = int(config.get("grade_log_score_threshold", 0) or 0)
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
    else:
        albums = _find_albums(folder)

    if not albums:
        log("No albums found.")
        return stats

    results = []
    counts = {"ok": 0, "skip": 0, "fail": 0}
    workers = worker_count(config, default=16, maximum=16, items=len(albums))

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
            _pbar_update(pbar, counts, kind="ok")

        if pbar:
            pbar.close()

    results.sort(key=lambda r: _relpath_guard(r.get("path", ""), folder).lower())

    summary_pass = 0
    summary_total = 0
    issue_counts = {}

    for result in results:
        failed_checks = result["total_checks"] - result["pass_count"]
        passed = failed_checks == 0
        # Binary grading: an album is 100% only when every check passes.
        pct = 100.0 if passed else 0.0
        grade = "PASS" if passed else "FAIL"

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
        )

        missing_tags = [
            t for t in ALBUM_TAGS if not result["album_values"].get(t)
        ]
        if missing_tags:
            log(c(f"    missing album tags: {', '.join(missing_tags)}",
                  Color.YELLOW))

        if result["issues"]:
            log(c(f"    issues: {', '.join(result['issues'])}", Color.RED))

        if verbose:
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
    stats["issue_counts"] = issue_counts

    return stats
