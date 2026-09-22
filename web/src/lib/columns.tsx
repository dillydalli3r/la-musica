/** Column defs, visibility/width prefs, and the columns chooser + resizer.
 *
 * Imported by the library/album/cached/trash tables and their tracklists
 * (see AlbumPage, LibraryPage, DownloadsPage, TrashPage, FavoritesPage).
 * Column visibility and widths persist per view in localStorage. */

import { useEffect, useRef, useState } from "react";
import { Columns3, X } from "lucide-react";
import type { MouseEvent as ReactMouseEvent } from "react";

/** One table column definition: id for prefs/widths, header label, sort key.
 *  defHidden columns exist (sortable, toggleable via the columns chooser)
 *  but are not part of the default visible set.
 *  `tag` marks a user-added column backed by that tag (see useCustomColumns);
 *  the columns chooser offers a remove control for exactly those. */
export interface Col {
  id: string;
  label: string;
  sortKey: string;
  defHidden?: boolean;
  tag?: string;
}

/** A user-added column: one file tag shown as its own table column. */
export interface CustomCol {
  id: string;
  label: string;
  tag: string;
}

/** Column layout shared by every album tracklist — the album page table and
 *  the expanded album rows in the library albums view are the same table, so
 *  visible columns and drag-resized widths are stored under one prefs key.
 *  These widths stay relative (percentages plus an auto Title) so the page
 *  keeps the fluid geometry it has; the floor that stops the title being
 *  squeezed out of existence is `ALBUM_TRACK_MIN_W` on the table. */
export const ALBUM_TRACK_COL_W: Record<string, string> = {
  num: "w-16",
  cover: "w-[52px]",
  title: "w-auto",
  genre: "w-[16%]",
  dur: "w-20",
  bitrate: "w-[16%]",
  dr: "w-[10%]",
};
/** Floor for a table whose columns carry their own width (`_COL_W` maps): a
 *  fixed layout squares up to `w-full` by scaling every column down and
 *  handing the auto column whatever is left, so a table too narrow for its
 *  columns renders the name column one character per line — the library's
 *  track table measured a 0 px title at 580 px wide. `min-w-max` is the
 *  table's floor: max-content of a fixed layout is the sum of its columns, so
 *  the table stops at the columns' own widths and the `overflow-x-auto`
 *  wrapper scrolls from there instead. `md:` only, because below it the phone
 *  fold has already dropped the columns that do not fit and the couple left
 *  share the width with room to spare — same as before this floor existed. */
export const TABLE_FIT = "w-full md:min-w-max";

/** The album tracklist's floor, derived from its own columns rather than
 *  picked: the fixed ones (num 64 + cover 52 + dur 80) and the corner control
 *  (76) take 272 px up front, the percentage ones (genre 16% + bitrate 16% +
 *  DR 10%) take 42% of what is left, and the remainder goes to the auto
 *  Title column — 814 px is the width where that title is still 200 px.
 *  Narrower than this the fixed layout hands the title the leftover, which
 *  measured 0 px at a 342 px phone width, so the table holds this width and
 *  its wrapper scrolls instead. The library's expanded album rows share these
 *  columns without the corner control, so the floor is slightly generous
 *  there — harmless, it only starts scrolling a little sooner. */
export const ALBUM_TRACK_MIN_W = "min-w-[814px]";

/** Floor for a user-added tag column (`tag:*` ids): the values are free text,
 *  so it gets the same readable minimum as a genre cell. Without a width of its
 *  own such a column is auto, and TABLE_FIT's `min-w-max` would then grow the
 *  table to the longest tag value it can find. */
export const TAG_COL_W = "w-[96px]";

/** Default album-tracklist columns (num/cover/title/genre/dur/bitrate/DR). */
export const ALBUM_TRACK_COLS: Col[] = [
  { id: "num", label: "#", sortKey: "tracknumber" },
  { id: "cover", label: "", sortKey: "" },
  { id: "title", label: "Title", sortKey: "tags.TITLE" },
  { id: "genre", label: "Genre", sortKey: "tags.GENRE" },
  { id: "dur", label: "Dur", sortKey: "tech.length" },
  { id: "bitrate", label: "Bitrate", sortKey: "tech.bitrate" },
  { id: "dr", label: "DR", sortKey: "tags.DYNAMIC RANGE" },
];
/** Visible-column ids per view, persisted in localStorage; toggle flips one id. */
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

/** User-added tag columns per view, persisted in localStorage. The id
 *  derives from the tag (`tag:MOOD`), so adding the same tag twice is a
 *  no-op and the column survives a reload. `add` returns the new id ("" when
 *  the tag is empty or already has a column) so callers can show it right
 *  away — a column the user just created should not start hidden. */
export function useCustomColumns(key: string): [CustomCol[], (tag: string, label?: string) => string, (id: string) => void] {
  const storageKey = `mlo-customcols-${key}`;
  const [customs, setCustoms] = useState<CustomCol[]>(() => {
    try {
      const raw = localStorage.getItem(storageKey);
      if (raw) {
        const parsed = JSON.parse(raw) as CustomCol[];
        if (Array.isArray(parsed))
          return parsed.filter((c) => c && typeof c.id === "string" && typeof c.tag === "string" && !!c.tag.trim());
      }
    } catch {
      /* fall through to no custom columns */
    }
    return [];
  });
  const save = (next: CustomCol[]) => {
    try {
      localStorage.setItem(storageKey, JSON.stringify(next));
    } catch {
      /* ignore */
    }
  };
  const add = (tag: string, label?: string) => {
    const t = tag.trim().replace(/^tag:/i, "").toUpperCase();
    if (!t) return "";
    const id = `tag:${t}`;
    if (customs.some((c) => c.id === id)) return "";
    const next = [...customs, { id, label: (label ?? "").trim() || t, tag: t }];
    setCustoms(next);
    save(next);
    return id;
  };
  const remove = (id: string) => {
    const next = customs.filter((c) => c.id !== id);
    setCustoms(next);
    save(next);
  };
  return [customs, add, remove];
}

/** Cell text of a tag column: the row's tags first (case-insensitive key),
 *  then its technical record, else "". Album rows pass their `meta` as
 *  `tags` — it is the same tag map, one level up. `tag` is either the bare
 *  tag or the column id (`tag:MOOD`) — callers hold one or the other. */
export function customColValue(row: { tags?: unknown; tech?: unknown } | null | undefined, tag: string): string {
  const want = tag.replace(/^tag:/i, "").toLowerCase();
  for (const rec of [row?.tags, row?.tech]) {
    if (!rec || typeof rec !== "object") continue;
    const key = Object.keys(rec).find((k) => k.toLowerCase() === want);
    const v = key ? (rec as Record<string, unknown>)[key] : null;
    if (v !== null && v !== undefined && v !== "") return String(v);
  }
  return "";
}

/** Custom columns as table columns — `scope` picks the dotted sort key
 *  rowValue resolves: "tags.X" on a track table, "meta.X" on an album one. */
export function customCols(customs: CustomCol[], scope: "tags" | "meta"): Col[] {
  return customs.map((c) => ({ id: c.id, label: c.label, sortKey: `${scope}.${c.tag}`, tag: c.tag }));
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
 * cell (where a labeled button would shout). With `onAddCustom` the menu also
 * grows the tag-column form; columns carrying a `tag` get a remove control. */
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
  extraOnRemoveCustom,
  extraTitle = "Tracklist columns",
  iconOnly = false,
  onAddCustom,
  onRemoveCustom,
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
  /** Removes a tag column from the nested table's list — the same list the
   *  primary table's `onRemoveCustom` edits when the two share one customs
   *  key (the album page and the library's expanded album rows do). */
  extraOnRemoveCustom?: (id: string) => void;
  extraTitle?: string;
  iconOnly?: boolean;
  /** Adds a tag-backed column (see useCustomColumns) — the menu shows the
   *  "Add tag column" form only when this is wired. */
  onAddCustom?: (tag: string, label?: string) => void;
  onRemoveCustom?: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [tag, setTag] = useState("");
  const [label, setLabel] = useState("");
  const add = () => {
    if (!tag.trim()) return;
    onAddCustom?.(tag, label);
    setTag("");
    setLabel("");
  };
  return (
    <div className="relative">
      <button
        className={
          iconOnly
            ? `p-1.5 rounded-lg border transition-colors tap min-w-11 md:min-w-0 ${
                open
                  ? "text-accent border-accent/50 bg-raise"
                  : "border-border bg-panel/60 text-zinc-500 hover:text-white hover:bg-raise"
              }`
            : `btn-ghost !py-1.5 text-xs tap ${open ? "!text-white !bg-raise" : ""}`
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
          <div className={`absolute right-0 top-full mt-1 z-50 bg-card border border-border rounded-lg p-2 ${onAddCustom ? "w-56" : "w-48"} shadow-2xl`}>
            <div className="text-[10px] uppercase tracking-wider text-zinc-500 px-2 pt-1 pb-1.5">{title}</div>
            {cols.map((c) => (
              <div key={c.id} className="flex items-center gap-2 px-2 py-1.5 text-xs hover:bg-panel rounded tap">
                <label className="flex items-center gap-2 cursor-pointer min-w-0 flex-1">
                  <input type="checkbox" checked={visible.includes(c.id)} onChange={() => onToggle(c.id)} className="" />
                  <span className="truncate" title={c.tag ? `Tag: ${c.tag}` : undefined}>{c.label || "Cover"}</span>
                </label>
                {c.tag && onRemoveCustom && (
                  <button
                    className="shrink-0 text-zinc-600 hover:text-red-400"
                    title={`Remove the ${c.label} column`}
                    aria-label={`Remove the ${c.label} column`}
                    onClick={() => onRemoveCustom(c.id)}
                  >
                    <X className="h-3 w-3" />
                  </button>
                )}
              </div>
            ))}
            {onAddCustom && (
              <>
                <div className="border-t border-border my-1.5" />
                <div className="text-[10px] uppercase tracking-wider text-zinc-500 px-2 pt-1 pb-1.5">Add tag column</div>
                <div className="flex gap-1 px-2">
                  <input
                    className="input !py-1 !px-2 text-xs min-w-0 flex-1 tap"
                    placeholder="TAG"
                    title="Tag name — e.g. MOOD, COMPOSER, CATALOGNUMBER"
                    value={tag}
                    onChange={(e) => setTag(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && add()}
                  />
                  <input
                    className="input !py-1 !px-2 text-xs min-w-0 flex-1 tap"
                    placeholder="Label"
                    title="Optional header label — the tag name is used when empty"
                    value={label}
                    onChange={(e) => setLabel(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && add()}
                  />
                </div>
                <button className="btn-ghost w-full !py-1 text-xs mt-1 tap" onClick={add} disabled={!tag.trim()}>
                  Add column
                </button>
              </>
            )}
            {extraCols && onExtraToggle && extraVisible && (
              <>
                <div className="border-t border-border my-1.5" />
                <div className="text-[10px] uppercase tracking-wider text-zinc-500 px-2 pt-1 pb-1.5">{extraTitle}</div>
                {extraCols.map((c) => (
                  <div key={c.id} className="flex items-center gap-2 px-2 py-1.5 text-xs hover:bg-panel rounded tap">
                    <label className="flex items-center gap-2 cursor-pointer min-w-0 flex-1">
                      <input type="checkbox" checked={extraVisible.includes(c.id)} onChange={() => onExtraToggle(c.id)} className="" />
                      <span className="truncate" title={c.tag ? `Tag: ${c.tag}` : undefined}>{c.label || "Cover"}</span>
                    </label>
                    {c.tag && extraOnRemoveCustom && (
                      <button
                        className="shrink-0 text-zinc-600 hover:text-red-400"
                        title={`Remove the ${c.label} column`}
                        aria-label={`Remove the ${c.label} column`}
                        onClick={() => extraOnRemoveCustom(c.id)}
                      >
                        <X className="h-3 w-3" />
                      </button>
                    )}
                  </div>
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
  const cleanupRef = useRef<(() => void) | null>(null);
  // A drag that's in progress when this th unmounts (sort/filter change,
  // navigation) would otherwise leak both window listeners and the
  // col-resize cursor.
  useEffect(() => () => cleanupRef.current?.(), []);
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
      cleanupRef.current = null;
    };
    cleanupRef.current = up;
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
