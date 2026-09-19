import { useState } from "react";
import type { Capabilities } from "../api";
import { useI18n } from "../lib/i18n";
import {
  backgroundHosting,
  isBackgroundHosting,
  setBackgroundHosting,
} from "../lib/clientSetup";

/** "Keep hosting in the background" — the switch that lets the phone stay a
 *  server while its screen is off (Soulseek sharing, a download another client
 *  is streaming from).
 *
 *  What it costs is not the same on the two platforms that can do it, so the
 *  help text comes from backgroundHosting() rather than from here: iOS holds a
 *  silent audio session (the one thing its sandbox lets an idle app keep
 *  running, and exactly what Apple's review guidance calls abuse — the copy
 *  says so plainly), Android runs a foreground service with an ongoing
 *  notification and wants the battery-optimisation exemption. A platform that
 *  cannot do it gets the reason instead of the switch, like every other row of
 *  the capability report.
 *
 *  The choice is written per device and read by the shell
 *  (BACKGROUND_HOSTING_KEY); this component only records it. */
export default function BackgroundHostingToggle({ caps }: { caps: Capabilities | null }) {
  const { t } = useI18n();
  const [on, setOn] = useState(isBackgroundHosting);
  const support = backgroundHosting(caps);
  if (!support) return null;

  if (!support.available) {
    return (
      <p className="text-[11px] text-zinc-600 leading-relaxed">
        <span className="text-zinc-500">{t("client.bg_label")}</span> — {support.help}
      </p>
    );
  }

  return (
    <label className="flex items-start gap-2 text-[11px] text-zinc-500 cursor-pointer">
      <input
        type="checkbox"
        className="mt-0.5"
        checked={on}
        onChange={(e) => {
          setOn(e.target.checked);
          void setBackgroundHosting(e.target.checked);
        }}
      />
      <span>
        <span className="text-zinc-300">{t("client.bg_label")}</span>
        <span className="block leading-relaxed">{support.help}</span>
      </span>
    </label>
  );
}
