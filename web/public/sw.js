/*
 * Offline playback service worker.
 *
 * "Download" in the app caches a track's stream into Cache Storage; this
 * worker serves those cached streams back to the <audio>/<video> elements
 * so playback keeps working with the server unreachable. Everything else
 * passes straight through to the network untouched.
 */
const CACHE_NAME = "mlo-media-v2";

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

/** Serve `range` ("bytes=0-", "bytes=100-200", "bytes=-500") out of a cached
 * full body as a 206. Chrome/Edge send a Range header for every media
 * element, so without this the cache is never consulted for a real
 * <audio>/<video> request. */
async function sliceCached(resp, range) {
  const m = /^bytes=(\d*)-(\d*)$/.exec(range.trim());
  if (!m || (!m[1] && !m[2])) return resp;
  const buf = await resp.arrayBuffer();
  const total = buf.byteLength;
  const start = m[1] ? Number(m[1]) : Math.max(total - Number(m[2]), 0);
  const end = m[1] && m[2] ? Math.min(Number(m[2]), total - 1) : total - 1;
  if (start >= total || start > end) {
    return new Response(null, { status: 416, headers: { "Content-Range": `bytes */${total}` } });
  }
  const headers = new Headers(resp.headers);
  headers.set("Content-Range", `bytes ${start}-${end}/${total}`);
  headers.set("Content-Length", String(end - start + 1));
  headers.set("Accept-Ranges", "bytes");
  return new Response(buf.slice(start, end + 1), { status: 206, statusText: "Partial Content", headers });
}

/** Artwork and lyrics: plain GETs whose payload is small and immutable enough
 *  to keep. Network-first (so a changed cover shows immediately when online)
 *  with the cache as the offline fallback, and every successful response is
 *  stored on the way through — browsing online is what fills the cache. */
const ART_PATHS = new Set(["/api/cover", "/api/artist/image", "/api/lyrics/get"]);

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  // Tauri shell serves UI from tauri://localhost but streams absolute
  // http://127.0.0.1:8000 URLs — match both same-origin and backend origin.
  const sameOrigin = url.origin === self.location.origin;
  const backendOrigin = /^(http:\/\/127\.0\.0\.1:8000|http:\/\/localhost:8000)$/.test(url.origin);
  if (!(sameOrigin || backendOrigin)) return;

  if (ART_PATHS.has(url.pathname)) {
    event.respondWith(
      (async () => {
        const cache = await caches.open(CACHE_NAME);
        try {
          const resp = await fetch(req);
          if (resp.ok) await cache.put(req, resp.clone());
          return resp;
        } catch {
          const hit = await cache.match(req);
          return hit ?? new Response("offline and not cached", { status: 504 });
        }
      })()
    );
    return;
  }

  // Media streams are served from the cache; every other API/UI request is
  // never intercepted.
  if (!(url.pathname === "/api/stream" || url.pathname === "/api/videos/stream")) return;

  event.respondWith(
    (async () => {
      const cache = await caches.open(CACHE_NAME);
      const range = req.headers.get("range");
      // Cache matching ignores the Range header, so a media element's
      // "bytes=0-" request still hits the full cached body.
      const hit = await cache.match(req);
      if (hit) return range ? sliceCached(hit, range) : hit;
      try {
        return await fetch(req);
      } catch (e) {
        return new Response("offline and not cached", { status: 504 });
      }
    })()
  );
});
