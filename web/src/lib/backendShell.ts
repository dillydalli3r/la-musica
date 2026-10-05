import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { IN_TAURI, setServerUrl } from "../api";
import { markClientSetupDone } from "./clientSetup";

/** The module's single `mlo-backend` registration, and its undo. Created on
 *  the first `attachBackendShell()` and shared by everyone after that: the
 *  effect that serves the chooser page and the app's own boot both want this
 *  listener, and a second `listen()` would handle every state twice. */
let detachListen: (() => void) | null = null;
let unlistenBackend: Promise<UnlistenFn> | null = null;

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
  /** The shell's LIVE backend state, answered by the same one-shot ask:
   *  "running" | "starting" | "stopped" for a local backend, "remote" when the
   *  backend is not the shell's to describe. This is here because an event is
   *  not enough — entering local mode RELOADS the page, so the document that
   *  needs the state has missed every event the shell emitted before it. */
  status: "running" | "starting" | "stopped" | "remote";
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
    // Fold the answer into the live flags the screens read, so a page that
    // arrived too late for the events still knows what is happening.
    if (choice.mode === "local") {
      setBackendStarting(choice.status === "starting");
      setBackendFailed(choice.status === "stopped");
    }
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

/** True once the shell has told us the local backend it was asked for is NOT
 *  running (`status: "stopped"` with no origin, emitted by
 *  `enter_backend_mode`'s failure path on a cold start, or later by the health
 *  poll when a running backend dies). The chooser renders that as a sentence
 *  and two ways out instead of an endless spinner. */
let backendFailed = false;
const failureWatchers = new Set<() => void>();

export function backendUnavailable(): boolean {
  return backendFailed;
}

/** Watch for that failure (subscribe). Returns the unsubscribe function. */
export function subscribeBackendFailure(fn: () => void): () => void {
  failureWatchers.add(fn);
  return () => {
    failureWatchers.delete(fn);
  };
}

function setBackendFailed(next: boolean) {
  if (next === backendFailed) return;
  backendFailed = next;
  for (const fn of failureWatchers) fn();
}

/** True while the shell is bringing up a LOCAL backend it was asked for
 *  (`status: "starting"`). The chooser shows its spinner for that window, and
 *  App keeps that screen up — without it, the page falls through to the
 *  address wizard for as long as the backend takes to answer, which is the
 *  wrong question in local mode and, on a cold start, several seconds of it. */
let backendStarting = false;
const startingWatchers = new Set<() => void>();

export function backendStartingUp(): boolean {
  return backendStarting;
}

export function subscribeBackendStarting(fn: () => void): () => void {
  startingWatchers.add(fn);
  return () => {
    startingWatchers.delete(fn);
  };
}

function setBackendStarting(next: boolean) {
  if (next === backendStarting) return;
  backendStarting = next;
  for (const fn of startingWatchers) fn();
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
  if (detachListen) return detachListen;
  unlistenBackend = listen<BackendState>("mlo-backend", (event) => {
    const state = event.payload;
    // An unset shell is asking the page to show the local-vs-remote chooser;
    // remember it so a listener mounted around this event can render it
    // without polling. `shellBackendChoice()` remains the one-shot ask App
    // makes before its first render.
    if (state.needs_choice) setNeedsChoice(true);
    if (state.local && state.status === "starting") setBackendStarting(true);
    if (state.local && state.status === "running" && state.origin) {
      // The shell's own backend answered. Its origin IS the API origin (the
      // SPA is served from there), which is also what makes the session
      // cookie travel: nothing to probe, nothing to ask the user.
      setServerUrl(state.origin);
      markClientSetupDone();
      setBackendFailed(false);
      setBackendStarting(false);
    }
    // "stopped" with no origin = the shell was asked for a local backend and
    // has none. On the first-run screen that is the whole story of a failed
    // start; afterwards it means the server died.
    if (state.local && state.status === "stopped" && !state.origin) {
      setBackendStarting(false);
      setBackendFailed(true);
    }
  });
  // One registration for the module, whoever asks first (main.tsx and the
  // chooser both do). A second `listen()` would double-handle every event and
  // leak the first registration on unmount.
  detachListen = () => {
    const pending = unlistenBackend;
    detachListen = null;
    unlistenBackend = null;
    void pending?.then((off) => off());
  };
  return detachListen;
}
