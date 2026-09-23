import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

/** The one page-header idiom: icon + title, optional overline/subtitle/chips,
 *  actions pinned to the right, and an optional extra row for the controls
 *  that belong with the title (search box, mode tabs, filters).
 *
 *  `sticky` pins the header below the floating 48px top bar. Sticky offsets
 *  resolve against the scrollport, NOT the padding box, so `top-12` (48px) is
 *  what actually clears the bar — `top-0` hides the header underneath it and
 *  the bar's search input swallows its clicks. */
export default function PageHeader({
  icon: Icon,
  title,
  subtitle,
  overline,
  chips,
  actions,
  back,
  sticky = false,
  children,
}: {
  icon?: LucideIcon;
  title: ReactNode;
  subtitle?: ReactNode;
  overline?: string;
  chips?: string[];
  actions?: ReactNode;
  /** Optional breadcrumb line above the title ("← MusicBrainz search"). */
  back?: { to: string; label: string };
  sticky?: boolean;
  children?: ReactNode;
}) {
  return (
    <div className={`${sticky ? "sticky top-12 z-20 -mt-2 pt-2 pb-3 bg-bg/95 backdrop-blur " : ""}space-y-3`}>
      {back && (
        <Link to={back.to} className="inline-block text-[11px] text-zinc-500 hover:text-zinc-300">
          ← {back.label}
        </Link>
      )}
      {/* One column below md — a wide actions set (Segmented + buttons) takes
          the full row and wraps there instead of squeezing the title to a
          sliver — and the original side-by-side row from md up.
          Up there the actions box GROWS INTO the leftover room instead of
          being held at its content width by `shrink-0`: at 834 px the Favorites
          actions row is 630 px (four tabs, two buttons) against a 594 px row,
          so a content-sized box that may not shrink took its 630 px out of the
          one sibling allowed to give any (the title column is `min-w-0`, for
          its own truncation) — the page title measured 0 px wide on
          /favorites/tracks and on /playlists, the title of the page with no
          room at all. Sized from the leftover, that same box wraps its own
          buttons onto another line and the title keeps the width its text
          needs. */}
      <div className="flex flex-col md:flex-row md:items-start md:justify-between gap-3 md:gap-4">
        <div className="min-w-0">
          {overline && <div className="text-[10px] uppercase tracking-widest text-zinc-500">{overline}</div>}
          <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2 min-w-0">
            {Icon && <Icon className="h-6 w-6 text-accent shrink-0" />}
            <span className="min-w-0 truncate" title={typeof title === "string" ? title : undefined}>
              {title}
            </span>
          </h1>
          {/* `break-words`: a subtitle carrying a file path (Soulseek's download
              dir, Dependencies' folder) has no space to wrap at, and an
              unbreakable run of text scrolls the whole page sideways. */}
          {subtitle && <div className="mt-1 text-xs text-zinc-500 break-words">{subtitle}</div>}
          {chips && chips.length > 0 && (
            <div className="flex flex-wrap gap-1.5 mt-2">
              {chips.map((c) => (
                <span key={c} className="chip bg-raise border border-border text-zinc-300">
                  {c}
                </span>
              ))}
            </div>
          )}
        </div>
        {actions && (
          <div className="flex items-center justify-end flex-wrap gap-1.5 md:flex-1 min-w-0">{actions}</div>
        )}
      </div>
      {children}
    </div>
  );
}
