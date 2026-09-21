import 'package:flutter/material.dart';

import '../api.dart';
import '../library_meta.dart';
import '../models.dart';
import '../shell.dart';
import '../state.dart';
import '../theme.dart';
import '../widgets/player_bar.dart';
import '../widgets/shelves.dart';
import 'home.dart';

/// Playlists: every manual and smart playlist the server knows, each with its
/// kind, its size and its length, a play button that queues its tracks, and the
/// recommended tracks underneath.
///
/// The list endpoint carries only each playlist's SIZE, so the page reads every
/// playlist's tracks in one pass — the cover, the total length and the play
/// button all come from the tracks themselves, exactly as the React cards do.
class PlaylistsPage extends StatefulWidget {
  const PlaylistsPage({super.key});

  @override
  State<PlaylistsPage> createState() => _PlaylistsPageState();
}

class _PlaylistsPageState extends State<PlaylistsPage> {
  List<Playlist> _playlists = const [];
  final Map<int, List<String>> _tracks = {};
  bool _loading = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    // The ApiClient is reached through an inherited widget, which initState may
    // not read; the first frame is where that lookup is legal.
    WidgetsBinding.instance.addPostFrameCallback((_) => _load());
  }

  Future<void> _load() async {
    if (!mounted) return;
    final client = AppScope.of(context).api;
    if (client == null) return;
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final playlists = await client.playlists();
      final tracks = <int, List<String>>{};
      await Future.wait([
        for (final playlist in playlists)
          client
              .playlist(playlist.id)
              .then<void>(
                (detail) => tracks[playlist.id] = detail.tracks,
                // One unreadable playlist must not blank the whole page: it
                // still renders, with its size and no cover.
                onError: (Object _) {},
              ),
      ]);
      if (mounted) {
        setState(() {
          _playlists = playlists;
          _tracks
            ..clear()
            ..addAll(tracks);
          _loading = false;
        });
      }
    } on ApiException catch (e) {
      if (mounted) {
        setState(() {
          _error = e.message;
          _loading = false;
        });
      }
    }
  }

  /// Queue a playlist from its first track. The paths were read with the list;
  /// a playlist whose read failed is read again here rather than playing
  /// nothing.
  Future<void> _play(Playlist playlist) async {
    final client = AppScope.of(context).api;
    final playback = AppScope.playbackOf(context);
    final messenger = ScaffoldMessenger.of(context);
    if (client == null) return;
    var paths = _tracks[playlist.id];
    if (paths == null) {
      try {
        paths = (await client.playlist(playlist.id)).tracks;
      } on ApiException catch (e) {
        messenger.showSnackBar(SnackBar(content: Text(e.message)));
        return;
      }
      if (!mounted) return;
    }
    final index = LibraryIndex(AppScope.of(context).library);
    final queue = [
      for (final path in paths)
        index.track(path)?.queueEntry ?? looseQueueEntry(path),
    ];
    if (queue.isEmpty) {
      messenger.showSnackBar(
        SnackBar(content: Text('“${playlist.name}” holds no tracks yet.')),
      );
      return;
    }
    await playback.playQueue(queue);
  }

  @override
  Widget build(BuildContext context) {
    final state = AppScope.of(context);
    if (_error != null) return ErrorView(message: _error!, onRetry: _load);
    if (_loading && _playlists.isEmpty) {
      return const Center(child: CircularProgressIndicator());
    }
    final index = LibraryIndex(state.library);
    final manual = [
      for (final playlist in _playlists)
        if (!playlist.isSmart) playlist,
    ];
    final smart = [
      for (final playlist in _playlists)
        if (playlist.isSmart) playlist,
    ];

    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
      children: [
        SectionLabel(
          'Playlists',
          trailing: Text(
            '${_playlists.length} total',
            style: const TextStyle(fontSize: 11),
          ),
        ),
        if (_playlists.isEmpty)
          const EmptyHint(
            text:
                'No playlists yet. Create one in the la musica web client, or import an .m3u8 '
                'file there.',
            icon: Icons.queue_music_outlined,
          )
        else ...[
          for (final playlist in manual) _tile(playlist, index),
          if (smart.isNotEmpty) SectionLabel('Smart playlists'),
          for (final playlist in smart) _tile(playlist, index),
        ],
        const RecommendationShelf(
          kind: 'favorites',
          target: 'tracks',
          title: 'Recommended tracks',
        ),
      ],
    );
  }

  Widget _tile(Playlist playlist, LibraryIndex index) {
    final muted = Theme.of(
      context,
    ).colorScheme.onSurface.withValues(alpha: 0.55);
    final tracks = _tracks[playlist.id] ?? const <String>[];
    final hits = [
      for (final path in tracks) index.track(path),
    ].whereType<TrackHit>().toList();
    final seconds = totalSeconds([for (final hit in hits) hit.track]);
    final size = '${playlist.count} track${playlist.count == 1 ? '' : 's'}';
    final first = hits.isEmpty ? null : hits.first;

    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: ListTile(
        onTap: () => openDetail(context, 'Playlist', '${playlist.id}'),
        leading: CoverArt(
          albumPath: first?.album.path ?? '',
          coverFile: first?.track.coverFile ?? first?.album.coverFile,
          size: 44,
        ),
        title: Text(
          playlist.name,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
        ),
        subtitle: Text(
          [
            playlist.isSmart ? 'Smart playlist' : 'Manual playlist',
            size,
            if (seconds > 0) fmtLength(seconds),
          ].join(' · '),
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: TextStyle(fontSize: 11, color: muted),
        ),
        trailing: IconButton(
          tooltip: 'Play playlist',
          icon: const Icon(Icons.play_arrow, size: 20),
          onPressed: () => _play(playlist),
        ),
      ),
    );
  }
}
