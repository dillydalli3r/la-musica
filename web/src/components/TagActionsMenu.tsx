import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowDownToLine, BadgeInfo, Disc3, Ellipsis, ExternalLink, FileOutput, Film, ImagePlus, Info, Music2, Play,
  RefreshCw, Sparkles, Tags, Users, Wand2,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { api } from "../api";
import BulkTagsDialog from "./BulkTagsDialog";
import ExportDialog from "./ExportDialog";
import OverflowMenu from "./OverflowMenu";
import MetadataReviewModal from "./MetadataReviewModal";
import Modal from "./Modal";
import { CreditsPanel } from "./TrackDetails";
import { DetailsDialog } from "./AlbumDetails";
import { advisoryOutcome } from "./Badges";
import { entityKind, scriptSections, type ScriptEntry } from "../lib/scriptMenu";
import { useI18n } from "../lib/i18n";
import { CACHED_PATHS_KEY, CACHED_SIZES_KEY } from "../lib/mediaCache";
import { downloadForOffline } from "../lib/offline";
import { trackRef } from "../lib/refs";
import { downloadTrackVideo } from "../lib/videoDownload";
import { toast } from "../store";
import type { EntityKind, ScriptRunResult } from "../types";

/** The one "tag actions" menu, mounted wherever a selection exists (artist /
 *  album / track level). Every entry re-runs on the CURRENT selection, so any
 *  tagging step can be repeated as often as the user likes. */
export default function TagActionsMenu({
  paths,
  artist,
  artistPath,
  albumPath,
  releaseMbid,
  covers,
  onDone,
  kind,
  buttonClass = "btn-ghost",
  buttonTitle = "Tag actions",
  buttonLabel,
  icon: Icon = Tags,
}: {
  /** The tracks/albums the actions apply to. */
  paths: string[];
  /** Artist folder path or name — enables the artist image/description review. */
  artist?: string;
  /** The artist's folder, when the caller has it: what a folder-scoped script
   *  (the artist image, the layout of the artist's subtrees) runs on. */
  artistPath?: string;
  /** Album folder — enables the album description review. */
  albumPath?: string;
  /** Release MBID, when known, so the advisory lookup can go straight to it. */
  releaseMbid?: string;
  /** Opens the page's cover search for the same selection, when it has one. */
  covers?: () => void;
  /** What the selection IS, when the caller knows better than the props imply
   *  (the track page holds its album's folder for its own panels, and is still
   *  a track). Left out, it is read from the props — see `entityKind`. */
  kind?: EntityKind;
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
  const [exportOpen, setExportOpen] = useState(false);
  const qc = useQueryClient();
  // Credits / Details of the CURRENT selection. Per release, so they need an
  // album folder (the whole release) or exactly one file (that recording);
  // a multi-track selection without an album has nothing to show.
  const [view, setView] = useState<null | "credits" | "details">(null);
  // The Run-all entry waits here while the user confirms it: a multi-script
  // pass over the selection is not a press to fire behind their back.
  const [confirmRunAll, setConfirmRunAll] = useState<ScriptEntry | null>(null);
  // The video download asks the track's own tags for its title/artist/length
  // before searching, so a press has a moment of work behind it.
  const [videoBusy, setVideoBusy] = useState(false);
  const viewable = !!albumPath || paths.length === 1;
  const singleTrack = albumPath ? undefined : paths[0];
  const navigate = useNavigate();
  const { t } = useI18n();

  // What this menu is ON decides which scripts it may offer (server/script_menu.py
  // derives the kinds each script applies to from the runner's own code).
  const entity = entityKind({ kind, albumPath, artist, paths });
  // The ONE track this menu is ON — the only selection that may be opened as a
  // page or searched for a music video. An album's folder menu and a multi-row
  // selection have no single track, and guessing one of several is exactly what
  // must not happen; `singleTrack` above is the FIRST of a list and is only
  // used where `viewable` has already excluded the multi-selection.
  const menuTrack = entity === "track" && paths.length === 1 ? paths[0] : undefined;
  // The registry, asked once per session: ids, labels, groups, the stack's
  // order, each script's force flag and its feature switch. A menu that cannot
  // reach the server (or is still loading) simply shows no script entries —
  // the tag editors, imports and reviews below are unaffected.
  const { data: scripts } = useQuery({
    queryKey: ["script-menu"],
    queryFn: api.scriptMenu,
    staleTime: 5 * 60 * 1000,
  });
  const generated = scriptSections(scripts, entity, { paths, albumPath, artistPath });

  /** Download the music video of the ONE track this menu is on.
   *
   *  The menu holds one fact about a listed track — its path — so the title,
   *  artist and length the search needs are read off the file's own tags at
   *  the press (the same read the track page makes), not guessed from the list
   *  the row came from. The download itself is the album page's own
   *  implementation (lib/videoDownload), so what this asks the server for is
   *  what the film button beside a row used to. */
  const downloadVideoHere = async () => {
    if (!menuTrack || videoBusy) return;
    setVideoBusy(true);
    try {
      const res = await api.tags(menuTrack);
      const tags = (res?.tags ?? {}) as Record<string, string | null>;
      const seconds = Number(res?.tech?.length ?? 0);
      await downloadTrackVideo(
        {
          path: menuTrack,
          artist: tags.ARTIST ?? tags.ALBUMARTIST ?? artist,
          title: tags.TITLE ?? undefined,
          duration: seconds > 0 ? Math.round(seconds) : undefined,
          tracknumber: Number(tags.TRACKNUMBER) || null,
          discnumber: Number(tags.DISCNUMBER) || null,
        },
        {
          // The queue lives on the Downloads page; a saved video changes the
          // album's own tracklist and its video shelf. Prefix keys: this menu
          // reaches the current page's queries without holding their album.
          onQueued: () => qc.invalidateQueries({ queryKey: ["soulseekDownloads"] }),
          onSaved: () => {
            qc.invalidateQueries({ queryKey: ["videos"] });
            qc.invalidateQueries({ queryKey: ["album"] });
          },
        }
      );
      onDone?.();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setVideoBusy(false);
    }
  };

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
  // A script run answers with per-script stats; the menu speaks in files.
  // The generated script entries below all report through it, so a script that
  // fixed four files, and one that failed, read the same way here as on the
  // Optimization page — and a skipped script says WHY it skipped, in the
  // runner's own words.
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
  /** One generated line: the ids the payload named, run over the paths THIS
   *  entity gives them, through the same /api/run the Optimization page uses —
   *  plain, or with the script's force key when the line is one of its forced
   *  twins. The force dict names exactly the one key the run needs (a supplied
   *  dict is authoritative server-side), so a forced entry can never force
   *  anything else on the way. */
  const scriptItem = (e: ScriptEntry) => {
    // The Run-all line: the whole applicable chain, in the stack's own order,
    // as ONE request. It asks first — it is a multi-script pass over the
    // selection, not one script the user named — and the confirmation lists
    // the scripts it will run, in order, from this same entry's ids.
    if (e.runAll) {
      return {
        // The count comes from the very list the request posts, so the label
        // and the request cannot disagree.
        label: t("menu.runAll", { count: e.ids.length }),
        icon: Play,
        disabled: e.disabled || !e.targets.length,
        title: t("menu.runAllHint"),
        onClick: () => setConfirmRunAll(e),
      };
    }
    // A forced twin reads as one ("Force: 6 · Audit library"): the two sections
    // hold the same scripts, and a bare repeated label would leave a reader
    // scrolling the panel unable to tell which one insists.
    return {
      label: e.force ? t("menu.forceEntry", { script: e.label }) : e.label,
      icon: Wand2,
      disabled: e.disabled || !e.targets.length,
      title: e.title,
      onClick: () =>
        run(() => api.run(e.ids, e.targets, e.force ? { [e.force]: true } : undefined), ran),
    };
  };
  /** The section a group of generated entries is shown under: the bundle's own
   *  name for the group the payload named (the payload's title is the fallback
   *  for a group a bundle does not know yet). */
  const scriptGroupTitle = (id: string, fallback: string): string =>
    id === "scripts" ? t("menu.scripts") : id === "force" ? t("menu.forced") : fallback;
  /** The menu's own name for a script id, for the confirmation's ordered list
   *  (the payload's labels, so the list names what the request names). */
  const scriptNames = new Map<number, string>();
  for (const g of generated.groups) for (const e of g.entries) if (!e.runAll) scriptNames.set(e.id, e.label);

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
            // track's own PAGE, the release's credits (or the single track's)
            // and the stored readout.
            title: "View",
            items: [
              {
                // The track's page, from the row that names it: the same
                // `trackRef` route the row's own title links to, so a listed
                // track no longer has to be found in its album page first.
                // Only ever offered for ONE track (`menuTrack`): a menu on
                // several paths has no single page to open, and the album
                // folder's own menu has its album page.
                label: t("menu.openTrackPage"),
                icon: ExternalLink,
                hidden: !menuTrack,
                title: t("menu.openTrackPageHint"),
                onClick: () => {
                  if (menuTrack) navigate(trackRef({ path: menuTrack }));
                },
              },
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
            // What the selection is FOR, next to what it IS: the same two
            // actions the album and track pages keep in their header row. The
            // row menu had neither, so "download this track" and "export this
            // album" both meant opening another page first (issue #50). Both
            // entries run the SHARED implementations — lib/offline for the
            // cache, the pages' own ExportDialog for the drive.
            title: "Files",
            items: [
              {
                label: "Download for offline playback",
                icon: ArrowDownToLine,
                disabled: !paths.length,
                title: "Cache the selection in this client for offline playback",
                onClick: () => {
                  void downloadForOffline(paths).then(() => {
                    // Every "downloaded" mark and the downloads page's own byte
                    // total read these two; both are stale the moment the cache
                    // changes (see DownloadButton.rescan).
                    qc.invalidateQueries({ queryKey: CACHED_PATHS_KEY });
                    qc.invalidateQueries({ queryKey: CACHED_SIZES_KEY });
                  });
                },
              },
              {
                label: "Export…",
                icon: FileOutput,
                disabled: !paths.length,
                title: "Transcode and save the selection to a drive",
                onClick: () => setExportOpen(true),
              },
              {
                // The music video of the one track this menu is on. It used to
                // be a film button beside that track's own row (the album page's
                // title cell), which read as a file mark rather than an action;
                // the release-wide version stays the album header's film button,
                // and the video overlay keeps its own while a video plays. All
                // three run the ONE implementation in lib/videoDownload.
                label: t("menu.downloadVideo"),
                icon: Film,
                hidden: !menuTrack,
                disabled: videoBusy,
                title: t("menu.downloadVideoHint"),
                onClick: () => void downloadVideoHere(),
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
          // The scripts THEMSELVES, generated from the registry
          // (GET /api/script-menu): every script that applies to what this
          // menu is ON, in the stack's own Run All order, each running over
          // the current selection through the same /api/run the Optimization
          // page uses. Nothing here types a script id — a script added to the
          // registry appears in this menu by itself, and one that does not
          // apply (an album-shaped script on a single track row) is not
          // offered. The group titles and the order are the server's.
          ...generated.groups.map((g, i) => ({
            title: scriptGroupTitle(g.id, g.title),
            // The applicable chain, as ONE entry at the head of the section —
            // the same place the Library page's script picker puts "Run all".
            items: [
              ...(i === 0 && generated.runAll ? [scriptItem(generated.runAll)] : []),
              ...g.entries.map(scriptItem),
            ],
          })),
          // The same scripts again, as the FORCED variant: the flag each one
          // owns, so work that was already done can be asked for again. Shown
          // only for the scripts that own a single flag, which is the set the
          // Optimization page's Force switch lists.
          ...(generated.forced
            ? [{
                title: scriptGroupTitle(generated.forced.id, generated.forced.title),
                items: generated.forced.entries.map(scriptItem),
              }]
            : []),
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
      {confirmRunAll && (
        // The app's confirmation idiom, the one `Apply fixes` uses: a Modal
        // that names what will happen, where Cancel, Escape and an outside
        // click all close it without a request, and only the confirm button
        // reaches /api/run. The list IS the request's ids, in the order it
        // posts them.
        <Modal
          onClose={() => setConfirmRunAll(null)}
          icon={Play}
          title={t("menu.runAllTitle", { count: confirmRunAll.ids.length })}
          width="max-w-[560px]"
          bodyClass="px-5 py-4 space-y-3"
          footer={
            <div className="flex justify-end gap-2">
              <button className="btn-ghost" onClick={() => setConfirmRunAll(null)}>
                {t("action.cancel")}
              </button>
              <button
                className="btn-primary"
                onClick={() => {
                  const e = confirmRunAll;
                  setConfirmRunAll(null);
                  void run(() => api.run(e.ids, e.targets), ran);
                }}
              >
                <Play className="h-3.5 w-3.5" /> {t("menu.runAll", { count: confirmRunAll.ids.length })}
              </button>
            </div>
          }
        >
          <p className="text-sm text-zinc-300 leading-relaxed">{t("menu.runAllHint")}</p>
          <ol className="text-[11px] font-mono text-zinc-400 max-h-56 overflow-y-auto space-y-0.5">
            {confirmRunAll.ids.map((id, i) => (
              <li key={id}>
                {i + 1}. {scriptNames.get(id) ?? `#${id}`}
              </li>
            ))}
          </ol>
        </Modal>
      )}
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
      {exportOpen && (
        <ExportDialog
          paths={paths}
          onClose={() => setExportOpen(false)}
          subtitle={albumPath ? "the selection" : "this track"}
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
