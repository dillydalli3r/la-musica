import { useEffect } from "react";
import { create } from "zustand";

import { heldBy } from "./lib/locks";

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
   *  same frame as the title: the title, the mark and the art are committed as
   *  ONE record (`lib/nowPlaying`), so the mark rides that frame instead of
   *  arriving with the per-track tags fetch a moment later. Refreshed by that
   *  fetch when it has a value. */
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
  playing: string | null;
  setPlaying: (p: string | null) => void;
  /** True while a lyric editor is open on the playing track (the docked pane
   *  or the fullscreen player set it through `useLyricsEditLock`). The player
   *  bar's `handleEnded` reads it and holds the queue — pauses — instead of
   *  advancing: the words being edited belong to THIS file, and an automatic
   *  advance (the gapless handover included) would swap it out from under the
   *  reader mid-edit. Cleared when the editor closes or its save lands. */
  lyricsEditing: boolean;
  setLyricsEditing: (v: boolean) => void;
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
  /** Bumped by every DELIBERATE play press: `playNow` (so every play button in
   *  the app), plus the queue popovers' own "play this row". The player
   *  restarts the current track when this changes even though the path did not
   *  — which is what pressing an album's play button a second time means. A
   *  reorder, a rolling queue edit or a trim must NOT bump it: those resolve to
   *  the same track too, and restarting the song for them would be the bug this
   *  token exists to separate from the press. */
  playToken: number;
  playNow: (q: QueueTrack[], i?: number) => void;
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
  /** App-wide playback volume (0-1) — shared by the player bar and the
   * fullscreen player, persisted across reloads. */
  vol: number;
  setVol: (v: number) => void;
  /** Where a RELOAD found the player (`mlo.player.state.v1`): the track the
   *  queue was on and the second inside it. The queue and `index` above are
   *  hydrated from the same record, so the player bar only has to seek — once
   *  the restored track's own metadata arrives — and it must do so PAUSED: a
   *  page cannot start sound on its own, and a surprise album is worse than
   *  pressing play. Both are 0/null for a session that was not restored. */
  resumePath: string | null;
  resumeTime: number;
  /** The restore has been honoured (the seek happened, or the queue moved on):
   *  no later load may replay it. */
  clearResume: () => void;
}

const VOL_KEY = "mlo.vol";

function initialVol(): number {
  const raw = localStorage.getItem(VOL_KEY);
  if (raw === null || raw === "") return 1;
  const n = Number(raw);
  return Number.isFinite(n) && n >= 0 && n <= 1 ? n : 1;
}

// ---- Where the player was when the page went away ------------------------
// A reload — a refresh, a phone that evicted the tab, a shell that restarted —
// used to come back to an empty bar: the queue, the row and the second inside
// it lived only in memory. They are now kept in one localStorage record and
// hydrated HERE, in the store module, because that is the only place that runs
// before the player bar mounts (and the bar is mounted once for the session).
//
// What is deliberately NOT in the record is whether the track was playing: a
// page cannot start sound without a gesture, and silently resuming an album
// into a room is worse than one press. A restored session is always paused.

const PLAYER_STATE_KEY = "mlo.player.state.v1";
/** How long two saves must be apart. Wide enough that a burst of queue edits
 *  (a drag-reorder, a trim, a press that replaces the queue) costs one write,
 *  tight enough that a reload right after an edit still comes back to it. */
const PLAYER_WRITE_MS = 500;
/** The slow tick that keeps the stored SECOND roughly current while the page
 *  lives: a reload then loses at most this much of the track. `timeupdate`
 *  fires about four times a second and is never itself a write — that would be
 *  a synchronous `localStorage.setItem` per frame. */
const PLAYER_SLOW_MS = 5000;

/** The record as written — and as read back. `path` rides beside the queue
 *  because `index` alone cannot say WHICH track a second belonged to: a queue
 *  edit between two writes leaves the index pointing at a row the position was
 *  never recorded for, and the path is what the position is matched against. */
interface PlayerState {
  queue: QueueTrack[];
  index: number;
  path: string;
  time: number;
}

/** One queue row as it survives a JSON round trip — or null when it is not a
 *  row this build can play. A hand-edited or half-written record must degrade
 *  to "nothing to restore", never to a queue of `undefined`. */
function asQueueTrack(v: unknown): QueueTrack | null {
  if (!v || typeof v !== "object") return null;
  const t = v as Partial<QueueTrack>;
  if (typeof t.path !== "string" || !t.path) return null;
  if (typeof t.file !== "string" || !t.file) return null;
  if (typeof t.albumPath !== "string") return null;
  return {
    path: t.path,
    file: t.file,
    albumPath: t.albumPath,
    artist: typeof t.artist === "string" ? t.artist : undefined,
    album: typeof t.album === "string" ? t.album : undefined,
    title: typeof t.title === "string" ? t.title : undefined,
    coverFile: typeof t.coverFile === "string" || t.coverFile === null ? t.coverFile : undefined,
    albumCover: typeof t.albumCover === "string" || t.albumCover === null ? t.albumCover : undefined,
    advisory: typeof t.advisory === "string" || t.advisory === null ? t.advisory : undefined,
  };
}

/** The stored record, or null when there is nothing usable in it. */
function readPlayerState(): PlayerState | null {
  try {
    const raw = localStorage.getItem(PLAYER_STATE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<PlayerState> | null;
    if (!parsed || !Array.isArray(parsed.queue)) return null;
    const queue = parsed.queue.map(asQueueTrack).filter((t): t is QueueTrack => t !== null);
    if (!queue.length) return null;
    const rawIndex = parsed.index;
    const index = typeof rawIndex === "number" && Number.isInteger(rawIndex)
      ? Math.min(Math.max(rawIndex, 0), queue.length - 1)
      : 0;
    const rawTime = parsed.time;
    const time = typeof rawTime === "number" && Number.isFinite(rawTime) && rawTime > 0 ? rawTime : 0;
    const path = queue[index].path;
    // The second is only restored for the row it was recorded for.
    return { queue, index, path, time: parsed.path === path ? time : 0 };
  } catch {
    return null; // storage disabled, or the value was edited by hand
  }
}

/** What this page load starts from (null on the first run). */
const RESTORED = readPlayerState();

/** The player's live position, reported by the player bar (which owns the
 *  element) and captured with the queue on the next write. A module variable,
 *  never store state: it changes several times a second and nothing renders
 *  from it. */
let playerPos: { path: string | null; time: number } = { path: null, time: 0 };

/** The player bar's report of what the ACTIVE element holds and where it is.
 *  The path is carried so a position can never be attributed to the wrong
 *  row (see `playerSnapshot`). */
export function notePlayerPosition(path: string | null, time: number) {
  playerPos = { path, time: Number.isFinite(time) && time >= 0 ? time : 0 };
}

let playerWrite: number | undefined;

/** The record to write — or null when there is no queue to come back to, in
 *  which case the key is REMOVED (a cleared queue must not return). */
function playerSnapshot(): PlayerState | null {
  const st = useStore.getState();
  const track = st.queue[st.index];
  if (!track) return null;
  // The second belongs to the track the ELEMENT holds. A track change or an
  // edit can leave the store naming another row while the recorded position is
  // still the outgoing track's; resuming the neighbour in the middle would be
  // the bug this guard exists for.
  const time = playerPos.path === track.path ? playerPos.time : 0;
  return {
    queue: st.queue,
    index: st.index,
    path: track.path,
    time: Math.round(time * 10) / 10,
  };
}

function writePlayerState() {
  playerWrite = undefined;
  try {
    const snap = playerSnapshot();
    if (snap) localStorage.setItem(PLAYER_STATE_KEY, JSON.stringify(snap));
    else localStorage.removeItem(PLAYER_STATE_KEY);
  } catch {
    /* storage disabled: the session simply survives no reload */
  }
}

/** Save, coalesced. Scheduled by every queue mutation (below), by each pause
 *  and by the slow tick — never per `timeupdate`. */
export function savePlayerState() {
  if (playerWrite !== undefined) return;
  playerWrite = window.setTimeout(writePlayerState, PLAYER_WRITE_MS);
}

/** Save NOW: the page is going away, the sound just stopped, or the store
 *  changed in a way whose next tick may never run. */
export function flushPlayerState() {
  if (playerWrite !== undefined) {
    clearTimeout(playerWrite);
    playerWrite = undefined;
  }
  writePlayerState();
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
          : e.total > 0 && e.done >= e.total && now - e.t >= PROGRESS_LEGACY_DONE_MS;
        if (gone) dropped = true;
        else kept[key] = e;
      }
      return dropped ? { progresses: kept, progress: newest(kept) } : {};
    }),
  playing: null,
  setPlaying: (playing) => set({ playing }),
  lyricsEditing: false,
  setLyricsEditing: (lyricsEditing) => set({ lyricsEditing }),
  // The queue and the row are the bulk of a restored session (see
  // `PLAYER_STATE_KEY`): a reload comes back to the same list, on the same
  // track, paused. `playing` above stays null through every one of these.
  queue: RESTORED?.queue ?? [],
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
  index: RESTORED?.index ?? 0,
  setIndex: (index) => set({ index }),
  queueId: 0,
  playToken: 0,
  playNow: (queue, index = 0) => {
    // A file a job is rewriting RIGHT NOW does not play: the server refuses the
    // stream (409) and the element would just sit there silent. Say what the
    // registry says — the same sentence the route answers with — and leave
    // whatever is playing (and the queue) exactly as it was. Every play entry
    // point in the app goes through here, so a locked row cannot start a
    // silent no-op from any of them.
    const held = heldBy(queue[index]?.path);
    if (held) {
      toast(held.held.why);
      return;
    }
    set((st) => ({ queue, index, queueId: st.queueId + 1, playToken: st.playToken + 1, playing: queue[index]?.path ?? null }));
  },
  // The restore's own two values, read once by the player bar. They are store
  // state rather than module constants so the bar can clear them the moment
  // the seek has happened — a later load of the same track must start at 0.
  resumePath: RESTORED?.path ?? null,
  resumeTime: RESTORED?.time ?? 0,
  clearResume: () => set((st) => (st.resumePath === null && st.resumeTime === 0 ? {} : { resumePath: null, resumeTime: 0 })),
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

/** Hold the queue on the track a lyric editor is open on: while `open` the
 *  store's `lyricsEditing` is set, which PlayerBar's `handleEnded` reads to
 *  pause instead of advancing — a track change would swap the file being
 *  edited out from under the reader, and the gapless handover would do it
 *  without even a load gap. The surfaces that open an editor (LyricsSidebar
 *  and NowPlayingView) call this; it is cleared when the editor closes or its
 *  save lands — both close the editor. */
export function useLyricsEditLock(open: boolean) {
  useEffect(() => {
    useStore.getState().setLyricsEditing(open);
    // Leaving the surface (the pane closing, the track changing) releases the
    // lock too: no render with `open === false` is guaranteed on unmount.
    return () => useStore.getState().setLyricsEditing(false);
  }, [open]);
}

let persistenceStarted = false;

/** Start the player's own persistence: the queue-mutation subscription, the
 *  slow save and the two flushes.
 *
 *  A function, and NOT module-scope work, on purpose: importing this module
 *  must have no side effects at all. Several Node/vite-SSR harnesses import
 *  the store's graph — `check_release_choice.mjs` and the payload checks
 *  transitively — and a `setInterval` created at import time keeps their event
 *  loop alive so they never exit, while a `window.addEventListener` throws in
 *  the ones that stub `window` with a bare object. Everything here is
 *  browser-only and reachable from the browser, so the shell calls this once
 *  when the app mounts (PlayerBar, which is mounted for the session's life).
 *  Idempotent: a re-mount costs nothing. */
export function startPlayerPersistence() {
  if (persistenceStarted) return;
  persistenceStarted = true;
  // Every queue mutation — a press that replaces the queue, a reorder, a
  // removal, the row moving on — schedules a save: the queue IS the restore's
  // bulk, and this is the one seam all of them pass. The SECOND inside a track
  // is not written from here (it changes four times a second); a pause and the
  // slow tick below keep it current. A pause is the moment worth keeping
  // exactly, so it flushes instead of waiting for a coalesced write.
  useStore.subscribe((st, prev) => {
    if (st.queue !== prev.queue || st.index !== prev.index) savePlayerState();
    if (st.playing !== prev.playing && !st.playing) flushPlayerState();
  });
  window.setInterval(savePlayerState, PLAYER_SLOW_MS);
  // The page going away is the last chance for a coalesced write, and
  // `visibilitychange` is the event iOS fires before it kills a backgrounded
  // tab (`pagehide` is not guaranteed there).
  window.addEventListener("pagehide", flushPlayerState);
  document.addEventListener("visibilitychange", flushPlayerState);
}
let volWrite: number | undefined;

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