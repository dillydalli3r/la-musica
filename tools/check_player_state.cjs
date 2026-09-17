#!/usr/bin/env node
/* Player state-machine check: after track changes and lyric-line seeks, the
 * decoder that is actually playing must be the track the UI says is current.
 *
 * Three ways the player used to break, all of which leave the app showing one
 * track while a different element is audible:
 *   1. the gapless swap at a natural track end resolved its element against
 *      the OLD queue index (React state had not re-rendered yet), so it
 *      replayed the finished track from 0 while the UI advanced;
 *   2. the same wrong-element resolution behind a rapid run of row clicks;
 *   3. a lyric line clicked while the PREVIOUS track's lyrics were still on
 *      screen (they are kept, dimmed, during the load), which seeked the new
 *      track to the old track's timestamp.
 *
 * Needs a live backend serving the built app (`web/dist`):
 *   npm --prefix web run build
 *   python -m uvicorn server.main:app --host 127.0.0.1 --port 8010
 *   node tools/check_player_state.cjs http://127.0.0.1:8010
 *
 * Playwright is required (same resolution as tools/shot.cjs): set PLAYWRIGHT
 * to a module path, or install it. Exit 2 when it is missing. */

let chromium;
try {
  ({ chromium } = require(process.env.PLAYWRIGHT || "playwright"));
} catch {
  console.error("[player] Playwright not found — `npm i -D playwright`, " +
    "or point PLAYWRIGHT at an installed module.");
  process.exit(2);
}

const BASE = process.argv[2] || process.env.BASE || "http://127.0.0.1:8010";
const results = [];
const check = (name, pass, detail) => results.push({ name, pass: !!pass, detail: String(detail) });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Both decoders, plus what the UI claims is current. */
const probe = (page) => page.evaluate(() => {
  const els = [...document.querySelectorAll("audio")].map((a) => ({
    file: decodeURIComponent(a.currentSrc || a.src || "").split("/").pop(),
    t: Number(a.currentTime.toFixed(2)),
    paused: a.paused,
    ended: a.ended,
  })).filter((e) => e.file);
  const qp = [...document.querySelectorAll("button")]
    .map((b) => b.title).find((t) => /Queue position/.test(t || "")) || "";
  const m = /Queue position — (\d+) of (\d+)/.exec(qp);
  const bar = document.querySelector("input.seek-fat");
  return {
    els,
    pos: m ? Number(m[1]) : -1,
    len: m ? Number(m[2]) : -1,
    seekMax: bar ? Number(bar.max) : 0,
    playing: [...document.querySelectorAll("button")].some((b) => b.title === "Pause"),
  };
});

/** The invariant: exactly one decoder runs, and it is the UI's current row. */
function assertConsistent(label, p, expectedFile) {
  const live = p.els.filter((e) => !e.paused);
  const okCount = live.length === 1;
  const file = okCount ? live[0].file : live.map((e) => e.file).join("+") || "(none)";
  check(`${label}: exactly one decoder playing`, okCount, `playing=[${file}] all=[${p.els.map((e) => e.file + (e.paused ? "" : "*")).join(", ")}]`);
  check(`${label}: playing decoder is the track the UI shows`,
    okCount && file === expectedFile && p.playing,
    `playing=${file} uiExpects=${expectedFile} transport=${p.playing ? "play" : "pause"} queuePos=${p.pos}/${p.len}`);
  return okCount && file === expectedFile;
}

(async () => {
  let browser;
  const errs = [];
  try {
    browser = await chromium.launch({ executablePath: process.env.CHROME, headless: true });
    const page = await browser.newContext({ viewport: { width: 1440, height: 900 } }).then((c) => c.newPage());
    page.setDefaultTimeout(15000);
    page.on("pageerror", (e) => errs.push(e.message));

    // Discover a real album with enough tracks to switch across.
    await page.goto(BASE, { waitUntil: "domcontentloaded" });
    const album = await page.evaluate(async (base) => {
      const lib = await (await fetch(base + "/api/library")).json();
      for (const a of lib.artists || []) {
        for (const al of a.albums || []) {
          const full = await (await fetch(base + "/api/album?path=" + encodeURIComponent(al.path))).json();
          if ((full.tracks || []).length >= 4) return { path: al.path, tracks: full.tracks.map((t) => t.file), name: al.name };
        }
      }
      return null;
    }, BASE);
    if (!album) throw new Error("no album with 4+ tracks found in the library");

    await page.goto(`${BASE}/album/${encodeURIComponent(album.path)}`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector('tr[title="Click to play"]');
    const rowSel = (i) => `tr[title="Click to play"] >> nth=${i}`;
    const playRow = async (i) => {
      const cell = page.locator(rowSel(i)).locator("td").first();
      await cell.click();
    };
    const seekNearEnd = async () => {
      const box = await page.locator("input.seek-fat").first().boundingBox();  // the volume slider shares the class
      if (!box || !box.width) throw new Error("seek bar missing");
      await page.mouse.click(box.x + box.width * 0.97, box.y + box.height / 2);
    };

    // 1. natural end: the swap must hand over to the NEXT track, not replay.
    await playRow(0);
    await sleep(1500);
    const started = await probe(page);
    check("playback started", started.playing && started.els.some((e) => !e.paused), JSON.stringify(started.els));
    await seekNearEnd();                                  // arms the gapless preload
    // wait out the tail (≤ ~12 s) plus the handover
    let after = null;
    for (let i = 0; i < 30; i++) {
      await sleep(1000);
      after = await probe(page);
      if (after.pos === started.pos + 1 && !after.els.some((e) => e.ended)) break;
    }
    const expectedAfterSwap = album.tracks[after.pos - 1];
    assertConsistent("track ended naturally", after, expectedAfterSwap);

    // 2. rapid row switching while the preload window is open.
    for (const i of [3, 2, 1, 2]) { await playRow(i); await sleep(150); }
    await sleep(2000);
    const burst = await probe(page);
    assertConsistent("rapid row switching", burst, album.tracks[burst.pos - 1]);
    const t0 = (await probe(page)).els.find((e) => !e.paused)?.t ?? -1;
    await sleep(1600);
    const t1 = (await probe(page)).els.find((e) => !e.paused)?.t ?? -1;
    check("playback keeps advancing after the burst", t1 > t0 + 0.5, `t ${t0} -> ${t1}`);

    // 3. lyric-line click while the previous track's lyrics are still on screen.
    const lyricsBtn = page.locator('button[title="Lyrics — open the sidebar"]');
    if (await lyricsBtn.count()) {
      await lyricsBtn.click();
      await sleep(400);
      await page.mouse.move(900, 400);                    // over the pane
      // Playwright takes wheel deltas positionally
      for (let i = 0; i < 6; i++) { await page.mouse.wheel(0, 1500); await sleep(120); }
      const before = await probe(page);
      const row = await page.evaluate(() => {
        const pane = document.querySelector("aside .no-scrollbar");
        const r = pane.getBoundingClientRect();
        const kids = [...pane.children].filter((k) => k.textContent.trim());
        const vis = kids.map((k) => ({ k, b: k.getBoundingClientRect() }))
          .filter((x) => x.b.top > r.top + 8 && x.b.bottom < r.bottom - 8);
        const pick = vis[Math.floor(vis.length / 2)];
        if (!pick) return null;
        const b = pick.k.getBoundingClientRect();
        return { x: Math.round(b.left + b.width / 2), y: Math.round(b.top + b.height / 2) };
      });
      if (row) {
        await playRow(1);                                 // switch, then click a lyric line immediately
        await sleep(60);
        await page.mouse.click(row.x, row.y);
        await sleep(1200);
        const after2 = await probe(page);
        const playing2 = after2.els.find((e) => !e.paused);
        check("lyric click during a switch cannot seek to the old track's line",
          !!playing2 && after2.pos === 2 && playing2.t <= after2.seekMax + 0.5,
          `t=${playing2?.t} of ${after2.seekMax} queuePos=${after2.pos}/${after2.len} before=${before.pos}`);
        assertConsistent("after lyric click during switch", after2, album.tracks[after2.pos - 1]);
      }

      // 4. rapid lyric-line clicks on a settled track: the seek must land on
      // the line last clicked, with the player still consistent afterwards.
      await sleep(800);
      if (!(await page.locator("aside .no-scrollbar").count())) {
        await page.locator('button[title="Lyrics — open the sidebar"]').click();
        await sleep(800);
      }
      let lastClicked = -1;
      for (const frac of [0.3, 0.5, 0.7, 0.55]) {
        const hit = await page.evaluate((f) => {
          const pane = document.querySelector("aside .no-scrollbar");
          if (!pane) return null;
          const r = pane.getBoundingClientRect();
          const kids = [...pane.children].filter((k) => k.textContent.trim());
          const vis = kids.map((k, i) => ({ i, b: k.getBoundingClientRect() }))
            .filter((x) => x.b.top > r.top + 8 && x.b.bottom < r.bottom - 8);
          const pick = vis[Math.floor(vis.length * f)];
          if (!pick) return null;
          return { i: pick.i, x: Math.round(pick.b.left + pick.b.width / 2), y: Math.round(pick.b.top + pick.b.height / 2) };
        }, frac);
        if (!hit) break;
        lastClicked = hit.i;
        await page.mouse.click(hit.x, hit.y);
        await sleep(130);
      }
      await sleep(1500);
      const rapid = await probe(page);
      const activeRow = await page.evaluate(() => {
        const pane = document.querySelector("aside .no-scrollbar");
        return [...pane.children].filter((k) => k.textContent.trim())
          .findIndex((k) => { const d = k.querySelector("div"); return d && /text-white/.test(d.className); });
      });
      assertConsistent("rapid lyric-line clicks", rapid, album.tracks[rapid.pos - 1]);
      check("rapid lyric clicks land on the clicked line",
        lastClicked >= 0 && Math.abs(activeRow - lastClicked) <= 1,
        `clickedRow=${lastClicked} activeRow=${activeRow} t=${rapid.els.find((e) => !e.paused)?.t}`);
    }

    check("no uncaught page errors", errs.length === 0, errs.join(" | "));
  } catch (e) {
    check("check script ran to completion", false, String(e).split("\n")[0]);
  } finally {
    if (browser) await browser.close().catch(() => {});
  }
  for (const r of results) console.log(`${r.pass ? "ok  " : "FAIL"} ${r.name} :: ${r.detail}`);
  const bad = results.filter((r) => !r.pass).length;
  console.log(`\n${results.length - bad}/${results.length} checks pass`);
  process.exit(bad ? 1 : 0);
})();
