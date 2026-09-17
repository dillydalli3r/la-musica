import { useEffect, useState } from "react";
import { CheckCircle2, Download, FileOutput } from "lucide-react";
import { api } from "../api";
import { toast } from "../store";
import { cacheTrack, isTrackCached, uncacheTrack } from "../lib/mediaCache";

/** Codec choices for per-track exports; lossy codecs expose a bitrate. */
const CODECS = [
  { id: "flac", label: "FLAC (lossless)", lossy: false },
  { id: "alac", label: "ALAC (lossless)", lossy: false },
  { id: "wav", label: "WAV (lossless)", lossy: false },
  { id: "mp3", label: "MP3", lossy: true, defaultBitrate: 320 },
  { id: "aac", label: "AAC / M4A", lossy: true, defaultBitrate: 256 },
  { id: "opus", label: "Opus", lossy: true, defaultBitrate: 192 },
];

const BITRATES = [96, 128, 160, 192, 256, 320, 448, 500];

/** "Download" caches the track inside the player (offline playback — no
 * file lands in the Downloads folder); "Export" is the real file-saving
 * action: transcode to the chosen codec/bitrate and save.
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
  const [codec, setCodec] = useState("flac");
  const [bitrate, setBitrate] = useState(320);
  const [busy, setBusy] = useState(false);
  // cache state for the current track
  const [cached, setCached] = useState(false);
  const [cacheBusy, setCacheBusy] = useState(false);
  const chosen = CODECS.find((c) => c.id === codec) ?? CODECS[0];

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
    } catch (e) {
      toast(`Cache failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setCacheBusy(false);
    }
  };

  const exportTrack = async () => {
    setBusy(true);
    try {
      const a = document.createElement("a");
      a.href = api.trackExportUrl(path, codec, bitrate);
      a.download = "";
      document.body.appendChild(a);
      a.click();
      a.remove();
      toast(`Exporting as ${chosen.label}${chosen.lossy ? ` @ ${bitrate} kbps` : ""} — the save dialog opens when it's ready`);
      setOpen(false);
    } finally {
      setBusy(false);
    }
  };

  const btnCls = iconOnly
    ? `p-2 rounded-lg text-zinc-400 ${disabled ? "opacity-40" : "hover:bg-raise hover:text-white"}`
    : "btn-ghost !py-1 text-xs";
  const posCls = up ? "bottom-full mb-2" : "top-full mt-1";

  return (
    <div className={compact || iconOnly ? "inline-flex items-center gap-1" : "flex items-center gap-1.5 flex-wrap"}>
      <button
        className={btnCls}
        onClick={toggleCache}
        disabled={disabled || cacheBusy}
        title={cached ? "Downloaded — cached in the player · click to remove" : "Download — cache in the player for offline playback"}
        aria-label="Download"
      >
        {cached ? <CheckCircle2 className="h-4 w-4 text-emerald-500" /> : <Download className="h-4 w-4" />}
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
        {open && (
          <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className={`absolute z-50 right-0 ${posCls} w-60 rounded-lg bg-zinc-950 border border-border shadow-2xl p-3 space-y-2.5`}>
            {title && <div className="text-[11px] text-zinc-400 truncate">{title}</div>}
            <label className="block text-[10px] uppercase tracking-wider text-zinc-500">Codec</label>
            <select className="input !py-1 text-xs" value={codec} onChange={(e) => {
              setCodec(e.target.value);
              const c = CODECS.find((x) => x.id === e.target.value);
              if (c?.lossy && c.defaultBitrate) setBitrate(c.defaultBitrate);
            }}>
              {CODECS.map((c) => (
                <option key={c.id} value={c.id}>{c.label}</option>
              ))}
            </select>
            {chosen.lossy && (
              <>
                <label className="block text-[10px] uppercase tracking-wider text-zinc-500">Bitrate</label>
                <select className="input !py-1 text-xs" value={bitrate} onChange={(e) => setBitrate(Number(e.target.value))}>
                  {BITRATES.map((b) => (
                    <option key={b} value={b}>{b} kbps</option>
                  ))}
                </select>
              </>
            )}
            <button className="btn-primary w-full !py-1.5 text-xs" onClick={exportTrack} disabled={busy}>
              {busy ? "Preparing…" : `Export ${chosen.label}${chosen.lossy ? ` · ${bitrate}k` : ""}`}
            </button>
          </div>
          </>
        )}
      </div>
    </div>
  );
}
