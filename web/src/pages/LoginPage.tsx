import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { KeyRound, Loader2, Lock, Server, ShieldCheck } from "lucide-react";
import { api, normalizeServerUrl, setServerUrl, setToken, serverUrl, IN_MOBILE_SHELL, IN_TAURI } from "../api";
import { toast } from "../store";
import { useI18n } from "../lib/i18n";

/** The sign-in screen, and the first-run screen behind it.
 *
 * Three different situations reach this page and they must not look alike:
 *
 *  * **login** — the server demands a password and we are not signed in;
 *  * **setup** — nobody has claimed this server yet, so the useful action is
 *    choosing a password, not guessing one (the API answers 428 for every
 *    other call until it exists);
 *  * **address** — a client shell (desktop/iOS/Android) with no server
 *    picked yet, where the first question is *where* the server is. The
 *    desktop shell finds its own on 127.0.0.1; a phone has to be told.
 *
 * The screen therefore renders whichever of those the server's own
 * `/api/auth/status` describes, rather than assuming "sign in".
 */
export default function LoginPage({ onSignedIn }: { onSignedIn: () => void }) {
  const { t } = useI18n();
  const status = useQuery({
    queryKey: ["auth", "status"],
    queryFn: api.authStatus,
    retry: 1,
    // The address field changes what this query points at, and the user may
    // have typed it a moment ago — so a failed probe is a hint ("no server
    // answered"), not a dead end.
    refetchOnWindowFocus: false,
    // While NOTHING has answered, keep probing — and stop the moment something
    // does. The shell's gate is monotone (once it is up, only a sign-in takes
    // it down), so a backend that is still booting, a Wi-Fi association that
    // has not come up yet, or an address the user just typed can only be
    // noticed here: with a single probe per mount the screen sat on "no answer"
    // until the user pressed a button or reloaded the app. 3s is slower than a
    // human can type, and a reachable server is asked exactly once.
    refetchInterval: (q) => (q.state.status === "error" ? 3000 : false),
  });
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [username, setUsername] = useState("");
  const [address, setAddress] = useState(serverUrl());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const needsSetup = !!status.data && !status.data.has_password;
  const hint = status.data?.setup_hint || "";

  useEffect(() => {
    if (status.data?.username) setUsername((u) => u || status.data!.username);
  }, [status.data]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const session = needsSetup
        ? await api.authSetup(password, confirm, username)
        : await api.authLogin(password, username.trim() || undefined);
      setToken(session.token);
      setPassword("");
      setConfirm("");
      toast.success(needsSetup ? t("auth.setup_done") : t("auth.signed_in"));
      onSignedIn();
    } catch (err) {
      // The server's own words: "wrong password", "password must be at least
      // 8 characters", "too many attempts — try again in 30s".
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const applyAddress = () => {
    const next = normalizeServerUrl(address);
    setAddress(next); // what was typed, as it will be saved (`example.com:8000` → `http://example.com:8000`)
    setServerUrl(next);
    setError("");
    status.refetch();
  };

  return (
    <div className="safe-shell min-h-dvh bg-bg text-zinc-100 flex items-center justify-center">
      <div className="w-full max-w-md p-6">
        <div className="flex items-center gap-3 mb-6">
          <div className="h-11 w-11 rounded-xl bg-accent/15 border border-accent/30 flex items-center justify-center">
            {needsSetup ? (
              <ShieldCheck className="h-5 w-5 text-accent-soft" />
            ) : (
              <Lock className="h-5 w-5 text-accent-soft" />
            )}
          </div>
          <div>
            <h1 className="text-lg font-semibold">la musica</h1>
            <p className="text-xs text-zinc-500">
              {needsSetup
                ? t("auth.setup_intro")
                : `${t("auth.sign_in")} — ${status.data?.public_url || serverUrl() || t("auth.server")}`}
            </p>
          </div>
        </div>

        <form onSubmit={submit} className="panel space-y-4">
          {(IN_TAURI || !status.data) && (
            <label className="block">
              <span className="text-xs text-zinc-400 flex items-center gap-1.5 mb-1.5">
                <Server className="h-3.5 w-3.5" /> {t("auth.server_address")}
              </span>
              <div className="flex gap-2">
                {/* `min-w-0` floors the shrink at zero: the flex default
                    (`min-width: auto`) lets Safari hold a text input at its
                    intrinsic `size` width, which would push "Use" past the
                    card's edge on a phone. Chromium already shrinks it. */}
                <input
                  className="input font-mono text-xs min-w-0"
                  value={address}
                  onChange={(e) => setAddress(e.target.value)}
                  placeholder="http://127.0.0.1:8000"
                  spellCheck={false}
                  autoComplete="off"
                />
                <button type="button" className="btn-ghost !py-1.5 text-xs shrink-0" onClick={applyAddress}>
                  {t("auth.use_address")}
                </button>
              </div>
              {normalizeServerUrl(address) && normalizeServerUrl(address) !== address.trim() && (
                <span className="text-[11px] text-zinc-500 font-mono block mt-1">
                  → {normalizeServerUrl(address)}
                </span>
              )}
              <span className="text-[11px] text-zinc-600 block mt-1">
                {t("auth.server_address_help")}
              </span>
            </label>
          )}

          {status.isError && (
            <p className="text-xs text-amber-300">
              {t("auth.no_answer")}
            </p>
          )}

          {needsSetup && (
            <p className="text-[11px] text-zinc-500 leading-relaxed">{t("auth.setup_hint")}</p>
          )}
          {/* Asked in both situations: claiming the server names the user, and
              signing in needs the name to pick between users when a server has
              more than one. It is prefilled from `/api/auth/status`, and left
              empty the server answers with the only user it has. */}
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
              // Not on a phone: this screen's FIRST field is the server address
              // (a mobile client has to be told where its server is), and an
              // auto-opened keyboard on the password box scrolls that field out
              // of the viewport before the user has seen it. iOS does the
              // scrolling itself, when the user taps the field they mean.
              autoFocus={!IN_MOBILE_SHELL}
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

          {error && (
            <p role="alert" className="text-xs text-red-300 bg-red-950/40 border border-red-900/60 rounded-md px-3 py-2">
              {error}
            </p>
          )}
          {hint && <p className="text-[11px] text-amber-300/90">{hint}</p>}

          <button type="submit" className="btn-primary w-full" disabled={busy || !password}>
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Lock className="h-4 w-4" />}
            {needsSetup ? t("auth.set_password_sign_in") : t("auth.sign_in")}
          </button>

          <p className="text-[11px] text-zinc-600 leading-relaxed">{t("auth.gate_help")}</p>
        </form>
      </div>
    </div>
  );
}
