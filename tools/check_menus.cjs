#!/usr/bin/env node
/* Sidebar/menu assertions against a running app — text evidence instead of
 * eyeballing screenshots. Run: node tools/check_menus.cjs [baseUrl]
 *
 * Two groups of checks: the sidebar/menu battery, and the album cover "…"
 * menu's flyout geometry. `--only=cover` / `--only=nav` runs one group (the
 * cover group needs an album in the library that HAS cover art, and skips
 * itself when there is none). */
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

// Must stay in sync with NAV in web/src/App.tsx.
// Downloads is the browser's offline cache, a page of its own again; the
// Soulseek *staging* list stays a tab on the Soulseek page.
const EXPECTED_NAV = [
  "Home", "Library", "Genres", "Trash", "Playlists", "Favorites", "Downloads",
  "Import", "Soulseek", "Export", "Optimization", "Grading", "Dependencies", "Settings",
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
  if (!want("nav")) return finish();

  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.waitForTimeout(1500);

  const navText = await page.locator("aside a").allInnerTexts();
  const labels = navText.map((t) => t.trim()).filter(Boolean);
  console.log("sidebar:", JSON.stringify(labels));
  check("sidebar lists every NAV entry in order", JSON.stringify(labels) === JSON.stringify(EXPECTED_NAV),
    `got ${JSON.stringify(labels)}`);

  for (const path of ["/library", "/mb/search?q=test"]) {
    const res = await page.goto(BASE + path, { waitUntil: "networkidle" });
    await page.waitForTimeout(800);
    check(`${path} answers 200`, res && res.status() === 200, String(res && res.status()));
    const h1 = (await page.locator("h1").first().innerText().catch(() => "")).trim();
    check(`${path} renders a heading`, h1.length > 0, JSON.stringify(h1));
  }

  // Every sidebar entry must actually lead somewhere: click it and confirm the
  // route answers and renders — the menu/routes can never drift apart silently.
  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.waitForTimeout(800);
  const hrefs = await page.locator("aside a").evaluateAll((as) =>
    as.map((a) => ({ label: a.textContent.trim(), href: a.getAttribute("href") }))
  );
  for (const { label, href } of hrefs) {
    const res = await page.goto(BASE + href, { waitUntil: "networkidle" });
    await page.waitForTimeout(600);
    const status = res && res.status();
    const h1 = (await page.locator("h1").first().innerText().catch(() => "")).trim();
    check(`nav "${label}" → ${href} renders (${status})`, status === 200 && h1.length > 0,
      JSON.stringify(h1));
  }

  await finish();
})();
