#!/usr/bin/env node
/* The Library's alphabet toolbar, and the numbers that prove it.
 *
 *   * The Library's own name box and A–Z rail are TWO controls over ONE filter,
 *     so the check drives them the way a reader does and counts what comes
 *     back: typing "asgeir" must find the album whose artist is "Ásgeir" (the
 *     fold takes the accents off), the rail must print one count per letter
 *     over the list the view on screen draws, a letter must narrow to exactly
 *     the rows filed under it, the two must COMPOSE, all FIVE views must obey
 *     both — grid, compact, albums, artists, tracks — and clearing either must
 *     give the whole list back. Every count the rail prints is then spent: the
 *     rows a letter leaves are counted on screen, because a rail whose "B 1"
 *     led to two rows would be lying about the one thing it exists for.
 *     The rules themselves (fold, letter, and the `#` bucket a digit, a symbol
 *     or a CJK title lands in) are also read straight off
 *     lib/libraryView.ts in the browser, which is where a click cannot reach:
 *     no album in the payload is titled with a symbol, and only one has an
 *     accent, so the fallback cases are proved there.
 *   * Select mode's "Select all" is ONE control (components/SelectAllButton)
 *     over three different selections — the Library's five views, the artist
 *     page and the trash — so the check drives each wiring: the label's number
 *     against what the list shows (the toolbar's own counts, the artist rows,
 *     the entries), every drawn box ticked by one click, the batch bar reading
 *     the same number, and the same button clearing it again. With a name
 *     typed, "all" is what the filter left.
 *   * At 390 px the name box and the letter button must still share ONE row and
 *     stay inside the screen: the phone is where a toolbar turns into a stack.
 *
 * The payload is a REAL capture (tools/fixtures/library-az.json): a scratch
 * server over tools/make_test_library.py's synthetic library, grown by hand so
 * the names span the letters, the `#` bucket and one diacritic name — the
 * fixture's own `note` says where it came from. The check serves the REAL app
 * (Vite, `web/src`, on a scratch port — 8011 and up, never the owner's live
 * 8000) and stands the payload up as the app's own backend, so a run needs no
 * Python server and no network.
 *
 * Run:  node tools/check_library_az.mjs [payload.json]
 * Exit codes: 0 pass, 1 a check failed (the failing ones are printed), 2 the
 * environment cannot run it (no web/node_modules, no payload, no playwright).
 */
import { existsSync, readFileSync } from "node:fs";
import { createServer as createHttpServer } from "node:http";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const webDir = path.join(here, "..", "web");
const payloadPath = process.argv[2] || path.join(here, "fixtures", "library-az.json");
if (!existsSync(path.join(webDir, "node_modules"))) {
  console.error("[library-az] web/node_modules is missing — run `npm --prefix web install`.");
  process.exit(2);
}
if (!existsSync(payloadPath)) {
  console.error(`[library-az] no payload at ${payloadPath}`);
  process.exit(2);
}
const payload = JSON.parse(readFileSync(payloadPath, "utf8"));
const { library, album } = payload;
if (!library?.artists?.length || !album) {
  console.error("[library-az] the payload carries no library or no album answer.");
  process.exit(2);
}

/* The advisory a card draws is read off the FILES, not off one tag: the
 * album-level `ITUNESADVISORY` can lag the tracks inside it — measured on the
 * owner's Evil Empire, whose album tag says 0 while ten of its eleven tracks
 * say 1 — which is why a grid of covers said nothing about albums whose titles
 * plainly belong to explicit releases. The stub serves a copy adjusted to
 * exactly that pair of shapes: one album explicit only through its tracks, one
 * clean all the way down. */
const served = JSON.parse(JSON.stringify(library));
const albumAt = (n) => served.artists.flatMap((a) => a.albums)[n];
const explicitByTracks = albumAt(0);
const cleanEverywhere = albumAt(1);
explicitByTracks.meta = { ...(explicitByTracks.meta || {}), ITUNESADVISORY: "0" };
for (const t of explicitByTracks.tracks ?? []) t.tags = { ...(t.tags || {}), ITUNESADVISORY: "0" };
if (explicitByTracks.tracks?.[0]) explicitByTracks.tracks[0].tags.ITUNESADVISORY = "1";
cleanEverywhere.meta = { ...(cleanEverywhere.meta || {}), ITUNESADVISORY: "0" };
for (const t of cleanEverywhere.tracks ?? []) t.tags = { ...(t.tags || {}), ITUNESADVISORY: "0" };
const titleOf = (al) => al.meta?.ALBUM || al.path.split("/").pop();

let chromium;
const webRequire = createRequire(path.join(webDir, "package.json"));
try {
  ({ chromium } = webRequire("playwright"));
} catch {
  console.error("[library-az] Playwright not found — `npm --prefix web i -D playwright`.");
  process.exit(2);
}
const { createServer } = await import(
  `file://${path.join(webDir, "node_modules/vite/dist/node/index.js").replace(/\\/g, "/")}`);

/* The same working directory `npm --prefix web run dev` uses. Tailwind v3 finds
 * BOTH its config file and the `content` globs inside it relative to the
 * process's cwd, and this check runs from the repo root: from there the
 * utilities never load and every box on the page is unstyled — a layout check
 * would then measure a page no reader ever sees (the shelf came out
 * `display: block` with full-width cards, and the toolbar stacked). One chdir,
 * and the app under test is the app. */
process.chdir(webDir);

// ---- the payload, read the way the page reads it ---------------------------
/** The name an album row is filed under, in the same two forms the page uses:
 *  its title, and — in the two views that DRAW artist headers (the grid and the
 *  album table) — the artist in front of it. */
const albumName = (al, artist, grouped) => {
  const title = al.meta?.ALBUM || al.path.split("/").pop();
  return grouped ? `${artist} ${title}` : title;
};
const albums = [];
const artists = [];
const tracks = [];
for (const a of library.artists) {
  const artist = a.display_name || a.name;
  artists.push(artist);
  for (const al of a.albums) {
    albums.push(albumName(al, al.album_artist || artist, false));
    for (const t of al.tracks || []) tracks.push(t.tags?.TITLE || t.file);
  }
}
const groupedAlbumNames = library.artists.flatMap((a) => {
  const artist = a.display_name || a.name;
  return a.albums.map((al) => albumName(al, al.album_artist || artist, true));
});

// The alphabet rule, implemented HERE as well as in the app: fold the accents
// off, take the first character, `#` for anything that is not A–Z. A separate
// copy on purpose — this is the answer the app's own is compared against.
const fold = (s) => s.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
const letterOf = (name) => {
  const first = fold(name.trim()).charAt(0);
  return first >= "a" && first <= "z" ? first.toUpperCase() : "#";
};
const AZ_LETTERS = [..."ABCDEFGHIJKLMNOPQRSTUVWXYZ".split(""), "#"];
/** Every letter the rail draws, with the ones that hold nothing at zero: the
 *  menu prints a 0 beside a letter it has no rows for, so the answer to compare
 *  against is a full alphabet and not only the letters with something in them. */
const countsOf = (names) => {
  const out = {};
  for (const l of AZ_LETTERS) out[l] = 0;
  for (const n of names) out[letterOf(n)] += 1;
  return out;
};

const failures = [];
let checks = 0;
const check = (name, pass, detail = "") => {
  checks += 1;
  if (!pass) failures.push(detail ? `${name} — ${detail}` : name);
};
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// ---- the payload, standing in for the app's backend ------------------------
/* A real HTTP stub rather than Playwright's request interception: the shell
 * asks a handful of questions before ANY page renders — the server version, the
 * config (`first_run_done` is what decides between the setup wizard and the
 * app), the auth status — and one left unanswered sends the reader to the setup
 * wizard, where there is no list to check. These answers come from the fixture;
 * anything else the shell asks for gets an empty object, which is what keeps the
 * page's own reads (grades, the play counts) out of the way without
 * pretending they said something. */
const VERSION = {
  version: "4.0.3",
  latest: "4.0.3",
  update_available: false,
  release_url: "",
  project_url: "https://github.com/",
  issues_url: "https://github.com/",
  build: { revision: "", built: "", container: false },
  checked_at: 0,
  source: "unavailable",
};
/* The artist under test. */
const ARTIST_ALPHA = "Artist Alpha";

/** `GET /api/artist` for one library row: the row's own albums/aggregate and
 *  its `grade` — the artist folder's verdict, which the grading surfaces read. */
const artistPayload = (row) => ({
  path: row.path,
  name: row.name,
  display_name: row.display_name || row.name,
  albums: row.albums,
  aggregate: row.aggregate,
  grade: row.grade,
});
/* The trash's own payload: enough rows for "Select all" to mean something, and
 * one entry with no recorded origin — an older removal, the case the restore
 * path has to ask about instead of guessing a destination. */
const TRASH = {
  folder: "F:/tmp/mlo-lib-az/.mlo/trash",
  music_folder: "F:/tmp/mlo-lib-az",
  exists: true,
  count: 12,
  bytes: 1234567,
  entries: Array.from({ length: 12 }, (_, i) => {
    const name = `entry-${String(i + 1).padStart(2, "0")}`;
    return {
      name,
      path: `F:/tmp/mlo-lib-az/.mlo/trash/${name}`,
      kind: i % 3 === 0 ? "file" : "album",
      label: `Trashed ${i + 1}`,
      tracks: i % 3 === 0 ? 0 : 5,
      bytes: 1000 * (i + 1),
      trashed_at: "2026-01-01T00:00:00",
      cover: false,
      origin: i === 11 ? null : "F:/tmp/mlo-lib-az/Somewhere",
    };
  }),
};
const apiStub = createHttpServer((req, res) => {
  const url = req.url || "";
  const json = (body) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify(body));
  };
  if (url.startsWith("/api/auth/status")) {
    return json({ required: false, gate: false, local: true, has_password: false,
      authenticated: true, username: "", public_url: "", session_days: 30, setup_hint: "" });
  }
  if (url.startsWith("/api/version")) return json(VERSION);
  if (url.startsWith("/api/config")) {
    return json({ first_run_done: true, music_folder: "F:/tmp/mlo-lib-az", ui_locale: "en" });
  }
  if (url.startsWith("/api/library")) return json(served);
  if (url.startsWith("/api/album?")) return json(album);
  /* The artist page's own payload, built from the row the LIBRARY answer
   * carries — one `grade` object drives both. */
  if (url.startsWith("/api/artist?")) {
    const want = new URL(url, "http://127.0.0.1").searchParams.get("path") || "";
    const row = served.artists.find((a) => a.path === want);
    if (!row) {
      res.writeHead(404, { "content-type": "application/json" });
      return res.end("{}");
    }
    return json(artistPayload(row));
  }
  if (url.startsWith("/api/trash")) return json(TRASH);
  if (url.startsWith("/api/auth/users")) return json({ users: [] });
  // Everything else — the grade summary, the lock list — is a
  // question the pages under test guard on: an ERROR is what those guards are
  // written for (`if (!data) return null`), while a hand-made empty object is
  // a shape the component trusts and then reads a field off.
  res.writeHead(404, { "content-type": "application/json" });
  res.end("{}");
});
// The progress socket is muted: nothing here publishes frames, and the dev
// server's own proxy would otherwise point it at a live install.
apiStub.on("upgrade", (_req, socket) => socket.destroy());
await new Promise((ready) => apiStub.listen(0, "127.0.0.1", ready));
const apiPort = apiStub.address().port;

// The scratch app: Vite's own dev server on 8011 (or the next free port), so
// the page under test is the REAL app — index.html, the module graph, the
// store — and not a harness copy of it. Its `/api` and `/ws` are pointed at the
// stub above, over the config file's proxy to the owner's 8000.
const vite = await createServer({
  configFile: path.join(webDir, "vite.config.ts"),
  root: webDir,
  logLevel: "error",
  server: {
    host: "127.0.0.1",
    port: 8011,
    strictPort: false,
    proxy: {
      "/api": `http://127.0.0.1:${apiPort}`,
      "/ws": { target: `ws://127.0.0.1:${apiPort}`, ws: true },
    },
  },
});
try {
  await vite.listen();
} catch (e) {
  console.error("[library-az] the scratch app could not start: " + String(e));
  process.exit(2);
}
const base = `http://127.0.0.1:${vite.httpServer.address().port}`;

const browser = await chromium.launch();
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: "en-US" });
  // The service worker caches the shell for offline use and has no business in
  // a check: it would serve a stale app for the rest of the run.
  await context.route("**/sw.js*", (route) => route.abort());
  const page = await context.newPage();
  page.on("pageerror", (e) => failures.push(`the page threw: ${String(e).split("\n")[0]}`));
  /* React's own dev-mode complaint about a setState during a render. The one
   * place this app used to earn it is leaving select mode (the clear ran inside
   * the state updater, which React calls during the render it schedules), so
   * the select-mode checks below read it as a failure — a console warning is
   * not a failing test on its own, this one is a named bug. */
  const renderPhaseWarnings = [];
  page.on("console", (msg) => {
    if (/Cannot update a component/.test(msg.text())) renderPhaseWarnings.push(msg.text());
  });

  // ---------------------------------------------------------------- helpers
  /** Which of `wanted` the page is showing, by TEXT: an element whose whole
   *  text is that name and which the layout actually placed (a hidden element
   *  has no offsetParent). Text and not a test-only attribute, because text is
   *  what a reader counts — a name nothing writes anywhere is not on screen. */
  const shown = (wanted) =>
    page.evaluate((names) => {
      const text = [...document.querySelectorAll("a, span, td, div, h2")]
        .map((el) => (el.textContent || "").trim());
      return names.filter((n) => text.includes(n));
    }, wanted);

  const nameField = page.locator("input[aria-label='Filter the list by name']");
  const clearField = page.locator("button[aria-label='Clear the name filter']");
  const railButton = page.locator("button[aria-label='Filter the list by letter']");
  const railLabel = async () => (await railButton.innerText()).trim().replace(/\s+/g, "");
  const viewTab = (name) => page.getByRole("tab", { name, exact: true });
  const typeName = async (value) => {
    await nameField.fill(value);
    await page.waitForTimeout(80);
  };
  const railClick = (label) =>
    page.evaluate((what) => {
      const buttons = [...document.querySelectorAll(".popover-panel button")];
      const el = buttons.find((b) => {
        const m = /^([A-Z#])\s*(\d+)$/.exec((b.innerText || "").trim().replace(/\n/g, " "));
        return m && m[1] === what;
      });
      if (!el) {
        throw new Error(`no rail button reading ${what} among `
          + JSON.stringify(buttons.map((b) => (b.innerText || "").trim())));
      }
      el.click();
    }, label);
  /** Open, and only then click: an open panel carries a full-viewport click
   *  shield, so a second click on the trigger would land on the shield and shut
   *  it again. */
  const openRail = async () => {
    if ((await page.locator(".popover-panel").count()) === 0) await railButton.click();
    await page.waitForSelector(".popover-panel");
  };
  /** Escape, the same way a reader dismisses it — the panel covers the list it
   *  is filtering, and the rows are what the next checks count. */
  const closeRail = async () => {
    if (await page.locator(".popover-panel").count()) {
      await page.keyboard.press("Escape");
      await page.waitForSelector(".popover-panel", { state: "detached" });
    }
  };
  /** The numbers the rail is printing, read off the menu's own buttons: each
   *  one is a letter with its count under it ("B" over "1"). */
  const railCounts = () =>
    page.$$eval(".popover-panel button", (els) => {
      const out = {};
      for (const el of els) {
        const m = /^([A-Z#])\s*(\d+)$/.exec((el.innerText || "").trim().replace(/\n/g, " "));
        if (m) out[m[1]] = Number(m[2]);
      }
      return out;
    });
  const countSpan = async () => {
    const found = await page.$$eval("span", (els) =>
      els.map((el) => (el.textContent || "").trim()).filter((t) => /^\d+ albums · \d+ tracks$/.test(t)));
    return found[0] || "";
  };

  // ---------------------------------------------- 1. the rail's own rules
  await page.goto(`${base}/library`);
  await page.waitForSelector("input[aria-label='Filter the list by name']", { timeout: 30000 });
  await page.waitForSelector(`a[title="${albums[0]}"]`, { timeout: 30000 });

  const rules = await page.evaluate(async () => {
    const m = await import("/src/lib/libraryView.ts");
    return {
      letters: m.AZ_LETTERS.join(""),
      folded: m.foldName("Ásgeir"),
      accentedLetter: m.azLetterOf("Ásgeir"),
      digit: m.azLetterOf("1989"),
      cjk: m.azLetterOf("教育"),
      symbol: m.azLetterOf("…And Justice for All"),
      blank: m.azLetterOf("   "),
      kept: m.azKeep("Ásgeir Hljóð", "asgeir", null),
      letterKept: m.azKeep("Belle and Sebastian", "", "B"),
      letterDropped: m.azKeep("Belle and Sebastian", "", "A"),
      composed: m.azKeep("Belle and Sebastian", "sebas", "B"),
      composedOut: m.azKeep("Belle and Sebastian", "sebas", "N"),
      counts: m.azCounts(["Ásgeir Hljóð", "Belle and Sebastian", "1989", "教育", "   "], ""),
    };
  });
  check("the rail offers A–Z and #, in that order",
        rules.letters === "ABCDEFGHIJKLMNOPQRSTUVWXYZ#", rules.letters);
  check("an accent is folded away, so Ásgeir is found by asgeir", rules.folded === "asgeir", rules.folded);
  check("and it files under A", rules.accentedLetter === "A", rules.accentedLetter);
  check("a digit files under #", rules.digit === "#", rules.digit);
  check("a CJK title files under #", rules.cjk === "#", rules.cjk);
  check("a leading symbol files under #", rules.symbol === "#", rules.symbol);
  check("and a name with no first character at all does too", rules.blank === "#", rules.blank);
  check("the folded name matches the typed word", rules.kept === true, String(rules.kept));
  check("a letter keeps the rows filed under it and drops the rest",
        rules.letterKept === true && rules.letterDropped === false,
        JSON.stringify([rules.letterKept, rules.letterDropped]));
  check("the words and the letter compose — both have to hold",
        rules.composed === true && rules.composedOut === false,
        JSON.stringify([rules.composed, rules.composedOut]));
  check("the counts put each name under its own letter",
        same(rules.counts, { A: 1, B: 1, "#": 3 }), JSON.stringify(rules.counts));

  // ------------------------------------- 2. the Library, at a wide window
  check("the grid opens on the whole library",
        same((await shown(albums)).sort(), [...albums].sort()),
        `${(await shown(albums)).length}/${albums.length} albums in the grid`);
  const allCount = await countSpan();

  await typeName("asgeir");
  check("typing a name without its accent finds the accented album (grid)",
        same(await shown(albums), ["Hljóð"]), JSON.stringify(await shown(albums)));
  check("and the header's own count follows the filter",
        (await countSpan()).startsWith("1 albums"), await countSpan());

  await clearField.click();
  await page.waitForTimeout(80);
  check("the ✕ clears the name box and the whole grid is back",
        (await shown(albums)).length === albums.length, `${(await shown(albums)).length} albums`);

  await openRail();
  const gridCounts = await railCounts();
  const wantGrouped = countsOf(groupedAlbumNames);
  check("the rail counts one number per letter over the list this view draws",
        same(gridCounts, wantGrouped), `${JSON.stringify(gridCounts)} vs ${JSON.stringify(wantGrouped)}`);
  check("every album in the grid is counted exactly once",
        Object.values(gridCounts).reduce((a, b) => a + b, 0) === albums.length,
        `${Object.values(gridCounts).reduce((a, b) => a + b, 0)} of ${albums.length}`);

  await railClick("B");
  await page.waitForTimeout(80);
  check("picking a letter narrows the grid to the rows filed under it",
        same(await shown(albums), ["If You Are Feeling Sinister"]), JSON.stringify(await shown(albums)));
  check("and the letter is what the button shows", (await railLabel()) === "B", await railLabel());
  check("the number the rail printed for that letter is the number of rows it left",
        (await shown(albums)).length === gridCounts.B, `${(await shown(albums)).length} vs ${gridCounts.B}`);
  await closeRail();

  await typeName("nope");
  check("a letter and a name that cannot both hold leave nothing — and say so",
        (await shown(albums)).length === 0
          && (await page.locator("p", { hasText: "Nothing here matches" }).count()) === 1,
        JSON.stringify(await shown(albums)));
  await typeName("sinister");
  check("the two compose: with B picked, the album's own title still narrows",
        same(await shown(albums), ["If You Are Feeling Sinister"]), JSON.stringify(await shown(albums)));

  await openRail();
  await page.evaluate(() => {
    const el = [...document.querySelectorAll(".popover-panel button")]
      .find((b) => (b.innerText || "").includes("Show all"));
    if (!el) throw new Error("no clear row in the rail");
    el.click();
  });
  await page.waitForTimeout(80);
  await closeRail();
  check("the menu's own ✕ clears the letter, and the name it was composed with stays",
        same(await shown(albums), ["If You Are Feeling Sinister"]), JSON.stringify(await shown(albums)));
  check("and the button goes back to naming the rail itself", (await railLabel()) === "A–Z", await railLabel());

  await clearField.click();
  await page.waitForTimeout(80);
  check("clearing both gives the whole library back",
        (await shown(albums)).length === albums.length && (await countSpan()) === allCount,
        `${(await shown(albums)).length} albums, "${await countSpan()}"`);

  /* ---- ONE artist, however its albums are spelled ------------------------
   * The owner's library held ONE artist tagged two ways ("System of a Down"
   * beside "System Of A Down": a hand-tagged album next to the folder's own
   * spelling), and the Grid and the album table grouped on the raw tag — so one
   * artist drew two headers and its albums were split across them. The group key
   * is the app's own name fold now (the one the A–Z rail files names under), and
   * the header is the FOLDER's spelling — the name the Artists view and the
   * artist page both draw. The fixture carries the case: one
   * "Zed Case" folder whose second album is tagged "ZED CASE". */
  const gridHeaders = () => page.evaluate(() =>
    [...document.querySelectorAll("main div.col-span-full")]
      .map((el) => (el.firstElementChild?.textContent || "").trim())
      .filter(Boolean));
  const heads = await gridHeaders();
  const caseHeads = heads.filter((h) => h.toLowerCase() === "zed case");
  check("two spellings of one artist draw ONE header, not two",
        caseHeads.length === 1, JSON.stringify(heads));
  check("and the header is the artist folder's spelling, not a tag's",
        caseHeads[0] === "Zed Case", JSON.stringify(caseHeads));
  // Both albums under it — the header is furniture around its own cards, so the
  // count is the cards between this header and the next.
  const underHeader = await page.evaluate(() => {
    const all = [...document.querySelectorAll("main div.col-span-full")];
    const at = all.findIndex((h) => (h.firstElementChild?.textContent || "").trim().toLowerCase() === "zed case");
    if (at < 0) return -1;
    let n = 0;
    for (let el = all[at].nextElementSibling; el && !el.classList.contains("col-span-full"); el = el.nextElementSibling) n += 1;
    return n;
  });
  check("both of that artist's albums sit under its one header",
        underHeader === 2, `${underHeader} cards`);

  const tableHeaders = async () => page.evaluate(() =>
    [...document.querySelectorAll("main table tbody tr td[colspan]")]
      .map((td) => (td.textContent || "").trim()));
  await viewTab("Albums").click();
  await page.waitForTimeout(80);
  const tableHeads = await tableHeaders();
  check("the Albums table draws that one header too, the same way",
        tableHeads.filter((h) => h.toLowerCase() === "zed case").length === 1
          && tableHeads.filter((h) => h.toLowerCase() === "zed case")[0] === "Zed Case",
        JSON.stringify(tableHeads));
  await viewTab("Grid").click();
  await page.waitForTimeout(80);

  // ---- each of the five views obeys both controls ----
  // Compact and Albums both draw album rows; only the album TABLE draws artist
  // headers, and the name a row answers to follows the headers the view draws
  // (`azGroupedAlbums`), so the two are asked with the words that name the row
  // they each show: the compact list is a title list ("hljod" for Hljóð — the
  // accent folded away), the table is grouped, so its albums answer to their
  // artist ("asgeir") as well.
  for (const v of [
    { tab: "Compact", word: "hljo", names: albums, want: ["Hljóð"] },
    { tab: "Albums", word: "asgeir", names: albums, want: ["Hljóð"] },
    { tab: "Artists", word: "belle", names: artists, want: ["Belle and Sebastian"] },
  ]) {
    await viewTab(v.tab).click();
    await page.waitForTimeout(60);
    check(`${v.tab}: the whole list is on screen to begin with`,
          (await shown(v.names)).length === v.names.length, `${(await shown(v.names)).length}/${v.names.length}`);
    await typeName(v.word);
    check(`${v.tab}: the name box filters this list too`,
          same(await shown(v.names), v.want), JSON.stringify(await shown(v.names)));
    await clearField.click();
    await page.waitForTimeout(60);
  }

  // The tracks view: every title, and the rail's `#` is the two CJK ones.
  await viewTab("Tracks").click();
  await page.waitForTimeout(80);
  check("Tracks: the whole table opens on every track",
        (await shown(tracks)).length === tracks.length, `${(await shown(tracks)).length}/${tracks.length}`);
  await typeName("vetur");
  check("Tracks: the name box narrows to the track title, accents folded",
        same(await shown(tracks), ["Í Vetur"]), JSON.stringify(await shown(tracks)));
  await clearField.click();
  await page.waitForTimeout(60);
  await openRail();
  const trackCounts = await railCounts();
  check("Tracks: the rail counts TRACK titles, not albums",
        same(trackCounts, countsOf(tracks)), `${JSON.stringify(trackCounts)} vs ${JSON.stringify(countsOf(tracks))}`);
  await railClick("#");
  await page.waitForTimeout(80);
  const cjk = tracks.filter((t) => letterOf(t) === "#");
  check("Tracks: # leaves the titles no A–Z letter covers, and only those",
        same((await shown(tracks)).sort(), [...cjk].sort()),
        `${JSON.stringify(await shown(tracks))} vs ${JSON.stringify(cjk)}`);
  check("Tracks: the letter rides on the button here too", (await railLabel()) === "#", await railLabel());
  await railClick("#");
  await page.waitForTimeout(80);
  check("pressing the letter that is already on turns it off again",
        (await shown(tracks)).length === tracks.length && (await railLabel()) === "A–Z",
        `${(await shown(tracks)).length} tracks, button ${await railLabel()}`);
  await closeRail();

  await viewTab("Artists").click();
  await page.waitForTimeout(60);
  await openRail();
  await railClick("T");
  await page.waitForTimeout(80);
  check("Artists: the rail filters the artist list to the letter it names",
        same(await shown(artists), ["Taylor Swift"]), JSON.stringify(await shown(artists)));
  await railClick("T");
  await page.waitForTimeout(60);
  await closeRail();

  await viewTab("Grid").click();
  await page.waitForTimeout(60);
  await typeName("");
  await page.waitForTimeout(60);

  // --------------------------------- 3. the Library, at a phone's window
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(150);
  const phone = await page.evaluate(() => {
    const field = document.querySelector("input[aria-label='Filter the list by name']").parentElement;
    const rail = document.querySelector("button[aria-label='Filter the list by letter']");
    const f = field.getBoundingClientRect();
    const r = rail.getBoundingClientRect();
    return {
      vw: window.innerWidth,
      // Where the two sit: the same ROW (their centres line up — the button is
      // the taller of the two, so its top edge is not the field's) and one after
      // the other across it, which is what "they share one row" means.
      centerGap: Math.round(Math.abs((f.top + f.height / 2) - (r.top + r.height / 2))),
      sideGap: Math.round(r.left - f.right),
      fLeft: Math.round(f.left), rRight: Math.round(r.right), fWidth: Math.round(f.width),
      overflow: document.documentElement.scrollWidth - window.innerWidth,
    };
  });
  check("390 px: the name box and the letter button share ONE row",
        phone.centerGap <= 8 && phone.sideGap >= 0 && phone.sideGap <= 32, JSON.stringify(phone));
  check("390 px: and both sit inside the screen",
        phone.fLeft >= 0 && phone.rRight <= phone.vw && phone.fWidth >= 60, JSON.stringify(phone));
  check("390 px: nothing runs off the side of the page",
        phone.overflow <= 1, `scrollWidth exceeds the viewport by ${phone.overflow} px`);
  await typeName("asgeir");
  check("390 px: the phone's own width filters the same list",
        same(await shown(albums), ["Hljóð"]), JSON.stringify(await shown(albums)));
  await clearField.click();
  await page.waitForTimeout(60);
  await page.setViewportSize({ width: 1440, height: 900 });

  // ------------------------- 5. the album's advisory mark
  // Every card reads the album's advisory through the same rule
  // (`albumAdvisory`), so the check walks both shapes it must get right: one
  // album that is explicit only through its tracks, and one that says clean
  // everywhere. The mark is the boxed letter the reader sees next to the title.
  await page.goto(`${base}/library`);
  await viewTab("Grid").click();
  await page.waitForSelector(`a[title="${titleOf(explicitByTracks)}"]`, { timeout: 30000 });
  const marks = await page.evaluate(([explicit, clean]) => {
    const read = (title) => {
      const link = document.querySelector(`a[title="${CSS.escape(title)}"]`);
      const block = link ? link.closest("div.mt-2") : null;
      if (!block) return null;
      return [...block.querySelectorAll("span")]
        .filter((s) => s.title === "Explicit" || s.title === "Clean")
        .map((s) => `${s.title}:${s.textContent}`);
    };
    return { explicit: read(explicit), clean: read(clean) };
  }, [titleOf(explicitByTracks), titleOf(cleanEverywhere)]);
  check(`a card whose tracks say explicit wears the mark (${titleOf(explicitByTracks)})`,
        Array.isArray(marks.explicit) && marks.explicit.includes("Explicit:E"),
        JSON.stringify(marks));
  check(`…and an album that says clean everywhere wears none (${titleOf(cleanEverywhere)})`,
        Array.isArray(marks.clean) && marks.clean.length === 0, JSON.stringify(marks));
  // ------------------------- 6. a stored column list that would draw nothing
  // The owner's Tracks view rendered a header of `#` and a column of row
  // numbers with nothing in it ("NOTHING SHOWS UP"). The view is seeded with
  // exactly that stored list — only the row-number column — and must still draw
  // its data columns, because a list that leaves nothing but furniture is not a
  // choice anyone made about which columns to read.
  await page.addInitScript(() => {
    localStorage.setItem("mlo-cols4-tracks", JSON.stringify(["num"]));
  });
  await page.goto(`${base}/library`);
  await page.waitForSelector("text=Tracks", { timeout: 30000 });
  await viewTab("Tracks").click();
  await page.waitForTimeout(900);
  const seededCols = await page.evaluate(() => {
    const table = document.querySelector("table");
    if (!table) return null;
    const head = [...table.querySelectorAll("thead th")].map((th) => (th.textContent || "").trim());
    const cells = [...(table.querySelectorAll("tbody tr")[0]?.querySelectorAll("td") ?? [])].length;
    // The stored list is repaired, not just ignored: the Columns menu draws
    // what this hook returns, so a list left as `["num"]` would make the next
    // tick collapse the table to the one column clicked.
    const stored = JSON.parse(localStorage.getItem("mlo-cols4-tracks") || "[]");
    return { head, cells, stored: stored.length, storedHasNum: stored.includes("num") };
  });
  check(`a stored list holding only the row number still draws the Tracks columns `
        + `(${(seededCols?.head ?? []).filter(Boolean).length} headers, ${seededCols?.cells ?? 0} cells in row 1)`,
        !!seededCols && seededCols.head.filter(Boolean).length >= 8 && seededCols.cells >= 8,
        JSON.stringify(seededCols));
  check(`…and the stored list is repaired, so the next toggle works from what is on screen `
        + `(${seededCols?.stored ?? 0} ids stored, # kept: ${seededCols?.storedHasNum})`,
        !!seededCols && seededCols.stored >= 8 && seededCols.storedHasNum === true,
        JSON.stringify(seededCols));
  /* ---- 8b. Select mode's "Select all" -----------------------------------
   * The owner's ask: one click for the whole list, wherever the Select button
   *     is. It is the same control in three places (lib: components/SelectAllButton)
   *     over three different selections, so the check drives each wiring: the
   *     Library's five views, the artist page and the trash.
   *
   * Two claims are read off the running app rather than assumed:
   *   * the number in the label is the number the list shows — the toolbar's
   *     own count for the albums and tracks views, the artist rows for the
   *     artists view, the entries for the trash;
   *   * clicking it ticks EVERY box the view draws, and clicking it again (it
   *     is the way back out once everything is ticked) leaves nothing ticked
   *     and no batch bar behind.
   * The filtered case is the third: with "asgeir" typed, "all" is the one album
   * left, not the eleven in the library.
   */
  const selectBtn = page.getByRole("button", { name: "Select", exact: true });
  const selectAllBtn = page.getByRole("button", { name: /^(Select all|Deselect all) \d+ / });
  const allLabel = async () => (await selectAllBtn.innerText()).replace(/\s+/g, " ").trim();
  const boxCounts = () =>
    page.$$eval("input[type=checkbox]", (els) => ({
      drawn: els.length,
      ticked: els.filter((e) => e.checked).length,
    }));
  /** The batch bar's own sentence, whichever page drew it: the Library's
   *  "N albums · M artists · K tracks · T total tracks", the artist page's
   *  "N albums selected", the trash's "N selected". Empty when no bar is
   *  drawn at all. */
  const barLine = () =>
    page.evaluate(() =>
      [...document.querySelectorAll("span, div")]
        .map((el) => (el.textContent || "").replace(/\s+/g, " ").trim())
        .find((t) =>
          /^\d+ albums? · \d+ artists? · \d+ tracks? · \d+ total tracks$/.test(t)
          || /^\d+ albums? selected$/.test(t)
          || /^\d+ entries selected$/.test(t)
          || /^\d+ selected$/.test(t)) || "");
  const headerCounts = async () => {
    const m = /^(\d+) albums · (\d+) tracks$/.exec(await countSpan());
    return m ? { albums: Number(m[1]), tracks: Number(m[2]) } : null;
  };
  /* The artists table: one row per artist, each of several cells (the empty
   * state is one cell spanning them all). */
  const artistRowCount = () =>
    page.$$eval("main table tbody tr", (rows) =>
      rows.filter((r) => r.querySelectorAll("td").length > 1).length);

  await page.setViewportSize({ width: 1440, height: 900 });
  for (const c of [
    { tab: "Grid", noun: "albums", want: async () => (await headerCounts())?.albums },
    { tab: "Compact", noun: "albums", want: async () => (await headerCounts())?.albums },
    { tab: "Albums", noun: "albums", want: async () => (await headerCounts())?.albums },
    { tab: "Artists", noun: "artists", want: async () => artistRowCount() },
    { tab: "Tracks", noun: "tracks", want: async () => (await headerCounts())?.tracks },
  ]) {
    await page.goto(`${base}/library`);
    await page.waitForSelector(`a[title="${albums[0]}"]`, { timeout: 30000 });
    await viewTab(c.tab).click();
    await page.waitForTimeout(80);
    await selectBtn.click();
    await page.waitForTimeout(80);
    const want = await c.want();
    const label = await allLabel();
    check(`${c.tab}: Select mode offers the whole list in one click, and says how many`,
          label === `Select all ${want} ${c.noun}`, `"${label}" vs ${want} ${c.noun}`);
    const before = await boxCounts();
    check(`${c.tab}: nothing is ticked before that click`,
          before.ticked === 0, JSON.stringify(before));
    await selectAllBtn.click();
    await page.waitForTimeout(80);
    const on = await boxCounts();
    check(`${c.tab}: one click ticks every box the view draws (${on.ticked}/${on.drawn})`,
          on.drawn > 0 && on.ticked === on.drawn, JSON.stringify(on));
    const bar = await barLine();
    check(`${c.tab}: and the batch bar reads the same number it promised`,
          bar.includes(`${want} ${c.noun}`), `"${bar}" for ${want} ${c.noun}`);
    check(`${c.tab}: the button is the way back out while everything is ticked`,
          (await allLabel()) === `Deselect all ${want} ${c.noun}`, await allLabel());
    await selectAllBtn.click();
    await page.waitForTimeout(80);
    const off = await boxCounts();
    check(`${c.tab}: unticks every one of them again, and the bar goes with them`,
          off.ticked === 0 && (await barLine()) === "", `${JSON.stringify(off)} "${await barLine()}"`);
  }

  // "All" is the list the filters left, not the library: the button's own
  // promise, on the view that opened.
  await page.goto(`${base}/library`);
  await page.waitForSelector(`a[title="${albums[0]}"]`, { timeout: 30000 });
  await selectBtn.click();
  await typeName("asgeir");
  check("Grid: with a name typed, 'all' is what the filter left — one album, not the library",
        (await allLabel()) === "Select all 1 albums", await allLabel());
  await selectAllBtn.click();
  await page.waitForTimeout(80);
  const filtered = await boxCounts();
  check("and ticking it ticks exactly that one",
        filtered.drawn === 1 && filtered.ticked === 1 && (await barLine()).startsWith("1 album ·"),
        `${JSON.stringify(filtered)} "${await barLine()}"`);

  // Leaving select mode drops the ticks with it — and does it from the
  // handler, not from inside the state updater React runs during a render.
  const warningsBeforeLeaving = renderPhaseWarnings.length;
  await page.goto(`${base}/library`);
  await page.waitForSelector(`a[title="${albums[0]}"]`, { timeout: 30000 });
  await selectBtn.click();
  await selectAllBtn.click();
  await page.waitForTimeout(80);
  await selectBtn.click();                       // leave select mode
  await page.waitForTimeout(120);
  const left = await boxCounts();
  check("leaving select mode drops what was ticked, and the boxes go with it",
        left.drawn === 0 && left.ticked === 0 && (await barLine()) === "",
        `${JSON.stringify(left)} "${await barLine()}"`);
  check("and it is not a store write inside a render (React's setState-in-render warning)",
        renderPhaseWarnings.length === warningsBeforeLeaving,
        renderPhaseWarnings.slice(warningsBeforeLeaving).join(" | "));

  // ---- the artist page: every album it lists ----
  const alphaRow = served.artists.find((a) => a.name === ARTIST_ALPHA);
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  await page.goto(`${base}/artist/${encodeURIComponent(alphaRow.path)}`);
  await selectBtn.waitFor({ timeout: 30000 });
  await selectBtn.click();
  await page.waitForTimeout(80);
  check("the artist page offers its whole discography in one click",
        (await allLabel()) === `Select all ${alphaRow.albums.length} albums`, await allLabel());
  await selectAllBtn.click();
  await page.waitForTimeout(80);
  const artistBoxes = await boxCounts();
  check("and ticking it ticks every album card on the page",
        artistBoxes.drawn === alphaRow.albums.length && artistBoxes.ticked === artistBoxes.drawn,
        JSON.stringify(artistBoxes));
  check("the artist page's own bar counts albums, in the singular when it is one",
        (await barLine()) === `${plural(alphaRow.albums.length, "album")} selected`, await barLine());

  // ---- the trash: the entries, grid view and table view alike ----
  await page.goto(`${base}/trash`);
  await selectBtn.waitFor({ timeout: 30000 });
  await selectBtn.click();
  await page.waitForTimeout(80);
  check("the trash offers every entry in one click",
        (await allLabel()) === `Select all ${TRASH.entries.length} entries`, await allLabel());
  await selectAllBtn.click();
  await page.waitForTimeout(80);
  const trashGrid = await boxCounts();
  check("its grid ticks every entry card",
        trashGrid.drawn === TRASH.entries.length && trashGrid.ticked === trashGrid.drawn,
        JSON.stringify(trashGrid));
  check("and the trash's bar counts the selection",
        (await barLine()) === `${TRASH.entries.length} selected`, await barLine());
  await viewTab("Albums").click();
  await page.waitForTimeout(80);
  const trashTable = await boxCounts();
  check("the table view keeps the selection — every entry row plus its own header box",
        trashTable.drawn === TRASH.entries.length + 1 && trashTable.ticked === trashTable.drawn
          && (await page.$$eval("main table thead input[type=checkbox]", (els) => els.every((e) => e.checked))),
        JSON.stringify(trashTable));
  check("and the button still offers the way out there",
        (await allLabel()) === `Deselect all ${TRASH.entries.length} entries`, await allLabel());

  /* ---- 9. stored column widths: hostile maps, and the reader's own ------
   * The browser-level reproduction of the owner's blank Albums/Tracks views:
   * a stored width map with 0/garbage values used to be applied verbatim into a
   * `table-layout: fixed` table and collapsed every data column. sanitizeWidths
   * drops anything the drag handle could not have produced (the setter clamps
   * to 40-900) so a column keeps its own floor from the `_COL_W` maps — while a
   * SANE stored width still has to be honoured, because it is the reader's own
   * choice.
   *
   * The seed is copied into the real key by an init script on every load: an
   * init script of its own per case would keep re-writing the older maps on
   * every later navigation (they accumulate on the context). */
  await page.addInitScript(() => {
    const seed = localStorage.getItem("mlo-test-width-seed");
    if (!seed) return;
    for (const [key, raw] of Object.entries(JSON.parse(seed))) {
      if (raw === null) localStorage.removeItem(key);
      else localStorage.setItem(key, raw);
    }
  });
  /** The table that carries a given header label, as drawn: the box its
   *  `overflow-x-auto` wrapper gives it, the column count, and the cover cell
   *  of its first real data row (the Artists table's rows are single-cell
   *  group headings, and the Albums table's first row is one too). */
  const tableView = (label) => page.evaluate((head) => {
    const table = [...document.querySelectorAll("main table")]
      .find((t) => [...t.querySelectorAll("thead th")].some((th) => (th.textContent || "").trim() === head));
    if (!table) return null;
    const wrap = table.parentElement;
    const heads = [...table.querySelectorAll("thead th")];
    const names = heads.map((th) => (th.textContent || "").trim());
    const coverAt = names.indexOf("Cover");
    // The first row that draws a whole row's worth of cells.
    const row = [...table.querySelectorAll("tbody tr")].find((tr) => tr.querySelectorAll("td").length > 2);
    const widths = {};
    heads.forEach((th, i) => { if (names[i]) widths[names[i]] = Math.round(th.getBoundingClientRect().width); });
    const coverCell = row && coverAt >= 0 ? row.querySelectorAll("td")[coverAt] : null;
    return {
      tableW: Math.round(table.getBoundingClientRect().width),
      wrapClient: wrap.clientWidth, wrapScroll: wrap.scrollWidth,
      headers: names.filter(Boolean).length,
      cells: row ? row.querySelectorAll("td").length : 0,
      cover: coverCell ? Math.round(coverCell.getBoundingClientRect().width) : 0,
      widths,
    };
  }, label);

  /** Load the Library with `seed` (a {key: rawJson} map) in place and open one
   *  view. */
  const widthCase = async (seed, tab, label) => {
    await page.evaluate((s) => localStorage.setItem("mlo-test-width-seed", s), JSON.stringify(seed));
    await page.goto(`${base}/library`);
    await page.waitForSelector("text=Artists", { timeout: 30000 });
    await viewTab(tab).click();
    await page.waitForTimeout(900);
    return tableView(label);
  };

  const zeroTracks = ["num", "cover", "title", "artist", "album", "year", "genre", "duration", "bitrate", "dr"];
  const hostileTracks = [];
  for (const [what, raw] of [
    ["every column pinned to 0", JSON.stringify(Object.fromEntries(zeroTracks.map((id) => [id, 0])))],
    ["a width that is not a number", JSON.stringify({ title: "wide", album: "wide" })],
    ["an array instead of a map", JSON.stringify([120, 240, 360])],
    ["null", "null"],
    ["a legacy key shape", JSON.stringify({ "tags.TITLE": 200, Title: 120, TITLE: 90 })],
  ]) {
    const t = await widthCase({ "mlo-colw-tracks": raw }, "Tracks", "Title");
    hostileTracks.push({ what, t });
    check(`Tracks: a stored width map holding ${what} still draws the table `
          + `(${t?.headers ?? 0} headers, ${t?.cells ?? 0} cells, cover ${t?.cover ?? 0} px)`,
          !!t && t.headers >= 8 && t.cells >= 8 && t.cover >= 20, JSON.stringify(t?.widths));
  }
  const zeroAlbums = await widthCase(
    { "mlo-colw-albums": JSON.stringify({ album: 0, artist: 0, year: 0, tracks: 0, grade: 0, media: 0 }) },
    "Albums", "Album");
  check(`Albums: the same hostile map leaves the album table's rows intact `
        + `(${zeroAlbums?.headers ?? 0} headers, ${zeroAlbums?.cells ?? 0} cells, cover ${zeroAlbums?.cover ?? 0} px)`,
        !!zeroAlbums && zeroAlbums.headers >= 8 && zeroAlbums.cells >= 8 && zeroAlbums.cover >= 20,
        JSON.stringify(zeroAlbums?.widths));
  /* A stored width is a PREFERENCE about how the table's width is spent, never
   * a floor the table has to be that wide. `table-layout: fixed` makes the sum
   * of a table's columns its own floor, so a stored map — every value inside
   * the handle's own 40-900 clamp, i.e. nothing the sanitizer may drop — used
   * to draw a 1420 px Artists table in a 1200 px box and an album tracklist of
   * 3848 px: the owner's "columns are VERY long ... rows seem really wide".
   * useFittedWidths gives each column its own floor first and shares what is
   * left over among the columns the reader sized, so a table comes out exactly
   * as wide as it would with no stored widths at all, and the sideways scroll
   * stays for the one case it is the design for: a table whose own floors
   * cannot fit (the Tracks view's 1532 px of floors in a 1200 px box). */
  const cleanTracks = await widthCase({ "mlo-colw-tracks": null, "mlo-colw-albums": null }, "Tracks", "Title");
  await page.setViewportSize({ width: 801, height: 900 });
  const trackFloors = await widthCase({ "mlo-colw-tracks": null }, "Tracks", "Title");
  await page.setViewportSize({ width: 1440, height: 900 });
  const mappedTracks = await widthCase(
    { "mlo-colw-tracks": JSON.stringify({ title: 900, artist: 900, album: 900, bitrate: 900 }) }, "Tracks", "Title");
  const shrunkTracks = Object.keys(trackFloors?.widths ?? {}).filter(
    (label) => (mappedTracks?.widths?.[label] ?? 0) + 1 < (trackFloors?.widths?.[label] ?? 0));
  check(`Tracks: a stored map cannot widen the table — ${mappedTracks?.tableW ?? 0} px with the map, `
        + `${cleanTracks?.tableW ?? 0} px without it (wrapper ${mappedTracks?.wrapClient ?? 0} px)`,
        !!cleanTracks && !!mappedTracks && mappedTracks.tableW === cleanTracks.tableW
          && mappedTracks.wrapScroll === cleanTracks.wrapScroll && shrunkTracks.length === 0,
        shrunkTracks.join(",") || JSON.stringify(mappedTracks?.widths));
  const cleanAlbums = await widthCase({ "mlo-colw-albums": null }, "Albums", "Album");
  await page.setViewportSize({ width: 801, height: 900 });
  const albumFloors = await widthCase({ "mlo-colw-albums": null }, "Albums", "Album");
  await page.setViewportSize({ width: 1440, height: 900 });
  const mappedAlbums = await widthCase(
    { "mlo-colw-albums": JSON.stringify({ album: 900, artist: 900, media: 900 }) }, "Albums", "Album");
  const shrunkAlbums = Object.keys(albumFloors?.widths ?? {}).filter(
    (label) => (mappedAlbums?.widths?.[label] ?? 0) + 1 < (albumFloors?.widths?.[label] ?? 0));
  check(`Albums: the same — ${mappedAlbums?.tableW ?? 0} px with the map, ${cleanAlbums?.tableW ?? 0} px without it `
        + `(wrapper ${mappedAlbums?.wrapClient ?? 0} px)`,
        !!cleanAlbums && !!mappedAlbums && mappedAlbums.tableW === cleanAlbums.tableW
          && mappedAlbums.wrapScroll === cleanAlbums.wrapScroll && shrunkAlbums.length === 0,
        shrunkAlbums.join(",") || JSON.stringify(mappedAlbums?.widths));
  /* And where the columns DO fit, the reader's width is honoured in the room
   * there is: a 300 px grade on the Artists tab at 1440 (its columns' floors
   * leave 604 px of 1200). The Tracks view has no such room to demonstrate —
   * its 1532 px of floors nearly fill its own box (the library's content area
   * caps at ~1552 px), which is the case R313 keeps the sideways scroll for. */
  const artistsSane = await widthCase(
    { "mlo-colw-artists": JSON.stringify({ grade: 300 }) }, "Artists", "Artist");
  check(`a stored width is honoured where the table has room for it `
        + `(Artists grade 300 px -> ${artistsSane?.widths?.Grade ?? 0} px, its floor is 112 px)`,
        (artistsSane?.widths?.Grade ?? 0) >= 300, JSON.stringify(artistsSane?.widths));

  /* ---- 10. the Artists tab must not scroll while its columns fit --------
   * The owner's screenshot: a short artist list with a scrollbar along the
   * bottom. Its four columns' own floors sum to 596 px, so the box has room
   * for them; the floor of the check is the FLOOR of each column, measured
   * here at 801 px (where the artists table is exactly its own floors), and
   * then the same table at 1440 with four legal 900 px drag widths stored. */
  await page.setViewportSize({ width: 801, height: 900 });
  const artistFloors = await widthCase({ "mlo-colw-artists": null }, "Artists", "Artist");
  await page.setViewportSize({ width: 1440, height: 900 });
  const artistsClean = await widthCase({ "mlo-colw-artists": null }, "Artists", "Artist");
  const overWide = await widthCase(
    { "mlo-colw-artists": JSON.stringify({ albums: 900, tracks: 900, checks: 900, grade: 900 }) },
    "Artists", "Artist");
  check(`Artists: the map cannot widen or scroll the table while its columns fit `
        + `(${overWide?.tableW ?? 0} px / wrapper ${overWide?.wrapScroll ?? 0} of ${overWide?.wrapClient ?? 0} px; `
        + `clean ${artistsClean?.tableW ?? 0} px)`,
        !!overWide && overWide.tableW === artistsClean?.tableW
          && overWide.wrapScroll <= overWide.wrapClient + 1,
        JSON.stringify(overWide?.widths));
  const shrunk = Object.keys(artistFloors?.widths ?? {}).filter(
    (label) => (overWide?.widths?.[label] ?? 0) < (artistFloors?.widths?.[label] ?? 0));
  check(`…and every column keeps at least its own floor `
        + `(${["Releases", "Tracks", "Checks", "Grade"].map((l) => `${l} ${artistFloors?.widths?.[l]}->${overWide?.widths?.[l]}`).join(", ")})`,
        shrunk.length === 0, shrunk.join(","));
  console.log(`\n[stored widths] hostile Tracks maps draw `
    + hostileTracks.map(({ what, t }) => `${t?.headers ?? 0}h/${t?.cells ?? 0}c/cover ${t?.cover ?? 0}px (${what})`).join(", ")
    + ` | hostile Albums map ${zeroAlbums?.headers ?? 0}h/${zeroAlbums?.cells ?? 0}c/cover ${zeroAlbums?.cover ?? 0}px`);
  console.log(`[library-az tables @1440] Artists ${cleanTracks ? "clean" : ""} `
    + `${artistsClean?.tableW ?? 0} px (hostile ${overWide?.tableW ?? 0} px, wrapper ${overWide?.wrapScroll ?? 0}/${overWide?.wrapClient ?? 0}), `
    + `Albums ${cleanAlbums?.tableW ?? 0} px (hostile ${mappedAlbums?.tableW ?? 0} px, scroll ${mappedAlbums?.wrapScroll ?? 0}/${mappedAlbums?.wrapClient ?? 0}), `
    + `Tracks ${cleanTracks?.tableW ?? 0} px (hostile ${mappedTracks?.tableW ?? 0} px, scroll ${mappedTracks?.wrapScroll ?? 0}/${mappedTracks?.wrapClient ?? 0})`);
  await page.evaluate(() => localStorage.removeItem("mlo-test-width-seed"));
} finally {
  await browser.close();
  await vite.close();
  await new Promise((done) => apiStub.close(done));
}

console.log(`\n[library-az] ${checks - failures.length}/${checks} checks passed`);
if (failures.length) {
  console.log(`\nFAIL — ${failures.length} problem(s)`);
  for (const f of failures) console.log(`  - ${f}`);
  process.exit(1);
}
console.log("\nPASS — the Library's alphabet filter and select mode's Select all");
