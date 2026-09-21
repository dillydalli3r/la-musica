import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ListPlus, Play, Sparkles } from "lucide-react";
import { api, getToken, serverUrl } from "../api";
import { albumRef, trackRef } from "../lib/refs";
import { toast, useStore, type QueueTrack } from "../store";
import AlbumCard from "./AlbumCard";
import CoverImg, { TrackCover } from "./CoverImg";
import type { Album } from "../types";

/** One row of GET/POST /api/recommend (server/recommend.py). `kind` is what
 *  the row IS, not what was asked for — an artist page is served albums, a
 *  playlist page is served tracks. */
export interface RecommendItem {
  kind: "album" | "track";
  id: string;
  path: string;
  mbid: string | null;
  title: string;
  subtitle: string;
  score: number;
  /** Why the row is here, strongest signal first (shown on hover). */
  reasons: string[];
  /** Album folder the cover belongs to, plus the cover file inside it. */
  cover_path: string;
  cover: string | null;
  /** Track rows only: the file inside its folder, the folder, the release and
   *  the artist — enough to queue the row without a second lookup. */
  file: string | null;
  album_path: string;
  album: string;
  artist: string;
}

/** Which library items a request is asking back. */
export type RecommendTarget = "albums" | "tracks";

export type RecommendKind =
  | "artist" | "album" | "track" | "playlist"
  /** Explicit seed references (paths or `mb:<uuid>`), each a track file, an
   *  album folder or an artist folder. */
  | "tracks" | "albums"
  /** The signed-in user's own likes (track shelf) or favourite albums and
   *  artists (album shelf) — the seeds are read server-side. */
  | "favorites";

/** The shelf's own fetch. Every other call goes through `api.ts`; this asks
 *  the route directly with the same credentials and token `api.ts` would have
 *  sent, and POSTs because a playlist or a favourites set is a seed LIST that
 *  does not belong in a URL. */
async function fetchRecommend(body: {
  kind: RecommendKind;
  id: string;
  target?: RecommendTarget;
  seeds?: string[];
  limit?: number;
}) {
  const token = getToken();
  const r = await fetch(`${serverUrl()}/api/recommend`, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`recommend failed: ${r.status}`);
  return (await r.json()) as { items: RecommendItem[] };
}

/** In-app route for one row: the MBID form when the item is tagged, the path
 *  form otherwise — the same rule every other link in the app follows. */
function refFor(item: RecommendItem): string {
  return item.kind === "track"
    ? trackRef({ path: item.path, tags: { MUSICBRAINZ_TRACKID: item.mbid ?? undefined } })
    : albumRef({ path: item.path, meta: { MUSICBRAINZ_ALBUMID: item.mbid } });
}

/** The queue entry a recommended track plays as. `coverFile` is the row's own
 *  cover (a track's sidecar, else its album's) — the same pair the player bar
 *  reads from every other queue site. */
function queueTrackOf(item: RecommendItem): QueueTrack {
  return {
    path: item.path,
    file: item.file ?? item.path.split("/").pop() ?? item.path,
    albumPath: item.album_path || item.path.split("/").slice(0, -1).join("/"),
    artist: item.artist || undefined,
    album: item.album || undefined,
    title: item.title || undefined,
    coverFile: item.cover,
    albumCover: item.cover,
  };
}

/** Why-row chip: the strongest signal, the rest of them on hover. */
function Reason({ reasons, className = "" }: { reasons: string[]; className?: string }) {
  const first = reasons[0];
  if (!first) return null;
  return (
    <span
      className={`truncate rounded-full px-1.5 py-0.5 text-[10px] leading-tight bg-accent/10 text-accent-soft ${className}`}
      title={reasons.join(" · ")}
    >
      {first}
    </span>
  );
}

/** One recommended track: play, open, queue — the row shape the library and
 *  playlist tables use, in a lighter form. */
function TrackRow({ item }: { item: RecommendItem }) {
  const playNow = useStore((s) => s.playNow);
  const queueAdd = useStore((s) => s.queueAdd);
  const queue = useStore((s) => s.queue);
  const t = useMemo(() => queueTrackOf(item), [item]);
  const enqueue = () => {
    if (!queue.length) {
      playNow([t]);
      return;
    }
    queueAdd([t], "end");
    toast("Added to the queue");
  };
  return (
    <li className="group flex items-center gap-2 py-1.5">
      <button
        className="btn-ghost !px-1.5 !py-1 shrink-0"
        title="Play"
        onClick={() => playNow([t])}
      >
        <Play className="h-3.5 w-3.5" />
      </button>
      <Link to={refFor(item)} className="flex items-center gap-2 min-w-0 flex-1">
        <TrackCover albumPath={item.album_path} trackCover={item.cover} albumCover={item.cover} />
        <span className="min-w-0 flex-1">
          <span className="block text-sm font-medium truncate group-hover:text-accent-soft" title={item.title}>
            {item.title}
          </span>
          <span className="block text-[11px] text-zinc-500 truncate" title={`${item.artist} — ${item.album}`}>
            {item.artist}{item.album ? ` — ${item.album}` : ""}
          </span>
        </span>
      </Link>
      <Reason reasons={item.reasons} className="hidden sm:block shrink-0 max-w-[9rem]" />
      <button
        className="btn-ghost !px-1.5 !py-1 shrink-0 row-hover"
        title="Add to queue"
        onClick={enqueue}
      >
        <ListPlus className="h-3.5 w-3.5" />
      </button>
    </li>
  );
}

/** Fallback tile for an album row the client's library payload does not hold
 *  (a payload that has not caught up with the server's index) — the cover and
 *  the link are still right, only the card chrome is missing. */
function AlbumTile({ item }: { item: RecommendItem }) {
  return (
    <Link to={refFor(item)} className="block group" title={item.reasons.join(" · ")}>
      <CoverImg
        albumPath={item.cover_path || item.path}
        coverFile={item.cover}
        wrapperClass="aspect-square w-full rounded-xl bg-raise border border-border overflow-hidden shadow-lg ring-1 ring-black/40"
      />
      <div className="mt-1.5 px-0.5">
        <div className="text-sm font-medium truncate group-hover:text-accent-soft">{item.title}</div>
        <div className="text-[11px] text-zinc-500 truncate">{item.subtitle}</div>
        <Reason reasons={item.reasons} className="inline-block mt-0.5 max-w-full" />
      </div>
    </Link>
  );
}

/** The recommendation shelf, one component for every context: an artist, an
 *  album, a track, a playlist, an explicit seed list, or the favourites set.
 *
 *  The context only decides the seeds; the server decides whether the answer
 *  is albums or tracks (`target`, defaulting per kind), and the shelf renders
 *  whichever it got — covers in a horizontal scroller for an album shelf, the
 *  native track row for a track shelf. Album rows are rendered through the
 *  shared `AlbumCard` (play, heart, DR/media chips, links) whenever the
 *  client's cached library payload holds that album, so the shelf is the same
 *  object the Library and Favorites grids show.
 *
 *  Renders nothing when there is nothing to suggest (an untagged or empty
 *  library, an id the library no longer holds, no favourites yet), so a page
 *  never shows an empty box. */
export default function MoreLikeThis({
  kind,
  id = "",
  seeds,
  target,
  limit,
  title = "More like this",
}: {
  kind: RecommendKind;
  /** Library path or `mb:<uuid>` — the server resolves either. Required for
   *  the entity kinds, unused by `tracks`/`albums`/`favorites`. */
  id?: string;
  /** Seed references for `kind="tracks"` / `kind="albums"`. */
  seeds?: string[];
  target?: RecommendTarget;
  limit?: number;
  title?: string;
}) {
  // Seeds are the same request whichever way they are spelled; a joined key
  // keeps react-query stable across renders without hashing the list.
  const seedKey = seeds?.join("\u0000") ?? "";
  const enabled = kind === "favorites" || !!id || seedKey !== "";
  const { data } = useQuery({
    queryKey: ["recommend", kind, id, target ?? "", seedKey, limit ?? 0],
    queryFn: () => fetchRecommend({ kind, id, target, seeds, limit }),
    enabled,
    // Scored locally but not for free, and the answer only changes when the
    // library does — a page being read must not re-ask on every focus.
    staleTime: 5 * 60_000,
    retry: false,
  });
  const items = data?.items ?? [];
  const albums = useMemo(() => items.filter((i) => i.kind === "album"), [items]);

  // Only an album shelf needs the full payload, and only to hand `AlbumCard`
  // the real album (tracks, media, grade). The query key is the app's own, so
  // a library the page already loaded is reused instead of fetched again.
  const { data: lib } = useQuery({
    queryKey: ["library"],
    queryFn: api.library,
    enabled: albums.length > 0,
    staleTime: 60_000,
  });
  const albumByPath = useMemo(() => {
    const map = new Map<string, Album>();
    for (const artist of lib?.artists ?? []) {
      for (const al of artist.albums ?? []) map.set(al.path, al);
    }
    return map;
  }, [lib]);

  if (!enabled || items.length === 0) return null;
  const albumShelf = items[0].kind === "album";
  return (
    <section className="section">
      <div className="flex items-center gap-1.5 mb-2">
        <Sparkles className="h-3.5 w-3.5 text-zinc-500" />
        <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-500">{title}</h2>
      </div>
      {albumShelf ? (
        <div className="flex gap-3 overflow-x-auto pb-1 -mx-1 px-1">
          {albums.map((item) => {
            const al = albumByPath.get(item.path);
            return (
              <div key={item.id} className="w-36 sm:w-40 shrink-0">
                {al ? (
                  <AlbumCard al={al} extraMeta={<Reason reasons={item.reasons} className="max-w-[7rem]" />} />
                ) : (
                  <AlbumTile item={item} />
                )}
              </div>
            );
          })}
        </div>
      ) : (
        <ul className="divide-y divide-border/60 stagger">
          {items.map((item) => (
            <TrackRow key={item.id} item={item} />
          ))}
        </ul>
      )}
    </section>
  );
}
