import { useState } from "react";
import { Cloud, HardDrive, Loader2, Server } from "lucide-react";
import PageHeader from "../components/PageHeader";
import { useI18n } from "../lib/i18n";
import { chooseBackend } from "../lib/backendShell";

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
export default function BackendChoice({ onDone }: { onDone: () => void }) {
  const { t } = useI18n();
  const [busy, setBusy] = useState<"local" | "remote" | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");

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
    <div className="safe-shell min-h-dvh bg-bg text-zinc-100 flex flex-col items-center justify-center">
      <div className="w-full max-w-2xl space-y-4 p-6">
        <PageHeader icon={Server} title={t("backend.title")} subtitle={t("backend.subtitle")} />

        {/* The same rail idiom as the wizard that follows: the question at
            hand is the accent chip, the sign-in that comes next is pending.
            The built-in path never reaches it, but the rail is about the
            install's shape, not one branch. */}
        <div className="flex flex-wrap items-center gap-2 text-[11px] text-zinc-500">
          <div className="flex items-center gap-2">
            <span className="h-5 w-5 rounded-sm flex items-center justify-center text-[10px] border bg-accent text-[var(--accent-fg)] border-accent">
              1
            </span>
            <span className="text-zinc-200">{t("backend.step_choice")}</span>
          </div>
          <div className="flex items-center gap-2">
            <span className="h-5 w-5 rounded-sm flex items-center justify-center text-[10px] border bg-panel border-border text-zinc-500">
              2
            </span>
            <span className="text-zinc-600">{t("client.step_account")}</span>
          </div>
        </div>

        <div className="panel p-6 space-y-4">
          {starting ? (
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
