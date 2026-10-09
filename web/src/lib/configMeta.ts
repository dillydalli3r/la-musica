/** Every config setting the app has, what it is called, how it is edited, and —
 *  for the few a first run has to ask — which setup step owns it: one module,
 *  so the wizard and the Settings page never keep two hand-maintained copies of
 *  the same list.
 *
 *  The groups below are the settings tabs by name and field for field; the keys
 *  no tab owns (the per-filetype matrices, the export defaults, the server's
 *  own address, the login gate) are appended to the group that explains them,
 *  plus a few groups of their own. `tools/test_setup_coverage.py` holds the two
 *  sides together: every key of `mlo/config.py`'s DEFAULT_CONFIG must be covered
 *  here or named in `HIDDEN_KEYS`, every key and group SettingsPage renders must
 *  exist here too, and every group no step renders has to be rendered by some
 *  other screen — so a setting added on one side fails the others until it is
 *  accounted for. */

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
  | { k: string; label: string; type: "list"; catalog: "lyrics" | "covers"; help?: string }
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

/** The fields a step `ask`s, in the order it names them. A name that is not a
 *  field of one of the step's groups throws, exactly like `cfgGroup`: an ask
 *  list that quietly rendered nothing would drop an account or a login from
 *  the only screen that asks for it. */
export function stepAskedFields(step: SetupStep): CfgField[] {
  const fields = stepFields(step);
  return (step.ask ?? []).map((k) => {
    const field = fields.find((f) => f.k === k);
    if (!field) throw new Error(`setup step ${step.label} asks for ${k}, which none of its groups has`);
    return field;
  });
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
  panel?: "folder" | "dependencies" | "sources" | "password" | "done";
  /** CONFIG_GROUPS titles this step renders, in order. */
  groups?: string[];
  /** Keys of THIS step's groups that are asked on the step itself, above the
   *  group's fold, because nothing has an answer for them yet (an account, a
   *  login). The group's other fields stay folded at their shipped default —
   *  and all of them stay on the Settings tab. A key named here is rendered
   *  once: the step draws the group's own field, and the group below it skips
   *  exactly that key. */
  ask?: string[];
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
        { k: "lyrics_allow_plain", label: "Accept plain (unsynced) lyrics", type: "bool", help: "Off by default: the chain prefers synced lyrics and writes an untimed one only when no source states timestamps, and this install then fails that track's lyrics check (“Plain” on the track's own surfaces). Turn this on to accept untimed text — LRCLIB's plain records — as a good answer here." },
        { k: "lyrics_search_aliases", label: "Search lyrics under alias names", type: "bool", help: "When the first search for a track's lyrics finds nothing, search again under the artist/album/title's other names from MusicBrainz (aliases) — e.g. Hikaru Utada for 宇多田ヒカル. On by default." },
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
        { k: "advisory_auto_fetch", label: "Fetch the advisory rating automatically (import + advisory fetch)", type: "bool" },
        { k: "advisory_ai_classify", label: "Judge the lyrics with the AI provider", type: "bool", help: "The configured AI model reads the track's lyrics (embedded, else the .lrc sidecar) and rates the SONG — excessive profanity, a slur or a very strong word, or graphic sex/violence/drug use is 1; a mild word in passing is 0; 3 = it cannot tell, which falls through. It is asked ONLY when no source stated anything at all — a value a source already stated is never second-guessed. Needs an AI base URL and model in the AI section; with no AI configured `advisory_fallback` answers instead." },
        { k: "advisory_fallback", label: "When nothing states an advisory", type: "select", options: [["0","Store 0 — not explicit"],["2","Store 2 — clean edition"],["none","Store nothing — leave it unrated"]], help: "The last resort for a track no source stated anything about and the AI could not rate: 0 (not explicit), 2 (clean edition) or nothing at all." },
        { k: "mood_enabled", label: "Write mood tags", type: "bool" },
        { k: "genre_autofill", label: "Trim genres to the configured count", type: "bool", help: "Genres are never imported by a script: the import (MusicBrainz/RateYourMusic per track, then the configured sources) and manual edits are the only writers. Script 8 only brings a list longer than the genre count back down to it." },
        { k: "mood_source", label: "Mood source", type: "select", options: [["audio","Audio analysis"],["provider","Provider metadata"],["hybrid","Hybrid — audio, trusting the genre when the audio is ambiguous"]] },
        { k: "auto_instrumental", label: "Set INSTRUMENTAL automatically", type: "bool" },
        { k: "auto_zero_advisory_for_instrumental", label: "Zero advisory on instrumentals (no words, no explicit content)", type: "bool" },
        { k: "fix_instrumental_from_lyrics", label: "Fix INSTRUMENTAL from lyrics", type: "bool" },
        { k: "instrumental_auto_fetch", label: "Look the INSTRUMENTAL verdict up when nothing states one", type: "bool" },
        { k: "instrumental_ai_classify", label: "Ask the AI provider about lyric-less tracks", type: "bool", help: "When a track has no lyrics at all (no LYRICS tag, no .lrc sidecar) and no source states an INSTRUMENTAL verdict, the configured AI model is asked once and must answer a single digit — 1 instrumental, 0 not. Costs one model call per such track; a reply that is not a lone 0 or 1 writes nothing. Needs an AI base URL and model in the AI section." },
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
      title: "Storage & cleanup",
      blurb:
        "What the app may keep on disk in the folders it fills by itself. The trash holds what was removed from the library — scratch space that may not grow without end: over its cap it is emptied OLDEST FIRST until it fits, and nothing in use is ever touched (a folder an import or a script run is working on).",
      fields: [
        {
          k: "trash_cap_gb", label: "Trash cap (GB, 0 = no cap)", type: "number", min: 0, max: 1000, step: 0.1,
          help: "The remove-from-library bin (<music folder>/.mlo/trash). Over this size the app deletes the OLDEST trashed entries for good (exactly like Delete on the Trash page; what is left stays restorable) until the bin is under again, and an entry being restored is skipped. 0 = the bin keeps everything.",
        },
      ],
    },
    {
      title: "Release choice",
      blurb: "Which edition of a release group an \"Add to library\" picks, and why — releases are ranked by these rules before one is added.",
      fields: [
        { k: "auto_import_avoid_promo", label: "Never pick promotional / bootleg editions", type: "bool" },
        { k: "auto_import_require_country", label: "Only auto-import editions with a release country", type: "bool", help: "A MusicBrainz release without RELEASECOUNTRY is usually an unsorted import, and the CD query templates are built from that field — such editions are skipped, and a group whose only editions lack one is reported as ineligible instead." },
        { k: "auto_import_medium_order", label: "Medium preference (comma-separated, best first)", type: "csv", help: "Editions are ranked by this media order first, then by how close the edition is to the release group's original date; a format not named here ranks after every configured one. Blank = the built-in order (CD, Vinyl, Cassette, Other, DVD, Blu-ray, VHS, Video CD, LaserDisc, Digital Media) — CD first, the other physical media next (the video carriers included, so a music video on a disc beats the same video published as a download), digital last." },
        { k: "prefer_release_country", label: "Preferred release country (ISO code, blank = none)", type: "text", help: "The spelling MusicBrainz publishes on the release, e.g. US or GB. A tie-breaker only: it never outranks status, medium, track count or the original-edition rule." },
        { k: "prefer_original_edition", label: "Prefer the original (explicit) edition over a clean or edited one", type: "bool", help: "MusicBrainz states this in the release title or its disambiguation comment. Off, a clean edition is ranked on the other rules like any other — a clean edition may carry altered audio." },
        { k: "prefer_disc_streams", label: "Prefer a disc's own streams over a compressed re-encode", type: "bool",
          help: "A BDRip/DVDRip/x264 release is a lossy derivative of the disc, and so is the 700 MB re-encode a rip sometimes ships beside its VIDEO_TS or BDMV folder. On (the default), an edition that names itself one ranks below the disc's own streams — a remux, a full disc — and a folder holding a disc structure beside a re-encode is remuxed as the disc's own single title, with the derivative left where it is. Off, the disc handling goes with it: those files take the ordinary per-file path and nothing prefers the disc's streams — this one switch covers both the download and the file pipeline." }
      ],
    },
    {
      title: "Discovery",
      blurb: "The credentials the link and advisory sources ask for: RateYourMusic's cookie and User-Agent, Spotify's ISRC advisory lookup, and the Discogs/Last.fm keys the genre chain reads. Each is optional — a source without its key is simply skipped.",
      fields: [
        { k: "rym_cookie", label: "RateYourMusic cookie", type: "password", help: "Only needed when RYM answers with a challenge. Two ways in: a cookies.txt in Netscape format — what a browser-extension exporter like \"Get cookies.txt\" writes — is imported by the cookie panel below (the same panel is on Settings' Sources and in this wizard) (paste it or drop the file on the box; only its rateyourmusic.com cookies are kept), or open the devtools route — sign in to rateyourmusic.com, press F12 → Network → reload → click any request to rateyourmusic.com → Headers → Request Headers → copy everything after \"Cookie:\" and paste it here (newlines and the \"Cookie:\" label are handled for you). The paste must include Cloudflare's `cf_clearance` — the pair its challenge hands the browser that solved it — and RYM honours it only alongside the matching `rym_user_agent` and network, so export from one signed-in tab and set that browser's User-Agent below if it is not the built-in Chrome one. RYM's `session` cookie is HttpOnly, so a browser extension's export is the only way to get it out of a browser at all. It is a session credential — do not share it, and paste a fresh one when RYM starts refusing, since signing out or clearing cookies invalidates it. Blank = RYM is skipped like any other unavailable source; MusicBrainz still resolves RYM links for well-known releases. Test it with the Sources panel's Test button." },
        { k: "rym_user_agent", label: "RateYourMusic User-Agent", type: "text", help: "The User-Agent RYM requests are sent with. Cloudflare binds its `cf_clearance` cookie to the exact User-Agent (and network) that passed its challenge, so the stored cookie only counts when this matches the browser it came from — copy that browser's User-Agent (devtools → Network → any request → Request Headers → User-Agent) and paste it here. Blank = the built-in Chrome User-Agent the app already sends." },
        { k: "rym_links_auto", label: "Auto-find RateYourMusic links", type: "bool", help: "Whether the app may ask rateyourmusic.com to resolve an album's or artist's RYM page (the RateYourMusic source probe, Settings → Sources → Test). A request the site refuses never fails anything — the caller carries on, and nothing is ever written into a tag." },
        { k: "rym_archive_fallback", label: "Read archived RateYourMusic pages when the live site refuses", type: "bool", help: "With no cookie — or one RYM no longer accepts — the Wayback Machine is asked for the release page instead. An archived page can predate the release, so its genre list may be short; the live site is always tried first." },
        { k: "spotify_client_id", label: "Spotify client ID (optional)", type: "text", help: "Optional second advisory source (Spotify's ISRC lookup) behind Deezer and ahead of Apple. Empty = Spotify is skipped; an import never fails without it." },
        { k: "spotify_client_secret", label: "Spotify client secret (optional)", type: "password", help: "Pairs with the client ID above — both are needed before the Spotify lookup runs." },
        { k: "discogs_token", label: "Discogs token (optional)", type: "password", help: "A personal access token from discogs.com/settings/developers. Used to rate a release while a genre import runs; without it Discogs is skipped." },
        { k: "lastfm_api_key", label: "Last.fm API key (optional)", type: "password", help: "A free API key from last.fm/api. Only used by the discovery providers; without it Last.fm is skipped." },
      ],
    },
    {
      title: "Import pipeline",
      blurb: "What happens after an album lands in the library (Drag & drop, Finish import). The script chain below runs in order; leaving it blank runs the built-in chain: dedupe → sort → tag → covers → lyrics → audit → ReplayGain → AccurateRip. A fingerprint never picks a release by itself: AcoustID is consulted only when you press Match from fingerprint (wizard) or Detect (import dialog), and it needs a free application key from acoustid.org.",
      fields: [
        { k: "auto_acquisition_enabled", label: "Automatic acquisition (searching and downloading on their own)", type: "bool", help: "Off, nothing the app starts by itself searches or downloads. What you asked for is still recorded, and the wizard and every import path still work, because those are you acting, not the app." },
        { k: "manual_import_enabled", label: "Manual importing (the wizard and POST /api/import/*)", type: "bool", help: "Off, the import wizard and every importing /api/import/* route refuse with a sentence naming this setting instead of importing — the wizard shows that sentence where its steps would be. The automatic pipeline still imports what it downloads; only the paths you drive by hand are turned off." },
        { k: "import_autonomy", label: "Import autonomy", type: "select", options: [["automatic", "Automatic — decide everything the sources can answer (default)"], ["review", "Review — stop at each step that needs a decision"]], help: "Automatic runs the whole chain and only comes back to you for what nothing could supply: the album is imported either way and whatever it still lacks is reported as ONE prompt — a notification plus an entry the Import page lists — naming the families and linking to the album at the step where each decision is made. Review is the wizard's own behaviour applied to an import: it stops before the first step that needs a decision (a family the album is still missing) and hands the album over instead of deciding past it." },
        { k: "import_review_families", label: "Decide by hand, even when automatic", type: "multi", options: [["links", "Links"], ["cover", "Cover art"], ["genres", "Genres"], ["lyrics", "Lyrics"], ["advisory", "Advisory"]], help: "Families an import must never decide for you, whatever the mode above. A cover kept here has its candidates staged instead of writing the first hit; the links and advisory fetches are skipped; lyrics drop out of the chain. The rest of the import stays automatic, and the album's prompt names that family as waiting for you rather than as unsourced." },
        { k: "import_keep_synced_lyrics", label: "Keep a synced lyric an import arrives with", type: "bool", help: "An import replaces the families it decides for itself with what it found — the album's lyrics, genres, advisories and cover art win over whatever the download came with (a family you kept above, or one whose own switch is off, is never touched). This is the lyric family's one exception: ON, a track whose lyric already carries timestamps (a synced one) keeps it and the fetch skips that track; OFF, the shipped default, the peer's lyric is replaced like everything else. A PLAIN (untimed) lyric is always replaced — that is the form the providers answer with." },
        { k: "import_auto_scripts", label: "Run the script chain after import", type: "bool" },
        { k: "import_scripts", label: "Import script ids (e.g. 1, 3, 5, 7 — blank = built-in chain)", type: "text", pattern: "^(\\s*\\d+\\s*[,;]?)*$", patternHelp: "comma-separated script ids, e.g. 1, 3, 5, 7" },
        { k: "import_bulk_concurrency", label: "Bulk import concurrency (albums imported at once; the rest wait in the queue)", type: "number", min: 1, max: 16 },
        { k: "acoustid_enabled", label: "AcoustID enabled", type: "bool" },
        {
          k: "acoustid_api_key", label: "AcoustID application key (free at acoustid.org; every lookup sends it)", type: "password",
          help: "AcoustID's API takes TWO keys, and they are not two spellings of one — a single field could not work, which is why this one and the user key below are asked separately. The APPLICATION key (this one) identifies the app: every fingerprint LOOKUP sends it alone. The USER key identifies your account: a fingerprint SUBMISSION sends BOTH (POST /v2/submit carries \"client\" and \"user\"; the API has no single-key form). Register an application at acoustid.org for it (free). Blank, the lookups cannot run at all and answer \"no application key\", which the user key cannot stand in for.",
        },
        {
          k: "acoustid_user_key", label: "AcoustID user key (submissions only; your acoustid.org account)", type: "password",
          help: "The USER key of your own acoustid.org account (AcoustID → your account → API keys) — the other half of AcoustID's pair, and a different key from the application one. It is needed ONLY to submit fingerprints — a submission gives AcoustID one fingerprint TOGETHER WITH the MusicBrainz recording id it is — MusicBrainz itself never receives a fingerprint. The wizard's AcoustID step, the details menus' 'Submit fingerprints (AcoustID)' entry and Run All (once you tick it) all submit with it, and a pair AcoustID already links — or one this app already sent — is never re-sent. Looking a release up never uses this key: the application key alone can do that. Test it in Settings → Sources.",
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
            + " pages' aliases and the beets import's alias translations both"
            + " read this value." },
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
        { k: "dependencies_auto_update", label: "Install missing tools and updates automatically", type: "bool", help: "A background pass every few hours installs every tool whose state is Missing or Update — into the dependencies folder, nothing system-wide. On by default: the tool chain is what the app's grading, log scoring and AccurateRip evidence are worth, so nothing is left silently unusable. Untick it to install nothing without asking; the button above still works either way." },
      ],
    },
    {
      title: "Library folders & naming",
      blurb: "How the library itself is laid out: the naming script every organize run and the grader both follow, the folder-name shortening, and where lyrics are kept.",
      fields: [
        { k: "naming_script", label: "Naming script", type: "text", help: "The beets-style template the library is organized by — %albumartist% [%musicbrainz_albumartistid%]/... Leave it blank to keep the shipped one." },
        { k: "short_folder_names", label: "Shorten folder names where the full one is too long", type: "bool" },
        { k: "layout_apply", label: "Optimize the library layout when it is scanned (script 20)", type: "bool", help: "On: a layout scan also settles what it can prove — an artist, album or file name spelled in the wrong letter case is renamed to the naming script's spelling, audio sitting outside any album folder is moved into the album its own tags name, and what is excess goes to the Trash: a stray file (an nfo, a db, a stray text file), a foreign folder holding no audio, an album folder with no audio in it, an artist folder with no album under it. Nothing is ever deleted — every removal sits in the Trash until you empty it, and the Trash page can put it back — and a file whose album cannot be read from its tags is reported instead. Off: the scan only reports." },
        { k: "lyrics_format", label: "Where lyrics are stored", type: "select", options: [["EMBEDDED", "Embedded in the audio file"], ["LRC", "LRC sidecar"], ["BOTH", "Both"]] },
        { k: "worker_limit", label: "Worker threads (0 = every core)", type: "number", min: 0, max: 64,
          help: "How many files a script works on at once, and how many CPU threads each of those gets. 0 uses every core this machine has (a script may keep a lower safe ceiling of its own, e.g. its native tool already saturates the disk); 1 makes every script work on one file at a time." },
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
        { k: "grade_check_expected_tracks", label: "Whole release present (the tracklist manifest, and every track it names)", type: "bool" },
        { k: "grade_check_disallowed", label: "Disallowed files", type: "bool" },
        { k: "grade_check_extra_images", label: "Extra artwork (images not tied to a track)", type: "bool" },
        { k: "grade_check_empty_folders", label: "Empty folders (no files anywhere beneath them)", type: "bool" },
        { k: "grade_check_naming", label: "Naming script paths", type: "bool" },
        { k: "grade_check_filename_case", label: "Filename capitalization (exact case)", type: "bool" },
        { k: "grade_check_ext_case", label: "Lowercase file extensions", type: "bool" },
        { k: "grade_check_excess_tags", label: "Excess tags (non-canonical)", type: "bool" },
        { k: "grade_check_flac_md5", label: "FLAC stream MD5 (STREAMINFO)", type: "bool" },
        { k: "grade_check_key_bpm", label: "Key & BPM tags", type: "bool" },
        { k: "grade_check_lyrics_lang_tags", label: "Transform tags carry language (TRANSLATION-EN)", type: "bool" },
        { k: "grade_check_mood", label: "Mood tag present", type: "bool" },
        { k: "grade_check_energy", label: "Energy tag present (0-100, with MOOD)", type: "bool" },
        { k: "grade_check_genre", label: "Genre tag present", type: "bool" },
        { k: "grade_check_genre_count", label: "Genre count per track (at most mb_genre_count)", type: "bool", help: "A track may hold at most the 'Genres per track' value — only an overflow fails (issue code GENRE_COUNT). There is no lower bound and no quota; keep the two in step." },
        { k: "grade_check_genre_order", label: "Genre order (the family, if present, comes first)", type: "bool", help: "The family must be the FIRST genre, e.g. Rock / Shoegaze (issue code GENRE_ORDER). A family in a later slot, or a genre repeated, fails. The names themselves are graded by the vocabulary check below." },
        { k: "grade_check_genre_vocab", label: "Genre vocabulary (MusicBrainz)", type: "bool", help: "Every GENRE name must be one MusicBrainz publishes (shoegaze, dream pop, …); an unknown name fails with issue code GENRE_VOCAB and is named in the report. Grading never rewrites the tag — run Auto tagging (8) or Format all (10) to canonicalize." },
        { k: "grade_check_replaygain", label: "ReplayGain tags present (only when a file already carries one)", type: "bool" },
        { k: "grade_check_acoustid", label: "AcoustID tags required (ACOUSTID_ID + ACOUSTID_FINGERPRINT)", type: "bool", help: "Every audio track must carry both halves; a track with neither fails naming both, a half pair naming the missing one. No API key is needed — script 21 Fix AcoustID pairs takes the fingerprint locally and reads the recording id off the file." },
        { k: "grade_check_alias_needed", label: "Locale alias for names the locale cannot read (TITLEALIAS / ARTISTALIAS / ALBUMALIAS)", type: "bool", help: "A TITLE, ARTIST or ALBUM written in a script the configured locale (Import & tags) cannot read must carry its locale alias tag, optionally locale-suffixed (TITLEALIAS-JA). A Latin name never needs one, and neither does a name in the locale's own script — a Latin library is never charged for it. Script 14 / the import write them from MusicBrainz's own aliases." },
        { k: "grade_check_alias_excess", label: "No locale alias where none is needed (TITLEALIAS / ARTISTALIAS / ALBUMALIAS)", type: "bool", help: "An alias tag the configured locale does not need fails: a name that locale already reads carrying one (\"Radiohead\" with an ARTISTALIAS), a spelling for another locale (TITLEALIAS-JA in an `en` library), a second spelling of the same alias, or a value that is the name itself. The writers store at most ONE alias per entity; the rest is excess — Optimize tags (23) clears it for the album you run it on (no re-encode, no format pass), and Format all (10) does it library-wide." },
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
        { k: "export_structure", label: "Folder structure", type: "select", options: [["albumartist_album_disc", "Album artist / Album / 1-01 Title"], ["album", "Album / 01 - Title"], ["flat", "Flat — one folder"], ["mirror", "Mirror library layout"], ["custom", "Custom — the script below"]], help: "The shipped structure writes the library's own file name — \"1-01 Title\", the disc number included — under ALBUMARTIST/Album, so a multi-disc album keeps its discs apart and a compilation stays one folder. The labels are the export menu's own (server.exporter.STRUCTURE_LABELS)." },
        { k: "export_structure_script", label: "Custom structure script (used when the structure above is Custom)", type: "text", help: "The same Picard-style grammar the library's naming script uses: %field% substitution, $if(a,b,c), \"/\" for folders. A field the app does not know, or a script that names no path, is refused with a sentence." },
        { k: "export_codec", label: "Audio", type: "select", options: [["copy", "Copy (no re-encode)"], ["flac", "FLAC"], ["mp3", "MP3"], ["opus", "Opus"]] },
        { k: "export_quality", label: "Lossy quality (blank = codec default)", type: "text" },
        { k: "export_subfolder", label: "Subfolder inside the destination", type: "text" },
        { k: "export_workers", label: "Parallel workers (0 = count them from the CPU)", type: "number", min: 0, max: 32 },
        { k: "export_verify", label: "Verify every written file", type: "bool" },
        { k: "export_prune", label: "Delete the destination leftovers first", type: "bool" },
        /* The file selection the Export page saves as this device's default
           (`export_copy_files`): the same ELEVEN families its own checkboxes
           draw — the keys of server.exporter.FILE_FAMILIES, in that table's
           own order — named here for installs whose exports are always the
           same shape. A family left out stays in the library and is named in
           the run's report; an empty selection is NOT "copy nothing" — it
           means nobody has chosen, so the classic sidecar switch below still
           decides. */
        { k: "export_copy_files", label: "What an export copies (blank = the switch below decides)", type: "multi", options: [["audio", "The tracks themselves"], ["cover", "Covers"], ["lyrics", "Lyrics (.lrc)"], ["cue", "Cue sheets (.cue)"], ["log", "Rip log (.log)"], ["accurip", "AccurateRip report (.accurip)"], ["checksum", "Checksum lists and .torrent"], ["text", "Notes, links and scans"], ["playlist", "Playlists the album carries"], ["other", "Anything else"]], help: "The families an export writes BESIDE the audio, as the Export page's own 'What gets copied' section offers them. A family left out stays in the library and is named in the run's report — never dropped in silence." },
        /* The switch the Export page's file selection replaced
           (`export_copy_files`): it still decides WHEN no selection has been
           saved, so a device that always wants the classic set can be served
           from here. A selection saved in the export form wins over it. */
        { k: "export_sidecars", label: "Export the classic sidecar set (cover/lrc/cue/log/accurip) when no file selection is saved", type: "bool" },
        { k: "export_clean_tags", label: "Write only canonical tags", type: "bool" },
        { k: "export_id3v2", label: "ID3v2 version for MP3", type: "select", options: [["2.3", "2.3"], ["2.4", "2.4"]] },
        { k: "export_id3v1", label: "Also write ID3v1 (MP3)", type: "bool" },
        { k: "export_replaygain_mode", label: "ReplayGain in an export", type: "select", options: [["off", "Off"], ["tags", "Write ReplayGain tags (the player applies them)"], ["apply", "Bake the gain into the audio"]] },
        { k: "export_eq_profile", label: "Equalizer profile (preset or imported profile id, blank = none)", type: "text" },
        /* "" is a real choice here, not a missing value: an export with nothing
           saved writes lyrics the way the LIBRARY does (its own
           `lyrics_format`), which is what `server.exporter.lyrics_mode`
           resolves and what the Export page opens on. */
        { k: "export_lyrics", label: "Lyrics in an export", type: "select", options: [["", "Follow the library's lyrics setting"], ["embedded", "Embedded in the file"], ["lrc", ".lrc files beside the audio"], ["both", "Both"]] },
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
      blurb: "The events this server pushes to every client that has notifications enabled — an import that needs a decision, a finished import, a store the server pruned on its own. Each client still asks for its own permission.",
      fields: [
        { k: "notify_import_start", label: "Notify when an import starts", type: "bool", help: "One notification per album when the import chain picks it up — from Add to library, the wizard or the panel." },
        { k: "notify_import_done", label: "Notify when an import finishes", type: "bool", help: "One notification per album when the chain has been over it, with its one-line summary (how many scripts ran, what failed)." },
      ],
    },
];

/** The groups the wizard's steps unfold. A group this does not name renders
 *  folded, with its shipped defaults one click away — a wall of 50 checkboxes
 *  is how a first run gets abandoned — and a group no step renders at all is
 *  the Settings page's, which is where the closing screen sends the reader. */
export const OPEN_GROUPS: Record<string, true> = {
  Dependencies: true,
  "Login gate": true,
};

/** The wizard, in order: a first run is asked only what it cannot answer for
 *  itself — where the library is, who may read it, the programs the scripts
 *  need, and the credentials the sources ask for.
 *
 *  Every quality and check knob is deliberately absent. Grading and the audit
 *  ship strict (mlo/config.py DEFAULT_CONFIG, held there by
 *  tools/test_setup_coverage.py), so a step asking about them could only offer
 *  a first run the chance to relax what it has no reason to touch — and every
 *  one of those groups is still rendered by the Settings page, so naming them
 *  here again would be a second screen for the same switch. */
export const SETUP_STEPS: SetupStep[] = [
  {
    label: "Folder",
    title: "Your music library",
    blurb:
      "Everything the app grades, tags and optimizes lives under one folder (your artist/album tree). The app's own state (data, downloads, trash) lives inside it too, so it moves with the library.",
    panel: "folder",
  },
  {
    label: "Account",
    title: "Your account",
    blurb:
      "This server's own login. The password is required — the wizard will not move on without one, because everything after this step can be read by anyone who can reach the address. The name is optional: leave it as it is (admin) or blank it, and the install keeps an unnamed owner; either way it is editable later in Settings → Sign-in & security.",
    panel: "password",
    groups: ["Login gate"],
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
      "What the app uses for lyrics, genres, ratings and artwork. The credentials below are the only thing a first run has to paste, and every one of them is optional — a source without its key is simply skipped, saved keys are re-tested as you save them, and all of it is editable later in Settings → Sources (where the provider rows, their orders and the live status of each one live too). " +
      "Cookies are a cookies.txt in Netscape format — what a browser-extension exporter like \"Get cookies.txt\" writes — imported on Settings' Discovery tab for RYM.",
    panel: "sources",
  },
  {
    label: "Done",
    title: "You're all set",
    blurb:
      "Everything you skipped keeps its shipped default — and those defaults are the strict ones, so a fresh install audits and grades the library the way the app intends. Every setting is still editable in Settings.",
    panel: "done",
  },
];

/** Keys the wizard writes itself rather than through a control: the folder its
 *  picker saves, and the flag its own buttons set when the run finishes. */
export const WIZARD_MANAGED_KEYS: string[] = ["music_folder", "first_run_done"];

/** Config keys no screen offers: every OTHER key of the app is a field of one
 *  of the groups above, which the Settings page renders whether or not a setup
 *  step does. */
export const HIDDEN_KEYS: string[] = [
  // The password hash is refused by POST /api/config (it must be: a session
  // could otherwise replace the credential with a hash of its choosing) and is
  // set through /api/auth/setup, which is where the wizard's own password
  // fields send it. Reading it back would publish the credential too.
  "auth_password_hash",
  // The per-cookie notes the cookie logins keep (server/api_cookies.py): the
  // comment the user wrote against an imported cookie, and the expiry an
  // import remembered for a credential that has nowhere to carry one. They are
  // edited in the cookie panel itself — next to the cookie they describe —
  // never in a raw JSON box, so no Settings row offers this key.
  "cookie_notes",
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
  "force_reencode_flac",
  "force_reencode_images",
  "force_tracklist",
  "force_xlit",
  // The one-time move of the ENCODER_VERSION default (v4.4.0, ON -> OFF):
  // `normalize_config` rewrites a stored `true` it finds while this flag is
  // unset, then sets the flag, so a later `true` (the user re-enabling the row
  // in Settings → Encoder Tags) is a decision and stays. Nothing to show: it
  // records that the rewrite ran, and the rows it is about are visible.
  "encoder_tags_version_default_moved",
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
