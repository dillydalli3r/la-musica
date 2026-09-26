#!/usr/bin/env node
/* Fullscreen player legibility over ANY cover — the metadata tiers AND the
 * chrome around them: the title, the format line ("16/44.1") and the
 * album/artist lines, plus the seek bar, the volume bar, the top bar's icon
 * family and the two bottom-right lyric chips. The failure the owner reported
 * was a mid-grey cover where the title's white read fine but the secondary
 * lines (zinc-500 / zinc-400) sat barely above 2:1 on that same field; the
 * same report a pass later was the BARS disappearing into the artwork.
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
 * The CHROME is measured in `measureChrome` below, from the same table and
 * the same way — the pixels, per polarity:
 *   - the sliders: the unplayed run, the played run and the thumb against the
 *     field above the track, and the two runs against each other (the fixed
 *     zinc track this replaced measured 1.70:1 on the dark cover and 1.01:1,
 *     i.e. invisible, on the mid-grey one; the white accent thumb 1.64:1 on
 *     the white one). The player wires these from its ink as `--seek-*`; the
 *     check asserts the variables are set AND what they paint.
 *   - the top bar's icons: one idle ink for the whole row, one engaged ink,
 *     one 36 px box around a 20 px glyph, one pitch — with both toggles
 *     flipped off and back on so BOTH families are measured, not whichever
 *     state the run happened to catch.
 *   - the two bottom-right chips (zoom `− 100% +`, offset `− 0.0s +`): one
 *     geometry (same boxes, same gaps, same cy), the value CENTRED between the
 *     two glyphs (it was right-packed, which left 27 px of air on the `−` side
 *     against 12 px on the `+`: issue #56), the `−`/`+` glyphs found in the
 *     PIXELS at the same offset from either chip's own edges, and their own
 *     contrast at rest. The offset chip also reserves its Save/Discard slot in
 *     BOTH states, and a real press on its `+` must leave every chip box where
 *     it was — appended-when-dirty, those two widened the strip by 58 px the
 *     instant the offset was dialled and the reader's next press landed on
 *     Save, a write into the track's own lyrics (issue #56 again).
 *
 * Four covers are stubbed: dark (#101014), the mid-grey one the old polarity
 * rule used to flip on (#808080), the bright-grey boundary case (#b4b4b4) and
 * white (#ffffff — the worst case for light ink, and the cover the old
 * near-black table failed at 2.5:1). Each run plays a track that CARRIES
 * LYRICS, so the lyrics pane (and with it the chips) is up in the saved
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
  /** The element that PAINTS the glyphs: the deepest descendant carrying the
   *  same text. The metadata lines are LINKS (`MetaLink`), and a link puts the
   *  ink class on the text inside it — the `<a>` itself carries only
   *  `hover:underline`, so it INHERITS the app's body colour (#e8e8e8) instead
   *  of the polarity table's ink. Reading the `<a>` therefore reported the
   *  near-white body colour on a white cover and called a line that renders
   *  near-black a 1.49:1 failure. */
  const painted = (el) => {
    if (!el) return null;
    const text = (el.textContent || "").trim();
    let cur = el;
    while (cur.children.length === 1 && (cur.firstElementChild.textContent || "").trim() === text) {
      cur = cur.firstElementChild;
    }
    return cur;
  };
  const title = painted(rows[0]?.querySelector(".text-2xl"));
  const tech = painted(rows[0]?.querySelector("span.font-mono"));
  const albumArtist = painted(rows[1]?.firstElementChild);
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
  return clipAvg(page, side === "left"
    ? { x: tiers.field.left + 6, y, width: 36, height: 12 }
    : { x: tiers.field.right - 42, y, width: 36, height: 12 });
}

/** The average colour of a screenshot clip. Everything below that measures a
 *  painted surface (and not a computed colour) goes through this: the field
 *  is a stack of blurred cover, washes, glow and scrim, and a slider's track
 *  is painted by a `::-webkit-slider-*` pseudo-element the computed-style API
 *  will not even hand back — only the pixels are the truth. */
async function clipAvg(page, clip) {
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

/** The ink inside a clip, and where it is: the column spans that deviate from
 *  the clip's own background (`runs`), plus the average colour of the
 *  deviating pixels (`ink`) and of the rest (`bg`) — the glyphs and the field
 *  they sit on, straight from the pixels.
 *
 *  This is how a chip's `−`, value and `+` are located without asking the DOM
 *  — the question "do the two chips' minus and plus line up" is about pixels,
 *  and a DOM rect would only report the (already equal) button boxes — and how
 *  their contrast is read: an average over a clip that is two thirds
 *  background is not a glyph's colour, and the first draft of this measured
 *  exactly that and reported 1.1:1 for a chip that reads fine.
 *
 *  The deviation is signed BOTH ways and the background is per-column median:
 *  the ink is near-white on a dark cover and near-black on a bright one, so a
 *  one-sided "brighter than the background" test finds no glyphs at all on
 *  half the covers, and a per-COLUMN MAXIMUM (the obvious first cut) measures
 *  the background even inside a glyph — the brightest pixel of a column that
 *  holds a dark glyph is the field around it. */
async function inkStats(page, clip, thresh = 14) {
  const png = (await page.screenshot({ clip })).toString("base64");
  return page.evaluate(async ({ b64, thresh }) => {
    const img = new Image();
    img.src = "data:image/png;base64," + b64;
    await img.decode();
    const c = document.createElement("canvas");
    c.width = img.width; c.height = img.height;
    const g = c.getContext("2d");
    g.drawImage(img, 0, 0);
    const d = g.getImageData(0, 0, c.width, c.height).data;
    const lumAt = (x, y) => {
      const i = (y * c.width + x) * 4;
      return 0.2126 * d[i] + 0.7152 * d[i + 1] + 0.0722 * d[i + 2];
    };
    const colMed = [];
    for (let x = 0; x < c.width; x++) {
      const col = [];
      for (let y = 0; y < c.height; y++) col.push(lumAt(x, y));
      col.sort((a, b) => a - b);
      colMed.push(col[Math.floor(col.length / 2)]);
    }
    const base = [...colMed].sort((a, b) => a - b)[Math.floor(colMed.length / 2)];
    const dev = [];
    for (let x = 0; x < c.width; x++) {
      let m = 0;
      for (let y = 0; y < c.height; y++) m = Math.max(m, Math.abs(lumAt(x, y) - base));
      dev.push(m);
    }
    const runs = [];
    let cur = null;
    for (let x = 0; x < dev.length; x++) {
      if (dev[x] > thresh) {
        if (cur) cur[1] = x; else cur = [x, x];
      } else if (cur) { runs.push(cur); cur = null; }
    }
    if (cur) runs.push(cur);
    const inkPx = [], bgPx = [];
    for (let x = 0; x < c.width; x++) {
      for (let y = 0; y < c.height; y++) {
        const i = (y * c.width + x) * 4;
        const dv = Math.abs(lumAt(x, y) - base);
        (dv > thresh ? inkPx : bgPx).push({ dv, rgb: [d[i], d[i + 1], d[i + 2]] });
      }
    }
    const avg = (a) => (a.length ? [0, 1, 2].map((k) => a.reduce((s, p) => s + p.rgb[k], 0) / a.length) : null);
    // The ink is the glyph's CORE, not the mean of everything that deviates:
    // a 10 px glyph is mostly anti-aliased edge, and the edge of a dark glyph
    // on a bright cover is a pale grey — averaging it in reported 2.0:1 for a
    // chip whose digits are a solid near-black. The top quarter by deviation
    // is the stroke's own colour, which is what a reader reads.
    const core = [...inkPx].sort((a, b) => b.dv - a.dv).slice(0, Math.max(1, Math.ceil(inkPx.length * 0.25)));
    return { base: Math.round(base), runs, ink: avg(core), bg: avg(bgPx), inkPixels: inkPx.length, total: inkPx.length + bgPx.length };
  }, { b64: png, thresh });
}

/** The fullscreen player's chrome as the DOM reports it: the two sliders (with
 *  the ink variables the player sets on them), the top bar's icon buttons, and
 *  the two bottom-right lyric chips with every child rect. */
const readChrome = (page) => page.evaluate(() => {
  const overlay = document.querySelector("div.fixed.inset-0.z-50");
  if (!overlay) return null;
  const rect = (el) => {
    const r = el.getBoundingClientRect();
    return { x: +r.x.toFixed(1), y: +r.y.toFixed(1), w: +r.width.toFixed(1), h: +r.height.toFixed(1),
             right: +r.right.toFixed(1), bottom: +r.bottom.toFixed(1), cy: +(r.y + r.height / 2).toFixed(1) };
  };
  const sliders = [...overlay.querySelectorAll('input[type="range"].seek-fat')].map((el) => {
    const cs = getComputedStyle(el);
    return {
      label: el.getAttribute("aria-label") || el.title || "?",
      rect: rect(el),
      value: Number(el.value), max: Number(el.max),
      pct: Number(el.max) > 0 ? Math.min(100, Math.max(0, (Number(el.value) / Number(el.max)) * 100)) : 0,
      ink: {
        track: cs.getPropertyValue("--seek-track").trim(),
        fill: cs.getPropertyValue("--seek-fill").trim(),
        thumb: cs.getPropertyValue("--seek-thumb").trim(),
        ring: cs.getPropertyValue("--seek-ring").trim(),
      },
    };
  });
  const bar = overlay.querySelector(".safe-np-top");
  const icons = bar ? [...bar.querySelectorAll("button")].filter((b) => b.querySelector("svg")).map((b) => {
    const cs = getComputedStyle(b);
    const svg = b.querySelector("svg");
    const r = rect(b), sr = rect(svg);
    return {
      label: b.getAttribute("aria-label") || b.title || "?",
      side: r.x < window.innerWidth / 2 ? "left" : "right",
      color: cs.color, opacity: Number(cs.opacity), hover: b.matches(":hover"),
      box: { w: r.w, h: r.h }, svg: { w: sr.w, h: sr.h },
      rect: r,
      pressed: b.getAttribute("aria-pressed"), expanded: b.getAttribute("aria-expanded"),
      engaged: b.getAttribute("aria-pressed") === "true" || b.getAttribute("aria-expanded") === "true",
    };
  }) : [];
  // the bottom-right lyric chips: the one `justify-end` row carrying the two
  // steppers (each chip is a span of ⊖ / value box / ⊕ and — on the offset
  // chip — the Save/Discard slot), with every child rect. One level of
  // children UNDER each child too: the slot is the box the two action buttons
  // live in, and the zoom chip's value box holds the typeable input.
  const kid = (k) => {
    const cs = getComputedStyle(k);
    const r = rect(k);
    return {
      tag: k.tagName.toLowerCase(),
      text: String(k.value !== undefined ? k.value : k.textContent || "").trim().slice(0, 10),
      rect: r,
      fontSize: cs.fontSize,
      color: cs.color,
      opacity: Number(cs.opacity),
      visibility: cs.visibility,
      scroll: k.tagName === "INPUT" ? { scrollWidth: k.scrollWidth, clientWidth: k.clientWidth } : null,
      kids: [...k.children].map(kid),
    };
  };
  const row = [...overlay.querySelectorAll("div")].find((d) =>
    typeof d.className === "string" && d.className.includes("justify-end") &&
    [...d.children].filter((c) => c.querySelectorAll && c.querySelectorAll("button").length >= 2).length >= 2);
  const chips = row ? {
    rect: rect(row),
    opacity: Number(getComputedStyle(row).opacity),
    color: getComputedStyle(row).color,
    items: [...row.children].map((chip) => ({
      gap: getComputedStyle(chip).gap,
      rect: rect(chip),
      kids: [...chip.children].map(kid),
    })),
  } : null;
  return { sliders, icons, chips };
});

/** Press the offset chip's `+` for real, and read every chip box back.
 *
 *  This is issue #56's second half: the Save and the Discard used to be
 *  appended when the offset turned dirty, so the first step widened the strip
 *  by 58 px and slid every box under the reader's finger — their next press
 *  landed on Save (a write into the track's own lyrics) or on Discard. The slot
 *  those two live in is reserved in BOTH states, so the row's own rect and the
 *  two chips' steppers must come back identical, and the slot's box must be the
 *  same size with its buttons now visible.
 *
 *  A real mouse press at the button's own centre is what makes the offset
 *  dirty: a synthetic `.click()` would reach the handler through a box the
 *  reader could not press, which is the thing being ruled out. The Discard
 *  press at the end puts the chip back at rest, so the contrast samples taken
 *  after this are read on the surface the reader gets. */
async function measureDirtyShift(page, chips, steppers) {
  const locate = (aria) => page.evaluate((aria) => {
    const overlay = document.querySelector("div.fixed.inset-0.z-50");
    if (!overlay) return null;
    const row = [...overlay.querySelectorAll("div")].find((d) =>
      typeof d.className === "string" && d.className.includes("justify-end") &&
      [...d.children].filter((c) => c.querySelectorAll && c.querySelectorAll("button").length >= 2).length >= 2);
    const chip = row && [...row.children][1];
    const btn = chip && [...chip.querySelectorAll("button")].find((b) => b.getAttribute("aria-label") === aria);
    if (!btn) return null;
    const r = btn.getBoundingClientRect();
    return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
  }, aria);
  // the rects that must not move: the row, both chips, and each chip's steppers
  const shape = (c) => {
    const out = { row: c.rect };
    c.items.forEach((chip, i) => {
      const who = ["zoom", "offset"][i];
      out[who] = chip.rect;
      chip.kids.slice(0, steppers).forEach((k, n) => { out[`${who}.kid${n}`] = k.rect; });
    });
    return out;
  };
  const KEYS = ["x", "y", "w", "h", "right", "cy"];
  const before = await shape(chips);
  const at = await locate("Lyrics 0.1 s later");
  if (!at) return null;
  await page.mouse.click(at.x, at.y);
  await sleep(350);
  const after = (await readChrome(page)).chips;
  if (!after) return null;
  const moved = [];
  const now = shape(after);
  for (const who of Object.keys(before)) {
    for (const key of KEYS) {
      if (now[who] && Math.abs(before[who][key] - now[who][key]) > 0.5) {
        moved.push(`${who}.${key} ${before[who][key]}→${now[who][key]}`);
      }
    }
  }
  const slotBefore = chips.items[1].kids[steppers];
  const slotAfter = after.items[1].kids[steppers];
  const away = await locate("Discard pending lyric offset");
  if (away) {
    await page.mouse.click(away.x, away.y);
    await sleep(350);
  }
  return {
    moved,
    slotBefore: slotBefore ? slotBefore.rect.w : null,
    slotAfter: slotAfter ? slotAfter.rect.w : null,
    slotVisibilityBefore: slotBefore ? slotBefore.visibility : "?",
    slotVisible: !!slotAfter && slotAfter.visibility === "visible",
  };
}

/** Park both sliders mid-way: without a live value there is no played run to
 *  measure, and the check would only ever see one colour of track. React only
 *  hears a synthetic change through the native setter, hence the descriptor. */
const parkSliders = (page, seekFraction, volFraction) => page.evaluate(({ seekFraction, volFraction }) => {
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
  const overlay = document.querySelector("div.fixed.inset-0.z-50");
  for (const el of overlay.querySelectorAll('input[type="range"].seek-fat')) {
    const frac = el.max === "1" ? volFraction : seekFraction;
    set.call(el, String(el.max === "1" ? frac : Number(el.max) * frac));
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }
}, { seekFraction, volFraction });

const clipOf = (x, y, width, height) => ({
  x: Math.max(0, Math.round(x)), y: Math.max(0, Math.round(y)), width, height,
});

/** The ink an element actually paints: its colour composited with its own
 *  `opacity` over the field behind it. A near-black at `opacity-65` over a
 *  white cover is what a reader sees, and the computed `color` alone would
 *  call it 19:1. */
function painted(colorCss, opacity, field) {
  const m = colorCss.match(/[\d.]+/g) || ["0", "0", "0", "1"];
  const a = (m.length > 3 ? Number(m[3]) : 1) * opacity;
  return [0, 1, 2].map((i) => Number(m[i]) * a + field[i] * (1 - a));
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

/** Wait until the top bar's icon inks stop changing, with the pointer parked.
 *
 *  A click leaves the pointer on the button it pressed, and `chromeOff`'s hover
 *  half (`hover:text-white`) and its `transition-colors` mean the ink of that
 *  one icon is genuinely in motion for a moment afterwards — a read taken then
 *  reports a tone no state owns ("rgb(255,255,255)@0.6": the hovered colour
 *  with the resting opacity), which is what made this check fail on its own
 *  transient twice (the panel above parks the pointer; this waits for the
 *  colours to agree with themselves). Two consecutive animation frames rather
 *  than a wall-clock sleep: a starved renderer can serve the same stalled frame
 *  twice, so a still-changing ink is what is waited on, not a duration. */
const settleIcons = (page) => page.evaluate(async () => {
  const read = () => {
    const bar = document.querySelector("div.fixed.inset-0.z-50 .safe-np-top");
    return [...bar.querySelectorAll("button")].filter((b) => b.querySelector("svg"))
      .map((b) => `${getComputedStyle(b).color}@${getComputedStyle(b).opacity}${b.matches(":hover") ? " HOVER" : ""}`);
  };
  const frame = () => new Promise((r) => requestAnimationFrame(() => r()));
  let prev = read().join(" | ");
  for (let i = 0; i < 60; i++) {
    await frame(); await frame();
    const now = read().join(" | ");
    if (now === prev && !now.includes("HOVER")) return { settled: true, frames: i, value: now };
    prev = now;
  }
  return { settled: false, value: prev };
});

/** The fullscreen player's CHROME, per cover polarity — the other half of the
 *  same rule the metadata tiers above are held to: nothing the player draws
 *  over the artwork may be the same tone as the artwork.
 *
 *  Three families, each measured from the pixels rather than from the class
 *  list, because a computed colour says nothing about the field it lands on:
 *
 *   - the SLIDERS (the seek bar and the volume bar). The unplayed run, the
 *     played run and the thumb are measured against the field just above the
 *     track, and the two runs against each other. The fixed zinc track this
 *     replaced measured 1.70:1 on the dark cover and 1.01:1 — invisible — on
 *     the mid-grey one, and the white accent thumb 1.64:1 on the white one.
 *   - the TOP BAR's icon family: every icon must wear the SAME ink when it is
 *     idle (one colour+opacity pair in the whole row) and the same ink when it
 *     is engaged, with one 36 px box and one 20 px glyph. The row used to mix
 *     full-strength ink on the plain buttons, `text-accent` on the fullscreen
 *     button and a dimmed toggle for the rest.
 *   - the two bottom-right LYRIC CHIPS (zoom `− 100% +`, offset `− 0.0s +`):
 *     one geometry (the `−` and `+` at the same offset from either chip's own
 *     edges, the value box the same width, the same cy) and their own
 *     contrast at rest, on both polarities.
 */
async function measureChrome(page, label, hex) {
  const coverLum = relLum(hexRgb(hex));
  const lightCover = coverLum > 0.42;
  /** Park the pointer on empty artwork before reading a colour. `chromeOff`
   *  carries a hover half (`hover:opacity-100` and, on the dark table,
   *  `hover:text-white`), and a click leaves the pointer exactly on the button
   *  it just pressed — so a read taken straight after one measures the HOVERED
   *  tone and calls an idle icon engaged. The same trap caught the first run
   *  of the throwaway probe. */
  const park = async () => {
    await page.mouse.move(40, Math.round(900 * 0.45));
    await sleep(300);
  };
  // A live position for both bars: with the value at zero the played run has
  // no length to measure, which is how a one-colour track stayed wrong.
  await parkSliders(page, 0.5, 0.55);
  await park();
  const settled0 = await settleIcons(page);
  const chrome = await readChrome(page);
  if (!chrome) {
    check(`${label}: the fullscreen chrome is in the DOM`, false, "no overlay");
    return null;
  }

  // ---- the sliders ------------------------------------------------------
  check(`${label}: the seek bar and the volume bar are both on screen`,
    chrome.sliders.length >= 2, JSON.stringify(chrome.sliders.map((s) => s.label)));
  const sliderReport = [];
  for (const s of chrome.sliders) {
    const wired = Object.values(s.ink).every((v) => v !== "");
    check(`${label}: the ${s.label} bar takes its ink from the player (--seek-*)`,
      wired, JSON.stringify(s.ink));
    if (!wired) continue;
    const y = s.rect.cy;
    const field = await clipAvg(page, clipOf(s.rect.x + 30, y - 11, 20, 4));
    // the unplayed run lives at the RIGHT end of the track (the played run
    // covers 0..pct), the played run just before the thumb
    const unfilled = await clipAvg(page, clipOf(s.rect.x + s.rect.w - 24, y - 2, 16, 4));
    const thumbX = s.rect.x + (s.pct / 100) * s.rect.w;
    const filled = s.pct > 25 ? await clipAvg(page, clipOf(thumbX - 30, y - 2, 16, 4)) : null;
    const thumb = await clipAvg(page, clipOf(thumbX - 4, y - 4, 8, 8));
    const cTrack = contrast(unfilled, field);
    const cFill = filled ? contrast(filled, field) : NaN;
    const cSplit = filled ? contrast(filled, unfilled) : NaN;
    const cThumb = contrast(thumb, field);
    sliderReport.push(`${s.label}: track=${cTrack.toFixed(2)}:1 fill=${cFill.toFixed(2)}:1 `
      + `fill/track=${cSplit.toFixed(2)}:1 thumb=${cThumb.toFixed(2)}:1 field=rgb(${field.map((v) => Math.round(v))})`);
    check(`${label}: the ${s.label} bar's unplayed run holds 3:1 on the field`,
      cTrack >= 3, `${cTrack.toFixed(2)}:1 field=rgb(${field.map((v) => Math.round(v))}) run=rgb(${unfilled.map((v) => Math.round(v))})`);
    check(`${label}: the ${s.label} bar's played run holds 3:1 on the field`,
      cFill >= 3, `${cFill.toFixed(2)}:1 fill=rgb(${filled ? filled.map((v) => Math.round(v)) : null})`);
    check(`${label}: the ${s.label} bar's two runs are told apart`,
      cSplit >= 2, `${cSplit.toFixed(2)}:1 (need 2) run=rgb(${unfilled.map((v) => Math.round(v))}) fill=rgb(${filled ? filled.map((v) => Math.round(v)) : null})`);
    check(`${label}: the ${s.label} bar's thumb holds 3:1 on the field`,
      cThumb >= 3, `${cThumb.toFixed(2)}:1 thumb=rgb(${thumb.map((v) => Math.round(v))})`);
    // …and the ink answers the COVER, like the text: light ink on a dark
    // cover, dark ink on a bright one. A fixed track cannot do this, and that
    // is exactly the failure the owner photographed.
    const runInkLighter = relLum(unfilled) > relLum(field);
    check(`${label}: the ${s.label} bar inks against the cover (${lightCover ? "bright" : "dark"})`,
      runInkLighter !== lightCover,
      `run=rgb(${unfilled.map((v) => Math.round(v))}) field=rgb(${field.map((v) => Math.round(v))}) coverLum=${coverLum.toFixed(3)}`);
  }
  console.log(`  ${label}: ${sliderReport.join(" | ")}`);

  // ---- the two bottom-right lyric chips ---------------------------------
  const chips = chrome.chips;
  if (!chips || chips.items.length < 2) {
    check(`${label}: both lyric chips are on screen (zoom + offset)`, false,
      JSON.stringify(chips && chips.items.length));
    return { sliderReport };
  }
  const [zoom, offset] = chips.items;
  const rel = (chip, k, key) => +(k.rect[key] - chip.rect.x).toFixed(1);
  // The STEPPERS are each chip's first three children (`−`, the value box,
  // `+`). The offset chip carries a fourth — the Save/Discard slot (issue #56)
  // — which the zoom chip has none of, so the comparison is over the common
  // prefix and the slot is asserted on its own.
  const STEPPERS = 3;
  const geom = (chip) => chip.kids.slice(0, STEPPERS).map((k) => `${k.tag}:${rel(chip, k, "x")}+${k.rect.w}x${k.rect.h}`);
  const slot = offset.kids[STEPPERS];
  check(`${label}: the two lyric chips are one stepper geometry (same boxes, same gaps, same cy)`,
    JSON.stringify(geom(zoom)) === JSON.stringify(geom(offset)) &&
      zoom.gap === offset.gap && zoom.rect.h === offset.rect.h,
    `zoom=[${geom(zoom)}] gap=${zoom.gap} :: offset=[${geom(offset)}] gap=${offset.gap}`);
  // The reserved slot: the two action boxes exist in EVERY state (hidden while
  // nothing is dialled in), which is what keeps the steppers still. Appended
  // only when dirty, they widened the strip by 58 px at the first step and the
  // reader's next press landed on Save — a write into the track's own lyrics.
  check(`${label}: the offset chip reserves its Save/Discard slot while clean`,
    !!slot && zoom.kids.length === STEPPERS && offset.kids.length === STEPPERS + 1 &&
      slot.tag === "span" && slot.rect.w === 58 && slot.visibility === "hidden" &&
      slot.kids.filter((k) => k.tag === "button").length === 2,
    `zoom kids=${zoom.kids.length} :: offset kids=${offset.kids.length} :: slot=`
    + (slot ? `${slot.tag} ${slot.rect.w}px ${slot.visibility} [${slot.kids.map((k) => `${k.tag} ${k.rect.w}`).join(", ")}]` : "missing"));
  check(`${label}: the zoom and offset values share one typography`,
    zoom.kids[1].fontSize === offset.kids[1].fontSize &&
      zoom.kids[1].rect.w === offset.kids[1].rect.w,
    `zoom value ${zoom.kids[1].fontSize} w=${zoom.kids[1].rect.w} :: offset ${offset.kids[1].fontSize} w=${offset.kids[1].rect.w}`);
  // the zoom's typeable value must not clip (the field is three mono digits)
  const sc = zoom.kids[1].kids ? zoom.kids[1].kids[0].scroll : null;
  if (sc) check(`${label}: the lyrics-size value is not clipped by its own box`,
    sc.scrollWidth <= sc.clientWidth + 1, JSON.stringify(sc));

  // The pixels: the two chips' `−` and `+` must sit at the same offset from
  // their own chip's left edge (that IS "the − / + line up on both"), and the
  // value must end the same distance before the `+`.
  const strip = clipOf(chips.rect.x, chips.rect.cy - 14, Math.round(chips.rect.w), 28);
  const runs = (await inkStats(page, strip)).runs;
  const off = (chip) => {
    const rs = runs.filter((r) => r[0] + chips.rect.x >= chip.rect.x && r[1] + chips.rect.x <= chip.rect.right + 1);
    return rs.length >= 3 ? {
      minus: ((rs[0][0] + rs[0][1]) / 2 + chips.rect.x - chip.rect.x),
      plus: ((rs[rs.length - 1][0] + rs[rs.length - 1][1]) / 2 + chips.rect.x - chip.rect.x),
      valueStart: rs[1][0] + chips.rect.x - chip.rect.x,
      valueEnd: rs[rs.length - 2][1] + chips.rect.x - chip.rect.x,
      n: rs.length,
    } : null;
  };
  const zo = off(zoom), oo = off(offset);
  check(`${label}: the chips' − / + glyphs line up on both`,
    !!zo && !!oo && Math.abs(zo.minus - oo.minus) <= 1.5 && Math.abs(zo.plus - oo.plus) <= 1.5,
    `zoom −=${zo ? zo.minus.toFixed(1) : "?"} +=${zo ? zo.plus.toFixed(1) : "?"} :: offset −=${oo ? oo.minus.toFixed(1) : "?"} +=${oo ? oo.plus.toFixed(1) : "?"} runs=${runs.length}`);
  check(`${label}: the value sits the same distance from the + on both chips`,
    !!zo && !!oo && Math.abs((zo.plus - zo.valueEnd) - (oo.plus - oo.valueEnd)) <= 3,
    `zoom value→+ = ${zo ? (zo.plus - zo.valueEnd).toFixed(1) : "?"}px :: offset = ${oo ? (oo.plus - oo.valueEnd).toFixed(1) : "?"}px`);
  // …and it sits CENTRED between the two glyphs (issue #56: "these buttons
  // should be more centered"). Right-packed — the shape this replaced — the
  // number hugged the `+` and left a hole beside the `−`: measured off the
  // owner's own screenshot, 27 px of air on the `−` side against 12 px on the
  // `+` side, on both chips. Measured as the value's ink CENTRE against the
  // midpoint of the two glyphs, which is the one reading a glyph's own side
  // bearings cannot bias: the first and last glyphs of a value (`1` in `100`,
  // `+` in `+0.1`) carry theirs inside their advance, so comparing the two air
  // gaps directly would charge the layout for the font.
  const centreOff = (o) => (o ? Math.abs((o.valueStart + o.valueEnd) / 2 - (o.minus + o.plus) / 2) : NaN);
  const zc = centreOff(zo), oc = centreOff(oo);
  check(`${label}: the value is centred between the − and the + on both chips`,
    !!zo && !!oo && zc <= 3 && oc <= 3,
    `zoom ink centre is ${zc.toFixed(1)}px off the midpoint (−${zo ? zo.minus.toFixed(1) : "?"}/+${zo ? zo.plus.toFixed(1) : "?"}) :: `
    + `offset ${oc.toFixed(1)}px (−${oo ? oo.minus.toFixed(1) : "?"}/+${oo ? oo.plus.toFixed(1) : "?"})`);

  // The steppers must still be exactly where they were once the offset is
  // dirty — the whole of issue #56's second report ("they shouldn't move when
  // the confirm button pops up, it makes it easily clickable by accident").
  // Read from the same DOM rects, before and after a press on the offset's `+`,
  // and discarded afterwards so the contrast samples below see the chip at
  // rest.
  const steady = await measureDirtyShift(page, chips, STEPPERS);
  check(`${label}: pressing + leaves every chip box where it was`,
    !!steady && steady.moved.length === 0,
    steady ? `moved=${JSON.stringify(steady.moved)} slot=${steady.slotBefore}→${steady.slotAfter} visible=${steady.slotVisible}` : "could not press the offset +");
  check(`${label}: the Save/Discard slot becomes visible on the same box`, !!steady && steady.slotVisible && steady.slotAfter === steady.slotBefore,
    steady ? `before=${steady.slotBefore}px ${steady.slotVisibilityBefore} → after=${steady.slotAfter}px visible=${steady.slotVisible}` : "?");

  // Contrast at rest, each element against the field IT sits on: the value box
  // (small text, AA is 4.5:1) and the two step buttons' glyphs (non-text, 3:1).
  //
  // The ink is the element's OWN colour composited with its opacity over the
  // field — the same measure the top bar's icons above get — and the field is
  // a real pixel sample (the average of the clip's non-ink pixels), so a
  // transparent or wrongly-inherited colour still fails. The pixel AVERAGE of
  // a glyph is not used for the floor: these glyphs are 1 px strokes (`Minus`
  // at 12 px is one pixel of stroke spread over two antialiased rows), so the
  // darkest pixel is about half coverage and the average of a clip is mostly
  // the field. Read that way a chip whose digits are a solid near-black
  // measured 2.0:1 on the white cover. The `inkSide` guard below is what keeps
  // the pixel evidence in the check: the painted pixels must actually sit on
  // the ink's side of the field.
  const inkSide = (ink, bg, inkTarget) => {
    if (!ink || !bg || !inkTarget) return false;
    const want = relLum(inkTarget) - relLum(bg);
    const got = relLum(ink) - relLum(bg);
    return Math.sign(want) === Math.sign(got) && Math.abs(got) > 0.02;
  };
  const chipReport = [];
  for (const [which, chip] of [["zoom", zoom], ["offset", offset]]) {
    const box = chip.kids[1];
    const valuePx = await inkStats(page, clipOf(box.rect.x - 1, box.rect.y - 2, Math.round(box.rect.w) + 2, Math.round(box.rect.h) + 4), 20);
    const valueInk = painted(box.color, box.opacity * chips.opacity, valuePx.bg);
    const cValue = contrast(valueInk, valuePx.bg);
    chipReport.push(`${which} value=${cValue.toFixed(2)}:1 valueBox=${box.rect.w}x${box.rect.h}@${(box.rect.x - chip.rect.x).toFixed(0)}[${box.color}@${box.opacity}]`);
    check(`${label}: the ${which} chip's value holds 4.5:1 on the field at rest`,
      cValue >= 4.5 && inkSide(valuePx.ink, valuePx.bg, valueInk),
      `${cValue.toFixed(2)}:1 value=${box.color}@${box.opacity} over field=rgb(${valuePx.bg && valuePx.bg.map((v) => Math.round(v))}) `
      + `painted=rgb(${valueInk.map((v) => Math.round(v))}) glyphPixels=rgb(${valuePx.ink && valuePx.ink.map((v) => Math.round(v))}) rowOpacity=${chips.opacity}`);
    const btn = chip.kids[0];
    const stepPx = await inkStats(page, clipOf(btn.rect.x + 7, chip.rect.cy - 4, 14, 8), 20);
    const stepInk = painted(btn.color, btn.opacity * chips.opacity, stepPx.bg);
    const cStep = contrast(stepInk, stepPx.bg);
    chipReport.push(`${which} glyph=${cStep.toFixed(2)}:1 [${btn.color}@${btn.opacity}]`);
    check(`${label}: the ${which} chip's step buttons hold 3:1 on the field at rest`,
      cStep >= 3 && inkSide(stepPx.ink, stepPx.bg, stepInk),
      `${cStep.toFixed(2)}:1 glyph=${btn.color}@${btn.opacity} over field=rgb(${stepPx.bg && stepPx.bg.map((v) => Math.round(v))}) `
      + `painted=rgb(${stepInk.map((v) => Math.round(v))}) glyphPixels=rgb(${stepPx.ink && stepPx.ink.map((v) => Math.round(v))})`);
  }
  const geomReport = chips.items.map((c, i) => `${["zoom", "offset"][i]}[${c.kids.map((k) => `${k.tag} ${k.rect.w}x${k.rect.h} @${(k.rect.x - c.rect.x).toFixed(0)}`).join(" | ")}] gap=${c.gap}`);
  console.log(`  ${label}: chips ${geomReport.join("  ")}  ${chipReport.join(" ")}`);

  // ---- the top bar's icon family ----------------------------------------
  const icons = chrome.icons;
  const right = icons.filter((i) => i.side === "right");
  check(`${label}: the top bar's icons are all one box and one glyph size`,
    icons.length >= 5 && icons.every((i) => i.box.w === 36 && i.box.h === 36 && i.svg.w === 20 && i.svg.h === 20),
    JSON.stringify(icons.map((i) => `${i.label}:${i.box.w}x${i.box.h}/${i.svg.w}@${i.rect.x}`)));
  const pitch = right.slice(1).map((i, n) => +(i.rect.x - right[n].rect.x).toFixed(1));
  check(`${label}: the top-right icons are evenly spaced`,
    new Set(pitch).size <= 1, `pitches=${JSON.stringify(pitch)}`);

  const family = async (wantEngaged) => {
    const set = icons.filter((i) => i.engaged === wantEngaged);
    const pairs = [...new Set(set.map((i) => `${i.color}@${i.opacity}`))];
    const fields = {};
    for (const i of set) fields[i.label] = await clipAvg(page, clipOf(i.rect.x + 1, i.rect.y + 1, 6, 6));
    const worst = Math.min(...set.map((i) => contrast(painted(i.color, i.opacity, fields[i.label]), fields[i.label])));
    return { set, pairs, worst, fields };
  };
  const detail = (set) => set.map((i) => `${i.label}=${i.color}@${i.opacity}${i.hover ? "(hover)" : ""}`).join(" | ");
  const engaged = await family(true);
  check(`${label}: every ENGAGED icon uses one ink (accent / full ink)`,
    settled0.settled && engaged.set.length > 0 && engaged.pairs.length === 1,
    JSON.stringify(engaged.pairs) + " :: " + detail(engaged.set) + " :: settled=" + settled0.settled + " state=" + settled0.value);
  check(`${label}: the engaged icons hold 3:1 on the field`,
    engaged.worst >= 3, `${engaged.worst.toFixed(2)}:1` + JSON.stringify(Object.entries(engaged.fields).map(([k, v]) => `${k}=rgb(${v.map(Math.round)})`)));

  // idle: the toggles off, so every icon in the row is in its resting state.
  // The pointer is parked first — a click leaves it on the button it pressed,
  // and `chromeOff`'s hover half would otherwise be what gets measured.
  for (const sel of ['button[aria-label="Toggle visualizer bars"]', 'button[aria-label="Toggle the lyrics pane"]']) {
    const b = page.locator(sel).first();
    if ((await b.count()) && (await b.getAttribute("aria-pressed")) === "true") { await b.click(); await sleep(400); }
  }
  await park();
  const settledIdle = await settleIcons(page);
  const idleChrome = await readChrome(page);
  const idle = idleChrome.icons;
  // …and once more, two frames later: a settled read that then moves is not a
  // state, and a row that flickers between inks is the bug this asserts away.
  const stillIdle = await settleIcons(page);
  const pairs = [...new Set(idle.map((i) => `${i.color}@${i.opacity}`))];
  const idleFields = {};
  for (const i of idle) idleFields[i.label] = await clipAvg(page, clipOf(i.rect.x + 1, i.rect.y + 1, 6, 6));
  const worstIdle = Math.min(...idle.map((i) => contrast(painted(i.color, i.opacity, idleFields[i.label]), idleFields[i.label])));
  check(`${label}: every IDLE icon uses one ink (the same dimmed tone for all)`,
    settledIdle.settled && pairs.length === 1,
    JSON.stringify(pairs) + " :: " + detail(idle) + " :: settled=" + settledIdle.settled);
  check(`${label}: the idle ink is a settled state, not a moving one`,
    stillIdle.settled && stillIdle.value === settledIdle.value,
    `first=${settledIdle.value} :: second=${stillIdle.value} (settled=${stillIdle.settled})`);
  check(`${label}: the idle icons hold 3:1 on the field`,
    worstIdle >= 3, `${worstIdle.toFixed(2)}:1 ` + JSON.stringify(Object.entries(idleFields).map(([k, v]) => `${k}=rgb(${v.map(Math.round)})`)));
  check(`${label}: engaged and idle are two different tones`,
    pairs.length === 1 && engaged.pairs.length === 1 && pairs[0] !== engaged.pairs[0],
    `idle=${JSON.stringify(pairs)} engaged=${JSON.stringify(engaged.pairs)} :: ${detail(idle)}`);
  console.log(`  ${label}: icons idle=${pairs.join(" or ")} worst=${worstIdle.toFixed(2)}:1 :: `
    + `engaged=${engaged.pairs.join(" or ")} worst=${engaged.worst.toFixed(2)}:1 :: boxes=${icons[0] ? `${icons[0].box.w}x${icons[0].box.h}/${icons[0].svg.w}` : "?"} pitch=${JSON.stringify(pitch)}`);
  // The row's state coverage rests on the two reads above — every toggle off,
  // every toggle on — plus the code's own rule: every icon button in this bar
  // draws from that one `barOn` / `barOff` pair (there is no per-button class
  // left in it). A queue-drawer sweep was tried here and dropped: the drawer's
  // own press shield (`absolute inset-0 z-[15]`) covers the bar while it is
  // open, so the "open it again to close it" click cannot land.
  // put the player back the way a reader would find it for the screenshot
  for (const sel of ['button[aria-label="Toggle the lyrics pane"]', 'button[aria-label="Toggle visualizer bars"]']) {
    const b = page.locator(sel).first();
    if ((await b.count()) && (await b.getAttribute("aria-pressed")) === "false") { await b.click(); await sleep(400); }
  }
  await sleep(400);
  await park();
  await page.screenshot({ path: path.join(SHOTS, `${label}-topright.png`), clip: { x: 880, y: 0, width: 560, height: 60 } }).catch(() => {});
  await page.screenshot({ path: path.join(SHOTS, `${label}-chips.png`), clip: { x: 880, y: 780, width: 560, height: 120 } }).catch(() => {});
  return { sliderReport, icons: idle.map((i) => `${i.label}=${i.color}@${i.opacity}`) };
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
  // …and the chrome the metadata sits among: the two sliders, the top bar's
  // icon family and the two lyric chips (see measureChrome).
  const chrome = await measureChrome(page, label, hex);
  await page.screenshot({ path: path.join(SHOTS, `${label}.png`) });
  return { lightInk, report, field, chrome };
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
