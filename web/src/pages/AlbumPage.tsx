import { Fragment, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ChevronDown, ChevronRight, CircleAlert, Play, Wand2, Trash2, FolderSync, FolderOpen, BarChart3, ImageUp, Image as ImageIcon, FileVideo, Disc3, CloudDownload, Sparkles, ListPlus, ListStart, ShieldCheck, FileMusic, ListChecks, Info as InfoIcon, X } from "lucide-react";
import { api } from "../api";
import { LinkChips, LinkEditorButton } from "../components/Links";
import { SubtitledVideo } from "../components/SubtitledVideo";
import { EmptyState, MediaChip, AdvisoryMark, GradeBadge, PageLoading } from "../components/Badges";
import CoverImg, { TrackCover } from "../components/CoverImg";
import CoverSearchModal from "../components/CoverSearchModal";
import FavHeart from "../components/FavHeart";
import { trackRef, entityLinkClick } from "../lib/refs";
import { invalidateLibrary } from "../lib/invalidate";
import { auditFails } from "../lib/status";
import { isVideoFile } from "../lib/fmt";
import BulkTagsDialog from "../components/BulkTagsDialog";
import OverflowMenu from "../components/OverflowMenu";
import StatsPanel from "../components/StatsPanel";
import TrackDetails from "../components/TrackDetails";
import { SortHeader, sortRows, toggleSort, groupByDisc, type SortState } from "../lib/sort.tsx";
import { ColumnsMenu, ColumnResizer, useColumnPrefs, useColumnWidths, ALBUM_TRACK_COLS, ALBUM_TRACK_COL_W } from "../lib/columns";
import { toast, useStore } from "../store";
import { fmtTech, albumTech } from "../lib/fmt";
import { fmtDuration } from "../lib/fmt";
import type { ExpectedTrack, Track } from "../types";

/** Whether a track file is a music video container (playable with <video>). */
export { isVideoFile };

export default function AlbumPage() {
  const { path = "" } = useParams();
  const decoded = decodeURIComponent(path);
  const { data, isLoading, error } = useQuery({
    queryKey: ["album", decoded],
    queryFn: () => api.album(decoded),
  });
  const { data: coverColor } = useQuery({
    queryKey: ["coverColor", decoded],
    queryFn: () => api.coverColor(decoded),
    retry: false,
  });
  const playNow = useStore((s) => s.playNow);
  const queue = useStore((s) => s.queue);
  const queueAdd = useStore((s) => s.queueAdd);
  const selection = useStore((s) => s.selection);
  const setSelection = useStore((s) => s.setSelection);
  const toggleTrack = useStore((s) => s.toggleTrack);
  const clearSelection = useStore((s) => s.clearSelection);
  const navigate = useNavigate();
  const [sort, setSort] = useState<SortState | null>(null);
  const [statsOpen, setStatsOpen] = useState(false);
  const [detailTrack, setDetailTrack] = useState<Track | null>(null);
  const [videoOpen, setVideoOpen] = useState<string | null>(null);
  const [remuxing, setRemuxing] = useState(false);
  const [coverSearchOpen, setCoverSearchOpen] = useState(false);
  const [beetsBusy, setBeetsBusy] = useState(false);
  const [lyricsBusy, setLyricsBusy] = useState(false);
  const [issuesOpen, setIssuesOpen] = useState(false);
  const [coverInfoOpen, setCoverInfoOpen] = useState(false);
  // track checkboxes (and the selection toolbar) only exist in select mode
  const [selectMode, setSelectMode] = useState(false);
  const [tagsDialogOpen, setTagsDialogOpen] = useState(false);
  // tracklist columns: visible set + drag-resized widths, persisted under the
  // SAME key the library's expanded album rows use — one tracklist, one prefs
  const [trackCols, toggleTrackCol] = useColumnPrefs("album-tracks", ALBUM_TRACK_COLS);
  const [trackW, setTrackW, resetTrackW] = useColumnWidths("album-tracks");
  const coverInput = useRef<HTMLInputElement>(null);
  const qc = useQueryClient();

  // Raw video files (VOB/MKV/...) in this album folder that the remuxer
  // could convert to MP4 — surfaced as a one-click action in the header.
  const { data: videosData, refetch: refetchVideos } = useQuery({
    queryKey: ["videos", decoded],
    queryFn: () => api.videosScan(decoded),
    retry: false,
  });
  const rawVideos = videosData?.videos ?? [];

  const convertVideos = async () => {
    setRemuxing(true);
    try {
      const res = await api.run([11], [decoded]);
      const r = res.results?.[0];
      if (r?.error) toast(`Remux failed: ${r.error}`);
      else toast(`Remuxed ${r?.stats?.converted ?? 0} video(s) to MKV`);
      qc.invalidateQueries({ queryKey: ["videos", decoded] });
      qc.invalidateQueries({ queryKey: ["library"] });
      qc.invalidateQueries({ queryKey: ["album", decoded] });
      refetchVideos();
    } catch (e) {
      toast(String(e));
    } finally {
      setRemuxing(false);
    }
  };

  const uploadCover = async (file: File) => {
    try {
      const r = await api.cover(decoded, file);
      toast(r.ok ? `Cover saved as ${r.path.split("/").pop()}` : "Cover upload failed");
      qc.invalidateQueries({ queryKey: ["library"] });
      qc.invalidateQueries({ queryKey: ["coverColor", decoded] });
      qc.invalidateQueries({ queryKey: ["album", decoded] });
    } catch (e) {
      toast(String(e));
    } finally {
      if (coverInput.current) coverInput.current.value = "";
    }
  };

  if (error)
    return (
      <EmptyState
        title="Album not found"
        hint="The folder may have been renamed, moved or deleted."
        action={{ label: "Back to the library", to: "/library" }}
      />
    );
  if (isLoading || !data) return <PageLoading label="Loading album…" />;

  const tracks = sortRows(data.tracks, sort);
  // highest disc number across the album (filename fallback included) —
  // drives the "N discs" note in the header and the Disc rows below
  const maxDisc = data.tracks.reduce((m, t) => Math.max(m, t.discnumber ?? 1), 1);
  // One condensed verdict: grading problems OR a FAKE/Mix audit → FAIL.
  const verdictPass = !!data.pass && !auditFails(data.audit_summary);
  const issueEntries = Object.entries(data.issues ?? {});
  const verdictTrack = (tr: Track) => !!tr.grade_pass && !auditFails(tr.audit);

  const runScripts = async (ids: number[]) => {
    await api.run(ids, [data.path]);
    // scripts rewrite tags in place — the album payload (tags, grading,
    // covers) is stale until the shared invalidation runs
    invalidateLibrary(qc);
  };

  // One shared square icon-button style for the header action row — play is
  // the only accent-filled button, everything else stays quiet and boxed.
  const iconBtn =
    "p-2 rounded-lg border border-border bg-panel/60 text-zinc-400 hover:text-white hover:bg-raise transition-colors";

  /** The library artist page for this album's artist: by album-artist MBID
   * when tagged, else the artist folder (the album's parent directory). */
  const artistHref = data.meta?.MUSICBRAINZ_ALBUMARTISTID
    ? `/artist/mb:${data.meta.MUSICBRAINZ_ALBUMARTISTID}`
    : `/artist/${encodeURIComponent(data.path.split(/[\\/]/).slice(0, -1).join("/"))}`;

  // Tracks of THIS album that are ticked in the global selection.
  const selectedHere = data.tracks.filter((t) => selection.tracks.includes(t.path));

  // Quick-select (select mode): everything on the album, or one disc's
  // tracks at a time. Selection is global, so merge / subtract by path.
  const discGroups = groupByDisc(tracks);
  const selectAllHere = () =>
    setSelection({ tracks: [...new Set([...selection.tracks, ...data.tracks.map((t) => t.path)])] });
  const selectDiscHere = (disc: number | null) => {
    const g = discGroups.find((x) => (x.disc ?? null) === (disc ?? null));
    if (!g) return;
    setSelection({ tracks: [...new Set([...selection.tracks, ...g.tracks.map((t) => t.path)])] });
  };
  const selectNoneHere = () =>
    setSelection({ tracks: selection.tracks.filter((p) => !data.tracks.some((t) => t.path === p)) });

  const queueTracks = data.tracks.map((t) => ({
    path: t.path, file: t.file, albumPath: data.path,
    artist: data.meta?.ALBUMARTIST ?? data.meta?.ARTIST ?? undefined,
    album: data.meta?.ALBUM ?? undefined, title: t.tags.TITLE || undefined,
    coverFile: t.cover_file ?? null, albumCover: data.cover_file ?? null,
  }));

  /** Append the album to the queue; an empty queue just starts playing. */
  const enqueue = (position: "next" | "end") => {
    if (!queue.length) {
      playNow(queueTracks);
      return;
    }
    queueAdd(queueTracks, position);
    toast(position === "next" ? `Playing ${queueTracks.length} track(s) next` : `Added ${queueTracks.length} track(s) to the queue`);
  };

  const playSelection = () => {
    if (!selectedHere.length) return;
    playNow(
      selectedHere.map((t) => ({
        path: t.path, file: t.file, albumPath: data.path,
        artist: data.meta?.ALBUMARTIST ?? data.meta?.ARTIST ?? undefined,
        album: data.meta?.ALBUM ?? undefined, title: t.tags.TITLE || undefined,
        coverFile: t.cover_file ?? null, albumCover: data.cover_file ?? null,
      }))
    );
  };

  const addSelectionToPlaylist = async () => {
    if (!selectedHere.length) return;
    const pls = await api.playlists();
    const manual = pls.find((p) => p.kind === "manual");
    const target = manual ?? (await api.createPlaylist("Library selection", "manual"));
    await api.playlistAdd(target.id, selectedHere.map((t) => t.path));
    toast(`Added ${selectedHere.length} track(s) to playlist`);
  };

  /** Pull the top-voted MusicBrainz genres (count from Settings → Import)
   * onto every track of this album, using the linked MB release. */
  const importGenres = async () => {
    try {
      const r = await api.mbGenresWrite(data.tracks.map((t) => t.path));
      toast(r.updated
        ? `Imported ${r.genres.join(", ") || "genres"} on ${r.updated} track(s)${r.per_track ? " (per-track where available)" : ""}`
        : "No genres found on the linked MusicBrainz release");
      invalidateLibrary(qc);
    } catch (e) {
      toast(String(e));
    }
  };

  /** Browse/download the raw file or a transcoded export of this album's
   * tracks — handled per-track from the track page & details panel. */

  const removeAlbum = async () => {
    if (!window.confirm(`Remove "${data.meta?.ALBUM ?? data.path.split("/").pop()}" from the library?\nIt moves to .mlo/trash in your music folder (recoverable).`)) return;
    try {
      await api.removeAlbum(data.path);
      toast("Album moved to trash");
      qc.invalidateQueries({ queryKey: ["library"] });
      navigate("/");
    } catch (e) {
      toast(String(e));
    }
  };

  const organizeAlbum = async () => {
    if (!window.confirm("Organize this album with the naming script from Settings?\nFiles are MOVED into the scripted folder structure.")) return;
    try {
      const r = await api.organize([data.path]);
      const res = r.results[0];
      if (res.error) {
        toast(`Organize failed: ${res.error}`);
        return;
      }
      toast(`Organized — ${res.moved} file(s) moved, ${res.leftovers} sidecar(s)`);
      qc.invalidateQueries({ queryKey: ["library"] });
      navigate("/");
    } catch (e) {
      toast(String(e));
    }
  };

  const beetsTagAlbum = async () => {
    if (!window.confirm("Tag this album with beets (MusicBrainz match + Picard-parity plugin)?\nFiles are moved into the scripted folder structure.")) return;
    setBeetsBusy(true);
    try {
      const r = await api.beetsImport([data.path]);
      toast(`Beets import done${r.organized ? " (re-organized)" : ""}`);
      invalidateLibrary(qc);
      qc.invalidateQueries({ queryKey: ["coverColor", decoded] });
    } catch (e) {
      toast(String(e));
    } finally {
      setBeetsBusy(false);
    }
  };

  /** Download missing lyrics from LRCLIB for every track in this album,
   * writing per the global lyrics_format (EMBEDDED / LRC / BOTH). */
  const downloadLyricsAlbum = async () => {
    setLyricsBusy(true);
    try {
      const cfg = await api.config();
      const fmt = String(cfg.lyrics_format ?? "EMBEDDED").toUpperCase();
      let fetched = 0;
      let skipped = 0;
      let missing = 0;
      for (const t of data.tracks) {
        if (t.tags.INSTRUMENTAL === "1" || t.lyrics_present) {
          skipped++;
          continue;
        }
        const artist = t.tags.ARTIST || data.meta?.ALBUMARTIST || data.meta?.ARTIST || undefined;
        const title = t.tags.TITLE;
        if (!artist || !title) {
          skipped++;
          continue;
        }
        try {
          const res = await api.lyricsGet(artist, title, (t.tags.ALBUM ?? data.meta?.ALBUM) || undefined, t.tech?.length ? Math.round(t.tech.length) : undefined);
          const lrc = res?.syncedLyrics ?? res?.plainLyrics;
          if (!lrc) {
            missing++;
          } else {
            if (fmt === "LRC" || fmt === "BOTH") await api.lyricsWrite(t.path, lrc);
            if (fmt === "EMBEDDED" || fmt === "BOTH") await api.lyricsEmbed(t.path, lrc);
            fetched++;
          }
        } catch {
          missing++;
        }
        await new Promise((r) => setTimeout(r, 350)); // LRCLIB rate-limit pacing
      }
      toast(`Lyrics: ${fetched} downloaded · ${skipped} skipped · ${missing} not found`);
      invalidateLibrary(qc);
    } catch (e) {
      toast(String(e));
    } finally {
      setLyricsBusy(false);
    }
  };

  return (
    <>
      {/* ambient blurred album cover behind the whole page */}
      <div className="fixed inset-0 z-0 pointer-events-none overflow-hidden" aria-hidden>
        <CoverImg
          albumPath={data.path}
          coverFile={data.cover_file}
          wrapperClass="w-full h-full blur-[90px] opacity-25 scale-125"
        />
        <div className="absolute inset-0 bg-bg/50" />
      </div>
      <div className="relative z-10 p-6 space-y-6">
      <div
        className="rounded-xl p-5 relative"
        style={
          coverColor
            ? { background: `linear-gradient(135deg, ${coverColor}22 0%, transparent 60%)` }
            : undefined
        }
      >
        <div className="flex items-start gap-5">
          <div className="shrink-0 relative group/cover">
            <CoverImg
              albumPath={data.path}
              coverFile={data.cover_file}
              wrapperClass="h-56 w-56 rounded-xl bg-raise overflow-hidden shadow-2xl ring-1 ring-black/40"
            />
            <input
              ref={coverInput}
              type="file"
              accept="image/*"
              className="hidden"
              onChange={(e) => e.target.files?.[0] && uploadCover(e.target.files[0])}
            />
            {/* small square menu over the cover: upload / find online / info.
                The button and panel keep an opaque backdrop so they stay
                readable on any cover. */}
            <div className="absolute top-1.5 right-1.5 opacity-0 group-hover/cover:opacity-100 transition-opacity">
              <OverflowMenu
                buttonClass="!p-1.5 bg-black/80 hover:bg-black border border-white/20 text-zinc-100"
                buttonTitle="Cover art actions"
                sections={[
                  {
                    items: [
                      { label: "Cover info", icon: InfoIcon, onClick: () => setCoverInfoOpen(true) },
                      { label: "Upload cover…", icon: ImageUp, onClick: () => coverInput.current?.click() },
                      { label: "Find cover online", icon: ImageIcon, onClick: () => setCoverSearchOpen(true) },
                    ],
                  },
                ]}
              />
            </div>
          </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2.5 min-w-0">
            <h1 className="text-3xl font-bold tracking-tight truncate">{data.meta?.ALBUM ?? data.path.split("/").pop()}</h1>
            <AdvisoryMark value={data.meta?.ITUNESADVISORY ?? data.meta?.ALBUMITUNESADVISORY} size="md" />
            {/* verdict sits right of the title — click for the problems */}
            <button
              className={`h-2 w-2 rounded-full shrink-0 transition-opacity ${verdictPass ? "bg-emerald-500/70" : "bg-red-500/80"}`}
              title={verdictPass ? `Pass — ${data.grade_pct ?? "?"}% of checks` : `Fail — ${data.grade_pct ?? "?"}% · ${issueEntries.length} problem type(s)`}
              onClick={() => setIssuesOpen(!issuesOpen)}
            />
            {/* MusicBrainz / RateYourMusic identity links, right of the title:
                exactly one of each — prefer the release over its group */}
            <LinkChips
              tags={(data.meta ?? {}) as Record<string, unknown>}
              only={[
                ...(data.meta?.MUSICBRAINZ_ALBUMID ? [] : ["MUSICBRAINZ_RELEASEGROUPID"]),
                "MUSICBRAINZ_ALBUMID",
                "RATEYOURMUSIC_ALBUM",
              ]}
            />
          </div>
          {/* artist opens the library's artist page */}
          <Link
            to={artistHref}
            className="text-zinc-400 mt-1 hover:text-accent-soft transition-colors w-fit"
            title="Open the artist page"
          >
            {data.meta?.ALBUMARTIST ?? data.meta?.ARTIST ?? "—"}
          </Link>
          {/* release dates under the title: original and release shown
              separately whenever they differ */}
          <div className="flex flex-wrap items-center gap-x-4 gap-y-0.5 mt-1.5 text-xs">
            <span className="text-zinc-500" title={data.meta?.ORIGINALDATE ?? undefined}>
              <span className="text-zinc-600 uppercase tracking-wider text-[10px] mr-1.5">Original</span>
              {data.meta?.ORIGINALDATE ?? "—"}
            </span>
            <span className="text-zinc-500" title={data.meta?.DATE ?? undefined}>
              <span className="text-zinc-600 uppercase tracking-wider text-[10px] mr-1.5">Released</span>
              {data.meta?.DATE ?? "—"}
            </span>
          </div>
          {(data.meta?.LABEL || data.meta?.CATALOGNUMBER) && (
            <div className="text-xs text-zinc-500 mt-0.5 truncate">
              {[data.meta?.LABEL, data.meta?.CATALOGNUMBER].filter(Boolean).join(" · ")}
            </div>
          )}
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <MediaChip media={data.media} />
            {/* aggregated codec / bitrate / depth-rate across the album's tracks */}
            {albumTech(data.tracks) && (
              <span
                className="chip bg-zinc-800/70 text-zinc-300 border border-border font-mono"
                title="Codec · bitrate · bit depth/sample rate across this album's tracks"
              >
                {albumTech(data.tracks)}
              </span>
            )}
            {/* album-level dynamic range, labeled ADR (vs the per-track
                DR column) — read from the ALBUM DYNAMIC RANGE tag the
                DR script writes on every track of the album */}
            {data.meta?.["ALBUM DYNAMIC RANGE"] && (
              <span
                className="chip bg-zinc-800/70 text-zinc-300 border border-border font-mono"
                title="Album dynamic range (DR meter)"
              >
                ADR {data.meta["ALBUM DYNAMIC RANGE"]}
              </span>
            )}
            {/* disc count lives in the header too, so multi-disc albums
                announce themselves before the tracklist */}
            {maxDisc > 1 && (
              <span className="text-xs text-zinc-500">
                {maxDisc} disc{maxDisc === 1 ? "" : "s"}
              </span>
            )}
          </div>
          {issueEntries.length > 0 && (
            <div className="mt-2.5">
              <button
                className="inline-flex items-center gap-1.5 text-xs text-red-400/80 hover:text-red-300"
                onClick={() => setIssuesOpen(!issuesOpen)}
              >
                <CircleAlert className="h-3.5 w-3.5" />
                {issueEntries.length} problem{issueEntries.length === 1 ? "" : "s"} to fix
                {issuesOpen ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
              </button>
              {issuesOpen && (
                <div className="mt-1.5 max-w-3xl rounded-lg border border-red-900/40 bg-red-950/20 p-1.5 space-y-0.5">
                  {issueEntries.map(([text, files]) => (
                    <div key={text} className="rounded-md px-2 py-1.5 hover:bg-red-950/40">
                      <div className="text-xs text-red-200 flex items-start gap-1.5">
                        <CircleAlert className="h-3 w-3 mt-0.5 shrink-0 text-red-400" />
                        <span>{text}</span>
                        <span className="ml-auto text-[10px] text-zinc-500 shrink-0">{files.length === 1 && files[0] === "album" ? "whole album" : `${files.length} file(s)`}</span>
                      </div>
                      {files[0] !== "album" && (
                        <div className="text-[10px] text-zinc-500 mt-0.5 pl-[18px]">
                          {files.length > 6 ? `${files.slice(0, 6).join(" · ")} · +${files.length - 6} more` : files.join(" · ")}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
          {/* bottom action row: play + every primary button (identity links
              live next to the album title now) */}
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <button
              className="btn-primary !p-2.5 !rounded-md"
              onClick={() => playNow(queueTracks)}
              title="Play the album from the top"
              aria-label="Play album"
            >
              <Play className="h-4 w-4 fill-current" />
            </button>
            <LinkEditorButton
              mode="album"
              paths={data.tracks.map((t) => t.path)}
              current={(data.meta ?? {}) as Record<string, unknown>}
              iconOnly
            />
            <FavHeart
              kind="album"
              id={data.path}
              mbid={data.meta?.MUSICBRAINZ_ALBUMID}
              className="!p-2 !rounded-md border border-border bg-panel/60 hover:!bg-raise"
              iconClass="h-4 w-4"
            />
            <OverflowMenu
              buttonClass={iconBtn}
              buttonTitle="All album actions"              sections={[
                {
                  items: [
                    { label: "Play next", icon: ListStart, onClick: () => enqueue("next") },
                    { label: "Add to queue", icon: ListPlus, onClick: () => enqueue("end") },
                  ],
                },
                {
                  title: "Album",
                  items: [
                    { label: "Import & link", icon: Wand2, onClick: () => navigate(`/import?album=${encodeURIComponent(data.path)}`) },
                    { label: "Organize (naming script)", icon: FolderSync, onClick: organizeAlbum },
                    { label: "Open folder", icon: FolderOpen, onClick: async () => { try { await api.openFolder(data.path); } catch (e) { toast(String(e)); } } },
                    { label: "Stats", icon: BarChart3, onClick: () => setStatsOpen(true) },
                  ],
                },
                {
                  title: "Lyrics",
                  items: [
                    { label: lyricsBusy ? "Fetching…" : "Download missing (LRCLIB)", icon: CloudDownload, onClick: downloadLyricsAlbum, disabled: lyricsBusy },
                  ],
                },
                {
                  title: "Tags & scripts",
                  items: [
                    { label: beetsBusy ? "Beets…" : "Tag with beets", icon: Disc3, onClick: beetsTagAlbum, disabled: beetsBusy },
                    { label: "Import genres (MusicBrainz)", icon: Sparkles, onClick: importGenres },
                    { label: "Format lyrics", icon: FileMusic, onClick: () => runScripts([1]) },
                    { label: "Format CUEs", icon: FileMusic, onClick: () => runScripts([2]) },
                    { label: "Optimize FLACs", icon: FileMusic, onClick: () => runScripts([3]) },
                    { label: "Process images", icon: FileMusic, onClick: () => runScripts([5]) },
                    { label: "Audit", icon: ShieldCheck, onClick: () => runScripts([6]) },
                    { label: "DR & ReplayGain", icon: FileMusic, onClick: () => runScripts([7]) },
                    { label: "Auto tagging", icon: FileMusic, onClick: () => runScripts([8]) },
                    { label: "Grade", icon: FileMusic, onClick: () => runScripts([4]) },
                    { label: "Remux videos", icon: FileVideo, hidden: rawVideos.length === 0, onClick: convertVideos, disabled: remuxing },
                  ],
                },
                {
                  items: [
                    { label: "Remove album (to trash)", icon: Trash2, danger: true, onClick: removeAlbum },
                  ],
                },
              ]}
            />
          </div>
        </div>
        </div>
      </div>

      {statsOpen && (
        <StatsPanel
          title={data.meta?.ALBUM ?? data.path.split("/").pop() ?? "album"}
          albums={[data]}
          tracks={data.tracks}
          onClose={() => setStatsOpen(false)}
        />
      )}

      {detailTrack && (
        <TrackDetails track={detailTrack} albumPath={data.path} onClose={() => setDetailTrack(null)} />
      )}

      {videoOpen && (
        <div className="fixed inset-0 z-50 bg-black/85 flex items-center justify-center p-6" onClick={() => setVideoOpen(null)}>
          <div className="w-full max-w-4xl" onClick={(e) => e.stopPropagation()}>
            <SubtitledVideo path={videoOpen} className="w-full max-h-[80vh] rounded-lg border border-border bg-black" />
            <div className="flex justify-end mt-2">
              <button className="btn-ghost !py-1" onClick={() => setVideoOpen(null)}>Close</button>
            </div>
          </div>
        </div>
      )}

      {coverInfoOpen && (
        <CoverInfoModal
          albumPath={data.path}
          coverFile={data.cover_file}
          onClose={() => setCoverInfoOpen(false)}
        />
      )}

      {(selectMode || selectedHere.length > 0) && (
        <div className="flex items-center gap-2 bg-accent/15 border border-accent/40 rounded-lg px-3 py-2 flex-wrap">
          {selectMode && (
            <>
              <span className="text-xs text-zinc-400">Select:</span>
              <button className="btn-ghost !py-0.5 text-xs" onClick={selectAllHere}>
                All
              </button>
              {discGroups.length > 1 &&
                discGroups.map((g) => (
                  <button key={g.disc ?? 0} className="btn-ghost !py-0.5 text-xs" onClick={() => selectDiscHere(g.disc ?? null)}>
                    {g.disc === null ? "Unnumbered" : `Disc ${g.disc}`}
                  </button>
                ))}
              <button className="btn-ghost !py-0.5 text-xs" onClick={selectNoneHere}>
                None
              </button>
              <span className="w-px h-4 bg-border mx-0.5" />
            </>
          )}
          <span className="text-xs font-medium text-accent-soft">
            {selectedHere.length} track{selectedHere.length === 1 ? "" : "s"} selected
          </span>
          <div className="ml-auto flex gap-1.5 flex-wrap">
            <button className="btn-primary !py-1 text-xs" onClick={playSelection} disabled={selectedHere.length === 0}>
              <Play className="h-3.5 w-3.5" /> Play selection
            </button>
            <button className="btn-ghost !py-1 text-xs" onClick={addSelectionToPlaylist} disabled={selectedHere.length === 0}>
              Playlist
            </button>
            <button className="btn-ghost !py-1 text-xs" onClick={() => setTagsDialogOpen(true)} disabled={selectedHere.length === 0} title="Bulk remove or set tags on the selected tracks">
              Tags
            </button>
            <button className="btn-ghost !py-1 text-xs" onClick={() => clearSelection()} disabled={selectedHere.length === 0}>
              Clear
            </button>
          </div>
        </div>
      )}

      {tagsDialogOpen && (
        <BulkTagsDialog
          paths={selectedHere.map((t) => t.path)}
          onClose={() => setTagsDialogOpen(false)}
        />
      )}

      <div>
        <table className="w-full text-sm">
          <thead className="border-b border-border">
            <tr>
              {selectMode && <th className="th w-10"></th>}
              {ALBUM_TRACK_COLS.filter((c) => trackCols.includes(c.id)).map((c) =>
                c.id === "cover" ? (
                  <th key={c.id} className={`th relative ${ALBUM_TRACK_COL_W[c.id] ?? ""}`} title="Cover art">
                    <span className="sr-only">Cover</span>
                  </th>
                ) : (
                  <SortHeader key={c.id} label={c.label} sort={sort} sortKey={c.sortKey} onSort={(k) => setSort(toggleSort(sort, k))}
                    className={`relative ${ALBUM_TRACK_COL_W[c.id] ?? ""}${c.id === "num" ? " cell-nowrap" : ""}`}
                    style={trackW[c.id] ? { width: trackW[c.id] } : undefined}>
                    <ColumnResizer width={trackW[c.id]} onDrag={(w) => setTrackW(c.id, w)} onReset={resetTrackW} />
                  </SortHeader>
                )
              )}
              {/* corner: select-mode toggle + the columns chooser, docked in
                  the grid's top-right so they cost no row of their own */}
              <th className="th relative px-1 w-[4.75rem]">
                <div className="flex items-center justify-end gap-0.5">
                  <button
                    className={`p-1.5 rounded-lg transition-colors ${
                      selectMode ? "text-accent bg-raise" : "text-zinc-500 hover:text-white hover:bg-raise"
                    }`}
                    onClick={() =>
                      setSelectMode((v) => {
                        if (v) clearSelection();
                        return !v;
                      })
                    }
                    title="Select tracks — show checkboxes for batch actions"
                    aria-label="Select tracks"
                  >
                    <ListChecks className="h-4 w-4" />
                  </button>
                  <ColumnsMenu
                    iconOnly
                    cols={ALBUM_TRACK_COLS}
                    visible={trackCols}
                    onToggle={toggleTrackCol}
                    title="Tracklist columns"
                    onResetWidths={resetTrackW}
                    hasCustomWidths={Object.keys(trackW).length > 0}
                  />
                </div>
              </th>
            </tr>
          </thead>
          <tbody>
            {(() => {
              const groups = groupByDisc(tracks);
              const multiDisc = groups.length > 1;
              // Tracks the RELEASE has but this folder does not — a PARTIAL
              // import. They render in release order, greyed out and inert
              // (there is no file to play, select or grade), so the tracklist
              // reads as the album rather than as the files that happen to be
              // present. Empty entirely for a full import.
              const missingByDisc = new Map<number, ExpectedTrack[]>();
              for (const e of data.expected_tracks ?? []) {
                if (!e.missing) continue;
                const d = e.disc || 1;
                const list = missingByDisc.get(d);
                if (list) list.push(e);
                else missingByDisc.set(d, [e]);
              }
              // A disc every one of whose tracks is absent has no group of its
              // own, so it gets one made from the missing rows alone.
              const emptyDiscs = [...missingByDisc.keys()]
                .filter((d) => !groups.some((g) => (g.disc ?? 1) === d))
                .sort((a, b) => a - b);
              return (
                <>
                  {groups.map((g) => (
                <Fragment key={g.disc ?? 0}>
                  {g.tracks.map((tr) => (
              <tr
                key={tr.path}
                className="table-row group cursor-pointer"
                title={selectMode ? "Click to select" : "Click to play"}
                onClick={selectMode ? () => toggleTrack(tr.path) : () =>
                  playNow(
                    tracks.map((t) => ({
                      path: t.path, file: t.file, albumPath: data.path,
                      artist: data.meta?.ALBUMARTIST ?? data.meta?.ARTIST ?? undefined,
                      album: data.meta?.ALBUM ?? undefined, title: t.tags.TITLE || undefined,
                      coverFile: t.cover_file ?? null, albumCover: data.cover_file ?? null,
                    })),
                    tracks.findIndex((t) => t.path === tr.path)
                  )
                }
              >
                {selectMode && (
                  <td className="td pr-0" onClick={(e) => e.stopPropagation()}>
                    <input
                      type="checkbox"
                      checked={selection.tracks.includes(tr.path)}
                      onChange={() => toggleTrack(tr.path)}
                    />
                  </td>
                )}
                {trackCols.includes("num") && (
                  <td className="td text-zinc-500 cell-nowrap">
                    {/* multi-disc albums number tracks D-TT (2-1, 2-2, …) */}
                    <span className="tabular-nums">
                      {multiDisc ? `${g.disc}-${tr.tracknumber ?? tr.tags.TRACKNUMBER ?? "?"}` : tr.tracknumber ?? tr.tags.TRACKNUMBER ?? "—"}
                    </span>
                  </td>
                )}
                {trackCols.includes("cover") && (
                  <td className="td cell-cover pr-0">
                    <TrackCover
                      albumPath={data.path}
                      trackCover={tr.cover_file}
                      albumCover={data.cover_file}
                      wrapperClass="h-8 w-8 rounded bg-raise border border-border overflow-hidden shrink-0"
                    />
                  </td>
                )}
                {trackCols.includes("title") && (
                  <td className="td">
                    <div className="flex items-center gap-1.5 min-w-0">
                      <Link
                        to={trackRef(tr)}
                        className="hover:text-accent-soft break-words min-w-0"
                        title="Click to play · Ctrl-click to open track page"
                        onClick={(e) => entityLinkClick(e, () => navigate(trackRef(tr)))}
                      >
                        {tr.tags.TITLE ?? tr.file}
                      </Link>
                      {!!tr.issues?.length && (
                        <button
                          className="text-[9px] text-red-400/70 shrink-0 hover:text-red-300"
                          title={`${tr.issues.join("\n")}\nClick for details`}
                          onClick={(e) => {
                            e.stopPropagation();
                            setDetailTrack(tr);
                          }}
                        >
                          {tr.issues.length}✗
                        </button>
                      )}
                      <GradeBadge pass={verdictTrack(tr)} audit={tr.audit} size="sm" />
                      <AdvisoryMark value={tr.tags.ITUNESADVISORY} />
                      {(tr.is_video || isVideoFile(tr.file)) && (
                        <button
                          className="text-zinc-500 hover:text-white shrink-0"
                          title="Watch music video"
                          onClick={(e) => {
                            e.stopPropagation();
                            setVideoOpen(tr.path);
                          }}
                        >
                          <FileVideo className="h-3.5 w-3.5" />
                        </button>
                      )}
                      <span className="row-hover shrink-0" onClick={(e) => e.stopPropagation()}>
                        <FavHeart kind="track" id={tr.path} mbid={tr.tags.MUSICBRAINZ_TRACKID} iconClass="h-3.5 w-3.5" title={undefined} />
                      </span>
                    </div>
                  </td>
                )}
                {trackCols.includes("genre") && <td className="td text-zinc-500 break-words">{tr.tags.GENRE ?? "—"}</td>}
                {trackCols.includes("dur") && <td className="td text-zinc-500">{fmtDuration(tr.tech.length)}</td>}
                {trackCols.includes("bitrate") && (
                  <td className="td text-zinc-500">
                    {tr.tech.bitrate || tr.tech.bits_per_sample ? fmtTech(tr.tech) : "—"}
                  </td>
                )}
                {trackCols.includes("dr") && (
                  <td className="td text-zinc-500 tabular-nums" title={`Dynamic range${tr.tags["ALBUM DYNAMIC RANGE"] ? ` · album ${tr.tags["ALBUM DYNAMIC RANGE"]}` : ""}`}>
                    {tr.tags["DYNAMIC RANGE"] ?? "—"}
                  </td>
                )}
              </tr>
                  ))}
                  {missingByDisc.get(g.disc ?? 1)?.map((e) => (
                    <MissingTrackRow key={`missing-${e.disc}-${e.position}`} e={e} />
                  ))}
                </Fragment>
                  ))}
                  {emptyDiscs.map((d) => (
                    <Fragment key={`missing-disc-${d}`}>
                      {missingByDisc.get(d)!.map((e) => (
                        <MissingTrackRow key={`missing-${e.disc}-${e.position}`} e={e} />
                      ))}
                    </Fragment>
                  ))}
                </>
              );
            })()}
          </tbody>
        </table>
      </div>

      {coverSearchOpen && (
        <CoverSearchModal
          albumPath={data.path}
          artist={data.meta?.ALBUMARTIST ?? data.meta?.ARTIST ?? ""}
          album={data.meta?.ALBUM ?? ""}
          onClose={() => setCoverSearchOpen(false)}
          onApplied={() => {
            qc.invalidateQueries({ queryKey: ["library"] });
            qc.invalidateQueries({ queryKey: ["coverColor", decoded] });
            qc.invalidateQueries({ queryKey: ["album", decoded] });
          }}
        />
      )}
      </div>

    </>
  );
}
/** One release track that this folder does not contain — a PARTIAL import.
 *  Deliberately inert: there is no file behind it, so it cannot be played,
 *  selected, graded or linked. Greyed out so the album still reads as a whole
 *  tracklist with visible holes rather than silently hiding what is absent. */
function MissingTrackRow({ e }: { e: ExpectedTrack }) {
  return (
    <tr
      className="opacity-40 select-none"
      title="On the MusicBrainz release but not in this folder — import the missing track to fill this in"
    >
      <td className="td cell-nowrap text-zinc-600 tabular-nums" colSpan={16}>
        <span className="inline-flex items-center gap-2">
          <span className="w-10 shrink-0 font-mono">
            {e.disc ? `${e.disc}.${String(e.position).padStart(2, "0")}` : String(e.position)}
          </span>
          <span className="italic">{e.title || "Untitled"}</span>
          <span className="chip text-[9px] bg-raise border border-border text-zinc-500">not imported</span>
        </span>
      </td>
    </tr>
  );
}

/** Cover info dialog: resolution, aspect ratio, format and byte size of the
 * album's cover art (the "Cover info" item in the cover's … menu). */
function CoverInfoModal({ albumPath, coverFile, onClose }: {
  albumPath: string;
  coverFile: string | null;
  onClose: () => void;
}) {
  const { data, isLoading } = useQuery({
    queryKey: ["coverInfo", albumPath, coverFile],
    queryFn: () => api.coverInfo(albumPath, coverFile),
  });
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const rows: [string, string | null | undefined][] = data
    ? [
      ["File", data.file],
      ["Resolution", data.width && data.height ? `${data.width} × ${data.height} px` : null],
      ["Aspect ratio", data.aspect ? `${data.aspect}${data.aspect_label && data.aspect_label !== data.aspect ? ` (${data.aspect_label})` : ""}` : null],
      ["Megapixels", data.megapixels ? `${data.megapixels} MP` : null],
      ["Format", data.format],
      ["File size", `${(data.bytes / 1024).toFixed(0)} kB (${(data.bytes / 1024 / 1024).toFixed(2)} MB)`],
    ]
    : [];
  return (
    <div className="fixed inset-0 z-[60] bg-black/70 flex items-center justify-center p-6" onClick={onClose}>
      <div
        className="bg-card border border-border rounded-xl p-5 w-[380px] shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-3 mb-3">
          <div>
            <div className="text-sm font-semibold">Cover info</div>
            <div className="text-[11px] text-zinc-500 mt-0.5">Image details for this album's artwork</div>
          </div>
          <button className="p-1.5 rounded-lg hover:bg-raise text-zinc-400 hover:text-white" onClick={onClose} title="Close (Esc)">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="rounded-lg overflow-hidden border border-border mb-3">
          <CoverImg albumPath={albumPath} coverFile={coverFile} wrapperClass="aspect-square w-full bg-raise" />
        </div>
        {isLoading ? (
          <div className="text-xs text-zinc-500 py-3 text-center">Reading image…</div>
        ) : !data ? (
          <div className="text-xs text-zinc-500 py-3 text-center">Cover details unavailable.</div>
        ) : (
          <div className="space-y-1.5">
            {rows.map(([k, v]) =>
              v != null && v !== "" ? (
                <div key={k} className="flex items-baseline gap-3 text-xs">
                  <span className="text-zinc-500 uppercase tracking-wider text-[10px] w-24 shrink-0">{k}</span>
                  <span className="text-zinc-200 font-mono truncate" title={String(v)}>{v}</span>
                </div>
              ) : null
            )}
          </div>
        )}
      </div>
    </div>
  );
}
