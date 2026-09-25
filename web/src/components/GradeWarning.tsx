import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, ChevronDown, ChevronUp } from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { albumRef, trackRef } from "../lib/refs";
import { useLocks } from "../lib/locks";
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
  FLAC_MD5: "FLAC MD5 mismatch",
  FLAC_MD5_ABSENT: "FLAC MD5 absent (unverified)",
  FLAC_MD5_UNKNOWN: "FLAC MD5 not verified",
  CD_FORMAT: "CD format",
  UNREADABLE: "unreadable file",
  TAGS: "tags",
  // The one code whose bare form says what is missing and not what to do about
  // it ("acoustid id"). A half pair is completed by a step this app already
  // has, named the way the grader's own messages name their actions ("run
  // Audit Library") so the words here are words the reader can find again —
  // the strip names the step that fixes THIS file's tag (script 21); what a
  // submission is for is on the Grading page's own check description.
  ACOUSTID_ID: "AcoustID id missing (run Fix AcoustID pairs)",
  ACOUSTID_FINGERPRINT: "AcoustID fingerprint missing (run Fix AcoustID pairs)",
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

/** How many findings a collapsed strip shows before it asks to be opened.
 *  Three fills about a phone screen without pushing the page's own content
 *  below the fold — the owner's report was a Library page whose grading strip
 *  took the top of the screen on seven failing albums. */
const COLLAPSED_ITEMS = 3;

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
 *  a library build to paint the same strip. What that leaves out is the run
 *  itself: the server drops the albums a live job holds from the findings
 *  (server.recommendations.grade_warning), so the answer changes when the
 *  client's own lock list does — see the effect below. */
/** The summary both presentations below read: one query, one lock-driven
 *  invalidation, one cache entry — so a dot and a strip on the same page can
 *  never disagree, and neither pays for a second walk of the library.
 *
 *  Extracted from the strip when the two pages started drawing the OK state as
 *  a dot in their own header instead of a full-width banner: the dot lives in
 *  the title, the warning stays where the strip was, and both need the same
 *  answer. */
export function useGradesSummary(initial?: GradeSummary): GradeSummary | undefined {
  const qc = useQueryClient();
  // The strip's answer depends on which albums a job holds RIGHT NOW, and the
  // 5-minute `staleTime` above was chosen for a library that only changes when
  // something runs — which is exactly when it is wrong: a run's first step
  // leaves an album half-written, so the strip would name the album being
  // worked on, and would keep naming it long after the run ended.
  //
  // The signal is the lock list the app already polls (lib/locks, behind the
  // "Script run" chip), reduced to ONE string so a poll that changes nothing
  // re-runs nothing: the effect below fires only when the held paths actually
  // change — a claim taken, followed (move) or given back — never per tick, and
  // invalidating the summary does not feed the lock list back, so it cannot
  // loop. It is coarser than the server's rule (only an album's own folder
  // matters there); a claim change is a step boundary in a run, not a tick.
  const busy = useLocks((s) =>
    s.index.entries.map((e) => e.held.key).sort().join("|")
  );
  const seen = useRef<string | null>(null);
  useEffect(() => {
    const first = seen.current === null;
    if (!first && seen.current === busy) return;
    seen.current = busy;
    // A first paint with nothing held already agrees with an idle library:
    // refetching there would undo `initialData`'s whole point for nothing.
    if (first && !busy) return;
    qc.invalidateQueries({ queryKey: ["gradesSummary"] });
  }, [busy, qc]);
  const { data } = useQuery({
    queryKey: ["gradesSummary"],
    queryFn: api.gradesSummary,
    staleTime: 5 * 60_000,
    initialData: initial,
  });
  return data;
}

/** The OK state as a dot for a page's own title — no sentence, no banner.
 *
 *  The owner's ask, verbatim: "no 'all checks pass' text in the library / home
 *  menus. It should only display a small green dot next to the library / home
 *  text. Thats all. If theres a warning it should be written out though." So
 *  the passing state is carried by colour and a tooltip, and everything else
 *  goes on being written out by <GradeWarning/> below the header.
 *
 *  `null` while the answer is unknown — a green dot nobody has verified would
 *  be the exact claim this component is not allowed to make. */
export function GradeDot({ initial }: { initial?: GradeSummary }) {
  const data = useGradesSummary(initial);
  if (!data?.ok) return null;
  const label = data.total_checks > 0
    ? "All checks pass"
    : "No grading checks to report yet";
  return (
    <span
      role="status"
      aria-label={label}
      title={label}
      data-testid="grade-dot"
      className="inline-block h-2 w-2 shrink-0 rounded-full bg-emerald-400 align-middle"
    />
  );
}

export default function GradeWarning({ initial, mode = "strip" }: {
  initial?: GradeSummary;
  /** `"strip"` draws both states (the green pill / the amber block).
   *  `"notice"` draws ONLY a problem: the passing state is the page title's
   *  dot (`GradeDot`) now, and a page that drew both would say "all checks
   *  pass" twice on a page whose owner asked for a dot and nothing else. */
  mode?: "strip" | "notice";
}) {
  const [open, setOpen] = useState(false);
  const data = useGradesSummary(initial);
  // No answer yet (or none came back): a strip that guessed would be worse
  // than the page's own loading/error state saying it, and this banner is
  // decoration on top of a page that stands on its own.
  if (!data) return null;

  if (data.ok) {
    if (mode === "notice") return null;
    return (
      <div className="text-xs text-emerald-300 bg-emerald-950/30 border border-emerald-900/60 rounded-lg px-3 py-2 flex items-center gap-2">
        <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />
        {/* "All 10281 checks pass" is a number the reader cannot use: the count
            is a fact about the checks, not about the library, and a library
            that passes says so in four words. The ZERO case still says
            something else on purpose — an install with every check switched
            off has nothing to report, and "All checks pass" would be a claim
            about a library nothing looked at. */}
        <span>
          {data.total_checks > 0 ? "All checks pass" : "No grading checks to report yet"}
        </span>
      </div>
    );
  }

  // A LONG list opens collapsed, and the summary line is ALWAYS the first
  // line: it is one sentence, and it is the answer. The toggle is a real
  // button (keyboard-reachable, with `aria-expanded`) rather than a text link,
  // because it controls a region of the page.
  const long = data.items.length > COLLAPSED_ITEMS;
  const shown = long && !open ? data.items.slice(0, COLLAPSED_ITEMS) : data.items;
  const hidden = data.items.length - shown.length;

  return (
    <div className="text-xs text-amber-200 bg-amber-950/30 border border-amber-900/60 rounded-lg px-3 py-2 space-y-1.5">
      <div className="flex items-start gap-2">
        <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
        <span>
          <span className="font-mono">{data.albums_failing}</span>{" "}
          {/* "falls short of" / "fall short of", not "falls/fail": the album is
              short of the grade, and "album falls the library's grading checks"
              has no complement — the verb needs the phrase to make a sentence.
              Both numbers read as sentences: one album falls short of them,
              two albums fall short of them. */}
          {data.albums_failing === 1 ? "album falls short of" : "albums fall short of"} the
          library's grading checks
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
        {shown.map((item) => (
          <Finding key={`${item.kind}:${item.album_path}:${item.track_path ?? ""}`} item={item} />
        ))}
      </ul>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 pl-5">
        {long && (
          <button
            type="button"
            className="tap inline-flex items-center gap-1 text-amber-100 hover:text-white underline underline-offset-2"
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
            title={open ? "Hide the rest of the findings" : "Show every failing album and track"}
          >
            {open ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
            {open ? "Show less" : `Read more — ${data.items.length} finding${data.items.length === 1 ? "" : "s"}`}
          </button>
        )}
        {/* The rest are not printed: the Library's own Failing filter lists every
            failing album and track, which is what a reader wants past a dozen.
            Both affordances carry `.tap`: as bare text links they were 16px
            tall, which is a coin toss with a thumb — the phone geometry check
            (tools/check_responsive.cjs) reports anything under 28px. */}
        {data.more > 0 && (
          <Link
            to={MORE_HREF}
            className="tap inline-flex items-center text-amber-300/90 underline underline-offset-2 hover:text-white"
          >
            +{data.more} more in the Library →
          </Link>
        )}
      </div>
      {hidden > 0 && !open && (
        <span className="sr-only">{hidden} more finding(s) hidden</span>
      )}
    </div>
  );
}
