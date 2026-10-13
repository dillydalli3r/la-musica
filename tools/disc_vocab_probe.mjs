#!/usr/bin/env node
/* Parity probe for the browser disc vocabulary.
 *
 * `tools/test_disc_vocab.py` hands this script a JSON case list and compares
 * every answer below with the Python original (`mlo/discs.py`), so the wizard's
 * mirror (`web/src/lib/discs.ts`) can never drift from the one vocabulary the
 * library uses. Node 24 strips the TypeScript types natively — no build step.
 *
 * Usage:  node tools/disc_vocab_probe.mjs <cases.json>
 *
 *   { "dirs": ["CD1", ...],
 *     "bares": ["1", ...],
 *     "parents": [["CD1", "CD2"], ...],
 *     "paths": ["Album/CD2/01 - x.flac", ...] }
 *
 * prints one JSON object on stdout; a key appears only for a key the input
 * carried, so the minimal `{"dirs", "parents"}` shape stays exactly:
 *
 *   { "dirs": { name: number|null },
 *     "bares": { name: number|null },
 *     "parents": { "<json childNames>": bool },
 *     "paths": { path: number|null } }
 *
 * Exit 2 is the repo's "the tooling is not installed" / bad usage convention. */
import { readFileSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const mod = await import(pathToFileURL(path.join(here, "..", "web", "src", "lib", "discs.ts")).href);
const { discDirNumber, bareDiscIndex, isDiscParent, discNumberPathOf } = mod;

const casesPath = process.argv[2];
if (!casesPath) {
  process.stderr.write("usage: node tools/disc_vocab_probe.mjs <cases.json>\n");
  process.exit(2);
}

const cases = JSON.parse(readFileSync(casesPath, "utf8"));
const out = {};
if (Array.isArray(cases.dirs)) {
  out.dirs = {};
  for (const name of cases.dirs) out.dirs[String(name)] = discDirNumber(String(name));
}
if (Array.isArray(cases.bares)) {
  out.bares = {};
  for (const name of cases.bares) out.bares[String(name)] = bareDiscIndex(String(name));
}
if (Array.isArray(cases.parents)) {
  out.parents = {};
  for (const kids of cases.parents) out.parents[JSON.stringify(kids)] = isDiscParent(kids);
}
if (Array.isArray(cases.paths)) {
  out.paths = {};
  for (const p of cases.paths) out.paths[String(p)] = discNumberPathOf(String(p));
}
process.stdout.write(JSON.stringify(out) + "\n");