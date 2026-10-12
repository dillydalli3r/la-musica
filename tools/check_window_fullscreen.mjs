#!/usr/bin/env node
/* The desktop shell's WINDOW fullscreen, from the web side.
 *
 * The fullscreen player's top-bar button drives the OS WINDOW (`setFullscreen`)
 * in a Tauri shell; this check drives the module that does it,
 * `web/src/lib/windowFullscreen.ts`, against a stubbed Tauri IPC and pins the
 * rule the owner's report produced (2026-10-12: "the fullscreen button doesn't
 * fullscreen the window … black space at the bottom … the top bar is still
 * visible").
 *
 * The trap: asking tao for borderless fullscreen while the window is MAXIMIZED
 * leaves Windows in the maximized state — MEASURED on the host shell, the
 * client area stayed the work area (innerHeight 1040 of a 1080p screen) and no
 * resize fired, so the in-page title bar (which re-reads on a resize) never
 * hid. The same call from a restored window gave innerHeight 1080 and
 * `--mlo-titlebar-h` 0. So the module must UNMAXIMIZE before entering, and put
 * the maximized state back after leaving (tao restores the placement it
 * captured, which is the restored one).
 *
 * Cases, each on the same stub whose window state the calls really move:
 *   * a normal window: enter = is_fullscreen → is_maximized → set_fullscreen,
 *     no unmaximize; leave = set_fullscreen(false), no maximize;
 *   * a maximized window: enter unmaximizes first; leave maximizes again;
 *   * a stale memory: after a maximized enter that the OS (not this module)
 *     took back out, the next enter from a normal window must NOT maximize on
 *     exit;
 *   * a shell that refuses (no permission, no IPC): both paths resolve without
 *     throwing and the toggle answers with the real state.
 *
 * Run:  node tools/check_window_fullscreen.mjs
 * Exit codes: 0 pass, 1 a check failed (the failed ones are printed), 2 the
 * environment cannot run it (no web/node_modules). */

import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const webDir = path.join(here, "..", "web");

if (!existsSync(path.join(webDir, "node_modules"))) {
  console.error("[window-fullscreen] web/node_modules is missing — run `npm --prefix web install`.");
  process.exit(2);
}

const FAILED = [];
function check(name, ok, detail = "") {
  console.log(`  ${ok ? "ok  " : "FAIL"} ${name}${!ok && detail ? `  — ${detail}` : ""}`);
  if (!ok) FAILED.push(name);
}

// The page globals lib/api.ts reads; a Tauri webview is a browser first and
// only `__TAURI_INTERNALS__` is added, exactly like tools/check_backend_choice.mjs.
globalThis.window = globalThis;
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
  clear: () => store.clear(),
};
Object.defineProperty(globalThis, "navigator", {
  value: { userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36", maxTouchPoints: 0 },
  configurable: true,
  writable: true,
});
Object.defineProperty(globalThis, "isSecureContext", { value: true, configurable: true });

/** The window state the stub's calls really move, and every call in order. */
let fullscreenState = false;
let maximizedState = false;
let refuseAll = false;
const calls = [];
const invokeImpl = async (cmd, args) => {
  calls.push(cmd);
  if (refuseAll) throw new Error("window command refused");
  switch (cmd) {
    case "plugin:window|is_fullscreen": return fullscreenState;
    case "plugin:window|is_maximized": return maximizedState;
    case "plugin:window|set_fullscreen": fullscreenState = !!args?.value; return null;
    case "plugin:window|maximize": maximizedState = true; return null;
    case "plugin:window|unmaximize": maximizedState = false; return null;
    default: throw new Error(`unexpected command: ${cmd}`);
  }
};
// Tauri injects this global before any script runs; `invoke` (from
// @tauri-apps/api/core) reads `window.__TAURI_INTERNALS__.invoke`, and
// `getCurrentWindow()` reads the injected metadata's current window label.
globalThis.__TAURI_INTERNALS__ = {
  metadata: { currentWindow: { label: "main" }, currentWebview: { label: "main" } },
  invoke: (cmd, args) => invokeImpl(cmd, args),
};

const { createServer } = await import(
  `file://${path.join(webDir, "node_modules/vite/dist/node/index.js").replace(/\\/g, "/")}`);

const server = await createServer({
  configFile: path.join(webDir, "vite.config.ts"),
  root: webDir,
  server: { middlewareMode: true },
  appType: "custom",
  logLevel: "error",
});

/** Let the module's fire-and-forget leave path (a promise chain) land. */
const settle = () => new Promise((r) => setTimeout(r, 10));

try {
  const mod = await server.ssrLoadModule("/src/lib/windowFullscreen.ts");
  check("the shell drives the WINDOW, not the document", mod.WINDOW_FULLSCREEN === true,
    String(mod.WINDOW_FULLSCREEN));

  console.log("== a normal window ==");
  calls.length = 0;
  fullscreenState = false;
  maximizedState = false;
  const entered = await mod.toggleWindowFullscreen();
  check("enter: reads the state, checks for maximize, then sets fullscreen",
    JSON.stringify(calls) === JSON.stringify([
      "plugin:window|is_fullscreen", "plugin:window|is_maximized", "plugin:window|set_fullscreen",
      "plugin:window|is_fullscreen"]),
    JSON.stringify(calls));
  check("…and answers with the state it landed in", entered === true && fullscreenState === true,
    `returned ${entered}, state ${fullscreenState}`);
  calls.length = 0;
  mod.leaveWindowFullscreen();
  await settle();
  check("leave: fullscreen off, and NO maximize for a window nobody maximized",
    JSON.stringify(calls) === JSON.stringify([
      "plugin:window|is_fullscreen", "plugin:window|set_fullscreen"]) && maximizedState === false,
    `${JSON.stringify(calls)} maximized=${maximizedState}`);

  console.log("== a maximized window (the owner's report) ==");
  calls.length = 0;
  maximizedState = true;
  const enteredMax = await mod.toggleWindowFullscreen();
  check("enter: the window is RESTORED before the fullscreen request",
    JSON.stringify(calls) === JSON.stringify([
      "plugin:window|is_fullscreen", "plugin:window|is_maximized",
      "plugin:window|unmaximize", "plugin:window|set_fullscreen",
      "plugin:window|is_fullscreen"]),
    JSON.stringify(calls));
  check("…and the toggle still answers with the state it landed in",
    enteredMax === true && fullscreenState === true && maximizedState === false,
    `returned ${enteredMax}, fs ${fullscreenState}, max ${maximizedState}`);
  calls.length = 0;
  mod.leaveWindowFullscreen();
  await settle();
  check("leave: fullscreen off, then the maximized state is put BACK",
    JSON.stringify(calls) === JSON.stringify([
      "plugin:window|is_fullscreen", "plugin:window|set_fullscreen", "plugin:window|maximize"]) &&
      maximizedState === true,
    `${JSON.stringify(calls)} maximized=${maximizedState}`);

  console.log("== an OS leave in between must not leave a stale memory ==");
  // Enter from maximized (the module unmaximizes and remembers), then let the
  // OS take fullscreen away (F11): no maximize of the module's own, and the
  // NEXT enter is from a restored window — its exit must not maximize it.
  maximizedState = true;
  await mod.toggleWindowFullscreen();
  invokeImpl("plugin:window|set_fullscreen", { value: false });   // the OS did it
  calls.length = 0;
  await mod.toggleWindowFullscreen();
  check("the next enter checks the window again (no stale maximized memory)",
    calls.includes("plugin:window|is_maximized") && !calls.includes("plugin:window|unmaximize"),
    JSON.stringify(calls));
  calls.length = 0;
  mod.leaveWindowFullscreen();
  await settle();
  check("…and its exit does not maximize a window nobody maximized",
    !calls.includes("plugin:window|maximize") && maximizedState === false,
    `${JSON.stringify(calls)} maximized=${maximizedState}`);

  console.log("== a shell that refuses ==");
  refuseAll = true;
  calls.length = 0;
  const refused = await mod.toggleWindowFullscreen().catch((e) => `threw: ${e}`);
  check("a refused toggle resolves with the real (closed) state, never throws",
    refused === false, String(refused));
  let leaveThrew = false;
  try {
    mod.leaveWindowFullscreen();
    await settle();
  } catch { leaveThrew = true; }
  check("…and so does the leave path", leaveThrew === false);
  refuseAll = false;
} finally {
  await server.close().catch(() => {});
}

console.log(`\n${FAILED.length} failure(s)`);
process.exit(FAILED.length ? 1 : 0);