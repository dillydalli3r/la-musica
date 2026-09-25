import { useEffect } from "react";
import { IN_TAURI } from "../api";
import { note } from "./pbDiag";

/** The iOS shell's audio session, told when the player is making sound.
 *
 *  ## Why this module exists
 *
 *  iOS decides whether a backgrounded app may keep playing by asking its audio
 *  session, and Apple's guidance for that session is to activate it when
 *  playback BEGINS and hand it back when playback stops — "defer this call
 *  until your app begins audio playback… to ensure that you won't prematurely
 *  interrupt any other background audio that may be playing". A session
 *  activated at launch does exactly that to somebody else, and a session that
 *  is never re-asserted is what iOS takes away from a backgrounded app, which
 *  is the owner's report: "audio still stops playing from the app when it tabs
 *  out… if I pause / play again the audio works, then doesn't work after
 *  entering and exiting the app again."
 *
 *  The shell half is `desktop/src-tauri/src/ios_audio.rs`; this is the wire
 *  between them. The player's own `playing` state is the signal — the same
 *  state the bar and the fullscreen player draw their play/pause from, so it
 *  is true exactly while sound is coming out (an element iOS paused, a decode
 *  error, an interruption: all of them clear it through the store).
 *
 *  ## It must never be able to break playback
 *
 *  Exactly like `lib/iosFavs.ts`, and for the same reason: inert outside the
 *  Tauri shell (`IN_TAURI`), every failure swallowed inside it. A shell too old
 *  to know the command, a refused call, an event API that is not there — the
 *  app keeps playing, and the session keeps whatever state it had.
 */

/** MUST match the `set_playback_active` Tauri command in
 *  `desktop/src-tauri/src/lib.rs`. */
const SET_ACTIVE_COMMAND = "set_playback_active";

/** Tell the shell whether the player is producing sound. `playing` is the
 *  store's currently-playing track path, or null when nothing is playing —
 *  exactly what the player bar already has. */
export function useIosPlaybackBridge(playing: string | null) {
  const active = playing != null;
  useEffect(() => {
    if (!IN_TAURI) return;
    // The push itself goes in the report: the heartbeat's `playing` and the
    // element's own events say what the app believed, and this row says what
    // the app then TOLD the shell — the pair is what separates "the shell was
    // never told" from "the shell was told and did not act".
    note("iosAudio", { active });
    void (async () => {
      try {
        // Dynamic on purpose, exactly like the star's bridge: a static import
        // would put a Tauri-only module in the browser bundle too.
        const { invoke } = await import("@tauri-apps/api/core");
        await invoke(SET_ACTIVE_COMMAND, { active });
      } catch {
        /* No such command (an older shell), or any other refusal: silence.
         * Nothing about the OS's audio furniture is worth an exception in the
         * player's render path. */
      }
    })();
  }, [active]);
}
