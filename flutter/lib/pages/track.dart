import 'package:flutter/material.dart';

import '../api.dart';
import '../models.dart';
import '../shell.dart';
import '../state.dart';
import '../theme.dart';
import '../widgets/player_bar.dart';
import '../widgets/shelves.dart';
import 'home.dart';

/// The per-track scripts the React page offers, with the ids it sends.
const List<(int, String)> _scripts = [
  (4, 'Grade'),
  (7, 'DR & ReplayGain'),
  (8, 'Auto tagging'),
];

/// The single-value tags the React page pulls out of the map, in its order.
const List<(String, String)> _tagRows = [
  ('GENRE', 'Genre'),
  ('MOOD', 'Mood'),
  ('BPM', 'BPM'),
  ('MEDIA', 'Media'),
  ('SOURCE', 'Source'),
  ('INSTRUMENTAL', 'Instrumental'),
  ('ITUNESADVISORY', 'Advisory'),
];

/// The loudness and integrity figures the React page shows beside the grade.
const List<(String, String)> _gainRows = [
  ('REPLAYGAIN_TRACK_GAIN', 'ReplayGain track'),
  ('REPLAYGAIN_TRACK_PEAK', 'ReplayGain track peak'),
  ('REPLAYGAIN_ALBUM_GAIN', 'ReplayGain album'),
  ('REPLAYGAIN_ALBUM_PEAK', 'ReplayGain album peak'),
  ('DYNAMIC RANGE', 'Dynamic range'),
];

/// One track: its tags, its technical facts, how it graded, and the actions
/// the file takes — the React `TrackPage` without its browser-only parts
/// (lyrics editing, downloads, exports, cover upload).
///
/// The tag map is the page's own payload; the album it lives in is what grades
/// it and carries its tech, so both are read here.
class TrackPage extends StatefulWidget {
  const TrackPage({super.key, required this.trackPath});

  final String trackPath;

  /// The album folder a track file lives in — the folder whose payload knows
  /// this track's verdicts.
  String? get albumPath => parentFolder(trackPath);

  @override
  State<TrackPage> createState() => _TrackPageState();
}

class _TrackPageState extends State<TrackPage> {
  Map<String, String> _tags = const {};
  Album? _album;
  Track? _track;
  String? _tagsError;
  String? _albumError;
  bool _loading = true;
  bool _running = false;
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
  void didUpdateWidget(covariant TrackPage oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.trackPath != widget.trackPath) _load();
  }

  Future<void> _load() async {
    final client = AppScope.of(context).api;
    if (client == null) {
      setState(() {
        _loading = false;
        _tagsError = 'This client is not connected to a server.';
      });
      return;
    }
    setState(() {
      _loading = true;
      _tagsError = null;
      _albumError = null;
    });

    Map<String, String> tags;
    try {
      tags = await client.trackTags(widget.trackPath);
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _tagsError = e.message;
        _loading = false;
      });
      return;
    }

    // The album supplies the grade and the tech. A failure there costs the
    // verdicts, not the page: the tags are already in hand.
    Album? album;
    String? albumError;
    final albumPath = widget.albumPath;
    if (albumPath == null) {
      albumError = 'This track path has no album folder to grade it from.';
    } else {
      try {
        album = await client.album(albumPath);
      } on ApiException catch (e) {
        albumError = e.message;
      }
    }

    if (!mounted) return;
    setState(() {
      _tags = {
        for (final entry in tags.entries)
          if (entry.value.isNotEmpty) entry.key: entry.value,
      };
      _album = album;
      _track = album == null ? null : _findTrack(album, widget.trackPath);
      _albumError = albumError;
      _loading = false;
    });
  }

  static Track? _findTrack(Album album, String path) {
    final needle = path.replaceAll('\\', '/');
    for (final track in album.tracks) {
      if (track.path.replaceAll('\\', '/') == needle) return track;
    }
    return null;
  }

  String get _fileName => widget.trackPath.split(RegExp(r'[\\/]')).last;

  String get _title =>
      _tags['TITLE'] ??
      _track?.displayTitle ??
      _fileName.replaceAll(RegExp(r'\.[^.]+$'), '');

  String? get _artist =>
      _tags['ALBUMARTIST'] ?? _tags['ARTIST'] ?? _track?.artist;

  String? get _albumTitle => _tags['ALBUM'] ?? _track?.album ?? _album?.title;

  String? get _date => _tags['DATE'] ?? _album?.date;

  Map<String, dynamic> _queueEntry() => {
    'path': widget.trackPath,
    'file': _fileName,
    'title': _tags['TITLE'] ?? _track?.title,
    'advisory': _tags['ITUNESADVISORY'] ?? _track?.advisory,
    'artist': _artist,
    'album': _albumTitle,
    'albumPath': widget.albumPath,
  };

  void _play() {
    final entry = _queueEntry();
    AppScope.playbackOf(
      context,
    ).playPath(entry['path'] as String, queueAfter: [entry]);
  }

  void _queue() {
    final playback = AppScope.playbackOf(context);
    final messenger = ScaffoldMessenger.of(context);
    playback.queueAdd([_queueEntry()]);
    messenger.showSnackBar(const SnackBar(content: Text('Added to the queue')));
  }

  /// Run one script on this file, then re-read its tags: the script rewrote
  /// them in place, so both the map and the verdicts are stale.
  Future<void> _runScript(int id, String label) async {
    final client = AppScope.of(context).api;
    if (client == null) return;
    final messenger = ScaffoldMessenger.of(context);
    setState(() => _running = true);
    try {
      await client.runScripts([id], [widget.trackPath]);
      if (!mounted) return;
      messenger.showSnackBar(
        SnackBar(content: Text('$label finished — reloading the track')),
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

  List<(String, String)> _techRows() {
    final tech = _track?.tech ?? const <String, dynamic>{};
    final rows = <(String, String)>[];
    final seconds = tech['length'];
    if (seconds is num) {
      rows.add(('Duration', _duration(seconds.toInt())));
    }
    final codec = tech['codec']?.toString();
    if (codec != null && codec.isNotEmpty) {
      rows.add(('Codec', codec.toUpperCase()));
    }
    final bitrate = tech['bitrate'];
    if (bitrate is num && bitrate > 0) {
      rows.add(('Bitrate', '${(bitrate / 1000).round()} kbps'));
    }
    final rate = tech['sample_rate'];
    if (rate is num && rate > 0) {
      rows.add(('Sample rate', '${_trim(rate / 1000)} kHz'));
    }
    final bits = tech['bits_per_sample'];
    if (bits is num && bits > 0) {
      rows.add(('Bit depth', '${bits.round()} bit'));
    }
    final channels = tech['channels'];
    if (channels is num && channels > 0) {
      rows.add(('Channels', '${channels.round()}'));
    }
    final width = tech['width'];
    final height = tech['height'];
    if (width is num && height is num) {
      rows.add(('Video', '${width.round()}×${height.round()}'));
    }
    return rows;
  }

  List<(String, String)> _verdictRows() {
    final rows = <(String, String)>[];
    final track = _track;
    if (track == null) {
      // The album loaded but does not list this file: there is no verdict to
      // show, and an absent verdict is not a PASS.
      rows.add(('Grade', '—'));
      return rows;
    }
    rows.add(('Grade', track.gradePass ? 'PASS' : 'FAILED'));
    rows.add(('Audit', track.audit ?? 'not audited'));
    return rows;
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) return const Center(child: CircularProgressIndicator());
    final tagsError = _tagsError;
    if (tagsError != null) return ErrorView(message: tagsError, onRetry: _load);

    final tagRows = [
      for (final (key, label) in _tagRows)
        if (_tags[key] != null) (label, _tags[key]!),
    ];
    final gainRows = [
      for (final (key, label) in _gainRows)
        if (_tags[key] != null) (label, _tags[key]!),
    ];
    final techRows = _techRows();
    final albumPath = widget.albumPath;
    final artistPath = albumPath == null ? null : parentFolder(albumPath);
    final albumError = _albumError;

    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 16, 16, 24),
      children: [
        _header(context, albumPath, artistPath),
        if (_tags.isEmpty && _track == null)
          const EmptyHint(
            text: 'No tags and no album file were found for this track.',
            icon: Icons.audiotrack_outlined,
          )
        else ...[
          if (techRows.isNotEmpty) ...[
            SectionLabel('Audio'),
            _Facts(rows: techRows),
          ],
          SectionLabel('Grading & audit'),
          if (albumError != null)
            Text(
              'Grading data unavailable — $albumError',
              style: TextStyle(
                fontSize: 13,
                color: Theme.of(
                  context,
                ).colorScheme.onSurface.withValues(alpha: 0.6),
              ),
            )
          else
            _Facts(rows: _verdictRows()),
          if (tagRows.isNotEmpty) ...[
            SectionLabel('Tags'),
            _Facts(rows: tagRows),
          ],
          if (gainRows.isNotEmpty) ...[
            SectionLabel('ReplayGain'),
            _Facts(rows: gainRows),
          ],
        ],
        RecommendationShelf(
          kind: 'track',
          id: widget.trackPath,
          target: 'tracks',
        ),
      ],
    );
  }

  Widget _header(BuildContext context, String? albumPath, String? artistPath) {
    final muted = Theme.of(
      context,
    ).colorScheme.onSurface.withValues(alpha: 0.55);
    final meta = [
      _artist,
      _albumTitle,
      _date,
    ].whereType<String>().where((s) => s.isNotEmpty).join(' · ');
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          'TRACK',
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
                _title,
                style: const TextStyle(
                  fontSize: 22,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ),
            const SizedBox(width: 8),
            AdvisoryMark(
              value: _tags['ITUNESADVISORY'] ?? _track?.advisory,
              size: 18,
            ),
          ],
        ),
        if (meta.isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(top: 6),
            child: Text(meta, style: TextStyle(fontSize: 13, color: muted)),
          ),
        Padding(
          padding: const EdgeInsets.only(top: 4),
          child: Text(_fileName, style: TextStyle(fontSize: 12, color: muted)),
        ),
        const SizedBox(height: 12),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            FilledButton.icon(
              onPressed: _play,
              icon: const Icon(Icons.play_arrow, size: 18),
              label: const Text('Play'),
            ),
            OutlinedButton.icon(
              onPressed: _queue,
              icon: const Icon(Icons.playlist_add, size: 18),
              label: const Text('Queue'),
            ),
            if (albumPath != null)
              OutlinedButton.icon(
                onPressed: () => openDetail(context, 'Album', albumPath),
                icon: const Icon(Icons.album_outlined, size: 18),
                label: const Text('Open album'),
              ),
            if (artistPath != null)
              OutlinedButton.icon(
                onPressed: () => openDetail(context, 'Artist', artistPath),
                icon: const Icon(Icons.person_outline, size: 18),
                label: const Text('Open artist'),
              ),
            PopupMenuButton<(int, String)>(
              enabled: !_running,
              tooltip: 'Run an optimisation script on this track',
              onSelected: (script) => _runScript(script.$1, script.$2),
              itemBuilder: (context) => [
                for (final script in _scripts)
                  PopupMenuItem(value: script, child: Text(script.$2)),
              ],
              child: Container(
                padding: const EdgeInsets.symmetric(
                  horizontal: 16,
                  vertical: 14,
                ),
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
                      color: _running ? Theme.of(context).disabledColor : null,
                    ),
                    const SizedBox(width: 8),
                    Text(
                      'Run scripts',
                      style: TextStyle(
                        fontWeight: FontWeight.w500,
                        color: _running
                            ? Theme.of(context).disabledColor
                            : null,
                      ),
                    ),
                    const Icon(Icons.arrow_drop_down, size: 20),
                  ],
                ),
              ),
            ),
          ],
        ),
      ],
    );
  }

  static String _duration(int seconds) =>
      '${seconds ~/ 60}:${(seconds % 60).toString().padLeft(2, '0')}';

  static String _trim(double value) {
    final rounded = value.toStringAsFixed(1);
    return rounded.endsWith('.0')
        ? rounded.substring(0, rounded.length - 2)
        : rounded;
  }
}

/// Label/value rows for the track's facts, skipping every absent one — an
/// absent figure gets no row rather than a dash the reader has to decode.
class _Facts extends StatelessWidget {
  const _Facts({required this.rows});

  final List<(String, String)> rows;

  @override
  Widget build(BuildContext context) {
    final muted = Theme.of(
      context,
    ).colorScheme.onSurface.withValues(alpha: 0.55);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        for (final (label, value) in rows)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 3),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                SizedBox(
                  width: 150,
                  child: Text(
                    label,
                    style: TextStyle(fontSize: 12, color: muted),
                  ),
                ),
                Expanded(
                  child: Text(value, style: const TextStyle(fontSize: 13)),
                ),
              ],
            ),
          ),
      ],
    );
  }
}
