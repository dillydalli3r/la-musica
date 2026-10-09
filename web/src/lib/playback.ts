/** Playback speed ladder + helpers shared by the player bar and the
 * fullscreen player so both surfaces always show the same rate. */
export const SPEEDS = [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2];
export const MIN_SPEED = SPEEDS[0];
export const MAX_SPEED = SPEEDS[SPEEDS.length - 1];

/** Next rung on the ladder in `dir`, wrapping; unknown rates snap to 1×. */
export function nextSpeed(s: number, dir: 1 | -1 = 1): number {
  const i = SPEEDS.indexOf(s);
  if (i === -1) return 1;
  return SPEEDS[(i + dir + SPEEDS.length) % SPEEDS.length];
}

/** Compact label: "1×", "1.25×" — no trailing zeros. */
export const fmtSpeed = (s: number) => (s === 1 ? "1×" : `${Number(s.toFixed(2))}×`);
