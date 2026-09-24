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
/** A phone (390 px) table keeps the row's own name and drops the numbers: at
 *  that width a row that keeps them squeezes the name to nothing, and the
 *  table's own scroll wrapper cannot give it back.
 *
 *  The class has to sit on the header AND on the cells, or the fixed-layout
 *  grid misaligns; `md` is where each column comes back. */
export const PHONE_HIDE = " hidden md:table-cell";

/** The track table's floors — one narrowest-usable width per column, in px,
 *  same rule as the album table's own map: they are also the table's floor,
 *  summed by TABLE_FIT's `min-w-max`, so a window wider than their sum shares
 *  the extra out in proportion.
 *
 *  Shared by every table that lists whole tracks in library order: the
 *  Library's Tracks view and the Export page's preview. They were two tables
 *  with two sets of numbers until the export preview's `#` column was 32 px
 *  wide — which is 8 px of content box after `td`'s px-3 padding, so a
 *  two-digit track number wrapped onto two lines. */
export const TRACK_COL_W: Record<string, string> = {
  // 64 px, not the 48 a single digit suggests: 24 px of the column is the
  // cell's own gutter, and what is left has to hold the widest number a row
  // can show. Library rows print a track number ("10"), and a row without one
  // is numbered by its position in the list ("200" in a full-library preview).
  num: "w-16",
  cover: "w-[52px]",
  // `md:` like the album table's name column: on a phone the title is the only
  // column left beside the cover, so it takes the whole row instead.
  //
  // 280 px, not the 220 it carried: the cell holds the name AND the marks that
  // belong to it (advisory, grade, cached, video) plus the row's own trailing
  // controls, and at 220 the title lost that fight — the link was squeezed to
  // zero and the Library's Tracks view rendered as empty rows (see
  // TrackTitleCell). 280 is what those parts need to sit on ONE line for an
  // ordinary title, which is what keeps the row 40 px tall instead of three
  // lines of marks under the name; the table's floor (TABLE_FIT's `min-w-max`)
  // grows with it and the wrapper scrolls, the documented trade for a
  // fixed-layout table.
  title: "md:w-[280px]",
  // 120, measured: "Artist Gamma" at the table's own font is 112 px wide, so
  // the old 108 broke the name across two lines inside a column whose whole
  // job is saying who the track is by. The floors below are sized the same way
  // (widest value + a few px of slack), which is what keeps a row one line tall
  // instead of three.
  artist: "w-[120px]",
  album: "w-[112px]",
  year: "w-16",
  genre: "w-24",
  // The MediumChip's own longest label ("Digital Media") is 108 px wide.
  media: "w-[112px]",
  // 80 px, the same floor the album tracklist gives its length column: an
  // hour-plus length is seven characters ("1:02:33"), which the old 64 px
  // floor could only break onto a second line.
  duration: "w-20",
  // 184, measured: `fmtTech`'s own string ("FLAC 16/44.1 · 104 kbps") is 177 px
  // — the app's precedent for this column is the duration one above, sized to
  // its widest value, and a format/bitrate readout that breaks into three lines
  // (one of them empty) is what the old 88 px floor did.
  bitrate: "w-[184px]",
  dr: "w-12",
  source: "w-20",
  type: "w-20",
  inst: "w-20",
  composer: "w-[112px]",
  lyricist: "w-[112px]",
  remixer: "w-[96px]",
  // The tracklist's own id for the same length column the Tracks view calls
  // `duration` (see TRACK_PHONE_CLS).
  dur: "w-20",
  // Five `sm` stars plus the click target around them — the same 104 px the
  // album table gives its Rating column, so a rating reads the same width
  // wherever it appears.
  rating: "w-[104px]",
};

/** The track table's columns, in render order. */
export const TRACK_COLS: Col[] = [
  { id: "num", label: "#", sortKey: "tracknumber" },
  { id: "cover", label: "", sortKey: "" },
  { id: "title", label: "Title", sortKey: "tags.TITLE" },
  { id: "artist", label: "Artist", sortKey: "artist" },
  { id: "album", label: "Album", sortKey: "album" },
  { id: "year", label: "Year", sortKey: "tags.DATE" },
  { id: "genre", label: "Genre", sortKey: "tags.GENRE" },
  { id: "media", label: "Media", sortKey: "tags.MEDIA" },
  { id: "duration", label: "Duration", sortKey: "tech.length" },
  { id: "bitrate", label: "Bitrate", sortKey: "tech.bitrate" },
  // ReplayGain deliberately has NO column: it is playback metadata — the
  // player applies it to keep loudness even between tracks. Only Dynamic
  // Range is shown.
  { id: "dr", label: "DR", sortKey: "tags.DYNAMIC RANGE" },
  { id: "source", label: "Source", sortKey: "tags.SOURCE" },
  { id: "type", label: "Type", sortKey: "is_video" },
  { id: "inst", label: "INST", sortKey: "tags.INSTRUMENTAL" },
  { id: "composer", label: "Composer", sortKey: "tags.COMPOSER", defHidden: true },
  { id: "lyricist", label: "Lyricist", sortKey: "tags.LYRICIST", defHidden: true },
  { id: "remixer", label: "Remixer", sortKey: "tags.REMIXER", defHidden: true },
];

/** The Library's Tracks view own Rating column.
 *
 *  Here rather than in `TRACK_COLS` because the Library is the only table whose
 *  rows carry a rating: the export preview and the offline cache draw the same
 *  tracks without any rating data, and a column they cannot fill would be a
 *  permanently blank 104 px on both. The value is the caller's own — the
 *  Library injects the folder/track ratings into its rows (`ratedTracks`), and
 *  the cell is a live star control, exactly like the album tracklist's. */
export const TRACK_RATING_COL: Col = { id: "rating", label: "Rating", sortKey: "rating" };

/** Both track tables (the Tracks view, the export preview and every album
 *  tracklist): only the cover and the title stay on a phone, every column id
 *  named here folds at `md`. The tables name the length column differently
 *  (`duration` in the Tracks view, `dur` in an album tracklist), which is why
 *  both ids appear. */
export const TRACK_PHONE_CLS: Record<string, string> = {
  num: PHONE_HIDE, artist: PHONE_HIDE, album: PHONE_HIDE, year: PHONE_HIDE,
  genre: PHONE_HIDE, media: PHONE_HIDE, duration: PHONE_HIDE, dur: PHONE_HIDE,
  bitrate: PHONE_HIDE, dr: PHONE_HIDE, source: PHONE_HIDE, type: PHONE_HIDE,
  inst: PHONE_HIDE, composer: PHONE_HIDE, lyricist: PHONE_HIDE, remixer: PHONE_HIDE,
  rating: PHONE_HIDE,
};

/** Tag columns the user added fold with the built-ins they sit beside. */
export function phoneHide(cls: Record<string, string>, id: string): string {
  return cls[id] ?? (id.startsWith("tag:") ? PHONE_HIDE : "");
}

/** Visible-column ids per view, persisted in localStorage; toggle flips one id.
 *
 *  The key is VERSIONED because the ids are (`mlo-cols4-*`): a list written by
 *  an older build holds ids this one no longer has, and the reader that only
 *  kept the ids it recognised turned that into a half-empty table — the owner's
 *  Albums view drew its expand chevron and nothing else, the Tracks view its
 *  row number, and no column chooser entry looked wrong, because the columns
 *  the old build never offered were never unticked. So a list from the previous
 *  key is MIGRATED, not trusted: the ids it still has are kept (a deliberate
 *  choice survives), and every column this build ships visible by default is
 *  added, because a prefs entry cannot be evidence about a column that did not
 *  exist when it was written. Unticking anything after that is stored under the
 *  new key and honoured for good. */
export function useColumnPrefs(key: string, defs: Col[]): [string[], (id: string) => void] {
  // v4: ids are versioned (the key moves when they change), and a v3 list is
  // migrated rather than filtered — see the block comment above.
  const storageKey = `mlo-cols4-${key}`;
  const legacyKey = `mlo-cols3-${key}`;
  const [visible, setVisible] = useState<string[]>(() => {
    const ids = new Set(defs.map((d) => d.id));
    const allVisible = defs.filter((c) => !c.defHidden).map((c) => c.id);
    try {
      const raw = localStorage.getItem(storageKey);
      if (raw) {
        const arr = JSON.parse(raw) as string[];
        const kept = arr.filter((x) => ids.has(x));
        if (kept.length) return kept;
      }
      const old = localStorage.getItem(legacyKey);
      if (old) {
        const arr = JSON.parse(old) as string[];
        const kept = arr.filter((x) => ids.has(x));
        const next = [...new Set([...kept, ...allVisible])];
        localStorage.setItem(storageKey, JSON.stringify(next));
        localStorage.removeItem(legacyKey);
        return next;
      }
    } catch {
      /* fall through to defaults */
    }
    return allVisible;
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
