#!/usr/bin/env node
/* The Library's alphabet toolbar, and the album page's LOCAL recommendation
 * shelf — the two things the owner asked for, and the numbers that prove them.
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
 *   * The album page's "Recommended (Local)" shelf was a single horizontal row
 *     that ran off the shelf's own width and cut the card at the edge (the
 *     owner's screenshot: six cards, the sixth in halves). It is a wrapped grid
 *     now, and the check measures that: one grid, more than one row, every
 *     card's right edge inside the shelf's, and no horizontal overflow in it.
 *   * The two shelves on that page are read as a PAIR and the reader counts
 *     them, so the local shelf must ask for the same 12 rows the online shelf
 *     asks for and print the count it got (issue #54) — the request body is
 *     kept by the stub and the printed count is read off the heading line.
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
const { library, album, recommend, album_path: albumPath } = payload;
if (!library?.artists?.length || !album || !recommend?.items?.length || !albumPath) {
  console.error("[library-az] the payload carries no library, no album answer or no recommendation answer.");
  process.exit(2);
}

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
 * page's own reads (grades, ratings, the play counts) out of the way without
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
/* Every body the shelf POSTs to `/api/recommend`, kept for the check at the
 * bottom of this file: how many rows the local shelf ASKS for is half of the
 * pair the server suite proves (tools/test_recommendations.py asserts the
 * server's own default is the same number). */
const recommendAsked = [];
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
  if (url.startsWith("/api/library")) return json(library);
  if (url.startsWith("/api/album?")) return json(album);
  if (url.startsWith("/api/recommend")) {
    // The shelf asks with a POST body (a playlist or a favourites set is a seed
    // LIST); the older GET form has none. Either way the answer is the
    // fixture's, and the body is kept for the check below.
    let raw = "";
    req.on("data", (chunk) => { raw += chunk; });
    req.on("end", () => {
      let body = {};
      try { body = JSON.parse(raw || "{}"); } catch { body = {}; }
      recommendAsked.push(body);
      json(recommend);
    });
    return;
  }
  if (url.startsWith("/api/ratings")) return json({ ratings: {} });
  if (url.startsWith("/api/auth/users")) return json({ users: [] });
  // Everything else — the grade summary, the favorites, the lock list — is a
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

  // ------------------------- 4. the album page's Recommended (Local) shelf
  await page.goto(`${base}/album/${encodeURIComponent(albumPath)}`);
  await page.waitForSelector("h2:has-text('Recommended (Local)')", { timeout: 30000 });
  // The cards enter on `.stagger`, which animates each one up from below on its
  // own delay: measured mid-animation every card reports a different top and
  // there is no layout to read. Settled first.
  await page.waitForTimeout(800);
  const shelf = await page.evaluate(() => {
    const h = [...document.querySelectorAll("h2")]
      .find((el) => (el.textContent || "").includes("Recommended (Local)"));
    const section = h.closest("section");
    const grid = [...section.querySelectorAll("div")]
      .find((el) => (el.style.gridTemplateColumns || "").includes("auto-fill"));
    if (!grid) return null;
    const box = grid.getBoundingClientRect();
    const style = getComputedStyle(grid);
    const cards = [...grid.children].map((el) => {
      const r = el.getBoundingClientRect();
      return {
        // offsetTop/Left are the LAYOUT position — a transform (the entry
        // animation, a hover lift) moves the painted box without moving the
        // grid, and the grid is what is being checked here.
        row: el.offsetTop,
        col: el.offsetLeft,
        left: r.left, right: r.right, width: Math.round(r.width),
      };
    });
    return {
      display: style.display,
      overflowX: style.overflowX,
      // The heading line: the title AND the count this shelf prints beside it.
      // `h.parentElement` is the flex row holding the icon, the title and the
      // meta slot.
      heading: (h.parentElement?.textContent || "").replace(/\s+/g, " ").trim(),
      // The RESOLVED layout, not the template string: how many columns the
      // browser actually drew is how many distinct left edges the cards have.
      columns: new Set(cards.map((c) => c.col)).size,
      scrollWidth: grid.scrollWidth,
      clientWidth: grid.clientWidth,
      boxLeft: box.left,
      boxRight: box.right,
      rows: new Set(cards.map((c) => c.row)).size,
      cards,
    };
  });
  check("the album page draws the local shelf", shelf !== null, "no grid found under the shelf's heading");
  if (shelf) {
    const items = recommend.items.length;
    check("the shelf's cards are laid out in a grid, not the old scroller",
          shelf.display === "grid" && shelf.overflowX !== "auto",
          `display ${shelf.display}, overflow-x ${shelf.overflowX}`);
    check("every recommended album is on the shelf", shelf.cards.length === items,
          `${shelf.cards.length} cards for ${items} items`);
    check("the cards wrap onto more than one row",
          shelf.rows >= 2, `${shelf.rows} row(s) across ${shelf.columns} column(s)`);
    check("and the rows are full ones — no orphan row of half a row's worth",
          shelf.rows === Math.ceil(shelf.cards.length / shelf.columns),
          `${shelf.cards.length} cards over ${shelf.rows} rows at ${shelf.columns} columns`);
    const outside = shelf.cards.filter((c) => c.right > shelf.boxRight + 0.5 || c.left < shelf.boxLeft - 0.5);
    check("no card is cut off at the shelf's edge (the sixth card in halves)",
          outside.length === 0, JSON.stringify(outside));
    check("and the shelf itself does not scroll sideways",
          shelf.scrollWidth <= shelf.clientWidth + 1,
          `scrollWidth ${shelf.scrollWidth} vs clientWidth ${shelf.clientWidth}`);
    const widths = shelf.cards.map((c) => c.width);
    check("every card clears the Library grid's own floor (164 px)",
          Math.min(...widths) >= 164, `narrowest card ${Math.min(...widths)} px`);
    // The pair's NUMBERS — issue #54. The page's two shelves are read side by
    // side and the reader counts them, so the local shelf asks for the same 12
    // the online shelf asks for (the server suite proves the server's own
    // default is that same number) and prints the count it got, which is what
    // makes "9 local albums beside 12 online suggestions" read as the library
    // rather than as a shelf that lost three rows.
    check("the local shelf asks for 12 rows — the same number the online shelf asks for",
          recommendAsked.length > 0 && recommendAsked.every((b) => b.limit === 12),
          JSON.stringify(recommendAsked));
    const counted = `${items} album${items === 1 ? "" : "s"}`;
    check(`the shelf prints its own count (${counted}), like the online shelf beside it`,
          shelf.heading.includes(counted), JSON.stringify(shelf.heading));
    console.log(`\n[library-az] shelf: ${shelf.cards.length} cards, ${shelf.rows} rows x ${shelf.columns} columns, `
      + `card widths ${Math.min(...widths)}–${Math.max(...widths)} px, heading "${shelf.heading}"`);
  }
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
console.log("\nPASS — the Library's alphabet filter, and the album page's wrapped shelf");
