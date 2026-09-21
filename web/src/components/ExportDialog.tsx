import { useState } from "react";
import type { ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, FileOutput, HardDriveDownload, RotateCcw, Save } from "lucide-react";
import { api } from "../api";
import type { ExportCodecSpec, ExportForm } from "../api";
import { toast } from "../store";
import { fmtDuration } from "../lib/fmt";
import Modal from "./Modal";

/** The dropdown's synthetic entry for a codec's "custom value" field; the
 * backend takes the plain number the field holds (kbps, or 0-10 for Vorbis),
 * so nothing but the form needs to know the word. */
export const CUSTOM = "custom";

export const STRUCTURES = [
  { v: "artist_album", label: "Artist / Album / 01 - Title" },
  { v: "album", label: "Album / 01 - Title" },
  { v: "flat", label: "Flat — one folder" },
  { v: "mirror", label: "Mirror library layout" },
];

/** Rendered while the saved defaults are still loading; the same shape and the
 * same first-run values the backend ships (server.exporter.EXPORT_DEFAULTS). */
export const BLANK_FORM: ExportForm = {
  dest: "",
  subfolder: "Music",
  codec: "copy",
  quality: "",
  structure: "artist_album",
  embed_covers: true,
  embed_cover_jpeg_quality: 90,
  embed_cover_resolution: 1200,
  id3v2: "2.3",
  id3v1: false,
  replaygain: false,
  clean_tags: true,
  playlists: true,
  sidecars: true,
  verify: true,
  prune: false,
  workers: 0,
};

export function fmtGB(n: number | null): string {
  return n === null ? "—" : `${(n / 1024 ** 3).toFixed(1)} GB`;
}

/** Effective kbps for the drive-fit estimate, from the server's own preset
 * hints (server/exporter.py CODECS). null = unpredictable: a bit-exact copy,
 * a lossless re-encode, or a custom Vorbis q. */
export function effectiveKbps(spec: ExportCodecSpec | undefined, quality: string, custom: string): number | null {
  if (!spec) return null;
  const preset = spec.presets.find((p) => p.v === quality);
  if (preset) return preset.kbps;
  if (spec.custom && spec.custom.mode === "kbps") {
    const n = parseInt(quality === CUSTOM ? custom : quality, 10);
    if (!Number.isFinite(n)) return null;
    return Math.min(spec.custom.max, Math.max(spec.custom.min, n));
  }
  return null;
}

/** One option row: the checkbox plus its one-line explanation. The
 * compatibility switches are numerous enough that bare checkbox rows would
 * not say what they do. */
function Opt({ checked, onChange, label, hint, danger }: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  hint: string;
  danger?: boolean;
}) {
  return (
    <label className="flex items-start gap-2 mt-2 text-xs text-zinc-300 cursor-pointer">
      <input
        type="checkbox"
        className="mt-0.5"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="min-w-0">
        <span className={"block " + (danger ? "text-amber-300" : "")}>{label}</span>
        <span className="block text-[10px] text-zinc-600">{hint}</span>
      </span>
    </label>
  );
}

/** Every track of the given playlists, in playlist order and deduplicated —
 *  what a page that represents a set of playlists exports/downloads. One
 *  query keyed by the id list, so the header buttons on a listing page cost
 *  one request per playlist instead of one per render. */
export function usePlaylistTracks(ids: number[], enabled = true) {
  const key = ids.join(",");
  return useQuery({
    queryKey: ["playlistTracks", key],
    queryFn: async () => {
      const details = await Promise.all(ids.map((id) => api.playlist(id)));
      const seen = new Set<string>();
      const out: string[] = [];
      for (const d of details)
        for (const p of d.tracks ?? [])
          if (!seen.has(p)) {
            seen.add(p);
            out.push(p);
          }
      return out;
    },
    enabled: enabled && ids.length > 0,
    staleTime: 30_000,
  });
}

/** One destination the server offers (server/exporter.list_drives). */
export interface ExportDrive {
  letter: string;
  root: string;
  type: string;
  free: number | null;
  total: number | null;
}

/** The codec table as the server publishes it (server/exporter.codec_specs). */
export interface ExportCodecs {
  codecs: Record<string, ExportCodecSpec>;
}

/** Whatever `useExportOptions` hands the panel: the live form, what it derives
 *  from the server's tables, and the run/save/reset actions. */
export interface ExportOptions {
  f: ExportForm;
  set: <K extends keyof ExportForm>(key: K, value: ExportForm[K]) => void;
  customValue: string;
  setCustomValue: (v: string) => void;
  busy: boolean;
  specs: ExportCodecs | undefined;
  spec: ExportCodecSpec | undefined;
  /** Effective kbps of the chosen codec+quality, or null when unpredictable. */
  kbps: number | null;
  /** Predicted output size, or null when `seconds` is 0 or kbps unknown. */
  estBytes: number | null;
  drives: ExportDrive[];
  selectedDrive: ExportDrive | null;
  overCapacity: boolean;
  /** The selection this form would export. */
  paths: string[];
  seconds: number;
  /** True when a run started and finished; false when validation refused it. */
  run: () => Promise<boolean>;
  saveDefaults: () => Promise<void>;
  resetDefaults: () => void;
  refreshDrives: () => void;
}

/** The export form's state and its run/save/reset actions, shared verbatim by
 *  the Export page and the per-page dialog: the user's edits override the
 *  saved `export_*` config values, which override the shipped defaults. An
 *  export sends exactly what is displayed; nothing is written back to config
 *  until "Save as default" is pressed. `seconds` is the selection's total
 *  duration (0 when unknown) and only feeds the drive-fit estimate. */
export function useExportOptions(paths: string[], seconds = 0): ExportOptions {
  const queryClient = useQueryClient();
  const { data: drivesData } = useQuery({ queryKey: ["exportDrives"], queryFn: api.exportDrives });
  const { data: specs } = useQuery({ queryKey: ["exportCodecs"], queryFn: api.exportCodecs });
  const { data: savedDefaults } = useQuery({ queryKey: ["exportDefaults"], queryFn: api.exportDefaults });

  const [form, setForm] = useState<ExportForm | null>(null);
  const [customValue, setCustomValue] = useState("192");
  const [busy, setBusy] = useState(false);

  const f = form ?? savedDefaults ?? BLANK_FORM;
  const set = <K extends keyof ExportForm>(key: K, value: ExportForm[K]) =>
    setForm({ ...f, [key]: value });

  const spec = specs?.codecs?.[f.codec];
  const kbps = effectiveKbps(spec, f.quality, customValue);
  const estBytes = kbps !== null && seconds > 0 ? (seconds * kbps * 1000) / 8 : null;
  const drives: ExportDrive[] = drivesData?.drives ?? [];
  const selectedDrive = drives.find((d) => d.root === f.dest) ?? null;
  const overCapacity = estBytes !== null && selectedDrive?.free != null && estBytes > selectedDrive.free;

  /** Runs the export for exactly `paths`. Returns true when a run started and
   *  finished (the caller closes a dialog on it); validation failures only
   *  toast, so the user keeps their place. */
  const run = async (): Promise<boolean> => {
    if (!paths.length) {
      toast("Select something to export first");
      return false;
    }
    const destRoot = drivesData?.drives.find((d) => d.root === f.dest)?.root;
    if (!destRoot) {
      toast("Choose a destination drive");
      return false;
    }
    setBusy(true);
    toast(`Exporting ${paths.length} track(s)…`);
    try {
      const r = await api.exportRun({
        ...f,
        dest: destRoot,
        quality: f.quality === CUSTOM ? customValue : f.quality || spec?.default || "",
        paths,
      });
      const gb = (r.bytes / 1024 ** 3).toFixed(2);
      const extras = [
        r.skipped ? `${r.skipped} already there` : "",
        r.playlists ? `${r.playlists} playlist(s)` : "",
        r.sidecars ? `${r.sidecars} sidecar file(s)` : "",
        r.pruned ? `${r.pruned} removed from the device` : "",
      ].filter(Boolean).join(" · ");
      if (r.failed) toast.error(`Export finished with ${r.failed} failure(s): ${r.errors[0] ?? ""}`);
      else toast.success(`Exported ${r.exported} track(s)${extras ? ` (${extras})` : ""} · ${gb} GB`);
      if (r.warnings?.length) toast(r.warnings[0]);
      return true;
    } catch (e) {
      toast.error(String(e));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const saveDefaults = async () => {
    try {
      await api.exportSaveDefaults(f);
      queryClient.invalidateQueries({ queryKey: ["exportDefaults"] });
      queryClient.invalidateQueries({ queryKey: ["config"] });
      toast.success("Export defaults saved");
    } catch (e) {
      toast.error(String(e));
    }
  };

  const resetDefaults = () => {
    if (savedDefaults) setForm(savedDefaults);
    toast("Form reset to the saved defaults");
  };

  return {
    f, set, customValue, setCustomValue, busy, spec, kbps, estBytes,
    specs, drives, selectedDrive, overCapacity, paths, seconds,
    run, saveDefaults, resetDefaults,
    refreshDrives: () => void queryClient.invalidateQueries({ queryKey: ["exportDrives"] }),
  };
}

/** The whole export option surface — target drive/subfolder, codec + quality,
 *  folder structure, artwork, tag compatibility, ReplayGain, sidecars,
 *  playlists, verification, concurrency and sync mode — plus the run / save /
 *  reset row. The Export page and the per-page dialog both render exactly
 *  this, so the two can never drift apart. */
export function ExportOptionsPanel({ e, hint }: {
  e: ExportOptions;
  /** Prepended above the options: what this particular run will export. */
  hint?: ReactNode;
}) {
  const { f, set, spec, kbps, estBytes, seconds, paths, busy } = e;
  return (
    <div className="min-w-0">
      {hint && <div className="text-[11px] text-zinc-500 mb-3">{hint}</div>}

      <div className="flex items-center justify-between mb-2">
        <div className="text-xs font-bold text-zinc-300">Destination</div>
        <button
          className="btn !py-0.5 !px-2 text-[11px] tap"
          onClick={e.refreshDrives}
          title="Rescan the drives (a device plugged in after this opened)"
        >
          <RotateCcw className="h-3 w-3" />
          Rescan
        </button>
      </div>
      <select
        className="input !py-1 text-xs w-full min-w-0 tap"
        value={f.dest}
        onChange={(ev) => set("dest", ev.target.value)}
      >
        <option value="">Choose a drive…</option>
        {(e.drives ?? []).map((d) => (
          <option key={d.root} value={d.root}>
            {d.letter} {d.type !== "fixed" ? `(${d.type})` : ""} — {fmtGB(d.free)} free
          </option>
        ))}
      </select>
      <label className="flex items-center gap-2 mt-2 text-xs text-zinc-300">
        <span className="shrink-0">Subfolder</span>
        <input className="input !py-1 text-xs flex-1 min-w-0 tap" value={f.subfolder} onChange={(ev) => set("subfolder", ev.target.value)} />
      </label>

      <div className="text-[11px] text-zinc-500 mt-2 flex items-center gap-2 flex-wrap">
        <span>
          {paths.length} track{paths.length === 1 ? "" : "s"} selected
          {seconds > 0 ? ` · ${fmtDuration(seconds)}` : ""}
        </span>
        {estBytes !== null && (
          <span className="text-zinc-600">· ~{(estBytes / 1024 ** 3).toFixed(2)} GB after export</span>
        )}
      </div>
      {e.overCapacity && (
        <div className="flex items-start gap-2 mt-2 text-[11px] text-amber-300 border border-amber-500/30 bg-amber-500/10 rounded-md p-2">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
          <span>
            Estimated output (~{(estBytes! / 1024 ** 3).toFixed(2)} GB) may not fit this drive
            ({fmtGB(e.selectedDrive?.free ?? null)} free). Pick fewer tracks or a lower bitrate.
          </span>
        </div>
      )}

      <div className="text-xs font-bold text-zinc-300 mt-4 mb-2">Format</div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        <label className="text-[10px] text-zinc-500 flex flex-col gap-1">
          Codec
          <select
            className="input !py-1 text-xs min-w-0 tap"
            value={f.codec}
            onChange={(ev) => {
              set("codec", ev.target.value);
              set("quality", "");
            }}
          >
            {Object.entries(e.specs?.codecs ?? {}).map(([v, cs]) => (
              <option key={v} value={v}>{cs.label}</option>
            ))}
          </select>
        </label>
        <label className="text-[10px] text-zinc-500 flex flex-col gap-1">
          Quality
          <select
            className="input !py-1 text-xs min-w-0 tap"
            value={f.quality || spec?.default || ""}
            onChange={(ev) => set("quality", ev.target.value)}
            disabled={!spec?.presets.length && !spec?.custom}
          >
            {/* copy has no knobs — a disabled placeholder keeps the box legible */}
            {!spec?.presets.length && !spec?.custom ? (
              <option value="">—</option>
            ) : (
              <>
                {spec.presets.map((q) => (
                  <option key={q.v} value={q.v}>{q.label}</option>
                ))}
                {spec.custom && (
                  <option value={CUSTOM}>
                    {spec.custom.mode === "q" ? "Custom q…" : "Custom bitrate…"}
                  </option>
                )}
              </>
            )}
          </select>
        </label>
      </div>
      {/* custom bitrate / q — the server clamps to the same range again */}
      {spec?.custom && f.quality === CUSTOM && (
        <label className="flex flex-wrap items-center gap-2 mt-2 text-[10px] text-zinc-500">
          {spec.custom.mode === "q"
            ? `Custom q (${spec.custom.min}–${spec.custom.max})`
            : `Custom bitrate (${spec.custom.min}–${spec.custom.max} kbps)`}
          <input
            className="input !py-1 text-xs w-24 min-w-0 tap"
            type="number"
            min={spec.custom.min}
            max={spec.custom.max}
            value={e.customValue}
            onChange={(ev) => e.setCustomValue(ev.target.value)}
          />
          {kbps !== null && <span className="text-zinc-600">~{kbps} kbps effective</span>}
        </label>
      )}
      <label className="text-[10px] text-zinc-500 flex flex-col gap-1 mt-2">
        Folder structure
        <select
          className="input !py-1 text-xs w-full min-w-0 tap"
          value={f.structure}
          onChange={(ev) => set("structure", ev.target.value)}
        >
          {STRUCTURES.map((st) => (
            <option key={st.v} value={st.v}>{st.label}</option>
          ))}
        </select>
      </label>
      <div className="text-[10px] text-zinc-600 mt-1">
        A multi-disc album gets a &quot;1-01 - Title&quot; file name, so the two discs
        cannot collide.
      </div>

      {/* ---- artwork, tags, extras -------------------------------- */}
      <div className="text-xs font-bold text-zinc-300 mt-4 mb-2">Artwork &amp; tags</div>
      <Opt
        checked={f.embed_covers}
        onChange={(v) => set("embed_covers", v)}
        label="Embed cover art into the exported files"
        hint="The album's cover.* (or the file's own art when the folder has none) is embedded, re-encoded at the quality below. Off leaves art exactly as the source had it."
      />
      {f.embed_covers && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 mt-1 pl-6">
          <label className="text-[10px] text-zinc-500 flex flex-col gap-1">
            Embedded JPEG quality — {f.embed_cover_jpeg_quality}
            <input
              type="range"
              min={60}
              max={100}
              value={f.embed_cover_jpeg_quality}
              onChange={(ev) => set("embed_cover_jpeg_quality", Number(ev.target.value))}
            />
          </label>
          <label className="text-[10px] text-zinc-500 flex flex-col gap-1">
            Max resolution (px, 0 = original)
            <input
              className="input !py-1 text-xs min-w-0 tap"
              type="number"
              min={0}
              max={4000}
              value={f.embed_cover_resolution}
              onChange={(ev) => set("embed_cover_resolution", Number(ev.target.value))}
            />
          </label>
        </div>
      )}
      <Opt
        checked={f.clean_tags}
        onChange={(v) => set("clean_tags", v)}
        label="Write only the canonical tag set"
        hint="Transcodes drop the source's leftover frames instead of carrying them along beside the tags this app writes."
      />
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 mt-2 items-end">
        <label className="text-[10px] text-zinc-500 flex flex-col gap-1">
          ID3 version (MP3)
          <select
            className="input !py-1 text-xs min-w-0 tap"
            value={f.id3v2}
            onChange={(ev) => set("id3v2", ev.target.value)}
          >
            <option value="2.3">2.3 — older players, car stereos</option>
            <option value="2.4">2.4 — newest frames</option>
          </select>
        </label>
        <Opt
          checked={f.id3v1}
          onChange={(v) => set("id3v1", v)}
          label="Also write ID3v1"
          hint="For players that read nothing else (short, latin-1 fields)."
        />
      </div>
      <Opt
        checked={f.replaygain}
        onChange={(v) => set("replaygain", v)}
        label="Write ReplayGain tags"
        hint="Measures each track (ffmpeg EBU R128, one pass that rides along with the transcode) and stores track + album gain/peak, so the player matches your library's loudness."
      />
      <Opt
        checked={f.playlists}
        onChange={(v) => set("playlists", v)}
        label="Write .m3u8 playlists"
        hint="One per exported album, plus all.m3u8 for the whole export — UTF-8 with relative paths and durations."
      />
      <Opt
        checked={f.sidecars}
        onChange={(v) => set("sidecars", v)}
        label="Copy covers, lyrics, cue, log and descriptions"
        hint="cover.*, description.txt, .lrc, .cue, .log and the artist image travel with the tracks."
      />
      <Opt
        checked={f.verify}
        onChange={(v) => set("verify", v)}
        label="Verify every written file"
        hint="Re-opens each export and proves it parses with the source's duration before reporting success."
      />
      <label className="flex flex-wrap items-center gap-2 mt-2 text-xs text-zinc-300">
        <span className="shrink-0">Parallel workers</span>
        <select
          className="input !py-1 text-xs min-w-0 tap"
          value={f.workers}
          onChange={(ev) => set("workers", Number(ev.target.value))}
        >
          <option value={0}>Auto (half the cores, max 8)</option>
          {[1, 2, 3, 4, 6, 8, 12, 16].map((n) => (
            <option key={n} value={n}>{n}</option>
          ))}
        </select>
      </label>
      <Opt
        checked={f.prune}
        onChange={(v) => set("prune", v)}
        danger
        label="Sync mode — remove audio the export does not write"
        hint="Deletes audio files under the export folder that this run did not produce. Meant for mirroring a player: leave it off unless you want the destination to match this selection exactly."
      />

      <div className="grid grid-cols-2 sm:grid-cols-[2fr_1fr_auto] gap-2 mt-4">
        <button className="btn-primary text-xs col-span-2 sm:col-span-1 tap" disabled={busy || !paths.length} onClick={e.run}>
          <HardDriveDownload className="h-3.5 w-3.5" />
          {busy ? "Exporting…" : `Export ${paths.length || ""} track${paths.length === 1 ? "" : "s"}`}
        </button>
        <button className="btn text-xs tap" disabled={busy} onClick={e.saveDefaults} title="Save these choices as the defaults for the next export">
          <Save className="h-3.5 w-3.5" />
          Save as default
        </button>
        <button className="btn text-xs !px-2 tap" disabled={busy} onClick={e.resetDefaults} title="Reload the saved defaults">
          <RotateCcw className="h-3.5 w-3.5" />
        </button>
      </div>
      <div className="text-[10px] text-zinc-600 mt-2">
        {f.codec === "copy"
          ? "Copy keeps the original files bit-exact (an embed-cover pass still rewrites tags when art must change)."
          : f.codec === "flac"
            ? "FLAC → FLAC exports are bit-copies; anything else is re-encoded with ffmpeg and fully re-tagged."
            : f.codec === "wav" || f.codec === "aiff"
              ? `${spec?.label ?? f.codec} carries no tag set this app can write — the export keeps the audio only.`
              : `Exporting as ${spec?.label ?? f.codec} — files are re-encoded with ffmpeg and fully re-tagged.`}
      </div>
    </div>
  );
}

/** The same option surface, in a dialog, for a page's own selection. Mounted
 *  only while open, so a page pays for the drives/codecs/defaults queries only
 *  when the user actually asks to export. */
export default function ExportDialog({ onClose, paths, seconds = 0, subtitle }: {
  onClose: () => void;
  /** Exactly what this page's Export action covers — the artist's tracks, the
   *  album, the one track, the playlist's tracks… */
  paths: string[];
  /** The selection's total duration, for the drive-fit estimate (0 = skip). */
  seconds?: number;
  subtitle?: ReactNode;
}) {
  const e = useExportOptions(paths, seconds);
  // The dialog closes itself once the run actually landed; the toast reports
  // the outcome either way.
  const onRun = async () => {
    const ok = await e.run();
    if (ok) onClose();
    return ok;
  };
  return (
    <Modal
      title="Export to device"
      subtitle={subtitle ?? `${paths.length} track${paths.length === 1 ? "" : "s"} from this page`}
      icon={HardDriveDownload}
      onClose={onClose}
      width="max-w-3xl"
      bodyClass="px-5 py-4"
    >
      <ExportOptionsPanel e={{ ...e, run: onRun }} />
    </Modal>
  );
}

/** The per-page Export action: the same icon/ghost button styles the pages'
 *  other header actions use, opening the shared dialog. Disabled — with the
 *  reason in its tooltip — when the page has nothing to export. */
export function ExportButton({
  paths,
  seconds = 0,
  label = "Export",
  size = "sm",
  iconOnly = false,
  emptyReason = "Nothing to export — this page has no tracks",
  title,
  dialogSubtitle,
}: {
  paths: string[];
  seconds?: number;
  label?: string;
  size?: "sm" | "md";
  iconOnly?: boolean;
  emptyReason?: string;
  title?: string;
  dialogSubtitle?: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const disabled = paths.length === 0;
  const cls = iconOnly
    ? "btn-icon"
    : size === "md"
      ? "btn-ghost"
      : "btn-ghost !py-1.5 text-xs";
  const iconCls = iconOnly || size === "md" ? "h-4 w-4" : "h-3.5 w-3.5";
  const count = `${paths.length} track${paths.length === 1 ? "" : "s"}`;
  const tip = disabled ? emptyReason : `${title ?? "Export to a drive"} (${count})`;
  return (
    <>
      <button
        className={cls}
        disabled={disabled}
        onClick={() => setOpen(true)}
        title={tip}
        aria-label={iconOnly || disabled ? tip : undefined}
      >
        <FileOutput className={iconCls} />
        {!iconOnly && ` ${label}`}
      </button>
      {open && (
        <ExportDialog
          onClose={() => setOpen(false)}
          paths={paths}
          seconds={seconds}
          subtitle={dialogSubtitle}
        />
      )}
    </>
  );
}
