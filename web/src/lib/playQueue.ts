/** Bounded play queues.
 *
 *  Pressing play in a 20 000-track library used to build a queue object for
 *  every track, hand it to the store and persist it — three full copies of the
 *  library per press, which is what made clicking a track in the Library
 *  visibly lag on a large install. A listener only ever needs the row they
 *  pressed, what they came from and a runway ahead; the player extends the
 *  queue itself as it nears the end (`lib/recommend.ts`'s runway).
 *
 *  One helper, so every surface windows the same way and a queue never depends
 *  on which page started it. */
export const QUEUE_BACK = 40;
export const QUEUE_AHEAD = 260;

/** The window of `rows` a play press should queue, and the pressed row's index
 *  inside it. Rows outside the window are reachable by playing again or by the
 *  queue's own extension — the alternative was allocating the whole library. */
export function playWindow<T>(rows: T[], index: number): { rows: T[]; index: number } {
  const total = rows.length;
  const at = Math.max(0, Math.min(index, Math.max(0, total - 1)));
  if (total <= QUEUE_BACK + QUEUE_AHEAD + 1) return { rows, index: at };
  const start = Math.max(0, at - QUEUE_BACK);
  const end = Math.min(total, start + QUEUE_BACK + QUEUE_AHEAD + 1);
  return { rows: rows.slice(start, end), index: at - start };
}