#!/usr/bin/env node
/* The check-stack page's gate switches — one rule, pinned.
 *
 * A script whose feature is switched off is skipped by every run; a script with
 * TWO switches runs while ANY of them is on (script 17 transliterates,
 * translates or both — `server/script_runners._DISABLED` and `run_script`'s
 * `if not any(...)`). `server/api_stack.py` states that aggregate per script as
 * `gate.enabled`. The page used to spell the aggregate itself as
 * `s.gate.keys.every(...)` in three places and seed each switch from the
 * script's aggregate, so a two-switch script read as "gated off" while a run of
 * it would have run.
 *
 * What this pins, against a payload whose script 17 has ONE switch on and one
 * off (the fixture is built by `server.api_stack.build_stack` from
 * DEFAULT_CONFIG with `lyrics_xlit_enabled: true, lyrics_translate_enabled:
 * false, mood_enabled: false` — so the SERVER's own aggregate is under test
 * too, not just the page's reading of it):
 *
 *   * the payload says 17 is runnable (any-of) and 16 is not;
 *   * the draft holds ONE boolean per SCRIPT, seeded from the payload's own
 *     answer, so a script's two switches can never be seeded half-and-half
 *     (which is what made an untick flip the wrong switch);
 *   * `gateOn` answers any-of for the two-switch script, follows the draft
 *     before it is saved, and treats a switch-less script as on;
 *   * `toggleGate` moves the pressed script and NOTHING else.
 *
 * Run:  node tools/check_stack_gates.mjs [payload.json]
 *       (default: tools/fixtures/stack.json)
 * Exit codes: 0 pass, 1 a check failed, 2 the environment cannot run it.
 */
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const webDir = path.join(here, "..", "web");
const payloadPath = process.argv[2] || path.join(here, "fixtures", "stack.json");

if (!existsSync(path.join(webDir, "node_modules"))) {
  console.error("[stack-gates] SKIP: web/node_modules is missing — run npm install in web/");
  process.exit(2);
}
if (!existsSync(payloadPath)) {
  console.error(`[stack-gates] no payload at ${payloadPath}`);
  process.exit(2);
}
const payload = JSON.parse(readFileSync(payloadPath, "utf8"));

const failures = [];
let checks = 0;
const check = (name, pass, detail = "") => {
  checks++;
  if (!pass) failures.push(`${name}${detail ? ` — ${detail}` : ""}`);
};
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

const two = payload.scripts.find((s) => s.gate.keys.length === 2);
const one = payload.scripts.find((s) => s.gate.keys.length === 1 && !s.gate.enabled);
const none = payload.scripts.find((s) => s.gate.keys.length === 0);

console.log("== the payload the page reads ==");
check("the fixture really holds a two-switch script", !!two && !!one && !!none,
  `two=${two?.id} oneOff=${one?.id} switchless=${none?.id}`);
check("the server states the two-switch aggregate as ANY-of (one switch on)",
  two.gate.enabled === true, JSON.stringify(two.gate));
check("...and a single switched-off script as off", one.gate.enabled === false,
  JSON.stringify(one.gate));

const { createServer } = await import(
  `file://${path.join(webDir, "node_modules/vite/dist/node/index.js").replace(/\\/g, "/")}`);

const server = await createServer({
  configFile: path.join(webDir, "vite.config.ts"),
  root: webDir,
  server: { middlewareMode: true },
  appType: "custom",
  logLevel: "error",
});

try {
  const gate = await server.ssrLoadModule("/src/lib/stackDraft.ts");

  console.log("\n== what the draft is seeded with ==");
  const seeded = gate.gateStateFrom(payload.scripts);
  check("one boolean per script, keyed by script",
    Object.keys(seeded).length === payload.scripts.length
    && Object.values(seeded).every((v) => typeof v === "boolean"),
    JSON.stringify(Object.keys(seeded).slice(0, 25)));
  check("...seeded from the payload's own answer, not a re-derivation",
    payload.scripts.every((s) => seeded[s.id] === (s.gate.keys.length ? s.gate.enabled : true)),
    JSON.stringify(payload.scripts.filter((s) => seeded[s.id] !== (s.gate.keys.length ? s.gate.enabled : true)).map((s) => s.id)));
  check("a two-switch script has ONE state, so its switches cannot be seeded half-and-half",
    typeof seeded[two.id] === "boolean" && !(two.gate.keys[0] in seeded),
    JSON.stringify(Object.keys(seeded).filter((k) => two.gate.keys.includes(k))));
  check("a switch-less script has nothing to hold off",
    seeded[none.id] === true);

  console.log("\n== what a row shows ==");
  check("the two-switch script reads as RUNNABLE (one switch on, one off)",
    gate.gateOn(seeded, two) === true, String(gate.gateOn(seeded, two)));
  check("a switched-off script reads as off", gate.gateOn(seeded, one) === false);
  check("a switch-less script reads as on", gate.gateOn(seeded, none) === true);
  check("a script the draft has not seen falls back to the payload's answer",
    gate.gateOn({}, two) === two.gate.enabled && gate.gateOn({}, one) === false);

  console.log("\n== toggling ==");
  const off = gate.toggleGate(seeded, two, false);
  check("unticking the two-switch script turns it off", gate.gateOn(off, two) === false);
  check("...and changes NO other script",
    payload.scripts.filter((s) => s.id !== two.id)
      .every((s) => gate.gateOn(off, s) === gate.gateOn(seeded, s)),
    JSON.stringify(payload.scripts.filter((s) => s.id !== two.id
      .toString() && gate.gateOn(off, s) !== gate.gateOn(seeded, s)).map((s) => s.id)));
  check("...by moving exactly one entry of the draft",
    Object.keys(off).length === Object.keys(seeded).length
    && Object.keys(seeded).filter((k) => off[k] !== seeded[k]).map(Number).join(",") === String(two.id),
    JSON.stringify(Object.keys(seeded).filter((k) => off[k] !== seeded[k])));
  check("ticking it back restores exactly the payload's state",
    same(gate.toggleGate(off, two, true), seeded));
  check("unticking a single-switch script moves only it",
    same(gate.toggleGate(seeded, one, true),
      { ...seeded, [one.id]: true }));
  check("a switch-less script's toggle is a no-op (its slot decides)",
    gate.toggleGate(seeded, none, false) === seeded);
  check("a row's `on` is its gate AND its place in the chain",
    seeded[two.id] === true && gate.gateOn(seeded, two) === true);

  console.log("\n== both switches off is off, both on is on ==");
  const bothOff = gate.gateStateFrom(payload.scripts.map((s) =>
    s.id === two.id ? { ...s, gate: { keys: s.gate.keys, enabled: false } } : s));
  check("the any-of rule still reads a fully switched-off script as off",
    gate.gateOn(bothOff, two) === false);
  const bothOn = gate.gateStateFrom(payload.scripts.map((s) =>
    s.id === two.id ? { ...s, gate: { keys: s.gate.keys, enabled: true } } : s));
  check("...and a fully switched-on one as on", gate.gateOn(bothOn, two) === true);

  console.log("\n== the page ==");
  const src = readFileSync(path.join(webDir, "src", "pages", "CheckStackPage.tsx"), "utf8");
  const code = src.replace(/\/\*[\s\S]*?\*\//g, "").split("\n").map((l) => l.split("//")[0]).join("\n");
  check("the page spells no aggregate of its own any more",
    !/gate\.keys\.every/.test(code) && !/gate\.keys\.some/.test(code),
    (code.match(/.*gate\.keys\.(every|some).*/g) || []).join(" | "));
  check("...it asks the shared rule at every site it needs one",
    (code.match(/gateOn\(/g) || []).length >= 4,
    String((code.match(/gateOn\(/g) || []).length));
  check("the per-KEY seeding loop is gone (one state per script)",
    !/for \(const k of s\.gate\.keys\) gates\[k\]/.test(code));
  check("the draft is seeded from the payload's own answer",
    /gates:\s*gateStateFrom\(stack\.scripts\)/.test(code));
  check("the toggle goes through the shared rule",
    /toggleGate\(d\.gates, s, on\)/.test(code));
  check("the checkbox, the counter, the save diff and the readout all read that rule",
    /const on = draft\.order\.includes\(s\.id\) && gateOn\(draft\.gates, s\)/.test(code)
    && /const now = s\.gate\.keys\.length \? gateOn\(draft\.gates, s\)/.test(code)
    && /&& gateOn\(draft\.gates, s\)\)\.length/.test(code)
    && /\$\{gateOn\(draft\.gates, s\) \? "on" : "off"\}/.test(code));
  check("the page's own readout no longer prints the stale server aggregate",
    !/s\.gate\.enabled \? "on" : "off"/.test(code));

  if (failures.length) {
    console.error(`\n${failures.length} failure(s):`);
    for (const f of failures) console.error(`  - ${f}`);
    console.error(`\n${checks - failures.length}/${checks} checks passed`);
    process.exit(1);
  }
  console.log(`\nok  the gate switches: ${checks} checks — script ${two.id}'s two switches read as runnable ` +
    `while either is on, the draft holds one state per script (seeded from the payload), and toggling ` +
    `moves exactly the script pressed`);
} finally {
  await server.close();
}
