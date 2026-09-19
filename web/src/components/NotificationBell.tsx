import { useEffect, useState } from "react";
import { Bell, BellOff, BellRing } from "lucide-react";
import {
  notificationState,
  requestNotifications,
  startEventStream,
  type NotifyState,
} from "../lib/notify";
import { useI18n } from "../lib/i18n";
import { toast } from "../store";

/** The top bar's notification control, and the lifetime of the event socket.
 *
 *  Two jobs, deliberately in one component: the socket (see lib/notify.ts)
 *  should exist exactly as long as the signed-in app does, and the button that
 *  asks for OS permission should sit where the user can see what it is for.
 *  The permission request hangs off a click because browsers require a user
 *  gesture — a request fired from a socket callback is silently ignored, which
 *  is the classic way this feature ships broken.
 *
 *  A denied permission is shown, not hidden: "notifications blocked" with a
 *  bell-off icon is the honest state, and it is also the one users need to
 *  find their way back from (the browser's own site settings).
 */
export default function NotificationBell() {
  const { t } = useI18n();
  const [state, setState] = useState<NotifyState>(() => notificationState());

  useEffect(() => {
    const stop = startEventStream();
    return stop;
  }, []);

  const enable = async () => {
    const next = await requestNotifications();
    setState(next);
    if (next === "granted") toast.success(t("notify.enabled"));
    else if (next === "denied") toast(t("notify.blocked_help"));
  };

  if (state === "unsupported") return null;

  const Icon = state === "granted" ? BellRing : state === "denied" ? BellOff : Bell;
  const title =
    state === "granted"
      ? t("notify.title_granted")
      : state === "denied"
      ? t("notify.blocked")
      : t("notify.enable");

  return (
    <button
      className={`h-9 w-9 rounded-full border border-border bg-panel/60 backdrop-blur hidden sm:flex items-center justify-center transition-colors ${
        state === "granted"
          ? "text-accent-soft hover:text-accent"
          : "text-zinc-300 hover:text-white hover:border-accent/50"
      }`}
      onClick={enable}
      title={title}
      aria-label={title}
    >
      <Icon className="h-4 w-4" />
    </button>
  );
}
