import { useEffect, useRef, useState } from "react";
import { Check, Loader2, Minus, Plus, RotateCcw } from "lucide-react";
import { api } from "../api";
import { LYRIC_STEP_BTN, LYRIC_VALUE_BOX, LYRIC_VALUE_UNIT } from "./LyricZoom";

/** The lyric-offset control both lyric surfaces carry: `−`, the pending shift,
 *  `+`, and a Save that appears only once there is something to save — in a
 *  slot whose width is there either way, so the steppers never move under the
 *  finger (issue #56; see the slot's own comment below).
 *
 *  What it moves is the SYNC, not the sound: the reader hears the lines arrive
 *  early or late against the track and nudges every timestamp until they land.
 *  The nudge is LOCAL while it is being dialled in (the surfaces add it to the
 *  parsed line times, so the highlight follows the buttons with no round trip
 *  and no half-written file), and Save writes it to the track's own lyrics
 *  (`POST /api/lyrics/offset`, which shifts the `.lrc` and/or the `LYRICS` tag
 *  the track actually carries). A stepper with an explicit Save rather than
 *  auto-save-on-every-press: a tag write is a file rewrite, and dialling in
 *  0.5 s by ear is ten of them.
 *
 *  The step is a tenth of a second — the finest shift an ear can judge against
 *  a lyric line, and the same order as the sync error providers hand back. The
 *  pending range is capped at ±10 s: past that the file's sync is wrong in
 *  kind, not by an offset, and script 1 / a re-fetch is the honest repair. */
export const LYRIC_OFFSET_STEP_MS = 100;
export const LYRIC_OFFSET_MAX_MS = 10000;

/** `+0.3` / `−0.2` / `0.0` — one decimal, the unit the buttons move in. The
 *  `s` the chip prints after it is NOT part of this string: the offset chip and
 *  the zoom chip beside it lay their value out identically (number, then the
 *  unit as a 9 px sub-element — see LYRIC_VALUE_UNIT in LyricZoom), so the unit
 *  is the chip's own element rather than something the formatter bakes in. */
export function fmtOffset(ms: number): string {
  const secs = ms / 1000;
  const sign = secs > 0 ? "+" : secs < 0 ? "−" : "";
  return `${sign}${Math.abs(secs).toFixed(1)}`;
}

export default function LyricOffset({ path, ms, onChange, onSaved, className }: {
  /** The track whose stored lyrics Save rewrites; null disables the control. */
  path: string | null;
  /** The pending shift in milliseconds (0 = the file plays as stored). */
  ms: number;
  onChange: (ms: number) => void;
  /** The text the server stored — the surface renders THIS, so what it shows
   *  is the file's own copy rather than the client's arithmetic. */
  onSaved: (lrc: string) => void;
  className?: string;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // Which track the pending shift was dialled against.
  const dialedFor = useRef(path);

  // A pending shift belongs to the track it was dialled in against: next /
  // previous must not carry it over, or the new track would be previewed with
  // the old one's correction and Save would write that correction into it.
  //
  // Only a CHANGE resets, never a mount. The fullscreen player renders this
  // control inside its options popover, so closing the menu to listen to the
  // shift and reopening it remounts the component — and a mount that reset
  // would throw the user's dialled-in offset away exactly when they came back
  // to it (the value lives in the surface, which is still mounted).
  useEffect(() => {
    if (dialedFor.current === path) return;
    dialedFor.current = path;
    onChange(0);
    setError("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path]);

  const move = (delta: number) => {
    const next = Math.max(-LYRIC_OFFSET_MAX_MS, Math.min(LYRIC_OFFSET_MAX_MS, ms + delta));
    if (next !== ms) {
      onChange(next);
      setError("");
    }
  };

  const save = async () => {
    if (!path || !ms || busy) return;
    setBusy(true);
    setError("");
    try {
      const res = await api.lyricsOffset(path, ms);
      onSaved(res.lrc);
      onChange(0);
    } catch (e) {
      const detail = e instanceof Error ? e.message : String(e);
      setError(detail);
    } finally {
      setBusy(false);
    }
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
  // w-7` is the sidebar header's own `p-1.5` + `h-4 w-4`) — the Save beside
  // them takes the same box so the row reads as one control.
  //
  // The STEPPER and the VALUE BOX are the ones LyricZoom exports, not copies:
  // this chip sits directly beside that one in the fullscreen player's footer
  // and in the sidebar header, and a value printed straight into a
  // `text-right` box here against an inset `%` there is what put the two
  // `−`/`+` pairs at different distances from the numbers they step.
  //
  // The Save and the Discard are a SLOT, not a pair of buttons that comes and
  // goes (issue #56: "they shouldn't move when the confirm button pops up — it
  // makes it easily clickable by accident"). Rendered only once the offset was
  // dirty, they widened this chip by their own 58 px the instant the first step
  // landed: in the fullscreen player's `justify-end` footer the whole strip
  // slid that far left, so the reader's next press — aimed at the `+` they had
  // just used — landed on the Save (a write into the file's own lyrics) or on
  // the Discard. The slot holds their width in every state and only its CONTENT
  // appears (`invisible`, so an unseen button is nothing to hit either), which
  // makes the steppers immovable no matter what is dialled in. It trails the
  // `+` because that is where the strip's own edge is on the fullscreen
  // surface — the reserved space reads as the row's padding, not as a hole
  // between two chips.
  const btn = LYRIC_STEP_BTN;
  const dirty = ms !== 0;

  return (
    <span className={`inline-flex items-center gap-0.5 shrink-0 ${className ?? ""}`}>
      <button
        className={btn}
        onClick={() => move(-LYRIC_OFFSET_STEP_MS)}
        disabled={!path || busy || ms <= -LYRIC_OFFSET_MAX_MS}
        title="Lyrics 0.1 s earlier"
        aria-label="Lyrics 0.1 s earlier"
      >
        <Minus className="h-3 w-3" />
      </button>
      <span
        className={LYRIC_VALUE_BOX}
        title={
          error
            ? `Could not save the offset: ${error}`
            : "Lyric offset — press − / + to line the lyrics up, then Save to write it to the track's tags"
        }
      >
        {/* the pending shift is the one value in the pair that carries state
            of its own, so it is the one that turns accent while it is unsaved;
            the ink of the chip around it stays the surface's (text-current) */}
        <span className={dirty ? "text-accent" : undefined}>{fmtOffset(ms)}</span>
        <span className={LYRIC_VALUE_UNIT} aria-hidden>s</span>
      </span>
      <button
        className={btn}
        onClick={() => move(LYRIC_OFFSET_STEP_MS)}
        disabled={!path || busy || ms >= LYRIC_OFFSET_MAX_MS}
        title="Lyrics 0.1 s later"
        aria-label="Lyrics 0.1 s later"
      >
        <Plus className="h-3 w-3" />
      </button>
      <span
        className={`inline-flex items-center gap-0.5 shrink-0 ${dirty ? "" : "invisible"}`}
        aria-hidden={!dirty}
      >
        <button
          className="h-7 w-7 inline-flex items-center justify-center rounded-md text-accent hover:text-accent-soft hover:bg-raise transition-colors disabled:opacity-40"
          onClick={save}
          disabled={busy}
          title="Save the offset into this track's lyrics (tags / .lrc)"
          aria-label="Save lyric offset"
        >
          {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Check className="h-3 w-3" />}
        </button>
        <button
          className={btn}
          onClick={() => onChange(0)}
          disabled={busy}
          title="Discard the pending offset"
          aria-label="Discard pending lyric offset"
        >
          <RotateCcw className="h-3 w-3" />
        </button>
      </span>
    </span>
  );
}
