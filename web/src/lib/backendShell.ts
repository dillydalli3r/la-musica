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
  /** How this shell was told to find a server. "unset" is a shell that has
   *  never been through the first-run chooser (see `BackendChoice.tsx`). */
  mode: "local" | "remote" | "unset";
  /** True when the shell spawned the backend it is telling us about. */
  local: boolean;
  /** True while the shell still needs the user to pick local vs remote.
   *  Present (and true) only for an "unset" shell. */
  needs_choice?: boolean;
}

/** What the shell knows about how this install should reach a backend: the
 *  mode it is in (or "unset" before the user has chosen) and the music folder
 *  the built-in backend would use. */
export interface BackendChoice {
  mode: "local" | "remote" | "unset";
  music_folder: string;
}

/** Ask the shell how this install reaches a backend. Answers null when there
 *  is no Tauri shell at all (browser) or when the command is absent/rejects
 *  — a mobile shell, or a desktop shell older than this feature. That is the
 *  "this shell has no chooser" case, and it NEVER throws: the caller's own
 *  first-run wizard then runs exactly as it did before. */
export async function shellBackendChoice(): Promise<BackendChoice | null> {
  if (!IN_TAURI) return null;
  try {
    // Dynamic on purpose, like the app's other shell bridges: a static import
    // would put a Tauri-only module in the browser bundle, where nothing
    // answers it.
    const { invoke } = await import("@tauri-apps/api/core");
    const choice = await invoke<BackendChoice>("shell_backend_choice");
    if (!choice || typeof choice !== "object") return null;
    return choice;
  } catch {
    // No such command (an older/mobile shell), or any other refusal:
    // "no chooser here", not an error the page has to handle.
    return null;
  }
}

/** Record the user's pick with the shell. Answers false on any failure, so
 *  the first-run screen can stay on its buttons instead of navigating. */
export async function chooseBackend(mode: "local" | "remote"): Promise<boolean> {
  if (!IN_TAURI) return false;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("choose_backend", { mode });
    return true;
  } catch {
    return false;
  }
}

/** Whether the shell has said (so far) that this install still needs the
 *  local-vs-remote choice. Updated by `attachBackendShell` from the event;
 *  `shellBackendChoice()` is the authoritative one-shot ask. */
let needsChoice = false;
const choiceWatchers = new Set<() => void>();

/** The current value of the shell's `needs_choice` flag (getter). */
export function backendNeedsChoice(): boolean {
  return needsChoice;
}

/** Watch `needs_choice` as the shell reports it (subscribe). Returns the
 *  unsubscribe function, the shape React's effects expect. */
export function subscribeBackendChoice(fn: () => void): () => void {
  choiceWatchers.add(fn);
  return () => {
    choiceWatchers.delete(fn);
  };
}

function setNeedsChoice(next: boolean) {
  if (next === needsChoice) return;
  needsChoice = next;
  for (const fn of choiceWatchers) fn();
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
    // An unset shell is asking the page to show the local-vs-remote chooser;
    // remember it so a listener mounted around this event can render it
    // without polling. `shellBackendChoice()` remains the one-shot ask App
    // makes before its first render.
    if (state.needs_choice) setNeedsChoice(true);
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
