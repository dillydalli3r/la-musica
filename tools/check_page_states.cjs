#!/usr/bin/env node
/* The STATES a page claims — the one-click whole-library apply gate on
 * /optimize, and the Refresh buttons' whole-library walk.
 *
 * The bug class this pins (found by a read-only UI audit): a page stating a
 * fact it never read.
 *
 * /optimize's "Apply fixes" renamed and moved files across the WHOLE library
 * from one unconfirmed click, and its Run All reported a partial failure as
 * "see console" — a place the user cannot open. The apply now asks first and
 * names what it will touch; the run reports its own results on the page. This
 * check pins the gate: cancelling performs no request, and confirming performs
 * exactly one. It also pins that a Refresh press re-walks the SERVER (not only
 * the page's own payload) and re-asks the grading summary the strip quotes.
 *
 * Needs a live backend serving the built app (`web/dist`) and a library with a
 * few tracks in it:
 *   npm --prefix web run build
 *   MLO_MUSIC_FOLDER=<scratch folder> \
 *     python -m uvicorn server.main:app --host 127.0.0.1 --port 8011
 *   PLAYWRIGHT=web/node_modules/playwright \
 *     node tools/check_page_states.cjs http://127.0.0.1:8011
 *
 * Never point it at port 8000 (the owner's own instance — see AGENTS.md).
 * Playwright is required (same resolution as tools/check_responsive.cjs): set
 * PLAYWRIGHT to a module path, or install it. Exit 2 when it is missing. */

let chromium;
try {
  ({ chromium } = require(process.env.PLAYWRIGHT || "playwright"));
} catch {
  console.error("[page-states] Playwright not found — `npm i -D playwright`, " +
    "or point PLAYWRIGHT at an installed module.");
  process.exit(2);
}

const BASE = process.argv.slice(2).find((a) => !a.startsWith("--")) ||
  process.env.BASE || "http://127.0.0.1:8011";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let fail = 0;
const check = (label, ok, detail = "") => {
  if (!ok) fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"} ${label}${ok || !detail ? "" : ` — ${detail}`}`);
};

/* What the canned apply response carries — a report with nothing left wrong,
 * so a check can prove the request was made without renaming the library it is
 * measuring. */
const CANNED_REPORT = {
  folder: "", artists_dir: "", exists: true, issues: [], counts: {}, total: 0,
  albums: 0, artists: 0, audio_files: 0, fixes: [], fixed: 0, fix_failed: 0, skipped: 0,
};

(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
  });
  const page = await context.newPage();
  const errs = [];
  page.on("pageerror", (e) => errs.push(e.message));

  /* A navigation that lands while the app's service worker is taking control is
   * aborted by the browser and restarted (net::ERR_ABORTED reaches Playwright,
   * not the user). Every load below goes through this, so the check measures
   * the page instead of that race. */
  const goto = async (route) => {
    for (let attempt = 0; ; attempt++) {
      try {
        await page.goto(`${BASE}${route}`, { waitUntil: "domcontentloaded", timeout: 20000 });
        return;
      } catch (e) {
        if (attempt >= 3 || !/ERR_ABORTED/.test(String(e))) throw e;
        await sleep(500);
      }
    }
  };

  /* ---- /optimize: Apply fixes asks first ---------------------------------
   * One click used to rename and move files across the whole library. The gate
   * is asserted the only way a gate can be: what it does NOT do when it is
   * dismissed, and that it does exactly one request when it is confirmed. */
  let applies = 0;
  page.on("request", (r) => {
    if (r.method() === "POST" && r.url().includes("/api/library/layout/apply")) applies++;
  });
  await goto("/optimize");
  await page.getByRole("button", { name: /^(Scan library layout|Rescan)$/ }).click();
  const applyTrigger = page.getByRole("button", { name: "Apply fixes" });
  await applyTrigger.waitFor({ timeout: 120000 }).catch(() => {});
  check("a scanned library offers Apply fixes", (await applyTrigger.count()) === 1);

  if (await applyTrigger.count()) {
    const dialog = page.locator('[role="dialog"][aria-modal="true"]');
    await applyTrigger.click();
    await dialog.waitFor({ timeout: 10000 }).catch(() => {});
    check("Apply fixes opens a confirmation", (await dialog.count()) === 1);
    const words = (await dialog.count()) ? await dialog.innerText() : "";
    check("the confirmation says what it will do",
      /whole music folder/i.test(words) && /rename/i.test(words) && /move/i.test(words) && /trash/i.test(words),
      words.slice(0, 200));
    check("the confirmation carries the report's own scope",
      /\d|no row/i.test(words) && /row/i.test(words), words.slice(0, 200));
    check("nothing has been applied yet, only confirmed",
      applies === 0, `apply requests: ${applies}`);

    // Dismissed: Escape, then the Cancel button. Neither may reach the
    // endpoint — the gate's whole point is that a change this large is asked
    // for, not assumed.
    await page.keyboard.press("Escape");
    await sleep(300);
    check("Escape closes the confirmation", (await dialog.count()) === 0);
    check("Escape applies nothing", applies === 0, `apply requests: ${applies}`);

    await applyTrigger.click();
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await sleep(300);
    check("Cancel closes the confirmation", (await dialog.count()) === 0);
    check("Cancel applies nothing", applies === 0, `apply requests: ${applies}`);

    // Confirmed: exactly one request, answered here so the check never touches
    // the library it measures.
    await page.route("**/api/library/layout/apply", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(CANNED_REPORT) }));
    await applyTrigger.click();
    await dialog.getByRole("button", { name: /Rename, move/ }).click();
    await sleep(800);
    check("confirming performs the apply", applies === 1, `apply requests: ${applies}`);
    check("the confirmation closes on confirm", (await dialog.count()) === 0);
    await page.unroute("**/api/library/layout/apply");
  }

  /* ---- Refresh means the WHOLE library, not just the page's own payload ----
   * Both routes render the library page, and its Refresh button re-walks the
   * music folder SERVER-side (`?refresh=1` drops the server's caches), but the
   * grading strip reads its own summary (`GET /api/grades/summary`,
   * `useGradesSummary`), so a press that only refetched the page's payload left
   * the strip quoting the counts from before the walk for its whole 5-minute
   * staleTime — reported as "even after pressing Refresh this warning doesn't
   * get updated". The claim is a request claim, and that is how it is measured:
   * after the press, the summary is asked again, on both routes that carry the
   * strip. */
  for (const [route, payload, button] of [["/library", "/api/library", /^Refresh$/], ["/", "/api/library", /^Refresh$/]]) {
    await goto(route);
    await sleep(1200);                       // the page's own first reads settle
    const asked = { payload: 0, summary: 0 };
    const watch = (r) => {
      const u = r.url();
      if (u.includes(payload) && u.includes("refresh=1")) asked.payload++;
      if (u.includes("/api/grades/summary")) asked.summary++;
    };
    page.on("request", watch);
    const trigger = page.getByRole("button", { name: button }).first();
    const present = (await trigger.count()) > 0;
    if (present) await trigger.click();
    await sleep(2500);
    page.off("request", watch);
    check(`${route}: the Refresh button re-walks the server (${payload}?refresh=1)`,
      present && asked.payload >= 1, `present=${present} refresh requests=${asked.payload}`);
    // The strip's own read. A poll or a mount inside the window cannot fake
    // this: those are the same request, and the point is that the press
    // produces one at all.
    check(`${route}: the same press re-asks the grading summary`, asked.summary >= 1,
      `summary requests=${asked.summary}`);
  }

  check("no uncaught page errors", errs.length === 0, errs.join(" | "));
  await browser.close();
  console.log(`\n${fail ? "FAIL" : "PASS"} — ${fail} problem(s)`);
  process.exit(fail ? 1 : 0);
})();
