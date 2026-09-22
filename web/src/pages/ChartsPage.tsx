import { Link, useSearchParams } from "react-router-dom";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { BarChart3, Loader2 } from "lucide-react";
import { api, type ChartPeriod, type DiscoverKind } from "../api";
import PageHeader from "../components/PageHeader";
import Segmented from "../components/Segmented";
import { EmptyState, PageLoading } from "../components/Badges";
import DiscoverRow, { NotesChips } from "../components/DiscoverRow";
import { useI18n } from "../lib/i18n";
import { albumRef, artistRef, trackRef } from "../lib/refs";
import { fmtDuration } from "../lib/fmt";

/** Rows per answer — the server's own ceiling is 200, and a chart is a front
 *  page rather than a catalogue: fifty is what a reader scrolls. */
const LIMIT = 50;

/** Which half of the page to draw. "library" is the user's own play history
 *  (`/api/top`), "online" the providers' charts (`/api/discover/charts`), and
 *  "all" both — side by side, never merged, because a play count and a
 *  provider's rank are different kinds of number. */
type Scope = "library" | "online" | "all";

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

/** Charts: what has actually been played, and what the world is playing.
 *
 *  Two sources of truth in one page, labelled as such. The library half counts
 *  THIS user's plays in the selected window, server-side (`GET /api/top`), and
 *  every row opens the real track/album/artist page. The online half shows each
 *  provider's own chart, keeps the providers separate (a rank belongs to one
 *  chart), names the provider on every row and offers the same Add action a
 *  Discover row does. Both halves say why they are empty: the library explains
 *  when a play is recorded, and the online side prints every source's own
 *  outcome — answered, skipped for a missing key, unsupported for this window,
 *  or the provider's own refusal, verbatim.
 *
 *  The window, the kind and the scope are query params, so a view someone
 *  found is a link. */
export default function ChartsPage() {
  const { t } = useI18n();
  const [params, setParams] = useSearchParams();
  const period = (params.get("period") as ChartPeriod) || "all";
  const kind = (params.get("kind") as DiscoverKind) || "tracks";
  const scope = (params.get("scope") as Scope) || "all";

  const write = (patch: Partial<Record<"period" | "kind" | "scope", string>>) => {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(patch)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    setParams(next, { replace: true });
  };

  const wantLibrary = scope !== "online";
  const wantOnline = scope !== "library";

  // The library's own counts — cheap (one grouped SQL read, TTL-cached client
  // side), and refetched when the window changes rather than recomputed here.
  const libQ = useQuery({
    queryKey: ["topCharts", period, kind],
    queryFn: () => api.topCharts({ period, kind, limit: LIMIT }),
    enabled: wantLibrary,
    placeholderData: keepPreviousData,
    staleTime: 60_000,
    retry: false,
  });
  // The providers: slow (a source chain, RateYourMusic's scrape included),
  // TTL-cached server-side, so a page being read does not re-walk it.
  const onQ = useQuery({
    queryKey: ["discoverCharts", period, kind],
    queryFn: () => api.discoverCharts({ period, kind, limit: LIMIT }),
    enabled: wantOnline,
    placeholderData: keepPreviousData,
    staleTime: 5 * 60_000,
    retry: false,
  });

  const libRows = libQ.data?.items ?? [];
  const onRows = onQ.data?.items ?? [];
  const notes = onQ.data?.notes ?? {};
  const asked = onQ.data?.sources_asked ?? [];
  // The one note that is not a source: the server's verdict when nobody had
  // anything to rank. It is the online half's own sentence, shown in place of
  // the chip list.
  const chartNote = (notes as Record<string, string>).charts ?? "";
  const sourceNotes = Object.fromEntries(
    Object.entries(notes).filter(([id]) => id !== "charts")
  );

  return (
    <div className="p-6 space-y-5 mx-auto max-w-6xl">
      <PageHeader
        icon={BarChart3}
        title={t("page.charts")}
        subtitle={t("charts.subtitle")}
        actions={
          <Segmented
            value={scope}
            onChange={(s) => write({ scope: s })}
            options={[
              { id: "library" as Scope, label: t("charts.scope.library") },
              { id: "online" as Scope, label: t("charts.scope.online") },
              { id: "all" as Scope, label: t("charts.scope.all") },
            ]}
          />
        }
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
          {wantOnline && <NotesChips notes={sourceNotes} sources={asked} />}
        </div>
      </PageHeader>

      {wantLibrary && (
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
      )}

      {wantOnline && (
        <section className="space-y-3">
          <div className="flex items-baseline gap-2">
            <h2 className="text-lg font-semibold">{t("charts.online")}</h2>
            <span className="text-[11px] text-zinc-500">{t("charts.online_note")}</span>
            {onQ.isFetching && <Loader2 className="h-3.5 w-3.5 animate-spin text-zinc-500" />}
          </div>
          {onQ.isLoading ? (
            <PageLoading label={t("charts.loading_online")} />
          ) : onQ.error ? (
            <EmptyState
              title={t("charts.error_title")}
              hint={onQ.error instanceof Error ? onQ.error.message : String(onQ.error)}
              onAction={{ label: t("action.retry"), onClick: () => void onQ.refetch() }}
            />
          ) : onRows.length === 0 ? (
            // Every reason is the server's own: a source that needs a key names
            // it, one that refused repeats the provider's words, and one with
            // no such window says which windows it does publish — all in the
            // chips above.
            <EmptyState
              title={chartNote ? t("charts.no_source_title") : t("charts.unsupported_title")}
              hint={chartNote || t("charts.empty_online")}
            />
          ) : (
            <div className="panel !p-0 overflow-hidden">
              <div className="px-3 py-2 border-b border-border/60 text-[11px] text-zinc-500">
                {t("charts.rows", { n: onRows.length })}
              </div>
              <ul className="divide-y divide-border/60 stagger">
                {onRows.map((item, i) => (
                  <DiscoverRow
                    key={`${item.source}:${item.kind}:${item.mbid || item.path || item.title}-${i}`}
                    item={item}
                  />
                ))}
              </ul>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
