import { useEffect, useState } from "react";

/** Percentage readout + typeable box for a volume slider. Typed values are
 * clamped to 0-100 and committed on Enter or blur (so typing "50" doesn't
 * jump to 5% mid-keystroke). The box stays in sync when the slider moves. */
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
    <span className={`relative inline-flex items-center shrink-0 ${className ?? ""}`}>
      <input
        className="w-10 bg-transparent border border-transparent hover:border-border focus:border-accent rounded px-1 pr-3.5 text-right text-[10px] font-mono tabular-nums text-current opacity-70 hover:opacity-100 focus:opacity-100 outline-none"
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
      <span className="absolute right-1 text-[9px] text-current opacity-60 pointer-events-none">%</span>
    </span>
  );
}
