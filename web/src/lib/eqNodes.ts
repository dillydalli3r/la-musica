import type { EqBand } from "../api";

/** The WebAudio graph the PLAYER applies an Equalizer APO profile with, and the
 *  response maths the editor draws from.
 *
 *  It lives beside lib/analyser.ts because it hangs off the same graph: the
 *  analyser module owns one AudioContext per page and one
 *  source → ReplayGain gain → analyser → speakers chain per media element, and
 *  an installed EQ is spliced in between the gain and the analyser, so what the
 *  visualizer reads is what you hear.
 *
 *  APO → biquad mapping, and the two places it is honestly not the same as an
 *  export (mlo.eq renders the ffmpeg chain):
 *
 *  | APO | ffmpeg (export)   | WebAudio (playback) |
 *  |-----|-------------------|---------------------|
 *  | PK  | equalizer t=q     | peaking             |
 *  | LS/HSC shelf | bass/treble t=q:w=Q | lowshelf/highshelf |
 *  | LP/HP | lowpass/highpass t=q:w=Q | lowpass/highpass |
 *  | BP/NO | bandpass/bandreject t=q:w=Q | bandpass/notch |
 *
 *  A peaking band, a pass and a notch are the same filter on both sides. A
 *  SHELF is not quite: APO's custom slope (LSC/HSC) reaches ffmpeg as a Q,
 *  while a WebAudio shelf is fixed-slope and ignores Q — so a shelf's width is
 *  the one number a listener may hear differently in the app than in an export.
 *  Everything else about the curve (which bands, at what frequency, with what
 *  gain) is identical, and it is the SHAPE the profile is about. */
const BIQUAD: Record<string, BiquadFilterType> = {
  PK: "peaking",
  LS: "lowshelf",
  LSC: "lowshelf",
  HS: "highshelf",
  HSC: "highshelf",
  LP: "lowpass",
  HP: "highpass",
  BP: "bandpass",
  NO: "notch",
};

/** The types whose whole effect is their shape: a pass or a notch has no gain
 *  to set (mlo.eq's `_GAIN_TYPES` is the complement of this list). */
const SHAPE_ONLY = new Set(["LP", "HP", "BP", "NO"]);

/** Every type a profile may carry, in the order an editor should offer them —
 *  the common peaking band first, then the shelves, then the shapes. */
export const EQ_TYPES = ["PK", "LS", "HS", "LSC", "HSC", "LP", "HP", "BP", "NO"] as const;

/** What a band added by hand starts as: an inaudible peaking filter in the
 *  middle of the spectrum, so a new row is a handle to drag, not a change to
 *  the sound before it is touched. */
export const EQ_NEW_BAND: EqBand = { type: "PK", fc: 1000, gain: 0, q: 1.41, on: true };

/** The frequency range an editor should draw and clamp to: below 20 Hz is
 *  inaudible and above 20 kHz is past every profile in the wild. */
export const EQ_FC_MIN = 20;
export const EQ_FC_MAX = 20000;
/** Gain headroom per band. APO itself allows ±20 dB; a curve that needs more
 *  is a curve that will clip, and the preamp is the right answer there. */
export const EQ_GAIN_LIMIT = 20;
export const EQ_PREAMP_LIMIT = 24;
/** The Q a rendered band is clamped to — the width outside this range is not a
 *  band any profile means. */
const Q_MIN = 0.1;
const Q_MAX = 30;
/** These four bounds are a CONTRACT with the exporter, not a local choice: a
 *  profile's Fc/gain/Q/preamp are clamped to exactly the same numbers when
 *  mlo.eq renders the ffmpeg chain (its FC_MIN_HZ…PREAMP_LIMIT_DB), so a band
 *  outside them cannot sound wider or louder in the app than in an export. A
 *  file's own out-of-range value is kept in the profile and reported at import
 *  (mlo.eq's notes); here it is clamped, because the node and the editor's own
 *  boxes are bounded. */

export type EqChain = {
  /** Where the signal enters (the preamp gain, or the first band without one). */
  head: AudioNode;
  /** Where it leaves. */
  tail: AudioNode;
  /** Everything built, for disconnecting on the next install. */
  nodes: AudioNode[];
};

function clamp(value: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, Number.isFinite(value) ? value : lo));
}

/** `type` as an APO type, upper-cased; "" when the profile carries one this
 *  graph has no filter for (a corrupt row must not throw mid-playback). */
export function eqType(type: unknown): string {
  const key = String(type ?? "").trim().toUpperCase();
  return BIQUAD[key] ? key : "";
}

/** Whether a band contributes anything at all: an OFF band never does, and so
 *  does no an on gain band set to 0 dB — it is transparent, and mlo.eq skips
 *  exactly the same ones when it renders an export. */
export function eqBandActive(band: EqBand): boolean {
  if (!band.on || !eqType(band.type)) return false;
  return SHAPE_ONLY.has(eqType(band.type)) || Number(band.gain) !== 0;
}

function configure(node: BiquadFilterNode, band: EqBand, type: string) {
  node.type = BIQUAD[type];
  node.frequency.value = clamp(Number(band.fc), EQ_FC_MIN, EQ_FC_MAX);
  // A shelf and a pass ignore Q in WebAudio; setting it is harmless and keeps
  // the node's own state the profile's state if it is ever read back.
  node.Q.value = clamp(Number(band.q) || 0.707, Q_MIN, Q_MAX);
  node.gain.value = SHAPE_ONLY.has(type) ? 0 : clamp(Number(band.gain), -EQ_GAIN_LIMIT, EQ_GAIN_LIMIT);
}

/** The sentence a profile that parsed WITH errors is refused with, in the same
 *  words the export fails a track with (mlo.eq's `apply_refusal`) and the same
 *  ones the editor's banner shows (`pages/EqualizerPage.tsx`): a band that
 *  could not be read means the bands that did are not the curve the file wrote,
 *  so the player must not install them either. "" when the profile may play.
 *  `PlayerBar` is where the row's own profile is installed, so it is the caller
 *  that refuses one of these instead of handing its bands to `applyEq`. */
export function eqApplyRefusal(profile: { errors?: string[] } | null | undefined): string {
  const first = (profile?.errors ?? [])[0];
  return first ? `this profile cannot be applied: ${first}` : "";
}

/** Build the chain for one profile: preamp first (a boosted curve that is not
 *  pulled down first clips), then every active band in the profile's own
 *  order. `null` when the profile changes nothing — a no-op profile must not
 *  add nodes to the graph. */
export function buildEqChain(ctx: BaseAudioContext, filters: EqBand[], preampDb: number): EqChain | null {
  const preamp = clamp(Number(preampDb) || 0, -EQ_PREAMP_LIMIT, EQ_PREAMP_LIMIT);
  const bands = (filters ?? []).filter(eqBandActive);
  if (!preamp && !bands.length) return null;
  const nodes: AudioNode[] = [];
  let head: AudioNode | null = null;
  let tail: AudioNode | null = null;
  const push = (node: AudioNode) => {
    if (!head) head = node;
    if (tail) tail.connect(node);
    tail = node;
    nodes.push(node);
  };
  if (preamp) {
    const gain = ctx.createGain();
    gain.gain.value = Math.pow(10, preamp / 20);
    push(gain);
  }
  for (const band of bands) {
    const type = eqType(band.type);
    const node = ctx.createBiquadFilter();
    configure(node, band, type);
    push(node);
  }
  return head && tail ? { head, tail, nodes } : null;
}

/** The chain's magnitude response in dB at each frequency — the curve the
 *  editor draws.
 *
 *  Summed from each band's OWN `getFrequencyResponse`, which is the browser's
 *  own biquad maths rather than a second implementation of it: a cascade's
 *  magnitude is the product of its parts, which in dB is their sum, so this is
 *  the chain's response exactly. `ctx` may be an OfflineAudioContext — nothing
 *  is connected to a destination and no audio is rendered. */
export function eqResponseDb(
  ctx: BaseAudioContext,
  filters: EqBand[],
  preampDb: number,
  freqs: Float32Array<ArrayBuffer>,
): Float32Array {
  const out = new Float32Array(freqs.length).fill(
    clamp(Number(preampDb) || 0, -EQ_PREAMP_LIMIT, EQ_PREAMP_LIMIT));
  const mag = new Float32Array(freqs.length);
  const phase = new Float32Array(freqs.length);
  for (const band of (filters ?? []).filter(eqBandActive)) {
    const type = eqType(band.type);
    const node = ctx.createBiquadFilter();
    configure(node, band, type);
    node.getFrequencyResponse(freqs, mag, phase);
    for (let i = 0; i < out.length; i++) {
      out[i] += 20 * Math.log10(Math.max(mag[i], 1e-6));
    }
  }
  return out;
}

/** The band's own response in dB, for the editor's per-band highlight. */
export function eqBandResponseDb(
  ctx: BaseAudioContext,
  band: EqBand,
  freqs: Float32Array<ArrayBuffer>,
): Float32Array {
  return eqResponseDb(ctx, [band], 0, freqs);
}

/** Numbers the way an APO file carries them: enough precision to keep the
 *  curve, no trailing noise (`105` not `105.0000`, `-6.3` not `-6.3000001`). */
function num(value: number, digits = 2): string {
  const v = Number.isFinite(value) ? value : 0;
  const fixed = v.toFixed(digits);
  return fixed.replace(/\.?0+$/, "") || "0";
}

/** A profile's bands as Equalizer APO text — what `POST /api/export/eq/import`
 *  stores, and what makes the editor able to save at all (the store is APO
 *  text, so an edit has to be RENDERED back into it).
 *
 *  What comes out is the curve in the editor, not the file it was read from: a
 *  profile imported from a `GraphicEQ` list (or from Peace with a `FilterCurve`
 *  line, or with `Include:` lines) is stored as the peaking/shelf bands those
 *  lines were converted to, and the original lines are gone. The magnitude is
 *  the same curve — the conversion is the one the importer already documented —
 *  but the TEXT is this editor's. A band whose `type` is not an APO type is
 *  written as PK so a corrupt row cannot make the file unreadable. */
export function eqToApoText(filters: EqBand[], preampDb: number): string {
  const lines: string[] = [];
  const preamp = clamp(Number(preampDb) || 0, -EQ_PREAMP_LIMIT, EQ_PREAMP_LIMIT);
  if (preamp) lines.push(`Preamp: ${num(preamp)} dB`);
  const bands = (filters ?? []).filter((b) => eqType(b.type));
  bands.forEach((band, i) => {
    const type = eqType(band.type);
    lines.push(
      `Filter ${i + 1}: ${band.on ? "ON" : "OFF"} ${type} ` +
      `Fc ${num(Number(band.fc), 1)} Hz ` +
      `Gain ${type && SHAPE_ONLY.has(type) ? "0" : num(Number(band.gain))} dB ` +
      `Q ${num(Number(band.q) || 0.707, 2)}`
    );
  });
  return lines.join("\n") + (lines.length ? "\n" : "");
}
