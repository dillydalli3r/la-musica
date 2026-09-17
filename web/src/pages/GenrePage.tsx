import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Tags } from "lucide-react";
import { api } from "../api";
import PageHeader from "../components/PageHeader";
import { EmptyState, PageLoading } from "../components/Badges";
import { useStore } from "../store";

/** Genre browsing: the genre facet of the library as category cards, every
 *  genre a chip that opens the library filtered to it. The chip writes the
 *  tag-scoped `genre:` query, which is the same filter the library search box
 *  understands (`genre:"progressive rock"`), so this page and the search box
 *  stay one mechanism. */
export default function GenrePage() {
  const navigate = useNavigate();
  const setQuery = useStore((s) => s.setQuery);
  const { data, isLoading, error } = useQuery({
    queryKey: ["genreFacets"],
    queryFn: api.genresFacets,
  });
  const [filter, setFilter] = useState("");

  const genres = data?.genres ?? [];
  const categories = data?.categories ?? [];
  // Genres no category claims still need somewhere to live, so they get a
  // trailing card instead of disappearing from the page.
  const uncategorized = useMemo(() => {
    const grouped = new Set(categories.flatMap((c) => c.genres));
    return genres.filter((g) => !grouped.has(g.name));
  }, [genres, categories]);

  const match = useMemo(() => {
    const f = filter.trim().toLowerCase();
    if (!f) return genres;
    return genres.filter((g) => g.name.toLowerCase().includes(f));
  }, [genres, filter]);

  const open = (genre: string) => {
    setQuery(`genre:"${genre}"`);
    navigate("/library");
  };

  const chip = (name: string, count?: number) => (
    <button
      key={name}
      className="chip bg-raise border border-border text-zinc-300 hover:text-white hover:border-accent transition-colors"
      onClick={() => open(name)}
      title={`Open the library filtered to ${name}`}
    >
      {name}
      {count != null && <span className="text-zinc-600 font-mono text-[10px]">{count}</span>}
    </button>
  );

  return (
    <div className="p-6 space-y-5 mx-auto max-w-6xl">
      <PageHeader
        icon={Tags}
        title="Genres"
        subtitle="Every genre in the library, grouped by category — a chip opens the library filtered to it."
        actions={
          <input
            className="input !py-1.5 w-56"
            placeholder="Filter genres…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        }
      />

      {isLoading ? (
        <PageLoading label="Counting genres…" />
      ) : error ? (
        <EmptyState title="Genres unavailable" hint={String(error instanceof Error ? error.message : error)} />
      ) : genres.length === 0 ? (
        <EmptyState
          title="No genres yet"
          hint="Import genres on an album (Tag actions → Import genres) or run the genre autofill script, then they show up here."
        />
      ) : filter.trim() ? (
        <div className="panel">
          <div className="text-[11px] font-semibold uppercase tracking-wider text-zinc-500 pb-2">
            {match.length} matching genre{match.length === 1 ? "" : "s"}
          </div>
          <div className="flex flex-wrap gap-1.5">{match.map((g) => chip(g.name, g.count))}</div>
        </div>
      ) : (
        <>
          {categories.map((cat) => {
            const rows = cat.genres
              .map((name) => genres.find((g) => g.name === name))
              .filter((g): g is { name: string; count: number } => !!g);
            if (!rows.length) return null;
            const total = rows.reduce((n, g) => n + g.count, 0);
            return (
              <section key={cat.name} className="panel">
                <div className="flex items-baseline gap-2 pb-2">
                  <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-500">{cat.name}</h2>
                  <span className="text-[10px] text-zinc-600">
                    {rows.length} genre{rows.length === 1 ? "" : "s"} · {total} tracks
                  </span>
                </div>
                <div className="flex flex-wrap gap-1.5 stagger">
                  {rows.map((g) => chip(g.name, g.count))}
                </div>
              </section>
            );
          })}
          {uncategorized.length > 0 && (
            <section className="panel">
              <div className="flex items-baseline gap-2 pb-2">
                <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Other</h2>
                <span className="text-[10px] text-zinc-600">{uncategorized.length} genres</span>
              </div>
              <div className="flex flex-wrap gap-1.5 stagger">
                {uncategorized.map((g) => chip(g.name, g.count))}
              </div>
            </section>
          )}
        </>
      )}
    </div>
  );
}
