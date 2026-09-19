import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { HardDrive, KeyRound, Loader2, LogOut, Server, ShieldCheck, Trash2, UserPlus, Wand2 } from "lucide-react";
import { api, IN_TAURI, normalizeServerUrl, serverUrl, setServerUrl, setToken } from "../api";
import ConfirmButton from "./ConfirmButton";
import ServerVersionNotice from "./ServerVersionNotice";
import { useI18n } from "../lib/i18n";
import {
  hostOnDeviceUrl,
  isClientShell,
  isHostingOnThisDevice,
  probeServer,
  resetClientSetup,
  type ProbeResult,
} from "../lib/clientSetup";
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
  const [mode, setMode] = useState<"server" | "host">(() => (isHostingOnThisDevice() ? "host" : "server"));
  const [address, setAddress] = useState(serverUrl());
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const [testing, setTesting] = useState(false);
  const status = useQuery({
    queryKey: ["auth", "status"],
    queryFn: api.authStatus,
    staleTime: 15000,
  });
  // The server's users. Fetched here rather than derived from the config's
  // claim: on a server claimed before users existed the two disagree, and the
  // list is the one that decides who can sign in.
  const users = useQuery({
    queryKey: ["auth", "users"],
    queryFn: api.authUsers,
    staleTime: 15000,
  });
  const [newName, setNewName] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [newConfirm, setNewConfirm] = useState("");
  const [userError, setUserError] = useState("");

  const addUser = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setUserError("");
    try {
      const added = await api.authAddUser(newName.trim(), newPassword, newConfirm);
      setNewName("");
      setNewPassword("");
      setNewConfirm("");
      toast.success(t("settings.user_added", { user: added.username }));
      void users.refetch();
    } catch (err) {
      // The server's own words: "username is required", "password must be at
      // least 8 characters", "the passwords do not match".
      setUserError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const removeUser = async (name: string) => {
    setBusy(true);
    try {
      await api.authRemoveUser(name);
      toast.success(t("settings.user_removed", { user: name }));
      void users.refetch();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  /** Probe whichever address the chosen mode means, so Save can only be
   *  pressed for an address that has actually answered — the same rule the
   *  wizard applies, and the reason a wrong address here cannot leave a
   *  client pointed at nothing. */
  const test = async () => {
    const target = mode === "host" ? hostOnDeviceUrl() : normalizeServerUrl(address);
    if (mode === "server") setAddress(target);
    setTesting(true);
    setProbe(null);
    const r = await probeServer(target);
    setProbe(r);
    setTesting(false);
  };

  const save = () => {
    setServerUrl(mode === "host" ? hostOnDeviceUrl() : normalizeServerUrl(address));
    // Reload rather than re-point by hand: the API base, every cached query and
    // the event socket are fixed when the module loads, and the session token
    // belongs to the server we just left.
    window.location.reload();
  };

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
      {/* Which server this client talks to, for EVERY client — the wizard that
          asks the same question is a client-shell gate, and the login screen
          only offers the address while nothing has answered, so the browser
          build had no way back from a wrong address at all. */}
      <div className="panel space-y-3">
        <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">
          {t("settings.connection")}
        </div>
        <div className="rounded-md border border-border bg-zinc-950/40 px-3 py-2 space-y-2">
          <div className="flex items-center justify-between gap-3 text-xs text-zinc-300">
            {mode === "host" ? <HardDrive className="h-3.5 w-3.5" /> : <Server className="h-3.5 w-3.5" />}
            <select
              className="input tap !w-auto !py-1 tap"
              aria-label={t("settings.connection")}
              value={mode}
              onChange={(e) => {
                setMode(e.target.value as "server" | "host");
                setProbe(null); // the other mode's answer says nothing about this one
              }}
            >
              <option value="server">{t("client.mode_connect")}</option>
              <option value="host">{t("client.mode_host")}</option>
            </select>
          </div>

          {mode === "server" ? (
            <div className="space-y-1">
              <div className="flex gap-2">
                <input
                  className="input font-mono text-xs"
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
                  className="btn-ghost tap !py-1.5 text-xs shrink-0"
                  onClick={() => void test()}
                  disabled={testing || !address.trim()}
                >
                  {testing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Server className="h-3.5 w-3.5" />}
                  {t("client.test")}
                </button>
              </div>
              {normalizeServerUrl(address) && normalizeServerUrl(address) !== address.trim() && (
                <div className="text-[11px] text-zinc-500 font-mono">→ {normalizeServerUrl(address)}</div>
              )}
              <p className="text-[11px] text-zinc-600 leading-relaxed">{t("auth.server_address_help")}</p>
            </div>
          ) : (
            <div className="space-y-1">
              <div className="font-mono text-[11px] text-zinc-400 break-all">
                {hostOnDeviceUrl() || window.location.origin}
              </div>
              <p className="text-[11px] text-zinc-600 leading-relaxed">{t("client.host_help")}</p>
              <button
                type="button"
                className="btn-ghost tap !py-1.5 text-xs"
                onClick={() => void test()}
                disabled={testing}
              >
                {testing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Server className="h-3.5 w-3.5" />}
                {t("client.test")}
              </button>
            </div>
          )}

          {probe &&
            (probe.ok ? (
              <p className="text-xs text-emerald-300">{t("client.test_ok", { version: probe.version || "" })}</p>
            ) : (
              <p className="text-xs text-amber-300 break-words">
                {mode === "host" && !isClientShell()
                  ? t("client.host_no_python")
                  : t("client.test_fail", { error: probe.error || "" })}
              </p>
            ))}

          <div className="flex items-center gap-2">
            <button
              type="button"
              className="btn-primary tap !py-1.5 text-xs"
              disabled={!probe?.ok}
              onClick={save}
            >
              {t("action.save")}
            </button>
            <span className="text-[11px] text-zinc-600">{probe?.ok ? "" : t("client.need_test")}</span>
          </div>
          <ServerVersionNotice />
        </div>
      </div>

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
          <p className="text-[11px] text-zinc-600 leading-relaxed">{t("auth.gate_help")}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button className="btn-ghost !py-1.5 text-xs" disabled={busy} onClick={() => void signOut(false)}>
            <LogOut className="h-3.5 w-3.5" /> {t("auth.sign_out")}
          </button>
          <button className="btn-ghost !py-1.5 text-xs" disabled={busy} onClick={() => void signOut(true)}>
            <LogOut className="h-3.5 w-3.5" /> {t("auth.revoke_all")}
          </button>
          {/* Client shells only: the wizard that asked for this device's
              server. It is re-runnable on purpose — moving the server, or
              having skipped a step, must not mean reinstalling the app. The
              reload is the wizard's own contract (the API base and token
              change under it). */}
          {isClientShell() && (
            <button
              className="btn-ghost !py-1.5 text-xs"
              disabled={busy}
              onClick={() => {
                resetClientSetup();
                window.location.reload();
              }}
            >
              <Wand2 className="h-3.5 w-3.5" /> {t("client.rerun")}
            </button>
          )}
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

      {/* Who can sign in. The password form above changes the CURRENT user's
          password; this is the server operator's view: everyone else on the
          server, adding a person, and removing one. */}
      <div className="panel space-y-3">
        <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">{t("settings.users")}</div>
        <p className="text-[11px] text-zinc-600 leading-relaxed">{t("settings.users_help")}</p>

        <div className="space-y-1">
          {(users.data?.users ?? []).map((name) => {
            const self = name === users.data?.you;
            // The last user is not removable at all (the server refuses it: no
            // users left means the config's claim decides who gets in), and
            // removing yourself would revoke the session you are using.
            const locked = self || (users.data?.users.length ?? 0) <= 1;
            return (
              <div
                key={name}
                className="flex items-center justify-between gap-2 rounded-md border border-border bg-zinc-950/40 px-3 py-1.5"
              >
                <span className="text-xs text-zinc-200 break-all">
                  {name}
                  {self && (
                    <span className="text-[10px] uppercase tracking-wider text-accent-soft ml-2">
                      {t("settings.users_you")}
                    </span>
                  )}
                </span>
                <ConfirmButton
                  iconOnly
                  className="btn-icon tap-hit"
                  onConfirm={() => void removeUser(name)}
                  disabled={busy || locked}
                  title={self ? t("settings.user_remove_self") : t("settings.user_remove")}
                >
                  <Trash2 className="h-4 w-4" />
                </ConfirmButton>
              </div>
            );
          })}
          {users.isError && <p className="text-xs text-amber-300">{t("settings.users_error")}</p>}
          {users.data && !users.data.users.length && (
            <p className="text-[11px] text-zinc-600 leading-relaxed">{t("settings.users_empty")}</p>
          )}
        </div>

        <form className="space-y-3 max-w-sm" onSubmit={addUser}>
          <label className="block">
            <span className="text-xs text-zinc-500 uppercase">{t("auth.username")}</span>
            <input
              className="input mt-1"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              autoComplete="off"
              spellCheck={false}
            />
          </label>
          <label className="block">
            <span className="text-xs text-zinc-500 uppercase">{t("auth.new_password")}</span>
            <input
              className="input mt-1"
              type="password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              autoComplete="new-password"
            />
            <span className="text-[11px] text-zinc-600 block mt-1">{t("settings.user_password_help")}</span>
          </label>
          <label className="block">
            <span className="text-xs text-zinc-500 uppercase">{t("auth.repeat_password")}</span>
            <input
              className="input mt-1"
              type="password"
              value={newConfirm}
              onChange={(e) => setNewConfirm(e.target.value)}
              autoComplete="new-password"
            />
          </label>
          {userError && (
            <p
              role="alert"
              className="text-xs text-red-300 bg-red-950/40 border border-red-900/60 rounded-md px-3 py-2"
            >
              {userError}
            </p>
          )}
          <button
            className="btn-primary tap !py-1.5 text-xs"
            disabled={busy || !newName.trim() || !newPassword}
          >
            {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <UserPlus className="h-3.5 w-3.5" />}
            {t("settings.user_add")}
          </button>
        </form>
      </div>
    </div>
  );
}
