/** Library scripts — the single source of truth for every menu that lists
 * them (Optimization page, library selection menu, Settings Run-All order).
 * Numbers match the runner registry in server/main.py and mlo/cliapp.py, and
 * the table in README.md. Add a script here once; every surface picks it up. */
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
  { ids: [15], label: "Lyrics xlit / translate (AI)" },
];

/** Script number → label, for surfaces that render a bare id. */
export const SCRIPT_LABEL: Record<number, string> = Object.fromEntries(
  SCRIPTS.map((s) => [s.ids[0], s.label])
);

/** Default Run All order: videos first (slow, bit-exact), grading last. */
export const DEFAULT_RUN_ALL = [11, 14, 1, 2, 8, 13, 15, 12, 3, 5, 9, 6, 4, 7, 10];

/** True when the id is a script the runner knows about. */
export function isScriptId(n: unknown): n is number {
  return typeof n === "number" && SCRIPTS.some((s) => s.ids.includes(n));
}
