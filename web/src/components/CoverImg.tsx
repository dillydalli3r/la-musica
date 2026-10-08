import { useState } from "react";
import { Disc3 } from "lucide-react";
import { api } from "../api";

/** The thumbnail width a row's cover asks the server for (bucketed
 *  server-side, see `artcache.THUMB_SIZES`). Declared here because this file
 *  is where a cover is drawn: a row's art is 32-40 px, and two surfaces
 *  asking for the same width share ONE request. */
export const ROW_COVER_W = 160;

/** Cover thumbnail with a graceful fallback when the art is missing or
 *  fails to load. `wrapperClass` sizes the box; the image fills it. */
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
   *  large, and what every grid card that omits it still does. Two surfaces
   *  asking for the same width share one request. */
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

  if (!networkUrl || failedUrl === networkUrl) {
    return (
      <div className={`${wrapperClass} flex items-center justify-center text-zinc-700`}>
        <Disc3 className="h-1/2 w-1/2 max-h-5 max-w-5" />
      </div>
    );
  }
  return (
    <div className={wrapperClass}>
      <img
        src={networkUrl}
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
  /** Row art is drawn at 32-36 px, so the default is a 160 px thumbnail. */
  w?: number;
  wrapperClass?: string;
}) {
  const file = trackCover ?? (albumFallback ? albumCover ?? undefined : undefined);
  if (!trackCover && !albumFallback) {
    return <div className={wrapperClass} aria-hidden style={{ visibility: "hidden" }} />;
  }
  return <CoverImg albumPath={albumPath} coverFile={file} staged={staged} w={w} wrapperClass={wrapperClass} />;
}