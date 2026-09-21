import 'package:flutter/material.dart';

import '../models.dart';
import '../shell.dart';
import '../state.dart';
import '../theme.dart';
import '../widgets/player_bar.dart';
import '../widgets/shelves.dart';

/// The library: one searchable list of albums with their artist, the same
/// payload every other page filters. Grouping is by artist because that is how
/// a music library is organised on disk, and the search box filters across
/// album, artist and track titles at once.
class LibraryPage extends StatefulWidget {
  const LibraryPage({super.key});

  @override
  State<LibraryPage> createState() => _LibraryPageState();
}

class _LibraryPageState extends State<LibraryPage> {
  final TextEditingController _query = TextEditingController();
  String _filter = '';

  @override
  void dispose() {
    _query.dispose();
    super.dispose();
  }

  bool _matches(Album album, String needle) {
    if (needle.isEmpty) return true;
    final haystack = [
      album.title,
      album.artist ?? '',
      album.folderName,
      for (final track in album.tracks) track.displayTitle,
    ].join(' ').toLowerCase();
    return haystack.contains(needle);
  }

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
    final needle = _filter.trim().toLowerCase();
    final groups = <MapEntry<Artist, List<Album>>>[];
    for (final artist in library.artists) {
      final albums = artist.albums.where((a) => _matches(a, needle)).toList();
      if (albums.isNotEmpty) groups.add(MapEntry(artist, albums));
    }
    final total = groups.fold<int>(0, (sum, g) => sum + g.value.length);

    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 12, 16, 8),
          child: Row(
            children: [
              Expanded(
                child: TextField(
                  controller: _query,
                  decoration: InputDecoration(
                    hintText: 'Filter albums, artists, tracks',
                    prefixIcon: const Icon(Icons.search, size: 18),
                    suffixIcon: _filter.isEmpty
                        ? null
                        : IconButton(
                            icon: const Icon(Icons.close, size: 16),
                            onPressed: () {
                              _query.clear();
                              setState(() => _filter = '');
                            },
                          ),
                  ),
                  onChanged: (value) => setState(() => _filter = value),
                ),
              ),
              const SizedBox(width: 10),
              IconButton(
                tooltip: 'Reload the library from the server',
                onPressed: state.loadingLibrary ? null : state.refreshLibrary,
                icon: state.loadingLibrary
                    ? const SizedBox(
                        height: 16,
                        width: 16,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : const Icon(Icons.refresh),
              ),
            ],
          ),
        ),
        Expanded(
          child: total == 0
              ? const EmptyHint(text: 'No album matches that filter.')
              : ListView.builder(
                  padding: const EdgeInsets.fromLTRB(16, 0, 16, 24),
                  itemCount: groups.length,
                  itemBuilder: (context, i) {
                    final artist = groups[i].key;
                    final albums = groups[i].value;
                    return Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        SectionLabel(
                          artist.name,
                          trailing: Text(
                            '${albums.length} album${albums.length == 1 ? '' : 's'}',
                            style: const TextStyle(fontSize: 11),
                          ),
                        ),
                        for (final album in albums)
                          _AlbumRow(
                            album: album,
                            onOpen: () =>
                                openDetail(context, 'Album', album.path),
                            onPlay: () => playAlbum(context, album),
                            onTrack: (track) =>
                                openDetail(context, 'Track', track.path),
                          ),
                      ],
                    );
                  },
                ),
        ),
      ],
    );
  }
}

/// One album in the library list: cover, album line, and its tracks folded
/// under it. Collapsed by default — a library of thousands of albums must
/// open cheap, and a row expands to the album's own list on demand.
class _AlbumRow extends StatefulWidget {
  const _AlbumRow({
    required this.album,
    this.onOpen,
    this.onPlay,
    this.onTrack,
  });

  final Album album;
  final VoidCallback? onOpen;
  final VoidCallback? onPlay;
  final void Function(Track track)? onTrack;

  @override
  State<_AlbumRow> createState() => _AlbumRowState();
}

class _AlbumRowState extends State<_AlbumRow> {
  bool _expanded = false;

  @override
  Widget build(BuildContext context) {
    final album = widget.album;
    final muted = Theme.of(
      context,
    ).colorScheme.onSurface.withValues(alpha: 0.55);
    final subtitle = [
      album.artist,
      album.year,
      '${album.tracks.length} tracks',
      if (album.gradePct != null) '${album.gradePct!.round()}%',
      if (album.auditSummary != null) album.auditSummary!,
    ].whereType<String>().where((s) => s.isNotEmpty).join(' · ');

    return Column(
      children: [
        InkWell(
          onTap: () => setState(() => _expanded = !_expanded),
          child: Padding(
            padding: const EdgeInsets.symmetric(vertical: 6, horizontal: 4),
            child: Row(
              children: [
                Icon(
                  _expanded ? Icons.expand_more : Icons.chevron_right,
                  size: 18,
                  color: muted,
                ),
                const SizedBox(width: 6),
                CoverArt(
                  albumPath: album.path,
                  coverFile: album.coverFile,
                  size: 44,
                ),
                const SizedBox(width: 10),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          Flexible(
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
                          const SizedBox(width: 6),
                          AdvisoryMark(value: album.advisory),
                        ],
                      ),
                      Text(
                        subtitle,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(fontSize: 11, color: muted),
                      ),
                    ],
                  ),
                ),
                IconButton(
                  tooltip: 'Play album',
                  onPressed: widget.onPlay,
                  icon: const Icon(Icons.play_arrow, size: 20),
                ),
                IconButton(
                  tooltip: 'Open album',
                  onPressed: widget.onOpen,
                  icon: const Icon(Icons.arrow_forward, size: 18),
                ),
              ],
            ),
          ),
        ),
        if (_expanded)
          Padding(
            padding: const EdgeInsets.only(left: 26, bottom: 6),
            child: Column(
              children: [
                for (final track in album.tracks)
                  TrackRow(
                    track: track,
                    albumPath: album.path,
                    showCover: false,
                    onTap: widget.onTrack == null
                        ? null
                        : () => widget.onTrack!(track),
                    trailing: IconButton(
                      tooltip: 'Play this track',
                      icon: const Icon(Icons.play_arrow, size: 18),
                      onPressed: () => playAlbum(
                        context,
                        album,
                        start: album.tracks.indexOf(track),
                      ),
                    ),
                  ),
              ],
            ),
          ),
      ],
    );
  }
}
