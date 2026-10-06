import { useEffect, useSyncExternalStore } from "react";
import { Download, RotateCw } from "lucide-react";
import { t } from "../lib/i18n";
import {
  checkForUpdate,
  installUpdate,
  subscribeUpdate,
  updateState,
  updateSupported,
} from "../lib/updater";

/** The desktop shell updates ITSELF, and this is the screen that says so.
 *
 *  Settings → Security is already where "which version am I and is there a
 *  newer one" is answered for the SERVER (`ServerVersionNotice`, beside this),
 *  and the shell is a second, independent thing to be out of date: it ships the
 *  backend but it is the binary the user launches, so it is the one that can be
 *  replaced from inside the app. `lib/updater.ts` owns the conversation with the
 *  shell (it is where the launch check lives too, so the tray has already been
 *  told by the time anyone opens this screen).
 *
 *  Four states, in the order they happen: nothing to install (the version and a
 *  way to ask again), an offer (what it is, what it will replace, one button),
 *  a download with real bytes behind the bar, then "installing" — the point
 *  where the app is about to be replaced and a percentage stops meaning
 *  anything. Failures are a sentence, never a broken-looking screen: being
 *  offline is a normal way for a version check to go. */
export default function ClientUpdate() {
  const state = useSyncExternalStore(subscribeUpdate, updateState);

  useEffect(() => {
    // Once, on mount: opening this screen is the user asking, and a check left
    // pending from boot is already in flight (the two race harmlessly).
    void checkForUpdate();
  }, []);

  if (!updateSupported()) return null;
  const { current, offer, checking, stage, progress, error } = state;
  const percent =
    progress?.total ? Math.min(100, Math.round((progress.downloaded / progress.total) * 100)) : null;

  return (
    // `data-update-row` is the hook `tools/check_update_row.mjs` reads the four
    // states through — the row has no role or heading of its own to select by.
    <div data-update-row="" className="space-y-1.5">
      {error && (
        <div className="text-[11px] text-red-400">{t("update.check_failed", { error })}</div>
      )}

      {stage !== "idle" ? (
        // The bytes are moving, or the swap is: no button can help now, and the
        // app is about to restart, so this says what is happening instead.
        <div className="space-y-1.5">
          <div className="text-[11px] text-zinc-300">
            {stage === "installing"
              ? t("update.installing", { version: offer?.version ?? "" })
              : t("update.downloading", { version: offer?.version ?? "" })}
          </div>
          {stage === "downloading" && (
            <div className="h-1 overflow-hidden rounded-full bg-raise">
              <div
                className="h-full bg-accent transition-[width] duration-200"
                style={{ width: percent === null ? "35%" : `${percent}%` }}
              />
            </div>
          )}
          {stage === "downloading" && (
            <div className="text-[10px] text-zinc-500">
              {percent === null ? "" : `${percent}% — `}
              {t("update.restart_hint")}
            </div>
          )}
        </div>
      ) : offer ? (
        <div className="flex items-start gap-2 rounded-md border border-amber-900/60 bg-amber-950/30 px-2.5 py-1.5">
          <div className="min-w-0 flex-1">
            <div className="text-[11px] text-amber-200/90">
              {t("update.available", { current, version: offer.version })}
            </div>
            {/* The release's own words, as text (`line-clamp` keeps a long
                changelog from taking the panel over). */}
            {offer.notes && (
              <div className="mt-0.5 line-clamp-2 whitespace-pre-line text-[10px] text-amber-200/60">
                {offer.notes}
              </div>
            )}
          </div>
          <button
            className="btn-primary shrink-0 text-xs"
            onClick={() => void installUpdate()}
          >
            <Download className="h-3.5 w-3.5" />
            {t("update.install")}
          </button>
        </div>
      ) : (
        <div className="flex items-center gap-2 text-[11px] text-zinc-500">
          <span>{checking ? t("update.checking") : t("update.up_to_date", { current })}</span>
          <button
            className="btn-ghost !py-0.5 text-[11px]"
            onClick={() => void checkForUpdate()}
            disabled={checking}
          >
            <RotateCw className="h-3 w-3" />
            {t("update.check_now")}
          </button>
        </div>
      )}
    </div>
  );
}