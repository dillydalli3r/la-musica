import { X } from "lucide-react";
import { useStore } from "../store";
import { useI18n } from "../lib/i18n";

/** The toast stack. Rendered by EVERY screen the app can show — the shell AND
 *  the setup wizard — so a message the wizard raises ("Saved", a dependency
 *  install finishing with failures) is actually seen. It used to live only in
 *  the shell's return, so a first-run wizard stored toasts nobody could see
 *  and then popped them over the library the moment setup finished. */
export default function Toasts() {
  const toasts = useStore((s) => s.toasts);
  const dismissToast = useStore((s) => s.dismissToast);
  const { t } = useI18n();
  return (
    <div
      className="fixed bottom-20 left-1/2 -translate-x-1/2 z-50 flex flex-col items-center gap-2 w-[min(92vw,30rem)] pointer-events-none"
      aria-live="polite"
    >
      {toasts.map((item) => (
        <div
          key={item.id}
          role={item.severity === "error" ? "alert" : "status"}
          className={`toast-in pointer-events-auto flex items-start gap-2 w-full rounded-lg border px-3.5 py-2 text-sm shadow-xl backdrop-blur ${
            item.severity === "error"
              ? "border-red-900/70 bg-red-950/85 text-red-100"
              : item.severity === "success"
              ? "border-emerald-900/70 bg-emerald-950/85 text-emerald-100"
              : "border-accent/40 bg-panel/95 text-zinc-200"
          }`}
        >
          <span className="flex-1 min-w-0 break-words">{item.message}</span>
          <button
            className="shrink-0 -mr-1 p-0.5 rounded opacity-70 hover:opacity-100"
            onClick={() => dismissToast(item.id)}
            title={t("toast.dismiss")}
            aria-label={t("toast.dismiss")}
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      ))}
    </div>
  );
}