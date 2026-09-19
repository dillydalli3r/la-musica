import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { KeyRound, Loader2, LogOut, ShieldCheck } from "lucide-react";
import { api, IN_TAURI, serverUrl, setToken } from "../api";
import { useI18n } from "../lib/i18n";
import { toast } from "../store";

/** Settings → Security: the password, this client's session, and the facts a
 *  user needs to reason about both.
 *
 *  It deliberately shows the *state* rather than just the controls — where the
 *  server is, whether the gate is on for this address and why, and how many
 *  sessions exist — because the two mistakes this screen prevents are "I set a
 *  password but my phone still gets in" (it is not reaching this server) and
 *  "I signed out but the tab still works" (an old token was still in
 *  localStorage).
 */
export default function SecurityPanel() {
  const { t } = useI18n();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const status = useQuery({
    queryKey: ["auth", "status"],
    queryFn: api.authStatus,
    staleTime: 15000,
  });

  const signOut = async (everywhere: boolean) => {
    setBusy(true);
    try {
      if (everywhere) await api.authRevokeAll();
      else await api.authLogout();
      setToken(null);
      toast.success(everywhere ? t("auth.revoked") : t("auth.sign_out"));
      // Reload rather than clearing state by hand: every cached query, the
      // player and the event socket are keyed on "being signed in", and the
      // shell's own gate renders the login screen on the way back up.
      window.location.reload();
    } catch (e) {
      toast.error(String(e));
      setBusy(false);
    }
  };

  const changePassword = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      // Changing the password revokes every session server-side, and the route
      // hands this caller a fresh one — keep it, or the tab signs itself out.
      const session = await api.authChangePassword(current, next, confirm);
      setToken(session.token);
      setCurrent("");
      setNext("");
      setConfirm("");
      toast.success(t("auth.changed"));
      void status.refetch();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const gateOn = !!status.data?.required;

  return (
    <div className="space-y-5">
      <div className="panel space-y-3">
        <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">
          {t("settings.security")}
        </div>
        <div className="rounded-md border border-border bg-zinc-950/40 px-3 py-2 space-y-1">
          <div className="flex items-center gap-2 text-xs text-zinc-300">
            <ShieldCheck className={`h-3.5 w-3.5 ${gateOn ? "text-emerald-400" : "text-zinc-500"}`} />
            {status.data?.authenticated
              ? status.data.username || t("auth.server")
              : gateOn
              ? t("auth.sign_in")
              : t("auth.server")}
          </div>
          <div className="font-mono text-[11px] text-zinc-400 break-all">
            {serverUrl() || window.location.origin}
          </div>
          <p className="text-[11px] text-zinc-600 leading-relaxed">{t("auth.gate_help")}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button className="btn-ghost !py-1.5 text-xs" disabled={busy} onClick={() => void signOut(false)}>
            <LogOut className="h-3.5 w-3.5" /> {t("auth.sign_out")}
          </button>
          <button className="btn-ghost !py-1.5 text-xs" disabled={busy} onClick={() => void signOut(true)}>
            <LogOut className="h-3.5 w-3.5" /> {t("auth.revoke_all")}
          </button>
        </div>
      </div>

      <details className="panel" open={!gateOn}>
        <summary className="text-sm font-semibold cursor-pointer">{t("auth.change_password")}</summary>
        <form className="mt-3 space-y-3 max-w-sm" onSubmit={changePassword}>
          <label className="block">
            <span className="text-xs text-zinc-500 uppercase">{t("auth.current_password")}</span>
            <input
              className="input mt-1"
              type="password"
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
              autoComplete="current-password"
            />
          </label>
          <label className="block">
            <span className="text-xs text-zinc-500 uppercase">{t("auth.new_password")}</span>
            <input
              className="input mt-1"
              type="password"
              value={next}
              onChange={(e) => setNext(e.target.value)}
              autoComplete="new-password"
            />
          </label>
          <label className="block">
            <span className="text-xs text-zinc-500 uppercase">{t("auth.repeat_password")}</span>
            <input
              className="input mt-1"
              type="password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              autoComplete="new-password"
            />
          </label>
          <button className="btn-primary !py-1.5 text-xs" disabled={busy || !next}>
            {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <KeyRound className="h-3.5 w-3.5" />}
            {t("auth.change_password")}
          </button>
          {!gateOn && (
            <p className="text-[11px] text-zinc-600 leading-relaxed">
              {IN_TAURI
                ? t("auth.server_address_help")
                : t("auth.gate_help")}
            </p>
          )}
        </form>
      </details>
    </div>
  );
}
