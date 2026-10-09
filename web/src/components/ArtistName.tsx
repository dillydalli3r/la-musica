import type { MouseEvent } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle } from "lucide-react";
import { DisambiguationMark } from "./Badges";
import type { ArtistGradeIssue } from "../types";

/** An artist's name, with the artist's OWN verdict beside it.
 *
 *  The artist's own grade (`mlo.grader.grade_artist` — the artist image and
 *  the description stored in the artist folder, never the album checks) is one
 *  bit a reader needs at a glance, and it is the SAME bit on the artist page
 *  and in every list that names an artist: the page's identity block, the
 *  Library's Artists view, Home's artist shelf and Favorites' artist table all
 *  draw the identical dot from the identical payload field, so a list and the
 *  page it links to can never disagree about an artist's health.
 *
 *  A PASS draws the green dot. A FAILURE draws the amber warning — the
 *  library's own failing idiom (components/GradeWarning) — because "the artist
 *  image and the description are missing" is exactly the kind of claim the
 *  library has to make where the artist is listed: a list that drew nothing at
 *  all for a failing artist made an artist whose artwork never arrived look
 *  like one nobody had graded, and the only place that said otherwise was the
 *  artist's own page (the owner's ask: "make sure the library displays some
 *  sort of warning / grade error … if artist images don't exist and … artist /
 *  album descriptions don't exist"). Its tooltip names the failing checks the
 *  way the artist page's chips do (`label — reason`), so the row says WHAT is
 *  missing and the page it links to is where it gets fixed. A verdict the
 *  payload does not carry (an artist that was never graded) still draws
 *  nothing: "not looked at" is not "failed". The counts beside it ("1 album ·
 *  17 tracks") are unaffected. */
export function ArtistDot({ pass, issues }: { pass?: boolean | null; issues?: ArtistGradeIssue[] | null }) {
  // The same shape, green and tooltip the library's own verdict dot uses
  // (GradeWarning's `grade-dot`): one green dot means "this passed its
  // checks" wherever it is drawn.
  // `role="img"` rather than the library dot's `role="status"`: this one is
  // drawn once per row, and a page of live regions is a page a screen reader
  // cannot read.
  if (pass === true) {
    const label = "Artist checks pass — the artist folder holds its image and a description";
    return (
      <span
        /* A test hook as well as a shape: the dot has no text to find it by. */
        className="artist-dot h-2 w-2 shrink-0 rounded-full bg-emerald-400"
        role="img"
        aria-label={label}
        title={label}
      />
    );
  }
  if (pass !== false) return null;
  const said = (issues ?? []).map((i) => [i.label, i.reason].filter(Boolean).join(" — "));
  const label = said.length
    ? `Artist checks failed — ${said.join("; ")}. Open the artist page to fetch what is missing.`
    : "Artist checks failed — open the artist page to see what is missing";
  return (
    <span
      /* A test hook as well as a shape (`.artist-warn`, the same way the pass
         side is pinned by `.artist-dot`): the mark has no text of its own. */
      className="artist-warn inline-flex shrink-0 text-amber-400"
      role="img"
      aria-label={label}
      title={label}
    >
      <AlertTriangle className="h-3.5 w-3.5" />
    </span>
  );
}

export default function ArtistName({
  name,
  pass,
  issues,
  disambiguation,
  to,
  onClick,
  className = "",
  nameClassName = "",
}: {
  /** The label to draw: the payload's `display_name` — never a folder basename. */
  name: string;
  /** The artist's own grade verdict, straight off the payload the caller
   *  holds (`grade.pass` on the artist page and on the Library's rows,
   *  `top_artists[].grade.pass` on Home). `true` draws the dot, `false` the
   *  warning mark, and anything else (absent — never graded) draws nothing. */
  pass?: boolean | null;
  /** The artist's failing checks (`grade.issues`, same payload field), read by
   *  the warning mark's tooltip: what is missing, in the app's own words. */
  issues?: ArtistGradeIssue[] | null;
  /** MusicBrainz's disambiguation comment for this artist ("UK rock band"),
   *  drawn in parentheses right after the name in a dimmer tone — the same
   *  `DisambiguationMark` every other surface wears. Absent or null draws
   *  nothing; the locale alias is already part of `name`. */
  disambiguation?: string | null;
  /** Wraps the name in its own link when set; absent, the caller's own link or
   *  row click owns the navigation. */
  to?: string;
  onClick?: (e: MouseEvent<HTMLAnchorElement>) => void;
  className?: string;
  /** Extra classes for the NAME text itself (the caller decides whether a
   *  long name truncates, breaks, or wraps: `truncate` in a fixed-width cell,
   *  `break-words` where the row may grow). */
  nameClassName?: string;
}) {
  const inner = (
    <>
      <ArtistDot pass={pass} issues={issues} />
      {/* `min-w-0` so a name too long for the row can be clipped by the
          caller's own class (`truncate` in a fixed-width cell) while the dot
          keeps its own width. The name always names itself on hover: the
          payload's own display name, with no folder `[mbid]` in it. */}
      <span className={`min-w-0 ${nameClassName}`} title={name}>
        {name}
      </span>
      {/* Sibling of the (possibly truncating) name span, not a child: the
          comment keeps its own width and can never be clipped away by a long
          name. */}
      <DisambiguationMark value={disambiguation} />
    </>
  );
  if (to) {
    return (
      <Link to={to} onClick={onClick} className={`inline-flex items-center gap-1.5 min-w-0 ${className}`}>
        {inner}
      </Link>
    );
  }
  return <span className={`inline-flex items-center gap-1.5 min-w-0 ${className}`}>{inner}</span>;
}
