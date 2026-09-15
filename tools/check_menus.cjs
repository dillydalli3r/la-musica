#!/usr/bin/env node
/* Sidebar/menu assertions against a running app — text evidence instead of
 * eyeballing screenshots. Run: node tools/check_menus.cjs [baseUrl] */
const { chromium } = require("C:/Users/dillydallier/AppData/Roaming/npm/node_modules/omniroute/node_modules/playwright");

const BASE = process.argv[2] || process.env.BASE || "http://127.0.0.1:8000";

// Must stay in sync with NAV in web/src/App.tsx.
const EXPECTED_NAV = [
  "Home", "Library", "Playlists", "Favorites", "Import", "Soulseek",
  "MusicBrainz", "Export", "Optimization", "Grading", "Dependencies", "Settings",
];

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
  check("no uncaught page errors", errs.length === 0, errs.join(" | "));
  await browser.close();
  console.log(`\n${fail ? "FAIL" : "PASS"} — ${fail} problem(s)`);
  process.exit(fail ? 1 : 0);
})();
