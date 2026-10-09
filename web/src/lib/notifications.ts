import { useSyncExternalStore } from "react";

import type { SlskAutoProgress } from "../api";

/** The notification tray's store: what the bell's panel lists.
 *
 *  One record per OUTCOME the server announced on `/ws/events` (see
 *  server/events.py) — a finished import, an import short of a
 *  family (a warning on an album already in the library, spec R166), a script
 *  run, a grade, a newer release. The event socket
 *  pushes them in (lib/notify.ts); nothing here polls, and nothing here
 *  raises an OS notification — that stays in notify.ts, because the tray must
 *  keep the outcome even when the browser is not allowed to pop one up.
 *
 *  Two independent bits of state, deliberately not the same thing:
 *
 *  * `read` — whether the user has SEEN the outcome. Opening the panel marks
 *    everything read, which is what puts the bell's light out.
 *  * the record itself — the log. Dismissing removes one, "clear all" empties
 *    it. Reading the tray must never erase it (that would lose the only place
 *    a failed import's note exists).
 *
 *  A notification carries a subject as well as words: `link` is the in-app
 *  route the outcome is about (`/album/<path>`, `/import?album=<path>`),
 *  `url` an outside page (the release notes of a
 *  newer version). Clicking an entry opens it; `openNotification` is the one
 *  place that decides how (see `registerNavigator`).
 *
 *  Kept dependency-free and plain (the same hand-rolled listener store as
 *  lib/i18n.ts, not the zustand store the shell uses — this file must be
 *  loadable by `tools/test_notifications.cjs` with no DOM, no server and no
 *  React renderer). The only import is `useSyncExternalStore` for the hook.
 */

/** A frame from `/ws/events`, as far as this store cares. `event` is the
 *  kind; everything else is the server's own wording plus its `data` payload. */
export interface NotifyEvent {
  type?: string;
  event: string;
  title?: string;
  body?: string;
  data?: Record<string, unknown>;
  at?: number; // unix SECONDS (server clock) — the frame's own timestamp
  seq?: number; // the server's event number (ms since epoch)
}

export interface NotificationRecord {
  /** Stable: the server's event number when it sent one, else kind+time+title. */
  id: string;
  kind: string;
  title: string;
  body: string;
  /** Unix MILLISECONDS (the server sends seconds; normalized on ingest). */
  at: number;
  /** In-app route this outcome is about ("" when the kind names no subject). */
  link: string;
  /** Outside page (the release notes of a newer version), "" when none. */
  url: string;
  read: boolean;
}

/** Kinds that deserve an operating-system notification — the ones about
 *  something happening while the user was looking elsewhere (an import waiting
 *  for a decision, a finished import, a store the server pruned on its own).
 *  A script run or a grade is the user's own foreground job: the tray and a
 *  toast say so, and an OS popup for it would be noise. So are the two
 *  "your add is on its way" kinds (`library_add`, `album_pending`) — an
 *  acknowledgement of a press, made while the user is still looking at the
 *  button.
 *  A kind listed here pops on EVERY client, open or closed. A kind missing
 *  here reaches the tray of an OPEN client and nothing else — which is a
 *  decision about the kind, not about the client, so it holds for the web,
 *  the desktop shell and the phone alike. */
export const OS_KINDS: Record<string, true> = {
  import_done: true,
  import_needs_data: true,
  storage_pruned: true,
};

/** The kinds this device asks the SERVER to push to it (lib/notify.ts sends
 *  this list when it subscribes).
 *
 *  The same set as the OS popups: an outcome worth interrupting an open app
 *  for is an outcome worth waking a closed one for, and keeping the two lists
 *  equal is what stops a kind from reaching a phone but not a desktop. A kind
 *  missing here still reaches an open app; it just cannot wake a closed one. */
export const PUSH_KINDS: Record<string, true> = { ...OS_KINDS };

/** How many entries the log keeps. Long enough to cover a working session,
 *  short enough that the panel stays a list and storage stays tiny. */
const MAX_ITEMS = 50;
const LOG_KEY = "mlo.notify.log";

function str(v: unknown): string {
  return typeof v === "string" ? v.trim() : v === undefined || v === null ? "" : String(v).trim();
}

/** The in-app route a kind's `data` points at, when the emit did not name one.
 *
 *  Derived rather than required of every emit site, and always a route the
 *  router really mounts (web/src/App.tsx). An unknown kind answers "" — a
 *  notification with no subject is still a notification, and clicking it then
 *  just dismisses the panel instead of navigating nowhere. */
export function linkFor(kind: string, data?: Record<string, unknown>): string {
  const album = str(data?.album_path);
  const track = str(data?.track_path);
  switch (kind) {
    case "import_done":
      if (track) return `/track/${encodeURIComponent(track)}`;
      return album ? `/album/${encodeURIComponent(album)}` : "/library";
    case "import_needs_data":
      return album ? `/import?album=${encodeURIComponent(album)}` : "/import";
    case "import_unfinished":
      return album ? `/import?album=${encodeURIComponent(album)}` : "/import";
    case "script_done":
    case "script_failed":
      return "/in-progress";
    case "grade_done":
      return "/library";
    case "update_available":
      return "/settings";
    default:
      return album ? `/album/${encodeURIComponent(album)}` : "";
  }
}

function load(): NotificationRecord[] {
  try {
    const raw = localStorage.getItem(LOG_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((r): r is NotificationRecord =>
      !!r && typeof r === "object" && !!str((r as NotificationRecord).id)
      // An older build logged one row PER grade finding; the tray draws them
      // as ONE live panel now (the same strip Home and the Library read), so
      // the old rows would only double it. Dropped on load.
      && (r as NotificationRecord).kind !== "grade_warning");
  } catch {
    return []; // storage disabled or a log written by an older shape: start empty
  }
}

let items: NotificationRecord[] = load();
const listeners = new Set<() => void>();

function persist() {
  try {
    localStorage.setItem(LOG_KEY, JSON.stringify(items));
  } catch {
    /* private mode: the tray still works for this page's lifetime */
  }
}

function commit() {
  persist();
  for (const fn of listeners) fn();
}

export function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

/** Newest first. */
export function notifications(): NotificationRecord[] {
  return items;
}

export function unreadCount(): number {
  return items.filter((n) => !n.read).length;
}

/** Turn one event frame into a tray entry. Returns the record, or null when
 *  the frame says nothing this tray can hold (no kind) or the same event
 *  arrived twice — the server replays its ring on reconnect, and a replayed
 *  frame must not become a second entry. */
export function ingest(ev: NotifyEvent): NotificationRecord | null {
  const kind = str(ev?.event);
  if (!kind) return null;
  const at = Number(ev.at) > 0 ? Math.round(Number(ev.at) * 1000) : Date.now();
  const title = str(ev.title);
  const id = Number(ev.seq) > 0 ? `e${Number(ev.seq)}` : `${kind}:${at}:${title}`;
  if (items.some((n) => n.id === id)) return null;
  const data = ev.data && typeof ev.data === "object" ? ev.data : undefined;
  const record: NotificationRecord = {
    id,
    kind,
    title,
    body: str(ev.body),
    at,
    // The emit site's own `link` wins — it knows the subject exactly; the
    // derived route is the fallback for the emitters that predate it.
    link: str(data?.link) || linkFor(kind, data),
    url: str(data?.url),
    read: false,
  };
  items = [record, ...items].slice(0, MAX_ITEMS);
  commit();
  return record;
}

export function markRead(id: string) {
  if (!items.some((n) => n.id === id && !n.read)) return;
  items = items.map((n) => (n.id === id ? { ...n, read: true } : n));
  commit();
}

/** Everything is seen: what the bell's light going out means. */
export function markAllRead() {
  if (!unreadCount()) return;
  items = items.map((n) => (n.read ? n : { ...n, read: true }));
  commit();
}

export function dismiss(id: string) {
  const next = items.filter((n) => n.id !== id);
  if (next.length === items.length) return;
  items = next;
  commit();
}

/** Throw the whole log away ("Clear all"). */
export function clearAll() {
  if (!items.length) return;
  items = [];
  commit();
}

/** The tray's kind for a WARNING the app derived from a payload it reads
 *  (see `ingestDerived`): the grade findings the Home and Library strips show.
 *  Deliberately NOT in OS_KINDS/PUSH_KINDS — this is a datapoint re-read from a
 *  page, not an outcome the server announced, so it must never pop a banner on
 *  a phone. */
export const GRADE_WARNING_KIND = "grade_warning";

/** The tray's kind for a manual import the user left unfinished. Like
 *  GRADE_WARNING_KIND this is a datapoint re-read from the server (the session
 *  list, GET /api/import/sessions), not an outcome announced on `/ws/events` —
 *  deliberately NOT in OS_KINDS/PUSH_KINDS, so it never pops a banner on a
 *  phone. The wizard writes/dismisses the underlying session; the tray only
 *  mirrors it. */
export const IMPORT_UNFINISHED_KIND = "import_unfinished";

/** Outcome kinds that are SIGNALS, not records.
 *
 *  `library_changed` says "the library moved under you, drop what you derived
 *  from it". The server publishes it on every library write (a tag, a cover, a
 *  rename, an import step) and again when its background tree rebuild lands —
 *  so it must never reach the tray and never raise an OS notification, or the
 *  panel would fill with rows nobody asked for. The `/ws/events` reader
 *  (`lib/notify.ts`) skips these for the log while still handing them to the
 *  listeners, which is the whole point: the App drops its library queries. */
export const SILENT_KINDS: Record<string, true> = { library_changed: true };

/** The stable tray id for one album's unfinished import: the wizard dismisses
 *  exactly this when the album is finished, and the sync that ingests the
 *  session list keys on it, so the two never disagree about which album a row
 *  is about. */
export function importSessionNotificationId(album: string): string {
  return `import-session:${str(album).replace(/\\/g, "/").toLowerCase()}`;
}

/** Record a WARNING the app derived from a payload it already reads, rather
 *  than one the server announced on `/ws/events`.
 *
 *  The grade strip is the case: the server has nothing to announce — its
 *  findings are computed per request, and they change when a script writes a
 *  tag rather than when an event fires — so a tray that only listened to the
 *  socket would never carry "Tool — Lateralus: Unrecognized MEDIA value: HDCD".
 *  `id` is the caller's own stable key for the finding, so a re-render, a poll
 *  or a second page reading the same summary cannot log it twice, while a NEW
 *  or CHANGED finding becomes a new entry. Nothing here raises an OS
 *  notification (that stays in lib/notify.ts). */
export function ingestDerived(rec: {
  id: string; kind: string; title: string; body: string; link?: string;
}): NotificationRecord | null {
  const id = str(rec.id);
  if (!id || items.some((n) => n.id === id)) return null;
  const record: NotificationRecord = {
    id,
    kind: str(rec.kind),
    title: str(rec.title),
    body: str(rec.body),
    at: Date.now(),
    link: str(rec.link),
    url: "",
    read: false,
  };
  items = [record, ...items].slice(0, MAX_ITEMS);
  commit();
  return record;
}

/** Where a click should take the user. The bell registers the router's
 *  navigate (it is the only surface with one); the fallback is a real
 *  navigation, so a click still works if nothing registered. */
type Navigator = (to: string) => void;
let navigator: Navigator | null = null;

export function registerNavigator(fn: Navigator): () => void {
  navigator = fn;
  return () => {
    if (navigator === fn) navigator = null;
  };
}

/** How an OUTSIDE page is opened — the `url` half of a record, as opposed to
 *  the in-app `link` above. Registered by the shell for the same reason the
 *  navigator is, and it matters more here than it looks: in a Tauri webview
 *  `window.open` is dropped outright (no tabs, and the new-window request is
 *  denied), so without a registered opener a click on "la musica 3.3.0 is
 *  available" did nothing at all. */
type Opener = (url: string) => void;
let opener: Opener | null = null;

export function registerOpener(fn: Opener): () => void {
  opener = fn;
  return () => {
    if (opener === fn) opener = null;
  };
}

/** Follow the route a record names, at record level = "" for the destinations
 *  the app itself owns. Exported for tests: the click path must be provable
 *  without a browser. */
export function resolveTarget(rec: NotificationRecord): string {
  if (rec.link) return rec.link;
  if (rec.url) return rec.url;
  return "";
}

/** Open what a notification is about: the album, the track, the queue page,
 *  the wizard. Marks the entry read first — the user has plainly seen it. */
export function openNotification(rec: NotificationRecord) {
  markRead(rec.id);
  const target = resolveTarget(rec);
  if (!target) return;
  if (rec.link) {
    if (navigator) navigator(rec.link);
    else window.location.assign(rec.link);
    return;
  }
  // An OUTSIDE page (`url`, not `link`): the shell registers its own opener for
  // this (see `registerOpener`), because a webview drops `window.open` and this
  // file must stay loadable with no DOM to import the shell's own.
  if (opener) opener(target);
  else window.open(target, "_blank", "noreferrer");
}

/** The hook the bell renders from. */
export function useNotifications(): { items: NotificationRecord[]; unread: number } {
  const snapshot = useSyncExternalStore(subscribe, notifications, notifications);
  return { items: snapshot, unread: snapshot.filter((n) => !n.read).length };
}



/* ------------------------------------------------------------------------- *
 * Live transfer progress
 *
 * A second, deliberately separate store: the shell's own socket (App.tsx)
 * hands every `{"type":"transfers"}` frame from the progress relay here, and
 * the Soulseek page's bars are drawn from the frame instead of its own 3 s
 * poll (server/main.py pushes the same rows /api/soulseek/downloads returns).
 *
 * It lives beside the tray because both are the client end of the server's
 * push channels, and because the separation has to be visible in one place: a
 * progress tick is NOT an outcome. `publishTransfers` never touches `ingest`,
 * so a byte count that changes four times a second can never raise a toast,
 * an OS notification or a tray row.
 * ------------------------------------------------------------------------- */

/** One slskd transfer, as pushed — slskd's own field names, because the frame
 *  carries slskd's own rows verbatim. */
export interface LiveTransfer {
  id?: string;
  state?: string;
  size?: number;
  bytesTransferred?: number;
  percentComplete?: number;
  averageSpeed?: number;
}

/** One live job, as its module publishes it — `progress` is the same block
 *  /api/soulseek/auto serves, handed over verbatim. */
export interface LiveJob {
  id?: number;
  state?: string;
  stage?: string;
  stage_key?: string;
  progress?: SlskAutoProgress | null;
}

/** One frame from the progress relay. `files` carries only the transfers
 *  whose bytes or state changed (a slskd tree holds the whole history), and
 *  `resync` says the list changed shape — a transfer appeared or was dropped
 *  — which is the one case that needs a real refetch. */
export interface TransfersFrame {
  type: "transfers";
  files?: LiveTransfer[];
  jobs?: LiveJob[];
  resync?: boolean;
}

let liveFrame: TransfersFrame | null = null;
const liveListeners = new Set<() => void>();

/** Hand one frame to the live-progress store. Called by the shell's socket
 *  handler; a frame with nothing in it is still a frame (the page decides
 *  what changed). */
export function publishTransfers(frame: TransfersFrame) {
  liveFrame = frame;
  for (const fn of liveListeners) fn();
}

function subscribeLive(fn: () => void): () => void {
  liveListeners.add(fn);
  return () => {
    liveListeners.delete(fn);
  };
}

/** The newest frame, or null before the first one. The identity changes once
 *  per frame, which is what the page's merge effect keys on. */
export function liveTransfers(): TransfersFrame | null {
  return liveFrame;
}

/** The hook a live progress surface renders from. */
export function useLiveTransfers(): TransfersFrame | null {
  return useSyncExternalStore(subscribeLive, liveTransfers, liveTransfers);
}
