import { useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Activity, BadgeInfo, Disc3, Ellipsis, Flame, Gauge, ImagePlus, Info, Languages, ListMusic, Music2,
  RefreshCw, ShieldCheck, Sparkles, Tags, UploadCloud, Users,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { api } from "../api";
import BulkTagsDialog from "./BulkTagsDialog";
import OverflowMenu from "./OverflowMenu";
import MetadataReviewModal from "./MetadataReviewModal";
import Modal from "./Modal";
import { CreditsPanel } from "./TrackDetails";
import { DetailsDialog } from "./AlbumDetails";
import { advisoryOutcome } from "./Badges";
import { toast } from "../store";
import type { LyricsPublishBatchResult, LyricsXlitResult, ScriptRunResult } from "../types";

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
  icon: Icon = Tags,
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
  /** Trigger glyph. The tag glyph where this menu IS the tagging button; the
   *  "…" where it is one more action among a row's others. */
  icon?: LucideIcon;
}) {
  const [review, setReview] = useState<null | "artist" | "album">(null);
  const [tagsOpen, setTagsOpen] = useState(false);
  // Credits / Details of the CURRENT selection. Per release, so they need an
  // album folder (the whole release) or exactly one file (that recording);
  // a multi-track selection without an album has nothing to show.
  const [view, setView] = useState<null | "credits" | "details">(null);
  const viewable = !!albumPath || paths.length === 1;
  const singleTrack = albumPath ? undefined : paths[0];
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
  /** The genre chain's own answer: how many files it wrote, which sources
   *  contributed (and how many names each), and why each silent one stayed
   *  silent — a blocked RateYourMusic reads as its own reason here instead of
   *  as a bare "0 updated". */
  const genresDone = (r: {
    updated: number;
    per_source?: Record<string, string[]>;
    notes?: Record<string, string>;
  }) => {
    const who = Object.entries(r.per_source ?? {})
      .filter(([, names]) => names.length)
      .map(([name, names]) => `${name} ${names.length}`)
      .join(", ");
    const silent = Object.values(r.notes ?? {});
    return [
      r.updated ? `${r.updated} file(s) updated` : "No genres to write",
      who,
      silent.join("; "),
    ].filter(Boolean).join(" — ");
  };
  /** Script 17's own answer: the files it changed, what it left alone, its
   *  per-file errors, and the reason it changed nothing when it had nothing to
   *  work with (both switches off, no AI configured) — never a bare "done". */
  const xlitDone = (r: LyricsXlitResult) =>
    [
      `${r.ok} file(s) updated`,
      r.skipped ? `${r.skipped} unchanged` : "",
      r.errors.length ? `${r.errors.length} failed — ${r.errors[0]}` : "",
      r.note,
    ].filter(Boolean).join(" · ");
  /** Script 18's own answer per track: submissions, what LRCLIB said about the
   *  rest, and the failures with the provider's own words. */
  const publishDone = (r: LyricsPublishBatchResult) => {
    const reasons = new Map<string, number>();
    for (const res of r.results) {
      if (res.status !== "skipped") continue;
      const why = res.reason || "skipped";
      reasons.set(why, (reasons.get(why) ?? 0) + 1);
    }
    const fails = r.results.filter((res) => res.status === "failed");
    return [
      `${r.ok} submitted`,
      ...[...reasons].map(([why, n]) => `${n} × ${why}`),
      fails.length ? `${fails.length} failed — ${fails[0].reason || fails[0].message || "no message"}` : "",
    ].filter(Boolean).join(" · ");
  };
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
        icon={Icon}
        label={buttonLabel}
        sections={[
          {
            // What the selection IS, before what can be done to it: the
            // release's credits (or the single track's) and the stored readout.
            title: "View",
            items: [
              {
                label: albumPath ? "Credits (this album)…" : "Credits (this track)…",
                icon: Users,
                hidden: !viewable,
                title: albumPath
                  ? "Performers, instruments and studio roles for the whole release — one MusicBrainz request"
                  : "Performers, instruments and studio roles for this recording",
                onClick: () => setView("credits"),
              },
              {
                label: albumPath ? "Details (this album)…" : "Details (this track)…",
                icon: Info,
                hidden: !viewable,
                title: "The stored tags, technical readout and grading for the selection",
                onClick: () => setView("details"),
              },
            ],
          },
          {
            title: "Tags",
            items: [
              {
                label: "Tag editor…",
                icon: Tags,
                disabled: !paths.length,
                title: "Set or remove tags on the selection — the bulk editor, applied straight to the files",
                onClick: () => setTagsOpen(true),
              },
              {
                // ONE entry, the configured chain: it asks every ticked source
                // in priority order (Settings → Import, or the wizard's tray)
                // and stops as soon as a track's list is complete. The
                // MusicBrainz-only entry this replaced asked one source the
                // chain already ranks for itself, so the user had to know the
                // order to pick correctly.
                label: "Import genres",
                icon: Tags,
                disabled: !paths.length,
                title: "Ask every configured genre source for the selection's genres, in the configured priority order",
                onClick: () => run(() => api.genresImport(paths), genresDone),
              },
              {
                // ONE advisory entry, and it asks anyway: the sources are
                // asked even for a file that already carries a 0/1/2, and what
                // they state IS written — a source's answer can lower a rating
                // (1 → 0), and a value an earlier run invented must not keep
                // answering for a file nobody asked about (see
                // `fetch_advisories`). A second, gentler entry would be the
                // one that leaves such a value standing, and a user who
                // presses THIS button is asking for the sources' answer. The
                // label says so, which is the only guard a deliberate press
                // needs; the fill-only pass a press must not get is the
                // IMPORT's own automatic fetch, where nobody pressed anything.
                label: "Fetch / refresh advisory rating",
                icon: BadgeInfo,
                disabled: !paths.length && !releaseMbid,
                title: "Ask the configured advisory sources for the selection's ITUNESADVISORY and write what they state — files that already carry a value are asked too, and a source's answer can lower a rating",
                onClick: () =>
                  run(() => api.mbAdvisoryFetch({ paths, release_mbid: releaseMbid, force: true }), (r) =>
                    advisoryOutcome(r)
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
            // The lyrics family in full: an import runs script 13 (fetch) →
            // 17 (transliterate/translate) → 18 (publish), and each half is
            // here by hand. Every entry calls the half's own entry point, so
            // the tags and sidecars a click writes are the ones an import
            // writes — and the publish, which owns no local tag, is the one
            // step that only talks to LRCLIB.
            title: "Lyrics",
            items: [
              {
                label: "Fetch / refresh lyrics",
                icon: ListMusic,
                disabled: !paths.length,
                title: "Script 13's engine over the selection: every configured provider in order, written per the lyrics format (tags and/or .lrc)",
                onClick: () => run(() => api.lyricsAuto(paths, true), (r) => `${r?.ok ?? 0} lyrics fetched`),
              },
              {
                label: "Transliterate / translate lyrics…",
                icon: Languages,
                disabled: !paths.length,
                title: "Script 17 over the selection: the TRANSLITERATION-<lang> / TRANSLATION-<lang> tags the lyrics need, plus the .romaji.lrc / .<lang>.lrc sidecars the LRC formats write. Needs AI configured in Settings → AI.",
                onClick: () => run(() => api.lyricsXlit(paths), xlitDone),
              },
              {
                label: "Publish lyrics to LRCLIB…",
                icon: UploadCloud,
                disabled: !paths.length,
                title: "Script 18 over the selection: submit only the lyrics LRCLIB does not have yet. Writes nothing to the files — this is the one outward lyrics step.",
                onClick: () => run(() => api.lyricsPublishBatch(paths), publishDone),
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
                onClick: () => run(() => api.run([6], paths, { audit: true }), ran),
              },
              {
                label: "Force AccurateRip (.accurip rewrite)",
                icon: Disc3,
                disabled: !paths.length,
                title: "Regenerate each disc's .accurip from CUETools, overwriting the existing file",
                onClick: () => run(() => api.run([9], paths, { accurip: true }), ran),
              },
              {
                label: "Force DR & ReplayGain (rewrite tags)",
                icon: Activity,
                disabled: !paths.length,
                title: "Re-measure dynamic range / ReplayGain and overwrite the stored values",
                onClick: () => run(() => api.run([7], paths, { dr: true }), ran),
              },
              {
                label: "Force re-encode FLACs",
                icon: Flame,
                disabled: !paths.length,
                title: "Re-encode the selection even where a FLAC is already optimized",
                onClick: () => run(() => api.run([3], paths, { flac: true }), ran),
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
      {view === "credits" && (
        <Modal
          onClose={() => setView(null)}
          icon={Users}
          title="Credits"
          subtitle={albumPath || singleTrack}
          width="max-w-lg"
          bodyClass="px-5 py-5"
        >
          {/* An album is one release request; a single file is one recording
              request. Both come from the same panel the album and track pages
              mount, so the three entry points cannot drift apart. */}
          <CreditsPanel album={albumPath} path={singleTrack} />
        </Modal>
      )}
      {view === "details" && (
        <DetailsDialog albumPath={albumPath} trackPath={singleTrack} onClose={() => setView(null)} />
      )}
      {tagsOpen && (
        <BulkTagsDialog
          paths={paths}
          onClose={() => {
            setTagsOpen(false);
            onDone?.();
          }}
        />
      )}
    </>
  );
}

/** The "…" a LISTED track wears: one file in, its tagging, scripts and credits
 *  out. The same menu the track page mounts, scoped to the one track — no
 *  album folder, so the release-wide entries stay off a row that is not a
 *  release. */
export function TrackActionsMenu({
  path,
  releaseMbid,
  buttonClass = "!p-1 text-zinc-500 hover:text-white",
}: {
  path: string;
  /** The track's release MBID, when its tags carry one — lets the advisory
   *  lookup go straight to the release instead of resolving it again. */
  releaseMbid?: string | null;
  buttonClass?: string;
}) {
  return (
    <TagActionsMenu
      paths={[path]}
      releaseMbid={releaseMbid ?? undefined}
      icon={Ellipsis}
      buttonClass={buttonClass}
      buttonTitle="Track actions — tagging, scripts, credits"
    />
  );
}
