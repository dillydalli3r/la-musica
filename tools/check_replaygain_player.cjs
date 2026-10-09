#!/usr/bin/env node
/* ReplayGain in the PLAYER: the dB the bar reports must be the gain the
 * decoder is actually running at, on every surface the app plays through.
 *
 * Three failures this pins, all of which the UI used to hide:
 *   1. an untagged track answered unity (the backend's on-demand measurement is
 *      bounded to PLAYBACK_WAIT_S, so it cannot block the click) and NOTHING
 *      re-applied the value when the decode finished: the track played its
 *      whole length unnormalised. The gain must land on the playing element
 *      within seconds of the measurement completing.
 *   2. a music video got no gain at all while the bar printed "RG -x dB" for
 *      it. Either it carries the gain (this is what the code now claims) or the
 *      readout must not name one — the check asserts both sides agree.
 *   3. album mode on an album with no REPLAYGAIN_ALBUM_GAIN degraded to per
 *      track normalisation silently. The readout has to say which value it
 *      used.
 *
 * The gain is read off the real WebAudio graph the player builds
 * (`el.__mloAnalyser.chain.gain.gain.value`), not off the readout: a readout
 * that agrees with itself proves nothing.
 *
 * Needs a live backend serving the built app (`web/dist`):
 *   npm --prefix web run build
 *   python -m uvicorn server.main:app --host 127.0.0.1 --port 8000
 *   node tools/check_replaygain_player.cjs http://127.0.0.1:8000
 *
 * `/api/replaygain` is STUBBED (a pending answer that later becomes a real
 * one) so the late-apply path is exercised deterministically instead of
 * depending on how fast this machine decodes a file. Playwright is required
 * (same resolution as tools/shot.cjs): set PLAYWRIGHT to a module path, or
 * install it. Exit 2 when it is missing. */

let chromium;
try {
  ({ chromium } = require(process.env.PLAYWRIGHT || "playwright"));
} catch {
  console.error("[rg] Playwright not found — `npm i -D playwright`, " +
    "or point PLAYWRIGHT at an installed module.");
  process.exit(2);
}

const BASE = process.argv[2] || process.env.BASE || "http://127.0.0.1:8000";
const GAIN_DB = -7.2;
const GAIN_LINEAR = Math.pow(10, GAIN_DB / 20); // 0.4365
const results = [];
const check = (name, pass, detail) => results.push({ name, pass: !!pass, detail: String(detail) });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Every decoder the page holds, with the gain its WebAudio stage is at. */
const readDecoders = (page) => page.evaluate(() => {
  const out = [];
  for (const el of document.querySelectorAll("audio, video")) {
    const graph = el.__mloAnalyser;
    out.push({
      tag: el.tagName.toLowerCase(),
      file: decodeURIComponent(el.currentSrc || el.src || "").split("?")[0].split("/").pop(),
      origin: el.crossOrigin,
      paused: el.paused,
      hasGraph: !!graph,
      gain: graph ? Number(graph.chain.gain.gain.value) : null,
    });
  }
  return out;
});

/** The decoder that is playing (the one the report is about). */
const playing = (list) => list.find((d) => !d.paused && d.file) || null;

/** The bar's gain readout, as the user sees it (chip text + its tooltip). */
const readout = (page) => page.evaluate(() => {
  const chip = [...document.querySelectorAll("[title]")]
    .find((e) => /^ReplayGain /.test(e.getAttribute("title") || ""));
  return chip ? { text: chip.textContent.trim(), title: chip.getAttribute("title") } : null;
});

/** Poll until `fn` returns true, or give up. */
async function until(fn, ms = 10000, step = 250) {
  const end = Date.now() + ms;
  for (;;) {
    if (await fn()) return true;
    if (Date.now() > end) return false;
    await sleep(step);
  }
}

/** A 5 s silent WAV — the stand-in for a music video's bytes. Chromium plays
 *  audio-only media in a <video> element, which is all this check needs. */
function silentWav(seconds = 5, rate = 8000) {
  const n = seconds * rate;
  const buf = Buffer.alloc(44 + n * 2);
  buf.write("RIFF", 0);
  buf.writeUInt32LE(36 + n * 2, 4);
  buf.write("WAVEfmt ", 8);
  buf.writeUInt32LE(16, 16);
  buf.writeUInt16LE(1, 20);
  buf.writeUInt16LE(1, 22);
  buf.writeUInt32LE(rate, 24);
  buf.writeUInt32LE(rate * 2, 28);
  buf.writeUInt16LE(2, 32);
  buf.writeUInt16LE(16, 34);
  buf.write("data", 36);
  buf.writeUInt32LE(n * 2, 40);
  return buf;
}

const VIDEO_PATH = "/music/Artists/Test Artefact/[Album] Playback/test video.mp4";
const videoTrack = {
  path: VIDEO_PATH,
  file: "test video.mp4",
  title: "Test video",
  duration: 5,
  track: 1,
};

/** Swap one album's track list for a single synthesized VIDEO track, in both
 *  payloads the album page reads. The fixture library has no video, and the
 *  video branch is exactly the claim being pinned here. The injected track is a
 *  CLONE of a real one (the row renders the tags/tech a payload carries, so a
 *  hand-built object crashes the page) with only its path/file/title pointing
 *  at a video. */
async function stubVideoLibrary(page) {
  await page.route(/\/api\/album(\?|$)/, async (route) => {
    const res = await route.fetch();
    const payload = await res.json();
    const real = (payload.tracks || [])[0] || {};
    payload.tracks = [{ ...real, path: VIDEO_PATH, file: videoTrack.file, title: videoTrack.title }];
    route.fulfill({ json: payload });
  });
  await page.route(/\/api\/videos\/stream/, (route) => route.fulfill({
    status: 200,
    headers: { "Content-Type": "audio/wav", "Accept-Ranges": "bytes" },
    body: silentWav(),
  }));
  await page.route(/\/api\/videos\/meta/, (route) => route.fulfill({
    json: { native: true, reason: null, duration: 5, video_codec: null, audio_codecs: ["pcm_s16le"] },
  }));
  await page.route(/\/api\/videos\/subtitles/, (route) => route.fulfill({
    json: { muxed: [], sidecars: [] },
  }));
}

(async () => {
  let browser;
  const errs = [];
  const getJson = async (path) => {
    const res = await fetch(`${BASE}${path}`);
    return res.json();
  };
  try {
    const cfg = await getJson("/api/config");
    const conf = cfg.config ?? cfg;
    check("the fixture plays in ReplayGain track mode with on-demand measurement on",
      conf.replaygain_mode === "track" && conf.replaygain_analyze_missing === true,
      `mode=${conf.replaygain_mode} analyze_missing=${conf.replaygain_analyze_missing}`);

    browser = await chromium.launch({
      executablePath: process.env.CHROME,
      headless: true,
      // headless Chrome blocks a play() that is not gesture-backed, and the app
      // starts tracks from a promise chain.
      args: ["--autoplay-policy=no-user-gesture-required"],
    });
    const page = await browser.newContext({
      viewport: { width: 1440, height: 900 },
      // The app's service worker would answer the stubbed requests from its own
      // cache; nothing here needs offline support.
      serviceWorkers: "block",
    }).then((c) => c.newPage());
    page.setDefaultTimeout(20000);
    page.on("pageerror", (e) => errs.push(e.message));

    // ---- the stubbed gain endpoint -----------------------------------------
    // First answer: "still measuring" (unity, pending). After `rgLanded`, the
    // real value — which is what the player must apply to the running track.
    let rgLanded = false;
    let rgCalls = 0;
    await page.route(/\/api\/replaygain/, (route) => {
      rgCalls++;
      const path = new URL(route.request().url()).searchParams.get("path") || "";
      const body = rgLanded
        ? { path, gain: GAIN_DB, peak: 1.0, mode: "track", source: "ffmpeg", analyzed: true, pending: false, album: false }
        : { path, gain: null, peak: null, mode: "track", source: null, analyzed: false, pending: true, album: false };
      return route.fulfill({ json: body });
    });

    // ---- 1. an untagged track: unity now, normalised seconds later ---------
    const lib = await getJson("/api/library");
    let album = null;
    for (const a of lib.artists || []) {
      for (const al of a.albums || []) if ((al.tracks || []).length) { album = al.path; break; }
      if (album) break;
    }
    check("the fixture has an album to play", !!album, JSON.stringify(album));

    await page.goto(`${BASE}/album/${encodeURIComponent(album)}`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector('tr[title="Click to play"]');
    await page.locator('tr[title="Click to play"]').first().locator("td").first().click();

    const started = await until(async () => !!playing(await readDecoders(page)), 10000);
    const atStart = await readDecoders(page);
    const liveStart = playing(atStart);
    check("the track starts playing", started && !!liveStart, JSON.stringify(atStart));
    check("its audio rides the player's gain stage", !!liveStart?.hasGraph, JSON.stringify(liveStart));
    check("a bounded-pending answer starts it at unity (held, not guessed)",
      !!liveStart && Math.abs(liveStart.gain - 1) < 0.01, `gain=${liveStart?.gain}`);
    check("nothing is claimed in the readout while no gain is known",
      (await readout(page)) === null, JSON.stringify(await readout(page)));

    // The measurement "finishes" — the client re-asks and must re-apply.
    rgLanded = true;
    const landed = await until(async () => {
      const d = playing(await readDecoders(page));
      return !!d && Math.abs(d.gain - GAIN_LINEAR) < 0.02;
    }, 12000);
    const afterLand = await readDecoders(page);
    const liveAfter = playing(afterLand);
    check("the late value is applied to the PLAYING track",
      landed && !!liveAfter && Math.abs(liveAfter.gain - GAIN_LINEAR) < 0.02,
      `gain=${liveAfter?.gain} want=${GAIN_LINEAR.toFixed(4)} calls=${rgCalls}`);
    check("it took a re-ask, not the first (bounded) answer",
      rgCalls > 1, `replaygain requests=${rgCalls}`);
    const chip = await readout(page);
    check("and the readout then agrees with the graph",
      !!chip && /-7\.2 dB/.test(chip.title) && /measured on demand/.test(chip.title),
      JSON.stringify(chip));
    check("the track is still playing while the gain lands",
      !!liveAfter && liveAfter.tag === "audio" && (await readDecoders(page)).some((d) => !d.paused),
      JSON.stringify(afterLand));

    // ---- 2. a music video: the readout may only claim a gain it has --------
    await stubVideoLibrary(page);
    await page.goto(`${BASE}/album/${encodeURIComponent(album)}`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector('tr[title="Click to play"]');
    await page.locator('tr[title="Click to play"]').first().locator("td").first().click();

    const videoUp = await until(async () => {
      const list = await readDecoders(page);
      return list.some((d) => d.tag === "video" && d.file);
    }, 15000);
    const vList = await readDecoders(page);
    const video = vList.find((d) => d.tag === "video" && d.file);
    check("the music video plays through the popout <video>", videoUp && !!video, JSON.stringify(vList));
    check("the video element is CORS-anonymous (the graph requires it cross-origin)",
      video?.origin === "anonymous", `crossOrigin=${video?.origin}`);
    check("the video's audio rides the same gain stage", !!video?.hasGraph, JSON.stringify(video));

    const videoLanded = await until(async () => {
      const d = (await readDecoders(page)).find((x) => x.tag === "video" && x.file);
      return !!d && d.hasGraph && Math.abs(d.gain - GAIN_LINEAR) < 0.02;
    }, 12000);
    const vFinal = (await readDecoders(page)).find((d) => d.tag === "video" && d.file);
    const vChip = await readout(page);
    check("the video carries the ReplayGain value",
      videoLanded && Math.abs(vFinal?.gain - GAIN_LINEAR) < 0.02,
      `gain=${vFinal?.gain} want=${GAIN_LINEAR.toFixed(4)}`);
    check("so the bar's claim about the video is true, not a readout with no gain",
      !!vChip && /-7\.2 dB/.test(vChip.title) && !!vFinal?.hasGraph && Math.abs(vFinal?.gain - GAIN_LINEAR) < 0.02,
      `${JSON.stringify(vChip)} graph=${vFinal?.hasGraph} gain=${vFinal?.gain}`);

    // ---- 3. album mode on an album with no album gain ----------------------
    // The mode comes from the config, so the stubbed config says "album" and
    // the stubbed gain says the value it returned is NOT the album's.
    await page.route(/\/api\/config/, async (route) => {
      const res = await route.fetch();
      route.fulfill({ json: { ...(await res.json()), replaygain_mode: "album" } });
    });
    await page.route(/\/api\/replaygain/, (route) => {
      const path = new URL(route.request().url()).searchParams.get("path") || "";
      return route.fulfill({
        json: { path, gain: GAIN_DB, peak: 1.0, mode: "album", source: "tags", analyzed: false, pending: false, album: false },
      });
    });
    await page.goto(`${BASE}/album/${encodeURIComponent(album)}`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector('tr[title="Click to play"]');
    await page.locator('tr[title="Click to play"]').first().locator("td").first().click();
    const albumChip = await until(async () => {
      const c = await readout(page);
      return !!c && /album gain/.test(c.title);
    }, 12000);
    const c = await readout(page);
    check("album mode with no album gain says the track value was used",
      albumChip && !!c && /-7\.2 dB \(album\)/.test(c.title) && /no album gain, so its track value was used/.test(c.title),
      JSON.stringify(c));

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
