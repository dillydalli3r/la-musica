import type {
  AcoustidMatch,
  ArtistArtwork,
  ArtistArtworkDescription,
  ArtistArtworkImage,
  CoverInfo,
  CoverSourceCatalog,
  CoverResult,
  CoverSearch,
  CoverWriteResult,
  DiscoveryCatalog,
  DiscoveryImageRow,
  DownloadEntry,
  DownloadsPayload,
  HomeData,
  ImportBulkJob,
  ImportBulkResult,
  ImportScriptsPreview,
  LayoutReport,
  LyricsAutoResult,
  LyricsHit,
  LyricsProviders,
  MBArtistBrowse,
  MBRecordingBrowse,
  MBSearchRows,
  ScriptRunResult,
  SourceHealth,
  SourceKind,
  SourcesHealth,
  Wish,
  WishesPayload,
} from "./types";
import { toast } from "./store";
import * as offline from "./lib/offlineCache";
import { coverVersion, rememberCoverVersion } from "./lib/invalidate";

// The Tauri shell (desktop, iOS, Android) serves the frontend from
// tauri://localhost, so relative /api paths cannot reach any server: a shell
// has to be told the address, because it hosts no backend of its own to fall
// back on. The setup wizard collects it on first run (`mlo.server`) and
// Settings → Security changes it later; the empty base below therefore means
// "the origin that served this page", which only the browser build can use.
// `in` rather than a cast on `window`: Tauri injects this global into the
// webview before any script runs, and its presence is the whole question.
export const IN_TAURI = "__TAURI_INTERNALS__" in window;

/** True inside the Tauri shell ON A PHONE OR TABLET.
 *
 *  The mobile builds register no Tauri commands at all (see
 *  desktop/src-tauri/capabilities/mobile.json): there is no native folder
 *  dialog to call, and `invoke("pick_folder")` would reject. Anything that
 *  asks the shell for a desktop-only capability must check this first.
 *  iPadOS reports itself as "Macintosh", so a touch-capable Mac is counted as
 *  a tablet — the same case, as far as a folder dialog goes. */
export const IN_MOBILE_SHELL = IN_TAURI && (() => {
  const ua = navigator.userAgent || "";
  return /android|iphone|ipad|ipod/i.test(ua)
    || (/macintosh/i.test(ua) && (navigator.maxTouchPoints || 0) > 1);
})();

const SERVER_KEY = "mlo.server";
const TOKEN_KEY = "mlo.token";

function readStore(key: string): string {
  try {
    return localStorage.getItem(key) || "";
  } catch {
    return ""; // private mode / storage disabled: same-origin + cookie still works
  }
}

function writeStore(key: string, value: string | null) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* nothing to do: the session simply will not survive a reload */
  }
}

/** Normalise a server address the user typed (login screen, the client
 *  wizard's first step, Settings → Security).
 *
 *  A typed address is almost never a URL: people write `example.com:8000` or
 *  paste one with a trailing slash. Scheme-less input gets `http://` — every
 *  LAN server and the shell's own backend speak plain HTTP — except when the
 *  address names port 443, where HTTPS is the only thing that can be
 *  listening. The host is lower-cased and any path or trailing slash is
 *  dropped, because the request base is built by appending `/api`; every
 *  caller shows the result back before saving it. "" stays "" — on the web
 *  app and in Docker that means "the origin that served this page". */
export function normalizeServerUrl(raw: string): string {
  const typed = (raw || "").trim();
  if (!typed) return "";
  const scheme = /^[a-z][a-z0-9+.-]*:\/\//i.test(typed) ? "" : /:443(\/|$)/.test(typed) ? "https://" : "http://";
  try {
    const u = new URL(`${scheme}${typed}`);
    return u.host ? `${u.protocol}//${u.host}` : typed;
  } catch {
    // Not parseable as an address at all — hand back what was typed (minus the
    // trailing slashes the request base would double up) and let the probe
    // report the failure.
    return `${scheme}${typed}`.replace(/\/+$/, "");
  }
}

function resolveBase(): string {
  // "" means "the origin that served this page" (the browser build, where the
  // session cookie is same-site). A shell has no origin to fall back on — it is
  // a client of a server the user named, and until one is saved every request
  // fails, which is exactly the state the setup wizard exists to leave.
  return normalizeServerUrl(readStore(SERVER_KEY));
}

/** The server address this client talks to. "" means "the origin that served
 *  this page" (the web app, where the session cookie is same-site). */
let BASE = resolveBase();
let API = `${BASE}/api`;

/** Point this client at another server — the login screen's address field and
 *  the setup wizard's first step both land here, as does Settings → Security,
 *  which is the only one the web app offers (its address is otherwise fixed to
 *  the origin that served it). This is the one way a client is pointed
 *  anywhere, and the only place a shell gets its address from: no client hosts
 *  a server of its own to fall back on. */
export function setServerUrl(url: string | null) {
  const clean = normalizeServerUrl(url || "");
  writeStore(SERVER_KEY, clean || null);
  // The offline copy is keyed by endpoint, not by server: left in place, a
  // client pointed at a second server would answer from the first one's
  // library the moment the network is gone.
  if (clean !== BASE) offline.clearAll();
  BASE = clean;
  API = `${BASE}/api`;
}

export function serverUrl(): string {
  return BASE;
}

export function getToken(): string {
  return readStore(TOKEN_KEY);
}

export function setToken(token: string | null) {
  writeStore(TOKEN_KEY, token || null);
}

/** The API returns 401 when the session is gone (expired, revoked, or the
 *  server was restarted with a new password). The shell listens for this and
 *  shows the login screen instead of a page full of "failed to load". */
export class AuthError extends Error {
  readonly auth = true;
  constructor(message: string) {
    super(message);
    this.name = "AuthError";
  }
}

type AuthListener = (reason: string) => void;
const authListeners = new Set<AuthListener>();

export function onAuthLost(fn: AuthListener): () => void {
  authListeners.add(fn);
  return () => authListeners.delete(fn);
}

function authLost(reason: string) {
  setToken(null);
  for (const fn of authListeners) {
    try {
      fn(reason);
    } catch {
      /* a listener must never break the request that reported 401 */
    }
  }
}

/** A URL for an <img>/<audio>/<video> src.
 *
 *  A session cookie is same-site only, so a media element served by a
 *  *different* origin — every Tauri build, and any client pointed at a remote
 *  server — cannot send it and cannot set an Authorization header either.
 *  Those clients carry the token in the query string instead. On the web app
 *  (BASE === "") nothing is appended: the cookie does the job and the token
 *  stays out of URLs, logs and history. */
function media(url: string): string {
  if (!BASE) return url;
  const token = getToken();
  if (!token) return url;
  return `${url}${url.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}`;
}

/** `track=` (one file) plus the comma-separated `tracks=` list — each name
 *  URL-encoded on its own, so commas inside a name survive. */
function coverQuery(track?: string, tracks?: string[]): string {
  const list = (tracks ?? []).filter(Boolean);
  return (
    (track ? `&track=${encodeURIComponent(track)}` : "") +
    (list.length ? `&tracks=${list.map(encodeURIComponent).join(",")}` : "")
  );
}

/** Remember the token a cover write reported for the file it wrote, so the
 *  next `coverUrl` for that album+file is a different URL than the previous
 *  image's (see lib/invalidate's cover versions). Every write path — the
 *  album page, the import wizard, the cover finder — goes through the two
 *  methods below, so no caller has to carry the token around.
 *
 *  The album's copy in the offline cache is dropped with it: that copy is
 *  painted IN PREFERENCE to the network one, so a replaced cover would
 *  otherwise keep showing the image it replaced. Imported lazily — the offline
 *  cache itself renders covers (it imports this module), and a write is the
 *  one moment the two need to meet. */
const noteCoverWrite =
  (albumPath: string) =>
  (res: CoverWriteResult): CoverWriteResult => {
    const file = res.path.split("/").pop() ?? null;
    rememberCoverVersion(albumPath, file, res.token);
    void import("./lib/mediaCache").then((m) => m.forgetAlbumArtwork(albumPath, file));
    return res;
  };

/** Set while what the app is showing came out of the offline copy (or out of
 *  the service worker's own) instead of from the server, so a banner can say
 *  "offline — showing what was saved". `at` is when that copy was written;
 *  null when a service worker served it and the write time depends on that
 *  cache's own lifetime. */
export interface OfflineInfo {
  /** The endpoint that could not be reached (path + query). */
  key: string;
  at: number | null;
}
type OfflineListener = (offline: OfflineInfo | null) => void;
const offlineListeners = new Set<OfflineListener>();
let offlineInfo: OfflineInfo | null = null;

/** Watch the offline state. Called on every CHANGE, including the change back
 *  to online (with null); not called during registration — read `isOffline()`
 *  once for the first render. */
export function onOfflineFallback(fn: OfflineListener): () => void {
  offlineListeners.add(fn);
  return () => offlineListeners.delete(fn);
}

/** True while the app is rendering cached answers. */
export function isOffline(): boolean {
  return offlineInfo !== null;
}

function setOffline(info: OfflineInfo | null) {
  if ((offlineInfo === null) === (info === null)) {
    // Same side of the line — a different endpoint, or the same one again.
    // Remember the newest detail but do NOT ping the listeners: a page
    // polling several endpoints while offline (or the service worker marking
    // every GET) would otherwise re-render the banner once per endpoint per
    // second, which on a phone reads as the app refreshing under your thumb.
    offlineInfo = info;
    return;
  }
  offlineInfo = info;
  for (const fn of offlineListeners) {
    try {
      fn(info);
    } catch {
      /* a listener must never break the request that reported the state */
    }
  }
}

/** Endpoints whose answers are never kept on disk.
 *
 *  `/api/auth/*` and `/api/config` are session and secret material: the
 *  config carries API keys and the Soulseek password in clear, and an auth
 *  status answered from disk would show a signed-in app to nobody (or a
 *  signed-out one to somebody with a live session).
 *
 *  The rest are byte streams — one of them a range request in the middle of a
 *  download — so their bodies are not JSON, are per-position, and would be
 *  the largest thing in a 5 MB store. `/api/soulseek/preview*` is a live
 *  transcode for the same reason.
 *
 *  The cover endpoint is listed as itself alone: /api/cover/info,
 *  /api/cover/search and /api/cover/sources are ordinary JSON and cache fine.
 *
 *  `/api/jobs/locks` is the "what is running right now" list: an answer from
 *  disk would show jobs that finished (or never started, after a restart) as
 *  still holding files, which is the one thing that page must never say. */
const NEVER_CACHE_EXACT: Record<string, true> = {
  "/api/config": true,
  "/api/cover": true,
  "/api/stream": true,
  "/api/videos/stream": true,
  "/api/videos/thumb": true,
  "/api/videos/subtitle": true,
  "/api/soulseek/local-file": true,
  "/api/artist/image": true,
  "/api/jobs/locks": true,
};
const NEVER_CACHE_PREFIX = ["/api/auth/", "/api/soulseek/preview"];

/** The offline store key for a request, or null when its answer must not be
 *  kept. Only GETs are cacheable: a write's reply describes a change, not a
 *  state that can be re-read later. */
function cacheable(url: string, init?: RequestInit): string | null {
  if ((init?.method || "GET").toUpperCase() !== "GET") return null;
  const path = offline.cacheKey(url);
  const endpoint = path.split("?")[0];
  if (NEVER_CACHE_EXACT[endpoint]) return null;
  if (NEVER_CACHE_PREFIX.some((p) => endpoint.startsWith(p))) return null;
  return path;
}

/** One place every request goes through: the deadline, the session token, the
 *  401 that means "sign in again", and the offline copy a GET falls back to.
 *
 *  `credentials: "include"` matters for the Tauri/mobile shells: the login
 *  response sets an HttpOnly cookie, and although a cross-site cookie is not
 *  sent on media requests (hence the query token above), the shell still
 *  wants the cookie for anything same-site it happens to do. On the web app
 *  it makes the session cookie travel on every call. */
async function json<T>(url: string, init?: RequestInit, timeoutMs = 20000): Promise<T> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  const token = getToken();
  const headers = new Headers(init?.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const key = cacheable(url, init);
  let r: Response;
  try {
    r = await fetch(url, { credentials: "include", ...init, headers, signal: ctrl.signal });
  } catch (e) {
    // No answer at all: the server is down, the network is gone, or the call
    // ran out of time. (An unreachable server's 4xx/5xx is an answer, and is
    // handled below exactly as before — a rejected request must not be turned
    // into a successful one.) Serve the last answer this endpoint gave.
    if (key) {
      const cached = offline.get<T>(key);
      if (cached !== null) {
        setOffline({ key, at: offline.cachedAt(key) });
        return cached;
      }
    }
    // an aborted fetch is OUR timeout, not the network being down — say which
    if (ctrl.signal.aborted) throw new Error(`no answer within ${Math.round(timeoutMs / 1000)}s`);
    throw e;
  } finally {
    clearTimeout(timer);
  }
  if (!r.ok) {
    let detail = r.statusText;
    let body: Record<string, unknown> = {};
    try {
      body = (await r.json()) as Record<string, unknown>;
      detail = (body.detail as string) || detail;
    } catch {
      /* keep statusText */
    }
    if (r.status === 401 || r.status === 428) {
      // The session is gone, or nobody has claimed this server yet. Both mean
      // "show the login screen" — the shell listens for this instead of
      // rendering a page of failed requests.
      const reason = body.needs_setup ? "setup" : "expired";
      // …except on the endpoints whose 401 means "that password was wrong".
      // Treating those as a lost session signed the user out of Settings
      // mid-edit (and of the login screen itself) while they were typing.
      const passwordRoute = /\/api\/auth\/(login|password|setup)$/.test(url);
      if (r.status === 401 && !passwordRoute) authLost(reason);
      throw new AuthError(String(detail || (reason === "setup" ? "setup required" : "sign in required")));
    }
    throw new Error(detail);
  }
  const body = (await r.json()) as T;
  if (key) offline.put(key, body);
  // The service worker answers an offline API GET out of its own cache with
  // this marker: the fetch succeeded, but the server never saw it, so the app
  // is showing a stored answer all the same. (Readable same-origin, which is
  // the only place that worker runs.)
  setOffline(r.headers.get("X-MLO-Offline") === "1" ? { key: key ?? offline.cacheKey(url), at: null } : null);
  return body;
}

/** Fields a tag-writing endpoint adds when the write re-emitted the file in
 *  another container (.vob/.avi/.webm/.mp4 -> .mkv, streams copied). */
type ContainerSwap = {
  container_changed?: boolean;
  /** Single-file writers: the file that now holds the data. */
  output_path?: string | null;
  /** Multi-file writers: only the files re-emitted as .mkv. */
  output_paths?: string[];
};

/** Surface a container swap on the response it arrives with. Every tag-writing
 *  call funnels through here, so the user is told which file now holds the
 *  data no matter which surface triggered the write. */
function noteContainerSwap<T extends ContainerSwap>(r: T): T {
  const swapped = r.output_paths?.length ? r.output_paths : r.container_changed && r.output_path ? [r.output_path] : [];
  if (swapped.length === 1) {
    toast(`Container changed — the file is now ${swapped[0]} (streams copied, nothing re-encoded)`);
  } else if (swapped.length > 1) {
    toast(`Container changed — ${swapped.length} files re-emitted as MKV: ${swapped.join(", ")}`);
  }
  return r;
}

/** One credit row of `/api/credits`: who did what on a track or an album.
 *  `role` arrives already lower-cased and grouped-ready, `attributes` are the
 *  instrument / vocal part the relation stated, and `mbid` is empty on rows
 *  that came from the files' own tags. */
export interface CreditRow {
  role: string;
  attributes: string[];
  artist: string;
  mbid: string;
}

/** `/api/credits` reply. `source` must be shown next to the rows: a tag
 *  fallback is not MusicBrainz data and must never read as if it were. */
export interface Credits {
  artist: string;
  album: string;
  rows: CreditRow[];
  source: "musicbrainz" | "tags";
  track_mbid?: string;
  release_mbid?: string;
}

/** One codec's spec as the server reports it (server/exporter.CODECS): the
 * quality presets the dropdown shows and the custom range behind the
 * "Custom…" entry. kbps is null when the output size cannot be predicted. */
export interface ExportCodecSpec {
  label: string;
  ext: string | null;
  presets: { v: string; label: string; kbps: number | null }[];
  custom: { mode: "kbps" | "q"; min: number; max: number; default: number } | null;
  default: string;
}

/** The Export page's form — the request body, and (key for key, under
 * `export_<field>`) the saved defaults it loads on open. */
export interface ExportForm {
  dest: string;
  subfolder: string;
  codec: string;
  quality: string;
  structure: string;
  embed_covers: boolean;
  embed_cover_jpeg_quality: number;
  embed_cover_resolution: number;
  id3v2: string;
  id3v1: boolean;
  replaygain: boolean;
  clean_tags: boolean;
  playlists: boolean;
  sidecars: boolean;
  verify: boolean;
  prune: boolean;
  workers: number;
}

/** One item in <music folder>/.mlo/trash. `cover` is false when the cover
 *  endpoint would 404 for this directory — render a placeholder then. */
export interface TrashEntry {
  name: string;
  path: string;
  kind: "album" | "file";
  label: string;
  tracks: number;
  bytes: number;
  /** ISO-8601 */
  trashed_at: string;
  cover: boolean;
  /** Where the entry was removed from; null when the move did not record it
   *  (older removals) — then it can only be restored to a chosen folder. */
  origin: string | null;
}

export interface TrashPayload {
  folder: string;
  /** The library root `<music folder>/.mlo/trash` lives in — the default
   *  destination when a trashed entry has no recorded origin. */
  music_folder: string;
  exists: boolean;
  count: number;
  bytes: number;
  entries: TrashEntry[];
}

export interface TrashRestoreResult {
  restored: { name: string; to: string }[];
  failed: { name: string; error: string }[];
}

export interface TrashDeleteResult {
  deleted: string[];
  failed: { name: string; error: string }[];
  /** Bytes reclaimed, only for what actually got deleted. */
  freed: number;
}

/** Live per-query search progress (server/soulseek_auto.py job_state()) while
 *  the search stage runs — null once the query has been scored. There is no
 *  clock here on purpose: slskd's window is a ceiling that a good candidate
 *  ends early, so the payload carries only what the network answered. */
export interface SlskSearchProgress {
  query: string;
  state: string;
  responses: number;
  files: number;
}

/** `/api/soulseek/status` — slskd's availability and login, plus the saved
 *  credentials and the ports from the config. */
export interface SlskStatus {
  installed: boolean;
  running: boolean;
  /** slskd's own network login; null while the daemon is not running. */
  logged_in: boolean | null;
  /** slskd's words for a failed login (INVALIDPASS, no credentials) — the
   *  only explanation some failures have. */
  error: string | null;
  /** Set when slskd's web port is held by ANOTHER app's slskd. */
  conflict: string | null;
  conflict_username: string | null;
  /** slskd's GET /application payload (transfer speeds, counters). */
  server: { uploadSpeed?: number; downloadSpeed?: number; [k: string]: unknown } | null;
  download_dir: string;
  web_port: number;
  listen_port: number;
  /** Saved config value — prefills the login form. */
  username: string;
  /** The account slskd is ACTUALLY signed in as, "" when unknown. Drifts from
   *  `username` as soon as the login is corrected on slskd's own page. */
  account: string;
  password: string;
  has_credentials: boolean;
  autostart: boolean;
  share_dirs: string[];
}

/** One file of the running auto-import download, as slskd reports it. */
export interface SlskAutoFile {
  name?: string;
  bytes?: number;
  size?: number;
  /** fractional file completion (0-100), never only 0-or-100 */
  percent?: number;
  speed?: number;
  state?: string;
  /** slskd reports the transfer finished */
  complete?: boolean;
  /** the pipeline formally accepted the file — it landed on disk */
  done?: boolean;
}

/** Live download progress of the auto-import job — null while nothing is in
 *  flight, and absent entirely on servers predating the payload. Every field
 *  is optional: the panel renders whatever subset the backend publishes. */
export interface SlskAutoProgress {
  phase?: string;
  username?: string;
  dir?: string;
  /** files SLKSD reports complete — a different measure from files_arrived */
  files_done?: number;
  /** files the pipeline has accepted onto disk */
  files_arrived?: number;
  files_total?: number;
  bytes?: number;
  size?: number;
  /** byte-weighted share of `size`, not a file count */
  percent?: number;
  /** instantaneous aggregate rate in bytes/s (delta between polls) */
  speed?: number;
  /** remaining bytes / speed, null while nothing is moving */
  eta_s?: number | null;
  files?: SlskAutoFile[] | null;
}

/** Auto-import job state (server/soulseek_auto.py job_state()). `confirm` is
 *  set while the job waits for the user to approve a lossy-only download. */
export interface SlskAutoJob {
  state: "idle" | "running" | "confirm" | "done" | "error" | "cancelled";
  stage: string;
  search: SlskSearchProgress | null;
  /** Present only while a download is in flight. */
  progress?: SlskAutoProgress | null;
  release: {
    id?: string | null; title?: string; artist?: string; date?: string | null;
    country?: string | null; catalog_number?: string | null; media?: string;
  } | null;
  log: { t: string; msg: string }[];
  attempts: { username: string; dir: string; reason: string }[];
  result: {
    album_path?: string; staging_path?: string; imported?: boolean; organized?: boolean;
    organize_error?: string | null; error?: string;
    /** Set when the job gave up on the search and parked the release in the
     *  wish list instead — there is no album_path in that case. */
    wished?: boolean; wish_id?: number;
  } | null;
  /** The prompt while state == "confirm". `reason` picks the card: "lossy_only"
   *  asks whether a lossy copy may be downloaded, "no_logs" asks whether a
   *  lossless album without a rip log may be downloaded, "no_results" reports a
   *  search that came back empty and offers the wish handoff. All are answered
   *  through soulseekAutoConfirm(). Only the fields of the variant at hand are
   *  published, so every field past `reason` is optional and the panel renders
   *  whatever subset arrives. */
  confirm: {
    reason?: "lossy_only" | "no_logs" | "no_results";
    /** How long the search ran, in seconds (no_results). */
    waited?: number;
    /** The queries that came back empty (no_results). */
    queries?: string[];
    /** The media the release is (no_logs) — what the album grades as without
     *  a rip log to verify it. */
    media?: string;
    formats?: string[];
    candidates?: {
      username: string; dir: string; format: string;
      matched: number; expected: number; size: number; score: number;
    }[];
  } | null;
}

/** One slskd transfer (download or upload). `state` is slskd's own enum:
 *  Queued / InProgress / Completed / Errored / Cancelled / Rejected / … */
export interface SlskTransfer {
  id: string;
  filename: string;
  size: number;
  state: string;
  bytesTransferred?: number | null;
  percentComplete?: number | null;
  averageSpeed?: number | null;
}

export interface SlskTransferUser {
  username: string;
  directories: { directory: string; files: SlskTransfer[] }[];
}

export interface SlskDownloads {
  downloads: SlskTransferUser[];
}

/** Which staging root an action targets. They are two directories —
 *  `<music>/.mlo/downloads` and its `<incomplete>` sibling — and a name is
 *  only unique INSIDE one of them, so every call names its root. */
export type StagingRootId = "downloads" | "incomplete";

/** One staging entry — the server's `_downloads_entry()` minus its private
 *  `_mtime` sort key. `partial` is slskd's own in-flight leftover, `album` an
 *  entry holding audio (so it can be imported). */
export interface StagingEntry extends DownloadEntry {
  /** Epoch seconds of the entry's newest file; the list arrives newest first. */
  modified: number;
}

/** One staging root. A folder that was never created reports `exists: false`
 *  with zero totals — never an error. */
export interface StagingRoot {
  /** Absolute path, forward slashes. */
  folder: string;
  exists: boolean;
  count: number;
  bytes: number;
  entries: StagingEntry[];
}

export interface SoulseekStaging {
  downloads: StagingRoot;
  incomplete: StagingRoot;
}

export interface SlskBrowse {
  username: string;
  directories: { directory: string; files: { filename: string; size: number }[] }[];
}

/** One private-message conversation (slskd): the peer and its unread count.
 *  slskd's list carries no last message — the thread holds the content. */
export interface SlskConversation {
  username: string;
  is_active?: boolean;
  unread?: number;
}

/** One private message. `direction` is slskd's own enum (In = from the peer). */
export interface SlskMessage {
  id?: number;
  direction?: "In" | "Out";
  message?: string;
  timestamp?: string;
  acknowledged?: boolean;
  replayed?: boolean;
}

/** One candidate artist image from the metadata review flow. */
export interface MetadataImageCandidate {
  url: string;
  source: string;
  width?: number | null;
  height?: number | null;
}

/** A description candidate with its provenance (which provider, fetched when). */
export interface MetadataText {
  text: string;
  source: string;
  fetched?: string | null;
}

export interface MetadataCandidates {
  images: MetadataImageCandidate[];
  artist_description: MetadataText | null;
  album_description: MetadataText | null;
  /** What the import chain STAGED for this album instead of applying, when
   *  the review switches are on (`metadata_review`, `cover_review`). The cover
   *  branch is the candidate set the album page offers as "Choose a cover". */
  staged?: {
    candidates?: unknown;
    covers?: {
      artist: string;
      album: string;
      release_group: string;
      /** Who answered the staged fetch — the badge the picker shows. */
      provider: string | null;
      staged_at: string;
      results: CoverResult[];
    } | null;
  };
}

/* ---------------------------------------------------------------------- *
 * Per-track checks — advisory (/api/mb/advisory/fetch) and instrumental   *
 * (/api/instrumental/fetch). Both WRITE the tag they check and hand back  *
 * who said what, which is the only provenance the UI can show.            *
 * ---------------------------------------------------------------------- */

/** `{path: {source: value}}` — every provider that had something to say about
 *  that track. Absent entirely on servers predating the provenance fields. */
export type TrackAnswers = Record<string, Record<string, string | number>>;

/** Advisory fetch reply. `values` is what the rating is (0 clean, 1 explicit,
 *  2 clean edition) and `sources` the one provider whose answer was written;
 *  `answers` carries all of them. */
export interface AdvisoryFetchResult {
  updated: number;
  values?: Record<string, string | number>;
  sources?: Record<string, string>;
  answers?: TrackAnswers;
}

/** Instrumental fetch reply: `values` is INSTRUMENTAL (0/1), `evidence` the
 *  answers behind it. */
export interface InstrumentalFetchResult {
  updated: number;
  values?: Record<string, number | string>;
  evidence?: TrackAnswers;
}

/** One track's entry in a check reply's per-path map. Those maps are keyed by
 *  the path the server normpathed — backslashes on Windows, while the UI
 *  carries "/" paths — so both spellings are tried. Undefined when the server
 *  said nothing about this track. */
export function replyFor<T>(map: Record<string, T> | undefined, path: string): T | undefined {
  return map?.[path] ?? map?.[path.replace(/\//g, "\\")] ?? map?.[path.replace(/\\/g, "/")];
}

/** The providers behind a track's value, the deciding one first
 *  ("deezer-isrc, apple-album"). Reads the entry `replyFor` returned, so a
 *  server without the maps yields [] — provenance is never inferred locally. */
export function answerSources(
  answers: Record<string, unknown> | undefined,
  winner?: string | null
): string[] {
  const out: string[] = [];
  for (const s of [winner, ...Object.keys(answers ?? {})]) {
    if (s && !out.includes(s)) out.push(s);
  }
  return out;
}

/** Run both per-track checks on one selection. The legs are independent: an
 *  endpoint a server does not have must not hide the other leg's values, so a
 *  failed leg arrives as null with its message in `errors`. */
export async function checkTrackValues(paths: string[]): Promise<{
  adv: AdvisoryFetchResult | null;
  inst: InstrumentalFetchResult | null;
  errors: string[];
}> {
  const [adv, inst] = await Promise.all([
    api.mbAdvisoryFetch({ paths }).catch((e) => e as Error),
    api.instrumentalFetch(paths).catch((e) => e as Error),
  ]);
  const errors: string[] = [];
  if (adv instanceof Error) errors.push(`advisory: ${adv.message}`);
  if (inst instanceof Error) errors.push(`instrumental: ${inst.message}`);
  return {
    adv: adv instanceof Error ? null : adv,
    inst: inst instanceof Error ? null : inst,
    errors,
  };
}

/* ---------------------------------------------------------------------- *
 * Artist image / descriptions — the album metadata step. One call accounts *
 * for all three items an import is expected to carry.                     *
 * ---------------------------------------------------------------------- */

/** The three artwork/text items `/api/album/metadata/fetch` accounts for. */
export type MetadataItemKind = "artist_image" | "artist_description" | "album_description";

/** One item's outcome: what happened, who answered, and why not.
 *  `present` = already stored, `fetched` = a provider answered now,
 *  `disabled` = switched off in Settings, `not-found` = no source has it. */
export interface MetadataFetchItem {
  state: "fetched" | "present" | "disabled" | "not-found" | "error" | string;
  source?: string | null;
  detail?: string | null;
}

/** `&staged=1` for the import wizard's own album folder.

 *  The wizard's per-track steps run on albums the library tree does not list
 *  yet, so the server accepts them only when the call asks for it. Every other
 *  caller leaves it off and stays as strict as before. */
const stagedQ = (staged?: boolean) => (staged ? "&staged=1" : "");

/** One line saying what an install just did.
 *
 *  Shared by the Dependencies page and the setup wizard so the two never tell
 *  the same request two different stories. It names the no-op case on purpose:
 *  a press that changes nothing used to end in "installed / updated", which is
 *  how a button that had quietly fetched a version already on disk came to be
 *  reported as "does nothing at all". */
export function installSummary(
  results: { ok: boolean; name: string; changed?: boolean }[]
): string {
  const failed = results.filter((r) => !r.ok);
  if (failed.length) {
    return `Install finished with ${failed.length} failure(s): ${failed.map((f) => f.name).join(", ")}`;
  }
  const changed = results.filter((r) => r.changed).length;
  if (!changed) {
    return results.length === 0
      ? "Nothing to install"
      : "Nothing to do — already at the newest release";
  }
  const already = results.length - changed;
  return `Updated ${changed} tool${changed === 1 ? "" : "s"}${already ? ` · ${already} already current` : ""}`;
}

/** `/api/auth/status`. `required` answers THIS request — a client on the
 *  network must sign in, while the machine the server runs on (its own browser,
 *  a desktop shell, the host of a container) never has to; `gate` is the
 *  server-wide answer behind it (is a password demanded of clients at all), and
 *  `local` says which of the two this client is. `has_password` false means
 *  nobody has claimed this server yet, so the screen that makes sense is
 *  "create a password", not "sign in". */
export interface AuthStatus {
  required: boolean;
  /** Does this server ask a password of non-local clients at all? */
  gate?: boolean;
  /** Is this client the machine the server runs on? */
  local?: boolean;
  has_password: boolean;
  authenticated: boolean;
  username: string;
  public_url: string;
  session_days: number;
  /** A sentence when the server's configuration is unsafe (gate off on a
   *  network address, etc), else "". */
  setup_hint: string;
}

/** What login/setup/password-change answer with: the token this client keeps
 *  (the cookie is already set for the browser). */
export interface AuthSession {
  token: string;
  expires_at: number;
  session_days: number;
  username: string;
}

/** `/api/auth/users` — every user on this server, and which one is asking.
 *  The unnamed default scope is not a user row and is never listed: it is
 *  where an unclaimed install's data lives. */
export interface AuthUsers {
  users: string[];
  you: string;
}

/** `/api/version` — the running server's version and whether upstream has a
 *  newer one. `latest` is the newest release tag ("" while the check has not
 *  succeeded), `update_available` is the server's own verdict, `release_url`
 *  a link to that release ("" when there is nothing to link), and `source` is
 *  `"github"` or `"unavailable"` — the GitHub check is cached and never fatal,
 *  so a client shows the version either way and only mentions `latest` when
 *  the server says it is behind. */
export interface ServerVersion {
  version: string;
  latest: string;
  update_available: boolean;
  release_url: string;
  checked_at: number;
  source: "github" | "unavailable";
}

/** One completed download waiting to be imported. */
export interface ReadyAlbum {
  path: string;
  name: string;
  /** Path relative to the download dir, for display. */
  rel: string;
  files: number;
  bytes: number;
}

export interface ReadyAlbums {
  ok: boolean;
  albums: ReadyAlbum[];
  download_dir: string;
}

/** A started import run (one album, or all of them). */
export interface ImportRun {
  ok: boolean;
  error?: string;
  status: ImportRunStatus;
}

/** Progress of the sequential import run: one album at a time, in order. */
export interface ImportRunStatus {
  state: "idle" | "running" | "done" | "error" | "cancelled";
  total: number;
  done: number;
  current: string | null;
  results: { path: string; ok: boolean; album_root: string; error: string }[];
  errors: string[];
  started_at: number;
  finished_at: number;
}

/** One in-flight job in the library lock registry (server/job_locks): the work
 *  running now and the paths it holds, so nothing else deletes, moves or
 *  retags them. `progress` is null for a job that has not reported any (a tag
 *  write, an organize); `elapsed` is measured on the server in seconds. */
export interface JobLock {
  job: string;
  kind: string;
  label: string;
  started_at: number;
  elapsed: number;
  paths: string[];
  progress: { done: number; total: number | null; text: string } | null;
}

/** GET /api/jobs/locks — everything holding library paths right now. */
export interface JobLocksPayload {
  jobs: JobLock[];
}

export const api = {
  health: () => json<{ status: string; version: string }>(`${API}/health`),
  /** The running server's version, checked against the latest GitHub release
   *  (cached on the server 6 h). `source: "unavailable"` still carries the
   *  version — the update check is never allowed to fail the request. */
  version: () => json<ServerVersion>(`${API}/version`, undefined, 8000),

  // ── session ────────────────────────────────────────────────────────────
  /** Does this server want a login, has anyone claimed it, and are we in? */
  authStatus: () => json<AuthStatus>(`${API}/auth/status`),
  /** Sign in. `username` names which user to sign in as and is only sent when
   *  the caller has one to offer (the screen prefills it from
   *  `/api/auth/status`); omitted, the server answers with the only user the
   *  server has — every install that was never asked for a second one. */
  authLogin: (password: string, username?: string) =>
    json<AuthSession>(`${API}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(username ? { password, username } : { password }),
    }, 30000),
  authSetup: (password: string, confirm: string, username?: string) =>
    json<AuthSession>(`${API}/auth/setup`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password, confirm, username }),
    }, 30000),
  authLogout: () => json<{ ok: boolean }>(`${API}/auth/logout`, { method: "POST" }),
  authChangePassword: (current: string, password: string, confirm: string) =>
    json<AuthSession>(`${API}/auth/password`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current, password, confirm }),
    }, 60000),
  authRevokeAll: () => json<{ ok: boolean; revoked: number }>(`${API}/auth/revoke-all`, { method: "POST" }),
  /** The server's users, and which of them is asking. */
  authUsers: () => json<AuthUsers>(`${API}/auth/users`),
  /** Add a user, or set an existing one's password. Unlike a password change
   *  this signs nobody out — adding a second person must not disconnect the
   *  first. The server's own words come back as an Error (`username is
   *  required`, `password must be at least 8 characters`, `the passwords do
   *  not match`), and are shown verbatim. */
  authAddUser: (username: string, password: string, confirm: string) =>
    json<{ ok: boolean; username: string; users: string[] }>(`${API}/auth/users`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password, confirm }),
    }, 30000),
  /** Remove a user and every session they hold. The last user is refused by
   *  the server (a 400 with a readable detail) — removing it would change
   *  which password opens the library instead of closing it. */
  authRemoveUser: (username: string) =>
    json<{ ok: boolean; users: string[] }>(`${API}/auth/users/${encodeURIComponent(username)}`, {
      method: "DELETE",
    }, 30000),
  config: () => json<Record<string, unknown>>(`${API}/config`),
  configDefaults: () => json<Record<string, unknown>>(`${API}/config/defaults`),
  /** Subdirectories of a path on the SERVER (GET /api/fs/dirs) — the music
   *  folder picker. No path means "where the library already is". Directory
   *  names only: nothing here opens a file. `pinned` is set when
   *  MLO_MUSIC_FOLDER pins the folder (Docker/compose): browsing still works,
   *  but a choice made here cannot stick. */
  fsDirs: (path?: string) =>
    json<{
      path: string;
      parent: string | null;
      roots: string[];
      dirs: { name: string; path: string; library: boolean; writable: boolean }[];
      pinned: string | null;
    }>(`${API}/fs/dirs${path ? `?path=${encodeURIComponent(path)}` : ""}`, {}, 30000),
  saveConfig: (cfg: Record<string, unknown>) =>
    json<Record<string, unknown>>(`${API}/config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cfg),
    }),
  /** One tiny round trip to the configured AI endpoint (POST /api/ai/test).
   *  The overrides let the wizard test keys before they are saved; a provider
   *  that refuses comes back as `{ok:false, error}` — that message is the
   *  point of the button, so it is never thrown as a request error. */
  aiTest: (body: { base_url?: string; api_key?: string; model?: string; effort?: string }) =>
    json<{ ok: boolean; reply: string; error: string }>(`${API}/ai/test`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }, 60000),
  library: () => json<import("./types").Library>(`${API}/library`),
  album: (path: string, staged = false) =>
    json<import("./types").Album>(`${API}/album?path=${encodeURIComponent(path)}${stagedQ(staged)}`),
  artist: (path: string) => json<import("./types").Artist>(`${API}/artist?path=${encodeURIComponent(path)}`),
  removeAlbum: (path: string) =>
    json<{ ok: boolean; trash: string }>(`${API}/album/remove`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    }),
  /** <music folder>/.mlo/trash — what "Remove from library" moved aside. */
  trash: () => json<TrashPayload>(`${API}/trash`),
  trashDelete: (names: string[]) =>
    json<TrashDeleteResult>(`${API}/trash/delete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ names }),
    }),
  /** Move trashed items back into the library. `dest` only applies to entries
   *  whose original location was never recorded — the server restores every
   *  other name to its own origin, and validates `dest` is inside the music
   *  folder. */
  trashRestore: (names: string[], dest?: string | null) =>
    json<TrashRestoreResult>(`${API}/trash/restore`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ names, dest: dest ?? null }),
    }),
  mbDetect: (path: string, staged = false) =>
    json<{ mbid: string | null; key?: string; track?: string }>(
      `${API}/album/mbdetect?path=${encodeURIComponent(path)}${stagedQ(staged)}`,
      undefined,
      8000
    ),
  scanTracks: (path: string, staged = false) =>
    json<{ path: string; tracks: any[] }>(
      `${API}/album/scan-tracks?path=${encodeURIComponent(path)}${stagedQ(staged)}`,
      undefined,
      30000
    ),
  organize: (paths: string[], dryRun = false) =>
    json<{ results: any[] }>(`${API}/organize`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths, dry_run: dryRun }),
    }),
  /** MAINTAIN → In progress: the jobs holding library paths right now
   *  (server.job_locks). Polled while the page is open; a job that has ended
   *  is gone from the next answer, because the registry releases with the
   *  work. */
  jobLocks: () => json<JobLocksPayload>(`${API}/jobs/locks`),

  streamUrl: (path: string) => media(`${API}/stream?path=${encodeURIComponent(path)}`),
  /** Library music-video stream: direct bytes by default (?transcode=1 pipes
   * MPEG-2/VC-1/etc. through ffmpeg into playable H.264/AAC MP4). */
  videoStreamUrl: (path: string, transcode = false) =>
    media(`${API}/videos/stream?path=${encodeURIComponent(path)}${transcode ? "&transcode=1" : ""}`),
  /** Playback decision for a video: native (browser-decodable container +
   * codecs) vs live transcode, plus ffprobe's real duration — fragmented
   * live transcodes report Infinity on the media element, so this is the
   * only reliable length source for those. */
  videoMeta: (path: string) =>
    json<{ native: boolean; reason: string | null; duration: number | null; video_codec: string | null; audio_codecs: string[] }>(
      `${API}/videos/meta?path=${encodeURIComponent(path)}`
    ),
  subtitles: (path: string) =>
    json<{ muxed: { n: number; codec: string; title: string }[]; sidecars: { file: string; name: string; language: string | null }[] }>(
      `${API}/videos/subtitles?path=${encodeURIComponent(path)}`
    ),
  subtitleUrl: (path: string, sidecar?: string, n?: number) =>
    media(`${API}/videos/subtitle?path=${encodeURIComponent(path)}${sidecar ? `&sidecar=${encodeURIComponent(sidecar)}` : ""}${typeof n === "number" && n >= 0 ? `&n=${n}` : ""}`),
  // Read-only tag view (tag writing was removed; grading scripts own writes).
  // `staged` reads a track of an album the import wizard is editing before it
  // is in the library.
  tags: (path: string, staged = false) =>
    json<any>(`${API}/tags?path=${encodeURIComponent(path)}${stagedQ(staged)}`),
  // ReplayGain for playback loudness matching. `mode` overrides the saved
  // replaygain_mode for one call (track/album/off); `analyzed` is true when
  // the gain had to be measured on the fly because the tags were missing.
  replaygain: (path: string, mode?: "track" | "album" | "off") =>
    json<{ path: string; gain: number | null; peak: number | null; mode: string; source: string | null; analyzed: boolean }>(
      `${API}/replaygain?path=${encodeURIComponent(path)}${mode ? `&mode=${mode}` : ""}`
    ),
  lyricsEmbed: (path: string, lyrics: string, staged = false) =>
    json<{ ok: boolean }>(`${API}/lyrics/embed`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, lyrics, staged }),
    }),
  videosScan: (path?: string) =>
    json<{ videos: { path: string; album: string; file: string; size: number; video_codec: string | null; audio_codecs: string[]; duration: number | null; mp4_safe: boolean | null }[] }>(
      `${API}/videos/scan${path ? `?path=${encodeURIComponent(path)}` : ""}`,
      undefined,
      60000
    ),
  // Tag a music video (TITLE/ARTIST/DISCNUMBER/...). Non-MKV containers are
  // remuxed losslessly to MKV — the response path is the final file.
  videoTag: (path: string, tags: Record<string, string>) =>
    json<{ ok: boolean; path: string; renamed: boolean; tech: Record<string, number | string> } & ContainerSwap>(`${API}/videos/tag`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, tags }),
    }, 600000).then(noteContainerSwap),
  /** Dimensions etc. of an album's cover — or, with `coverFile`, of any image
   *  in the album folder, which is how a track's own art is measured. */
  coverInfo: (albumPath: string, coverFile?: string | null, staged = false) =>
    json<CoverInfo>(
      `${API}/cover/info?album=${encodeURIComponent(albumPath)}${coverFile ? `&file=${encodeURIComponent(coverFile)}` : ""}${stagedQ(staged)}`
    ),

  // Scripts run synchronously on the server, so the client must wait far
  // longer than the shared 20 s default: a single album's chain (mood
  // analysis, lyrics, beets, rsgain, audit, grade) legitimately takes
  // minutes, and aborting would hide the per-script report behind a
  // client-side timeout while the server kept working.
  run: (ids: number[], targets?: string[], force?: Record<string, boolean>) =>
    json<{ results: ScriptRunResult[] }>(`${API}/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids, targets, force }),
    }, 3600000),

  // playlists
  playlists: () => json<import("./types").Playlist[]>(`${API}/playlists`),
  playlist: (id: number) => json<import("./types").Playlist>(`${API}/playlists/${id}`),
  createPlaylist: (name: string, kind: "manual" | "smart", filter?: unknown) =>
    json<import("./types").Playlist>(`${API}/playlists`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, kind, filter }),
    }),
  /** Partial update (currently: rename). */
  playlistUpdate: (id: number, patch: { name?: string }) =>
    json<import("./types").Playlist>(`${API}/playlists/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
  deletePlaylist: (id: number) => json<{ ok: boolean }>(`${API}/playlists/${id}`, { method: "DELETE" }),
  playlistAdd: (id: number, paths: string[], position?: number) =>
    json<{ added: number }>(`${API}/playlists/${id}/tracks`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths, position }),
    }),
  playlistOrder: (id: number, paths: string[]) =>
    json<{ ok: boolean }>(`${API}/playlists/${id}/tracks`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths }),
    }),
  playlistRemove: (id: number, paths: string[]) =>
    json<{ ok: boolean }>(`${API}/playlists/${id}/tracks`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths }),
    }),
  playlistFilter: (id: number, filter: unknown) =>
    json<import("./types").Playlist>(`${API}/playlists/${id}/filter`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filter }),
    }),
  playlistEvaluate: (id: number) => json<{ paths: string[] }>(`${API}/playlists/${id}/evaluate`, { method: "POST" }),
  playlistExportUrl: (id: number) => media(`${API}/playlists/${id}/export`),
  playlistImport: (name: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return json<import("./types").Playlist>(`${API}/playlists/import?name=${encodeURIComponent(name)}`, {
      method: "POST",
      body: fd,
    });
  },

  // integrations
  mbRelease: (id: string) => json<import("./types").MBRelease>(`${API}/mb/release?mbid=${encodeURIComponent(id)}`),
  /** Credits / performers for one track (`path`) or a whole album (`album`):
   *  a role-grouped MusicBrainz answer, or the files' own credit tags
   *  (`source: "tags"`) when MB has nothing. At most one MB request (cached
   *  server-side), but a cold cache is slow — hence the generous timeout. */
  credits: (opts: { path?: string; album?: string }) => {
    const q = new URLSearchParams();
    if (opts.path) q.set("path", opts.path);
    else if (opts.album) q.set("album", opts.album);
    return json<Credits>(`${API}/credits?${q}`, undefined, 60000);
  },
  mbGenres: (id: string, limit?: number) =>
    json<import("./types").GenreCascade>(
      `${API}/mb/release-genres?mbid=${encodeURIComponent(id)}${limit ? `&limit=${limit}` : ""}`
    ),
  mbSearchReleases: (q: string, mode: "release" | "track" | "catno" | "barcode" = "release") =>
    json<any[]>(`${API}/mb/search/releases?q=${encodeURIComponent(q)}&mode=${mode}`),
  mbSearchArtists: (q: string) => json<any[]>(`${API}/mb/search/artists?q=${encodeURIComponent(q)}`),
  mbReleaseGroup: (id: string, offset = 0, limit = 300) =>
    json<any>(`${API}/mb/release-group/${id}?offset=${offset}&limit=${limit}`),
  // Generic MusicBrainz browser (in-app entity pages). Searches and
  // discographies page 100 rows at a time — pass offset for "load more".
  // primaryType/secondaryType map onto MusicBrainz's own release-type
  // qualifiers (Album/EP/Single + Soundtrack/Live/Compilation/...).
  mbSearch: (
    type: string, q: string, limit = 100,
    mode: "free" | "catno" | "barcode" = "free", offset = 0,
    primaryType = "", secondaryType = ""
  ) =>
    json<MBSearchRows>(
      `${API}/mb/search?type=${encodeURIComponent(type)}&q=${encodeURIComponent(q)}` +
      `&limit=${limit}&offset=${offset}&mode=${mode}` +
      `&primary_type=${encodeURIComponent(primaryType)}&secondary_type=${encodeURIComponent(secondaryType)}`
    ),
  mbArtist: (id: string, offset = 0, limit = 300) =>
    json<MBArtistBrowse>(`${API}/mb/artist/${id}?offset=${offset}&limit=${limit}`),
  mbRecording: (id: string, offset = 0, limit = 300) =>
    json<MBRecordingBrowse>(`${API}/mb/recording/${id}?offset=${offset}&limit=${limit}`),
  /** Which MusicBrainz kind a bare pasted MBID is — the browser routes a
   *  pasted ID to its entity page without making the user pick a type. */
  mbIdentify: (id: string) =>
    json<{ type: string; id: string; title: string }>(`${API}/mb/detect/${id}`),
  mbMatch: (albumPath: string, releaseId: string, staged = false) =>
    json<{ release: import("./types").MBRelease; suggestions: import("./types").MatchSuggestion[] }>(
      `${API}/mb/match`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ album_path: albumPath, release_id: releaseId, staged }),
      }
    ),
  /** Write tags onto tracks. A value is a string, or a LIST for a
   *  multi-valued field (GENRE): the server splits a string on the separator
   *  but writes an array one field per name, which is the only way a track
   *  keeps "Rock" and "Shoegaze" apart. */
  mbAssign: (tracks: Record<string, Record<string, string | string[] | null>>, staged = false) =>
    json<{ ok: boolean; changed: number } & ContainerSwap>(`${API}/mb/assign`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tracks, staged }),
    }).then(noteContainerSwap),

  lyricsSearch: (artist: string, track: string, album?: string, duration?: number) =>
    json<any[]>(`${API}/lyrics/search?artist=${encodeURIComponent(artist)}&track=${encodeURIComponent(track)}${album ? `&album=${encodeURIComponent(album)}` : ""}${duration ? `&duration=${duration}` : ""}`),
  lyricsGet: (artist: string, track: string, album?: string, duration?: number) =>
    json<any>(
      `${API}/lyrics/get?artist=${encodeURIComponent(artist)}&track=${encodeURIComponent(track)}${album ? `&album=${encodeURIComponent(album)}` : ""}${duration ? `&duration=${duration}` : ""}`
    ),
  lyricsWrite: (path: string, lrc: string, staged = false) =>
    json<{ ok: boolean; lrc: string }>(`${API}/lyrics/write`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, lrc, staged }),
    }),
  // Submit lyrics to LRCLIB on behalf of a track (or with explicit fields).
  /** Submit lyrics to LRCLIB. `force` overrides the "the database already has
   *  this recording" rule — the editor sends it only on an explicitly-confirmed
   *  second press, and the reply carries `exists` so the UI can offer that. */
  lyricsPublish: (body: { path?: string; artist?: string; track?: string; album?: string; duration?: number; plain?: string; synced?: string; force?: boolean }) =>
    json<{ ok: boolean; message: string; exists?: boolean; forced?: boolean }>(`${API}/lyrics/publish`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }, 60000),
  /** Distribute word/syllable times inside each line's slot, weighted by
   *  length (deterministic, no network). `text` defaults to the stored
   *  lyrics; the reply is the full LRC. */
  lyricsWordsync: (path: string, text?: string) =>
    json<{ lrc: string }>(`${API}/lyrics/wordsync`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(text === undefined ? { path } : { path, text }),
    }, 180000),

  // Bulk tag surgery: delete `remove` tags and set `set` {tag: value}
  // (empty value = delete) across the given tracks.
  tagsBulk: (body: { paths: string[]; remove: string[]; set: Record<string, string> }) =>
    json<{ ok: boolean; removed: number; added: number; failed: number; errors?: string[] } & ContainerSwap>(`${API}/tags/bulk`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }, 300000).then(noteContainerSwap),

  rymValidate: (url: string) => json<{ valid: boolean }>(`${API}/rym/validate?url=${encodeURIComponent(url)}`),

  /** Verified RateYourMusic links for the link editor's auto-find. Each one is
   *  null when RYM itself did not confirm that page and `note` says why — a
   *  miss is a normal 200, never an error. */
  rymResolve: (artist: string, album = "") =>
    json<{ album: string | null; artist: string | null; note: string }>(
      `${API}/rym/resolve?artist=${encodeURIComponent(artist)}&album=${encodeURIComponent(album)}`,
    ),

  /** The cover URL. `token` (the `CoverWriteResult.token` of the write that
   *  just happened) rides along as `&v=`: a cover is replaced IN PLACE, so
   *  without it a replaced cover.jpg is the same URL — and neither the
   *  rendered `<img>` nor a browser cache would ever ask for the new bytes.
   *  Omitted, it falls back to the version this session wrote for that
   *  album+file (see lib/invalidate), which is what keeps the grids, the
   *  player bar and the ambient background on the freshly written image
   *  without any of them knowing about the write.
   *
   *  `staged` marks the import wizard's album — the folder the library does
   *  not list yet: the server serves its cover only to a request that carries
   *  the same opt-in every other wizard call passes, so a preview that leaves
   *  it out is refused (400) and stays empty however well the cover was
   *  written.
   *
   *  An explicit `token` — including `null` — wins over the remembered
   *  version, which is how a caller names the version-less key the offline
   *  cache stores (see mediaCache's forgetAlbumArtwork). */
  coverUrl: (
    albumPath: string,
    coverFile?: string | null,
    opts?: { token?: string | null; staged?: boolean }
  ) => {
    const v = opts && "token" in opts
      ? opts.token
      : coverFile
        ? coverVersion(albumPath, coverFile)
        : null;
    return media(
      `${API}/cover?album=${encodeURIComponent(albumPath)}` +
        (coverFile ? `&file=${encodeURIComponent(coverFile)}` : "") +
        (v ? `&v=${encodeURIComponent(v)}` : "") +
        (opts?.staged ? "&staged=true" : "")
    );
  },
  /** A remote provider image (`/api/art`), proxied and cached by the backend —
   *  NEVER the provider URL itself. Several cover CDNs (Deezer's among them)
   *  refuse the browser outright, and the app can both get past them and fall
   *  back to a provider that answers when it cannot. `artist`/`album`/`rg`
   *  (release-group MBID) are the identity that fallback is asked about.
   *
   *  Anything that is not an http(s) URL — a local `/api/cover` path, an
   *  already-proxied URL — is handed back untouched, so the helper is safe to
   *  slap on every image the app renders. */
  artUrl: (
    url: string | null | undefined,
    opts?: { artist?: string | null; album?: string | null; rg?: string | null }
  ) => {
    const u = (url ?? "").trim();
    if (!/^https?:\/\//i.test(u)) return u;
    const q = new URLSearchParams({ url: u });
    if (opts?.artist) q.set("artist", opts.artist);
    if (opts?.album) q.set("album", opts.album);
    if (opts?.rg) q.set("rg", opts.rg);
    return media(`${API}/art?${q}`);
  },
  coverColor: (albumPath: string) =>
    json<{ color: string; album: string }>(
      `${API}/cover?album=${encodeURIComponent(albumPath)}&color=1`,
      undefined,
      8000
    ),

  importUpload: (targetDir: string, files: { file: File; relPath: string }[]) => {
    const fd = new FormData();
    for (const { file, relPath } of files) fd.append("files", file, relPath);
    return json<{ ok: boolean; saved: string[]; album_path: string }>(
      `${API}/import/upload?target_dir=${encodeURIComponent(targetDir)}`,
      { method: "POST", body: fd },
      1800000
    );
  },
  importScan: (path: string) =>
    json<{ root: string; files: { relPath: string; size: number }[] }>(
      `${API}/import/scan?path=${encodeURIComponent(path)}`,
      { method: "POST" }
    ),
  importIngest: (source: string, target: string) =>
    json<{ ok: boolean; path: string }>(
      `${API}/import/ingest?source=${encodeURIComponent(source)}&target=${encodeURIComponent(target)}`,
      { method: "POST" }
    ),
  importCommit: (targetDir: string, mbLink?: string, rymLink?: string, staged = false) =>
    json<{ ok: boolean; changed: number }>(`${API}/import/commit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target_dir: targetDir, mb_link: mbLink || null, rym_link: rymLink || null, staged }),
    }),
  /** Record the release's full tracklist on the album, so a PARTIAL import
   *  can grey out the tracks that never came in. Empty tracks clears it. */
  importExpected: (
    targetDir: string,
    releaseId: string | null,
    tracks: { disc: number; position: number; title?: string; recording_mbid?: string | null }[],
    staged = false
  ) =>
    json<{ ok: boolean; tracks: number }>(`${API}/import/expected`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target_dir: targetDir, release_id: releaseId || null, tracks, staged }),
    }),

  /** slskd's staging area: <music>/.mlo/downloads. */
  downloads: () => json<DownloadsPayload>(`${API}/downloads`),
  downloadsDelete: (names: string[]) =>
    json<{ deleted: string[]; failed: { name: string; error: string }[]; freed: number }>(
      `${API}/downloads/delete`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ names }) }
    ),
  downloadsImport: (names: string[]) =>
    json<{ moved: { name: string; path: string }[]; failed: { name: string; error: string }[] }>(
      `${API}/downloads/import`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ names }) }
    ),

  /** Read-only scan of the whole music folder: misplaced files, unexpected
   *  folders, and anything that breaks the Artists/<Artist>/<Album> shape. */
  libraryLayout: () => json<LayoutReport>(`${API}/library/layout`),

  namingPreview: (script: string, shortFolderNames: boolean, sample?: Record<string, string>) =>
    json<{ path: string | null; ok: boolean; error?: string }>(`${API}/naming/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ script, short_folder_names: shortFolderNames, sample }),
    }),

  openFolder: (path: string) =>
    json<{ ok: boolean }>(`${API}/open-folder`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    }),

  /** What THIS build can do (`GET /api/capabilities`).
   *
   *  Answered from the server's cached tool detection plus one spawn probe —
   *  no network, no GitHub check — so a page can ask it on load. A phone
   *  running the backend inside the app answers the same shape as a desktop
   *  server and differs only in the per-feature rows: that difference is what
   *  the Dependencies page and both wizards read instead of guessing from the
   *  platform. */
  capabilities: () => json<Capabilities>(`${API}/capabilities`),

  dependencies: (refresh = false) =>
    json<{
      deps_dir: string;
      /** True while a background check of the upstream (GitHub) versions runs. */
      checking?: boolean;
      /** When the last completed upstream check finished (ISO, null = never). */
      upstream_checked_at?: string | null;
      /** The last upstream-check error, if any — the table still renders. */
      note?: string | null;
      tools: {
        key: string;
        name: string;
        installed_version?: string;
        /** The pinned release the installer fetches (the reviewed version). */
        latest_version?: string;
        detected_version?: string;
        path?: string | null;
        /** ok | update | missing | error — derived from the upstream value. */
        state: string;
        /** Newest release upstream has; null while unknown / not on GitHub. */
        upstream_version?: string | null;
        upstream_checked_at?: string | null;
        update_available?: boolean;
        note?: string | null;
        /** Whether an Install press can fetch this tool HERE: false for a
         *  Windows-only tool on Linux, or one the host ships as a distro
         *  package. The backend also skips those when there is no key list. */
        installable?: boolean;
        /** Why it cannot be installed here — the sentence the row shows. */
        install_note?: string | null;
        /** deps (the installer fetches it), system (a distro package this host
         *  provides) or unsupported (no build for this platform). */
        install_kind?: "deps" | "system" | "unsupported";
      }[];
    }>(`${API}/dependencies${refresh ? "?refresh=1" : ""}`),
  installDependencies: (keys?: string[]) =>
    json<{ results: { key: string; name: string; ok: boolean; error?: string;
                      /** The version this install landed on. */
                      version?: string;
                      /** False when that version is the one already on disk. */
                      changed?: boolean }[] }>(
      `${API}/dependencies/install`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keys: keys ?? null }),
      },
      900000
    ),

  /** Upload a cover. No track/tracks → the album cover; `track` → that file's
   *  sidecar; `tracks` → ONE image for the whole selection (sidecar of the
   *  first, recorded per track in the manifest). Never gated on size: the
   *  response still carries `warning` when the image is under the target. */
  cover: (albumPath: string, file: File, track?: string, tracks?: string[], staged = false) => {
    const fd = new FormData();
    fd.append("file", file);
    return json<CoverWriteResult>(
      `${API}/cover?album=${encodeURIComponent(albumPath)}${coverQuery(track, tracks)}${stagedQ(staged)}`,
      { method: "POST", body: fd }
    ).then(noteCoverWrite(albumPath));
  },

  /** Album covers for artist/album. `releaseGroupMbid`, when the caller knows
   *  it, is the identity the Cover Art Archive fallback is asked about; the
   *  reply's `provider` says who actually answered. */
  coverSearch: (
    artist: string,
    album: string,
    opts?: { sources?: string[]; country?: string; releaseGroupMbid?: string }
  ) => {
    const q = new URLSearchParams({ artist, album });
    if (opts?.sources?.length) q.set("sources", opts.sources.join(","));
    if (opts?.country) q.set("country", opts.country);
    if (opts?.releaseGroupMbid) q.set("release_group_mbid", opts.releaseGroupMbid);
    return json<CoverSearch>(`${API}/cover/search?${q}`, undefined, 90000);
  },
  /** Selectable cover sources + regions, plus the saved defaults. */
  coverSources: () => json<CoverSourceCatalog>(`${API}/cover/sources`),
  /** Apply a cover image from a URL; same track/tracks targeting as `cover`.
   *  For a provider URL, pass the identity the backend's fallback needs when
   *  the CDN itself refuses us (see `artUrl`). */
  coverFromUrl: (
    albumPath: string,
    url: string,
    track?: string,
    tracks?: string[],
    identity?: { artist?: string | null; album?: string | null; rg?: string | null },
    staged = false
  ) =>
    json<CoverWriteResult>(
      `${API}/cover/fromurl?album=${encodeURIComponent(albumPath)}&url=${encodeURIComponent(url)}${coverQuery(track, tracks)}` +
        (identity?.artist ? `&artist=${encodeURIComponent(identity.artist)}` : "") +
        (identity?.album ? `&title=${encodeURIComponent(identity.album)}` : "") +
        (identity?.rg ? `&rg=${encodeURIComponent(identity.rg)}` : "") +
        stagedQ(staged),
      { method: "POST" },
      120000
    ).then(noteCoverWrite(albumPath)),
  /** Drop `tracks` from the album's per-track cover manifest (all of them when
   *  omitted). The image file itself is never deleted. */
  coverClear: (albumPath: string, tracks?: string[], staged = false) =>
    json<{ ok: boolean }>(`${API}/cover/clear`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ album: albumPath, tracks: tracks?.length ? tracks : undefined, staged }),
    }),

  beetsStatus: () =>
    json<{ installed: boolean; version: string | null; db: string; config: string }>(`${API}/beets/status`),
  beetsInstall: () =>
    json<{ ok: boolean; version: string }>(`${API}/beets/install`, { method: "POST" }, 900000),
  beetsImport: (paths: string[]) =>
    json<{ ok: boolean; output: string; organized: any[] | null }>(
      `${API}/beets/import`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths }),
      },
      3600000
    ),

  soulseekStatus: () =>
    json<SlskStatus>(`${API}/soulseek/status`),
  soulseekStart: () =>
    json<{ ok: boolean; ready: boolean; message: string; has_credentials: boolean }>(`${API}/soulseek/start`, { method: "POST" }, 30000),
  soulseekRestart: () =>
    json<{ ok: boolean }>(`${API}/soulseek/restart`, { method: "POST" }, 60000),
  /** Share config + the live share audit. `probe` also pulls slskd's own share
   *  index (tens of megabytes on a large library) to look for a file that is on
   *  disk, so it is only asked for on demand. */
  soulseekShares: (probe = false) =>
    json<any>(`${API}/soulseek/shares${probe ? "?probe=1" : ""}`, undefined, probe ? 180000 : undefined),
  soulseekSharesSave: (dirs: string[], autostart: boolean | null, apply = true) =>
    json<{ ok: boolean; dirs: string[]; restarted: boolean }>(`${API}/soulseek/shares`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dirs, autostart, apply }),
    }, 60000),
  soulseekSharesRescan: () =>
    json<{ ok: boolean }>(`${API}/soulseek/shares/rescan`, { method: "POST" }, 60000),
  soulseekUploads: () => json<any>(`${API}/soulseek/uploads`),
  soulseekStop: () =>
    json<{ ok: boolean; message: string }>(`${API}/soulseek/stop`, { method: "POST" }, 15000),
  soulseekSearch: (query: string) =>
    json<{ id: string }>(`${API}/soulseek/search`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    }, 60000),
  soulseekSearchResults: (id: string) =>
    json<{
      state: string | null;
      isComplete?: boolean;
      responseCount?: number;
      fileCount?: number;
      responses: {
        username: string;
        file: string;
        size: number;
        bitrate: number | null;
        duration: number | null;
        vbr: boolean | null;
        slot: boolean;
        speed: number;
        queue: number;
      }[];
    }>(
      `${API}/soulseek/search/${encodeURIComponent(id)}`,
      undefined,
      30000
    ),
  soulseekDownload: (username: string, files: { filename: string; size: number }[]) =>
    json<{ ok: boolean; queued: number }>(`${API}/soulseek/download`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, files }),
    }, 60000),
  soulseekDownloads: () => json<SlskDownloads>(`${API}/soulseek/downloads`, undefined, 30000),
  /** What is actually sitting in the two staging folders, each reported on its
   *  own. Separate from soulseekDownloads(): that one is slskd's transfer
   *  history (and empty while the daemon is stopped), this one is the disk. */
  soulseekStaging: () => json<SoulseekStaging>(`${API}/soulseek/staging`, undefined, 60000),
  /** Delete ONE entry (file or folder tree) from a staging root. */
  soulseekStagingDelete: (root: StagingRootId, name: string) =>
    json<{ ok: boolean; freed: number }>(`${API}/soulseek/staging/delete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ root, name }),
    }, 60000),
  /** Empty one staging root. Per-entry failures come back in `failed` — the
   *  rest still goes, so the reply is a report, not an abort. */
  soulseekStagingClear: (root: StagingRootId) =>
    json<{ ok: boolean; cleared: number; freed: number; failed: { name: string; reason: string }[] }>(
      `${API}/soulseek/staging/clear`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ root }) },
      120000
    ),
  // Private messages — the conversation list carries the unread total (for the
  // tab badge), the thread is fetched per peer, usernames percent-encoded.
  soulseekMessages: () =>
    json<{ ok: boolean; unread: number; conversations: SlskConversation[] }>(
      `${API}/soulseek/messages`, undefined, 30000
    ),
  soulseekConversation: (username: string) =>
    json<{ ok: boolean; username: string; messages: SlskMessage[] }>(
      `${API}/soulseek/messages/${encodeURIComponent(username)}`, undefined, 30000
    ),
  soulseekSendMessage: (username: string, message: string) =>
    json<{ ok: boolean; sent: boolean }>(`${API}/soulseek/messages/${encodeURIComponent(username)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    }, 15000),
  soulseekMarkRead: (username: string) =>
    json<{ ok: boolean; acknowledged: boolean }>(
      `${API}/soulseek/messages/${encodeURIComponent(username)}/read`, { method: "POST" }, 15000
    ),
  soulseekCloseConversation: (username: string) =>
    json<{ ok: boolean; closed: boolean }>(
      `${API}/soulseek/messages/${encodeURIComponent(username)}`, { method: "DELETE" }, 15000
    ),
  /** A peer's whole shared tree (slskd browse) — what they're offering, so a
   *  folder can be queued or handed to auto-import without a search hit. */
  soulseekBrowse: (username: string) =>
    json<SlskBrowse>(`${API}/soulseek/browse/${encodeURIComponent(username)}`, undefined, 120000),
  /** Drop transfers from slskd's list (per-file or whole-queue cancel). */
  soulseekDownloadsCancel: (username: string, transfer_ids: string[]) =>
    json<{ ok: boolean; cancelled: number }>(`${API}/soulseek/downloads/cancel`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, transfer_ids }),
    }, 30000),
  /** Bulk-clear transfers from slskd's history. `finished` drops completed and
   *  failed rows and keeps the queue, `failed` only the ones that did not
   *  succeed, `incomplete` also stops what is in flight and DELETES the partial
   *  bytes already on disk, `all` does both. The server answers with the counts
   *  plus a `failed` list of transfers whose cleanup was refused. */
  soulseekDownloadsClear: (scope: "finished" | "failed" | "incomplete" | "all" = "finished") =>
    json<{
      cleared: number;
      files_deleted?: number;
      bytes_freed?: number;
      failed?: { username: string; filename: string; reason: string }[];
    }>(`${API}/soulseek/downloads/clear`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scope }),
    }, 60000),
  // Completed downloads on disk — the review workflow (preview → tag → import).
  soulseekReview: () =>
    json<{ dir: string; files: { path: string; file: string; ext: string; is_video: boolean; size: number; mtime: number; user: string; tags: Record<string, string | null>; tech: Record<string, number | string> }[] }>(
      `${API}/soulseek/review`, undefined, 60000
    ),
  soulseekLocalFileUrl: (path: string) => media(`${API}/soulseek/local-file?path=${encodeURIComponent(path)}`),
  // Playable video preview — native stream when the browser can decode the
  // container, otherwise a live ffmpeg transcode (DVD VOB / Blu-ray M2TS).
  soulseekPreviewStreamUrl: (path: string) => media(`${API}/soulseek/preview-stream?path=${encodeURIComponent(path)}`),
  soulseekDeleteLocal: (path: string) =>
    json<{ ok: boolean }>(`${API}/soulseek/local-file/delete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    }),
  soulseekImport: () =>
    json<{ ok: boolean; moved: string[]; failed?: { album: string; reason: string }[]; organized?: boolean; organize_error?: string; media_tagged?: number; converted?: number }>(`${API}/soulseek/import`, { method: "POST" }, 120000),
  soulseekAutoStatus: () =>
    json<SlskAutoJob>(`${API}/soulseek/auto`, undefined, 30000),
  soulseekAutoStart: (body: { release_mbid?: string; queries?: string[]; username?: string; target_dir?: string }) =>
    json<{ ok: boolean; job: SlskAutoJob }>(`${API}/soulseek/auto`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }, 180000),
  soulseekAutoCancel: () =>
    json<{ ok: boolean }>(`${API}/soulseek/auto/cancel`, { method: "POST" }, 30000),
  /** Answer the "only lossy copies found" prompt (accept = download anyway). */
  soulseekAutoConfirm: (accept: boolean) =>
    json<{ ok: boolean; accepted: boolean }>(`${API}/soulseek/auto/confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ accept }),
    }, 30000),
  soulseekTestLog: (username: string, files: { filename: string; size: number }[]) =>
    json<{ ok: boolean; threshold: number; logs: { file: string; score: number | null; checksum: string | null; detail: string | null }[] }>(`${API}/soulseek/test-log`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, files }),
    }, 240000),
  soulseekSharesRefresh: () =>
    json<{ ok: boolean; message: string }>(`${API}/soulseek/shares/refresh`, { method: "POST" }, 120000),
  soulseekLogin: (username: string, password: string) =>
    json<{ ok: boolean; logged_in: boolean; message: string }>(`${API}/soulseek/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    }, 90000),
  mbGenresWrite: (paths: string[], count?: number) =>
    json<{ ok: boolean; updated: number; genres: string[]; per_track: boolean }>(`${API}/mb/genres`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths, count }),
    }, 120000),
  trackDownloadUrl: (path: string) => media(`${API}/track/download?path=${encodeURIComponent(path)}`),
  trackExportUrl: (path: string, codec: string, bitrate: number, level = 5) =>
    media(`${API}/track/export?path=${encodeURIComponent(path)}&codec=${encodeURIComponent(codec)}&bitrate=${bitrate}&level=${level}`),

  likes: () => json<{ paths: string[] }>(`${API}/likes`),
  likeToggle: (path: string, mbid?: string) =>
    json<{ ok: boolean; liked: boolean }>(`${API}/likes/toggle`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, mbid: mbid ?? null }),
    }),
  favorites: () => json<{ albums: string[]; artists: string[]; playlists: string[] }>(`${API}/favorites`),
  favoriteToggle: (kind: "album" | "artist" | "playlist", key: string, mbid?: string) =>
    json<{ ok: boolean; fav: boolean }>(`${API}/favorites/toggle`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, key, mbid: mbid ?? null }),
    }),
  // export to device
  exportDrives: () => json<{ drives: { letter: string; root: string; type: string; free: number | null; total: number | null }[] }>(`${API}/export/drives`),
  /** Codec specs come from the server (quality presets, custom ranges and
   * the kbps hints the drive-fit estimate uses) so the page never mirrors a
   * table the backend owns. */
  exportCodecs: () => json<{ codecs: Record<string, ExportCodecSpec> }>(`${API}/export/codecs`),
  exportDefaults: () => json<ExportForm>(`${API}/export/defaults`),
  exportRun: (body: ExportForm & { paths: string[] }, timeoutMs = 1800000) =>
    json<{
      ok: boolean; total: number; exported: number; skipped: number; failed: number;
      bytes: number; sidecars: number; playlists: number; verified: number;
      pruned: number; pruned_files: string[]; warnings: string[];
      error_count: number; errors: string[]; estimated_bytes: number | null;
    }>(`${API}/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }, timeoutMs),
  /** Write the Export page's form back into config.json (its saved defaults).
   * Every field maps onto the `export_<field>` config key the server reads. */
  exportSaveDefaults: (form: ExportForm) =>
    json<Record<string, unknown>>(`${API}/config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.fromEntries(
        Object.entries(form).map(([k, v]) => [`export_${k}`, v]))),
    }),

  soulseekUser: (username: string) =>
    json<any>(`${API}/soulseek/user/${encodeURIComponent(username)}`, undefined, 30000),

  // Wishes — MusicBrainz releases saved now, auto-filled from Soulseek later
  wishes: () => json<WishesPayload>(`${API}/wishes`, undefined, 30000),
  wishAdd: (body: { release_mbid: string; title?: string; artist?: string; year?: string; note?: string; target_dir?: string; queries?: string[] }) =>
    json<{ ok: boolean; wish: Wish }>(`${API}/wishes`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }, 60000),
  wishUpdate: (id: number, patch: { note?: string; target_dir?: string; status?: string; queries?: string[] }) =>
    json<{ ok: boolean; wish: Wish }>(`${API}/wishes/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
  wishDelete: (id: number) => json<{ ok: boolean }>(`${API}/wishes/${id}`, { method: "DELETE" }),
  wishSearch: (id: number) => json<{ ok: boolean; error?: string }>(`${API}/wishes/${id}/search`, { method: "POST" }, 30000),
  wishesSearchAll: () => json<{ ok: boolean; error?: string }>(`${API}/wishes/search-all`, { method: "POST" }, 30000),
  wishesReconcile: () => json<{ ok: boolean; resolved: number }>(`${API}/wishes/reconcile`, { method: "POST" }, 120000),
  /** Import the download a wish is waiting on (its album, all the way
   *  through: convert → tag → organize → the configured script chain). The
   *  wish turns `imported` when it lands, which raises the "wish found"
   *  notification. 409 = nothing has downloaded for it yet. */
  wishImport: (id: number) =>
    json<ImportRun>(`${API}/wishes/${id}/import`, { method: "POST" }, 30000),

  /** Albums sitting in the download dir, done downloading, waiting to be
   *  imported (the "Import all completed" worklist). */
  soulseekReady: () => json<ReadyAlbums>(`${API}/soulseek/ready`, undefined, 60000),
  /** Import ONE album, in the background (progress: importAllStatus). */
  soulseekImportOne: (path: string) =>
    json<ImportRun>(`${API}/soulseek/import-one`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    }, 30000),
  /** Import every finished download, one album at a time. */
  soulseekImportAll: () => json<ImportRun>(`${API}/soulseek/import-all`, { method: "POST" }, 30000),
  importAllStatus: () => json<ImportRunStatus>(`${API}/soulseek/import-all/status`),
  importAllCancel: () => json<ImportRun>(`${API}/soulseek/import-all/cancel`, { method: "POST" }),

  // Home page (recommendations + highlights)
  // `refresh` is the "Your library" card's button: the payload is TTL-cached
  // server-side and built from the equally-cached library tree, so a plain
  // refetch showed the same rows for minutes. The flag drops those caches.
  home: (refresh = false) =>
    json<HomeData>(`${API}/home${refresh ? "?refresh=1" : ""}`, undefined, 120000),

  // ----------------------------------------------------------------- //
  // Discovery — the provider catalogue behind Settings' order editors. //
  // ----------------------------------------------------------------- //
  discoverySources: () => json<DiscoveryCatalog>(`${API}/discovery/sources`),

  // ----------------------------------------------------------------- //
  // Artist artwork + descriptions, album descriptions                  //
  // ----------------------------------------------------------------- //
  /** `artist` accepts the artist folder path (from the artist payload) or a
   *  plain artist name. */
  artistArtwork: (artist: string) =>
    json<ArtistArtwork>(`${API}/artist/artwork?artist=${encodeURIComponent(artist)}`),
  artistImageUrl: (artist: string) => media(`${API}/artist/image?artist=${encodeURIComponent(artist)}`),
  artistImageCandidates: (artist: string) =>
    json<{ artist: string; rows: DiscoveryImageRow[] }>(
      `${API}/artist/image/candidates?artist=${encodeURIComponent(artist)}`, undefined, 45000
    ),
  artistImageSave: (artist: string, url = "", source = "") =>
    json<{ ok: boolean; file: string; source: string; source_url: string; image: ArtistArtworkImage }>(
      `${API}/artist/image`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ artist, url, source }),
      },
      60000
    ),
  artistImageUpload: (artist: string, file: File) => {
    const fd = new FormData();
    fd.append("artist", artist);
    fd.append("file", file);
    return json<{ ok: boolean; file: string; image: ArtistArtworkImage }>(
      `${API}/artist/image/upload`,
      { method: "POST", body: fd },
      60000
    );
  },
  artistImageClear: (artist: string) =>
    json<{ ok: boolean }>(`${API}/artist/image?artist=${encodeURIComponent(artist)}`, { method: "DELETE" }),
  artistDescriptionSave: (artist: string, text = "") =>
    json<{ ok: boolean; source: string | null; text: string; description: ArtistArtworkDescription }>(
      `${API}/artist/description`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ artist, text }),
      },
      45000
    ),
  artistDescriptionClear: (artist: string) =>
    json<{ ok: boolean }>(`${API}/artist/description?artist=${encodeURIComponent(artist)}`, { method: "DELETE" }),
  albumDescriptionSave: (path: string, text = "", artist = "", album = "") =>
    json<{ ok: boolean; source: string | null; text: string }>(
      `${API}/album/description`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, text, artist, album }),
      },
      45000
    ),
  albumDescriptionClear: (path: string) =>
    json<{ ok: boolean }>(`${API}/album/description?path=${encodeURIComponent(path)}`, { method: "DELETE" }),

  // ----------------------------------------------------------------- //
  // Lyrics — the synced provider chain (LRCLIB → NetEase → Kugou →    //
  // QQ Music → Kuwo → YouTube captions); see Settings → Lyrics.       //
  // ----------------------------------------------------------------- //
  lyricsProviders: () => json<LyricsProviders>(`${API}/lyrics/providers`),
  /** Auto-import lyrics for one or more tracks through the provider chain.
   *  Set force to re-fetch a track that already has lyrics. */
  lyricsAuto: (paths: string[], force = false, staged = false) =>
    json<{ results: LyricsAutoResult[]; order: string[]; ok: number; skipped: number; failed: number }>(
      `${API}/lyrics/auto`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths, force, staged }),
      },
      120000
    ),
  lyricsFind: (artist: string, title: string, album = "", duration = 0) => {
    const p = new URLSearchParams({ artist, title });
    if (album) p.set("album", album);
    if (duration) p.set("duration", String(duration));
    return json<LyricsHit>(`${API}/lyrics/find?${p}`, undefined, 45000);
  },

  // ----------------------------------------------------------------- //
  // Import — AcoustID matching, script chain, bulk queue               //
  // ----------------------------------------------------------------- //
  /** Fingerprint an album (folder or track paths) and return the MusicBrainz
   *  release group the audio actually is. `apply` also writes the accepted
   *  match's identity tags (ACOUSTID_ID / ACOUSTID_FINGERPRINT) into the files. */
  importAcoustid: (paths: string[], apply = false, staged = false) =>
    json<AcoustidMatch>(
      `${API}/import/acoustid`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths, apply, staged }),
      },
      600000
    ),
  /** Run the configured import script chain over already-imported albums. */
  importFinish: (paths: string[], force: Record<string, boolean> = {}, staged = false) =>
    json<{ albums: { path: string; chain: number[]; scripts: unknown[]; errors: unknown[] }[] }>(
      `${API}/import/finish`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths, force, staged }),
      },
      1800000
    ),
  /** Bulk import: move several staged albums into the library at once. */
  importBulk: (items: { path: string; move?: boolean; release?: Record<string, unknown> }[]) =>
    json<ImportBulkResult>(
      `${API}/import/bulk`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items }),
      },
      60000
    ),
  importBulkStatus: () => json<ImportBulkJob>(`${API}/import/bulk/status`),
  importScriptsPreview: (paths: string[] = []) =>
    json<ImportScriptsPreview>(
      `${API}/import/scripts/preview`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths }),
      }
    ),

  // ----------------------------------------------------------------- //
  // Bulk acquisition, genre facets, metadata review, video matching.   //
  // ----------------------------------------------------------------- //
  /** Queue a release / release group / whole artist into the auto-import
   *  pipeline. `mode: "best"` takes one release per release group (the
   *  preferred format), `"all"` every release. The server only RESOLVES for a
   *  few seconds and queues the rest unresolved, so a slow MusicBrainz must
   *  not hold the button: 20 s is already generous. */
  mbAutoImport: (body: { mbid: string; kind?: "release" | "release_group" | "artist" | "auto"; mode?: "best" | "all" }) =>
    json<{ queued: number; items: { mbid: string; title: string; status: string }[]; skipped: { mbid: string; reason: string }[] }>(
      `${API}/mb/auto-import`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
      20000
    ),
  /** Fetch the advisory rating (ITUNESADVISORY) for one release or a set of
   *  tracks — the values land in `values` and are written to `paths`, and
   *  `answers` reports every provider that had something to say (the UI's
   *  provenance). */
  mbAdvisoryFetch: (body: { paths?: string[]; release_mbid?: string; staged?: boolean }) =>
    json<AdvisoryFetchResult>(`${API}/mb/advisory/fetch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }, 120000),

  /** Check INSTRUMENTAL for a set of tracks: each value the sources can state
   *  is written, and `evidence` reports who decided it. */
  instrumentalFetch: (paths: string[], staged = false) =>
    json<InstrumentalFetchResult>(`${API}/instrumental/fetch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths, staged }),
    }, 120000),

  soulseekDownloadBulk: (username: string, files: { filename: string; size?: number }[]) =>
    json<{ queued: number }>(`${API}/soulseek/download-bulk`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, files }),
    }, 120000),
  /** Queue everything a peer shares (or one folder of it). */
  soulseekDownloadUser: (username: string, folder?: string) =>
    json<{ queued: number; scanned: number; skipped: number }>(`${API}/soulseek/download-user`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, folder }),
    }, 180000),
  soulseekSearchCancel: (id: string) =>
    json<{ ok: boolean }>(`${API}/soulseek/search/cancel`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    }, 30000),

  /** Import genres for a set of album/track paths.

   *  `sources` picks which providers to ask — the wizard's two per-source
   *  buttons pass one each (MusicBrainz, RateYourMusic); omitted asks every
   *  configured source in order. `per_source`/`notes` report what each source
   *  contributed and why one stayed silent. */
  genresImport: (paths: string[], limit?: number, sources?: string[], staged = false) =>
    json<{
      updated: number;
      genres: string[];
      per_source: Record<string, string[]>;
      notes: Record<string, string>;
      per_track: boolean;
      sources: Record<string, string[]>;
      levels: Record<string, string | null>;
      /** The configured genres-per-track cap (`mb_genre_count`) this run
       *  applied, and how many tracks had extra values trimmed to reach it. */
      genre_count: number;
      trimmed: number;
    }>(`${API}/genres/import`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths, limit, sources, staged }),
    }, 300000),
  /** Fetch what an album's metadata step owes: the artist's image and
   *  description, and the album's own description. Omit `items` for all
   *  three, or send one per request to drive a bar per item (each call is
   *  cheap once the content is stored). Per item: `{state, source, detail}` —
   *  `state` is `fetched` / `present` / `disabled` (the Settings toggle) /
   *  `not-found` / `error`, so a row can say why nothing arrived instead of
   *  showing a dead button. `force=false` twice in a row is a no-op. */
  albumMetadataFetch: (body: { path: string; items?: MetadataItemKind[]; force?: boolean; staged?: boolean }) =>
    json<{ ok: boolean; path: string; items: Partial<Record<MetadataItemKind, MetadataFetchItem>> }>(
      `${API}/album/metadata/fetch`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
      300000
    ),
  /** Genre browsing surface: every genre with its track count, plus the
   *  category cards that group them. */
  genresFacets: () =>
    json<{ genres: { name: string; count: number }[]; categories: { name: string; genres: string[] }[] }>(
      `${API}/genres/facets`
    ),

  /** Candidate artist images + descriptions for the metadata review modal. */
  metadataCandidates: (artist: string, albumPath?: string, staged = false) => {
    const p = new URLSearchParams({ artist });
    if (albumPath) p.set("album_path", albumPath);
    if (staged) p.set("staged", "1");
    return json<MetadataCandidates>(`${API}/metadata/candidates?${p}`, undefined, 90000);
  },
  /** Apply one reviewed candidate: an artist image URL, or the artist/album
   *  description text picked in the modal. */
  metadataApply: (body: {
    kind: "artist_image" | "artist_description" | "album_description";
    artist?: string;
    album_path?: string;
    image_url?: string;
    description?: string;
  }) =>
    json<{ ok: boolean; saved: string }>(`${API}/metadata/apply`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }, 120000),

  // ----------------------------------------------------------------- //
  // Source health — every provider the app can talk to, with the       //
  // status of its last probe (setup wizard + Settings → Sources).      //
  // ----------------------------------------------------------------- //
  /** All sources. `probe` runs a live test per source (slow, one network
   *  round trip each); without it the rows come back with their cheap
   *  configured/needs state and the previous status. */
  sourcesHealth: (probe = false, kind?: SourceKind) => {
    const q = new URLSearchParams();
    if (kind) q.set("kind", kind);
    q.set("probe", probe ? "1" : "0");
    return json<SourcesHealth>(`${API}/sources/health?${q}`, undefined, 120000);
  },
  /** One source's row — the per-row Test button. Always probes live. `kind`
   *  disambiguates the ids that exist in two families (deezer and itunes are
   *  both a genre source and a metadata provider); the row carries its own
   *  `kind` back, which is what the caller matches on. The live route answers
   *  `{source, checked_at}` and the agreed shape is the bare row — both read. */
  sourceHealth: (id: string, probe = true, kind?: SourceKind) =>
    json<SourceHealth | { source: SourceHealth }>(
      `${API}/sources/health/${encodeURIComponent(id)}?probe=${probe ? "1" : "0"}${kind ? `&kind=${kind}` : ""}`,
      undefined,
      60000
    ).then((r) => ("source" in r ? r.source : r)),

  /** URL of one video frame (`GET /api/videos/thumb`): JPEG bytes for the
   *  scrub preview. `t` is floored to whole seconds so repeated positions
   *  reuse one cache entry; 404s for a non-video or a path outside the
   *  music folder, which the caller swallows. */
  videoThumbUrl: (path: string, t: number, w = 160) =>
    media(`${API}/videos/thumb?path=${encodeURIComponent(path)}&t=${Math.max(0, Math.floor(t))}&w=${Math.round(w)}`),
  /** Download a music video from YouTube for one track (web/digital media). */
  videosDownloadYoutube: (body: { path?: string; artist: string; title: string; duration?: number }) =>
    json<{ ok: boolean; file?: string; candidate?: Record<string, unknown> }>(`${API}/videos/download-youtube`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }, 900000),
  /** Write TITLE/TRACKNUMBER/DISCNUMBER onto video files from the match-assist
   *  panel (one assignment per video file). */
  videosMatch: (albumPath: string, assignments: { path: string; title: string; tracknumber?: number; discnumber?: number }[]) =>
    json<{ updated: number } & ContainerSwap>(`${API}/videos/match`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ album_path: albumPath, assignments }),
    }, 600000).then(noteContainerSwap),
};
/** One row of `GET /api/capabilities`: what one feature can do HERE. */
export interface Capability {
  label: string;
  available: boolean;
  /** Why not — either the tool is missing, or this device can never run it
   *  (a sandbox that refuses to start a program). Null when it works. */
  reason: string | null;
  /** What would provide it; null when it already works or never can. */
  how: string | null;
  /** The Dependencies-table keys this feature needs, so those rows can be
   *  marked rather than each caller re-deriving the tool list. */
  tools: string[];
}

/** The features the report covers. The names are the server's own keys. */
export type CapabilityKey =
  | "core"
  | "lyrics"
  | "transcode"
  | "video"
  | "loudness"
  | "accuraterip"
  | "audit"
  | "logchecker"
  | "beets"
  | "acoustid"
  | "images"
  | "soulseek"
  | "keybpm"
  | "can_spawn";

export type Capabilities = Record<CapabilityKey, Capability> & {
  /** `sys.platform` of the host that answered ("win32", "linux", "darwin"). */
  platform: string;
  python: string;
  /** Whether the Dependencies installer can help at all on this platform. */
  installable: boolean;
};

/** Why every external tool is unavailable on this device, or null when the
 *  server can start one at all.
 *
 *  One measurement covers the whole Dependencies table: if the backend cannot
 *  start a program, no row of it can ever work here, and the reason is the
 *  platform's own (a sandbox that refuses to exec anything). When it CAN start
 *  one, a missing tool really is missing and the Install button is honest —
 *  which is why nothing here guesses per tool. */
export function deviceUnavailable(caps: Capabilities | undefined): string | null {
  if (!caps || caps.can_spawn.available) return null;
  return caps.can_spawn.reason || "unavailable on this device";
}

/** The features this device cannot run, in CAPABILITY_KEYS order. */
export function unavailableFeatures(caps: Capabilities | undefined): Capability[] {
  if (!caps) return [];
  return CAPABILITY_KEYS.filter((k) => !caps[k].available).map((k) => caps[k]);
}

/** Iteration order for the report — a fixed list, so both the summary line
 *  and the Dependencies table read the same features in the same order. */
export const CAPABILITY_KEYS: CapabilityKey[] = [
  "core",
  "lyrics",
  "transcode",
  "video",
  "loudness",
  "accuraterip",
  "audit",
  "logchecker",
  "beets",
  "acoustid",
  "images",
  "soulseek",
  "keybpm",
];
