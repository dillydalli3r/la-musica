#!/usr/bin/env node
/* WHERE the lyrics KIND may be said — one track's own details, never a list.
 *
 * The owner asked every surface that says whether a track HAS lyrics to say
 * WHICH KIND they are, and to show a plain lyric as a FAILING state while the
 * user's own setting says plain is not acceptable. Then the owner looked at
 * the ALBUM TRACKLIST — rows of `# | Title | Genre | Dur | Bitrate | DR` with a
 * green `Synced` / red `✗ Plain` chip beside every title — and said the app
 * should only say synced or plain in the import menu and the track details.
 * So the mark's HOME is a surface about ONE track's own details:
 *
 *   * the import wizard's Lyrics step,
 *   * the track page's header and its lyrics pane (`LyricsViewer`),
 *   * the track's own readout (`TrackDetails`) and its lyrics manager
 *     (`LyricsManagerModal`);
 *
 * and it is GONE from every surface where a track appears in a LIST — the
 * album page's track rows, and the album's own readout (`AlbumDetails`, whose
 * "Lyrics" row counted the kinds too). This check renders each of those for
 * real and pins both halves, so the clutter cannot come back unnoticed:
 *
 *   absent   → the album page's tracklist and the album readout carry no kind
 *              chip at all, at either setting (with a negative control: the
 *              same detection DOES find the chip on the surfaces that keep it,
 *              and FAILS on the album markup with one chip spliced into it);
 *   present  → the wizard's Lyrics step, the track page, the track's own
 *              readout, and its lyrics pane/manager render one chip per track
 *              (or per text the pane holds) THAT HAS lyrics: "Synced" for a
 *              timed one, and "Plain" — the app's failing mark (red chip, the
 *              cross, `data-lyrics-fail`) with a reason naming the setting —
 *              exactly while `lyrics_allow_plain` is off, neutral while it is
 *              on, and NOTHING for a track with no lyrics at all.
 *
 * The facts behind that are untouched: the payload field `lyrics_kind`
 * ("synced" | "plain" | null), stamped server-side (mlo.lyrics
 * .stored_lyrics_kind → mlo.grader) and pinned by tools/test_lyrics_kind.py;
 * and `lyrics_allow_plain` (off by default, mlo/config.py), the user's own
 * statement that untimed lyrics are acceptable. This is a presentation change:
 * the server's field, the derivation and the rule are the same as before.
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

// The app's modals (Modal.tsx, which every details surface and the lyrics
// manager render through) portal into `document.body`, and the server renderer
// refuses portals outright. Rendering them INLINE is the one change this
// harness makes to the app's own render path: the portal's DESTINATION is
// dropped, nothing else — the same component tree, with the same markup the
// browser mounts into <body> — so the modal surfaces below can be asserted on
// without a browser.
//
// Patched HERE, before any module that could reach react-dom is imported: the
// named export a CJS module hands to ESM is snapshotted when that import is
// first evaluated, so a patch applied after it would be invisible.
const ReactDOM = webRequire("react-dom");
ReactDOM.createPortal = (children) => children;
globalThis.document = { body: {} };

const { createServer } = await import(
  `file://${path.join(webDir, "node_modules/vite/dist/node/index.js").replace(/\\/g, "/")}`);
const React = webRequire("react");
const { renderToString } = webRequire("react-dom/server");
const { MemoryRouter, Routes, Route } = webRequire("react-router-dom");
// react-query is imported as the ESM file the pages' own import resolves to
// (package exports: "import" → build/modern/index.js): two copies would mean
// two providers, and the page's useQuery would find no client.
const { QueryClient, QueryClientProvider } = await import(pathToFileURL(
  path.join(webDir, "node_modules/@tanstack/react-query/build/modern/index.js")).href);

// The app is a browser app: its store reaches for localStorage at import time.
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
  clear: () => store.clear(),
};
globalThis.window = globalThis;
// renderToString runs no effects, so nothing here may depend on a request
// landing: every query a surface fires is answered from the cache below, and
// anything it has not cached stays PENDING forever instead of reaching for the
// network (a relative URL has no meaning outside the browser anyway).
globalThis.fetch = () => new Promise(() => {});

const ALBUM = "F:/Music/Artists/Kind Artist/2020 - Kind Album";
const SYNCED = `${ALBUM}/01 - Timed.flac`;
const PLAIN = `${ALBUM}/02 - Untimed.flac`;
const NONE = `${ALBUM}/03 - Wordless.flac`;

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
  track(2, "02 - Untimed.flac", "plain"),
  track(3, "03 - Wordless.flac", null),
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

/** The library the import wizard reads — the album AND its tracks, which is
 *  what the per-track steps (lyrics among them) list. */
const LIBRARY = {
  folder: "F:/Music",
  artists: [{
    path: "F:/Music/Artists/Kind Artist",
    name: "Kind Artist",
    albums: [{ ...ALBUM_PAYLOAD }],
    aggregate: { album_count: 1, track_count: 3, pass_count: 3, total_checks: 3, grade_pct: 100 },
  }],
};

const cfg = (allowPlain) => ({ music_folder: "F:/Music", lyrics_allow_plain: allowPlain });

/** What the track page's own read reads (GET /api/tags): the track the page is
 *  about, its tags and its stored lyrics. */
const trackTags = (p, lyrics) => ({
  path: p,
  file: p.split("/").pop(),
  tags: { TITLE: "Untimed", ARTIST: "Kind Artist", ALBUM: "Kind Album", DISCNUMBER: "1", MEDIA: "CD" },
  tech: { length: 200, bitrate: 900, samplerate: 44100, bits: 16, format: "FLAC", channels: 2 },
  lyrics,
});

// A plain text and a timed one — the two kinds an EDITOR's own text can be
// (`lyricsKindOf`), which is what the pane and the manager mark.
const PLAIN_TEXT = "Just words\nand no timestamps at all";
const TIMED_TEXT = "[00:01.00]Just words\n[00:02.50]and no timestamps at all";

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

/** One surface rendered for real: `module` is loaded through Vite (so it is
 *  the app's own React), the queries it fires come from `cache`, and whatever
 *  it reads off its route comes from `url`. `build` renders the surface —
 *  a routed page needs its own `<Route>` (its `useParams` answers "" without
 *  one), a component is rendered directly with its props. */
async function render(module, url, cache, build) {
  const mod = await server.ssrLoadModule(module);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  for (const [key, data] of cache) qc.setQueryData(key, data);
  const html = renderToString(
    React.createElement(QueryClientProvider, { client: qc },
      React.createElement(MemoryRouter, { initialEntries: [url] },
        build ? build(mod) : React.createElement(mod.default))));
  return { html, text: flatten(html) };
}

/** How many kind chips a rendered surface carries at all. */
const kindCount = (html) => (html.match(/data-lyrics-kind="/g) || []).length;

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

const enc = encodeURIComponent;

/** The cache one surface reads: `[query key, data]` pairs, because a page's
 *  queries are answered — never fired — under SSR. */
const albumCache = (allowPlain) => [
  [["config"], cfg(allowPlain)],
  [["album", ALBUM], ALBUM_PAYLOAD],
];
const wizardCache = (allowPlain) => [
  [["config"], cfg(allowPlain)],
  [["library"], LIBRARY],
];

// --------------------------------------------------------------------------- #
// 1. the ALBUM TRACKLIST and the album readout: no kind anywhere
// --------------------------------------------------------------------------- #
const albumOff = await render("/src/pages/AlbumPage.tsx", `/album/${enc(ALBUM)}`,
  albumCache(false),
  (mod) => React.createElement(Routes, null,
    React.createElement(Route, { path: "/album/:path", element: React.createElement(mod.default) })));
const albumOn = await render("/src/pages/AlbumPage.tsx", `/album/${enc(ALBUM)}`,
  albumCache(true),
  (mod) => React.createElement(Routes, null,
    React.createElement(Route, { path: "/album/:path", element: React.createElement(mod.default) })));

check("album tracklist: the three rows render", albumOff.text.includes("Timed")
  && albumOff.text.includes("Untimed") && albumOff.text.includes("Wordless"));
check("album tracklist (setting off): not one kind chip in the tracklist",
      kindCount(albumOff.html) === 0, String(kindCount(albumOff.html)));
check("album tracklist (setting on): not one kind chip in the tracklist",
      kindCount(albumOn.html) === 0, String(kindCount(albumOn.html)));
check("album tracklist: the rows do not say Synced",
      !albumOff.text.includes("Synced") && !albumOn.text.includes("Synced"));
check("album tracklist: the rows do not claim Plain either",
      !albumOff.text.includes("Plain") && !albumOn.text.includes("Plain"),
      albumOff.text.slice(0, 400));

// The album's OWN readout (`AlbumDetails`, opened from the header's Details):
// the "Lyrics" row counts the tracks, and the counts line it used to append is
// gone with the chip it summarised.
const readoutOff = await render("/src/components/AlbumDetails.tsx", "/",
  albumCache(false),
  (mod) => React.createElement(mod.AlbumDetails, { album: ALBUM_PAYLOAD, onClose: () => {} }));
const readoutOn = await render("/src/components/AlbumDetails.tsx", "/",
  albumCache(true),
  (mod) => React.createElement(mod.AlbumDetails, { album: ALBUM_PAYLOAD, onClose: () => {} }));
check("album readout: the Lyrics row still says how many tracks hold lyrics",
      readoutOff.text.includes("2 of 2 tracks"), readoutOff.text.slice(0, 300));
check("album readout (setting off): no kind counts appended",
      !/\d+ synced · \d+ plain/.test(readoutOff.text));
check("album readout (setting on): no kind counts appended",
      !/\d+ synced · \d+ plain/.test(readoutOn.text));
check("album readout: no kind chip", kindCount(readoutOff.html) === 0 && kindCount(readoutOn.html) === 0);

// --------------------------------------------------------------------------- #
// 2. negative control: the absence checks are not vacuous
// --------------------------------------------------------------------------- #
// The same detection the album assertions run, over markup that DOES carry a
// chip: it must find it (so "absent" means absent, not blind) …
const spliced = `${albumOff.html}<span data-lyrics-kind="plain"></span>`;
check("negative control: the album-page absence check fails on markup carrying a chip",
      kindCount(albumOff.html) === 0 && kindCount(spliced) === 1, String(kindCount(spliced)));
check("negative control: the chip reader finds the plain chip it is given",
      chipElement(spliced, "plain") !== null && chipElement(albumOff.html, "plain") === null);

// --------------------------------------------------------------------------- #
// 3. the import wizard's LYRICS step: the mark lives here
// --------------------------------------------------------------------------- #
const wizardUrl = `/import?album=${enc(ALBUM)}&step=Lyrics`;
const wizardOff = await render("/src/pages/ImportWizard.tsx", wizardUrl,
  wizardCache(false));
const wizardOn = await render("/src/pages/ImportWizard.tsx", wizardUrl,
  wizardCache(true));

check("wizard lyrics step: it is the Lyrics step and lists the three tracks",
      wizardOff.text.includes("Lyrics") && wizardOff.text.includes("Timed")
      && wizardOff.text.includes("Untimed") && wizardOff.text.includes("Wordless"));
check("wizard (setting off): one chip per track that HAS lyrics — two, not three",
      kindCount(wizardOff.html) === 2, String(kindCount(wizardOff.html)));
check("wizard (setting on): the same two chips", kindCount(wizardOn.html) === 2, String(kindCount(wizardOn.html)));

const wizSynced = chipElement(wizardOff.html, "synced");
check("wizard (setting off): the synced track wears the timed chip", !!wizSynced);
if (wizSynced) {
  check("wizard: …not a failing mark", !wizSynced.includes("data-lyrics-fail"), wizSynced);
  check("wizard: …and it says which way it is synced", wizSynced.includes("Timed lyrics"), wizSynced);
}
const wizPlainOff = chipElement(wizardOff.html, "plain");
check("wizard (setting off): the plain track wears the failing mark", !!wizPlainOff);
if (wizPlainOff) {
  check("wizard (setting off): …red, with the cross inside it",
        wizPlainOff.includes("data-lyrics-fail") && /bg-red-900\/50/.test(wizPlainOff)
        && /text-red-300/.test(wizPlainOff) && /<svg/.test(wizPlainOff), wizPlainOff);
  check("wizard (setting off): …beside the word the kind is called", wizPlainOff.includes("Plain"), wizPlainOff);
  check("wizard (setting off): …with the reason naming the setting",
        wizPlainOff.includes("Accept plain (unsynced) lyrics"), wizPlainOff);
  check("wizard (setting off): …and no neutral plain wording instead",
        !wizPlainOff.includes("Untimed lyrics"), wizPlainOff);
}
const wizPlainOn = chipElement(wizardOn.html, "plain");
check("wizard (setting on): the plain chip renders neutral", !!wizPlainOn);
if (wizPlainOn) {
  check("wizard (setting on): …not a failing mark, and no cross",
        !wizPlainOn.includes("data-lyrics-fail") && !/<svg/.test(wizPlainOn), wizPlainOn);
  check("wizard (setting on): …in the tone the other facts wear", /bg-zinc-800/.test(wizPlainOn), wizPlainOn);
  check("wizard (setting on): …with the neutral hint instead of the reason",
        wizPlainOn.includes("Untimed lyrics") && !wizPlainOn.includes("Accept plain (unsynced) lyrics"),
        wizPlainOn);
}
// The step's own album-level line (kept: the step is about these lyrics), and
// the sentence that says what the failing marks mean there.
check("wizard (setting off): the step counts the kinds it is showing",
      /\d+ synced · \d+ plain/.test(wizardOff.text), wizardOff.text.slice(0, 300));
check("wizard (setting off): …and says those plain tracks fail the setting",
      wizardOff.text.includes("hold plain lyrics"), wizardOff.text.slice(0, 300));
check("wizard (setting on): the counts line is there but no failure is claimed",
      /\d+ synced · \d+ plain/.test(wizardOn.text) && !wizardOn.text.includes("hold plain lyrics"));

// --------------------------------------------------------------------------- #
// 4. the TRACK PAGE — one track's own details: header + lyrics pane
// --------------------------------------------------------------------------- #
const trackPage = (kind, allowPlain) => {
  const p = kind === "synced" ? SYNCED : kind === "plain" ? PLAIN : NONE;
  const lyrics = kind === "synced" ? TIMED_TEXT : kind === "plain" ? PLAIN_TEXT : "";
  return render("/src/pages/TrackPage.tsx", `/track/${enc(p)}`,
    [...albumCache(allowPlain), [["track-tags", p], trackTags(p, lyrics)]],
    (mod) => React.createElement(Routes, null,
      React.createElement(Route, { path: "/track/:path", element: React.createElement(mod.default) })));
};

const tpSynced = await trackPage("synced", false);
check("track page: the synced track's header wears the timed chip",
      !!chipElement(tpSynced.html, "synced") && tpSynced.text.includes("Timed lyrics"));
const tpPlainOff = await trackPage("plain", false);
const tpPlainOn = await trackPage("plain", true);
const tpNone = await trackPage("none", false);
check("track page (setting off): the plain track's header wears the failing mark",
      !!chipElement(tpPlainOff.html, "plain")
      && chipElement(tpPlainOff.html, "plain").includes("data-lyrics-fail")
      && tpPlainOff.text.includes("Accept plain (unsynced) lyrics"));
check("track page (setting on): …and the neutral chip instead",
      !!chipElement(tpPlainOn.html, "plain")
      && !chipElement(tpPlainOn.html, "plain").includes("data-lyrics-fail")
      && tpPlainOn.text.includes("Untimed lyrics"));
// Nothing for a track without lyrics: no chip from the header and none from
// the pane either (the pane marks the TEXT it holds, and it holds none).
check("track page: the lyric-less track wears no kind mark at all",
      kindCount(tpNone.html) === 0, String(kindCount(tpNone.html)));
check("track page: the synced track's lyrics PANE says timed too",
      (tpSynced.html.match(/data-lyrics-kind="synced"/g) || []).length >= 1);

// --------------------------------------------------------------------------- #
// 5. the track's own READOUT (TrackDetails) — the "Lyrics" row
// --------------------------------------------------------------------------- #
const details = (kind, allowPlain) => {
  const t = kind === "synced" ? TRACKS[0] : kind === "plain" ? TRACKS[1] : TRACKS[2];
  return render("/src/components/TrackDetails.tsx", "/",
    albumCache(allowPlain),
    (mod) => React.createElement(mod.default, { track: t, albumPath: ALBUM, onClose: () => {} }));
};

const detSynced = await details("synced", false);
const detPlainOff = await details("plain", false);
const detPlainOn = await details("plain", true);
const detNone = await details("none", false);
check("track details: the synced track's readout wears the timed chip",
      !!chipElement(detSynced.html, "synced") && detSynced.text.includes("Timed lyrics"));
check("track details (setting off): the plain track's readout wears the failing mark",
      !!chipElement(detPlainOff.html, "plain")
      && chipElement(detPlainOff.html, "plain").includes("data-lyrics-fail")
      && detPlainOff.text.includes("Accept plain (unsynced) lyrics"));
check("track details (setting on): …and the neutral chip instead",
      !!chipElement(detPlainOn.html, "plain")
      && !chipElement(detPlainOn.html, "plain").includes("data-lyrics-fail"));
check("track details: the lyric-less track's row says missing, with no chip",
      detNone.text.includes("missing") && kindCount(detNone.html) === 0, String(kindCount(detNone.html)));

// --------------------------------------------------------------------------- #
// 6. the track's own lyric surfaces: LyricsViewer and LyricsManagerModal
// --------------------------------------------------------------------------- #
const viewer = (text, allowPlain) => render("/src/components/LyricsViewer.tsx", "/",
  [[["config"], cfg(allowPlain)]],
  (mod) => React.createElement(mod.default,
    { path: PLAIN, initialLyrics: text, onChange: () => {}, allowPlain }));
const manager = (text, allowPlain) => render("/src/components/LyricsManagerModal.tsx", "/",
  [[["config"], cfg(allowPlain)]],
  (mod) => React.createElement(mod.default,
    { path: PLAIN, artist: "Kind Artist", track: "Untimed", currentText: text,
      allowPlain, onApplied: () => {}, onClose: () => {} }));

// The pane (LyricsViewer) marks the TEXT IT HOLDS, live — so it is asked for a
// TIMED text, the one it renders as a line list: the chip is there, in the
// pane's own header, and it says which way that text is synced. (A PLAIN text
// has no lines for that view to show — the pane opens its raw editor for it,
// a state only a running client reaches, since the server renderer runs no
// effects — so the plain half of this same gate is pinned on the lyrics
// MANAGER below: the same `allowPlain` prop and the same `lyricsKindOf` rule,
// reachable in one render.)
const viewTimed = await viewer(TIMED_TEXT, false);
const viewPlain = await viewer(PLAIN_TEXT, false);
check("lyrics pane: the timed text it holds wears the timed chip",
      !!chipElement(viewTimed.html, "synced") && viewTimed.text.includes("Timed lyrics"));
check("lyrics pane: …and its own header/controls are rendered with it",
      kindCount(viewTimed.html) === 1 && viewTimed.text.includes("Auto-import"),
      String(kindCount(viewTimed.html)));
check("lyrics pane: the plain text is handed to the pane, which still renders its editor",
      viewPlain.text.includes("Auto-import") && viewPlain.text.includes("Preview"));

const mgrTimed = await manager(TIMED_TEXT, false);
const mgrPlainOff = await manager(PLAIN_TEXT, false);
const mgrPlainOn = await manager(PLAIN_TEXT, true);
const mgrNone = await manager("", false);
check("lyrics manager: the track's timed text wears the timed chip",
      !!chipElement(mgrTimed.html, "synced"));
check("lyrics manager (setting off): the track's plain text wears the failing mark, reason and all",
      !!chipElement(mgrPlainOff.html, "plain")
      && chipElement(mgrPlainOff.html, "plain").includes("data-lyrics-fail")
      && mgrPlainOff.text.includes("Accept plain (unsynced) lyrics"));
check("lyrics manager (setting on): …and the neutral chip instead",
      !!chipElement(mgrPlainOn.html, "plain")
      && !chipElement(mgrPlainOn.html, "plain").includes("data-lyrics-fail"));
check("lyrics manager: a track with no text wears no kind chip",
      kindCount(mgrNone.html) === 0, String(kindCount(mgrNone.html)));

// --------------------------------------------------------------------------- #
// 7. the FULLSCREEN player's MODE — which pane is drawn for which lyrics state
// --------------------------------------------------------------------------- #
// The player's whole layout decision is `npLyricsMode` (NowPlayingView.tsx),
// taken from the module the view itself calls — not a copy of the rule. It is
// asked here in the same table the live DOM is held to by
// tools/check_fullscreen_player.cjs (the toggle and the pane follow THIS), and
// the five states are the five facts a track can present:
//
//   synced · plain · plain-refused · instrumental · none
//
// `allowPlain` is the user's `lyrics_allow_plain` (off by default); `undefined`
// is the config not yet read, which must not claim a refusal.
const np = await server.ssrLoadModule("/src/components/NowPlayingView.tsx");
const mode = (lyrics, instrumental, allowPlain, showLyrics, mdUp = false) =>
  np.npLyricsMode({ lyrics, instrumental, allowPlain, showLyrics, mdUp });
const SYNCED_TEXT = "[00:04.00]The first light\n[00:13.00]A kettle ticking";

// one state, one row: what it is, whether the pane may be drawn, and whether
// the phone's compact header (the LYRICS composition) is what a phone wears
const row = (label, m, state, drawable, paneOpen, phoneHeader) => {
  check(`player mode (${label}): the state is ${state}`, m.state === state, m.state);
  check(`player mode (${label}): the pane ${drawable ? "may" : "may NOT"} be drawn`,
    m.drawable === drawable, String(m.drawable));
  check(`player mode (${label}): the pane is ${paneOpen ? "open" : "closed"}`,
    m.paneOpen === paneOpen, String(m.paneOpen));
  check(`player mode (${label}): the phone ${phoneHeader ? "wears the compact header" : "gets the full composition"}`,
    m.compactHeader === phoneHeader, String(m.compactHeader));
};

row("timed lyrics, reader's pick on", mode(SYNCED_TEXT, false, false, true), "synced", true, true, true);
row("timed lyrics, reader's pick off", mode(SYNCED_TEXT, false, false, false), "synced", true, false, false);
row("untimed lyrics the install accepts", mode(PLAIN_TEXT, false, true, true), "plain", true, true, true);
row("untimed lyrics while lyrics_allow_plain is OFF",
  mode(PLAIN_TEXT, false, false, true), "plain-refused", false, false, false);
row("untimed lyrics before the config has been read",
  mode(PLAIN_TEXT, false, undefined, true), "plain", true, true, true);
row("instrumental, stored timed lyrics and all",
  mode(SYNCED_TEXT, true, false, true), "instrumental", false, false, false);
row("no lyrics at all", mode(null, false, false, true), "none", false, false, false);
row("a lyric-less text (a stamp and nothing else)",
  mode("[00:00.00]", false, false, true), "none", false, false, false);

// The pane follows the TRACK, not a remembered mode: one reader pick, five
// tracks one after another, in the order the fixture album plays them. This is
// the owner's report stated as a sequence — the pane used to stay drawn (and
// the phone kept the header row it makes room for) when the next track had
// nothing to fill it with.
const sweep = [
  [SYNCED_TEXT, false, false, true],
  [PLAIN_TEXT, false, false, false],
  [SYNCED_TEXT, true, false, false],
  [null, false, false, false],
  [SYNCED_TEXT, false, false, true],
].map(([lyrics, instrumental, allowPlain, want]) => {
  const m = mode(lyrics, instrumental, allowPlain, true);
  return m.paneOpen === want && m.drawable === want && m.compactHeader === want;
});
check("player mode: one reader pick, five tracks in a row — the pane (and the phone's header) follow each track's own lyrics",
  sweep.every(Boolean), sweep.join(","));

// The desktop half: the pane is the right-hand column at every drawable state,
// and the compact header — a PHONE composition — never appears at md and up.
const wide = [
  mode(SYNCED_TEXT, false, false, true, true),
  mode(PLAIN_TEXT, false, true, true, true),
  mode(PLAIN_TEXT, false, false, true, true),
  mode(SYNCED_TEXT, true, false, true, true),
  mode(null, false, false, true, true),
];
check("player mode: no compact header at md and up, whatever the track holds",
  wide.every((m) => !m.compactHeader), wide.map((m) => m.compactHeader).join(","));
check("player mode: the desktop pane is the same pane — open for timed and accepted-untimed lyrics only",
  wide[0].paneOpen && wide[1].paneOpen && !wide[2].paneOpen && !wide[3].paneOpen && !wide[4].paneOpen,
  wide.map((m) => m.paneOpen).join(","));
// The toggle is drawn exactly where the pane can be filled: one flag, so a
// control can never promise a pane the track cannot fill (the red-crossed
// plain text is the state that used to get both).
check("player mode: the toggle is offered exactly where the pane is drawable",
  mode(SYNCED_TEXT, false, false, true).drawable
  && !mode(PLAIN_TEXT, false, false, true).drawable
  && !mode(null, false, false, true).drawable);

await server.close();

if (failures.length) {
  console.error(`\n[lyrics-kind] ${failures.length} of ${checks} checks FAILED`);
  for (const f of failures) console.error(`  FAIL ${f}`);
  process.exit(1);
}
console.log(`ok  the lyrics kind is said on ONE track's own surfaces only — the wizard's ` +
  `Lyrics step, the track page, its readout and its lyrics pane/manager say ` +
  `Synced / Plain (the plain one failing exactly while lyrics_allow_plain is off, ` +
  `nothing at all without lyrics) — and no album LIST does: the album page's ` +
  `tracklist and the album readout carry no kind at all. The FULLSCREEN player's ` +
  `mode is the same rule asked of a track's own lyrics: the pane (and the phone's ` +
  `header row, which exists to make room for it) is drawn only where there are ` +
  `words this install shows — timed, or untimed and accepted — and a refused ` +
  `plain text, an instrumental and a lyric-less track each draw neither (${checks} checks)`);
