import { Component, StrictMode, type ErrorInfo, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CircleAlert } from "lucide-react";
import "./index.css";
import App from "./App";
import { applyConfigLocale } from "./lib/i18n";

// The locale is fixed before the first render: the app has not fetched
// /api/config yet, so this resolves this browser's own pick, then its
// language, and only then English — applyConfigLocale() with no argument is
// exactly that boot pass. When the config does arrive, App (and the settings
// page) call applyConfigLocale(cfg) so the server's `ui_locale` applies too.
applyConfigLocale();

/** A render-time throw anywhere in the app (a lazy page chunk included)
 * would otherwise unmount the whole tree to a blank screen. */
class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Unhandled render error", error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="min-h-screen flex items-center justify-center bg-bg p-6 text-zinc-100">
        <div className="w-full max-w-lg rounded-xl border border-border bg-card p-5 space-y-3">
          <div className="flex items-center gap-2 text-sm font-semibold">
            <CircleAlert className="h-4 w-4 text-red-400" /> Something went wrong
          </div>
          <p className="text-xs text-zinc-400">
            The app hit an error while rendering this page. Reloading usually clears it.
          </p>
          <pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded border border-red-900/40 bg-red-950/30 p-2 text-[11px] font-mono text-red-300/90">
            {error.message}
          </pre>
          <button className="btn-ghost text-xs" onClick={() => window.location.reload()}>
            Reload
          </button>
        </div>
      </div>
    );
  }
}

const queryClient = new QueryClient({
  // /api/library and /api/album re-grade on the server per request — cache
  // results aggressively; every mutating action invalidates explicitly.
  defaultOptions: {
    queries: { staleTime: 60_000, retry: 1, refetchOnWindowFocus: false },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <ErrorBoundary>
          <App />
        </ErrorBoundary>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>
);

// Offline playback: the worker serves cached audio/video streams so
// "Download" (cache in the app) keeps tracks playable without the server.
// Needs a secure context (localhost qualifies; plain-LAN http does not).
// Skipped in Tauri (no SW support) and insecure contexts.
// ponytail: Tauri offline playback needs native-side cache; add when requested.
if ("serviceWorker" in navigator && window.isSecureContext
    && !(window as any).__TAURI_INTERNALS__) {
  window.addEventListener("load", () => {
    // absolute path — a relative "sw.js" resolves against the current route
    // (/album/<path> → /album/sw.js) which the SPA fallback answers with
    // index.html, so deep links never got offline playback.
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {
      /* offline cache stays unavailable — streaming still works */
    });
  });
}