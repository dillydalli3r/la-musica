#!/usr/bin/env node
/* The wizard's progress strip, driven by the frames a REAL run publishes.
 *
 * The wizard reports every action to one strip (ImportWizard.tsx's ActionBar),
 * and two things can disagree in it: the action the user started, and the relay
 * frame the engine publishes over the websocket the header bar draws. The
 * frames in <frames.json> come from tools/test_import_pipeline.py, which
 * records them off a real chain run: first an import's stage frame (a bare 4/8,
 * no step pair) as the frame already on screen when the chain button is
 * pressed, then the run's own frames — the zero state
 * `script_runners.run_start_frame` claims its surfaces with, and every frame
 * after it, each carrying the whole-step pair `steps`.
 *
 * This check serves the real page (Vite, on a scratch port — 8011 and up,
 * never the owner's 8000), presses "Run ticked scripts" with that stage
 * frame on screen, then pushes each recorded frame into the store the app
 * draws from and asserts what a user sees:
 *
 *   * while the chain is still starting, the strip is INDETERMINATE under the
 *     chain's own label — the stage's 4/8 is in the store and is never drawn
 *     as the chain's progress;
 *   * the moment the run's own frames land, the strip is the RUN's: the run's
 *     text as the label, its step pair as the readout, its fraction as the bar;
 *   * the Finish step's own bar shows the same numbers as the strip, because
 *     both are drawn from one source.
 *
 * Run:  node tools/check_chain_bar.mjs <frames.json>
 * Exit codes: 0 pass, 1 a check failed (the failing ones are printed), 2 the
 * environment cannot run it (no web/node_modules, no playwright).
 */
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const webDir = path.join(here, "..", "web");
const payloadPath = process.argv[2];
if (!payloadPath) {
  console.error("[chain-bar] usage: node tools/check_chain_bar.mjs <frames.json>");
  process.exit(2);
}
if (!existsSync(path.join(webDir, "node_modules"))) {
  console.error("[chain-bar] web/node_modules is missing — run `npm --prefix web install`.");
  process.exit(2);
}

const payload = JSON.parse(readFileSync(payloadPath, "utf8"));
const { stage, later_stage: laterStage, frames, busy } = payload;
if (!stage || !laterStage || !busy || !Array.isArray(frames) || !frames.length) {
  console.error("[chain-bar] the payload carries no stage frames, no refusal sentence or no run frames.");
  process.exit(2);
}

let chromium;
const webRequire = createRequire(path.join(webDir, "package.json"));
try {
  ({ chromium } = webRequire("playwright"));
} catch {
  console.error("[chain-bar] Playwright not found — `npm --prefix web i -D playwright`.");
  process.exit(2);
}
const { createServer } = await import(
  `file://${path.join(webDir, "node_modules/vite/dist/node/index.js").replace(/\\/g, "/")}`);

const failures = [];
let checks = 0;
const check = (name, pass, detail = "") => {
  checks += 1;
  if (!pass) failures.push(detail ? `${name} — ${detail}` : name);
};

// The app's own answers, so the wizard renders its Finish step: the library it
// looks the album up in, the config it reads, and the import chain it previews.
// Everything else stays PENDING (the route handler simply never answers), which
// is what the wizard's other steps do with an unanswered query anyway.
const ALBUM = "F:/Music/Artists/Daft Punk - Homework";
const CONFIG = { music_folder: "F:/Music", mb_genre_count: 2, manual_import_enabled: true };
const LIBRARY = { artists: [{ name: "Daft Punk", albums: [{ path: ALBUM, tracks: [] }] }] };
const PREVIEW = { chain: [3, 5], labels: { 3: "Optimize FLACs", 5: "Process images" }, count: 2 };

// The scratch app: Vite's own dev server on 8011 (or the next free port), so
// the page under test is the REAL app — index.html, the module graph, the
// store — and not a harness copy of it. The owner's 8000 is never touched.
const vite = await createServer({
  configFile: path.join(webDir, "vite.config.ts"),
  root: webDir,
  logLevel: "error",
  server: { host: "127.0.0.1", port: 8011, strictPort: false },
});
try {
  await vite.listen();
} catch (e) {
  console.error("[chain-bar] the scratch app could not start: " + String(e));
  process.exit(2);
}
const port = vite.httpServer.address().port;

/** Every bar on the page whose text belongs to the chain this check started —
 *  the wizard's strip and the Finish step's own bar, both. The handles are
 *  kept across the frames below: React updates these nodes in place. */
const readBar = (handle) => handle.evaluate((el) => {
  const label = el.querySelector("span[title]");
  // The bar: the track div, and the fill inside it — the last div the bar
  // holds (a determinate frame sets its width, an indeterminate one pulses).
  const divs = el.querySelectorAll("div");
  const fill = divs.length ? divs[divs.length - 1] : null;
  const readout = el.lastElementChild;
  return {
    label: label ? label.getAttribute("title") : null,
    readout: readout ? (readout.textContent || "").trim() : null,
    width: fill ? fill.style.width : null,
    pulsing: fill ? fill.className.includes("animate-pulse") : null,
  };
});

const browser = await chromium.launch();
try {
  const page = await browser.newPage();
  await page.route("**/sw.js", (route) => route.abort());
  await page.route("**/api/**", (route) => {
    const url = route.request().url();
    const send = (body) => route.fulfill({
      status: 200, contentType: "application/json", body: JSON.stringify(body) });
    if (url.includes("/api/auth/status")) return send({ required: false, authenticated: true });
    if (url.includes("/api/config")) return send(CONFIG);
    if (url.includes("/api/library")) return send(LIBRARY);
    if (url.includes("/api/import/scripts/preview")) return send(PREVIEW);
    // The chain's own request is HELD OPEN on purpose: the wizard's strip is
    // being checked for the seconds before the chain starts and while it runs,
    // and both come from frames — never from this reply.
    if (url.includes("/api/run")) return undefined;
    return undefined;                 // anything else: left unanswered (pending)
  });

  await page.goto(`http://127.0.0.1:${port}/import?album=${encodeURIComponent(ALBUM)}&step=Finish`);
  // The preview has landed once the step names the chain it will run.
  await page.waitForSelector("text=Runs automatically after import:", { timeout: 20000 });
  await page.waitForSelector("text=Run ticked scripts", { timeout: 20000 });

  /** One relay frame, exactly as the websocket delivers it — into the same
   *  store the app's own pages read. */
  const pushFrame = (frame) => page.evaluate(
    (f) => import("/src/store.ts").then((m) => m.useStore.getState().setProgress(f)),
    frame);

  // The frame already on screen when the user presses the button: the import's
  // stage, at 4/8 — no step pair, so it is nobody's chain.
  await pushFrame(stage);
  await page.waitForTimeout(50);

  await page.getByRole("button", { name: "Run ticked scripts" }).click();
  await page.waitForTimeout(200);

  const bars = [];
  for (const handle of await page.$$('[role="status"]')) {
    const text = await handle.evaluate((el) => el.textContent || "");
    if (text.includes("Run scripts")) bars.push(handle);
  }
  check("the wizard shows the chain's progress strip (and the Finish step's own bar)",
        bars.length === 2, `${bars.length} bar(s)`);
  if (!bars.length) throw new Error("no chain bar rendered — nothing to assert");
  const [strip, stepBar] = bars;

  // ---- 1. the chain is still starting, and the stage's 4/8 is on screen ----
  const pending = await readBar(strip);
  check("the strip is indeterminate while the chain has not started",
        pending.readout !== null && pending.readout.startsWith("…")
        && pending.width === "35%" && pending.pulsing === true,
        JSON.stringify(pending));
  check("and it is the run's own label that is shown, not the import stage's",
        (pending.label || "").startsWith("Run scripts —")
        && !(pending.label || "").includes("Previous album"),
        String(pending.label));
  check("the import stage's 4/8 is never drawn as the chain's readout",
        !(pending.readout || "").includes("4/8"), String(pending.readout));
  check("the Finish step's own bar agrees with the strip",
        JSON.stringify(await readBar(stepBar)) === JSON.stringify(pending),
        JSON.stringify(await readBar(stepBar)));

  // A stage frame that arrives WHILE the chain is still starting: the stage's
  // own numbers are its own to print — under its own name, never the chain's,
  // and the run's frames below replace them the moment the run claims the bar.
  await pushFrame(laterStage);
  await page.waitForTimeout(60);
  const staging = await readBar(strip);
  check("a stage frame arriving mid-start names itself, not the chain",
        staging.label === laterStage.desc, `${staging.label} vs ${laterStage.desc}`);
  check("and prints its own numbers beside that name",
        (staging.readout || "").startsWith("5/8"), String(staging.readout));
  check("while the run's own label is gone from the strip",
        !(staging.label || "").includes("Run scripts"), String(staging.label));

  // ---- 2. the run's own frames, one by one --------------------------------
  for (let i = 0; i < frames.length; i += 1) {
    const frame = frames[i];
    await pushFrame(frame);
    await page.waitForTimeout(60);
    const shown = await readBar(strip);
    const pair = frame.steps ? `${frame.steps[0]}/${frame.steps[1]}` : null;
    check(`frame ${i + 1}/${frames.length} (${frame.desc}) names itself as the label`,
          shown.label === frame.desc, `${shown.label} vs ${frame.desc}`);
    check(`frame ${i + 1}/${frames.length} prints the run's own step pair`,
          pair !== null && (shown.readout || "").startsWith(pair),
          `${shown.readout} for steps ${JSON.stringify(frame.steps)}`);
    check(`frame ${i + 1}/${frames.length} draws the run's own fraction`,
          shown.width === `${Math.min(100, (frame.done / frame.total) * 100)}%`,
          `${shown.width} for ${frame.done}/${frame.total}`);
    check(`frame ${i + 1}/${frames.length} never falls back to the stage's 4/8`,
          !(shown.readout || "").includes("4/8"), String(shown.readout));
    const twin = await readBar(stepBar);
    check(`frame ${i + 1}/${frames.length} shows the same on the Finish step's bar`,
          twin.readout === shown.readout && twin.label === shown.label,
          JSON.stringify(twin));
  }

  // ---- 3. the run's own end -----------------------------------------------
  const last = await readBar(strip);
  const end = frames[frames.length - 1];
  check("the finished run reads as the run's own last step",
        end.steps && (last.readout || "").startsWith(`${end.steps[0]}/${end.steps[1]}`),
        String(last.readout));
  check("and its bar is the run's own completion, not a stage's",
        last.width === "100%", String(last.width));

  // Sections 4 and 5 that used to sit here drove the wizard's own
  // "Run the import chain" press against a 409 and a batch reply, to pin how a
  // REFUSED chain reads. That press is gone on purpose (spec R9: it re-ran the
  // whole import and undid the work the steps had just done by hand), so the
  // surface it described does not exist. The refusal itself is still pinned
  // where it lives — `tools/test_job_locks.py` covers a user press answered 409
  // with the claim's own sentence, and the batch entry that carries it, at the
  // API the press used.
} finally {
  await browser.close();
  await vite.close();
}

if (failures.length) {
  console.error(`[chain-bar] FAILED (${failures.length} of ${checks}):`);
  for (const f of failures) console.error("  - " + f);
  process.exit(1);
}
console.log(`ok  the wizard's progress strip is indeterminate under the chain's own label ` +
  `until the run claims it, then shows the run's own step pair — never an import stage's ` +
  `percentage (${checks} checks)`);
