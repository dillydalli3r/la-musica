import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2 } from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { albumRef, trackRef } from "../lib/refs";
import type { GradeWarning as GradeSummary, GradeWarningItem } from "../types";

/** The grader's BARE codes (mlo.grader's per-track `issues`) read as words.
 *  A code that is NOT listed still gets shown — its underscores opened up —
 *  because the alternative is a code quietly missing from the one surface
 *  whose whole job is to say what failed. */
const CODE_WORDS: Record<string, string> = {
  GENRE_MISSING: "genre missing",
  MOOD_MISSING: "mood missing",
  ENERGY_MISSING: "energy missing",
  COVER: "cover",
  CRC_MISMATCH: "CRC mismatch",
  LOG_CHECKSUM: "log checksum",
  AUDIT: "audit",
  CD_FORMAT: "CD format",
  UNREADABLE: "unreadable file",
  TAGS: "tags",
};

/** Where "+N more" lands: the Library page's own Failing quick filter, which
 *  reads this off the URL on arrival (see LibraryPage). */
const MORE_HREF = "/library?filter=failing";

/** One finding. What the row NAMES is what it links to: a single failing track
 *  opens the track, everything else opens the album — the album is what failed
 *  there, and its track list is where the failing files are. */
function Finding({ item }: { item: GradeWarningItem }) {
  const isTrack = item.kind === "track";
  return (
    <li className="flex flex-wrap items-baseline gap-x-1.5">
      <Link
        to={isTrack ? trackRef({ path: item.track_path ?? "" }) : albumRef({ path: item.album_path })}
        className="text-amber-100 hover:text-white hover:underline underline-offset-2"
      >
        {item.artist ? `${item.artist} — ` : ""}
        {item.album}
        {isTrack && item.title ? `: ${item.title}` : ""}
      </Link>
      {/* An album row says how many of its tracks fail — 0 when the album
          failed a check of its own (its cover, its .log) rather than a file's. */}
      {!isTrack && !!item.failing_tracks && (
        <span className="text-amber-300/80">
          {item.failing_tracks} track{item.failing_tracks === 1 ? "" : "s"}
        </span>
      )}
      {item.codes.length > 0 && (
        <span className="text-amber-300/80">
          {item.codes.map((c) => CODE_WORDS[c] ?? c.toLowerCase().replace(/_/g, " ")).join(", ")}
        </span>
      )}
      {item.reason && <span className="text-amber-300/80">— {item.reason}</span>}
    </li>
  );
}

/** Whether the library passes its grading checks, and — when it does not —
 *  what is wrong, specifically: one row per finding, each linking to the thing
 *  it names. Sits at the top of the Home page and of the Library page, which
 *  are the two pages that answer for the whole library.
 *
 *  `initial` is the object Home already holds (its own payload carries it, off
 *  the same builder as the route): with it the strip paints with the page
 *  instead of asking the server for an answer the page has just received. The
 *  Library page fetches no Home payload and leaves it out — the query below
 *  then asks `/api/grades/summary`, the one route either page reads this from,
 *  so the two never count the library differently.
 *
 *  `staleTime` matches Home's own payload cache: both describe a library whose
 *  grades only change when something runs, and re-asking per view would spend
 *  a library build to paint the same strip. */
export default function GradeWarning({ initial }: { initial?: GradeSummary }) {
  const { data } = useQuery({
    queryKey: ["gradesSummary"],
    queryFn: api.gradesSummary,
    staleTime: 5 * 60_000,
    initialData: initial,
  });
  // No answer yet (or none came back): a strip that guessed would be worse
  // than the page's own loading/error state saying it, and this banner is
  // decoration on top of a page that stands on its own.
  if (!data) return null;

  if (data.ok) {
    return (
      <div className="text-xs text-emerald-300 bg-emerald-950/30 border border-emerald-900/60 rounded-lg px-3 py-2 flex items-center gap-2">
        <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />
        {/* "All 0 checks pass" is not a sentence: an install with every check
            switched off has nothing to report, and says so. */}
        <span>
          {data.total_checks > 0
            ? `All ${data.total_checks} checks pass`
            : "No grading checks to report yet"}
        </span>
      </div>
    );
  }

  return (
    <div className="text-xs text-amber-200 bg-amber-950/30 border border-amber-900/60 rounded-lg px-3 py-2 space-y-1.5">
      <div className="flex items-start gap-2">
        <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
        <span>
          <span className="font-mono">{data.albums_failing}</span>{" "}
          {data.albums_failing === 1 ? "album fails" : "albums fail"} the library's grading
          checks
          {data.tracks_failing > 0 && (
            <>
              {" · "}
              <span className="font-mono">
                {data.tracks_failing} track{data.tracks_failing === 1 ? "" : "s"}
              </span>{" "}
              affected
            </>
          )}
          {data.grade_pct != null && (
            <>
              {" · "}
              <span className="font-mono">{data.grade_pct}%</span> of checks pass
            </>
          )}
        </span>
      </div>
      <ul className="space-y-1 pl-5 list-disc marker:text-amber-700">
        {data.items.map((item) => (
          <Finding key={`${item.kind}:${item.album_path}:${item.track_path ?? ""}`} item={item} />
        ))}
      </ul>
      {/* The rest are not printed: the Library's own Failing filter lists every
          failing album and track, which is what a reader wants past a dozen. */}
      {data.more > 0 && (
        <Link
          to={MORE_HREF}
          className="inline-block text-amber-300/90 underline underline-offset-2 hover:text-white"
        >
          +{data.more} more in the Library →
        </Link>
      )}
    </div>
  );
}
