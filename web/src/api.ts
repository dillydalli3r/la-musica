import type {
  AcoustidAlbumMatch,
  AcoustidMatch,
  AcoustidSubmitResult,
  ArtistArtwork,
  ArtistArtworkDescription,
  ArtistArtworkImage,
  CoverChoicePolicy,
  CoverInfo,
  CoverResult,
  CoverSearch,
  CoverSourceCatalog,
  CoverWriteResult,
  DiscoveryCatalog,
  DiscoveryImageRow,
  DownloadEntry,
  DownloadsPayload,
  HomeData,
  ImportAutonomy,
  ImportBulkJob,
  ImportBulkResult,
  ImportPrompt,
  ImportScriptsPreview,
  LayoutReport,
  LayoutSnapshot,
  LibraryAddResult,
  LyricsAutoResult,
  LyricsHit,
  LyricsProviders,
  LyricsPublishBatchResult,
  LyricsXlitResult,
  MBArtistBrowse,
  MBRecordingBrowse,
  MBReleaseChoicePayload,
  MBSearchFieldHelp,
  MBSearchRows,
  NeedsWarning,
  ScriptRunResult,
  SlskReleaseIdentity,
  SourceHealth,
  SourceKind,
  SourcesHealth,
  Wish,
  WishesPayload,
} from "./types";
import { toast } from "./store";
import * as offline from "./lib/offlineCache";
import { coverVersion, rememberCoverVersion } from "./lib/invalidate";
import { coverSearchPath, type CoverQuery } from "./lib/coverSearch";

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

/** The offline fallback in force, if any — which endpoint's request got no
 *  answer, and when the copy that answered it was written. Read right after a
 *  request by a caller that has to say WHICH answer came off disk (the cover
 *  finder does): `null` means the server answered. */
export function offlineFallback(): OfflineInfo | null {
  return offlineInfo;
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
  // The cookie jar's state is what the user is looking at RIGHT NOW while
  // pasting a file in: an offline copy would say "4 cookies saved" over a jar
  // that was just deleted.
  "/api/youtube/cookies": true,
  // Same reason for the RYM jar: names of a cookie that was just
  // replaced or cleared must never come from an offline copy.
  "/api/rym/cookies": true,
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

/** The YouTube cookie jar (`server/api_youtube.py`): the settings that decide
 *  whether yt-dlp sends cookies, plus what the jar on disk actually holds.
 *  `warnings` are the server's own sentences about it (a jar with no
 *  youtube.com cookie cannot sign a download in), shown as-is. */
export interface YoutubeCookies {
  mode: "none" | "file" | "browser";
  browser: string;
  present: boolean;
  path: string;
  bytes: number;
  /** COOKIE lines that parsed — a jar's comments are not cookies. */
  lines: number;
  sites: string[];
  saved_at: string | null;
  browsers: string[];
  max_bytes: number;
  warnings: string[];
}

/** The RateYourMusic credential (`server/api_rym.py`): what the stored
 *  `rym_cookie` holds. NAMES only — a `session` cookie is a live credential,
 *  so no route returns a value — plus the server's own sentences about it (a
 *  saved cookie with no `session` pair means RYM answers as a guest). */
export interface RymCookies {
  present: boolean;
  /** cookie pairs in the stored `Cookie` header. */
  lines: number;
  /** the one host these cookies are ever sent to. */
  sites: string[];
  names: string[];
  max_bytes: number;
  warnings: string[];
}

/** What an import answers with: the same state, plus how many pairs the last
 *  import actually stored (`0` when the file held no rateyourmusic.com cookie —
 *  nothing was replaced) and whether the stored credential now carries RYM's
 *  `session` cookie. */
export interface RymCookiesSaveReply extends RymCookies {
  stored: number;
  session: boolean;
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

/** `GET /api/export/structures` — the folder-structure menu the Export page
 *  renders (keys and labels, straight from server.exporter.STRUCTURES) and the
 *  vocabulary a custom structure script is written in. */
export interface ExportStructures {
  structures: { v: string; label: string }[];
  /** The %fields% a custom script may use (the naming grammar's variables). */
  fields: string[];
  /** The $functions it may call. */
  functions: string[];
}

/** `POST /api/export/structure/preview` — what a user-typed structure writes
 *  for the server's sample track, or the sentence that refuses it (an unknown
 *  %field%, an empty result). Same validation the run itself applies. */
export interface ExportStructurePreview {
  ok: boolean;
  path: string;
  error: string;
}

/** One family of files a run can be asked to copy (`GET /api/export/files`,
 *  straight from server.exporter.FILE_FAMILIES): the key the request carries,
 *  the label the checkbox shows and the one-line explanation under it. */
export interface ExportFamily {
  v: string;
  label: string;
  hint: string;
}

/** The Export page's form — the request body, and (key for key, under
 * `export_<field>`) the saved defaults it loads on open. */
export interface ExportForm {
  /** "server" writes under a drive on the server; "zip" builds one archive the
   *  client downloads. A browser cannot write to the server's filesystem, so
   *  it resolves an unusable/empty value to "zip" (see ExportDialog). */
  target: string;
  dest: string;
  subfolder: string;
  codec: string;
  quality: string;
  /** "albumartist_album_disc" (the shipped tree, the library's own shape),
   *  "album", "flat", "mirror", or "custom" for `structure_script`. */
  structure: string;
  /** The user's own structure, a naming script (mlo.naming's grammar: %field%
   *  substitution, $if(), "/" for folders). Read when `structure` is
   *  "custom". */
  structure_script: string;
  embed_covers: boolean;
  embed_cover_jpeg_quality: number;
  embed_cover_resolution: number;
  id3v2: string;
  id3v1: boolean;
  /** "off" | "tags" (write ReplayGain tags, the player applies them) |
   *  "apply" (bake the correction into the exported audio). */
  replaygain_mode: string;
  /** How lyrics travel: "embedded" (the LYRICS tag inside the file), "lrc"
   *  (a .lrc beside the exported file) or "both". The saved default follows
   *  the library's own `lyrics_format`, so an export writes lyrics the way the
   *  library does until the user says otherwise. */
  lyrics: string;
  /** An equalizer preset or imported profile id; "" = none. */
  eq_profile: string;
  clean_tags: boolean;
  playlists: boolean;
  /** The switch `copy_files` replaced (server.exporter.LEGACY_SIDECAR_FAMILIES
   *  when it is on). The form no longer writes it — the file selection below
   *  is what the run reads — but a saved default or a saved config from before
   *  the selection existed still carries it, and the run still honours it. */
  sidecars: boolean;
  /** WHICH files the run writes: the family keys of server.exporter.
   *  FILE_FAMILIES — "audio" (the tracks themselves), "cover", "lyrics",
   *  "cue", "log", "description", "checksum", "text", "playlist", "other".
   *  An EMPTY list is refused by the server with a sentence (a run that copies
   *  nothing would write an empty folder), so the form must leave one ticked. */
  copy_files: string[];
  /** Write `checksums.sha256` at the export root (sha256sum -c compatible). */
  manifest: boolean;
  verify: boolean;
  prune: boolean;
  workers: number;
}

/** The archive a `target: "zip"` run built — one at a time, kept in the
 *  server's own export folder until the next one replaces it (`id` is a run
 *  stamp, not a durable key). */
export interface ExportZip {
  id: string;
  name: string;
  bytes: number;
  files: number;
  /** The server path to download it from ("/api/export/zip/<id>"). */
  url: string;
}

/** One biquad band of an equalizer profile — the shape mlo.eq's parser emits
 *  (`type`/`fc`/`gain`/`q`/`on`) and the one the player's WebAudio chain and
 *  the editor both build. `type` is an Equalizer APO type (PK, LS, HS, LSC,
 *  HSC, LP, HP, BP, NO); `fc` is Hz, `gain` dB, `q` the filter's width. */
export interface EqBand {
  type: string;
  fc: number;
  gain: number;
  q: number;
  /** An OFF band stays in the list — it is what the file said — and is simply
   *  not applied. */
  on: boolean;
}

/** One equalizer profile the server can bake into an export
 *  (`GET /api/export/eq`): the built-in presets, and the profiles the user
 *  imported from Equalizer APO / Peace text. `unsupported` names lines the
 *  server's own processor has no equivalent for (skipped, and reported);
 *  `errors` names BAND lines it cannot read, in which case the server refuses
 *  to apply the profile at all rather than exporting a different curve;
 *  `empty` is a profile with no filters in it at all — it exports the audio
 *  unchanged, which is not the same as a flat curve. */
export interface ExportEqProfile {
  id: string;
  label: string;
  preamp_db: number;
  filters: EqBand[];
  imported_at?: string;
  unsupported?: string[];
  errors?: string[];
  empty?: boolean;
  notes?: string[];
}

/** One AutoEq measurement (`GET /api/eq/autoeq/search`): a headphone the
 *  AutoEq project has equalized, from one measurement source. */
export interface EqAutoEqRow {
  /** The results-relative directory (`<source>/<rig>/<model>`) — pass it back
   *  to eqAutoEqImport() verbatim; it is not a URL. */
  id: string;
  model: string;
  /** Who measured it (oratory1990, crinacle, Rtings…) and on what rig. */
  source: string;
  rig: string;
}

export interface EqAutoEqSearch {
  rows: EqAutoEqRow[];
  /** How many measurements the cached index holds (0 = nothing fetched yet). */
  models: number;
  /** Unix seconds the cached index was written (0 = never). */
  fetched_at: number;
  /** Why the index could not be refreshed, or "" — the rows are still usable. */
  error: string;
}

export interface ExportEq {
  presets: ExportEqProfile[];
  profiles: ExportEqProfile[];
  /** The server's own line about what applying a profile does, shown verbatim. */
  note: string;
}

/** One saved export configuration (`/api/export/configs`): the Export page's
 *  form under a name.
 *
 *  `config` is that form — every value the page's controls carry, plus
 *  `source_kind`, the source tab it was saved from — so a loaded config can be
 *  posted to `/api/export` exactly as it stands. The SELECTION is not part of
 *  it (which albums are ticked is data, not configuration).
 *
 *  `eq_missing` / `eq_problem` describe the equalizer profile it names: a
 *  profile deleted or renamed after the config was saved is reported here (and
 *  by the run, which refuses it) instead of the export quietly using another
 *  curve. */
export interface ExportSavedConfig {
  id: string;
  name: string;
  saved_at: string;
  config: Partial<ExportForm> & { source_kind?: string };
  /** The profile id the config names ("" = no equalizer). */
  eq_profile: string;
  /** The profile is not there any more (deleted or renamed). */
  eq_missing: boolean;
  /** The sentence to show about that profile, or "" when it is usable. */
  eq_problem: string;
  /** Set by a save that replaced a config of the same name. */
  replaced?: boolean;
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
  /** The LISTEN port's real state: whether anything accepts on it here, who
   *  holds it, and what the router was actually told (mlo.portmap). A mapping
   *  is only `mapped` when a router confirmed it, so the tab may say "opened"
   *  only then — `refused` carries the router's own words in `detail`. */
  listen_port_state: {
    listen_port: number;
    listening: boolean;
    /** "slskd" when our daemon accepts on it, "another program" when a
     *  foreign process does — the conflict the WEB port already reports. */
    holder: "" | "slskd" | "another program";
    bindable: boolean;
    /** Set only when another program holds the port. */
    conflict: string | null;
    /** What is true here: the port unbound while slskd runs, or unholdable. */
    error: string;
    /** slskd's OWN last log line about a listen port ("" when it said none). */
    slskd_error: string;
    mapping: {
      enabled: boolean;
      listen_port: number;
      /** The port the last verdict was about. It can differ from
       *  `listen_port_state.listen_port`, which is the SAVED port, so a bar
       *  that describes a mapping must name this one. */
      mapped_port: number;
      state: "off" | "mapped" | "refused" | "no_gateway" | "unsupported"
        | "error" | "pending" | "checking" | "client_down";
      detail: string;
      method: "" | "upnp" | "natpmp";
      verified: boolean;
      external_ip: string;
      internal_ip: string;
      gateway: string;
      tried: { method: string; state: string; detail: string }[];
      /** The raw protocol record behind `tried`: whether each method's
       *  request was answered at all, and the endpoint's own words (a SOAP
       *  fault code, a NAT-PMP result code) when it was. */
      attempts: { method: string; answered: boolean; ok: boolean; detail: string }[];
      checked_at: number;
      in_flight: boolean;
      /** When the lease a gateway granted runs out (0 = none was stated, so the
       *  entry does not expire on its own). A confirmed mapping whose lease has
       *  passed may be gone — the gateway drops the entry when it runs out. */
      expires_at: number;
    };
  };
}

/** `/api/soulseek/port-check` — one row per thing this machine can actually
 *  prove about the listen port (server/soulseek_port.py), for the "Test port"
 *  button.
 *
 *  `proves`/`cannot` are part of every row on purpose: a green row is not a
 *  promise that the internet reaches the port, and the pair is what says so where
 *  the state alone would not be read as more than it is. `verdict` is the worst
 *  row's state, or `ok` only when something accepts on the port AND the router
 *  lists a mapping for it; `note` is what no row can say. */
export interface SlskPortCheck {
  ok: boolean;
  port: number;
  checks: {
    id: string;
    label: string;
    state: "ok" | "warn" | "fail" | "unknown";
    detail: string;
    /** What a pass on this row means. */
    proves: string;
    /** What it can never tell, however green it is. */
    cannot: string;
  }[];
  verdict: "ok" | "warn" | "fail" | "unknown";
  note: string;
  checked_at: string;
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
  /** The job's own id, as server/soulseek_auto.py publishes every job (its
   *  `_published`). The queue rows carry the same id as `job_id`, which is what
   *  lets a live progress frame be matched to the job it belongs to. */
  id?: number;
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
  /** The album's own import chain (links, metadata, cover art, then the
   *  configured scripts), which runs on a thread of its own AFTER the job is
   *  state "done". `running` is what tells a surface that the album is in the
   *  library but NOT finished — the job's "done" alone does not say that. */
  chain?: { running?: boolean } | null;
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

/** One row of the ONE download queue (`GET /api/queue`, server/api_queue.py):
 *  a wish, a running auto-import job, a release waiting in the pipeline, an
 *  import run or a finished download still in the download folder.
 *
 *  `stage` is the shared vocabulary — queued / searching / downloading /
 *  verifying / importing / completed / failed / needs_attention — so a wish
 *  from MusicBrainz and a folder grabbed off the Soulseek page read the same.
 *  `source` says which of those put it there. */
export interface SlskQueueItem {
  /** "<kind>:<ref>" — pass it back verbatim to queueCancel(). */
  id: string;
  kind: "wish" | "job" | "pipeline" | "import" | "ready" | "prompt";
  /** Set when this row IS a pipeline job (running or settled). */
  job_id: number | null;
  /** Set when a wish owns this row. */
  wish_id: number | null;
  stage: "queued" | "searching" | "downloading" | "verifying" | "importing"
       | "completed" | "failed" | "needs_attention" | "background";
  source_key: "musicbrainz" | "soulseek" | "auto" | string;
  source: string;
  title: string;
  artist: string;
  release_mbid: string;
  /** Where the album is (or landed) — the album link, when there is one. */
  album_path: string;
  /** A finished download's folder in the download dir (kind "ready"): what
   *  the Import action on that row sends. Not a library album. */
  path?: string;
  /** The release's own identity — the row's `release` block: the catalogue
   *  number and medium that identify the pressing, its country/date/track
   *  count, the edition's disambiguation and MusicBrainz's status. Present on
   *  every row the server builds (server/api_queue.py); empty on the rows that
   *  cannot know one (a stalled album already in the library, a finished
   *  download in the folder). Nothing here ever costs a MusicBrainz request. */
  release?: SlskReleaseIdentity;
  progress: {
    text?: string;
    /** The search query slskd is answering RIGHT NOW (searching rows only):
     *  a job asks its queries one after another, so this is which one the row
     *  is waiting on. Set from `soulseek_auto._job_search_progress`. */
    query?: string;
    /** slskd's own state word for that query ("InProgress", "Completed", …),
     *  never invented here. */
    state?: string;
    /** The candidate whose folder is being fetched (downloading rows only):
     *  `peer` is the username and `peer_dir` its folder, both as the job's own
     *  download snapshot publishes them. `phase` is what the job says it is
     *  doing with that candidate. */
    peer?: string;
    peer_dir?: string;
    phase?: string;
    done?: number;
    total?: number;
    /** byte-weighted share, null while a search has nothing to weigh */
    percent?: number | null;
    files_done?: number;
    /** Files the job's wait has ACCEPTED on disk — deliberately not the same
     *  number as `files_done` (what slskd calls complete). */
    files_arrived?: number;
    files_total?: number;
    speed?: number | null;
    eta_s?: number | null;
  } | null;
  /** Why it failed (or what the job is waiting on). On a wish row this is the
   *  store's own `last_error`, which now carries the rejected candidates'
   *  reasons (the peer and the refusal), so a row stuck on rejections explains
   *  itself without the log. */
  reason: string;
  /** WHAT the job is doing right now, in its own words — the last line it
   *  wrote to its log ("Waiting for the album folder …"). Empty when nothing
   *  live is behind the row. */
  stage_text?: string;
  /** WHY nothing has landed yet: the candidates already refused, newest last,
   *  each with slskd's own reason (`rejected_count` is how many there really
   *  were — this list is bounded). */
  rejected?: { username: string; dir: string; reason: string }[];
  rejected_count?: number;
  /** True while the album's own import chain (links, metadata, cover art and
   *  the configured scripts) is still running on its own thread: the album IS
   *  in the library at that point, and this is what says it is not finished. */
  chain_running?: boolean;
  /** Whether this row can be taken off the list (POST /api/queue/clear). True
   *  only for rows whose work is OVER (a settled job, an imported wish, one
   *  nothing was found for) — a row still in the pipeline is CANCELLED instead,
   *  and a finished download waiting to be imported is not clearable at all
   *  (its action is the import; its bytes are the staging card's). */
  clearable: boolean;
  /** One line about what this row's state means right now. */
  note: string;
  /** True on a "pipeline" row: the release has NOT started. It is waiting for a
   *  free slot in the pipeline (see `position`), because
   *  `soulseek_search_concurrency` releases are already running — the page
   *  groups these rows as Waiting, which is a different thing from a wish
   *  waiting for the network to answer. */
  waiting?: boolean;
  /** 1-based place in the waiting queue; on `waiting` rows only. */
  position?: number;
  attempts?: number;
  /** Why a FAILED row stopped: "not_found" is never retried on its own, every
   *  other reason is worth another press once the cause is fixed. */
  outcome?: string;
  /** Partial bytes of rejected candidates that could not be removed — still in
   *  the download folder, reported rather than left for someone to find. */
  leftovers?: string[];
  /** True when the import moved the album but the naming script could not
   *  place EVERY file: the album is in the library and the files that stayed
   *  behind are still in the download folder (import that folder again to
   *  finish it). `organize_error` is the import's own sentence about them. */
  partial?: boolean;
  organize_error?: string;
  /** The format a completed album arrived in when it was taken as a LOSSY copy
   *  under `soulseek_auto_lossy_policy` ("MP3", …; "" otherwise) — the row must
   *  not read as the lossless import every other row is. */
  lossy?: string;
  /** What this row's manual action is ("manual" = enter it by hand in the
   *  wizard, "answer" = a question parked on the auto-import tab), and where
   *  it happens. "" when the row has no such action. */
  action?: "manual" | "answer" | "";
  action_link?: string;
  /** Whether POST /api/queue/retry can bring this row back (a terminal state:
   *  nothing found, or a failed job). */
  retryable?: boolean;
  /** Families this album is still missing, in wizard order — the ids the
   *  wizard's `?missing=` parameter takes. Set on the row of ANY album an
   *  import is short of a family, whatever the row's own kind and stage: the
   *  release is finished (the album is in the library), and this is the
   *  warning on it, not a hold. */
  missing?: string[];
  missing_labels?: string[];
  /** The whole warning, when there is one: what is missing, the wizard link
   *  and which kind of entry it came from (`NeedsWarning.waiting` is the one
   *  case that really is held — a review import). */
  needs?: NeedsWarning;
  /** The wizard link that opens the album AT the first missing family — the
   *  same link the import_needs_data notification carries. */
  wizard_link?: string;
  /** Whether POST /api/import/prompts/dismiss applies (a row carrying
   *  `needs`, or a standalone warning row). */
  dismissable?: boolean;
  /** Empty searches so far (wish rows): the budget `wishes_not_found_attempts`
   *  is compared against. */
  not_found?: number;
  /** When the next AUTOMATIC attempt may run, 0 when none will (a terminal
   *  row waits for the user's own retry). */
  retry_at?: number;
  /** When the WORKER looks at this row again on its own (0 = never: the row is
   *  terminal): the failed attempt's backoff when there is one, the search
   *  interval otherwise (`server.wishes.due_at`). What a waiting row counts
   *  down to — `retry_at` alone cannot answer it, because a wish with no
   *  failure behind it still has a next search. */
  due_at?: number;
  /** The ranked-candidate FALLBACK WALK this release is being searched with
   *  (spec R150-R154): the release-choice policy's ranked editions, best first,
   *  walked one at a time inside the one wish — so a release is ONE row however
   *  many candidates it is trying. `{index, total, label, mbid, title, tried}`,
   *  null for a wish with nothing to walk (one candidate, or none). */
  walk?: SlskWalk | null;
  /** A wish whose framework album ("Add to library") is on disk with no audio
   *  yet: cancelling it removes the folder too. */
  pending?: boolean;
  created_at: number;
  updated_at: number;
  cancelable: boolean;
  /** The job's last few log lines, for the row's expander. */
  log_tail: string[];
}

/** One step of the ranked-candidate fallback walk (`SlskQueueItem.walk`).
 *  Built by `server.wishes.candidate_state`, so the row, the album page and the
 *  notification all describe the position the same way. */
export interface SlskWalk {
  /** 0-based position being asked for right now. */
  index: number;
  /** How many ranked editions the walk may ask (`soulseek_fallback_candidates`,
   *  clamped to the list the release group actually has). */
  total: number;
  /** "Release 2 of 3" — the ONE wording for the position. */
  label: string;
  mbid: string;
  title: string;
  /** The candidates already asked in this attempt, best first. */
  tried: { mbid: string; title: string }[];
}

/** `GET /api/queue` — every section with its rows, plus the counts the tab
 *  badges show. The three numbers the pipeline runs on: `concurrency` releases
 *  at once (the overflow WAITS), `candidate_slots` candidate downloads per
 *  release, and `download_slots` — slskd's own transfer ceiling, which is the
 *  product of the other two at the shipped defaults (3 × 3 = 9) and which the
 *  app never relies on to hold either limit. */
export interface SlskQueuePayload {
  sections: {
    queued: SlskQueueItem[];
    in_progress: SlskQueueItem[];
    /** Releases whose ranked-candidate walk is spent: still searched, in their
     *  own subsection (spec R153). */
    background: SlskQueueItem[];
    needs_attention: SlskQueueItem[];
    completed: SlskQueueItem[];
    failed: SlskQueueItem[];
  };
  counts: {
    queued: number; in_progress: number; background: number;
    needs_attention: number;
    completed: number; failed: number; total: number;
  };
  running: number;
  concurrency: number;
  candidate_slots: number;
  download_slots: number;
}

/** What a clear may be scoped to (POST /api/queue/clear): every finished row
 *  the queue owns ("finished"), the wishlist alone ("wishes"), or one SECTION
 *  of the queue by name — what a section header's own Clear button sends, and
 *  exactly the rows it counted. */
export type SlskQueueScope = "finished" | "wishes"
  | "queued" | "in_progress" | "background" | "needs_attention" | "completed"
  | "failed";

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
      /** The release id the staged fetch was made for, so the entry is found
       *  again after the import chain relocates the album. */
      album_id?: string;
      /** Who answered the staged fetch — the badge the picker shows. */
      provider: string | null;
      staged_at: string;
      /** The candidates RANKED best-first by the one cover policy, each row
       *  carrying the reasons that put it there. */
      results: CoverResult[];
      /** The policy's own pick (mlo/cover_choice) and its reasons — what the
       *  picker shows as the default before the user overrides it. */
      chosen?: CoverResult | null;
      /** What every source did, including any that was skipped and why. */
      notes?: string[];
      /** The policy the ranking was made under (the minimum, the source order
       *  and the rules). */
      policy?: CoverChoicePolicy;
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
 *  `answers` carries all of them. `albums` is the ALBUMITUNESADVISORY the
 *  server DERIVED per album folder from those values (`album_updated` counts
 *  the writes), `gated` is how many files the ADVISORY write gate refused, and
 *  `skipped` says why nothing was written when it refused every file — a fetch
 *  that wrote nothing is never a success. */
export interface AdvisoryFetchResult {
  updated: number;
  values?: Record<string, string | number>;
  sources?: Record<string, string>;
  answers?: TrackAnswers;
  albums?: Record<string, string | number>;
  album_updated?: number;
  gated?: number;
  /** Album tags the per-filetype/derivation gate refused (see `gated`). */
  album_gated?: number;
  /** What happened to each reported value THIS run: `written` (the tag now
   *  holds what this run wrote), `unchanged` (the sources were asked and state
   *  what the file already carries), `existing` (the file's own valid 0/1/2 was
   *  echoed — nobody was asked) or `gated` (the write gate refused it). This is
   *  what stops `updated == 0` from reading as "the sources answered nothing":
   *  a fetch that rated nothing because nothing needed rating says so here. */
  status?: Record<string, "written" | "unchanged" | "existing" | "gated">;
  skipped?: string;
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
 *  failed leg arrives as null with its message in `errors`. The advisory leg is
 *  FORCED — every file is asked and what the sources state is written,
 *  including ones already carrying a 0/1/2 — because this function IS the
 *  advisory action of the surfaces that use it (the track page, the metadata
 *  review): a rating an earlier run invented must not keep answering for a file
 *  nobody asked about. The fill-only pass belongs to the IMPORT's own automatic
 *  fetch, where no user pressed anything. Only evidence lowers a stored rating,
 *  so the ladder's invented fallback never overwrites one even here. */
export async function checkTrackValues(paths: string[]): Promise<{
  adv: AdvisoryFetchResult | null;
  inst: InstrumentalFetchResult | null;
  errors: string[];
}> {
  const [adv, inst] = await Promise.all([
    api.mbAdvisoryFetch({ paths, force: true }).catch((e) => e as Error),
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
  /** Where the project lives and where a bug report goes — always present, so
   *  the app can link back to itself (credits footer, Settings). */
  project_url: string;
  issues_url: string;
  /** What this process is running as: the commit the image was built from
   *  (empty outside a release build), when, and whether it is an image at all.
   *  `version` alone cannot tell a stale image from a current one. */
  build: { revision: string; built: string; container: boolean };
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
 *  write, an organize); `elapsed` is measured on the server in seconds.
 *  `steps` is the `<at>/<of>` pair of a multi-step run (a script chain), the
 *  same pair the header bar prints; both surfaces are written from one frame
 *  (server/job_locks.publish), so they cannot show different steps. */
export interface JobLock {
  job: string;
  kind: string;
  label: string;
  started_at: number;
  elapsed: number;
  paths: string[];
  progress: { done: number; total: number | null; text: string; steps?: number[] | null } | null;
}

/** GET /api/jobs/locks — everything holding library paths right now. */
export interface JobLocksPayload {
  jobs: JobLock[];
}

/* ---------------------------------------------------------------------- *
 * Discover — browsing the library AND the online providers by genre       *
 * (server/api_discover.py), plus the online recommendation shelf.         *
 * ---------------------------------------------------------------------- */

/** Which side of the line a Discover request reads: the library, the online
 *  providers, or both at once. */
export type DiscoverScope = "library" | "online" | "all";

/** What a Discover row IS — the three shapes every view asks for. */
export type DiscoverKind = "albums" | "artists" | "tracks";

/** What an ENTITY shelf is seeded by: the page it sits on. An artist page
 *  seeds by the artist's MusicBrainz id when its tags carry one (else by its
 *  name), an album page by its release-group/release id (else artist+title),
 *  a track page by its recording id (else artist+title). */
export type DiscoverSeedKind = "artist" | "album" | "track";

/** One row of the Discover genre / recommendation routes.
 *
 *  `owned` means the library holds THIS item (matched by MBID, then by
 *  normalized artist+title) and `path` is set exactly then — so an owned row
 *  links into the library and an unowned one offers the add action, never the
 *  other way round. `in_library` is the weaker statement: the library holds
 *  something by that artist, which is a hint and not ownership. `source` is the
 *  provider that stated the row (registry-first when several did) and
 *  `source_label` the name to print; `also_from` lists the other sources that
 *  named the same row. */
export interface DiscoverItem {
  kind: "album" | "artist" | "track";
  title: string;
  artist: string;
  /** Release year as the provider stated it — "" when it stated none. */
  year: string;
  source: string;
  source_label: string;
  /** Provider artwork and page, both absolute URLs (the artwork is rendered
   *  through `artUrl`, never straight from the provider). */
  cover_url: string | null;
  page_url: string | null;
  mbid: string | null;
  release_group_mbid: string | null;
  /** Library path — present exactly when the library has this item. */
  path: string | null;
  /** The library holds this exact item. */
  owned?: boolean;
  /** The library holds something by this artist (not this item). */
  in_library?: boolean;
  /** The further sources that named the same row, primary excluded. */
  also_from?: string[];
  /** Library album rows: the track titles the folder holds. */
  tracks?: string[];
  /** The provider's OWN relevance for the row (Last.fm's match, Deezer's
   *  fans/rank, ListenBrainz's score) — null when the provider states none.
   *  The providers' scales are not comparable, so it orders rows within one
   *  source and is not a cross-provider percentage. */
  score?: number | null;
  /** Why this row is here ("genre: shoegaze"). */
  reason?: string;
}

/** Why a source said nothing: source id → the server's own words ("skipped:
 *  no lastfm_api_key"). A source absent from this map answered — or was never
 *  asked because no feed of its kind exists for the request (see
 *  `DiscoverNotApplicable`, which is information, not a silence).
 *
 *  Three keys are not failures: `recommended` is the recommendation shelf's own
 *  verdict on the seed (the one non-source key), and a note beginning
 *  "partial:" is a source answering with part of a long list. A source the
 *  server does not know is keyed by what was asked for, with "unknown source". */
export type DiscoverNotes = Record<string, string>;

/** A source an ENTITY shelf cannot use at all, with the provider's own reason
 *  (`GET /api/discover/recommended?seed_kind=…`): a similar-ARTISTS feed
 *  cannot be asked about an album, and a limit the provider publishes is not a
 *  failed request. `short` is the compact marker to print beside the source's
 *  name ("artist pages only", "no similar-entity feed") and `why` the full
 *  sentence for the tooltip — the label is short BY DESIGN, never a sentence
 *  cut off by the pill it sits in. */
export interface DiscoverNotApplicable {
  id: string;
  label: string;
  short: string;
  why: string;
}

/** `GET /api/discover/genres` — the genre list of the requested scope, with
 *  the counts each side can state (library counts are tracks, albums and
 *  artists; online ones are what the providers reported). */
export interface DiscoverGenres {
  genres: {
    name: string;
    track_count: number;
    album_count: number;
    artist_count: number;
    /** Which side(s) contributed this genre — "library", "musicbrainz", … */
    sources: string[];
  }[];
  sources_asked: string[];
  notes: DiscoverNotes;
}

/** `GET /api/discover/genre` — one page of rows. `next_offset` is the cursor
 *  for the next page, null/absent at the end. */
export interface DiscoverItems {
  genre: string;
  kind: DiscoverKind;
  source: string;
  items: DiscoverItem[];
  sources_asked: string[];
  notes: DiscoverNotes;
  next_offset: number | null;
}

/** `GET /api/discover/recommended` — an online shelf seeded by the whole
 *  library (`seed=library`) or by one genre. `basis` says what the rows were
 *  built from. The entity seed (`seed_kind`) also returns `not_applicable`:
 *  the sources whose feed cannot answer a page of this kind, reported as
 *  information rather than as a skip. */
export interface DiscoverRecommended {
  items: DiscoverItem[];
  sources_asked: string[];
  notes: DiscoverNotes;
  basis: string;
  not_applicable?: DiscoverNotApplicable[];
}

/** The four closed windows every chart can be asked for — the same vocabulary
 *  on both sides of the line: the library's own history (`/api/top`) and the
 *  providers' charts (`/api/discover/charts`). */
export type ChartPeriod = "all" | "year" | "month" | "week";

/** One provider's chart, as ONE row of `GET /api/discover/charts`: the shared
 *  Discover row (so it renders and adds exactly like a genre row) plus the
 *  provider's own rank and score. `rank` is its position in that provider's
 *  chart and `score_label` its own words ("1.2M listens") — null for a
 *  provider that states no number, never a made-up one. */
export interface DiscoverChartItem extends DiscoverItem {
  rank: number;
  score: number | null;
  score_label: string | null;
}

/** What one provider can chart HERE — the payload's own support matrix, so the
 *  page can say "this source does not publish this week" from the answer it
 *  already has instead of a second request. */
export interface DiscoverChartSource {
  id: string;
  label: string;
  /** The windows this source really publishes. */
  periods: ChartPeriod[];
  /** Whether THIS request's window is one of them. */
  supports_period: boolean;
  needs: string[];
  missing: string[];
  ready: boolean;
}

/** `GET /api/discover/charts` — what the online providers rank for one window
 *  and one kind. Rows keep each provider's own order (they are never merged
 *  across providers), `sources_asked` is who was asked in the order they were
 *  asked — RateYourMusic first for tracks — and `notes` carries every outcome
 *  that was not an answer: `skipped:` (no key), `unsupported:` (no such window)
 *  or `failed:` (the provider's own words). The one non-source key, `charts`,
 *  is the verdict when nobody had anything to rank. */
export interface DiscoverCharts {
  period: ChartPeriod;
  kind: DiscoverKind;
  source: string;
  limit: number;
  items: DiscoverChartItem[];
  sources_asked: string[];
  notes: DiscoverNotes;
  sources: DiscoverChartSource[];
}

/** One row of `GET /api/top` — a track, an album or an artist from the USER'S
 *  own play history, with how many times it was played in the requested
 *  window. `path` is the library path and `in_library` whether the library
 *  still holds it (a play outlives a deleted file, so a row can be both). */
export interface TopRow {
  kind: "track" | "album" | "artist";
  /** Track and album rows: the library path. */
  path?: string;
  /** Track and album rows. */
  title?: string;
  artist?: string;
  album?: string;
  album_path?: string;
  /** Artist rows: the name, and the artist folder when the library has one. */
  name?: string;
  plays: number;
  in_library: boolean;
}

/** `GET /api/top` — the library's own most-played rows for one window, with
 *  the window echoed back (`window.start`/`end` are epoch seconds, null when
 *  unbounded) and a `note` that explains an empty list. */
export interface TopCharts {
  period: ChartPeriod;
  kind: DiscoverKind;
  limit: number;
  window: {
    period: string;
    start: number | null;
    end: number | null;
    start_iso: string | null;
    end_iso: string | null;
  };
  items: TopRow[];
  /** The WINDOW's own totals — every play it holds, not the page's rows.
   *  `listened_seconds` sums the played tracks' own measured lengths (a play
   *  records a start, so the track's length is the time behind it), and
   *  `listened_unknown` counts the plays with no length to add (a file the
   *  library no longer holds) rather than guessing one. */
  plays_total: number;
  listened_seconds: number;
  listened_plays: number;
  listened_unknown: number;
  note: string;
}

/** The three entities a star can name: a track (a file), an album (its folder)
 *  and an artist (their folder). The scope is what makes one store hold three
 *  INDEPENDENT verdicts — an album rating is the user's verdict on the album,
 *  NOT the average of its tracks, and the album page draws both, labelled. */
export type RatingsScope = "track" | "album" | "artist";

/** `GET /api/ratings` — every rated entity of ONE scope, or the requested
 *  subset.
 *
 *  `ratings` maps a normalized path (a track's file, an album's or an artist's
 *  folder) to the rating in HALF-STARS as an integer 0-10 (0 = unrated, 1 = a
 *  half star … 10 = five stars) — the unit the app-owned SQLite table, the API
 *  and (for tracks only) the `RATING` file tag (0-100, one half-star = 10,
 *  Picard's convention) all agree on. The UI works in the familiar 0-5 scale
 *  and converts in exactly one place (lib/ratings.ts); nothing outside it
 *  should ever see these integers.
 *
 *  `counts` is how many rows of that scope sit at each value, keyed "1"…"10"
 *  (a value with no rows is absent) — it is what the library/genre surfaces
 *  show without walking the map. `scope` echoes what was asked for, so a
 *  client can never decorate a row with the wrong scope's value. */
export interface RatingsPayload {
  scope?: RatingsScope;
  ratings: Record<string, number>;
  counts: Record<string, number>;
}

/* ── the library query engine ──────────────────────────────────────────────
 *
 *  ONE engine answers every way of browsing the library (see mlo/query.py):
 *  the Browse page's ad-hoc builder, the facet rail, the live match count and
 *  a saved smart playlist all send the SAME filter spec
 *  (`{conditions:[{field,op,value}], match}`), so a saved playlist can never
 *  disagree with the browser it was saved from.
 *
 *  `GET /api/library/fields` is the ONE field catalogue: the builder, the
 *  facet rail and the smart-playlist rule editor all render from it, so a
 *  field added on the server appears in every surface at once. */

/** One operator a field offers. `op` is the spec's vocabulary — the id the
 *  engine evaluates and a saved playlist stores; `label` is what the picker
 *  shows. Read them from the catalogue, never hardcode a list: the engine
 *  owns the set. */
export interface LibraryFieldOp {
  op: string;
  label: string;
}

/** One filterable field. `type` decides the value control (text input, number
 *  input, or a select over `values`); `ops` is the field's own operator set,
 *  in the order the picker lists it. `facetable` marks the fields the rail may
 *  count and list; `sortable` marks the keys `/api/library/query` accepts as a
 *  `sort.key`. Numeric fields carry `unit`/`min`/`max` for the value control
 *  (rating is 0-5 in half stars). */
export interface LibraryField {
  field: string;
  label: string;
  type: "text" | "number" | "enum" | "bool" | string;
  ops: LibraryFieldOp[];
  values?: (string | number)[];
  facetable?: boolean;
  sortable?: boolean;
  /** False when the library payload does not carry this tag yet, so every
   *  query on it answers empty — the picker says so rather than letting a user
   *  build a rule that can only ever match nothing. */
  in_payload?: boolean;
  /** Which results the field is meaningful for ("tracks"/"albums"); rating is
   *  tracks-only. The picker warns when the page is showing the other one. */
  targets?: string[];
  unit?: string;
  min?: number;
  max?: number;
  /** Value-control step (rating moves in half stars). */
  step?: number;
  /** One sentence on what the field means, shown as the picker row's tooltip. */
  hint?: string;
}

export interface LibraryFieldGroup {
  id: string;
  label: string;
  fields: LibraryField[];
}

/** `GET /api/library/fields`. */
export interface LibraryFields {
  groups: LibraryFieldGroup[];
}

/** One value of a facet with the number of matching rows. `value` is a number
 *  for numeric fields (a year, a rating) and a string otherwise. */
export interface FacetValue {
  value: string | number;
  count: number;
}

export interface LibraryFacet {
  field: string;
  values: FacetValue[];
  /** How many rows have NO value for this field (unrated, no genre). A blank
   *  value is never a `values[]` entry, so "Unrated" / "No genre" comes from
   *  here — without it a rating facet would hide the unrated rows entirely. */
  missing?: number;
  /** Distinct values before `limit` — what lets the rail say "50 of 1,204". */
  total_values: number;
}

/** A row-level condition. `value` is a scalar for the comparison ops, a
 *  two-element `[lo, hi]` for "between", an ARRAY for eq/ne ("is any of" /
 *  "is none of" — how a facet's multi-select becomes one OR group), and
 *  unused by the empty/present/unrated ops. */
export interface LibraryCondition {
  field: string;
  op: string;
  value?: string | number | boolean | (string | number)[] | null;
}

export interface LibraryQueryRequest {
  conditions: LibraryCondition[];
  match: "all" | "any";
  target: "tracks" | "albums";
  sort?: { key: string; dir: 1 | -1 } | null;
  limit?: number;
  offset?: number;
  /** Grouping key: artist|album|genre|year|rating (null = flat). With a group
   *  set, every returned item also carries `group` — its key — and the rows
   *  arrive sorted by group. */
  group?: string | null;
  /** Field names to count over the matched set. */
  facets?: string[];
}

/** One grouped bucket: `key` and how many TARGET rows (tracks or albums,
 *  whichever `target` asked for) fall in it, over the whole matched set —
 *  not just this page. */
export interface LibraryGroupCount {
  key: string;
  count: number;
}

export interface LibraryQueryResponse {
  /** The same row shape the library page renders (tracks carry the album
   *  folder's `artist`/`album`/`album_path`), plus `group` when grouping. */
  items: Record<string, any>[];
  /** Matched rows before pagination. */
  total: number;
  facets: LibraryFacet[];
  group_counts: LibraryGroupCount[];
  took_ms: number;
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
  /** The library tree. `refresh` makes the server drop the caches the payload
   *  is built from and re-walk the music folder — what the page's Refresh
   *  button asks for; an ordinary call may answer from its TTL entry. */
  library: (refresh = false) =>
    json<import("./types").Library>(`${API}/library${refresh ? "?refresh=1" : ""}`),
  /** The ONE field catalogue every filter UI renders (the Browse builder, the
   *  facet rail, the smart-playlist rule editor). Cached hard: it only changes
   *  when the server's own field list does. */
  libraryFields: () =>
    json<LibraryFields>(`${API}/library/fields`, undefined, 30000),
  /** A field's distinct values with counts, over the WHOLE library (not the
   *  current result — the rows a query matched come back as `facets` in the
   *  query's own reply). `q` searches the values server-side (what the
   *  tag-value autocomplete needs), `limit` caps what comes back while
   *  `total_values` stays honest, and `target` decides whether the counts are
   *  of tracks or of albums. */
  libraryFacets: (field: string, limit = 50, q = "", target: "tracks" | "albums" = "tracks") =>
    json<LibraryFacet>(
      `${API}/library/facets?field=${encodeURIComponent(field)}&limit=${limit}&target=${target}${q ? `&q=${encodeURIComponent(q)}` : ""}`,
      undefined,
      30000
    ),
  /** Run a filter spec. `limit: 0` is honoured and is how the builder's live
   *  count asks "how many match?" without transferring a single row. */
  libraryQuery: (req: LibraryQueryRequest, timeoutMs = 60000) =>
    json<LibraryQueryResponse>(`${API}/library/query`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(req),
    }, timeoutMs),
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
  // `pending` is true while that on-demand measurement is still decoding, so
  // a null `gain` is temporary and asking again is worth it; `album` is true
  // only when the number really is the album gain (false in album mode =
  // this album has none, per-track values were used).
  replaygain: (path: string, mode?: "track" | "album" | "off") =>
    json<{ path: string; gain: number | null; peak: number | null; mode: string; source: string | null; analyzed: boolean; pending: boolean; album: boolean }>(
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
  /** Which edition of a release group the download policy picks, why, and the
   *  ranked alternatives — the same policy the auto-import and the watch run.
   *  `prefer` forces one release (the user's override); the server re-ranks
   *  with it and says so in its reasons, so the UI explains nothing itself.
   *  primaryType/secondaryType are the release-group kind the caller is after
   *  (see mlo/naming); empty means "whatever the group is". */
  releaseChoice: (opts: {
    releaseGroupMbid: string;
    prefer?: string;
    primaryType?: string;
    secondaryType?: string;
  }) => {
    const p = new URLSearchParams({ release_group_mbid: opts.releaseGroupMbid });
    if (opts.prefer) p.set("prefer", opts.prefer);
    if (opts.primaryType) p.set("primary_type", opts.primaryType);
    if (opts.secondaryType) p.set("secondary_type", opts.secondaryType);
    return json<MBReleaseChoicePayload>(`${API}/mb/release-choice?${p}`, undefined, 60000);
  },
  // Generic MusicBrainz browser (in-app entity pages). Searches page 100
  // rows at a time — the reply's `next` is the offset of the following page
  // (null at the end) and `query` is the Lucene query MusicBrainz answered.
  // primaryType/secondaryType map onto MusicBrainz's own release-type
  // qualifiers (Album/EP/Single + Soundtrack/Live/Compilation/...); artist,
  // year, label and catno are further constraints, ANDed into that same
  // query, so a narrow search is answered by the index and not by throwing
  // rows away afterwards.
  mbSearch: (opts: {
    type: string; q: string; limit?: number;
    mode?: "free" | "catno" | "barcode"; offset?: number;
    primaryType?: string; secondaryType?: string;
    artist?: string; year?: string; label?: string; catno?: string;
  }) => {
    const p = new URLSearchParams({
      type: opts.type, q: opts.q, limit: String(opts.limit ?? 100),
      offset: String(opts.offset ?? 0), mode: opts.mode ?? "free",
    });
    for (const [key, value] of [["primary_type", opts.primaryType],
                                ["secondary_type", opts.secondaryType],
                                ["artist", opts.artist], ["year", opts.year],
                                ["label", opts.label], ["catno", opts.catno]] as const) {
      if (value) p.set(key, value);
    }
    return json<MBSearchRows>(`${API}/mb/search?${p}`);
  },
  /** The search box's own help: MusicBrainz's index fields per entity kind
   *  (name, kind, example, whether it takes quotes, what it means) and the
   *  Lucene syntax they combine with — the server's own catalogue, so the
   *  completion can never offer a field the index would not answer. */
  mbSearchFields: () =>
    json<MBSearchFieldHelp>(`${API}/mb/search/fields`, undefined, 30000),
  mbArtist: (id: string, offset = 0, limit = 300, primaryType = "", secondaryType = "") =>
    json<MBArtistBrowse>(
      `${API}/mb/artist/${id}?offset=${offset}&limit=${limit}` +
      `&primary_type=${encodeURIComponent(primaryType)}&secondary_type=${encodeURIComponent(secondaryType)}`
    ),
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
  /** Store the album's MB/RYM links. `rymArtistLink` is the artist page the
   *  wizard already confirmed as an artist (server-side kind check) — it is
   *  stamped per track as RATEYOURMUSIC_ARTIST in the same container write as
   *  the album tags, so the Links step costs one pass over the album. */
  importCommit: (targetDir: string, mbLink?: string, rymLink?: string, staged = false, rymArtistLink?: string) =>
    json<{ ok: boolean; changed: number }>(`${API}/import/commit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target_dir: targetDir, mb_link: mbLink || null, rym_link: rymLink || null, rym_artist_link: rymArtistLink || null, staged }),
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

  /** The report the LAST layout scan stored (script 20, or the panel's Scan) —
   *  walk-free, which is why the Library page can afford to ask on load. */
  libraryLayoutReport: () => json<LayoutSnapshot>(`${API}/library/layout/report`),

  /** Scan the library AND settle what the folder itself proves (script 20's
   *  apply phase): wrong-case names, audio outside any album folder, and what
   *  is excess — a stray file, an album folder with no audio, a folder inside
   *  an album that holds no audio, a foreign root folder holding no audio, an
   *  album-less artist folder, the old layout's leftovers — all to the Trash,
   *  never deleted. A foreign folder that HOLDS AUDIO and a hidden folder are
   *  reported, not moved, and every removal's reason is re-derived server-side.
   *  Returns the rows that are left plus `fixes`, and stores the report, so the
   *  panel and the Library page's warning stay the same answer. Long-running:
   *  it walks and reads one file's tags per album. */
  libraryLayoutApply: () =>
    json<LayoutReport>(`${API}/library/layout/apply`, { method: "POST" }, 1800000),

  /** Move an album-less artist folder into the app's Trash. The server
   *  re-derives the finding, so a folder that gained an album since the scan
   *  is refused instead of moved. */
  libraryLayoutRemoveEmptyArtist: (path: string) =>
    json<{ ok: boolean; trash: string }>(`${API}/library/layout/remove-empty-artist`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    }),

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
        /** The reviewed pinned release this app installs on a FIRST install.
         *  An installed copy takes the NEWEST release upstream has instead
         *  (`upstream_version`) — mlo.fetchdeps._install_one. */
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
        /** What this row's ACTION column offers: `install`/`update` download
         *  into .dependencies, `upgrade` copies `upgrade_command` (the OS
         *  package manager owns the tool), `none` is nothing to do here. */
        action?: "install" | "update" | "upgrade" | "none";
        /** The exact command that upgrades a `system` row on this host. Shown
         *  and copied, never run — the app drives no package manager. */
        upgrade_command?: string | null;
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

  /** Album covers, RANKED by the one cover policy (mlo/cover_choice).
   *
   *  The query object IS the request: `web/src/lib/coverSearch.ts` builds it
   *  (and its path), so the finder's automatic search, its Search button and
   *  this URL can never disagree about what was asked. `releaseMbid` /
   *  `releaseGroupMbid` are the identities the Cover Art Archive is asked
   *  about: the release's own front cover by the release id (which is what the
   *  policy prefers above every other candidate) and its release group's
   *  stand-in by the group id. */
  coverSearch: (q: CoverQuery) =>
    json<CoverSearch>(`${API}${coverSearchPath(q)}`, undefined, 90000),
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
    staged = false
  ) =>
    json<CoverWriteResult>(
      `${API}/cover/fromurl?album=${encodeURIComponent(albumPath)}&url=${encodeURIComponent(url)}${coverQuery(track, tracks)}` +
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
  /** The listen port's own check (server/api_soulseek.py): the listener here,
   *  what the router holds for the port, the addresses both depend on, a
   *  connection from this machine to the public address, and slskd's login —
   *  each with what it proves. Every step is bounded on the server and nothing
   *  there takes a lock, so it may be asked while a download runs; the outside
   *  half of the answer needs a probe from outside, which this app does not ship. */
  soulseekPortCheck: () => json<SlskPortCheck>(`${API}/soulseek/port-check`, undefined, 60000),
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
  /** Start the auto-import for one release (or one browsed folder). With
   *  `soulseek_search_concurrency` releases already running it does not fail:
   *  the release TAKES ITS PLACE in the waiting queue and the reply says so —
   *  `waiting` true, `position` where in the line it is and `queue_key` the id
   *  its row is named by — with no `job`, because it starts by itself (and only
   *  then) when one of the running releases finishes. */
  soulseekAutoStart: (body: { release_mbid?: string; queries?: string[]; username?: string; target_dir?: string }) =>
    json<{ ok: boolean; job?: SlskAutoJob; waiting?: boolean; position?: number; queue_key?: string }>(
      `${API}/soulseek/auto`, {
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

  /** Ratings of ONE scope (`track` by default, so every older caller is
   *  unchanged). The `rating` argument on the write side is the half-star
   *  INTEGER 0-10 the DB and (for tracks) the file tag speak — see
   *  lib/ratings.ts, the one place the 0-5 UI value is converted to it. */
  ratings: (paths?: string[], scope: RatingsScope = "track") =>
    json<RatingsPayload>(
      `${API}/ratings?scope=${scope}${paths?.length ? `&${paths.map((p) => `paths=${encodeURIComponent(p)}`).join("&")}` : ""}`
    ),
  /** Set one entity's rating; 0 clears it (row removed, and for a track the
   *  `RATING` tag removed too). An album or artist rating names that entity's
   *  FOLDER and reports `tag: null` — a folder has no file to tag, and the
   *  store says so rather than pretending. For a track, `tag` reports the file
   *  write: `written`/`skipped` and, when the file refused it, `error` — the
   *  rating is STORED either way, so a tag failure is a warning, never a
   *  rollback. */
  setRating: (path: string, rating: number, scope: RatingsScope = "track") =>
    json<{
      ok: boolean;
      scope: RatingsScope;
      path: string;
      rating: number;
      tag: { rating100: number; written: boolean; skipped: boolean; error: string | null } | null;
    }>(
      `${API}/ratings`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, rating, scope }),
      },
      60000
    ),
  /** The same value onto many tracks at once (select-all → rate). One failed
   *  file does not fail the call: `failed` is a rating that was NOT stored,
   *  `tags_failed` a stored rating whose file could not be tagged (it still
   *  counts in `updated`). */
  bulkRating: (paths: string[], rating: number) =>
    json<{
      ok: boolean;
      updated: number;
      failed: { path: string; error: string }[];
      tags_written: number;
      tags_failed: { path: string; error: string }[];
    }>(
      `${API}/ratings/bulk`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths, rating }),
      },
      300000
    ),
  // export to device
  exportDrives: () => json<{ drives: { letter: string; root: string; type: string; free: number | null; total: number | null }[] }>(`${API}/export/drives`),
  /** Codec specs come from the server (quality presets, custom ranges and
   * the kbps hints the drive-fit estimate uses) so the page never mirrors a
   * table the backend owns. */
  exportCodecs: () => json<{ codecs: Record<string, ExportCodecSpec> }>(`${API}/export/codecs`),
  exportDefaults: () => json<ExportForm>(`${API}/export/defaults`),
  /** The folder-structure menu (keys + labels) and the %fields% / $functions a
   *  custom structure script may use — the exporter's own tables, so the
   *  dropdown cannot offer a structure a run would refuse. */
  exportStructures: () => json<ExportStructures>(`${API}/export/structures`),
  /** The file families a run can be asked to copy (keys, labels, hints) —
   *  the exporter's own table, so a checkbox the run would refuse cannot be
   *  drawn, and the sentence a refused selection comes back with names the
   *  same families. */
  exportFileFamilies: () => json<{ families: ExportFamily[] }>(`${API}/export/files`),
  /** The path a user-typed structure writes for one sample track, or the
   *  server's sentence refusing it (an unknown %field%, an empty result).
   *  Same grammar and same validation the run applies. */
  exportStructurePreview: (script: string, ext: string) =>
    json<ExportStructurePreview>(`${API}/export/structure/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ script, ext }),
    }),
  /** The equalizer profiles an export can bake in — the built-in presets and
   *  everything the user imported — and the server's own line about what
   *  applying one does. */
  exportEq: () => json<ExportEq>(`${API}/export/eq`),
  /** Import one Equalizer APO / Peace profile (its text: `Preamp:`/`Filter N:`
   *  lines, a `GraphicEQ:` band list, or a Peace `FilterCurve:` line) under a
   *  name. The server refuses a name that could name a file, a body past its
   *  cap, a name a built-in preset already has, and — with the line named — a
   *  band line it cannot read: a curve that silently loses a band is worse
   *  than a refusal. */
  exportEqImport: (name: string, text: string) =>
    json<ExportEqProfile>(`${API}/export/eq/import`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, text }),
    }),
  /** Drop one imported profile (404 when it is already gone). */
  exportEqDelete: (id: string) =>
    json<{ ok: boolean; id: string }>(`${API}/export/eq/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  /** Search AutoEq's measured headphones by name (`GET /api/eq/autoeq/search`).
   *  The server keeps the project's own index cached for a month; `refresh`
   *  re-fetches it — the button for a headphone the list does not have yet. */
  eqAutoEqSearch: (q: string, refresh = false) =>
    json<EqAutoEqSearch>(
      `${API}/eq/autoeq/search?q=${encodeURIComponent(q)}${refresh ? "&refresh=1" : ""}`),
  /** Fetch one AutoEq correction and store it as a profile, returning the
   *  stored row. `id` is a search row's own id. */
  eqAutoEqImport: (id: string, form = "parametric") =>
    json<ExportEqProfile>(`${API}/eq/autoeq/import`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, form }),
    }),
  /** The saved export configurations, newest first. Each row carries the state
   *  of the equalizer profile it names, so a config whose profile has been
   *  deleted or renamed is visible as such before it is loaded. */
  exportConfigs: () => json<{ configs: ExportSavedConfig[] }>(`${API}/export/configs`),
  /** Save the export form under a name (replacing that name's config). The
   *  server validates the form against the exporter's own tables, so a config
   *  it would refuse to run cannot be saved either. */
  exportConfigSave: (name: string, config: ExportSavedConfig["config"]) =>
    json<ExportSavedConfig>(`${API}/export/configs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, config }),
    }),
  /** One saved config: the form values to load, plus `eq_missing`/`eq_problem`
   *  when the equalizer profile it names is gone or unreadable. */
  exportConfigLoad: (id: string) =>
    json<ExportSavedConfig>(`${API}/export/configs/${encodeURIComponent(id)}`),
  exportConfigDelete: (id: string) =>
    json<{ ok: boolean; id: string }>(`${API}/export/configs/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  /** The archive a `target: "zip"` run built, as a URL an `<a download>` can
   *  be pointed at. The token rides in the query string because a download
   *  cannot send an Authorization header; on the web app (`BASE === ""`) the
   *  path stays relative and the same-site cookie does the job. */
  exportZipUrl: (url: string) => media(url.startsWith("/") ? `${BASE}${url}` : url),
  exportRun: (body: ExportForm & { paths: string[] }, timeoutMs = 1800000) =>
    json<{
      ok: boolean; total: number; exported: number; skipped: number; failed: number;
      bytes: number; sidecars: number; playlists: number; verified: number;
      pruned: number; pruned_files: string[]; warnings: string[];
      error_count: number; errors: string[]; estimated_bytes: number | null;
      /** Present only for `target: "zip"` — what to hand the browser. */
      zip: ExportZip | null;
      /** The file selection the run RESOLVED (server.exporter.copy_files):
       *  the per-run selection, else the saved `export_copy_files`, else the
       *  `sidecars` switch it replaced, else the tracks alone. `kind` below is
       *  one of these families for every row. */
      copy_files: string[];
      /** Every non-audio file the export left in the library, with the reason
       *  it did not travel (server.exporter.extra_files). `kind` is the file
       *  FAMILY (`copy_files` above). */
      excluded: { album: string; name: string; kind: string; reason: string; dir: boolean }[];
      excluded_counts: Record<string, number>;
      excluded_total: number;
      excluded_note: string;
      /** What the run did with the audio: the mode it used, the profile id it
       *  resolved, and how many files it processed / equalized. */
      replaygain_mode: string; eq_profile: string;
      /** What the run did with lyrics: the mode it used and how many `.lrc`
       *  files it wrote. */
      lyrics_mode: string; lyrics_files: number;
      processed: number; eq_applied: number;
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

  // ----------------------------------------------------------------- //
  // Watched artists — an artist the app keeps an eye on. A watch is    //
  // the artist-level counterpart of a wish: the server browses         //
  // MusicBrainz for the artist's release groups and ENQUEUES the ones  //
  // the rules allow into the wish queue, a few per cycle (see          //
  // server/api_watch.py). It never queues a discography.               //
  // ----------------------------------------------------------------- //
  /** Every watch, plus the worker's own state: when it runs next and what
   *  its last cycle did. The reads are one request each way — the release
   *  groups themselves are browsed once per artist, not once per release. */
  watches: () => json<WatchesPayload>(`${API}/watches`, undefined, 30000),
  /** Start watching an artist. 400 = unknown/invalid artist id, 409 = the
   *  artist is already watched (both carry the reason in `detail`). */
  watchAdd: (body: WatchInput) =>
    json<{ ok: boolean; watch: Watch }>(`${API}/watches`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }, 60000),
  /** Change one watch — any subset of its rules, plus `enabled` (the
   *  pause/resume the row's button uses). */
  watchUpdate: (id: number, patch: WatchPatch) =>
    json<{ ok: boolean; watch: Watch }>(`${API}/watches/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }, 60000),
  watchDelete: (id: number) => json<{ ok: boolean; id: number }>(`${API}/watches/${id}`, { method: "DELETE" }),
  /** One check, now, on the user's command: browse the artist once, queue
   *  at most `max_per_cycle` release groups, and answer what it did
   *  (`summary` is the server's own sentence about this run). */
  watchCheck: (id: number) =>
    json<WatchCheckResult>(`${API}/watches/${id}/check`, { method: "POST" }, 120000),
  /** The release groups this artist has, with the watch's own rules applied
   *  to each row — the picker behind the allow/never lists. One MusicBrainz
   *  browse per request. */
  watchCandidates: (id: number) =>
    json<WatchCandidates>(`${API}/watches/${id}/candidates`, undefined, 60000),
  /** The same list for an artist nobody watches yet — the dialog asks for it
   *  while the watch is still being created. The rule params are the ones the
   *  user has picked so far (repeatable, or comma-separated), so each row's
   *  `allowed` and `reason` describe the rules on screen, not the defaults. */
  watchCandidatesFor: (artistMbid: string, rules: {
    policy?: WatchPolicy; release_types?: string[]; include?: string[]; exclude?: string[];
  } = {}) => {
    const q = new URLSearchParams();
    q.set("artist_mbid", artistMbid);
    if (rules.policy) q.set("policy", rules.policy);
    for (const t of rules.release_types ?? []) q.append("release_types", t);
    for (const id of rules.include ?? []) q.append("include", id);
    for (const id of rules.exclude ?? []) q.append("exclude", id);
    return json<WatchCandidates>(`${API}/watches/candidates?${q}`, undefined, 60000);
  },

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

  /** The ONE download queue: queued/searching, in-progress, needs-attention,
   *  completed and failed rows for the whole pipeline (see api_queue.py). */
  queue: () => json<SlskQueuePayload>(`${API}/queue`, undefined, 30000),
  /** Cancel ONE row (`item.id`, e.g. "job:3" / "wish:12" / "pipeline:<mbid>").
   *  409 = the row is not cancelable any more (it finished, or it is only a
   *  finished download whose action is the import). */
  queueCancel: (id: string) =>
    json<{
      ok: boolean; cancelled: string; removed?: boolean;
      /** Job ids the cancel STOPPED (a wish's own acquisition). Non-empty
       *  means the download really was stopped, not just the row removed. */
      jobs?: number[];
      /** Queue tickets dropped: acquisitions that had not started yet. */
      dropped?: string[];
    }>(`${API}/queue/cancel`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    }, 60000),
  /** Retry ONE terminal row by hand — the other half of the retry policy: a
   *  "nothing found" wish or a spent attempts cap is never retried by the
   *  server on its own, so this press is the way back. A `wish:` row is
   *  re-armed and searched now; a `job:` row's release goes back into the
   *  pipeline. 409 = the row is still running, or has nothing to retry with. */
  queueRetry: (id: string) =>
    json<{ ok: boolean; retried: string }>(`${API}/queue/retry`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    }, 60000),
  /** Take FINISHED rows off the queue: one row (`item.id`), every finished one
   *  (`scope: "finished"`), the wishlist alone (`"wishes"`), or ONE SECTION of
   *  the queue (`"completed"` … — what a section header's own Clear button
   *  sends, and exactly what it counted). Nothing in the library is touched
   *  and no download is deleted; `cleared` is how many rows went, `ids`
   *  which. 409 = the row is not finished (cancel is a different action, and
   *  the message names it). */
  queueClear: (body: { id?: string; scope?: SlskQueueScope }) =>
    json<{ ok: boolean; cleared: number; ids: string[] }>(`${API}/queue/clear`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }, 60000),
  /** Cancel EXACTLY these queue rows — the queue's own selection
   *  (`item.id`: "pipeline:<key>" for a release still waiting, "job:<id>" for
   *  a running one). One press, one call, and an honest answer: `cancelled` is
   *  how many of the ids went, `ids` which, and `missed` the ones that had
   *  already started, already finished or were never there. A release that has
   *  not started never downloads a byte. */
  queueCancelIds: (ids: string[]) =>
    json<{ ok: boolean; cancelled: number; ids: string[]; missed: string[] }>(
      `${API}/soulseek/downloads/cancel`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids }),
      }, 60000),
  /** "Clear all": empty the pipeline's WAITING queue — every release queued
   *  behind the ones already running, dropped before it starts anything. A
   *  RUNNING release is untouched (that is a cancel on its own row), and no
   *  settled row, library album or slskd transfer is affected. Needs no daemon
   *  (it is this app's own queue); `cleared`/`ids` say what went. */
  queueClearWaiting: () =>
    json<{ ok: boolean; cleared: number; ids: string[] }>(
      `${API}/soulseek/downloads/clear`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scope: "queued" }),
      }, 60000),

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
  /** Transliterate / translate the lyrics these tracks already carry — script
   *  17's own runner over a selection, writing TRANSLITERATION-<lang> /
   *  TRANSLATION-<lang> tags (and the .romaji.lrc / .<lang>.lrc sidecars for the
   *  LRC formats). `ok`/`skipped` are files the pass modified / left alone,
   *  `errors` its own per-file lines, and `note` says why nothing was written
   *  when the switches are off or no AI is configured. */
  lyricsXlit: (paths: string[], force = false, staged = false) =>
    json<LyricsXlitResult>(`${API}/lyrics/xlit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths, force, staged }),
    }, 600000),
  /** Move every timestamp of ONE track's stored lyrics by `deltaMs` and save
   *  it where the lyrics live (the `.lrc` beside the track and/or its LYRICS
   *  tag — never a migration between the two). `lrc` is the text that was
   *  stored, so a caller renders the file's own copy; `targets` names what was
   *  written. Untimed lines are left as they were. */
  lyricsOffset: (path: string, deltaMs: number, staged = false) =>
    json<{ ok: boolean; lrc: string; targets: string[] }>(`${API}/lyrics/offset`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, delta_ms: Math.round(deltaMs), staged }),
    }),
  /** Give LRCLIB the lyrics these tracks carry and the database lacks — script
   *  18's per-track core over a selection, the album-level counterpart of the
   *  editor's per-track publish. Each result carries LRCLIB's own answer in
   *  `message` ("already has this track" is the database refusing a duplicate,
   *  not a failure). Nothing is written locally: publishing owns no tag. */
  lyricsPublishBatch: (paths: string[], force = false, staged = false) =>
    json<LyricsPublishBatchResult>(`${API}/lyrics/publish-batch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths, force, staged }),
    }, 600000),

  // ----------------------------------------------------------------- //
  // Import — AcoustID matching, script chain, bulk queue               //
  // ----------------------------------------------------------------- //
  /** Fingerprint an album (folder or track paths) and return the MusicBrainz
   *  release group the audio actually is. `apply` also writes the accepted
   *  match's identity tags (ACOUSTID_ID / ACOUSTID_FINGERPRINT) into the files.
   *
   *  `match` is the ROW the apply=false pass answered with (release group +
   *  `recordings`): the server writes straight from that payload, so accepting
   *  a displayed match runs no fpcalc and no lookup — the second pass used to
   *  fingerprint and look up a release it had already been shown, and one
   *  transient network failure there turned the match into zero tags. */
  importAcoustid: (
    paths: string[],
    apply = false,
    staged = false,
    match?: AcoustidAlbumMatch
  ) =>
    json<AcoustidMatch>(
      `${API}/import/acoustid`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths, apply, staged, match }),
      },
      600000
    ),
  /** Publish to AcoustID's public database the fingerprint/id pair the files
   *  already carry (POST /api/import/acoustid/submit). Outward-facing and
   *  public, so `confirm: true` is sent from the UI's second, explicitly
   *  labelled press; the ids come off the files (nothing is re-fingerprinted,
   *  nothing is written locally). A refusal — no `acoustid_user_key`, or the
   *  one it was given refused — comes back as `available: false` with the
   *  service's own `note`/`code`. */
  importAcoustidSubmit: (paths: string[], staged = false) =>
    json<AcoustidSubmitResult>(
      `${API}/import/acoustid/submit`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths, staged, confirm: true }),
      },
      600000
    ),
  /** Run the configured import script chain over already-imported albums.
   *  Each album's reply also carries its `autonomy` block (what the import
   *  could not finish, see server/imports._report_gaps) and the chain's own
   *  `note` line — which is where an import says that a family it was
   *  CONFIGURED not to fetch was skipped (`skipped_families`, the same reasons
   *  spelled out in the note), since a switched-off family is not a gap.
   *
   *  `force` is omitted, not defaulted to `{}`: a SUPPLIED dict is
   *  authoritative and complete (every flag it does not name is turned off),
   *  so `{}` meant "clear everything" — including `layout_apply`, which left
   *  the chain's layout pass a read-only report and the album it just imported
   *  unfixed. Omitting it leaves the saved switches alone, which is what the
   *  bulk queue and the Soulseek import already do (`finish_album(force=None)`);
   *  the wizard's re-run and the row menu were the two paths that disagreed
   *  with them. */
  importFinish: (paths: string[], force?: Record<string, boolean>, staged = false) =>
    json<{
      albums: {
        path: string; chain: number[]; scripts: unknown[]; errors: unknown[];
        note?: string; skipped_families?: string[];
        autonomy: ImportAutonomy;
      }[];
    }>(
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
  /** Albums an import could not finish, each with the wizard link that lands
   *  on the album at the step needing a decision (GET /api/import/prompts). */
  importPrompts: () => json<{ prompts: ImportPrompt[] }>(`${API}/import/prompts`),
  /** Stop asking about one album. The next import of it recomputes the gaps
   *  and raises the prompt again if the family is still missing. */
  dismissImportPrompt: (path: string, staged = false) =>
    json<{ ok: boolean }>(`${API}/import/prompts/dismiss`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, staged }),
    }),
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
  /** Add a MusicBrainz entity to the LIBRARY: the framework album (the folder
   *  the naming script names, with the release tracklist and the
   *  release-group cover) is created on disk, shown as pending, and the search
   *  for its audio starts immediately — the release is then looked for until
   *  it is found or the user cancels it, not just until the next quiet search.
   *  `kind: "artist"` prepares the discography in the background
   *  (`background: true`) and reports itself on the event channel — one
   *  MusicBrainz browse per release group does not fit in a request. 60 s: a
   *  release group's editions are a handful of lookups. */
  libraryAdd: (body: {
    mbid: string;
    kind?: "release" | "release_group" | "artist" | "recording" | "auto";
    mode?: "best" | "all";
    release_mbid?: string;
    /** MusicBrainz release-group types to restrict an artist / release-group
     *  add to ("album", "album + compilation"); empty = every type. */
    types?: string[];
    title?: string;
    artist?: string;
    year?: string;
    queries?: string[];
  }) =>
    json<LibraryAddResult>(`${API}/library/add`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }, 60000),
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
   *  provenance). A file that already carries a valid 0/1/2 is ECHOED, not
   *  re-asked; `force: true` is the re-rate that asks anyway — the only route
   *  that can lower a rating. The REQUEST's own default is the routine pass
   *  (`false`), which is the import's automatic fetch: nobody pressed anything
   *  there, so it must not overrule a value it did not decide. Every surface a
   *  user PRESSES sends `true` (see `checkTrackValues`, the tag actions' entry
   *  and the wizard's advisory step). */
  mbAdvisoryFetch: (body: { paths?: string[]; release_mbid?: string; staged?: boolean; force?: boolean }) =>
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

  /** The genre list of one scope — the library's own genres, the online
   *  providers' (MusicBrainz, Last.fm…), or both. 45 s: the online side walks
   *  the provider chains, and the answer is TTL-cached server-side. */
  discoverGenres: (scope: DiscoverScope = "all") =>
    json<DiscoverGenres>(`${API}/discover/genres?scope=${scope}`, undefined, 45000),
  /** One page of a genre's albums/artists/tracks from one source or all of
   *  them. `offset` pages by the previous reply's `next_offset`. */
  discoverGenre: (p: {
    genre: string;
    kind: DiscoverKind;
    /** Source id, or "all" (the server asks everything it can). */
    source?: string;
    limit?: number;
    offset?: number;
  }) => {
    const q = new URLSearchParams({ genre: p.genre, kind: p.kind });
    if (p.source) q.set("source", p.source);
    if (p.limit != null) q.set("limit", String(p.limit));
    if (p.offset != null) q.set("offset", String(p.offset));
    return json<DiscoverItems>(`${API}/discover/genre?${q}`, undefined, 60000);
  },
  /** Online recommendations for the whole library, for one genre, or — with
   *  `seedKind` — for ONE ENTITY: the artist/album/track page the shelf sits
   *  on, seeded by its MusicBrainz id when its tags carry one and by
   *  artist+name when they do not. */
  discoverRecommended: (p: {
    seed?: string;
    kind: DiscoverKind;
    limit?: number;
    seedKind?: DiscoverSeedKind;
    seedMbid?: string;
    seedName?: string;
    seedArtist?: string;
  }) => {
    const q = new URLSearchParams({ seed: p.seed || "library", kind: p.kind });
    if (p.limit != null) q.set("limit", String(p.limit));
    if (p.seedKind) q.set("seed_kind", p.seedKind);
    if (p.seedMbid) q.set("seed_mbid", p.seedMbid);
    if (p.seedName) q.set("seed_name", p.seedName);
    if (p.seedArtist) q.set("seed_artist", p.seedArtist);
    return json<DiscoverRecommended>(`${API}/discover/recommended?${q}`, undefined, 60000);
  },
  /** What the online providers RANK for one window and one kind. Rows are not
   *  merged across providers (a chart's rank is its data), so each row names
   *  its provider, its rank and its own score — and `sources` says which
   *  providers can answer THIS window at all. 90 s: a chart walks every source
   *  (RateYourMusic's scrape included) and the answer is TTL-cached. */
  discoverCharts: (p: { period: ChartPeriod; kind: DiscoverKind; source?: string; limit?: number }) => {
    const q = new URLSearchParams({ period: p.period, kind: p.kind });
    if (p.source) q.set("source", p.source);
    if (p.limit != null) q.set("limit", String(p.limit));
    return json<DiscoverCharts>(`${API}/discover/charts?${q}`, undefined, 90000);
  },

  /** The LIBRARY's own most-played rows for one window (`GET /api/top`) —
   *  this user's play history, counted server-side, never a provider's chart.
   *  An empty history answers with an empty list and the `note` that explains
   *  when a play is recorded. */
  topCharts: (p: { period: ChartPeriod; kind: DiscoverKind; limit?: number }) => {
    const q = new URLSearchParams({ period: p.period, kind: p.kind });
    if (p.limit != null) q.set("limit", String(p.limit));
    return json<TopCharts>(`${API}/top?${q}`, undefined, 60000);
  },
  /** Record ONE playback start (`POST /api/plays`) — the one seam every client
   *  reports a play to, called when a track actually starts (never on a seek
   *  or a resume; a repeat is a play). Deliberately not awaited by the player:
   *  the row is a statistic, and a slow server must not delay the audio. */
  recordPlay: (path: string) =>
    json<{ ok: boolean; path: string; album: string; started_at: number }>(
      `${API}/plays`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      },
      20000
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
  /** Download a music video from YouTube for one track (web/digital media).
   *  `ok: false` carries the server's reason in `error` (YouTube off, yt-dlp
   *  missing, nothing acceptable found) rather than a bare failure. */
  videosDownloadYoutube: (body: { path?: string; artist: string; title: string; duration?: number }) =>
    json<{ ok: boolean; file?: string; candidate?: Record<string, unknown>; error?: string }>(`${API}/videos/download-youtube`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }, 900000),
  /** The YouTube cookie jar: which mode is on and what the file holds. */
  youtubeCookies: () => json<YoutubeCookies>(`${API}/youtube/cookies`),
  /** Save a pasted or dropped cookies.txt. The server validates it IS a
   *  Netscape cookie file first, so junk comes back as a 400 with the reason
   *  instead of replacing a jar that worked. */
  youtubeCookiesSave: (text: string) =>
    json<YoutubeCookies>(`${API}/youtube/cookies`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    }, 60000),
  /** Remove the jar (the mode setting is untouched). */
  youtubeCookiesDelete: () =>
    json<YoutubeCookies>(`${API}/youtube/cookies`, { method: "DELETE" }),
  /** The stored RateYourMusic credential: the cookie names it holds, in the
   *  order they are sent, no values. */
  rymCookies: () => json<RymCookies>(`${API}/rym/cookies`),
  /** Save the rateyourmusic.com cookies of a pasted or dropped cookies.txt
   *  into `rym_cookie` (the credential the RYM scraper already reads). The
   *  server validates it IS a Netscape cookie file first, so junk comes back
   *  as a 400 instead of replacing a credential that worked — and a
   *  well-formed export holding no RYM cookie stores nothing at all
   *  (`stored: 0`) rather than clearing it. */
  rymCookiesSave: (text: string) =>
    json<RymCookiesSaveReply>(`${API}/rym/cookies`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    }, 60000),
  /** Write TITLE/TRACKNUMBER/DISCNUMBER onto video files from the match-assist
   *  panel (one assignment per video file). */
  videosMatch: (albumPath: string, assignments: { path: string; title: string; tracknumber?: number; discnumber?: number }[]) =>
    json<{ updated: number } & ContainerSwap>(`${API}/videos/match`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ album_path: albumPath, assignments }),
    }, 600000).then(noteContainerSwap),
};

/* ---------------------------------------------------------------------- *
 * Watched artists (server/api_watch.py) — the types the `watch*` methods  *
 * above answer with.                                                     *
 * ---------------------------------------------------------------------- */

/** How far back a watch looks. `new_only` (the default) takes release groups
 *  it has not seen before, so a fresh watch can never pull in a catalogue it
 *  has been sitting on for years; `backfill` deliberately walks the existing
 *  discography, still `max_per_cycle` at a time. */
export type WatchPolicy = "new_only" | "backfill";

/** One release group a watch acted on — the row behind the "queued" and
 *  "imported" lists. `status` is what happened to it: handed to the wish
 *  queue, announced only (auto-add off), imported, or failed. */
export interface WatchItem {
  release_group_mbid: string;
  title: string;
  year: string;
  release_id: string;
  /** The wish this became; null when the watch only notified. */
  wish_id: number | null;
  status: "queued" | "notified" | "imported" | "failed";
  at: number;
  note: string;
}

/** One watched artist. The rules are the whole point: `release_types` says
 *  which kinds of release group count, `include` narrows it to a chosen
 *  allow-list, `exclude` blocks individual release groups, and
 *  `max_per_cycle` caps how many the watch may queue per check. */
export interface Watch {
  id: number;
  artist_mbid: string;
  artist: string;
  added_at: number;
  /** false = paused: the worker skips it and `next_check_at` reads 0. */
  enabled: boolean;
  policy: WatchPolicy;
  /** MusicBrainz's own release-group type names, lowercased (album, ep,
   *  single, broadcast, other, compilation, soundtrack, spokenword,
   *  interview, audiobook, live, remix, dj-mix, mixtape/street, demo,
   *  field recording). A release group matches when its PRIMARY type is in
   *  here OR any of its secondary types is. */
  release_types: string[];
  /** Allow-list of release-group MBIDs; EMPTY means "no restriction" — any
   *  new release of the allowed types. Non-empty means only these. */
  include: string[];
  /** Release groups that must never be fetched. */
  exclude: string[];
  /** The per-check ceiling (1-10): how many release groups ONE cycle may
   *  queue. This is what keeps a watch from ever dumping a discography. */
  max_per_cycle: number;
  /** true = queue straight into the library; false = notify and let the user
   *  decide from the wish list. */
  auto_add: boolean;
  /** 0 = never checked yet. */
  last_checked_at: number;
  /** When the worker checks next; 0 while the watch is paused. */
  next_check_at: number;
  /** The newest release group the watch has seen, so the next `new_only`
   *  check knows what is new. */
  last_seen_release_group: string;
  checked_count: number;
  queued_count: number;
  notified_count: number;
  imported_count: number;
  /** The server's own sentence about the last check. */
  last_result: string;
  last_error: string;
  note: string;
  /** What this watch queued / imported, newest first. */
  queued: WatchItem[];
  imported: WatchItem[];
}

/** `POST /api/watches` — a new watch, or a prefill for one. Everything but
 *  the artist id is optional; the server fills the rest with its defaults
 *  (policy `new_only`, types album+ep, one release per cycle, auto-add on). */
export interface WatchInput {
  artist_mbid: string;
  artist?: string;
  policy?: WatchPolicy;
  release_types?: string[];
  include?: string[];
  exclude?: string[];
  max_per_cycle?: number;
  auto_add?: boolean;
  note?: string;
}

/** `PATCH /api/watches/{id}` — any subset of the rules, plus the pause. */
export interface WatchPatch {
  artist?: string;
  enabled?: boolean;
  policy?: WatchPolicy;
  release_types?: string[];
  include?: string[];
  exclude?: string[];
  max_per_cycle?: number;
  auto_add?: boolean;
  note?: string;
}

/** `GET /api/watches` — the watches, plus the worker that walks them. */
export interface WatchesPayload {
  watches: Watch[];
  worker: {
    running: boolean;
    /** Cycles completed since the server started. */
    cycles: number;
    last_cycle: number;
    next_run: number;
    last_result: string;
    /** The artists the running cycle is working on. */
    current: string[];
  };
}

/** `POST /api/watches/{id}/check` — what one on-demand check did. */
export interface WatchCheckResult {
  ok: boolean;
  /** Release groups the browse returned for the artist. */
  checked: number;
  queued: WatchItem[];
  notified: WatchItem[];
  /** The server's own sentence: what it queued, or why it queued nothing. */
  summary: string;
}

/** One row of the release-group picker. `allowed` is the server's verdict
 *  once the watch's rules are applied, and `reason` says why in words —
 *  including for rows it would NOT fetch ("single", "blocked", "in the
 *  library", "already queued"). */
export interface WatchCandidate {
  release_group_mbid: string;
  title: string;
  /** First-release year, "" when MusicBrainz dates it not at all. */
  year: string;
  primary_type: string;
  secondary_types: string[];
  first_release_date: string;
  in_library: boolean;
  queued: boolean;
  allowed: boolean;
  /** True when the watch has not seen this release group yet. */
  is_new: boolean;
  reason: string;
}

/** `GET /api/watches/{id}/candidates` (and the by-artist form for a watch
 *  that does not exist yet): the artist's release groups, filtered so
 *  `allowed` reflects the current rules. `watch_id` is null for the
 *  by-artist form. */
export interface WatchCandidates {
  artist_mbid: string;
  artist: string;
  watch_id: number | null;
  items: WatchCandidate[];
  sources_asked: string[];
  /** What the rows were filtered by — the policy sentence, the type names
   *  asked for, and how many ids the include / exclude lists hold. */
  notes: { policy: string; types: string[]; include: number; exclude: number };
}

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
