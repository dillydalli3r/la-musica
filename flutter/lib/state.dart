import 'dart:async';

import 'package:flutter/foundation.dart' show kIsWeb;
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:just_audio/just_audio.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'api.dart';
import 'models.dart';

/// Everything the app knows between launches: the server it was pointed at,
/// the session token, the theme choice, and — as a cache the pages subscribe
/// to — the library payload and the config.
///
/// One object, passed down with an InheritedNotifier, rather than a framework:
/// the app has exactly three pieces of global state and a page that needs one
/// of them subscribes to it by rebuilding on [notifyListeners].
class AppState extends ChangeNotifier {
  AppState({http.Client Function()? clientFactory})
    : _clientFactory = clientFactory;

  final http.Client Function()? _clientFactory;

  static const _serverKey = 'mlo.server';
  static const _tokenKey = 'mlo.token';
  static const _themeKey = 'mlo.theme';

  ApiClient? _api;
  ApiClient? get api => _api;
  bool get connected => _api != null;

  String serverUrl = '';
  String? authError;
  bool loadingLibrary = false;
  Library? library;
  Map<String, dynamic> config = const {};
  ThemeMode themeMode = ThemeMode.dark;

  /// The value the client shows for a config key, with the shipped default as
  /// the fallback — a page asked for `replaygain_mode` before the config
  /// arrived must not invent one of its own.
  T configValue<T>(String key, T fallback) {
    final value = config[key];
    if (value is T) return value;
    if (value is num && T == double) return value.toDouble() as T;
    if (value is num && T == int) return value.toInt() as T;
    return fallback;
  }

  Future<void> boot() async {
    final prefs = await SharedPreferences.getInstance();
    themeMode = switch (prefs.getString(_themeKey)) {
      'light' => ThemeMode.light,
      'system' => ThemeMode.system,
      _ => ThemeMode.dark,
    };
    var saved = prefs.getString(_serverKey) ?? '';
    if (saved.isEmpty && kIsWeb) {
      // The web build is normally served BY the server, so "no address
      // configured" means this very origin: asking a browser user to type the
      // address they are already talking to would be a needless first run.
      saved = Uri.base.origin;
    }
    serverUrl = saved;
    final token = prefs.getString(_tokenKey);
    if (saved.isNotEmpty) {
      _api = ApiClient(
        baseUrl: serverUrl,
        token: token,
        client: _clientFactory?.call(),
      );
    }
    notifyListeners();
  }

  /// Point this client at a server and prove it answers before saving it: a
  /// wrong address must fail HERE, with the health check's own error, not on
  /// the first page the user opens.
  Future<void> connect(String url) async {
    final candidate = ApiClient(baseUrl: url, client: _clientFactory?.call());
    try {
      final health = await candidate.health();
      if (health['status']?.toString() != 'ok') {
        throw ApiException(
          0,
          'that address answered, but it is not a la musica server',
        );
      }
    } on ApiException {
      candidate.close();
      rethrow;
    }
    _api?.close();
    _api = candidate;
    serverUrl = candidate.baseUrl;
    authError = null;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_serverKey, serverUrl);
    notifyListeners();
  }

  Future<void> setTheme(ThemeMode mode) async {
    themeMode = mode;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(
      _themeKey,
      mode == ThemeMode.light
          ? 'light'
          : (mode == ThemeMode.system ? 'system' : 'dark'),
    );
    notifyListeners();
  }

  Future<AuthStatus> authStatus() async {
    final client = _api;
    if (client == null) return AuthStatus();
    try {
      final status = await client.authStatus();
      authError = null;
      notifyListeners();
      return status;
    } on ApiException catch (e) {
      authError = e.message;
      notifyListeners();
      rethrow;
    }
  }

  Future<void> login(String password, {String? username}) async {
    final client = _requireApi();
    try {
      final token = await client.login(password, username: username);
      client.token = token;
      authError = null;
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString(_tokenKey, token);
    } on ApiException catch (e) {
      authError = e.message;
      notifyListeners();
      rethrow;
    }
    notifyListeners();
  }

  Future<void> setupPassword(String password, {String? username}) async {
    final client = _requireApi();
    final token = await client.setup(password, username: username);
    client.token = token;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_tokenKey, token);
    notifyListeners();
  }

  Future<void> logout() async {
    await _api?.logout();
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_tokenKey);
    _api?.token = null;
    notifyListeners();
  }

  Future<void> refreshLibrary() async {
    final client = _api;
    if (client == null) return;
    loadingLibrary = true;
    notifyListeners();
    try {
      library = await client.library();
      authError = null;
    } on ApiException catch (e) {
      if (e.isAuth) {
        authError = e.message;
      } else {
        rethrow;
      }
    } finally {
      loadingLibrary = false;
      notifyListeners();
    }
  }

  Future<void> refreshConfig() async {
    final client = _api;
    if (client == null) return;
    config = await client.config();
    notifyListeners();
  }

  Future<void> saveConfig(Map<String, dynamic> patch) async {
    final client = _requireApi();
    config = await client.saveConfig({...config, ...patch});
    notifyListeners();
  }

  ApiClient _requireApi() {
    final client = _api;
    if (client == null) {
      throw ApiException(0, 'no server configured yet');
    }
    return client;
  }
}

/// The audio player the whole app shares: one queue, one position, one gain.
///
/// ReplayGain is the reason this class exists rather than a `just_audio` call
/// in the player widget. The gain MUST be on the pipeline before the first
/// sample is audible — applying it after `play()` is exactly the loud burst
/// this app is fixing — so [playPath] fetches the track's gain, sets the
/// player volume, and only then hands the URL to the player.
class PlaybackController extends ChangeNotifier {
  PlaybackController({required this.state});

  final AppState state;
  final AudioPlayer player = AudioPlayer();

  final List<Map<String, dynamic>> queue = [];
  int index = -1;

  double _gainDb = 0;
  String? _gainSource;
  bool _gainAnalyzed = false;
  double _volume = 1;

  double get gainDb => _gainDb;
  String? get gainSource => _gainSource;
  bool get gainAnalyzed => _gainAnalyzed;
  double get volume => _volume;
  bool get hasTrack => index >= 0 && index < queue.length;
  Map<String, dynamic>? get current => hasTrack ? queue[index] : null;

  Duration get position => player.position;
  Duration? get duration => player.duration;
  bool get playing => player.playing;

  /// Load and start one path immediately (a row click), replacing the queue.
  Future<void> playPath(
    String path, {
    List<Map<String, dynamic>>? queueAfter,
  }) async {
    queue
      ..clear()
      ..addAll(
        queueAfter ??
            [
              {'path': path},
            ],
      );
    index = queue.indexWhere((t) => t['path'] == path);
    if (index < 0) index = 0;
    await _load();
  }

  Future<void> playQueue(
    List<Map<String, dynamic>> tracks, {
    int start = 0,
  }) async {
    if (tracks.isEmpty) return;
    queue
      ..clear()
      ..addAll(tracks);
    index = start.clamp(0, tracks.length - 1);
    await _load();
  }

  /// Add tracks without touching what is playing — at the end of the queue, or
  /// straight after the current track for a "play next". An empty queue has
  /// nothing to preserve, so it starts playing instead.
  Future<void> queueAdd(
    List<Map<String, dynamic>> tracks, {
    bool next = false,
  }) async {
    if (tracks.isEmpty) return;
    if (queue.isEmpty || index < 0) {
      await playQueue(tracks);
      return;
    }
    queue.insertAll(next ? index + 1 : queue.length, tracks);
    notifyListeners();
  }

  Future<void> next() async {
    if (index + 1 >= queue.length) return;
    index += 1;
    await _load();
  }

  Future<void> previous() async {
    if (index <= 0) return;
    index -= 1;
    await _load();
  }

  Future<void> toggle() async {
    if (player.playing) {
      await player.pause();
    } else {
      await player.play();
    }
    notifyListeners();
  }

  Future<void> seek(Duration to) => player.seek(to);

  Future<void> setVolume(double value) async {
    _volume = value.clamp(0.0, 1.0);
    await player.setVolume(_volume * _gainLinear());
    notifyListeners();
  }

  /// Re-fetch the gain for the current track (a mode or preamp change) and
  /// apply it to the RUNNING element — used when the user edits the setting
  /// mid-song, where a small step is preferable to a re-load.
  Future<void> refreshGain() async {
    if (!hasTrack) return;
    await _applyGain();
    notifyListeners();
  }

  double _gainLinear() {
    // dB → linear amplitude without dart:math: 10^(db/20).
    var result = 1.0;
    var term = 1.0;
    final x = (_gainDb / 20) * 2.302585092994046;
    for (var i = 1; i <= 12; i++) {
      term *= x / i;
      result += term;
    }
    return result;
  }

  Future<void> _applyGain() async {
    final client = state.api;
    final track = current;
    if (client == null || track == null) return;
    final path = track['path']?.toString() ?? '';
    if (path.isEmpty) return;
    final mode = state.configValue<String>('replaygain_mode', 'track');
    try {
      if (mode == 'off') {
        _gainDb = 0;
        _gainSource = null;
        _gainAnalyzed = false;
      } else {
        final rg = await client.replaygain(path, mode: mode);
        _gainDb = rg.gain ?? 0;
        _gainSource = rg.source;
        _gainAnalyzed = rg.analyzed;
      }
    } on ApiException {
      // A gain we could not read is unity, never a guess.
      _gainDb = 0;
      _gainSource = null;
      _gainAnalyzed = false;
    }
    await player.setVolume(_volume * _gainLinear());
  }

  Future<void> _load() async {
    final client = state.api;
    final track = current;
    if (client == null || track == null) return;
    final path = track['path']?.toString() ?? '';
    if (path.isEmpty) return;
    notifyListeners();

    // Silence first, so nothing plays while the new track is being prepared —
    // then the gain, THEN the URL and play. The order is the fix.
    await player.setVolume(0);
    await _applyGain();
    try {
      await player.setUrl(client.streamUrl(path));
    } on PlayerException catch (e) {
      _loadError = 'cannot play this file: ${e.message}';
      notifyListeners();
      return;
    } on PlayerInterruptedException {
      return;
    }
    _loadError = null;
    await player.play();
    notifyListeners();
  }

  String? _loadError;
  String? get loadError => _loadError;

  /// The label under the player: the TITLE tag, or the file name when the
  /// file has none.
  String get currentTitle {
    final track = current;
    if (track == null) return '';
    final title = track['title']?.toString();
    if (title != null && title.isNotEmpty) return title;
    final file = track['file']?.toString() ?? track['path']?.toString() ?? '';
    return file
        .split(RegExp(r'[\\/]'))
        .last
        .replaceAll(RegExp(r'\.[^.]+$'), '');
  }

  @override
  void dispose() {
    player.dispose();
    super.dispose();
  }
}

/// Exposes [AppState] and [PlaybackController] to the widget tree.
class AppScope extends InheritedNotifier<AppState> {
  const AppScope({
    super.key,
    required AppState state,
    required this.playback,
    required super.child,
  }) : super(notifier: state);

  final PlaybackController playback;

  static AppState of(BuildContext context) =>
      context.dependOnInheritedWidgetOfExactType<AppScope>()!.notifier!;

  static PlaybackController playbackOf(BuildContext context) =>
      context.dependOnInheritedWidgetOfExactType<AppScope>()!.playback;
}
