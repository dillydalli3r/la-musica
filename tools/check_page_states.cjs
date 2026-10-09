#!/usr/bin/env node
/* The STATES a page claims — the Refresh buttons' whole-library walk.
 *
 * The bug class this pins (found by a read-only UI audit): a page stating a
 * fact it never read.
 *
 * The Refresh press must re-walk the SERVER (not only the page's own payload)
 * and re-ask the grading summary the strip quotes — a press that refetched
 * only the page payload left the strip quoting stale counts for its whole
 * 5-minute staleTime, reported as "even after pressing Refresh this warning
 * doesn't get updated". (This check used to also pin the Optimization page's
 * separate Library-layout panel's Apply-fixes gate; that panel is gone — the
 * same job is script 20, Optimize library layout — so only the Refresh half
 * remains.)
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

  /* ---- Refresh means the WHOLE library, not just the page's own payload ----
   * Both routes render the library page, and its Refresh button re-walks the
   * music folder SERVER-side (`?refresh=1` drops the server's caches), but the
   * grading strip reads its own summary (`GET /api/grades/summary`,
   * `useGradesSummary`), so a press that only refetched the page's payload left
   * the strip quoting the counts from before the walk for its whole 5-minute
   * staleTime. The claim is a request claim, and that is how it is measured:
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