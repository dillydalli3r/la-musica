#!/usr/bin/env node
/* The Soulseek page's PORTS panel, rendered for real.
 *
 * The panel shows a DERIVED obfuscated port beside the listen port — the listen
 * port + 1, the number a Soulseek client (SoulseekQt, Nicotine+) would CALL this
 * host's obfuscated port. It is INFORMATION, not an instruction: slskd implements
 * no obfuscated route (Soulseek.NET logs in with the plain listen_port only and
 * advertises no obfuscated port), so nothing listens on that number and nothing
 * has to be forwarded to it. This check pins BOTH directions and the wording that
 * keeps it a fact rather than a to-do:
 *   - when the payload carries a derived port, the panel reads
 *     "obfuscated <listen+1> (slskd does not listen there)" beside the listen
 *     port, with a tooltip saying it is information, not a route;
 *   - when the listen port is unknown/0, nothing is shown at all.
 *
 * Payload-driven and serverless: the REAL page renders on a stub query cache
 * (the payload is the `/api/soulseek/status` shape `server/soulseek.py`'s
 * `port_status_payload` fills), exactly like tools/check_queue_view.mjs.
 *
 * Run:  node tools/check_soulseek_ports.mjs <status-payload.json>
 * Exit 0 pass · 1 failed · 2 cannot run here (node or web/node_modules missing).
 */
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const webDir = path.join(here, "..", "web");
const payloadPath = process.argv[2];
if (!payloadPath) {
  console.error("[ports] usage: node tools/check_soulseek_ports.mjs <payload.json>");
  process.exit(2);
}
if (!existsSync(payloadPath) || !existsSync(path.join(webDir, "node_modules"))) {
  console.error("[ports] cannot run here: the payload or web/node_modules is missing");
  process.exit(2);
}

// Loaded FROM web/ so the harness and the page share ONE React instance: the
// page's own imports are externalized by Vite and resolve to the project's own
// node_modules, and Node caches a module by resolved path — two copies of React
// would break every hook in the page.
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
const state = payload.listen_port_state || {};
const port = Number(payload.listen_port) || Number(state.listen_port) || 0;
const obfuscated = Number(state.obfuscated_port) || 0;

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
  qc.setQueryData(["soulseekStatus"], payload);
  const html = renderToString(
    React.createElement(QueryClientProvider, { client: qc },
      React.createElement(MemoryRouter, { initialEntries: ["/soulseek?tab=settings"] },
        React.createElement(page.default)))
  );
  // The rendered markup as the words on the panel: React's <!-- --> separators
  // and the tags (and the tooltips riding in their attributes) dropped,
  // whitespace collapsed — so "obfuscated" and the number it names are read
  // across the spans that style them, wherever the components put them.
  const flat = html.replace(/<!-- -->/g, "").replace(/<[^>]*>/g, " ").replace(/\s+/g, " ");

  const problems = [];
  const check = (what, ok, detail = "") => {
    if (!ok) problems.push(`${what}${detail ? `  ${detail}` : ""}`);
  };

  // The panel really is the one under test: it names the live (Soulseek) port.
  check("the Ports panel rendered", flat.includes("Listen (Soulseek)"), flat.slice(0, 400));

  if (obfuscated > 0) {
    // The derivation is the contract: listen port + 1, shown as information.
    check("the derived port is the listen port + 1",
          port > 0 && obfuscated === port + 1, `listen ${port} → obfuscated ${obfuscated}`);
    // The readout is beside the listen port and says the number out loud.
    check("the panel shows the derived obfuscated port beside the listen port",
          flat.includes(`obfuscated ${obfuscated}`), flat.slice(0, 800));
    check("...and marks it as one slskd does not listen on",
          flat.includes(`obfuscated ${obfuscated} (slskd does not listen there)`),
          flat.slice(0, 800));
    // The wording keeps it a fact: information, not a route to reach.
    const title = (html.match(/title="([^"]*obfuscated[^"]*)"/) || [])[1] || "";
    check("...with a tooltip saying it is information, not a route",
          /information, not a route/i.test(title), title.slice(0, 220));
    check("...and that slskd advertises no obfuscated port",
          /advertises no obfuscated port/i.test(title), title.slice(0, 220));
    check("...and that nothing needs forwarding to it",
          /nothing needs forwarding/i.test(title), title.slice(0, 220));
  } else {
    // Unknown/0 listen port: nothing to show, and nothing invented.
    check("no obfuscated port is shown when the listen port is unknown/0",
          !flat.includes("obfuscated "), flat.slice(0, 800));
  }

  if (problems.length) {
    console.error("[ports] MISSING: " + JSON.stringify(problems));
    console.error(flat.replace(/></g, ">\n<").split("\n")
      .filter((l) => /obfuscated|Listen \(/.test(l)).slice(0, 20).join("\n"));
    process.exit(1);
  }
  console.log(`ok  the Soulseek ports panel shows the derived obfuscated port ` +
    `(listen ${port} → obfuscated ${obfuscated}) as information, and nothing ` +
    `when it is unknown`);
} finally {
  await server.close();
}