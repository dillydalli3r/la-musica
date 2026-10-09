/**
 * The offline copy of the JSON the app reads.
 *
 * Every page is built from a handful of GET payloads (the library, an album,
 * an artist, credits…). This keeps the last successful answer for each of
 * them in localStorage, so a client whose server cannot be reached still
 * renders what it last saw instead of a wall of failed requests.
 *
 * localStorage rather than Cache Storage: the shells (desktop, iOS, Android)
 * have no service worker to answer a request out of Cache Storage, and this
 * cache is consulted by the `fetch`-then-parse path in api.ts, which wants a
 * parsed value, not a stored Response. A few hundred KB of JSON survives a
 * reload and a cold start, which is the whole point.
 *
 * Nothing here may throw at the caller: this runs on the request path, and
 * private mode, a disabled storage or a full quota must all degrade to "no
 * cache off this endpoint", never to a broken request.
 */

/** Browsers count these strings in UTF-16 code units against a ~5 MB quota
 *  per origin, and the JSON kept here is ASCII, so one character is about one
 *  byte. The budget leaves room for the index and for the app's own
 *  localStorage keys (the server address and the session token). */
const MAX_ENTRY_BYTES = 512 * 1024;
const BUDGET_BYTES = 3 * 1024 * 1024;

/** A payload this wide is skipped BEFORE it is serialized: a 50k-track
 *  library would be a multi-megabyte string, and building it only to throw it
 *  away is the one cost this hot path must not pay. Anything not an array of
 *  rows is measured after serializing instead — there is no cheaper bound. */
const MAX_ROWS = 4000;

const PREFIX = "mlo.cache.";
const INDEX_KEY = `${PREFIX}CACHE_INDEX`;

interface EntryMeta {
  /** Epoch ms of the write; prune() evicts the oldest of these first. */
  at: number;
  bytes: number;
}
type Index = Record<string, EntryMeta>;

/** The store key for a URL: path plus query, origin dropped. Data is
 *  per-server (see setServerUrl's clear), and dropping the origin is what
 *  lets the web app's relative URLs and a shell's absolute ones name the same
 *  entry. */
export function cacheKey(url: string): string {
  try {
    const u = new URL(url, typeof location === "undefined" ? undefined : location.href);
    return u.pathname + u.search;
  } catch {
    return url;
  }
}

function readIndex(): Index {
  try {
    const raw = localStorage.getItem(INDEX_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    return parsed && typeof parsed === "object" ? (parsed as Index) : {};
  } catch {
    return {}; // unreadable or disabled storage: account for nothing
  }
}

function writeIndex(index: Index): void {
  try {
    localStorage.setItem(INDEX_KEY, JSON.stringify(index));
  } catch {
    /* full or disabled: the bodies still read back, only eviction is blind */
  }
}

/** Evict oldest-first until `index` fits the budget. `keep` is never dropped
 *  (the entry just written is worth more than the one before it). Returns the
 *  keys that left, so their bodies can go too. */
function pruneIndex(index: Index, keep: string): string[] {
  const keys = Object.keys(index);
  let total = keys.reduce((n, k) => n + index[k].bytes, 0);
  if (total <= BUDGET_BYTES) return [];
  const dropped: string[] = [];
  for (const k of keys.sort((a, b) => index[a].at - index[b].at)) {
    if (total <= BUDGET_BYTES) break;
    if (k === keep) continue;
    total -= index[k].bytes;
    delete index[k];
    dropped.push(k);
  }
  return dropped;
}

/** Remember one payload. Returns the bytes stored, 0 when it was skipped. */
export function put(key: string, value: unknown): number {
  if (Array.isArray(value) && value.length > MAX_ROWS) return 0;
  let body: string;
  try {
    body = JSON.stringify(value);
  } catch {
    return 0; // cyclic or unserializable: nothing to keep
  }
  if (body.length > MAX_ENTRY_BYTES) return 0;

  const index = readIndex();
  index[key] = { at: Date.now(), bytes: body.length };
  const dropped = pruneIndex(index, key);
  let ok = true;
  try {
    for (const k of dropped) localStorage.removeItem(PREFIX + k);
    localStorage.setItem(PREFIX + key, body);
  } catch {
    // Quota (or storage turned off between calls): drop this entry. Failing
    // an offline copy must not fail the request that produced it.
    ok = false;
    delete index[key];
  }
  writeIndex(index);
  return ok ? body.length : 0;
}

/** The stored payload, or null when there is none (a cached JSON `null` would
 *  be indistinguishable — no endpoint here answers with one). */
export function get<T>(key: string): T | null {
  try {
    const body = localStorage.getItem(PREFIX + key);
    return body === null ? null : (JSON.parse(body) as T);
  } catch {
    return null;
  }
}

/** When this endpoint's copy was saved (epoch ms), null when nothing is kept. */
export function cachedAt(key: string): number | null {
  return readIndex()[key]?.at ?? null;
}

export function forget(key: string): void {
  try {
    localStorage.removeItem(PREFIX + key);
  } catch {
    /* nothing to remove */
  }
  const index = readIndex();
  if (key in index) {
    delete index[key];
    writeIndex(index);
  }
}

/** Drop every cached payload — used when this client is pointed at another
 *  server, where the old server's library would be the wrong answer. */
export function clearAll(): void {
  try {
    const keys: string[] = [];
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k && k.startsWith(PREFIX)) keys.push(k);
    }
    for (const k of keys) localStorage.removeItem(k);
  } catch {
    /* storage disabled — there is nothing kept */
  }
}

/** What the store holds now, for a storage readout. */
export function stats(): { entries: number; bytes: number } {
  const index = readIndex();
  const keys = Object.keys(index);
  return { entries: keys.length, bytes: keys.reduce((n, k) => n + index[k].bytes, 0) };
}

/** Enforce the budget when nothing is being written (put() prunes too).
 *  Returns how many entries left. */
export function prune(): number {
  const index = readIndex();
  const dropped = pruneIndex(index, "");
  if (!dropped.length) return 0;
  try {
    for (const k of dropped) localStorage.removeItem(PREFIX + k);
  } catch {
    /* the bodies are the garbage; the index above them is what matters */
  }
  writeIndex(index);
  return dropped.length;
}
