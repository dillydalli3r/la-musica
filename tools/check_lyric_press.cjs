#!/usr/bin/env node
/* A press on a lyric line CARRIES the reader: the pane glides, the emphasis
 * lands with the press.
 *
 * The bug this pins (owner-reported): pressing a line teleported the pane to
 * it and the emphasis cut over a frame later, which read as a flash —
 * "when i skip to a lyric it kinda teleports to it … for like one frame it
 * shows the previous line then cuts to the next one very quickly and looks
 * jarring". The old behaviour was deliberate on the pane's half (`centerLine`
 * passed "snap", and the seek the press causes re-snapped it through
 * `markJump`), and the emphasis lag came from the 60 fps clock: the pressed
 * line only became active on the NEXT frame's read of the audio element.
 *
 * Run:  node tools/check_lyric_press.cjs [baseUrl]      (a RUNNING scratch app)
 *
 * What is measured, against the app's own DOM:
 *
 *   * the pane MOVES over several frames instead of arriving in one — a
 *     teleport is one frame with a new scrollTop, a glide is a run of them;
 *   * the clicked line is the emphasised one within a frame or two of the
 *     press (the surfaces seed the 60 fps clock with the target time);
 *   * the pane really arrives: the pressed line ends up on the pane's anchor
 *     line, so "carries the reader" cannot be satisfied by moving somewhere.
 *
 * The seek still snaps for a SCRUB (the scrub bar and the offset / zoom
 * controls go through `jump`), which `tools/check_lyrscroll.cjs` pins at the
 * module level: the glider's `snap`, `lyricMove`'s rule, and the retarget that
 * a jump cancels.
 *
 * Point it at a scratch library (`MLO_MUSIC_FOLDER`), never at the library you
 * listen to: it plays a track. Ports 8011 and up — never the owner's 8000.
 */
let chromium;
try {
  ({ chromium } = require(process.env.PLAYWRIGHT || "playwright"));
} catch (e) {
  console.error("[check_lyric_press] Playwright not found — install it with " +
    "`npm i -D playwright` (or set PLAYWRIGHT=/path/to/playwright).");
  process.exit(2);
}

const ARGS = process.argv.slice(2);
const BASE = ARGS.find((a) => !a.startsWith("--")) || process.env.BASE || "http://127.0.0.1:8011";
const ROWS_BELOW = 12;          // far enough that a teleport is unmistakable
const TRACE_MS = 1600;

let failures = 0;
const check = (name, ok, detail) => {
  console.log(`  ${ok ? "ok  " : "FAIL"} ${name}${detail ? ` — ${detail}` : ""}`);
  if (!ok) failures++;
};

(async () => {
  const lib = await (await fetch(`${BASE}/api/library`)).json();
  const tracks = (lib.artists || []).flatMap((a) => a.albums.flatMap((al) => al.tracks || []));
  if (!tracks.length) {
    console.error("[check_lyric_press] the library has no tracks — point it at a scratch library");
    process.exit(2);
  }
  // A track whose lyrics are SYNCED (a line list to press on): ask /api/tags
  // until one answers with [mm:ss] text.
  let track = null;
  for (const t of tracks.slice(0, 20)) {
    const tags = await (await fetch(`${BASE}/api/tags?path=${encodeURIComponent(t.path)}`)).json();
    if (typeof tags.lyrics === "string" && /\[\d{1,2}:\d{1,2}/.test(tags.lyrics)) { track = t; break; }
  }
  if (!track) {
    console.error("[check_lyric_press] no track with synced lyrics in this library");
    process.exit(2);
  }
  console.log(`[check_lyric_press] track: ${track.file}`);

  const browser = await chromium.launch({ headless: true, args: ["--autoplay-policy=no-user-gesture-required"] });
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 900 } });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => console.log("  PAGE_ERR:", e.message));
  // The album the track sits on, from the same payload: its page is what plays
  // a track (the row click starts the queue at that track).
  const album = (lib.artists || [])
    .flatMap((a) => a.albums || [])
    .find((al) => (al.tracks || []).some((t) => t.path === track.path));
  await page.goto(`${BASE}/album/${encodeURIComponent(album?.path ?? track.path)}`, { waitUntil: "networkidle", timeout: 60000 });
  await page.waitForSelector("main table tbody tr", { timeout: 30000 });
  await page.waitForTimeout(1200);
  const title = track.tags?.TITLE || track.file;
  await page.locator("main table tbody tr", { hasText: title.slice(0, 18) }).first()
    .locator("td").first().click({ timeout: 20000 });
  await page.waitForTimeout(2500);
  await page.click('button[title="Fullscreen player with lyrics"]', { timeout: 20000 });
  await page.waitForTimeout(2500);

  const paneReady = await page.evaluate(() => {
    const pane = [...document.querySelectorAll("div")].find((d) => {
      const s = getComputedStyle(d);
      return s.overflowY === "auto" && d.scrollHeight > d.clientHeight + 40;
    });
    if (!pane) return { err: "no pane" };
    const rows = [...pane.children].filter((c) => c.querySelector?.(":scope > div"));
    window.__pane = pane;
    window.__rows = rows;
    window.__active = () => rows.findIndex((r) => r.querySelector(":scope > div")?.style.transform === "scale(1)");
    return { rows: rows.length, active: window.__active(), top: Math.round(pane.scrollTop) };
  });
  if (paneReady.err || paneReady.rows < ROWS_BELOW + 4) {
    console.error(`[check_lyric_press] no usable lyric pane (${paneReady.err || `${paneReady.rows} rows`})`);
    await browser.close();
    process.exit(2);
  }
  console.log(`[check_lyric_press] pane: ${paneReady.rows} rows, line ${paneReady.active} sung`);

  const trace = await page.evaluate(({ below, ms }) =>
    new Promise((res) => {
      const rows = window.__rows;
      const from = window.__active();
      const target = Math.min(rows.length - 2, (from < 0 ? 0 : from) + below);
      const startTop = Math.round(window.__pane.scrollTop);
      const out = [];
      rows[target].dispatchEvent(new MouseEvent("click", { bubbles: true }));
      const t0 = performance.now();
      let closest = Infinity;
      const centreFromAnchor = () => {
        const r = rows[target].getBoundingClientRect();
        const pr = window.__pane.getBoundingClientRect();
        return Math.abs((r.top + r.height / 2 - pr.top) - pr.height * 0.33);
      };
      const tick = () => {
        closest = Math.min(closest, centreFromAnchor());
        out.push([Math.round(performance.now() - t0), Math.round(window.__pane.scrollTop), window.__active()]);
        if (performance.now() - t0 < ms) return requestAnimationFrame(tick);
        // The glide's own ease is frame-limited (a starved pane advances it at
        // the clamped 64 ms step), and the song keeps playing underneath — the
        // NEXT line's change starts a new glide once it arrives. So what is
        // measured is the closest approach to the anchor lane over the whole
        // move: a pane that never gets there fails, and a pane still chasing
        // the current line afterwards cannot make it pass.
        res({ target, startTop, out, closest: Math.round(closest), paneH: Math.round(window.__pane.getBoundingClientRect().height) });
      };
      requestAnimationFrame(tick);
    }), { below: ROWS_BELOW, ms: TRACE_MS });

  const frames = trace.out;
  const anchor = Math.round(trace.paneH * 0.33);
  // The move the PRESS made, frame by frame — `startTop` included, because a
  // teleport writes its new position before the first traced frame is taken.
  const tops = [trace.startTop, ...frames.map((f) => f[1])];
  const total = Math.abs(tops[tops.length - 1] - tops[0]);
  const biggest = Math.max(...tops.slice(1).map((t, i) => Math.abs(t - tops[i])));
  const emphasisAt = frames.findIndex((f) => f[2] === trace.target);

  console.log(`  pressed line ${trace.target}; ${frames.length} frames traced, ` +
    `moving ${total} px, biggest single step ${biggest} px`);
  check("the pane carries the reader there instead of teleporting",
        total >= 200 && biggest <= total * 0.8,
        `biggest step is ${Math.round((biggest / (total || 1)) * 100)}% of the ${total} px move`);
  check("the emphasis lands with the press", emphasisAt >= 0 && emphasisAt <= 1,
        `emphasised on frame ${emphasisAt}`);
  check("and the pressed line really lands on the pane's anchor line", trace.closest <= 12,
        `closest approach ${trace.closest} px from the ${anchor} px lane`);

  await browser.close();
  console.log(failures ? `\n[check_lyric_press] ${failures} check(s) FAILED` : "\nall lyric-press checks passed");
  process.exit(failures ? 1 : 0);
})();
