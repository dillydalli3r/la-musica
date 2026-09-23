#!/usr/bin/env node
/* The push client: what each platform is offered, and what subscribing does.
 *
 * Issue #48 asks for notifications that reach web, mobile and desktop. The
 * server half is tools/test_notifications.py (RFC 8291's own vector, the
 * fan-out, the pruning). This is the other half — the browser, whose rules are
 * not about our code but about what each platform will actually deliver:
 *
 *   * a secure page in a browser with service workers can subscribe; a plain
 *     http LAN page cannot (no worker, no secure context);
 *   * an iOS/iPadOS TAB cannot — Web Push there needs the app on the Home
 *     Screen, so the switch must not be offered;
 *   * the desktop shell cannot either (no service worker), and it says so;
 *   * and where subscribing IS possible, what goes to the server is proved:
 *     the server's own VAPID key as `applicationServerKey`, the endpoint, the
 *     two keys, and the kinds this device asked for.
 *
 * Nothing here talks to a push service: `navigator.serviceWorker`/`PushManager`
 * and `fetch` are the stub, so a subscription can be driven end to end without
 * one (a real subscribe needs the browser to reach ITS push service, which no
 * offline machine can do — see the report in the issue).
 *
 * The TIERS half is payload-driven: `PushRow` is rendered with each platform's
 * answer, straight through Vite, and only what a user would see is asserted.
 *
 * Run:  node tools/check_push.mjs
 * Exit codes: 0 pass, 1 a check failed (the failed ones are printed), 2 the
 * environment cannot run it (no web/node_modules).
 */
import { existsSync, readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";
import vm from "node:vm";

const here = path.dirname(fileURLToPath(import.meta.url));
const webDir = path.join(here, "..", "web");
const TAURI_PHASE = process.argv.includes("--tauri");

if (!existsSync(path.join(webDir, "node_modules"))) {
  console.error("[push] web/node_modules is missing — run `npm --prefix web install`.");
  process.exit(2);
}

const FAILED = [];
function check(name, ok, detail = "") {
  console.log(`  ${ok ? "ok  " : "FAIL"} ${name}${!ok && detail ? `  — ${detail}` : ""}`);
  if (!ok) FAILED.push(name);
}

/** A P-256 public key in base64url, the shape the server hands a client. */
const VAPID = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4";
const P256DH = "BJUtb2mnrWqiOzK_oKjE4iiGc-6a99faTseodik5wwxSXIEuyjz0qdfPRbd7MgIutI6x7NyKiGY0Ok3slPFfCc0";
const AUTH = "AzhttrkPAxhM242DATiPuA";

/** What the browser this harness pretends to be can do. Each case mutates it. */
const env = {
  secure: true,
  serviceWorker: true,
  pushManager: true,
  userAgent: "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
  maxTouchPoints: 0,
  standalone: false,
  permission: "granted",
};

// The page globals lib/notify.ts reads. `window` IS globalThis here, which is
// what makes `"PushManager" in window` and `isSecureContext` answer at all.
const store = new Map();
globalThis.window = globalThis;
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
  clear: () => store.clear(),
};
Object.defineProperty(globalThis, "navigator", {
  value: {
    get userAgent() {
      return env.userAgent;
    },
    get maxTouchPoints() {
      return env.maxTouchPoints;
    },
    get standalone() {
      return env.standalone;
    },
    get serviceWorker() {
      return env.serviceWorker ? registration.serviceWorker : undefined;
    },
  },
  configurable: true,
  writable: true,
});
Object.defineProperty(globalThis, "isSecureContext", {
  get: () => env.secure,
  configurable: true,
});
globalThis.Notification = {
  get permission() {
    return env.permission;
  },
  requestPermission: async () => env.permission,
};
globalThis.atob = globalThis.atob || ((b64) => Buffer.from(b64, "base64").toString("binary"));

/** The Push API is a global that either EXISTS or does not; a flag would not
 *  fool `"PushManager" in window`, which is the question lib/notify.ts asks. */
function setPushManager(present) {
  if (present) {
    Object.defineProperty(globalThis, "PushManager", {
      value: function PushManager() {},
      configurable: true,
      writable: true,
    });
  } else {
    delete globalThis.PushManager;
  }
}
setPushManager(true);

// ── the stubbed push service and server ─────────────────────────────────────
let subscription = null;
const subscribedWith = [];
const unsubscribed = [];
const requests = [];

function newSubscription() {
  return {
    endpoint: "https://push.example.invalid/send/harness",
    toJSON() {
      return { endpoint: this.endpoint, keys: { p256dh: P256DH, auth: AUTH } };
    },
    async unsubscribe() {
      unsubscribed.push(this.endpoint);
      subscription = null;
      return true;
    },
  };
}

const pushManager = {
  async getSubscription() {
    return subscription;
  },
  async subscribe(options) {
    subscribedWith.push(options);
    subscription = newSubscription();
    return subscription;
  },
};

const registration = {
  pushManager,
  active: {},
  async update() {},
  serviceWorker: {
    async getRegistration() {
      return registration;
    },
    async register() {
      return registration;
    },
    // A getter, not a value: the object literal cannot refer to itself before
    // it is initialized.
    get ready() {
      return Promise.resolve(registration);
    },
    addEventListener() {},
  },
};

let reply = (url) => ({});
let failNext = false;
globalThis.fetch = async (url, init) => {
  requests.push({ url: String(url), init: init || {} });
  if (failNext) throw new Error("the server is unreachable");
  return {
    ok: true,
    status: 200,
    headers: { get: () => null },
    json: async () => reply(String(url)),
  };
};

const webRequire = createRequire(path.join(webDir, "package.json"));
const { createServer } = await import(
  `file://${path.join(webDir, "node_modules/vite/dist/node/index.js").replace(/\\/g, "/")}`);
const React = webRequire("react");
const { renderToString } = webRequire("react-dom/server");

const server = await createServer({
  configFile: path.join(webDir, "vite.config.ts"),
  root: webDir,
  server: { middlewareMode: true },
  appType: "custom",
  logLevel: "error",
});

try {
  if (TAURI_PHASE) {
    // The desktop shell: Tauri injects this global into its webview before any
    // script runs, and `IN_TAURI` is read at import — hence a second process
    // rather than a second import.
    globalThis.__TAURI_INTERNALS__ = {};
    const notify = await server.ssrLoadModule("/src/lib/notify.ts");
    console.log("== the desktop shell ==");
    check("the Tauri shell is never offered a push subscription",
      notify.pushSupport() === "desktop", notify.pushSupport());
    console.log(`\n${FAILED.length} failure(s)`);
    await server.close();
    process.exit(FAILED.length ? 1 : 0);
  }

  const notify = await server.ssrLoadModule("/src/lib/notify.ts");
  const bell = await server.ssrLoadModule("/src/components/NotificationBell.tsx");

  console.log("== what each client can be offered ==");
  const DESKTOP_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36";
  const IPHONE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) Version/17.4 Safari/604.1";
  const CLEAN = { secure: true, userAgent: DESKTOP_UA, maxTouchPoints: 0,
    standalone: false, permission: "granted" };
  const supportCases = [
    ["a secure page in a real browser", {}, "ok"],
    ["a plain-http LAN page (no secure context)", { secure: false }, "insecure"],
    ["a browser with no Push API", { noPushApi: true }, "unsupported"],
    ["an iPhone Safari tab", { userAgent: IPHONE_UA }, "ios_install"],
    ["the same iPhone with the app on the Home Screen",
      { userAgent: IPHONE_UA, standalone: true }, "ok"],
    ["an iPad reporting itself as a Mac",
      { userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1", maxTouchPoints: 5 },
      "ios_install"],
  ];
  for (const [name, over, want] of supportCases) {
    Object.assign(env, CLEAN, over);
    setPushManager(!over.noPushApi);
    check(`${name} -> ${want}`, notify.pushSupport() === want, notify.pushSupport());
  }
  Object.assign(env, CLEAN);
  setPushManager(true);

  console.log("== turning it on: the subscription the server is told about ==");
  Object.defineProperty(globalThis, "PushManager", { value: function PushManager() {}, configurable: true });
  reply = (url) => (url.endsWith("/api/push/status") ? { available: true, public_key: VAPID, subscriptions: 0 } : { ok: true, subscriptions: 1 });
  const on = await notify.enablePush();
  const asked = subscribedWith[0] || {};
  const key = asked.applicationServerKey;
  check("subscribing asks the browser for a subscription", on.ok === true, JSON.stringify(on));
  check("...with the SERVER's own VAPID key, as raw bytes",
    key instanceof Uint8Array && key.length === 65 && key[0] === 4
    && Buffer.from(key).toString("base64url") === VAPID,
    key ? `len ${key.length}` : "no key");
  check("...visible-notification-only (Chrome requires it)", asked.userVisibleOnly === true);
  const post = requests.find((r) => r.url.endsWith("/api/push/subscribe"));
  const body = post ? JSON.parse(post.init.body) : {};
  check("the endpoint and both keys reach the server",
    body.endpoint === "https://push.example.invalid/send/harness"
    && body.keys?.p256dh === P256DH && body.keys?.auth === AUTH, JSON.stringify(body));
  check("...with the kinds this device asked for",
    Array.isArray(body.kinds) && body.kinds.includes("import_done")
    && body.kinds.includes("wish_found") && !body.kinds.includes("script_done"),
    JSON.stringify(body.kinds));
  check("...over the session's own credentials", post?.init.credentials === "same-origin");
  check("...and the device remembers it is subscribed", notify.pushEnabled() === true);

  console.log("== keeping it fresh ==");
  requests.length = 0;
  await notify.refreshPush();
  const refreshed = requests.find((r) => r.url.endsWith("/api/push/subscribe"));
  check("a load of the signed-in app re-registers the subscription",
    !!refreshed && JSON.parse(refreshed.init.body).endpoint === "https://push.example.invalid/send/harness");
  store.delete("mlo.push.endpoint");
  requests.length = 0;
  await notify.refreshPush();
  check("a device that never asked is never subscribed behind its back", requests.length === 0);
  store.set("mlo.push.endpoint", "https://push.example.invalid/send/harness");
  env.permission = "denied";
  requests.length = 0;
  await notify.refreshPush();
  check("permission taken away stops the refresh", requests.length === 0);
  env.permission = "granted";

  console.log("== the test button ==");
  reply = () => ({ sent: 1, gone: 0, failed: 2, subscriptions: 3 });
  const tested = await notify.sendTestPush();
  check("the answer is what the server really did, not a hopeful yes",
    tested.sent === 1 && tested.failed === 2 && tested.subscriptions === 3, JSON.stringify(tested));
  check("...and it goes through the test route",
    requests.some((r) => r.url.endsWith("/api/push/test") && r.init.method === "POST"));

  console.log("== turning it off, and signing out ==");
  requests.length = 0;
  await notify.disablePush();
  check("the browser's own subscription is dropped", unsubscribed.length === 1);
  check("...and the server is told to forget the device",
    requests.some((r) => r.url.endsWith("/api/push/unsubscribe")
      && JSON.parse(r.init.body).endpoint === "https://push.example.invalid/send/harness"));
  check("...and this device no longer claims to be subscribed", notify.pushEnabled() === false);

  await pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: new Uint8Array(65) });
  store.set("mlo.push.endpoint", "https://push.example.invalid/send/harness");
  failNext = true;
  let threw = false;
  try {
    await notify.dropPush();
  } catch {
    threw = true;
  }
  check("a sign-out never fails on push, even with the server down", threw === false);
  check("...and the device still stops claiming the subscription", notify.pushEnabled() === false);
  check("...with the browser subscription dropped as well", unsubscribed.length === 2);
  failNext = false;

  console.log("== what the panel shows, per platform (payload-driven) ==");
  const status = { available: true, public_key: VAPID, subscriptions: 1 };
  const row = (props) =>
    renderToString(React.createElement(bell.PushRow, {
      on: true, busy: false, onToggle: () => {}, onTest: () => {}, ...props,
    })).replace(/<!-- -->/g, "");

  const capable = row({ support: "ok", status });
  check("a browser that can subscribe gets the switch", capable.includes('role="switch"'));
  check("...and the test button with it", capable.includes("Send a test notification"));

  const desktop = row({ support: "desktop", status });
  check("the desktop shell is NOT offered a switch it cannot honour",
    !desktop.includes('role="switch"'), desktop.slice(0, 120));
  check("...and is told what it can do instead",
    desktop.includes("desktop app notifies you while it is running"));

  const ios = row({ support: "ios_install", status });
  check("an iOS tab is NOT offered a switch", !ios.includes('role="switch"'));
  check("...and is told to add the app to the Home Screen", ios.includes("Home Screen"));

  const insecure = row({ support: "insecure", status });
  check("a plain-http page is NOT offered a switch", !insecure.includes('role="switch"'));
  check("...and hears why (https)", insecure.includes("secure (https) page"));

  const unsupported = row({ support: "unsupported", status });
  check("a browser without service workers is NOT offered a switch",
    !unsupported.includes('role="switch"'));

  const unkeyed = row({ support: "ok", status: { available: false, public_key: "", subscriptions: 0 } });
  check("a server that cannot send push is NOT offered a switch either",
    !unkeyed.includes('role="switch"'));
  check("...and says so", unkeyed.includes("cannot send push"));

  const off = renderToString(React.createElement(bell.PushRow, {
    support: "ok", status, on: false, busy: false, onToggle: () => {}, onTest: () => {},
  })).replace(/<!-- -->/g, "");
  check("a device that is not subscribed gets no test button",
    off.includes('role="switch"') && !off.includes("Send a test notification"));

  console.log("== the service worker's own handlers (the closed-app path) ==");
  // sw.js is plain script against the worker globals, so it can be loaded into
  // a fake `self` and driven directly: the push/notificationclick pair is the
  // ONLY code that runs when the app is closed, and a browser probe cannot
  // reach it deterministically (a real click needs user activation).
  const swSource = readFileSync(path.join(webDir, "public", "sw.js"), "utf8");
  const loadWorker = () => {
    const handlers = new Map();
    const shown = [];
    const popups = [];
    const contexts = [];
    const worker = {
      location: { origin: "https://music.example" },
      addEventListener: (type, fn) => handlers.set(type, fn),
      skipWaiting: async () => {},
      registration: {
        showNotification: async (title, options) => {
          shown.push({ title, options: options || {} });
        },
        pushManager: {
          subscribe: async (options) => {
            contexts.push(options);
            return {
              endpoint: "https://push.example.invalid/send/rotated",
              toJSON: () => ({
                endpoint: "https://push.example.invalid/send/rotated",
                keys: { p256dh: "p256dh-key", auth: "auth-key" },
              }),
            };
          },
        },
      },
      clients: {
        matchAll: async () => [],
        openWindow: async (url) => {
          popups.push(url);
          return null;
        },
      },
      fetch: async () => ({
        ok: true,
        json: async () => ({ available: true, public_key: VAPID }),
      }),
    };
    worker.self = worker;
    // `fetch` is delegated rather than copied: the worker script (correctly)
    // calls the GLOBAL fetch, and this phase replaces it per case.
    const sandbox = { self: worker, console, URL, Promise, JSON, Object, Array, String, Error,
      atob: globalThis.atob, Uint8Array, fetch: (...args) => worker.fetch(...args) };
    vm.createContext(sandbox);
    vm.runInContext(swSource, sandbox, { filename: "sw.js" });
    return { handlers, shown, popups, contexts, worker };
  };
  /** Drive one handler with the event shape the browser would hand it. */
  const fire = async (handlers, type, event) => {
    let settled;
    event.waitUntil = (p) => {
      settled = p;
    };
    handlers.get(type)(event);
    await settled;
  };

  const workerRun = loadWorker();
  check("the worker registers a push handler", typeof workerRun.handlers.get("push") === "function");
  check("...and a click handler", typeof workerRun.handlers.get("notificationclick") === "function");
  check("...and survives a subscription change",
    typeof workerRun.handlers.get("pushsubscriptionchange") === "function");

  const deliver = (w, payload) =>
    fire(w.handlers, "push", {
      data: { json: () => JSON.parse(payload), text: () => payload },
    });

  await deliver(workerRun, JSON.stringify({
    type: "event", event: "import_done", title: "Imported Kind of Blue",
    body: "21 scripts ran", data: { link: "/album/Kind%20of%20Blue" },
  }));
  check("a pushed frame raises the notification with the server's wording",
    workerRun.shown.length === 1 && workerRun.shown[0].title === "Imported Kind of Blue"
    && workerRun.shown[0].options.body === "21 scripts ran", JSON.stringify(workerRun.shown));
  check("...and the album as its click target",
    workerRun.shown[0]?.options.data?.url === "/album/Kind%20of%20Blue",
    JSON.stringify(workerRun.shown[0]?.options?.data));

  for (const [name, payload, want] of [
    ["a bare `link` (what an older emitter sent)", { title: "T", link: "/library" }, "/library"],
    ["a `url` (what the page's own notifications carry)", { title: "T", url: "/soulseek" }, "/soulseek"],
    ["no subject at all", { title: "T" }, "/"],
  ]) {
    const w = loadWorker();
    await deliver(w, JSON.stringify(payload));
    check(`...and reads ${name}`, w.shown[0]?.options?.data?.url === want,
      String(w.shown[0]?.options?.data?.url));
  }

  // The click: what happens to the window, per answer the browser can give.
  const clickWorker = loadWorker();
  const navigations = [];
  let focused = 0;
  clickWorker.worker.clients.matchAll = async () => [
    {
      url: "https://music.example/setup",
      navigate: async (url) => {
        navigations.push(url);
        return { focus: async () => { focused += 1; return "focused"; } };
      },
    },
  ];
  await fire(clickWorker.handlers, "notificationclick", {
    notification: { close() {}, data: { url: "/album/Kind%20of%20Blue" } },
  });
  check("clicking a notification navigates to the album it is about",
    navigations[0] === "https://music.example/album/Kind%20of%20Blue", String(navigations[0]));
  check("...and focuses that window", focused === 1 && clickWorker.popups.length === 0);

  const refusing = loadWorker();
  refusing.worker.clients.matchAll = async () => [
    { url: "https://music.example/", navigate: async () => null },
    { url: "https://music.example/", navigate: async () => ({ focus: async () => "focused" }) },
  ];
  await fire(refusing.handlers, "notificationclick", {
    notification: { close() {}, data: { url: "/library" } },
  });
  check("a window that refuses to move is skipped, not swallowed",
    refusing.popups.length === 0);

  const nowhere = loadWorker();
  nowhere.worker.clients.matchAll = async () => [];
  await fire(nowhere.handlers, "notificationclick", {
    notification: { close() {}, data: { url: "/library" } },
  });
  check("with no window open the app is opened at the subject",
    nowhere.popups[0] === "https://music.example/library", String(nowhere.popups));

  const deniedFocus = loadWorker();
  deniedFocus.worker.clients.matchAll = async () => [
    { url: "https://music.example/", navigate: async () => ({ focus: async () => { throw new Error("denied"); } }) },
  ];
  await fire(deniedFocus.handlers, "notificationclick", {
    notification: { close() {}, data: { url: "/library" } },
  });
  check("a refused focus does not open a SECOND window", deniedFocus.popups.length === 0);

  // A rotated subscription: the worker re-subscribes and tells the server.
  const rotated = loadWorker();
  const posts = [];
  rotated.worker.fetch = async (url, init) => {
    posts.push({ url: String(url), init: init || {} });
    if (String(url).endsWith("/api/push/status")) {
      return { ok: true, json: async () => ({ available: true, public_key: VAPID }) };
    }
    return { ok: true, json: async () => ({ ok: true }) };
  };
  await fire(rotated.handlers, "pushsubscriptionchange", {
    oldSubscription: null,
    newSubscription: null,
  });
  const resub = posts.find((p) => p.url.endsWith("/api/push/subscribe"));
  check("a rotated endpoint is re-subscribed with the server's own key",
    !!resub && JSON.parse(resub.init.body).endpoint === "https://push.example.invalid/send/rotated",
    JSON.stringify(posts.map((p) => p.url)));
  check("...signed by the same session the page uses", resub?.init.credentials === "include");

  console.log("\n== the desktop shell (a second process: Tauri is read at import) ==");
  const sub = spawnSync(process.execPath, [fileURLToPath(import.meta.url), "--tauri"], {
    encoding: "utf8",
  });
  const subLines = String(sub.stdout || "")
    .split("\n")
    .filter((l) => l.includes("  ok  ") || l.includes("FAIL"))
    .map((l) => l.replace(/^\s*/, ""));
  for (const line of subLines) console.log(`  ${line.replace(/^ok\s+/, "ok   ")}`);
  check("the desktop shell's own phase passed", sub.status === 0,
    (sub.stderr || "").split("\n").slice(0, 3).join(" "));
} finally {
  await server.close();
}

console.log(`\n${FAILED.length} failure(s)`);
process.exit(FAILED.length ? 1 : 0);
