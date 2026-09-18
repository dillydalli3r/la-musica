import { useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Activity, BadgeInfo, Disc3, Flame, Gauge, ImagePlus, ListMusic, Music2, Music4, RefreshCw,
  ShieldCheck, Sparkles, Tags,
} from "lucide-react";
import { api } from "../api";
import OverflowMenu from "./OverflowMenu";
import MetadataReviewModal from "./MetadataReviewModal";
import { toast } from "../store";
import type { ScriptRunResult } from "../types";

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
  buttonLabel,
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
  /** Optional trigger text. Left out, the trigger is the tag glyph alone —
   *  square and icon-only like the buttons it sits beside, with `buttonTitle`
   *  as the tooltip. */
  buttonLabel?: string;
}) {
  const [review, setReview] = useState<null | "artist" | "album">(null);
  const navigate = useNavigate();

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
  // A script run answers with per-script stats; the menu speaks in files.
  const ran = (r: { results?: ScriptRunResult[] }) => {
    const results = r.results ?? [];
    const failed = results.filter((x) => x.error);
    if (failed.length) return `${failed.length} script(s) failed — ${failed[0].error}`;
    if (results.length && results.every((x) => x.skipped))
      return results[0].reason || "Nothing to do";
    const touched = results.reduce(
      (n, x) => n + Number((x.stats as { modified_count?: number } | undefined)?.modified_count ?? 0),
      0
    );
    return `${touched} file(s) updated`;
  };

  return (
    <>
      <OverflowMenu
        buttonClass={buttonClass}
        buttonTitle={buttonTitle}
        icon={Tags}
        label={buttonLabel}
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
                // Opens the wizard for this album instead of firing the chain
                // behind the user's back: the import screen is where the chain
                // button, the script picker and "Run all scripts" live, so the
                // menu item takes them there. A track/artist selection has no
                // album folder to open, so the chain still runs directly.
                label: albumPath ? "Open in the import wizard…" : "Re-run import chain (MB re-stamp)",
                icon: RefreshCw,
                disabled: !paths.length,
                title: albumPath
                  ? "Open the import wizard for this album — chain, scripts and Run all live there"
                  : "Re-run the configured import chain over the selection",
                onClick: () =>
                  albumPath
                    ? navigate(`/import?album=${encodeURIComponent(albumPath)}`)
                    : run(() => api.importFinish(paths), () => "Import chain re-run"),
              },
            ],
          },
          {
            // Library scripts that rewrite files the selection already
            // carries (an AUDIT tag, a .accurip, a DR value): the FORCE flag
            // is what makes them redo the work instead of skipping it, which
            // is the only way to fix a wrong verdict/sidecar from here.
            title: "Re-run & overwrite",
            items: [
              {
                label: "Force re-audit (rewrite AUDIT tags)",
                icon: ShieldCheck,
                disabled: !paths.length,
                title: "Audit the selection again even where an AUDIT tag already exists, and overwrite it with the new verdict",
                onClick: () => run(() => api.run([6], paths, { force_audit: true }), ran),
              },
              {
                label: "Force AccurateRip (.accurip rewrite)",
                icon: Disc3,
                disabled: !paths.length,
                title: "Regenerate each disc's .accurip from CUETools, overwriting the existing file",
                onClick: () => run(() => api.run([9], paths, { force_accurip: true }), ran),
              },
              {
                label: "Force DR & ReplayGain (rewrite tags)",
                icon: Activity,
                disabled: !paths.length,
                title: "Re-measure dynamic range / ReplayGain and overwrite the stored values",
                onClick: () => run(() => api.run([7], paths, { force_dr_replaygain: true }), ran),
              },
              {
                label: "Force re-encode FLACs",
                icon: Flame,
                disabled: !paths.length,
                title: "Re-encode the selection even where a FLAC is already optimized",
                onClick: () => run(() => api.run([3], paths, { force_reencode_flac: true }), ran),
              },
              {
                label: "Re-grade",
                icon: Gauge,
                disabled: !paths.length,
                title: "Grade the selection again and refresh the cached verdicts",
                onClick: () => run(() => api.run([4], paths), ran),
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
