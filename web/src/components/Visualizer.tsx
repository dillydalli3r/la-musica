import { useEffect, useRef } from "react";
import { activeAnalyser } from "../lib/analyser";

/** Frequency-bar visualizer driven ONLY by the shared WebAudio analyser.
 * Bars use logarithmic band mapping — music's energy lives in the low
 * octaves, a linear split makes the left half dance and the right half
 * sit flat — plus per-bar peak caps that fall with gravity. With no live
 * signal (paused, idle, unobservable stream) it holds a flat baseline:
 * the strip never invents motion or noise. */
export default function Visualizer({
  playing,
  bars = 56,
  className = "",
  mirror = false,
}: {
  playing: boolean;
  bars?: number;
  className?: string;
  /** Draw a mirrored (top+bottom) field instead of bars on one baseline. */
  mirror?: boolean;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const levels = useRef<Float32Array>(new Float32Array(bars));
  const peaks = useRef<Float32Array>(new Float32Array(bars));
  const stale = useRef(0); // consecutive frames with no analyser signal
  const playingRef = useRef(playing);
  const startRef = useRef<() => void>(() => {});
  useEffect(() => {
    playingRef.current = playing;
    // Idle frames are skipped, so playback must restart the loop.
    if (playing) startRef.current();
  }, [playing]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const accent = getComputedStyle(document.documentElement)
      .getPropertyValue("--accent")
      .trim() || "255 255 255";
    const accentSoft = getComputedStyle(document.documentElement)
      .getPropertyValue("--accent-soft")
      .trim() || "212 212 216";

    let raf = 0;
    let freq: Uint8Array | null = null;
    let lastW = 0;

    const start = () => {
      if (!raf) raf = requestAnimationFrame(tick);
    };
    startRef.current = start;

    const tick = () => {
      raf = requestAnimationFrame(tick);
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      const w = canvas.clientWidth;
      const h = canvas.clientHeight;
      if (!w || !h) return;
      if (w !== lastW) {
        canvas.width = Math.round(w * dpr);
        canvas.height = Math.round(h * dpr);
        lastW = w;
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);

      const analyser = activeAnalyser();
      const n = bars;

      // ---- gather band values (0..1) -------------------------------
      let live = false;
      let data: Uint8Array | null = null;
      if (analyser && playingRef.current) {
        if (!freq || freq.length !== analyser.frequencyBinCount) {
          freq = new Uint8Array(analyser.frequencyBinCount);
        }
        analyser.getByteFrequencyData(freq as Uint8Array<ArrayBuffer>);
        let sum = 0;
        for (let i = 0; i < freq.length; i++) sum += freq[i];
        live = sum > 0; // all-zero means tainted media / suspended ctx
        if (live) {
          stale.current = 0;
          data = freq;
        } else {
          stale.current += 1;
        }
      } else {
        stale.current += 1;
      }
      const synthetic = !live;

      for (let i = 0; i < n; i++) {
        let target: number;
        if (!synthetic && data) {
          // Logarithmic bands: the lowest band starts near DC, each next
          // band spans more bins — even energy spread across the strip.
          const lo = Math.floor(Math.pow(i / n, 2.0) * (data.length * 0.72));
          const hi = Math.max(lo + 1, Math.floor(Math.pow((i + 1) / n, 2.0) * (data.length * 0.72)));
          let acc = 0;
          for (let k = lo; k < hi; k++) acc = Math.max(acc, data[k]);
          // Musical tilt: boost the highs a touch, tame the bass peak.
          const tilt = 1 + (i / n) * 0.9;
          target = Math.min(1, (acc / 255) * tilt);
          target = Math.pow(target, 0.8);
        } else {
          // No real signal (paused, idle, or unobservable stream): hold a
          // flat near-zero baseline. The strip must only ever draw actual
          // audio — never synthesized motion or noise.
          target = 0.015;
        }
        // Smooth rise, slower fall — reads as energy, not noise.
        const cur = levels.current[i] ?? 0;
        levels.current[i] = cur + (target - cur) * (target > cur ? 0.5 : 0.18);
        // Peak caps with gravity.
        const pk = peaks.current[i] ?? 0;
        peaks.current[i] = Math.max(levels.current[i], pk - 0.008);
      }

      // ---- draw ------------------------------------------------------
      const gap = Math.max(1.5, w / n * 0.28);
      const bw = Math.max(1.5, (w - gap * (n - 1)) / n);
      const base = mirror ? h / 2 : h;
      for (let i = 0; i < n; i++) {
        const v = Math.max(0.02, levels.current[i] ?? 0);
        const x = i * (bw + gap);
        const bh = Math.max(2, v * (mirror ? h / 2 - 2 : h - 2));
        const grad = ctx.createLinearGradient(0, base - bh, 0, base);
        grad.addColorStop(0, `rgb(${accentSoft} / 0.95)`);
        grad.addColorStop(1, `rgb(${accent} / 0.35)`);
        ctx.fillStyle = grad;
        const r = Math.min(bw / 2, 2);
        ctx.beginPath();
        ctx.roundRect(x, base - bh, bw, bh, r);
        ctx.fill();
        if (mirror) {
          ctx.beginPath();
          ctx.roundRect(x, base, bw, bh * 0.62, r);
          ctx.fill();
        }
        // falling peak cap
        const pk = peaks.current[i] ?? 0;
        if (pk > 0.03) {
          const py = base - Math.max(2, pk * (mirror ? h / 2 - 2 : h - 2)) - 2;
          ctx.fillStyle = `rgb(${accent} / 0.8)`;
          ctx.fillRect(x, py, bw, 1.5);
        }
      }

      // Nothing left to animate (paused and fully settled): the flat
      // baseline is drawn, so stop burning frames until playback resumes.
      if (
        synthetic &&
        stale.current > 30 &&
        levels.current.every((v) => v < 0.02) &&
        peaks.current.every((p) => p <= 0.03)
      ) {
        cancelAnimationFrame(raf);
        raf = 0;
      }
    };
    raf = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(raf);
      raf = 0;
    };
  }, [bars, mirror]);

  return <canvas ref={canvasRef} className={className} aria-hidden />;
}
