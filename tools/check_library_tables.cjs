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
 *   * a stored visible-id list cannot gut the album tracklist: the owner's own
 *     state — `mlo-cols4-album-tracks = ["num","cover","title"]`, with a v1
 *     record or none — draws all seven columns and is rewritten as the v2
 *     record (`{v: 2, removed, added}`, `mlo-coldft-*`) that says what the
 *     reader CHOSE; and a v2 record that says "removed dr" keeps `dr` hidden
 *     across reloads. The owner's 900 px width map beside the same list hides
 *     nothing either. A one-line row is also pinned at the clean row's height
 *     and under 60 px: the 70 px the owner's screenshot shows is that 52 px row
 *     at the app's own zoom (`mlo.zoom` 135 → 70 px exactly), not a box in the
 *     row.
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
 *     folds and the album table holds the floor its own columns sum to
 *     (`ALBUM_TRACK_MIN_W`, derived from the column spec — it was a pinned
 *     814 px before that). Before the phone fold existed the album page held
 *     that floor at every width, so a 390 px phone drew an 814 px table in a
 *     342 px wrapper and gave the name 8 px — one syllable per line, which is
 *     the owner's screenshot (issue #55).
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
   * tappable — and, at `md` and up, the fold gone and the floor the columns
   * themselves sum to back.
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
      // Every row's height: a ROW that is taller than a one-line row is a fact
      // about the row, and the owner's tracklist rows were all ~70 px.
      rowMin: Math.min(...[...table.querySelectorAll("tbody tr")].map((tr) => Math.round(tr.getBoundingClientRect().height))),
      rowMax: Math.max(...[...table.querySelectorAll("tbody tr")].map((tr) => Math.round(tr.getBoundingClientRect().height))),
      // Every column's own width, folded ones as 0 — what the stored-widths
      // cases below compare against the clean table's floors.
      widths: ths.map((t) => ({ label: t.label, w: t.display === "none" ? 0 : t.w })),
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
  // album table holds the floor its own columns sum to (ALBUM_TRACK_COL_W plus
  // the corner control; 1000 px, and 1192 with the owner's two 96 px tag
  // columns this check sets up — the genre floor alone is 208 of that, sized to
  // a two-name value ("Metal; Alternative Metal" is 178 at this font). It used
  // to be a pinned `md:min-w-[814px]`,
  // which is why the bound below is the columns' own sum rather than the old
  // constant — the floor is DERIVED from the column spec now (see
  // ALBUM_TRACK_MIN_W in lib/columns), so what is pinned is that the table is
  // wider than the wrapper it sits in and the wrapper is what scrolls.
  await page.setViewportSize({ width: 800, height: 900 });
  await page.waitForTimeout(300);
  const md = await albumGeom();
  console.log("\n[album tracklist @800]");
  check("md and up fold nothing", (md.folded || []).length === 0, (md.folded || []).join(","));
  check("and the table holds its columns' own floor", md.tableW >= 880, `${md.tableW} px`);
  check("…with the wrapper scrolling for it",
        md.wrapScroll > md.wrapClient + 1, `${md.wrapScroll} vs ${md.wrapClient}`);

  // ---- the album tracklist at 1440: the NAME column takes the free width ----
  /* The owner's second report on this table: a long title wrapped to two lines
   * while the table had hundreds of px of free width beside it. The fixed
   * layout was spreading that free width over EVERY column in proportion to its
   * width, so the name column grew from its 280 px floor to 378 of 1200 while
   * the name's own box (the column minus the row's chrome) got 216 — and the
   * leftover of the other columns sat under their own text, which is the gap
   * the screenshot shows between the name and Genre.
   *
   * The name column is `md:w-auto` now (ALBUM_TRACK_COL_W.title) so it takes
   * what the fixed columns leave, and it keeps its floor through
   * `ColFloorHolder` — an empty zero-height box inside the header cell, because
   * a `min-width` on the cell is IGNORED in a fixed layout (measured: a title
   * cell asking for 280 px came out 66 px wide with a 0 px name). What this
   * case pins is both halves: the column takes the free width — it must be
   * exactly what the wrapper has after the fixed columns, measured as that
   * difference rather than as a number, since the floors themselves are
   * measurements of their own values and move when one is re-measured (the
   * genre floor did: 96 → 160 → 208 px, "Metal; Alternative Metal" is 178) — and the wrapper
   * still does not scroll at this width, so the width it took WAS free.
   *
   * The second half is the reader's own outcome: of the album's titles, any
   * title whose one-line width fits the name's box must be drawn on ONE line,
   * the same row height as a short one. A title whose one-line width does not
   * fit (a genuinely long name at this window) wraps — that is the case the
   * floor-and-scroll rule exists for, and the @800 block above still measures
   * the scroll. */
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(400);
  const wide = await albumGeom();
  /** The album's titles as drawn, with each one's ONE-LINE width measured off
   *  a clone (`nowrap`, no max-width), so "it should not have wrapped" is a
   *  fact about the title and not about the window. */
  const titleRows = await page.evaluate(() => {
    const table = [...document.querySelectorAll("main table")].find((t) =>
      [...t.querySelectorAll("thead th")].some((th) => (th.textContent || "").trim() === "Title"));
    if (!table) return [];
    return [...table.querySelectorAll("tbody tr")].map((tr) => {
      const link = [...tr.querySelectorAll("a[title*='Click to play']")].find((a) => (a.textContent || "").trim());
      if (!link) return null;
      const clone = link.cloneNode(true);
      clone.style.cssText = "position:absolute;white-space:nowrap;max-width:none;visibility:hidden;left:-9999px";
      document.body.appendChild(clone);
      const need = Math.round(clone.getBoundingClientRect().width);
      clone.remove();
      const lh = parseFloat(getComputedStyle(link).lineHeight) || 20;
      return {
        text: (link.textContent || "").trim(),
        need,
        box: Math.round(link.getBoundingClientRect().width),
        lines: +(link.getBoundingClientRect().height / lh).toFixed(2),
        rowH: Math.round(tr.getBoundingClientRect().height),
      };
    }).filter(Boolean);
  });
  console.log("\n[album tracklist @1440]");
  console.log(`  name column ${wide.titleColW} px, wrapper ${wide.wrapScroll}/${wide.wrapClient} px, rows: `
    + titleRows.map((r) => `"${r.text.slice(0, 18)}…" need ${r.need} box ${r.box} ${r.lines} line(s)`).join(" | "));
  // The name column is the row's ONE flexible column, so what it takes is
  // whatever the wrapper has left after every other cell — the data columns
  // AND the corner control, which is chrome but still holds width. Stated as
  // that difference rather than as a number, because the fixed columns' floors
  // are measurements of their own values and move when one of them is
  // re-measured (the genre floor did: 96 → 160 → 208 px, sized to a two-name value).
  const otherCols = wide.widths.filter((w) => w.w > 0 && w.label !== "Title")
    .reduce((n, w) => n + w.w, 0);
  const slack = wide.wrapClient - otherCols - wide.titleColW;
  check(`the name column takes the table's free width (${wide.titleColW} px = `
        + `${wide.wrapClient} − ${otherCols} of other cells, its own floor is 280)`,
        wide.titleColW >= 280 && Math.abs(slack) <= 2, `${wide.titleColW} px, ${slack} px unaccounted`);
  check(`…and the table still fits, so what it took was free `
        + `(${wide.wrapScroll}/${wide.wrapClient} px)`,
        wide.wrapScroll <= wide.wrapClient + 1, JSON.stringify({ scroll: wide.wrapScroll, client: wide.wrapClient }));
  const shortRow = titleRows.slice().sort((a, b) => a.need - b.need)[0];
  const wrapped = titleRows.filter((r) => r.need <= r.box + 1 && r.lines > 1);
  check(`a title that fits its box is drawn on ONE line, at a short title's row height `
        + `(${titleRows.length} title(s), longest needs ${Math.max(0, ...titleRows.map((r) => r.need))} px `
        + `in a ${shortRow?.box ?? 0} px box)`,
        wrapped.length === 0 && titleRows.every((r) => r.need > r.box + 1 || r.rowH <= (shortRow?.rowH ?? 0) + 2),
        JSON.stringify(wrapped));

  // ---- the same tracklist with hostile-but-legal stored widths -------------
  /* The owner's report on v4.2.0: "columns are VERY long for some reason, this
   * is a major bug. Also rows seem really wide." A `table-layout: fixed` table
   * takes the sum of its columns for its own floor, so a stored width map the
   * resize handle itself could have produced — every value inside its 40-900
   * clamp — drew an album tracklist of 3848 px in a 1200 px box with
   * `mlo-colw-album-tracks` = {title:900, genre:900, bitrate:900, dur:900},
   * pushing every column after the title off the right edge. useFittedWidths
   * makes a stored width a preference about how the table's width is SPENT,
   * never a floor the table has to be that wide: each column keeps its own
   * floor, what is left over is shared among the columns the reader sized, and
   * the table comes out exactly as wide as it is with NO stored widths at all
   * (see lib/columns.tsx). The check states exactly that, at four widths: the
   * table's width and its wrapper's scroll are the CLEAN numbers, every column
   * is still drawn, and no column fell under its own floor (read off the clean
   * table at 801 px, where the table is exactly its floors). */
  await ctx.addInitScript(() => {
    const seed = localStorage.getItem("mlo-test-width-seed");
    if (!seed) return;
    for (const [key, raw] of Object.entries(JSON.parse(seed))) {
      if (raw === null) localStorage.removeItem(key);
      else localStorage.setItem(key, raw);
    }
  });
  const WIDTHS = [1440, 1100, 801, 390];
  const HOSTILE = JSON.stringify({
    "mlo-colw-album-tracks": JSON.stringify({ title: 900, genre: 900, bitrate: 900, dur: 900 }),
  });
  const at = async (vw, seed) => {
    await page.setViewportSize({ width: vw, height: 900 });
    await page.evaluate((s) => {
      if (s === null) localStorage.removeItem("mlo-test-width-seed");
      else localStorage.setItem("mlo-test-width-seed", s);
    }, seed);
    await page.reload({ waitUntil: "networkidle" });
    await page.waitForSelector("main table tbody tr", { timeout: 30000 });
    await page.waitForTimeout(700);
    return albumGeom();
  };
  const floorGeom = await at(801, null);
  const floors800 = Object.fromEntries((floorGeom.widths || []).map((w) => [w.label, w.w]));
  const clean = {};
  const hostile = {};
  for (const vw of WIDTHS) {
    clean[vw] = await at(vw, null);
    hostile[vw] = await at(vw, HOSTILE);
    const c = clean[vw];
    const h = hostile[vw];
    const drawn = h.widths.filter((w) => w.w > 0);
    // The floor of each column, read where the table IS its floors.
    const under = drawn.filter((w) => (floors800[w.label] ?? 0) > 0 && w.w + 1 < floors800[w.label])
      .map((w) => `${w.label}:${w.w}<${floors800[w.label]}`);
    console.log(`\n[album tracklist ${vw} px] clean table ${c.tableW}, wrapper ${c.wrapScroll}/${c.wrapClient} px, `
      + `name ${c.titleColW} px | hostile table ${h.tableW}, wrapper ${h.wrapScroll}/${h.wrapClient} px, name ${h.titleColW} px`
      + (vw === 1440 ? `\n  hostile columns: ${h.widths.filter((w) => w.w > 0).map((w) => `${w.label}=${w.w}`).join(" ")}` : ""));
    check(`${vw} px: a stored width map cannot widen the tracklist (${h.tableW} px vs ${c.tableW} px clean)`,
          h.tableW === c.tableW, `${h.tableW} vs ${c.tableW}`);
    check(`${vw} px: …nor add a sideways scroll it did not have (${h.wrapScroll} vs ${c.wrapScroll} px)`,
          h.wrapScroll === c.wrapScroll, `${h.wrapScroll} vs ${c.wrapScroll}`);
    /* Every column keeps at least its own floor — compared against the floors
     * read at 801 px, where the table IS its floors. Below `md` the floors of
     * the folded columns are not in force at all (the phone's own fold decides
     * the layout there), so what is pinned at 390 px is the two checks above
     * plus that no column went away. */
    check(`${vw} px: …and every column is still drawn at least its own floor `
          + `(${drawn.length} drawn, ${vw < 768 ? "the phone's own fold decides below md" : `${under.length} under`})`,
          drawn.length === (c.widths.filter((w) => w.w > 0).length) && (vw < 768 || under.length === 0),
          under.join(",") || JSON.stringify(drawn));
  }
  await page.evaluate(() => localStorage.removeItem("mlo-test-width-seed"));
  await page.setViewportSize({ width: 1440, height: 900 });

  // ---- the album page obeys the app's own page width ----------------------
  /* The report behind it: "Columns in the app are way to long, atleast on
   * album pages, also rows seem to wide. To be perfectly clear, columns should
   * auto-fit to the space on screen." AlbumPage (and TrackPage) were the only
   * pages without the `mx-auto max-w-[1600px]` wrapper every other page
   * carries — Home, Library, Artist, Downloads, Favorites, Grading, Settings,
   * the import wizard — so the tracklist was the one surface that stretched to
   * the window: at 2560 the table was 2320 px wide and the name column alone
   * took 1712 of it. Since the name column is deliberately the row's one
   * flexible column (R313, and the owner's own earlier report about a title
   * wrapping while free width sat beside it), the page width IS the lever: the
   * free width is the page's, and the page is 1600 px like every other one. */
  const capped = await at(2560, null);
  check("a 2560 px window does not widen the album tracklist past the app's page width",
        capped.tableW <= 1600 && capped.wrapScroll <= capped.wrapClient + 1
        && capped.titleColW < 1200,
        `table ${capped.tableW} px, name column ${capped.titleColW} px, wrapper ${capped.wrapScroll}/${capped.wrapClient} px`);

  // ---- a stored visible-id list that hides most of the table --------------
  /* The owner's album page, read as a SHAPE: three columns (`#`, COVER, Title)
   * with the title eating the row and Genre / Dur / Bitrate / DR simply not
   * drawn — while the same table's Columns menu lists all seven, because that
   * menu draws the list the hook RETURNS. A stored list under the CURRENT key
   * was obeyed blindly: `mlo-cols4-album-tracks = ["num","cover","title"]` drew
   * three columns of a seven-column build and nothing on screen said where the
   * other four had gone.
   *
   * A stored list is a reading of the build that WROTE it, never evidence about
   * this one — the rule the v3 key (`mlo-cols3-*`) and `useFittedWidths` below
   * already follow. What is stored beside the list (`mlo-coldft-*`) is the
   * reader's own CHOICE as a versioned record — `{v: 2, removed, added}`, which
   * columns this build ships visible they took away and which it does not ship
   * they put there — and the drawn set is derived from that, never from the
   * list. A record without the version (a v1 fingerprint, a bare array, no
   * record at all) is UNKNOWN rather than a choice: the list is MIGRATED, its
   * own ids kept and every column this view draws by default added back, and
   * the result is re-recorded as v2. That is what the owner's own state needs —
   * the three-id list PLUS the v1 record — and what makes a reader's real
   * unticking stick: a v2 record that says "removed dr" hides dr on every later
   * load.
   *
   * The ROW height is pinned here too, because the owner's screenshot carries
   * the same number: ~70 px per row. There is no 70 px box in a row — a
   * one-line row is the cover cell's own 32 px box plus the `.td`'s 20 px of
   * padding = 52 px, measured. The 70 in the screenshot is that 52 px at the
   * app's own zoom (main.tsx's device zoom scales the ROOT FONT SIZE: at
   * `mlo.zoom` 135 the same row measures exactly 70 px), so what is pinned is
   * the number the layout owns — the row must be the CLEAN row's height, and
   * under 60 px at 100 %. */
  /* The hostile states seed `mlo-coldft-album-tracks` explicitly on purpose: a
   * record left behind by the case above would make the next seed read as the
   * reader's own choice. `THREE_IDS_AND_V1` is the owner's state exactly — the
   * three-id list plus a v1 record (a bare array of ids, the shape the first
   * record had), which says nothing about what they CHOSE and must therefore be
   * reconciled, not obeyed. */
  const THREE_IDS = JSON.stringify({
    "mlo-cols4-album-tracks": JSON.stringify(["num", "cover", "title"]),
    "mlo-colw-album-tracks": null,
    "mlo-coldft-album-tracks": null,
  });
  const THREE_IDS_AND_V1 = JSON.stringify({
    "mlo-cols4-album-tracks": JSON.stringify(["num", "cover", "title"]),
    "mlo-colw-album-tracks": null,
    "mlo-coldft-album-tracks": JSON.stringify(["num", "cover", "title", "genre", "dur", "bitrate", "dr"]),
  });
  const THREE_IDS_AND_WIDTHS = JSON.stringify({
    "mlo-cols4-album-tracks": JSON.stringify(["num", "cover", "title"]),
    "mlo-colw-album-tracks": JSON.stringify({ title: 900, genre: 900, bitrate: 900, dur: 900 }),
    "mlo-coldft-album-tracks": null,
  });
  /* The reader's OWN state as v2: they removed `dr` and added the two tag
   * columns. No list is seeded at all — the v2 record is the state, and the
   * drawn set has to come from it. */
  const UNTICKED_V2 = JSON.stringify({
    "mlo-cols4-album-tracks": null,
    "mlo-colw-album-tracks": null,
    "mlo-coldft-album-tracks": JSON.stringify({ v: 2, removed: ["dr"], added: ["tag:RC", "tag:WR"] }),
  });
  const storedList = () => page.evaluate(() => ({
    list: JSON.parse(localStorage.getItem("mlo-cols4-album-tracks") || "null"),
    record: JSON.parse(localStorage.getItem("mlo-coldft-album-tracks") || "null"),
  }));
  const control = await at(1568, JSON.stringify({
    "mlo-cols4-album-tracks": null,
    "mlo-colw-album-tracks": null,
    "mlo-coldft-album-tracks": null,
  }));
  const gutted = await at(1568, THREE_IDS);
  const guttedStored = await storedList();
  const guttedV1 = await at(1568, THREE_IDS_AND_V1);
  const guttedV1Stored = await storedList();
  const guttedWidths = await at(1568, THREE_IDS_AND_WIDTHS);
  const unticked = await at(1568, UNTICKED_V2);
  const untickedStored = await storedList();
  // The same v2 record, read a SECOND time: the reader's removal is the state,
  // so a reload must not resurrect `dr` (that is the whole point of the version).
  const untickedReload = await at(1568, UNTICKED_V2);
  const untickedReloadStored = await storedList();
  /** The label of every column the table DRAWS, in its own order — the corner
   *  cell (select toggle + columns chooser) is the table's furniture, not a
   *  data column, so it is left out of the set the two states are compared on. */
  const colset = (g) => (g.kept || []).filter((l) => l !== "(corner)").join(",");
  /** The SHORTEST row on the table: the one line's height. The longest title in
   *  this library is 55 characters and legitimately wraps in the 9-column
   *  control, which is what `albumGeom`'s own `name.rowH` measures — the owner's
   *  rows were all the same height, so the one-line row is the number to pin. */
  const oneLineRow = (g) => g.rowMin ?? 0;
  const V2 = (r) => !!r && r.v === 2 && Array.isArray(r.removed) && Array.isArray(r.added);
  const CONTROL_COLS = colset(control);
  const UNTICKED_COLS = CONTROL_COLS.split(",").filter((l) => l !== "DR").join(",");
  console.log("\n[album tracklist @1568, stored visible list]");
  for (const [what, g] of [["control", control], ["three ids", gutted], ["three ids + v1 record", guttedV1],
    ["three ids + 900 px widths", guttedWidths], ["v2 record: removed dr", unticked],
    ["…and the same record reloaded", untickedReload]]) {
    console.log(`  ${what.padEnd(28)} ${colset(g)} · table ${g.tableW} px in ${g.wrapScroll}/${g.wrapClient} px `
      + `· rows ${oneLineRow(g)}-${g.rowMax} px`);
  }
  console.log(`  after the three-id list: list ${JSON.stringify(guttedStored.list)} · record ${JSON.stringify(guttedStored.record)}`);
  console.log(`  after the v1 record:     list ${JSON.stringify(guttedV1Stored.list)} · record ${JSON.stringify(guttedV1Stored.record)}`);
  console.log(`  after the v2 record:     list ${JSON.stringify(untickedStored.list)} · record ${JSON.stringify(untickedStored.record)}`
    + ` | reloaded: ${JSON.stringify(untickedReloadStored.list)}`);
  check(`the control draws the album tracklist's own default columns (${CONTROL_COLS})`,
        control.kept.includes("Title") && CONTROL_COLS.split(",").length >= 9, control.kept.join(","));
  check(`a stored list of three ids cannot gut the tracklist (${colset(gutted)} vs ${CONTROL_COLS})`,
        colset(gutted) === CONTROL_COLS, colset(gutted));
  check(`…nor can it beside the v1 record — the owner's own state (${colset(guttedV1)})`,
        colset(guttedV1) === CONTROL_COLS, colset(guttedV1));
  check(`…and the owner's own stored 900 px widths beside it do not hide anything either `
        + `(${colset(guttedWidths)}, table ${guttedWidths.tableW} px vs ${control.tableW} px)`,
        colset(guttedWidths) === CONTROL_COLS && guttedWidths.tableW === control.tableW
          && guttedWidths.wrapScroll === control.wrapScroll,
        JSON.stringify({ cols: colset(guttedWidths), table: guttedWidths.tableW, wrap: guttedWidths.wrapScroll }));
  check(`…the repaired state is STORED as v2, so the menu and the next tick work from what is on screen `
        + `(list ${(guttedStored.list || []).length} ids, record ${JSON.stringify(guttedStored.record)})`,
        Array.isArray(guttedStored.list) && guttedStored.list.includes("dr") && guttedStored.list.length >= 9
          && V2(guttedStored.record) && guttedStored.record.removed.length === 0,
        JSON.stringify(guttedStored));
  check(`…and the v1 record is rewritten as v2 too (${JSON.stringify(guttedV1Stored.record)})`,
        V2(guttedV1Stored.record) && Array.isArray(guttedV1Stored.list) && guttedV1Stored.list.includes("dr")
          && guttedV1Stored.list.length >= 9,
        JSON.stringify(guttedV1Stored));
  check(`a v2 record's own removal is obeyed — the reader unticked DR (${colset(unticked)})`,
        colset(unticked) === UNTICKED_COLS, JSON.stringify({ drawn: colset(unticked), stored: untickedStored.list }));
  check(`…and it still holds on the NEXT load, so the removal is the state `
        + `(${colset(untickedReload)}, list ${JSON.stringify(untickedReloadStored.list)})`,
        colset(untickedReload) === UNTICKED_COLS && V2(untickedReloadStored.record)
          && (untickedReloadStored.record.removed || []).join(",") === "dr",
        JSON.stringify({ drawn: colset(untickedReload), record: untickedReloadStored.record }));
  check(`a one-line row is the CLEAN row's height, not 70 px `
        + `(${oneLineRow(gutted)} px gutted, ${oneLineRow(control)} px clean at 100 %)`,
        oneLineRow(control) > 0 && oneLineRow(gutted) === oneLineRow(control) && oneLineRow(control) < 60,
        `gutted ${oneLineRow(gutted)}, control ${oneLineRow(control)}, table ${gutted.tableW} px`);

  await page.evaluate(() => localStorage.removeItem("mlo-test-width-seed"));

  await browser.close();
  console.log(`\n${failures ? `${failures} problem(s)` : "PASS — library tables, facets, the album tracklist at 390 px, and the stored column list"}`);
  process.exit(failures ? 1 : 0);
})();
