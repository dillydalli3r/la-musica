import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { AlertTriangle, ArrowDownUp, Clock, Disc3, Heart, RefreshCw, Sparkles, Star, Users } from "lucide-react";
import { api } from "../api";
import { EmptyState, PageLoading } from "../components/Badges";
import PageHeader from "../components/PageHeader";
import { useI18n } from "../lib/i18n";
import CoverImg from "../components/CoverImg";
import { useFav } from "../lib/favs";
import type { HomeAlbum, HomeArtist } from "../types";

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

/** The Home page's like control: a small star in the corner of an owned
 *  album's cover.
 *
 *  It writes through the same favourites store as every other heart in the
 *  app (`useFav` → /api/favorites), so a star set here shows up on the album
 *  page, in Favorites, and on every other client — including the installed
 *  iOS/Android builds, which reach the same server with the session token the
 *  API client attaches (see web/src/api.ts). A wish that is not in the
 *  library yet has nothing to favourite, so it gets no star.
 */
function StarLike({ a }: { a: HomeAlbum }) {
  const { fav, toggle } = useFav("album", a.owned ? a.path : undefined, a.mbid);
  if (!a.owned || !a.path) return null;
  return (
    <button
      className={`absolute top-1.5 right-1.5 h-7 w-7 rounded-full border border-white/10 bg-black/60 backdrop-blur flex items-center justify-center transition-colors ${
        fav ? "text-accent" : "text-zinc-300 hover:text-white"
      }`}
      onClick={(e) => {
        // The cover sits inside a link to the album page: the star must not
        // navigate.
        e.preventDefault();
        e.stopPropagation();
        toggle();
      }}
      title={fav ? "Remove from favorites" : "Add to favorites"}
      aria-pressed={fav}
      aria-label={fav ? "Remove from favorites" : "Add to favorites"}
    >
      <Star className={`h-3.5 w-3.5 ${fav ? "fill-current" : ""}`} />
    </button>
  );
}

/** One Home shelf card. Every shelf is library-derived now, so an owned album
 *  links to its page and the rest (a wish being hunted) renders inert. */
function HomeCard({ a }: { a: HomeAlbum }) {
  const to = a.owned && a.path ? `/album/${encodeURIComponent(a.path)}` : null;

  const art =
    a.owned && a.path ? (
      <CoverImg
        albumPath={a.path}
        coverFile={a.cover}
        wrapperClass="aspect-square w-full rounded-xl shadow-lg ring-1 ring-black/40 overflow-hidden"
      />
    ) : (
      <div className="aspect-square w-full rounded-xl shadow-lg ring-1 ring-black/40 overflow-hidden bg-raise flex items-center justify-center text-zinc-700">
        <Disc3 className="h-1/3 w-1/3" />
      </div>
    );

  return (
    <div className="group flex h-full flex-col rounded-xl p-2 transition-all duration-200 hover:bg-panel/70 hover:-translate-y-0.5">
      {to ? (
        <Link to={to} className="block" title="Open album page">
          <div className="relative">
            {art}
            <StarLike a={a} />
          </div>
        </Link>
      ) : (
        <div className="relative" title={a.artist ? `${a.artist} — ${a.album}` : a.album}>
          {art}
          <StarLike a={a} />
        </div>
      )}
      <div className="mt-2 px-0.5 flex flex-1 flex-col gap-1">
        {to ? (
          <Link
            to={to}
            className="text-sm font-medium truncate hover:text-accent-soft"
            title={a.album}
          >
            {a.album || "—"}
          </Link>
        ) : (
          <div className="text-sm font-medium truncate" title={a.album}>
            {a.album || "—"}
          </div>
        )}
        <div className="text-[11px] text-zinc-500 truncate flex items-center gap-1.5">
          <span className="truncate" title={a.artist}>{a.artist}</span>
          {a.year && <span className="ml-auto shrink-0 tabular-nums">{a.year}</span>}
          {a.owned && a.grade_pct != null && (
            <span className="ml-auto shrink-0 tabular-nums" title="Checks passed">
              {Math.round(a.grade_pct)}%
            </span>
          )}
        </div>
        {a.reason && <Chip text={a.reason} />}
      </div>
    </div>
  );
}

function Shelf({
  title,
  icon: Icon,
  items,
  blurb,
}: {
  title: string;
  icon: typeof Sparkles;
  items: HomeAlbum[];
  blurb?: string;
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
      <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-6 gap-3 stagger">
        {items.map((a, i) => (
          // Identity first: an index in the key remounts a card (and replays
          // its stagger animation) whenever the shelf order changes.
          <HomeCard key={a.mbid ?? a.path ?? `shelf-${i}`} a={a} />
        ))}
      </div>
    </section>
  );
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
            <CoverImg
              albumPath={ar.cover_path}
              coverFile={ar.cover}
              wrapperClass="h-20 w-20 rounded-full bg-raise border border-border overflow-hidden shrink-0"
            />
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
  const { data, isLoading, isError, isFetching, refetch } = useQuery({
    queryKey: ["home"],
    queryFn: api.home,
    staleTime: 5 * 60_000,
  });

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
            className="btn-ghost !py-1.5 text-xs min-h-[2rem] md:min-h-0"
            onClick={() => refetch()}
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
              <button
                className="btn-ghost !py-1.5 text-xs min-h-[2rem] md:min-h-0"
                onClick={() => refetch()}
                disabled={isFetching}
                title={t("home.refresh_title")}
              >
                <RefreshCw className={`h-3.5 w-3.5 ${isFetching ? "animate-spin" : ""}`} /> {t("home.refresh")}
              </button>
            }
          />
        </div>
      </div>

      <Shelf title={t("home.shelf.recent")} icon={Clock} items={data.recent} />
      <Shelf
        title={t("home.shelf.wanted")}
        icon={ArrowDownUp}
        items={data.wanted ?? []}
        blurb={t("home.shelf.wanted_blurb")}
      />
      <Shelf title={t("home.shelf.best")} icon={Star} items={data.top_rated} />
      <Shelf
        title={t("home.shelf.attention")}
        icon={AlertTriangle}
        items={data.needs_attention ?? []}
        blurb={t("home.shelf.attention_blurb")}
      />
      <ArtistShelf title={t("home.shelf.artists")} artists={data.top_artists} />
      <Shelf title={t("home.shelf.rediscover")} icon={Disc3} items={data.discover} />
      <Shelf title={t("page.favorites")} icon={Heart} items={data.favorites} />
    </div>
  );
}
