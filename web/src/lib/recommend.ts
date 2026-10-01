/** The local scorer's own row shape and the queue batch built from it.
 *
 *  `server/recommend.py` answers one item shape for every route in the family
 *  (`_item`) — the "Recommended (Local)" shelf on an artist/album/track page
 *  (GET/POST `/api/recommend`, fetched by `components/MoreLikeThis.tsx`) and
 *  the batch that keeps a playing queue going (GET/POST
 *  `/api/recommend/queue`). The shape and the queue row it becomes live here,
 *  in one place, so the two ends cannot drift apart.
 *
 *  Deliberately NOT in `MoreLikeThis.tsx`: the player bar (mounted for the
 *  whole session) needs the mapper, and a lazy page's component module must not
 *  be pulled into the shell's chunk for one function. */
import { getToken, serverUrl } from "../api";
import type { QueueTrack } from "../store";

/** One row of GET/POST `/api/recommend` and `/api/recommend/queue`
 *  (server/recommend.py's `_item`). `kind` is what the row IS, not what was
 *  asked for — an artist page is served albums, a queue batch is served
 *  tracks. */
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

/** The queue entry a recommended track plays as. `coverFile` is the row's own
 *  cover (a track's sidecar, else its album's) — the same pair the player bar
 *  reads from every other queue site. */
export function queueTrackOf(item: RecommendItem): QueueTrack {
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

/** How many rows ONE extension of a playing queue asks for — a handful, so the
 *  queue someone is listening to grows by a batch and not by a library. The
 *  server's own default for the route is this number and it clamps to its
 *  ceiling whatever a caller states (`server/recommend.py`'s
 *  QUEUE_DEFAULT_LIMIT / QUEUE_MAX_LIMIT); tools/test_recommendations.py pins
 *  the two ends equal. */
export const QUEUE_BATCH = 5;

/** How many rows the queue keeps BETWEEN the playing one and its end.
 *
 *  The batch and the runway are the same number on purpose: the player asks
 *  for one more batch while fewer than this many rows are still up next, so
 *  the queue is topped up before the reader reaches its end — the count grows
 *  while there is music to cover (31/36, then 32/37) instead of jumping in the
 *  same breath as the next press (31/32 → 32/37). Keeping it equal to
 *  QUEUE_BATCH means one batch is always enough to restore the runway, so the
 *  ask cannot loop. */
export const QUEUE_RUNWAY = QUEUE_BATCH;

/** The batch that keeps a playing queue going: tracks similar to `paths` (the
 *  queue itself — the seed set AND the exclusion set), answered by the
 *  library's own tags with nothing fetched online.
 *
 *  POSTed rather than GETted because a queue is a seed list of any length (a
 *  playlist's worth of paths does not belong in a URL), with the same
 *  credentials the shelf's own fetch sends. Throws only when the server cannot
 *  be reached: the caller plays music with this answer, so a failure is a queue
 *  that ends where it did before, never an interruption. */
export async function fetchQueueRecommend(paths: string[]): Promise<RecommendItem[]> {
  const token = getToken();
  const r = await fetch(`${serverUrl()}/api/recommend/queue`, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ paths, limit: QUEUE_BATCH }),
  });
  if (!r.ok) throw new Error(`queue recommend failed: ${r.status}`);
  return ((await r.json()) as { items?: RecommendItem[] }).items ?? [];
}
