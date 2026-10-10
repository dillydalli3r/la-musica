import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { ArrowUpRight, type LucideIcon } from "lucide-react";

/** One typed row in the top-bar search dropdown: a direct in-app link to the
 *  entity, with its kind on the right. A MusicBrainz hit is a link into the
 *  app's own browser (`/mb/…`) exactly like a local hit is a link into the
 *  library — `external` adds musicbrainz.org as a small secondary affordance,
 *  never the primary click. */
export default function SearchHit({ to, icon: Icon, label, hint, onGo, external, marker }: {
  to: string;
  icon: LucideIcon;
  label: string;
  hint: string;
  onGo: () => void;
  /** musicbrainz.org URL of the same entity, for the escape-hatch icon */
  external?: string;
  /** A state mark the hit carries beside its name — a pending album's dot
   *  (`PendingMark`), so a search result reads as "not downloaded yet" the
   *  same way the library row it opens does. */
  marker?: ReactNode;
}) {
  const body = (
    <>
      <Icon className="h-4 w-4 text-accent-soft shrink-0" />
      <span className="flex-1 min-w-0 truncate">{label}</span>
      {marker}
      <span className="text-[10px] uppercase tracking-wider text-zinc-600 shrink-0">{hint}</span>
    </>
  );
  const cls = "w-full flex items-center gap-2.5 px-3 py-2 text-xs text-zinc-200 hover:bg-raise transition-colors";
  return (
    <div className="flex items-center">
      <Link to={to} onClick={onGo} className={cls}>
        {body}
      </Link>
      {external && (
        <a
          href={external}
          target="_blank"
          rel="noreferrer"
          onClick={onGo}
          title="Open on MusicBrainz"
          className="shrink-0 mr-1.5 p-1.5 rounded-lg text-zinc-600 hover:text-white hover:bg-raise transition-colors"
        >
          <ArrowUpRight className="h-3.5 w-3.5" />
        </a>
      )}
    </div>
  );
}