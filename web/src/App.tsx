import { Suspense, lazy, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Link, Navigate, NavLink, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity, ArrowDownUp, ArrowUpRight, BarChart3, ChevronLeft, ChevronRight, ClipboardCheck, Compass, Disc3, Download, Eye, Gauge, HardDriveDownload, Heart, HeartHandshake, Home, Import,
  Keyboard, Library, ListChecks, ListMusic, Menu, Music2, Music4, PanelLeftClose, Search, Sliders, SlidersHorizontal, Sparkles, Tags, Trash2, User, WifiOff, X,
  Settings as SettingsIcon, Wrench,
} from "lucide-react";
import { api, AuthError, getToken, IN_TAURI, onAuthLost, serverUrl } from "./api";
import type { AuthStatus } from "./api";
import type { Library as LibraryData } from "./types";
import type { LucideIcon } from "lucide-react";
import { albumRef, artistRef, trackRef } from "./lib/refs";
import { toast, useStore } from "./store";
import { withAlias } from "./lib/mbtext";
import CreditsFooter from "./components/Credits";
import AccountMenu from "./components/AccountMenu";
import NotificationBell from "./components/NotificationBell";
import ShortcutsOverlay from "./components/Shortcuts";
import { applyConfigLocale, useI18n, type MessageKey } from "./lib/i18n";
import { applyAccentVars, resolveAccent } from "./lib/accent";
import { publishTransfers } from "./lib/notifications";
import { useJobLocks, type LocksPayload } from "./lib/locks";

// Route-level code splitting: only the landing page ships in the initial
// bundle, every other page is fetched on first visit. Without this the whole
// app (library, player, soulseek, import wizard, settings) loads up front.
const HomePage = lazy(() => import("./pages/HomePage"));
const LibraryPage = lazy(() => import("./pages/LibraryPage"));
const TrashPage = lazy(() => import("./pages/TrashPage"));
const ArtistPage = lazy(() => import("./pages/ArtistPage"));
const AlbumPage = lazy(() => import("./pages/AlbumPage"));
const TrackPage = lazy(() => import("./pages/TrackPage"));
const PlaylistsPage = lazy(() => import("./pages/PlaylistsPage"));
const PlaylistDetailPage = lazy(() => import("./pages/PlaylistDetailPage"));
const FavoritesPage = lazy(() => import("./pages/FavoritesPage"));
const SettingsPage = lazy(() => import("./pages/SettingsPage"));
const SetupPage = lazy(() => import("./pages/SetupPage"));
const SoulseekPage = lazy(() => import("./pages/SoulseekPage"));
const ExportPage = lazy(() => import("./pages/ExportPage"));
const GradingPage = lazy(() => import("./pages/GradingPage"));
const OptimizationPage = lazy(() => import("./pages/OptimizationPage"));
const EqualizerPage = lazy(() => import("./pages/EqualizerPage"));
const DependenciesPage = lazy(() => import("./pages/DependenciesPage"));
const GenrePage = lazy(() => import("./pages/GenrePage"));
const DownloadsPage = lazy(() => import("./pages/DownloadsPage"));
const ImportWizard = lazy(() => import("./pages/ImportWizard"));
const DonationsPage = lazy(() => import("./pages/DonationsPage"));
const InProgressPage = lazy(() => import("./pages/InProgressPage"));
// A podcast SERIES page (route only — podcasts are reached from Home's shelf
// and from an episode's own page, so the sidebar needs no entry for them).
const PodcastPage = lazy(() => import("./pages/PodcastPage"));
const BrowsePage = lazy(() => import("./pages/BrowsePage"));
const DiscoverPage = lazy(() => import("./pages/DiscoverPage"));
const RecommendedPage = lazy(() => import("./pages/RecommendedPage"));
const ChartsPage = lazy(() => import("./pages/ChartsPage"));
const WatchedArtistsPage = lazy(() => import("./pages/WatchedArtistsPage"));
const CheckStackPage = lazy(() => import("./pages/CheckStackPage"));
const MBSearchPage = lazy(() => import("./pages/MusicBrainzPage").then((m) => ({ default: m.MBSearchPage })));
const MBArtistPage = lazy(() => import("./pages/MusicBrainzPage").then((m) => ({ default: m.MBArtistPage })));
const MBReleaseGroupPage = lazy(() => import("./pages/MusicBrainzPage").then((m) => ({ default: m.MBReleaseGroupPage })));
const MBReleasePage = lazy(() => import("./pages/MusicBrainzPage").then((m) => ({ default: m.MBReleasePage })));
const MBRecordingPage = lazy(() => import("./pages/MusicBrainzPage").then((m) => ({ default: m.MBRecordingPage })));
// Not lazy: the sign-in screen is what a signed-out client sees FIRST, and a
// chunk fetch that itself needs the server would be a poor greeting.
import LoginPage from "./pages/LoginPage";
// The client shells (desktop/iOS/Android) get their own first-run wizard: the
// web app is served BY the server it talks to, so it never has to be told
// where that server is — a shell does, and it must be able to change its mind
// later (Settings → Security re-runs this).
import ClientSetup from "./pages/ClientSetup";
import { isClientSetupDone, isClientShell } from "./lib/clientSetup";
import { isOffline, onOfflineFallback } from "./api";
import type { OfflineInfo } from "./api";
// The app event stream's own outcome kinds, and the ONE invalidation a write
// to the library implies (see the subscription below).
import { onAppEvent } from "./lib/notify";
import { affectsLibrary, invalidateLibrary } from "./lib/invalidate";

import PlayerBar from "./components/PlayerBar";
import { ProgressStack } from "./components/ProgressBar";
import { EmptyState, PendingMark } from "./components/Badges";

// Sidebar sections: a long flat list of 14 entries is hard to scan, so the
// rail groups them by what the user is doing (browse / acquire / maintain)
// and renders a label above each group. Collapsed, the labels give way to a
// hairline divider so the rail stays a clean icon column.
/** How long a burst of app events is allowed to gather before the library is
 *  re-read: long enough that an import chain's several outcomes cost one
 *  refetch, short enough that the page is live to the eye. */
const LIVE_LIBRARY_MS = 1500;

const NAV_GROUPS: { labelKey: MessageKey; items: { to: string; labelKey: MessageKey; icon: LucideIcon; end: boolean }[] }[] = [
  {
    labelKey: "nav.group.library",
    items: [
      { to: "/", labelKey: "nav.home", icon: Home, end: true },
      { to: "/library", labelKey: "nav.library", icon: Library, end: false },
      // Complex browsing (tag/rating/technical queries, facets, grouping) is a
      // view OF the library, so it sits with it rather than under Discover.
      { to: "/browse", labelKey: "nav.browse", icon: SlidersHorizontal, end: false },
      { to: "/genres", labelKey: "nav.genres", icon: Tags, end: true },
      { to: "/trash", labelKey: "nav.trash", icon: Trash2, end: true },
      { to: "/playlists", labelKey: "nav.playlists", icon: ListMusic, end: false },
      { to: "/favorites", labelKey: "nav.favorites", icon: Heart, end: false },
      { to: "/downloads", labelKey: "nav.downloads", icon: Download, end: true },
    ],
  },
  {
    labelKey: "nav.group.discover",
    items: [
      { to: "/discover", labelKey: "nav.discover", icon: Compass, end: false },
      { to: "/recommended", labelKey: "nav.recommended", icon: Sparkles, end: false },
      // Charts sits with Discover: the same sources, ranked, over the windows
      // the user picked — and beside them the library's own play history.
      { to: "/charts", labelKey: "nav.charts", icon: BarChart3, end: false },
      // Watched artists are the same queue as Discover: a release group added
      // here is searched, downloaded and imported by the one pipeline.
      { to: "/watched", labelKey: "nav.watched", icon: Eye, end: false },
      { to: "/import", labelKey: "nav.import", icon: Import, end: false },
      { to: "/soulseek", labelKey: "nav.soulseek", icon: ArrowDownUp, end: false },
      // MusicBrainz sits with acquiring: browsing the database IS how a user
      // finds the release they are about to import, and every entity page
      // carries the auto-import and wish actions.
      { to: "/mb/search", labelKey: "nav.musicbrainz", icon: Music4, end: false },
      { to: "/export", labelKey: "nav.export", icon: HardDriveDownload, end: false },
    ],
  },
  {
    labelKey: "nav.group.maintain",
    items: [
      { to: "/optimize", labelKey: "nav.optimize", icon: Gauge, end: false },
      { to: "/grading", labelKey: "nav.grading", icon: ClipboardCheck, end: false },
      // What the library is DOING right now, and what the app is doing it
      // WITH: the two answers a user needs while a run is in flight — which
      // files are locked and by what, and the whole check/script stack the
      // runs are made of, editable in one place.
      { to: "/in-progress", labelKey: "nav.inProgress", icon: Activity, end: false },
      { to: "/checks", labelKey: "nav.checks", icon: ListChecks, end: false },
      { to: "/dependencies", labelKey: "nav.dependencies", icon: Wrench, end: false },
      // The equalizer shapes what the player SOUNDS like, so it sits with the
      // app's other configuration — one page away from Settings, in the sidebar
      // the issue asked for, and one key (`playback_eq_profile`) shared by every
      // client of the server.
      { to: "/equalizer", labelKey: "nav.equalizer", icon: Sliders, end: false },
      { to: "/settings", labelKey: "nav.settings", icon: SettingsIcon, end: false },
      // The donation page sits with the app's own pages rather than in a
      // footer: it is a page like any other, and a user who wants to support
      // the project should not have to hunt for it.
      { to: "/donations", labelKey: "nav.donations", icon: HeartHandshake, end: true },
    ],
  },
];

const COLLAPSE_KEY = "mlo.sidebar.collapsed";

/** The top bar's search answers from the library (the client-side filter over
 *  the payload it already has) or from MusicBrainz (live). The pick is a
 *  device preference, so it lives here rather than in the server config. */
const SEARCH_SOURCE_KEY = "mlo.search.source";
type SearchSource = "local" | "mb";

/** The MusicBrainz page a search hit points at — the secondary link on a
 *  dropdown row (the row itself opens the app's own /mb/… route). */
const mbUrl = (kind: string, id: string) => `https://musicbrainz.org/${kind}/${id}`;

/** The value, `ms` after it stopped changing. MusicBrainz rate-limits a
 *  client to about one request a second, and a query keyed on the raw input
 *  fires one search per keystroke. */
function useSettled<T>(value: T, ms = 400): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setSettled(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return settled;
}

/** A `setTimeout`/`setInterval` handle in this build: a number in the browser
 *  typings, an object under Node's — naming it once keeps the shell's refs and
 *  locals readable and gives the pair one place to change. */
type Timer = ReturnType<typeof setTimeout>;

/** Paint the app's accent. The ONLY caller-facing setter: the shell calls it at
 *  boot with whatever this browser stored, and the settings page calls it when
 *  a swatch or a custom hex is picked. The colour maths, the table of presets
 *  and the write itself live in lib/accent (which also announces the change to
 *  the canvas consumers); this is the app's own entry point into it, kept here
 *  because both the shell and the settings page already import it from App.
 *
 *  `name` is a preset id ("violet") or a custom "#rrggbb"; anything else —
 *  including null, an empty string, a value from an older build — resolves to
 *  the default black & white, never to an unset colour. */
export function applyAccent(name: string | null) {
  applyAccentVars(resolveAccent(name));
}

/** Soulseek availability dot: green = logged into the Soulseek network,
 * amber = slskd running but not logged in, hidden = not running. Sits on
 * the nav icon's corner so it reads the same with the sidebar collapsed.
 * When logged in, `name` carries the account name — shown in the tooltip.
 *
 * `enabled` is false while the shell is behind the login gate: the dot is not
 * rendered there (the sidebar is not), and a client that has not been told
 * where its server is must not poll an address it cannot reach — that poll was
 * one more failed request per 20s on the screen whose whole job is to ask for
 * the address. */
function useSlskDot(enabled: boolean) {
  const { data: st } = useQuery({
    queryKey: ["soulseek", "status-dot"],
    queryFn: api.soulseekStatus,
    enabled,
    refetchInterval: 20000,
    refetchIntervalInBackground: false,
    staleTime: 15000,
    retry: false,
  });
  if (st?.logged_in) {
    // The LIVE account, not the saved username — those drift apart.
    const name = String(st.account || st.username || "").trim() || null;
    return {
      cls: "bg-emerald-500",
      tip: name ? `Soulseek — logged in as ${name}` : "Soulseek — connected",
      name,
    };
  }
  if (st?.conflict) return { cls: "bg-red-500", tip: `Soulseek — ${st.conflict}`, name: null };
  if (st?.running) {
    // The daemon's own words (INVALIDPASS, no credentials…) beat a generic
    // "not logged in" — the dot is the only place some users will look.
    return {
      cls: "bg-amber-400",
      tip: st.error ? `Soulseek — not logged in: ${st.error}` : "Soulseek — running, not logged in",
      name: null,
    };
  }
  return null;
}

function SlskIconDot({ dot }: { dot: { cls: string; tip: string; name?: string | null } | null }) {  if (!dot) return null;
  return (
    <span
      className={`absolute -top-1 -right-1.5 h-2 w-2 rounded-full ${dot.cls} ring-2 ring-panel`}
      title={dot.tip}
    />
  );
}

/** How often the bars check the lock registry for a producer that died without
 *  saying so. ONE timer for the whole stack, and none at all while no bar is
 *  up — never a timer per bar. */
const PROGRESS_SWEEP_MS = 1000;

/** Bars end on the producer's own `progress_end` (the socket handler below).
 *  A producer that dies without one — an older server, a killed run — would
 *  otherwise leave its bar up forever, so this asks the lock registry instead:
 *  a job the server no longer lists, whose numbers have ALSO stopped moving,
 *  is gone. A job that is merely slow still holds its lock, so a stalled
 *  export keeps its bar. */
function useProgressSweep(count: number) {
  // Mounting the poll here keeps the fallback honest wherever the stack is
  // drawn: same query key, so it is the request the player bar already makes.
  useJobLocks();
  const qc = useQueryClient();
  const prune = useStore((s) => s.pruneProgress);
  const live = count > 0;
  useEffect(() => {
    if (!live) return;
    const tick = window.setInterval(() => {
      const payload = qc.getQueryData<LocksPayload>(["jobLocks"]);
      // No answer from the registry yet (a server that is not there): keep
      // every bar — this only ever drops what it can prove is gone.
      if (payload) prune(payload.jobs.map((j) => String(j.job)), Date.now());
    }, PROGRESS_SWEEP_MS);
    return () => window.clearInterval(tick);
  }, [live, prune, qc]);
}

/** The export row's ✕. The server checks its own flag at the next file
 *  boundary and keeps everything already written, so the answer's `cancelled`
 *  is what says a press was taken — the row stays pressed until its bar goes.
 *  A refusal (nothing running, server gone) re-arms the button. */
function cancelExport() {
  return api.exportCancel().then(
    (r) => r.cancelled,
    (err: unknown) => {
      toast.error(String(err));
      return false;
    }
  );
}

/** The live progress bars, with a store subscription of its own.
 *  The relay pushes a progress frame per step — during a chained run that is
 *  many per second — and App re-rendering the whole shell for each of them is
 *  what a phone shows as the page refreshing while the user tries to scroll.
 *  Subscribing here repaints these bars and nothing else; one bar per live
 *  producer is what lets an export and a run be on screen at the same time. */
function LiveProgress() {
  const progresses = useStore((s) => s.progresses);
  const entries = useMemo(() => Object.values(progresses), [progresses]);
  useProgressSweep(entries.length);
  if (!entries.length) return null;
  return (
    <div className="absolute right-4 top-full mt-1 z-40 flex flex-col gap-1">
      <ProgressStack entries={entries} onCancelExport={cancelExport} />
    </div>
  );
}

/** Shown while a lazy route's chunk downloads. Inlined (no spinner
 * library) so the fallback itself is part of the initial bundle. */
function PageLoading() {
  const { t } = useI18n();
  return (
    <div className="p-6 max-w-6xl mx-auto animate-pulse" aria-busy="true" aria-live="polite">
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

/** One typed row in the top-bar search dropdown: a direct in-app link to the
 *  entity, with its kind on the right. A MusicBrainz hit is a link into the
 *  app's own browser (`/mb/…`) exactly like a local hit is a link into the
 *  library — `external` adds musicbrainz.org as a small secondary affordance,
 *  never the primary click. */
function SearchHit({ to, icon: Icon, label, hint, onGo, external, marker }: {
  to: string;
  icon: LucideIcon;
  label: string;
  hint: string;
  onGo: () => void;
  /** musicbrainz.org URL of the same entity, for the escape-hatch icon */
  external?: string;
  /** A state mark the hit carries beside its name — a pending album's dot
   *  (`PendingMark`), so a search result reads as "not downloaded yet" the
   *  same way the library row it opens does. */
  marker?: ReactNode;
}) {
  const body = (
    <>
      <Icon className="h-4 w-4 text-accent-soft shrink-0" />
      <span className="flex-1 min-w-0 truncate">{label}</span>
      {marker}
      <span className="text-[10px] uppercase tracking-wider text-zinc-600 shrink-0">{hint}</span>
    </>
  );
  const cls = "w-full flex items-center gap-2.5 px-3 py-2 text-xs text-zinc-200 hover:bg-raise transition-colors";
  return (
    <div className="flex items-center">
      <Link to={to} onClick={onGo} className={cls}>
        {body}
      </Link>
      {external && (
        <a
          href={external}
          target="_blank"
          rel="noreferrer"
          onClick={onGo}
          title="Open on MusicBrainz"
          className="shrink-0 mr-1.5 p-1.5 rounded-lg text-zinc-600 hover:text-white hover:bg-raise transition-colors"
        >
          <ArrowUpRight className="h-3.5 w-3.5" />
        </a>
      )}
    </div>
  );
}

export default function App() {
  // Subscribed field by field, never as `useStore()`: a selector-less call
  // re-renders App — and App is the ENTIRE shell, every route and the player
  // bar included — on EVERY store write, and the store has writers that fire
  // many times a second (a progress frame per relay tick, a selection toggle
  // per row click, a volume drag). That churn is what a phone shows as the
  // app refreshing under the user's finger, and it is what kills momentum
  // scrolling. The progress readout keeps a subscription of its own, in
  // LiveProgress, so a frame repaints a 40px bar and nothing else.
  const toasts = useStore((s) => s.toasts);
  const dismissToast = useStore((s) => s.dismissToast);
  const query = useStore((s) => s.query);
  const setQuery = useStore((s) => s.setQuery);
  const qc = useQueryClient();

  // LIVE LIBRARY UPDATES. The server announces every settled outcome on
  // /ws/events (lib/notify.ts owns that socket); an outcome nearly always means
  // the library changed under whichever page is open — an import landed, a
  // script wrote tags, a wish was filled, an album was added as pending — so
  // the queries derived from it are dropped NOW, and the Library and Home pages
  // repaint themselves instead of waiting for a visit or a manual Refresh.
  //
  // Coalesced by a short timer: one import chain emits several outcomes in a
  // burst, and the library payload is the biggest one the client asks for, so
  // a burst must cost ONE refetch. The kinds that cannot have changed anything
  // are named in lib/invalidate's QUIET_KINDS.
  const liveTimer = useRef<number | undefined>(undefined);
  useEffect(() => {
    const stop = onAppEvent((kind) => {
      if (!affectsLibrary(kind)) return;
      if (liveTimer.current !== undefined) window.clearTimeout(liveTimer.current);
      liveTimer.current = window.setTimeout(() => {
        liveTimer.current = undefined;
        invalidateLibrary(qc);
      }, LIVE_LIBRARY_MS);
    });
    return () => {
      stop();
      if (liveTimer.current !== undefined) window.clearTimeout(liveTimer.current);
      liveTimer.current = undefined;
    };
  }, [qc]);
  const { t } = useI18n();
  // The shortcut sheet: opened by "?" or the keyboard button in the top bar.
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);

  // ---- the login gate -----------------------------------------------------
  // `signedOut` is set by api.ts when any request comes back 401: the session
  // expired, the server's password changed, or "sign out everywhere" was
  // pressed elsewhere. Re-checking the server's status on that edge is what
  // decides between the login screen and the first-run "claim this server"
  // screen — the page must not guess which one the user needs.
  const [signedOut, setSignedOut] = useState(false);
  const auth = useQuery({
    queryKey: ["auth", "status"],
    // A server that does not answer is not a *failure* to retry into a
    // spinner — it is the answer "no server", and the shell's gate needs it as
    // a VALUE (`null`), not as react-query's internal error state: a rejected
    // query keeps `data` undefined, which is indistinguishable from "still
    // asking", and the client needs the difference.
    queryFn: async (): Promise<AuthStatus | null> => {
      try {
        return await api.authStatus();
      } catch (e) {
        if (e instanceof AuthError) throw e;   // a real 401/428: the server answered
        return null;
      }
    },
    retry: false,
    refetchInterval: 60000,
    refetchIntervalInBackground: false,
  });
  useEffect(() => onAuthLost(() => setSignedOut(true)), []);
  useEffect(() => {
    if (signedOut) void auth.refetch();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signedOut]);
  // Three ways in, and the third is the one a phone hits first: a client shell
  // whose status query FAILED has no server to talk to, and the only screen
  // that can set one is the login page (it owns the server-address field) —
  // without this, a fresh mobile install renders the whole shell with every
  // request erroring and no way to point it anywhere. Once an address answers,
  // the 60s poll resolves `isError` and the gate opens by itself.
  //
  // `!auth.data` is what makes that gate MONOTONE, and it is the difference
  // between a usable app and one that looks like it reloads itself: `isError`
  // goes true for any failed request, so once the server HAD answered, one
  // dropped poll — Wi-Fi hiccup, backend restarting on a config save, a phone
  // coming back from the lock screen — used to tear the entire shell out of
  // the DOM (sidebar, page, player, scroll position) and remount it as this
  // screen, and the next successful poll put it back. A network failure is not
  // a lost session: it must not gate. Only a real answer may — a 401 from the
  // server (`signedOut`, which stays latched until a sign-in), or the server
  // saying it wants a password this client does not have.
  const needsLogin = signedOut
    || (IN_TAURI && auth.data === null)
    || (!!auth.data?.required && !auth.data.authenticated);

  // ---- offline ------------------------------------------------------------
  // `isOffline()` is true while the API is answering from the on-disk cache
  // (see lib/offlineCache.ts): the app keeps working — that is the point of
  // the cache — but the user has to know why the numbers stopped moving. The
  // listener fires only on the offline↔online transition, so this cannot
  // re-render per request.
  const [offline, setOffline] = useState<boolean>(() => isOffline());
  useEffect(() => onOfflineFallback((info: OfflineInfo | null) => setOffline(!!info)), []);

  // Global keyboard shortcuts. The player owns its own transport keys
  // (Space, arrows, brackets — see PlayerBar) and this layer deliberately adds
  // only what belongs to the shell: the fullscreen viewer, the search box and
  // this sheet. Never fires while a field has focus (typing "f" in a search
  // field must type an f), nor under a modifier, nor while the lyrics editor
  // is stamping (it owns the whole keyboard then).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      if (t && (t instanceof HTMLInputElement || t instanceof HTMLTextAreaElement || t.isContentEditable)) return;
      if (document.querySelector("[data-lrc-editor]")) return;
      if (e.key === "f" || e.key === "F") {
        e.preventDefault();
        // The viewer's state lives in the player bar (it owns the decoders);
        // an event keeps the two from fighting over it.
        window.dispatchEvent(new CustomEvent("mlo:fullscreen-toggle"));
      } else if (e.key === "?") {
        e.preventDefault();
        setShortcutsOpen((v) => !v);
      } else if (e.key === "/") {
        e.preventDefault();
        searchRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const { data: config } = useQuery({ queryKey: ["config"], queryFn: api.config });
  const slskDot = useSlskDot(!needsLogin);

  // The server's `ui_locale` is the app-wide language; this browser's own pick
  // (if any) wins, and i18n.ts owns that precedence — this only hands it the
  // config once it arrives.
  useEffect(() => {
    if (config) applyConfigLocale(config as { ui_locale?: string });
  }, [config]);

  // ---- navigation history (top-bar back / forward) ------------------------
  const location = useLocation();
  const navigate = useNavigate();
  // The stack itself lives in a ref (mutations shouldn't re-render), but the
  // position is STATE: a ref-only position never triggers a re-render, so
  // the Back/Forward buttons would render with a stale `disabled` flag.
  const stackRef = useRef<string[]>([location.pathname]);
  const [pos, setPos] = useState(0);
  useEffect(() => {
    // Record navigations into the history stack. Back/forward moves update
    // pos BEFORE navigating, and the router update can land in a separate
    // commit from the setPos — so each pass below reconciles towards the
    // observed path instead of assuming a fresh navigation.
    const stack = stackRef.current;
    if (stack[pos] === location.pathname) return; // already in place
    // the router landed on the entry just behind / ahead of us → that was a
    // back / forward move, just adopt its position
    if (pos > 0 && stack[pos - 1] === location.pathname) {
      setPos(pos - 1);
      return;
    }
    if (pos < stack.length - 1 && stack[pos + 1] === location.pathname) {
      setPos(pos + 1);
      return;
    }
    // otherwise it's a new jump: drop the forward entries and append
    stack.splice(pos + 1);
    stack.push(location.pathname);
    setPos(stack.length - 1);
  }, [location.pathname, pos]);
  const goBack = () => {
    if (pos > 0) {
      setPos(pos - 1);
      navigate(stackRef.current[pos - 1]);
    }
  };
  const goForward = () => {
    if (pos < stackRef.current.length - 1) {
      setPos(pos + 1);
      navigate(stackRef.current[pos + 1]);
    }
  };

  // A finished auto-import is a download the user asked for minutes ago and has
  // long stopped watching, so it lands them in the tagging wizard whatever page
  // they drifted to. Same query key as the auto panel — one poll, react-query
  // keeps the more aggressive interval — and the panel's own toast is untouched.
  //
  // The observer takes the job's STATE alone, as one primitive, and the album
  // path is read from the cache only when the edge actually fires. Observing
  // the raw job (stage text, byte counts, a growing log) re-rendered App on
  // every poll — and App is the whole shell: sidebar, page, player, and the
  // scroll position in `main`. A tree that re-renders under the user's finger
  // every couple of seconds is what "the app keeps refreshing, I cannot scroll"
  // is; a string that only changes on a state change re-renders nothing.
  const { data: autoStateNow } = useQuery({
    queryKey: ["soulseekAuto"],
    queryFn: api.soulseekAutoStatus,
    // No server to ask yet: the login screen owns the screen and the only
    // request it should be making is its own status probe.
    enabled: !needsLogin,
    // 2s while a job is live is the point of the poll — it is how the queue and
    // the confirm prompt advance — and it drops to 15s the moment the job
    // leaves running/confirm, so an idle client is not polling a list at all.
    refetchInterval: (q) => (q.state.data?.state === "running" || q.state.data?.state === "confirm" ? 2000 : 15000),
    refetchIntervalInBackground: false,
    select: (j) => j.state,
  });
  const autoState = useRef<string | null>(null);
  useEffect(() => {
    const st = autoStateNow ?? null;
    const prev = autoState.current;
    autoState.current = st;
    // Only the running/confirm → done EDGE announces. The ref holds that edge
    // to one firing, so a later poll, a re-render or a reload of an old
    // finished job cannot re-announce it.
    if (st !== "done" || (prev !== "running" && prev !== "confirm")) return;
    // A wish handoff also ends "done" — but nothing landed on disk to tag.
    const job = qc.getQueryData<{
      chain?: { running?: boolean } | null;
      result?: { album_path?: string } | null;
    }>(["soulseekAuto"]);
    const album = job?.result?.album_path ?? "";
    if (!album) return;
    // Say it landed, and NOTHING MORE. This used to open the import wizard on
    // whatever page the user was on — the menu, cover and lyrics steps, one
    // after another — which is exactly what a one-press `Add to library` that
    // does the work by itself must not do. Anything a source could not supply
    // arrives as a prompt (the bell, the queue's "Needs you" row, the wizard
    // linked from there), so the screen the user did not ask for is gone.
    //
    // But "done" is the DOWNLOAD's edge, not the pipeline's: `_import` starts
    // the chain thread and returns, so the album is in the library while the
    // scripts are still to come. One sentence used to cover both — "the
    // configured chain has run over it", untrue at that instant. Now it says
    // which of the two it is; the queue's row carries the rest.
    const chainRunning = !!job?.chain?.running;
    toast(`Imported ${album.split(/[\\/]/).filter(Boolean).pop() || album} — it is in your library`
      + (chainRunning ? ` — ${t("queue.chain_running_brief")}` : ""));
  }, [autoStateNow, qc]);

  // Global search lives in the top bar and drives the library filter from
  // anywhere — typing on another page jumps to the library. The dropdown
  // below the input offers the typed local hits.
  const [searchOpen, setSearchOpen] = useState(false);
  // Which of the two searches the box is running. Remembered per device: it is
  // a way of working, not a server setting.
  const [source, setSource] = useState<SearchSource>(() =>
    localStorage.getItem(SEARCH_SOURCE_KEY) === "mb" ? "mb" : "local"
  );
  const pickSource = (next: SearchSource) => {
    setSource(next);
    localStorage.setItem(SEARCH_SOURCE_KEY, next);
  };
  const onSearch = (q: string) => {
    setQuery(q);
    // A MusicBrainz query is not a library filter: typing must not drag the
    // user off the page they are on into a library narrowed by a query that
    // does not describe it.
    if (source === "mb") return;
    if (location.pathname !== "/library") navigate("/library");
  };

  // Typed local results for the top-bar dropdown: artists, albums and tracks
  // with direct links into the library. The library payload is fetched ONLY
  // while the dropdown is open — same query key as the library page, so an
  // already-loaded library costs nothing.
  const q = query.trim().toLowerCase();
  const { data: lib } = useQuery<LibraryData>({
    queryKey: ["library"],
    queryFn: () => api.library(),
    enabled: searchOpen && q.length >= 2,
    staleTime: 30000,
    refetchIntervalInBackground: false,
  });
  const hits = useMemo(() => {
    if (!lib || q.length < 2) return { artists: [], albums: [], tracks: [] };
    const has = (v: string | null | undefined) => (v ?? "").toLowerCase().includes(q);
    const albums = lib.artists.flatMap((a) => a.albums);
    return {
      artists: lib.artists.filter((a) => has(a.display_name) || has(a.name)).slice(0, 4),
      albums: albums.filter((al) => has(al.meta?.ALBUM)).slice(0, 4),
      tracks: albums
        .flatMap((al) => al.tracks.map((t) => ({ al, t })))
        .filter(({ t }) => has(t.tags?.TITLE))
        .slice(0, 5),
    };
  }, [lib, q]);

  // MusicBrainz mode: the SAME two client calls the import wizard's search
  // uses (releases + artists), so a hit here opens exactly the release the
  // wizard would have matched. Live remote calls against a rate-limited
  // service, so they run only while the dropdown is open, only on a settled
  // query, and only from two characters on.
  const mbQuery = useSettled(query.trim());
  const mbOn = searchOpen && source === "mb" && mbQuery.length >= 2;
  const { data: mbReleases = [] } = useQuery({
    queryKey: ["mb-search", "releases", mbQuery],
    queryFn: () => api.mbSearchReleases(mbQuery, "release"),
    enabled: mbOn,
    staleTime: 300000,
    refetchOnWindowFocus: false,
    retry: false,
  });
  const { data: mbArtists = [] } = useQuery({
    queryKey: ["mb-search", "artists", mbQuery],
    queryFn: () => api.mbSearchArtists(mbQuery),
    enabled: mbOn,
    staleTime: 300000,
    refetchOnWindowFocus: false,
    retry: false,
  });
  const mbRows = (Array.isArray(mbReleases) ? mbReleases : []).slice(0, 6);
  const mbArtistRows = (Array.isArray(mbArtists) ? mbArtists : []).slice(0, 4);

  // A pasted musicbrainz.org link is an identity, not a query: it becomes a
  // direct in-app open at the top of the dropdown, and Enter takes it to that
  // page — so a link copied from a browser or a chat lands in the app.
  const mbLink = source === "mb" && query.trim()
    ? /^(?:https?:\/\/)?(?:www\.)?musicbrainz\.org\/(artist|release-group|release|recording)\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i.exec(
        query.trim()
      )
    : null;
  /** Its in-app route — a release group browses at /mb/rg/… . */
  const mbLinkTo = mbLink
    ? `/mb/${mbLink[1] === "release-group" ? "rg" : mbLink[1]}/${mbLink[2]}`
    : null;

  /** Enter opens the artist page when the query IS an artist (exact match, or
   *  the one artist the query narrows to) — otherwise it leaves the already
   *  applied library filter alone. */
  const openOnEnter = () => {
    const exact = hits.artists.find((a) => (a.display_name || a.name).trim().toLowerCase() === q);
    const target = exact ?? (hits.artists.length === 1 ? hits.artists[0] : null);
    if (target) return artistRef(target);
    return null;
  };

  // ---- collapsible sidebar (icons-only rail) ------------------------------
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem(COLLAPSE_KEY) === "1");
  const toggleCollapse = () => {
    const v = !collapsed;
    setCollapsed(v);
    localStorage.setItem(COLLAPSE_KEY, v ? "1" : "0");
  };
  // On phones the rail can't fit — it becomes a hamburger + overlay drawer.
  const [navOpen, setNavOpen] = useState(false);
  const navTriggerRef = useRef<HTMLButtonElement>(null);
  const navCloseRef = useRef<HTMLButtonElement>(null);
  // Esc closes the drawer; focus moves in on open and returns on close.
  useEffect(() => {
    if (!navOpen) return;
    navCloseRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setNavOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      navTriggerRef.current?.focus();
    };
  }, [navOpen]);

  useEffect(() => {
    applyAccent(localStorage.getItem("mlo.accent"));
  }, []);

  useEffect(() => {
    // Nothing to talk to: the login screen is up, which means the shell has no
    // server (or no session). Opening a socket it cannot complete — and worse,
    // restarting that socket on a fixed timer — is request churn on a client
    // that is still being told where its server is.
    if (needsLogin) return;
    // The progress socket must reach the SAME server the API does — the
    // shell's configured address, or this page's own origin. Hardcoding
    // 127.0.0.1:8000 (as this did) left every phone pointed at a LAN server
    // with no progress bars at all.
    const base = serverUrl();
    const wsBase = base
      ? base.replace(/^http/, "ws")
      : `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}`;
    let alive = true;
    let ws: WebSocket | null = null;
    let retry: Timer | undefined;
    let attempt = 0;
    const connect = () => {
      // The token is read on EVERY attempt: a WebSocket handshake cannot carry
      // an Authorization header, so it rides the query string (same rule as
      // /ws/events; see lib/notify.ts) — and a URL built once at mount kept
      // reconnecting with the token a sign-in had just replaced, which the
      // server answers with a 4401 close.
      const token = getToken();
      ws = new WebSocket(`${wsBase}/ws/progress${token ? `?token=${encodeURIComponent(token)}` : ""}`);
      // A completed handshake means the server IS there, whatever happens
      // next: reset the backoff so a healthy connection is never treated as a
      // dead one.
      ws.onopen = () => {
        attempt = 0;
      };
      ws.onmessage = (e) => {
        try {
          const p = JSON.parse(e.data);
          if (p?.type === "soulseek") {
            // The daemon's login/run state changed under us (a login that
            // just landed, an unexpected logout, a port conflict): re-read
            // the status both the tab and the nav dot derive from, so the
            // dot repaints without waiting for the next poll or a reload.
            qc.invalidateQueries({ queryKey: ["soulseek"] });
            qc.invalidateQueries({ queryKey: ["soulseekStatus"] });
            return;
          }
          if (p?.type === "transfers") {
            // Live transfer progress (see server/main.py's transfer watcher).
            // Handed to the store the Soulseek page draws its bars from — NOT
            // invalidated, because a byte count changing four times a second
            // must not refetch anything, and never routed through the
            // notification tray (lib/notifications.ts keeps the two apart).
            publishTransfers(p);
            return;
          }
          if (p?.type === "progress_end") {
            // The producer's own "finished" — the ONE thing that ends its bar.
            // A total-complete frame does NOT: producers publish those
            // mid-job, which is what used to blink the bar out 2.5 s later.
            if (p.job !== undefined && p.job !== null) useStore.getState().dropProgress(p.job);
            return;
          }
          if (typeof p?.done !== "number") return; // ping / non-progress frame
          // Through getState, not a hook value: App does not subscribe to the
          // bars (see LiveProgress), so a frame repaints the bars alone. The
          // frame's own `job` is what keeps two producers on their own rows.
          useStore.getState().setProgress(p);
        } catch {
          /* ignore */
        }
      };
      // The backend restarts on every config save / Soulseek restart /
      // dependency install, so this has to retry — but NOT on a fixed 3s:
      // a client whose server is simply not there (phone off the Wi-Fi, wrong
      // address, backend still booting) opened 20 sockets a minute, forever,
      // each one a connect the phone's radio had to make and tear down, and
      // each one a 4401 close when there was no session. Back off like the
      // event stream does, capped at 30s, and let the retry pick up a
      // re-signed-in token by itself.
      ws.onclose = () => {
        if (!alive) return;
        attempt += 1;
        retry = setTimeout(connect, Math.min(30000, 1000 * 2 ** Math.min(attempt, 5)));
      };
    };
    connect();
    return () => {
      alive = false;
      clearTimeout(retry);
      ws?.close();
    };
    // Re-run when the gate changes: the socket belongs to a signed-in shell,
    // and re-entering one must not wait for the backoff to expire.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [needsLogin]);

  // The gate CLOSING is a server-and-session change, and everything the login
  // screen blocked was fetched while there was nothing to answer — react-query
  // does not refetch on a re-render, so `config` (which decides the first-run
  // wizard) and every page's payload would stay empty until the user happened
  // to navigate. The login screen's own probe can now close the gate with no
  // sign-in click at all (a backend that was still booting when the shell
  // opened), so this cannot rely on the sign-in callback alone.
  const wasGated = useRef(needsLogin);
  useEffect(() => {
    if (wasGated.current && !needsLogin) void qc.invalidateQueries();
    wasGated.current = needsLogin;
  }, [needsLogin, qc]);

  // The shell's own first-run wizard, before every other gate: it needs no
  // server and no token, and it is what asks which server this device talks
  // to. Re-runnable from Settings → Security (which clears the flag).
  if (isClientShell() && !isClientSetupDone()) {
    // A reload rather than an in-place re-render: finishing the wizard changes
    // the API base URL and the token, and module-level state (the event
    // socket, the media-cache keys, every cached query) was built against the
    // OLD server. This is a one-time, user-triggered reload on the client
    // shells only — never a timer.
    return <ClientSetup onDone={() => window.location.reload()} />;
  }

  // The gate comes before the first-run wizard: an unclaimed remote server has
  // no config to show anyone yet, and every route below would answer 428.
  if (needsLogin) {
    return (
      <LoginPage
        onSignedIn={() => {
          setSignedOut(false);
          void auth.refetch();
          void qc.invalidateQueries();
        }}
      />
    );
  }

  // First-run gate: setup not completed → setup wizard. Only first_run_done
  // is checked — an empty music folder is a normal unconfigured state, so the
  // setup page's "Skip for now" leaves Settings (and the app) reachable.
  if (config && !config.first_run_done) {
    return (
      <Routes>
        <Route path="/setup" element={<SetupPage />} />
        <Route path="*" element={<Navigate to="/setup" replace />} />
      </Routes>
    );
  }

  return (
    // The sidebar owns the entire left edge, top to bottom (brand header, nav,
    // footer); the top bar, content and player bar all live in the column to
    // its right. `.safe-shell` carries the notch / home-indicator insets for
    // everything inside it at once (index.html sets `viewport-fit=cover`, so
    // without it the chrome paints under them).
    <div className="safe-shell h-dvh overflow-hidden bg-bg text-zinc-100 flex">
      <aside
        className={`${collapsed ? "w-14" : "w-48"} hidden md:flex h-full shrink-0 border-r border-border bg-panel p-2 flex-col gap-1 overflow-y-auto transition-[width] duration-150 relative z-20`}
      >
        {/* sidebar header: brand + collapse toggle, split from the nav by a
            hairline. Collapses to a stacked icon rail. Every metric here is
            the SAME in both states on purpose: the logo button used to gain
            p-0.5 only while collapsed, which made the header 4px taller and
            pushed the whole nav below it down (the icon jump). */}
        <div className="flex items-center gap-2 border-b border-border pb-2 mb-1 shrink-0 px-1.5">
          {/* the logo toggles the rail in both directions, so the header never
              rearranges (no column flip, no icon jump) */}
          <button
            className="shrink-0 rounded-md cursor-pointer hover:bg-raise p-0.5 -ml-0.5"
            onClick={toggleCollapse}
            title={collapsed ? t("sidebar.expand") : t("sidebar.collapse")}
          >
            <img
              src="/icon.png"
              alt="la musica"
              className="h-7 w-7 rounded-md object-cover ring-1 ring-border shadow-sm shrink-0"
            />
          </button>
          <span
            className={`flex-1 overflow-hidden whitespace-nowrap font-bold tracking-tight text-sm transition-[max-width,opacity] duration-150 ${
              collapsed ? "max-w-0 opacity-0" : "max-w-[110px] opacity-100"
            }`}
          >
            la musica
          </span>
          <span
            className={`overflow-hidden shrink-0 transition-[max-width,opacity] duration-150 ${
              collapsed ? "max-w-0 opacity-0" : "max-w-[40px] opacity-100"
            }`}
          >
            <button
              className="p-1.5 rounded-lg text-zinc-500 hover:text-white hover:bg-raise transition-colors"
              onClick={toggleCollapse}
              title={t("sidebar.collapse")}
            >
              <PanelLeftClose className="h-4 w-4" />
            </button>
          </span>
        </div>
        {NAV_GROUPS.map((group, gi) => (
          <div key={group.labelKey} className="flex flex-col gap-1">
            {/* Section label: one box in BOTH states, so collapsing never
                moves an icon — collapsed it only turns invisible, and
                truncates because the narrower rail must not rewrap the name
                into a taller box. A group after the first then swaps its name
                for a hairline; the FIRST group keeps nothing, because the
                header's own border-b is already the rule above the nav (a
                hairline here stacked a second line 13px under it). */}
            <div
              className={`relative px-3 pt-2 pb-0.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-600 ${
                collapsed ? "invisible truncate" : ""
              }`}
            >
              {t(group.labelKey)}
              {collapsed && gi > 0 && (
                <div className="visible absolute inset-x-2 top-1/2 -translate-y-1/2 border-t border-border/60" />
              )}
            </div>
            {group.items.map(({ to, labelKey, icon: Icon, end }) => (
              <NavLink
                key={to}
                to={to}
                end={end}
                title={collapsed ? t(labelKey) : undefined}
                className={({ isActive }) =>
                  // Monochrome-style: the active entry is a solid accent block
                  // with contrast text; inactive ones stay quiet. The label
                  // collapses via max-width so the icon glides with the
                  // shrinking sidebar instead of jumping to a new layout.
                  // nav-link adds the compositor-only hover nudge (CSS).
                  `nav-link flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm border ${
                    isActive
                      ? "bg-accent on-accent font-semibold border-transparent shadow-sm"
                      : "text-zinc-400 hover:text-white hover:bg-raise border-transparent"
                  }`
                }
              >
                <span className="nav-icon relative shrink-0 inline-flex">
                  <Icon className="h-4 w-4 shrink-0" />
                  {to === "/soulseek" && <SlskIconDot dot={slskDot} />}
                </span>
                <span
                  className={`overflow-hidden whitespace-nowrap text-ellipsis transition-[max-width,opacity] duration-150 ${
                    collapsed ? "max-w-0 opacity-0" : "max-w-[110px] opacity-100"
                  }`}
                >
                  {/* the tab always reads "Soulseek"; the account name is in the dot tooltip */}
                  {t(labelKey)}
                </span>
              </NavLink>
            ))}
          </div>
        ))}
        {/* Credits live in the corner where the app can always show them:
            every service it queries, as links out to the project. */}
        <div className="mt-auto">
          <CreditsFooter collapsed={collapsed} />
        </div>
      </aside>

      {/* phone nav drawer: the rail's content as a full overlay, opened from
          the header hamburger; every link closes it */}
      {navOpen && (
        <>
          <div className="anim-fade fixed inset-0 z-40 bg-black/50 md:hidden" onClick={() => setNavOpen(false)} />
          <aside
            role="dialog"
            aria-modal="true"
            aria-label={t("topbar.menu_open")}
            className="safe-drawer anim-pop fixed left-0 top-0 bottom-0 z-50 w-52 bg-panel border-r border-border p-2 flex flex-col gap-1 overflow-y-auto overscroll-contain md:hidden shadow-2xl"
          >
            <div className="flex items-center gap-2 border-b border-border pb-2 mb-1 px-1">
              <img src="/icon.png" alt="la musica" className="h-7 w-7 rounded-md object-cover ring-1 ring-border shadow-sm" />
              <span className="flex-1 overflow-hidden whitespace-nowrap font-bold tracking-tight text-sm">la musica</span>
              <button
                ref={navCloseRef}
                className="tap-hit p-1.5 rounded-lg text-zinc-500 hover:text-white hover:bg-raise transition-colors"
                onClick={() => setNavOpen(false)}
                title={t("topbar.menu_close")}
                aria-label={t("topbar.menu_close")}
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            {NAV_GROUPS.map((group) => (
              <div key={group.labelKey} className="flex flex-col gap-1">
                <div className="px-3 pt-2 pb-0.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-600">
                  {t(group.labelKey)}
                </div>
                {group.items.map(({ to, labelKey, icon: Icon, end }) => (
                  <NavLink
                    key={to}
                    to={to}
                    end={end}
                    onClick={() => setNavOpen(false)}
                    className={({ isActive }) =>
                      `nav-link flex items-center gap-2.5 rounded-lg px-3 py-3 text-sm border ${
                        isActive
                          ? "bg-accent on-accent font-semibold border-transparent shadow-sm"
                          : "text-zinc-400 hover:text-white hover:bg-raise border-transparent"
                      }`
                    }
                  >
                    <span className="nav-icon relative shrink-0 inline-flex">
                      <Icon className="h-4 w-4 shrink-0" />
                      {to === "/soulseek" && <SlskIconDot dot={slskDot} />}
                    </span>
                    <span className="whitespace-nowrap">{t(labelKey)}</span>
                  </NavLink>
                ))}
              </div>
            ))}
            <div className="mt-auto">
              <CreditsFooter />
            </div>
          </aside>
        </>
      )}

      <div className="flex-1 min-w-0 flex flex-col overflow-hidden relative z-10">
        {/* floating top bar: transparent overlay on the content — only the
            controls themselves catch the pointer. The spacing tightens on a
            phone, where this bar shares its width with the drawer and the
            search field: six controls at desktop spacing left the input about
            100px of a 390px screen. The icon buttons keep their 36px box (a
            44px box would not fit a 48px bar) and take `.tap-hit` instead,
            which grows the touch area without moving anything. */}
        <header className="absolute inset-x-0 top-0 h-12 z-30 flex items-center gap-1.5 sm:gap-3 px-2 sm:px-4 pointer-events-none">
          <div className="flex items-center gap-1.5 shrink-0 pointer-events-auto">
            <button
              ref={navTriggerRef}
              className="tap-hit h-9 w-9 rounded-full border border-border bg-panel/60 backdrop-blur flex md:hidden items-center justify-center text-zinc-300 hover:text-white hover:border-accent/50 transition-colors"
              onClick={() => setNavOpen(true)}
              title={t("topbar.menu")}
              aria-label={t("topbar.menu_open")}
              aria-expanded={navOpen}
            >
              <Menu className="h-4 w-4" />
            </button>
            <button
              className="tap-hit h-9 w-9 rounded-full border border-border bg-panel/60 backdrop-blur flex items-center justify-center text-zinc-300 hover:text-white hover:border-accent/50 disabled:opacity-30 disabled:pointer-events-none transition-colors"
              onClick={goBack}
              disabled={pos === 0}
              title={t("topbar.back")}
            >
              <ChevronLeft className="h-4 w-4" />
            </button>
            <button
              className="tap-hit h-9 w-9 rounded-full border border-border bg-panel/60 backdrop-blur hidden sm:flex items-center justify-center text-zinc-300 hover:text-white hover:border-accent/50 disabled:opacity-30 disabled:pointer-events-none transition-colors"
              onClick={goForward}
              disabled={pos >= stackRef.current.length - 1}
              title={t("topbar.forward")}
            >
              <ChevronRight className="h-4 w-4" />
            </button>
          </div>
          {/* Which search the box runs. A native <select>: it is keyboard
              reachable and screen-reader labelled for free, and it is the
              control every platform already knows how to open. Kept narrow on
              a phone (the bar has to hold the input too) — the placeholder and
              the title both say which mode is active, so a clipped option
              label is never the only clue. */}
          <select
            className="input tap-hit h-9 w-[5.5rem] sm:w-[7.5rem] shrink-0 !py-0 !px-2 text-[11px] sm:text-xs !bg-panel/60 backdrop-blur cursor-pointer pointer-events-auto"
            aria-label={t("topbar.search_source")}
            title={source === "mb" ? t("topbar.search_mb_hint") : t("topbar.search_local_hint")}
            value={source}
            onChange={(e) => pickSource(e.target.value as SearchSource)}
          >
            <option value="local">{t("topbar.search_local")}</option>
            <option value="mb">{t("topbar.search_mb")}</option>
          </select>
          {/* the search input spans the rest of the bar. `.search-field` is a
              query container: index.css drops the magnifier's inset when the
              field itself gets too narrow to say anything (phone widths, and
              any app zoom — see the rule). */}
          <div className="search-field relative flex-1 pointer-events-auto">
            {/* Above the input, not under it: the input paints its own
                translucent panel background and comes LATER in the DOM, so a
                positioned icon with no z-index sat behind it — the field then
                read as a 40px gap before the placeholder, with nothing in it
                (the owner's report). `pointer-events-none` keeps the click on
                the field where it belongs. */}
            <Search className="absolute left-3.5 top-1/2 -translate-y-1/2 h-4 w-4 text-zinc-500 z-10 pointer-events-none" />
            <input
              ref={searchRef}
              className="input !py-2 !pl-10 text-xs w-full !bg-panel/60 backdrop-blur"
              placeholder={source === "mb" ? t("topbar.search_mb_placeholder") : t("topbar.search")}
              title={
                source === "mb"
                  ? t("topbar.search_mb_hint")
                  : "Tag-scoped search: composer:name · person:name (any credit) · genre:metal · tag:anything — quotes keep spaces · press / to jump here"
              }
              value={query}
              onChange={(e) => onSearch(e.target.value)}
              onFocus={() => setSearchOpen(true)}
              onBlur={() => setTimeout(() => setSearchOpen(false), 150)}
              onKeyDown={(e) => {
                if (e.key !== "Enter" || !query.trim()) return;
                e.preventDefault();
                setSearchOpen(false);
                // MusicBrainz mode: a pasted entity link opens that entity,
                // anything else is a search of the in-app browser (which is
                // where its results are read and downloaded from). Local mode
                // prefers the artist the query names; otherwise it leaves the
                // library filter that typing already applied alone.
                if (source === "mb") {
                  navigate(mbLinkTo ?? `/mb/search?q=${encodeURIComponent(query.trim())}`);
                  return;
                }
                const artistTo = openOnEnter();
                if (artistTo) navigate(artistTo);
              }}
            />
            {searchOpen && query.trim() && (source === "local" || !!mbLinkTo || mbRows.length > 0 || mbArtistRows.length > 0) && (
              <div className="anim-fade absolute left-0 right-0 top-full mt-1 z-40 rounded-lg border border-border bg-zinc-950/95 backdrop-blur shadow-xl overflow-hidden max-h-[70vh] overflow-y-auto">
                {source === "local" ? (
                  <>
                    {hits.artists.map((a) => (
                      <SearchHit
                        key={a.path}
                        to={artistRef(a)}
                        icon={User}
                        label={a.display_name || a.name}
                        hint={t("page.artist")}
                        onGo={() => setSearchOpen(false)}
                      />
                    ))}
                    {hits.albums.map((al) => (
                      <SearchHit
                        key={al.path}
                        to={albumRef(al)}
                        icon={Disc3}
                        label={al.meta?.ALBUM ?? al.path}
                        hint={t("page.album")}
                        marker={<PendingMark album={al} />}
                        onGo={() => setSearchOpen(false)}
                      />
                    ))}
                    {hits.tracks.map(({ al, t: tr }) => (
                      <SearchHit
                        key={tr.path}
                        to={trackRef(tr)}
                        icon={Music2}
                        label={tr.tags?.TITLE ?? tr.file}
                        hint={al.album_artist || t("page.track")}
                        onGo={() => setSearchOpen(false)}
                      />
                    ))}
                  </>
                ) : (
                  <>
                    {/* A pasted entity link first (it is an identity, not a
                        query), then artists, then releases. Every row opens
                        the app's own page; the icon on the right is the
                        escape hatch to musicbrainz.org. */}
                    {mbLinkTo && mbLink && (
                      <SearchHit
                        key="pasted"
                        to={mbLinkTo}
                        icon={Music4}
                        label={query.trim()}
                        hint={mbLink[1].replace("-", " ")}
                        onGo={() => setSearchOpen(false)}
                        external={mbUrl(mbLink[1] === "release-group" ? "release-group" : mbLink[1], mbLink[2])}
                      />
                    )}
                    {mbArtistRows.map((a: { id: string; name: string; type?: string; alias?: string }) => (
                      <SearchHit
                        key={`a-${a.id}`}
                        to={`/mb/artist/${a.id}`}
                        icon={User}
                        label={withAlias(a.name, a.alias)}
                        hint={a.type || t("page.artist")}
                        onGo={() => setSearchOpen(false)}
                        external={mbUrl("artist", a.id)}
                      />
                    ))}
                    {mbRows.map((h: { id: string; title: string; artist?: string; date?: string; alias?: string }) => (
                      <SearchHit
                        key={`r-${h.id}`}
                        to={`/mb/release/${h.id}`}
                        icon={Disc3}
                        label={withAlias(h.title || h.id, h.alias)}
                        hint={h.artist || h.date || ""}
                        onGo={() => setSearchOpen(false)}
                        external={mbUrl("release", h.id)}
                      />
                    ))}
                  </>
                )}
              </div>
            )}
          </div>
          {/* The session's own controls sit at the bar's top RIGHT: the left
              is navigation (menu, back, forward) and the middle is the search,
              so this is where a user looks for status and help. The bell is
              also what opens the /ws/events socket (lib/notify.ts); the pill
              and the shortcuts button hide on a phone, where the bar shares
              its width with the input. */}
          <div className="flex items-center gap-1.5 sm:gap-3 shrink-0 pointer-events-auto">
            {/* Offline: the app is answering from its own cache, so it stays
                usable with the server gone — but the user must be able to tell
                "nothing changed" from "nothing can reach me". WifiOff is a
                lucide icon; the pill is deliberately quiet (this is a state,
                not an error). */}
            {offline && (
              <span
                className="h-9 px-2.5 rounded-full border border-amber-900/60 bg-amber-950/40 backdrop-blur hidden sm:flex items-center gap-1.5 text-[11px] text-amber-200/90"
                title={t("offline.help")}
              >
                <WifiOff className="h-3.5 w-3.5" />
                {t("offline.label")}
              </span>
            )}
            <button
              className="tap-hit h-9 w-9 rounded-full border border-border bg-panel/60 backdrop-blur hidden sm:flex items-center justify-center text-zinc-300 hover:text-white hover:border-accent/50 transition-colors"
              onClick={() => setShortcutsOpen(true)}
              title={t("topbar.shortcuts") + " (?)"}
              aria-label={t("topbar.shortcuts")}
            >
              <Keyboard className="h-4 w-4" />
            </button>
            <NotificationBell />
            {/* Who you are signed in as, and the switch to someone else. Last
                in the row: the rightmost control of the bar is the account. */}
            <AccountMenu />
          </div>
          {/* live script progress floats below the bar so the search keeps
              the full width — its own component, so a progress frame does not
              re-render the shell (see LiveProgress) */}
          <LiveProgress />
        </header>

        <main className="flex-1 overflow-auto overscroll-contain min-w-0 pt-12">
          {/* keyed by pathname so each navigation eases the new page in */}
          <div key={location.pathname} className="page-enter">
            {/* lazy routes: the page chunk is fetched on first visit */}
            <Suspense fallback={<PageLoading />}>
            <Routes>
            <Route path="/" element={<HomePage />} />
            <Route path="/library" element={<LibraryPage />} />
            <Route path="/genres" element={<GenrePage />} />
            {/* The offline downloads: the tracks this browser can play with
                the server down, as the library's own album table. */}
            <Route path="/downloads" element={<DownloadsPage />} />
            <Route path="/trash" element={<TrashPage />} />
            <Route path="/artist/:path" element={<ArtistPage />} />
            <Route path="/podcast/:series" element={<PodcastPage />} />
            <Route path="/album/:path" element={<AlbumPage />} />
            <Route path="/track/:path" element={<TrackPage />} />
            <Route path="/playlists" element={<PlaylistsPage />} />
            <Route path="/playlist/:id" element={<PlaylistDetailPage />} />
            <Route path="/favorites" element={<Navigate to="/favorites/tracks" replace />} />
            <Route path="/favorites/:kind" element={<FavoritesPage />} />
            <Route path="/soulseek" element={<SoulseekPage />} />
            <Route path="/export" element={<ExportPage />} />
            <Route path="/optimize" element={<OptimizationPage />} />
            <Route path="/equalizer" element={<EqualizerPage />} />
            <Route path="/grading" element={<GradingPage />} />
            <Route path="/in-progress" element={<InProgressPage />} />
            <Route path="/browse" element={<BrowsePage />} />
            <Route path="/discover" element={<DiscoverPage />} />
            <Route path="/recommended" element={<RecommendedPage />} />
            {/* What is being played: the user's own play history beside the
                providers' charts, one window at a time. */}
            <Route path="/charts" element={<ChartsPage />} />
            <Route path="/watched" element={<WatchedArtistsPage />} />
            <Route path="/checks" element={<CheckStackPage />} />
            <Route path="/dependencies" element={<DependenciesPage />} />
            <Route path="/import" element={<ImportWizard />} />
            <Route path="/settings" element={<SettingsPage />} />
            {/* MusicBrainz browser: search + the four entity pages. Reachable
                from the sidebar, from the top bar's MusicBrainz mode, and from
                a pasted musicbrainz.org link. */}
            <Route path="/mb" element={<Navigate to="/mb/search" replace />} />
            <Route path="/mb/search" element={<MBSearchPage />} />
            <Route path="/mb/artist/:id" element={<MBArtistPage />} />
            <Route path="/mb/rg/:id" element={<MBReleaseGroupPage />} />
            <Route path="/mb/release/:id" element={<MBReleasePage />} />
            <Route path="/mb/recording/:id" element={<MBRecordingPage />} />
            {/* Re-runnable: the wizard is the app's setup surface, not a
                one-shot gate — Settings → General opens it again. Every step
                is skippable and it only writes what is entered there. */}
            <Route path="/setup" element={<SetupPage />} />
            <Route path="/donations" element={<DonationsPage />} />
            <Route
              path="*"
              element={
                /* The app's own empty state, which is the affordance it was
                   built for: a 404 is a dead end, so it gets the same glyph,
                   the same voice and a way back to the library — the bare
                   "Page not found — Library" line sat 40 px under the top of an
                   otherwise empty pane, which read as a page that failed to
                   load rather than as a route that does not exist. */
                <div className="p-6">
                  <EmptyState
                    title={t("page.not_found")}
                    hint="This address does not lead to a page in the app."
                    action={{ label: t("nav.library"), to: "/library" }}
                  />
                </div>
              }
            />
          </Routes>
            </Suspense>
          </div>
        </main>

        <PlayerBar />
      </div>

      {shortcutsOpen && <ShortcutsOverlay onClose={() => setShortcutsOpen(false)} />}

      {/* Toasts stack instead of overwriting each other; errors are red and
          announce as alerts, confirmations are green and polite. */}
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
    </div>
  );
}
