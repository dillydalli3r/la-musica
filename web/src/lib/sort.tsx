/** Table sorting: toggle state, row comparison, and the sortable header.
 *
 * Imported by the library/cached/trash/favorites tables and album
 * tracklists (see LibraryPage, AlbumPage, CachedTracksView). */

import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";
import type { CSSProperties, ReactNode } from "react";
/** Active table sort: dotted row key + direction. */
export interface SortState {
  key: string;
  dir: 1 | -1;
}
/** Toggle sort on a column: same key flips direction, new key sorts ascending. */
export function toggleSort(current: SortState | null, key: string): SortState {
  if (current?.key === key) return { key, dir: current.dir === 1 ? -1 : 1 };
  return { key, dir: 1 };
}

/** Resolve a dotted path like "tags.GENRE" or "tech.length" against a row. */
export function rowValue(row: Record<string, any>, key: string): unknown {
  let v: unknown = row;
  for (const part of key.split(".")) {
    if (v === null || v === undefined) return undefined;
    v = (v as Record<string, unknown>)[part];
  }
  return v;
}
/** Null-tolerant comparison: numbers numerically, numeric strings by value, rest case-insensitive. */
export function compareValues(a: unknown, b: unknown): number {
  if (a === null || a === undefined) return b === null || b === undefined ? 0 : -1;
  if (b === null || b === undefined) return 1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  // Numeric strings ("10" vs "2") compare numerically, not as text.
  if (typeof a !== "object" && typeof b !== "object") {
    const sa = String(a);
    const sb = String(b);
    if (/^\d+$/.test(sa) && /^\d+$/.test(sb)) return Number(sa) - Number(sb);
  }
  if (typeof a === "number" || typeof b === "number") {
    const na = Number(a);
    const nb = Number(b);
    if (!Number.isNaN(na) && !Number.isNaN(nb)) return na - nb;
  }
  const sa = String(a).toLowerCase();
  const sb = String(b).toLowerCase();
  return sa < sb ? -1 : sa > sb ? 1 : 0;
}

/** Tie-breaker keys used when the primary sort values are equal. */
const SECONDARY_KEYS: Record<string, string[]> = {
  // Track # must fall back to disc number, then filename ("1-01 …").
  tracknumber: ["discnumber", "file"],
  discnumber: ["tracknumber", "file"],
};
/** Rows sorted by the active sort (stable tie-breaks via secondary keys, then filename); null sort returns input order. */
export function sortRows<T extends Record<string, any>>(rows: T[], sort: SortState | null): T[] {
  if (!sort) return rows;
  const key = sort.key;
  const secondary = SECONDARY_KEYS[key] ?? [];
  return [...rows].sort((x, y) => {
    let c = compareValues(rowValue(x, key), rowValue(y, key));
    if (c === 0) {
      for (const k of secondary) {
        c = compareValues(rowValue(x, k), rowValue(y, k));
        if (c !== 0) break;
      }
      if (c === 0 && key !== "file") c = compareValues(rowValue(x, "file"), rowValue(y, "file"));
    }
    return c * sort.dir;
  });
}

/** Disc then track number (missing numbers last), filename as the tie-break —
 *  the canonical order of an album tracklist and its disc groups. */
export function byDiscThenTrack(
  a: { discnumber?: number | null; tracknumber?: number | null; file?: string },
  b: { discnumber?: number | null; tracknumber?: number | null; file?: string },
): number {
  return (
    (a.discnumber ?? 99) - (b.discnumber ?? 99) ||
    (a.tracknumber ?? 999) - (b.tracknumber ?? 999) ||
    String(a.file).localeCompare(String(b.file))
  );
}
/** One disc's slice of an album tracklist. */
export interface DiscGroup<T> {
  disc: number | null;
  tracks: T[];
}

/** Group tracks by disc number for album tracklists. One group (disc=null)
 *  unless the album really has more than one disc. */
export function groupByDisc<T extends Record<string, any>>(tracks: T[]): DiscGroup<T>[] {
  const discs = new Set<number>();
  for (const t of tracks) {
    if (typeof t.discnumber === "number") discs.add(t.discnumber);
  }
  if (discs.size <= 1) return [{ disc: discs.size ? [...discs][0] : null, tracks }];
  const groups: DiscGroup<T>[] = [];
  for (const d of [...discs].sort((a, b) => a - b)) {
    groups.push({ disc: d, tracks: tracks.filter((t) => t.discnumber === d) });
  }
  const noDisc = tracks.filter((t) => typeof t.discnumber !== "number");
  if (noDisc.length) groups.push({ disc: null, tracks: noDisc });
  return groups;
}
/** Clickable sortable table header cell with direction indicator. */
export function SortHeader({
  label,
  sort,
  sortKey,
  onSort,
  className,
  style,
  children,
}: {
  label: string;
  sort: SortState | null;
  sortKey: string;
  onSort: (k: string) => void;
  className?: string;
  style?: CSSProperties;
  /** Rendered inside the th (used for the column resize handle). */
  children?: ReactNode;
}) {
  const active = sort?.key === sortKey;
  return (
    <th
      scope="col"
      aria-sort={active ? (sort!.dir === 1 ? "ascending" : "descending") : "none"}
      className={`th ${className ?? ""}`}
      style={style}
    >
      {/* the sort target is a real button so Enter/Space sort too — the resizer
          (children) stays a sibling, outside the button */}
      <button
        type="button"
        className="inline-flex items-center gap-1 cursor-pointer hover:text-zinc-300"
        onClick={() => onSort(sortKey)}
      >
        {label}
        {active ? (
          sort!.dir === 1 ? (
            <ArrowUp className="h-3 w-3 text-accent" />
          ) : (
            <ArrowDown className="h-3 w-3 text-accent" />
          )
        ) : (
          <ArrowUpDown className="h-3 w-3 opacity-30" />
        )}
      </button>
      {children}
    </th>
  );
}

/* The ColumnResizer handle lives in lib/columns.tsx (with the prefs hooks
   that persist the widths it edits) — one implementation, one import site. */