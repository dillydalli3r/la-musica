import 'package:flutter/material.dart';

import '../api.dart';
import '../models.dart';
import '../shell.dart';
import '../state.dart';
import '../theme.dart';
import '../widgets/player_bar.dart';
import '../widgets/shelves.dart';
import 'home.dart';

/// The album-level scripts the React page offers, with the ids it sends.
const List<(int, String)> _scripts = [
  (4, 'Grade'),
  (7, 'DR & ReplayGain'),
  (8, 'Auto tagging'),
  (2, 'Format CUEs'),
  (1, 'Format lyrics'),
  (3, 'Optimize FLACs'),
  (5, 'Process images'),
  (6, 'Audit'),
];

/// One album: the cover with the facts the shelf cannot show, the album's
/// actions, and the tracklist.
///
/// The album payload is fetched here instead of read from [AppState.library]
/// because only `GET /api/album` grades the album and carries each track's
/// tech — the library payload is the tree, not the verdicts.
class AlbumPage extends StatefulWidget {
  const AlbumPage({super.key, required this.albumPath});

  final String albumPath;

  @override
  State<AlbumPage> createState() => _AlbumPageState();
}

class _AlbumPageState extends State<AlbumPage> {
  Album? _album;
  String? _error;
  bool _loading = true;
  bool _running = false;
  bool _started = false;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    // AppScope cannot be read from initState (Flutter asserts on an inherited
    // lookup there), so the first load rides the dependency callback.
    if (_started) return;
    _started = true;
    _load();
  }

  @override
  void didUpdateWidget(covariant AlbumPage oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.albumPath != widget.albumPath) _load();
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
      final album = await client.album(widget.albumPath);
      if (!mounted) return;
      setState(() {
        _album = album;
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

  /// Run one optimisation script on the folder, then re-read the album: a
  /// script rewrites tags in place, so everything on this page is stale.
  Future<void> _runScript(int id, String label) async {
    final album = _album;
    final client = AppScope.of(context).api;
    if (album == null || client == null) return;
    final messenger = ScaffoldMessenger.of(context);
    setState(() => _running = true);
    try {
      await client.runScripts([id], [album.path]);
      if (!mounted) return;
      messenger.showSnackBar(
        SnackBar(content: Text('$label finished — reloading the album')),
      );
      await _load();
    } on ApiException catch (e) {
      if (!mounted) return;
      messenger.showSnackBar(
        SnackBar(content: Text('$label failed: ${e.message}')),
      );
    } finally {
      if (mounted) setState(() => _running = false);
    }
  }

  void _queue(Album album) {
    final playback = AppScope.playbackOf(context);
    final messenger = ScaffoldMessenger.of(context);
    playback.queueAdd(albumQueue(album));
    messenger.showSnackBar(
      SnackBar(
        content: Text(
          '${album.tracks.length} track${album.tracks.length == 1 ? '' : 's'} added to the queue',
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) return const Center(child: CircularProgressIndicator());
    final error = _error;
    if (error != null) return ErrorView(message: error, onRetry: _load);
    final album = _album;
    if (album == null) {
      return ErrorView(
        message: 'The server sent no album for this path.',
        onRetry: _load,
      );
    }
    final albumError = album.error;
    if (albumError != null) {
      return ErrorView(message: albumError, onRetry: _load);
    }
    // A framework album: nothing on disk to play and nothing to grade, so the
    // page states what it is waiting for instead of drawing an empty album.
    final pending = pendingNote(album);

    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 16, 16, 24),
      children: [
        _hero(context, album),
        if (pending != null) _PendingPanel(note: pending),
        if (pending == null && album.tracks.isEmpty)
          const EmptyHint(
            text: 'This album folder has no tracks.',
            icon: Icons.audiotrack_outlined,
          )
        else ...[
          SectionLabel(
            'Tracks',
            trailing: Text(
              '${album.tracks.length}',
              style: TextStyle(
                fontSize: 11,
                color: Theme.of(
                  context,
                ).colorScheme.onSurface.withValues(alpha: 0.5),
              ),
            ),
          ),
          for (var i = 0; i < album.tracks.length; i++)
            TrackRow(
              track: album.tracks[i],
              albumPath: album.path,
              onTap: () => openDetail(context, 'Track', album.tracks[i].path),
              trailing: IconButton(
                tooltip: 'Play ${album.tracks[i].displayTitle}',
                icon: const Icon(Icons.play_arrow, size: 18),
                onPressed: () => playAlbum(context, album, start: i),
              ),
            ),
        ],
        RecommendationShelf(
          kind: 'album',
          id: widget.albumPath,
          target: 'albums',
        ),
      ],
    );
  }

  Widget _hero(BuildContext context, Album album) {
    final muted = Theme.of(
      context,
    ).colorScheme.onSurface.withValues(alpha: 0.55);
    final meta = [
      album.artist,
      album.year,
      album.media,
    ].whereType<String>().where((s) => s.isNotEmpty).join(' · ');
    final gradePct = album.gradePct;
    final hasTracks = album.tracks.isNotEmpty;
    final artistFolder = parentFolder(album.path);
    final pending = pendingNote(album);

    return LayoutBuilder(
      builder: (context, constraints) {
        final info = Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              'ALBUM',
              style: TextStyle(
                fontSize: 11,
                letterSpacing: 1.6,
                fontWeight: FontWeight.w600,
                color: muted,
              ),
            ),
            const SizedBox(height: 4),
            Row(
              children: [
                Flexible(
                  child: Text(
                    album.title,
                    style: const TextStyle(
                      fontSize: 22,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ),
                const SizedBox(width: 8),
                AdvisoryMark(value: album.advisory, size: 18),
                const SizedBox(width: 8),
                // The same marker the rows carry, saying the same sentence.
                PendingMark(album, size: 18, label: true),
              ],
            ),
            if (meta.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: Text(meta, style: TextStyle(fontSize: 13, color: muted)),
              ),
            const SizedBox(height: 10),
            // Nothing was graded — the verdict's red "Not graded" would report
            // an album the app itself created on purpose as a broken one. The
            // panel below carries what is actually known about the folder.
            if (pending == null) ...[
              _verdict(context, album, gradePct),
              const SizedBox(height: 12),
            ],
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                FilledButton.icon(
                  // A framework album has no audio: the button is off rather
                  // than starting an empty queue.
                  onPressed: hasTracks && pending == null
                      ? () => playAlbum(context, album)
                      : null,
                  icon: const Icon(Icons.play_arrow, size: 18),
                  label: const Text('Play'),
                ),
                OutlinedButton.icon(
                  onPressed: hasTracks && pending == null
                      ? () => _queue(album)
                      : null,
                  icon: const Icon(Icons.playlist_add, size: 18),
                  label: const Text('Queue'),
                ),
                _ScriptsMenu(enabled: !_running, onSelected: _runScript),
                if (artistFolder != null)
                  OutlinedButton.icon(
                    onPressed: () =>
                        openDetail(context, 'Artist', artistFolder),
                    icon: const Icon(Icons.person_outline, size: 18),
                    label: const Text('Open artist'),
                  ),
              ],
            ),
          ],
        );
        final cover = CoverArt(
          albumPath: album.path,
          coverFile: album.coverFile,
          size: 180,
          radius: 10,
        );
        // A phone cannot hold the cover beside the text: the action row alone
        // needs the full width, so the hero stacks instead of squeezing.
        if (constraints.maxWidth < 560) {
          return Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [cover, const SizedBox(height: 14), info],
          );
        }
        return Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            cover,
            const SizedBox(width: 18),
            Expanded(child: info),
          ],
        );
      },
    );
  }

  Widget _verdict(BuildContext context, Album album, double? gradePct) {
    final muted = Theme.of(
      context,
    ).colorScheme.onSurface.withValues(alpha: 0.55);
    final audit = album.auditSummary;
    return Row(
      children: [
        Container(
          width: 9,
          height: 9,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: album.pass
                ? const Color(0xFF10B981)
                : const Color(0xFFDC2626),
          ),
        ),
        const SizedBox(width: 8),
        Text(
          gradePct == null
              ? 'Not graded'
              : '${gradePct.toStringAsFixed(0)}% of checks',
          style: TextStyle(
            fontSize: 13,
            color: album.pass
                ? const Color(0xFF34D399)
                : const Color(0xFFF87171),
          ),
        ),
        if (audit != null) ...[
          Text(' · ', style: TextStyle(color: muted)),
          Flexible(
            child: Text(
              audit,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(fontSize: 13, color: muted),
            ),
          ),
        ],
      ],
    );
  }
}

/// The amber panel an album page shows for a framework album: what the folder
/// is waiting for, where the search stands, and what is already there — the
/// block the React page draws (`web/src/pages/AlbumPage.tsx`), so a folder the
/// app created on purpose reads the same in both clients.
class _PendingPanel extends StatelessWidget {
  const _PendingPanel({required this.note});

  final PendingNote note;

  @override
  Widget build(BuildContext context) {
    const title = Color(0xFFFDE68A);
    const body = Color(0xFFFCD34D);
    final reason = note.reason;
    return Container(
      margin: const EdgeInsets.only(top: 14),
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
      decoration: BoxDecoration(
        color: const Color(0x14F59E0B),
        border: Border.all(color: const Color(0x4DF59E0B)),
        borderRadius: BorderRadius.circular(10),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Icon(Icons.schedule, size: 16, color: Color(0xFFFBBF24)),
              const SizedBox(width: 8),
              const Text(
                pendingTitle,
                style: TextStyle(
                  fontSize: 13,
                  fontWeight: FontWeight.w600,
                  color: title,
                ),
              ),
              if (reason != null) ...[
                const SizedBox(width: 8),
                Flexible(
                  child: Text(
                    reason,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                      fontSize: 12,
                      color: body.withValues(alpha: 0.7),
                    ),
                  ),
                ),
              ],
            ],
          ),
          const SizedBox(height: 6),
          Text(note.state, style: const TextStyle(fontSize: 12, color: body)),
          const SizedBox(height: 4),
          Text(
            note.note,
            style: TextStyle(fontSize: 11, color: body.withValues(alpha: 0.6)),
          ),
        ],
      ),
    );
  }
}

/// The album's scripts behind one button: eight menu items would crowd the
/// action row, and every one of them runs on the same folder.
class _ScriptsMenu extends StatelessWidget {
  const _ScriptsMenu({required this.enabled, required this.onSelected});

  final bool enabled;
  final void Function(int id, String label) onSelected;

  @override
  Widget build(BuildContext context) {
    return PopupMenuButton<(int, String)>(
      enabled: enabled,
      tooltip: 'Run an optimisation script on this album',
      onSelected: (script) => onSelected(script.$1, script.$2),
      itemBuilder: (context) => [
        for (final script in _scripts)
          PopupMenuItem(value: script, child: Text(script.$2)),
      ],
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
        decoration: BoxDecoration(
          border: Border.all(color: Theme.of(context).dividerColor),
          borderRadius: BorderRadius.circular(8),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              Icons.auto_fix_high,
              size: 18,
              color: enabled ? null : Theme.of(context).disabledColor,
            ),
            const SizedBox(width: 8),
            Text(
              'Run scripts',
              style: TextStyle(
                fontWeight: FontWeight.w500,
                color: enabled ? null : Theme.of(context).disabledColor,
              ),
            ),
            const Icon(Icons.arrow_drop_down, size: 20),
          ],
        ),
      ),
    );
  }
}
