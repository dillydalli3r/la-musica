import { useEffect, useState } from "react";

/** Percentage readout + typeable box for a volume slider. Typed values are
 * clamped to 0-100 and committed on Enter or blur (so typing "50" doesn't
 * jump to 5% mid-keystroke). The box stays in sync when the slider moves.
 *
 * The size and the colour are the CALLER's, never its own: it sits in a row of
 * readouts — the fullscreen player's seek row (the time readouts, `text-xs`
 * and the ink's chrome tone) and the player bar's volume line (`text-[10px]`,
 * zinc-400) — and it reads as ONE of them, so a hardcoded size or an opacity
 * of its own made it "a step smaller and fainter than the text beside it"
 * (reported twice: first the `%` sign, then the whole readout against the time
 * readouts it shares a row with). `className` is where a caller that needs a
 * size (the bar's compact line) says so. */
export default function VolumePct({ value, onChange, className }: {
  value: number;
  onChange: (v: number) => void;
  className?: string;
}) {
  const [text, setText] = useState(() => String(Math.round(value * 100)));

  // slider / store moved the volume — reflect it in the box
  useEffect(() => {
    setText(String(Math.round(value * 100)));
  }, [value]);

  const commit = () => {
    const n = Number(text.trim());
    if (text.trim() === "" || Number.isNaN(n)) {
      setText(String(Math.round(value * 100)));
      return;
    }
    const clamped = Math.max(0, Math.min(100, n));
    setText(String(clamped));
    onChange(clamped / 100);
  };

  return (
    <span className={`inline-flex items-center shrink-0 font-mono tabular-nums text-current ${className ?? ""}`}>
      {/* The box is sized in `ch`, not pixels: the value is capped at three
          digits, and 3ch IS three digits of the mono font it renders in, so no
          font, DPI or browser text-size setting can push a glyph out of it.
          A pixel width ("w-7") was 1px short of "100" in the shipped font
          already — the trailing 0 clipped by the input's own content box, the
          chopped readout the owner reported — and worse wherever the system
          mono is wider. The rest is px-1's own 0.5rem (which scales with the
          root font, hence rem here, not a fixed 10px) plus the 2px border. */}
      <input
        className="w-[calc(3ch+0.5rem+2px)] bg-transparent border border-transparent hover:border-border focus:border-accent rounded px-1 text-right text-current outline-none"
        inputMode="numeric"
        value={text}
        title="Volume — type a percentage"
        onChange={(e) => setText(e.target.value.replace(/[^\d]/g, "").slice(0, 3))}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            commit();
            (e.target as HTMLInputElement).blur();
          } else if (e.key === "Escape") {
            setText(String(Math.round(value * 100)));
          }
        }}
      />
      <span className="pointer-events-none">%</span>
    </span>
  );
}
