import { useEffect, useRef, useState } from "react";
import { Navigate, NavLink, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowDownUp, ChevronLeft, ChevronRight, ClipboardCheck, Gauge, HardDriveDownload, Heart, Import,
  Library, ListMusic, Menu, Music4, PanelLeftClose, Search, X,
  Settings as SettingsIcon, Wrench,
} from "lucide-react";
import { api } from "./api";
import { useStore } from "./store";
import LibraryPage from "./pages/LibraryPage";
import ArtistPage from "./pages/ArtistPage";
import AlbumPage from "./pages/AlbumPage";
import TrackPage from "./pages/TrackPage";
import PlaylistsPage from "./pages/PlaylistsPage";
import PlaylistDetailPage from "./pages/PlaylistDetailPage";
import FavoritesPage from "./pages/FavoritesPage";
import SettingsPage from "./pages/SettingsPage";
import SetupPage from "./pages/SetupPage";
import SoulseekPage from "./pages/SoulseekPage";
import ExportPage from "./pages/ExportPage";
import GradingPage from "./pages/GradingPage";
import OptimizationPage from "./pages/OptimizationPage";
import DependenciesPage from "./pages/DependenciesPage";
import {
  MBSearchPage, MBArtistPage, MBReleaseGroupPage, MBReleasePage, MBRecordingPage,
} from "./pages/MusicBrainzPage";
import PlayerBar from "./components/PlayerBar";
import ImportWizard from "./pages/ImportWizard";
import { ProgressInline } from "./components/ProgressBar";

const NAV = [
  { to: "/", label: "Library", icon: Library, end: true },
  { to: "/playlists", label: "Playlists", icon: ListMusic, end: false },
  { to: "/favorites", label: "Favorites", icon: Heart, end: false },
  { to: "/import", label: "Import", icon: Import, end: false },
  { to: "/soulseek", label: "Soulseek", icon: ArrowDownUp, end: false },
  { to: "/export", label: "Export", icon: HardDriveDownload, end: false },
  { to: "/optimize", label: "Optimization", icon: Gauge, end: false },
  { to: "/grading", label: "Grading", icon: ClipboardCheck, end: false },
  { to: "/dependencies", label: "Dependencies", icon: Wrench, end: false },
  { to: "/settings", label: "Settings", icon: SettingsIcon, end: false },
];

const COLLAPSE_KEY = "mlo.sidebar.collapsed";

const ACCENTS: Record<string, [string, string, string]> = {
  violet: ["139 92 246", "167 139 250", "255 255 255"],
  pink: ["236 72 153", "249 168 212", "255 255 255"],
  emerald: ["16 185 129", "110 231 183", "255 255 255"],
  sky: ["14 165 233", "125 211 252", "255 255 255"],
  amber: ["245 158 11", "252 211 77", "24 24 27"],
  red: ["239 68 68", "252 165 165", "255 255 255"],
  mono: ["255 255 255", "212 212 216", "9 9 11"],
};

export function applyAccent(name: string | null) {
  const [accent, soft, fg] = ACCENTS[name ?? "mono"] ?? ACCENTS.mono;
  document.documentElement.style.setProperty("--accent", accent);
  document.documentElement.style.setProperty("--accent-soft", soft);
  document.documentElement.style.setProperty("--accent-fg", fg);
}

/** Soulseek availability dot: green = logged into the Soulseek network,
 * amber = slskd running but not logged in, hidden = not running. Sits on
 * the nav icon's corner so it reads the same with the sidebar collapsed.
 * When logged in, `name` carries the account name — the tab then shows it
 * instead of the plain "Soulseek" label. */
function useSlskDot() {
  const { data: st } = useQuery({
    queryKey: ["soulseek", "status-dot"],
    queryFn: api.soulseekStatus,
    refetchInterval: 20000,
    staleTime: 15000,
    retry: false,
  });
  if (st?.logged_in) {
    const name = String(st.username ?? "").trim() || null;
    return {
      cls: "bg-emerald-500",
      tip: name ? `Soulseek — logged in as ${name}` : "Soulseek — connected",
      name,
    };
  }
  if (st?.running) return { cls: "bg-amber-400", tip: "Soulseek — running, not logged in", name: null };
  return null;
}

function SlskIconDot({ dot }: { dot: { cls: string; tip: string; name?: string | null } | null }) {
  if (!dot) return null;
  return (
    <span
      className={`absolute -top-1 -right-1.5 h-2 w-2 rounded-full ${dot.cls} ring-2 ring-panel`}
      title={dot.tip}
    />
  );
}

export default function App() {
  const { progress, setProgress, toast: toastMsg, query, setQuery } = useStore();
  const progressClear = useRef<ReturnType<typeof setTimeout> | null>(null);
  const { data: config } = useQuery({ queryKey: ["config"], queryFn: api.config });
  const slskDot = useSlskDot();

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

  // Global search lives in the top bar and drives the library filter from
  // anywhere — typing on another page jumps to the library. The dropdown
  // below the input offers the MusicBrainz browser (and recognizes pasted
  // musicbrainz.org links).
  const [searchOpen, setSearchOpen] = useState(false);
  const onSearch = (q: string) => {
    setQuery(q);
    if (location.pathname !== "/") navigate("/");
  };
  const goMbSearch = () => {
    setSearchOpen(false);
    navigate(`/mb/search?q=${encodeURIComponent(query.trim())}`);
  };
  const mbLink = /^(?:https?:\/\/)?(?:www\.)?musicbrainz\.org\/(artist|release-group|release|recording)\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i.exec(
    query.trim()
  );

  // ---- collapsible sidebar (icons-only rail) ------------------------------
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem(COLLAPSE_KEY) === "1");
  const toggleCollapse = () => {
    const v = !collapsed;
    setCollapsed(v);
    localStorage.setItem(COLLAPSE_KEY, v ? "1" : "0");
  };
  // On phones the rail can't fit — it becomes a hamburger + overlay drawer.
  const [navOpen, setNavOpen] = useState(false);

  useEffect(() => {
    applyAccent(localStorage.getItem("mlo.accent"));
  }, []);

  useEffect(() => {
    const inTauri = !!(window as any).__TAURI_INTERNALS__;
    // window.location (not the router's location object) — this is a URL.
    const wsBase = inTauri ? "ws://127.0.0.1:8000" : `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}`;
    const ws = new WebSocket(`${wsBase}/ws/progress`);
    ws.onmessage = (e) => {
      try {
        const p = JSON.parse(e.data);
        if (typeof p?.done !== "number") return; // ping / non-progress frame
        setProgress(p);
        // The relay never sends an explicit "finished" frame — clear the
        // indicator shortly after the bar completes.
        if (progressClear.current) clearTimeout(progressClear.current);
        if (p.total && p.done >= p.total) {
          progressClear.current = setTimeout(() => setProgress(null), 2500);
        }
      } catch {
        /* ignore */
      }
    };
    return () => {
      ws.close();
      if (progressClear.current) clearTimeout(progressClear.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // First-run gate: no music folder (or setup not completed) → setup wizard.
  if (config && (!String(config.music_folder ?? "").trim() || !config.first_run_done)) {
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
    // its right.
    <div className="h-screen overflow-hidden bg-bg text-zinc-100 flex">
      <aside
        className={`${collapsed ? "w-14" : "w-48"} hidden md:flex h-full shrink-0 border-r border-border bg-panel p-2 flex-col gap-1 overflow-y-auto transition-[width] duration-150 relative z-20`}
      >
        {/* sidebar header: brand + collapse toggle, split from the nav by a
            hairline. Collapses to a stacked icon rail. */}
        <div
          className={`flex items-center gap-2 border-b border-border pb-2 mb-1 shrink-0 transition-[padding] duration-150 ${
            collapsed ? "px-1.5" : "px-1"
          }`}
        >
          {/* the logo itself expands the sidebar when collapsed, so the
              header never rearranges (no column flip, no icon jump) */}
          <button
            className={`shrink-0 rounded-md ${collapsed ? "cursor-pointer hover:bg-raise p-0.5 -ml-0.5" : "cursor-default"}`}
            onClick={() => collapsed && toggleCollapse()}
            title={collapsed ? "Expand sidebar" : undefined}
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
              title="Collapse sidebar"
            >
              <PanelLeftClose className="h-4 w-4" />
            </button>
          </span>
        </div>
        {NAV.map(({ to, label, icon: Icon, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            title={collapsed ? label : undefined}
            className={({ isActive }) =>
              // Monochrome-style: the active entry is a solid accent block
              // with contrast text; inactive ones stay quiet. The label
              // collapses via max-width so the icon glides with the
              // shrinking sidebar instead of jumping to a new layout.
              `flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm transition-colors border ${
                isActive
                  ? "bg-accent on-accent font-semibold border-transparent shadow-sm"
                  : "text-zinc-400 hover:text-white hover:bg-raise border-transparent"
              }`
            }
          >
            <span className="relative shrink-0 inline-flex">
              <Icon className="h-4 w-4 shrink-0" />
              {to === "/soulseek" && <SlskIconDot dot={slskDot} />}
            </span>
            <span
              className={`overflow-hidden whitespace-nowrap text-ellipsis transition-[max-width,opacity] duration-150 ${
                collapsed ? "max-w-0 opacity-0" : "max-w-[110px] opacity-100"
              }`}
            >
              {/* the Soulseek tab shows the logged-in account name */}
              {to === "/soulseek" && slskDot?.name ? slskDot.name : label}
            </span>
          </NavLink>
        ))}
        {!collapsed && (
          <div className="mt-auto text-[10px] text-zinc-600 px-3 pb-2">
            Grading · Auditing · Optimization
            <br />
            MusicBrainz · LRCLIB · RYM
          </div>
        )}
      </aside>

      {/* phone nav drawer: the rail's content as a full overlay, opened from
          the header hamburger; every link closes it */}
      {navOpen && (
        <>
          <div className="fixed inset-0 z-40 bg-black/50 md:hidden" onClick={() => setNavOpen(false)} />
          <aside className="fixed left-0 top-0 bottom-0 z-50 w-52 bg-panel border-r border-border p-2 flex flex-col gap-1 overflow-y-auto md:hidden shadow-2xl">
            <div className="flex items-center gap-2 border-b border-border pb-2 mb-1 px-1">
              <img src="/icon.png" alt="la musica" className="h-7 w-7 rounded-md object-cover ring-1 ring-border shadow-sm" />
              <span className="flex-1 overflow-hidden whitespace-nowrap font-bold tracking-tight text-sm">la musica</span>
              <button
                className="p-1.5 rounded-lg text-zinc-500 hover:text-white hover:bg-raise transition-colors"
                onClick={() => setNavOpen(false)}
                title="Close menu"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            {NAV.map(({ to, label, icon: Icon, end }) => (
              <NavLink
                key={to}
                to={to}
                end={end}
                onClick={() => setNavOpen(false)}
                className={({ isActive }) =>
                  `flex items-center gap-2.5 rounded-lg px-3 py-2.5 text-sm transition-colors border ${
                    isActive
                      ? "bg-accent on-accent font-semibold border-transparent shadow-sm"
                      : "text-zinc-400 hover:text-white hover:bg-raise border-transparent"
                  }`
                }
              >
                <span className="relative shrink-0 inline-flex">
                  <Icon className="h-4 w-4 shrink-0" />
                  {to === "/soulseek" && <SlskIconDot dot={slskDot} />}
                </span>
                <span className="whitespace-nowrap">{to === "/soulseek" && slskDot?.name ? slskDot.name : label}</span>
              </NavLink>
            ))}
          </aside>
        </>
      )}

      <div className="flex-1 min-w-0 flex flex-col overflow-hidden relative z-10">
        {/* floating top bar: transparent overlay on the content — only the
            controls themselves catch the pointer */}
        <header className="absolute inset-x-0 top-0 h-12 z-30 flex items-center gap-3 px-4 pointer-events-none">
          <div className="flex items-center gap-1.5 shrink-0 pointer-events-auto">
            <button
              className="h-9 w-9 rounded-full border border-border bg-panel/60 backdrop-blur flex md:hidden items-center justify-center text-zinc-300 hover:text-white hover:border-accent/50 transition-colors"
              onClick={() => setNavOpen(true)}
              title="Menu"
            >
              <Menu className="h-4 w-4" />
            </button>
            <button
              className="h-9 w-9 rounded-full border border-border bg-panel/60 backdrop-blur flex items-center justify-center text-zinc-300 hover:text-white hover:border-accent/50 disabled:opacity-30 disabled:pointer-events-none transition-colors"
              onClick={goBack}
              disabled={pos === 0}
              title="Back"
            >
              <ChevronLeft className="h-4 w-4" />
            </button>
            <button
              className="h-9 w-9 rounded-full border border-border bg-panel/60 backdrop-blur hidden sm:flex items-center justify-center text-zinc-300 hover:text-white hover:border-accent/50 disabled:opacity-30 disabled:pointer-events-none transition-colors"
              onClick={goForward}
              disabled={pos >= stackRef.current.length - 1}
              title="Forward"
            >
              <ChevronRight className="h-4 w-4" />
            </button>
          </div>
          {/* the search input spans the rest of the bar */}
          <div className="relative flex-1 pointer-events-auto">
            <Search className="absolute left-3.5 top-1/2 -translate-y-1/2 h-4 w-4 text-zinc-500" />
            <input
              className="input !py-2 !pl-10 text-xs w-full !bg-panel/60 backdrop-blur"
              placeholder="Search for tracks, artists, albums…"
              title="Tag-scoped search: composer:name · person:name (any credit) · genre:metal · tag:anything — quotes keep spaces"
              value={query}
              onChange={(e) => onSearch(e.target.value)}
              onFocus={() => setSearchOpen(true)}
              onBlur={() => setTimeout(() => setSearchOpen(false), 150)}
              onKeyDown={(e) => {
                // Enter hands the query straight to the MusicBrainz browser
                // (a pasted musicbrainz.org link opens that entity instead)
                if (e.key !== "Enter" || !query.trim()) return;
                e.preventDefault();
                setSearchOpen(false);
                if (mbLink) {
                  navigate(`/mb/${mbLink[1] === "release-group" ? "rg" : mbLink[1]}/${mbLink[2]}`);
                } else {
                  goMbSearch();
                }
              }}
            />
            {searchOpen && query.trim() && (
              <div className="absolute left-0 right-0 top-full mt-1 z-40 rounded-lg border border-border bg-zinc-950/95 backdrop-blur shadow-xl overflow-hidden">
                {mbLink ? (
                  <button
                    className="w-full flex items-center gap-2.5 px-3 py-2.5 text-xs text-zinc-200 hover:bg-raise transition-colors text-left"
                    onClick={() => {
                      setSearchOpen(false);
                      navigate(`/mb/${mbLink[1] === "release-group" ? "rg" : mbLink[1]}/${mbLink[2]}`);
                    }}
                  >
                    <Music4 className="h-4 w-4 text-accent-soft shrink-0" />
                    Open this MusicBrainz {mbLink[1].replace("-", " ")} in the browser
                  </button>
                ) : (
                  <button
                    className="w-full flex items-center gap-2.5 px-3 py-2.5 text-xs text-zinc-200 hover:bg-raise transition-colors text-left"
                    onClick={goMbSearch}
                  >
                    <Music4 className="h-4 w-4 text-accent-soft shrink-0" />
                    Search MusicBrainz for “{query.trim()}”
                  </button>
                )}
              </div>
            )}
          </div>
          {/* live script progress floats below the bar so the search keeps
              the full width */}
          {progress && (
            <div className="absolute right-4 top-full mt-1 z-40">
              <ProgressInline progress={progress} />
            </div>
          )}
        </header>

        <main className="flex-1 overflow-auto min-w-0 pt-12">
          {/* keyed by pathname so each navigation eases the new page in */}
          <div key={location.pathname} className="page-enter">
            <Routes>
            <Route path="/" element={<LibraryPage />} />
            <Route path="/artist/:path" element={<ArtistPage />} />
            <Route path="/album/:path" element={<AlbumPage />} />
            <Route path="/track/:path" element={<TrackPage />} />
            <Route path="/playlists" element={<PlaylistsPage />} />
            <Route path="/playlist/:id" element={<PlaylistDetailPage />} />
            <Route path="/favorites" element={<Navigate to="/favorites/tracks" replace />} />
            <Route path="/favorites/:kind" element={<FavoritesPage />} />
            <Route path="/soulseek" element={<SoulseekPage />} />
            <Route path="/export" element={<ExportPage />} />
            <Route path="/optimize" element={<OptimizationPage />} />
            <Route path="/grading" element={<GradingPage />} />
            <Route path="/dependencies" element={<DependenciesPage />} />
            {/* MusicBrainz browser */}
            <Route path="/mb" element={<Navigate to="/mb/search" replace />} />
            <Route path="/mb/search" element={<MBSearchPage />} />
            <Route path="/mb/artist/:id" element={<MBArtistPage />} />
            <Route path="/mb/rg/:id" element={<MBReleaseGroupPage />} />
            <Route path="/mb/release/:id" element={<MBReleasePage />} />
            <Route path="/mb/recording/:id" element={<MBRecordingPage />} />
            <Route path="/import" element={<ImportWizard />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/setup" element={<Navigate to="/" replace />} />
            <Route
              path="*"
              element={
                <div className="p-10 text-center text-sm text-zinc-500">
                  Page not found —{" "}
                  <NavLink to="/" className="text-accent-soft hover:underline">
                    back to the library
                  </NavLink>
                </div>
              }
            />
          </Routes>
          </div>
        </main>

        <PlayerBar />
      </div>

      {toastMsg && (
        <div className="fixed bottom-20 left-1/2 -translate-x-1/2 z-50 rounded-lg border border-accent/40 bg-panel px-4 py-2 text-sm shadow-xl">
          {toastMsg}
        </div>
      )}
    </div>
  );
}
