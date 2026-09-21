import { useState } from "react";
import { ArrowLeft, ArrowRight, Bell, Check, KeyRound, Loader2, Server } from "lucide-react";
import { api, normalizeServerUrl, serverUrl, setToken } from "../api";
import PageHeader from "../components/PageHeader";
import ServerVersionNotice from "../components/ServerVersionNotice";
import { toast } from "../store";
import { useI18n } from "../lib/i18n";
import { notificationState, requestNotifications, type NotifyState } from "../lib/notify";
import {
  STEP_IDS,
  STEP_LABELS,
  markClientSetupDone,
  probeServer,
  saveServer,
  type ProbeResult,
  type StepId,
} from "../lib/clientSetup";

/** The first-run wizard for the client shells (desktop, iOS, Android).
 *
 *  Those builds bundle this SPA and open it from `tauri://localhost`, so
 *  there is no same-origin backend to fall back on: the device has to be told
 *  where one runs before anything else can work. The web app and Docker are
 *  served BY their backend, so they never need this page.
 *
 *  Nothing here is one-shot — the address can be corrected and the wizard
 *  re-run at any time; `markClientSetupDone()` only records that this device
 *  has been through it once. */
export default function ClientSetup({ onDone }: { onDone: () => void }) {
  const { t } = useI18n();
  const [step, setStep] = useState<StepId>("server");
  const index = STEP_IDS.indexOf(step);

  const [address, setAddress] = useState(serverUrl());
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const [testing, setTesting] = useState(false);

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [account, setAccount] = useState("");

  const [notify, setNotify] = useState<NotifyState>(() => notificationState());

  // Only a false `has_password` means "claim this server"; an unknown one
  // (the status call failed) falls back to sign-in, which is never wrong.
  const needsSetup = probe?.hasPassword === false;

  const test = async () => {
    // Normalised first so the field shows the address that is about to be
    // saved (`example.com:8000` → `http://…`).
    const target = normalizeServerUrl(address);
    setAddress(target);
    setTesting(true);
    setProbe(null);
    const r = await probeServer(target);
    setProbe(r);
    setTesting(false);
    // Every later call (auth, library, the event socket) goes through the API
    // module's base, so the address that just answered becomes this client's.
    if (r.ok) saveServer(target);
  };

  const submitAccount = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const session = needsSetup
        ? await api.authSetup(password, confirm, username.trim() || undefined)
        : await api.authLogin(password, username.trim() || undefined);
      setToken(session.token);
      setAccount(session.username || username.trim());
      setPassword("");
      setConfirm("");
      toast.success(t(needsSetup ? "auth.setup_done" : "auth.signed_in"));
      setStep("notifications");
    } catch (err) {
      // The server's own words: "wrong password", "at least 8 characters",
      // "too many attempts".
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const askNotifications = async () => {
    const next = await requestNotifications();
    setNotify(next);
    if (next === "granted") toast.success(t("notify.enabled"));
    else if (next === "denied") toast.error(t("notify.blocked_help"));
  };

  return (
    <div className="safe-shell min-h-dvh bg-bg text-zinc-100 flex flex-col items-center justify-center">
      <div className="w-full max-w-2xl space-y-4 p-6">
        <PageHeader icon={Server} title={t("client.title")} subtitle={t("client.subtitle")} />

        {/* The rail is the house idiom (see pages/SetupPage.tsx): the steps
            behind us are ticked, the current one is the accent chip. */}
        <div className="flex flex-wrap items-center gap-2 text-[11px] text-zinc-500">
          {STEP_IDS.map((id, i) => (
            <div key={id} className="flex items-center gap-2">
              <span
                className={`h-5 w-5 rounded-sm flex items-center justify-center text-[10px] border ${
                  id === step
                    ? "bg-accent text-[var(--accent-fg)] border-accent"
                    : i < index
                    ? "bg-emerald-900/60 text-emerald-300 border-emerald-800"
                    : "bg-panel border-border text-zinc-500"
                }`}
              >
                {i < index ? <Check className="h-3 w-3" /> : i + 1}
              </span>
              <span className={id === step ? "text-zinc-200" : "text-zinc-600"}>{t(STEP_LABELS[id])}</span>
            </div>
          ))}
        </div>

        <div className="panel p-6 space-y-4">
          {step === "server" && (
            <>
              {/* One question, one answer: which server this device talks to.
                  A shell hosts no backend of its own, so an address that does
                  not answer leaves nothing to fall back on — the step is
                  blocked until one does, and says which error came back
                  rather than offering a mode that cannot work.

                  A build with no local backend is therefore not a case the
                  wizard has to ask about: the honest answer is the address. */}
              <label className="block">
                <span className="text-xs text-zinc-400 flex items-center gap-1.5 mb-1.5">
                  <Server className="h-3.5 w-3.5" /> {t("auth.server_address")}
                </span>
                <div className="flex gap-2">
                  {/* Deliberately no autoFocus: this is the first screen a phone
                      sees, and the keyboard would cover the help text and the
                      Test button before the address has even been read. */}
                  {/* `min-w-0` is what keeps this row inside the card on
                      every engine: `min-width: auto` (the flex default)
                      floors a text input at its own intrinsic width, and
                      Safari reads that floor off the `size` attribute — the
                      field would refuse to shrink and push "Test connection"
                      past the card's edge on a phone. Chromium already
                      shrinks it, so nothing moves on the desktop. */}
                  <input
                    className="input font-mono text-xs min-w-0"
                    value={address}
                    onChange={(e) => {
                      setAddress(e.target.value);
                      setProbe(null); // a changed address makes the old answer meaningless
                    }}
                    placeholder="http://127.0.0.1:8000"
                    spellCheck={false}
                    autoComplete="off"
                  />
                  <button
                    type="button"
                    className="btn-ghost !py-1.5 text-xs shrink-0"
                    onClick={test}
                    disabled={testing || !address.trim()}
                  >
                    {testing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Server className="h-3.5 w-3.5" />}
                    {t("client.test")}
                  </button>
                </div>
                {/* What is about to be saved, as it will be saved: a typed
                    `example.com:8000` becomes `http://example.com:8000`, and
                    the user sees that before the probe, not after. */}
                {normalizeServerUrl(address) && normalizeServerUrl(address) !== address.trim() && (
                  <span className="text-[11px] text-zinc-500 font-mono block mt-1">
                    → {normalizeServerUrl(address)}
                  </span>
                )}
                <span className="text-[11px] text-zinc-600 block mt-1">{t("auth.server_address_help")}</span>
              </label>

              {probe &&
                (probe.ok ? (
                  <p className="text-xs text-emerald-300">{t("client.test_ok", { version: probe.version || "" })}</p>
                ) : (
                  <p className="text-xs text-amber-300 break-words">
                    {t("client.test_fail", { error: probe.error || "" })}
                  </p>
                ))}

              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-[11px] text-zinc-600">{probe?.ok ? "" : t("client.need_test")}</span>
                <button className="btn-primary ml-auto" onClick={() => setStep("account")} disabled={!probe?.ok}>
                  {t("client.next")} <ArrowRight className="h-3.5 w-3.5" />
                </button>
              </div>
            </>
          )}

          {step === "account" && (
            <form onSubmit={submitAccount} className="space-y-4">
              <p className="text-xs text-zinc-400 leading-relaxed">
                {needsSetup
                  ? t("client.account_setup")
                  : t("client.account_signin", { server: serverUrl() || window.location.origin })}
              </p>

              {/* The name is asked in both cases: claiming the server sets it,
                  and signing in needs it to pick between users on a server
                  that has more than one (a server with a single user answers
                  either way). */}
              <label className="block">
                <span className="text-xs text-zinc-400 mb-1.5 block">
                  {t(needsSetup ? "auth.name_optional" : "auth.username")}
                </span>
                <input
                  className="input text-sm"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder={t("auth.name_placeholder")}
                  autoComplete="username"
                />
              </label>

              <label className="block">
                <span className="text-xs text-zinc-400 flex items-center gap-1.5 mb-1.5">
                  <KeyRound className="h-3.5 w-3.5" /> {t("auth.password")}
                </span>
                <input
                  className="input text-sm"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete={needsSetup ? "new-password" : "current-password"}
                  autoFocus
                />
              </label>

              {needsSetup && (
                <label className="block">
                  <span className="text-xs text-zinc-400 mb-1.5 block">{t("auth.repeat_password")}</span>
                  <input
                    className="input text-sm"
                    type="password"
                    value={confirm}
                    onChange={(e) => setConfirm(e.target.value)}
                    autoComplete="new-password"
                  />
                </label>
              )}

              {error && <p className="text-xs text-amber-300 break-words">{error}</p>}

              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex flex-wrap gap-2">
                  <button type="button" className="btn-ghost" onClick={() => setStep("server")} disabled={busy}>
                    <ArrowLeft className="h-3.5 w-3.5" /> {t("client.back")}
                  </button>
                  <button type="button" className="btn-ghost" onClick={() => setStep("notifications")} disabled={busy}>
                    {t("client.skip")}
                  </button>
                </div>
                <button className="btn-primary ml-auto" disabled={busy || !password}>
                  {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <KeyRound className="h-3.5 w-3.5" />}
                  {t(needsSetup ? "auth.set_password_sign_in" : "auth.sign_in")}
                </button>
              </div>
            </form>
          )}

          {step === "notifications" && (
            <>
              <div className="flex items-center gap-2">
                <Bell className="h-4 w-4 text-accent" />
                <span className="text-sm font-semibold">{t("client.step_notifications")}</span>
              </div>
              <p className="text-xs text-zinc-400 leading-relaxed">{t("client.notify_text")}</p>
              <p className="text-[11px] text-zinc-500">
                {notify === "granted"
                  ? t("notify.enabled")
                  : notify === "denied"
                  ? t("notify.blocked_help")
                  : ""}
              </p>
              {/* Two groups that cannot share a phone's line: at 390px
                  "Enable notifications" is a 129px two-line button, and with
                  "Next" beside it the row was 421px inside a 294px card — the
                  primary action sat off-screen and the page scrolled sideways.
                  Wrapping keeps the wording and the 44px targets; `ml-auto` is
                  what right-aligns the right group on the line it wraps onto
                  (justify-between leaves a lone item at the left edge). The
                  same two attributes are on the footers of every other step. */}
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex flex-wrap gap-2">
                  <button type="button" className="btn-ghost" onClick={() => setStep("account")}>
                    <ArrowLeft className="h-3.5 w-3.5" /> {t("client.back")}
                  </button>
                  <button type="button" className="btn-ghost" onClick={() => setStep("done")}>
                    {t("client.skip")}
                  </button>
                </div>
                <div className="ml-auto flex flex-wrap justify-end gap-2">
                  <button
                    type="button"
                    className="btn-primary"
                    onClick={askNotifications}
                    disabled={notify === "granted" || notify === "unsupported"}
                  >
                    <Bell className="h-3.5 w-3.5" /> {t("notify.enable")}
                  </button>
                  <button type="button" className="btn-primary" onClick={() => setStep("done")}>
                    {t("client.next")} <ArrowRight className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
            </>
          )}

          {step === "done" && (
            <>
              <div className="rounded-md border border-border bg-bg/60 px-3 py-2 space-y-2">
                <div>
                  <div className="text-[10px] uppercase tracking-widest text-zinc-500">{t("client.step_server")}</div>
                  <div className="font-mono text-xs text-zinc-200 break-all mt-0.5">
                    {serverUrl() || window.location.origin}
                  </div>
                </div>
                <div>
                  <div className="text-[10px] uppercase tracking-widest text-zinc-500">{t("client.step_account")}</div>
                  <div className="text-xs text-zinc-200 mt-0.5">{account || "—"}</div>
                </div>
              </div>
              <p className="text-[11px] text-zinc-500 leading-relaxed">{t("client.done_note")}</p>
              {/* What that server actually runs — and, when it is behind, the
                  one line that says so. Same notice as Settings → Security. */}
              <ServerVersionNotice />
              <div className="flex flex-wrap items-center justify-between gap-2">
                <button type="button" className="btn-ghost" onClick={() => setStep("notifications")}>
                  <ArrowLeft className="h-3.5 w-3.5" /> {t("client.back")}
                </button>
                <button
                  className="btn-primary ml-auto"
                  onClick={() => {
                    markClientSetupDone();
                    onDone();
                  }}
                >
                  <Check className="h-3.5 w-3.5" /> {t("client.finish")}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
