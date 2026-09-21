import 'models.dart';

/// The library payload indexed for the pages that hold bare paths — a
/// playlist's tracks, a like, a favourite folder. The shell loads the library
/// once; this turns it into the lookups those pages need, instead of each of
/// them walking the tree and each of them deciding how a queue entry is built.
class LibraryIndex {
  LibraryIndex(this.library) {
    for (final artist in library?.artists ?? const <Artist>[]) {
      artists[artist.path] = artist;
      for (final album in artist.albums) {
        albums[album.path] = album;
        for (final track in album.tracks) {
          tracks[track.path] = TrackHit(
            track: track,
            album: album,
            artist: artist,
          );
        }
      }
    }
  }

  final Library? library;
  final Map<String, TrackHit> tracks = {};
  final Map<String, Album> albums = {};
  final Map<String, Artist> artists = {};

  TrackHit? track(String path) => tracks[path];
}

/// One library track with the album and artist it lives in: what a row drawn
/// from a bare path needs, including the queue entry the player takes.
class TrackHit {
  TrackHit({required this.track, required this.album, required this.artist});

  final Track track;
  final Album album;
  final Artist artist;

  /// ALBUMARTIST when the album has one, the artist folder otherwise — the
  /// same name the library rows show.
  String get artistName => album.artist ?? artist.name;

  /// The queue entry the player takes: the file's own tags for the row, with
  /// the advisory in-band so the player bar can mark it.
  Map<String, dynamic> get queueEntry => {
    ...track.toQueueJson(),
    'albumPath': album.path,
    'album': album.title,
    'artist': artistName,
    'coverFile': track.coverFile,
    'albumCover': album.coverFile,
    'advisory': track.advisory,
  };
}

/// The file name of a path, with its extension — the visible part of a path.
String fileName(String path) => path.split(RegExp(r'[\\/]')).last;

/// The library album a discovery row's path belongs to: the folder itself for
/// an album, the album holding the file for a track. Null for a row the library
/// does not have, and for an artist row — an artist folder is not an album, so
/// its art comes from the provider instead.
Album? discoverOwnedAlbum(LibraryIndex index, DiscoverItem item) {
  if (!item.owned && !item.inLibrary) return null;
  final path = item.path ?? '';
  if (path.isEmpty) return null;
  return index.albums[path] ?? index.track(path)?.album;
}

/// The row a path the library does not know still deserves: its file name as
/// the title, and nothing else claimed about a track that is not there.
Track looseTrack(String path) => Track(path: path, file: fileName(path));

/// The queue entry for a path outside the library (a moved or deleted file):
/// the player gets the path and the folder it expected to be in, and the
/// stream fails by itself if the file is really gone.
Map<String, dynamic> looseQueueEntry(String path) {
  final parts = path.split(RegExp(r'[\\/]'));
  return {
    'path': path,
    'file': parts.last,
    'albumPath': parts.length > 1
        ? parts.sublist(0, parts.length - 1).join('/')
        : '',
  };
}

/// A track's length in seconds, when its probe found one.
num? trackSeconds(Track? track) {
  final value = track?.tech['length'];
  return value is num ? value : null;
}

/// The summed length of a set of tracks, in seconds.
num totalSeconds(Iterable<Track?> tracks) =>
    tracks.fold<num>(0, (sum, track) => sum + (trackSeconds(track) ?? 0));

/// mm:ss, or h:mm:ss past an hour — "—" when there is no length to show.
/// The same readout the React client formats, so the two agree digit for digit.
String fmtLength(num? seconds) {
  if (seconds == null || seconds < 0) return '—';
  final total = seconds.floor();
  final hours = total ~/ 3600;
  final minutes = (total % 3600) ~/ 60;
  final rest = (total % 60).toString().padLeft(2, '0');
  if (hours > 0) return '$hours:${minutes.toString().padLeft(2, '0')}:$rest';
  return '$minutes:$rest';
}
