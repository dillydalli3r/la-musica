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