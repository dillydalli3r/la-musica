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
   *  stray_in_artists, hidden_folder, legacy_state_file. */
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
}

export interface CoverResult {
  source: string;
  small: string | null;
  big: string | null;
  title: string | null;
  artist: string | null;
  tracks: number | null;
  url: string | null;
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

/** Home page payload: album recommendations + library highlights. */
export interface HomeData {
  stats: {
    artists: number;
    albums: number;
    tracks: number;
    playlists: number;
    grade_pct: number | null;
  };
  recent: HomeAlbum[];
  recommended: HomeAlbum[];
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
}