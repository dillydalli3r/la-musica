import type { ReactNode } from "react";

/** The title cell of a track row: the name and its own marks on the left, the
 *  per-track actions in a FIXED trailing slot on the right.
 *
 *  Why the split. The marks a row carries are not the same from track to track
 *  — one has an EXPLICIT badge, one a clean badge, one a heart, one a cached
 *  marker — and when they all sat in one left-to-right run, every row's marks
 *  landed at a different x: they drifted with the title's length. The fix is
 *  the shape of the cell, not a per-row tweak: `children` is the flexible part
 *  (it absorbs whatever width the column has, so the name and its marks stay
 *  together and the EXPLICIT/CLEAN badge sits directly beside the title), and
 *  `trailing` is the fixed part, whose contents are all constant-width — the
 *  heart keeps its space whether or not the track is liked (`FavHeart`'s
 *  `revealOnHover` is opacity, never `display`), the actions menu is always
 *  drawn — so those controls end at the same x on every row.
 *
 *  A caller that draws the rating passes it LAST in `trailing`, because it is
 *  the one thing a reader scans down a column and the cell's edge is what makes
 *  that scan straight (the album tracklist does). The Library's Tracks view
 *  gives the rating a COLUMN of its own instead and passes none: one cell
 *  holding the name, the marks and the stars at once is how that table ended up
 *  with a 0 px title.
 *
 *  Devices: nothing here wraps or hides by breakpoint EXCEPT `stackOnPhone`,
 *  which the album tracklist asks for because its own cell is narrower than the
 *  trailing slot there — see the prop. Elsewhere the flexible part shrinks and
 *  WRAPS its marks onto a second line (the app's table rules prefer wrapping
 *  over clipping), the trailing slot keeps its width, and a phone therefore
 *  gets the same aligned rows as a desktop — narrower, not rearranged. */
export default function TrackTitleCell({
  children,
  trailing,
  className = "",
  stackOnPhone = false,
}: {
  children: ReactNode;
  /** Constant-width marks only: a variable-width mark belongs in `children`,
   *  or the slot — and the rating with it — shifts from row to row. */
  trailing: ReactNode;
  /** For a row that is itself a flex container (the compact list): `flex-1`
   *  makes the cell take the space the row has left, which is what puts the
   *  trailing slot at the row's edge instead of at the title's. */
  className?: string;
  /** Below `md`, fold the trailing slot UNDER the name instead of beside it.
   *
   *  The album tracklist is the one caller that needs this, and the numbers
   *  are why: at 390 px its cell measured 122 px while the slot measured
   *  ~138 px (heart, "…", five stars) — the fixed part won, the name was
   *  handed 0 px, and the album page rendered one syllable per line. The slot
   *  still may not shrink (its constant width is what this cell exists for),
   *  so what gives is the LINE: the name gets the cell's full width first, the
   *  slot keeps its own row — and its own x on every row, which is the
   *  alignment a stack of rows is read by. `md:flex-1` puts the two back side
   *  by side, so nothing above `md` changes. */
  stackOnPhone?: boolean;
}) {
  return (
    <div className={`flex items-center gap-1.5 min-w-0${stackOnPhone ? " max-md:flex-wrap" : ""} ${className}`}>
      {/* `flex-wrap` is what keeps a narrow cell readable. A table column
          cannot grow (the layout is fixed — see index.css), so a row has a
          fixed width to spend, and before this the trailing slot's
          constant-width marks simply won: the title link (`flex-1 min-w-0`)
          was squeezed to 0 px and wrapped one character per line, which is how
          the Library's Tracks view came to render as empty rows 200 px tall.
          Wrapping gives the title its own line — the marks move UNDER it when
          a cell is too narrow to hold both — while the trailing slot keeps its
          own x on every row with the width for it (the alignment this cell
          exists for). */}
      <div className={`flex items-center gap-1.5 min-w-0 flex-wrap${stackOnPhone ? " max-md:grow max-md:basis-full md:flex-1" : " flex-1"}`}>{children}</div>
      {/* gap-1.5, the same as the title side: at gap-0.5 the actions menu sat
          2 px from the star rating, so the `…` and the stars ran together as
          one cluster while every other pair in the row was 6 px apart — the
          spacing the eye reads as "these are separate controls" (#36).
          `max-md:flex-wrap` with `stackOnPhone`: below `md` the whole slot is
          narrower than the album cell (132 px of controls against 98 px of
          content box), so the controls break into their own rows there instead
          of painting over the length column beside them — which is why the
          slot is allowed to shrink to the cell below `md` and keeps its fixed
          width from `md` up, where it has the room. */}
      <div className={`flex items-center gap-1.5${stackOnPhone ? " max-md:min-w-0 max-md:shrink max-md:flex-wrap md:shrink-0" : " shrink-0"}`}>{trailing}</div>
    </div>
  );
}
