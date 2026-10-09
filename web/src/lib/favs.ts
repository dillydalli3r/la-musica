import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { toast } from "../store";

export type FavKind = "album" | "artist" | "playlist";

export interface Favorites {
  albums: string[];
  artists: string[];
  playlists: string[];
}

/** API payloads use plural keys; the heart kind is singular. */
const PLURAL: Record<FavKind, keyof Favorites> = {
  album: "albums",
  artist: "artists",
  playlist: "playlists",
};

/** The toast pair the PLAYER's hearts share, ready to spread into `FavHeart`.
 *
 *  A heart in a library row toggles silently — a toast per row would be noise —
 *  but the heart under the transport (and the fullscreen player's copy of it)
 *  names the track it just changed, and a write that failed says so instead of
 *  leaving a heart that looks switched. `title` is a getter so the name is the
 *  one current at the moment of the toggle, not the one captured on render.
 *
 *  Both player homes use THIS function, so the bar and the fullscreen player
 *  cannot end up wording the same toggle differently. */
export function likeToasts(title: () => string) {
  return {
    onToggled: (nowLiked: boolean) => toast(`${nowLiked ? "Liked" : "Unliked"} — ${title()}`),
    onError: (e: unknown) => toast.error(String(e)),
  };
}

export function useFavorites() {
  return useQuery({ queryKey: ["favorites"], queryFn: api.favorites, staleTime: 30_000 });
}

export function useTrackLikes() {
  return useQuery({ queryKey: ["likes"], queryFn: api.likes, staleTime: 30_000 });
}

/** Shared heart state for one entity. `kind: "track"` uses the backend's
 * liked-tracks store; the rest use the favorites table. Passing the
 * entity's MusicBrainz ID lets the backend keep the entry alive across
 * file moves.
 *
 * This is THE way a favourite is written in this app — the rows' hearts, the
 * player bar's, the fullscreen player's and the iOS star (lib/iosFavs.ts) all
 * call it, so they cannot drift into four different ideas of what liking a
 * track does: one optimistic patch of the same `["likes"]`/`["favorites"]`
 * keys, one invalidation of both, one error path.
 *
 * `opts` carries what a call SITE wants to say about the outcome (the player
 * toasts, a row in a library does not); it never changes what is written. */
/** Take several favourites off in ONE action — the Favorites page's select
 *  mode, and the bulk half of what `useFav` does one entity at a time.
 *
 *  Every write is the same call the heart makes (`api.likeToggle` for a track,
 *  `api.favoriteToggle` for an album/artist/playlist) and the two query keys
 *  are invalidated ONCE at the end: a `useFav` per item would patch the same
 *  cache N times and re-render the list between ticks, which is exactly the
 *  flicker a select mode exists to avoid. The caller passes ids it is SHOWING
 *  as favourites, so a toggle can only ever take one off; a write that failed
 *  is counted and reported rather than thrown, because the rest of the batch
 *  still has to happen.
 *
 *  Returns how many the server took and how many it refused. */
export async function unfavoriteMany(
  kind: FavKind | "track",
  ids: string[],
  qc: QueryClient,
): Promise<{ removed: number; failed: number }> {
  let removed = 0;
  let failed = 0;
  for (const id of ids) {
    try {
      if (kind === "track") await api.likeToggle(id);
      else await api.favoriteToggle(kind, id);
      removed += 1;
    } catch {
      failed += 1;
    }
  }
  qc.invalidateQueries({ queryKey: ["likes"] });
  qc.invalidateQueries({ queryKey: ["favorites"] });
  return { removed, failed };
}

export function useFav(
  kind: FavKind | "track",
  id: string | undefined | null,
  mbid?: string | null,
  opts?: {
    /** The entity's new state, once the server agreed. */
    onToggled?: (liked: boolean) => void;
    /** The write failed (offline, a dead session, a locked file). Reports the
     *  error and leaves the store to the invalidation — a heart that could not
     *  be written must not claim it was. */
    onError?: (e: unknown) => void;
  },
) {
  const qc = useQueryClient();
  const favs = useFavorites();
  const likes = useTrackLikes();
  const isTrack = kind === "track";
  const fav = isTrack
    ? !!id && (likes.data?.paths ?? []).includes(id)
    : !!id && !!favs.data && ((favs.data[PLURAL[kind]] ?? []) as string[]).includes(id);
  const mutation = useMutation({
    mutationFn: async () => {
      if (!id) throw new Error("nothing to favorite");
      return isTrack ? api.likeToggle(id, mbid ?? undefined) : api.favoriteToggle(kind as FavKind, id, mbid ?? undefined);
    },
    onSuccess: (r) => {
      const on = "liked" in r ? r.liked : r.fav;
      // Optimistic single-entity update; the invalidation re-syncs lists.
      qc.setQueryData(["likes"], (old: { paths: string[] } | undefined) =>
        !isTrack || !old ? old : { paths: on ? [id!, ...old.paths] : old.paths.filter((p) => p !== id) }
      );
      qc.setQueryData(["favorites"], (old: Favorites | undefined) => {
        if (isTrack || !old) return old;
        const key = PLURAL[kind];
        const list: string[] = old[key] ?? [];
        return { ...old, [key]: on ? [id!, ...list] : list.filter((k) => k !== id) } as Favorites;
      });
      opts?.onToggled?.(on);
    },
    onError: (e) => opts?.onError?.(e),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["likes"] });
      qc.invalidateQueries({ queryKey: ["favorites"] });
    },
  });
  return { fav, toggle: () => id && mutation.mutate(), pending: mutation.isPending };
}
