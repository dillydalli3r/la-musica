#!/usr/bin/env node
/* The STATES a page claims — the downloads page's four.
 *
 * The bug class this pins (found by a read-only UI audit): a page stating a
 * fact it never read.
 *
 * `DownloadsPage` compared the offline cache against the library — and derived
 * its "only tracks the library does not list" verdict from a library query it
 * never checked. On a cold load, and forever on a server whose /api/library
 * failed, that comparison ran against an empty library and the page told the
 * user their entire offline cache was orphaned, with a "Clear all" button
 * beside it that evicts every downloaded byte. Loading is its own state, a
 * failed fetch is its own state, and Clear all belongs to the state it clears.
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

/* The four verdicts the downloads page may show, in its own words. */
const LOADING = "Reading this browser's offline cache";
const ORPHANED = "Only tracks the library does not list";
const NOTHING = "Nothing cached yet";
const LIB_FAILED = "Could not read the library";
/* A cached track the library cannot list: written at the URL shape
 * lib/mediaCache files its bytes under, with the identity index left alone, so
 * `cachedTracks()` reads it straight off the path key. */
const GHOST = "Nope/Deleted/ghost.flac";
const CACHE = "mlo-media-v2";

(async () => {
  const browser = await chromium.launch();
  /* serviceWorkers: "block" on purpose. The states under test are the page's
   * own reads of /api/library, and once the app's service worker controls the
   * document its fetches never reach Playwright's route interception (the
   * worker answers them), so a "hung" or "failed" library could not be staged
   * at all — the check would quietly measure a healthy server instead. Blocking
   * the worker keeps every request interceptable; nothing here depends on
   * offline playback, and Cache Storage (what the downloads page reads) is
   * available to the page either way. */
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    serviceWorkers: "block",
  });
  const page = await context.newPage();
  const errs = [];
  page.on("pageerror", (e) => errs.push(e.message));

  const bodyText = () => page.evaluate(() => document.body.innerText);
  const clearAll = () => page.getByRole("button", { name: "Clear all" });
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

  /* ---- /downloads: four states, and the accusation only in the real one ---
   * The cache is seeded with ONE track no library row can claim, so the orphan
   * verdict has something to be about: the page must withhold it while the
   * library is being read and when that read failed, and offer Clear all only
   * when the comparison it stands on is real. */
  await goto("/downloads");
  await page.evaluate(async ({ base, path }) => {
    const c = await caches.open("mlo-media-v2");
    await c.put(
      `${base}/api/stream?path=${encodeURIComponent(path)}`,
      new Response(new Uint8Array(4096), {
        headers: { "Content-Type": "audio/flac", "Content-Length": "4096" },
      })
    );
  }, { base: BASE, path: GHOST });

  // (1) LOADING — the library has not answered yet.
  let releaseLibrary = () => {};
  const held = new Promise((r) => { releaseLibrary = r; });
  await page.route("**/api/library**", async (route) => {
    await held;
    await route.continue().catch(() => {});
  });
  await goto("/downloads");
  await page.getByText(LOADING).waitFor({ timeout: 15000 }).catch(() => {});
  const loadingText = await bodyText();
  check("a cold load shows a loading state, not a verdict",
    loadingText.includes(LOADING), loadingText.slice(0, 160));
  check("a cold load does not call the cache orphaned",
    !loadingText.includes(ORPHANED) && !loadingText.includes(NOTHING));
  check("a cold load offers no Clear all (the cache is not known yet)",
    (await clearAll().count()) === 0);
  releaseLibrary();
  await page.unroute("**/api/library**");

  // (2) LIBRARY FAILED — the read the verdict depends on did not happen.
  await page.route("**/api/library**", (route) =>
    route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"library is down"}' }));
  await goto("/downloads");
  await page.getByText(LIB_FAILED).waitFor({ timeout: 15000 }).catch(() => {});
  const failedText = await bodyText();
  check("a failed library fetch says it failed",
    failedText.includes(LIB_FAILED), failedText.slice(0, 200));
  check("a failed library fetch does not accuse the cache",
    !failedText.includes(ORPHANED) && !failedText.includes("match no track in the library"));
  check("a failed library fetch offers no Clear all",
    (await clearAll().count()) === 0);
  await page.unroute("**/api/library**");

  // (3) GENUINELY ORPHANED — both payloads in, the comparison real.
  await goto("/downloads");
  await page.getByText(ORPHANED).waitFor({ timeout: 15000 }).catch(() => {});
  const orphanText = await bodyText();
  check("an orphaned cache is called orphaned once the comparison is real",
    orphanText.includes(ORPHANED), orphanText.slice(0, 200));
  check("the orphaned state says how many rows it is about",
    /1 cached track\(s\) match no track in the library/.test(orphanText));
  check("the orphaned state offers Clear all (it is the state it clears)",
    (await clearAll().count()) === 1);
  // The offer has to be real, not decoration: armed and confirmed, it empties
  // the cache — which drops the page into its empty state.
  await clearAll().click();
  await clearAll().click();
  await page.getByText(NOTHING).waitFor({ timeout: 15000 }).catch(() => {});
  check("the offered Clear all really empties the cache",
    (await bodyText()).includes(NOTHING));

  // (4) EMPTY — the honest empty state, with nothing to clear.
  check("an empty cache says so and offers no Clear all",
    (await bodyText()).includes(NOTHING) && (await clearAll().count()) === 0);
  await page.evaluate(async () => { await caches.delete("mlo-media-v2"); });

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
    const favText = await bodyText();
    check("a failed favorites fetch says it failed",
      favText.includes("Could not load your favorite playlists"), favText.slice(0, 200));
    check("a failed favorites fetch does not read as 'you have none'",
      !favText.includes("No favorite playlists yet"));
    await page.unroute("**/api/favorites");
  }

  /* ---- Refresh means the WHOLE library, not just the page's own payload ----
   * Both Refresh buttons re-walk the music folder SERVER-side (`?refresh=1`
   * drops the library, home and recommendation caches), but the grading strip
   * reads its own summary (`GET /api/grades/summary`, `useGradesSummary`), so
   * a press that only refetched the page's payload left the strip quoting the
   * counts from before the walk for its whole 5-minute staleTime — reported as
   * "even after pressing Refresh this warning doesn't get updated". The claim
   * is a request claim, and that is how it is measured: after the press, the
   * summary is asked again, on both pages that carry the strip. */
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
    // produces one at all.
    check(`${route}: the same press re-asks the grading summary`, asked.summary >= 1,
      `summary requests=${asked.summary}`);
  }

  check("no uncaught page errors", errs.length === 0, errs.join(" | "));
  await browser.close();
  console.log(`\n${fail ? "FAIL" : "PASS"} — ${fail} problem(s)`);
  process.exit(fail ? 1 : 0);
})();
