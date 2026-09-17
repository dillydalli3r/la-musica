import { useState } from "react";
import {
  BadgeInfo, Disc3, ImagePlus, ListMusic, Music2, Music4, RefreshCw, Sparkles, Tags,
} from "lucide-react";
import { api } from "../api";
import OverflowMenu from "./OverflowMenu";
import MetadataReviewModal from "./MetadataReviewModal";
import { toast } from "../store";

/** The subset of the API replies the menu reports back to the user. */
type ActionCounts = { updated?: number; queued?: number };

/** The one "tag actions" menu, mounted wherever a selection exists (artist /
 *  album / track level). Every entry re-runs on the CURRENT selection, so any
 *  tagging step can be repeated as often as the user likes. */
export default function TagActionsMenu({
  paths,
  artist,
  albumPath,
  releaseMbid,
  covers,
  onDone,
  buttonClass = "btn-ghost",
  buttonTitle = "Tag actions",
}: {
  /** The tracks/albums the actions apply to. */
  paths: string[];
  /** Artist folder path or name — enables the artist image/description review. */
  artist?: string;
  /** Album folder — enables the album description review. */
  albumPath?: string;
  /** Release MBID, when known, so the advisory lookup can go straight to it. */
  releaseMbid?: string;
  /** Opens the page's cover search for the same selection, when it has one. */
  covers?: () => void;
  onDone?: () => void;
  buttonClass?: string;
  buttonTitle?: string;
}) {
  const [review, setReview] = useState<null | "artist" | "album">(null);

  // Generic over the reply: each action reports from its OWN payload, so the
  // handler type is the real response shape, not a lowest common denominator.
  const run = async <T,>(fn: () => Promise<T>, done: (r: T) => string) => {
    if (!paths.length && !artist && !albumPath) return;
    try {
      toast(await fn().then(done));
      onDone?.();
    } catch (e) {
      toast.error(String(e));
    }
  };
  const json = (r: ActionCounts, unit: string) =>
    typeof r.updated === "number" ? `${r.updated} ${unit} updated` : `${r.queued ?? 0} queued`;

  return (
    <>
      <OverflowMenu
        buttonClass={buttonClass}
        buttonTitle={buttonTitle}
        sections={[
          {
            title: "Tags",
            items: [
              {
                label: "Import genres (all sources)",
                icon: Tags,
                disabled: !paths.length,
                title: "Ask every configured genre source for the selection's genres",
                onClick: () => run(() => api.genresImport(paths), (r) => json(r, "album")),
              },
              {
                label: "Import genres (MusicBrainz)",
                icon: Music4,
                disabled: !paths.length,
                onClick: () => run(() => api.mbGenresWrite(paths), (r) => json(r, "track")),
              },
              {
                label: "Fetch advisory rating",
                icon: BadgeInfo,
                disabled: !paths.length && !releaseMbid,
                title: "Look the ITUNESADVISORY value up and write it",
                onClick: () =>
                  run(() => api.mbAdvisoryFetch({ paths, release_mbid: releaseMbid }), (r) =>
                    `${r?.updated ?? 0} track(s) re-rated`
                  ),
              },
              {
                label: "Check instrumental",
                icon: Music2,
                disabled: !paths.length,
                title: "Ask the configured sources whether each track is instrumental, and write INSTRUMENTAL",
                onClick: () =>
                  run(() => api.instrumentalFetch(paths), (r) =>
                    `${r?.updated ?? 0} track(s) checked`
                  ),
              },
              {
                label: "Fetch / refresh lyrics",
                icon: ListMusic,
                disabled: !paths.length,
                onClick: () => run(() => api.lyricsAuto(paths, true), (r) => `${r?.ok ?? 0} lyrics fetched`),
              },
              {
                label: "Re-run import chain (MB re-stamp)",
                icon: RefreshCw,
                disabled: !paths.length,
                title: "Re-run the configured import chain over the selection",
                onClick: () => run(() => api.importFinish(paths), () => "Import chain re-run"),
              },
            ],
          },
          {
            title: "Artwork & text",
            items: [
              {
                label: "Artist image + description…",
                icon: ImagePlus,
                hidden: !artist,
                onClick: () => setReview("artist"),
              },
              {
                label: "Album description…",
                icon: Disc3,
                hidden: !albumPath,
                onClick: () => setReview("album"),
              },
              {
                label: "Cover search…",
                icon: Sparkles,
                hidden: !covers,
                onClick: () => covers?.(),
              },
            ],
          },
        ]}
      />
      {review && (
        <MetadataReviewModal
          artist={review === "artist" ? artist : undefined}
          albumPath={review === "album" ? albumPath : undefined}
          paths={review === "album" ? paths : undefined}
          title={review === "artist" ? "Artist metadata" : "Album description"}
          onClose={() => setReview(null)}
          onSaved={onDone}
        />
      )}
    </>
  );
}
