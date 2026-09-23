import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Bell, BellOff, BellRing, Send, X } from "lucide-react";
import {
  disablePush,
  enablePush,
  notificationState,
  pushEnabled,
  pushStatus,
  pushSupport,
  refreshPush,
  requestNotifications,
  sendTestPush,
  startEventStream,
  type NotifyState,
  type PushStatus,
  type PushSupport,
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
  // Remote push (see lib/notify.ts): what this client can do is fixed for the
  // session, what the SERVER can do needs one call, and whether this device is
  // subscribed is storage.
  const [pushCap] = useState<PushSupport>(() => pushSupport());
  const [push, setPush] = useState<PushStatus | null>(null);
  const [pushOn, setPushOn] = useState(() => pushEnabled());
  const [pushBusy, setPushBusy] = useState(false);

  useEffect(() => {
    const stop = startEventStream();
    return stop;
  }, []);

  useEffect(() => registerNavigator((to) => navigate(to)), [navigate]);

  // Kept fresh on every load: a browser rotates its push endpoint and key
  // material on its own schedule, and a row the server kept would then encrypt
  // to a key nobody holds (lib/notify.ts refreshPush).
  useEffect(() => {
    if (pushCap !== "ok") return;
    let live = true;
    void refreshPush();
    void pushStatus().then((s) => {
      if (live) setPush(s);
    });
    return () => {
      live = false;
    };
  }, [pushCap]);

  const enable = async () => {
    const next = await requestNotifications();
    setState(next);
    if (next === "granted") toast.success(t("notify.enabled"));
    else if (next === "denied") toast(t("notify.blocked_help"));
  };

  const togglePush = async () => {
    if (pushBusy) return;
    setPushBusy(true);
    try {
      if (pushOn) {
        await disablePush();
        setPushOn(false);
        toast(t("notify.push_off"));
      } else {
        const res = await enablePush();
        if (res.ok) {
          setPushOn(true);
          setPush((s) => (s ? { ...s, subscriptions: s.subscriptions + 1 } : s));
          toast.success(t("notify.push_on"));
        } else if (res.reason === "blocked") {
          toast(t("notify.blocked_help"));
        } else if (res.reason === "server") {
          toast(t("notify.push_unavailable"));
        } else {
          toast(t("notify.push_hint_unsupported"));
        }
      }
    } finally {
      setPushBusy(false);
    }
  };

  const testPush = async () => {
    if (pushBusy) return;
    setPushBusy(true);
    try {
      const res = await sendTestPush();
      // The server's own count, not a hopeful "sent": a test that reached
      // nothing must say so, which is the only way a user learns their phone
      // dropped the subscription.
      if (res.subscriptions === 0) toast(t("notify.push_test_none"));
      else if (res.sent > 0) toast.success(t("notify.push_test_sent", { count: res.sent }));
      else toast.error(t("notify.push_test_failed"));
    } catch (e) {
      toast.error(String(e));
    } finally {
      setPushBusy(false);
    }
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
        /* Every ornament stays INSIDE the 36 px circle: the unread ring is
           drawn inset and the glow is an inset shadow, because the bar gives
           the button only 6 px of headroom at the app's own top edge — an
           outer ring, an outer glow or a badge hung off the corner is half
           clipped by the shell's `overflow-hidden`, which is exactly the
           "the bell is cut off" report. Nothing here MOVES on a click: the
           press is a background change and the open state is the glyph's own
           brightness pulse (`bell-open`), because a scale or a swing reads as
           the icon shifting under the cursor. */
        className={`relative h-9 w-9 rounded-full border border-border bg-panel/60 backdrop-blur flex items-center justify-center transition-[color,background-color,box-shadow] duration-150 active:bg-white/10 ${
          hot
            ? "text-accent ring-2 ring-inset ring-accent/70 shadow-[inset_0_0_12px_rgb(var(--accent)_/_0.35)]"
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
        <Icon className={`h-4 w-4${open ? " bell-open" : ""}`} />
        {hot && (
          <span className="absolute top-0 right-0 h-4 min-w-4 px-0.5 rounded-full bg-accent text-[10px] font-semibold text-zinc-950 flex items-center justify-center ring-1 ring-panel">
            {unread > 9 ? "9+" : unread}
          </span>
        )}
      </button>
      <Popover
        open={open}
        onClose={() => setOpen(false)}
        align="right"
        // The tray is the widest panel in the app and lives in the top bar,
        // whose column is `overflow-hidden` and `z-10` while the sidebar is
        // `z-20` — anchored in place it was clipped at the column's edge and
        // painted under the sidebar (the "unviewable tray": the text started
        // mid-word). Portaled to <body> and positioned from this button's own
        // rect it stays above everything and inside the viewport — and pinned
        // to the app's own 8 px viewport gutter, because the bell sits in the
        // top bar's padded right cluster: aligning the tray to the BUTTON
        // inherited that padding and left the tray visibly inset from the
        // screen edge (#40).
        fixed
        rightGap={8}
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
        <PushRow
          support={pushCap}
          status={push}
          on={pushOn}
          busy={pushBusy}
          onToggle={() => void togglePush()}
          onTest={() => void testPush()}
        />
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

/** The remote-push controls: whether THIS device can be woken with the app
 *  closed, and the button that proves it.
 *
 *  Prop-driven on purpose — the whole point of this row is what it does NOT
 *  draw. `pushSupport()` (lib/notify.ts) is the honest answer to "can this
 *  client be pushed to at all": the desktop shell has no service worker, an
 *  iOS tab cannot subscribe, a plain-http LAN page has no secure context. In
 *  each of those cases the switch is NOT rendered (a switch we cannot honour
 *  teaches the user that the feature is broken) and one sentence says what the
 *  situation really is. tools/check_push.mjs renders every one of those cases.
 */
export function PushRow({
  support,
  status,
  on,
  busy,
  onToggle,
  onTest,
}: {
  support: PushSupport;
  status: PushStatus | null;
  on: boolean;
  busy: boolean;
  onToggle: () => void;
  onTest: () => void;
}) {
  const { t } = useI18n();
  const unavailable =
    support === "desktop"
      ? t("notify.push_hint_desktop")
      : support === "ios_install"
      ? t("notify.push_hint_ios")
      : support === "ok"
      ? t("notify.push_unavailable")
      : t("notify.push_hint_unsupported");
  if (support !== "ok" || !status?.available) {
    return (
      <div className="px-2.5 pb-1.5 pt-0.5 text-[10px] leading-snug text-zinc-500">
        {unavailable}
      </div>
    );
  }
  return (
    <div className="px-2.5 pb-1.5">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[10px] uppercase tracking-wider text-zinc-500">
          {t("notify.push_title")}
        </span>
        <span className="flex items-center gap-2">
          <span className="text-[11px] text-zinc-400">{t("notify.push_this_device")}</span>
          <button
            role="switch"
            aria-checked={on}
            aria-label={t("notify.push_this_device")}
            disabled={busy}
            title={on ? t("notify.push_off") : t("notify.push_on")}
            onClick={onToggle}
            className={`relative h-5 w-9 shrink-0 rounded-full transition-colors ${
              on ? "bg-accent" : "bg-white/15"
            } ${busy ? "opacity-60" : ""}`}
          >
            <span
              className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-[left] ${
                on ? "left-[18px]" : "left-0.5"
              }`}
            />
          </button>
        </span>
      </div>
      {on && (
        <button
          className="mt-1 w-full text-left text-[11px] px-2 py-1 rounded-md text-accent-soft hover:text-accent hover:bg-white/10 transition-colors tap"
          disabled={busy}
          onClick={onTest}
        >
          <Send className="inline h-3 w-3 mr-1.5 align-[-2px]" />
          {t("notify.push_test")}
        </button>
      )}
    </div>
  );
}
