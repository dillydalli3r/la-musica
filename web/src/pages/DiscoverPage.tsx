import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { keepPreviousData, useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Compass, Loader2 } from "lucide-react";
import { api, type DiscoverKind, type DiscoverScope } from "../api";
import PageHeader from "../components/PageHeader";
import Segmented from "../components/Segmented";
import { EmptyState, PageLoading } from "../components/Badges";
import DiscoverRow, { NotesChips, sourceLabel } from "../components/DiscoverRow";

/** Rows per request — the page's own page size; the server caps it and hands
 *  back the cursor (`next_offset`) for the next one. */
const PAGE = 25;

/** How far the server lets a genre list be paged (a deeper offset is a 400).
 *  A cursor beyond it is not followed: the list ends here instead. */
const MAX_OFFSET = 200;

const SCOPES: { id: DiscoverScope; label: string }[] = [
  { id: "library", label: "Library" },
  { id: "online", label: "Online" },
  { id: "all", label: "Both" },
];

const KINDS: { id: DiscoverKind; label: string }[] = [
  { id: "albums", label: "Albums" },
  { id: "artists", label: "Artists" },
  { id: "tracks", label: "Tracks" },
];

/** Discover: one genre at a time, across the library AND the online providers.
 *
 *  The genre list, the kind and the source filter are all query params, so a
 *  view someone found is a link they can keep (GenrePage's online half and the
 *  recommendations page both hand one over). The rows are the shared
 *  `DiscoverRow`: cover, title, artist, year, the provider it came from, a
 *  reason line, and either a link into the library or the add action.
 *
 *  Every answer states which sources were asked and which stayed silent with
 *  their reason, and a source that had nothing produces an empty state naming
 *  it rather than a blank panel. */
export default function DiscoverPage() {
  const [params, setParams] = useSearchParams();
  const scope = (params.get("scope") as DiscoverScope) || "all";
  const kind = (params.get("kind") as DiscoverKind) || "albums";
  const source = params.get("source") || "all";
  const genreParam = params.get("genre") || "";
  const [filter, setFilter] = useState("");

  /** One place the page writes its own URL — every key at once, because two
   *  separate writes would each start from the same `params` snapshot and the
   *  second would drop the first (picking a genre clears the source filter). */
  const write = (patch: Partial<Record<"scope" | "kind" | "source" | "genre", string>>) => {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(patch)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    setParams(next, { replace: true });
  };

  const genresQ = useQuery({
    queryKey: ["discoverGenres", scope],
    queryFn: () => api.discoverGenres(scope),
    // The provider chains behind this are throttled and TTL-cached server-side;
    // a page being read must not re-walk them on every focus.
    staleTime: 5 * 60_000,
    retry: false,
  });
  const genres = genresQ.data?.genres ?? [];
  // No genre in the URL yet: the first one the server listed is what a reader
  // would have picked anyway, and it keeps the right half of the page alive.
  const active = genreParam || genres[0]?.name || "";
  const activeRow = useMemo(() => genres.find((g) => g.name === active), [genres, active]);

  const shown = useMemo(() => {
    const f = filter.trim().toLowerCase();
    return f ? genres.filter((g) => g.name.toLowerCase().includes(f)) : genres;
  }, [genres, filter]);

  const itemsQ = useInfiniteQuery({
    queryKey: ["discoverGenre", active, kind, source, scope],
    queryFn: ({ pageParam }) =>
      api.discoverGenre({ genre: active, kind, source, limit: PAGE, offset: pageParam as number }),
    initialPageParam: 0,
    getNextPageParam: (last, _all, lastParam) => {
      const nx = last.next_offset;
      // Forward only: a cursor that does not move would make "Load more"
      // refetch the page it is already showing. `MAX_OFFSET` is the server's
      // own bound — asking past it is a 400, so the button stops instead.
      if (typeof nx !== "number" || nx <= (lastParam as number) || nx > MAX_OFFSET) return undefined;
      return nx;
    },
    enabled: !!active,
    placeholderData: keepPreviousData, // keep the list up while kind/source change
    staleTime: 5 * 60_000,
    retry: false,
  });

  const pages = itemsQ.data?.pages ?? [];
  const rows = pages.flatMap((p) => p.items);
  const last = pages.at(-1);
  const notes = last?.notes ?? {};
  const asked = last?.sources_asked ?? [];

  // The filter offers what the genre's own answer names, plus whatever the
  // rows and the current URL carry. Ids with no row of their own (a source that
  // answered nothing this time) still get their filter chip.
  const sourceIds = useMemo(() => {
    const ids = new Set<string>();
    for (const s of activeRow?.sources ?? []) ids.add(s);
    for (const s of asked) ids.add(s);
    for (const r of rows) if (r.source) ids.add(r.source);
    if (source !== "all") ids.add(source);
    return [...ids];
  }, [activeRow, asked, rows, source]);
  const labels = useMemo(() => {
    const m = new Map<string, string>();
    for (const r of rows) if (r.source && r.source_label) m.set(r.source, r.source_label);
    return m;
  }, [rows]);

  return (
    <div className="p-6 space-y-5 mx-auto max-w-[1600px]">
      <PageHeader
        icon={Compass}
        title="Discover"
        subtitle="Pick a genre, then read its albums, artists or tracks — from the library, from the online providers, or from both at once. Every row names where it came from and whether you already have it."
        actions={<Segmented value={scope} onChange={(s) => write({ scope: s })} options={SCOPES} />}
      >
        {genresQ.data && <NotesChips notes={genresQ.data.notes} sources={genresQ.data.sources_asked} />}
      </PageHeader>

      {genresQ.isLoading ? (
        <PageLoading label="Reading genres…" />
      ) : genresQ.error ? (
        <EmptyState title="Genres unavailable" hint={String(genresQ.error instanceof Error ? genresQ.error.message : genresQ.error)} />
      ) : genres.length === 0 ? (
        <EmptyState
          title={scope === "library" ? "No genres in the library yet" : "No online genres"}
          hint={
            scope === "library"
              ? "Import genres on an album (Tag actions → Import genres) or run the genre autofill script, then they show up here."
              : "Every online provider the server could reach came back with nothing. The chips above say which ones it asked, and why the rest were skipped."
          }
        />
      ) : (
        <div className="flex flex-col lg:flex-row gap-4 items-start">
          <aside className="panel w-full lg:w-72 shrink-0 space-y-2">
            <input
              className="input !py-1.5"
              placeholder="Filter genres…"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
            <div className="lg:max-h-[65vh] lg:overflow-auto -mx-1 px-1 space-y-0.5">
              {shown.map((g) => (
                <button
                  key={g.name}
                  onClick={() => {
                    // A source filter is a statement about ONE genre's sources.
                    write({ genre: g.name, source: "" });
                  }}
                  className={`w-full text-left rounded-md px-2 py-1.5 transition-colors ${
                    g.name === active ? "bg-accent/15 text-white" : "hover:bg-raise text-zinc-300"
                  }`}
                  title={`Browse ${g.name}`}
                >
                  <span className="flex items-baseline gap-2">
                    <span className="text-sm truncate flex-1">{g.name}</span>
                    <span className="text-[10px] text-zinc-600 tabular-nums shrink-0">
                      {scope === "library" ? `${g.track_count} tracks` : `${g.album_count} albums · ${g.artist_count} artists`}
                    </span>
                  </span>
                  {g.sources.length > 0 && (
                    <span className="text-[10px] text-zinc-600 truncate block">{g.sources.join(" · ")}</span>
                  )}
                </button>
              ))}
              {shown.length === 0 && (
                <div className="text-xs text-zinc-600 py-2">No genre matches “{filter}”.</div>
              )}
            </div>
          </aside>

          <section className="min-w-0 flex-1 w-full space-y-3">
            <div className="panel space-y-3">
              <div className="flex flex-wrap items-center gap-3">
                <h2 className="text-lg font-semibold truncate">{active}</h2>
                {activeRow && (
                  <span className="text-[11px] text-zinc-500">
                    {activeRow.album_count} albums · {activeRow.artist_count} artists · {activeRow.track_count} tracks
                    {activeRow.sources.length > 0 ? ` · ${activeRow.sources.join(", ")}` : ""}
                  </span>
                )}
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Segmented value={kind} onChange={(k) => write({ kind: k })} options={KINDS} />
                <div className="flex flex-wrap items-center gap-1.5">
                  <button
                    className={`chip border transition-colors tap ${
                      source === "all" ? "bg-accent/20 border-accent/50 text-white" : "bg-raise border-border text-zinc-400 hover:text-white"
                    }`}
                    title="Ask every source this server can use"
                    onClick={() => write({ source: "all" })}
                  >
                    All sources
                  </button>
                  {sourceIds.map((id) => (
                    <button
                      key={id}
                      className={`chip border transition-colors tap ${
                        source === id ? "bg-accent/20 border-accent/50 text-white" : "bg-raise border-border text-zinc-400 hover:text-white"
                      }`}
                      title={`Only ${sourceLabel(id, labels.get(id))}`}
                      onClick={() => write({ source: id })}
                    >
                      {sourceLabel(id, labels.get(id))}
                    </button>
                  ))}
                </div>
              </div>
              <NotesChips notes={notes} sources={asked} />
            </div>

            {itemsQ.isLoading ? (
              <PageLoading label={`Asking ${source === "all" ? "every source" : sourceLabel(source, labels.get(source))}…`} />
            ) : rows.length === 0 ? (
              itemsQ.error ? (
                <EmptyState
                  title={`${kind} for “${active}” could not be read`}
                  hint={String(itemsQ.error instanceof Error ? itemsQ.error.message : itemsQ.error)}
                />
              ) : (
                <EmptyState
                  title={`Nothing for “${active}” in ${source === "all" ? "any source" : sourceLabel(source, labels.get(source))}`}
                  hint="A source that had nothing to say is listed above with the server's own reason; one the server could not reach is listed with its failure."
                />
              )
            ) : (
              <div className="panel !p-0 overflow-hidden">
                <div className="flex items-center gap-2 px-3 py-2 border-b border-border/60">
                  <span className="text-[11px] text-zinc-500">
                    {rows.length} {kind} shown
                  </span>
                  {itemsQ.isPlaceholderData && itemsQ.isFetching && (
                    <span className="inline-flex items-center gap-1 text-[11px] text-zinc-500" title="Loading this selection">
                      <Loader2 className="h-3 w-3 animate-spin" /> updating
                    </span>
                  )}
                </div>
                {/* A failed PAGE must not throw away the rows already read: the
                    list stays, and the failure is stated above its end. */}
                {itemsQ.error && (
                  <div className="border-t border-border/60 px-3 py-2 text-[11px] text-amber-300/90">
                    More could not be read: {String(itemsQ.error instanceof Error ? itemsQ.error.message : itemsQ.error)}
                  </div>
                )}
                <ul className="divide-y divide-border/60 stagger">
                  {rows.map((item, i) => (
                    <DiscoverRow key={`${item.source}:${item.kind}:${item.mbid || item.path || item.title}-${i}`} item={item} />
                  ))}
                </ul>
                {itemsQ.hasNextPage && (
                  <div className="border-t border-border/60 px-3 py-2">
                    <button
                      className="btn-ghost !py-1.5 text-xs tap"
                      disabled={itemsQ.isFetchingNextPage}
                      onClick={() => itemsQ.fetchNextPage()}
                    >
                      {itemsQ.isFetchingNextPage && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                      Load {PAGE} more
                    </button>
                  </div>
                )}
              </div>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
