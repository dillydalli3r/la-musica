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
 *   * at 390 px the album page's TRACKLIST folds the columns a phone cannot use
 *     (the same `PHONE_HIDE` rule the Library's tables use), keeps the row's
 *     spine (# / name / length), hands the name a reading column — at least
 *     120 px, at most two lines, the whole title in its `title` — and keeps the
 *     row's chrome (the heart, the "…", the stars) laid out inside that cell
 *     and tappable with the phone's own 44 px hit area. At `md` and up nothing
 *     folds and the album table keeps its 814 px floor. Before this the album
 *     page held that floor at every width, so a 390 px phone drew an 814 px
 *     table in a 342 px wrapper and gave the name 8 px — one syllable per line,
 *     which is the owner's screenshot (issue #55).
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

  // ---- the album page's tracklist at 390 px (issue #55) ----
  /* The owner's report: "title text in album pages looks very squished, title
   * text has aggressive text wrapping". The cause was geometric — the album
   * table held the desktop floor (`min-w-[814px]`) at every width, so a 390 px
   * phone got an 814 px table inside a 342 px wrapper, the fixed layout handed
   * the auto Name column the 8 px left over, and the browser broke the title
   * one syllable per line. What is pinned here is the phone's answer: the same
   * fold the Library's tables use, the row's spine (# / name / length) kept, a
   * name column with room to read (>= 120 px, at most two lines, the whole
   * title in its `title`), the row's own chrome laid out INSIDE that cell and
   * tappable — and, at `md` and up, the fold gone and the 814 px floor back.
   *
   * The library it points at needs at least one title long enough to wrap, or
   * the >= 40 px link rule flags a name that is legitimately 35 px wide (the
   * same rule the Library's own views are held to). `F:/tmp/mlo-iss55a` is the
   * scratch library this was written against: one album, five tracks, titles
   * from 5 to 55 characters, one explicit, and the owner's two extra tag
   * columns (Rc / Wr).
   */
  const albumPath = (lib.artists || []).flatMap((a) => a.albums || [])[0]?.path;
  if (!albumPath) {
    console.error("[check_library_tables] the library has no album to open");
    process.exit(1);
  }
  // The owner's own shape: two user-added tag columns (Rc, Wr) beside the
  // built-ins. A fold that only knew the built-in ids would leave both 96 px
  // tag columns in the phone row — and they are what squeezed the name first.
  await ctx.addInitScript(() => {
    localStorage.setItem("mlo-customcols-album-tracks", JSON.stringify([
      { id: "tag:RC", label: "Rc", tag: "RC" },
      { id: "tag:WR", label: "Wr", tag: "WR" },
    ]));
  });
  /** The album tracklist as the width in force draws it: which columns folded,
   *  what the NAME column got, and where the row's own chrome landed. The row
   *  measured is the one with the LONGEST title — a short name is as wide as
   *  its own text and would say nothing about whether the column has room. */
  const albumGeom = () => page.evaluate(() => {
    const table = [...document.querySelectorAll("main table")].find((t) =>
      [...t.querySelectorAll("thead th")].some((th) => (th.textContent || "").trim() === "Title"));
    if (!table) return { none: true };
    const wrap = table.parentElement;
    const ths = [...table.querySelectorAll("thead th")].map((th) => ({
      // The corner cell (the select toggle + the columns chooser) draws no
      // label of its own; the cover column does (`sr-only` "Cover").
      label: (th.textContent || "").trim() || "(corner)",
      w: Math.round(th.getBoundingClientRect().width),
      display: getComputedStyle(th).display,
    }));
    const named = [...table.querySelectorAll("tbody tr")].map((tr) => {
      const link = [...tr.querySelectorAll("a[title*='Click to play']")]
        .find((a) => (a.textContent || "").trim());
      return link ? { tr, link, text: (link.textContent || "").trim() } : null;
    }).filter(Boolean).sort((a, b) => b.text.length - a.text.length);
    let name = null;
    if (named[0]) {
      const { tr, link, text } = named[0];
      const cell = link.closest("td");
      const lh = parseFloat(getComputedStyle(link).lineHeight) || 20;
      /** The chrome the cell must not squeeze out. Each control is asked for by
       *  the name it PUBLISHES (its aria-label / title), not by a class the
       *  markup may rename: the like heart, the "…" track actions, the stars. */
      const chrome = [
        ["heart", cell.querySelector("button[aria-label*='favorite' i]")],
        ["…", cell.querySelector("button[title^='Track actions']")],
        ["stars", cell.querySelector("[role='group'][aria-label^='Rating']")],
      ].map(([what, el]) => {
        if (!el) return { what, missing: true };
        // On screen before hit-testing: a point below the fold has no element.
        el.scrollIntoView({ block: "center" });
        const r = el.getBoundingClientRect();
        const hitArea = getComputedStyle(el, "::after");
        const at = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        const box = cell.getBoundingClientRect();
        return {
          what,
          w: Math.round(r.width), h: Math.round(r.height),
          // The phone hit area the element itself carries (`tap-hit`'s ::after,
          // the app's own way of reaching 44 px without growing the control).
          tap: hitArea.content !== "none" ? Math.round(parseFloat(hitArea.width)) : 0,
          inside: r.left >= box.left - 1 && r.right <= box.right + 1,
          hit: !!at && (at === el || el.contains(at)),
        };
      });
      const box = cell.getBoundingClientRect();
      name = {
        text,
        titleAttr: link.getAttribute("title") || "",
        w: Math.round(link.getBoundingClientRect().width),
        lines: +(link.getBoundingClientRect().height / lh).toFixed(2),
        lh,
        cellW: Math.round(box.width),
        rowH: Math.round(tr.getBoundingClientRect().height),
        chrome,
      };
    }
    return {
      vw: innerWidth,
      docScroll: document.documentElement.scrollWidth,
      wrapClient: Math.round(wrap.clientWidth),
      wrapScroll: Math.round(wrap.scrollWidth),
      tableW: Math.round(table.getBoundingClientRect().width),
      kept: ths.filter((t) => t.display !== "none").map((t) => t.label),
      folded: ths.filter((t) => t.display === "none").map((t) => t.label),
      titleColW: ths.find((t) => t.label === "Title")?.w ?? 0,
      name,
    };
  });

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${BASE}/album/${encodeURIComponent(albumPath)}`, { waitUntil: "networkidle", timeout: 60000 });
  await page.waitForSelector("main table tbody tr", { timeout: 30000 });
  // The rows fade in on `.stagger` (a 10 px translateY), and a rect read
  // mid-animation is not the layout: settled first.
  await page.waitForTimeout(900);
  const phone = await albumGeom();
  if (process.env.OUT) await page.screenshot({ path: `${OUT}/album-phone.png` });
  console.log("\n[album tracklist @390]");
  check("the album page draws its tracklist", !!phone.name,
        phone.name ? `"${phone.name.text.slice(0, 26)}…"` : "no row with a title link");
  const fits = +!!phone.name && phone.wrapScroll <= phone.wrapClient + 1 && phone.docScroll <= phone.vw + 1;
  check("the table fits the phone — nothing scrolls sideways",
        fits, `wrapper ${phone.wrapScroll}/${phone.wrapClient} px, page ${phone.docScroll}/${phone.vw} px`);
  check("the phone folds the columns it cannot use",
        (phone.folded || []).join(",") === "Cover,Genre,Bitrate,DR,Rc,Wr", (phone.folded || []).join(","));
  // The corner cell is the table's own chrome (the columns chooser and the
  // select toggle), not a data column: a phone keeps it, and keeps the row's
  // spine with it.
  const spine = (phone.kept || []).filter((l) => l !== "(corner)").join(",");
  check("and keeps the row's spine — number, name, length",
        spine === "#,Title,Dur" && (phone.kept || []).includes("(corner)"),
        (phone.kept || []).join(","));
  check("the name column has room to read as a name (>= 120 px)",
        phone.titleColW >= 120, `${phone.titleColW} px, name cell ${phone.name?.cellW} px`);
  check("a long name is not squeezed (its link is >= 40 px wide)",
        (phone.name?.w ?? 0) >= 40, `${phone.name?.w} px for "${phone.name?.text?.slice(0, 26)}…"`);
  check("and it wraps to at most two lines",
        (phone.name?.lines ?? 99) <= 2, `${phone.name?.lines} line(s) of ${phone.name?.lh} px`);
  check("the whole title stays one tap-hold away",
        !!phone.name && phone.name.titleAttr.includes(phone.name.text), (phone.name?.titleAttr || "").slice(0, 48));
  const chrome = phone.name?.chrome || [];
  check("the row's chrome lands inside the name cell, not squeezed out",
        chrome.length === 3 && chrome.every((c) => !c.missing && c.inside && c.hit),
        chrome.map((c) => `${c.what} ${c.w ?? "?"}x${c.h ?? "?"}${c.inside ? "" : " outside"}${c.hit ? "" : " unhittable"}`).join(", "));
  // The icon buttons carry the phone's 44 px hit area (the app's `tap-hit`);
  // the stars keep the app's own star geometry — a 70 px strip whose halves are
  // as small as they are on every other rating control in the app — so what is
  // pinned for them is the width of that strip.
  check("the icon buttons carry the phone's 44 px hit area",
        chrome.filter((c) => c.what !== "stars").every((c) => c.tap >= 40),
        chrome.filter((c) => c.what !== "stars").map((c) => `${c.what} ${c.tap} px`).join(", "));
  check("and the rating strip is a target in its own right (>= 40 px wide)",
        (chrome.find((c) => c.what === "stars")?.w ?? 0) >= 40,
        `${chrome.find((c) => c.what === "stars")?.w} px`);
  check("the phone row is not a runaway ribbon",
        (phone.name?.rowH ?? 0) < 180, `${phone.name?.rowH} px tall`);

  // Desktop and tablet are untouched: at `md` and up nothing folds and the
  // album table holds the 814 px floor of its own columns, so its wrapper
  // scrolls rather than the name collapsing any further.
  await page.setViewportSize({ width: 800, height: 900 });
  await page.waitForTimeout(300);
  const md = await albumGeom();
  console.log("\n[album tracklist @800]");
  check("md and up fold nothing", (md.folded || []).length === 0, (md.folded || []).join(","));
  check("and the table keeps its 814 px floor", md.tableW >= 814, `${md.tableW} px`);
  check("…with the wrapper scrolling for it",
        md.wrapScroll > md.wrapClient + 1, `${md.wrapScroll} vs ${md.wrapClient}`);
  await page.setViewportSize({ width: 1440, height: 900 });

  await browser.close();
  console.log(`\n${failures ? `${failures} problem(s)` : "PASS — library tables, facets, and the album tracklist at 390 px"}`);
  process.exit(failures ? 1 : 0);
})();
