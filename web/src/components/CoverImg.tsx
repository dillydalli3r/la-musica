import { useCallback, useEffect, useRef, useState } from "react";
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

/** How long to wait before asking for a cover again after its request failed,
 *  per failed attempt. Three retries over ~4.6 s, then the surface's own
 *  placeholder is the answer.
 *
 *  A cover request can fail for reasons that clear on their own — the file is
 *  being replaced at that instant (a cover write, script 5's re-encode), an
 *  import has not written it yet, a scanner or anti-malware pass holds it — and
 *  an <img> whose `src` stays the same never asks again. That is what made a
 *  transient miss PERMANENT for the track on screen: the player's art slot kept
 *  the disc glyph for the whole track however well the album's cover sat on
 *  disk. The delays grow so a genuinely absent cover costs three requests, not
 *  a loop, while a lock that clears in a moment is caught by the first one. */
const COVER_RETRY_DELAYS = [400, 1200, 3000];

/** One cover URL, re-asked on failure a bounded number of times.
 *
 *  Returns the `src` to draw (the URL itself, then the same URL with a retry
 *  counter the server ignores — a NEW url is the only way an <img> asks
 *  again, both for the element and for any cache that answered the failure),
 *  the `onError` to hand it, and `failed` once the last retry has been waited
 *  out and the surface should show its placeholder. The counter is remembered
 *  WITH the url it belongs to, so a row recycled onto another album starts at
 *  a first attempt instead of inheriting the previous cover's failures. */
export function useCoverRetry(url: string | null): {
  src: string | null;
  onError: () => void;
  failed: boolean;
} {
  const [cover, setCover] = useState({ url: null as string | null, failed: 0, released: 0 });
  const cur = cover.url === url ? cover : { url, failed: 0, released: 0 };

  // One timer per failure: the retry goes out after its delay, never in the
  // same tick the first request failed in.
  useEffect(() => {
    if (cur.failed <= cur.released) return;
    const delay = COVER_RETRY_DELAYS[cur.released];
    if (delay === undefined) return;
    const t = setTimeout(
      () => setCover((s) => (s.url === url ? { ...s, released: s.released + 1 } : s)),
      delay
    );
    return () => clearTimeout(t);
  }, [cur.failed, cur.released, url]);

  const onError = useCallback(() => {
    setCover((s) => {
      const b = s.url === url ? s : { url, failed: 0, released: 0 };
      return { ...b, failed: b.failed + 1 };
    });
  }, [url]);

  const spent = cur.failed > COVER_RETRY_DELAYS.length;
  const src = url && cur.released > 0 ? `${url}&r=${cur.released}` : url;
  return { src: spent ? null : src, onError, failed: spent };
}

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
  token,
  staged = false,
  w,
  wrapperClass = "h-9 w-9 rounded bg-raise overflow-hidden shrink-0",
}: {
  albumPath: string;
  coverFile?: string | null;
  /** The cover file's SERVER version token (`cover_token` on the album and
   *  track payloads — mtime + size). It is what makes a replaced cover a new
   *  URL for the browser's cache and the offline blob even when the write was
   *  the SERVER's own (the import's cover step, script 5's re-encode), which
   *  leaves no response to learn a token from — the reason a track row could
   *  keep drawing the previous cover after an import had replaced it. */
  token?: string | null;
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
  // The art's address, and the one to DRAW: a failed request is asked again a
  // bounded few times before the disc glyph is the answer (see
  // `useCoverRetry` — an <img> whose src never changes never asks again, which
  // is what used to make a transient miss permanent for the track on screen).
  // A write that changed this cover's bytes gives it a new URL (see
  // lib/invalidate): a different src is what re-fetches it, and the retry
  // state is keyed on the URL, so it clears a remembered failure too — a cover
  // that was missing and then uploaded loads without a reload.
  const networkUrl = coverFile ? api.coverUrl(albumPath, coverFile, { staged, w, token }) : null;
  const cover = useCoverRetry(networkUrl);
  const [offlineUrl, setOfflineUrl] = useState<string | null>(null);
  // Answered from the cached-track snapshot (lib/mediaCache), never by opening
  // Cache Storage here: it is the one question the probe below is gated on.
  const artworkDownloaded = useCachedArtwork(albumPath);
  const imgRef = useRef<HTMLImageElement | null>(null);

  /** Start the cover WITH its row, not one layout pass later.
   *
   * The `<img>` below keeps `loading="lazy"` so a long, un-virtualised list
   * (the Library page mounts every filtered row) does not begin by fetching
   * every cover behind the fold — but lazy means the browser only requests an
   * image after it has LAID OUT the page and then decided, on a later frame,
   * that the element is near the viewport. Measured on a 23-album scratch
   * library: `/api/library` ends at ~200 ms and the first `?w=160` request
   * starts at ~295 ms, with the row's own text already painted — 95 ms of
   * blank art the user watches.
   *
   * A bare `Image()` is not subject to that gate: setting its `src` kicks the
   * fetch at commit time, and Chromium joins the `<img>`'s later request to
   * the same in-flight one (same URL, same memory-cache entry), so this costs
   * no second transfer. The rect test keeps the eager kick for the covers a
   * reader can actually see — a row far below the fold stays lazy — which is
   * exactly the wholesale `loading="eager"` de-optimisation this avoids.
   * Sized covers only (`w` set): those are the small bucketed thumbnails a row,
   * card or pane draws, where 95 ms of latency is the whole cost; an
   * unsized `w` fetches the multi-megabyte master and keeps default priority.
   */
  useEffect(() => {
    if (!networkUrl || offlineUrl) return;
    const el = imgRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const vh = window.innerHeight || document.documentElement.clientHeight || 0;
    if (r.bottom < -vh * 0.5 || r.top > vh * 1.5) return;   // not near the fold
    const pre = new Image();
    // Same hint as the `<img>`; the property may be missing in older DOM
    // typings, and the attribute is what the browser reads either way.
    if (w) pre.setAttribute("fetchpriority", "high");
    pre.decoding = "async";
    pre.src = networkUrl;
  }, [networkUrl, offlineUrl, w]);

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
  if (!networkUrl || (cover.failed && !offlineUrl)) {
    return (
      <div className={`${wrapperClass} flex items-center justify-center text-zinc-700`}>
        <Disc3 className="h-1/2 w-1/2 max-h-5 max-w-5" />
      </div>
    );
  }
  return (
    <div className={wrapperClass}>
      {/* A sized request is a small bucketed thumb (~4 KB at w=160, see
          `artcache.THUMB_SIZES`) and belongs to the page's first paint, so it
          must not sit in the browser's default queue behind everything else;
          an unsized `w` pulls the multi-megabyte master and keeps default. */}
      <img
        ref={imgRef}
        src={offlineUrl ?? cover.src ?? undefined}
        alt=""
        loading="lazy"
        decoding="async"
        fetchPriority={w ? "high" : undefined}
        onError={cover.onError}
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
  trackToken,
  albumCover,
  albumToken,
  albumFallback = true,
  staged = false,
  w = ROW_COVER_W,
  wrapperClass = "h-9 w-9 rounded bg-raise overflow-hidden shrink-0",
}: {
  albumPath: string;
  trackCover?: string | null;
  /** The token of `trackCover`'s own file (`track.cover_token`). */
  trackToken?: string | null;
  albumCover?: string | null;
  /** The token of the ALBUM's cover file (`album.cover_token`) — the one a row
   *  falls back to when it has no sidecar of its own. */
  albumToken?: string | null;
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
  const token = trackCover ? trackToken : albumToken;
  if (!trackCover && !albumFallback) {
    return <div className={wrapperClass} aria-hidden style={{ visibility: "hidden" }} />;
  }
  return (
    <CoverImg albumPath={albumPath} coverFile={file} token={token} staged={staged} w={w} wrapperClass={wrapperClass} />
  );
}