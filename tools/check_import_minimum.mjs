#!/usr/bin/env node
/* The import wizard's minimum entry mode, rendered for real.
 *
 * A prompt's own link is `/import?album=…&step=Covers&missing=cover,advisory`
 * (mlo.import_policy.wizard_link): the user is asked for what the import could
 * NOT decide, not for the whole step's form. This renders the ACTUAL page
 * component with that URL — through Vite, in memory, no build output — and
 * asserts what a user sees:
 *
 *   * on the step that answers a missing family (Covers, Lyrics, Advisory) the
 *     family's own controls are out in the open and the step's other blocks sit
 *     in ONE closed "show everything else on this step" disclosure;
 *   * the unrelated families' controls (Links, Genres) are in none of it;
 *   * the banner still names every missing family, so the other one is a click
 *     away;
 *   * the ordinary wizard (no `?missing=`) renders the same step in full — no
 *     disclosure at all, which is the no-regression half;
 *   * a step with nothing missing says so instead of showing a form;
 *   * the Finish step's script boxes ARE the import chain the server previews
 *     (`GET /api/import/scripts/preview`): ticked on the chain's own ids in the
 *     chain's own order, never on the library-wide Run All order.
 *
 * The second half loads the rendered markup in a real browser and clicks the
 * affordance: the collapsed blocks are invisible until then, visible after, and
 * the family's controls come BEFORE the disclosure, whichever order the source
 * is in.
 *
 * Run:  node tools/check_import_minimum.mjs
 * Exit codes: 0 pass, 1 a check failed (the failing ones are printed), 2 the
 * environment cannot run it (no web/node_modules).
 */
import { existsSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const webDir = path.join(here, "..", "web");
if (!existsSync(path.join(webDir, "node_modules"))) {
  console.error("[minimum] web/node_modules is missing — run `npm --prefix web install`.");
  process.exit(2);
}

// Loaded FROM web/ so the harness and the page share ONE React instance: the
// page's own imports are externalized by Vite and resolve to the project's
// own node_modules, and Node caches a module by resolved path — two copies of
// React would break every hook in the page.
const webRequire = createRequire(path.join(webDir, "package.json"));
const { createServer } = await import(
  `file://${path.join(webDir, "node_modules/vite/dist/node/index.js").replace(/\\/g, "/")}`);
const React = webRequire("react");
const { renderToString } = webRequire("react-dom/server");
const { MemoryRouter } = webRequire("react-router-dom");
// react-query is imported as the ESM file the PAGE's own import resolves to
// (package exports: "import" -> build/modern/index.js, "require" ->
// build/modern/index.cjs — two files, i.e. two providers, and the page's
// useQuery would find no client). Same URL, same module instance.
const { QueryClient, QueryClientProvider } = await import(pathToFileURL(
  path.join(webDir, "node_modules/@tanstack/react-query/build/modern/index.js")).href);

// The page is a browser app: its store reaches for localStorage at import time.
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
  clear: () => store.clear(),
};
globalThis.window = globalThis;
// renderToString runs no effects, so nothing here may depend on a request
// landing: every query the wizard fires is answered from the cache below, and
// anything it has not cached stays PENDING forever instead of reaching for the
// network (a relative URL has no meaning outside the browser anyway).
globalThis.fetch = () => new Promise(() => {});

const ALBUM = "F:/Music/Artists/Daft Punk - Homework";
const TRACKS = [
  { file: "01 - Daftendirekt.flac", path: `${ALBUM}/01 - Daftendirekt.flac`,
    tracknumber: 1, discnumber: 1, issues: [], values: {}, audit: null, log_grade: null,
    lyrics_embedded: false, lyrics_lrc: false, unreadable: false, tech: { length: 195 },
    tags: { TITLE: "Daftendirekt", ARTIST: "Daft Punk", ALBUM: "Homework", DISCNUMBER: "1" },
    grade_pass: false, lyrics_present: false },
  { file: "02 - WDPK 83.7 FM.flac", path: `${ALBUM}/02 - WDPK 83.7 FM.flac`,
    tracknumber: 2, discnumber: 1, issues: [], values: {}, audit: null, log_grade: null,
    lyrics_embedded: false, lyrics_lrc: false, unreadable: false, tech: { length: 28 },
    tags: { TITLE: "WDPK 83.7 FM", ARTIST: "Daft Punk", ALBUM: "Homework", DISCNUMBER: "1" },
    grade_pass: false, lyrics_present: false },
];
const LIBRARY = { artists: [{ name: "Daft Punk", albums: [{ path: ALBUM, tracks: TRACKS }] }] };
const CONFIG = { music_folder: "F:/Music", mb_genre_count: 2, manual_import_enabled: true,
                 auto_acquisition_enabled: true };

const server = await createServer({
  configFile: path.join(webDir, "vite.config.ts"),
  root: webDir,
  server: { middlewareMode: true },
  appType: "custom",
  logLevel: "error",
});

const failures = [];
let checks = 0;
const check = (name, pass, detail = "") => {
  checks += 1;
  if (!pass) failures.push(detail ? `${name} — ${detail}` : name);
};

/** The rendered text as a reader would see it: React's text-node markers and
 *  the HTML entities (an `&` in a label is `&amp;` in the markup) removed. */
const flatten = (html) => html.replace(/<!-- -->/g, "")
  .replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/&#x27;|&#39;/g, "'")
  .replace(/&lt;/g, "<").replace(/&gt;/g, ">")
  .replace(/\s+/g, " ");

/** The wizard at one URL, with the library and the config it reads. `extra`
 *  seeds any further query the step under test reads by key (the Finish step
 *  reads the import chain preview). */
async function render(url, config = CONFIG, extra = {}) {
  const page = await server.ssrLoadModule("/src/pages/ImportWizard.tsx");
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(["config"], config);
  qc.setQueryData(["library"], LIBRARY);
  for (const [key, data] of Object.entries(extra)) qc.setQueryData([key], data);
  const html = renderToString(
    React.createElement(QueryClientProvider, { client: qc },
      React.createElement(MemoryRouter, { initialEntries: [url] },
        React.createElement(page.default))));
  // React separates adjacent text nodes with <!-- --> markers; they are not
  // content, so every assertion reads the flattened text.
  const text = flatten(html);
  // What a user can see and what the ONE disclosure holds: a closed <details>
  // hides its content (the browser's own rule — the second half of this check
  // clicks it and watches the blocks appear).
  const cut = text.indexOf("<details");
  if (cut === -1) return { open: text, collapsed: "", html: text, blocks: 0 };
  const end = text.indexOf("</details>") + "</details>".length;
  return { open: text.slice(0, cut) + text.slice(end), collapsed: text.slice(cut, end),
           html: text, blocks: 1 };
}

/** Every one of `want` is in `where` (one check per string). */
function has(where, want, label) {
  for (const text of want) check(`${label}: ${text}`, where.includes(text));
}

function lacks(where, unwanted, label) {
  for (const text of unwanted) check(`${label}: no ${text}`, !where.includes(text));
}

// --------------------------------------------------------------------------- #
// 1. the prompt's own link, landing on the step that answers the FIRST gap
// --------------------------------------------------------------------------- #
const link = (step, missing) =>
  `/import?album=${encodeURIComponent(ALBUM)}&step=${step}&missing=${missing}`;

const covers = await render(link("Covers", "cover,advisory"));

// The cover controls ARE the visible step: what the user was sent here to
// decide, and the button that finishes the step with it.
has(covers.open, ["Current album cover", "MusicBrainz release-group cover",
                  "Album cover from URL", "Per-track covers",
                  "Daftendirekt", "WDPK 83.7 FM", "Continue to genres"],
    "minimum covers step shows the cover controls");
// Everything else the covers step carries waits behind the one affordance.
has(covers.collapsed, ["Show everything else on this step", "Artist & album metadata",
                       "Fetch missing"],
    "minimum covers step collapses the rest of the step");
lacks(covers.open, ["Artist & album metadata", "Fetch missing"],
      "minimum covers step hides the rest");
// No step anywhere in the wizard is the other families' business: the families
// this link does not name have no controls among them.
lacks(covers.html, ["Genres from MusicBrainz", "Save genres", "Auto-import lyrics",
                    "Save lyrics & instrumental", "RateYourMusic links (optional)",
                    "Fetch release & auto-match", "Set iTunes advisory per track"],
      "a cover+advisory visit renders no other family's step");
// Both missing families are named, and the one this step does not answer is one
// click away.
has(covers.open, ["Cover art is still open for this album",
                  "Still missing: Cover art, Advisory",
                  "Advisory"],
    "the banner keeps the whole missing list and the step says which family is open");

// --------------------------------------------------------------------------- #
// 2. the other missing family's own step, from the same link
// --------------------------------------------------------------------------- #
const advisory = await render(link("Advisory", "cover,advisory"));
has(advisory.open, ["Save advisory", "0 · clean", "1 · explicit", "2 · safe",
                    "Daftendirekt", "WDPK 83.7 FM"],
    "minimum advisory step shows the per-track controls");
has(advisory.collapsed, ["Show everything else on this step",
                         "Set iTunes advisory per track", "Apply to all tracks",
                         "Auto-import advisory for all tracks"],
    "minimum advisory step collapses the album-wide helpers");
lacks(advisory.open, ["Apply to all tracks", "Auto-import advisory for all tracks"],
      "minimum advisory step hides the helpers");
// ONE advisory action, and it is the one that asks the sources anyway (the
// "Re-rate…" button beside it is gone: a second, gentler entry would be the one
// that leaves a value an earlier run invented standing). The button's own
// title says so — the re-rate is what this step's button DOES now.
lacks(advisory.collapsed.concat(advisory.open), ["Re-rate"],
      "the advisory step offers ONE advisory action");
lacks(advisory.collapsed, ["already carries a value keeps it", "use Re-rate"],
      "and its title does not promise the fill-only behaviour it no longer has");
has(advisory.open, ["Still missing: Cover art, Advisory"],
    "the banner names both families on the advisory step too");

// --------------------------------------------------------------------------- #
// 3. a step with nothing missing says so, and the ordinary wizard is untouched
// --------------------------------------------------------------------------- #
const nothing = await render(link("Lyrics", "advisory"));
check("a step with nothing missing says so",
      nothing.open.includes("Nothing is missing on this step for this album"),
      "the plain words are missing");
check("a step with nothing missing hides nothing", nothing.blocks === 0,
      "a disclosure appeared on a step with nothing missing");
has(nothing.open, ["Save lyrics & instrumental", "Auto-import lyrics", "WDPK 83.7 FM"],
    "a step with nothing missing still renders its own controls");
has(nothing.open, ["Still missing: Advisory"],
    "the banner still names the family that IS missing");

const ordinary = await render(`/import?album=${encodeURIComponent(ALBUM)}&step=Advisory`);
check("the ordinary wizard has no disclosure", ordinary.blocks === 0,
      "a disclosure appeared without ?missing=");
has(ordinary.html, ["Set iTunes advisory per track", "Apply to all tracks",
                    "Auto-import advisory for all tracks", "Save advisory"],
    "the ordinary wizard still renders the whole step");
lacks(ordinary.html, ["Nothing is missing on this step"], "the ordinary wizard narrates nothing");
lacks(ordinary.html, ["Re-rate"], "and offers the same ONE advisory action");

// --------------------------------------------------------------------------- #
// 3b. the switch: manual importing off offers no path at all
// --------------------------------------------------------------------------- #
const off = await render(`/import?album=${encodeURIComponent(ALBUM)}&step=Advisory`,
                         { ...CONFIG, manual_import_enabled: false });
has(off.open, ["Importing by hand is off", "manual_import_enabled",
               "Settings → Import pipeline"],
    "manual importing off says why, naming the setting");
lacks(off.html, ["Save advisory", "Auto-import advisory for all tracks",
                 "Show everything else on this step"],
      "manual importing off offers no step to run");

// --------------------------------------------------------------------------- #
// 4. the Finish step runs the IMPORT CHAIN — never the library's Run All order
// --------------------------------------------------------------------------- #
// A CONFIGURED chain (import_scripts = the three ids below), which shares
// neither its set nor its order with the 21-id Run All order: a page that
// still read `run_all_order` cannot pass this by accident.
const CHAIN = [14, 3, 4];
const PREVIEW = { chain: CHAIN, count: CHAIN.length,
                  labels: { 14: "Beets tagging", 3: "Optimize FLACs", 4: "Grade" } };
const finish = await render(`/import?album=${encodeURIComponent(ALBUM)}&step=Finish`,
                            CONFIG, { importScripts: PREVIEW });

/** The Finish step's script grid, read off the rendered markup: every box's
 *  own label and whether it is ticked, in the order the page lays them out. */
const boxes = (html) => [...html.matchAll(/<input type="checkbox"([^>]*)>([^<]*)/g)]
  .map(([, attrs, text]) => ({ checked: attrs.includes("checked"), label: text.trim() }));

const grid = boxes(finish.html);
const ticked = grid.filter((b) => b.checked).map((b) => b.label);
const chainly = "Beets tagging → Optimize FLACs → Grade";
// One box per script in the app's own post-import list (`DEFAULT_RUN_ALL` in
// web/src/lib/scripts.ts — the source the wizard reads), NOT a literal: this
// asserted 21 and went red the moment a script was added, which says nothing
// about the page.
const scripts = await server.ssrLoadModule("/src/lib/scripts.ts");
const declared = scripts.DEFAULT_RUN_ALL.length;
check("the Finish step renders a box per script", grid.length === declared,
      `${grid.length} boxes, ${declared} in DEFAULT_RUN_ALL`);
check("the boxes are ticked on the import chain, and only on it",
      ticked.join(" → ") === chainly, ticked.join(" → "));
check("the chain's ids head the grid, in the chain's own order",
      grid.slice(0, 3).map((b) => b.label).join(" → ") === chainly,
      grid.slice(0, 3).map((b) => b.label).join(" → "));
// A script the configured chain leaves out keeps its box — unticked, so it is
// run only if the user asks for it — and the box it keeps is not the box the
// Run All order would have put there.
check("a script the chain does not name keeps its box, unticked",
      grid.some((b) => b.label === "Remux videos (MKV)" && !b.checked));
// The bulk press says "Run all": it runs every ticked box, and the boxes
// directly above it are the list of which those are — the import chain until
// the user changes them.
has(finish.open, ["Run all"],
    "the Finish step's bulk press is Run all — over its own ticked boxes");
lacks(finish.open, ["Run ticked scripts"],
      "…worded as the one press it is, not as a narrowed variant");
// …and it offers NO re-run of the import chain: that press went back through
// `finish_album`, whose own steps (drop_arrived_values included) emptied the
// four families an import decides and re-fetched them, undoing the work just
// done by hand in these steps. The button is gone; the ticked boxes are what
// Finish runs, and the album page can re-run any script on its own.
lacks(finish.open, ["Run the import chain"],
      "the Finish step offers no chain re-run");

// --------------------------------------------------------------------------- #
// 5. the affordance, clicked — and the order the user reads
// --------------------------------------------------------------------------- #
let chromium;
try {
  ({ chromium } = webRequire("playwright"));
} catch {
  console.error("[minimum] Playwright not found — `npm --prefix web i -D playwright`.");
  await server.close();
  process.exit(2);
}
// Tailwind's own declarations for the three classes the minimum step uses to
// put the controls first (this harness loads no built stylesheet; the
// disclosure itself needs no CSS to be revealable).
const ORDER_CSS = ".flex{display:flex}.flex-col{flex-direction:column}" +
  ".order-last{order:9999}.gap-3{gap:.75rem}.gap-4{gap:1rem}";

const browser = await chromium.launch();
try {
  const page = await browser.newPage();
  await page.setContent(`<!doctype html><html><body>${covers.html}</body></html>`);
  await page.addStyleTag({ content: ORDER_CSS });

  /** The node whose OWN text starts with `needle` — an element, not an
   *  ancestor that happens to contain it (the step container contains
   *  everything and would answer first otherwise). */
  const node = (needle) => `(() => {
    const hit = [...document.querySelectorAll("summary, div, span, button")].find((n) =>
      [...n.childNodes].some((c) => c.nodeType === 3 && c.textContent.trim().startsWith(${JSON.stringify(needle)})));
    if (!hit) throw new Error("no element carries the text " + ${JSON.stringify(needle)});
    return hit;
  })()`;

  /** Is that node rendered? (`checkVisibility` is the browser's own answer — a
   *  closed <details> lays its content out, it just does not render it.) */
  const shown = (needle) => page.evaluate(`(${node(needle)}).checkVisibility()`);
  const top = (needle) => page.evaluate(
    `Math.round((${node(needle)}).getBoundingClientRect().top)`);

  check("the collapsed blocks are not rendered", (await shown("Artist & album metadata")) === false);
  check("the cover controls are rendered", (await shown("Current album cover")) === true);
  const controlTop = await top("Current album cover");
  const barTop = await top("Show everything else on this step");
  check("the cover controls come before the disclosure",
      controlTop !== null && barTop !== null && controlTop < barTop,
      `controls at ${controlTop}, disclosure at ${barTop}`);

  await page.click("summary");
  check("the affordance reveals what it holds", (await shown("Artist & album metadata")) === true);
} finally {
  await browser.close();
  await server.close();
}

// --------------------------------------------------------------------------- #
// 6. Done while the album is held by another job: the press completes
// --------------------------------------------------------------------------- #
// The behaviour here is client-only — the wizard's own `finish` — so it is
// pinned the way the repo pins a page's behaviour: the REAL app (vite, the
// module graph, the router) on a scratch port with the API answered from
// canned payloads, and the ONE Done press driven for real.
//
// The refusal sentence is the server's own (`server/job_locks.refusal`): the
// album an Organize job is finishing, which `/api/run` and `/api/organize`
// answer 409 with. The press must read it as "already being finished" — not as
// a failure — and must still CLOSE (the Link to the album page is what closes
// the wizard; clearing the params afterwards would carry the wizard's route
// back over it). A genuine 500 is the second half: it must still be an error.
const REFUSAL = "Homework is in use by Organize (job-2) — wait for it to finish, then retry";

const app = await createServer({
  configFile: path.join(webDir, "vite.config.ts"),
  root: webDir,
  logLevel: "error",
  server: { host: "127.0.0.1", port: 8011, strictPort: false },
});
await app.listen();
const appPort = app.httpServer.address().port;

const appBrowser = await chromium.launch();
try {
  const context = await appBrowser.newContext({ serviceWorkers: "block" });
  const page = await context.newPage();
  await page.route("**/sw.js", (route) => route.abort());
  /** The one reply the second half changes: /api/run answers the claim's 409
   *  refusal, then a genuine 500. */
  let runReply = { status: 409, body: { detail: REFUSAL } };
  await page.route("**/api/**", (route) => {
    const url = route.request().url();
    const send = (status, body) => route.fulfill({
      status, contentType: "application/json", body: JSON.stringify(body) });
    if (url.includes("/api/auth/status")) return send(200, { required: false, authenticated: true });
    if (url.includes("/api/config")) return send(200, { ...CONFIG, first_run_done: true });
    if (url.includes("/api/library")) return send(200, LIBRARY);
    if (url.includes("/api/import/scripts/preview")) return send(200, PREVIEW);
    if (url.includes("/api/import/settle")) return send(200, {});
    if (url.includes("/api/organize")) return send(200, { results: [{ path: ALBUM }] });
    if (url.includes("/api/run")) return send(runReply.status, runReply.body);
    if (url.includes("/api/album?")) return send(200, { ...LIBRARY.artists[0].albums[0], artist: "Daft Punk" });
    if (url.includes("/api/artist/artwork")) return send(200, {});
    return undefined;                 // anything else: left unanswered (pending)
  });

  /** The Finish step's own Done press, through the real component. */
  const pressDone = async () => {
    await page.goto(`http://127.0.0.1:${appPort}/import?album=${encodeURIComponent(ALBUM)}&step=Finish`);
    await page.waitForSelector("text=Ticked by default", { timeout: 20000 });
    await page.locator("a", { hasText: "Done" }).first().click();
    // The press is done once the router has left for the album page; a beat
    // more so the closing status (a toast) is on screen to read.
    await page.waitForFunction(() => location.pathname.startsWith("/album/"),
                               null, { timeout: 20000 }).catch(() => {});
    await page.waitForTimeout(600);
  };
  const pageText = () => page.evaluate(
    "document.body.innerText.replace(/\\s+/g, ' ').trim()");

  await pressDone();
  check("Done under a held claim lands on the album, not back on the wizard",
        page.url().includes("/album/"), page.url());
  const held = await pageText();
  check("…and the album is reported as already being finished",
        held.includes("already being finished"), held.slice(0, 400));
  check("…without the refusal toasted as an error",
        !/Error:.*in use by/.test(held), held.slice(0, 400));

  // A genuine failure is not swallowed by the same handling.
  runReply = { status: 500, body: { detail: "the runner exploded" } };
  await pressDone();
  check("a real failure still surfaces as an error",
        (await pageText()).includes("the runner exploded"), (await pageText()).slice(0, 400));
} finally {
  await appBrowser.close();
  await app.close();
}

// --------------------------------------------------------------------------- #
// 7. staging a NEW album opens the Links step on an EMPTY release-id field
// --------------------------------------------------------------------------- #
// The owner's report: opening the import wizard for a new album arrived with a
// MusicBrainz release id already in the field — the PREVIOUS album's, never
// typed for this one. `doImport` replaces the album it stages (`setUploaded` /
// `setAlbumIndex` / `setAlbumPath`) but nothing cleared the release identity, so
// the field carried album A's id (and URL) onto album B — a claim about the
// wrong album. The reset is now ONE effect keyed on `albumPath` (the callback
// `resetAlbumRelease`), so it holds for every way a different album arrives.
//
// This drives the real page the way a user stages album after album: stage A
// (whose OWN tags name a release), read the filled field, walk back to the
// picker, stage B, and read the field again — it must be EMPTY, never A's id,
// while C (whose own tags DO name a release) still fills from its own tags.
const A_ALBUM = "F:/Music/Staged/Album A";
const B_ALBUM = "F:/Music/Staged/Album B";
const C_ALBUM = "F:/Music/Staged/Album C";
const A_MBID = "11111111-1111-4111-8111-111111111111";
const C_MBID = "33333333-3333-4333-8333-333333333333";
const UPLOADS = {
  "01 - Album A.wav": A_ALBUM,
  "01 - Album B.wav": B_ALBUM,   // deliberately NOT in the library: no MBID tag
  "01 - Album C.wav": C_ALBUM,
};
const stagedTrack = (album, mbid) => ({
  file: "01.wav", path: `${album}/01.wav`, tracknumber: 1, discnumber: 1,
  issues: [], values: {}, audit: null, log_grade: null, lyrics_embedded: false,
  lyrics_lrc: false, unreadable: false, tech: { length: 200 },
  tags: { TITLE: "One", ARTIST: "Staged", ALBUM: album.split("/").pop(), ...(mbid ? { MUSICBRAINZ_ALBUMID: mbid } : {}) },
  grade_pass: false, lyrics_present: false,
});
const LIB2 = {
  artists: [{ name: "Staged", albums: [
    { path: A_ALBUM, tracks: [stagedTrack(A_ALBUM, A_MBID)] },
    { path: C_ALBUM, tracks: [stagedTrack(C_ALBUM, C_MBID)] },
  ] }],
};
const wav = (name) => ({ name, mimeType: "audio/wav", buffer: Buffer.from("RIFF0000WAVEfmt ") });

const app2 = await createServer({
  configFile: path.join(webDir, "vite.config.ts"),
  root: webDir,
  logLevel: "error",
  server: { host: "127.0.0.1", port: 8011, strictPort: false },
});
await app2.listen();
const port2 = app2.httpServer.address().port;

const browser2 = await chromium.launch();
try {
  const context = await browser2.newContext({ serviceWorkers: "block" });
  const page = await context.newPage();
  await page.route("**/sw.js", (route) => route.abort());
  await page.route("**/api/**", (route) => {
    const url = route.request().url();
    const send = (status, body) => route.fulfill({
      status, contentType: "application/json", body: JSON.stringify(body) });
    if (url.includes("/api/auth/status")) return send(200, { required: false, authenticated: true });
    if (url.includes("/api/config")) return send(200, { ...CONFIG, first_run_done: true, metadata_review: false });
    if (url.includes("/api/library")) return send(200, LIB2);
    if (url.includes("/api/import/scripts/preview")) return send(200, PREVIEW);
    if (url.includes("/api/import/prompts")) return send(200, { prompts: [] });
    if (url.includes("/api/album/mbdetect")) return send(200, { mbid: null });
    if (url.includes("/api/mb/release?")) {
      const mbid = new URL(url).searchParams.get("mbid");
      // The Links step prints the fetched release (artists, media.length, …),
      // so the stub answers the whole shape, not only the id.
      return send(200, {
        id: mbid, title: "Staged", date: "2020", release_group_id: null,
        artists: [], genres: [], media: [], countries: [], medium_formats: [], medium_count: 1,
      });
    }
    if (url.includes("/api/mb/match")) return send(200, { suggestions: [] });
    if (url.includes("/api/mb/search/releases")) return send(200, []);
    if (url.includes("/api/import/upload")) {
      const td = new URL(url).searchParams.get("target_dir") || "";
      const album = UPLOADS[td];
      if (!album) return send(400, { detail: `unexpected target_dir ${td}` });
      return send(200, { ok: true, saved: [], album_path: album, album_name: td.replace(/\.wav$/, "") });
    }
    return undefined;                 // anything else: left unanswered (pending)
  });

  const FIELD = 'input[placeholder^="MusicBrainz release URL"]';
  const field = () => page.locator(FIELD);
  const importButton = () => page.getByRole("button", { name: /Import \d+ album\(s\) into library/ });
  /** Stage one file as an album and land on the Links step. */
  const stage = async (name) => {
    await page.setInputFiles("#import-files", wav(name));
    await importButton().click();
    await page.waitForSelector(FIELD, { timeout: 20000 });
  };
  /** Back to the picker (step 0) — a fresh pick REPLACES the selection. */
  const backToPicker = async () => {
    await page.getByRole("button", { name: "Select & separate" }).click();
    await page.waitForSelector("text=Drop albums, files or archives here", { timeout: 20000 });
  };

  await page.goto(`http://127.0.0.1:${port2}/import`);
  await page.waitForSelector(".panel-hero", { timeout: 20000 });

  // Album A: its own tags name a release, so the field is honestly filled.
  await stage("01 - Album A.wav");
  await page.waitForFunction((id) => {
    const el = document.querySelector('input[placeholder^="MusicBrainz release URL"]');
    return !!el && el.value.includes(id);
  }, A_MBID, { timeout: 20000 }).catch(() => {});
  const aValue = await field().inputValue();
  check("album A's own tags fill the release field", aValue.includes(A_MBID), aValue);

  // Album B, staged into the SAME wizard instance: A's id must not survive.
  await backToPicker();
  await stage("01 - Album B.wav");
  let sawA = false;
  let bValue = null;
  for (let i = 0; i < 40; i++) {
    bValue = await field().inputValue().catch(() => bValue);
    if (typeof bValue === "string" && bValue.includes(A_MBID)) sawA = true;
    await page.waitForTimeout(50);
  }
  check("staging album B never shows album A's release id", !sawA,
        `the field carried ${A_MBID}`);
  check("album B opens on an EMPTY release-id field", bValue === "",
        `field = ${JSON.stringify(bValue)}`);

  // Album C: its own tags DO name a release — the reset must not disable the
  // detection, only drop the WRONG album's answer.
  await backToPicker();
  await stage("01 - Album C.wav");
  await page.waitForFunction((id) => {
    const el = document.querySelector('input[placeholder^="MusicBrainz release URL"]');
    return !!el && el.value.includes(id);
  }, C_MBID, { timeout: 20000 }).catch(() => {});
  const cValue = await field().inputValue();
  check("album C's own tags still fill the field after the reset", cValue.includes(C_MBID), cValue);
  check("…and never show album A's id", !cValue.includes(A_MBID), cValue);
} finally {
  await browser2.close();
  await app2.close();
}

// --------------------------------------------------------------------------- #
// The Genres step's per-track row, driven on the REAL page: the derived
// PARENTS, never the name twice
// --------------------------------------------------------------------------- #
// The row used to print the facets `path`, which ENDS ON THE NAME ("Hip Hop ›
// Experimental Hip Hop") right beside the chip that already says it — the
// owner's question was "I don't understand the > purpose" (2026-10-12). The
// line is the parents only now, prefixed to read as a relation ("under Hip
// Hop"), and each chip carries its own place as a tooltip. `genreParents`
// (web/src/lib/genres.ts) is what drops the tail.
//
// This is a BROWSER drive, not server rendering: the wizard fills its genre
// state in an effect (React effects do not run under renderToString), so only
// the live page shows the chips at all.
const GENRE_LIBRARY = { artists: [{ name: "Daft Punk", albums: [{ path: ALBUM, tracks: [
  { ...TRACKS[0], tags: { ...TRACKS[0].tags, GENRE: "Experimental Hip Hop" } },
  { ...TRACKS[1], tags: { ...TRACKS[1].tags, GENRE: "Electropop" } },
] }] }] };
const FACETS = { genres: [
  { name: "Experimental Hip Hop", count: 1, path: "Hip Hop › Experimental Hip Hop" },
  { name: "Electropop", count: 1, path: "Pop › Electropop" },
] };
const app3 = await createServer({
  configFile: path.join(webDir, "vite.config.ts"), root: webDir, logLevel: "error",
  server: { host: "127.0.0.1", port: 8011, strictPort: false },
});
await app3.listen();
const port3 = app3.httpServer.address().port;
const browser3 = await chromium.launch();
try {
  const page = await (await browser3.newContext({ serviceWorkers: "block" })).newPage();
  await page.route("**/sw.js", (route) => route.abort());
  await page.route("**/api/**", (route) => {
    const url = route.request().url();
    const send = (body) => route.fulfill({
      status: 200, contentType: "application/json", body: JSON.stringify(body) });
    if (url.includes("/api/auth/status")) return send({ required: false, authenticated: true });
    if (url.includes("/api/config"))
      return send({ ...CONFIG, first_run_done: true, metadata_review: false });
    if (url.includes("/api/library")) return send(GENRE_LIBRARY);
    if (url.includes("/api/genres/facets")) return send(FACETS);
    // The SHELL's own queries (the wizard mounts inside the app): a stub that
    // answers `{}` for a list crashes the render (`sessionData.sessions is not
    // iterable`), so the three list-shaped payloads are named.
    if (url.includes("/api/import/sessions")) return send({ sessions: [] });
    if (url.includes("/api/import/prompts")) return send({ prompts: [] });
    if (url.includes("/api/auth/users"))
      return send({ users: ["owner"], current: "owner", required: false });
    return send({});          // every other payload this step reads: empty is fine
  });
  await page.goto(`http://127.0.0.1:${port3}/import?album=${encodeURIComponent(ALBUM)}&step=Genres`);
  // Wait for the TITLE, not only for the chip: the parents come from the
  // facets query, and a chip that exists a moment before that payload lands
  // has no tooltip yet (the first run of this check read exactly that race).
  await page.waitForFunction(
    () => [...document.querySelectorAll("span.chip")].some((el) => /sits under Hip Hop/.test(el.title)),
    { timeout: 20000 }).catch(() => {});
  const row = await page.evaluate(() => ({
    chips: [...document.querySelectorAll("span.chip")]
      .filter((el) => /Experimental Hip Hop|Electropop/.test(el.textContent))
      .map((c) => ({ text: c.textContent.trim(), title: c.title })),
    // What a READER sees: innerText carries no attributes, so the tooltip's
    // path cannot satisfy the "printed once" check below by accident.
    text: document.body.innerText,
  }));
  check("the Genres step renders the track row with its chips", row.chips.length >= 2,
        JSON.stringify(row.chips));
  check("…and the row shows the derived PARENTS, prefixed as a relation "
        + "(\"under Hip Hop\")", /under Hip Hop/.test(row.text),
        row.text.replace(/\n+/g, " | ").slice(-260));
  check("…never the whole path beside the name (the name is printed once)",
        !/Hip Hop › Experimental Hip Hop/.test(row.text),
        row.text.replace(/\n+/g, " | ").slice(-260));
  check("…while each chip carries its own place as its tooltip",
        row.chips.some((c) => c.text.startsWith("Experimental Hip Hop")
                        && c.title.includes("sits under Hip Hop"))
        && row.chips.some((c) => c.text.startsWith("Electropop")
                          && c.title.includes("sits under Pop")),
        JSON.stringify(row.chips));
} finally {
  await browser3.close();
  await app3.close();
}

if (failures.length) {
  console.error(`[minimum] FAILED (${failures.length} of ${checks}):`);
  for (const f of failures) console.error("  - " + f);
  process.exit(1);
}
console.log(`ok  the wizard's minimum entry mode shows the missing family's controls, ` +
  `collapses the rest behind one affordance that reveals it, leaves the ordinary ` +
  `wizard untouched, and runs the import chain — not the Run All order — from ` +
  `its Finish step; Done under a held claim completes on the album page with ` +
  `an "already being finished" status (a 500 still shows an error); a newly ` +
  `staged album opens the Links step on an EMPTY release-id field, never the ` +
  `previous album's, while its own tags still fill it; and the Genres step's ` +
  `per-track row shows a genre's derived PARENTS ("under Hip Hop"), never the ` +
  `whole path beside the name that already is the chip (${checks} checks)`);
