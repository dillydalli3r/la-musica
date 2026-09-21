import 'package:flutter/material.dart';

import '../models.dart';
import '../state.dart';
import '../theme.dart';
import 'player_bar.dart';

/// An album card: cover, album name, artist and year — the tile every shelf
/// and grid is built from. Tapping opens the album; the play badge queues it.
class AlbumCard extends StatelessWidget {
  const AlbumCard({
    super.key,
    required this.album,
    this.width = 150,
    this.onOpen,
    this.onPlay,
  });

  final Album album;
  final double width;
  final VoidCallback? onOpen;
  final VoidCallback? onPlay;

  @override
  Widget build(BuildContext context) {
    final muted = Theme.of(
      context,
    ).colorScheme.onSurface.withValues(alpha: 0.55);
    return SizedBox(
      width: width,
      child: InkWell(
        onTap: onOpen,
        borderRadius: BorderRadius.circular(8),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Stack(
              children: [
                CoverArt(
                  albumPath: album.path,
                  coverFile: album.coverFile,
                  size: width,
                  radius: 8,
                ),
                if (onPlay != null)
                  Positioned(
                    left: 8,
                    bottom: 8,
                    child: Material(
                      color: Colors.black.withValues(alpha: 0.6),
                      shape: const CircleBorder(),
                      child: InkWell(
                        customBorder: const CircleBorder(),
                        onTap: onPlay,
                        child: const Padding(
                          padding: EdgeInsets.all(8),
                          child: Icon(
                            Icons.play_arrow,
                            size: 18,
                            color: Colors.white,
                          ),
                        ),
                      ),
                    ),
                  ),
              ],
            ),
            const SizedBox(height: 6),
            Row(
              children: [
                Expanded(
                  child: Text(
                    album.title,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      fontSize: 13,
                      fontWeight: FontWeight.w500,
                    ),
                  ),
                ),
                const SizedBox(width: 4),
                AdvisoryMark(value: album.advisory),
              ],
            ),
            Text(
              [
                album.artist,
                album.year,
              ].whereType<String>().where((s) => s.isNotEmpty).join(' · '),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(fontSize: 11, color: muted),
            ),
          ],
        ),
      ),
    );
  }
}

/// A horizontal shelf of albums with a heading — Recently added, Best graded,
/// Rediscover, and the recommendation shelves.
class AlbumShelf extends StatelessWidget {
  const AlbumShelf({
    super.key,
    required this.title,
    required this.albums,
    this.onOpen,
    this.onPlay,
  });

  final String title;
  final List<Album> albums;
  final void Function(Album album)? onOpen;
  final void Function(Album album)? onPlay;

  @override
  Widget build(BuildContext context) {
    if (albums.isEmpty) return const SizedBox.shrink();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        SectionLabel(title),
        SizedBox(
          height: 218,
          child: ListView.separated(
            scrollDirection: Axis.horizontal,
            itemCount: albums.length,
            separatorBuilder: (_, __) => const SizedBox(width: 12),
            itemBuilder: (context, i) => AlbumCard(
              album: albums[i],
              onOpen: onOpen == null ? null : () => onOpen!(albums[i]),
              onPlay: onPlay == null ? null : () => onPlay!(albums[i]),
            ),
          ),
        ),
      ],
    );
  }
}

/// The empty state every page shows instead of a blank screen.
class EmptyHint extends StatelessWidget {
  const EmptyHint({
    super.key,
    required this.text,
    this.icon = Icons.info_outline,
  });

  final String text;
  final IconData icon;

  @override
  Widget build(BuildContext context) => Center(
    child: Padding(
      padding: const EdgeInsets.all(32),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(
            icon,
            size: 28,
            color: Theme.of(
              context,
            ).colorScheme.onSurface.withValues(alpha: 0.4),
          ),
          const SizedBox(height: 10),
          Text(
            text,
            textAlign: TextAlign.center,
            style: TextStyle(
              fontSize: 13,
              color: Theme.of(
                context,
              ).colorScheme.onSurface.withValues(alpha: 0.6),
            ),
          ),
        ],
      ),
    ),
  );
}

/// An album's tracks as player-queue entries.
///
/// The queue carries the metadata the player bar shows while a track plays
/// (title, advisory, cover), so every path that queues an album — playing it,
/// appending it, an artist's play-all — must send the same shape.
List<Map<String, dynamic>> albumQueue(Album album) => [
  for (final track in album.tracks)
    {
      ...track.toQueueJson(),
      'albumPath': album.path,
      'album': album.title,
      'artist': album.artist,
      'coverFile': track.coverFile,
      'albumCover': album.coverFile,
    },
];

/// The folder above [path] — an album's artist folder, a track's album folder.
/// Null when [path] has no parent, so a caller hides the link that needs it
/// instead of opening the drive root.
String? parentFolder(String path) {
  final cut = path.replaceAll('\\', '/').lastIndexOf('/');
  return cut <= 0 ? null : path.substring(0, cut);
}

/// Queue a whole album and start it — the shared action behind every play
/// badge, so the queue always carries the advisory the player bar shows.
Future<void> playAlbum(
  BuildContext context,
  Album album, {
  int start = 0,
}) async {
  final playback = AppScope.playbackOf(context);
  await playback.playQueue(albumQueue(album), start: start);
}
