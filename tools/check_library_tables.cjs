#!/usr/bin/env node
/* Library table geometry + filter facets, measured against a RUNNING app.
 *
 * Run:  node tools/check_library_tables.cjs [baseUrl]
 *
 * What it pins (the failures this file exists for):
 *
 *   * every one of the three table views draws rows — the Tracks view once
 *     rendered as empty 200 px-tall rows because its title cell was squeezed to
 *     zero by the marks sharing it (see `TrackTitleCell`);
 *   * no text-bearing link is narrower than 40 px, and no row is taller than
 *     120 px, in any view;
 *   * every header label fits its column: the layout is `table-layout: fixed`,
 *     so a nowrap label wider than its cell paints over the neighbour;
 *   * every column is at least as wide as its widest VALUE — measured from a
 *     clone of the cell, so the report says how much a floor is short;
 *   * a value longer than any sane column may be CLIPPED instead, but only in
 *     the open: the clipping element (the app's `.cell-ellipsis`, or Tailwind's
 *     `truncate` — the check reads the computed style, not one class name)
 *     carries the whole value in its `title`. A clip with no title is the
 *     failure this rule exists for: the column hides text the reader cannot
 *     get back. The clone used for the width rule sets `max-width: none`, so a
 *     cell that caps ITSELF is measured at its full value and still fails.
 *   * a cell that spans columns (`<td colSpan>`, the artist heading row) is not
 *     one column's value: it must cover exactly the columns it spans and must
 *     not overflow them. Counting it as column 0's value is what once reported
 *     a 40 px chevron column as "needs 108" — the group heading's own name;
 *   * the toolbar's facets do what they say: Rated keeps only rated rows,
 *     Unrated drops them, Explicit matches the count the payload reports, and
 *     Clean excludes them.
 *
 * It RATES ONE TRACK through `PUT /api/ratings` so the rating facet has
 * something to find: point it at a scratch library (`MLO_MUSIC_FOLDER`), never
 * at the library you listen to. The servers under test are the ones the other
 * checks use — 8011 and up, never the owner's 8000.
 */
let chromium;
try {
  // Plain require resolves from this file's folder up to the repo root's
  // node_modules; PLAYWRIGHT overrides it (e.g. a global install).
  ({ chromium } = require(process.env.PLAYWRIGHT || "playwright"));
} catch (e) {
  console.error("[check_library_tables] Playwright not found — install it with " +
    "`npm i -D playwright` (or set PLAYWRIGHT=/path/to/playwright).");
  process.exit(1);
}

const ARGS = process.argv.slice(2);
const BASE = ARGS.find((a) => !a.startsWith("--")) || process.env.BASE || "http://127.0.0.1:8011";
const OUT = process.env.OUT || ".pi/shots-library-tables";
const VIEWS = ["Albums", "Artists", "Tracks"];

let failures = 0;
const check = (name, ok, detail) => {
  console.log(`  ${ok ? "ok  " : "FAIL"} ${name}${detail ? ` — ${detail}` : ""}`);
  if (!ok) failures++;
};

(async () => {
  const lib = await (await fetch(`${BASE}/api/library`)).json();
  const tracks = (lib.artists || []).flatMap((a) => a.albums.flatMap((al) => al.tracks || []));
  if (!tracks.length) {
    console.error("[check_library_tables] the library has no tracks — point it at a scratch library with audio");
    process.exit(1);
  }
  // One rated track, so "Rated" has exactly one row to find.
  await fetch(`${BASE}/api/ratings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: tracks[0].path, rating: 8, scope: "track" }),
  });
  const explicit = tracks.filter((t) => t.tags.ITUNESADVISORY === "1").length;

  const browser = await chromium.launch({ headless: process.env.HEADED ? false : true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => console.log("  PAGE_ERR:", e.message));
  // The regression the owner hit: a prefs list written under the PREVIOUS key
  // (`mlo-cols3-*`, an older column vocabulary) that keeps only some of the ids
  // this build has. Trusting it drew an Albums view with its chevron and
  // nothing else — so the check starts from exactly that state.
  await ctx.addInitScript(() => {
    localStorage.setItem("mlo-cols3-tracks", JSON.stringify(["num", "dur"]));
    localStorage.setItem("mlo-cols3-artists", JSON.stringify([]));
    localStorage.removeItem("mlo-cols4-tracks");
    localStorage.removeItem("mlo-cols4-artists");
  });
  await page.goto(BASE + "/library", { waitUntil: "networkidle", timeout: 60000 });
  await page.waitForTimeout(1500);

  /** Geometry + content of the table on screen right now. */
  const geom = () => page.evaluate(() => {
    const table = document.querySelector("main table");
    if (!table) return { none: true };
    // The view's OWN rows: an expanded album row holds the album tracklist as a
    // nested table inside this tbody, and those cells are not columns of this
    // one (the nested table has its own header and its own floors).
    const rows = [...table.querySelectorAll(":scope > tbody > tr")];
    const squeezed = [];
    for (const tr of rows.slice(0, 8)) {
      for (const el of tr.querySelectorAll("a")) {
        const text = el.textContent.trim();
        if (!text) continue; // a cover link wraps an image, nothing to squeeze
        const w = el.getBoundingClientRect().width;
        if (w < 40) squeezed.push(`${text.slice(0, 16)}=${Math.round(w)}px`);
      }
    }
    /** A deliberate single-line clip: `overflow: hidden` with an ellipsis on
     *  nowrap text. Both of the app's idioms compute to that — the
     *  `.cell-ellipsis` class and Tailwind's `truncate` — so this reads the
     *  computed style rather than picking a class name to trust. */
    const clips = (el) => {
      const s = getComputedStyle(el);
      return (s.overflowX === "hidden" || s.overflowX === "clip")
        && s.textOverflow === "ellipsis" && s.whiteSpace === "nowrap";
    };
    /** The element that would clip this cell's value: the cell itself, or the
     *  one inside it (a name is a link, and its cell may hold marks beside). */
    const clipperOf = (td) => (clips(td) ? td : [...td.querySelectorAll("*")].find(clips) || null);
    const overflowing = [];   // a header label wider than its own column
    const shortColumns = [];  // a value wider than its column, not clipped
    const silentClips = [];   // a clip that keeps the lost text to itself
    const misaligned = [];    // a spanning cell that misses its columns
    const spannedOver = [];   // a spanning cell's text overflowing its span
    const clipped = [];       // fine, and worth saying out loud
    const ths = [...table.querySelectorAll("thead th")];
    const shown = ths.filter((th) => getComputedStyle(th).display !== "none");
    const need = ths.map(() => 0), widest = ths.map(() => ""), clipper = ths.map(() => null);
    for (const tr of rows) {
      let col = 0;
      for (const td of tr.children) {
        const span = td.colSpan > 0 ? td.colSpan : 1;
        if (span > 1) {
          // Not one column's value: it must cover exactly the columns it spans
          // (a colSpan that misses is a misaligned grid) and hold its text.
          let spanW = 0;
          for (let k = col; k < col + span; k++) spanW += ths[k] ? ths[k].clientWidth : 0;
          const label = td.textContent.trim().slice(0, 16);
          if (Math.abs(td.clientWidth - spanW) > 1)
            misaligned.push(`${label || `${span} cols`}: ${td.clientWidth} vs ${spanW}`);
          if (td.scrollWidth > td.clientWidth + 1)
            spannedOver.push(`${label}:${td.scrollWidth}>${td.clientWidth}`);
        } else if (ths[col]) {
          const clone = td.cloneNode(true);
          clone.style.cssText = "position:absolute;visibility:hidden;white-space:nowrap;width:auto;max-width:none";
          document.body.appendChild(clone);
          const w = clone.getBoundingClientRect().width;
          clone.remove();
          if (w > need[col]) {
            need[col] = w;
            widest[col] = td.textContent.trim().slice(0, 20);
            clipper[col] = clipperOf(td);
          }
        }
        col += span;
      }
    }
    ths.forEach((th, i) => {
      const label = (th.textContent || "").trim().slice(0, 14);
      if (th.scrollWidth > th.clientWidth + 1)
        overflowing.push(`${label}:${th.scrollWidth}>${th.clientWidth}`);
      if (need[i] > th.clientWidth + 1) {
        const el = clipper[i];
        const value = el ? el.textContent.trim() : widest[i];
        const title = el ? el.getAttribute("title") || "" : "";
        if (!el) shortColumns.push(`${label}: has ${th.clientWidth}, needs ${Math.round(need[i])}`);
        else if (!title.includes(value))
          silentClips.push(`${label}: "${value.slice(0, 16)}" clipped, title "${title.slice(0, 24)}"`);
        else clipped.push(`${label}: ${th.clientWidth}<${Math.round(need[i])}, title "${value.slice(0, 16)}"`);
      }
    });
    return {
      rowCount: rows.length,
      visibleColumns: shown.length,
      rowHeights: rows.slice(0, 4).map((tr) => Math.round(tr.getBoundingClientRect().height)),
      squeezed,
      overflowing,
      shortColumns,
      silentClips,
      misaligned,
      spannedOver,
      clipped,
    };
  });

  // A stale list is migrated, not obeyed: the ids it kept survive and this
  // build's default columns come back with them.
  const migrated = await page.evaluate(() => ({
    tracks: JSON.parse(localStorage.getItem("mlo-cols4-tracks") || "[]"),
    legacy: localStorage.getItem("mlo-cols3-tracks"),
  }));
  console.log("\n[column prefs]");
  check("a stale prefs list migrates instead of gutting the table",
        migrated.tracks.includes("num") && migrated.tracks.includes("title") && migrated.tracks.includes("album"),
        migrated.tracks.join(","));
  check("the migrated list replaces the old key", migrated.legacy === null, String(migrated.legacy));

  for (const view of VIEWS) {
    await page.getByRole("tab", { name: view, exact: true }).click();
    await page.waitForTimeout(600);
    const g = await geom();
    if (process.env.OUT) await page.screenshot({ path: `${OUT}/${view.toLowerCase()}.png` });
    console.log(`\n[${view}]`);
    check("rows rendered", g.rowCount > 0, `${g.rowCount} rows`);
    // The floor, not the exact set: the reported failure drew ONE column, and
    // a table is not allowed to lose its data to a preference list again.
    const floor = view === "Artists" ? 4 : 6;
    check("the table kept its columns", g.visibleColumns >= floor,
          `${g.visibleColumns} column(s)`);
    check("no squeezed names", g.squeezed.length === 0, g.squeezed.join(", "));
    check("every header label fits its column", g.overflowing.length === 0, g.overflowing.join(", "));
    check("every column holds its widest value", g.shortColumns.length === 0, g.shortColumns.join(", "));
    // A clip is allowed — a name longer than any sane column has to go
    // somewhere — but only with the whole value in its title, so the text is
    // one hover away instead of gone.
    check("a clipped value keeps its whole text in the title",
          g.silentClips.length === 0, g.silentClips.join(", "));
    check("a spanning cell covers the columns it spans",
          g.misaligned.length === 0, g.misaligned.join(", "));
    check("no spanning cell overflows its own row",
          g.spannedOver.length === 0, g.spannedOver.join(", "));
    if (g.clipped.length) console.log(`  note clipped on purpose — ${g.clipped.join(", ")}`);
    check("row heights are sane", Math.max(...g.rowHeights) < 120, g.rowHeights.join("/"));
  }

  // Facets. The menu stays open across picks (they compose), so the view is
  // switched after closing it — its backdrop covers the page by design.
  const pick = async (label) => {
    await page.getByRole("button", { name: new RegExp(`^${label}\\b`) }).first().click();
    await page.waitForTimeout(500);
  };
  const openFilter = async () => {
    await page.getByRole("button", { name: /All|Filter the library/ }).first().click();
    await page.waitForTimeout(400);
  };
  console.log("\n[facets]");
  await openFilter();
  await pick("Rated");
  check("Rated keeps only the rated rows", (await geom()).rowCount === 1, `${(await geom()).rowCount} rows`);
  await pick("Unrated");
  check("Unrated drops the rated row", (await geom()).rowCount === tracks.length - 1,
        `${(await geom()).rowCount} of ${tracks.length}`);
  await pick("Any rating");
  await pick("Explicit");
  check("Explicit matches the payload's own count", (await geom()).rowCount === explicit,
        `${(await geom()).rowCount} vs ${explicit}`);
  await pick("Clean");
  check("Clean excludes the explicit tracks", (await geom()).rowCount === tracks.length - explicit,
        `${(await geom()).rowCount} of ${tracks.length}`);
  await page.keyboard.press("Escape");
  if (process.env.OUT) await page.screenshot({ path: `${OUT}/facet-clean.png` });

  await browser.close();
  console.log(`\n${failures ? `${failures} problem(s)` : "PASS — library tables and facets"}`);
  process.exit(failures ? 1 : 0);
})();
