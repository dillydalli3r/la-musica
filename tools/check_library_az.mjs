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
 *   * The ARTIST's own verdict is one green dot beside the name — the artist
 *     folder's checks (image + description, `grade_artist`) — instead of the
 *     two chips the artist hero used to spell out ("albums", "artist artwork
 *     2/2"), and the SAME dot, from the SAME payload field, is drawn wherever
 *     a name is listed: the hero, the Library's Artists view, Home's artist
 *     shelf and Favorites' artist table. The stub answers all four out of the
 *     two `grade` objects the payload carries (one artist passing, one
 *     failing), so the check reads both sides of the rule on every surface.
 *     The artist hero's cover-derived backdrop is measured too — the blurred
 *     layer covers the hero's box and is extended past it, it is masked by a
 *     gradient whose transparent stop is already reached along the hero's
 *     whole border (a fade, never the hard line `overflow-hidden` used to slice
 *     it off at), and the legibility rules it must not touch — the layer's
 *     opacity and the `bg` gradient over it — are still exactly what they were.
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
delete explicitByTracks.meta.ALBUMITUNESADVISORY;
for (const t of explicitByTracks.tracks ?? []) t.tags = { ...(t.tags || {}), ITUNESADVISORY: "0" };
if (explicitByTracks.tracks?.[0]) explicitByTracks.tracks[0].tags.ITUNESADVISORY = "1";
cleanEverywhere.meta = { ...(cleanEverywhere.meta || {}), ITUNESADVISORY: "0", ALBUMITUNESADVISORY: "0" };
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
/* A 4x4 PNG (the fixture's artists claim `has_image`, so the page asks for
 * one): real bytes, so the hero's backdrop layer is a layer that painted
 * something rather than a broken image the browser drew a glyph for. */
const PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGOsiDrBAANMcBZeDgBQyAGi+HlBlgAAAABJRU5ErkJggg==",
  "base64");
/* The artist under test. */
const ARTIST_ALPHA = "Artist Alpha";
const ARTIST_BETA = "Artist Beta";

/** `GET /api/artist` for one library row: the row's own albums/aggregate plus
 *  the artwork block the page draws (image, description, provenance) and the
 *  row's own `grade` — the artist folder's verdict, which is what the dot
 *  beside the name reads. */
const artistPayload = (row) => {
  const hasImage = !!row.has_image;
  return {
    path: row.path,
    name: row.name,
    display_name: row.display_name || row.name,
    albums: row.albums,
    aggregate: row.aggregate,
    artwork: {
      image: hasImage,
      image_file: hasImage ? "artist.jpg" : null,
      image_url: hasImage ? `/api/artist/image?artist=${encodeURIComponent(row.path)}` : null,
      description: row.grade?.artwork?.description ? "A stored artist description." : null,
      description_source: null,
      description_url: null,
      provenance: {},
      auto_image: true,
      auto_description: true,
    },
    grade: row.grade,
  };
};

/** What Home's artist shelf draws, per row (`recommendations._top_artists`):
 *  the card's own fields plus the artist's grade, which the shelf passes
 *  through from the library row it was built from. */
const homeArtist = (row) => ({
  path: row.path,
  artist: row.display_name || row.name,
  album_count: row.albums.length,
  track_count: row.aggregate?.track_count ?? 0,
  grade_pct: row.aggregate?.grade_pct ?? null,
  cover_path: row.albums[0]?.path || "",
  cover: row.albums[0]?.cover_file ?? null,
  has_image: !!row.has_image,
  grade: row.grade,
});
/* Home's payload, kept to what the page reads: the shelves the fixture has no
 * rows for answer empty (each shelf draws nothing), and the artist shelf
 * carries the same graded rows the Library lists. */
const HOME = {
  stats: {
    artists: served.artists.length,
    albums: served.artists.reduce((n, a) => n + a.albums.length, 0),
    tracks: served.artists.reduce(
      (n, a) => n + a.albums.reduce((m, al) => m + (al.tracks?.length || 0), 0), 0),
    playlists: 0,
    grade_pct: null,
  },
  recent: [], top_rated: [], rated: [], favorites: [], discover: [],
  podcasts: [], pending: [], wanted: [], needs_attention: [],
  top_artists: served.artists.slice(0, 3).map(homeArtist),
  grade_warning: null,
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
   * carries — one `grade` object drives both, which is exactly the agreement
   * the dot cases below are about. `/api/artist/image` answers real PNG bytes
   * so the hero's cover-derived backdrop is a layer with pixels in it (the
   * fixture's artists carry `has_image`, so the page asks for it). */
  if (url.startsWith("/api/artist/image")) {
    res.writeHead(200, { "content-type": "image/png" });
    return res.end(PNG);
  }
  if (url.startsWith("/api/artist?")) {
    const want = new URL(url, "http://127.0.0.1").searchParams.get("path") || "";
    const row = served.artists.find((a) => a.path === want);
    if (!row) {
      res.writeHead(404, { "content-type": "application/json" });
      return res.end("{}");
    }
    return json(artistPayload(row));
  }
  if (url.startsWith("/api/home")) return json(HOME);
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
  /* The Favorites page's own store: the two graded artists are hearted, so its
   * artist table lists them (it joins these paths with the library payload the
   * page also holds, and draws the shared name render). */
  if (url.startsWith("/api/favorites")) {
    return json({
      albums: [],
      artists: [served.artists[0].path, served.artists[1].path],
      playlists: [],
    });
  }
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
  // ------------------------- 7. the artist's own verdict: one dot
  // The owner's ask: an artist's health is ONE green dot beside its name, not
  // two chips spelling the checks out — and the dot is the SAME bit on the
  // artist page and in every list that names an artist, so a list and the page
  // it opens can never disagree. The fixture grades two artists: Artist Alpha
  // passes its own checks (artist image + description, 2/2) and Artist Beta
  // fails one (no description, 1/2). The stub answers the artist page, the
  // library and Home out of those same two `grade` objects, which is what makes
  // "the list and the page agree" a property of what is DRAWN and not of two
  // fixtures that happen to match.
  const alphaRow = served.artists.find((a) => a.name === ARTIST_ALPHA);
  const betaRow = served.artists.find((a) => a.name === ARTIST_BETA);
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  check("fixture: one artist passes its own checks and one fails",
        alphaRow?.grade?.pass === true && betaRow?.grade?.pass === false,
        `alpha=${alphaRow?.grade?.pass} beta=${betaRow?.grade?.pass}`);

  /** One artist's hero, read off the live DOM: the name and its dot, the
   *  counts line, whether the two removed chips are still drawn anywhere in
   *  it, and the cover-derived backdrop's own geometry and computed style. */
  const heroOf = async (row) => {
    await page.goto(`${base}/artist/${encodeURIComponent(row.path)}`);
    await page.waitForSelector(".hero-flat h1", { timeout: 30000 });
    await page.waitForTimeout(250);
    return page.evaluate(() => {
      const box = (el) => {
        const r = el.getBoundingClientRect();
        return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height };
      };
      const hero = document.querySelector(".hero-flat");
      const layer = hero.querySelector(".hero-ink");
      const style = layer ? getComputedStyle(layer) : null;
      const texts = [...hero.querySelectorAll("span")].map((s) => (s.textContent || "").trim());
      const dot = hero.querySelector("h1 .artist-dot");
      return {
        name: (hero.querySelector("h1")?.textContent || "").trim(),
        dot: !!dot,
        dotLabel: dot?.getAttribute("aria-label") || "",
        counts: texts.find((t) => /^\d+ albums? · \d+ tracks?$/.test(t)) || "",
        // The two pass chips this hero used to spell its verdicts out with:
        // the album rollup's "albums", and "artist artwork 2/2".
        passChips: texts.filter((t) => t === "albums" || /^artist artwork/.test(t)),
        failures: texts.filter((t) => t === "Artist description"),
        heroBox: box(hero),
        layerBox: layer ? box(layer) : null,
        layerOpacity: style?.opacity ?? null,
        layerMask: style ? style.maskImage || style.webkitMaskImage : null,
        // The legibility rule this fade is not allowed to change: the `bg`
        // gradient still painted over the wash, between it and the text.
        overlay: !!hero.querySelector("div.absolute.inset-0.bg-gradient-to-t"),
      };
    });
  };

  const alphaHero = await heroOf(alphaRow);
  check(`the hero draws the passing artist's dot beside the name (${alphaHero.name})`,
        alphaHero.name === ARTIST_ALPHA && alphaHero.dot === true
          && alphaHero.dotLabel.includes("Artist checks pass"),
        JSON.stringify({ name: alphaHero.name, dot: alphaHero.dot, label: alphaHero.dotLabel }));
  check("and no longer spells the checks out in chips (`albums`, `artist artwork`)",
        alphaHero.passChips.length === 0, JSON.stringify(alphaHero.passChips));
  check(`while the informational counts stay (${alphaHero.counts})`,
        alphaHero.counts === `${plural(alphaRow.aggregate.album_count, "album")} · `
          + plural(alphaRow.aggregate.track_count, "track"),
        `"${alphaHero.counts}"`);

  // The backdrop: the layer is scaled PAST the hero's box and masked, so the
  // wash fades instead of being sliced off at the hero's `overflow-hidden`
  // edge. Coverage is geometry — the layer's own box against the hero's.
  const covers = (l, h) => !!l
    && l.left <= h.left + 1 && l.top <= h.top + 1
    && l.right >= h.right - 1 && l.bottom >= h.bottom - 1;
  // The numbers, the way this check logs the shelf's own geometry: what the
  // backdrop measured, so a passing run says how much bigger the layer is.
  console.log(`\n[library-az] artist hero: backdrop layer `
    + `${Math.round(alphaHero.layerBox?.width ?? 0)}x${Math.round(alphaHero.layerBox?.height ?? 0)} px `
    + `over the hero's ${Math.round(alphaHero.heroBox.width)}x${Math.round(alphaHero.heroBox.height)} px, `
    + `opacity ${alphaHero.layerOpacity}, mask "${String(alphaHero.layerMask).replace(/\s+/g, " ").slice(0, 120)}", `
    + `overlay ${alphaHero.overlay}`);
  check(`the backdrop's blur layer covers the hero box `
        + `(layer ${Math.round(alphaHero.layerBox?.width ?? 0)}x${Math.round(alphaHero.layerBox?.height ?? 0)} `
        + `over hero ${Math.round(alphaHero.heroBox.width)}x${Math.round(alphaHero.heroBox.height)})`,
        covers(alphaHero.layerBox, alphaHero.heroBox), JSON.stringify(alphaHero.layerBox));
  check("…and it is extended past that box, so the fade lands inside the hero's clip",
        !!alphaHero.layerBox
          && alphaHero.layerBox.width > alphaHero.heroBox.width + 8
          && alphaHero.layerBox.height > alphaHero.heroBox.height + 8,
        JSON.stringify({ layer: alphaHero.layerBox, hero: alphaHero.heroBox }));
  check("…and the blur is masked by a gradient ending transparent, not a hard edge",
        typeof alphaHero.layerMask === "string" && /gradient/.test(alphaHero.layerMask)
          && /rgba\(0, 0, 0, 0\)|transparent/.test(alphaHero.layerMask),
        String(alphaHero.layerMask));
  /* The fade itself, as geometry rather than by eye: the mask's transparent
   * stop, resolved against the layer the browser actually laid out, must
   * already be reached along the hero's WHOLE border. Positive alpha anywhere
   * on that border is the hard edge the owner reported — `overflow-hidden`
   * slicing a wash that was still visible — and coverage alone would not catch
   * it: the old `scale-110` layer covered the box too. The border is sampled
   * (corners, edge midpoints and between) because the binding point is not a
   * corner: it is wherever the ellipse's own radius runs out first. */
  const fadeAtBorder = (mask, layer, hero) => {
    const m = /radial-gradient\(\s*([\d.]+)%\s+([\d.]+)%\s+at\s+([\d.]+)%\s+([\d.]+)%\s*,([\s\S]*)\)/
      .exec(mask || "");
    const stop = /(?:rgba\(0, 0, 0, 0\)|transparent)\s+([\d.]+)%/.exec(m?.[5] || "");
    if (!m || !stop || !layer || !hero) return null;
    const rx = (Number(m[1]) / 100) * layer.width;
    const ry = (Number(m[2]) / 100) * layer.height;
    const cx = layer.left + (Number(m[3]) / 100) * layer.width;
    const cy = layer.top + (Number(m[4]) / 100) * layer.height;
    // 32 points around the hero's own border, in the mask's own units.
    const points = [];
    for (const [edge, n] of [["top", hero.width], ["bottom", hero.width],
                             ["left", hero.height], ["right", hero.height]]) {
      for (let i = 0; i <= 8; i += 1) {
        const t = i / 8;
        const x = edge === "left" ? hero.left
          : edge === "right" ? hero.right : hero.left + t * hero.width;
        const y = edge === "top" ? hero.top
          : edge === "bottom" ? hero.bottom : hero.top + t * hero.height;
        points.push(Math.sqrt(((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2));
      }
    }
    return { end: Number(stop[1]) / 100, min: Math.min(...points) };
  };
  const fade = fadeAtBorder(alphaHero.layerMask, alphaHero.layerBox, alphaHero.heroBox);
  check(`…and that fade is already complete along the hero's whole border `
        + `(transparent by ${Math.round((fade?.end ?? 0) * 100)}% of the mask's radius; `
        + `the border's own nearest point is at ${Math.round((fade?.min ?? 0) * 100)}%)`,
        !!fade && fade.min >= fade.end, JSON.stringify(fade));
  check("…with the legibility rules untouched: the wash keeps its opacity and the "
        + "bg gradient is still painted over it",
        alphaHero.layerOpacity === "0.25" && alphaHero.overlay === true,
        `opacity=${alphaHero.layerOpacity} overlay=${alphaHero.overlay}`);

  const betaHero = await heroOf(betaRow);
  check(`an artist that fails its own checks carries no dot (${betaHero.name})`,
        betaHero.name === ARTIST_BETA && betaHero.dot === false,
        JSON.stringify({ name: betaHero.name, dot: betaHero.dot, label: betaHero.dotLabel }));
  check("…and the failing check is still named in the hero, in words",
        betaHero.failures.length === 1, JSON.stringify(betaHero.failures));

  // The Library's Artists view: the same dot, on the rows whose `grade` says
  // the same thing — and nothing at all on an artist the payload never graded.
  await page.goto(`${base}/library`);
  await page.waitForSelector("text=Artists", { timeout: 30000 });
  await viewTab("Artists").click();
  await page.waitForTimeout(250);
  const artistRows = await page.evaluate(([alpha, beta]) => {
    const rows = [...document.querySelectorAll("main table tbody tr")];
    const read = (name) => {
      const row = rows.find((r) => (r.querySelector("a")?.textContent || "").trim() === name);
      return row
        ? { found: true, dot: !!row.querySelector("a .artist-dot"), name: (row.querySelector("a")?.textContent || "").trim() }
        : { found: false, dot: false, name: "" };
    };
    return {
      alpha: read(alpha),
      beta: read(beta),
      dots: rows.filter((r) => r.querySelector("a .artist-dot")).length,
    };
  }, [ARTIST_ALPHA, ARTIST_BETA]);
  check("the Library's Artists view draws that dot beside the name that passes",
        artistRows.alpha.found && artistRows.alpha.dot === true, JSON.stringify(artistRows));
  check("…and none beside the name that fails",
        artistRows.beta.found && artistRows.beta.dot === false, JSON.stringify(artistRows));
  check("…and a row the payload never graded grew none either",
        artistRows.dots === 1, `${artistRows.dots} dots`);

  // Home's artist shelf — the third place a name is listed, and the one whose
  // rows come off the SAME library row: one dot, two surfaces.
  await page.goto(`${base}/`);
  await page.waitForSelector(`a[title="${ARTIST_ALPHA}"]`, { timeout: 30000 });
  await page.waitForTimeout(250);
  const shelfDots = await page.evaluate(([alpha, beta]) => {
    const read = (name) => {
      const card = document.querySelector(`a[title="${CSS.escape(name)}"]`);
      return card
        ? {
            found: true,
            dot: !!card.querySelector(".artist-dot"),
            // What the row NAMES, read the way tools/check_home_artists.cjs
            // reads a shelf caption (`span.text-sm` — the shared render's own
            // name span): the display name, never the folder's `[mbid]`.
            caption: card.querySelector("span.text-sm")?.textContent?.trim() || "",
          }
        : { found: false, dot: false, caption: "" };
    };
    return { alpha: read(alpha), beta: read(beta) };
  }, [ARTIST_ALPHA, ARTIST_BETA]);
  check("Home's artist shelf draws it too, on the same artist",
        shelfDots.alpha.found && shelfDots.alpha.dot === true, JSON.stringify(shelfDots));
  check("…and none on the artist that fails, so the shelf and the page agree",
        shelfDots.beta.found && shelfDots.beta.dot === false, JSON.stringify(shelfDots));
  check("…while the card still names the artist (the caption the shelf's own check reads)",
        shelfDots.alpha.caption === ARTIST_ALPHA && shelfDots.beta.caption === ARTIST_BETA,
        JSON.stringify([shelfDots.alpha.caption, shelfDots.beta.caption]));

  // Favorites' artist table is the fourth surface listing an artist's name,
  // off the same library rows: same dot, same rule.
  await page.goto(`${base}/favorites/artists`);
  await page.waitForSelector("text=Artist Alpha", { timeout: 30000 });
  await page.waitForTimeout(250);
  const favDots = await page.evaluate(([alpha, beta]) => {
    const rows = [...document.querySelectorAll("main table tbody tr")];
    const read = (name) => {
      const row = rows.find((r) => (r.querySelector("a")?.textContent || "").trim() === name);
      return row
        ? { found: true, dot: !!row.querySelector("a .artist-dot") }
        : { found: false, dot: false };
    };
    return { alpha: read(alpha), beta: read(beta) };
  }, [ARTIST_ALPHA, ARTIST_BETA]);
  check("Favorites' artist table draws it on the artist that passes…",
        favDots.alpha.found && favDots.alpha.dot === true, JSON.stringify(favDots));
  check("…and none on the one that fails",
        favDots.beta.found && favDots.beta.dot === false, JSON.stringify(favDots));

  /* The sidebar's OWN entry, pressed while its page is already open, is a
   * RESET of that page's search rather than a navigation (App.tsx's
   * `clearSearchOnRePress`): it empties the app-wide query box — the Library's
   * own box edits the same store value — tells the page to clear the rest of
   * its toolbar, and does NOT navigate. Pressed from ANOTHER page it is an
   * ordinary first navigation and must leave the search alone. */
  const libraryRows = () => page.evaluate(() =>
    document.querySelectorAll("main table tbody tr, main a[href^='/album/']").length);
  const searchBox = page.locator('main input[title^="Plain words match"]').first();
  await page.goto(`${base}/library`);
  await page.waitForSelector('main input[title^="Plain words match"]', { timeout: 30000 });
  await page.waitForTimeout(600);
  const allRows = await libraryRows();
  await searchBox.fill("zzzz-matches-nothing");
  await page.waitForTimeout(600);
  const narrowedRows = await libraryRows();
  await page.getByRole("link", { name: "Library", exact: true }).first().click();
  await page.waitForTimeout(600);
  const pressed = { value: await searchBox.inputValue(), rows: await libraryRows() };
  check(`re-pressing the sidebar's own entry clears the Library's search `
        + `(${narrowedRows} of ${allRows} rows while typed, ${pressed.rows} rows after, box "${pressed.value}")`,
        narrowedRows < allRows && pressed.value === "" && pressed.rows === allRows,
        JSON.stringify(pressed));

  await page.goto(`${base}/genres`);
  await page.waitForTimeout(400);
  await page.getByRole("link", { name: "Library", exact: true }).first().click();
  await page.waitForTimeout(700);
  check(`and a first press from another page still just navigates (${new URL(page.url()).pathname})`,
        new URL(page.url()).pathname === "/library" && (await searchBox.inputValue()) === "",
        page.url());

  /* ---- 8. a download's badge names where its files came from ------------
   * A pressing's badge is its medium and its release countries ("CD · US, CA").
   * A DIGITAL release has no pressing, so the fact that takes that place is
   * where the files came from — the album's own `source_summary`, built by the
   * server from the tracks' SOURCE tags ("Bandcamp", "Qobuz", the reader's own
   * shop word, "Soulseek"). The app's own default source is the word "Digital"
   * (mlo.paths.DEFAULT_DIGITAL_SOURCE): it repeats the medium and is dropped
   * rather than printed twice. The card and the album page print the same
   * words in the same order, because they are built by the same helper. */
  const sourced = albumAt(0);           // the album page's own release
  sourced.source_summary = "Bandcamp";
  sourced.meta = { ...(sourced.meta || {}), RELEASECOUNTRY: "US; CA" };
  album.source_summary = "Bandcamp";    // the page's payload for the same album
  album.meta = { ...(album.meta || {}), RELEASECOUNTRY: "US; CA" };
  const unsourced = albumAt(3);         // a download with no shop of its own
  unsourced.source_summary = "Digital";
  const physical = albumAt(1);          // the rip: its badge must not move
  physical.source_summary = null;

  /** One card's badges: the medium (+source) chip and, beside it, the release
   *  countries — read off the card that links to `title`, the way a reader
   *  finds it. */
  const cardBadge = (title) => page.evaluate((name) => {
    const link = [...document.querySelectorAll("a")].find((a) => (a.getAttribute("title") || "") === name);
    const card = link?.closest(".group");
    const media = card?.querySelector('[title^="Media: "]');
    const country = card?.querySelector('[title^="Released in "]');
    return media
      ? { media: (media.textContent || "").trim(), title: media.getAttribute("title"),
          country: (country?.textContent || "").trim() }
      : null;
  }, title);

  await page.goto(`${base}/library`);
  await page.waitForSelector("input[aria-label='Filter the list by name']", { timeout: 30000 });
  await page.waitForTimeout(700);
  const sourcedCard = await cardBadge(titleOf(sourced));
  check(`a digital release that states a source wears it beside the medium `
        + `("${sourcedCard?.media || "no media chip"}")`,
        sourcedCard?.media === "Digital · Bandcamp", JSON.stringify(sourcedCard));
  check(`…and the tooltip still names the release's own medium `
        + `("${sourcedCard?.title || "no tooltip"}")`,
        sourcedCard?.title === "Media: Digital Media · Source: Bandcamp", String(sourcedCard?.title));
  const unsourcedCard = await cardBadge(titleOf(unsourced));
  check(`a digital release that states none keeps the medium alone `
        + `("${unsourcedCard?.media || "no media chip"}")`,
        unsourcedCard?.media === "Digital", JSON.stringify(unsourcedCard));
  const physicalCard = await cardBadge(titleOf(physical));
  check(`a disc release's badge is unchanged ("${physicalCard?.media || "no media chip"}")`,
        physicalCard?.media === "CD", JSON.stringify(physicalCard));
  await page.goto(`${base}/album/${encodeURIComponent(albumPath)}`);
  await page.waitForSelector("h1", { timeout: 30000 });
  await page.waitForTimeout(600);
  const WANT_CHIP = "Digital · Bandcamp · US, CA";
  const albumChip = await page.evaluate((want) => {
    const seen = [...document.querySelectorAll("main span")].map((el) => (el.textContent || "").trim());
    // The chip itself, not a card's badge on the page's own shelf.
    return { hit: seen.includes(want) || null, digital: seen.filter((t) => t.startsWith("Digital")) };
  }, WANT_CHIP);
  check(`…and the album page names the same release the same way, countries and all `
        + `("${albumChip.digital.join(" / ") || "no chip"}")`,
        albumChip.hit === true, JSON.stringify(albumChip.digital));
  console.log(`\n[digital badges] card "${sourcedCard?.media}" (tooltip "${sourcedCard?.title}") | `
    + `no source of its own "${unsourcedCard?.media}" | disc "${physicalCard?.media}" (countries "${physicalCard?.country}") | `
    + `album page "${albumChip.digital.join(" / ")}"`);

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
  const sane = await widthCase({ "mlo-colw-tracks": JSON.stringify({ title: 300 }) }, "Tracks", "Title");
  check(`a sane stored width is still honoured — the reader's own 300 px title column `
        + `(${sane?.widths?.Title ?? 0} px)`,
        (sane?.widths?.Title ?? 0) === 300, JSON.stringify(sane?.widths));

  /* ---- 10. the Artists tab must not scroll while its columns fit --------
   * The owner's screenshot: a short artist list with a scrollbar along the
   * bottom. `table-layout: fixed` makes the sum of the columns' own widths the
   * table's floor, so four stored drag widths — every one of them well inside
   * the handle's own 40-900 range — drew a 1420 px table in a 1200 px wrapper
   * although the artists columns' own floors sum to 596 px. useFittedWidths
   * brings the stored map inside what the table can show (the columns' floors
   * plus the box's free room), which keeps the reader's proportions and leaves
   * the sideways scroll to the case it is the design for: a table whose own
   * floors genuinely exceed its box (the Albums and Tracks views at 1440,
   * measured below). */
  await viewTab("Artists").click();
  await page.waitForTimeout(400);
  const artistsClean = await tableView("Artist");
  const overWide = await widthCase(
    { "mlo-colw-artists": JSON.stringify({ albums: 300, tracks: 300, checks: 300, grade: 300 }) },
    "Artists", "Artist");
  check(`Artists: the columns' own floors fit the box, so no stored map may scroll it `
        + `(${overWide?.wrapScroll ?? 0} vs ${overWide?.wrapClient ?? 0} px with four stored 300 px columns; `
        + `clean ${artistsClean?.wrapScroll ?? 0} vs ${artistsClean?.wrapClient ?? 0} px)`,
        !!overWide && overWide.wrapScroll <= overWide.wrapClient + 1
          && (overWide.widths.Grade ?? 0) > 0 && (overWide.widths.Checks ?? 0) > 0,
        JSON.stringify(overWide?.widths));
  const albumsOver = await widthCase({ "mlo-colw-albums": null, "mlo-colw-tracks": null }, "Albums", "Album");
  const tracksOver = await widthCase({ "mlo-colw-tracks": null }, "Tracks", "Title");
  console.log(`\n[stored widths] hostile Tracks maps draw `
    + hostileTracks.map(({ what, t }) => `${t?.headers ?? 0}h/${t?.cells ?? 0}c/cover ${t?.cover ?? 0}px (${what})`).join(", ")
    + ` | hostile Albums map ${zeroAlbums?.headers ?? 0}h/${zeroAlbums?.cells ?? 0}c/cover ${zeroAlbums?.cover ?? 0}px`
    + ` | sane 300 px title -> ${sane?.widths?.Title ?? 0} px (its own floor: ${tracksOver?.widths?.Title ?? 0} px)`);
  console.log(`[library-az tables @1440] Artists ${artistsClean?.wrapScroll}/${artistsClean?.wrapClient} px `
    + `(with four stored 300 px columns: ${overWide?.wrapScroll}/${overWide?.wrapClient} px), `
    + `Albums ${albumsOver?.wrapScroll}/${albumsOver?.wrapClient} px, Tracks ${tracksOver?.wrapScroll}/${tracksOver?.wrapClient} px`);
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
console.log("\nPASS — the Library's alphabet filter, and the album page's wrapped shelf");
