import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { EqBand } from "../api";
import { EQ_FC_MAX, EQ_FC_MIN, EQ_GAIN_LIMIT, EQ_PREAMP_LIMIT, eqBandResponseDb, eqBandActive, eqResponseDb, eqType } from "../lib/eqNodes";

/** The decibel window the plot draws. Wider than any sane correction (AutoEq's
 *  are within ±12 dB) and narrower than the ±20 a single band may be set to, so
 *  a hand-made band that runs off the top is visibly off the plot rather than
 *  silently squashed into it. */
const DB_TOP = 20;
/** Frequency ticks: one per decade-ish step, labelled the way an equalizer
 *  labels them (1k, not 1000). */
const FREQ_TICKS = [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000];
const DB_TICKS = [-20, -10, 0, 10, 20];
const PAD = { left: 34, right: 12, top: 12, bottom: 20 };
/** How close to a handle a press counts as grabbing it, in CSS pixels. */
const GRAB_PX = 16;
/** Curve resolution. 240 points is smooth at any width this pane gets and
 *  keeps one redraw's `getFrequencyResponse` calls to a few thousand. */
const POINTS = 240;

const logOf = (f: number) => Math.log(Math.max(1, f) / EQ_FC_MIN) / Math.log(EQ_FC_MAX / EQ_FC_MIN);

function fmtFreq(f: number): string {
  if (f >= 1000) return `${(f / 1000).toFixed(f >= 10000 ? 0 : 1).replace(/\.0$/, "")}k`;
  return String(Math.round(f));
}

/** The interactive response curve: what the profile does to the spectrum, with
 *  one draggable handle per band.
 *
 *  The curve is not drawn from a hand-rolled formula — every line comes from the
 *  browser's own `BiquadFilterNode.getFrequencyResponse` (lib/eqNodes), the same
 *  maths the player applies, so the plot cannot disagree with what is heard.
 *  Dragging a handle writes fc (horizontally, logarithmically) and gain
 *  (vertically); a shape-only band (a pass or a notch) has no gain to drag, so
 *  its handle moves on the frequency axis alone. Editing a NUMBER precisely is
 *  the band table's job — the point here is to see and to sweep. */
export default function EqCurve({
  filters,
  preampDb,
  selected,
  onSelect,
  onChange,
  height = 260,
}: {
  filters: EqBand[];
  preampDb: number;
  /** Index of the selected band, or null. */
  selected: number | null;
  onSelect: (index: number | null) => void;
  onChange: (index: number, patch: Partial<EqBand>) => void;
  height?: number;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(640);
  const dragRef = useRef<number | null>(null);

  // One offline context for the response maths: `getFrequencyResponse` needs a
  // node, and the main playback graph may not even exist yet (the EQ page can
  // be opened before anything has played).
  const audio = useMemo(() => {
    try {
      return new OfflineAudioContext(1, 128, 48000);
    } catch {
      return null;
    }
  }, []);

  const freqs = useMemo(() => {
    const out = new Float32Array(POINTS);
    for (let i = 0; i < POINTS; i++) {
      out[i] = EQ_FC_MIN * Math.pow(EQ_FC_MAX / EQ_FC_MIN, i / (POINTS - 1));
    }
    return out;
  }, []);

  useEffect(() => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    const ro = new ResizeObserver(() => setWidth(Math.max(280, wrap.clientWidth)));
    ro.observe(wrap);
    setWidth(Math.max(280, wrap.clientWidth));
    return () => ro.disconnect();
  }, []);

  /** Plot geometry: the canvas box, and the two maps used by both the drawing
   *  and the pointer maths — one definition, so a handle is always where the
   *  pointer thinks it is. */
  const geom = useMemo(() => {
    const w = width;
    const h = height;
    const plotW = Math.max(10, w - PAD.left - PAD.right);
    const plotH = Math.max(10, h - PAD.top - PAD.bottom);
    return {
      w, h, plotW, plotH,
      xOf: (f: number) => PAD.left + logOf(f) * plotW,
      yOf: (db: number) => PAD.top + plotH / 2 - (db / DB_TOP) * (plotH / 2),
      fOf: (x: number) => EQ_FC_MIN * Math.pow(EQ_FC_MAX / EQ_FC_MIN, (x - PAD.left) / plotW),
      dbOf: (y: number) => ((PAD.top + plotH / 2 - y) / (plotH / 2)) * DB_TOP,
    };
  }, [width, height]);

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.round(geom.w * dpr);
    canvas.height = Math.round(geom.h * dpr);
    const c = canvas.getContext("2d");
    if (!c) return;
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    const style = getComputedStyle(document.documentElement);
    const accent = style.getPropertyValue("--accent").trim() || "255 255 255";
    const rgb = (a: number) => `rgb(${accent.split(/\s+/).join(" ")} / ${a})`;
    const muted = "#71717a";

    c.clearRect(0, 0, geom.w, geom.h);
    // grid
    c.strokeStyle = "rgba(255,255,255,0.07)";
    c.fillStyle = muted;
    c.font = "10px ui-monospace, SFMono-Regular, Menlo, monospace";
    c.lineWidth = 1;
    for (const db of DB_TICKS) {
      const y = Math.round(geom.yOf(db)) + 0.5;
      c.beginPath();
      c.moveTo(PAD.left, y);
      c.lineTo(geom.w - PAD.right, y);
      c.stroke();
      c.fillText(`${db > 0 ? "+" : ""}${db}`, 4, y + 3);
    }
    for (const f of FREQ_TICKS) {
      const x = Math.round(geom.xOf(f)) + 0.5;
      c.beginPath();
      c.moveTo(x, PAD.top);
      c.lineTo(x, geom.h - PAD.bottom);
      c.stroke();
      const label = fmtFreq(f);
      c.fillText(label, Math.min(x + 3, geom.w - PAD.right - label.length * 6), geom.h - 6);
    }

    if (!audio) {
      c.fillStyle = muted;
      c.fillText("This browser has no WebAudio — the curve cannot be drawn.", PAD.left, PAD.top + 14);
      return;
    }

    // each band on its own, faint — what the handles are asking for
    filters.forEach((band, i) => {
      if (!eqType(band.type)) return;
      const resp = eqBandResponseDb(audio, band, freqs);
      c.beginPath();
      for (let p = 0; p < freqs.length; p++) {
        const x = geom.xOf(freqs[p]);
        const y = geom.yOf(resp[p]);
        if (p === 0) c.moveTo(x, y);
        else c.lineTo(x, y);
      }
      c.strokeStyle = eqBandActive(band)
        ? (i === selected ? rgb(0.55) : "rgba(255,255,255,0.18)")
        : "rgba(255,255,255,0.08)";
      c.setLineDash([3, 3]);
      c.stroke();
      c.setLineDash([]);
    });

    // the whole chain, including the preamp
    const total = eqResponseDb(audio, filters, preampDb, freqs);
    c.beginPath();
    for (let p = 0; p < freqs.length; p++) {
      const x = geom.xOf(freqs[p]);
      const y = geom.yOf(total[p]);
      if (p === 0) c.moveTo(x, y);
      else c.lineTo(x, y);
    }
    c.strokeStyle = rgb(1);
    c.lineWidth = 2;
    c.stroke();
    c.lineWidth = 1;

    // handles
    const preamp = Math.max(-EQ_PREAMP_LIMIT, Math.min(EQ_PREAMP_LIMIT, Number(preampDb) || 0));
    filters.forEach((band, i) => {
      if (!eqType(band.type)) return;
      const shape = !eqBandActive(band) || !["PK", "LS", "LSC", "HS", "HSC"].includes(eqType(band.type));
      const x = geom.xOf(Math.max(EQ_FC_MIN, Math.min(EQ_FC_MAX, Number(band.fc) || EQ_FC_MIN)));
      const y = geom.yOf(shape ? 0 : Math.max(-EQ_GAIN_LIMIT, Math.min(EQ_GAIN_LIMIT, Number(band.gain) || 0)));
      c.beginPath();
      c.arc(x, y, i === selected ? 7 : 5, 0, Math.PI * 2);
      c.fillStyle = eqBandActive(band) ? rgb(i === selected ? 1 : 0.75) : "rgba(255,255,255,0.25)";
      c.fill();
      if (i === selected) {
        c.strokeStyle = "rgba(255,255,255,0.85)";
        c.stroke();
      }
    });
    // the preamp as its own dashed level — the curve already includes it, and
    // this is what says WHY a curve that looks flat is not at 0 dB
    if (preamp) {
      const y = Math.round(geom.yOf(preamp)) + 0.5;
      c.setLineDash([2, 4]);
      c.strokeStyle = "rgba(255,255,255,0.28)";
      c.beginPath();
      c.moveTo(PAD.left, y);
      c.lineTo(geom.w - PAD.right, y);
      c.stroke();
      c.setLineDash([]);
    }
  }, [audio, filters, freqs, geom, preampDb, selected]);

  useEffect(() => { draw(); }, [draw]);

  /** The band whose handle is nearest to a pointer position, within reach. */
  const handleAt = (px: number, py: number): number | null => {
    let best: number | null = null;
    let bestDist = GRAB_PX;
    // Last band first: the later one is drawn on top, so it is the one the
    // user is pointing at when two handles overlap.
    for (let i = filters.length - 1; i >= 0; i--) {
      const band = filters[i];
      if (!eqType(band.type)) continue;
      const shape = !eqBandActive(band) || !["PK", "LS", "LSC", "HS", "HSC"].includes(eqType(band.type));
      const x = geom.xOf(Math.max(EQ_FC_MIN, Math.min(EQ_FC_MAX, Number(band.fc) || EQ_FC_MIN)));
      const y = geom.yOf(shape ? 0 : Math.max(-EQ_GAIN_LIMIT, Math.min(EQ_GAIN_LIMIT, Number(band.gain) || 0)));
      const d = Math.hypot(px - x, py - y);
      if (d <= bestDist) {
        bestDist = d;
        best = i;
      }
    }
    return best;
  };

  const onPointerDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const i = handleAt(e.clientX - rect.left, e.clientY - rect.top);
    onSelect(i);
    if (i === null) return;
    dragRef.current = i;
    e.currentTarget.setPointerCapture(e.pointerId);
  };

  const onPointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const i = dragRef.current;
    if (i === null || i === undefined) return;
    const band = filters[i];
    if (!band) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const fc = Math.min(EQ_FC_MAX, Math.max(EQ_FC_MIN, geom.fOf(e.clientX - rect.left)));
    const shape = !["PK", "LS", "LSC", "HS", "HSC"].includes(eqType(band.type));
    const patch: Partial<EqBand> = { fc: Number(fc.toFixed(fc < 100 ? 1 : 0)) };
    if (!shape) {
      const gain = geom.dbOf(e.clientY - rect.top);
      patch.gain = Number(Math.max(-EQ_GAIN_LIMIT, Math.min(EQ_GAIN_LIMIT, gain)).toFixed(1));
    }
    onChange(i, patch);
  };

  const endDrag = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (dragRef.current === null) return;
    dragRef.current = null;
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
  };

  return (
    <div ref={wrapRef} className="w-full">
      <canvas
        ref={canvasRef}
        style={{ width: geom.w, height: geom.h, touchAction: "none" }}
        className="rounded-md border border-border bg-panel/60 cursor-crosshair"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        role="img"
        aria-label="Equalizer response curve — drag a band's handle to move it"
      />
    </div>
  );
}
