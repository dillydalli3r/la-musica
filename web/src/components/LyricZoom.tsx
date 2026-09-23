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
  const btn = "p-0.5 rounded-md text-current opacity-70 hover:opacity-100 hover:bg-white/10 disabled:opacity-30 disabled:hover:bg-transparent transition-colors";

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
      <span className="relative inline-flex items-center shrink-0">
        <input
          className="w-10 bg-transparent border border-transparent hover:border-border focus:border-accent rounded px-1 pr-3.5 text-right text-[10px] font-mono tabular-nums text-current opacity-80 hover:opacity-100 focus:opacity-100 outline-none"
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
        <span className="absolute right-1 text-[9px] text-current opacity-60 pointer-events-none">%</span>
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
