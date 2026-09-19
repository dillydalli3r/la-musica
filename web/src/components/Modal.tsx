import { useEffect, useRef } from "react";
import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { X } from "lucide-react";

const FOCUSABLE =
  'a[href],button:not([disabled]),textarea:not([disabled]),input:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])';

/** The one dialog shell every modal uses: backdrop click + Escape close,
 *  `role="dialog"` / `aria-modal`, focus moved into the panel and restored to
 *  the opener on close, Tab trapped inside, and the shared `.anim-pop` entry.
 *
 *  The header (title / subtitle / extra actions) and the close button are
 *  supplied here; the body is whatever the caller passes as children. */
export default function Modal({
  onClose,
  title,
  subtitle,
  icon: Icon,
  headerExtra,
  footer,
  width = "max-w-xl",
  bodyClass = "px-5 py-4",
  panelClass = "",
  z = "z-[60]",
  children,
}: {
  onClose: () => void;
  title?: ReactNode;
  subtitle?: ReactNode;
  icon?: LucideIcon;
  /** Buttons next to the close cross (refresh, upload…). */
  headerExtra?: ReactNode;
  /** Pinned row under the scrolling body. */
  footer?: ReactNode;
  /** Panel width utility — always a max-width so phones never overflow. */
  width?: string;
  bodyClass?: string;
  panelClass?: string;
  z?: string;
  children: ReactNode;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const body = useRef<HTMLDivElement>(null);
  const opener = useRef<HTMLElement | null>(null);

  useEffect(() => {
    opener.current = document.activeElement as HTMLElement | null;
    // Focus the first control of the BODY, not the header — otherwise every
    // dialog would open with the close cross focused and a caller's
    // autoFocus input would be overridden. `data-autofocus` wins outright.
    const firstFocusable =
      body.current?.querySelector<HTMLElement>("[data-autofocus]") ??
      body.current?.querySelector<HTMLElement>(FOCUSABLE) ??
      panel.current;
    firstFocusable?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
        return;
      }
      if (e.key !== "Tab" || !panel.current) return;
      const items = Array.from(panel.current.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (el) => el.offsetParent !== null
      );
      if (!items.length) return;
      const firstEl = items[0];
      const lastEl = items[items.length - 1];
      if (e.shiftKey && document.activeElement === firstEl) {
        e.preventDefault();
        lastEl.focus();
      } else if (!e.shiftKey && document.activeElement === lastEl) {
        e.preventDefault();
        firstEl.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      // Put focus back where the dialog was opened from — a modal that drops
      // focus on <body> strands keyboard users at the top of the page.
      opener.current?.focus?.();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div
      className={`fixed inset-0 ${z} bg-black/70 backdrop-blur-sm flex items-center justify-center p-4`}
      onClick={onClose}
    >
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === "string" ? title : "Dialog"}
        className={`anim-pop w-full ${width} max-h-[85vh] flex flex-col bg-card border border-border rounded-xl shadow-2xl ${panelClass}`}
        onClick={(e) => e.stopPropagation()}
      >
        {(title || headerExtra) && (
          <div className="flex flex-wrap items-center gap-3 gap-y-2 px-5 py-3.5 border-b border-border shrink-0">
            <div className="flex-1 min-w-0">
              {title && (
                <div className="text-sm font-semibold flex items-center gap-2 min-w-0">
                  {Icon && <Icon className="h-4 w-4 text-accent shrink-0" />}
                  <span className="truncate">{title}</span>
                </div>
              )}
              {subtitle && <div className="text-[11px] text-zinc-500 mt-0.5">{subtitle}</div>}
            </div>
            {headerExtra}
            <button
              className="p-1.5 rounded-lg hover:bg-raise text-zinc-400 hover:text-white shrink-0 tap-hit"
              onClick={onClose}
              title="Close (Esc)"
              aria-label="Close"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        )}
        <div ref={body} className={`flex-1 min-h-0 overflow-auto ${bodyClass}`}>{children}</div>
        {/* The footer is caller markup, so the wrap has to be applied to whatever
            row the caller hands in: `[&>*]` keeps every button reachable at
            390px (the rows are full-width block children, so a wrapping row can
            only ever wrap, never change the desktop layout). */}
        {footer && (
          <div className="shrink-0 border-t border-border px-5 py-3 [&>*]:flex-wrap [&>*]:gap-y-2">{footer}</div>
        )}
      </div>
    </div>
  );
}
