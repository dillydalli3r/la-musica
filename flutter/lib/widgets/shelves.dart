import 'package:flutter/material.dart';

import '../models.dart';
import '../state.dart';
import '../theme.dart';
import 'player_bar.dart';

/// The pending marker's copy — the same sentences the React client draws
/// (`web/src/locales/en.ts`, the `pending.*` keys), so the two front ends
/// describe the same album the same way.
const String pendingTitle = 'Waiting for its audio';
const String pendingShort = 'not downloaded yet';
const String pendingNoteCopy =
    'The folder exists and its page content — the artist image and '
    'descriptions, the album description and the ranked cover candidates — is '
    'already fetched. The search for the audio is what is still running.';
const String _pendingNoSearch = 'nothing is searching for it right now';
const String _pendingQueued = 'waiting its turn in the wish queue';
const String _pendingNotFound = 'not found yet — retry it from Soulseek';
const String _pendingFailed = 'the last search failed';

/// What a framework album's marker SAYS: the short label a row draws, the
/// state of the search behind it and the full sentence its tooltip carries.
///
/// One function, so the grid, the artist page, the library list, Home and the
/// album page can never describe the same album differently — the split the
/// React client makes (`web/src/components/Badges.tsx::pendingSummary`), in
/// the same words. Every part of it is the server's own data: [pendingReason]
/// is what the folder is waiting for, and the wish is the queue's state — how
/// many attempts ran, the reason a run left behind, and whether another
/// search is even coming. Null for a complete album: there is no marker to
/// draw and nothing to say.
class PendingNote {
  const PendingNote({
    required this.short,
    required this.state,
    required this.reason,
    required this.note,
    required this.full,
  });

  /// The label a row with room draws beside the mark.
  final String short;
  /// The state of the search, in one sentence.
  final String state;
  /// What the folder is waiting for; null when the server said nothing.
  final String? reason;
  /// The album page's paragraph about what is already there.
  final String note;
  /// "Waiting for its audio · what it waits for · the state" — the tooltip.
  final String full;
}

PendingNote? pendingNote(Album album) {
  if (!album.pending) return null;
  final w = album.wish;
  final String state;
  if (w == null) {
    state = _pendingNoSearch;
  } else if (w.status == 'not_found') {
    state = _pendingNotFound;
  } else if (w.status == 'failed') {
    state = w.reason.isNotEmpty ? w.reason : _pendingFailed;
  } else if (w.terminal || w.dueIn == null) {
    state = w.reason.isNotEmpty ? w.reason : _pendingNoSearch;
  } else if (w.attempts == 0 && w.status == 'wanted') {
    // Recorded and waiting its turn: nothing has searched it yet, so "attempt
    // 1" would claim a run that has not happened.
    state = _pendingQueued;
  } else {
    final minutes = ((w.dueIn ?? 0) / 60).round();
    state =
        'searching — attempt ${w.attempts + 1}, '
        'next try in ${minutes < 1 ? 1 : minutes} min';
  }
  final reason = album.pendingReason;
  final waitingFor = (reason != null && reason.isNotEmpty) ? reason : null;
  final full = [
    pendingTitle,
    if (waitingFor != null) waitingFor,
    state,
  ].join(' · ');
  return PendingNote(
    short: pendingShort,
    state: state,
    reason: waitingFor,
    note: pendingNoteCopy,
    full: full,
  );
}

/// A framework album's mark: the app's own boxed-badge idiom (the one the
/// advisory mark draws with), amber while a search is still coming and red
/// once nothing will search the folder again — "needs attention" in the app's
/// own tokens, not a second palette.
///
/// It renders NOTHING for a complete album, so any row can mount it
/// unconditionally, and its sentence rides on the tooltip — hover on a
/// desktop, long-press on touch — where the web client puts it too, because a
/// tile has no room for the whole sentence.
class PendingMark extends StatelessWidget {
  const PendingMark(
    this.album, {
    super.key,
    this.size = 14,
    this.label = false,
  });

  final Album album;
  /// The mark's edge, the same 14 the advisory mark draws at.
  final double size;
  /// Draw the short state beside the mark, for a row with room to say it.
  final bool label;

  @override
  Widget build(BuildContext context) {
    final note = pendingNote(album);
    if (note == null) return const SizedBox.shrink();
    final w = album.wish;
    final stuck = w == null || w.terminal || w.status == 'failed';
    final edge = stuck ? const Color(0xFFEF4444) : const Color(0xFFF59E0B);
    final fill = stuck ? const Color(0xFF450A0A) : const Color(0xFF451A03);
    final glyph = stuck ? const Color(0xFFFCA5A5) : const Color(0xFFFBBF24);
    return Tooltip(
      message: note.full,
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Container(
            width: size,
            height: size,
            alignment: Alignment.center,
            decoration: BoxDecoration(
              color: fill,
              border: Border.all(color: edge),
              borderRadius: BorderRadius.circular(3),
            ),
            child: Icon(Icons.schedule, size: size * 0.72, color: glyph),
          ),
          if (label) ...[
            const SizedBox(width: 5),
            Text(note.short, style: TextStyle(fontSize: 10, color: glyph)),
          ],
        ],
      ),
    );
  }
}

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
    // A framework album has no audio, so its play badge is OFF rather than a
    // control that starts nothing — the tooltip carries the sentence saying
    // why (web: components/AlbumCard.tsx).
    final note = pendingNote(album);
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
                    child: Tooltip(
                      // The reason the badge is off, on the one control that
                      // would have started an empty queue.
                      message: note?.full ?? 'Play album',
                      child: Material(
                        color: Colors.black.withValues(alpha: 0.6),
                        shape: const CircleBorder(),
                        child: InkWell(
                          customBorder: const CircleBorder(),
                          onTap: note == null ? onPlay : null,
                          child: Padding(
                            padding: const EdgeInsets.all(8),
                            child: Icon(
                              Icons.play_arrow,
                              size: 18,
                              color: note == null
                                  ? Colors.white
                                  : Colors.white.withValues(alpha: 0.5),
                            ),
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
                // The same marker every album-shaped surface carries; nothing
                // at all for a complete album.
                const SizedBox(width: 4),
                PendingMark(album),
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
