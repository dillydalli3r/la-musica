import { IN_TAURI, getToken, serverUrl } from "../api";
import { t, type MessageKey } from "./i18n";
import { toast } from "../store";
import {
  OS_KINDS,
  ingest,
  openNotification,
  type NotificationRecord,
  type NotifyEvent,
} from "./notifications";

/** Desktop notifications for the events the server publishes.
 *
 *  The backend already knows when a wished-for release shows up on Soulseek, a
 *  download finishes, an album is ready to import, an import needs a decision,
 *  a script run ends, a grade lands and a newer release exists — it announces
 *  each one on `/ws/events` (see server/events.py). This module is the client
 *  half: it keeps that socket open and hands every frame to the notification
 *  tray (lib/notifications.ts), which is what the bell's panel lists. The kinds
 *  that happen while the user is looking elsewhere (`OS_KINDS`) additionally
 *  raise an operating-system notification, so the news reaches them with the
 *  app in the background — which is the entire point, since a wish can be found
 *  hours after it was saved.
 *
 *  What this deliberately is NOT: remote push (Web Push / APNs / FCM). Those
 *  need a public server with VAPID keys (or an Apple/Google developer account)
 *  to reach a *closed* app, which a self-hosted LAN app cannot provide. So the
 *  reach is: any client with la musica open (tab, desktop window, phone app),
 *  including one that is hidden behind other windows. The service worker
 *  handles the `push` event too, so adding a push subscription later needs no
 *  client change.
 *
 *  Two sources of truth for "already told": the frame's own `seq` (the server
 *  numbers every event), kept in localStorage, so a reconnect that replays the
 *  ring does not re-notify what the user already saw.
 */

export type EventKind =
  | "wish_found"
  | "wish_failed"
  /** A wish whose searches found nothing: it stops being searched and waits
   *  for the user's own retry (see server/wishes' retry policy). */
  | "wish_not_found"
  | "download_done"
  | "download_failed"
  | "import_ready"
  | "import_needs_data"
  | "script_done"
  | "script_failed"
  | "grade_done"
  | "update_available";

/** A frame from `/ws/events`, plus the envelope field that marks it as one. */
export interface AppEvent extends NotifyEvent {
  type: string;
}

const SEQ_KEY = "mlo.notify.seq";

function lastSeq(): number {
  try {
    return Number(localStorage.getItem(SEQ_KEY) || 0) || 0;
  } catch {
    return 0;
  }
}

function rememberSeq(seq: number | undefined) {
  if (!seq) return;
  try {
    localStorage.setItem(SEQ_KEY, String(seq));
  } catch {
    /* storage disabled: duplicates on reconnect are the acceptable cost */
  }
}

/** How the OS permission stands right now, for the bell's own state. */
export type NotifyState = "granted" | "denied" | "prompt" | "unsupported";

export function notificationState(): NotifyState {
  // In the desktop/mobile shells the plugin owns permission; treat it as
  // available until asked (requestNotifications() knows the truth).
  if (IN_TAURI) return "prompt";
  if (typeof Notification === "undefined") return "unsupported";
  return Notification.permission === "granted"
    ? "granted"
    : Notification.permission === "denied"
    ? "denied"
    : "prompt";
}

/** Ask for permission. MUST be called from a real user gesture on the web —
 *  the browser rejects a request made from a timer or a socket callback. */
export async function requestNotifications(): Promise<NotifyState> {
  if (IN_TAURI) {
    try {
      const mod = await import("@tauri-apps/plugin-notification");
      const granted = await mod.isPermissionGranted();
      if (granted) return "granted";
      const asked = await mod.requestPermission();
      return asked === "granted" ? "granted" : "denied";
    } catch {
      return "unsupported";
    }
  }
  if (typeof Notification === "undefined") return "unsupported";
  if (Notification.permission === "granted") return "granted";
  if (Notification.permission === "denied") return "denied";
  try {
    return (await Notification.requestPermission()) === "granted" ? "granted" : "denied";
  } catch {
    return "denied";
  }
}

/** Fallback wording for a frame the server sent without a title. Every emitter
 *  in this app composes its own (the server owns the wording: it knows the
 *  artist and the album), so this is the safety net for an older server or a
 *  kind a newer one grew. */
const FALLBACK_TITLE: Partial<Record<EventKind, MessageKey>> = {
  wish_found: "notify.wish_found",
  download_done: "notify.download_done",
  import_ready: "notify.import_ready",
};

/** Show one notification, wherever this client can. The tray already holds
 *  `rec` (see lib/notifications.ts); this raises the OS popup for the kinds
 *  that deserve one and falls back to a toast otherwise — a toast is not a
 *  downgrade the user should have to discover. */
export async function showNotification(ev: AppEvent, rec: NotificationRecord): Promise<void> {
  const title = ev.title || t(FALLBACK_TITLE[ev.event as EventKind] ?? "notify.event");
  const body = ev.body || "";
  if (!OS_KINDS[ev.event]) {
    // The user's own foreground work (a script run, a grade, a newer release):
    // the tray keeps it, a toast says it now, and no OS popup interrupts.
    toast(body ? `${title} — ${body}` : title);
    return;
  }
  // `@tauri-apps/plugin-notification` is a dependency of the desktop/mobile
  // shell (desktop/package.json), NOT of the web app — it is a
  // platform-specific module that does not exist in a browser bundle, so the
  // import stays dynamic and is only ever reached inside a Tauri webview.
  if (IN_TAURI) {
    try {
      const mod = await import("@tauri-apps/plugin-notification");
      const granted = await mod.isPermissionGranted();
      if (granted) {
        mod.sendNotification({ title, body });
        return;
      }
    } catch {
      /* fall through to the toast below */
    }
  } else if (typeof Notification !== "undefined" && Notification.permission === "granted") {
    try {
      const reg = await navigator.serviceWorker?.getRegistration();
      // Through the worker when there is one: the notification then survives
      // this page being closed, and clicking it focuses the app and follows the
      // frame's own route (the worker's notificationclick handler reads
      // `data.url`). Without a worker the page keeps the click itself.
      if (reg) {
        await reg.showNotification(title, {
          body,
          tag: `mlo-${ev.event}`,
          data: { url: rec.link || rec.url },
        });
      } else {
        const popup = new Notification(title, { body });
        popup.onclick = () => {
          window.focus();
          openNotification(rec);
        };
      }
      return;
    } catch {
      /* fall through to the toast */
    }
  }
  // No permission (or no platform API): the toast is not a downgrade the user
  // should have to discover — they still learn that the wish landed.
  toast(body ? `${title} — ${body}` : title);
}

let socket: WebSocket | null = null;
let stopped = false;
let attempt = 0;
let timer: number | undefined;
// Bumped by every start()/stop(): handlers captured by an older socket compare
// their own generation against this and bail out. Without it, the sign-in /
// sign-out flip (and StrictMode's double mount in dev) left the previous
// socket subscribed on the server while its late onclose scheduled another
// connect — a leaked socket and a growing subscriber set per re-mount.
let generation = 0;

/** Where `/ws/events` lives. Same resolution as the API: the shell's server
 *  address, or this page's own origin. */
function eventsUrl(): string {
  const base = serverUrl();
  const origin = base || window.location.origin;
  const ws = origin.replace(/^http/, "ws");
  const token = getToken();
  const q = new URLSearchParams();
  if (token) q.set("token", token);
  // `since` is the last event NUMBER this client saw, which the server seeds
  // from the clock (see server/events.py) — so it divides down to the unix
  // second the replay window starts at. A client that has never seen an event
  // asks for "from now": replaying the ring would greet a fresh install with
  // up to a hundred OS notifications for things that happened before it
  // existed.
  const seen = lastSeq();
  q.set("since", String(seen ? seen / 1000 : Date.now() / 1000));
  return `${ws}/ws/events?${q}`;
}

function handle(raw: string) {
  let frame: AppEvent;
  try {
    frame = JSON.parse(raw) as AppEvent;
  } catch {
    return; // not ours to interpret
  }
  if (frame.type !== "event") return; // "ping" and anything future
  if (frame.seq && frame.seq <= lastSeq()) return; // already told
  rememberSeq(frame.seq);
  // The tray takes EVERY outcome — including the kinds that raise no OS popup,
  // because the panel is the only place a "download failed" lives after the
  // toast is gone. `ingest` answers null for a frame it has already seen (a
  // replayed ring), which is what keeps one outcome to one entry.
  // A frame the server sent no title for still gets words: the tray must never
  // list a blank row.
  if (!frame.title) frame.title = t(FALLBACK_TITLE[frame.event as EventKind] ?? "notify.event");
  const rec = ingest(frame);
  if (!rec) return;
  void showNotification(frame, rec);
}

/** Open the event socket and keep it open. Safe to call once per app mount;
 *  calling it again while a socket is live does nothing. Returns a stop
 *  function (used on unmount and by HMR). */
export function startEventStream(): () => void {
  stopped = false;
  const mine = ++generation;
  const current = () => !stopped && mine === generation;
  const connect = () => {
    if (!current()) return;
    try {
      socket = new WebSocket(eventsUrl());
    } catch {
      schedule();
      return;
    }
    const ws = socket;
    ws.onmessage = (e) => {
      if (!current()) return;
      attempt = 0;
      if (typeof e.data === "string") handle(e.data);
    };
    ws.onclose = (e) => {
      if (ws === socket) socket = null;
      if (!current()) return;
      if (e.code === 4401) {
        // The session is gone. Reconnecting with the same token would spin, so
        // wait for the shell's own re-login (which restarts this stream).
        return;
      }
      schedule();
    };
    ws.onerror = () => {
      /* onclose follows and owns the retry */
    };
  };
  const schedule = () => {
    if (!current()) return;
    attempt += 1;
    const wait = Math.min(30_000, 1000 * 2 ** Math.min(attempt, 5));
    if (timer !== undefined) window.clearTimeout(timer);
    timer = window.setTimeout(connect, wait);
  };
  // A second start() while one is live must not strand the first socket.
  socket?.close();
  socket = null;
  connect();
  return () => {
    stopped = true;
    generation += 1; // any in-flight handler from this mount is now stale
    if (timer !== undefined) window.clearTimeout(timer);
    socket?.close();
    socket = null;
  };
}
