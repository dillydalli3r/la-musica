/** Stable entity references.

 The app prefers MusicBrainz IDs wherever an entity is *referenced* (page
 URLs, artist names) so links keep working when files move or
 get reorganized. Resolving "mb:<id>" back to the current path happens
 server-side (server/mbresolve.py). Tag writes always use the
 freshly resolved path.
*/

type TrackLike = { path?: string; tags?: { MUSICBRAINZ_TRACKID?: string | null } };
type AlbumLike = {
  path: string;
  meta?: { MUSICBRAINZ_ALBUMID?: string | null; MUSICBRAINZ_ALBUMARTISTID?: string | null };
};
type ArtistLike = {
  path: string;
  display_name?: string | null;
  albums?: { meta?: { MUSICBRAINZ_ALBUMARTISTID?: string | null } }[];
};

/** In-app route for a track: MBID URL when tagged, path URL otherwise. */
export function trackRef(t: TrackLike): string {
  const id = t.tags?.MUSICBRAINZ_TRACKID;
  return id ? `/track/mb:${id}` : `/track/${encodeURIComponent(t.path ?? "")}`;
}

/** In-app route for an album: MBID URL when tagged, path URL otherwise. */
export function albumRef(a: AlbumLike): string {
  const id = a.meta?.MUSICBRAINZ_ALBUMID;
  return id ? `/album/mb:${id}` : `/album/${encodeURIComponent(a.path)}`;
}

/** In-app route for the artist an ALBUM is filed under — what a card's artist
 *  caption links to. The album's own album-artist ID is the identity that
 *  survives a move (the same preference the routes above make), and the
 *  fallback is the folder the album sits in: this library's layout keeps an
 *  album at `<artist>/<album>`, so dropping the last segment names the artist
 *  folder every artist route already uses. An album with no folder of its own
 *  (a path with no separator) IS the artist's root, and is linked as itself. */
export function albumArtistRef(a: AlbumLike): string {
  const id = a.meta?.MUSICBRAINZ_ALBUMARTISTID;
  if (id) return `/artist/mb:${id}`;
  const cut = Math.max(a.path.lastIndexOf("/"), a.path.lastIndexOf("\\"));
  return `/artist/${encodeURIComponent(cut > 0 ? a.path.slice(0, cut) : a.path)}`;
}

/** The library payload has no artist-level MBID — fall back to the first
 * album's album-artist ID, then the (MBID-suffixed) folder name. */
export function artistRef(a: ArtistLike): string {
  const id = a.albums?.find((al) => al.meta?.MUSICBRAINZ_ALBUMARTISTID)?.meta
    ?.MUSICBRAINZ_ALBUMARTISTID;
  return id ? `/artist/mb:${id}` : `/artist/${encodeURIComponent(a.path)}`;
}

/** The artist's MusicBrainz album-artist ID, if any album carries one. */
export function artistMbid(a: ArtistLike): string | undefined {
  return a.albums?.find((al) => al.meta?.MUSICBRAINZ_ALBUMARTISTID)?.meta
    ?.MUSICBRAINZ_ALBUMARTISTID ?? undefined;
}
