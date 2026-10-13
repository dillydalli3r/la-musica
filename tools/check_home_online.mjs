#!/usr/bin/env node
/* Home's ONLINE section, rendered for real.
 *
 * The owner asked Home to stop being only about grading and to pull
 * recommendations / trending from the online sources. This check drives the
 * page itself (Vite SSR, seed the react-query cache, flatten the markup) and
 * proves the three things that change is:
 *
 *   1. the section RENDERS the rows the providers answered with — the three
 *      shelves (Trending now / New & notable / For you) and one row of each
 *      stubbed answer are on the page, drawn through the shared DiscoverRow
 *      inside the shared RecommendShelf, exactly like the Discover pages;
 *   2. the LOCAL shelves are untouched and never held up: with every online
 *      query in its ERROR state (a provider outage / an offline install), the
 *      library shelf and its album still render and no Online heading is left
 *      over an empty box;
 *   3. the GATE holds: `discovery_enabled: false` (the Discovery tab) makes the
 *      whole section absent even when the online answers are cached, and a
 *      config that is still loading (or absent) fires no provider request.
 *
 * It also exercises the two rules that live in the REQUEST rather than the
 * render, by calling the page's own exported query functions:
 *   * Apple Music answering with rows is used as-is and Deezer is never asked;
 *   * Apple answering with NOTHING falls back to Deezer's chart.
 *
 * Payload: a stub file mapping the one answer each read wants —
 *   { home, config, charts_trending, charts_new, recommended }
 * — see tools/fixtures/home-online.json. The home/shelf rows in it are real
 * library albums lifted from tools/fixtures/library-az.json, so the local half
 * is a library row and not a hand-made lookalike.
 *
 * Run:  node tools/check_home_online.mjs [payload.json]
 * Exit codes: 0 pass, 1 a check failed (the failures are printed), 2 the
 * environment cannot run it (no web/node_modules, no payload).
 */
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const webDir = path.join(here, "..", "web");
const payloadPath = process.argv[2] || path.join(here, "fixtures", "home-online.json");

if (!existsSync(path.join(webDir, "node_modules"))) {
  console.error("[home-online] SKIP: web/node_modules is missing — run `npm --prefix web install`.");
  process.exit(2);
}
if (!existsSync(payloadPath)) {
  console.error(`[home-online] no payload at ${payloadPath}`);
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
// build/modern/index.cjs — two files, i.e. two providers). Same URL, same
// module instance.
const { QueryClient, QueryClientProvider } = await import(pathToFileURL(
  path.join(webDir, "node_modules/@tanstack/react-query/build/modern/index.js")).href);

// The page is a browser app: the globals its imports reach for (the toast
// store and the grid-size setting keep their state in localStorage, and i18n
// writes <html lang> when the locale is applied).
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
  clear: () => store.clear(),
};
globalThis.window = globalThis;
globalThis.document = { documentElement: {}, addEventListener() {}, removeEventListener() {} };
// No request ever leaves the harness: the render cases read seeded cache
// entries only, and a query left to LOAD is what this holds open (the loading
// case below). A provider must never be reached from a check.
globalThis.fetch = () => new Promise(() => {});

const payload = JSON.parse(readFileSync(payloadPath, "utf8"));
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
  checks++;
  if (!pass) failures.push(detail ? `${name} — ${detail}` : name);
};
/** React separates adjacent text nodes with `<!-- -->`; they are not content.
 *  HTML entities are turned back into the characters the reader sees, so a
 *  shelf titled "New & notable" is asserted as that and not as "New &amp;
 *  notable". */
const flatOf = (html) => html
  .replace(/<!-- -->/g, "")
  .replace(/&amp;/g, "&")
  .replace(/&#x27;/g, "'")
  .replace(/&quot;/g, '"')
  .replace(/\s+/g, " ");

try {
  // The page is rendered in English whatever the machine's own locale is: the
  // local shelf label is asserted by its English text, and a CI box with
  // LANG=de would otherwise fail it.
  const i18n = await server.ssrLoadModule("/src/lib/i18n.ts");
  i18n.applyConfigLocale({ ui_locale: "en" });
  const page = await server.ssrLoadModule("/src/pages/HomePage.tsx");
  const HomePage = page.default;
  const { HOME_ONLINE } = await server.ssrLoadModule("/src/lib/homeOnline.ts");
  const en = (await server.ssrLoadModule("/src/locales/en.ts")).default;

  const LOCAL_SHELF = en["home.shelf.recent"];      // "Recently added"
  const firstAlbum = payload.home.recent[0];
  const ALBUM_TITLE = String(firstAlbum.meta?.ALBUM || firstAlbum.path.split(/[\\/]/).pop());

  const render = ({ online = "seeded", config = payload.config } = {}) => {
    // No mount refetch and no retry: every case below is exactly the cache it
    // was handed. A query with no data still loads (that is the loading case),
    // and its fn is the never-resolving fetch above.
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchOnMount: false, retryOnMount: false, refetchOnWindowFocus: false } },
    });
    qc.setQueryData(["home"], payload.home);
    qc.setQueryData(["config"], config);
    if (online === "seeded") {
      qc.setQueryData(HOME_ONLINE.trending.queryKey, payload.charts_trending);
      qc.setQueryData(HOME_ONLINE.fresh.queryKey, payload.charts_new);
      qc.setQueryData(HOME_ONLINE.forYou.queryKey, payload.recommended);
    } else if (online === "error") {
      for (const q of [HOME_ONLINE.trending, HOME_ONLINE.fresh, HOME_ONLINE.forYou]) {
        qc.getQueryCache().build(qc, { queryKey: q.queryKey }, {
          status: "error",
          error: new Error("stubbed provider outage"),
          fetchStatus: "idle",
        });
      }
    }
    return flatOf(renderToString(
      React.createElement(QueryClientProvider, { client: qc },
        React.createElement(MemoryRouter, { initialEntries: ["/"] },
          React.createElement(HomePage)))
    ));
  };

  // ---- 1. the online answers are drawn, beside the library shelves ---------
  {
    const flat = render({ online: "seeded" });
    // The library half first: a real library album, through the same card it
    // always used.
    check(`the library shelf "${LOCAL_SHELF}" still renders`, flat.includes(LOCAL_SHELF));
    check(`its album "${ALBUM_TITLE}" is on the page`, flat.includes(ALBUM_TITLE));
    // The online half: the section heading and all three shelves.
    check("the Online section heading renders", flat.includes(">Online<"));
    check("shelf: Trending now", flat.includes("Trending now"));
    check("shelf: New & notable", flat.includes("New & notable"));
    check("shelf: For you", flat.includes("For you"));
    // A row of EACH shelf's own answer — the chart/recommendation rows the
    // providers returned, not a placeholder.
    check("Trending shows Apple's row", flat.includes("Chart Song One"));
    check("New & notable shows Deezer's row", flat.includes("Fresh Album One"));
    check("For you shows the library-seeded row", flat.includes("Suggested Album"));
    check("the row's own source is named (the DiscoverRow chrome)",
      flat.includes("Apple Music") && flat.includes("Deezer"));
    // Order: the Online section sits BELOW the library shelves (after the
    // shelf it was asked to sit under), never in place of them.
    check("the Online section is below the library shelf",
      flat.indexOf(">Online<") > flat.indexOf(LOCAL_SHELF),
      `${flat.indexOf(">Online<")} vs ${flat.indexOf(LOCAL_SHELF)}`);
  }

  // ---- 2. an online outage leaves the local shelves exactly as they were ---
  {
    const flat = render({ online: "error" });
    check("error: the library shelf still renders", flat.includes(LOCAL_SHELF));
    check("error: its album is still on the page", flat.includes(ALBUM_TITLE));
    check("error: no Online heading is left over nothing", !flat.includes(">Online<"));
    check("error: no empty online shelf is drawn",
      !flat.includes("Trending now") && !flat.includes("New & notable") && !flat.includes("For you"));
    check("error: no chart row leaked in",
      !flat.includes("Chart Song One") && !flat.includes("Fresh Album One") && !flat.includes("Suggested Album"));
  }

  // ---- 3. discovery_enabled off makes the whole section absent -------------
  {
    const flat = render({ online: "seeded", config: { ...payload.config, discovery_enabled: false } });
    check("discovery off: the library shelf still renders", flat.includes(LOCAL_SHELF));
    check("discovery off: the Online section is absent", !flat.includes(">Online<"));
    check("discovery off: a cached online row is NOT drawn",
      !flat.includes("Chart Song One") && !flat.includes("Suggested Album"));
  }

  // ---- 4. a request in flight shows its own loading state ------------------
  {
    const flat = render({ online: "none" });
    check("loading: the library shelf renders immediately", flat.includes(LOCAL_SHELF));
    check("loading: the Online heading is up", flat.includes(">Online<"));
    const spinners = (flat.match(/Asking the online providers/g) || []).length;
    check("loading: each online shelf carries its own loading line", spinners === 3, `saw ${spinners}`);
  }

  // ---- 5. the request rules: Apple first, Deezer only as a fallback --------
  {
    const resp = (body) => ({
      ok: true, status: 200, statusText: "OK",
      headers: { get: () => null },
      json: async () => body,
    });
    const asked = [];
    const emptyApple = { ...payload.charts_trending, items: [] };
    const deezerTracks = {
      ...payload.charts_trending,
      source: "deezer",
      sources: [{ id: "deezer", label: "Deezer", periods: ["all"], supports_period: true, needs: [], missing: [], ready: true }],
      items: payload.charts_trending.items.map((r) => ({ ...r, source: "deezer", source_label: "Deezer" })),
    };

    // Apple WITH rows: Deezer is never asked.
    globalThis.fetch = async (url) => {
      asked.push(String(url));
      return resp(payload.charts_trending);
    };
    const withRows = await HOME_ONLINE.trending.queryFn();
    check("Apple with rows is used as-is", withRows.items.length > 0);
    check("Apple with rows never asks Deezer",
      asked.some((u) => u.includes("source=itunes")) && !asked.some((u) => u.includes("source=deezer")),
      JSON.stringify(asked));

    // Apple with NOTHING: Deezer's chart stands in.
    asked.length = 0;
    globalThis.fetch = async (url) => {
      asked.push(String(url));
      return resp(String(url).includes("source=itunes") ? emptyApple : deezerTracks);
    };
    const fell = await HOME_ONLINE.trending.queryFn();
    check("Apple answering nothing falls back to Deezer",
      fell.source === "deezer" && fell.items.length > 0 && fell.items.every((r) => r.source === "deezer"),
      JSON.stringify(fell.items.map((r) => r.source)));
    check("the fallback really did ask both",
      asked.some((u) => u.includes("source=itunes")) && asked.some((u) => u.includes("source=deezer")),
      JSON.stringify(asked));
  }
} finally {
  await server.close();
}

if (failures.length) {
  console.error("[home-online] FAILED:");
  for (const f of failures) console.error("  - " + f);
  process.exit(1);
}
console.log(`ok  Home's Online section renders the providers' rows, leaves the local ` +
  `shelves untouched when they fail, and is gone when discovery is off (${checks} checks)`);
process.exit(0);