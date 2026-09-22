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
                  "This import could not finish: Cover art, Advisory still missing",
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
has(advisory.open, ["This import could not finish: Cover art, Advisory still missing"],
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
has(nothing.open, ["This import could not finish: Advisory still missing"],
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
check("the Finish step renders a box per script", grid.length === 21, `${grid.length} boxes`);
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
lacks(finish.open, ["Run all scripts"], "the Finish step offers no Run All of its own");
has(finish.open, ["Run ticked scripts", "Run the import chain"],
    "the Finish step runs the ticked boxes or the chain itself");

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

if (failures.length) {
  console.error(`[minimum] FAILED (${failures.length} of ${checks}):`);
  for (const f of failures) console.error("  - " + f);
  process.exit(1);
}
console.log(`ok  the wizard's minimum entry mode shows the missing family's controls, ` +
  `collapses the rest behind one affordance that reveals it, leaves the ordinary ` +
  `wizard untouched, and runs the import chain — not the Run All order — from ` +
  `its Finish step (${checks} checks)`);
