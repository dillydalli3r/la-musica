import 'package:flutter/material.dart';

import '../api.dart';
import '../library_meta.dart';
import '../models.dart';
import '../shell.dart';
import '../state.dart';
import '../theme.dart';
import '../widgets/discover_row.dart';
import '../widgets/shelves.dart';

/// Discover: the genres the library has and the genres the providers know, then
/// — for the one the user opens — its albums, artists or tracks. The scope
/// toggle is the honesty switch: "library" is the user's own files, "online" is
/// what the providers suggest, "all" is both, and every row names its source.
class DiscoverPage extends StatefulWidget {
  const DiscoverPage({super.key});

  @override
  State<DiscoverPage> createState() => _DiscoverPageState();
}

class _DiscoverPageState extends State<DiscoverPage> {
  static const int _pageSize = 25;

  final TextEditingController _filter = TextEditingController();

  String _scope = 'all';
  String _kind = 'albums';
  String _source = 'all';
  String _genreQuery = '';

  DiscoverGenresResult? _genres;
  String? _genresError;

  /// The genre whose rows are on screen, or null while the list is showing.
  String? _genre;
  DiscoverGenreResult? _page;
  List<DiscoverItem> _items = const [];
  bool _loadingItems = false;
  String? _itemsError;

  @override
  void initState() {
    super.initState();
    // The ApiClient is reached through an inherited widget, which initState may
    // not read; the first frame is where that lookup is legal.
    WidgetsBinding.instance.addPostFrameCallback((_) => _loadGenres());
  }

  @override
  void dispose() {
    _filter.dispose();
    super.dispose();
  }

  Future<void> _loadGenres() async {
    final client = AppScope.of(context).api;
    if (client == null) return;
    setState(() => _genresError = null);
    try {
      final result = await client.discoverGenres(scope: _scope);
      if (mounted) setState(() => _genres = result);
    } on ApiException catch (e) {
      if (mounted) setState(() => _genresError = e.message);
    }
  }

  Future<void> _openGenre(String name) async {
    setState(() {
      _genre = name;
      _items = const [];
      _page = null;
      _itemsError = null;
    });
    await _loadItems();
  }

  /// The rows for the open genre. The offset is where the list already ends,
  /// because every page is appended to the same list.
  Future<void> _loadItems() async {
    final genre = _genre;
    final client = AppScope.of(context).api;
    if (genre == null || client == null) return;
    final offset = _items.length;
    setState(() {
      _loadingItems = true;
      _itemsError = null;
    });
    try {
      final page = await client.discoverGenre(
        genre: genre,
        kind: _kind,
        source: _source,
        limit: _pageSize,
        offset: offset,
      );
      // Another genre's answer arriving late must not land in this list.
      if (!mounted || _genre != genre) return;
      setState(() {
        _page = page;
        _items = offset == 0 ? page.items : [..._items, ...page.items];
        _loadingItems = false;
      });
    } on ApiException catch (e) {
      if (mounted && _genre == genre) {
        setState(() {
          _itemsError = e.message;
          _loadingItems = false;
        });
      }
    }
  }

  void _setScope(String scope) {
    if (scope == _scope) return;
    setState(() => _scope = scope);
    // The scope decides what the genre LIST counts; an open genre's rows come
    // from the genre route, which has no scope of its own, so they stay.
    _loadGenres();
  }

  void _setKind(String kind) {
    if (kind == _kind) return;
    _reset(kind: kind);
  }

  void _setSource(String source) {
    if (source == _source) return;
    _reset(source: source);
  }

  /// Changing a filter that the rows themselves depend on clears them: showing
  /// the previous kind or source's rows under the new switch would be a lie.
  void _reset({String? kind, String? source}) {
    setState(() {
      if (kind != null) _kind = kind;
      if (source != null) _source = source;
      _items = const [];
      _page = null;
      _itemsError = null;
    });
    if (_genre != null) _loadItems();
  }

  void _backToList() {
    setState(() {
      _genre = null;
      _items = const [];
      _page = null;
      _itemsError = null;
    });
  }

  /// The sources the filter can offer: the ones the server says it asked, so
  /// the chips can never name a provider this server does not have. "library"
  /// and "all" are the two ends of the range.
  List<String> get _sourceIds => <String>{
    'all',
    'library',
    ...?_page?.sourcesAsked,
    ...?_genres?.sourcesAsked,
  }.toList();

  @override
  Widget build(BuildContext context) {
    if (AppScope.of(context).api == null) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_genres == null && _genresError != null) {
      return ErrorView(message: _genresError!, onRetry: _loadGenres);
    }
    final genres = _genres;
    if (genres == null) return const Center(child: CircularProgressIndicator());

    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 12, 16, 4),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Wrap(
                spacing: 10,
                runSpacing: 8,
                children: [
                  _scopeSwitch(),
                  DiscoverKindSwitch(kind: _kind, onChanged: _setKind),
                ],
              ),
              const SizedBox(height: 8),
              if (_genre == null) _genreFilterField() else _sourceChips(),
              if (_genresError != null)
                Padding(
                  padding: const EdgeInsets.only(top: 6),
                  child: Text(
                    _genresError!,
                    style: const TextStyle(fontSize: 11, color: _fail),
                  ),
                ),
            ],
          ),
        ),
        Expanded(child: _genre == null ? _genreList(genres) : _itemsView()),
      ],
    );
  }

  Widget _scopeSwitch() => SegmentedButton<String>(
    showSelectedIcon: false,
    segments: const [
      ButtonSegment(
        value: 'library',
        label: Text('Library'),
        icon: Icon(Icons.library_music_outlined, size: 16),
      ),
      ButtonSegment(
        value: 'online',
        label: Text('Online'),
        icon: Icon(Icons.public, size: 16),
      ),
      ButtonSegment(
        value: 'all',
        label: Text('All'),
        icon: Icon(Icons.select_all, size: 16),
      ),
    ],
    selected: {_scope},
    onSelectionChanged: (selection) => _setScope(selection.first),
  );

  Widget _genreFilterField() => TextField(
    controller: _filter,
    decoration: InputDecoration(
      hintText: 'Filter genres',
      prefixIcon: const Icon(Icons.search, size: 18),
      suffixIcon: _genreQuery.isEmpty
          ? null
          : IconButton(
              icon: const Icon(Icons.close, size: 16),
              onPressed: () {
                _filter.clear();
                setState(() => _genreQuery = '');
              },
            ),
    ),
    onChanged: (value) => setState(() => _genreQuery = value),
  );

  /// The source filter: which provider the open genre's rows may come from.
  Widget _sourceChips() => Wrap(
    spacing: 6,
    runSpacing: 6,
    children: [
      for (final id in _sourceIds)
        FilterChip(
          label: Text(id == 'all' ? 'All sources' : discoverSourceLabel(id)),
          selected: _source == id,
          onSelected: (_) => _setSource(id),
        ),
    ],
  );

  Widget _genreList(DiscoverGenresResult result) {
    if (result.genres.isEmpty) {
      // The notes come first even here: "no genre" is a much better sentence
      // with the providers that were skipped printed above it.
      return ListView(
        padding: const EdgeInsets.fromLTRB(16, 0, 16, 24),
        children: [
          DiscoverNotes(notes: result.notes),
          const EmptyHint(
            text:
                'No genre came back: the library has no GENRE tags yet and the '
                'providers named none. Import genres on an album, or switch the '
                'scope to "Online".',
            icon: Icons.sell_outlined,
          ),
        ],
      );
    }
    final needle = _genreQuery.trim().toLowerCase();
    final genres = needle.isEmpty
        ? result.genres
        : [
            for (final genre in result.genres)
              if (genre.name.toLowerCase().contains(needle)) genre,
          ];
    if (genres.isEmpty) {
      return const EmptyHint(
        text: 'No genre matches that filter.',
        icon: Icons.search_off,
      );
    }
    return ListView.builder(
      padding: const EdgeInsets.fromLTRB(16, 0, 16, 24),
      // The provider notes sit above the list: a source that was skipped is
      // part of reading the counts that follow.
      itemCount: genres.length + 1,
      itemBuilder: (context, i) {
        if (i == 0) return DiscoverNotes(notes: result.notes);
        final genre = genres[i - 1];
        return _GenreRow(genre: genre, onTap: () => _openGenre(genre.name));
      },
    );
  }

  Widget _itemsView() {
    final items = _items;
    final page = _page;
    final genre = _genre ?? '';
    if (_itemsError != null && items.isEmpty) {
      return ErrorView(message: _itemsError!, onRetry: _loadItems);
    }
    if (_loadingItems && items.isEmpty) {
      return const Center(child: CircularProgressIndicator());
    }
    final index = LibraryIndex(AppScope.of(context).library);
    final muted = Theme.of(
      context,
    ).colorScheme.onSurface.withValues(alpha: 0.55);

    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 0, 16, 24),
      children: [
        Align(
          alignment: Alignment.centerLeft,
          child: TextButton.icon(
            onPressed: _backToList,
            icon: const Icon(Icons.arrow_back, size: 16),
            label: const Text('All genres'),
          ),
        ),
        SectionLabel(
          genre,
          trailing: Text(
            '${items.length} ${items.length == 1 ? _singular : _kind}',
            style: const TextStyle(fontSize: 11),
          ),
        ),
        DiscoverNotes(notes: page?.notes ?? const {}),
        if (_itemsError != null)
          Padding(
            padding: const EdgeInsets.only(bottom: 6),
            child: Text(
              _itemsError!,
              style: const TextStyle(fontSize: 11, color: _fail),
            ),
          ),
        if (items.isEmpty) ...[
          // The provider notes stay above this: an empty list is explained by
          // which sources answered, not by a bare "nothing here".
          EmptyHint(text: _emptyText(genre), icon: Icons.search_off),
        ] else ...[
          for (final item in items)
            DiscoverRow(
              item: item,
              ownedAlbum: discoverOwnedAlbum(index, item),
              onAdd: (row) => addDiscoverItem(context, row),
            ),
          if (page != null && page.hasMore(_pageSize))
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Center(
                child: OutlinedButton.icon(
                  onPressed: _loadingItems ? null : _loadItems,
                  icon: _loadingItems
                      ? const SizedBox(
                          width: 14,
                          height: 14,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.expand_more, size: 16),
                  label: Text(_loadingItems ? 'Loading…' : 'Load more'),
                ),
              ),
            )
          else if (page != null)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Text(
                'All ${items.length} answered for.',
                style: TextStyle(fontSize: 11, color: muted),
              ),
            ),
        ],
      ],
    );
  }

  String _emptyText(String genre) {
    final hint = StringBuffer('No $_kind came back for "$genre".');
    if (_source != 'all') {
      hint.write(
        ' The source filter is ${discoverSourceLabel(_source)} — set it to all '
        'sources to see every provider.',
      );
    }
    return hint.toString();
  }

  String get _singular => switch (_kind) {
    'artists' => 'artist',
    'tracks' => 'track',
    _ => 'album',
  };
}

const Color _fail = Color(0xFFF87171);

/// One genre in the list: its name, the counts the current scope produced, and
/// the sources that named it.
class _GenreRow extends StatelessWidget {
  const _GenreRow({required this.genre, required this.onTap});

  final DiscoverGenre genre;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final muted = Theme.of(
      context,
    ).colorScheme.onSurface.withValues(alpha: 0.55);
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(8),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 8),
        child: Row(
          children: [
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    genre.name,
                    style: const TextStyle(
                      fontSize: 13,
                      fontWeight: FontWeight.w500,
                    ),
                  ),
                  if (genre.counts.isNotEmpty)
                    Text(
                      genre.counts,
                      style: TextStyle(fontSize: 11, color: muted),
                    ),
                  if (genre.sources.isNotEmpty)
                    Padding(
                      padding: const EdgeInsets.only(top: 4),
                      child: Wrap(
                        spacing: 6,
                        runSpacing: 4,
                        children: [
                          for (final source in genre.sources)
                            SourceChip(label: discoverSourceLabel(source)),
                        ],
                      ),
                    ),
                ],
              ),
            ),
            Icon(Icons.chevron_right, size: 18, color: muted),
          ],
        ),
      ),
    );
  }
}
