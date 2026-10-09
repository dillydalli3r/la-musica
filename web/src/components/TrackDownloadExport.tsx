import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowDown, ArrowDownToLine, FileOutput, Save } from "lucide-react";
import { api } from "../api";
import { toast } from "../store";
import { CACHED_PATHS_KEY, CACHED_SIZES_KEY, cacheTrack, isTrackCached, uncacheTrack } from "../lib/mediaCache";
import Popover from "./Popover";
import CodecQualitySelect, { CUSTOM } from "./CodecQualitySelect";

/** "Download" caches the track inside the player (offline playback — no
 * file lands in the Downloads folder); "Export" is the real file-saving
 * action: transcode to the chosen codec/quality and save.
 *
 * The codec list and the quality control come straight from the server
 * (`GET /api/export/codecs` → server.exporter.CODECS), the SAME vocabulary the
 * Export page renders and the run accepts — MP3 offers V0…V5 and the CBR
 * rates, FLAC its levels, AAC/Opus their bitrates, and the codecs that take one
 * a custom value. Nothing here mirrors a table of its own.
 *
 * `up` opens the popover above the button — required on the bottom-anchored
 * player bar, where a downward menu is off-screen. */
export default function TrackDownloadExport({ path, title, compact, iconOnly, disabled, up = false }: {
  path: string;
  title?: string;
  compact?: boolean;
  /** Icon-only buttons (for the player bar) — labels live in the tooltips. */
  iconOnly?: boolean;
  /** Nothing loaded — the buttons stay on the bar but inert. */
  disabled?: boolean;
  /** Popover opens upward (player bar) instead of downward. */
  up?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const qc = useQueryClient();
  // cache state for the current track
  const [cached, setCached] = useState(false);
  const [cacheBusy, setCacheBusy] = useState(false);

  /* The codec table and the saved defaults are the server's own: the panel
   * opens on the `export_codec`/`export_quality` the Export page's "Save as
   * default" wrote (read from the app's shared ["config"] cache), and only the
   * user's own edit overrides them. `null` state = "still following the
   * saved default". */
  const { data: specs } = useQuery({ queryKey: ["exportCodecs"], queryFn: api.exportCodecs });
  const { data: cfg } = useQuery({ queryKey: ["config"], queryFn: api.config, staleTime: 5 * 60 * 1000 });
  const table = specs?.codecs;
  const rawCodec = String(cfg?.["export_codec"] ?? "").trim() || "copy";
  const savedCodec = !table ? rawCodec : (rawCodec in table ? rawCodec : "copy");
  const savedQuality = String(cfg?.["export_quality"] ?? "");

  const [codecState, setCodecState] = useState<string | null>(null);
  const [qualityState, setQualityState] = useState<string | null>(null);
  const [custom, setCustom] = useState<string | null>(null);

  const codec = codecState ?? savedCodec;
  const spec = table?.[codec];
  // A codec with knobs starts on its own default preset (the server's table).
  const knobs = !!(spec && (spec.presets.length > 0 || spec.custom));
  const shownQuality = knobs ? (qualityState ?? savedQuality) || spec?.default || "" : "";
  const customValue = custom ?? String(spec?.custom?.default ?? "");
  // What the run actually receives: "custom" resolves to the number, "" to the
  // codec's own default — the same resolution the Export page applies.
  const requestQuality = shownQuality === CUSTOM ? customValue : shownQuality;

  useEffect(() => {
    let dead = false;
    setCached(false);
    if (path) isTrackCached(path).then((v) => !dead && setCached(v));
    return () => {
      dead = true;
    };
  }, [path]);

  const toggleCache = async () => {
    if (!path) return;
    setCacheBusy(true);
    try {
      if (cached) {
        await uncacheTrack(path);
        setCached(false);
        toast.success("Removed from the offline cache");
      } else {
        toast("Caching for offline playback…");
        await cacheTrack(path);
        setCached(true);
        toast("Cached — plays without the server");
      }
      // The title marks and the downloads page read the shared snapshot.
      qc.invalidateQueries({ queryKey: CACHED_PATHS_KEY });
      qc.invalidateQueries({ queryKey: CACHED_SIZES_KEY });
    } catch (e) {
      toast.error(`Cache failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setCacheBusy(false);
    }
  };

  const exportTrack = async () => {
    setBusy(true);
    try {
      const a = document.createElement("a");
      a.href = api.trackExportUrl(path, codec, requestQuality);
      a.download = "";
      document.body.appendChild(a);
      a.click();
      a.remove();
      const label = spec?.presets.find((p) => p.v === shownQuality)?.label;
      toast(`Exporting as ${spec?.label ?? codec}${label ? ` · ${label}` : ""} — the save dialog opens when it's ready`);
      setOpen(false);
    } finally {
      setBusy(false);
    }
  };

  /** Save this codec + quality as the export default, the same
   * `export_codec`/`export_quality` keys the Export page's "Save as default"
   * writes — so the panel opens on it next time, and every export surface
   * agrees. */
  const saveAsDefault = async () => {
    try {
      await api.exportSaveDefaults({ codec, quality: requestQuality });
      qc.invalidateQueries({ queryKey: ["config"] });
      qc.invalidateQueries({ queryKey: ["exportDefaults"] });
      toast.success(`Default export: ${spec?.label ?? codec}`);
    } catch (e) {
      toast.error(`Could not save the default: ${e instanceof Error ? e.message : e}`);
    }
  };

  const btnCls = iconOnly
    ? `p-2 rounded-lg text-zinc-400 ${disabled ? "opacity-40" : "hover:bg-raise hover:text-white"}`
    : "btn-ghost !py-1 text-xs";

  return (
    <div className={compact || iconOnly ? "inline-flex items-center gap-1" : "flex items-center gap-1.5 flex-wrap"}>
      <button
        className={btnCls}
        onClick={toggleCache}
        disabled={disabled || cacheBusy}
        title={cached ? "Downloaded — cached in the player · click to remove" : "Download — cache in the player for offline playback"}
        aria-label="Download"
      >
        {cached ? <ArrowDown className="h-4 w-4 text-emerald-500" /> : <ArrowDownToLine className="h-4 w-4" />}
        {!iconOnly && (cached ? " Downloaded" : " Download")}
      </button>
      <div className="relative">
        <button
          className={btnCls}
          onClick={() => setOpen(!open)}
          disabled={disabled}
          title="Export — transcode to FLAC / MP3 / … and save the file"
          aria-label="Export"
        >
          <FileOutput className="h-4 w-4" />
          {!iconOnly && " Export"}
        </button>
        <Popover
          open={open}
          onClose={() => setOpen(false)}
          placement={up ? "top" : "bottom"}
          panelClass="w-64 p-3 space-y-2.5"
        >
            {title && <div className="text-[11px] text-zinc-400 truncate">{title}</div>}
            <CodecQualitySelect
              stacked
              specs={table}
              codec={codec}
              quality={shownQuality}
              customValue={customValue}
              /* A new codec invalidates the quality it was on, so it resets to
                 the codec's own default — one write, codec and quality
                 together. */
              onCodec={(v) => {
                setCodecState(v);
                setQualityState("");
                setCustom(null);
              }}
              onQuality={setQualityState}
              onCustomValue={setCustom}
            />
            <button className="btn-primary w-full !py-1.5 text-xs" onClick={exportTrack} disabled={busy}>
              {busy ? "Preparing…" : `Export ${spec?.label ?? codec}`}
            </button>
            <button
              className="btn-ghost w-full !py-1 text-[11px] inline-flex items-center justify-center gap-1.5"
              onClick={saveAsDefault}
              title="Use this codec and quality for every export — the Export page's own saved default"
            >
              <Save className="h-3.5 w-3.5" /> Set as default
            </button>
        </Popover>
      </div>
    </div>
  );
}