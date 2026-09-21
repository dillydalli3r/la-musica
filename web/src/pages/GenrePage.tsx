import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Compass, Tags } from "lucide-react";
import { api } from "../api";
import PageHeader from "../components/PageHeader";
import Segmented from "../components/Segmented";
import { EmptyState, PageLoading } from "../components/Badges";
import { NotesChips } from "../components/DiscoverRow";
import { GENRE_FAMILIES } from "../lib/genres";
import { useStore } from "../store";

/** The two halves of genre browsing this page offers: the library's own
 *  genres (the facet cards below), and the ONLINE providers' genres — which
 *  are listed here and browsed in Discover, where the kind switch and the
 *  per-source filter live. The browser is not duplicated: this page names the
 *  online genres, and one click hands over the genre to the page that can
 *  show its albums, artists and tracks from every source at once. */
type Scope = "library" | "online";

const SCOPES: { id: Scope; label: string }[] = [
  { id: "library", label: "Library" },
  { id: "online", label: "Online" },
];

/** Genre browsing: the genre facet of the library as category cards, every
 *  genre a chip that opens the library filtered to it — plus, behind the scope
 *  toggle, the genres the online providers know, each opening Discover.
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
  const [scope, setScope] = useState<Scope>("library");
  const [filter, setFilter] = useState("");

  // The online half is only asked for while it is on screen: a library-only
  // visit must not cost a provider chain.
  const online = useQuery({
    queryKey: ["discoverGenres", "online"],
    queryFn: () => api.discoverGenres("online"),
    enabled: scope === "online",
    staleTime: 5 * 60_000,
    retry: false,
  });

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
  const onlineGenres = useMemo(() => {
    const list = online.data?.genres ?? [];
    return filterText ? list.filter((g) => g.name.toLowerCase().includes(filterText)) : list;
  }, [online.data, filterText]);

  const open = (genre: string) => {
    setQuery(`genre:"${genre}"`);
    navigate("/library");
  };

  const chip = (name: string, count?: number) => {
    // A family is the DERIVED last slot of a track's genre list
    // (mlo.genre_vocab.parent_of). The library holds it as a genre of its own,
    // so it is labelled here instead of left looking like one more specific
    // genre sitting under this card's head — the hierarchy the old
    // parent/main/sub model implied is gone.
    const family = GENRE_FAMILIES[name.toLowerCase()];
    return (
      <button
        key={name}
        className={`chip border ${family ? "bg-panel border-dashed border-border text-zinc-400" : "bg-raise border-border text-zinc-300"} hover:text-white hover:border-accent transition-colors`}
        onClick={() => open(name)}
        title={family
          ? `${name} is a family — the app derives it from a track's specific genre and writes it last. Open the library filtered to ${name}`
          : `Open the library filtered to ${name}`}
      >
        {family && <span className="text-[9px] uppercase tracking-wider text-zinc-600">family</span>}
        {name}
        {count != null && <span className="text-zinc-600 font-mono text-[10px]">{count}</span>}
      </button>
    );
  };

  return (
    <div className="p-6 space-y-5 mx-auto max-w-6xl">
      <PageHeader
        icon={Tags}
        title="Genres"
        subtitle={
          scope === "library"
            ? "Every genre in the library, in the app's filter buckets — one chip per name, a family marked as the derived head it is, and a click opens the library filtered to it. Switch to Online for the genres the providers know."
            : "The genres the online providers name for the library's artists — seeded from what you have, so the list is about your collection and not the whole internet. A click opens Discover, where albums, artists and tracks can be browsed per source."
        }
        actions={
          <>
            <Segmented value={scope} onChange={setScope} options={SCOPES} />
            <input
              className="input !py-1.5 w-56"
              placeholder="Filter genres…"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
          </>
        }
      />

      {scope === "online" ? (
        online.isLoading ? (
          <PageLoading label="Asking the online providers…" />
        ) : online.error ? (
          <EmptyState
            title="Online genres unavailable"
            hint={String(online.error instanceof Error ? online.error.message : online.error)}
          />
        ) : onlineGenres.length === 0 ? (
          <EmptyState
            title={filterText ? "No online genre matches that filter" : "No online genres yet"}
            hint="The providers name genres for an artist they recognise. Sources that were skipped or failed are listed above with the server's own reason."
          />
        ) : (
          <section className="panel space-y-3">
            <div className="flex flex-wrap items-baseline gap-2">
              <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Online genres</h2>
              <span className="text-[10px] text-zinc-600">
                {onlineGenres.length} genre{onlineGenres.length === 1 ? "" : "s"} · browse their albums, artists and tracks per source in Discover
              </span>
              <Link to="/discover" className="btn-ghost !py-1 text-xs ml-auto tap" title="Open the full genre browser">
                <Compass className="h-3.5 w-3.5" /> Open Discover
              </Link>
            </div>
            <NotesChips notes={online.data?.notes} sources={online.data?.sources_asked} />
            <div className="flex flex-wrap gap-1.5 stagger">
              {onlineGenres.map((g) => (
                <Link
                  key={g.name}
                  to={`/discover?genre=${encodeURIComponent(g.name)}`}
                  className="chip bg-raise border border-border text-zinc-300 hover:text-white hover:border-accent transition-colors"
                  title={`Browse ${g.name} in Discover — albums, artists and tracks, from the library and every online source. Named by ${g.sources.join(", ") || "no source"}.`}
                >
                  {g.name}
                  <span className="text-zinc-600 font-mono text-[10px]">
                    {g.album_count} alb · {g.artist_count} art
                  </span>
                </Link>
              ))}
            </div>
          </section>
        )
      ) : isLoading ? (
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
