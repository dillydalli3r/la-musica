import { useEffect, useRef, useState, type ReactNode } from "react";
import { Check, Trash2, X } from "lucide-react";
import type { LucideIcon } from "lucide-react";

/** Two-step button for destructive-ish actions (resets, removals): the first
 * click arms it in place — no native window.confirm dialogs. The armed state
 * confirms on a second click and disarms on outside click, Escape, or after
 * a few seconds of inactivity. */
export default function ConfirmButton({
  onConfirm,
  children,
  confirmLabel = "Confirm?",
  title,
  className = "btn-ghost",
  disabled = false,
  iconOnly = false,
  confirmIcon: ConfirmIcon = Trash2,
}: {
  onConfirm: () => void;
  children: ReactNode;
  confirmLabel?: string;
  title?: string;
  className?: string;
  disabled?: boolean;
  /** Square icon button: the armed state stays the SAME box — it turns red
   *  and pulses instead of expanding into a label plus a cancel button (a
   *  text label appearing in a row of icons is what read as jarring). An
   *  outside click, Escape or the timeout still disarms it. */
  iconOnly?: boolean;
  /** Glyph worn while armed. */
  confirmIcon?: LucideIcon;
}) {
  const [armed, setArmed] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const btnRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!armed) return;
    const onDown = (e: MouseEvent) => {
      const box = ref.current ?? btnRef.current;
      if (box && !box.contains(e.target as Node)) setArmed(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setArmed(false);
    };
    const t = setTimeout(() => setArmed(false), 6000);
    document.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
      clearTimeout(t);
    };
  }, [armed]);

  if (armed) {
    if (iconOnly) {
      return (
        <button
          ref={btnRef}
          className="btn-icon-danger btn-armed"
          onClick={() => {
            setArmed(false);
            onConfirm();
          }}
          title="Click again to confirm"
          aria-label="Confirm"
        >
          <ConfirmIcon className="h-4 w-4" />
        </button>
      );
    }
    return (
      <div ref={ref} className="inline-flex items-center gap-1">
        <button
          className="btn-danger !py-1.5 text-xs tap"
          onClick={() => {
            setArmed(false);
            onConfirm();
          }}
          title="Confirm"
        >
          <Check className="h-3.5 w-3.5" /> {confirmLabel}
        </button>
        <button className="btn-ghost !py-1.5 text-xs tap" onClick={() => setArmed(false)} title="Cancel">
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
    );
  }

  return (
    <button className={className} onClick={() => setArmed(true)} disabled={disabled} title={title}>
      {children}
    </button>
  );
}
