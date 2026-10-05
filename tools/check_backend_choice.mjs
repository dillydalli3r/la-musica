#!/usr/bin/env node
/* The desktop shell's first-run backend choice, from the web side.
 *
 * A desktop shell can run the app's own `mlo-server` or talk to a server the
 * user runs; until the user picks, it reports `mode: "unset"`. This check
 * drives `web/src/lib/backendShell.ts` against a stubbed Tauri IPC and proves
 * the three cases the page relies on:
 *
 *   * a shell that answers `shell_backend_choice` with each mode maps through,
 *     and `choose_backend` carries the picked mode to the shell;
 *   * a shell whose invoke REJECTS (a mobile shell, or a desktop one without
 *     the command) resolves null and never throws — that is "no chooser here",
 *     and the classic wizard stays in charge;
 *   * with no `__TAURI_INTERNALS__` at all (a plain browser) it is null too.
 *
 * `IN_TAURI` is read when api.ts is first imported, so the browser case runs in
 * a second process — the same trick tools/check_push.mjs uses for its Tauri
 * phase. Nothing here reads a real shell.
 *
 * Run:  node tools/check_backend_choice.mjs
 * Exit codes: 0 pass, 1 a check failed (the failed ones are printed), 2 the
 * environment cannot run it (no web/node_modules).
 */
import { existsSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const webDir = path.join(here, "..", "web");
const NO_TAURI = process.argv.includes("--no-tauri");

if (!existsSync(path.join(webDir, "node_modules"))) {
  console.error("[backend-choice] web/node_modules is missing — run `npm --prefix web install`.");
  process.exit(2);
}

const FAILED = [];
function check(name, ok, detail = "") {
  console.log(`  ${ok ? "ok  " : "FAIL"} ${name}${!ok && detail ? `  — ${detail}` : ""}`);
  if (!ok) FAILED.push(name);
}

// The page globals lib/api.ts and its imports read. A Tauri webview is a
// browser first; only `__TAURI_INTERNALS__` is added for the shell cases.
globalThis.window = globalThis;
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
  clear: () => store.clear(),
};
Object.defineProperty(globalThis, "navigator", {
  value: { userAgent: "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36", maxTouchPoints: 0 },
  configurable: true,
  writable: true,
});
Object.defineProperty(globalThis, "isSecureContext", { value: true, configurable: true });

/** What the shell's IPC answers. Each case replaces `invokeImpl`. */
let currentMode = "unset";
let currentStatus = "remote";
const calls = [];
let invokeImpl = async (cmd, args) => {
  calls.push({ cmd, args });
  if (cmd === "shell_backend_choice")
    return { mode: currentMode, music_folder: "/music", status: currentStatus };
  if (cmd === "choose_backend") return null;
  throw new Error(`unexpected command: ${cmd}`);
};
if (!NO_TAURI) {
  // Tauri injects this global before any script runs; `invoke` (from
  // @tauri-apps/api/core) reads `window.__TAURI_INTERNALS__.invoke`.
  globalThis.__TAURI_INTERNALS__ = { invoke: (cmd, args) => invokeImpl(cmd, args) };
}

const { createServer } = await import(
  `file://${path.join(webDir, "node_modules/vite/dist/node/index.js").replace(/\\/g, "/")}`);

const server = await createServer({
  configFile: path.join(webDir, "vite.config.ts"),
  root: webDir,
  server: { middlewareMode: true },
  appType: "custom",
  logLevel: "error",
});

try {
  const shell = await server.ssrLoadModule("/src/lib/backendShell.ts");

  if (NO_TAURI) {
    console.log("== no shell at all (a plain browser) ==");
    const choice = await shell.shellBackendChoice();
    check("shellBackendChoice() is null with no Tauri shell", choice === null, JSON.stringify(choice));
    const recorded = await shell.chooseBackend("local");
    check("chooseBackend() reports false with no Tauri shell", recorded === false, String(recorded));
    console.log(`\n${FAILED.length} failure(s)`);
    await server.close();
    process.exit(FAILED.length ? 1 : 0);
  }

  console.log("== what the shell says (a desktop shell with the command) ==");
  // `status` is the live backend state on the SAME ask, and it is the reason a
  // page that entered local mode (which reloads it) does not fall through to
  // asking for a server ADDRESS while its own backend is still booting: the
  // events have already been emitted by then, so the answer must carry it.
  currentMode = "local";
  currentStatus = "starting";
  const starting = await shell.shellBackendChoice();
  check("a starting local backend reads as starting", shell.backendStartingUp() === true, JSON.stringify(starting));
  check("...and not as a failure", shell.backendUnavailable() === false, String(shell.backendUnavailable()));
  currentStatus = "stopped";
  await shell.shellBackendChoice();
  check("a local backend that is not there reads as unavailable", shell.backendUnavailable() === true);
  currentStatus = "running";
  await shell.shellBackendChoice();
  check("a running local backend clears both flags",
    shell.backendUnavailable() === false && shell.backendStartingUp() === false);
  currentMode = "unset";
  currentStatus = "remote";

  for (const mode of ["local", "remote", "unset"]) {
    currentMode = mode;
    const choice = await shell.shellBackendChoice();
    check(`shell_backend_choice "${mode}" maps through`, choice?.mode === mode, JSON.stringify(choice));
    check(`...carrying the shell's music folder`, choice?.music_folder === "/music", JSON.stringify(choice));
  }

  console.log("== recording the pick ==");
  for (const mode of ["local", "remote"]) {
    calls.length = 0;
    const ok = await shell.chooseBackend(mode);
    const call = calls.find((c) => c.cmd === "choose_backend");
    check(`chooseBackend("${mode}") reaches the shell`, ok === true, JSON.stringify(calls));
    check(`...with mode "${mode}"`, call?.args?.mode === mode, JSON.stringify(call?.args ?? null));
  }

  console.log("== a local backend that did not come up, then did ==");
  // The shell reports a failed start as `stopped` with no origin; the chooser
  // renders that as the reason plus two ways out. Driving it needs no new
  // machinery: `listen()` is `transformCallback(handler)` (the host stores the
  // function under an id) followed by `plugin:event|listen`, so the stub keeps
  // the callback table and the module's own handler is called with the same
  // payload the shell emits (`backend_handle::emit_unavailable`).
  {
    const callbacks = new Map();
    let nextId = 1;
    const ipcInvoke = globalThis.__TAURI_INTERNALS__.invoke;
    globalThis.__TAURI_INTERNALS__.transformCallback = (cb) => {
      const id = nextId++;
      callbacks.set(id, cb);
      return id;
    };
    globalThis.__TAURI_INTERNALS__.invoke = async (cmd, args) => {
      if (cmd === "plugin:event|listen") return args.handler;
      if (cmd === "plugin:event|unlisten") return null;
      return ipcInvoke(cmd, args);
    };

    let fired = 0;
    shell.subscribeBackendFailure(() => { fired += 1; });
    const unlisten = await shell.attachBackendShell();
    check("attachBackendShell returns an undo function", typeof unlisten === "function", String(unlisten));

    const handler = callbacks.get(1);
    check("the module registered exactly one mlo-backend listener",
      callbacks.size === 1 && typeof handler === "function",
      `callbacks=${callbacks.size} handler=${typeof handler}`);

    if (typeof handler === "function") {
      check("no failure is reported before the shell says anything",
        shell.backendUnavailable() === false, String(shell.backendUnavailable()));
      handler({ event: "mlo-backend", id: 1, payload: { status: "stopped", origin: "", port: 0, mode: "local", needs_choice: false, local: true } });
      check("a stopped local backend reads as unavailable",
        shell.backendUnavailable() === true, String(shell.backendUnavailable()));
      check("...and notifies the watcher", fired === 1, String(fired));
      handler({ event: "mlo-backend", id: 2, payload: { status: "running", origin: "http://127.0.0.1:8011", port: 8011, mode: "local", needs_choice: false, local: true } });
      check("a running backend clears it again",
        shell.backendUnavailable() === false, String(shell.backendUnavailable()));
      // A second attach must NOT register again: two listeners would handle
      // every state twice and the first would leak.
      await shell.attachBackendShell();
      check("attaching twice registers nothing extra", callbacks.size === 1, `callbacks=${callbacks.size}`);
      unlisten();
      check("the undo function unregisters", callbacks.size === 1, String(callbacks.size));
    }
  }

  console.log("== a shell with no chooser (mobile, or an older desktop) ==");
  invokeImpl = async (cmd) => {
    calls.push({ cmd });
    throw new Error(`command not found: ${cmd}`);
  };
  let threw = null;
  let rejected = null;
  try {
    rejected = await shell.shellBackendChoice();
  } catch (err) {
    threw = err;
  }
  check("a rejecting invoke resolves null, never throws",
    threw === null && rejected === null,
    threw ? String(threw) : JSON.stringify(rejected));
  let recorded = null;
  threw = null;
  try {
    recorded = await shell.chooseBackend("local");
  } catch (err) {
    threw = err;
  }
  check("...and chooseBackend reports failure instead of throwing", threw === null && recorded === false);

  console.log("\n== the browser case (a second process: IN_TAURI is read at import) ==");
  const sub = spawnSync(process.execPath, [fileURLToPath(import.meta.url), "--no-tauri"], {
    encoding: "utf8",
  });
  for (const line of String(sub.stdout || "")
    .split("\n")
    .filter((l) => l.includes("  ok  ") || l.includes("FAIL"))) {
    console.log(line.replace(/^\s*/, "  "));
  }
  check("the browser process passes", sub.status === 0, `exit ${sub.status}${sub.stderr ? `: ${sub.stderr}` : ""}`);
} finally {
  await server.close();
}

console.log(`\n${FAILED.length} failure(s)`);
process.exit(FAILED.length ? 1 : 0);
