#!/usr/bin/env node
/* Fullscreen player metadata legibility: the title, the format line ("16/44.1")
 * and the album/artist lines must all be readable against the field the
 * ambience paints, on EVERY cover — the failure the owner reported was a
 * mid-grey cover where the title's white read fine but the secondary lines
 * (zinc-500 / zinc-400) sat barely above 2:1 on that same field.
 *
 * The metadata block takes its colours from the player's ONE ink table
 * (`INK`, white / zinc-100 / zinc-300) and draws NOTHING of its own — no fill,
 * no border, no blur, no halo (the `np-veil np-veil-pill` box it used to carry
 * is gone). So this checks the claim the way a reader sees it: the computed
 * colour of each tier against the ACTUAL rendered field, measured from a
 * screenshot of the pixels just inside the block's edges (not from a CSS
 * variable — the field is a stack of blurred cover, colour washes, glow and
 * the legibility scrim, so only the screenshot is the truth). The gutters are
 * the field the glyphs sit on: there is no veil under them any more, which is
 * the point.
 *
 * Three covers are stubbed: dark (#101014), the mid-grey one the old polarity
 * rule used to flip on (#808080), and white (#ffffff — the worst case for
 * light ink, and the cover the old near-black table failed at 2.5:1). Each run
 * plays a track that CARRIES LYRICS, so the lyrics pane is open in the saved
 * screenshots. Runs against a live backend serving the built app:
 *   npm --prefix web run build
 *   MLO_MUSIC_FOLDER=<scratch folder> \
 *     python -m uvicorn server.main:app --host 127.0.0.1 --port 8011
 *   node tools/check_np_metadata_contrast.cjs http://127.0.0.1:8011
 *
 * Screenshots land in .pi/shots-player-panels/, named by cover polarity. Exit
 * 2 when Playwright is missing. */

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
const BASE = process.argv[2] || process.env.BASE || "http://127.0.0.1:8011";
const SHOTS = path.join(".pi", "shots-player-panels");
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
const hexRgb = (h) => {
  const n = parseInt(/^#?([0-9a-f]{6})$/i.exec(String(h || ""))[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
};

/** The four tiers of the metadata block, with their boxes, plus the block's own
 *  paint. The block is the box the title row (h-8, title + format line) lives
 *  in, together with the album and artist rows. */
const readTiers = (page) => page.evaluate(() => {
  const overlay = document.querySelector("div.fixed.inset-0.z-50");
  // Structural anchor: the title is the only text-2xl inside the overlay. It
  // is a SPAN since the marquee (`ScrollingText` wraps the text to measure and
  // drift it), and the album + artist are ONE row ("Album · Artist"), so the
  // pair is read as a single tier — the thing the checked floor is about is
  // the text, not how many rows it is laid out in.
  const titleEl = overlay && overlay.querySelector(".text-2xl");
  const titleRow = titleEl && titleEl.closest("div.h-8");
  const block = titleRow && titleRow.parentElement;
  if (!block || block.children.length < 3) return null;
  const rows = [...block.children];
  const title = rows[0]?.querySelector(".text-2xl");
  const tech = rows[0]?.querySelector("span.font-mono");
  const albumArtist = rows[1]?.firstElementChild;
  const box = block.getBoundingClientRect();
  const cs = getComputedStyle(block);
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
    paint: {
      bg: cs.backgroundColor,
      bgImage: cs.backgroundImage === "none" ? "none" : cs.backgroundImage.slice(0, 40),
      backdrop: cs.backdropFilter,
      shadow: cs.boxShadow,
    },
    title: read(title), tech: read(tech), albumArtist: read(albumArtist),
  };
});

/** The colour the glyphs actually contrast against: the pixels INSIDE the
 *  block at the text's own height, in the end of the row the centred text does
 *  not reach — the block draws nothing there, so that IS the field the glyphs
 *  sit on. A glyph caught in the strip would only brighten it (the ink is
 *  light), which reads as a failure rather than a false pass. */
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

async function measure(page, hex, label, pick) {
  let step = "cover stub";
  try {
    await stubCover(page, hex);
    step = "album page";
    await page.goto(`${BASE}/album/${encodeURIComponent(pick.album.path)}`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector('tr[title="Click to play"]');
    step = "lyric track";
    // The row of the track that carries lyrics, so the pane is up (and in the
    // screenshot). Its title is what the row prints; fall back to the first
    // playable row when nothing matched (the contrast check still stands).
    const row = page.locator('tr[title="Click to play"]').filter({ hasText: pick.track.title }).first();
    await ((await row.count()) ? row : page.locator('tr[title="Click to play"]').first()).locator("td").first().click();
    await sleep(1200);
    step = "fullscreen";
    // Open the fullscreen player the way a user does (the app-wide shortcut is
    // layout-independent: the bar's own button is a phone-only control).
    await page.keyboard.press("f");
    await page.waitForSelector("div.fixed.inset-0.z-50 .text-2xl", { timeout: 15000 });
    // The fixture's tracks are a couple of seconds long, so the queue has
    // already run past the lyric track by the time the player is up. Pause and
    // step BACK to it: the pane (and its toggle) only exists while the current
    // track carries lyrics, so this is also the assertion that the pane is up
    // for the shot rather than a lucky default.
    const hasToggle = () => page.locator('button[aria-label="Toggle the lyrics pane"]').count();
    const pause = async () => {
      if (await page.locator('button[aria-label="Pause"]').count()) {
        await page.keyboard.press("Space");
        await sleep(250);
      }
    };
    await pause();
    for (let i = 0; i < 8 && !(await hasToggle()); i++) {
      await page.locator('button[aria-label="Previous track"]').first().click();
      await sleep(600);
      await pause();
    }
    step = "settle";
    // Let the ambience settle and the glow stop moving: a pulse would otherwise
    // make the field (and so the measurement) drift between samples.
    await page.addStyleTag({ content: "*, *::before, *::after { animation-play-state: paused !important; }" });
    await sleep(1500);
    step = "lyrics pane";
    const toggle = page.locator('button[aria-label="Toggle the lyrics pane"]').first();
    if ((await toggle.count()) && (await toggle.getAttribute("aria-pressed")) === "false") {
      await toggle.click();
      await sleep(600);
    }
    check(`${label}: the lyrics pane is up for the shot`,
      await page.locator("div.fixed.inset-0.z-50 .no-scrollbar").count() > 0,
      "the pane scroller is in the DOM");
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
  check(`${label}: the metadata block renders the title, its readout and the album·artist pair`,
    !!tiers && !!tiers.title && !!tiers.tech && !!tiers.albumArtist,
    JSON.stringify(tiers && { t: tiers.title?.text, tech: tiers.tech?.text, a: tiers.albumArtist?.text }));
  if (!tiers) {
    await page.screenshot({ path: path.join(SHOTS, `${label}-NO-BLOCK.png`) }).catch(() => {});
    return null;
  }

  // The field measured below is only the field the glyphs sit on while the
  // block itself paints nothing. Guard it here so a re-introduced veil cannot
  // quietly change what this script has been measuring all along.
  check(`${label}: the block draws no background, blur or shadow of its own`,
    tiers.paint.bg === "rgba(0, 0, 0, 0)" && tiers.paint.bgImage === "none" &&
      tiers.paint.backdrop === "none" && tiers.paint.shadow === "none",
    JSON.stringify(tiers.paint));

  const fieldL = await fieldColor(page, tiers, "left");
  const fieldR = await fieldColor(page, tiers, "right");
  const field = [0, 1, 2].map((i) => (fieldL[i] + fieldR[i]) / 2);

  const rows = [
    ["title", tiers.title, AA_LARGE],
    ["the format line", tiers.tech, AA_SMALL],
    ["the album·artist line", tiers.albumArtist, AA_SMALL],
  ];
  const report = [];
  for (const [name, tier, need] of rows) {
    if (!tier) continue;
    const c = contrast(parseRgb(tier.color), field);
    report.push(`${name}=${c.toFixed(2)}:1`);
    check(`${label}: ${name} holds ${need}:1 on the field`,
      c >= need, `${c.toFixed(2)}:1 (need ${need}) colour=${tier.color} field=rgb(${field.map((v) => Math.round(v))})`);
  }
  // The ink ANSWERS to the cover (spec R52c, `npInk`): a dark cover gets the
  // light table and draws nothing behind the text, a bright one flips to the
  // dark table and lifts the field with a scrim built from the cover's own
  // colour. So the polarity is asserted per cover — against the cover's own
  // luminance, the value the decision is made from — and the contrast numbers
  // above are what prove the chosen table clears its floor.
  const coverLum = relLum(hexRgb(hex));
  const lightInk = relLum(parseRgb(tiers.title.color)) > 0.5;
  check(`${label}: the ink flips with the cover (${coverLum > 0.42 ? "bright" : "dark"} cover)`,
    lightInk === coverLum <= 0.42,
    `title colour=${tiers.title.color} cover=${hex} coverLum=${coverLum.toFixed(3)}`);
  check(`${label}: the metadata tiers carry the glyph shadow`,
    tiers.title.shadow !== "none", `shadow=${tiers.title.shadow}`);
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

    // One album whose track carries lyrics (the pane has to be up, and the
    // metadata block only exists once something is playing through the bar).
    const lib = await (await fetch(`${BASE}/api/library`)).json();
    let pick = null;
    for (const a of lib.artists || []) {
      for (const al of a.albums || []) {
        for (const t of (al.tracks || []).slice(0, 4)) {
          const tags = await (await fetch(`${BASE}/api/tags?path=${encodeURIComponent(t.path)}`)).json().catch(() => null);
          if (typeof tags?.lyrics === "string" && tags.lyrics.trim()) {
            pick = { album: al, track: { title: t.title || t.file.replace(/\.[^.]+$/, "") } };
            break;
          }
        }
        if (pick) break;
      }
      if (pick) break;
    }
    check("the fixture has a track with lyrics to open the pane on", !!pick,
      JSON.stringify(pick && { album: pick.album.path, track: pick.track.title }));
    if (!pick) pick = { album: null, track: { title: "" } };
    if (!pick.album) {
      for (const a of lib.artists || []) {
        for (const al of a.albums || []) if ((al.tracks || []).length) { pick = { album: al, track: { title: "" } }; break; }
        if (pick.album) break;
      }
    }
    check("the fixture has an album to play", !!pick.album, JSON.stringify(pick.album && pick.album.path));

    const covers = [
      ["#101014", "dark-cover"],
      ["#808080", "mid-grey-cover"],
      // Just ABOVE `NP_INK_FLIP` (0.42 relative luminance: #b4b4b4 is 0.47):
      // the boundary the lift is weakest on, so it is the case that says
      // whether the flip point and the lift's slope are right.
      ["#b4b4b4", "bright-grey-cover"],
      ["#ffffff", "light-cover"],
    ];
    for (const [hex, label] of covers) {
      const m = await measure(page, hex, label, pick);
      if (m) console.log(`  ${label}: field rgb(${m.field.map((v) => Math.round(v))}) ${m.report.join(" ")}`);
    }
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
