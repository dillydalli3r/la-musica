import { useEffect, useRef } from "react";
import { IN_TAURI } from "../api";
import { useFav } from "./favs";

/** The iOS shell's Now Playing star, wired to the app's own like store.
 *
 *  ## Why this module exists
 *
 *  On iOS the Now Playing module (Control Center, the lock screen, CarPlay)
 *  draws a star for the track the system believes is playing, and pressing it
 *  sends `MPRemoteCommandCenter.likeCommand` to the APP — the Media Session API
 *  the player already uses for play/pause/next/seek has no like action at all,
 *  so that star can only be handled natively. The shell does that
 *  (`desktop/src-tauri/src/ios_like.rs`) and this is the other half of the two
 *  wires between them:
 *
 *  * the OS star was pressed → the shell emits an event → this hook toggles the
 *    CURRENT track's like through `useFav` — the same writer every heart in the
 *    app uses, so the same optimistic patch, the same query invalidation, the
 *    same toast rules. The shell deliberately does NOT write a like itself:
 *    one writer, and the star can never disagree with the hearts on screen.
 *  * the current track, or its liked state, changed → this hook pushes that
 *    state to the shell, which mirrors it onto the OS star's `active` property
 *    ("the user already likes this item", per MediaPlayer's MPFeedbackCommand.h)
 *    so the star is filled exactly when the app's hearts are.
 *
 *  ## It must never be able to break playback
 *
 *  Everything here is inert outside the Tauri shell (`IN_TAURI`), and inside it
 *  every failure is swallowed: no such event, no such command (an older shell),
 *  a shell that refuses the call — the app keeps playing and the hearts keep
 *  working. A favourite is never worth an exception in the player's render path.
 *
 *  The gate is `IN_TAURI` alone, not `IN_MOBILE_SHELL`: the command is
 *  registered by every shell (it is a no-op off iOS), so the extra call on
 *  desktop costs one IPC per track change and buys immunity to the iPad's
 *  desktop-class user agent, which is exactly the kind of guess a favourite
 *  should not hang on. */

/** MUST match `LIKE_EVENT` in `desktop/src-tauri/src/ios_like.rs`. */
const LIKE_EVENT = "mlo-ios-like";

/** The shell command that mirrors the current track's liked state onto the OS
 *  star (see `set_now_playing_liked` in `desktop/src-tauri/src/lib.rs`). */
const SET_LIKED_COMMAND = "set_now_playing_liked";

/** Bridge the CURRENT track's like state to the iOS shell. `path` is the
 *  playing track (null while nothing is loaded) and `mbid` its MusicBrainz
 *  track id, both exactly as the player bar already has them. */
export function useIosFavBridge(path: string | null | undefined, mbid?: string | null) {
  const { fav, toggle } = useFav("track", path, mbid);

  // The listener is registered once, but the track under the star changes every
  // few minutes: the ref is what keeps a press toggling whatever is playing
  // NOW rather than whatever was playing when the event was wired up.
  const toggleRef = useRef(toggle);
  toggleRef.current = toggle;

  useEffect(() => {
    if (!IN_TAURI) return;
    let unlisten: (() => void) | undefined;
    let cancelled = false;
    void (async () => {
      try {
        // Dynamic on purpose, exactly like lib/notify.ts's plugin imports: a
        // static import would put a Tauri-only module in the browser bundle
        // too, where there is no shell behind it.
        const { listen } = await import("@tauri-apps/api/event");
        const un = await listen(LIKE_EVENT, () => toggleRef.current());
        // The component can unmount (or StrictMode can remount) while the
        // import resolves; unsubscribing here keeps exactly one listener.
        if (cancelled) un();
        else unlisten = un;
      } catch {
        /* No event API, or a shell that never emits this event: the OS star
         * simply stays a display of state, which is still true. */
      }
    })();
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, []);

  useEffect(() => {
    if (!IN_TAURI) return;
    void (async () => {
      try {
        // Dynamic for the same reason as the listener above: this module is
        // reachable from the browser build, where no shell exists.
        const { invoke } = await import("@tauri-apps/api/core");
        await invoke(SET_LIKED_COMMAND, { liked: !!path && fav });
      } catch {
        /* A shell too old to know the command (or any other refusal): silence.
         * The star keeps whatever state it had; nothing else is affected. */
      }
    })();
    // `path` is in the deps because the shell's star follows the TRACK, not
    // just the flag: moving to another track re-declares the state even when
    // both tracks happen to be liked.
  }, [path, fav]);
}
