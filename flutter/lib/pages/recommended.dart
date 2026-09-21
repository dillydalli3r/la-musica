import 'package:flutter/material.dart';

import '../api.dart';
import '../library_meta.dart';
import '../models.dart';
import '../shell.dart';
import '../state.dart';
import '../theme.dart';
import '../widgets/discover_row.dart';
import '../widgets/shelves.dart';

/// Recommended: what to listen to next — from the whole library or from one of
/// its genres — drawing on the online providers as well as the library's own
/// catalogue, in the same rows and with the same add action the genre browser
/// uses. The basis line is the server's own sentence about why these rows were
/// picked, shown as it stands.
class RecommendedPage extends StatefulWidget {
  const RecommendedPage({super.key});

  @override
  State<RecommendedPage> createState() => _RecommendedPageState();
}

class _RecommendedPageState extends State<RecommendedPage> {
  static const int _limit = 20;

  /// `library` means the library as a whole; anything else is a genre name.
  String _seed = 'library';
  String _kind = 'albums';
  DiscoverRecommendedResult? _result;
  bool _loading = false;
  String? _error;

  List<DiscoverGenre> _seeds = const [];
  String? _seedsError;

  @override
  void initState() {
    super.initState();
    // The ApiClient is reached through an inherited widget, which initState may
    // not read; the first frame is where that lookup is legal.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _loadSeeds();
      _load();
    });
  }

  Future<void> _loadSeeds() async {
    final client = AppScope.of(context).api;
    if (client == null) return;
    try {
      // Only the library's own genres can be seeds: "more like this genre" is a
      // question about what the user actually listens to.
      final result = await client.discoverGenres(scope: 'library');
      if (!mounted) return;
      final names = {for (final genre in result.genres) genre.name};
      final seed = _seed == 'library' || names.contains(_seed)
          ? _seed
          : 'library';
      setState(() {
        _seeds = result.genres;
        _seedsError = null;
        // A seed that is not in the list would leave the control without a
        // matching item, and the library as a whole always exists.
        _seed = seed;
      });
    } on ApiException catch (e) {
      // A seed list that cannot load still leaves the whole library usable, so
      // this is a line beside the control, not a page-level failure.
      if (mounted) setState(() => _seedsError = e.message);
    }
  }

  Future<void> _load() async {
    final client = AppScope.of(context).api;
    if (client == null) return;
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final result = await client.discoverRecommended(
        seed: _seed,
        kind: _kind,
        limit: _limit,
      );
      if (mounted) {
        setState(() {
          _result = result;
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

  void _setSeed(String seed) {
    if (seed == _seed) return;
    setState(() {
      _seed = seed;
      // The previous seed's rows must not sit under the new seed's name.
      _result = null;
    });
    _load();
  }

  void _setKind(String kind) {
    if (kind == _kind) return;
    setState(() {
      _kind = kind;
      _result = null;
    });
    _load();
  }

  @override
  Widget build(BuildContext context) {
    if (AppScope.of(context).api == null) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_error != null) return ErrorView(message: _error!, onRetry: _load);
    final result = _result;

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
                crossAxisAlignment: WrapCrossAlignment.center,
                children: [
                  _seedControl(),
                  DiscoverKindSwitch(kind: _kind, onChanged: _setKind),
                ],
              ),
              if (_seedsError != null)
                Padding(
                  padding: const EdgeInsets.only(top: 6),
                  child: Text(
                    'Genre seeds unavailable: $_seedsError',
                    style: const TextStyle(fontSize: 11, color: _fail),
                  ),
                ),
            ],
          ),
        ),
        Expanded(
          child: result == null
              ? const Center(child: CircularProgressIndicator())
              : _list(result),
        ),
      ],
    );
  }

  Widget _seedControl() => ConstrainedBox(
    // A genre name may be long; the control keeps a fixed share of the row and
    // ellipsises, rather than widening until the row overflows on a phone.
    constraints: const BoxConstraints(maxWidth: 220),
    child: DropdownButton<String>(
      value: _seed,
      isExpanded: true,
      items: [
        const DropdownMenuItem(
          value: 'library',
          child: Text(
            'Whole library',
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
          ),
        ),
        for (final genre in _seeds)
          DropdownMenuItem(
            value: genre.name,
            child: Text(
              genre.name,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
            ),
          ),
      ],
      onChanged: (value) {
        if (value != null) _setSeed(value);
      },
    ),
  );

  Widget _list(DiscoverRecommendedResult result) {
    final index = LibraryIndex(AppScope.of(context).library);
    final muted = Theme.of(
      context,
    ).colorScheme.onSurface.withValues(alpha: 0.55);
    final basis = result.basis;

    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 0, 16, 24),
      children: [
        SectionLabel(
          _seed == 'library' ? 'Recommended' : 'Recommended from $_seed',
          trailing: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (_loading) ...[
                const SizedBox(
                  width: 12,
                  height: 12,
                  child: CircularProgressIndicator(strokeWidth: 2),
                ),
                const SizedBox(width: 8),
              ],
              Text(
                '${result.items.length}',
                style: const TextStyle(fontSize: 11),
              ),
            ],
          ),
        ),
        if (basis != null)
          Padding(
            padding: const EdgeInsets.only(bottom: 6),
            child: Text(basis, style: TextStyle(fontSize: 11, color: muted)),
          ),
        // The provider notes come before the rows AND before the empty state:
        // a short list is explained by which sources answered.
        DiscoverNotes(notes: result.notes),
        if (result.items.isEmpty)
          EmptyHint(
            text:
                'Nothing to recommend came back from the sources that answered'
                '${_seed == 'library' ? '' : ' for "$_seed"'}.',
            icon: Icons.auto_awesome_outlined,
          )
        else
          for (final item in result.items)
            DiscoverRow(
              item: item,
              ownedAlbum: discoverOwnedAlbum(index, item),
              onAdd: (row) => addDiscoverItem(context, row),
            ),
      ],
    );
  }
}

const Color _fail = Color(0xFFF87171);
