import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, Play, RefreshCw, Trash2 } from "lucide-react";
import { api } from "../api";
import { toast, useStore, type QueueTrack } from "../store";
import { CACHED_PATHS_KEY, cachedBytes, cachedTracks, clearMediaCache, trackIdentity, uncacheTrack, useCachedPaths } from "../lib/mediaCache";
import { sortRows, SortHeader, toggleSort, type SortState } from "../lib/sort";
import { ColumnResizer, ColumnsMenu, useColumnPrefs, useColumnWidths, type Col } from "../lib/columns";
import { fmtDuration, fmtTech, originalYear } from "../lib/fmt";
import { albumRef } from "../lib/refs";
import type { Album, Track } from "../types";
import { CachedMark, EmptyState } from "./Badges";
import AlbumRow, { type AlbumRowCell } from "./AlbumRow";
import ConfirmButton from "./ConfirmButton";

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

/** Album-level columns of this view only — the library's own table keys stay
 *  untouched, so tuning this table never reflows the library. */
const COLS: Col[] = [
  { id: "album", label: "Album", sortKey: "path" },
  { id: "artist", label: "Artist", sortKey: "artist" },
  { id: "year", label: "Year", sortKey: "year" },
  { id: "cached", label: "Cached", sortKey: "cachedCount" },
];

const COL_W: Record<string, string> = {
  album: "w-auto",
  artist: "w-[22%]",
  year: "w-16",
  cached: "w-20",
};

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
 *  actually holds. */
type CachedAlbum = {
  path: string;
  album: Album;
  tracks: Track[];
  artist: string;
  year: string;
  cachedCount: number;
};

/** Cached tracks are playable offline, so the queue is only ever built from
 *  them — the row click and the album play button share this. */
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

  return (
    <AlbumRow
      title={showAlbumCol ? (row.album.meta?.ALBUM ?? row.path.split("/").pop()) : null}
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
          <table className="w-full">
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

/** What the browser holds for offline playback: the cached files, grouped into
 *  the albums they belong to and rendered as the library's album table. The
 *  bytes live in Cache Storage (lib/mediaCache), not on disk — this is the only
 *  place that shows them, so it is also where they are evicted. */
export default function CachedTracksView() {
  const qc = useQueryClient();
  const [sort, setSort] = useState<SortState | null>(null);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [cols, toggleCol] = useColumnPrefs("cached", COLS);
  const [widths, setWidth, resetWidths] = useColumnWidths("cached");
  const [trackCols, toggleTrackCol] = useColumnPrefs("cached-tracks", TRACK_COLS);
  const [trackW, setTrackW, resetTrackW] = useColumnWidths("cached-tracks");

  const { data: lib } = useQuery({ queryKey: ["library"], queryFn: api.library });
  const { data: tracks, isFetching, refetch } = useQuery({ queryKey: CACHED_PATHS_KEY, queryFn: cachedTracks });
  const { data: total } = useQuery({ queryKey: ["cachedBytes"], queryFn: cachedBytes });

  // A cached track is matched to the library by IDENTITY (its MusicBrainz
  // recording id, else the path): this set holds every cached track's path plus
  // where the library keeps that recording today, so a download is not lost
  // when the organizer moves the file.
  const cached = useCachedPaths();

  const rows = useMemo(() => {
    const knownIds = new Set<string>();
    const knownPaths = new Set<string>();
    const out: CachedAlbum[] = [];
    for (const a of lib?.artists ?? []) {
      for (const al of a.albums ?? []) {
        const tracks = (al.tracks ?? []).filter((t) => cached.has(t.path));
        for (const t of al.tracks ?? []) {
          knownPaths.add(t.path);
          knownIds.add(trackIdentity(t.path, t.tags.MUSICBRAINZ_TRACKID));
        }
        if (!tracks.length) continue;
        out.push({
          path: al.path,
          album: al,
          tracks,
          artist: a.name,
          year: originalYear(al.meta),
          cachedCount: tracks.length,
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
  }, [lib, cached, tracks, sort]);

  const after = () => {
    qc.invalidateQueries({ queryKey: CACHED_PATHS_KEY });
    qc.invalidateQueries({ queryKey: ["cachedBytes"] });
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
  const toggleRow = (path: string) =>
    setOpen((s) => {
      const next = new Set(s);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });

  return (
    <div className="panel">
      <div className="flex items-center justify-between gap-2 flex-wrap mb-2">
        <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500 flex items-center gap-1.5">
          <Download className="h-3.5 w-3.5" /> Cached tracks
          <span className="text-[10px] font-mono normal-case text-zinc-400">
            {count} track{count === 1 ? "" : "s"} · {fmtSize(total ?? 0)}
          </span>
        </div>
        <div className="flex items-center gap-1 flex-wrap">
          <button
            className="btn-ghost !py-1 text-xs min-h-[2rem] md:min-h-0"
            onClick={() => refetch()}
            disabled={isFetching}
            title="Re-read what this browser holds offline"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${isFetching ? "animate-spin" : ""}`} /> Refresh
          </button>
          {count > 0 && (
            <ConfirmButton
              className="btn-danger !py-1 text-xs min-h-[2rem] md:min-h-0"
              confirmLabel="Clear all"
              onConfirm={clearAll}
              title="Delete every cached track from this browser — offline playback stops working until they are downloaded again"
            >
              <Trash2 className="h-3.5 w-3.5" /> Clear all
            </ConfirmButton>
          )}
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
        </div>
      </div>
      <div className="text-[11px] text-zinc-500 mb-2.5">
        Tracks this browser plays without the server — Download caches the audio, the player then serves it from here.
        Removing one frees the space and takes it back to streaming.
      </div>

      {count === 0 ? (
        <EmptyState
          title="Nothing cached yet"
          hint="Download an album, artist or single track and its audio is kept in this browser for offline playback."
        />
      ) : (
        <>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
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
                      onSort={(k) => setSort((s) => toggleSort(s, k))}
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
          {rows.list.length === 0 && (
            <EmptyState
              title="Only tracks the library does not list"
              hint="What is cached here matches no track in the library any more — deleted, or moved outside the music folder. Clear all evicts it."
            />
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
