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
}

interface Store {
  progress: { done: number; total: number; desc: string } | null;
  setProgress: (p: { done: number; total: number; desc: string } | null) => void;
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
  toast: string | null;
  setToast: (t: string | null) => void;
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
  toast: null,
  setToast: (toast) => set({ toast }),
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
  setVol: (vol) => {
    localStorage.setItem(VOL_KEY, String(vol));
    set({ vol });
  },
}));

let toastTimer: ReturnType<typeof setTimeout> | null = null;

export function toast(msg: string) {
  // reset the timer on every call so rapid toasts each get their full 3s
  // instead of an earlier toast's timer cutting them short
  useStore.getState().setToast(msg);
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toastTimer = null;
    useStore.getState().setToast(null);
  }, 3000);
}