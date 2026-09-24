import { useEffect, useRef } from "react";
import { MAX_DB, MIN_DB, activeAnalyser } from "../lib/analyser";
import { currentAccent, subscribeAccent } from "../lib/accent";

/** Height-mapping constants, all in dBFS. FLOOR_DB is the empty baseline,
 *  CEIL_DB the full height (full scale), TILT_DB the lift of the top band
 *  over the bottom one.
 *
 *  Tuned by MEASURING four candidate mappings over two seconds of a real
 *  track, on the bytes this analyser actually produces (treble in music is
 *  20-40 dB below the bass, so without a lift the right half is dead):
 *
 *    tilt 30, contrast 1.25, 2x per-band peak lift   right/left 0.99  spread 0.112  movement 0.0084
 *    tilt 30, contrast 1.5,  no lift                 right/left 0.86  spread 0.306  movement 0.0098
 *    tilt 24, contrast 1.6,  no lift                 right/left 0.72  spread 0.422  movement 0.0101  <-- this
 *    tilt 30, contrast 1.5, 1.15x lift + envelope    right/left 0.95  spread 0.156  movement 0.0094
 *
 *  The first row is what the strip used to be, and it is the user's
 *  "ascending triangle that gets stuck": the per-band peak lift pulled every
 *  quiet (treble) band halfway to a common reference, so the right side
 *  measured as loud as the left however the music moved, and the strip barely
 *  changed shape. The lift is GONE (the shape is the spectrum's own), the
 *  tilt keeps the top octave visible, and contrast 1.6 turns the real
 *  variation into height. */
const FLOOR_DB = -90;
const CEIL_DB = 0;
const TILT_DB = 24;

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
const CONTRAST = 1.6;
/** Level above which a bar gets its halo — the decorative motion that is
 * skipped under prefers-reduced-motion. */
const GLOW_AT = 0.55;

/** Are the bars already sitting on the idle baseline? Then there is nothing
 *  left to ease and nothing to redraw: a paused strip should cost its timer
 *  and nothing else, not the same flat line ten times a second. */
function atRest(levels: Float32Array, n: number): boolean {
  for (let i = 0; i < n; i++) {
    if (Math.abs((levels[i] ?? 0) - BASE) > REST_EPS) return false;
  }
  return true;
}

/** Frequency-bar visualizer driven ONLY by the shared WebAudio analyser.
 * Bars use logarithmic band mapping — music's energy lives in the low
 * octaves, a linear split makes the left half dance and the right half
 * sit flat. With no live signal (paused, idle, unobservable stream) it
 * holds a flat baseline: the strip never invents motion or noise.
 *
 * No peak caps: the falling bars themselves are the meter, and a row of
 * detached lines floating above them read as clutter rather than as
 * information.
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
 * There is deliberately NO per-band peak normalisation any more. It used to
 * lift quiet bands up to 2× toward a common reference, which measured as
 * right/left = 0.99 on real material — a flat, slightly ascending strip that
 * barely changed shape (the reported "ascending triangle that gets stuck"),
 * because the lift cancelled the spectrum's own slope. What is drawn now is
 * the spectrum: bass high, treble falling away, cymbals and snares spiking.
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
export default function Visualizer({ playing, className = "", ink }: {
  playing: boolean;
  className?: string;
  /** The ink to draw the bars with, when the strip sits ON something whose
   *  brightness is not the app's own dark surface.
   *
   *  The fullscreen player paints its text straight onto the artwork and picks
   *  its ink per cover (`npInk`) — white over a dark field, near-black over a
   *  bright one — and the bars follow the SAME table. They used to draw with
   *  the app's accent (white), which is exactly "text that blends into the
   *  background" on a white cover: a white strip on a white field. Left out
   *  (the docked sidebar, whose surface IS the app's), the accent is right. */
  ink?: "light" | "dark";
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const levels = useRef<Float32Array>(new Float32Array(BARS));
  const playingRef = useRef(playing);
  useEffect(() => {
    playingRef.current = playing;
  }, [playing]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    // The strip's own palette: the app's accent, or — when the caller says the
    // bars are drawn over ARTWORK — the same two tones the lyrics use, so a
    // bright cover gets dark bars and a dark one gets white (see `ink`).
    //
    // Read from the live custom properties every time, including on an accent
    // change: the tones used to be read ONCE here and cached in the closure, so
    // a colour picked in Settings left an already-mounted strip painting the
    // old one until a reload.
    const palette = () => {
      const [accentRoot, accentSoftRoot] = currentAccent();
      return ink === "dark"
        ? (["9 9 11", "63 63 70"] as const)
        : ink === "light"
        ? (["255 255 255", "244 244 245"] as const)
        : ([accentRoot, accentSoftRoot] as const);
    };
    let [accent, accentSoft] = palette();
    // Read once: a matchMedia change mid-session is not worth a listener for a
    // halo this small (the ambient background re-reads its own on every open).
    const motion = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    let handle = 0;
    let onTimer = false;
    let freq: Uint8Array | null = null;
    let lastIdle = 0;
    // A frame the accent change asked for even though the strip is idle: an
    // idle strip skips repainting by design (see the idle skip below), and a
    // paused one at rest draws nothing at all, so without this the new colour
    // would only arrive with the next track.
    let repaint = false;
    const unsubscribe = subscribeAccent(() => {
      [accent, accentSoft] = palette();
      repaint = true;
      // An idle strip is asleep for IDLE_MS; wake it now, replacing that one
      // timer rather than adding a second loop beside it.
      if (onTimer) {
        window.clearTimeout(handle);
        handle = window.setTimeout(tick, 0);
      }
    });

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
      // frame is always repainted (see the idle skip below). The check is on
      // the BACKING STORE, not on the CSS width: a height change (the strip's
      // box, a zoomed pane) or a devicePixelRatio change (the window dragged
      // to another monitor, a browser zoom step) used to leave the old bitmap
      // in place, and the browser then stretched it into the new box — two
      // offset rows of bars, the reported "messed up" strip. Comparing what
      // the canvas actually holds catches all three; width alone missed two.
      const backW = Math.round(w * dpr);
      const backH = Math.round(h * dpr);
      const resized = backW !== canvas.width || backH !== canvas.height;
      if (resized) {
        canvas.width = backW;
        canvas.height = backH;
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
      if (synthetic && !resized && !repaint) {
        if (now - lastIdle < IDLE_MS) return;
        if (atRest(levels.current, n)) {
          lastIdle = now;
          return;
        }
      }
      repaint = false;
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
      }

      // ---- draw ------------------------------------------------------
      const gap = Math.max(1.5, w / n * 0.28);
      const bw = Math.max(1.5, (w - gap * (n - 1)) / n);
      const base = h;
      // ONE ramp for the whole strip, anchored to the canvas instead of
      // rebuilt per bar: the old code allocated a CanvasGradient for every
      // bar on every frame (56 throwaway objects at 60 fps). The ramp runs
      // from the quiet colour at the baseline to the bright one at the top of
      // the strip, so a taller bar carries MORE of it — the meter reads level
      // as brightness as well as height.
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
      }
    };

    handle = requestAnimationFrame(tick);
    return () => {
      unsubscribe();
      if (onTimer) window.clearTimeout(handle);
      else cancelAnimationFrame(handle);
    };
    // `ink` is a dependency and not a mount-time constant: the polarity is
    // decided per COVER, so a track change can turn a dark strip into a light
    // one — the loop restarts with the new palette (the levels it eases stay
    // in their ref, so the strip does not jump back to the baseline).
  }, [ink]);

  return <canvas ref={canvasRef} className={className} aria-hidden />;
}
