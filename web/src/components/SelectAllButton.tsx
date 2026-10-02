import { CheckCheck } from "lucide-react";

/** Ticking EVERYTHING the list in front of you shows — one control, in the
 *  four places that offer select mode (the Library, Home, the artist page, the
 *  trash).
 *
 *  The count is in the label, and that is the point: "all" is what the filters
 *  left, never the whole library, so the batch bar beside it acts on exactly
 *  what this ticks. The Library's three tables carry their own header checkbox
 *  for the same set; this is that answer for the views that draw cards
 *  (the grid, the compact list, the trash's grid), and for a header row on a
 *  phone, where a column-heading checkbox is off screen.
 *
 *  It reflects state: with everything already ticked it becomes the way back
 *  out, so a stray "Select all" on 4,000 tracks is one click to undo.
 */
export default function SelectAllButton({
  count,
  noun,
  all,
  onSelectAll,
  onClear,
}: {
  /** How many the list currently shows — the number this button ticks. */
  count: number;
  /** What gets ticked, plural and lower case ("albums", "entries"). */
  noun: string;
  /** Is every one of them already ticked? */
  all: boolean;
  onSelectAll: () => void;
  onClear: () => void;
}) {
  // Nothing listed, nothing to tick: a "Select all 0" is a dead label.
  if (count === 0) return null;
  return (
    <button
      type="button"
      className="btn-ghost !py-1.5 text-xs tap"
      onClick={all ? onClear : onSelectAll}
      title={
        all
          ? `Untick every ${noun} the list shows`
          : `Tick every ${noun} the list shows — the search, the letter rail and this view's own filter are what decide how many that is`
      }
    >
      <CheckCheck className="h-3.5 w-3.5" />
      {all ? `Deselect all ${count} ${noun}` : `Select all ${count} ${noun}`}
    </button>
  );
}
