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
 * Section 8 is a different kind of check on the same surface: the cover the
 * player bar draws must be fetched once, at the width it draws it, and a
 * repeat play of the same album must not touch the network at all (see
 * server/artcache.py's `cover_thumb` and `api.coverUrl`'s `w`).
 *
 * Section 10 measures infinite playback (`infinite_playback`, shipped OFF):
 * on the library's smallest queue (two rows) the last row has to gain the
 * batch of similar rows the server's local scorer answers for it — appended as
 * ordinary rows, preloaded before its own end, and handed over to gaplessly —
 * while the switch off, or Repeat one armed, appends nothing and ends the queue
 * exactly as it did before (and Shuffle, which is not exempt, still gets the
 * seeds' similar set). It needs a two-track album and at least two similar
 * tracks elsewhere in the library, and says so instead of failing when there
 * is none.
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
    // No service worker: the built app registers one, and its first
    // `controllerchange` reloads the page — which destroys the context this
    // script's very first `evaluate` is running in ("Execution context was
    // destroyed, most likely because of a navigation", 0/1 checks). The rest of
    // the suite's Playwright checks block it for the same reason.
    const page = await browser.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: "block" }).then((c) => c.newPage());
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

    // ---- The player's persisted state, the gapless switch, and Ctrl+Z -------

    /** Press an album row the way the sections above do, but without
     *  Playwright's actionability wait: the lyric section leaves the page
     *  scrolled inside the pane, and the first row then never "receives
     *  events" within the timeout. The row's own click is dispatched instead —
     *  the click plumbing is what sections 1-4 already prove. */
    const pressRow = async (i) => {
      // The album page refetches after every reload in these sections, so the
      // rows may not be there yet: wait for them, or the dispatch below would
      // silently press nothing (which read as "playback paused").
      await page.waitForSelector('tr[title="Click to play"]', { timeout: 10000 }).catch(() => {});
      await page.evaluate((n) => {
        const tr = [...document.querySelectorAll('tr[title="Click to play"]')][n];
        if (tr) tr.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      }, i);
    };
    const playingT = (p) => p.els.find((e) => !e.paused)?.t ?? -1;
    /** A REAL pointer seek on the bar (the only kind the player records as a
     *  user jump — the undo stack is fed by the bar's own pointer/drag, the
     *  ±s keys and the OS's skip actions). */
    const clickBarAt = async (frac) => {
      const box = await page.locator("input.seek-fat").first().boundingBox();
      const x = box.x + Math.max(2, Math.min(box.width - 2, box.width * frac));
      await page.mouse.click(x, box.y + box.height / 2);
    };
    const playsTotal = (page0) => page0.evaluate(async () =>
      (await (await fetch("/api/top?period=all&kind=tracks&limit=1")).json()).plays_total);
    const cfgValue = (page0, key) => page0.evaluate(async (k) =>
      (await (await fetch("/api/config")).json())[k], key);

    // 4b. The bar shows the ORIGINAL release year (ORIGINALDATE, `DATE` as the
    //     fallback) on the album line — a remaster keeps the year the work came
    //     out. Conditional: only a library whose tags carry one can say.
    {
      await pressRow(0);
      await sleep(1800);
      const albumLine = await page.evaluate(() => {
        const a = document.querySelector('a[title="Open the album page"]');
        return a ? a.textContent : null;
      });
      const tags = await page.evaluate(async (base) => {
        const el = document.querySelector("a[title='Open the track page']");
        if (!el) return null;
        const href = el.getAttribute("href") || "";
        const m = /[?&]path=([^&]+)/.exec(href);
        if (!m) return null;
        const r = await fetch(base + "/api/tags?path=" + m[1]);
        return r.ok ? await r.json() : null;
      }, BASE);
      const raw = tags && (tags.ORIGINALDATE || tags.DATE || (tags.tags && (tags.tags.ORIGINALDATE || tags.tags.DATE)));
      const want = /^(\d{4})/.exec(String(raw || ""))?.[1] || "";
      check("the bar's album line carries the original release year",
        !want || String(albumLine || "").includes(want),
        `albumLine=${JSON.stringify(albumLine)} tag=${JSON.stringify(raw)} want=${want || "(no date tag — skipped)"}`);
    }

    // 5. Ctrl+Z undoes the user's own last jump inside a track (owner request:
    //    "a way to rewind the last action of the user" — no button, shortcut
    //    only). The stack is bounded, one press drains one entry, a micro-seek
    //    never enters it, and a track change drops it.
    //
    // The sections above leave the lyrics pane open and the page wherever the
    // pane's own scrolling put it: reload onto the album route so the rows this
    // section clicks are the ones it means.
    await page.goto(`${BASE}/album/${encodeURIComponent(album.path)}`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector('tr[title="Click to play"]');
    await pressRow(0);
    await sleep(1500);
    const beforeUndo = await probe(page);
    const tFrom = playingT(beforeUndo);
    const longEnough = (beforeUndo.seekMax || 0) >= 8;
    if (!longEnough) {
      check("undo: a track long enough to jump in", true, `skipped — track is ${beforeUndo.seekMax}s`);
    } else {
      await clickBarAt(0.7);
      await sleep(400);
      const jumped = playingT(await probe(page));
      check("undo: a seek-bar jump lands far from where it started", jumped > tFrom + 4, `t ${tFrom} -> ${jumped}`);
      // The seek bar keeps the focus after a seek — Ctrl+Z must still work
      // (a range input is not "typing"; a text field would be).
      await page.evaluate(() => document.querySelector("input.seek-fat").focus());
      await page.keyboard.press("Control+z");
      await sleep(400);
      const undone = playingT(await probe(page));
      check("undo: Ctrl+Z seeks back to where the jump started (focus on the seek bar)",
        Math.abs(undone - tFrom) <= 1.5, `t=${undone} was ${tFrom}`);
      const singleFrom = playingT(await probe(page));
      await page.keyboard.press("Control+z");
      await sleep(400);
      const singleTo = playingT(await probe(page));
      check("undo: one entry, one press — a second Ctrl+Z is a no-op",
        singleTo >= singleFrom - 0.2 && singleTo - singleFrom <= 1.5, `t ${singleFrom} -> ${singleTo}`);
      // A micro-seek is noise, not an action: nothing to undo afterwards.
      // Clicked ~1 s on from where the track is now.
      const microT = playingT(await probe(page));
      const microMax = (await probe(page)).seekMax || 1;
      await clickBarAt(Math.min(0.99, (microT + 1) / microMax));
      await sleep(300);
      const micro = playingT(await probe(page));
      await page.keyboard.press("Control+z");
      await sleep(400);
      const afterMicro = playingT(await probe(page));
      check("undo: a 1 s micro-seek never enters the stack",
        afterMicro >= micro - 0.2 && afterMicro - micro <= 1.5, `micro t=${micro} after Ctrl+Z t=${afterMicro}`);
      // A stale entry: the jump was made in a track that is gone (a track
      // change clears the stack), so Ctrl+Z cannot seek the new one into it.
      await clickBarAt(0.85);
      await sleep(300);
      await pressRow(1);
      await sleep(1200);
      const newTrack = await probe(page);
      const newT = playingT(newTrack);
      await page.keyboard.press("Control+z");
      await sleep(400);
      const afterStale = playingT(await probe(page));
      check("undo: a jump made in another track is dropped, not applied",
        newT >= 0 && newT < 6 && afterStale >= 0 && afterStale < 6,
        `new track t=${newT} -> ${afterStale} (a stale entry would have jumped to the old track's second)`);
      assertConsistent("after Ctrl+Z", await probe(page), album.tracks[1]);
    }

    // 6. A reload mid-track: the queue, the track and the second inside it come
    //    back PAUSED (mlo.player.state.v1) — and the resume that follows is not
    //    a new play, while pressing the row again is one.
    await pressRow(0);
    await sleep(1500);
    const preReload = await probe(page);
    const tReload = playingT(preReload);
    const file0 = preReload.els.find((e) => !e.paused)?.file ?? "";
    const playsBefore = await playsTotal(page);
    await page.reload({ waitUntil: "domcontentloaded" });
    await sleep(2500);
    const postReload = await probe(page);
    const restored = postReload.els.find((e) => e.file === file0);
    check("reload: the queue and the row come back",
      postReload.pos === preReload.pos && postReload.len === preReload.len,
      `pos=${postReload.pos}/${postReload.len} (was ${preReload.pos}/${preReload.len})`);
    check("reload: the same track comes back PAUSED at the remembered second",
      !!restored && postReload.els.every((e) => e.paused) && !postReload.playing
        && Math.abs((restored?.t ?? -1) - tReload) <= 1 && tReload > 0.3,
      `t=${restored?.t} was ${tReload} playing=${postReload.playing} files=[${postReload.els.map((e) => e.file).join(", ")}]`);
    if (!longEnough) {
      check("reload: the play count is kept honest", true, "skipped — track too short to resume into");
    } else {
      await page.evaluate(() => (document.activeElement instanceof HTMLElement) && document.activeElement.blur());
      await page.keyboard.press("Space");                 // resume
      await sleep(1200);
      const resumeT = playingT(await probe(page));
      check("reload: resuming plays on from the restored second (no restart at 0)",
        resumeT > tReload - 0.5, `t=${resumeT} restored at ${restored?.t}`);
      check("reload: the resume is not counted as a new play",
        (await playsTotal(page)) === playsBefore, `plays ${playsBefore} -> ${await playsTotal(page)}`);
      await pressRow(0);                             // the same row, from the start
      await sleep(1500);
      check("pressing the same track from the start still counts a play",
        (await playsTotal(page)) === playsBefore + 1, `plays ${playsBefore} -> ${await playsTotal(page)}`);
    }

    // 7. `gapless_playback: false` — the preload and the handover are gated off,
    //    and the natural end still plays the next track through the normal load.
    const gaplessWas = await cfgValue(page, "gapless_playback");
    const setGapless = (v) => page.evaluate(async (val) => {
      const r = await fetch("/api/config", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ gapless_playback: val }),
      });
      return r.status;
    }, v);
    const setStatus = await setGapless(false);
    check("gapless: the server accepts gapless_playback=false", setStatus === 200, `status=${setStatus}`);
    await page.reload({ waitUntil: "domcontentloaded" });
    await sleep(1400);
    await pressRow(0);
    await sleep(1500);
    await seekNearEnd();                                  // the window a preload would use
    await sleep(600);
    const gaplessOff = await probe(page);
    check("gapless off: the idle element is NOT preloaded with the next track",
      gaplessOff.els.length <= 1,
      `elements-with-src=${gaplessOff.els.length} files=[${gaplessOff.els.map((e) => e.file).join(", ")}]`);
    let afterOff = null;
    for (let i = 0; i < 30; i++) {
      await sleep(1000);
      afterOff = await probe(page);
      if (afterOff.pos === gaplessOff.pos + 1 && afterOff.els.some((e) => !e.paused)) break;
    }
    assertConsistent("gapless off: the natural end still loads the next track", afterOff,
      album.tracks[afterOff.pos - 1]);
    await setGapless(gaplessWas === undefined ? true : gaplessWas);
    await page.reload({ waitUntil: "domcontentloaded" });

    // 8. The cover on the play path: the width the surface DRAWS, one request
    //    for it, and no network at all on a repeat play.
    //
    //    Pressing play used to fetch the album's whole cover file (a
    //    1200-3000 px JPEG, 0.3-3 MB) for a 74 px bar thumb — twice, because
    //    the OS media session asks for its own — and every later track of that
    //    album revalidated it (`Cache-Control: no-cache`), which is the second
    //    the owner measured between pressing play and the artwork appearing.
    //    `?w=` serves the file shrunk to a bucket (server/artcache.py's
    //    `cover_thumb`), re-encoded once and then read from disk, and its
    //    answer is cacheable for minutes. This pins the CLIENT's half of the
    //    contract: ask for a thumbnail, keep it, and do not ask again for the
    //    same album's cover.
    //
    //    The browser cache is emptied first: the sections above have already
    //    played this album, and a property about what a play COSTS cannot be
    //    read off a warm cache.
    const coversOnWire = () => page.evaluate(() =>
      performance.getEntriesByType("resource")
        .filter((e) => e.name.includes("/api/cover") && !e.name.includes("color=1"))
        .map((e) => ({ url: e.name, bytes: e.transferSize })));
    const barArt = () => page.evaluate(() => {
      const img = document.querySelector('button[title^="Album art"] img');
      return img ? { src: img.currentSrc || img.src, w: img.naturalWidth, complete: img.complete } : null;
    });
    const albumCoverOf = (url) => url.includes(encodeURIComponent(album.path)) || url.includes(album.path);

    const cdp = await page.context().newCDPSession(page);
    await cdp.send("Network.enable");
    await cdp.send("Network.clearBrowserCache");
    await page.goto(`${BASE}/album/${encodeURIComponent(album.path)}`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector('tr[title="Click to play"]');
    await page.evaluate(() => performance.clearResourceTimings());
    await pressRow(0);
    await sleep(3000);
    const art1 = await barArt();
    const played = await coversOnWire();
    const barUrl = art1?.src || "";
    const mine = played.filter((e) => e.url === barUrl);
    const fetched = mine.filter((e) => e.bytes > 0);
    check("the play path asks for a cover at the width it draws (not the master)",
      !!art1 && art1.complete && /[?&]w=\d+/.test(art1.src) && art1.w > 0 && art1.w <= 320,
      `src=…${(art1?.src || "").split("/api/cover")[1]} naturalWidth=${art1?.w} complete=${art1?.complete}`);
    // A track row draws its cover at 32-36 px and asks for the same bucket, so
    // the row that queued the track and the bar that shows it are ONE request;
    // a row carrying its own art (a per-track sidecar) legitimately differs.
    const rowSrc = await page.evaluate(() => {
      const img = document.querySelector('tr img[src*="/api/cover"]');
      return img ? img.src : "";
    });
    const fileParam = (u) => (/[?&]file=([^&]*)/.exec(u || "") || [, ""])[1];
    check("the bar and a track row share ONE cover URL",
      !rowSrc || fileParam(rowSrc) !== fileParam(barUrl) || rowSrc === barUrl,
      `row=…${rowSrc.split("/api/cover")[1] || rowSrc} bar=…${barUrl.split("/api/cover")[1]}`);
    check("the play path fetches it once, small — never the master",
      fetched.length <= 1 && fetched.every((e) => e.bytes < 128 * 1024),
      `fetches=${fetched.length} bytes=[${fetched.map((e) => e.bytes).join(", ")}] (0 = the ` +
      `entry was already in the browser's memory cache) ` +
      `album-cover entries=${played.filter((e) => albumCoverOf(e.url)).length}`);

    // The repeat play: the next row of the SAME album (same cover URL). Its
    // cached bytes are still fresh, so the network must not be touched — the
    // bar's own re-render, the queue row and the OS media session all read the
    // entry the first play put there.
    await pressRow(1);
    await sleep(2500);
    const after2 = await coversOnWire();
    const freshForBar = after2.filter((e) => e.url === barUrl && e.bytes > 0)
      .filter((e) => !mine.some((p) => p.url === e.url && p.bytes === e.bytes));
    check("a repeat play of the same album asks the network for nothing",
      freshForBar.length === 0,
      `fresh entries for the bar's cover: ${JSON.stringify(freshForBar)}`);
    await cdp.detach().catch(() => {});

    // 10. Infinite playback. `infinite_playback` (shipped OFF) is the switch
    //     that lets the queue continue past its own last row: the player hands
    //     the queue's OWN paths to the server's local scorer (they are the seed
    //     set AND the exclusion set) and appends the bounded batch it answers
    //     with as ORDINARY queue rows. Measured here on the smallest queue the
    //     library holds (two rows, so "the last row" is reachable) and only
    //     when that queue really scores a batch — a library with nothing
    //     similar skips the case by name instead of failing on its contents.
    //
    //     The bug this pins is the ORDER of the two things: the batch has to
    //     land BEFORE the gapless preload arms for the row that follows the
    //     last one, or the queue runs dry mid-track, stops, and the suggested
    //     tracks start after a silence. So the idle decoder's own contents are
    //     read here (the state section 1 measures for an album), and the
    //     natural end must hand over into the added set.
    const infWas = await cfgValue(page, "infinite_playback");
    const setInfinite = (v) => page.evaluate(async (val) => {
      const r = await fetch("/api/config", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ infinite_playback: val }),
      });
      return r.status;
    }, v);
    /** The rows the bar's queue popover shows as up next, in its own order. */
    const infUpNext = () => page.evaluate(() =>
      [...document.querySelectorAll('div[title="Drag to reorder · click to play now"]')]
        .map((r) => (r.querySelector("span.text-xs") || {}).textContent || ""));
    const infOpenQueue = async () => {
      await page.locator('button[aria-label="Queue"]').first().click();
      await sleep(350);
    };
    /** The app's OWN persisted queue (store.ts → `mlo.player.state.v1`), read
     *  as paths: the store's record of the rows, which is what a duplicate row
     *  can be told apart from a repeated title by. Polled, because the write is
     *  coalesced (PLAYER_WRITE_MS). */
    const infStored = async (want) => {
      const read = () => page.evaluate(() => {
        try {
          const st = JSON.parse(localStorage.getItem("mlo.player.state.v1") || "null");
          return st ? { index: st.index, paths: (st.queue || []).map((t) => t.path) } : null;
        } catch { return null; }
      });
      let st = null;
      for (let i = 0; i < 25; i++) {
        st = await read();
        if (st && st.paths.length >= want) return st;
        await sleep(200);
      }
      return st;
    };
    const infSeekTo = async (frac) => {
      const box = await page.locator("input.seek-fat").first().boundingBox();
      if (!box || !box.width) throw new Error("seek bar missing");
      await page.mouse.click(box.x + box.width * frac, box.y + box.height / 2);
    };
    /** One drag phase of the popover's OWN reorder (the `queueMove` every other
     *  queue row has always used): start = dragstart+dragover, end = drop+
     *  dragend. Split in two so React has re-rendered `overOff` before the drop
     *  reads it — a real drag has that gap, a scripted one has to make it. */
    const infDrag = (phase, from, to) => page.evaluate(([ph, f, t]) => {
      const els = [...document.querySelectorAll('div[title="Drag to reorder · click to play now"]')];
      if (!els[f] || !els[t]) return false;
      const dt = new DataTransfer();
      dt.setData("text/plain", String(f));
      els[f].dispatchEvent(new DragEvent(ph === "start" ? "dragstart" : "dragend",
        { bubbles: true, dataTransfer: dt }));
      els[t].dispatchEvent(new DragEvent(ph === "start" ? "dragover" : "drop",
        { bubbles: true, cancelable: true, dataTransfer: dt }));
      return true;
    }, [phase, from, to]);

    // The two-track album (the smallest queue there is) and the batch its own
    // paths score — fetched here, so the app cannot be credited for rows the
    // scorer does not answer with.
    const infPair = await page.evaluate(async (base) => {
      const lib = await (await fetch(base + "/api/library")).json();
      for (const a of lib.artists || []) {
        for (const al of a.albums || []) {
          if ((al.track_count ?? (al.tracks || []).length) !== 2) continue;
          const full = await (await fetch(base + "/api/album?path=" + encodeURIComponent(al.path))).json();
          const tracks = (full.tracks || []).map((t) => ({ path: t.path, file: t.file }));
          if (tracks.length === 2) return { path: al.path, tracks };
        }
      }
      return null;
    }, BASE);
    const infBatch = infPair ? await page.evaluate(async (paths) => {
      const q = new URLSearchParams();
      for (const p of paths) q.append("paths", p);
      const r = await fetch("/api/recommend/queue?" + q.toString());
      if (!r.ok) return null;
      return ((await r.json()).items || []).map((i) => ({ path: i.path, title: i.title, file: i.file }));
    }, infPair.tracks.map((t) => t.path)) : null;

    if (infWas === undefined) {
      check("infinite: the server ships the switch", true,
        "skipped — this server has no infinite_playback key");
    } else if (!infPair || !infBatch || infBatch.length < 2) {
      check("infinite: the library offers a two-track queue that scores a batch", true,
        `skipped — pair=${infPair ? infPair.path : "none"} batch=${infBatch ? infBatch.length : "n/a"} ` +
        "(this case needs a two-track album and at least two similar tracks elsewhere)");
    } else {
      const infBatchTitles = infBatch.map((i) => i.title);

      // 10a. ON: the last row of the two-track queue gains exactly the batch,
      //      as rows the popover draws, reorders and counts like any other.
      const infStatus = await setInfinite(true);
      check("infinite: the server accepts infinite_playback=true", infStatus === 200, `status=${infStatus}`);
      await page.reload({ waitUntil: "domcontentloaded" });
      await sleep(1500);
      check("infinite: the switch reads back ON",
        (await cfgValue(page, "infinite_playback")) === true,
        `infinite_playback=${JSON.stringify(await cfgValue(page, "infinite_playback"))}`);
      await page.goto(`${BASE}/album/${encodeURIComponent(infPair.path)}`, { waitUntil: "domcontentloaded" });
      await page.waitForSelector('tr[title="Click to play"]');
      const infRows = await page.locator('tr[title="Click to play"]').count();
      await pressRow(infRows - 1);                       // the album's LAST row
      // The GROWN queue, read from the store's own record (paths) and from the
      // bar's counter. Nothing here races the append: the base is the album's
      // two rows BY CONSTRUCTION (the pair album is the two-track one this case
      // selected), so the growth measured below is the batch itself.
      const infQueued = await infStored(2 + infBatch.length);
      check(`infinite: the two-row queue gains exactly the batch (${infBatch.length})`,
        !!infQueued && infQueued.paths.length === 2 + infBatch.length,
        `len=${infQueued ? infQueued.paths.length : "?"} want=${2 + infBatch.length}`);
      const infBatchPaths = infBatch.map((i) => i.path);
      check("infinite: the added rows ARE the batch these seeds score, in order",
        !!infQueued && JSON.stringify(infQueued.paths.slice(2)) === JSON.stringify(infBatchPaths),
        `added=${JSON.stringify(infQueued?.paths.slice(2))} expected=${JSON.stringify(infBatchPaths)}`);
      check("infinite: two rows in front of the batch, nothing in the queue twice",
        !!infQueued && infQueued.paths.slice(0, 2).join() === infPair.tracks.map((t) => t.path).join()
          && new Set(infQueued.paths).size === infQueued.paths.length
          && infQueued.index === 1,
        `paths=${JSON.stringify(infQueued?.paths)} index=${infQueued?.index}`);
      let infGrown = await probe(page);
      for (let i = 0; i < 30 && !(infGrown.len === 2 + infBatch.length && infGrown.pos === 2); i++) {
        await sleep(300);
        infGrown = await probe(page);
      }
      check("infinite: the bar's own queue counter shows the grown queue",
        infGrown.len === 2 + infBatch.length && infGrown.pos === 2,
        `pos=${infGrown.pos}/${infGrown.len}`);
      // And the queue PANE draws each added row, in the batch's order — the rows
      // are ordinary queue rows, not a hidden mode.
      await infOpenQueue();
      const infAdded = await infUpNext();
      check("infinite: the queue pane draws every added row",
        JSON.stringify(infAdded) === JSON.stringify(infBatchTitles),
        `drawn=${JSON.stringify(infAdded)} expected=${JSON.stringify(infBatchTitles)}`);

      // Ordinary rows: the popover's own drag reorders them (the same
      // `queueMove` every other queue row has always used), and the reorder
      // reaches the store like any other edit's does.
      const infOrderBefore = infQueued.paths.slice(2);
      await infDrag("start", 0, 1);
      await sleep(250);
      await infDrag("end", 0, 1);
      await sleep(800);
      const infQueued2 = await infStored(2 + infBatch.length);
      const infOrderAfter = infQueued2 ? infQueued2.paths.slice(2) : [];
      check("infinite: an added row reorders like any other queue row",
        infOrderAfter.length === infOrderBefore.length
          && infOrderAfter[0] === infOrderBefore[1] && infOrderAfter[1] === infOrderBefore[0]
          && infOrderAfter.slice(2).join() === infOrderBefore.slice(2).join(),
        `${JSON.stringify(infOrderBefore)} -> ${JSON.stringify(infOrderAfter)}`);

      // The batch landed BEFORE the preload window opened: the idle decoder
      // holds the row that now follows the last one — the first of the batch,
      // as the drag left the order.
      await infSeekTo(0.85);
      let infArmed = null;
      for (let i = 0; i < 15; i++) {
        await sleep(200);
        infArmed = await probe(page);
        if (infArmed.els.length > 1) break;
      }
      const infFileByPath = new Map(infBatch.map((i) => [i.path, i.file]));
      const infExpectedNext = infFileByPath.get(infOrderAfter[0] || "");
      check("infinite: the idle decoder preloads the first row of the batch",
        !!infArmed && infArmed.els.length === 2 && !!infExpectedNext
          && infArmed.els.some((e) => e.file === infExpectedNext && e.paused),
        `els=[${(infArmed?.els || []).map((e) => e.file + (e.paused ? "" : "*")).join(", ")}] ` +
        `expected=${infExpectedNext}`);
      let infAfter = null;
      for (let i = 0; i < 20; i++) {
        await sleep(700);
        infAfter = await probe(page);
        if (infAfter.pos === infGrown.pos + 1) break;
      }
      assertConsistent("infinite: the natural end hands over into the batch", infAfter, infExpectedNext);
      check("infinite: the hand-over is a queue advance, not a restart",
        !!infAfter && infAfter.pos === infGrown.pos + 1 && infAfter.len === infGrown.len,
        `pos ${infGrown.pos} -> ${infAfter?.pos} len=${infAfter?.len} (was ${infGrown.len})`);

      // 10a2. Shuffle is NOT exempt: a shuffled queue's last row still gains the
      //       seeds' similar set — the appended rows are the scorer's answer,
      //       not a random draw. Only Repeat one appends nothing.
      await page.keyboard.press("Escape");               // the pane 10a opened
      await sleep(250);
      const infShuffleBtn = page.locator('button[title="Shuffle"]').first();
      await infShuffleBtn.click();
      await sleep(250);
      await pressRow(infRows - 1);
      await sleep(900);                                  // the append, then its coalesced write
      const infShuffled = await infStored(2 + infBatch.length);
      check("infinite: with Shuffle on the added rows are still the seeds' set",
        !!infShuffled && infShuffled.index === 1
          && infShuffled.paths.length === 2 + infBatch.length
          && JSON.stringify(infShuffled.paths.slice(2)) === JSON.stringify(infBatchPaths),
        `paths=${JSON.stringify(infShuffled?.paths)} index=${infShuffled?.index}`);
      await infShuffleBtn.click();                       // Shuffle off
      await sleep(250);

      // 10b. Repeat one: that row's successor is ITSELF, so nothing is appended.
      const infLoopBtn = page.locator('button[title="Repeat one"]').first();
      await infLoopBtn.click();
      await page.keyboard.press("Escape");
      await sleep(250);
      await pressRow(infRows - 1);
      await sleep(900);
      const infLoopStart = await probe(page);
      await infSeekTo(0.9);
      let infLooped = null;
      for (let i = 0; i < 24; i++) {
        await sleep(500);
        infLooped = await probe(page);
        if (infLooped.els.some((e) => !e.paused && e.t < 3)) break;
      }
      check("infinite: Repeat one appends nothing and restarts the row in place",
        !!infLooped && infLoopStart.len === 2 && infLooped.len === 2
          && infLooped.pos === 2 && infLooped.playing,
        `len ${infLoopStart.len} -> ${infLooped?.len} pos=${infLooped?.pos} playing=${infLooped?.playing}`);
      await infLoopBtn.click();                          // Repeat one off

      // 10c. OFF (the shipped default): the same queue ends exactly as it did
      //      before this feature existed — nothing is appended and no batch is
      //      even asked for.
      const infSeen = [];
      const infWatch = (r) => { if (r.url().includes("/api/recommend/queue")) infSeen.push(r.url()); };
      await setInfinite(false);
      await page.reload({ waitUntil: "domcontentloaded" });
      await sleep(1500);
      page.on("request", infWatch);
      await page.goto(`${BASE}/album/${encodeURIComponent(infPair.path)}`, { waitUntil: "domcontentloaded" });
      await page.waitForSelector('tr[title="Click to play"]');
      await pressRow(infRows - 1);
      await sleep(1200);
      const infOffStart = await probe(page);
      await infSeekTo(0.9);
      let infOffEnd = null;
      for (let i = 0; i < 25; i++) {
        await sleep(600);
        infOffEnd = await probe(page);
        if (!infOffEnd.playing) break;
      }
      check("infinite off: the last row is not extended",
        infOffStart.len === 2 && infOffEnd?.len === 2, `len ${infOffStart.len} -> ${infOffEnd?.len}`);
      check("infinite off: the queue ends after its last track, exactly as before",
        !!infOffEnd && !infOffEnd.playing && infOffEnd.pos === infOffStart.pos
          && infOffEnd.els.every((e) => e.paused),
        `pos=${infOffEnd?.pos}/${infOffEnd?.len} playing=${infOffEnd?.playing} ` +
        `els=[${(infOffEnd?.els || []).map((e) => e.file + (e.paused ? "" : "*")).join(", ")}]`);
      check("infinite off: the player never even asks for a batch", infSeen.length === 0,
        `requests=${JSON.stringify(infSeen)}`);
      await infOpenQueue();
      const infHonest = await page.evaluate(() =>
        document.body.innerText.includes("Nothing up next — it ends after this track."));
      check("infinite off: the queue pane promises the end it delivers", infHonest, "");
      page.off("request", infWatch);
      await setInfinite(infWas === true);
      await page.reload({ waitUntil: "domcontentloaded" });
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
