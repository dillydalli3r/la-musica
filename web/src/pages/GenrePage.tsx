import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Tags } from "lucide-react";
import { api } from "../api";
import PageHeader from "../components/PageHeader";
import { EmptyState, PageLoading } from "../components/Badges";
import { GENRE_FAMILIES } from "../lib/genres";
import { titleCaseGenre } from "../lib/fmt";
import { useStore } from "../store";

/** Genre browsing: the genre facet of the library as category cards, every
 *  genre a chip that opens the library filtered to it. (The ONLINE providers'
 *  genre list and the Discover browser it opened are gone with the online
 *  discovery feeds — this page is the library's own genres.)
 *
 *  The cards are filter buckets, not a genre tree — a track's genre LIST is a
 *  specific genre and the family the app derives from it (mlo.genres), so a
 *  family is marked as one rather than drawn as a subgenre of a card. The chip
 *  writes the tag-scoped `genre:` query, which is the same filter the library
 *  search box understands (`genre:"progressive rock"`), so this page and the
 *  search box stay one mechanism. */
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

  const filterText = filter.trim().toLowerCase();
  const match = useMemo(() => {
    if (!filterText) return genres;
    return genres.filter((g) => g.name.toLowerCase().includes(filterText));
  }, [genres, filterText]);

  const open = (genre: string) => {
    setQuery(`genre:"${genre}"`);
    navigate("/library");
  };

  const chip = (name: string, count?: number) => {
    // A family is one of the names the vocabulary publishes (mlo.genre_vocab
    // .FAMILIES): a track's own genre can BE one ("Rock" alone is an answer),
    // so it is marked here rather than shown as one more specific name.
    // Nothing above a name is derived or stored any more, so no position is
    // implied.
    const family = GENRE_FAMILIES[name.toLowerCase()];
    // The CHIP prints the reader's form; `open()` and the filter match keep the
    // stored lower-case name (see `titleCaseGenre` — the app's genre buckets
    // and the `genre:"…"` query are lower-case by convention).
    const shown = titleCaseGenre(name);
    return (
      <button
        key={name}
        className={`chip border ${family ? "bg-panel border-dashed border-border text-zinc-400" : "bg-raise border-border text-zinc-300"} hover:text-white hover:border-accent transition-colors tap`}
        onClick={() => open(name)}
        title={family
          ? `${shown} is a family (the head of the genre tree). Open the library filtered to ${shown}`
          : `Open the library filtered to ${shown}`}
      >
        {family && <span className="text-[9px] uppercase tracking-wider text-zinc-600">family</span>}
        {shown}
        {count != null && <span className="text-zinc-600 font-mono text-[10px]">{count}</span>}
      </button>
    );
  };

  return (
    <div className="p-6 space-y-5 mx-auto max-w-[1600px]">
      <PageHeader
        icon={Tags}
        title="Genres"
        subtitle="Every genre in the library, in the app's filter buckets — one chip per name, a family marked as the derived head it is, and a click opens the library filtered to it."
        actions={
          <>
            <input
              className="input !py-1.5 w-56"
              placeholder="Filter genres…"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
          </>
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
          <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500 pb-2">
            {match.length} matching genre{match.length === 1 ? "" : "s"}
          </div>
          <div className="flex flex-wrap gap-1.5">{match.map((g) => chip(g.name, g.count))}</div>
        </div>
      ) : (
        <>
          {categories.map((cat) => {
            const rows = cat.genres
              .map((name) => genres.find((g) => g.name === name))
              .filter((g): g is { name: string; count: number; path: string } => !!g);
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
