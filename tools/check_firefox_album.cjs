#!/usr/bin/env node
/* The album tracklist's geometry IN FIREFOX, against the same page in Chromium.
 *
 * The bug class this pins (issue #71, "Album view pages on firefox don't load
 * correctly"): a `table-layout: fixed` table that carries a width AND an
 * intrinsic floor (`md:min-w-max`) is laid out by Gecko's intrinsic pass at its
 * unconstrained sentinel. Measured on the album page: the table came out
 * 17,895,482 px wide (2^30 app units, not a name), the name column took
 * 17,895,026 of that, and every column to the right of it — bitrate, DR, the
 * column control — measured 0 px and sat 17 million px off the right edge. One
 * engine's whole page, from one declaration: Chromium drew 1200 px there, and
 * WebKit did not honour the floor at all (the name column measured 188 px where
 * its floor is 280, and 0 px in a 620 px box — the crushed name the floor
 * exists to prevent). Every other table in the app shares that floor
 * (`TABLE_FIT`), so the same measurements are what a Gecko user saw on the
 * Library, Browse, Export and Music Brainz pages too.
 *
 * What is asserted here is therefore GEOMETRY, in both engines, on the same
 * page and the same library:
 *
 *   * the tracklist's table is as wide as its columns: no runaway (`tableW`
 *     within 2000 px of the wrapper — 17,895,482 is not a name), its columns
 *     add up to its width, and its last column ends at its right edge;
 *   * every column is drawn above `md`: a 0 px bitrate, DR or column control is
 *     the failure this check exists for (below `md` the phone fold drops those
 *     columns on purpose, so the phone pass asserts the shape instead);
 *   * the name column is a reading column: at least its own 280 px floor, and
 *     when the columns do not fit the box, no wider than the widest of its
 *     cells needs on one line (measured from a clone, `nowrap`, no max-width —
 *     the same trick check_library_tables uses), so the width it has is a
 *     width its content asked for and never an engine's invention; when the
 *     table does fit, it is the column that took the free width;
 *   * the wrapper — not the page — is what scrolls when the columns do not fit;
 *   * Firefox and Chromium agree column for column (the declared ones to the
 *     pixel; the flexible name column within 2 px, since the two engines
 *     measure a flex row's content a fraction apart);
 *   * rows are rows: drawn, and 20-200 px tall.
 *
 * Needs a live backend serving the app and a library with at least one album:
 *   npm --prefix web run build
 *   MLO_MUSIC_FOLDER=<scratch> python -m uvicorn server.main:app --port 8012
 *   node tools/check_firefox_album.cjs http://127.0.0.1:8012
 * (the vite dev server works too: `cd web && MLO_WEB_PORT=5182
 * MLO_API_TARGET=http://127.0.0.1:8012 npx vite`).
 *
 * Playwright's FIREFOX and CHROMIUM are both required (same resolution as
 * tools/shot.cjs): set PLAYWRIGHT to a module path, or install it. Exit 2 when
 * they are missing. */

let chromium, firefox;
try {
  ({ chromium, firefox } = require(process.env.PLAYWRIGHT || "playwright"));
} catch {
  console.error("[check_firefox_album] Playwright not found — `npm i -D playwright`, " +
    "or point PLAYWRIGHT at an installed module.");
  process.exit(2);
}

const BASE = process.argv.slice(2).find((a) => !a.startsWith("--"))
  || process.env.BASE || "http://127.0.0.1:8011";

/* The three shapes that matter for this table: a desktop window (the table
 * fills, the name takes the slack), a narrow desktop window (the columns no
 * longer fit, so the wrapper must scroll) and a phone (the fold drops the
 * columns it cannot use). */
const VIEWPORTS = [
  { name: "desktop", width: 1440, height: 900 },
  { name: "narrow", width: 860, height: 900 },
  { name: "phone", width: 390, height: 780 },
];

/* The album page's own tracklist wrapper (`overflow-x-auto` around the table)
 * and everything about its geometry that can be read from the page. */
const MEASURE = () => {
  const box = document.querySelector("main .section.overflow-x-auto")
    || document.querySelector(".section.overflow-x-auto");
  const table = box && box.querySelector("table");
  if (!table) return { none: true };
  const r = (el) => {
    const b = el.getBoundingClientRect();
    return { w: +b.width.toFixed(2), h: +b.height.toFixed(2), right: +b.right.toFixed(2) };
  };
  /** What a cell's content needs on ONE line: the cell cloned into a nowrap,
   *  uncapped box (a self-cap must not make a squeezed column look right). */
  const need = (cell) => {
    const clone = cell.cloneNode(true);
    clone.style.cssText = "position:absolute;visibility:hidden;white-space:nowrap;width:auto;max-width:none";
    document.body.appendChild(clone);
    const w = clone.getBoundingClientRect().width;
    clone.remove();
    return +w.toFixed(1);
  };
  const ths = [...table.querySelectorAll("thead th")];
  const rows = [...table.querySelectorAll("tbody tr")];
  const titleIndex = ths.findIndex((th) => /^title$/i.test((th.innerText || "").trim()));
  const titleCells = titleIndex < 0 ? [] : [ths[titleIndex], ...rows.map((tr) => tr.children[titleIndex]).filter(Boolean)];
  const lastTh = ths[ths.length - 1];
  return {
    tableW: r(table).w,
    tableRight: r(table).right,
    lastThRight: r(lastTh).right,
    layout: getComputedStyle(table).tableLayout,
    boxClient: box.clientWidth,
    boxScroll: box.scrollWidth,
    th: ths.map((th) => r(th).w),
    titleIndex,
    titleNeed: titleCells.length ? Math.max(...titleCells.map(need)) : 0,
    rows: rows.length,
    rowH: rows.slice(0, 5).map((tr) => r(tr).h),
    docScroll: document.documentElement.scrollWidth,
    vw: window.innerWidth,
  };
};

const results = [];
const check = (name, pass, detail) => {
  console.log(`  ${pass ? "ok  " : "FAIL"} ${name}${detail ? ` — ${detail}` : ""}`);
  results.push(!!pass);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Wait until the tracklist is drawn AND its geometry stops moving, then read
 *  it. The app mounts after first paint and a full reload can land while a read
 *  is in flight, so a read that hits a destroyed context is retried rather than
 *  reported — two agreeing reads are the contract. */
async function settle(page) {
  const deadline = Date.now() + 30000;
  let last = null;
  while (Date.now() < deadline) {
    await sleep(300);
    let m;
    try {
      m = await page.evaluate(MEASURE);
    } catch (e) {
      if (/Execution context was destroyed|Target closed|navigation/i.test(String(e))) { last = null; continue; }
      throw e;
    }
    if (!m || m.none || !m.rows) { last = null; continue; }
    const key = JSON.stringify(m);
    if (key === last) return m;
    last = key;
  }
  throw new Error("the album tracklist never settled");
}

(async () => {
  const lib = await (await fetch(`${BASE}/api/library`)).json();
  const album = (lib.artists || []).flatMap((a) => a.albums || []).find((al) => al.path);
  if (!album) {
    console.error("[check_firefox_album] the library has no album — point it at a scratch library with audio");
    process.exit(1);
  }
  const url = `${BASE}/album/${encodeURIComponent(album.path)}`;
  const per = {};   // per[engine][viewportName] = MEASURE

  for (const [engine, launcher] of [["chromium", chromium], ["firefox", firefox]]) {
    const browser = await launcher.launch({ headless: process.env.HEADED ? false : true });
    for (const vp of VIEWPORTS) {
      const page = await browser.newPage({ viewport: { width: vp.width, height: vp.height } });
      page.setDefaultTimeout(30000);
      await page.goto(url, { waitUntil: "domcontentloaded" });
      per[engine] = per[engine] || {};
      per[engine][vp.name] = await settle(page);
      await page.close();
    }
    await browser.close();
  }

  for (const vp of VIEWPORTS) {
    for (const engine of ["chromium", "firefox"]) {
      const m = per[engine][vp.name];
      const label = `${engine} ${vp.name} ${vp.width}px`;
      const others = m.th.filter((_, i) => i !== m.titleIndex);
      const titleW = m.titleIndex >= 0 ? m.th[m.titleIndex] : 0;
      const colsSum = m.th.reduce((n, w) => n + w, 0);

      check(`${label} — the tracklist is drawn`, m.rows > 0 && m.layout === "fixed",
        `${m.rows} row(s), table-layout ${m.layout}`);
      // 17,895,482 px is the failure: a table's width is its columns' width.
      check(`${label} — the table is its columns' width, not a 17-million-pixel ribbon`,
        m.tableW <= m.boxClient + 2000,
        `table ${Math.round(m.tableW)} px in a ${m.boxClient} px wrapper`);
      check(`${label} — the columns add up to the table`,
        Math.abs(m.tableW - colsSum) <= 2, `${Math.round(m.tableW)} px vs ${Math.round(colsSum)} px of columns`);
      check(`${label} — the columns end at the table's edge`,
        Math.abs(m.lastThRight - m.tableRight) <= 2 && Math.abs(m.tableW - m.boxScroll) <= 2,
        `last column right ${Math.round(m.lastThRight)}, table right ${Math.round(m.tableRight)}, wrapper scroll ${m.boxScroll}`);
      if (vp.width >= 768) {
        // The fold is `md:`-scoped: above it every drawn column is a real one,
        // and a 0 px column is the bug (the name column, the bitrate, the DR
        // and the corner control all measured 0 px in Gecko).
        check(`${label} — every column is drawn`,
          m.th.every((w) => w > 0), JSON.stringify(m.th.map(Math.round)));
        check(`${label} — the name column holds its floor (280 px)`,
          titleW >= 280, `name column ${Math.round(titleW)} px`);
        if (m.tableW <= m.boxClient + 1) {
          // The table fits: the name column is the one that took the slack.
          const free = m.boxClient - others.reduce((n, w) => n + w, 0);
          check(`${label} — the name column takes the table's free width`,
            Math.abs(titleW - free) <= 3, `${Math.round(titleW)} px in ${free} px of free width`);
        } else {
          // The columns do not fit: the table stops at its own width (never at
          // a name the engine invented) and the wrapper is what scrolls.
          check(`${label} — and is no wider than its own content needs`,
            titleW <= m.titleNeed + 2,
            `name column ${Math.round(titleW)} px, its widest cell needs ${Math.round(m.titleNeed)} px`);
          check(`${label} — the wrapper scrolls for the columns`,
            m.boxScroll > m.boxClient + 1, `${m.boxScroll} vs ${m.boxClient}`);
        }
      }
      check(`${label} — the page itself does not scroll sideways`,
        m.docScroll <= m.vw + 2, `document ${m.docScroll} px in a ${m.vw} px viewport`);
      check(`${label} — rows are rows`,
        m.rowH.every((h) => h >= 20 && h <= 200), JSON.stringify(m.rowH.map(Math.round)));

      if (engine === "firefox") {
        // The issue itself: the same page, the same columns, in both engines.
        const c = per.chromium[vp.name];
        const colsAgree = m.th.every((w, i) => i === m.titleIndex || Math.abs(w - c.th[i]) <= 1);
        check(`${label} — Chromium draws the same declared columns`,
          colsAgree && m.th.length === c.th.length,
          `firefox ${JSON.stringify(m.th.map(Math.round))} vs chromium ${JSON.stringify(c.th.map(Math.round))}`);
        check(`${label} — and the same name column width (within 2 px)`,
          Math.abs(titleW - c.th[c.titleIndex]) <= 2,
          `firefox ${Math.round(titleW)} px vs chromium ${Math.round(c.th[c.titleIndex])} px`);
        check(`${label} — and the same table width`,
          Math.abs(m.tableW - c.tableW) <= 3, `firefox ${Math.round(m.tableW)} px vs chromium ${Math.round(c.tableW)} px`);
      }
    }
  }

  const failed = results.filter((r) => !r).length;
  console.log(`\n${results.length - failed}/${results.length} checks passed` +
    (failed ? ` — ${failed} failed` : ""));
  process.exit(failed ? 1 : 0);
})();
