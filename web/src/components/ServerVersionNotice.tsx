import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Github, X } from "lucide-react";
import { api } from "../api";
import { useI18n } from "../lib/i18n";

/** The version the connected server is running, plus the one-line update
 *  notice when upstream has a newer release.
 *
 *  Shown by Settings → Security and by the setup wizard's last step, so a
 *  fresh client is told what it just connected to. The server does the GitHub
 *  check (cached 6 h, never fatal), so this never waits on the network itself:
 *  without an answer the line simply does not render.
 *
 *  Dismissal is per client and per version — dismissing 3.1.0 must not hide
 *  3.2.0 — and localStorage-only, like the other per-device display picks. */
const DISMISS_KEY = "mlo.updateDismissed";

export default function ServerVersionNotice() {
  const { t } = useI18n();
  const [dismissed, setDismissed] = useState(() => localStorage.getItem(DISMISS_KEY) || "");
  const { data } = useQuery({
    queryKey: ["version"],
    queryFn: api.version,
    staleTime: 3600_000,
    retry: 0, // an older server has no such route; the version line is enough
  });

  if (!data?.version) return null;
  const offer = data.update_available && !!data.latest && data.latest !== dismissed;
  const revision = data.build?.revision ?? "";
  const built = (data.build?.built ?? "").slice(0, 10);
  const inImage = !!data.build?.container;

  return (
    <div className="space-y-1">
      <div className="flex items-baseline gap-2">
        <span className="font-mono text-[11px] text-zinc-400 break-all">
          {t("settings.server_version", { version: data.version })}
        </span>
        {/* Right where the version is read: the repository it came from. */}
        {data.project_url && (
          <a
            className="inline-flex items-center gap-1 text-[11px] text-zinc-500 hover:text-accent-soft shrink-0"
            href={data.project_url}
            target="_blank"
            rel="noreferrer"
            title={t("credits.repo")}
          >
            <Github className="h-3 w-3 shrink-0" />
            {t("credits.source")}
          </a>
        )}
      </div>
      {/* The image's own identity, when the build baked one in. Without it a
          user cannot tell "the updater is broken" from "the image really is
          the newest one" — the running version string looks the same either
          way until watchtower replaces the image. */}
      {inImage && revision && (
        <div className="font-mono text-[10px] text-zinc-600 break-all">
          {built
            ? t("settings.image_build", { revision, built })
            : t("settings.image_build_no_date", { revision })}
        </div>
      )}
      {offer && (
        <div className="flex items-start gap-2 rounded-md border border-amber-900/60 bg-amber-950/30 px-2.5 py-1.5">
          <span className="flex-1 text-[11px] text-amber-200/90 leading-relaxed">
            {t("settings.update_available", { version: data.version, latest: data.latest })}
          </span>
          {data.release_url && (
            <a
              className="text-[11px] text-amber-200 underline shrink-0"
              href={data.release_url}
              target="_blank"
              rel="noreferrer"
            >
              {t("settings.update_link")}
            </a>
          )}
          <button
            type="button"
            aria-label={t("settings.update_dismiss")}
            className="tap-hit text-amber-200/80 hover:text-amber-100 shrink-0"
            onClick={() => {
              try {
                localStorage.setItem(DISMISS_KEY, data.latest);
              } catch {
                /* private mode: the notice simply returns next launch */
              }
              setDismissed(data.latest);
            }}
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      )}
      {/* A Docker install updates ITSELF, and the question that follows is
          "so why has nothing happened?". This says how the mechanism works and
          how to check on it right now, so the honest answer ("the image really
          is current") is readable without reading watchtower's log. */}
      {inImage && (
        <div className="text-[10px] leading-relaxed text-zinc-600">
          {t("settings.updater_auto")}
        </div>
      )}
    </div>
  );
}
