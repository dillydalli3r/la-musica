/** Which library files a job holds RIGHT NOW — the client half of
 *  server/job_locks.
 *
 *  The registry publishes what it holds at GET /api/jobs/locks (the payload
 *  MAINTAIN → In progress renders): every held path with the identity the
 *  server matches under (`key`), the sentence it refuses a request with
 *  (`why`), and the job's kind/label. This module polls that ONE payload,
 *  keeps the latest answer in a small store, and answers "is this file
 *  playable?" with the registry's own rule — the same path, or inside a held
 *  folder at a separator boundary. That is why the player and the 409 cannot
 *  disagree: both are reading the same keys.
 *
 *  Only what a job holds RIGHT NOW is here. A file merely QUEUED for a job is
 *  not locked and plays exactly as before.
 */
import { useQuery } from "@tanstack/react-query";
import { create } from "zustand";
import { api } from "../api";
import type { JobLock } from "../api";

/** One path a job holds, as the server publishes it. */
export interface LockedPath {
  /** The path as the job claimed it (a folder, usually). */
  path: string;
  /** The lock identity to match a client path against (server.job_locks
   *  normalize(), forward-slashed, case-folded when `fold_case` says so). */
  key: string;
  /** The sentence the server refuses a request for this path with. */
  why: string;
}

/** An in-flight job with the paths it holds (server.job_locks.jobs()). */
export type LockedJob = JobLock & { locked?: LockedPath[] };

/** GET /api/jobs/locks — the whole payload. `fold_case` says whether the keys
 *  were case-folded (Windows), which this side's own match has to apply too. */
export interface LocksPayload {
  jobs: LockedJob[];
  fold_case?: boolean;
}

/** How often the app re-reads the lock list. ONE interval for every surface
 *  that needs it: react-query dedupes on the query key, so the player bar, the
 *  In-progress page and every marked row share one request per tick — the
 *  interval MAINTAIN → In progress already polled at. */
export const LOCKS_POLL_MS = 2000;

/** What each kind of job is called on screen — the registry's own kinds
 *  (server.job_locks callers), in the words MAINTAIN → In progress already
 *  uses for them. A kind this build does not know is shown raw rather than
 *  hidden: a job the page cannot name still holds files. */
export const KIND_LABEL: Record<string, string> = {
  scripts: "Script run",
  import: "Import",
  organize: "Organize",
  tags: "Tag write",
  cover: "Cover write",
  lyrics: "Lyrics write",
  beets: "Beets tagging",
  export: "Export",
  remove: "Remove from library",
};

export const kindLabel = (kind: string | null | undefined) =>
  KIND_LABEL[kind ?? ""] ?? (kind || "Job");

/** A client path in the form the server's keys are in: forward slashes, one
 *  trailing separator dropped, and the same case fold the server applied. */
function matchKey(path: string, fold: boolean): string {
  const slashed = String(path ?? "").replace(/\\/g, "/").replace(/\/+$/, "");
  return fold ? slashed.toLowerCase() : slashed;
}

interface Held {
  job: LockedJob;
  held: LockedPath;
}

interface LocksIndex {
  fold: boolean;
  entries: Held[];
}

const EMPTY: LocksIndex = { fold: false, entries: [] };

function buildIndex(payload: LocksPayload | undefined): LocksIndex {
  const entries: Held[] = [];
  for (const job of payload?.jobs ?? []) {
    for (const held of job.locked ?? []) entries.push({ job, held });
  }
  return { fold: payload?.fold_case === true, entries };
}

/** The registry's containment rule, on the server's own keys: the same path, or
 *  under it at a separator boundary — "…/Album" does not cover "…/Album 2". */
function find(index: LocksIndex, path: string | null | undefined): Held | null {
  if (!path || index.entries.length === 0) return null;
  const key = matchKey(path, index.fold);
  for (const entry of index.entries) {
    const held = entry.held.key;
    if (key === held || key.startsWith(`${held}/`)) return entry;
  }
  return null;
}

/** The latest list, in a store rather than a module variable, so a row can
 *  subscribe to the ONE string it needs and re-render only when that changes —
 *  a poll every 2 s must not repaint a library table. */
export const useLocks = create<{ index: LocksIndex }>(() => ({ index: EMPTY }));

/** The job holding *path* right now, or null — the synchronous question the
 *  play path asks before handing a URL to an <audio> element (a locked track
 *  answers with the server's sentence instead of silence). */
export function heldBy(path: string | null | undefined): Held | null {
  return find(useLocks.getState().index, path);
}

/** A row's subscription: one primitive out of the lock list, so a poll that
 *  changes nothing re-renders nothing. */
function useHeldField(
  path: string | null | undefined,
  pick: (hit: Held) => string
): string {
  return useLocks((s) => {
    const hit = find(s.index, path);
    return hit ? pick(hit) : "";
  });
}

/** The server's refusal sentence for this path, "" while it is playable. */
export const useLockWhy = (path: string | null | undefined) =>
  useHeldField(path, (hit) => hit.held.why);

/** The job's own name ("Optimize FLACs"), "" while the path is playable. */
export const useLockLabel = (path: string | null | undefined) =>
  useHeldField(path, (hit) => hit.job.label);

/** What KIND of work holds this path, in the app's words, "" while playable. */
export const useLockKind = (path: string | null | undefined) =>
  useHeldField(path, (hit) => kindLabel(hit.job.kind));

/** The one poll. Mounted by the player bar (always on screen) and by the pages
 *  that render the list; the shared query key means N callers, one request. */
export function useJobLocks() {
  return useQuery({
    queryKey: ["jobLocks"],
    queryFn: async () => {
      const payload = (await api.jobLocks()) as LocksPayload;
      // The same answer the hooks above read, kept where a non-React caller
      // (the play store, the player's own load) can reach it synchronously.
      useLocks.setState({ index: buildIndex(payload) });
      return payload;
    },
    refetchInterval: LOCKS_POLL_MS,
  });
}
