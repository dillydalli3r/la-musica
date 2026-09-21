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
