// Customizable keyboard bindings for the lyrics editor.
// Bindings are stored per-browser in localStorage ("mlo.lyricsKeys") and
// serialized as "Mod+Alt+<code>" using KeyboardEvent.code values, so they
// are layout-independent (Space stays Space, letters are physical keys).

export type LyricsAction =
  | "prevLine"
  | "nextLine"
  | "undo"
  | "save";

export const LYRICS_ACTIONS: { id: LyricsAction; label: string; hint: string }[] = [
  { id: "prevLine", label: "Previous line", hint: "Select the line above" },
  { id: "nextLine", label: "Next line", hint: "Select the line below" },
  { id: "undo", label: "Undo", hint: "Revert the last edit" },
  { id: "save", label: "Save", hint: "Save the lyrics" },
];

export const LYRICS_KEY_DEFAULTS: Record<LyricsAction, string> = {
  prevLine: "ArrowUp",
  nextLine: "ArrowDown",
  undo: "Ctrl+KeyZ",
  save: "Ctrl+KeyS",
};

const STORAGE_KEY = "mlo.lyricsKeys";

export function loadLyricsKeys(): Record<LyricsAction, string> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      return { ...LYRICS_KEY_DEFAULTS, ...JSON.parse(raw) };
    }
  } catch {
    /* fall through to defaults */
  }
  return { ...LYRICS_KEY_DEFAULTS };
}

export function saveLyricsKeys(map: Record<LyricsAction, string>) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(map));
}

export function resetLyricsKeys() {
  localStorage.removeItem(STORAGE_KEY);
}

/** Serialize a KeyboardEvent the same way bindings are stored. */
export function keyLabel(e: KeyboardEvent): string {
  const parts: string[] = [];
  if (e.ctrlKey) parts.push("Ctrl");
  if (e.altKey) parts.push("Alt");
  if (e.shiftKey) parts.push("Shift");
  if (e.metaKey) parts.push("Meta");
  parts.push(e.code || e.key);
  return parts.join("+");
}

/** True when the event matches the binding for an action. */
export function matchKey(e: KeyboardEvent, map: Record<LyricsAction, string>, action: LyricsAction): boolean {
  return keyLabel(e) === map[action];
}
