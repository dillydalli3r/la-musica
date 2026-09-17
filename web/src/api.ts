import type { CoverInfo, CoverSourceCatalog, CoverWriteResult, DownloadsPayload, LayoutReport } from "./types";

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
  } | null;
  confirm: {
    reason: string;
    formats: string[];
    candidates: {
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
  /** Server-side directory browser — lets the web UI pick a custom music
   * directory without the desktop shell's native folder picker. */
  fsList: (path?: string) =>
    json<{ path: string; parent: string | null; dirs: string[] }>(
      `${API}/fs/list${path ? `?path=${encodeURIComponent(path)}` : ""}`
    ),
  /** Opens the OS folder dialog on the backend's machine (Windows dialog in
   * the browser version). `supported:false` means fall back to fsList. */
  fsPickFolder: (initial?: string) =>
    json<{ path: string | null; supported: boolean }>(
      `${API}/fs/pick${initial ? `?initial=${encodeURIComponent(initial)}` : ""}`,
      undefined,
      300000
    ),

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
  // ReplayGain preamp for playback loudness matching (null when untagged).
  replaygain: (path: string) =>
    json<{ path: string; gain: number | null; peak: number | null }>(
      `${API}/replaygain?path=${encodeURIComponent(path)}`
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
    json<{ ok: boolean; path: string; renamed: boolean; tech: Record<string, number | string> }>(`${API}/videos/tag`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, tags }),
    }, 600000),
  /** Dimensions etc. of an album's cover — or, with `coverFile`, of any image
   *  in the album folder, which is how a track's own art is measured. */
  coverInfo: (albumPath: string, coverFile?: string | null) =>
    json<CoverInfo>(
      `${API}/cover/info?album=${encodeURIComponent(albumPath)}${coverFile ? `&file=${encodeURIComponent(coverFile)}` : ""}`
    ),

  run: (ids: number[], targets?: string[], force?: Record<string, boolean>) =>
    json<{ results: any[] }>(`${API}/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids, targets, force }),
    }),

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
    json<{ ok: boolean; changed: number }>(`${API}/mb/assign`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tracks }),
    }),

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
  lyricsAi: (
    mode: "clean" | "repair" | "wordsync",
    text: string,
    opts?: { artist?: string; track?: string; candidates?: string[] }
  ) =>
    json<{ mode: string; result: string }>(`${API}/lyrics/ai`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mode,
        text,
        artist: opts?.artist ?? "",
        track: opts?.track ?? "",
        candidates: opts?.candidates,
      }),
    }, 180000),

  // Transliterate one track and store it like the Lyrics Translate script
  // (TRANSLITERATION-<lang>-LATN tag + sidecar per settings). The LYRICS
  // field keeps the original language.
  lyricsXlitStore: (path: string) =>
    json<{ ok?: boolean; skipped?: string; xlit: string }>(`${API}/lyrics/xlit/store`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    }, 300000),

  // Bulk tag surgery: delete `remove` tags and set `set` {tag: value}
  // (empty value = delete) across the given tracks.
  tagsBulk: (body: { paths: string[]; remove: string[]; set: Record<string, string> }) =>
    json<{ ok: boolean; removed: number; added: number; failed: number; errors?: string[] }>(`${API}/tags/bulk`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }, 300000),

  rymValidate: (url: string) => json<{ valid: boolean }>(`${API}/rym/validate?url=${encodeURIComponent(url)}`),

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

  coverSearch: (
    artist: string,
    album: string,
    opts?: { sources?: string[]; country?: string }
  ) => {
    const q = new URLSearchParams({ artist, album });
    if (opts?.sources?.length) q.set("sources", opts.sources.join(","));
    if (opts?.country) q.set("country", opts.country);
    return json<{ results: import("./types").CoverResult[] }>(
      `${API}/cover/search?${q}`,
      undefined,
      90000
    );
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
  lyricsAiLines: (mode: "translate" | "transliterate", lines: string[]) =>
    json<{ mode: string; lines: string[]; skipped?: string }>(`${API}/lyrics/ai/lines`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode, lines }),
    }, 180000),
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
  wishes: () => json<import("./types").WishesPayload>(`${API}/wishes`, undefined, 30000),
  wishAdd: (body: { release_mbid: string; title?: string; artist?: string; year?: string; note?: string; target_dir?: string; queries?: string[] }) =>
    json<{ ok: boolean; wish: import("./types").Wish }>(`${API}/wishes`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }, 60000),
  wishUpdate: (id: number, patch: { note?: string; target_dir?: string; status?: string; queries?: string[] }) =>
    json<{ ok: boolean; wish: import("./types").Wish }>(`${API}/wishes/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
  wishDelete: (id: number) => json<{ ok: boolean }>(`${API}/wishes/${id}`, { method: "DELETE" }),
  wishSearch: (id: number) => json<{ ok: boolean; error?: string }>(`${API}/wishes/${id}/search`, { method: "POST" }, 30000),
  wishesSearchAll: () => json<{ ok: boolean; error?: string }>(`${API}/wishes/search-all`, { method: "POST" }, 30000),
  wishesReconcile: () => json<{ ok: boolean; resolved: number }>(`${API}/wishes/reconcile`, { method: "POST" }, 120000),

  // Home page (recommendations + highlights)
  home: () => json<import("./types").HomeData>(`${API}/home`, undefined, 60000),
};