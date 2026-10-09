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
/** `--one-group`: this payload is ONE item seen twice (a release still being
 *  verified and the finished download of the same album), and the page must
 *  draw it in exactly one group. The base payload's own expectations below
 *  describe a whole queue, so they are not what this mode asserts. */
const mode = process.argv[3] || "";
if (!payloadPath) {
  console.error("[queue] usage: node tools/check_queue_view.mjs <payload.json> [--one-group]");
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

  if (mode === "--one-group") {
    // ONE ITEM, ONE GROUP (issue #68). The payload carries the same album
    // twice, exactly as build_queue() reports it while a release is being
    // verified: a JOB row in in_progress and the READY row for the folder
    // slskd already wrote, in completed. An item whose chain is still running
    // belongs to In progress alone, so the finished row must not be drawn —
    // and the item itself must still be there, in one group, with the other
    // finished rows untouched.
    const liveRows = payload.sections.in_progress || [];
    const doneRows = payload.sections.completed || [];
    const album = String((liveRows.find((r) => r.title) || {}).title || "");
    const folder = String((doneRows.find((r) => r.path) || {}).path || "");
    const leaf = folder.replace(/[\\/]+$/, "").replace(/^.*[\\/]/, "");
    const problems = [];
    const check = (what, ok, detail = "") => {
      if (!ok) problems.push(`${what}${detail ? `  ${detail}` : ""}`);
    };
    // The payload really is one item seen twice: the folder slskd wrote is
    // named by the album the job is fetching, which is what the page has to
    // recognize as the same thing (nothing else about the two rows agrees).
    check("the payload is one item in two rows (album name == folder leaf)",
          !!album && leaf.toLowerCase() === album.toLowerCase(),
          `${JSON.stringify(album)} vs ${JSON.stringify(leaf)}`);
    // Where each group's text starts, so a row can be attributed to a group:
    // the sections are rendered in the page's own order (In progress before
    // Completed before Failed).
    const at = (header) => flat.indexOf(`>${header} · `);
    const inStart = at("In progress"), doneStart = at("Completed"), failStart = at("Failed");
    check("both groups rendered", inStart > 0 && doneStart > inStart && failStart > doneStart,
          `${inStart}/${doneStart}/${failStart}`);
    const inChunk = inStart >= 0 && doneStart > inStart ? flat.slice(inStart, doneStart) : "";
    const doneChunk = doneStart >= 0 && failStart > doneStart ? flat.slice(doneStart, failStart) : "";
    // The item is drawn ONCE, in In progress, and Completed holds the one
    // finished row this payload has that nothing is running on.
    check("the running item is in In progress", inChunk.includes(album));
    check("the same item is NOT in Completed", !doneChunk.includes(album));
    check("In progress counts it once", inChunk.includes(">In progress · 1<"), inChunk.slice(0, 120));
    check("Completed does not count it", doneChunk.includes(">Completed · 1<"), doneChunk.slice(0, 120));
    // The dropped row is the FINISHED one: its note and its Import action are
    // gone, while the finished row nothing is running on keeps its own.
    check("the dropped ready row's own line is not drawn",
          !flat.includes("In the download folder — ready to import"));
    check("no Import button for the folder being verified",
          !flat.includes("Import this album all the way through"));
    check("the untouched finished row is still drawn there",
          doneChunk.includes("Imported into the library"));
    if (problems.length) {
      console.error("[queue] MISSING: " + JSON.stringify(problems));
      console.error("In progress: " + inChunk.slice(0, 1200));
      console.error("Completed:   " + doneChunk.slice(0, 1200));
      process.exit(1);
    }
    console.log("ok  an item whose chain is still running is drawn in ONE group, " +
      "with the finished row for the same album dropped");
  } else {
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
    // A wish whose ATTEMPT failed while the store still owns its next attempt
    // is in the Background with the releases still being searched (tools/
    // test_queue_view.py fixture wish 3) — so Failed holds only the job with no
    // wish behind it (a settled job whose wish is GONE keeps its row too, see
    // §8 of that suite).
    ["background section with its count", "Background · 1"],
    ["a failed attempt says the failure and the next search",
     "failed this attempt — searched again automatically at"],
    ["failed section with its count", "Failed · 1"],
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
    ["a section that only clears finished rows stays one press",
     "Clear this list: takes the 1 finished row(s) off this list"],
    ["a section that cancels live work arms its own empty",
     "Empty this list: cancels the 1 row(s) still running or waiting here"],
    ["...and names the bytes the Completed section would delete",
     "DELETES the 1 finished download(s) still in the download folder"],
    ["...and says what the destructive press does not touch",
     "Nothing in your library is touched."],
    ["the finished download's own one-press Delete (no arm step)",
     "from the download folder — its files go"],
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
  // ONE header button per list that has something to lose, and its label says
  // which kind of press it is: a section that cancels live work or deletes
  // downloaded bytes arms through a confirm ("Empty"), while one that only
  // clears finished history rows stays a single press ("Clear finished"). This
  // payload has 6 destructive sections — waiting, queued/searching, in
  // progress, background, needs-you (the parked job) and completed (the
  // finished download in the folder) — and exactly one plain clear (failed).
  // The panel's own global "Clear finished" is the other plain button for the
  // 4 clearable rows overall.
  const sectionEmpties = (flat.match(/Empty \(/g) || []).length;
  const sectionClears = (flat.match(/Clear finished \(/g) || []).length;
  if (sectionEmpties !== 6 || sectionClears !== 2) {
    console.error("[queue] header buttons rendered " + sectionEmpties +
      " Empty and " + sectionClears + " Clear finished — expected 6 and 2");
    process.exit(1);
  }
  console.log(`ok  the Soulseek page renders the queue sections and their rows ` +
    `(${want.length} checks, ${html.length} bytes of HTML)`);
  }   // end of the base payload's own expectations (the --one-group mode above)
} finally {
  await server.close();
}
