#!/usr/bin/env node
/* Fullscreen player ambience — the STRAIGHT LINES (issue #73).
 *
 * The owner's report: straight "lines" appear to approach from the corners of
 * the fullscreen player's background. Nothing in the layer stack is a line, so
 * the lines have to be found in the pixels, and two layers were DRAWING them —
 * every measurement below is on the shipped tree, Chromium, 1440x900, DPR 1,
 * the cover stubbed to white (the worst case for any 8-bit step), the ambience
 * isolated (`visibility: hidden` on the player's other children) and every CSS
 * clock paused so two reads of one state are pixel-identical:
 *
 *  1) `.amb-sweep` is a CONIC gradient, and a conic gradient's isophotes are
 *     straight rays from its centre. Its value ramp is 8-bit, so the fan is a
 *     ladder of straight 1-level ribs — 1 every 4-13 px along a circle around
 *     the frame centre — and the angle SINGULARITY in the middle (where all
 *     360 degrees land on a couple of pixels) is the hardest edge anywhere in
 *     the ambience: 21.9 levels on a white cover, 4.9 in the composite. Every
 *     other layer of the stack measures at most 2.1 levels, so the fan is
 *     provably this layer's (hiding it removes the only composite step that no
 *     other single-layer removal touches). It is GEOMETRY, not banding, which
 *     is why `.amb-grain`'s dither cannot touch it: the ribs are a real radial
 *     ladder and the middle is a point. tools/check_fullscreen_player.cjs pins
 *     the same layer family's canvas arithmetic; this file pins the pixels.
 *  2) `.amb-cover`'s own edge FADES — `blur-3xl` samples nothing past the
 *     element's box, so the last ~64 px inside every edge ramp from 34 % to
 *     zero, and that ramp quantizes into a ladder of straight 1-level ribs
 *     parallel to each frame edge: 21 adjacent-pixel jumps in the first 160 px
 *     of the left edge, i.e. 1440 px-long straight lines at every edge, and
 *     they meet in the corners.
 *  3) `.amb-vignette` (the first suspicion: it is painted ABOVE the grain) is
 *     NOT a source of these: rendered alone on all three cover polarities it
 *     produces no step at all (max 0.0 levels) — its banding is below the
 *     8-bit floor. The canvas strip is not a source either: at rest its ink is
 *     confined to the baseline rows inside the grid's own columns (asserted
 *     below), which is the discrimination the report needed between the bars
 *     and the background behind them.
 *
 * The check therefore measures, for each of the two line-drawing layers, the
 * pixels it draws — and, in the composite, the hardest step the picture still
 * contains. Run it against a scratch server:
 *   node tools/check_viz_corners.cjs http://127.0.0.1:8014
 * Screenshots land in .pi/shots-viz-corners/. */

let chromium;
try {
  ({ chromium } = require(process.env.PLAYWRIGHT || "playwright"));
} catch {
  console.error('playwright not found — set PLAYWRIGHT (e.g. PLAYWRIGHT=F:/Coding/GitHub/la-musica/web/node_modules/playwright)');
  process.exit(2);
}

const fs = require("fs");
const path = require("path");

const BASE = process.argv[2] || process.env.BASE || "http://127.0.0.1:8011";
const SHOTS = path.join(".pi", "shots-viz-corners");
const results = [];
const check = (name, pass, detail) => results.push({ name, pass: !!pass, detail: String(detail) });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const VIEW = { width: 1440, height: 900 };
/** One screenshot as raw luminance, plus the step/edge/band readings the
 *  assertions are made of. All of them are PER PIXEL of the captured frame:
 *  a CSS value says nothing about what a gradient quantizes to. */
const readFrame = (page, b64) => page.evaluate(async ({ b64 }) => {
  const img = new Image();
  img.src = "data:image/png;base64," + b64;
  await img.decode();
  const c = document.createElement("canvas");
  c.width = img.width; c.height = img.height;
  const g = c.getContext("2d", { willReadFrequently: true });
  g.drawImage(img, 0, 0);
  const d = g.getImageData(0, 0, c.width, c.height).data;
  const W = c.width, H = c.height;
  const lum = new Float32Array(W * H);
  for (let i = 0, p = 0; i < d.length; i += 4, p++) lum[p] = 0.2126 * d[i] + 0.7152 * d[i + 1] + 0.0722 * d[i + 2];
  /** The hardest adjacent-pixel step in a box: a straight line IS such a step,
   *  and a soft gradient of these sizes cannot produce one above 1 level
   *  (measured: every ambience layer except the sweep and the cover's edge
   *  ramp measures <= 2 levels summed over two pixels, and the layers that own
   *  no line at all measure <= 1). */
  const steps = (x0, y0, x1, y1, atLeast) => {
    let max = 0, n = 0, where = null;
    const one = (x, y, v) => { if (v > max) { max = v; where = [x, y]; } if (v >= atLeast) n++; };
    for (let y = y0; y < y1; y++) for (let x = x0; x < x1 - 1; x++) one(x, y, Math.abs(lum[y * W + x + 1] - lum[y * W + x]));
    for (let x = x0; x < x1; x++) for (let y = y0; y < y1 - 1; y++) one(x, y, Math.abs(lum[(y + 1) * W + x] - lum[y * W + x]));
    return { max: +max.toFixed(2), n, where };
  };
  /** The frame's left edge as a column profile (mean of 200 rows): a ladder of
   *  straight lines parallel to the edge shows as repeated 1-level JUMPS down
   *  the profile, which is exactly what the eye reads as lines. */
  const edge = (() => {
    const prof = [];
    for (let x = 0; x < 160; x++) { let s = 0; for (let y = 350; y < 550; y++) s += lum[y * W + x]; prof.push(s / 200); }
    let jumps = 0, maxJump = 0;
    for (let x = 1; x < 160; x++) { const v = prof[x] - prof[x - 1]; if (Math.abs(v) >= 0.75) jumps++; if (Math.abs(v) > maxJump) maxJump = Math.abs(v); }
    return { jumps, maxJump: +maxJump.toFixed(2) };
  })();
  /** The conic band's own RANGE around the frame centre at R=300: the sweep is
   *  a designed layer, so a "fix" that flattens it to nothing must fail too. */
  const band = (() => {
    const at = (x, y) => lum[Math.round(y) * W + Math.round(x)] || 0;
    let lo = 1e9, hi = -1e9;
    for (let k = 0; k < 720; k++) {
      const t = (k * Math.PI * 2) / 720;
      const v = at(W / 2 + 300 * Math.cos(t), H / 2 + 300 * Math.sin(t));
      lo = Math.min(lo, v); hi = Math.max(hi, v);
    }
    return +(hi - lo).toFixed(1);
  })();
  return {
    whole: steps(0, 0, W, H, 2),
    centre: steps(W / 2 - 64, H / 2 - 64, W / 2 + 64, H / 2 + 64, 3),
    left: edge, band,
  };
}, { b64 });

/** Serve a solid WHITE cover and its colour — the polarity every 8-bit step in
 *  the ambience is largest on, so a threshold that holds here holds for every
 *  cover the app can be given. */
const stubWhiteCover = (page) => page.route(/\/api\/cover/, (route) => {
  const color = new URL(route.request().url()).searchParams.get("color");
  if (color) return route.fulfill({ json: { color: "#ffffff", album: "" } });
  return route.fulfill({
    status: 200,
    headers: { "Content-Type": "image/svg+xml" },
    body: '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="600"><rect width="600" height="600" fill="#ffffff"/></svg>',
  });
});

/** The ambience ALONE: the player's other children hidden, every CSS clock
 *  paused, and `--amb` pinned (a beat-driven value would move the glow and the
 *  orbs between two reads and the measurement would be of the motion). */
const AMBIENCE_ONLY = `
  div.fixed.inset-0.z-50 > * { visibility: hidden !important; }
  div.fixed.inset-0.z-50 > div[aria-hidden] { visibility: visible !important; }
  div.fixed.inset-0.z-50 > div[aria-hidden] { --amb: 0.9 !important; --amb-pulse: 0 !important; }
`;
const ONLY_SWEEP = ".amb-cover,.amb-orbs,.amb-glow,.amb-bloom,.amb-grain,.amb-vignette{display:none!important}";
const ONLY_COVER = ".amb-sweep,.amb-orbs,.amb-glow,.amb-bloom,.amb-grain,.amb-vignette{display:none!important}";

/** What the canvas strip holds at rest, read from its own backing store (the
 *  canvas is where the bar grid lives; the reported lines could equally have
 *  been residual ink from an earlier frame). */
const stripInk = (page) => page.evaluate(() => {
  const c = document.querySelector("div.fixed.inset-0.z-50 canvas");
  if (!c) return null;
  const d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
  const W = c.width, H = c.height;
  const rows = new Array(H).fill(0), cols = new Array(W).fill(0);
  let ink = 0;
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
    if (d[(y * W + x) * 4 + 3] > 8) { ink++; rows[y]++; cols[x]++; }
  }
  if (!ink) return { W, H, ink: 0, top: -1, spanW: 0, first: -1, last: -1 };
  const first = rows.findIndex((v) => v > 0);
  const last = H - 1 - [...rows].reverse().findIndex((v) => v > 0);
  const firstCol = cols.findIndex((v) => v > 0);
  const lastCol = W - 1 - [...cols].reverse().findIndex((v) => v > 0);
  return { W, H, ink, top: first, bottom: last, firstCol, lastCol };
});

(async () => {
  let browser;
  const errs = [];
  try {
    fs.mkdirSync(SHOTS, { recursive: true });
    browser = await chromium.launch({ executablePath: process.env.CHROME, headless: true, args: ["--autoplay-policy=no-user-gesture-required"] });
    const page = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 1, serviceWorkers: "block" }).then((c) => c.newPage());
    page.setDefaultTimeout(20000);
    page.on("pageerror", (e) => errs.push(e.message));
    await stubWhiteCover(page);

    const lib = await (await fetch(`${BASE}/api/library`)).json();
    let album = null;
    for (const a of lib.artists || []) {
      for (const al of a.albums || []) if ((al.tracks || []).length) { album = al; break; }
      if (album) break;
    }
    check("the fixture has an album to open the fullscreen player on", !!album, JSON.stringify(album && album.path));
    if (!album) throw new Error("no playable album in the library");

    await page.goto(`${BASE}/album/${encodeURIComponent(album.path)}`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector('tr[title="Click to play"]', { timeout: 20000 });
    await page.locator('tr[title="Click to play"]').first().locator("td").first().click();
    await sleep(1200);
    await page.keyboard.press("f");
    await page.waitForSelector("div.fixed.inset-0.z-50", { timeout: 15000 });
    await sleep(1200);
    // Pause (the ambience's --amb settles at its floor) and freeze every CSS
    // clock: the geometry under measurement must not move between samples.
    await page.keyboard.press("Space");
    await sleep(400);
    await page.addStyleTag({ content: "*, *::before, *::after { animation-play-state: paused !important; }" });
    await sleep(600);
    const ambience = await page.addStyleTag({ content: AMBIENCE_ONLY });
    await sleep(600);

    const clip = { x: 0, y: 0, width: VIEW.width, height: VIEW.height };
    const shot = async (name) => {
      const b64 = (await page.screenshot({ clip })).toString("base64");
      if (name) fs.writeFileSync(path.join(SHOTS, `${name}.png`), Buffer.from(b64, "base64"));
      return b64;
    };
    /** `display:none` a set of layers for one measurement, then put them back. */
    const withLayers = async (css, fn) => {
      const tag = css ? await page.addStyleTag({ content: css }) : null;
      await sleep(400);
      try { return await fn(); } finally {
        if (tag) await tag.evaluate((el) => el.remove());
        await sleep(150);
      }
    };

    // ---- 1) the conic sweep draws no hard edge --------------------------
    // Rendered ALONE, because this is the layer's own geometry: on top of the
    // colour field the composite's steps are the field's, but this fan is the
    // one structure no other layer of the stack emits.
    const sweep = await withLayers(ONLY_SWEEP, async () => readFrame(page, await shot("sweep-alone")));
    check("the conic sweep paints its band (the layer is still a layer)",
      sweep.band >= 20, `band range at R=300 around the frame centre = ${sweep.band} levels (need >= 20; measured 39 with the fix, 40 without)`);
    check("the conic sweep draws no hard edge anywhere in the frame",
      sweep.whole.n === 0 && sweep.whole.max <= 1.5,
      `steps >= 2 levels: ${sweep.whole.n} (need 0), hardest step ${sweep.whole.max} levels at ${JSON.stringify(sweep.whole.where)} `
      + `(unfixed: 1146 steps, 21.9 levels at the conic's centre, which is where the fan's rays and its singularity are)`);
    check("the conic centre is not a glint",
      sweep.centre.n === 0 && sweep.centre.max <= 1.5,
      `within 64 px of the centre: ${sweep.centre.n} steps >= 2 levels (need 0), hardest ${sweep.centre.max} (unfixed: 272 steps, 21.9 levels)`);

    // ---- 2) the blurred cover leaves no edge ladder ----------------------
    const cover = await withLayers(ONLY_COVER, async () => readFrame(page, await shot("cover-alone")));
    check("the blurred cover's edge fades off the frame, not into a ladder of lines",
      cover.left.jumps <= 2,
      `1-level jumps down the left edge's first 160 px: ${cover.left.jumps} (need <= 2; worst jump ${cover.left.maxJump} levels) `
      + `— unfixed: 21 jumps, i.e. 1440 px-long straight lines at every edge`);

    // ---- 3) the composite has no hard structure at the frame centre ------
    // With `.amb-grain` off: the grain is the DITHER (see its comment in
    // index.css), its own noise is 0-7 levels over a bright field, and this
    // assertion is about the STRUCTURE the picture holds, not the noise the
    // dither adds. The centre is where the conic's singularity lands.
    const composite = await withLayers(".amb-grain{display:none!important}", async () => readFrame(page, await shot("composite-nograin")));
    check("the whole ambience holds no step of 3 levels or more near the frame centre",
      composite.centre.n === 0,
      `within 64 px of the centre: ${composite.centre.n} steps >= 3 levels (need 0), hardest ${composite.centre.max} `
      + `(unfixed: 11 steps, 4.93 levels, all of them on the conic's centre pixel)`);
    const shipped = await readFrame(page, await shot("composite"));
    check("the whole ambience's edge holds no line ladder",
      shipped.left.jumps <= 4,
      `1-level jumps down the left edge's first 160 px in the SHIPPED composite (grain on): ${shipped.left.jumps} (need <= 4; unfixed: 15-18)`);

    // ---- 4) the canvas strip is not the source ---------------------------
    await ambience.evaluate((el) => el.remove());
    await sleep(400);
    // at rest the strip eases onto its baseline and stops repainting; wait for
    // two identical reads rather than a duration (an idle strip repaints on a
    // 100 ms timer, so a wall-clock sleep can catch the last easing frame)
    let ink = await stripInk(page);
    for (let i = 0; i < 40; i++) {
      await sleep(150);
      const next = await stripInk(page);
      if (ink && next && next.ink === ink.ink && next.top === ink.top) { ink = next; break; }
      ink = next;
    }
    check("the visualizer strip's ink sits inside its own baseline rows at rest",
      !!ink && ink.ink > 0 && ink.top >= ink.H - 4,
      ink ? `${ink.ink} ink pixels, rows ${ink.top}..${ink.bottom} of ${ink.H}, columns ${ink.firstCol}..${ink.lastCol} of ${ink.W}` : "the strip's canvas is not in the DOM");
    check("the strip's ink spans the strip exactly (bar 0 at column 0, the last bar at the last column)",
      !!ink && ink.firstCol === 0 && ink.lastCol === ink.W - 1,
      ink ? `columns ${ink.firstCol}..${ink.lastCol} of ${ink.W}` : "no canvas");
    await page.screenshot({ path: path.join(SHOTS, "strip.png") }).catch(() => {});

    check("no uncaught page errors", errs.length === 0, errs.join(" | "));
  } catch (e) {
    check("check script ran to completion", false, String(e).split("\n")[0]);
  } finally {
    if (browser) await browser.close().catch(() => {});
  }
  for (const r of results) console.log(`  ${r.pass ? "ok  " : "FAIL"} ${r.name}${r.pass ? "" : "  :: " + r.detail}`);
  const bad = results.filter((r) => !r.pass).length;
  console.log(`\n${results.length - bad}/${results.length} checks pass  (screenshots in ${SHOTS}/)`);
  process.exit(bad ? 1 : 0);
})();
