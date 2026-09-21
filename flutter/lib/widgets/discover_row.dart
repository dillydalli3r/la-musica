import 'package:flutter/material.dart';

import '../api.dart';
import '../models.dart';
import '../state.dart';
import '../shell.dart';
import 'player_bar.dart';

/// The provider ids the discovery pages name on screen, with the spellings the
/// settings UI already uses. An id this build does not know is shown as its raw
/// id: a source the page cannot name is still a source that answered or failed.
const Map<String, String> discoverSourceLabels = {
  'library': 'Library',
  'musicbrainz': 'MusicBrainz',
  'deezer': 'Deezer',
  'itunes': 'iTunes',
  'audiodb': 'TheAudioDB',
  'theaudiodb': 'TheAudioDB',
  'lastfm': 'Last.fm',
  'listenbrainz': 'ListenBrainz',
  'discogs': 'Discogs',
  'wikidata': 'Wikidata',
  'wikipedia': 'Wikipedia',
  'spotify': 'Spotify',
  'rateyourmusic': 'RateYourMusic',
  'bandcamp': 'Bandcamp',
};

String discoverSourceLabel(String id) => discoverSourceLabels[id] ?? id;

/// The `openDetail` label a row's path is opened under.
String discoverDetailLabel(String kind) => switch (kind) {
  'artist' => 'Artist',
  'track' => 'Track',
  _ => 'Album',
};

/// One discovery row: a provider's album, artist or track, or the library's own
/// copy of it. The cover comes from the local file when the library has the row
/// and from the provider URL otherwise; a row the library does not have offers
/// the add action instead of linking to a page that is not there.
class DiscoverRow extends StatefulWidget {
  const DiscoverRow({
    super.key,
    required this.item,
    this.ownedAlbum,
    this.onAdd,
  });

  final DiscoverItem item;

  /// The library album holding this row, when it is the user's own — its cover
  /// file is what the row draws, so owned rows never fetch art the disk has.
  final Album? ownedAlbum;

  /// The add action, owned by the page, which knows how to report the answer.
  /// It returns what the server did, so the row can show the release as queued,
  /// or null when the add failed and the reason has already been reported.
  final Future<LibraryAddResult?> Function(DiscoverItem item)? onAdd;

  @override
  State<DiscoverRow> createState() => _DiscoverRowState();
}

class _DiscoverRowState extends State<DiscoverRow> {
  bool _busy = false;
  bool _tracksOpen = false;

  /// What the server did when this row was added, once it has been: the row
  /// then shows the pending album instead of the button that made it.
  LibraryAddResult? _queued;

  bool get _owned => widget.item.owned || widget.item.inLibrary;

  String get _path => widget.item.path ?? '';

  @override
  Widget build(BuildContext context) {
    final item = widget.item;
    final muted = Theme.of(
      context,
    ).colorScheme.onSurface.withValues(alpha: 0.55);
    final openable = _owned && _path.isNotEmpty;

    return InkWell(
      onTap: openable
          ? () => openDetail(context, discoverDetailLabel(item.kind), _path)
          : null,
      borderRadius: BorderRadius.circular(8),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 8),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            _cover(context),
            const SizedBox(width: 10),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    item.title.isEmpty ? 'Untitled' : item.title,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      fontSize: 13,
                      fontWeight: FontWeight.w500,
                    ),
                  ),
                  if (_subtitle(item).isNotEmpty)
                    Text(
                      _subtitle(item),
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(fontSize: 11, color: muted),
                    ),
                  if (item.reason != null)
                    Padding(
                      padding: const EdgeInsets.only(top: 2),
                      child: Text(
                        item.reason!,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                          fontSize: 11,
                          fontStyle: FontStyle.italic,
                          color: muted,
                        ),
                      ),
                    ),
                  const SizedBox(height: 5),
                  Wrap(
                    spacing: 6,
                    runSpacing: 4,
                    crossAxisAlignment: WrapCrossAlignment.center,
                    children: [
                      if (item.sourceName.isNotEmpty)
                        SourceChip(label: item.sourceName, url: item.pageUrl),
                      for (final extra in item.alsoFrom)
                        SourceChip(label: discoverSourceLabel(extra)),
                    ],
                  ),
                  if (item.tracks.isNotEmpty) _tracks(context),
                ],
              ),
            ),
            const SizedBox(width: 8),
            _action(context),
          ],
        ),
      ),
    );
  }

  String _subtitle(DiscoverItem item) => [
    item.artist,
    item.year,
  ].whereType<String>().where((part) => part.isNotEmpty).join(' · ');

  Widget _cover(BuildContext context) {
    const size = 52.0;
    final album = widget.ownedAlbum;
    if (album != null) {
      return CoverArt(
        albumPath: album.path,
        coverFile: album.coverFile,
        size: size,
      );
    }
    final url = widget.item.coverUrl;
    if (url != null && url.isNotEmpty) {
      return ClipRRect(
        borderRadius: BorderRadius.circular(6),
        child: Image.network(
          url,
          width: size,
          height: size,
          fit: BoxFit.cover,
          errorBuilder: (context, _, _) => _placeholder(context, size),
          loadingBuilder: (context, child, progress) =>
              progress == null ? child : _placeholder(context, size),
        ),
      );
    }
    return _placeholder(context, size);
  }

  Widget _placeholder(BuildContext context, double size) => Container(
    width: size,
    height: size,
    alignment: Alignment.center,
    decoration: BoxDecoration(
      color: Theme.of(context).colorScheme.surfaceContainerHighest,
      borderRadius: BorderRadius.circular(6),
    ),
    child: Icon(
      Icons.album_outlined,
      size: size / 2,
      color: Theme.of(context).colorScheme.onSurface.withValues(alpha: 0.35),
    ),
  );

  Widget _tracks(BuildContext context) {
    final tracks = widget.item.tracks;
    final muted = Theme.of(
      context,
    ).colorScheme.onSurface.withValues(alpha: 0.55);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        TextButton(
          onPressed: () => setState(() => _tracksOpen = !_tracksOpen),
          style: TextButton.styleFrom(
            padding: EdgeInsets.zero,
            minimumSize: const Size(0, 26),
            tapTargetSize: MaterialTapTargetSize.shrinkWrap,
          ),
          child: Text(
            _tracksOpen
                ? 'Hide the tracklist'
                : '${tracks.length} track${tracks.length == 1 ? '' : 's'}',
            style: const TextStyle(fontSize: 11),
          ),
        ),
        if (_tracksOpen)
          for (var i = 0; i < tracks.length; i++)
            Text(
              '${i + 1}. ${tracks[i]}',
              style: TextStyle(fontSize: 11, color: muted),
            ),
      ],
    );
  }

  Widget _action(BuildContext context) {
    final item = widget.item;
    final queued = _queued;
    if (queued != null) return _queuedAction(context, queued);
    if (_owned && _path.isNotEmpty) {
      return TextButton.icon(
        onPressed: () =>
            openDetail(context, discoverDetailLabel(item.kind), _path),
        icon: const Icon(Icons.library_music_outlined, size: 16),
        label: const Text('In library'),
      );
    }
    if (_owned) {
      // The library has it but this answer carried no path: naming that is
      // honest, while an add that can only be refused is not.
      return Padding(
        padding: const EdgeInsets.only(top: 8),
        child: Text(
          'In library',
          style: TextStyle(
            fontSize: 11,
            color: Theme.of(
              context,
            ).colorScheme.onSurface.withValues(alpha: 0.45),
          ),
        ),
      );
    }
    final onAdd = widget.onAdd;
    if (onAdd == null || item.addMbid == null) return const SizedBox.shrink();
    return OutlinedButton.icon(
      onPressed: _busy ? null : () => _add(onAdd),
      icon: _busy
          ? const SizedBox(
              width: 14,
              height: 14,
              child: CircularProgressIndicator(strokeWidth: 2),
            )
          : const Icon(Icons.add, size: 16),
      label: Text(_busy ? 'Adding…' : 'Add to library'),
    );
  }

  /// A successful add leaves a framework album on disk searching for its
  /// audio: the row says so, and offers the undo the server supports, instead
  /// of looking untouched the moment the button is pressed.
  Widget _queuedAction(BuildContext context, LibraryAddResult result) {
    final muted = Theme.of(
      context,
    ).colorScheme.onSurface.withValues(alpha: 0.55);
    // The pending album the undo applies to: the path it was created at, and
    // the wish searching for its audio.
    String? albumPath;
    int? wishId;
    for (final album in result.albums) {
      if (albumPath == null && album.created && album.albumPath.isNotEmpty) {
        albumPath = album.albumPath;
      }
      wishId ??= album.wishId;
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.end,
      children: [
        ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 140),
          child: Text(
            result.background
                ? 'Discography queued'
                : (result.added > 0
                      ? 'Queued — searching for its audio'
                      : 'Nothing added'),
            textAlign: TextAlign.right,
            style: TextStyle(fontSize: 11, color: muted),
          ),
        ),
        if (albumPath != null || wishId != null)
          TextButton(
            onPressed: _busy ? null : () => _cancel(albumPath, wishId),
            child: const Text('Undo'),
          ),
      ],
    );
  }

  Future<void> _add(
    Future<LibraryAddResult?> Function(DiscoverItem item) onAdd,
  ) async {
    setState(() => _busy = true);
    try {
      final result = await onAdd(widget.item);
      if (mounted && result != null) setState(() => _queued = result);
    } finally {
      // The row stays mounted while the snack bar is up; the busy flag belongs
      // to this row either way.
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _cancel(String? albumPath, int? wishId) async {
    setState(() => _busy = true);
    try {
      final message = await cancelDiscoverAdd(
        context,
        albumPath: albumPath,
        wishId: wishId,
      );
      if (mounted) {
        setState(() => _queued = null);
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text(message)));
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }
}

/// The albums/artists/tracks switch both discovery pages carry — one control,
/// so the two pages offer exactly the same three views of a genre.
class DiscoverKindSwitch extends StatelessWidget {
  const DiscoverKindSwitch({
    super.key,
    required this.kind,
    required this.onChanged,
  });

  final String kind;
  final ValueChanged<String> onChanged;

  @override
  Widget build(BuildContext context) => SegmentedButton<String>(
    showSelectedIcon: false,
    segments: const [
      ButtonSegment(
        value: 'albums',
        label: Text('Albums'),
        icon: Icon(Icons.album_outlined, size: 16),
      ),
      ButtonSegment(
        value: 'artists',
        label: Text('Artists'),
        icon: Icon(Icons.person_outline, size: 16),
      ),
      ButtonSegment(
        value: 'tracks',
        label: Text('Tracks'),
        icon: Icon(Icons.music_note_outlined, size: 16),
      ),
    ],
    selected: {kind},
    onSelectionChanged: (selection) => onChanged(selection.first),
  );
}

/// A small chip naming a provider. [url] is the row's own page on that provider
/// — offered as a tooltip, since the client carries no browser to open it with.
/// [accent] is the colour of a chip that is reporting a failure rather than a
/// name.
class SourceChip extends StatelessWidget {
  const SourceChip({super.key, required this.label, this.url, this.accent});

  final String label;
  final String? url;
  final Color? accent;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final chip = Container(
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
      decoration: BoxDecoration(
        color: theme.colorScheme.surfaceContainerHighest,
        border: Border.all(color: accent ?? theme.dividerColor),
        borderRadius: BorderRadius.circular(4),
      ),
      child: Text(
        label,
        style: TextStyle(
          fontSize: 10,
          letterSpacing: 0.4,
          color: accent ?? theme.colorScheme.onSurface.withValues(alpha: 0.6),
        ),
      ),
    );
    final tooltip = url;
    return tooltip == null || tooltip.isEmpty
        ? chip
        : Tooltip(message: tooltip, child: chip);
  }
}

const Color _warn = Color(0xFFFBBF24);
const Color _fail = Color(0xFFF87171);

/// The provider status strip: one chip per source the server asked, carrying
/// its own note — "skipped: no lastfm_api_key" is shown, never swallowed, so a
/// short list of results stays explainable.
class DiscoverNotes extends StatelessWidget {
  const DiscoverNotes({super.key, required this.notes});

  /// source id -> the server's line: "" answered, else "skipped: …" or
  /// "failed: …".
  final Map<String, String> notes;

  @override
  Widget build(BuildContext context) {
    if (notes.isEmpty) return const SizedBox.shrink();
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Wrap(
        spacing: 6,
        runSpacing: 6,
        children: [
          for (final entry in notes.entries)
            SourceChip(
              label: entry.value.isEmpty
                  ? discoverSourceLabel(entry.key)
                  : '${discoverSourceLabel(entry.key)} — ${entry.value}',
              accent: entry.value.isEmpty
                  ? null
                  : (entry.value.startsWith('failed') ? _fail : _warn),
            ),
        ],
      ),
    );
  }
}

/// Ask the server to add one row, and report what it actually did. The result
/// comes back so the row can show the album as pending; null means the add
/// failed and the reason has already been shown.
///
/// A 404 is the one failure worth spelling out: no add route on this server
/// means the button cannot work here at all, which is stated plainly instead of
/// reporting a wish that was never filed.
Future<LibraryAddResult?> addDiscoverItem(
  BuildContext context,
  DiscoverItem item,
) async {
  final client = AppScope.of(context).api;
  final messenger = ScaffoldMessenger.of(context);
  if (client == null) return null;
  final mbid = item.addMbid;
  if (mbid == null) {
    messenger.showSnackBar(
      const SnackBar(content: Text('This row has no MusicBrainz id to add.')),
    );
    return null;
  }
  try {
    final result = await client.addToLibrary(
      mbid: mbid,
      kind: item.wishKind,
      title: item.title,
      artist: item.artist,
      year: item.year,
    );
    messenger.showSnackBar(SnackBar(content: Text(result.message)));
    return result;
  } on ApiException catch (e) {
    messenger.showSnackBar(
      SnackBar(
        content: Text(
          e.status == 404
              ? 'This server has no add route: POST /api/library/add answered '
                    '404, so nothing was added.'
              : e.message,
        ),
        backgroundColor: _fail,
      ),
    );
    return null;
  }
}

/// Undo an add the row made, and report what the server still had to cancel. A
/// 404 here means the pending album is already gone, which is an answer too.
Future<String> cancelDiscoverAdd(
  BuildContext context, {
  String? albumPath,
  int? wishId,
}) async {
  final client = AppScope.of(context).api;
  if (client == null) return 'Not connected.';
  try {
    return await client.cancelAdd(albumPath: albumPath, wishId: wishId);
  } on ApiException catch (e) {
    return e.status == 404 ? 'Nothing was pending any more.' : e.message;
  }
}
