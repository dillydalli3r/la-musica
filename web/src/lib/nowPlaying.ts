import { useEffect, useState } from "react";

/** How long the block waits for a piece that is still resolving, in ms — see
 *  `useNowPlayingMeta`. One round trip on the owner's own LAN is ~20 ms, so the
 *  wait only ever bites on a source that STALLS (a tags read that is retrying,
 *  a cover that never comes back); past it the block paints with what it has,
 *  exactly as it did before this record existed. */
export const NOW_PLAYING_WAIT_MS = 1500;

/** Is `url`'s image decoded — or already known to be undrawable?
 *
 *  Decoded, not merely requested: the metadata block must not paint its words
 *  a round trip before the picture that belongs with them, and the browser's
 *  own image cache is keyed by URL, so the `Image` fetched here hands the
 *  surface's `<img>`/`CoverImg` the same bytes with no second request. An
 *  `error` is an answer too: the disc placeholder is then the FINAL state, not
 *  a piece still to come. `false` is only ever "still working on it". */
function useArtReady(url: string | null): boolean {
  const [state, setState] = useState<{ url: string | null; done: boolean }>({ url, done: url === null });
  useEffect(() => {
    if (!url) {
      setState({ url: null, done: true });
      return;
    }
    let live = true;
    setState({ url, done: false });
    const img = new Image();
    img.decoding = "async";
    const done = () => {
      if (live) setState({ url, done: true });
    };
    img.onload = () => {
      // `decode()` is the promise the browser resolves once the bytes are
      // READY to paint; where it is missing or refuses the format, the load
      // event is the best answer there is.
      Promise.resolve(img.decode?.()).then(done, done);
    };
    img.onerror = done;
    img.src = url;
    const cap = window.setTimeout(done, NOW_PLAYING_WAIT_MS);
    return () => {
      live = false;
      window.clearTimeout(cap);
      img.onload = null;
      img.onerror = null;
    };
  }, [url]);
  return state.url === url && state.done;
}

/** The now-playing metadata block's own clock: ONE record per track, committed
 *  in ONE paint.
 *
 *  The title, the artist · album line, the marks that ride them and the art
 *  used to be painted from four different clocks, each landing on its own
 *  frame: the queue row's own strings (nothing to wait for), the per-track tags
 *  payload (the title/artist/album fallbacks and the release year that REWRITES
 *  the album line after it has already been read), the payload that NAMES the
 *  cover files (the library tree in the bar, the album payload in the
 *  fullscreen player — a queue row built from an album card carries no cover
 *  filenames at all, and the fullscreen player's `CoverImg` adds a Cache
 *  Storage hop on top), and the image's own bytes last. A queue row that had no
 *  metadata yet painted the filename stem, the folder name and a "—" first and
 *  then REPLACED them, which is the same stagger seen from the other end.
 *
 *  So the surfaces hand this hook the record as it resolves right now and keep
 *  painting the record they LAST committed until the new one is ready — the
 *  stale-hold the lyrics pane and the bar's tech readout already use for
 *  exactly this reason. A record is ready when
 *
 *    - `settled` — the per-track tags have answered (or failed), so every
 *      string they feed is final: the title, the artist, the album and the
 *      year that rides the album line;
 *    - the cover's ADDRESS is known — `cover` is `undefined` while only a
 *      payload that is still in flight can name the art, `null` when the track
 *      has none, the URL otherwise;
 *    - and, when there is art, its bytes are decoded (`useArtReady`).
 *
 *  `record === null` is the idle player: nothing is held and null comes back,
 *  so the surface draws its own idle state. Once a record is committed, the
 *  LIVE record is returned (not the snapshot taken at the commit), so a tag
 *  edit, a replaced cover or a late payload still lands on the block the way it
 *  always did — the gate is about track CHANGES only. */
export function useNowPlayingMeta<T extends { path: string }>(
  record: T | null,
  settled: boolean,
  cover: string | null | undefined
): T | null {
  const artReady = useArtReady(record && cover ? cover : null);
  const complete = settled && cover !== undefined && artReady;
  // The track the block is painting. A record is only committed when its own
  // path is complete, so `held` doubles as "what is on screen right now".
  const [held, setHeld] = useState<T | null>(null);
  // The give-up timer: a source that stalls must not leave the previous
  // track's block up for the length of the retries. Keyed on the path (the
  // record object is rebuilt every render), and only armed while a track
  // change really is waiting on it.
  const [stalled, setStalled] = useState<string | null>(null);
  const pendingPath = record && held?.path !== record.path ? record.path : null;
  useEffect(() => {
    if (!pendingPath || complete) return;
    const t = window.setTimeout(() => setStalled(pendingPath), NOW_PLAYING_WAIT_MS);
    return () => window.clearTimeout(t);
  }, [pendingPath, complete]);
  // Render-phase, never an effect: a track change whose pieces are all cached
  // must paint in THAT commit — an effect would paint the outgoing block one
  // more time first, which is the extra frame this exists to remove.
  if (!record) {
    if (held !== null) setHeld(null);
    return null;
  }
  if (held?.path !== record.path && (complete || stalled === record.path)) setHeld(record);
  return held && held.path === record.path ? record : held;
}
