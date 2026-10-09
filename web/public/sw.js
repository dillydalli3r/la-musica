/*
 * Offline playback service worker.
 *
 * "Download" in the app caches a track's stream into Cache Storage; this
 * worker serves those cached streams back to the <audio>/<video> elements
 * so playback keeps working with the server unreachable. Everything else
 * passes straight through to the network untouched.
 */
/* Two caches, two lifecycles. `mlo-media-v2` holds what the user downloaded
 * — media streams, artwork, lyrics, the payloads a download warms — and
 * `web/src/lib/mediaCache.ts` writes into the same name from the page. The
 * shell below (the document, the hashed bundles, the icon) is versioned on its
 * own: 3.11.0 replaced the app icon, and a single shared name would have
 * forced the choice between serving the old artwork forever and making every
 * downloaded track look undownloaded. */
const CACHE_NAME = "mlo-media-v2";
/* v2 (3.21.0): the download-navigation bug below could store an EXPORT ARCHIVE
 * under SHELL_URL, and an install that did would keep opening its "shell" as a
 * zip with the server down. `activate` deletes every cache this file no longer
 * names, so the bump is what purges it on the next activation. */
const SHELL_CACHE = "mlo-shell-v2";
const KEPT_CACHES = new Set([CACHE_NAME, SHELL_CACHE]);

/** The URL the shell is cached under: one document for every client-side
 *  route, which is what the server's SPA fallback serves. */
const SHELL_URL = new URL("/", self.location.origin).href;

self.addEventListener("install", () => {
  self.skipWaiting();
});

/** Web Push.
 *
 *  The app's own notifications are raised by the page over the /ws/events
 *  socket (see web/src/lib/notify.ts) — but a page that is CLOSED cannot raise
 *  anything, and that is the case this handler exists for: the server sends
 *  the same frame through a push service (server/events.py, Web Push) and the
 *  worker raises the notification, so "the import finished" reaches a phone in
 *  a pocket. The frame is the shape the socket carries, so both transports
 *  produce the same wording.
 */
self.addEventListener("push", (event) => {
  let frame = { title: "la musica", body: "", url: "/" };
  try {
    const data = event.data ? event.data.json() : null;
    if (data) {
      const inner = data.data || {};
      frame = {
        title: data.title || frame.title,
        body: data.body || "",
        // The frame names the route it is about (`link`, set by the emitter:
        // server/events.py). `url` is the same field for a page that raised
        // its own notification, and a top-level `link` is what an older
        // deployment's frame carried — read all three rather than dropping a
        // click-through.
        url: inner.link || data.link || data.url || "/",
      };
    }
  } catch {
    if (event.data) frame.body = event.data.text();
  }
  event.waitUntil(
    self.registration.showNotification(frame.title, {
      body: frame.body,
      icon: "/icon.png",
      badge: "/icon.png",
      tag: "mlo-push",
      data: { url: frame.url },
    })
  );
});

/** The push service rotated this device's subscription.
 *
 *  Chrome does not fire this (it just stops delivering, and the server sees a
 *  410 on the next send and prunes the row — see server/events.py); Firefox
 *  and Safari do, and without this the device would go quiet until its next
 *  page load. The kinds list is deliberately NOT sent from here: the worker
 *  has no access to the app's own preference, and a row with no kinds gets
 *  everything the server publishes — which the next page load narrows back to
 *  exactly what this device asked for (lib/notify.ts refreshPush). Reaching a
 *  device with one kind too many for a moment beats not reaching it at all.
 */
self.addEventListener("pushsubscriptionchange", (event) => {
  event.waitUntil(
    (async () => {
      try {
        const status = await fetch("/api/push/status", { credentials: "include" });
        if (!status.ok) return;
        const info = await status.json();
        if (!info?.public_key) return;
        const pad = "=".repeat((4 - (info.public_key.length % 4)) % 4);
        const raw = atob((info.public_key + pad).replace(/-/g, "+").replace(/_/g, "/"));
        const key = new Uint8Array(raw.length);
        for (let i = 0; i < raw.length; i++) key[i] = raw.charCodeAt(i);
        const sub =
          event.newSubscription ||
          (await self.registration.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: key,
          }));
        const keys = sub.toJSON().keys || {};
        await fetch("/api/push/subscribe", {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ endpoint: sub.endpoint, keys }),
        });
      } catch {
        /* No session (the page is closed and the cookie is gone), or the
           server is unreachable: the row is pruned on the next send, and the
           next page load re-subscribes. */
      }
    })()
  );
});

/** Clicking a notification focuses the app and follows the route the frame
 *  named — the album or the page the outcome is about (the page passes it as
 *  `data.url` in showNotification options) — or opens the app when none is
 *  running. */
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  // Resolved against this worker's origin rather than handed to navigate() as
  // it arrived: the frame carries an app route ("/album/…"), and a relative URL
  // is otherwise resolved against whatever page the client happens to be on.
  const raw = String((event.notification.data || {}).url || "/");
  let target = "/";
  try {
    target = new URL(raw, self.location.origin).href;
  } catch {
    target = new URL("/", self.location.origin).href;
  }
  event.waitUntil(
    (async () => {
      const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      for (const client of all) {
        if (!("navigate" in client)) continue;
        // A client that can navigate is sent to the subject even when it is
        // already showing the app: the user clicked a notification about THAT
        // album, so focusing the last page they left would be the wrong answer.
        let moved = null;
        try {
          // navigate() answers null when it refused to move the client (it was
          // closing, or the URL was rejected) — that is a client to skip, not a
          // click to swallow, and the loop then tries the next window.
          moved = await client.navigate(target);
        } catch {
          continue;
        }
        if (!moved) continue;
        // Focusing can be refused (a headless or minimised window). That is not
        // a reason for the click to do nothing: the navigation above already
        // happened, so the answer is "this window", never a SECOND one — which
        // is what falling through to openWindow would give.
        if ("focus" in moved) {
          try {
            return await moved.focus();
          } catch {
            return undefined;
          }
        }
        return undefined;
      }
      if (self.clients.openWindow) return self.clients.openWindow(target);
      return undefined;
    })()
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(names.filter((n) => !KEPT_CACHES.has(n)).map((n) => caches.delete(n)));
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

/** Cache the built app: the document, the shell it needs to run, and nothing
 *  else.
 *
 *  The bundle names are content-hashed, so they cannot be listed in this file:
 *  the document's own references ARE the shell (the entry chunk, the chunks it
 *  imports statically, the stylesheet, the icons), and `/precache.json` — the
 *  build's shell subset, see web/vite.config.ts — adds what the document does
 *  not name, which is the font its stylesheet loads. Without this the UI is
 *  unreachable with the server down, which is the one moment the downloaded
 *  music is supposed to matter.
 *
 *  The LAZY route chunks are deliberately not in either list any more. Pulling
 *  every page's code in at activation (~2 MB) competed with the first paint for
 *  routes the user had not opened; they come in on first use now, through the
 *  cache-first static path below (`STATIC_RE`), which stores whatever the
 *  browser fetches — so a visited page is cached exactly as before. A manifest
 *  from an OLDER build that still lists every chunk is cached as it always was:
 *  the list is only ever a list of URLs. */
async function precacheShell() {
  const cache = await caches.open(SHELL_CACHE);
  const resp = await fetch(SHELL_URL, { cache: "reload" });
  if (!resp.ok) return;
  const html = await resp.clone().text();
  await cache.put(SHELL_URL, resp);
  const referenced = [...html.matchAll(/(?:src|href)="(\/assets\/[^"]+|\/[^"'/]+\.(?:js|css|png|svg|webmanifest|ico|woff2?))"/g)].map(
    (m) => m[1]
  );
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

/** A response served out of the cache because the network could not answer,
 *  tagged so the page can tell it apart from a real one. api.ts reads this
 *  header and turns the offline banner on; without it a stale 200 would look
 *  like the server's own answer. A navigation cannot read its own document's
 *  headers, but the app's first API call is answered the same way, so the
 *  banner still appears. */
function marked(resp) {
  const headers = new Headers(resp.headers);
  headers.set("X-MLO-Offline", "1");
  return new Response(resp.body, { status: resp.status, statusText: resp.statusText, headers });
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
 *  with the server down instant instead of a white page.
 *
 *  `/fonts/` is here for the shell's own font: the STYLESHEET names it, not the
 *  document, so the browser asks for it through this path — and without an
 *  entry for it here the copy `precacheShell` stores would never be reachable
 *  offline. The same path is what caches a lazy route chunk the first time a
 *  page asks for it. */
const STATIC_RE = /^\/(assets\/|fonts\/|icon\.png|favicon|manifest|apple-touch)/;

/** The DEV server's module graph, which never exists in a production build.
 *
 *  `npm run dev` (and dev.py, and the compose bed) serves `web/src/**` as
 *  individual ES modules with cache-busting query strings — there is no bundle
 *  to precache. With the vite process gone (it was stopped, or the machine went
 *  offline), a lazy route's `import()` therefore failed outright: the owner's
 *  report was exactly that, "Error loading dynamically imported module:
 *  http://127.0.0.1:5181/src/pages/LibraryPage.tsx", and it is why the app
 *  "doesn't work that well offline" in a dev run.
 *
 *  NETWORK-FIRST, deliberately: a dev module served from the cache would be the
 *  stale module HMR exists to replace. The cache is the offline fallback only,
 *  filled on the way through — so a page the reader HAS opened in this run keeps
 *  working with vite down, and an unopened one fails with a 504 rather than a
 *  network error. A built app never asks for these paths (its modules are the
 *  hashed /assets/* files above), so this branch is dead in production. */
const DEV_MODULE_RE = /^\/(src\/|@vite\/|@id\/|@react-refresh|node_modules\/\.vite\/)/;

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
  //
  // A DOWNLOAD is a navigation too, and this branch used to take it: an
  // `<a download href="/api/export/zip/<id>">` arrives with `mode: "navigate"`,
  // so the worker fetched the archive, tried to store it as the SHELL under
  // SHELL_URL — and when that store failed (a 300 MB archive against the
  // cache's quota) the `catch` below answered the *download* with the cached
  // document. That is the owner's report exactly: "a 2.6 KB invalid .zip" —
  // which is this app's index.html (2,689 bytes) saved under the archive's
  // name. A real navigation's destination is `document` and a download's is
  // empty, and no app ROUTE lives under /api/ either — both facts are checked
  // here, so only a page the app can actually render takes this branch.
  if (req.mode === "navigate" && req.destination === "document" && !url.pathname.startsWith("/api/")) {
    event.respondWith(
      (async () => {
        const cache = await caches.open(SHELL_CACHE);
        try {
          const resp = await fetch(req);
          if (resp.ok) await cache.put(SHELL_URL, resp.clone());
          return resp;
        } catch {
          const hit = await cache.match(SHELL_URL);
          return hit ? marked(hit) : new Response("offline and not cached", { status: 504 });
        }
      })()
    );
    return;
  }

  if (sameOrigin && STATIC_RE.test(url.pathname)) {
    event.respondWith(
      (async () => {
        const cache = await caches.open(SHELL_CACHE);
        const hit = await cache.match(req);
        if (hit) return hit;
        const resp = await fetch(req);
        if (resp.ok) await cache.put(req, resp.clone());
        return resp;
      })()
    );
    return;
  }

  // The dev server's module graph (see DEV_MODULE_RE): network-first, cached
  // as the offline fallback so a dev run survives vite going away.
  if (sameOrigin && DEV_MODULE_RE.test(url.pathname)) {
    event.respondWith(
      (async () => {
        const cache = await caches.open(SHELL_CACHE);
        try {
          const resp = await fetch(req);
          if (resp.ok) await cache.put(req, resp.clone());
          return resp;
        } catch {
          // `ignoreVary`: the stored response is vite's, and it carries
          // `Vary: Origin` — so a lookup by a request that does not carry the
          // same Origin header MISSES the entry my own `cache.put(req, …)`
          // just wrote, which read as "offline and not cached" for a module
          // that was demonstrably in the cache (measured: LibraryPage.tsx and
          // main.tsx missed, a module precacheShell stored as a bare URL hit).
          // One origin serves these paths, so the vary header is noise here.
          const hit = await cache.match(req, { ignoreVary: true });
          return hit ?? new Response("offline and not cached", { status: 504 });
        }
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
          return hit ? marked(hit) : new Response("offline and not cached", { status: 504 });
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
