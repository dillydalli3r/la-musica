#!/usr/bin/env node
/* The rating's READOUT must not move the STARS down a column.
 *
 * The owner rated a track in the album tracklist and the row's stars jumped
 * right: with a web reading the row prints "3.9 Web" beside the stars, and the
 * moment the user has rated nothing… the row prints THEIR number ("5") instead
 * (R359), which is narrower — and because the trailing slot is anchored at the
 * cell's right edge, the shorter readout pushed the stars along with it. Every
 * row of the column then had its stars at a different x, which is exactly what
 * `TrackTitleCell`'s fixed trailing slot exists to prevent.
 *
 * The fix is `webReadout="slot"` on StarRating (see the prop's doc): the
 * readout is drawn inside a box reserved for the widest string the control can
 * print, so the stars keep one x as the row changes between the web reading
 * and the user's own number. This check renders the REAL album page in a real
 * browser, with the app's own API stubbed, over four rows that cover every
 * shape a row can be in — web only, rated, neither, rated-with-no-web — and
 * measures what a reader sees:
 *
 *   * every row's star strip starts at the SAME x (and the reserved slot is
 *     what makes that true: the READOUT contents are asserted to have
 *     different widths, so a control without the reservation cannot pass);
 *   * an INLINE surface keeps its natural width — the album header's own
 *     control, with nothing to read out, is narrower than a row's, i.e. the
 *     reservation did not leak into the surfaces that do not want it.
 *
 * Run:  node tools/check_rating_alignment.mjs
 * Exit codes: 0 pass, 1 a check failed (the failing ones are printed), 2 the
 * environment cannot run it (no web/node_modules, no Playwright browser).
 */
import { existsSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const webDir = path.join(here, "..", "web");
if (!existsSync(path.join(webDir, "node_modules"))) {
  console.error("[rating-align] web/node_modules is missing — run `npm --prefix web install`.");
  process.exit(2);
}
const webRequire = createRequire(path.join(webDir, "package.json"));
let chromium;
try {
  ({ chromium } = webRequire("playwright"));
} catch {
  console.error("[rating-align] Playwright not found — `npm --prefix web i -D playwright`.");
  process.exit(2);
}
const { createServer } = await import(
  `file://${path.join(webDir, "node_modules/vite/dist/node/index.js").replace(/\\/g, "/")}`);

const ALBUM = "F:/Music/Artists/Align Artist/2020 - Align Album";
const WEB_TAGS = { WEBRATING: "78", WEBRATING_SOURCE: "RateYourMusic; MusicBrainz" };

/** One track row of the album payload — the shape tools/check_lyrics_kind.mjs
 *  builds, with the WEBRATING family on the rows that state one. */
const track = (n, title, tags) => ({
  file: `${String(n).padStart(2, "0")} - ${title}.flac`,
  path: `${ALBUM}/${String(n).padStart(2, "0")} - ${title}.flac`,
  tracknumber: n, discnumber: 1, issues: [], values: {}, audit: null,
  log_grade: null, lyrics_embedded: false, lyrics_lrc: false, lyrics_kind: null,
  lyrics_present: false, unreadable: false, grade_pass: true, cover_file: null,
  tech: { length: 200, bitrate: 900, samplerate: 44100, bits: 16, format: "FLAC", channels: 2 },
  tags: {
    TITLE: title, ARTIST: "Align Artist", ALBUMARTIST: "Align Artist",
    ALBUM: "Align Album", DISCNUMBER: "1", MEDIA: "CD", DATE: "2020",
    ...tags,
  },
});

const TRACKS = [
  track(1, "Web Only", WEB_TAGS),          // "3.9 Web" beside the stars
  track(2, "Rated", WEB_TAGS),             // the user's "5" instead
  track(3, "Unrated", {}),                 // nothing to read out
  track(4, "Rated No Web", {}),            // the user's "3", no web reading
];
const ALBUM_PAYLOAD = {
  path: ALBUM, album_artist: "Align Artist",
  meta: { ALBUM: "Align Album", ALBUMARTIST: "Align Artist", ARTIST: "Align Artist", MEDIA: "CD", DATE: "2020" },
  tracks: TRACKS, expected_tracks: [], partial: false, pending: false,
  pending_reason: null, wish: null, wish_id: null, artwork: null, issues: {},
  notes: [], grade_pct: 100, pass: true, pass_count: 4, total_checks: 4,
  track_count: 4, audit_summary: null, cover_file: null, has_log: false,
  has_cue: false, checksum_status: "", accuraterip_status: "", lyrics_present: 0,
  lyrics_expected: 0, instrumental_count: 0, media: "CD", source_summary: null,
};
// The store's own units (0-10, RATING / 10): 10 -> five stars, 6 -> three.
const RATINGS = { [TRACKS[1].path]: 10, [TRACKS[3].path]: 6 };

// Tailwind's `content` globs (`./index.html`, `./src/**`) are resolved against
// the PROCESS cwd, and the app's own entry points (`npm run build`, `vite`)
// run from web/. Without this the dev server emits no utility classes at all
// (it says so: "The `content` option ... is missing or empty") and every
// measurement below would be of an unstyled page.
process.chdir(webDir);

const vite = await createServer({
  configFile: path.join(webDir, "vite.config.ts"), root: webDir, logLevel: "error",
  server: { host: "127.0.0.1", port: 8011, strictPort: false },
});
await vite.listen();
const port = vite.httpServer.address().port;

const failures = [];
let checks = 0;
const check = (name, pass, detail = "") => {
  checks += 1;
  console.log(`${pass ? "  ok  " : "  FAIL"} ${name}${pass || !detail ? "" : " — " + detail}`);
  if (!pass) failures.push(name);
};

const browser = await chromium.launch();
try {
  const context = await browser.newContext({ serviceWorkers: "block" });
  const page = await context.newPage();
  await page.route("**/sw.js", (r) => r.abort());
  await page.route("**/api/**", (route) => {
    const url = route.request().url();
    const send = (body) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    if (url.includes("/api/auth/status")) return send({ required: false, authenticated: true });
    if (url.includes("/api/config")) return send({ music_folder: "F:/Music", first_run_done: true });
    if (url.includes("/api/ratings")) return send({ scope: "track", ratings: RATINGS, counts: {} });
    if (url.includes("/api/album")) return send(ALBUM_PAYLOAD);
    return undefined;               // everything else stays pending
  });

  await page.goto(`http://127.0.0.1:${port}/album/${encodeURIComponent(ALBUM)}`);
  await page.waitForSelector("table tbody tr [role='group']", { timeout: 20000 });
  await page.waitForTimeout(400);

  const all = await page.$$eval("[role='group']", (nodes) => nodes.map((el) => {
    const box = el.getBoundingClientRect();
    const readout = el.lastElementChild;
    const stars = [...el.children].filter((c) => c !== readout);
    const first = stars.length ? stars[0].getBoundingClientRect() : null;
    const last = stars.length ? stars[stars.length - 1].getBoundingClientRect() : null;
    // The WORDS' own width, not the reserved box's: the readout's inner span
    // carries only what is printed (the sizer is its sibling), so a Range over
    // it measures the text exactly as the reader sees it.
    const content = readout ? readout.lastElementChild : null;
    let contentW = 0;
    if (content) {
      const range = document.createRange();
      range.selectNodeContents(content);
      contentW = range.getBoundingClientRect().width;
    }
    return {
      inRow: !!el.closest("table tbody tr"),
      x: box.x, w: box.width,
      text: content ? (content.textContent || "").trim() : "",
      contentW,
      starsW: first && last ? last.right - first.left : 0,
    };
  }));
  const rows = all.filter((r) => r.inRow);
  const inline = all.filter((r) => !r.inRow);
  check("all four rating rows render", rows.length === 4, JSON.stringify(all.map((r) => r.text)));
  check("the stars really carry their size (the CSS loaded)", rows.every((r) => r.starsW > 60),
        `star strip widths=${rows.map((r) => r.starsW.toFixed(1))}`);
  console.log("    rows:", JSON.stringify(rows.map((r) => ({
    text: r.text, x: Math.round(r.x), control: Math.round(r.w),
    stars: Math.round(r.starsW), words: Math.round(r.contentW),
  }))));

  // The readouts really do differ in width — the condition that made the stars
  // drift. Without this the alignment below would prove nothing.
  const words = rows.map((r) => r.contentW);
  check("the readouts differ in width (the drift this check is about exists)",
        Math.max(...words) - Math.min(...words) > 12,
        `word widths=${words.map((w) => Math.round(w))}`);
  check("the four rows really print the shapes (web / the user's number / nothing)",
        rows.some((r) => /Web/.test(r.text)) && rows.some((r) => r.text === "5") && rows.some((r) => r.text === ""),
        JSON.stringify(rows.map((r) => r.text)));

  const xs = rows.map((r) => r.x);
  const spread = Math.max(...xs) - Math.min(...xs);
  check("every row's stars start at the same x", spread <= 0.75,
        `spread=${spread.toFixed(2)}px xs=${xs.map((x) => x.toFixed(1))}`);
  const ws = rows.map((r) => r.w);
  check("every row's control is the same width", Math.max(...ws) - Math.min(...ws) <= 0.75,
        `widths=${ws.map((w) => w.toFixed(1))}`);
  const slots = rows.map((r) => r.w - r.starsW);
  check("…because every row reserves the same box after its stars",
        Math.max(...slots) - Math.min(...slots) <= 0.75 && Math.min(...slots) >= 35,
        `reserved=${slots.map((v) => v.toFixed(1))}`);

  // The inline surface keeps its natural width: the album header's control has
  // no album rating and no ALBUMWEBRATING on any file, so it reads out "—" —
  // a few px, not a reserved box. A reservation that leaked into `text` mode
  // would give it the same box as a row.
  check("exactly one inline (album header) control", inline.length === 1, String(inline.length));
  check("the inline album control keeps its natural width",
        inline.length === 1 && inline[0].contentW < 20 && inline[0].w - inline[0].starsW < 30,
        `inline words=${inline.map((r) => Math.round(r.contentW))} reserved=${inline.map((r) => Math.round(r.w - r.starsW))}`);
} finally {
  await browser.close();
  await vite.close();
}
console.log(failures.length ? `\nFAIL — ${checks} checked, ${failures.length} failed` : `\nPASS — ${checks} checks`);
process.exit(failures.length ? 1 : 0);
