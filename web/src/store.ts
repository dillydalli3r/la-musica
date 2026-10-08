import { create } from "zustand";

export type ToastSeverity = "info" | "success" | "error";

export interface ToastItem {
  id: number;
  message: string;
  severity: ToastSeverity;
}

const TOAST_MAX = 4;
const TOAST_TTL_MS = 3000;
const TOAST_TTL_ERROR_MS = 6000;

/** The single-bar shape: what one progress bar draws. `steps` is the
 *  whole-step pair a chained script run publishes ("script 3 of 18") —
 *  readouts print that instead of the fractional done/total the bar is drawn
 *  from. */
export interface ProgressSample {
  done: number;
  total: number;
  desc: string;
  steps?: number[];
}

/** A frame off /ws/progress as this build reads it. The producer's identity
 *  (`job`/`kind`/`label`) is what the shell needs to give each running job its
 *  own bar; an older server sends the numbers alone and still gets a bar. */
export interface ProgressFrame {
  job?: number | string | null;
  kind?: string | null;
  label?: string | null;
  done: number;
  total: number;
  desc: string;
  steps?: number[];
}

/** One live producer's bar, as the map holds it. A frame updates its own
 *  entry and nothing else's, so an export and a run are on screen together
 *  instead of overwriting one another in a single slot. */
export interface ProgressEntry extends ProgressSample {
  /** The producer's id, as /api/jobs/locks lists it ("" for the legacy frame
   *  that names none). */
  job: string;
  /** "export" | "run" | "script" ("" when the frame names none). */
  kind: string;
  /** The producer's own name, "" when the frame names none. */
  label: string;
  /** When the last frame landed (ms). Only the fallback removal reads it, and
   *  it is the half that keeps a merely SLOW job on screen: an entry is never
   *  dropped for standing still while its job is still listed. */
  t: number;
}

/** Where a frame lands in the map: its producer's own id, so two jobs are two
 *  bars. A frame that names no id is keyed by its kind — which is what the
 *  relay published before producers were named — and one that names neither
 *  keeps the single legacy slot, so an old server still shows its bar. */
export function progressKey(f: { job?: number | string | null; kind?: string | null }): string {
  const job = f.job === undefined || f.job === null ? "" : String(f.job);
  if (job) return `j:${job}`;
  return `k:${f.kind ? String(f.kind) : "legacy"}`;
}

/** The newest entry, for the surfaces that draw ONE bar (an entry IS the
 *  single-bar shape, so the newest one is the answer without a copy). */
function newest(m: Record<string, ProgressEntry>): ProgressSample | null {
  let top: ProgressEntry | null = null;
  for (const e of Object.values(m)) if (!top || e.t >= top.t) top = e;
  return top;
}

/** How long a job must be BOTH missing from /api/jobs/locks and quiet before
 *  its bar is dropped. Neither half alone is proof of anything: a stalled
 *  export still holds its locks and keeps its bar, and a job between two
 *  frames is not gone. */
const PROGRESS_GONE_MS = 5000;

/** The id-less legacy frame never gets a `progress_end` — the relay did not
 *  send one. The old client cleared it 2.5 s after a total-complete frame, and
 *  that is still the only thing that ends it; kept as a rule the fallback
 *  sweep applies rather than a timer armed per frame. */
const PROGRESS_LEGACY_DONE_MS = 2500;

/** …and how long an id-less bar may stay QUIET before it is dropped anyway.
 *  The total-complete rule above is not enough for a producer that ticks
 *  without numbers at all: the Beets page's own import published 0/0 frames
 *  (they have an owner now — server/beetscfg.py — but a third-party or older
 *  producer still might), and such a bar sat on screen for good. A minute with
 *  no frame and no job in `/api/jobs/locks` is stale by any reading; a NAMED
 *  job is exempt, because a stalled export still holds its locks. */
const PROGRESS_LEGACY_QUIET_MS = 60000;

interface Store {
  /** Every live producer's bar, keyed by `progressKey`. Frames land here and
   *  only `progress_end` (or the fallback in `pruneProgress`) clears one: a
   *  bar must not vanish while its job is still working. */
  progresses: Record<string, ProgressEntry>;
  /** The single-bar view of the map, for the surfaces that draw ONE strip (the
   *  optimization page, the import wizard). Derived on every write, so the map
   *  stays the only place a frame is stored. */
  progress: ProgressSample | null;
  /** One relay frame: upserts its producer's entry. */
  setProgress: (p: ProgressFrame) => void;
  /** `progress_end` — the producer said it is done; its bar goes. */
  dropProgress: (job: number | string) => void;
  /** The fallback for a producer that died without `progress_end`: drop the
   *  entries whose job the lock registry (`live`) no longer lists whose
   *  numbers have also stopped moving, plus — those producers send no end
   *  frame at all — the id-less legacy entry once it reports its own
   *  completion and stops. */
  pruneProgress: (live: string[], now: number) => void;
  query: string;
  setQuery: (q: string) => void;
  sort: { key: string; dir: 1 | -1 } | null;
  setSort: (s: { key: string; dir: 1 | -1 } | null) => void;
  filter: Record<string, unknown>;
  setFilter: (f: Record<string, unknown>) => void;
  /** Toasts queue instead of overwriting each other; the shell renders them
   *  stacked, each with its own severity and dismiss control. `updateToast`
   *  rewrites one that is still on screen — the press acknowledgement an
   *  action showed before its request settled, replaced by the server's own
   *  answer when it lands, WITHOUT a second toast beside it. */
  toasts: ToastItem[];
  addToast: (t: ToastItem) => void;
  updateToast: (id: number, message: string, severity: ToastSeverity) => void;
  dismissToast: (id: number) => void;
  selection: { tracks: string[]; albums: string[]; artists: string[] };
  setSelection: (s: Partial<{ tracks: string[]; albums: string[]; artists: string[] }>) => void;
  toggleTrack: (p: string) => void;
  toggleAlbum: (p: string) => void;
  toggleArtist: (p: string) => void;
  clearSelection: () => void;
}

export const useStore = create<Store>((set) => ({
  progresses: {},
  progress: null,
  setProgress: (p) =>
    set((st) => {
      const key = progressKey(p);
      const prev = st.progresses[key];
      const job = p.job === undefined || p.job === null ? "" : String(p.job);
      const entry: ProgressEntry = {
        job,
        // Identity a frame omits keeps what the last one said: the relay sends
        // a step change as two frames (with the pair, then without), and the
        // second must not blank the label the first just put on the row.
        kind: p.kind ? String(p.kind) : prev?.kind ?? "",
        label: p.label ? String(p.label) : prev?.label ?? "",
        done: p.done,
        total: p.total,
        desc: p.desc,
        steps: p.steps ?? prev?.steps,
        t: Date.now(),
      };
      return { progresses: { ...st.progresses, [key]: entry }, progress: entry };
    }),
  dropProgress: (job) =>
    set((st) => {
      const key = progressKey({ job });
      if (!(key in st.progresses)) return {};
      const progresses = { ...st.progresses };
      delete progresses[key];
      return { progresses, progress: newest(progresses) };
    }),
  pruneProgress: (live, now) =>
    set((st) => {
      const listed = new Set(live);
      const kept: Record<string, ProgressEntry> = {};
      let dropped = false;
      for (const [key, e] of Object.entries(st.progresses)) {
        const gone = e.job
          ? !listed.has(e.job) && now - e.t >= PROGRESS_GONE_MS
          : (e.total > 0 && e.done >= e.total && now - e.t >= PROGRESS_LEGACY_DONE_MS) ||
            now - e.t >= PROGRESS_LEGACY_QUIET_MS;
        if (gone) dropped = true;
        else kept[key] = e;
      }
      return dropped ? { progresses: kept, progress: newest(kept) } : {};
    }),
  query: "",
  setQuery: (query) => set({ query }),
  sort: null,
  setSort: (sort) => set({ sort }),
  filter: {},
  setFilter: (filter) => set({ filter }),
  toasts: [],
  addToast: (t) => set((st) => ({ toasts: [...st.toasts, t].slice(-TOAST_MAX) })),
  updateToast: (id, message, severity) =>
    set((st) => ({
      toasts: st.toasts.map((t) => (t.id === id ? { ...t, message, severity } : t)),
    })),
  dismissToast: (id) => set((st) => ({ toasts: st.toasts.filter((t) => t.id !== id) })),
  selection: { tracks: [], albums: [], artists: [] },
  setSelection: (s) => set((st) => ({ selection: { ...st.selection, ...s } })),
  toggleTrack: (p) =>
    set((st) => {
      const tracks = st.selection.tracks.includes(p)
        ? st.selection.tracks.filter((x) => x !== p)
        : [...st.selection.tracks, p];
      return { selection: { ...st.selection, tracks } };
    }),
  toggleAlbum: (p) =>
    set((st) => {
      const albums = st.selection.albums.includes(p)
        ? st.selection.albums.filter((x) => x !== p)
        : [...st.selection.albums, p];
      return { selection: { ...st.selection, albums } };
    }),
  toggleArtist: (p) =>
    set((st) => {
      const artists = st.selection.artists.includes(p)
        ? st.selection.artists.filter((x) => x !== p)
        : [...st.selection.artists, p];
      return { selection: { ...st.selection, artists } };
    }),
  clearSelection: () => set({ selection: { tracks: [], albums: [], artists: [] } }),
}));

let toastSeq = 0;

/** Dismiss timer per toast id. Kept so `toast.update` can put the server's own
 *  answer in the place of the acknowledgement a press showed: without
 *  re-arming, the FIRST timer (3 s, started when the press happened) would pull
 *  the toast off screen mid-answer. */
const toastTimers = new Map<number, number>();

function armToast(id: number, severity: ToastSeverity) {
  clearTimeout(toastTimers.get(id));       // no-op when this id has no timer
  toastTimers.set(
    id,
    window.setTimeout(() => {
      toastTimers.delete(id);
      useStore.getState().dismissToast(id);
    }, severity === "error" ? TOAST_TTL_ERROR_MS : TOAST_TTL_MS)
  );
}

/** Severity-aware toast: `toast(msg)` is informational, `toast.error(msg)` /
 *  `toast.success(msg)` are the explicit variants. Toasts queue (never
 *  overwrite), each dismisses on its own timer — errors stay twice as long —
 *  and any of them can be dismissed by hand from the shell.
 *
 *  All of them return the toast's id, and `toast.update(id, msg, severity)`
 *  rewrites that toast in place with a fresh timer: that is how a press is
 *  acknowledged at once (the id it gets) and then answered by what the server
 *  actually said, in one toast rather than two. */
type ToastFn = {
  (message: string, severity?: ToastSeverity): number;
  info: (message: string) => number;
  success: (message: string) => number;
  error: (message: string) => number;
  update: (id: number, message: string, severity?: ToastSeverity) => void;
};

function pushToast(message: string, severity: ToastSeverity) {
  // Ids are never reused, so the timer map needs no cross-checking: an id whose
  // toast already left the queue is a no-op filter on dismiss and a stale
  // entry nothing reads.
  const id = ++toastSeq;
  useStore.getState().addToast({ id, message, severity });
  armToast(id, severity);
  return id;
}

export const toast: ToastFn = Object.assign(
  (message: string, severity: ToastSeverity = "info") => pushToast(message, severity),
  {
    info: (message: string) => pushToast(message, "info"),
    success: (message: string) => pushToast(message, "success"),
    error: (message: string) => pushToast(message, "error"),
    update: (id: number, message: string, severity: ToastSeverity = "info") => {
      useStore.getState().updateToast(id, message, severity);
      armToast(id, severity);
    },
  }
);