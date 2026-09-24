import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { Radio } from "lucide-react";
import { api } from "../api";
import PageHeader from "../components/PageHeader";
import Segmented from "../components/Segmented";
import AlbumCard from "../components/AlbumCard";
import { EmptyState, PageLoading } from "../components/Badges";
import { useI18n } from "../lib/i18n";
import { albumRef } from "../lib/refs";
import { GRID_SIZE_MIN } from "../lib/fmt";
import { GRID_SIZES, useGridSize } from "../lib/libraryView";
import type { HomeAlbum } from "../types";

/** One line of an episode's own identity, under the shared album card: the
 *  episode number MusicBrainz states and the episode's release date. A
 *  podcast episode is not an album by a band, so the card's own caption (the
 *  episode title) is not the whole of what a reader needs — this is where the
 *  series' own numbering shows, and where an episode with neither fact says
 *  nothing instead of inventing one. */
function EpisodeLine({ ep }: { ep: HomeAlbum }) {
  const { t } = useI18n();
  const number = ep.podcast?.episode;
  const date = ep.meta?.DATE || ep.meta?.ORIGINALDATE || "";
  if (number == null && !date) return null;
  return (
    <span className="truncate rounded-full px-1.5 py-0.5 text-[10px] leading-tight bg-accent/10 text-accent-soft">
      {number != null && <>{t("podcast.episode")} {number}</>}
      {number != null && date && <span className="text-zinc-500"> · </span>}
      {date}
    </span>
  );
}

/** A podcast SERIES and every episode of it the library holds, newest first.
 *
 *  MusicBrainz has no Podcast release-group type: a podcast is a SERIES of
 *  type Podcast whose episodes are release groups linked `part of` it, and
 *  the app records that series on the episodes' own files. So this page is a
 *  view of the LIBRARY — no MusicBrainz request per visit — and every episode
 *  is the library's own album row, drawn with the shared card and linking to
 *  the album (and track) pages the rest of the app already has.
 *
 *  The order is the server's: newest episode first, by the episode's date and
 *  then by MusicBrainz's episode number (see server.recommendations). The
 *  series name is the route key, disambiguation included, so two same-named
 *  shows are two pages.
 */
export default function PodcastPage() {
  const { series = "" } = useParams();
  const decoded = decodeURIComponent(series);
  const { t } = useI18n();
  const [gridSize, pickGridSize] = useGridSize();
  const { data, isLoading, error } = useQuery({
    queryKey: ["podcastSeries", decoded],
    queryFn: () => api.podcastSeries(decoded),
    // A series the library no longer holds a single episode of is a 404, not
    // a state worth retrying: the page says so and offers the way back.
    retry: false,
  });

  if (isLoading) {
    return (
      <div className="p-6 mx-auto max-w-6xl">
        <PageLoading />
      </div>
    );
  }
  if (error || !data) {
    return (
      <div className="p-6 mx-auto max-w-6xl">
        <EmptyState
          title={t("page.not_found")}
          hint={t("podcast.missing_hint", { series: decoded })}
          action={{ label: t("nav.home"), to: "/" }}
        />
      </div>
    );
  }

  const episodes = data.episodes ?? [];
  const newest = episodes[0];
  const newestDate = newest?.meta?.DATE || newest?.meta?.ORIGINALDATE || "";
  return (
    <div className="p-6 space-y-5 mx-auto max-w-6xl">
      <PageHeader
        icon={Radio}
        overline={t("page.podcast")}
        title={data.series}
        subtitle={
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-zinc-400">
            <span>
              <b className="text-zinc-100">{data.episode_count}</b> {t("podcast.episodes")}
            </span>
            {newestDate && (
              <>
                <span className="text-zinc-700">·</span>
                <span>{t("podcast.newest_episode")} {newestDate}</span>
              </>
            )}
            <span className="text-zinc-700">·</span>
            <span>{t("podcast.order")}</span>
            {data.series_mbid && (
              <>
                <span className="text-zinc-700">·</span>
                <a
                  className="text-accent-soft hover:text-accent"
                  href={`https://musicbrainz.org/series/${data.series_mbid}`}
                  target="_blank"
                  rel="noreferrer"
                  title="MusicBrainz series"
                >
                  MusicBrainz
                </a>
              </>
            )}
          </div>
        }
        actions={
          <span title="Cover size">
            <Segmented value={gridSize} onChange={pickGridSize} options={GRID_SIZES} />
          </span>
        }
      />
      {/* The shared album card, at the same cover size the Library and Home
          draw — one album can never look like two. */}
      <div
        className="grid gap-x-4 gap-y-5 stagger"
        style={{ gridTemplateColumns: `repeat(auto-fill, minmax(${GRID_SIZE_MIN[gridSize]}px, 1fr))` }}
      >
        {episodes.map((ep) => (
          <AlbumCard
            key={ep.path}
            al={ep}
            href={albumRef(ep)}
            extraMeta={<EpisodeLine ep={ep} />}
          />
        ))}
      </div>
    </div>
  );
}
