#!/usr/bin/env node
/* The STATES a page claims, and what Refresh actually re-reads.
 *
 * The bug class this pins: a page stating a fact it never read. The favorites
 * page, for one, must say when its fetch failed instead of drawing the failed
 * read as "you have none".
 *
 * It also pins Refresh as a WHOLE-library action: the buttons re-walk the
 * music folder server-side (`?refresh=1`), and the Library's grading strip
 * reads its own summary (`GET /api/grades/summary`), so a press that only
 * refetched the page's payload would leave the strip quoting stale counts.
 * Home is checked for its own payload refresh; the grading-summary assertion
 * belongs to /library alone, because Home no longer draws that strip.
 *
 * The /optimize "Apply fixes" gate this file used to pin is gone with the
 * standalone Library-layout panel: layout fixes are script 20 now, and its
 * whole-library pass is the script's own run rather than an unconfirmed click.
 * That surface is pinned by tools/check_layout.cjs.
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
  /* serviceWorkers: "block" on purpose. The states under test are the page's
   * own reads of /api/library and /api/favorites, and once the app's service
   * worker controls the document its fetches never reach Playwright's route
   * interception (the worker answers them), so a "hung" or "failed" fetch
   * could not be staged at all — the check would quietly measure a healthy
   * server instead. Blocking the worker keeps every request interceptable. */
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    serviceWorkers: "block",
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

  /* ---- /favorites: the row goes to the playlist, and a failed fetch is not
   *      an empty list ----------------------------------------------------- */
  const playlistList = await (await fetch(`${BASE}/api/playlists`)).json();
  let firstPlaylist = (Array.isArray(playlistList) ? playlistList : playlistList.playlists ?? [])[0];
  if (!firstPlaylist) {
    // A scratch server may have none: make one, so the link assertion below
    // always runs instead of quietly reporting itself out.
    firstPlaylist = await (await fetch(`${BASE}/api/playlists`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "Check playlist" }),
    })).json();
  }
  if (!firstPlaylist?.id) {
    console.log("  --   no playlist on this server and none could be made — the favorites checks are reported, not passed");
  } else {
    const favs = await (await fetch(`${BASE}/api/favorites`)).json();
    if (!(favs.playlists ?? []).map(String).includes(String(firstPlaylist.id))) {
      await fetch(`${BASE}/api/favorites/toggle`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: "playlist", key: String(firstPlaylist.id) }),
      });
    }
    await goto("/favorites/playlists");
    await page.getByRole("link", { name: firstPlaylist.name }).waitFor({ timeout: 15000 }).catch(() => {});
    const href = await page.getByRole("link", { name: firstPlaylist.name }).getAttribute("href").catch(() => null);
    check("a favorite playlist's name links to the playlist itself",
      href === `/playlist/${firstPlaylist.id}`, `href=${href}, expected /playlist/${firstPlaylist.id}`);

    await page.route("**/api/favorites", (route) =>
      route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"favorites are down"}' }));
    await goto("/favorites/playlists");
    await page.getByText("Could not load your favorite playlists").waitFor({ timeout: 15000 }).catch(() => {});
    const favText = await page.evaluate(() => document.body.innerText);
    check("a failed favorites fetch says it failed",
      favText.includes("Could not load your favorite playlists"), favText.slice(0, 200));
    check("a failed favorites fetch does not read as 'you have none'",
      !favText.includes("No favorite playlists yet"));
    await page.unroute("**/api/favorites");
  }

  /* ---- Refresh means the WHOLE library, not just the page's own payload ----
   * Both Refresh buttons re-walk the music folder SERVER-side (`?refresh=1`
   * drops the library and home caches), so a press that only refetched the
   * page's payload would leave everything built from that walk stale. The
   * Library additionally draws the grading strip, which reads its own summary
   * (`GET /api/grades/summary`, `useGradesSummary`): there, the same press
   * must also re-ask that summary, or the strip keeps quoting the counts from
   * before the walk for its whole 5-minute staleTime. Home no longer renders
   * the strip, so only its payload refresh is asserted. The claims are request
   * claims, and that is how they are measured. */
  for (const [route, payload, button] of [["/library", "/api/library", /^Refresh$/], ["/", "/api/home", /^Refresh$/]]) {
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
    // produces one at all. Only the Library carries the strip.
    if (route === "/library") {
      check(`${route}: the same press re-asks the grading summary`, asked.summary >= 1,
        `summary requests=${asked.summary}`);
    }
  }

  check("no uncaught page errors", errs.length === 0, errs.join(" | "));
  await browser.close();
  console.log(`\n${fail ? "FAIL" : "PASS"} — ${fail} problem(s)`);
  process.exit(fail ? 1 : 0);
})();
