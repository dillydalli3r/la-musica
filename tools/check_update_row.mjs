#!/usr/bin/env node
/* The desktop app's own update row, in every state it can be in.
 *
 * The row is the only place an INSTALLED app can be updated from, and "an update
 * is available" has to mean four different things there: you are current (with a
 * way to ask again), there is one (what it is, what it will replace, one
 * button), it is downloading (real bytes behind a bar), and it is installing
 * (the app is about to be replaced, so a percentage stops meaning anything).
 * A fifth is the honest one: the check failed, and being offline is a normal way
 * for that to happen.
 *
 * This renders the REAL app in a real browser with the shell's commands stubbed
 * (`__TAURI_INTERNALS__`, the same global Tauri injects before any script runs),
 * drives the row through all five, and reads what a user would see. It also
 * asserts the OTHER half of the rule: a client that is not the desktop shell —
 * a browser, a phone — is offered no installer at all.
 *
 * Run:  node tools/check_update_row.mjs
 * Exit codes: 0 pass, 1 a check failed (the failing ones are printed), 2 the
 * environment cannot run it (no web/node_modules, no Playwright browser).
 */
import { existsSync } from "node:fs";
import { mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const webDir = path.join(here, "..", "web");
if (!existsSync(path.join(webDir, "node_modules"))) {
  console.error("[update-row] web/node_modules is missing — run `npm --prefix web install`.");
  process.exit(2);
}
const webRequire = createRequire(path.join(webDir, "package.json"));
let chromium;
try {
  ({ chromium } = webRequire("playwright"));
} catch {
  console.error("[update-row] Playwright not found — `npm --prefix web i -D playwright`.");
  process.exit(2);
}
const { createServer } = await import(
  `file://${path.join(webDir, "node_modules/vite/dist/node/index.js").replace(/\\/g, "/")}`);

// Tailwind resolves its `content` globs against the process cwd (see the note in
// check_library_az.mjs): without this the page arrives unstyled.
process.chdir(webDir);

const vite = await createServer({
  configFile: path.join(webDir, "vite.config.ts"), root: webDir, logLevel: "error",
  server: { host: "127.0.0.1", port: 8011, strictPort: false },
});
await vite.listen();
const port = vite.httpServer.address().port;

const failures = [];
let checks = 0;
const check = (name, pass, detail = "") => {
  checks += 1;
  console.log(`${pass ? "  ok  " : "  FAIL"} ${name}${pass || !detail ? "" : " — " + detail}`);
  if (!pass) failures.push(name);
};

/** The shell, as the page sees it: the five commands this row uses, and a way
 *  for the check to push an `mlo-update` event at it. Everything else resolves
 *  to nothing, which is what the app's other Tauri calls do in a browser. */
const SHELL_STUB = () => {
  const handlers = new Map();
  let nextId = 1;
  window.__mlo = {
    answer: { current: "5.2.0", offer: null },
    // Read from storage, not baked in: this script runs again on every
    // navigation, so a flag the check sets in the page would be wiped by the
    // reload the next state needs.
    failWith: window.localStorage.getItem("mlo.check.fail") || "",
    emit(event, payload) {
      for (const cb of handlers.get(event) || []) cb({ event, id: 0, payload });
    },
  };
  const invoke = async (cmd, args) => {
    if (cmd === "update_check") {
      if (window.__mlo.failWith) throw new Error(window.__mlo.failWith);
      return window.__mlo.answer;
    }
    if (cmd === "update_install") {
      // Never resolves: on Windows the shell exits so the installer can take
      // over, and everywhere else it re-execs itself. The row is meant to sit
      // in "installing" for as long as that takes.
      return new Promise(() => {});
    }
    if (cmd === "plugin:event|listen") {
      const id = args?.handler;
      const list = handlers.get(args?.event) || [];
      list.push((ev) => window.__TAURI_INTERNALS__.callbacks.get(id)?.(ev));
      handlers.set(args?.event, list);
      return nextId++;
    }
    if (cmd === "plugin:window|is_maximized") return false;
    if (cmd === "plugin:window|is_decorated") return false;
    return undefined;
  };
  window.__TAURI_INTERNALS__ = {
    invoke,
    callbacks: new Map(),
    transformCallback(cb) {
      const id = nextId++;
      window.__TAURI_INTERNALS__.callbacks.set(id, cb);
      return id;
    },
    convertFileSrc: (p) => p,
    // Tauri injects this before any script runs, and the shell's own title bar
    // reads it (`getCurrentWindow()`), so a stub without it crashes the app
    // rather than rendering it.
    metadata: {
      currentWindow: { label: "main" },
      currentWebview: { label: "main", windowLabel: "main" },
    },
  };
  // Tauri injects this too, and `@tauri-apps/api/event` calls it when a listener
  // is removed — without it the app throws on unmount (a rejected promise the
  // page reports as an unhandled error).
  window.__TAURI_EVENT_PLUGIN_INTERNALS__ = { unregisterListener() {} };
};

const browser = await chromium.launch();
const shots = "F:/tmp/mlo-update-row-shots";
try {
  const context = await browser.newContext({ serviceWorkers: "block" });
  await context.addInitScript(SHELL_STUB);
  const page = await context.newPage();
  await page.route("**/sw.js", (r) => r.abort());
  await page.route("**/api/**", (route) => {
    const url = route.request().url();
    const send = (body) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    if (url.includes("/api/auth/status")) return send({ required: false, authenticated: true });
    if (url.includes("/api/config")) return send({ music_folder: "F:/Music", first_run_done: true });
    return undefined;               // everything else stays pending
  });
  mkdirSync(shots, { recursive: true });

  const row = () => page.$$eval("[data-update-row]", (nodes) => nodes.map((el) => ({
    text: (el.innerText || "").replace(/\s+/g, " ").trim(),
    bar: el.querySelector(".bg-accent")?.getBoundingClientRect().width ?? null,
    buttons: [...el.querySelectorAll("button")].map((b) => b.textContent.trim()),
  }))[0] || null);

  // ---- the desktop shell, and only it --------------------------------
  await page.goto(`http://127.0.0.1:${port}/settings?tab=security`);
  await page.waitForSelector("[data-update-row]", { timeout: 20000 });
  const current = await row();
  check("the row renders in the desktop shell", !!current, JSON.stringify(current));
  check("…and says which version this is", /5\.2\.0/.test(current?.text || ""), current?.text);
  check("…with a way to ask again", (current?.buttons || []).includes("Check now"), JSON.stringify(current?.buttons));

  // ---- an update exists ---------------------------------------------
  await page.evaluate(() => {
    window.__mlo.answer = {
      current: "5.2.0",
      offer: { version: "5.3.0", notes: "Fixes the thing you reported.", date: "2026-10-06 21:00:00.0 +00:00:00" },
    };
  });
  await page.getByRole("button", { name: "Check now" }).click();
  await page.waitForFunction(() => /5\.3\.0/.test(document.querySelector("[data-update-row]")?.innerText || ""), { timeout: 5000 });
  const offered = await row();
  check("an available release names both versions", /5\.3\.0/.test(offered.text) && /5\.2\.0/.test(offered.text), offered.text);
  check("…and carries the release's own notes", /Fixes the thing you reported/.test(offered.text), offered.text);
  check("…and offers exactly one action", (offered.buttons || []).join("|") === "Update now", JSON.stringify(offered.buttons));
  await page.screenshot({ path: path.join(shots, "offer.png") });

  // ---- downloading, with real bytes behind the bar ------------------
  await page.getByRole("button", { name: "Update now" }).click();
  await page.evaluate(() => window.__mlo.emit("mlo-update", { stage: "downloading", downloaded: 5, total: 10 }));
  // The bar animates its width (`transition-[width] duration-200`), so a
  // measurement taken on the frame the event lands reads a bar still on its way
  // — the text is immediate, the fill is not.
  await page.waitForTimeout(350);
  const half = await row();
  check("a download reports its percentage", /50%/.test(half.text), half.text);
  const ratios = await page.$eval("[data-update-row]", (el) => {
    const fill = el.querySelector(".bg-accent");
    const track = fill?.parentElement;
    return fill && track
      ? { ratio: fill.getBoundingClientRect().width / track.getBoundingClientRect().width }
      : null;
  });
  check("…and the bar is half full", ratios !== null && Math.abs(ratios.ratio - 0.5) < 0.02,
        JSON.stringify(ratios));
  check("…with no button left to press twice", (half.buttons || []).length === 0, JSON.stringify(half.buttons));
  await page.screenshot({ path: path.join(shots, "downloading.png") });

  // ---- installing: the swap has started -----------------------------
  await page.evaluate(() => window.__mlo.emit("mlo-update", { stage: "installing", downloaded: 0, total: null }));
  await page.waitForFunction(() => /Installing/.test(document.querySelector("[data-update-row]")?.innerText || ""), { timeout: 5000 });
  const installing = await row();
  check("installing says so, and that the app restarts itself", /Installing la musica 5\.3\.0/.test(installing.text), installing.text);
  check("…and stops drawing a percentage it cannot advance", installing.bar === null, `bar=${installing.bar}`);
  await page.screenshot({ path: path.join(shots, "installing.png") });

  // ---- the honest failure ---------------------------------------------
  await page.evaluate(() => {
    window.localStorage.setItem("mlo.check.fail", "Could not fetch a valid release JSON from the remote");
  });
  await page.reload();
  await page.waitForSelector("[data-update-row]", { timeout: 20000 });
  await page.waitForFunction(() => /Could not check/.test(document.querySelector("[data-update-row]")?.innerText || ""), { timeout: 5000 });
  const failed = await row();
  check("a failed check is a sentence, not a broken row", /Could not check for updates/.test(failed.text), failed.text);
  check("…and the way to try again is still there", (failed.buttons || []).includes("Check now"), JSON.stringify(failed.buttons));

  // ---- a client that is not the desktop shell -------------------------
  const plain = await browser.newContext({ serviceWorkers: "block" });
  const plainPage = await plain.newPage();
  await plainPage.route("**/api/**", (route) => {
    const url = route.request().url();
    if (url.includes("/api/auth/status")) return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ required: false, authenticated: true }) });
    if (url.includes("/api/config")) return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ music_folder: "F:/Music", first_run_done: true }) });
    return undefined;
  });
  await plainPage.goto(`http://127.0.0.1:${port}/settings?tab=security`);
  await plainPage.waitForSelector(".panel", { timeout: 20000 });
  await plainPage.waitForTimeout(600);
  check("a browser client is offered no installer",
        (await plainPage.$$("[data-update-row]")).length === 0);
} finally {
  await browser.close();
  await vite.close();
}
console.log(failures.length ? `\nFAIL — ${checks} checked, ${failures.length} failed` : `\nPASS — ${checks} checks`);
process.exit(failures.length ? 1 : 0);