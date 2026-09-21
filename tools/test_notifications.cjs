#!/usr/bin/env node
/* The notification tray's contract — no framework, no browser.
 *
 * The store (web/src/lib/notifications.ts) is deliberately plain: no DOM, no
 * fetch, no React renderer, so Node can load it directly (Node 24 strips the
 * types) and this script can drive the REAL module the app ships. What it
 * asserts, in the order the acceptance criteria name them:
 *
 *   * a frame from /ws/events becomes one entry carrying kind, title, body, a
 *     timestamp, a stable id and a LINK;
 *   * one outcome notifies exactly once — the same frame replayed (the server
 *     replays its ring on reconnect) never becomes a second entry, and a frame
 *     with no kind is refused;
 *   * every click target the store can produce resolves to a route App.tsx
 *     really mounts, whether the emit site named it or the store derived it
 *     from the entity ids;
 *   * the badge: unread counts what is new, opening the panel clears it WITHOUT
 *     losing the entries, dismissing drops one and "Clear all" empties the log.
 *
 * Run: node tools/test_notifications.cjs
 */
const fs = require("node:fs");
const path = require("node:path");

const problems = [];
const fail = (message) => problems.push(message);
const check = (label, condition) => {
  console.log(`  ${condition ? "ok  " : "FAIL"} ${label}`);
  if (!condition) fail(label);
};

// A storage shim, installed BEFORE the module is loaded: the store reads its
// log at import time, exactly as it does in a browser reload.
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};

/** The routes the app actually mounts, from App.tsx itself — the ground truth
 *  for "clicking a notification goes somewhere real". Only the signed-in
 *  Routes block matters, but the auth routes are excluded by their paths
 *  anyway (a notification never links to /login). */
function mountedRoutes() {
  const src = fs.readFileSync(path.join(__dirname, "..", "web", "src", "App.tsx"), "utf8");
  const out = new Set();
  const re = /<Route\s+path="([^"]+)"/g;
  for (let m = re.exec(src); m; m = re.exec(src)) out.add(m[1]);
  return out;
}

/** Does a route pattern match a link? Query strings are not part of the path;
 *  `:param` matches one non-empty segment (React Router's own rule, which is
 *  why an album path is percent-encoded into a single segment). */
function matchesRoute(pattern, link) {
  const target = link.split("?")[0];
  const want = pattern.split("/").filter(Boolean);
  const got = target.split("/").filter(Boolean);
  if (want.length !== got.length) return false;
  return want.every((seg, i) => (seg.startsWith(":") ? got[i].length > 0 : seg === got[i]));
}

(async () => {
  const mod = await import("../web/src/lib/notifications.ts");
  const {
    ingest,
    notifications,
    unreadCount,
    markAllRead,
    dismiss,
    clearAll,
    linkFor,
    registerNavigator,
    openNotification,
    resolveTarget,
  } = mod;

  console.log("== the payload a frame must carry ==");
  const at = 1_712_345_678.9;
  const frame = {
    type: "event",
    event: "wish_found",
    title: "Wish found: A — B",
    body: "It downloaded and imported into your library.",
    data: { wish_id: 7, release_mbid: "abc", album_path: "F:/Music/A/B" },
    at,
    seq: 1_712_345_678_901,
  };
  const rec = ingest(frame);
  check("ingest returns the record", !!rec);
  check("kind is the frame's event", rec.kind === "wish_found");
  check("title and body are carried", rec.title === frame.title && rec.body === frame.body);
  check("the timestamp is the frame's own `at` (unix seconds -> ms)", rec.at === Math.round(at * 1000));
  check("the id is the server's event number", rec.id === "e1712345678901");
  check("a fresh entry is unread", rec.read === false);
  check("an entry carries a link", typeof rec.link === "string" && rec.link.startsWith("/"));
  check("the tray holds it", notifications().length === 1 && notifications()[0].id === rec.id);
  check("the badge counts it", unreadCount() === 1);

  console.log("== one outcome, one entry ==");
  check("a replayed frame is refused", ingest(frame) === null);
  check("the tray did not grow", notifications().length === 1);
  check("a frame with no kind is refused", ingest({ type: "event", title: "x" }) === null);
  const second = ingest({
    type: "event",
    event: "download_done",
    title: "Imported 2 albums",
    body: "all done",
    data: { link: "/library" },
    at: at + 1,
    seq: 1_712_345_679_901,
  });
  check("a different outcome is a second entry", !!second && notifications().length === 2);
  check("newest first", notifications()[0].id === second.id);

  console.log("== every click target resolves to a mounted route ==");
  const routes = mountedRoutes();
  check("App.tsx's route table was read", routes.size > 10 && routes.has("/album/:path") && routes.has("/import"));
  const links = [
    // named by the emit site (the server puts its own route in `data.link`)
    ingest({
      type: "event",
      event: "script_failed",
      title: "Grade failed",
      data: { link: "/in-progress" },
      at: at + 2,
      seq: 1_712_345_680_901,
    }).link,
    // derived from the entity ids the older emitters already publish
    linkFor("download_done", { album_path: "F:/Music/A/B" }),
    linkFor("import_ready", { album_path: "F:/Music/A/B" }),
    linkFor("import_needs_data", { album_path: "F:/Music/A/B" }),
    linkFor("wish_failed", {}),
    linkFor("wish_found", { album_path: "F:/Music/A/B" }),
    linkFor("script_done", {}),
    linkFor("grade_done", {}),
    linkFor("update_available", {}),
    // the two "it began" kinds: a running transfer lives in the queue
    linkFor("download_started", {}),
    linkFor("upload_started", {}),
  ];
  for (const link of links) {
    const hit = [...routes].some((p) => matchesRoute(p, link));
    check(`${link} is a real route`, hit);
  }
  check("an album path is one encoded segment", linkFor("download_done", { album_path: "F:/Music/A/B" }) === `/album/${encodeURIComponent("F:/Music/A/B")}`);
  check("an unknown kind with no subject links nowhere", linkFor("something_new", {}) === "");
  check(
    "an emit site's own link wins over the derived one",
    ingest({ type: "event", event: "update_available", title: "x", data: { link: "/library" }, at: at + 3, seq: 1_712_345_681_901 }).link === "/library"
  );

  console.log("== the badge, opening the panel and clearing ==");
  const seen = notifications().length;
  check("every entry that arrived is unread", unreadCount() === seen && seen > 3);
  markAllRead();
  check("opening the panel puts the badge out", unreadCount() === 0);
  check("...without losing the log", notifications().length === seen);
  const first = notifications()[notifications().length - 1];
  dismiss(first.id);
  check("dismiss drops exactly one entry", notifications().length === seen - 1 && !notifications().some((n) => n.id === first.id));
  clearAll();
  check("clear all empties the tray", notifications().length === 0 && unreadCount() === 0);

  console.log("== clicking an entry opens its subject ==");
  const opened = [];
  registerNavigator((to) => opened.push(to));
  const clickable = ingest({
    type: "event",
    event: "import_needs_data",
    title: "Needs a decision",
    data: { album_path: "F:/Music/A/B" },
    at: at + 4,
    seq: 1_712_345_682_901,
  });
  openNotification(clickable);
  check("the router is sent to the album's wizard", opened[0] === `/import?album=${encodeURIComponent("F:/Music/A/B")}`);
  check("clicking marks that entry read", unreadCount() === 0);
  check("an outside page opens in a new tab, not the router", (() => {
    const tabs = [];
    globalThis.window = { open: (u) => tabs.push(u), location: { assign: () => {} } };
    const external = ingest({
      type: "event",
      event: "update_available",
      title: "la musica 3.3.0 is available",
      data: { link: "/settings", url: "https://example.invalid/notes" },
      at: at + 5,
      seq: 1_712_345_683_901,
    });
    check("the in-app route still wins when both are present", resolveTarget(external) === "/settings");
    openNotification(external);
    return opened[opened.length - 1] === "/settings" && tabs.length === 0;
  })());

  console.log("== the log survives a reload ==");
  const persisted = store.get("mlo.notify.log");
  check("the log was written to storage", typeof persisted === "string" && persisted.includes('"kind"'));
  const reloaded = await import("../web/src/lib/notifications.ts?reload=1");
  check("a reload restores the entries", reloaded.notifications().length === notifications().length);
  check("...and their read state", reloaded.unreadCount() === unreadCount());
  reloaded.clearAll();
  check("clear all is what empties storage too", store.get("mlo.notify.log") === "[]");

  if (problems.length) {
    console.log(`\nFAIL — ${problems.length} problem(s)`);
    for (const p of problems) console.log(`  - ${p}`);
    // exitCode, not exit(): this script runs inside Node's type-stripping
    // loader, and tearing the process down from inside a dynamic import trips
    // a libuv teardown assertion on Windows (the failure would then look like
    // a crash instead of a report).
    process.exitCode = 1;
    return;
  }
  console.log("\nPASS — one entry per outcome, real click targets, a badge that clears on demand");
})();
