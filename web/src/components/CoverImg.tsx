import { useEffect, useState } from "react";
import { Disc3 } from "lucide-react";
import { api } from "../api";
import { offlineArtworkUrl } from "../lib/mediaCache";

/** Cover thumbnail with a graceful fallback when the art is missing or
 * fails to load. `wrapperClass` sizes the box; the image fills it.
 *
 * When the image's bytes are in the offline cache (the album was downloaded),
 * the network URL paints first and the cached copy swaps in behind it — the
 * first paint MUST NOT wait on Cache Storage, so the online path looks and
 * times exactly as it does without the cache. Offline, that swap is what
 * keeps a downloaded album's art from collapsing to the Disc3 placeholder. */
export default function CoverImg({
  albumPath,
  coverFile,
  staged = false,
  wrapperClass = "h-9 w-9 rounded bg-raise border border-border overflow-hidden shrink-0",
}: {
  albumPath: string;
  coverFile?: string | null;
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
  const networkUrl = coverFile ? api.coverUrl(albumPath, coverFile, { staged }) : null;
  const [offlineUrl, setOfflineUrl] = useState<string | null>(null);

  useEffect(() => {
    if (!networkUrl) return;
    let live = true;
    setOfflineUrl(null); // a different cover must not inherit the old blob
    offlineArtworkUrl(networkUrl).then((u) => {
      if (live) setOfflineUrl(u);
    });
    return () => {
      live = false;
    };
  }, [networkUrl]);

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
  wrapperClass = "h-9 w-9 rounded bg-raise border border-border overflow-hidden shrink-0",
}: {
  albumPath: string;
  trackCover?: string | null;
  albumCover?: string | null;
  albumFallback?: boolean;
  /** See CoverImg — the wizard's staged album. */
  staged?: boolean;
  wrapperClass?: string;
}) {
  const file = trackCover ?? (albumFallback ? albumCover ?? undefined : undefined);
  if (!trackCover && !albumFallback) {
    return <div className={wrapperClass} aria-hidden style={{ visibility: "hidden" }} />;
  }
  return <CoverImg albumPath={albumPath} coverFile={file} staged={staged} wrapperClass={wrapperClass} />;
}