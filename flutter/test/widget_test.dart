// What the client must get right no matter which platform renders it: the
// advisory mark's value space, the fact that a row's mark is drawn from the
// value the PAYLOAD carries (never from a later fetch), and the two payload
// shapes the server actually sends (a track's `tags` map and an album's
// `meta`, where the advisory may sit on either key).
//
// Run:  flutter test

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:la_musica/api.dart';
import 'package:la_musica/models.dart';
import 'package:la_musica/state.dart';
import 'package:la_musica/widgets/player_bar.dart';

/// The smallest tree `TrackRow` needs: it draws a cover through
/// `AppScope.of(context).api`, which is null until a server is configured —
/// and that is exactly the case the placeholder path covers.
Widget harness(Widget child) => AppScope(
  state: AppState(),
  playback: PlaybackController(state: AppState()),
  child: MaterialApp(home: Scaffold(body: child)),
);

void main() {
  group('AdvisoryMark', () {
    testWidgets('draws E for explicit and C for a clean edition', (
      tester,
    ) async {
      await tester.pumpWidget(harness(const AdvisoryMark(value: '1')));
      expect(find.text('E'), findsOneWidget);

      await tester.pumpWidget(harness(const AdvisoryMark(value: '2')));
      expect(find.text('C'), findsOneWidget);
    });

    testWidgets('draws nothing for 0, an unknown value or no value at all', (
      tester,
    ) async {
      for (final value in <String?>['0', '3', '', null]) {
        await tester.pumpWidget(harness(AdvisoryMark(value: value)));
        expect(find.text('E'), findsNothing, reason: 'value=$value');
        expect(find.text('C'), findsNothing, reason: 'value=$value');
      }
    });
  });

  group('Track', () {
    test('reads the advisory and the title out of the tags map', () {
      final track = Track.fromJson({
        'path': r'F:\music\Artists\A\Album\01 - Song.flac',
        'file': '01 - Song.flac',
        'tags': {'TITLE': 'Song', 'ITUNESADVISORY': '1', 'INSTRUMENTAL': '1'},
        'tech': {'length': 187},
      });
      expect(track.advisory, '1');
      expect(track.instrumental, '1');
      expect(track.displayTitle, 'Song');
    });

    test('falls back to the file name only when the file has no TITLE tag', () {
      final track = Track.fromJson({
        'path': r'F:\music\Artists\A\Album\01 - Song.flac',
        'file': '01 - Song.flac',
        'tags': <String, dynamic>{},
      });
      expect(track.displayTitle, '01 - Song');
    });
  });

  group('Album', () {
    test('prefers the per-track advisory and falls back to the album one', () {
      final explicit = Album.fromJson({
        'path': '/music/A/B',
        'meta': {
          'ALBUM': 'B',
          'ITUNESADVISORY': '1',
          'ALBUMITUNESADVISORY': '0',
        },
      });
      expect(explicit.advisory, '1');

      final cleanEdition = Album.fromJson({
        'path': '/music/A/C',
        'meta': {'ALBUM': 'C', 'ALBUMITUNESADVISORY': '2'},
      });
      expect(cleanEdition.advisory, '2');

      final unknown = Album.fromJson({
        'path': '/music/A/D',
        'meta': {'ALBUM': 'D'},
      });
      expect(unknown.advisory, isNull);
    });
  });

  group('ReplayGain', () {
    test('converts dB to the linear amplitude a player takes', () {
      // -6.02 dB is half amplitude; 0 dB is unity; a missing gain is unity too.
      expect(ReplayGain(gain: -6.0206).linear, closeTo(0.5, 0.001));
      expect(ReplayGain(gain: 0).linear, closeTo(1.0, 0.0001));
      expect(ReplayGain().linear, 1.0);
    });

    test('clamps a hot gain instead of letting it run away', () {
      expect(ReplayGain(gain: 40).linear, lessThanOrEqualTo(4.0));
    });
  });

  group('TrackRow', () {
    testWidgets('shows the mark beside the title on the first frame', (
      tester,
    ) async {
      final track = Track.fromJson({
        'path': '/music/A/B/01 - Song.flac',
        'file': '01 - Song.flac',
        'tags': {'TITLE': 'Song', 'ITUNESADVISORY': '1'},
      });
      await tester.pumpWidget(harness(TrackRow(track: track)));
      expect(find.text('Song'), findsOneWidget);
      expect(find.text('E'), findsOneWidget);
    });
  });
}
