#!/usr/bin/env node
/* Browse's track sheet: whose names it prints, whose rating it shows, and which
 * order it asks for. Measured against a RUNNING app.
 *
 * Run:  node tools/check_browse_sheet.cjs [baseUrl]
 *
 * What it pins (the failures this file exists for):
 *
 *   * the Artist and Album columns print the LIBRARY's names, not the folder
 *     basenames the engine stamps on each row — the sheet used to show
 *     `Radiohead [a74b1b7f-…]` and `[Album] 1994-11-29 … {GB - CD …} [<mbid>]`
 *     where every other page shows `Radiohead` and `The Bends` (R106/R227);
 *   * the Rating column shows the app's OWN rating (0-5, the store behind
 *     `GET /api/ratings`), not the file's Picard `RATING` tag, which is 0-100 —
 *     a five-star track printed "100" and an app-only rating printed "—"
 *     (R227). One track is rated through `PUT /api/ratings` to prove it;
 *   * no request asks the engine to sort by `library.path`: while
 *     `/api/library/fields` was still in flight the sheet asked for a file-path
 *     order and painted an "Artist" header over it. The check reads the POST
 *     bodies and requires the last one's key to be the one the toolbar shows.
 *
 * Point it at a scratch library (`MLO_MUSIC_FOLDER`) — it writes a rating. The
 * servers under test are the ones the other checks use: 8011 and up, never the
 * owner's 8000.
 */
let chromium;
try {
  // Plain require resolves from this file's folder up to the repo root's
  // node_modules; PLAYWRIGHT overrides it (e.g. a global install).
  ({ chromium } = require(process.env.PLAYWRIGHT || "playwright"));
} catch (e) {
  console.error("[check_browse_sheet] Playwright not found — install it with " +
    "`npm i -D playwright` (or set PLAYWRIGHT=/path/to/playwright).");
  process.exit(1);
}

const ARGS = process.argv.slice(2);
const BASE = ARGS.find((a) => !a.startsWith("--")) || process.env.BASE || "http://127.0.0.1:8011";
const OUT = process.env.OUT || ".pi/shots-browse-sheet";
/** The rating written for the fixture track, in the API's own 0-10 units. */
const RATING_API = 8;
/** ...and the text the sheet has to show for it (UI 0-5, halves). */
const RATING_TEXT = "4";

let failures = 0;
const check = (name, ok, detail) => {
  console.log(`  ${ok ? "ok  " : "FAIL"} ${name}${detail ? ` — ${detail}` : ""}`);
  if (!ok) failures++;
};

(async () => {
  const lib = await (await fetch(`${BASE}/api/library`)).json();
  const flat = [];
  for (const a of lib.artists || []) {
    for (const al of a.albums || []) {
      for (const t of al.tracks || []) {
        flat.push({
          track: t,
          // what the sheet must print, taken from the SAME payload it reads
          artist: al.album_artist || a.display_name || a.name,
          album: al.meta?.ALBUM || "",
          folderArtist: a.name,
        });
      }
    }
  }
  if (!flat.length) {
    console.error("[check_browse_sheet] the library has no tracks — point it at a scratch library with audio");
    process.exit(1);
  }
  const pick = flat[0];
  await fetch(`${BASE}/api/ratings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: pick.track.path, rating: RATING_API, scope: "track" }),
  });

  const browser = await chromium.launch({ headless: process.env.HEADED ? false : true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => console.log("  PAGE_ERR:", e.message));
  const bodies = [];
  page.on("request", (r) => {
    if (r.url().includes("/api/library/query")) bodies.push(String(r.postData() || ""));
  });
  await page.goto(BASE + "/browse", { waitUntil: "networkidle", timeout: 60000 });
  await page.waitForTimeout(2000);
  if (process.env.OUT) await page.screenshot({ path: `${OUT}/browse-sheet.png` });

  const rows = await page.evaluate(() =>
    [...document.querySelectorAll("tbody tr.table-row")].slice(0, 3).map((tr) => {
      const tds = [...tr.querySelectorAll("td")];
      const text = (i) => (tds[i]?.textContent || "").trim();
      return { title: text(2), artist: text(3), album: text(4), rating: text(9) };
    })
  );
  check("the track sheet draws rows", rows.length > 0, `${rows.length} row(s)`);

  const first = rows[0] || { artist: "", album: "", rating: "", title: "" };
  check("Artist is the library's name, not the folder's",
    first.artist === pick.artist,
    `shows "${first.artist}", payload says "${pick.artist}"` +
      (pick.folderArtist !== pick.artist ? ` (folder is "${pick.folderArtist}")` : ""));
  if (pick.album) {
    check("Album is the release's own title",
      first.album === pick.album, `shows "${first.album}", payload says "${pick.album}"`);
  } else {
    console.log("  --  Album: the payload states no ALBUM tag for the first row, skipped");
  }
  check("Rating is the app's own rating (0-5), not the 0-100 file tag",
    first.rating === RATING_TEXT, `shows "${first.rating}", expected "${RATING_TEXT}"`);

  const sortKeys = bodies.map((b) => { try { return JSON.parse(b).sort?.key; } catch { return null; } });
  check("no request sorts by the raw file path",
    !sortKeys.includes("library.path"), sortKeys.join(" -> "));
  const shown = await page.$eval('select[aria-label="Sort field"]', (s) => s.value);
  check("the last request asks for the sort the toolbar shows",
    sortKeys[sortKeys.length - 1] === shown,
    `asked ${JSON.stringify(sortKeys[sortKeys.length - 1])}, toolbar shows "${shown}"`);

  await browser.close();
  console.log(`\n${failures ? `${failures} problem(s)` : "PASS — the browse sheet reads the library"}`);
  process.exit(failures ? 1 : 0);
})();
