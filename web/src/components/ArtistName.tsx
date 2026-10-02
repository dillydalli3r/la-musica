import type { MouseEvent } from "react";
import { Link } from "react-router-dom";
import { DisambiguationMark } from "./Badges";

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
 *  A PASS draws the dot; a failure (or a verdict the payload does not carry —
 *  an artist that was never graded) draws nothing. The dot is the whole
 *  statement, spelled out only in its tooltip: two chips saying "albums" and
 *  "artist artwork 2/2" beside the name said the same thing in far more room.
 *  The counts beside it ("1 album · 17 tracks") are unaffected. */
export function ArtistDot({ pass }: { pass?: boolean | null }) {
  if (pass !== true) return null;
  // The same shape, green and tooltip the library's own verdict dot uses
  // (GradeWarning's `grade-dot`): one green dot means "this passed its
  // checks" wherever it is drawn, and a failure draws nothing at all here —
  // the artist page lists what failed, in words, right below the name.
  // `role="img"` rather than the library dot's `role="status"`: this one is
  // drawn once per row, and a page of live regions is a page a screen reader
  // cannot read.
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

export default function ArtistName({
  name,
  pass,
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
   *  `top_artists[].grade.pass` on Home). Anything but `true` draws no dot. */
  pass?: boolean | null;
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
      <ArtistDot pass={pass} />
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
