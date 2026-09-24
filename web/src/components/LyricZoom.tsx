import { useEffect, useState } from "react";
import { Minus, Plus } from "lucide-react";

/** The lyric-size control both lyric surfaces carry: `−`, a percentage you can
 *  also type into, `+`. The buttons move in 5 % steps; a typed value is taken
 *  as given (clamped), because someone who types 137 wants 137.
 *
 *  Why a stepper and not the range slider the fullscreen player used to have:
 *  a slider is a drag, so it cannot be used from the keyboard without
 *  arrow-stepping anyway, and it showed a percentage you could not correct —
 *  the one number on the surface you had to guess at. The bounds are the
 *  slider's own 85 %–160 %: below that the sidebar's 15 px lines stop being
 *  words, above it a single lyric line fills the pane.
 *
 *  Both surfaces persist their OWN value (see the callers): the fullscreen
 *  pane's comfortable reading size and the 380 px sidebar's are different
 *  numbers, and sharing one would re-lay-out the surface you were not
 *  touching. */
export const LYRIC_ZOOM_MIN = 85;
export const LYRIC_ZOOM_MAX = 160;
export const LYRIC_ZOOM_STEP = 5;

/** The lyric chips' geometry — the zoom (`− 100 % +`) and the offset
 *  (`− 0.0s +`) are ONE layout, so these three strings are shared with
 *  components/LyricOffset.tsx rather than written out in both.
 *
 *  Why it has to be shared: the two chips sit side by side in the fullscreen
 *  player's footer and in the sidebar header, and the buttons were already the
 *  same box — but the VALUE between them was not. The offset printed straight
 *  into a `text-right` box while the zoom put a 3-digit input plus an
 *  absolutely-positioned `%` inside its own, so the number sat flush right on
 *  one chip and inset on the other and the `−`/`+` stood at visibly different
 *  distances from the number they step. Measured, the gap from the value to
 *  the `+` was 17 px on the zoom against 13 px on the offset. Both now use one
 *  fixed 40 px value box, right-packed, number + unit as two inline elements
 *  (the unit is a 9 px sub-element on BOTH chips — `%` and `s` read the same
 *  way), so the two buttons land at the same offset from either end of either
 *  chip. tools/check_np_metadata_contrast.cjs asserts the rects. */
export const LYRIC_STEP_BTN =
  "h-7 w-7 inline-flex items-center justify-center rounded-md text-current opacity-70 hover:opacity-100 hover:bg-white/10 disabled:opacity-30 disabled:hover:bg-transparent transition-colors";
export const LYRIC_VALUE_BOX =
  "h-5 w-10 inline-flex items-center justify-end gap-0.5 shrink-0 text-[10px] leading-none font-mono tabular-nums text-current opacity-80 hover:opacity-100 focus-within:opacity-100 transition-opacity";
export const LYRIC_VALUE_UNIT = "text-[9px]";

export default function LyricZoom({ pct, onChange, className }: {
  /** The current size, as a percentage (100 = the surface's own base size). */
  pct: number;
  onChange: (pct: number) => void;
  className?: string;
}) {
  const [text, setText] = useState(() => String(Math.round(pct)));

  // The value moved elsewhere (a preset button, another tab): show it.
  useEffect(() => {
    setText(String(Math.round(pct)));
  }, [pct]);

  // `clamp` is the bounds rule itself (a magic-constant formula used by the
  // typed value and by both buttons) — named once so the three cannot drift.
  const clamp = (n: number) => Math.max(LYRIC_ZOOM_MIN, Math.min(LYRIC_ZOOM_MAX, Math.round(n)));

  const commit = () => {
    const n = Number(text.trim());
    if (text.trim() === "" || Number.isNaN(n)) {
      setText(String(Math.round(pct)));
      return;
    }
    const next = clamp(n);
    setText(String(next));
    if (next !== Math.round(pct)) onChange(next);
  };

  // The ink is the SURFACE's, not a fixed grey: this control is rendered on
  // three of them — the sidebar header (a dark panel), the fullscreen
  // player's options popover, and the fullscreen player ITSELF, straight over
  // the artwork, where a zinc-500 glyph is the grey-on-grey failure of R52c.
  // `text-current` inside the caller's own ink class is what makes one control
  // read right on all three.
  //
  // Each step is one SQUARE box with the glyph centred in it by flex, never by
  // its own metrics: a padded glyph sits wherever the icon's box puts it,
  // which is what left the `+` reading low and heavy beside the `−` and the
  // value between them. Both sides use the same lucide icon at the same size,
  // and the box is the size of the buttons beside it on every surface (`h-7
  // w-7` is the sidebar header's own `p-1.5` + `h-4 w-4`).
  const btn = LYRIC_STEP_BTN;

  return (
    <span className={`inline-flex items-center gap-0.5 shrink-0 ${className ?? ""}`}>
      <button
        className={btn}
        onClick={() => {
          const next = clamp(Math.round(pct) - LYRIC_ZOOM_STEP);
          if (next !== Math.round(pct)) onChange(next);
        }}
        disabled={Math.round(pct) <= LYRIC_ZOOM_MIN}
        title="Smaller lyrics"
        aria-label="Smaller lyrics"
      >
        <Minus className="h-3 w-3" />
      </button>
      <span className={LYRIC_VALUE_BOX}>
        {/* The input is exactly three mono digits wide (`ch`, so no font or
            browser text-size setting can clip the value — the same rule
            components/VolumePct documents) with a transparent 1 px border that
            only takes a colour on hover/focus: the border is always there, so
            the digits never shift when it lights up. The `%` is a SIBLING of
            the input rather than a superscript inside its padding — that is
            what makes this chip's value box identical to the offset chip's
            (see LYRIC_VALUE_BOX). */}
        <input
          className="w-[calc(3ch+2px)] bg-transparent border border-transparent hover:border-border focus:border-accent rounded text-right text-current outline-none"
          inputMode="numeric"
          value={text}
          title={`Lyrics size — type a percentage (${LYRIC_ZOOM_MIN}–${LYRIC_ZOOM_MAX})`}
          aria-label="Lyrics size percentage"
          onChange={(e) => setText(e.target.value.replace(/[^\d]/g, "").slice(0, 3))}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              commit();
              (e.target as HTMLInputElement).blur();
            } else if (e.key === "Escape") {
              setText(String(Math.round(pct)));
            }
          }}
        />
        <span className={LYRIC_VALUE_UNIT} aria-hidden>%</span>
      </span>
      <button
        className={btn}
        onClick={() => {
          const next = clamp(Math.round(pct) + LYRIC_ZOOM_STEP);
          if (next !== Math.round(pct)) onChange(next);
        }}
        disabled={Math.round(pct) >= LYRIC_ZOOM_MAX}
        title="Larger lyrics"
        aria-label="Larger lyrics"
      >
        <Plus className="h-3 w-3" />
      </button>
    </span>
  );
}
