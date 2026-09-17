#!/usr/bin/env node
/* Trash end-to-end: view, restore, and permanent delete — through the page,
 * against a real backend, with the user's own trashed albums left untouched.
 *
 * What it proves, in order:
 *   1. a fixture album moved to the bin by the real endpoint comes back to
 *      the EXACT path it was trashed from (one click, no prompt);
 *   2. an entry with no recorded origin cannot be restored silently — the row
 *      asks for a destination and lands there;
 *   3. path escapes are refused, and a refused delete frees nothing;
 *   4. deleting through the UI erases exactly the entry it was pointed at;
 *   5. select mode + the selection bar expose the batch actions, and the
 *      Empty trash / Delete permanently affordances exist;
 *   6. every entry that was in the bin before the run is still there, with
 *      the same bytes — the only entries that disappear are the fixtures this
 *      script created.
 *
 * Needs a live backend serving the built app (`web/dist`):
 *   npm --prefix web run build
 *   python -m uvicorn server.main:app --host 127.0.0.1 --port 8000
 *   node tools/check_trash.cjs http://127.0.0.1:8000
 *
 * Playwright is required (same resolution as tools/shot.cjs): set PLAYWRIGHT
 * to a module path, or install it. Exit 2 when it is missing. */

const fs = require("fs");
const path = require("path");

let chromium;
try {
  ({ chromium } = require(process.env.PLAYWRIGHT || "playwright"));
} catch {
  console.error("[trash] Playwright not found — `npm i -D playwright`, " +
    "or point PLAYWRIGHT at an installed module.");
  process.exit(2);
}

const BASE = process.argv[2] || process.env.BASE || "http://127.0.0.1:8000";
const FIXTURE_TRASHED = "__mlo_check_album__";   // moved to the bin via the API
const FIXTURE_ORPHAN = "__mlo_check_orphan__";   // dropped straight into the bin
const DEST_LIB = "__mlo_check_dest__";           // library-side restore target
const results = [];
const check = (name, pass, detail) => results.push({ name, pass: !!pass, detail: String(detail) });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const getTrash = async () => await (await fetch(BASE + "/api/trash")).json();
const post = async (route, body) =>
  await (await fetch(BASE + route, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  })).json();

const dirs = [];   // everything this script creates, removed in the finally
const mkFixture = (dir) => {
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "01 - check.flac"), Buffer.alloc(2048, 7));
  dirs.push(dir);
};

(async () => {
  let browser;
  try {
    const before = await getTrash();
    const library = String(before.music_folder || "").replace(/\\/g, "/");
    const bin = String(before.folder || "").replace(/\\/g, "/");
    if (!library) {
      // The contract field is missing: this is almost always a backend started
      // before the trash/restore work — point at a fresh one instead of
      // failing further down with a confusing "outside music folder".
      throw new Error(`the backend at ${BASE} does not serve the current trash contract ` +
        `(no music_folder) — restart it, or point this check at a freshly started server`);
    }
    const real = before.entries.filter((e) => !e.name.startsWith("__mlo_check"));
    // Raw directory contents, not just the parsed entries: a leftover
    // bookkeeping file (the origin manifest) or any other residue shows up
    // here and nowhere else.
    const binFilesBefore = fs.readdirSync(bin).length;
    check("trash endpoint answers", typeof before.exists === "boolean", JSON.stringify(before).slice(0, 140));
    check("trash folder is the app's bin", bin.endsWith("/.mlo/trash"), bin);
    check("payload carries the music folder for restore prompts", !!library, library);
    check("path escapes are refused",
      (await post("/api/trash/delete", { names: ["../outside.txt", "..\\outside.txt", "a/b", ".", "..", ""] }))
        .deleted.length === 0,
      "at least one escape name was accepted by the API");
    check("refused deletes free nothing",
      (await post("/api/trash/delete", { names: ["../outside.txt", "..\\outside.txt"] })).freed === 0, "");
    console.log(`  (bin holds ${real.length} real entr${real.length === 1 ? "y" : "ies"}, ${before.bytes} bytes)`);

    browser = await chromium.launch({ executablePath: process.env.CHROME, headless: true });
    const page = await browser.newContext({ viewport: { width: 1440, height: 900 } }).then((c) => c.newPage());
    page.setDefaultTimeout(15000);
    const errs = [];
    page.on("pageerror", (e) => errs.push(e.message));
    let promptAnswer = null;
    let lastConfirm = "";
    page.on("dialog", async (d) => {
      if (d.type() === "prompt") { lastConfirm = d.message(); await d.accept(promptAnswer ?? ""); }
      else { lastConfirm = d.message(); await d.accept(); }
    });
    /** Switch the view tabs (accessible-name matching can miss, so fall back
     *  to a plain DOM click). */
    const setView = async (label) => {
      await page.evaluate((l) => {
        const b = [...document.querySelectorAll("button")].find((x) => new RegExp(`^\\s*${l}\\s*$`).test(x.innerText));
        if (b) b.click();
      }, label);
      await sleep(600);
    };
    const openTrash = async (view = "Albums") => {
      await page.goto(BASE + "/trash", { waitUntil: "domcontentloaded" });
      await page.waitForSelector("h1");
      // Like the library, the page comes up in the grid; the row-level
      // assertions below need the Albums presentation.
      await setView(view);
      await page.waitForSelector("tbody tr");
    };
    const rowFor = (name) => page.locator("tbody tr").filter({ hasText: name });

    // ---- 1. trashed album comes back to exactly where it was ---------------
    const albumDir = path.join(library, FIXTURE_TRASHED);
    mkFixture(albumDir);
    check("temp artist folder recreated by the fixture", fs.existsSync(albumDir), albumDir);

    const moved = await post("/api/album/remove", { path: albumDir });
    check("an album moved to the bin reports its trash path", !!moved.ok && String(moved.trash).length > 0, JSON.stringify(moved));
    const listed = await getTrash();
    const pooled = listed.entries.find((e) => e.name === FIXTURE_TRASHED);
    check("the bin records where the album came from",
      !!pooled && String(pooled.origin || "").replace(/\\/g, "/") === albumDir.replace(/\\/g, "/"),
      pooled ? `origin=${pooled.origin}` : "entry missing");

    await openTrash();
    const row = rowFor(FIXTURE_TRASHED);
    check("the trashed album is listed as restorable",
      (await row.getByRole("button", { name: /^\s*Restore\s*$/ }).count()) === 1,
      "a row with a known origin must offer a one-click Restore");
    await row.getByRole("button", { name: /^\s*Restore\s*$/ }).click();
    await sleep(1500);
    check("restore puts it back at the original path",
      fs.existsSync(path.join(albumDir, "01 - check.flac")), albumDir);
    check("restore drops it from the bin",
      !(await getTrash()).entries.some((e) => e.name === FIXTURE_TRASHED), "the restored entry is still in the bin");

    // ---- 2. an entry with no recorded origin asks where to put it ---------
    const orphanDir = path.join(bin, FIXTURE_ORPHAN);
    mkFixture(orphanDir);
    const destDir = path.join(library, DEST_LIB);
    fs.mkdirSync(destDir, { recursive: true });
    dirs.push(destDir);
    await openTrash();
    const orphanRow = rowFor(FIXTURE_ORPHAN);
    check("an entry with no origin offers 'Restore to…' instead of a silent no-op",
      (await orphanRow.getByRole("button", { name: /Restore to/ }).count()) === 1,
      "an origin-less row must offer Restore to…");
    promptAnswer = destDir;
    await orphanRow.getByRole("button", { name: /Restore to/ }).click();
    await sleep(1500);
    check("the destination prompt names the entry and the reason",
      /original location was not recorded/i.test(lastConfirm), JSON.stringify(lastConfirm.slice(0, 120)));
    check("the entry lands in the folder the prompt was given",
      fs.existsSync(path.join(destDir, FIXTURE_ORPHAN, "01 - check.flac")),
      path.join(destDir, FIXTURE_ORPHAN));

    // ---- 3. permanent delete through the UI ------------------------------
    const doomed = path.join(bin, "__mlo_check_delete__");
    mkFixture(doomed);
    await openTrash();
    const doomedRow = rowFor("__mlo_check_delete__");
    await doomedRow.getByRole("button", { name: /Delete permanently/ }).click();
    await sleep(1500);
    check("deleting asks first and says it is permanent",
      /cannot be undone/i.test(lastConfirm), JSON.stringify(lastConfirm.slice(0, 120)));
    check("the deleted entry is gone from disk", !fs.existsSync(doomed), doomed);

    // ---- 4. the viewer's own affordances ---------------------------------
    // Library-viewer parity: the same view tabs, the same column menu, and the
    // library's album→tracklist expansion replaced by the entry's file list.
    const tabs = await page.evaluate(() =>
      [...document.querySelectorAll("button")].map((b) => b.innerText.trim()).filter((t) => /^(Grid|Albums|Compact|Artists|Tracks)$/.test(t)));
    check("the same view tabs as the library", tabs.includes("Grid") && tabs.includes("Albums"),
      `tabs=${JSON.stringify(tabs)}`);
    check("the Albums view offers the library's column menu",
      (await page.getByRole("button", { name: /Columns/i }).count()) >= 1, "no Columns control in the Albums view");

    await page.locator("tbody tr").first().click();
    await sleep(600);
    const filesListed = await page.evaluate(() => {
      const nested = [...document.querySelectorAll("tbody table")];
      if (!nested.length) return null;
      const rows = [...nested[0].querySelectorAll("tbody tr")];
      return {
        head: [...nested[0].querySelectorAll("thead th")].map((t) => t.innerText.trim()),
        texts: rows.map((tr) => tr.innerText.replace(/\s+/g, " ").trim()),
        count: rows.length,
      };
    });
    const payloadEntry = (await getTrash()).entries.find((e) => e.name === real[0]?.name);
    check("expanding an entry lists the files inside it (library tracklist idiom)",
      !!filesListed && filesListed.count > 0 && /name|size/i.test(filesListed.head.join(" ")),
      JSON.stringify(filesListed));
    check("the listed files are the entry's real files",
      !!filesListed && !!payloadEntry &&
        payloadEntry.files.slice(0, 3).every((f) => filesListed.texts.some((t) => t.startsWith(f.name))),
      `listed=${JSON.stringify(filesListed?.texts.slice(0, 3))} api=${JSON.stringify(payloadEntry?.files.slice(0, 3).map((f) => f.name))}`);
    await page.locator("tbody tr").first().click();
    await sleep(300);

    // Grid view renders the shared album card.
    await setView("Grid");
    await sleep(400);
    const grid = await page.evaluate(() => {
      const imgs = [...document.querySelectorAll("main img, div img")].filter((i) => /\/api\/cover/.test(i.getAttribute("src") || ""));
      return { covers: imgs.length, text: (document.body.innerText.match(/files?\s·/g) || []).length };
    });
    check("Grid view renders album cards with covers",
      grid.covers >= 1, JSON.stringify(grid));
    await setView("Albums");
    await sleep(400);

    // Layout: the row actions must stay reachable. They were clipped off the
    // right edge before (table content 103px wider than a non-scrolling
    // wrapper, Delete button 78px past the viewport).
    await page.setViewportSize({ width: 1280, height: 900 });
    await openTrash();
    const layout = await page.evaluate(() => {
      const table = document.querySelector("table");
      const wrap = table.parentElement;
      const btns = [...document.querySelectorAll("tbody tr:first-child button")];
      return {
        viewport: window.innerWidth,
        tableOverflow: table.scrollWidth - table.clientWidth,
        wrapOverflow: wrap.scrollWidth - wrap.clientWidth,
        wrapClass: wrap.className,
        maxRight: Math.max(...btns.map((b) => Math.round(b.getBoundingClientRect().right))),
      };
    });
    check("row actions fit at 1280px (no clipped buttons)",
      layout.maxRight <= layout.viewport && layout.tableOverflow === 0,
      `rightmost button=${layout.maxRight} viewport=${layout.viewport} table overflow=${layout.tableOverflow}px wrap=${layout.wrapClass}`);
    check("the table is never clipped by a non-scrolling wrapper",
      layout.wrapOverflow <= 0 || /overflow-x-auto/.test(layout.wrapClass),
      `wrapper overflow=${layout.wrapOverflow}px class=${layout.wrapClass}`);
    await page.setViewportSize({ width: 1440, height: 900 });
    await openTrash();
    check("the toolbar offers Empty trash",
      (await page.getByRole("button", { name: /Empty trash/ }).count()) === 1, "no Empty trash button in the toolbar");
    check("the toolbar offers Select mode",
      (await page.locator('button[title*="Select mode"]').count()) === 1, "no Select toggle in the toolbar");
    await page.locator('button[title*="Select mode"]').click();
    await page.locator('thead input[type="checkbox"]').check();
    await sleep(400);
    // The accent selection bar the library uses, not just any "selected" text.
    const selBar = page.locator("div.bg-accent\\/15").filter({ hasText: /selected/i }).first();
    const barText = (await selBar.count()) ? (await selBar.innerText()).replace(/\s+/g, " ") : "";
    check("select-all raises the selection bar with the batch actions",
      /\d+\s*selected/i.test(barText) && /Restore/.test(barText) && /Delete permanently/.test(barText),
      JSON.stringify(barText.slice(0, 120)));
    const restoredInBar = selBar.getByRole("button", { name: /^\s*Restore\s*$/ });
    check("the selection bar acts on the whole selection",
      (await restoredInBar.count()) === 1, "selection-bar Restore missing");
    await page.getByRole("button", { name: /^\s*Clear\s*$/ }).click();
    await sleep(200);

    // ---- 5. nothing else was touched -------------------------------------
    const after = await getTrash();
    check("every pre-existing entry survived, byte for byte",
      real.length === after.entries.filter((e) => !e.name.startsWith("__mlo_check")).length &&
      real.every((e) => {
        const now = after.entries.find((x) => x.name === e.name);
        return now && now.bytes === e.bytes && now.tracks === e.tracks;
      }),
      `${real.length} before -> ${after.entries.map((e) => e.name).join(", ")}`);
    check("no fixture is left in the bin", !after.entries.some((e) => e.name.startsWith("__mlo_check")),
      after.entries.map((e) => e.name).join(", "));
    // Nothing at all should be left behind — no manifest residue either.
    const binFilesAfter = fs.readdirSync(bin).length;
    check("the bin directory itself is back to its original contents",
      binFilesAfter === binFilesBefore,
      `${binFilesBefore} -> ${binFilesAfter} entries: ${JSON.stringify(fs.readdirSync(bin))}`);
    check("no uncaught page errors", errs.length === 0, errs.join(" | "));
  } catch (e) {
    check("check script ran to completion", false, String(e).split("\n")[0]);
  } finally {
    // Remove only what this script created — never anything pre-existing.
    for (const d of dirs) {
      try { fs.rmSync(d, { recursive: true, force: true }); } catch { /* reported below */ }
    }
    const leftover = dirs.filter((d) => fs.existsSync(d));
    check("fixtures cleaned up", leftover.length === 0, leftover.join(", "));
    if (browser) await browser.close().catch(() => {});
  }
  for (const r of results) console.log(`${r.pass ? "ok  " : "FAIL"} ${r.name} :: ${r.detail}`);
  const bad = results.filter((r) => !r.pass).length;
  console.log(`\n${results.length - bad}/${results.length} checks pass`);
  process.exit(bad ? 1 : 0);
})();
