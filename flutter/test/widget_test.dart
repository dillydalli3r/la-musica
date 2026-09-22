// What the client must get right no matter which platform renders it: the
// advisory mark's value space, the fact that a row's mark is drawn from the
// value the PAYLOAD carries (never from a later fetch), and the two payload
// shapes the server actually sends (a track's `tags` map and an album's
// `meta`, where the advisory may sit on either key).
//
// The pending mark is the same kind of claim: a framework album's marker is
// drawn from the album payload's own block — the flag, what the folder waits
// for and the wish behind it — so a folder with no audio can never be drawn
// as one that plays, and a complete album never carries a mark at all.
//
// Run:  flutter test

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:la_musica/api.dart';
import 'package:la_musica/models.dart';
import 'package:la_musica/pages/settings.dart';
import 'package:la_musica/state.dart';
import 'package:la_musica/widgets/discover_row.dart';
import 'package:la_musica/widgets/player_bar.dart';
import 'package:la_musica/widgets/shelves.dart';

/// The smallest tree `TrackRow` needs: it draws a cover through
/// `AppScope.of(context).api`, which is null until a server is configured —
/// and that is exactly the case the placeholder path covers.
Widget harness(Widget child) => AppScope(
  state: AppState(),
  playback: PlaybackController(state: AppState()),
  child: MaterialApp(home: Scaffold(body: child)),
);

/// A controller that records the one thing the settings page has to reach: the
/// gain stage of the track that is ALREADY playing. `refreshGain` is that
/// re-apply — it had no caller at all before the settings page called it, so a
/// mode/preamp edit only ever reached the next track. Overridden because the
/// real one talks to just_audio, which has no platform implementation under
/// `flutter test`.
class _SpyPlayback extends PlaybackController {
  _SpyPlayback({required super.state});

  int refreshes = 0;

  @override
  Future<void> refreshGain() async {
    refreshes++;
  }
}

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

  group('PendingMark', () {
    // The framework-album payload as the server stamps it: the flag, what the
    // folder waits for, and the wish hunting its audio (`server/library.py`).
    // `due_in` is seconds until the next search.
    Map<String, dynamic> payload({required bool pending}) => {
      'path': r'F:\music\Artists\A\Album',
      'meta': {'ALBUM': 'Album', 'ALBUMARTIST': 'A'},
      'pending': pending,
      if (pending) ...{
        'pending_reason': 'a verified Soulseek download',
        'wish_id': 7,
        'wish': {
          'id': 7,
          'status': 'searching',
          'attempts': 1,
          'due_in': 720,
          'terminal': false,
          'reason': '',
        },
      },
    };

    testWidgets('marks a framework album and leaves a complete one alone', (
      tester,
    ) async {
      final waiting = Album.fromJson(payload(pending: true));
      await tester.pumpWidget(harness(PendingMark(waiting, label: true)));
      expect(find.text('not downloaded yet'), findsOneWidget);
      expect(
        find.byTooltip(
          'Waiting for its audio · a verified Soulseek download · '
          'searching — attempt 2, next try in 12 min',
        ),
        findsOneWidget,
      );

      // On the tile itself: the mark is drawn, and the play badge — the
      // control that would start an empty queue — is off.
      var played = 0;
      await tester.pumpWidget(
        harness(AlbumCard(album: waiting, onPlay: () => played++)),
      );
      expect(find.byIcon(Icons.schedule), findsOneWidget);
      await tester.tap(find.byIcon(Icons.play_arrow));
      expect(played, 0);

      // A complete album carries none of it: no mark, and the badge plays.
      final owned = Album.fromJson(payload(pending: false));
      await tester.pumpWidget(
        harness(AlbumCard(album: owned, onPlay: () => played++)),
      );
      expect(find.byIcon(Icons.schedule), findsNothing);
      expect(find.byTooltip('Play album'), findsOneWidget);
      await tester.tap(find.byIcon(Icons.play_arrow));
      expect(played, 1);
    });
  });

  group('ReplayGain', () {
    test('converts dB to the linear amplitude a player takes', () {
      // -6.02 dB is half amplitude; 0 dB is unity; a missing gain is unity too.
      expect(ReplayGain(gain: -6.0206).linear, closeTo(0.5, 0.001));
      expect(ReplayGain(gain: 0).linear, closeTo(1.0, 0.0001));
      expect(ReplayGain().linear, 1.0);
    });

    test('clamps dB to the same window the web player uses', () {
      // A quiet master legitimately asks for more than +12 dB, and clamping the
      // LINEAR value at 4.0 silently under-applied every gain above it — the
      // phone then played the same track at a different level than the desktop.
      // The window is the player's own: ±24 dB (web/src/lib/analyser.ts), which
      // is what the preamp is clamped to server-side too.
      expect(ReplayGain(gain: 18).linear, closeTo(7.943, 0.01));
      expect(ReplayGain(gain: 40).linear, closeTo(15.849, 0.01));
      expect(ReplayGain(gain: -60).linear, closeTo(0.063, 0.001));
    });

    test('reads pending and album out of the payload', () {
      // `pending` says the unity is temporary — the server is still measuring
      // the file — and `album` says the number really is the album gain. Both
      // change what the controller does, so both have to survive the parse.
      final pending = ReplayGain.fromJson({
        'gain': null,
        'mode': 'album',
        'pending': true,
      });
      expect(pending.linear, 1.0);
      expect(pending.pending, isTrue);
      expect(pending.album, isFalse);
      final album = ReplayGain.fromJson({
        'gain': -7.2,
        'album': true,
        'analyzed': true,
      });
      expect(album.album, isTrue);
      expect(album.pending, isFalse);
    });
  });

  group('ReplayGain settings reach the running player', () {
    testWidgets('saving the section re-applies the gain to the playing track', (
      tester,
    ) async {
      SharedPreferences.setMockInitialValues({});
      final calls = <String>[];
      final state = AppState(
        clientFactory: () => MockClient((request) async {
          calls.add('${request.method} ${request.url.path}');
          if (request.url.path.endsWith('/health')) {
            return http.Response(
              jsonEncode({'status': 'ok', 'version': 'test'}),
              200,
            );
          }
          if (request.url.path.endsWith('/config')) {
            return http.Response(
              jsonEncode({'replaygain_mode': 'album', 'replaygain_preamp_db': 0}),
              200,
            );
          }
          return http.Response('{}', 200);
        }),
      );
      state.config = {'replaygain_mode': 'track', 'replaygain_preamp_db': 0};
      await state.connect('http://fixture.invalid');
      final playback = _SpyPlayback(state: state);

      await tester.pumpWidget(
        AppScope(
          state: state,
          playback: playback,
          child: const MaterialApp(home: Scaffold(body: SettingsPage())),
        ),
      );
      await tester.pump();

      // The ReplayGain section's own Save: the sheet's first section, and the
      // button sits in that section's label row.
      final save = find.descendant(
        of: find
            .ancestor(of: find.text('REPLAYGAIN'), matching: find.byType(Row))
            .first,
        matching: find.byType(TextButton),
      );
      expect(save, findsOneWidget);
      await tester.tap(save);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(calls, contains('POST /api/config'));
      expect(
        playback.refreshes,
        1,
        reason: 'the track that is already playing must get the new value',
      );
      playback.dispose();
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

  group('Discover notes and the add affordance', () {
    // The server's own lines, as `server/discover.py` writes them: a real
    // failure in the provider's words, the one actionable silence, a long
    // reason, and the artist level an album page cannot be answered at. What
    // the client must get right is that the LABEL never ends mid-sentence
    // (the whole sentence stays in the tooltip) and that a capability the
    // provider publishes is never drawn as a failure.
    test('labels a note with its own first clause, never a cut sentence', () {
      expect(
        discoverNoteLabel('skipped: no lastfm_api_key'),
        'no lastfm_api_key',
      );
      expect(
        discoverNoteLabel('failed: Deezer refused: 403 "Quota exceeded"'),
        'Deezer refused: 403 "Quota exceeded"',
      );
      expect(
        discoverNoteLabel(
          'skipped: RateYourMusic knows no artist called "Blur" — its own '
          'chart filter matched no chart',
        ),
        'RateYourMusic knows no artist called "Blur"',
      );
      expect(
        discoverNoteLabel(
          'partial: 500 of 2202 genres — the list is cut at the source\'s '
          'page size',
        ),
        'partial: 500 of 2202 genres',
      );
      // One clause, longer than a label may be: the outcome word stands in
      // rather than the clause being clipped to fit.
      expect(
        discoverNoteLabel(
          'skipped: could not resolve "Nobody At All" on MusicBrainz, and this '
          'feed is MBID-native',
        ),
        'skipped',
      );
    });

    testWidgets('shows a capability as information and a failure as an error', (
      tester,
    ) async {
      await tester.pumpWidget(
        harness(
          DiscoverNotes(
            notes: {'deezer': 'failed: Deezer refused: 403 "Quota exceeded"'},
            notApplicable: [
              DiscoverNotApplicable(
                id: 'lastfm',
                label: 'Last.fm',
                short: 'artist & track pages only',
                why:
                    'Last.fm\'s entity feeds are similar ARTISTS and similar '
                    'TRACKS — it has no similar-ALBUMS feed',
              ),
            ],
          ),
        ),
      );
      expect(
        find.text('Deezer — Deezer refused: 403 "Quota exceeded"'),
        findsOneWidget,
      );
      expect(
        find.text(
          'cannot answer for this page: Last.fm (artist & track pages only)',
        ),
        findsOneWidget,
      );
      // Neither sentence is lost: each is the chip's (or the line's) tooltip.
      expect(
        find.byTooltip('failed: Deezer refused: 403 "Quota exceeded"'),
        findsOneWidget,
      );
      expect(
        find.byTooltip(
          'Last.fm — Last.fm\'s entity feeds are similar ARTISTS and similar '
          'TRACKS — it has no similar-ALBUMS feed',
        ),
        findsOneWidget,
      );
    });

    testWidgets('offers the add for a row with no MusicBrainz id', (
      tester,
    ) async {
      final deezer = DiscoverItem.fromJson({
        'kind': 'album',
        'title': 'The Magic Whip',
        'artist': 'Blur',
        'year': '2015',
        'source': 'deezer',
        'source_label': 'Deezer',
      });
      await tester.pumpWidget(
        harness(DiscoverRow(item: deezer, onAdd: (item) async => null)),
      );
      expect(find.text('Add to library'), findsOneWidget);

      // A row that names NOTHING has nothing to search for: the action that
      // could only be answered with a 400 is not offered at all.
      await tester.pumpWidget(
        harness(
          DiscoverRow(
            item: DiscoverItem.fromJson({'kind': 'album', 'title': ''}),
            onAdd: (item) async => null,
          ),
        ),
      );
      expect(find.text('Add to library'), findsNothing);
    });

    testWidgets('reports a name-keyed add for what it was, not as nothing', (
      tester,
    ) async {
      final noMatch = LibraryAddResult.fromJson({
        'ok': true,
        'matched': false,
        'by_name': true,
        'wish_id': 9,
        'queued': 0,
      });
      expect(
        noMatch.message,
        'Added — no MusicBrainz match, searching by name',
      );
      final deezer = DiscoverItem.fromJson({
        'kind': 'album',
        'title': 'The Magic Whip',
        'artist': 'Blur',
        'source': 'deezer',
        'source_label': 'Deezer',
      });
      await tester.pumpWidget(
        harness(DiscoverRow(item: deezer, onAdd: (item) async => noMatch)),
      );
      await tester.tap(find.text('Add to library'));
      await tester.pumpAndSettle();
      // A wish is searching, and the server named it: the row says so and the
      // undo applies to that wish rather than to no album at all.
      expect(find.text('Queued — searching by name'), findsOneWidget);
      expect(find.text('Undo'), findsOneWidget);
    });

    test('parses the capability line an entity shelf carries', () {
      final shelf = DiscoverRecommendedResult.fromJson({
        'items': [],
        'sources_asked': ['musicbrainz', 'deezer'],
        'notes': {'spotify': 'skipped: no spotify_client_id'},
        'basis': 'album: Blur — The Magic Whip (by name)',
        'not_applicable': [
          {
            'id': 'lastfm',
            'label': 'Last.fm',
            'short': 'artist & track pages only',
            'why':
                'Last.fm\'s entity feeds are similar ARTISTS and similar '
                'TRACKS — it has no similar-ALBUMS feed',
          },
        ],
      });
      expect(shelf.notApplicable.single.id, 'lastfm');
      expect(shelf.notApplicable.single.short, 'artist & track pages only');
      // The library and genre seeds send none, and that is not an error.
      expect(
        DiscoverRecommendedResult.fromJson({'items': []}).notApplicable,
        isEmpty,
      );
    });
  });
}
