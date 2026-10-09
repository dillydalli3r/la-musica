import { BarChart3 } from "lucide-react";
import { useMemo, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { trackRef } from "../lib/refs";
import { canonicalGenre, splitGenres } from "../lib/genres";
import { useStore } from "../store";
import type { TrackTags } from "../types";
import Modal from "./Modal";

interface StatTrack {
  path?: string;
  grade_pass?: boolean;
  audit?: string | null;
  log_grade?: string | number | null;
  lyrics_present?: boolean;
  tags?: TrackTags;
  issues?: string[];
}

interface StatAlbum {
  pass?: boolean;
  grade_pct?: number | null;
  pass_count?: number;
  total_checks?: number;
  audit_summary?: string | null;
  media?: string | null;
  track_count?: number;
}

/** The library-layout condition, as the stored scan reported it. Optional and
 *  only ever passed for the WHOLE-library scope: the scan describes the music
 *  folder, so it says nothing about a track selection graded on its own. */
export interface StatLayout {
  total: number;
  counts: Record<string, number>;
  /** When the scan ran (UTC ISO), or null if the report does not say. */
  scanned_at: string | null;
}

/** Aggregated grading + audit statistics for any scope (library, selection,
 * artist or album). Rendered as a modal panel. */
export default function StatsPanel({
  title,
  albums,
  tracks,
  layout,
  onClose,
}: {
  title: string;
  albums?: StatAlbum[];
  tracks: StatTrack[];
  layout?: StatLayout;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const setQuery = useStore((s) => s.setQuery);
  const s = useMemo(() => {
    const albumsArr = albums ?? [];
    const checks = albumsArr.reduce((n, a) => n + (a.total_checks ?? 0), 0);
    const checksPassed = albumsArr.reduce((n, a) => n + (a.pass_count ?? 0), 0);
    const trackTotal = tracks.length || albumsArr.reduce((n, a) => n + (a.track_count ?? 0), 0);
    const trackPass = tracks.filter((t) => t.grade_pass).length;
    const audit = { REAL: 0, FAKE: 0, MIX: 0, none: 0 };
    for (const t of tracks) {
      const a = t.audit ?? "none";
      if (a === "REAL" || a === "FAKE" || a === "MIX") audit[a]++;
      else audit.none++;
    }
    const media: Record<string, number> = {};
    for (const a of albumsArr) {
      const m = (a.media ?? "").toUpperCase();
      if (m) media[m.includes("CD") ? "CD" : m.includes("DIGITAL") ? "Digital" : a.media ?? "?"] = (media[a.media ?? "?"] ?? 0) + 1;
    }
    const advisory = { 0: 0, 1: 0, 2: 0, none: 0 };
    const genres = new Map<string, number>();
    let instrumental = 0;
    let withLyrics = 0;
    for (const t of tracks) {
      const tags = t.tags ?? {};
      const adv = tags.ITUNESADVISORY;
      if (adv === "0" || adv === "1" || adv === "2") advisory[adv]++;
      else advisory.none++;
      if (tags.INSTRUMENTAL === "1") instrumental++;
      if (t.lyrics_present) withLyrics++;
      // A GENRE tag holds a LIST now (repeated fields, read back joined): one
      // name, one chip — the shared splitter handles both the storage "; " and
      // the rendered " / " separator, so "shoegaze / rock" is two genres.
      for (const g of splitGenres(tags.GENRE)) {
        // MusicBrainz's own spelling, the same fold `/api/genres/facets`
        // applies, so this list and the Genres page cannot report the same
        // tags under two different names ("Shoegaze" vs "shoegaze").
        const gg = canonicalGenre(g);
        if (gg) genres.set(gg, (genres.get(gg) ?? 0) + 1);
      }
    }
    const lyricsCoverage = trackTotal ? Math.round((100 * withLyrics) / trackTotal) : 0;
    const logGrades = tracks
      .map((t) => Number(t.log_grade))
      .filter((v) => Number.isFinite(v));
    const avgLogGrade = logGrades.length ? Math.round(logGrades.reduce((a, b) => a + b, 0) / logGrades.length) : null;
    return {
      trackTotal,
      trackPass,
      checks, checksPassed,
      audit, media, advisory,
      instrumental, lyricsCoverage,
      genres: [...genres.entries()].sort((a, b) => b[1] - a[1]).slice(0, 10),
      avgLogGrade, logGradeCount: logGrades.length,
    };
  }, [albums, tracks]);

  const gradePct =
    s.checks > 0
      ? Math.round((100 * s.checksPassed) / s.checks)
      : s.trackTotal > 0
        ? Math.round((100 * s.trackPass) / s.trackTotal)
        : null;

  return (
    <Modal
      onClose={onClose}
      icon={BarChart3}
      title={`Statistics — ${title}`}
      width="max-w-xl"
      bodyClass="px-5 py-5 space-y-4"
    >
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
        <div className="rounded-lg bg-panel border border-border px-3 py-2">
          <div className="text-[10px] uppercase tracking-wider text-zinc-500">Tracks</div>
          <div className="text-lg font-bold">{s.trackTotal}</div>
          <div className="text-[11px] text-zinc-500">{s.trackPass} passing</div>
        </div>
        <div className="rounded-lg bg-panel border border-border px-3 py-2">
          <div className="text-[10px] uppercase tracking-wider text-zinc-500">Grade</div>
          <div className="text-lg font-bold">{gradePct === null ? "—" : `${gradePct}%`}</div>
          <div className="text-[11px] text-zinc-500">
            {s.checksPassed}/{s.checks} checks
          </div>
        </div>
        <div className="rounded-lg bg-panel border border-border px-3 py-2">
          <div className="text-[10px] uppercase tracking-wider text-zinc-500">Albums</div>
          <div className="text-lg font-bold">{albums?.length ?? "—"}</div>
          <div className="text-[11px] text-zinc-500">{(albums ?? []).filter((a) => a.pass).length} passing</div>
        </div>
        <div className="rounded-lg bg-panel border border-border px-3 py-2">
          <div className="text-[10px] uppercase tracking-wider text-zinc-500">Lyrics</div>
          <div className="text-lg font-bold">{s.lyricsCoverage}%</div>
          <div className="text-[11px] text-zinc-500">{s.instrumental} instrumental</div>
        </div>
        {layout && (
          <div className="rounded-lg bg-panel border border-border px-3 py-2">
            <div className="text-[10px] uppercase tracking-wider text-zinc-500">Library layout</div>
            <div className={`text-lg font-bold ${layout.total ? "text-amber-300" : "text-emerald-400"}`}>
              {layout.total === 0 ? "clean" : layout.total}
            </div>
            <div className="text-[11px] text-zinc-500">
              {layout.total === 0
                ? "every folder in place"
                : `${Object.keys(layout.counts).length} categor${Object.keys(layout.counts).length === 1 ? "y" : "ies"}`}
            </div>
          </div>
        )}
      </div>

      {/* The grade tiles above count only what the library can SEE: a file
          outside Artists/<Artist>/<Album>/ is never graded, an empty album
          folder grades as an error the albums tile cannot attribute. So the
          scan's findings belong beside them — a library with layout problems
          is not a clean library, whatever the grade percentage says. Kept
          apart from that percentage on purpose: these are not tag failures
          on any album, and folding them in would blame tracks that are fine. */}
      {layout && layout.total > 0 && (
        <div className="text-xs text-amber-200 bg-amber-950/30 border border-amber-900/60 rounded-lg px-3 py-2">
          <div className="font-semibold">
            Not clean: the last layout scan ({layout.scanned_at ? new Date(layout.scanned_at).toLocaleString() : "time unknown"})
            found {layout.total} problem{layout.total === 1 ? "" : "s"} in the music folder.
          </div>
          <div className="mt-1 text-amber-200/90">
            {Object.entries(layout.counts)
              .sort((a, b) => b[1] - a[1])
              .map(([kind, n]) => `${n}× ${kind.replace(/_/g, " ")}`)
              .join(" · ")}
          </div>
          <div className="mt-1 text-amber-200/70">
            Library-wide, not per-album tag failures — audio outside the artist/album tree is not graded at all.
            Script 20 and Optimization → Library layout both report and fix it.
          </div>
        </div>
      )}

      <div>
        <div className="text-xs font-semibold uppercase tracking-wider text-zinc-400 mb-1.5">AUDIT</div>
        <div className="flex flex-wrap gap-1.5">
          <span className="chip bg-emerald-900/50 text-emerald-300 border border-emerald-800">{s.audit.REAL} real</span>
          <span className="chip bg-red-900/50 text-red-300 border border-red-900">{s.audit.FAKE} fake</span>
          <span className="chip bg-amber-900/50 text-amber-300 border border-amber-900">{s.audit.MIX} mixed</span>
          <span className="chip bg-raise border border-border text-zinc-400">{s.audit.none} not audited</span>
          {s.avgLogGrade !== null && (
            <span className="chip bg-raise border border-border text-zinc-400">log score avg {s.avgLogGrade}/100 ({s.logGradeCount})</span>
          )}
        </div>
      </div>

      <div>
        <div className="text-xs font-semibold uppercase tracking-wider text-zinc-400 mb-1.5">Media</div>
        <div className="flex flex-wrap gap-1.5">
          {Object.entries(s.media).map(([m, n]) => (
            <span key={m} className="chip bg-raise border border-border text-zinc-300">{m}: {n}</span>
          ))}
          {!Object.keys(s.media).length && <span className="text-xs text-zinc-600">no album media info</span>}
        </div>
      </div>

      <div>
        <div className="text-xs font-semibold uppercase tracking-wider text-zinc-400 mb-1.5">Advisory</div>
        <div className="flex flex-wrap gap-1.5">
          <span className="chip bg-raise border border-border text-zinc-300">0 clean: {s.advisory["0"]}</span>
          <span className="chip bg-raise border border-border text-zinc-300">1 explicit: {s.advisory["1"]}</span>
          <span className="chip bg-raise border border-border text-zinc-300">2 safe: {s.advisory["2"]}</span>
          <span className="chip bg-raise border border-border text-zinc-500">unset: {s.advisory.none}</span>
        </div>
      </div>

      <div>
        <div className="text-xs font-semibold uppercase tracking-wider text-zinc-400 mb-1.5">Top genres</div>
        <div className="flex flex-wrap gap-1.5">
          {s.genres.map(([g, n]) => (
            <button
              key={g}
              className="chip bg-accent/10 text-accent-soft border border-accent/25 hover:border-accent hover:bg-accent/20 transition-colors"
              title={`Show every ${g} track in the library`}
              onClick={() => {
                setQuery(`genre:"${g}"`);
                navigate("/library");
              }}
            >
              {g} × {n}
            </button>
          ))}
          {!s.genres.length && <span className="text-xs text-zinc-600">no genre tags</span>}
        </div>
      </div>

      {tracks.length > 0 && (
        <div>
          <div className="text-xs font-semibold uppercase tracking-wider text-zinc-400 mb-1.5">
            Per-track breakdown ({tracks.length})
          </div>
          <div className="rounded-md border border-border overflow-hidden max-h-64 overflow-y-auto">
            <table className="w-full text-xs">
              <thead className="bg-panel/60">
                <tr>
                  <th className="th">Title</th>
                  <th className="th">Grade</th>
                  <th className="th">Checks</th>
                  <th className="th">Audit</th>
                  <th className="th">Log</th>
                </tr>
              </thead>
              <tbody>
                {tracks.map((t, i) => {
                  const name = t.tags?.TITLE ?? `Track ${i + 1}`;
                  const cell = (inner: ReactNode) =>
                    t.path ? (
                      <Link to={trackRef(t)} className="hover:text-accent-soft">
                        {inner}
                      </Link>
                    ) : (
                      <span>{inner}</span>
                    );
                  return (
                    <tr key={t.path ?? i} className={`table-row ${t.issues?.length ? "bg-red-950/20" : ""}`}>
                      <td className="td truncate max-w-[220px]">{cell(name)}</td>
                      <td className="td">{cell(t.grade_pass ? <span className="text-emerald-400">PASS</span> : <span className="text-red-400">FAIL</span>)}</td>
                      <td className="td text-zinc-500">{cell(t.issues?.length ?? 0)}</td>
                      <td className="td">
                        {cell(
                          t.audit === "REAL" ? <span className="text-emerald-400">REAL</span>
                          : t.audit === "FAKE" ? <span className="text-red-400">FAKE</span>
                          : t.audit === "MIX" ? <span className="text-amber-400">MIX</span>
                          : <span className="text-zinc-600">—</span>
                        )}
                      </td>
                      <td className="td text-zinc-500">{cell(t.log_grade != null && t.log_grade !== "" ? `${t.log_grade}/100` : "—")}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="text-[10px] text-zinc-600 mt-1">Click a row to open the track page with full check details.</div>
        </div>
      )}
    </Modal>
  );
}