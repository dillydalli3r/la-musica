#!/usr/bin/env node
/* Sidebar/menu assertions against a running app — text evidence instead of
 * eyeballing screenshots. Run: node tools/check_menus.cjs [baseUrl]
 *
 * The groups of checks, `--only=<group>` runs one of them:
 *   nav      — the sidebar/menu battery: the rail's entries, every route, the
 *              PHONE DRAWER (open, tap each entry, active state, Esc, focus)
 *   cover    — the album cover "…" menu's flyout geometry (needs an album with
 *              cover art; skips itself when there is none)
 *   pagemenu — the page-level menus (the trash sort list)
 *   flyout   — a flyout taller than the window (the Force menu) is clamped to
 *              it and scrolls, so its last row is reachable
 *   trackmenu — the track-only entries of the details ("…") menu: "Open track
 *              page" lands on that track's own page, and a menu that is NOT on
 *              one track does not offer it
 *   credits  — the Credits popout's identity header: its heading is the
 *              release's (or recording's) NAME and the facts the files state
 *              ride BELOW it, never a raw path in the title slot */
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
  "Library", "Browse", "Genres", "Trash", "Import",
  "MusicBrainz", "Export", "Optimization", "Grading", "In progress",
  "Checks & scripts", "Dependencies", "Equalizer", "Settings",
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

    // A long nav does not fit a phone: the drawer has to scroll inside itself,
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
      // The entry's own href is the route it lands on; the active-state
      // check below is what proves the link owns the page it opened.
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
      // Same settled read as the rail walk: the active block is the ACCENT
      // paint, not merely "painted differently from its transparent siblings".
      const ink = await reopened().evaluate(settledInk);
      check(`phone drawer: "${e.label}" reads as active without hover`,
        ink.bg === accentRgb(ink.accent), `${JSON.stringify(ink)} vs accent ${accentRgb(ink.accent)}`);
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

/* ---- the page-level menus ----------------------------------------------
 * The trash sort list was hand-rolled: its own full-viewport catcher, no
 * Escape, no `role="menu"` — the one menu idiom in the app but two
 * implementations of it. It is a Popover now, and this pins the behaviour a
 * user notices: opens from its trigger, Escape closes it, a press outside
 * closes it, and picking a row sorts.
 */
async function pageMenuChecks(page, check) {
  const cases = [
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
    await panel.evaluate(settledRect);
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
  await page.goto(BASE + "/trash", { waitUntil: "networkidle" });
  await page.waitForTimeout(900);
  await page.locator('button[title="Sort the trash"]').click();
  await page.waitForTimeout(250);
  await page.locator('[role="menu"]').evaluate(settledRect);
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
    await panel.evaluate(settledRect);
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

/* ---- reading a nav entry's ACTIVE paint ---------------------------------
 * `bg-accent` does not land in one frame: the active class swap goes through
 * the `.nav-link` `background-color` transition (index.css), so a single
 * `getComputedStyle` read can catch the entry on its way — and a starved
 * renderer can serve that pre-swap frame to two consecutive runs. That is the
 * whole of a flake this check used to report as
 *   `nav "Grading" reads as current after the press 1 rgba(0,0,0,0) vs …`
 * while the DOM was already correct (aria-current="page", the accent class
 * applied, the live value "rgb(255, 255, 255)").
 *
 * So settle first: sample until two consecutive reads agree, the pointer is
 * not hovering the entry (a hover paint is not its resting state), spaced by a
 * TIMER rather than by rAF — a stalled rAF is exactly the starvation this
 * defends against — and give up after 2s so a genuinely wrong value fails the
 * assertion instead of hanging the run.
 *
 * The settled value is then compared with the entry's OWN `--accent` triplet,
 * not with a sibling's transparent background: "different from the others" is
 * satisfied by any paint at all, including a half-applied one. */
const settledInk = async (el) => {
  const read = () => {
    const cs = getComputedStyle(el);
    return {
      bg: cs.backgroundColor,
      accent: cs.getPropertyValue("--accent").trim(),
      hover: el.matches(":hover"),
    };
  };
  const deadline = performance.now() + 2000;
  let last = null;
  let stable = 0;
  let s = read();
  while (performance.now() < deadline) {
    await new Promise((r) => setTimeout(r, 50));
    s = read();
    if (s.hover) {
      last = null;
      stable = 0;
      continue;
    }
    if (s.bg === last) {
      if (++stable >= 2) break;
    } else {
      stable = 0;
      last = s.bg;
    }
  }
  return s;
};

/** `--accent` is an RGB triplet ("255 255 255", commas tolerated); the paint it
 *  produces is `rgb(255, 255, 255)`. */
const accentRgb = (triplet) => {
  const n = String(triplet || "").split(/[\s,]+/).map(Number).filter((v) => !Number.isNaN(v));
  return n.length >= 3 ? `rgb(${n[0]}, ${n[1]}, ${n[2]})` : "";
};

/** The same "wait for it to stop moving" rule for a BOX, used before any
 *  geometry is measured on a panel that animates in: `.anim-pop` scales and
 *  translates the dialog (6px of `pop-in` is exactly the tolerance the
 *  sheet/centred assertions work in) and `sheet-up` slides it. A starved
 *  renderer can still be serving a mid-animation frame at the first
 *  measurement — the value moves, the layout is right. */
const settledRect = async (el) => {
  const read = () => {
    const r = el.getBoundingClientRect();
    return { box: [r.left, r.top, r.width, r.height].map((n) => Math.round(n * 10) / 10).join(","), vh: window.innerHeight };
  };
  const deadline = performance.now() + 2000;
  let last = null;
  let stable = 0;
  let s = read();
  while (performance.now() < deadline) {
    await new Promise((r) => setTimeout(r, 50));
    s = read();
    if (s.box === last) {
      if (++stable >= 2) break;
    } else {
      stable = 0;
      last = s.box;
    }
  }
  return s;
};

/* ---- the details ("…") menu's TRACK entries ----------------------------
 * An owner-reported move, measured on the real album page: "Open track page" —
 * the "…" a listed track wears opens that track's own page. The row's title
 * link is the other way in (a plain click PLAYS the track — lib/refs'
 * entityLinkClick), so the menu is the deliberate one.
 *
 * The negative is here too: the album header's own tag menu is on the whole
 * release, so the entry may not appear there — a menu on several paths must
 * not pick one track out of them. */

/** `/album/<path>` — the page for an album the library payload gave us. */
function albumRoute(al) {
  const id = al.meta && al.meta.MUSICBRAINZ_ALBUMID;
  return id ? `/album/mb:${id}` : `/album/${encodeURIComponent(al.path)}`;
}

async function trackMenuChecks(page, check) {
  await page.setViewportSize({ width: 1440, height: 900 });
  const lib = await (await fetch(`${BASE}/api/library`)).json();
  const album = (lib.artists ?? [])
    .flatMap((a) => a.albums ?? [])
    .find((al) => (al.tracks ?? []).length);
  if (!album) {
    console.log("  note: no album with tracks in this library — the track menu cannot be measured");
    return;
  }
  // The track the pass works on, and the ROW that carries it: found by the
  // title its row shows, so the read of the DOM and the payload cannot point
  // at two different rows.
  const track = album.tracks.find((t) => !t.is_video) ?? album.tracks[0];
  const url = BASE + albumRoute(album);
  const open = async () => {
    await page.goto(url, { waitUntil: "networkidle", timeout: 60000 });
    await page.waitForTimeout(1200);
  };
  // The album's own tag menu (the header's) — the whole release, not a track.
  const albumMenu = () => page.locator('button[title="Tag actions"]').first();

  await open();
  const rows = page.locator("table tbody tr");
  const rowCount = await rows.count();
  check(`the tracklist rendered (${album.path.split("/").pop()})`, rowCount > 0, `${rowCount} rows`);
  const rowMatches = rows.filter({ hasText: track.tags.TITLE ?? track.file });
  check(`the row for "${track.tags.TITLE}" is on the page`, (await rowMatches.count()) === 1,
    `${await rowMatches.count()} of ${rowCount} rows match`);
  const row = rowMatches.first();

  // The trigger is the row's own "…" (TrackActionsMenu): `^=` because its
  // title carries the longer sentence the page-level tag menu does not.
  const triggers = await row.locator('button[title^="Track actions"]').count();
  check("a row carries exactly ONE actions trigger, the \"…\"",
    triggers === 1, `${triggers} triggers in the row`);

  // ---- the "…" opens that track's own page --------------------------------
  await row.hover(); // the row's own controls are revealed on hover
  await row.locator('button[title^="Track actions"]').first().click();
  await page.waitForTimeout(350);
  const items = await page.locator('[role="menu"] [role="menuitem"]').allInnerTexts();
  const labels = items.map((s) => s.trim());
  check("the row's \"…\" offers \"Open track page\"",
    labels.includes("Open track page"), JSON.stringify(labels.slice(0, 14)));
  await page.locator('[role="menu"] [role="menuitem"]', { hasText: /^Open track page$/ }).first().click();
  await page.waitForTimeout(1500);
  const landed = decodeURIComponent(new URL(page.url()).pathname);
  const heading = (await page.locator("h1").first().innerText({ timeout: 8000 }).catch(() => "")).trim();
  check("pressing \"Open track page\" lands on THAT track's own page",
    landed === `/track/${track.path}` && heading.includes(track.tags.TITLE ?? track.file),
    `${page.url()} h1 ${JSON.stringify(heading)} vs ${JSON.stringify(track.tags.TITLE)}`);

  // ---- a menu on the whole release does not offer it ----------------------
  await open();
  await albumMenu().click();
  await page.waitForTimeout(350);
  const albumItems = (await page.locator('[role="menu"] [role="menuitem"]').allInnerTexts())
    .map((s) => s.trim());
  check("a menu on the whole album offers no track-only entry",
    !albumItems.includes("Open track page"),
    `${albumItems.length} entries, ${JSON.stringify(albumItems.slice(0, 8))}`);
  await page.keyboard.press("Escape");
  await page.waitForTimeout(250);
}

/* ---- the Credits popout's identity header -------------------------------
 * The report this pins: the Credits modal printed the raw PATH where a name
 * belongs — the file's own location sitting in the title slot, saying what
 * the panel was opened ON rather than what the credits ARE. The panel draws
 * an identity HEADER now: an <h3> carrying the release's (or the recording's)
 * NAME, then one label/value line per fact the files state (Artist, Album,
 * label · catalogue number, barcode, date, country, type, media, and the
 * MusicBrainz ids), every value with its own copy button, and the raw path
 * LAST — small and dimmed, below the heading, never as it.
 *
 * What is asserted, and why it is the right thing to look at: a NAME has no
 * path separator and does not end in an audio extension, so a path handed to
 * the heading fails the shape check by itself; and the ORDER is the other
 * half — whatever path-shaped string is still drawn has to sit BELOW the
 * heading, with the grid's facts and a copy button drawn with it.
 *
 * Two entry points, because the same panel is mounted from both: the album
 * page's own actions menu ("All album actions" → "Credits", the whole
 * release) and a listed row's "…" (TrackActionsMenu → "Credits (this
 * track)…"). The track half skips itself, with a note, when no file in the
 * library carries a MUSICBRAINZ_TRACKID — there is no id for it to expect. */

/** The extensions a raw-path title ends in — the shape a heading must never
 *  have (the old subtitle was always one of these files, or the folder above
 *  it). */
const AUDIO_EXTS = [".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav"];

/** Whether a heading reads as a NAME: something there at all, no path
 *  separator, and no audio extension at its end. */
const nameShaped = (s) => !!s && !/[\\/]/.test(s) &&
  !AUDIO_EXTS.some((ext) => s.toLowerCase().endsWith(ext));

/** The open Credits dialog's identity header, read out of the DOM: the
 *  heading, the label/value rows of the grid under it, where the raw path
 *  line sits, and every path-shaped string drawn ABOVE the heading (exactly
 *  the shape the old subtitle had). */
const creditHeader = (page) => page.evaluate(() => {
  const dialog = document.querySelector('[role="dialog"][aria-label="Credits"]');
  if (!dialog) return null;
  const h3 = dialog.querySelector("h3");
  // The header block the heading opens: the h3's flex row and the bordered
  // container around it, which holds the fact grid and the path line.
  const header = h3 && h3.parentElement ? h3.parentElement.parentElement : null;
  const grid = header ? header.querySelector("div.grid") : null;
  const rows = [];
  if (grid) {
    // The grid is label/value PAIRS, one element each.
    const kids = [...grid.children];
    for (let i = 0; i + 1 < kids.length; i += 2)
      rows.push({ label: kids[i].textContent.trim(), value: kids[i + 1].textContent.trim() });
  }
  const top = h3 ? h3.getBoundingClientRect().top : null;
  const above = [...dialog.querySelectorAll("*")]
    .filter((el) => !el.children.length && /[\\/]/.test(el.textContent || ""))
    .map((el) => ({ text: el.textContent.trim(), top: Math.round(el.getBoundingClientRect().top) }))
    .filter((p) => top !== null && p.top < top - 1);
  const pathEl = header ? header.querySelector(".font-mono") : null;
  return {
    heading: h3 ? h3.textContent.trim() : "",
    rows, above,
    path: pathEl ? pathEl.textContent.trim() : "",
    pathTop: pathEl ? pathEl.getBoundingClientRect().top : null,
    copies: dialog.querySelectorAll('button[aria-label^="Copy"]').length,
    top,
  };
});

async function creditsChecks(page, check) {
  await page.setViewportSize({ width: 1440, height: 900 });
  const lib = await (await fetch(`${BASE}/api/library`)).json();
  // The album the pass works on: the library's first one with tracks, and one
  // whose files carry a MUSICBRAINZ_TRACKID is preferred, so the track half
  // below has a row to open the panel on (and its own "Track MBID" row to
  // find).
  let album = null;
  let identified = null;
  for (const ar of lib.artists ?? []) {
    for (const al of ar.albums ?? []) {
      if (!(al.tracks ?? []).length) continue;
      if (!album) album = al;
      if (al.tracks.some((t) => String(t.tags?.MUSICBRAINZ_TRACKID ?? "").trim())) {
        identified = al;
        break;
      }
    }
    if (identified) break;
  }
  album = identified ?? album;
  if (!album) {
    console.log("  --   credits: skipped, no album with tracks in this library");
    return;
  }
  // The facts the FILES state, straight from the library payload — what the
  // header is supposed to draw a row for. A name the panel already draws as
  // the heading is asserted in whichever slot it lands.
  const tagOf = (k) => String(album.meta?.[k] ?? "").trim();
  const albumName = tagOf("ALBUM") || album.path.split("/").pop();
  const url = BASE + albumRoute(album);

  // ---- the album page's own Credits entry --------------------------------
  const tag = `credits (album "${albumName}")`;
  await page.goto(url, { waitUntil: "networkidle", timeout: 60000 });
  await page.waitForTimeout(1000);
  const opener = page.locator('button[title="All album actions"]').first();
  if ((await opener.count()) === 0) {
    console.log(`  --   ${tag}: skipped, the album page offers no "All album actions" menu`);
    return;
  }
  await opener.click();
  await page.waitForTimeout(350);
  const menuLabels = (await page.locator('[role="menu"] [role="menuitem"]').allInnerTexts())
    .map((s) => s.trim());
  const entry = page.locator('[role="menu"] [role="menuitem"]', { hasText: /^Credits$/ }).first();
  check(`${tag}: the page's actions menu offers "Credits"`,
    (await entry.count()) === 1, JSON.stringify(menuLabels.slice(0, 14)));
  if ((await entry.count()) === 0) return;
  await entry.click();
  const dialog = page.locator('[role="dialog"][aria-label="Credits"]');
  await dialog.waitFor({ state: "visible", timeout: 10000 }).catch(() => {});
  check(`${tag}: the entry opens a dialog titled "Credits"`,
    await dialog.isVisible().catch(() => false));
  // The panel renders a spinner until the lookup lands; the header is what
  // this pass reads, so that is what it waits for.
  await dialog.locator("h3").first().waitFor({ timeout: 60000 }).catch(() => {});
  const h = await creditHeader(page);
  if (!h || !h.heading) {
    check(`${tag}: the panel drew an identity header (an <h3> heading)`, false,
      h ? "the open dialog has no heading" : "no Credits dialog in the DOM");
    await page.keyboard.press("Escape");
    return;
  }
  check(`${tag}: the heading is a NAME, not a path`,
    nameShaped(h.heading), JSON.stringify(h.heading));
  check(`${tag}: the heading is not the raw path line`,
    h.heading !== h.path,
    `heading ${JSON.stringify(h.heading)} vs path ${JSON.stringify(h.path)}`);
  const labels = h.rows.map((r) => r.label);
  check(`${tag}: the header grid drew its label/value facts`,
    h.rows.length > 0, JSON.stringify(h.rows.slice(0, 8)));
  // `Artist` the files state, plus a `Release MBID` row exactly when they
  // carry that id. The release's own NAME is normally the heading on an album
  // panel (CreditHeader leaves out a fact that would only repeat it), so it is
  // checked in whichever slot is drawn.
  const wantFacts = [];
  if (tagOf("ARTIST") || tagOf("ALBUMARTIST")) wantFacts.push("Artist");
  if (tagOf("MUSICBRAINZ_ALBUMID")) wantFacts.push("Release MBID");
  const missing = wantFacts.filter((l) => !labels.includes(l));
  check(`${tag}: the grid lists the facts the files state (${wantFacts.join(", ") || "none"})`,
    wantFacts.length > 0 && missing.length === 0,
    `missing ${JSON.stringify(missing)} of ${JSON.stringify(labels)}`);
  check(`${tag}: the release's name is drawn — the Album row, or the heading itself`,
    labels.includes("Album") || h.heading === albumName,
    `heading ${JSON.stringify(h.heading)} vs ${JSON.stringify(albumName)}; labels ${JSON.stringify(labels)}`);
  check(`${tag}: nothing path-shaped is drawn ABOVE the heading`,
    h.above.length === 0, JSON.stringify(h.above));
  check(`${tag}: the raw path rides below the heading (or is not drawn at all)`,
    h.pathTop === null || (h.top !== null && h.pathTop > h.top),
    `path ${JSON.stringify(h.path)} @ ${h.pathTop} vs heading @ ${h.top}`);
  check(`${tag}: the header gives its facts and ids a copy button`,
    h.copies >= 1, `${h.copies} button[aria-label^="Copy"]`);
  await page.keyboard.press("Escape");
  await page.waitForTimeout(250);

  // ---- the same header on a TRACK ----------------------------------------
  // TagActionsMenu mounts the very same panel for one file (`path`, not
  // `album`), so the header has to read the same way there — with the one row
  // only a track can carry.
  const track = (album.tracks ?? [])
    .find((t) => String(t.tags?.MUSICBRAINZ_TRACKID ?? "").trim());
  if (!track) {
    console.log(`  --   ${tag} (track): skipped, no file in this album carries a MUSICBRAINZ_TRACKID`);
    return;
  }
  const ttag = `credits (track "${track.tags.TITLE ?? track.file}")`;
  await page.goto(url, { waitUntil: "networkidle", timeout: 60000 });
  await page.waitForTimeout(1200);
  const row = page.locator("table tbody tr").filter({ hasText: track.tags.TITLE ?? track.file }).first();
  if ((await row.count()) === 0) {
    console.log(`  --   ${ttag}: skipped, the tracklist has no row naming it`);
    return;
  }
  await row.hover(); // the row's own controls are revealed on hover
  await row.locator('button[title^="Track actions"]').first().click();
  await page.waitForTimeout(350);
  const rowLabels = (await page.locator('[role="menu"] [role="menuitem"]').allInnerTexts())
    .map((s) => s.trim());
  const trackEntry = page.locator('[role="menu"] [role="menuitem"]',
    { hasText: /^Credits \(this track\)/ }).first();
  check(`${ttag}: the row's "…" offers "Credits (this track)…"`,
    (await trackEntry.count()) === 1, JSON.stringify(rowLabels.slice(0, 14)));
  if ((await trackEntry.count()) === 0) return;
  await trackEntry.click();
  await dialog.waitFor({ state: "visible", timeout: 10000 }).catch(() => {});
  check(`${ttag}: the entry opens a dialog titled "Credits"`,
    await dialog.isVisible().catch(() => false));
  await dialog.locator("h3").first().waitFor({ timeout: 60000 }).catch(() => {});
  const th = await creditHeader(page);
  if (!th || !th.heading) {
    check(`${ttag}: the panel drew an identity header (an <h3> heading)`, false,
      th ? "the open dialog has no heading" : "no Credits dialog in the DOM");
    await page.keyboard.press("Escape");
    return;
  }
  check(`${ttag}: the heading is a NAME, not a path`,
    nameShaped(th.heading), JSON.stringify(th.heading));
  check(`${ttag}: the file's MUSICBRAINZ_TRACKID is drawn as a "Track MBID" row`,
    th.rows.some((r) => r.label === "Track MBID"), JSON.stringify(th.rows.map((r) => r.label)));
  await page.keyboard.press("Escape");
  await page.waitForTimeout(250);
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
  if (want("pagemenu")) await pageMenuChecks(page, check);
  if (want("credits")) await creditsChecks(page, check);
  if (want("trackmenu")) { await trackMenuChecks(page, check); return finish(); }
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
    // the router. The heading is what the walk is really asserting, so it waits
    // for that rather than for `networkidle`: several pages poll (the progress
    // socket among them), so "no network in flight for 500ms" may never arrive.
    await page.locator(`aside a[href="${href}"]`).first().click();
    const h1 = await page.locator("h1").first().innerText({ timeout: 10000 }).catch(() => "");
    const landed = new URL(page.url()).pathname;
    check(`nav "${label}" → ${href} navigates and renders`,
      (landed === href || landed.startsWith(href + "/")) && h1.trim().length > 0,
      `${page.url()} ${JSON.stringify(h1)}`);
    // The active entry has to be readable as ACTIVE, not merely present: the
    // same assertion the phone drawer's walk makes, read once the paint has
    // stopped moving (see settledInk) and required to be the accent block.
    const active = page.locator(`aside a[href="${href}"][aria-current="page"]`);
    const ink = await active.evaluate(settledInk).catch(() => null);
    check(`nav "${label}" reads as current after the press`,
      (await active.count()) === 1 && !!ink && ink.bg === accentRgb(ink.accent),
      `${JSON.stringify(ink)} vs accent ${ink ? accentRgb(ink.accent) : "?"}`);
  }

  // The rail above is the desktop nav. The phone drawer is the same menu on
  // the device the app is installed on, and gets its own walk.
  await drawerChecks(browser, check, errs);

  await finish();
})();
