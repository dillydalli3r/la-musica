import 'package:flutter/material.dart';

import '../api.dart';
import '../models.dart';
import '../shell.dart';
import '../state.dart';
import '../widgets/shelves.dart';
import 'home.dart';

/// One artist: the name and its counts, then the releases grid the album cards
/// are built for.
///
/// The artist payload is fetched here instead of read from [AppState.library]
/// because `GET /api/artist` returns the artist's own albums with their tracks
/// — which is what "Play all" queues — and a folder payload the library tree
/// does not carry.
class ArtistPage extends StatefulWidget {
  const ArtistPage({super.key, required this.artistPath});

  final String artistPath;

  @override
  State<ArtistPage> createState() => _ArtistPageState();
}

class _ArtistPageState extends State<ArtistPage> {
  Artist? _artist;
  String? _error;
  bool _loading = true;
  bool _started = false;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    // The scope is unreadable from initState (Flutter asserts on an inherited
    // lookup there), so the first load rides the dependency callback.
    if (_started) return;
    _started = true;
    _load();
  }

  @override
  void didUpdateWidget(covariant ArtistPage oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.artistPath != widget.artistPath) _load();
  }

  Future<void> _load() async {
    final client = AppScope.of(context).api;
    if (client == null) {
      setState(() {
        _loading = false;
        _error = 'This client is not connected to a server.';
      });
      return;
    }
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final artist = await client.artist(widget.artistPath);
      if (!mounted) return;
      setState(() {
        _artist = artist;
        _loading = false;
      });
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _error = e.message;
        _loading = false;
      });
    }
  }

  Future<void> _playAll(Artist artist) async {
    final playback = AppScope.playbackOf(context);
    final tracks = [for (final album in artist.albums) ...albumQueue(album)];
    if (tracks.isEmpty) return;
    await playback.playQueue(tracks);
  }

  /// The tag-derived name. The server's own `display_name` is the first album
  /// artist it found — the folder name carries an MBID suffix a reader should
  /// not see.
  static String _displayName(Artist artist) {
    for (final album in artist.albums) {
      final name = album.artist;
      if (name != null && name.isNotEmpty) return name;
    }
    return artist.name;
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) return const Center(child: CircularProgressIndicator());
    final error = _error;
    if (error != null) return ErrorView(message: error, onRetry: _load);
    final artist = _artist;
    if (artist == null) {
      return ErrorView(
        message: 'The server sent no artist for this path.',
        onRetry: _load,
      );
    }

    final albums = artist.albums;
    final trackCount = albums.fold<int>(
      0,
      (sum, album) => sum + album.tracks.length,
    );
    final hasTracks = trackCount > 0;

    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 16, 16, 24),
      children: [
        _header(context, artist, albums.length, trackCount, hasTracks),
        if (albums.isEmpty)
          const EmptyHint(
            text:
                'Albums appear here once a folder is imported under this artist.',
            icon: Icons.album_outlined,
          )
        else
          GridView.builder(
            // Inside the page's own scroll view: the grid lays out, the page
            // scrolls.
            shrinkWrap: true,
            physics: const NeverScrollableScrollPhysics(),
            padding: const EdgeInsets.symmetric(vertical: 6),
            gridDelegate: const SliverGridDelegateWithMaxCrossAxisExtent(
              maxCrossAxisExtent: 180,
              crossAxisSpacing: 12,
              mainAxisSpacing: 12,
              childAspectRatio: 0.68,
            ),
            itemCount: albums.length,
            itemBuilder: (context, i) => LayoutBuilder(
              // The card fills its tile, so the cover keeps the grid's width
              // instead of a fixed size that would drift from the columns.
              builder: (context, constraints) => AlbumCard(
                album: albums[i],
                width: constraints.maxWidth,
                onOpen: () => openDetail(context, 'Album', albums[i].path),
                onPlay: () => playAlbum(context, albums[i]),
              ),
            ),
          ),
        RecommendationShelf(
          kind: 'artist',
          id: widget.artistPath,
          target: 'albums',
        ),
      ],
    );
  }

  Widget _header(
    BuildContext context,
    Artist artist,
    int albumCount,
    int trackCount,
    bool hasTracks,
  ) {
    final muted = Theme.of(
      context,
    ).colorScheme.onSurface.withValues(alpha: 0.55);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          'ARTIST',
          style: TextStyle(
            fontSize: 11,
            letterSpacing: 1.6,
            fontWeight: FontWeight.w600,
            color: muted,
          ),
        ),
        const SizedBox(height: 4),
        Text(
          _displayName(artist),
          style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w600),
        ),
        const SizedBox(height: 6),
        Text(
          '$albumCount album${albumCount == 1 ? '' : 's'} · '
          '$trackCount track${trackCount == 1 ? '' : 's'}',
          style: TextStyle(fontSize: 13, color: muted),
        ),
        const SizedBox(height: 12),
        Align(
          alignment: Alignment.centerLeft,
          child: FilledButton.icon(
            onPressed: hasTracks ? () => _playAll(artist) : null,
            icon: const Icon(Icons.play_arrow, size: 18),
            label: const Text('Play all'),
          ),
        ),
      ],
    );
  }
}
