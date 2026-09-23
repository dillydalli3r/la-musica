import { useQuery } from "@tanstack/react-query";
import { useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, ArrowDownUp, BarChart3, Clock, Disc3, Heart, ListChecks, Loader2, RefreshCw, Sparkles, Star, Users } from "lucide-react";
import { api } from "../api";
import { EmptyState, PageLoading } from "../components/Badges";
import StorageCard from "../components/StorageCard";
import GradeWarning from "../components/GradeWarning";
import PageHeader from "../components/PageHeader";
import AlbumCard from "../components/AlbumCard";
import Segmented from "../components/Segmented";
import StarRating from "../components/StarRating";
import StatsPanel from "../components/StatsPanel";
import { useI18n } from "../lib/i18n";
import CoverImg from "../components/CoverImg";
import { GRID_SIZE_MIN } from "../lib/fmt";
import { albumRef } from "../lib/refs";
import { GRID_SIZES, useGridSize, useSelectMode } from "../lib/libraryView";
import { toUi } from "../lib/ratings";
import { useStore } from "../store";
import type { ReactNode } from "react";
import type { HomeAlbum, HomeArtist, Track } from "../types";

/** Shelf chip: why a row is here (a wish's status, a favorite's origin). */
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
  title,
  icon: Icon,
  items,
  blurb,
  extraOf,
  gridSize,
  selectable,
  selected,
  onSelect,
}: {
  title: string;
  icon: typeof Sparkles;
  items: T[];
  blurb?: string;
  /** What each card carries under its caption. The shelf's own reason chip
   *  ("Recently added", "Rediscover") unless the shelf draws its line — the
   *  rated shelf shows the user's stars, which ARE its reason for a row. */
  extraOf?: (row: T) => ReactNode;
  /** The shared cover size (lib/libraryView) — Home's shelves and the
   *  Library's grid draw their covers at the one the user picked. */
  gridSize: "s" | "m" | "l";
  selectable: boolean;
  selected: string[];
  onSelect: (path: string) => void;
}) {
  // An empty shelf is hidden outright: the page-level empty state is Home's,
  // so a shelf never invents an empty look of its own.
  if (!items.length) return null;
  return (
    <section className="space-y-2">
      <div className="flex items-baseline gap-2 px-1">
        <Icon className="h-4 w-4 text-accent self-center" />
        <h2 className="text-sm font-semibold tracking-tight">{title}</h2>
        {blurb && <span className="text-[11px] text-zinc-600">{blurb}</span>}
      </div>
      {/* The Library's own grid geometry, so a shelf and the Library draw the
          covers at the same size — and the same shared album card. */}
      <div
        className="grid gap-x-4 gap-y-5 stagger"
        style={{ gridTemplateColumns: `repeat(auto-fill, minmax(${GRID_SIZE_MIN[gridSize]}px, 1fr))` }}
      >
        {items.map((a, i) => {
          // A shelf row is a library album (the server sends the library's own
          // row) unless it is a wish the library does not hold: that one has
          // no page to open and nothing to tick.
          const inLibrary = a.owned !== false && !!a.path;
          return (
            <AlbumCard
              // Identity first: an index in the key remounts a card (and
              // replays its stagger animation) whenever the shelf order
              // changes. A wish has neither a path nor always an MBID.
              key={a.path || a.mbid || `shelf-${i}`}
              al={a}
              href={inLibrary ? albumRef(a) : null}
              selectable={selectable && inLibrary}
              selected={selected.includes(a.path)}
              onSelect={onSelect}
              // The shelf's own words for why the row is here, under the
              // card's caption.
              extraMeta={extraOf ? extraOf(a) : a.reason ? <Chip text={a.reason} /> : undefined}
            />
          );
        })}
      </div>
    </section>
  );
}

/** The artist shelf's avatar.
 *
 *  The artist's own picture first: it is the artist the row names, and the
 *  shelf used to draw a representative ALBUM cover in that circle — an album's
 *  sleeve where a face belongs, and never the artist image the app had already
 *  fetched for the artist page (`GET /api/artist/image`, the same endpoint the
 *  artist page and every Discover row read).
 *
 *  That endpoint answers 404 for a folder holding no picture, and a URL that
 *  404s paints the browser's broken-image glyph before anything can replace it
 *  — so the request is only made when the payload says the folder has one
 *  (`has_image`, no probe of our own). The URL that failed is remembered as
 *  itself, the rule CoverImg/DiscoverRow keep, so a shelf re-rendered onto
 *  another artist never inherits a failure; the album cover stands in next and
 *  the placeholder is last. */
function ArtistAvatar({ artist }: { artist: HomeArtist }) {
  const [failed, setFailed] = useState<string[]>([]);
  const box = "h-20 w-20 rounded-full bg-raise overflow-hidden shrink-0";
  const picture = artist.has_image ? api.artistImageUrl(artist.path) : null;
  if (picture && !failed.includes(picture)) {
    return (
      <div className={box}>
        <img
          src={picture}
          alt=""
          loading="lazy"
          decoding="async"
          onError={() => setFailed((seen) => (seen.includes(picture) ? seen : [...seen, picture]))}
          className="h-full w-full object-cover"
        />
      </div>
    );
  }
  // No picture stored, or one that would not load: the artist's representative
  // album cover stands in, and CoverImg keeps its own placeholder for an album
  // with no cover either.
  return <CoverImg albumPath={artist.cover_path} coverFile={artist.cover} wrapperClass={box} />;
}

function ArtistShelf({ title, artists }: { title: string; artists?: HomeArtist[] }) {
  const { t } = useI18n();
  if (!artists?.length) return null;
  return (
    <section className="space-y-2">
      <div className="flex items-baseline gap-2 px-1">
        <Users className="h-4 w-4 text-accent self-center" />
        <h2 className="text-sm font-semibold tracking-tight">{title}</h2>
        <span className="text-[11px] text-zinc-600">{t("home.shelf.artists_blurb")}</span>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-6 gap-3 stagger">
        {artists.map((ar) => (
          <Link
            key={ar.path || ar.artist}
            to={`/artist/${encodeURIComponent(ar.path)}`}
            className="group rounded-xl p-2 flex flex-col items-center text-center transition-all duration-200 hover:bg-panel/70 hover:-translate-y-0.5"
            title={ar.artist}
          >
            <ArtistAvatar artist={ar} />
            <div className="mt-2 text-sm font-medium truncate w-full">{ar.artist}</div>
            <div className="text-[11px] text-zinc-500 tabular-nums">
              {ar.album_count} album{ar.album_count === 1 ? "" : "s"}
              {ar.grade_pct != null && <span> · {Math.round(ar.grade_pct)}%</span>}
            </div>
          </Link>
        ))}
      </div>
    </section>
  );
}

export default function HomePage() {
  const { t } = useI18n();
  // Refresh means "look at the music folder again", not "ask again": the
  // server caches this payload and the library tree behind it, so a plain
  // refetch redrew the same rows for minutes. The flag is one-shot — a ref
  // rather than a query key, so an ordinary background refetch (window focus)
  // keeps using the cache instead of forcing a rebuild every time.
  const forceRefresh = useRef(false);
  const { data, isLoading, isError, isFetching, refetch } = useQuery({
    queryKey: ["home"],
    queryFn: () => {
      const force = forceRefresh.current;
      forceRefresh.current = false;
      return api.home(force);
    },
    staleTime: 5 * 60_000,
  });
  const refresh = () => {
    forceRefresh.current = true;
    void refetch();
  };
  const [gridSize, pickGridSize] = useGridSize();
  const { selectMode, toggleSelectMode } = useSelectMode();
  const [statsOpen, setStatsOpen] = useState(false);
  // The GLOBAL selection (store.ts): ticking an album here is ticking the
  // album the Library's batch toolbar acts on.
  const selection = useStore((s) => s.selection);
  const toggleAlbum = useStore((s) => s.toggleAlbum);
  // What the Stats panel reads: the albums on this page, in shelf order and
  // de-duplicated (an album rides on several shelves). Home is a set of
  // curated shelves rather than the library — the whole-library numbers are
  // the ones the header above prints — so the panel reads what is on screen.
  const shown = useMemo(() => {
    const albums: HomeAlbum[] = [];
    const tracks: Track[] = [];
    const seen = new Set<string>();
    for (const row of [
      ...(data?.recent ?? []), ...(data?.pending ?? []), ...(data?.wanted ?? []),
      ...(data?.top_rated ?? []), ...(data?.rated ?? []), ...(data?.needs_attention ?? []),
      ...(data?.discover ?? []), ...(data?.favorites ?? []),
    ]) {
      const key = row.path || `mb:${row.mbid ?? ""}`;
      if (seen.has(key)) continue;
      seen.add(key);
      albums.push(row);
      tracks.push(...(row.tracks ?? []));
    }
    return { albums, tracks };
  }, [data]);

  if (isLoading) {
    return (
      <div className="p-6 mx-auto max-w-6xl">
        <PageLoading label={t("home.loading")} />
      </div>
    );
  }
  // A dead or unreachable server answers nothing, so the query only ever ends
  // in its error state — without this branch the page sat on the spinner for
  // good. An empty payload is the same dead end (the shelves are all derived
  // server-side), so both render the error with a way to try again.
  // Testing `!data` here (rather than `!isLoading && !data`) is also what
  // narrows `data` for the render below.
  if (isError || !data) {
    return (
      <div className="p-6 mx-auto max-w-6xl">
        <EmptyState
          title={t("home.error_title")}
          hint={t("home.error_hint")}
        />
        <div className="flex justify-center">
          <button
            className="btn-ghost !py-1.5 text-xs tap"
            onClick={refresh}
            disabled={isFetching}
            title={t("home.refresh_title")}
          >
            <RefreshCw className={`h-3.5 w-3.5 ${isFetching ? "animate-spin" : ""}`} /> {t("home.refresh")}
          </button>
        </div>
      </div>
    );
  }

  const { stats } = data;
  // The card options every shelf below draws with.
  const shelfProps = {
    gridSize,
    selectable: selectMode,
    selected: selection.albums,
    onSelect: toggleAlbum,
  };
  return (
    <div className="p-6 space-y-5 mx-auto max-w-6xl">
      {/* hero — the gradient and its glow stay; the title block is the shared
          PageHeader so every page announces itself the same way */}
      <div className="panel-hero relative overflow-hidden bg-gradient-to-br from-panel to-bg">
        <div className="absolute -right-20 -top-20 h-64 w-64 rounded-full bg-accent/10 blur-3xl pointer-events-none" />
        <div className="relative">
          <PageHeader
            overline={t("home.welcome")}
            title={t("home.title")}
            subtitle={
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-zinc-400">
                {/* the counts stay bold, the unit is the part that is translated */}
                <span><b className="text-zinc-100">{stats.artists}</b> {t("home.stat.artists")}</span>
                <span className="text-zinc-700">·</span>
                <span><b className="text-zinc-100">{stats.albums}</b> {t("home.stat.albums")}</span>
                <span className="text-zinc-700">·</span>
                <span><b className="text-zinc-100">{stats.tracks}</b> {t("home.stat.tracks")}</span>
                <span className="text-zinc-700">·</span>
                <span><b className="text-zinc-100">{stats.playlists}</b> {t("home.stat.playlists")}</span>
                {stats.grade_pct != null && (
                  <>
                    <span className="text-zinc-700">·</span>
                    <span><b className="text-zinc-100">{stats.grade_pct}%</b> {t("home.stat.checks")}</span>
                  </>
                )}
              </div>
            }
            actions={
              <>
                {/* The Library's own controls, over Home's own content: the
                    cover size the shelves draw at (the SAME setting the
                    Library's grid reads — one choice, both pages), Select and
                    Stats. They carry the Library's labels because they are
                    the Library's controls.

                    Its view tabs, Sort, quick filter and Group by artist are
                    deliberately NOT here: all four configure ONE flat album
                    table (which columns, which rows survive a preset, whether
                    artist headers are drawn), and a page of curated shelves
                    has no such table — the shelf IS the grouping. Every
                    setting of them would produce this same page. */}
                <span title="Cover size">
                  <Segmented value={gridSize} onChange={pickGridSize} options={GRID_SIZES} />
                </span>
                <button
                  className="btn-ghost !py-1.5 text-xs tap"
                  onClick={() => setStatsOpen(true)}
                  title="Statistics for the albums on this page"
                >
                  <BarChart3 className="h-3.5 w-3.5" /> Stats
                </button>
                <button
                  className={`btn-ghost !py-1.5 text-xs tap ${selectMode ? "!text-accent !border-accent/50" : ""}`}
                  onClick={toggleSelectMode}
                  title="Select mode — show checkboxes for batch actions"
                >
                  <ListChecks className="h-3.5 w-3.5" /> Select
                </button>
                <button
                  className="btn-ghost !py-1.5 text-xs tap"
                  onClick={refresh}
                  disabled={isFetching}
                  title={t("home.refresh_title")}
                >
                  <RefreshCw className={`h-3.5 w-3.5 ${isFetching ? "animate-spin" : ""}`} /> {t("home.refresh")}
                </button>
              </>
            }
          />
        </div>
      </div>

      {/* The library's own grading verdict, above the shelves: whether every
          album passed its checks, and — when one did not — the tracks and
          albums that failed, each linking to the thing it names. It paints
          from Home's own copy of the summary (the payload's `grade_warning`,
          the same object `/api/grades/summary` serves), so the strip costs no
          second walk of the library. */}
      <GradeWarning initial={data.grade_warning} />

      {/* Home ticks the same albums the Library's batch toolbar acts on (the
          selection is global), but the actions themselves — play, playlist,
          trash, scripts, tags, organize — are the Library's toolbar. Saying so
          is what keeps a tick here from being a dead end. */}
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

      {/* How much room the library is taking, against what the disk has —
          the first thing a user checks before importing another batch. */}
      <StorageCard />

      {/* Every shelf draws the shared album card with the page's own options:
          one cover size, one selection, one checkbox pass — passed once here
          instead of at each of the seven shelves. (Sort, the preset filter and
          Group by artist are not among them: see the header.) */}

      <Shelf title={t("home.shelf.recent")} icon={Clock} items={data.recent} {...shelfProps} />
      {/* The one shelf that answers "what am I still waiting for": a pending
          album also rides along where it would otherwise be listed (recent —
          its folder is the newest thing in the library — and its artist's own
          page), but a skeleton that is only ever mixed in with complete albums
          is a skeleton the user has to hunt for. */}
      <Shelf
        title={t("home.shelf.pending")}
        icon={Loader2}
        items={data.pending ?? []}
        blurb={t("home.shelf.pending_blurb")}
        {...shelfProps}
      />
      <Shelf
        title={t("home.shelf.wanted")}
        icon={ArrowDownUp}
        items={data.wanted ?? []}
        blurb={t("home.shelf.wanted_blurb")}
        {...shelfProps}
      />
      <Shelf title={t("home.shelf.best")} icon={Star} items={data.top_rated} {...shelfProps} />
      {/* The one shelf that is the READER's verdict rather than the library's:
          the releases they gave stars to, best first, each card carrying the
          stars it was ranked by. Releases with NO star are not a shelf —
          "unrated" says nothing about why a row is here and cannot be ordered
          (Rediscover already draws a random slice of what has not been
          looked at). */}
      <Shelf
        title={t("home.shelf.rated")}
        icon={Star}
        items={data.rated ?? []}
        extraOf={(row) => <StarRating readOnly size="sm" showValue value={toUi(row.rating)} />}
        {...shelfProps}
      />
      <Shelf
        title={t("home.shelf.attention")}
        icon={AlertTriangle}
        items={data.needs_attention ?? []}
        blurb={t("home.shelf.attention_blurb")}
        {...shelfProps}
      />
      <ArtistShelf title={t("home.shelf.artists")} artists={data.top_artists} />
      <Shelf title={t("home.shelf.rediscover")} icon={Disc3} items={data.discover} {...shelfProps} />
      <Shelf title={t("page.favorites")} icon={Heart} items={data.favorites} {...shelfProps} />

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
