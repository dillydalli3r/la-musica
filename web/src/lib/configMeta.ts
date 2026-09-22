/** Every config setting the app has, what it is called, how it is edited, and
 *  which setup step owns it — in one module, so the first-run wizard can offer
 *  all of it without a second hand-maintained copy of the same list.
 *
 *  The groups below are the settings tabs by name and field for field; the keys
 *  no tab owns (the per-filetype matrices, the export defaults, the server's
 *  own address, the login gate) are appended to the group that explains them,
 *  plus a few groups of their own. `tools/test_setup_coverage.py` holds the two
 *  sides together: every key of `mlo/config.py`'s DEFAULT_CONFIG must be covered
 *  here or named in `HIDDEN_KEYS`, and every key and group SettingsPage renders
 *  must exist here too — so a setting added on one side fails the other until
 *  it is accounted for. */

import { CODEC_CHOICES } from "./codecMeta";

export type CfgField =
  | { k: string; label: string; type: "bool"; help?: string }
  | { k: string; label: string; type: "number"; min?: number; max?: number; step?: number; help?: string }
  /** `options` is empty exactly when `optionsFrom` names the list to fetch
   *  (a catalogue, or the app's own locales) — the value list is not a
   *  constant the module could carry. */
  | {
      k: string;
      label: string;
      type: "select";
      options: [string, string][];
      optionsFrom?: "coverCountries" | "locales";
      help?: string;
    }
  | { k: string; label: string; type: "text"; pattern?: string; patternHelp?: string; help?: string }
  | { k: string; label: string; type: "password"; help?: string }
  /** Ordered provider preference list; an empty list means the built-in order. */
  | { k: string; label: string; type: "list"; catalog: "discovery" | "lyrics" | "covers"; help?: string }
  /** Unordered set of values (a string list in config) shown as checkboxes. */
  | { k: string; label: string; type: "multi"; options: [string, string][]; optionsFrom?: "genres"; help?: string }
  /** Comma-separated list in one text input; kept in config as a string list. */
  | { k: string; label: string; type: "csv"; help?: string }
  /** A per-filetype grid of switches: config holds `{ filetype: { row: bool } }`
   *  (audio_tag_writes, encoder_tags). */
  | {
      k: string;
      label: string;
      type: "matrix";
      rows: [string, string][];
      cols: [string, string][];
      help?: string;
    }
  /** A list of script ids, ticked in the order they run (run_all_order). */
  | { k: string; label: string; type: "chain"; help?: string };

export interface CfgGroup {
  title: string;
  blurb?: string;
  fields: CfgField[];
}

/** The group with this title. Steps name groups, never indices, so a renamed
 *  tab fails loudly here instead of silently rendering the wrong fields. */
export function cfgGroup(title: string): CfgGroup {
  const group = CONFIG_GROUPS.find((g) => g.title === title);
  if (!group) throw new Error(`no config group titled ${title}`);
  return group;
}

/** Every field of every group a step renders, in order. */
export function stepFields(step: SetupStep): CfgField[] {
  return (step.groups ?? []).flatMap((title) => cfgGroup(title).fields);
}

/** One step of the first-run wizard. A step is a panel it draws (if any) plus
 *  the config groups it renders — and every step may be skipped, so nothing
 *  here is a gate on the app working afterwards. */
export interface SetupStep {
  /** The rail's own label; short, because the rail wraps. */
  label: string;
  title: string;
  blurb: string;
  /** The panel this step draws above its groups. */
  panel?: "folder" | "dependencies" | "sources" | "slskd" | "password" | "done";
  /** CONFIG_GROUPS titles this step renders, in order. */
  groups?: string[];
}

export const CONFIG_GROUPS: CfgGroup[] = [
    {
      title: "AI — lyric transforms & genre ranking",
      blurb: "The app's only optional model, shared by script 17's lyric transforms and the genre ranking on the Import & tags tab. Script 17 romanizes non-Latin lyrics and translates them into the languages below, writing TRANSLITERATION-*/TRANSLATION-* tags (and .romaji.lrc / .<lang>.lrc sidecars for LRC/BOTH lyric formats). Any OpenAI-compatible /chat/completions endpoint works — OpenAI, OpenRouter, LM Studio, llama.cpp, or Google Gemini's OpenAI-compatible endpoint (paste the bare generativelanguage.googleapis.com host and it is routed). Nothing else in the app depends on it: with the URL or model empty, the lyric script logs one line and skips, and the genre ranking falls back to the source list. Reasoning effort is sent as `reasoning_effort` on every call — a provider that rejects the field gets one plain retry.",
      fields: [
        { k: "ai_base_url", label: "Base URL", type: "text", help: "e.g. https://api.openai.com/v1, http://localhost:1234/v1, or generativelanguage.googleapis.com" },
        { k: "ai_api_key", label: "API key", type: "password", help: "Sent as a Bearer token. Local servers (LM Studio, llama.cpp) usually ignore it — leave it empty there." },
        { k: "ai_model", label: "Model", type: "text", help: "The model id the endpoint expects, e.g. gpt-4o-mini or gemini-2.5-flash." },
        { k: "ai_effort", label: "Reasoning effort", type: "select", options: [["max","Max — the provider's highest thinking budget"],["high","High — best quality (default)"],["medium","Medium"],["low","Low"],["minimal","Minimal — no thinking, fastest"]] },
        { k: "lyrics_translation_langs", label: "Translation languages", type: "text", help: "Comma separated, e.g. en,de. The first is the reader's language (it decides when romanization is worth generating) and names the TRANSLATION tag; each language also gets its own .<lang>.lrc sidecar." },
        { k: "lyrics_xlit_enabled", label: "Transliterate non-Latin lyrics", type: "bool" },
        { k: "lyrics_translate_enabled", label: "Translate lyrics", type: "bool" },
        { k: "lyrics_xlit_sidecars", label: "Write .romaji.lrc / .<lang>.lrc sidecars (LRC formats only)", type: "bool" },
      ],
    },
    {
      title: "Library codec (script 3)",
      blurb: "The audio format the whole library ends up as — the same conversion runs on imports. By default a LOSSLESS source is converted to the target while a lossy one is left exactly as it is: lossy → lossless cannot restore a sample, and lossy → lossy is a generation loss. A converted original is moved to Trash, never deleted.",
      fields: [
        { k: "library_codec", label: "Library codec", type: "select", options: CODEC_CHOICES, help: "What every file in the library ends up as. Lossless targets: FLAC (the shipped default — what the verification tools are built around), ALAC (.m4a), WAV, AIFF. Lossy targets: MP3 and AAC (CBR), Ogg Vorbis and Opus. \"Keep\" never converts anything." },
        { k: "library_codec_optimize", label: "What the optimisation pass may convert", type: "select", options: [["lossless_to_lossy","Lossless → the target (lossy left alone) — default"],["all","Anything → the target"],["keep","Never convert"]], help: "The default converts a lossless source to the target above and leaves lossy sources alone. \"Anything\" also re-encodes lossy sources (a generation loss) — it still refuses a lossy source under a lossless target, which can only lose quality." },
        { k: "library_codec_bitrate", label: "Lossy bitrate (kbps) / Vorbis quality", type: "number", min: 0, max: 512, help: "Applies to the lossy targets only: kbps for MP3/AAC/Opus, Vorbis' own 0-10 quality scale for Ogg. 0 uses the codec's own default (MP3 320, AAC 256, Ogg 6, Opus 128)." },
        { k: "library_codec_quality", label: "Lossless compression level (FLAC 0-8)", type: "number", min: 0, max: 8, help: "Used by the FLAC encode and re-encode. ALAC and the PCM targets have no such control and ignore it." },
        { k: "library_codec_args", label: "Extra encoder arguments (appended verbatim)", type: "text", help: "flac.exe options when the target is FLAC, ffmpeg options for every other target — added after the quality/bitrate flags, so your own flag wins. They are not validated: a bad flag fails the encode of that file, and the run reports it per file." },
        { k: "lossless_remove_original", label: "Move the converted original to Trash", type: "bool" },
        { k: "add_seektables", label: "Add seektables", type: "bool" },
        { k: "flac_preserve_picture", label: "Preserve embedded picture", type: "bool" },
        { k: "flac_no_padding", label: "No padding", type: "bool" },
      ],
    },
    {
      title: "Embedded covers",
      blurb: "Off by default: optimization removes embedded art from audio files — covers live on disk as cover.* / sidecars. When on, the album cover is embedded into every track instead.",
      fields: [
        { k: "embed_covers", label: "Embed covers into audio files", type: "bool" },
        { k: "embed_cover_jpeg_quality", label: "Embedded JPEG quality (JPEG embeds only)", type: "number", min: 60, max: 100 },
        { k: "embed_cover_resolution", label: "Embedded cover max resolution (px, 0 = original)", type: "number", min: 0, max: 4000 },
      ],
    },
    {
      title: "Images (script 5)",
      blurb: "Requires libjxl in .dependencies for JPEG XL conversion.",
      fields: [
        { k: "reencode_images", label: "Re-encode images (master switch)", type: "bool" },
        { k: "rename_to_cover", label: "Rename album art to cover.*", type: "bool" },
        { k: "reencode_to_jxl", label: "Convert images to JPEG XL", type: "bool" },
        { k: "convert_jxl_back", label: "Convert JXL back to original", type: "bool" },
        { k: "images_convert_to_jpeg", label: "Convert other formats to JPEG", type: "bool" },
        { k: "images_convert_lossless_to_png", label: "Convert lossless to PNG", type: "bool" },
        { k: "remove_alpha", label: "Remove PNG alpha", type: "bool" },
        { k: "jpeg_progressive", label: "Progressive JPEG", type: "bool" },
        { k: "jpegxl_effort", label: "JPEG XL effort", type: "number", min: 1, max: 10 },
        { k: "jpegxl_distance", label: "JPEG XL distance (0 = lossless)", type: "number", min: 0, max: 2, step: 0.1 },
        { k: "images_jpeg_quality", label: "JPEG quality", type: "number", min: 70, max: 100 },
        { k: "png_optimization_level", label: "PNG optimization level", type: "number", min: 0, max: 6 },
        { k: "cover_jpeg_quality", label: "Cover JPEG quality", type: "number", min: 70, max: 100 },
        { k: "cover_auto_fetch", label: "Fetch missing covers during import", type: "bool", help: "During import, an album with no image gets cover candidates looked up (Cover Art Archive first, then Deezer/Apple). Off leaves the finder and the Grading screen's Missing cover verdict to you." },
        { k: "cover_review", label: "…and let me pick which one (off = take the best automatically)", type: "bool", help: "On: the candidates are shown on the album page for you to choose, and nothing is written until you do. Off: the best candidate is downloaded and normalised on the spot, as before. Ignored while 'Fetch missing covers during import' is off." },
        { k: "cover_resize_enabled", label: "Resize covers", type: "bool" },
        { k: "cover_target_size", label: "Cover target size (px)", type: "number", min: 0, max: 4000 },
        { k: "cover_crop_enabled", label: "Crop covers to square", type: "bool" },
        { k: "cover_crop_threshold", label: "Crop threshold (aspect deviation)", type: "number", min: 0, max: 0.5, step: 0.05 },
        { k: "cover_force_exact_size", label: "Force exact target size", type: "bool" },
        { k: "cover_enforce_size", label: "Enforce size in grading", type: "bool" },
        { k: "cover_enforce_square", label: "Enforce square in grading", type: "bool" },
        { k: "cover_jpeg_enabled", label: "Process JPEG covers", type: "bool" },
        { k: "cover_png_enabled", label: "Process PNG covers", type: "bool" },
        { k: "cover_jxl_enabled", label: "Process JXL covers", type: "bool" },
        { k: "cover_jpeg_target_size", label: "JPEG cover size override (0 = global)", type: "number", min: 0, max: 4000 },
        { k: "cover_png_target_size", label: "PNG cover size override (0 = global)", type: "number", min: 0, max: 4000 },
        { k: "cover_jxl_target_size", label: "JXL cover size override (0 = global)", type: "number", min: 0, max: 4000 },
        { k: "cover_sources", label: "Cover finder sources (order)", type: "list", catalog: "covers", help: "The order the cover finder asks its providers in; unticked sources are never asked. Blank = every source, in the built-in order." },
        { k: "cover_country", label: "Cover finder region", type: "select", options: [], optionsFrom: "coverCountries", help: "The region whose releases the finder prefers — artwork often differs per country." },
      ],
    },
    {
      title: "Lyrics & CUEs (scripts 1, 2 & 18)",
      fields: [
        { k: "optimize_lrc", label: "Optimize .lrc sidecars", type: "bool" },
        { k: "optimize_embedded_lyrics", label: "Optimize embedded lyrics", type: "bool" },
        { k: "lyrics_sources", label: "Lyrics providers (order)", type: "list", catalog: "lyrics", help: "Providers are tried top to bottom when lyrics are fetched from an album, artist or track page. Providers left out of the list are never used." },
        { k: "lyrics_allow_plain", label: "Accept plain (unsynced) lyrics", type: "bool", help: "Off by default: every provider in the chain answers with timestamps, and an answer without them is thrown away as if it had none. Turn this on only to let untimed text (LRCLIB's plain records) through when nothing synced exists." },
        { k: "lrclib_auto_publish", label: "Auto-publish missing lyrics to LRCLIB", type: "bool", help: "Script 18 (and every import chain that includes it) submits this library's own lyrics to LRCLIB for tracks the database does not have yet — artist, title, album and duration decide that, and a track LRCLIB already answers for is never touched. Outward-facing: with it off, nothing is ever submitted automatically (the manual 'Publish to LRCLIB' button on the lyrics editor still works)." },
        { k: "lyrics_youtube_captions", label: "Use YouTube captions (yt-dlp)", type: "bool", help: "Time-synced captions, but only for tracks that carry a YouTube id — the id the video download records — so this never searches YouTube for a track. Automatic captions are used when a video has no typed subtitles and can mishear; needs yt-dlp under Dependencies, otherwise the provider is skipped." },
        { k: "lrc_timestamp_precision", label: "Timestamp precision (decimals)", type: "number", min: 2, max: 3 },
        { k: "lrc_strip_metadata", label: "Strip metadata tags ([ti:], [ar:])", type: "bool" },
        { k: "lrc_collapse_blank_lines", label: "Collapse blank lines", type: "bool" },
        { k: "lrc_enhanced_enabled", label: "Enhanced LRC (word timestamps)", type: "bool" },
        { k: "lrc_enhanced_word_sync", label: "Enhanced LRC word sync", type: "bool" },
        { k: "lrc_sync_level", label: "Required lyrics sync level", type: "select", options: [["LINE","Line timestamps only (default)"],["WORD","Word"],["SYLLABLE","Syllable"]] },
        { k: "lrc_extended_enabled", label: "Extended LRC (E-LRC)", type: "bool" },
        { k: "lrc_add_zero_timestamp", label: "Add [00:00.00] opening line", type: "bool" },
        { k: "lrc_zero_timestamp_blank", label: "Zero timestamp is blank line", type: "bool" },
        { k: "lrc_zero_timestamp_target", label: "Zero timestamp target", type: "select", options: [["EMBEDDED","Embedded"],["LRC","LRC sidecar"],["BOTH","Both"]] },
        { k: "append_final_newline", label: "Append final newline", type: "bool" },
        { k: "keep_empty_cue_lines", label: "Keep empty CUE lines", type: "bool" },
        { k: "keep_other_cue_lines", label: "Keep non-track CUE lines", type: "bool" },
        { k: "keep_empty_accurip_lines", label: "Keep empty .accurip lines", type: "bool" },
        { k: "cue_file_type", label: "CUE file type", type: "select", options: [["WAVE","WAVE"],["MP3","MP3"]] },
      ],
    },
    {
      title: "DR / ReplayGain (script 7)",
      fields: [
        { k: "dr_replaygain_enabled", label: "Enabled", type: "bool" },
        { k: "replaygain_mode", label: "Gain mode", type: "select", options: [["track","Track gain"],["album","Album gain"],["off","Off — no gain applied"]] },
        { k: "replaygain_preamp_db", label: "Preamp (dB)", type: "number", min: -24, max: 24, step: 0.5 },
        { k: "replaygain_analyze_missing", label: "Measure tracks without ReplayGain tags instead of playing them at unity", type: "bool" },
        { k: "replaygain_clip_protection", label: "Clip protection", type: "bool" },
        { k: "replaygain_skip_existing", label: "Skip files that already have RG tags", type: "bool" },
      ],
    },
    {
      title: "Audit (script 6)",
      blurb: "AudioAuditor verdict + CD rip verification (REAL/FAKE).",
      fields: [
        { k: "audit_thorough", label: "Thorough mode (full-track detectors)", type: "bool" },
        { k: "audit_cutoff_allow", label: "Frequency cutoff allowance (Hz, 0 = default)", type: "number", min: 0, max: 24000 },
        { k: "audit_verify_cd_checksums", label: "Verify CD .log CRC checksums", type: "bool" },
        { k: "audit_cd_require_both", label: "Require log CRC AND auditor for REAL", type: "bool" },
        { k: "audit_integrity", label: "Verify file integrity (flac -t / decode)", type: "bool" },
        { k: "audit_fail_on_unscorable_log", label: "Fail on unscorable .log", type: "bool" },
        { k: "audit_verify_log_checksum", label: "Verify .log checksum", type: "bool" },
        { k: "audit_require_accuraterip", label: "Require AccurateRip data", type: "bool" },
        { k: "audit_log_score_threshold", label: "Log score threshold", type: "number", min: 0, max: 100 },
        { k: "audit_batch_size", label: "Batch size", type: "number", min: 50, max: 500 },
        { k: "audit_batch_timeout_s", label: "Batch timeout (s)", type: "number", min: 10, max: 120 },
        { k: "audit_per_file_timeout_s", label: "Per-file timeout (s)", type: "number", min: 10, max: 60 },
        { k: "audit_clipping", label: "Detect clipping", type: "bool" },
        { k: "audit_scaled_clipping", label: "Detect scaled clipping", type: "bool" },
        { k: "audit_mqa", label: "Detect MQA", type: "bool" },
        { k: "audit_ai", label: "Detect upscaled audio", type: "bool" },
        { k: "audit_fake_stereo", label: "Detect fake stereo", type: "bool" },
        { k: "audit_silence", label: "Detect silence", type: "bool" },
        { k: "audit_dynamic_range", label: "Measure dynamic range", type: "bool" },
        { k: "audit_true_peak", label: "Measure true peak", type: "bool" },
        { k: "audit_lufs", label: "Measure LUFS", type: "bool" },
        { k: "audit_bpm", label: "Measure BPM", type: "bool" },
        { k: "audit_check_cd_format", label: "Verify CD format (16/44.1)", type: "bool" },
      ],
    },
    {
      title: "AutoTag (script 8)",
      fields: [
        { k: "auto_advisory", label: "Set advisory automatically", type: "bool" },
        { k: "advisory_auto_fetch", label: "Fetch the advisory rating automatically (import + advisory fetch)", type: "bool" },
        { k: "advisory_ai_classify", label: "Judge the lyrics with the AI provider", type: "bool", help: "The configured AI model reads the track's lyrics (embedded, else the .lrc sidecar) and rates the SONG — excessive profanity, a slur or a very strong word, or graphic sex/violence/drug use is 1; a mild word in passing is 0; 3 = it cannot tell, which falls through. It is asked whether or not a provider stated a value, and its answer is ranked with theirs (an AI 1 beats a stated 0 or 2, a stated 1 survives it). Needs an AI base URL and model in the AI section; with no AI configured the word scan below answers instead." },
        { k: "advisory_lyrics_scan", label: "Scan the lyrics for profanity", type: "bool", help: "The multilingual word list. Only a STRONG term makes a track explicit (1); its mild tier (ass, arse, culo, arsch, reet) is reported in the reply's hits and never decides, so a song carrying one in passing is 0. Runs when the AI is not configured (or could not tell) and the track has lyrics." },
        { k: "advisory_fallback", label: "When nothing states an advisory", type: "select", options: [["0","Store 0 — not explicit"],["2","Store 2 — clean edition"],["none","Store nothing — leave it unrated"]], help: "The last resort for a track with no provider answer, no AI answer and no lyrics evidence." },
        { k: "mood_enabled", label: "Write mood tags", type: "bool" },
        { k: "genre_autofill", label: "Trim genres to the configured count", type: "bool", help: "Genres are never imported by a script: the import (MusicBrainz/RateYourMusic per track, then the configured sources) and manual edits are the only writers. Script 8 only brings a list longer than the genre count back down to it." },
        { k: "mood_source", label: "Mood source", type: "select", options: [["audio","Audio analysis"],["provider","Provider metadata"],["hybrid","Hybrid — audio, trusting the genre when the audio is ambiguous"]] },
        { k: "auto_instrumental", label: "Set INSTRUMENTAL automatically", type: "bool" },
        { k: "auto_zero_advisory_for_instrumental", label: "Zero advisory on instrumentals (no words, no explicit content)", type: "bool" },
        { k: "fix_instrumental_from_lyrics", label: "Fix INSTRUMENTAL from lyrics", type: "bool" },
        { k: "instrumental_auto_fetch", label: "Look the INSTRUMENTAL verdict up when nothing states one", type: "bool" },
      ],
    },
    {
      title: "Release tracklist (script 15)",
      fields: [
      ],
    },
    {
      title: "AccurateRip (script 9)",
      fields: [
        { k: "write_accurip_files", label: "Write .accurip files", type: "bool" },
      ],
    },
    {
      title: "Tag writes (global switches)",
      blurb: "Which tag families may be written at all. Per-filetype overrides and encoder marker tags are below.",
      fields: [
        { k: "write_audit_tag", label: "Write AUDIT verdicts", type: "bool" },
        { k: "write_log_grade", label: "Write LOG_GRADE scores", type: "bool" },
        { k: "write_replaygain_tags", label: "Write ReplayGain tags", type: "bool" },
        { k: "write_dynamic_range_tags", label: "Write DR tags", type: "bool" },
        { k: "write_rating_tags", label: "Write RATING tags (your stars)", type: "bool",
          help: "Rating a track also writes RATING (0-100, Picard's scale: one half-star = 10) into the file, "
                + "and clearing a rating removes the tag. Off, ratings stay in the app only. Keeping it on is what "
                + "makes an imported Picard-rated library and this app agree." },
        { k: "normalize_media_source", label: "Normalize MEDIA / SOURCE", type: "bool" },
        { k: "strip_source_on_cd", label: "Strip SOURCE on CD rips", type: "bool" },
        { k: "fill_empty_source", label: "Fill empty SOURCE on digital", type: "bool" },
        { k: "digital_media_source_value", label: "Digital SOURCE value", type: "text" },
        { k: "audio_tag_writes", label: "Tag writes per file type", type: "matrix", rows: [["AUDIT", "AUDIT"], ["LOG_GRADE", "LOG_GRADE"], ["REPLAYGAIN", "ReplayGain"], ["DYNAMIC_RANGE", "DR"], ["MEDIA_SOURCE", "MEDIA / SOURCE"], ["INSTRUMENTAL", "INSTRUMENTAL"], ["ADVISORY", "ADVISORY"], ["LYRICS", "Lyrics"], ["GENRE", "Genre"], ["BPM", "BPM"], ["INITIALKEY", "Key"], ["MOOD", "MOOD"], ["ENERGY", "ENERGY"]], cols: [["flac", "FLAC"], ["mp3", "MP3"], ["mp4", "M4A"], ["ogg", "OGG"], ["opus", "Opus"], ["aac", "AAC"]], help: "Unchecking a family for a file type means that tag is never written into those files, whatever the switches above say." },
        { k: "encoder_tags", label: "Encoder marker tags per lossless format", type: "matrix", rows: [["ENCODER_PROGRAM", "Program"], ["ENCODER_QUALITY", "Quality"], ["ENCODER_VERSION", "Version"]], cols: [["flac", "FLAC"], ["jpeg", "JPEG"], ["png", "PNG"], ["jxl", "JXL"]] },
      ],
    },
    {
      title: "CD Rips (scripts 2/6/9/10)",
      blurb: "Deterministic CD-N renaming of .log/.cue/.accurip and conservative CUE FILE-name fixes.",
      fields: [
        { k: "discs_rename_enabled", label: "Auto-rename disc sheets to CD-{n}", type: "bool" },
        { k: "discs_rename_single_fallback", label: "Rename lone sheet in single-disc album", type: "bool" },
        { k: "discs_rename_pattern", label: "Rename pattern (must contain {n})", type: "text", pattern: "\\{n\\}", patternHelp: "the sheet name must contain {n} — that is where the disc number goes" },
        { k: "discs_toc_tolerance_s", label: "TOC tolerance (s)", type: "number", min: 0.5, max: 10, step: 0.5 },
        { k: "discs_toc_unique_margin_s", label: "TOC unique margin (s)", type: "number", min: 0.5, max: 10, step: 0.5 },
        { k: "cue_fix_filenames", label: "Fix CUE FILE lines to real files", type: "bool" },
      ],
    },
    {
      title: "Grading (script 4)",
      blurb: "Which file types are allowed in an album and which checks count as failures.",
      fields: [
        { k: "grade_include_music", label: "Allow audio files", type: "bool" },
        { k: "grade_include_cover", label: "Allow cover images", type: "bool" },
        { k: "grade_include_cue", label: "Allow CUE files", type: "bool" },
        { k: "grade_include_log", label: "Allow LOG files", type: "bool" },
        { k: "grade_include_lrc", label: "Allow LRC files", type: "bool" },
        { k: "grade_include_accurip", label: "Allow .accurip files", type: "bool" },
        { k: "grade_include_description", label: "Allow album description files (description.txt)", type: "bool" },
        { k: "grade_include_video", label: "Allow remuxed videos (MKV)", type: "bool" },
        { k: "grade_include_other", label: "Allow other files", type: "bool" },
        { k: "grade_check_raw_video", label: "Fail un-remuxed videos (VOB/AVI...)", type: "bool" },
        { k: "grade_check_lossless_source", label: "Fail uncompressed sources (WAV...)", type: "bool" },
        { k: "grade_log_score_threshold", label: "Log score threshold", type: "number", min: 0, max: 100 },
        { k: "grade_check_log_checksum", label: "Check log checksum", type: "bool" },
        { k: "grade_check_accuraterip", label: "Check AccurateRip", type: "bool" },
        { k: "grader_cover_size_tolerance_px", label: "Cover size tolerance (px)", type: "number", min: 0, max: 5 },
        { k: "grader_strict_square_threshold", label: "Strict square threshold", type: "number", min: 0, max: 0.05, step: 0.005 },
        { k: "grade_verbose", label: "Verbose grading diagnostics", type: "bool" },
      ],
    },
    {
      title: "Videos (script 11)",
      blurb: "Lossless remux: any video container → MKV with the video copied bit-exact and lossless audio converted to FLAC (level below); lossy audio (AC3/DTS/AAC) is copied rather than inflated into FLAC unless that is turned off. Captions/subtitles are always kept and verified — never removed. If the muxer refuses the video codec, H.264 is a last-resort fallback (off by default: it re-encodes the only copy). The original (e.g. the .VOB) is removed after a verified remux.",
      fields: [
        { k: "youtube_enabled", label: "Fetch missing music videos from YouTube", type: "bool", help: "The master switch for every YouTube download: the album header's film button and a track's \"Download music video\" action both refuse to search while it is off. Script 11 itself never searches YouTube — it remuxes the video files that are already in the folder." },
        { k: "youtube_max_height", label: "Maximum video height (px, 0 = best available)", type: "number", min: 0, max: 4320 },
        {
          k: "youtube_cookies_mode", label: "Cookies for YouTube", type: "select",
          options: [["none", "None — anonymous"], ["file", "A cookies file saved below"], ["browser", "Read from a browser"]],
          help: "Your own YouTube session is the only thing that opens an age-gated or members-only video — without it YouTube answers \"Sign in to confirm your age\" — and it stops the throttling a fresh IP gets. None: yt-dlp asks anonymously and no cookie is ever sent. A cookies file: yt-dlp reads the jar saved in the box below this field (paste it or drop the cookies.txt on it). Read from a browser: yt-dlp opens that browser's own cookie store, which needs the browser on the same machine and signed in to YouTube — close it first if the store is locked.",
        },
        {
          k: "youtube_cookies_browser", label: "Browser to read cookies from", type: "select",
          options: [["chrome", "Chrome"], ["chromium", "Chromium"], ["edge", "Edge"], ["firefox", "Firefox"], ["brave", "Brave"], ["opera", "Opera"], ["safari", "Safari"], ["vivaldi", "Vivaldi"], ["whale", "Whale"]],
          help: "Which browser \"Read from a browser\" opens. It reads the browser's DEFAULT profile, so pick the one you are signed in to YouTube with; a profile the app cannot decrypt (Chrome's cookie encryption on another OS user) simply yields no cookies and the download stays anonymous.",
        },
        { k: "video_reencode_incompatible", label: "Allow H.264 video fallback (lossy re-encode, last resort)", type: "bool" },
        { k: "video_lossy_audio_copy", label: "Copy lossy audio streams instead of re-encoding to FLAC", type: "bool" },
        { k: "video_crf", label: "H.264 CRF (lower = better)", type: "number", min: 0, max: 51 },
        { k: "video_preset", label: "H.264 preset", type: "select", options: [["ultrafast","ultrafast"],["superfast","superfast"],["veryfast","veryfast"],["faster","faster"],["fast","fast"],["medium","medium"],["slow","slow"],["slower","slower"],["veryslow","veryslow"]] },
        { k: "video_flac_level", label: "FLAC compression (0-8)", type: "number", min: 0, max: 8 },
        { k: "video_remove_original", label: "Remove original after verified remux", type: "bool" },
        { k: "video_process_mp4", label: "Also re-mux MP4s into MKV", type: "bool" },
      ],
    },
    {
      title: "Key & BPM (script 12)",
      blurb: "Detects tempo and musical key with librosa and writes BPM + INITIALKEY. Install librosa from the Dependencies tab.",
      fields: [
        { k: "audiometa_enabled", label: "Enabled", type: "bool" },
        { k: "audiometa_overwrite", label: "Overwrite existing BPM/key values", type: "bool" },
        { k: "audiometa_min_seconds", label: "Skip tracks shorter than (seconds)", type: "number", min: 1, max: 120 },
        { k: "audiometa_key_notation", label: "Key notation", type: "select", options: [["musical","Musical (A min)"],["camelot","Camelot (8A)"],["openkey","Open Key (1m)"]] },
      ],
    },
    {
      title: "Soulseek (managed slskd)",
      blurb: "Shares = the library folder (<music folder>/Artists). Downloads land in the download dir below; use the Soulseek page to search and download. Install slskd from the Dependencies tab.",
      fields: [
        { k: "soulseek_username", label: "Soulseek username", type: "text" },
        { k: "soulseek_password", label: "Soulseek password", type: "password" },
        { k: "soulseek_description", label: "Profile description (shown to other users)", type: "text" },
        { k: "soulseek_listen_port", label: "Listen port", type: "number", min: 1024, max: 65535 },
        {
          k: "soulseek_upnp", label: "Open the listen port on the router automatically", type: "bool",
          help: "Asks the router to forward the listen port to this machine whenever slskd starts, replaces the mapping when the port changes, and removes it when this is turned off. Without a router that answers UPnP or NAT-PMP it does nothing at all — the Soulseek page's port state names which it was: mapped, refused (with the router's own reason), or no gateway answered. Turn it off when you forward the port yourself.",
        },
        { k: "soulseek_web_port", label: "Web/API port", type: "number", min: 1024, max: 65535 },
        { k: "soulseek_up_limit", label: "Upload speed limit (kB/s, 0 = unlimited)", type: "number", min: 0, max: 100000 },
        { k: "soulseek_down_limit", label: "Download speed limit (kB/s, 0 = unlimited)", type: "number", min: 0, max: 100000 },
        {
          k: "soulseek_download_slots", label: "Concurrent download slots (slskd)", type: "number", min: 1, max: 20,
          help: "How many transfers slskd runs at once — the OUTER ceiling, and the only one of the three numbers that is slskd's rather than this app's. The app enforces `Releases … at once` × `Candidate downloads per release` itself; at the shipped defaults that product is 3 × 3 = 9, which is why this defaults to 9. Set it below the product and the app narrows each release's batch to fit (`slots ÷ releases`), so nothing you configure here ends up queued inside slskd.",
        },
        { k: "soulseek_upload_slots", label: "Concurrent upload slots (0 = unlimited)", type: "number", min: 0, max: 20 },
        { k: "soulseek_upload_limit_kib", label: "Per-transfer upload limit (KiB/s, 0 = unlimited)", type: "number", min: 0, max: 1000000 },
        { k: "soulseek_download_limit_kib", label: "Per-transfer download limit (KiB/s, 0 = unlimited)", type: "number", min: 0, max: 1000000 },
        { k: "soulseek_web_https", label: "Serve the slskd web UI over HTTPS (extra listener, self-signed)", type: "bool" },
        { k: "soulseek_download_dir", label: "Download dir (blank = <music folder>/.mlo/downloads)", type: "text" },
        {
          k: "soulseek_clear_downloads", label: "Delete the downloaded copy after a successful import", type: "bool",
          help: "The album is moved into the library first, so the downloaded folder is only staging — deleting it avoids a second full copy of everything you acquire. Only ever the folder the job itself downloaded into, and only after the import reported success: a FAILED import keeps its files so it can be retried without downloading them again.",
        },
        { k: "soulseek_autostart", label: "Start slskd with the app backend", type: "bool" },
        { k: "soulseek_share_library", label: "Share the library folder on the network", type: "bool" },
        { k: "soulseek_share_dirs", label: "Extra shared folders (; separated, blank = the library folder <music>/Artists)", type: "text" },
        { k: "soulseek_share_exclude", label: "Never share these paths (; separated)", type: "text" },
      ],
    },
    {
      title: "Auto-import (MusicBrainz → Soulseek)",
      blurb: "Search terms are templates of release fields (artist album year date country catalognumber barcode label). Physical pressings — CDs included — are found by their catalog number and barcode, digital media by title + year; every disc's .log must reach the score threshold before the album downloads.",
      fields: [
        { k: "soulseek_auto_physical_queries", label: "Physical query templates (; separated)", type: "text", help: "A physical pressing — a CD included — is searched by its catalog number and barcode by default, the traits that name the exact pressing. Add templates (semicolon-separated) to widen the search; a pressing that states neither falls back to its label and country, never to an artist/title query (which asks the network for every other pressing of the album)." },
        { k: "soulseek_auto_cd_queries", label: "CD query templates (; separated)", type: "text", help: "Wins for a CD you set it for: this CD is searched by these templates instead of the physical ones above. Blank follows the physical defaults (catalog number + barcode) — the shipped default here (the catalog number alone) was a catalog-number-only search, which the physical default already covers." },
        { k: "soulseek_auto_digital_queries", label: "Digital query templates (; separated)", type: "text", help: "Digital Media is searched by these — artist, album and year by default — because it carries no pressing trait to be identified by." },
        { k: "soulseek_auto_log_min_score", label: "Min .log score (0–100)", type: "number", min: 0, max: 100 },
        { k: "soulseek_auto_complete_ratio", label: "Required track completeness (0.5–1)", type: "number", min: 0.5, max: 1, step: 0.05 },
        { k: "soulseek_auto_search_wait", label: "Fallback search window (seconds of quiet on a rare album)", type: "number", min: 5, max: 300 },
        { k: "soulseek_auto_response_limit", label: "Responses before a search is scored (5–500)", type: "number", min: 5, max: 500, help: "slskd only hands back a search's results once it has ENDED, and a popular album never goes quiet — this ends the search early instead of waiting out the whole window. Lower = faster and fewer peers; higher = slower and more candidates." },
        { k: "auto_import_avoid_promo", label: "Never auto-import promotional / bootleg editions", type: "bool" },
        { k: "auto_import_require_country", label: "Only auto-import editions with a release country", type: "bool", help: "A MusicBrainz release without RELEASECOUNTRY is usually an unsorted import, and the CD query templates are built from that field — such editions are skipped, and a group whose only editions lack one is reported as ineligible instead." },
        { k: "auto_import_medium_order", label: "Medium preference (comma-separated, best first)", type: "csv", help: "Editions are ranked by this media order first, then by how close the edition is to the release group's original date; a format not named here ranks after every configured one. Blank = the built-in order (CD, Vinyl, Cassette, Other, Digital Media) — CD first, other physical media next, digital last." },
        { k: "prefer_release_country", label: "Preferred release country (ISO code, blank = none)", type: "text", help: "The spelling MusicBrainz publishes on the release, e.g. US or GB. A tie-breaker only: it never outranks status, medium, track count or the original-edition rule." },
        { k: "prefer_original_edition", label: "Prefer the original (explicit) edition over a clean or edited one", type: "bool", help: "MusicBrainz states this in the release title or its disambiguation comment. Off, a clean edition is ranked on the other rules like any other — a clean edition may carry altered audio." },
      ],
    },
    {
      title: "Wishes (auto-fill)",
      blurb: "Releases saved to the library without downloading them. The background worker re-searches Soulseek for every open wish on the interval below and imports a release the moment a verified match appears.",
      fields: [
        {
          k: "soulseek_candidate_slots", label: "Candidate downloads per release", type: "number", min: 1, max: 20,
          help: "How many candidate peers of ONE release may download at the same time (3 by default). The first that verifies good becomes the import and the others are cancelled and swept, and the NEXT candidate is only asked for when one of them lands or fails — so however many candidates a search turns up, one release never talks to more peers than this. Enforced by the app's own enqueueing; slskd's download slots (Soulseek group above) are only the outer ceiling on the transfers it produces.",
        },
        {
          k: "soulseek_search_concurrency", label: "Releases searched / downloaded at once", type: "number", min: 1, max: 8,
          help: "Over this ceiling a release is NOT refused: it takes its place in the queue (Queue → Waiting, with its position) and starts by itself the moment one of the running releases finishes. The wishes worker fills up to this many wishes per pass, and a bulk auto-import run keeps this many jobs in flight. What it does not do on its own is open more connections: that is what `soulseek_candidate_slots` (per release) and slskd's own download slots add up to.",
        },
        { k: "wishes_enabled", label: "Run the wishes worker", type: "bool" },
        { k: "wishes_interval_hours", label: "Search interval (hours)", type: "number", min: 1, max: 168 },
        { k: "wishes_max_attempts", label: "Max attempts per wish (0 = forever)", type: "number", min: 0, max: 1000 },
        { k: "wishes_not_found_attempts", label: "Empty searches before a wish ends as not-found (0 = never give up)", type: "number", min: 0, max: 1000, help: "A wish that many searched-and-found-nothing turns ends as not-found: it is notified once and only a manual retry searches again. 0 never gives up." },
        { k: "wishes_retry_backoff_minutes", label: "Extra wait before retrying after a transient failure (minutes, doubles per attempt, 0 = none)", type: "number", min: 0, max: 1440, help: "A refused slskd, a MusicBrainz outage or a failed verification is not the album being unavailable — the wish waits this long before the next try, doubling each attempt up to 24 hours." },
        { k: "wishes_auto_import", label: "Auto-import when a verified match is found", type: "bool" },
        { k: "soulseek_auto_wish_prompt", label: "Keep searching wishes automatically while the app runs", type: "bool" },
      ],
    },
    {
      title: "Home",
      blurb: "The Home section in the sidebar — its shelves are built from the library itself (recently added, best graded, top artists, favorites, wants, needs attention) with nothing fetched online.",
      fields: [
        { k: "home_recent_count", label: "Recently-added albums shown", type: "number", min: 4, max: 60 },
      ],
    },
    {
      title: "Discovery",
      blurb: "Online providers (Deezer, ListenBrainz, MusicBrainz, Last.fm, Wikipedia…) used for artist images and album/artist descriptions. Each order list is tried top to bottom; the first provider with a usable answer wins. An empty list means the built-in order shown as the placeholder.",
      fields: [
        { k: "artist_image_sources", label: "Artist image sources (order)", type: "list", catalog: "discovery", help: "Used when fetching an artist image automatically; the picked image can still be overridden per artist." },
        { k: "description_sources", label: "Description sources (order)", type: "list", catalog: "discovery", help: "Used for artist and album descriptions." },
        { k: "discovery_timeout_s", label: "Request timeout (s)", type: "number", min: 3, max: 30 },
        { k: "rym_cookie", label: "RateYourMusic cookie", type: "password", help: "Only needed when RYM answers with a challenge. Sign in to rateyourmusic.com, press F12 → Network → reload → click any request to rateyourmusic.com → Headers → Request Headers → copy everything after \"Cookie:\" and paste it here (newlines and the \"Cookie:\" label are handled for you). Settings' Discovery tab can also import it: a \"cookies.txt\" browser extension (the Firefox one is Rob W's cookies.txt) exports the cookies of a signed-in rateyourmusic.com profile, and only its rateyourmusic.com cookies land here — RYM's `session` cookie is HttpOnly, so that export is the only way to get it out of a browser at all. It is a session credential — do not share it, and paste a fresh one when RYM starts refusing, since signing out or clearing cookies invalidates it. Blank = RYM is skipped like any other unavailable source; MusicBrainz still resolves RYM links for well-known releases. Test it with the Sources panel's Test button." },
        { k: "rym_links_auto", label: "Auto-find RateYourMusic links", type: "bool", help: "Asks rateyourmusic.com for the album and artist pages during an import (and from the link editor's Auto-find button). An existing link is never overwritten, and when RYM refuses the request the import carries on untouched — the link is then left for you to paste by hand." },
        { k: "rym_archive_fallback", label: "Read archived RateYourMusic pages when the live site refuses", type: "bool", help: "With no cookie — or one RYM no longer accepts — the Wayback Machine is asked for the release page instead. An archived page can predate the release, so its genre list may be short; the live site is always tried first." },
        { k: "spotify_client_id", label: "Spotify client ID (optional)", type: "text", help: "Optional second advisory source (Spotify's ISRC lookup) behind Deezer and ahead of Apple. Empty = Spotify is skipped; an import never fails without it." },
        { k: "spotify_client_secret", label: "Spotify client secret (optional)", type: "password", help: "Pairs with the client ID above — both are needed before the Spotify lookup runs." },
        { k: "discogs_token", label: "Discogs token (optional)", type: "password", help: "A personal access token from discogs.com/settings/developers. Used to rate a release while a genre import runs; without it Discogs is skipped." },
        { k: "lastfm_api_key", label: "Last.fm API key (optional)", type: "password", help: "A free API key from last.fm/api. Only used by the discovery providers; without it Last.fm is skipped." },
        { k: "discovery_enabled", label: "Use online discovery providers", type: "bool", help: "Off, the Home shelves and every artist/album lookup answer from the library and MusicBrainz alone: no Deezer, ListenBrainz, Last.fm or Wikipedia request leaves the machine." },
      ],
    },
    {
      title: "Artist images & descriptions",
      blurb: "Artwork and text that live next to the audio: artist photos stored with the artist, and descriptions stored in non-destructive tags. Grading can require them (see the Grading tab).",
      fields: [
        { k: "metadata_auto_fetch", label: "Fetch artist image / descriptions on import", type: "bool" },
        { k: "metadata_review", label: "Review metadata candidates before writing them", type: "bool" },
        { k: "artist_image_enabled", label: "Fetch artist images", type: "bool" },
        { k: "artist_image_crop", label: "Crop artist images to the configured aspect", type: "bool", help: "Off keeps whatever shape the provider served. The aspect below is judged and cropped only while this is on and covers are configured with an aspect at all (cover_crop_enabled)." },
        { k: "artist_image_aspect", label: "Artist image aspect (W:H)", type: "text", pattern: "^\\s*\\d+(\\.\\d+)?\\s*[:x/×]\\s*\\d+(\\.\\d+)?\\s*$", patternHelp: "width:height, e.g. 1:1 (square), 4:5, 16:9", help: "The shape artist images are stored in, e.g. 1:1. The fetch crops to it, grading fails an image further than 2% from it, and script 19 (Optimize artist images) crops the ones already in the library back to it." },
        { k: "artist_image_target_size", label: "Artist image max size (px, 0 = keep native size)", type: "number", min: 0, max: 4000 },
        { k: "artist_description_enabled", label: "Fetch artist descriptions", type: "bool" },
        { k: "album_description_enabled", label: "Fetch album descriptions", type: "bool" },
        { k: "description_full", label: "Fetch the full description text (not just the summary)", type: "bool" },
      ],
    },
    {
      title: "Import pipeline",
      blurb: "What happens after an album lands in the library (Soulseek downloads, Drag & drop, Finish import). The script chain below runs in order; leaving it blank runs the built-in chain: dedupe → sort → tag → covers → lyrics → audit → ReplayGain → AccurateRip. AcoustID fingerprints the audio to identify the exact release — it needs a free application key from acoustid.org; without one, matching falls back to title/artist/genre against MusicBrainz.",
      fields: [
        { k: "auto_acquisition_enabled", label: "Automatic acquisition (searching and downloading on their own)", type: "bool", help: "Off, nothing the app starts by itself searches or downloads: the wishes worker stops its passes, an artist watch queues nothing, and \"Add to library\" records the album and its wish without starting a download. What you asked for is still recorded and a check says the switch is off rather than \"nothing found\" — the wish's own Search now, the wizard and the Soulseek page still work, because those are you acting, not the app." },
        { k: "manual_import_enabled", label: "Manual importing (the wizard and POST /api/import/*)", type: "bool", help: "Off, the import wizard and every importing /api/import/* route refuse with a sentence naming this setting instead of importing — the wizard shows that sentence where its steps would be. The automatic pipeline still imports what it downloads; only the paths you drive by hand are turned off." },
        { k: "import_autonomy", label: "Import autonomy", type: "select", options: [["automatic", "Automatic — decide everything the sources can answer (default)"], ["review", "Review — stop at each step that needs a decision"]], help: "Automatic runs the whole chain and only comes back to you for what nothing could supply: the album is imported either way and whatever it still lacks is reported as ONE prompt — a notification plus an entry the Import page lists — naming the families and linking to the album at the step where each decision is made. Review is the wizard's own behaviour applied to an import: it stops before the first step that needs a decision (a family the album is still missing) and hands the album over instead of deciding past it." },
        { k: "import_review_families", label: "Decide by hand, even when automatic", type: "multi", options: [["links", "Links"], ["cover", "Cover art"], ["genres", "Genres"], ["lyrics", "Lyrics"], ["advisory", "Advisory"]], help: "Families an import must never decide for you, whatever the mode above. A cover kept here has its candidates staged instead of writing the first hit; the links and advisory fetches are skipped; lyrics drop out of the chain. The rest of the import stays automatic, and the album's prompt names that family as waiting for you rather than as unsourced." },
        { k: "import_auto_scripts", label: "Run the script chain after import", type: "bool" },
        { k: "import_scripts", label: "Import script ids (e.g. 1, 3, 5, 7 — blank = built-in chain)", type: "text", pattern: "^(\\s*\\d+\\s*[,;]?)*$", patternHelp: "comma-separated script ids, e.g. 1, 3, 5, 7" },
        { k: "import_bulk_concurrency", label: "Bulk import concurrency", type: "number", min: 1, max: 8 },
        { k: "import_acoustid", label: "Fingerprint with AcoustID", type: "bool" },
        { k: "acoustid_enabled", label: "AcoustID enabled", type: "bool" },
        { k: "acoustid_api_key", label: "AcoustID application key (free, acoustid.org)", type: "password" },
        {
          k: "acoustid_user_key", label: "AcoustID user key (fingerprint submissions)", type: "password",
          help: "The USER key of your own acoustid.org account (AcoustID → your account → API keys), which is a different key from the application key above. It is needed ONLY to submit fingerprints — the wizard's Submit to AcoustID publishes the ACOUSTID_FINGERPRINT/ID pair a matched album carries to AcoustID's public database, and AcoustID refuses that submission in its own words while this is blank or wrong. Looking a release up never uses it: the application key alone can do that.",
        },
        { k: "acoustid_min_score", label: "Minimum AcoustID match score", type: "number", min: 0, max: 1, step: 0.05 },
        { k: "acoustid_fpcalc_path", label: "fpcalc path (blank = the bundled one)", type: "text", help: "Only needed when AcoustID should use a fingerprint tool outside the dependencies folder." },
      ],
    },
    {
      title: "Import & tag cleanup",
      blurb: "Genre importing from MusicBrainz and tag hygiene applied while optimizing. The genre inference below runs once per album during an import (with a model configured in the AI section), so its effort costs a slower import, never a slower app.",
      fields: [
        { k: "mb_genre_count", label: "Genres per track (import, trimming and grading)", type: "number", min: 1, max: 3, help: "One value, three consumers: an import writes up to this many genres onto a track (specific genres first, the derived FAMILY last), script 8 / the genre import / script 10 trim any excess off, and grading fails a track carrying more than this. Fewer is fine — the family is derived from the specific genre, so one specific genre is a complete answer and nothing is topped up with filler. A per-run import limit may only lower this. Default 2." },
        { k: "genre_sources", label: "Genre sources — priority order: asked top to bottom, stopped as soon as a track's list is full; unticked = never asked", type: "multi", options: [], optionsFrom: "genres", help: "A PRIORITY list, not a set: the sources are asked top to bottom and the chain stops as soon as a track's list is complete. The shipped default is every source the app knows (RateYourMusic first, then MusicBrainz — the order shown here, which the wizard's tray saves back in the same order), and an unticked source is NEVER asked. The genres the sources answer with are merged, deduped and capped at the count above, per track. MusicBrainz is the app's own identity anchor — it also supplies the family every list ends with — so leave it on in most setups." },
        { k: "ai_genre_inference", label: "Let a model rank the genres", type: "bool", help: "The model is given what the sources above already answered and picks which of them describe the track, most specific first. Needs a base URL and model in the AI section; with none configured the source list is used as it stands." },
        { k: "ai_genre_effort", label: "Reasoning effort for that ranking", type: "select", options: [["max","Max — the provider's highest thinking budget"],["high","High — reason, then research what the list misses (default)"],["medium","Medium"],["low","Low"],["minimal","Minimal — no thinking, fastest"]] },
        { k: "ai_genre_research", label: "Let the model go beyond the fetched genres", type: "bool", help: "On, the model may name a genre the sources did not answer with when it knows the artist better than they do — the name must still be a MusicBrainz genre to survive. Off, the answer is strictly a re-ranking of what was fetched." },
        { k: "strip_unknown_tags", label: "Remove non-canonical tags on optimize (script 10)", type: "bool" },
      ],
    },
    {
      title: "Beets tagging (script 14)",
      blurb: "Managed beets import with Picard-parity behaviors: MusicBrainz matching, locale alias translations, WORK/MOVEMENT from work relationships, and release-type capitalization (EP uppercased). Files are organized by your naming script via the mlo_dir path hook. Runs as part of Run All; skipped quietly when beets isn't installed.",
      fields: [
        { k: "locale", label: "Locale for names and aliases (e.g. en, ja, de)", type: "text",
          help: "The ONE locale the app writes and shows names in: the MusicBrainz"
            + " pages' aliases, the beets import's alias translations and the"
            + " Soulseek alias searches all read this value." },
        { k: "beets_translations", label: "Translate titles/names to preferred locale", type: "bool" },
        { k: "beets_work_movement", label: "Write WORK / MOVEMENT from work relationships", type: "bool" },
        { k: "beets_release_type_caps", label: "Capitalize release types (EP uppercased)", type: "bool" },
        { k: "beets_organize_after", label: "Re-run organize after each beets import", type: "bool" },
      ],
    },
    {
      title: "Dependencies",
      blurb: "External tools are pinned to reviewed releases; the table above shows what upstream has published as well. This is the only switch that belongs to the tool chain itself.",
      fields: [
        { k: "dependencies_auto_update", label: "Install missing tools and updates automatically", type: "bool", help: "A background pass every few hours installs every tool whose state is Missing or Update — into the dependencies folder, nothing system-wide. Off by default: the app downloads binaries on its own schedule only if you ask it to. The button above still works either way." },
      ],
    },
    {
      title: "Library folders & naming",
      blurb: "How the library itself is laid out: the naming script every organize run and the grader both follow, the folder-name shortening, and where lyrics are kept.",
      fields: [
        { k: "naming_script", label: "Naming script", type: "text", help: "The beets-style template the library is organized by — %albumartist% [%musicbrainz_albumartistid%]/... Leave it blank to keep the shipped one." },
        { k: "short_folder_names", label: "Shorten folder names where the full one is too long", type: "bool" },
        { k: "layout_apply", label: "Fix the library layout when it is scanned (script 20)", type: "bool", help: "On: a layout scan also fixes what it can prove — an artist, album or file name spelled in the wrong letter case is renamed to the naming script's spelling, audio sitting outside any album folder is moved into the album its own tags name, and an artist folder with no album under it goes to the Trash. Nothing is ever deleted, and a file whose album cannot be read from its tags is reported instead. Off: the scan only reports." },
        { k: "lyrics_format", label: "Where lyrics are stored", type: "select", options: [["EMBEDDED", "Embedded in the audio file"], ["LRC", "LRC sidecar"], ["BOTH", "Both"]] },
        { k: "worker_limit", label: "Worker threads (0 = count them from the CPU)", type: "number", min: 0, max: 64 },
      ],
    },
    {
      title: "Interface",
      blurb: "What this client shows, and in which language — the server's own language setting, shared by every client.",
      fields: [
        { k: "ui_locale", label: "Language", type: "select", options: [], optionsFrom: "locales" },
        { k: "show_sidecar_files", label: "Show sidecar files (cue/log/lrc/accurip) in the library", type: "bool" },
        { k: "auto_advance", label: "Auto-advance between Run All scripts", type: "bool" },
      ],
    },
    {
      title: "Individual grading checks",
      blurb: "Each check the grader runs, one by one. A check that is off is not evaluated at all — no pass, no fail, no issue code — so switch off what this library deliberately does without.",
      fields: [
        { k: "grade_check_tag_spaces", label: "Tag spaces", type: "bool" },
        { k: "grade_check_tag_case", label: "Tag value case", type: "bool" },
        { k: "grade_check_lyrics_spaces", label: "Lyrics spaces", type: "bool" },
        { k: "grade_check_cue_spaces", label: "CUE spaces", type: "bool" },
        { k: "grade_check_cover_crop", label: "Cover aspect ratio (squareness)", type: "bool" },
        { k: "grade_check_lyrics_zero", label: "Lyrics zero timestamp", type: "bool" },
        { k: "grade_check_tag_blank_lines", label: "Tag blank lines", type: "bool" },
        { k: "grade_check_lyrics_blank_lines", label: "Lyrics blank lines", type: "bool" },
        { k: "grade_check_cue_blank_lines", label: "CUE blank lines", type: "bool" },
        { k: "grade_check_unreadable", label: "Unreadable files", type: "bool" },
        { k: "grade_check_missing_tags", label: "Missing tags", type: "bool" },
        { k: "grade_check_encoder", label: "Encoder markers", type: "bool" },
        { k: "grade_check_audit", label: "AUDIT present", type: "bool" },
        { k: "grade_check_instrumental", label: "INSTRUMENTAL", type: "bool" },
        { k: "grade_check_lyrics", label: "Lyrics present", type: "bool" },
        { k: "grade_check_lyrics_format", label: "Lyrics format", type: "bool" },
        { k: "grade_check_sidecar_cover", label: "Sidecar cover", type: "bool" },
        { k: "grade_check_mb_links", label: "MusicBrainz links (album/artist/track)", type: "bool" },
        { k: "grade_check_rym_links", label: "RateYourMusic links (album/artist/track)", type: "bool" },
        { k: "grade_check_media", label: "MEDIA tag", type: "bool" },
        { k: "grade_check_source", label: "SOURCE tag", type: "bool" },
        { k: "grade_check_album_tags", label: "Album-level tags", type: "bool" },
        { k: "grade_check_cd_log", label: "CD log", type: "bool" },
        { k: "grade_check_cd_cue", label: "CD cue", type: "bool" },
        { k: "grade_check_disc_naming", label: "Disc naming", type: "bool" },
        { k: "grade_check_log_grade", label: "Log grade", type: "bool" },
        { k: "grade_check_crc", label: "CRC", type: "bool" },
        { k: "grade_check_cd_format", label: "CD format", type: "bool" },
        { k: "grade_check_cover", label: "Cover present", type: "bool" },
        { k: "grade_check_cue_format", label: "CUE format", type: "bool" },
        { k: "grade_check_cue_files", label: "Per-track CUE sheets (a CUE beside the tracks)", type: "bool" },
        { k: "grade_check_accurip_format", label: ".accurip format", type: "bool" },
        { k: "grade_check_expected_tracks", label: "Release tracklist manifest (albums carrying a MusicBrainz release id)", type: "bool" },
        { k: "grade_check_disallowed", label: "Disallowed files", type: "bool" },
        { k: "grade_check_extra_images", label: "Extra artwork (images not tied to a track)", type: "bool" },
        { k: "grade_check_empty_folders", label: "Empty folders (no files anywhere beneath them)", type: "bool" },
        { k: "grade_check_naming", label: "Naming script paths", type: "bool" },
        { k: "grade_check_filename_case", label: "Filename capitalization (exact case)", type: "bool" },
        { k: "grade_check_ext_case", label: "Lowercase file extensions", type: "bool" },
        { k: "grade_check_excess_tags", label: "Excess tags (non-canonical)", type: "bool" },
        { k: "grade_check_key_bpm", label: "Key & BPM tags", type: "bool" },
        { k: "grade_check_lyrics_lang_tags", label: "Transform tags carry language (TRANSLATION-EN)", type: "bool" },
        { k: "grade_check_mood", label: "Mood tag present", type: "bool" },
        { k: "grade_check_energy", label: "Energy tag present (0-100, with MOOD)", type: "bool" },
        { k: "grade_check_genre", label: "Genre tag present", type: "bool" },
        { k: "grade_check_genre_count", label: "Genre count per track (at most mb_genre_count)", type: "bool", help: "A track may hold at most the 'Genres per track' value — only an overflow fails (issue code GENRE_COUNT). There is no lower bound and no quota; keep the two in step." },
        { k: "grade_check_genre_order", label: "Genre order (the family, if present, comes first)", type: "bool", help: "The family must be the FIRST genre, e.g. Rock / Shoegaze (issue code GENRE_ORDER). A family in a later slot, or a genre repeated, fails. The names themselves are graded by the vocabulary check below." },
        { k: "grade_check_genre_vocab", label: "Genre vocabulary (MusicBrainz)", type: "bool", help: "Every GENRE name must be one MusicBrainz publishes (shoegaze, dream pop, …); an unknown name fails with issue code GENRE_VOCAB and is named in the report. Grading never rewrites the tag — run Auto tagging (8) or Format all (10) to canonicalize." },
        { k: "grade_check_album_description", label: "Album description stored", type: "bool" },
        { k: "grade_check_artist_image", label: "Artist image stored", type: "bool" },
        { k: "grade_check_artist_description", label: "Artist description stored", type: "bool" },
        { k: "grade_check_replaygain", label: "ReplayGain tags present (only when a file already carries one)", type: "bool" },
        { k: "grade_check_acoustid", label: "AcoustID tags present (only when a file already carries one)", type: "bool" },
        { k: "grade_check_xlit_transliteration", label: "TRANSLITERATION tag on non-Latin lyrics", type: "bool", help: "A track whose lyrics are already Latin script must NOT carry a TRANSLITERATION tag (or a .romaji.lrc sidecar); non-Latin lyrics must have one. Instrumental tracks are never graded on it." },
        { k: "grade_check_xlit_translation", label: "TRANSLATION tag matches the translation language", type: "bool", help: "Computed against the first of the translation languages above: a track already in that language must not carry a TRANSLATION tag, and one that is not must have the right one." },
      ],
    },
    {
      title: "Script chain (Run All)",
      blurb: "The order Run All and every import chain execute the scripts in. Everything switched off here stays out of the pipeline; the scripts themselves are configured on the steps before this one.",
      fields: [
        { k: "run_all_order", label: "Scripts, in execution order", type: "chain", help: "Ticked scripts run in the order shown; unticking one leaves it out of Run All entirely." },
      ],
    },
    {
      title: "Export defaults",
      blurb: "What an export (Trash → Export, or the Export page) writes by default: the destination, the structure, and how much of the library's own work is carried along.",
      fields: [
        { k: "export_dest", label: "Destination folder (blank = ask every time)", type: "text" },
        { k: "export_structure", label: "Folder structure", type: "select", options: [["artist_album", "Artist / Album"], ["album", "Album"], ["flat", "One folder"], ["artist_album_disc", "Artist / Album / Disc"]] },
        { k: "export_codec", label: "Audio", type: "select", options: [["copy", "Copy (no re-encode)"], ["flac", "FLAC"], ["mp3", "MP3"], ["opus", "Opus"]] },
        { k: "export_quality", label: "Lossy quality (blank = codec default)", type: "text" },
        { k: "export_subfolder", label: "Subfolder inside the destination", type: "text" },
        { k: "export_workers", label: "Parallel workers (0 = count them from the CPU)", type: "number", min: 0, max: 32 },
        { k: "export_verify", label: "Verify every written file", type: "bool" },
        { k: "export_prune", label: "Delete the destination leftovers first", type: "bool" },
        { k: "export_playlists", label: "Export playlists", type: "bool" },
        { k: "export_sidecars", label: "Export sidecar files (cue/log/lrc/accurip)", type: "bool" },
        { k: "export_clean_tags", label: "Write only canonical tags", type: "bool" },
        { k: "export_id3v2", label: "ID3v2 version for MP3", type: "select", options: [["2.3", "2.3"], ["2.4", "2.4"]] },
        { k: "export_id3v1", label: "Also write ID3v1 (MP3)", type: "bool" },
        { k: "export_replaygain_mode", label: "ReplayGain in an export", type: "select", options: [["off", "Off"], ["tags", "Write ReplayGain tags (the player applies them)"], ["apply", "Bake the gain into the audio"]] },
        { k: "export_eq_profile", label: "Equalizer profile (preset or imported profile id, blank = none)", type: "text" },
        { k: "export_target", label: "Destination", type: "select", options: [["server", "A folder this machine can see"], ["zip", "A zip the client downloads"]] },
        { k: "export_manifest", label: "Write checksums.sha256 beside the export", type: "bool" },
        { k: "export_embed_covers", label: "Embed the cover into every exported file", type: "bool" },
        { k: "export_embed_cover_jpeg_quality", label: "Embedded cover quality (JPEG)", type: "number", min: 60, max: 100 },
        { k: "export_embed_cover_resolution", label: "Embedded cover max size (px)", type: "number", min: 0, max: 4000 },
      ],
    },
    {
      title: "Server & remote access",
      blurb: "Where this server listens and the address clients should dial. A change to the port or host is picked up at the next start; the address is what a phone or desktop client is told to use.",
      fields: [
        { k: "download_concurrency", label: "Files read at once for one download (1–8)", type: "number", min: 1, max: 8, help: "The reader pool behind a queue or offline-cache download: higher fills a socket faster, at the cost of staging more file data in memory." },
        { k: "server_host", label: "Listen address (0.0.0.0 = every interface)", type: "text", help: "Anything but 127.0.0.1 means other machines can reach this server — the login gate turns itself on there (see the next group)." },
        { k: "server_port", label: "Port", type: "number", min: 1, max: 65535 },
        { k: "server_public_url", label: "Public address clients should use (blank = this machine)", type: "text" },
      ],
    },
    {
      title: "Login gate",
      blurb: "Whether clients must sign in before they can read the library. Off is only safe for a server that listens on this machine alone; the password itself is set below.",
      fields: [
        { k: "auth_mode", label: "Ask clients to sign in", type: "select", options: [["auto", "Automatically — on for any non-local client"], ["required", "Always"], ["off", "Never"]] },
        { k: "auth_username", label: "Name of the first user (blank = this machine)", type: "text" },
        { k: "auth_session_days", label: "Days before a session expires", type: "number", min: 1, max: 365 },
      ],
    },
    {
      title: "Notifications",
      blurb: "The events this server pushes to every client that has notifications enabled — outcomes (a wish found, a download finished, an album ready to import) and the two Soulseek starts (a download whose first bytes moved, a peer taking files from you). Each client still asks for its own permission.",
      fields: [
        { k: "notify_wish_found", label: "Wish found on Soulseek", type: "bool" },
        { k: "notify_download_done", label: "Download finished", type: "bool" },
        { k: "notify_import_ready", label: "Album ready to import", type: "bool" },
        { k: "notify_soulseek_download_start", label: "Soulseek download started", type: "bool" },
        { k: "notify_soulseek_upload_start", label: "A peer started downloading from you", type: "bool" },
      ],
    },
    {
      title: "Artist watch",
      blurb:
        "Follow an artist instead of re-checking them by hand: the worker asks MusicBrainz for releases after the watch was created and queues what matches. Nothing is queued twice, an undated release group is never 'new', and the per-cycle cap is what keeps a first check from dumping a back catalogue into the queue.",
      fields: [
        { k: "artist_watch_enabled", label: "Watch artists for new releases", type: "bool" },
        { k: "artist_watch_interval_hours", label: "Check interval (hours)", type: "number", min: 1, max: 720, help: "How long between two checks of the SAME artist; the worker itself ticks far more often." },
        { k: "artist_watch_max_per_cycle", label: "Releases queued per artist per check", type: "number", min: 1, max: 50, help: "The hard anti-dump cap. One is the shipped default: a watch that queued a hundred at once is the discography dump this feature exists to avoid." },
        { k: "artist_watch_types", label: "Release types a watch may queue", type: "multi", options: [["album","Album"],["ep","EP"],["single","Single"],["broadcast","Broadcast"],["other","Other"],["compilation","Compilation"],["soundtrack","Soundtrack"],["spokenword","Spoken word"],["interview","Interview"],["audiobook","Audiobook"],["live","Live"],["remix","Remix"],["dj-mix","DJ mix"],["mixtape/street","Mixtape / street"],["demo","Demo"],["audio drama","Audio drama"],["field recording","Field recording"]], help: "MusicBrainz's own type names. A release group matches when its primary type is ticked or ANY secondary type is (a live album is Album + Live, so ticking Live finds it). A watch can narrow this per artist." },
        { k: "artist_watch_auto_add", label: "Queue a matched release into the library automatically", type: "bool", help: "Off, a watch only reports what it found (the notification is the whole output) — which is what you want if you pick the edition by hand." },
      ],
    },
];

/** The groups a first run actually asks about. Every other group is one click
 *  away inside the step that owns it (the wizard folds those): the factory
 *  defaults ARE the answer for a first run, and a wall of 50 checkboxes is how
 *  a first run gets abandoned. */
export const OPEN_GROUPS: Record<string, true> = {
  "AI — lyric transforms & genre ranking": true,
  Discovery: true,
  "Import pipeline": true,
  "Soulseek (managed slskd)": true,
  "Library folders & naming": true,
  Interface: true,
  "Script chain (Run All)": true,
  "Server & remote access": true,
  "Login gate": true,
  Notifications: true,
};

/** The wizard, in order: the things a first run must decide come first, and
 *  every step may be skipped. Between them the steps reference EVERY group
 *  above — the coverage test fails on a group no step renders. */
export const SETUP_STEPS: SetupStep[] = [
  {
    label: "Account",
    title: "Your account",
    blurb:
      "This server's own login, asked FIRST because everything after it can be read by anyone who can reach the address. The password is required — the wizard will not move on without one. The name is optional: leave it as it is (admin) or blank it, and the install keeps an unnamed owner; either way it is editable later in Settings → Sign-in & security.",
    panel: "password",
    groups: ["Login gate"],
  },
  {
    label: "Folder",
    title: "Your music library",
    blurb:
      "Everything the app grades, tags and optimizes lives under one folder (your artist/album tree). The app's own state (data, downloads, trash) lives inside it too, so it moves with the library.",
    panel: "folder",
  },
  {
    label: "Tools",
    title: "External tools",
    blurb:
      "The scripts need these programs. Missing ones are downloaded into the app's dependencies folder; the ones this device cannot run are listed with the reason instead.",
    panel: "dependencies",
    groups: ["Dependencies"],
  },
  {
    label: "Keys",
    title: "Sources & API keys",
    blurb:
      "What the app asks for lyrics, genres, ratings and artwork. All of them are free, and the keyed ones work without keys too — they are simply skipped.",
    panel: "sources",
    groups: ["Discovery", "Import pipeline"],
  },
  {
    label: "AI",
    title: "AI model",
    blurb:
      "Optional, and the only model in the app: it romanizes and translates lyrics, and ranks genres during an import. With the URL or model empty both are skipped.",
    groups: ["AI — lyric transforms & genre ranking"],
  },
  {
    label: "Soulseek",
    title: "Soulseek sharing & auto-import",
    blurb:
      "Sharing your library, and letting the app fill the wishes you save by downloading from the network. Both need a free Soulseek account. The listen port is opened on your router automatically when slskd starts (UPnP first, then NAT-PMP) so peers can reach this client; turn that off below if you forward the port yourself or your router supports neither.",
    panel: "slskd",
    groups: ["Soulseek (managed slskd)", "Auto-import (MusicBrainz → Soulseek)", "Wishes (auto-fill)"],
  },
  {
    label: "Library",
    title: "Library & interface",
    blurb: "How the library is laid out on disk, and what this client shows you.",
    groups: ["Library folders & naming", "Interface", "Home"],
  },
  {
    label: "Imports",
    title: "Imports & tagging",
    blurb:
      "What an album goes through after it lands: the script chain's own options, genre and mood tagging, and the metadata fetched from online providers — plus the artist watch that queues new releases on its own.",
    groups: [
      "Import & tag cleanup",
      "AutoTag (script 8)",
      "Beets tagging (script 14)",
      "Release tracklist (script 15)",
      "Artist images & descriptions",
      "Artist watch",
    ],
  },
  {
    label: "Scripts",
    title: "Optimization scripts",
    blurb:
      "The defaults every run uses for the library's files: lossless conversion, images and covers, lyrics and CUEs, CD rip sheets, videos, and which tag families may be written at all.",
    groups: [
      "Library codec (script 3)",
      "Embedded covers",
      "Images (script 5)",
      "Lyrics & CUEs (scripts 1, 2 & 18)",
      "CD Rips (scripts 2/6/9/10)",
      "Videos (script 11)",
      "Tag writes (global switches)",
    ],
  },
  {
    label: "Quality",
    title: "Audit, loudness & grading",
    blurb:
      "What the app measures and what it accepts: the audio audit, loudness and ReplayGain, AccurateRip, key and BPM detection, and every rule the grader applies.",
    groups: [
      "Audit (script 6)",
      "DR / ReplayGain (script 7)",
      "AccurateRip (script 9)",
      "Key & BPM (script 12)",
      "Grading (script 4)",
      "Individual grading checks",
    ],
  },
  {
    label: "Chain",
    title: "Script chain & export",
    blurb: "Which scripts Run All executes, in what order, and what an export writes by default.",
    groups: ["Script chain (Run All)", "Export defaults"],
  },
  {
    label: "Access",
    title: "Access & notifications",
    blurb:
      "Where this server listens, and which events it pushes to your clients. The login itself was the first step; this one is the address and the alerts.",
    groups: ["Server & remote access", "Notifications"],
  },
  {
    label: "Done",
    title: "You're all set",
    blurb: "Everything you skipped keeps its shipped default, and every one of these settings stays editable in Settings.",
    panel: "done",
  },
];

/** Keys the wizard writes itself rather than through a control: the folder its
 *  picker saves, and the flag its own buttons set when the run finishes. */
export const WIZARD_MANAGED_KEYS: string[] = ["music_folder", "first_run_done"];

/** Config keys the wizard deliberately does not offer. Everything else the
 *  app can configure is reachable from one of the steps above. */
export const HIDDEN_KEYS: string[] = [
  // The password hash is refused by POST /api/config (it must be: a session
  // could otherwise replace the credential with a hash of its choosing) and is
  // set through /api/auth/setup, which is where the wizard's own password
  // fields send it. Reading it back would publish the credential too.
  "auth_password_hash",
  // One-shot re-run switches. They say "process this track even though it
  // already carries a result", which cannot be answered before the library has
  // been through the pipeline once; each one lives on the Settings tab of the
  // script that reads it, one click from where it matters, and a first run
  // that shipped them ticked would re-encode everything it touched.
  "force_accurip",
  "force_audiometa",
  "force_audit",
  "force_auto_tag",
  "force_cue",
  "force_dr_replaygain",
  "force_lyrics",
  "force_mood",
  "force_publish",
  "force_reencode_flac",
  "force_reencode_images",
  "force_tracklist",
  "force_xlit",
];

/** The controls' starting values: the saved config, so a field the user never
 *  touches saves nothing and the value in force stands. */
export function cfgDraft(config: Record<string, unknown>, fields: CfgField[]): Record<string, unknown> {
  const draft: Record<string, unknown> = {};
  for (const field of fields) {
    const saved = config[field.k];
    switch (field.type) {
      case "bool":
        draft[field.k] = !!saved;
        break;
      case "number":
        draft[field.k] = saved === undefined || saved === null ? "" : String(saved);
        break;
      case "matrix": {
        const rows = (saved ?? {}) as Record<string, Record<string, boolean>>;
        draft[field.k] = Object.fromEntries(Object.entries(rows).map(([type, cells]) => [type, { ...cells }]));
        break;
      }
      case "list":
      case "multi":
      case "chain":
        draft[field.k] = Array.isArray(saved) ? [...saved] : [];
        break;
      case "csv":
        draft[field.k] = Array.isArray(saved)
          ? saved.map(String)
          : String(saved ?? "")
              .split(",")
              .map((part) => part.trim())
              .filter(Boolean);
        break;
      default:
        // A list-valued text field (the "; "-separated share dirs and templates)
        // is edited as the string the server parses it back from.
        draft[field.k] = Array.isArray(saved) ? saved.join("; ") : String(saved ?? "");
    }
  }
  return draft;
}

/** The value a control holds right now, as the config stores it — or
 *  `undefined` for "keep the saved value", which is what a blank number
 *  means. */
export function cfgDraftValue(field: CfgField, raw: unknown): unknown {
  if (field.type === "number") {
    const text = String(raw ?? "").trim();
    return text === "" ? undefined : Number(text);
  }
  if (field.type === "matrix") {
    const rows = (raw ?? {}) as Record<string, Record<string, boolean>>;
    return Object.fromEntries(Object.entries(rows).map(([type, cells]) => [type, { ...cells }]));
  }
  return raw;
}

/** What a save sends: the fields whose control now differs from the config.
 *  A partial save is the documented contract of POST /api/config — every key
 *  left out keeps its stored value — so a step only ever writes what the user
 *  actually answered. */
export function cfgChanges(
  draft: Record<string, unknown>,
  config: Record<string, unknown>,
  fields: CfgField[],
): Record<string, unknown> {
  const changed: Record<string, unknown> = {};
  for (const field of fields) {
    const next = cfgDraftValue(field, draft[field.k]);
    if (next === undefined) continue;
    if (JSON.stringify(next) === JSON.stringify(config[field.k])) continue;
    changed[field.k] = next;
  }
  return changed;
}

/** What is wrong with a control's value, or null when it can be saved.
 *
 *  Only the rules the server does NOT enforce: it clamps a number into range
 *  and drops what it cannot read rather than refusing the save, so a value it
 *  would silently replace has to be caught here, where the field that produced
 *  it can be named. */
export function cfgError(field: CfgField, raw: unknown): string | null {
  const text = String(raw ?? "").trim();
  if (field.type === "number") {
    if (text === "") return null; // blank keeps the saved value
    const n = Number(text);
    if (!Number.isFinite(n)) return "must be a number";
    if (field.min !== undefined && n < field.min) return `must be at least ${field.min}`;
    if (field.max !== undefined && n > field.max) return `must be at most ${field.max}`;
    return null;
  }
  if (field.type === "text" && field.pattern && text && !new RegExp(field.pattern).test(text)) {
    return field.patternHelp ?? "that value is not in the right form";
  }
  return null;
}
