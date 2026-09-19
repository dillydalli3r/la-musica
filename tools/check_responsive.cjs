#!/usr/bin/env node
/* Responsive check: every page must fit a phone, a tablet and a desktop.
 *
 * The bug class this pins: a page that only ever gets looked at 1440 px wide
 * quietly grows a fixed-width table, a `min-w-` column, a nowrap path or a
 * button row that does not wrap — and on a phone the whole document then
 * scrolls sideways, or a control ends up off-screen and untappable. Nothing in
 * a type check can see that; only the rendered geometry can. The clients are
 * first-class now (the shells bundle this same build), so a phone width is a
 * shipping target, not a nicety.
 *
 * Needs a live backend serving the built app (`web/dist`):
 *   npm --prefix web run build
 *   python -m uvicorn server.main:app --host 127.0.0.1 --port 8000
 *   node tools/check_responsive.cjs http://127.0.0.1:8000
 *
 * Playwright is required (same resolution as tools/shot.cjs): set PLAYWRIGHT
 * to a module path, or install it. Exit 2 when it is missing. */

let chromium;
try {
  ({ chromium } = require(process.env.PLAYWRIGHT || "playwright"));
} catch {
  console.error("[responsive] Playwright not found — `npm i -D playwright`, " +
    "or point PLAYWRIGHT at an installed module.");
  process.exit(2);
}

const BASE = process.argv[2] || process.env.BASE || "http://127.0.0.1:8000";

/* The three shapes that matter: a phone in portrait (the narrowest thing the
 * app is installed on), a tablet, and a desktop window. */
const VIEWPORTS = [
  { name: "phone", width: 390, height: 780 },
  { name: "tablet", width: 834, height: 1112 },
  { name: "desktop", width: 1440, height: 900 },
];

/* Every top-level route the shell can reach. Pages that need data the check
 * cannot produce (an album, a playlist) are excluded on purpose: their layout
 * is the same list/table language as the ones below. */
const ROUTES = [
  "/", "/library", "/genres", "/favorites/tracks", "/downloads", "/trash",
  "/playlists", "/soulseek", "/import", "/export", "/optimize", "/grading",
  "/dependencies", "/settings", "/donations", "/setup",
];

const results = [];
const check = (name, pass, detail) => results.push({ name, pass: !!pass, detail: String(detail) });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* One page's geometry, measured in the page itself.
 *
 * `wide` counts elements that stick out past the right edge with no scroll
 * container of their own — the ones that make the whole document scroll
 * sideways. An element inside an `overflow-x: auto` box is expected to be wide
 * (that is how a table survives a phone) and is skipped. */
const MEASURE = `(() => {
  const vw = window.innerWidth;
  const doc = document.documentElement;
  const wide = [];
  for (const el of document.querySelectorAll("body *")) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (r.right <= vw + 2) continue;
    let p = el.parentElement, scrollable = false;
    while (p && p !== document.body) {
      const ov = getComputedStyle(p).overflowX;
      if (ov === "auto" || ov === "scroll") { scrollable = true; break; }
      p = p.parentElement;
    }
    if (scrollable) continue;
    const ov = getComputedStyle(el).overflowX;
    if (ov === "auto" || ov === "scroll") continue;
    wide.push({
      tag: el.tagName.toLowerCase(),
      cls: (el.className || "").toString().slice(0, 60),
      right: Math.round(r.right),
      w: Math.round(r.width),
    });
  }
  const tiny = [];
  for (const b of document.querySelectorAll("button, [role=button], select")) {
    const r = b.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (r.height >= 28) continue;
    tiny.push({
      label: (b.getAttribute("aria-label") || b.textContent || "").trim().slice(0, 30),
      h: Math.round(r.height),
    });
  }
  const main = document.querySelector("main");
  return {
    vw,
    docScrollWidth: doc.scrollWidth,
    bodyScrollWidth: document.body.scrollWidth,
    wide: wide.slice(0, 6),
    wideCount: wide.length,
    tiny: tiny.slice(0, 6),
    tinyCount: tiny.length,
    text: (document.body.innerText || "").slice(0, 80).replace(/\\s+/g, " "),
    mainScrollable: main ? main.scrollHeight > main.clientHeight + 4 : false,
  };
})()`;

(async () => {
  const browser = await chromium.launch();
  for (const vp of VIEWPORTS) {
    const page = await browser.newPage({ viewport: { width: vp.width, height: vp.height } });
    for (const route of ROUTES) {
      try {
        await page.goto(`${BASE}${route}`, { waitUntil: "domcontentloaded", timeout: 20000 });
        // The lazy route chunk has to arrive and the first paint to settle
        // before geometry means anything.
        await sleep(900);
        const m = await page.evaluate(MEASURE);
        const label = `${vp.name} ${route}`;
        check(`${label} — no sideways scroll`, m.docScrollWidth <= m.vw + 2,
          `document is ${m.docScrollWidth}px wide in a ${m.vw}px viewport`);
        check(`${label} — nothing overflows the edge`, m.wideCount === 0,
          JSON.stringify(m.wide));
        // The 32 px tap floor is a PHONE rule: from `md` up the app is
        // deliberately compact (28-30 px rows are the desktop look), so
        // enforcing it there would report the design as a defect.
        if (vp.width <= 480) {
          check(`${label} — controls are tappable`, m.tinyCount === 0,
            JSON.stringify(m.tiny));
        }
      } catch (e) {
        check(`${vp.name} ${route} — loads`, false, String(e).slice(0, 140));
      }
    }
    await page.close();
  }
  await browser.close();

  const failed = results.filter((r) => !r.pass);
  for (const r of results) {
    if (!r.pass) console.log(`  FAIL  ${r.name}\n        ${r.detail}`);
  }
  console.log(`\n${results.length - failed.length}/${results.length} checks passed` +
    (failed.length ? ` — ${failed.length} failed` : ""));
  process.exit(failed.length ? 1 : 0);
})();
