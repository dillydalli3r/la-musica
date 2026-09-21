import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../models.dart';
import '../shell.dart';
import '../state.dart';
import '../theme.dart';
import '../widgets/player_bar.dart';
import '../widgets/shelves.dart';
import '../widgets/storage_card.dart';

/// Home: what the library IS (counts, grade, audit) and three shelves the
/// React home page opens on — Recently added, Best graded, Rediscover — plus
/// the recommendation shelf for the library as a whole.
class HomePage extends StatelessWidget {
  const HomePage({super.key});

  @override
  Widget build(BuildContext context) {
    final state = AppScope.of(context);
    final library = state.library;
    if (library == null) {
      return state.loadingLibrary
          ? const Center(child: CircularProgressIndicator())
          : const EmptyHint(
              text: 'No library loaded yet.',
              icon: Icons.library_music_outlined,
            );
    }
    final albums = [for (final artist in library.artists) ...artist.albums];
    final rated = [...albums]
      ..sort((a, b) => (b.gradePct ?? -1).compareTo(a.gradePct ?? -1));
    final recent = [...albums]
      ..sort((a, b) => (b.date ?? '').compareTo(a.date ?? ''));
    final waiting = albums.where((a) => a.pending).toList();
    // "Rediscover" means the user owns it and forgot it; a folder whose audio
    // is still being hunted is not that, so the framework albums sit on their
    // own shelf instead.
    final random = [...albums.where((a) => !a.pending)]
      ..shuffle(math.Random(7));
    final passed = albums.where((a) => a.pass).length;

    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
      children: [
        SectionLabel(
          'Library',
          trailing: Text(
            '${library.trackCount} tracks · ${albums.length} albums',
          ),
        ),
        Wrap(
          spacing: 10,
          runSpacing: 10,
          children: [
            _Stat(label: 'Artists', value: '${library.artists.length}'),
            _Stat(label: 'Albums', value: '${albums.length}'),
            _Stat(label: 'Tracks', value: '${library.trackCount}'),
            _Stat(label: 'Albums passing', value: '$passed/${albums.length}'),
          ],
        ),
        if (library.error != null) ...[
          const SizedBox(height: 12),
          Text(
            library.error!,
            style: const TextStyle(fontSize: 12, color: Color(0xFFF87171)),
          ),
        ],
        const StorageCard(),
        AlbumShelf(
          title: 'Recently added',
          albums: recent.take(12).toList(),
          onOpen: (album) => openDetail(context, 'Album', album.path),
          onPlay: (album) => playAlbum(context, album),
        ),
        // The one shelf that answers "what am I still waiting for". A pending
        // album also rides along where it would otherwise be listed — Recent,
        // because its folder is the newest thing in the library, and its
        // artist's own page — but a framework folder only ever mixed in with
        // complete albums is one the user has to hunt for (the React home
        // draws the same shelf from the server's payload).
        AlbumShelf(
          title: 'Waiting for its audio',
          albums: waiting.take(12).toList(),
          onOpen: (album) => openDetail(context, 'Album', album.path),
          onPlay: (album) => playAlbum(context, album),
        ),
        AlbumShelf(
          title: 'Best graded',
          albums: rated.where((a) => a.gradePct != null).take(12).toList(),
          onOpen: (album) => openDetail(context, 'Album', album.path),
          onPlay: (album) => playAlbum(context, album),
        ),
        const RecommendationShelf(
          kind: 'library',
          target: 'albums',
          title: 'Recommended',
        ),
        AlbumShelf(
          title: 'Rediscover',
          albums: random.take(12).toList(),
          onOpen: (album) => openDetail(context, 'Album', album.path),
          onPlay: (album) => playAlbum(context, album),
        ),
      ],
    );
  }
}

class _Stat extends StatelessWidget {
  const _Stat({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
    decoration: BoxDecoration(
      color: Theme.of(context).colorScheme.surfaceContainerHighest,
      border: Border.all(color: Theme.of(context).dividerColor),
      borderRadius: BorderRadius.circular(10),
    ),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          value,
          style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w600),
        ),
        Text(
          label.toUpperCase(),
          style: TextStyle(
            fontSize: 10,
            letterSpacing: 1.4,
            color: Theme.of(
              context,
            ).colorScheme.onSurface.withValues(alpha: 0.5),
          ),
        ),
      ],
    ),
  );
}

/// The recommendation shelf: one POST-free GET to `/api/recommend` for a
/// context (a page's seed kind) and one rendering for both answers — album
/// cards or track rows — so every page shows recommendations the same way.
class RecommendationShelf extends StatefulWidget {
  const RecommendationShelf({
    super.key,
    required this.kind,
    this.id,
    this.target = 'albums',
    this.title = 'More like this',
  });

  final String kind;
  final String? id;
  final String target;
  final String title;

  @override
  State<RecommendationShelf> createState() => _RecommendationShelfState();
}

class _RecommendationShelfState extends State<RecommendationShelf> {
  List<Recommendation> _items = const [];
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    // The client comes from an inherited widget, which initState may not read;
    // the first frame is where an inherited lookup is legal.
    WidgetsBinding.instance.addPostFrameCallback((_) => _load());
  }

  @override
  void didUpdateWidget(covariant RecommendationShelf old) {
    super.didUpdateWidget(old);
    if (old.kind != widget.kind || old.id != widget.id) _load();
  }

  Future<void> _load() async {
    if (!mounted) return;
    final client = AppScope.of(context).api;
    if (client == null) return;
    setState(() => _loading = true);
    try {
      final items = await client.recommendations(
        kind: widget.kind,
        id: widget.id,
        target: widget.target,
        limit: widget.target == 'tracks' ? 20 : 12,
      );
      if (mounted) setState(() => _items = items);
    } catch (_) {
      // A shelf that cannot load is an absent shelf, never an error page.
      if (mounted) setState(() => _items = const []);
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_loading || _items.isEmpty) return const SizedBox.shrink();
    if (widget.target == 'tracks') {
      return Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SectionLabel(widget.title),
          for (final item in _items)
            TrackRow(
              track: Track(
                path: item.path ?? '',
                file: item.file ?? '',
                title: item.title,
                advisory: item.advisory,
                artist: item.artist,
                album: item.album,
              ),
              albumPath: item.albumPath ?? '',
              onTap: () {
                final path = item.path;
                if (path != null && path.isNotEmpty) {
                  openDetail(context, 'Track', path);
                }
              },
              trailing: Tooltip(
                message: item.reason ?? 'Recommended',
                child: IconButton(
                  icon: const Icon(Icons.play_arrow, size: 18),
                  onPressed: () {
                    final path = item.path;
                    if (path != null && path.isNotEmpty) {
                      AppScope.playbackOf(context).playPath(
                        path,
                        queueAfter: [
                          for (final r in _items)
                            {
                              'path': r.path,
                              'file': r.file,
                              'title': r.title,
                              'advisory': r.advisory,
                              'artist': r.artist,
                              'album': r.album,
                              'albumPath': r.albumPath,
                            },
                        ],
                      );
                    }
                  },
                ),
              ),
            ),
        ],
      );
    }
    final library = AppScope.of(context).library;
    final albums = <Album>[];
    for (final item in _items) {
      final path = item.path;
      if (path == null) continue;
      for (final artist in library?.artists ?? const <Artist>[]) {
        for (final album in artist.albums) {
          if (album.path == path) albums.add(album);
        }
      }
    }
    if (albums.isEmpty) return const SizedBox.shrink();
    return AlbumShelf(
      title: widget.title,
      albums: albums,
      onOpen: (album) => openDetail(context, 'Album', album.path),
      onPlay: (album) => playAlbum(context, album),
    );
  }
}
