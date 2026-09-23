import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowDownUp, Download, Play, RefreshCw, Trash2 } from "lucide-react";
import { api } from "../api";
import { toast, useStore, type QueueTrack } from "../store";
import {
  CACHED_PATHS_KEY,
  CACHED_SIZES_KEY,
  cachedEntrySizes,
  cachedTracks,
  clearMediaCache,
  trackIdentity,
  uncacheTrack,
  useCachedPaths,
} from "../lib/mediaCache";
import { sortRows, SortHeader } from "../lib/sort";
import { ColumnResizer, ColumnsMenu, useColumnPrefs, useColumnWidths, type Col } from "../lib/columns";
import { fmtDuration, fmtTech, GRID_SIZE_MIN, originalYear } from "../lib/fmt";
import { albumRef } from "../lib/refs";
import { GRID_SIZES, useGridSize, useLocalSort } from "../lib/libraryView";
import type { Album, Track } from "../types";
import { CachedMark, EmptyState } from "../components/Badges";
import AlbumCard from "../components/AlbumCard";
import AlbumRow, { type AlbumRowCell } from "../components/AlbumRow";
import ConfirmButton from "../components/ConfirmButton";
import PageHeader from "../components/PageHeader";
import Segmented from "../components/Segmented";

/** Human byte size. Local deliberately: the player bar's formatter is tuned for
 *  audio readouts, and a cache total spans multi-GB files. */
function fmtSize(bytes: number): string {
  if (!bytes) return "0 B";
  const units = ["B", "kB", "MB", "GB", "TB"];
  let v = bytes;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v < 10 && i > 0 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

/** The two shapes of the same list: the library's cover grid and its album
 *  table. The library's own five tabs are not offered here — cached downloads
 *  have no artists view, no compact status view and no flat track list. */
type DownloadView = "grid" | "albums";

const VIEW_TABS: { id: DownloadView; label: string }[] = [
  { id: "grid", label: "Grid" },
  { id: "albums", label: "Albums" },
];

/** What this list is worth sorting by. NOT the library's own album sorts: half
 *  of those (grade, audit, videos, label, catalog #) say nothing about what a
 *  browser holds, and the keys here resolve against a row built below — hence
 *  the flat `name`/`cachedCount`/`bytes` beside the payload's own fields.
 *
 *  These keys are the table's column sort keys too: a header click and this
 *  menu have to name the same sort, or the menu would read "Sort" over a
 *  column the table is visibly sorted by. */
const SORTS: { key: string; label: string }[] = [
  { key: "name", label: "Album name" },
  { key: "artist", label: "Artist" },
  { key: "year", label: "Year" },
  { key: "cachedCount", label: "Cached tracks" },
  { key: "bytes", label: "Size" },
];

/** Album-level columns of this view only — the library's own table keys stay
 *  untouched, so tuning this table never reflows the library. */
const COLS: Col[] = [
  { id: "album", label: "Album", sortKey: "name" },
  { id: "artist", label: "Artist", sortKey: "artist" },
  { id: "year", label: "Year", sortKey: "year" },
  { id: "cached", label: "Cached", sortKey: "cachedCount" },
  { id: "size", label: "Size", sortKey: "bytes" },
];

const COL_W: Record<string, string> = {
  album: "w-auto",
  artist: "w-[22%]",
  year: "w-16",
  cached: "w-20",
  size: "w-20",
};

/** Floors, from each table's own columns: the chevron and cover take 96 px and
 *  the Actions header 96 more, so the album list's name (auto, 22% beside it)
 *  and the tracklist's title (auto, 16% beside it) are what the fixed layout
 *  shrinks when the columns do not fit — 770 px and 486 px are where each of
 *  them still has its 200 px (the album row carries one more 80 px column than
 *  the library's own table: the cached size). Both `md:` only: below it the
 *  fold has already left the name the row. */
const CACHE_MIN_W = "md:min-w-[770px]";
const CACHE_TRACK_MIN_W = "md:min-w-[486px]";

/** The tracklist under an expanded album row — its own prefs key for the same
 *  reason. */
const TRACK_COLS: Col[] = [
  { id: "num", label: "#", sortKey: "tracknumber" },
  { id: "title", label: "Title", sortKey: "tags.TITLE" },
  { id: "dur", label: "Dur", sortKey: "tech.length" },
  { id: "bitrate", label: "Bitrate", sortKey: "tech.bitrate" },
];

/** A phone (390 px) row keeps the album or the title and the buttons: the
 *  percentages below leave a few characters at that width. The class has to
 *  sit on the header AND the cells or the fixed-layout grid misaligns; `md` is
 *  where each column comes back. */
const PHONE_HIDE = " hidden md:table-cell";

const TRACK_COL_W: Record<string, string> = {
  num: "w-16",
  title: "w-auto",
  dur: "w-20",
  bitrate: "w-[16%]",
};

/** One album of the cache: the album itself plus the tracks of it the browser
 *  actually holds. `name`/`bytes` are this list's own fields — the row is built
 *  here rather than read out of the library, so the view's sort keys resolve
 *  against it (`COLS`, `SORTS`). */
type CachedAlbum = {
  path: string;
  album: Album;
  tracks: Track[];
  name: string;
  artist: string;
  year: string;
  cachedCount: number;
  /** Bytes of the tracks of it this browser holds. */
  bytes: number;
};

/** Cached tracks are playable offline, so the queue is only ever built from
 *  them — the row click, the album play button and the grid card share this. */
function queueFor(row: CachedAlbum): QueueTrack[] {
  return row.tracks.map((t) => ({
    path: t.path,
    file: t.file,
    albumPath: row.path,
    artist: row.artist,
    album: row.album.meta?.ALBUM ?? undefined,
    title: t.tags.TITLE || undefined,
    coverFile: t.cover_file ?? null,
    albumCover: row.album.cover_file ?? null,
    advisory: t.tags.ITUNESADVISORY ?? null,
  }));
}

function CachedAlbumRow({
  row,
  cols,
  expanded,
  onToggle,
  onPlay,
  onRemove,
  onRemoveTrack,
  trackCols,
  trackWidths,
  onTrackWidth,
  onResetTrackWidths,
}: {
  row: CachedAlbum;
  cols: string[];
  expanded: boolean;
  onToggle: () => void;
  onPlay: (from?: string) => void;
  onRemove: () => void;
  onRemoveTrack: (t: Track) => void;
  trackCols: string[];
  trackWidths: Record<string, number>;
  onTrackWidth: (id: string, px: number) => void;
  onResetTrackWidths: () => void;
}) {
  const showAlbumCol = cols.includes("album");
  const cells: AlbumRowCell[] = [];
  if (cols.includes("artist"))
    cells.push({ id: "artist", cls: `td text-zinc-400 break-words${PHONE_HIDE}`, node: row.artist });
  if (cols.includes("year"))
    cells.push({ id: "year", cls: `td text-zinc-500${PHONE_HIDE}`, node: row.year || "—" });
  if (cols.includes("cached"))
    cells.push({
      id: "cached",
      cls: `td text-zinc-500 tabular-nums${PHONE_HIDE}`,
      title: `${row.cachedCount} of ${row.album.track_count} track(s) of this album are cached`,
      node: `${row.cachedCount} / ${row.album.track_count}`,
    });
  if (cols.includes("size"))
    cells.push({
      id: "size",
      cls: `td text-zinc-500 tabular-nums${PHONE_HIDE}`,
      title: "Audio cached for this album",
      node: fmtSize(row.bytes),
    });

  return (
    <AlbumRow
      title={showAlbumCol ? row.name : null}
      titleHref={albumRef(row.album)}
      coverPath={row.path}
      coverFile={row.album.cover_file}
      coverTitle="Open album page"
      cells={cells}
      actions={
        <>
          {/* Touch shows these always: 32 px targets on a phone, compact from `md`. */}
          <button className="btn-ghost !px-1.5 !py-2 md:!py-1" title="Play these cached tracks" onClick={() => onPlay()}>
            <Play className="h-3.5 w-3.5" />
          </button>
          <button
            className="btn-danger !px-1.5 !py-2 md:!py-1"
            title="Remove this album from the offline cache"
            onClick={onRemove}
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        </>
      }
      onRowClick={onToggle}
      showExpand
      expanded={expanded}
      onToggle={onToggle}
      colSpan={cols.length + 3}
      expandedContent={
        <div className="overflow-x-auto">
          <table className={`w-full ${CACHE_TRACK_MIN_W}`}>
          <thead className="border-b border-border">
            <tr>
              {TRACK_COLS.filter((c) => trackCols.includes(c.id)).map((c) => (
                <th
                  key={c.id}
                  className={`th relative ${TRACK_COL_W[c.id] ?? ""}${c.id === "num" || c.id === "bitrate" ? PHONE_HIDE : ""}`}
                  style={trackWidths[c.id] ? { width: trackWidths[c.id] } : undefined}
                >
                  {c.label}
                  <ColumnResizer
                    width={trackWidths[c.id]}
                    onDrag={(w) => onTrackWidth(c.id, w)}
                    onReset={onResetTrackWidths}
                  />
                </th>
              ))}
              <th className="th w-16 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {row.tracks.map((t) => (
              <tr
                key={t.path}
                className="table-row group cursor-pointer"
                title="Click to play from the cache"
                onClick={() => onPlay(t.path)}
              >
                {trackCols.includes("num") && (
                  <td className={`td text-zinc-600 tabular-nums cell-nowrap${PHONE_HIDE}`}>
                    {t.tracknumber ?? t.tags.TRACKNUMBER ?? "—"}
                  </td>
                )}
                {trackCols.includes("title") && (
                  <td className="td break-words">
                    <div className="flex items-center gap-1.5 min-w-0">
                      <span className="break-words">{t.tags.TITLE ?? t.file}</span>
                      <CachedMark path={t.path} />
                    </div>
                  </td>
                )}
                {trackCols.includes("dur") && <td className="td text-zinc-500">{fmtDuration(t.tech.length)}</td>}
                {trackCols.includes("bitrate") && <td className={`td text-zinc-500${PHONE_HIDE}`}>{fmtTech(t.tech) || "—"}</td>}
                <td className="td text-right">
                  <div
                    className="flex justify-end gap-1 opacity-0 group-hover:opacity-100 [@media(hover:none)]:opacity-100 transition-opacity"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <button className="btn-ghost !px-1.5 !py-1.5 min-h-[2rem] md:min-h-0" title="Play this track" onClick={() => onPlay(t.path)}>
                      <Play className="h-3 w-3" />
                    </button>
                    <button
                      className="btn-danger !px-1.5 !py-1.5 min-h-[2rem] md:min-h-0"
                      title="Remove this track from the offline cache"
                      onClick={() => onRemoveTrack(t)}
                    >
                      <Trash2 className="h-3 w-3" />
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
          </table>
        </div>
      }
    />
  );
}

/** One cached album as the library's own card. The album IS the library's
 *  album, so the card is too; what this view adds is how much of it is here,
 *  what that costs, and the one control that hands the space back. */
function CachedAlbumCard({ row, onPlay, onRemove }: {
  row: CachedAlbum;
  onPlay: () => void;
  onRemove: () => void;
}) {
  return (
    <AlbumCard
      al={row.album}
      artistName={row.artist}
      /* The card's own play button queues the WHOLE album, and the tracks this
         browser did not download are exactly the ones that need the server —
         so the button here queues the cached ones. */
      /* The card places the button in its own band (`AlbumCard`'s overlay is
         one flow column now), so it carries no `absolute left-2 top-9` of its
         own — an offset here would put it back in the chips' way. */
      actions={
        <button
          className="tap-hit btn-primary !rounded-lg !p-3 row-hover transition-opacity shadow-2xl"
          title={`Play the ${row.cachedCount} cached track${row.cachedCount === 1 ? "" : "s"} of this album`}
          aria-label="Play the cached tracks"
          onClick={(e) => {
            e.stopPropagation();
            onPlay();
          }}
        >
          <Play className="h-4 w-4 fill-current" />
        </button>
      }
      extraMeta={
        <>
          <span
            className="chip bg-raise border border-border text-zinc-300 tabular-nums"
            title={`${row.cachedCount} of ${row.album.track_count} track(s) of this album are cached`}
          >
            {row.cachedCount} / {row.album.track_count}
          </span>
          <span className="text-[10px] text-zinc-500 font-mono tabular-nums" title="Audio cached for this album">
            {fmtSize(row.bytes)}
          </span>
          <button
            className="btn-danger !px-1.5 !py-1 ml-auto"
            title="Remove this album from the offline cache"
            onClick={onRemove}
          >
            <Trash2 className="h-3 w-3" />
          </button>
        </>
      }
    />
  );
}

/** The offline downloads: exactly the tracks this browser can play without the
 *  server, grouped into the albums they belong to and laid out as the library
 *  is — the same cover grid over the same album table, with the library's own
 *  controls. The bytes live in Cache Storage (lib/mediaCache), not on disk;
 *  this is the one place that shows them, so it is also where they are evicted.
 *
 *  Downloading happens where the music already is — the album, artist, playlist
 *  and player download buttons — so this page has no download control of its
 *  own. Saving a file to disk is Export's job, and the Soulseek page owns the
 *  staging folder downloads land in; neither is this. */
export default function DownloadsPage() {
  const qc = useQueryClient();
  const [view, setView] = useState<DownloadView>("grid");
  const [sort, setSort] = useLocalSort("downloads");
  const [gridSize, setGridSize] = useGridSize();
  const [sortOpen, setSortOpen] = useState(false);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [cols, toggleCol] = useColumnPrefs("cached", COLS);
  const [widths, setWidth, resetWidths] = useColumnWidths("cached");
  const [trackCols, toggleTrackCol] = useColumnPrefs("cached-tracks", TRACK_COLS);
  const [trackW, setTrackW, resetTrackW] = useColumnWidths("cached-tracks");

  const { data: lib } = useQuery({ queryKey: ["library"], queryFn: () => api.library() });
  const { data: tracks, isFetching, refetch } = useQuery({ queryKey: CACHED_PATHS_KEY, queryFn: cachedTracks });
  const { data: sizes } = useQuery({ queryKey: CACHED_SIZES_KEY, queryFn: cachedEntrySizes });

  // A cached track is matched to the library by IDENTITY (its MusicBrainz
  // recording id, else the path): this set holds every cached track's path plus
  // where the library keeps that recording today, so a download is not lost
  // when the organizer moves the file.
  const cached = useCachedPaths();

  // The bytes are filed under those same identities, so a moved file still
  // contributes to its album's total — with the path it was downloaded from as
  // the fallback, exactly the two keys `useCachedPaths` resolves a track by.
  const bytesOf = useMemo(() => {
    const byIdentity = new Map<string, number>();
    const byPath = new Map<string, number>();
    for (const c of tracks ?? []) {
      const n = sizes?.[c.url] ?? 0;
      byIdentity.set(c.key, n);
      byPath.set(c.path, n);
    }
    return { byIdentity, byPath };
  }, [tracks, sizes]);

  const rows = useMemo(() => {
    const knownIds = new Set<string>();
    const knownPaths = new Set<string>();
    const out: CachedAlbum[] = [];
    for (const a of lib?.artists ?? []) {
      for (const al of a.albums ?? []) {
        const held = (al.tracks ?? []).filter((t) => cached.has(t.path));
        let bytes = 0;
        for (const t of al.tracks ?? []) {
          knownPaths.add(t.path);
          const id = trackIdentity(t.path, t.tags.MUSICBRAINZ_TRACKID);
          knownIds.add(id);
          if (cached.has(t.path)) bytes += bytesOf.byIdentity.get(id) ?? bytesOf.byPath.get(t.path) ?? 0;
        }
        if (!held.length) continue;
        out.push({
          path: al.path,
          album: al,
          tracks: held,
          name: al.meta?.ALBUM ?? al.path.split("/").pop() ?? al.path,
          artist: a.name,
          year: originalYear(al.meta),
          cachedCount: held.length,
          bytes,
        });
      }
    }
    // Cached tracks no library row claims any more — deleted, or moved to a
    // folder the library does not list: nothing to show them on, so they are
    // counted, not hidden. Identity is what decides, not the stored path: a
    // cached file the library still holds under its recording id is not an
    // orphan just because its path changed.
    const orphans = (tracks ?? []).filter((t) => !knownIds.has(t.key) && !knownPaths.has(t.path)).length;
    return { list: sortRows(out, sort), orphans };
  }, [lib, cached, bytesOf, tracks, sort]);

  const total = useMemo(() => Object.values(sizes ?? {}).reduce((n, b) => n + b, 0), [sizes]);

  const after = () => {
    qc.invalidateQueries({ queryKey: CACHED_PATHS_KEY });
    qc.invalidateQueries({ queryKey: CACHED_SIZES_KEY });
  };

  const play = (row: CachedAlbum, from?: string) => {
    const queue = queueFor(row);
    useStore.getState().playNow(queue, from ? Math.max(0, queue.findIndex((t) => t.path === from)) : 0);
  };

  const remove = async (tracks: Track[]) => {
    // A rejected Cache Storage call used to be an unhandled rejection: the row
    // stayed on screen with no explanation. Say what happened instead.
    try {
      // The recording id goes along: after a move the stored path no longer
      // names the bytes, and the identity is what does.
      await Promise.all(
        tracks.map((t) => uncacheTrack({ path: t.path, mbid: t.tags.MUSICBRAINZ_TRACKID }))
      );
      toast(`Removed ${tracks.length} track(s) from the offline cache`);
    } catch (e) {
      toast.error(`Could not remove from the offline cache: ${e instanceof Error ? e.message : e}`);
    }
    after();
  };

  const clearAll = async () => {
    try {
      await clearMediaCache();
      toast("Offline cache cleared");
    } catch (e) {
      toast.error(`Could not clear the offline cache: ${e instanceof Error ? e.message : e}`);
    }
    after();
  };

  const count = tracks?.length ?? 0;
  const albums = rows.list.length;
  const toggleRow = (path: string) =>
    setOpen((s) => {
      const next = new Set(s);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });

  return (
    <div className="p-6 space-y-5 mx-auto max-w-6xl">
      <PageHeader
        icon={Download}
        title="Downloads"
        subtitle="Tracks this browser plays without the server — downloading caches the audio, removing one frees the space and takes it back to streaming."
      >
      {/* toolbar — the library's line-up: view tabs, sort, cover size and
          columns — then Refresh, Clear all and the counts on the right. */}
      <div className="flex items-center gap-2 flex-wrap">
        <Segmented value={view} onChange={setView} options={VIEW_TABS} />

        <div className="relative">
          <button
            className={`btn-ghost !py-1.5 text-xs tap ${sortOpen ? "!text-white !bg-raise" : ""}`}
            onClick={() => setSortOpen(!sortOpen)}
            title="Sort the downloads"
          >
            <ArrowDownUp className="h-3.5 w-3.5" />
            {sort ? `${SORTS.find((s) => s.key === sort.key)?.label ?? "Sort"} ${sort.dir === 1 ? "↑" : "↓"}` : "Sort"}
          </button>
          {sortOpen && (
            <>
              <div className="fixed inset-0 z-30" onClick={() => setSortOpen(false)} />
              <div className="absolute left-0 top-full mt-1 z-40 w-44 rounded-lg border border-border bg-zinc-950 shadow-2xl p-1.5">
                {SORTS.map((s) => (
                  <button
                    key={s.key}
                    onClick={() => {
                      setSort(s.key);
                      setSortOpen(false);
                    }}
                    className={`w-full text-left px-2.5 py-1.5 rounded-md text-xs flex items-center justify-between gap-3 ${
                      sort?.key === s.key ? "bg-raise text-white" : "text-zinc-400 hover:text-white hover:bg-raise"
                    }`}
                  >
                    <span>{s.label}</span>
                    {sort?.key === s.key && <span className="font-mono">{sort.dir === 1 ? "↑" : "↓"}</span>}
                  </button>
                ))}
              </div>
            </>
          )}
        </div>

        {/* Cover size is a grid control — it is shared with the library's own
            grids (lib/libraryView), so one pick sizes every cover in the app. */}
        {view === "grid" && (
          <span title="Cover size">
            <Segmented value={gridSize} onChange={setGridSize} options={GRID_SIZES} />
          </span>
        )}

        {view === "albums" && (
          <ColumnsMenu
            cols={COLS}
            visible={cols}
            onToggle={toggleCol}
            onResetWidths={() => {
              resetWidths();
              resetTrackW();
            }}
            hasCustomWidths={Object.keys(widths).length > 0 || Object.keys(trackW).length > 0}
            extraCols={TRACK_COLS}
            extraVisible={trackCols}
            onExtraToggle={toggleTrackCol}
            extraTitle="Cached tracklist columns"
          />
        )}

        {/* min-w-0 + wrap: the counts grow with the cache, so on a phone this
            group takes its own line instead of pushing the row past the edge. */}
        <div className="ml-auto flex items-center gap-2 flex-wrap min-w-0">
          <button
            className="btn-ghost !py-1.5 text-xs tap"
            onClick={() => refetch()}
            disabled={isFetching}
            title="Re-read what this browser holds offline"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${isFetching ? "animate-spin" : ""}`} /> Refresh
          </button>
          {count > 0 && (
            <ConfirmButton
              className="btn-danger !py-1.5 text-xs tap"
              confirmLabel="Clear all"
              onConfirm={clearAll}
              title="Delete every cached track from this browser — offline playback stops working until they are downloaded again"
            >
              <Trash2 className="h-3.5 w-3.5" /> Clear all
            </ConfirmButton>
          )}
          <span className="text-xs text-zinc-500 whitespace-nowrap">
            {albums} album{albums === 1 ? "" : "s"} · {count} track{count === 1 ? "" : "s"} · {fmtSize(total)}
          </span>
        </div>
      </div>
      </PageHeader>

      {count === 0 ? (
        <EmptyState
          title="Nothing cached yet"
          hint="Download an album, artist or single track and its audio is kept in this browser for offline playback."
        />
      ) : (
        <>
          {albums === 0 ? (
            <EmptyState
              title="Only tracks the library does not list"
              hint="What is cached here matches no track in the library any more — deleted, or moved outside the music folder. Clear all evicts it."
            />
          ) : view === "grid" ? (
            <div
              className="grid gap-x-4 gap-y-5 stagger"
              style={{ gridTemplateColumns: `repeat(auto-fill, minmax(${GRID_SIZE_MIN[gridSize]}px, 1fr))` }}
            >
              {rows.list.map((row) => (
                <CachedAlbumCard
                  key={row.path}
                  row={row}
                  onPlay={() => play(row)}
                  onRemove={() => remove(row.tracks)}
                />
              ))}
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className={`w-full text-sm ${CACHE_MIN_W}`}>
                <thead className="border-b border-border">
                  <tr>
                    <th className="th w-10"></th>
                    <th className="th w-14"></th>
                    {COLS.filter((c) => cols.includes(c.id)).map((c) => (
                      <SortHeader
                        key={c.id}
                        label={c.label}
                        sort={sort}
                        sortKey={c.sortKey}
                        onSort={setSort}
                        className={`relative ${COL_W[c.id] ?? ""}${c.id === "album" ? "" : PHONE_HIDE}`}
                        style={widths[c.id] ? { width: widths[c.id] } : undefined}
                      >
                        <ColumnResizer
                          width={widths[c.id]}
                          onDrag={(w) => setWidth(c.id, w)}
                          onReset={resetWidths}
                        />
                      </SortHeader>
                    ))}
                    <th className="th w-24 text-right">Actions</th>
                  </tr>
                </thead>
                <tbody className="stagger">
                  {rows.list.map((row) => (
                    <CachedAlbumRow
                      key={row.path}
                      row={row}
                      cols={cols}
                      expanded={open.has(row.path)}
                      onToggle={() => toggleRow(row.path)}
                      onPlay={(from) => play(row, from)}
                      onRemove={() => remove(row.tracks)}
                      onRemoveTrack={(t) => remove([t])}
                      trackCols={trackCols}
                      trackWidths={trackW}
                      onTrackWidth={setTrackW}
                      onResetTrackWidths={resetTrackW}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {rows.orphans > 0 && (
            <div className="text-[10px] text-zinc-600 mt-2">
              {rows.orphans} cached track(s) match no track in the library any more — Clear all evicts them too.
            </div>
          )}
        </>
      )}
    </div>
  );
}
