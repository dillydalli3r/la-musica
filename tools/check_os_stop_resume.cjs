#!/usr/bin/env node
/* The platform stopped the music while the page was hidden — the app must pick
 * the track up again when the reader comes back, and only then.
 *
 * The owner's other #55 report is "audio stops playing after the app is
 * unfocused": iOS takes the audio session away from a backgrounded webview, the
 * element pauses, and the app comes back showing a bar that has to be pressed
 * again. Nothing in the page asked for that pause, so it is the one class of
 * stop the app can undo on its own — and the one class it must not overreach
 * on: a pause the reader (or the lock screen, or the sleep timer) asked for has
 * to survive a trip to the background.
 *
 * Four cases, all with the document's own visibility under test control (the
 * getter is replaced in an init script; the real events are dispatched):
 *   (a) OS-caused: hidden, the element's own `pause` with no app cause, then
 *       visible + `visibilitychange` + `pageshow` -> PLAYING again, resumed
 *       where it stopped, exactly one extra play(), and an `os-stop`/`os-resume`
 *       pair in the playback report naming what happened;
 *   (b) deliberate: a pause the app asked for (the transport while visible, and
 *       a lock-screen press that lands while hidden and armed) is never undone
 *       by coming back;
 *   (c) untouched: nothing stopped, so coming back issues no play() and adds no
 *       row — the fix must be invisible on the happy path;
 *   (d) already restarted: the platform put the track back itself while hidden,
 *       so there is nothing left to restart and coming back must play nothing.
 *
 * Needs the app served at 390 px against a scratch backend with a real FLAC
 * library — a Vite dev server in front of it is enough, and is what this was
 * developed against (the repo's own vite.config proxies /api to the owner's
 * LIVE server on 8000, so a scratch config has to override the proxy):
 *   # an album of silent FLACs is enough, e.g. with the bundled ffmpeg:
 *   ffmpeg -f lavfi -i anullsrc=channel_layout=stereo:sample_rate=44100 -t 20 \
 *     -c:a flac -metadata title=One -metadata artist=A -metadata album=B 01.flac
 *   MLO_MUSIC_FOLDER=F:/tmp/mlo-iss55-player/music \
 *     python -m uvicorn server.main:app --host 127.0.0.1 --port 8013
 *   curl -X POST 127.0.0.1:8013/api/config -H 'Content-Type: application/json' \
 *     -d '{"music_folder":"F:/tmp/mlo-iss55-player/music","first_run_done":true}'
 *   curl "127.0.0.1:8013/api/library?refresh=1"      # so the album page has rows
 *   # <scratch>.mjs, run from web/:  export default { ...base,
 *   #   root: "<repo>/web", server: { host: "127.0.0.1", port: 8014,
 *   #   strictPort: true, proxy: { "/api": "http://127.0.0.1:8013" } } }
 *   npx vite --config <scratch>.mjs
 *   PLAYWRIGHT=web/node_modules/playwright node tools/check_os_stop_resume.cjs http://127.0.0.1:8014
 *
 * Playwright is required (same resolution as tools/shot.cjs): set PLAYWRIGHT
 * to a module path, or install it. Exit 2 when it is missing. */

let chromium;
try {
  ({ chromium } = require(process.env.PLAYWRIGHT || "playwright"));
} catch {
  console.error("[os-stop] Playwright not found — `npm i -D playwright`, " +
    "or point PLAYWRIGHT at an installed module.");
  process.exit(2);
}

const BASE = process.argv[2] || process.env.BASE || "http://127.0.0.1:8013";
const results = [];
const check = (name, pass, detail) => results.push({ name, pass: !!pass, detail: String(detail) });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Wait for a truthy probe, or give up and answer null. */
async function until(fn, ms = 10000, step = 150) {
  const end = Date.now() + ms;
  for (;;) {
    const v = await fn();
    if (v) return v;
    if (Date.now() > end) return null;
    await sleep(step);
  }
}

/* Everything the three cases need the page to lie about, installed before any
 * app script runs: a writable visibility, the platform's own transport press
 * (mediaSession handlers cannot be fired from page script, so the check keeps
 * them), and a count of every play() the page issues — an element that is
 * "playing again" after two starts is not the same thing as one start. */
const INIT = `(() => {
  /* The file a media element really holds. The stream URL carries the library
   * path in its query, so the basename comes out of it — the same name the
   * player's own report rows use. */
  window.__fileOf = (raw) => {
    const s = String(raw || "");
    if (!s) return "";
    let p = s;
    try {
      const u = new URL(s, location.href);
      p = u.searchParams.get("path") || u.pathname;
    } catch { /* not a URL: use what we were given */ }
    return decodeURIComponent(p).split(/[\\\\/]/).pop();
  };
  window.__playCalls = [];
  const origPlay = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function (...args) {
    try { window.__playCalls.push(window.__fileOf(this.currentSrc || this.src)); } catch { /* the count still moved */ }
    return origPlay.apply(this, args);
  };
  let vis = "visible";
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => vis });
  Object.defineProperty(document, "hidden", { configurable: true, get: () => vis === "hidden" });
  window.__setVis = (v) => { vis = v; document.dispatchEvent(new Event("visibilitychange")); };
  window.__pageShow = (persisted) =>
    window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: !!persisted }));
  window.__ms = {};
  try {
    const ms = navigator.mediaSession;
    if (ms && typeof ms.setActionHandler === "function") {
      const orig = ms.setActionHandler.bind(ms);
      ms.setActionHandler = (name, fn) => { window.__ms[name] = fn; return orig(name, fn); };
    }
  } catch { /* no media session here: that sub-case reports itself skipped */ }
})();`;

/** What the decoder is doing, what the page thinks it can see, and how many
 *  starts it has issued. */
const state = (page) => page.evaluate(() => {
  const els = [...document.querySelectorAll("audio")].map((a) => ({
    file: window.__fileOf(a.currentSrc || a.src),
    t: Number(a.currentTime.toFixed(2)),
    paused: a.paused,
    ended: a.ended,
    readyState: a.readyState,
  })).filter((e) => e.file && e.file !== "stream");
  return {
    vis: document.visibilityState,
    els,
    live: els.find((e) => !e.paused) || null,
    plays: window.__playCalls || [],
    transport: [...document.querySelectorAll("button")].some((b) => b.title === "Pause"),
  };
});

const setVis = (page, v) => page.evaluate((v) => window.__setVis(v), v);

/** The way back in, the way the platform announces it: visibility first, then
 *  the bfcache-safe `pageshow`. */
async function foreground(page) {
  await setVis(page, "visible");
  await page.evaluate(() => window.__pageShow(false));
}

/** The platform stopping the decoder, with no app code involved: the element's
 *  own pause, exactly what a suspended webview leaves behind. */
const osPause = (page) => page.evaluate(() => {
  const a = [...document.querySelectorAll("audio")].find((x) => !x.paused);
  if (a) a.pause();
  return !!a;
});

const playsOf = (s, file) => s.plays.filter((f) => f === file).length;

/** The decoders, for a failure message: "<file>:paused" each. */
const fmtEls = (s) => `[${s.els.map((e) => `${e.file}:${e.paused ? "paused" : "playing"}`).join(", ")}]`;

/** The playback report's rows, as the Settings panel renders them:
 *  "<clock> <kind> <k=v …>". Found through its Copy button's icon — the
 *  heading is translated, the icon is not. */
const diagRows = (page) => page.evaluate(() => {
  const det = document.querySelector("details:has(svg.lucide-copy)");
  if (!det) return null;
  const box = [...det.querySelectorAll("div")].find((d) => /leading-relaxed/.test(d.className || "") && d.children.length);
  if (!box) return null;
  return [...box.children].map((c) => (c.textContent || "").replace(/\s+/g, " ").trim());
});

/** The rows carrying one kind. The panel renders the kind and its first detail
 *  key with nothing between them ("os-stoppath=…"), so a `\b` word boundary is
 *  no use — anchor after the clock instead, and take what follows as either a
 *  space, the end, or the "k=v" pair that starts the detail. */
const kindRe = (kind) => new RegExp(`^\\d{2}:\\d{2}:\\d{2}\\s*${kind}(?=$|[\\s]|[a-z_]+=)`);
const rowsOf = (rows, kind) => (rows || []).filter((r) => kindRe(kind).test(r));

(async () => {
  let browser;
  const errs = [];
  try {
    browser = await chromium.launch({ executablePath: process.env.CHROME, headless: true });
    const ctx = await browser.newContext({
      viewport: { width: 390, height: 780 },   // the phone both reports came from
      serviceWorkers: "block",                 // a cached shell would hide the source under test
    });
    await ctx.addInitScript(INIT);
    const page = await ctx.newPage();
    page.setDefaultTimeout(20000);
    page.on("pageerror", (e) => errs.push(e.message));

    // The one album of the scratch library, and its first track.
    await page.goto(BASE, { waitUntil: "domcontentloaded" });
    const album = await page.evaluate(async (base) => {
      const lib = await (await fetch(base + "/api/library")).json();
      for (const ar of lib.artists || []) {
        for (const al of ar.albums || []) {
          const full = await (await fetch(base + "/api/album?path=" + encodeURIComponent(al.path))).json();
          if ((full.tracks || []).length) return { path: al.path, file: full.tracks[0].file };
        }
      }
      return null;
    }, BASE);
    if (!album) throw new Error("no album with a track in the scratch library");

    await page.goto(`${BASE}/album/${encodeURIComponent(album.path)}`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector('tr[title="Click to play"]');
    await page.locator('tr[title="Click to play"]').first().locator("td").first().click();

    const started = await until(async () => {
      const s = await state(page);
      return s.live && s.live.readyState >= 2 ? s : null;
    });
    check("playback started on the phone layout",
      !!started && started.live.file === album.file && started.vis === "visible",
      started ? `live=${started.live.file} t=${started.live.t} vis=${started.vis}` : "nothing playing");
    if (!started) throw new Error("playback never started — nothing below can be judged");
    await sleep(1200);   // clear the first second, so "resumed" is distinguishable from "restarted"

    // ---------------------------------------------------------------- (a) OS
    const beforeA = await state(page);
    const tStopped = beforeA.live.t;
    await setVis(page, "hidden");
    const didPause = await osPause(page);
    const hiddenA = await state(page);
    check("(a) the platform stop happens while the page is hidden",
      didPause && hiddenA.vis === "hidden", `element paused=${didPause} vis=${hiddenA.vis}`);
    await sleep(400);
    const stoppedA = await state(page);
    check("(a) the element is really stopped and the bar knows it",
      !stoppedA.live && stoppedA.els.length > 0 && !stoppedA.transport,
      `paused=[${stoppedA.els.map((e) => e.file + ":" + e.paused).join(", ")}] transport=${stoppedA.transport ? "Pause" : "Play"}`);

    await foreground(page);
    const backA = await until(async () => { const s = await state(page); return s.live ? s : null; }, 5000);
    const afterA = await state(page);
    check("(a) coming back RESTARTS the track the OS stopped", !!backA && afterA.vis === "visible",
      backA ? `live=${backA.live.file} t=${backA.live.t}` : `still paused=[${afterA.els.map((e) => e.file + ":" + e.paused).join(", ")}]`);
    check("(a) it resumes where it stopped rather than replaying from 0",
      !!backA && backA.live.file === album.file && backA.live.t >= tStopped,
      `t ${tStopped} -> ${backA?.live?.t}`);
    check("(a) exactly one extra play() was issued (visibilitychange + pageshow together)",
      playsOf(afterA, album.file) === playsOf(beforeA, album.file) + 1,
      `plays ${playsOf(beforeA, album.file)} -> ${playsOf(afterA, album.file)}`);

    // ------------------------------------------------------------- (c) no-op
    // Judged only while a track really is playing: on a build where (a) failed
    // there is nothing to leave alone, and that failure is already reported —
    // a second one here would only blur it.
    const beforeC = await state(page);
    if (!beforeC.live) {
      check("(c) nothing stopped: the track is still playing itself", false,
        `nothing playing to leave alone — ${fmtEls(beforeC)}`);
    } else {
      await setVis(page, "hidden");
      await sleep(400);
      await foreground(page);
      await sleep(700);
      const afterC = await state(page);
      check("(c) nothing stopped: the track is still playing itself", !!afterC.live && afterC.live.file === album.file, afterC.live ? `live=${afterC.live.file} t=${afterC.live.t}` : `nothing playing — ${fmtEls(afterC)}`);
      check("(c) nothing stopped: no play() was issued on the way back",
        playsOf(afterC, album.file) === playsOf(beforeC, album.file),
        `plays ${playsOf(beforeC, album.file)} -> ${playsOf(afterC, album.file)}`);
      check("(c) nothing stopped: the clock kept running through the flip", !!afterC.live && afterC.live.t > beforeC.live.t, `t ${beforeC.live.t} -> ${afterC.live?.t}`);
    }

    // ------------------------------------------ (b) the reader asked for this
    const pauseBtn = page.locator('button[title="Pause"]');
    if (!(await pauseBtn.count())) {
      check("(b) the transport pauses the track", false, `no Pause button — ${fmtEls(await state(page))}`);
    } else {
      await pauseBtn.click();
      await sleep(300);
      const pausedB1 = await state(page);
      check("(b) the transport pauses the track", !!pausedB1.els.length && !pausedB1.live && pausedB1.els.every((e) => e.paused), fmtEls(pausedB1));
      await setVis(page, "hidden");
      await sleep(300);
      await foreground(page);
      await sleep(700);
      const afterB1 = await state(page);
      check("(b) a pause the reader pressed survives coming back",
        !afterB1.live && afterB1.els.every((e) => e.paused) && playsOf(afterB1, album.file) === playsOf(pausedB1, album.file),
        `${fmtEls(afterB1)} plays ${playsOf(pausedB1, album.file)} -> ${playsOf(afterB1, album.file)}`);
    }

    // (b2) The dangerous order: the OS stops it while hidden (so the recovery
    // is armed), and THEN the lock screen's pause arrives — still hidden — as it
    // does when the owner presses the widget on the lock screen. That pause is
    // the reader's, so the armed recovery must be dropped, not fired.
    const playBtn = page.locator('button[title="Play"]');
    if (await playBtn.count()) await playBtn.click();   // already playing: the transport offers Pause
    const resumedB2 = await until(async () => { const s = await state(page); return s.live ? s : null; }, 5000);
    const msHandler = await page.evaluate(() => typeof window.__ms.pause === "function");
    // How many platform stops the run staged: (a) always, (b2) when it can.
    let stagedStops = 1;
    if (!msHandler) {
      check("(b2) lock-screen pause handler is registered (skipped when the browser has no media session)", true, "no mediaSession in this browser — sub-case not exercised");
    } else if (!resumedB2) {
      check("(b2) playback is running for the lock-screen case", false, `nothing playing — ${fmtEls(await state(page))}`);
    } else {
      const beforeB2 = await state(page);
      await setVis(page, "hidden");
      await osPause(page);                                     // arms the recovery
      stagedStops += 1;
      await sleep(250);
      const armed = await state(page);
      await page.evaluate(() => window.__ms.pause());          // the lock-screen press, still hidden
      await sleep(250);
      const pressed = await state(page);
      check("(b2) the lock-screen press pauses the track it arrived at",
        !armed.live && !pressed.live && pressed.els.every((e) => e.paused),
        `armed=${fmtEls(armed)} pressed=${fmtEls(pressed)}`);
      await foreground(page);
      await sleep(700);
      const afterB2 = await state(page);
      check("(b2) the armed recovery is dropped: a lock-screen pause is not undone by coming back",
        !afterB2.live && afterB2.els.every((e) => e.paused) && playsOf(afterB2, album.file) === playsOf(beforeB2, album.file),
        `${fmtEls(afterB2)} plays ${playsOf(beforeB2, album.file)} -> ${playsOf(afterB2, album.file)}`);
    }

    // (e) What the app DECLARES to the OS, which is what decides which glyphs
    // the lock screen draws: a track step and nothing else. WebKit offers a web
    // page skip-forward/skip-backward by default — with its own interval,
    // whether or not the page ever asked — and the app removes both (a `null`
    // handler is the spec's "this action is not supported") while registering
    // the step its own transport has. The owner's card (issue #55) showed
    // ⟲10 / 10⟳ beside this app's own metadata. Which glyphs the OS finally
    // paints needs a device; the SET that produces them is read here.
    const actions = await page.evaluate(() => Object.fromEntries(
      Object.entries(window.__ms || {}).map(([k, v]) => [k, v === null ? "null" : typeof v])));
    check("(e) the track step is declared to the OS (previous / next / seek / play / pause)",
      ["previoustrack", "nexttrack", "seekto", "play", "pause"].every((k) => actions[k] === "function"),
      JSON.stringify(actions));
    check("(e) the skip pair the lock screen would draw instead is declared unsupported",
      actions.seekbackward === "null" && actions.seekforward === "null",
      JSON.stringify(actions));

    // (d) The other shape of the same report: iOS restarted the track itself
    // while hidden (the `play` with nobody looking that the report exists to
    // name). The stop is still marked, but there is nothing left to restart —
    // coming back must add no play() at all, which is what keeps a platform
    // restart from being counted, or doubled, on the way in.
    if (await playBtn.count()) await playBtn.click();
    const forD = await until(async () => { const s = await state(page); return s.live ? s : null; }, 5000);
    if (!forD) {
      check("(d) playback is running for the already-restarted case", false, `nothing playing — ${fmtEls(await state(page))}`);
    } else {
      await setVis(page, "hidden");
      await osPause(page);
      stagedStops += 1;
      await sleep(250);
      const restarted = await page.evaluate((file) => {
        const a = [...document.querySelectorAll("audio")]
          .find((x) => x.paused && window.__fileOf(x.currentSrc) === file);
        if (a) a.play();
        return !!a;
      }, album.file);
      await sleep(400);
      const hiddenD = await state(page);
      check("(d) the platform restarted the track while the page was hidden",
        restarted && !!hiddenD.live && hiddenD.vis === "hidden",
        `restarted=${restarted} live=${hiddenD.live?.file ?? "(none)"} vis=${hiddenD.vis}`);
      const playsBeforeD = playsOf(hiddenD, album.file);
      await foreground(page);
      await sleep(700);
      const afterD = await state(page);
      check("(d) a track the platform already restarted is not played at again",
        playsOf(afterD, album.file) === playsBeforeD && !!afterD.live,
        `plays ${playsBeforeD} -> ${playsOf(afterD, album.file)} ${fmtEls(afterD)}`);
    }

    // ------------------------------------------------------- the report rows
    // Read last, in the app: the playback report lives on Settings → Downloads
    // & playback (`?tab=downloads`), and the player bar (and its elements)
    // survive an in-app route change.
    await page.evaluate(() => {
      history.pushState({}, "", "/settings?tab=downloads");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    const rows = await until(async () => (await diagRows(page)) ?? false, 8000);
    if (!rows) {
      check("the playback report is reachable (Settings)", false, "no report block found at /settings");
    } else {
      // Two platform stops were staged — (a) and (b2), both hidden and both with
      // no app cause — and exactly one of them may be resumed: the one the
      // reader did not answer with a pause.
      const stops = rowsOf(rows, "os-stop");
      const resumes = rowsOf(rows, "os-resume");
      const hiddenPause = rowsOf(rows, "pause").find((r) => /asked=false/.test(r) && /vis=hidden/.test(r)) || "";
      const transportPause = rowsOf(rows, "pause").find((r) => /asked=true/.test(r) && /why=user-transport/.test(r)) || "";
      const lockScreen = rowsOf(rows, "mediaSession").find((r) => /action=pause/.test(r)) || "";
      const stopFields = stops.every((r) =>
        r.includes(`path=${album.file}`) && /vis=hidden/.test(r) && /late=false/.test(r) &&
        /readyState=/.test(r) && /at=/.test(r) && /ended=/.test(r) && /net=/.test(r));
      check("(a) the report names the stop, and the element's state with it",
        stops.length === stagedStops && stopFields, `${stops.length} os-stop row(s), ${stagedStops} staged: ${stops[0] || "(none)"}`);
      check("(a) the stop is the one the element reported with no app cause",
        !!hiddenPause, hiddenPause || `no "pause … asked=false vis=hidden" row in ${rows.length} rows`);
      check("(a) the report names the resume and its outcome",
        resumes.length === 1 && resumes[0].includes(`path=${album.file}`) && /ok=true/.test(resumes[0]),
        `${resumes.length} os-resume row(s): ${resumes[0] || "(none)"}`);
      const laterStops = stops.slice(1).map((r) => rows.findIndex((x) => x === r));
      const iResume = rows.findIndex((r) => r === resumes[0]);
      const iLock = rows.findIndex((r) => r === lockScreen);
      check("(b)(d) the armed stops the reader (or the platform) answered are never resumed",
        stops.length === stagedStops && stagedStops >= 2 && resumes.length === 1 &&
        laterStops.every((i) => i > iResume) && iLock > laterStops[0],
        `the one resume at #${iResume}, the later armed stops at #${laterStops.join(",#")}, the lock-screen pause at #${iLock} (of ${rows.length} rows)`);
      check("the deliberate pauses are still reported as the app's own",
        !!transportPause && !!lockScreen,
        `transport: ${transportPause || "(none)"} | lock screen: ${lockScreen || "(none)"}`);
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
