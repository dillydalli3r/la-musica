#!/usr/bin/env node
/* Regression check for web/src/lib/lyrScroll.ts — the shared lyrics-pane
 * auto-scroller (fullscreen player, sidebar, editor preview).
 *
 * The glider is pure arithmetic over a scroller element, so it runs in Node
 * against a fake pane: no server, no browser, no test framework. The cases
 * below are the ones that actually bit:
 *
 *   * a retarget landing mid-glide used to cancel the pending frame while
 *     the loop still counted as active — the pane froze between two lines
 *     and stayed frozen for the rest of the track. This is the case to keep.
 *   * a pane that unmounts (or collapses) mid-glide pins scrollTop to 0, so
 *     the target became unreachable and the rAF loop spun forever.
 *   * the ease was a fixed per-frame step, so a 144 Hz display glided 2.4x
 *     faster than a 60 Hz one.
 *
 * Run:  node tools/check_lyrscroll.cjs      (exit 2 = toolchain missing)
 * Bundles the real module with the esbuild that ships alongside vite, so the
 * assertions always exercise the shipped source. */

const fs = require("fs");
const path = require("path");

let ts;
try {
  ts = require(path.join(__dirname, "..", "web", "node_modules", "typescript"));
} catch {
  console.error("[lyrscroll] typescript not found — run `npm install` in web/ first.");
  process.exit(2);
}

// ---- fake pane -----------------------------------------------------------
/** A scroller with `n` fixed-height lines, offsetParent chains and a
 * scrollTop that clamps exactly like a real element's. */
function makePane({ n = 40, lineH = 40, viewport = 300, wrapper = false } = {}) {
  const scroller = {
    isConnected: true,
    offsetParent: null,
    offsetTop: 0,
    scrollHeight: n * lineH,
    clientHeight: viewport,
    _top: 0,
    get scrollTop() { return this._top; },
    set scrollTop(v) { this._top = Math.min(Math.max(0, v), Math.max(0, this.scrollHeight - this.clientHeight)); },
  };
  const holder = wrapper
    ? { offsetParent: scroller, offsetTop: 12, offsetHeight: 0, isConnected: true }
    : scroller;
  const lines = Array.from({ length: n }, (_, i) => ({
    offsetParent: holder,
    offsetTop: wrapper ? i * lineH : i * lineH,
    offsetHeight: lineH,
  }));
  return { scroller, lines, base: wrapper ? 12 : 0 };
}

// ---- frame pump ----------------------------------------------------------
let clock = 0;
const pending = new Map();
let nextId = 0;
globalThis.window = { matchMedia: () => ({ matches: false }) };
globalThis.performance = { now: () => clock };
globalThis.requestAnimationFrame = (cb) => { pending.set(++nextId, cb); return nextId; };
globalThis.cancelAnimationFrame = (id) => { pending.delete(id); };

/** Run `count` frames of `ms` each; returns how many callbacks fired. */
function frames(count, ms) {
  let fired = 0;
  for (let i = 0; i < count; i++) {
    clock += ms;
    const due = [...pending.values()];
    pending.clear();
    fired += due.length;
    due.forEach((cb) => cb(clock));
  }
  return fired;
}

// ---- the shipped module --------------------------------------------------
const SRC = path.join(__dirname, "..", "web", "src", "lib", "lyrScroll.ts");
const compiled = ts.transpileModule(fs.readFileSync(SRC, "utf8"), {
  fileName: SRC,
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText;

// The react import only feeds the follow hook, which this check does not
// exercise — stub it so the real glider source loads with no bundler and no
// DOM. Everything asserted below is the shipped code path.
const reactStub = {
  useCallback: (fn) => fn,
  useEffect: () => {},
  useRef: () => ({ current: null }),
  useState: (init) => [init, () => {}],
};
const mod = { exports: {} };
new Function("require", "module", "exports", compiled)(
  (id) => (id === "react" ? reactStub : require(id)), mod, mod.exports);
const LYR = mod.exports;

const TARGET = (pane, i) => Math.min(
  Math.max(0, pane.scroller.scrollHeight - pane.scroller.clientHeight),
  pane.base + pane.lines[i].offsetTop + pane.lines[i].offsetHeight / 2 - pane.scroller.clientHeight * LYR.LYRICS_ANCHOR);

// ---- cases ---------------------------------------------------------------
const results = [];
const check = (name, pass, detail) => results.push({ name, pass: !!pass, detail });

{
  // The regression: a line change lands while the previous glide is running.
  const pane = makePane({ n: 200 });
  const g = LYR.createLyricsGlider(pane.scroller);
  g.center(pane.lines[5]);
  frames(4, 16.667);
  const mid = pane.scroller.scrollTop;
  g.center(pane.lines[30]);
  frames(60, 16.667);
  const want = TARGET(pane, 30);
  check("retarget mid-glide keeps gliding to the new line",
    mid > 0 && mid < want * 0.2 && Math.abs(pane.scroller.scrollTop - want) < 1,
    `mid=${mid.toFixed(1)} end=${pane.scroller.scrollTop.toFixed(1)} want=${want.toFixed(1)}`);
}

{
  // A retarget on every line — fast lyrics never let a glide settle.
  const pane = makePane({ n: 200 });
  const g = LYR.createLyricsGlider(pane.scroller);
  for (let i = 0; i < 10; i++) { g.center(pane.lines[i * 5]); frames(1, 20); }
  frames(60, 16.667);
  const want = TARGET(pane, 45);
  check("survives a retarget on every line",
    Math.abs(pane.scroller.scrollTop - want) < 1,
    `end=${pane.scroller.scrollTop.toFixed(1)} want=${want.toFixed(1)}`);
}

{
  const pane = makePane({ n: 40 });
  LYR.createLyricsGlider(pane.scroller).center(pane.lines[10], true);
  const want = TARGET(pane, 10);
  check("centres the sung line on the anchor",
    Math.abs(pane.scroller.scrollTop - want) < 1 && want === 10 * 40 + 20 - 300 * LYR.LYRICS_ANCHOR,
    `scrollTop=${pane.scroller.scrollTop} want=${want}`);
}

{
  // offsetTop is walked up the offsetParent chain, so a positioned wrapper
  // between the line and the pane must not shift the target.
  const pane = makePane({ n: 40, wrapper: true });
  LYR.createLyricsGlider(pane.scroller).center(pane.lines[10], true);
  const want = TARGET(pane, 10);
  check("centres through a nested positioned wrapper",
    Math.abs(pane.scroller.scrollTop - want) < 1,
    `scrollTop=${pane.scroller.scrollTop} want=${want}`);
}

{
  const pane = makePane({ n: 200 });
  const g = LYR.createLyricsGlider(pane.scroller);
  g.center(pane.lines[180]);
  frames(2, 16.667);
  pane.scroller.isConnected = false;               // pane unmounts mid-glide
  frames(3, 16.667);
  const after = frames(20, 16.667);
  check("detached pane stops the frame loop", after === 0, `frames after detach=${after}`);
}

{
  const pane = makePane({ n: 200 });
  const g = LYR.createLyricsGlider(pane.scroller);
  g.center(pane.lines[180]);
  frames(2, 16.667);
  pane.scroller.scrollHeight = 0;                  // collapsed: scrollTop pins to 0
  frames(3, 16.667);
  const after = frames(20, 16.667);
  check("collapsed pane stops the frame loop", after === 0, `frames after collapse=${after}`);
}

{
  // Same travel per unit time on any refresh rate: one 33 ms frame must cover
  // what two 16.7 ms frames cover, not one fixed step.
  const pane = makePane({ n: 200 });
  const want = TARGET(pane, 100);
  LYR.createLyricsGlider(pane.scroller).center(pane.lines[100]);
  frames(1, 33.333);
  const moved = pane.scroller.scrollTop / want;
  const expect = 1 - Math.pow(1 - 0.16, 33.333 / (1000 / 60));
  check("ease normalises for frame time", Math.abs(moved - expect) < 0.002,
    `progress=${moved.toFixed(4)} want=${expect.toFixed(4)} (a fixed step would be 0.16)`);
}

{
  globalThis.window.matchMedia = () => ({ matches: true });
  const pane = makePane({ n: 40 });
  pending.clear();                       // drop any loop left by an earlier case
  LYR.createLyricsGlider(pane.scroller).center(pane.lines[20]);
  globalThis.window.matchMedia = () => ({ matches: false });
  const want = TARGET(pane, 20);
  check("reduced motion arrives without animating",
    Math.abs(pane.scroller.scrollTop - want) < 1 && frames(2, 16.667) === 0,
    `scrollTop=${pane.scroller.scrollTop} want=${want}`);
}

{
  const pane = makePane({ n: 40 });
  const outside = { offsetParent: null, offsetTop: 0, offsetHeight: 10 };
  LYR.createLyricsGlider(pane.scroller).center(outside, true);
  check("refuses elements outside the pane", pane.scroller.scrollTop === 0,
    `scrollTop=${pane.scroller.scrollTop}`);
}

{
  // The pads are what let the FIRST and LAST line reach the anchor; without
  // the tail one the pane runs out of travel and looks frozen at the outro.
  const top = parseFloat(LYR.LYRICS_PAD_TOP), bottom = parseFloat(LYR.LYRICS_PAD_BOTTOM);
  check("anchor pads bracket the pane",
    Math.abs(top - LYR.LYRICS_ANCHOR * 100) < 0.1 && Math.abs(top + bottom - 100) < 0.1,
    `top=${top}% bottom=${bottom}%`);
}

for (const r of results) console.log(`${r.pass ? "ok  " : "FAIL"} ${r.name} :: ${r.detail}`);
const bad = results.filter((r) => !r.pass).length;
console.log(`${results.length - bad}/${results.length} checks pass`);
process.exit(bad ? 1 : 0);
