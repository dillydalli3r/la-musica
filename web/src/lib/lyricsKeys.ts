// Customizable keyboard bindings for the lyrics editor.
// Bindings are stored per-browser in localStorage ("mlo.lyricsKeys") and
// serialized as "Mod+Alt+<code>" using KeyboardEvent.code values, so they
// are layout-independent (Space stays Space, letters are physical keys).

export type LyricsAction =
  | "playPause"
  | "stampLine"
  | "stampWord"
  | "stampSyllable"
  | "seekBack"
  | "seekForward"
  | "prevLine"
  | "nextLine"
  | "undo"
  | "save"
  | "speedSlower"
  | "speedFaster";

export const LYRICS_ACTIONS: { id: LyricsAction; label: string; hint: string }[] = [
  { id: "stampLine", label: "Stamp line time", hint: "Assign the current playback time to the selected line and advance" },
  { id: "stampWord", label: "Stamp word time", hint: "Assign the current time to the next word of the line (ELRC)" },
  { id: "stampSyllable", label: "Stamp syllable time", hint: "Assign the current time to the next SYLLABLE of the line (tap along with the vocals)" },
  { id: "playPause", label: "Play / pause", hint: "Toggle preview playback" },
  { id: "seekBack", label: "Seek back 2s", hint: "Move playback back two seconds" },
  { id: "seekForward", label: "Seek forward 2s", hint: "Move playback forward two seconds" },
  { id: "prevLine", label: "Previous line", hint: "Select the line above" },
  { id: "nextLine", label: "Next line", hint: "Select the line below" },
  { id: "undo", label: "Undo", hint: "Revert the last edit" },
  { id: "save", label: "Save", hint: "Save the lyrics" },
  { id: "speedSlower", label: "Slower", hint: "Step preview playback speed down (timestamps stay in song time)" },
  { id: "speedFaster", label: "Faster", hint: "Step preview playback speed up" },
];

export const LYRICS_KEY_DEFAULTS: Record<LyricsAction, string> = {
  playPause: "KeyK",
  stampLine: "Space",
  stampWord: "KeyW",
  stampSyllable: "KeyD",
  seekBack: "ArrowLeft",
  seekForward: "ArrowRight",
  prevLine: "ArrowUp",
  nextLine: "ArrowDown",
  undo: "Ctrl+KeyZ",
  save: "Ctrl+KeyS",
  speedSlower: "BracketLeft",
  speedFaster: "BracketRight",
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
