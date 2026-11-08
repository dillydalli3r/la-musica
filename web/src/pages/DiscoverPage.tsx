import { useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Compass, Disc3, Library, Loader2, Music2, Sparkles, TrendingUp } from "lucide-react";
import { api, type DiscoverRow } from "../api";
import PageHeader from "../components/PageHeader";
import Segmented from "../components/Segmented";
import { EmptyState, PageLoading } from "../components/Badges";
import { toast } from "../store";
import { fmtDuration } from "../lib/fmt";

/** How many rows one shelf asks for. Apple's keyless feed answers 100; the
 *  page shows the front of it, like a storefront. */
const LIMIT = 40;

const KINDS: { id: "tracks" | "albums"; label: string }[] = [
  { id: "tracks", label: "Songs" },
  { id: "albums", label: "Albums" },
];

/** A provider badge: the service that stated the row, never a claim that all
 *  of them are one catalogue. */
function SourceBadge({ source }: { source?: string }) {
  const label = source === "itunes" ? "Apple Music"
    : source === "deezer" ? "Deezer"
      : source === "lastfm" ? "Last.fm"
        : source === "listenbrainz" ? "ListenBrainz"
          : source === "spotify" ? "Spotify"
            : source || "";
  if (!label) return null;
  return (
    <span className="chip text-[9px] text-zinc-500 border border-border bg-raise shrink-0" title={`Stated by ${label}`}>
      {label}
    </span>
  );
}

/** One online row: cover, title, artist · album, and the one action an
 *  unowned row has — add the release it names to the library (the server
 *  matches it on MusicBrainz and queues the search). */
function Row({ row, rank, onAdd, busy }: {
  row: DiscoverRow;
  rank?: number;
  onAdd: (row: DiscoverRow) => void;
  busy: boolean;
}) {
  return (
    <li className="flex items-center gap-2.5 px-3 py-2 group">
      <span className="w-6 text-right text-[11px] font-mono text-zinc-600 tabular-nums shrink-0">
        {rank ?? ""}
      </span>
      <span className="h-10 w-10 rounded bg-raise overflow-hidden shrink-0">
        {row.cover ? (
          <img src={row.cover} alt="" loading="lazy" className="h-full w-full object-cover" />
        ) : (
          <span className="h-full w-full flex items-center justify-center text-zinc-600">
            <Music2 className="h-4 w-4" />
          </span>
        )}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block text-sm text-zinc-100 truncate" title={row.title}>
          {row.title}
        </span>
        <span className="block text-[11px] text-zinc-500 truncate">
          {[row.artist, row.album, row.year].filter(Boolean).join(" · ")}
        </span>
      </span>
      <SourceBadge source={row.source} />
      {typeof row.duration === "number" && row.duration > 0 ? (
        <span className="text-[10px] font-mono text-zinc-600 w-10 text-right shrink-0">
          {fmtDuration(row.duration)}
        </span>
      ) : null}
      <button
        className="btn-ghost !px-2 !py-1 shrink-0 row-hover"
        disabled={busy}
        title={`Add “${row.title}” to your library and start searching for it`}
        onClick={() => onAdd(row)}
      >
        {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Library className="h-3.5 w-3.5" />}
      </button>
    </li>
  );
}

function Section({
  icon: Icon,
  title,
  hint,
  children,
  busy,
  error,
}: {
  icon: typeof Sparkles;
  title: string;
  hint: string;
  children: ReactNode;
  busy?: boolean;
  error?: string;
}) {
  return (
    <section className="space-y-2">
      <div className="flex items-baseline gap-2 flex-wrap">
        <Icon className="h-4 w-4 text-zinc-500" />
        <h2 className="text-sm font-semibold text-zinc-200">{title}</h2>
        <span className="text-[11px] text-zinc-600">{hint}</span>
        {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin text-zinc-500" /> : null}
      </div>
      {error ? <EmptyState title="Nothing came back" hint={error} /> : children}
    </section>
  );
}

/** Discover: the online storefront the owner asked back — top songs by genre
 *  from the streaming services, the current chart, and a library-seeded shelf.
 *  Every row is ADD-able (the server matches it on MusicBrainz and queues the
 *  acquisition), so a recommendation is one press from the library. */
export default function DiscoverPage() {
  const [params, setParams] = useSearchParams();
  const genre = params.get("genre") || "pop";
  const kind = (params.get("kind") as "tracks" | "albums") || "tracks";
  const [busyRow, setBusyRow] = useState("");

  const write = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(patch)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    setParams(next, { replace: true });
  };

  const genresQ = useQuery({
    queryKey: ["discoverGenres"],
    queryFn: () => api.discoverGenres(),
    staleTime: 24 * 60 * 60_000,
    retry: false,
  });

  const genreQ = useQuery({
    queryKey: ["discoverGenre", genre, kind],
    queryFn: () => api.discoverGenre({ genre, kind, limit: LIMIT }),
    placeholderData: keepPreviousData,
    staleTime: 5 * 60_000,
    retry: 1,
  });

  const chartQ = useQuery({
    queryKey: ["discoverChart", "tracks"],
    queryFn: () => api.discoverCharts({ kind: "tracks", source: "auto", limit: 20 }),
    staleTime: 10 * 60_000,
    retry: 1,
  });

  const recQ = useQuery({
    queryKey: ["discoverRecommended", "albums"],
    queryFn: () => api.discoverRecommended({ kind: "albums", limit: 12 }),
    staleTime: 30 * 60_000,
    retry: 1,
  });

  const add = async (row: DiscoverRow) => {
    setBusyRow(row.title);
    try {
      const res = await api.libraryAdd({
        mbid: "",
        kind: row.kind === "track" ? "track" : "album",
        title: row.title,
        artist: row.artist || "",
        year: row.year || "",
      });
      const added = res.albums?.filter((a) => a.created).length ?? 0;
      if (added) toast(`Added “${row.title}” — searching for it now`, "success");
      else toast(res.note || "MusicBrainz has no match for that row", "error");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyRow("");
    }
  };

  const genres = genresQ.data?.genres ?? [];
  const genreRows = genreQ.data?.rows ?? [];

  return (
    <div className="p-6 space-y-6 mx-auto max-w-[1200px]">
      <PageHeader
        icon={Compass}
        title="Discover"
        subtitle="Top songs by genre and what the streaming services are ranking now — Apple Music first, Deezer and Last.fm where they answer better."
      >
        <div className="flex flex-wrap items-center gap-2">
          <Segmented
            value={kind}
            onChange={(v) => write({ kind: v })}
            options={KINDS}
          />
        </div>
      </PageHeader>

      <div className="flex flex-wrap gap-1.5">
        {genresQ.isLoading ? <PageLoading label="Loading genres…" /> : null}
        {genres.map((g) => (
          <button
            key={g.name}
            className={`chip text-[11px] border ${genre === g.name ? "bg-accent on-accent border-accent" : "bg-raise border-border text-zinc-400 hover:text-white hover:border-accent"}`}
            onClick={() => write({ genre: g.name })}
            title={`Top ${kind} in ${g.label}`}
          >
            {g.label}
          </button>
        ))}
      </div>

      <Section
        icon={Disc3}
        title={`Top ${kind === "tracks" ? "songs" : "albums"} in ${genres.find((g) => g.name === genre)?.label ?? genre}`}
        hint={genreQ.data?.source === "itunes"
          ? "Apple Music's genre index — one press adds the release to your library"
          : genreQ.data?.source === "lastfm"
            ? "Last.fm's tag chart — one press adds the release to your library"
            : "the service's own catalogue order"}
        busy={genreQ.isFetching}
        error={genreQ.data?.ok === false ? genreQ.data.reason : undefined}
      >
        {genreQ.isLoading ? (
          <PageLoading label="Reading the genre…" />
        ) : genreRows.length === 0 ? (
          <EmptyState
            title="Nothing in this genre right now"
            hint={genreQ.data?.reason || "The service answered with no rows — try another genre."}
          />
        ) : (
          <div className="panel !p-0 overflow-hidden">
            <ul className="divide-y divide-border/60">
              {genreRows.map((row, i) => (
                <Row
                  key={`${row.source}-${row.title}-${i}`}
                  row={row}
                  rank={row.rank ?? i + 1}
                  onAdd={add}
                  busy={busyRow === row.title}
                />
              ))}
            </ul>
          </div>
        )}
      </Section>

      <Section
        icon={TrendingUp}
        title="Trending now"
        hint="Apple Music's most-played songs for your storefront"
        busy={chartQ.isFetching}
        error={chartQ.data?.ok === false ? chartQ.data.reason : undefined}
      >
        {chartQ.isLoading ? (
          <PageLoading label="Reading the chart…" />
        ) : (chartQ.data?.rows ?? []).length === 0 ? (
          <EmptyState title="The chart is empty" hint={chartQ.data?.reason || ""} />
        ) : (
          <div className="panel !p-0 overflow-hidden">
            <ul className="divide-y divide-border/60">
              {(chartQ.data?.rows ?? []).map((row, i) => (
                <Row
                  key={`${row.source}-${row.title}-${i}`}
                  row={row}
                  rank={row.rank ?? i + 1}
                  onAdd={add}
                  busy={busyRow === row.title}
                />
              ))}
            </ul>
          </div>
        )}
      </Section>

      <Section
        icon={Sparkles}
        title="For you"
        hint={recQ.data?.basis || "seeded by the genre your library listens to most"}
        busy={recQ.isFetching}
        error={recQ.data?.ok === false ? recQ.data.reason : undefined}
      >
        {recQ.isLoading ? (
          <PageLoading label="Building the shelf…" />
        ) : (recQ.data?.rows ?? []).length === 0 ? (
          <EmptyState title="No online suggestions right now" hint={recQ.data?.reason || "Everything this shelf found is already in your library."} />
        ) : (
          <div className="grid gap-3 grid-cols-2 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-6">
            {(recQ.data?.rows ?? []).map((row, i) => (
              <div key={`${row.source}-${row.title}-${i}`} className="group">
                <div className="aspect-square rounded-xl bg-raise overflow-hidden shadow-lg">
                  {row.cover ? (
                    <img src={row.cover} alt="" loading="lazy" className="h-full w-full object-cover" />
                  ) : (
                    <div className="h-full w-full flex items-center justify-center text-zinc-600">
                      <Music2 className="h-8 w-8" />
                    </div>
                  )}
                </div>
                <div className="mt-1.5 min-w-0">
                  <div className="text-sm font-medium truncate" title={row.title}>{row.title}</div>
                  <div className="text-[11px] text-zinc-500 truncate">{row.artist}</div>
                </div>
                <div className="mt-1 flex items-center gap-1">
                  <SourceBadge source={row.source} />
                  <button
                    className="btn-ghost !px-2 !py-1 text-[10px]"
                    disabled={busyRow === row.title}
                    onClick={() => void add(row)}
                    title="Add this release to your library"
                  >
                    {busyRow === row.title ? <Loader2 className="h-3 w-3 animate-spin" /> : <Library className="h-3 w-3" />}
                    Add
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </Section>
    </div>
  );
}
