#!/usr/bin/env node
/* Sidebar rail check: collapsing must not move anything vertically, the
 * brand logo must toggle the rail both ways, and the rail must show exactly
 * ONE horizontal rule above the nav in both states.
 *
 * The bugs this pins:
 *  - the logo button carried `p-0.5` only while collapsed, so the header row
 *    grew 4px taller in the collapsed state and pushed the whole nav down with
 *    it (and the logo across by 2px);
 *  - the collapsed section label was replaced by a 9px hairline, 16px shorter
 *    than the label's own box, so collapsing yanked every icon up;
 *  - that same hairline landed 13px under the header's border-b, stacking TWO
 *    rules between the brand and the first nav row where expanded shows one.
 * Nothing in a type check or a unit test can see any of that — only the
 * rendered geometry can, which is why the rules are counted from the border
 * boxes rather than judged from a screenshot.
 *
 * Needs a live backend serving the built app (`web/dist`):
 *   npm --prefix web run build
 *   python -m uvicorn server.main:app --host 127.0.0.1 --port 8000
 *   node tools/check_sidebar.cjs http://127.0.0.1:8000
 *
 * Playwright is required (same resolution as tools/shot.cjs): set PLAYWRIGHT
 * to a module path, or install it. Exit 2 when it is missing.
 * A first-run install redirects to /setup, which has no rail: finish or skip
 * setup once against that server before running this.
 *
 *   PLAYWRIGHT=web/node_modules/playwright node tools/check_sidebar.cjs ... */

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

/** Geometry of the rail: header box, brand logo, first nav row, and the
 *  horizontal rules drawn between the header's top and that first row. */
const geometry = (page) => page.evaluate(() => {
  const aside = document.querySelector("aside");
  if (!aside) return null;
  const box = (el) => {
    const b = el.getBoundingClientRect();
    return [b.top, b.left, b.height].map((n) => Math.round(n * 10) / 10).join(",");
  };
  const logo = aside.querySelector("button img");
  const firstNav = aside.querySelector("a");
  const navs = [...aside.querySelectorAll("a.nav-link")];

  /** The y of every border edge that actually PAINTS (a width, a style and a
   *  non-transparent colour) in the band `from`..`to`. Nested boxes drawing
   *  the same edge count once. The nav links' `border-transparent` does not
   *  count — that is the whole point of reading the colour, not the width. */
  const rulesBetween = (from, to) => {
    const alpha = (color) => {
      const n = String(color).match(/[\d.]+/g) || [];
      return n.length === 4 ? parseFloat(n[3]) : 1;
    };
    const ys = [];
    for (const el of aside.querySelectorAll("*")) {
      const b = el.getBoundingClientRect();
      if (b.width < 4 || b.height === 0) continue;
      const cs = getComputedStyle(el);
      const edges = [
        [cs.borderTopWidth, cs.borderTopStyle, cs.borderTopColor, b.top],
        [cs.borderBottomWidth, cs.borderBottomStyle, cs.borderBottomColor,
          b.bottom - parseFloat(cs.borderBottomWidth || 0)],
      ];
      for (const [w, style, color, y] of edges) {
        const px = parseFloat(w || 0);
        if (!px || style === "none" || alpha(color) === 0) continue;
        if (y < from - 0.5 || y > to) continue;
        ys.push(Math.round(y));
      }
    }
    return ys.sort((a, b) => a - b).filter((y, i, all) => i === 0 || y - all[i - 1] > 1);
  };

  const headerTop = aside.firstElementChild.getBoundingClientRect().top;
  const navTop = firstNav.getBoundingClientRect().top;
  const navBottom = navs.length ? navs[navs.length - 1].getBoundingClientRect().bottom : navTop;
  return {
    collapsed: aside.offsetWidth < 60,
    width: aside.offsetWidth,
    // The collapsed name is only hidden, never unwrapped: a sideways overflow
    // here would mean the rail has grown a horizontal scrollbar.
    overflowX: aside.scrollWidth - aside.clientWidth,
    header: box(aside.firstElementChild),
    logo: box(logo),
    logoTitle: logo.closest("button").title,
    firstNav: box(firstNav),
    rulesAboveNav: rulesBetween(headerTop, navTop),
    rulesBetweenGroups: rulesBetween(navTop, navBottom),
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

    // Dividers: the header's own rule must be the ONLY one above the nav in
    // both states — collapsed used to draw the first group's hairline 13px
    // under it — and the later groups must still be split when collapsed.
    check("expanded rail draws one rule above the nav", expanded.rulesAboveNav.length === 1,
      `y=${expanded.rulesAboveNav.join("/")}`);
    check("collapsed rail draws one rule above the nav", collapsed.rulesAboveNav.length === 1,
      `y=${collapsed.rulesAboveNav.join("/")}`);
    check("collapsed rail still splits its later groups", collapsed.rulesBetweenGroups.length > 0,
      `y=${collapsed.rulesBetweenGroups.join("/")}`);
    check("collapsed rail does not overflow sideways", collapsed.overflowX <= 0,
      `overflowX=${collapsed.overflowX}`);

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
