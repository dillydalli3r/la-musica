import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Bell, BellOff, BellRing, X } from "lucide-react";
import {
  notificationState,
  requestNotifications,
  startEventStream,
  type NotifyState,
} from "../lib/notify";
import {
  clearAll,
  dismiss,
  markAllRead,
  openNotification,
  registerNavigator,
  useNotifications,
} from "../lib/notifications";
import { useI18n } from "../lib/i18n";
import Popover from "./Popover";
import { toast } from "../store";

/** The top bar's notification control: the tray, and the lifetime of the event
 *  socket.
 *
 *  Two jobs, deliberately in one component: the socket (see lib/notify.ts)
 *  should exist exactly as long as the signed-in app does, and the button that
 *  asks for OS permission should sit where the user can see what it is for.
 *  The permission request hangs off a click because browsers require a user
 *  gesture — a request fired from a socket callback is silently ignored, which
 *  is the classic way this feature ships broken.
 *
 *  The panel lists every outcome the server announced, newest first (the store
 *  is lib/notifications.ts). Opening it marks them read — that is what puts the
 *  bell's light out — while the entries stay until they are dismissed one by
 *  one or thrown away with Clear all: reading the tray must not erase the only
 *  record of a failed download. A denied permission is shown, not hidden:
 *  "notifications blocked" with a bell-off icon is the honest state, and it is
 *  also the one users need to find their way back from (the browser's own site
 *  settings).
 *
 *  Clicking an entry goes to what it is about — the album, the track, the
 *  Soulseek page, the wizard for an album that needs data. `useNavigate` comes
 *  from THIS component (it sits inside the router in App.tsx), and is handed to
 *  the store so the store itself needs no react-router dependency.
 */
export default function NotificationBell() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const [state, setState] = useState<NotifyState>(() => notificationState());
  const [open, setOpen] = useState(false);
  const { items, unread } = useNotifications();

  useEffect(() => {
    const stop = startEventStream();
    return stop;
  }, []);

  useEffect(() => registerNavigator((to) => navigate(to)), [navigate]);

  const enable = async () => {
    const next = await requestNotifications();
    setState(next);
    if (next === "granted") toast.success(t("notify.enabled"));
    else if (next === "denied") toast(t("notify.blocked_help"));
  };

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (!next) return;
    markAllRead(); // the panel is being read: the badge goes out
    // Permission has to be asked for from a real gesture. Opening the tray is
    // one, so the bell keeps its original one-click "enable notifications".
    if (state === "prompt") void enable();
  };

  const hot = unread > 0;
  const Icon = state === "granted" ? BellRing : state === "denied" ? BellOff : Bell;
  const label = hot
    ? t("notify.unread", { count: unread })
    : state === "granted"
    ? t("notify.title_granted")
    : state === "denied"
    ? t("notify.blocked")
    : state === "unsupported"
    ? t("notify.tray_title")
    : t("notify.enable");

  return (
    <div className="relative">
      <button
        className={`h-9 w-9 rounded-full border border-border bg-panel/60 backdrop-blur flex items-center justify-center transition-colors ${
          hot
            ? "text-accent ring-2 ring-accent/60 shadow-accent/60 shadow-[0_0_12px]"
            : state === "granted"
            ? "text-accent-soft hover:text-accent"
            : "text-zinc-300 hover:text-white hover:border-accent/50"
        }`}
        onClick={toggle}
        title={label}
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <Icon className="h-4 w-4" />
        {hot && (
          <span className="absolute -top-1 -right-1 h-4 min-w-4 px-1 rounded-full bg-accent text-[10px] font-semibold text-zinc-950 flex items-center justify-center">
            {unread > 9 ? "9+" : unread}
          </span>
        )}
      </button>
      <Popover
        open={open}
        onClose={() => setOpen(false)}
        align="right"
        panelClass="w-80 max-h-[70vh] overflow-y-auto p-1.5"
      >
        <div className="flex items-center justify-between gap-2 px-2.5 pt-1 pb-0.5">
          <span className="text-[10px] uppercase tracking-wider text-zinc-500">
            {t("notify.tray_title")}
          </span>
          {items.length > 0 && (
            <button
              className="text-[11px] text-accent-soft hover:text-accent tap"
              onClick={clearAll}
            >
              {t("notify.clear_all")}
            </button>
          )}
        </div>
        {state !== "granted" && state !== "unsupported" && (
          <button
            className="w-full text-left text-xs px-2.5 py-1.5 rounded-lg flex items-center gap-2.5 text-zinc-400 hover:bg-white/10 hover:text-zinc-200 transition-colors tap"
            onClick={enable}
            title={t("notify.blocked_help")}
          >
            <BellOff className="h-3.5 w-3.5 shrink-0" />
            <span className="flex-1">{state === "denied" ? t("notify.blocked") : t("notify.enable")}</span>
          </button>
        )}
        {items.length === 0 ? (
          <div className="px-2.5 py-5 text-center text-xs text-zinc-500">{t("notify.tray_empty")}</div>
        ) : (
          items.map((n) => (
            <div
              key={n.id}
              className="group flex items-start gap-1 rounded-lg hover:bg-white/10 transition-colors"
            >
              <button
                role="menuitem"
                className="min-w-0 flex-1 text-left px-2.5 py-1.5"
                title={n.body ? `${n.title} — ${n.body}` : n.title}
                onClick={() => {
                  setOpen(false);
                  openNotification(n);
                }}
              >
                <div className="text-xs text-zinc-200 truncate flex items-center gap-1.5">
                  {!n.read && <span className="h-1.5 w-1.5 rounded-full bg-accent shrink-0" />}
                  <span className="truncate">{n.title}</span>
                </div>
                {n.body && <div className="text-[11px] text-zinc-500 leading-snug">{n.body}</div>}
                <div className="text-[10px] text-zinc-600 mt-0.5">{aged(n.at)}</div>
              </button>
              <button
                className="tap-hit mt-1 mr-1 p-1 rounded-md text-zinc-600 opacity-60 group-hover:opacity-100 focus-visible:opacity-100 hover:text-zinc-300 shrink-0"
                aria-label={t("notify.dismiss")}
                title={t("notify.dismiss")}
                onClick={() => dismiss(n.id)}
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          ))
        )}
      </Popover>
    </div>
  );
}

/** When an outcome landed, as a clock time today and a date before that. */
function aged(ms: number): string {
  const mins = Math.floor((Date.now() - ms) / 60_000);
  if (mins < 1) return new Date(ms).toLocaleTimeString();
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h`;
  return new Date(ms).toLocaleDateString();
}
