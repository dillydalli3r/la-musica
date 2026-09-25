/** Shared WebAudio graph for playback: ReplayGain loudness + the equalizer + the visualizer.
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

import { buildEqChain, type EqChain } from "./eqNodes";
import type { EqBand } from "../api";

let ctx: AudioContext | null = null;
let current: AnalyserNode | null = null;
// A WebAudio failure is retried rather than latched for ever: a browser that
// refuses to build a context for a moment (too many contexts across tabs, a
// policy hiccup on a page that is not focused yet) used to cost the meters and
// the equalizer for the whole session. The cool-down is what keeps a retry
// from becoming one attempt per animation frame.
let brokenUntil = 0;
const BROKEN_RETRY_MS = 5000;

/** Every element carrying a graph, oldest attach first. The `<audio>` pair and
 *  the video popout each own one, and only one of them is making sound at a
 *  time — but `current` is the LAST attach, which is not always the one
 *  PLAYING (a gapless handover attaches the next element early, a popout
 *  attaches the <video> the <audio> never stops feeding). Reading the wrong
 *  element returns an all-zero spectrum, so both meters sat on their synthetic
 *  fallback until something re-attached. The registry is what lets the read
 *  follow the sound instead of the attach order. */
const graphs = new Set<HTMLMediaElement>();

/** The equalizer profile the player is applying (see lib/eqNodes.ts), or null
 *  while none is installed. Module state on purpose: it is what an element
 *  attached later has to inherit, so a gapless handover does not drop the
 *  curve mid-album. */
let eqProfile: { filters: EqBand[]; preampDb: number } | null = null;

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

type Chain = {
  analyser: AnalyserNode;
  /** ReplayGain loudness. */
  gain: GainNode;
  /** The app's own volume slider. A second stage on purpose: iOS IGNORES
   *  `HTMLMediaElement.volume` (the property is read-only there, so assigning
   *  it is silently dropped), and the level the user sets is only heard when
   *  it is applied inside the graph the element is routed through. On every
   *  other platform both stages work and `applyVolume` keeps them in step. */
  volume: GainNode;
  eq?: EqChain | null;
};

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
  if (Date.now() < brokenUntil) return null;
  try {
    const AC =
      window.AudioContext ||
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!AC) {
      brokenUntil = Date.now() + BROKEN_RETRY_MS;
      return null;
    }
    ctx = new AC();
    armContext(ctx);
  } catch {
    brokenUntil = Date.now() + BROKEN_RETRY_MS;
    ctx = null;
  }
  return ctx;
}

/** Keep the shared context RUNNING where the platform parks it.
 *
 *  Every element this module attaches routes its audio through the graph, so a
 *  context that is not running is a track that is not heard — and iOS parks a
 *  context for reasons that have nothing to do with the page: it starts
 *  "suspended" until a user gesture unlocks it, and a call, an alarm or another
 *  app taking the audio session parks it as "interrupted". `activeAnalyser`
 *  already re-asks on every meter frame, but a suspended context also stops the
 *  element's own clock from advancing — the meter is not the point, the sound
 *  is — so the two events that can bring it back are wired here as well: the
 *  context telling us it changed state, and the app coming back to the
 *  foreground.
 *
 *  `resumeAnalyser` is the only writer and it is a no-op unless something is
 *  really parked, so this cannot fight a healthy context. */
function armContext(c: AudioContext) {
  try {
    c.addEventListener("statechange", () => {
      // Resuming is only worth asking for while the page is on screen: a
      // backgrounded page that reopens the audio graph is exactly what iOS
      // suspends it for.
      if (document.visibilityState === "visible") resumeAnalyser();
    });
  } catch {
    /* no event API on this context: the gesture unlock below still runs */
  }
}

// The FIRST user gesture is what unlocks WebAudio on iOS: a context created
// outside one (the load path attaches the graph before the element is played)
// starts suspended, and only a `resume()` issued FROM a gesture — or a later
// one — gets it running. One listener per event kind, capture phase, passive:
// this must never intercept or delay anything the app does with the gesture.
// They stay armed for the life of the page: a context that is parked again
// (an interruption) is unlocked again by the next touch.
//
// `addEventListener` is checked for being a FUNCTION, not just for `document`
// existing: the repo's own SSR checks (`tools/check_lyrics_kind.mjs` and its
// siblings) import these modules through Vite's Node module runner, where a
// `document` stub exists and arming a listener would throw on load — a check
// harness is not a browser, and this module must load in one.
if (typeof document !== "undefined" && typeof document.addEventListener === "function") {
  const unlock = () => resumeAnalyser();
  for (const ev of ["pointerdown", "touchend", "keydown"] as const) {
    document.addEventListener(ev, unlock, { capture: true, passive: true });
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") resumeAnalyser();
  });
}

/** Attach the source → ReplayGain gain → analyser → speakers chain to an
 * <audio> or <video> element and make it the active visualizer source.
 * Idempotent: a second call for the same element reuses the graph (and an
 * attach in progress can't happen twice). Resumes the context it just
 * created, so the caller does not have to order resume-after-attach. */
export function attachAnalyser(el: HTMLMediaElement): AnalyserNode | null {
  // Widening cast to the element tag: optional property, we are its only writer.
  const tagged: Attached = el;
  graphs.add(el);
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
    // The volume stage sits FIRST, before the ReplayGain gain: it is the
    // listener's own level, and it must multiply whatever loudness matching
    // installed rather than be replaced by it.
    const volume = c.createGain();
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
    source.connect(volume);
    volume.connect(gain);
    gain.connect(analyser);
    analyser.connect(c.destination);
    const chain: Chain = { analyser, gain, volume, eq: null };
    tagged.__mloAnalyser = { ctx: c, chain };
    // An element attached AFTER a profile was installed (the gapless pair's
    // other half, a video popout) must start with the same curve the playing
    // element has, or a handover would drop the equalizer mid-album.
    if (eqProfile) installEq(chain, eqProfile);
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
 * chain. Unity (0 dB) when no value is given. No-op for an element that has
 * no graph yet.
 *
 * The value is STEPPED onto a paused element and RAMPED onto a running one:
 * a ramp is what stops a mid-song preamp drag from clicking, but it would
 * also leak the old gain into the first tens of milliseconds of a track
 * whose gain is being installed for the first time — the loud start this
 * stage exists to prevent. ``immediate`` says "installing before playback"
 * for the caller that knows it (a track load, a gapless handover reusing the
 * element that was just playing); everywhere else a paused element is
 * stepped too, since it cannot be making sound. */
export function applyReplayGain(el: HTMLMediaElement, db?: number | null,
                                immediate = false) {
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
    if (immediate || el.paused) {
      chain.gain.gain.cancelScheduledValues(graphCtx.currentTime);
      chain.gain.gain.setValueAtTime(v, graphCtx.currentTime);
    } else {
      chain.gain.gain.setTargetAtTime(v, graphCtx.currentTime, 0.05);
    }
  } catch {
    /* never let loudness matching break playback */
  }
}

/** The app's own volume, applied where the platform will actually hear it.
 *
 *  `HTMLMediaElement.volume` is READ-ONLY on iOS: assigning it is dropped
 *  without an error, so a slider that only wrote that property did nothing at
 *  all on a phone (and a set element still played at full scale). The element
 *  is routed through this module's graph whenever a graph exists, so the level
 *  goes on the graph's own volume stage and `el.volume` is pinned to unity —
 *  the two must never multiply, or 50 % would be heard as 25 %.
 *
 *  An element with no graph (the lyrics previews, a browser without WebAudio)
 *  keeps the plain property, which is correct everywhere it works. */
export function applyVolume(el: HTMLMediaElement, vol: number) {
  const v = Number.isFinite(vol) ? Math.max(0, Math.min(1, vol)) : 1;
  const tag: Attached = el;
  const graph = tag.__mloAnalyser;
  if (!graph) {
    try { el.volume = v; } catch { /* never let a level break playback */ }
    return;
  }
  try {
    el.volume = 1;
    // A short ramp rather than a step: the same slider that clicks in the
    // graph would click at the speakers, and a drag sends one of these per
    // pixel.
    graph.chain.volume.gain.setTargetAtTime(v, graph.ctx.currentTime, 0.02);
  } catch {
    /* never let a level break playback */
  }
}

/** Resume the shared context. Safe to call from anywhere (a suspended
 * context routes audio into silence); a no-op before the context exists —
 * attachAnalyser() resumes the one it creates.
 *
 * WebKit parks a context as "interrupted" rather than "suspended" (a phone
 * call, another app taking the audio session) and only `resume()` brings it
 * back, so both states are asked about. */
export function resumeAnalyser() {
  if (ctx && (ctx.state === "suspended" || (ctx.state as string) === "interrupted")) {
    ctx.resume().catch(() => {});
  }
}

/** Re-route one chain as gain → [preamp, bands…] → analyser, tearing the
 *  previous equalizer out first. The analyser stays LAST so the meters and the
 *  ambience read the equalized signal — the curve is part of what is playing.
 *  Never throws: an EQ that cannot be built leaves plain playback alone. */
function installEq(chain: Chain, profile: { filters: EqBand[]; preampDb: number }) {
  try {
    chain.gain.disconnect();
    for (const node of chain.eq?.nodes ?? []) node.disconnect();
    chain.eq = null;
    const built = buildEqChain(chain.analyser.context, profile.filters, profile.preampDb);
    if (!built) {
      chain.gain.connect(chain.analyser);
      return;
    }
    chain.gain.connect(built.head);
    built.tail.connect(chain.analyser);
    chain.eq = built;
  } catch {
    /* A profile that cannot be built must never stop the music — and the
       tear-down above already unplugged the gain, while the element's audio
       reaches the speakers through this graph only, so put the direct edge
       back instead of leaving silence. Guarded in turn: the context itself may
       be what failed. */
    try { chain.gain.connect(chain.analyser); } catch { /* nothing left to connect */ }
  }
}

/** Install the playback equalizer on every attached element, and on the ones
 *  attached later. Called with the profile the app's config names — an empty
 *  band list (or a 0 dB preamp) is "no equalizer" and leaves the graph as it
 *  was. */
export function applyEq(filters: EqBand[], preampDb = 0) {
  eqProfile = { filters: filters ?? [], preampDb: Number(preampDb) || 0 };
  for (const el of graphs) {
    const tag = el as Attached;
    if (!tag.__mloAnalyser) continue;
    installEq(tag.__mloAnalyser.chain, eqProfile);
  }
}

/** The profile currently installed, for a UI that wants to show it. */
export function activeEq(): { filters: EqBand[]; preampDb: number } | null {
  return eqProfile;
}

/** The analyser to read now, or null when WebAudio isn't available.
 *
 *  Whichever element is PLAYING answers, not whichever attached last: with a
 *  graph per element, the last attach is regularly the idle half of the
 *  gapless pair, and its spectrum is zeros — the strip and the ambience would
 *  both fall back to their synthetic animation while real audio was playing.
 *  No element playing (paused, idle) keeps the last attach, which is the one
 *  the user just heard and the one a resume re-attaches anyway. */
export function activeAnalyser(): AnalyserNode | null {
  // A context the browser suspended on its own (tab in the background, iOS
  // inactivity, a phone call) routes audio into silence AND reads as zeros,
  // which is the other way a working meter looks dead — and asking has to
  // happen BEFORE the early return below, because the element that is still
  // playing is exactly the case that needs it. The resume is a no-op unless
  // the context really is suspended; a browser that wants a gesture refuses.
  resumeAnalyser();
  for (const el of graphs) {
    const tag = el as Attached;
    // `isConnected` keeps a removed element (a video popout that was unmounted
    // mid-track) out of the scan: its `paused` flag can stay false after it
    // leaves the document, and it would then answer for the app for ever.
    if (tag.__mloAnalyser && el.isConnected && !el.paused && !el.ended) {
      return tag.__mloAnalyser.chain.analyser;
    }
  }
  return current;
}

/** How far BEHIND the media element's own clock the sound is, in seconds.
 *
 *  Every element this module attaches is routed through an `AudioContext`
 *  (`createMediaElementSource` → gain → analyser → speakers), and that graph
 *  has a real output delay: the element's `currentTime` says where the decoder
 *  is, while the buffer the speakers are playing was handed to the device
 *  `baseLatency + outputLatency` ago. A lyric pane driven straight off
 *  `currentTime` is therefore ahead of what the listener actually hears — the
 *  owner's "audio in general is de-synced from what the app displays for
 *  synced lyrics" — so the panes read the clock through this correction
 *  (`PlayerBar`'s `getAudioTime`).
 *
 *  Zero for an element with no graph (nothing attached, a browser without
 *  WebAudio): its output goes straight out of the media pipeline, where this
 *  module has no measurement and no business guessing one. Capped at half a
 *  second so a nonsense reading can never throw a lyric pane a whole verse
 *  off; the app's own offset control is the fine adjustment on top. */
export function audibleLatencySec(el?: HTMLMediaElement | null): number {
  const ctxOf = (el as Attached | null | undefined)?.__mloAnalyser?.ctx;
  if (!ctxOf) return 0;
  // `outputLatency` is newer than `baseLatency` and missing on some Safari
  // builds; the sum of what exists is still the right shape of the number.
  const secs =
    (ctxOf.baseLatency || 0) +
    ((ctxOf as AudioContext & { outputLatency?: number }).outputLatency || 0);
  if (!Number.isFinite(secs) || secs <= 0) return 0;
  return Math.min(secs, 0.5);
}
