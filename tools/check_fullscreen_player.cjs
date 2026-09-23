#!/usr/bin/env node
/* Fullscreen player surface: the album art carries no decorative frame, the
 * lyrics pane owns exactly ONE control and that control lives in the player's
 * own control row, nothing is painted on top of the artwork, and neither the
 * pane nor the metadata block draws a panel behind its text.
 *
 * The bugs this pins (owner-reported): the lyrics had no control at all — the
 * pane appeared whenever the track happened to carry lyrics, so the display
 * was an accident of the tags rather than something the reader owns; the cover
 * still carried a hairline frame after the ring removal (the art's own
 * `border-white/10`, plus the shared `border-border` on every row cover); and
 * the legibility tint that replaced the old `bg-white/35` slabs — the lyrics
 * pane's `np-veil-pane` and the title's `np-veil-pill` — still read as grey
 * boxes over the artwork. The tint is gone for good: legibility is the
 * full-bleed wash plus the glyph shadow now, and the metadata block and the
 * lyrics pane must compute to NO fill, blur, shadow or border of their own.
 *
 * Everything here is measured, not eyeballed: the frame from the cover
 * wrapper's COMPUTED border/outline (a `ring-*` in Tailwind is a box-shadow,
 * so a ring is told apart from the art's deliberate drop shadow by its zero
 * offset and blur), the pane's state from its own box, "nothing floats over
 * the art" by hit-testing every control at the art's centre and corners with
 * elementFromPoint, "no layer paints on the art" by walking every element that
 * overlaps the art and paints (a fill, gradient, blur, shadow or border) while
 * painting AFTER it in document order — the ambience stack and the wash come
 * before the cover, so they are behind it — and the removed selectors from the
 * built stylesheet itself.
 *
 * Needs a live backend serving the built app (`web/dist`):
 *   npm --prefix web run build
 *   MLO_MUSIC_FOLDER=<scratch folder> \
 *     python -m uvicorn server.main:app --host 127.0.0.1 --port 8011
 *   PLAYWRIGHT=web/node_modules/playwright \
 *     node tools/check_fullscreen_player.cjs http://127.0.0.1:8011
 *
 * Playwright is required (same resolution as tools/shot.cjs): set PLAYWRIGHT
 * to a module path, or install it. Exit 2 when it is missing, 0 when the
 * library has no album with lyrics (nothing to measure) — that case is named
 * in the output rather than counted as a pass. Screenshots land in
 * .pi/shots-fullscreen-player/. */

let chromium;
try {
  ({ chromium } = require(process.env.PLAYWRIGHT || "playwright"));
} catch {
  console.error("[fullscreen] Playwright not found — `npm i -D playwright`, " +
    "or point PLAYWRIGHT at an installed module.");
  process.exit(2);
}

const fs = require("fs");
const path = require("path");
const BASE = process.argv[2] || process.env.BASE || "http://127.0.0.1:8011";
const SHOTS = path.join(".pi", "shots-fullscreen-player");
const results = [];
const check = (name, pass, detail) => results.push({ name, pass: !!pass, detail: String(detail) });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Everything this check asserts on, read from the live DOM.
 *
 *  `ring` separates a Tailwind `ring-*` from a real elevation shadow: a ring
 *  is `rgb(…) 0px 0px 0px <spread>` — no offset, no blur — while `shadow-lg`
 *  and friends always carry an offset or a blur. That distinction is the whole
 *  point of keeping the shadow and dropping the ring. */
const surface = (page) => page.evaluate(() => {
  const box = (el) => {
    const r = el.getBoundingClientRect();
    return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height), cx: Math.round(r.x + r.width / 2), cy: Math.round(r.y + r.height / 2) };
  };
  const shadowParts = (shadow) => shadow === "none" ? [] : shadow.split(/,(?![^(]*\))/).map((s) => s.trim());
  const ringish = (shadow) => shadowParts(shadow).some((p) => /^rgba?\([^)]*\)\s+0px\s+0px\s+0px\s+(?!0px)/.test(p));

  const root = document.querySelector("div.fixed.inset-0.z-50");
  if (!root) return { error: "the fullscreen player is not open" };

  // The art: the cover <img> that is not the ambience layer's blurred copy.
  const artImg = [...root.querySelectorAll("img")].find((i) => !i.closest(".amb-cover") && i.naturalWidth > 0);
  const art = artImg ? artImg.parentElement : null;
  const artCs = art ? getComputedStyle(art) : null;
  const artBox = art ? box(art) : null;

  // The pane: the scroll container, and the box that collapses around it.
  const scroller = root.querySelector(".no-scrollbar");
  const pane = scroller ? scroller.parentElement : null;
  const paneCs = pane ? getComputedStyle(pane) : null;

  // The actives: rows whose primary line is at full scale (the pane lays every
  // line out at the active size and scales the inactive ones down).
  const activeRows = scroller
    ? [...scroller.children].filter((row) => {
        const inner = row.firstElementChild;
        if (!inner) return false;
        const t = getComputedStyle(inner).transform;
        return t === "none" || /matrix\(1,\s*0,\s*0,\s*1,/.test(t);
      }).length
    : 0;
  const lyricRows = scroller
    ? [...scroller.children].filter((row) => row.firstElementChild && (row.textContent || "").trim()).length
    : 0;

  const toggles = [...root.querySelectorAll('button[aria-label="Toggle the lyrics pane"]')];
  const toggle = toggles[0] || null;
  const row = toggle ? toggle.parentElement : null;

  // Controls actually painted on top of the art: a control counts only when it
  // overlaps the art box AND the hit test at its centre resolves to it, so an
  // element merely sitting behind the art is not a hit.
  const overArt = [];
  if (artBox) {
    for (const el of document.querySelectorAll("button, a, input, [role='button']")) {
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      const ix = Math.max(0, Math.min(r.right, artBox.x + artBox.w) - Math.max(r.left, artBox.x));
      const iy = Math.max(0, Math.min(r.bottom, artBox.y + artBox.h) - Math.max(r.top, artBox.y));
      if (ix * iy < 0.5 * r.width * r.height) continue;
      let hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      let onTop = false;
      while (hit) { if (hit === el) { onTop = true; break; } hit = hit.parentElement; }
      if (onTop) overArt.push(el.getAttribute("title") || el.getAttribute("aria-label") || el.tagName);
    }
  }

  // Layers that PAINT over the art. The ambience stack and the legibility wash
  // are the only things allowed on the art's rectangle, and both sit BEHIND it:
  // they precede the cover in document order, so the cover's own <img> paints
  // last. Anything overlapping the art that comes AFTER it (or lifts itself
  // with a z-index) and actually paints — a fill, a gradient, a backdrop blur,
  // a shadow or a border — is a panel over the artwork, which is exactly the
  // owner's report. The removed `np-veil-pane` layer would land in this list.
  const paints = (cs) =>
    (cs.backgroundColor !== "rgba(0, 0, 0, 0)" && cs.backgroundColor !== "transparent") ||
    cs.backgroundImage !== "none" ||
    cs.backdropFilter !== "none" ||
    cs.boxShadow !== "none" ||
    ["Top", "Right", "Bottom", "Left"].some((s) => parseFloat(cs["border" + s + "Width"]) > 0);
  const paintedOverArt = [];
  if (artBox) {
    for (const el of root.querySelectorAll("*")) {
      if (el === art || art.contains(el)) continue;
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      const ix = Math.max(0, Math.min(r.right, artBox.x + artBox.w) - Math.max(r.left, artBox.x));
      const iy = Math.max(0, Math.min(r.bottom, artBox.y + artBox.h) - Math.max(r.top, artBox.y));
      if (ix * iy < 0.5 * r.width * r.height) continue;
      const after = !!(art.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING);
      const z = getComputedStyle(el).zIndex;
      if (!after && !(z !== "auto" && parseInt(z, 10) > 0)) continue;
      if (!paints(getComputedStyle(el))) continue;
      paintedOverArt.push(String(el.className).slice(0, 70) || el.tagName);
    }
  }

  // What a panel WOULD look like, read off the two surfaces that must not have
  // one: the block that carries the title and the pane that carries the lyrics.
  const bare = (el) => {
    if (!el) return null;
    const cs = getComputedStyle(el);
    return {
      cls: String(el.className).slice(0, 80),
      bg: cs.backgroundColor,
      bgImage: cs.backgroundImage === "none" ? "none" : cs.backgroundImage.slice(0, 50),
      backdrop: cs.backdropFilter,
      shadow: cs.boxShadow,
      border: ["Top", "Right", "Bottom", "Left"].map((s) => cs["border" + s + "Width"]).join("/"),
    };
  };
  // The title rides the marquee (components/ScrollingText), so the
  // `text-2xl` marker is on a span inside it, any tag; the row keeps its
  // fixed `h-8` height, so the metadata block is that row's parent.
  const titleLine = root.querySelector(".text-2xl");

  // The selectors the cutover deleted, read from the BUILT stylesheet (a dead
  // rule would still ship even with no reader in the markup). Matched with a
  // trailing non-identifier so `.np-veil-panel`, which survives, cannot satisfy
  // a search for `.np-veil-pane`.
  let cssText = "";
  for (const sheet of document.styleSheets) {
    try { for (const rule of sheet.cssRules) cssText += rule.cssText + "\n"; } catch { /* cross-origin sheet */ }
  }
  const deadRules = ["np-veil-pane", "np-veil-pill", "np-veil-light", "np-shade-dark"]
    .filter((c) => new RegExp("\\." + c + "(?![\\w-])").test(cssText));

  return {
    art: art ? {
      box: artBox,
      borderWidth: artCs.borderTopWidth,
      borderColor: artCs.borderTopColor,
      outlineStyle: artCs.outlineStyle,
      outlineWidth: artCs.outlineWidth,
      shadow: artCs.boxShadow,
      ring: ringish(artCs.boxShadow),
      focused: document.activeElement === art,
    } : null,
    pane: pane ? {
      box: box(pane),
      hidden: pane.getAttribute("aria-hidden"),
      opacity: Number(paneCs.opacity),
    } : null,
    // The row the two columns live in, and its CONTENT-box centre: "centred"
    // has to mean the middle of the reading width the body actually uses (the
    // safe-area insets are not always symmetric), not the raw viewport middle.
    body: (() => {
      const b = document.querySelector(".safe-np-body");
      if (!b) return null;
      const r = b.getBoundingClientRect();
      const cs = getComputedStyle(b);
      const pl = parseFloat(cs.paddingLeft), pr = parseFloat(cs.paddingRight);
      return { cx: Math.round((r.x + pl + (r.width - pl - pr) / 2) * 10) / 10 };
    })(),
    scroller: scroller ? {
      same: window.__lyrScroller === scroller,
      zoom: scroller.style.zoom,
      scrollTop: Math.round(scroller.scrollTop),
      scrollHeight: scroller.scrollHeight,
      clientHeight: scroller.clientHeight,
    } : null,
    activeRows,
    lyricRows,
    toggleCount: toggles.length,
    toggle: toggle ? {
      title: toggle.getAttribute("title"),
      pressed: toggle.getAttribute("aria-pressed"),
      color: getComputedStyle(toggle).color,
      box: box(toggle),
    } : null,
    rowTitles: row ? [...row.querySelectorAll("button")].map((b) => b.getAttribute("title")) : [],
    rowBox: row ? box(row) : null,
    viewport: { w: window.innerWidth, h: window.innerHeight },
    overArt,
    paintedOverArt,
    meta: bare(titleLine ? (titleLine.closest("div.h-8")?.parentElement ?? null) : null),
    paneBare: bare(scroller),
    deadRules,
  };
});

/** The album card cover wrapper on /library — the same shared CoverImg the
 *  grid, the rows and the player all render through. */
const gridCover = (page) => page.evaluate(() => {
  const img = [...document.querySelectorAll("img")].find(
    (i) => i.closest(".group.relative.rounded-xl") && i.naturalWidth > 0
  );
  if (!img) return { error: "no album card with cover art on this page" };
  const wrapper = img.parentElement;
  const cs = getComputedStyle(wrapper);
  const link = img.closest("a");
  const ringish = cs.boxShadow !== "none" &&
    cs.boxShadow.split(/,(?![^(]*\))/).some((p) => /^rgba?\([^)]*\)\s+0px\s+0px\s+0px\s+(?!0px)/.test(p.trim()));
  return {
    wrapper: {
      borderWidth: cs.borderTopWidth,
      outlineStyle: cs.outlineStyle,
      outlineWidth: cs.outlineWidth,
      ring: ringish,
      shadow: cs.boxShadow,
      cls: String(wrapper.className).slice(0, 120),
    },
    // The card's cover is a link — the focusable element a keyboard user
    // reaches. Its focus ring has to survive the frame removal.
    link: link ? {
      focusable: true,
      outlineStyle: getComputedStyle(link).outlineStyle,
      outlineWidth: getComputedStyle(link).outlineWidth,
    } : { focusable: false },
  };
});

/** Mark the pane scroller so a later read can prove the SAME node survived the
 *  toggle (that is what keeps its scroll position and its zoom). */
const stampScroller = (page) => page.evaluate(() => {
  const root = document.querySelector("div.fixed.inset-0.z-50");
  window.__lyrScroller = root ? root.querySelector(".no-scrollbar") : null;
  return !!window.__lyrScroller;
});

/** Just the pane's box state, for polling while it moves. */
const paneState = (page) => page.evaluate(() => {
  const root = document.querySelector("div.fixed.inset-0.z-50");
  const scroller = root ? root.querySelector(".no-scrollbar") : null;
  const pane = scroller ? scroller.parentElement : null;
  return pane ? {
    w: Math.round(pane.getBoundingClientRect().width),
    h: Math.round(pane.getBoundingClientRect().height),
    opacity: Number(getComputedStyle(pane).opacity),
    hidden: pane.getAttribute("aria-hidden"),
    scrollTop: Math.round(scroller.scrollTop * 10) / 10,
  } : null;
});

/** Click the pane's control and wait for the box to stop moving. The collapse
 *  is a 300 ms transition, and when the pane comes back the follow controller
 *  glides the sung line to its anchor a few frames later — reading the box
 *  before that settles measures the animation, not the design. */
const clickToggle = async (page) => {
  await page.locator('button[aria-label="Toggle the lyrics pane"]').first().click();
  // The box animates width/opacity for 300 ms (max-height snaps, the other
  // two interpolate), and the poll below can see two equal samples at the
  // tail of the curve while the box is still moving. Let the transition run
  // out first, then look for the settled state.
  await sleep(450);
  let last = null;
  let stable = 0;
  for (let i = 0; i < 25; i++) {
    await sleep(120);
    const s = await paneState(page);
    if (last && s && s.w === last.w && s.h === last.h && s.scrollTop === last.scrollTop) {
      if (++stable >= 2) return s;
    } else {
      stable = 0;
    }
    last = s;
  }
  return last;
};

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1568, height: 817 } });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => console.log("PAGE_ERR:", e.message));

  // ---- the library has to offer a track with lyrics ------------------------
  const lib = await (await page.request.get(BASE + "/api/library")).json();
  let album = null;
  let track = null;
  outer:
  for (const ar of lib.artists ?? []) {
    for (const al of ar.albums ?? []) {
      for (const t of (al.tracks ?? []).slice(0, 4)) {
        const tags = await (await page.request.get(`${BASE}/api/tags?path=${encodeURIComponent(t.path)}`)).json().catch(() => null);
        if (typeof tags?.lyrics === "string" && tags.lyrics.trim()) { album = al; track = t; break outer; }
      }
    }
  }
  if (!album) {
    console.log("[fullscreen] skip — no album in this library carries lyrics (the pane cannot be measured)");
    console.log("0/0 checks pass");
    await browser.close();
    process.exit(0);
  }
  const lyricLines = String((await (await page.request.get(`${BASE}/api/tags?path=${encodeURIComponent(track.path)}`)).json()).lyrics).split(/\r?\n/).filter((l) => l.trim()).length;

  // ---- open the fullscreen player on it ------------------------------------
  await page.goto(BASE + "/library", { waitUntil: "networkidle", timeout: 60000 });
  await sleep(1500);
  // The card is found by the album it LINKS to, not by its title text: the
  // folder carries the year ("2020 - First Album") while the card shows the
  // ALBUM tag, and the two are not required to match.
  const played = await page.evaluate((albumPath) => {
    const norm = (s) => String(s).replace(/\\/g, "/").toLowerCase();
    const card = [...document.querySelectorAll("div.group.relative.rounded-xl")].find((c) => {
      const a = c.querySelector("a[href]");
      let href = a ? a.getAttribute("href") || "" : "";
      try { href = decodeURIComponent(href); } catch { /* leave it raw */ }
      return norm(href).includes(norm(albumPath));
    });
    const btn = card?.querySelector('button[title="Play album"]');
    if (!btn) return false;
    btn.click();
    return true;
  }, album.path);
  check("the library grid plays the album that carries lyrics", played, `album ${album.path}`);
  await sleep(1200);
  await page.locator('button[title="Fullscreen player with lyrics"]').first().click();
  await sleep(1500);

  // The scratch library's tracks are a couple of seconds long, so the queue has
  // already run past the lyric track by the time the player is open. Pause, then
  // step back to it — the pane's own control is what appears once the CURRENT
  // track is the lyric one, so this doubles as the check that the control
  // follows the track (rather than being drawn for the album as a whole).
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
  await sleep(600);
  await page.screenshot({ path: `${SHOTS}/player-lyrics.png` });

  const before = await surface(page);
  check("the player is open on the album that carries lyrics", !before.error && !!before.pane && before.pane.box.w > 0,
    before.error || `pane ${before.pane?.box.w}×${before.pane?.box.h} art ${before.art?.box.w}px (${lyricLines} lyric lines)`);

  // ---- the art carries no decorative frame ---------------------------------
  check("the album art wrapper draws no border",
    before.art?.borderWidth === "0px", `border-top ${before.art?.borderWidth} ${before.art?.borderColor}`);
  check("the album art wrapper draws no ring",
    before.art?.ring === false, `box-shadow ${before.art?.shadow?.slice(0, 90)}`);
  check("the album art wrapper has no outline in the default state",
    before.art?.outlineStyle === "none" && !before.art?.focused, `outline ${before.art?.outlineStyle} ${before.art?.outlineWidth}`);

  // ---- ONE lyrics control, in the player's own control row -----------------
  check("the lyrics pane has exactly one control", before.toggleCount === 1, `found ${before.toggleCount}`);
  check("that control sits in the player's control row",
    !!before.toggle && before.rowTitles.includes("Toggle visualizer bars") && before.rowTitles.includes("Up next (queue)"),
    `row carries ${JSON.stringify(before.rowTitles)}`);
  check("the control is styled like its neighbours",
    (before.toggle?.box.w ?? 0) === 36 && before.toggle?.pressed === "true",
    `box ${before.toggle?.box.w}×${before.toggle?.box.h} aria-pressed=${before.toggle?.pressed} colour ${before.toggle?.color}`);

  // ---- the artwork is clear ------------------------------------------------
  check("nothing is painted over the artwork", before.overArt.length === 0, `over the art: ${JSON.stringify(before.overArt)}`);
  check("no layer but the ambience and the legibility wash paints on the art",
    before.paintedOverArt.length === 0, `painted over the art: ${JSON.stringify(before.paintedOverArt)}`);

  // ---- no panel behind the text --------------------------------------------
  // The owner rejected the two grey boxes on the artwork; the lyrics pane's
  // blurred `np-veil-pane` tint and the metadata block's `np-veil-pill` are
  // both gone, and this is what says so from the computed style rather than
  // from the source text.
  check("the metadata block draws no background, blur, shadow or border of its own",
    !!before.meta && before.meta.bg === "rgba(0, 0, 0, 0)" && before.meta.bgImage === "none" &&
      before.meta.backdrop === "none" && before.meta.shadow === "none" && before.meta.border === "0px/0px/0px/0px",
    JSON.stringify(before.meta));
  check("the lyrics pane draws no background, blur or shadow of its own",
    !!before.paneBare && before.paneBare.bg === "rgba(0, 0, 0, 0)" && before.paneBare.bgImage === "none" &&
      before.paneBare.backdrop === "none" && before.paneBare.shadow === "none",
    JSON.stringify(before.paneBare));
  check("the removed veil/pill rules are gone from the built stylesheet",
    before.deadRules.length === 0, `leftover selectors: ${JSON.stringify(before.deadRules)}`);

  // ---- seamless hide ------------------------------------------------------
  const hadTravel = !!before.scroller && before.scroller.scrollHeight > before.scroller.clientHeight + 8;
  await stampScroller(page);
  const zoom = before.scroller?.zoom;
  await clickToggle(page);
  const hidden = await surface(page);
  await page.screenshot({ path: `${SHOTS}/player-lyrics-hidden.png` });

  check("hiding the pane takes it out of the layout",
    !!hidden.pane && hidden.pane.box.w === 0 && hidden.pane.hidden === "true" && hidden.pane.opacity === 0,
    `pane ${hidden.pane?.box.w}×${hidden.pane?.box.h} aria-hidden=${hidden.pane?.hidden} opacity ${hidden.pane?.opacity}`);
  check("hiding the pane keeps the same scroll container (scroll + zoom survive)",
    hidden.scroller?.same === true && hidden.scroller.zoom === zoom && hidden.scroller.zoom === "1.5",
    `same node=${hidden.scroller?.same} zoom ${hidden.scroller?.zoom}`);
  check("the pane really held the right half while it was up",
    before.art.box.cx < before.body.cx - before.pane.box.w / 2,
    `art centre ${before.art.box.cx} vs body centre ${before.body.cx} with a ${before.pane.box.w}px pane`);
  check("the art ends up centred in the reading width, not shoved aside",
    !!hidden.art && Math.abs(hidden.art.box.cx - hidden.body.cx) <= 2,
    `art centre ${hidden.art?.box.cx} vs body centre ${hidden.body?.cx}`);
  check("nothing is painted over the artwork with the pane hidden",
    hidden.overArt.length === 0, `over the art: ${JSON.stringify(hidden.overArt)}`);
  check("the control is still there (and now unpressed) while the pane is hidden",
    hidden.toggleCount === 1 && hidden.toggle?.pressed === "false",
    `count ${hidden.toggleCount} aria-pressed=${hidden.toggle?.pressed}`);

  // ---- seamless show ------------------------------------------------------
  await clickToggle(page);
  const shown = await surface(page);
  await page.screenshot({ path: `${SHOTS}/player-lyrics-back.png` });

  check("showing it again restores the pane in place",
    !!shown.pane && shown.pane.hidden === "false" && shown.pane.box.w > 0 &&
      Math.abs(shown.pane.box.w - before.pane.box.w) <= 2 &&
      Math.abs(shown.pane.box.h - before.pane.box.h) <= 2,
    `pane ${shown.pane?.box.w}×${shown.pane?.box.h} was ${before.pane?.box.w}×${before.pane?.box.h}`);
  check("showing it again reuses the same scroll container",
    shown.scroller?.same === true && shown.scroller.zoom === zoom,
    `same node=${shown.scroller?.same} zoom ${shown.scroller?.zoom}`);
  check("the active line is emphasised exactly once, before and after",
    before.activeRows === 1 && shown.activeRows === 1 && before.lyricRows === shown.lyricRows,
    `active ${before.activeRows}→${shown.activeRows} of ${shown.lyricRows} rows`);
  if (hadTravel) {
    check("the scroll position survives the toggle",
      Math.abs(shown.scroller.scrollTop - before.scroller.scrollTop) <= 4,
      `scrollTop ${before.scroller.scrollTop} → ${shown.scroller.scrollTop}`);
  } else {
    console.log(`note: the pane has no scroll travel with ${lyricLines} lyric lines — the scroll-position check was not applicable`);
  }

  // ---- the same promises at other window sizes ----------------------------
  await page.keyboard.press("Escape");
  await sleep(400);
  for (const [w, h] of [[1440, 900], [1920, 1080]]) {
    await page.setViewportSize({ width: w, height: h });
    await page.locator('button[title="Fullscreen player with lyrics"]').first().click();
    await sleep(1200);
    const s = await surface(page);
    check(`${w}×${h}: nothing over the art, no frame, one control`,
      !s.error && s.overArt.length === 0 && s.art?.borderWidth === "0px" && s.art?.ring === false && s.toggleCount === 1,
      s.error || `over the art: ${JSON.stringify(s.overArt)} border ${s.art?.borderWidth} ring ${s.art?.ring} toggles ${s.toggleCount}`);
    await page.keyboard.press("Escape");
    await sleep(400);
  }
  await page.setViewportSize({ width: 1568, height: 817 });

  // ---- the album grid ------------------------------------------------------
  await page.goto(BASE + "/library", { waitUntil: "networkidle", timeout: 60000 });
  await sleep(1800);
  await page.screenshot({ path: `${SHOTS}/album-grid.png` });
  const grid = await gridCover(page);
  check("the album grid cover draws no border",
    grid.error ? false : grid.wrapper.borderWidth === "0px",
    grid.error || `border-top ${grid.wrapper.borderWidth} outline ${grid.wrapper.outlineStyle} ring ${grid.wrapper.ring} (${grid.wrapper.cls})`);
  check("the album grid cover draws no ring, and keeps its elevation shadow",
    grid.error ? false : grid.wrapper.ring === false,
    grid.error || `box-shadow ${grid.wrapper.shadow.slice(0, 90)}`);

  // The ring that must NOT come back is the decorative one. Keyboard focus on
  // the card's cover link is a different thing and has to survive the removal:
  // reached by Tab, so `:focus-visible` is the state that applies. Hover is
  // measured FIRST — once the link has been focused from the keyboard it stays
  // `:focus-visible`, and a later "hover" read would just re-measure the ring.
  if (grid.link?.focusable) {
    const link = page.locator("div.group.relative.rounded-xl a").first();
    await link.hover();
    const hover = await link.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { matches: el.matches(":focus-visible"), outlineStyle: cs.outlineStyle, outlineWidth: cs.outlineWidth };
    });
    check("a plain hover never draws the focus ring",
      hover.outlineStyle === "none" && !hover.matches,
      `hovered outline ${hover.outlineStyle} ${hover.outlineWidth} :focus-visible=${hover.matches}`);

    // Keyboard focus on the cover link itself: Tab first so the browser's last
    // interaction is a keyboard one (that is what makes a programmatic focus
    // count as `:focus-visible`), then focus the link and read its ring.
    await link.blur();
    await page.keyboard.press("Tab");
    const focused = await link.evaluate((el) => {
      el.focus();
      const cs = getComputedStyle(el);
      return { matches: el.matches(":focus-visible"), outlineStyle: cs.outlineStyle, outlineWidth: cs.outlineWidth };
    });
    check("keyboard focus on the cover link still shows the focus ring",
      focused.matches && focused.outlineStyle === "solid" && parseFloat(focused.outlineWidth) > 0,
      `:focus-visible=${focused.matches} outline ${focused.outlineStyle} ${focused.outlineWidth}`);
  }

  for (const r of results) console.log(`${r.pass ? "ok  " : "FAIL"} ${r.name} :: ${r.detail}`);
  const bad = results.filter((r) => !r.pass).length;
  console.log(`${results.length - bad}/${results.length} checks pass — screenshots in ${SHOTS}/`);
  await browser.close();
  process.exit(bad ? 1 : 0);
})();
