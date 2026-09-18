import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Save, RotateCcw, LayoutGrid, Settings as SettingsIcon, Check, Eye, EyeOff, ChevronDown, ChevronUp, Wand2, X } from "lucide-react";
import { api } from "../api";
import ConfirmButton from "../components/ConfirmButton";
import SourcesPanel from "../components/SourcesPanel";
import AiTestButton from "../components/AiTestButton";
import PageHeader from "../components/PageHeader";
import { toast } from "../store";
import { applyAccent } from "../App";
import { DEFAULT_RUN_ALL, SCRIPT_LABEL, isScriptId } from "../lib/scripts";

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
const TAG_FAMILIES = ["AUDIT", "LOG_GRADE", "REPLAYGAIN", "DYNAMIC_RANGE", "MEDIA_SOURCE", "INSTRUMENTAL", "ADVISORY", "LYRICS", "BPM", "INITIALKEY", "ENERGY"] as const;

/** Wave-2 keys an older config file predates. The form reads them through
 *  this map so a fresh install shows the real default (the backend fills in
 *  the same values when it loads the file) instead of an empty field. */
const CFG_DEFAULTS: Record<string, unknown> = {
  mb_genre_count: 3,
  genre_sources: ["rateyourmusic", "listenbrainz", "musicbrainz", "itunes", "wikidata", "lastfm", "discogs", "theaudiodb", "deezer"],
  advisory_auto_fetch: true,
  metadata_auto_fetch: true,
  metadata_review: false,
  soulseek_download_slots: 3,
  soulseek_upload_slots: 2,
  soulseek_upload_limit_kib: 0,
  soulseek_download_limit_kib: 0,
  soulseek_web_https: false,
  youtube_enabled: true,
  youtube_max_height: 0,
  auto_import_avoid_promo: true,
  auto_import_require_country: true,
  auto_import_medium_order: ["CD", "Digital Media", "Vinyl", "Cassette", "Other"],
  cover_auto_fetch: true,
  cover_review: true,
};

type ProviderOption = { id: string; label: string; notes?: string; rank?: number };

/** Genre sources that never answer for a single track — the chain files their
 *  answer under every track and marks it `level: "album"`/`"artist"`
 *  (server/integrations._genre_source_answers). Everything absent here is
 *  asked per track first, with its own album/artist answer as the fallback
 *  tier, so "per track" is what the picker says for them. */
const GENRE_LEVEL: Record<string, string> = {
  rateyourmusic: "album, per track when its release page states one",
  discogs: "album only",
  bandcamp: "album only",
  deezer: "album only",
  spotify: "artist only",
};

/** Ordered provider picker for a `list` config key: the listed providers are
 *  tried in order (first one with an answer wins). An empty list means the
 *  built-in order, shown as a placeholder. */
function ProviderOrder({
  options, builtin, order, onChange, help,
}: {
  options: ProviderOption[];
  builtin: string[];
  order: string[];
  onChange: (next: string[]) => void;
  help?: string;
}) {
  const move = (i: number, d: number) => {
    const next = order.slice();
    [next[i], next[i + d]] = [next[i + d], next[i]];
    onChange(next);
  };
  return (
    <div className="space-y-1.5">
      {order.length === 0 ? (
        <div className="rounded-md border border-dashed border-border px-2 py-1.5 text-[11px] text-zinc-500">
          <span className="text-zinc-400">Built-in order: </span>
          {builtin.map((id) => options.find((o) => o.id === id)?.label ?? id).join(" → ")}
        </div>
      ) : (
        <div className="rounded-md border border-border divide-y divide-border/60">
          {order.map((id, i) => {
            const opt = options.find((o) => o.id === id);
            return (
              <div key={id} className="flex items-start gap-2 px-2 py-1">
                <span className="w-4 pt-0.5 text-[10px] text-zinc-600">{i + 1}</span>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-1.5">
                    <span className="text-[12px] text-zinc-200 truncate">{opt?.label ?? id}</span>
                    {opt?.rank !== undefined && (
                      <span
                        className="chip border border-accent/25 bg-accent/10 text-accent-soft shrink-0"
                        title="Rank in the BUILT-IN chain — this list replaces the order, it does not change the rank"
                      >
                        #{opt.rank} preferred
                      </span>
                    )}
                  </div>
                  {opt?.notes && <div className="text-[10px] text-zinc-600">{opt.notes}</div>}
                </div>
                <button
                  type="button"
                  className="text-zinc-500 hover:text-white disabled:opacity-30"
                  title="Move up"
                  disabled={i === 0}
                  onClick={() => move(i, -1)}
                >
                  <ChevronUp className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  className="text-zinc-500 hover:text-white disabled:opacity-30"
                  title="Move down"
                  disabled={i === order.length - 1}
                  onClick={() => move(i, 1)}
                >
                  <ChevronDown className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  className="text-zinc-500 hover:text-red-300"
                  title="Remove from the list (unlisted providers are not used)"
                  onClick={() => onChange(order.filter((x) => x !== id))}
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>
            );
          })}
        </div>
      )}
      {options.some((o) => !order.includes(o.id)) && (
        <div className="flex flex-wrap gap-1.5">
          {options.filter((o) => !order.includes(o.id)).map((o) => (
            <button
              key={o.id}
              type="button"
              className="chip border border-white/15 bg-white/5 text-[10px] text-zinc-400 hover:text-white"
              title={o.notes}
              onClick={() => onChange([...order, o.id])}
            >
              + {o.label}
            </button>
          ))}
        </div>
      )}
      {order.length > 0 && (
        <button
          type="button"
          className="text-[10px] text-zinc-500 hover:text-white underline"
          onClick={() => onChange([])}
        >
          Reset to the built-in order
        </button>
      )}
      {help && <div className="text-[10px] text-zinc-600">{help}</div>}
    </div>
  );
}

/** Cover-art defaults (Settings → Images): the region and source list a cover
 *  search starts from. These are the SAVED values (`cover_country`,
 *  `cover_sources`) that a search with no overrides uses — the cover finder's
 *  own region/source pickers are per-search and change nothing here until its
 *  "Save as default" is pressed. */
function CoverDefaults() {
  const qc = useQueryClient();
  const { data: cat } = useQuery({ queryKey: ["coverSources"], queryFn: api.coverSources });
  const [srcSel, setSrcSel] = useState<string[] | null>(null);
  const [country, setCountry] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Seeded from the EFFECTIVE values (`default_sources`/`default_country` are
  // what a search with no overrides uses — the saved list, or the built-in one
  // while nothing is saved), and re-seeded whenever they change so a save made
  // in the finder shows up here instead of leaving a stale draft behind.
  const savedKey = `${cat?.default_country ?? ""}|${(cat?.default_sources ?? []).join(",")}`;
  useEffect(() => {
    if (!cat) return;
    setSrcSel(cat.default_sources);
    setCountry(cat.default_country);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [savedKey]);

  if (!cat || srcSel === null || country === null) {
    return <div className="text-[11px] text-zinc-600">Loading the cover source catalogue…</div>;
  }
  const cap = cat.active_source_limit;
  const dirty = srcSel.join(",") !== cat.default_sources.join(",") || country !== cat.default_country;

  const save = async () => {
    setBusy(true);
    try {
      await api.saveConfig({ cover_sources: srcSel, cover_country: country });
      qc.invalidateQueries({ queryKey: ["coverSources"] });
      qc.invalidateQueries({ queryKey: ["config"] });
      toast.success(`Saved the default cover search: ${srcSel.length} source(s), ${country.toUpperCase()}`);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-2">
      <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Cover art</div>
      <div className="text-[11px] text-zinc-600">
        The defaults a cover search starts from. The finder's own region and source pickers are
        <span className="text-zinc-400"> per search</span> — they change nothing here until you press
        its "Save as default", which writes exactly these two values.
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[11px] font-semibold text-zinc-400">Region</span>
        <select
          className="input !w-auto !py-1 text-xs"
          value={country}
          onChange={(e) => setCountry(e.target.value)}
        >
          {cat.countries.map((c) => (
            <option key={c} value={c}>{c.toUpperCase()}</option>
          ))}
        </select>
        <span className="text-[10px] text-zinc-600">
          The storefront the sources are asked about — it decides which releases and artwork exist for a region.
        </span>
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[11px] font-semibold text-zinc-400">Sources</span>
        <span className="text-[10px] text-zinc-500">
          {srcSel.length}/{cap} — at most {cap} are used per search
        </span>
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1">
        {cat.sources.map((s) => {
          const on = srcSel.includes(s.id);
          const full = !on && srcSel.length >= cap;
          return (
            <label
              key={s.id}
              className={`flex items-center gap-1.5 text-[11px] select-none ${
                full ? "text-zinc-600" : "text-zinc-300 cursor-pointer"
              }`}
              title={full ? `Already at the ${cap}-source limit` : s.name}
            >
              <input
                type="checkbox"
                checked={on}
                disabled={full}
                onChange={() =>
                  setSrcSel(srcSel.includes(s.id) ? srcSel.filter((x) => x !== s.id) : [...srcSel, s.id])
                }
              />
              {s.color && <span className="h-2 w-2 rounded-full shrink-0" style={{ background: s.color }} />}
              {s.name}
              {!s.enabled && <span className="text-[9px] text-amber-400/80">off</span>}
            </label>
          );
        })}
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        <button className="btn-primary !py-1 text-xs" onClick={save} disabled={busy || !dirty}>
          <Check className="h-3 w-3" /> Save as default
        </button>
        {dirty && (
          <button
            className="btn-ghost !py-1 text-xs"
            disabled={busy}
            onClick={() => {
              setSrcSel(cat.default_sources);
              setCountry(cat.default_country);
            }}
          >
            <RotateCcw className="h-3 w-3" /> Discard changes
          </button>
        )}
        {!dirty && <span className="text-[10px] text-zinc-600">Saved — a search with no overrides uses this.</span>}
      </div>
    </div>
  );
}

export default function SettingsPage() {
  const navigate = useNavigate();
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
  // Filled from the config the server normalizes (naming_script is never
  // empty there) — the shipped default is the server's, never a copy here.
  const [namingScript, setNamingScript] = useState("");
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

  // Provider catalogues behind the order editors (Discovery / Lyrics lists).
  const { data: discoveryCat } = useQuery({ queryKey: ["discoverySources"], queryFn: api.discoverySources });
  const { data: lyricsCat } = useQuery({ queryKey: ["lyricsProviders"], queryFn: api.lyricsProviders });

  // The genre picker offers EXACTLY the chain's own genre sources (the genre
  // rows of /api/sources/health are integrations.GENRE_SOURCES), so a chain
  // change shows up here on its own and no stale id can be ticked. Same cache
  // entry the Sources panel reads, so this costs no extra request.
  const { data: health } = useQuery({
    queryKey: ["sourcesHealth"],
    queryFn: () => api.sourcesHealth(false),
    staleTime: 30000,
  });
  const genreOptions: [string, string][] = (health?.sources ?? [])
    .filter((s) => s.kind === "genre")
    .map((s): [string, string] => [s.id, `${s.label} — ${GENRE_LEVEL[s.id] ?? "per track"}`]);

  // ---- script options (persisted to config; /api/run uses them as defaults) ----
  type CfgField =
    | { k: string; label: string; type: "bool"; help?: string }
    | { k: string; label: string; type: "number"; min?: number; max?: number; step?: number; help?: string }
    | { k: string; label: string; type: "select"; options: [string, string][] }
    | { k: string; label: string; type: "text"; help?: string }
    | { k: string; label: string; type: "password"; help?: string }
    /** Ordered provider preference list; an empty list means the built-in order. */
    | { k: string; label: string; type: "list"; catalog: "discovery" | "lyrics"; help?: string }
    /** Unordered set of values (a string list in config) shown as checkboxes. */
    | { k: string; label: string; type: "multi"; options: [string, string][]; help?: string }
    /** Comma-separated list in one text input; kept in config as a string list. */
    | { k: string; label: string; type: "csv"; help?: string };
  interface CfgGroup {
    title: string;
    blurb?: string;
    fields: CfgField[];
  }
  const CFG_GROUPS: CfgGroup[] = [
    {
      title: "AI lyric transforms (script 17)",
      blurb:
        "The one optional model in the app. Script 17 romanizes non-Latin lyrics and translates them into the languages below, writing TRANSLITERATION-*/TRANSLATION-* tags (and .romaji.lrc / .<lang>.lrc sidecars for LRC/BOTH lyric formats). Any OpenAI-compatible /chat/completions endpoint works — OpenAI, OpenRouter, LM Studio, llama.cpp, or Google Gemini's OpenAI-compatible endpoint (paste the bare generativelanguage.googleapis.com host and it is routed). Nothing else in the app depends on it: with the URL or model empty, the script logs one line and skips. Reasoning effort is sent as `reasoning_effort` on every call — a provider that rejects the field gets one plain retry.",
      fields: [
        { k: "ai_base_url", label: "Base URL", type: "text", help: "e.g. https://api.openai.com/v1, http://localhost:1234/v1, or generativelanguage.googleapis.com" },
        { k: "ai_api_key", label: "API key", type: "password", help: "Sent as a Bearer token. Local servers (LM Studio, llama.cpp) usually ignore it — leave it empty there." },
        { k: "ai_model", label: "Model", type: "text", help: "The model id the endpoint expects, e.g. gpt-4o-mini or gemini-2.5-flash." },
        {
          k: "ai_effort", label: "Reasoning effort", type: "select",
          options: [["high", "High — best quality (default)"], ["medium", "Medium"], ["low", "Low"], ["minimal", "Minimal — no thinking, fastest"]],
        },
        { k: "lyrics_translation_langs", label: "Translation languages", type: "text", help: "Comma separated, e.g. en,de. The first is the reader's language (it decides when romanization is worth generating) and names the TRANSLATION tag; each language also gets its own .<lang>.lrc sidecar." },
        { k: "lyrics_xlit_enabled", label: "Transliterate non-Latin lyrics", type: "bool" },
        { k: "lyrics_translate_enabled", label: "Translate lyrics", type: "bool" },
        { k: "lyrics_xlit_sidecars", label: "Write .romaji.lrc / .<lang>.lrc sidecars (LRC formats only)", type: "bool" },
        { k: "force_xlit", label: "Force: re-transform tracks that already have one", type: "bool" },
      ],
    },
    {
      title: "FLACs & lossless sources (script 3)",
      blurb: "Re-encodes FLACs at the target level and converts every other lossless source (WAV/AIFF/APE/WV/SHN/TTA, ALAC in MP4) to the target codec below, losslessly — the same conversion runs on Soulseek imports. FLAC is what the rest of the pipeline assumes; choosing ALAC re-containers FLACs into .m4a as well.",
      fields: [
        { k: "optimize_convert_lossless", label: "Convert lossless sources to the target codec", type: "bool" },
        { k: "lossless_target_codec", label: "Target lossless codec", type: "select", options: [["flac", "FLAC (.flac)"], ["alac", "ALAC (.m4a)"]] },
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
        {
          k: "cover_auto_fetch", label: "Fetch missing covers during import", type: "bool",
          help: "During import, an album with no image gets cover candidates looked up (Cover Art Archive first, then Deezer/Apple). Off leaves the finder and the Grading screen's Missing cover verdict to you.",
        },
        {
          k: "cover_review", label: "…and let me pick which one (off = take the best automatically)", type: "bool",
          help: "On: the candidates are shown on the album page for you to choose, and nothing is written until you do. Off: the best candidate is downloaded and normalised on the spot, as before. Ignored while 'Fetch missing covers during import' is off.",
        },
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
        // The cover FINDER defaults (region + source list) live in the
        // "Cover art" panel below, which reads the real region list and the
        // per-search source cap from /api/cover/sources.
        { k: "force_reencode_images", label: "Force re-process", type: "bool" },
      ],
    },
    {
      title: "Lyrics & CUEs (scripts 1, 2 & 18)",
      fields: [
        { k: "optimize_lrc", label: "Optimize .lrc sidecars", type: "bool" },
        { k: "optimize_embedded_lyrics", label: "Optimize embedded lyrics", type: "bool" },
        {
          k: "lyrics_sources", label: "Lyrics providers (order)", type: "list", catalog: "lyrics",
          help: "Providers are tried top to bottom when lyrics are fetched from an album, artist or track page. Providers left out of the list are never used.",
        },
        {
          k: "lyrics_allow_plain", label: "Accept plain (unsynced) lyrics", type: "bool",
          help: "Off by default: every provider in the chain answers with timestamps, and an answer without them is thrown away as if it had none. Turn this on only to let untimed text (LRCLIB's plain records) through when nothing synced exists.",
        },
        {
          k: "lrclib_auto_publish", label: "Auto-publish missing lyrics to LRCLIB", type: "bool",
          help: "Script 18 (and every import chain that includes it) submits this library's own lyrics to LRCLIB for tracks the database does not have yet — artist, title, album and duration decide that, and a track LRCLIB already answers for is never touched. Outward-facing: with it off, nothing is ever submitted automatically (the manual 'Publish to LRCLIB' button on the lyrics editor still works).",
        },
        {
          k: "force_publish", label: "Force: re-submit lyrics LRCLIB already has", type: "bool",
          help: "One-shot per run — re-publishes even when LRCLIB answers for the track, e.g. when this library's text is the better one. LRCLIB may still reject the duplicate.",
        },
        {
          k: "lyrics_youtube_captions", label: "Use YouTube captions (yt-dlp)", type: "bool",
          help: "Time-synced captions, but only for tracks that carry a YouTube id — the id the video download records — so this never searches YouTube for a track. Automatic captions are used when a video has no typed subtitles and can mishear; needs yt-dlp under Dependencies, otherwise the provider is skipped.",
        },
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
        {
          k: "replaygain_mode", label: "Gain mode", type: "select",
          options: [["track", "Track gain"], ["album", "Album gain"], ["off", "Off — no gain applied"]],
        },
        { k: "replaygain_preamp_db", label: "Preamp (dB)", type: "number", min: -24, max: 24, step: 0.5 },
        {
          k: "replaygain_analyze_missing", label: "Measure tracks without ReplayGain tags instead of playing them at unity",
          type: "bool",
        },
        { k: "replaygain_clip_protection", label: "Clip protection", type: "bool" },
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
        { k: "force_audit", label: "Force re-audit", type: "bool" },
      ],
    },
    {
      title: "AutoTag (script 8)",
      fields: [
        { k: "auto_advisory", label: "Set advisory automatically", type: "bool" },
        { k: "advisory_auto_fetch", label: "Fetch the advisory rating automatically (import + advisory fetch)", type: "bool" },
        { k: "mood_enabled", label: "Write mood tags", type: "bool" },
        { k: "genre_autofill", label: "Fill in missing genres", type: "bool" },
        {
          k: "mood_source", label: "Mood source", type: "select",
          options: [
            ["audio", "Audio analysis"],
            ["provider", "Provider metadata"],
            ["hybrid", "Hybrid — audio, trusting the genre when the audio is ambiguous"],
          ],
        },
        { k: "auto_instrumental", label: "Set INSTRUMENTAL automatically", type: "bool" },
        { k: "auto_zero_advisory_for_instrumental", label: "Zero advisory on instrumentals", type: "bool" },
        { k: "fix_instrumental_from_lyrics", label: "Fix INSTRUMENTAL from lyrics", type: "bool" },
        { k: "force_auto_tag", label: "Force re-tag", type: "bool" },
        { k: "force_mood", label: "Force mood & energy re-analysis (script 16)", type: "bool" },
      ],
    },
    {
      title: "Release tracklist (script 15)",
      fields: [
        { k: "force_tracklist", label: "Force rewrite of an existing .mlo_expected.json", type: "bool" },
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
      blurb: "Lossless remux: any video container → MKV with the video copied bit-exact and every audio stream re-encoded to FLAC (lossless, level below). Captions/subtitles are always kept and verified — never removed. If the muxer refuses the video codec, H.264 is a last-resort fallback. The original (e.g. the .VOB) is removed after a verified remux.",
      fields: [
        { k: "youtube_enabled", label: "Fetch missing music videos from YouTube", type: "bool" },
        { k: "youtube_max_height", label: "Maximum video height (px, 0 = best available)", type: "number", min: 0, max: 4320 },
        { k: "video_reencode_incompatible", label: "Allow H.264 video fallback (last resort)", type: "bool" },
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
        { k: "soulseek_download_slots", label: "Concurrent download slots", type: "number", min: 1, max: 20 },
        { k: "soulseek_upload_slots", label: "Concurrent upload slots (0 = unlimited)", type: "number", min: 0, max: 20 },
        { k: "soulseek_upload_limit_kib", label: "Per-transfer upload limit (KiB/s, 0 = unlimited)", type: "number", min: 0, max: 1000000 },
        { k: "soulseek_download_limit_kib", label: "Per-transfer download limit (KiB/s, 0 = unlimited)", type: "number", min: 0, max: 1000000 },
        {
          k: "soulseek_web_https", label: "Serve the slskd web UI over HTTPS (extra listener, self-signed)", type: "bool",
        },
        { k: "soulseek_download_dir", label: "Download dir (blank = <music folder>/.mlo/downloads)", type: "text" },
        { k: "soulseek_autostart", label: "Start slskd with the app backend", type: "bool" },
        { k: "soulseek_share_library", label: "Share the music folder on the network", type: "bool" },
        { k: "soulseek_share_dirs", label: "Extra shared folders (; separated, blank = whole music folder)", type: "text" },
        { k: "soulseek_share_exclude", label: "Never share these paths (; separated)", type: "text" },
      ],
    },
    {
      title: "Auto-import (MusicBrainz → Soulseek)",
      blurb: "Search terms are templates of release fields (artist album year date country catalognumber barcode label). CD rips are found by catalog number, digital media by title + year; every disc's .log must reach the score threshold before the album downloads.",
      fields: [
        {
          k: "soulseek_auto_cd_queries", label: "CD query templates (; separated)", type: "text",
          help: "A CD is searched by its catalog number alone by default — the one trait rip folder names carry. Add templates (semicolon-separated) to widen the search; a release with no catalog number falls back to artist + album + year automatically.",
        },
        { k: "soulseek_auto_digital_queries", label: "Digital query templates (; separated)", type: "text" },
        { k: "soulseek_auto_log_min_score", label: "Min .log score (0–100)", type: "number", min: 0, max: 100 },
        { k: "soulseek_auto_complete_ratio", label: "Required track completeness (0.5–1)", type: "number", min: 0.5, max: 1, step: 0.05 },
        { k: "soulseek_auto_search_wait", label: "Fallback search window (seconds of quiet on a rare album)", type: "number", min: 5, max: 300 },
        {
          k: "soulseek_auto_response_limit", label: "Responses before a search is scored (5–500)", type: "number", min: 5, max: 500,
          help: "slskd only hands back a search's results once it has ENDED, and a popular album never goes quiet — this ends the search early instead of waiting out the whole window. Lower = faster and fewer peers; higher = slower and more candidates.",
        },
        { k: "auto_import_avoid_promo", label: "Never auto-import promotional / bootleg editions", type: "bool" },
        {
          k: "auto_import_require_country", label: "Only auto-import editions with a release country", type: "bool",
          help: "A MusicBrainz release without RELEASECOUNTRY is usually an unsorted import, and the CD query templates are built from that field — such editions are skipped, and a group whose only editions lack one is reported as ineligible instead.",
        },
        {
          k: "auto_import_medium_order", label: "Medium preference (comma-separated, best first)", type: "csv",
          help: "Editions are chosen by this media order first, then by earliest release date — and among editions of the same year the one that states the full date, since the album folder is named after it. Blank = the built-in order (CD, Digital Media, Vinyl, Cassette, Other).",
        },
      ],
    },
    {
      title: "Wishes (auto-fill)",
      blurb:
        "Releases saved to the library without downloading them. The background worker re-searches Soulseek for every open wish on the interval below and imports a release the moment a verified match appears.",
      fields: [
        { k: "wishes_enabled", label: "Run the wishes worker", type: "bool" },
        { k: "wishes_interval_hours", label: "Search interval (hours)", type: "number", min: 1, max: 168 },
        { k: "wishes_max_attempts", label: "Max attempts per wish (0 = forever)", type: "number", min: 0, max: 1000 },
        { k: "wishes_auto_import", label: "Auto-import when a verified match is found", type: "bool" },
      ],
    },
    {
      title: "Home",
      blurb:
        "The Home section in the sidebar — its shelves are built from the library itself (recently added, best graded, top artists, favorites, wants, needs attention) with nothing fetched online.",
      fields: [
        { k: "home_recent_count", label: "Recently-added albums shown", type: "number", min: 4, max: 60 },
      ],
    },
    {
      title: "Discovery",
      blurb:
        "Online providers (Deezer, ListenBrainz, MusicBrainz, Last.fm, Wikipedia…) used for artist images and album/artist descriptions. Each order list is tried top to bottom; the first provider with a usable answer wins. An empty list means the built-in order shown as the placeholder.",
      fields: [
        {
          k: "artist_image_sources", label: "Artist image sources (order)", type: "list", catalog: "discovery",
          help: "Used when fetching an artist image automatically; the picked image can still be overridden per artist.",
        },
        {
          k: "description_sources", label: "Description sources (order)", type: "list", catalog: "discovery",
          help: "Used for artist and album descriptions.",
        },
        { k: "discovery_timeout_s", label: "Request timeout (s)", type: "number", min: 3, max: 30 },
        {
          k: "rym_cookie", label: "RateYourMusic cookie", type: "password",
          help: "Only needed when RYM answers with a challenge. Sign in to rateyourmusic.com, press F12 → Network → reload → click any request to rateyourmusic.com → Headers → Request Headers → copy everything after \"Cookie:\" and paste it here (newlines and the \"Cookie:\" label are handled for you). It is a session credential — do not share it, and paste a fresh one when RYM starts refusing, since signing out or clearing cookies invalidates it. Blank = RYM is skipped like any other unavailable source; MusicBrainz still resolves RYM links for well-known releases. Test it with the Sources panel's Test button.",
        },
        {
          k: "rym_links_auto", label: "Auto-find RateYourMusic links", type: "bool",
          help: "Asks rateyourmusic.com for the album and artist pages during an import (and from the link editor's Auto-find button). An existing link is never overwritten, and when RYM refuses the request the import carries on untouched — the link is then left for you to paste by hand.",
        },
        {
          k: "spotify_client_id", label: "Spotify client ID (optional)", type: "text",
          help: "Optional second advisory source (Spotify's ISRC lookup) behind Deezer and ahead of Apple. Empty = Spotify is skipped; an import never fails without it.",
        },
        {
          k: "spotify_client_secret", label: "Spotify client secret (optional)", type: "password",
          help: "Pairs with the client ID above — both are needed before the Spotify lookup runs.",
        },
      ],
    },
    {
      title: "Artist images & descriptions",
      blurb:
        "Artwork and text that live next to the audio: artist photos stored with the artist, and descriptions stored in non-destructive tags. Grading can require them (see the Grading tab).",
      fields: [
        { k: "metadata_auto_fetch", label: "Fetch artist image / descriptions on import", type: "bool" },
        { k: "metadata_review", label: "Review metadata candidates before writing them", type: "bool" },
        { k: "artist_image_enabled", label: "Fetch artist images", type: "bool" },
        { k: "artist_image_crop", label: "Crop artist images to a square", type: "bool" },
        { k: "artist_image_target_size", label: "Artist image max size (px, 0 = keep native size)", type: "number", min: 0, max: 4000 },
        { k: "artist_description_enabled", label: "Fetch artist descriptions", type: "bool" },
        { k: "album_description_enabled", label: "Fetch album descriptions", type: "bool" },
      ],
    },
    {
      title: "Import pipeline",
      blurb:
        "What happens after an album lands in the library (Soulseek downloads, Drag & drop, Finish import). The script chain below runs in order; leaving it blank runs the built-in chain: dedupe → sort → tag → covers → lyrics → audit → ReplayGain → AccurateRip. AcoustID fingerprints the audio to identify the exact release — it needs a free application key from acoustid.org; without one, matching falls back to title/artist/genre against MusicBrainz.",
      fields: [
        { k: "import_auto_scripts", label: "Run the script chain after import", type: "bool" },
        {
          k: "import_scripts", label: "Import script ids (e.g. 1, 3, 5, 7 — blank = built-in chain)", type: "text",
        },
        { k: "import_bulk_concurrency", label: "Bulk import concurrency", type: "number", min: 1, max: 8 },
        { k: "import_acoustid", label: "Fingerprint with AcoustID", type: "bool" },
        { k: "acoustid_enabled", label: "AcoustID enabled", type: "bool" },
        { k: "acoustid_api_key", label: "AcoustID application key (free, acoustid.org)", type: "password" },
        { k: "acoustid_min_score", label: "Minimum AcoustID match score", type: "number", min: 0, max: 1, step: 0.05 },
      ],
    },
    {
      title: "Import & tag cleanup",
      blurb: "Genre importing from MusicBrainz and tag hygiene applied while optimizing.",
      fields: [
        { k: "mb_genre_count", label: "Genres imported per release (MusicBrainz)", type: "number", min: 1, max: 10 },
        {
          k: "genre_sources", label: "Genre sources — every ticked source is asked; unticked ones are never used", type: "multi",
          // The list IS the backend chain (the genre rows of
          // /api/sources/health = server/integrations.GENRE_SOURCES), and each
          // row says whether it can answer per track or only for the release.
          options: genreOptions,
          help: "The genres the sources answer with are merged, deduped and capped at the count above, per track. MusicBrainz is the app's own identity anchor, so leave it on in most setups.",
        },
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
    { k: "grade_check_disallowed", label: "Disallowed files", type: "bool" },
    { k: "grade_check_extra_images", label: "Extra artwork (images not tied to a track)", type: "bool" },
    { k: "grade_check_empty_folders", label: "Empty folders (nothing anywhere beneath them)", type: "bool" },
    { k: "grade_check_naming", label: "Naming script paths", type: "bool" },
    { k: "grade_check_filename_case", label: "Filename capitalization (exact case)", type: "bool" },
    { k: "grade_check_ext_case", label: "Lowercase file extensions", type: "bool" },
    { k: "grade_check_excess_tags", label: "Excess tags (non-canonical)", type: "bool" },
    { k: "grade_check_key_bpm", label: "Key & BPM tags", type: "bool" },
    { k: "grade_check_lyrics_lang_tags", label: "Transform tags carry language (TRANSLATION-EN)", type: "bool" },
    { k: "grade_check_mood", label: "Mood tag present", type: "bool" },
    { k: "grade_check_energy", label: "Energy tag present (0-100, with MOOD)", type: "bool" },
    { k: "grade_check_genre", label: "Genre tag present", type: "bool" },
    { k: "grade_check_album_description", label: "Album description stored", type: "bool" },
    { k: "grade_check_artist_image", label: "Artist image stored", type: "bool" },
    { k: "grade_check_artist_description", label: "Artist description stored", type: "bool" },
    { k: "grade_check_replaygain", label: "ReplayGain tags present (only when a file already carries one)", type: "bool" },
    { k: "grade_check_acoustid", label: "AcoustID tags present (only when a file already carries one)", type: "bool" },
  ];
  // Toggles the General tab renders by hand (they belong to no group tab) —
  // listed here so they load, save and search like every other setting.
  const GENERAL_TOGGLES: CfgField[] = [
    { k: "auto_advance", label: "Auto-advance between Run All scripts", type: "bool" },
    { k: "show_sidecar_files", label: "Show sidecar files (cue/log/lrc/accurip) in library", type: "bool" },
  ];
  const ALL_CFG_KEYS = [
    ...CFG_GROUPS.flatMap((g) => g.fields),
    ...GRADE_CHECK_KEYS,
    ...GENERAL_TOGGLES,
  ].map((f) => f.k);
  const [scriptCfg, setScriptCfg] = useState<Record<string, unknown>>({});
  const setCfg = (k: string, v: unknown) => setScriptCfg((c) => ({ ...c, [k]: v }));
  const [previewPath, setPreviewPath] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [rawConfig, setRawConfig] = useState("{}");
  const [tab, setTab] = useState("general");
  const [runAll, setRunAll] = useState<number[]>(DEFAULT_RUN_ALL);
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
      toast.success(`beets v${r.version} installed`);
      refetchBeets();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBeetsBusy(false);
    }
  };

  // The grid follows the live order (Run All executes exactly this list), so
  // a tick can never move a script in the pipeline without saying so. Scripts
  // that are switched off stay listed after the enabled ones, in factory order.
  const runAllScripts: { id: number; label: string }[] = [
    ...runAll,
    ...DEFAULT_RUN_ALL.filter((id) => !runAll.includes(id)),
  ].map((id) => ({ id, label: SCRIPT_LABEL[id] ?? `#${id}` }));

  /** Tick / untick a script; a re-ticked script goes back to its default
   * pipeline position instead of jumping to the end. */
  const toggleRunAllScript = (id: number, on: boolean) =>
    setRunAll((ids) => {
      if (!on) return ids.filter((i) => i !== id);
      const at = ids.filter((i) => DEFAULT_RUN_ALL.indexOf(i) < DEFAULT_RUN_ALL.indexOf(id)).length;
      return [...ids.slice(0, at), id, ...ids.slice(at)];
    });

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
    { k: "force_tracklist", label: "15 · Release tracklist rewrite" },
    { k: "force_mood", label: "16 · Mood & Energy re-analysis" },
    { k: "force_xlit", label: "17 · Lyrics re-transliterate / re-translate" },
    { k: "force_publish", label: "18 · Lyrics re-publish to LRCLIB" },
  ];

  const NAV: { id: string; label: string; section?: string }[] = [
    { id: "general", label: "General" },
    { id: "appearance", label: "Appearance" },
    { id: "home", label: "Home" },
    { id: "naming", label: "Naming" },
    { id: "tagwrites", label: "Tagging" },
    { id: "grading", label: "Grading" },
    { id: "beets", label: "Beets", section: "Integrations" },
    { id: "soulseek", label: "Soulseek", section: "Integrations" },
    { id: "autoimport", label: "Auto-import", section: "Integrations" },
    { id: "wishes", label: "Wishes", section: "Integrations" },
    { id: "deps", label: "Dependencies", section: "Integrations" },
    { id: "discovery", label: "Discovery", section: "Providers" },
    { id: "sources", label: "Sources", section: "Providers" },
    { id: "artistimages", label: "Artist images", section: "Providers" },
    { id: "ai", label: "AI", section: "Providers" },
    { id: "import", label: "Import", section: "Providers" },
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
    setNamingScript(String(config.naming_script ?? ""));
    setShortFolderNames(!!config.short_folder_names);
    setScriptCfg(Object.fromEntries(ALL_CFG_KEYS.map((k) => [k, config[k] ?? CFG_DEFAULTS[k]])));
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
    setRunAll(Array.isArray(config.run_all_order) ? config.run_all_order.map(Number).filter(isScriptId) : DEFAULT_RUN_ALL);
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

  const { data: configDefaults } = useQuery({
    queryKey: ["configDefaults"],
    queryFn: api.configDefaults,
  });

  /** Restore factory defaults for everything the settings form edits.
   * Identity-critical values the user configured are kept: music folder
   * and first-run flag.
   * Persisted via the normal Save. */
  const resetAllDefaults = () => {
    const d = configDefaults as Record<string, unknown> | undefined;
    if (!d) return;
    const cur = scriptCfg as Record<string, unknown>;
    const next: Record<string, unknown> = { ...d };
    for (const k of ["music_folder", "first_run_done"]) {
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

  /** Clear every UI preference this browser kept: accent, sidebar collapse,
   *  grid sizes, table column layouts and widths, custom columns, the
   *  fullscreen player's look, and the lyrics editor's key map. None of it
   *  lives in the server config, so "Reset to defaults" above cannot reach
   *  it — and a reload is what makes the built-in defaults apply again. */
  const resetUiLayout = () => {
    const keys = Object.keys(localStorage).filter((k) => k.startsWith("mlo"));
    keys.forEach((k) => localStorage.removeItem(k));
    toast(`Layout reset — ${keys.length} UI preference(s) cleared`);
    setTimeout(() => window.location.reload(), 500);
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
      toast.success("Config saved");
      qc.invalidateQueries({ queryKey: ["config"] });
      qc.invalidateQueries({ queryKey: ["library"] });
      qc.invalidateQueries({ queryKey: ["importScriptsPreview"] });
    } catch (e) {
      toast.error(String(e));
    }
  };

  const applyRaw = () => {
    try {
      const parsed = JSON.parse(rawConfig) as Record<string, unknown>;
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new Error("expected an object");
      const enc = (parsed.encoder_tags ?? {}) as Record<string, Record<string, boolean>>;
      const aw = (parsed.audio_tag_writes ?? {}) as Record<string, Record<string, boolean>>;
      setScriptCfg(Object.fromEntries(ALL_CFG_KEYS.map((k) => [k, parsed[k] ?? CFG_DEFAULTS[k]])));
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
      setRunAll(Array.isArray(parsed.run_all_order) ? parsed.run_all_order.map(Number).filter(isScriptId) : runAll);
      toast("Raw config applied — click Save all settings to persist");
    } catch (e) {
      toast.error("Invalid JSON: " + String(e));
    }
  };

  // Tab → group, matched by title (robust against group reordering).
  const GROUP_BY_TAB: Record<string, CfgGroup> = Object.fromEntries(
    [
      ["flac", "FLACs"], ["embedcovers", "Embedded covers"], ["images", "Images"], ["lyrics", "Lyrics & CUEs"],
      ["dr", "DR / ReplayGain"], ["audit", "Audit"], ["autotag", "AutoTag"],
      ["accurip", "AccurateRip"], ["tagwrites", "Tag writes"],
      ["grading", "Grading"], ["cdrips", "CD Rips"], ["videos", "Videos"],
      ["audiometa", "Key & BPM"], ["beets", "Beets tagging"],
      ["soulseek", "Soulseek (managed slskd)"], ["autoimport", "Auto-import"],
      ["wishes", "Wishes"], ["home", "Home"],
      ["discovery", "Discovery"], ["artistimages", "Artist images"], ["ai", "AI lyric transforms"], ["import", "Import pipeline"],
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
    for (const f of GENERAL_TOGGLES) {
      if (f.label.toLowerCase().includes(needle) || f.k.toLowerCase().includes(needle)) {
        hits.push({ tab: "general", group: "General", field: f.label });
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
      if (failed.length) toast.error("Install finished with " + failed.length + " failure(s)");
      else toast.success("Dependencies installed / updated");
      refetchDeps();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setDepsBusy(false);
    }
  };

  // Which scripts the saved import chain runs (blank import_scripts = built-in).
  const { data: chainPreview } = useQuery({
    queryKey: ["importScriptsPreview"],
    queryFn: () => api.importScriptsPreview([]),
    enabled: tab === "import",
  });

  // Catalogues behind the `list` fields: pickable providers plus the built-in
  // order, shown as the placeholder while a list is empty.
  const listCatalogs: Record<"discovery" | "lyrics", { options: ProviderOption[]; builtin: (k: string) => string[] }> = {
    discovery: { options: discoveryCat?.sources ?? [], builtin: (k) => discoveryCat?.defaults?.[k] ?? [] },
    // Only time-synced providers are pickable: a plain-lyrics-only entry has
    // no place in the chain (the `synced` flag comes from the endpoint).
    lyrics: {
      options: (lyricsCat?.sources ?? []).filter((s) => s.synced !== false),
      builtin: () => lyricsCat?.default_order ?? [],
    },
  };

  const renderFields = (fields: CfgField[]) => (
    <div className="stagger grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-x-6 gap-y-2.5 mt-2">
      {fields.map((f) => {
        if (f.type === "list") {
          const raw = scriptCfg[f.k];
          const order = Array.isArray(raw)
            ? raw.map(String)
            : typeof raw === "string"
              ? raw.split(/[,;\s]+/).filter(Boolean)
              : [];
          return (
            <div key={f.k} className="md:col-span-2 xl:col-span-3">
              <div className="text-[11px] text-zinc-400 mb-1">{f.label}</div>
              <ProviderOrder
                options={listCatalogs[f.catalog].options}
                builtin={listCatalogs[f.catalog].builtin(f.k)}
                order={order}
                onChange={(next) => setCfg(f.k, next)}
                help={f.help}
              />
            </div>
          );
        }
        if (f.type === "multi") {
          const on = Array.isArray(scriptCfg[f.k]) ? (scriptCfg[f.k] as unknown[]).map(String) : [];
          // An id the catalogue does not list (a source the chain dropped, or
          // the health payload not arrived yet) still has to be visible, or a
          // saved value would silently disappear from the picker.
          const opts: [string, string][] = [
            ...f.options,
            ...on.filter((v) => !f.options.some(([o]) => o === v)).map((v): [string, string] => [v, v]),
          ];
          return (
            <div key={f.k} className="md:col-span-2 xl:col-span-3">
              <div className="text-[11px] text-zinc-400 mb-1">{f.label}</div>
              <div className="flex flex-wrap gap-x-4 gap-y-1">
                {opts.map(([v, l]) => (
                  <label key={v} className="flex items-center gap-2 text-[13px] text-zinc-300 cursor-pointer select-none">
                    <input
                      type="checkbox"
                      checked={on.includes(v)}
                      onChange={(e) => setCfg(f.k, e.target.checked ? [...on, v] : on.filter((x) => x !== v))}
                    />
                    {l}
                  </label>
                ))}
              </div>
              {f.help && <div className="text-[10px] text-zinc-600 mt-1">{f.help}</div>}
            </div>
          );
        }
        if (f.type === "csv") {
          // Edited as text, stored as the string list the config expects;
          // blank entries are dropped by the backend's own normalization.
          const parts = Array.isArray(scriptCfg[f.k]) ? (scriptCfg[f.k] as unknown[]).map(String) : [];
          return (
            <div key={f.k} className="md:col-span-2 xl:col-span-3">
              <div className="text-[11px] text-zinc-400 mb-1">{f.label}</div>
              <input
                className="input !py-1 text-xs w-full"
                value={parts.join(", ")}
                onChange={(e) => setCfg(f.k, e.target.value.split(",").map((s) => s.trim()))}
              />
              {f.help && <div className="text-[10px] text-zinc-600 mt-1">{f.help}</div>}
            </div>
          );
        }
        return (
        <label key={f.k} className="flex items-center gap-2 text-[13px] text-zinc-300 cursor-pointer select-none">
          {f.type === "bool" ? (
            <div className="w-full">
              <span className="flex items-center gap-2 w-full">
                <input type="checkbox" checked={!!scriptCfg[f.k]} onChange={(e) => setCfg(f.k, e.target.checked)} />
                {f.label}
              </span>
              {f.help && <div className="text-[10px] text-zinc-600 mt-0.5">{f.help}</div>}
            </div>
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
            <div className="w-full">
              <div className="flex items-center gap-2 w-full">
                <span className="flex-1 min-w-0 truncate">{f.label}</span>
                <input
                  className="input !w-32 !py-0.5 text-[11px] shrink-0"
                  value={
                    Array.isArray(scriptCfg[f.k])
                      ? (scriptCfg[f.k] as unknown[]).join("; ")
                      : String(scriptCfg[f.k] ?? "")
                  }
                  onChange={(e) => setCfg(f.k, e.target.value)}
                />
              </div>
              {f.help && <div className="text-[10px] text-zinc-600 mt-0.5">{f.help}</div>}
            </div>
          ) : f.type === "password" ? (
            <div className="w-full">
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
              {f.help && <div className="text-[10px] text-zinc-600 mt-0.5">{f.help}</div>}
            </div>
          ) : (
            <div className="w-full">
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
              {f.help && <div className="text-[10px] text-zinc-600 mt-0.5">{f.help}</div>}
            </div>
          )}
        </label>
        );
      })}
    </div>
  );

  return (
    <div className="p-6 space-y-5 mx-auto max-w-6xl">
      <PageHeader icon={SettingsIcon} title="Settings">
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
      </PageHeader>

      <div className="flex gap-6">
        <nav className="w-44 shrink-0 space-y-0.5 sticky top-20 self-start max-h-[calc(100vh-120px)] overflow-auto pr-1">
          {NAV.map((n, i) => (
            <div key={n.id}>
              {n.section && (i === 0 || NAV[i - 1].section !== n.section) && (
                <div className="px-3 pt-3 pb-1 text-[10px] uppercase tracking-wider text-zinc-600 first:pt-0">{n.section}</div>
              )}
              <button
                onClick={() => setTab(n.id)}
                className={`w-full text-left px-3 py-1.5 rounded-md text-xs transition-colors ${
                  tab === n.id ? "bg-accent on-accent font-medium" : "text-zinc-400 hover:text-white hover:bg-panel border border-transparent"
                }`}
              >
                {n.label}
              </button>
            </div>
          ))}
        </nav>

        <div className="flex-1 min-w-0 space-y-5 pb-10">
          {tab === "general" && (
            <div className="panel space-y-3">
              <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Library</div>
              <div className="rounded-md border border-border bg-zinc-950/40 px-3 py-2">
                <div className="text-xs text-zinc-500 uppercase">Music folder</div>
                <div className="font-mono text-xs text-zinc-200 break-all mt-1">
                  {musicFolder.trim() || "not configured yet"}
                </div>
                <div className="text-[11px] text-zinc-600 mt-1 leading-relaxed">
                  Decided at startup and read-only here: <code>MLO_MUSIC_FOLDER</code> (Docker / compose) or{" "}
                  <code>music_folder</code> in config.json (the Raw config box below). Restart the app after changing it.
                </div>
              </div>
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
                {GENERAL_TOGGLES.map((f) => (
                  <label key={f.k} className="flex items-center gap-2 text-xs text-zinc-300 cursor-pointer select-none">
                    <input type="checkbox" checked={!!scriptCfg[f.k]} onChange={(e) => setCfg(f.k, e.target.checked)} />
                    {f.label}
                  </label>
                ))}
              </div>
              <details className="bg-zinc-950/40 rounded-lg border border-border px-3 py-2">
                <summary className="text-xs font-medium cursor-pointer text-zinc-400 select-none">
                  Run All — scripts in order ({runAll.length} enabled)
                </summary>
                <div className="grid grid-cols-2 md:grid-cols-3 gap-x-4 gap-y-1.5 mt-2">
                  {runAllScripts.map((s) => (
                    <label key={s.id} className="flex items-center gap-2 text-xs text-zinc-300 cursor-pointer select-none">
                      <input
                        type="checkbox"
                        checked={runAll.includes(s.id)}
                        onChange={(e) => toggleRunAllScript(s.id, e.target.checked)}
                      />
                      <span className="text-zinc-600 w-4">{s.id}</span>
                      {s.label}
                    </label>
                  ))}
                </div>
                <div className="text-[10px] text-zinc-600 mt-1">The Run All button executes them in this order.</div>
              </details>
              <div className="rounded-md border border-border bg-zinc-950/40 px-3 py-2 space-y-1.5">
                <div className="text-xs text-zinc-400">Setup wizard</div>
                <div className="text-[11px] text-zinc-600 leading-relaxed">
                  Re-runs the first-run walkthrough — music folder, dependencies, sources, AI &amp;
                  RateYourMusic, Soulseek sharing. Every step is skippable, and nothing on this page is
                  reset by it: it only writes what you enter there.
                </div>
                <button
                  className="btn-ghost !py-1 text-xs"
                  onClick={() => navigate("/setup")}
                >
                  <Wand2 className="h-3 w-3" /> Run the setup wizard again
                </button>
              </div>
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
            <div className="panel space-y-3">
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
            <div className="panel space-y-3">
              <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">File naming (Picard-style script)</div>
              <textarea
                className="input font-mono text-xs min-h-[110px]"
                value={namingScript}
                onChange={(e) => setNamingScript(e.target.value)}
                spellCheck={false}
              />
              <div className="text-[11px] text-zinc-600 leading-relaxed">
                Variables: <code>%albumartist% %musicbrainz_albumartistid% %album% %musicbrainz_albumid% %title% %musicbrainz_trackid% %releasetype% %year% %originaldate% %date% %label% %releasecountry% %media% %catalognumber% %discnumber% %tracknumber%</code> ·
                Functions: <code>$if(a,b,c) $left(s,n) $right(s,n) $num(s,n) $lower $upper $replace $eq $ne $not $and $or</code> · <code>/</code> creates folders.
                Applied from the album page or the bulk selection toolbar; Grading compares every path against this script.
              </div>
              <div className="flex items-center gap-4 flex-wrap">
                <label className="flex items-center gap-2 text-xs text-zinc-400 cursor-pointer select-none">
                  <input type="checkbox" checked={shortFolderNames} onChange={(e) => setShortFolderNames(e.target.checked)} />
                  Shorter folder names (truncate MusicBrainz IDs to 8 chars)
                </label>
                <button
                  className="btn-ghost !py-1 text-xs"
                  onClick={() =>
                    setNamingScript(
                      String((configDefaults as Record<string, unknown> | undefined)?.naming_script ?? "")
                    )
                  }
                >
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

          {tab === "sources" && (
            <div className="panel space-y-3">
              <SourcesPanel />
            </div>
          )}

          {tab === "deps" && (
            <div className="panel space-y-3">
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
              <div className="rounded-md border border-border overflow-hidden table-scroll">
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
                          {t.state === "ok" && <span className="chip bg-emerald-900/50 text-emerald-300 border border-emerald-800">Ready</span>}
                          {t.state === "update" && <span className="chip bg-amber-900/50 text-amber-300 border border-amber-900">Update</span>}
                          {t.state === "missing" && <span className="chip bg-red-900/50 text-red-300 border border-red-900">Missing</span>}
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
            <div className="panel space-y-3">
              <div className="text-xs font-bold text-zinc-300">{GROUP_BY_TAB[tab].title}</div>
              {GROUP_BY_TAB[tab].blurb && <div className="text-[10px] text-zinc-600">{GROUP_BY_TAB[tab].blurb}</div>}
              {renderFields(GROUP_BY_TAB[tab].fields)}
              {tab === "ai" && (
                <div className="pt-2 border-t border-border space-y-1">
                  <AiTestButton value={scriptCfg} />
                  <div className="text-[10px] text-zinc-600">
                    Tests the values on screen — save first if you want them to stick. Script 17 runs from the
                    Optimization page, the library selection menu, or as part of Run All.
                  </div>
                </div>
              )}
              {tab === "images" && (
                <div className="pt-2 border-t border-border">
                  <CoverDefaults />
                </div>
              )}
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
                      <summary className="cursor-pointer text-zinc-500">generated beets config ({"<music folder>/.mlo/data/beets-config.yaml"})</summary>
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
              {tab === "import" && (
                <div className="pt-2 border-t border-border space-y-1">
                  <div className="text-[11px] text-zinc-400">
                    Chain after saving ({chainPreview?.count ?? 0}{" "}
                    {(chainPreview?.count ?? 0) === 1 ? "script" : "scripts"}):{" "}
                    <span className="text-zinc-300">
                      {chainPreview?.chain.length
                        ? chainPreview.chain
                            .map((id) => chainPreview.labels[String(id)] ?? `#${id}`)
                            .join(" → ")
                        : "nothing runs"}
                    </span>
                  </div>
                  <div className="text-[10px] text-zinc-600">
                    Read from the saved config — click <em>Save all settings</em> to preview an edited list.
                    A blank field falls back to the built-in chain, and the chain is skipped entirely when
                    &ldquo;Run the script chain after import&rdquo; is off.
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
            <details className="panel" open>
              <summary className="text-sm font-semibold cursor-pointer">Individual grading checks ({GRADE_CHECK_KEYS.length})</summary>
              <div className="mt-2">{renderFields(GRADE_CHECK_KEYS)}</div>
            </details>
          )}

          <div className="flex items-center gap-2">
            <ConfirmButton
              onConfirm={resetAllDefaults}
              confirmLabel="Reset all"
              disabled={!configDefaults}
              title="Restore factory defaults for every setting (music folder and first-run flag are kept)"
            >
              <RotateCcw className="h-4 w-4" /> Reset to defaults
            </ConfirmButton>
            <ConfirmButton
              onConfirm={resetUiLayout}
              confirmLabel="Reset layout"
              title="Clear this browser's UI preferences — accent, sidebar, grid sizes, column layouts and widths, custom columns, viewer options — and reload"
            >
              <LayoutGrid className="h-4 w-4" /> Reset UI & layout
            </ConfirmButton>
            <button className="btn-primary" onClick={save}>
              <Save className="h-4 w-4" /> Save all settings
            </button>
          </div>

          <details className="panel">
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

          <details className="panel">
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