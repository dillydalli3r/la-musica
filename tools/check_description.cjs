#!/usr/bin/env node
/* The description's SECOND way out (web/src/components/Description.tsx).
 *
 * A stored description can be many screens long — the album page for a release
 * with a full Wikipedia blurb, the artist page for a biography — and the only
 * collapse control was the "Show less" AFTER the text, so putting a long
 * description away meant travelling to the end of it (the owner's ask: "make
 * descriptions easier to close when they're very long (add another close
 * button for descriptions in album + artist pages)"). Expanded, the block now
 * carries a control at its TOP as well, pinned under the app's own top bar for
 * as long as the block is being read (`sticky top-12`, the line `PageHeader`
 * uses), so the way out is in view from the first line to the last. Album and
 * artist pages are the SAME component, so the same story is measured on both.
 *
 * What is measured, against the app's own DOM:
 *
 *   * collapsed, the block has exactly ONE control ("Read more");
 *   * expanded, it has TWO, the first one above the text;
 *   * that first control is hit-testable (the words pass under its row, so the
 *     row must not swallow their clicks) and STILL on screen after scrolling
 *     into the middle of a long description — the whole point of it;
 *   * both controls collapse the block, and the body really shrinks back.
 *
 * Needs a live backend serving the built app (`web/dist`) and a library with a
 * description long enough to be collapsed:
 *   npm --prefix web run build
 *   MLO_MUSIC_FOLDER=<scratch folder> python -m uvicorn server.main:app --host 127.0.0.1 --port 8011
 *   node tools/check_description.cjs http://127.0.0.1:8011
 *
 * Exit codes: 0 pass, 1 a check failed, 2 the environment cannot run it (no
 * Playwright, or no description in this library that is long enough to judge).
 */
let chromium;
try {
  ({ chromium } = require(process.env.PLAYWRIGHT || "playwright"));
} catch {
  console.error("[description] Playwright not found — `npm i -D playwright`, " +
    "or point PLAYWRIGHT at an installed module.");
  process.exit(2);
}

const BASE = process.argv[2] || process.env.BASE || "http://127.0.0.1:8011";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const results = [];
const check = (name, pass, detail) => results.push({ name, pass: !!pass, detail: String(detail ?? "") });

/** The block's own controls, topmost first, with the box each one paints. */
const readControls = (page) => page.evaluate(() => {
  const root = [...document.querySelectorAll("main div")].find((d) =>
    /^mt-2 text-sm/.test(String(d.className)) && d.querySelector("[id]"));
  if (!root) return null;
  const btns = [...root.querySelectorAll("button")].map((b) => {
    const r = b.getBoundingClientRect();
    const mid = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return {
      label: (b.textContent || "").trim(),
      expanded: b.getAttribute("aria-expanded"),
      x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
      hit: !!mid && (mid === b || b.contains(mid)),
      inView: r.top >= 0 && r.bottom <= window.innerHeight,
    };
  }).sort((a, b) => a.y - b.y);
  const body = root.querySelector("[id]");
  const br = body.getBoundingClientRect();
  const main = document.querySelector("main");
  return {
    controls: btns,
    bodyChars: (body.textContent || "").length,
    bodyH: Math.round(br.height),
    bodyBottom: Math.round(br.bottom),
    scrollTop: Math.round(main.scrollTop),
    scrollMax: main.scrollHeight - main.clientHeight,
    viewportH: window.innerHeight,
  };
});

/** Press the control whose painted top is `y` (two controls carry the same
 *  action, so geometry — not the label — is what tells them apart). */
const pressAt = async (page, y) => {
  await page.evaluate((top) => {
    const b = [...document.querySelectorAll("main button")]
      .find((x) => Math.abs(x.getBoundingClientRect().y - top) < 2);
    b?.click();
  }, y);
  await sleep(400);
};

/** The whole story for ONE description. False when this page's description is
 *  short enough that no control is offered (nothing to judge there). */
async function story(page, url, label) {
  await page.goto(url, { waitUntil: "networkidle", timeout: 60000 });
  await sleep(1200);
  if (!(await page.locator('main button:has-text("Read more")').count())) return false;

  // ---- collapsed: ONE control, at the end ---------------------------------
  const start = await readControls(page);
  const collapsed = start.controls.filter((c) => /Read more|Show less/.test(c.label));
  check(`${label}: a long description starts collapsed, with one control`,
    collapsed.length === 1 && collapsed[0].label === "Read more",
    `controls ${JSON.stringify(collapsed.map((c) => c.label))}`);

  // ---- expanded: the second control appears ABOVE the text ----------------
  await page.locator('main button:has-text("Read more")').first().click();
  await sleep(400);
  const expanded = await readControls(page);
  const both = expanded.controls.filter((c) => /Read more|Show less/.test(c.label));
  check(`${label}: expanding it adds a second collapse control`,
    both.length === 2 && both.every((c) => c.label === "Show less"),
    `controls ${JSON.stringify(both.map((c) => `${c.label}@${c.y}`))}`);
  const top = both[0];
  const bottom = both[1];
  check(`${label}: the new one sits at the TOP of the block, the old one at the end`,
    !!top && !!bottom && top.y < expanded.bodyBottom - 40 && bottom.y > top.y,
    `top y=${top?.y} bottom y=${bottom?.y} body bottom=${expanded.bodyBottom}`);
  check(`${label}: it is where the reader is, not off screen`,
    !!top && top.inView, `top ${top?.y}+${top?.h} in ${expanded.viewportH}px viewport`);

  if (expanded.bodyH <= expanded.viewportH * 1.2) {
    console.log(`note: ${label}: this description is ${expanded.bodyH}px tall — shorter than the ` +
      `${expanded.viewportH}px viewport, so the pinned-control half was not applicable`);
    return true;
  }

  // ---- and it is still there in the middle of the text --------------------
  await page.evaluate(() => {
    const main = document.querySelector("main");
    const body = [...document.querySelectorAll("main div")]
      .find((d) => /^mt-2 text-sm/.test(String(d.className)))?.querySelector("[id]");
    const r = body.getBoundingClientRect();
    main.scrollTop += Math.round(r.top + r.height / 2 - main.clientHeight / 2);
  });
  await sleep(400);
  const mid = await readControls(page);
  const midTop = mid.controls.filter((c) => /Show less/.test(c.label))[0];
  check(`${label}: the top control follows the reader down the description`,
    !!midTop && midTop.inView && midTop.hit && mid.bodyBottom > mid.viewportH,
    `scrolled ${mid.scrollTop}/${mid.scrollMax}; control ${midTop?.y}+${midTop?.h} ` +
    `in ${mid.viewportH}px (body ends at ${mid.bodyBottom}), hit=${midTop?.hit}`);
  const under = await page.evaluate((y) => {
    // A point below the control, level with its own column: the words there
    // must still be reachable (the sticky ROW takes no clicks).
    const el = document.elementFromPoint(700, y);
    return el ? (el.tagName + "." + String(el.className).slice(0, 40)) : "none";
  }, (midTop?.y ?? 0) + (midTop?.h ?? 0) + 20);
  check(`${label}: its row does not swallow the words underneath it`,
    !/Show less/.test(under), `a point just under it resolves to ${under}`);

  // ---- the top control collapses the block --------------------------------
  await pressAt(page, midTop?.y ?? 0);
  const back = await readControls(page);
  const backControls = back.controls.filter((c) => /Read more|Show less/.test(c.label));
  check(`${label}: pressing it puts the description away`,
    backControls.length === 1 && backControls[0].label === "Read more" &&
      back.bodyChars < mid.bodyChars && back.bodyH < mid.bodyH,
    `controls ${JSON.stringify(backControls.map((c) => c.label))}, ` +
    `body ${mid.bodyChars}→${back.bodyChars} chars, ${mid.bodyH}→${back.bodyH}px`);

  // ---- and the control at the END still does too --------------------------
  await page.evaluate(() => { document.querySelector("main").scrollTop = 0; });
  await sleep(300);
  await page.locator('main button:has-text("Read more")').first().click();
  await sleep(400);
  const again = await readControls(page);
  const last = again.controls.filter((c) => /Show less/.test(c.label)).pop();
  check(`${label}: the control at the end of the text is still there`,
    !!last, `controls ${JSON.stringify(again.controls.filter((c) => /Read more|Show less/.test(c.label)).map((c) => c.label))}`);
  await pressAt(page, last?.y ?? 0);
  const done = await readControls(page);
  check(`${label}: and it still collapses the description`,
    done.controls.filter((c) => /Read more|Show less/.test(c.label)).length === 1 &&
      done.bodyChars <= again.bodyChars,
    `body ${again.bodyChars}→${done.bodyChars} chars`);
  return true;
}

(async () => {
  const lib = await (await fetch(`${BASE}/api/library`)).json();
  const albums = (lib.artists || []).flatMap((a) => a.albums || []);
  const artists = lib.artists || [];
  if (!albums.length && !artists.length) {
    console.error("[description] skip — this library has no albums or artists to read a description from");
    process.exit(2);
  }
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.on("pageerror", (e) => console.log("PAGE_ERR:", e.message));

  let ran = 0;
  for (const al of albums.slice(0, 12)) {
    if (await story(page, `${BASE}/album/${encodeURIComponent(al.path)}`, "the album page")) {
      ran++;
      break;
    }
  }
  for (const ar of artists.slice(0, 12)) {
    if (await story(page, `${BASE}/artist/${encodeURIComponent(ar.path)}`, "the artist page")) {
      ran++;
      break;
    }
  }
  await browser.close();
  if (!ran) {
    console.error("[description] skip — no description in this library is long enough to collapse");
    process.exit(2);
  }
  for (const r of results) console.log(`${r.pass ? "ok  " : "FAIL"} ${r.name}${r.detail ? ` :: ${r.detail}` : ""}`);
  const bad = results.filter((r) => !r.pass).length;
  console.log(`${results.length - bad}/${results.length} checks pass`);
  process.exit(bad ? 1 : 0);
})();