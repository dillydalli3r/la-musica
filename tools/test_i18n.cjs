#!/usr/bin/env node
/* Locale-bundle contract check — no framework, no dependencies.
 *
 * The bundles are plain `export default { "key": "value", … } as const` files,
 * so a quote-aware scan of the TEXT reads every pair exactly: no TypeScript
 * compiler is needed, and a value carrying ":" or an escaped quote cannot fool
 * it the way a regex would. Asserted against the real files, in both
 * directions:
 *
 *   * every English key exists in every other locale, AND no locale carries a
 *     key English lacks — dropping one key from one bundle fails, and so does
 *     adding a key to only one;
 *   * `{placeholder}` names match between locales for each key, so a
 *     translation cannot silently swallow an interpolation;
 *   * the codes listed in web/src/lib/i18n.ts LOCALES and the files in
 *     web/src/locales/ are the same set (a bundle nobody loads, or a locale
 *     entry with no bundle, fails);
 *   * no bundle repeats a key or ships an empty string.
 *
 * Run: node tools/test_i18n.cjs
 */
const fs = require("node:fs");
const path = require("node:path");

const LOCALES_DIR = path.join(__dirname, "..", "web", "src", "locales");
const I18N_TS = path.join(__dirname, "..", "web", "src", "lib", "i18n.ts");
const SOURCE_LOCALE = "en";

/** Read the double-quoted string starting at `text[i]` (the opening quote);
 *  answers [value, index after the closing quote]. */
function readString(text, i) {
  let value = "";
  let j = i + 1;
  while (j < text.length) {
    const c = text[j];
    if (c === "\\") {
      // Only the escapes these bundles can carry; anything else keeps the
      // escaped character itself, so the value still says what the file says.
      const esc = text[j + 1];
      value += esc === "n" ? "\n" : esc === "t" ? "\t" : esc;
      j += 2;
      continue;
    }
    if (c === '"') return [value, j + 1];
    value += c;
    j++;
  }
  throw new Error("unterminated string");
}

/** Every `"key": "value"` pair of a locale module, in file order. */
function readPairs(text, file) {
  const pairs = [];
  // Anchored at `export default`: the file's header comment may itself contain
  // braces (the `{placeholder}` it documents), which must not start the scan.
  const open = text.indexOf("{", text.indexOf("export default"));
  const end = text.lastIndexOf("}");
  if (open < 0 || end < open) throw new Error(`${file}: no object literal`);
  let i = open + 1;
  while (i < end) {
    const c = text[i];
    if (c === " " || c === "\t" || c === "\r" || c === "\n" || c === ",") {
      i++;
      continue;
    }
    if (c === "/" && text[i + 1] === "/") {
      i = text.indexOf("\n", i);
      if (i < 0) break;
      continue;
    }
    if (c !== '"') throw new Error(`${file}: unexpected ${JSON.stringify(c)} at offset ${i}`);
    const [key, afterKey] = readString(text, i);
    let k = afterKey;
    while (text[k] === " " || text[k] === "\t") k++;
    if (text[k] !== ":") throw new Error(`${file}: no ':' after the key "${key}"`);
    k++;
    while (text[k] === " " || text[k] === "\t") k++;
    if (text[k] !== '"') throw new Error(`${file}: the value of "${key}" is not a string`);
    const [value, afterValue] = readString(text, k);
    pairs.push([key, value]);
    i = afterValue;
  }
  return pairs;
}

/** The `{name}` placeholders a string carries, as a sorted, de-duplicated list. */
function placeholders(value) {
  const names = [];
  for (const m of value.matchAll(/\{(\w+)\}/g)) {
    if (!names.includes(m[1])) names.push(m[1]);
  }
  return names.sort();
}

const problems = [];
const fail = (message) => problems.push(message);

const files = fs.readdirSync(LOCALES_DIR).filter((f) => f.endsWith(".ts")).sort();
const codeOfFile = (f) => f.slice(0, -3);
const bundles = new Map();
for (const file of files) {
  const raw = fs.readFileSync(path.join(LOCALES_DIR, file), "utf8");
  const pairs = readPairs(raw, file);
  const bundle = new Map();
  for (const [key, value] of pairs) {
    if (bundle.has(key)) fail(`${file}: duplicate key "${key}"`);
    if (!value) fail(`${file}: "${key}" is empty`);
    bundle.set(key, value);
  }
  bundles.set(codeOfFile(file), { file, bundle, count: pairs.length });
}

if (!bundles.has(SOURCE_LOCALE)) {
  console.error(`[test_i18n] no ${SOURCE_LOCALE}.ts to compare against`);
  process.exit(1);
}
const source = bundles.get(SOURCE_LOCALE).bundle;

for (const [code, { file, bundle }] of bundles) {
  console.log(`  ${code.padEnd(6)} ${file.padEnd(12)} ${String(bundle.size).padStart(3)} keys`);
  if (code === SOURCE_LOCALE) continue;
  for (const key of source.keys()) {
    if (!bundle.has(key)) fail(`${file}: missing key "${key}"`);
  }
  for (const key of bundle.keys()) {
    if (!source.has(key)) fail(`${file}: unknown key "${key}" (${SOURCE_LOCALE}.ts has no such key)`);
  }
  for (const [key, value] of bundle) {
    if (!source.has(key)) continue;
    const want = placeholders(source.get(key)).join(",");
    const got = placeholders(value).join(",");
    if (want !== got) fail(`${file}: "${key}" placeholders {${got}} do not match {${want}}`);
  }
}

// The locale list the app actually loads must name exactly the bundles on
// disk: an unlisted file is dead weight, a listed one with no file is a blank
// option in the picker.
const i18n = fs.readFileSync(I18N_TS, "utf8");
const listed = [];
for (let i = i18n.indexOf('code: "'); i >= 0; i = i18n.indexOf('code: "', i + 1)) {
  listed.push(readString(i18n, i + 6)[0]);
}
for (const code of listed) {
  if (!bundles.has(code)) fail(`i18n.ts lists locale "${code}" but web/src/locales/${code}.ts does not exist`);
}
for (const code of bundles.keys()) {
  if (!listed.includes(code)) fail(`web/src/locales/${code}.ts is not listed in i18n.ts LOCALES`);
}

console.log(`\ni18n.ts LOCALES: ${listed.join(", ")}`);
if (problems.length) {
  console.log(`\nFAIL — ${problems.length} problem(s)`);
  for (const p of problems) console.log(`  - ${p}`);
  process.exit(1);
}
console.log(`\nPASS — ${bundles.size} locales, ${source.size} keys each, placeholders aligned`);
