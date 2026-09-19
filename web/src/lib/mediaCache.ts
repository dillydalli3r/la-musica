import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
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

/** The JSON a downloaded track needs offline: its album payload (description,
 *  credits-of-the-page, cover reference — the album page's body) and its
 *  artist payload. MusicBrainz credit ROWS are not part of either (they come
 *  from /api/credits, which needs the network), so the credits menu is the one
 *  part of an album page that needs the server. Same shape of GET as artwork,
 *  and the service worker serves them from this cache too, so an album page
 *  opens with the server down.
 *  The URLs must stay identical to `api.album()` / `api.artist()`. */
function entityUrls(trackPath: string): string[] {
  const album = parentDir(trackPath);
  return [
    absolute(`/api/album?path=${encodeURIComponent(album)}`),
    absolute(`/api/artist?path=${encodeURIComponent(parentDir(album))}`),
  ];
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

/** How long one track's stream may take before the attempt is abandoned.
 *  Without this the fetch had no deadline at all: a stalled server left the
 *  button spinning until the browser gave up (minutes), and the track was
 *  reported as failed with nothing to show for the wait. */
const STREAM_TIMEOUT_MS = 120_000;

/** Fetch a stream with a deadline and one retry.
 *
 *  A dropped connection or a 5xx is worth a second attempt (the server may be
 *  mid-restart); a 4xx is the server saying no, so it is reported as-is —
 *  "could not be downloaded" with a status is actionable, a bare count is
 *  not. */
async function fetchStream(url: string): Promise<Response> {
  const attempt = () => fetch(url, { signal: AbortSignal.timeout(STREAM_TIMEOUT_MS) });
  let resp: Response;
  try {
    resp = await attempt();
  } catch {
    resp = await attempt();
  }
  if (!resp.ok && resp.status >= 500) resp = await attempt();
  if (!resp.ok) {
    throw new Error(
      resp.status === 404 ? "the server has no file at that path" : `the server answered ${resp.status}`
    );
  }
  return resp;
}

export async function cacheTrack(path: string): Promise<void> {
  // Optimistic direct URL — no probe up front; the player retries via
  // transcode only if direct playback actually fails.
  const url = isVideoFile(path) ? absolute(api.videoStreamUrl(path)) : absolute(api.streamUrl(path));
  const c = await cache();
  // The response is explicitly moved into the cache; the stream endpoint
  // has no custom headers we need to preserve beyond the defaults.
  const resp = await fetchStream(url);
  await c.put(url, resp);
  // A short body is worse than no body: the service worker would serve a
  // truncated stream as if it were the whole track, and the player would fail
  // at the end of it with the server long gone. Both the server and the cache
  // entry carry a Content-Length, so the mismatch is visible without reading
  // the payload back.
  const want = Number(resp.headers.get("content-length") || 0);
  const got = Number((await c.match(url))?.headers.get("content-length") || 0);
  if (want && got && want !== got) {
    await c.delete(url);
    throw new Error(`the download was cut short (${got} of ${want} bytes)`);
  }
  // Covers, the artist image and the album/artist payloads ride along, so
  // offline playback is not left with a placeholder where the artwork should
  // be, and the album page still has its description.
  await Promise.allSettled([...artworkUrls(path), ...entityUrls(path)].map((u) => warm(c, u)));
}

export async function uncacheTrack(path: string): Promise<void> {
  const c = await cache();
  await Promise.all(cacheUrls(path).map((u) => c.delete(u)));
  await pruneEntityPayloads();
}

/** Drop every album/artist payload no cached track needs any more.
 *
 *  Called after a removal instead of deleting the payload per track: an album
 *  still holding cached tracks must keep the payload it renders from, and a
 *  bulk removal is one sweep rather than N racing ones. */
export async function pruneEntityPayloads(): Promise<void> {
  const keep = new Set<string>();
  for (const p of await cachedPaths()) {
    const album = parentDir(p);
    keep.add(album);
    keep.add(parentDir(album));
  }
  const c = await cache();
  for (const u of await cachedUrls()) {
    let url: URL;
    try {
      url = new URL(u);
    } catch {
      continue;
    }
    if (url.pathname !== "/api/album" && url.pathname !== "/api/artist") continue;
    if (!keep.has(url.searchParams.get("path") || "")) await c.delete(u);
  }
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
 *  direct and its transcoded URL collapses to one path.
 *
 *  The album/artist PAYLOAD keys (see entityUrls) carry `path=` too, and they
 *  are JSON for a whole folder rather than a track — they are excluded by
 *  endpoint, or the downloads page counts each downloaded album twice. */
export async function cachedPaths(): Promise<string[]> {
  const paths = new Set<string>();
  for (const u of await cachedUrls()) {
    try {
      const url = new URL(u);
      if (url.pathname === "/api/album" || url.pathname === "/api/artist") continue;
      const p = url.searchParams.get("path");
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

/** The query key for the cached-path snapshot. One key, so CachedTracksView,
 *  the download controls and every downloaded mark on a title read the SAME
 *  list — a download anywhere shows up everywhere on the next invalidation. */
export const CACHED_PATHS_KEY = ["cachedPaths"] as const;

/** The downloaded paths as a set. Each row mounts its own observer, but the
 *  shared key dedupes them into one Cache Storage scan. */
export function useCachedPaths(): Set<string> {
  const { data } = useQuery({ queryKey: CACHED_PATHS_KEY, queryFn: cachedPaths });
  return useMemo(() => new Set(data ?? []), [data]);
}
