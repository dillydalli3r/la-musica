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
 * The pane's idle cursor is measured here too: after 3 s of stillness over the
 * pane (and over nothing else — a point that carries no control) the pane root
 * computes `cursor: none` and one move brings the arrow back; parking on the
 * pane's OWN controls for the same window keeps the arrow (they opt out of the
 * class in CSS), so does holding the button down anywhere, an open menu over
 * the pane keeps it outright, and the same is re-measured against the shortcut
 * sheet the shell opens on a keystroke — the dialog the pane cannot see in its
 * own state. A coarse-pointer context then pins the other half: with no fine
 * pointer nothing is ever hidden, tap or no tap.
 *
 * The phone has its own section (390×844 and the owner's 566×1040). The report
 * was that the lyrics-mode fullscreen player showed no lyrics on a phone and
 * that the offset / zoom controls the pane carries were nowhere on screen. The
 * cause was one button with two meanings: at `md` and up the lyrics control
 * wrote the persisted pane pick, below `md` the SAME button flipped a compact
 * MODE and wrote nothing, so a phone could not show the pane without unfolding
 * the whole block, the reader's stored pick was ignored there, and the pane's
 * controls never mounted at all. So the pass measures the phone the way a
 * reader meets it: the pane is up with the track's own lines under the compact
 * header, it scrolls without pushing the transport off the viewport, both
 * controls sit under the last line and really move the display (one zoom press
 * = the scroller's inline `zoom` 1.5 → 1.575; a +2.0 s offset takes the
 * emphasis off every line of a two-second track and puts it back), turning
 * lyrics off leaves the plain compact block, and a track with no lyrics draws
 * no pane and no inert control. The one-state rule is taken the long way round
 * there too: the OFF press is what a re-opened player comes back to AND what the
 * same window reads after it is widened to 1440×900, and the wide layout's
 * right-hand column is re-measured at 1440×900 / 1920×1080 while it is at it.
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
 *   PLAYWRIGHT="$PWD/web/node_modules/playwright" \
 *     node tools/check_fullscreen_player.cjs http://127.0.0.1:8011
 *
 * Playwright is required (same resolution as tools/shot.cjs): PLAYWRIGHT is a
 * plain `require()` specifier, so it has to be a path Node can resolve — an
 * absolute one (`web/node_modules/playwright` is NOT, it reads as a package
 * name) — or the bare module name from a directory where it resolves. Exit 2
 * when it is missing, 0 when the library has no album with lyrics (nothing to
 * measure) — that case is named in the output rather than counted as a pass.
 * Screenshots land in .pi/shots-fullscreen-player/. */

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

/** Wait for the pane's box to STOP MOVING — measured in FRAMES, not wall clock.
 *  Opening / closing the pane is a 300 ms transition (a resize moves it too),
 *  and on a starved renderer — a sibling's browser running beside this one —
 *  a timer-based poll can sample the same STALLED frame twice and call a
 *  half-drawn pane settled: `768 px wide, zero tall, opacity 1` was exactly a
 *  pane one frame past the class swap (max-width already `max-w-3xl`, the width
 *  animation not yet run). `requestAnimationFrame` ticks only when a frame is
 *  actually produced, so six identical frames in a row are six PAINTED frames
 *  of the same box — the animation cannot be hiding inside them. */
const settlePane = (page) => page.evaluate(() => new Promise((resolve) => {
  const root = document.querySelector("div.fixed.inset-0.z-50");
  const pane = root ? root.querySelector(".no-scrollbar")?.parentElement ?? null : null;
  if (!pane) return resolve(null);
  const sample = () => {
    const r = pane.getBoundingClientRect();
    return `${Math.round(r.width)}|${Math.round(r.height)}|${getComputedStyle(pane).opacity}|${Math.round(pane.querySelector(".no-scrollbar")?.scrollTop ?? 0)}`;
  };
  let last = "";
  let same = 0;
  let frames = 0;
  const step = () => {
    const s = sample();
    if (s === last) same += 1;
    else { same = 0; last = s; }
    // …and a hard ceiling on frames, so a pane that never stops moving is a
    // measurement of movement rather than a hung run.
    if (same >= 6 || (frames += 1) > 300) return resolve(s);
    requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}));

/** `settlePane` then read — what the phone pass wants after every press. */
const settledPane = async (page) => {
  await settlePane(page);
  return paneState(page);
};

/** What the pane is actually showing as a cursor at an x/y: the pane root's own
 *  computed cursor (the idle class lands there) AND the computed cursor of the
 *  element under the pointer — the pane's own control rows opt back out of
 *  `cursor: none`, so a hidden pane is not a hidden arrow over a button. */
const cursorAt = (page, x, y) => page.evaluate(([x, y]) => {
  const root = document.querySelector("div.fixed.inset-0.z-50");
  const el = document.elementFromPoint(x, y);
  const name = (e) => (e ? String(e.getAttribute?.("aria-label") || e.className || e.tagName).slice(0, 44) : "—");
  return {
    root: root ? getComputedStyle(root).cursor : null,
    under: el ? getComputedStyle(el).cursor : null,
    underName: name(el),
    underInteractive: !!(el && el.closest("button, a, input, select, label, [role='button'], [role='menuitem']")),
  };
}, [x, y]);

/** A point over the pane that carries nothing: no control above it (a button,
 *  link, field or menu row, at any depth), no element that styles its own
 *  cursor — the lyric rows are clickable divs, so they are "live" too — and no
 *  element inside one of the rows that opts OUT of the idle cursor
 *  (`cursor-auto`, the class the transport / seek / chips rows carry). The last
 *  rule matters because this scan is a search for a point where the pane's own
 *  idle state will be visible, and the opt-out rows keep the arrow by design:
 *  with the pointer already past the idle window every other element computes
 *  `cursor: none` and an opt-out row would be the ONLY thing left that looks
 *  "bare". Searched rather than hardcoded: the layout moves with the viewport
 *  and the pane. */
const freeSpot = (page) => page.evaluate(() => {
  const root = document.querySelector("div.fixed.inset-0.z-50");
  if (!root) return null;
  const r = root.getBoundingClientRect();
  for (let yi = 2; yi <= 8; yi++) {
    for (let xi = 1; xi <= 19; xi++) {
      const x = Math.round(r.left + (xi / 20) * r.width);
      const y = Math.round(r.top + (yi / 10) * r.height);
      const el = document.elementFromPoint(x, y);
      if (!el || !root.contains(el)) continue;
      if (el.closest("button, a, input, select, textarea, label, [role='button'], [role='menuitem']")) continue;
      if (el.closest('[class*="cursor-auto"]')) continue;
      if (getComputedStyle(el).cursor !== "auto") continue;
      return { x, y, tag: String(el.className || el.tagName).slice(0, 40) };
    }
  }
  return null;
});

/** Click the pane's control and wait for the box to stop moving. The collapse
 *  is a 300 ms transition, and when the pane comes back the follow controller
 *  glides the sung line to its anchor a few frames later — reading the box
 *  before that settles measures the animation, not the design. Both are covered
 *  by one frame-based wait (the sample carries the scroll position too). */
const clickToggle = async (page) => {
  await page.locator('button[aria-label="Toggle the lyrics pane"]').first().click();
  await settlePane(page);
  return paneState(page);
};

/** Pause playback by pressing the transport — never the Space key: focus sits
 *  on whatever button was pressed last, and a keystroke would re-press THAT
 *  instead of toggling playback. Re-paused after every track step: changing
 *  track resumes playback, and a playing 2-second track walks away from the
 *  state under measurement between two reads. */
const pauseTransport = async (page) => {
  const b = page.locator('div.fixed.inset-0.z-50 button[aria-label="Pause"]').first();
  if (await b.count()) { await b.click(); await sleep(350); }
};

/** Open the fullscreen player on the track that carries lyrics, whatever
 *  width the context is at, and leave it PAUSED there.
 *
 *  Two things are deliberate. The pause is a click on the transport, never the
 *  Space key: focus sits on whatever button was pressed last, and a keystroke
 *  would re-press THAT instead of toggling playback. And the walk back to the
 *  lyric track is driven by the PANE's own presence (`.no-scrollbar` exists
 *  only for a track with lyrics), not by the lyrics control: on a phone the
 *  control used to be drawn for every track — which is exactly the confusion
 *  this pass pins — so "the control is there" could mean "this track has
 *  lyrics" or "this width draws it anyway". The scratch tracks are seconds
 *  long, so the queue has run past the lyric track by the time the player is
 *  open. */
const openOnLyricTrack = async (page, albumPath) => {
  await page.goto(BASE + "/library", { waitUntil: "networkidle", timeout: 60000 });
  await sleep(1500);
  await page.evaluate((p) => {
    const norm = (s) => String(s).replace(/\\/g, "/").toLowerCase();
    const card = [...document.querySelectorAll("div.group.relative.rounded-xl")].find((c) => {
      const a = c.querySelector("a[href]");
      let href = a ? a.getAttribute("href") || "" : "";
      try { href = decodeURIComponent(href); } catch { /* leave it raw */ }
      return norm(href).includes(norm(p));
    });
    card?.querySelector('button[title="Play album"]')?.click();
  }, albumPath);
  await sleep(1200);
  // The bar's fullscreen button is drawn differently per width (and some of its
  // siblings are hidden there), so it is found the way a reader finds it: the
  // visible one.
  const opened = await page.evaluate(() => {
    const b = [...document.querySelectorAll('button[title^="Fullscreen player"]')].find((n) => n.offsetParent !== null);
    if (!b) return false;
    b.click();
    return true;
  });
  await sleep(1500);
  await pauseTransport(page);
  for (let i = 0; i < 8 && !(await page.locator("div.fixed.inset-0.z-50 .no-scrollbar").count()); i++) {
    await page.locator('div.fixed.inset-0.z-50 button[aria-label="Previous track"]').first().click();
    await sleep(700);
    await pauseTransport(page);
  }
  await pauseTransport(page);
  await sleep(400);
  return opened;
};

/** The fullscreen player as a PHONE renders it: the pane's box, the lines it is
 *  showing, the two controls under them, and the chrome that has to stay put
 *  around them. Everything is read from the live DOM — boxes, computed styles,
 *  hit tests — never from the source text. */
const phoneState = (page) => page.evaluate(() => {
  const root = document.querySelector("div.fixed.inset-0.z-50");
  if (!root) return { error: "the fullscreen player is not open" };
  const rectOf = (el) => el.getBoundingClientRect();
  const box = (el) => {
    const r = rectOf(el);
    return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height), bottom: Math.round(r.bottom) };
  };
  /** Fully on screen: a control half outside the viewport is not reachable. */
  const inViewport = (el) => {
    const r = rectOf(el);
    return r.width > 0 && r.height > 0 && r.top >= -0.5 && r.bottom <= window.innerHeight + 0.5 &&
      r.left >= -0.5 && r.right <= window.innerWidth + 0.5;
  };
  /** The pointer's own answer: the centre of the control is what a click would
   *  hit. A box that is on screen but covered (a wash, a shield, the collapsed
   *  box's own `pointer-events-none`) fails here. */
  const reachable = (el) => {
    const r = rectOf(el);
    const hit = document.elementFromPoint(Math.round(r.x + r.width / 2), Math.round(r.y + r.height / 2));
    return !!(hit && (hit === el || el.contains(hit)));
  };
  const scroller = root.querySelector(".no-scrollbar");
  const pane = scroller ? scroller.parentElement : null;
  const toggle = root.querySelector('button[aria-label="Toggle the lyrics pane"]');
  const zoomOut = root.querySelector('button[aria-label="Smaller lyrics"]');
  const zoomIn = root.querySelector('button[aria-label="Larger lyrics"]');
  const zoomBox = root.querySelector('input[aria-label="Lyrics size percentage"]');
  const offMinus = root.querySelector('button[aria-label="Lyrics 0.1 s earlier"]');
  const offPlus = root.querySelector('button[aria-label="Lyrics 0.1 s later"]');
  const offsetRow = offPlus ? offPlus.parentElement : null;
  const offsetLabel = offsetRow ? offsetRow.querySelector("span[title^='Lyric offset']") : null;
  const transport = root.querySelector('button[aria-label="Play"], button[aria-label="Pause"]');
  // The phone's favourite sits in the row pinned under the scrolling body —
  // found through that row, not by its own label: the transport row draws a
  // second heart above `lg` (hidden here, and a zero-size box is not "on
  // screen").
  const heartRow = root.querySelector("div.cursor-auto.lg\\:hidden");
  const heart = heartRow ? heartRow.querySelector("button") : null;
  // The phone's own header row (thumbnail, title, album · artist) and the track
  // title it carries — the one readable name for "which track is this".
  const compactRow = root.querySelector("div.md\\:hidden.w-\\[26rem\\]");
  const headerTitle = compactRow ? compactRow.querySelector("div[title]")?.getAttribute("title") ?? null : null;
  // The lines the pane is actually showing, and how many of them are on screen
  // (the pane renders every line; the ones past the fold are only in the DOM).
  const rows = scroller
    ? [...scroller.children].filter((r) => r.firstElementChild && (r.textContent || "").trim())
    : [];
  const emphasised = rows.filter((r) => {
    const t = getComputedStyle(r.firstElementChild).transform;
    return t === "none" || /matrix\(1,\s*0,\s*0,\s*1,/.test(t);
  }).length;
  const scrollerBox = scroller ? rectOf(scroller) : null;
  const visibleRows = scrollerBox
    ? rows.filter((r) => {
        const b = rectOf(r);
        return b.bottom > scrollerBox.top + 1 && b.top < scrollerBox.bottom - 1;
      }).length
    : 0;
  return {
    viewport: { w: window.innerWidth, h: window.innerHeight },
    pane: pane ? { ...box(pane), hidden: pane.getAttribute("aria-hidden"), opacity: Number(getComputedStyle(pane).opacity) } : null,
    scroller: scroller ? {
      box: box(scroller),
      same: window.__lyrScroller === scroller,
      zoom: scroller.style.zoom,
      scrollTop: Math.round(scroller.scrollTop),
      scrollHeight: scroller.scrollHeight,
      clientHeight: scroller.clientHeight,
      text: (scroller.textContent || "").replace(/\s+/g, " ").trim(),
      rows: rows.length,
      visibleRows,
      emphasised,
      inViewport: inViewport(scroller),
    } : null,
    toggle: toggle ? { title: toggle.getAttribute("title"), pressed: toggle.getAttribute("aria-pressed"), box: box(toggle) } : null,
    zoom: zoomIn ? { box: box(zoomIn), inViewport: inViewport(zoomIn), hit: reachable(zoomIn), value: zoomBox ? zoomBox.value : null } : null,
    offset: offPlus ? { box: box(offPlus), inViewport: inViewport(offPlus), hit: reachable(offPlus), minusHit: offMinus ? reachable(offMinus) : false, label: offsetLabel ? offsetLabel.textContent.trim() : null, save: !!(offsetRow && offsetRow.querySelector('button[title^="Save"], button[aria-label^="Save"]')) } : null,
    compactRow: compactRow ? { box: box(compactRow), visible: compactRow.offsetParent !== null, title: headerTitle } : null,
    transport: transport ? { label: transport.getAttribute("aria-label"), inViewport: inViewport(transport) } : null,
    heart: heart ? { inViewport: inViewport(heart) } : null,
  };
});

/** Press the pane's offset stepper `n` times — one press is one 0.1 s step and
 *  one re-parse of the line times, so the count is the shift. */
const stepOffset = async (page, n, which) => {
  const sel = which === "+" ? 'button[aria-label="Lyrics 0.1 s later"]' : 'button[aria-label="Lyrics 0.1 s earlier"]';
  const b = page.locator(`div.fixed.inset-0.z-50 ${sel}`).first();
  for (let i = 0; i < n; i++) { await b.click(); await sleep(90); }
  await sleep(350);
};

/** Everything a phone fullscreen player must do with lyrics, at one size.
 *
 *  The owner's report: below `md` the lyrics pane was not shown for a reader
 *  who had lyrics ON, and the offset / zoom controls the pane carries were not
 *  on screen anywhere. The cause was ONE button with two meanings — at `md` and
 *  up it wrote the persisted pane pick, below `md` it flipped the compact mode
 *  and wrote nothing — so this pass measures the fix where a reader notices it:
 *  the lines are on screen under the phone's own header, the pane scrolls
 *  without shoving the transport off the viewport, both controls sit under the
 *  last line and really change the display, the OFF press leaves the plain
 *  compact block (and is what a reload comes back to on a DESKTOP too — one
 *  state, both layouts), and a track with no lyrics shows no pane at all. */
const phoneLyricsPass = async (browser, albumPath, lyricText, w, h) => {
  const tag = `${w}×${h}`;
  const ctx = await browser.newContext({ viewport: { width: w, height: h } });
  try {
    const page = await ctx.newPage();
    page.on("pageerror", (e) => console.log(`PHONE PAGE_ERR (${tag}):`, e.message));
    const opened = await openOnLyricTrack(page, albumPath);
    await settledPane(page);
    const start = await phoneState(page);
    await page.screenshot({ path: `${SHOTS}/phone-${w}x${h}-lyrics-on.png` });
    check(`${tag}: the player opens on the track that carries lyrics`,
      opened && !start.error && !!start.scroller && !!start.toggle,
      start.error || `fullscreen button ${opened ? "pressed" : "not found"}, pane ${!!start.scroller}, control ${!!start.toggle} (${start.compactRow?.title ?? "—"})`);
    if (!start.scroller || !start.toggle) return;

    // ---- the persisted pick is honoured below `md` --------------------------
    // Default ON, and the pane is a REAL reading surface: present, in front of
    // assistive tech, and on screen inside the viewport — not a zero-width box
    // parked off the edge as it was.
    check(`${tag}: lyrics ON puts the pane up (the reader's own pick, below md too)`,
      start.toggle.pressed === "true" && start.pane.w > 0 && start.pane.h > 0 &&
        start.pane.hidden === "false" && start.pane.opacity === 1 &&
        start.scroller.inViewport && start.pane.bottom <= start.viewport.h,
      `pressed=${start.toggle.pressed} pane ${start.pane.w}×${start.pane.h} hidden=${start.pane.hidden} opacity ${start.pane.opacity} inViewport=${start.scroller.inViewport}`);
    check(`${tag}: the control is named the same thing it is at md and up`,
      start.toggle.title === "Toggle the lyrics pane", `title "${start.toggle.title}"`);

    // ---- real lyric lines, on screen ---------------------------------------
    // The pane renders what the track stores (timestamps stripped by the
    // parser), so the lines are compared against the track's OWN lyrics text
    // rather than against a fixture or a row count.
    const stored = lyricText
      .split(/\r?\n/)
      .map((l) => l.replace(/\[[^\]]*\]/g, "").trim())
      .filter(Boolean);
    const rendered = stored.filter((l) => start.scroller.text.includes(l));
    check(`${tag}: the pane is showing this track's own lyric lines, on screen`,
      rendered.length >= 2 && start.scroller.visibleRows >= 2 && start.scroller.emphasised === 1,
      `${rendered.length}/${stored.length} stored lines rendered, ${start.scroller.visibleRows} of ${start.scroller.rows} rows on screen, ${start.scroller.emphasised} emphasised`);
    check(`${tag}: the pane ends up full width of the phone, not a sliver`,
      start.pane.w >= start.viewport.w - 32 && start.scroller.box.h >= 150,
      `pane ${start.pane.w} of ${start.viewport.w} wide, reading box ${start.scroller.box.h} tall`);

    // ---- it scrolls, and the chrome stays put ------------------------------
    const travel = start.scroller.scrollHeight - start.scroller.clientHeight;
    await page.evaluate(() => {
      const s = document.querySelector("div.fixed.inset-0.z-50 .no-scrollbar");
      if (s) s.scrollTop = s.scrollHeight;
    });
    await sleep(300);
    const scrolled = await phoneState(page);
    check(`${tag}: the lyrics scroll inside their own box`,
      travel > 8 && scrolled.scroller.scrollTop > 0 && scrolled.scroller.scrollTop <= travel + 1,
      `travel ${travel}px, scrollTop 0 → ${scrolled.scroller.scrollTop}`);
    check(`${tag}: the transport and the heart stay on the phone's screen`,
      !!scrolled.transport && scrolled.transport.inViewport && !!scrolled.heart && scrolled.heart.inViewport &&
        !!scrolled.compactRow && scrolled.compactRow.visible,
      `transport ${scrolled.transport?.label} inViewport=${scrolled.transport?.inViewport}, heart ${scrolled.heart?.inViewport}, compact header ${scrolled.compactRow?.visible}`);

    // ---- the two controls, at the BOTTOM of the words ----------------------
    const bottomEdge = scrolled.pane.y + scrolled.pane.h / 2;
    check(`${tag}: the zoom control is on the pane's bottom edge and clickable`,
      !!scrolled.zoom && scrolled.zoom.hit && scrolled.zoom.inViewport && scrolled.zoom.box.y > bottomEdge,
      scrolled.zoom ? `at y=${scrolled.zoom.box.y} of a pane ${scrolled.pane.y}..${scrolled.pane.bottom} (hit=${scrolled.zoom.hit}, on screen=${scrolled.zoom.inViewport})` : "not rendered");
    check(`${tag}: the offset control is beside it and clickable`,
      !!scrolled.offset && scrolled.offset.hit && scrolled.offset.inViewport && scrolled.offset.minusHit && scrolled.offset.box.y > bottomEdge,
      scrolled.offset ? `at y=${scrolled.offset.box.y}, label ${scrolled.offset.label} (hit=${scrolled.offset.hit}/${scrolled.offset.minusHit}, on screen=${scrolled.offset.inViewport})` : "not rendered");
    if (!scrolled.zoom || !scrolled.offset) return;

    // The zoom has to MOVE the lines, not just its own label: the pane takes
    // the multiplier as an inline `zoom` on the scroller, so the computed value
    // is the measurement. One press is 5 % of the surface's base (1.5).
    const before = scrolled.scroller.zoom;
    await page.locator('div.fixed.inset-0.z-50 button[aria-label="Larger lyrics"]').first().click();
    await sleep(350);
    const bigger = await phoneState(page);
    await page.locator('div.fixed.inset-0.z-50 button[aria-label="Smaller lyrics"]').first().click();
    await sleep(350);
    const back = await phoneState(page);
    check(`${tag}: the zoom control really resizes the lyrics`,
      before === "1.5" && bigger.scroller.zoom === "1.575" && bigger.zoom.value === "105" &&
        back.scroller.zoom === "1.5" && back.zoom.value === "100",
      `scroller zoom ${before} → ${bigger.scroller.zoom} (${bigger.zoom.value}%) → ${back.scroller.zoom} (${back.zoom.value}%)`);

    // The offset's own preview: two seconds of shift is past EVERY line of a
    // two-second scratch track, so the emphasis has to leave the words — the
    // display follows the buttons with no round trip and no file write. (20
    // presses of 0.1 s; the Save control appears only once there is something
    // to save, and it is deliberately never pressed here — that rewrites the
    // track's stored lyrics.)
    const preOffset = back.scroller.emphasised;
    await stepOffset(page, 20, "+");
    const shifted = await phoneState(page);
    await stepOffset(page, 20, "-");
    const restored = await phoneState(page);
    check(`${tag}: the offset control shifts the pending sync, and the display follows`,
      preOffset === 1 && shifted.offset.label === "+2.0s" && shifted.offset.save &&
        shifted.scroller.emphasised === 0 && restored.offset.label === "0.0s" &&
        !restored.offset.save && restored.scroller.emphasised === 1,
      `emphasised ${preOffset} → ${shifted.scroller.emphasised} (${shifted.offset.label}, save=${shifted.offset.save}) → ${restored.scroller.emphasised} (${restored.offset.label})`);

    // ---- OFF leaves the plain compact block, and it STICKS ----------------
    await page.locator('div.fixed.inset-0.z-50 button[aria-label="Toggle the lyrics pane"]').first().click();
    await settledPane(page);
    const off = await phoneState(page);
    await page.screenshot({ path: `${SHOTS}/phone-${w}x${h}-lyrics-off.png` });
    const persisted = await page.evaluate(() => localStorage.getItem("mlo.np.lyrics"));
    check(`${tag}: turning the lyrics off puts the pane away and brings the compact block back`,
      off.pane.w === 0 && off.pane.hidden === "true" && off.pane.opacity === 0 &&
        !off.zoom && !off.offset && off.toggle.pressed === "false" &&
        !!off.compactRow && off.compactRow.visible && !!off.transport && off.transport.inViewport,
      `pane ${off.pane.w}×${off.pane.h} hidden=${off.pane.hidden} controls ${!!off.zoom}/${!!off.offset} pressed=${off.toggle.pressed} compact header ${off.compactRow?.visible}`);
    check(`${tag}: that press is the persisted pick (the same key every width reads)`,
      persisted === "0", `localStorage mlo.np.lyrics = ${JSON.stringify(persisted)}`);

    // Closing and re-opening the player is where a phone-only mode used to die:
    // the pane state must come back from the READER's pick, and the same pick
    // must be what the wide layout reads — that is the whole point of one state
    // and one derivation. Widening the very same window is the sharpest form of
    // the question, because nothing else about the player changes.
    await page.keyboard.press("Escape");
    await sleep(500);
    const reopened = await openOnLyricTrack(page, albumPath);
    await settledPane(page);
    const after = await phoneState(page);
    check(`${tag}: the OFF press survives closing and re-opening the player`,
      reopened && !after.error && after.toggle && after.toggle.pressed === "false" &&
        after.pane.w === 0 && after.pane.hidden === "true",
      after.error || `reopened=${reopened} pressed=${after.toggle?.pressed} pane ${after.pane?.w}×${after.pane?.h}`);
    await page.setViewportSize({ width: 1440, height: 900 });
    await settledPane(page);
    const wide = await phoneState(page);
    check(`${tag} → 1440×900: the phone's OFF press is the desktop's OFF too (one state)`,
      !wide.error && !!wide.toggle && wide.toggle.pressed === "false" && wide.pane.w === 0 &&
        wide.pane.hidden === "true" && !wide.compactRow,
      wide.error || `pressed=${wide.toggle?.pressed} pane ${wide.pane?.w}×${wide.pane?.h} compact header ${!!wide.compactRow}`);
    await page.locator('div.fixed.inset-0.z-50 button[aria-label="Toggle the lyrics pane"]').first().click();
    await settledPane(page);
    const wideOn = await phoneState(page);
    check(`${tag} → 1440×900: ON at the desktop width, and back at ${tag}`,
      wideOn.pane.w > 0 && wideOn.pane.hidden === "false" && !!wideOn.zoom && wideOn.zoom.hit,
      `pane ${wideOn.pane?.w}×${wideOn.pane?.h} hidden=${wideOn.pane?.hidden} zoom ${!!wideOn.zoom}`);
    await page.setViewportSize({ width: w, height: h });
    await settledPane(page);
    const narrow = await phoneState(page);
    check(`${tag}: the desktop press follows the reader back down to the phone`,
      narrow.pane.w > 0 && narrow.pane.hidden === "false" && !!narrow.compactRow && narrow.compactRow.visible &&
        !!narrow.zoom && narrow.zoom.hit && narrow.offset.hit,
      `pane ${narrow.pane?.w}×${narrow.pane?.h} compact header ${narrow.compactRow?.visible} controls ${!!narrow.zoom}/${!!narrow.offset}`);
    await page.screenshot({ path: `${SHOTS}/phone-${w}x${h}-lyrics-back.png` });

    // ---- a track with NO lyrics says so honestly ---------------------------
    // The scratch album's next track carries none. Nothing is drawn for it: no
    // pane (not an empty one), no control that could not do anything — the
    // phone's compact header with the track's own name is the whole screen.
    const lyricTrackTitle = after.compactRow?.title ?? start.compactRow?.title ?? null;
    let plain = null;
    for (let i = 0; i < 4; i++) {
      await page.locator('div.fixed.inset-0.z-50 button[aria-label="Next track"]').first().click();
      // A track step starts playing again, and a playing two-second track walks
      // out from under the next read — so pause before looking.
      await sleep(700);
      await pauseTransport(page);
      await settledPane(page);
      const s = await phoneState(page);
      if (s.error) { plain = s; break; }
      if (s.compactRow?.title && s.compactRow.title !== lyricTrackTitle && !s.scroller && !s.toggle) { plain = s; break; }
      plain = s;
    }
    check(`${tag}: a track with no lyrics draws no pane and no inert control`,
      !!plain && !plain.error && !plain.scroller && !plain.toggle && plain.pane === null &&
        !!plain.compactRow && plain.compactRow.visible && plain.compactRow.title !== lyricTrackTitle &&
        plain.transport?.inViewport === true,
      plain?.error || `track "${plain?.compactRow?.title}" (was "${lyricTrackTitle}"): pane ${!!plain?.scroller} control ${!!plain?.toggle} header ${plain?.compactRow?.visible} transport ${plain?.transport?.inViewport}`);
    await page.screenshot({ path: `${SHOTS}/phone-${w}x${h}-no-lyrics.png` });
  } catch (e) {
    // A context that cannot be driven is a FAILED check, not a lost run: the
    // results already collected still have to reach the console.
    check(`${tag}: the phone pass could be driven`,
      false, `could not complete the pass: ${String(e.message || e).split("\n")[0].slice(0, 140)}`);
  } finally {
    await ctx.close();
  }
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
  const lyricText = String((await (await page.request.get(`${BASE}/api/tags?path=${encodeURIComponent(track.path)}`)).json()).lyrics);
  const lyricLines = lyricText.split(/\r?\n/).filter((l) => l.trim()).length;

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

  // ---- the idle cursor ----------------------------------------------------
  // The owner's ask: "the cursor goes away if it doesn't move for a while".
  // Read as COMPUTED style, at a point that carries nothing, so what is being
  // measured is the pane's own idle state and never a control's own cursor.
  const IDLE = 3000; // IDLE_CURSOR_MS in components/NowPlayingView.tsx
  // Wake the pointer first: everything above ran without moving it, so the pane
  // may already be idling — and while it idles the ONLY elements computing
  // `cursor: auto` are the rows that opt out of it, which is the opposite of the
  // bare spot this search is for.
  await page.mouse.move(60, 60);
  await sleep(300);
  const spot = await freeSpot(page);
  check("the pane has a bare spot to idle the pointer over", !!spot,
    spot ? `(${spot.x},${spot.y}) over ${spot.tag}` : "no point outside the pane's own controls");
  if (spot) {
    await page.mouse.move(spot.x, spot.y);
    const awake = await cursorAt(page, spot.x, spot.y);
    check("with the pointer in use the pane shows the arrow",
      awake.root !== "none" && awake.under !== "none",
      `pane ${awake.root} / under ${awake.under} (${awake.underName})`);
    await page.screenshot({ path: `${SHOTS}/cursor-awake.png` });

    await sleep(IDLE + 1200);
    const idleCursor = await cursorAt(page, spot.x, spot.y);
    check(`the pane drops the pointer after ${IDLE / 1000} s of stillness`,
      idleCursor.root === "none" && idleCursor.under === "none",
      `pane ${idleCursor.root} / under ${idleCursor.under} (${idleCursor.underName})`);
    await page.screenshot({ path: `${SHOTS}/cursor-idle.png` });

    await page.mouse.move(spot.x + 30, spot.y + 20);
    const back = await cursorAt(page, spot.x + 30, spot.y + 20);
    check("one move brings the arrow straight back",
      back.root !== "none" && back.under !== "none",
      `pane ${back.root} / under ${back.under} (${back.underName})`);

    // Stillness ON a control is not idleness: park on the play button and let
    // the whole window pass. The pane still idles (that is what makes this a
    // measurement of the opt-out rather than of a dead timer), but the row the
    // pointer is on keeps the arrow.
    const play = await page.locator('div.fixed.inset-0.z-50 button[aria-label="Play"], div.fixed.inset-0.z-50 button[aria-label="Pause"]').first().boundingBox();
    const px = Math.round(play.x + play.width / 2);
    const py = Math.round(play.y + play.height / 2);
    await page.mouse.move(px, py);
    await sleep(IDLE + 1200);
    const onControl = await cursorAt(page, px, py);
    check("a still pointer resting ON the pane's controls keeps the arrow",
      onControl.root === "none" && onControl.under !== "none" && onControl.underInteractive,
      `pane ${onControl.root} / under ${onControl.under} (${onControl.underName}) interactive=${onControl.underInteractive}`);

    // A held button is a gesture in progress, not idleness: hold the pointer
    // perfectly still — ON the same bare spot, so nothing else can explain it —
    // for the whole window and the pane must keep the arrow. This is the scrub
    // drag, where a hidden cursor is the bug.
    await page.mouse.move(spot.x, spot.y);
    await page.mouse.down();
    await sleep(IDLE + 1200);
    const heldStill = await cursorAt(page, spot.x, spot.y);
    check("a held pointer (a drag in progress) keeps the arrow",
      heldStill.root !== "none" && heldStill.under !== "none" && !heldStill.underInteractive,
      `pane ${heldStill.root} / under ${heldStill.under} (${heldStill.underName})`);
    await page.mouse.up();
    await sleep(IDLE + 1200);
    const released = await cursorAt(page, spot.x, spot.y);
    check("...and releasing it lets the pane idle again",
      released.root === "none" && released.under === "none",
      `pane ${released.root} / under ${released.under} (${released.underName})`);

    // A menu opened over the pane brings the arrow back and keeps it: the
    // player's own options menu, opened and left up past the window.
    const optionsBtn = 'div.fixed.inset-0.z-50 button[aria-label="Lyrics and display options"]';
    await page.locator(optionsBtn).first().click();
    await sleep(IDLE + 1200);
    const menu = await cursorAt(page, spot.x, spot.y);
    check("an open menu over the pane keeps the arrow",
      menu.root !== "none" && menu.under !== "none",
      `pane ${menu.root} / under ${menu.under} (${menu.underName})`);
    await page.screenshot({ path: `${SHOTS}/cursor-menu.png` });
    // Put it away the way the popover expects: a press anywhere else (while it
    // is up its shield owns the whole viewport, so its own trigger is not
    // clickable from outside).
    await page.mouse.click(spot.x, spot.y);
    await sleep(500);
    check("the options menu closes again",
      (await page.locator('div.fixed.inset-0.z-50 [role="menu"]').count()) === 0, "no menu left up");

    // The dialog the pane cannot see in its own state: the shortcut sheet
    // belongs to the shell and opens on a keystroke — exactly the case where
    // "the pointer moved recently" is not a safe proxy for "nothing on screen
    // is waiting for it".
    await page.keyboard.press("?");
    await sleep(500);
    const sheetUp = await page.locator('[aria-modal="true"]').count();
    check("the shortcut sheet opens over the pane (the keystroke case)", sheetUp > 0, `dialogs up: ${sheetUp}`);
    await sleep(IDLE + 1200);
    const overSheet = await cursorAt(page, spot.x, spot.y);
    check("the arrow stays while a dialog the pane does not own is open",
      overSheet.root !== "none" && overSheet.under !== "none",
      `pane ${overSheet.root} / under ${overSheet.under} (${overSheet.underName})`);
    const sheetBox = await page.locator('[aria-modal="true"]').first().boundingBox();
    if (sheetBox) {
      const sx = Math.round(sheetBox.x + sheetBox.width / 2);
      const sy = Math.round(sheetBox.y + sheetBox.height / 2);
      const overPanel = await cursorAt(page, sx, sy);
      check("...and the dialog's own surface is not hidden either",
        overPanel.under !== "none", `under ${overPanel.under} (${overPanel.underName})`);
    }
    await page.locator('[aria-modal="true"] button[aria-label="Close"]').first().click();
    await sleep(500);
    check("closing the sheet takes it out of the way",
      (await page.locator('[aria-modal="true"]').count()) === 0, "the sheet is gone");

    // The dialog guard must not latch: with it closed the pane idles again.
    await page.mouse.move(spot.x, spot.y);
    await sleep(IDLE + 1200);
    const again = await cursorAt(page, spot.x, spot.y);
    check("with the sheet closed the pane idles again",
      again.root === "none" && again.under === "none",
      `pane ${again.root} / under ${again.under} (${again.underName})`);
    await page.mouse.move(spot.x + 30, spot.y + 20); // leave it awake for what follows
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
    // …and the pane is still the DESKTOP's right-hand column, driven by the same
    // pick the phone press writes (one state, one derivation): the art ends up
    // left of the reading width's centre with the pane's own half beside it.
    check(`${w}×${h}: the pane is still the wide layout's right-hand column`,
      !s.error && !!s.pane && s.pane.hidden === "false" && s.pane.box.w > 0 &&
        s.toggle?.pressed === "true" && s.art.box.cx < s.body.cx - s.pane.box.w / 2,
      s.error || `pane ${s.pane?.box.w}×${s.pane?.box.h} hidden=${s.pane?.hidden} art centre ${s.art?.box.cx} vs body centre ${s.body?.cx} pressed=${s.toggle?.pressed}`);
    await page.keyboard.press("Escape");
    await sleep(400);
  }

  // ---- the same promises on a PHONE ----------------------------------------
  // The owner's report, measured: at 390×844 (a phone) and at 566×1040 (their
  // own window) the lyrics control has to put REAL lines on screen under the
  // phone's compact header, the pane has to carry the offset / zoom controls at
  // its bottom edge where a thumb can reach them, turning it off has to leave
  // the plain compact block, and a track with no lyrics has to draw no pane at
  // all. The pass also proves the one-state rule the long way round: the OFF
  // press taken on the phone is what a RELOAD comes back to, and what the SAME
  // window reads after it is widened to 1440×900 — the case that used to fail,
  // because the phone press wrote nothing and the desktop only ever read
  // `showLyrics`.
  for (const [w, h] of [[390, 844], [566, 1040]]) {
    await phoneLyricsPass(browser, album.path, lyricText, w, h);
  }
  await page.setViewportSize({ width: 1568, height: 817 });

  // ---- closed means gone ---------------------------------------------------
  // The pane is a portal the player bar mounts only while it is open, so its
  // idle effect (listeners and timer) has to die with it — and it must leave
  // nothing hidden behind: no pane in the DOM, and not one element anywhere in
  // the shell computing `cursor: none`.
  const closed = await page.evaluate(() => ({
    panes: document.querySelectorAll("div.fixed.inset-0.z-50").length,
    hidden: [...document.querySelectorAll("body *")].filter((el) => getComputedStyle(el).cursor === "none").length,
  }));
  check("with the player closed no pane is left mounted and nothing is hidden",
    closed.panes === 0 && closed.hidden === 0,
    `panes ${closed.panes}, cursor:none elements ${closed.hidden}`);

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

  // ---- a coarse pointer has no arrow to drop ------------------------------
  // The other half of the rule: where there is no fine pointer nothing is ever
  // hidden, so a tap (or a finger-scroll) cannot leave the pane in
  // `cursor: none`. A separate context, because `isMobile` is what makes
  // Chromium report the coarse pointer the guard reads.
  const touchCtx = await browser.newContext({
    viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 3,
  });
  try {
  const tPage = await touchCtx.newPage();
  tPage.on("pageerror", (e) => console.log("TOUCH PAGE_ERR:", e.message));
  await tPage.goto(BASE + "/library", { waitUntil: "networkidle", timeout: 60000 });
  await sleep(1500);
  const finePointer = await tPage.evaluate(() => window.matchMedia("(hover: hover) and (pointer: fine)").matches);
  check("a coarse-pointer context reports no fine pointer (the guard's precondition)",
    finePointer === false, `(hover: hover) and (pointer: fine) matches ${finePointer}`);
  // Same path as the desktop run, driven from the page: the grid card's and the
  // bar's buttons are reachable by script at any width even where the phone
  // layout reveals them differently.
  const phoneOpened = await tPage.evaluate((albumPath) => {
    const norm = (s) => String(s).replace(/\\/g, "/").toLowerCase();
    const card = [...document.querySelectorAll("div.group.relative.rounded-xl")].find((c) => {
      const a = c.querySelector("a[href]");
      let href = a ? a.getAttribute("href") || "" : "";
      try { href = decodeURIComponent(href); } catch { /* leave it raw */ }
      return norm(href).includes(norm(albumPath));
    });
    const play = card?.querySelector('button[title="Play album"]');
    if (!play) return "no Play album on the card";
    play.click();
    return "";
  }, album.path);
  await sleep(1500);
  const fsPressed = await tPage.evaluate(() => {
    const b = [...document.querySelectorAll('button[title^="Fullscreen player"]')].find((n) => n.offsetParent !== null);
    if (!b) return false;
    b.click();
    return true;
  });
  await sleep(1500);
  check("the pane opens on the phone-sized coarse-pointer context",
    phoneOpened === "" && fsPressed && (await tPage.locator("div.fixed.inset-0.z-50").count()) > 0,
    phoneOpened || `fullscreen button ${fsPressed ? "pressed" : "not found"}, panes ${await tPage.locator("div.fixed.inset-0.z-50").count()}`);
  await tPage.touchscreen.tap(200, 300);
  await sleep(IDLE + 1200);
  const tapped = await cursorAt(tPage, 200, 300);
  check("a tap never leaves the pane in cursor: none",
    tapped.root === "auto" && tapped.under !== "none",
    `pane ${tapped.root} / under ${tapped.under} (${tapped.underName})`);
  await tPage.screenshot({ path: `${SHOTS}/cursor-touch.png` });
  } catch (e) {
    // A context that cannot be driven is a FAILED check, not a lost run: the
    // results collected so far still have to reach the console (this was found
    // the hard way — a backend that went away mid-run took the whole report
    // with it).
    check("the coarse-pointer context could be driven",
      false, `could not complete the pass: ${String(e.message || e).split("\n")[0].slice(0, 120)}`);
  } finally {
    await touchCtx.close();
  }

  for (const r of results) console.log(`${r.pass ? "ok  " : "FAIL"} ${r.name} :: ${r.detail}`);
  const bad = results.filter((r) => !r.pass).length;
  console.log(`${results.length - bad}/${results.length} checks pass — screenshots in ${SHOTS}/`);
  await browser.close();
  process.exit(bad ? 1 : 0);
})();
