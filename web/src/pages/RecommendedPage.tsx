import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Sparkles } from "lucide-react";
import { api, type DiscoverKind } from "../api";
import PageHeader from "../components/PageHeader";
import Segmented from "../components/Segmented";
import { EmptyState, PageLoading } from "../components/Badges";
import DiscoverRow, { NotesChips } from "../components/DiscoverRow";

/** How many rows a recommendation answer carries — the shelf is a starting
 *  point, and the genre browser is where a chosen row is followed up. */
const LIMIT = 20;

const KINDS: { id: DiscoverKind; label: string }[] = [
  { id: "albums", label: "Albums" },
  { id: "artists", label: "Artists" },
  { id: "tracks", label: "Tracks" },
];

/** Recommendations from the online providers, seeded by the library the user
 *  actually has (every artist and genre in it) or by one genre they name.
 *
 *  The seed and the kind are query params, so a shelf someone liked is a link.
 *  Rows are the shared `DiscoverRow`: what the library already holds links
 *  into it, what it does not offers the add action, and a source that answered
 *  nothing is shown with the server's own reason instead of being hidden. */
export default function RecommendedPage() {
  const [params, setParams] = useSearchParams();
  const seed = params.get("seed") || "library";
  const kind = (params.get("kind") as DiscoverKind) || "albums";

  const setParam = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  };

  // The seed picker offers the same genre list the Discover page browses.
  const genresQ = useQuery({
    queryKey: ["discoverGenres", "all"],
    queryFn: () => api.discoverGenres("all"),
    staleTime: 5 * 60_000,
    retry: false,
  });

  const recQ = useQuery({
    queryKey: ["discoverRecommended", seed, kind],
    queryFn: () => api.discoverRecommended({ seed, kind, limit: LIMIT }),
    staleTime: 5 * 60_000,
    retry: false,
  });

  const rows = recQ.data?.items ?? [];
  const notes = recQ.data?.notes ?? {};
  const asked = recQ.data?.sources_asked ?? [];

  return (
    <div className="p-6 space-y-5 mx-auto max-w-6xl">
      <PageHeader
        icon={Sparkles}
        title="Recommended"
        subtitle="What the online providers suggest for your library — seeded by everything you have, or by one genre — with the library's own copy of each row whenever it already has one."
        actions={<Segmented value={kind} onChange={(k) => setParam("kind", k)} options={KINDS} />}
      >
        <div className="flex flex-wrap items-center gap-2">
          <label className="text-[11px] uppercase tracking-wider text-zinc-500" htmlFor="rec-seed">
            Seed
          </label>
          <select
            id="rec-seed"
            className="input !py-1.5 !w-64"
            value={seed}
            onChange={(e) => setParam("seed", e.target.value)}
            title="What the suggestions are built from"
          >
            <option value="library">Whole library</option>
            {(genresQ.data?.genres ?? []).map((g) => (
              <option key={g.name} value={g.name}>
                {g.name}
              </option>
            ))}
          </select>
          {recQ.data?.basis && (
            <span className="chip bg-raise border border-border text-zinc-400" title="What this answer was built from">
              basis: {recQ.data.basis}
            </span>
          )}
          <NotesChips notes={notes} sources={asked} />
        </div>
      </PageHeader>

      {recQ.isLoading ? (
        <PageLoading label="Asking the providers for suggestions…" />
      ) : recQ.error ? (
        <EmptyState
          title="No recommendations could be read"
          hint={String(recQ.error instanceof Error ? recQ.error.message : recQ.error)}
          action={{ label: "Browse genres instead", to: "/discover" }}
        />
      ) : rows.length === 0 ? (
        <EmptyState
          title="No suggestions for this seed"
          hint={
            seed === "library"
              ? "Every source the server could reach had nothing to suggest for the library as it stands — the chips above say which sources were asked and which stayed silent."
              : `No source had anything for “${seed}”. Try another genre, or the whole library as the seed.`
          }
          action={{ label: "Browse this genre", to: `/discover?genre=${encodeURIComponent(seed)}` }}
        />
      ) : (
        <div className="panel !p-0 overflow-hidden">
          <div className="px-3 py-2 border-b border-border/60 text-[11px] text-zinc-500">
            {rows.length} suggestion{rows.length === 1 ? "" : "s"} for{" "}
            {seed === "library" ? "the whole library" : `“${seed}”`}
          </div>
          <ul className="divide-y divide-border/60 stagger">
            {rows.map((item, i) => (
              <DiscoverRow key={`${item.source}:${item.kind}:${item.mbid || item.path || item.title}-${i}`} item={item} />
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
