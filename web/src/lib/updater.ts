import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { IN_TAURI } from "../api";
import { t } from "./i18n";
import { ingestDerived } from "./notifications";
import { toast } from "../store";

/** A newer release the shell found, with what the release manifest carried.
 *  `notes` is free text from the manifest and is drawn as text, never markup. */
export interface UpdateOffer {
  version: string;
  notes: string;
  date: string;
}

/** Everything the update surface draws from.
 *
 *  `current` is the SHELL's own version (not the server's — see
 *  `desktop/src-tauri/src/lib.rs`), and it is known even when there is nothing
 *  to install, which is what lets Settings say "you are on X" without a second
 *  question. `stage` is "idle" until an install is asked for, then the stage the
 *  shell reports, and `error` holds what the last check or install said went
 *  wrong — never thrown, because being offline is a normal way to be. */
export interface UpdateState {
  current: string;
  offer: UpdateOffer | null;
  checking: boolean;
  stage: "idle" | "downloading" | "installing";
  progress: { downloaded: number; total: number | null } | null;
  error: string;
}

let state: UpdateState = {
  current: "",
  offer: null,
  checking: false,
  stage: "idle",
  progress: null,
  error: "",
};
// The same hand-rolled watcher store as lib/backendShell.ts and lib/i18n.ts:
// this module is outside React's tree (main.tsx starts the launch check), so it
// cannot be a hook of its own.
const watchers = new Set<() => void>();

function setState(next: Partial<UpdateState>) {
  state = { ...state, ...next };
  for (const fn of watchers) fn();
}

/** Whether THIS client can install an update itself: the desktop shell, and
 *  nothing else. A browser is served by the server it talks to, so its update
 *  is that install's business (the Docker image updates itself). */
export function updateSupported(): boolean {
  return IN_TAURI;
}

/** The state the surface draws (`useSyncExternalStore` getter). */
export function updateState(): UpdateState {
  return state;
}

/** Watch it (subscribe). Returns the unsubscribe function React expects. */
export function subscribeUpdate(fn: () => void): () => void {
  watchers.add(fn);
  return () => {
    watchers.delete(fn);
  };
}

/** Ask the shell whether a newer release exists. Never throws: an offline box,
 *  a proxy or a GitHub hiccup lands in `error`, where the surface says so. */
export async function checkForUpdate(): Promise<UpdateOffer | null> {
  if (!updateSupported()) return null;
  setState({ checking: true, error: "" });
  try {
    const status = await invoke<{ current: string; offer: UpdateOffer | null }>("update_check");
    setState({ current: status.current, offer: status.offer, checking: false });
    return status.offer;
  } catch (e) {
    setState({ checking: false, error: e instanceof Error ? e.message : String(e) });
    return null;
  }
}

/** Download the offered release and install it. The shell takes it from there:
 *  on Windows this never returns — the shell exits so the installer can replace
 *  it, and the installer starts the new build — and everywhere else the shell
 *  re-execs itself the moment the swap is done. Either way the page's part ends
 *  at "installing", which is what the surface says while it waits. */
export async function installUpdate(): Promise<void> {
  if (!updateSupported() || !state.offer) return;
  setState({ stage: "downloading", progress: null, error: "" });
  const stop = await listen<{ stage: string; downloaded: number; total: number | null }>(
    "mlo-update",
    (event) => {
      const { stage, downloaded, total } = event.payload;
      setState({
        stage: stage === "installing" ? "installing" : "downloading",
        progress: stage === "installing" ? null : { downloaded, total },
      });
    },
  );
  try {
    await invoke("update_install");
  } catch (e) {
    stop();
    setState({
      stage: "idle",
      progress: null,
      error: e instanceof Error ? e.message : String(e),
    });
  }
}

/** The launch check, started once by `main.tsx`.
 *
 *  A newer release is announced where this app already announces things: an
 *  entry in the notification tray, and a toast the first time that VERSION
 *  appears. `ingestDerived` is idempotent by id and the id carries the version,
 *  so the tray keeps the announcement across launches while the toast does not
 *  repeat — which is the difference between "you are being told" and "you are
 *  being nagged". Nothing here runs outside the desktop shell, so no other
 *  client pays for it. */
let launched = false;

export function startUpdateWatch() {
  if (launched || !updateSupported()) return;
  launched = true;
  window.setTimeout(() => {
    void checkForUpdate().then((offer) => {
      if (!offer) return;
      const first = ingestDerived({
        id: `update:${offer.version}`,
        kind: "update",
        title: t("update.tray_title", { version: offer.version }),
        body: t("update.tray_body", { current: state.current }),
        link: "/settings",
      });
      if (first) toast.info(t("update.toast", { version: offer.version }));
    });
  }, 3000);
}