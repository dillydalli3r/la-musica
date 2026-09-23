import { IN_TAURI, getToken, onAuthLost, serverUrl } from "../api";
import { t, type MessageKey } from "./i18n";
import { toast } from "../store";
import {
  OS_KINDS,
  PUSH_KINDS,
  ingest,
  openNotification,
  type NotificationRecord,
  type NotifyEvent,
} from "./notifications";

/** Desktop notifications for the events the server publishes.
 *
 *  The backend already knows when a wished-for release shows up on Soulseek, a
 *  download finishes, an album is ready to import, an import finished short of
 *  something is left to announce, a script run ends, a grade lands and a newer
 *  release exists — it announces
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
  | "download_started"
  | "download_done"
  | "download_failed"
  /** A peer started downloading from us: our files are being shared. */
  | "upload_started"
  | "import_ready"
  | "import_done"
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

/** ── Web Push ─────────────────────────────────────────────────────────────
 *
 *  Remote push: the way a device that is CLOSED hears that an import finished
 *  (#48). A live page is already told everything over /ws/events — and that is
 *  why this is a SEPARATE switch, per device, rather than another kind of
 *  notification: it is about *where* the news can reach, not which news.
 *
 *  What works where, because a switch we cannot honour must not be offered:
 *
 *  * Browser over https (or localhost): Web Push, app open or closed. This is
 *    the full story and the only client that gets the switch by default.
 *  * Android: the same Web Push, but the subscription lives in an installed
 *    PWA — Chrome will not deliver push to a plain tab forever, and the
 *    installed app is what the user actually wants on a phone.
 *  * iOS/iPadOS: push needs the app ADDED TO THE HOME SCREEN (Web Push landed
 *    in Safari 16.4, for installed web apps only). A tab in Safari can raise a
 *    foreground notification and nothing else, so `pushSupport()` answers
 *    "ios_install" there and the panel explains that instead of offering a
 *    subscription that would silently never fire.
 *  * The desktop shell (Tauri): no service worker, therefore no Web Push. It
 *    raises its OWN notification while it is running (showNotification, via
 *    the Tauri plugin), which is what it can honestly promise.
 *
 *  The subscription is kept fresh on every load (`refreshPush`) because a
 *  browser rotates its endpoint and key material — and it is dropped on
 *  sign-out (`dropPush`) because a device that has been signed out must not
 *  keep receiving this server's news.
 */

const PUSH_KEY = "mlo.push.endpoint";

/** Why this device can or cannot be pushed to, as the panel renders it. */
export type PushSupport =
  | "ok" // this browser can subscribe
  | "desktop" // the Tauri shell: no service worker there
  | "ios_install" // iOS/iPadOS outside an installed app
  | "insecure" // plain-http LAN: no service worker, so no push
  | "unsupported"; // browser without the Push API

function storedPush(): string {
  try {
    return localStorage.getItem(PUSH_KEY) || "";
  } catch {
    return "";
  }
}

function rememberPushEndpoint(endpoint: string | null) {
  try {
    if (endpoint) localStorage.setItem(PUSH_KEY, endpoint);
    else localStorage.removeItem(PUSH_KEY);
  } catch {
    /* storage disabled: the subscription still works, it just cannot be
       refreshed or dropped by endpoint on the next load */
  }
}

/** Whether this client can be offered the push switch at all — and when it
 *  cannot, the reason the panel shows in its place (see the block above). */
export function pushSupport(): PushSupport {
  if (IN_TAURI) return "desktop";
  if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return "unsupported";
  if (!("PushManager" in window)) return "unsupported";
  // A service worker needs a secure context, and so does push: on a plain-http
  // LAN address the browser registers neither (see main.tsx's own check).
  if (!window.isSecureContext) return "insecure";
  // iPadOS 13+ reports itself as a Mac; the touch points give it away, and the
  // home-screen rule below is Safari's alone.
  const ua = navigator.userAgent || "";
  const ios =
    /iPad|iPhone|iPod/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);
  // An installed app is the only place iOS delivers a push: in a tab, Safari
  // 16.4's Web Push raises nothing once the page is gone. `display-mode` is
  // the standard answer; `standalone` is WebKit's older spelling of it.
  let installed = false;
  try {
    installed = window.matchMedia?.("(display-mode: standalone)").matches === true;
  } catch {
    /* no matchMedia: the standalone flag below is the only answer left */
  }
  if (!installed && "standalone" in navigator && navigator.standalone === true) installed = true;
  if (ios && !installed) return "ios_install";
  return "ok";
}

/** Whether this device asked for push, for the switch's own state. Read from
 *  storage rather than the browser's PushManager because the two can disagree:
 *  the row the SERVER holds is what decides whether a frame is sent. */
export function pushEnabled(): boolean {
  return !!storedPush();
}

export interface PushStatus {
  /** The server can sign a push (it has key material and the crypto library). */
  available: boolean;
  /** The VAPID public key to subscribe with, base64url. */
  public_key: string;
  /** How many devices this user already registered. */
  subscriptions: number;
}

/** What the test button reports: `sent` is devices that took the frame, `gone`
 *  ones the push service called dead, `failed` the rest. */
export interface PushTestResult {
  sent: number;
  gone: number;
  failed: number;
  subscriptions: number;
}

/** Ask the server what it can do. Never throws: a server that cannot be
 *  reached reports "unavailable", which hides the switch rather than offering
 *  a button that fails. */
export async function pushStatus(): Promise<PushStatus> {
  try {
    const res = await pushFetch<Partial<PushStatus>>("/api/push/status");
    return {
      available: !!res.available,
      public_key: String(res.public_key || ""),
      subscriptions: Number(res.subscriptions || 0),
    };
  } catch {
    return { available: false, public_key: "", subscriptions: 0 };
  }
}

/** The API's own call shape (see api.ts's `json`): the session travels as the
 *  `mlo_session` cookie for the web app, or as a Bearer token for a client
 *  pointed at another server. */
async function pushFetch<T>(path: string, body?: unknown): Promise<T> {
  const token = getToken();
  const res = await fetch(`${serverUrl()}${path}`, {
    method: body === undefined ? "GET" : "POST",
    credentials: "same-origin",
    headers: {
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`push: ${res.status}`);
  return (await res.json()) as T;
}

/** The worker's registration, registered here if the page has not done it yet
 *  (main.tsx registers it on load; a client that turns push on immediately
 *  after signing in must not race that). */
async function pushRegistration(): Promise<ServiceWorkerRegistration | null> {
  try {
    if (!("serviceWorker" in navigator)) return null;
    const existing = await navigator.serviceWorker.getRegistration();
    if (!existing) await navigator.serviceWorker.register("/sw.js", { scope: "/" });
    const reg = await navigator.serviceWorker.ready;
    return reg ?? null;
  } catch {
    return null;
  }
}

/** VAPID keys are base64url; `applicationServerKey` wants the raw bytes. The
 *  explicit `ArrayBuffer` is what `BufferSource` accepts — a `Uint8Array` over
 *  a possibly-shared buffer does not type-check. */
function keyBytes(base64url: string): Uint8Array<ArrayBuffer> {
  const pad = "=".repeat((4 - (base64url.length % 4)) % 4);
  const raw = atob((base64url + pad).replace(/-/g, "+").replace(/_/g, "/"));
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

/** The subscription this device currently holds, creating one if needed. */
async function subscribe(
  reg: ServiceWorkerRegistration,
  publicKey: string,
): Promise<{ endpoint: string; keys: { p256dh: string; auth: string } } | null> {
  const manager = reg.pushManager;
  if (!manager) return null;
  const existing = await manager.getSubscription();
  const sub =
    existing ??
    (await manager.subscribe({
      // Required by Chrome: every push MUST raise a visible notification, which
      // is exactly what sw.js's push handler does.
      userVisibleOnly: true,
      applicationServerKey: keyBytes(publicKey),
    }));
  if (!sub) return null;
  const keys = sub.toJSON().keys;
  if (!keys?.p256dh || !keys.auth) return null;
  return { endpoint: sub.endpoint, keys: { p256dh: keys.p256dh, auth: keys.auth } };
}

/** Tell the server about this device. */
async function registerSubscription(
  sub: { endpoint: string; keys: { p256dh: string; auth: string } },
): Promise<void> {
  await pushFetch<{ subscriptions?: number }>("/api/push/subscribe", {
    endpoint: sub.endpoint,
    keys: sub.keys,
    // What this device asked to be woken for (lib/notifications.ts). The
    // server honours it per subscription, so a device that asked for less is
    // not woken for the rest.
    kinds: Object.keys(PUSH_KINDS),
  });
  rememberPushEndpoint(sub.endpoint);
}

/** Turn push on for this device. MUST be called from a user gesture: the OS
 *  permission dialog can only be raised from one (see requestNotifications),
 *  and Safari refuses the subscription otherwise. Returns the reason when it
 *  cannot, so the panel can say what to do instead of nothing happening. */
export async function enablePush(): Promise<{ ok: boolean; reason?: string }> {
  const support = pushSupport();
  if (support !== "ok") return { ok: false, reason: support };
  const permission = await requestNotifications();
  if (permission !== "granted") return { ok: false, reason: "blocked" };
  const reg = await pushRegistration();
  if (!reg) return { ok: false, reason: "unsupported" };
  const status = await pushStatus();
  if (!status.available || !status.public_key) return { ok: false, reason: "server" };
  try {
    const sub = await subscribe(reg, status.public_key);
    if (!sub) return { ok: false, reason: "unsupported" };
    await registerSubscription(sub);
    return { ok: true };
  } catch {
    // A refused subscription (a browser that will not reach its push service,
    // a key the browser rejects) must leave the switch off rather than claim
    // it is on.
    return { ok: false, reason: "failed" };
  }
}

/** Turn push off for this device: drop the browser's subscription AND the
 *  server's row, so neither side keeps the other's state. */
export async function disablePush(): Promise<void> {
  const reg = await pushRegistration();
  let endpoint = storedPush();
  try {
    const sub = reg ? await reg.pushManager.getSubscription() : null;
    if (sub) {
      endpoint = sub.endpoint || endpoint;
      await sub.unsubscribe();
    }
  } catch {
    /* the browser's own unsubscribe failed: the server row still goes */
  }
  if (endpoint) {
    try {
      await pushFetch<{ removed?: number }>("/api/push/unsubscribe", { endpoint });
    } catch {
      /* Signed out, or the server is unreachable. Nothing is lost: the push
         service itself answers 410 for an unsubscribed endpoint, and the
         server prunes the row on the next send (server/events.py). */
    }
  }
  rememberPushEndpoint(null);
}

/** Drop this device's subscription because the user signed out. Never throws:
 *  a sign-out must not fail on this. */
export async function dropPush(): Promise<void> {
  try {
    if (!storedPush()) return; // this device was never subscribed
    await disablePush();
  } catch {
    /* the sign-out carries on regardless */
  }
}

// A session that died under us (it expired, it was revoked, "log out
// everywhere") is a sign-out too: this device must stop being woken for the
// person who is no longer signed in. The two explicit sign-out buttons call
// dropPush themselves (AccountMenu, SecurityPanel) — this covers the path
// where nobody pressed anything.
onAuthLost(() => {
  void dropPush();
});

/** Re-register this device's subscription with the server.
 *
 *  Called on every load of the signed-in app, because a browser rotates a
 *  subscription's endpoint and its key material on its own schedule — a row
 *  the server kept would then encrypt to a key nobody holds, which is the
 *  silent failure mode of every push implementation. It also restores a row
 *  the server dropped (a password change revokes them: see auth.revoke_all).
 */
export async function refreshPush(): Promise<void> {
  if (pushSupport() !== "ok") return;
  if (!storedPush()) return; // this device never asked to be pushed to
  if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
  try {
    const reg = await pushRegistration();
    if (!reg) return;
    const status = await pushStatus();
    if (!status.available || !status.public_key) return;
    const sub = await subscribe(reg, status.public_key);
    if (sub) await registerSubscription(sub);
  } catch {
    /* the next load tries again; the subscription the browser holds is still
       valid, so nothing is lost by a refresh that could not be sent */
  }
}

/** Send a test frame to this user's devices and report what really happened —
 *  the button that lets somebody prove push on their own phone. */
export async function sendTestPush(): Promise<PushTestResult> {
  const res = await pushFetch<Partial<PushTestResult>>("/api/push/test", {});
  return {
    sent: Number(res.sent || 0),
    gone: Number(res.gone || 0),
    failed: Number(res.failed || 0),
    subscriptions: Number(res.subscriptions || 0),
  };
}

/** Fallback wording for a frame the server sent without a title. Every emitter
 *  in this app composes its own (the server owns the wording: it knows the
 *  artist and the album), so this is the safety net for an older server or a
 *  kind a newer one grew. */
const FALLBACK_TITLE: Partial<Record<EventKind, MessageKey>> = {
  wish_found: "notify.wish_found",
  download_started: "notify.download_started",
  download_done: "notify.download_done",
  upload_started: "notify.upload_started",
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
/** Subscribers to the OUTCOME kinds (`onAppEvent`): screens that draw what an
 *  outcome changed, rather than the tray that lists it. Plain callbacks, never
 *  React state — the listener decides what to do with a kind. */
const eventListeners = new Set<(kind: string) => void>();
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

/** Call `fn` with the `event` string of every app event this client has not
 *  seen yet — the server's own OUTCOME channel (see the module docstring), not
 *  the byte-level progress frames on `/ws/progress`.
 *
 *  A screen that draws what those outcomes CHANGE — the library tree, the home
 *  shelves — can refresh itself the moment one lands instead of waiting for
 *  its next stale window. Listeners never see a replayed ring frame, and one
 *  that throws cannot break the stream. Returns a stop function. */
export function onAppEvent(fn: (kind: string) => void): () => void {
  eventListeners.add(fn);
  return () => {
    eventListeners.delete(fn);
  };
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
  for (const fn of eventListeners) {
    try {
      fn(frame.event);
    } catch {
      /* a listener's own problem: the tray and the socket carry on */
    }
  }
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
