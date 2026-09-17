import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Disc3, ExternalLink, Heart, Loader2 } from "lucide-react";
import { api } from "../api";
import { toast } from "../store";
import type { DiscoveryRow } from "../types";

type Kind = "album" | "track" | "artist";

/** MusicBrainz browser search type per discovery kind — the app's own route,
 *  so an unowned row opens the in-app browser instead of musicbrainz.org. */
const MB_TYPE: Record<Kind, string> = {
  album: "release-group",
  track: "recording",
  artist: "artist",
};

/** In-app MusicBrainz target for a row: the entity page when the id is known,
 *  the browser's search otherwise (Deezer rows carry no MBID). */
function mbHref(row: DiscoveryRow, kind: Kind): string {
  if (row.mbid) {
    if (kind === "artist") return `/mb/artist/${row.mbid}`;
    if (kind === "album") return `/mb/rg/${row.mbid}`;
  }
  const q = row.name ?? [row.artist, row.title].filter(Boolean).join(" ");
  return `/mb/search?q=${encodeURIComponent(q)}&type=${MB_TYPE[kind]}`;
}

/** Provider artwork with a placeholder when the URL is missing or 404s. */
function Thumb({ url, className }: { url: string | null | undefined; className: string }) {
  const [failed, setFailed] = useState(false);
  if (!url || failed)
    return (
      <div className={`${className} flex items-center justify-center text-zinc-700 bg-raise`}>
        <Disc3 className="h-1/3 w-1/3" />
      </div>
    );
  return (
    <img
      src={url}
      alt=""
      loading="lazy"
      decoding="async"
      onError={() => setFailed(true)}
      className={`${className} object-cover group-hover:scale-105 transition-transform duration-200`}
    />
  );
}

/** One "more like this" card: provider art, title/artist, why it is here, and
 * either a link into the library or the wish + MusicBrainz handoff. */
function Card({
  row,
  kind,
  wishing,
  onWish,
}: {
  row: DiscoveryRow;
  kind: Kind;
  wishing: boolean;
  onWish: (row: DiscoveryRow) => void;
}) {
  const name = row.name ?? row.title ?? "";
  const sub = row.name ? row.country ?? (row.tags ?? [])[0] ?? "" : row.artist ?? "";
  const owned = kind !== "artist" && !!row.owned_path;
  const href = owned ? `/album/${encodeURIComponent(row.owned_path as string)}` : mbHref(row, kind);
  // Provider artwork, served by the app (see `api.artUrl`); the row's own
  // artist/title/MBID is what the backend's fallback is asked about.
  const art = api.artUrl(row.image ?? row.cover, {
    artist: row.artist,
    album: row.title,
    rg: kind === "album" ? row.mbid : null,
  });
  return (
    <div className="group w-[8.5rem] shrink-0" title={`${name}${sub ? ` — ${sub}` : ""}`}>
      <Link
        to={href}
        className="block relative h-[8.5rem] w-[8.5rem] rounded-lg overflow-hidden bg-raise border border-border hover:border-zinc-600 transition-colors"
      >
        <Thumb url={art} className="h-full w-full" />
        {(row.year || row.popularity_label) && (
          <span className="absolute bottom-1 left-1 chip !px-1.5 !py-0 text-[9px] bg-black/70 border border-white/10 text-zinc-300">
            {row.year || row.popularity_label}
          </span>
        )}
      </Link>
      <div className="mt-1.5 min-w-0">
        <Link
          to={href}
          className="block text-xs font-medium truncate hover:text-accent-soft transition-colors"
          title={name}
        >
          {name}
        </Link>
        {sub && (
          <div className="text-[11px] text-zinc-500 truncate" title={sub}>
            {sub}
          </div>
        )}
        <div className="text-[10px] text-zinc-600 truncate" title={row.reason ?? undefined}>
          {row.reason ?? (kind === "artist" && row.popularity_label) ?? ""}
        </div>
        <div className="mt-1 flex items-center gap-1.5 h-5">
          {owned ? (
            <span className="text-[10px] text-emerald-400/80" title={row.owned_path ?? undefined}>
              In library
            </span>
          ) : (
            <>
              {kind !== "artist" && (
                <button
                  className="inline-flex items-center gap-1 text-[10px] text-zinc-500 hover:text-accent-soft disabled:opacity-50"
                  disabled={wishing}
                  onClick={() => onWish(row)}
                  title="Add to the wish list (downloads via Soulseek)"
                >
                  {wishing ? <Loader2 className="h-3 w-3 animate-spin" /> : <Heart className="h-3 w-3" />}
                  Wish
                </button>
              )}
              <Link
                to={mbHref(row, kind)}
                className="inline-flex items-center gap-1 text-[10px] text-zinc-600 hover:text-zinc-300"
                title="Open in the MusicBrainz browser"
              >
                <ExternalLink className="h-3 w-3" /> MB
              </Link>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

/** "More like this" row for album / track / artist pages. Renders
 * independently of the host page — a provider failure yields the empty state
 * rather than blocking the page, and nothing renders without an artist. */
export default function MoreLikeThis({
  kind,
  artist,
  title,
  album,
  mbid,
  limit = 12,
  heading,
}: {
  kind: Kind;
  artist: string;
  /** Track title (kind="track") or album title (kind="album"). */
  title?: string;
  album?: string;
  /** MusicBrainz release-group (album) or artist id — sharpens the match. */
  mbid?: string;
  limit?: number;
  heading?: string;
}) {
  const [wishing, setWishing] = useState<string | null>(null);
  const qc = useQueryClient();
  const artistName = artist.trim();
  const { data, isLoading, error } = useQuery({
    queryKey: ["similar", kind, artistName, title ?? "", album ?? "", mbid ?? ""],
    queryFn: () => api.discoverySimilar(kind, artistName, { title, album, mbid, limit }),
    enabled: !!artistName,
    retry: false,
    staleTime: 10 * 60 * 1000,
  });
  const wish = useMutation({
    mutationFn: (row: DiscoveryRow) =>
      api.discoveryWish({
        artist: row.artist ?? "",
        title: row.title ?? "",
        year: row.year ?? undefined,
        mbid: row.mbid ?? undefined,
        note: "Added from “more like this”",
      }),
    onSuccess: (r) => {
      toast(`Wished: ${r.wish.title || r.wish.artist || "album"}`);
      // the wish list is its own query (shared with the home shelves)
      qc.invalidateQueries({ queryKey: ["wishes"] });
    },
    onError: (e) => toast.error(String(e)),
  });
  if (!artistName) return null;

  const label = heading ?? (kind === "artist" ? "Similar artists" : kind === "track" ? "More like this" : "Similar albums");
  const rows = data?.rows ?? [];
  const addWish = (row: DiscoveryRow) => {
    setWishing(row.title ?? "");
    wish.mutate(row, { onSettled: () => setWishing(null) });
  };

  return (
    <section className="space-y-2">
      <div className="flex items-baseline gap-2">
        <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-500">{label}</h2>
        {rows.length > 0 && <span className="text-[10px] text-zinc-600">{rows.length}</span>}
      </div>
      {isLoading ? (
        <div className="flex items-center gap-2 py-4 text-xs text-zinc-600">
          <Loader2 className="h-3.5 w-3.5 animate-spin" /> Finding similar {kind}s…
        </div>
      ) : rows.length === 0 ? (
        <div className="text-xs text-zinc-600 py-3">
          {error
            ? "Similar music is unavailable right now."
            : `No similar ${kind}s found${artistName ? ` for ${artistName}` : ""}.`}
        </div>
      ) : (
        <div className="flex gap-3 overflow-x-auto pb-2 -mx-1 px-1">
          {rows.map((row) => (
            <Card
              key={`${row.source}-${row.mbid ?? row.deezer_id ?? row.name ?? row.title}`}
              row={row}
              kind={kind}
              wishing={wishing !== null && wishing === (row.title ?? "")}
              onWish={addWish}
            />
          ))}
        </div>
      )}
    </section>
  );
}
