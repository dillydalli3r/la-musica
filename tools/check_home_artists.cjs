#!/usr/bin/env node
/* Home's "Most played artists" lane and its "Rated albums" shelf, measured in
 * a real browser.
 *
 * What it proves, in order:
 *   1. an artist folder named the way the naming script writes it
 *      ("Artist Alpha [<mbid>]") reaches the page as "Artist Alpha" — the
 *      on-disk folder name with its MusicBrainz id is never drawn;
 *   2. plays recorded through the app's own route (`POST /api/plays`) put that
 *      artist on "Most played artists", and the card's title carries the play
 *      count the payload reports;
 *   3. the "Rated albums" shelf lists `rated_albums` best first, each card
 *      carrying its own stars.
 *
 * The fixture is this script's own: it renames two folders of the scratch
 * library to carry an [mbid] suffix. Point LIB at a throwaway library
 * (`python tools/make_test_library.py` prints one), never a real one: this
 * script records plays and ratings in it.
 *
 * Needs a live backend serving the built app (`web/dist`), against LIB:
 *   npm --prefix web run build
 *   LIB=<scratch folder> node tools/check_home_artists.cjs
 *   LIB=<scratch folder> MLO_MUSIC_FOLDER=<scratch folder> \
 *     python -m uvicorn server.main:app --host 127.0.0.1 --port 8011
 *
 * Playwright is required (same resolution as tools/shot.cjs): set PLAYWRIGHT
 * to a module path, or install it. Exit 2 when it is missing or LIB is not a
 * library. Screenshots land in .pi/shots-home-artists/. */

let chromium;
try {
  ({ chromium } = require(process.env.PLAYWRIGHT ||
    "C:/Users/dillydallier/AppData/Roaming/npm/node_modules/omniroute/node_modules/playwright"));
} catch {
  console.error("[home-artists] Playwright not found — `npm i -D playwright`, " +
    "or point PLAYWRIGHT at an installed module.");
  process.exit(2);
}

const fs = require("fs");
const path = require("path");

const BASE = process.argv[2] || process.env.BASE || "http://127.0.0.1:8011";
const LIB = process.env.LIB || process.env.MLO_MUSIC_FOLDER || "";
const SHOTS = path.join(".pi", "shots-home-artists");

const results = [];
const check = (name, pass, detail) => results.push({ name, pass: !!pass, detail: String(detail) });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// The two name sources the shelves have to draw from, and the folder names
// that make the [mbid] suffix something the page could wrongly show.
const MBID_ALPHA = "a74b1b7f-71a5-4011-9441-d0b5e4122711";
const MBID_BETA = "a16371b9-4f1c-4b0d-9c53-8f2b1b4c9d10";
const ALPHA = `Artist Alpha [${MBID_ALPHA}]`;
const BETA = `Artist Beta [${MBID_BETA}]`;

function prepareFixture() {
  const rename = (from, to) => {
    const a = path.join(LIB, from);
    const b = path.join(LIB, to);
    if (!fs.existsSync(b) && fs.existsSync(a)) fs.renameSync(a, b);
  };
  rename("Artist Alpha", ALPHA);
  rename("Artist Beta", BETA);
}

const api = async (route, init) => {
  const r = await fetch(BASE + route, init);
  const text = await r.text();
  let body = text;
  try { body = JSON.parse(text); } catch { /* a non-JSON answer is the detail */ }
  return { status: r.status, body };
};
const json = (method) => async (route, payload) =>
  api(route, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
const putJSON = json("PUT");
const postJSON = json("POST");

/** One shelf's rows, read from the live DOM: the section whose heading is
 *  `title` — its artist cards (the link's own title, which carries the play
 *  count) and its album cards (title, the stars' accessible name, href). */
const readShelf = (page, title) => page.evaluate((want) => {
  const sec = [...document.querySelectorAll("section")]
    .find((s) => s.querySelector("h2")?.textContent?.trim() === want);
  if (!sec) return null;
  return {
    artists: [...sec.querySelectorAll("a")].map((a) => ({
      title: a.getAttribute("title") || "",
      // The name is drawn by the shared artist-name render
      // (components/ArtistName): the NAME span is `min-w-0` + the caller's
      // `truncate`, holding the name, with the artist's own verdict mark beside
      // it (no text of its own).
      caption: a.querySelector("span.min-w-0")?.textContent?.trim() || "",
    })),
    cards: [...sec.querySelectorAll("div.group")].map((c) => ({
      title: c.querySelector("a.text-sm, span.text-sm")?.textContent?.trim() || "",
      stars: c.querySelector('[role="img"]')?.getAttribute("aria-label") || null,
      href: c.querySelector("a")?.getAttribute("href") || null,
    })),
  };
}, title);

/** Wait for a shelf to render, then read it: the page's own fetch is async, so
 *  the first sample can land on a page still holding its spinner. */
async function shelf(page, title, timeout = 45000) {
  const until = Date.now() + timeout;
  for (;;) {
    const got = await readShelf(page, title);
    if (got && (got.artists.length || got.cards.length)) return got;
    if (Date.now() > until) return got ?? null;
    await sleep(400);
  }
}

(async () => {
  if (!LIB || !fs.existsSync(LIB)) {
    console.error(`[home-artists] LIB is not a folder: ${LIB || "(unset)"} — ` +
      "point it at a throwaway library (`python tools/make_test_library.py` prints one).");
    process.exit(2);
  }
  fs.mkdirSync(SHOTS, { recursive: true });
  prepareFixture();

  // The scratch scope is freshly created, so its wizard is still open and "/"
  // would render the setup page instead of Home.
  await postJSON("/api/config", { first_run_done: true });

  // The rename above is a FILE change: re-walk the folder so the library rows
  // (and Home, built from them) are the folders as they now sit on disk.
  await api("/api/library?refresh=1");
  const lib = (await api("/api/library")).body;
  const byArtist = (folder) => (lib?.artists || []).find((ar) => path.basename(ar.path) === folder);
  const alpha = byArtist(ALPHA);
  const beta = byArtist(BETA);
  check("fixture: renamed artist folders are in the library",
    !!alpha && !!beta,
    `alpha=${!!alpha} beta=${!!beta} of ${(lib?.artists || []).length} artists`);

  // Plays are what puts an artist on the lane. Seed a few of Alpha's own
  // tracks through the same route the player bar calls, then ask the payload
  // to rebuild from that history.
  const alphaTracks = (alpha?.albums || [])
    .flatMap((alb) => alb.tracks || [])
    .map((tr) => tr.path)
    .filter(Boolean)
    .slice(0, 3);
  const playStatuses = [];
  for (const track of alphaTracks) {
    playStatuses.push((await postJSON("/api/plays", { path: track })).status);
  }
  const seededPlays = alphaTracks.length;
  check("store: POST /api/plays recorded the artist's plays",
    seededPlays > 0 && playStatuses.every((s) => s === 200),
    `${seededPlays} track(s): ${playStatuses.join(", ")}`);

  // Two ratings the shelf has to order between: 9 half-stars over 6.
  const rate = [];
  for (const [artist, half] of [[alpha, 9], [beta, 6]]) {
    const alb = (artist?.albums || [])[0];
    if (!alb) continue;
    const r = await putJSON("/api/ratings", { path: alb.path, rating: half, scope: "album" });
    rate.push(`${path.basename(alb.path)}=${half} → ${r.status}`);
  }
  check("store: the album ratings were accepted", rate.length === 2 && / → 200/.test(rate.join(" ")), rate.join(", "));

  await api("/api/home?refresh=1");
  const home = (await api("/api/home")).body;
  const played = home?.top_artists || [];
  const rated = home?.rated_albums || [];
  // A scratch port is shared real estate: another agent's server answers on it
  // just as readily, and its payload would be graded as this run's failures.
  // `top_artists[].plays` and `rated_albums` are the fields this check is
  // about, and they say which tree is answering.
  if (!home || !Array.isArray(home.top_artists) || !Array.isArray(home.rated_albums)
      || !played.every((a) => "plays" in a)) {
    console.error(`[home-artists] the server on ${BASE} is not serving this tree's ` +
      "payload (no `top_artists[].plays` / `rated_albums`) — start one against LIB on a free port.");
    process.exit(2);
  }
  console.log("played artists:", JSON.stringify(played.map(
    (a) => ({ artist: a.artist, plays: a.plays }))));
  console.log("rated albums:", JSON.stringify(rated.map(
    (r) => ({ album: r.meta?.ALBUM, artist: r.artist, rating: r.rating }))));

  check("payload: no artist entry carries an [mbid] suffix",
    played.length > 0 && played.every((a) => !/\[[0-9a-f-]{8,}\]/i.test(a.artist)),
    played.map((a) => a.artist).join(" | "));
  const alphaRow = played.find((a) => a.artist === "Artist Alpha");
  check("payload: the seeded artist is on the most-played lane with its plays",
    !!alphaRow && alphaRow.path.endsWith(ALPHA) && alphaRow.plays >= seededPlays,
    JSON.stringify(played.map((a) => [a.artist, a.plays])));
  // The scratch library is shared: it may already rate other albums, so the
  // shelf is not expected to hold only these two. Locate the fixture's own
  // rows and grade those; their order relative to each other is the rule.
  const alphaRated = rated.find((r) => r.artist === "Artist Alpha");
  const betaRated = rated.find((r) => r.artist === "Artist Beta");
  const alphaAt = rated.indexOf(alphaRated);
  const betaAt = rated.indexOf(betaRated);
  check("payload: rated_albums carries the two fixture ratings, best first",
    alphaRated?.rating === 9 && betaRated?.rating === 6
      && alphaAt >= 0 && betaAt >= 0 && alphaAt < betaAt,
    JSON.stringify(rated.map((r) => [r.artist, r.rating])));

  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));

  // The scratch scope boots with its first-run wizard open ("/" renders the
  // setup page, so every shelf below would be measured as missing). Flipped
  // here, immediately before the navigation, and checked: the flag is what
  // decides whether there is a Home page at all.
  const gate = await postJSON("/api/config", { first_run_done: true });
  check("fixture: the scope's first-run wizard is closed", gate.status === 200, `${gate.status}`);

  await page.goto(BASE + "/", { waitUntil: "networkidle", timeout: 60000 });
  if (await page.locator("text=First-run setup").count()) {
    console.error("[home-artists] the app is showing the setup wizard — " +
      "`first_run_done` did not stick, so no shelf can be read.");
    await browser.close();
    process.exit(2);
  }
  const artists = await shelf(page, "Most played artists");
  const albums = await shelf(page, "Rated albums");
  await page.waitForTimeout(1500);
  await page.screenshot({ path: path.join(SHOTS, "home-1440x900.png") });
  await page.screenshot({ path: path.join(SHOTS, "home-full.png"), fullPage: true });

  check("page: the most-played-artists lane rendered",
    (artists?.artists || []).length > 0, JSON.stringify(artists?.artists || []));
  check("page: captions are clean names, not folder names",
    (artists?.artists || []).every((a) => !/\[[0-9a-f-]{8,}\]/i.test(a.caption)),
    (artists?.artists || []).map((a) => a.caption).join(" | "));

  // The card's own title is `"<artist> · <n> play(s)"` — the same count the
  // payload reports.
  const card = (artists?.artists || []).find((a) => /^Artist Alpha · /.test(a.title));
  const cardPlays = Number((card?.title || "").match(/ · ([\d,]+) play\(s\)$/)?.[1]?.replace(/,/g, ""));
  check("page: the seeded artist's card carries its play count",
    !!card && cardPlays === alphaRow?.plays,
    `title=${JSON.stringify(card?.title)} payload plays=${alphaRow?.plays}`);

  // The payload's `rating` is the API's 0-10 integer; StarRating draws half of
  // it ("Album rating: 4.5 of 5"). Find the fixture's own cards — other rated
  // albums may sit between them — and read their stars and their order.
  const cards = albums?.cards || [];
  const alphaCard = cards.find((c) => c.title && c.title === alphaRated?.meta?.ALBUM);
  const betaCard = cards.find((c) => c.title && c.title === betaRated?.meta?.ALBUM);
  const alphaCardAt = cards.indexOf(alphaCard);
  const betaCardAt = cards.indexOf(betaCard);
  check("page: the rated shelf rendered the fixture's stars, Alpha before Beta",
    !!alphaRated && !!betaRated && !!alphaCard && !!betaCard
      // StarRating prints ONE decimal ("3.0 of 5"), so the expected text is
      // the same spelling — a bare "3 of 5" never matches a whole value.
      && alphaCard.stars.includes(`${(alphaRated.rating / 2).toFixed(1)} of 5`)
      && betaCard.stars.includes(`${(betaRated.rating / 2).toFixed(1)} of 5`)
      && alphaCardAt < betaCardAt,
    `alpha=${JSON.stringify(alphaCard)} beta=${JSON.stringify(betaCard)} ` +
      `got=${JSON.stringify(cards)}`);
  check("page: no uncaught page error", errors.length === 0, errors.join(" | "));

  // Frame the two shelves on their own, since a 900px window cannot hold every
  // shelf Home draws.
  for (const [name, title] of [["home-rated-shelf.png", "Rated albums"],
                               ["home-artist-shelf.png", "Most played artists"]]) {
    const heading = page.locator("section h2", { hasText: title }).first();
    if (!(await heading.count())) continue;   // an empty shelf is hidden by design
    await heading.scrollIntoViewIfNeeded({ timeout: 10000 });
    await page.waitForTimeout(700);
    await page.screenshot({ path: path.join(SHOTS, name) });
  }

  await browser.close();

  for (const r of results) console.log(`${r.pass ? "PASS" : "FAIL"}  ${r.name}\n        ${r.detail}`);
  const bad = results.filter((r) => !r.pass);
  console.log(`\nRESULT: ${bad.length ? `${bad.length} FAILED` : "PASS"} (${results.length} checks)`);
  process.exit(bad.length ? 1 : 0);
})();