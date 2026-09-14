import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import "./index.css";
import App from "./App";

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
        <App />
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