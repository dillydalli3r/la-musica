import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../api";

/** How far ahead of the playing track the queue's data is warmed. Three rows is
 *  a handover's worth of slack: the next track is always warm, and a skip or a
 *  reorder still lands on a row that was read while this one played. */
export const QUEUE_WARM_AHEAD = 3;

/** One queue row's own facts, as the warm needs them — the queue rows the
 *  player holds carry both (`PlayQueueItem`). */
export type WarmRow = { path: string; albumPath?: string | null };

/** Warm the CURRENT track's data and the next few rows' — the tags payload
 *  (title, artist, album, year, tech, lyrics, MBIDs and the public web rating
 *  all ride that one answer), the album payload that names the cover, and the
 *  cover's own colour, which the fullscreen player's ambience is painted from.
 *
 *  Why: a handover is the one moment the player paints a track it has never
 *  shown. Every now-playing surface reads those three through react-query, so a
 *  payload already in the cache is there in the SAME commit that changes the
 *  track — the title, the sub-lines, the tech readout, the lyrics and the
 *  cover's address all land together with no round trip after the paint (the
 *  owner's ask: "integrate song data caching for the current track / next
 *  tracks in queue"). The rows AFTER the next one are warmed in the same call,
 *  so pressing next twice, or a queue that advances on its own, is still warm.
 *
 *  The window re-runs on every track change, queue change and play/pause; a
 *  payload that is already fresh is not fetched again (`staleTime` matches each
 *  consumer's own, so `prefetchQuery` is a cache lookup), and nothing is
 *  fetched at all while the player is paused — a paused queue's rows may never
 *  be played, and the readback this exists for only happens on a handover. */
export function useQueueWarm(queue: WarmRow[], index: number, playing: boolean): void {
  const qc = useQueryClient();
  useEffect(() => {
    if (!playing || !queue.length) return;
    const first = Math.max(0, index);
    const last = Math.min(queue.length, index + 1 + QUEUE_WARM_AHEAD);
    for (let i = first; i < last; i++) {
      const row = queue[i];
      if (!row?.path) continue;
      void qc.prefetchQuery({
        queryKey: ["tags", row.path],
        queryFn: () => api.tags(row.path),
        staleTime: 5 * 60 * 1000,
      });
      const album = row.albumPath;
      if (!album) continue;
      void qc.prefetchQuery({
        queryKey: ["album", album],
        queryFn: () => api.album(album),
        staleTime: 5 * 60 * 1000,
      });
      void qc.prefetchQuery({
        queryKey: ["coverColor", album],
        queryFn: () => api.coverColor(album),
        retry: false,
        staleTime: 10 * 60 * 1000,
      });
    }
  }, [playing, queue, index, qc]);
}