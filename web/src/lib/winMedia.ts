import { useEffect, useRef } from "react";
import { IN_TAURI } from "../api";
import { note } from "./pbDiag";

/** Windows' own media card, fed by the app and answered by the app.
 *
 *  ## Why this module exists
 *
 *  The player already drives `navigator.mediaSession`, and inside WebView2 that
 *  does reach Windows — but as a session published by the WebView2 RUNTIME, so
 *  Windows 11's media flyout has no app to name and draws "Unknown app" over
 *  the app's own music. Nothing in the webview can change that: the id belongs
 *  to `msedgewebview2.exe`, a process that carries no Application User Model
 *  ID (see `desktop/src-tauri/src/win_media.rs` for the measurement, and
 *  docs/release-notes/release-notes-5.2.0.md for why the AUMID route is closed
 *  upstream).
 *
 *  So the SHELL publishes the session — from this app's own window, under this
 *  app's own id, which Windows resolves to "la musica" — and this module is the
 *  bridge to it, in the same shape as the iOS ones next door:
 *
 *  * the state the player hands `navigator.mediaSession` (title/artist/album/
 *    artwork, whether the element is really playing, the timeline, whether the
 *    queue can step) is pushed through the `set_now_playing` command;
 *  * a press in the flyout, on a media key or on a headset arrives on the
 *    `mlo-media-key` event and runs through the SAME handlers the page
 *    registered for `navigator.mediaSession` — one set of functions, two
 *    doors, so a press can never mean something different depending on which
 *    surface drew the button.
 *
 *  The webview's own session is switched off in `tauri.conf.json`
 *  (`additionalBrowserArgs`, `HardwareMediaKeyHandling`): two live sessions
 *  would draw two cards for one song.
 *
 *  ## It must never be able to break playback
 *
 *  Exactly like `lib/iosFavs.ts` and `lib/iosAudio.ts`, and for the same
 *  reason: inert outside the Tauri shell (`IN_TAURI`), and every failure inside
 *  it swallowed. A shell too old to know the command, a target with no such
 *  card (macOS and Linux keep the webview's own session, and the command is a
 *  no-op there), an event API that is not there — the music keeps playing. */

/** MUST match the `set_now_playing` Tauri command and `MEDIA_KEY_EVENT` in
 *  `desktop/src-tauri/src/lib.rs`. */
const SET_NOW_PLAYING = "set_now_playing";
const MEDIA_KEY_EVENT = "mlo-media-key";

/** How far the playhead may move before the card is told again, in seconds.
 *  The OS draws its own progress bar from a position and a rate, so a
 *  per-second push would be an IPC message a second for a bar that reads the
 *  same either way — and `PlayerBar` ticks far faster than that. */
const POSITION_STEP_S = 5;

/** The Media Session actions the shell can send back, named exactly as
 *  `navigator.mediaSession.setActionHandler` names them. */
export type MediaAction =
  | "play"
  | "pause"
  | "previoustrack"
  | "nexttrack"
  | "seekbackward"
  | "seekforward"
  | "seekto";

export interface WindowsMediaBridge {
  title?: string | null;
  artist?: string | null;
  album?: string | null;
  /** The artwork URL the OS fetches itself — the one the bar is already
   *  drawing, so it is in the webview's cache. */
  artwork?: string | null;
  /** Sound is coming out (the store's playing track, not the intent). */
  playing: boolean;
  position: number;
  duration: number;
  rate: number;
  /** Whether the queue can step either way. */
  next: boolean;
  previous: boolean;
  /** Run one press from the shell. `seconds` is a position only for a scrub
   *  (`seekto`); a skip carries the platform's own interval or nothing. */
  onAction: (action: MediaAction, seconds: number | null) => void;
}

/** Feed Windows' own media card and answer it. See the module docs. */
export function useWindowsMediaBridge(state: WindowsMediaBridge) {
  const { title, artist, album, artwork, playing, duration, rate, next, previous } = state;
  // The playhead is bucketed, so the effect runs on the buckets the shell is
  // told about rather than on every tick of the player's clock.
  const position = Math.floor(Math.max(0, state.position) / POSITION_STEP_S) * POSITION_STEP_S;

  // What the shell was last told: an unchanged payload is not an IPC message.
  // (The metadata and the state move together on a track change, so one row of
  // this cache covers the push's whole life.)
  const lastSent = useRef("");
  useEffect(() => {
    if (!IN_TAURI) return;
    const payload = {
      title: title ?? null,
      artist: artist ?? null,
      album: album ?? null,
      artwork: artwork ?? null,
      playing,
      position,
      duration,
      rate,
      next,
      previous,
    };
    const shape = JSON.stringify(payload);
    if (shape === lastSent.current) return;
    lastSent.current = shape;
    // The push goes in the report, like every other bridge's: a card that
    // shows the wrong track is then a row about what the app TOLD the shell
    // rather than a guess about whether it ever spoke.
    note("winMedia", { title: payload.title, playing, duration, art: payload.artwork != null });
    void (async () => {
      try {
        // Dynamic on purpose, exactly like the iOS bridges: a static import
        // would put a Tauri-only module in the browser bundle too.
        const { invoke } = await import("@tauri-apps/api/core");
        await invoke(SET_NOW_PLAYING, { state: payload });
      } catch {
        /* No such command (an older shell, or one whose target has no card):
         * silence. Nothing about the OS's media card is worth an exception in
         * the player's render path. */
      }
    })();
  }, [title, artist, album, artwork, playing, position, duration, rate, next, previous]);

  // The inbound half: a press on the shell's card runs the page's own Media
  // Session handlers. Kept in a ref so the listener is registered once and
  // still calls the CURRENT handlers (they close over the current track) —
  // and written in an effect, never during render, because a render React
  // discards would otherwise leave the ref holding handlers for a track that
  // never came on screen (oxlint's "cannot access refs during render" is that
  // rule; lib/iosFavs.ts's star carries the same one).
  const onAction = useRef(state.onAction);
  useEffect(() => {
    onAction.current = state.onAction;
  }, [state.onAction]);
  useEffect(() => {
    if (!IN_TAURI) return;
    let unlisten: (() => void) | undefined;
    let cancelled = false;
    void (async () => {
      try {
        const { listen } = await import("@tauri-apps/api/event");
        const stop = await listen<{ action: MediaAction; seconds: number | null }>(
          MEDIA_KEY_EVENT,
          (event) => {
            const action = event.payload?.action;
            if (!action) return;
            note("winMedia", { press: action, seconds: event.payload?.seconds ?? null });
            onAction.current(action, event.payload?.seconds ?? null);
          }
        );
        if (cancelled) stop();
        else unlisten = stop;
      } catch {
        /* No such event (an older shell, or a target that never sends one):
         * the card simply does not drive the player. Nothing else changes. */
      }
    })();
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, []);
}