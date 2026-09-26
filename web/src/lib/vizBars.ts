/** The frequency strip's bar grid, in DEVICE pixels.
 *
 * A canvas is a raster: a bar whose edges land between device pixels is
 * painted as a partially covered column, and a frame that is only *mostly*
 * erased (`clearRect` in CSS pixels stops at a fractional edge) leaves that
 * column's ink behind — the faint lines the owner photographed at the strip's
 * edges, which need a fractional layout width or a fractional
 * devicePixelRatio to show up at all, hence "sometimes".
 *
 * So the grid is computed on the device grid, and the strip is spanned
 * EXACTLY: the first bar starts at column 0 and the last one ends at `width`,
 * which is what keeps the outermost bars flush with the strip's edges instead
 * of a seam or an empty margin.
 *
 * The design's 28 %-of-a-slot gap gives way (down to 0) before the FIT does.
 * The old `Math.max(1.5, …)` floors could not: a strip narrower than 166 px
 * (56 bars × 1.5 px + 55 gaps × 1.5 px) drew its last bars PAST the right
 * edge, so the tail of the spectrum was silently clipped mid-bar.
 *
 * Pure and exported: the arithmetic is what a canvas cannot be asked about,
 * so tools/check_fullscreen_player.cjs asserts it directly. */
export interface VizBar {
  /** Left column, in device pixels. */
  x: number;
  /** Width, in device pixels — always at least one whole column. */
  w: number;
}

export function vizBars(width: number, count: number): VizBar[] {
  const W = Math.max(0, Math.round(width));
  if (!W) return [];
  // One whole column per bar is the floor: a strip with no room for `count`
  // even at zero gap carries as many bars as fit rather than running past the
  // edge, and its spectrum is resampled over the bars it does have.
  const n = Math.max(1, Math.min(count, Math.floor(W / 2) || 1));
  const slot = W / n;
  const gap = Math.max(0, Math.min(Math.round(slot * 0.28), Math.floor(slot) - 1));
  const base = Math.max(1, Math.floor((W - gap * (n - 1)) / n));
  // The columns the whole-pixel gap did not use are handed out one each, so
  // the grid ends exactly at W instead of a column short.
  const spare = W - (base * n + gap * (n - 1));
  const out: VizBar[] = [];
  let x = 0;
  for (let i = 0; i < n; i++) {
    const w = base + (i < spare ? 1 : 0);
    out.push({ x, w });
    x += w + gap;
  }
  return out;
}
