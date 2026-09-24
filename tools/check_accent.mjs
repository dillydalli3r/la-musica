#!/usr/bin/env node
/* The accent colour's maths, pinned — web/src/lib/accent.ts.
 *
 * Issue #53 added a custom colour picker on top of the seven presets the app
 * shipped with. Three values come out of every pick and every preset, and all
 * three are load-bearing on screen: the accent itself, its `soft` twin (secondary
 * text on the near-black surface, and the hover fill of a solid accent button)
 * and `fg` (the ink drawn ON the accent). Getting `fg` wrong is text that cannot
 * be read; getting `soft` wrong is a hover that looks like a different colour;
 * and getting the presets wrong is every existing install's theme changing
 * under it. So the rules are asserted here, once, instead of by eye next time:
 *
 *   * the seven pre-picker presets resolve BYTE-FOR-BYTE to the triplets the app
 *     carried before the picker (the payload's `pre_picker`, the old App.tsx
 *     table) — no visual regression for anyone;
 *   * every preset the module offers is covered by the payload, so a preset
 *     added without its colours being stated fails here;
 *   * a custom hex resolves to the colour itself, a `soft` of the same hue and
 *     saturation lifted for legibility, and the `fg` that clears the WCAG AA
 *     floor — asserted against the check's OWN luminance/contrast arithmetic,
 *     and against the other ink's contrast, so the choice has to be the better
 *     of the two and not merely a passing one;
 *   * `#rgb` / `#rrggbb`, with or without the `#`, in any case, with padding,
 *     all normalise to one form, and everything else — an empty field, a
 *     4- or 8-digit alpha form, `orange`, a stale capitalised name — resolves to
 *     the default theme instead of to nothing;
 *   * the variables are WRITTEN in one place and announced ONCE per apply, with
 *     unsubscribe honoured (what the canvas consumers repaint from).
 *
 * The module is loaded through Vite from the project's own source — no build
 * output, the real file — with a stub <html> in place of a document.
 *
 * Run:  node tools/check_accent.mjs [payload.json]   (default: tools/fixtures/accent.json)
 * Exit codes: 0 pass, 1 a check failed (the missing ones are printed), 2 the
 * environment cannot run it (no web/node_modules).
 */
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const webDir = path.join(here, "..", "web");
const payloadPath = process.argv[2] || path.join(here, "fixtures", "accent.json");

if (!existsSync(path.join(webDir, "node_modules"))) {
  console.error("[accent] SKIP: web/node_modules is missing — run npm install in web/");
  process.exit(2);
}
if (!existsSync(payloadPath)) {
  console.error(`[accent] no payload at ${payloadPath}`);
  process.exit(2);
}
const payload = JSON.parse(readFileSync(payloadPath, "utf8"));

// <html>'s inline style, which applyAccentVars writes to, and the computed
// properties currentAccent() reads back from it.
const written = new Map();
globalThis.document = {
  documentElement: { style: { setProperty: (name, value) => written.set(name, value) } },
};
globalThis.getComputedStyle = () => ({ getPropertyValue: (name) => written.get(name) ?? "" });

const failures = [];
let checks = 0;
const check = (name, pass, detail = "") => {
  checks++;
  if (!pass) failures.push(`${name}${detail ? ` — ${detail}` : ""}`);
};

// The check's OWN arithmetic. It must not import the module's: a test that
// reuses the code under test cannot disagree with a wrong answer, and the whole
// point of pinning `fg` is to state the contrast rule independently.
const luminance = (triplet) => {
  const [r, g, b] = triplet.split(" ").map(Number);
  const lin = (v) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
};
const contrast = (a, b) => {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
};
// Hue and saturation of a triplet, 0..360 and 0..1 — needed to assert that the
// soft twin is the SAME colour rather than merely a lighter one.
const hueSat = (triplet) => {
  const [r, g, b] = triplet.split(" ").map((v) => Number(v) / 255);
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const d = max - min;
  const l = (max + min) / 2;
  if (!d) return [null, 0, l];
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  const h = max === r ? ((g - b) / d + (g < b ? 6 : 0)) * 60 : max === g ? ((b - r) / d + 2) * 60 : ((r - g) / d + 4) * 60;
  return [h, s, l];
};
const sameTriplets = (a, b) => JSON.stringify(a) === JSON.stringify(b);

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
  const accent = await server.ssrLoadModule("/src/lib/accent.ts");
  const SHIPPED_IDS = Object.keys(payload.pre_picker);

  console.log("== the presets an install may already have ==");
  for (const id of SHIPPED_IDS) {
    check(`${id} keeps its pre-picker triplets byte-for-byte`,
      sameTriplets(accent.resolveAccent(id), payload.pre_picker[id]),
      `${accent.resolveAccent(id)} vs ${payload.pre_picker[id]}`);
  }
  const offered = accent.ACCENT_PRESETS.map((p) => p.id);
  check("the default theme is one of the presets", offered.includes(accent.DEFAULT_ACCENT), accent.DEFAULT_ACCENT);
  check("...and it is the fallback every unreadable value lands on",
    sameTriplets(payload.pre_picker[accent.DEFAULT_ACCENT], payload.fallback));

  console.log("\n== the whole swatch grid, preset by preset ==");
  for (const id of Object.keys(payload.presets)) {
    check(`the ${id} swatch paints its stated triplets`,
      sameTriplets(accent.resolveAccent(id), payload.presets[id]),
      `${accent.resolveAccent(id)} vs ${payload.presets[id]}`);
  }
  check("every preset the page offers is covered by this payload",
    offered.every((id) => id in payload.presets), offered.filter((id) => !(id in payload.presets)).join(", "));
  check("...and the payload invents none", Object.keys(payload.presets).every((id) => offered.includes(id)));
  check("the wheel is a real spread, not one hue repeated",
    Math.max(...offered.filter((id) => id !== "mono")
      .map((id) => hueSat(payload.presets[id][0])[0])) - Math.min(...offered.filter((id) => id !== "mono")
      .map((id) => hueSat(payload.presets[id][0])[0])) > 300);

  console.log("\n== a custom colour: accent, soft twin, and the ink on it ==");
  for (const { input, expect: want } of payload.custom) {
    const got = accent.resolveAccent(input);
    check(`"${input}" paints ${want[0]}`, sameTriplets(got, want), JSON.stringify(got));
    // The stored form is the normalised hex; re-resolving it must be the same
    // colour, or a reload would change what the user picked.
    const stored = accent.normalizeHex(input);
    check(`"${input}" survives a store/reload round trip`,
      stored !== null && sameTriplets(accent.resolveAccent(stored), want), String(stored));

    const [accentT, softT, fgT] = got;
    const [aHue, aSat, aLight] = hueSat(accentT);
    const [sHue, sSat, sLight] = hueSat(softT);
    if (aSat > payload.saturation_tolerance && sSat > payload.saturation_tolerance) {
      check(`"${input}": the soft twin keeps the hue (${Math.round(aHue)}°)`,
        Math.abs(aHue - sHue) <= payload.hue_tolerance_deg, `soft is ${Math.round(sHue)}°`);
      check(`"${input}": the soft twin keeps the saturation`,
        Math.abs(aSat - sSat) <= payload.saturation_tolerance, `${aSat.toFixed(3)} vs ${sSat.toFixed(3)}`);
    }
    if (aLight < 0.72) {
      check(`"${input}": the soft twin is LIGHTER, as a hover fill and secondary text both need`,
        sLight > aLight, `${aLight.toFixed(3)} -> ${sLight.toFixed(3)}`);
    } else {
      check(`"${input}": a light accent's soft twin stays in the legible band (0.72–0.88)`,
        sLight >= 0.72 - 1e-9 && sLight <= 0.88 + 1e-9, String(sLight));
    }
    check(`"${input}": the ink is white or black, never a third colour`,
      fgT === "255 255 255" || fgT === "0 0 0", fgT);
    const other = fgT === "255 255 255" ? "0 0 0" : "255 255 255";
    check(`"${input}": the ink is the better-contrasting of the two`,
      contrast(fgT, accentT) >= contrast(other, accentT),
      `${contrast(fgT, accentT).toFixed(2)}:1 for ${fgT} against ${contrast(other, accentT).toFixed(2)}:1 for ${other}`);
    check(`"${input}": ...and clears the WCAG AA floor (${payload.contrast_floor}:1)`,
      contrast(fgT, accentT) >= payload.contrast_floor,
      `${contrast(fgT, accentT).toFixed(2)}:1`);
  }

  console.log("\n== what a person can type ==");
  for (const { input, expect: want } of payload.normalize) {
    check(`"${input}" normalises to ${want}`, accent.normalizeHex(input) === want, String(accent.normalizeHex(input)));
    check(`"${input}" parses to channels`, Array.isArray(accent.parseHexColor(input)), String(accent.parseHexColor(input)));
  }
  check("6-digit hex parses to the obvious channels",
    sameTriplets(accent.parseHexColor("#8b5cf6"), [139, 92, 246]), String(accent.parseHexColor("#8b5cf6")));
  check("3-digit hex doubles each digit, it does not pad",
    sameTriplets(accent.parseHexColor("#abc"), [170, 187, 204]), String(accent.parseHexColor("#abc")));

  console.log("\n== nothing to paint: the default, never an unset colour ==");
  for (const bad of payload.invalid) {
    check(`${JSON.stringify(bad)} is not a colour`, accent.normalizeHex(bad) === null, String(accent.normalizeHex(bad)));
    check(`${JSON.stringify(bad)} falls back to the default theme`,
      sameTriplets(accent.resolveAccent(bad), payload.fallback), JSON.stringify(accent.resolveAccent(bad)));
  }

  console.log("\n== the write the shell and the settings page share ==");
  let heard = 0;
  const unsubscribe = accent.subscribeAccent(() => {
    heard++;
  });
  accent.applyAccentVars(["255 122 24", "255 173 112", "0 0 0"]);
  check("all three variables land on <html>",
    written.get("--accent") === "255 122 24"
    && written.get("--accent-soft") === "255 173 112"
    && written.get("--accent-fg") === "0 0 0",
    JSON.stringify([...written]));
  check("the change is announced once per apply", heard === 1, String(heard));
  check("the canvases can read back what was painted",
    sameTriplets(accent.currentAccent(), ["255 122 24", "255 173 112", "0 0 0"]),
    JSON.stringify(accent.currentAccent()));
  accent.applyAccentVars(accent.resolveAccent("violet"));
  check("the shell's own applyAccent path (resolve, then write) paints the preset it is given",
    written.get("--accent") === "139 92 246" && written.get("--accent-fg") === "255 255 255"
    && written.get("--accent-soft") === "167 139 250",
    JSON.stringify([...written]));
  check("...and announces that too", heard === 2, String(heard));
  unsubscribe();
  accent.applyAccentVars(accent.resolveAccent("mono"));
  check("an unsubscribed consumer is left alone", heard === 2, String(heard));

  if (failures.length) {
    console.error(`\n${failures.length} failure(s):`);
    for (const f of failures) console.error(`  - ${f}`);
    console.error(`\n${checks - failures.length}/${checks} checks passed`);
    process.exit(1);
  }
  console.log(`\nok  the accent maths: ${checks} checks — the ${SHIPPED_IDS.length} pre-picker presets are ` +
    `byte-for-byte unchanged, all ${offered.length} presets paint their stated colours, a custom hex derives a ` +
    `same-hue soft twin and the AA-clearing ink, unreadable input falls back to the default, and one writer ` +
    `announces every change to the canvases`);
} finally {
  await server.close();
}
