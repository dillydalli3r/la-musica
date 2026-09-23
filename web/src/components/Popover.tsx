import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { ReactNode, RefObject } from "react";
import type { LucideIcon } from "lucide-react";

/** The app's edge gutter: a fixed panel keeps this much air between itself and
 *  the viewport on every side. */
const GUTTER = 8;

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
  frost = false,
  rightGap,
  anchorRef,
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
  /** Frosted panel for menus that float over the fullscreen player's artwork:
   *  the blur-and-tint veil (`np-veil np-veil-dark np-veil-panel`,
   *  index.css) instead of the opaque `bg-zinc-950` fill, so the cover stays
   *  visible behind the panel. The tint is pinned to the DARK one whatever
   *  cover is up: these rows are zinc-300, and a light-polarity veil would
   *  invert them into unreadability. Every other caller keeps the solid panel
   *  it was designed with. */
  frost?: boolean;
  /** Fixed mode only: pin the panel this many px from the VIEWPORT's right
   *  edge instead of aligning its right edge to the trigger's. For a panel
   *  whose trigger lives in a padded bar, aligning to the trigger inherits
   *  that bar's own gutter, so the widest panel in the app (the notification
   *  tray) floated a bar-padding away from the screen edge on a wide window
   *  while every other edge-anchored surface sat at the app's 8 px gutter. */
  rightGap?: number;
  /** Fixed mode only: the caller's own trigger, measured instead of the
   *  zero-size marker below. The marker is a block box, so it sits at the
   *  trigger's LEFT edge — a `"right"`-aligned panel positioned from it lines
   *  its right edge up with that edge and lands a button-width further left
   *  than the panel the caller sees today. */
  anchorRef?: RefObject<HTMLElement | null>;
}) {
  useEffect(() => {
    if (!open) return;
    // A handler that already claimed Escape (a host modal, a hotkey rebind)
    // preventDefaults it; that claim wins, so this shield stands down.
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !e.defaultPrevented && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const markerRef = useRef<HTMLSpanElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const [at, setAt] = useState<
    { top: number; bottom: number; above: number; right: number; left: number; center: number; vw: number } | null
  >(null);
  useLayoutEffect(() => {
    if (!open || !fixed) return;
    const place = () => {
      const box = (anchorRef?.current ?? markerRef.current)?.getBoundingClientRect();
      if (!box) return;
      setAt({
        top: box.bottom + 4,
        bottom: window.innerHeight - box.top + 4,
        // The room ABOVE the trigger, which is what caps a top-placed panel:
        // `bottom` measures the room below its top edge and is the wrong bound
        // for a panel that opens upwards.
        above: box.top,
        // Distances/offsets, not one anchor: the panel keeps its alignment to
        // the trigger across a resize or a scroll, and every value is clamped
        // so the panel stays inside the viewport on a narrow window.
        right: rightGap != null
          ? Math.max(GUTTER, rightGap)
          : Math.max(GUTTER, window.innerWidth - box.right),
        left: Math.min(Math.max(GUTTER, box.left), window.innerWidth - GUTTER),
        center: Math.min(Math.max(box.width, box.left + box.width / 2), window.innerWidth - GUTTER),
        vw: window.innerWidth,
      });
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open, fixed, rightGap, anchorRef]);

  // The panel's own width, read once it is on screen: it is what decides
  // whether a right-aligned panel has room on the left at all. Measured (and
  // re-measured when the menu's contents change) instead of hardcoded, since
  // the panel class is the caller's.
  const [panelWidth, setPanelWidth] = useState(0);
  useLayoutEffect(() => {
    if (!open || !fixed) return;
    const w = panelRef.current?.getBoundingClientRect().width ?? 0;
    if (w && Math.abs(w - panelWidth) > 0.5) setPanelWidth(w);
  });

  // A right-aligned panel keeps its right edge on the trigger and grows
  // LEFTWARDS, so the room it has is what the trigger leaves on its left: the
  // panel's left edge lands at `vw - at.right - width`, and once that crosses
  // the gutter the panel would leave the window — or slide under the rail, as
  // the album cover menu did, its trigger sitting at the content pane's left
  // edge with the menu wider than the space in front of it. Flip to the
  // trigger's LEFT edge and open rightwards instead; a flip that still would
  // not fit is clamped to the gutter, so a panel wider than the window stops
  // at the edge rather than overflowing it.
  const flipped =
    align === "right" && !!at && panelWidth > 0 && at.right + panelWidth > at.vw - GUTTER;
  const flipLeft = at
    ? Math.min(at.left, Math.max(GUTTER, at.vw - GUTTER - panelWidth))
    : GUTTER;

  if (!open) return null;
  const portaled = fixed && at;
  const panel = (
    <div
      ref={panelRef}
      role="menu"
      style={
        portaled
          ? {
              position: "fixed",
              ...(placement === "top" ? { bottom: at!.bottom } : { top: at!.top }),
              maxWidth: "calc(100vw - 1rem)",
              // A panel taller than the room the trigger leaves below (or above)
              // it was the one thing that could not be reached: the app shell is
              // `h-dvh overflow-hidden`, so a flyout running past the fold is
              // clipped by the window with no way to scroll to it. The cap is
              // measured from the same trigger rect the position is, so it
              // follows the row on scroll and resize, and `dvh` keeps a phone's
              // URL bar out of the arithmetic. Panels that carry
              // `overflow-y-auto` scroll inside it; the rest simply stop.
              maxHeight: placement === "top"
                ? `calc(${at!.above}px - ${GUTTER}px)`
                : `calc(100dvh - ${at!.top}px - ${GUTTER}px)`,
              ...(align === "right"
                ? flipped
                  ? { left: flipLeft }
                  : { right: at!.right }
                : align === "left"
                  ? { left: at!.left }
                  : { left: at!.center, transform: "translateX(-50%)" }),
            }
          : undefined
      }
      className={`anim-fade ${fixed ? "z-[60]" : "absolute z-50"} ${
        fixed ? "" : placement === "top" ? "bottom-full mb-1" : "mt-1"
      } rounded-xl shadow-2xl border border-white/10 ${
        frost ? "np-veil np-veil-dark np-veil-panel" : "bg-zinc-950"
      } ${
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
              contributes its own height, which is zero. A caller that hands in
              its own trigger (`anchorRef`) needs none of this: the trigger
              itself is measured, which is also what keeps a right-aligned panel
              on the trigger's RIGHT edge. */}
          {!anchorRef && <span ref={markerRef} className="block w-0 h-0" aria-hidden="true" />}
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
