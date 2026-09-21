import 'package:flutter/material.dart';

import '../api.dart';
import '../shell.dart';
import '../state.dart';
import '../theme.dart';
import '../widgets/shelves.dart';

/// Settings: the essentials of the server's config, grouped by the work they
/// change, with the two things about this client (the address it talks to and
/// the theme) beside them.
///
/// Nothing here is client-only state: every switch and field is written back
/// through `AppState.saveConfig`, and a save the server refuses is shown in the
/// server's own words. A key this server does not know is skipped rather than
/// sent into the void.
class SettingsPage extends StatefulWidget {
  const SettingsPage({super.key});

  @override
  State<SettingsPage> createState() => _SettingsPageState();
}

class _SettingsPageState extends State<SettingsPage> {
  /// The library codec settings, in the order the group shows them. A server
  /// that does not carry a key yet simply does not render it.
  static const List<String> _codecKeys = [
    'library_codec',
    'library_codec_bitrate',
    'library_codec_quality',
    'library_codec_args',
    'library_codec_optimize',
  ];

  /// The codec targets the server accepts (`mlo.containers.CODECS`).
  static const Map<String, String> _codecLabels = {
    'flac': 'FLAC — lossless (the default)',
    'alac': 'ALAC — compressed lossless',
    'wav': 'WAV — uncompressed',
    'aiff': 'AIFF — uncompressed',
    'mp3': 'MP3 — lossy, CBR',
    'aac': 'AAC — lossy, CBR',
    'ogg': 'Ogg Vorbis — lossy',
    'opus': 'Opus — lossy',
    'keep': 'Keep — never convert',
  };

  /// What the optimisation pass may do with the target above.
  static const Map<String, String> _optimizeLabels = {
    'lossless_to_lossy': 'Lossless sources',
    'all': 'All sources',
    'keep': 'Convert nothing',
  };

  bool _loaded = false;
  bool _asked = false;
  bool _loading = true;
  bool _saving = false;
  String? _error;
  String? _healthNote;

  String _rgMode = 'track';
  final TextEditingController _preamp = TextEditingController();
  bool _measureMissing = false;
  bool _clipProtection = false;

  bool _writeRgTags = false;
  bool _writeDrTags = false;
  bool _advisoryAutoFetch = false;
  bool _moodEnabled = false;
  bool _genreAutofill = false;

  bool _importAutoScripts = false;
  final TextEditingController _importScripts = TextEditingController();

  String? _codecName;
  final TextEditingController _codecBitrate = TextEditingController();
  final TextEditingController _codecQuality = TextEditingController();
  final TextEditingController _codecArgs = TextEditingController();
  String _codecOptimize = 'lossless_to_lossy';

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final state = AppScope.of(context);
    if (!_loaded && state.config.isNotEmpty) _seed(state.config);
    if (!_asked && state.config.isEmpty) {
      _asked = true;
      _load();
    }
  }

  @override
  void dispose() {
    _preamp.dispose();
    _importScripts.dispose();
    _codecBitrate.dispose();
    _codecQuality.dispose();
    _codecArgs.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    if (!mounted) return;
    final state = AppScope.of(context);
    if (!state.connected) {
      setState(() => _loading = false);
      return;
    }
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      await state.refreshConfig();
      final client = state.api;
      if (client != null) {
        // The version line is a read-out, not a page: a health check that fails
        // says so there instead of blanking the settings.
        try {
          final health = await client.health();
          if (mounted) {
            setState(
              () => _healthNote = health['version']?.toString() ?? 'unknown',
            );
          }
        } on ApiException catch (e) {
          if (mounted) setState(() => _healthNote = e.message);
        }
      }
      if (mounted) setState(() => _loading = false);
    } on ApiException catch (e) {
      if (mounted) {
        setState(() {
          _error = e.message;
          _loading = false;
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _error = '$e';
          _loading = false;
        });
      }
    }
  }

  /// Fill the drafts from the config the server sent. Every field mirrors a
  /// real key, so a switch can never show "off" for a setting that is on.
  void _seed(Map<String, dynamic> config) {
    _loaded = true;
    _rgMode = config['replaygain_mode']?.toString() ?? 'track';
    _preamp.text = _numberText(config['replaygain_preamp_db']);
    _measureMissing = config['replaygain_analyze_missing'] == true;
    _clipProtection = config['replaygain_clip_protection'] == true;
    _writeRgTags = config['write_replaygain_tags'] == true;
    _writeDrTags = config['write_dynamic_range_tags'] == true;
    _advisoryAutoFetch = config['advisory_auto_fetch'] == true;
    _moodEnabled = config['mood_enabled'] == true;
    _genreAutofill = config['genre_autofill'] == true;
    _importAutoScripts = config['import_auto_scripts'] == true;
    _importScripts.text = _scriptText(config['import_scripts']);
    _codecName = config['library_codec']?.toString();
    _codecBitrate.text = _numberText(config['library_codec_bitrate']);
    _codecQuality.text = _numberText(config['library_codec_quality']);
    _codecArgs.text = config['library_codec_args']?.toString() ?? '';
    _codecOptimize =
        config['library_codec_optimize']?.toString() ?? 'lossless_to_lossy';
  }

  Future<void> _save(String what, Map<String, dynamic> patch) async {
    final state = AppScope.of(context);
    final messenger = ScaffoldMessenger.of(context);
    setState(() => _saving = true);
    try {
      await state.saveConfig(patch);
      messenger.showSnackBar(SnackBar(content: Text('$what saved.')));
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(e.message)));
    } catch (e) {
      messenger.showSnackBar(SnackBar(content: Text('$e')));
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  void _complain(String message) => ScaffoldMessenger.of(
    context,
  ).showSnackBar(SnackBar(content: Text(message)));

  void _saveReplayGain() {
    final preamp = double.tryParse(_preamp.text.trim());
    if (preamp == null) {
      _complain('Preamp must be a number of dB, e.g. 0, -6 or 1.5.');
      return;
    }
    _save('ReplayGain', {
      'replaygain_mode': _rgMode,
      'replaygain_preamp_db': preamp,
      'replaygain_analyze_missing': _measureMissing,
      'replaygain_clip_protection': _clipProtection,
    });
  }

  void _saveTagging() => _save('Tagging', {
    'write_replaygain_tags': _writeRgTags,
    'write_dynamic_range_tags': _writeDrTags,
    'advisory_auto_fetch': _advisoryAutoFetch,
    'mood_enabled': _moodEnabled,
    'genre_autofill': _genreAutofill,
  });

  void _saveImport() => _save('Import', {
    'import_auto_scripts': _importAutoScripts,
    // The server accepts the comma-separated form and normalizes it into the
    // id list it stores, so the field is written the way it is typed.
    'import_scripts': _importScripts.text.trim(),
  });

  void _saveCodec(Map<String, dynamic> config) {
    final patch = <String, dynamic>{};
    if (config.containsKey('library_codec') && _codecName != null) {
      patch['library_codec'] = _codecName;
    }
    if (config.containsKey('library_codec_bitrate')) {
      final bitrate = int.tryParse(_codecBitrate.text.trim());
      if (bitrate == null) {
        _complain(
          'Codec bitrate must be a whole number of kbps — 0 uses the codec\'s own default.',
        );
        return;
      }
      patch['library_codec_bitrate'] = bitrate;
    }
    if (config.containsKey('library_codec_quality')) {
      final quality = int.tryParse(_codecQuality.text.trim());
      if (quality == null) {
        _complain('Codec quality must be a whole number — 0 to 8 for FLAC.');
        return;
      }
      patch['library_codec_quality'] = quality;
    }
    if (config.containsKey('library_codec_args')) {
      patch['library_codec_args'] = _codecArgs.text;
    }
    if (config.containsKey('library_codec_optimize')) {
      patch['library_codec_optimize'] = _codecOptimize;
    }
    if (patch.isEmpty) return;
    _save('Audio codec', patch);
  }

  Future<void> _signOut() async {
    final state = AppScope.of(context);
    final navigator = Navigator.of(context, rootNavigator: true);
    setState(() => _saving = true);
    try {
      await state.logout();
    } finally {
      if (mounted) setState(() => _saving = false);
    }
    if (!mounted) return;
    // The gate checks the session when it mounts, so signing out has to put a
    // fresh one in front of the app — otherwise the pages stay on screen with
    // a token the server no longer accepts.
    navigator.pushAndRemoveUntil(
      MaterialPageRoute<void>(builder: (_) => const Gate()),
      (route) => false,
    );
  }

  @override
  Widget build(BuildContext context) {
    final state = AppScope.of(context);
    if (_error != null) return ErrorView(message: _error!, onRetry: _load);
    if (!_loaded) {
      // The shell loads the config; nothing is editable before it arrives, or a
      // switch would read as "off" and save that over the real value.
      return _loading
          ? const Center(child: CircularProgressIndicator())
          : const EmptyHint(
              text: 'No server is configured yet.',
              icon: Icons.settings_outlined,
            );
    }
    final config = state.config;
    final muted = Theme.of(
      context,
    ).colorScheme.onSurface.withValues(alpha: 0.55);
    final musicFolder = config['music_folder']?.toString() ?? '';
    final codecKeys = [
      for (final key in _codecKeys)
        if (config.containsKey(key)) key,
    ];
    // A stored codec this build does not know is still shown, not swapped for a
    // value the user did not choose.
    final codecName = _codecName ?? 'flac';
    final codecNames = [
      for (final name in _codecLabels.keys) name,
      if (!_codecLabels.containsKey(codecName)) codecName,
    ];
    final codecOptimize = _optimizeLabels.containsKey(_codecOptimize)
        ? _codecOptimize
        : 'lossless_to_lossy';

    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
      children: [
        SectionLabel(
          'ReplayGain',
          trailing: _saveButton(() => _saveReplayGain()),
        ),
        _card([
          Padding(
            padding: const EdgeInsets.fromLTRB(14, 12, 14, 4),
            child: _labeled(
              'Gain mode',
              muted,
              SegmentedButton<String>(
                segments: const [
                  ButtonSegment(value: 'track', label: Text('Track')),
                  ButtonSegment(value: 'album', label: Text('Album')),
                  ButtonSegment(value: 'off', label: Text('Off')),
                ],
                selected: {_rgMode},
                onSelectionChanged: (selection) =>
                    setState(() => _rgMode = selection.first),
              ),
            ),
          ),
          _field(
            _preamp,
            'Preamp (dB)',
            'Added to every track\'s measured gain. Between -24 and 24.',
          ),
          _switch(
            _measureMissing,
            'Measure tracks with no ReplayGain tags',
            'ffmpeg measures the loudness on the fly instead of playing the track at unity.',
            (value) => setState(() => _measureMissing = value),
          ),
          _switch(
            _clipProtection,
            'Clip protection',
            'Pull the gain back when the track\'s known peak would clip.',
            (value) => setState(() => _clipProtection = value),
          ),
        ]),
        SectionLabel('Tagging', trailing: _saveButton(_saveTagging)),
        _card([
          _switch(
            _writeRgTags,
            'Write ReplayGain tags',
            'REPLAYGAIN_TRACK_GAIN and ALBUM_GAIN.',
            (value) => setState(() => _writeRgTags = value),
          ),
          _switch(
            _writeDrTags,
            'Write DR tags',
            'DYNAMIC_RANGE, from the audio measurement.',
            (value) => setState(() => _writeDrTags = value),
          ),
          _switch(
            _advisoryAutoFetch,
            'Fetch the advisory rating automatically',
            'The import and the advisory fetch ask the providers for it.',
            (value) => setState(() => _advisoryAutoFetch = value),
          ),
          _switch(
            _moodEnabled,
            'Write mood tags',
            'MOOD and ENERGY, from the audio analysis.',
            (value) => setState(() => _moodEnabled = value),
          ),
          _switch(
            _genreAutofill,
            'Trim genres to the configured count',
            'Script 8 cuts a longer genre list back down to the genre count.',
            (value) => setState(() => _genreAutofill = value),
          ),
        ]),
        SectionLabel('Import', trailing: _saveButton(_saveImport)),
        _card([
          _switch(
            _importAutoScripts,
            'Run the script chain after import',
            'Off means an import lands in the library untouched.',
            (value) => setState(() => _importAutoScripts = value),
          ),
          _field(
            _importScripts,
            'Import script ids in order',
            'e.g. 1, 3, 5, 7 — blank runs the built-in chain.',
          ),
        ]),
        SectionLabel(
          'Audio codec',
          trailing: codecKeys.isEmpty
              ? null
              : _saveButton(() => _saveCodec(config)),
        ),
        _card([
          if (codecKeys.isEmpty)
            ListTile(
              dense: true,
              leading: Icon(Icons.info_outline, size: 18, color: muted),
              title: Text(
                'This server has no library codec settings yet.',
                style: TextStyle(fontSize: 12, color: muted),
              ),
            )
          else ...[
            if (codecKeys.contains('library_codec'))
              Padding(
                padding: const EdgeInsets.fromLTRB(14, 12, 14, 4),
                child: _labeled(
                  'Library codec',
                  muted,
                  DropdownButton<String>(
                    value: codecName,
                    isExpanded: true,
                    onChanged: _saving
                        ? null
                        : (value) => setState(() => _codecName = value),
                    items: [
                      for (final name in codecNames)
                        DropdownMenuItem(
                          value: name,
                          child: Text(_codecLabels[name] ?? name),
                        ),
                    ],
                  ),
                ),
              ),
            if (codecKeys.contains('library_codec_bitrate'))
              _field(
                _codecBitrate,
                'Bitrate (kbps)',
                'For the lossy targets; 0 uses the codec\'s own default.',
              ),
            if (codecKeys.contains('library_codec_quality'))
              _field(
                _codecQuality,
                'Compression level',
                'FLAC -0 to -8; the lossless targets that take no level ignore it.',
              ),
            if (codecKeys.contains('library_codec_args'))
              _field(
                _codecArgs,
                'Extra encoder arguments',
                'Appended verbatim — flac options for FLAC, ffmpeg options otherwise.',
              ),
            if (codecKeys.contains('library_codec_optimize'))
              Padding(
                padding: const EdgeInsets.fromLTRB(14, 8, 14, 12),
                child: _labeled(
                  'What the optimisation pass may convert',
                  muted,
                  Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      SegmentedButton<String>(
                        segments: [
                          for (final entry in _optimizeLabels.entries)
                            ButtonSegment(
                              value: entry.key,
                              label: Text(entry.value),
                            ),
                        ],
                        selected: {codecOptimize},
                        onSelectionChanged: (selection) =>
                            setState(() => _codecOptimize = selection.first),
                      ),
                      const SizedBox(height: 6),
                      Text(
                        'Lossless sources converts lossless files to the codec above and '
                        'leaves lossy ones alone; all sources also re-encodes lossy ones; '
                        'convert nothing skips the conversion entirely.',
                        style: TextStyle(fontSize: 11, color: muted),
                      ),
                    ],
                  ),
                ),
              ),
          ],
        ]),
        const SectionLabel('Server'),
        _card([
          ListTile(
            dense: true,
            leading: const Icon(Icons.dns_outlined, size: 20),
            title: const Text('Address'),
            subtitle: Text(
              state.serverUrl.isEmpty ? 'not connected' : state.serverUrl,
            ),
          ),
          if (musicFolder.isNotEmpty)
            ListTile(
              dense: true,
              leading: const Icon(Icons.folder_outlined, size: 20),
              title: const Text('Music folder'),
              subtitle: Text(musicFolder),
            ),
          ListTile(
            dense: true,
            leading: const Icon(Icons.info_outline, size: 20),
            title: const Text('Server version'),
            subtitle: Text(_healthNote ?? 'not known yet'),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(14, 4, 14, 4),
            child: _labeled(
              'Theme',
              muted,
              SegmentedButton<ThemeMode>(
                segments: const [
                  ButtonSegment(value: ThemeMode.dark, label: Text('Dark')),
                  ButtonSegment(value: ThemeMode.light, label: Text('Light')),
                  ButtonSegment(value: ThemeMode.system, label: Text('System')),
                ],
                selected: {state.themeMode},
                onSelectionChanged: (selection) =>
                    state.setTheme(selection.first),
              ),
            ),
          ),
          ListTile(
            dense: true,
            leading: const Icon(Icons.logout, size: 20),
            title: const Text('Sign out'),
            subtitle: const Text(
              'Ends this session; the library stays on the server.',
            ),
            onTap: _saving ? null : _signOut,
          ),
        ]),
      ],
    );
  }

  Widget _card(List<Widget> children) => Card(
    margin: const EdgeInsets.only(bottom: 4),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: children,
    ),
  );

  Widget _saveButton(VoidCallback onPressed) => TextButton.icon(
    onPressed: _saving ? null : onPressed,
    icon: const Icon(Icons.save_outlined, size: 16),
    label: const Text('Save'),
  );

  Widget _labeled(String label, Color muted, Widget child) => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: [
      Text(label, style: TextStyle(fontSize: 12, color: muted)),
      const SizedBox(height: 6),
      child,
    ],
  );

  Widget _switch(
    bool value,
    String title,
    String help,
    ValueChanged<bool> onChanged,
  ) => SwitchListTile(
    dense: true,
    value: value,
    onChanged: _saving ? null : onChanged,
    title: Text(title, style: const TextStyle(fontSize: 13)),
    subtitle: Text(help, style: const TextStyle(fontSize: 11)),
  );

  Widget _field(TextEditingController controller, String label, String help) =>
      Padding(
        padding: const EdgeInsets.fromLTRB(14, 6, 14, 12),
        child: TextField(
          controller: controller,
          decoration: InputDecoration(labelText: label, helperText: help),
        ),
      );

  static String _numberText(dynamic value) {
    if (value is int) return '$value';
    if (value is num) {
      // 0.0 shows as "0": a setting that starts out looking like a measurement
      // invites edits it does not need.
      return value == value.roundToDouble() ? '${value.round()}' : '$value';
    }
    return value?.toString() ?? '';
  }

  /// The id list the field holds stays an id list; anything else is passed on
  /// as typed, and the server decides.
  static String _scriptText(dynamic value) => value is List
      ? value.map((id) => id.toString()).join(', ')
      : value?.toString() ?? '';
}
