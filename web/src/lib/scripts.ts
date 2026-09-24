/** Library scripts — the single source of truth for every menu that lists
 * them (Optimization page, library selection menu, Settings Run-All order).
 * Numbers match the runner registry in server/main.py, the table in README.md
 * and the frozen EXPECTED_SCRIPTS in tools/test_script_menus.py, which gates
 * that agreement. Add a script here once; every surface picks it up. */
export const SCRIPTS: { ids: number[]; label: string }[] = [
  { ids: [1], label: "Format lyrics" },
  { ids: [2], label: "Format CUEs" },
  { ids: [3], label: "Optimize FLACs" },
  { ids: [4], label: "Grade" },
  { ids: [5], label: "Process images" },
  { ids: [6], label: "Audit library" },
  { ids: [7], label: "DR & ReplayGain" },
  { ids: [8], label: "Auto tagging" },
  { ids: [9], label: "AccurateRip" },
  { ids: [10], label: "Format all" },
  { ids: [11], label: "Remux videos (MKV)" },
  { ids: [12], label: "Key & BPM" },
  { ids: [13], label: "Fetch lyrics" },
  { ids: [14], label: "Beets tagging" },
  { ids: [15], label: "Release tracklist" },
  { ids: [16], label: "Mood & Energy" },
  { ids: [17], label: "Lyrics transliterate (AI)" },
  { ids: [18], label: "Publish lyrics (LRCLIB)" },
  { ids: [19], label: "Optimize artist images" },
  { ids: [20], label: "Scan library layout" },
  { ids: [21], label: "Fix AcoustID pairs" },
  { ids: [22], label: "Submit fingerprints (AcoustID)" },
];

/** The scripts the SHIPPED Run All chain deliberately does not carry
 *  (`server.script_runners.OPT_IN_SCRIPTS`): their work leaves the machine —
 *  22 publishes a fingerprint + MusicBrainz recording id to AcoustID's public
 *  database — so shipping them in the default chain would publish on every
 *  import of every install without anyone asking. They are offered everywhere
 *  a script is; a user who ticks one gets it in their own order. */
export const OPT_IN_SCRIPTS: number[] = [22];

/** Script number → label, for surfaces that render a bare id. */
export const SCRIPT_LABEL: Record<number, string> = Object.fromEntries(
  SCRIPTS.map((s) => [s.ids[0], s.label])
);

/** Default Run All order: PATH-CHANGING SCRIPTS FIRST, then content, then the
 *  library-wide grader.
 *
 *  11 videos → 3 FLACs (a lossless conversion changes the extension) → 14
 *  beets (`move: yes`: it renames and moves the album) → 2 CUEs (canonical
 *  sidecar names AND the cue's FILE lines, now pointed at the names the album
 *  actually has) → 1 lyrics format (writes .lrc named after the track). From
 *  there every script reads or writes final paths: 13 fetch lyrics → 18
 *  publish → 17 transliterate, 8 auto tagging, 5 images → 19 the artist images
 *  stored beside them, 6 audit, 7 DR & ReplayGain, 9 AccurateRip (its own
 *  sidecar names), 12 key & BPM, 16 mood, 15 the release manifest (after the
 *  tagger that gives it its release id), 10 format all, 20 the layout report
 *  of the tree format all just settled, 21 the AcoustID pair the grader then
 *  reads as complete, and 4 grade last. This mirrors
 *  server/imports.py's DEFAULT_CHAIN, which lists the same steps minus the
 *  opt-in 16/17 and 19 — and minus 20, which describes the whole music folder
 *  and so has nothing to say about the one album a chain is finishing. */
export const DEFAULT_RUN_ALL = [11, 3, 14, 15, 2, 1, 13, 18, 17, 8, 5, 19, 6, 7, 9, 12, 16, 10, 20, 21, 4];

/** True when the id is a script the runner knows about. */
export function isScriptId(n: unknown): n is number {
  return typeof n === "number" && SCRIPTS.some((s) => s.ids.includes(n));
}
