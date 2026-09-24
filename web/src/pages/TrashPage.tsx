import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowDownUp, FolderOpen, ListChecks, Loader2, Search, Trash2, Undo2 } from "lucide-react";
import { api, type TrashEntry } from "../api";
import { toast } from "../store";
import { invalidateLibrary } from "../lib/invalidate";
import { sortRows, SortHeader, toggleSort, type SortState } from "../lib/sort.tsx";
import { ColumnsMenu, useColumnPrefs, type Col } from "../lib/columns";
import { GRID_SIZE_MIN } from "../lib/fmt";
import { EmptyState, PageLoading } from "../components/Badges";
import Segmented from "../components/Segmented";
import PageHeader from "../components/PageHeader";
import Popover, { MenuItem } from "../components/Popover";
import AlbumCard from "../components/AlbumCard";
import AlbumRow, { type AlbumRowCell } from "../components/AlbumRow";
import type { Album } from "../types";

/** The two presentations the bin needs — the library's Segmented control with
 *  the library's own ids (Artists/Tracks/Compact have no meaning here). */
type View = "grid" | "albums";

const VIEW_TABS: { id: View; label: string }[] = [
  { id: "grid", label: "Grid" },
  { id: "albums", label: "Albums" },
];

/** The columns that make sense to order the trash by — the same four the
 *  Sort control offers, and the keys `rowValue` resolves on a trashed entry. */
const SORTS = [
  { key: "label", label: "Name" },
  { key: "tracks", label: "Tracks" },
  { key: "bytes", label: "Size" },
  { key: "trashed_at", label: "Removed" },
];

/** The trash listing carries the files inside each entry (capped at 200 —
 *  `file_count` is the true total) on top of what api.ts declares. */
type TrashFile = { name: string; rel: string; bytes: number };
type Entry = TrashEntry & { files?: TrashFile[]; file_count?: number };

/** Trash columns for the shared ColumnsMenu — the library's Col shape, so the
 *  chooser and its persisted "which columns are visible" prefs behave the
 *  same. Kind has no header sort affordance: it is not in the Sort list. */
const TRASH_COLS: Col[] = [
  { id: "kind", label: "Kind", sortKey: "kind" },
  { id: "tracks", label: "Tracks", sortKey: "tracks" },
  { id: "size", label: "Size", sortKey: "bytes" },
  { id: "removed", label: "Removed", sortKey: "trashed_at" },
];

/** Fixed widths per column — the global `table { table-layout: fixed }` clips
 *  a column without one; the item column absorbs what is left over. */
const TRASH_COL_W: Record<string, string> = {
  kind: "w-24",
  tracks: "w-24",
  size: "w-28",
  removed: "w-32",
};

/** The trash list's floor, from its own columns: the chevron and cover take
 *  96 px, the four summary columns 432 px and the Actions header 240 px from
 *  `md` up — 768 px of fixed width that the item name has to fit behind.
 *  Without a floor the fixed layout takes the deficit out of that name (it is
 *  the only column without a width), so 968 px is where the name keeps its
 *  200 px and the wrapper scrolls below that. `md:` because below it the
 *  summary columns have folded and the name shares the row with the buttons. */
const TRASH_MIN_W = "md:min-w-[968px]";

/** A phone (390 px) row keeps the item and its two buttons: the summary
 *  columns are added up in the toolbar anyway, and keeping them squeezes the
 *  item name to nothing. The class has to sit on the header AND the cells, or
 *  the fixed-layout grid misaligns; `md` is where they come back. */
const PHONE_HIDE = " hidden md:table-cell";

/** Root-level art names GET /api/cover auto-detects. CoverImg only ever asks
 *  for a named file (no name = placeholder box), so the client names the file
 *  the server would have found — the same search, on the entry's own listing. */
const COVER_NAMES = ["cover.jpg", "cover.jpeg", "cover.png", "cover.jxl", "cover.webp", "cover.bmp"];

function coverOf(e: Entry): string | null {
  if (!e.cover) return null;
  return (
    (e.files ?? []).find((f) => !f.rel.includes("/") && COVER_NAMES.includes(f.rel.toLowerCase()))
      ?.rel ?? null
  );
}

/** Bytes → "12.4 MB" (same buckets as the Soulseek wish list). */
function fmtSize(n: number): string {
  if (!n) return "0 B";
  if (n > 1024 ** 3) return `${(n / 1024 ** 3).toFixed(2)} GB`;
  if (n > 1024 ** 2) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(n / 1024))} kB`;
}

/** ISO-8601 → "3d ago". */
function ago(iso: string): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "—";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

/** AlbumCard reads a handful of album fields; a trashed folder has no tags
 *  and no grade of its own, so everything it does not read stays neutral. */
function cardAlbum(e: Entry): Album & { artist?: string } {
  return {
    path: e.path,
    meta: { ALBUM: e.label },
    cover_file: coverOf(e),
    tracks: [],
    grade_pct: null,
    pass: true,
    pass_count: 0,
    total_checks: 0,
    track_count: e.tracks ?? 0,
    audit_summary: null,
    has_log: false,
    has_cue: false,
    checksum_status: "",
    accuraterip_status: "",
    lyrics_present: 0,
    lyrics_expected: 0,
    instrumental_count: 0,
    media: "",
    source_summary: null,
  };
}

/** The files inside one trashed entry — the bin's mirror of the library's
 *  album → tracklist expansion, in the library's nested-table markup. */
function FilesTable({ e }: { e: Entry }) {
  const files = useMemo(
    () => [...(e.files ?? [])].sort((a, b) => a.rel.localeCompare(b.rel)),
    [e.files]
  );
  const total = e.file_count ?? files.length;
  return (
    /* Its own scroll wrapper: two columns leave the name a positive leftover,
       so nothing collapses in here today — but a table with no wrapper can
       never scroll its way out of one, and the shell clips sideways overflow
       rather than scrolling it. */
    <div className="overflow-x-auto">
      {total > files.length && (
        <div className="px-3 py-1.5 text-[11px] text-zinc-500">
          showing {files.length} of {total} files
        </div>
      )}
      {/* Name is the only column without a width, so it takes the deficit; the
          floor keeps it at 200 px next to the 112 px size column. No fold here,
          so unlike the list above this one applies at every width. */}
      <table className="w-full min-w-[312px]">
        <thead className="border-b border-border">
          <tr>
            <th className="th w-auto">Name</th>
            <th className="th w-28 text-right">Size</th>
          </tr>
        </thead>
        <tbody>
          {files.map((f) => (
            <tr key={f.rel}>
              <td className="td font-mono text-xs break-words">{f.rel}</td>
              <td className="td text-zinc-500 tabular-nums text-right">{fmtSize(f.bytes)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Sidebar "Trash" — what "Remove from library" moved into <music folder>/
 *  .mlo/trash. The library scan skips that folder, so this page is the only
 *  way back to a removed album: put it back where it came from, or erase it
 *  from disk for good. Rendered through the library's own pieces (Segmented,
 *  Sort, ColumnsMenu, AlbumRow, AlbumCard) so the bin reads like the viewer
 *  it undoes — only the actions differ. */
export default function TrashPage() {
  const qc = useQueryClient();
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["trash"],
    queryFn: api.trash,
  });

  const [view, setView] = useState<View>("grid");
  const [filter, setFilter] = useState("");
  const [sort, setSort] = useState<SortState | null>(null);
  const [sortOpen, setSortOpen] = useState(false);
  // checkboxes (and the batch toolbar they feed) only exist in select mode
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [expanded, setExpanded] = useState<string[]>([]);
  const [cols, toggleCol] = useColumnPrefs("trash", TRASH_COLS);

  const del = useMutation({
    mutationFn: (names: string[]) => api.trashDelete(names),
    onSuccess: (r) => {
      const failed = r.failed ?? [];
      if (failed.length) {
        toast(
          `${failed.length} item(s) could not be deleted — ${failed
            .map((f) => `${f.name}: ${f.error}`)
            .join("; ")}`
        );
      } else {
        toast.success(`Deleted ${r.deleted.length} item(s), freed ${fmtSize(r.freed)}`);
      }
    },
    onError: (e) => toast.error(String(e)),
    // A partial failure still changes the disk — never leave a stale list.
    onSettled: () => {
      setSelected([]);
      qc.invalidateQueries({ queryKey: ["trash"] });
    },
  });

  const restore = useMutation({
    mutationFn: ({ names, dest }: { names: string[]; dest: string | null }) =>
      api.trashRestore(names, dest),
    onSuccess: (r) => {
      const done = r.restored ?? [];
      if (done.length === 1) {
        toast.success(`Restored "${done[0].name}" to ${done[0].to}`);
      } else if (done.length) {
        toast.success(`Restored ${done.length} item(s) — ${done.map((d) => `"${d.name}" → ${d.to}`).join("; ")}`);
      }
      const failed = r.failed ?? [];
      if (failed.length) {
        toast(
          `${failed.length} item(s) could not be restored — ${failed
            .map((f) => `${f.name}: ${f.error}`)
            .join("; ")}`
        );
      }
    },
    onError: (e) => toast.error(String(e)),
    // The library gained (or lost) albums — refresh both sides of the move,
    // even after a partial failure.
    onSettled: () => {
      setSelected([]);
      qc.invalidateQueries({ queryKey: ["trash"] });
      invalidateLibrary(qc);
    },
  });

  // Stable array identity: the memo below must not re-run on every render
  // just because `data?.entries ?? []` allocates a fresh fallback.
  const entries = useMemo(() => (data?.entries ?? []) as Entry[], [data]);
  const busy = del.isPending || restore.isPending;
  const totalBytes = data?.bytes ?? 0;
  const folder = (data?.folder ?? "").replace(/\\/g, "/");
  const musicFolder = (data?.music_folder ?? "").replace(/\\/g, "/");

  // The filter narrows what both views show (label and the raw folder name);
  // the sort then orders exactly that set — one list, two presentations.
  const terms = filter.trim().toLowerCase();
  const rows = useMemo(
    () =>
      sortRows(
        terms
          ? entries.filter(
              (e) => e.label.toLowerCase().includes(terms) || e.name.toLowerCase().includes(terms)
            )
          : entries,
        sort
      ),
    [entries, terms, sort]
  );

  const allSelected = rows.length > 0 && rows.every((e) => selected.includes(e.name));
  const colSpan = 3 + cols.length + (selectMode ? 1 : 0); // checkbox?, chevron+cover, item, cols, actions

  const toggle = (name: string) =>
    setSelected((cur) => (cur.includes(name) ? cur.filter((n) => n !== name) : [...cur, name]));

  const toggleExpand = (name: string) =>
    setExpanded((cur) => (cur.includes(name) ? cur.filter((n) => n !== name) : [...cur, name]));

  const toggleSelectMode = () => {
    setSelectMode((v) => {
      if (v) setSelected([]);
      return !v;
    });
    setSortOpen(false);
  };

  const copyFolder = async () => {
    if (!data?.folder) return;
    try {
      await navigator.clipboard.writeText(data.folder);
      toast("Trash folder path copied");
    } catch {
      toast(data.folder);
    }
  };

  /** A trashed entry whose origin was never recorded can still be restored —
   *  the server just needs a folder inside the music folder to put it in, and
   *  an artist folder is what the library scan expects to find an album in. */
  const askDest = (label: string): string | null => {
    const v = window.prompt(
      `Restore "${label}" to which folder?\nIts original location was not recorded — pick the folder it should live in (an existing artist folder works).`,
      musicFolder ? `${musicFolder}/Artists` : ""
    );
    const dest = (v ?? "").trim();
    return dest || null;
  };

  const restoreOne = (e: Entry) => {
    if (busy) return;
    const dest = e.origin ? null : askDest(e.label);
    if (!e.origin && !dest) return; // cancelled or empty — nothing to do
    restore.mutate({ names: [e.name], dest });
  };

  const restoreSelected = () => {
    if (busy || !selected.length) return;
    // Entries with an origin restore to their own path (the server decides);
    // ask once for the missing ones, then let the server sort them out.
    const orphans = entries.filter((e) => selected.includes(e.name) && !e.origin);
    let dest: string | null = null;
    if (orphans.length) {
      dest = askDest(orphans.length === 1 ? orphans[0].label : `${orphans.length} entries`);
      if (!dest) return;
    }
    restore.mutate({ names: selected, dest });
  };

  const removeOne = (e: Entry) => {
    if (
      !window.confirm(
        `Permanently delete "${e.label}"?\n\nThis CANNOT be undone — the files are erased from disk and free ${fmtSize(e.bytes)}.`
      )
    )
      return;
    del.mutate([e.name]);
  };

  const emptyAll = () => {
    if (
      !window.confirm(
        `Permanently delete all ${entries.length} item(s) in the trash (${fmtSize(totalBytes)})?\n\nThis CANNOT be undone — every file is erased from disk.`
      )
    )
      return;
    del.mutate(entries.map((e) => e.name));
  };

  const removeSelected = () => {
    if (busy || !selected.length) return;
    const bytes = entries.filter((e) => selected.includes(e.name)).reduce((n, e) => n + e.bytes, 0);
    if (
      !window.confirm(
        `Permanently delete ${selected.length} selected item(s) (${fmtSize(bytes)})?\n\nThis CANNOT be undone — the files are erased from disk.`
      )
    )
      return;
    del.mutate(selected);
  };

  /** Restore (or "Restore to…" when the origin is unknown) + permanent
   *  delete — the library's row actions, trash-sized. */
  const rowActions = (e: Entry) => (
    <>
      <button
        className="btn-ghost !py-2 !px-2 text-xs md:!py-1 tap"
        onClick={() => restoreOne(e)}
        disabled={busy}
        aria-label={e.origin ? "Restore" : "Restore to…"}
        title={
          e.origin
            ? `Restore to ${e.origin}`
            : "Original location unknown — choose the folder to restore into"
        }
      >
        <Undo2 className="h-3 w-3" />
        <span className="hidden md:inline">{e.origin ? "Restore" : "Restore to…"}</span>
      </button>
      <button
        className="btn-danger !py-2 !px-2 text-xs md:!py-1 tap"
        onClick={() => removeOne(e)}
        disabled={busy}
        aria-label="Delete permanently"
      >
        <Trash2 className="h-3 w-3" />
        <span className="hidden md:inline">Delete permanently</span>
      </button>
    </>
  );

  /** Card actions replace the library's play button (a trashed album cannot
   *  be played) and sit exactly where it did. */
  const cardActions = (e: Entry) => (
    <div className="absolute left-2 top-9 flex gap-1 row-hover transition-opacity">
      <button
        className="btn-primary !rounded-lg !p-2.5 tap-hit"
        onClick={(ev) => {
          ev.stopPropagation();
          restoreOne(e);
        }}
        disabled={busy}
        title={e.origin ? `Restore to ${e.origin}` : "Original location unknown — choose the folder to restore into"}
      >
        <Undo2 className="h-4 w-4" />
      </button>
      <button
        className="btn-danger !rounded-lg !p-2.5 tap-hit"
        onClick={(ev) => {
          ev.stopPropagation();
          removeOne(e);
        }}
        disabled={busy}
        title="Delete permanently — cannot be undone"
      >
        <Trash2 className="h-4 w-4" />
      </button>
    </div>
  );

  const cellsFor = (e: Entry): AlbumRowCell[] => {
    const cells: AlbumRowCell[] = [];
    if (cols.includes("kind"))
      cells.push({
        id: "kind",
        cls: `td${PHONE_HIDE}`,
        node: <span className="chip bg-raise border border-border text-zinc-400">{e.kind}</span>,
      });
    if (cols.includes("tracks"))
      cells.push({
        id: "tracks",
        cls: `td text-zinc-500 tabular-nums${PHONE_HIDE}`,
        node: e.kind === "album" ? `${e.tracks ?? 0} track${e.tracks === 1 ? "" : "s"}` : "—",
      });
    if (cols.includes("size"))
      cells.push({ id: "size", cls: `td text-zinc-500 tabular-nums${PHONE_HIDE}`, node: fmtSize(e.bytes) });
    if (cols.includes("removed"))
      cells.push({
        id: "removed",
        cls: `td text-zinc-500${PHONE_HIDE}`,
        title: e.trashed_at,
        node: `trashed ${ago(e.trashed_at)}`,
      });
    return cells;
  };

  return (
    <div className="p-6 space-y-5 mx-auto max-w-6xl">
      <PageHeader icon={Trash2} title="Trash">
      {/* toolbar — the library's line-up: view tabs, sort, columns, filter —
          then Empty trash, the trash folder, select mode and the counts on
          the right, all on ONE line. */}
      <div className="flex items-center gap-2 flex-wrap">
        <Segmented value={view} onChange={setView} options={VIEW_TABS} />

        <div className="relative">
          <button
            className={`btn-ghost !py-1.5 text-xs tap ${sortOpen ? "!text-white !bg-raise" : ""}`}
            onClick={() => setSortOpen(!sortOpen)}
            title="Sort the trash"
            aria-haspopup="menu"
            aria-expanded={sortOpen}
          >
            <ArrowDownUp className="h-3.5 w-3.5" />
            {sort
              ? `${SORTS.find((s) => s.key === sort.key)?.label ?? "Sort"} ${sort.dir === 1 ? "↑" : "↓"}`
              : "Sort"}
          </button>
          {/* Same Popover idiom as the downloads page's sort list (and every
              other menu): shield, Escape, click-outside and `role="menu"` from
              the primitive, not re-implemented per page. */}
          <Popover open={sortOpen} onClose={() => setSortOpen(false)} align="left" panelClass="w-44 p-1.5">
            {SORTS.map((s) => (
              <MenuItem
                key={s.key}
                active={sort?.key === s.key}
                label={sort?.key === s.key ? `${s.label} ${sort.dir === 1 ? "↑" : "↓"}` : s.label}
                onClick={() => {
                  setSort(toggleSort(sort, s.key));
                  setSortOpen(false);
                }}
              />
            ))}
          </Popover>
        </div>

        {view === "albums" && <ColumnsMenu cols={TRASH_COLS} visible={cols} onToggle={toggleCol} />}

        {/* quick filter — the album name, or the raw folder name */}
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-zinc-600" />
          <input
            className="input !py-1.5 !pl-8 text-xs !w-52 tap"
            placeholder="Filter the trash"
            title="Filter by album name or by the raw folder name"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        </div>

        {/* min-w-0 + wrap: on a phone this group is wider than the row, so it
            takes its own line and the folder path truncates instead of pushing
            the row past the viewport. */}
        <div className="ml-auto flex items-center gap-2 flex-wrap min-w-0">
          {entries.length > 0 && (
            <button className="btn-danger !py-1.5 text-xs tap" onClick={emptyAll} disabled={busy}>
              {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Trash2 className="h-3 w-3" />}
              Empty trash
            </button>
          )}
          {folder && (
            <button
              className="btn-ghost !py-1.5 text-xs font-mono max-w-full sm:max-w-[24rem] tap min-w-0"
              onClick={copyFolder}
              title="Copy the trash folder path"
            >
              <FolderOpen className="h-3.5 w-3.5 shrink-0" />
              {/* The ellipsis has to live on a child: text overflow does not
                  apply to a flex container, so a `truncate` on the button
                  itself clips both ends of the path with no ellipsis. */}
              <span className="truncate min-w-0">{folder}</span>
            </button>
          )}
          <button
            className={`btn-ghost !py-1.5 text-xs tap ${selectMode ? "!text-accent !border-accent/50" : ""}`}
            onClick={toggleSelectMode}
            title="Select mode — show checkboxes for batch actions"
          >
            <ListChecks className="h-3.5 w-3.5" /> Select
          </button>
          <span className="text-xs text-zinc-500 whitespace-nowrap">
            {isLoading
              ? "Reading trash…"
              : `${entries.length} ${entries.length === 1 ? "entry" : "entries"} · ${fmtSize(totalBytes)}`}
          </span>
        </div>
      </div>
      </PageHeader>

      {/* selection toolbar */}
      {selected.length > 0 && (
        <div className="flex items-center gap-2 bg-accent/15 border border-accent/40 rounded-lg px-3 py-2 flex-wrap">
          <span className="text-xs font-medium text-accent-soft">
            {selected.length} selected
          </span>
          <div className="ml-auto flex gap-1.5 flex-wrap">
            <button
              className="btn-primary !py-1 text-xs tap"
              onClick={restoreSelected}
              disabled={busy}
              title="Put the selected items back into the library"
            >
              {restore.isPending ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Undo2 className="h-3.5 w-3.5" />
              )}
              Restore
            </button>
            <button
              className="btn-danger !py-1 text-xs tap"
              onClick={removeSelected}
              disabled={busy}
              title="Erase the selected items from disk — cannot be undone"
            >
              <Trash2 className="h-3.5 w-3.5" /> Delete permanently
            </button>
            <button className="btn-ghost !py-1 text-xs tap" onClick={() => setSelected([])}>
              Clear
            </button>
          </div>
        </div>
      )}

      {isLoading ? (
        <PageLoading label="Reading trash…" />
      ) : isError ? (
        <EmptyState
          title="Could not read the trash"
          hint={`${String(error)} — is the backend running?`}
        />
      ) : entries.length === 0 ? (
        <EmptyState
          title="Trash is empty"
          hint={`Nothing has been removed from the library. Removed albums would live in ${folder || ".mlo/trash"}.`}
        />
      ) : rows.length === 0 ? (
        <EmptyState title="Nothing matches" hint={`No trashed entry matches "${filter.trim()}".`} />
      ) : view === "grid" ? (
        /* ---------------- Grid ---------------- */
        <div
          className="grid gap-x-4 gap-y-5 stagger"
          style={{ gridTemplateColumns: `repeat(auto-fill, minmax(${GRID_SIZE_MIN.m}px, 1fr))` }}
        >
          {rows.map((e) => {
            const files = e.file_count ?? e.tracks ?? 0;
            return (
              <AlbumCard
                key={e.name}
                al={cardAlbum(e)}
                href={null}
                artistName={`${files} file${files === 1 ? "" : "s"} · ${fmtSize(e.bytes)} · trashed ${ago(e.trashed_at)}`}
                selectable={selectMode}
                selected={selected.includes(e.name)}
                onSelect={() => toggle(e.name)}
                actions={cardActions(e)}
              />
            );
          })}
        </div>
      ) : (
        /* ---------------- Albums ---------------- */
        <div className="overflow-x-auto">
          <table className={`w-full text-sm ${TRASH_MIN_W}`}>
            <thead className="border-b border-border">
              <tr>
                {selectMode && (
                  <th className="th w-8">
                    <input
                      type="checkbox"
                      checked={allSelected}
                      onChange={() => setSelected(allSelected ? [] : rows.map((e) => e.name))}
                    />
                  </th>
                )}
                <th className="th w-10"></th>
                <th className="th w-14"></th>
                <SortHeader
                  label="Item"
                  sort={sort}
                  sortKey="label"
                  onSort={(k) => setSort(toggleSort(sort, k))}
                  className="w-auto"
                />
                {TRASH_COLS.filter((c) => cols.includes(c.id)).map((c) =>
                  c.id === "kind" ? (
                    <th key={c.id} className={`th ${TRASH_COL_W[c.id]}${PHONE_HIDE}`}>
                      {c.label}
                    </th>
                  ) : (
                    <SortHeader
                      key={c.id}
                      label={c.label}
                      sort={sort}
                      sortKey={c.sortKey}
                      onSort={(k) => setSort(toggleSort(sort, k))}
                      className={`${TRASH_COL_W[c.id]}${PHONE_HIDE}`}
                    />
                  )
                )}
                {/* 240 px of labels only fit from `md` up; below that the two
                    buttons keep their icons and their aria-labels. */}
                <th className="th w-24 md:w-[15rem] text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="stagger">
              {rows.map((e) => (
                <AlbumRow
                  key={e.name}
                  title={e.label}
                  titleExtra={
                    /* The raw folder name is a hover/provenance detail: on a
                       phone it would take the whole cell and leave the item
                       name invisible. */
                    <span
                      className="hidden md:inline-block text-[11px] text-zinc-600 font-mono truncate max-w-[10rem] shrink-0"
                      title={e.path}
                    >
                      {e.name}
                    </span>
                  }
                  coverPath={e.path}
                  coverFile={coverOf(e)}
                  coverTitle={e.name}
                  cells={cellsFor(e)}
                  actions={rowActions(e)}
                  showExpand
                  expanded={expanded.includes(e.name)}
                  onToggle={() => toggleExpand(e.name)}
                  onRowClick={() => toggleExpand(e.name)}
                  selected={selected.includes(e.name)}
                  selectMode={selectMode}
                  onToggleSel={() => toggle(e.name)}
                  colSpan={colSpan}
                  expandedContent={<FilesTable e={e} />}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
