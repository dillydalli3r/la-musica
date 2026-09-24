/** The app's accent colour — one hue the whole shell is drawn in.
 *
 *  An accent is three CSS custom properties on <html>, each an "r g b" triplet
 *  (index.css declares them; tailwind.config.js wraps them as
 *  `rgb(var(--accent) / <alpha>)` so `bg-accent/15` and friends compile):
 *
 *    --accent       the colour itself — fills, borders, focus rings, links
 *    --accent-soft  the SAME hue, lifted — secondary text on the app's
 *                   near-black surface, and the hover fill of a solid accent
 *                   button (see `.btn-icon-primary` in index.css)
 *    --accent-fg    the ink drawn ON the accent (`.on-accent`) — white or
 *                   black, whichever clears WCAG AA against the accent
 *
 *  `localStorage["mlo.accent"]` holds either a preset id ("violet") or a custom
 *  "#rrggbb". The choice is per-browser and per-device on purpose: it is a
 *  presentation preference, so nothing about it goes to the server config.
 *  `resolveAccent()` answers whichever form it finds and falls back to the
 *  default (black & white) for anything else, so a stale or hand-edited value
 *  can never leave the app with no colour at all.
 *
 *  Every consumer subscribes through `subscribeAccent()` rather than caching a
 *  computed colour: the canvas components (Visualizer, EqCurve) used to read
 *  the custom properties once and keep them, so a live accent change left them
 *  painting the old hue until a reload. `applyAccentVars()` is the one place
 *  the variables are written, and the one place the change is announced.
 */

export type Rgb = [number, number, number];
/** `[accent, soft, fg]`, each an "r g b" triplet — the shape the CSS wants. */
export type Triplets = [string, string, string];

export const INK_ON_ACCENT: Rgb = [255, 255, 255];
export const INK_ON_LIGHT_ACCENT: Rgb = [0, 0, 0];

/** What the app boots with and what an unreadable stored value falls back to:
 *  the black & white theme (`mono`). */
export const DEFAULT_ACCENT = "mono";

/** The swatch grid's presets, in the order the settings page lists them: walked
 *  around the wheel (red → rose) and ending on the black & white theme the app
 *  ships with, so the spread reads at a glance and the default is always the
 *  last swatch. */
export const ACCENT_PRESETS: { id: string; hex: string }[] = [
  { id: "red", hex: "#ef4444" },
  { id: "orange", hex: "#f97316" },
  { id: "amber", hex: "#f59e0b" },
  { id: "yellow", hex: "#eab308" },
  { id: "lime", hex: "#84cc16" },
  { id: "emerald", hex: "#10b981" },
  { id: "teal", hex: "#14b8a6" },
  { id: "sky", hex: "#0ea5e9" },
  { id: "blue", hex: "#3b82f6" },
  { id: "indigo", hex: "#6366f1" },
  { id: "violet", hex: "#8b5cf6" },
  { id: "fuchsia", hex: "#d946ef" },
  { id: "pink", hex: "#ec4899" },
  { id: "rose", hex: "#f43f5e" },
  { id: "mono", hex: "#ffffff" },
];

/** The seven presets the app shipped before the colour picker existed, with
 *  their exact triplets. PINNED, not derived: these are the colours an install
 *  that picked one years ago has been looking at, and the derivation below
 *  lands a few channels away (a lighter violet, a deeper amber) — a visible
 *  change on a hover fill, which is not what "more accent options" may cost.
 *  Only these are literal; every other preset and every custom hex is derived,
 *  so the two can never drift apart in style. */
const SHIPPED: Record<string, Triplets> = {
  violet: ["139 92 246", "167 139 250", "255 255 255"],
  pink: ["236 72 153", "249 168 212", "255 255 255"],
  emerald: ["16 185 129", "110 231 183", "255 255 255"],
  sky: ["14 165 233", "125 211 252", "255 255 255"],
  amber: ["245 158 11", "252 211 77", "24 24 27"],
  red: ["239 68 68", "252 165 165", "255 255 255"],
  mono: ["255 255 255", "212 212 216", "9 9 11"],
};

const HEX_6 = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i;
const HEX_3 = /^#?([0-9a-f])([0-9a-f])([0-9a-f])$/i;

/** A colour typed by a person, or null.
 *
 *  Accepts `#rgb`, `#rrggbb`, and both without the `#` (a phone keyboard's
 *  `#` is a long press away, and a pasted palette entry rarely carries one).
 *  Anything else — 4- or 8-digit alpha forms, `orange`, an empty field — is
 *  null, which is what lets the settings text field say "not a colour yet"
 *  instead of quietly painting the app something else. */
export function parseHexColor(input: string | null | undefined): Rgb | null {
  if (typeof input !== "string") return null;
  const s = input.trim();
  const m6 = HEX_6.exec(s);
  if (m6) return [parseInt(m6[1], 16), parseInt(m6[2], 16), parseInt(m6[3], 16)];
  const m3 = HEX_3.exec(s);
  if (m3) {
    const [r, g, b] = [m3[1], m3[2], m3[3]].map((d) => parseInt(d + d, 16));
    return [r, g, b];
  }
  return null;
}

/** The canonical form of a typed colour — `#rrggbb`, lowercase — or null.
 *  What gets written to localStorage and shown back in the text field, so the
 *  stored value is one shape whatever was typed. */
export function normalizeHex(input: string | null | undefined): string | null {
  const rgb = parseHexColor(input);
  if (!rgb) return null;
  return `#${rgb.map((v) => v.toString(16).padStart(2, "0")).join("")}`;
}

/** An `[r, g, b]` colour as the "r g b" triplet the CSS custom properties hold. */
export function rgbTriplet(rgb: Rgb): string {
  return `${rgb[0]} ${rgb[1]} ${rgb[2]}`;
}

/** WCAG relative luminance (sRGB, 0..1). */
export function relativeLuminance([r, g, b]: Rgb): number {
  const lin = (v: number) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

/** WCAG contrast ratio between two opaque colours (1..21). */
export function contrastRatio(a: Rgb, b: Rgb): number {
  const [hi, lo] = [relativeLuminance(a), relativeLuminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/** The ink for text drawn ON `rgb`.
 *
 *  White or black, whichever of the two the colour contrasts MORE with. Both
 *  extremes are considered rather than a "light background → dark ink"
 *  luminance threshold, because that is what actually holds the promise: the
 *  crossover sits at L ≈ 0.179, where BOTH choices measure 4.58:1, so the
 *  worse of the two picks is still above the 4.5:1 AA floor for normal-size
 *  text — at every possible colour, mid-greys included (a threshold rule
 *  cannot say that: at L 0.18 it would pick white at 4.5:1 and at L 0.19 black
 *  at 4.7:1, but the shipped violet's own white ink measures 4.2:1).
 *
 *  The presets predate the rule and keep their handed-down inks (see SHIPPED);
 *  this is what a custom colour, and any preset added from now on, gets. */
export function inkFor(rgb: Rgb): string {
  const onWhite = contrastRatio(INK_ON_ACCENT, rgb);
  const onBlack = contrastRatio(INK_ON_LIGHT_ACCENT, rgb);
  return rgbTriplet(onWhite >= onBlack ? INK_ON_ACCENT : INK_ON_LIGHT_ACCENT);
}

/** The de-emphasised twin of `rgb`: same hue and saturation, lightness lifted
 *  into the band that reads as secondary text on the app's near-black surface
 *  (L 0.72) while staying visibly the same colour as the accent — and, for a
 *  dark accent, acting as the lighter hover fill of a solid accent button.
 *
 *  Saturation is kept, not boosted, so a muted colour stays muted; a grey has
 *  no hue to keep and comes out a grey of the same value. The 0.88 ceiling
 *  stops a near-white accent (the `mono` theme) from losing its soft twin
 *  entirely: white's soft lands on a light grey, which is what the shipped
 *  black & white theme uses too (zinc-300). */
export function softFor(rgb: Rgb): string {
  const [h, s, l] = rgbToHsl(rgb);
  const soft = hslToRgb(h, s, Math.min(0.88, Math.max(l + 0.12, 0.72)));
  return rgbTriplet(soft);
}

/** `[accent, soft, fg]` for a colour the user typed or a preset's hex. */
export function deriveTriplets(hex: string): Triplets | null {
  const rgb = parseHexColor(hex);
  if (!rgb) return null;
  return [rgbTriplet(rgb), softFor(rgb), inkFor(rgb)];
}

/** The preset a stored value names, if it names one. */
export function presetHex(id: string | null | undefined): string | null {
  const preset = ACCENT_PRESETS.find((p) => p.id === id);
  return preset ? preset.hex : null;
}

/** A stored accent — a preset id or a custom hex, in any of the forms a person
 *  can type — as the three triplets to paint. Always answers: an unreadable
 *  value (a stale id, a typo, nothing at all) is the default black & white.
 *
 *  The shipped presets answer with their PINNED triplets, not a derivation of
 *  their own hex: an install that picked violet in 3.0.0 must not get a
 *  slightly different violet because the maths arrived later. */
export function resolveAccent(key: string | null | undefined): Triplets {
  const shipped = SHIPPED[key ?? ""];
  if (shipped) return shipped;
  const hex = presetHex(key) ?? normalizeHex(key);
  if (hex) return deriveTriplets(hex) ?? SHIPPED[DEFAULT_ACCENT];
  return SHIPPED[DEFAULT_ACCENT];
}

/** The hex behind an accent key — a preset's own hex, a custom value
 *  normalised, or the default's — for the settings text field and swatch to
 *  start from. */
export function accentHex(key: string | null | undefined): string {
  return presetHex(key) ?? normalizeHex(key) ?? presetHex(DEFAULT_ACCENT)!;
}

/** The accent currently ON the document, read back from the custom properties
 *  (not from localStorage, so a value written by hand or not yet stored is what
 *  gets drawn). Falls back to the default's triplets where a property is unset,
 *  e.g. before the shell's first apply. */
export function currentAccent(): Triplets {
  const style = typeof document === "undefined" ? null : getComputedStyle(document.documentElement);
  const read = (name: string, fallback: string) => style?.getPropertyValue(name).trim() || fallback;
  return [
    read("--accent", SHIPPED[DEFAULT_ACCENT][0]),
    read("--accent-soft", SHIPPED[DEFAULT_ACCENT][1]),
    read("--accent-fg", SHIPPED[DEFAULT_ACCENT][2]),
  ];
}

const listeners = new Set<() => void>();

/** Repaint when the accent changes.
 *
 *  ONE mechanism for every consumer: the shell writes the variables through
 *  `applyAccentVars()` and announces it here, and each canvas re-reads
 *  `currentAccent()` and redraws. Anything that simply uses `rgb(var(--accent))`
 *  in a class or a style repaints on its own and needs none of this — the two
 *  canvas components cache the colour in a closure, which is why they do.
 *  Returns the unsubscribe, for an effect's cleanup. */
export function subscribeAccent(fn: () => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

/** Announce an accent change to every subscriber. Called by `applyAccentVars`;
 *  separate so a test can drive the notification without a document. */
export function notifyAccent() {
  // A copy: a listener may unsubscribe itself (a component unmounting) while
  // the loop runs, and mutating the set under the iterator would skip entries.
  for (const fn of [...listeners]) fn();
}

/** The ONE writer of the accent variables: paint them on <html> and tell the
 *  subscribers. `applyAccent()` in App.tsx (the shell's own entry point, called
 *  at boot and by the settings page) resolves a stored key to triplets and
 *  lands here. */
export function applyAccentVars([accent, soft, fg]: Triplets) {
  const root = document.documentElement;
  root.style.setProperty("--accent", accent);
  root.style.setProperty("--accent-soft", soft);
  root.style.setProperty("--accent-fg", fg);
  notifyAccent();
}

/** HSL helpers. Hue in degrees, saturation and lightness 0..1 — the space the
 *  soft twin is derived in, because "same colour, lighter" is one coordinate
 *  there and a per-channel mix is not. */
export function rgbToHsl([r, g, b]: Rgb): [number, number, number] {
  const [rr, gg, bb] = [r / 255, g / 255, b / 255];
  const max = Math.max(rr, gg, bb);
  const min = Math.min(rr, gg, bb);
  const l = (max + min) / 2;
  const d = max - min;
  if (!d) return [0, 0, l];
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  let h: number;
  if (max === rr) h = ((gg - bb) / d + (gg < bb ? 6 : 0)) * 60;
  else if (max === gg) h = ((bb - rr) / d + 2) * 60;
  else h = ((rr - gg) / d + 4) * 60;
  return [h, s, l];
}

export function hslToRgb(h: number, s: number, l: number): Rgb {
  if (!s) {
    const v = Math.round(l * 255);
    return [v, v, v];
  }
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
  const p = 2 * l - q;
  const channel = (t: number) => {
    let x = t;
    if (x < 0) x += 1;
    if (x > 1) x -= 1;
    if (x < 1 / 6) return p + (q - p) * 6 * x;
    if (x < 1 / 2) return q;
    if (x < 2 / 3) return p + (q - p) * (2 / 3 - x) * 6;
    return p;
  };
  const phi = h / 360;
  return [
    Math.round(channel(phi + 1 / 3) * 255),
    Math.round(channel(phi) * 255),
    Math.round(channel(phi - 1 / 3) * 255),
  ];
}
