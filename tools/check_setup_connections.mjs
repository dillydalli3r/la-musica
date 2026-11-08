#!/usr/bin/env node
/* The setup wizard's Connections step, and the how-to both Connect panels
 * carry.
 *
 * The owner's ask: the first-run wizard must cover "logging into musicbrainz /
 * last.fm", and a person must not have to leave the app to find how to log in.
 * The wizard was shortened from seven steps to five, and the one that used to
 * be "Sources, keys & cookies" is gone as a step of its own: its Sources panel
 * now sits on Connections, together with the two Connect flows and the
 * Soulseek login. This check pins the merged contract on the code that actually
 * draws it:
 *
 *   * `SETUP_STEPS` holds ONE step, right after "Account" (`password`, the
 *     mandatory one) and before "Tools" (`dependencies` — the credentials come
 *     FIRST, so an optional step can be read and skipped before any download is
 *     explained), labelled "Connections", whose title says it is optional and
 *     whose TWO groups are "Scrobbling & submissions" and "Soulseek (managed
 *     slskd)" — so the Last.fm shared secret, the MusicBrainz client id +
 *     secret AND the Soulseek login are editable ON the step
 *     (test_setup_coverage.py owns the one-step-per-group rule).
 *   * the step asks the three Soulseek keys BY NAME (they render above the
 *     fold, where a first run cannot miss them), and every one of the step's
 *     fields passes `cfgError` on a blank draft, so the page's own `badCount`
 *     is 0 and Continue is enabled with nothing filled in — the step stays
 *     skippable.
 *   * the ScrobblePanel and the MbLoginPanel each render the shared
 *     ConnnectGuide above their controls — three numbered lines, with ONE
 *     button per line that names a page, opened through `openExternal` (the
 *     shell's own external-link route). Rendered for real, through Vite, with
 *     the answer already in the query cache (renderToString runs no effects, so
 *     nothing may depend on a request landing).
 *   * SetupPage draws the Sources credential panel AND BOTH Connect panels
 *     under `step.panel === "connections"`. SetupPage itself cannot be
 *     server-rendered to a step's content — its draft is seeded in an effect
 *     and renderToString runs none — so that one claim is read from the page's
 *     source, exactly as tools/check_stack_gates reads its page.
 *
 * Run:  node tools/check_setup_connections.mjs
 * Exit codes: 0 pass, 1 a check failed, 2 the environment cannot run it.
 */
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const webDir = path.join(here, "..", "web");
if (!existsSync(path.join(webDir, "node_modules"))) {
  console.error("[setup-connections] web/node_modules is missing — run `npm --prefix web install`.");
  process.exit(2);
}

// Loaded FROM web/ so the harness and the page share ONE React instance (see
// tools/check_queue_view.mjs for the reason).
const webRequire = createRequire(path.join(webDir, "package.json"));
const { createServer } = await import(
  `file://${path.join(webDir, "node_modules/vite/dist/node/index.js").replace(/\\/g, "/")}`);
const React = webRequire("react");
const { renderToString } = webRequire("react-dom/server");
const { QueryClient, QueryClientProvider } = await import(pathToFileURL(
  path.join(webDir, "node_modules/@tanstack/react-query/build/modern/index.js")).href);

// The pages reach for these at import time (the toast store keeps state in
// localStorage; lib/api reads `window`).
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
  clear: () => store.clear(),
};
globalThis.window = globalThis;

const failures = [];
let checks = 0;
const check = (name, pass, detail = "") => {
  checks++;
  console.log(`  ${pass ? "ok  " : "FAIL"} ${name}${pass || !detail ? "" : `  ${detail}`}`);
  if (!pass) failures.push(name);
};

const server = await createServer({
  configFile: path.join(webDir, "vite.config.ts"),
  root: webDir,
  server: { middlewareMode: true },
  appType: "custom",
  logLevel: "error",
});

/** Render a component to its flattened text (React's text-node markers and the
 *  markup are not content). */
function render(Component, props, cache) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  for (const [key, data] of cache) qc.setQueryData(key, data);
  const html = renderToString(
    React.createElement(QueryClientProvider, { client: qc }, React.createElement(Component, props)));
  return html
    .replace(/<!-- -->/g, "")
    .replace(/&amp;/g, "&")
    .replace(/&#x27;|&#39;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/\s+/g, " ");
}

try {
  const meta = await server.ssrLoadModule("/src/lib/configMeta.ts");
  const steps = meta.SETUP_STEPS;
  const at = (panel) => steps.findIndex((s) => s.panel === panel);
  const iPassword = at("password");
  const iConn = at("connections");
  const iDeps = at("dependencies");
  // The panels the merged step replaced: neither may survive as a step of its
  // own, or the Sources panel and the Soulseek login would be asked twice.
  const iSources = at("sources");
  const iSlskd = at("slskd");

  console.log("\n== the step list ==");
  check("a step with panel \"connections\" exists", iConn >= 0);
  check("...right after the mandatory Account step and before Tools",
    iPassword >= 0 && iConn === iPassword + 1 && iDeps === iConn + 1,
    `password=${iPassword} connections=${iConn} dependencies=${iDeps}`);
  check("the old Sources and Soulseek steps are gone (merged into Connections)",
    iSources === -1 && iSlskd === -1, `sources=${iSources} slskd=${iSlskd}`);
  const step = steps[iConn];
  check("its rail label is \"Connections\"", step.label === "Connections", step.label);
  check("its title says it is optional", /optional/i.test(step.title), step.title);
  check("it renders the two groups that hold the credentials",
    JSON.stringify(step.groups) === JSON.stringify(["Scrobbling & submissions", "Soulseek (managed slskd)"]),
    JSON.stringify(step.groups));
  check("it asks the Soulseek login by name (hoisted above the fold)",
    JSON.stringify(step.ask)
      === JSON.stringify(["soulseek_username", "soulseek_password", "soulseek_share_library"]),
    JSON.stringify(step.ask));
  check("its blurb routes both services to their credential page",
    step.blurb.includes("last.fm/api/account/create")
      && step.blurb.includes("musicbrainz.org/account/applications"),
    step.blurb.slice(0, 120));

  console.log("\n== the credentials proven on the step ==");
  const fields = meta.stepFields(step);
  const keys = fields.map((f) => f.k);
  for (const key of ["lastfm_api_secret", "musicbrainz_client_id", "musicbrainz_client_secret",
    "soulseek_username", "soulseek_password", "soulseek_share_library"]) {
    check(`${key} is editable on the step`, keys.includes(key));
  }
  const errors = fields
    .map((f) => [f.k, meta.cfgError(f, undefined)])
    .filter(([, msg]) => msg);
  check("every field passes `cfgError` on a blank draft (the page's own Continue gate)",
    errors.length === 0,
    errors.map(([k, m]) => `${k}: ${m}`).join("; "));

  console.log("\n== the panels, rendered for real ==");
  const { default: ScrobblePanel } = await server.ssrLoadModule("/src/components/ScrobblePanel.tsx");
  const { default: MbLoginPanel } = await server.ssrLoadModule("/src/components/MbLoginPanel.tsx");
  const lastfm = render(ScrobblePanel, {},
    [["lastfmStatus", { connected: false, key: "", secret: "", queued: 0 }],
     ["config", { lastfm_scrobble_enabled: true }]]);
  const mb = render(MbLoginPanel, { ids: [], kind: "album" },
    [["mbAuth", { connected: false, username: "", client_id_set: false, submit_enabled: true, expires_at: 0 }],
     ["library", { artists: [] }]]);

  const guide = (html, name, pageLabel, pageUrl) => {
    console.log(`\n  -- ${name} --`);
    // The ordering is the `<ol>`/`list-decimal` (the numbers are CSS, not
    // text), so what is asserted is the three list items themselves.
    const items = (html.match(/<li>/g) || []).length;
    check(`${name}: the how-to is a numbered list of 3 steps`, items === 3, `${items} <li>`);
    check(`${name}: one clickable link to the credential page`,
      html.includes(`>${pageLabel}</button>`) && html.includes(`title="${pageUrl}"`),
      pageUrl);
    check(`${name}: the Connect control is drawn`, html.includes("Connect"), "");
  };
  guide(lastfm, "ScrobblePanel", "Open last.fm/api/account/create", "https://www.last.fm/api/account/create");
  check("ScrobblePanel names the API key AND the shared secret page",
    lastfm.includes("API key") && lastfm.includes("shared secret"), "");
  guide(mb, "MbLoginPanel", "Open musicbrainz.org/account/applications", "https://musicbrainz.org/account/applications");
  check("MbLoginPanel names the out-of-band redirect URI",
    mb.includes("urn:ietf:wg:oauth:2.0:oob"), "");
  check("MbLoginPanel still offers Connect while nothing is saved",
    mb.includes("Connect") && !mb.includes("connected as"), "");

  console.log("\n== how SetupPage draws it ==");
  const pageSrc = readFileSync(path.join(webDir, "src", "pages", "SetupPage.tsx"), "utf8");
  // `&&` (the JSX branch) rather than a bare match: the step's icon row also
  // names this panel with a ternary, and slicing from THAT would leave the
  // branch's own panels outside the window.
  const branchAt = pageSrc.indexOf('{step.panel === "connections" &&');
  check("the page has a `step.panel === \"connections\"` branch", branchAt >= 0);
  const branch = pageSrc.slice(branchAt < 0 ? 0 : branchAt);
  const end = branch.indexOf("step.panel ===", 10);
  const branchBody = end > 0 ? branch.slice(0, end) : branch;
  check("...that draws the Sources credential panel (moved off the old Keys step)",
    /<SourcesPanel\b[^>]*\baskKeys\b/.test(branchBody),
    branchBody.replace(/\s+/g, " ").slice(0, 160));
  check("...and BOTH Connect panels",
    /<ScrobblePanel\b/.test(branchBody) && /<MbLoginPanel\b/.test(branchBody),
    branchBody.replace(/\s+/g, " ").slice(0, 160));
  check("the page's Continue is gated by `badCount` alone (which is 0 here)",
    /disabled=\{busy \|\| badCount > 0\}/.test(pageSrc));

  const guideSrc = readFileSync(path.join(webDir, "src", "components", "ConnectGuide.tsx"), "utf8");
  check("the guide opens its page through the app's external-link helper",
    /openExternal\(step\.url!\)/.test(guideSrc) && /from "\.\.\/lib\/externalLinks"/.test(guideSrc));
} finally {
  await server.close();
}

if (failures.length) {
  console.error(`\nsetup-connections: ${checks - failures.length}/${checks} checks passed`);
  console.error(`${failures.length} failure(s):`);
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log(`\nok  the setup wizard's Connections step and both Connect guides render ` +
  `(${checks} checks)`);
process.exit(0);