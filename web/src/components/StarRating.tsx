import { useState } from "react";
import { Star } from "lucide-react";
import { MAX_RATING } from "../lib/ratings";
import { useI18n } from "../lib/i18n";
import { toast } from "../store";

/** The two sizes a surface needs: `sm` for a table row, `md` for a card or
 *  the player bar, `lg` for a page header. */
export type StarSize = "sm" | "md" | "lg";

const ICON: Record<StarSize, string> = { sm: "h-3.5 w-3.5", md: "h-4 w-4", lg: "h-6 w-6" };
// A 14px star is not a tap target: the two halves are grown VERTICALLY only,
// so the row keeps its own height while a thumb still lands on a half.
const HALF_BOX: Record<StarSize, string> = { sm: "h-5", md: "h-6", lg: "h-8" };
const READOUT: Record<StarSize, string> = { sm: "text-[10px]", md: "text-xs", lg: "text-sm" };

/** The WEB tone — the same five-star geometry in an ink that cannot be taken
 *  for the user's own mark.
 *
 *  The user's stars are the app's ink at full strength (near-white); the web
 *  reading is GREY. The owner's report was that the sky-blue web stars were
 *  "disorienting" beside their own — a second colour competing with the one
 *  they chose — so the web reading is a shade of the same ink instead: a ghost
 *  of a rating, which is what it is (somebody else's number the script
 *  recorded), and the readout says the words "Album Web" beside it.
 *  Overridable for the one surface drawn on artwork (see the doc above);
 *  `NowPlayingView` hands in the cover's own ink there — opacity does the same
 *  job in a picture, so the fullscreen player inverts without this file
 *  knowing what it is drawn on. */
const WEB_EMPTY = "text-zinc-700";
const WEB_FILL = "fill-current text-zinc-400";
/** The mark `webReadout="mark"` draws instead of the text readout, for a cell
 *  that is a fixed narrow column (see that prop). Grey, like the tone: the
 *  mark is never drawn on artwork, which uses the text readout. */
const WEB_MARK = "bg-zinc-400";
/** The web readout's own colour: a shade brighter than the fill, because it
 *  is the part that has to be READ. Overridden beside the other two on the
 *  artwork surface. */
const WEB_TEXT = "text-zinc-400";

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
 *  widget itself stays scope-blind: it draws stars and reports a number.
 *
 *  `emptyClass` / `fillClass` are the two colours, and they exist for the one
 *  surface whose background is not the app's: the fullscreen player draws its
 *  star row straight onto the artwork, where the fixed `zinc-600` outline and
 *  the near-white FILL both blend into a bright cover (reported). The caller
 *  there hands in the player's own ink (`text-current` + the surface's tone),
 *  the same rule the lyrics, the chrome and the frequency strip follow — so
 *  the stars read as part of the block instead of vanishing into the picture.
 *
 *  `webValue` is the WEB rating (script 24's `WEBRATING`, or the album's
 *  `ALBUMWEBRATING`), already in this control's units — lib/ratings does the
 *  0-100 → 0-5 step. It is a DIFFERENT FACT from the user's rating, and the
 *  control never lets the two be read as one another: the web value is drawn
 *  only while the user has rated nothing here (in the dimmed web tone, same
 *  geometry), and once they have, their own stars win the field and the web
 *  number becomes a small readout beside them. Hovering the field while it is
 *  still unrated previews the user's own rating in their own ink, as it always
 *  has. `undefined` — the tag is absent — draws nothing at all, because an
 *  absence is not a rating of zero.
 *
 *  `webSources` is the `*_SOURCE` tag's own "; "-joined names; `webKind` says
 *  whether the value is the track's or the album's, so every word about it
 *  (readout, tooltip, accessible name) says which one it is; and `webReadout`
 *  picks between the text readout and a single dot, for a cell that is a fixed
 *  narrow column where text would paint over its neighbour. */
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
  webValue,
  webSources,
  webKind = "track",
  webReadout = "text",
  className = "",
  emptyClass = "text-zinc-600",
  fillClass = "fill-current text-zinc-100",
  webEmptyClass = WEB_EMPTY,
  webFillClass = WEB_FILL,
  webTextClass = WEB_TEXT,
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
  /** The WEB rating in THIS control's units (0-5) — lib/ratings' webRatingOf /
   *  trackWebRating / albumWebRating hand it over. Absent (or 0) draws nothing:
   *  a tag nobody wrote is not a rating of zero. */
  webValue?: number;
  /** The `WEBRATING_SOURCE` / `ALBUMWEBRATING_SOURCE` names, as the script
   *  joined them. Carried verbatim in the tooltip. */
  webSources?: string;
  /** Which fact `webValue` is — the two are different facts and both are
   *  labelled as their own. Defaults to the track's. */
  webKind?: "track" | "album";
  /** How the web value reads once the user's own stars are the ones drawn:
   * "text" prints "4.4 Album Web" while the user has rated nothing; "mark"
   * draws one dot instead, for a
   * cell that is a FIXED narrow column (the Tracks view's Rating column is
   * 104 px) where a text readout would paint over the column beside it. The
   * value and its sources stay on the tooltip either way. "slot" prints the
   * same words as "text" — but inside a box reserved for the WIDEST thing this
   * control can print, so the stars keep one x as the readout changes from the
   * web reading to the user's own number: for a control that sits in a COLUMN
   * the eye scans (the album tracklist's trailing slot, the album tables). A
   * plain "text" readout is only as wide as its words, so a row that swaps
   * "3.9 Web" for "5" moves its stars right — invisible inline, misaligned
   * down a column (the owner's report). */
  webReadout?: "text" | "mark" | "slot";
  className?: string;
  /** The empty outline's colour and the filled halves' — overrides for the one
   *  surface drawn on artwork (see the doc above). */
  emptyClass?: string;
  fillClass?: string;
  /** The same two colours for the WEB tone — overridden by the surface that
   *  draws on artwork, where a fixed sky would be one cover photo away from
   *  illegible (NowPlayingView passes the block's own ink). */
  webEmptyClass?: string;
  webFillClass?: string;
  /** The web READOUT's colour — the third piece of the web tone, because the
   *  same artwork surface needs it too: a text readout has to keep the ink
   *  rule its stars follow, and the sky it defaults to is a fixed colour. */
  webTextClass?: string;
}) {
  const { t } = useI18n();
  const [hover, setHover] = useState<number | null>(null);
  const readOnlyFinal = readOnly ?? !onChange;
  const stars = Math.max(1, Math.round(max));
  const value = snap(rawValue);
  // The drawn stars may be a proportional read-out of the same 0-5 value.
  const drawn = stars === MAX_RATING ? value : snap((value * stars) / MAX_RATING);
  // The web value is scaled the same way, for the same reason: a card that
  // draws three stars draws the web reading on three.
  const web = webValue !== undefined && Number.isFinite(webValue) ? Math.max(0, Math.min(MAX_RATING, webValue)) : 0;
  const hasWeb = web > 0;
  const webDrawn = stars === MAX_RATING ? snap(web) : snap((web * stars) / MAX_RATING);
  // The user's own stars always win the field. The web value is DRAWN only
  // where they have rated nothing here; once they have, it steps aside to the
  // readout below — never a second row of stars that could be read as theirs.
  const webOnly = value <= 0 && webDrawn > 0;
  const previewing = !readOnlyFinal && !pending && hover !== null;
  const shown = previewing ? (hover as number) : webOnly ? webDrawn : drawn;
  const text = value > 0 ? `${half(value)} of ${MAX_RATING}` : "unrated";
  // Everything said about the web value is built once, so the readout, the
  // tooltip and what a screen reader hears can never disagree. `half` is the
  // grid's own writer, but the web number is NOT on that grid: 4.4 is the
  // web's own reading and is printed as 4.4.
  const webText = hasWeb ? half(web) : "";
  const webReadoutText = hasWeb
    ? t(webKind === "album" ? "rating.webAlbumReadout" : "rating.webReadout", { value: webText })
    : "";
  const webTip = !hasWeb
    ? ""
    : webSources?.trim()
      ? t(webKind === "album" ? "rating.webAlbumTitle" : "rating.webTitle", { value: webText, sources: webSources.trim() })
      : t(webKind === "album" ? "rating.webAlbumTitleNoSources" : "rating.webTitleNoSources", { value: webText });
  const webAria = hasWeb ? t(webKind === "album" ? "rating.webAlbumAria" : "rating.webAria", { value: webText }) : "";
  // The readout: the user's own number comes first (`showValue` surfaces
  // always print one — "—" while unrated — and a row that has a web reading
  // prints THEIR number instead of the web's words, R359), else the web
  // reading while it is what the field draws. `ownNumber` and `webWords` are
  // mutually exclusive: `webOnly` is exactly "the user has rated nothing here".
  const slot = webReadout === "slot";
  const ownNumber = (showValue && !webOnly) || (value > 0 && hasWeb && webReadout !== "mark");
  const webWords = hasWeb && webOnly;
  const readout = ownNumber ? (value > 0 ? half(value) : "—") : webWords ? webReadoutText : "";
  // The slot's width: an invisible copy of the WIDEST string this control can
  // print reserves the box, so the words inside never move the stars. A px
  // constant would be wrong in another locale or font — "4.4 Album Web",
  // "4.4 album Web", "4.4 アルバム Web" are three widths — so the sizer is the
  // control's OWN template with the widest value the grid allows.
  const readoutSizer = t(webKind === "album" ? "rating.webAlbumReadout" : "rating.webReadout", { value: "4.5" });

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
      aria-label={
        readOnlyFinal
          ? webOnly
            ? webAria
            : `${label}: ${text}${hasWeb ? ` — ${webAria}` : ""}`
          : `${label} — ${text}. Arrow keys change by half a star, Delete clears.${hasWeb ? ` ${webAria}.` : ""}`
      }
      aria-busy={!readOnlyFinal && pending ? true : undefined}
      tabIndex={readOnlyFinal ? undefined : 0}
      title={
        readOnlyFinal
          ? hasWeb
            ? webTip
            : undefined
          : `${hint ??
              "Click a star's left half for a half star — click the value already set to clear it (← / → nudge, Delete clears)"}${hasWeb ? ` · ${webTip}` : ""}`
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
        // The web tone applies only while the web value is what the field is
        // DRAWING. A previewing field is the user's own rating-to-be, so it
        // switches back to the user's own ink the moment they point at a star.
        const webTone = webOnly && !previewing;
        return (
          <span key={star} className="relative inline-flex shrink-0">
            <Star className={`${ICON[size]} ${webTone ? webEmptyClass : emptyClass}`} strokeWidth={2} aria-hidden="true" />
            {fill > 0 && (
              // Half a star is a clipped full star, so both ends of the
              // clip line up with the outline underneath.
              <span
                className="absolute inset-y-0 left-0 overflow-hidden pointer-events-none"
                style={{ width: `${fill * 100}%` }}
              >
                <Star className={`${ICON[size]} ${webTone ? webFillClass : fillClass}`} strokeWidth={2} aria-hidden="true" />
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
      {/* The readout. `showValue` is the surfaces that always print a number
          (a card, a page header); the SECOND case is the replacement for the
          web words the owner asked to be rid of: a row that showed "4.2 Web"
          beside their four stars now shows the rating they gave, and the web
          reading stays on the stars' tooltip (R359). In `slot` the box is
          reserved whether or not it has words (see `readoutSizer`), so the
          stars cannot move as the row changes between the two; in `text` it
          is only as wide as its words, which is what an inline surface wants. */}
      {(readout !== "" || slot) && (
        <span
          className={`relative ml-1.5 shrink-0 tabular-nums ${READOUT[size]} ${ownNumber ? "text-zinc-400" : webTextClass}`}
          title={!ownNumber && webWords ? webTip : undefined}
          aria-hidden="true"
        >
          {slot && <span className="invisible block whitespace-nowrap">{readoutSizer}</span>}
          <span className={slot ? "absolute inset-0 flex items-center whitespace-nowrap" : undefined}>{readout}</span>
        </span>
      )}
      {/* The web reading's `mark` form: one dot, for a cell that is a FIXED
          narrow column where even the reserved text box would paint over its
          neighbour. The value and its sources stay on the tooltip. */}
      {webReadout === "mark" && webWords && (
        <span
          className={`ml-0.5 shrink-0 ${webTextClass} ${READOUT[size]}`}
          title={webTip}
          aria-hidden="true"
        >
          <span className={`inline-block h-1.5 w-1.5 rounded-full align-middle ${WEB_MARK}`} />
        </span>
      )}
    </span>
  );
}
