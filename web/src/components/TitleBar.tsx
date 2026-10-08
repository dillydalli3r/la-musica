import { useEffect, useState } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { IN_TAURI } from "../api";

/** The three window glyphs, drawn HERE rather than taken from the icon set.
 *
 *  An icon set's glyphs fill different shares of their own box — Lucide's
 *  `Minus`, `Square` and `X` cover 58 %, 75 % and 50 % of a 24×24 viewBox — so
 *  at one icon size the cross read smaller and lighter than the two beside it
 *  ("the x doesn't seem as consistent as the other buttons"). Window chrome has
 *  its own metrics instead: all three are drawn in ONE 10×10 box with the same
 *  stroke, which is what makes them read as a set, and `crispEdges` keeps the
 *  axis-aligned ones on whole pixels rather than blurred across two. */
const GLYPHS = {
  minus: "M0.5 5 H9.5",
  square: "M0.5 0.5 H9.5 V9.5 H0.5 Z",
  restore: "M2.5 0.5 H9.5 V7.5 M0.5 2.5 H7.5 V9.5 Z",
  cross: "M0.5 0.5 L9.5 9.5 M9.5 0.5 L0.5 9.5",
} as const;

function Glyph({ d, crisp }: { d: string; crisp?: boolean }) {
  return (
    <svg
      viewBox="0 0 10 10"
      className="h-3 w-3"
      fill="none"
      stroke="currentColor"
      strokeWidth={1}
      shapeRendering={crisp ? "crispEdges" : undefined}
      aria-hidden="true"
    >
      <path d={d} />
    </svg>
  );
}

/** One window control: a glyph, the name it announces, and the call it makes.
 *  All three are that shape, so the bar below is a list rather than a wall of
 *  near-identical buttons. */
function WindowButton({
  d,
  crisp,
  label,
  danger,
  onClick,
}: {
  d: string;
  crisp?: boolean;
  label: string;
  danger?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={label}
      aria-label={label}
      className={`h-full w-11 shrink-0 flex items-center justify-center text-zinc-400 transition-colors ${
        danger ? "hover:bg-red-600 hover:text-white" : "hover:bg-raise hover:text-white"
      }`}
    >
      <Glyph d={d} crisp={crisp} />
    </button>
  );
}

/** The bar's height — and the top inset every surface that anchors to the
 *  WINDOW instead of to the app box has to leave it (index.css's
 *  `--mlo-titlebar-h`). One number, because the strip the bar takes in the
 *  app's own flow and the strip it covers for those surfaces are the same
 *  strip. */
export const TITLEBAR_H = "2rem";

/** The window's own title bar — the strip the OS used to draw.
 *
 *  The shell runs undecorated (`decorations: false` in tauri.conf.json),
 *  because the stock Windows bar is a light-themed slab that matches nothing
 *  else on this screen. This is its replacement, in the app's own ink: the
 *  brand on one side, the three controls on the other, and everything between
 *  them dragging the window (`data-tauri-drag-region="deep"` is Tauri's own
 *  injected handler — the same one that maximizes on a double click).
 *
 *  Rendered on EVERY screen of the desktop shell, the setup wizard and the
 *  address/claim screens included: an undecorated window with no bar there
 *  would have no way left to be moved or closed. `null` everywhere else — a
 *  plain browser gets its window chrome from its host.
 *
 *  It sits at `z-[70]`, above the dialogs (`z-[60]`): the window controls are
 *  the one thing that must never be painted over, so a modal dims the app and
 *  leaves the chrome alone.
 *
 *  macOS draws its controls on the LEFT, in its own order — the whole point of
 *  a custom bar is that it looks native, and a Mac with Windows' three buttons
 *  in the corner is the tell that it is not. */
export default function TitleBar() {
  const [maximized, setMaximized] = useState(false);

  // Publish the bar's height as that inset. The bar sits in the app's flow, so
  // everything INSIDE the shell is below it already; a `fixed inset-0` overlay
  // anchors to the window, and its own top row came out underneath the window
  // controls on Windows (owner report). Set on <html> rather than the app box
  // because overlays are portalled to <body>. Retired with the bar, so a browser
  // — where the host draws the chrome and this component renders nothing —
  // keeps 0. */
  useEffect(() => {
    if (!IN_TAURI) return;
    const root = document.documentElement;
    root.style.setProperty("--mlo-titlebar-h", TITLEBAR_H);
    return () => {
      root.style.removeProperty("--mlo-titlebar-h");
    };
  }, []);

  useEffect(() => {
    if (!IN_TAURI) return;
    const win = getCurrentWindow();
    let alive = true;
    const read = () => {
      win.isMaximized().then((v) => {
        if (alive) setMaximized(v);
      }).catch(() => {});
    };
    read();
    // The state is only ever read to pick the icon — the toggle itself is the
    // window's business, so this one listener is the whole of the bookkeeping.
    const stop = win.onResized(read);
    return () => {
      alive = false;
      stop.then((off) => off()).catch(() => {});
    };
  }, []);

  if (!IN_TAURI) return null;
  const win = getCurrentWindow();
  // Desktop macOS — never an iPad in desktop mode, which differs by reporting
  // touch points.
  const mac = /macintosh|mac os x/i.test(navigator.userAgent) && (navigator.maxTouchPoints || 0) <= 1;

  const minimize = { d: GLYPHS.minus, crisp: true, label: "Minimize", onClick: () => void win.minimize() };
  const zoom = {
    d: maximized ? GLYPHS.restore : GLYPHS.square,
    crisp: true,
    label: maximized ? "Restore" : "Maximize",
    onClick: () => void win.toggleMaximize(),
  };
  const close = { d: GLYPHS.cross, label: "Close", danger: true, onClick: () => void win.close() };
  const controls = mac ? [close, minimize, zoom] : [minimize, zoom, close];

  return (
    // `select-none`: a drag across the bar must never start a text selection,
    // which is what a stray drag over the brand looked like.
    <div
      data-tauri-drag-region="deep"
      style={{ height: TITLEBAR_H }}
      className="relative z-[70] shrink-0 flex items-stretch bg-bg text-zinc-500 select-none"
    >
      {mac && (
        <div className="flex items-stretch">
          {controls.map((c) => <WindowButton key={c.label} {...c} />)}
        </div>
      )}
      <div className="flex items-center gap-2 pl-3 min-w-0">
        <img src="/icon.png" alt="" className="h-4 w-4 rounded-[3px] object-cover shrink-0" />
        <span className="text-[11px] font-semibold tracking-tight">la musica</span>
      </div>
      {/* The drag area proper: an empty box between the brand and the
          controls, so neither needs to be a drag surface itself. */}
      <div className="flex-1" />
      {!mac && (
        <div className="flex items-stretch">
          {controls.map((c) => <WindowButton key={c.label} {...c} />)}
        </div>
      )}
    </div>
  );
}
