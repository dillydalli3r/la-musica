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

/* Every route the shell can reach WITHOUT any data in it, plus (below) the ones
 * a phone user reaches from a list. The tail of this list used to be missing —
 * `/equalizer`, `/charts`, `/recommended`, `/discover`, `/watched`, `/checks`,
 * `/in-progress`, and every entity page (`/artist/:path`, `/album/:path`,
 * `/track/:path`, `/playlist/:id`) — and that blind spot is exactly where the
 * playlist hero shipped a row that never folded: 224 px of mosaic beside the
 * title at 390 px, with nothing overflowing, nothing scrolling sideways and no
 * word crushed, so a geometry sweep that never opened the route could not have
 * seen it. */
const ROUTES = [
  "/", "/library", "/genres", "/favorites/tracks", "/downloads", "/trash",
  "/playlists", "/soulseek", "/import", "/export", "/optimize", "/grading",
  "/dependencies", "/settings", "/donations", "/setup", "/equalizer", "/charts",
  "/recommended", "/discover", "/watched", "/checks", "/in-progress",
];

/* The entity routes only exist against a library that HAS the row, so they are
 * discovered from the running server instead of hard-coded: one artist, one
 * album, one track and one playlist is all it takes, and a server with no
 * library contributes none of them. `hero` marks the routes whose page header
 * is the shared `.hero-flat` block — the fold section below measures those. */
async function entityRoutes() {
  const out = [];
  const get = async (path) => {
    const r = await fetch(`${BASE}${path}`);
    if (!r.ok) throw new Error(`${path} → ${r.status}`);
    return r.json();
  };
  try {
    const lib = await get("/api/library");
    const artists = lib.artists ?? [];
    const artist = artists.find((a) => a.path);
    const album = artists.flatMap((a) => a.albums ?? []).find((al) => al.path);
    const track = artists
      .flatMap((a) => a.albums ?? [])
      .flatMap((al) => al.tracks ?? [])
      .find((t) => t.path);
    if (artist) out.push({ route: `/artist/${encodeURIComponent(artist.path)}`, hero: true });
    if (album) out.push({ route: `/album/${encodeURIComponent(album.path)}`, hero: true });
    if (track) out.push({ route: `/track/${encodeURIComponent(track.path)}`, hero: false });
  } catch { /* no library on this server: the static routes are all there is */ }
  try {
    const body = await get("/api/playlists");
    const first = (Array.isArray(body) ? body : body.playlists ?? [])[0];
    if (first?.id != null) out.push({ route: `/playlist/${first.id}`, hero: true });
  } catch { /* no playlists yet */ }
  return out;
}

const results = [];
const check = (name, pass, detail) => results.push({ name, pass: !!pass, detail: String(detail) });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Wait until a box stops moving, then read it.
 *
 *  A dialog animates in (`.anim-pop` scales and translates the panel, the phone
 *  sheet slides up), so the FIRST measurement after opening it can land on a
 *  frame that is still in flight — 6px of `pop-in` is exactly the tolerance the
 *  "still centred" assertion works in, and a starved renderer can serve that
 *  same frame to two consecutive runs. Sampled on a TIMER rather than rAF (a
 *  stalled rAF is the starvation being defended against), two agreeing reads,
 *  capped at 2s so a genuinely misplaced dialog still fails rather than hangs.
 *  A moving value is not a wrong one: this is what made the dialog sweep flaky
 *  the same way the nav active-state read was. */
const settleBox = async (el) => {
  const read = () => {
    const r = el.getBoundingClientRect();
    return [r.left, r.top, r.width, r.height].map((n) => Math.round(n * 10) / 10).join(",");
  };
  const deadline = performance.now() + 2000;
  let last = null;
  let stable = 0;
  let v = read();
  while (performance.now() < deadline) {
    await new Promise((r) => setTimeout(r, 50));
    v = read();
    if (v === last) {
      if (++stable >= 2) break;
    } else {
      stable = 0;
      last = v;
    }
  }
  return v;
};

/* ---- the controls that stay small on purpose, per page ------------------
 *
 * The phone tap floor (28px here; the app's own `.tap`/`.btn-icon*` recipes aim
 * at 44) is a rule about ACTIONS. A handful of real controls cannot take a
 * bigger box without costing more than it buys, and each one is named here
 * with the size it really has and the reason it stays that way — an allow-list
 * inside the check, not a wider floor: the phone pass asserts that the small
 * controls it FINDS are exactly this set, so a new small control fails and
 * names itself, and an entry that stops being small (or disappears) fails too
 * instead of rotting.
 *
 * Keyed by route PREFIX, because the entity routes carry the library's own
 * paths (`/album/F%3A%2Ftmp%2F…`): a page's density is a property of the page.
 * `match` is a prefix of the control's name (aria-label / title / text) or of
 * its class list, whichever identifies it; `h` is the measured painted height
 * (px) at 390 wide. */
const SMALL_AT_PHONE = {
  "/artist": [
    { match: "look for one or upload your own", h: 16,
      why: "prose action inside the 'No artist image yet — …' sentence: a 44px box would re-flow the paragraph it is a word of" },
    { match: "fetch one", h: 16,
      why: "prose action inside the description sentence (same paragraph as 'write your own')" },
    { match: "write your own", h: 16,
      why: "prose action inside the description sentence" },
  ],
  "/album": [
    { match: "inline-flex items-center gap-1 cursor-pointer", h: 20,
      why: "SortHeader's table-head button: a 44px box would overlap the neighbouring column's sort target in the fixed table" },
    { match: "!p-1 text-zinc-500 hover:text-white", h: 20,
      why: "a track row's own actions button: rows are ~30px tall, so an expanded target would overlap the rows above and below" },
    { match: "Rate ", h: 20,
      why: "the rating strip's half-star zones (two per star, one for a half and one for a whole): expanded targets would overlap each other and make a half-tap ambiguous" },
    { match: "text-[9px] text-red-400/70", h: 14,
      why: "a tag-issue badge in a table cell: the badges sit a few px apart, so an expanded target would steal its neighbour's taps" },
    { match: "fetch one", h: 16,
      why: "prose action inside the description sentence" },
    { match: "write your own", h: 16,
      why: "prose action inside the description sentence" },
  ],
  "/track": [
    { match: "Seek to", h: 16,
      why: "a lyrics line's seek glyph, one per line: a 44px box per glyph would triple the height of a 100-line sheet" },
    { match: "Add line after", h: 14,
      why: "lyrics line glyph, flush against the remove glyph beside it (overlapping targets would mis-tap)" },
    { match: "Remove line", h: 14,
      why: "lyrics line glyph, flush against the add glyph beside it" },
  ],
};

/** The allow-list for a route: entity routes carry the library's own paths, so
 *  a page is matched by its prefix. */
function smallAllowedFor(route) {
  const key = Object.keys(SMALL_AT_PHONE).find((p) => route === p || route.startsWith(`${p}/`));
  return key ? SMALL_AT_PHONE[key] : [];
}

/* ONE PAGE'S HERO, measured in the page — the fold, not the overflow.
 *
 * The album, artist and playlist heroes are the same two-part block: a cover
 * and the identity beside it, which folds to a column below `sm`. The playlist
 * hero shipped WITHOUT that fold — `flex items-start gap-5` around a fixed
 * `h-56 w-56` mosaic — and nothing else here could see it: at 390 px nothing
 * overflowed, nothing scrolled sideways and no word was crushed; the identity
 * block was simply squeezed to ~90 px beside a 224 px tile. So the assertion is
 * the fold itself: stacked at 390 (cover above the identity, the identity at
 * the page's own left edge and width) and side by side at 1440.
 *
 * The row is found structurally — the hero's first div child that holds the two
 * halves — so it survives class renames, which is the point: this measures the
 * SHAPE the three pages share, not one page's spelling of it. */
const FOLD_MEASURE = `(() => {
  const hero = document.querySelector(".hero-flat");
  if (!hero) return { found: false };
  const row = [...hero.children].find((el) => el.tagName === "DIV" && el.children.length >= 2);
  if (!row) return { found: false };
  const box = (el) => {
    const r = el.getBoundingClientRect();
    return {
      left: Math.round(r.left), right: Math.round(r.right),
      top: Math.round(r.top), bottom: Math.round(r.bottom),
      width: Math.round(r.width), height: Math.round(r.height),
    };
  };
  return { found: true, vw: window.innerWidth, cover: box(row.children[0]), body: box(row.children[1]) };
})()`;

/* Dialogs, swept at the same three widths as the pages. A dialog is a page on
 * top of a page and has its own way of being unusable — off the window's edge,
 * taller than the screen, or with its confirm row below the fold — and that
 * grew a second form on a phone: `Modal` becomes a bottom sheet below `sm` and
 * keeps the centred panel above it. Both forms have to fit, and the desktop
 * one has to stay the desktop one.
 * Two dialogs, reachable without any library data: the shortcut sheet (the `?`
 * binding, on every route) and Browse's "Save as smart playlist" form — a
 * header, a field and a two-button footer, which is the shape every other
 * dialog in the app shares. */
const DIALOGS = [
  { name: "shortcuts dialog", route: "/", hint: "?", open: (page) => page.keyboard.press("?") },
  {
    name: "form dialog",
    route: "/browse",
    hint: "Save as smart playlist",
    open: (page) => page.locator('.btn:has-text("Save as smart playlist")').first().click(),
  },
];

/* One open dialog, measured in the page. */
const DIALOG_MEASURE = `(() => {
  const panel = document.querySelector('[role="dialog"][aria-modal="true"]');
  if (!panel) return { open: false };
  const vw = window.innerWidth, vh = window.innerHeight;
  const r = panel.getBoundingClientRect();
  const rows = [...panel.querySelectorAll("button, input, select, textarea")].map((el) => {
    const b = el.getBoundingClientRect();
    return {
      label: (el.getAttribute("aria-label") || el.textContent || "").trim().slice(0, 24),
      top: Math.round(b.top), bottom: Math.round(b.bottom),
      left: Math.round(b.left), right: Math.round(b.right),
    };
  });
  return {
    open: true, vw, vh,
    box: {
      left: Math.round(r.left), top: Math.round(r.top),
      right: Math.round(r.right), bottom: Math.round(r.bottom), width: Math.round(r.width),
    },
    rows,
    offscreen: rows.filter((b) => b.top < -1 || b.bottom > vh + 1 || b.left < -1 || b.right > vw + 1),
    docScrollWidth: document.documentElement.scrollWidth,
  };
})()`;

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
  /* A control's EFFECTIVE tap target, measured in the DOM rather than assumed.
   *
   * The app has two documented shapes for a control that must not be a 44px
   * box: .tap grows the box itself, and .tap-hit / .btn-icon* grow only the HIT
   * AREA, through a transparent ::after sized max(100%, 44px) centred on the
   * control (index.css). A rule about tap targets that reads only the painted
   * box reports the app's own fix as a defect — so this reads the hit-area
   * pseudo's used size too, and then PROBES the element with elementFromPoint
   * at the edge of that target: a hit area whose positioning was overridden
   * (the R195 bug: .tap-hit's position:relative lost to an absolute utility,
   * and the button left its overlay) is a box that exists in CSS and is not
   * where the thumb lands. Measured, never trusted. */
  const effectiveTarget = (el, box) => {
    let claimed = box.height;
    for (const pseudo of ["::after", "::before"]) {
      const cs = getComputedStyle(el, pseudo);
      if (!cs || cs.display === "none" || cs.content === "none") continue;
      const ph = parseFloat(cs.height);
      if (Number.isFinite(ph)) claimed = Math.max(claimed, ph);
    }
    /* What the control really ANSWERS for: how far the element (or a child of
     * it) is the topmost thing under a thumb aimed at its own centre column —
     * sampled point by point outwards from the centre, not at one edge. The
     * difference matters: a neighbour that the layout wraps over the edge of an
     * expanded target covers ONE point and the control still answers for the
     * ~40px around its centre (measured on the album page, where the rating
     * row sits 20px under the grade dot), while a hit area that is mostly
     * stolen measures small and is reported. An ANCESTOR is deliberately not
     * counted as answering: a tap that lands on the parent's background does
     * not reach the control, and counting it would hand every tiny control in a
     * padded row its parent's area. */
    const cx = box.left + box.width / 2;
    const cy = box.top + box.height / 2;
    const onScreen = box.top > -1 && box.bottom < window.innerHeight + 1;
    const answers = (dy) => {
      const t = document.elementFromPoint(cx, cy + dy);
      return !!t && (t === el || el.contains(t));
    };
    let span = 1;
    if (onScreen && box.height < 28) {
      let up = 0;
      while (up < 24 && answers(-(up + 1))) up++;
      let down = 0;
      while (down < 24 && answers(down + 1)) down++;
      span = up + down + 1;
    }
    /* Inside the viewport the measurement decides; off it there is nothing to
     * probe, so the CSS's own claim stands (a control the viewport cannot
     * reach is not a tap that missed). */
    return { claimed: Math.round(claimed), measured: Math.round(onScreen ? Math.max(box.height, span) : claimed) };
  };
  for (const b of document.querySelectorAll("button, [role=button], select")) {
    const r = b.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const target = effectiveTarget(b, r);
    if (target.measured >= 28) continue;
    tiny.push({
      /* What the control IS: its accessible name, else its tooltip, else its
       * own text — icon-only glyphs carry only a title attribute. */
      key: (b.getAttribute("aria-label") || b.getAttribute("title") || b.textContent || "").trim().replace(/\\s+/g, " ").slice(0, 40),
      cls: (b.className || "").toString().slice(0, 60),
      h: Math.round(r.height),
      target: target.measured,
      claimed: target.claimed,
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
    tiny: tiny.slice(0, 80),
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
  const entities = await entityRoutes();
  const sweep = [...ROUTES, ...entities.map((e) => e.route)];
  /* The phone tile size per hero route, so the desktop pass can assert the
   * cover did not SHRINK with the viewport (the shape under test is the fold;
   * the artist hero's 160px tile is its own design and not a defect). */
  const phoneCoverWidth = {};
  console.log(`[responsive] ${sweep.length} routes × ${VIEWPORTS.length} widths` +
    (entities.length ? ` (${entities.length} discovered from the library)` : ""));
  for (const vp of VIEWPORTS) {
    const page = await browser.newPage({ viewport: { width: vp.width, height: vp.height } });
    for (const route of sweep) {
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
          // Measured as a SET, not a count: the controls under the floor must be
          // exactly the ones SMALL_AT_PHONE documents. A new small control is
          // named and fails; so is a documented one that has grown past the
          // floor or gone (an allow-list that cannot rot).
          const allowed = smallAllowedFor(route);
          const matched = new Set();
          let unaccounted = 0;
          for (const t of m.tiny) {
            const hit = allowed.find((a) => t.key.startsWith(a.match) || t.cls.startsWith(a.match));
            if (hit) matched.add(hit.match);
            else {
              unaccounted++;
              check(`${label} — a control under the 28px floor that nothing accounts for`,
                false, `${t.key || t.cls} — box ${t.h}px, usable target ${t.target}px (its own CSS claims ${t.claimed}px)`);
            }
          }
          for (const a of allowed) {
            if (matched.has(a.match)) continue;
            check(`${label} — the documented small control "${a.match}" is gone (or no longer small)`,
              false, `it used to be ${a.h}px: ${a.why}`);
          }
          if (!unaccounted && matched.size === allowed.length) {
            check(`${label} — controls: ${m.tinyCount} under the floor, all documented`,
              true, allowed.map((a) => `${a.match} ${a.h}px`).join(", ") || "none");
          }
        }
      } catch (e) {
        check(`${vp.name} ${route} — loads`, false, String(e).slice(0, 140));
      }
    }

    // The hero fold (see FOLD_MEASURE), at the two widths that decide it: a
    // phone stacks the cover above the identity, a desktop puts them side by
    // side. Every hero route must answer both the same way — the album and
    // artist heroes are the idiom the playlist hero had to be brought onto.
    if (vp.width === 390 || vp.width === 1440) {
      for (const e of entities.filter((x) => x.hero)) {
        const label = `${vp.name} ${e.route} hero`;
        try {
          await page.goto(`${BASE}${e.route}`, { waitUntil: "domcontentloaded", timeout: 20000 });
          await sleep(900);
          const m = await page.evaluate(FOLD_MEASURE);
          check(`${label} — a hero with its two halves`, m.found, JSON.stringify(m));
          if (!m.found) continue;
          if (vp.width === 390) {
            phoneCoverWidth[e.route] = m.cover.width;
            check(`${label} — the cover stacks above the identity`,
              m.cover.bottom <= m.body.top + 2,
              `cover.bottom ${m.cover.bottom}, identity.top ${m.body.top}`);
            check(`${label} — the identity keeps the page's width, not what is left beside the cover`,
              m.body.width > m.vw * 0.7,
              `${m.body.width}px of ${m.vw}px`);
            check(`${label} — the phone cover is the small tile`,
              m.cover.width <= 176, `${m.cover.width}px wide`);
          } else {
            check(`${label} — the cover sits beside the identity`,
              m.cover.right <= m.body.left + 2,
              `cover.right ${m.cover.right}, identity.left ${m.body.left}`);
            check(`${label} — both halves start at the same top`,
              Math.abs(m.cover.top - m.body.top) <= 6,
              `cover.top ${m.cover.top}, identity.top ${m.body.top}`);
            /* No tile-SIZE assertion here on purpose: the artist hero keeps its
             * 160px tile at every width (the album and playlist heroes grow to
             * 224 from `sm` up), and the shape under test is the FOLD — the tile
             * being *smaller* than a phone tile is the regression worth naming,
             * and that is checked on the phone pass above. */
            const phone = phoneCoverWidth[e.route];
            check(`${label} — the desktop cover is not smaller than the phone tile`,
              phone === undefined || m.cover.width >= phone,
              `phone ${phone}px, desktop ${m.cover.width}px`);
          }
        } catch (e) {
          check(`${label} — loads`, false, String(e).slice(0, 140));
        }
      }
    }

    // Dialogs, in the same three shapes (see DIALOGS).
    for (const d of DIALOGS) {
      const label = `${vp.name} ${d.name}`;
      try {
        await page.goto(`${BASE}${d.route}`, { waitUntil: "domcontentloaded", timeout: 20000 });
        await sleep(900);
        await d.open(page);
        await sleep(350);
        // Settled first: the panel animates in (see settleBox). Skipped when no
        // dialog is on screen so a dialog that never opened reports as such
        // instead of waiting out an element that will not arrive.
        const dialog = page.locator('[role="dialog"][aria-modal="true"]').first();
        if (await dialog.count()) await dialog.evaluate(settleBox);
        const m = await page.evaluate(DIALOG_MEASURE);
        check(`${label} — opens from ${d.hint}`, m.open);
        if (!m.open) continue;
        check(`${label} — panel inside the window`,
          m.box.left >= -1 && m.box.right <= m.vw + 1 && m.box.top >= -1 && m.box.bottom <= m.vh + 1,
          JSON.stringify(m.box) + ` of ${m.vw}x${m.vh}`);
        check(`${label} — every control on screen`, m.offscreen.length === 0,
          JSON.stringify(m.offscreen));
        check(`${label} — no sideways scroll with the dialog open`,
          m.docScrollWidth <= m.vw + 2, `document is ${m.docScrollWidth}px wide`);
        if (vp.width < 640) {
          // The phone form: the sheet, i.e. the device's own width, resting on
          // the bottom edge.
          check(`${label} — sheet: full width, flush to the bottom`,
            m.box.width >= m.vw - 1 && Math.abs(m.box.bottom - m.vh) <= 1,
            JSON.stringify(m.box));
        } else {
          // The desktop form: the centred panel, untouched by the sheet recipe.
          const middle = m.box.top + (m.box.bottom - m.box.top) / 2;
          check(`${label} — panel: centred and narrower than the window`,
            m.box.width < m.vw && Math.abs(middle - m.vh / 2) <= 2,
            `centre ${Math.round(middle)} of ${m.vh}, width ${m.box.width} of ${m.vw}`);
        }
        await page.keyboard.press("Escape");
        await sleep(250);
        check(`${label} — Escape closes it`,
          !(await page.evaluate(DIALOG_MEASURE)).open);
      } catch (e) {
        check(`${label} — opens from ${d.hint}`, false, String(e).slice(0, 140));
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
