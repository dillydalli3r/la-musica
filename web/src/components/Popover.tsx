import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
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
  fixed = false,
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
  /** Portal the panel to <body> and position it from the trigger's own rect.
   *  Needed when the trigger sits in a narrow, `overflow-hidden` or low-z
   *  column — the top bar is `z-10 overflow-hidden` inside a shell whose
   *  sidebar is `z-20`, so a panel anchored there was clipped at the column's
   *  edge AND painted under the sidebar: that is what made the notification
   *  tray unreadable. */
  fixed?: boolean;
}) {
  useEffect(() => {
    if (!open) return;
    // A handler that already claimed Escape (a host modal, a hotkey rebind)
    // preventDefaults it; that claim wins, so this shield stands down.
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !e.defaultPrevented && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const anchorRef = useRef<HTMLSpanElement>(null);
  const [at, setAt] = useState<{ top: number; bottom: number; right: number; left: number; center: number } | null>(null);
  useLayoutEffect(() => {
    if (!open || !fixed) return;
    const place = () => {
      const box = anchorRef.current?.getBoundingClientRect();
      if (!box) return;
      setAt({
        top: box.bottom + 4,
        bottom: window.innerHeight - box.top + 4,
        // Distances/offsets, not one anchor: the panel keeps its alignment to
        // the trigger across a resize or a scroll, and every value is clamped
        // so the panel stays inside the viewport on a narrow window.
        right: Math.max(8, window.innerWidth - box.right),
        left: Math.min(Math.max(8, box.left), window.innerWidth - 8),
        center: Math.min(Math.max(box.width, box.left + box.width / 2), window.innerWidth - 8),
      });
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open, fixed]);

  if (!open) return null;
  const portaled = fixed && at;
  const panel = (
    <div
      role="menu"
      style={
        portaled
          ? {
              position: "fixed",
              ...(placement === "top" ? { bottom: at!.bottom } : { top: at!.top }),
              maxWidth: "calc(100vw - 1rem)",
              ...(align === "right"
                ? { right: at!.right }
                : align === "left"
                  ? { left: at!.left }
                  : { left: at!.center, transform: "translateX(-50%)" }),
            }
          : undefined
      }
      className={`anim-fade ${fixed ? "z-[60]" : "absolute z-50"} ${
        fixed ? "" : placement === "top" ? "bottom-full mb-1" : "mt-1"
      } rounded-xl shadow-2xl bg-zinc-950 border border-white/10 ${
        fixed
          ? ""
          : align === "right"
            ? "right-0"
            : align === "left"
              ? "left-0"
              : "left-1/2 -translate-x-1/2"
      } ${panelClass}`}
    >
      {children}
    </div>
  );
  return (
    <>
      {shield && <div className="fixed inset-0 z-40" onClick={onClose} />}
      {fixed ? (
        <>
          {/* The in-place marker the panel is positioned from: the caller's
              own wrapper, measured instead of assumed. It is a BLOCK box of
              zero size, not an inline one: an inline-block on the text
              baseline adds a line box (~24 px at the app's line-height), which
              grew the notification bell's 36 px wrapper to 60 px the moment the
              tray opened — the bar centers that wrapper, so the whole button
              (and its icon) jumped 12 px up under the cursor. A block box
              contributes its own height, which is zero. */}
          <span ref={anchorRef} className="block w-0 h-0" aria-hidden="true" />
          {portaled && createPortal(panel, document.body)}
        </>
      ) : (
        panel
      )}
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
      className={`w-full text-left text-xs px-2.5 py-1.5 rounded-lg flex items-center gap-2.5 transition-colors disabled:opacity-40 tap ${
        danger ? "text-red-300 hover:bg-red-950/50" : "text-zinc-300 hover:bg-white/10"
      } ${active ? "bg-white/10" : ""}`}
    >
      {Icon && <Icon className="h-3.5 w-3.5 shrink-0" />}
      <span className="flex-1 truncate">{label}</span>
    </button>
  );
}
