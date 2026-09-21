/// The wire shapes the server sends, mirroring `web/src/types.ts`.
///
/// Only the fields the client renders are modelled, and every one is read
/// defensively: the server may add keys freely, and a field the client does
/// not know about must never break a page. Where a value can be absent
/// (`null`), the Dart type says so instead of a sentinel.
library;

int? _int(dynamic v) {
  if (v == null) return null;
  if (v is int) return v;
  if (v is num) return v.toInt();
  final parsed = int.tryParse(v.toString());
  return parsed;
}

double? _double(dynamic v) {
  if (v == null) return null;
  if (v is num) return v.toDouble();
  return double.tryParse(v.toString());
}

String? _str(dynamic v) {
  if (v == null) return null;
  final s = v.toString();
  return s.isEmpty ? null : s;
}

Map<String, String> _stringMap(dynamic v) {
  if (v is! Map) return const {};
  final out = <String, String>{};
  v.forEach((key, value) {
    if (value == null) return;
    out[key.toString()] = value.toString();
  });
  return out;
}

/// One audio file of an album: the tags the app reads, the technical facts the
/// player shows beside the title, and the two verdicts (grade, audit) that
/// decide the row's marks.
class Track {
  Track({
    required this.path,
    required this.file,
    this.title,
    this.tracknumber,
    this.discnumber,
    this.advisory,
    this.instrumental,
    this.artist,
    this.album,
    this.genre,
    this.isrc,
    this.audit,
    this.gradePass = false,
    this.isVideo = false,
    this.unreadable = false,
    this.lyricsPresent = false,
    this.coverFile,
    this.tags = const {},
    this.tech = const {},
  });

  final String path;
  final String file;
  final String? title;
  final int? tracknumber;
  final int? discnumber;

  /// ITUNESADVISORY as stored ("1" explicit, "2" clean edition) — the E/C mark.
  final String? advisory;

  /// INSTRUMENTAL ("1" = the track has no words).
  final String? instrumental;
  final String? artist;
  final String? album;
  final String? genre;
  final String? isrc;
  final String? audit;
  final bool gradePass;
  final bool isVideo;
  final bool unreadable;
  final bool lyricsPresent;
  final String? coverFile;
  final Map<String, String> tags;
  final Map<String, dynamic> tech;

  /// The name a row shows: the TITLE tag, never the file name while it exists.
  String get displayTitle => title ?? file.replaceAll(RegExp(r'\.[^.]+$'), '');

  String? get gainTag => tags['REPLAYGAIN_TRACK_GAIN'];

  factory Track.fromJson(Map<String, dynamic> json) {
    final tags = _stringMap(json['tags']);
    return Track(
      path: json['path']?.toString() ?? '',
      file: json['file']?.toString() ?? '',
      title: _str(tags['TITLE']),
      tracknumber: _int(json['tracknumber']) ?? _int(tags['TRACKNUMBER']),
      discnumber: _int(json['discnumber']) ?? _int(tags['DISCNUMBER']),
      advisory: _str(tags['ITUNESADVISORY']),
      instrumental: _str(tags['INSTRUMENTAL']),
      artist: _str(tags['ARTIST']),
      album: _str(tags['ALBUM']),
      genre: _str(tags['GENRE']),
      isrc: _str(tags['ISRC']),
      audit: _str(json['audit']),
      gradePass: json['grade_pass'] == true,
      isVideo: json['is_video'] == true,
      unreadable: json['unreadable'] == true,
      lyricsPresent: json['lyrics_present'] == true,
      coverFile: _str(json['cover_file']),
      tags: tags,
      tech: json['tech'] is Map
          ? Map<String, dynamic>.from(json['tech'] as Map)
          : const {},
    );
  }

  Map<String, dynamic> toQueueJson() => {
    'path': path,
    'file': file,
    'title': title,
    'advisory': advisory,
    'artist': artist,
    'album': album,
  };
}

/// The wish filling a framework album — `server/library.py::_wish_state_of`.
///
/// The queue row's own state travels in the album payload, so a surface can
/// say "searching — attempt 2, next try in 12 min" from the album it is
/// looking at instead of asking the queue again per row. Absent for a
/// complete album, and for a framework album whose wish the queue no longer
/// has: the marker then says nothing is searching for it, which is the truth.
class AlbumWish {
  AlbumWish({
    this.id,
    this.status = '',
    this.attempts = 0,
    this.dueIn,
    this.terminal = false,
    this.reason = '',
    this.note = '',
  });

  final int? id;
  /// The queue's own state name ("wanted", "searching", "not_found", …).
  final String status;
  /// Searches that have run; 0 means the queue recorded it but nothing has
  /// looked yet.
  final int attempts;
  /// Seconds until the next search, or null when none is coming.
  final int? dueIn;
  /// True once the queue will not try again — a failure it has given up on.
  final bool terminal;
  /// Why the last run left it, in the queue's own words.
  final String reason;
  final String note;

  factory AlbumWish.fromJson(Map<String, dynamic> json) => AlbumWish(
    id: _int(json['id']),
    status: _str(json['status']) ?? '',
    attempts: _int(json['attempts']) ?? 0,
    dueIn: _int(json['due_in']),
    terminal: json['terminal'] == true,
    reason: _str(json['reason']) ?? '',
    note: _str(json['note']) ?? '',
  );
}

/// One album folder: what the shelf shows (cover, year, grade) plus its
/// tracks, which the album page and the queue read.
class Album {
  Album({
    required this.path,
    required this.title,
    this.artist,
    this.year,
    this.date,
    this.coverFile,
    this.genre,
    this.media,
    this.gradePct,
    this.pass = false,
    this.auditSummary,
    this.trackCount = 0,
    this.advisory,
    this.tracks = const [],
    this.meta = const {},
    this.pending = false,
    this.pendingReason,
    this.wishId,
    this.wish,
    this.error,
  });

  final String path;
  final String title;
  final String? artist;
  final String? year;
  final String? date;
  final String? coverFile;
  final String? genre;
  final String? media;
  final double? gradePct;
  final bool pass;
  final String? auditSummary;
  final int trackCount;
  final String? advisory;
  final List<Track> tracks;
  final Map<String, String> meta;

  /// True for a FRAMEWORK album: added to the library, its audio not
  /// downloaded yet (`server.pending_albums`). Its folder is on disk before
  /// anything is in it, so every surface that lists albums has to say so
  /// instead of drawing an empty album.
  final bool pending;
  /// What it is waiting for ("a verified Soulseek download").
  final String? pendingReason;
  /// The wish searching for its audio — the queue row it belongs to.
  final int? wishId;
  /// That wish's own state; null when the queue has none for this album.
  final AlbumWish? wish;
  final String? error;

  String get folderName => path.split(RegExp(r'[\\/]')).last;

  factory Album.fromJson(Map<String, dynamic> json) {
    final meta = _stringMap(json['meta']);
    final rawTracks = json['tracks'];
    return Album(
      path: json['path']?.toString() ?? '',
      title:
          _str(meta['ALBUM']) ??
          (json['path']?.toString() ?? '').split(RegExp(r'[\\/]')).last,
      artist: _str(meta['ALBUMARTIST']) ?? _str(meta['ARTIST']),
      year: _str(meta['DATE']) ?? _str(meta['ORIGINALDATE']),
      date: _str(meta['DATE']),
      coverFile: _str(json['cover_file']),
      genre: _str(meta['GENRE']),
      media: _str(meta['MEDIA']),
      gradePct: _double(json['grade_pct']),
      pass: json['pass'] == true,
      auditSummary: _str(json['audit_summary']),
      trackCount: _int(json['track_count']) ?? 0,
      advisory:
          _str(meta['ITUNESADVISORY']) ?? _str(meta['ALBUMITUNESADVISORY']),
      tracks: rawTracks is List
          ? rawTracks
                .whereType<Map>()
                .map((t) => Track.fromJson(Map<String, dynamic>.from(t)))
                .toList()
          : const [],
      meta: meta,
      // The framework block, exactly as the server stamps it on every
      // album-shaped row: a complete album carries `pending: false` and
      // nothing else, and anything absent reads as "not pending".
      pending: json['pending'] == true,
      pendingReason: _str(json['pending_reason']),
      wishId: _int(json['wish_id']),
      wish: json['wish'] is Map
          ? AlbumWish.fromJson(Map<String, dynamic>.from(json['wish'] as Map))
          : null,
      error: _str(json['error']),
    );
  }
}

/// An artist folder and its albums.
class Artist {
  Artist({
    required this.path,
    required this.name,
    this.albums = const [],
    this.albumCount = 0,
    this.trackCount = 0,
  });

  final String path;
  final String name;
  final List<Album> albums;
  final int albumCount;
  final int trackCount;

  factory Artist.fromJson(Map<String, dynamic> json) {
    final raw = json['albums'];
    return Artist(
      path: json['path']?.toString() ?? '',
      name: _str(json['name']) ?? json['path']?.toString() ?? '',
      albums: raw is List
          ? raw
                .whereType<Map>()
                .map((a) => Album.fromJson(Map<String, dynamic>.from(a)))
                .toList()
          : const [],
      albumCount: _int(json['album_count']) ?? 0,
      trackCount: _int(json['track_count']) ?? 0,
    );
  }
}

/// The whole library: every artist, with their albums and tracks.
class Library {
  Library({required this.folder, this.artists = const [], this.error});

  final String folder;
  final List<Artist> artists;
  final String? error;

  int get trackCount => artists.fold(
    0,
    (sum, a) => sum + a.albums.fold(0, (s, al) => s + al.tracks.length),
  );

  int get albumCount => artists.fold(0, (sum, a) => sum + a.albums.length);

  factory Library.fromJson(Map<String, dynamic> json) {
    final raw = json['artists'];
    return Library(
      folder: json['folder']?.toString() ?? '',
      artists: raw is List
          ? raw
                .whereType<Map>()
                .map((a) => Artist.fromJson(Map<String, dynamic>.from(a)))
                .toList()
          : const [],
      error: _str(json['error']),
    );
  }
}

/// One playlist row (`GET /api/playlists`).
class Playlist {
  Playlist({
    required this.id,
    required this.name,
    this.kind = 'manual',
    this.count = 0,
    this.coverFile,
    this.tracks = const [],
  });

  final int id;
  final String name;
  final String kind;
  final int count;
  final String? coverFile;
  final List<String> tracks;

  bool get isSmart => kind == 'smart';

  factory Playlist.fromJson(Map<String, dynamic> json) {
    final raw = json['tracks'];
    return Playlist(
      id: _int(json['id']) ?? 0,
      name: json['name']?.toString() ?? '',
      kind: json['kind']?.toString() ?? 'manual',
      count:
          _int(json['track_count']) ??
          _int(json['count']) ??
          (raw is List ? raw.length : 0),
      coverFile: _str(json['cover_file']),
      tracks: raw is List ? raw.map((p) => p.toString()).toList() : const [],
    );
  }
}

/// A recommendation row (`GET /api/recommend`): an album the shelf renders as
/// a card, or a track it renders as a row, with the reason it was suggested.
class Recommendation {
  Recommendation({
    required this.kind,
    this.path,
    this.title,
    this.artist,
    this.album,
    this.coverFile,
    this.albumPath,
    this.reason,
    this.score,
    this.file,
    this.advisory,
  });

  final String kind; // "album" | "track"
  final String? path;
  final String? title;
  final String? artist;
  final String? album;
  final String? coverFile;
  final String? albumPath;
  final String? reason;
  final double? score;
  final String? file;
  final String? advisory;

  factory Recommendation.fromJson(Map<String, dynamic> json) => Recommendation(
    kind: json['kind']?.toString() ?? 'album',
    path: _str(json['path']),
    title: _str(json['title']) ?? _str(json['name']),
    artist: _str(json['artist']),
    album: _str(json['album']),
    coverFile: _str(json['cover_file']),
    albumPath: _str(json['album_path'] ?? json['path']),
    reason: _str(json['reason']),
    score: _double(json['score']),
    file: _str(json['file']),
    advisory: _str(json['advisory']),
  );
}

List<String> _strings(dynamic v) {
  if (v is! List) return const [];
  return [
    for (final value in v)
      if (value != null && value.toString().isNotEmpty) value.toString(),
  ];
}

/// One genre the discovery pages can browse (`GET /api/discover/genres`): the
/// counts each scope contributed and the providers that named it — `library`
/// whenever the user's own files carry the genre.
class DiscoverGenre {
  DiscoverGenre({
    required this.name,
    this.trackCount = 0,
    this.albumCount = 0,
    this.artistCount = 0,
    this.sources = const [],
  });

  final String name;
  final int trackCount;
  final int albumCount;
  final int artistCount;
  final List<String> sources;

  /// What the genre list sorts and counts on. An online-only genre has no
  /// tracks, so its albums and artists are all there is to count.
  int get weight => trackCount + albumCount + artistCount;

  bool get fromLibrary => sources.contains('library');

  /// "42 tracks · 7 albums · 3 artists", with the zero parts left out — a
  /// count the scope did not produce is not shown as a zero the client made up.
  String get counts => [
    if (trackCount > 0) '$trackCount track${trackCount == 1 ? '' : 's'}',
    if (albumCount > 0) '$albumCount album${albumCount == 1 ? '' : 's'}',
    if (artistCount > 0) '$artistCount artist${artistCount == 1 ? '' : 's'}',
  ].join(' · ');

  factory DiscoverGenre.fromJson(Map<String, dynamic> json) => DiscoverGenre(
    name: json['name']?.toString() ?? '',
    trackCount: _int(json['track_count']) ?? 0,
    albumCount: _int(json['album_count']) ?? 0,
    artistCount: _int(json['artist_count']) ?? 0,
    sources: _strings(json['sources']),
  );
}

/// One discovery row (`GET /api/discover/genre` and `/api/discover/recommended`):
/// what a provider suggests and what the library already has, in one shape.
/// `owned`/`inLibrary` say the library has it and `path` is set only then, so a
/// row either opens in the library or offers to add the release, never both.
class DiscoverItem {
  DiscoverItem({
    required this.kind,
    required this.title,
    this.artist,
    this.year,
    this.source,
    this.sourceLabel,
    this.coverUrl,
    this.pageUrl,
    this.mbid,
    this.releaseGroupMbid,
    this.path,
    this.owned = false,
    this.inLibrary = false,
    this.reason,
    this.alsoFrom = const [],
    this.tracks = const [],
  });

  final String kind; // "album" | "artist" | "track"
  final String title;
  final String? artist;
  final String? year;
  final String? source;
  final String? sourceLabel;
  final String? coverUrl;
  final String? pageUrl;
  final String? mbid;
  final String? releaseGroupMbid;

  /// The library path of the row's own album/artist/track — set only when the
  /// library has it.
  final String? path;
  final bool owned;
  final bool inLibrary;
  final String? reason;

  /// The other providers that had the same result. A row honest about its
  /// provenance is one the user can weigh.
  final List<String> alsoFrom;

  /// The album's track titles, when the provider gave them.
  final List<String> tracks;

  /// The provider named the way it names itself — a user must never be shown a
  /// bare internal id.
  String get sourceName => sourceLabel ?? source ?? '';

  /// The MBID an add is made with: the release group where the row has one
  /// (the app wishes for groups, not single editions), else the row's own id.
  String? get addMbid {
    final group = releaseGroupMbid;
    if (group != null && group.isNotEmpty) return group;
    final id = mbid;
    return (id != null && id.isNotEmpty) ? id : null;
  }

  /// The `kind` `POST /api/library/add` takes for this row.
  String get wishKind => switch (kind) {
    'artist' => 'artist',
    'track' => 'recording',
    // An album row without a release group is left for the server to identify
    // rather than guessed at into the wrong entity type.
    _ => releaseGroupMbid == null ? 'auto' : 'release_group',
  };

  factory DiscoverItem.fromJson(Map<String, dynamic> json) => DiscoverItem(
    kind: json['kind']?.toString() ?? 'album',
    title: _str(json['title']) ?? _str(json['name']) ?? '',
    artist: _str(json['artist']),
    year: _str(json['year']),
    source: _str(json['source']),
    sourceLabel: _str(json['source_label']),
    coverUrl: _str(json['cover_url']),
    pageUrl: _str(json['page_url']),
    mbid: _str(json['mbid']),
    releaseGroupMbid: _str(json['release_group_mbid']),
    path: _str(json['path']),
    owned: json['owned'] == true,
    inLibrary: json['in_library'] == true,
    reason: _str(json['reason']),
    alsoFrom: _strings(json['also_from']),
    tracks: _strings(json['tracks']),
  );
}

/// `GET /api/discover/genres`. [notes] carries one line per asked source — ""
/// when it answered, "skipped: …" or "failed: …" when it did not — so a page
/// can name the providers that went dark instead of quietly showing fewer.
class DiscoverGenresResult {
  DiscoverGenresResult({
    this.genres = const [],
    this.sourcesAsked = const [],
    this.notes = const {},
  });

  final List<DiscoverGenre> genres;
  final List<String> sourcesAsked;
  final Map<String, String> notes;

  factory DiscoverGenresResult.fromJson(Map<String, dynamic> json) {
    final raw = json['genres'];
    return DiscoverGenresResult(
      genres: raw is List
          ? raw
                .whereType<Map>()
                .map(
                  (g) => DiscoverGenre.fromJson(Map<String, dynamic>.from(g)),
                )
                .toList()
          : const [],
      sourcesAsked: _strings(json['sources_asked']),
      notes: _stringMap(json['notes']),
    );
  }
}

/// `GET /api/discover/genre`: one genre's albums, artists or tracks, from one
/// source or all of them.
class DiscoverGenreResult {
  DiscoverGenreResult({
    this.genre = '',
    this.kind = 'albums',
    this.source = 'all',
    this.items = const [],
    this.sourcesAsked = const [],
    this.notes = const {},
    this.nextOffset,
  });

  final String genre;
  final String kind;
  final String source;
  final List<DiscoverItem> items;
  final List<String> sourcesAsked;
  final Map<String, String> notes;
  final int? nextOffset;

  /// Whether there is another page to ask for. A server that omits the cursor
  /// still gets asked again while it keeps filling the request, and a page
  /// that comes back empty ends the paging by itself.
  bool hasMore(int limit) => nextOffset != null || items.length >= limit;

  factory DiscoverGenreResult.fromJson(Map<String, dynamic> json) =>
      DiscoverGenreResult(
        genre: json['genre']?.toString() ?? '',
        kind: json['kind']?.toString() ?? 'albums',
        source: json['source']?.toString() ?? 'all',
        items: _discoverItems(json['items']),
        sourcesAsked: _strings(json['sources_asked']),
        notes: _stringMap(json['notes']),
        nextOffset: _int(json['next_offset']),
      );
}

/// `GET /api/discover/recommended`. [basis] is the server's own sentence about
/// why these rows were picked — shown as it stands, never re-worded here.
class DiscoverRecommendedResult {
  DiscoverRecommendedResult({
    this.items = const [],
    this.sourcesAsked = const [],
    this.notes = const {},
    this.basis,
  });

  final List<DiscoverItem> items;
  final List<String> sourcesAsked;
  final Map<String, String> notes;
  final String? basis;

  factory DiscoverRecommendedResult.fromJson(Map<String, dynamic> json) =>
      DiscoverRecommendedResult(
        items: _discoverItems(json['items']),
        sourcesAsked: _strings(json['sources_asked']),
        notes: _stringMap(json['notes']),
        basis: _str(json['basis']),
      );
}

List<DiscoverItem> _discoverItems(dynamic raw) => raw is List
    ? raw
          .whereType<Map>()
          .map((r) => DiscoverItem.fromJson(Map<String, dynamic>.from(r)))
          .toList()
    : const [];

/// One job's self-reported progress: the line it publishes and, when it knows
/// them, the parts done and total — the registry's `progress` object.
class JobProgress {
  JobProgress({this.text, this.done, this.total});

  final String? text;
  final double? done;
  final double? total;

  factory JobProgress.fromJson(Map<String, dynamic> json) => JobProgress(
    text: _str(json['text']),
    done: _double(json['done']),
    total: _double(json['total']),
  );
}

/// One folder's share of the storage snapshot: bytes and file count, or null
/// when the folder does not exist.
class StorageFolder {
  const StorageFolder({
    this.path,
    this.bytes = 0,
    this.files = 0,
    this.audioBytes,
    this.sidecarBytes,
  });

  final String? path;
  final int bytes;
  final int files;
  final int? audioBytes;
  final int? sidecarBytes;

  factory StorageFolder.fromJson(Map<String, dynamic> json) => StorageFolder(
    path: _str(json['path']),
    bytes: _int(json['bytes']) ?? 0,
    files: _int(json['files']) ?? 0,
    audioBytes: _int(json['audio_bytes']),
    sidecarBytes: _int(json['sidecar_bytes']),
  );
}

/// `GET /api/storage`: the library's volume, the app's own state, the trash and
/// the transfers. Any figure the OS refused to give is null — the UI says
/// "unknown" rather than showing a zero it made up.
class StorageInfo {
  const StorageInfo({
    this.mount,
    this.label,
    this.totalBytes,
    this.freeBytes,
    this.usedBytes,
    this.percentUsed,
    this.library,
    this.appData,
    this.trash,
    this.downloadBytes,
    this.downloadStagingBytes,
    this.skippedCount = 0,
  });

  final String? mount;
  final String? label;
  final int? totalBytes;
  final int? freeBytes;
  final int? usedBytes;
  final double? percentUsed;
  final StorageFolder? library;
  final StorageFolder? appData;
  final StorageFolder? trash;
  final int? downloadBytes;
  final int? downloadStagingBytes;
  final int skippedCount;

  factory StorageInfo.fromJson(Map<String, dynamic> json) {
    StorageFolder? folder(dynamic value) => value is Map
        ? StorageFolder.fromJson(value.cast<String, dynamic>())
        : null;
    final downloads = json['downloads'];
    return StorageInfo(
      mount: _str(json['mount']),
      label: _str(json['label']),
      totalBytes: _int(json['total_bytes']),
      freeBytes: _int(json['free_bytes']),
      usedBytes: _int(json['used_bytes']),
      percentUsed: _double(json['percent_used']),
      library: folder(json['library']),
      appData: folder(json['app_data']),
      trash: folder(json['trash']),
      downloadBytes: downloads is Map ? _int(downloads['bytes']) : null,
      downloadStagingBytes: downloads is Map
          ? _int(downloads['staging_bytes'])
          : null,
      skippedCount: _int(json['skipped_count']) ?? 0,
    );
  }
}

/// The in-flight job registry (`GET /api/jobs/locks`): what is holding which
/// paths right now, which is what makes a locked file explainable.
class JobLock {
  JobLock({
    required this.jobId,
    required this.kind,
    required this.label,
    this.paths = const [],
    this.startedAt,
    this.elapsed,
    this.progress,
  });

  final String jobId;
  final String kind;
  final String label;
  final List<String> paths;

  /// When the job started, in unix seconds.
  final double? startedAt;

  /// How long the SERVER says the job has been running, in seconds. A client
  /// clock that is off would otherwise report a run time that never happened.
  final double? elapsed;
  final JobProgress? progress;

  factory JobLock.fromJson(Map<String, dynamic> json) {
    final raw = json['progress'];
    return JobLock(
      jobId: json['job_id']?.toString() ?? json['id']?.toString() ?? '',
      kind: json['kind']?.toString() ?? '',
      label: json['label']?.toString() ?? '',
      paths: json['paths'] is List
          ? (json['paths'] as List).map((p) => p.toString()).toList()
          : const [],
      startedAt: _double(json['started_at']),
      elapsed: _double(json['elapsed']),
      progress: raw is Map
          ? JobProgress.fromJson(Map<String, dynamic>.from(raw))
          : (_str(raw) == null ? null : JobProgress(text: _str(raw))),
    );
  }
}
