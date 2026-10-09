import { useQuery } from "@tanstack/react-query";

import { api } from "../api";
import type { SlskQueueItem, SlskQueuePayload } from "../api";
import { useLiveTransfers, type TransfersFrame } from "./notifications";

/** The ONE download queue's cache key.
 *
 *  The Soulseek page's rows and the Library/Album pages' stage badges read the
 *  SAME entry, so there is one interval behind both and no way for one album
 *  to be "Downloading" on one page and something else on another. */
export const SOULSEEK_QUEUE_KEY = ["soulseekQueue"];

/** How a stage is NAMED, in the queue's own vocabulary.
 *
 *  These are `server/soulseek_auto.py`'s STAGES plus the one name that is not
 *  (an "Add to library" that answered before MusicBrainz did —
 *  `server/pending_albums.STAGE_RESOLVING`), so every surface that names a
 *  stage takes the word from here: the Soulseek page's chips, a Library row's
 *  badge, the album header. A second spelling beside this one is what makes a
 *  page describe an album differently from the page that started it. */
export const STAGE_LABEL: Record<string, string> = {
  queued: "Queued",
  searching: "Searching",
  searching_musicbrainz: "Searching MusicBrainz…",
  downloading: "Downloading",
  verifying: "Verifying",
  importing: "Importing",
  completed: "Completed",
  failed: "Failed",
  needs_attention: "Needs you",
  // The fallback walk's resting phase (server/wishes' `background` status,
  // spec R153): the release asked every ranked edition it may and none
  // answered, so it is NOT given up on — it keeps its place and is searched
  // again on the worker's own ticks. Its own word, because "Needs you" would
  // be false and "Queued" would hide that something is still being watched.
  background: "Background",
};

/** Where an added album's acquisition is, as a page draws it. */
export interface Acquisition {
  /** The stage key (`STAGES`, or `searching_musicbrainz`). */
  stage: string;
  /** Its name, from STAGE_LABEL. */
  label: string;
  /** Byte share (0-100) while bytes are moving, else null — never a guess. */
  percent: number | null;
}

/** Two paths naming the same folder: Windows hands one back with either slash,
 *  and the app writes forward slashes into a marker and into the queue. */
function normPath(path: string): string {
  return String(path || "").replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
}

/** The queue row about one album folder.
 *
 *  The join is the FRAMEWORK FOLDER: "Add to library" writes it
 *  (`server/pending_albums`) and the queue's row names the same folder as its
 *  `album_path`, so a path is what both sides already have. `wish_id` is the
 *  second try rather than the first because it is the id the add itself wrote
 *  into the marker — it survives a chain step renaming the album, which the
 *  path does not. */
function queueRowAbout(path: string, wishId: number | null | undefined, queue?: SlskQueuePayload): SlskQueueItem | null {
  const sections = Object.values(queue?.sections ?? {}) as SlskQueueItem[][];
  const want = normPath(path);
  if (want) {
    for (const rows of sections) {
      for (const row of rows) if (row.album_path && normPath(row.album_path) === want) return row;
    }
  }
  if (wishId !== null && wishId !== undefined) {
    for (const rows of sections) {
      for (const row of rows) if (row.wish_id === wishId) return row;
    }
  }
  return null;
}

/** The stage of one album, from the shared queue payload plus the pushed
 *  frames: the queue says WHICH row is this album's (and what that row's state
 *  is), the frame says where that row's job is RIGHT NOW — the same 2.5 Hz push
 *  the Soulseek page's bars are drawn from, so a stage change reaches this
 *  badge at the speed of the transfer, not of a poll. */
export function acquisitionOf(
  path: string,
  wishId: number | null | undefined,
  queue: SlskQueuePayload | undefined,
  frame: TransfersFrame | null,
): Acquisition | null {
  const row = queueRowAbout(path, wishId, queue);
  if (!row) return null;
  const live = row.job_id === null || row.job_id === undefined
    ? null
    : (frame?.jobs ?? []).find((j) => j.id === row.job_id) ?? null;
  const stage = live?.stage_key || row.stage || "";
  if (!stage) return null;
  const percent = live?.progress?.percent ?? row.progress?.percent ?? null;
  return { stage, label: STAGE_LABEL[stage] ?? stage, percent: typeof percent === "number" ? percent : null };
}

/** The acquisitions of the albums a page is showing.
 *
 *  The queue payload is fetched ONLY while that page has something pending: a
 *  library with nothing on its way asks the queue nothing at all, and a
 *  library that does have something shares one cache entry with the Soulseek
 *  page (see SOULSEEK_QUEUE_KEY) instead of polling a second copy of it. */
export function useAcquisitions(enabled: boolean) {
  const { data: queue } = useQuery({
    queryKey: SOULSEEK_QUEUE_KEY,
    queryFn: api.queue,
    enabled,
    refetchInterval: enabled ? 10000 : false,
  });
  const frame = useLiveTransfers();
  return (path: string, wishId?: number | null) => acquisitionOf(path, wishId, queue, frame);
}
