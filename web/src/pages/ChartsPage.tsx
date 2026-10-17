import { Link, useSearchParams } from "react-router-dom";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { BarChart3, Loader2 } from "lucide-react";
import { api, type ChartPeriod, type DiscoverKind } from "../api";
import PageHeader from "../components/PageHeader";
import Segmented from "../components/Segmented";
import { EmptyState, PageLoading } from "../components/Badges";
import { useI18n } from "../lib/i18n";
import { albumRef, artistRef, trackRef } from "../lib/refs";
import { fmtDuration } from "../lib/fmt";

/** Rows per answer — the server's own ceiling is 200, and a chart is a front
 *  page rather than a catalogue: fifty is what a reader scrolls. */
const LIMIT = 50;

/* The page is the user's own play history (`GET /api/top`). The providers'
 *  charts that used to sit beside it (`/api/discover/charts`) are gone with the
 *  online discovery feeds, so there is no scope to pick: a rank on this page is
 *  always a play count from THIS library. */

/** The two halves of the payload this page renders, so the row renderers below
 *  are driven by the answer rather than by a second hardcoded table. */
const PERIODS: { id: ChartPeriod; key: "all" | "year" | "month" | "week" }[] = [
  { id: "all", key: "all" },
  { id: "year", key: "year" },
  { id: "month", key: "month" },
  { id: "week", key: "week" },
];

const KINDS: { id: DiscoverKind; key: "tracks" | "albums" | "artists" }[] = [
  { id: "tracks", key: "tracks" },
  { id: "albums", key: "albums" },
  { id: "artists", key: "artists" },
];

/** Charts: what has actually been played in this library.
 *
 *  THIS user's plays in the selected window, counted server-side
 *  (`GET /api/top`), every row opening the real track/album/artist page, and
 *  the empty case saying when a play is recorded. The window and the kind are
 *  query params, so a view someone found is a link. */
export default function ChartsPage() {
  const { t } = useI18n();
  const [params, setParams] = useSearchParams();
  const period = (params.get("period") as ChartPeriod) || "all";
  const kind = (params.get("kind") as DiscoverKind) || "tracks";
  const write = (patch: Partial<Record<"period" | "kind", string>>) => {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(patch)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    setParams(next, { replace: true });
  };


  // The library's own counts — cheap (one grouped SQL read, TTL-cached client
  // side), and refetched when the window changes rather than recomputed here.
  const libQ = useQuery({
    queryKey: ["topCharts", period, kind],
    queryFn: () => api.topCharts({ period, kind, limit: LIMIT }),
    placeholderData: keepPreviousData,
    staleTime: 60_000,
    retry: false,
  });
  const libRows = libQ.data?.items ?? [];

  return (
    <div className="p-6 space-y-5 mx-auto max-w-[1600px]">
      <PageHeader
        icon={BarChart3}
        title={t("page.charts")}
        subtitle={t("charts.subtitle")}
      >
        <div className="flex flex-wrap items-center gap-3">
          <Segmented
            value={period}
            onChange={(p) => write({ period: p })}
            options={PERIODS.map((p) => ({ id: p.id, label: t(`charts.period.${p.key}`) }))}
          />
          <Segmented
            value={kind}
            onChange={(k) => write({ kind: k })}
            options={KINDS.map((k) => ({ id: k.id, label: t(`charts.kind.${k.key}`) }))}
          />
        </div>
      </PageHeader>

        <section className="space-y-3">
          <div className="flex items-baseline gap-2">
            <h2 className="text-lg font-semibold">{t("charts.your_library")}</h2>
            <span className="text-[11px] text-zinc-500">{t("charts.library_note")}</span>
            {libQ.isFetching && <Loader2 className="h-3.5 w-3.5 animate-spin text-zinc-500" />}
          </div>
          {libQ.isLoading ? (
            <PageLoading label={t("charts.loading_library")} />
          ) : libQ.error ? (
            <EmptyState
              title={t("charts.error_title")}
              hint={libQ.error instanceof Error ? libQ.error.message : String(libQ.error)}
              onAction={{ label: t("action.retry"), onClick: () => void libQ.refetch() }}
            />
          ) : libRows.length === 0 ? (
            // The server's own sentence about an empty history: "no plays
            // recorded yet — a play is written when a track actually starts",
            // or which window is empty while the store holds plays.
            <EmptyState title={t("charts.no_plays_title")} hint={libQ.data?.note || ""} />
          ) : (
            <div className="panel !p-0 overflow-hidden">
              <div className="px-3 py-2 border-b border-border/60 text-[11px] text-zinc-500">
                {t("charts.rows", { n: libRows.length })}
                {/* The window's own totals, next to the page's row count: the
                    plays it holds and the time behind them (the played
                    tracks' own lengths), so the two numbers a reader wants are
                    not "however many rows the limit printed". */}
                {libQ.data && libQ.data.plays_total > 0 && (
                  <span title={libQ.data.listened_unknown > 0
                    ? `${libQ.data.listened_unknown} play(s) have no length to add — their files are no longer in the library`
                    : undefined}>
                    {" · "}
                    {t("charts.listened", {
                      plays: libQ.data.plays_total,
                      time: fmtDuration(libQ.data.listened_seconds),
                    })}
                  </span>
                )}
              </div>
              <ul className="divide-y divide-border/60 stagger">
                {libRows.map((row, i) => {
                  // A play outlives a file: a row the library no longer holds
                  // still counts but has nothing to open, and says so.
                  const to = !row.in_library || !row.path
                    ? null
                    : row.kind === "album"
                      ? albumRef({ path: row.path })
                      : row.kind === "artist"
                        ? artistRef({ path: row.path, albums: [] })
                        : trackRef({ path: row.path });
                  const name =
                    row.kind === "artist" ? row.name || row.path || "" : row.title || row.path || "";
                  const under =
                    row.kind === "track"
                      ? [row.artist, row.album].filter(Boolean).join(" · ")
                      : row.artist || "";
                  const played =
                    row.plays === 1 ? t("charts.play_one") : t("charts.plays", { n: row.plays });
                  return (
                    <li key={`${row.kind}:${row.path}:${i}`} className="flex items-center gap-3 px-3 py-2">
                      <span className="w-6 shrink-0 text-right text-xs text-zinc-600 tabular-nums">{i + 1}</span>
                      <span className="min-w-0 flex-1">
                        {to ? (
                          <Link to={to} className="hover:text-accent-soft break-words" title={t("charts.open_page")}>
                            {name}
                          </Link>
                        ) : (
                          <span className="break-words text-zinc-400" title={t("charts.gone")}>
                            {name}
                          </span>
                        )}
                        {under && <span className="block text-[11px] text-zinc-500 break-words">{under}</span>}
                      </span>
                      <span className="shrink-0 chip bg-raise border border-border text-zinc-300" title={played}>
                        {played}
                      </span>
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
        </section>


    </div>
  );
}
