#!/usr/bin/env node
/* R266 — the lyric panes' clock is the AUDIBLE instant, not the decoder's.
 *
 * Every element the app plays is routed through the WebAudio graph
 * (`createMediaElementSource` → the ReplayGain gain → the analyser → the
 * speakers, `web/src/lib/analyser.ts`), and that graph has a real output delay:
 * the element's `currentTime` says where the decoder is, while the buffer the
 * speakers are playing was handed to the device `baseLatency + outputLatency`
 * ago. `PlayerBar`'s `getAudioTime` — the ONE clock both lyric surfaces read —
 * therefore subtracts `audibleLatencySec(element)`, and the owner's report
 * ("audio in general is de-synced from what the app displays for synced
 * lyrics") is what the subtraction is for.
 *
 * This pins the number itself, case by case, because the failure mode of
 * getting it wrong is invisible on a desk with no graph attached:
 *
 *   * no element, no graph, latencies of zero → 0 (nothing to correct);
 *   * `baseLatency` alone, and base + `outputLatency` together → the sum (the
 *     older-Safari shape is the first of those);
 *   * a nonsense reading (NaN, negative, absurdly large) → 0, or the 0.5 s cap,
 *     never a number that could throw a pane a verse off. The reader's own
 *     offset control is the fine adjustment on top, and stays untouched.
 *
 * Exit codes: 0 pass, 1 a check failed (the failing ones are printed), 2 the
 * environment cannot run it (no web/node_modules).
 */
import { existsSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const webDir = path.join(here, "..", "web");
if (!existsSync(path.join(webDir, "node_modules"))) {
  console.error("[lyric-clock] web/node_modules is missing — run `npm --prefix web install`.");
  process.exit(2);
}

// The module is browser-shaped but its import path touches nothing global:
// `window.AudioContext` is only reached inside `ensureCtx`. Set anyway, so an
// import that grows such a reach fails here rather than in the app.
globalThis.window = globalThis;

// Loaded THROUGH vite, exactly like tools/check_lyrics_kind.mjs: the app's own
// TypeScript, resolved by the app's own config, so this check cannot drift from
// what the bundle ships.
const { createServer } = await import(pathToFileURL(
  path.join(webDir, "node_modules/vite/dist/node/index.js")).href);

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

const { audibleLatencySec } = await server.ssrLoadModule("/src/lib/analyser.ts");

/** An element carrying the graph the module itself writes onto it. */
const element = (ctx) => ({ __mloAnalyser: ctx ? { ctx, chain: {} } : undefined });

const close = (a, b) => Math.abs(a - b) < 1e-9;

check("an element with no graph is not corrected (nothing to be late in)",
  audibleLatencySec({}) === 0 && audibleLatencySec(element(null)) === 0);
check("no element at all is not corrected",
  audibleLatencySec(null) === 0 && audibleLatencySec(undefined) === 0);
check("a zeroed context (no device open yet) is not corrected",
  audibleLatencySec(element({ baseLatency: 0, outputLatency: 0 })) === 0);
check("base latency alone is corrected by exactly that",
  close(audibleLatencySec(element({ baseLatency: 0.02 })), 0.02));
check("base + output latency together are the correction",
  close(audibleLatencySec(element({ baseLatency: 0.01, outputLatency: 0.09 })), 0.1));
check("a missing outputLatency (older Safari) still uses baseLatency",
  close(audibleLatencySec(element({ baseLatency: 0.03, outputLatency: undefined })), 0.03));
check("NaN, negative and absurd readings never reach the panes",
  audibleLatencySec(element({ baseLatency: NaN, outputLatency: 0 })) === 0
  && audibleLatencySec(element({ baseLatency: -1, outputLatency: 0 })) === 0
  && audibleLatencySec(element({ baseLatency: 9, outputLatency: 9 })) === 0.5);

await server.close();

if (failures.length) {
  console.error(`\n[lyric-clock] ${failures.length} of ${checks} checks FAILED`);
  for (const f of failures) console.error(`  FAIL ${f}`);
  process.exit(1);
}
console.log("ok  the lyric panes' clock is the audible instant: the correction is " +
  "the attached graph's baseLatency + outputLatency, zero without a graph, and " +
  `capped so a nonsense reading cannot skew the words (${checks} checks)`);
