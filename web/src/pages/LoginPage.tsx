import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Loader2, Lock, Server, ShieldCheck } from "lucide-react";
import { api, normalizeServerUrl, setServerUrl, setToken, serverUrl, IN_MOBILE_SHELL, IN_TAURI } from "../api";
import { toast } from "../store";
import { useI18n } from "../lib/i18n";
import PageHeader from "../components/PageHeader";
import ShellSplash from "../shell/ShellSplash";

/** How long a client shell may show the splash before the address field
 *  appears instead: long enough for a cold backend to boot (see
 *  BackendChoice's own "few seconds"), short enough that a server that never
 *  answers leaves the user something they can act on. */
const SPLASH_MAX_MS = 8000;

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
 * `/api/auth/status` describes, rather than assuming "sign in". A fourth
 * window — mounted while nothing has answered yet — is the splash, not a form:
 * see `splash` below.
 */
export default function LoginPage({
  onSignedIn,
  now = 0,
}: {
  onSignedIn: () => void;
  /** The shell's "the user asked for the address field" clock: non-zero once
   *  the shell has shown the same splash for its own bound, so the page starts
   *  past ITS splash and the address field is the first thing drawn. Only the
   *  SPLASH reads it. */
  now?: number;
}) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const status = useQuery({
    queryKey: ["auth", "status"],
    // The same entry the shell gates on (`App`'s own status query): when THIS
    // ask succeeds it writes the shell's answer directly. That is what opens
    // the app — a latched `signedOut` is cleared by the shell when the answer
    // says this client needs no login, instead of waiting up to a minute for
    // the shell's own poll.
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
  // This server just said it wants no login from this client (a local one).
  // The answer is shared cache, so the shell's gate already has it (and opens
  // on its own); this hands the page the fresh data the gate unblocked, the
  // same way a sign-in does — the cache was filled while there was a gate up,
  // so what it holds was fetched from behind it.
  const opened = !!status.data && !status.data.required;
  useEffect(() => {
    if (opened) void qc.invalidateQueries();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opened]);

  // The screen was mounted with NO server to describe (still asking, or the
  // ask failed). A client shell asks for a password on this screen, and a
  // desktop shell whose own backend is a second away from answering must not
  // be handed a password box for a localhost server that never wants one — so
  // that window is a neutral splash with NO form in it at all (not even the
  // username row: an empty name box reads as a password prompt). It is
  // BOUNDED: past SPLASH_MAX_MS the address field and the form take over (the
  // only things a user can act on when nothing answers), and any real answer
  // replaces the splash at once.
  //
  // A 401/428 from the server is already an ANSWER — the shell only gets here
  // on a real one — so `status.data` can stay empty on an unclaimed server
  // (428) and the setup form must not be held behind the splash. `isError` is
  // react-query's carrier for those two (the query function IS
  // `api.authStatus`, so a status error is an AuthError) — and it ends the
  // splash either way: after two failed asks, the address field is what the
  // user needs, not a third wait.
  const answered = status.data !== undefined || status.isError;
  const splash = !answered;
  // Right after a retry (e.g. the splash's "connect to a different server"
  // giving way to the address field) the LAST answer is the failure that
  // ended the splash — usually "nothing is listening on 127.0.0.1:8015" — and
  // isError has gone false while the new ask is in flight. Carrying it forward
  // keeps the address screen saying "no answer" instead of a form that looks
  // like a password prompt for a server nobody has found.
  const [noAnswer, setNoAnswer] = useState(false);
  useEffect(() => {
    if (status.isError) setNoAnswer(true);
    else if (status.data !== undefined) setNoAnswer(false);
  }, [status.data, status.isError]);
  // Bounded by its own clock: mounting or a re-ask restarts it, the timer
  // re-renders the page exactly when the cap is reached — `Date.now()` alone
  // never re-renders — and the shell's `now` starts it past the cap at once
  // (the shell already spent the same bound on ITS splash).
  const [splashStarted, setSplashStarted] = useState(() => Date.now());
  const splashCap = now > 0 || Date.now() - splashStarted >= SPLASH_MAX_MS;
  const [, tick] = useState(0);
  useEffect(() => {
    if (!splash || splashCap) return;
    const id = window.setTimeout(
      () => tick((n) => n + 1),
      Math.max(0, SPLASH_MAX_MS - (Date.now() - splashStarted)),
    );
    return () => window.clearTimeout(id);
  }, [splash, splashStarted, splashCap]);
  useEffect(() => {
    if (!splash) setSplashStarted(Date.now());
  }, [splash]);
  useEffect(() => {
    if (status.data?.username) setUsername((u) => u || status.data!.username);
  }, [status.data]);

  // Every hook is declared ABOVE this return, in the same order on every
  // render: an early return among them (a splash, first draft) would change
  // the hook count between renders and React rejects that outright.
  if (splash && !splashCap) {
    return <ShellSplash />;
  }

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
    <div className="safe-shell min-h-full bg-bg text-zinc-100 flex flex-col items-center justify-center">
      <div className="w-full max-w-2xl space-y-4 p-6">
        {/* The house header, the same one the setup wizard and the backend
            chooser draw: the auth screen used to hand-roll a narrower title
            block, so the first screen of the flow did not match the ones after
            it. */}
        <PageHeader
          icon={needsSetup ? ShieldCheck : Lock}
          title="la musica"
          subtitle={
            needsSetup
              ? t("auth.setup_intro")
              : `${t("auth.sign_in")} — ${status.data?.public_url || serverUrl() || t("auth.server")}`
          }
        />

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

          {noAnswer && (
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
