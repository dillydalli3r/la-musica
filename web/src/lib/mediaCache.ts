import { api } from "../api";
import { isVideoFile } from "./fmt";

/**
 * Offline media cache: "Download" in the app caches a track's audio into
 * the browser's Cache Storage so playback keeps working without the
 * server (a registered service worker serves cached streams). Saving a
 * file to disk is Export's job, not Download's.
 *
 * The cache key is the exact URL the player element requests, so the
 * service worker's cache.match hits on ordinary playback.
 */
const CACHE_NAME = "mlo-media-v2";

function absolute(base: string): string {
  // api.ts prefixes an absolute origin inside Tauri; resolve to absolute
  // here too so cache keys match SW-intercepted request URLs.
  return new URL(base, window.location.href).toString();
}

/** Every URL a track's stream may have been cached under: the direct one,
 * plus the live transcode for videos (the player retries a failed direct
 * stream through it). Lets lookups and removals work without a probe, so
 * "Downloaded" survives a server that is down. */
function cacheUrls(path: string): string[] {
  if (!isVideoFile(path)) return [absolute(api.streamUrl(path))];
  return [absolute(api.videoStreamUrl(path)), absolute(api.videoStreamUrl(path, true))];
}

/** The folder holding `path` — the album directory of a track file, and (one
 *  level further up) its artist directory. Server-side paths use "/"; a
 *  backslash is tolerated so a Windows-shaped path still resolves. */
function parentDir(path: string): string {
  const cut = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  return cut > 0 ? path.slice(0, cut) : path;
}

/** The artwork a downloaded album needs offline: its cover and the artist
 *  image. Both are plain GETs on the same origin as the streams, so the
 *  service worker serves them from this same cache. */
function artworkUrls(trackPath: string): string[] {
  const album = parentDir(trackPath);
  return [absolute(api.coverUrl(album)), absolute(api.artistImageUrl(parentDir(album)))];
}

async function cache(): Promise<Cache> {
  return caches.open(CACHE_NAME);
}

/** Warm one URL into the cache, best-effort: a missing cover or artist image
 *  must never fail the download that asked for it. */
async function warm(c: Cache, url: string): Promise<void> {
  try {
    const resp = await fetch(url);
    if (resp.ok) await c.put(url, resp);
  } catch {
    /* offline or absent — the audio cache is what matters */
  }
}

export async function cacheTrack(path: string): Promise<void> {
  // Optimistic direct URL — no probe up front; the player retries via
  // transcode only if direct playback actually fails.
  const url = isVideoFile(path) ? absolute(api.videoStreamUrl(path)) : absolute(api.streamUrl(path));
  const c = await cache();
  // The response is explicitly moved into the cache; the stream endpoint
  // has no custom headers we need to preserve beyond the defaults.
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`server responded ${resp.status}`);
  await c.put(url, resp);
  // Covers and the artist image ride along, so offline playback is not left
  // with a placeholder where the artwork should be.
  await Promise.allSettled(artworkUrls(path).map((u) => warm(c, u)));
}

export async function uncacheTrack(path: string): Promise<void> {
  const c = await cache();
  await Promise.all(cacheUrls(path).map((u) => c.delete(u)));
}

export async function isTrackCached(path: string): Promise<boolean> {
  try {
    const c = await cache();
    const hits = await Promise.all(cacheUrls(path).map((u) => c.match(u)));
    return hits.some(Boolean);
  } catch {
    return false; // Cache Storage unavailable (insecure context)
  }
}

/** Cached-track cache keys (absolute stream URLs), for debugging/UX. */
export async function cachedUrls(): Promise<string[]> {
  try {
    const c = await cache();
    return (await c.keys()).map((r) => r.url);
  } catch {
    return [];
  }
}

/** Library-relative paths behind those keys — the cache key IS the stream URL,
 *  so the path has to be read back out of its query string. Artwork keys carry
 *  `album=`/`artist=` instead and are skipped; a video cached under both its
 *  direct and its transcoded URL collapses to one path. */
export async function cachedPaths(): Promise<string[]> {
  const paths = new Set<string>();
  for (const u of await cachedUrls()) {
    try {
      const p = new URL(u).searchParams.get("path");
      if (p) paths.add(p);
    } catch {
      /* not a URL this module wrote */
    }
  }
  return [...paths];
}

/** Total cached bytes (Content-Length sums), for a storage readout. */
export async function cachedBytes(): Promise<number> {
  try {
    const c = await cache();
    let total = 0;
    for (const req of await c.keys()) {
      const r = await c.match(req);
      if (!r) continue;
      const len = r.headers.get("content-length");
      total += len ? Number(len) : (await r.clone().blob()).size;
    }
    return total;
  } catch {
    return 0;
  }
}

/** Evict everything (offline cache reset). */
export async function clearMediaCache(): Promise<void> {
  await caches.delete(CACHE_NAME);
}
