/*
 * Offline playback service worker.
 *
 * "Download" in the app caches a track's stream into Cache Storage; this
 * worker serves those cached streams back to the <audio>/<video> elements
 * so playback keeps working with the server unreachable. Everything else
 * passes straight through to the network untouched.
 */
const CACHE_NAME = "mlo-media-v2";

/** The URL the shell is cached under: one document for every client-side
 *  route, which is what the server's SPA fallback serves. */
const SHELL_URL = new URL("/", self.location.origin).href;

self.addEventListener("install", () => {
  self.skipWaiting();
});

/** Web Push.
 *
 *  The app's own notifications (a found wish, a finished download) are raised
 *  by the page over the /ws/events socket — see web/src/lib/notify.ts — because
 *  this server is self-hosted and has no VAPID key pair to sign a real push
 *  with. These two handlers exist so that a push subscription, if a deployment
 *  ever adds one, needs no client change: the frame is the same shape the
 *  socket carries, so the same wording reaches the user, with the app closed.
 */
self.addEventListener("push", (event) => {
  let frame = { title: "la musica", body: "" };
  try {
    const data = event.data ? event.data.json() : null;
    if (data) frame = { title: data.title || frame.title, body: data.body || "" };
  } catch {
    if (event.data) frame.body = event.data.text();
  }
  event.waitUntil(
    self.registration.showNotification(frame.title, {
      body: frame.body,
      icon: "/icon.png",
      badge: "/icon.png",
      tag: "mlo-push",
    })
  );
});

/** Clicking a notification focuses the app instead of opening a second copy —
 *  or opens one when none is running. */
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    (async () => {
      const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      for (const client of all) {
        if ("focus" in client) return client.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow("/");
      return undefined;
    })()
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)));
      // The shell is precached here rather than in `install`: a worker that
      // has been updated (or one installed while the server was down) still
      // needs a document to open, and this runs on every activation.
      try {
        await precacheShell();
      } catch {
        /* offline install: the fetches below will fill it in */
      }
      await self.clients.claim();
    })()
  );
});

/** Cache the built app: the document plus every bundle it references.
 *
 *  The bundle names are content-hashed, so they cannot be listed in this file
 *  — they are discovered from the served index.html. Without this the UI is
 *  unreachable with the server down, which is the one moment the downloaded
 *  music is supposed to matter. */
async function precacheShell() {
  const cache = await caches.open(CACHE_NAME);
  const resp = await fetch(SHELL_URL, { cache: "reload" });
  if (!resp.ok) return;
  const html = await resp.clone().text();
  await cache.put(SHELL_URL, resp);
  const referenced = [...html.matchAll(/(?:src|href)="(\/assets\/[^"]+|\/[^"'/]+\.(?:js|css|png|svg|webmanifest|ico|woff2?))"/g)].map(
    (m) => m[1]
  );
  // The build's own list adds the lazy route chunks, which index.html never
  // mentions — without them an offline navigation to an unvisited page would
  // wait on a chunk that can never arrive.
  let built = [];
  try {
    const manifest = await fetch("/precache.json", { cache: "reload" });
    if (manifest.ok) built = await manifest.json();
  } catch {
    /* an older build without the manifest: the document's own list stands */
  }
  await Promise.allSettled(
    [...new Set([...referenced, ...built])].map(async (a) => {
      const r = await fetch(a, { cache: "reload" });
      if (r.ok) await cache.put(a, r);
    })
  );
}

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

/** JSON the offline app is built from: artwork and lyrics, plus the payloads
 *  a download warms — the library (the downloads page groups by it), the
 *  config (the shell gates on it at boot) and the album/artist bodies (their
 *  descriptions, credits and cover references). Network-first, so online is
 *  always fresh, with the cache as the offline fallback; every successful
 *  response is stored on the way through, so browsing online fills the cache. */
const API_PATHS = new Set([
  "/api/cover",
  "/api/artist/image",
  "/api/lyrics/get",
  "/api/library",
  "/api/config",
  "/api/album",
  "/api/artist",
  // The credit rows of an album page: warmed by lib/mediaCache's entityUrls
  // with the download, so a cached album keeps its "who played what" offline.
  "/api/credits",
]);

/** Build output never changes under a given name (the names are hashed), so
 *  these are served from the cache first — that is what makes a cold start
 *  with the server down instant instead of a white page. */
const STATIC_RE = /^\/(assets\/|icon\.png|favicon|manifest|apple-touch)/;

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  // Tauri shell serves UI from tauri://localhost but streams absolute
  // http://127.0.0.1:8000 URLs — match both same-origin and backend origin.
  const sameOrigin = url.origin === self.location.origin;
  const backendOrigin = /^(http:\/\/127\.0\.0\.1:8000|http:\/\/localhost:8000)$/.test(url.origin);
  if (!(sameOrigin || backendOrigin)) return;

  // Navigations: network-first so a redeploy lands immediately, cached
  // document as the fallback — the shell must open with the server down.
  if (req.mode === "navigate") {
    event.respondWith(
      (async () => {
        const cache = await caches.open(CACHE_NAME);
        try {
          const resp = await fetch(req);
          if (resp.ok) await cache.put(SHELL_URL, resp.clone());
          return resp;
        } catch {
          const hit = await cache.match(SHELL_URL);
          return hit ?? new Response("offline and not cached", { status: 504 });
        }
      })()
    );
    return;
  }

  if (sameOrigin && STATIC_RE.test(url.pathname)) {
    event.respondWith(
      (async () => {
        const cache = await caches.open(CACHE_NAME);
        const hit = await cache.match(req);
        if (hit) return hit;
        const resp = await fetch(req);
        if (resp.ok) await cache.put(req, resp.clone());
        return resp;
      })()
    );
    return;
  }

  if (API_PATHS.has(url.pathname)) {
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
