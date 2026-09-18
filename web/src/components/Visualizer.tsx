import { useEffect, useRef } from "react";
import { MAX_DB, MIN_DB, activeAnalyser } from "../lib/analyser";

/** Height-mapping constants, all in dBFS. FLOOR_DB is the empty baseline,
 * CEIL_DB the full height (full scale), TILT_DB the lift of the top band
 * over the bottom one. Tuned against a real spectrum: with this material's
 * bands peaking between −29 dBFS (bass) and −67 dBFS (top octave), the old
 * tilt of +6 left the whole right half dead, and +30 (≈3 dB/octave, music's
 * roll-off) puts every band in the 0.4–0.6 range and still swings ~0.25 of
 * the strip per frame. */
const FLOOR_DB = -90;
const CEIL_DB = 0;
const TILT_DB = 30;
/** Normaliser: the peak a band is expected to reach, and the fraction of a
 * band's rolling peak that survives one frame (~8 s half-life at 60 fps). */
const PEAK_REF = 0.55;
const PEAK_DECAY = 0.9985;
/** Idle redraw interval (ms) when no signal is live. */
const IDLE_MS = 250;

/** Frequency-bar visualizer driven ONLY by the shared WebAudio analyser.
 * Bars use logarithmic band mapping — music's energy lives in the low
 * octaves, a linear split makes the left half dance and the right half
 * sit flat — plus per-bar peak caps that fall with gravity. With no live
 * signal (paused, idle, unobservable stream) it holds a flat baseline:
 * the strip never invents motion or noise.
 *
 * Height mapping — the exact maths, because the old one is why loud tracks
 * drew one solid block:
 *   db = MIN_DB + byte/255 * (MAX_DB − MIN_DB)        (undo the window)
 *   h  = (db + TILT_DB·i/(n−1) − FLOOR_DB) / (CEIL_DB − FLOOR_DB)
 *        clamped to 0..1
 * FLOOR_DB −90 (nothing) → CEIL_DB 0 dBFS (full height), so a band at
 * byte 255 — the top of the analyser's window, −10 dBFS — still only reads
 * 0.89: full scale has genuine room above it instead of pinning. The tilt
 * is a dB slope over the strip (music rolls off ~3 dB/octave, so the top
 * octave needs the lift), NOT the old multiplier on the finished height,
 * which pushed everything up and pinned the highs hardest. No pow()
 * inflation either: the dB scale is already perceptual.
 * A per-band rolling peak (PEAK_DECAY per frame, ~8 s half-life) then lifts
 * quiet bands by up to 2× and never attenuates, so a quiet master still
 * shows its shape while a sustained fortissimo keeps its raw height. */
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
  const norm = useRef<Float32Array>(new Float32Array(bars));
  const playingRef = useRef(playing);
  useEffect(() => {
    playingRef.current = playing;
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
    let lastIdle = 0;

    const tick = () => {
      // The loop NEVER cancels itself. It used to stop after ~2 s of silent
      // frames and restart only on a `playing` PROP CHANGE — so a buffering
      // stall, a seek, a gapless element swap or a silent passage killed the
      // strip for the rest of the session (track changes keep `playing`
      // true), and the first play (suspended context) never recovered.
      raf = requestAnimationFrame(tick);
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      const w = canvas.clientWidth;
      const h = canvas.clientHeight;
      if (!w || !h) return;
      // A canvas resize blanks the backing store by itself, so a resized
      // frame is always repainted (see the idle skip below).
      const resized = w !== lastW;
      if (resized) {
        canvas.width = Math.round(w * dpr);
        canvas.height = Math.round(h * dpr);
        lastW = w;
      }

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
        if (live) data = freq;
      }
      const synthetic = !live;

      // Idle: nothing to animate, so redraw at ~4 Hz (the baseline still
      // eases down) while the loop keeps running — the next live frame is
      // drawn immediately, with no prop change to restart anything.
      // The skipped frames must not CLEAR either: the canvas is composited at
      // the display rate, so clearing and returning left the strip blank for
      // 59 of every 60 frames while paused (measured: 0 painted pixels on
      // 7 of 8 samples; the baseline showed only as a 4 Hz flash). An idle
      // frame now leaves the previous one on screen.
      const now = performance.now();
      if (synthetic && !resized && now - lastIdle < IDLE_MS) return;
      lastIdle = now;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);

      for (let i = 0; i < n; i++) {
        let target: number;
        if (!synthetic && data) {
          // Logarithmic bands: the lowest band starts near DC, each next
          // band spans more bins — even energy spread across the strip.
          const lo = Math.floor(Math.pow(i / n, 2.0) * (data.length * 0.72));
          const hi = Math.max(lo + 1, Math.floor(Math.pow((i + 1) / n, 2.0) * (data.length * 0.72)));
          let acc = 0;
          for (let k = lo; k < hi; k++) acc = Math.max(acc, data[k]);
          // The byte is the analyser's dB window squeezed into 0..255 —
          // see the mapping note in the header.
          const db = MIN_DB + (acc / 255) * (MAX_DB - MIN_DB);
          const tilt = TILT_DB * (n > 1 ? i / (n - 1) : 0);
          target = Math.min(1, Math.max(0, (db + tilt - FLOOR_DB) / (CEIL_DB - FLOOR_DB)));
          // Per-band rolling peak → bounded lift (≤2×, never attenuation).
          const roll = Math.max(target, (norm.current[i] ?? 0) * PEAK_DECAY);
          norm.current[i] = roll;
          target = Math.min(1, target * Math.min(2, Math.max(1, PEAK_REF / Math.max(roll, 0.06))));
        } else {
          // No real signal (paused, idle, or unobservable stream): hold a
          // flat near-zero baseline. The strip must only ever draw actual
          // audio — never synthesized motion or noise.
          target = 0.015;
        }
        // Smooth rise, slower fall — reads as energy, not noise.
        const cur = levels.current[i] ?? 0;
        levels.current[i] = cur + (target - cur) * (target > cur ? 0.5 : 0.18);
        // Peak caps with gravity. The 0.008/frame fall is PLAYBACK gravity:
        // idle frames only repaint at 4 Hz, so the same constant left the caps
        // hanging at half height above collapsed bars for ~20 s. With no signal
        // they instead fall with the bars (same 18%/frame easing, the constant
        // the rise/fall smooth above uses) until they meet the baseline.
        const pk = peaks.current[i] ?? 0;
        peaks.current[i] = synthetic
          ? Math.max(levels.current[i], pk + (levels.current[i] - pk) * 0.18)
          : Math.max(levels.current[i], pk - 0.008);
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
    };

    raf = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(raf);
      raf = 0;
    };
  }, [bars, mirror]);

  return <canvas ref={canvasRef} className={className} aria-hidden />;
}
