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
  /* SQUEEZED TEXT — the defect this checker used to be blind to.
   *
   * Everything inside <main> is skipped above (a wide table is allowed to live
   * in its own scroll box), but "inside a scroll box" is exactly where a table
   * can be squeezed instead of scrolled: a table with width:100% inside an
   * overflow-x: auto box can never exceed that box, so the browser wraps a
   * cell's text one character per line rather than overflowing. That shipped
   * to a user's phone — a track title rendered vertically, one letter a row —
   * and no check here noticed, because it never looked inside main.
   *
   * The test is deliberately about WORDS, not pixels: a text box narrower than
   * its own longest word cannot show that word, so the text is not laid out,
   * it is crushed. Measured with a hidden span in the same font, so no magic
   * threshold is involved.
   *
   * The split/token regexes below use a DOUBLE backslash on purpose: this
   * whole program is ONE template literal, and a single backslash-s inside it
   * is the escape for the letter "s". Written with one backslash the page
   * splits on runs of "s" and reports a whole sentence — spaces and all, e.g.
   * 40.57 GB free of 2..., 18 characters — as the "longest word", which is how
   * this was caught: a word has no spaces. Every wrapping paragraph then
   * measured as its own longest word, and pages whose text wraps perfectly
   * well were reported as crushed. Two backslashes reach the page as one,
   * which is what the regex there needs to mean.
   */
  const squeezed = [];
  const hidden = document.createElement("span");
  hidden.style.cssText = "position:absolute;visibility:hidden;white-space:nowrap;left:-9999px;top:0";
  document.body.appendChild(hidden);
  const wordWidth = (el, word) => {
    const cs = getComputedStyle(el);
    hidden.style.font = cs.font;
    hidden.style.letterSpacing = cs.letterSpacing;
    hidden.textContent = word;
    return hidden.getBoundingClientRect().width;
  };
  for (const el of document.querySelectorAll("main td, main th, main span, main div, main p, main h1, main h2, main h3, main a, main button")) {
    const text = (el.textContent || "").trim();
    if (text.length < 8 || el.children.length) continue;
    const r = el.getBoundingClientRect();
    if (r.width > vw) continue;
    const cs = getComputedStyle(el);
    // A screen-reader-only box is text the page deliberately does not SHOW:
    // its 1px width IS the clip — the only rule in the built CSS that sets
    // clip is Tailwind's .sr-only, which sets exactly this rect — and the
    // words it carries exist for a screen reader: an icon-only button's name
    // (Export's "Rescan drives") or the header of a column too narrow to
    // print a label (Export's checkbox and cover columns). Measuring it
    // reports the design as a defect.
    if (cs.clip !== "auto") continue;
    // A cell that collapsed to ZERO width is the same defect at its worst: the
    // text is in the DOM, has lines of height, and has no room at all. Real
    // report: a track title rendered one character per line at 0px wide while
    // the columns beside it kept 50px each. Tested BEFORE the exemptions
    // below, deliberately: a page title at 0px is one word in a box that
    // truncates with an ellipsis and carries a title, so every exemption that
    // excuses a deliberate truncation would also excuse the one state that
    // shows nothing at all.
    if (r.width === 0 && r.height > 0) {
      squeezed.push({ tag: el.tagName.toLowerCase(), cls: (el.className || "").toString().slice(0, 52),
                      w: 0, need: Math.round(wordWidth(el, text.split(/\\s+/)[0] || text)),
                      word: text.slice(0, 18) });
      continue;
    }
    if (r.width < 1) continue;
    // Text that cannot WRAP cannot be crushed into a column: with nowrap the
    // text has one line by construction, there is no column to squeeze it
    // into, and what these boxes carry is a path, a URL or an id — a token
    // with no word boundary to wrap at. The dependency table's Location cell
    // is 264 px of path in a 180 px cell, ellipsised with the whole path in
    // its title: that is the intended behaviour, not this defect.
    const nowrap = cs.whiteSpace === "nowrap" || cs.whiteSpace === "pre";
    if (nowrap && (cs.textOverflow === "ellipsis" || el.hasAttribute("title"))) continue;
    // A box that may break INSIDE a word can still lay that word out, so a box
    // narrower than it is not crushed either. Measured on /settings, where the
    // music folder is a 66-character path in a 282 px box and renders in full,
    // wrapped onto two lines.
    if (cs.wordBreak === "break-all" || cs.overflowWrap === "break-word" ||
        cs.overflowWrap === "anywhere") continue;
    const words = text.split(/\\s+/).filter((w) => w.length > 3);
    if (!words.length) continue;
    const longest = words.reduce((a, b) => (b.length > a.length ? b : a));
    const need = wordWidth(el, longest);
    if (need > 0 && r.width < need * 0.85) {
      squeezed.push({
        tag: el.tagName.toLowerCase(),
        cls: (el.className || "").toString().slice(0, 52),
        w: Math.round(r.width),
        need: Math.round(need),
        word: longest.slice(0, 18),
      });
    }
  }
  hidden.remove();

  const main = document.querySelector("main");
  return {
    squeezed: squeezed.slice(0, 6),
    squeezedCount: squeezed.length,
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
  // A server that has not finished its first run measures nothing here: every
  // route redirects to the setup wizard, which is a form with no table to
  // squeeze and no long value to clip, so the whole run comes back "160/160
  // passed" for pages nobody opened. Measured once, before the first
  // navigation: the backend answers /api/config without a session until a
  // password is claimed, and anything else (a 401 on a claimed server, no such
  // endpoint) decides nothing and is left alone — this stops only on a config
  // that SAYS the wizard is still up.
  try {
    const r = await fetch(`${BASE}/api/config`, { headers: { Accept: "application/json" } });
    if (r.ok) {
      const cfg = await r.json();
      if (cfg && cfg.first_run_done === false) {
        console.error(`[responsive] ${BASE} has not finished its first run — every route ` +
          "redirects to the setup wizard, so the geometry measured here would be the " +
          "wizard's, not the app's. Finish (or skip) setup on that server first, or " +
          'POST {"first_run_done": true} to /api/config, then run this again.');
        process.exit(2);
      }
    }
  } catch { /* no reachable config endpoint: nothing to decide from */ }

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
        // A text box narrower than its longest word is crushed, not laid out
        // (see SQUEEZED TEXT in MEASURE): the failure mode is a title wrapped
        // one character per line, which is what a `w-full` table inside a
        // scroll box does when its columns do not fit.
        check(`${label} — no text is crushed into a column`, m.squeezedCount === 0,
          JSON.stringify(m.squeezed));
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
