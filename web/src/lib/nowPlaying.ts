import { useEffect, useState } from "react";

/** How long the block waits for a piece that is still resolving, in ms — see
 *  `useNowPlayingMeta`. One round trip on the owner's own LAN is ~20 ms, so the
 *  wait only ever bites on a source that STALLS (a tags read that is retrying,
 *  a cover that never comes back); past it the block paints with what it has,
 *  exactly as it did before this record existed. */
export const NOW_PLAYING_WAIT_MS = 1500;

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
 *  exactly this reason. A record is ready when `settled`: the per-track tags
 *  have answered (or failed), so every STRING the record feeds is final — the
 *  title, the artist, the album, the year that rides the album line, the tech
 *  readout and the marks.
 *
 *  The ART is deliberately NOT part of that, and this is the one rule that has
 *  changed: the block used to hold its words until the cover's address was
 *  known AND the image was decoded, so a slow cover held the title back with
 *  it. The owner's ask is the other way round — "all data shows at the same
 *  [time] … other info can load before the cover is updated" — so the words
 *  land as soon as they are known and the cover arrives after them. The
 *  queue's own data is warmed ahead (`lib/queueWarm`), so in the ordinary
 *  handover the art is already in the cache and the record commits complete
 *  anyway; what the change removes is the case where the picture is LATE.
 *
 *  `record === null` is the idle player: nothing is held and null comes back,
 *  so the surface draws its own idle state. Once a record is committed, the
 *  LIVE record is returned (not the snapshot taken at the commit), so a tag
 *  edit, a replaced cover or a late payload still lands on the block the way it
 *  always did — the gate is about track CHANGES only. */
export function useNowPlayingMeta<T extends { path: string }>(
  record: T | null,
  settled: boolean
): T | null {
  // `complete` is the gate: the strings are final. The art is the LIVE
  // record's own business — a payload that names the cover, or the image's
  // bytes, may land after this commit and update the block in place.
  const complete = settled;
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
