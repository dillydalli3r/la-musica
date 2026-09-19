import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { isVideoFile } from "./fmt";
import type { Library, Track } from "../types";

/**
 * Offline media cache: "Download" in the app caches a track's audio into
 * the browser's Cache Storage so playback keeps working without the
 * server (a registered service worker serves cached streams). Saving a
 * file to disk is Export's job, not Download's.
 *
 * The cache key is the exact URL the player element requests, so the
 * service worker's cache.match hits on ordinary playback. That URL is built
 * from a file PATH, though, and organizing the library is what this app does
 * — a moved file leaves its download at a key nothing asks for any more.
 * Beside the bytes lives an identity index (below): MusicBrainz recording id
 * → where its bytes actually sit, so a rename cannot orphan a download.
 */
const CACHE_NAME = "mlo-media-v2";

/** The `blob:` URLs handed out by offlineMediaUrl, keyed by the cache key
 *  they wrap: created once per key (a player that asks twice gets the same
 *  URL) and revoked the moment the bytes are removed. */
const blobUrls = new Map<string, string>();

/** Where the identity index lives: one JSON entry in the same Cache Storage
 *  as the media, so "Clear all" evicts both together and the two can never
 *  drift apart. */
const INDEX_KEY = "/mlo-media-index.json";

/** One indexed track: the URL its bytes are stored under, plus the library
 *  path they were downloaded FROM. The identity is the index's own key and
 *  what matching uses — the recorded path only feeds artwork/payload
 *  bookkeeping, which is written from the same path at download time. */
type IndexRow = { url: string; path: string };

/** A cached track as the rest of the app sees it. */
export type CachedTrack = {
  /** The identity it is filed under: `mb:<uuid>`, or `path:<path>` for a file
   *  with no MusicBrainz recording id. */
  key: string;
  /** Where the bytes sit — the stream URL the player requests. */
  url: string;
  /** The path they were downloaded from (stale once the file is moved). */
  path: string;
  /** The recording id behind `key`, when there is one. */
  mbid: string | null;
};

/** The identity a track is cached under: its MusicBrainz recording id when it
 *  has one, the path otherwise — a file with no MBID carries nothing that
 *  would survive a move. */
export function trackIdentity(path: string, mbid?: string | null): string {
  const id = (mbid ?? "").trim().toLowerCase();
  return id ? `mb:${id}` : `path:${path}`;
}

/** One track to look up or remove: its path, plus the MusicBrainz recording id
 *  it is filed under when the caller has one. The id is what survives a move —
 *  a path alone cannot name a file that was renamed or reorganized.
 *
 *  A single parameter on purpose: callers hand these straight to `Array.map`
 *  with only paths in hand (`paths.map(uncacheTrack)`), and a second
 *  positional slot would swallow the array index as if it were an id. */
export type CacheTarget = string | { path: string; mbid?: string | null };

function targetOf(target: CacheTarget): { path: string; mbid: string | null } {
  if (typeof target === "string") return { path: target, mbid: null };
  return { path: target.path, mbid: (target.mbid ?? "").trim().toLowerCase() || null };
}

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
 *  cover reference, the graded rows — the album page's body), its artist
 *  payload (bio, the artist image's identity) and the credit ROWS for the
 *  track's album (who played what — a separate endpoint, and the one part of
 *  an album page the server used to be needed for). Same shape of GET as
 *  artwork, and the service worker serves them from this cache too, so an
 *  album page opens with the server down.
 *  The URLs must stay identical to `api.album()` / `api.artist()` /
 *  `api.credits()` — keyed on the same `path=`/`album=` the pages ask with,
 *  or the cache is filled with entries nothing ever reads. */
function entityUrls(trackPath: string): string[] {
  const album = parentDir(trackPath);
  return [
    absolute(`/api/album?path=${encodeURIComponent(album)}`),
    absolute(`/api/artist?path=${encodeURIComponent(parentDir(album))}`),
    absolute(`/api/credits?album=${encodeURIComponent(album)}`),
  ];
}

async function cache(): Promise<Cache> {
  return caches.open(CACHE_NAME);
}

/** The identity index, out of Cache Storage as a plain object (absent or
 *  unreadable reads as empty: every caller falls back to path matching). */
async function readIndex(c: Cache): Promise<Record<string, IndexRow>> {
  try {
    const hit = await c.match(absolute(INDEX_KEY));
    if (!hit) return {};
    const raw: unknown = await hit.json();
    return raw && typeof raw === "object" ? (raw as Record<string, IndexRow>) : {};
  } catch {
    return {}; // absent, or written by a version that shaped it differently
  }
}

/** Best-effort on purpose: the bytes are cached whether or not this lands, and
 *  a lost row costs a moved track its mark — never the download itself. */
async function writeIndex(c: Cache, index: Record<string, IndexRow>): Promise<void> {
  try {
    await c.put(
      absolute(INDEX_KEY),
      new Response(JSON.stringify(index), { headers: { "Content-Type": "application/json" } })
    );
  } catch {
    /* quota, or Cache Storage unavailable (insecure context) */
  }
}

/** The URL a track's bytes are actually filed at, when that is no longer the
 *  path in hand: the index row written at download time. */
async function indexedUrl(c: Cache, path: string, mbid: string): Promise<string | null> {
  // The identity key holds the id alone, so `path` here is irrelevant to the
  // lookup — which is exactly how a moved file still resolves.
  const index = await readIndex(c);
  return index[trackIdentity(path, mbid)]?.url ?? null;
}

/** A track's MusicBrainz recording id, read back out of the album payload the
 *  download already warmed for offline use (the tag the library payload
 *  carries as `MUSICBRAINZ_TRACKID`). Reaching for the library payload here
 *  instead would cost a whole-library scan per download. */
async function trackMbid(c: Cache, path: string): Promise<string | null> {
  try {
    const hit = await c.match(absolute(`/api/album?path=${encodeURIComponent(parentDir(path))}`));
    if (!hit) return null;
    const album = (await hit.json()) as { tracks?: Track[] };
    const tag = (album.tracks ?? []).find((t) => t.path === path)?.tags?.MUSICBRAINZ_TRACKID;
    return (tag ?? "").trim().toLowerCase() || null;
  } catch {
    return null; // payload missing or not JSON — the path key stands
  }
}

/** A `blob:` URL for one stored response: created once per cache key, so a
 *  player that asks twice gets the same URL instead of leaking another blob. */
async function blobFor(key: string, resp: Response): Promise<string> {
  const existing = blobUrls.get(key);
  if (existing) return existing;
  const blob = URL.createObjectURL(await resp.blob());
  blobUrls.set(key, blob);
  return blob;
}

/** Drop the `blob:` URL wrapping a cache key, if one was ever handed out — a
 *  live blob keeps its bytes alive after the entry is gone. */
function revoke(key: string): void {
  const blob = blobUrls.get(key);
  if (blob) {
    URL.revokeObjectURL(blob);
    blobUrls.delete(key);
  }
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
  // File the bytes under the track's identity too — the album payload is in
  // the cache by now (warm() above), and its row is what keeps this download
  // attached to the track when the organizer moves the file. A download made
  // before this index existed keeps working off its path key and gains a row
  // the next time it is cached.
  const mbid = await trackMbid(c, path);
  if (!mbid) return;
  const key = trackIdentity(path, mbid);
  const index = await readIndex(c);
  const prev = index[key];
  if (prev && prev.url !== url) {
    // The same recording, downloaded earlier at a path it has since been
    // moved off. These bytes just superseded those: drop them instead of
    // leaving a second full-length copy nothing will ever ask for.
    await c.delete(prev.url);
    revoke(prev.url);
  }
  index[key] = { url, path };
  await writeIndex(c, index);
}

export async function uncacheTrack(target: CacheTarget): Promise<void> {
  const { path, mbid } = targetOf(target);
  const c = await cache();
  // The index row first: for a track that has been MOVED since it was
  // downloaded, it is the only thing naming the bytes behind the mark — the
  // path the caller holds resolves to nothing.
  const index = await readIndex(c);
  const key = mbid ? trackIdentity(path, mbid) : null;
  let row = key ? index[key] : undefined;
  if (key && row) delete index[key];
  if (!row) {
    // No id in hand (or no row for it): a row that remembers this path is the
    // same download seen from the other side.
    const found = Object.entries(index).find(([, r]) => r?.path === path);
    if (found) {
      row = found[1];
      delete index[found[0]];
    }
  }
  // Last row gone: drop the index entry itself rather than leave an empty
  // document sitting in the cache (it would count as stored bytes forever).
  if (row) {
    if (Object.keys(index).length) await writeIndex(c, index);
    else await c.delete(absolute(INDEX_KEY));
  }

  const urls = new Set(cacheUrls(path));
  if (row) {
    urls.add(row.url);
    // Its artwork was warmed from the path it was downloaded at.
    for (const u of artworkUrls(row.path)) urls.add(u);
  }
  await Promise.all([...urls].map((u) => c.delete(u)));
  // A live blob: URL keeps its bytes alive after the cache entry is gone, and
  // would keep feeding an element a track the user just removed. The album
  // cover and artist image downloaded alongside it go the same way: their
  // blobs (offlineArtworkUrl) are keyed as artworkUrls() warms them.
  for (const u of [...urls, ...artworkUrls(path)]) revoke(u);
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
    const key = url.pathname === "/api/credits" ? "album" : "path";
    if (!["/api/album", "/api/artist", "/api/credits"].includes(url.pathname)) continue;
    if (!keep.has(url.searchParams.get(key) || "")) await c.delete(u);
  }
}

export async function isTrackCached(target: CacheTarget): Promise<boolean> {
  const { path, mbid } = targetOf(target);
  try {
    const c = await cache();
    const hits = await Promise.all(cacheUrls(path).map((u) => c.match(u)));
    if (hits.some(Boolean)) return true;
    // Nothing at the path: the file may have been moved since it was
    // downloaded, and its bytes then only answer to the identity.
    const idUrl = mbid ? await indexedUrl(c, path, mbid) : null;
    return !!idUrl && !!(await c.match(idUrl));
  } catch {
    return false; // Cache Storage unavailable (insecure context)
  }
}

/** Cached-track cache keys, for debugging/UX: the streams, the artwork and
 *  the payloads this module warms, plus the identity index itself. */
export async function cachedUrls(): Promise<string[]> {
  try {
    const c = await cache();
    return (await c.keys()).map((r) => r.url);
  } catch {
    return [];
  }
}

/** Every cached track: an indexed download as one row keyed by its recording
 *  id, and everything the index does not know — an entry cached before the
 *  index existed, or a file with no MBID — read straight off its path key.
 *  Rows whose bytes are gone are skipped, so a stale index can never claim a
 *  download that was removed.
 *
 *  Artwork keys carry `album=`/`artist=` instead of `path=` and are skipped;
 *  the album/artist PAYLOAD keys (see entityUrls) carry `path=` too but are
 *  JSON for a whole folder rather than a track, and are excluded by endpoint
 *  or the downloads page would count each downloaded album twice. A video
 *  cached under both its direct and its transcoded URL collapses to one row. */
export async function cachedTracks(): Promise<CachedTrack[]> {
  try {
    const c = await cache();
    const keys = (await c.keys()).map((r) => r.url);
    const stored = new Set(keys);
    const index = await readIndex(c);
    const out: CachedTrack[] = [];
    const seen = new Set<string>();
    const indexKey = absolute(INDEX_KEY);
    for (const [key, row] of Object.entries(index)) {
      if (!row || !stored.has(row.url)) continue;
      seen.add(row.url);
      seen.add(row.path);
      out.push({ key, url: row.url, path: row.path, mbid: key.startsWith("mb:") ? key.slice(3) : null });
    }
    for (const url of keys) {
      if (url === indexKey || seen.has(url)) continue;
      let parsed: URL;
      try {
        parsed = new URL(url);
      } catch {
        continue; // not a URL this module wrote
      }
      if (parsed.pathname !== "/api/stream" && parsed.pathname !== "/api/videos/stream") continue;
      const path = parsed.searchParams.get("path");
      if (!path || seen.has(path)) continue;
      seen.add(path);
      seen.add(url);
      out.push({ key: trackIdentity(path, null), url, path, mbid: null });
    }
    return out;
  } catch {
    return []; // Cache Storage unavailable (insecure context)
  }
}

/** Library-relative paths behind those tracks, for the byte/count readouts
 *  and the artwork payload sweep. Artwork keys carry `album=`/`artist=`
 *  instead and are skipped. */
export async function cachedPaths(): Promise<string[]> {
  return [...new Set((await cachedTracks()).map((t) => t.path))];
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
  // One map holds the stream AND artwork blobs, so this sweep covers both.
  for (const url of blobUrls.values()) URL.revokeObjectURL(url);
  blobUrls.clear();
  await caches.delete(CACHE_NAME);
}

/** A `blob:` URL for a downloaded track, or null when it is not cached.
 *
 *  In a shell there is no service worker, so the stream URL the player
 *  normally requests (http://host:8000/api/stream?…) simply fails when the
 *  server is away — the bytes are in Cache Storage, but nothing hands them to
 *  an <audio>/<video> element. This is that hand-off: look the path up under
 *  the same keys `cacheUrls()` uses and turn the stored body into a URL the
 *  element can play. Created once per cache key and revoked on removal.
 *
 *  Pass the track's MusicBrainz recording id (a CacheTarget) when it has one:
 *  the file may have been moved since it was downloaded, and its bytes then
 *  sit at the URL the index remembers rather than at this path's. */
export async function offlineMediaUrl(target: CacheTarget): Promise<string | null> {
  const { path, mbid } = targetOf(target);
  try {
    const c = await cache();
    for (const url of cacheUrls(path)) {
      const hit = await c.match(url);
      if (hit) return await blobFor(url, hit);
    }
    // Moved since it was downloaded: the bytes are still there, filed under
    // the identity, at whatever URL they were fetched from.
    const idUrl = mbid ? await indexedUrl(c, path, mbid) : null;
    const hit = idUrl ? await c.match(idUrl) : null;
    return hit && idUrl ? await blobFor(idUrl, hit) : null;
  } catch {
    return null; // Cache Storage unavailable (insecure context), or no entry
  }
}

/** A `blob:` URL for an image already in the offline cache, or null when its
 *  bytes were never downloaded.
 *
 *  The image twin of offlineMediaUrl: in a shell nothing serves Cache Storage
 *  back to the webview, so a cover sitting in the cache is still requested
 *  over the network. With the server down that request fails and every
 *  thumbnail falls back to the placeholder, even for a downloaded album.
 *
 *  Keyed exactly as `cacheTrack`'s warm() wrote it — with one wrinkle: warm()
 *  caches an album cover WITHOUT the `&file=` a page requests it with (the
 *  folder's default art), so the same picture is stored under two keys. The
 *  requested key is tried first, then the same key minus `file=`, or a
 *  downloaded cover would never be found. Created once per key and revoked
 *  with the other blob URLs. */
export async function offlineArtworkUrl(url: string): Promise<string | null> {
  try {
    const key = absolute(url);
    // Not `searchParams.delete`: re-serializing would rewrite `%20` as `+` and
    // never match the warmed key for an album path with a space in it.
    const cut = key.indexOf("&file=");
    const keys = [key];
    if (cut > 0) {
      const next = key.indexOf("&", cut + 1);
      keys.push(key.slice(0, cut) + (next < 0 ? "" : key.slice(next)));
    }
    const c = await cache();
    for (const k of keys) {
      const hit = await c.match(k);
      if (hit) return await blobFor(k, hit);
    }
    return null;
  } catch {
    return null; // Cache Storage unavailable (insecure context), or no entry
  }
}

/** The query key for the cached-track snapshot. One key, so CachedTracksView,
 *  the download controls and every downloaded mark on a title read the SAME
 *  list — a download anywhere shows up everywhere on the next invalidation.
 *  (The stored value is `cachedTracks()`, identity rows included.) */
export const CACHED_PATHS_KEY = ["cachedPaths"] as const;

/** Paths of cached tracks, resolved against where the library keeps them NOW.
 *
 *  Each cached track contributes the path it was downloaded from plus, when it
 *  was filed under a MusicBrainz recording id, whichever path the library
 *  reports for that recording today. That second path is the whole point: the
 *  organizer is free to move a file, and the Mark on its title (and every
 *  download control reading this set) still finds it cached.
 *
 *  The library is only read from the query cache, never fetched from here —
 *  a title mark must not trigger a whole-library scan on a page that was
 *  happy with its album payload. */
export function useCachedPaths(): Set<string> {
  const { data: tracks } = useQuery({ queryKey: CACHED_PATHS_KEY, queryFn: cachedTracks });
  const { data: lib } = useQuery<Library>({
    queryKey: ["library"],
    queryFn: api.library,
    enabled: false,
  });
  return useMemo(() => {
    const set = new Set((tracks ?? []).map((t) => t.path));
    if (!lib || !tracks?.some((t) => t.mbid)) return set;
    const nowAt = new Map<string, string>();
    for (const a of lib.artists ?? []) {
      for (const al of a.albums ?? []) {
        for (const t of al.tracks ?? []) {
          const key = trackIdentity(t.path, t.tags.MUSICBRAINZ_TRACKID);
          if (key.startsWith("mb:")) nowAt.set(key, t.path);
        }
      }
    }
    for (const t of tracks ?? []) {
      const now = t.mbid ? nowAt.get(t.key) : undefined;
      if (now) set.add(now);
    }
    return set;
  }, [tracks, lib]);
}
