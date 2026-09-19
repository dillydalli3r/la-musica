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
];

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
 *  publish → 17 transliterate, 8 auto tagging, 5 images, 6 audit, 7 DR &
 *  ReplayGain, 9 AccurateRip (its own sidecar names), 12 key & BPM, 16 mood,
 *  15 the release manifest (after the tagger that gives it its release id),
 *  10 format all, and 4 grade last. This mirrors server/imports.py's
 *  DEFAULT_CHAIN, which lists the same steps minus the opt-in 16/17. */
export const DEFAULT_RUN_ALL = [11, 3, 14, 15, 2, 1, 13, 18, 17, 8, 5, 6, 7, 9, 12, 16, 10, 4];

/** True when the id is a script the runner knows about. */
export function isScriptId(n: unknown): n is number {
  return typeof n === "number" && SCRIPTS.some((s) => s.ids.includes(n));
}
