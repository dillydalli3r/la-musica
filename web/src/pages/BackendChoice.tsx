import { useEffect, useState } from "react";
import { AlertTriangle, Cloud, HardDrive, Loader2, Server } from "lucide-react";
import PageHeader from "../components/PageHeader";
import { useI18n } from "../lib/i18n";
import {
  attachBackendShell,
  chooseBackend,
  subscribeBackendFailure,
  subscribeBackendStarting,
} from "../lib/backendShell";
import { STEP_IDS } from "../lib/clientSetup";
import SetupRail from "../components/SetupRail";

/** The shell's very first question: where does this install get a backend?
 *
 *  The desktop shell can either run the app's own backend (the bundled
 *  `mlo-server`, spawned on this machine) or talk to a server the user runs
 *  themselves. The shell reports `mode: "unset"` / `needs_choice: true` until
 *  one is picked; App renders this screen exactly then, before the classic
 *  address wizard.
 *
 *  Choosing tells the SHELL (`choose_backend`). The built-in path then has the
 *  shell spawn the server and navigate this window once it answers — so this
 *  page must never navigate or reload itself: it just shows that starting is
 *  under way. The remote path hands off to the existing wizard via `onDone`.
 */
export default function BackendChoice({ onDone, starting: startingProp = false }: { onDone: () => void; starting?: boolean }) {
  const { t } = useI18n();
  const [busy, setBusy] = useState<"local" | "remote" | null>(null);
  const [starting, setStarting] = useState(startingProp);
  const [failed, setFailed] = useState(false);
  const [error, setError] = useState("");

  // The shell's own answer arrives as an event, and this page may be the one
  // waiting for it: `choose_backend` returns as soon as the choice is
  // recorded, so the only way to learn that the spawn failed is to listen.
  // (Also covers a backend that dies while the spinner is up: the shell
  // reports `stopped` within a few seconds.) The same event says when a start
  // is UNDER WAY — which is also how a choice made from the TRAY reaches this
  // screen: no click happened here, so the state has to come from the shell.
  useEffect(() => {
    const off = attachBackendShell();
    const offFailure = subscribeBackendFailure(() => setFailed(true));
    const offStarting = subscribeBackendStarting(() => {
      setFailed(false);
      setStarting(true);
    });
    return () => {
      offStarting();
      offFailure();
      off?.();
    };
  }, []);

  const pick = async (mode: "local" | "remote") => {
    if (busy) return;
    setBusy(mode);
    setError("");
    const ok = await chooseBackend(mode);
    if (!ok) {
      // The shell could not record it (an older shell, a refusal): stay on the
      // buttons rather than moving a user whose choice was not saved.
      setBusy(null);
      setError(t("backend.error"));
      return;
    }
    if (mode === "local") {
      setStarting(true);
      return;
    }
    onDone();
  };

  return (
    <div className="safe-shell min-h-full bg-bg text-zinc-100 flex flex-col items-center justify-center">
      <div className="w-full max-w-2xl space-y-4 p-6">
        <PageHeader icon={Server} title={t("backend.title")} subtitle={t("backend.subtitle")} />

        {/* The SAME rail as the wizard this leads into, so answering the
            question reads as step 1 of one flow rather than as a screen with
            its own menu: "a server I run" continues on step 2, and the
            built-in path leaves the shell for the app's own first run, whose
            rail is drawn by the same component. */}
        <SetupRail
          steps={STEP_IDS}
          current="server"
          labels={{
            server: t("client.step_server"),
            account: t("client.step_account"),
            notifications: t("client.step_notifications"),
            done: t("client.step_done"),
          }}
        />

        <div className="panel p-6 space-y-4">
          {failed ? (
            /* The shell was in local mode and its backend did not come up (no
               bundled server, no free loopback port) — or it died while this
               screen was waiting. Staying on the spinner forever was the old
               behaviour, and it told the user nothing: no process, no message,
               no way out. The way out matters most, so it comes first. */
            <div className="space-y-3">
              <div className="flex items-start gap-2">
                <AlertTriangle className="h-4 w-4 text-amber-300 mt-0.5 shrink-0" />
                <div className="space-y-1">
                  <p className="text-sm font-semibold">{t("backend.failed_title")}</p>
                  <p className="text-xs text-zinc-400 leading-relaxed">{t("backend.failed_hint")}</p>
                </div>
              </div>
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  className="rounded-md border border-border bg-panel hover:border-accent px-3 py-1.5 text-xs font-medium transition-colors"
                  onClick={() => window.location.reload()}
                  disabled={busy !== null}
                >
                  {t("backend.failed_retry")}
                </button>
                <button
                  type="button"
                  className="rounded-md border border-border bg-panel hover:border-accent px-3 py-1.5 text-xs font-medium transition-colors disabled:opacity-60"
                  onClick={() => void pick("remote")}
                  disabled={busy !== null}
                >
                  {busy === "remote" ? <Loader2 className="h-3.5 w-3.5 animate-spin inline" /> : t("backend.failed_remote")}
                </button>
              </div>
              {error && <p className="text-xs text-amber-300 break-words">{error}</p>}
            </div>
          ) : starting ? (
            <div className="flex flex-col items-center gap-3 py-6 text-center">
              <Loader2 className="h-6 w-6 animate-spin text-accent" />
              <p className="text-sm font-semibold">{t("backend.starting_title")}</p>
              <p className="text-xs text-zinc-400 leading-relaxed max-w-md">{t("backend.starting_hint")}</p>
            </div>
          ) : (
            <>
              <div className="grid gap-3">
                <button
                  type="button"
                  className="w-full text-left rounded-lg border border-border bg-panel hover:border-accent focus:outline-none focus-visible:border-accent p-4 transition-colors disabled:opacity-60"
                  onClick={() => void pick("local")}
                  disabled={busy !== null}
                >
                  <div className="flex items-center gap-2">
                    <HardDrive className="h-4 w-4 text-accent shrink-0" />
                    <span className="text-sm font-semibold">{t("backend.local_title")}</span>
                    <span className="chip bg-raise border border-border text-emerald-300 ml-auto">
                      {t("backend.recommended")}
                    </span>
                  </div>
                  <p className="mt-1.5 text-xs text-zinc-400 leading-relaxed">{t("backend.local_hint")}</p>
                </button>

                <button
                  type="button"
                  className="w-full text-left rounded-lg border border-border bg-panel hover:border-accent focus:outline-none focus-visible:border-accent p-4 transition-colors disabled:opacity-60"
                  onClick={() => void pick("remote")}
                  disabled={busy !== null}
                >
                  <div className="flex items-center gap-2">
                    <Cloud className="h-4 w-4 text-accent shrink-0" />
                    <span className="text-sm font-semibold">{t("backend.remote_title")}</span>
                    {busy === "remote" && <Loader2 className="h-3.5 w-3.5 animate-spin ml-auto" />}
                  </div>
                  <p className="mt-1.5 text-xs text-zinc-400 leading-relaxed">{t("backend.remote_hint")}</p>
                </button>
              </div>

              {error && <p className="text-xs text-amber-300 break-words">{error}</p>}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
