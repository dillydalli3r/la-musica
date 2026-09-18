export interface Library {
  folder: string;
  artists: Artist[];
  error?: string;
}

export interface Aggregate {
  album_count: number;
  track_count: number;
  pass_count: number;
  total_checks: number;
  grade_pct: number | null;
  audit_summary: "REAL" | "FAKE" | "Mix" | null;
}

export interface AlbumMeta {
  ALBUM?: string | null;
  ALBUMARTIST?: string | null;
  ARTIST?: string | null;
  DATE?: string | null;
  ORIGINALDATE?: string | null;
  ITUNESADVISORY?: string | null;
  ALBUMITUNESADVISORY?: string | null;
  MUSICBRAINZ_ALBUMID?: string | null;
  MUSICBRAINZ_ALBUMARTISTID?: string | null;
  MUSICBRAINZ_RELEASEGROUPID?: string | null;
  RATEYOURMUSIC_ALBUM?: string | null;
  MEDIA?: string | null;
  CATALOGNUMBER?: string | null;
  LABEL?: string | null;
  "ALBUM DYNAMIC RANGE"?: string | null;
}

export interface Tech {
  length?: number;
  bitrate?: number;
  sample_rate?: number;
  bits_per_sample?: number;
  channels?: number;
  codec?: string;
  width?: number;
  height?: number;
}

export interface TrackTags {
  TITLE?: string | null;
  "DYNAMIC RANGE"?: string | null;
  "ALBUM DYNAMIC RANGE"?: string | null;
  REPLAYGAIN_TRACK_GAIN?: string | null;
  ARTIST?: string | null;
  ALBUM?: string | null;
  DATE?: string | null;
  GENRE?: string | null;
  ITUNESADVISORY?: string | null;
  INSTRUMENTAL?: string | null;
  MEDIA?: string | null;
  SOURCE?: string | null;
  TRACKNUMBER?: string | null;
  DISCNUMBER?: string | null;
  MUSICBRAINZ_ALBUMID?: string | null;
  MUSICBRAINZ_ALBUMARTISTID?: string | null;
  MUSICBRAINZ_ARTISTID?: string | null;
  MUSICBRAINZ_TRACKID?: string | null;
  MUSICBRAINZ_RELEASEGROUPID?: string | null;
  RATEYOURMUSIC_ALBUM?: string | null;
  RATEYOURMUSIC_TRACK?: string | null;
  RATEYOURMUSIC_ARTIST?: string | null;
  CATALOGNUMBER?: string | null;
  LABEL?: string | null;
  RELEASETYPE?: string | null;
  RELEASECOUNTRY?: string | null;
  COMPOSER?: string | null;
  LYRICIST?: string | null;
  REMIXER?: string | null;
  COPYRIGHT?: string | null;
  ISRC?: string | null;
}

export interface Track {
  file: string;
  path: string;
  tracknumber?: number | null;
  discnumber?: number | null;
  issues: string[];
  values: Record<string, string | null>;
  audit: string | null;
  log_grade: string | null;
  accuraterip_status?: string;
  checksum_status?: string;
  lyrics_embedded: boolean;
  lyrics_lrc: boolean;
  unreadable: boolean;
  tech: Tech;
  tags: TrackTags;
  grade_pass: boolean;
  lyrics_present: boolean;
  cover_file?: string | null;
  sidecar_cover?: boolean;
  sidecar_cover_file?: string | null;
  /** Music-video container (MKV/MP4/VOB/…) — plays with <video>. */
  is_video?: boolean;
}

export interface Album {
  path: string;
  error?: string;
  meta?: AlbumMeta;
  album_artist?: string | null;
  album_values?: Record<string, string>;
  grade_pct: number | null;
  pass: boolean;
  pass_count: number;
  total_checks: number;
  track_count: number;
  audit_summary: "REAL" | "FAKE" | "Mix" | null;
  cover_file: string | null;
  has_log: boolean;
  has_cue: boolean;
  checksum_status: string;
  accuraterip_status: string;
  lyrics_present: number;
  lyrics_expected: number;
  instrumental_count: number;
  media: string;
  source_summary: string | null;
  /** Grouped grading issues: full issue text → affected files ("album" for
   * album-level checks). Powers the condensed FAIL details on the album page. */
  issues?: Record<string, string[]>;
  tracks: Track[];
  /** The release's own tracklist, recorded at import time. Present only for
   *  albums the import wizard matched to a MusicBrainz release; `missing`
   *  marks the tracks that never made it into the folder (a PARTIAL import),
   *  which the album page renders greyed out. */
  expected_tracks?: ExpectedTrack[];
  expected_release_id?: string | null;
  /** True when at least one expected track is absent. */
  partial?: boolean;
  /** The album folder's stored description (see mlo/artistdata). The library
   *  payload only reports whether one exists; the album page carries the text. */
  artwork?: AlbumArtwork;
}

/** One track of the MusicBrainz release an album was matched to. */
export interface ExpectedTrack {
  disc: number;
  position: number;
  title: string;
  recording_mbid?: string | null;
  /** Not present on disk — greyed out on the album page. */
  missing: boolean;
}

/** One entry of <music>/.mlo/downloads — slskd's staging area. */
export interface DownloadEntry {
  name: string;
  /** A folder (an album) rather than a loose file. */
  dir: boolean;
  bytes: number;
  files: number;
  audio: number;
  images: number;
  /** Holds audio, so it can be imported into the library as an album. */
  album: boolean;
  /** slskd's own in-flight leftover — not a completed result. */
  partial: boolean;
}

export interface DownloadsPayload {
  folder: string;
  exists: boolean;
  count: number;
  bytes: number;
  entries: DownloadEntry[];
  music_folder: string;
}

/** One place the music folder does not match
 *  `<music>/Artists/<Artist>/<Album>/<files>`. */
export interface LayoutIssue {
  /** What is wrong: audio_at_root, audio_in_artists, audio_in_artist,
   *  unexpected_folder, unexpected_subfolder, empty_album, stray_file,
   *  stray_in_artists, hidden_folder, legacy_state_file, wrong_case. */
  kind: string;
  /** Music-folder-relative path, for display. */
  path: string;
  /** Absolute path, for "open folder" style actions. */
  abs: string;
  detail: string;
  hint: string;
}

export interface LayoutReport {
  folder: string;
  artists_dir: string;
  exists: boolean;
  issues: LayoutIssue[];
  /** issue kind → count. */
  counts: Record<string, number>;
  total: number;
  albums: number;
  artists: number;
  audio_files: number;
}

/** One cover-art provider the musichoarders meta-search can query. */
export interface CoverSource {
  id: string;
  name: string;
  enabled: boolean;
  color?: string | null;
  /** Regions this source serves (lower-case ISO codes). */
  countries: string[];
}

export interface CoverSourceCatalog {
  sources: CoverSource[];
  /** Every region the meta-search accepts (lower-case ISO codes). */
  countries: string[];
  /** How many sources one search may use at once. */
  active_source_limit: number;
  /** What a search with no overrides would use. */
  default_sources: string[];
  default_country: string;
  saved_sources: string[];
  saved_country: string;
}

export interface Artist {
  path: string;
  name: string;
  display_name?: string | null;
  albums: Album[];
  aggregate: Aggregate;
  /** Artist image + description stored in the artist folder. */
  artwork?: ArtistArtworkFields;
  /** Artist-level grading: only the checks that apply to an artist folder. */
  grade?: ArtistGrade;
}

export interface CoverResult {
  source: string;
  small: string | null;
  big: string | null;
  title: string | null;
  artist: string | null;
  tracks: number | null;
  url: string | null;
  /** The image's REAL pixel size, read from its own header bytes by the
   *  backend. `null` means unknown — the first results are probed, the rest
   *  report null rather than a guess (a CDN URL's "500x0w" is a request hint,
   *  not the size the URL answers with). */
  width?: number | null;
  height?: number | null;
}

/** `/api/cover/search` — `provider` names who actually answered: `"cov"` for
 *  the covers.musichoarders.xyz meta-search, the fallback id that filled in
 *  (`coverartarchive`/`deezer`/`itunes`), or null when nobody had anything. */
export interface CoverSearch {
  provider: string | null;
  results: CoverResult[];
}

/** Response of the cover write endpoints (`/api/cover`, `/api/cover/fromurl`):
 *  dimensions of the file that landed on disk, plus `warning` when it is below
 *  the configured `cover_target_size` — feedback only, the write succeeded. */
export interface CoverWriteResult {
  ok: boolean;
  path: string;
  width?: number;
  height?: number;
  megapixels?: number;
  warning?: string | null;
  /** The minimum resolution this cover will be graded against (px). */
  target?: number;
  /** Under that minimum — the warning above says so, and grading will flag it. */
  below_target?: boolean;
  /** The image was downscaled/cropped/re-encoded on write. */
  compressed?: boolean;
  /** Dimensions and byte size BEFORE that compression, for the feedback. */
  original_width?: number | null;
  original_height?: number | null;
  original_bytes?: number;
  bytes?: number;
}

/** `/api/cover/info` — the album cover, or with `file` any image in the album
 *  folder (which is how a track's own art gets measured). */
export interface CoverInfo {
  file: string | null;
  format: string;
  bytes: number;
  width: number | null;
  height: number | null;
  aspect: string | null;
  aspect_label: string | null;
  megapixels: number | null;
}

/** Per-track cover state the tagging wizard reads off `Album.tracks`:
 *  `cover_file` is the image actually serving that track (per-track manifest
 *  or sidecar — identical to `Album.cover_file` when the track just uses the
 *  album cover). Feed it to `api.coverInfo(album, cover_file)` for dimensions,
 *  and to `api.coverUrl(album, cover_file)` for the thumbnail. */
export type TrackCoverState = Pick<
  Track,
  "file" | "cover_file" | "sidecar_cover" | "sidecar_cover_file"
>;

export interface Playlist {
  id: number;
  name: string;
  kind: "manual" | "smart";
  filter: { conditions: FilterCondition[]; match: "all" | "any" } | null;
  track_count: number;
  tracks?: string[];
  created?: number;
  updated?: number;
}

export interface FilterCondition {
  field: string;
  op: string;
  value?: string | number | boolean;
}

export interface Progress {
  done: number;
  total: number;
  desc: string;
}

export interface GradeResult {
  path: string;
  pass_count: number;
  total_checks: number;
  grade_pct: number | null;
  pass: boolean;
  audit_summary: string | null;
  tracks: Track[];
  issues?: Record<string, string[]>;
}

export interface MBPerson {
  name: string;
  mbid?: string;
}

export interface MBTrack {
  position: number;
  disc: number;
  title: string;
  length: number | null;
  recording_mbid: string | null;
  artist_mbids: string[];
  artist_credit: string;
  genres: string[];
}

export interface MBRelease {
  id: string;
  title: string;
  date: string;
  release_group_id: string | null;
  release_type?: string;
  /** the release group's MusicBrainz type: primary (Album/EP/Single/…) plus
   *  any secondary types (Soundtrack/Live/Compilation/…) */
  primary_type?: string;
  secondary_types?: string[];
  barcode?: string;
  country?: string;
  catalog_number?: string;
  label?: string;
  /** MusicBrainz release status ("Official", "Promotion", "Bootleg", …) — the
   *  value the promo/format badges read. */
  status?: string;
  /** The release's carrier description ("CD", "Digital Media", …). */
  medium?: string;
  /** The credited artist's MBID, when release_lookup supplies one. */
  artist_mbid?: string;
  artists: MBPerson[];
  genres: string[];
  media: MBTrack[];
  medium_count: number;
  medium_formats?: string[];
}

export interface MatchSuggestion {
  local: string;
  file: string;
  matched: boolean;
  confidence: number;
  release_track: MBTrack | null;
}

export interface GenreCascade {
  per_track: { position: number; disc: number; title: string; genres: string[]; source: string | null }[];
  levels: { track: boolean; release: boolean; release_group: boolean; artist: boolean };
}

export interface Wish {
  id: number;
  release_mbid: string;
  title: string;
  artist: string;
  year: string;
  status: "wanted" | "searching" | "imported" | "failed" | "available";
  note: string;
  target_dir: string;
  queries: string[];
  attempts: number;
  added_at: number;
  updated_at: number;
  last_search: number;
  last_error: string;
  album_path: string;
}

export interface WishesPayload {
  wishes: Wish[];
  worker: {
    running: boolean;
    enabled: boolean;
    current: string | null;
    last_cycle: number;
    last_result: string;
    next_run: number;
    interval_hours: number;
  };
  log: { t: number; level: string; msg: string }[];
}

/** Home page payload: library highlights. */
export interface HomeData {
  stats: {
    artists: number;
    albums: number;
    tracks: number;
    playlists: number;
    grade_pct: number | null;
  };
  recent: HomeAlbum[];
  top_rated: HomeAlbum[];
  favorites: HomeAlbum[];
  discover: HomeAlbum[];
  top_artists: HomeArtist[];
  wanted: HomeAlbum[];
  needs_attention: HomeAlbum[];
}

export interface HomeArtist {
  path: string;
  artist: string;
  album_count: number;
  track_count: number;
  grade_pct: number | null;
  cover_path: string;
  cover: string | null;
}

export interface HomeAlbum {
  /** Library path when owned, else "" (recommendation). */
  path: string;
  album: string;
  artist: string;
  year?: string | null;
  cover: string | null;
  reason?: string;
  mbid?: string | null;
  /** MusicBrainz entity type behind `mbid` — "rg" (release group) or "release". */
  mb_kind?: string;
  grade_pct?: number | null;
  owned?: boolean;
  /** Which discovery provider produced the row ("deezer", "listenbrainz", …).
   *  Absent for owned-library shelves. */
  source?: string;
  /** Provider popularity, already formatted ("82k fans", "287k listens"). */
  popularity_label?: string | null;
  /** Raw provider popularity, for sorting. */
  popularity?: number | null;
  /** Provider catalogue id behind the row. */
  deezer_id?: number | null;
  /** Remote artwork URL — `cover` is the local cover file. */
  cover_url?: string | null;
}

/* ---------------------------------------------------------------------- *
 * Discovery — provider chain (server/discovery.py)                        *
 * ---------------------------------------------------------------------- */

/** One selectable provider, as reported by `/api/discovery/sources`. */
export interface DiscoverySource {
  id: string;
  label: string;
  notes: string;
}

/** Provider catalogue + the per-feature orders the settings page edits. */
export interface DiscoveryCatalog {
  sources: DiscoverySource[];
  /** Built-in order per feature: artist_image_sources, description_sources. */
  defaults: Record<string, string[]>;
  enabled: boolean;
  /** The user's saved orders (empty = built-in). */
  saved: Record<string, string[]>;
  mb_search_source: "auto" | "discovery" | "musicbrainz";
}

/** Candidate artist image for the picker. */
export interface DiscoveryImageRow {
  url: string;
  source: string;
  label: string;
  kind: "photo" | "wide" | "album_art";
}

/* ---------------------------------------------------------------------- *
 * Artist artwork + descriptions (mlo/artistdata.py, /api/artist/artwork)  *
 * ---------------------------------------------------------------------- */

export interface ArtistArtworkImage {
  present: boolean;
  file: string | null;
  /** `/api/artist/image?artist=…` — present only when an image exists. */
  url: string | null;
  source: string | null;
  label: string | null;
  source_url: string | null;
  fetched: string | null;
  updated: string | null;
}

export interface ArtistArtworkDescription {
  present: boolean;
  text: string | null;
  source: string | null;
  source_url: string | null;
  fetched: string | null;
}

/** Artist payload's stored artwork (same shape minus the grade). */
export interface ArtistArtworkFields {
  image: boolean;
  image_file: string | null;
  image_url: string | null;
  description: string | null;
  description_source: string | null;
  description_url: string | null;
  provenance?: Record<string, string | null>;
}

export interface ArtistGradeIssue {
  code: string;
  label: string;
  where?: string;
}

/** Artist-level grading: only what applies to an artist folder (image and
 *  description), never the album checks. */
export interface ArtistGrade {
  path?: string;
  checks?: number;
  pass_count?: number;
  failed_checks?: number;
  pct?: number | null;
  pass?: boolean;
  issues?: ArtistGradeIssue[];
  artwork?: { image: boolean; image_file: string | null; description: boolean };
  error?: string;
}

export interface ArtistArtwork {
  artist: string;
  path: string;
  image: ArtistArtworkImage;
  description: ArtistArtworkDescription;
  provenance: Record<string, string | null>;
  grade: ArtistGrade;
}

/** Album folder description metadata (server/library.py → build_album). */
export interface AlbumArtwork {
  description: boolean;
  /** Null in the library payload (presence only) — the album page has the text. */
  description_text: string | null;
  description_source: string | null;
  description_url: string | null;
}

/* ---------------------------------------------------------------------- *
 * Lyrics chain (/api/lyrics/providers, /api/lyrics/auto)                  *
 * ---------------------------------------------------------------------- */

export interface LyricsProvider {
  id: string;
  label: string;
  notes: string;
  /** 1-based rank in the built-in chain (mlo/lyrics_providers.available_sources). */
  rank?: number;
  /** Every shipped provider is time-synced and free; the flags come from the
   *  backend (`available_sources`) so a plain-only one could never be
   *  ticked in the settings picker by accident. */
  kind?: string;
  synced?: boolean;
  free?: boolean;
  needs?: string[];
}

export interface LyricsProviders {
  sources: LyricsProvider[];
  default_order: string[];
  /** The order actually used (saved order, or the built-in one). */
  order: string[];
  saved: string[];
  allow_plain: boolean;
  labels: Record<string, string>;
  notes: Record<string, string>;
}

export interface LyricsAutoResult {
  path: string;
  status: "ok" | "skipped" | "failed";
  provider: string | null;
  provider_label: string | null;
  synced: boolean;
  wrote: { embedded: boolean; lrc: string | null };
  reason?: string;
  error?: string;
}

/** A lyrics lookup that wrote nothing (`/api/lyrics/find`). */
export interface LyricsHit {
  found: boolean;
  order: string[];
  provider?: string;
  provider_label?: string;
  synced?: string | null;
  plain?: string | null;
  instrumental?: boolean;
  duration?: number | null;
  matched_artist?: string;
  matched_title?: string;
  matched_album?: string | null;
}

/* ---------------------------------------------------------------------- *
 * Import — AcoustID, script chain, bulk queue (/api/import/*)              *
 * ---------------------------------------------------------------------- */

export interface AcoustidRecording {
  path: string;
  recording_id: string;
  title: string;
  score: number;
}

export interface AcoustidAlbumMatch {
  path: string;
  release_group_id?: string | null;
  release_group_title?: string | null;
  release_group_type?: string | null;
  artists?: string[];
  score?: number;
  matched?: number;
  total?: number;
  /** Tracks whose ACOUSTID_ID / ACOUSTID_FINGERPRINT tags were written when
   *  the request asked to apply the match. */
  tagged?: number;
  recordings?: AcoustidRecording[];
}

export interface AcoustidMatch {
  /** False when no API key is configured or fpcalc is not installed. */
  available: boolean;
  /** Human reason when unavailable ("no API key", "fpcalc not installed"). */
  note: string;
  albums: AcoustidAlbumMatch[];
}

export interface ImportBulkItem {
  path: string;
  status: "imported" | "failed" | "skipped" | string;
  album_path?: string;
  error?: string;
  /** Per-script results of the import chain for this item. */
  scripts?: { id: number; name?: string; error?: string }[];
}

export interface ImportBulkJob {
  id?: string;
  kind?: string;
  status?: "idle" | "running" | "done" | "failed" | string;
  started?: number;
  finished?: number;
  total?: number;
  done?: number;
  label?: string;
  items?: ImportBulkItem[];
  error?: string;
}

export interface ImportBulkResult {
  ok: boolean;
  job?: ImportBulkJob;
  error?: string;
}

export interface ImportScriptsPreview {
  chain: number[];
  labels: Record<string, string>;
  count: number;
}

/** One entry of `/api/run`'s (and the import chain's) per-script report. */
export interface ScriptRunResult {
  id: number;
  name?: string;
  label?: string;
  stats?: Record<string, unknown>;
  /** The script was skipped because its feature is switched off. */
  skipped?: boolean;
  reason?: string;
  error?: string;
}

/* ---------------------------------------------------------------------- *
 * Source health (/api/sources/health) — the setup wizard + Settings panel   *
 * ---------------------------------------------------------------------- */

/** The provider families the backend reports on (`server.sources_health.KINDS`). */
export type SourceKind = "lyrics" | "advisory" | "genre" | "metadata" | "links";

/** One provider row. `needs` are config keys this source reads; `configured`
 *  says whether they are all set, and `status`/`detail`/`ms` come from the
 *  last probe (`fail`/`skipped` are states, never errors). */
export interface SourceHealth {
  id: string;
  kind: SourceKind;
  label: string;
  free: boolean;
  synced?: boolean;
  /** 1-based position in the built-in chain, and the registry's own blurb —
   *  only lyrics rows carry these today. */
  rank?: number;
  notes?: string;
  needs: string[];
  configured: boolean;
  status: "ok" | "skipped" | "fail";
  detail: string;
  ms: number;
}

export interface SourcesHealth {
  checked_at: string;
  sources: SourceHealth[];
}

