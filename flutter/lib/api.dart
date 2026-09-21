import 'dart:convert';

import 'package:http/http.dart' as http;

import 'models.dart';

/// The server answered with an error the caller should show as-is.
class ApiException implements Exception {
  ApiException(this.status, this.message);

  final int status;
  final String message;

  /// The session is gone or was never established — the shell shows the login
  /// screen instead of an error toast.
  bool get isAuth => status == 401 || status == 403;

  @override
  String toString() => message.isEmpty ? 'HTTP $status' : message;
}

/// The `/api/replaygain` answer: the dB the player must apply before the first
/// sample, and where that number came from.
class ReplayGain {
  ReplayGain({
    this.gain,
    this.peak,
    this.mode = 'track',
    this.source,
    this.analyzed = false,
  });

  final double? gain;
  final double? peak;
  final String mode;
  final String? source;
  final bool analyzed;

  /// Linear amplitude for a dB gain — what a player's volume control takes.
  /// Unity when the server sent nothing (an untagged file at `off`).
  double get linear {
    final db = gain ?? 0;
    final value = _pow10(db / 20);
    return value.clamp(0.0, 4.0);
  }

  static double _pow10(double exponent) {
    // 10^x without dart:math's pow double-dispatch: exp(x * ln 10).
    var result = 1.0;
    var term = 1.0;
    final x = exponent * 2.302585092994046;
    for (var i = 1; i <= 12; i++) {
      term *= x / i;
      result += term;
    }
    return result;
  }

  factory ReplayGain.fromJson(Map<String, dynamic> json) => ReplayGain(
    gain: (json['gain'] as num?)?.toDouble(),
    peak: (json['peak'] as num?)?.toDouble(),
    mode: json['mode']?.toString() ?? 'track',
    source: json['source']?.toString(),
    analyzed: json['analyzed'] == true,
  );
}

/// The auth state a client checks before showing a login screen.
class AuthStatus {
  AuthStatus({
    this.hasPassword = false,
    this.authenticated = false,
    this.required = false,
    this.gate = false,
    this.local = false,
    this.username = '',
    this.setupHint = '',
  });

  final bool hasPassword;

  /// "Does THIS request need to sign in?" — already true for a client that
  /// does not (loopback, and the machine running the server), which is why a
  /// local user is never shown a sign-in screen nobody needs.
  final bool authenticated;

  /// Whether this request is one the server wants authenticated.
  final bool required;

  /// The server-wide policy behind [required] — what a security panel shows.
  final bool gate;
  final bool local;
  final String username;

  /// A sentence for the UI when the configuration is unsafe, else "".
  final String setupHint;

  factory AuthStatus.fromJson(Map<String, dynamic> json) => AuthStatus(
    hasPassword: json['has_password'] == true,
    authenticated: json['authenticated'] == true,
    required: json['required'] == true,
    gate: json['gate'] == true,
    local: json['local'] == true,
    username: json['username']?.toString() ?? '',
    setupHint: json['setup_hint']?.toString() ?? '',
  );
}

/// The `/api/favorites` answer: the folder paths the user hearted, by kind.
/// A favourite whose folder is gone comes back here too — dropping it is the
/// client's job to explain, never to hide.
class Favorites {
  Favorites({
    this.albums = const [],
    this.artists = const [],
    this.playlists = const [],
  });

  final List<String> albums;
  final List<String> artists;
  final List<String> playlists;

  factory Favorites.fromJson(Map<String, dynamic> json) => Favorites(
    albums: _paths(json['albums']),
    artists: _paths(json['artists']),
    playlists: _paths(json['playlists']),
  );

  static List<String> _paths(dynamic value) =>
      value is List ? [for (final path in value) path.toString()] : const [];
}

/// One album `POST /api/library/add` created or found already on disk:
/// `created` is false for a folder that was already a real album, and `wishId`
/// is the queue entry searching for its audio.
class LibraryAddAlbum {
  LibraryAddAlbum({
    required this.albumPath,
    this.title = '',
    this.artist = '',
    this.year = '',
    this.created = false,
    this.alreadyInLibrary = false,
    this.wishId,
  });

  final String albumPath;
  final String title;
  final String artist;
  final String year;
  final bool created;
  final bool alreadyInLibrary;
  final int? wishId;

  factory LibraryAddAlbum.fromJson(Map<String, dynamic> json) =>
      LibraryAddAlbum(
        albumPath: json['album_path']?.toString() ?? '',
        title: json['title']?.toString() ?? '',
        artist: json['artist']?.toString() ?? '',
        year: json['year']?.toString() ?? '',
        created: json['created'] == true,
        alreadyInLibrary: json['already_in_library'] == true,
        wishId: (json['wish_id'] as num?)?.toInt(),
      );
}

/// The `/api/library/add` answer. It is read as the server ACTUALLY did it —
/// a client that counts what it asked for is a client that lies about what
/// happened. [background] is an artist's discography, prepared off-request.
class LibraryAddResult {
  LibraryAddResult({
    this.ok = false,
    this.background = false,
    this.note,
    this.albums = const [],
    this.skipped = const [],
    this.errors = const [],
  });

  final bool ok;
  final bool background;
  final String? note;
  final List<LibraryAddAlbum> albums;

  /// The reasons the server gave for what it did not add: its own `reason`, or
  /// the bare MBID when it gave none.
  final List<String> skipped;
  final List<String> errors;

  int get added => albums.where((a) => a.created).length;
  int get alreadyInLibrary => albums.where((a) => a.alreadyInLibrary).length;

  /// The one line the UI shows: what was added, what was already there, what
  /// was skipped and the first reason why — the sentence the React client's
  /// toast carries, so the two front ends report the same add the same way.
  String get message {
    if (background) {
      return note ??
          'Preparing the discography — the albums appear as they are added';
    }
    final tail = StringBuffer();
    if (alreadyInLibrary > 0) {
      tail.write(' · $alreadyInLibrary already in the library');
    }
    final missed = skipped.length + errors.length;
    if (missed > 0) tail.write(' · $missed skipped');
    if (errors.isNotEmpty) {
      tail.write(' · ${errors.first}');
    } else if (skipped.isNotEmpty) {
      tail.write(' · ${skipped.first}');
    }
    if (added > 0) return 'Added $added to your library$tail';
    return 'Nothing added${tail.isEmpty ? ' — MusicBrainz is busy, try again' : tail}';
  }

  factory LibraryAddResult.fromJson(Map<String, dynamic> json) =>
      LibraryAddResult(
        ok: json['ok'] == true,
        background: json['background'] == true,
        note: _text(json['note']),
        albums: json['albums'] is List
            ? [
                for (final row in json['albums'] as List)
                  if (row is Map)
                    LibraryAddAlbum.fromJson(row.cast<String, dynamic>()),
              ]
            : const [],
        skipped: _issues(json['skipped']),
        errors: _issues(json['errors']),
      );

  static List<String> _issues(dynamic raw) => raw is List
      ? [
          for (final row in raw)
            if (row != null)
              _text(row is Map ? (row['reason'] ?? row['mbid']) : row) ?? '',
        ].where((reason) => reason.isNotEmpty).toList()
      : const [];

  static String? _text(dynamic value) {
    final text = value?.toString().trim();
    return (text == null || text.isEmpty) ? null : text;
  }
}

/// The REST client for one server. Every method either returns the parsed
/// payload or throws [ApiException] with the server's own message: a client
/// that swallows an error is a client that lies about what it did.
class ApiClient {
  ApiClient({required String baseUrl, this.token, http.Client? client})
    : baseUrl = _normalize(baseUrl),
      _client = client ?? http.Client();

  /// "" means "same origin" — the web build is served BY the server, so a
  /// relative URL is both correct and immune to a changed address.
  String baseUrl;
  String? token;
  final http.Client _client;

  static String _normalize(String value) {
    var url = value.trim();
    if (url.isEmpty) return '';
    if (!url.startsWith('http://') && !url.startsWith('https://')) {
      url = 'http://$url';
    }
    while (url.endsWith('/')) {
      url = url.substring(0, url.length - 1);
    }
    return url;
  }

  Uri uri(String path, [Map<String, String?>? query]) =>
      Uri.parse('$baseUrl$path').replace(
        queryParameters: query == null
            ? null
            : {
                for (final entry in query.entries)
                  if (entry.value != null) entry.key: entry.value!,
              },
      );

  Map<String, String> get _headers => {
    'Accept': 'application/json',
    if (token != null && token!.isNotEmpty) 'Authorization': 'Bearer $token',
  };

  Future<dynamic> _send(
    String method,
    String path, {
    Map<String, String?>? query,
    Object? body,
    Duration? timeout,
  }) async {
    final request = http.Request(method, uri(path, query));
    request.headers.addAll(_headers);
    if (body != null) {
      request.headers['Content-Type'] = 'application/json';
      request.body = jsonEncode(body);
    }
    final streamed = await _client
        .send(request)
        .timeout(timeout ?? const Duration(seconds: 30));
    final response = await http.Response.fromStream(streamed);
    if (response.statusCode >= 400) {
      throw ApiException(response.statusCode, _errorMessage(response));
    }
    if (response.body.isEmpty) return null;
    try {
      return jsonDecode(utf8.decode(response.bodyBytes));
    } on FormatException {
      return response.body;
    }
  }

  String _errorMessage(http.Response response) {
    try {
      final decoded = jsonDecode(utf8.decode(response.bodyBytes));
      if (decoded is Map && decoded['detail'] != null) {
        final detail = decoded['detail'];
        if (detail is String) return detail;
        return jsonEncode(detail);
      }
    } on FormatException {
      // fall through to the raw body
    }
    final text = response.body.trim();
    return text.isEmpty ? 'HTTP ${response.statusCode}' : text;
  }

  Future<dynamic> getJson(
    String path, {
    Map<String, String?>? query,
    Duration? timeout,
  }) => _send('GET', path, query: query, timeout: timeout);

  Future<dynamic> postJson(
    String path, {
    Object? body,
    Map<String, String?>? query,
    Duration? timeout,
  }) => _send('POST', path, body: body, query: query, timeout: timeout);

  // ---- auth ---------------------------------------------------------------

  Future<AuthStatus> authStatus() async => AuthStatus.fromJson(
    await getJson('/api/auth/status') as Map<String, dynamic>,
  );

  /// Sign in. The server answers `{token, expires_at, session_days, username}`
  /// and ALSO sets an HttpOnly cookie for browsers; this client keeps the
  /// token and sends it as a bearer header, which is what a native shell does.
  Future<String> login(String password, {String? username}) async {
    final reply =
        await postJson(
              '/api/auth/login',
              body: {
                'password': password,
                if (username != null && username.isNotEmpty)
                  'username': username,
              },
            )
            as Map<String, dynamic>;
    return reply['token']?.toString() ?? '';
  }

  /// First run: set the password (and the first user) on a fresh server.
  Future<String> setup(
    String password, {
    String? username,
    int? sessionDays,
  }) async {
    final reply =
        await postJson(
              '/api/auth/setup',
              body: {
                'password': password,
                if (username != null && username.isNotEmpty)
                  'username': username,
                if (sessionDays != null) 'session_days': sessionDays,
              },
            )
            as Map<String, dynamic>;
    return reply['token']?.toString() ?? '';
  }

  Future<void> logout() async {
    try {
      await postJson('/api/auth/logout');
    } on ApiException {
      // A session the server already forgot is still a signed-out client.
    }
  }

  // ---- library ------------------------------------------------------------

  Future<Map<String, dynamic>> health() async =>
      (await getJson('/api/health') as Map).cast<String, dynamic>();

  Future<Library> library() async => Library.fromJson(
    await getJson('/api/library', timeout: const Duration(seconds: 120))
        as Map<String, dynamic>,
  );

  Future<Album> album(String path) async => Album.fromJson(
    await getJson('/api/album', query: {'path': path}) as Map<String, dynamic>,
  );

  Future<Artist> artist(String path) async => Artist.fromJson(
    await getJson('/api/artist', query: {'path': path}) as Map<String, dynamic>,
  );

  Future<Map<String, String>> trackTags(String path) async {
    final json = await getJson('/api/tags', query: {'path': path});
    if (json is Map && json['tags'] is Map) {
      return (json['tags'] as Map).map(
        (k, v) => MapEntry(k.toString(), v?.toString() ?? ''),
      );
    }
    return const {};
  }

  Future<List<Playlist>> playlists() async {
    final json = await getJson('/api/playlists');
    if (json is! List) return const [];
    return json
        .whereType<Map>()
        .map((p) => Playlist.fromJson(p.cast<String, dynamic>()))
        .toList();
  }

  Future<Playlist> playlist(int id) async => Playlist.fromJson(
    await getJson('/api/playlists/$id') as Map<String, dynamic>,
  );

  Future<StorageInfo> storage() async => StorageInfo.fromJson(
    await getJson('/api/storage') as Map<String, dynamic>,
  );

  Future<List<JobLock>> jobLocks() async {
    final json = await getJson('/api/jobs/locks');
    final rows = json is Map ? json['jobs'] : json;
    if (rows is! List) return const [];
    return rows
        .whereType<Map>()
        .map((j) => JobLock.fromJson(j.cast<String, dynamic>()))
        .toList();
  }

  Future<List<Recommendation>> recommendations({
    required String kind,
    String? id,
    String target = 'albums',
    int limit = 12,
  }) async {
    final json = await getJson(
      '/api/recommend',
      query: {
        'kind': kind,
        if (id != null) 'id': id,
        'target': target,
        'limit': '$limit',
      },
    );
    final rows = json is Map ? json['items'] : json;
    if (rows is! List) return const [];
    return rows
        .whereType<Map>()
        .map((r) => Recommendation.fromJson(r.cast<String, dynamic>()))
        .toList();
  }

  // ---- likes & favorites --------------------------------------------------

  /// The liked (hearted) track paths, newest first.
  Future<List<String>> likes() async {
    final json = await getJson('/api/likes');
    if (json is! Map) return const [];
    final paths = json['paths'];
    if (paths is! List) return const [];
    return [for (final path in paths) path.toString()];
  }

  Future<Favorites> favorites() async => Favorites.fromJson(
    (await getJson('/api/favorites') as Map).cast<String, dynamic>(),
  );

  // ---- playback -----------------------------------------------------------

  Future<ReplayGain> replaygain(String path, {String? mode}) async =>
      ReplayGain.fromJson(
        await getJson(
              '/api/replaygain',
              query: {'path': path, if (mode != null) 'mode': mode},
            )
            as Map<String, dynamic>,
      );

  /// The audio stream URL. `token` rides as a query parameter because a media
  /// element cannot send headers on every platform.
  String streamUrl(String path) => uri('/api/stream', {
    'path': path,
    if (token != null && token!.isNotEmpty) 'token': token,
  }).toString();

  String coverUrl(String albumPath, String? coverFile) => uri('/api/cover', {
    'album': albumPath,
    if (coverFile != null && coverFile.isNotEmpty) 'file': coverFile,
    if (token != null && token!.isNotEmpty) 'token': token,
  }).toString();

  /// Report ONE playback start (`POST /api/plays`) — the very same seam the
  /// web player calls, so the server sees one definition of "a play" whatever
  /// client started it. Called when a track actually starts, never on a seek
  /// or a resume; a repeat is a play of its own.
  Future<void> recordPlay(String path) async {
    await postJson('/api/plays', body: {'path': path});
  }

  // ---- discovery ----------------------------------------------------------

  /// The genres the discovery scopes can name, with the counts each source
  /// contributed (`GET /api/discover/genres`).
  Future<DiscoverGenresResult> discoverGenres({String scope = 'all'}) async =>
      DiscoverGenresResult.fromJson(
        await getJson(
              '/api/discover/genres',
              query: {'scope': scope},
              timeout: _discoverTimeout,
            )
            as Map<String, dynamic>,
      );

  /// One genre's albums, artists or tracks, from [source] or all of them,
  /// one page at a time (`GET /api/discover/genre`).
  Future<DiscoverGenreResult> discoverGenre({
    required String genre,
    String kind = 'albums',
    String source = 'all',
    int limit = 25,
    int offset = 0,
  }) async => DiscoverGenreResult.fromJson(
    await getJson(
          '/api/discover/genre',
          query: {
            'genre': genre,
            'kind': kind,
            'source': source,
            'limit': '$limit',
            'offset': '$offset',
          },
          timeout: _discoverTimeout,
        )
        as Map<String, dynamic>,
  );

  /// What to listen to next, from the library as a whole or from one genre
  /// (`GET /api/discover/recommended`).
  Future<DiscoverRecommendedResult> discoverRecommended({
    String seed = 'library',
    String kind = 'albums',
    int limit = 20,
  }) async => DiscoverRecommendedResult.fromJson(
    await getJson(
          '/api/discover/recommended',
          query: {'seed': seed, 'kind': kind, 'limit': '$limit'},
          timeout: _discoverTimeout,
        )
        as Map<String, dynamic>,
  );

  /// Take one discovery row into the library (`POST /api/library/add`): the
  /// server creates the framework album and the wish queue hunts its audio.
  ///
  /// [mbid] is the entity to add; [releaseGroupMbid] and [releaseMbid] are the
  /// row's own ids when it has them — a release group wins, because the app
  /// wishes for groups and lets the server pick the edition. [kind] is one of
  /// the server's entity kinds (`DiscoverItem.wishKind` derives it).
  Future<LibraryAddResult> addToLibrary({
    String? releaseGroupMbid,
    String? releaseMbid,
    String? mbid,
    String kind = 'auto',
    String? title,
    String? artist,
    String? year,
  }) async {
    final id = _firstId([releaseGroupMbid, mbid, releaseMbid]);
    if (id == null) {
      // Nothing to add is a client-side mistake: said here rather than sent as
      // a request the server can only answer with a 400.
      throw ApiException(0, 'this row has no MusicBrainz id to add');
    }
    final reply = await postJson(
      '/api/library/add',
      body: {
        'mbid': id,
        'kind': kind,
        if (releaseMbid != null && releaseMbid.isNotEmpty)
          'release_mbid': releaseMbid,
        if (title != null && title.isNotEmpty) 'title': title,
        if (artist != null && artist.isNotEmpty) 'artist': artist,
        if (year != null && year.isNotEmpty) 'year': year,
      },
      // A release group's editions and an artist's discography are several
      // MusicBrainz round trips; the default 30 s is not enough for either.
      timeout: const Duration(minutes: 2),
    );
    return LibraryAddResult.fromJson((reply as Map).cast<String, dynamic>());
  }

  /// Undo an add (`POST /api/library/add/cancel`): the framework album and the
  /// wish both go, and the answer says which of them was still there.
  Future<String> cancelAdd({String? albumPath, int? wishId}) async {
    final reply = await postJson(
      '/api/library/add/cancel',
      body: {
        if (albumPath != null && albumPath.isNotEmpty) 'album_path': albumPath,
        if (wishId != null) 'wish_id': wishId,
      },
    );
    final answer = reply is Map ? reply : const <String, dynamic>{};
    final removed = answer['removed'] == true;
    final wishDeleted = answer['wish_deleted'] == true;
    if (removed && wishDeleted) {
      return 'Removed the pending album and its wish.';
    }
    if (removed) return 'Removed the pending album.';
    if (wishDeleted) return 'Removed the wish.';
    return 'Nothing was pending any more.';
  }

  static const Duration _discoverTimeout = Duration(seconds: 60);

  static String? _firstId(List<String?> ids) {
    for (final id in ids) {
      if (id != null && id.isNotEmpty) return id;
    }
    return null;
  }

  // ---- config -------------------------------------------------------------

  Future<Map<String, dynamic>> config() async =>
      (await getJson('/api/config') as Map).cast<String, dynamic>();

  /// The whole config is written back (the server validates every key), which
  /// is what the React settings form does too.
  Future<Map<String, dynamic>> saveConfig(Map<String, dynamic> values) async =>
      (await postJson('/api/config', body: values) as Map)
          .cast<String, dynamic>();

  /// Run optimisation scripts on a selection (`ids` are the script numbers).
  Future<void> runScripts(
    List<int> ids,
    List<String> paths, {
    bool force = false,
  }) async {
    await postJson(
      '/api/run',
      body: {'ids': ids, 'paths': paths, 'force': force},
      timeout: const Duration(minutes: 5),
    );
  }

  void close() => _client.close();
}
