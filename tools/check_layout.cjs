#!/usr/bin/env node
/* The library-layout scan is script 20 (Scan library layout), not a panel.
 *
 * The Optimization page's own Scan/Apply "Library layout" card is gone: script
 * 20 in the script list is the one path, and the read-only `GET
 * /api/library/layout` route it shares with the Library page's warning is the
 * surviving scan surface. That route SCANS and stores its report, and the
 * Library page reads that stored copy for its own warning — so a scan and the
 * warning can never disagree.
 *
 * What is measured, against a running app:
 *
 *   * the Optimization script list carries script 20, and the standalone
 *     Library-layout card (Rescan / Apply fixes) is gone;
 *   * `GET /api/library/layout` scans AND stores: the stored report's
 *     `scanned_at` moves with nobody pressing anything;
 *   * when the music folder changed behind the app's back (an empty artist
 *     folder deleted on disk, the finding the owner actually hit), the stored
 *     report still lists the row until the next scan, and a fresh scan clears
 *     it — the Library page's warning reads that copy, so a report left stale
 *     would leave the warning lying;
 *   * the two POST routes the removed panel alone used are gone.
 *
 * The stale-report half needs a music folder with an album-less artist folder
 * to delete; a library without one runs the rest and says so.
 *
 * Needs a live backend serving the built app (`web/dist`), pointed at a SCRATCH
 * music folder — the stale half DELETES a folder from it:
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
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const BASE = process.argv[2] || process.env.BASE || "http://127.0.0.1:8011";
const results = [];
const check = (name, pass, detail) => results.push({ name, pass: !!pass, detail: String(detail ?? "") });

const report = async () => (await (await fetch(`${BASE}/api/library/layout/report`)).json());
const scan = async () => {
  const res = await fetch(`${BASE}/api/library/layout`);
  if (!res.ok) throw new Error(`GET /api/library/layout → ${res.status}`);
  return res.json();
};

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.on("pageerror", (e) => check("no uncaught page error", false, e.message));

  // ---- 1. script 20 is the only path; the panel is gone --------------------
  await page.goto(`${BASE}/optimize`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("text=Individual scripts", { timeout: 30000 });
  const scriptListed = await page.locator('text=Scan library layout').count();
  check("the Optimization page lists script 20 (Scan library layout)", scriptListed > 0,
    `matches=${scriptListed}`);
  const rescan = await page.locator('button:has-text("Rescan")').count();
  const card = await page.getByText("Library layout", { exact: true }).count();
  check("the standalone Library-layout card (Rescan / Apply fixes) is gone",
    rescan === 0 && card === 0, `Rescan buttons=${rescan} card heading=${card}`);

  // ---- 2. the scan route scans AND stores ----------------------------------
  const first = await scan();
  const stamp1 = (await report()).scanned_at;
  await sleep(500);
  const second = await scan();
  const stored = await report();
  check("GET /api/library/layout returns the scan and STORES its report",
    first.total !== undefined && stored.exists === true &&
    !!stamp1 && !!stored.scanned_at && stored.scanned_at !== stamp1,
    `total=${first.total} scanned_at ${stamp1} → ${stored.scanned_at}`);

  // ---- 3. a folder that changed behind the app's back ----------------------
  // The owner's case: the row is about a folder that is no longer there, and
  // the stored report (which the Library's warning reads) still lists it.
  // The row to delete is the SCAN's own: the report names it by its absolute
  // path (`abs`), so the check never has to parse it back out of any prose —
  // and a folder name with a space in it cannot confuse it.
  const issue = (second.issues ?? []).find((i) => i.kind === "empty_artist" && i.abs);
  const empty = issue ? String(issue.abs) : "";
  if (!empty || !fs.existsSync(empty)) {
    console.log("[layout] note: no album-less artist folder to delete — the "
      + "stale-report half was not applicable");
  } else {
    const before = await report();
    fs.rmSync(empty, { recursive: true, force: true });
    await sleep(1200);
    const mid = await report();
    check("the stored report still lists the row (the stale state)",
      (mid.report?.total ?? 0) === (before.report?.total ?? 0) && (mid.report?.total ?? 0) > 0,
      `stored total=${mid.report?.total}`);

    // With the problem still stored, the Library page's own warning is on
    // screen — and it must point at script 20, never at a panel that no
    // longer exists.
    await page.goto(`${BASE}/library`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(600);
    const pointsAtScript = await page.locator('a[title^="Run 20"]').count();
    const pointsAtPanel = await page.locator('[title*="library-layout panel"]').count();
    check("the Library layout warning points at script 20, not a removed panel",
      pointsAtScript > 0 && pointsAtPanel === 0,
      `script-20 links=${pointsAtScript} panel links=${pointsAtPanel}`);

    const fresh = await scan();
    const after = await report();
    const listed = (after.report?.issues ?? []).some((i) => String(i.abs || "") === empty);
    check("a fresh scan clears the deleted folder from the report",
      (fresh.total ?? 0) === (before.report?.total ?? 0) - 1 &&
      (after.report?.total ?? 0) === (fresh.total ?? 0) && !listed,
      `stored total ${before.report?.total} → ${after.report?.total} (listed=${listed})`);
  }

  // ---- 4. the panel-only POST routes are gone ------------------------------
  const gone = [];
  for (const p of ["/api/library/layout/apply", "/api/library/layout/remove-empty-artist"]) {
    const res = await fetch(`${BASE}${p}`, { method: "POST" });
    gone.push(`${p}=${res.status}`);
    check(`POST ${p} is gone`, res.status === 404 || res.status === 405, `status=${res.status}`);
  }

  await browser.close();
  for (const r of results) console.log(`${r.pass ? "ok  " : "FAIL"} ${r.name}${r.detail ? ` :: ${r.detail}` : ""}`);
  const bad = results.filter((r) => !r.pass).length;
  console.log(`${results.length - bad}/${results.length} checks pass :: ${gone.join(" ")}`);
  process.exit(bad ? 1 : 0);
})();