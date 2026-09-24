#!/usr/bin/env node
/* The lyrics KIND on the album's own lyrics surface, rendered for real.
 *
 * The owner asked every surface that says whether a track HAS lyrics to say
 * WHICH KIND they are, and to show a plain lyric as a FAILING state while the
 * user's own setting says plain is not acceptable. The facts behind that:
 *
 *   * the payload field `lyrics_kind` ("synced" | "plain" | null), stamped
 *     server-side (mlo.lyrics.stored_lyrics_kind → mlo.grader) and pinned by
 *     tools/test_lyrics_kind.py; here it is only what the surface receives;
 *   * `lyrics_allow_plain` (off by default, mlo/config.py) — the user's own
 *     statement that untimed lyrics are acceptable.
 *
 * This check renders the REAL album page (web/src/pages/AlbumPage.tsx, the
 * tracklist whose rows carry the chip) against a payload holding one track of
 * each kind and the config's own value, twice — setting off and setting on:
 *
 *   off  → "Synced" and "Plain" are rendered, the PLAIN one is the app's
 *          failing mark (red chip with the cross, `data-lyrics-fail`) whose
 *          reason names the setting, and the track with NO lyrics renders no
 *          kind mark at all;
 *   on   → the same two words, the plain chip neutral, no cross anywhere, and
 *          the reason replaced by the plain hint.
 *
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
  console.error("[lyrics-kind] web/node_modules is missing — run `npm --prefix web install`.");
  process.exit(2);
}

// Loaded FROM web/ so the harness and the page share ONE React instance (see
// tools/check_import_minimum.mjs, whose harness this is).
const webRequire = createRequire(path.join(webDir, "package.json"));
const { createServer } = await import(
  `file://${path.join(webDir, "node_modules/vite/dist/node/index.js").replace(/\\/g, "/")}`);
const React = webRequire("react");
const { renderToString } = webRequire("react-dom/server");
const { MemoryRouter, Routes, Route } = webRequire("react-router-dom");
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
// landing: every query the page fires is answered from the cache below, and
// anything it has not cached stays PENDING forever instead of reaching for the
// network (a relative URL has no meaning outside the browser anyway).
globalThis.fetch = () => new Promise(() => {});

const ALBUM = "F:/Music/Artists/Kind Artist/2020 - Kind Album";

/** One track row of the album payload, of a known lyrics kind. */
const track = (n, file, kind) => ({
  file,
  path: `${ALBUM}/${file}`,
  tracknumber: n,
  discnumber: 1,
  issues: [],
  values: {},
  audit: null,
  log_grade: null,
  lyrics_embedded: kind !== null,
  lyrics_lrc: false,
  lyrics_kind: kind,
  lyrics_present: kind !== null,
  unreadable: false,
  tech: { length: 200, bitrate: 900, samplerate: 44100, bits: 16, format: "FLAC", channels: 2 },
  tags: {
    TITLE: file.replace(/^\d+ - /, "").replace(/\.flac$/, ""),
    ARTIST: "Kind Artist", ALBUMARTIST: "Kind Artist", ALBUM: "Kind Album",
    DISCNUMBER: "1", MEDIA: "CD", DATE: "2020", "DYNAMIC RANGE": "8",
  },
  grade_pass: true,
  cover_file: null,
});

const TRACKS = [
  track(1, "01 - Timed.flac", "synced"),
  track(2, "02 - Plain.flac", "plain"),
  track(3, "03 - None.flac", null),
];

const ALBUM_PAYLOAD = {
  path: ALBUM,
  album_artist: "Kind Artist",
  meta: { ALBUM: "Kind Album", ALBUMARTIST: "Kind Artist", ARTIST: "Kind Artist",
          MEDIA: "CD", DATE: "2020" },
  tracks: TRACKS,
  expected_tracks: [],
  partial: false,
  pending: false,
  pending_reason: null,
  wish: null,
  wish_id: null,
  artwork: null,
  issues: {},
  notes: [],
  grade_pct: 100,
  pass: true,
  pass_count: 3,
  total_checks: 3,
  track_count: 3,
  audit_summary: null,
  cover_file: null,
  has_log: false,
  has_cue: false,
  checksum_status: "",
  accuraterip_status: "",
  lyrics_present: 2,
  lyrics_expected: 2,
  instrumental_count: 0,
  media: "CD",
  source_summary: null,
};

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

/** The rendered text as a reader would see it (React's text-node markers and
 *  the HTML entities removed). */
const flatten = (html) => html.replace(/<!-- -->/g, "")
  .replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/&#x27;|&#39;/g, "'")
  .replace(/&lt;/g, "<").replace(/&gt;/g, ">")
  .replace(/\s+/g, " ");

/** The album page at its own URL, with the payload and the config it reads. */
async function render(allowPlain) {
  const page = await server.ssrLoadModule("/src/pages/AlbumPage.tsx");
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(["album", ALBUM], ALBUM_PAYLOAD);
  qc.setQueryData(["config"], { music_folder: "F:/Music", lyrics_allow_plain: allowPlain });
  const html = renderToString(
    React.createElement(QueryClientProvider, { client: qc },
      React.createElement(MemoryRouter, { initialEntries: [`/album/${encodeURIComponent(ALBUM)}`] },
        // The page reads its album path off the ROUTE (`/album/:path`), so it
        // has to be rendered at that route — bare, useParams answers "" and the
        // page sits in its loading state forever.
        React.createElement(Routes, null,
          React.createElement(Route, { path: "/album/:path", element: React.createElement(page.default) })))));
  return { html, text: flatten(html) };
}

/** One chip's own element: from its opening `<span` to its closing one, so a
 *  check can say what THAT mark carries (its class, its title, and whether the
 *  cross the failing state wears is inside it) instead of matching something
 *  else on the row. */
function chipElement(html, kind) {
  const i = html.indexOf(`data-lyrics-kind="${kind}"`);
  if (i < 0) return null;
  const start = html.lastIndexOf("<span", i);
  const end = html.indexOf("</span>", i);
  return end < 0 ? null : html.slice(start, end + "</span>".length);
}

const off = await render(false);
const on = await render(true);

// --------------------------------------------------------------------------- #
// 1) the setting OFF (the shipped default): plain is the failing state
// --------------------------------------------------------------------------- #
const offPlain = chipElement(off.html, "plain");
const offSynced = chipElement(off.html, "synced");
check("setting off: the plain track renders a kind chip", !!offPlain);
check("setting off: the synced track renders a kind chip", !!offSynced);
if (offPlain) {
  check("setting off: the plain chip is the app's failing mark",
        offPlain.includes("data-lyrics-fail") && /bg-red-900\/50/.test(offPlain)
        && /text-red-300/.test(offPlain), offPlain);
  check("setting off: …and the cross the failing state wears is inside it",
        /<svg/.test(offPlain), offPlain);
  check("setting off: …beside the word the kind is called",
        offPlain.includes("Plain"), offPlain);
  check("setting off: the reason names the setting",
        offPlain.includes("Accept plain (unsynced) lyrics"), offPlain);
  check("setting off: no neutral plain wording is claimed instead",
        !offPlain.includes("Untimed lyrics"), offPlain);
}
if (offSynced) {
  check("setting off: the synced chip is not a failing mark",
        !offSynced.includes("data-lyrics-fail"), offSynced);
  check("setting off: the synced chip says which way it is synced",
        offSynced.includes("Timed lyrics"), offSynced);
}

// --------------------------------------------------------------------------- #
// 2) the setting ON: plain is simply plain
// --------------------------------------------------------------------------- #
const onPlain = chipElement(on.html, "plain");
const onSynced = chipElement(on.html, "synced");
check("setting on: the plain chip still renders", !!onPlain);
if (onPlain) {
  check("setting on: the plain chip is NOT a failing mark",
        !onPlain.includes("data-lyrics-fail") && !/bg-red-900/.test(onPlain), onPlain);
  check("setting on: …and it is the neutral kind chip the other facts wear",
        /bg-zinc-800/.test(onPlain), onPlain);
  check("setting on: the neutral plain hint replaces the reason",
        onPlain.includes("Untimed lyrics") && !onPlain.includes("Accept plain (unsynced) lyrics"),
        onPlain);
}
if (onSynced) {
  check("setting on: the synced chip is unchanged",
        !onSynced.includes("data-lyrics-fail") && onSynced.includes("Timed lyrics"), onSynced);
}

// --------------------------------------------------------------------------- #
// 3) the words a reader sees, and the track with NO lyrics
// --------------------------------------------------------------------------- #
check("both settings: the word Synced is rendered", off.text.includes("Synced") && on.text.includes("Synced"));
check("both settings: the word Plain is rendered", off.text.includes("Plain") && on.text.includes("Plain"));
// One chip per track THAT HAS lyrics: the lyric-less track renders none, at
// either setting (a missing lyric is a different state with its own
// affordance — "Plain" would state lyrics it does not have).
const count = (html) => (html.match(/data-lyrics-kind="/g) || []).length;
check("setting off: exactly the two tracks with lyrics wear a kind chip", count(off.html) === 2, String(count(off.html)));
check("setting on: exactly the two tracks with lyrics wear a kind chip", count(on.html) === 2, String(count(on.html)));

await server.close();

if (failures.length) {
  console.error(`\n[lyrics-kind] ${failures.length} of ${checks} checks FAILED`);
  for (const f of failures) console.error(`  FAIL ${f}`);
  process.exit(1);
}
console.log(`ok  the album's tracklist says which KIND each track's lyrics are — ` +
  `Synced / Plain, the plain one failing exactly while lyrics_allow_plain is off ` +
  `and neutral while it is on, and no kind at all for a track without lyrics ` +
  `(${checks} checks)`);
