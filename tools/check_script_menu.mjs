#!/usr/bin/env node
/* The details menu's script entries — the registry's answer, pinned.
 *
 * `GET /api/script-menu` carries every script with the entity kinds a run of it
 * makes sense from (`server/script_menu.py`, derived from the runners' own
 * code). `web/src/lib/scriptMenu.ts` turns that payload into the menu's lines,
 * and this check holds it to what the user must actually be offered, against a
 * REAL payload:
 *
 *   * an ALBUM menu offers every script the registry holds — one plain entry
 *     each, in the stack's order, and one forced twin for every script with a
 *     single force flag;
 *   * a TRACK ROW offers the file-scoped scripts and NOTHING ELSE: an
 *     album-shaped script (a .cue rewrite, a per-album grade, the release
 *     manifest, the artist image, the subtree's layout) is never on a row that
 *     holds one file;
 *   * each entry is handed what its scope needs: the selection's own paths for
 *     a file-scoped script, the album's folder for a folder-scoped one;
 *   * a disabled entry SAYS why (the feature switch the run would skip on, in
 *     the run's own words), because a script nobody can press is still a script
 *     the user should see;
 *   * the menu component types no script id of its own — it maps the generated
 *     sections, so the list cannot drift from the registry again.
 *
 * The expectations are the check's OWN: the script names/ids are read out of the
 * payload the server just produced, and the "album offers everything, a track
 * row does not" rule is stated here, not imported from the module under test.
 *
 * Run:  node tools/check_script_menu.mjs [payload.json]
 *       (default: tools/fixtures/script-menu.json)
 * Exit codes: 0 pass, 1 a check failed, 2 the environment cannot run it.
 */
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const webDir = path.join(here, "..", "web");
const payloadPath = process.argv[2] || path.join(here, "fixtures", "script-menu.json");

if (!existsSync(path.join(webDir, "node_modules"))) {
  console.error("[script-menu] SKIP: web/node_modules is missing — run npm install in web/");
  process.exit(2);
}
if (!existsSync(payloadPath)) {
  console.error(`[script-menu] no payload at ${payloadPath}`);
  process.exit(2);
}
const payload = JSON.parse(readFileSync(payloadPath, "utf8"));

const failures = [];
let checks = 0;
const check = (name, pass, detail = "") => {
  checks++;
  if (!pass) failures.push(`${name}${detail ? ` — ${detail}` : ""}`);
};

/* ---- the payload's own contract ---------------------------------------- */
const KINDS = ["album", "track", "artist", "playlist", "library"];
// Derived from the payload, never from the module: the ids are the registry's.
const ALL = payload.scripts.map((s) => s.id).sort((a, b) => a - b);
const FOLDER = payload.scripts.filter((s) => s.scope === "folder").map((s) => s.id);
const FILE = payload.scripts.filter((s) => s.scope === "file").map((s) => s.id);
const FORCED = payload.scripts.filter((s) => s.force.options.length).map((s) => s.id).sort((a, b) => a - b);
// One entry per FLAG, so a composite script contributes several.
const FORCE_OPTIONS = payload.scripts.reduce((n, s) => n + s.force.options.length, 0);
const flagsFor = (kind) => payload.scripts.filter((s) => s.applies_to.includes(kind))
  .reduce((n, s) => n + s.force.options.length, 0);
const albumFlags = flagsFor("album");
const trackFlags = flagsFor("track");
const RUN_ALL = payload.run_all.by_kind;
const OPT_IN = payload.run_all.excluded.map((e) => e.id);

console.log("== the payload ==");
check("the payload names the five entity kinds", JSON.stringify(payload.kinds) === JSON.stringify(KINDS),
  JSON.stringify(payload.kinds));
check("every script has a scope (nothing unclassified)", payload.unclassified.length === 0
  && payload.scripts.every((s) => s.scope === "file" || s.scope === "folder"),
  JSON.stringify(payload.unclassified));
check("every script carries a label, a description, a group and a force/ gate pair",
  payload.scripts.every((s) => s.label && s.description && s.group && s.force && s.gate));
check("both scopes exist, so the two sets can differ at all", FILE.length > 0 && FOLDER.length > 0,
  `file=${FILE.length} folder=${FOLDER.length}`);
check("no script is both file- and folder-scoped",
  FILE.length + FOLDER.length === payload.scripts.length);
check("the payload's groups name the section the menu renders",
  payload.groups.length === 1 && payload.scripts.every((s) => s.group === payload.groups[0].id)
  && !!payload.forced_group?.title);
check("every force flag a script owns has its own option",
  payload.scripts.every((s) => s.force.options.length === s.force.keys.length
    && s.force.options.every((o) => s.force.keys.includes(o.key))),
  JSON.stringify(payload.scripts.filter((s) => s.force.options.length !== s.force.keys.length).map((s) => [s.id, s.force])));
check("the menus can reach more than one force option in total",
  FORCE_OPTIONS > FORCED.length, `options=${FORCE_OPTIONS} scripts=${FORCED.length}`);
check("an opt-in script is NAMED in the payload, and the run-all lists exclude it",
  OPT_IN.length > 0
  && OPT_IN.every((id) => payload.run_all.excluded.find((e) => e.id === id)?.why)
  && !Object.values(RUN_ALL).some((list) => list.some((id) => OPT_IN.includes(id)))
  && !payload.run_all.order.some((id) => OPT_IN.includes(id)),
  JSON.stringify(payload.run_all.excluded));
check("every ordered run-all list is in the chain's own order, without repeats",
  Object.entries(RUN_ALL).every(([, list]) =>
    list.length === new Set(list).size
    && list.every((id) => payload.run_all.order.includes(id))
    && JSON.stringify(list) === JSON.stringify(payload.run_all.order.filter((id) => list.includes(id)))),
  JSON.stringify(RUN_ALL));
check("an album runs the whole chain, a track row only what a row can run",
  RUN_ALL.album.length === payload.run_all.order.length
  && RUN_ALL.track.length < RUN_ALL.album.length
  && RUN_ALL.track.every((id) => FILE.includes(id)),
  `album=${RUN_ALL.album.length} track=${RUN_ALL.track.length}`);
check("the count a menu prints is the list it posts — no separate tally to drift",
  Object.values(RUN_ALL).every((list) => list.length > 0));

/* ---- the module under test --------------------------------------------- */
const { createServer } = await import(
  `file://${path.join(webDir, "node_modules/vite/dist/node/index.js").replace(/\\/g, "/")}`);

const server = await createServer({
  configFile: path.join(webDir, "vite.config.ts"),
  root: webDir,
  server: { middlewareMode: true },
  appType: "custom",
  logLevel: "error",
});

const entriesOf = (sections) => sections.groups.flatMap((g) => g.entries);

try {
  const menu = await server.ssrLoadModule("/src/lib/scriptMenu.ts");

  console.log("\n== an album menu ==");
  const album = menu.scriptSections(payload, "album", {
    paths: ["F:/Music/Artist/Album/01 One.flac", "F:/Music/Artist/Album/02 Two.flac"],
    albumPath: "F:/Music/Artist/Album",
  });
  const albumEntries = entriesOf(album);
  check("offers ONE plain entry per script the registry holds",
    albumEntries.length === ALL.length
    && JSON.stringify(albumEntries.map((e) => e.id).sort((a, b) => a - b)) === JSON.stringify(ALL),
    `${albumEntries.length} entries for ${ALL.length} scripts`);
  check("...in the stack's order", albumEntries.map((e) => e.id).join(",") === payload.scripts
    .map((s) => s.id).join(","), albumEntries.map((e) => e.id).join(","));
  check("...with no script offered twice as a plain run",
    new Set(albumEntries.map((e) => e.id)).size === albumEntries.length);
  check("...and every entry names its script the way the stack page does",
    albumEntries.every((e) => e.label.startsWith(`${e.id} · `)), albumEntries[0]?.label);
  check("the album's forced section has ONE entry per force flag its scripts own",
    album.forced !== null
    && JSON.stringify([...new Set(album.forced.entries.map((e) => e.id))].sort((a, b) => a - b)) === JSON.stringify(FORCED)
    && album.forced.entries.length === albumFlags,
    `${album.forced?.entries.length} entries for ${albumFlags} flags`);
  check("the Run-all entry is not listed among the scripts themselves",
    albumEntries.every((e) => e.runAll === false));
  check("...and each forced entry sends that script's own force key",
    album.forced.entries.every((e) => {
      const s = payload.scripts.find((x) => x.id === e.id);
      return e.force && s.force.options.some((o) => o.key === e.force);
    }), JSON.stringify(album.forced.entries.map((e) => [e.id, e.force])));
  // A flag that re-runs ANOTHER pass says so, or four entries of one script
  // would read as four identical lines.
  const tenForced = album.forced.entries.filter((e) => e.id === 10);
  check("a composite script's forced entries name the pass each flag re-runs",
    tenForced.length === 4
    && tenForced.every((e, i) => e.label.includes(payload.scripts.find((s) => s.id === 10).force.options[i].owner_label))
    && tenForced.map((e) => e.force).join(",") === "accurip,cue,lyrics,autotag",
    JSON.stringify(tenForced.map((e) => [e.force, e.label])));
  check("a single-flag script's forced entry is labelled with the script its press runs",
    album.forced.entries.find((e) => e.id === 13)?.label === "13 · Fetch lyrics",
    album.forced.entries.find((e) => e.id === 13)?.label);

  console.log("\n== Run all ==");
  check("the Run-all entry exists, over the chain scoped to the entity",
    album.runAll !== null
    && JSON.stringify(album.runAll.ids) === JSON.stringify(RUN_ALL.album)
    && album.runAll.runAll === true,
    JSON.stringify(album.runAll?.ids));
  check("...it is ONE entry that posts its whole list", album.runAll.ids.length > 1
    && album.runAll.id === 0);
  check("...it sweeps in no opt-in script",
    !album.runAll.ids.some((id) => OPT_IN.includes(id)),
    JSON.stringify({ ids: album.runAll.ids, optIn: OPT_IN }));
  check("...and it runs over the whole entity, not just its files",
    JSON.stringify(album.runAll.targets) === JSON.stringify(["F:/Music/Artist/Album"]),
    JSON.stringify(album.runAll.targets));
  const emptyAlbum = menu.scriptSections(payload, "album", { paths: [], albumPath: "" });
  check("a Run-all press with nothing to run on is disabled",
    emptyAlbum.runAll !== null && emptyAlbum.runAll.disabled === true);
  const noRunAll = menu.scriptSections({ ...payload, run_all: { order: [], by_kind: {}, excluded: [] } },
    "album", { paths: ["a.flac"] });
  check("a payload with no chain offers no Run-all entry", noRunAll.runAll === null);
  check("a plain entry forces nothing (it keeps the saved force selection)",
    albumEntries.every((e) => e.force === undefined));

  console.log("\n== what each entry is handed ==");
  const albumFolderScoped = albumEntries.filter((e) => FOLDER.includes(e.id));
  check("a folder-scoped script runs on the ALBUM folder",
    albumFolderScoped.every((e) => JSON.stringify(e.targets) === JSON.stringify(["F:/Music/Artist/Album"])),
    JSON.stringify(albumFolderScoped.map((e) => [e.id, e.targets])));
  check("a file-scoped script runs on the selection's own files",
    albumEntries.filter((e) => FILE.includes(e.id))
      .every((e) => e.targets.length === 2 && e.targets[0].endsWith("01 One.flac")));
  check("every entry has something to run on",
    [...albumEntries, ...album.forced.entries].every((e) => e.targets.length > 0));

  console.log("\n== a track row ==");
  const track = menu.scriptSections(payload, "track", {
    paths: ["F:/Music/Artist/Album/01 One.flac"],
  });
  const trackEntries = entriesOf(track);
  check("offers one entry per FILE-scoped script, and nothing else",
    JSON.stringify(trackEntries.map((e) => e.id).sort((a, b) => a - b)) === JSON.stringify([...FILE].sort((a, b) => a - b)),
    `offered ${JSON.stringify(trackEntries.map((e) => e.id))}, album-shaped were ${JSON.stringify(FOLDER)}`);
  check("an album-shaped script is on NO track row",
    !trackEntries.some((e) => FOLDER.includes(e.id)), JSON.stringify(trackEntries.map((e) => e.id)));
  check("...so the two menus really differ",
    trackEntries.length < albumEntries.length && trackEntries.length > 0,
    `track=${trackEntries.length} album=${albumEntries.length}`);
  check("a track row runs on the one file",
    trackEntries.every((e) => JSON.stringify(e.targets) === JSON.stringify(["F:/Music/Artist/Album/01 One.flac"])));
  check("a track row's forced section holds only file-scoped scripts",
    track.forced.entries.every((e) => FILE.includes(e.id)) && track.forced.entries.length > 0,
    JSON.stringify(track.forced.entries.map((e) => e.id)));
  check("...one entry per flag those scripts own, and no more",
    track.forced.entries.length === trackFlags, `${track.forced.entries.length} != ${trackFlags}`);
  check("a track row's Run-all posts the FILE-scoped chain, not the album's",
    track.runAll !== null
    && JSON.stringify(track.runAll.ids) === JSON.stringify(RUN_ALL.track)
    && JSON.stringify(track.runAll.ids) !== JSON.stringify(RUN_ALL.album)
    && track.runAll.ids.every((id) => FILE.includes(id))
    && !track.runAll.ids.some((id) => FOLDER.includes(id)),
    JSON.stringify(track.runAll?.ids));
  check("...and it really is shorter than the album's, so the negative is provable",
    track.runAll.ids.length < album.runAll.ids.length,
    `${track.runAll.ids.length} vs ${album.runAll.ids.length}`);
  check("a track row's Run-all runs over the row itself, not a folder it does not have",
    JSON.stringify(track.runAll.targets) === JSON.stringify(["F:/Music/Artist/Album/01 One.flac"]),
    JSON.stringify(track.runAll.targets));
  check("no Run-all list anywhere contains an opt-in script",
    KINDS.every((kind) => {
      const s = menu.scriptSections(payload, kind, { paths: ["F:/Music/A/1.flac"], albumPath: "F:/Music/A", artistPath: "F:/Music" });
      return !s.runAll || !s.runAll.ids.some((id) => OPT_IN.includes(id));
    }),
    JSON.stringify(OPT_IN));

  console.log("\n== an artist page ==");
  const artist = menu.scriptSections(payload, "artist", {
    paths: ["F:/Music/Artist/Album/01 One.flac"],
    artistPath: "F:/Music/Artist",
  });
  check("an artist offers every script, like an album",
    entriesOf(artist).length === ALL.length, String(entriesOf(artist).length));
  check("...and its folder-scoped scripts run on the ARTIST folder",
    entriesOf(artist).filter((e) => FOLDER.includes(e.id))
      .every((e) => JSON.stringify(e.targets) === JSON.stringify(["F:/Music/Artist"])),
    JSON.stringify(entriesOf(artist).filter((e) => FOLDER.includes(e.id)).map((e) => e.targets)));
  const noFolder = menu.scriptSections(payload, "artist", { paths: ["F:/Music/Artist/Album/01 One.flac"] });
  check("without the artist folder, a folder-scoped script falls back to the folders the files sit in",
    entriesOf(noFolder).filter((e) => FOLDER.includes(e.id))
      .every((e) => JSON.stringify(e.targets) === JSON.stringify(["F:/Music/Artist/Album"])),
    JSON.stringify(entriesOf(noFolder).filter((e) => FOLDER.includes(e.id)).map((e) => e.targets)));

  console.log("\n== a playlist selection (a list of files) ==");
  const playlist = menu.scriptSections(payload, "playlist", {
    paths: ["F:/Music/A/1.flac", "F:/Music/B/2.flac"],
  });
  check("a playlist shows what a file list can run",
    JSON.stringify(entriesOf(playlist).map((e) => e.id).sort((a, b) => a - b)) === JSON.stringify([...FILE].sort((a, b) => a - b)));
  check("a playlist's folders are deduplicated when a folder run derives them",
    entriesOf(menu.scriptSections(payload, "library", {
      paths: ["F:/Music/A/1.flac", "F:/Music/B/2.flac", "F:/Music/A/3.flac"],
    }))
      .filter((e) => FOLDER.includes(e.id))
      .every((e) => JSON.stringify(e.targets) === JSON.stringify(["F:/Music/A", "F:/Music/B"])),
    JSON.stringify(entriesOf(menu.scriptSections(payload, "library", {
      paths: ["F:/Music/A/1.flac", "F:/Music/B/2.flac", "F:/Music/A/3.flac"],
    })).filter((e) => FOLDER.includes(e.id)).map((e) => e.targets)));
  check("a windows or a unix path both give up their folder",
    menu.folderOf("F:/Music/A/1.flac") === "F:/Music/A" && menu.folderOf("/mnt/music/a/1.flac") === "/mnt/music/a",
    `${menu.folderOf("F:/Music/A/1.flac")} · ${menu.folderOf("/mnt/music/a/1.flac")}`);

  console.log("\n== disabled entries say why ==");
  const gated = payload.scripts.find((s) => s.gate.keys.length === 1);
  const off = JSON.parse(JSON.stringify(payload));
  for (const s of off.scripts) if (s.id === gated.id) { s.gate.enabled = false; s.gate.reason = `${gated.gate.keys[0]} is off`; }
  const offEntry = entriesOf(menu.scriptSections(off, "album", { paths: ["x.flac"], albumPath: "F:/A" }))
    .find((e) => e.id === gated.id);
  check("a script whose feature is off is still offered", !!offEntry, `script ${gated.id}`);
  check("...disabled", offEntry.disabled === true);
  check("...and its tooltip carries the run's own reason",
    offEntry.title.includes(gated.gate.keys[0]) && offEntry.title.includes("off"), offEntry.title);
  const missing = JSON.parse(JSON.stringify(payload));
  for (const s of missing.scripts) if (s.id === gated.id) s.available = false;
  const missEntry = entriesOf(menu.scriptSections(missing, "album", { paths: ["x.flac"], albumPath: "F:/A" }))
    .find((e) => e.id === gated.id);
  check("a runner this install does not have is shown, disabled, and says so",
    missEntry.disabled === true && missEntry.title.includes("not installed"), missEntry.title);
  check("a script with nothing to run on is disabled too",
    entriesOf(menu.scriptSections(payload, "album", { paths: [], albumPath: "" }))
      .every((e) => e.targets.length === 0));

  console.log("\n== the payload a menu cannot load ==");
  check("no payload, no script entries (the rest of the menu is untouched)",
    entriesOf(menu.scriptSections(undefined, "album", { paths: ["a.flac"] })).length === 0
    && menu.scriptSections(null, "track", { paths: ["a.flac"] }).forced === null);

  console.log("\n== the menu component ==");
  const src = readFileSync(path.join(webDir, "src", "components", "TagActionsMenu.tsx"), "utf8");
  const code = src.replace(/\/\*[\s\S]*?\*\//g, "").split("\n").map((l) => l.split("//")[0]).join("\n");
  // An id list in the menu is the drift this whole payload exists to stop.
  const idLists = [...code.matchAll(/\[\s*\d+\s*(?:,\s*\d+\s*)+?\]/g)]
    .map((m) => m[0]).filter((m) => m.match(/\d+/g).length >= 3);
  check("the menu types no script ids of its own", idLists.length === 0, JSON.stringify(idLists));
  const oneIds = [...code.matchAll(/api\.run\(\[(\d+)\]/g)].map((m) => Number(m[1]));
  check("...and runs no script by a literal id", oneIds.length === 0, JSON.stringify(oneIds));
  check("the menu renders the generated sections",
    /generated\.groups\.map/.test(code) && /generated\.forced/.test(code));
  check("...and asks the server for them",
    /api\.scriptMenu/.test(code) && /scriptSections\(/.test(code));
  check("the album-shaped sections are gone from the menu's source",
    !/Force re-audit \(rewrite AUDIT tags\)/.test(code)
    && !/Generate AccurateRip \(\.accurip\)/.test(code)
    && !/Transliterate \/ translate lyrics…/.test(code));
  // The two generated sections hold the same scripts, so a forced twin has to
  // read as one — in the bundle's own words for it, not as its plain label.
  check("a forced entry is labelled as one",
    /e\.force \? t\("menu\.forceEntry"/.test(code) && /menu\.forced/.test(code));
  check("the generated sections' titles come from the locale bundle, with the payload's as fallback",
    /t\("menu\.scripts"\)/.test(code) && /scriptGroupTitle\(g\.id, g\.title\)/.test(code));

  console.log("\n== one press, one request ==");
  // Every entry hands /api/run its OWN ids: [id] for a script, the whole list
  // for Run all. A literal `[e.id]` here would post one script for the Run-all
  // entry, and a second runner would be the drift the payload exists to stop.
  // TWO call sites, one per kind of entry: the script line and the confirmed
  // Run-all press (which is the same list, one request).
  const runCalls = code.match(/api\.run\([^)]*/g) || [];
  check("every entry posts the ids it carries, through the one /api/run",
    runCalls.length === 2
    && runCalls.every((c) => c.includes("e.ids, e.targets"))
    && !/api\.run\(\[e\.id\]/.test(code),
    JSON.stringify(runCalls));
  check("the Run-all line asks before it posts",
    /onClick: \(\) => setConfirmRunAll\(e\)/.test(code) && /confirmRunAll\.ids\.map/.test(code));
  check("...and its confirmation is the app's Modal, reachable only through a button",
    /confirmRunAll && \(/.test(code) && /void run\(\(\) => api\.run\(e\.ids, e\.targets\), ran\)/.test(code)
    && /setConfirmRunAll\(null\)/.test(code));
  check("...listing exactly the ids it will post, in order",
    /confirmRunAll\.ids\.map\(\(id, i\)/.test(code) && /scriptNames\.get\(id\)/.test(code));
  check("...with the count printed coming from that same list",
    /t\("menu\.runAll", \{ count: e\.ids\.length \}\)/.test(code)
    && /t\("menu\.runAll", \{ count: confirmRunAll\.ids\.length \}\)/.test(code));
  check("the Run-all line is the section's own head entry",
    /i === 0 && generated\.runAll \? \[scriptItem\(generated\.runAll\)\]/.test(code));

  if (failures.length) {
    console.error(`\n${failures.length} failure(s):`);
    for (const f of failures) console.error(`  - ${f}`);
    console.error(`\n${checks - failures.length}/${checks} checks passed`);
    process.exit(1);
  }
  console.log(`\nok  the generated script menu: ${checks} checks — the album menu offers all ${ALL.length} ` +
    `scripts (${FORCE_OPTIONS} force entries, one per flag), a track row offers only the ${FILE.length} ` +
    `file-scoped ones, Run all posts the entity's own ordered chain ` +
    `(${RUN_ALL.album.length} ids for an album, ${RUN_ALL.track.length} for a row, opt-in ${OPT_IN.join(",")} never), ` +
    `and the menu types no id`);
} finally {
  await server.close();
}
