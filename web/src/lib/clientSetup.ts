import { IN_TAURI } from "../api";
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

/** Point this client at a server that just answered (the wizard's "Next"
 *  after a successful probe). The API module owns the storage key, the
 *  normalising and the request base, so nothing here duplicates any of it. */
export { setServerUrl as saveServer } from "../api";
