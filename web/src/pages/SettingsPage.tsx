import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { FolderOpen, Save, RotateCcw, Settings as SettingsIcon, Check, Eye, EyeOff } from "lucide-react";
import { api } from "../api";
import ConfirmButton from "../components/ConfirmButton";
import { toast } from "../store";
import { applyAccent } from "../App";

const DEFAULT_NAMING_SCRIPT =
  "%albumartist% [%musicbrainz_albumartistid%]/$if(%releasetype%,[%releasetype%] ,)$if(%originaldate%,%originaldate% - ,)$if(%date%,%date% - ,)%album% {$if(%releasecountry%,%releasecountry% - )%media%$if(%catalognumber%, - %catalognumber%)}/%discnumber%-$num(%tracknumber%,2) %title%";

const ACCENT_OPTIONS: { id: string; name: string; color: string }[] = [
  { id: "violet", name: "Violet", color: "#8b5cf6" },
  { id: "pink", name: "Pink", color: "#ec4899" },
  { id: "emerald", name: "Emerald", color: "#10b981" },
  { id: "sky", name: "Sky", color: "#0ea5e9" },
  { id: "amber", name: "Amber", color: "#f59e0b" },
  { id: "red", name: "Red", color: "#ef4444" },
  { id: "mono", name: "Black & white", color: "#ffffff" },
];

const ENCODER_FORMATS = ["flac", "jpeg", "png", "jxl"] as const;
const ENCODER_FIELDS = ["ENCODER_PROGRAM", "ENCODER_QUALITY", "ENCODER_VERSION"] as const;
const AUDIO_TYPES = ["flac", "mp3", "mp4", "ogg", "opus", "aac"] as const;
const TAG_FAMILIES = ["AUDIT", "LOG_GRADE", "REPLAYGAIN", "DYNAMIC_RANGE", "MEDIA_SOURCE", "INSTRUMENTAL", "ADVISORY", "LYRICS", "BPM", "INITIALKEY"] as const;

export default function SettingsPage() {
  const { data: config } = useQuery({ queryKey: ["config"], queryFn: api.config });
  // open-source credits (vendored tools + packages), rendered at the bottom
  const { data: credits } = useQuery({
    queryKey: ["credits"],
    queryFn: async () => {
      const r = await fetch("/credits.json");
      return r.json();
    },
    staleTime: Infinity,
  });
  const qc = useQueryClient();
  const [musicFolder, setMusicFolder] = useState("");
  const [lyricsFormat, setLyricsFormat] = useState("EMBEDDED");
  const [workerLimit, setWorkerLimit] = useState(0);
  const [namingScript, setNamingScript] = useState(DEFAULT_NAMING_SCRIPT);
  const [shortFolderNames, setShortFolderNames] = useState(false);
  const [accent, setAccent] = useState<string>(() => localStorage.getItem("mlo.accent") ?? "mono");
  const [defaultView, setDefaultView] = useState<string>(() => localStorage.getItem("mlo.defaultView.v2") ?? "grid");
  const [loaded, setLoaded] = useState(false);
  // Settings search: matches field labels/keys across every tab; picking a
  // result jumps straight to the tab that owns it (computed below, after
  // the group tables exist).
  const [q, setQ] = useState("");
  const searching = q.trim().length >= 2;
  // which password fields are currently revealed
  const [showPasswords, setShowPasswords] = useState<Set<string>>(new Set());

  // ---- script options (persisted to config; /api/run uses them as defaults) ----
  type CfgField =
    | { k: string; label: string; type: "bool" }
    | { k: string; label: string; type: "number"; min?: number; max?: number; step?: number }
    | { k: string; label: string; type: "select"; options: [string, string][] }
    | { k: string; label: string; type: "text" }
    | { k: string; label: string; type: "password" };
  interface CfgGroup {
    title: string;
    blurb?: string;
    fields: CfgField[];
    presets?: { name: string; values: Record<string, string> }[];
  }
  const CFG_GROUPS: CfgGroup[] = [
    {
      title: "FLACs & lossless sources (script 3)",
      blurb: "Re-encodes FLACs at the target level and converts uncompressed sources (WAV/AIFF/APE/WV/SHN) to FLAC losslessly.",
      fields: [
        { k: "optimize_convert_lossless", label: "Convert WAV/AIFF/APE/WV to FLAC", type: "bool" },
        { k: "lossless_remove_original", label: "Remove original after verified conversion", type: "bool" },
        { k: "flac_level", label: "Compression level", type: "number", min: 0, max: 8 },
        { k: "add_seektables", label: "Add seektables", type: "bool" },
        { k: "flac_preserve_picture", label: "Preserve embedded picture", type: "bool" },
        { k: "flac_no_padding", label: "No padding", type: "bool" },
        { k: "force_reencode_flac", label: "Force re-encode", type: "bool" },
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
        { k: "force_reencode_images", label: "Force re-process", type: "bool" },
      ],
    },
    {
      title: "Lyrics & CUEs (scripts 1 & 2)",
      fields: [
        { k: "optimize_lrc", label: "Optimize .lrc sidecars", type: "bool" },
        { k: "optimize_embedded_lyrics", label: "Optimize embedded lyrics", type: "bool" },
        { k: "lrc_timestamp_precision", label: "Timestamp precision (decimals)", type: "number", min: 2, max: 3 },
        { k: "lrc_strip_metadata", label: "Strip metadata tags ([ti:], [ar:])", type: "bool" },
        { k: "lrc_collapse_blank_lines", label: "Collapse blank lines", type: "bool" },
        { k: "lrc_enhanced_enabled", label: "Enhanced LRC (word timestamps)", type: "bool" },
        { k: "lrc_enhanced_word_sync", label: "Enhanced LRC word sync", type: "bool" },
        {
          k: "lrc_sync_level", label: "Required lyrics sync level", type: "select",
          options: [["LINE", "Line timestamps only (default)"], ["WORD", "Word"], ["SYLLABLE", "Syllable"]],
        },
        { k: "lrc_extended_enabled", label: "Extended LRC (E-LRC)", type: "bool" },
        { k: "lrc_add_zero_timestamp", label: "Add [00:00.00] opening line", type: "bool" },
        { k: "lrc_zero_timestamp_blank", label: "Zero timestamp is blank line", type: "bool" },
        { k: "lrc_zero_timestamp_target", label: "Zero timestamp target", type: "select", options: [["EMBEDDED", "Embedded"], ["LRC", "LRC sidecar"], ["BOTH", "Both"]] },
        { k: "append_final_newline", label: "Append final newline", type: "bool" },
        { k: "keep_empty_cue_lines", label: "Keep empty CUE lines", type: "bool" },
        { k: "keep_other_cue_lines", label: "Keep non-track CUE lines", type: "bool" },
        { k: "keep_empty_accurip_lines", label: "Keep empty .accurip lines", type: "bool" },
        { k: "cue_file_type", label: "CUE file type", type: "select", options: [["WAVE", "WAVE"], ["MP3", "MP3"]] },
        { k: "force_lyrics", label: "Force lyrics re-format", type: "bool" },
        { k: "force_cue", label: "Force CUE re-format", type: "bool" },
      ],
    },
    {
      title: "DR / ReplayGain (script 7)",
      fields: [
        { k: "dr_replaygain_enabled", label: "Enabled", type: "bool" },
        { k: "replaygain_skip_existing", label: "Skip files that already have RG tags", type: "bool" },
        { k: "force_dr_replaygain", label: "Force re-run", type: "bool" },
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
        { k: "audit_integrity", label: "Write integrity tags (AUDIO_MD5)", type: "bool" },
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
        { k: "audit_ai", label: "Detect AI-upscaled audio", type: "bool" },
        { k: "audit_fake_stereo", label: "Detect fake stereo", type: "bool" },
        { k: "audit_silence", label: "Detect silence", type: "bool" },
        { k: "audit_dynamic_range", label: "Measure dynamic range", type: "bool" },
        { k: "audit_true_peak", label: "Measure true peak", type: "bool" },
        { k: "audit_lufs", label: "Measure LUFS", type: "bool" },
        { k: "audit_bpm", label: "Measure BPM", type: "bool" },
        { k: "audit_check_cd_format", label: "Verify CD format (16/44.1)", type: "bool" },
        { k: "force_audit", label: "Force re-audit", type: "bool" },
      ],
    },
    {
      title: "AutoTag (script 8)",
      fields: [
        { k: "auto_advisory", label: "Set advisory automatically", type: "bool" },
        { k: "auto_instrumental", label: "Set INSTRUMENTAL automatically", type: "bool" },
        { k: "auto_zero_advisory_for_instrumental", label: "Zero advisory on instrumentals", type: "bool" },
        { k: "fix_instrumental_from_lyrics", label: "Fix INSTRUMENTAL from lyrics", type: "bool" },
        { k: "force_auto_tag", label: "Force re-tag", type: "bool" },
      ],
    },
    {
      title: "AccurateRip (script 9)",
      fields: [
        { k: "write_accurip_files", label: "Write .accurip files", type: "bool" },
        { k: "force_accurip", label: "Force re-generate", type: "bool" },
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
      ],
    },
    {
      title: "CD Rips (scripts 2/6/9/10)",
      blurb: "Deterministic CD-N renaming of .log/.cue/.accurip and conservative CUE FILE-name fixes.",
      fields: [
        { k: "discs_rename_enabled", label: "Auto-rename disc sheets to CD-{n}", type: "bool" },
        { k: "discs_rename_single_fallback", label: "Rename lone sheet in single-disc album", type: "bool" },
        { k: "discs_rename_pattern", label: "Rename pattern (must contain {n})", type: "text" },
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
      ],
    },
    {
      title: "Videos (script 11)",
      blurb: "Lossless remux: any video container → MKV with the video copied bit-exact and every audio stream re-encoded to FLAC (lossless, level below). Captions/subtitles are always kept and verified — never removed. If the muxer refuses the video codec, H.264 is a last-resort fallback. The original (e.g. the .VOB) is removed after a verified remux.",
      fields: [
        { k: "video_reencode_incompatible", label: "Allow H.264 video fallback (last resort)", type: "bool" },
        { k: "video_crf", label: "H.264 CRF (lower = better)", type: "number", min: 0, max: 51 },
        { k: "video_preset", label: "H.264 preset", type: "select", options: [["ultrafast","ultrafast"],["superfast","superfast"],["veryfast","veryfast"],["faster","faster"],["fast","fast"],["medium","medium"],["slow","slow"],["slower","slower"],["veryslow","veryslow"]] },
        { k: "video_flac_level", label: "FLAC compression (0-8)", type: "number", min: 0, max: 8 },
        { k: "video_remove_original", label: "Remove original after verified remux", type: "bool" },
        { k: "video_process_mp4", label: "Also re-mux MP4s into MKV", type: "bool" },
      ],
    },
    {
      title: "AI-assisted lyrics",
      blurb: "Any OpenAI-compatible chat endpoint — Google Gemini (OpenAI-compatible endpoint, key from AI Studio), OpenAI, OpenRouter, LM Studio, llama.cpp. Powers AI lyrics clean/repair and the fullscreen player's translation + transliteration.",
      presets: [
        { name: "Google Gemini", values: { ai_base_url: "https://generativelanguage.googleapis.com/v1beta/openai", ai_model: "gemini-3.5-flash-lite" } },
        { name: "OpenAI", values: { ai_base_url: "https://api.openai.com/v1", ai_model: "gpt-4o-mini" } },
        { name: "OpenRouter", values: { ai_base_url: "https://openrouter.ai/api/v1", ai_model: "openai/gpt-4o-mini" } },
        { name: "Ollama (local)", values: { ai_base_url: "http://localhost:11434/v1", ai_model: "llama3.2" } },
        { name: "LM Studio (local)", values: { ai_base_url: "http://localhost:1234/v1", ai_model: "local-model" } },
      ],
      fields: [
        { k: "ai_base_url", label: "Base URL", type: "text" },
        { k: "ai_api_key", label: "API key", type: "text" },
        { k: "ai_model", label: "Model (e.g. gemini-3.5-flash-lite)", type: "text" },
        {
          k: "ai_effort", label: "Reasoning effort (all AI features)", type: "select",
          options: [["high", "High — maximum thinking (default)"], ["medium", "Medium"], ["low", "Low"], ["minimal", "Minimal — fastest, no thinking"]],
        },
        { k: "lyrics_xlit_enabled", label: "Transliteration enabled (script 15)", type: "bool" },
        { k: "lyrics_translate_enabled", label: "Translation enabled (script 15)", type: "bool" },
        { k: "lyrics_translation_langs", label: "Translation languages (comma separated, e.g. en,de)", type: "text" },
        { k: "lyrics_xlit_sidecars", label: "Write .romaji.lrc / .<lang>.lrc sidecars", type: "bool" },
        { k: "ai_translate_lang", label: "Fullscreen player translation language (e.g. en, de)", type: "text" },
      ],
    },
    {
      title: "Key & BPM (script 12)",
      blurb: "Detects tempo and musical key with librosa and writes BPM + INITIALKEY. Install librosa from the Dependencies tab.",
      fields: [
        { k: "audiometa_enabled", label: "Enabled", type: "bool" },
        { k: "audiometa_overwrite", label: "Overwrite existing BPM/key values", type: "bool" },
        { k: "audiometa_min_seconds", label: "Skip tracks shorter than (seconds)", type: "number", min: 1, max: 120 },
        { k: "audiometa_key_notation", label: "Key notation", type: "select", options: [["musical", "Musical (A min)"], ["camelot", "Camelot (8A)"], ["openkey", "Open Key (1m)"]] },
        { k: "force_audiometa", label: "Force re-analysis", type: "bool" },
      ],
    },
    {
      title: "Soulseek (managed slskd)",
      blurb: "Shares = your music folder. Downloads land in the download dir below; use the Soulseek page to search and download. Install slskd from the Dependencies tab.",
      fields: [
        { k: "soulseek_username", label: "Soulseek username", type: "text" },
        { k: "soulseek_password", label: "Soulseek password", type: "password" },
        { k: "soulseek_description", label: "Profile description (shown to other users)", type: "text" },
        { k: "soulseek_listen_port", label: "Listen port", type: "number", min: 1024, max: 65535 },
        { k: "soulseek_web_port", label: "Web/API port", type: "number", min: 1024, max: 65535 },
        { k: "soulseek_up_limit", label: "Upload speed limit (kB/s, 0 = unlimited)", type: "number", min: 0, max: 100000 },
        { k: "soulseek_down_limit", label: "Download speed limit (kB/s, 0 = unlimited)", type: "number", min: 0, max: 100000 },
        { k: "soulseek_download_dir", label: "Download dir (blank = <music folder>/.mlo_downloads)", type: "text" },
        { k: "soulseek_autostart", label: "Start slskd with the app backend", type: "bool" },
        { k: "soulseek_share_library", label: "Share the music folder on the network", type: "bool" },
      ],
    },
    {
      title: "Auto-import (MusicBrainz → Soulseek)",
      blurb: "Search terms are templates of release fields (artist album year date country catalognumber barcode label). CD rips are found by catalog number, digital media by title + year; every disc's .log must reach the score threshold before the album downloads.",
      fields: [
        { k: "soulseek_auto_cd_queries", label: "CD query templates (; separated)", type: "text" },
        { k: "soulseek_auto_digital_queries", label: "Digital query templates (; separated)", type: "text" },
        { k: "soulseek_auto_log_min_score", label: "Min .log score (0–100)", type: "number", min: 0, max: 100 },
        { k: "soulseek_auto_complete_ratio", label: "Required track completeness (0.5–1)", type: "number", min: 0.5, max: 1, step: 0.05 },
      ],
    },
    {
      title: "Import & tag cleanup",
      blurb: "Genre importing from MusicBrainz and tag hygiene applied while optimizing.",
      fields: [
        { k: "mb_genre_count", label: "Genres imported per release (MusicBrainz)", type: "number", min: 1, max: 10 },
        { k: "strip_unknown_tags", label: "Remove non-canonical tags on optimize (script 10)", type: "bool" },
      ],
    },
    {
      title: "Beets tagging (script 14)",
      blurb: "Managed beets import with Picard-parity behaviors: MusicBrainz matching, locale alias translations, WORK/MOVEMENT from work relationships, and release-type capitalization (EP uppercased). Files are organized by your naming script via the mlo_dir path hook. Runs as part of Run All; skipped quietly when beets isn't installed.",
      fields: [
        { k: "beets_locale", label: "Preferred locale for aliases (e.g. en, ja, de)", type: "text" },
        { k: "beets_translations", label: "Translate titles/names to preferred locale", type: "bool" },
        { k: "beets_work_movement", label: "Write WORK / MOVEMENT from work relationships", type: "bool" },
        { k: "beets_release_type_caps", label: "Capitalize release types (EP uppercased)", type: "bool" },
        { k: "beets_organize_after", label: "Re-run organize after each beets import", type: "bool" },
      ],
    },
  ];
  const GRADE_CHECK_KEYS: CfgField[] = [
    { k: "grade_check_tag_spaces", label: "Tag spaces", type: "bool" },
    { k: "grade_check_lyrics_spaces", label: "Lyrics spaces", type: "bool" },
    { k: "grade_check_cue_spaces", label: "CUE spaces", type: "bool" },
    { k: "grade_check_cover_crop", label: "Cover crop", type: "bool" },
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
    { k: "grade_check_disallowed", label: "Disallowed files", type: "bool" },
    { k: "grade_check_extra_images", label: "Extra artwork (images not tied to a track)", type: "bool" },
    { k: "grade_check_xlit", label: "Transliteration (script 15)", type: "bool" },
    { k: "grade_check_trans", label: "Translation (script 15)", type: "bool" },
    { k: "grade_check_naming", label: "Naming script paths", type: "bool" },
    { k: "grade_check_filename_case", label: "Filename capitalization (exact case)", type: "bool" },
    { k: "grade_check_ext_case", label: "Lowercase file extensions", type: "bool" },
    { k: "grade_check_excess_tags", label: "Excess tags (non-canonical)", type: "bool" },
    { k: "grade_check_key_bpm", label: "Key & BPM tags", type: "bool" },
    { k: "grade_check_lyrics_lang_tags", label: "Transform tags carry language (TRANSLATION-EN)", type: "bool" },
  ];
  const ALL_CFG_KEYS = [...CFG_GROUPS.flatMap((g) => g.fields), ...GRADE_CHECK_KEYS].map((f) => f.k);
  const [scriptCfg, setScriptCfg] = useState<Record<string, unknown>>({});
  const setCfg = (k: string, v: unknown) => setScriptCfg((c) => ({ ...c, [k]: v }));
  const [previewPath, setPreviewPath] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [rawConfig, setRawConfig] = useState("{}");
  const [tab, setTab] = useState("general");
  const [runAll, setRunAll] = useState<number[]>([11, 14, 1, 2, 8, 13, 12, 3, 5, 9, 6, 4, 7, 10]);
  const [beetsBusy, setBeetsBusy] = useState(false);
  const { data: beetsStatus, refetch: refetchBeets } = useQuery({
    queryKey: ["beetsStatus"],
    queryFn: api.beetsStatus,
    enabled: tab === "beets",
  });
  const installBeets = async () => {
    setBeetsBusy(true);
    try {
      const r = await api.beetsInstall();
      toast(`beets v${r.version} installed`);
      refetchBeets();
    } catch (e) {
      toast(String(e));
    } finally {
      setBeetsBusy(false);
    }
  };

  const RUN_ALL_SCRIPTS: { id: number; label: string }[] = [
    { id: 11, label: "Video Remux" },
    { id: 14, label: "Beets Tag" },
    { id: 1, label: "Lyrics" },
    { id: 2, label: "CUEs" },
    { id: 8, label: "AutoTag" },
    { id: 13, label: "Lyrics Fetch" },
    { id: 3, label: "FLACs" },
    { id: 5, label: "Images" },
    { id: 9, label: "AccurateRip" },
    { id: 6, label: "Audit" },
    { id: 4, label: "Grade" },
    { id: 7, label: "DR / ReplayGain" },
    { id: 10, label: "Format All" },
    { id: 12, label: "Key & BPM" },
  ];

  // Every per-script force switch. They also live in their own script tab;
  // both places bind to the same config keys, and the master toggle below
  // flips them all at once.
  const FORCE_KEYS: { k: string; label: string }[] = [
    { k: "force_lyrics", label: "1 · Lyrics re-format" },
    { k: "force_cue", label: "2 · CUE re-format" },
    { k: "force_reencode_flac", label: "3 · FLAC re-encode" },
    { k: "force_reencode_images", label: "5 · Image re-process" },
    { k: "force_audit", label: "6 · Re-audit" },
    { k: "force_dr_replaygain", label: "7 · DR / ReplayGain re-run" },
    { k: "force_auto_tag", label: "8 · AutoTag re-run" },
    { k: "force_accurip", label: "9 · AccurateRip re-generate" },
    { k: "force_audiometa", label: "12 · Key & BPM re-analysis" },
    { k: "force_xlit", label: "15 · Lyrics xlit/translate re-run" },
  ];

  const NAV: { id: string; label: string; section?: string }[] = [
    { id: "general", label: "General" },
    { id: "appearance", label: "Appearance" },
    { id: "naming", label: "Naming" },
    { id: "tagwrites", label: "Tagging" },
    { id: "grading", label: "Grading" },
    { id: "ai", label: "AI", section: "Integrations" },
    { id: "beets", label: "Beets", section: "Integrations" },
    { id: "soulseek", label: "Soulseek", section: "Integrations" },
    { id: "autoimport", label: "Auto-import", section: "Integrations" },
    { id: "deps", label: "Dependencies", section: "Integrations" },
    { id: "flac", label: "FLACs & lossless", section: "Scripts" },
    { id: "embedcovers", label: "Embedded covers", section: "Scripts" },
    { id: "images", label: "Images", section: "Scripts" },
    { id: "lyrics", label: "Lyrics & CUEs", section: "Scripts" },
    { id: "dr", label: "DR / ReplayGain", section: "Scripts" },
    { id: "audit", label: "Audit", section: "Scripts" },
    { id: "autotag", label: "AutoTag", section: "Scripts" },
    { id: "accurip", label: "AccurateRip", section: "Scripts" },
    { id: "cdrips", label: "CD Rips", section: "Scripts" },
    { id: "videos", label: "Videos", section: "Scripts" },
    { id: "audiometa", label: "Key & BPM", section: "Scripts" },
    { id: "importtags", label: "Import & tags", section: "Scripts" },
  ];

  const runPreview = async () => {
    setPreviewing(true);
    setPreviewError(null);
    try {
      const r = await api.namingPreview(namingScript, shortFolderNames);
      if (r.ok && r.path) {
        setPreviewPath(r.path);
      } else {
        setPreviewError(r.error || "Script produced an empty path");
        setPreviewPath(null);
      }
    } catch (e) {
      setPreviewError(String(e));
      setPreviewPath(null);
    } finally {
      setPreviewing(false);
    }
  };

  useEffect(() => {
    if (!config || loaded) return;
    setMusicFolder(String(config.music_folder ?? ""));
    setLyricsFormat(String(config.lyrics_format ?? "EMBEDDED"));
    setWorkerLimit(Number(config.worker_limit ?? 0));
    setNamingScript(String(config.naming_script ?? "") || DEFAULT_NAMING_SCRIPT);
    setShortFolderNames(!!config.short_folder_names);
    setScriptCfg(Object.fromEntries(ALL_CFG_KEYS.map((k) => [k, config[k]])));
    const enc = (config.encoder_tags ?? {}) as Record<string, Record<string, boolean>>;
    const aw = (config.audio_tag_writes ?? {}) as Record<string, Record<string, boolean>>;
    setEncoderTags(
      Object.fromEntries(
        ENCODER_FORMATS.map((fmt) => [fmt, Object.fromEntries(ENCODER_FIELDS.map((f) => [f, !!enc[fmt]?.[f]]))])
      )
    );
    setAudioTagWrites(
      Object.fromEntries(
        AUDIO_TYPES.map((t) => [t, Object.fromEntries(TAG_FAMILIES.map((fam) => [fam, aw[t]?.[fam] ?? true]))])
      )
    );
    setRawConfig(JSON.stringify(config, null, 2));
    setRunAll(Array.isArray(config.run_all_order) ? config.run_all_order.map(Number).filter((n) => n >= 1 && n <= 15) : [11, 14, 1, 2, 8, 13, 15, 12, 3, 5, 9, 6, 4, 7, 10]);
    setLoaded(true);
  }, [config, loaded]);

  const pickAccent = (id: string) => {
    setAccent(id);
    localStorage.setItem("mlo.accent", id);
    applyAccent(id);
  };

  const pickDefaultView = (v: string) => {
    setDefaultView(v);
    localStorage.setItem("mlo.defaultView.v2", v);
  };

  const pickNative = async () => {
    if (!(window as any).__TAURI_INTERNALS__) {
      toast("Native picker is only available in the desktop app");
      return;
    }
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const picked = await invoke<string | null>("pick_folder");
      if (picked) setMusicFolder(picked);
    } catch (e) {
      toast(String(e));
    }
  };

  const { data: configDefaults } = useQuery({
    queryKey: ["configDefaults"],
    queryFn: api.configDefaults,
  });

  /** Restore factory defaults for everything the settings form edits.
   * Identity-critical values the user configured are kept: music folder,
   * first-run flag and the whole AI connection (endpoint, model, key).
   * Persisted via the normal Save. */
  const resetAllDefaults = () => {
    const d = configDefaults as Record<string, unknown> | undefined;
    if (!d) return;
    const cur = scriptCfg as Record<string, unknown>;
    const next: Record<string, unknown> = { ...d };
    for (const k of ["music_folder", "first_run_done", "ai_api_key", "ai_base_url", "ai_model"]) {
      if (cur[k] !== undefined) next[k] = cur[k];
    }
    setScriptCfg(next);
    if (d.encoder_tags) setEncoderTags(d.encoder_tags as Record<string, Record<string, boolean>>);
    if (d.audio_tag_writes) setAudioTagWrites(d.audio_tag_writes as Record<string, Record<string, boolean>>);
    if (typeof d.lyrics_format === "string") setLyricsFormat(d.lyrics_format);
    if (d.worker_limit !== undefined) setWorkerLimit(Number(d.worker_limit));
    if (typeof d.naming_script === "string") setNamingScript(d.naming_script);
    setShortFolderNames(!!d.short_folder_names);
    if (Array.isArray(d.run_all_order)) setRunAll((d.run_all_order as number[]).map(Number));
    toast("Settings reset to defaults — click Save all settings to persist");
  };

  const save = async () => {
    try {
      await api.saveConfig({
        ...config,
        ...scriptCfg,
        encoder_tags: encoderTags,
        audio_tag_writes: audioTagWrites,
        music_folder: musicFolder,
        lyrics_format: lyricsFormat,
        worker_limit: workerLimit,
        naming_script: namingScript,
        short_folder_names: shortFolderNames,
        run_all_order: runAll,
      });
      toast("Config saved");
      qc.invalidateQueries({ queryKey: ["config"] });
      qc.invalidateQueries({ queryKey: ["library"] });
    } catch (e) {
      toast(String(e));
    }
  };

  const applyRaw = () => {
    try {
      const parsed = JSON.parse(rawConfig) as Record<string, unknown>;
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new Error("expected an object");
      const enc = (parsed.encoder_tags ?? {}) as Record<string, Record<string, boolean>>;
      const aw = (parsed.audio_tag_writes ?? {}) as Record<string, Record<string, boolean>>;
      setScriptCfg(Object.fromEntries(ALL_CFG_KEYS.map((k) => [k, parsed[k]])));
      setEncoderTags(
        Object.fromEntries(
          ENCODER_FORMATS.map((fmt) => [fmt, Object.fromEntries(ENCODER_FIELDS.map((f) => [f, !!enc[fmt]?.[f]]))])
        )
      );
      setAudioTagWrites(
        Object.fromEntries(
          AUDIO_TYPES.map((t) => [t, Object.fromEntries(TAG_FAMILIES.map((fam) => [fam, aw[t]?.[fam] ?? true]))])
        )
      );
      setMusicFolder(String(parsed.music_folder ?? musicFolder));
      setLyricsFormat(String(parsed.lyrics_format ?? lyricsFormat));
      setWorkerLimit(Number(parsed.worker_limit ?? workerLimit));
      setNamingScript(String(parsed.naming_script ?? "") || namingScript);
      setShortFolderNames(!!parsed.short_folder_names);
      setRunAll(Array.isArray(parsed.run_all_order) ? parsed.run_all_order.map(Number).filter((n: number) => n >= 1 && n <= 15) : runAll);
      toast("Raw config applied — click Save all settings to persist");
    } catch (e) {
      toast("Invalid JSON: " + String(e));
    }
  };

  // Tab → group, matched by title (robust against group reordering).
  const GROUP_BY_TAB: Record<string, CfgGroup> = Object.fromEntries(
    [
      ["flac", "FLACs"], ["embedcovers", "Embedded covers"], ["images", "Images"], ["lyrics", "Lyrics & CUEs"],
      ["dr", "DR / ReplayGain"], ["audit", "Audit"], ["autotag", "AutoTag"],
      ["accurip", "AccurateRip"], ["tagwrites", "Tag writes"],
      ["grading", "Grading"], ["cdrips", "CD Rips"], ["videos", "Videos"],
      ["ai", "AI-assisted"], ["audiometa", "Key & BPM"], ["beets", "Beets tagging"],
      ["soulseek", "Soulseek (managed slskd)"], ["autoimport", "Auto-import"],
      ["importtags", "Import & tag cleanup"],
    ].map(([tab, prefix]) => [
      tab,
      CFG_GROUPS.find((g) => g.title.toLowerCase().startsWith(String(prefix).toLowerCase())),
    ])
  ) as Record<string, CfgGroup>;

  const searchHits = (() => {
    if (!searching) return [] as { tab: string; group: string; field: string }[];
    const needle = q.trim().toLowerCase();
    const hits: { tab: string; group: string; field: string }[] = [];
    const tabFor = (title: string) =>
      Object.entries(GROUP_BY_TAB).find(([, g]) => g?.title === title)?.[0] ?? "";
    for (const g of CFG_GROUPS) {
      const tab = tabFor(g.title);
      for (const f of g.fields) {
        if (f.label.toLowerCase().includes(needle) || f.k.toLowerCase().includes(needle)) {
          hits.push({ tab, group: g.title, field: f.label });
        }
      }
    }
    for (const c of GRADE_CHECK_KEYS) {
      if (c.label.toLowerCase().includes(needle) || c.k.toLowerCase().includes(needle)) {
        hits.push({ tab: "grading", group: "Grading checks", field: c.label });
      }
    }
    return hits.slice(0, 24);
  })();

  const [encoderTags, setEncoderTags] = useState<Record<string, Record<string, boolean>>>({});
  const toggleEncoder = (fmt: string, field: string, on: boolean) =>
    setEncoderTags((e) => ({ ...e, [fmt]: { ...e[fmt], [field]: on } }));

  const [audioTagWrites, setAudioTagWrites] = useState<Record<string, Record<string, boolean>>>({});
  const toggleTagWrite = (ftype: string, fam: string, on: boolean) =>
    setAudioTagWrites((a) => ({ ...a, [ftype]: { ...a[ftype], [fam]: on } }));

  const { data: deps, refetch: refetchDeps } = useQuery({
    queryKey: ["dependencies"],
    queryFn: api.dependencies,
    retry: false,
  });
  const [depsBusy, setDepsBusy] = useState(false);

  const installDeps = async (keys?: string[]) => {
    setDepsBusy(true);
    try {
      const r = await api.installDependencies(keys);
      const failed = r.results.filter((x) => !x.ok);
      toast(failed.length ? "Install finished with " + failed.length + " failure(s)" : "Dependencies installed / updated");
      refetchDeps();
    } catch (e) {
      toast(String(e));
    } finally {
      setDepsBusy(false);
    }
  };

  const renderFields = (fields: CfgField[]) => (
    <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-x-6 gap-y-2.5 mt-2">
      {fields.map((f) => (
        <label key={f.k} className="flex items-center gap-2 text-[13px] text-zinc-300 cursor-pointer select-none">
          {f.type === "bool" ? (
            <>
              <input type="checkbox" checked={!!scriptCfg[f.k]} onChange={(e) => setCfg(f.k, e.target.checked)} />
              {f.label}
            </>
          ) : f.type === "select" ? (
            <div className="flex items-center gap-2 w-full">
              <span className="flex-1 min-w-0 truncate">{f.label}</span>
              <select
                className="input !w-32 !py-0.5 text-[11px] shrink-0"
                value={String(scriptCfg[f.k] ?? "")}
                onChange={(e) => setCfg(f.k, e.target.value)}
              >
                {f.options.map(([v, l]) => (
                  <option key={v} value={v}>{l}</option>
                ))}
              </select>
            </div>
          ) : f.type === "text" ? (
            <div className="flex items-center gap-2 w-full">
              <span className="flex-1 min-w-0 truncate">{f.label}</span>
              <input
                className="input !w-32 !py-0.5 text-[11px] shrink-0"
                value={String(scriptCfg[f.k] ?? "")}
                onChange={(e) => setCfg(f.k, e.target.value)}
              />
            </div>
          ) : f.type === "password" ? (
            <div className="flex items-center gap-2 w-full">
              <span className="flex-1 min-w-0 truncate">{f.label}</span>
              <div className="relative shrink-0">
                <input
                  className="input !w-32 !py-0.5 !pr-7 text-[11px]"
                  type={showPasswords.has(f.k) ? "text" : "password"}
                  value={String(scriptCfg[f.k] ?? "")}
                  onChange={(e) => setCfg(f.k, e.target.value)}
                />
                <button
                  type="button"
                  className="absolute right-1.5 top-1/2 -translate-y-1/2 text-zinc-500 hover:text-zinc-200"
                  title={showPasswords.has(f.k) ? "Hide password" : "Show password"}
                  onClick={() =>
                    setShowPasswords((prev) => {
                      const next = new Set(prev);
                      if (next.has(f.k)) next.delete(f.k);
                      else next.add(f.k);
                      return next;
                    })
                  }
                >
                  {showPasswords.has(f.k) ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                </button>
              </div>
            </div>
          ) : (
            <div className="flex items-center gap-2 w-full">
              <span className="flex-1 min-w-0 truncate">{f.label}</span>
              <input
                className="input !w-20 !py-0.5 text-[11px] shrink-0 text-right"
                type="number"
                min={f.min}
                max={f.max}
                step={f.step ?? 1}
                value={String(scriptCfg[f.k] ?? "")}
                onChange={(e) => setCfg(f.k, Number(e.target.value))}
              />
            </div>
          )}
        </label>
      ))}
    </div>
  );

  return (
    <div className="p-6">
      <div className="flex items-end justify-between gap-4 flex-wrap">
        <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
          <SettingsIcon className="h-6 w-6 text-accent" /> Settings
        </h1>
        <div className="relative w-full max-w-md">
          <input
            className="input !py-1.5 text-sm w-full"
            placeholder="Search settings… (e.g. cover, lyrics, catalog)"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          {searching && (
            <div className="absolute z-30 left-0 right-0 top-full mt-1 rounded-lg bg-zinc-950 border border-border shadow-2xl max-h-80 overflow-auto p-1.5">
              {searchHits.length === 0 && (
                <div className="px-2.5 py-2 text-xs text-zinc-500">No settings match “{q.trim()}”.</div>
              )}
              {searchHits.map((h, i) => (
                <button
                  key={i}
                  className="w-full text-left px-2.5 py-1.5 rounded-md hover:bg-white/10"
                  onClick={() => {
                    if (h.tab) setTab(h.tab);
                    setQ("");
                  }}
                >
                  <div className="text-xs text-zinc-200">{h.field}</div>
                  <div className="text-[10px] text-zinc-500">{h.group}</div>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="flex gap-6 mt-4">
        <nav className="w-44 shrink-0 space-y-0.5 sticky top-20 self-start max-h-[calc(100vh-120px)] overflow-auto pr-1">
          {NAV.map((n, i) => (
            <div key={n.id}>
              {n.section && (i === 0 || NAV[i - 1].section !== n.section) && (
                <div className="px-3 pt-3 pb-1 text-[10px] uppercase tracking-wider text-zinc-600 first:pt-0">{n.section}</div>
              )}
              <button
                onClick={() => setTab(n.id)}
                className={`w-full text-left px-3 py-1.5 rounded-md text-xs transition-colors ${
                  tab === n.id ? "bg-raise text-white border border-accent/40" : "text-zinc-400 hover:text-white hover:bg-panel border border-transparent"
                }`}
              >
                {n.label}
              </button>
            </div>
          ))}
        </nav>

        <div className="flex-1 min-w-0 space-y-5 pb-10">
          {tab === "general" && (
            <div className="bg-card rounded-lg border border-border p-4 space-y-3">
              <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Library</div>
              <label className="block">
                <span className="text-xs text-zinc-500 uppercase">Music folder</span>
                <div className="flex gap-2 mt-1">
                  <input className="input" value={musicFolder} onChange={(e) => setMusicFolder(e.target.value)} placeholder="F:\Music" />
                  <button className="btn-ghost" onClick={pickNative} title="Native folder picker (desktop)">
                    <FolderOpen className="h-4 w-4" />
                  </button>
                </div>
              </label>
              <label className="block">
                <span className="text-xs text-zinc-500 uppercase">Lyrics format</span>
                <select className="input mt-1" value={lyricsFormat} onChange={(e) => setLyricsFormat(e.target.value)}>
                  <option value="EMBEDDED">Embedded</option>
                  <option value="LRC">LRC sidecar</option>
                  <option value="BOTH">Both</option>
                </select>
              </label>
              <label className="block">
                <span className="text-xs text-zinc-500 uppercase">Worker limit (0 = auto)</span>
                <input className="input mt-1" type="number" min={0} value={workerLimit} onChange={(e) => setWorkerLimit(Number(e.target.value))} />
              </label>
              <div className="flex flex-wrap gap-x-6 gap-y-1.5">
                <label className="flex items-center gap-2 text-xs text-zinc-300 cursor-pointer select-none">
                  <input type="checkbox" checked={!!scriptCfg.auto_advance} onChange={(e) => setCfg("auto_advance", e.target.checked)} />
                  Auto-advance between Run All scripts
                </label>
                <label className="flex items-center gap-2 text-xs text-zinc-300 cursor-pointer select-none">
                  <input type="checkbox" checked={!!scriptCfg.show_sidecar_files} onChange={(e) => setCfg("show_sidecar_files", e.target.checked)} />
                  Show sidecar files (cue/log/lrc/accurip) in library
                </label>
              </div>
              <details className="bg-zinc-950/40 rounded-lg border border-border px-3 py-2">
                <summary className="text-xs font-medium cursor-pointer text-zinc-400 select-none">
                  Run All — scripts in order ({runAll.length} enabled)
                </summary>
                <div className="grid grid-cols-2 md:grid-cols-3 gap-x-4 gap-y-1.5 mt-2">
                  {RUN_ALL_SCRIPTS.map((s) => (
                    <label key={s.id} className="flex items-center gap-2 text-xs text-zinc-300 cursor-pointer select-none">
                      <input
                        type="checkbox"
                        checked={runAll.includes(s.id)}
                        onChange={(e) =>
                          setRunAll((ids) => (e.target.checked ? [...ids, s.id] : ids.filter((i) => i !== s.id)))
                        }
                      />
                      <span className="text-zinc-600 w-4">{s.id}</span>
                      {s.label}
                    </label>
                  ))}
                </div>
                <div className="text-[10px] text-zinc-600 mt-1">The Run All button executes them in this order.</div>
              </details>
              <details className="bg-zinc-950/40 rounded-lg border border-border px-3 py-2">
                <summary className="text-xs font-medium cursor-pointer text-zinc-400 select-none">
                  Force options — re-run scripts even when up to date
                </summary>
                <label className="flex items-center gap-2 text-xs text-zinc-300 cursor-pointer select-none mt-2 font-medium">
                  <input
                    type="checkbox"
                    checked={FORCE_KEYS.every((f) => !!scriptCfg[f.k])}
                    onChange={(e) =>
                      setScriptCfg((c) => {
                        const next = { ...c };
                        for (const f of FORCE_KEYS) next[f.k] = e.target.checked;
                        return next;
                      })
                    }
                  />
                  Force every script (ignore all &ldquo;already done&rdquo; skips)
                </label>
                <div className="grid grid-cols-2 md:grid-cols-3 gap-x-4 gap-y-1.5 mt-1.5">
                  {FORCE_KEYS.map((f) => (
                    <label key={f.k} className="flex items-center gap-2 text-xs text-zinc-300 cursor-pointer select-none">
                      <input
                        type="checkbox"
                        checked={!!scriptCfg[f.k]}
                        onChange={(e) => setCfg(f.k, e.target.checked)}
                      />
                      {f.label}
                    </label>
                  ))}
                </div>
                <div className="text-[10px] text-zinc-600 mt-1">
                  Forced scripts redo work even when output already looks up to date. Each toggle also appears in its script tab; the Run All button has its own one-shot Force switch.
                </div>
              </details>
            </div>
          )}

          {tab === "appearance" && (
            <div className="bg-card rounded-lg border border-border p-4 space-y-3">
              <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Appearance</div>
              <div>
                <span className="text-xs text-zinc-500 uppercase">Accent color</span>
                <div className="flex gap-2 mt-1.5">
                  {ACCENT_OPTIONS.map((a) => (
                    <button
                      key={a.id}
                      title={a.name}
                      onClick={() => pickAccent(a.id)}
                      className="h-8 w-8 rounded-lg border-2 flex items-center justify-center transition-transform hover:scale-110"
                      style={{
                        backgroundColor: a.color,
                        borderColor: accent === a.id ? "#fff" : "#3f3f46",
                      }}
                    >
                      {accent === a.id && <Check className="h-4 w-4 text-black" />}
                    </button>
                  ))}
                </div>
              </div>
              <label className="block">
                <span className="text-xs text-zinc-500 uppercase">Default library view</span>
                <select className="input mt-1" value={defaultView} onChange={(e) => pickDefaultView(e.target.value)}>
                  <option value="grid">Grid (cover browse)</option>
                  <option value="compact">Compact (grading status)</option>
                  <option value="albums">Albums table</option>
                  <option value="artists">Artists</option>
                  <option value="tracks">Tracks</option>
                </select>
              </label>

              <div className="pt-2 border-t border-border">
                <span className="text-xs text-zinc-500 uppercase">Player &amp; lyrics display</span>
                <div className="space-y-2.5 mt-2">
                  <label className="flex items-center justify-between gap-3 text-xs text-zinc-300">
                    <span>Fullscreen lyrics size</span>
                    <select
                      className="input !w-28 !py-1"
                      value={localStorage.getItem("mlo.np.size") ?? "md"}
                      onChange={(e) => localStorage.setItem("mlo.np.size", e.target.value)}
                    >
                      <option value="sm">Small</option>
                      <option value="md">Medium</option>
                      <option value="lg">Large</option>
                    </select>
                  </label>
                  <label className="flex items-center justify-between gap-3 text-xs text-zinc-300 cursor-pointer">
                    <span>Karaoke word highlight (vs. whole-line)</span>
                    <input
                      type="checkbox"
                      defaultChecked={localStorage.getItem("mlo.np.karaoke") === "1"}
                      onChange={(e) => localStorage.setItem("mlo.np.karaoke", e.target.checked ? "1" : "0")}
                    />
                  </label>
                  <label className="flex items-center justify-between gap-3 text-xs text-zinc-300 cursor-pointer">
                    <span>Animated background in fullscreen player</span>
                    <input
                      type="checkbox"
                      defaultChecked={localStorage.getItem("mlo.np.orbs") !== "0"}
                      onChange={(e) => localStorage.setItem("mlo.np.orbs", e.target.checked ? "1" : "0")}
                    />
                  </label>
                  <label className="flex items-center justify-between gap-3 text-xs text-zinc-300">
                    <span>Default lyrics save target</span>
                    <select
                      className="input !w-40 !py-1"
                      value={localStorage.getItem("mlo.lyricsSaveTarget") ?? "embedded"}
                      onChange={(e) => localStorage.setItem("mlo.lyricsSaveTarget", e.target.value)}
                    >
                      <option value="embedded">Embedded tag</option>
                      <option value="sidecar">.lrc sidecar</option>
                      <option value="both">Tag + .lrc</option>
                    </select>
                  </label>
                </div>
                <div className="text-[10px] text-zinc-600 mt-2">Stored per browser, like the accent color.</div>
              </div>
            </div>
          )}

          {tab === "naming" && (
            <div className="bg-card rounded-lg border border-border p-4 space-y-3">
              <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">File naming (Picard-style script)</div>
              <textarea
                className="input font-mono text-xs min-h-[110px]"
                value={namingScript}
                onChange={(e) => setNamingScript(e.target.value)}
                spellCheck={false}
              />
              <div className="text-[11px] text-zinc-600 leading-relaxed">
                Variables: <code>%albumartist% %musicbrainz_albumartistid% %releasetype% %originaldate% %date% %album% %releasecountry% %media% %catalognumber% %discnumber% %tracknumber% %title%</code> ·
                Functions: <code>$if(a,b,c) $left(s,n) $num(s,n) $lower $upper $replace $ne $right</code> · <code>/</code> creates folders.
                Applied from the album page or the bulk selection toolbar.
              </div>
              <div className="flex items-center gap-4 flex-wrap">
                <label className="flex items-center gap-2 text-xs text-zinc-400 cursor-pointer select-none">
                  <input type="checkbox" checked={shortFolderNames} onChange={(e) => setShortFolderNames(e.target.checked)} />
                  Shorter folder names (truncate MusicBrainz IDs to 8 chars)
                </label>
                <button className="btn-ghost !py-1 text-xs" onClick={() => setNamingScript(DEFAULT_NAMING_SCRIPT)}>
                  <RotateCcw className="h-3.5 w-3.5" /> Reset to default
                </button>
                <button className="btn-ghost !py-1 text-xs" onClick={runPreview} disabled={previewing}>
                  Preview
                </button>
              </div>
              {previewPath && (
                <div className="rounded-md border border-border bg-panel px-3 py-2 font-mono text-xs text-accent-soft break-all">
                  <span className="text-zinc-500">sample album → </span>
                  {previewPath}
                </div>
              )}
              {previewError && (
                <div className="rounded-md border border-red-900 bg-red-950/40 px-3 py-2 font-mono text-xs text-red-300 break-all">
                  {previewError}
                </div>
              )}
            </div>
          )}

          {tab === "deps" && (
            <div className="bg-card rounded-lg border border-border p-4 space-y-3">
              <div className="flex items-center justify-between flex-wrap gap-2">
                <div>
                  <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Dependencies</div>
                  <div className="text-[11px] text-zinc-600 mt-0.5">
                    Tools the scripts need. Installed from <code className="font-mono">{deps?.deps_dir ?? ".dependencies"}</code> or found on PATH.
                  </div>
                </div>
                <div className="flex gap-2">
                  <button className="btn-ghost !py-1 text-xs" onClick={() => refetchDeps()} disabled={depsBusy}>
                    Refresh
                  </button>
                  <button
                    className="btn-ghost !py-1 text-xs"
                    onClick={() => installDeps(deps?.tools.filter((t) => t.state === "missing").map((t) => t.key))}
                    disabled={depsBusy}
                  >
                    Install missing
                  </button>
                  <button className="btn-primary !py-1 text-xs" onClick={() => installDeps()} disabled={depsBusy}>
                    {depsBusy ? "Installing…" : "Install / update all"}
                  </button>
                </div>
              </div>
              <div className="rounded-md border border-border overflow-hidden">
                <table className="w-full text-sm">
                  <thead className="bg-panel/60">
                    <tr>
                      <th className="th">Tool</th>
                      <th className="th">Status</th>
                      <th className="th">Installed</th>
                      <th className="th">Latest</th>
                      <th className="th">Path</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(deps?.tools ?? []).map((t) => (
                      <tr key={t.key} className="table-row cursor-default">
                        <td className="td font-medium">{t.name}</td>
                        <td className="td">
                          {t.state === "ok" && <span className="chip bg-emerald-900/50 text-emerald-300 border border-emerald-800">ready</span>}
                          {t.state === "update" && <span className="chip bg-amber-900/50 text-amber-300 border border-amber-900">update</span>}
                          {t.state === "missing" && <span className="chip bg-red-900/50 text-red-300 border border-red-900">missing</span>}
                        </td>
                        <td className="td text-zinc-500">{t.installed_version ?? t.detected_version ?? "—"}</td>
                        <td className="td text-zinc-500">{t.latest_version ?? "—"}</td>
                        <td className="td text-zinc-500 truncate max-w-[280px]">{t.path ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="text-[10px] text-zinc-600">
                Install downloads the pinned release from GitHub into the dependencies folder; PATH-installed tools (scoop etc.) are shown as ready.
              </div>
            </div>
          )}

          {GROUP_BY_TAB[tab] && (
            <div className="bg-card rounded-lg border border-border p-4 space-y-3">
              <div className="text-xs font-bold text-zinc-300">{GROUP_BY_TAB[tab].title}</div>
              {GROUP_BY_TAB[tab].blurb && <div className="text-[10px] text-zinc-600">{GROUP_BY_TAB[tab].blurb}</div>}
              {!!GROUP_BY_TAB[tab].presets?.length && (
                <div className="flex items-center gap-1.5 flex-wrap">
                  <span className="text-[10px] text-zinc-600">Presets:</span>
                  {GROUP_BY_TAB[tab].presets!.map((pr) => (
                    <button
                      key={pr.name}
                      className="chip text-[10px] border border-white/15 bg-white/5 text-zinc-400 hover:text-white"
                      title={`Fill base URL + model for ${pr.name} (your API key is kept)`}
                      onClick={() => Object.entries(pr.values).forEach(([k, v]) => setCfg(k, v))}
                    >
                      {pr.name}
                    </button>
                  ))}
                </div>
              )}
              {renderFields(GROUP_BY_TAB[tab].fields)}
              {tab === "beets" && (
                <div className="pt-2 border-t border-border space-y-2">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-xs text-zinc-400">
                      {beetsStatus?.installed
                        ? `beets v${beetsStatus.version} installed (vendored in .dependencies)`
                        : "beets is not installed yet"}
                    </span>
                    <button className="btn-primary !py-1 text-xs" onClick={installBeets} disabled={beetsBusy}>
                      {beetsBusy ? "Installing…" : beetsStatus?.installed ? "Reinstall" : "Install beets"}
                    </button>
                  </div>
                  {beetsStatus?.installed && (
                    <details className="text-[11px]">
                      <summary className="cursor-pointer text-zinc-500">generated beets config (server/data/beets-config.yaml)</summary>
                      <pre className="mt-1 p-2 bg-zinc-950 border border-border rounded overflow-auto max-h-64 text-[10px] font-mono text-zinc-400">{beetsStatus.config}</pre>
                    </details>
                  )}
                  <div className="text-[10px] text-zinc-600">
                    Run "Tag with beets" from an album page to import it: beets matches against MusicBrainz, writes tags
                    (translations / work &amp; movement / release-type caps per the settings above) and organizes files with
                    your naming script.
                  </div>
                </div>
              )}
              {tab === "tagwrites" && (
                <>
                  <div className="pt-2 border-t border-border">
                    <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Per-filetype tag writes</div>
                    <div className="text-[10px] text-zinc-600 mt-0.5 mb-1.5">
                      Which tag families each audio container receives (ANDed with the global switches above).
                    </div>
                    <table className="w-full text-xs">
                      <thead>
                        <tr>
                          <th className="text-left text-zinc-500 font-medium py-1">Type</th>
                          {TAG_FAMILIES.map((fam) => (
                            <th key={fam} className="text-zinc-500 font-medium py-1">{fam}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {AUDIO_TYPES.map((t) => (
                          <tr key={t}>
                            <td className="py-0.5 text-zinc-300">{t}</td>
                            {TAG_FAMILIES.map((fam) => (
                              <td key={fam} className="py-0.5">
                                <input
                                  type="checkbox"
                                  className="accent-[var(--accent)]"
                                  checked={!!audioTagWrites[t]?.[fam]}
                                  onChange={(e) => toggleTagWrite(t, fam, e.target.checked)}
                                />
                              </td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <div className="pt-2 border-t border-border">
                    <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Encoder marker tags</div>
                    <div className="text-[10px] text-zinc-600 mt-0.5 mb-1.5">
                      Written to files when re-encoded. QUALITY/VERSION gate re-optimization; PROGRAM is informational.
                    </div>
                    <table className="w-full text-xs">
                      <thead>
                        <tr>
                          <th className="text-left text-zinc-500 font-medium py-1">Format</th>
                          {ENCODER_FIELDS.map((f) => (
                            <th key={f} className="text-zinc-500 font-medium py-1">{f}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {ENCODER_FORMATS.map((fmt) => (
                          <tr key={fmt}>
                            <td className="py-0.5 text-zinc-300">{fmt}</td>
                            {ENCODER_FIELDS.map((f) => (
                              <td key={f} className="py-0.5">
                                <input
                                  type="checkbox"
                                  className="accent-[var(--accent)]"
                                  checked={!!encoderTags[fmt]?.[f]}
                                  onChange={(e) => toggleEncoder(fmt, f, e.target.checked)}
                                />
                              </td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </div>
          )}

          {tab === "grading" && (
            <details className="bg-card rounded-lg border border-border p-4" open>
              <summary className="text-sm font-semibold cursor-pointer">Individual grading checks ({GRADE_CHECK_KEYS.length})</summary>
              <div className="mt-2">{renderFields(GRADE_CHECK_KEYS)}</div>
            </details>
          )}

          <div className="flex items-center gap-2">
            <ConfirmButton
              onConfirm={resetAllDefaults}
              confirmLabel="Reset all"
              disabled={!configDefaults}
              title="Restore factory defaults for every setting (music folder, first-run flag and AI connection are kept)"
            >
              <RotateCcw className="h-4 w-4" /> Reset to defaults
            </ConfirmButton>
            <button className="btn-primary" onClick={save}>
              <Save className="h-4 w-4" /> Save all settings
            </button>
          </div>

          <details className="bg-card rounded-lg border border-border p-4">
            <summary className="text-sm font-semibold cursor-pointer">Credits & open-source licenses</summary>
            <p className="text-[11px] text-zinc-500 mt-2">
              la musica is MIT-licensed (see LICENSE) and stands on the shoulders
              of these projects — their licenses require this credit, and they
              deserve it. The full legal text lives in THIRD-PARTY-NOTICES.md.
            </p>
            <div className="mt-2 space-y-3">
              {((credits as { groups?: { title: string; items: { name: string; license: string; url: string }[] }[] } | undefined)?.groups ?? []).map((g) => (
                <div key={g.title}>
                  <div className="text-[10px] uppercase tracking-widest text-zinc-500 mb-1">{g.title}</div>
                  <div className="grid gap-x-4 gap-y-0.5" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))" }}>
                    {g.items.map((c: { name: string; license: string; url: string }) => (
                      <a
                        key={c.name}
                        href={c.url}
                        target="_blank"
                        rel="noreferrer"
                        className="text-[11px] text-zinc-400 hover:text-accent-soft truncate"
                        title={`${c.name} — ${c.license}`}
                      >
                        <span className="text-zinc-200">{c.name}</span>
                        <span className="text-zinc-600"> · {c.license}</span>
                      </a>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </details>

          <details className="bg-card rounded-lg border border-border p-4">
            <summary className="text-sm font-semibold cursor-pointer">Raw config (advanced)</summary>
            <textarea
              className="input font-mono text-[11px] min-h-[220px] mt-2"
              value={rawConfig}
              onChange={(e) => setRawConfig(e.target.value)}
              spellCheck={false}
            />
            <div className="flex items-center gap-2 mt-2">
              <button className="btn-ghost !py-1 text-xs" onClick={applyRaw}>
                Apply to form
              </button>
              <span className="text-[10px] text-zinc-600">
                Edits the form fields (then click Save all settings). Invalid JSON is rejected.
              </span>
            </div>
          </details>
        </div>
      </div>
    </div>
  );
}