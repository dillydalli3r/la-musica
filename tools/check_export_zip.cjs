#!/usr/bin/env node
/* The finished export's DOWNLOAD: the archive the browser actually saves.
 *
 * The bug class this pins (owner-reported): a zip export finished, the toast
 * named the archive and its size, and what landed in the Downloads folder was
 * a 2.6 KB "invalid .zip". It was this app's own index.html (2,689 bytes),
 * saved under the archive's name — because a download IS a navigation: an
 * `<a download href="/api/export/zip/<id>">` reaches the service worker with
 * `mode: "navigate"`, so the worker's navigation branch fetched the archive,
 * tried to store it as the SHELL (a 300 MB body against the cache's quota),
 * and when that store threw, its `catch` answered the download with the cached
 * document. Two promises broke at once: the user's export, and the offline
 * shell (which would have been the archive on an install where the store
 * succeeded).
 *
 * Everything here is the real path end to end: a real export built by the
 * server, a real `<a download>` click, and the bytes the browser wrote to disk.
 * The shell promise is checked the only way it can be — go offline and open the
 * app.
 *
 * Needs a live backend serving the built app (`web/dist`) and a library with a
 * few tracks in it:
 *   npm --prefix web run build
 *   MLO_MUSIC_FOLDER=<scratch folder> \
 *     python -m uvicorn server.main:app --host 127.0.0.1 --port 8011
 *   PLAYWRIGHT=web/node_modules/playwright \
 *     node tools/check_export_zip.cjs http://127.0.0.1:8011
 *
 * Never point it at port 8000 (the owner's own instance — see AGENTS.md).
 * Playwright is required (same resolution as tools/check_menus.cjs): set
 * PLAYWRIGHT to a module path, or install it. Exit 2 when it is missing. */
let chromium;
try {
  ({ chromium } = require(process.env.PLAYWRIGHT || "playwright"));
} catch {
  console.error("[export-zip] Playwright not found — install it with " +
    "`npm i -D playwright` (or set PLAYWRIGHT=/path/to/playwright).");
  process.exit(2);
}

const fs = require("fs");
const os = require("os");
const path = require("path");

const BASE = process.argv.slice(2).find((a) => !a.startsWith("--")) ||
  process.env.BASE || "http://127.0.0.1:8011";

/* Enough tracks for a real archive, few enough that staging is instant. An
 * export of the whole library would be the honest stress case, but the bug is
 * about the RESPONSE, not the size — and a check that copies a library is a
 * check nobody runs. */
const TRACKS = 8;

let fail = 0;
const check = (label, ok, detail = "") => {
  if (!ok) fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"} ${label}${ok || !detail ? "" : ` — ${detail}`}`);
};

(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, acceptDownloads: true });
  const page = await context.newPage();
  const errs = [];
  page.on("pageerror", (e) => errs.push(e.message));

  await page.goto(`${BASE}/`, { waitUntil: "networkidle" });
  // Start from a CLEAN worker and empty caches. A reused browser profile (the
  // harness's shared Chromium, a developer's own) keeps the registration from
  // the last run, so without this the check measures whatever worker that
  // profile installed earlier — which is how a reverted worker still "passed".
  await page.evaluate(async () => {
    for (const reg of await navigator.serviceWorker.getRegistrations()) await reg.unregister();
    for (const name of await caches.keys()) await caches.delete(name);
  });
  await page.reload({ waitUntil: "networkidle" });
  // A worker claims the page after it activates, which can be one load late —
  // reload so the download below genuinely travels through it. Without
  // control this check would pass on a plane with no service worker at all.
  await page.evaluate(() => navigator.serviceWorker.ready.then(() => true));
  await page.reload({ waitUntil: "networkidle" });
  const controlled = await page.evaluate(() => !!navigator.serviceWorker.controller);
  check("the page is controlled by the service worker", controlled,
    "no controller — the download would bypass the worker and prove nothing");
  if (!controlled) {
    await browser.close();
    console.log(`\nFAIL — ${fail} problem(s)`);
    process.exit(1);
  }

  // A real export, asked for by the page itself (so the session the app holds
  // is the one that runs it).
  const built = await page.evaluate(async (limit) => {
    const lib = await (await fetch("/api/library?refresh=1")).json();
    const paths = [];
    for (const artist of lib.artists || [])
      for (const album of artist.albums || [])
        for (const t of album.tracks || []) paths.push(t.path);
    if (!paths.length) return { tracks: 0 };
    const r = await fetch("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths: paths.slice(0, limit), target: "zip", codec: "copy" }),
    });
    return { tracks: paths.length, status: r.status, body: await r.json() };
  }, TRACKS);

  if (!built.tracks) {
    await browser.close();
    console.log("  --   no tracks in this library — nothing to export (reported, not passed)");
    console.log("\nPASS — 0 problem(s)");
    process.exit(0);
  }
  const zip = built.body.zip || {};
  check("the export returned an archive", !!zip.url && zip.bytes > 0,
    JSON.stringify(built.body).slice(0, 200));
  check("the archive holds the tracks it exported", zip.files === Math.min(built.tracks, TRACKS),
    `files=${zip.files} of ${Math.min(built.tracks, TRACKS)}`);
  console.log(`  ..   ${zip.name} · ${zip.files} file(s) · ${zip.bytes} bytes`);

  // The owner's exact gesture: the dialog's own `<a download>` click.
  const saved = path.join(os.tmpdir(), `mlo-export-check-${Date.now()}.zip`);
  const [download] = await Promise.all([
    page.waitForEvent("download", { timeout: 60000 }),
    page.evaluate((url) => {
      const a = document.createElement("a");
      a.href = url;
      a.download = "mlo-export-check.zip";
      document.body.appendChild(a);
      a.click();
      a.remove();
    }, zip.url),
  ]);
  await download.saveAs(saved);
  const buf = fs.readFileSync(saved);
  const magic = buf.subarray(0, 4).toString("latin1");
  fs.unlinkSync(saved);

  check("the saved file is a ZIP, not the app shell",
    magic === "PK\u0003\u0004", `magic=${JSON.stringify(magic)} size=${buf.length}`);
  check("the saved file is the whole archive", buf.length === zip.bytes,
    `${buf.length} bytes saved, ${zip.bytes} promised`);
  check("the saved file is not an HTML document", !/^\s*<!doctype|^\s*<html/i.test(buf.subarray(0, 80).toString("latin1")),
    buf.subarray(0, 60).toString("latin1"));

  // The branch itself, exercised the way the browser really reaches it: a
  // NAVIGATION to the archive URL — a download navigation differs only in
  // `destination`, which the old code never looked at, and Playwright's own
  // download plumbing can bypass the worker entirely. What is asserted is the
  // damage the branch did, measured where it landed: the shell cache entry went
  // from the document (2.7 KB of HTML) to the archive ("PK", 23 KB), so an
  // offline start opened a zip.
  const probe = await context.newPage();
  await probe.goto(`${BASE}${zip.url}`, { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => null);
  await probe.close().catch(() => {});

  const shells = await page.evaluate(async () => {
    const out = [];
    for (const name of await caches.keys()) {
      const cache = await caches.open(name);
      for (const req of await cache.keys()) {
        const path = new URL(req.url).pathname;
        if (path !== "/" && !/\.html?$/.test(path)) continue;
        const hit = await cache.match(req);
        if (!hit) continue;
        const head = (await hit.clone().text()).slice(0, 40);
        out.push({ cache: name, path, starts: head.slice(0, 15).replace(/\s+/g, " "), archive: head.startsWith("PK") });
      }
    }
    return out;
  });
  check("no cache holds an archive as a document",
    shells.every((s) => !s.archive), JSON.stringify(shells.filter((s) => s.archive)));

  // The other half: the shell cache must still hold the APP, which offline is
  // the only way to ask — a poisoned install opens its "shell" as an archive.
  await context.setOffline(true);
  const offline = await page.goto(`${BASE}/library`, { waitUntil: "domcontentloaded" }).catch(() => null);
  const shellOk = !!offline && (await page.evaluate(() =>
    !!document.querySelector("#root, aside, main") && document.body.innerText.trim().length > 0
  ).catch(() => false));
  check("with the server unreachable the app still opens (the shell is a document)", shellOk,
    `status=${offline ? offline.status() : "no response"}`);
  await context.setOffline(false);

  check("no uncaught page errors", errs.length === 0, errs.join(" | "));
  await browser.close();
  console.log(`\n${fail ? "FAIL" : "PASS"} — ${fail} problem(s)`);
  process.exit(fail ? 1 : 0);
})();
