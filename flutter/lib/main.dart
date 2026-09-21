import 'package:flutter/material.dart';

import 'shell.dart';
import 'state.dart';
import 'theme.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const LaMusicaApp());
}

/// The app itself: one [AppState] and one [PlaybackController] for the whole
/// session, a theme that follows the user's pick (dark by default), and the
/// gate in front of the pages.
class LaMusicaApp extends StatefulWidget {
  const LaMusicaApp({super.key});

  @override
  State<LaMusicaApp> createState() => _LaMusicaAppState();
}

class _LaMusicaAppState extends State<LaMusicaApp> {
  final AppState _state = AppState();
  late final PlaybackController _playback = PlaybackController(state: _state);
  bool _booted = false;

  @override
  void initState() {
    super.initState();
    _state.boot().then((_) {
      if (mounted) setState(() => _booted = true);
    });
  }

  @override
  void dispose() {
    _playback.dispose();
    _state.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => AppScope(
    state: _state,
    playback: _playback,
    child: AnimatedBuilder(
      animation: _state,
      builder: (context, _) => MaterialApp(
        title: 'la musica',
        debugShowCheckedModeBanner: false,
        theme: AppTheme.light(),
        darkTheme: AppTheme.dark(),
        themeMode: _state.themeMode,
        home: _booted
            ? const Gate()
            : const Scaffold(body: Center(child: CircularProgressIndicator())),
      ),
    ),
  );
}
