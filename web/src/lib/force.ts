/** Which per-script "force" overrides the header Force switch applies.
 * Shared by the Run All button and the library selection runs; persisted
 * per browser so the selection survives reloads. */
export const FORCE_SCRIPTS: { key: string; label: string }[] = [
  { key: "lyrics", label: "1 · Lyrics re-format" },
  { key: "cue", label: "2 · CUE re-format" },
  { key: "flac", label: "3 · FLAC re-encode" },
  { key: "images", label: "5 · Image re-process" },
  { key: "audit", label: "6 · Re-audit" },
  { key: "dr", label: "7 · DR / ReplayGain re-run" },
  { key: "autotag", label: "8 · AutoTag re-run" },
  { key: "accurip", label: "9 · AccurateRip re-generate" },
  { key: "audiometa", label: "12 · Key & BPM re-analysis" },
  { key: "tracklist", label: "15 · Release tracklist rewrite" },
  { key: "mood", label: "16 · Mood & Energy re-analysis" },
  { key: "xlit", label: "17 · Lyrics re-transliterate / re-translate" },
  { key: "publish", label: "18 · Lyrics re-publish to LRCLIB" },
  // Script 20 is the one force key that turns work OFF rather than redoing it:
  // the layout pass always scans, and its apply (rename wrong-case names,
  // gather loose audio) is what an unticked box asks to skip for this run.
  { key: "layout", label: "20 · Layout fix" },
];

const KEY = "mlo.force.sel";

export function loadForceSel(): Record<string, boolean> {
  const defaults = Object.fromEntries(FORCE_SCRIPTS.map((f) => [f.key, true]));
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) {
      const saved = JSON.parse(raw) as Record<string, boolean>;
      // A selection saved before a key existed must not read as "off" for it:
      // the one-shot Force menu sends exactly the keys it knows, and a key it
      // does not name is cleared server-side — so a switch added later would
      // be silently disabled for every user who had ever opened the menu. A
      // key the saved selection does not mention keeps its default.
      return { ...defaults, ...saved };
    }
  } catch {
    /* fall through to default */
  }
  return defaults;
}

export function saveForceSel(sel: Record<string, boolean>) {
  localStorage.setItem(KEY, JSON.stringify(sel));
}

/** {lyrics: true, ...} for api.run's force argument — only selected keys. */
export function forceDict(sel: Record<string, boolean>): Record<string, boolean> {
  const out: Record<string, boolean> = {};
  for (const f of FORCE_SCRIPTS) if (sel[f.key]) out[f.key] = true;
  return out;
}
