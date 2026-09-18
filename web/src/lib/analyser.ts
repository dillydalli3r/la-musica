/** Shared WebAudio graph for playback: ReplayGain loudness + the visualizer.
 *
 * A single AudioContext holds one MediaElementSource per media element
 * (<audio> AND the music-video <video> — a media element can only ever be
 * attached once) routed through a GainNode (ReplayGain) into an
 * AnalyserNode back to the speakers. The stream responses carry CORS
 * headers and the elements are loaded with crossOrigin="anonymous", so the
 * frequency data is never tainted-silence.
 *
 * Everything is defensive: if WebAudio is unavailable or the context
 * can't run, callers fall back to the synthetic animation and unity gain
 * instead of ever risking playback itself.
 */

let ctx: AudioContext | null = null;
let current: AnalyserNode | null = null;
let broken = false;

/** Decibel window of every analyser we build, in dBFS. getByteFrequencyData
 *  squeezes THIS window into 0..255, so the mapping that turns bytes back
 *  into bar heights (Visualizer.tsx) undoes exactly these two constants —
 *  hence they are exported instead of duplicated. The browser defaults
 *  (−100..−30) put the top of the byte range at −30 dBFS, which is below
 *  what a loud master leaves in most FFT bands: nearly every byte pinned at
 *  255 and the strip drew one solid block with no shape. −10 dBFS is above
 *  the per-band level of a full-scale mix, so loud material still has range.
 */
export const MIN_DB = -90;
export const MAX_DB = -10;

type Chain = { analyser: AnalyserNode; gain: GainNode };

/** The graph is stored ON the element. A media element can be attached to
 *  exactly one MediaElementSourceNode ever, so keeping the reference here
 *  (rather than only in module state) is what lets a hot-reload re-import —
 *  fresh module state, element still holding the previous context's source —
 *  find the running graph and reuse it, instead of calling
 *  createMediaElementSource again and throwing InvalidStateError until a full
 *  page reload. The context comes along because the old module's `ctx`
 *  variable is gone after a reload and the graph's context is still live. */
type Attached = HTMLMediaElement & {
  __mloAnalyser?: { ctx: AudioContext; chain: Chain };
};

function ensureCtx(): AudioContext | null {
  if (ctx) return ctx;
  if (broken) return null;
  try {
    const AC =
      window.AudioContext ||
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!AC) {
      broken = true;
      return null;
    }
    ctx = new AC();
  } catch {
    broken = true;
    ctx = null;
  }
  return ctx;
}

/** Attach the source → ReplayGain gain → analyser → speakers chain to an
 * <audio> or <video> element and make it the active visualizer source.
 * Idempotent: a second call for the same element reuses the graph (and an
 * attach in progress can't happen twice). Resumes the context it just
 * created, so the caller does not have to order resume-after-attach. */
export function attachAnalyser(el: HTMLMediaElement): AnalyserNode | null {
  // Widening cast to the element tag: optional property, we are its only writer.
  const tagged: Attached = el;
  const have = tagged.__mloAnalyser;
  if (have) {
    // Already attached — by this module, by a hot reload, or by the other
    // attach path (the gapless pair, the video popout). Reuse, don't retry.
    ctx = have.ctx;
    current = have.chain.analyser;
    resumeAnalyser();
    return current;
  }
  // A cross-origin element without crossOrigin="anonymous" would be routed
  // into SILENCE by the media-element source (tainted stream) — worse than
  // having no meter, so leave it alone.
  const src = el.currentSrc || el.src;
  if (el.crossOrigin !== "anonymous" && src) {
    try {
      if (new URL(src, location.href).origin !== location.origin) return current;
    } catch {
      return current;
    }
  }
  const c = ensureCtx();
  if (!c) return null;
  try {
    const source = c.createMediaElementSource(el);
    const gain = c.createGain();
    const analyser = c.createAnalyser();
    analyser.fftSize = 1024;
    // 0.68 rather than the old 0.78: the analyser's own frame-to-frame
    // smoothing stacks with the visualizer's easing, and at 0.78 transients
    // arrived already flattened. Still well above raw (0 = jitter), so the
    // bars read as motion, not noise. The ambience reads the same node.
    analyser.smoothingTimeConstant = 0.68;
    analyser.minDecibels = MIN_DB;
    analyser.maxDecibels = MAX_DB;
    source.connect(gain);
    gain.connect(analyser);
    analyser.connect(c.destination);
    tagged.__mloAnalyser = { ctx: c, chain: { analyser, gain } };
  } catch {
    // This element can't be metered (already attached by a graph we can't
    // see, or unplayable media). Keep whatever analyser is active and keep
    // WebAudio itself usable: latching `broken` here would kill the meter
    // for every later track because one element failed.
    return current;
  }
  current = tagged.__mloAnalyser.chain.analyser;
  resumeAnalyser();
  return current;
}

/** Apply a ReplayGain preamp (dB, e.g. −7.20) to an element's playback
 * chain. Unity (0 dB) when no value is given. Smoothed to avoid clicks.
 * No-op for an element that has no graph yet (it is re-applied on attach). */
export function applyReplayGain(el: HTMLMediaElement, db?: number | null) {
  // Optional property we are the only writer of — the cast widens the
  // element type, no unchecked read.
  const tag: Attached = el;
  if (!tag.__mloAnalyser) return;
  const { chain, ctx: graphCtx } = tag.__mloAnalyser;
  const v =
    typeof db === "number" && isFinite(db)
      ? Math.pow(10, Math.max(-24, Math.min(24, db)) / 20)
      : 1;
  try {
    chain.gain.gain.setTargetAtTime(v, graphCtx.currentTime, 0.05);
  } catch {
    /* never let loudness matching break playback */
  }
}

/** Resume the shared context. Safe to call from anywhere (a suspended
 * context routes audio into silence); a no-op before the context exists —
 * attachAnalyser() resumes the one it creates. */
export function resumeAnalyser() {
  if (ctx && ctx.state === "suspended") ctx.resume().catch(() => {});
}

/** The analyser to read now, or null when WebAudio isn't available. */
export function activeAnalyser(): AnalyserNode | null {
  return current;
}
