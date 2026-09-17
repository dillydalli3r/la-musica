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

/** The URL this track's player element will request. A video whose probe
 * says the browser cannot decode it natively is played through the live
 * transcode (`?transcode=1`), so it must be cached under that same URL —
 * the direct-stream URL is never requested for exactly those videos. */
export async function playableUrl(path: string): Promise<string> {
  if (!isVideoFile(path)) return absolute(api.streamUrl(path));
  let transcode = false;
  try {
    transcode = (await api.videoMeta(path)).native === false;
  } catch {
    /* probe unavailable (server down) — fall back to the direct stream */
  }
  return absolute(api.videoStreamUrl(path, transcode));
}

/** Every URL a track's stream may have been cached under: the direct one,
 * plus the live transcode for videos (the player retries a failed direct
 * stream through it). Lets lookups and removals work without a probe, so
 * "Downloaded" survives a server that is down. */
function cacheUrls(path: string): string[] {
  if (!isVideoFile(path)) return [absolute(api.streamUrl(path))];
  return [absolute(api.videoStreamUrl(path)), absolute(api.videoStreamUrl(path, true))];
}

async function cache(): Promise<Cache> {
  return caches.open(CACHE_NAME);
}

export async function cacheTrack(path: string): Promise<void> {
  const url = await playableUrl(path);
  const c = await cache();
  // The response is explicitly moved into the cache; the stream endpoint
  // has no custom headers we need to preserve beyond the defaults.
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`server responded ${resp.status}`);
  await c.put(url, resp);
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
