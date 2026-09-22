#!/usr/bin/env node
/* Fullscreen player metadata legibility: the title, the format line ("16/44.1")
 * and the album/artist lines must all be readable against the field the
 * ambience paints, on BOTH polarities — the failure the owner reported was a
 * mid-grey cover where the title's white read fine but the secondary lines
 * (zinc-500 / zinc-400) sat barely above 2:1 on that same field.
 *
 * The metadata block now takes its colours from the SAME `ink` decision the
 * lyric pane uses (`INK_ON_DARK` / `INK_ON_LIGHT`), so this checks the claim
 * the way a reader sees it: the computed colour of each tier against the
 * ACTUAL rendered field, measured from a screenshot of the pixels beside the
 * text (not from a CSS variable — the field is a stack of blurred cover,
 * colour washes and glow layers, so only the screenshot is the truth).
 *
 * The cover is stubbed: a mid-grey one (#808080, the reported hard case, where
 * the polarity rule lands nearest its flip) and a light one (#dcdcdc). Runs
 * against a live backend serving the built app:
 *   npm --prefix web run build
 *   python -m uvicorn server.main:app --host 127.0.0.1 --port 8000
 *   node tools/check_np_metadata_contrast.cjs http://127.0.0.1:8000
 *
 * Screenshots land in .pi/shots-np-metadata/. Exit 2 when Playwright is
 * missing. */

let chromium;
try {
  ({ chromium } = require(process.env.PLAYWRIGHT || "playwright"));
} catch {
  console.error("[np] Playwright not found — `npm i -D playwright`, " +
    "or point PLAYWRIGHT at an installed module.");
  process.exit(2);
}

const fs = require("fs");
const path = require("path");
const BASE = process.argv[2] || process.env.BASE || "http://127.0.0.1:8000";
const SHOTS = path.join(".pi", "shots-np-metadata");
// WCAG AA: 4.5:1 for body text, 3:1 for large (the 24px bold title).
const AA_SMALL = 4.5;
const AA_LARGE = 3.0;
const results = [];
const check = (name, pass, detail) => results.push({ name, pass: !!pass, detail: String(detail) });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const channel = (c) => {
  const v = c / 255;
  return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
};
const relLum = ([r, g, b]) => 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
const contrast = (a, b) => {
  const [hi, lo] = [relLum(a), relLum(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
};
const parseRgb = (css) => css.match(/\d+(\.\d+)?/g).slice(0, 3).map(Number);

/** The four tiers of the metadata block, with their boxes. */
const readTiers = (page) => page.evaluate(() => {
  const overlay = document.querySelector("div.fixed.inset-0.z-50");
  // Structural anchor: the title is the only text-2xl inside the overlay; its
  // row (h-8, title + format line) sits in the block with the album and
  // artist rows.
  const titleEl = overlay && overlay.querySelector("div.text-2xl");
  const titleRow = titleEl && titleEl.parentElement;
  const block = titleRow && titleRow.parentElement;
  if (!block || block.children.length < 3) return null;
  const rows = [...block.children];
  const title = rows[0]?.querySelector("div");
  const tech = rows[0]?.querySelector("span.font-mono");
  const album = rows[1]?.firstElementChild;
  const artist = rows[2]?.firstElementChild;
  const box = block.getBoundingClientRect();
  const read = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return {
      text: (el.textContent || "").trim(),
      color: getComputedStyle(el).color,
      shadow: getComputedStyle(el).textShadow,
      y: Math.round(r.top + r.height / 2),
      x: Math.round(r.left),
      h: Math.max(2, Math.round(r.height)),
    };
  };
  return {
    field: { left: Math.round(box.left), right: Math.round(box.right), top: Math.round(box.top), bottom: Math.round(box.bottom) },
    title: read(title), tech: read(tech), album: read(album), artist: read(artist),
  };
});

/** The colour the glyphs actually contrast against: the pixels INSIDE the
 *  block at the text's own height, in the end of the row the centred text does
 *  not reach. That is the veil the ink sits on — the gutters beside the block
 *  are the bare ambience and say nothing about the tiers' legibility. */
async function fieldColor(page, tiers, side) {
  const y = tiers.title.y - 6;
  const clip = side === "left"
    ? { x: tiers.field.left + 6, y, width: 36, height: 12 }
    : { x: tiers.field.right - 42, y, width: 36, height: 12 };
  const png = (await page.screenshot({ clip })).toString("base64");
  return page.evaluate(async (b64) => {
    const img = new Image();
    img.src = "data:image/png;base64," + b64;
    await img.decode();
    const c = document.createElement("canvas");
    c.width = img.width; c.height = img.height;
    const g = c.getContext("2d");
    g.drawImage(img, 0, 0);
    const d = g.getImageData(0, 0, c.width, c.height).data;
    let r = 0, gg = 0, b = 0, n = 0;
    for (let i = 0; i < d.length; i += 4) { r += d[i]; gg += d[i + 1]; b += d[i + 2]; n++; }
    return [r / n, gg / n, b / n];
  }, png);
}

/** Serve a solid-colour cover (SVG — no encoder needed) and its colour. */
async function stubCover(page, hex) {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="600" height="600"><rect width="600" height="600" fill="${hex}"/></svg>`;
  await page.route(/\/api\/cover/, (route) => {
    const color = new URL(route.request().url()).searchParams.get("color");
    if (color) return route.fulfill({ json: { color: hex, album: "" } });
    return route.fulfill({ status: 200, headers: { "Content-Type": "image/svg+xml" }, body: svg });
  });
}

async function measure(page, hex, label, album) {
  let step = "cover stub";
  try {
    await stubCover(page, hex);
    step = "album page";
    await page.goto(`${BASE}/album/${encodeURIComponent(album)}`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector('tr[title="Click to play"]');
    step = "first play";
    await page.locator('tr[title="Click to play"]').first().locator("td").first().click();
    await sleep(1200);
    step = "fullscreen";
    // Open the fullscreen player the way a user does (the app-wide shortcut is
    // layout-independent: the bar's own button is a phone-only control).
    await page.keyboard.press("f");
    await page.waitForSelector("div.fixed.inset-0.z-50 div.text-2xl", { timeout: 15000 });
    step = "settle";
    // Let the ambience settle and the glow stop moving: a pulse would otherwise
    // make the field (and so the measurement) drift between samples.
    await page.addStyleTag({ content: "*, *::before, *::after { animation-play-state: paused !important; }" });
    await sleep(1500);
    step = "read";
  } catch (e) {
    const where = await page.evaluate(() => location.href).catch(() => "?");
    await page.screenshot({ path: path.join(SHOTS, `${label}-FAILED.png`) }).catch(() => {});
    const text = await page.evaluate(() => document.body.innerText).catch(() => "");
    check(`${label}: reached the fullscreen player (failed at: ${step})`, false,
      `${String(e).split("\n")[0]} @ ${where} :: ${text.slice(0, 200).replace(/\n/g, " | ")}`);
    return null;
  }

  const tiers = await readTiers(page);
  check(`${label}: the metadata block renders all four tiers`,
    !!tiers && !!tiers.title && !!tiers.tech && !!tiers.album && !!tiers.artist,
    JSON.stringify(tiers && { t: tiers.title?.text, tech: tiers.tech?.text, a: tiers.album?.text }));
  if (!tiers) {
    await page.screenshot({ path: path.join(SHOTS, `${label}-NO-BLOCK.png`) }).catch(() => {});
    return null;
  }

  const fieldL = await fieldColor(page, tiers, "left");
  const fieldR = await fieldColor(page, tiers, "right");
  const field = [0, 1, 2].map((i) => (fieldL[i] + fieldR[i]) / 2);

  const rows = [
    ["title", tiers.title, AA_LARGE],
    ["the format line", tiers.tech, AA_SMALL],
    ["the album line", tiers.album, AA_SMALL],
    ["the artist line", tiers.artist, AA_SMALL],
  ];
  const report = [];
  for (const [name, tier, need] of rows) {
    if (!tier) continue;
    const c = contrast(parseRgb(tier.color), field);
    report.push(`${name}=${c.toFixed(2)}:1`);
    check(`${label}: ${name} holds ${need}:1 on the field`,
      c >= need, `${c.toFixed(2)}:1 (need ${need}) colour=${tier.color} field=rgb(${field.map((v) => Math.round(v))})`);
  }
  // The tier table in use, read from the INK the title was given, and the
  // shadow rule that goes with it: only the dark-field polarity (white ink)
  // carries the pane's glyph shadow.
  const lightInk = relLum(parseRgb(tiers.title.color)) > 0.5;
  check(`${label}: the metadata takes the ${lightInk ? "DARK" : "LIGHT"} FIELD polarity's ink table`,
    lightInk ? hex === "#808080" : hex === "#dcdcdc",
    `title colour=${tiers.title.color} cover=${hex}`);
  check(`${label}: only the dark-field polarity carries the pane's glyph shadow`,
    lightInk ? tiers.title.shadow !== "none" : tiers.title.shadow === "none",
    `shadow=${tiers.title.shadow}`);
  await page.screenshot({ path: path.join(SHOTS, `${label}.png`) });
  return { lightInk, report, field };
}

(async () => {
  let browser;
  const errs = [];
  try {
    fs.mkdirSync(SHOTS, { recursive: true });
    browser = await chromium.launch({
      executablePath: process.env.CHROME,
      headless: true,
      args: ["--autoplay-policy=no-user-gesture-required"],
    });
    const page = await browser.newContext({
      viewport: { width: 1440, height: 900 },
      serviceWorkers: "block",
    }).then((c) => c.newPage());
    page.setDefaultTimeout(20000);
    page.on("pageerror", (e) => errs.push(e.message));

    // One album with tracks to play (the metadata block only exists once
    // something is playing through the bar).
    const lib = await (await fetch(`${BASE}/api/library`)).json();
    let album = null;
    for (const a of lib.artists || []) {
      for (const al of a.albums || []) if ((al.tracks || []).length) { album = al.path; break; }
      if (album) break;
    }
    check("the fixture has an album to play", !!album, JSON.stringify(album));

    const grey = await measure(page, "#808080", "mid-grey-cover", album);
    if (grey) console.log(`  mid-grey field: rgb(${grey.field.map((v) => Math.round(v))}) ${grey.report.join(" ")}`);
    const light = await measure(page, "#dcdcdc", "light-cover", album);
    if (light) console.log(`  light field:    rgb(${light.field.map((v) => Math.round(v))}) ${light.report.join(" ")}`);
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
