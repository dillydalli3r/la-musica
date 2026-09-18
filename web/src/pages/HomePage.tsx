import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { AlertTriangle, ArrowDownUp, Clock, Disc3, Heart, RefreshCw, Sparkles, Star, Users } from "lucide-react";
import { api } from "../api";
import { EmptyState, PageLoading } from "../components/Badges";
import PageHeader from "../components/PageHeader";
import CoverImg from "../components/CoverImg";
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
          <div className="relative">{art}</div>
        </Link>
      ) : (
        <div className="relative" title={a.artist ? `${a.artist} — ${a.album}` : a.album}>
          {art}
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
  empty,
}: {
  title: string;
  icon: typeof Sparkles;
  items: HomeAlbum[];
  blurb?: string;
  /** Shown instead of nothing when the shelf is empty (omit to hide the shelf). */
  empty?: string;
}) {
  if (!items.length && !empty) return null;
  return (
    <section className="space-y-2">
      <div className="flex items-baseline gap-2 px-1">
        <Icon className="h-4 w-4 text-accent self-center" />
        <h2 className="text-sm font-semibold tracking-tight">{title}</h2>
        {blurb && <span className="text-[11px] text-zinc-600">{blurb}</span>}
      </div>
      {items.length ? (
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-6 gap-3 stagger">
          {items.map((a, i) => (
            <HomeCard key={`${a.mbid ?? a.path}-${i}`} a={a} />
          ))}
        </div>
      ) : (
        <p className="px-1 text-xs text-zinc-600">{empty}</p>
      )}
    </section>
  );
}

function ArtistShelf({ title, artists }: { title: string; artists?: HomeArtist[] }) {
  if (!artists?.length) return null;
  return (
    <section className="space-y-2">
      <div className="flex items-baseline gap-2 px-1">
        <Users className="h-4 w-4 text-accent self-center" />
        <h2 className="text-sm font-semibold tracking-tight">{title}</h2>
        <span className="text-[11px] text-zinc-600">Deepest artist collections</span>
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
  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ["home"],
    queryFn: api.home,
    staleTime: 5 * 60_000,
  });

  if (isLoading) {
    return (
      <div className="p-6 mx-auto max-w-6xl">
        <PageLoading label="Loading your library…" />
      </div>
    );
  }
  if (!data) {
    return (
      <div className="p-6 mx-auto max-w-6xl">
        <EmptyState
          title="Could not build the Home page"
          hint="Check that your music folder is set."
        />
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
            overline="Welcome back"
            title="Your library"
            subtitle={
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-zinc-400">
                <span><b className="text-zinc-100">{stats.artists}</b> artists</span>
                <span className="text-zinc-700">·</span>
                <span><b className="text-zinc-100">{stats.albums}</b> albums</span>
                <span className="text-zinc-700">·</span>
                <span><b className="text-zinc-100">{stats.tracks}</b> tracks</span>
                <span className="text-zinc-700">·</span>
                <span><b className="text-zinc-100">{stats.playlists}</b> playlists</span>
                {stats.grade_pct != null && (
                  <>
                    <span className="text-zinc-700">·</span>
                    <span><b className="text-zinc-100">{stats.grade_pct}%</b> checks passed</span>
                  </>
                )}
              </div>
            }
            actions={
              <button
                className="btn-ghost !py-1.5 text-xs"
                onClick={() => refetch()}
                disabled={isFetching}
                title="Reload the shelves"
              >
                <RefreshCw className={`h-3.5 w-3.5 ${isFetching ? "animate-spin" : ""}`} /> Refresh
              </button>
            }
          />
        </div>
      </div>

      <Shelf title="Recently added" icon={Clock} items={data.recent} />
      <Shelf
        title="Wanted on Soulseek"
        icon={ArrowDownUp}
        items={data.wanted ?? []}
        blurb="Being hunted in the background"
      />
      <Shelf title="Best graded" icon={Star} items={data.top_rated} />
      <Shelf
        title="Needs attention"
        icon={AlertTriangle}
        items={data.needs_attention ?? []}
        blurb="Albums failing at least one check"
      />
      <ArtistShelf title="Top artists" artists={data.top_artists} />
      <Shelf title="Rediscover" icon={Disc3} items={data.discover} />
      <Shelf title="Favorites" icon={Heart} items={data.favorites} />
    </div>
  );
}
