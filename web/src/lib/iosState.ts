import { IN_TAURI } from "../api";

/** The iOS shell's own view of the audio session, for the playback report.
 *
 *  ## Why this module exists
 *
 *  The playback black box (`lib/pbDiag.ts`) can only record what the PAGE saw:
 *  the element's events, the mediaSession callbacks iOS chose to deliver, the
 *  WebAudio context's state. The two candidate causes of the owner's report —
 *  "audio stops and the lock-screen controls do nothing" — both live mostly on
 *  the other side of the wire: whether the shell's `AVAudioSession` is active
 *  and in which category, whether the app was told to deactivate it, whether the
 *  Now Playing info the lock screen draws is still set. A report that only says
 *  "the page heard nothing" cannot tell "iOS never delivered the press" from
 *  "the shell never claimed the session"; this reads the shell's own answer into
 *  the same report, and it is the shell half
 *  (`desktop/src-tauri/src/ios_audio.rs`) that produces it.
 *
 *  ## It must never be able to break anything
 *
 *  Exactly like `lib/iosAudio.ts` and `lib/iosFavs.ts`: inert outside the Tauri
 *  shell (`IN_TAURI`), the import dynamic so the browser bundle stays clean,
 *  and every failure swallowed — no such command (an older shell), a refused
 *  call, an answer in a shape this build does not know. The caller gets `null`
 *  and the panel says "no shell (browser)", which is a true statement about a
 *  report rather than a broken Settings page. */

/** MUST match the `ios_audio_state` Tauri command in
 *  `desktop/src-tauri/src/lib.rs`. */
const STATE_COMMAND = "ios_audio_state";

/** The shell's audio-session state as `[key, value]` rows, or null when there
 *  is no shell to ask. Values are stringified here (never formatted): the panel
 *  renders value-first rows and the copy button writes them verbatim. */
export async function iosShellState(): Promise<[string, string][] | null> {
  if (!IN_TAURI) return null;
  try {
    // Dynamic on purpose, exactly like the two bridges: a static import would
    // put a Tauri-only module in the browser bundle, where nothing answers it.
    const { invoke } = await import("@tauri-apps/api/core");
    const rows = await invoke(STATE_COMMAND);
    if (!Array.isArray(rows)) return null;
    // Shape-checked rather than trusted: this command is written in parallel
    // with the panel, and a report is only worth reading if a half-built shell
    // yields "no shell" instead of a row of `undefined`.
    const out: [string, string][] = [];
    for (const row of rows) {
      if (!Array.isArray(row) || row.length < 2) continue;
      const [k, v] = row as unknown[];
      if (k == null) continue;
      out.push([String(k), v == null ? "" : String(v)]);
    }
    return out;
  } catch {
    /* No such command, no event API, a refused call: the panel shows the
     * browser case, and nothing about playback is affected. */
    return null;
  }
}
