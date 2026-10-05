import { listen } from "@tauri-apps/api/event";
import { IN_TAURI, setServerUrl } from "../api";
import { markClientSetupDone } from "./clientSetup";

/** The event the desktop shell emits with the local backend's state
 *  (`desktop/src-tauri/src/backend_handle.rs`).
 *
 *  This is the web side of the local-backend story. The shell bundles and
 *  spawns the app's own backend (mlo-server), serves the SPA from the SAME
 *  origin as its API, and tells the page three things it cannot see itself:
 *  that the backend is the SHELL's own (not a remote server the user typed),
 *  what port it is on, and whether it is up.
 */
export interface BackendState {
  status: "starting" | "running" | "stopped";
  /** The loopback origin of the shell's own backend ("" in remote mode). */
  origin: string;
  port: number;
  /** "local" | "remote" — how this shell was told to find a server. */
  mode: "local" | "remote";
  /** True when the shell spawned the backend it is telling us about. */
  local: boolean;
}

/**
 * When the shell runs a LOCAL backend, the page is served from that same
 * origin — so the session cookie works and the app needs no "which server?"
 * wizard: the shell already knows the answer. This module listens for the
 * shell's event and, on the first "running" state for a local backend,
 * points the API base at it (a no-op when they already match) and marks the
 * client-shell setup done, so the first-run wizard is skipped.
 *
 * In REMOTE mode nothing changes: the shell serves the SPA's own build and
 * the page's usual wizard asks for the server address, exactly as before.
 */
export function attachBackendShell() {
  if (!IN_TAURI) return;
  void listen<BackendState>("mlo-backend", (event) => {
    const state = event.payload;
    if (state.local && state.status === "running" && state.origin) {
      // The shell's own backend answered. Its origin IS the API origin (the
      // SPA is served from there), which is also what makes the session
      // cookie travel: nothing to probe, nothing to ask the user.
      setServerUrl(state.origin);
      markClientSetupDone();
    }
  });
  // In remote mode (the classic shell) the page keeps the old behaviour:
  // the wizard runs once and the saved address is used. We do not touch
  // anything here — the absence of the event IS the remote-mode signal.
}
