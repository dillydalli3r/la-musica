import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Disc3, FileVideo, Heart, ListMusic, Mic2, Play } from "lucide-react";import { api } from "../api";
import { useStore } from "../store";
import { toast } from "../store";
import { useFavorites, useTrackLikes } from "../lib/favs";
import { AdvisoryMark, CachedMark, EmptyState, PageLoading } from "../components/Badges";
import PageHeader from "../components/PageHeader";
import { TrackCover } from "../components/CoverImg";
import AlbumCard from "../components/AlbumCard";
import DownloadButton from "../components/DownloadButton";
import { ExportButton, usePlaylistTracks } from "../components/ExportDialog";
import MoreLikeThis from "../components/MoreLikeThis";
import FavHeart from "../components/FavHeart";
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

export default function FavoritesPage() {
  const { kind: raw } = useParams();
  const navigate = useNavigate();
  const kind: Kind = (TABS.some((t) => t.id === raw) ? raw : "tracks") as Kind;

  const { artists, albums, tracks } = useLibraryMaps();
  const { data: likes } = useTrackLikes();
  const { data: favs } = useFavorites();

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

  return (
    <div className="p-6 space-y-5 mx-auto max-w-6xl">
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
            {/* both act on the tab in view — a like list is the set this page
                represents */}
            <DownloadButton
              paths={favPaths}
              label={kind === "tracks" ? "Download likes" : "Download all"}
              emptyReason="Nothing to download — this favorites tab is empty"
            />
            <ExportButton
              paths={favPaths}
              seconds={favSeconds}
              label={kind === "tracks" ? "Export likes" : "Export all"}
              emptyReason="Nothing to export — this favorites tab is empty"
              title="Export these favorites to a drive"
              dialogSubtitle={`${favPaths.length} track${favPaths.length === 1 ? "" : "s"} from your favorites`}
            />
          </>
        }
      />
      {kind === "tracks" && <LikedTracks />}
      {/* the shelf renders null while it has nothing to suggest, so it costs
          the tabs without a recommendation nothing */}
      {kind === "tracks" && <MoreLikeThis kind="favorites" target="tracks" title="Recommended tracks" />}
      {kind === "albums" && <FavAlbums />}
      {kind === "artists" && <FavArtists />}
      {(kind === "albums" || kind === "artists") && (
        <MoreLikeThis kind="favorites" target="albums" title="Recommended albums" />
      )}
      {kind === "playlists" && <FavPlaylists />}
    </div>
  );
}

/** Flat library lookups shared by every tab. */
function useLibraryMaps() {
  const { data: lib } = useQuery({ queryKey: ["library"], queryFn: api.library });
  return useMemo(() => {
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

function LikedTracks() {
  const { data: likes, isLoading } = useTrackLikes();
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
  if (!rows.length)
    return (
      <EmptyState
        title="No liked tracks yet"
        hint="Use the heart in the player bar or on any track row — liked tracks show up here."
      />
    );

  return (
    <div>
      <div className="flex items-center gap-2 pb-2">
        <span className="text-xs text-zinc-500">
          {rows.length} liked track{rows.length === 1 ? "" : "s"}
        </span>
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
                className="table-row group cursor-pointer"
                title="Click to play · Ctrl-click to open track page"
                onClick={(e) => {
                  if (!r.missing && (e.ctrlKey || e.metaKey || e.shiftKey)) {
                    navigate(`/track/${encodeURIComponent(r.trackPath)}`);
                  } else if (!r.missing) {
                    play(i);
                  }
                }}
              >
                {likedCols.includes("num") && <td className={`td cell-nowrap text-zinc-600 tabular-nums${PHONE_HIDE}`}>{i + 1}</td>}
                {likedCols.includes("cover") && (
                <td className="td cell-cover pr-0">
                  {"coverFile" in r ? (
                    <TrackCover
                      albumPath={r.albumPath}
                      trackCover={r.coverFile}
                      albumCover={r.albumCover}
                      wrapperClass="h-9 w-9 rounded bg-raise border border-border overflow-hidden shrink-0"
                    />
                  ) : null}
                </td>
                )}
                {likedCols.includes("title") && (
                <td className="td">
                  <div className="flex items-center gap-1.5 min-w-0">
                    <span className={`break-words flex-1 min-w-0 ${r.missing ? "text-zinc-500" : "hover:text-accent-soft"}`} title={r.missing ? r.path : r.title}>
                      {r.title}
                    </span>
                    {!r.missing && <AdvisoryMark value={r.advisory} />}
                    <CachedMark path={r.path} />
                    {!r.missing && r.isVideo && (
                      <span title="Music video" className="shrink-0 inline-flex"><FileVideo className="h-3.5 w-3.5 text-zinc-500" /></span>
                    )}
                    <span className="row-hover shrink-0" onClick={(e) => e.stopPropagation()}>
                      <FavHeart kind="track" id={r.path} mbid={r.mbid} iconClass="h-3.5 w-3.5" title="Unlike" />
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
function FavAlbums() {
  const { data: favs, isLoading } = useFavorites();
  const { albums } = useLibraryMaps();

  const rows = useMemo(
    () =>
      (favs?.albums ?? [])
        .map((p) => albums.get(p))
        .filter((x): x is NonNullable<typeof x> => !!x),
    [favs, albums]
  );

  if (isLoading) return <PageLoading />;
  if (!rows.length)
    return <EmptyState title="No favorite albums yet" hint="Heart an album on its page or in the library grid." />;

  // Same card layout AND same cover-size preference as the library grid
  // (shared AlbumCard, shared mlo.gridSize setting).
  const gridSize = (localStorage.getItem("mlo.gridSize") as "s" | "m" | "l" | null) ?? "m";
  return (
    <div
      className="grid gap-x-4 gap-y-5 stagger"
      style={{ gridTemplateColumns: `repeat(auto-fill, minmax(${GRID_SIZE_MIN[gridSize] ?? 164}px, 1fr))` }}
    >
      {rows.map(({ album: al, artist: a }) => (
        <AlbumCard key={al.path} al={al} artistName={displayArtist(al, a)} />
      ))}
    </div>
  );
}

// ------------------------------------------------------------------------ //
// Favorite artists
// ------------------------------------------------------------------------ //
function FavArtists() {
  const { data: favs, isLoading } = useFavorites();
  const { artists } = useLibraryMaps();
  const playNow = useStore((s) => s.playNow);

  const rows = useMemo(
    () =>
      (favs?.artists ?? [])
        .map((p) => artists.get(p))
        .filter((x): x is Artist => !!x),
    [favs, artists]
  );

  if (isLoading) return <PageLoading />;
  if (!rows.length) return <EmptyState title="No favorite artists yet" hint="Heart an artist on their page." />;

  // Same table language as the library's artist view (Artist / Albums /
  // Tracks columns, same cell classes); play + heart ride in the Artist
  // cell as hover affordances, exactly like hearts in the track table.
  return (
    <div className="overflow-x-auto">
      <table className={`w-full text-sm ${FAV_TABLE_MIN_W}`}>
        <thead className="border-b border-border">
          <tr>
            <th className="th">Artist</th>
            {/* a phone-width 12% is ~46 px — too narrow for a count */}
            <th className="th w-16 md:w-[12%]">Albums</th>
            <th className="th w-16 md:w-[12%]">Tracks</th>
          </tr>
        </thead>
        <tbody className="stagger">
          {rows.map((a) => {
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
              <tr key={a.path} className="table-row group">
                <td className="td">
                  <div className="flex items-center gap-1.5 min-w-0">
                    <button
                      className="btn-ghost !px-1.5 !py-1 shrink-0 row-hover min-h-[2rem] md:min-h-0"
                      title="Play all"
                      onClick={() => q.length && playNow(q)}
                    >
                      <Play className="h-3.5 w-3.5" />
                    </button>
                    <Link to={artistRef(a)} className="font-medium hover:text-accent-soft break-words flex-1 min-w-0">
                      {displayName}
                    </Link>
                    <span className="row-hover shrink-0">
                      <FavHeart kind="artist" id={a.path} mbid={artistMbid(a)} iconClass="h-3.5 w-3.5" />
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
  );
}

// ------------------------------------------------------------------------ //
// Favorite playlists
// ------------------------------------------------------------------------ //
function FavPlaylists() {
  const { data: favs, isLoading } = useFavorites();
  const { data: playlists } = useQuery({ queryKey: ["playlists"], queryFn: api.playlists });
  const { tracks } = useLibraryMaps();
  const playNow = useStore((s) => s.playNow);
  const navigate = useNavigate();

  const rows = useMemo(() => {
    const byId = new Map<string, Playlist>((playlists ?? []).map((p) => [String(p.id), p]));
    return (favs?.playlists ?? [])
      .map((id) => byId.get(id))
      .filter((x): x is Playlist => !!x);
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
  if (!rows.length) return <EmptyState title="No favorite playlists yet" hint="Heart a playlist on the Playlists page." />;

  // Same table language as the other favorites tabs / the library tables.
  return (
    <div className="overflow-x-auto">
      <table className={`w-full text-sm ${FAV_TABLE_MIN_W}`}>
        <thead className="border-b border-border">
          <tr>
            <th className="th">Playlist</th>
            <th className="th w-16 md:w-[12%]">Tracks</th>
          </tr>
        </thead>
        <tbody className="stagger">
          {rows.map((p) => (
            <tr key={p.id} className="table-row group">
              <td className="td">
                <div className="flex items-center gap-1.5 min-w-0">
                  <button
                    className="btn-ghost !px-1.5 !py-1 shrink-0 row-hover min-h-[2rem] md:min-h-0"
                    title="Play playlist"
                    onClick={() => play(p)}
                  >
                    <Play className="h-3.5 w-3.5" />
                  </button>
                  <button
                    className="font-medium hover:text-accent-soft break-words flex-1 min-w-0 text-left"
                    onClick={() => navigate("/playlists")}
                    title="Open the Playlists page"
                  >
                    {p.name}
                  </button>
                  {p.kind === "smart" && <span className="chip bg-accent/10 text-accent-soft border border-accent/25 text-[10px] shrink-0">SMART</span>}
                  <span className="row-hover shrink-0">
                    <FavHeart kind="playlist" id={String(p.id)} iconClass="h-3.5 w-3.5" />
                  </span>
                </div>
              </td>
              <td className="td text-zinc-500">{p.track_count}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="text-[11px] text-zinc-600 px-3 pt-2">
        Playlists are managed on the <Link to="/playlists" className="text-accent-soft hover:underline">Playlists page</Link>.
      </div>
    </div>
  );
}
