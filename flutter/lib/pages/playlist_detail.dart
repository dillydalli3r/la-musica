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

/// One playlist: what it is (kind, size, total length), the two ways to start
/// it — from the top, or after whatever is already playing — and its tracks in
/// playlist order.
class PlaylistDetailPage extends StatefulWidget {
  const PlaylistDetailPage({super.key, required this.playlistId});

  final int playlistId;

  @override
  State<PlaylistDetailPage> createState() => _PlaylistDetailPageState();
}

class _PlaylistDetailPageState extends State<PlaylistDetailPage> {
  Playlist? _playlist;
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
      final playlist = await client.playlist(widget.playlistId);
      if (mounted) {
        setState(() {
          _playlist = playlist;
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

  /// The queue this playlist makes: one entry per stored path, in playlist
  /// order. A path the library no longer holds is queued as itself, so the
  /// queue still matches the list the user is looking at.
  List<Map<String, dynamic>> _queue() {
    final index = LibraryIndex(AppScope.of(context).library);
    return [
      for (final path in _playlist?.tracks ?? const <String>[])
        index.track(path)?.queueEntry ?? looseQueueEntry(path),
    ];
  }

  Future<void> _play({int start = 0}) async {
    final playback = AppScope.playbackOf(context);
    final queue = _queue();
    if (queue.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('This playlist holds no tracks yet.')),
      );
      return;
    }
    await playback.playQueue(queue, start: start);
  }

  Future<void> _queueNext() async {
    final playback = AppScope.playbackOf(context);
    final messenger = ScaffoldMessenger.of(context);
    final queue = _queue();
    if (queue.isEmpty) {
      messenger.showSnackBar(
        const SnackBar(content: Text('This playlist holds no tracks yet.')),
      );
      return;
    }
    // An empty queue has nothing to play "next": that case starts the playlist.
    final afterCurrent = playback.hasTrack;
    await playback.queueAdd(queue, next: true);
    messenger.showSnackBar(
      SnackBar(
        content: Text(
          afterCurrent
              ? 'Playing ${queue.length} track${queue.length == 1 ? '' : 's'} next'
              : 'Playing the playlist',
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final state = AppScope.of(context);
    if (_error != null) return ErrorView(message: _error!, onRetry: _load);
    final playlist = _playlist;
    if (playlist == null) {
      return _loading
          ? const Center(child: CircularProgressIndicator())
          : const EmptyHint(
              text: 'This playlist could not be read.',
              icon: Icons.queue_music_outlined,
            );
    }
    final muted = Theme.of(
      context,
    ).colorScheme.onSurface.withValues(alpha: 0.55);
    final index = LibraryIndex(state.library);
    final hits = [for (final path in playlist.tracks) index.track(path)];
    final known = hits.whereType<TrackHit>().toList();
    final seconds = totalSeconds([for (final hit in known) hit.track]);
    final first = known.isEmpty ? null : known.first;
    final size =
        '${playlist.tracks.length} track${playlist.tracks.length == 1 ? '' : 's'}';

    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
      children: [
        Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            CoverArt(
              albumPath: first?.album.path ?? '',
              coverFile: first?.track.coverFile ?? first?.album.coverFile,
              size: 96,
              radius: 8,
            ),
            const SizedBox(width: 14),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    (playlist.isSmart ? 'Smart playlist' : 'Manual playlist')
                        .toUpperCase(),
                    style: TextStyle(
                      fontSize: 10,
                      letterSpacing: 1.4,
                      fontWeight: FontWeight.w600,
                      color: muted,
                    ),
                  ),
                  const SizedBox(height: 4),
                  Text(
                    playlist.name,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      fontSize: 20,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const SizedBox(height: 4),
                  Text(
                    '$size · ${seconds > 0 ? fmtLength(seconds) : '—'}',
                    style: TextStyle(fontSize: 12, color: muted),
                  ),
                ],
              ),
            ),
          ],
        ),
        const SizedBox(height: 14),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            FilledButton.icon(
              onPressed: () => _play(),
              icon: const Icon(Icons.play_arrow, size: 18),
              label: const Text('Play from the top'),
            ),
            OutlinedButton.icon(
              onPressed: _queueNext,
              icon: const Icon(Icons.playlist_play, size: 18),
              label: const Text('Queue next'),
            ),
          ],
        ),
        SectionLabel(
          'Tracks',
          trailing: Text(size, style: const TextStyle(fontSize: 11)),
        ),
        if (playlist.tracks.isEmpty)
          const EmptyHint(
            text:
                'This playlist holds no tracks yet — add some from an album or a track page.',
            icon: Icons.queue_music_outlined,
          )
        else
          for (var i = 0; i < playlist.tracks.length; i++)
            TrackRow(
              track: hits[i]?.track ?? looseTrack(playlist.tracks[i]),
              albumPath: hits[i]?.album.path ?? '',
              onTap: () => openDetail(context, 'Track', playlist.tracks[i]),
              trailing: hits[i] == null
                  ? const Text('missing', style: TextStyle(fontSize: 11))
                  : IconButton(
                      tooltip: 'Play from here',
                      icon: const Icon(Icons.play_arrow, size: 18),
                      onPressed: () => _play(start: i),
                    ),
            ),
        RecommendationShelf(
          kind: 'playlist',
          id: '${widget.playlistId}',
          target: 'tracks',
          title: 'Recommended tracks',
        ),
      ],
    );
  }
}
