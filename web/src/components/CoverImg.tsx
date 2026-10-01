import { useEffect, useState } from "react";
import { Disc3 } from "lucide-react";
import { api, isOffline } from "../api";
import { offlineArtworkUrl, useCachedArtwork } from "../lib/mediaCache";

/** The two widths the player's own surfaces ask the server for (bucketed
 *  server-side, see `artcache.THUMB_SIZES`). Declared here because this file
 *  is where a cover is drawn: a row's art and the player BAR's thumb are
 *  32-74 px and both ask for `ROW_COVER_W`, so they share ONE request; the
 *  fullscreen picture is up to 448 px and asks for `PANE_COVER_W`, which its
 *  blurred ambient layer shares. */
export const ROW_COVER_W = 160;
export const PANE_COVER_W = 640;

/** Cover thumbnail with a graceful fallback when the art is missing or
 *  fails to load. `wrapperClass` sizes the box; the image fills it.
 *
 * When the image's bytes are in the offline cache (the album was downloaded),
 * the network URL paints first and the cached copy swaps in behind it — the
 * first paint MUST NOT wait on Cache Storage, so the online path looks and
 * times exactly as it does without the cache. Offline, that swap is what
 * keeps a downloaded album's art from collapsing to the Disc3 placeholder.
 *
 * That swap is only worth ASKING for when the bytes can be there, though: the
 * question costs a Cache Storage transaction and a state update, and a library
 * of 50 000 rows asks it 50 000 times. So the probe runs only for an album the
 * offline cache is known to hold (`useCachedArtwork`, off the shared
 * `['cachedPaths']` snapshot) or while the client is actually offline, where
 * every cover is a candidate. Online with nothing downloaded — the ordinary
 * case — the network URL is the whole story. */
export default function CoverImg({
  albumPath,
  coverFile,
  staged = false,
  w,
  wrapperClass = "h-9 w-9 rounded bg-raise overflow-hidden shrink-0",
}: {
  albumPath: string;
  coverFile?: string | null;
  /** The width the surface DRAWS, so the server can serve a thumbnail instead
   *  of the 1200-3000 px master (see `api.coverUrl`'s `w`). Omitted, the
   *  master is served — which is right for a surface that really draws it
   *  large, and what every grid card outside the player's own path still
   *  does. Two surfaces asking for the same width share one request. */
  w?: number;
  /** The import wizard's album — a folder the library does not list yet, whose
   *  cover the server serves only to a request that says so (the same opt-in
   *  every other call the wizard makes passes). A preview URL without it is
   *  refused, and the image stays empty however well the cover was written. */
  staged?: boolean;
  wrapperClass?: string;
}) {
  // Remembered per URL, not as a bare flag: a row recycled onto another album
  // must not inherit the previous cover's failure.
  const [failedUrl, setFailedUrl] = useState<string | null>(null);
  // A write that changed this cover's bytes gives it a new URL (see
  // lib/invalidate): a different src is what re-fetches it, and it also clears
  // a remembered failure, so a cover that was missing and then uploaded loads
  // without a reload.
  const networkUrl = coverFile ? api.coverUrl(albumPath, coverFile, { staged, w }) : null;
  const [offlineUrl, setOfflineUrl] = useState<string | null>(null);
  // Answered from the cached-track snapshot (lib/mediaCache), never by opening
  // Cache Storage here: it is the one question the probe below is gated on.
  const artworkDownloaded = useCachedArtwork(albumPath);

  useEffect(() => {
    if (!networkUrl) return;
    let live = true;
    setOfflineUrl(null); // a different cover must not inherit the old blob
    // Nothing to find when the album is not in the offline cache AND the
    // client is on the network: the probe would open Cache Storage, miss, and
    // re-render — per cover, per visit. Offline (the SW's own readout, or the
    // browser's) every cover is a candidate again: `navigator.onLine` is false
    // in a shell or a browser with no link at all, and `isOffline()` is what
    // the API sets the moment it answers from its offline copy, which is the
    // state a downloaded cover exists for. Both are read at run time rather
    // than watched: a client that goes offline keeps the art it is already
    // showing, and the next mount probes (see the doc comment above).
    if (!artworkDownloaded && !isOffline() && navigator.onLine) return;
    offlineArtworkUrl(networkUrl).then((u) => {
      if (live) setOfflineUrl(u);
    });
    return () => {
      live = false;
    };
  }, [networkUrl, artworkDownloaded]);

  // A blob URL from Cache Storage still wins over a failed network load: the
  // request that failed was made precisely because the cached copy was not
  // consulted first.
  if (!networkUrl || (failedUrl === networkUrl && !offlineUrl)) {
    return (
      <div className={`${wrapperClass} flex items-center justify-center text-zinc-700`}>
        <Disc3 className="h-1/2 w-1/2 max-h-5 max-w-5" />
      </div>
    );
  }
  return (
    <div className={wrapperClass}>
      <img
        src={offlineUrl ?? networkUrl}
        alt=""
        loading="lazy"
        decoding="async"
        onError={() => setFailedUrl(networkUrl)}
        className="h-full w-full object-cover"
      />
    </div>
  );
}

/** Track-row cover: per-track sidecar art when the track has its own,
 * otherwise the album cover as fallback. In album views pass
 * `albumFallback={null}` to show ONLY track-specific covers (an empty
 * cell keeps the column aligned when the track has none). */
export function TrackCover({
  albumPath,
  trackCover,
  albumCover,
  albumFallback = true,
  staged = false,
  w = ROW_COVER_W,
  wrapperClass = "h-9 w-9 rounded bg-raise overflow-hidden shrink-0",
}: {
  albumPath: string;
  trackCover?: string | null;
  albumCover?: string | null;
  albumFallback?: boolean;
  /** See CoverImg — the wizard's staged album. */
  staged?: boolean;
  /** Row art is drawn at 32-36 px, so the default is a 160 px thumbnail: it is
   *  the width the player BAR asks for too, which is what makes a row's cover
   *  and the bar's cover one request rather than two. */
  w?: number;
  wrapperClass?: string;
}) {
  const file = trackCover ?? (albumFallback ? albumCover ?? undefined : undefined);
  if (!trackCover && !albumFallback) {
    return <div className={wrapperClass} aria-hidden style={{ visibility: "hidden" }} />;
  }
  return <CoverImg albumPath={albumPath} coverFile={file} staged={staged} w={w} wrapperClass={wrapperClass} />;
}