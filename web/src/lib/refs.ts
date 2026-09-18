/** Stable entity references.

 The app prefers MusicBrainz IDs wherever an entity is *referenced* (page
 URLs, favorites, playlist rows) so links keep working when files move or
 get reorganized. Resolving "mb:<id>" back to the current path happens
 server-side (server/mbresolve.py). Playback and tag writes always use the
 freshly resolved path.
*/

import type { MouseEvent } from "react";

/** Shared click behavior for entity-title links on play rows: a plain mouse
 * click falls through to the row's play handler (preventDefault stops the
 * router), while Ctrl/Shift/cmd-click — and a keyboard activation, which
 * arrives as a click with detail 0 — opens the target page IN-APP: the
 * browser's new-tab default is deliberately suppressed so the viewer stays
 * inside the app. Middle-click keeps the native new-tab behavior. */
export function entityLinkClick(e: MouseEvent, open: () => void) {
  if (e.ctrlKey || e.metaKey || e.shiftKey || e.detail === 0) {
    e.preventDefault();
    e.stopPropagation();
    open();
  } else {
    e.preventDefault();
  }
}

type TrackLike = { path?: string; tags?: { MUSICBRAINZ_TRACKID?: string | null } };
type AlbumLike = { path: string; meta?: { MUSICBRAINZ_ALBUMID?: string | null } };
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
