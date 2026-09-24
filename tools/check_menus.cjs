#!/usr/bin/env node
/* Sidebar/menu assertions against a running app — text evidence instead of
 * eyeballing screenshots. Run: node tools/check_menus.cjs [baseUrl]
 *
 * Four groups of checks, `--only=<group>` runs one of them:
 *   nav      — the sidebar/menu battery: the rail's entries, every route, the
 *              PHONE DRAWER (open, tap each entry, active state, Esc, focus)
 *   cover    — the album cover "…" menu's flyout geometry (needs an album with
 *              cover art; skips itself when there is none)
 *   sheet    — a dialog on a phone (bottom sheet: full width, bottom-anchored,
 *              safe-area aware, reachable header/footer) and its unchanged
 *              desktop form
 *   pagemenu — the page-level menus (the downloads/trash sort lists)
 *   flyout   — a flyout taller than the window (the Force menu) is clamped to
 *              it and scrolls, so its last row is reachable
 *   lyrics   — the lyrics pane's safe-area geometry between the top bar and
 *              the player bar */
let chromium;
try {
  // Plain require resolves from this file's folder up to the repo root's
  // node_modules; PLAYWRIGHT overrides it (e.g. a global install).
  ({ chromium } = require(process.env.PLAYWRIGHT || "playwright"));
} catch (e) {
  console.error("[check_menus] Playwright not found — install it with " +
    "`npm i -D playwright` (or set PLAYWRIGHT=/path/to/playwright).");
  process.exit(1);
}

const ARGS = process.argv.slice(2);
const BASE = ARGS.find((a) => !a.startsWith("--")) || process.env.BASE || "http://127.0.0.1:8000";
const ONLY = (ARGS.find((a) => a.startsWith("--only=")) || "").slice("--only=".length);
const want = (group) => !ONLY || ONLY === group;

/** The app's edge gutter (see web/src/components/Popover.tsx). */
const GUTTER = 8;

// Must stay in sync with NAV_GROUPS in web/src/App.tsx — every in-app route the
// sidebar lists, in order, and nothing else (the footer's outbound links and
// the credits providers are not menu entries).
const EXPECTED_NAV = [
  "Home", "Library", "Browse", "Genres", "Trash", "Playlists", "Favorites",
  "Downloads", "Discover", "Recommended", "Charts", "Watched artists", "Import",
  "Soulseek", "MusicBrainz", "Export", "Optimization", "Grading", "In progress",
  "Checks & scripts", "Dependencies", "Equalizer", "Settings", "Donations",
];

/* ---- the album cover "…" menu -------------------------------------------
 * The cover sits at the left edge of the content pane, so its panel has only
 * that edge to grow into. It used to be an absolutely positioned box inside
 * `main` — which is `overflow-auto`, so a panel reaching past the pane's left
 * edge was clipped — and painted under the sidebar's z-index. The panel is
 * portalled now, and when the window leaves it no room on the left the
 * primitive flips it to the trigger's LEFT edge, opening rightwards, clamped
 * to the viewport's gutter.
 *
 * Only geometry can see this: a clipped panel is still in the DOM, and
 * `elementFromPoint` is what tells a row's own left edge from the rail sitting
 * on top of it. */

/** Route of the first album in the library that has cover art. */
async function coverAlbumRoute() {
  const lib = await (await fetch(`${BASE}/api/library`)).json();
  for (const artist of lib.artists ?? []) {
    for (const album of artist.albums ?? []) {
      if (!album.cover_file) continue;
      const id = album.meta && album.meta.MUSICBRAINZ_ALBUMID;
      return id ? `/album/mb:${id}` : `/album/${encodeURIComponent(album.path)}`;
    }
  }
  return null;
}

/** The open panel, its trigger, the rail, and every row hit-tested at its own
 *  left edge, middle and right edge (what "not clipped" actually means). */
const menuGeometry = (page) => page.evaluate(() => {
  const rect = (el) => {
    const b = el.getBoundingClientRect();
    return { left: b.left, right: b.right, top: b.top, bottom: b.bottom, width: b.width };
  };
  const panel = document.querySelector('[role="menu"]');
  if (!panel) return null;
  const row = (el) => {
    const b = el.getBoundingClientRect();
    const y = Math.round(b.top + b.height / 2);
    const hits = (x) => {
      const hit = document.elementFromPoint(Math.round(x), y);
      return !!hit && (hit === el || el.contains(hit));
    };
    return {
      label: el.textContent.trim(),
      box: rect(el),
      leftHit: hits(b.left + 2), midHit: hits(b.left + b.width / 2), rightHit: hits(b.right - 2),
    };
  };
  const rowEls = [...panel.querySelectorAll('[role="menuitem"]')];
  const trigger = document.querySelector('button[title="Cover art actions"]');
  // A point where the panel and the rail overlap: whichever is painted last
  // answers the hit test, so the panel must be the one that does.
  const rail = document.querySelector("aside");
  const rb = rail && rail.getBoundingClientRect();
  const pb = panel.getBoundingClientRect();
  const overlap = rb && rb.left < pb.right && rb.right > pb.left
    ? { x: (Math.max(rb.left, pb.left) + Math.min(rb.right, pb.right)) / 2, y: pb.top + pb.height / 2 }
    : null;
  const overRail = overlap
    ? (() => { const hit = document.elementFromPoint(Math.round(overlap.x), Math.round(overlap.y));
        return !!hit && (hit === panel || panel.contains(hit)); })()
    : null;
  return {
    position: getComputedStyle(panel).position,
    panel: rect(panel),
    rail: rb ? rect(rail) : null,
    button: trigger ? rect(trigger) : null,
    rows: rowEls.map(row), overRail, vw: window.innerWidth,
  };
});

/** Open the cover's "…" menu on the album page and assert where the panel
 *  lands, at four window widths. */
async function coverMenuChecks(page, check) {
  const route = await coverAlbumRoute();
  if (!route) {
    console.log("  --   cover menu: skipped, no album in the library has cover art");
    return;
  }
  // The rail is what the pane is inset by; the expanded one is the state the
  // panel has to survive. Set on every load rather than in a one-shot
  // `evaluate`, which the app's own first-load reload tears down.
  await page.addInitScript(() => localStorage.setItem("mlo.sidebar.collapsed", "0"));

  const cases = [
    // `expect` is where the panel has to sit: `right` = right-aligned to the
    // trigger, today's alignment; `trigger-left` = the flip, opening
    // rightwards; `inside` = a flip with no room even rightwards, clamped.
    { label: "narrow 800x700, sidebar expanded", w: 800, h: 700, expect: "right", acts: true },
    { label: "narrow 700x700, rail hidden", w: 700, h: 700, expect: "trigger-left" },
    { label: "phone 360x700, panel wider than the space in front", w: 360, h: 700 },
    { label: "desktop 1440x900, sidebar expanded", w: 1440, h: 900, expect: "right" },
  ];
  for (const c of cases) {
    const tag = `cover menu (${c.label})`;
    await page.setViewportSize({ width: c.w, height: c.h });
    await page.goto(BASE + route, { waitUntil: "networkidle" });
    await page.waitForTimeout(900);
    await page.locator(".group\\/cover").first().hover().catch(() => {});
    await page.locator('button[title="Cover art actions"]').first().click();
    await page.waitForTimeout(300);
    const g = await menuGeometry(page);
    if (!g || !g.button) {
      check(`${tag}: panel opens from its trigger`, false, "no [role=menu] / cover-actions trigger in the DOM");
      continue;
    }
    const px = (v) => Math.round(v);
    check(`${tag}: panel is portalled out of the pane`, g.position === "fixed", g.position);
    check(`${tag}: panel inside the viewport (${GUTTER} px gutter)`,
      g.panel.left >= GUTTER - 0.5 && g.panel.right <= g.vw - GUTTER + 0.5,
      `panel ${px(g.panel.left)}..${px(g.panel.right)} in 0..${g.vw}`);
    const covered = g.rows.filter((r) => !r.leftHit || !r.midHit || !r.rightHit);
    check(`${tag}: all ${g.rows.length} rows hit-testable across their width`,
      g.rows.length > 0 && covered.length === 0,
      covered.map((r) => `${r.label} [${r.leftHit ? "" : "left "}${r.midHit ? "" : "mid "}${r.rightHit ? "" : "right"}]`).join(", "));
    if (c.expect === "right") {
      check(`${tag}: still right-aligned to the trigger`,
        Math.abs(g.panel.right - g.button.right) <= 1,
        `panel.right ${px(g.panel.right)} vs button.right ${px(g.button.right)}`);
    } else if (c.expect === "trigger-left") {
      check(`${tag}: flipped to the trigger's left edge, opening rightwards`,
        Math.abs(g.panel.left - g.button.left) <= 1,
        `panel.left ${px(g.panel.left)} vs button.left ${px(g.button.left)}`);
    }
    // A case with no `expect` is a panel wider than the space in front of the
    // trigger: there is no alignment to pin, and the gutter/hit-test checks
    // above are the whole point of that width — the panel is clamped inside
    // the viewport instead of hanging off its left edge.
    if (g.overRail !== null) {
      check(`${tag}: panel paints above the rail where they overlap`, g.overRail,
        `panel ${px(g.panel.left)} vs rail.right ${px(g.rail.right)}`);
    }
    // The panel is portalled now, i.e. the rows act from outside the trigger's
    // own subtree: opening a dialog from one is the cheapest proof of that.
    if (c.acts) {
      await page.getByRole("menuitem", { name: "Cover info" }).click();
      await page.waitForTimeout(400);
      const dialog = page.locator('[role="dialog"][aria-label="Cover info"]');
      check(`${tag}: a row opens its dialog`, await dialog.isVisible().catch(() => false));
      await page.keyboard.press("Escape");
      await page.waitForTimeout(300);
      check(`${tag}: Escape closes it again`,
        (await page.locator('[role="dialog"]').count()) === 0);
    }
  }
}

/* ---- the phone drawer ---------------------------------------------------
 * The rail is a desktop affordance: on a phone it is `hidden`, and the same
 * NAV_GROUPS render into a drawer behind the hamburger. That drawer IS the
 * navigation on the device the app is installed on, so it is exercised from
 * the device's own side: a touch context (coarse pointer, no hover), every
 * entry tapped, and every entry's state read back after the route changed.
 *
 * The failure classes pinned here, one assertion each:
 *   - the rail leaking through at phone width (two navs on one screen)
 *   - an entry that is not a touch target (a 20px row only a cursor can hit)
 *   - a drawer taller than the phone that clips instead of scrolling
 *   - an entry that navigates but leaves the drawer open over the new page
 *   - an entry that navigates but is not marked current on arrival
 *   - an active entry that is only distinguishable on hover
 *   - no keyboard way out (Esc) and focus dropped on <body> when it closes
 */
async function drawerChecks(browser, check, errs) {
  const ctx = await browser.newContext({
    viewport: { width: 390, height: 780 },
    hasTouch: true,
    isMobile: true,
  });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => errs.push("phone: " + e.message));
  const trigger = () => page.locator("header button[aria-expanded]").first();
  const open = async () => {
    await trigger().tap();
    await page.waitForTimeout(250);
  };
  try {
    await page.goto(BASE + "/", { waitUntil: "networkidle" });
    await page.waitForTimeout(1200);

    check("phone: the desktop rail is not rendered",
      !(await page.locator("aside").first().isVisible().catch(() => false)));
    check("phone: the hamburger announces its state",
      (await trigger().getAttribute("aria-expanded")) === "false",
      String(await trigger().getAttribute("aria-expanded")));
    await open();
    const drawer = page.locator('aside[role="dialog"]');
    check("phone: the hamburger opens the drawer", await drawer.isVisible().catch(() => false));

    const entries = await drawer.locator("a").evaluateAll((as) =>
      as
        .map((a) => ({ label: a.textContent.trim(), href: a.getAttribute("href") }))
        .filter((e) => e.href && !/^[a-z]+:/i.test(e.href))
    );
    console.log("drawer:", JSON.stringify(entries.map((e) => e.label)));
    check("phone: the drawer lists every NAV entry in order",
      JSON.stringify(entries.map((e) => e.label)) === JSON.stringify(EXPECTED_NAV),
      JSON.stringify(entries.map((e) => e.label)));

    // 24 entries do not fit a phone: the drawer has to scroll inside itself,
    // and its LAST entry has to come into view doing it.
    const scroll = await drawer.evaluate((el) => {
      const last = [...el.querySelectorAll("a")].pop();
      last.scrollIntoView({ block: "nearest" });
      const r = last.getBoundingClientRect();
      const dr = el.getBoundingClientRect();
      return {
        overflowY: getComputedStyle(el).overflowY,
        taller: el.scrollHeight > el.clientHeight + 1,
        lastInView: r.top >= dr.top - 1 && r.bottom <= dr.bottom + 1,
      };
    });
    check("phone: the drawer scrolls its own overflow",
      !scroll.taller || scroll.overflowY === "auto" || scroll.overflowY === "scroll",
      JSON.stringify(scroll));
    check("phone: the last entry scrolls into the drawer", scroll.lastInView, JSON.stringify(scroll));

    const vp = page.viewportSize();
    const isOpen = async () => (await page.locator('aside[role="dialog"]').count()) > 0;
    for (const e of entries) {
      // Each press closes the drawer, so every entry starts from the same
      // state: closed, then opened with the hamburger. (While it is open the
      // hamburger is under the drawer's own backdrop — which is a close.)
      if (!(await isOpen())) await open();
      const link = () => page.locator(`aside[role="dialog"] a[href="${e.href}"]`).first();
      await link().scrollIntoViewIfNeeded();
      const box = await link().boundingBox();
      check(`phone drawer: "${e.label}" is a touch target in view`,
        !!box && box.height >= 44 && box.y >= -1 && box.y + box.height <= vp.height + 1,
        JSON.stringify(box));
      await link().tap();
      await page.waitForTimeout(300);
      // The SECTION, not the exact string: `nav.favorites` points at
      // `/favorites`, which the router redirects to its default tab
      // (`/favorites/tracks`) — the entry leads where it says, and the
      // active-state check below is what proves the link owns the section.
      const landed = new URL(page.url()).pathname;
      check(`phone drawer: "${e.label}" → ${e.href} navigates`,
        landed === e.href || landed.startsWith(e.href + "/"), page.url());
      check(`phone drawer: "${e.label}" closes the drawer`, !(await isOpen()));
      // Reopen to read the entry's own state on the page it just opened.
      await open();
      const reopened = () => page.locator(`aside[role="dialog"] a[href="${e.href}"]`).first();
      check(`phone drawer: "${e.label}" is marked current after the press`,
        (await reopened().getAttribute("aria-current")) === "page",
        String(await reopened().getAttribute("aria-current")));
      const ink = await reopened().evaluate((a) => getComputedStyle(a).backgroundColor);
      const other = await page.locator('aside[role="dialog"] a:not([aria-current])').first()
        .evaluate((a) => getComputedStyle(a).backgroundColor);
      check(`phone drawer: "${e.label}" reads as active without hover`, ink !== other, `${ink} vs ${other}`);
      await page.keyboard.press("Escape");
      await page.waitForTimeout(200);
      check(`phone drawer: Esc closes it after "${e.label}"`,
        (await page.locator('aside[role="dialog"]').count()) === 0);
    }

    // Focus goes back to the hamburger: the drawer is the only nav on a phone,
    // so dropping focus on <body> strands a keyboard/AT user at the top of the
    // page with no way forward.
    await open();
    await page.keyboard.press("Escape");
    await page.waitForTimeout(250);
    check("phone drawer: Esc returns focus to the hamburger",
      await page.evaluate(() => document.activeElement?.getAttribute("aria-expanded") === "false"));
  } finally {
    await ctx.close();
  }
}

/* ---- a dialog on a phone ------------------------------------------------
 * `Modal` keeps the desktop form (a centred panel) and gains a bottom-sheet
 * form below `sm`. What can go wrong there is geometry, so the sheet is
 * measured: full width, flush to the bottom, rounded only at the top, its
 * header/footer rows still hit-testable, the home-indicator inset inside the
 * panel, and the keyboard lift wired to the variables the component writes.
 * The subject is Browse's "Save as smart playlist" dialog — a real sheet with
 * a field and a two-button footer, reachable without any library data.
 * The desktop half opens the SAME dialog at 1440x900 and asserts it is still
 * the centred panel it was, which is the no-regression half of the change. */
const panelState = (el) => {
  const r = el.getBoundingClientRect();
  const cs = getComputedStyle(el);
  const overlay = getComputedStyle(el.parentElement);
  const body = el.querySelector("[data-modal-body]");
  const br = body?.getBoundingClientRect();
  return {
    box: { top: r.top, bottom: r.bottom, left: r.left, right: r.right, width: r.width, height: r.height },
    radius: { top: cs.borderTopLeftRadius, bottom: cs.borderBottomLeftRadius },
    padBottom: cs.paddingBottom,
    lift: overlay.getPropertyValue("--mlo-vv-lift").trim(),
    body: body
      ? { overflowY: getComputedStyle(body).overflowY, top: br.top, bottom: br.bottom }
      : null,
    rows: [...el.querySelectorAll("button")].map((b) => {
      const q = b.getBoundingClientRect();
      const hit = document.elementFromPoint(Math.round(q.left + q.width / 2), Math.round(q.top + q.height / 2));
      return {
        label: (b.getAttribute("aria-label") || b.textContent || "").trim(),
        top: q.top, bottom: q.bottom,
        hit: !!hit && (hit === b || b.contains(hit)),
      };
    }),
    vw: window.innerWidth,
    vh: window.innerHeight,
  };
};

async function sheetChecks(browser, page, check, errs) {
  const SAVE = '.btn:has-text("Save as smart playlist")';
  const near = (a, b, tol = 1) => Math.abs(a - b) <= tol;
  const ctx = await browser.newContext({
    viewport: { width: 390, height: 780 },
    hasTouch: true,
    isMobile: true,
  });
  const phone = await ctx.newPage();
  phone.on("pageerror", (e) => errs.push("phone: " + e.message));
  try {
    await phone.goto(BASE + "/browse", { waitUntil: "networkidle" });
    await phone.waitForTimeout(1200);
    await phone.locator(SAVE).first().tap();
    await phone.waitForTimeout(350);
    const panel = phone.locator('[role="dialog"][aria-modal="true"]').first();
    check("phone sheet: the dialog opens from its trigger", await panel.isVisible().catch(() => false));
    // Name the playlist first: an empty field leaves the confirm button
    // DISABLED, and a disabled button reports `pointer-events: none` — which
    // is not what "unreachable behind another layer" looks like. The hit test
    // below asks about a control the user can actually press.
    await panel.locator("input").first().fill("Menus check");
    await phone.waitForTimeout(200);
    let g = await panel.evaluate(panelState);

    check("phone sheet: full device width, flush left", near(g.box.width, g.vw) && g.box.left <= 1,
      `panel ${Math.round(g.box.left)}..${Math.round(g.box.right)} of ${g.vw}`);
    check("phone sheet: anchored to the bottom edge", near(g.box.bottom, g.vh),
      `panel.bottom ${Math.round(g.box.bottom)} of ${g.vh}`);
    check("phone sheet: rounded at the top only",
      g.radius.top === "12px" && g.radius.bottom === "0px", JSON.stringify(g.radius));
    check("phone sheet: the keyboard hook is wired", g.lift === "0px", JSON.stringify(g.lift));
    check("phone sheet: the body is the scrolling area", g.body?.overflowY === "auto");
    const offscreen = g.rows.filter((r) => r.top < -1 || r.bottom > g.vh + 1);
    check("phone sheet: every control is inside the sheet's rows", offscreen.length === 0,
      JSON.stringify(offscreen));
    const dead = g.rows.filter((r) => !r.hit);
    check("phone sheet: close and confirm are hit-testable", g.rows.length >= 3 && dead.length === 0,
      JSON.stringify(dead));

    // The safe-area half: the sheet's own padding has to carry the home
    // indicator, or the confirm row sits under it. The inset is injected with
    // the app's own test hook (`--mlo-inset-*`, index.css) because env() is 0
    // on every machine without a notch.
    await phone.evaluate(() => {
      document.documentElement.style.setProperty("--mlo-inset-bottom", "34px");
      document.documentElement.style.setProperty("--mlo-inset-top", "47px");
    });
    await phone.waitForTimeout(120);
    g = await panel.evaluate(panelState);
    check("phone sheet: the home indicator is the sheet's own bottom padding",
      near(parseFloat(g.padBottom), 34), g.padBottom);
    const under = g.rows.filter((r) => r.bottom > g.vh - 34 + 1);
    check("phone sheet: no control sits under the home indicator", under.length === 0,
      JSON.stringify(under));

    // The keyboard half: the component's visualViewport measurement writes the
    // lift and the visible height onto the overlay, and the recipe is what
    // turns them into geometry. Injecting them is the only way to test a
    // keyboard without one.
    await phone.evaluate(() => {
      const overlay = document.querySelector('[role="dialog"][aria-modal="true"]').parentElement;
      overlay.style.setProperty("--mlo-vv-lift", "320px");
      overlay.style.setProperty("--mlo-vv-h", "420px");
    });
    await phone.waitForTimeout(120);
    g = await panel.evaluate(panelState);
    check("phone sheet: the keyboard lifts the sheet out of its way",
      near(g.box.bottom, g.vh - 320), `panel.bottom ${Math.round(g.box.bottom)} of ${g.vh - 320}`);
    check("phone sheet: and caps it to the visible strip", g.box.height <= 420 - 8 + 1,
      String(Math.round(g.box.height)));

    await phone.keyboard.press("Escape");
    await phone.waitForTimeout(300);
    check("phone sheet: Escape closes it", (await phone.locator('[role="dialog"]').count()) === 0);
    check("phone sheet: focus returns to the trigger",
      await phone.evaluate(() => (document.activeElement?.textContent || "").includes("Save as smart playlist")));
  } finally {
    await ctx.close();
  }

  // The same dialog on a desktop window: the centred panel, untouched.
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(BASE + "/browse", { waitUntil: "networkidle" });
  await page.waitForTimeout(1000);
  await page.locator(SAVE).first().click();
  await page.waitForTimeout(300);
  const desk = await page.locator('[role="dialog"][aria-modal="true"]').first().evaluate(panelState);
  const middle = desk.box.top + desk.box.height / 2;
  check("desktop dialog: the panel is still centred",
    near(middle, desk.vh / 2, 2), `centre ${Math.round(middle)} of ${desk.vh}`);
  check("desktop dialog: still the caller's max-width, not the window",
    desk.box.width <= 449 && desk.box.width > 400, String(Math.round(desk.box.width)));
  check("desktop dialog: still rounded on all four corners",
    desk.radius.top === "12px" && desk.radius.bottom === "12px", JSON.stringify(desk.radius));
  check("desktop dialog: not flush with the bottom edge", desk.box.bottom < desk.vh - 40,
    `panel.bottom ${Math.round(desk.box.bottom)} of ${desk.vh}`);
  await page.keyboard.press("Escape");
  await page.waitForTimeout(250);
  check("desktop dialog: Escape closes it", (await page.locator('[role="dialog"]').count()) === 0);
}

/* ---- the page-level menus ----------------------------------------------
 * The downloads and trash sort lists were hand-rolled: their own
 * full-viewport catcher, no Escape, no `role="menu"` — the one menu idiom in
 * the app but two implementations of it. They are Popovers now, and this pins
 * the behaviour a user notices: opens from its trigger, Escape closes it,
 * a press outside closes it, and picking a row sorts.
 */
async function pageMenuChecks(page, check) {
  const cases = [
    { route: "/downloads", title: "Sort the downloads", row: "Artist" },
    { route: "/trash", title: "Sort the trash", row: "Name" },
  ];
  for (const c of cases) {
    const tag = `${c.route} sort menu`;
    await page.goto(BASE + c.route, { waitUntil: "networkidle" });
    await page.waitForTimeout(900);
    const trigger = page.locator(`button[title="${c.title}"]`);
    check(`${tag}: starts closed`, (await page.locator('[role="menu"]').count()) === 0);
    check(`${tag}: the trigger advertises its state`,
      (await trigger.getAttribute("aria-expanded")) === "false" &&
        (await trigger.getAttribute("aria-haspopup")) === "menu");
    await trigger.click();
    await page.waitForTimeout(250);
    const panel = page.locator('[role="menu"]');
    check(`${tag}: opens a role=menu panel`, await panel.isVisible().catch(() => false));
    check(`${tag}: every row is a menu item`,
      (await panel.locator('[role="menuitem"]').count()) >= 4,
      String(await panel.locator('[role="menuitem"]').count()));
    check(`${tag}: the trigger now reads as open`,
      (await trigger.getAttribute("aria-expanded")) === "true");
    await page.keyboard.press("Escape");
    await page.waitForTimeout(250);
    check(`${tag}: Escape closes it`, (await page.locator('[role="menu"]').count()) === 0);
    await trigger.click();
    await page.waitForTimeout(200);
    await page.mouse.click(700, 700);
    await page.waitForTimeout(250);
    check(`${tag}: a press outside closes it`, (await page.locator('[role="menu"]').count()) === 0);
    await trigger.click();
    await page.waitForTimeout(200);
    await page.getByRole("menuitem", { name: new RegExp(`^${c.row}`) }).click();
    await page.waitForTimeout(250);
    check(`${tag}: picking a row sorts and closes it`,
      ((await trigger.innerText()) || "").includes(c.row) && (await page.locator('[role="menu"]').count()) === 0,
      (await trigger.innerText()) || "");
  }

  // A phone is where a dropdown falls off the screen: the non-fixed panel is
  // anchored inside the toolbar, so it must still fit the 390px viewport.
  await page.setViewportSize({ width: 390, height: 780 });
  await page.goto(BASE + "/downloads", { waitUntil: "networkidle" });
  await page.waitForTimeout(900);
  await page.locator('button[title="Sort the downloads"]').click();
  await page.waitForTimeout(250);
  const fit = await page.locator('[role="menu"]').evaluate((el) => {
    const r = el.getBoundingClientRect();
    return { left: r.left, right: r.right, vw: window.innerWidth };
  });
  check("phone: the sort menu stays inside the viewport",
    fit.left >= -1 && fit.right <= fit.vw + 1, JSON.stringify(fit));
  await page.keyboard.press("Escape");
  await page.waitForTimeout(200);
  await page.setViewportSize({ width: 1440, height: 900 });
}

/* ---- a flyout taller than the window ------------------------------------
 * Every flyout is clamped to the viewport and scrolls inside itself
 * (`.popover-panel`, index.css). The subject is the Force menu on /optimize —
 * 14 force flags, then its All/None row: the menu that ran past the bottom of
 * the window with its lower flags unreachable and nothing to scroll (#53).
 * Two of the four windows are short enough to GUARANTEE the clamp (a menu that
 * fits proves nothing about one that does not), and the same invariants are
 * asserted at the standard 390x780 / 1440x900 so a fitting menu is covered
 * too. The home-indicator case rides the app's own `--mlo-inset-*` test hook:
 * the bound is written as a `var()` calc precisely so it resolves live. */
const FORCE_MENU = 'button[title="Choose which scripts are forced"]';

const flyoutState = (el) => {
  const r = el.getBoundingClientRect();
  const cs = getComputedStyle(el);
  return {
    box: {
      top: Math.round(r.top), bottom: Math.round(r.bottom),
      left: Math.round(r.left), right: Math.round(r.right), height: Math.round(r.height),
    },
    overflowY: cs.overflowY,
    overscroll: cs.overscrollBehaviorY,
    scrollTop: Math.round(el.scrollTop),
    scrollHeight: el.scrollHeight,
    clientHeight: el.clientHeight,
    vw: window.innerWidth,
    vh: window.innerHeight,
  };
};

/** A row inside the panel, its place in the panel's own box, and whether a
 *  press on it would land on it (i.e. it is not behind the panel's edge or
 *  another layer). */
const flyoutRow = (el, text) => {
  const row = [...el.querySelectorAll("label, button")].find((n) => (n.textContent || "").includes(text));
  if (!row) return null;
  const b = row.getBoundingClientRect();
  const pr = el.getBoundingClientRect();
  const x = Math.round(b.left + Math.min(10, b.width / 2));
  const y = Math.round(b.top + b.height / 2);
  const hit = document.elementFromPoint(x, y);
  return {
    top: Math.round(b.top), bottom: Math.round(b.bottom),
    inside: b.top >= pr.top - 1 && b.bottom <= pr.bottom + 1,
    hit: !!hit && (hit === row || row.contains(hit)),
  };
};

async function flyoutChecks(page, check) {
  const cases = [
    { label: "phone 390x780", w: 390, h: 780 },
    { label: "desktop 1440x900", w: 1440, h: 900 },
    { label: "short window 390x520", w: 390, h: 520 },
    { label: "short window 1440x520", w: 1440, h: 520 },
  ];
  for (const c of cases) {
    await page.setViewportSize({ width: c.w, height: c.h });
    await page.goto(BASE + "/optimize", { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(900);
    const trigger = page.locator(FORCE_MENU);
    check(`${c.label}: the Force trigger advertises its menu`,
      (await trigger.getAttribute("aria-haspopup")) === "menu" &&
        (await trigger.getAttribute("aria-expanded")) === "false");
    await trigger.click();
    await page.waitForTimeout(300);
    const panel = page.locator('[role="menu"]').first();
    check(`${c.label}: the Force menu opens`, await panel.isVisible().catch(() => false));
    const g = await panel.evaluate(flyoutState);
    check(`${c.label}: the flyout never runs past the bottom edge`,
      g.box.bottom <= g.vh - 8 + 1, `bottom ${g.box.bottom} of ${g.vh}`);
    check(`${c.label}: the flyout stays inside the window horizontally`,
      g.box.left >= 7 && g.box.right <= g.vw - 7, JSON.stringify(g.box));
    check(`${c.label}: the flyout scrolls inside itself`,
      g.overflowY === "auto" && g.overscroll === "contain", `${g.overflowY}/${g.overscroll}`);
    check(`${c.label}: the flyout is never taller than the window`,
      g.box.height <= g.vh - 8 + 1, `${g.box.height} of ${g.vh}`);

    const clamped = g.scrollHeight > g.clientHeight + 1;
    if (!clamped) {
      // A flyout that fits is content-sized: a max-height that stretched every
      // menu to the window would be a jump, not a fix. `scrollHeight` is the
      // padding box, so the panel's own 1px top and bottom borders (2px) are
      // the difference from the border box, not a stretch.
      check(`${c.label}: a fitting flyout is content-sized, not stretched`,
        Math.abs(g.box.height - (g.scrollHeight + 2)) <= 1, `${g.box.height} vs ${g.scrollHeight} + 2`);
    } else {
      // Scroll it to the end: the last force flag and the All/None row have to
      // come into view, and a press on them has to reach them.
      await panel.evaluate((el) => {
        el.scrollTop = el.scrollHeight;
      });
      await page.waitForTimeout(150);
      const after = await panel.evaluate(flyoutState);
      check(`${c.label}: the list scrolled, the window did not`,
        after.scrollTop > 0 && after.box.bottom === g.box.bottom,
        `scrollTop ${after.scrollTop}, bottom ${after.box.bottom} vs ${g.box.bottom}`);
      const last = await panel.evaluate(flyoutRow, "20 · Layout fix");
      check(`${c.label}: the last force flag is reachable`,
        !!last && last.inside && last.hit, JSON.stringify(last));
      const none = await panel.evaluate(flyoutRow, "None");
      check(`${c.label}: the All/None row is reachable`,
        !!none && none.inside && none.hit, JSON.stringify(none));
      const mid = await panel.evaluate(flyoutRow, "1 · Lyrics re-format");
      check(`${c.label}: and the first flag scrolled out of the way`,
        !!mid && !mid.inside, JSON.stringify(mid));
    }

    // The home indicator is inside the bound: a menu that stopped 8px above
    // the bottom edge would stop UNDER it.
    await page.evaluate(() => document.documentElement.style.setProperty("--mlo-inset-bottom", "34px"));
    await page.waitForTimeout(150);
    const inset = await panel.evaluate(flyoutState);
    check(`${c.label}: the flyout clears the home indicator`,
      inset.box.bottom <= inset.vh - 34 + 1, `bottom ${inset.box.bottom} of ${inset.vh}`);
    await page.evaluate(() => document.documentElement.style.removeProperty("--mlo-inset-bottom"));

    await page.keyboard.press("Escape");
    await page.waitForTimeout(250);
    check(`${c.label}: Escape closes the flyout`, (await page.locator('[role="menu"]').count()) === 0);
  }
  await page.setViewportSize({ width: 1440, height: 900 });
}

/* ---- the lyrics pane's own chrome ---------------------------------------
 * The pane is `fixed`, so it inherits nothing from `.safe-shell` and has to
 * repeat the insets its two anchors carry: the top bar (3rem, itself pushed
 * down by the notch) and the player bar (5.75rem, already lifted by the home
 * indicator). Measured from the viewport edge without them, the pane covered
 * the player bar's top row and hung under the home indicator.
 * The pane opens with nothing playing (it shows a hint), so no track is
 * needed, and the inset is injected with the app's `--mlo-inset-*` hook. */
const lyricsGeometry = () => {
  const pane = document.querySelector("aside.safe-lyrics");
  if (!pane) return null;
  const r = pane.getBoundingClientRect();
  return {
    top: Math.round(r.top), bottom: Math.round(r.bottom),
    vh: window.innerHeight, vw: window.innerWidth,
    close: !!pane.querySelector('[title="Close lyrics"]'),
  };
};

async function lyricsChecks(page, check) {
  await page.setViewportSize({ width: 1024, height: 700 });
  await page.goto(BASE + "/", { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(1200);
  await page.evaluate(() => {
    document.documentElement.style.setProperty("--mlo-inset-top", "47px");
    document.documentElement.style.setProperty("--mlo-inset-bottom", "34px");
  });
  const trigger = page.locator('[aria-label="Lyrics"]').first();
  check("lyrics: the player bar carries its trigger", await trigger.isVisible().catch(() => false));
  await trigger.click();
  await page.waitForTimeout(400);
  const g = await page.evaluate(lyricsGeometry);
  check("lyrics: the pane opens", !!g);
  if (g) {
    // Under the top bar, above the player bar, both lifted by the notch and
    // the home indicator: 3rem + 47 and 5.75rem + 34.
    check("lyrics: clears the notch under the top bar", Math.abs(g.top - 95) <= 1, String(g.top));
    check("lyrics: clears the home indicator above the player bar",
      Math.abs(g.bottom - (700 - 126)) <= 1, String(g.bottom));
    check("lyrics: the close control is on the pane", g.close);
  }
  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);
  check("lyrics: Escape closes the pane", (await page.locator("aside.safe-lyrics").count()) === 0);
  await page.evaluate(() => {
    document.documentElement.style.removeProperty("--mlo-inset-top");
    document.documentElement.style.removeProperty("--mlo-inset-bottom");
  });
  await page.setViewportSize({ width: 1440, height: 900 });
}

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROME, headless: true });
  const page = await browser.newContext({ viewport: { width: 1440, height: 900 } }).then((c) => c.newPage());
  const errs = [];
  page.on("pageerror", (e) => errs.push(e.message));
  let fail = 0;
  const check = (label, ok, detail = "") => {
    console.log(`  ${ok ? "ok  " : "FAIL"} ${label}${ok ? "" : "  " + detail}`);
    if (!ok) fail++;
  };
  const finish = async () => {
    check("no uncaught page errors", errs.length === 0, errs.join(" | "));
    await browser.close();
    console.log(`\n${fail ? "FAIL" : "PASS"} — ${fail} problem(s)`);
    process.exit(fail ? 1 : 0);
  };

  if (want("cover")) await coverMenuChecks(page, check);
  if (want("flyout")) await flyoutChecks(page, check);
  if (want("lyrics")) await lyricsChecks(page, check);
  if (want("sheet")) await sheetChecks(browser, page, check, errs);
  if (want("pagemenu")) await pageMenuChecks(page, check);
  if (!want("nav")) return finish();

  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.waitForTimeout(1500);

  // Only the app's OWN routes are the menu: the sidebar's footer carries
  // outbound links (the repo, the credits' providers) and they are not NAV
  // entries — gathering every `aside a` made this check fail on the credits
  // list rather than on a missing menu item.
  const navLinks = await page.locator("aside a").evaluateAll((as) =>
    as.filter((a) => (a.getAttribute("href") || "").startsWith("/"))
      .map((a) => a.textContent.trim())
      .filter(Boolean)
  );
  console.log("sidebar:", JSON.stringify(navLinks));
  check("sidebar lists every NAV entry in order", JSON.stringify(navLinks) === JSON.stringify(EXPECTED_NAV),
    `got ${JSON.stringify(navLinks)}`);

  for (const path of ["/library", "/mb/search?q=test"]) {
    const res = await page.goto(BASE + path, { waitUntil: "networkidle" });
    await page.waitForTimeout(800);
    check(`${path} answers 200`, res && res.status() === 200, String(res && res.status()));
    const h1 = (await page.locator("h1").first().innerText().catch(() => "")).trim();
    check(`${path} renders a heading`, h1.length > 0, JSON.stringify(h1));
  }

  // Every sidebar entry must actually lead somewhere: click it and confirm the
  // route answers and renders — the menu/routes can never drift apart silently.
  //
  // Only IN-APP hrefs are routes. The sidebar's footer carries outbound links
  // (the repo, the credits) and `BASE + "https://…"` is not a URL: the walk
  // used to try exactly that and died on `http://127.0.0.1:8000https://…`,
  // which is how this check reported a failure that was neither the menu's nor
  // the server's.
  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.waitForTimeout(800);
  const hrefs = await page.locator("aside a").evaluateAll((as) =>
    as.map((a) => ({ label: a.textContent.trim(), href: a.getAttribute("href") }))
  );
  for (const { label, href } of hrefs) {
    if (!href || /^[a-z]+:/i.test(href)) continue; // outbound / mailto / tel
    // PRESS it, rather than `goto` the same URL: the menu's job is the click,
    // and a route that answers to a full page load can still be unreachable in
    // the router (or land somewhere else entirely, as `/favorites` does — it
    // redirects to its default tab, so the section, not the exact string, is
    // what this asserts).
    //
    // `domcontentloaded` on the presses below is not `networkidle`: several
    // pages poll (the progress socket, Discover's shelves), so "no network in
    // flight for 500ms" never arrives and the walk died on /discover — an
    // unreadable failure that was neither the menu's nor the server's. The
    // heading is what the walk is really asserting, so it waits for that.
    await page.locator(`aside a[href="${href}"]`).first().click();
    const h1 = await page.locator("h1").first().innerText({ timeout: 10000 }).catch(() => "");
    const landed = new URL(page.url()).pathname;
    check(`nav "${label}" → ${href} navigates and renders`,
      (landed === href || landed.startsWith(href + "/")) && h1.trim().length > 0,
      `${page.url()} ${JSON.stringify(h1)}`);
    // The active entry has to be readable as ACTIVE, not merely present: the
    // same assertion the phone drawer's walk makes (a solid accent block, whose
    // background differs from every inactive entry's).
    const active = page.locator(`aside a[href="${href}"][aria-current="page"]`);
    const ink = await active.evaluate((a) => getComputedStyle(a).backgroundColor).catch(() => "");
    const other = await page.locator("aside a:not([aria-current])").first()
      .evaluate((a) => getComputedStyle(a).backgroundColor).catch(() => "");
    check(`nav "${label}" reads as current after the press`,
      (await active.count()) === 1 && !!ink && ink !== other, `${await active.count()} ${ink} vs ${other}`);
  }

  // The rail above is the desktop nav. The phone drawer is the same menu on
  // the device the app is installed on, and gets its own walk.
  await drawerChecks(browser, check, errs);

  await finish();
})();
