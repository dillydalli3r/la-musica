import { create } from "zustand";

export interface QueueTrack {
  path: string;
  file: string;
  albumPath: string;
  artist?: string;
  album?: string;
  title?: string; // TITLE tag — the player bar must never fall back to the file name while the tag exists
  /** Per-track sidecar cover filename (e.g. "01 - Song.jpg"); falls back to the album cover. */
  coverFile?: string | null;
  /** Album cover filename fallback when the track has no own cover. */
  albumCover?: string | null;
  /** ITUNESADVISORY as the row that queued the track knew it ("1" explicit,
   *  "2" clean edition). Carried in the entry so the E/C mark paints on the
   *  same frame as the title — the per-track tags fetch lands later, and the
   *  mark must not wait for it. Refreshed by that fetch when it has a value. */
  advisory?: string | null;
}

/** Severity drives the toast's colours and its screen-reader role: errors
 *  announce as `role="alert"`, everything else as a polite status. */
export type ToastSeverity = "info" | "success" | "error";

export interface ToastItem {
  id: number;
  message: string;
  severity: ToastSeverity;
}

const TOAST_MAX = 4;
const TOAST_TTL_MS = 3000;
const TOAST_TTL_ERROR_MS = 6000;

interface Store {
  /** Live progress frame from the engine relay. `steps` is the whole-step
   *  pair a chained script run publishes ("script 3 of 18") — readouts print
   *  that instead of the fractional done/total the bar is drawn from. */
  progress: { done: number; total: number; desc: string; steps?: number[] } | null;
  setProgress: (p: { done: number; total: number; desc: string; steps?: number[] } | null) => void;
  playing: string | null;
  setPlaying: (p: string | null) => void;
  queue: QueueTrack[];
  setQueue: (q: QueueTrack[]) => void;
  /** Append to the queue without restarting the current track. */
  queueAdd: (tracks: QueueTrack[], position?: "next" | "end") => void;
  /** Remove one queue row (queue popover ✕) without interrupting playback —
   * deliberately does not bump queueId. Removing a row before the playing
   * one shifts the index so the same track keeps playing. */
  queueRemoveAt: (i: number) => void;
  /** Move one queue row onto another position (drag & drop) without
   * interrupting playback — no queueId bump; the index follows the playing
   * track when it is the one being moved. */
  queueMove: (from: number, to: number) => void;
  index: number;
  setIndex: (i: number) => void;
  queueId: number; // bumped on every queue replacement — player reloads even
  // when the new queue starts at the same index
  playNow: (q: QueueTrack[], i?: number) => void;
  query: string;
  setQuery: (q: string) => void;
  sort: { key: string; dir: 1 | -1 } | null;
  setSort: (s: { key: string; dir: 1 | -1 } | null) => void;
  filter: Record<string, unknown>;
  setFilter: (f: Record<string, unknown>) => void;
  /** Toasts queue instead of overwriting each other; the shell renders them
   *  stacked, each with its own severity and dismiss control. */
  toasts: ToastItem[];
  addToast: (t: ToastItem) => void;
  dismissToast: (id: number) => void;
  selection: { tracks: string[]; albums: string[]; artists: string[] };
  setSelection: (s: Partial<{ tracks: string[]; albums: string[]; artists: string[] }>) => void;
  toggleTrack: (p: string) => void;
  toggleAlbum: (p: string) => void;
  toggleArtist: (p: string) => void;
  clearSelection: () => void;
  /** App-wide playback volume (0-1) — shared by the player bar and the
   * fullscreen player, persisted across reloads. */
  vol: number;
  setVol: (v: number) => void;
}

const VOL_KEY = "mlo.vol";

function initialVol(): number {
  const raw = localStorage.getItem(VOL_KEY);
  if (raw === null || raw === "") return 1;
  const n = Number(raw);
  return Number.isFinite(n) && n >= 0 && n <= 1 ? n : 1;
}

export const useStore = create<Store>((set) => ({
  progress: null,
  setProgress: (progress) => set({ progress }),
  playing: null,
  setPlaying: (playing) => set({ playing }),
  queue: [],
  setQueue: (queue) => set((st) => ({ queue, queueId: st.queueId + 1 })),
  // Deliberately does NOT bump queueId: the player reloads audio on queueId
  // changes, and appending "next"/"end" must not interrupt the playing track.
  queueAdd: (tracks, position = "end") =>
    set((st) => {
      if (!tracks.length) return {};
      if (position === "next" && st.index < st.queue.length) {
        const queue = [...st.queue];
        queue.splice(st.index + 1, 0, ...tracks);
        return { queue };
      }
      return { queue: [...st.queue, ...tracks] };
    }),
  queueRemoveAt: (i) =>
    set((st) => {
      if (i < 0 || i >= st.queue.length) return {};
      const queue = [...st.queue];
      queue.splice(i, 1);
      // Removing the PLAYING row: the audio element is still the one the
      // player loaded, so the queue and the UI would disagree about what is
      // playing (title/cover/seek move to the next entry while the old track
      // keeps sounding, and an emptied queue leaves audio nobody can pause).
      // Bumping queueId makes the player reload — onto the row that took its
      // place, or onto nothing when the queue ran out.
      if (i === st.index) {
        if (!queue.length) return { queue, index: 0, queueId: st.queueId + 1 };
        return { queue, index: Math.min(i, queue.length - 1), queueId: st.queueId + 1 };
      }
      const index = i < st.index ? st.index - 1 : st.index;
      return { queue, index };
    }),
  queueMove: (from, to) =>
    set((st) => {
      if (from === to) return {};
      if (from < 0 || to < 0 || from >= st.queue.length || to >= st.queue.length) return {};
      const queue = [...st.queue];
      const [moved] = queue.splice(from, 1);
      queue.splice(to, 0, moved);
      let index = st.index;
      if (from === st.index) index = to;
      else if (from < st.index && to >= st.index) index = st.index - 1;
      else if (from > st.index && to <= st.index) index = st.index + 1;
      return { queue, index };
    }),
  index: 0,
  setIndex: (index) => set({ index }),
  queueId: 0,
  playNow: (queue, index = 0) =>
    set((st) => ({ queue, index, queueId: st.queueId + 1, playing: queue[index]?.path ?? null })),
  query: "",
  setQuery: (query) => set({ query }),
  sort: null,
  setSort: (sort) => set({ sort }),
  filter: {},
  setFilter: (filter) => set({ filter }),
  toasts: [],
  addToast: (t) => set((st) => ({ toasts: [...st.toasts, t].slice(-TOAST_MAX) })),
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
  vol: initialVol(),
  // `setVol` is called once per pointermove while the slider is dragged, and a
  // synchronous `localStorage.setItem` on that path is a disk write per frame
  // (a real stutter on a phone, and it also notifies every subscriber that
  // reads `vol`). The value in the store is still set immediately — the audio
  // element follows the finger — but the write to disk is coalesced to the
  // end of the drag's frame budget.
  setVol: (vol) => {
    set({ vol });
    if (volWrite !== undefined) return;
    volWrite = window.setTimeout(() => {
      volWrite = undefined;
      try {
        localStorage.setItem(VOL_KEY, String(useStore.getState().vol));
      } catch {
        /* storage disabled: the level still applies to this session */
      }
    }, 250);
  },
}));
let volWrite: number | undefined;

let toastSeq = 0;

/** Severity-aware toast: `toast(msg)` is informational, `toast.error(msg)` /
 *  `toast.success(msg)` are the explicit variants. Toasts queue (never
 *  overwrite), each dismisses on its own timer — errors stay twice as long —
 *  and any of them can be dismissed by hand from the shell. */
type ToastFn = {
  (message: string, severity?: ToastSeverity): void;
  info: (message: string) => void;
  success: (message: string) => void;
  error: (message: string) => void;
};

function pushToast(message: string, severity: ToastSeverity) {
  const id = ++toastSeq;
  useStore.getState().addToast({ id, message, severity });
  // Ids are never reused, so the timer needs no bookkeeping: dismissing an id
  // that already left the queue is a no-op filter.
  setTimeout(
    () => useStore.getState().dismissToast(id),
    severity === "error" ? TOAST_TTL_ERROR_MS : TOAST_TTL_MS
  );
}

export const toast: ToastFn = Object.assign(
  (message: string, severity: ToastSeverity = "info") => pushToast(message, severity),
  {
    info: (message: string) => pushToast(message, "info"),
    success: (message: string) => pushToast(message, "success"),
    error: (message: string) => pushToast(message, "error"),
  }
);