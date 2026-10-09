import type {
  AcoustidAlbumMatch,
  AcoustidMatch,
  AcoustidSubmitResult,
  Album,
  Artist,
  CoverInfo,
  CoverSearch,
  CoverSourceCatalog,
  CoverWriteResult,
  CoverChoicePolicy,
  CoverResult,
  GenreCascade,
  GradeWarning,
  ImportAutonomy,
  ImportBulkJob,
  ImportBulkResult,
  ImportPrompt,
  ImportScriptsPreview,
  ImportSession,
  ImportSettleResult,
  ImportSourceResult,
  LayoutReport,
  LayoutSnapshot,
  Library,
  LibraryAddResult,
  LyricsAutoResult,
  LyricsHit,
  LyricsProviders,
  LyricsXlitResult,
  LogReportPayload,
  MatchSuggestion,
  MBArtistBrowse,
  MBRecordingBrowse,
  MBRelease,
  MBReleaseChoicePayload,
  MBSearchFieldHelp,
  MBSearchRows,
  ScriptRunResult,
  ScriptMenu,
  SourceHealth,
  SourceKind,
  SourcesHealth,
  UnpackedTree,
} from "./types";
import { toast } from "./store";
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
 *  The remembered cover version is bumped so the album's art repaints in
 *  preference to the version it replaced. */
const noteCoverWrite =
  (albumPath: string) =>
  (res: CoverWriteResult): CoverWriteResult => {
    const file = res.path.split("/").pop() ?? null;
    rememberCoverVersion(albumPath, file, res.token);
    return res;
  };

/** One place every request goes through: the deadline, the session token and
 *  the 401 that means "sign in again".
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
  let r: Response;
  try {
    r = await fetch(url, { credentials: "include", ...init, headers, signal: ctrl.signal });
  } catch (e) {
    // No answer at all: the server is down, the network is gone, or the call
    // ran out of time. (An unreachable server's 4xx/5xx is an answer, and is
    // handled below exactly as before — a rejected request must not be turned
    // into a successful one.)
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
  return (await r.json()) as T;
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
 *  nothing was replaced), how many cookie lines in the file were for other
 *  sites, and whether the stored credential now carries RYM's `session`
 *  cookie. */
export interface RymCookiesSaveReply extends RymCookies {
  stored: number;
  session: boolean;
  filtered: number;
}

/** One cookie of a credential, as `GET /api/cookies/{source}` states it
 *  (`server/api_cookies.py`) — the row a user reads and writes against. There
 *  is NO value: a session cookie is a live credential. `expires_at` and
 *  `expired` are the server's own reading of the expiry column, so the browser
 *  never has to do date arithmetic on a timestamp. */
export interface CookieEntry {
  /** the host this cookie is sent to, lower-cased, no leading dot. */
  domain: string;
  path: string;
  name: string;
  /** the Netscape expiry column as written; "" is a session cookie. */
  expiry: string;
  /** that expiry as an ISO 8601 UTC date, or "" when none is stated. */
  expires_at: string;
  expired: boolean;
  /** the user's own note for this cookie ("" when none). */
  comment: string;
}

/** The per-cookie view of one cookie login: what it stores, and the hosts its
 *  import keeps cookies for (a browser export is the whole profile). */
export interface CookieList {
  source: string;
  hosts: string[];
  present: boolean;
  cookies: CookieEntry[];
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

/** The `identity` block `/api/credits` carries beside the rows: everything the
 *  panel's header may print — what the track or release IS, read from the
 *  files' own tags plus the MusicBrainz ids the route resolved. Always present
 *  in a fresh reply, with "" wherever the files state nothing (an album leaves
 *  the recording ids blank: they would name one of its tracks). `path` is the
 *  file or folder the panel was opened on — the raw path the header prints
 *  LAST and dimmed, never as a heading. */
export interface CreditIdentity {
  title: string;
  artist: string;
  album: string;
  album_artist: string;
  catalog_number: string;
  label: string;
  barcode: string;
  date: string;
  original_date: string;
  country: string;
  release_type: string;
  media: string;
  track_mbid: string;
  release_mbid: string;
  release_group_mbid: string;
  artist_mbid: string;
  recording_mbid: string;
  path: string;
}

/** `/api/credits` reply. `source` must be shown next to the rows: a tag
 *  fallback is not MusicBrainz data and must never read as if it were. */
export interface Credits {
  artist: string;
  album: string;
  rows: CreditRow[];
  source: "musicbrainz" | "tags";
  /** What the panel's header draws — see CreditIdentity. */
  identity: CreditIdentity;
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
  /** "off" | "tags" (write ReplayGain tags, a compatible player applies them) |
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
  /** The switch `copy_files` replaced (server.exporter.LEGACY_SIDECAR_FAMILIES
   *  when it is on). The form no longer writes it — the file selection below
   *  is what the run reads — but a saved default or a saved config from before
   *  the selection existed still carries it, and the run still honours it. */
  sidecars: boolean;
  /** WHICH files the run writes: the family keys of server.exporter.
   *  FILE_FAMILIES — "audio" (the tracks themselves), "cover", "lyrics",
   *  "cue", "log", "accurip", "checksum", "text", "playlist",
   *  "other". An EMPTY list is refused by the server with a sentence (a run
   *  that copies nothing would write an empty folder), so the form must leave
   *  one ticked. */
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
 *  (`type`/`fc`/`gain`/`q`/`on`) and the one the editor's WebAudio response
 *  maths builds (lib/eqNodes). `type` is an Equalizer APO type (PK, LS, HS, LSC,
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
 *  `answers` carries all of them. `albums`/`album_updated` are always empty/0
 *  (the app no longer writes an album-level advisory tag — see
 *  mlo.audio.TAG_MAP), kept so an older client reads the same shape. `gated`
 *  is how many files the ADVISORY write gate refused, and `skipped` says why
 *  nothing was written when it refused every file — a fetch that wrote nothing
 *  is never a success. */
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

/* ── the library query engine ──────────────────────────────────────────────
 *
 *  ONE engine answers every way of browsing the library (see mlo/query.py):
 *  the Browse page's ad-hoc builder, the facet rail and the live match count
 *  all send the SAME filter spec (`{conditions:[{field,op,value}], match}`).
 *
 *  `GET /api/library/fields` is the ONE field catalogue: the builder and the
 *  facet rail render from it, so a field added on the server appears in every
 *  surface at once. */

/** One operator a field offers. `op` is the spec's vocabulary — the id the
 *  engine evaluates; `label` is what the picker shows. Read them from the
 *  catalogue, never hardcode a list: the engine owns the set. */
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

/** The cover candidate set the import chain STAGED for an album instead of
 *  applying (the `cover_review` switch on, see mlo.imports.stage_cover_candidates).
 *  It is what the album page offers as "Choose a cover" for a cover-less album:
 *  the candidates ranked best-first by the one cover policy (mlo/cover_choice),
 *  each row carrying the reasons that put it there. */
export interface StagedCovers {
  /** Who answered the staged fetch — the badge the picker shows. */
  provider: string | null;
  /** The ranked candidates, best first. */
  results: CoverResult[];
  /** The policy's own pick and its reasons — the picker's default before the
   *  user overrides it. */
  chosen?: CoverResult | null;
  /** What every source did, including any that was skipped and why. */
  notes?: string[];
  /** The policy the ranking was made under (the minimum, the source order and
   *  the rules). */
  policy?: CoverChoicePolicy;
}

/** `/api/metadata/candidates` — only the cover branch the import staged; the
 *  artist-image and description branches this reply used to carry are gone. */
export interface MetadataCandidates {
  staged?: { covers?: StagedCovers | null };
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
    json<Library>(`${API}/library${refresh ? "?refresh=1" : ""}`),
  /** The ONE field catalogue every filter UI renders (the Browse builder, the
   *  facet rail). Cached hard: it only changes
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
    json<Album>(`${API}/album?path=${encodeURIComponent(path)}${stagedQ(staged)}`),
  artist: (path: string) => json<Artist>(`${API}/artist?path=${encodeURIComponent(path)}`),
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

  // Read-only tag view (tag writing was removed; grading scripts own writes).
  // `staged` reads a track of an album the import wizard is editing before it
  // is in the library.
  tags: (path: string, staged = false) =>
    json<any>(`${API}/tags?path=${encodeURIComponent(path)}${stagedQ(staged)}`),
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

  /** The registry, as the details menu needs it: every script, its slot in the
   *  stack's order, its force flag, its feature switch and the entity kinds a
   *  run of it makes sense from (server/script_menu.py). Read-only — a menu
   *  runs its ids through the same /api/run above. */
  scriptMenu: () => json<ScriptMenu>(`${API}/script-menu`),

  // integrations
  mbRelease: (id: string) => json<MBRelease>(`${API}/mb/release?mbid=${encodeURIComponent(id)}`),
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
    json<GenreCascade>(
      `${API}/mb/release-genres?mbid=${encodeURIComponent(id)}${limit ? `&limit=${limit}` : ""}`
    ),
  mbSearchReleases: (q: string, mode: "release" | "track" | "catno" | "barcode" = "release") =>
    json<any[]>(`${API}/mb/search/releases?q=${encodeURIComponent(q)}&mode=${mode}`),
  mbSearchArtists: (q: string) => json<any[]>(`${API}/mb/search/artists?q=${encodeURIComponent(q)}`),
  mbReleaseGroup: (id: string, offset = 0, limit = 300) =>
    json<any>(`${API}/mb/release-group/${id}?offset=${offset}&limit=${limit}`),
  /** Which edition of a release group the download policy picks, why, and the
   *  ranked alternatives — the same policy an "Add to library" runs.
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
    json<{ release: MBRelease; suggestions: MatchSuggestion[] }>(
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

  /** The cover URL. `token` (the `CoverWriteResult.token` of the write that
   *  just happened) rides along as `&v=`: a cover is replaced IN PLACE, so
   *  without it a replaced cover.jpg is the same URL — and neither the
   *  rendered `<img>` nor a browser cache would ever ask for the new bytes.
   *  Omitted, it falls back to the version this session wrote for that
   *  album+file (see lib/invalidate), which is what keeps the grids and the
   *  album's hero on the freshly written image without any of them knowing
   *  about the write.
   *
   *  `staged` marks the import wizard's album — the folder the library does
   *  not list yet: the server serves its cover only to a request that carries
   *  the same opt-in every other wizard call passes, so a preview that leaves
   *  it out is refused (400) and stays empty however well the cover was
   *  written.
   *
   *  An explicit `token` — including `null` — wins over the remembered
   *  version.
   *
   *  `w` asks the server for the cover SHRUNK to that width (bucketed
   *  server-side), which is what a surface that draws a row's art must ask
   *  for: the master is 1200–3000 px, so fetching it puts megabytes and a
   *  full-size decode behind a 40 px cell. The sized answer is cacheable for
   *  minutes (a replaced cover's URL carries a new `v`, so the app's own
   *  writes still refetch immediately), and two surfaces that ask for the
   *  same width share ONE request. Omitted, the master is served — anything
   *  that needs full resolution keeps it. */
  coverUrl: (
    albumPath: string,
    coverFile?: string | null,
    opts?: { token?: string | null; staged?: boolean; w?: number }
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
        (opts?.staged ? "&staged=true" : "") +
        (opts?.w ? `&w=${Math.round(opts.w)}` : "")
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

  /** Upload a set of files as ONE album.
   *
   *  `album_name`/`merged` are what actually happened to a SINGLE-song
   *  upload: the server places one track on the album it belongs to (the
   *  release the library already holds, or the album the file's own tags
   *  name) instead of making an album out of the track, so the name that
   *  comes back can differ from `targetDir`.
   *
   *  `staged` is the other way in, and it is the same call: absolute paths the
   *  server already holds — the files `importUnpack` unpacked out of a user's
   *  archive — are moved into the album instead of being uploaded a second
   *  time. A group may carry both (a drop of a folder AND an archive), and the
   *  album that comes out is the same one either way. */
  importUpload: (targetDir: string, files: { file: File; relPath: string }[], staged?: string[]) => {
    const fd = new FormData();
    for (const { file, relPath } of files) fd.append("files", file, relPath);
    if (staged?.length) fd.append("staged", JSON.stringify(staged));
    return json<{ ok: boolean; saved: string[]; album_path: string; album_name?: string; merged?: boolean }>(
      `${API}/import/upload?target_dir=${encodeURIComponent(targetDir)}`,
      { method: "POST", body: fd },
      1800000
    );
  },
  /** Unpack ONE archive on the server and list what came out — the wizard
   *  shows that list BEFORE anything is committed, so an archive is a
   *  selection like a folder is.
   *
   *  No parallel import path: this only stages the tree (under <music>/.mlo).
   *  The commit is the ordinary `importUpload`, which takes the files it kept
   *  as `staged`. `audio` is the server's own count over the extracted tree —
   *  an archive with none is a fact (`audio: 0`), not an empty album.
   *
   *  `path` is for a shell that hands over OS paths instead of bytes (the
   *  desktop app): the archive is read from the filesystem the server shares.
   *  The same ceiling as an upload applies — this request carries the app's
   *  30-minute allowance, and a bigger/slower archive fails with "no answer
   *  within 1800s" rather than hanging. */
  importUnpack: (archive: File | { path: string }) => {
    const to = 1800000;
    if (archive instanceof File) {
      const fd = new FormData();
      fd.append("file", archive, archive.name);
      return json<UnpackedTree>(`${API}/import/unpack`, { method: "POST", body: fd }, to);
    }
    return json<UnpackedTree>(
      `${API}/import/unpack?path=${encodeURIComponent(archive.path)}`,
      { method: "POST" },
      to
    );
  },
  /** Drop unpacked trees the wizard is done with. Only a folder the app made
   *  under its own state root is ever removed (the server answers the rest as
   *  `skipped`), and one left behind by a closed wizard is swept after a day. */
  importUnpackDiscard: (dirs: string[]) => {
    const fd = new FormData();
    for (const d of dirs) fd.append("dirs", d);
    return json<{ ok: boolean; removed: string[]; skipped: string[] }>(
      `${API}/import/unpack/discard`,
      { method: "POST", body: fd }
    );
  },
  importScan: (path: string) =>
    json<{ root: string; file?: boolean; files: { relPath: string; size: number }[] }>(
      `${API}/import/scan?path=${encodeURIComponent(path)}`,
      { method: "POST" }
    ),
  importIngest: (source: string, target: string) =>
    json<{ ok: boolean; path: string; album_name?: string; merged?: boolean }>(
      `${API}/import/ingest?source=${encodeURIComponent(source)}&target=${encodeURIComponent(target)}`,
      { method: "POST" }
    ),
  /** Store the album's MusicBrainz release link. */
  importCommit: (targetDir: string, mbLink?: string, staged = false) =>
    json<{ ok: boolean; changed: number }>(`${API}/import/commit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target_dir: targetDir, mb_link: mbLink || null, staged }),
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

  /** Read-only scan of the whole music folder: misplaced files, unexpected
   *  folders, and anything that breaks the Artists/<Artist>/<Album> shape. */
  libraryLayout: () => json<LayoutReport>(`${API}/library/layout`),

  /** The report the LAST layout scan stored (script 20, or the panel's Scan) —
   *  walk-free, which is why the Library page can afford to ask on load. */
  libraryLayoutReport: () => json<LayoutSnapshot>(`${API}/library/layout/report`),

  /** Whether the library passes its grading checks, and — when it does not —
   *  what fails, with a link target per row. The SAME object `/api/home`
   *  carries as `grade_warning`: the Library page fetches no Home payload, so
   *  this is how both pages read one answer rather than counting the library
   *  twice. `ok` and `grade_pct` come from one sum, so the strip can never
   *  contradict the header's percentage. */
  gradesSummary: () => json<GradeWarning>(`${API}/grades/summary`),

  /** One rip log in full: Logchecker's own report, this app's checksum
   *  verdict, and the log's text — the answer to "why did this score 60".
   *  `path` is the `.log` itself or the album folder holding it; an album with
   *  several logs is asked disc by disc (`disc`). Read-only, and `available`
   *  false means no scorer is installed — a missing report, never a zero. */
  logReport: (path: string, disc?: number) =>
    json<LogReportPayload>(
      `${API}/log/report?path=${encodeURIComponent(path)}${disc ? `&disc=${disc}` : ""}`
    ),

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
  /** The cover candidates the import chain staged for an album — the album
   *  page's "Choose a cover" flow. `staged` opts into the wizard's album (a
   *  folder the library does not list yet). */
  metadataCandidates: (artist: string, albumPath?: string, staged = false) => {
    const p = new URLSearchParams({ artist });
    if (albumPath) p.set("album_path", albumPath);
    if (staged) p.set("staged", "1");
    return json<MetadataCandidates>(`${API}/metadata/candidates?${p}`, undefined, 90000);
  },
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

  trackExportUrl: (path: string, codec: string, bitrate: number, level = 5) =>
    media(`${API}/track/export?path=${encodeURIComponent(path)}&codec=${encodeURIComponent(codec)}&bitrate=${bitrate}&level=${level}`),

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
      bytes: number; sidecars: number; verified: number;
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
  /** Ask the RUNNING export to stop. It stops at the next FILE BOUNDARY and
   *  KEEPS everything it has already written — nothing is deleted — so the
   *  files that landed before the request stand. `cancelled` is false when no
   *  run was in flight (it had already finished, or was never started). */
  exportCancel: () =>
    json<{ ok: boolean; cancelled: boolean }>(`${API}/export/cancel`, { method: "POST" }),
  /** Write the Export page's form back into config.json (its saved defaults).
   * Every field maps onto the `export_<field>` config key the server reads. */
  exportSaveDefaults: (form: ExportForm) =>
    json<Record<string, unknown>>(`${API}/config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.fromEntries(
        Object.entries(form).map(([k, v]) => [`export_${k}`, v]))),
    }),

  // ----------------------------------------------------------------- //
  // Lyrics — the synced provider chain (LRCLIB → NetEase → Kugou →    //
  // QQ Music → Kuwo); see Settings → Lyrics.                          //
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
  /** Publish to AcoustID's public database what the files state: their
   *  fingerprint and the MusicBrainz recording id they name
   *  (POST /api/import/acoustid/submit). Outward-facing and public, so
   *  `confirm: true` is sent from the UI's second, explicitly labelled press.
   *  The fingerprint comes off the file (`ACOUSTID_FINGERPRINT`) or is taken
   *  locally when the file carries none, and nothing is written locally. A pair
   *  AcoustID already links — or that this app already submitted — is reported
   *  as `already_known` and NOT re-sent; a file that names no recording is a
   *  named skip; `results` is the per-track report. A refusal — no
   *  `acoustid_user_key`, or the one it was given refused — comes back as
   *  `available: false` with the service's own `note`/`code`, and nothing is
   *  read or sent. */
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
   *  bulk queue already does (`finish_album(force=None)`);
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
  importBulk: (items: { path: string; move?: boolean; release?: Record<string, unknown>; mbid?: string }[]) =>
    json<ImportBulkResult>(
      `${API}/import/bulk`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items }),
      },
      60000
    ),
  importBulkStatus: (job?: string) =>
    json<ImportBulkJob>(
      `${API}/import/bulk/status${job ? `?job=${encodeURIComponent(job)}` : ""}`),
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
  /** Manual imports the user left unfinished — the wizard's persisted
   *  bookmarks, newest first. The tray turns each into "Continue import". */
  importSessions: () => json<{ sessions: ImportSession[] }>(`${API}/import/sessions`),
  /** Bookmark (or refresh) one album's unfinished manual import. */
  importSessionSave: (album: string, step: number, album_name = "", staged = false) =>
    json<{ session: ImportSession | null }>(`${API}/import/sessions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ album, step, album_name, staged }),
    }),
  /** Forget one album's unfinished import, or all of them (no argument). */
  importSessionDismiss: (album?: string) =>
    json<{ ok: boolean }>(`${API}/import/sessions/dismiss`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ album: album ?? null }),
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
  /** Settle a digital release's own three answers before the ticked scripts
   *  run — the same call `server.imports.settle_digital_import` makes on every
   *  other import path, so an album imported by hand ends in the state an
   *  unattended import leaves:
   *
   *   * `source` — SOURCE written when the release (its own store URLs) or the
   *     acquisition's provider states one; `state: "asked"` (with the config's
   *     `default`) when nothing may honestly be written, which is what the
   *     Match step's own control answers;
   *   * `lyrics` — the untimed lyrics this install refuses (`state:
   *     "cleaned"` with the count, or `"no-fetch"` when script 13 is not in
   *     `scripts` and nothing could replace them);
   *   * `metadata` — the album description (and artist image/description)
   *     fetched through the import's own metadata step, the same machinery the
   *     album page's fetch uses.
   *
   *  `source` is the value the user just answered (written here), `scripts`
   *  the chain the Finish step is about to run. */
  importSettle: (
    path: string,
    opts: { scripts?: number[]; source?: string; metadata?: boolean; release?: Record<string, unknown>; staged?: boolean } = {}
  ) =>
    json<ImportSettleResult>(`${API}/import/settle`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, ...opts }),
    }),
  /** Report or write a Digital Media album's SOURCE (POST /api/import/source).
   *  No `value` = the value the pipeline would write (`state: "suggested"`) or
   *  `"asked"`, plus the config's own default — what the wizard pre-fills;
   *  `value` given = THE user's answer, written to every track that lacks one
   *  (fill-only, the pipeline's own rule). */
  importSource: (
    path: string,
    opts: { value?: string; release?: Record<string, unknown>; provider?: string; staged?: boolean } = {}
  ) =>
    json<ImportSourceResult>(`${API}/import/source`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, ...opts }),
    }),

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
  /** Genre browsing surface: every genre with its track count, plus the
   *  category cards that group them. */
  genresFacets: () =>
    json<{ genres: { name: string; count: number }[]; categories: { name: string; genres: string[] }[] }>(
      `${API}/genres/facets`
    ),

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
  /** The cookies the RateYourMusic credential stores, with each cookie's own
   *  comment — `GET /api/cookies/rym` (server/api_cookies.py). Never carries a
   *  cookie value. */
  cookieList: (source: "rym") =>
    json<CookieList>(`${API}/cookies/${source}`),
  /** Write (or, with an empty `comment`, clear) one cookie's comment. The
   *  cookie is named by its IDENTITY — domain, path, name — so the note follows
   *  the cookie across a re-import instead of the line it sat on. Answers with
   *  the fresh list. */
  cookieComment: (
    source: "rym",
    body: { domain: string; path: string; name: string; comment: string }
  ) =>
    json<CookieList>(`${API}/cookies/${source}/comments`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
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
  "keybpm",
];
