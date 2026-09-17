#!/usr/bin/env node
/* Sidebar rail check: collapsing must not move anything vertically, and the
 * brand logo must toggle the rail both ways.
 *
 * The bug this pins: the logo button carried `p-0.5` only while collapsed, so
 * the header row grew 4px taller in the collapsed state and pushed the whole
 * nav down with it (and the logo across by 2px). Nothing in a type check or a
 * unit test can see that — only the rendered geometry can.
 *
 * Needs a live backend serving the built app (`web/dist`):
 *   npm --prefix web run build
 *   python -m uvicorn server.main:app --host 127.0.0.1 --port 8000
 *   node tools/check_sidebar.cjs http://127.0.0.1:8000
 *
 * Playwright is required (same resolution as tools/shot.cjs): set PLAYWRIGHT
 * to a module path, or install it. Exit 2 when it is missing. */

let chromium;
try {
  ({ chromium } = require(process.env.PLAYWRIGHT || "playwright"));
} catch {
  console.error("[sidebar] Playwright not found — `npm i -D playwright`, " +
    "or point PLAYWRIGHT at an installed module.");
  process.exit(2);
}

const BASE = process.argv[2] || process.env.BASE || "http://127.0.0.1:8000";
const results = [];
const check = (name, pass, detail) => results.push({ name, pass: !!pass, detail: String(detail) });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Geometry of the rail: header box, brand logo, first nav row. */
const geometry = (page) => page.evaluate(() => {
  const aside = document.querySelector("aside");
  if (!aside) return null;
  const box = (el) => {
    const b = el.getBoundingClientRect();
    return [b.top, b.left, b.height].map((n) => Math.round(n * 10) / 10).join(",");
  };
  const logo = aside.querySelector("button img");
  return {
    collapsed: aside.offsetWidth < 60,
    width: aside.offsetWidth,
    header: box(aside.firstElementChild),
    logo: box(logo),
    logoTitle: logo.closest("button").title,
    firstNav: box(aside.querySelector("a")),
  };
});

const clickCollapseToggle = (page) => page.evaluate(() => {
  const b = [...document.querySelectorAll("aside button")].find((x) => x.title === "Collapse sidebar" && x.querySelector("svg"));
  if (!b) throw new Error("rail collapse toggle missing");
  b.id = "check-rail-toggle";
}).then(() => page.click("#check-rail-toggle"));

const clickLogo = (page) => page.evaluate(() => {
  document.querySelector("aside button img").closest("button").id = "check-logo";
}).then(() => page.click("#check-logo"));

(async () => {
  let browser;
  try {
    browser = await chromium.launch({ executablePath: process.env.CHROME, headless: true });
    const page = await browser.newContext({ viewport: { width: 1440, height: 900 } }).then((c) => c.newPage());
    page.setDefaultTimeout(15000);
    await page.goto(BASE, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("aside a");

    // Normalise the starting state: the rail remembers its state in
    // localStorage, so drive it to expanded first.
    let g = await geometry(page);
    if (g.collapsed) { await clickLogo(page); await sleep(400); }

    const expanded = await geometry(page);
    check("expanded rail is wide", expanded.width > 150, `width=${expanded.width}`);

    await clickCollapseToggle(page);
    await sleep(400);
    const collapsed = await geometry(page);
    check("collapse toggle collapses the rail", collapsed.collapsed && collapsed.width < 80,
      `width=${collapsed.width} collapsed=${collapsed.collapsed}`);

    // The whole point: collapsing moves nothing vertically (and the brand
    // stays put horizontally too).
    check("header box does not move or resize", expanded.header === collapsed.header,
      `expanded=${expanded.header} collapsed=${collapsed.header}`);
    check("nav rows do not move", expanded.firstNav === collapsed.firstNav,
      `expanded=${expanded.firstNav} collapsed=${collapsed.firstNav}`);
    check("brand logo does not move", expanded.logo === collapsed.logo,
      `expanded=${expanded.logo} collapsed=${collapsed.logo}`);

    // The logo toggles both directions, and says what it will do.
    await clickLogo(page);
    await sleep(400);
    const viaLogo = await geometry(page);
    check("logo expands a collapsed rail",
      !viaLogo.collapsed && viaLogo.logo === expanded.logo,
      `collapsed=${viaLogo.collapsed} logo=${viaLogo.logo} title=${viaLogo.logoTitle}`);
    await clickLogo(page);
    await sleep(400);
    const back = await geometry(page);
    check("logo collapses an expanded rail",
      back.collapsed && back.logo === expanded.logo && back.firstNav === expanded.firstNav,
      `collapsed=${back.collapsed} logo=${back.logo} nav=${back.firstNav}`);

    // Leave the user's rail as we found it (expanded).
    if (back.collapsed) { await clickLogo(page); await sleep(400); }
    const final = await geometry(page);
    check("rail restored to expanded", !final.collapsed, `collapsed=${final.collapsed}`);
  } catch (e) {
    check("check script ran to completion", false, String(e).split("\n")[0]);
  } finally {
    if (browser) await browser.close().catch(() => {});
  }
  for (const r of results) console.log(`${r.pass ? "ok  " : "FAIL"} ${r.name} :: ${r.detail}`);
  const bad = results.filter((r) => !r.pass).length;
  console.log(`\n${results.length - bad}/${results.length} checks pass`);
  process.exit(bad ? 1 : 0);
})();
