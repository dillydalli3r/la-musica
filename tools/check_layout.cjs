#!/usr/bin/env node
/* The Library-layout panel keeps itself up to date: opening it SCANS.
 *
 * The report it draws is a scan's output (`mlo.layout.scan_library`, the walk
 * script 20 runs), and both the panel and the Library page's own layout warning
 * read the STORED copy of it. The panel used to paint that stored answer and
 * wait for the reader to press Rescan, so a folder the app (or the reader) had
 * already changed kept being reported until they did (owner report: "I need to
 * manually use this section under rescan for the library to update. It should
 * be done automatically"). It now scans when it opens, and every action that
 * moves a folder (an album to the Trash, an artist folder to the Trash, a
 * restore, an applied fix) asks for a fresh scan too.
 *
 * What is measured, against a running app:
 *
 *   * the panel's own stamp comes from a scan of THIS visit — the stored
 *     report's `scanned_at` moves when the page is opened, with nobody pressing
 *     Rescan;
 *   * when the music folder has changed behind the app's back (an empty artist
 *     folder deleted on disk, the finding the owner actually hit), reopening
 *     the panel clears the row AND leaves the stored report matching the folder
 *     — the Library page reads that copy for its warning, so a panel that only
 *     fixed its own screen would leave the other surface lying.
 *
 * The second half needs a music folder with an album-less artist folder to
 * delete; a library without one runs the first half and says so.
 *
 * Needs a live backend serving the built app (`web/dist`), pointed at a SCRATCH
 * music folder — the second half DELETES a folder from it:
 *   npm --prefix web run build
 *   MLO_MUSIC_FOLDER=<scratch folder> python -m uvicorn server.main:app --host 127.0.0.1 --port 8011
 *   node tools/check_layout.cjs http://127.0.0.1:8011
 *
 * Exit codes: 0 pass, 1 a check failed, 2 the environment cannot run it.
 */
let chromium;
try {
  ({ chromium } = require(process.env.PLAYWRIGHT || "playwright"));
} catch {
  console.error("[layout] Playwright not found — `npm i -D playwright`, " +
    "or point PLAYWRIGHT at an installed module.");
  process.exit(2);
}
const fs = require("fs");
const path = require("path");
const { fileURLToPath } = require("url");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const BASE = process.argv[2] || process.env.BASE || "http://127.0.0.1:8011";
const results = [];
const check = (name, pass, detail) => results.push({ name, pass: !!pass, detail: String(detail ?? "") });

const report = async () => (await (await fetch(`${BASE}/api/library/layout/report`)).json());

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.on("pageerror", (e) => check("no uncaught page error", false, e.message));

  const openPanel = async () => {
    await page.goto(`${BASE}/optimize`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("text=Library layout", { timeout: 30000 });
    await page.locator('button:has-text("Rescan"), button:has-text("Scan library layout")').first()
      .waitFor({ state: "visible", timeout: 30000 }).catch(() => {});
    await page.waitForTimeout(2500);
  };

  // ---- 1. opening the panel scans ------------------------------------------
  await openPanel();
  const first = await report();
  check("the panel is there and the report is stored", first.exists === true,
    `exists=${first.exists} scanned_at=${first.scanned_at}`);
  const stamp1 = first.scanned_at;
  await page.goto(`${BASE}/library`, { waitUntil: "domcontentloaded" });
  await sleep(400);
  await openPanel();
  const second = await report();
  check("opening the panel scans by itself — the stored report's stamp moves with no Rescan press",
    !!stamp1 && !!second.scanned_at && second.scanned_at !== stamp1,
    `before=${stamp1} after=${second.scanned_at}`);

  // ---- 2. a folder that changed behind the app's back ----------------------
  // The owner's case: the row is about a folder that is no longer there, and
  // the stored report (which the Library's warning reads) still lists it.
  // The row to delete is the SCAN's own: the report names it by its absolute
  // path (`abs`), so the check never has to parse it back out of the panel's
  // prose — and a folder name with a space in it cannot confuse it.
  const issue = (second.report?.issues ?? []).find((i) => i.kind === "empty_artist" && i.abs);
  const empty = issue ? String(issue.abs) : "";
  if (!empty || !fs.existsSync(empty)) {
    console.log("[layout] note: no album-less artist folder to delete — the "
      + "stale-report half was not applicable");
  } else {
    const before = await report();
    fs.rmSync(empty, { recursive: true, force: true });
    await sleep(1200);
    const mid = await report();
    check("the report on disk still lists the row (the stale state)",
      (mid.report?.total ?? 0) === (before.report?.total ?? 0) && (mid.report?.total ?? 0) > 0,
      `stored total=${mid.report?.total}`);
    await openPanel();
    const after = await report();
    const listed = (after.report?.issues ?? []).some((i) => String(i.abs || "") === empty);
    check("reopening the panel re-scans: the deleted folder is gone from the report",
      (after.report?.total ?? 0) === (before.report?.total ?? 0) - 1 && !listed,
      `stored total ${before.report?.total} → ${after.report?.total} (listed=${listed})`);
    const onScreen = await page.evaluate(() =>
      /artist folder .*holds no album folder/i.test(document.querySelector("main")?.innerText || ""));
    check("…and the panel's own rows are what the stored report says",
      !onScreen, "the row is off the panel too");
  }

  await browser.close();
  for (const r of results) console.log(`${r.pass ? "ok  " : "FAIL"} ${r.name}${r.detail ? ` :: ${r.detail}` : ""}`);
  const bad = results.filter((r) => !r.pass).length;
  console.log(`${results.length - bad}/${results.length} checks pass`);
  process.exit(bad ? 1 : 0);
})();