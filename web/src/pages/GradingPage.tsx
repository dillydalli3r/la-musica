import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ClipboardCheck, RotateCcw, Save } from "lucide-react";
import { api } from "../api";
import { toast } from "../store";
import ConfirmButton from "../components/ConfirmButton";

/** In-depth grading configuration: every check that can count for or
 * against grading, grouped the way they apply — track/album checks,
 * auditing, links, covers, strict formatting, lyrics & translations and
 * the file categories that participate in album grading at all. Toggles
 * edit a local copy; Save writes the whole grading block via saveConfig. */

interface CheckDef {
  k: string;
  label: string;
  desc: string;
  /** the companion feature must be enabled for the check to ever fire */
  requires?: string;
  /** check fires only when AI tooling is configured */
  needsAi?: boolean;
}

interface Group {
  id: string;
  title: string;
  desc: string;
  items: CheckDef[];
}

const GROUPS: Group[] = [
  {
    id: "tracks",
    title: "Tracks & albums",
    desc: "Core checks applied to every track and album in the library.",
    items: [
      { k: "grade_check_unreadable", label: "Unreadable files", desc: "Files that can't be opened or decoded fail the album." },
      { k: "grade_check_missing_tags", label: "Required tags", desc: "Every required per-track tag (title, artist, date, …) must exist and be non-empty." },
      { k: "grade_check_album_tags", label: "Album-level tags", desc: "Album-wide tags (album, album artist, catalog number, …) must be present on the tracks." },
      { k: "grade_check_encoder", label: "Encoder identity", desc: "ENCODER_PROGRAM / QUALITY / VERSION must be present." },
      { k: "grade_check_naming", label: "Naming script match", desc: "File paths must match the configured naming script (full or shortened MusicBrainz IDs both accepted)." },
      { k: "grade_check_filename_case", label: "Path capitalization", desc: "Filenames and folder names must match the naming script's letter case exactly — TOXICITY vs Toxicity fails. Organize applies the canonical casing." },
      { k: "grade_check_ext_case", label: "Lowercase extensions", desc: "File extensions must be lowercase (01 - Song.FLAC fails). Organize lowercases every extension it touches." },
      { k: "grade_check_key_bpm", label: "Key & BPM", desc: "INITIALKEY and BPM tags (written by script 12) are required." },
      { k: "grade_check_excess_tags", label: "Excess tags", desc: "Any tag the optimizer would strip — outside the known tag set — fails the track. Run Optimization to remove them." },
      { k: "grade_check_media", label: "Media type", desc: "The MEDIA tag must be present and consistent with the release." },
      { k: "grade_check_source", label: "Source tag", desc: "The SOURCE tag must be present (with different rules for CD vs digital releases)." },
      { k: "grade_check_instrumental", label: "Instrumental consistency", desc: "INSTRUMENTAL=1 tracks must not carry lyrics; INSTRUMENTAL=0 tracks are graded for lyrics below." },
      { k: "grade_check_disallowed", label: "Disallowed file types", desc: "Unclassified files (.txt, .pdf, .m3u, …) fail the album unless their category is enabled under File categories." },
      { k: "grade_check_extra_images", label: "Stray images", desc: "Images that are neither cover.* nor per-track sidecars fail the album." },
      { k: "grade_check_raw_video", label: "Raw videos", desc: "Un-remuxed videos (VOB/AVI/WMV/TS) fail — run script 11 to normalize them to MKV." },
      { k: "grade_check_lossless_source", label: "Lossless sources", desc: "Uncompressed lossless sources (WAV/AIFF/APE/WV/SHN) fail — script 3 converts them to FLAC." },
      { k: "grade_check_disc_naming", label: "Disc folder naming", desc: "Multi-disc albums must follow the disc naming pattern (Disc 1, …)." },
      { k: "grade_check_cd_log", label: "CD — .log present", desc: "Every CD disc needs an exact-match .log file." },
      { k: "grade_check_cd_cue", label: "CD — .cue present", desc: "Every CD disc needs a .cue sheet." },
      { k: "grade_check_cd_format", label: "CD — lossless format", desc: "CD tracks must be FLAC (lossless)." },
      { k: "grade_check_crc", label: "CRC checksums", desc: "CUE sheet CRCs / embedded checksums must match the audio." },
    ],
  },
  {
    id: "auditing",
    title: "Auditing",
    desc: "Audio verification (script 6) and the log scores it produces. A FAKE or MIX audit always fails regardless of these toggles.",
    items: [
      { k: "grade_check_audit", label: "Require audit tag", desc: "Tracks must carry an AUDIT tag (run Audit Library). Off by default so unaudited libraries aren't auto-failed." },
      { k: "grade_check_log_checksum", label: "Log checksum valid", desc: "When a .log with checksums exists, its checksums must verify." },
      { k: "grade_check_accuraterip", label: "AccurateRip verified", desc: ".accurip results must match the reference database." },
      { k: "grade_check_log_grade", label: "Log grade present & in range", desc: "LOG_GRADE tag must exist and be 0–100." },
    ],
  },
  {
    id: "links",
    title: "Identity links",
    desc: "The two release-level identity links, graded per track.",
    items: [
      { k: "grade_check_mb_links", label: "MusicBrainz release link", desc: "The MusicBrainz release (or its release group) must be tagged." },
      { k: "grade_check_rym_links", label: "RateYourMusic release link", desc: "The RateYourMusic release page URL must be tagged." },
    ],
  },
  {
    id: "covers",
    title: "Covers",
    desc: "Cover art presence and the size / square rules from Settings → Images.",
    items: [
      { k: "grade_check_cover", label: "Cover art", desc: "The album must have cover art meeting the configured size, squareness and crop rules." },
      { k: "grade_check_cover_crop", label: "Cropped cover detection", desc: "Covers that look cropped from a larger source fail (when size enforcement is on)." },
      { k: "grade_check_sidecar_cover", label: "Per-track sidecar covers", desc: "Sidecar covers (01 - Song.jpg) must meet the same cover rules." },
    ],
  },
  {
    id: "formatting",
    title: "Strict formatting",
    desc: "Whitespace / blank-line / canonical-form rules. These make near-miss files fail so the formatter scripts can fix them.",
    items: [
      { k: "grade_check_tag_spaces", label: "Tags — no padding", desc: "Leading/trailing spaces or tabs in any tag value fail." },
      { k: "grade_check_tag_blank_lines", label: "Tags — no blank lines", desc: "Blank lines inside tag values fail (LYRICS is exempt — its own rules apply)." },
      { k: "grade_check_lyrics_spaces", label: "Lyrics — no padding", desc: "Leading/trailing spaces on lyric lines fail." },
      { k: "grade_check_lyrics_blank_lines", label: "Lyrics — blank line rules", desc: "Blank-line placement must match the lyrics formatter's canonical output." },
      { k: "grade_check_lyrics_zero", label: "Lyrics — zero timestamp rule", desc: "The [00:00.00] leader line must follow the configured lyrics rules." },
      { k: "grade_check_lyrics_format", label: "Lyrics — canonical formatting", desc: "Stored lyrics must exactly match what the Format Lyrics script would produce." },
      { k: "grade_check_cue_spaces", label: "CUE — no padding", desc: "Leading/trailing spaces in CUE lines fail." },
      { k: "grade_check_cue_blank_lines", label: "CUE — no blank lines", desc: "Blank lines in CUE sheets fail." },
      { k: "grade_check_cue_format", label: "CUE — canonical formatting", desc: "CUE sheets must match the canonical formatter output." },
    ],
  },
  {
    id: "lyrics",
    title: "Lyrics & translations",
    desc: "Presence checks for lyrics and the script 15 transforms. The transliteration/translation checks only fire when AI tooling is configured and the lyrics actually need them (cross-script rules).",
    items: [
      { k: "grade_check_lyrics", label: "Lyrics present", desc: "Every non-instrumental track needs lyrics (embedded and/or .lrc sidecar, per the lyrics format)." },
      { k: "grade_check_lyrics_lang_tags", label: "Transform language tags", desc: "Transform tags must carry their language (TRANSLATION-EN, TRANSLITERATION-JA-LATN — never the bare legacy names)." },
      { k: "grade_check_xlit", label: "Transliteration present", desc: "Lyrics in a script you don't read need romanization (.romaji.lrc or the TRANSLITERATION tag).", needsAi: true },
      { k: "grade_check_trans", label: "Translation present", desc: "Lyrics in another script need a translation (.<lang>.lrc or the TRANSLATION tag).", needsAi: true },
    ],
  },
  {
    id: "categories",
    title: "File categories",
    desc: "Which file types participate in album grading at all — turning a category off means those files neither help nor hurt the grade.",
    items: [
      { k: "grade_include_music", label: "Audio tracks", desc: "The music files themselves." },
      { k: "grade_include_cover", label: "Cover art", desc: "cover.* images count toward the grade." },
      { k: "grade_include_cue", label: "CUE sheets", desc: ".cue sidecars count toward the grade." },
      { k: "grade_include_log", label: "Log files", desc: ".log sidecars count toward the grade." },
      { k: "grade_include_lrc", label: "LRC lyrics", desc: ".lrc sidecars count toward the grade." },
      { k: "grade_include_accurip", label: "AccurateRip files", desc: ".accurip files count toward the grade." },
      { k: "grade_include_video", label: "Remuxed videos", desc: "MKV/MP4 music videos count toward the grade." },
      { k: "grade_include_other", label: "Other files", desc: "Anything unclassified counts toward the grade. Off by default." },
    ],
  },
];

/** Numeric settings shown alongside the toggles. */
const NUMBERS: { k: string; label: string; desc: string; min: number; max: number }[] = [
  {
    k: "grade_log_score_threshold",
    label: "Logchecker score threshold",
    desc: "LOG_GRADE must be at least this value when the master log-grade check is on. 0 disables the threshold.",
    min: 0,
    max: 100,
  },
];

/** Every grading key this page owns — the reset-to-defaults scope. */
const GRADING_KEYS = [
  ...GROUPS.flatMap((g) => g.items.map((i) => i.k)),
  ...NUMBERS.map((n) => n.k),
];

export default function GradingPage() {
  const { data: config } = useQuery({ queryKey: ["config"], queryFn: api.config });
  const { data: defaults } = useQuery({ queryKey: ["configDefaults"], queryFn: api.configDefaults });
  const qc = useQueryClient();
  const [local, setLocal] = useState<Record<string, unknown> | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (config && local === null) setLocal({ ...config });
  }, [config, local]);

  const dirty = useMemo(() => {
    if (!local || !config) return false;
    return JSON.stringify(local) !== JSON.stringify(config);
  }, [local, config]);

  const set = (k: string, v: unknown) => setLocal((c) => ({ ...(c ?? {}), [k]: v }));

  const save = async () => {
    if (!local) return;
    setSaving(true);
    try {
      await api.saveConfig(local);
      // refresh the base truth so the dirty flag clears against what the
      // server now holds
      await qc.invalidateQueries({ queryKey: ["config"] });
      toast("Grading settings saved");
    } catch (e) {
      toast(String(e));
    } finally {
      setSaving(false);
    }
  };

  const discard = () => {
    if (config) setLocal({ ...config });
    toast("Changes discarded");
  };

  const resetDefaults = () => {
    const d = defaults as Record<string, unknown> | undefined;
    if (!d) return;
    setLocal((c) => {
      const next = { ...(c ?? {}) };
      for (const k of GRADING_KEYS) if (d[k] !== undefined) next[k] = d[k];
      return next;
    });
    toast("Grading checks reset to defaults — Save to apply");
  };

  const val = (k: string) => !!local?.[k];
  const aiReady = !!String(local?.ai_base_url ?? "").trim() && !!String(local?.ai_model ?? "").trim();

  return (
    <div className="p-6 space-y-5">
      <div className="flex items-start justify-between gap-4 sticky top-0 z-20 bg-bg/95 backdrop-blur py-2 -mt-2">
        <div>
          <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
            <ClipboardCheck className="h-6 w-6 text-accent" /> Grading
          </h1>
          <p className="text-xs text-zinc-500 mt-0.5 max-w-2xl">
            Everything that can count for or against a grade, checked per track, album and file.
            Toggles take effect the next time the grader runs (any grade view or the Grade script).
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {dirty && <span className="text-[10px] font-mono text-amber-400/80">unsaved changes</span>}
          <ConfirmButton
            onConfirm={resetDefaults}
            confirmLabel="Reset checks"
            disabled={!defaults || saving}
            title="Restore factory defaults for every grading check"
          >
            <RotateCcw className="h-3.5 w-3.5" /> Reset to defaults
          </ConfirmButton>
          <button className="btn-ghost !py-1.5 text-xs" onClick={discard} disabled={!dirty || saving}>
            <RotateCcw className="h-3.5 w-3.5" /> Discard
          </button>
          <button className="btn-primary !py-1.5 text-xs" onClick={save} disabled={!dirty || saving}>
            <Save className="h-3.5 w-3.5" /> {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>

      {!local ? (
        <div className="text-sm text-zinc-500 py-10 text-center">Loading grading settings…</div>
      ) : (
        GROUPS.map((g) => (
          <section key={g.id} className="space-y-1.5">
            <div className="px-1 pt-2">
              <div className="text-sm font-semibold">{g.title}</div>
              <div className="text-[11px] text-zinc-500">{g.desc}</div>
            </div>
            <div className="divide-y divide-border/40 rounded-lg border border-border/60 bg-panel/40">
              {g.items.map((it) => {
                const off = it.needsAi && !aiReady;
                return (
                  <label
                    key={it.k}
                    className="flex items-start gap-3 px-3.5 py-2.5 cursor-pointer select-none hover:bg-raise/40 transition-colors"
                  >
                    <input
                      type="checkbox"
                      className="mt-0.5"
                      checked={val(it.k)}
                      onChange={(e) => set(it.k, e.target.checked)}
                    />
                    <span className="min-w-0">
                      <span className="text-sm text-zinc-200 block">
                        {it.label}
                        {off && <span className="ml-2 text-[10px] font-mono text-zinc-600">needs AI configured</span>}
                        {it.requires && <span className="ml-2 text-[10px] font-mono text-zinc-600">requires {it.requires}</span>}
                      </span>
                      <span className="text-[11px] text-zinc-500 block leading-snug">{it.desc}</span>
                    </span>
                  </label>
                );
              })}
              {g.id === "auditing" &&
                NUMBERS.map((n) => (
                  <div key={n.k} className="flex items-start gap-3 px-3.5 py-2.5">
                    <span className="min-w-0 flex-1">
                      <span className="text-sm text-zinc-200 block">{n.label}</span>
                      <span className="text-[11px] text-zinc-500 block leading-snug">{n.desc}</span>
                    </span>
                    <input
                      className="input !w-20 !py-1 text-sm shrink-0"
                      type="number"
                      min={n.min}
                      max={n.max}
                      value={Number(local[n.k] ?? 0)}
                      onChange={(e) => set(n.k, Math.max(n.min, Math.min(n.max, Number(e.target.value) || 0)))}
                    />
                  </div>
                ))}
            </div>
          </section>
        ))
      )}
      <div className="h-4" />
    </div>
  );
}
