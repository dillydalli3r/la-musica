import { useState } from "react";
import { Star } from "lucide-react";
import { MAX_RATING } from "../lib/ratings";
import { toast } from "../store";

/** The two sizes a surface needs: `sm` for a table row, `md` for a card or
 *  the player bar, `lg` for a page header. */
export type StarSize = "sm" | "md" | "lg";

const ICON: Record<StarSize, string> = { sm: "h-3.5 w-3.5", md: "h-4 w-4", lg: "h-6 w-6" };
// A 14px star is not a tap target: the two halves are grown VERTICALLY only,
// so the row keeps its own height while a thumb still lands on a half.
const HALF_BOX: Record<StarSize, string> = { sm: "h-5", md: "h-6", lg: "h-8" };
const READOUT: Record<StarSize, string> = { sm: "text-[10px]", md: "text-xs", lg: "text-sm" };

/** Clamp into range and snap to the half-star grid — an album average
 *  arrives as 3.6667 and the drawn stars have to land on halves. */
const snap = (v: number) => {
  const n = Number.isFinite(v) ? Math.max(0, Math.min(MAX_RATING, v)) : 0;
  return Math.round(n * 2) / 2;
};
/** The one way a grid value is written out: "4", "4.5". */
const half = (v: number) => (Number.isInteger(v) ? String(v) : v.toFixed(1));

/** The one rating control: five stars, halves included.
 *
 *  `value` is the UI rating, 0-5 in steps of 0.5 (0 = unrated) — the same
 *  scale lib/ratings.ts hands out. It is NOT the API's 0-10 integer: the
 *  conversion happens in that one module, so nothing here ever sees it.
 *
 *  Clicking a star's LEFT half sets the half value, its RIGHT half the whole
 *  one, and clicking the value already set clears the rating back to
 *  unrated; hovering previews what a click would set. Keyboard: the widget
 *  takes focus as one stop, ←/→ (or ↑/↓) move by half a star, Delete or
 *  Backspace clears. Each star exposes its own name ("Rate 4.5 of 5"), and
 *  the group is named with the current value ("Rating — 4.5 of 5").
 *
 *  `readOnly` (or simply no `onChange`) is the display-only mode used on
 *  cards: no focus stop, no click targets, one accessible name for the row.
 *
 *  `pending` is `useSetRating().pending(path)` for this element. While it is
 *  true the control refuses further changes — a second write for the same
 *  element mid-flight would race the first — and shows itself busy. The
 *  optimistic value and the rollback live in lib/ratings.ts; the value this
 *  control draws is whatever the query cache now holds.
 *
 *  `max` is how many stars are DRAWN, and it exists for the compact card
 *  read-out: a 3-star row shows the same 0-5 rating on a 3-star scale (a
 *  proportional read-out, snapped to the nearest half star). Leave it at 5
 *  for every editing surface.
 *
 *  `hint` replaces the control's own "how to click this" tooltip where the
 *  CALLER knows something the control does not — an album or artist rating is
 *  the user's verdict on that entity (never the average of its tracks) and
 *  lives in the app's store alone, because a folder has no file to tag. The
 *  widget itself stays scope-blind: it draws stars and reports a number. */
export default function StarRating({
  value: rawValue,
  onChange,
  size = "md",
  max = MAX_RATING,
  readOnly,
  pending = false,
  label = "Rating",
  hint,
  showValue = false,
  className = "",
}: {
  /** The rating in UI units: 0-5, step 0.5. */
  value: number;
  /** Omit for a display-only control. A rejected promise is toasted. */
  onChange?: (value: number) => void | Promise<unknown>;
  size?: StarSize;
  /** How many stars to draw; 5 unless a compact card asks for fewer. */
  max?: number;
  readOnly?: boolean;
  /** A write for this element is in flight — refuse further changes. */
  pending?: boolean;
  /** Accessible name of the group ("Rating", "Album rating"). */
  label?: string;
  /** Tooltip for an editing control, in place of the built-in click hint. */
  hint?: string;
  /** Print the numeric value beside the stars (page headers). */
  showValue?: boolean;
  className?: string;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const readOnlyFinal = readOnly ?? !onChange;
  const stars = Math.max(1, Math.round(max));
  const value = snap(rawValue);
  // The drawn stars may be a proportional read-out of the same 0-5 value.
  const drawn = stars === MAX_RATING ? value : snap((value * stars) / MAX_RATING);
  const shown = !readOnlyFinal && !pending && hover !== null ? hover : drawn;
  const text = value > 0 ? `${half(value)} of ${MAX_RATING}` : "unrated";

  /** A drawn-scale value back to the UI scale (identity at max = 5). */
  const toValue = (v: number) => (stars === MAX_RATING ? snap(v) : snap((v * MAX_RATING) / stars));

  const fire = (next: number) => {
    // A write for this track is already in flight: a second one would race it.
    if (!onChange || pending) return;
    const r = onChange(snap(next));
    // A caller with its own mutation may hand back a promise; lib/ratings'
    // hook reports its own failures, so this never double-toasts.
    if (r instanceof Promise) {
      r.catch((e: unknown) => toast(e instanceof Error ? e.message : String(e), "error"));
    }
  };
  /** Selecting the value already set clears the rating. */
  const pick = (drawnValue: number) => fire(drawnValue === drawn ? 0 : toValue(drawnValue));

  return (
    <span
      className={`relative inline-flex items-center shrink-0 align-middle select-none ${pending ? "opacity-60" : ""} ${className}`}
      role={readOnlyFinal ? "img" : "group"}
      aria-label={readOnlyFinal ? `${label}: ${text}` : `${label} — ${text}. Arrow keys change by half a star, Delete clears.`}
      aria-busy={!readOnlyFinal && pending ? true : undefined}
      tabIndex={readOnlyFinal ? undefined : 0}
      title={
        readOnlyFinal
          ? undefined
          : hint ??
            "Click a star's left half for a half star — click the value already set to clear it (← / → nudge, Delete clears)"
      }
      onPointerLeave={() => setHover(null)}
      onBlur={() => setHover(null)}
      onKeyDown={(e) => {
        if (readOnlyFinal || pending) return;
        if (e.key === "ArrowRight" || e.key === "ArrowUp") {
          e.preventDefault();
          if (value < MAX_RATING) fire(value + 0.5);
        } else if (e.key === "ArrowLeft" || e.key === "ArrowDown") {
          e.preventDefault();
          fire(Math.max(0, value - 0.5));
        } else if (e.key === "Delete" || e.key === "Backspace") {
          e.preventDefault();
          if (value > 0) fire(0);
        }
      }}
    >
      {Array.from({ length: stars }, (_, i) => {
        const star = i + 1;
        const fill = Math.max(0, Math.min(1, shown - (star - 1)));
        return (
          <span key={star} className="relative inline-flex shrink-0">
            <Star className={`${ICON[size]} text-zinc-600`} strokeWidth={2} aria-hidden="true" />
            {fill > 0 && (
              // Half a star is a clipped full star, so both ends of the
              // clip line up with the outline underneath.
              <span
                className="absolute inset-y-0 left-0 overflow-hidden pointer-events-none"
                style={{ width: `${fill * 100}%` }}
              >
                <Star className={`${ICON[size]} fill-current text-accent`} strokeWidth={2} aria-hidden="true" />
              </span>
            )}
            {!readOnlyFinal && (
              <>
                <button
                  type="button"
                  tabIndex={-1}
                  disabled={pending}
                  className={`absolute left-0 top-1/2 -translate-y-1/2 w-1/2 ${HALF_BOX[size]} cursor-pointer disabled:cursor-default`}
                  aria-label={`Rate ${half(star - 0.5)} of ${stars}`}
                  onPointerEnter={() => setHover(star - 0.5)}
                  onClick={(e) => {
                    // rows and cards are themselves clickable
                    e.preventDefault();
                    e.stopPropagation();
                    pick(star - 0.5);
                  }}
                />
                <button
                  type="button"
                  tabIndex={-1}
                  disabled={pending}
                  className={`absolute right-0 top-1/2 -translate-y-1/2 w-1/2 ${HALF_BOX[size]} cursor-pointer disabled:cursor-default`}
                  aria-label={`Rate ${star} of ${stars}`}
                  onPointerEnter={() => setHover(star)}
                  onClick={(e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    pick(star);
                  }}
                />
              </>
            )}
          </span>
        );
      })}
      {showValue && (
        <span className={`ml-1.5 tabular-nums text-zinc-400 ${READOUT[size]}`} aria-hidden="true">
          {value > 0 ? half(value) : "—"}
        </span>
      )}
    </span>
  );
}
