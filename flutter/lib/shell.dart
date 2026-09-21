import 'package:flutter/material.dart';

import 'api.dart';
import 'pages/album.dart';
import 'pages/artist.dart';
import 'pages/connect.dart';
import 'pages/favorites.dart';
import 'pages/home.dart';
import 'pages/in_progress.dart';
import 'pages/library.dart';
import 'pages/login.dart';
import 'pages/playlist_detail.dart';
import 'pages/playlists.dart';
import 'pages/settings.dart';
import 'pages/track.dart';
import 'state.dart';
import 'widgets/player_bar.dart';

/// The navigation groups of the React client, in the same order, so the two
/// front ends are recognisably one app.
///
/// A destination whose page is not ported yet is deliberately NOT listed: a
/// rail entry that opens nothing is worse than a shorter rail, and the React
/// UI stays the complete client until parity.
const List<NavGroup> navGroups = [
  NavGroup('Library', [
    NavEntry('Home', Icons.home_outlined, Icons.home),
    NavEntry('Library', Icons.library_music_outlined, Icons.library_music),
    NavEntry('Playlists', Icons.queue_music_outlined, Icons.queue_music),
    NavEntry('Favorites', Icons.favorite_border, Icons.favorite),
  ]),
  NavGroup('Maintain', [
    NavEntry(
      'In progress',
      Icons.pending_actions_outlined,
      Icons.pending_actions,
    ),
    NavEntry('Settings', Icons.settings_outlined, Icons.settings),
  ]),
];

class NavEntry {
  const NavEntry(this.label, this.icon, this.selectedIcon);
  final String label;
  final IconData icon;
  final IconData selectedIcon;
}

class NavGroup {
  const NavGroup(this.label, this.entries);
  final String label;
  final List<NavEntry> entries;
}

/// Every label the rail can open, and its page.
const List<String> tabLabels = [
  'Home',
  'Library',
  'Playlists',
  'Favorites',
  'In progress',
  'Settings',
];

/// The page a label or a detail push opens. One table, so the rail, the
/// settings shortcuts and the row taps cannot drift apart.
Widget pageFor(String label, {String? argument}) => switch (label) {
  'Home' => const HomePage(),
  'Library' => const LibraryPage(),
  'Playlists' => const PlaylistsPage(),
  'Favorites' => const FavoritesPage(),
  'In progress' => const InProgressPage(),
  'Settings' => const SettingsPage(),
  'Album' => AlbumPage(albumPath: argument ?? ''),
  'Track' => TrackPage(trackPath: argument ?? ''),
  'Artist' => ArtistPage(artistPath: argument ?? ''),
  'Playlist' => PlaylistDetailPage(
    playlistId: int.tryParse(argument ?? '') ?? 0,
  ),
  _ => const HomePage(),
};

/// Push a detail page (album, track, artist, playlist) on top of the current
/// tab — the back button then walks the user's own path back, which is what a
/// library browser is for.
void openDetail(BuildContext context, String label, String argument) {
  Navigator.of(context, rootNavigator: false).push(
    MaterialPageRoute<void>(builder: (_) => pageFor(label, argument: argument)),
  );
}

/// The app shell: a rail on wide windows, a drawer on a phone, the player bar
/// under every page.
class AppShell extends StatelessWidget {
  const AppShell({
    super.key,
    required this.page,
    required this.title,
    required this.onOpenTab,
  });

  final Widget page;
  final String title;
  final void Function(String label) onOpenTab;

  @override
  Widget build(BuildContext context) {
    final state = AppScope.of(context);
    final wide = MediaQuery.sizeOf(context).width >= 900;
    final flat = <NavEntry>[for (final group in navGroups) ...group.entries];

    void toggleTheme() => state.setTheme(
      state.themeMode == ThemeMode.light ? ThemeMode.dark : ThemeMode.light,
    );

    final body = Column(
      children: [
        Expanded(child: page),
        const PlayerBar(),
      ],
    );

    if (!wide) {
      return Scaffold(
        appBar: AppBar(
          title: Text(title),
          actions: [
            IconButton(
              tooltip: state.themeMode == ThemeMode.light
                  ? 'Dark theme'
                  : 'Light theme',
              onPressed: toggleTheme,
              icon: Icon(
                state.themeMode == ThemeMode.light
                    ? Icons.dark_mode_outlined
                    : Icons.light_mode_outlined,
              ),
            ),
          ],
        ),
        drawer: Drawer(
          child: SafeArea(
            child: ListView(
              padding: const EdgeInsets.symmetric(vertical: 8),
              children: [
                for (final group in navGroups) ...[
                  Padding(
                    padding: const EdgeInsets.fromLTRB(16, 14, 16, 4),
                    child: Text(
                      group.label.toUpperCase(),
                      style: const TextStyle(fontSize: 11, letterSpacing: 1.6),
                    ),
                  ),
                  for (final entry in group.entries)
                    ListTile(
                      dense: true,
                      leading: Icon(entry.icon),
                      title: Text(entry.label),
                      onTap: () {
                        Navigator.of(context).pop();
                        onOpenTab(entry.label);
                      },
                    ),
                ],
              ],
            ),
          ),
        ),
        body: body,
      );
    }

    return Scaffold(
      body: Row(
        children: [
          NavigationRail(
            labelType: NavigationRailLabelType.all,
            selectedIndex: flat
                .indexWhere((e) => e.label == title)
                .clamp(0, flat.length - 1),
            onDestinationSelected: (i) => onOpenTab(flat[i].label),
            destinations: [
              for (final entry in flat)
                NavigationRailDestination(
                  icon: Icon(entry.icon),
                  selectedIcon: Icon(entry.selectedIcon),
                  label: Text(entry.label),
                ),
            ],
          ),
          const VerticalDivider(width: 1),
          Expanded(child: body),
        ],
      ),
    );
  }
}

/// A page that shows the app's own error text instead of a blank screen.
class ErrorView extends StatelessWidget {
  const ErrorView({super.key, required this.message, this.onRetry});

  final String message;
  final VoidCallback? onRetry;

  @override
  Widget build(BuildContext context) => Center(
    child: Padding(
      padding: const EdgeInsets.all(32),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          const Icon(Icons.error_outline, size: 32),
          const SizedBox(height: 12),
          Text(message, textAlign: TextAlign.center),
          if (onRetry != null) ...[
            const SizedBox(height: 16),
            FilledButton(onPressed: onRetry, child: const Text('Try again')),
          ],
        ],
      ),
    ),
  );
}

/// The three gates in front of the app: a server address, then a session
/// (setup when the server has no password yet, login when it does).
class Gate extends StatefulWidget {
  const Gate({super.key});

  @override
  State<Gate> createState() => _GateState();
}

class _GateState extends State<Gate> {
  AuthStatus? _status;
  bool _upToDate = false;
  String? _error;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    _refresh();
  }

  Future<void> _refresh() async {
    final state = AppScope.of(context);
    if (!state.connected || _upToDate) return;
    _upToDate = true;
    try {
      final status = await state.authStatus();
      if (mounted) setState(() => _status = status);
    } on ApiException catch (e) {
      if (mounted) {
        setState(() {
          _status = AuthStatus(hasPassword: true, required: true);
          _error = e.message;
        });
      }
    }
  }

  /// Called by the login and setup pages once a session exists: the status is
  /// re-read from the server rather than assumed, so a session the server
  /// rejects cannot leave the client pretending to be signed in.
  void signedIn() {
    setState(() {
      _upToDate = false;
      _status = null;
      _error = null;
    });
  }

  @override
  Widget build(BuildContext context) {
    final state = AppScope.of(context);
    if (!state.connected) {
      return ConnectPage(
        onConnected: () {
          setState(() {
            _upToDate = false;
            _status = null;
          });
        },
      );
    }
    final status = _status;
    if (status == null) {
      return const Scaffold(body: Center(child: CircularProgressIndicator()));
    }
    // The server answers per request: `authenticated` is already true for a
    // client that does not need to sign in at all, so this client never shows
    // a sign-in screen it does not need.
    if (status.authenticated) return const LibraryShell();
    if (status.required && !status.hasPassword) {
      return SetupPasswordPage(onDone: signedIn);
    }
    return LoginPage(error: _error, onSignedIn: signedIn);
  }
}

/// The shell above the app's own pages: it holds the current tab and pushes
/// detail pages onto it.
class LibraryShell extends StatefulWidget {
  const LibraryShell({super.key});

  @override
  State<LibraryShell> createState() => _LibraryShellState();
}

class _LibraryShellState extends State<LibraryShell> {
  final GlobalKey<NavigatorState> _navigator = GlobalKey<NavigatorState>();
  String _tab = 'Home';

  @override
  void initState() {
    super.initState();
    // The library payload is what every page filters, groups and shows covers
    // for; loading it once here means no page pays for it twice.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      AppScope.of(context).refreshLibrary();
      AppScope.of(context).refreshConfig();
    });
  }

  void _openTab(String label) {
    setState(() => _tab = label);
    _navigator.currentState?.pushReplacement(
      MaterialPageRoute<void>(builder: (_) => pageFor(label)),
    );
  }

  @override
  Widget build(BuildContext context) => AppShell(
    title: _tab,
    onOpenTab: _openTab,
    page: Navigator(
      key: _navigator,
      onGenerateRoute: (_) =>
          MaterialPageRoute<void>(builder: (_) => pageFor(_tab)),
    ),
  );
}
