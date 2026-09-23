import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Check, Loader2, LogIn, LogOut, RefreshCw, Server, User } from "lucide-react";
import { api, setToken } from "../api";
import { useI18n } from "../lib/i18n";
import Popover, { MenuItem } from "./Popover";
import { toast } from "../store";

/** The top bar's account control: who this browser is signed in as, and how to
 *  become someone else.
 *
 *  Two questions the app could only answer in Settings → Security — which user
 *  am I, and how do I change it — moved to where a user actually looks for
 *  their own account. The list is the server's own (`/api/auth/users`), and it
 *  is offered as a SWITCH and not as administration: pick a user, type that
 *  user's password, and you are them. Nobody changes anyone's password here.
 *
 *  The switch ends in `setToken(session.token)` followed by a reload — the same
 *  ending SecurityPanel uses for sign-out and password change, and for the same
 *  reason: every cached query, the player and the event socket are keyed on
 *  being signed in, so swapping the identity out from under them would leave
 *  another user's library, queue and history on screen.
 *
 *  Three states are shown rather than hidden, because each has its own fix. The
 *  server's own account — the default/admin scope, where an unclaimed install's
 *  playlists, likes and favourites live — is NOT a row in the users table, so
 *  the list names it instead of leaving the current identity blank. A server
 *  with no users has nothing to switch TO, so the panel says that and points at
 *  Settings → Security, where users are made. A user list that could not be
 *  read is reported with a Retry: an empty list would read as "nobody can sign
 *  in", which is a different and much worse claim than "the server did not
 *  answer". Choosing the identity already in use does nothing at all — a
 *  password prompt for the password just proved is a prompt with no answer.
 *
 *  The users request is issued from the bar rather than on open, because the
 *  button's own face is the current user's initial; it is the same query key
 *  the Security panel uses (staleTime 15 s), so the two surfaces share one
 *  answer and cannot disagree about who is on the server.
 *
 *  The rows carry `role="menuitem"` like every other popover row in the app,
 *  although a password field sits among them — `role="menu"` is Popover's, and
 *  the alternative (a textbox in a menu) only reads as one to a screen reader
 *  that implements menu traversal, which browsers do not. A labelled input that
 *  submits on Enter is the accessible part here, and it is real.
 */
export default function AccountMenu() {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  // The identity a password is being asked for, or null while none is.
  const [target, setTarget] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const passwordRef = useRef<HTMLInputElement>(null);

  const users = useQuery({
    queryKey: ["auth", "users"],
    queryFn: api.authUsers,
    staleTime: 15000,
  });

  const identities = useMemo<Identity[]>(() => {
    const data = users.data;
    if (!data) return []; // pending or failed: the panel says which
    const list: Identity[] = [];
    // The default scope. It is deliberately absent from `users` (see the
    // server's own users route), so nothing else here would mark it as the
    // identity in use.
    if (!data.you) list.push({ name: "", current: true });
    for (const name of data.users) list.push({ name, current: name === data.you });
    // A session carrying the config's own claim: a name no users row holds (an
    // install claimed before the users table existed). It is still who you are,
    // so it is still listed and still marked.
    if (data.you && !data.users.includes(data.you)) list.push({ name: data.you, current: true });
    return list;
  }, [users.data]);

  useEffect(() => {
    // Picking a name is only a step towards typing a password, so the field
    // takes the keyboard as soon as it is on screen.
    if (target !== null) passwordRef.current?.focus();
  }, [target]);

  const you = users.data?.you ?? "";
  const label = (name: string) => name || t("account.server_account");
  // The server's own account has no name to take a letter from: its face is the
  // person glyph, which is also the honest face while the list is still being
  // fetched (an initial at that moment would be a guess).
  const initial = you ? Array.from(you)[0].toUpperCase() : "";

  const close = () => {
    setOpen(false);
    // Closing forgets a half-done switch: a password typed for someone else
    // must not still be sitting in the field next time this is opened.
    setTarget(null);
    setPassword("");
    setError("");
  };

  const pick = (name: string) => {
    if (name === you) return; // the identity already in use: nothing to prove
    setTarget(name);
    setPassword("");
    setError("");
  };

  const switchTo = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy || target === null || !password) return;
    setBusy(true);
    setError("");
    try {
      // No username means the server's own account: the server resolves that to
      // its config claim, or to the only user it has (auth.login_user), which is
      // the same request the login screen makes when no name is offered.
      // `session.username` is who actually got in — never assume it is the name
      // this client sent.
      const session = await api.authLogin(password, target || undefined);
      setToken(session.token);
      toast.success(t("account.title", { user: session.username || label(target) }));
      // Reload rather than swapping state by hand: every cached query, the
      // player and the event socket are keyed on "being signed in", and the
      // shell renders the other user's data only from a clean start.
      window.location.reload();
    } catch (err) {
      // The server's own words: "wrong password", "too many attempts — try
      // again in 30s". Nothing here has changed yet, so the retry is the same
      // form with the same target.
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  };

  const signOut = async () => {
    if (busy) return;
    setBusy(true);
    try {
      await api.authLogout();
      setToken(null);
      toast.success(t("auth.sign_out"));
      // The same reload SecurityPanel signs out with, and the same reason: the
      // shell's own gate renders the login screen on the way back up.
      window.location.reload();
    } catch (e) {
      toast.error(String(e));
      setBusy(false);
    }
  };

  return (
    <div className="relative">
      <button
        /* The bell's 36 px circle, at the bar's right edge: the face is the
           current user's initial (or the person glyph when there is no name to
           take one from). `.tap-hit` grows the touch target without moving the
           36 px box, which is the rule the rest of this bar's icon buttons
           follow. */
        className="tap-hit relative h-9 w-9 rounded-full border border-border bg-panel/60 backdrop-blur flex items-center justify-center text-zinc-300 hover:text-white hover:border-accent/50 transition-colors text-[13px] font-semibold"
        onClick={() => (open ? close() : setOpen(true))}
        title={users.data ? t("account.title", { user: label(you) }) : t("account.header")}
        aria-label={users.data ? t("account.title", { user: label(you) }) : t("account.header")}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        {initial || <User className="h-4 w-4" aria-hidden="true" />}
      </button>
      <Popover
        open={open}
        onClose={close}
        align="right"
        // Portaled, fixed and pinned to the app's own 8 px viewport gutter for
        // the bell's reasons: the top bar is a narrow `z-10 overflow-hidden`
        // column inside a shell whose sidebar is `z-20`, so an anchored panel
        // here is clipped at the column's edge and painted under the rail.
        fixed
        rightGap={8}
        panelClass="w-72 max-h-[70vh] overflow-y-auto p-1.5"
      >
        <div className="px-2.5 pt-1 pb-1">
          <div className="text-[10px] uppercase tracking-wider text-zinc-500">{t("account.header")}</div>
          {users.isPending ? (
            <div className="mt-1 flex items-center gap-2 text-xs text-zinc-500">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              {t("account.loading")}
            </div>
          ) : users.isError ? (
            <div className="mt-1 space-y-1.5">
              <p className="text-xs text-amber-300">{t("settings.users_error")}</p>
              <button
                className="btn-ghost tap !py-1 text-xs"
                onClick={() => void users.refetch()}
                disabled={users.isFetching}
              >
                {users.isFetching ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <RefreshCw className="h-3.5 w-3.5" />
                )}
                {t("account.retry")}
              </button>
            </div>
          ) : (
            <div className="mt-1 text-xs text-zinc-200 break-all">{t("account.title", { user: label(you) })}</div>
          )}
        </div>

        {/* Who else can be signed in as. The rows are the same shape as every
            other popover row (see MenuItem); the current one is a plain marker
            — clicking it does nothing. */}
        {users.data && (
          <div className="mt-0.5 border-t border-white/5 pt-1">
            <div className="px-2.5 pb-0.5 text-[10px] uppercase tracking-wider text-zinc-500">
              {t("account.choose")}
            </div>
            {identities.map((id) => (
              <div key={id.name || "default"}>
                <button
                  role="menuitem"
                  aria-current={id.current ? "true" : undefined}
                  className={`w-full text-left text-xs px-2.5 py-1.5 rounded-lg flex items-center gap-2.5 transition-colors tap ${
                    id.current ? "text-zinc-400 cursor-default" : "text-zinc-300 hover:bg-white/10"
                  } ${target === id.name && !id.current ? "bg-white/10" : ""}`}
                  title={id.current ? `${label(id.name)} — ${t("settings.users_you")}` : label(id.name)}
                  onClick={() => pick(id.name)}
                >
                  {id.current ? (
                    <Check className="h-3.5 w-3.5 shrink-0 text-accent" />
                  ) : id.name ? (
                    <User className="h-3.5 w-3.5 shrink-0" />
                  ) : (
                    <Server className="h-3.5 w-3.5 shrink-0" />
                  )}
                  <span className="flex-1 truncate">{label(id.name)}</span>
                  {id.current && (
                    <span className="text-[10px] uppercase tracking-wider text-accent-soft shrink-0">
                      {t("settings.users_you")}
                    </span>
                  )}
                </button>
                {/* The password appears under the row it belongs to, so the
                    target is never in doubt — and it is a real form, so Enter
                    submits it. */}
                {target === id.name && !id.current && (
                  <form
                    className="mx-1 my-1.5 space-y-1.5 rounded-lg border border-border bg-zinc-950/60 p-2"
                    onSubmit={switchTo}
                  >
                    <label className="block">
                      <span className="text-[10px] uppercase tracking-wider text-zinc-500">
                        {t("auth.password")}
                      </span>
                      <input
                        ref={passwordRef}
                        className="input mt-1 text-xs"
                        type="password"
                        value={password}
                        onChange={(e) => setPassword(e.target.value)}
                        autoComplete="current-password"
                        spellCheck={false}
                        disabled={busy}
                      />
                    </label>
                    {error && (
                      <p role="alert" className="text-[11px] text-red-300 break-words">
                        {t("account.failed", { error })}
                      </p>
                    )}
                    <button className="btn-primary tap !py-1.5 text-xs w-full" disabled={busy || !password}>
                      {busy ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <LogIn className="h-3.5 w-3.5" />
                      )}
                      {busy ? t("account.switching") : t("account.switch")}
                    </button>
                  </form>
                )}
              </div>
            ))}
            {/* Nothing to switch to, and that is a state of the server, not an
                error: an unclaimed install has one scope and no users at all. */}
            {!users.data.users.length && (
              <p className="px-2.5 py-2 text-[11px] leading-relaxed text-zinc-600">{t("account.no_users")}</p>
            )}
          </div>
        )}

        {/* Signing out is duplicated from Settings on purpose: this is where a
            user looks for it. */}
        <div className="mt-0.5 border-t border-white/5 pt-1">
          <MenuItem
            label={t("auth.sign_out")}
            icon={LogOut}
            danger
            disabled={busy}
            onClick={() => void signOut()}
          />
        </div>
      </Popover>
    </div>
  );
}

/** One identity this menu can offer: `name` is "" for the server's own account,
 *  which is a scope and not a row in the users table. */
type Identity = { name: string; current: boolean };
