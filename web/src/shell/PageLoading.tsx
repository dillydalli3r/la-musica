import { useI18n } from "../lib/i18n";

/** Shown while a lazy route's chunk downloads. Inlined (no spinner library) so
 *  the fallback itself is part of the initial bundle. */
export default function PageLoading() {
  const { t } = useI18n();
  return (
    <div className="p-6 max-w-[1600px] mx-auto animate-pulse" aria-busy="true" aria-live="polite">
      <div className="h-7 w-52 rounded bg-raise" />
      <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="h-28 rounded-lg border border-border bg-card" />
        ))}
      </div>
      <span className="sr-only">{t("loading.page")}</span>
    </div>
  );
}