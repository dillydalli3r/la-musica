import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { FileVideo, HardDriveDownload, Library, Search } from "lucide-react";
import { api } from "../api";
import CoverImg, { TrackCover } from "../components/CoverImg";
import { fmtDateCell, fmtDuration, fmtTech } from "../lib/fmt";
import Segmented from "../components/Segmented";
import PageHeader from "../components/PageHeader";
import { EmptyState, MediaChip } from "../components/Badges";
import { ExportOptionsPanel, useExportOptions } from "../components/ExportDialog";
import TrackTitleCell from "../components/TrackTitleCell";
import StarRating from "../components/StarRating";
import { ratingOf, useRatings, useSetRating } from "../lib/ratings";
import {
  TABLE_FIT, TRACK_COLS, TRACK_COL_W, TRACK_PHONE_CLS, PHONE_HIDE, phoneHide, type Col,
} from "../lib/columns";
import type { Artist, Track } from "../types";

const SOURCE_KINDS = [
  { id: "playlist", label: "Playlist" },
  { id: "albums", label: "Albums" },
  { id: "artists", label: "Artists" },
  { id: "tracks", label: "Tracks" },
  { id: "library", label: "Entire library" },
] as const;
type SourceKind = (typeof SOURCE_KINDS)[number]["id"];

/** The tabs where the user TICKS rows. The other two are a whole playlist and
 *  the whole library: their selection is the entity itself, so they have
 *  nothing to tick and nothing to select-all. */
const PICK_KINDS = ["albums", "artists", "tracks"] as const;
type PickKind = (typeof PICK_KINDS)[number];
type ListTab = PickKind | "playlist";

/** The plural noun each picker tab counts by, so its counts read as sentences
 *  ("12 of 34 albums match") instead of "12 of 34". */
const NOUN: Record<PickKind, string> = { albums: "albums", artists: "artists", tracks: "tracks" };

/** How many preview rows are drawn before the "+N more…" note. A whole-library
 *  export is tens of thousands of rows, and the preview's job is to show what
 *  the selection RESOLVES TO — not to list it exhaustively. */
const PREVIEW_ROWS = 200;

/** The preview's columns: the library's track table, minus the opt-in credit
 *  columns — the same set the library's own column prefs show by default.
 *  Rating is not a column in either table: it rides in the title cell's fixed
 *  trailing slot (`TrackTitleCell`), which is what keeps it at one x per row,
 *  and the same `TRACK_COL_W` floors put both tables on the same grid. */
const PREVIEW_COLS = TRACK_COLS.filter((c) => !c.defHidden);

/** The picker tables' columns: what each tab lists, in render order. `sel` and
 *  `cover` are labelled through `sr-only` headers — a checkbox and a thumbnail
 *  have no header text of their own. */
const PICK_COLS: Record<ListTab, Col[]> = {
  playlist: [
    { id: "cover", label: "Cover", sortKey: "" },
    { id: "name", label: "Title", sortKey: "" },
    { id: "artist", label: "Artist", sortKey: "" },
    { id: "album", label: "Album", sortKey: "" },
    { id: "dur", label: "Dur", sortKey: "" },
  ],
  albums: [
    { id: "sel", label: "Select", sortKey: "" },
    { id: "cover", label: "Cover", sortKey: "" },
    { id: "name", label: "Album", sortKey: "" },
    { id: "artist", label: "Artist", sortKey: "" },
    { id: "count", label: "Tracks", sortKey: "" },
  ],
  artists: [
    { id: "sel", label: "Select", sortKey: "" },
    { id: "name", label: "Artist", sortKey: "" },
    { id: "count", label: "Library", sortKey: "" },
  ],
  tracks: [
    { id: "sel", label: "Select", sortKey: "" },
    { id: "cover", label: "Cover", sortKey: "" },
    { id: "name", label: "Title", sortKey: "" },
    { id: "artist", label: "Artist", sortKey: "" },
    { id: "album", label: "Album", sortKey: "" },
    { id: "dur", label: "Dur", sortKey: "" },
  ],
};

/** One floor per picker column, same rule as the library's own column maps:
 *  these are the table's floor, so a window too narrow for them scrolls the
 *  wrapper instead of wrapping a cell one character per line. The checkbox is
 *  32 px — the library's own select column — which leaves the name the rest. */
const PICK_COL_W: Record<string, string> = {
  sel: "w-8",
  cover: "w-[52px]",
  name: "md:w-[220px]",
  artist: "w-[108px]",
  album: "w-[112px]",
  count: "w-[88px]",
  dur: "w-20",
};

/** A phone keeps the checkbox and the name: the columns beside them fold at
 *  `md`, exactly as they do in the library's tables. */
const PICK_PHONE_CLS: Record<string, string> = {
  artist: PHONE_HIDE, album: PHONE_HIDE, count: PHONE_HIDE, dur: PHONE_HIDE,
};

/** What a picker cell carries beyond `td` — the classes the library gives the
 *  same kind of value: figures tabular, text wrapping rather than clipping. */
const PICK_CELL_CLS: Record<string, string> = {
  name: "break-words min-w-0",
  artist: "text-zinc-400 break-words",
  album: "text-zinc-500 break-words",
  count: "text-zinc-500 tabular-nums",
  dur: "text-zinc-500 tabular-nums",
};

/** One flat row per library track: every field the source lists and the
 *  preview read, gathered once per payload. `hay` is the lowercase blob the
 *  filter box tests, so a keystroke costs one substring check per row instead
 *  of a join and a lowercase per row. */
interface TrackRow {
  path: string;
  file: string;
  title: string;
  artist: string;
  album: string;
  albumPath: string;
  num: string;
  tech: Track["tech"];
  tags: Track["tags"];
  isVideo: boolean;
  coverFile: string | null;
  albumCover: string | null;
  hay: string;
}

/** A source list, rendered the way the app's tables are: column floors on the
 *  header row (a fixed layout reads its widths from there), `cell-cover` covers
 *  that use the cell's padding, tabular figures, and the library's own phone
 *  folds — so a picker row and a library row read as the same row.
 *
 *  `emptyNote` is the tab's own answer to "why is this list empty": a filter
 *  that matches nothing and a library that holds nothing are different
 *  sentences, and the tab says which one it is. */
function PickTable({ cols, rows, emptyNote }: {
  cols: Col[];
  rows: {
    key: string;
    checked?: boolean;
    onToggle?: () => void;
    cover?: ReactNode;
    cells: Record<string, ReactNode>;
  }[];
  emptyNote: ReactNode;
}) {
  if (!rows.length) return <>{emptyNote}</>;
  return (
    <div className="max-h-72 overflow-auto border border-border rounded-md table-scroll">
      <table className={`${TABLE_FIT} text-xs`}>
        <thead className="border-b border-border sticky top-0 z-10 bg-card">
          <tr>
            {cols.map((c) => (
              <th key={c.id} className={`th !py-1 ${PICK_COL_W[c.id] ?? ""}${phoneHide(PICK_PHONE_CLS, c.id)}`}>
                {c.id === "sel" || c.id === "cover" ? <span className="sr-only">{c.label}</span> : c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="stagger">
          {rows.map((r) => (
            <tr
              key={r.key}
              className={`table-row ${r.onToggle ? "cursor-pointer" : ""} ${r.checked ? "bg-accent/10" : ""}`}
              title={r.onToggle ? "Click to select" : undefined}
              onClick={r.onToggle}
            >
              {cols.map((c) => {
                if (c.id === "sel")
                  return (
                    <td key={c.id} className="td pr-0" onClick={(ev) => ev.stopPropagation()}>
                      <input type="checkbox" checked={!!r.checked} onChange={r.onToggle} />
                    </td>
                  );
                if (c.id === "cover") return <td key={c.id} className="td cell-cover pr-0">{r.cover}</td>;
                return (
                  <td key={c.id} className={`td ${PICK_CELL_CLS[c.id] ?? ""}${phoneHide(PICK_PHONE_CLS, c.id)}`}>
                    {r.cells[c.id] ?? "—"}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Export any slice of the library — playlists, albums, artists, single
 * tracks or everything — to a target drive with a codec / bitrate
 * configurator and folder-structure choices. The "put music on my MP3
 * player" feature.
 *
 * This page owns the SOURCE half (what to export); the destination, format
 * and tag options are the shared ExportOptionsPanel, so the per-page Export
 * dialog offers exactly the same surface.
 *
 * Every tab answers the same three questions in the same order: what it lists
 * (its counts), what is selected, and what the export therefore is. The
 * preview at the bottom is that third answer — the selection RESOLVED — drawn
 * with the library's own track table, because a selection you cannot read as a
 * table is a selection you cannot check. */
export default function ExportPage() {
  const { data: lib } = useQuery({ queryKey: ["library"], queryFn: api.library });
  const { data: playlists } = useQuery({ queryKey: ["playlists"], queryFn: api.playlists });

  const [sourceKind, setSourceKind] = useState<SourceKind>("playlist");
  const [playlistId, setPlaylistId] = useState<number | null>(null);
  const [albumPaths, setAlbumPaths] = useState<Set<string>>(new Set());
  const [artistPaths, setArtistPaths] = useState<Set<string>>(new Set());
  const [trackPaths, setTrackPaths] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState("");

  const artists = useMemo<Artist[]>(() => lib?.artists ?? [], [lib]);
  const albums = useMemo(() => artists.flatMap((a) => a.albums ?? []), [artists]);

  // The preview's rating is the library's: same scope, same half-star scale,
  // the same store the Tracks view writes. One GET per scope, from cache.
  const { data: ratingsData } = useRatings();
  const { setRating, pending } = useSetRating();
  const ratings = ratingsData?.ratings;

  // One flat track metadata table for every source kind / the preview.
  const trackRows = useMemo<TrackRow[]>(() => {
    const rows: TrackRow[] = [];
    for (const a of lib?.artists ?? [])
      for (const al of a.albums)
        for (const t of al.tracks ?? []) {
          const title = t.tags.TITLE ?? t.file;
          const artist = al.album_artist || a.name || "";
          const album = al.meta?.ALBUM ?? al.path.split(/[\\/]/).pop() ?? "";
          rows.push({
            path: t.path,
            file: t.file,
            title,
            artist,
            album,
            albumPath: al.path,
            num: String(t.tracknumber ?? t.tags.TRACKNUMBER ?? ""),
            tech: t.tech,
            tags: t.tags,
            isVideo: !!t.is_video,
            coverFile: t.cover_file ?? null,
            albumCover: al.cover_file ?? null,
            hay: [title, artist, album, t.tags.GENRE, t.tags.DATE, t.tags.SOURCE]
              .filter(Boolean).join(" ").toLowerCase(),
          });
        }
    return rows;
  }, [lib]);
  const trackByPath = useMemo(() => new Map(trackRows.map((r) => [r.path, r])), [trackRows]);

  const q = filter.trim().toLowerCase();
  const filteredAlbums = useMemo(
    () => albums.filter((a) => !q || [
      a.meta?.ALBUM ?? "",
      a.album_artist ?? a.meta?.ALBUMARTIST ?? a.meta?.ARTIST ?? "",
    ].join(" ").toLowerCase().includes(q)),
    [albums, q]
  );
  const filteredArtists = useMemo(
    () => artists.filter((a) => !q || (a.display_name || a.name || "").toLowerCase().includes(q)),
    [artists, q]
  );
  const filteredTracks = useMemo(() => trackRows.filter((t) => !q || t.hay.includes(q)), [trackRows, q]);

  const { data: playlistDetail, error: playlistError } = useQuery({
    queryKey: ["playlist", playlistId],
    queryFn: () => api.playlist(playlistId!),
    enabled: sourceKind === "playlist" && playlistId !== null,
  });

  // The playlist the select is on, resolved against the loaded list: a
  // playlist deleted or renamed away elsewhere leaves `playlistId` naming
  // nothing, and a stale id drew one name in the select while the query 404'd
  // and the preview reported the playlist that no longer exists.
  const playlist = useMemo(
    () => (playlists ?? []).find((pl) => pl.id === playlistId) ?? null,
    [playlists, playlistId]
  );
  useEffect(() => {
    if (playlistId !== null && playlists && !playlists.some((pl) => pl.id === playlistId)) {
      setPlaylistId(null);
    }
  }, [playlistId, playlists]);

  // The playlist's own rows, in playlist order, resolved to their library
  // metadata (a path the payload no longer holds keeps its file name instead
  // of vanishing — it is still what this playlist would export).
  const playlistList = useMemo(
    () => (playlistDetail?.tracks ?? [])
      .map((p) => ({ path: p, row: trackByPath.get(p) ?? null }))
      .filter(({ path, row }) => !q || (row?.hay ?? path.toLowerCase()).includes(q)),
    [playlistDetail, trackByPath, q]
  );

  // Bulk selection over the FILTERED list: "All" means "everything the filter
  // shows", the only reading that cannot surprise after a search.
  const pick: PickKind | null = PICK_KINDS.find((k) => k === sourceKind) ?? null;
  const noun = pick ? NOUN[pick] : "";
  const listKeys = useMemo(() => {
    if (sourceKind === "albums") return filteredAlbums.map((a) => a.path);
    if (sourceKind === "artists") return filteredArtists.map((a) => a.path);
    if (sourceKind === "tracks") return filteredTracks.map((t) => t.path);
    return [];
  }, [sourceKind, filteredAlbums, filteredArtists, filteredTracks]);
  const selected: Set<string> =
    sourceKind === "albums" ? albumPaths : sourceKind === "artists" ? artistPaths : trackPaths;
  const total = sourceKind === "albums" ? albums.length
    : sourceKind === "artists" ? artists.length
      : sourceKind === "tracks" ? trackRows.length : 0;
  const listed = listKeys.length;
  // What the filter is hiding from a selection the user already made: those
  // rows are still exported, so the tab states them instead of letting the
  // list imply the selection.
  const selListed = listKeys.filter((k) => selected.has(k)).length;
  const selHidden = pick ? selected.size - selListed : 0;

  const bulkSelect = (on: boolean) => {
    const apply = sourceKind === "albums" ? setAlbumPaths
      : sourceKind === "artists" ? setArtistPaths : setTrackPaths;
    apply((prev) => {
      const next = new Set(prev);
      for (const key of listKeys) {
        if (on) next.add(key);
        else next.delete(key);
      }
      return next;
    });
  };

  // Resolve the exact track paths for the chosen source. This is the ONE
  // answer to "what will be exported": the preview, the duration and the
  // options panel all read it, so none of them can claim a different set.
  const paths = useMemo(() => {
    if (sourceKind === "playlist") return playlistDetail?.tracks ?? [];
    if (sourceKind === "library") return trackRows.map((t) => t.path);
    if (sourceKind === "albums") {
      const out: string[] = [];
      for (const a of albums) if (albumPaths.has(a.path)) for (const t of a.tracks ?? []) out.push(t.path);
      return out;
    }
    if (sourceKind === "artists") {
      const out: string[] = [];
      for (const a of artists) if (artistPaths.has(a.path)) for (const al of a.albums ?? []) for (const t of al.tracks ?? []) out.push(t.path);
      return out;
    }
    return trackRows.filter((t) => trackPaths.has(t.path)).map((t) => t.path);
  }, [sourceKind, playlistDetail, albums, albumPaths, artists, artistPaths, trackPaths, trackRows]);

  const totalSeconds = useMemo(
    () => paths.reduce((sum, p) => sum + (trackByPath.get(p)?.tech.length ?? 0), 0),
    [paths, trackByPath]
  );

  // The destination/format half lives in the shared panel, driven by this
  // exact selection.
  const e = useExportOptions(paths, totalSeconds);

  const toggle = (set: Set<string>, path: string, apply: (s: Set<string>) => void) => {
    const next = new Set(set);
    if (next.has(path)) next.delete(path);
    else next.add(path);
    apply(next);
  };

  // "Why is this list empty" — a filter that matched nothing and a library
  // that holds nothing are different sentences, and each tab says which.
  const pickEmptyNote = total === 0
    ? <EmptyState title={`No ${noun} in the library`} hint="Nothing here to export yet — import or download some music first." />
    : <EmptyState
        title="Nothing matches"
        hint={`No ${noun} match “${filter.trim()}”.`}
        onAction={{ label: "Clear the filter", onClick: () => setFilter("") }}
      />;

  // The same question for the preview, answered per tab: nothing picked,
  // nothing in the playlist, nothing in the library. An empty table with no
  // reason was the state this page used to show.
  const previewWhy = sourceKind === "playlist"
    ? playlistId === null
      ? "Choose a playlist above — its tracks are what this tab exports."
      : playlistError
        ? `Could not load that playlist: ${String(playlistError)}`
        : `${playlist?.name ?? "That playlist"} holds no tracks.`
    : sourceKind === "library"
      ? "The library holds no tracks yet."
      : `Nothing selected yet — tick ${noun} above, or use Select all.`;

  const filterBox = (placeholder: string) => (
    <div className="relative mb-2">
      <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-zinc-500" />
      <input
        className="input !py-1 !pl-7 text-xs w-full min-w-0 tap"
        placeholder={placeholder}
        value={filter}
        onChange={(ev) => setFilter(ev.target.value)}
      />
    </div>
  );

  return (
    <div className="p-6 space-y-5 mx-auto max-w-6xl">
      <PageHeader
        icon={HardDriveDownload}
        title="Export"
        subtitle="Copy or convert any part of the library — playlists, albums, artists, single tracks or everything — onto a drive. Tags and artwork ride along; already-exported tracks are skipped on re-runs."
      />

      <div className="grid lg:grid-cols-2 gap-4">
        {/* ---- source -------------------------------------------------- */}
        {/* `min-w-0`: as a grid item the panel's automatic minimum is its
            content's min-content, which the tab strip below sets — and then
            the strip grows the panel instead of scrolling inside its own box
            on a phone. */}
        <div className="panel min-w-0">
          <div className="text-xs font-bold text-zinc-300 mb-2">Source</div>
          {/* Five options are wider than a phone: the strip scrolls in its own
              box instead of pushing the page sideways. */}
          <div className="overflow-x-auto">
            <Segmented value={sourceKind} onChange={setSourceKind} options={SOURCE_KINDS} className="mb-3" />
          </div>

          {sourceKind === "playlist" && (
            <>
              <select
                className="input !py-1 text-xs w-full min-w-0 tap"
                value={playlistId ?? ""}
                onChange={(ev) => setPlaylistId(ev.target.value ? Number(ev.target.value) : null)}
              >
                <option value="">Choose a playlist…</option>
                {(playlists ?? []).map((pl) => (
                  <option key={pl.id} value={pl.id}>
                    {pl.name} ({pl.track_count})
                  </option>
                ))}
              </select>
              {playlistId !== null && (
                <>
                  {filterBox("Filter this playlist's tracks…")}
                  {/* The filter narrows the LIST, never the playlist: this tab
                      exports the playlist the user chose, and saying so is the
                      difference between a narrowed view and a narrowed
                      export. */}
                  <div className="text-[11px] text-zinc-500 mb-2">
                    {q
                      ? `${playlistList.length} of ${playlistDetail?.tracks?.length ?? 0} tracks match “${filter.trim()}” — the filter narrows this list only; the whole playlist is exported`
                      : `${playlistDetail?.tracks?.length ?? 0} tracks from ${playlist?.name ?? "this playlist"}`}
                  </div>
                  <PickTable
                    cols={PICK_COLS.playlist}
                    rows={playlistList.map(({ path, row }) => ({
                      key: path,
                      cover: (
                        <TrackCover
                          albumPath={row?.albumPath ?? path.split(/[\\/]/).slice(0, -1).join("/")}
                          trackCover={row?.coverFile}
                          albumCover={row?.albumCover}
                          wrapperClass="h-9 w-9 rounded bg-raise border border-border overflow-hidden shrink-0"
                        />
                      ),
                      cells: {
                        name: <span className="break-words min-w-0">{row?.title ?? path.split(/[\\/]/).pop()}</span>,
                        artist: row?.artist,
                        album: row?.album,
                        dur: row ? fmtDuration(row.tech.length) : "—",
                      },
                    }))}
                    emptyNote={
                      playlistError
                        ? <EmptyState title="Could not load that playlist" hint={String(playlistError)} />
                        : (playlistDetail?.tracks?.length ?? 0) === 0
                          ? <EmptyState title="That playlist is empty" hint={`${playlist?.name ?? "It"} holds no tracks to export.`} />
                          : <EmptyState
                              title="Nothing matches"
                              hint={`No track of this playlist matches “${filter.trim()}”.`}
                              onAction={{ label: "Clear the filter", onClick: () => setFilter("") }}
                            />
                    }
                  />
                </>
              )}
            </>
          )}

          {sourceKind === "library" && (
            <div className="flex items-start gap-2 text-xs text-zinc-400 border border-border rounded-md p-3 bg-panel/50">
              <Library className="h-4 w-4 text-accent shrink-0 mt-0.5" />
              <span>
                Every track the library holds — <b className="text-zinc-200">{trackRows.length} tracks</b>{" "}
                across {albums.length} albums by {artists.length} artists. There is nothing to pick
                and nothing to filter: the whole library <em>is</em> the selection, and the preview
                below is all of it.
              </span>
            </div>
          )}

          {pick && (
            <>
              {filterBox(
                pick === "albums" ? "Filter albums / artists…"
                  : pick === "artists" ? "Filter artists…"
                    : "Filter tracks / artists / albums…"
              )}
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1 mb-2 text-[11px] text-zinc-500">
                <span>
                  {q ? `${listed} of ${total} ${noun} match “${filter.trim()}”` : `${total} ${noun}`}
                </span>
                <span className="text-zinc-600">·</span>
                <span>{selected.size ? `${selected.size} selected` : "none selected"}</span>
                {selHidden > 0 && (
                  <span className="text-zinc-600">
                    · {selHidden} of those the filter hides (still exported — the preview shows every selected row)
                  </span>
                )}
              </div>
              <div className="flex flex-wrap items-center gap-2 mb-2 text-[11px]">
                <button
                  className="btn !py-0.5 !px-2 text-[11px] tap"
                  onClick={() => bulkSelect(true)}
                  title="Ticks every row the filter currently shows"
                >
                  {q ? `Select all ${listed} matching` : `Select all ${listed}`}
                </button>
                <button
                  className="btn !py-0.5 !px-2 text-[11px] tap"
                  disabled={!selListed}
                  onClick={() => bulkSelect(false)}
                  title="Unticks the rows the filter currently shows; rows it hides stay selected"
                >
                  {q ? `Clear ${selListed} matching` : `Clear ${selListed} listed`}
                </button>
              </div>
              <PickTable
                cols={PICK_COLS[pick]}
                rows={pick === "albums"
                  ? filteredAlbums.map((a) => ({
                      key: a.path,
                      checked: albumPaths.has(a.path),
                      onToggle: () => toggle(albumPaths, a.path, setAlbumPaths),
                      cover: (
                        <CoverImg
                          albumPath={a.path}
                          coverFile={a.cover_file}
                          wrapperClass="h-9 w-9 rounded bg-raise border border-border overflow-hidden shrink-0"
                        />
                      ),
                      cells: {
                        name: <span className="break-words min-w-0">{a.meta?.ALBUM ?? a.path.split(/[\\/]/).pop()}</span>,
                        artist: a.album_artist ?? a.meta?.ALBUMARTIST ?? a.meta?.ARTIST,
                        count: a.tracks?.length ?? 0,
                      },
                    }))
                  : pick === "artists"
                    ? filteredArtists.map((a) => ({
                        key: a.path,
                        checked: artistPaths.has(a.path),
                        onToggle: () => toggle(artistPaths, a.path, setArtistPaths),
                        cells: {
                          name: <span className="break-words min-w-0">{a.display_name || a.name}</span>,
                          count: `${a.albums?.length ?? 0} albums · ${(a.albums ?? []).reduce((n, al) => n + (al.tracks?.length ?? 0), 0)} tracks`,
                        },
                      }))
                    : filteredTracks.map((t) => ({
                        key: t.path,
                        checked: trackPaths.has(t.path),
                        onToggle: () => toggle(trackPaths, t.path, setTrackPaths),
                        cover: (
                          <TrackCover
                            albumPath={t.albumPath}
                            trackCover={t.coverFile}
                            albumCover={t.albumCover}
                            wrapperClass="h-9 w-9 rounded bg-raise border border-border overflow-hidden shrink-0"
                          />
                        ),
                        cells: {
                          name: <span className="break-words min-w-0">{t.title}</span>,
                          artist: t.artist,
                          album: t.album,
                          dur: fmtDuration(t.tech.length),
                        },
                      }))}
                emptyNote={pickEmptyNote}
              />
            </>
          )}
        </div>

        {/* ---- destination + format (the shared option surface) --------- */}
        <div className="panel min-w-0">
          <ExportOptionsPanel e={e} />
        </div>
      </div>

      {/* ---- preview ---------------------------------------------------- */}
      {/* Full width and OUTSIDE the two-column grid, because it is the whole
          point: this is the library's own track table, and half a page is what
          made its `#` column wrap a two-digit number one digit per line and
          its Artist/Album cells truncate mid-word. The source and option
          panels above stay side by side. */}
      <div className="panel">
        <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 mb-2">
          <div className="text-xs font-bold text-zinc-300">Preview</div>
          <div className="text-[11px] text-zinc-500">
            {paths.length
              ? `${paths.length} track${paths.length === 1 ? "" : "s"}${totalSeconds > 0 ? ` · ${fmtDuration(totalSeconds)}` : ""}`
              : "nothing to export yet"}
          </div>
        </div>
        {paths.length === 0 ? (
          <EmptyState title="Nothing to preview" hint={previewWhy} />
        ) : (
          <div className="max-h-96 overflow-auto border border-border rounded-md table-scroll">
            <table className={`${TABLE_FIT} text-xs`}>
              <thead className="border-b border-border sticky top-0 z-10 bg-card">
                <tr>
                  {PREVIEW_COLS.map((c) => (
                    <th key={c.id} className={`th !py-1 ${TRACK_COL_W[c.id] ?? ""}${phoneHide(TRACK_PHONE_CLS, c.id)}`}>
                      {c.id === "cover" ? <span className="sr-only">Cover</span> : c.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {paths.slice(0, PREVIEW_ROWS).map((p, i) => {
                  const m = trackByPath.get(p);
                  return (
                    <tr key={`${p}-${i}`} className="table-row">
                      {PREVIEW_COLS.map((c) => {
                        // Every cell carries its column's phone fold: a class on
                        // the header alone would misalign the fixed-layout grid.
                        const cls = phoneHide(TRACK_PHONE_CLS, c.id);
                        switch (c.id) {
                          case "num":
                            /* `cell-nowrap`, not just the column floor: the
                               floor says how wide the cell may be, this says
                               the number is never broken across two lines.
                               Untagged rows fall back to their position, which
                               is the playlist's own order. */
                            return <td key={c.id} className={`td cell-nowrap text-zinc-600${cls}`}>{m?.num || i + 1}</td>;
                          case "cover":
                            return (
                              <td key={c.id} className="td cell-cover pr-0">
                                <TrackCover
                                  albumPath={m?.albumPath ?? p.split(/[\\/]/).slice(0, -1).join("/")}
                                  trackCover={m?.coverFile}
                                  albumCover={m?.albumCover}
                                  wrapperClass="h-9 w-9 rounded bg-raise border border-border overflow-hidden shrink-0"
                                />
                              </td>
                            );
                          case "title":
                            return (
                              <td key={c.id} className="td">
                                <TrackTitleCell
                                  trailing={
                                    <span className="shrink-0" onClick={(ev) => ev.stopPropagation()}>
                                      <StarRating
                                        size="sm"
                                        value={ratingOf(ratings, p)}
                                        onChange={(v) => setRating(p, v)}
                                        pending={pending(p)}
                                      />
                                    </span>
                                  }
                                >
                                  <span className="break-words min-w-0">{m?.title ?? p.split(/[\\/]/).pop()}</span>
                                </TrackTitleCell>
                              </td>
                            );
                          case "artist":
                            return <td key={c.id} className={`td text-zinc-400 break-words${cls}`}>{m?.artist || "—"}</td>;
                          case "album":
                            return <td key={c.id} className={`td text-zinc-500 break-words${cls}`}>{m?.album || "—"}</td>;
                          case "year":
                            return (
                              <td key={c.id} className={`td text-zinc-500${cls}`} title={m?.tags.DATE ?? undefined}>
                                {m?.tags.DATE ? fmtDateCell(m.tags.DATE, false) : "—"}
                              </td>
                            );
                          case "genre":
                            return <td key={c.id} className={`td text-zinc-500 break-words${cls}`}>{m?.tags.GENRE || "—"}</td>;
                          case "media":
                            return <td key={c.id} className={`td${cls}`}><MediaChip media={m?.tags.MEDIA} /></td>;
                          case "duration":
                            return <td key={c.id} className={`td text-zinc-500${cls}`}>{m ? fmtDuration(m.tech.length) : "—"}</td>;
                          case "bitrate":
                            return <td key={c.id} className={`td text-zinc-500${cls}`}>{m ? fmtTech(m.tech) || "—" : "—"}</td>;
                          case "dr":
                            return (
                              <td key={c.id} className={`td text-zinc-500 tabular-nums${cls}`} title="Dynamic range">
                                {m?.tags["DYNAMIC RANGE"] || "—"}
                              </td>
                            );
                          case "source":
                            return <td key={c.id} className={`td text-zinc-500 break-words${cls}`}>{m?.tags.SOURCE || "—"}</td>;
                          case "type":
                            return (
                              <td key={c.id} className={`td text-zinc-500${cls}`} title={m?.isVideo ? "Music video" : "Audio track"}>
                                {m?.isVideo
                                  ? <span className="inline-flex items-center gap-1"><FileVideo className="h-3.5 w-3.5" /> Video</span>
                                  : "Audio"}
                              </td>
                            );
                          case "inst":
                            return (
                              <td key={c.id} className={`td${cls}`}>
                                {m?.tags.INSTRUMENTAL === "1"
                                  ? <span className="chip bg-zinc-800 text-zinc-400 border border-border text-[10px]">INST</span>
                                  : <span className="text-zinc-600">—</span>}
                              </td>
                            );
                          default:
                            return <td key={c.id} className={`td text-zinc-500${cls}`}>—</td>;
                        }
                      })}
                    </tr>
                  );
                })}
                {paths.length > PREVIEW_ROWS && (
                  <tr className="table-row">
                    <td colSpan={PREVIEW_COLS.length} className="td !py-1.5 text-[11px] text-zinc-600">
                      +{paths.length - PREVIEW_ROWS} more…
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
