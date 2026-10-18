import { useQuery } from "@tanstack/react-query";
import { useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  BarChart3, Clock, Disc3, Flame, Headphones, Heart, ListChecks, ListMusic,
  Moon, Music2, Play, Radio, RefreshCw, Sparkles, Star, Tags,
  TrendingUp, Users, Waves,
} from "lucide-react";
import { api } from "../api";
import { EmptyState, PageLoading } from "../components/Badges";
import StorageCard from "../components/StorageCard";
import PageHeader from "../components/PageHeader";
import AlbumCard, { GRID_COVER_W } from "../components/AlbumCard";
import CoverImg from "../components/CoverImg";
import Segmented from "../components/Segmented";
import SelectAllButton from "../components/SelectAllButton";
import StatsPanel from "../components/StatsPanel";
import StarRating from "../components/StarRating";
import ArtistAvatar from "../components/ArtistAvatar";
import ArtistName from "../components/ArtistName";
import { useI18n } from "../lib/i18n";
import { GRID_SIZE_MIN, fmtDuration } from "../lib/fmt";
import { albumRef } from "../lib/refs";
import { GRID_SIZES, useGridSize, useSelectMode } from "../lib/libraryView";
import { useStore } from "../store";
import type { ReactNode } from "react";
import type {
  HomeAlbum, HomeData, HomeLane, HomePlayedArtist,
  HomePlaylistRow, HomePodcast, HomeTrack, Track,
} from "../types";

/** Shelf chip: why a row is here. */
function Chip({ text, title }: { text: string; title?: string }) {
  return (
    <span
      className="truncate rounded-full px-1.5 py-0.5 text-[10px] leading-tight bg-accent/10 text-accent-soft"
      title={title ?? text}
    >
      {text}
    </span>
  );
}

function Shelf<T extends HomeAlbum>({
  title, icon: Icon, items, blurb, extraOf, gridSize, selectable, selected,
  onSelect,
}: {
  title: string;
  icon: typeof Sparkles;
  items: T[];
  blurb?: string;
  extraOf?: (row: T) => ReactNode;
  gridSize: "s" | "m" | "l";
  selectable: boolean;
  selected: string[];
  onSelect: (path: string) => void;
}) {
  if (!items.length) return null;
  return (
    <section className="space-y-2">
      <div className="flex items-baseline gap-2 px-1">
        <Icon className="h-4 w-4 text-accent self-center" />
        <h2 className="text-sm font-semibold tracking-tight">{title}</h2>
        {blurb && <span className="text-[11px] text-zinc-600">{blurb}</span>}
      </div>
      <div
        className="grid gap-x-4 gap-y-5 stagger"
        style={{ gridTemplateColumns: `repeat(auto-fill, minmax(${GRID_SIZE_MIN[gridSize]}px, 1fr))` }}
      >
        {items.map((a, i) => {
          const inLibrary = a.owned !== false && !!a.path;
          return (
            <AlbumCard
              key={a.path || a.mbid || `shelf-${i}`}
              al={a}
              href={inLibrary ? albumRef(a) : null}
              selectable={selectable && inLibrary}
              selected={selected.includes(a.path)}
              onSelect={onSelect}
              extraMeta={extraOf ? extraOf(a) : a.reason ? <Chip text={a.reason} /> : undefined}
              size={gridSize}
            />
          );
        })}
      </div>
    </section>
  );
}

/** A genre or mood lane: the name and a few records from it, on one row.
 *  Deliberately NOT the full grid: Home has several lanes, and a page that
 *  stacks seven album grids is a page you scroll, not a page you read. */
function Lane({ lane, icon: Icon }: { lane: HomeLane; icon: typeof Flame }) {
  if (!lane.albums.length) return null;
  return (
    <section className="space-y-2">
      <div className="flex items-baseline gap-2 px-1">
        <Icon className="h-4 w-4 text-accent self-center" />
        <h2 className="text-sm font-semibold tracking-tight">{lane.name}</h2>
        <span className="text-[11px] text-zinc-600">
          {lane.tracks.toLocaleString()} track{lane.tracks === 1 ? "" : "s"}
          {lane.plays ? ` · ${lane.plays.toLocaleString()} play${lane.plays === 1 ? "" : "s"}` : ""}
        </span>
      </div>
      <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-6 xl:grid-cols-8 gap-x-3 gap-y-4 stagger">
        {lane.albums.slice(0, 8).map((a) => (
          <Link key={a.path} to={albumRef(a)} className="group min-w-0">
            <CoverImg
              albumPath={a.path}
              coverFile={a.cover_file}
              w={GRID_COVER_W.s}
              wrapperClass="aspect-square w-full rounded-lg bg-raise overflow-hidden"
            />
            <div className="mt-1.5 px-0.5 text-xs font-medium truncate">{a.meta?.ALBUM || a.path.split("/").pop()}</div>
            <div className="px-0.5 text-[11px] text-zinc-500 truncate">
              {a.album_artist || a.meta?.ALBUMARTIST || a.meta?.ARTIST || ""}
            </div>
          </Link>
        ))}
      </div>
    </section>
  );
}

/** A track row: click to play, the title opens the track's page. */
function TrackList({ title, icon: Icon, rows, blurb }: {
  title: string; icon: typeof Music2; rows: HomeTrack[]; blurb?: string;
}) {
  const playNow = useStore((s) => s.playNow);
  if (!rows.length) return null;
  const queue = rows.map((r) => ({
    path: r.path,
    file: r.file || r.path.split("/").pop() || "",
    albumPath: r.album_path,
    artist: r.artist,
    album: r.album,
    title: r.title,
    coverFile: r.cover_file ?? null,
    albumCover: r.album_cover ?? null,
  }));
  return (
    <section className="space-y-2">
      <div className="flex items-baseline gap-2 px-1">
        <Icon className="h-4 w-4 text-accent self-center" />
        <h2 className="text-sm font-semibold tracking-tight">{title}</h2>
        {blurb && <span className="text-[11px] text-zinc-600">{blurb}</span>}
      </div>
      <div className="panel divide-y divide-border">
        {rows.map((r, i) => (
          <div key={r.path} className="group flex items-center gap-3 px-3 py-1.5 hover:bg-panel/70">
            <span className="w-5 text-right text-[11px] text-zinc-600 tabular-nums">{i + 1}</span>
            <button
              className="btn-icon hover:bg-accent/20 tap shrink-0"
              title="Play"
              onClick={() => playNow(queue, i)}
            >
              <Play className="h-3.5 w-3.5 fill-current" />
            </button>
            <CoverImg
              albumPath={r.album_path}
              coverFile={r.cover_file ?? r.album_cover}
              w={80}
              wrapperClass="h-9 w-9 rounded bg-raise overflow-hidden shrink-0"
            />
            <div className="min-w-0 flex-1">
              <Link
                to={`/track/${encodeURIComponent(r.path)}`}
                className="block truncate text-sm font-medium hover:underline"
                title={r.title}
              >
                {r.title}
              </Link>
              <div className="truncate text-[11px] text-zinc-500">
                {r.artist}
                {r.album && <span className="text-zinc-600"> · {r.album}</span>}
              </div>
            </div>
            {r.rating != null && r.rating > 0 && (
              <span className="shrink-0 hidden sm:block" title={`Your rating: ${(r.rating / 2).toFixed(1)} / 5`}>
                <StarRating value={r.rating / 2} readOnly size="sm" />
              </span>
            )}
            {r.plays > 0 && (
              <span className="shrink-0 text-[11px] text-zinc-500 tabular-nums" title={`${r.plays} play(s)`}>
                <Headphones className="inline h-3 w-3" /> {r.plays}
              </span>
            )}
            {r.duration ? (
              <span className="shrink-0 text-[11px] text-zinc-600 tabular-nums">{fmtDuration(r.duration)}</span>
            ) : null}
          </div>
        ))}
      </div>
    </section>
  );
}

function PlayedArtists({ title, artists }: { title: string; artists?: HomePlayedArtist[] }) {
  if (!artists?.length) return null;
  return (
    <section className="space-y-2">
      <div className="flex items-baseline gap-2 px-1">
        <Users className="h-4 w-4 text-accent self-center" />
        <h2 className="text-sm font-semibold tracking-tight">{title}</h2>
        <span className="text-[11px] text-zinc-600">ranked by plays</span>
      </div>
      <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-6 xl:grid-cols-8 gap-2 stagger">
        {artists.map((ar) => (
          <Link
            key={ar.path || ar.artist}
            to={`/artist/${encodeURIComponent(ar.path)}`}
            className="group rounded-lg p-1.5 flex flex-col items-center text-center transition-all duration-200 hover:bg-panel/70 hover:-translate-y-0.5"
            title={`${ar.artist} · ${ar.plays} play(s)`}
          >
            <ArtistAvatar
              path={ar.path}
              hasImage={ar.has_image}
              coverPath={ar.cover_path}
              coverFile={ar.cover}
              className="h-16 w-16 rounded-lg bg-raise overflow-hidden shrink-0"
            />
            <ArtistName
              name={ar.artist}
              pass={ar.grade?.pass}
              issues={ar.grade?.issues}
              disambiguation={ar.disambiguation}
              className="mt-1.5 w-full justify-center text-xs font-medium"
              nameClassName="truncate"
            />
            <div className="text-[10px] text-zinc-500 tabular-nums flex items-center gap-1">
              <Headphones className="h-3 w-3" /> {ar.plays.toLocaleString()}
              {ar.rating != null && ar.rating > 0 && <span> · ★ {(ar.rating / 2).toFixed(1)}</span>}
            </div>
          </Link>
        ))}
      </div>
    </section>
  );
}

function PlaylistRows({ title, rows }: { title: string; rows: HomePlaylistRow[] }) {
  if (!rows.length) return null;
  return (
    <section className="space-y-2">
      <div className="flex items-baseline gap-2 px-1">
        <ListMusic className="h-4 w-4 text-accent self-center" />
        <h2 className="text-sm font-semibold tracking-tight">{title}</h2>
        <span className="text-[11px] text-zinc-600">where your plays came from</span>
      </div>
      <div className="panel divide-y divide-border">
        {rows.map((p) => (
          <Link key={p.id} to={p.path} className="flex items-center gap-3 px-3 py-2 hover:bg-panel/70">
            <ListFilterIcon smart={p.kind_of === "smart"} />
            <span className="min-w-0 flex-1 truncate text-sm font-medium">{p.name}</span>
            <span className="text-[11px] text-zinc-500 tabular-nums">
              {p.track_count ?? 0} track{p.track_count === 1 ? "" : "s"}
            </span>
            <span className="text-[11px] text-accent-soft tabular-nums flex items-center gap-1">
              <Headphones className="h-3 w-3" /> {p.plays.toLocaleString()}
            </span>
          </Link>
        ))}
      </div>
    </section>
  );
}

function ListFilterIcon({ smart }: { smart: boolean }) {
  return smart
    ? <Waves className="h-4 w-4 text-accent shrink-0" />
    : <ListMusic className="h-4 w-4 text-zinc-500 shrink-0" />;
}

function PodcastShelf({ title, items, gridSize }: {
  title: string; items?: HomePodcast[]; gridSize: "s" | "m" | "l";
}) {
  const { t } = useI18n();
  if (!items?.length) return null;
  return (
    <section className="space-y-2">
      <div className="flex items-baseline gap-2 px-1">
        <Radio className="h-4 w-4 text-accent self-center" />
        <h2 className="text-sm font-semibold tracking-tight">{title}</h2>
        <span className="text-[11px] text-zinc-600">{t("home.shelf.podcasts_blurb")}</span>
      </div>
      <div
        className="grid gap-x-4 gap-y-5 stagger"
        style={{ gridTemplateColumns: `repeat(auto-fill, minmax(${GRID_SIZE_MIN[gridSize]}px, 1fr))` }}
      >
        {items.map((row) => {
          const newest = row.meta?.ALBUM || "";
          const date = row.meta?.DATE || row.meta?.ORIGINALDATE || "";
          return (
            <Link
              key={row.podcast_series}
              to={`/podcast/${encodeURIComponent(row.podcast_series)}`}
              className="group rounded-xl p-1 transition-all duration-200 hover:bg-panel/70"
              title={`${row.podcast_series} — ${t("podcast.episodes")} ${row.podcast_episode_count}; ${t("podcast.newest_episode")}: ${newest}`}
            >
              <CoverImg
                albumPath={row.path}
                coverFile={row.cover_file}
                w={GRID_COVER_W[gridSize]}
                wrapperClass="aspect-square w-full rounded-lg bg-raise overflow-hidden"
              />
              <div className="mt-2 px-1 text-sm font-medium truncate w-full">{row.podcast_series}</div>
              <div className="px-1 text-[11px] text-zinc-400 truncate w-full" title={newest}>{newest}</div>
              <div className="px-1 text-[11px] text-zinc-600 tabular-nums truncate w-full">
                {row.podcast_episode_count} {t("podcast.episodes")}
                {date && <span> · {date}</span>}
              </div>
            </Link>
          );
        })}
      </div>
    </section>
  );
}

/** The section header Home's groups share: a plain, quiet label. */
function SectionTitle({ children }: { children: ReactNode }) {
  return (
    <h2 className="text-[10px] uppercase tracking-[0.14em] text-zinc-600 px-1 pt-2">{children}</h2>
  );
}

export default function HomePage() {
  const { t } = useI18n();
  const forceRefresh = useRef(false);
  const { data, isLoading, isError, isFetching, refetch } = useQuery<HomeData>({
    queryKey: ["home"],
    queryFn: () => {
      const force = forceRefresh.current;
      forceRefresh.current = false;
      return api.home(force);
    },
    staleTime: 60_000,
    refetchInterval: 60_000,
  });
  const refresh = () => {
    forceRefresh.current = true;
    void refetch();
  };
  const [gridSize, pickGridSize] = useGridSize();
  const { selectMode, toggleSelectMode } = useSelectMode();
  const [statsOpen, setStatsOpen] = useState(false);
  const selection = useStore((s) => s.selection);
  const toggleAlbum = useStore((s) => s.toggleAlbum);
  const setSelection = useStore((s) => s.setSelection);

  const shown = useMemo(() => {
    const albums: HomeAlbum[] = [];
    const tracks: Track[] = [];
    const seen = new Set<string>();
    for (const row of [
      ...(data?.recent ?? []), ...(data?.top_albums ?? []),
      ...(data?.rated_albums ?? []), ...(data?.rediscover ?? []),
      ...(data?.favorites ?? []), ...(data?.podcasts ?? []),
      ...(data?.genres ?? []).flatMap((g) => g.albums),
      ...(data?.moods ?? []).flatMap((g) => g.albums),
    ]) {
      const key = row.path || `mb:${row.mbid ?? ""}`;
      if (seen.has(key)) continue;
      seen.add(key);
      albums.push(row);
      tracks.push(...(row.tracks ?? []));
    }
    return { albums, tracks };
  }, [data]);

  const selectableShown = useMemo(
    () => shown.albums.filter((a) => a.owned !== false && !!a.path),
    [shown]
  );
  const allShownSelected =
    selectMode && selectableShown.length > 0 && selectableShown.every((a) => selection.albums.includes(a.path));

  if (isLoading) {
    return (
      <div className="p-6 mx-auto max-w-[1600px]">
        <PageLoading label={t("home.loading")} />
      </div>
    );
  }
  if (isError || !data) {
    return (
      <div className="p-6 mx-auto max-w-[1600px]">
        <EmptyState title={t("home.error_title")} hint={t("home.error_hint")} />
        <div className="flex justify-center">
          <button className="btn-ghost !py-1.5 text-xs tap" onClick={refresh} disabled={isFetching}
            title={t("home.refresh_title")}>
            <RefreshCw className={`h-3.5 w-3.5 ${isFetching ? "animate-spin" : ""}`} /> {t("home.refresh")}
          </button>
        </div>
      </div>
    );
  }

  const { stats } = data;
  const shelfProps = {
    gridSize,
    selectable: selectMode,
    selected: selection.albums,
    onSelect: toggleAlbum,
  };
  return (
    <div className="p-6 space-y-5 mx-auto max-w-[1600px]">
      <div className="panel-hero relative overflow-hidden bg-gradient-to-br from-panel to-bg">
        <div className="absolute -right-20 -top-20 h-64 w-64 rounded-full bg-accent/10 blur-3xl pointer-events-none" />
        <div className="relative">
          <PageHeader
            overline={t("home.welcome")}
            title={t("home.title")}
            subtitle={
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-zinc-400">
                <span><b className="text-zinc-100">{stats.artists}</b> {t("home.stat.artists")}</span>
                <span className="text-zinc-700">·</span>
                <span><b className="text-zinc-100">{stats.albums}</b> {t("home.stat.albums")}</span>
                <span className="text-zinc-700">·</span>
                <span><b className="text-zinc-100">{stats.tracks}</b> {t("home.stat.tracks")}</span>
                <span className="text-zinc-700">·</span>
                <span><b className="text-zinc-100">{stats.playlists}</b> {t("home.stat.playlists")}</span>
                <span className="text-zinc-700">·</span>
                <span><b className="text-zinc-100">{(stats.plays ?? 0).toLocaleString()}</b> plays</span>
              </div>
            }
            actions={
              <>
                <span title="Cover size">
                  <Segmented value={gridSize} onChange={pickGridSize} options={GRID_SIZES} />
                </span>
                <button className="btn-ghost !py-1.5 text-xs tap" onClick={() => setStatsOpen(true)}
                  title="Statistics for the albums on this page">
                  <BarChart3 className="h-3.5 w-3.5" /> Stats
                </button>
                <button
                  className={`btn-ghost !py-1.5 text-xs tap ${selectMode ? "!text-accent !border-accent/50" : ""}`}
                  onClick={toggleSelectMode}
                  title="Select mode — show checkboxes for batch actions"
                >
                  <ListChecks className="h-3.5 w-3.5" /> Select
                </button>
                {selectMode && (
                  <SelectAllButton
                    count={selectableShown.length}
                    noun="albums"
                    all={allShownSelected}
                    onSelectAll={() => setSelection({ albums: selectableShown.map((a) => a.path) })}
                    onClear={() => setSelection({ albums: [] })}
                  />
                )}
                <button className="btn-ghost !py-1.5 text-xs tap" onClick={refresh} disabled={isFetching}
                  title={t("home.refresh_title")}>
                  <RefreshCw className={`h-3.5 w-3.5 ${isFetching ? "animate-spin" : ""}`} /> {t("home.refresh")}
                </button>
              </>
            }
          />
        </div>
      </div>

      {selection.albums.length > 0 && (
        <div className="flex items-center gap-2 bg-accent/15 border border-accent/40 rounded-lg px-3 py-2 flex-wrap">
          <span className="text-xs font-medium text-accent-soft">
            {selection.albums.length} album{selection.albums.length === 1 ? "" : "s"} selected
          </span>
          <Link to="/library" className="ml-auto btn-ghost !py-1 text-xs tap">
            <ListChecks className="h-3.5 w-3.5" /> Act on them in the Library
          </Link>
        </div>
      )}

      {/* What you have been listening to — the page's own opening answer. */}
      <TrackList title="Most played tracks" icon={TrendingUp} rows={data.top_tracks}
        blurb="your own play history, all time" />
      <PlaylistRows title="Most played playlists" rows={data.top_playlists} />

      <Shelf title="Most played albums" icon={Headphones} items={data.top_albums} {...shelfProps} />
      {data.top_artists.length > 0 && <PlayedArtists title="Most played artists" artists={data.top_artists} />}

      {/* What it sounds like: genres and moods, each with a few records. */}
      <SectionTitle>By sound</SectionTitle>
      {data.genres.map((lane) => <Lane key={`g-${lane.name}`} lane={lane} icon={Tags} />)}
      {data.moods.map((lane) => <Lane key={`m-${lane.name}`} lane={lane} icon={Moon} />)}

      {/* The caller's own stars. */}
      <SectionTitle>Your stars</SectionTitle>
      <TrackList title="Rated tracks" icon={Star} rows={data.rated_tracks} blurb="highest first" />
      <Shelf title="Rated albums" icon={Star} items={data.rated_albums}
        extraOf={(a) => a.rating > 0
          ? <StarRating value={a.rating / 2} readOnly size="sm" />
          : undefined}
        {...shelfProps} />
      {data.rated_artists.length > 0 && <PlayedArtists title="Rated artists" artists={data.rated_artists} />}

      {/* The library itself, newest and favourite. */}
      <Shelf title={t("home.shelf.recent")} icon={Clock} items={data.recent} {...shelfProps} />
      <Shelf title={t("page.favorites")} icon={Heart} items={data.favorites} {...shelfProps} />
      <PodcastShelf title={t("home.shelf.podcasts")} items={data.podcasts} gridSize={gridSize} />
      <Shelf title={t("home.shelf.rediscover")} icon={Disc3} items={data.rediscover} {...shelfProps} />

      {/* How much room the library takes — the one maintenance fact that is
          also a LISTENER fact (what is safe to download next). */}
      <StorageCard />

      {statsOpen && (
        <StatsPanel
          title="the albums on this page"
          albums={shown.albums}
          tracks={shown.tracks}
          onClose={() => setStatsOpen(false)}
        />
      )}
    </div>
  );
}