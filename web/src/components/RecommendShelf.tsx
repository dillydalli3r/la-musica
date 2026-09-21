import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

/** The ONE chrome every recommendation shelf wears: the local scorer's shelf,
 *  the online providers' shelf and the Recommended page's list are the same
 *  object with different sources, so the heading, the hint line and the
 *  spacing live here — a shelf cannot drift from the one beside it.
 *
 *  `meta` is the slot for what is true of THIS answer (how many rows, what the
 *  basis was); it rides the title line rather than opening a box, because the
 *  rows are what the reader came for. */
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
