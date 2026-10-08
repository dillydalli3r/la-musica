import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Disc3, FileVideo, Heart, HeartOff, ListChecks, ListMusic, Mic2, Play } from "lucide-react";import { api } from "../api";
import { useStore } from "../store";
import { toast } from "../store";
import { unfavoriteMany, useFavorites, useTrackLikes, type FavKind } from "../lib/favs";
import SelectAllButton from "../components/SelectAllButton";
import { AdvisoryMark, CachedMark, EmptyState, PageLoading } from "../components/Badges";
import PageHeader from "../components/PageHeader";
import { TrackCover } from "../components/CoverImg";
import AlbumCard from "../components/AlbumCard";
import ArtistName from "../components/ArtistName";
import DownloadButton from "../components/DownloadButton";
import { ExportButton, usePlaylistTracks } from "../components/ExportDialog";
import MoreLikeThis from "../components/MoreLikeThis";
import FavHeart from "../components/FavHeart";
import { TrackActionsMenu } from "../components/TagActionsMenu";
import { fmtDuration, GRID_SIZE_MIN } from "../lib/fmt";
import Segmented from "../components/Segmented";
import { sortRows, SortHeader, toggleSort, type SortState } from "../lib/sort.tsx";
import { ColumnsMenu, useColumnPrefs, useCustomColumns, customColValue, customCols, type Col } from "../lib/columns";
import { artistRef, artistMbid } from "../lib/refs";
import type { Album, Artist, Playlist, Track } from "../types";

const TABS = [
  { id: "tracks", label: "Liked tracks", icon: Heart },
  { id: "albums", label: "Albums", icon: Disc3 },
  { id: "artists", label: "Artists", icon: Mic2 },
  { id: "playlists", label: "Playlists", icon: ListMusic },
] as const;

type Kind = (typeof TABS)[number]["id"];

/** What the page hands every tab in select mode: the mode, what is ticked, and
 *  the one way to tick it. The BATCH action lives on the page (one bar, one
 *  wording per tab), so a tab never grows its own idea of what removal is. */
type TabSelectProps = {
  selectMode: boolean;
  picked: string[];
  onPick: (key: string) => void;
};

export default function FavoritesPage() {
  const { kind: raw } = useParams();
  const navigate = useNavigate();
  const kind: Kind = (TABS.some((t) => t.id === raw) ? raw : "tracks") as Kind;

  const { artists, albums, tracks, libError } = useLibraryMaps();
  const { data: likes, isError: likesFailed } = useTrackLikes();
  const { data: favs, isError: favsFailed } = useFavorites();
  const qc = useQueryClient();

  // The page's ONE select mode, over the tab in view (the same idiom the
  // Library, Home, the artist page and the trash use: a Select toggle in the
  // header, a Select-all beside it, and a batch bar once something is ticked).
  // The keys of one tab mean nothing in another — a track path is not an album
  // path — so a tab change starts over rather than carrying a selection the
  // reader can no longer see.
  const [selectMode, setSelectMode] = useState(false);
  // The selection CARRIES ITS TAB, so a tab change cannot leave a selection
  // behind: the keys of one tab (track paths) mean nothing in another (album
  // paths), and a batch action must never fire on a list the reader cannot
  // see. An effect that emptied a shared array on `kind` would do the same
  // thing a render later; this cannot be a render out of step at all.
  const [pickedState, setPicked] = useState<{ tab: Kind; keys: string[] }>({ tab: kind, keys: [] });
  const picked = pickedState.tab === kind ? pickedState.keys : [];
  const [removing, setRemoving] = useState(false);

  const togglePick = (key: string) =>
    setPicked((prev) => {
      const keys = prev.tab === kind ? prev.keys : [];
      return { tab: kind, keys: keys.includes(key) ? keys.filter((k) => k !== key) : [...keys, key] };
    });

  /** Take everything ticked off the favourites list in one action. The writes
   *  are `unfavoriteMany` (the same two endpoints the hearts call, one
   *  invalidation at the end), and what happened is reported: a batch that
   *  half-wrote says so instead of looking complete. */
  const removePicked = async () => {
    if (!picked.length || removing) return;
    setRemoving(true);
    try {
      // The tab ids ARE the singular kind with an "s" (tracks/albums/artists/
      // playlists), which is exactly what the store wants.
      const singular = kind.slice(0, -1) as FavKind;
      const { removed, failed } = await unfavoriteMany(kind === "tracks" ? "track" : singular, picked, qc);
      const what = kind === "tracks" ? "track" : kind.slice(0, -1);
      if (removed && !failed) toast(`Removed ${removed} ${what}${removed === 1 ? "" : "s"} from your favorites`);
      else if (removed) toast(`Removed ${removed} of ${picked.length} — ${failed} could not be written`);
      else toast.error(`Nothing could be removed (${failed} write${failed === 1 ? "" : "s"} failed)`);
      setPicked({ tab: kind, keys: [] });
    } finally {
      setRemoving(false);
    }
  };

  // A liked playlist holds no paths of its own — only the playlists tab needs
  // them, so the detail fetches wait for that tab (same shared hook the
  // Playlists page uses for its own bulk actions).
  const playlistIds = useMemo(
    () => (favs?.playlists ?? []).map(Number).filter((n) => Number.isFinite(n)),
    [favs]
  );
  const { data: playlistPaths = [] } = usePlaylistTracks(playlistIds, kind === "playlists");

  // What the header's Download/Export act on: the CURRENT tab's liked set —
  // "the whole liked set" as the page presents it.
  const favPaths = useMemo(() => {
    if (kind === "tracks") return likes?.paths ?? [];
    if (kind === "albums")
      return (favs?.albums ?? []).flatMap((p) => albums.get(p)?.album.tracks.map((t) => t.path) ?? []);
    if (kind === "artists")
      return (favs?.artists ?? []).flatMap(
        (p) => artists.get(p)?.albums.flatMap((al) => al.tracks.map((t) => t.path)) ?? []
      );
    return playlistPaths;
  }, [kind, likes, favs, albums, artists, playlistPaths]);

  const favSeconds = useMemo(
    () => favPaths.reduce((s, p) => s + (tracks.get(p)?.track.tech?.length ?? 0), 0),
    [favPaths, tracks]
  );

  // The header's Download/Export act on the CURRENT tab's liked set, and to
  // them an empty set and a failed fetch look identical: "this favorites tab
  // is empty" would then report a request failure as the user having nothing.
  // The two tabs whose paths are joined against the library count its failure
  // as theirs for the same reason.
  const tabFailed =
    kind === "tracks"
      ? likesFailed
      : kind === "albums" || kind === "artists"
        ? favsFailed || !!libError
        : favsFailed;
  const downloadHint = tabFailed
    ? "Could not load this favorites tab — nothing is known to be downloadable"
    : "Nothing to download — this favorites tab is empty";
  const exportHint = tabFailed
    ? "Could not load this favorites tab — nothing is known to be exportable"
    : "Nothing to export — this favorites tab is empty";

  return (
    <div className="p-6 space-y-5 mx-auto max-w-[1600px]">
      <PageHeader
        icon={Heart}
        title="Favorites"
        actions={
          // Four tabs do not fit beside the title at 390 px: they wrap, and
          // the width cap (viewport-relative — the header's actions box is
          // sized by its content, so `max-w-full` cannot bound it) keeps the
          // box inside the phone. The tab height is NOT forced here: the
          // buttons' own `.tap` sets the phone floor (44px) and would lose to
          // a `[&>button]:min-h-…` utility on this wrapper, which is more
          // specific than the class.
          <>
            <Segmented
              value={kind}
              onChange={(k) => navigate(`/favorites/${k}`)}
              options={TABS.map((t) => ({ id: t.id, label: t.label, icon: t.icon }))}
              className="max-w-[calc(100vw-9rem)] flex-wrap justify-end"
            />
            <button
              className={`btn-ghost !py-1.5 text-xs tap ${selectMode ? "!text-accent !border-accent/50" : ""}`}
              onClick={() => {
                if (selectMode) setPicked({ tab: kind, keys: [] });
                setSelectMode(!selectMode);
              }}
              title="Select mode — tick several and take them off your favorites at once"
            >
              <ListChecks className="h-3.5 w-3.5" /> Select
            </button>
            {/* both act on the tab in view — a like list is the set this page
                represents */}
            <DownloadButton
              paths={favPaths}
              label={kind === "tracks" ? "Download likes" : "Download all"}
              emptyReason={downloadHint}
            />
            <ExportButton
              paths={favPaths}
              seconds={favSeconds}
              label={kind === "tracks" ? "Export likes" : "Export all"}
              emptyReason={exportHint}
              title="Export these favorites to a drive"
              dialogSubtitle={`${favPaths.length} track${favPaths.length === 1 ? "" : "s"} from your favorites`}
            />
          </>
        }
      />
      {/* The batch bar the app's other select modes use (TrashPage's own
          strip): what is ticked, and the ONE action that acts on it. */}
      {selectMode && picked.length > 0 && (
        <div className="flex items-center gap-2 bg-accent/15 border border-accent/40 rounded-lg px-3 py-2 flex-wrap">
          <span className="text-xs font-medium text-accent-soft">
            {picked.length} selected
          </span>
          <div className="ml-auto flex gap-1.5 flex-wrap">
            <button
              className="btn-danger !py-1 text-xs tap"
              onClick={removePicked}
              disabled={removing}
              title={kind === "tracks" ? "Unlike every ticked track" : "Remove every ticked entry from your favorites"}
            >
              <HeartOff className="h-3.5 w-3.5" />
              {kind === "tracks" ? "Unlike" : "Remove from favorites"}
            </button>
            <button className="btn-ghost !py-1 text-xs tap" onClick={() => setPicked({ tab: kind, keys: [] })}>
              Clear
            </button>
          </div>
        </div>
      )}

      {kind === "tracks" && <LikedTracks selectMode={selectMode} picked={picked} onPick={togglePick} />}
      {/* the shelf renders null while it has nothing to suggest, so it costs
          the tabs without a recommendation nothing */}
      {kind === "tracks" && <MoreLikeThis kind="favorites" target="tracks" />}
      {kind === "albums" && <FavAlbums selectMode={selectMode} picked={picked} onPick={togglePick} />}
      {kind === "artists" && <FavArtists selectMode={selectMode} picked={picked} onPick={togglePick} />}
      {(kind === "albums" || kind === "artists") && (
        <MoreLikeThis kind="favorites" target="albums" />
      )}
      {kind === "playlists" && <FavPlaylists selectMode={selectMode} picked={picked} onPick={togglePick} />}
    </div>
  );
}

/** Flat library lookups shared by every tab. The query's own state rides
 *  along: every tab JOINS its favorite ids against these maps, so a library
 *  that failed to load produces rows that are empty for a reason the tab has
 *  to be able to tell apart from "you have none". */
function useLibraryMaps() {
  const { data: lib, isError, error, refetch } = useQuery({ queryKey: ["library"], queryFn: () => api.library() });
  const maps = useMemo(() => {
    const tracks = new Map<string, { track: Track; album: Album; artist: Artist }>();
    const albums = new Map<string, { album: Album; artist: Artist }>();
    const artists = new Map<string, Artist>();
    for (const a of lib?.artists ?? []) {
      artists.set(a.path, a);
      for (const al of a.albums) {
        albums.set(al.path, { album: al, artist: a });
        for (const t of al.tracks) tracks.set(t.path, { track: t, album: al, artist: a });
      }
    }
    return { lib, tracks, albums, artists };
  }, [lib]);
  return { ...maps, libError: isError ? error : null, refetchLib: refetch };
}

/** A failed fetch is not an empty set. Every tab below renders "No … yet" when
 *  its rows come back empty — which is only the truth once the payload
 *  ARRIVED. A request that failed says so here instead of reporting the user's
 *  favorites as none, and offers the retry rather than a dead end. */
function LoadFailed({ what, error, onRetry }: { what: string; error: unknown; onRetry?: () => void }) {
  return (
    <EmptyState
      title={`Could not load ${what}`}
      hint={`${error instanceof Error ? error.message : String(error)} — this is a failed request, not an empty list; what you have here is unchanged.`}
      onAction={onRetry && { label: "Try again", onClick: onRetry }}
    />
  );
}

/** What an entry the LIBRARY cannot resolve is called: the folder's own name,
 *  with the artist/album folder's MusicBrainz suffix stripped (`Artist
 *  [a466c2a2-…]` reads "Artist", the same rule the artist rows use). */
function folderName(path: string): string {
  const base = String(path || "").split("/").filter(Boolean).pop() ?? String(path || "");
  return base.replace(/\s*\[[0-9a-f-]{8,}\]\s*$/, "");
}

function displayArtist(al: Album, a: Artist) {
  return al.album_artist || a.name;
}

// ------------------------------------------------------------------------ //
// Liked tracks
// ------------------------------------------------------------------------ //

/** The liked-track table's columns — the library's track view, trimmed to
 *  what a like row actually carries. The # and cover cells have no sort key:
 *  the "#" is the row's own position, not a track number. */
const LIKED_COLS: Col[] = [
  { id: "num", label: "#", sortKey: "" },
  { id: "cover", label: "", sortKey: "" },
  { id: "title", label: "Title", sortKey: "title" },
  { id: "artist", label: "Artist", sortKey: "artistName" },
  { id: "album", label: "Album", sortKey: "albumName" },
  { id: "duration", label: "Duration", sortKey: "dur" },
];

/** Fixed widths — the global `table-layout: fixed` needs one per column;
 *  the title absorbs what is left. */
const LIKED_COL_W: Record<string, string> = {
  num: "w-12",
  cover: "w-12",
  title: "w-auto",
  artist: "w-[16%]",
  album: "w-[16%]",
  // A phone-width 7% is ~27 px — narrower than the "3:45" it holds, so the
  // duration gets a real width below `md` and its share only from there up.
  duration: "w-14 md:w-[7%]",
};

/** The liked table's floor, derived from its own columns: the fixed num and
 *  cover take 96 px, artist + album + duration take 39% of what is left, and
 *  the Title (auto) gets the remainder — but Duration is the tight one, since
 *  7% of the width has to hold the 56 px a "3:45" needs, and 56 / 0.07 = 800.
 *  There the title still has 0.61 × 800 − 96 = 392 px. Without it the fixed
 *  layout simply shrinks columns past what they hold (the title measured 0 px
 *  in the library's own table at 580 px), so the table keeps this width and
 *  the wrapper scrolls instead. `md:` because below it the phone fold has
 *  already left the title the whole row. */
const LIKED_MIN_W = "md:min-w-[800px]";

/** Floor for the two favorites tables that follow the same shape — an auto name
 *  column plus percentages: each counter column holds a count at 12 %, so 380 px
 *  is where a count still fits; the name gets 0.76 / 0.88 of the width there. */
const FAV_TABLE_MIN_W = "md:min-w-[380px]";

/** Liked-tracks table on a phone (390 px): the row keeps its cover, its title
 *  and the length. #, artist and album fold below `md` — their percentage
 *  widths leave about five characters of text there, and this is a like list,
 *  so the track names are the point. The class must go on the header AND its
 *  cells or the fixed grid misaligns. */
const PHONE_HIDE = " hidden md:table-cell";
const LIKED_PHONE_CLS: Record<string, string> = {
  num: PHONE_HIDE,
  artist: PHONE_HIDE,
  album: PHONE_HIDE,
};
/** User-added tag columns fold with the built-ins they sit beside. */
function likedHide(id: string): string {
  return LIKED_PHONE_CLS[id] ?? (id.startsWith("tag:") ? PHONE_HIDE : "");
}

function LikedTracks({ selectMode, picked, onPick }: {
  selectMode: boolean;
  picked: string[];
  onPick: (key: string) => void;
}) {
  const { data: likes, isLoading, isError, error, refetch } = useTrackLikes();
  const { tracks } = useLibraryMaps();
  const playNow = useStore((s) => s.playNow);
  const navigate = useNavigate();
  // Same column machinery as the library's track table, on its own prefs key:
  // the visible set and any tag columns are per-table.
  const [likedCustom, addLikedCustomCol, removeLikedCustomCol] = useCustomColumns("fav-tracks");
  const likedDefs: Col[] = [...LIKED_COLS, ...customCols(likedCustom, "tags")];
  const [likedCols, toggleLikedCol] = useColumnPrefs("fav-tracks", likedDefs);
  const [sort, setSort] = useState<SortState | null>(null);
  const addLikedCustom = (tag: string, label?: string) => {
    const id = addLikedCustomCol(tag, label);
    if (id) toggleLikedCol(id);
  };

  const rows = useMemo(() => {
    const paths = likes?.paths ?? [];
    return paths.map((p) => {
      const hit = tracks.get(p);
      if (hit) {
        const { track, album, artist } = hit;
        return {
          path: p,
          missing: false,
          mbid: track.tags.MUSICBRAINZ_TRACKID ?? undefined,
          title: track.tags.TITLE ?? track.file,
          artistName: displayArtist(album, artist),
          albumName: album.meta?.ALBUM ?? album.path.split("/").pop() ?? "",
          advisory: (track.tags.ITUNESADVISORY as string | undefined) ?? null,
          isVideo: !!track.is_video,
          albumPath: album.path,
          trackPath: track.path,
          dur: track.tech?.length,
          // the file's own tag maps — what the tag columns read
          tags: track.tags,
          tech: track.tech,
          coverFile: track.cover_file ?? null,
          albumCover: album.cover_file ?? null,
          queue: {
            path: track.path,
            file: track.file,
            albumPath: album.path,
            artist: displayArtist(album, artist),
            album: album.meta?.ALBUM ?? undefined,
            title: track.tags.TITLE || undefined,
            coverFile: track.cover_file ?? null,
            albumCover: album.cover_file ?? null,
            advisory: track.tags.ITUNESADVISORY ?? null,
          },
        };
      }
      return {
        path: p,
        missing: true,
        mbid: undefined,
        title: p.split("/").pop() ?? p,
        artistName: "",
        albumName: "",
        advisory: null,
        isVideo: false,
        albumPath: p.split("/").slice(0, -1).join("/"),
        trackPath: p,
        dur: undefined,
        queue: { path: p, file: p.split("/").pop() ?? p, albumPath: p.split("/").slice(0, -1).join("/") },
      };
    });
  }, [likes, tracks]);

  const view = useMemo(() => sortRows(rows, sort), [rows, sort]);

  // `i` indexes the full rows array (missing files included); the queue only
  // holds playable tracks, so translate it before handing it to playNow.
  const play = (i: number) => {
    const playable = view.filter((r) => !r.missing).map((r) => r.queue);
    if (!playable.length) return;
    const idx = view.slice(0, i).filter((r) => !r.missing).length;
    playNow(playable, Math.min(idx, playable.length - 1));
  };

  if (isLoading) return <PageLoading />;
  if (isError) return <LoadFailed what="your liked tracks" error={error} onRetry={() => refetch()} />;
  if (!rows.length)
    return (
      <EmptyState
        title="No liked tracks yet"
        hint="Use the heart in the player bar or on any track row — liked tracks show up here."
      />
    );

  return (
    <div>
      <div className="flex items-center gap-2 pb-2 flex-wrap">
        <span className="text-xs text-zinc-500">
          {rows.length} liked track{rows.length === 1 ? "" : "s"}
        </span>
        {selectMode && (
          <SelectAllButton
            count={rows.length}
            noun="tracks"
            all={rows.length > 0 && picked.length === rows.length}
            onSelectAll={() => rows.forEach((r) => !picked.includes(r.path) && onPick(r.path))}
            onClear={() => rows.forEach((r) => picked.includes(r.path) && onPick(r.path))}
          />
        )}
        <div className="ml-auto flex items-center gap-2">
          <ColumnsMenu
            cols={likedDefs}
            visible={likedCols}
            onToggle={toggleLikedCol}
            onAddCustom={addLikedCustom}
            onRemoveCustom={removeLikedCustomCol}
          />
          <button className="btn-primary !py-1 text-xs min-h-[2rem] md:min-h-0" onClick={() => play(0)}>
            <Play className="h-3.5 w-3.5" /> Play all
          </button>
        </div>
      </div>
      {/* Same table language as the library's track view: identical columns,
          cell classes, cover chips and hover-revealed hearts. Click plays,
          ctrl/shift-click opens the track page. */}
      <div className="overflow-x-auto">
        <table className={`w-full text-sm ${LIKED_MIN_W}`}>
          <thead className="border-b border-border">
            <tr>
              {/* The tick column exists only while select mode is on, exactly
                  like the library's own tables. */}
              {selectMode && <th className="th pr-0 w-8" aria-label="Select" />}
              {likedDefs.filter((c) => likedCols.includes(c.id)).map((c) =>
                c.sortKey ? (
                  <SortHeader
                    key={c.id}
                    label={c.label}
                    sort={sort}
                    sortKey={c.sortKey}
                    onSort={(k) => setSort(toggleSort(sort, k))}
                    className={(LIKED_COL_W[c.id] ?? (c.tag ? "w-[10%]" : "")) + likedHide(c.id)}
                  />
                ) : (
                  <th key={c.id} className={`th ${LIKED_COL_W[c.id] ?? ""}${likedHide(c.id)}`} title={c.id === "cover" ? "Cover art" : undefined}>
                    {c.id === "cover" ? <span className="sr-only">Cover</span> : c.label}
                  </th>
                )
              )}
            </tr>
          </thead>
          <tbody className="stagger">
            {view.map((r, i) => (
              <tr
                key={r.path}
                className={`table-row group ${selectMode ? "" : "cursor-pointer"} ${picked.includes(r.path) ? "bg-accent/10" : ""}`}
                title={selectMode ? "Click to select" : "Click to play · Ctrl-click to open track page"}
                onClick={(e) => {
                  // Select mode owns the row click, like every other list in
                  // the app: a tick and a play on the same gesture would do
                  // both at once.
                  if (selectMode) {
                    onPick(r.path);
                  } else if (!r.missing && (e.ctrlKey || e.metaKey || e.shiftKey)) {
                    navigate(`/track/${encodeURIComponent(r.trackPath)}`);
                  } else if (!r.missing) {
                    play(i);
                  }
                }}
              >
                {selectMode && (
                  <td className="td pr-0" onClick={(e) => e.stopPropagation()}>
                    <input type="checkbox" checked={picked.includes(r.path)} onChange={() => onPick(r.path)} title="Select track" />
                  </td>
                )}
                {likedCols.includes("num") && <td className={`td cell-nowrap text-zinc-600 tabular-nums${PHONE_HIDE}`}>{i + 1}</td>}
                {likedCols.includes("cover") && (
                <td className="td cell-cover pr-0">
                  {"coverFile" in r ? (
                    <TrackCover
                      albumPath={r.albumPath}
                      trackCover={r.coverFile}
                      albumCover={r.albumCover}
                      wrapperClass="h-9 w-9 rounded bg-raise overflow-hidden shrink-0"
                    />
                  ) : null}
                </td>
                )}
                {likedCols.includes("title") && (
                <td className="td">
                  <div className="flex items-center gap-1.5 min-w-0">
                    <span className={`break-words flex-1 min-w-[8rem] ${r.missing ? "text-zinc-500" : "hover:text-accent-soft"}`} title={r.missing ? r.path : r.title}>
                      {r.title}
                    </span>
                    {!r.missing && <AdvisoryMark value={r.advisory} />}
                    <CachedMark path={r.path} />
                    {!r.missing && r.isVideo && (
                      <span title="Music video" className="shrink-0 inline-flex"><FileVideo className="h-3.5 w-3.5 text-zinc-500" /></span>
                    )}
                    <span className="shrink-0" onClick={(e) => e.stopPropagation()}>
                      <FavHeart kind="track" id={r.path} mbid={r.mbid} iconClass="h-3.5 w-3.5" title="Unlike" revealOnHover />
                    </span>
                    <span className="row-hover shrink-0" onClick={(e) => e.stopPropagation()}>
                      <TrackActionsMenu path={r.path} />
                    </span>
                  </div>
                </td>
                )}
                {likedCols.includes("artist") && (
                  <td className={`td text-zinc-400 break-words${PHONE_HIDE}`}>{r.missing ? "—" : r.artistName}</td>
                )}
                {likedCols.includes("album") && (
                  <td className={`td text-zinc-500 break-words${PHONE_HIDE}`}>{r.missing ? "—" : r.albumName}</td>
                )}
                {likedCols.includes("duration") && <td className="td text-zinc-500">{fmtDuration(r.dur)}</td>}
                {likedCustom.filter((c) => likedCols.includes(c.id)).map((c) => (
                  <td key={c.id} className={`td text-zinc-500 break-words${likedHide(c.id)}`} title={`Tag: ${c.tag}`}>
                    {customColValue(r, c.tag) || "—"}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------------ //
// Favorite albums
// ------------------------------------------------------------------------ //
function FavAlbums({ selectMode, picked, onPick }: TabSelectProps) {
  const { data: favs, isLoading, isError, error, refetch } = useFavorites();
  const { albums, libError, refetchLib } = useLibraryMaps();

  // EVERY favourite, in the order the store returns them (newest first). One
  // the library cannot resolve — its folder was moved, renamed, or holds no
  // audio at all — is NOT dropped: it becomes the row shape the app already
  // draws for a favourite the library no longer holds (`owned: false`: the
  // card keeps the identity it was favourited by and draws no grade, no play
  // button and no link), which is what Home's own favorites shelf does. The
  // filter that used to sit here is why a favourite could be missing from this
  // page while still being in the store, with nothing on screen able to take
  // it off.
  const rows = useMemo(
    () =>
      (favs?.albums ?? []).map((p) => {
        const hit = albums.get(p);
        if (hit) return { key: p, al: { ...hit.album, owned: true } as Album & { owned?: boolean },
                          artistName: displayArtist(hit.album, hit.artist) };
        return {
          key: p,
          al: {
            path: p, owned: false, artist: "", tracks: [], cover_file: null,
            meta: { ALBUM: folderName(p) },
          } as unknown as Album & { owned?: boolean },
          artistName: "",
        };
      }),
    [favs, albums]
  );

  if (isLoading) return <PageLoading />;
  if (isError) return <LoadFailed what="your favorite albums" error={error} onRetry={() => refetch()} />;
  if (libError) return <LoadFailed what="the library" error={libError} onRetry={refetchLib} />;
  if (!rows.length)
    return <EmptyState title="No favorite albums yet" hint="Heart an album on its page or in the library grid." />;

  // Same card layout AND same cover-size preference as the library grid
  // (shared AlbumCard, shared mlo.gridSize setting).
  const gridSize = (localStorage.getItem("mlo.gridSize") as "s" | "m" | "l" | null) ?? "m";
  return (
    <div>
      {selectMode && (
        <div className="flex items-center gap-2 pb-2">
          <SelectAllButton
            count={rows.length}
            noun="albums"
            all={rows.length > 0 && picked.length === rows.length}
            onSelectAll={() => rows.forEach((r) => !picked.includes(r.key) && onPick(r.key))}
            onClear={() => rows.forEach((r) => picked.includes(r.key) && onPick(r.key))}
          />
        </div>
      )}
      <div
        className="grid gap-x-4 gap-y-5 stagger"
        style={{ gridTemplateColumns: `repeat(auto-fill, minmax(${GRID_SIZE_MIN[gridSize] ?? 164}px, 1fr))` }}
      >
        {rows.map(({ key, al, artistName }) => (
          <AlbumCard
            key={key}
            al={al}
            artistName={artistName}
            /* A row the library does not hold has no page to open. */
            href={al.owned === false ? null : undefined}
            selectable={selectMode}
            selected={picked.includes(key)}
            onSelect={onPick}
          />
        ))}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------------ //
// Favorite artists
// ------------------------------------------------------------------------ //
function FavArtists({ selectMode, picked, onPick }: TabSelectProps) {
  const { data: favs, isLoading, isError, error, refetch } = useFavorites();
  const { artists, libError, refetchLib } = useLibraryMaps();
  const playNow = useStore((s) => s.playNow);

  // Every favourite, resolved or not: an artist the library cannot resolve
  // (the folder was removed or renamed) stays a row — named by the folder it
  // was favourited under, with no counts to show — so it can be seen and taken
  // off here instead of silently vanishing from the page that owns it.
  const rows = useMemo(
    () =>
      (favs?.artists ?? []).map((p) => {
        const hit = artists.get(p);
        return hit
          ? { key: p, a: hit as Artist | null, name: "" }
          : { key: p, a: null, name: folderName(p) };
      }),
    [favs, artists]
  );

  if (isLoading) return <PageLoading />;
  if (isError) return <LoadFailed what="your favorite artists" error={error} onRetry={() => refetch()} />;
  if (libError) return <LoadFailed what="the library" error={libError} onRetry={refetchLib} />;
  if (!rows.length) return <EmptyState title="No favorite artists yet" hint="Heart an artist on their page." />;

  // Same table language as the library's artist view (Artist / Albums /
  // Tracks columns, same cell classes); play + heart ride in the Artist
  // cell as hover affordances, exactly like hearts in the track table.
  return (
    <div>
      {selectMode && (
        <div className="flex items-center gap-2 pb-2">
          <SelectAllButton
            count={rows.length}
            noun="artists"
            all={rows.length > 0 && picked.length === rows.length}
            onSelectAll={() => rows.forEach((r) => !picked.includes(r.key) && onPick(r.key))}
            onClear={() => rows.forEach((r) => picked.includes(r.key) && onPick(r.key))}
          />
        </div>
      )}
    <div className="overflow-x-auto">
      <table className={`w-full text-sm ${FAV_TABLE_MIN_W}`}>
        <thead className="border-b border-border">
          <tr>
            {selectMode && <th className="th pr-0 w-8" aria-label="Select" />}
            <th className="th">Artist</th>
            {/* a phone-width 12% is ~46 px — too narrow for a count */}
            {/* "Releases" — the Library's own Artists column has spelled an
                artist's albums this way since it was added (see ARTIST_COLS),
                and the same count under two names was the owner's report. */}
            <th className="th w-16 md:w-[12%]">Releases</th>
            <th className="th w-16 md:w-[12%]">Tracks</th>
          </tr>
        </thead>
        <tbody className="stagger">
          {rows.map(({ key, a, name: folder }) => {
            if (!a) {
              // The library cannot resolve this favourite any more: the row
              // says so, keeps the folder's own name, and the heart is the way
              // off (the same control the resolved rows carry).
              return (
                <tr key={key} className={`table-row group ${picked.includes(key) ? "bg-accent/10" : ""}`}
                    title="This artist is no longer in the library — remove it from your favorites with the heart">
                  {selectMode && (
                    <td className="td pr-0" onClick={(e) => e.stopPropagation()}>
                      <input type="checkbox" checked={picked.includes(key)} onChange={() => onPick(key)} title="Select artist" />
                    </td>
                  )}
                  <td className="td" colSpan={2}>
                    <div className="flex items-center gap-1.5 min-w-0">
                      <span className="font-medium break-words flex-1 min-w-0">{folder}</span>
                      <span className="chip bg-amber-950/40 text-amber-300/90 border border-amber-900/50 text-[10px] shrink-0">
                        not in the library
                      </span>
                      <span className="shrink-0">
                        <FavHeart kind="artist" id={key} iconClass="h-3.5 w-3.5" revealOnHover />
                      </span>
                    </div>
                  </td>
                </tr>
              );
            }
            const displayName =
              a.display_name ||
              a.albums.find((al) => al.album_artist)?.album_artist ||
              a.name.replace(/\s*\[[0-9a-f-]{8,}\]\s*$/, "");
            const q = a.albums.flatMap((al) =>
              al.tracks.map((t) => ({
                path: t.path, file: t.file, albumPath: al.path,
                artist: displayArtist(al, a), album: al.meta?.ALBUM ?? undefined, title: t.tags.TITLE || undefined,
                coverFile: t.cover_file ?? null, albumCover: al.cover_file ?? null,
              }))
            );
            return (
              <tr
                key={a.path}
                className={`table-row group ${selectMode ? "" : ""} ${picked.includes(key) ? "bg-accent/10" : ""}`}
                onClick={selectMode ? () => onPick(key) : undefined}
                title={selectMode ? "Click to select" : undefined}
              >
                {selectMode && (
                  <td className="td pr-0" onClick={(e) => e.stopPropagation()}>
                    <input type="checkbox" checked={picked.includes(key)} onChange={() => onPick(key)} title="Select artist" />
                  </td>
                )}
                <td className="td">
                  <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 min-w-0">
                    <button
                      className="btn-ghost !px-1.5 !py-1 shrink-0 row-hover min-h-[2rem] md:min-h-0"
                      title="Play all"
                      onClick={(e) => {
                        if (selectMode) {
                          e.stopPropagation();
                          onPick(key);
                          return;
                        }
                        if (q.length) playNow(q);
                      }}
                    >
                      <Play className="h-3.5 w-3.5" />
                    </button>
                    {/* The shared artist-name render: the same dot the
                        Library's artist view and the artist page draw, off
                        the same `grade` the library row carries. */}
                    <ArtistName
                      to={artistRef(a)}
                      name={displayName}
                      pass={a.grade?.pass}
                      issues={a.grade?.issues}
                      disambiguation={a.disambiguation}
                      className="font-medium hover:text-accent-soft flex-1 min-w-0"
                      nameClassName="break-words"
                    />
                    <span className="shrink-0">
                      <FavHeart kind="artist" id={a.path} mbid={artistMbid(a)} iconClass="h-3.5 w-3.5" revealOnHover />
                    </span>
                  </div>
                </td>
                <td className="td text-zinc-500">{a.aggregate.album_count}</td>
                <td className="td text-zinc-500">{a.aggregate.track_count}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
    </div>
  );
}

// ------------------------------------------------------------------------ //
// Favorite playlists
// ------------------------------------------------------------------------ //
function FavPlaylists({ selectMode, picked, onPick }: TabSelectProps) {
  const { data: favs, isLoading, isError, error, refetch } = useFavorites();
  const {
    data: playlists,
    isError: playlistsFailed,
    error: playlistsError,
    refetch: refetchPlaylists,
  } = useQuery({ queryKey: ["playlists"], queryFn: api.playlists });
  const { tracks } = useLibraryMaps();
  const playNow = useStore((s) => s.playNow);

  // Every favourite id, resolved or not: a playlist that is gone (deleted, or
  // another server's) stays a row — it says which playlist it was and carries
  // the heart that takes it off — instead of disappearing from the one page
  // whose job is to manage it.
  const rows = useMemo(() => {
    const byId = new Map<string, Playlist>((playlists ?? []).map((p) => [String(p.id), p]));
    return (favs?.playlists ?? []).map((id) => ({
      key: String(id),
      p: byId.get(String(id)) ?? null,
    }));
  }, [favs, playlists]);

  const play = async (p: Playlist) => {
    try {
      const detail = await api.playlist(p.id);
      const q = (detail.tracks ?? []).map((path) => {
        const hit = tracks.get(path);
        return hit
          ? {
              path,
              file: hit.track.file,
              albumPath: hit.album.path,
              artist: displayArtist(hit.album, hit.artist),
              album: hit.album.meta?.ALBUM ?? undefined,
              title: hit.track.tags.TITLE || undefined,
              coverFile: hit.track.cover_file ?? null,
              albumCover: hit.album.cover_file ?? null,
            }
          : { path, file: path.split("/").pop() ?? path, albumPath: path.split("/").slice(0, -1).join("/") };
      });
      if (q.length) playNow(q);
    } catch (e) {
      toast.error(String(e));
    }
  };

  if (isLoading) return <PageLoading />;
  // The rows are a JOIN: the favorite ids come from /api/favorites and the
  // names from /api/playlists, so a failure in either one is an empty table
  // for a reason that is not "you have none".
  if (isError) return <LoadFailed what="your favorite playlists" error={error} onRetry={() => refetch()} />;
  if (playlistsFailed)
    return <LoadFailed what="the playlists" error={playlistsError} onRetry={() => refetchPlaylists()} />;
  if (!rows.length) return <EmptyState title="No favorite playlists yet" hint="Heart a playlist on the Playlists page." />;

  // Same table language as the other favorites tabs / the library tables.
  return (
    <div>
      {selectMode && (
        <div className="flex items-center gap-2 pb-2">
          <SelectAllButton
            count={rows.length}
            noun="playlists"
            all={rows.length > 0 && picked.length === rows.length}
            onSelectAll={() => rows.forEach((r) => !picked.includes(r.key) && onPick(r.key))}
            onClear={() => rows.forEach((r) => picked.includes(r.key) && onPick(r.key))}
          />
        </div>
      )}
    <div className="overflow-x-auto">
      <table className={`w-full text-sm ${FAV_TABLE_MIN_W}`}>
        <thead className="border-b border-border">
          <tr>
            {selectMode && <th className="th pr-0 w-8" aria-label="Select" />}
            <th className="th">Playlist</th>
            <th className="th w-16 md:w-[12%]">Tracks</th>
          </tr>
        </thead>
        <tbody className="stagger">
          {rows.map(({ key, p }) => {
            if (!p) {
              return (
                <tr key={key} className={`table-row group ${picked.includes(key) ? "bg-accent/10" : ""}`}>
                  {selectMode && (
                    <td className="td pr-0" onClick={(e) => e.stopPropagation()}>
                      <input type="checkbox" checked={picked.includes(key)} onChange={() => onPick(key)} title="Select playlist" />
                    </td>
                  )}
                  <td className="td" colSpan={2}>
                    <div className="flex items-center gap-1.5 min-w-0">
                      <span className="font-medium flex-1 min-w-0">Playlist #{key}</span>
                      <span className="chip bg-amber-950/40 text-amber-300/90 border border-amber-900/50 text-[10px] shrink-0">
                        not in this library
                      </span>
                      <span className="shrink-0">
                        <FavHeart kind="playlist" id={key} iconClass="h-3.5 w-3.5" revealOnHover />
                      </span>
                    </div>
                  </td>
                </tr>
              );
            }
            return (
            <tr
              key={p.id}
              className={`table-row group ${picked.includes(key) ? "bg-accent/10" : ""}`}
              onClick={selectMode ? () => onPick(key) : undefined}
              title={selectMode ? "Click to select" : undefined}
            >
              {selectMode && (
                <td className="td pr-0" onClick={(e) => e.stopPropagation()}>
                  <input type="checkbox" checked={picked.includes(key)} onChange={() => onPick(key)} title="Select playlist" />
                </td>
              )}
              <td className="td">
                <div className="flex items-center gap-1.5 min-w-0">
                  <button
                    className="btn-ghost !px-1.5 !py-1 shrink-0 row-hover min-h-[2rem] md:min-h-0"
                    title="Play playlist"
                    onClick={() => play(p)}
                  >
                    <Play className="h-3.5 w-3.5" />
                  </button>
                  {/* The playlist's OWN page (`/playlist/:id`), which is where
                      its tracks, rules and rename live — this used to send
                      every row to the Playlists index instead. */}
                  <Link
                    to={`/playlist/${p.id}`}
                    className="font-medium hover:text-accent-soft break-words flex-1 min-w-0 text-left"
                    title="Open the playlist page"
                  >
                    {p.name}
                  </Link>
                  {p.kind === "smart" && <span className="chip bg-accent/10 text-accent-soft border border-accent/25 text-[10px] shrink-0">SMART</span>}
                  <span className="shrink-0">
                    <FavHeart kind="playlist" id={String(p.id)} iconClass="h-3.5 w-3.5" revealOnHover />
                  </span>
                </div>
              </td>
              <td className="td text-zinc-500">{p.track_count}</td>
            </tr>
            );
          })}
        </tbody>
      </table>
      <div className="text-[11px] text-zinc-600 px-3 pt-2">
        Playlists are managed on the <Link to="/playlists" className="text-accent-soft hover:underline">Playlists page</Link>.
      </div>
    </div>
    </div>
  );
}
