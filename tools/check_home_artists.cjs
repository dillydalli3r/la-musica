#!/usr/bin/env node
/* Home's artist shelf and its ratings shelf, measured in a real browser.
 *
 * What it proves, in order:
 *   1. an artist folder named the way the naming script writes it
 *      ("Artist Alpha [<mbid>]") reaches the page as "Artist Alpha" — the
 *      on-disk folder name with its MusicBrainz id is what the shelf used to
 *      draw (the owner-reported bug);
 *   2. the avatar is the ARTIST picture (`GET /api/artist/image`) when the
 *      folder holds one — seeded here through the upload route — and the
 *      artist's album cover when it does not;
 *   3. a picture that goes away after the payload was built (the URL answers
 *      404) falls back to the cover, so the shelf never shows a broken image;
 *   4. the rated shelf lists the ratings the store holds, best first, each
 *      card carrying its own stars.
 *
 * The fixture is this script's own: it renames two folders of the scratch
 * library to carry an [mbid] suffix and adds a third artist whose only album
 * folder is EMPTY — the one row whose display name has no tag to come from, so
 * the stripped folder name is the only answer. Point LIB at a throwaway
 * library (`python tools/make_test_library.py` prints one), never a real one:
 * this script writes and deletes an artist image in it.
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
// A real photo for the seeded artist picture: an album cover out of the
// fixture would be the same purple square the fallback draws, and the two
// would be indistinguishable in the screenshot.
const PHOTO = process.env.PHOTO || path.join(__dirname, "..", "kitten-headphones.jpg");

const results = [];
const check = (name, pass, detail) => results.push({ name, pass: !!pass, detail: String(detail) });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// The two name sources the shelf has to draw from, and the folder names that
// make each one the only thing that can answer.
const MBID_ALPHA = "a74b1b7f-71a5-4011-9441-d0b5e4122711";
const MBID_BETA = "a16371b9-4f1c-4b0d-9c53-8f2b1b4c9d10";
const MBID_DELTA = "b2c3d4e5-1111-2222-3333-444455556666";
const ALPHA = `Artist Alpha [${MBID_ALPHA}]`;
const BETA = `Artist Beta [${MBID_BETA}]`;
const DELTA = `Artist Delta [${MBID_DELTA}]`;

function prepareFixture() {
  const rename = (from, to) => {
    const a = path.join(LIB, from);
    const b = path.join(LIB, to);
    if (!fs.existsSync(b) && fs.existsSync(a)) fs.renameSync(a, b);
  };
  rename("Artist Alpha", ALPHA);
  rename("Artist Beta", BETA);
  // Delta: an artist folder holding one EMPTY album folder. The library's
  // empty-folder sweep lists it, and no album under it carries an
  // album_artist — the fallback (folder name minus the id) is all that is left.
  // The stray file is what keeps the ARTIST folder itself out of that sweep:
  // a folder with nothing in it at all is reported as an empty album of the
  // music folder above it, which would put the library root's own name on the
  // shelf beside the artists.
  const delta = path.join(LIB, DELTA);
  fs.mkdirSync(path.join(delta, "2023 - Nothing"), { recursive: true });
  fs.writeFileSync(path.join(delta, "notes.txt"), "no tags on the audio here\n");
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

/** Seed the artist's own picture through the app's own route — the manual
 *  upload (`POST /api/artist/image/upload`), so the bytes are the app's to
 *  validate, crop and store and no provider is asked for anything. */
async function seedArtistImage(artistPath) {
  const fd = new FormData();
  fd.append("artist", artistPath);
  fd.append("file", new Blob([fs.readFileSync(PHOTO)], { type: "image/jpeg" }), path.basename(PHOTO));
  return await api("/api/artist/image/upload", { method: "POST", body: fd });
}

/** One shelf's rows, read from the live DOM: the section whose heading is
 *  `title` — the artist links (name, avatar src, whether that avatar is a
 *  circle) and the album cards (title, the stars' own accessible name, href). */
const readShelf = (page, title) => page.evaluate((want) => {
  const sec = [...document.querySelectorAll("section")]
    .find((s) => s.querySelector("h2")?.textContent?.trim() === want);
  if (!sec) return null;
  return {
    artists: [...sec.querySelectorAll("a")].map((a) => {
      const img = a.querySelector("img");
      return {
        name: a.getAttribute("title") || "",
        caption: a.querySelector("div.text-sm")?.textContent?.trim() || "",
        src: img?.getAttribute("src") || null,
        decoded: img ? img.naturalWidth > 0 : null,
        circle: !!a.querySelector("div.rounded-full"),
      };
    }),
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

  const lib = (await api("/api/library")).body;
  const albums = (lib?.artists || []).flatMap((ar) => ar.albums || []);
  const byArtist = (folder) => (lib?.artists || []).find((ar) => path.basename(ar.path) === folder);
  const alpha = byArtist(ALPHA);
  const beta = byArtist(BETA);
  check("fixture: renamed artist folders are in the library",
    !!alpha && !!beta && !!byArtist(DELTA),
    `alpha=${!!alpha} beta=${!!beta} delta=${!!byArtist(DELTA)} of ${(lib?.artists || []).length} artists`);

  // Two ratings the shelf has to order between: 9 half-stars over 6.
  const rate = [];
  for (const [artist, half] of [[alpha, 9], [beta, 6]]) {
    const alb = (artist?.albums || [])[0];
    if (!alb) continue;
    const r = await putJSON("/api/ratings", { path: alb.path, rating: half, scope: "album" });
    rate.push(`${path.basename(alb.path)}=${half} → ${r.status}`);
  }
  check("store: the album ratings were accepted", rate.length === 2 && / → 200/.test(rate.join(" ")), rate.join(", "));

  const seeded = await seedArtistImage(path.join(LIB, ALPHA));
  const imageFile = seeded.body?.file ? path.join(LIB, ALPHA, seeded.body.file) : "";
  check("api: POST /api/artist/image/upload stored the picture",
    seeded.status === 200 && !!imageFile && fs.existsSync(imageFile),
    `${seeded.status} ${JSON.stringify(seeded.body).slice(0, 160)}`);

  // A payload built AFTER the picture exists: `has_image` is what keeps the
  // shelf from asking for a URL that 404s.
  await api("/api/home?refresh=1");
  const home = (await api("/api/home")).body;
  const shelfArtists = home?.top_artists || [];
  // A scratch port is shared real estate: another agent's server answers on it
  // just as readily, and its payload would be graded as this run's failures.
  // The two fields this check is about say which tree is answering.
  if (!home || !("rated" in home) || !shelfArtists.every((a) => "has_image" in a)) {
    console.error(`[home-artists] the server on ${BASE} is not serving this tree's ` +
      "payload (no `rated` shelf / `has_image`) — start one against LIB on a free port.");
    process.exit(2);
  }
  console.log("artist entries:", JSON.stringify(shelfArtists.map(
    (a) => ({ artist: a.artist, albums: a.album_count, has_image: a.has_image }))));
  console.log("rated entries:", JSON.stringify((home?.rated || []).map(
    (r) => ({ album: r.meta?.ALBUM, artist: r.artist, rating: r.rating }))));
  check("payload: no artist entry carries an [mbid] suffix",
    shelfArtists.length > 0 && shelfArtists.every((a) => !/\[[0-9a-f-]{8,}\]/i.test(a.artist)),
    shelfArtists.map((a) => a.artist).join(" | "));
  check("payload: the tag-derived name wins and the folder name is the fallback",
    shelfArtists.some((a) => a.artist === "Artist Alpha" && a.path.endsWith(ALPHA))
      && shelfArtists.some((a) => a.artist === "Artist Delta"),
    shelfArtists.map((a) => `${a.artist}${a.has_image ? " (picture)" : ""}`).join(", "));
  check("payload: has_image is true only for the artist that has one",
    shelfArtists.find((a) => a.path.endsWith(ALPHA))?.has_image === true
      && shelfArtists.find((a) => a.path.endsWith(DELTA))?.has_image === false,
    JSON.stringify(shelfArtists.map((a) => [a.artist, a.has_image])));

  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  const imageCalls = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("response", (r) => {
    if (r.url().includes("/api/artist/image")) imageCalls.push(`${r.status()} ${r.url()}`);
  });

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
  const artists = await shelf(page, "Top artists");
  const rated = await shelf(page, "Your ratings");
  await page.waitForTimeout(1500);
  await page.screenshot({ path: path.join(SHOTS, "home-1440x900.png") });
  await page.screenshot({ path: path.join(SHOTS, "home-full.png"), fullPage: true });

  check("page: the artist shelf rendered",
    (artists?.artists || []).length >= 3, JSON.stringify(artists?.artists || []));
  check("page: captions are clean names, not folder names",
    (artists?.artists || []).every((a) => !/\[[0-9a-f-]{8,}\]/i.test(a.name) && !/\[[0-9a-f-]{8,}\]/i.test(a.caption)),
    (artists?.artists || []).map((a) => a.caption).join(" | "));
  check("page: the artist picture is drawn for the artist that has one",
    (artists?.artists || []).some((a) => a.src?.includes("/api/artist/image?") && a.decoded === true),
    (artists?.artists || []).map((a) => `${a.name} → ${a.src}`).join(" | "));
  check("page: that picture really came from the endpoint (200, one request)",
    imageCalls.some((c) => c.startsWith("200 ")), JSON.stringify(imageCalls));
  check("page: the avatar is still a circle",
    (artists?.artists || []).every((a) => a.circle), `${(artists?.artists || []).length} avatars`);
  // Beta has a cover on disk, Delta has neither a picture nor a cover: the
  // cover stands in for one, the placeholder for the other.
  check("page: a folder with no picture falls back to its album cover",
    (artists?.artists || []).some((a) => a.name === "Artist Beta"
      && a.src?.includes("/api/cover?") && a.decoded === true),
    (artists?.artists || []).map((a) => `${a.name} → ${a.src}`).join(" | "));
  check("page: a folder with neither shows the placeholder, never a broken image",
    (artists?.artists || []).every((a) => a.src === null || a.decoded === true),
    (artists?.artists || []).map((a) => `${a.name} → ${a.src}${a.decoded === false ? " (BROKEN)" : ""}`).join(" | "));

  check("page: the rated shelf rendered with the user's stars, best first",
    (rated?.cards || []).length === 2 && /4\.5/.test(rated?.cards?.[0]?.stars || "")
      && /3/.test(rated?.cards?.[1]?.stars || ""),
    JSON.stringify(rated?.cards || []));

  // Frame the two shelves on their own, since a 900px window cannot hold every
  // shelf Home draws (three albums alone fill a row).
  for (const [name, title] of [["home-rated-shelf.png", "Your ratings"],
                               ["home-artist-shelf.png", "Top artists"]]) {
    const heading = page.locator("section h2", { hasText: title }).first();
    if (!(await heading.count())) continue;   // an empty shelf is hidden by design
    await heading.scrollIntoViewIfNeeded({ timeout: 10000 });
    await page.waitForTimeout(700);
    await page.screenshot({ path: path.join(SHOTS, name) });
  }

  // Both shelves in ONE 1440×900 frame — the acceptance for this change — and
  // measured from their own boxes rather than assumed: Home scrolls its main
  // column, so the rated shelf has to be scrolled to the TOP of that column
  // for the two to share a window.
  await page.evaluate(() => {
    const sec = [...document.querySelectorAll("section")]
      .find((s) => s.querySelector("h2")?.textContent?.trim() === "Your ratings");
    sec?.scrollIntoView({ block: "start" });
  });
  await page.waitForTimeout(700);
  await page.screenshot({ path: path.join(SHOTS, "home-rated-and-artists.png") });
  const framed = await page.evaluate(() => {
    const box = (title) => {
      const sec = [...document.querySelectorAll("section")]
        .find((s) => s.querySelector("h2")?.textContent?.trim() === title);
      if (!sec) return null;
      const r = sec.getBoundingClientRect();
      return { top: Math.round(r.top), bottom: Math.round(r.bottom) };
    };
    return { viewport: window.innerHeight, rated: box("Your ratings"), artists: box("Top artists") };
  });
  check("page: the rated shelf and the artist shelf share one 1440x900 frame",
    !!framed.rated && !!framed.artists
      && framed.rated.top >= 0 && framed.artists.bottom <= framed.viewport,
    JSON.stringify(framed));

  // The picture goes away AFTER the payload that promised it was built: the
  // shelf asks for it, the server answers 404, and the cover has to take over.
  // Read in a FRESH context — with its own empty Cache Storage — so the cover
  // is the one the network serves and not an offline copy a previous load
  // warmed, which is the state a browser reaching this shelf for the first
  // time is in.
  fs.rmSync(imageFile);
  const reloadCalls = [];
  const page2 = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  page2.on("response", (r) => {
    if (r.url().includes("/api/artist/image")) reloadCalls.push(r.status());
  });
  await page2.goto(BASE + "/", { waitUntil: "networkidle", timeout: 60000 });
  const after = await shelf(page2, "Top artists");
  await page2.waitForTimeout(1500);
  const fallbackHeading = page2.locator("section h2", { hasText: "Top artists" }).first();
  if (await fallbackHeading.count()) {
    await fallbackHeading.scrollIntoViewIfNeeded({ timeout: 10000 });
    await page2.waitForTimeout(700);
    await page2.screenshot({ path: path.join(SHOTS, "home-artist-fallback.png") });
  }

  check("fallback: the deleted picture was asked for and answered 404",
    reloadCalls.includes(404), `reload=${JSON.stringify(reloadCalls)}`);
  // What the avatar actually paints, byte for byte: the artist's cover as
  // /api/cover serves it, whether it arrived over the network or out of the
  // offline copy the app warms (those entries are the only artwork in Cache
  // Storage — an artist picture is never warmed). A `blob:` src is therefore
  // the cover, and hashing both ends settles it without trusting the URL.
  const alphaCard = (after?.artists || []).find((a) => a.name === "Artist Alpha");
  const alphaSrc = String(alphaCard?.src || "");
  const firstAlbum = (alpha?.albums || [])[0] || {};
  const coverUrl = `/api/cover?album=${encodeURIComponent(firstAlbum.path || "")}` +
    `&file=${encodeURIComponent(firstAlbum.cover_file || "")}`;
  const pixels = alphaSrc ? await page2.evaluate(async ([src, cover]) => {
    const digest = async (u) => {
      const r = await fetch(u);
      const buf = await r.arrayBuffer();
      const sha = await crypto.subtle.digest("SHA-256", buf);
      return { ok: r.ok, len: buf.byteLength, sha: [...new Uint8Array(sha)].map((b) => b.toString(16).padStart(2, "0")).join("") };
    };
    return { avatar: await digest(src), cover: await digest(cover) };
  }, [alphaSrc, coverUrl]) : null;
  check("fallback: the avatar is no longer the artist picture",
    !!alphaSrc && !alphaSrc.includes("/api/artist/image") && alphaCard?.decoded === true,
    `src=${alphaSrc} decoded=${alphaCard?.decoded}`);
  check("fallback: what it paints is that album's cover, byte for byte",
    !!pixels && pixels.cover.ok && pixels.avatar.ok && pixels.avatar.sha === pixels.cover.sha,
    JSON.stringify(pixels));
  check("fallback: nothing on the shelf is left undecoded",
    (after?.artists || []).every((a) => a.src === null || a.decoded === true),
    (after?.artists || []).map((a) => `${a.name} → ${a.src}`).join(" | "));
  check("page: no uncaught page error", errors.length === 0, errors.join(" | "));

  await browser.close();

  for (const r of results) console.log(`${r.pass ? "PASS" : "FAIL"}  ${r.name}\n        ${r.detail}`);
  const bad = results.filter((r) => !r.pass);
  console.log(`\nRESULT: ${bad.length ? `${bad.length} FAILED` : "PASS"} (${results.length} checks)`);
  process.exit(bad.length ? 1 : 0);
})();
