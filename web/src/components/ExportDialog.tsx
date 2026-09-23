import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Download, FileOutput, HardDrive, HardDriveDownload, RotateCcw, Save, Trash2, Upload } from "lucide-react";
import { api, IN_TAURI } from "../api";
import type { ExportCodecSpec, ExportEq, ExportFamily, ExportForm, ExportStructurePreview, ExportStructures } from "../api";
import { toast } from "../store";
import { fmtBytes, fmtDuration } from "../lib/fmt";
import Segmented from "./Segmented";
import Modal from "./Modal";
import ConfirmButton from "./ConfirmButton";
import EqProfileModal from "./EqProfileModal";

/** The dropdown's synthetic entry for a codec's "custom value" field; the
 * backend takes the plain number the field holds (kbps, or 0-10 for Vorbis),
 * so nothing but the form needs to know the word. */
export const CUSTOM = "custom";

/** The dropdown value that means "the structure the user typed" in
 *  `structure_script` (server.exporter.CUSTOM_STRUCTURE). */
export const CUSTOM_STRUCTURE = "custom";

/** The two places an export can go. A browser cannot write to the server's
 *  filesystem, so "download a .zip" is the mode that works everywhere; a server
 *  folder is for a machine whose drives the user can actually see — a
 *  self-hosted box, or the desktop shell, which runs beside the server. */
export const DESTINATIONS = [
  { id: "zip", label: "Download a .zip", icon: Download },
  { id: "server", label: "Server folder", icon: HardDrive },
] as const;

/** The mode a form value resolves to. An empty saved value means "whichever
 *  one this client can use": a browser has no server filesystem to write to,
 *  so it downloads the archive; the desktop shell runs on the server's own
 *  machine and writes to a folder. */
export function resolveTarget(value: string): "server" | "zip" {
  if (value === "zip" || value === "server") return value;
  return IN_TAURI ? "server" : "zip";
}

/** Rendered while the saved defaults are still loading; the same shape and the
 * same first-run values the backend ships (server.exporter.EXPORT_DEFAULTS).
 * `target` is left at the server's own default; `resolveTarget` turns a value
 * the client cannot use into the one it can. */
export const BLANK_FORM: ExportForm = {
  target: "server",
  dest: "",
  subfolder: "Music",
  codec: "copy",
  quality: "",
  structure: "albumartist_album_disc",
  structure_script: "",
  embed_covers: true,
  embed_cover_jpeg_quality: 90,
  embed_cover_resolution: 1200,
  id3v2: "2.3",
  id3v1: false,
  replaygain_mode: "off",
  /* "embedded" is the library's own shipped `lyrics_format` (mlo/config.py),
     which is what a saved export default resolves to until the user picks. */
  lyrics: "embedded",
  eq_profile: "",
  clean_tags: true,
  playlists: false,
  /* WHICH files a run writes (server.exporter.FILE_FAMILIES): the tracks
     alone, which is what an export has always written — no .m3u8, no
     cover.jpg, no rip evidence (the cover travels EMBEDDED). The dialog's
     checkbox group below is this list; /api/export/defaults serves the
     resolved one, so a config that still holds only `export_sidecars` opens
     showing the set that switch stands for. */
  copy_files: ["audio"],
  /* The switch `copy_files` replaced: the run still honours it when no file
     selection is saved, and a saved config from before this field exists
     carries it. The form itself no longer writes it. */
  sidecars: false,
  manifest: false,
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
  /** Writes several fields in ONE update. Two `set` calls in the same handler
   *  cannot both land when each derives the next form from the render it was
   *  created in, so a handler that changes two fields uses this. */
  setMany: (patch: Partial<ExportForm>) => void;
  customValue: string;
  setCustomValue: (v: string) => void;
  busy: boolean;
  specs: ExportCodecs | undefined;
  spec: ExportCodecSpec | undefined;
  /** The folder-structure menu and the grammar a custom structure is written
   *  in, as the server publishes them (server/exporter.structure_menu). */
  structures: ExportStructures | undefined;
  /** The file families a run can be asked to copy — the exporter's own table
   *  (GET /api/export/files, server.exporter.FILE_FAMILIES), which is what the
   *  "What gets copied" checkboxes render. */
  fileFamilies: ExportFamily[];
  /** Effective kbps of the chosen codec+quality, or null when unpredictable. */
  kbps: number | null;
  /** Predicted output size, or null when `seconds` is 0 or kbps unknown. */
  estBytes: number | null;
  drives: ExportDrive[];
  selectedDrive: ExportDrive | null;
  overCapacity: boolean;
  /** The mode this run will use, with a saved `target` this client cannot
   *  honour already resolved to the one it can (see `resolveTarget`). */
  target: "server" | "zip";
  /** The server's equalizer presets and imported profiles. */
  eq: ExportEq | undefined;
  /** The source tab this surface is on and its setter — the Export page's own
   *  state, so a saved config can carry the tab it was saved from and put it
   *  back. The per-page export dialog has no tabs (its selection is the page it
   *  was opened from), so it supplies neither and saves the form alone. */
  sourceKind?: string;
  setSourceKind?: (v: string) => void;
  /** The selection this form would export. */
  paths: string[];
  seconds: number;
  /** True when a run started and finished; false when validation refused it. */
  run: () => Promise<boolean>;
  saveDefaults: () => Promise<void>;
  resetDefaults: () => void;
  refreshDrives: () => void;
}

/** What a surface with source tabs hands `useExportOptions` — see
 *  `ExportOptions.sourceKind`. */
export interface ExportPageFields {
  sourceKind: string;
  setSourceKind: (v: string) => void;
}

/** The export form's state and its run/save/reset actions, shared verbatim by
 *  the Export page and the per-page dialog: the user's edits override the
 *  saved `export_*` config values, which override the shipped defaults. An
 *  export sends exactly what is displayed; nothing is written back to config
 *  until "Save as default" is pressed. `seconds` is the selection's total
 *  duration (0 when unknown) and only feeds the drive-fit estimate.
 *
 *  `page` is the source tab of the surface that has one, so a saved config can
 *  remember it; nothing else about a page's own state is the form's business. */
export function useExportOptions(paths: string[], seconds = 0, page?: ExportPageFields): ExportOptions {
  const queryClient = useQueryClient();
  const { data: drivesData } = useQuery({ queryKey: ["exportDrives"], queryFn: api.exportDrives });
  const { data: specs } = useQuery({ queryKey: ["exportCodecs"], queryFn: api.exportCodecs });
  const { data: savedDefaults } = useQuery({ queryKey: ["exportDefaults"], queryFn: api.exportDefaults });
  const { data: structures } = useQuery({ queryKey: ["exportStructures"], queryFn: api.exportStructures });
  const { data: fileMenu } = useQuery({ queryKey: ["exportFiles"], queryFn: api.exportFileFamilies });
  const { data: eq } = useQuery({ queryKey: ["exportEq"], queryFn: api.exportEq });

  const [form, setForm] = useState<ExportForm | null>(null);
  const [customValue, setCustomValue] = useState("192");
  const [busy, setBusy] = useState(false);

  const f = form ?? savedDefaults ?? BLANK_FORM;
  /* Both writers take a FUNCTIONAL update: they derive the next form from what
   * the state actually holds, never from the form this render closed over.
   * The old shape — `setForm({ ...f, [key]: value })` — silently dropped all
   * but the last write of any handler that wrote twice, and one does: picking
   * a codec writes the codec and resets the quality, so the quality reset was
   * applied to the UNCHANGED codec and the select could never leave "copy" —
   * taking the Quality select (disabled for `copy`, which has no knobs) down
   * with it. */
  const set = <K extends keyof ExportForm>(key: K, value: ExportForm[K]) =>
    setForm((prev) => ({ ...(prev ?? savedDefaults ?? BLANK_FORM), [key]: value }));
  const setMany = (fields: Partial<ExportForm>) =>
    setForm((prev) => ({ ...(prev ?? savedDefaults ?? BLANK_FORM), ...fields }));

  const target = resolveTarget(f.target);
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
    // The drive is only ours to require in server mode: a zip never touches a
    // drive, and requiring one would block the run on a picker that mode hides.
    let dest = "";
    if (target === "server") {
      const destRoot = drivesData?.drives.find((d) => d.root === f.dest)?.root;
      if (!destRoot) {
        toast("Choose a destination drive");
        return false;
      }
      dest = destRoot;
    }
    setBusy(true);
    toast(target === "zip"
      ? `Building a .zip of ${paths.length} track(s)…`
      : `Exporting ${paths.length} track(s)…`);
    try {
      const r = await api.exportRun({
        ...f,
        target,
        dest,
        quality: f.quality === CUSTOM ? customValue : f.quality || spec?.default || "",
        paths,
      });
      const gb = (r.bytes / 1024 ** 3).toFixed(2);
      const extras = [
        r.skipped ? `${r.skipped} already there` : "",
        r.playlists ? `${r.playlists} playlist(s)` : "",
        r.sidecars ? `${r.sidecars} file(s) beside the audio` : "",
        r.excluded_total ? `${r.excluded_total} file(s) left behind` : "",
        r.pruned ? `${r.pruned} removed from the device` : "",
      ].filter(Boolean).join(" · ");
      if (r.failed) toast.error(`Export finished with ${r.failed} failure(s): ${r.errors[0] ?? ""}`);
      else toast.success(`Exported ${r.exported} track(s)${extras ? ` (${extras})` : ""} · ${gb} GB`);
      if (r.warnings?.length) toast(r.warnings[0]);
      // A zip run leaves nothing where the user can find it, so the finished
      // archive is handed to the browser here — the same `<a download>` idiom
      // the per-track export uses — and reported with its own size and count.
      if (r.zip) {
        const a = document.createElement("a");
        a.href = api.exportZipUrl(r.zip.url);
        a.download = r.zip.name;
        document.body.appendChild(a);
        a.click();
        a.remove();
        toast(`${r.zip.name} · ${r.zip.files} file(s) · ${fmtBytes(r.zip.bytes)} — your browser is saving it to its downloads`);
      }
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
    f, set, setMany, customValue, setCustomValue, busy, spec, kbps, estBytes,
    specs, structures, fileFamilies: fileMenu?.families ?? [],
    drives, selectedDrive, overCapacity, target, eq, paths,
    seconds, run, saveDefaults, resetDefaults,
    sourceKind: page?.sourceKind,
    setSourceKind: page?.setSourceKind,
    refreshDrives: () => void queryClient.invalidateQueries({ queryKey: ["exportDrives"] }),
  };
}

/** The whole export option surface — where the files go, codec + quality,
 *  folder structure, artwork, tag compatibility, audio processing (ReplayGain
 *  and the equalizer), WHICH files a run copies (the file-family checkboxes,
 *  server.exporter.FILE_FAMILIES), the files written beside the audio
 *  (playlists, a checksum manifest), verification, concurrency and sync mode —
 *  plus the
 *  run / save / reset row. The Export page and the per-page dialog both render
 *  exactly this, so the two can never drift apart.
 *
 *  The destination is a CHOICE, and each mode hides what does not exist in it
 *  rather than disabling it: a zip never touches a drive, so the drive picker,
 *  the subfolder and the sync switch are not drawn at all — a disabled control
 *  still reads as an option. */
export function ExportOptionsPanel({ e, hint }: {
  e: ExportOptions;
  /** Prepended above the options: what this particular run will export. */
  hint?: ReactNode;
}) {
  const queryClient = useQueryClient();
  const { f, set, setMany, spec, kbps, estBytes, seconds, paths, busy, target, eq } = e;
  const zip = target === "zip";
  const eqSelected = [...(eq?.presets ?? []), ...(eq?.profiles ?? [])].find((p) => p.id === f.eq_profile);
  /* The id the form carries but the catalogue does not: a profile that was
   * deleted or renamed after the form (or a saved config) named it. Nothing
   * here falls back to another profile — the id stays, is shown as missing, and
   * the run refuses it. */
  const missingEq = !!f.eq_profile && !eqSelected;
  const importedEq = (eq?.profiles ?? []).some((p) => p.id === f.eq_profile);
  const [importing, setImporting] = useState(false);
  const removeProfile = async () => {
    if (!f.eq_profile) return;
    try {
      await api.exportEqDelete(f.eq_profile);
      // The removal was deliberate, so the form lets go of the id too; a SAVED
      // config that still names it will say the profile is gone when it loads.
      set("eq_profile", "");
      toast(`Removed the equalizer profile “${f.eq_profile}”`);
      void queryClient.invalidateQueries({ queryKey: ["exportEq"] });
      // ...and the saved configs, whose rows carry whether the profile they
      // name is still there: a cached list would keep saying "fine" about a
      // config that has just lost its curve.
      void queryClient.invalidateQueries({ queryKey: ["exportConfigs"] });
    } catch (err) {
      toast.error(String(err));
    }
  };
  /* The user's own structure is checked by the SERVER while it is typed — the
   * same validator a run refuses with, so the sentence under the box is the
   * one an export would give. Debounced: one request per pause instead of one
   * per keystroke. */
  const custom = f.structure === CUSTOM_STRUCTURE;
  const script = f.structure_script;
  const ext = spec?.ext ?? "";
  const [customPreview, setCustomPreview] = useState<ExportStructurePreview | null>(null);
  useEffect(() => {
    if (!custom) return;
    let live = true;
    const timer = setTimeout(() => {
      api.exportStructurePreview(script, ext)
        .then((r) => { if (live) setCustomPreview(r); })
        .catch((err) => { if (live) setCustomPreview({ ok: false, path: "", error: String(err) }); });
    }, 350);
    return () => { live = false; clearTimeout(timer); };
  }, [custom, script, ext]);

  /* Tick/untick one file family, keeping the form's list in the SERVER's own
   * table order — the order the checkboxes are drawn in — so a saved config
   * and a re-opened form read the way the menu does. */
  const toggleFamily = (key: string, on: boolean) => {
    const chosen = new Set(f.copy_files);
    if (on) chosen.add(key);
    else chosen.delete(key);
    set("copy_files", e.fileFamilies.map((fam) => fam.v).filter((v) => chosen.has(v)));
  };

  return (
    <div className="min-w-0">
      {hint && <div className="text-[11px] text-zinc-500 mb-3">{hint}</div>}

      <div className="text-xs font-bold text-zinc-300 mb-2">Destination</div>
      <Segmented value={target} onChange={(v) => set("target", v)} options={DESTINATIONS} className="mb-2" />
      <div className="text-[11px] text-zinc-500 mb-2">
        {zip
          ? "The server builds a .zip and this browser saves it to its downloads folder. Nothing lands on the server, so there is no drive or subfolder to choose."
          : "Written to a drive this machine can see; a re-run skips what is already there."}
      </div>

      {!zip && (
        <>
          <div className="flex items-center gap-2">
            <select
              className="input !py-1 text-xs flex-1 min-w-0 tap"
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
            <button
              className="btn !py-1 !px-2 tap"
              onClick={e.refreshDrives}
              title="Rescan the drives (a device plugged in after this opened)"
            >
              <RotateCcw className="h-3.5 w-3.5" />
              <span className="sr-only">Rescan drives</span>
            </button>
          </div>
          <label className="flex items-center gap-2 mt-2 text-xs text-zinc-300">
            <span className="shrink-0">Subfolder</span>
            {/* Capped: this holds a folder name, and on the Export page's wide
                column a `flex-1` box would be 600 px of empty field beside a
                two-word label. `max-w-xs` is the app's own cap for a short
                input in a wide column (same as the Settings-family inputs).
                Long values stay readable by scrolling inside the box; the
                drive above keeps its full width because drive labels are long. */}
            <input className="input !py-1 text-xs flex-1 min-w-0 max-w-xs tap" value={f.subfolder} onChange={(ev) => set("subfolder", ev.target.value)} />
          </label>
        </>
      )}

      <div className="text-[11px] text-zinc-500 mt-2 flex items-center gap-2 flex-wrap">
        <span>
          {paths.length} track{paths.length === 1 ? "" : "s"} selected
          {seconds > 0 ? ` · ${fmtDuration(seconds)}` : ""}
        </span>
        {estBytes !== null && (
          <span className="text-zinc-600">
            · ~{(estBytes / 1024 ** 3).toFixed(2)} GB {zip ? "in the archive" : "after export"}
          </span>
        )}
      </div>
      {!zip && e.overCapacity && (
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
            /* ONE write, not two: the codec and the quality reset it triggers
               have to land together, or the reset is applied to the codec this
               render still held and the choice is lost. */
            onChange={(ev) => setMany({ codec: ev.target.value, quality: "" })}
          >
            {!e.specs && <option value={f.codec}>Loading codecs…</option>}
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
            /* Disabled for a codec with no knobs (copy) AND while the codec
               table is still on its way: an enabled box with nothing in it
               reads as a control that does not work. */
            disabled={!spec || (!spec.presets.length && !spec.custom)}
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
          {/* The server's own menu (exporter.structure_menu), NOT a list of
              this page's own: a structure a run would refuse must not be on
              offer, and its labels are where the shipped file name is spelled
              out. Until it lands the form's own value stays selected. */}
          {!e.structures && <option value={f.structure}>{f.structure || "Loading…"}</option>}
          {(e.structures?.structures ?? []).map((st) => (
            <option key={st.v} value={st.v}>{st.label}</option>
          ))}
        </select>
      </label>
      {f.structure === CUSTOM_STRUCTURE ? (
        <>
          <label className="text-[10px] text-zinc-500 flex flex-col gap-1 mt-2">
            Custom structure
            <input
              className="input font-mono !py-1 text-xs min-w-0 tap"
              value={f.structure_script}
              spellCheck={false}
              placeholder="%albumartist%/%album%/%discnumber%-$num(%tracknumber%,2) %title%"
              onChange={(ev) => set("structure_script", ev.target.value)}
            />
          </label>
          {/* The server validates it — one grammar, one validator, so what the
              page shows here is what a run would say. */}
          <div className="text-[10px] mt-1 break-all">
            {customPreview === null ? (
              <span className="text-zinc-600">Checking…</span>
            ) : customPreview.ok ? (
              <span className="text-zinc-600">
                A sample track goes to <code className="text-zinc-400">{customPreview.path}</code>
              </span>
            ) : (
              <span className="text-amber-300">{customPreview.error}</span>
            )}
          </div>
          <div className="text-[10px] text-zinc-600 mt-1 leading-relaxed">
            Fields: <code>{(e.structures?.fields ?? []).map((x) => `%${x}%`).join(" ")}</code>
            <br />
            Functions: <code>{(e.structures?.functions ?? []).map((x) => `$${x}()`).join(" ")}</code>
            {" "}· <code>/</code> creates folders, <code>$if(%field%,then,else)</code> drops a level
            a tag cannot fill.
          </div>
        </>
      ) : (
        <div className="text-[10px] text-zinc-600 mt-1">
          The shipped structure writes the library&apos;s own file name —{" "}
          <code>1-01 Title</code>, the disc number included, so a two-disc album
          cannot collide — under the album artist and the album. &quot;Custom&quot;
          takes your own tag fields instead.
        </div>
      )}

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
      {/* The library's own three-way choice (mlo.lyrics' `lyrics_format`), in
          the export's words: what travels INSIDE the file, beside it, or both.
          The default follows the library, so an export writes lyrics the way
          the app keeps them until the user says otherwise. */}
      <label className="text-[10px] text-zinc-500 flex flex-col gap-1 mt-2 max-w-xs">
        Lyrics
        <select
          className="input !py-1 text-xs min-w-0 tap"
          value={f.lyrics}
          onChange={(ev) => set("lyrics", ev.target.value)}
        >
          <option value="embedded">Embedded in the file</option>
          <option value="lrc">.lrc files beside the audio</option>
          <option value="both">Both</option>
        </select>
      </label>

      {/* ---- audio processing ------------------------------------- */}
      {/* The section the applied-audio work lives in: what a run does to the
          SOUND, as opposed to what it writes beside it. */}
      <div className="text-xs font-bold text-zinc-300 mt-4 mb-2">Audio processing</div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        <label className="text-[10px] text-zinc-500 flex flex-col gap-1">
          ReplayGain
          <select
            className="input !py-1 text-xs min-w-0 tap"
            value={f.replaygain_mode}
            onChange={(ev) => set("replaygain_mode", ev.target.value)}
          >
            <option value="off">Off</option>
            <option value="tags">Write tags — the player applies them</option>
            <option value="apply">Apply to the audio — permanent</option>
          </select>
        </label>
        {/* A div, not a label: this cell holds THREE controls (the profile, the
            import, and the removal of an imported one), and a label wrapping a
            button would hand its clicks to the select. */}
        <div className="text-[10px] text-zinc-500 flex flex-col gap-1">
          <span id="eq-profile-label">Equalizer profile</span>
          <select
            className="input !py-1 text-xs min-w-0 tap"
            aria-labelledby="eq-profile-label"
            value={f.eq_profile}
            onChange={(ev) => set("eq_profile", ev.target.value)}
            disabled={!eq || (!eq.presets.length && !eq.profiles.length)}
          >
            <option value="">No equalizer</option>
            {/* A saved config (or a saved default) can name a profile that has
                since been deleted or renamed. The select SHOWS that id, marked
                as missing, instead of quietly falling back to the first option:
                the run refuses a profile it cannot find, and the user has to
                see why before they run it. */}
            {missingEq && (
              <option value={f.eq_profile}>{f.eq_profile} — missing (no such profile)</option>
            )}
            {(eq?.presets ?? []).map((pr) => (
              <option key={pr.id} value={pr.id}>{pr.label}</option>
            ))}
            {(eq?.profiles ?? []).map((pr) => (
              <option key={pr.id} value={pr.id}>
                {pr.label}{pr.errors?.length ? " — unreadable line(s)" : ""}
              </option>
            ))}
          </select>
          <div className="flex flex-wrap items-center gap-1">
            {/* The app imports these from Peace / Equalizer APO text — paste it
                or drop the file — and the profile then sits in this very list.
                Without this the import endpoint had no way in from the UI. */}
            <button
              className="btn-ghost !py-0.5 text-[10px] tap"
              disabled={busy}
              onClick={() => setImporting(true)}
            >
              <Upload className="h-3 w-3" /> Import a profile…
            </button>
            {importedEq && (
              <ConfirmButton
                className="btn-ghost !py-0.5 text-[10px] tap"
                confirmLabel="Remove this profile?"
                disabled={busy}
                onConfirm={removeProfile}
              >
                <Trash2 className="h-3 w-3" /> Remove
              </ConfirmButton>
            )}
          </div>
        </div>
      </div>
      <div className="text-[10px] text-zinc-600 mt-1">
        {f.replaygain_mode === "apply"
          ? "Measures each track (ffmpeg EBU R128, one pass that rides along with the transcode) and writes the correction into the exported audio, so the levels match on every player — not just the ones that read ReplayGain tags."
          : f.replaygain_mode === "tags"
            ? "Measures each track (ffmpeg EBU R128, one pass that rides along with the transcode) and stores track + album gain/peak, so a player that reads ReplayGain matches your library's loudness. The audio itself is untouched."
            : "No ReplayGain measurement or tags."}
        {f.eq_profile && " The equalizer is applied to the exported audio."}
        {missingEq
          ? " This equalizer profile is not on this server any more — pick another profile or “No equalizer”; an export that names it is refused rather than exporting a different curve."
          : eqSelected?.errors?.length
            ? ` This profile has ${eqSelected.errors.length} band line(s) this server cannot read — it is refused at import and by a run, rather than applying a different curve.`
            : eqSelected?.empty && importedEq
              ? " This profile has no filters in it — the export leaves the audio as it is, which is not the same as a flat curve."
              : ""}
        {!missingEq && eqSelected?.unsupported?.length
          ? ` ${eqSelected.unsupported.length} line(s) of this profile have no equivalent here and are skipped.`
          : ""}
      </div>

      {/* ---- what gets copied ------------------------------------- */}
      {/* WHICH files a run writes, ticked one by one. The list IS the server's
          own table (server.exporter.FILE_FAMILIES, GET /api/export/files): the
          checkboxes, the sentence a refused selection comes back with and the
          engine's own copy pass are one vocabulary, so a tick the run would
          ignore cannot be drawn, and what the run reports as left behind is
          exactly what was not ticked. */}
      <div className="text-xs font-bold text-zinc-300 mt-4 mb-2">What gets copied</div>
      <div className="text-[11px] text-zinc-500 mb-2">
        Everything an export writes, family by family. A family you leave
        unticked stays in the library and is named in the run report — never
        dropped in silence.
      </div>
      {!e.fileFamilies.length && (
        <div className="text-[11px] text-zinc-600">Loading the file families…</div>
      )}
      {e.fileFamilies.map((fam) => (
        <Opt
          key={fam.v}
          checked={f.copy_files.includes(fam.v)}
          onChange={(on) => toggleFamily(fam.v, on)}
          label={fam.label}
          hint={fam.hint}
        />
      ))}
      {!f.copy_files.length && (
        <div className="flex items-start gap-2 mt-2 text-[11px] text-amber-300 border border-amber-500/30 bg-amber-500/10 rounded-md p-2">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
          <span>
            Nothing is ticked, so there would be no files to copy. An export
            with an empty selection is refused rather than writing an empty
            folder — tick at least one kind of file.
          </span>
        </div>
      )}

      {/* ---- files written beside the audio ----------------------- */}
      <div className="text-xs font-bold text-zinc-300 mt-4 mb-2">Files written beside the audio</div>
      <Opt
        checked={f.playlists}
        onChange={(v) => set("playlists", v)}
        label="Write .m3u8 playlists"
        hint="One per exported album, plus all.m3u8 for the whole export — UTF-8 with relative paths and durations."
      />
      <Opt
        checked={f.manifest}
        onChange={(v) => set("manifest", v)}
        label="Write a checksum manifest (checksums.sha256)"
        hint="One line per exported file — its SHA-256 and its path — at the root of the export, in the format `sha256sum -c` reads, so the device's copy can be verified later. Costs one read of everything the run just wrote."
      />
      <Opt
        checked={f.verify}
        onChange={(v) => set("verify", v)}
        label="Verify every written file"
        hint="Re-opens each export and proves it parses with the source's duration before reporting success."
      />
      <label className="flex flex-wrap items-center gap-2 mt-2 text-xs text-zinc-300">
        <span className="shrink-0">Parallel workers</span>
        {/* A `.input` in a wrapping flex row is `w-full`, so on the Export
            page's wide column this one select claimed the whole line for a
            choice whose longest option is "Auto (half the cores, max 8)". */}
        <select
          className="input !py-1 text-xs min-w-0 max-w-[16rem] tap"
          value={f.workers}
          onChange={(ev) => set("workers", Number(ev.target.value))}
        >
          <option value={0}>Auto (half the cores, max 8)</option>
          {[1, 2, 3, 4, 6, 8, 12, 16].map((n) => (
            <option key={n} value={n}>{n}</option>
          ))}
        </select>
      </label>

      {/* Sync deletes from a DRIVE this run wrote to. A zip run has no drive,
          so the switch is not drawn there at all. */}
      {!zip && (
        <>
          <div className="text-xs font-bold text-zinc-300 mt-4 mb-2">Sync</div>
          <Opt
            checked={f.prune}
            onChange={(v) => set("prune", v)}
            danger
            label="Sync mode — remove audio the export does not write"
            hint="Deletes audio files under the export folder that this run did not produce. Meant for mirroring a player: leave it off unless you want the destination to match this selection exactly."
          />
        </>
      )}

      <SavedConfigs e={e} />

      <div className="grid grid-cols-2 sm:grid-cols-[2fr_1fr_auto] gap-2 mt-4">
        <button className="btn-primary text-xs col-span-2 sm:col-span-1 tap" disabled={busy || !paths.length} onClick={e.run}>
          <HardDriveDownload className="h-3.5 w-3.5" />
          {busy
            ? "Exporting…"
            : `${zip ? "Export & download" : "Export"} ${paths.length || ""} track${paths.length === 1 ? "" : "s"}`}
        </button>
        <button className="btn text-xs tap" disabled={busy} onClick={e.saveDefaults} title="Save these choices as the defaults for the next export">
          <Save className="h-3.5 w-3.5" />
          Save as default
        </button>
        <button className="btn text-xs !px-2 tap" disabled={busy} onClick={e.resetDefaults} title="Reload the saved defaults">
          <RotateCcw className="h-3.5 w-3.5" />
        </button>
      </div>
      {importing && (
        <EqProfileModal
          onClose={() => setImporting(false)}
          onImported={(id) => {
            set("eq_profile", id);
            void queryClient.invalidateQueries({ queryKey: ["exportConfigs"] });
          }}
        />
      )}
      <div className="text-[10px] text-zinc-600 mt-2">
        {f.codec === "copy"
          ? f.replaygain_mode === "apply" || f.eq_profile
            /* A copy run that also processes the audio is no longer a copy of
               the bytes: saying "bit-exact" there would be a lie about the
               files the user is about to get. */
            ? "Copy keeps the original files bit-exact — except the ones the audio processing above changes: applying ReplayGain or an equalizer re-encodes those so the correction is in the audio."
            : "Copy keeps the original files bit-exact (an embed-cover pass still rewrites tags when art must change)."
          : f.codec === "flac"
            ? "FLAC → FLAC exports are bit-copies; anything else is re-encoded with ffmpeg and fully re-tagged."
            : f.codec === "wav" || f.codec === "aiff"
              ? `${spec?.label ?? f.codec} carries no tag set this app can write — the export keeps the audio only.`
              : `Exporting as ${spec?.label ?? f.codec} — files are re-encoded with ffmpeg and fully re-tagged.`}
      </div>
    </div>
  );
}

/** The saved export configurations: the whole form under a name, so the user
 *  who exports the same way twice does not rebuild it — and, when they pick a
 *  different destination or curve, a second named copy instead of losing the
 *  first.
 *
 *  A config is the form PLUS the source tab it was saved from (`source_kind`,
 *  only for a surface that has tabs) and MINUS the selection: which albums are
 *  ticked is data, not configuration, and a config carrying paths would export
 *  something else the moment the library moved.
 *
 *  Loading says what it could not restore: a config that names an equalizer
 *  profile which has since been deleted or renamed comes back marked, with the
 *  server's own sentence, because the run refuses that profile — quietly
 *  substituting another curve is the one thing neither half may do. */
function SavedConfigs({ e }: { e: ExportOptions }) {
  const queryClient = useQueryClient();
  const { data } = useQuery({ queryKey: ["exportConfigs"], queryFn: api.exportConfigs });
  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const configs = data?.configs ?? [];
  const picked = configs.find((c) => c.id === id) ?? null;

  const load = async (configId: string) => {
    if (!configId) {
      setId("");
      setProblem("");
      return;
    }
    setBusy(true);
    try {
      const row = await api.exportConfigLoad(configId);
      const { source_kind, ...form } = row.config;
      e.setMany(form);
      if (source_kind) e.setSourceKind?.(source_kind);
      setId(row.id);
      setName(row.name);
      setProblem(row.eq_problem);
      if (row.eq_problem) toast.error(row.eq_problem);
      else toast.success(`Loaded “${row.name}”`);
    } catch (err) {
      toast.error(String(err));
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    const target = name.trim();
    if (!target) {
      toast.error("Name this config first");
      return;
    }
    setBusy(true);
    try {
      const row = await api.exportConfigSave(target, {
        ...e.f,
        ...(e.sourceKind ? { source_kind: e.sourceKind } : {}),
      });
      setId(row.id);
      setName(row.name);
      setProblem(row.eq_problem);
      void queryClient.invalidateQueries({ queryKey: ["exportConfigs"] });
      toast.success(row.replaced ? `Updated “${row.name}”` : `Saved “${row.name}”`);
    } catch (err) {
      // The server refuses a value a run would refuse (an unknown codec, a bad
      // custom structure…) with its own sentence: show it, do not swallow it.
      toast.error(String(err));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!picked) return;
    setBusy(true);
    try {
      await api.exportConfigDelete(picked.id);
      toast(`Deleted “${picked.name}”`);
      setId("");
      setName("");
      setProblem("");
      void queryClient.invalidateQueries({ queryKey: ["exportConfigs"] });
    } catch (err) {
      toast.error(String(err));
    } finally {
      setBusy(false);
    }
  };

  const updating = !!picked && picked.name === name.trim();

  return (
    <div className="mt-4">
      <div className="text-xs font-bold text-zinc-300 mb-2">Saved configs</div>
      <div className="flex flex-wrap items-center gap-2">
        <select
          className="input !py-1 text-xs min-w-0 max-w-xs tap"
          value={id}
          disabled={busy}
          onChange={(ev) => void load(ev.target.value)}
        >
          <option value="">Load a saved config…</option>
          {configs.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}{c.eq_problem ? " — equalizer profile missing" : ""}
            </option>
          ))}
        </select>
        <input
          className="input !py-1 text-xs min-w-0 max-w-xs"
          value={name}
          placeholder="Name this config"
          disabled={busy}
          onChange={(ev) => setName(ev.target.value)}
        />
        <button className="btn text-xs tap" disabled={busy || !name.trim()} onClick={save}>
          <Save className="h-3.5 w-3.5" />
          {updating ? "Update" : "Save"}
        </button>
        {picked && (
          <ConfirmButton
            className="btn text-xs tap"
            confirmLabel="Delete this saved config?"
            disabled={busy}
            onConfirm={remove}
          >
            <Trash2 className="h-3.5 w-3.5" /> Delete
          </ConfirmButton>
        )}
      </div>
      <div className="text-[10px] text-zinc-600 mt-1">
        {problem
          ? <span className="text-amber-300">“{picked?.name}” — {problem}</span>
          : "Everything this form carries — destination, codec and quality, folder structure, artwork and tags, ReplayGain, and the equalizer profile by name — under one name. What you have ticked is not part of a config."}
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
