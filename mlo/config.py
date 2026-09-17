"""Configuration persisted to config.json next to the application."""
import copy
import json
import os
import tempfile

from .paths import (CONFIG_FILE, DEFAULT_DIGITAL_SOURCE, app_data_dir, downloads_dir,
                    legacy_state_dirs, read_music_folder_guess, trash_dir)
from .naming import DEFAULT_NAMING_SCRIPT
from .ui import c, Color

# Shipped default naming scripts from BEFORE 2.4.0 (only ever one). A config
# that saved the old default still pins the old layout — normalize_config
# swaps a stored legacy default for the current one, so the new default takes
# effect without ever touching a script the user actually edited.
LEGACY_DEFAULT_NAMING_SCRIPTS = (
    "%albumartist% [%musicbrainz_albumartistid%]/$if(%releasetype%,[%releasetype%] ,)"
    "$if(%originaldate%,%originaldate% - ,)$if(%date%,%date% - ,)"
    "%album% {$if(%releasecountry%,%releasecountry% - )%media%$if(%catalognumber%, - %catalognumber%)}/"
    "%discnumber%-$num(%tracknumber%,2) %title%",
)

# Run All order — strict pipeline v1.7.0: 1 Lyrics → 2 CUEs → 8 Auto Tagging → 3 FLAC → 5 Images → 9 AccurateRip → 6 Audit → 4 Grade → 7 DR/ReplayGain → 10 Format All.
# AccurateRip must run before Audit/Grade so the .accurip is present for real-time AUDIT; Grade after Audit so AUDIT tags are fresh; Format All at end does final canonical trims.
# User's strict config default as of v1.6.0; reorder via Settings → Run All Order.
# Run All pipeline: remux first, then beets tagging (MusicBrainz), then
# formatting/tagging scripts, lyric fetching (before grade sees it), and
# analysis/audit/grade at the end.
DEFAULT_RUN_ALL_ORDER = [11, 14, 1, 2, 8, 13, 12, 3, 5, 9, 6, 4, 7, 10]

# Audio tag families that can be toggled per filetype.
# Each family groups related TAG_MAP keys that are written together.
AUDIO_TAG_FAMILIES = [
    "AUDIT",          # AUDIT (Audit Library)
    "LOG_GRADE",      # LOG_GRADE (disc rip log scores)
    "REPLAYGAIN",     # REPLAYGAIN_TRACK/ALBUM_GAIN/PEAK (4 tags via rsgain)
    "DYNAMIC_RANGE",  # DYNAMIC RANGE + ALBUM DYNAMIC RANGE (simple-dr-meter)
    "MEDIA_SOURCE",   # MEDIA + SOURCE (Digital Media normalization)
    "INSTRUMENTAL",   # INSTRUMENTAL (lyrics presence)
    "ADVISORY",       # ITUNESADVISORY + ALBUMITUNESADVISORY
    "LYRICS",         # embedded LYRICS tag (and .lrc sidecar)
    "BPM",            # BPM (Key & BPM analysis)
    "INITIALKEY",     # INITIALKEY (Key & BPM analysis)
    "ENERGY",         # ENERGY (0-100, audio analysis, written with MOOD)
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
    "MOOD": "MOOD",
    "ENERGY": "ENERGY",
    # integrity tags follow AUDIT family (written alongside audit when present)
    "AUDIO_MD5": "AUDIT",
    "INTEGRITY": "AUDIT",
    "LOG_CRC": "LOG_GRADE",
}

def _audio_tag_family(tag_name):
    return _TAG_TO_FAMILY.get(str(tag_name).upper())

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
        "MOOD": "mood_enabled",
        "LYRICS": None,  # lyrics_format gates this separately
    }
    gkey = family_global.get(family)
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

DEFAULT_CONFIG = {
    # The first-run wizard supplies this; never ship a developer-specific
    # library path in the application defaults.
    "music_folder": "",

    # FLAC
    "flac_level": 8,
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
    # Cover FINDER (covers.musichoarders.xyz meta-search): which region the
    # storefront sources are queried against, and which sources to query.
    # An empty `cover_sources` means "use the providers' own enabled list in
    # the app's quality order"; the region defaults to the US storefront.
    "cover_country": "us",
    "cover_sources": [],
    # Album covers re-encode to 90% quality; other images keep max quality.
    "cover_jpeg_quality": 90,
    # Artist artwork & text pulled from the discovery providers and stored in
    # the library itself — Artists/<Artist>/artist.jpg, description.txt and
    # artist.json (provenance: provider, source URL, fetch time). Artist
    # images have no minimum resolution by default; they are only cropped to
    # the configured cover aspect and re-encoded at the cover JPEG quality.
    # A target size of 0 keeps the provider's native size.
    "artist_image_enabled": True,
    "artist_image_sources": [],       # ordered provider ids; [] = built-in order
    "artist_image_crop": True,
    "artist_image_target_size": 0,
    "artist_description_enabled": True,
    "album_description_enabled": True,
    "description_sources": [],        # ordered provider ids; [] = built-in order

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

    # Force flags used by the Run All order / individual runs — re-format even
    # when a file already looks canonical.
    "force_lyrics": False,
    "force_cue": False,

    # Music-file tag writes
    "fix_instrumental_from_lyrics": True,
    "write_audit_tag": True,
    "write_log_grade": True,
    "write_replaygain_tags": True,
    "write_dynamic_range_tags": True,

    # Grading
    "grade_verbose": True,
    # What file categories are allowed when grading an album folder. A
    # folder with files of a disallowed category fails grading. 'other' is
    # opt-in: by default any file that is not music/cover/cue/log/lrc fails.
    "grade_include_music": True,
    "grade_include_cover": True,
    "grade_include_cue": True,
    "grade_include_log": True,
    "grade_include_lrc": True,
    "grade_include_accurip": True,
    "grade_include_other": False,
    # Remuxed music videos (MKV sidecars from script 11) are allowed by default.
    "grade_include_video": True,
    # Configurable strict checks for grading (all on by default, per request)
    # These make trailing/leading spaces, blank lines, cover aspect ratio
    # (squareness) and zero timestamps count as failures for the relevant
    # file types.
    "grade_check_tag_spaces": True,
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
    "grade_check_audit": False,
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
    "grade_check_disallowed": True,
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
    # AudioAuditor for those files). When audit_cd_require_both is True
    # (now the default), BOTH the .log CRC and AudioAuditor must be REAL
    # for the final AUDIT to be REAL; if either is FAKE, the result is FAKE.
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

    # DR / ReplayGain (script 7): rsgain + simple-dr-meter.
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
    # (gated by video_reencode_incompatible). The original (e.g. the VOB) is
    # deleted after a verified remux; a stray original whose same-stem MKV
    # already exists is duration-verified and then removed as well.
    "video_reencode_incompatible": True,
    "video_crf": 18,
    "video_preset": "medium",
    "video_flac_level": 8,
    "video_remove_original": True,
    "video_process_mp4": False,

    # Music videos from YouTube (server/youtube.py, yt-dlp). 0 max height =
    # whatever the source offers (best).
    "youtube_enabled": True,
    "youtube_max_height": 0,

    # Lossless source conversion (part of script 3): uncompressed WAV /
    # AIFF (and ffmpeg-decodable APE/WV/SHN/TTA, plus ALAC in MP4) are
    # re-encoded to the target lossless codec with tags copied over. The
    # original is only removed after a verified conversion and when
    # lossless_remove_original is on.
    "optimize_convert_lossless": True,
    # Target codec every lossless file ends up in ("flac" or "alac"). FLAC is
    # what the rest of the pipeline assumes; ALAC exists for Apple-centric
    # libraries (m4a containers, no metaflac).
    "lossless_target_codec": "flac",
    "lossless_remove_original": True,

    # Auto Tagging (script 8)
    "auto_advisory": True,
    "auto_instrumental": True,
    # OFF by default: a track without a specified advisory stays untagged —
    # ITUNESADVISORY=0 is never assumed just because a track is instrumental
    # (or for any other reason). Re-enable to restore the old zero-fill.
    "auto_zero_advisory_for_instrumental": False,
    "force_auto_tag": False,

    # Key & BPM analysis (script 12): librosa-backed BPM + initial key.
    "audiometa_enabled": True,
    "audiometa_overwrite": False,
    "audiometa_min_seconds": 10,
    "audiometa_key_notation": "musical",  # musical | camelot | openkey
    "force_audiometa": False,

    # Lyrics sources, tried in order until one has the song — each provider
    # falls back to the next, and MusicBrainz-independent providers are the
    # only ones that can serve lyrics. Empty = the built-in order
    # (LRCLIB, NetEase, lyrics.ovh); see Settings → Lyrics.
    "lyrics_sources": [],
    # Accept plain (unsynced) lyrics when no provider has a synced version.
    # Off means "synced or nothing" — the canonical formatter still runs.
    "lyrics_allow_plain": True,

    # Managed beets tagging (Picard parity).
    "beets_locale": "en",
    "beets_translations": True,
    "beets_work_movement": True,
    "beets_release_type_caps": True,
    # Default True: beets only relocates AUDIO files; the follow-up organize
    # applies the naming script to filenames and gathers sidecars / covers
    # into the final album folder, which grading requires.
    "beets_organize_after": True,

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
    "acoustid_min_score": 0.75,

    # Soulseek via managed slskd (shares = music folder).
    "soulseek_username": "",
    "soulseek_password": "",
    "soulseek_description": "",
    "soulseek_listen_port": 50000,
    "soulseek_web_port": 5030,
    "soulseek_up_limit": 0,
    "soulseek_down_limit": 0,
    # slskd transfer slots (concurrent transfers) and speed limits in KiB/s
    # (0 = unlimited, emitted as slskd's int.MaxValue default).
    "soulseek_download_slots": 3,
    "soulseek_upload_slots": 2,
    "soulseek_upload_limit_kib": 0,
    "soulseek_download_limit_kib": 0,
    # slskd's HTTPS listener binds an extra port (5031) with a self-signed
    # cert by default. The app talks plain HTTP to the loopback port, so the
    # second listener is disabled unless explicitly wanted.
    "soulseek_web_https": False,
    "soulseek_download_dir": "",
    # ON by default: the Soulseek client should be up whenever the app is.
    "soulseek_autostart": True,
    # Share the library with the network on the configured listen port.
    "soulseek_share_library": True,
    # Auto-import (MusicBrainz release → Soulseek). Each template is a
    # space-separated list of release fields: artist album year date country
    # catalognumber barcode label. CD rips are searched by catalog number
    # (the only trait usually present in rip folder names); digital media
    # by title + year.
    "soulseek_auto_cd_queries": [
        "catalognumber", "artist album catalognumber", "artist album"],
    "soulseek_auto_digital_queries": ["artist album year", "artist album"],
    # Every disc's .log must score at least this (Logchecker 0-100) before
    # the full album is downloaded.
    "soulseek_auto_log_min_score": 100,
    # Fraction of the release track list a candidate folder must contain.
    "soulseek_auto_complete_ratio": 1.0,
    # How many candidates may be rejected before the search gives up on the
    # release (the first candidate that verifies is imported immediately).
    "soulseek_auto_max_attempts": 3,
    # How long to let a Soulseek search collect responses before scoring the
    # candidates (seconds). Longer = more peers + better chance of a match.
    # The real cap is this plus the search's grace tail; the no-results prompt
    # (below) fires exactly when that window ends with nothing usable.
    "soulseek_auto_search_wait": 15,
    # Park an interactive job that found no usable folder and ask the user
    # whether to add the release to the wishes list, instead of failing the job
    # outright: a rare album is worth watching for, and the background wishes
    # worker keeps searching with the queries the job already used. The
    # background path itself never asks (a wish must not be turned into a wish).
    "soulseek_auto_wish_prompt": True,
    # Release-choice policy for auto-import (single release, release group or
    # an artist's whole catalogue): prefer status=Official, never auto-pick a
    # Promotion / Bootleg / Pseudo-Release while avoid-promo is on, and order
    # the remaining editions by medium (MusicBrainz format), earliest date
    # breaking ties.
    "auto_import_avoid_promo": True,
    "auto_import_medium_order": ["CD", "Digital Media", "Vinyl", "Cassette", "Other"],
    # Explicit shared folders (empty = share the whole music folder).
    "soulseek_share_dirs": [],
    # Extra share filters — substrings/paths slskd must NOT share.
    "soulseek_share_exclude": [],

    # Wishes — MusicBrainz releases saved to the library WITHOUT downloading.
    # A background worker re-searches Soulseek for each wish on an interval
    # and auto-imports the release the moment a verified match appears.
    "wishes_enabled": True,
    "wishes_interval_hours": 6,
    "wishes_max_attempts": 0,       # 0 = retry forever
    "wishes_auto_import": True,

    # Genres imported per release/track (top voted first). Sources are tried
    # in this order and merged; see mlo/genres.py.
    "mb_genre_count": 3,
    "genre_sources": [
        "rateyourmusic", "soulseek", "discogs", "lastfm", "theaudiodb",
        "musicbrainz", "deezer", "itunes",
    ],
    # Optional keys for the genre sources that need one. Left empty the source
    # is skipped instead of guessed (Discogs' search endpoint requires a
    # token; Last.fm requires an API key). RYM answers only a real browser
    # session: paste the Cookie header of a logged-in rateyourmusic.com tab
    # (it carries Cloudflare's cf_clearance) — empty, RYM is skipped like any
    # other unavailable source. Deezer/iTunes/TheAudioDB/MusicBrainz are
    # keyless.
    "discogs_token": "",
    "lastfm_api_key": "",
    "rym_cookie": "",
    # Auto-resolve RateYourMusic album + artist links during import; off =
    # links are only ever set by hand in the link editor.
    "rym_links_auto": True,
    # Advisory (ITUNESADVISORY) auto-fetch on import: asks Deezer by ISRC
    # first, then Spotify (below), then Apple's album/song routes, and
    # MusicBrainz supplies the ISRC when the file has none. Never writes a
    # value when the providers do not state one — an absent advisory is
    # 'unrated', not 'clean'.
    "advisory_auto_fetch": True,
    # Optional Spotify Web API credentials (client-credentials flow) used as
    # the second advisory source. Empty = the source is skipped entirely; it
    # is never required and its absence can never fail an import.
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

    # Home — album recommendations loaded on the sidebar's Home section.
    "home_recommendations": True,
    "home_rec_count": 18,
    "home_recent_count": 12,
    # What drives "Recommended for you": the discovery chain (popularity-ranked
    # from Deezer + ListenBrainz, resolved to MusicBrainz release groups),
    # ListenBrainz's sitewide charts, or the plain MusicBrainz release-group
    # search.
    "home_rec_source": "discovery",
    # Size of the "Popular right now" shelf (ListenBrainz sitewide listens).
    "home_popular_count": 12,
    # Discovery — the external music APIs behind recommendations, catalogue
    # search, artist artwork and descriptions (Deezer, ListenBrainz, iTunes,
    # TheAudioDB, Wikipedia). Empty source lists = the built-in order; every
    # feature walks its list and falls back to the next provider, and
    # MusicBrainz stays the final fallback so results keep their MBIDs.
    "discovery_enabled": True,
    "discovery_rec_sources": [],
    "discovery_search_sources": [],
    "discovery_timeout_s": 8,
    # MusicBrainz browse page search: "auto" queries discovery first and falls
    # back to MusicBrainz, "discovery" never falls back, "musicbrainz" keeps
    # the plain MusicBrainz search.
    "mb_search_source": "auto",

    # Misc
    "auto_advance": True,
    # 0 means automatic. A positive value caps every module's worker pool,
    # which is useful on slower disks or shared machines.
    "worker_limit": 0,
    "run_all_order": list(DEFAULT_RUN_ALL_ORDER),

    # First run / updates
    "first_run_done": False,
    "last_update_check": 0,
    "update_check_interval_days": 7,
    # Sidecar files (cue/log/lrc/accurip) shown as extra rows in library views
    "show_sidecar_files": False,
}


_BOOL_KEYS = {
    key for key, value in DEFAULT_CONFIG.items() if isinstance(value, bool)
}
_INT_RANGES = {
    "flac_level": (0, 8),
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
    "update_check_interval_days": (1, 30),
    "cover_target_size": (0, 4000),
    "cover_jpeg_target_size": (0, 4000),
    "cover_png_target_size": (0, 4000),
    "cover_jxl_target_size": (0, 4000),
    "soulseek_auto_log_min_score": (0, 100),
    "soulseek_auto_max_attempts": (1, 50),
    "soulseek_auto_search_wait": (5, 300),
    "wishes_interval_hours": (1, 168),
    "wishes_max_attempts": (0, 1000),
    "home_rec_count": (4, 60),
    "home_recent_count": (4, 60),
    "home_popular_count": (4, 60),
    "artist_image_target_size": (0, 4000),
    "discovery_timeout_s": (3, 30),
    "import_bulk_concurrency": (1, 8),
    "mb_genre_count": (1, 10),
}
_CHOICES = {
    "lyrics_format": {"EMBEDDED", "LRC", "BOTH"},
    "lrc_zero_timestamp_target": {"EMBEDDED", "LRC", "BOTH"},
    # Sync granularity required of (and targeted for) synced lyrics:
    # SYLLABLE = glued per-syllable ELRC tags, WORD = per-word ELRC tags,
    # LINE = plain [mm:ss.xx] line timestamps only.
    "lrc_sync_level": {"SYLLABLE", "WORD", "LINE"},
    "cue_file_type": {"WAVE", "MP3"},
    "audiometa_key_notation": {"musical", "camelot", "openkey"},
    "video_preset": {"ultrafast", "superfast", "veryfast", "faster", "fast",
                     "medium", "slow", "slower", "veryslow"},
    # Discovery: which source drives recommendations / the MB-page search.
    "home_rec_source": {"discovery", "listenbrainz", "musicbrainz"},
    "mb_search_source": {"auto", "discovery", "musicbrainz"},
    "mood_source": {"audio", "provider", "hybrid"},
    "replaygain_mode": {"track", "album", "off"},
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

    try:
        last_check = float(cfg.get("last_update_check", 0) or 0)
        cfg["last_update_check"] = max(0.0, last_check)
    except (TypeError, ValueError):
        cfg["last_update_check"] = 0.0

    try:
        thr = float(cfg.get("cover_crop_threshold", 0.05))
        cfg["cover_crop_threshold"] = max(0.0, min(0.5, thr))
    except (TypeError, ValueError):
        cfg["cover_crop_threshold"] = 0.05
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
    for k in ("soulseek_auto_cd_queries", "soulseek_auto_digital_queries"):
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
    cfg["genre_sources"] = (
        [str(t).strip().lower() for t in v if str(t).strip()][:16]
        or list(DEFAULT_CONFIG["genre_sources"])
    )

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
    for k in ("discovery_rec_sources", "discovery_search_sources",
              "lyrics_sources", "artist_image_sources", "description_sources"):
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
            # ids run 1..14: script 15 (lyrics xlit/translate) was removed, so
            # a saved order still naming it sheds it right here
            if 1 <= script_id <= 14 and script_id not in clean_order:
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


def load_config() -> dict:
    _migrate_to_data_dir()
    path = active_config_file()
    user = None
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                user = json.load(f)
        except Exception:
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
            if os.path.exists(d) and not (os.path.isfile(s) and _is_empty_db(d)):
                continue
            if copy:
                if os.path.isdir(s):
                    shutil.copytree(s, d)
                else:
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
            _move_state_dir(trash_dir(prev_mf), trash_dir(new_mf))
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
        print(c(f"ERROR: Could not save config: {e}", Color.RED))
        return False
