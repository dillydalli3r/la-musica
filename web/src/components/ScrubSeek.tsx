import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { api } from "../api";
import { fmtDuration } from "../lib/fmt";

/** Cut frames, keyed `path@second`. Scrubbing back and forth across a video
 *  then costs one cut per distinct second instead of one per pointer move.
 *  Every URL in here is revoked when the player unmounts (below), so the
 *  cache never outlives the video it belongs to. */
const thumbs = new Map<string, string>();

/** Wide enough to recognize the shot, small enough that ffmpeg answers while
 *  the cursor is still there. */
const THUMB_W = 168;
/** Pointer moves arrive every few ms; only a settled position earns a cut. */
const DEBOUNCE_MS = 120;

interface Props {
  /** null for audio — then this is exactly the seek bar it has always been. */
  videoPath: string | null;
  value: number;
  max: number;
  className?: string;
  onChange: (t: number) => void;
}

/** The fullscreen seek bar. For a video it grows a scrub preview: the frame
 *  at the hovered/dragged position, above the cursor, with a m:ss label. If a
 *  cut fails (or has not arrived yet) the label stands alone — a scrub never
 *  raises a toast or blocks the drag. */
export default function ScrubSeek({ videoPath, value, max, className, onChange }: Props) {
  const boxRef = useRef<HTMLDivElement>(null);
  const timer = useRef<number | null>(null);
  const inflight = useRef<AbortController | null>(null);
  const [tip, setTip] = useState<{ pct: number; t: number; src: string | null } | null>(null);

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
      inflight.current?.abort();
      for (const url of thumbs.values()) URL.revokeObjectURL(url);
      thumbs.clear();
    },
    []
  );

  /** Cut (or re-use) the frame at `sec` and show it if the cursor is still
   *  in that second — a slow answer never paints where the cursor used to be. */
  const cut = async (sec: number) => {
    if (!videoPath) return;
    const bucket = Math.floor(sec);
    const key = `${videoPath}@${bucket}`;
    const show = (src: string) =>
      setTip((prev) => (prev && Math.floor(prev.t) === bucket ? { ...prev, src } : prev));
    const hit = thumbs.get(key);
    if (hit) {
      show(hit);
      return;
    }
    inflight.current?.abort();
    const ctrl = new AbortController();
    inflight.current = ctrl;
    try {
      const r = await fetch(api.videoThumbUrl(videoPath, sec, THUMB_W), { signal: ctrl.signal });
      if (!r.ok) return; // no frame for this position: the label alone is the fallback
      const url = URL.createObjectURL(await r.blob());
      thumbs.set(key, url);
      show(url);
    } catch {
      /* aborted or unreachable — silent, never an error mid-scrub */
    }
  };

  const track = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!videoPath || max <= 0) return;
    const box = boxRef.current;
    if (!box) return;
    const rect = box.getBoundingClientRect();
    const pct = Math.min(100, Math.max(0, ((e.clientX - rect.left) / rect.width) * 100));
    // the last frame second reads better than a black end-of-file still
    const t = Math.min(max - 0.25, (pct / 100) * max);
    setTip({ pct, t, src: thumbs.get(`${videoPath}@${Math.floor(t)}`) ?? null });
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => cut(t), DEBOUNCE_MS);
  };

  return (
    <div
      ref={boxRef}
      className={`relative ${className ?? ""}`}
      onPointerMove={track}
      onPointerDown={track}
      onPointerLeave={() => setTip(null)}
    >
      <input
        type="range"
        min={0}
        max={max || 0}
        step={0.05}
        value={Math.min(value, max || 0)}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-full seek-fat"
        title="Seek"
      />
      {tip && (
        <div
          className="absolute bottom-full mb-2 -translate-x-1/2 z-20 pointer-events-none rounded-md overflow-hidden border border-white/20 shadow-xl bg-zinc-950"
          style={{ left: `${tip.pct}%` }}
        >
          {tip.src && <img src={tip.src} alt="" className="block" style={{ width: THUMB_W }} />}
          <div className="px-1.5 py-0.5 text-center text-[11px] font-mono text-zinc-100 tabular-nums">
            {fmtDuration(tip.t)}
          </div>
        </div>
      )}
    </div>
  );
}
