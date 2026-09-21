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
  /** Every country the release was released in. One code on most files, a
   *  list ("US; CA") on the ones tagged from a release group's events — the
   *  album card's badge shows them all. */
  RELEASECOUNTRY?: string | null;
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
  /** The release's own track total, as the file states it — the second source
   *  `server.imports._album_track_count` reads for the album's identity (a
   *  folder's FILE count is not it: a partial import would contradict every
   *  correct release). */
  TRACKTOTAL?: string | null;
  TOTALTRACKS?: string | null;
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
  /** A FRAMEWORK album: "Add to library" created this folder before its audio
   *  arrived, so it is listed with `track_count` 0, `tracks` empty and the
   *  release's tracklist in `expected_tracks` (every entry missing). The album
   *  page must say it is pending rather than complete, and the placeholder
   *  cover is the only artwork it has until the import writes a real one. */
  pending?: boolean;
  /** What the album is waiting for ("a verified Soulseek download"). */
  pending_reason?: string;
  /** The wish searching for its audio (the queue row it belongs to). */
  wish_id?: number | null;
  /** The wish's own state, for a framework album: `status`/`attempts`/the
   *  reason a run left behind, plus when it is next searched (`due_at` /
   *  `due_in` seconds, `terminal` once the queue will not try again) — decided
   *  by the worker's own policy, so the page states it rather than guessing. */
  wish?: AlbumWish | null;
  /** What "Add to library" already fetched for this folder before its audio
   *  existed: the artist image / descriptions and the album description it
   *  wrote (their paths), the links it resolved and the cover candidates it
   *  ranked, with the winner it picked. */
  prefetched?: AlbumPrefetch | null;
  /** The album folder's stored description (see mlo/artistdata). The library
   *  payload only reports whether one exists; the album page carries the text. */
  artwork?: AlbumArtwork;
}

/** The wish filling a framework album, as the album page reads it
 *  (`server/library.py::_wish_state`). */
export interface AlbumWish {
  id: number;
  /** "wanted" | "searching" | "imported" | "failed" | "not_found". */
  status: string;
  attempts: number;
  retry_at: number;
  last_search: number;
  /** Epoch seconds of the next search the worker will run, null once it will
   *  not run one again. */
  due_at: number | null;
  /** Seconds until that search, null when `due_at` is null. */
  due_in: number | null;
  /** No further automatic search is coming (see server.wishes.is_terminal). */
  terminal: boolean;
  /** The last error the queue recorded, or the note the wish was added with. */
  reason: string;
  note: string;
  source: string;
  queries: string[];
}

/** What "Add to library" pre-fetched for a folder whose audio has not arrived
 *  (`server/imports.prefetch_album`, recorded in the framework marker). */
export interface AlbumPrefetch {
  at: number;
  artist_image: string | null;
  artist_description: string | null;
  album_description: string | null;
  cover_candidates: number;
  cover_pick: string | null;
  cover_source: string | null;
  links: { album: string | null; artist: string | null };
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
  /** The container those same bytes really are ("jpeg"/"png"/"webp"), or null
   *  when the image was never probed. */
  format?: string | null;
  /** How many bytes the URL answered with (0 = an empty answer). */
  bytes?: number | null;
  /** The provider's own labelling: `front` is true for a labelled front cover,
   *  false for anything it labels otherwise, null when it labels nothing. */
  front?: boolean | null;
  kind?: string | null;
  /** True for the RELEASE's own front cover, false for a release-group
   *  stand-in, null when the source states neither. */
  release_cover?: boolean | null;
  /** The provider's own order for this row (0 = its first answer). */
  rank?: number | null;
  /** 0..1, higher = better; rows arrive sorted by it (see mlo/cover_choice). */
  score?: number;
  /** The sentences that put this candidate where it is — for the winner, the
   *  one that says why it won; for a loser, why it lost. */
  reasons?: string[];
  /** Set when the candidate cannot be the AUTOMATIC pick (below the cover
   *  target, undecodable, an empty answer): the sentence naming why. It is
   *  still listed, and can still be applied by hand. */
  rejected?: string | null;
}

/** The cover policy as configured — what the pick was judged by, and the
 *  rules in prose (mlo/cover_choice.py). */
export interface CoverChoicePolicy {
  target: number;
  /** The floor a candidate must reach: `cover_target_size` while the write
   *  path resizes to it, 0 when nothing is enforced. */
  minimum: number;
  sources: string[];
  square_threshold: number;
  enforce_square: boolean;
  jpeg_quality: number;
  rules: string[];
}

/** The album a cover reply's rows were CHECKED against — what the server echoes
 *  back after verifying each candidate's own release (mlo/cover_choice rule 2).
 *  It is the request's own `artist`/`album`/`tracks`, so the finder can say
 *  what a row had to BE; `tracks` is null when the caller stated no track
 *  count, i.e. nothing was verified against one. */
export interface CoverSearchIdentity {
  artist: string;
  album: string;
  tracks: number | null;
}

/** `/api/cover/search` — `results` are the policy's RANKED candidates (best
 *  first, each carrying its reasons), `chosen` is the winner (null when none
 *  could be one) and `notes` states what every source did — including a source
 *  that was skipped, and why nothing was chosen when nothing could be. */
export interface CoverSearch {
  provider: string | null;
  results: CoverResult[];
  chosen?: CoverResult | null;
  notes?: string[];
  policy?: CoverChoicePolicy;
  candidate_count?: number;
  rejected_count?: number;
  /** The identity the rows were verified against (see `CoverSearchIdentity`).
   *  Additive: an older server answers without it. */
  identity?: CoverSearchIdentity;
}

/** Response of the cover write endpoints (`/api/cover`, `/api/cover/fromurl`):
 *  dimensions of the file that landed on disk, plus `warning` when it is below
 *  the configured `cover_target_size` — feedback only, the write succeeded. */
export interface CoverWriteResult {
  ok: boolean;
  path: string;
  /** Identifies the file's BYTES (its mtime + size). A cover is replaced in
   *  place — the album and file name, and therefore the plain cover URL, are
   *  the same before and after — so this is what makes the new image a new
   *  URL. `api.coverUrl` picks it up automatically after a write. */
  token?: string;
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
  /** MusicBrainz's own release-level country CODE — the FIRST release event,
   *  and the value the release-choice policy keys on. `countries` below is the
   *  whole list. */
  country?: string;
  /** every country this pressing was released in, with its date */
  countries?: MBCountryEvent[];
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

/** One row of the in-app MusicBrainz browser's search (`GET /api/mb/search`).
 *  The server normalizes MusicBrainz's own payloads, so only the MBID is
 *  guaranteed and each entity kind fills in what its index carries. */
export interface MBSearchRow {
  id: string;
  score?: number;
  title?: string;
  disambiguation?: string;
  /** the credited artist — release groups, releases and recordings */
  artist?: string;
  artist_mbid?: string;
  /** the credited artists, when a payload carries them separately */
  artists?: { name?: string; mbid?: string }[];
  status?: string;
  formats?: string;
  /** the release group's type: Album/EP/Single/… — releases and groups */
  primary_type?: string;
  secondary_types?: string[];
  release_type?: string;
  catalog_number?: string;
  track_count?: number;
  country?: string;
  date?: string;
  first_release_date?: string;
  /** artist rows: MusicBrainz's own kind ("Group", "Person", …) */
  type?: string;
  /** artist rows: [begin, end], either possibly empty */
  life_span?: string[];
  /** artist rows: the first few tag names */
  tags?: string[];
  /** recording rows: length in milliseconds */
  length?: number | null;
}

/** A page of search rows plus MusicBrainz's match count — the browser pages
 *  100 rows at a time by following `next` (the offset of the following page,
 *  null at the end; rows MusicBrainz repeated are already de-duplicated, which
 *  is why the next offset is the server's answer and not a row count). */
export interface MBSearchRows {
  rows: MBSearchRow[];
  total: number;
  /** Offset of this page. */
  offset?: number;
  /** Offset of the next page, or null/absent at the end. */
  next?: number | null;
  /** The Lucene query the index was asked — shown to the user. */
  query?: string;
}

/** One edition row of a release-group or recording page. */
export interface MBReleaseRow {
  id: string;
  title: string;
  date?: string;
  country?: string;
  status?: string;
  formats?: string;
  disc_count?: number;
  track_count?: number;
  /** per-disc track counts ("10 + 11") for a multi-disc edition */
  track_breakdown?: string;
  barcode?: string;
  disambiguation?: string;
  primary_type?: string;
  secondary_types?: string[];
}

/** A release group as the artist page lists it. */
export interface MBReleaseGroupRow {
  id: string;
  title: string;
  primary_type?: string;
  secondary_types?: string[];
  first_release_date?: string;
}

/** An artist page: identity + the release groups MusicBrainz holds. */
export interface MBArtistBrowse {
  id: string;
  name: string;
  disambiguation?: string;
  type?: string;
  country?: string;
  life_span?: string[];
  genres?: string[];
  tags?: string[];
  total?: number;
  offset?: number;
  /** Offset of the next page of the discography, or null at the end. */
  next?: number | null;
  release_groups: MBReleaseGroupRow[];
}

/** A release-group page: identity + its editions. */
export interface MBReleaseGroupBrowse {
  id: string;
  title: string;
  disambiguation?: string;
  artist?: string;
  artist_mbid?: string | null;
  primary_type?: string;
  secondary_types?: string[];
  genres?: string[];
  first_release_date?: string;
  /** every (country, date) the group's loaded editions were released in, each
   *  naming the edition that carries it */
  countries?: MBCountryEvent[];
  total?: number;
  offset?: number;
  /** Offset of the next page, or null at the end. */
  next?: number | null;
  releases: MBReleaseRow[];
}

/** One country a release was released in, with that release event's date — a
 *  release group is usually released in several at once, and the release and
 *  release-group pages show them all rather than MusicBrainz's first one.
 *
 *  `country` is MusicBrainz's own area name ("United States"), `code` its ISO
 *  3166-1 code ("US", "" for an area that carries none), `preferred` marks the
 *  configured `prefer_release_country`, and `release_id` names the edition the
 *  event came from (release-group pages: the group's own releases; a release
 *  carries its own events, so it has none). */
export interface MBCountryEvent {
  country: string;
  code: string;
  date: string;
  preferred?: boolean;
  release_id?: string;
}

/** One field of MusicBrainz's search index (`GET /api/mb/search/fields`) —
 *  the catalogue the search box's completion and its help are built from, so
 *  neither can offer a field the server would not send to the index. */
export interface MBSearchField {
  field: string;
  /** text | enum | date | number | boolean | id | code */
  kind: string;
  /** whether a value may be quoted: true for text/enum values, where quoting
   *  is what keeps a multi-word value one phrase. A quoted field is inserted
   *  as `field:""` with the caret between the quotes. */
  quotes: boolean;
  /** a value to insert after `field:` — the shape the index expects */
  example: string;
  /** one line: what MusicBrainz matches, in MusicBrainz's own words */
  meaning: string;
}

/** One Lucene form MusicBrainz's index accepts, as the box's help lists it. */
export interface MBSearchSyntax {
  form: string;
  meaning: string;
}

/** The search box's whole help: the fields of every entity kind MusicBrainz
 *  documents, plus the syntax to combine them. */
export interface MBSearchFieldHelp {
  fields: Record<string, MBSearchField[]>;
  syntax: MBSearchSyntax[];
}

/** One edition in the release-choice policy's ranking (server/release_choice.py).
 *  Every field here is MusicBrainz's own answer: `media` is the list of medium
 *  formats, `track_count` the number of tracks the release carries. */
export interface MBReleaseChoiceEdition {
  release_mbid: string;
  title: string;
  date?: string;
  country?: string;
  status?: string;
  media?: string[];
  track_count?: number;
  disambiguation?: string;
  /** 0..1, higher = better; candidates arrive sorted by it. */
  score: number;
  /** False when the policy would never download this edition on its own (an
   *  unofficial release beside an official one, or one short of the group).
   *  It is still listed — and can still be forced, which the reasons then say. */
  eligible?: boolean;
  /** Human sentences naming the facts that decided its rank. */
  reasons: string[];
}

/** The release group a choice was made for — the `track_count` is what
 *  completeness is measured against, and the type is the kind that was asked
 *  for (the caller's primary/secondary filter, or the group's own). */
export interface MBReleaseChoiceGroup {
  title: string;
  first_release_date?: string;
  primary_type?: string;
  secondary_types?: string[];
  track_count?: number;
}

/** The policy that ranked the candidates, as configured — the same knobs the
 *  auto-import path reads, plus `rules` in prose. */
export interface MBReleaseChoicePolicy {
  medium_order: string[];
  /** `prefer_release_country`; "" = no country preference. */
  preferred_country: string;
  prefer_original_edition: boolean;
  status_order: string[];
  rules: string[];
}

/** `GET /api/mb/release-choice` — which edition the download policy will
 *  fetch, why, and the ranked alternatives. `chosen` is null when nothing is
 *  eligible; `candidates` then still ranks what exists, with `eligible: false`. */
export interface MBReleaseChoicePayload {
  release_group_mbid: string;
  release_group: MBReleaseChoiceGroup;
  chosen: MBReleaseChoiceEdition | null;
  candidates: MBReleaseChoiceEdition[];
  policy: MBReleaseChoicePolicy;
}

/** A recording page: identity + the releases carrying it. */
export interface MBRecordingBrowse {
  id: string;
  title: string;
  disambiguation?: string;
  artist?: string;
  artist_mbid?: string | null;
  length?: number | null;
  genres?: string[];
  isrcs?: string[];
  total?: number;
  offset?: number;
  /** Offset of the next page, or null at the end. */
  next?: number | null;
  releases: MBReleaseRow[];
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

/** A release's own identity — the ONE block every surface that knows a
 *  release carries (server/wishes.py RELEASE_KEYS): the two facts that
 *  identify a PRESSING first (its catalog number, the medium it is on), then
 *  where and when it came out and how much it carries, then the edition's own
 *  disambiguation and the status MusicBrainz gives it (Official / Promotion /
 *  Bootleg / …).
 *
 *  Every key is present on the server's rows; a fact nobody could resolve is
 *  empty (or 0) and renders as absent — never invented, never a placeholder
 *  that looks like data. Optional here only because a client may be reading a
 *  payload from an older server. */
export interface SlskReleaseIdentity {
  id: string;
  title: string;
  artist: string;
  date: string;
  /** MusicBrainz's FIRST release event — the singular code `country` has
   *  always been. `countries` below is the release's whole event set. */
  country: string;
  /** Every country the release came out in, in MusicBrainz's own event order
   *  (first = `country`). It is what an import writes to RELEASECOUNTRY as a
   *  "; "-joined list — a release out in several countries is not one that
   *  came out in the first of them. Optional: a payload from a server or a job
   *  summary predating the field states only the singular one. */
  countries?: string[];
  status: string;
  media: string[];
  track_count: number;
  disambiguation: string;
  catalog_number: string;
  label: string;
}

export interface Wish {
  id: number;
  release_mbid: string;
  title: string;
  artist: string;
  year: string;
  /** `not_found` is terminal: the searches came back empty
   *  `wishes_not_found_attempts` times, so the worker stops searching it and
   *  the row waits for the user's own retry (server/wishes' retry policy). */
  status: "wanted" | "searching" | "imported" | "failed" | "available" | "not_found";
  note: string;
  target_dir: string;
  queries: string[];
  attempts: number;
  /** Empty searches so far, and when the next AUTOMATIC one may run (0 = none
   *  will: a terminal row is re-armed only by the queue's retry). */
  not_found: number;
  retry_at: number;
  added_at: number;
  updated_at: number;
  last_search: number;
  last_error: string;
  album_path: string;
  /** The store's own verdict: the worker will never search this wish again on
   *  its own (imported, nothing was found, or failed for good — see
   *  server/wishes' retry policy). This is what makes it safe to take off the
   *  list; a wish that is still wanted or being searched is not terminal, and
   *  clearing it is refused. */
  terminal?: boolean;
  /** WHICH pressing this wish is waiting for (server/wishes' release
   *  identity). Present on every row the server builds; empty facts mean the
   *  release was never looked up (or MusicBrainz could not answer). */
  release?: SlskReleaseIdentity;
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
  /** Every album added but not downloaded yet, newest first — the one shelf a
   *  user can read to see everything still waiting. */
  pending?: HomeAlbum[];
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
  /** A FRAMEWORK album on a shelf: added to the library before its audio
   *  arrived. Present (true) only while it waits — a complete album carries
   *  none of these keys, so `pending` is the whole test a card needs. The
   *  block is the library row's own (`server/recommendations._owned_row`),
   *  so a Home card says exactly what the album's library row says. */
  pending?: boolean;
  pending_reason?: string;
  wish_id?: number | null;
  wish?: AlbumWish | null;
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
  /** The numbers the check judged ("1600x1600: longest side 1600px, 400px over
   *  the configured 1200px target"), for the chip's tooltip. */
  reason?: string;
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
  /** Informational only — an artist image below the configured target size is
   *  accepted, so it is reported here and never fails the check. */
  notes?: ArtistGradeIssue[];
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

/** The transliteration/translation pass over a selection (POST
 *  /api/lyrics/xlit) — script 17's own runner, reporting its own numbers:
 *  `ok` files it modified, `skipped` it left alone, `failed` its error lines.
 *  `note` is why nothing was written when it had nothing to work with (both
 *  switches off in Settings → Lyrics, or no AI configured). */
export interface LyricsXlitResult {
  stats: Record<string, number> | null;
  paths: string[];
  ok: number;
  skipped: number;
  failed: number;
  errors: string[];
  note: string;
}

/** One track of the LRCLIB publish (POST /api/lyrics/publish-batch) — script
 *  18's per-track core. Nothing is written locally: `status` is "ok" for a
 *  submission LRCLIB accepted, "skipped" for a no-op (`reason` says which:
 *  "LRCLIB already has it", "no lyrics stored", "instrumental", …) and
 *  "failed" for a refusal or an error, `message` carrying LRCLIB's own words. */
export interface LyricsPublishResult {
  path: string;
  status: "ok" | "skipped" | "failed";
  reason: string;
  message: string;
  synced: boolean;
}

export interface LyricsPublishBatchResult {
  results: LyricsPublishResult[];
  ok: number;
  skipped: number;
  failed: number;
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
  /** This album's own verdict: "matched", "no_match", "skipped" (nothing
   *  fingerprintable) or "error" (the fingerprint or the lookup failed). A
   *  failed lookup is never reported as "no match". */
  status?: "matched" | "no_match" | "skipped" | "error" | string;
  /** Machine code behind the verdict (mlo.acoustid's code vocabulary). */
  code?: string | null;
  /** The sentence to show for the verdict, why it is not "matched". */
  reason?: string | null;
  /** Tag-vs-fingerprint disagreement, when the album's own tags claim another
   *  release group. Nothing is overwritten on the strength of it. */
  conflict?: boolean | null;
  conflicts?: AcoustidConflict[];
  /** Tracks that could not be fingerprinted, and ones whose lookup failed. */
  skips?: AcoustidTrackProblem[];
  failures?: AcoustidTrackProblem[];
  /** Every track's own tag-write outcome, present when the request applied the
   *  match (mlo.acoustid.write_tags). `tagged` alone was the silent lie: an
   *  album of .wv files is a real match nothing can be written to, and "0
   *  tagged" said the files carried none of the tag families instead. */
  writes?: AcoustidWrite[];
}

/** One track's identity-tag write (`writes` above).
 *
 *  `code` is the write vocabulary (unsupported_container / unreadable_file /
 *  no_recording_id / no_fingerprint / write_failed / verify_failed), `reason`
 *  is the sentence the server wrote for it (the unsupported one names the
 *  extension), and `output` names the file that now holds the pair when a
 *  video container was remuxed (which can change the extension). */
export interface AcoustidWrite {
  path: string;
  ok: boolean;
  code?: string | null;
  reason?: string | null;
  output?: string | null;
}

/** POST /api/import/acoustid/submit — the AcoustID database's own answer to
 *  publishing what the files already carry (nothing is written locally).
 *
 *  `available: false` is a refusal: no user key configured, or the key
 *  AcoustID refused, in the service's own words (`note`, `code`). `submitted`
 *  counts what it accepted (each with its submission id and status), `skips`
 *  names the files there was nothing to submit for, and `tracks` accounts for
 *  every file the paths resolved to. */
export interface AcoustidSubmitResult {
  available: boolean;
  note: string;
  ok: boolean;
  code?: string | null;
  submitted: number;
  failed: number;
  skips: AcoustidTrackProblem[];
  submissions: {
    path: string;
    index: number;
    id?: string | null;
    status?: string | null;
  }[];
  tracks: { total: number; submitted: number; skipped: number };
}

/** One tag-vs-fingerprint disagreement (`conflicts`). */
export interface AcoustidConflict {
  kind: "release_group" | "title" | "artist" | string;
  reason: string;
  fingerprint?: string | string[] | null;
  tags?: string | string[] | null;
}

/** One track behind a `skips` / `failures` count. */
export interface AcoustidTrackProblem {
  path: string;
  code?: string | null;
  reason?: string | null;
}

export interface AcoustidMatch {
  /** False when no API key is configured or fpcalc is not installed. */
  available: boolean;
  /** Human reason when unavailable ("no API key", "fpcalc not installed"), or
   *  the first failed album's sentence when every album was checked. */
  note: string;
  /** False when any album's lookup failed — a per-album `status` says which. */
  ok?: boolean;
  /** Machine code behind `ok` (mlo.acoustid's OK when everything worked). */
  code?: string | null;
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

/** One family an import could not finish by itself — the wizard's own steps
 *  are the families (mlo/import_policy.FAMILIES owns the list and the order).
 *  `state` is "decision" when the pipeline left it to the user and "unsourced"
 *  when every configured source was asked and none could supply it. */
export interface ImportFamilyGap {
  id: string;
  label: string;
  step: string;
  state: "decision" | "unsourced" | string;
  fields: string[];
  codes: string[];
  note?: string;
}

/** The autonomy block of a finished import (server/imports.finish_album):
 *  `stopped` is the family a review handed the album over at, `missing` is
 *  what the grader still fails it for, and `prompt` is the entry raised for
 *  the user when there was anything to report. */
export interface ImportAutonomy {
  mode: "automatic" | "review" | string;
  stopped: string | null;
  missing: Record<string, ImportFamilyGap>;
  prompt: ImportPrompt | null;
}

/** One album waiting on the user (GET /api/import/prompts). `link` is the
 *  wizard URL that lands on the album at the first step needing a decision. */
export interface ImportPrompt {
  id?: string;
  album: string;
  album_name: string;
  at: number;
  mode: string;
  reason: "missing" | "stopped" | string;
  link: string;
  families: ImportFamilyGap[];
}

/** One album POST /api/library/add created (or found already there): the
 *  framework album on disk. `created` is false for a folder that was already
 *  a real album; `wish_id` is the queue entry searching for its audio. */
export interface LibraryAddAlbum {
  album_path: string;
  title: string;
  artist: string;
  year: string;
  release_id: string;
  release_group_id: string;
  wish_id: number | null;
  cover?: string | null;
  created: boolean;
  already_in_library: boolean;
}

/** The "Add to library" answer. `background` is true for an artist's
 *  discography, which is prepared off-request (the albums appear as they are
 *  created and are announced on the event channel). `queued` is how many
 *  release groups the call is queueing — stated by a type-filtered artist
 *  handover, which knows it from the group list it just read, and null when
 *  the call did not have to browse. */
export interface LibraryAddResult {
  ok: boolean;
  background?: boolean;
  queued?: number | null;
  note?: string;
  albums: LibraryAddAlbum[];
  skipped: { mbid?: string; reason?: string }[];
  errors: { mbid?: string; reason?: string }[];
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
export type SourceKind =
  | "lyrics"
  | "advisory"
  | "genre"
  | "metadata"
  | "links"
  | "discover"
  | "credentials";

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

