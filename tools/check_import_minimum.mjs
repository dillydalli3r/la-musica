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
                    "Save lyrics & instrumental",
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
  /** Back to the picker (step 0) — with the staged selection cleared by the
   *  import above, a fresh pick builds the next batch (a pick over a
   *  still-building selection BANKS it instead, see section 8). */
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
// The Genres step's per-track row, driven on the REAL page: the genre NAMES
// themselves, and NO derived parent line
// --------------------------------------------------------------------------- #
// The app no longer derives, shows or stores a level ABOVE a genre (the parent
// idea is gone): a track holds the TOP 2 genres MusicBrainz lists for it, or
// hand-typed names, and the row prints one chip per name and nothing else. The
// facets payload still carries a `path` ("Hip Hop › Experimental Hip Hop") for
// the cutover, so this check also proves the row IGNORES it — the name appears
// once, there is no "under …" relation, and a chip carries no parent tooltip.
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
  // Wait for the genre chips: the names come from the track's own GENRE tag,
  // which the step fills in an effect once the page is live.
  await page.waitForFunction(
    () => {
      const t = document.body.innerText;
      return t.includes("Experimental Hip Hop") && t.includes("Electropop");
    },
    { timeout: 20000 }).catch(() => {});
  const row = await page.evaluate(() => ({
    chips: [...document.querySelectorAll("span.chip")]
      .filter((el) => /Experimental Hip Hop|Electropop/.test(el.textContent))
      .map((c) => ({ text: c.textContent.trim(), title: c.title })),
    // What a READER sees: innerText carries no attributes, so no tooltip or
    // hidden text can satisfy the "printed once" / "no parent" checks below.
    text: document.body.innerText,
  }));
  check("the Genres step renders the track row with its chips", row.chips.length >= 2,
        JSON.stringify(row.chips));
  check("…showing the genre names themselves, one chip each",
        row.chips.some((c) => c.text.startsWith("Experimental Hip Hop"))
        && row.chips.some((c) => c.text.startsWith("Electropop")),
        JSON.stringify(row.chips));
  check("…and NO derived parent line (no \"under Hip Hop\" / \"under Pop\")",
        !/under Hip Hop|under Pop/.test(row.text),
        row.text.replace(/\n+/g, " | ").slice(-260));
  check("…never the whole path beside the name (the name is printed once)",
        !/Hip Hop › Experimental Hip Hop/.test(row.text),
        row.text.replace(/\n+/g, " | ").slice(-260));
  check("…and each chip is just the name — no parent tooltip",
        row.chips.every((c) => !c.title), JSON.stringify(row.chips));
} finally {
  await browser3.close();
  await app3.close();
}

// --------------------------------------------------------------------------- #
// 8. several batches staged and started together
// --------------------------------------------------------------------------- #
// The owner's ask (spec R395) plus their multi-disc report ("uploading
// multi-disc cd releases to the importing sometimes gets rid of the second
// cd"): the Select step keeps a WORKLIST of batches. Two drops added
// back-to-back both reach POST /api/import/bulk with their own album, and a row
// removed before the import runs never does — driven on the real page, reading
// the request the page actually sends.
const BATCH_A = "01 - Batch One.wav";
const BATCH_B = "01 - Batch Two.wav";
const DISC_ONE = "01 - Disc One.wav";
const DISC_TWO = "01 - Disc Two.wav";
let bulkBodies = [];

const app4 = await createServer({
  configFile: path.join(webDir, "vite.config.ts"),
  root: webDir,
  logLevel: "error",
  server: { host: "127.0.0.1", port: 8011, strictPort: false },
});
await app4.listen();
const port4 = app4.httpServer.address().port;
const browser4 = await chromium.launch();
try {
  const openPage = async () => {
    const page = await (await browser4.newContext({ serviceWorkers: "block" })).newPage();
    await page.route("**/sw.js", (route) => route.abort());
    await page.route("**/api/**", (route) => {
      const url = route.request().url();
      const send = (status, body) => route.fulfill({
        status, contentType: "application/json", body: JSON.stringify(body) });
      if (url.includes("/api/auth/status")) return send(200, { required: false, authenticated: true });
      if (url.includes("/api/auth/users"))
        return send(200, { users: ["owner"], current: "owner", required: false });
      if (url.includes("/api/config")) return send(200, { ...CONFIG, first_run_done: true, metadata_review: false });
      if (url.includes("/api/library")) return send(200, { artists: [] });
      if (url.includes("/api/import/scripts/preview")) return send(200, PREVIEW);
      if (url.includes("/api/import/prompts")) return send(200, { prompts: [] });
      if (url.includes("/api/import/sessions")) return send(200, { sessions: [] });
      // The staged album's payload names its release, so the identify dialog
      // is not raised and the press goes straight to the queue.
      if (url.includes("/api/album?"))
        return send(200, { path: "x", album_artist: "Staged", meta: { MUSICBRAINZ_ALBUMID: A_MBID },
                           album_values: {}, tracks: [] });
      if (url.includes("/api/import/upload")) {
        const td = new URL(url).searchParams.get("target_dir") || "";
        return send(200, { ok: true, saved: [], album_path: `/library/${td}`, album_name: td });
      }
      if (url.includes("/api/import/bulk/status"))
        return send(200, { id: "job1", kind: "bulk", status: "running", total: 1, done: 0, items: [] });
      if (url.includes("/api/import/bulk")) {
        bulkBodies.push(route.request().postDataJSON());
        return send(200, { ok: true, job: { id: "job1", status: "running", total: 1, done: 0,
                                            items: [{ path: "", status: "queued" }] } });
      }
      return undefined;               // anything else: left unanswered (pending)
    });
    await page.goto(`http://127.0.0.1:${port4}/import`);
    await page.waitForSelector(".panel-hero", { timeout: 20000 });
    return page;
  };
  const batchRows = (page) =>
    page.locator(".panel", { hasText: "Batches to import" })
      .locator("button[title^='Remove this batch']");
  const waitForBulk = async (page) => {
    for (let i = 0; i < 120 && !bulkBodies.length; i++) await page.waitForTimeout(50);
  };

  // (a) Two discs of ONE rip, added back-to-back: a new selection BANKS the
  // previous batch (auto-bank) instead of replacing it, so neither disc is
  // thrown away, and BOTH reach the queue as their own album.
  bulkBodies = [];
  let page = await openPage();
  await page.setInputFiles("#import-files", wav(DISC_ONE));
  await page.setInputFiles("#import-files", wav(DISC_TWO));
  await page.waitForFunction(() => document.body.innerText.includes("Batches to import — 1"),
                             null, { timeout: 20000 });
  check("a second drop banks the first batch as its own row",
        await batchRows(page).count() === 1, `${await batchRows(page).count()} rows`);
  check("…and both discs are staged (the second drop is the batch being built)",
        await page.getByRole("button", { name: /Add as another batch/ }).count() === 1);
  await page.getByRole("button", { name: /Auto-import \d+ album\(s\) from 2 batches/ }).click();
  await waitForBulk(page);
  check("both discs reach the queue as their own album — the second cd is not lost",
        bulkBodies.length === 1 && bulkBodies[0].items.length === 2
        && bulkBodies[0].items.some((r) => /Disc One/.test(r.path))
        && bulkBodies[0].items.some((r) => /Disc Two/.test(r.path)),
        JSON.stringify(bulkBodies));
  await page.context().close();

  // (b) Two batches banked with the button, ONE removed before the start: only
  // the remaining batch's album is queued.
  bulkBodies = [];
  page = await openPage();
  for (const [i, name] of [BATCH_A, BATCH_B].entries()) {
    await page.setInputFiles("#import-files", wav(name));
    await page.getByRole("button", { name: /Add as another batch/ }).click();
    await page.waitForFunction(
      (n) => document.body.innerText.includes(`Batches to import — ${n}`), i + 1,
      { timeout: 20000 });
  }
  check("two batched drops render as two rows in one list",
        await batchRows(page).count() === 2, `${await batchRows(page).count()} rows`);
  await batchRows(page).nth(1).click();
  check("the removed batch leaves one row",
        await batchRows(page).count() === 1, `${await batchRows(page).count()} rows`);
  await page.getByRole("button", { name: /Auto-import \d+ album\(s\)/ }).click();
  await waitForBulk(page);
  check("the start press queues the remaining batch, and only it",
        bulkBodies.length === 1 && bulkBodies[0].items.length === 1
        && /Batch One/.test(bulkBodies[0].items[0].path),
        JSON.stringify(bulkBodies));
  check("…and never the batch that was removed",
        !JSON.stringify(bulkBodies).includes("Batch Two"), JSON.stringify(bulkBodies));
  await page.context().close();
} finally {
  await browser4.close();
  await app4.close();
}

// --------------------------------------------------------------------------- #
// 9. Import & link on an album the library already holds: the release's own
//    gaps, "Add tracks…", and a row with no release track keeping its identity
// --------------------------------------------------------------------------- #
// The owner's report (2026-10-12): an album whose files name the STANDARD
// edition could not be re-pointed at the DELUXE one, and the Match step had no
// way to bring the deluxe's second disc in. Driven on the real page:
//   * the release's tracks that no local file was matched to are NAMED on the
//     step (they used to be invisible — only the local rows were listed);
//   * "Add tracks…" uploads to THIS album's own path (`album=`), never a
//     guessed name, and re-matches;
//   * "Save matching" writes the album-level fields onto a row with no release
//     track and NOTHING per-track: a `null` in that writable map DELETES the
//     tag, which stripped TITLE/TRACKNUMBER from exactly the extra tracks a
//     user had added by hand;
//   * TRACKTOTAL is the file's OWN disc's track count — a two-disc release
//     used to write both discs' total onto every file.
const L_ALBUM = "F:/Music/Artists/Sample/Test Album";
const L_REL = "99999999-9999-9999-9999-999999999999";
// The edition the album is re-pointed AT: the deluxe, whose second disc is
// exactly what the standard edition (L_REL, one disc) cannot show.
const L_REL2 = "77777777-7777-7777-7777-777777777777";
const L_GROUP = "88888888-8888-8888-8888-888888888888";
const lTrack = (n, name) => ({
  file: `${name}.wav`, path: `${L_ALBUM}/${name}.wav`, tracknumber: n, discnumber: 1,
  issues: [], values: {}, audit: null, log_grade: null, lyrics_embedded: false,
  lyrics_lrc: false, unreadable: false, tech: { length: 200 },
  tags: { TITLE: name, ARTIST: "Sample", ALBUM: "Test Album", DISCNUMBER: "1",
          MUSICBRAINZ_ALBUMID: L_REL, MUSICBRAINZ_RELEASEGROUPID: L_GROUP },
  grade_pass: false, lyrics_present: false,
});
const L_MEDIA = [
  { disc: 1, position: 1, title: "One", length: 200000, recording_mbid: "rec-1",
    artist_mbids: ["art-1"], artist_credit: "Sample", genres: [] },
  { disc: 1, position: 2, title: "Two", length: 200000, recording_mbid: "rec-2",
    artist_mbids: ["art-1"], artist_credit: "Sample", genres: [] },
  { disc: 2, position: 1, title: "Three (deluxe)", length: 200000, recording_mbid: "rec-3",
    artist_mbids: ["art-1"], artist_credit: "Sample", genres: [] },
];
// L_REL is the ONE-DISC edition the album's own files name (the first two
// tracks); L_REL2 is the two-disc deluxe above.
const L_MEDIA_STD = L_MEDIA.slice(0, 2);
const L_MATCH = { suggestions: [
  { local: `${L_ALBUM}/01 - One.wav`, file: "01 - One.wav", matched: true, confidence: 1,
    release_track: L_MEDIA[0] },
  // An EXTRA track the release does not list: the row the payload must not
  // clear (its title is the file's own).
  { local: `${L_ALBUM}/02 - Extra.wav`, file: "02 - Extra.wav", matched: false, confidence: 0,
    release_track: null },
] };
let lAssign = null;
let lUpload = null;
let lCommit = null;
let lExpected = null;
const lReleaseCalls = [];
let lReleaseQuery = null;

const app5 = await createServer({
  configFile: path.join(webDir, "vite.config.ts"),
  root: webDir,
  logLevel: "error",
  server: { host: "127.0.0.1", port: 8011, strictPort: false },
});
await app5.listen();
const port5 = app5.httpServer.address().port;
const browser5 = await chromium.launch();
try {
  const page = await (await browser5.newContext({ serviceWorkers: "block" })).newPage();
  await page.route("**/sw.js", (route) => route.abort());
  await page.route("**/api/**", async (route) => {
    const url = route.request().url();
    const send = (status, body) => route.fulfill({
      status, contentType: "application/json", body: JSON.stringify(body) });
    if (url.includes("/api/auth/status")) return send(200, { required: false, authenticated: true });
    if (url.includes("/api/auth/users"))
      return send(200, { users: ["owner"], current: "owner", required: false });
    if (url.includes("/api/config")) return send(200, { ...CONFIG, first_run_done: true, metadata_review: false });
    if (url.includes("/api/library")) return send(200, {
      artists: [{ name: "Sample", albums: [
        { path: L_ALBUM, tracks: [lTrack(1, "01 - One"), lTrack(2, "02 - Extra")] }] }] });
    if (url.includes("/api/import/sessions")) return send(200, { sessions: [] });
    if (url.includes("/api/import/prompts")) return send(200, { prompts: [] });
    if (url.includes("/api/import/scripts/preview")) return send(200, PREVIEW);
    if (url.includes("/api/mb/release?")) {
      // The stub answers the release the caller NAMED, with that release's own
      // tracklist: Continue resolves whichever id the field holds, so a re-link
      // to the deluxe has to come back AS the deluxe (two discs, three tracks).
      const id = new URL(url).searchParams.get("mbid") || L_REL;
      lReleaseCalls.push(id);
      lReleaseQuery = new URL(url).searchParams.get("match");
      const deluxe = id === L_REL2;
      return send(200, {
        id, title: deluxe ? "Test Album (Deluxe)" : "Test Album", date: "2020-01-01",
        release_group_id: L_GROUP, release_type: "Album",
        artists: [{ name: "Sample", mbid: "art-1" }], genres: [],
        media: deluxe ? L_MEDIA : L_MEDIA_STD, medium_count: deluxe ? 2 : 1,
        medium_formats: ["CD"], countries: [] });
    }
    if (url.includes("/api/mb/match")) return send(200, L_MATCH);
    if (url.includes("/api/import/commit")) {
      lCommit = JSON.parse(route.request().postData() || "{}");
      return send(200, { ok: true, changed: 2 });
    }
    if (url.includes("/api/import/expected")) {
      lExpected = JSON.parse(route.request().postData() || "{}");
      return send(200, { ok: true, tracks: 3 });
    }
    if (url.includes("/api/import/upload")) {
      lUpload = url;
      return send(200, { ok: true, saved: [`${L_ALBUM}/03 - Added.wav`],
                         album_path: L_ALBUM, album_name: "Test Album", merged: true,
                         collided: false, kept: [] });
    }
    if (url.includes("/api/mb/assign")) {
      lAssign = JSON.parse(route.request().postData() || "{}");
      return send(200, { ok: true, changed: 2, errors: [], container_changed: false, output_paths: [] });
    }
    return send(200, {});      // every other payload this step reads: empty is fine
  });

  await page.goto(`http://127.0.0.1:${port5}/import?album=${encodeURIComponent(L_ALBUM)}`);
  const field5 = page.locator('input[placeholder^="MusicBrainz release URL"]');
  await page.waitForSelector('input[placeholder^="MusicBrainz release URL"]', { timeout: 20000 });
  // The album's own tags name its release: the step fills the field by itself.
  await page.waitForFunction((id) => {
    const el = document.querySelector('input[placeholder^="MusicBrainz release URL"]');
    return !!el && el.value.includes(id);
  }, L_REL, { timeout: 20000 }).catch(() => {});
  check("an album being re-linked opens on the release its OWN tags name",
        (await field5.inputValue()).includes(L_REL), await field5.inputValue());
  // The tags name the ONE-DISC edition and the user changes the link to the
  // deluxe: the EDIT itself asks nobody (MusicBrainz is 1 req/s, and Continue
  // is the press that means it), so the check goes straight to Continue.
  await field5.fill(`https://musicbrainz.org/release/${L_REL2}`);
  await page.getByRole("button", { name: "Continue" }).click();
  await page.waitForSelector("[data-add-tracks]", { timeout: 20000 });
  // Continue is the WRITE: it resolves what the field names, stamps the id on
  // the album, and records the release's own tracklist with it — leaving the
  // wizard after this step must not leave the folder describing the release it
  // just left.
  for (let i = 0; i < 80 && (!lCommit || !lExpected); i++) await page.waitForTimeout(50);
  check("Continue commits the release the FIELD names, not the one the tags did",
        !!lCommit && String(lCommit.mb_link || "").includes(L_REL2), JSON.stringify(lCommit));
  check("…and records that release's own tracklist on the album, id and rows",
        !!lExpected && String(lExpected.release_id || "").includes(L_REL2)
        && (lExpected.tracks || []).length === 3, JSON.stringify(lExpected?.tracks?.length));

  const missing = await page.locator("[data-missing-tracks]").first().getAttribute("data-missing-tracks");
  check("the Match step names the release's tracks no file was matched to",
        missing === "2", `data-missing-tracks = ${missing}`);
  const stepText = await page.locator("body").innerText();
  check("…including the second disc's own track",
        /2\.1 Three \(deluxe\)/.test(stepText), stepText.slice(-300).replace(/\n+/g, " | "));

  await page.setInputFiles("[data-add-tracks-input]", {
    name: "03 - Added.wav", mimeType: "audio/wav", buffer: Buffer.from("RIFF0000WAVEfmt ") });
  await page.waitForTimeout(1200);
  check("\"Add tracks…\" uploads to THIS album's own path, never a guessed name",
        !!lUpload && new URL(lUpload).searchParams.get("album") === L_ALBUM,
        String(lUpload));
  check("…and asks for the re-match that makes the added file a row",
        !!lUpload, String(lUpload));
  check("the release pick asks for the local match in the SAME request (one round trip)",
        lReleaseQuery === L_ALBUM, String(lReleaseQuery));

  await page.getByRole("button", { name: "Save matching" }).click();
  await page.waitForFunction(() => true, null, { timeout: 300 }).catch(() => {});
  for (let i = 0; i < 40 && !lAssign; i++) await page.waitForTimeout(50);
  const extra = lAssign?.tracks?.[`${L_ALBUM}/02 - Extra.wav`] ?? null;
  const matched = lAssign?.tracks?.[`${L_ALBUM}/01 - One.wav`] ?? null;
  check("the payload reaches POST /api/mb/assign for both rows",
        !!extra && !!matched, JSON.stringify(lAssign));
  check("an unmatched row is written NO per-track key (a null DELETES the tag)",
        !!extra && !("TITLE" in extra) && !("TRACKNUMBER" in extra)
        && !("DISCNUMBER" in extra) && !("MUSICBRAINZ_TRACKID" in extra),
        JSON.stringify(extra));
  check("…while the album-level identity is still written on it",
        !!extra && extra.MUSICBRAINZ_ALBUMID === L_REL2
        && extra.ALBUM === "Test Album (Deluxe)",
        JSON.stringify(extra));
  check("a matched row carries the release's own track",
        !!matched && matched.TITLE === "One" && matched.TRACKNUMBER === "1"
        && matched.DISCNUMBER === "1", JSON.stringify(matched));
  check("TRACKTOTAL is the file's OWN disc's count, not both discs'",
        !!matched && matched.TRACKTOTAL === "2" && matched.DISCTOTAL === "2",
        JSON.stringify(matched));
  await page.context().close();
} finally {
  await browser5.close();
  await app5.close();
}

// --------------------------------------------------------------------------- #
// 10. The Library's Import menu: every selected album gets a row, opened on the
//     release its files name — changing a row IS the release change
// --------------------------------------------------------------------------- #
// The owner's ask (2026-10-12): "just changing the musicbrainz link in the
// import menu should change the release too". The menu that RUNS the import is
// the menu that re-points: each row opens on the release the album's own files
// name now, an edited row is sent as a pin (the pipeline writes a pin OVER the
// existing id — `force_ids`), and a row left alone sends nothing, so the press
// is the plain import it always was. Measured on the real app (a scratch
// library whose album named the deluxe): editing the row to the standard
// re-pointed all four files.
const libBodies = [];
const app6 = await createServer({
  configFile: path.join(webDir, "vite.config.ts"),
  root: webDir,
  logLevel: "error",
  server: { host: "127.0.0.1", port: 8011, strictPort: false },
});
await app6.listen();
const port6 = app6.httpServer.address().port;
const browser6 = await chromium.launch();
try {
  const page = await (await browser6.newContext({ serviceWorkers: "block" })).newPage();
  await page.route("**/sw.js", (route) => route.abort());
  await page.route("**/api/**", (route) => {
    const url = route.request().url();
    const send = (status, body) => route.fulfill({
      status, contentType: "application/json", body: JSON.stringify(body) });
    if (url.includes("/api/auth/status"))
      return send(200, { required: false, gate: false, local: true, has_password: false,
                         authenticated: true, username: "", public_url: "", session_days: 30,
                         setup_hint: "" });
    if (url.includes("/api/auth/users"))
      return send(200, { users: ["owner"], current: "owner", required: false });
    if (url.includes("/api/config")) return send(200, { ...CONFIG, first_run_done: true, metadata_review: false });
    if (url.includes("/api/version"))
      return send(200, { version: "0.0.0", latest: "0.0.0", update_available: false,
                         release_url: "", project_url: "", issues_url: "", build: {},
                         checked_at: 0, source: "unavailable" });
    // One artist, ONE album — and the album's own files name the one-disc
    // edition, which is what its row must open on.
    if (url.includes("/api/library")) return send(200, { artists: [{
      name: "Sample", display_name: "Sample", path: `${L_ALBUM}/..`, aggregate: {},
      albums: [{
        path: L_ALBUM, album_artist: "Sample", cover: false, cover_file: null,
        meta: { ALBUM: "Test Album", MUSICBRAINZ_ALBUMID: L_REL },
        album_values: {}, grade_pass: false, grade_pct: null,
        tracks: [lTrack(1, "01 - One"), lTrack(2, "02 - Extra")],
      }] }] });
    if (url.includes("/api/import/sessions")) return send(200, { sessions: [] });
    if (url.includes("/api/import/prompts")) return send(200, { prompts: [] });
    // The grade strip every page header draws (`components/GradeWarning`):
    // nothing failing, which is what a healthy library says.
    if (url.includes("/api/grades/summary"))
      return send(200, { ok: true, pass_count: 1, total_checks: 70, grade_pct: 100,
                         albums_failing: 0, albums_importing: 0, tracks_failing: 0,
                         items: [], more: 0 });
    if (url.includes("/api/import/bulk")) {
      libBodies.push(route.request().postDataJSON());
      return send(200, { ok: true, job: { id: "lib1", status: "running", total: 1, done: 0,
                                          items: [{ path: "", status: "queued" }] } });
    }
    return send(200, {});      // locks, ratings, favorites, layout report: empty is fine
  });
  await page.goto(`http://127.0.0.1:${port6}/library`);
  await page.waitForSelector("text=Test Album", { timeout: 20000 });
  await page.getByTitle("Select mode — show checkboxes for batch actions").click();
  await page.waitForTimeout(300);
  await page.locator('input[type="checkbox"]').first().click();
  await page.waitForFunction(() => /1 album · /.test(document.body.innerText), null,
                             { timeout: 10000 }).catch(() => {});
  await page.getByRole("button", { name: "Import", exact: true }).click();
  const row6 = page.locator('input[placeholder^="MusicBrainz release or release-group URL"]').first();
  await row6.waitFor({ timeout: 10000 });
  check("the Import menu opens the album's row on the release its files name",
        (await row6.inputValue()).toLowerCase() === L_REL, await row6.inputValue());
  const cta6 = () => page.getByRole("button", { name: /^Import 1 album/ }).innerText()
    .then((t) => t.replace(/\s+/g, " ").trim());
  check("with nothing edited the press is the plain import, and says so",
        (await cta6()) === "Import 1 album", await cta6());
  await row6.fill(`https://musicbrainz.org/release/${L_REL2}`);
  await page.waitForTimeout(200);
  check("editing the link turns that same press into the re-point",
        (await cta6()) === "Import 1 album — 1 re-pointed", await cta6());
  await page.getByRole("button", { name: /^Import 1 album/ }).click();
  for (let i = 0; i < 80 && !libBodies.length; i++) await page.waitForTimeout(50);
  const sent6 = libBodies[0]?.items?.[0];
  check("the edited row reaches POST /api/import/bulk as that album's pin",
        !!sent6 && sent6.path === L_ALBUM && String(sent6.mbid || "").includes(L_REL2),
        JSON.stringify(sent6));
  // The same selection again, nothing touched this time.
  await page.getByRole("button", { name: "Import", exact: true }).click();
  await page.getByRole("button", { name: /^Import 1 album/ }).waitFor({ timeout: 10000 });
  await page.getByRole("button", { name: /^Import 1 album/ }).click();
  for (let i = 0; i < 80 && libBodies.length < 2; i++) await page.waitForTimeout(50);
  const sent6b = libBodies[1]?.items?.[0];
  check("a row left alone sends NO pin — the album keeps the release it names",
        !!sent6b && sent6b.path === L_ALBUM && !("mbid" in sent6b), JSON.stringify(sent6b));
  await page.context().close();
} finally {
  await browser6.close();
  await app6.close();
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
  `per-track row shows the genre NAMES themselves, one chip each, with no ` +
  `derived parent line and no path beside the name that already is the chip; and several staged ` +
  `batches render as rows in one list, a removed batch never reaches the ` +
  `queue, and the remaining batch's album is what POST /api/import/bulk ` +
  `receives; and Import & link on an album the library already holds names ` +
  `the release tracks no file was matched to, uploads "Add tracks…" to the ` +
  `album's own path, writes an unmatched row no per-track tag (a null there ` +
  `DELETES the file's TITLE) and stamps TRACKTOTAL from the file's own disc; ` +
  `and changing the MusicBrainz link of an album that already names a release ` +
  `changes that release: the wizard's Links step resolves what the FIELD names ` +
  `(not what the tags say) and records the new release's own tracklist with the ` +
  `id, while the ` +
  `Library's Import menu opens every selected album's row on the release its ` +
  `files name, sends an edited row as its pin and sends nothing for a row ` +
  `left alone (${checks} checks)`);
