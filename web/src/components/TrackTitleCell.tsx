import type { ReactNode } from "react";

/** The title cell of a track row: the name and its own marks on the left, the
 *  per-track actions and the rating in a FIXED trailing slot on the right.
 *
 *  Why the split. The marks a row carries are not the same from track to track
 *  — one has an EXPLICIT badge, one a clean badge, one a heart, one a cached
 *  marker — and when they all sat in one left-to-right run, every row's stars
 *  landed at a different x: the rating drifted with the title's length and with
 *  whichever marks happened to precede it. The fix is the shape of the cell,
 *  not a per-row tweak: `children` is the flexible part (it absorbs whatever
 *  width the column has, so the name and its marks stay together and the
 *  EXPLICIT/CLEAN badge sits directly beside the title), and `trailing` is the
 *  fixed part, whose contents are all constant-width — the heart keeps its
 *  space whether or not the track is liked (`FavHeart`'s `revealOnHover` is
 *  opacity, never `display`), the actions menu is always drawn — so the rating
 *  ends at the same x on every row of the album.
 *
 *  The rating goes LAST in `trailing`: it is the one thing a reader scans down
 *  a column, and putting it at the cell's edge is what makes that scan
 *  straight.
 *
 *  Devices: nothing here wraps or hides by breakpoint. The flexible part
 *  shrinks (its text wraps, which the app's table rules prefer over clipping),
 *  the trailing slot keeps its width, and a phone therefore gets the same
 *  aligned rows as a desktop — narrower, not rearranged. */
export default function TrackTitleCell({
  children,
  trailing,
  className = "",
}: {
  children: ReactNode;
  /** Constant-width marks only: a variable-width mark belongs in `children`,
   *  or the slot — and the rating with it — shifts from row to row. */
  trailing: ReactNode;
  /** For a row that is itself a flex container (the compact list): `flex-1`
   *  makes the cell take the space the row has left, which is what puts the
   *  trailing slot at the row's edge instead of at the title's. */
  className?: string;
}) {
  return (
    <div className={`flex items-center gap-1.5 min-w-0 ${className}`}>
      <div className="flex items-center gap-1.5 min-w-0 flex-1">{children}</div>
      <div className="flex items-center gap-0.5 shrink-0">{trailing}</div>
    </div>
  );
}
