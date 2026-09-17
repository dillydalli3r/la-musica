import type {
  AcoustidMatch,
  ArtistArtwork,
  ArtistArtworkDescription,
  ArtistArtworkImage,
  CoverInfo,
  CoverSourceCatalog,
  CoverSearch,
  CoverWriteResult,
  DiscoveryAlbumDetail,
  DiscoveryCatalog,
  DiscoveryImageRow,
  DiscoveryRow,
  DiscoverySearch,
  DownloadsPayload,
  HomeData,
  ImportBulkJob,
  ImportBulkResult,
  ImportScriptsPreview,
  LayoutReport,
  LyricsAutoResult,
  LyricsHit,
  LyricsProviders,
  ScriptRunResult,
  SourceHealth,
  SourceKind,
  SourcesHealth,
  Wish,
  WishesPayload,
} from "./types";
import { toast } from "./store";

// In the Tauri desktop shell the frontend is served from tauri://localhost,
// so relative /api paths cannot reach the Python backend — use absolute.
const IN_TAURI = !!(window as any).__TAURI_INTERNALS__;
const BASE = IN_TAURI ? "http://127.0.0.1:8000" : "";
const API = `${BASE}/api`;

/** `track=` (one file) plus the comma-separated `tracks=` list — each name
 *  URL-encoded on its own, so commas inside a name survive. */
function coverQuery(track?: string, tracks?: string[]): string {
  const list = (tracks ?? []).filter(Boolean);
  return (
    (track ? `&track=${encodeURIComponent(track)}` : "") +
    (list.length ? `&tracks=${list.map(encodeURIComponent).join(",")}` : "")
  );
}

async function json<T>(url: string, init?: RequestInit, timeoutMs = 20000): Promise<T> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let r: Response;
  try {
    r = await fetch(url, { ...init, signal: ctrl.signal });
  } finally {
    clearTimeout(timer);
  }
  if (!r.ok) {
    let detail = r.statusText;
    try {
      const j = await r.json();
      detail = j.detail || detail;
    } catch {
      /* keep statusText */
    }
    throw new Error(detail);
  }
  return r.json() as Promise<T>;
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
 *  the search stage runs — null once the query has been scored. `elapsed` and
 *  `wait` are seconds of the current query's window. */
export interface SlskSearchProgress {
  query: string;
  elapsed: number;
  wait: number;
  state: string;
  responses: number;
  files: number;
  /** Seconds left in this query's window, and the absolute deadline the
   *  server computed for it. Older servers send neither. */
  remaining?: number | null;
  deadline_s?: number | null;
}

/** One file of the running auto-import download, as slskd reports it. */
export interface SlskAutoFile {
  name?: string;
  bytes?: number;
  size?: number;
  percent?: number;
  speed?: number;
  state?: string;
  done?: boolean;
}

/** Live download progress of the auto-import job — null while nothing is in
 *  flight, and absent entirely on servers predating the payload. Every field
 *  is optional: the panel renders whatever subset the backend publishes. */
export interface SlskAutoProgress {
  phase?: string;
  username?: string;
  dir?: string;
  files_done?: number;
  files_total?: number;
  bytes?: number;
  size?: number;
  percent?: number;
  speed?: number;
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
   *  asks whether a lossy copy may be downloaded, "no_results" reports a search
   *  that came back empty and offers the wish handoff. Both are answered through
   *  soulseekAutoConfirm(). Only the fields of the variant at hand are
   *  published, so every field past `reason` is optional and the panel renders
   *  whatever subset arrives. */
  confirm: {
    reason?: "lossy_only" | "no_results";
    /** How long the search ran, in seconds (no_results). */
    waited?: number;
    /** The queries that came back empty (no_results). */
    queries?: string[];
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

export const api = {
  health: () => json<{ status: string; version: string }>(`${API}/health`),
  config: () => json<Record<string, unknown>>(`${API}/config`),
  configDefaults: () => json<Record<string, unknown>>(`${API}/config/defaults`),
  saveConfig: (cfg: Record<string, unknown>) =>
    json<Record<string, unknown>>(`${API}/config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cfg),
    }),
  library: () => json<import("./types").Library>(`${API}/library`),
  album: (path: string) => json<import("./types").Album>(`${API}/album?path=${encodeURIComponent(path)}`),
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
  mbDetect: (path: string) =>
    json<{ mbid: string | null; key?: string; track?: string }>(
      `${API}/album/mbdetect?path=${encodeURIComponent(path)}`,
      undefined,
      8000
    ),
  scanTracks: (path: string) =>
    json<{ path: string; tracks: any[] }>(
      `${API}/album/scan-tracks?path=${encodeURIComponent(path)}`,
      undefined,
      30000
    ),
  organize: (paths: string[], dryRun = false) =>
    json<{ results: any[] }>(`${API}/organize`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths, dry_run: dryRun }),
    }),

  streamUrl: (path: string) => `${API}/stream?path=${encodeURIComponent(path)}`,
  /** Library music-video stream: direct bytes by default (?transcode=1 pipes
   * MPEG-2/VC-1/etc. through ffmpeg into playable H.264/AAC MP4). */
  videoStreamUrl: (path: string, transcode = false) =>
    `${API}/videos/stream?path=${encodeURIComponent(path)}${transcode ? "&transcode=1" : ""}`,
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
    `${API}/videos/subtitle?path=${encodeURIComponent(path)}${sidecar ? `&sidecar=${encodeURIComponent(sidecar)}` : ""}${typeof n === "number" && n >= 0 ? `&n=${n}` : ""}`,
  // Read-only tag view (tag writing was removed; grading scripts own writes).
  tags: (path: string) => json<any>(`${API}/tags?path=${encodeURIComponent(path)}`),
  // ReplayGain for playback loudness matching. `mode` overrides the saved
  // replaygain_mode for one call (track/album/off); `analyzed` is true when
  // the gain had to be measured on the fly because the tags were missing.
  replaygain: (path: string, mode?: "track" | "album" | "off") =>
    json<{ path: string; gain: number | null; peak: number | null; mode: string; source: string | null; analyzed: boolean }>(
      `${API}/replaygain?path=${encodeURIComponent(path)}${mode ? `&mode=${mode}` : ""}`
    ),
  lyricsEmbed: (path: string, lyrics: string) =>
    json<{ ok: boolean }>(`${API}/lyrics/embed`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, lyrics }),
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
  coverInfo: (albumPath: string, coverFile?: string | null) =>
    json<CoverInfo>(
      `${API}/cover/info?album=${encodeURIComponent(albumPath)}${coverFile ? `&file=${encodeURIComponent(coverFile)}` : ""}`
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
  playlistExportUrl: (id: number) => `${API}/playlists/${id}/export`,
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
  mbGenres: (id: string, limit?: number) =>
    json<import("./types").GenreCascade>(
      `${API}/mb/release-genres?mbid=${encodeURIComponent(id)}${limit ? `&limit=${limit}` : ""}`
    ),
  mbSearchReleases: (q: string, mode: "release" | "track" | "catno" | "barcode" = "release") =>
    json<any[]>(`${API}/mb/search/releases?q=${encodeURIComponent(q)}&mode=${mode}`),
  mbSearchArtists: (q: string) => json<any[]>(`${API}/mb/search/artists?q=${encodeURIComponent(q)}`),
  // Generic MusicBrainz browser (in-app entity pages). Searches and
  // discographies page 100 rows at a time — pass offset for "load more".
  // primaryType/secondaryType map onto MusicBrainz's own release-type
  // qualifiers (Album/EP/Single + Soundtrack/Live/Compilation/...).
  mbSearch: (
    type: string, q: string, limit = 100,
    mode: "free" | "catno" | "barcode" = "free", offset = 0,
    primaryType = "", secondaryType = ""
  ) =>
    json<{ rows: any[]; total: number }>(
      `${API}/mb/search?type=${encodeURIComponent(type)}&q=${encodeURIComponent(q)}` +
      `&limit=${limit}&offset=${offset}&mode=${mode}` +
      `&primary_type=${encodeURIComponent(primaryType)}&secondary_type=${encodeURIComponent(secondaryType)}`
    ),
  mbArtist: (id: string, offset = 0, limit = 300) =>
    json<any>(`${API}/mb/artist/${id}?offset=${offset}&limit=${limit}`),
  mbReleaseGroup: (id: string, offset = 0, limit = 300) =>
    json<any>(`${API}/mb/release-group/${id}?offset=${offset}&limit=${limit}`),
  mbRecording: (id: string, offset = 0, limit = 300) =>
    json<any>(`${API}/mb/recording/${id}?offset=${offset}&limit=${limit}`),
  mbIdentify: (id: string) =>
    json<{ type: string; id: string; title: string }>(`${API}/mb/detect/${id}`),
  mbMatch: (albumPath: string, releaseId: string) =>
    json<{ release: import("./types").MBRelease; suggestions: import("./types").MatchSuggestion[] }>(
      `${API}/mb/match`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ album_path: albumPath, release_id: releaseId }),
      }
    ),
  mbAssign: (tracks: Record<string, Record<string, string | null>>) =>
    json<{ ok: boolean; changed: number } & ContainerSwap>(`${API}/mb/assign`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tracks }),
    }).then(noteContainerSwap),

  lyricsSearch: (artist: string, track: string, album?: string, duration?: number) =>
    json<any[]>(`${API}/lyrics/search?artist=${encodeURIComponent(artist)}&track=${encodeURIComponent(track)}${album ? `&album=${encodeURIComponent(album)}` : ""}${duration ? `&duration=${duration}` : ""}`),
  lyricsGet: (artist: string, track: string, album?: string, duration?: number) =>
    json<any>(
      `${API}/lyrics/get?artist=${encodeURIComponent(artist)}&track=${encodeURIComponent(track)}${album ? `&album=${encodeURIComponent(album)}` : ""}${duration ? `&duration=${duration}` : ""}`
    ),
  lyricsWrite: (path: string, lrc: string) =>
    json<{ ok: boolean; lrc: string }>(`${API}/lyrics/write`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, lrc }),
    }),
  // Submit lyrics to LRCLIB on behalf of a track (or with explicit fields).
  lyricsPublish: (body: { path?: string; artist?: string; track?: string; album?: string; duration?: number; plain?: string; synced?: string }) =>
    json<{ ok: boolean; message: string }>(`${API}/lyrics/publish`, {
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

  coverUrl: (albumPath: string, coverFile?: string | null) =>
    `${API}/cover?album=${encodeURIComponent(albumPath)}${coverFile ? `&file=${encodeURIComponent(coverFile)}` : ""}`,
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
  importCommit: (targetDir: string, mbLink?: string, rymLink?: string) =>
    json<{ ok: boolean; changed: number }>(`${API}/import/commit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target_dir: targetDir, mb_link: mbLink || null, rym_link: rymLink || null }),
    }),
  /** Record the release's full tracklist on the album, so a PARTIAL import
   *  can grey out the tracks that never came in. Empty tracks clears it. */
  importExpected: (
    targetDir: string,
    releaseId: string | null,
    tracks: { disc: number; position: number; title?: string; recording_mbid?: string | null }[]
  ) =>
    json<{ ok: boolean; tracks: number }>(`${API}/import/expected`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target_dir: targetDir, release_id: releaseId || null, tracks }),
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

  dependencies: () =>
    json<{ deps_dir: string; tools: { key: string; name: string; installed_version?: string; latest_version?: string; detected_version?: string; path?: string | null; state: string }[] }>(
      `${API}/dependencies`
    ),
  installDependencies: (keys?: string[]) =>
    json<{ results: { key: string; name: string; ok: boolean; error?: string }[] }>(
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
  cover: (albumPath: string, file: File, track?: string, tracks?: string[]) => {
    const fd = new FormData();
    fd.append("file", file);
    return json<CoverWriteResult>(
      `${API}/cover?album=${encodeURIComponent(albumPath)}${coverQuery(track, tracks)}`,
      { method: "POST", body: fd }
    );
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
  /** Apply a cover image from a URL; same track/tracks targeting as `cover`. */
  coverFromUrl: (albumPath: string, url: string, track?: string, tracks?: string[]) =>
    json<CoverWriteResult>(
      `${API}/cover/fromurl?album=${encodeURIComponent(albumPath)}&url=${encodeURIComponent(url)}${coverQuery(track, tracks)}`,
      { method: "POST" },
      120000
    ),
  /** Drop `tracks` from the album's per-track cover manifest (all of them when
   *  omitted). The image file itself is never deleted. */
  coverClear: (albumPath: string, tracks?: string[]) =>
    json<{ ok: boolean }>(`${API}/cover/clear`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ album: albumPath, tracks: tracks?.length ? tracks : undefined }),
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
    json<any>(`${API}/soulseek/status`),
  soulseekStart: () =>
    json<{ ok: boolean; ready: boolean; message: string; has_credentials: boolean }>(`${API}/soulseek/start`, { method: "POST" }, 30000),
  soulseekRestart: () =>
    json<{ ok: boolean }>(`${API}/soulseek/restart`, { method: "POST" }, 60000),
  soulseekShares: () => json<any>(`${API}/soulseek/shares`),
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
  /** Clear FINISHED transfers (completed / errored / cancelled) from the
   *  history. In-progress and queued transfers are never touched. */
  soulseekDownloadsClear: (username?: string, states?: string[]) =>
    json<{ ok: boolean; cleared: number }>(`${API}/soulseek/downloads/clear`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, states }),
    }, 30000),
  // Completed downloads on disk — the review workflow (preview → tag → import).
  soulseekReview: () =>
    json<{ dir: string; files: { path: string; file: string; ext: string; is_video: boolean; size: number; mtime: number; user: string; tags: Record<string, string | null>; tech: Record<string, number | string> }[] }>(
      `${API}/soulseek/review`, undefined, 60000
    ),
  soulseekLocalFileUrl: (path: string) => `${API}/soulseek/local-file?path=${encodeURIComponent(path)}`,
  // Playable video preview — native stream when the browser can decode the
  // container, otherwise a live ffmpeg transcode (DVD VOB / Blu-ray M2TS).
  soulseekPreviewStreamUrl: (path: string) => `${API}/soulseek/preview-stream?path=${encodeURIComponent(path)}`,
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
  trackDownloadUrl: (path: string) => `${API}/track/download?path=${encodeURIComponent(path)}`,
  trackExportUrl: (path: string, codec: string, bitrate: number, level = 5) =>
    `${API}/track/export?path=${encodeURIComponent(path)}&codec=${encodeURIComponent(codec)}&bitrate=${bitrate}&level=${level}`,

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
  exportCodecs: () => json<{ codecs: Record<string, string> }>(`${API}/export/codecs`),
  exportRun: (body: { paths: string[]; dest: string; subfolder: string; codec: string; quality: string; structure: string }, timeoutMs = 1800000) =>
    json<{ ok: boolean; total: number; exported: number; skipped: number; failed: number; bytes: number; errors: string[] }>(`${API}/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }, timeoutMs),

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

  // Home page (recommendations + highlights)
  home: () => json<HomeData>(`${API}/home`, undefined, 60000),

  // ----------------------------------------------------------------- //
  // Discovery — recommendations, catalogue search, more-like-this.     //
  // Provider order and fallbacks live server-side; these calls just    //
  // ask for rows (see server/discovery.py).                            //
  // ----------------------------------------------------------------- //
  discoverySources: () => json<DiscoveryCatalog>(`${API}/discovery/sources`),
  discoverySearch: (
    q: string,
    type: "album" | "artist" = "album",
    limit = 25,
    opts?: { artist?: string; album?: string; source?: string }
  ) => {
    const p = new URLSearchParams({ q, type, limit: String(limit) });
    if (opts?.artist) p.set("artist", opts.artist);
    if (opts?.album) p.set("album", opts.album);
    if (opts?.source) p.set("source", opts.source);
    return json<DiscoverySearch>(`${API}/discovery/search?${p}`, undefined, 45000);
  },
  discoveryAlbum: (deezerId: number, resolve = false) =>
    json<DiscoveryAlbumDetail>(
      `${API}/discovery/album?deezer_id=${deezerId}${resolve ? "&resolve=1" : ""}`,
      undefined,
      45000
    ),
  discoveryPopular: (limit = 12, range = "week") =>
    json<{ rows: DiscoveryRow[]; range: string }>(
      `${API}/discovery/popular?limit=${limit}&range=${range}`,
      undefined,
      45000
    ),
  discoverySimilar: (
    kind: "album" | "track" | "artist",
    artist: string,
    opts?: { title?: string; album?: string; mbid?: string; limit?: number }
  ) => {
    const p = new URLSearchParams({ kind, artist, limit: String(opts?.limit ?? 12) });
    if (opts?.title) p.set("title", opts.title);
    if (opts?.album) p.set("album", opts.album);
    if (opts?.mbid) p.set("mbid", opts.mbid);
    return json<{ kind: string; artist: string; rows: DiscoveryRow[] }>(
      `${API}/discovery/similar?${p}`, undefined, 45000
    );
  },
  /** Turn a discovery row into a Soulseek wish (MusicBrainz-resolved when the
   *  row has no MBID yet). */
  discoveryWish: (body: { artist: string; title: string; year?: string; note?: string; mbid?: string }) =>
    json<{ ok: boolean; wish: Wish; resolved: DiscoveryRow | null }>(
      `${API}/discovery/wish`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
      45000
    ),

  // ----------------------------------------------------------------- //
  // Artist artwork + descriptions, album descriptions                  //
  // ----------------------------------------------------------------- //
  /** `artist` accepts the artist folder path (from the artist payload) or a
   *  plain artist name. */
  artistArtwork: (artist: string) =>
    json<ArtistArtwork>(`${API}/artist/artwork?artist=${encodeURIComponent(artist)}`),
  artistImageUrl: (artist: string) => `${API}/artist/image?artist=${encodeURIComponent(artist)}`,
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
  lyricsAuto: (paths: string[], force = false) =>
    json<{ results: LyricsAutoResult[]; order: string[]; ok: number; skipped: number; failed: number }>(
      `${API}/lyrics/auto`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths, force }),
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
  importAcoustid: (paths: string[], apply = false) =>
    json<AcoustidMatch>(
      `${API}/import/acoustid`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths, apply }),
      },
      600000
    ),
  /** Run the configured import script chain over already-imported albums. */
  importFinish: (paths: string[], force: Record<string, boolean> = {}) =>
    json<{ albums: { path: string; chain: number[]; scripts: unknown[]; errors: unknown[] }[] }>(
      `${API}/import/finish`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths, force }),
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
   *  preferred format), `"all"` every release. */
  mbAutoImport: (body: { mbid: string; kind?: "release" | "release_group" | "artist" | "auto"; mode?: "best" | "all" }) =>
    json<{ queued: number; items: { mbid: string; title: string; status: string }[]; skipped: { mbid: string; reason: string }[] }>(
      `${API}/mb/auto-import`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
      180000
    ),
  /** Fetch the advisory rating (ITUNESADVISORY) for one release or a set of
   *  tracks — the values land in `values` and are written to `paths`, and
   *  `answers` reports every provider that had something to say (the UI's
   *  provenance). */
  mbAdvisoryFetch: (body: { paths?: string[]; release_mbid?: string }) =>
    json<AdvisoryFetchResult>(`${API}/mb/advisory/fetch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }, 120000),

  /** Check INSTRUMENTAL for a set of tracks: each value the sources can state
   *  is written, and `evidence` reports who decided it. */
  instrumentalFetch: (paths: string[]) =>
    json<InstrumentalFetchResult>(`${API}/instrumental/fetch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths }),
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

  /** Import genres for a set of album/track paths from the configured
   *  sources; `per_source` reports how many values each source contributed. */
  genresImport: (paths: string[], limit?: number) =>
    json<{ updated: number; per_source: Record<string, number> }>(`${API}/genres/import`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths, limit }),
    }, 300000),
  /** Genre browsing surface: every genre with its track count, plus the
   *  category cards that group them. */
  genresFacets: () =>
    json<{ genres: { name: string; count: number }[]; categories: { name: string; genres: string[] }[] }>(
      `${API}/genres/facets`
    ),

  /** Candidate artist images + descriptions for the metadata review modal. */
  metadataCandidates: (artist: string, albumPath?: string) => {
    const p = new URLSearchParams({ artist });
    if (albumPath) p.set("album_path", albumPath);
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
    `${API}/videos/thumb?path=${encodeURIComponent(path)}&t=${Math.max(0, Math.floor(t))}&w=${Math.round(w)}`,
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