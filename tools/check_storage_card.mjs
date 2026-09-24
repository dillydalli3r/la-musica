#!/usr/bin/env node
/* The Storage card's THREE figures, rendered for real.
 *
 * The card (web/src/components/StorageCard.tsx) answers one question in one
 * place: how big the library is, how big the app is, and what the two add up
 * to. `/api/storage` (server/api_storage.py) is one payload; this check renders
 * the ACTUAL card with it — through Vite, in memory, no build output — and
 * asserts what a user sees:
 *
 *   * three metric rows, in this order and with these labels: Library (with
 *     its `(N audio)` subtotal), App (renamed from "App total" — with a row
 *     below it also called "total", the pair read as the same figure) and
 *     Total (App + Library);
 *   * the arithmetic HOLDS ON SCREEN: the Total row is the two rows above it
 *     added up, formatted the way they are, so the card cannot show a total
 *     that disagrees with its own halves. It is the byte totals that are
 *     summed — never the library's audio SUBTOTAL, which is a different
 *     quantity (both fixtures are built so that summing it would print a
 *     different number, and the check fails if it ever appears);
 *   * the App row keeps its file count and its state · bin · transfers · tools
 *     breakdown, and the volume's own used/free readout stays;
 *   * a figure nobody could take (an unmeasurable app folder, no music folder)
 *     leaves the TOTAL unknown too, rather than summing the half that is
 *     known.
 *
 * The two payloads are synthetic and live in tools/fixtures/: the check owns
 * its fixtures because the arithmetic is what is under test — a payload built
 * by the server would have to be kept consistent with the card separately.
 * They differ in magnitude and in the units the SUM lands in (fixture A adds
 * two GB figures to a GB total; fixture B adds two MB figures to a total that
 * crosses into GB), which is what catches a total derived from the printed
 * strings rather than from the bytes.
 *
 * Run:  node tools/check_storage_card.mjs [payload.json …]
 *       (default: tools/fixtures/storage-card-a.json and -b.json)
 * Exit codes: 0 pass, 1 a check failed (the failures are printed), 2 the
 * environment cannot run it (no web/node_modules, no payload).
 */
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const webDir = path.join(here, "..", "web");

const args = process.argv.slice(2);
const payloadPaths = args.length
  ? args
  : [
      path.join(here, "fixtures", "storage-card-a.json"),
      path.join(here, "fixtures", "storage-card-b.json"),
    ];

if (!existsSync(path.join(webDir, "node_modules"))) {
  console.error("[storage-card] SKIP: web/node_modules is missing — run npm install in web/");
  process.exit(2);
}
for (const p of payloadPaths) {
  if (!existsSync(p)) {
    console.error(`[storage-card] no payload at ${p}`);
    process.exit(2);
  }
}

// Loaded FROM web/ so the harness and the card share ONE React instance: the
// card's own imports are externalized by Vite and resolve to the project's own
// node_modules, and Node caches a module by resolved path — two copies of React
// would break every hook in the card.
const webRequire = createRequire(path.join(webDir, "package.json"));
const { createServer } = await import(
  `file://${path.join(webDir, "node_modules/vite/dist/node/index.js").replace(/\\/g, "/")}`);
const React = webRequire("react");
const { renderToString } = webRequire("react-dom/server");
// react-query is imported as the ESM file the CARD's own import resolves to
// (package exports: "import" -> build/modern/index.js, "require" ->
// build/modern/index.cjs — two files, i.e. two providers, and the card's
// useQuery would find no client). Same URL, same module instance.
const { QueryClient, QueryClientProvider } = await import(pathToFileURL(
  path.join(webDir, "node_modules/@tanstack/react-query/build/modern/index.js")).href);

// The card is a browser app: the globals its imports reach for (the toast store
// and the locale pick both keep their state in localStorage, and i18n writes
// <html lang> when the locale is applied).
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
  clear: () => store.clear(),
};
globalThis.window = globalThis;
globalThis.document = { documentElement: {} };

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
  if (!pass) failures.push(`${name}${detail ? ` — ${detail}` : ""}`);
};

/* The card's units, written out AGAIN here on purpose: a check that borrowed
 * fmtBytes() would move both sides together when the rounding changed and prove
 * nothing about what the user reads. The thresholds and the decimals are the
 * card's own (web/src/lib/fmt.ts) — binary units, and the one decimal a MB
 * figure carries. */
function bytes(n) {
  const v = Math.max(0, Number(n));
  if (v >= 1024 ** 4) return `${(v / 1024 ** 4).toFixed(2)} TB`;
  if (v >= 1024 ** 3) return `${(v / 1024 ** 3).toFixed(2)} GB`;
  if (v >= 1024 ** 2) return `${(v / 1024 ** 2).toFixed(1)} MB`;
  if (v >= 1024) return `${Math.round(v / 1024)} kB`;
  return `${Math.round(v)} B`;
}
const fileCount = (n) => `${n} file${n === 1 ? "" : "s"}`;

/** The rendered markup as the words on the card: tags (and the tooltips riding
 *  in their attributes) dropped, whitespace collapsed — so a row reads
 *  "Library 2.47 GB (2.46 GB audio)" and its neighbours follow in order.
 *  React's own `<!-- -->` separators go first and go to NOTHING: they sit
 *  between two text nodes of ONE sentence ("3<!-- --> links not followed"), so
 *  they are invisible on screen and a space here would read as "3 links" in one
 *  place and "3 link s" in another. */
function words(html) {
  return html
    .replace(/<!-- -->/g, "")
    .replace(/<[^>]*>/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/\s+/g, " ")
    .trim();
}

try {
  // The card is rendered in English whatever the machine's own locale is: the
  // labels are asserted by their English text, and a CI box with LANG=de would
  // otherwise fail every one of them.
  const i18n = await server.ssrLoadModule("/src/lib/i18n.ts");
  i18n.applyConfigLocale({ ui_locale: "en" });
  const card = await server.ssrLoadModule("/src/components/StorageCard.tsx");
  const en = (await server.ssrLoadModule("/src/locales/en.ts")).default;

  // The new label is a translated string (all six bundles carry it — see
  // tools/test_i18n.cjs), so the row's text comes from the bundle the app
  // ships rather than from a literal this check invented.
  const LABEL = en["storage.total"];
  check("storage.total ships as the Total row's label",
    LABEL === "Total (App + Library)", JSON.stringify(LABEL));

  const render = (payload) => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    qc.setQueryData(["storage"], payload);
    return renderToString(
      React.createElement(QueryClientProvider, { client: qc },
        React.createElement(card.default))
    );
  };

  const cases = payloadPaths.map((p) => [
    path.basename(p),
    JSON.parse(readFileSync(p, "utf8")),
  ]);

  for (const [name, payload] of cases) {
    const html = render(payload);
    const text = words(html);
    const lib = payload.library;
    const app = payload.app_total;
    const libShown = bytes(lib.bytes);
    const appShown = bytes(app.bytes);
    const sumShown = bytes(lib.bytes + app.bytes);
    // What a total built from the wrong half would print: the library's AUDIO
    // subtotal plus the app. Both fixtures are built so this is NOT the sum of
    // the two totals — that is what makes the check able to see the mistake.
    const subtotalSum = bytes(lib.audio_bytes + app.bytes);

    // Requirement 1, whole: the rows are the three the card promises, in this
    // order, called these things. (The label spans are the rows' own labels and
    // nothing else — the Row helper puts `shrink-0` on the label and nowhere
    // else in the card.)
    const labels = [...html.matchAll(/class="[^"]*shrink-0[^"]*">([^<]+)<\/span>/g)]
      .map((m) => m[1].trim());
    check(`${name}: the metric rows are Library, App, Total — in that order`,
      labels.join(" | ") === `Library | App | ${LABEL}`, labels.join(" | "));

    check(`${name}: the Library row keeps its figure and its (… audio) subtotal`,
      text.includes(`Library ${libShown} (${bytes(lib.audio_bytes)} audio)`));

    check(`${name}: the App row shows its figure and its file count`,
      text.includes(`App ${appShown} (${fileCount(app.files)})`));

    // Requirement 2: the Total the card PRINTS is the two rows it prints, added
    // up — the assertion recomputes both halves and their sum from the payload,
    // so a total that tracked anything else (a server field, a subtotal, the
    // printed strings) fails here.
    check(`${name}: the Total row is the Library row plus the App row, ${libShown} + ${appShown} = ${sumShown}`,
      text.includes(`${LABEL} ${sumShown} (${fileCount(lib.files + app.files)})`),
      `want "${LABEL} ${sumShown}"`);
    check(`${name}: the fixture can tell a subtotal sum from the real one (audio+app would print ${subtotalSum})`,
      subtotalSum !== sumShown);
    check(`${name}: the Total never sums the library's audio subtotal`,
      !text.includes(`${LABEL} ${subtotalSum}`));
    check(`${name}: the Total row's tooltip spells the addition out`,
      html.includes(`title="Library ${libShown} + App ${appShown}"`));

    // Requirement 1 (again): no row is called "App total" any more — the name
    // now belongs to the row that IS the total of the two above it.
    check(`${name}: no row is called "App total"`, !text.includes("App total"));

    // Requirement 3: the rest of the card is untouched. The detail line under
    // the App row still names its four parts (the same figures its tooltip
    // carries), the volume's own used/free readout still rides the bar, and the
    // walk's own notes still render.
    const staging = payload.downloads.staging_bytes
      ? ` (${bytes(payload.downloads.staging_bytes)} staging)` : "";
    check(`${name}: the App row keeps its state · bin · transfers · tools breakdown`,
      text.includes(`state ${bytes(payload.app_data.bytes)} · bin ${bytes(payload.trash.bytes)} · `
        + `transfers ${bytes(payload.downloads.bytes)}${staging} · tools ${bytes(payload.dependencies.bytes)}`));
    // The breakdown is the App figure's own parts — said out loud so a fixture
    // that stopped adding up could not silently weaken the breakdown assertion.
    check(`${name}: the fixture's breakdown really adds up to the App figure`,
      payload.app_data.bytes + payload.trash.bytes + payload.downloads.bytes
        + payload.dependencies.bytes === app.bytes);

    const usedLine = payload.percent_used === null
      ? "used space unknown" : `${payload.percent_used}% used`;
    const freeLine = payload.free_bytes === null || payload.total_bytes === null
      ? "unknown free of unknown total"
      : `${bytes(payload.free_bytes)} free of ${bytes(payload.total_bytes)}`;
    check(`${name}: the volume bar keeps its used and free/total readout`,
      text.includes(usedLine) && text.includes(freeLine), `want "${usedLine}" + "${freeLine}"`);

    if (payload.skipped_unreadable > 0) {
      check(`${name}: an unreadable folder is still counted under the rows`,
        text.includes(`${payload.skipped_unreadable} folder`
          + `${payload.skipped_unreadable === 1 ? "" : "s"} could not be read`));
    }
    if (payload.skipped_links > 0) {
      check(`${name}: an unfollowed link is still explained quietly`,
        text.includes(`${payload.skipped_links} link`
          + `${payload.skipped_links === 1 ? "" : "s"} not followed`));
    }
  }

  // A figure nobody could take. The app's folders are all unreadable (the
  // payload says so with `measured: false`, which is what an empty folder and a
  // refused one are told apart by), and then there is no music folder either:
  // the row above goes unknown, and the Total must go unknown WITH it. Summing
  // the half that is known would put a number on screen that answers a question
  // nobody asked ("the library plus an app of unknown size").
  const base = cases[0][1];
  const unmeasured = { ...base, app_total: { bytes: 0, files: 0, measured: false } };
  const noApp = words(render(unmeasured));
  check("app folders unreadable: the App row reads —", noApp.includes("App —"));
  check("app folders unreadable: the Total reads — too, never the library alone",
    noApp.includes(`${LABEL} —`)
    && !noApp.includes(`${LABEL} ${bytes(base.library.bytes)}`));
  check("app folders unreadable: an unmeasured App gets no file count and no breakdown",
    !noApp.includes("file)") && !noApp.includes("state "));

  const noLib = { ...base, library: null };
  const noLibrary = words(render(noLib));
  check("no music folder: the Library row reads —", noLibrary.includes("Library —"));
  check("no music folder: the Total reads — too, never the app alone",
    noLibrary.includes(`${LABEL} —`)
    && !noLibrary.includes(`${LABEL} ${bytes(base.app_total.bytes)}`));

  if (failures.length) {
    console.error(`[storage-card] FAIL — ${failures.length} of ${checks} checks:`);
    for (const f of failures) console.error(`  · ${f}`);
    process.exit(1);
  }
  console.log(`ok  the Storage card shows Library, App and their total `
    + `(${checks} checks over ${cases.length} payloads)`);
  for (const [name, payload] of cases) {
    const lib = payload.library.bytes, app = payload.app_total.bytes;
    console.log(`    ${name}: library ${bytes(lib)} + app ${bytes(app)} = ${bytes(lib + app)}`);
  }
} finally {
  await server.close();
}
