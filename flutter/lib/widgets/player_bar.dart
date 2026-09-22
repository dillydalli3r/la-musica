import 'package:flutter/material.dart';

import '../models.dart';
import '../state.dart';

/// The cover thumbnail: the album's own art when the file exists, the album
/// cover as fallback, and a disc icon when neither does. Same order as the
/// React `CoverImg`, including "a track with no cover gets NO cover in album
/// view" (`albumFallback: false`).
class CoverArt extends StatelessWidget {
  const CoverArt({
    super.key,
    required this.albumPath,
    this.coverFile,
    this.size = 40,
    this.radius = 6,
  });

  final String albumPath;
  final String? coverFile;
  final double size;
  final double radius;

  @override
  Widget build(BuildContext context) {
    final client = AppScope.of(context).api;
    final file = coverFile;
    final child = (client == null || file == null || file.isEmpty)
        ? _placeholder(context)
        : Image.network(
            client.coverUrl(albumPath, file),
            width: size,
            height: size,
            fit: BoxFit.cover,
            errorBuilder: (context, _, __) => _placeholder(context),
            loadingBuilder: (context, child, progress) =>
                progress == null ? child : _placeholder(context),
          );
    return ClipRRect(
      borderRadius: BorderRadius.circular(radius),
      child: SizedBox(width: size, height: size, child: child),
    );
  }

  Widget _placeholder(BuildContext context) => Container(
    color: Theme.of(context).colorScheme.surfaceContainerHighest,
    alignment: Alignment.center,
    child: Icon(
      Icons.album_outlined,
      size: size / 2,
      color: Theme.of(context).colorScheme.onSurface.withValues(alpha: 0.35),
    ),
  );
}

/// The iTunes-style advisory mark beside a title: a red boxed E for explicit,
/// a green boxed C for a clean edition, nothing for 0 or unknown.
///
/// It is drawn from the value the row already carries, so it paints on the
/// same frame as the title — the whole point of the queue carrying
/// ITUNESADVISORY in-band (web: issue #13).
class AdvisoryMark extends StatelessWidget {
  const AdvisoryMark({super.key, this.value, this.size = 14});

  final String? value;
  final double size;

  @override
  Widget build(BuildContext context) {
    final v = value?.trim();
    if (v != '1' && v != '2') return const SizedBox.shrink();
    final explicit = v == '1';
    return Container(
      width: size,
      height: size,
      alignment: Alignment.center,
      decoration: BoxDecoration(
        color: explicit ? const Color(0xFFDC2626) : const Color(0xFF064E3B),
        border: explicit ? null : Border.all(color: const Color(0xFF10B981)),
        borderRadius: BorderRadius.circular(3),
      ),
      child: Text(
        explicit ? 'E' : 'C',
        style: TextStyle(
          fontSize: size * 0.62,
          height: 1,
          fontWeight: FontWeight.w700,
          color: explicit ? Colors.white : const Color(0xFF34D399),
        ),
      ),
    );
  }
}

/// A track row: number, cover, title with its advisory mark, the bits the
/// player shows (duration, codec), and the two verdicts as a leading edge —
/// the layout the React library rows use.
class TrackRow extends StatelessWidget {
  const TrackRow({
    super.key,
    required this.track,
    this.albumPath = '',
    this.showCover = true,
    this.onTap,
    this.trailing,
  });

  final Track track;
  final String albumPath;
  final bool showCover;
  final VoidCallback? onTap;
  final Widget? trailing;

  @override
  Widget build(BuildContext context) {
    final muted = Theme.of(
      context,
    ).colorScheme.onSurface.withValues(alpha: 0.5);
    final seconds = track.tech['length'];
    final duration = seconds is num ? _mmss(seconds.toInt()) : null;
    return InkWell(
      onTap: onTap,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
        child: Row(
          children: [
            SizedBox(
              width: 28,
              child: Text(
                track.tracknumber?.toString() ?? '—',
                textAlign: TextAlign.right,
                style: TextStyle(
                  fontSize: 12,
                  color: muted,
                  fontFeatures: const [],
                ),
              ),
            ),
            const SizedBox(width: 8),
            if (showCover) ...[
              CoverArt(
                albumPath: albumPath,
                coverFile: track.coverFile,
                size: 34,
              ),
              const SizedBox(width: 8),
            ],
            Container(
              width: 3,
              height: 22,
              decoration: BoxDecoration(
                color: track.gradePass
                    ? const Color(0xFF10B981)
                    : const Color(0xFF52525B),
                borderRadius: BorderRadius.circular(2),
              ),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: Row(
                children: [
                  Flexible(
                    child: Text(
                      track.displayTitle,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(fontSize: 13),
                    ),
                  ),
                  const SizedBox(width: 6),
                  AdvisoryMark(value: track.advisory),
                  if (track.instrumental == '1') ...[
                    const SizedBox(width: 6),
                    Icon(Icons.music_note_outlined, size: 13, color: muted),
                  ],
                ],
              ),
            ),
            if (duration != null)
              Padding(
                padding: const EdgeInsets.only(left: 8),
                child: Text(
                  duration,
                  style: TextStyle(fontSize: 11, color: muted),
                ),
              ),
            if (trailing != null) trailing!,
          ],
        ),
      ),
    );
  }

  static String _mmss(int seconds) =>
      '${seconds ~/ 60}:${(seconds % 60).toString().padLeft(2, '0')}';
}

/// The player bar under every page: title with its advisory mark, transport,
/// a seek bar, volume, and the ReplayGain readout (dB + where it came from).
class PlayerBar extends StatelessWidget {
  const PlayerBar({super.key});

  @override
  Widget build(BuildContext context) {
    final playback = AppScope.playbackOf(context);
    final state = AppScope.of(context);
    final muted = Theme.of(
      context,
    ).colorScheme.onSurface.withValues(alpha: 0.55);
    final surface = Theme.of(context).colorScheme.surfaceContainerHighest;

    return AnimatedBuilder(
      animation: playback,
      builder: (context, _) {
        final track = playback.current;
        return Container(
          decoration: BoxDecoration(
            color: surface,
            border: Border(
              top: BorderSide(color: Theme.of(context).dividerColor),
            ),
          ),
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (track != null)
                Row(
                  children: [
                    CoverArt(
                      albumPath: track['albumPath']?.toString() ?? '',
                      coverFile: track['coverFile']?.toString(),
                      size: 34,
                    ),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Row(
                            children: [
                              Flexible(
                                child: Text(
                                  playback.currentTitle,
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                  style: const TextStyle(
                                    fontSize: 13,
                                    fontWeight: FontWeight.w600,
                                  ),
                                ),
                              ),
                              const SizedBox(width: 6),
                              AdvisoryMark(
                                value: track['advisory']?.toString(),
                              ),
                            ],
                          ),
                          Text(
                            [
                                  track['artist']?.toString() ??
                                      (track['albumPath']?.toString() ?? '')
                                          .split(RegExp(r'[\\/]'))
                                          .last,
                                  track['album']?.toString(),
                                ]
                                .whereType<String>()
                                .where((s) => s.isNotEmpty)
                                .join(' · '),
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: TextStyle(fontSize: 11, color: muted),
                          ),
                        ],
                      ),
                    ),
                    IconButton(
                      onPressed: playback.previous,
                      icon: const Icon(Icons.skip_previous, size: 20),
                      tooltip: 'Previous',
                    ),
                    IconButton.filled(
                      onPressed: playback.toggle,
                      icon: Icon(
                        playback.playing ? Icons.pause : Icons.play_arrow,
                        size: 22,
                      ),
                      tooltip: playback.playing ? 'Pause' : 'Play',
                    ),
                    IconButton(
                      onPressed: playback.next,
                      icon: const Icon(Icons.skip_next, size: 20),
                      tooltip: 'Next',
                    ),
                    Tooltip(
                      message: _gainTooltip(
                        playback,
                        state.configValue<String>('replaygain_mode', 'track'),
                      ),
                      child: Text(
                        playback.gainDb == 0
                            ? 'RG 0.0 dB'
                            : 'RG ${playback.gainDb.toStringAsFixed(2)} dB',
                        style: TextStyle(fontSize: 11, color: muted),
                      ),
                    ),
                    SizedBox(
                      width: 120,
                      child: Slider(
                        value: playback.volume,
                        onChanged: playback.setVolume,
                      ),
                    ),
                    if (state.configValue<String>('replaygain_mode', 'track') !=
                        'track')
                      Padding(
                        padding: const EdgeInsets.only(left: 6),
                        child: Text(
                          state.configValue<String>('replaygain_mode', 'track'),
                          style: TextStyle(fontSize: 11, color: muted),
                        ),
                      ),
                  ],
                ),
              StreamBuilder<Duration>(
                stream: playback.player.positionStream,
                builder: (context, snapshot) {
                  final position = snapshot.data ?? Duration.zero;
                  final total = playback.duration;
                  final max = (total?.inMilliseconds ?? 1).toDouble();
                  return Slider(
                    value: position.inMilliseconds
                        .clamp(0, max.toInt())
                        .toDouble(),
                    max: max <= 0 ? 1 : max,
                    onChanged: total == null
                        ? null
                        : (v) =>
                              playback.seek(Duration(milliseconds: v.toInt())),
                  );
                },
              ),
              if (playback.loadError != null)
                Align(
                  alignment: Alignment.centerLeft,
                  child: Text(
                    playback.loadError!,
                    style: const TextStyle(
                      fontSize: 11,
                      color: Color(0xFFF87171),
                    ),
                  ),
                ),
            ],
          ),
        );
      },
    );
  }

  static String _gainTooltip(PlaybackController playback, String mode) {
    if (playback.gainSource == null) {
      return 'Unity — no ReplayGain for this file';
    }
    final origin = playback.gainAnalyzed
        ? 'measured on demand (no ReplayGain tags in the file)'
        : 'from the file\'s ReplayGain tags';
    // Album mode and NOT the album gain: this album carries no
    // REPLAYGAIN_ALBUM_GAIN, so each of its tracks is normalised on its own —
    // say which value was used instead of implying album normalisation.
    final album = mode == 'album' && !playback.gainAlbum
        ? '; this album has no album gain, so its track value was used'
        : '';
    return 'ReplayGain ${playback.gainDb.toStringAsFixed(2)} dB — $origin ($playback.gainSource)$album';
  }
}
