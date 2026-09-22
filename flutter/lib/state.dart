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
  /// The linear factor actually handed to the player — the model's own
  /// [ReplayGain.linear], unity until the first answer arrives.
  double _gainLinear = 1;
  String? _gainSource;
  bool _gainAnalyzed = false;
  /// True only when the number came from the album gain; false in album mode
  /// means this album has none and the track value was used (the readout says
  /// so instead of implying the album was matched as an album).
  bool _gainAlbum = false;
  double _volume = 1;

  double get gainDb => _gainDb;
  String? get gainSource => _gainSource;
  bool get gainAnalyzed => _gainAnalyzed;
  bool get gainAlbum => _gainAlbum;
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
    await player.setVolume(_volume * _gainLinear);
    notifyListeners();
  }

  /// Re-fetch the gain for the current track (a mode or preamp change) and
  /// apply it to the RUNNING element — the settings page calls this after it
  /// saves the ReplayGain section, so a mode/preamp edit lands on the song that
  /// is already playing instead of only on the next one, where a small step is
  /// preferable to a re-load.
  Future<void> refreshGain() async {
    if (!hasTrack) return;
    await _applyGain();
    notifyListeners();
  }

  /// How long to wait between re-asks for a gain the server is still measuring
  /// on the fly, in ms. Growing, because a whole-file EBU R128 decode lands in
  /// seconds on a warm disk and only a long file on a cold one is slow; the
  /// list's LENGTH is the number of attempts, and its sum (~48 s) is where this
  /// client gives up — by then the server has the value cached, so the next
  /// play reads it in one request.
  static const List<int> _rgRetryMs = [1500, 3000, 5000, 8000, 12000, 18000];
  Timer? _rgTimer;
  String? _rgWatchPath;
  int _rgTries = 0;

  /// Keep asking for [path]'s gain while the server reports its on-demand
  /// measurement as still running, and apply the value to the track that is
  /// ALREADY PLAYING when it lands.
  ///
  /// The request is bounded server-side (`PLAYBACK_WAIT_S`) because playback
  /// must not wait on a full decode, so an untagged track starts at unity — but
  /// that answer is not final: the decode keeps going and caches its result.
  /// Without this the phone played the whole track at unity and nothing but a
  /// settings change ever fetched the gain again. just_audio sets its volume as
  /// a step (there is no ramp to schedule on the platform player), so this one
  /// lands as a step — the same edit [refreshGain] makes mid-song.
  void _watchGain(String path, bool pending) {
    _rgTimer?.cancel();
    _rgTimer = null;
    final playing = current?['path']?.toString() ?? '';
    if (!pending || path == '' || path != playing) {
      _rgWatchPath = null;
      _rgTries = 0;
      return;
    }
    if (_rgWatchPath != path) {
      _rgWatchPath = path;
      _rgTries = 0;
    }
    if (_rgTries >= _rgRetryMs.length) {
      // A decode that has not finished in ~48 s is not worth more requests: its
      // value is cached for the next play either way.
      _rgWatchPath = null;
      return;
    }
    final wait = _rgRetryMs[_rgTries];
    _rgTries += 1;
    _rgTimer = Timer(Duration(milliseconds: wait), () async {
      _rgTimer = null;
      if (_rgWatchPath != path || (current?['path']?.toString() ?? '') != path) {
        _rgWatchPath = null;
        _rgTries = 0;
        return;
      }
      await _applyGain(); // re-arms itself while the answer is still pending
      notifyListeners();
    });
  }

  Future<void> _applyGain() async {
    final client = state.api;
    final track = current;
    if (client == null || track == null) return;
    final path = track['path']?.toString() ?? '';
    if (path.isEmpty) return;
    final mode = state.configValue<String>('replaygain_mode', 'track');
    var pending = false;
    try {
      if (mode == 'off') {
        _gainDb = 0;
        _gainLinear = 1;
        _gainSource = null;
        _gainAnalyzed = false;
        _gainAlbum = false;
      } else {
        final rg = await client.replaygain(path, mode: mode);
        _gainDb = rg.gain ?? 0;
        // The model's own maths (10^(dB/20), clamped to the same ±24 dB window
        // as the web player) instead of a second copy of it in here — the
        // hand-rolled Taylor series computed the same value, and two
        // implementations of one gain is exactly how the clients drift.
        _gainLinear = rg.linear;
        _gainSource = rg.source;
        _gainAnalyzed = rg.analyzed;
        _gainAlbum = rg.album;
        pending = rg.pending;
      }
    } on ApiException {
      // A gain we could not read is unity, never a guess.
      _gainDb = 0;
      _gainLinear = 1;
      _gainSource = null;
      _gainAnalyzed = false;
      _gainAlbum = false;
    }
    await player.setVolume(_volume * _gainLinear);
    _watchGain(path, pending);
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
    // One play per start, reported to the same endpoint the web player uses.
    // Fire and forget: the row is a statistic, so a server that is away costs
    // the play and never the audio.
    unawaited(client.recordPlay(path).catchError((Object _) {}));
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
    // A pending re-ask must not outlive the player (it would set the volume of
    // a disposed platform player).
    _rgTimer?.cancel();
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
