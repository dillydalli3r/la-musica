import { invoke } from "@tauri-apps/api/core";
import { CAPABILITY_KEYS, HOST_DEVICE_URL, IN_TAURI, normalizeServerUrl, serverUrl } from "../api";
import type { Capabilities } from "../api";
import { t, type MessageKey } from "./i18n";

/** Where this client records that its own setup wizard has been through.
 *
 *  Deliberately NOT the server's `first_run_done`: that flag describes the
 *  music library being configured and is the same answer for every client.
 *  Which server a phone talks to — and whether that phone has been asked —
 *  is a property of this device alone, so a client pointed at a second
 *  server is asked again. */
export const CLIENT_SETUP_KEY = "mlo.clientSetup";

function readStore(key: string): string {
  try {
    return localStorage.getItem(key) || "";
  } catch {
    return ""; // private mode / storage disabled
  }
}

function writeStore(key: string, value: string | null) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* nothing to do: the wizard simply asks again on the next launch */
  }
}

/** True inside the Tauri shells (desktop, iOS, Android) — the builds that
 *  bundle this SPA and must be told where a backend runs. The web app and
 *  Docker are served BY their backend and never need this wizard. */
export function isClientShell(): boolean {
  return IN_TAURI;
}

/** The address "Host on this device" means for THIS client.
 *
 *  In a shell that is the backend the shell serves itself — on the desktop the
 *  Tauri shell spawns it (desktop/src-tauri/src/lib.rs), never the origin,
 *  because `tauri://localhost` is not a backend. On the web app it is the
 *  origin that served the page, which is exactly what the empty base already
 *  means to the API module.
 *
 *  A phone is the awkward case: it bundles no CPython, so nothing answers
 *  unless an on-device Python (a-Shell, iSH, Termux) runs the backend — which
 *  is why the wizard probes this address and says so plainly when nothing
 *  does, instead of saving it and leaving every page failing to load. */
export function hostOnDeviceUrl(): string {
  return isClientShell() ? HOST_DEVICE_URL : "";
}

/** True while this client already points at the backend on this device. */
export function isHostingOnThisDevice(): boolean {
  return normalizeServerUrl(serverUrl()) === normalizeServerUrl(hostOnDeviceUrl());
}

export function isClientSetupDone(): boolean {
  return readStore(CLIENT_SETUP_KEY) === "1";
}

export function markClientSetupDone() {
  writeStore(CLIENT_SETUP_KEY, "1");
}

/** Forget "this device is set up", so the wizard opens again on next launch. */
export function resetClientSetup() {
  writeStore(CLIENT_SETUP_KEY, null);
}

/** The wizard's steps, in order. `STEP_LABELS` keeps the rail and the step
 *  headings reading the same translated string. */
export const STEP_IDS = ["server", "account", "notifications", "done"] as const;
export type StepId = (typeof STEP_IDS)[number];

export const STEP_LABELS: Record<StepId, MessageKey> = {
  server: "client.step_server",
  account: "client.step_account",
  notifications: "client.step_notifications",
  done: "client.step_done",
};

export interface ProbeResult {
  ok: boolean;
  version?: string;
  requiresLogin?: boolean;
  hasPassword?: boolean;
  error?: string;
}

/** The two probes below are a person waiting on a "Test" button, not a page
 *  load — three seconds is long enough for a LAN address to answer and short
 *  enough that a wrong one is a reply, not a hang. */
const PROBE_TIMEOUT_MS = 3000;

/** One JSON GET against a bare address that NEVER throws: every failure comes
 *  back as a message the wizard can show verbatim. */
async function getJson(
  base: string,
  path: string,
): Promise<{ ok: boolean; body: Record<string, unknown> | null; error?: string }> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), PROBE_TIMEOUT_MS);
  try {
    const r = await fetch(`${base}${path}`, {
      credentials: "include",
      signal: ctrl.signal,
      headers: { Accept: "application/json" },
    });
    let body: Record<string, unknown> | null = null;
    try {
      body = (await r.json()) as Record<string, unknown>;
    } catch {
      /* not JSON — the caller decides what that means */
    }
    if (!r.ok) return { ok: false, body, error: String(body?.detail || body?.error || `HTTP ${r.status}`) };
    return { ok: true, body };
  } catch (e) {
    // An aborted fetch is OUR deadline, not the network being down — say which.
    if (ctrl.signal.aborted) return { ok: false, body: null, error: t("client.timeout") };
    return { ok: false, body: null, error: e instanceof Error ? e.message : String(e) };
  } finally {
    clearTimeout(timer);
  }
}

/** Ask an address whether a la musica backend answers there, and what that
 *  backend says about its own login gate.
 *
 *  A 2xx whose body carries no `version` counts as a failure: a captive
 *  portal, a router's admin page and a plain web server all answer 200, and
 *  "something answered" must not read as "your server is there". The login
 *  facts come from the second call (`/api/auth/status` is public), and are
 *  left undefined when it does not answer — the wizard then offers sign-in,
 *  the safe reading. Never throws. */
export async function probeServer(url: string): Promise<ProbeResult> {
  const base = (url || "").trim().replace(/\/+$/, "");
  // "" = the origin that served this page (the web app; a Tauri shell can
  // never reach one this way, and gets a network error it can show).
  const origin = base || window.location.origin;

  const health = await getJson(origin, "/api/health");
  const reported = health.body?.version;
  const version = typeof reported === "string" ? reported : "";
  if (!health.ok) return { ok: false, error: health.error || "" };
  if (!version) return { ok: false, error: t("client.bad_reply") };

  const status = await getJson(origin, "/api/auth/status");
  const s = status.ok ? status.body : null;
  return {
    ok: true,
    version,
    requiresLogin: typeof s?.required === "boolean" ? s.required : undefined,
    hasPassword: typeof s?.has_password === "boolean" ? s.has_password : undefined,
  };
}

/** What this device's own address answers, and what it says it can do. */
export interface LocalBackend {
  /** True when a la musica backend answers on this device RIGHT NOW. */
  possible: boolean;
  /** That backend's capability report, or null when it did not answer the
   *  capability call — an older build without the route, or a gated server
   *  that wants a session first. `possible` is true in both cases. */
  capabilities: Capabilities | null;
  /** Why nothing answered, when nothing did. */
  error?: string;
}

/** The mobile shell's own answer to the same question
 *  (`desktop/src-tauri/src/mobile_backend.rs`, the `backend_info` command):
 *  whether it hosts a backend, where, and — when it does not — why not, in its
 *  own words. Only the fields this file reads are declared. */
interface BackendInfo {
  /** hosting | starting | unavailable | busy */
  state: string;
  url: string;
  reason: string;
}

/** Ask this device's own address what it is. Never throws.
 *
 *  The client wizard cannot decide whether offering "Host on this device" is
 *  honest by looking at the platform: the desktop shell starts a backend
 *  before its window shows, a phone build may run one inside the app, and a
 *  build that ships no local backend answers nothing at all — three different
 *  situations with one question, so this asks it. The capability call is a
 *  second, optional question about what such a backend could do; it is read
 *  directly only when the answer comes from THIS device (a gated server
 *  replies 401 and is left to the Dependencies page, which has a session). */
export async function probeLocalBackend(): Promise<LocalBackend> {
  if (!isClientShell()) return { possible: false, capabilities: null, error: "" };

  // The shell knows this better than a fetch can: it started (or is starting,
  // or failed to start) the backend itself and holds the reason why
  // (desktop/src-tauri/src/mobile_backend.rs). A build whose capability list
  // does not permit the command rejects, and the HTTP probe below answers
  // instead — the same questions, asked of whoever can answer them.
  try {
    const info = await invoke<BackendInfo>("backend_info");
    if (info?.state === "starting") {
      // Coming up: the honest answer is "yes, not yet", never "impossible".
      return { possible: true, capabilities: null };
    }
    if (info?.state !== "hosting" || !info.url) {
      return { possible: false, capabilities: null, error: info?.reason || "" };
    }
    return { possible: true, capabilities: await fetchCapabilities(info.url) };
  } catch {
    /* no such command in this build — the HTTP probe below is the fallback */
  }

  const health = await getJson(HOST_DEVICE_URL, "/api/health");
  const version = health.body?.version;
  if (!health.ok || typeof version !== "string") {
    return { possible: false, capabilities: null, error: health.error || "" };
  }
  return { possible: true, capabilities: await fetchCapabilities(HOST_DEVICE_URL) };
}

/** A backend's capability report, or null when it did not answer the call —
 *  an older build without the route, or a gated server that wants a session
 *  first. Never throws. */
async function fetchCapabilities(base: string): Promise<Capabilities | null> {
  const caps = await getJson(base, "/api/capabilities");
  const body = caps.ok ? caps.body : null;
  return body && typeof body.platform === "string" ? (body as unknown as Capabilities) : null;
}

/** One honest line about a backend that runs ON THIS DEVICE: what works and
 *  what does not, read from its own capability report rather than assumed
 *  from the platform. Null while the report is unavailable, so a caller shows
 *  nothing instead of a claim it cannot back. */
export function localBackendSummary(caps: Capabilities | null): string | null {
  if (!caps) return null;
  const total = CAPABILITY_KEYS.length;
  const missing = CAPABILITY_KEYS.filter((k) => !caps[k].available).length;
  if (!missing) return t("client.host_local_all");
  return t("client.host_local_partial", { missing, total });
}

/** Device preference: keep the local backend hosting while the app is not in
 *  the foreground — the "Soulseek sharing with the phone locked" case.
 *
 *  Stored per device (localStorage, like this client's server address) because
 *  it is a property of THIS phone's shell and never of a server: a client
 *  pointed at a LAN server has no backend of its own to keep alive.
 *
 *  The stored key is this UI's own memory of the switch — the shell keeps its
 *  own copy, because it has to act on the choice at launch and across a
 *  resume, with no webview in sight (it persists what set_backend_keepalive
 *  tells it). One writer, so the two cannot disagree. */
export const BACKGROUND_HOSTING_KEY = "mlo.hostBackground";

export function isBackgroundHosting(): boolean {
  return readStore(BACKGROUND_HOSTING_KEY) === "1";
}

/** Record the choice and tell the shell about it.
 *
 *  The shell is what keeps the process alive while the screen is off, so this
 *  is not a switch that only paints itself: it hands the decision over
 *  (mobile shell only — a build without the command, or the desktop app,
 *  rejects and the stored choice simply stands). Never throws. */
export async function setBackgroundHosting(on: boolean): Promise<void> {
  writeStore(BACKGROUND_HOSTING_KEY, on ? "1" : null);
  try {
    await invoke("set_backend_keepalive", { keep: on });
  } catch {
    /* no such command in this build: the choice is recorded and nothing here
       depends on the shell acknowledging it */
  }
}

/** How this device can keep a backend hosted in the background. */
export interface BackgroundHosting {
  /** True when the switch is offered; false carries the reason instead. */
  available: boolean;
  /** Which platform's cost this is — it picks the wording. */
  kind: "ios" | "android" | "desktop";
  /** One line: what staying up costs here, or why there is nothing to switch. */
  help: string;
}

/** The background-hosting answer for this device, or null when the question
 *  does not apply (the browser build, or no capability report yet).
 *
 *  The platform comes from the backend's own report rather than the user
 *  agent: it is the process that will be kept alive, so it is the one that
 *  knows what keeping it alive means. iOS and Android pay for it in different
 *  currency and a desktop shell pays nothing, which is why this is not one
 *  switch with one explanation. */
export function backgroundHosting(caps: Capabilities | null): BackgroundHosting | null {
  if (!caps || !isClientShell()) return null;
  if (caps.platform === "ios") {
    return { available: true, kind: "ios", help: t("client.bg_ios") };
  }
  if (caps.platform === "android") {
    return { available: true, kind: "android", help: t("client.bg_android") };
  }
  // A desktop shell supervises its own backend for as long as it runs: the
  // problem being solved here (the OS suspending the app that hosts it) does
  // not exist there, so this says so instead of offering a switch.
  return { available: false, kind: "desktop", help: t("client.bg_desktop") };
}

/** Point this client at a server that just answered (the wizard's "Next"
 *  after a successful probe). The API module owns the storage key, the
 *  normalising and the request base, so nothing here duplicates any of it. */
export { setServerUrl as saveServer } from "../api";
