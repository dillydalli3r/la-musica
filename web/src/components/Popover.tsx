import { useEffect } from "react";
import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";

/** The one dropdown idiom: mount it inside a `relative` parent next to the
 *  trigger, pass `open`, and it renders the click shield (a full-viewport
 *  catcher that closes on outside click, which keeps the panel from being
 *  clipped by overflow containers), an Escape handler and the shared
 *  `.anim-fade` entry on a positioned panel. `MenuItem` is the row inside. */
export default function Popover({
  open,
  onClose,
  children,
  align = "right",
  placement = "bottom",
  panelClass = "w-56 p-1.5",
  shield = true,
}: {
  open: boolean;
  onClose: () => void;
  children: ReactNode;
  /** `"top"` is for triggers pinned to the bottom of the window (the player
   *  bar): a downward panel there would open off-screen. */
  placement?: "bottom" | "top";
  /** `"center"` centres the panel under a trigger that has no left or right
   *  edge worth aligning to (the transport row's icon buttons). */
  align?: "left" | "right" | "center";
  panelClass?: string;
  /** Off for popovers that live inside a dialog which already has its own
   *  click shield (a second shield would swallow the dialog's clicks). */
  shield?: boolean;
}) {
  useEffect(() => {
    if (!open) return;
    // A handler that already claimed Escape (a host modal, a hotkey rebind)
    // preventDefaults it; that claim wins, so this shield stands down.
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !e.defaultPrevented && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <>
      {shield && <div className="fixed inset-0 z-40" onClick={onClose} />}
      <div
        role="menu"
        className={`anim-fade absolute z-50 ${placement === "top" ? "bottom-full mb-1" : "mt-1"} rounded-xl shadow-2xl bg-zinc-950 border border-white/10 ${
          align === "right" ? "right-0" : align === "left" ? "left-0" : "left-1/2 -translate-x-1/2"
        } ${panelClass}`}
      >
        {children}
      </div>
    </>
  );
}

/** One row of a Popover — same shape as OverflowMenu's items. */
export function MenuItem({
  label,
  icon: Icon,
  onClick,
  danger = false,
  disabled = false,
  title,
  active = false,
}: {
  label: string;
  icon?: LucideIcon;
  onClick?: () => void;
  danger?: boolean;
  disabled?: boolean;
  title?: string;
  active?: boolean;
}) {
  return (
    <button
      role="menuitem"
      disabled={disabled}
      title={title}
      onClick={onClick}
      className={`w-full text-left text-xs px-2.5 py-1.5 rounded-lg flex items-center gap-2.5 transition-colors disabled:opacity-40 ${
        danger ? "text-red-300 hover:bg-red-950/50" : "text-zinc-300 hover:bg-white/10"
      } ${active ? "bg-white/10" : ""}`}
    >
      {Icon && <Icon className="h-3.5 w-3.5 shrink-0" />}
      <span className="flex-1 truncate">{label}</span>
    </button>
  );
}
