import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Globe, Loader2 } from "lucide-react";
import { api, type DiscoverKind, type DiscoverSeedKind } from "../api";
import DiscoverRow, { NotesChips } from "./DiscoverRow";

/** How many rows the shelf asks for: a starting point beside the page's own
 *  list, not a second library. */
const LIMIT = 12;

/** What the shelf is, in one line — the ONLINE half of the pair of shelves an
 *  artist/album/track page shows, and the half whose rows came off the
 *  network. It is written as a statement about WHERE the rows come from,
 *  because the local shelf beside it is computed from the user's own tags and
 *  the two must never read as one list. */
const HINT =
  "Fetched from the online providers — every row names the source it came from, and anything you do not own can be added to the library.";

/** The online recommendation shelf for ONE entity: the artist, album or track
 *  page it sits on is the seed, and the server asks every provider that can
 *  speak about that entity (`GET /api/discover/recommended?seed_kind=…`).
 *
 *  It is DELIBERATELY a separate request from the page's own data: the page
 *  renders and is usable first, and the shelf arrives with its own loading
 *  state (one request per page — the fan-out over providers, their caching and
 *  their throttling are the server's business, not the browser's).
 *
 *  Rows are the shared `DiscoverRow`, so a row the library already holds links
 *  into it and a row it does not offers the same add action as anywhere else
 *  in Discover. A shelf with nothing to show says WHY in one line — the
 *  server's own verdict — beside a chip for every source that stayed silent,
 *  so an empty shelf is never a blank box. */
export default function OnlineRecommendations({
  kind,
  seedKind,
  seedMbid = "",
  seedName,
  seedArtist = "",
  title = "Recommended (online)",
  limit = LIMIT,
}: {
  /** The rows the shelf shows — the page's own kind (an artist page: artists). */
  kind: DiscoverKind;
  /** What the page IS: its entity seeds the shelf. */
  seedKind: DiscoverSeedKind;
  /** The entity's MusicBrainz id when its tags carry one. */
  seedMbid?: string;
  /** The entity's own name — an artist's name, an album's or a track's title. */
  seedName: string;
  /** The entity's artist (an album's or a track's; unused by an artist seed). */
  seedArtist?: string;
  title?: string;
  limit?: number;
}) {
  const mbid = seedMbid.trim();
  const name = seedName.trim();
  const artist = seedArtist.trim();
  const { data, isLoading, error } = useQuery({
    queryKey: ["discoverRecommended", seedKind, kind, mbid, name, artist, limit],
    queryFn: () =>
      api.discoverRecommended({ seedKind, kind, limit, seedMbid: mbid, seedName: name, seedArtist: artist }),
    // A page that names nothing has nothing to seed from: the server would say
    // exactly that, and there is no point in asking it to.
    enabled: name !== "",
    // The providers behind this are throttled and TTL-cached server-side; a
    // page being read must not re-walk them on every focus.
    staleTime: 5 * 60_000,
    retry: false,
  });

  const rows = data?.items ?? [];
  const notes = data?.notes ?? {};
  const asked = data?.sources_asked ?? [];
  // The shelf's own verdict ("no recommendation source had anything to suggest
  // for this album") is the one-line WHY when nothing came back, so it is
  // printed as that line rather than as a chip repeating it.
  const verdict = (notes.recommended ?? "").trim();
  const sourceNotes = useMemo(() => {
    const out: Record<string, string> = {};
    for (const [id, note] of Object.entries(notes)) {
      if (id !== "recommended") out[id] = note;
    }
    return out;
  }, [notes]);

  return (
    <section className="section">
      <div className="flex items-center gap-1.5 mb-1">
        <Globe className="h-3.5 w-3.5 text-zinc-500" />
        <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-500">{title}</h2>
      </div>
      <p className="text-[11px] text-zinc-600 mb-2">{HINT}</p>

      {isLoading ? (
        <div className="panel px-3 py-3 flex items-center gap-2 text-[11px] text-zinc-500">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          Asking the providers for what is like this…
        </div>
      ) : error ? (
        <div className="panel px-3 py-3 text-[11px] text-amber-300/90" title={String(error)}>
          The online shelf could not be read: {error instanceof Error ? error.message : String(error)}
        </div>
      ) : rows.length === 0 ? (
        <div className="panel px-3 py-3 space-y-1.5">
          <p className="text-[11px] text-zinc-500" title={verdict}>
            {verdict ||
              (name
                ? "No online provider had anything for this page."
                : "This page states nothing to seed an online shelf from.")}
          </p>
          <NotesChips notes={sourceNotes} sources={asked} />
        </div>
      ) : (
        <div className="panel !p-0 overflow-hidden">
          <div className="flex flex-wrap items-center gap-2 px-3 py-2 border-b border-border/60">
            <span className="text-[11px] text-zinc-500">
              {rows.length} suggestion{rows.length === 1 ? "" : "s"}
            </span>
            {data?.basis && (
              <span className="chip bg-raise border border-border text-zinc-400" title="What these rows were built from">
                basis: {data.basis}
              </span>
            )}
            <NotesChips notes={sourceNotes} sources={asked} />
          </div>
          <ul className="divide-y divide-border/60 stagger">
            {rows.map((item, i) => (
              <DiscoverRow
                key={`${item.source}:${item.kind}:${item.mbid || item.path || item.title}-${i}`}
                item={item}
              />
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
