import { useState } from "react";
import { Columns3 } from "lucide-react";
import type { MouseEvent as ReactMouseEvent } from "react";

/** One table column definition: id for prefs/widths, header label, sort key.
 *  defHidden columns exist (sortable, toggleable via the columns chooser)
 *  but are not part of the default visible set. */
export interface Col {
  id: string;
  label: string;
  sortKey: string;
  defHidden?: boolean;
}

/** Column layout shared by every album tracklist — the album page table and
 *  the expanded album rows in the library albums view are the same table, so
 *  visible columns and drag-resized widths are stored under one prefs key. */
export const ALBUM_TRACK_COL_W: Record<string, string> = {
  num: "w-16",
  cover: "w-[52px]",
  title: "w-auto",
  genre: "w-[16%]",
  dur: "w-20",
  bitrate: "w-[16%]",
  dr: "w-[10%]",
};

export const ALBUM_TRACK_COLS: Col[] = [
  { id: "num", label: "#", sortKey: "tracknumber" },
  { id: "cover", label: "", sortKey: "" },
  { id: "title", label: "Title", sortKey: "tags.TITLE" },
  { id: "genre", label: "Genre", sortKey: "tags.GENRE" },
  { id: "dur", label: "Dur", sortKey: "tech.length" },
  { id: "bitrate", label: "Bitrate", sortKey: "tech.bitrate" },
  { id: "dr", label: "DR", sortKey: "tags.DYNAMIC RANGE" },
];

export function useColumnPrefs(key: string, defs: Col[]): [string[], (id: string) => void] {
  // v3: type/INST columns joined the default sets and credit columns became
  // toggleable opt-ins — bumping the key lets the new defaults apply once
  // for everyone (older prefs lived under mlo-cols-* / mlo-cols2-*).
  const storageKey = `mlo-cols3-${key}`;
  const [visible, setVisible] = useState<string[]>(() => {
    try {
      const raw = localStorage.getItem(storageKey);
      if (raw) {
        const arr = JSON.parse(raw) as string[];
        const ids = new Set(defs.map((d) => d.id));
        const kept = arr.filter((x) => ids.has(x));
        if (kept.length) return kept;
      }
    } catch {
      /* fall through to defaults */
    }
    return defs.filter((d) => !d.defHidden).map((d) => d.id);
  });
  const toggle = (id: string) =>
    setVisible((v) => {
      const next = v.includes(id) ? v.filter((x) => x !== id) : [...v, id];
      try {
        localStorage.setItem(storageKey, JSON.stringify(next));
      } catch {
        /* ignore */
      }
      return next;
    });
  return [visible, toggle];
}

/** Drag-resized column widths per table view, persisted in localStorage.
 * Absent entries fall back to the fluid % classes; double-clicking a
 * handle (or the Columns menu reset) clears them. */
export function useColumnWidths(key: string): [Record<string, number>, (id: string, px: number) => void, () => void] {
  const storageKey = `mlo-colw-${key}`;
  const [widths, setWidths] = useState<Record<string, number>>(() => {
    try {
      const raw = localStorage.getItem(storageKey);
      if (raw) {
        const parsed = JSON.parse(raw) as Record<string, number>;
        if (parsed && typeof parsed === "object") return parsed;
      }
    } catch {
      /* ignore */
    }
    return {};
  });
  const set = (id: string, px: number) =>
    setWidths((w) => {
      const next = { ...w, [id]: Math.max(40, Math.min(900, Math.round(px))) };
      try {
        localStorage.setItem(storageKey, JSON.stringify(next));
      } catch {
        /* ignore */
      }
      return next;
    });
  const reset = () => {
    setWidths({});
    try {
      localStorage.removeItem(storageKey);
    } catch {
      /* ignore */
    }
  };
  return [widths, set, reset];
}

/** Per-view "which columns are visible" menu with a width reset. An optional
 * second section (extraCols) covers a nested table that lives inside the same
 * view — e.g. the tracklist under an expanded album row. `iconOnly` renders
 * a small square icon button for placement inside a table's corner header
 * cell (where a labeled button would shout). */
export function ColumnsMenu({
  cols,
  visible,
  onToggle,
  fullDates,
  onFullDates,
  onResetWidths,
  hasCustomWidths,
  title = "Visible columns",
  extraCols,
  extraVisible,
  onExtraToggle,
  extraTitle = "Tracklist columns",
  iconOnly = false,
}: {
  cols: Col[];
  visible: string[];
  onToggle: (id: string) => void;
  fullDates?: boolean;
  onFullDates?: (v: boolean) => void;
  onResetWidths?: () => void;
  hasCustomWidths?: boolean;
  title?: string;
  extraCols?: Col[];
  extraVisible?: string[];
  onExtraToggle?: (id: string) => void;
  extraTitle?: string;
  iconOnly?: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="relative">
      <button
        className={
          iconOnly
            ? `p-1.5 rounded-md border transition-colors ${
                open
                  ? "text-accent border-accent/50 bg-raise"
                  : "border-border bg-panel/60 text-zinc-500 hover:text-white hover:bg-raise"
              }`
            : `btn-ghost !py-1.5 text-xs ${open ? "!text-white !bg-raise" : ""}`
        }
        onClick={() => setOpen(!open)}
        title="Columns"
        aria-label="Columns"
      >
        <Columns3 className="h-3.5 w-3.5" />
        {!iconOnly && " Columns"}
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute right-0 top-full mt-1 z-50 bg-card border border-border rounded-lg p-2 w-48 shadow-2xl">
            <div className="text-[10px] uppercase tracking-wider text-zinc-500 px-2 pt-1 pb-1.5">{title}</div>
            {cols.map((c) => (
              <label key={c.id} className="flex items-center gap-2 px-2 py-1.5 text-xs cursor-pointer hover:bg-panel rounded">
                <input type="checkbox" checked={visible.includes(c.id)} onChange={() => onToggle(c.id)} className="" />
                {c.label || "Cover"}
              </label>
            ))}
            {extraCols && onExtraToggle && extraVisible && (
              <>
                <div className="border-t border-border my-1.5" />
                <div className="text-[10px] uppercase tracking-wider text-zinc-500 px-2 pt-1 pb-1.5">{extraTitle}</div>
                {extraCols.map((c) => (
                  <label key={c.id} className="flex items-center gap-2 px-2 py-1.5 text-xs cursor-pointer hover:bg-panel rounded">
                    <input type="checkbox" checked={extraVisible.includes(c.id)} onChange={() => onExtraToggle(c.id)} className="" />
                    {c.label || "Cover"}
                  </label>
                ))}
              </>
            )}
            {onFullDates && (
              <>
                <div className="border-t border-border my-1.5" />
                <label className="flex items-center gap-2 px-2 py-1.5 text-xs cursor-pointer hover:bg-panel rounded">
                  <input type="checkbox" checked={!!fullDates} onChange={(e) => onFullDates(e.target.checked)} />
                  Show full dates
                </label>
              </>
            )}
            {onResetWidths && (
              <>
                <div className="border-t border-border my-1.5" />
                <button
                  className="w-full text-left px-2 py-1.5 text-xs text-zinc-400 hover:text-white hover:bg-panel rounded disabled:opacity-40"
                  onClick={() => {
                    onResetWidths();
                    setOpen(false);
                  }}
                  disabled={!hasCustomWidths}
                  title="Restore the default fluid column widths"
                >
                  Reset column widths
                </button>
              </>
            )}
          </div>
        </>
      )}
    </div>
  );
}

/** Drag handle that resizes the column it lives in (persisted per view by
 * the caller). Lives at a th's right edge; the th needs `relative`. */
export function ColumnResizer({ width, onDrag, onReset }: {
  width: number | undefined;
  onDrag: (px: number) => void;
  onReset: () => void;
}) {
  const start = (e: ReactMouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const x0 = e.clientX;
    const w0 = width ?? (e.currentTarget.parentElement as HTMLElement)?.getBoundingClientRect().width ?? 100;
    const move = (ev: MouseEvent) => onDrag(ev.clientX - x0 + w0);
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };
  return (
    <span
      className="absolute right-0 top-0 h-full w-1.5 cursor-col-resize hover:bg-accent/40"
      onMouseDown={start}
      onDoubleClick={(e) => {
        e.stopPropagation();
        onReset();
      }}
      title="Drag to resize · double-click to reset"
      onClick={(e) => e.stopPropagation()}
    />
  );
}
