import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, getToken, isOffline, serverUrl } from "../api";
import { isVideoFile } from "./fmt";
import type { Library, Track } from "../types";

/**
 * Offline media cache: "Download" in the app caches a track's audio into
 * the browser's Cache Storage so playback keeps working without the
 * server (a registered service worker serves cached streams; a shell, which
 * has none, gets the bytes as a `blob:` URL — see `playbackSource`). Saving a
 * file to disk is Export's job, not Download's.
 *
 * The cache key is the exact URL the player element requests, so the
 * service worker's cache.match hits on ordinary playback. That URL is built
 * from a file PATH, though, and organizing the library is what this app does
 * — a moved file leaves its download at a key nothing asks for any more.
 * Beside the bytes lives an identity index (below): MusicBrainz recording id
 * → where its bytes actually sit, so a rename cannot orphan a download.
 *
 * Which bytes: the library's own, unless `download_codec` names a target —
 * then `/api/stream?download=1` re-encodes the track for the cache (the
 * library file is untouched), and the queue drops the bulk route, which can
 * only frame files that already exist. `playback_source` decides which of
 * the two the PLAYER takes while both are available.
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

/** An absolute URL for an API path this module builds by hand. `absolute()`
 *  cannot be used for those: in the Tauri shell the document's own origin is
 *  `tauri://localhost` while the backend lives at api.ts's serverUrl, so a
 *  relative path resolved against the document would name a URL nothing ever
 *  requests (and the warmed entry is then never found again). On the web app
 *  serverUrl() is "" and this is the origin that served the page. */
function apiAbsolute(path: string): string {
  return new URL(path, serverUrl() || window.location.href).toString();
}

/** `url` without the session token, as a CACHE KEY.
 *
 *  In a shell every media URL carries `?token=…` (a webview is not the origin
 *  that holds the session cookie, and a media element cannot set a header),
 *  and a token is re-issued at every sign-in. A key that carries one stops
 *  naming the bytes it stored the moment the user signs in again: a downloaded
 *  album reads as undownloaded with all of its bytes still in the cache. The
 *  token is authorization, never identity, so it is dropped from the key and a
 *  download outlives the session that made it.
 *
 *  Textual rather than re-serialized through `URL`/`URLSearchParams`, which
 *  would rewrite a space as `+` where `streamUrl` wrote `%20` — and the key
 *  would then miss the very request the service worker matches (the element's
 *  own URL). */
function cacheKey(url: string): string {
  return url.replace(/([?&])token=[^&]*/i, "$1").replace(/[?&]$/, "");
}

/** The URL the PLAYER asks for — and with it the key a track's bytes are
 *  cached under: the service worker matches the element's own request, so the
 *  download is what makes offline playback work. */
function playbackUrl(path: string): string {
  return cacheKey(absolute(isVideoFile(path) ? api.videoStreamUrl(path) : api.streamUrl(path)));
}

/** Headers every download request carries: the session, as the app's own
 *  `json()` sends it. The gate is off for a loopback browser, but the Tauri
 *  shells and any client on a LAN need the token. */
function authHeaders(extra?: HeadersInit): Headers {
  const headers = new Headers(extra);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return headers;
}

/** Every URL a track's stream may have been cached under: the direct one,
 * plus the live transcode for videos (the player retries a failed direct
 * stream through it). Lets lookups and removals work without a probe, so
 * "Downloaded" survives a server that is down. */
function cacheUrls(path: string): string[] {
  if (!isVideoFile(path)) return [playbackUrl(path)];
  return [
    cacheKey(absolute(api.videoStreamUrl(path))),
    cacheKey(absolute(api.videoStreamUrl(path, true))),
  ];
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
  return [
    cacheKey(absolute(api.coverUrl(album))),
    cacheKey(absolute(api.artistImageUrl(parentDir(album)))),
  ];
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
    apiAbsolute(`/api/album?path=${encodeURIComponent(album)}`),
    apiAbsolute(`/api/artist?path=${encodeURIComponent(parentDir(album))}`),
    apiAbsolute(`/api/credits?album=${encodeURIComponent(album)}`),
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
    // Only a FULL body can be stored: `resp.ok` would accept a 206 as well,
    // and Cache Storage refuses those. Nothing warmed here is ever asked for
    // as a range, so any other answer is one this cache cannot hold.
    if (resp.status !== 200) return;
    await c.put(url, resp);
  } catch {
    /* offline or absent — the audio cache is what matters */
  }
}

/** How long one track's stream may take before the attempt is abandoned.
 *  Without this the fetch had no deadline at all: a stalled server left the
 *  button spinning until the browser gave up (minutes), and the track was
 *  reported as failed with nothing to show for the wait. */
const STREAM_TIMEOUT_MS = 120_000;

/** A download failure, and whether another attempt is worth making.
 *
 *  `retryable` is what the queue reads: a dropped connection, a 5xx or a
 *  partial answer are worth one more try, while "no such file" or "not signed
 *  in" would only say the same thing again. */
class DownloadError extends Error {
  readonly retryable: boolean;

  constructor(message: string, retryable = false) {
    super(message);
    this.name = "DownloadError";
    this.retryable = retryable;
  }
}

/** The caller's cancel joined with our own deadline: whichever fires first
 *  aborts the one signal the fetch sees. Hand-rolled rather than
 *  `AbortSignal.any` so a browser that predates it still downloads. */
function withDeadline(signal?: AbortSignal): AbortSignal {
  const timeout = AbortSignal.timeout(STREAM_TIMEOUT_MS);
  if (!signal) return timeout;
  const ctrl = new AbortController();
  const stop = () => ctrl.abort();
  signal.addEventListener("abort", stop, { once: true });
  timeout.addEventListener("abort", stop, { once: true });
  return ctrl.signal;
}

/** One field of a JSON value, when that value is an object carrying it — the
 *  boundary every framing field is read through, so an unexpected shape reads
 *  as a missing field instead of a TypeError. */
function prop(value: unknown, name: string): unknown {
  if (!value || typeof value !== "object" || !(name in value)) return undefined;
  return Reflect.get(value, name);
}

/** What a refused download answer means, in words the user can act on — with
 *  the server's own `detail` when it sent one. */
async function describeStatus(resp: Response): Promise<string> {
  let detail = "";
  try {
    const body: unknown = await resp.json();
    const sent = prop(body, "detail");
    detail = typeof sent === "string" ? sent : "";
  } catch {
    /* not JSON: the status is all there is to say */
  }
  const tail = detail ? ` — ${detail}` : "";
  if (resp.status === 401 || resp.status === 403) return `not signed in, or the session expired${tail}`;
  if (resp.status === 404) return `the server has no file at that path${tail}`;
  if (resp.status === 416) return `the server could not read that file${tail}`;
  return `the server answered ${resp.status}${tail}`;
}

/** The stream response as the full 200 a cache entry can hold, or null when it
 *  is genuinely partial.
 *
 *  Cache Storage takes a 200 and nothing else — `Cache.put()` throws on a 206
 *  ("Cache got basic response with bad status 206"), which is exactly how
 *  every download used to fail. A 206 that happens to BE the whole file (what
 *  `Range: bytes=0-` comes back as, and what the service worker's own media
 *  branch produces) carries every byte and is rebuilt as the 200 the cache
 *  wants; a real slice is refused, because storing it would leave the player a
 *  truncated track with nothing in the cache to say why. */
async function asFullBody(resp: Response): Promise<Response | null> {
  if (resp.status === 200 && !resp.headers.get("content-range")) return resp;
  const range = /^bytes\s+(\d+)-(\d+)\/(\d+|\*)$/i.exec((resp.headers.get("content-range") || "").trim());
  let body: ArrayBuffer;
  try {
    body = await resp.arrayBuffer();
  } catch (e) {
    // Reading the body is where a connection dropped mid-transfer shows up,
    // and it shows up as the browser's own TypeError. A fresh request can
    // still deliver the file, so this is a failure worth one more attempt.
    throw new DownloadError(
      `the download was cut off mid-file${e instanceof Error && e.message ? ` (${e.message})` : ""}`,
      true
    );
  }
  const total = range && range[3] !== "*" ? Number(range[3]) : 0;
  if (!range || Number(range[1]) !== 0 || !total || body.byteLength !== total) return null;
  const headers = new Headers({ "Content-Length": String(body.byteLength) });
  const mime = resp.headers.get("content-type");
  if (mime) headers.set("Content-Type", mime);
  return new Response(body, { status: 200, headers });
}

/** Whether the browser's cache REFUSED a response rather than losing the
 *  transfer under it — the one case a second attempt cannot change.
 *
 *  The Cache API reports both the same way: a thrown TypeError whose message
 *  is the browser's own wording ("...status code 206 is unsupported", "quota
 *  exceeded", "Cache got basic response with bad status 206" in Firefox). A
 *  transfer that died says something about the network instead, so those words
 *  are the only signal there is — and being wrong the other way (retrying what
 *  a refusal already settled) costs one wasted attempt, while being wrong THIS
 *  way loses a track the user could have had. */
function cacheRefused(message: string): boolean {
  return /quota|status|partial|unsupported|exceeded/i.test(message);
}

/** Fetch one track's WHOLE audio.
 *
 *  The URL is the stream endpoint asked for the full file (`download=1`), so
 *  the range path that answered the old request with a 206 — and the range
 *  path the service worker keeps for playback — is out of the way; `no-store`
 *  keeps a browser's HTTP cache from handing back whatever partial entry a
 *  media element left at this origin. A 206 is still handled rather than
 *  trusted (see asFullBody), because a download must not depend on every hop
 *  honouring `download=1`. */
async function fetchStream(path: string, signal?: AbortSignal): Promise<Response> {
  const url = api.streamUrl(path);
  const full = absolute(`${url}${url.includes("?") ? "&" : "?"}download=1`);
  let resp: Response;
  try {
    resp = await fetch(full, {
      cache: "no-store",
      credentials: "include",
      headers: authHeaders(),
      signal: withDeadline(signal),
    });
  } catch (e) {
    if (signal?.aborted) throw e; // the user's own cancel, not a failure
    throw new DownloadError(
      `could not reach the server${e instanceof Error && e.message ? ` (${e.message})` : ""}`,
      true
    );
  }
  if (!resp.ok) {
    const retryable = resp.status >= 500 || resp.status === 408 || resp.status === 429;
    throw new DownloadError(await describeStatus(resp), retryable);
  }
  const whole = await asFullBody(resp);
  if (!whole) {
    throw new DownloadError(
      `the server answered ${resp.status} Partial Content to a full download` +
        ` (${resp.headers.get("content-range") || "no range given"}) — the request went through the media range path`,
      true
    );
  }
  return whole;
}

/** Move one track's response into the cache, under the key playback uses. */
async function storeStream(c: Cache, path: string, resp: Response): Promise<void> {
  const key = playbackUrl(path);
  const want = Number(resp.headers.get("content-length") || 0);
  try {
    await c.put(key, resp);
  } catch (e) {
    // put() consumes the body, so this is also where a connection that died
    // mid-transfer surfaces — with the browser's own wording either way
    // ("Cache got basic response with bad status 206" names the refused
    // status; a lost transfer names the network). Say which happened in the
    // download's own terms, and retry the ones a fresh request can fix.
    const message = e instanceof Error ? e.message : String(e);
    throw new DownloadError(`the browser could not store the download: ${message}`, !cacheRefused(message));
  }
  // A short body is worse than no body: the service worker would serve a
  // truncated stream as if it were the whole track, and the player would fail
  // at the end of it with the server long gone. Both the server and the cache
  // entry carry a Content-Length, so the mismatch is visible without reading
  // the payload back.
  const got = Number((await c.match(key))?.headers.get("content-length") || 0);
  if (want && got && want !== got) {
    await c.delete(key);
    throw new DownloadError(`the download was cut short (${got} of ${want} bytes)`, true);
  }
}

/** Everything a downloaded track drags along: the artwork and payloads its
 *  pages need offline, and the identity row that keeps it attached to the
 *  track once the organizer moves the file. */
async function finishTrack(c: Cache, path: string): Promise<void> {
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
  const url = playbackUrl(path);
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

/** Cache one track's audio for offline playback, once. */
export async function cacheTrack(path: string, opts: { signal?: AbortSignal } = {}): Promise<void> {
  const c = await cache();
  await storeStream(c, path, await fetchStream(path, opts.signal));
  await finishTrack(c, path);
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

/** Bytes of every stored entry (Content-Length sums), keyed by the URL it is
 *  filed at — the per-key form of the storage readout, so a view can size ONE
 *  album of the cache and not only the whole of it. The total is the sum of
 *  these: one sweep of Cache Storage answers both. */
export async function cachedEntrySizes(): Promise<Record<string, number>> {
  try {
    const c = await cache();
    const sizes: Record<string, number> = {};
    for (const req of await c.keys()) {
      const r = await c.match(req);
      if (!r) continue;
      const len = r.headers.get("content-length");
      sizes[req.url] = len ? Number(len) : (await r.clone().blob()).size;
    }
    return sizes;
  } catch {
    return {}; // Cache Storage unavailable (insecure context)
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

/** The source a player element should load for one track, and whether that is
 *  the downloaded copy.
 *
 *  Every player surface goes through here — the audio elements, their gapless
 *  preload, the lyric previews and the video popout — so one rule decides
 *  which bytes play, and `playback_source` (stream | downloaded, shipped
 *  `stream`) cannot be honored in one place and ignored in another.
 *
 *  The downloaded copy is taken when the setting says so, and ALWAYS when the
 *  server cannot be reached (`isOffline()`): with the API answering from its
 *  own cache the copy is the only thing that plays, so a preference must never
 *  strand the player. `opts.transcode` (the video popout's live transcode) is
 *  a different rendition of the track, so a copy — the DIRECT stream's bytes —
 *  only stands in for it offline.
 *
 *  A stream URL that a downloaded copy exists for carries `nocache=1`: the
 *  service worker's media branch is cache-first and matches the element's own
 *  URL, so without it "prefer streaming" would play the very bytes it is
 *  asking to avoid. The server ignores the parameter, and nothing is stored
 *  under that URL, so it costs one miss and nothing else. */
export async function playbackSource(
  target: CacheTarget,
  opts: { video?: boolean; transcode?: boolean } = {}
): Promise<{ src: string; cached: boolean }> {
  const { path } = targetOf(target);
  const cached = await offlineMediaUrl(target);
  const offline = isOffline();
  if (cached && (offline || (!opts.transcode && (await prefersDownloaded())))) {
    return { src: cached, cached: true };
  }
  const url = opts.video ? api.videoStreamUrl(path, !!opts.transcode) : api.streamUrl(path);
  return { src: cached ? streamingNoCache(url) : url, cached: false };
}

/** `url` marked so a cache-first service worker must go to the network for it.
 *  Absolute, because the marker is compared against the warmed keys. */
function streamingNoCache(url: string): string {
  return absolute(`${url}${url.includes("?") ? "&" : "?"}nocache=1`);
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
    const key = cacheKey(absolute(url));
    // A URL carrying a cover version (`&v=`) names a SPECIFIC image, and the
    // warmed entry is whatever the download stored — possibly an earlier
    // version of the same file. It is therefore only consulted as itself: no
    // entry means the network URL paints, which is the fresh one. Matching it
    // against the version-less key would resurrect the image the write just
    // replaced.
    const versioned = key.includes("&v=");
    // Otherwise the warmed key is the plain `?album=` the download wrote (see
    // artworkUrls), while what is rendered may add `&file=` (a track's own
    // art) or `&staged=` — neither of which the cache knows. Cut at the FIRST
    // of them rather than `searchParams.delete`: re-serializing would rewrite
    // `%20` as `+` and never match the warmed key for an album path with a
    // space in it.
    let cut = -1;
    for (const part of ["&file=", "&staged="]) {
      const at = key.indexOf(part);
      if (at > 0 && (cut < 0 || at < cut)) cut = at;
    }
    const keys = versioned || cut < 0 ? [key] : [key, key.slice(0, cut)];
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

/** Drop an album's cached cover from the offline copy.
 *
 *  That copy is served in preference to the network one (the swap-in above),
 *  which is right for a downloaded album and wrong the moment its cover is
 *  REPLACED: the stored body is the image the user just changed away from, and
 *  it would paint over the new one on every later view. The service worker
 *  keeps its own entries in this same cache, so deleting a key drops it from
 *  both. Called after a cover write (see api.cover/coverFromUrl); the net
 *  loses nothing — the album is re-warmed by its next download. */
export async function forgetAlbumArtwork(
  albumPath: string,
  coverFile?: string | null
): Promise<void> {
  try {
    const c = await cache();
    // `token: null` names the VERSION-LESS key — the one warm() wrote and the
    // page asks for (see artworkUrls). Left to fall back to the remembered
    // version, these would resolve to the URL the write just recorded, and the
    // stale key would survive.
    const plain = absolute(api.coverUrl(albumPath, null, { token: null }));
    const keys = [plain];
    if (coverFile) keys.push(absolute(api.coverUrl(albumPath, coverFile, { token: null })));
    // A version an EARLIER write left behind is dead weight — every future
    // write carries a new token, so nothing can name it again, and a cover is
    // megabytes of it. The prefix cannot reach `&color=1`: that entry, the
    // album's tint, has no `&file=`.
    for (const req of await c.keys()) {
      if (req.url.startsWith(`${plain}&file=`)) keys.push(req.url);
    }
    for (const key of keys) {
      await c.delete(key);
      revoke(key);
    }
  } catch {
    /* no Cache Storage (insecure context): the network copy is the only one */
  }
}

/** The query key for the cached-track snapshot. One key, so the Downloads
 *  page, the download controls and every downloaded mark on a title read the
 *  SAME list — a download anywhere shows up everywhere on the next
 *  invalidation. (The stored value is `cachedTracks()`, identity rows
 *  included.) */
export const CACHED_PATHS_KEY = ["cachedPaths"] as const;

/** The query key for the cache's byte sizes. Its own key rather than a value
 *  hung off the track snapshot: it is the one readout that costs a sweep of
 *  Cache Storage, so it is only asked for by the page that shows bytes, and a
 *  download or a removal invalidates it just as explicitly. */
export const CACHED_SIZES_KEY = ["cachedSizes"] as const;

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
    queryFn: () => api.library(),
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

/* ------------------------------------------------------------------ *
 * The download queue
 * ------------------------------------------------------------------ */

/** The shipped `download_concurrency`, and the ceiling the config loader
 *  enforces on it. A queue wider than that mostly thrashes the disk and the
 *  socket, and the browser caps a handful of connections per origin anyway. */
const DEFAULT_CONCURRENCY = 3;
const MAX_CONCURRENCY = 8;

/** Tracks per bulk request. The endpoint accepts 500; a smaller chunk keeps a
 *  dropped connection from costing the whole selection, and keeps the JSON
 *  header line that names them short. */
const BULK_CHUNK = 100;

/** How many tracks a run has finished, which one is in flight, and how many
 *  have failed — what a download control shows while it works. */
export type DownloadProgress = {
  done: number;
  total: number;
  current: string | null;
  failed: number;
};

export type DownloadFailure = { path: string; message: string };

export type DownloadReport = {
  /** Tracks this run cached. */
  done: number;
  /** One row per track that could not be cached, with the real reason. */
  failures: DownloadFailure[];
  /** True when the user stopped the run before the queue was empty. */
  cancelled: boolean;
};

/** The run in flight, so a second press can stop it. One at a time on purpose:
 *  two runs over the same tracks would double the load and report the same
 *  failures twice. */
let activeRun: AbortController | null = null;

/** Stop the running download: in-flight requests are aborted and the queue
 *  empties. Tracks that already landed stay cached. */
export function cancelDownloads(): void {
  activeRun?.abort();
}

/** `download_concurrency` from the server config, clamped. The app's query
 *  cache already holds the payload, but this module has no React context to
 *  read it from, so it asks — an ordinary GET, cached like any other. */
async function queueWidth(explicit?: number): Promise<number> {
  if (explicit && explicit > 0) return Math.min(MAX_CONCURRENCY, Math.max(1, Math.trunc(explicit)));
  try {
    const n = Number((await api.config())?.download_concurrency);
    return n > 0 ? Math.min(MAX_CONCURRENCY, Math.max(1, Math.trunc(n))) : DEFAULT_CONCURRENCY;
  } catch {
    return DEFAULT_CONCURRENCY; // server unreachable: the default is still a queue
  }
}

/** Whether this server re-encodes downloads (`download_codec` other than
 *  `copy`). The rendition is produced by `/api/stream?download=1` itself, so
 *  the only thing the client has to know is that the bulk route cannot carry
 *  one: it frames each file's size up front, and a re-encode has none until it
 *  is done. Read the same way `queueWidth` reads its key — an ordinary GET the
 *  query cache already holds. */
async function encodesDownloads(): Promise<boolean> {
  try {
    const codec = String((await api.config())?.download_codec ?? "copy").trim().toLowerCase();
    return !!codec && codec !== "copy";
  } catch {
    return false; // server unreachable: the download itself will say why
  }
}

/** `playback_source` from the server config: true when this app plays a
 *  downloaded copy in preference to streaming the library file. `stream` — the
 *  shipped default — asks the server even for a track that IS downloaded. */
async function prefersDownloaded(): Promise<boolean> {
  try {
    const want = String((await api.config())?.playback_source ?? "stream").trim().toLowerCase();
    return want === "downloaded";
  } catch {
    return false; // server unreachable: streaming is the default anyway
  }
}

const UTF8 = new TextDecoder();

/** One file of a bulk header line, as the server described it. */
type BulkFile = { path: string; size: number; mime: string; error: string | null };

/** A cursor that hands out exactly the bytes the framing promised.
 *
 *  A bulk body is one JSON header line followed by the files' bytes back to
 *  back, so a reader that could not ask for an exact count would have to hold
 *  the whole batch. Running out before the promised count is fatal for the
 *  batch: every following file would start wherever the missing bytes ended. */
class BodyReader {
  private readonly reader: ReadableStreamDefaultReader<Uint8Array<ArrayBuffer>>;
  private buffer = new Uint8Array(0);

  constructor(body: ReadableStream<Uint8Array<ArrayBuffer>>) {
    this.reader = body.getReader();
  }

  private async fill(want: number): Promise<void> {
    while (this.buffer.byteLength < want) {
      let chunk: ReadableStreamReadResult<Uint8Array<ArrayBuffer>>;
      try {
        chunk = await this.reader.read();
      } catch (e) {
        // The connection died under the batch: the reader's own TypeError,
        // which a fresh request may well not repeat.
        throw new DownloadError(
          `the download stream was cut off${e instanceof Error && e.message ? ` (${e.message})` : ""}`,
          true
        );
      }
      const { done, value } = chunk;
      if (done) return;
      if (!value || !value.byteLength) continue;
      const grown = new Uint8Array(this.buffer.byteLength + value.byteLength);
      grown.set(this.buffer, 0);
      grown.set(value, this.buffer.byteLength);
      this.buffer = grown;
    }
  }

  /** The next line, up to and excluding its newline. */
  async line(): Promise<string> {
    for (;;) {
      const end = this.buffer.indexOf(10);
      if (end >= 0) {
        const out = this.buffer.slice(0, end);
        this.buffer = this.buffer.slice(end + 1);
        return UTF8.decode(out);
      }
      const had = this.buffer.byteLength;
      await this.fill(had + 1);
      if (this.buffer.byteLength === had) {
        throw new DownloadError("the download stream ended before it named the files");
      }
    }
  }

  /** Exactly `want` bytes. */
  async take(want: number): Promise<ArrayBuffer> {
    await this.fill(want);
    if (this.buffer.byteLength < want) {
      throw new DownloadError(
        `the download stream ended early — ${this.buffer.byteLength} of ${want} bytes of the track had not arrived`
      );
    }
    const out = new ArrayBuffer(want);
    new Uint8Array(out).set(this.buffer.subarray(0, want));
    this.buffer = this.buffer.slice(want);
    return out;
  }

  /** Give up on the rest of the body (a cancel, or a batch gone wrong). */
  close(): void {
    void this.reader.cancel().catch(() => undefined);
  }
}

/** The batch description out of a bulk header line. A shape this version does
 *  not know is refused rather than guessed: guessing would file the wrong
 *  bytes under a track's key. */
function parseBulkHeader(line: string): BulkFile[] {
  let parsed: unknown;
  try {
    parsed = JSON.parse(line);
  } catch {
    throw new DownloadError("the download stream did not start with a file list");
  }
  const files = prop(parsed, "files");
  if (!Array.isArray(files)) throw new DownloadError("the download stream did not start with a file list");
  return files.map((raw) => {
    const size = prop(raw, "size");
    const error = prop(raw, "error");
    return {
      path: String(prop(raw, "path") ?? ""),
      size: Math.max(0, Math.trunc(Number(size)) || 0),
      mime: String(prop(raw, "mime") ?? ""),
      error: error ? String(error) : null,
    };
  });
}

/** Cache ONE chunk through the bulk endpoint.
 *
 *  Returns the paths whose bytes did not land — they are then fetched one by
 *  one, where a failure carries the single-track error and gets its retry — or
 *  null when the batch cannot be used at all (no such route on this server, a
 *  server having a bad moment, an unusable body), which sends the queue to the
 *  per-track pool. */
async function bulkChunk(
  chunk: string[],
  signal: AbortSignal,
  report: DownloadReport,
  progress: (current: string | null) => void
): Promise<string[] | null> {
  let resp: Response;
  try {
    resp = await fetch(apiAbsolute("/api/media/bulk"), {
      method: "POST",
      cache: "no-store",
      credentials: "include",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ paths: chunk }),
      signal,
    });
  } catch (e) {
    if (signal.aborted) throw e;
    return null; // no answer at all: the per-track path says why
  }
  // A server without the bulk route (404/405) or one that finds this chunk too
  // large (413) still serves every track — one request each. So does a server
  // having a bad moment (a 5xx, or a 429 telling us to slow down): the tracks
  // are still downloadable, just one request at a time.
  if (resp.status === 404 || resp.status === 405 || resp.status === 413) return null;
  if (resp.status >= 500 || resp.status === 408 || resp.status === 429) return null;
  // Anything else is the server's final answer (a 4xx): asking again per track
  // would repeat it, so it travels as one reason for the chunk.
  if (!resp.ok) throw new DownloadError(await describeStatus(resp));
  if (!resp.body) throw new DownloadError("the server sent no body for the download batch");

  const reader = new BodyReader(resp.body);
  let files: BulkFile[];
  try {
    files = parseBulkHeader(await reader.line());
  } catch {
    // Nothing usable arrived — no header, or one this version cannot read.
    // Every track in the chunk is still downloadable on its own, where the
    // failure is reported per track with the response it got.
    reader.close();
    return null;
  }
  const c = await cache();
  const missed: string[] = [];
  for (let i = 0; i < files.length; i++) {
    const file = files[i];
    if (signal.aborted) {
      reader.close();
      return missed;
    }
    progress(file.path);
    // A path the server could not read (gone, or outside the library) is not
    // reported here: the per-track path words that failure better than the
    // header can.
    if (file.error) {
      missed.push(file.path);
      continue;
    }
    let data: ArrayBuffer;
    try {
      data = await reader.take(file.size);
    } catch {
      // Short by however much was missing: every following file would start in
      // the wrong place, so the rest of the batch goes the slow way — where a
      // dropped connection gets the retry the batch could not give it.
      reader.close();
      missed.push(...files.slice(i).map((f) => f.path));
      return missed;
    }
    const headers = new Headers({ "Content-Length": String(data.byteLength) });
    if (file.mime) headers.set("Content-Type", file.mime);
    try {
      await storeStream(c, file.path, new Response(data, { status: 200, headers }));
      report.done += 1;
      await finishTrack(c, file.path);
      progress(file.path);
    } catch {
      // The cache would not take them (or the body was cut short): either way
      // this file goes the slow way, where the failure is reported per track.
      missed.push(file.path);
    }
  }
  return missed;
}

/** Cache one track, trying once more when the failure looked transient: a
 *  dropped connection or a 5xx is worth a second attempt, while a 404 ("no
 *  such file") or a 401 would only say the same thing again. */
async function cacheWithRetry(path: string, signal: AbortSignal): Promise<void> {
  try {
    await cacheTrack(path, { signal });
  } catch (e) {
    if (signal.aborted || !(e instanceof DownloadError) || !e.retryable) throw e;
    await cacheTrack(path, { signal });
  }
}

/** Cache `paths` one request each, `width` of them at a time.
 *
 *  Bounded on purpose: a 5000-track selection must not open 5000 requests at
 *  once. Workers take the next index off a shared counter, so the queue drains
 *  in order and every track is attempted once. */
async function runPool(
  paths: string[],
  width: number,
  signal: AbortSignal,
  report: DownloadReport,
  progress: (current: string | null) => void
): Promise<void> {
  let next = 0;
  const worker = async () => {
    for (;;) {
      const index = next++;
      if (index >= paths.length || signal.aborted) return;
      const path = paths[index];
      progress(path);
      try {
        await cacheWithRetry(path, signal);
        report.done += 1;
        progress(path);
      } catch (e) {
        if (signal.aborted) return;
        report.failures.push({ path, message: e instanceof Error ? e.message : String(e) });
      }
    }
  };
  await Promise.all(Array.from({ length: Math.max(1, Math.min(width, paths.length)) }, worker));
}

/** Which of `paths` are already cached, probed a few at a time: an album of
 *  5000 tracks must not fire 5000 Cache Storage lookups at once. */
export async function cachedFlags(paths: string[]): Promise<boolean[]> {
  const out: boolean[] = new Array(paths.length).fill(false);
  let next = 0;
  const worker = async () => {
    for (;;) {
      const index = next++;
      if (index >= paths.length) return;
      out[index] = await isTrackCached(paths[index]);
    }
  };
  await Promise.all(Array.from({ length: Math.max(1, Math.min(8, paths.length)) }, worker));
  return out;
}

/** Cache every track in `paths` for offline playback.
 *
 *  The work happens in two shapes, both bounded by `download_concurrency`:
 *  chunks of the queue through the bulk endpoint (one response, the server's
 *  own pool reading them N at a time), and — for whatever the bulk route
 *  cannot carry (a configured download rendition, a server without it, or a
 *  stream that dies mid-batch) — the per-track pool, N requests at a time.
 *
 *  Never throws for a track that failed: every failure comes back in the
 *  report with its reason, because one dead file must not abandon the rest of
 *  a selection. A cancel stops what is in flight and empties the queue; what
 *  already landed stays cached. */
export async function downloadTracks(
  paths: string[],
  opts: { onProgress?: (p: DownloadProgress) => void; concurrency?: number } = {}
): Promise<DownloadReport> {
  const total = paths.length;
  const report: DownloadReport = { done: 0, failures: [], cancelled: false };
  const ctrl = new AbortController();
  activeRun?.abort();
  activeRun = ctrl;
  // Reports are coalesced: a 5000-track queue would otherwise re-render the
  // control once per track start AND once per track end. The count is what the
  // user reads, so a second apart is plenty — and the last one always lands,
  // because the run's own toast is built from the report, not from this.
  let reportedAt = 0;
  const progress = (current: string | null) => {
    if (!opts.onProgress) return;
    const at = Date.now();
    if (current !== null && at - reportedAt < 250) return;
    reportedAt = at;
    opts.onProgress({ done: report.done, total, current, failed: report.failures.length });
  };
  const pending = [...paths];
  try {
    const width = await queueWidth(opts.concurrency);
    if (await encodesDownloads()) {
      // A download rendition: the bulk route frames each file's SIZE up front
      // and so can only carry bytes that already exist — and it refuses while
      // one is configured. Every track goes one request at a time, where
      // /api/stream?download=1 does the encoding.
      await runPool(pending.splice(0), width, ctrl.signal, report, progress);
    } else {
      while (pending.length && !ctrl.signal.aborted) {
        const chunk = pending.splice(0, BULK_CHUNK);
        progress(null);
        const missed = await bulkChunk(chunk, ctrl.signal, report, progress);
        if (missed === null || missed.length) {
          // Not (all) carried: the rest of the queue goes one request per track.
          await runPool([...(missed ?? chunk), ...pending.splice(0)], width, ctrl.signal, report, progress);
        }
      }
    }
  } catch (e) {
    if (!ctrl.signal.aborted) {
      report.failures.push({ path: "", message: e instanceof Error ? e.message : String(e) });
    }
  } finally {
    if (activeRun === ctrl) activeRun = null;
    report.cancelled = ctrl.signal.aborted;
  }
  return report;
}
