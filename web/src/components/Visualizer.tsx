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
/** Idle redraw interval (ms) when no signal is live. The loop drops from the
 *  display clock to this timer while there is nothing to draw, and stops
 *  repainting altogether once the strip has eased onto its baseline — a
 *  paused canvas costs ten wake-ups a second, not sixty frames. */
const IDLE_MS = 100;
/** Bar count. One strip reads the same at 56 everywhere it is mounted. */
const BARS = 56;
/** The idle baseline a bar rests on, and how close to it counts as "at
 *  rest" for the stop-painting check. */
const BASE = 0.015;
const REST_EPS = 0.002;
/** Per-frame height easing: fast attack, slower release. The attack is
 * near-instant (~1.5 frames) so a kick lands on the frame it happens; the
 * release is what makes the strip readable, and it must stay well under the
 * idle rate so a bar does not shrink BETWEEN idle repaints. */
const ATTACK = 0.72;
const RELEASE = 0.12;
/** Expansion around mid-height: the raw dB ratio packs every band into a
 * narrow band of heights, so the strip reads as a silhouette only after a
 * mild contrast stretch. Clamped, so it can never push a bar over full. */
const CONTRAST = 1.25;
/** Peak-cap gravity, as strip-heights per frame (~2.6 s to fall the full
 * strip). With the bars releasing in ~20 frames the caps separate from them
 * and read as falling, instead of riding the bar tops. */
const PEAK_FALL = 0.0065;
/** Level above which a bar gets its halo — the decorative motion that is
 * skipped under prefers-reduced-motion. */
const GLOW_AT = 0.55;

/** Are the bars — and their falling caps — already sitting on the idle
 *  baseline? Then there is nothing left to ease and nothing to redraw: a
 *  paused strip should cost its timer and nothing else, not the same flat
 *  line ten times a second. */
function atRest(levels: Float32Array, peaks: Float32Array, n: number): boolean {
  for (let i = 0; i < n; i++) {
    if (Math.abs((levels[i] ?? 0) - BASE) > REST_EPS) return false;
    if (Math.abs((peaks[i] ?? 0) - BASE) > REST_EPS) return false;
  }
  return true;
}

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
 * shows its shape while a sustained fortissimo keeps its raw height.
 * Heights then ease asymmetrically (ATTACK/RELEASE) and the loudest bars get
 * a faint halo — the one decorative touch, and the only thing dropped under
 * prefers-reduced-motion: the bars themselves are the meter and keep moving.
 *
 * Cost: ONE ramp gradient per frame instead of one per bar, and the loop
 * moves to the idle timer — then stops painting entirely — when there is no
 * signal, so a paused strip is not a 60 fps redraw of the same flat line.
 * The ramp is anchored to the strip rather than to each bar (quiet colour at
 * the baseline, bright at the top), so a taller bar carries more of it and
 * level reads as brightness as well as height. */
export default function Visualizer({ playing, className = "" }: { playing: boolean; className?: string }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const levels = useRef<Float32Array>(new Float32Array(BARS));
  const peaks = useRef<Float32Array>(new Float32Array(BARS));
  const norm = useRef<Float32Array>(new Float32Array(BARS));
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
    // Read once: a matchMedia change mid-session is not worth a listener for a
    // halo this small (the ambient background re-reads its own on every open).
    const motion = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    let handle = 0;
    let onTimer = false;
    let freq: Uint8Array | null = null;
    let lastW = 0;
    let lastIdle = 0;

    const tick = () => {
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      const w = canvas.clientWidth;
      const h = canvas.clientHeight;

      const analyser = activeAnalyser();
      const n = BARS;

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

      // The loop NEVER cancels itself: with a signal it re-arms on the
      // display clock, without one on the idle timer. It used to stop after
      // ~2 s of silent frames and restart only on a `playing` PROP CHANGE —
      // so a buffering stall, a seek, a gapless element swap or a silent
      // passage killed the strip for the rest of the session (track changes
      // keep `playing` true), and the first play (suspended context) never
      // recovered.
      onTimer = synthetic;
      handle = synthetic ? window.setTimeout(tick, IDLE_MS) : requestAnimationFrame(tick);
      if (!w || !h) return;

      // A canvas resize blanks the backing store by itself, so a resized
      // frame is always repainted (see the idle skip below).
      const resized = w !== lastW;
      if (resized) {
        canvas.width = Math.round(w * dpr);
        canvas.height = Math.round(h * dpr);
        lastW = w;
      }

      // Idle: nothing to animate, so the strip eases onto its baseline and
      // then stops repainting altogether — a paused canvas costs the timer
      // and nothing else. The skipped frames must not CLEAR either: the
      // canvas is composited at the display rate, so clearing and returning
      // left the strip blank for 59 of every 60 frames while paused
      // (measured: 0 painted pixels on 7 of 8 samples; the baseline showed
      // only as a 4 Hz flash). An idle frame leaves the previous one on
      // screen.
      const now = performance.now();
      if (synthetic && !resized) {
        if (now - lastIdle < IDLE_MS) return;
        if (atRest(levels.current, peaks.current, n)) {
          lastIdle = now;
          return;
        }
      }
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
          target = Math.min(1, Math.max(0, (target - 0.5) * CONTRAST + 0.5));
          // Per-band rolling peak → bounded lift (≤2×, never attenuation).
          const roll = Math.max(target, (norm.current[i] ?? 0) * PEAK_DECAY);
          norm.current[i] = roll;
          target = Math.min(1, target * Math.min(2, Math.max(1, PEAK_REF / Math.max(roll, 0.06))));
        } else {
          // No real signal (paused, idle, or unobservable stream): hold a
          // flat near-zero baseline. The strip must only ever draw actual
          // audio — never synthesized motion or noise.
          target = BASE;
        }
        // Fast attack, slower release: the rise is what a kick is, the fall is
        // what lets the eye follow one band instead of a wall.
        const cur = levels.current[i] ?? 0;
        levels.current[i] = cur + (target - cur) * (target > cur ? ATTACK : RELEASE);
        // Peak caps with gravity. The 0.008/frame fall was PLAYBACK gravity:
        // idle frames only repaint at 4 Hz, so the same constant left the caps
        // hanging at half height above collapsed bars for ~20 s. With no signal
        // they instead fall with the bars (the same release easing) until they
        // meet the baseline.
        const pk = peaks.current[i] ?? 0;
        peaks.current[i] = synthetic
          ? Math.max(levels.current[i], pk + (levels.current[i] - pk) * RELEASE)
          : Math.max(levels.current[i], pk - PEAK_FALL);
      }

      // ---- draw ------------------------------------------------------
      const gap = Math.max(1.5, w / n * 0.28);
      const bw = Math.max(1.5, (w - gap * (n - 1)) / n);
      const base = h;
      // ONE ramp for the whole strip, anchored to the canvas instead of
      // rebuilt per bar: the old code allocated a CanvasGradient for every
      // bar on every frame (56 throwaway objects at 60 fps). The ramp still
      // runs from the quiet colour at the baseline to the bright one at the
      // top of the strip, so a taller bar now carries MORE of it — the
      // meter reads level as brightness too, and the peak caps stay bright
      // against it.
      const ramp = ctx.createLinearGradient(0, base, 0, base - Math.max(1, h - 2));
      ramp.addColorStop(0, `rgb(${accent} / 0.5)`);
      ramp.addColorStop(1, `rgb(${accentSoft} / 0.95)`);
      for (let i = 0; i < n; i++) {
        const v = Math.max(0.02, levels.current[i] ?? 0);
        const x = i * (bw + gap);
        const bh = Math.max(2, v * (h - 2));
        const r = Math.min(bw / 2, 2);
        // Halo behind the loudest bars: a wider, faint rounded rect instead of
        // shadowBlur, which would re-blur the whole strip every frame.
        if (motion && v > GLOW_AT) {
          const pad = gap * 1.2;
          ctx.fillStyle = `rgb(${accent} / ${(0.08 + (v - GLOW_AT) * 0.4).toFixed(2)})`;
          ctx.beginPath();
          ctx.roundRect(x - pad, base - bh - pad, bw + pad * 2, bh + pad, r + pad);
          ctx.fill();
        }
        ctx.fillStyle = ramp;
        ctx.beginPath();
        ctx.roundRect(x, base - bh, bw, bh, r);
        ctx.fill();
        // falling peak cap
        const pk = peaks.current[i] ?? 0;
        if (pk > 0.02) {
          const py = base - Math.max(2, pk * (h - 2)) - 2;
          ctx.fillStyle = `rgb(${accent} / 0.9)`;
          ctx.fillRect(x, py, bw, 2);
        }
      }
    };

    handle = requestAnimationFrame(tick);
    return () => {
      if (onTimer) window.clearTimeout(handle);
      else cancelAnimationFrame(handle);
    };
  }, []);

  return <canvas ref={canvasRef} className={className} aria-hidden />;
}
