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
  /** The artist's verdict by the albums' own rule (failed checks == 0).
   *  Not derivable from grade_pct, which is rounded. */
  pass?: boolean;
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
  /** The ORIGINAL release date (ID3 TORY / Vorbis ORIGINALDATE / MP4
   *  originaldate): a remaster keeps the year the work was first released, so
   *  album rows show THIS in preference to `DATE` (lib/fmt.originalYear).
   *  Written only when the tagger set it — an untagged file falls back to
   *  `DATE`. */
  ORIGINALDATE?: string | null;
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
  /** WHICH KIND the track's stored lyrics are: "synced" when the stored text
   *  carries timestamps (either source — a timed `.lrc` beside plain embedded
   *  lyrics IS synced), "plain" when it holds lyrics without any, null when
   *  the track has none. Stamped server-side from the two stored texts
   *  (mlo.lyrics.stored_lyrics_kind), so `lyrics_present` is exactly
   *  `lyrics_kind !== null` — the two can never disagree. */
  lyrics_kind?: "synced" | "plain" | null;
  unreadable: boolean;
  tech: Tech;
  tags: TrackTags;
  grade_pass: boolean;
  lyrics_present: boolean;
  /** MusicBrainz's disambiguation comment for this recording, or null when it
   *  states none — rendered in parentheses after the title in a dimmer tone
   *  (`DisambiguationMark`), the way MusicBrainz shows "title (comment)".
   *  Read server-side from the TITLEDISAMBIGUATION tag by
   *  `server.library._enrich_track`. */
  disambiguation?: string | null;
  cover_file?: string | null;
  sidecar_cover?: boolean;
  sidecar_cover_file?: string | null;
  /** Music-video container (MKV/MP4/VOB/…). */
  is_video?: boolean;
}

/** What an import could not supply for an album, if anything — one shape for
 *  every surface that carries it (the album page's banner, the queue's
 *  finished row, the wizard's own prompt banner; `server.import_autonomy
 *  .warning`). `waiting` is the one distinction that matters to a reader: a
 *  `"stopped"` import (review mode) really is holding the album for an answer
 *  and its chain has not run, while everything else is a warning on an album
 *  that already landed — nothing about it is held. `link` opens the wizard at
 *  the album and at the step that decides the first missing family. */
export interface NeedsWarning {
  families: string[];
  labels: string[];
  link: string;
  detail: string;
  reason: string;
  mode: string;
  waiting: boolean;
}

export interface Album {
  path: string;
  error?: string;
  meta?: AlbumMeta;
  /** Set when an import could not supply a family for this album (see
   *  `NeedsWarning`). The album is IN the library either way. */
  needs?: NeedsWarning;
  album_artist?: string | null;
  /** MusicBrainz's disambiguation comment for the release GROUP, or null —
   *  rendered in parentheses after the album name ("1967–1970 (The Blue
   *  Album)"), dimmer than the name itself. Read from the album's
   *  ALBUMDISAMBIGUATION tag by `server.library.build_album`. */
  disambiguation?: string | null;
  /** MusicBrainz's disambiguation comment for the credited artist, or null —
   *  the artist caption's own parentheses ("The Beatles (UK rock band)"),
   *  read from the album's ARTISTDISAMBIGUATION tag. Album-level: every file
   *  of the release carries it. */
  artist_disambiguation?: string | null;
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
    /** Informational lines from the same grading pass: what could NOT be
   *  checked (an AccurateRip verdict the database has no entry for). They
   *  never fail the album — `issues` is the failing half. */
  notes?: string[];
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
  /** What the album is waiting for (the server's own sentence). */
  pending_reason?: string;
  /** What "Add to library" already fetched for this folder before its audio
   *  existed: the cover candidates it ranked and the winner it picked, plus
   *  the links it resolved. */
  prefetched?: AlbumPrefetch | null;
}

/** What "Add to library" pre-fetched for a folder whose audio has not arrived
 *  (`server/imports.prefetch_album`, recorded in the framework marker). */
export interface AlbumPrefetch {
  at: number;
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

/** One place the music folder does not match
 *  `<music>/Artists/<Artist>/<Album>/<files>`. */
export interface LayoutIssue {
  /** What is wrong: audio_at_root, audio_in_artists, audio_in_artist,
   *  unexpected_folder, unexpected_subfolder, empty_album, stray_file,
   *  stray_in_artists, hidden_folder, legacy_state_file, wrong_case,
   *  sidecar_copy (a numbered copy like `description (2).txt` beside the
   *  album's own description). */
  kind: string;
  /** Music-folder-relative path, for display. */
  path: string;
  /** Absolute path, for "open folder" style actions. */
  abs: string;
  detail: string;
  hint: string;
  /** What Apply fixes would do about this row (absent = nothing may act on
   *  it, so the row is a report and nothing else). */
  fix?: { action: "rename" | "move" | "trash"; to?: string };
}

/** What the apply phase did about one row — or why it left it alone. */
export interface LayoutFix {
  kind: string;
  path: string;
  result: "fixed" | "failed" | "skipped";
  /** The outcome in words, e.g. `renamed Artists/lower to "Lower"`. */
  action: string;
}

export interface LayoutReport {
  folder: string;
  artists_dir: string;
  exists: boolean;
  /** What is STILL wrong. A run that fixed something drops the rows it fixed,
   *  so this always describes the library as it is now. */
  issues: LayoutIssue[];
  /** issue kind → count. */
  counts: Record<string, number>;
  total: number;
  albums: number;
  artists: number;
  audio_files: number;
  /** Only after an apply (script 20 with layout_apply on, or the panel's
   *  Apply fixes): every row the run acted on, or decided not to. */
  fixes?: LayoutFix[];
  fixed?: number;
  fix_failed?: number;
  skipped?: number;
}

/** The report the LAST layout scan stored under `<music>/.mlo/data` — what
 *  the Library page warns from, so the warning costs no walk of the library.
 *
 *  `exists` is false until a scan has run (`report`/`scanned_at` are then
 *  null): a warning must never stand in for a scan that did not happen.
 *  `scanned_at` is when it ran (UTC, ISO); `music_folder` is the folder it
 *  looked at, and `stale` says that folder is NOT the one configured now, so
 *  nothing may be claimed from the report. Age alone is not staleness — see
 *  mlo/layout.py: only a fresh walk could tell whether the folder changed. */
export interface LayoutSnapshot {
  exists: boolean;
  scanned_at: string | null;
  music_folder: string | null;
  stale: boolean;
  report: LayoutReport | null;
}

/** `/api/grades/summary` — whether the library passes its grading checks, and
 *  what fails (server.recommendations.grade_warning). The Home payload carries
 *  the SAME object as `grade_warning`, so the strip on Home and the one on the
 *  Library page are one answer rather than two counts of one library.
 *
 *  `ok` is the grader's own per-album rule (failed checks == 0) read over the
 *  whole library, so it agrees with `grade_pct` by construction; that
 *  percentage is null when no check ran at all. A pending framework album and
 *  an album with zero checks are never findings — nothing was graded either
 *  way — so `albums_failing` counts the albums that really failed, one entry
 *  of `items` each. An album an import is running over right now is not one of
 *  them (its chain is filling the very tags the grader read as missing) and is
 *  counted apart, in `albums_importing`, so the strip can say where such an
 *  album went instead of looking like it lost one. */
export interface GradeWarning {
  ok: boolean;
  pass_count: number;
  total_checks: number;
  grade_pct: number | null;
  albums_failing: number;
  /** Failing albums an import holds right now: left out of the counts above
   *  (and of `items`), and named in one clause so the strip does not read as
   *  an album short. */
  albums_importing: number;
  tracks_failing: number;
  /** Worst first (lowest grade, then the most failing tracks), capped at 12;
   *  whatever did not fit is `more`. */
  items: GradeWarningItem[];
  /** Findings past that cap — the strip links this to the Library's own
   *  Failing filter rather than printing a hundred rows. */
  more: number;
}

/** One failing album, or one failing track inside an album that otherwise
 *  passes. The owner's rule for which: ONE failing track in an album is shown
 *  AS THAT TRACK (the album is only the frame around it), two or more as THE
 *  ALBUM — the row then carries `failing_tracks` and the union of their codes
 *  instead of a dozen rows of the same album. */
export interface GradeWarningItem {
  kind: "track" | "album";
  album_path: string;
  /** The failing file — present exactly on a `track` item. */
  track_path?: string;
  artist: string;
  album: string;
  /** The track's own name (its TITLE tag, the file name failing that) —
   *  present with `track_path`. */
  title?: string;
  /** How many of the album's tracks fail — present on an `album` item, and 0
   *  when the album failed a check of its own rather than its files'. */
  failing_tracks?: number;
  /** The grader's BARE codes — GENRE_MISSING, COVER, CRC_MISMATCH and the
   *  like; the union of the failing tracks' own, on an album item. */
  codes: string[];
  /** The grader's own sentence for an album-level failure ("Missing cover
   *  image", "Missing .log file", …); absent when a file failed instead. */
  reason?: string;
  /** EVERY album-wide failing check's own sentence, in the grader's order —
   *  the row prints `reason` (the first) and its tooltip names the rest, so a
   *  check that is not the first sentence still reaches the reader. */
  reasons?: string[];
  /** The album's grade (what the worst-first order sorts on). */
  grade_pct: number | null;
}

/** `GET /api/log/report` — one rip log read in full: Logchecker's report, this
 *  app's verdict on its checksum, and the log's own text (mlo.discs
 *  .log_report). Read-only: nothing is scored into the tags or renamed.
 *
 *  `available` is false when no Logchecker is installed — `report` is then
 *  empty and MUST NOT be read as a score of zero (a missing scorer is not a
 *  bad rip); `text` still holds the log, which is what a reader falls back to.
 *  `checksum.state` is this app's verdict on the log's checksum line — "ok",
 *  "invalid", "missing", "unsupported", "unverified", or null when the log
 *  states none — with `detail` saying which line or helper decided it. `text`
 *  is the log's decoded bytes, capped at `report`'s own limit; `truncated`
 *  says the file was longer. */
export interface LogReportPayload {
  path: string;
  name: string;
  exists: boolean;
  available: boolean;
  bytes: number;
  truncated: boolean;
  report: {
    ripper: string;
    version: string;
    language: string;
    score: number | null;
    checksum: string;
    details: string[];
    raw: string;
  };
  checksum: { state: string | null; detail: string | null };
  text: string;
  /** The `.log` files in the folder that was asked for, when the caller named
   *  a FOLDER — empty for a path that was already one log. The route reports
   *  the list either way, so a viewer can switch discs without asking again. */
  siblings: string[];
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
  /** MusicBrainz's disambiguation comment for this artist, or null when none
   *  of the artist's albums states one — rendered in parentheses after the
   *  name (`DisambiguationMark`). Read from the artist's own albums by
   *  `server.library._artist_disambiguation`, the same rule for the artist
   *  page (`/api/artist`), the Library's artist rows and Home's shelf. */
  disambiguation?: string | null;
  albums: Album[];
  aggregate: Aggregate;
  /** Artist-level grading: only the checks that apply to an artist folder.
   *  The artist page computes it for the folder it opened, and every
   *  `/api/library` row carries its own (`server.library`), so the dot beside
   *  a name in a list is the verdict beside the same name on the page it
   *  opens — one rule, one payload field. */
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

export interface Progress {
  done: number;
  total: number;
  desc: string;
}

export interface MBPerson {
  name: string;
  mbid?: string;
}

export interface MBTrack {
  position: number;
  disc: number;
  title: string;
  /** The title in the reader's locale (`locale`), when MusicBrainz states one
   *  — rendered in parentheses beside the title. */
  alias?: string;
  length: number | null;
  recording_mbid: string | null;
  artist_mbids: string[];
  artist_credit: string;
  genres: string[];
}

export interface MBRelease {
  id: string;
  /** The title in the reader's locale (`locale`), when MusicBrainz states
   *  one — rendered in parentheses beside the title. */
  alias?: string;
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
  /** The name/title in the reader's locale (`locale`), when the payload states
   *  one — the search index carries aliases for ARTISTS only, so those rows
   *  show it and the rest leave it out. */
  alias?: string;
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
  /** The name/title in the reader's locale (`locale`), when MusicBrainz
   *  states one — rendered in parentheses beside the title. */
  alias?: string;
  date?: string;
  country?: string;
  status?: string;
  formats?: string;
  disc_count?: number;
  track_count?: number;
  /** per-disc track counts ("10 + 11") for a multi-disc edition */
  track_breakdown?: string;
  barcode?: string;
  /** Every catalog number MusicBrainz states for this edition, in its own
   *  order — one per label, so a pressing released by two labels carries two.
   *  Read off the browse's `label-info` by `mlo.release_choice.catalog_numbers`,
   *  the same reader the release-choice ranking fills a candidate from, so the
   *  group page's rows and the ranking can never disagree about which number an
   *  edition carries. */
  catalog_numbers?: string[];
  /** The first of `catalog_numbers`, kept as its own key for the payloads that
   *  only state one (a search hit, a release lookup). */
  catalog_number?: string;
  disambiguation?: string;
  primary_type?: string;
  secondary_types?: string[];
}

/** A release group as the artist page lists it. */
export interface MBReleaseGroupRow {
  id: string;
  title: string;
  /** See MBReleaseRow.alias. */
  alias?: string;
  primary_type?: string;
  secondary_types?: string[];
  first_release_date?: string;
}

/** An artist page: identity + the release groups MusicBrainz holds. */
export interface MBArtistBrowse {
  id: string;
  name: string;
  /** See MBReleaseRow.alias. */
  alias?: string;
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
  /** See MBReleaseRow.alias. */
  alias?: string;
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

export interface ArtistGradeIssue {
  code: string;
  label: string;
  where?: string;
  /** The numbers the check judged ("1600x1600: longest side 1600px, 400px over
   *  the configured 1200px target"), for the chip's tooltip. */
  reason?: string;
}

/** Artist-level grading: only what applies to an artist folder (that it
 *  holds albums at all), never the album checks. */
export interface ArtistGrade {
  path?: string;
  checks?: number;
  pass_count?: number;
  failed_checks?: number;
  pct?: number | null;
  pass?: boolean;
  issues?: ArtistGradeIssue[];
  /** Informational only — an item the check reported without failing. */
  notes?: ArtistGradeIssue[];
  error?: string;
}

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
 *  publishing what the files state (nothing is written locally).
 *
 *  `available: false` is a refusal: no user key configured, the key AcoustID
 *  refused, or the feature switched off — in the service's own words (`note`,
 *  `code`), and nothing was read or sent. `submitted` counts what the service
 *  accepted (each with its submission id and status), `known` the pairs it
 *  already linked (or this app had already sent), `skips` names the files
 *  there was nothing to submit for, and `tracks` accounts for every file the
 *  paths resolved to.
 *
 *  `results` is the per-track report, one row per file in the order they were
 *  resolved: `outcome` is "accepted" (the service took it), "already_known"
 *  (its `reason` says which half of the dedupe said so), "rejected" (the
 *  service's own sentence) or "skipped" (a named cause: no MusicBrainz
 *  recording id on the file, no fpcalc, no duration, or a question that could
 *  not be asked). */
export interface AcoustidSubmitResult {
  available: boolean;
  note: string;
  ok: boolean;
  code?: string | null;
  submitted: number;
  /** Pairs AcoustID already had, or that this app had already given it. */
  known: number;
  failed: number;
  skips: AcoustidTrackProblem[];
  submissions: {
    path: string;
    index: number;
    id?: string | null;
    status?: string | null;
  }[];
  results: AcoustidSubmitTrack[];
  tracks: { total: number; submitted: number; known: number; skipped: number; failed: number };
}

/** One track of a submission run (`AcoustidSubmitResult.results`). */
export interface AcoustidSubmitTrack {
  path: string;
  outcome: "accepted" | "already_known" | "rejected" | "skipped" | string;
  code?: string | null;
  reason?: string | null;
  /** The MusicBrainz recording this fingerprint was submitted with. */
  recording_id?: string | null;
  id?: number | null;
  status?: string | null;
  index?: number | null;
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

/** An archive the server unpacked for an import, before anything is committed
 *  (`POST /api/import/unpack`).
 *
 *  `files[].relPath` is the path INSIDE the archive (so a rip's CD1/ folder,
 *  its .cue and its .log keep their places), and `files[].path` is where the
 *  server staged it — what the commit passes back as `staged`. `audio` is the
 *  server's own count over that tree: 0 means the archive holds no music, and
 *  the wizard says so instead of offering an empty album. */
export interface UnpackedTree {
  ok: boolean;
  /** The archive's own name, for the "what was unpacked" line. */
  label: string;
  /** The staging folder; discarded once the import finishes with it. */
  dir: string;
  files: { relPath: string; path: string; size: number }[];
  unpacked: number;
  audio: number;
}

export interface ImportBulkJob {
  id?: string;
  kind?: string;
  status?: "idle" | "running" | "done" | "failed" | string;
  started?: number;
  finished?: number;
  total?: number;
  done?: number;
  /** Rows in the "running" state right now (imported concurrently). */
  running?: number;
  /** Rows still waiting for a worker — the queue behind `concurrency`. */
  queued?: number;
  /** Albums imported at once for the whole app (`import_bulk_concurrency`):
   *  every batch shares it, so this is the queue's width, not each batch's. */
  concurrency?: number;
  label?: string;
  items?: ImportBulkItem[];
  error?: string;
  /** What else is running (the server's registry): every live batch, plus the
   *  one this payload is about. A batch of albums started here does not block
   *  one started elsewhere, so a surface can say so. */
  jobs?: { id: string; status: string; total: number; done: number;
           label: string; started: number }[];
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

/** One half of the digital settle (POST /api/import/settle): the SOURCE the
 *  import could state, written / "present" / "suggested" / "asked" /
 *  "not-digital" / "gated" (the user's own per-filetype write gate) / "failed",
 *  with the value, where it came from and the config's own default. */
export interface ImportSourceResult {
  state: string;
  value: string;
  from: string;
  default: string;
  media: string;
  tracks: number;
  missing: number;
  written: number;
  failed: number;
}

/** The lyrics half: `state` "cleaned" (with `dropped` files), "ok",
 *  "allow-plain" (untimed lyrics are this install's own answer), "no-fetch"
 *  (script 13 is not in the chain — nothing was touched) or "failed".
 *  `unformatted` is how many of `dropped` were timed but not in the form the
 *  grade asks for, `empty` is how many files hold no lyric at all, `formatted`
 *  is how many arrived lyrics script 1's own pass had to canonicalize, and
 *  `message` is the import's own sentence about what it removed ("" when it
 *  removed nothing — a caller shows it rather than composing its own). */
export interface ImportLyricsSettle {
  state: string;
  checked: number;
  dropped: number;
  unformatted: number;
  empty: number;
  formatted: number;
  kept: number;
  failed: number;
  tracks: string[];
  message: string;
  allow_plain: boolean;
  fetch: boolean;
}

/** What `POST /api/import/settle` answers — the wizard's Finish step shows it
 *  before it runs the ticked scripts. */
export interface ImportSettleResult {
  path: string;
  source: ImportSourceResult;
  lyrics: ImportLyricsSettle;
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

/** A manual import the user left unfinished (GET /api/import/sessions): the
 *  wizard's persisted bookmark. `step` is the numeric wizard step it was left
 *  on; the tray's "Continue import" links back to `/import?album=&step=`. */
export interface ImportSession {
  album: string;
  album_name: string;
  step: number;
  staged: boolean;
  at: number | null;
}

/** One album POST /api/library/add created (or found already there): the
 *  framework album on disk. `created` is false for a folder that was already
 *  a real album. */
export interface LibraryAddAlbum {
  album_path: string;
  title: string;
  artist: string;
  year: string;
  release_id: string;
  release_group_id: string;
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

/** The entity a details menu is mounted on — what its selection IS, which is
 *  what decides the scripts it may offer (`server/script_menu.py`). A track row
 *  holds FILES; an album, an artist and the library hold FOLDERs. */
export type EntityKind = "album" | "track" | "artist" | "library";

/** One script of `/api/script-menu`.
 *
 *  `scope` is what the runner is handed for it: its work unit is a FILE (hand
 *  it the selection's own paths) or the FOLDER that holds them (its sidecars,
 *  its cover art, a per-album measurement). `applies_to` is the kinds whose
 *  menus may offer it, derived from `scope`; both come from the runner's own
 *  code, never from the menu. */
export interface ScriptMenuScript {
  id: number;
  label: string;
  description: string;
  group: string;
  /** Slot in the stack's Run All order; null when the script holds none. */
  order: number | null;
  in_order: boolean;
  scope: "file" | "folder" | null;
  applies_to: EntityKind[];
  /** The force options a forced re-run of this script may send through
   *  /api/run — ONE PER FLAG it owns (10 re-runs what the flags above it force,
   *  so it carries four). `owner`/`owner_label` name the pass a flag re-runs,
   *  which is what makes "10 · Format all — 9 · AccurateRip" mean something. */
  force: {
    keys: string[];
    options: { key: string | null; config: string; owner: number | null; owner_label: string }[];
  };
  /** The feature switch that makes the run skip it — `reason` is the run's own
   *  sentence, so the menu and the report say the same thing. */
  gate: { keys: string[]; enabled: boolean; reason: string };
  /** False when this install has no such runner (a stripped checkout). */
  available: boolean;
}

/** Every script, for every entity kind — the details menu's one source of
 *  truth, so no menu keeps its own list of ids. */
export interface ScriptMenu {
  kinds: EntityKind[];
  groups: { id: string; title: string }[];
  /** The section the forced re-runs go in — a variant of the entries above,
   *  not a script group of its own. */
  forced_group: { id: string; title: string };
  /** What a "run everything that applies" press posts, per entity kind: the
   *  chain's own order, scoped to the entity, WITHOUT the opt-in scripts
   *  (`excluded`) — one is a public, outward-facing submission and must never
   *  be swept up by a menu button. */
  run_all: {
    order: number[];
    by_kind: Record<string, number[]>;
    excluded: { id: number; label: string; why: string }[];
  };
  /** In the stack's order. */
  scripts: ScriptMenuScript[];
  /** Registry ids with no applicability entry: offered everywhere and named
   *  here, because a menu that silently dropped one is the drift this payload
   *  exists to stop (`tools/test_script_menu.py` fails on a non-empty list). */
  unclassified: number[];
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
   *  the lyrics rows and the genre rows carry these (a genre row's `rank` is
   *  its position in the priority chain the tray saves). */
  rank?: number;
  notes?: string;
  /** One line saying what this source provides the app — on every row of
   *  every kind (the wizard's Keys step renders it under each name; the genre
   *  tray reads it as the source's one-line description). */
  provides?: string;
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

