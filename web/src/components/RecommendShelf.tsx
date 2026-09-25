import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

/** How many rows EVERY "Recommended" shelf is asked for — the local scorer's
 *  and the online providers' alike, on an album, artist, track, playlist or
 *  favourites page and on the Recommended page itself.
 *
 *  A page shows its two shelves side by side: same headings, same chrome, two
 *  lists of the same thing. The reader counts them, so ONE number keeps them
 *  honest together — a local track shelf carrying 20 rows beside an online
 *  shelf carrying 12 read as a fault in whichever shelf happened to be shorter
 *  (the owner's report). `server/recommend.py`'s `DEFAULT_LIMIT` is the same
 *  12, and the local shelf sends this number rather than leaning on that
 *  default, so the pair cannot drift apart silently.
 *
 *  A page may still pass its own `limit` — this is a default, not a ceiling. */
export const SHELF_LIMIT = 12;

/** The ONE chrome every recommendation shelf wears: the local scorer's shelf,
 *  the online providers' shelf and the Recommended page's list are the same
 *  object with different sources, so the heading, the hint line and the
 *  spacing live here — a shelf cannot drift from the one beside it.
 *
 *  `meta` is the slot for what is true of THIS answer (how many rows, what the
 *  basis was); it rides the title line rather than opening a box, because the
 *  rows are what the reader came for.
 *
 *  The CHILDREN's own layout is the caller's: an album shelf hands in cards and
 *  wraps them in the Library grid's own `repeat(auto-fill, minmax(…, 1fr))`
 *  (components/MoreLikeThis.tsx — the wide row that used to scroll sideways is
 *  what became a grid), while the online shelf hands in rows and keeps them a
 *  list. Both wear this chrome, and neither is a layout the other should have. */
export default function RecommendShelf({
  icon: Icon,
  title,
  hint,
  meta,
  children,
}: {
  icon: LucideIcon;
  /** "Recommended (Local)" / "Recommended (Online)" — the source is part of
   *  the name, so the two shelves are never read as one list. */
  title: string;
  /** Where these rows came from, in one line. */
  hint: ReactNode;
  /** Answer-specific facts, on the title line. */
  meta?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="section">
      <div className="flex items-center gap-1.5 mb-1 flex-wrap">
        <Icon className="h-3.5 w-3.5 text-zinc-500" />
        <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-500">{title}</h2>
        {meta}
      </div>
      <p className="text-[11px] text-zinc-600 mb-2">{hint}</p>
      {children}
    </section>
  );
}
