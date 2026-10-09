#!/usr/bin/env node
/* A DELIBERATE play press must do something, on every surface. The reported
 * bug: while an album's first track was playing, pressing the album's play
 * button again did nothing at all — neither a restart nor anything else.
 *
 * Mechanism: every play surface funnels into the store's `playNow`, which
 * resolves to the track that is already loaded, and the player's load effect
 * skipped a same-path load (right for a reorder or a queue edit, wrong for a
 * press). The fix makes the press explicit — `playNow` bumps the store's
 * `playToken`, the load effect restarts the track when that changes — so this
 * check presses each surface TWICE and asserts what the second press does:
 *
 *   needs a restart: album card play, album page header play, artist play all,
 *                    a track row's own play, a queue row press
 *   must NOT restart: the queue's "clear upcoming" trim, and the transport's
 *                    play-after-pause (that is a resume)
 *
 * Needs a live backend serving the built app (`web/dist`):
 *   npm --prefix web run build
 *   python -m uvicorn server.main:app --host 127.0.0.1 --port 8000
 *   node tools/check_play_press.cjs http://127.0.0.1:8000
 *
 * Exit 2 when Playwright is missing. */

let chromium;
try {
  ({ chromium } = require(process.env.PLAYWRIGHT || "playwright"));
} catch {
  console.error("[press] Playwright not found — `npm i -D playwright`, " +
    "or point PLAYWRIGHT at an installed module.");
  process.exit(2);
}

const BASE = process.argv[2] || process.env.BASE || "http://127.0.0.1:8000";
const results = [];
const check = (name, pass, detail) => results.push({ name, pass: !!pass, detail: String(detail) });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** What the decoder is doing right now. */
const probe = (page) => page.evaluate(() => {
  const els = [...document.querySelectorAll("audio")].map((a) => ({
    file: decodeURIComponent(a.currentSrc || a.src || "").split("?")[0].split("/").pop(),
    t: Number(a.currentTime.toFixed(2)),
    paused: a.paused,
  })).filter((e) => e.file);
  return { els, live: els.find((e) => !e.paused) || null };
});

async function until(fn, ms = 8000, step = 150) {
  const end = Date.now() + ms;
  for (;;) {
    if (await fn()) return true;
    if (Date.now() > end) return false;
    await sleep(step);
  }
}

/** Press `press` on a surface that is already playing, and report whether the
 *  playing decoder went back to the start (restart) or carried on (no-op).
 *
 *  The fixture's tracks are two seconds long, so every observation happens
 *  EARLY in the track: wait any longer and the song's own end (and the queue
 *  running out) is what the probe would be looking at, not the press. */
async function secondPress(page, label, press, { expectRestart }) {
  const before = (await probe(page)).live;
  if (!before || before.t < 0.4) {
    check(`${label}: was playing before the second press`, false, JSON.stringify(before));
    return null;
  }
  await press();
  const moved = await until(async () => {
    const live = (await probe(page)).live;
    if (!live) return false;
    return expectRestart ? live.t < before.t - 0.3 : live.t > before.t + 0.1;
  }, 5000);
  const after = (await probe(page)).live;
  const restarted = !!after && after.t < before.t - 0.25;
  check(
    `${label}: a second press ${expectRestart ? "restarts the track" : "does not restart it"}`,
    expectRestart ? moved && restarted : moved && !restarted,
    `t ${before.t} -> ${after ? after.t : "?"} paused=${after ? after.paused : "?"}`
  );
  return after;
}

(async () => {
  let browser;
  const errs = [];
  try {
    const lib = await (await fetch(`${BASE}/api/library`)).json();
    let album = null;
    for (const a of lib.artists || []) {
      for (const al of a.albums || []) if ((al.tracks || []).length) { album = al.path; break; }
      if (album) break;
    }
    check("the fixture has an album to play", !!album, JSON.stringify(album));

    browser = await chromium.launch({
      executablePath: process.env.CHROME,
      headless: true,
      args: ["--autoplay-policy=no-user-gesture-required"],
    });
    const page = await browser.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: "block" })
      .then((c) => c.newPage());
    page.setDefaultTimeout(20000);
    page.on("pageerror", (e) => errs.push(e.message));
    const rowPlay = (i = 0) => page.locator('tr[title="Click to play"]').nth(i).locator("td").first().click();
    const albumPage = async () => {
      await page.goto(`${BASE}/album/${encodeURIComponent(album)}`, { waitUntil: "domcontentloaded" });
      await page.waitForSelector('tr[title="Click to play"]');
    };
    const settle = async () => {
      // The fixture's tracks are two seconds long: settle a little way in (and
      // never past the point where the song has ended on its own).
      await until(async () => {
        const l = (await probe(page)).live;
        return !!l && l.t > 0.6;
      }, 12000);
    };

    // ---- 1. a track row's own play ----------------------------------------
    await albumPage();
    await rowPlay(0);
    await settle();
    await secondPress(page, "track row play", () => rowPlay(0), { expectRestart: true });

    // ---- 2. the album page's header play button ---------------------------
    await albumPage();
    await page.locator('button[title="Play the album from the top"]').first().click();
    await settle();
    await secondPress(page, "album header play", () => page.locator('button[title="Play the album from the top"]').first().click(), { expectRestart: true });

    // ---- 3. the album card's play button (the library grid) ---------------
    await page.goto(`${BASE}/library`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector('button[title="Play album"]', { timeout: 20000 });
    await page.locator('button[title="Play album"]').first().click();
    await settle();
    await secondPress(page, "album card play", () => page.locator('button[title="Play album"]').first().click(), { expectRestart: true });

    // ---- 4. an artist's "play all" ----------------------------------------
    const artist = (lib.artists || []).find((a) => (a.albums || []).some((al) => (al.tracks || []).length));
    if (artist) {
      await page.goto(`${BASE}/artist/${encodeURIComponent(artist.path)}`, { waitUntil: "domcontentloaded" });
      const ready = await page.waitForSelector('button[title^="Play all"]', { timeout: 15000 }).then(() => true).catch(() => false);
      check("artist play all: the button renders", ready, `artist=${artist.path}`);
      if (ready) {
        const playAll = page.locator('button[title^="Play all"]').first();
        await playAll.click();
        await settle();
        await secondPress(page, "artist play all", () => playAll.click(), { expectRestart: true });
      }
    }

    // ---- 5. a queue row press (the popover) -------------------------------
    await albumPage();
    await rowPlay(0);
    await settle();
    await page.locator('button[title="Queue"]').first().click();
    await page.waitForSelector('button[title="Play this track now"]');
    await secondPress(
      page, "queue row press",
      () => page.locator('button[title="Play this track now"]').first().click(),
      { expectRestart: true });

    // ---- 6. the queue's "clear upcoming" trim must NOT restart ------------
    await page.locator('button[title="Queue"]').first().click();
    const clear = page.locator('button[title="Remove upcoming tracks"]').first();
    if (await clear.count()) {
      const before = (await probe(page)).live;
      await clear.click();
      await sleep(700);
      const after = (await probe(page)).live;
      check("queue trim: keeps the song where it is",
        !!after && !!before && !after.paused && after.t > before.t - 0.5,
        `t ${before?.t} -> ${after?.t} paused=${after?.paused}`);
    } else {
      check("queue trim: the CLEAR control is present", false, "not visible in the queue popover");
    }

    // ---- 7. the transport play after a pause is a RESUME -----------------
    await albumPage();
    await rowPlay(0);
    await settle();
    const playing = (await probe(page)).live;
    const running = !!playing && !playing.paused;
    check("transport: the row press started a track to pause", running, JSON.stringify(playing));
    if (running) {
      const pauseBtn = page.locator('button[title="Pause"]').first();
      const pauseOffered = (await pauseBtn.count()) > 0;
      check("transport: the bar offers a Pause while a track plays", pauseOffered,
        JSON.stringify(await page.evaluate(() => [...document.querySelectorAll("button")]
          .map((b) => b.title).filter((t) => t && /ause|^Play$/.test(t)))));
      if (pauseOffered) {
        // A click can be blocked by whatever the page has over the bar
        // (a toast, a popover that just closed) — the press is what matters.
        await pauseBtn.click({ timeout: 4000 }).catch(() => pauseBtn.dispatchEvent("click"));
        await sleep(500);
        // `probe().live` is the UNPAUSED element, so it is null by definition
        // once the pause worked: read the decoder that holds the track instead.
        const pausedAll = await probe(page);
        const paused = pausedAll.els.find((e) => e.paused) || null;
        check("transport: pause stops the clock", !!paused, JSON.stringify(pausedAll.els));
        // Hold the pause long enough that a RESTART would show a clearly
        // smaller clock than the resume does (two-second fixture tracks).
        await sleep(700);
        const playBtn = page.locator('button[title="Play"]').first();
        await playBtn.click({ timeout: 4000 }).catch(() => playBtn.dispatchEvent("click"));
        await sleep(400);
        const resumed = (await probe(page)).live;
        check("transport: play after pause RESUMES, it does not restart",
          !!resumed && !resumed.paused && resumed.t > (paused?.t ?? 0) + 0.2,
          `paused at ${paused?.t}, after resume ${resumed?.t} (a restart would sit near 0)`);
      }
    }

    check("no uncaught page errors", errs.length === 0, errs.join(" | "));
  } catch (e) {
    check("check script ran to completion", false, String(e).split("\n")[0]);
  } finally {
    if (browser) await browser.close().catch(() => {});
  }
  for (const r of results) console.log(`  ${r.pass ? "ok  " : "FAIL"} ${r.name}${r.pass ? "" : "  :: " + r.detail}`);
  const bad = results.filter((r) => !r.pass).length;
  console.log(`\n${results.length - bad}/${results.length} checks pass`);
  process.exit(bad ? 1 : 0);
})();
