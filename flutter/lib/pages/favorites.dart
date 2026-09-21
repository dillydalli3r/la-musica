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

/// Favorites, in the two lists that share them: the liked tracks, and the
/// album / artist folders the user hearted. A like or a favourite whose file or
/// folder is gone is shown as missing rather than dropped — a vanished row is
/// exactly what the user needs to see.
class FavoritesPage extends StatefulWidget {
  const FavoritesPage({super.key});

  @override
  State<FavoritesPage> createState() => _FavoritesPageState();
}

class _FavoritesPageState extends State<FavoritesPage> {
  int _tab = 0;
  List<String> _likes = const [];
  Favorites _favorites = Favorites();
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
      final likes = await client.likes();
      final favorites = await client.favorites();
      if (mounted) {
        setState(() {
          _likes = likes;
          _favorites = favorites;
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

  @override
  Widget build(BuildContext context) {
    final state = AppScope.of(context);
    if (_error != null) return ErrorView(message: _error!, onRetry: _load);
    if (_loading && _likes.isEmpty && _favorites.albums.isEmpty) {
      return const Center(child: CircularProgressIndicator());
    }
    final index = LibraryIndex(state.library);

    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 12, 16, 4),
          child: SegmentedButton<int>(
            segments: const [
              ButtonSegment(
                value: 0,
                label: Text('Liked tracks'),
                icon: Icon(Icons.favorite_border, size: 16),
              ),
              ButtonSegment(
                value: 1,
                label: Text('Albums & artists'),
                icon: Icon(Icons.album_outlined, size: 16),
              ),
            ],
            selected: {_tab},
            onSelectionChanged: (selection) =>
                setState(() => _tab = selection.first),
          ),
        ),
        Expanded(child: _tab == 0 ? _likedTracks(index) : _favoritesTab(index)),
      ],
    );
  }

  // ---- liked tracks -------------------------------------------------------

  Widget _likedTracks(LibraryIndex index) {
    final muted = Theme.of(
      context,
    ).colorScheme.onSurface.withValues(alpha: 0.55);
    final hits = [for (final path in _likes) index.track(path)];
    final queue = [
      for (final hit in hits)
        if (hit != null) hit.queueEntry,
    ];
    // The queue holds only the likes that are still playable, so each row needs
    // its own position in it — a missing path shifts nothing.
    final starts = <int>[];
    var playable = 0;
    for (final hit in hits) {
      starts.add(hit == null ? -1 : playable++);
    }

    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 4, 16, 24),
      children: [
        SectionLabel(
          'Liked tracks',
          trailing: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text('${_likes.length}', style: const TextStyle(fontSize: 11)),
              if (queue.isNotEmpty) ...[
                const SizedBox(width: 8),
                TextButton.icon(
                  onPressed: () =>
                      AppScope.playbackOf(context).playQueue(queue),
                  icon: const Icon(Icons.play_arrow, size: 16),
                  label: const Text('Play all'),
                ),
              ],
            ],
          ),
        ),
        if (_likes.isEmpty)
          const EmptyHint(
            text:
                'No liked tracks yet — heart a track in the la musica web client and it shows '
                'up here.',
            icon: Icons.favorite_border,
          )
        else
          for (var i = 0; i < _likes.length; i++)
            _likedRow(i, hits[i], queue, starts[i], muted),
        const RecommendationShelf(kind: 'favorites', target: 'tracks'),
      ],
    );
  }

  Widget _likedRow(
    int i,
    TrackHit? hit,
    List<Map<String, dynamic>> queue,
    int start,
    Color muted,
  ) {
    final path = _likes[i];
    if (hit == null || start < 0) {
      // Kept visible: the path is what the user can act on, and the library
      // failing to know it is the information.
      return TrackRow(
        track: looseTrack(path),
        trailing: const Text('missing', style: TextStyle(fontSize: 11)),
      );
    }
    return TrackRow(
      track: hit.track,
      albumPath: hit.album.path,
      onTap: () => openDetail(context, 'Track', path),
      trailing: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 200),
            child: Text(
              [
                hit.artistName,
                hit.album.title,
              ].where((s) => s.isNotEmpty).join(' · '),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              textAlign: TextAlign.right,
              style: TextStyle(fontSize: 11, color: muted),
            ),
          ),
          IconButton(
            tooltip: 'Play this track',
            icon: const Icon(Icons.play_arrow, size: 18),
            onPressed: () =>
                AppScope.playbackOf(context).playQueue(queue, start: start),
          ),
        ],
      ),
    );
  }

  // ---- albums & artists ---------------------------------------------------

  Widget _favoritesTab(LibraryIndex index) {
    final albumHits = [
      for (final path in _favorites.albums) index.albums[path],
    ];
    final albums = albumHits.whereType<Album>().toList();
    final lostAlbums = [
      for (var i = 0; i < _favorites.albums.length; i++)
        if (albumHits[i] == null) _favorites.albums[i],
    ];
    final artistHits = [
      for (final path in _favorites.artists) index.artists[path],
    ];
    final artists = artistHits.whereType<Artist>().toList();
    final lostArtists = [
      for (var i = 0; i < _favorites.artists.length; i++)
        if (artistHits[i] == null) _favorites.artists[i],
    ];

    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 4, 16, 24),
      children: [
        SectionLabel(
          'Favorite albums',
          trailing: Text(
            '${albums.length}',
            style: const TextStyle(fontSize: 11),
          ),
        ),
        if (albums.isEmpty && lostAlbums.isEmpty)
          const EmptyHint(
            text:
                'No favorite albums yet — heart an album in the la musica web client and it '
                'shows up here.',
            icon: Icons.album_outlined,
          )
        else
          Wrap(
            spacing: 12,
            runSpacing: 12,
            children: [
              for (final album in albums)
                AlbumCard(
                  album: album,
                  onOpen: () => openDetail(context, 'Album', album.path),
                  onPlay: () => playAlbum(context, album),
                ),
            ],
          ),
        for (final path in lostAlbums) _lostFolder(path, 'album'),
        SectionLabel(
          'Favorite artists',
          trailing: Text(
            '${artists.length}',
            style: const TextStyle(fontSize: 11),
          ),
        ),
        if (artists.isEmpty && lostArtists.isEmpty)
          const EmptyHint(
            text:
                'No favorite artists yet — heart an artist in the la musica web client and it '
                'shows up here.',
            icon: Icons.person_outline,
          )
        else
          for (final artist in artists)
            ListTile(
              dense: true,
              leading: IconButton(
                tooltip: 'Play this artist',
                icon: const Icon(Icons.play_arrow, size: 20),
                onPressed: () => _playArtist(artist),
              ),
              title: Text(
                artist.name,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
              ),
              subtitle: Text(
                '${artist.albums.length} album${artist.albums.length == 1 ? '' : 's'} · '
                '${fmtLength(totalSeconds([for (final album in artist.albums) ...album.tracks]))}',
                style: const TextStyle(fontSize: 11),
              ),
              onTap: () => openDetail(context, 'Artist', artist.path),
            ),
        for (final path in lostArtists) _lostFolder(path, 'artist'),
        const RecommendationShelf(kind: 'favorites', target: 'albums'),
      ],
    );
  }

  Widget _lostFolder(String path, String kind) {
    final muted = Theme.of(
      context,
    ).colorScheme.onSurface.withValues(alpha: 0.55);
    return ListTile(
      dense: true,
      leading: Icon(Icons.help_outline, size: 20, color: muted),
      title: Text(fileName(path), maxLines: 1, overflow: TextOverflow.ellipsis),
      subtitle: Text(
        'favourite $kind — this folder is not in the library any more',
        style: TextStyle(fontSize: 11, color: muted),
      ),
    );
  }

  Future<void> _playArtist(Artist artist) async {
    final playback = AppScope.playbackOf(context);
    final index = LibraryIndex(AppScope.of(context).library);
    final queue = [
      for (final album in artist.albums)
        for (final track in album.tracks)
          index.track(track.path)?.queueEntry ?? track.toQueueJson(),
    ];
    if (queue.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text('“${artist.name}” holds no tracks in the library.'),
        ),
      );
      return;
    }
    await playback.playQueue(queue);
  }
}
