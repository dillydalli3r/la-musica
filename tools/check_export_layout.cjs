#!/usr/bin/env node
/* The Export page's WIDTH assertions against a running app — the geometry a
 * screenshot review is too imprecise to hold.
 *
 * The bug class this pins: a page that caps its own width and then gives its
 * tables fixed-pixel columns, so on a wide window the cards stop growing, the
 * widest column is cut off by the wrapper's own edge, and the list ends after
 * two rows with half the card empty below it. On the owner's 1568 px window
 * the Source panel was 544 px wide holding a 736 px table: the Artist column
 * began at 448 px and the rest of it sat outside the visible box, which reads
 * exactly like a cell clipped mid-word ("Radiohe…").
 *
 * Nothing in the code says "1152"; only the rendered box can see that the
 * container stopped growing, that the table is wider than its wrapper, or that
 * the list does not fill the card it was given. So every assertion here is
 * measured in the page, at the owner's window and at a wider one.
 *
 * Needs a live backend serving the built app (`web/dist`):
 *   npm --prefix web run build
 *   MLO_MUSIC_FOLDER=/path/to/a/test/library \
 *     python -m uvicorn server.main:app --host 127.0.0.1 --port 8011
 *   node tools/check_export_layout.cjs http://127.0.0.1:8011
 *
 * Never point it at port 8000 (the owner's own instance — see AGENTS.md).
 * The Albums/Artists/Tracks tabs and the layout checks need a library with
 * albums in it; without rows the tab's own empty state is asserted instead and
 * the row-level checks are skipped, which is reported, not silently passed.
 *
 * Playwright is required (same resolution as tools/check_menus.cjs): set
 * PLAYWRIGHT to a module path, or install it. Exit 2 when it is missing. */
let chromium;
try {
  // Plain require resolves from this file's folder up to the repo root's
  // node_modules; PLAYWRIGHT overrides it (e.g. a global install).
  ({ chromium } = require(process.env.PLAYWRIGHT || "playwright"));
} catch (e) {
  console.error("[check_export_layout] Playwright not found — install it with " +
    "`npm i -D playwright` (or set PLAYWRIGHT=/path/to/playwright).");
  process.exit(2);
}

const BASE = process.argv.slice(2).find((a) => !a.startsWith("--")) || process.env.BASE || "http://127.0.0.1:8011";

/* The owner's window, a laptop, and a wide desktop: the two ends are what the
 * container's growth is measured between. */
const WIDE = { width: 1568, height: 756 };
const WIDER = { width: 1920, height: 1080 };
const NARROW = { width: 1024, height: 756 };

/* A card's list is expected to end at the card's own padding: 16 px (`.panel`'s
 * p-4) plus a little slack. The regression this pins left 749 px. */
const SLACK_MAX = 32;

/* The picker's rows, the table's own box, and the page container — everything
 * measured inside the page, in one round trip. `clipped` is the strict form of
 * "the cell cannot show what it holds": a box whose content is wider or taller
 * than the box itself. */
const MEASURE = `(() => {
  const box = (el) => { const b = el.getBoundingClientRect(); return { x: Math.round(b.left), w: Math.round(b.width), h: Math.round(b.height), bottom: Math.round(b.bottom), right: Math.round(b.right) }; };
  const main = document.querySelector("main");
  // The page container is the route's own width-capped div.
  const page = main && main.querySelector('[class*="max-w-"]');
  const cs = page ? getComputedStyle(page) : null;
  const pads = cs ? parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight) : 0;
  const panels = [...document.querySelectorAll("main .panel")];
  const src = panels[0], dst = panels[1];
  const table = src && src.querySelector("table");
  const wrapper = table && table.parentElement;
  const heads = table ? [...table.querySelectorAll("thead th")].map((th) => {
    const sr = th.querySelector(".sr-only");
    return (sr ? sr.textContent : th.textContent).trim();
  }) : [];
  const cells = [];
  if (table) for (const [i, tr] of [...table.querySelectorAll("tbody tr")].entries()) {
    for (const [j, td] of [...tr.children].entries()) {
      if (!td.clientWidth && !td.clientHeight) continue; // a phone fold
      cells.push({
        row: i, col: heads[j] || "#" + j,
        text: (td.textContent || "").trim().slice(0, 30),
        w: Math.round(td.getBoundingClientRect().width),
        clipped: td.scrollWidth > td.clientWidth + 1 || td.scrollHeight > td.clientHeight + 1,
      });
    }
  }
  const last = table && table.querySelector("tbody tr:last-child");
  const contentW = page ? Math.round(page.getBoundingClientRect().width - pads) : null;
  const pageBox = page ? box(page) : null;
  return {
    vw: window.innerWidth,
    docScrollWidth: document.documentElement.scrollWidth,
    contentW,
    contentLeft: pageBox ? pageBox.x + (pads / 2) : null,
    contentRight: pageBox ? pageBox.right - (pads / 2) : null,
    src: src ? box(src) : null,
    dst: dst ? box(dst) : null,
    table: table ? { w: Math.round(table.getBoundingClientRect().width), wrapperW: Math.round(wrapper.getBoundingClientRect().width) } : null,
    wrapperBottom: wrapper ? box(wrapper).bottom : null,
    listBottom: last ? box(last).bottom : null,
    rowCount: table ? table.querySelectorAll("tbody tr").length : 0,
    cells,
    clipped: cells.filter((c) => c.clipped),
    emptyNote: table ? null : (src ? (src.innerText || "").split("\\n").slice(-2).join(" ") : null),
  };
})()`;

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROME, headless: true });
  const errs = [];
  let fail = 0;
  const check = (label, ok, detail = "") => {
    console.log(`  ${ok ? "ok  " : "FAIL"} ${label}${ok ? "" : "  " + detail}`);
    if (!ok) fail++;
  };

  /* Load /export on one tab and measure it. The picker's tabs are `role=tab`
   * buttons in a Segmented control, and Albums is the tab whose rows carry
   * album / artist prose. */
  const measure = async (vp, tab) => {
    const page = await browser.newPage({ viewport: vp });
    page.on("pageerror", (e) => errs.push(e.message));
    await page.goto(`${BASE}/export`, { waitUntil: "networkidle" });
    await page.waitForTimeout(900);
    await page.getByRole("tab", { name: tab, exact: true }).click();
    await page.waitForTimeout(500);
    const m = await page.evaluate(MEASURE);
    await page.close();
    return m;
  };

  const dimensions = async (page) => page.evaluate(() => {
    const p = document.querySelector('main [class*="max-w-"]');
    const cs = getComputedStyle(p);
    return {
      contentW: Math.round(p.getBoundingClientRect().width - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)),
      vw: window.innerWidth,
      ds: document.documentElement.scrollWidth,
    };
  });

  // ---- the page gives its width to the window ----------------------------
  // Measured without clicking a tab: this is the container, not the list.
  const widths = {};
  for (const vp of [WIDE, WIDER]) {
    const page = await browser.newPage({ viewport: vp });
    page.on("pageerror", (e) => errs.push(e.message));
    await page.goto(`${BASE}/export`, { waitUntil: "networkidle" });
    await page.waitForTimeout(700);
    widths[vp.width] = await dimensions(page);
    await page.close();
  }
  const own = widths[WIDE.width], wide = widths[WIDER.width];
  check("the page container grows with the window (1568 → 1920)",
    wide.contentW > own.contentW + 100,
    `content ${own.contentW}px at 1568 vs ${wide.contentW}px at 1920 — the container is capped`);
  check("no page-level horizontal scrollbar",
    own.ds <= own.vw + 2 && wide.ds <= wide.vw + 2,
    `document ${own.ds}/${own.vw} and ${wide.ds}/${wide.vw}`);

  // ---- the picker: rows readable, list filling its card ------------------
  for (const tab of ["Albums", "Artists", "Tracks"]) {
    const m = await measure(WIDE, tab);
    const head = `${tab} tab @1568`;
    if (!m.table) {
      console.log(`  --   ${head}: skipped, no rows in this library (${JSON.stringify(m.emptyNote)})`);
      continue;
    }
    check(`${head}: ${m.rowCount} rows are drawn`, m.rowCount > 0, "no rows");
    check(`${head}: no cell is clipped (scrollWidth ≤ clientWidth)`,
      m.clipped.length === 0, JSON.stringify(m.clipped));
    check(`${head}: the table fits its wrapper (the row is not cut off at the edge)`,
      m.table.w <= m.table.wrapperW + 1,
      `table ${m.table.w}px in a ${m.table.wrapperW}px wrapper`);
    check(`${head}: the list fills the card it was given (no dead half)`,
      m.src.bottom - m.wrapperBottom <= SLACK_MAX,
      `${m.src.bottom - m.wrapperBottom}px of the ${m.src.h}px card below the list`);
    if (tab === "Albums") {
      const prose = m.cells.filter((c) => ["Album", "Artist"].includes(c.col));
      check(`${head}: the album row's prose columns are wider than the old floors`,
        prose.length > 0 && prose.every((c) => c.w > 108),
        JSON.stringify(prose));
      const m2 = await measure(WIDER, tab);
      const wideCells = new Map((m2.cells || []).map((c) => [c.col, c.w]));
      const grew = prose.filter((c) => (wideCells.get(c.col) ?? 0) > c.w);
      check(`${head}: those columns take the extra width at 1920`,
        prose.length > 0 && grew.length === prose.length,
        `1568 ${JSON.stringify(prose.map((c) => [c.col, c.w]))} vs 1920 ${JSON.stringify([...wideCells])}`);
      check(`${tab} tab @1920: no cell is clipped`,
        (m2.clipped || []).length === 0, JSON.stringify(m2.clipped));
    }
  }

  // ---- the narrow window keeps the same promises -------------------------
  const narrow = await measure(NARROW, "Albums");
  if (narrow.table) {
    check("Albums tab @1024: no cell is clipped", narrow.clipped.length === 0, JSON.stringify(narrow.clipped));
    check("Albums tab @1024: the table fits its wrapper",
      narrow.table.w <= narrow.table.wrapperW + 1,
      `table ${narrow.table.w}px in a ${narrow.table.wrapperW}px wrapper`);
    check("Albums tab @1024: both cards fill the row",
      Math.abs(narrow.dst.right - narrow.contentRight) <= 2 && Math.abs(narrow.src.x - narrow.contentLeft) <= 2,
      `cards ${narrow.src.x}..${narrow.dst.right} vs content ${narrow.contentLeft}..${narrow.contentRight}`);
  } else {
    console.log("  --   Albums tab @1024: skipped, no rows in this library");
  }

  // ---- the destination side keeps short inputs short --------------------
  const dest = await browser.newPage({ viewport: WIDER });
  dest.on("pageerror", (e) => errs.push(e.message));
  await dest.goto(`${BASE}/export`, { waitUntil: "networkidle" });
  await dest.waitForTimeout(900);
  const wideInputs = await dest.evaluate(() => {
    const panel = [...document.querySelectorAll("main .panel")][1];
    const label = (el) => (el.closest("label")?.textContent || el.parentElement?.textContent || "").trim().slice(0, 24);
    return [...panel.querySelectorAll("input, select")].map((el) => ({
      tag: el.tagName.toLowerCase(),
      label: label(el),
      w: Math.round(el.getBoundingClientRect().width),
      wide: el.getBoundingClientRect().width > 400,
    }));
  });
  await dest.close();
  const absurd = wideInputs.filter((f) => f.wide && /subfolder|workers/i.test(f.label));
  check("a short-labelled input does not stretch across the wide column",
    absurd.length === 0, JSON.stringify(absurd));
  check("every wide control on the destination side is a text/label control",
    wideInputs.every((f) => !f.wide || f.tag === "select"),
    JSON.stringify(wideInputs.filter((f) => f.wide)));

  check("no uncaught page errors", errs.length === 0, errs.join(" | "));
  await browser.close();
  console.log(`\n${fail ? "FAIL" : "PASS"} — ${fail} problem(s)`);
  process.exit(fail ? 1 : 0);
})();
