/*
 * Offline playback service worker.
 *
 * "Download" in the app caches a track's stream into Cache Storage; this
 * worker serves those cached streams back to the <audio>/<video> elements
 * so playback keeps working with the server unreachable. Everything else
 * passes straight through to the network untouched.
 */
const CACHE_NAME = "mlo-media-v1";

self.addEventListener("install", () => {
  // No precaching — tracks are cached explicitly from the UI.
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)));
      await self.clients.claim();
    })()
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  // Tauri shell serves UI from tauri://localhost but streams absolute
  // http://127.0.0.1:8000 URLs — match both same-origin and backend origin.
  const sameOrigin = url.origin === self.location.origin;
  const backendOrigin = /^(http:\/\/127\.0\.0\.1:8000|http:\/\/localhost:8000)$/.test(url.origin);
  if (!(sameOrigin || backendOrigin)) return;
  // Only media streams are served from the cache; API/UI requests are
  // never intercepted.
  if (!(url.pathname === "/api/stream" || url.pathname === "/api/videos/stream")) return;
  // Range seeks need byte slices; cached full body served whole stalls <audio>.
  if (req.headers.has("range")) {
    try {
      return void event.respondWith(fetch(req));
    } catch (e) {
      return;
    }
  }

  event.respondWith(
    (async () => {
      const cache = await caches.open(CACHE_NAME);
      const hit = await cache.match(req, { ignoreSearch: false });
      if (hit) return hit;
      try {
        return await fetch(req);
      } catch (e) {
        return new Response("offline and not cached", { status: 504 });
      }
    })()
  );
});
