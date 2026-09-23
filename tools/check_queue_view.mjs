#!/usr/bin/env node
/* The Soulseek page's queue view, rendered for real.
 *
 * `/api/queue` (server/api_queue.py) is one payload; this check renders the
 * ACTUAL page component with it — through Vite, in memory, no build output —
 * and asserts what a user would see: the six groups (the waiting queue first,
 * then queued/searching) with their counts, a downloading row with its
 * progress, a finished download with its import outcome, a parked row saying
 * what it waits for, a failure with its reason, the album link, the source chip
 * and the per-item cancel.
 *
 * It is the UI half of tools/test_queue_view.py, which owns the payload: that
 * test writes the real `build_queue()` output to a file and runs this script
 * with it (skipping, loudly, when Node or web/node_modules is missing).
 *
 * Run:  node tools/check_queue_view.mjs <payload.json>
 * Exit codes: 0 pass, 1 a check failed (the missing ones are printed), 2 the
 * environment cannot run it (no web/node_modules).
 */
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const webDir = path.join(here, "..", "web");
const payloadPath = process.argv[2];
if (!payloadPath) {
  console.error("[queue] usage: node tools/check_queue_view.mjs <payload.json>");
  process.exit(2);
}
if (!existsSync(path.join(webDir, "node_modules"))) {
  console.error("[queue] web/node_modules is missing — run `npm --prefix web install`.");
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

// The page is a browser app: two globals it reaches for at import time (the
// toast store keeps its state in localStorage).
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
  clear: () => store.clear(),
};
globalThis.window = globalThis;

const payload = JSON.parse(readFileSync(payloadPath, "utf8"));
const server = await createServer({
  configFile: path.join(webDir, "vite.config.ts"),
  root: webDir,
  server: { middlewareMode: true },
  appType: "custom",
  logLevel: "error",
});

try {
  const page = await server.ssrLoadModule("/src/pages/SoulseekPage.tsx");
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(["soulseekQueue"], payload);
  const html = renderToString(
    React.createElement(QueryClientProvider, { client: qc },
      React.createElement(MemoryRouter, { initialEntries: ["/soulseek"] },
        React.createElement(page.default)))
  );
  // React separates adjacent text nodes with <!-- --> markers; they are not
  // content, so the assertions read the flattened text.
  const flat = html.replace(/<!-- -->/g, "").replace(/\s+/g, " ");
  const want = [
    ["the waiting group with its count", "Waiting · 1"],
    ["a waiting row says where in the line it is", "Waiting · #1"],
    ["the waiting group says why it waits", "starts by itself when one of them finishes"],
    ["clear all for the waiting queue", "Clear all (1)"],
    // The counts include the deferred-add fixture rows: an add whose
    // MusicBrainz identity is still being resolved, one whose resolution never
    // landed, and one the store has ENDED (tools/test_queue_view.py).
    ["queued section with its count", "Queued / searching · 4"],
    ["the deferred add's own stage label", "Searching MusicBrainz…"],
    ["in-progress section with its count", "In progress · 3"],
    ["needs-attention section", "Needs you · 2"],
    ["completed section with its count", "Completed · 3"],
    ["failed section with its count", "Failed · 2"],
    ["pipeline header counts", "3/3 running"],
    ["the per-release candidate ceiling named", "3 candidate(s) each"],
    // The walk (spec R150-R153): one wish working through the group's ranked
    // editions says so with its own badge, using the server's wording — and
    // the row it rides is still ONE row, whatever the walk does.
    ["the walk badge names the position being asked", "> Release 2 of 2<"],
    ["the walk badge explains what it is doing",
     "Also searching this release group&#x27;s other pressings, Release 2 of 2"],
    ["the walk badge names the edition being asked", "Asking: Isles (Japan)"],
    ["and what already came back empty", "Came back empty: Isles"],
    ["slskd transfer ceiling named", "9 slskd transfer slot(s)"],
    ["the downloading row's release", "Isles"],
    ["its artist", "Bicep"],
    ["byte-weighted progress", "50%"],
    ["the live rate", "500 kB/s"],
    ["the release identity line's tooltip (a job row)", "ZEN-124 · CD · GB 2018-04-20 · 12 track(s) · (Deluxe Edition) · Official · Ninja Tune"],
    ["the pressing's catalogue number", ">ZEN-124<"],
    ["its track count", "12 track(s)"],
    ["the edition's disambiguation", "(Deluxe Edition)"],
    ["MusicBrainz's own status for it", "Official"],
    ["a wish row's own identity line (tooltip)", "PRO-CD-1 · CD · US 1990-03 · 3 track(s) · (promo) · Promotion · Void Recordings"],
    ["the wish row's catalog number", ">PRO-CD-1<"],
    ["a finished download's import outcome", "In the download folder — ready to import"],
    ["a parked item says what it waits for", "Only lossy copies found"],
    ["a wish nothing was found for says so", "not searched again unless you retry it"],
    ["the failure reason", "every candidate was rejected"],
    ["an album link into the library", "/album/F%3A%2FMusic%2FArtists%2FDaft%20Punk%20-%20Homework"],
    ["the MusicBrainz source chip", "MusicBrainz"],
    ["the Soulseek source chip", "Soulseek"],
    ["the per-item clear on a finished row", "Remove this row from the queue — nothing is searched for it again"],
    ["the section-wide clear", "Clear finished ("],
    ["a section's own clear", "Take the finished rows off this list"],
    ["a finished job's own clear", "Take this finished row off the queue — nothing in your library is deleted"],
    ["the per-item cancel", "Cancel this item"],
    ["cancelling a waiting row", "Take it back off the queue"],
    // THE ROW DETAIL: what a row is doing, whose copy is arriving, what the
    // search is asking, why it is still here and until when — each line
    // rendered from the server's own field (see the fixture in
    // tools/test_queue_view.py for where every value comes from).
    ["the live step, in the job's own words", "Now: Downloading 12 file(s) from peer"],
    ["the query the search is asking", "Crimson Nova"],
    ["the peer whose copy is in flight", "peer_one · Music/Isles"],
    ["the files the job's wait accepted on disk", "5/12 file(s)"],
    ["the candidates already refused, with their reasons", "Rejected candidates (2)"],
    ["a refusal's own reason", "User appears to be offline"],
    ["a refusal's own peer", ">peer_nolog<"],
    ["the failure's own reasons in its message", "peer_offline: User appears to be offline"],
    ["a waiting row counts down to the failure's backoff", "Retrying in "],
    ["the album whose import chain is still running", "In the library — the import pipeline is still running"],
  ];
  const missing = want.filter(([, text]) => !flat.includes(text));
  if (missing.length) {
    console.error("[queue] MISSING: " +
      JSON.stringify(missing.map(([what]) => what)));
    console.error(flat.replace(/></g, ">\n<").split("\n")
      .filter((l) => /chip|Completed|Failed/.test(l)).slice(0, 20).join("\n"));
    process.exit(1);
  }
  // ONE section-wide clear per list that has something to lose: this payload
  // has a clearable row in completed, in failed and in needs-you (the imported
  // job, the job that gave up, the wish nothing was found for), and none in
  // queued / in progress — so the button is per section, not one global control
  // wearing three labels.
  const sectionClears = (flat.match(/Take the finished rows off this list/g) || []).length;
  if (sectionClears !== 3) {
    console.error("[queue] the section-wide clear rendered " + sectionClears +
      " time(s) — expected one each for completed, failed and needs-you");
    process.exit(1);
  }
  console.log(`ok  the Soulseek page renders the queue sections and their rows ` +
    `(${want.length} checks, ${html.length} bytes of HTML)`);
} finally {
  await server.close();
}
