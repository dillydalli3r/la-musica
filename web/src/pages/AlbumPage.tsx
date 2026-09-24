import { Fragment, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ChevronDown, ChevronRight, CircleAlert, Play, Wand2, Trash2, FolderSync, FolderOpen, BarChart3, ImageUp, Image as ImageIcon, FileVideo, Film, Disc3, CloudDownload, Sparkles, ListPlus, ListStart, ShieldCheck, FileMusic, ListChecks, Info as InfoIcon, Loader2, Pencil, RefreshCw, Users } from "lucide-react";
import { api } from "../api";
import { LinkChips, LinkEditorButton } from "../components/Links";
import { SubtitledVideo } from "../components/SubtitledVideo";
import { EmptyState, AdvisoryMark, CachedMark, GradeBadge, PageLoading, PendingMark, pendingSummary, mediaCountryLabel } from "../components/Badges";
import CoverImg, { TrackCover } from "../components/CoverImg";
import CoverSearchModal from "../components/CoverSearchModal";
import Description from "../components/Description";
import DownloadButton from "../components/DownloadButton";
import { ExportButton } from "../components/ExportDialog";
import FavHeart from "../components/FavHeart";
import TrackTitleCell from "../components/TrackTitleCell";
import { trackRef, entityLinkClick } from "../lib/refs";
import { invalidateLibrary } from "../lib/invalidate";
import { auditFails } from "../lib/status";
import { useAcquisitions } from "../lib/acquisition";
import { isVideoFile } from "../lib/fmt";
import { SCRIPT_LABEL } from "../lib/scripts";
import BulkTagsDialog from "../components/BulkTagsDialog";
import Modal from "../components/Modal";
import MoreLikeThis from "../components/MoreLikeThis";
import OnlineRecommendations from "../components/OnlineRecommendations";
import LockedChip from "../components/LockedChip";
import { useLockWhy } from "../lib/locks";
import OverflowMenu from "../components/OverflowMenu";
import PageHeader from "../components/PageHeader";
import StarRating from "../components/StarRating";
import { ratingOf, useRatings, useSetRating, FOLDER_RATING_NOTE } from "../lib/ratings";
import TagActionsMenu, { TrackActionsMenu } from "../components/TagActionsMenu";
import StatsPanel from "../components/StatsPanel";
import TrackDetails, { CreditsPanel, creditTagsFrom } from "../components/TrackDetails";
import { AlbumDetails } from "../components/AlbumDetails";
import { SortHeader, sortRows, toggleSort, groupByDisc, type SortState } from "../lib/sort.tsx";
import { ColumnsMenu, ColumnResizer, useColumnPrefs, useColumnWidths, useCustomColumns, customCols, customColValue, ALBUM_TRACK_COLS, ALBUM_TRACK_COL_W, ALBUM_TRACK_MIN_W, TAG_COL_W, type Col } from "../lib/columns";
import { useI18n } from "../lib/i18n";
import { toast, useStore } from "../store";
import { fmtTech, albumTech } from "../lib/fmt";
import { fmtDuration } from "../lib/fmt";
import type { CoverResult, ExpectedTrack, Track } from "../types";

/** Provider id → the name a reader knows ("wikipedia" → Wikipedia). */
const SOURCE_NAMES: Record<string, string> = {
  wikipedia: "Wikipedia",
  lastfm: "Last.fm",
  discogs: "Discogs",
  musicbrainz: "MusicBrainz",
  deezer: "Deezer",
  itunes: "Apple Music",
  audiodb: "TheAudioDB",
  listenbrainz: "ListenBrainz",
  upload: "Uploaded",
  manual: "Manual",
};

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
  // Candidates the import staged for a cover-less album (`cover_review` on).
  // Asked ONLY while the album has no cover file: a covered album's staged set
  // is irrelevant, and this must not add a request to every album page.
  const stagedCovers = useQuery({
    queryKey: ["stagedCovers", decoded],
    queryFn: async () => {
      const c = await api.metadataCandidates(data?.meta?.ALBUMARTIST ?? data?.meta?.ARTIST ?? "", decoded);
      return c.staged?.covers ?? null;
    },
    enabled: !!data && (!data.cover_file || !!data.pending),
    retry: false,
  });
  const stagedCoverRows = stagedCovers.data?.results ?? null;
  // A job holding this folder (a run, an import, an organize) means these files
  // are not playable right now: the header and every row say so BEFORE the
  // click, in the registry's own words.
  const albumLockWhy = useLockWhy(data?.path ?? decoded);
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
  // Cover finder modal: null = closed, else the candidates it opens on (empty
  // = search from scratch, staged rows = the import's own picks).
  const [coverSearch, setCoverSearch] = useState<{ results?: CoverResult[]; provider?: string | null } | null>(null);
  const [beetsBusy, setBeetsBusy] = useState(false);
  // The genre chain is a live network walk over every ticked source, so the
  // menu entry disables and renames itself while it runs (a second click used
  // to start a second chain over the same files).
  const [genresBusy, setGenresBusy] = useState(false);
  const [lyricsBusy, setLyricsBusy] = useState(false);
  const [issuesOpen, setIssuesOpen] = useState(false);
  const [coverInfoOpen, setCoverInfoOpen] = useState(false);
  // Album-wide credits dialog — nothing is fetched until it is opened.
  const [creditsOpen, setCreditsOpen] = useState(false);
  // Album details: the stored readout, rendered from THIS page's payload.
  const [detailsOpen, setDetailsOpen] = useState(false);
  // track checkboxes (and the selection toolbar) only exist in select mode
  const [selectMode, setSelectMode] = useState(false);
  const [tagsDialogOpen, setTagsDialogOpen] = useState(false);
  // Music-video matching (see the "Video matching" panel): video file path →
  // the track index the user assigned it to, plus the in-flight save flag.
  const [videoAssigned, setVideoAssigned] = useState<Record<string, number>>({});
  const [videoSaving, setVideoSaving] = useState(false);
  // The album-level "download the missing videos" run (one at a time).
  const [videosBusy, setVideosBusy] = useState(false);
  // album description (description.txt in the album folder): the edit buffer
  // and one busy flag for the fetch/save/clear trio
  const [descEditing, setDescEditing] = useState(false);
  const [descDraft, setDescDraft] = useState("");
  const [descBusy, setDescBusy] = useState<string | null>(null);
  // tracklist columns: visible set + drag-resized widths, persisted under the
  // SAME key the library's expanded album rows use — one tracklist, one prefs
  // set, and the tag columns the user adds here (mlo-customcols-album-tracks)
  // are therefore the same columns there.
  const [trackCustom, addTrackCustomCol, removeTrackCustomCol] = useCustomColumns("album-tracks");
  const trackDefs: Col[] = [...ALBUM_TRACK_COLS, ...customCols(trackCustom, "tags")];
  const [trackCols, toggleTrackCol] = useColumnPrefs("album-tracks", trackDefs);
  /** A column the user just created should not start hidden (same rule the
   *  library's track table follows). */
  const addCustomTrackCol = (tag: string, label?: string) => {
    const id = addTrackCustomCol(tag, label);
    if (id) toggleTrackCol(id);
  };
  const [trackW, setTrackW, resetTrackW] = useColumnWidths("album-tracks");
  const coverInput = useRef<HTMLInputElement>(null);
  const qc = useQueryClient();
  // The one cover policy's own words (mlo/cover_choice) and the pending state
  // of a framework album are read by the panel and the picker below.
  const { t } = useI18n();
  // The marker's sentence — the same one the library row and the cards show —
  // used by the header chip, the panel below and as the reason the play button
  // is off. Null for a complete album (and until the payload arrives).
  // Named apart from `albumPending` above, which is the RATING write's
  // in-flight flag from useSetRating — a different fact entirely.
  const pendingNote = data ? pendingSummary(data, t) : null;

  // Raw video files (VOB/MKV/...) in this album folder that the remuxer
  // could convert to MP4 — surfaced as a one-click action in the header.
  const { data: videosData, refetch: refetchVideos } = useQuery({
    queryKey: ["videos", decoded],
    queryFn: () => api.videosScan(decoded),
    retry: false,
  });
  const rawVideos = videosData?.videos ?? [];

  // Whether the album-description check grades this folder (Settings →
  // Grading). The config is already in the app-wide cache, so this is free.
  const { data: config } = useQuery({ queryKey: ["config"], queryFn: api.config });
  // Where this album's acquisition is, from the shared queue row and the
  // pushed job frames (lib/acquisition) — the queue is asked only while this
  // IS a framework album waiting for its audio.
  const acquisition = useAcquisitions(!!data?.pending)(data?.path ?? "", data?.wish_id);
  // One GET /api/ratings per scope for the whole page (react-query dedupes it
  // across every row) and the optimistic setters the star controls share. The
  // header needs the ALBUM scope beside the track one: the user's verdict on
  // the album is a different fact from the average of its tracks' ratings, and
  // only the average is derived from the track map.
  const { data: ratingsData } = useRatings();
  const { setRating, pending } = useSetRating();
  const { data: albumRatingsData } = useRatings("album");
  const { setRating: setAlbumRating, pending: albumPending } = useSetRating("album");

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
      toast.error(String(e));
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
      toast.error(String(e));
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
  // Album identity (title/artist) for the description calls, and the stored
  // description the folder carries (description.txt).
  const albumTitle = data.meta?.ALBUM ?? data.path.split("/").pop() ?? "";
  const albumArtist = data.meta?.ALBUMARTIST ?? data.meta?.ARTIST ?? "";
  const desc = data.artwork ?? null;
  // The grading check ("Album description missing") is on by default; with it
  // off, a missing description is not a problem and gets no hint.
  const descGraded = config?.grade_check_album_description !== false;
  // highest disc number across the album (filename fallback included) —
  // drives the "N discs" note in the header and the Disc rows below
  const maxDisc = data.tracks.reduce((m, t) => Math.max(m, t.discnumber ?? 1), 1);
  // One condensed verdict: grading problems OR a FAKE/Mix audit → FAIL.
  const verdictPass = !!data.pass && !auditFails(data.audit_summary);
  const issueEntries = Object.entries(data.issues ?? {});
  const verdictTrack = (tr: Track) => !!tr.grade_pass && !auditFails(tr.audit);

  // Web/digital releases hand out a YouTube music video per track; the medium
  // (album-level tag, then the API's own field) decides whether that is
  // offered at all.
  const digitalMedia = /digital|web|download/i.test(`${data.media ?? ""} ${data.meta?.MEDIA ?? ""}`);
  // YouTube downloads are switchable off (Settings → Videos) and the server
  // refuses them there, so the album's own button says why instead of firing
  // a search that can only come back empty.
  const youtubeOff = config?.youtube_enabled === false;

  /** The track a video row defaults to: the one whose length is closest (an
   *  untagged duration leaves the select empty rather than guessing). */
  const suggestedTrack = (duration: number | null) => {
    if (duration == null) return -1;
    let best = -1;
    let bestDelta = Infinity;
    data.tracks.forEach((t, i) => {
      const len = t.tech.length;
      if (!len) return;
      const delta = Math.abs(len - duration);
      if (delta < bestDelta) {
        bestDelta = delta;
        best = i;
      }
    });
    return best;
  };

  /** Post every assignment the user recorded in ONE call. */
  const saveVideoAssignments = async () => {
    const entries = Object.entries(videoAssigned).filter(([, i]) => i >= 0);
    if (!entries.length) return;
    setVideoSaving(true);
    try {
      const r = await api.videosMatch(
        data.path,
        entries.map(([vpath, i]) => {
          const t = data.tracks[i];
          return {
            path: vpath,
            title: t.tags.TITLE ?? t.file,
            tracknumber: t.tracknumber ?? undefined,
            discnumber: t.discnumber ?? undefined,
          };
        })
      );
      toast(`${r.updated} video(s) matched`);
      qc.invalidateQueries({ queryKey: ["album", decoded] });
    } catch (e) {
      toast.error(String(e));
    } finally {
      setVideoSaving(false);
    }
  };

  /** Download the music video for one track: YouTube, else Soulseek (web/
   *  digital releases only — see `digitalMedia`), then tag it as THAT track's
   *  video.
   *
   *  The download lands in the album folder named after the YouTube upload,
   *  so nothing about the file says which release track it is: the tag write
   *  is what makes it this track's music video (it sorts, plays and grades
   *  with the album from then on). It is the same /api/videos/match the
   *  matching panel posts — one tag path, not a second one — and here the
   *  assignment is certain, because the video was searched for by this
   *  track's own artist and title. Returns whether the video was fetched.
   *
   *  A track YouTube does not have comes back QUEUED from Soulseek instead:
   *  the transfer runs in the app's own downloads for minutes, so there is no
   *  file to tag yet and the answer says so — nothing here can wait for it. */
  const downloadVideo = async (tr: Track): Promise<boolean> => {
    const title = tr.tags.TITLE;
    if (!title) return false;
    try {
      const r = await api.videosDownloadYoutube({
        path: tr.path,
        artist: tr.tags.ARTIST || albumArtist,
        title,
        duration: tr.tech.length || undefined,
      });
      if (r.ok && r.queued) {
        const what = String(r.candidate?.filename ?? title);
        toast(`Queued from Soulseek: ${what} — it downloads into Downloads`);
        qc.invalidateQueries({ queryKey: ["soulseekDownloads"] });
        return true;
      }
      if (!r.ok || !r.file) {
        toast(r.error ? `No music video: ${r.error}` : "No matching music video found");
        return false;
      }
      toast(`Music video saved: ${r.file.split(/[\\/]/).pop()}`);
      try {
        await api.videosMatch(data.path, [
          {
            path: r.file,
            title,
            tracknumber: tr.tracknumber ?? undefined,
            discnumber: tr.discnumber ?? undefined,
          },
        ]);
      } catch (e) {
        // The file IS downloaded; only the tag write failed, and the matching
        // panel can still record the assignment by hand — so say that rather
        // than reporting the whole download as failed.
        toast.error(`Downloaded, but tagging it as "${title}" failed: ${e}`);
      }
      qc.invalidateQueries({ queryKey: ["videos", decoded] });
      qc.invalidateQueries({ queryKey: ["album", decoded] });
      return true;
    } catch (e) {
      toast.error(String(e));
      return false;
    }
  };

  /** Tracks of this album that have no music video yet.
   *
   *  A video is a track's when it carries the same TITLE — exactly what the
   *  tag write above (and the matching panel's Save) produces — so a file
   *  name that happens to look like a title never counts as a match. */
  const albumVideos = data.tracks.filter((t) => t.is_video);
  const missingVideos = data.tracks.filter((t) => {
    const title = (t.tags.TITLE ?? "").trim().toLowerCase();
    if (t.is_video || !title) return false;
    return !albumVideos.some((v) => (v.tags.TITLE ?? "").trim().toLowerCase() === title);
  });

  /** Fetch this album's missing music videos, one at a time.
   *
   *  The SAME per-track call the row menu and the video overlay make, so
   *  there is one download path and one definition of "this track's video".
   *  Sequential on purpose: each video is a multi-hundred-megabyte download
   *  plus a tag remux, and firing the whole album at once would put every one
   *  of them on the same connection. */
  const downloadMissingVideos = async () => {
    if (!missingVideos.length) {
      toast("Every track already has its music video");
      return;
    }
    setVideosBusy(true);
    let saved = 0;
    try {
      for (const [i, tr] of missingVideos.entries()) {
        toast(`Music video ${i + 1}/${missingVideos.length}: ${tr.tags.TITLE}`);
        if (await downloadVideo(tr)) saved++;
      }
    } finally {
      setVideosBusy(false);
      refetchVideos();
    }
    toast(`${saved} of ${missingVideos.length} music video(s) saved`);
  };

  const runScripts = async (ids: number[]) => {
    await api.run(ids, [data.path]);
    // scripts rewrite tags in place — the album payload (tags, grading,
    // covers) is stale until the shared invalidation runs
    invalidateLibrary(qc);
  };

  // Every button in the album's action row is the same 36px square (`.btn-icon`
  // in index.css) — play is the accent-filled one, everything else is quiet.
  /** The library artist page for this album's artist: by album-artist MBID
   * when tagged, else the artist folder (the album's parent directory). */
  const artistHref = data.meta?.MUSICBRAINZ_ALBUMARTISTID
    ? `/artist/mb:${data.meta.MUSICBRAINZ_ALBUMARTISTID}`
    : `/artist/${encodeURIComponent(data.path.split(/[\\/]/).slice(0, -1).join("/"))}`;

  // Tracks of THIS album that are ticked in the global selection.
  const selectedHere = data.tracks.filter((t) => selection.tracks.includes(t.path));
  // The track behind the open video overlay — one of the album's own files.
  const openVideoTrack = videoOpen ? data.tracks.find((t) => t.path === videoOpen) : undefined;

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

  const ratings = ratingsData?.ratings;
  // The album's OWN rating: the user's verdict on the album, out of the album
  // scope. Deliberately NOT derived from the tracks below.
  const albumVerdict = ratingOf(albumRatingsData?.ratings, data?.path);
  // The track AVERAGE, for the labelled read-out beside it: the mean over the
  // tracks that ARE rated (an unrated track must not drag it toward zero),
  // snapped to a half star, plus how many tracks it is over.
  const ratedTracks = (data?.tracks ?? []).map((t) => ratingOf(ratings, t.path)).filter((v) => v > 0);
  const trackAverage = ratedTracks.length
    ? Math.round((ratedTracks.reduce((a, b) => a + b, 0) / ratedTracks.length) * 2) / 2
    : 0;

  const queueTracks = data.tracks.map((t) => ({
    path: t.path, file: t.file, albumPath: data.path,
    artist: data.meta?.ALBUMARTIST ?? data.meta?.ARTIST ?? undefined,
    album: data.meta?.ALBUM ?? undefined, title: t.tags.TITLE || undefined,
    coverFile: t.cover_file ?? null, albumCover: data.cover_file ?? null,
    advisory: t.tags.ITUNESADVISORY ?? null,
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
        advisory: t.tags.ITUNESADVISORY ?? null,
      }))
    );
  };

  const addSelectionToPlaylist = async () => {
    if (!selectedHere.length) return;
    const pls = await api.playlists();
    const manual = pls.find((p) => p.kind === "manual");
    const target = manual ?? (await api.createPlaylist("Library selection", "manual"));
    await api.playlistAdd(target.id, selectedHere.map((t) => t.path));
    toast.success(`Added ${selectedHere.length} track(s) to playlist`);
  };

  /** Import genres for the whole album through the CONFIGURED chain: every
   *  source ticked in Settings → Import (or in the wizard's tray), in their
   *  priority order, stopping as soon as a track's list is complete. The reply
   *  carries what each source wrote (`per_source`) and why one stayed silent
   *  (`notes`), so a blocked RateYourMusic is reported instead of leaving a
   *  bare "0 updated". */
  const importGenres = async () => {
    if (genresBusy) return;
    setGenresBusy(true);
    try {
      const r = await api.genresImport(data.tracks.map((t) => t.path));
      const who = Object.entries(r.per_source ?? {})
        .filter(([, names]) => names.length)
        .map(([name, names]) => `${name} ${names.length}`)
        .join(", ");
      const silent = Object.values(r.notes ?? {});
      toast(r.updated
        ? `${r.updated} track(s) updated${who ? ` — ${who}` : ""}${silent.length ? ` (${silent.join("; ")})` : ""}`
        : `No genres to write${silent.length ? ` — ${silent.join("; ")}` : ""}`);
      invalidateLibrary(qc);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setGenresBusy(false);
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
      toast.error(String(e));
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
      toast.error(String(e));
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
      toast.error(String(e));
    } finally {
      setBeetsBusy(false);
    }
  };

  /** Auto-import missing lyrics for every track in this album.
   *
   * One backend call per album runs the whole synced provider chain
   * (LRCLIB → NetEase → Kugou → QQ Music → Kuwo → YouTube captions, in
   * the order Settings → Lyrics sets),
   * writes per the global lyrics_format and canonicalizes exactly like
   * script 13 — the same code path the track page's button uses. */
  const downloadLyricsAlbum = async () => {
    setLyricsBusy(true);
    try {
      const paths = data.tracks
        .filter((t) => t.tags.INSTRUMENTAL !== "1" && !t.lyrics_present)
        .map((t) => t.path);
      if (!paths.length) {
        toast("Every track already has lyrics (or is instrumental)");
        return;
      }
      const res = await api.lyricsAuto(paths);
      const providers: Record<string, number> = {};
      for (const r of res.results) {
        if (r.status === "ok" && r.provider_label) {
          providers[r.provider_label] = (providers[r.provider_label] ?? 0) + 1;
        }
      }
      const source = Object.entries(providers)
        .sort((a, b) => b[1] - a[1])
        .map(([label, n]) => `${label} ${n}`)
        .join(" · ");
      toast(
        `Lyrics: ${res.ok} imported · ${res.skipped} skipped · ${res.failed} failed` +
          (source ? ` — ${source}` : "")
      );
      invalidateLibrary(qc);
      qc.invalidateQueries({ queryKey: ["album", decoded] });
    } catch (e) {
      toast.error(String(e));
    } finally {
      setLyricsBusy(false);
    }
  };

  /** Description writes change the album's grade as well as its text, so both
   *  the album payload and the library listing are refreshed. */
  const refreshDescription = () => {
    qc.invalidateQueries({ queryKey: ["album", decoded] });
    qc.invalidateQueries({ queryKey: ["library"] });
  };

  const fetchDescription = async () => {
    setDescBusy("fetch");
    try {
      await api.albumDescriptionSave(data.path, "", albumArtist, albumTitle);
      toast("Description fetched");
      refreshDescription();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setDescBusy(null);
    }
  };

  const saveDescription = async () => {
    setDescBusy("save");
    try {
      await api.albumDescriptionSave(data.path, descDraft, albumArtist, albumTitle);
      toast("Description saved");
      setDescEditing(false);
      refreshDescription();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setDescBusy(null);
    }
  };

  const clearDescription = async () => {
    if (!window.confirm("Remove this album's stored description?")) return;
    setDescBusy("clear");
    try {
      await api.albumDescriptionClear(data.path);
      toast("Description removed");
      refreshDescription();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setDescBusy(null);
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
      <div className="relative z-10 p-6 space-y-5">
        {/* hero: the cover plus the album identity, flush on the page
            background; the cover's own colour tints it. */}
        <div
          className="hero-flat relative"
          style={
            coverColor
              ? { background: `linear-gradient(135deg, ${coverColor}22 0%, transparent 60%)` }
              : undefined
          }
        >
          <div className="flex flex-col sm:flex-row items-start gap-5">
            <div className="shrink-0 relative group/cover mx-auto sm:mx-0">
              <CoverImg
                albumPath={data.path}
                coverFile={data.cover_file}
                wrapperClass="h-40 w-40 sm:h-56 sm:w-56 rounded-xl bg-raise overflow-hidden shadow-2xl"
              />
              <input
                ref={coverInput}
                type="file"
                accept="image/*"
                className="hidden"
                onChange={(e) => e.target.files?.[0] && uploadCover(e.target.files[0])}
              />
              {/* small square menu over the cover: upload / find online / info.
                  Same box as the action row below (`.btn-icon`), with an
                  opaque backdrop instead of the panel tint so it stays
                  readable on any cover. `fixed` because the cover sits at the
                  left edge of the scrolled pane: an in-place panel was clipped
                  by `main` and slid under the sidebar. */}
              <div className="absolute top-1.5 right-1.5 opacity-0 group-hover/cover:opacity-100 [@media(hover:none)]:opacity-100 transition-opacity">
                <OverflowMenu
                  fixed
                  buttonClass="btn-icon bg-black/80 border-white/20 text-zinc-100 hover:bg-black hover:text-white"
                  buttonTitle="Cover art actions"
                  sections={[
                    {
                      items: [
                        { label: "Cover info", icon: InfoIcon, onClick: () => setCoverInfoOpen(true) },
                        { label: "Upload cover…", icon: ImageUp, onClick: () => coverInput.current?.click() },
                        { label: "Find cover online", icon: ImageIcon, onClick: () => setCoverSearch({}) },
                      ],
                    },
                  ]}
                />
              </div>
              {/* The import fetched candidates and wrote nothing (`cover_review`
                  on). The pick the silent auto-apply used to make is now one
                  click — shown only for a cover-less album that has a staged
                  set, so an album with art looks exactly as before. */}
              {(!data.cover_file || !!data.pending) && !!stagedCoverRows?.length && (
                <>
                  <button
                    className="btn-primary mt-2 w-40 sm:w-56 !py-1.5 text-xs"
                    onClick={() =>
                      setCoverSearch({
                        results: stagedCoverRows,
                        provider: stagedCovers.data?.provider ?? null,
                      })
                    }
                    title="Covers fetched during import, ranked by the cover policy, waiting for you to pick one"
                  >
                    <ImageIcon className="h-3.5 w-3.5" />
                    Choose a cover ({stagedCoverRows.length})
                  </button>
                  {stagedCovers.data?.chosen && (
                    <div className="mt-1 w-40 sm:w-56 text-[11px] text-zinc-500 leading-snug">
                      <span className="text-zinc-400">{t("cover.best_pick")}: </span>
                      <span className="text-accent-soft">
                        {SOURCE_NAMES[stagedCovers.data.chosen.source] ?? stagedCovers.data.chosen.source}
                        {stagedCovers.data.chosen.width && stagedCovers.data.chosen.height
                          ? ` ${stagedCovers.data.chosen.width}×${stagedCovers.data.chosen.height}`
                          : ""}
                      </span>
                      <span className="block text-zinc-600">
                        {(stagedCovers.data.chosen.reasons ?? []).slice(-1)[0] ?? ""}
                      </span>
                    </div>
                  )}
                </>
              )}
            </div>
            <div className="flex-1 min-w-0 w-full">
              <PageHeader
                overline="Album"
                title={
                  // The advisory (E / C) rides WITH the title — the badge row
                  // below already carries grade/audit/cover state, and the one
                  // mark that tells a reader "this release is explicit" belongs
                  // on the name they are reading, not in a chip two lines down.
                  <span className="inline-flex items-center gap-2 min-w-0">
                    <span className="truncate">{data.meta?.ALBUM ?? data.path.split("/").pop() ?? ""}</span>
                    <AdvisoryMark value={data.meta?.ITUNESADVISORY ?? data.meta?.ALBUMITUNESADVISORY} />
                    {/* held right now (a run, an import, an organize) */}
                    <LockedChip path={data.path} />
                    {/* added, not downloaded yet — the same mark the library
                        row and the cards carry, saying the same sentence */}
                    <PendingMark album={data} size="md" label />
                  </span>
                }
                subtitle={
                  /* the album's meta line: the artist (opens the artist page),
                     then both release dates and label · catalogue */
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5">
                    <Link
                      to={artistHref}
                      className="text-zinc-400 hover:text-accent-soft transition-colors w-fit"
                      title="Open the artist page"
                    >
                      {data.meta?.ALBUMARTIST ?? data.meta?.ARTIST ?? "—"}
                    </Link>
                    <span className="text-zinc-500" title={data.meta?.ORIGINALDATE ?? undefined}>
                      <span className="text-zinc-600 uppercase tracking-wider text-[10px] mr-1.5">Original</span>
                      {data.meta?.ORIGINALDATE ?? "—"}
                    </span>
                    <span className="text-zinc-500" title={data.meta?.DATE ?? undefined}>
                      <span className="text-zinc-600 uppercase tracking-wider text-[10px] mr-1.5">Released</span>
                      {data.meta?.DATE ?? "—"}
                    </span>
                    {(data.meta?.LABEL || data.meta?.CATALOGNUMBER) && (
                      <span className="text-zinc-500 min-w-0 truncate">
                        {[data.meta?.LABEL, data.meta?.CATALOGNUMBER].filter(Boolean).join(" · ")}
                      </span>
                    )}
                  </div>
                }
                chips={[
                  // media + release countries / tech / album DR / disc count,
                  // where the identity block already showed them. The media
                  // chip names the pressing the same way the album's card does.
                  mediaCountryLabel(data.media, data.meta?.RELEASECOUNTRY),
                  albumTech(data.tracks),
                  data.meta?.["ALBUM DYNAMIC RANGE"] ? `ADR ${data.meta["ALBUM DYNAMIC RANGE"]}` : null,
                  maxDisc > 1 ? `${maxDisc} disc${maxDisc === 1 ? "" : "s"}` : null,
                ].filter((c): c is string => !!c)}
              >
                {/* identity strip: the grade verdict and the MB / RYM links —
                    deliberately outside the truncating title. The advisory
                    mark is NOT here any more: it sits with the title. */}
                <div className="flex items-center gap-2 flex-wrap">
                  <button
                    className={`h-2 w-2 rounded-full shrink-0 transition-opacity ${verdictPass ? "bg-emerald-500/70" : "bg-red-500/80"}`}
                    title={verdictPass ? `Pass — ${data.grade_pct ?? "?"}% of checks` : `Fail — ${data.grade_pct ?? "?"}% · ${issueEntries.length} problem type(s)`}
                    onClick={() => setIssuesOpen(!issuesOpen)}
                    aria-label="Grading verdict"
                  />
                  {/* the album's OWN rating — the user's verdict on the album,
                      editable, and a different fact from the mean of its
                      tracks drawn beside it. It lives in the app's store: a
                      folder has no file to carry a RATING tag, which the
                      tooltip says outright. */}
                  <span className="inline-flex items-center gap-1.5" title="Your rating for the album — kept in the app's database; an album folder has no file tag">
                    <StarRating
                      size="lg"
                      showValue
                      label="Album rating"
                      hint={`Your rating for the album: click a star's left half for a half star, click the value already set to clear it (← / → nudge, Delete clears). ${FOLDER_RATING_NOTE}`}
                      value={albumVerdict}
                      onChange={(v) => setAlbumRating(data.path, v)}
                      pending={albumPending(data.path)}
                    />
                    <span className="text-[11px] text-zinc-500">Album rating</span>
                  </span>
                  {/* the mean over the RATED tracks only — an unrated track must
                      not drag it toward zero — labelled as an average so it can
                      never be read as the album's own rating */}
                  {trackAverage > 0 && (
                    <span
                      className="inline-flex items-center gap-1.5"
                      title="The mean of the ratings you gave this album's tracks — not the album rating beside it"
                    >
                      <StarRating readOnly size="sm" value={trackAverage} />
                      <span className="text-[11px] text-zinc-500">
                        avg {trackAverage} from {ratedTracks.length} rated track{ratedTracks.length === 1 ? "" : "s"}
                      </span>
                    </span>
                  )}
                  {/* MusicBrainz / RateYourMusic identity links: exactly one
                      of each — prefer the release over its group */}
                  <LinkChips
                    tags={(data.meta ?? {}) as Record<string, unknown>}
                    only={[
                      ...(data.meta?.MUSICBRAINZ_ALBUMID ? [] : ["MUSICBRAINZ_RELEASEGROUPID"]),
                      "MUSICBRAINZ_ALBUMID",
                      "RATEYOURMUSIC_ALBUM",
                    ]}
                  />
                </div>
                {/* WHAT AN IMPORT COULD NOT SUPPLY (server.import_autonomy):
                    the album is IN the library either way — this is a warning,
                    not a hold, and the one case that really is held says so
                    outright (a review import whose chain has not run). The
                    action is the wizard at the step that decides it, the very
                    link the notification carries. */}
                {data.needs && (
                  <div className="rounded-lg border border-amber-900/60 bg-amber-950/20 px-3 py-2 space-y-1">
                    <div className="flex items-center gap-2 flex-wrap text-xs text-amber-200">
                      <CircleAlert className="h-3.5 w-3.5 shrink-0" />
                      <span className="font-semibold">
                        {data.needs.waiting ? "Waiting for an answer" : "In the library — needs data"}
                      </span>
                      <span className="text-amber-200/70 min-w-0">
                        {data.needs.detail || data.needs.labels.join(", ")}
                      </span>
                    </div>
                    <div className="flex items-center gap-2 flex-wrap text-[11px] text-amber-200/60">
                      <span>
                        {data.needs.waiting
                          ? "The script chain has not run for this album yet: answering the step below finishes it."
                          : "Nothing about this album is held — it is graded like any other, and this is what no source could supply."}
                      </span>
                      {data.needs.link && (
                        <button className="btn-ghost !py-0.5 text-[11px] tap"
                          onClick={() => navigate(data.needs!.link)}
                          title="Open the import wizard at this album and at the step that decides it">
                          <Wand2 className="h-3 w-3" /> Enter it by hand
                        </button>
                      )}
                    </div>
                  </div>
                )}
                {/* A FRAMEWORK album: "Add to library" created this folder
                    before its audio existed, so the page must say what is
                    happening to it rather than read as an empty album. The
                    wish's own state (attempts, the reason a run left, when the
                    next search is due) comes from the queue's policy, and the
                    content the ADD already fetched is named with it. */}
                {data.pending && (
                  <div className="rounded-lg border border-amber-900/60 bg-amber-950/20 px-3 py-2 space-y-1">
                    <div className="flex items-center gap-2 flex-wrap text-xs text-amber-200">
                      <Loader2 className="h-3.5 w-3.5 animate-spin shrink-0" />
                      <span className="font-semibold">{t("pending.title")}</span>
                      {data.pending_reason && (
                        <span className="text-amber-200/70">{data.pending_reason}</span>
                      )}
                      {/* the state, in the marker's own words — one sentence
                          for the dot, the panel and every row that carries it */}
                      {pendingNote && (
                        <span className="text-amber-200/70">{pendingNote.state}</span>
                      )}
                      {/* WHERE the acquisition is: the queue's own stage and,
                          while bytes are moving, slskd's own share of them —
                          pushed at 2.5 Hz, so this reads as live rather than
                          as the last poll's snapshot. */}
                      {acquisition && (
                        <span className="text-amber-100 inline-flex items-center gap-1.5">
                          <span className="font-medium">{acquisition.label}</span>
                          {acquisition.percent !== null && (
                            <>
                              <span className="inline-block w-24 h-1 rounded-sm bg-amber-900/50 overflow-hidden align-middle">
                                <span
                                  className="block h-full bg-amber-300"
                                  style={{ width: `${Math.max(0, Math.min(100, acquisition.percent))}%` }}
                                />
                              </span>
                              <span className="tabular-nums">{Math.round(acquisition.percent)}%</span>
                            </>
                          )}
                        </span>
                      )}
                      {data.wish_id != null && (
                        <Link
                          to="/soulseek"
                          className="text-amber-200/80 underline underline-offset-2 hover:text-amber-100"
                        >
                          queue
                        </Link>
                      )}
                    </div>
                    <div className="text-[11px] text-amber-200/60">{t("pending.note")}</div>
                    {data.wish?.reason && data.wish.reason !== data.pending_reason && (
                      <div className="text-[11px] text-amber-200/60">{data.wish.reason}</div>
                    )}
                    {data.prefetched && (
                      <div className="text-[11px] text-zinc-500">
                        {[
                          data.prefetched.artist_image && "artist image",
                          data.prefetched.artist_description && "artist description",
                          data.prefetched.album_description && "album description",
                          !!data.prefetched.cover_candidates &&
                            `${data.prefetched.cover_candidates} cover candidates`,
                          !!(data.prefetched.links?.album || data.prefetched.links?.artist) && "links",
                        ]
                          .filter((x): x is string => typeof x === "string" && !!x)
                          .join(" · ")}
                      </div>
                    )}
                  </div>
                )}
                {issueEntries.length > 0 && (
                  <div>
                    <button
                      className="tap inline-flex items-center gap-1.5 text-xs text-red-400/80 hover:text-red-300"
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
                {/* the album's actions, directly under the problems line */}
                <div className="flex items-center gap-2 flex-wrap pt-2">
                    {/* A framework album has nothing to play yet, so its play
                        button is OFF and says why (the lock reason first: a job
                        holding the folder is the more immediate fact). */}
                    <span title={pendingNote?.full}>
                      <button
                        className={`btn-icon-primary${albumLockWhy || pendingNote ? " opacity-60" : ""}`}
                        onClick={() => playNow(queueTracks)}
                        disabled={!!pendingNote}
                        title={albumLockWhy || pendingNote?.full || "Play the album from the top"}
                        aria-label={pendingNote?.full || "Play album"}
                      >
                        <Play className="h-4 w-4 fill-current" />
                      </button>
                    </span>
                    <FavHeart
                      kind="album"
                      id={data.path}
                      mbid={data.meta?.MUSICBRAINZ_ALBUMID}
                      className="btn-icon"
                      iconClass="h-4 w-4"
                    />
                    <DownloadButton
                      paths={data.tracks.map((t) => t.path)}
                      iconOnly
                      emptyReason="Nothing to download — this album has no tracks"
                    />
                    <ExportButton
                      paths={data.tracks.map((t) => t.path)}
                      seconds={data.tracks.reduce((s, t) => s + (t.tech?.length ?? 0), 0)}
                      iconOnly
                      title="Export this album to a drive"
                      emptyReason="Nothing to export — this album has no tracks"
                      dialogSubtitle={`${data.meta?.ALBUM ?? data.path.split("/").pop() ?? ""} · ${data.tracks.length} track${data.tracks.length === 1 ? "" : "s"}`}
                    />
                    <LinkEditorButton
                      mode="album"
                      paths={data.tracks.map((t) => t.path)}
                      current={(data.meta ?? {}) as Record<string, unknown>}
                      iconOnly
                    />
                    {/* One album-level entry to the video download, for the
                        whole release: the row menu and the video overlay both
                        fetch a single track's video, and a digital album is
                        missing every one of them at once. Same per-track call
                        underneath (see downloadMissingVideos), so nothing here
                        is a second download path. */}
                    {digitalMedia && (
                      <button
                        className="btn-icon"
                        onClick={downloadMissingVideos}
                        disabled={videosBusy || youtubeOff || !missingVideos.length}
                        title={
                          youtubeOff
                            ? "YouTube downloads are off (Settings → Videos)"
                            : !missingVideos.length
                              ? "No track here is missing a music video"
                              : `Download ${missingVideos.length} missing music video${
                                  missingVideos.length === 1 ? "" : "s"
                                } from YouTube and tag ${missingVideos.length === 1 ? "it" : "them"} as this album's tracks`
                        }
                        aria-label="Download missing music videos"
                      >
                        {videosBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Film className="h-4 w-4" />}
                      </button>
                    )}
                    <TagActionsMenu
                      paths={data.tracks.map((t) => t.path)}
                      albumPath={data.path}
                      artist={data.album_artist ?? undefined}
                      releaseMbid={data.meta?.MUSICBRAINZ_ALBUMID ?? undefined}
                      covers={() => setCoverSearch({})}
                      buttonClass="btn-icon"
                      buttonTitle="Tag actions"
                      onDone={() => invalidateLibrary(qc)}
                    />
                    <OverflowMenu
              buttonClass="btn-icon"
              buttonTitle="All album actions"
              sections={[
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
                    { label: "Open folder", icon: FolderOpen, onClick: async () => { try { await api.openFolder(data.path); } catch (e) { toast.error(String(e)); } } },
                    { label: "Stats", icon: BarChart3, onClick: () => setStatsOpen(true) },
                    { label: "Details", icon: InfoIcon, onClick: () => setDetailsOpen(true) },
                    { label: "Credits", icon: Users, onClick: () => setCreditsOpen(true) },
                  ],
                },
                {
                  title: "Lyrics",
                  items: [
                    { label: lyricsBusy ? "Fetching…" : "Auto-import lyrics", icon: CloudDownload, onClick: downloadLyricsAlbum, disabled: lyricsBusy },
                  ],
                },
                {
                  title: "Tags & scripts",
                  items: [
                    { label: beetsBusy ? "Beets…" : "Tag with beets", icon: Disc3, onClick: beetsTagAlbum, disabled: beetsBusy },
                    { label: genresBusy ? "Importing genres…" : "Import genres", icon: Sparkles, onClick: importGenres, disabled: genresBusy },
                    // A curated subset of the library scripts — the ones worth
                    // one click while looking at one album — but each one is
                    // NAMED by the registry (web/src/lib/scripts.ts) rather
                    // than by a string typed here: this list used to spell
                    // every label itself, so a script the app renamed, or a
                    // number that changed meaning, would have left this menu
                    // quietly wrong. Running scripts in a chain is not what a
                    // single-album menu is for; the order lives in one place
                    // (Settings → Script chain) and every Run All surface
                    // reads it from there.
                    { label: SCRIPT_LABEL[1], icon: FileMusic, onClick: () => runScripts([1]) },
                    { label: SCRIPT_LABEL[2], icon: FileMusic, onClick: () => runScripts([2]) },
                    { label: SCRIPT_LABEL[3], icon: FileMusic, onClick: () => runScripts([3]) },
                    { label: SCRIPT_LABEL[5], icon: FileMusic, onClick: () => runScripts([5]) },
                    { label: SCRIPT_LABEL[6], icon: ShieldCheck, onClick: () => runScripts([6]) },
                    { label: SCRIPT_LABEL[7], icon: FileMusic, onClick: () => runScripts([7]) },
                    { label: SCRIPT_LABEL[8], icon: FileMusic, onClick: () => runScripts([8]) },
                    { label: SCRIPT_LABEL[4], icon: FileMusic, onClick: () => runScripts([4]) },
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
              </PageHeader>
            </div>
          </div>
        </div>

      {/* the folder's description.txt — fetch a Wikipedia summary or write
          your own; it is one of the grading checks, so its absence is called
          out here rather than only in the issue list */}
      <div className="section">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Description</span>
          {desc?.description_source && (
            <span className="text-[10px] text-zinc-600">
              {desc.description_url ? (
                <a
                  href={desc.description_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="hover:text-accent-soft underline decoration-dotted"
                  title={desc.description_url}
                >
                  {SOURCE_NAMES[desc.description_source] ?? desc.description_source}
                </a>
              ) : (
                SOURCE_NAMES[desc.description_source] ?? desc.description_source
              )}
            </span>
          )}
          {!desc?.description && descGraded && (
            <span
              className="text-[10px] text-amber-400/80 border border-amber-900/40 bg-amber-950/20 rounded px-1.5 py-0.5"
              title="The album-description grading check fails while description.txt is missing (Settings → Grading)"
            >
              needs a description
            </span>
          )}
          {/* Every description action sits in one boxed "…" at the block's
              top right — the same square the columns chooser wears over a
              table, so the header row stays two words and a chip. */}
          <div className="ml-auto">
            <OverflowMenu
              buttonClass="p-1.5 rounded-lg border border-border bg-panel/60 text-zinc-500 hover:text-white hover:bg-raise transition-colors"
              buttonTitle="Description actions"
              sections={[
                {
                  items: [
                    {
                      label: descBusy === "fetch" ? "Fetching…" : "Fetch description",
                      icon: RefreshCw,
                      onClick: fetchDescription,
                      disabled: !!descBusy,
                      title: "Fetch the album description from the configured sources (Wikipedia first)",
                    },
                    {
                      label: "Edit description",
                      icon: Pencil,
                      onClick: () => {
                        setDescDraft(desc?.description_text ?? "");
                        setDescEditing(true);
                      },
                      title: "Write or edit the description yourself",
                    },
                    {
                      label: descBusy === "clear" ? "Removing…" : "Remove description",
                      icon: Trash2,
                      danger: true,
                      hidden: !desc?.description,
                      onClick: clearDescription,
                      disabled: !!descBusy,
                      title: "Remove the stored description",
                    },
                  ],
                },
              ]}
            />
          </div>
        </div>
        {descEditing ? (
          <div className="mt-2.5 space-y-2">
            <textarea
              className="input w-full h-40 text-sm leading-relaxed"
              value={descDraft}
              onChange={(e) => setDescDraft(e.target.value)}
              placeholder={`About ${albumTitle}…`}
            />
            <div className="flex items-center gap-2 flex-wrap">
              <button
                className="btn-primary !py-1 text-xs"
                onClick={saveDescription}
                disabled={descBusy === "save" || !descDraft.trim()}
              >
                {descBusy === "save" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null} Save
              </button>
              <button className="btn-ghost !py-1 text-xs" onClick={() => setDescEditing(false)}>
                Cancel
              </button>
              <span className="text-[10px] text-zinc-600">
                Stored as description.txt in the album folder — counts for the album grade.
              </span>
            </div>
          </div>
        ) : desc?.description_text ? (
          <Description key={data.path} text={desc.description_text} />
        ) : (
          <div className="mt-2 text-xs text-zinc-500">
            No description yet —{" "}
            <button className="text-accent-soft hover:underline" onClick={fetchDescription} disabled={!!descBusy}>
              fetch one
            </button>{" "}
            or{" "}
            <button
              className="text-accent-soft hover:underline"
              onClick={() => {
                setDescDraft("");
                setDescEditing(true);
              }}
            >
              write your own
            </button>
            .
          </div>
        )}
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
        <TrackDetails
          track={detailTrack}
          albumPath={data.path}
          onClose={() => setDetailTrack(null)}
          /* The grader puts the MESSAGE on the album ("PATH: expected '…'
             (run organize)") and only the check CODE on the track, so the
             modal would otherwise list bare codes with nothing to act on.
             These are that track's own messages, in the order its codes were
             appended; the modal falls back to the codes if the counts ever
             disagree. */
          messages={issueEntries
            .filter(([, files]) => files.includes(detailTrack.file))
            .map(([text]) => text)}
        />
      )}

      {videoOpen && (
        <div className="fixed inset-0 z-50 bg-black/85 flex items-center justify-center p-6" onClick={() => setVideoOpen(null)}>
          <div className="w-full max-w-4xl" onClick={(e) => e.stopPropagation()}>
            <SubtitledVideo path={videoOpen} className="w-full max-h-[80vh] rounded-lg border border-border bg-black" />
            <div className="flex justify-end items-center gap-2 mt-2">
              {digitalMedia && openVideoTrack?.tags.TITLE && (
                <button
                  className="btn-ghost !py-1"
                  onClick={() => downloadVideo(openVideoTrack)}
                  title={`Download ${openVideoTrack.tags.TITLE} from YouTube`}
                >
                  <Film className="h-3.5 w-3.5" /> Download music video
                </button>
              )}
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

      {creditsOpen && (
        <Modal
          onClose={() => setCreditsOpen(false)}
          icon={Users}
          title="Credits"
          subtitle={data.meta?.ALBUM ?? data.path.split("/").pop() ?? "album"}
          width="max-w-lg"
          bodyClass="px-5 py-5"
        >
          {/* The album-wide lookup: no track path (the server answers for the
              whole release), but with the same tag fallback a track panel
              gets, so an album MusicBrainz does not describe still shows the
              file's own PERFORMER/COMPOSER instead of an empty box. */}
          <CreditsPanel
            album={data.path}
            tags={creditTagsFrom(data.tracks[0]?.tags as Record<string, unknown> | undefined)}
          />
        </Modal>
      )}

      {detailsOpen && <AlbumDetails album={data} onClose={() => setDetailsOpen(false)} />}

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
              <span className="w-px h-5 bg-border mx-0.5 self-center shrink-0" />
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

      {/* Music-video matching: one row per video file in this folder. The
          album's own tracklist is what a video gets tagged with, so the
          picker lists those tracks; assignments post in ONE call. */}
      {rawVideos.length > 0 && (
        <div className="section space-y-3">
          <div className="flex items-center gap-2 flex-wrap">
            <FileVideo className="h-4 w-4 text-accent shrink-0" />
            <h2 className="text-sm font-semibold">Video matching</h2>
            <span className="text-xs text-zinc-500">
              {rawVideos.length} video file{rawVideos.length === 1 ? "" : "s"} in this folder
            </span>
            <button
              className="btn-primary !py-1 text-xs ml-auto"
              onClick={saveVideoAssignments}
              disabled={videoSaving || !Object.values(videoAssigned).some((i) => i >= 0)}
            >
              {videoSaving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null} Save assignments
            </button>
          </div>
          <div className="space-y-1.5">
            {rawVideos.map((v) => {
              // closest-length track: a suggestion that pre-fills the select,
              // not an assignment until the user records one
              const picked = videoAssigned[v.path] ?? suggestedTrack(v.duration);
              return (
                <div key={v.path} className="flex items-center gap-2 flex-wrap">
                  <span className="min-w-0 flex-1 basis-full sm:basis-auto truncate text-xs text-zinc-300" title={v.path}>
                    {v.file}
                  </span>
                  <span className="text-xs text-zinc-500 tabular-nums shrink-0 w-12 text-right">
                    {fmtDuration(v.duration ?? undefined)}
                  </span>
                  <select
                    className="input !py-1 text-xs min-w-0 flex-1 sm:flex-none sm:w-56"
                    value={picked < 0 ? "" : String(picked)}
                    onChange={(e) =>
                      setVideoAssigned({ ...videoAssigned, [v.path]: e.target.value === "" ? -1 : Number(e.target.value) })
                    }
                    title="The track this video belongs to"
                  >
                    <option value="">— not matched —</option>
                    {data.tracks.map((t, i) => (
                      <option key={t.path} value={i}>
                        {`${t.tracknumber ?? t.tags.TRACKNUMBER ?? "?"} · ${t.tags.TITLE ?? t.file}`}
                      </option>
                    ))}
                  </select>
                  <button
                    className="btn-ghost !py-1 text-xs shrink-0"
                    onClick={() => setVideoAssigned({ ...videoAssigned, [v.path]: picked })}
                    disabled={picked < 0 || videoAssigned[v.path] === picked}
                    title="Record this assignment — Save posts them all"
                  >
                    Assign
                  </button>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* `overflow-x-auto`: the tracklist is what overflows on a phone and the
          page cannot scroll sideways for it. */}
      <div className="section overflow-x-auto">
        <table className={`w-full text-sm ${ALBUM_TRACK_MIN_W}`}>
          {/* Borderless header: the only separator is this section's own
              hairline above the table. */}
          <thead>
            <tr>
              {selectMode && <th className="th w-10"></th>}
              {trackDefs.filter((c) => trackCols.includes(c.id)).map((c) =>
                c.id === "cover" ? (
                  <th key={c.id} className={`th relative ${ALBUM_TRACK_COL_W[c.id] ?? TAG_COL_W}`} title="Cover art">
                    <span className="sr-only">Cover</span>
                  </th>
                ) : (
                  <SortHeader key={c.id} label={c.label} sort={sort} sortKey={c.sortKey} onSort={(k) => setSort(toggleSort(sort, k))}
                    className={`relative ${ALBUM_TRACK_COL_W[c.id] ?? TAG_COL_W}${c.id === "num" ? " cell-nowrap" : ""}`}
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
                    cols={trackDefs}
                    visible={trackCols}
                    onToggle={toggleTrackCol}
                    title="Tracklist columns"
                    onAddCustom={addCustomTrackCol}
                    onRemoveCustom={removeTrackCustomCol}
                    onResetWidths={resetTrackW}
                    hasCustomWidths={Object.keys(trackW).length > 0}
                  />
                </div>
              </th>
            </tr>
          </thead>
          <tbody className="stagger">
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
                      advisory: t.tags.ITUNESADVISORY ?? null,
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
                      wrapperClass="h-8 w-8 rounded bg-raise overflow-hidden shrink-0"
                    />
                  </td>
                )}
                {trackCols.includes("title") && (
                  <td className="td">
                    <TrackTitleCell
                      trailing={
                        <>
                          <span className="shrink-0" onClick={(e) => e.stopPropagation()}>
                            <FavHeart kind="track" id={tr.path} mbid={tr.tags.MUSICBRAINZ_TRACKID} iconClass="h-3.5 w-3.5" title={undefined} revealOnHover />
                          </span>
                          {/* The "…": what this ONE file can be asked to do —
                              tagging, its scripts (lyrics among them), credits
                              and the stored readout. Hover-revealed like the
                              heart beside it: a row's actions are not worth
                              permanent space. */}
                          <span className="row-hover shrink-0" onClick={(e) => e.stopPropagation()}>
                            <TrackActionsMenu path={tr.path} releaseMbid={tr.tags.MUSICBRAINZ_ALBUMID} />
                          </span>
                          <span className="shrink-0" onClick={(e) => e.stopPropagation()}>
                            <StarRating size="sm" value={ratingOf(ratings, tr.path)} onChange={(v) => setRating(tr.path, v)} pending={pending(tr.path)} />
                          </span>
                        </>
                      }
                    >
                      <Link
                        to={trackRef(tr)}
                        className="hover:text-accent-soft break-words min-w-0"
                        title="Click to play · Ctrl-click to open track page"
                        onClick={(e) => entityLinkClick(e, () => navigate(trackRef(tr)))}
                      >
                        {tr.tags.TITLE ?? tr.file}
                      </Link>
                      {/* The marks that describe the FILE, directly beside the
                          name they belong to — the EXPLICIT/CLEAN badge first,
                          because it is the one a reader looks for by the title
                          (the rating and the row's actions are in the fixed
                          slot on the right; see TrackTitleCell). */}
                      <AdvisoryMark value={tr.tags.ITUNESADVISORY} />
                      <LockedChip path={tr.path} />
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
                      <GradeBadge pass={verdictTrack(tr)} size="sm" />
                      <CachedMark path={tr.path} />
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
                      {digitalMedia && (
                        <span className="row-hover shrink-0" onClick={(e) => e.stopPropagation()}>
                          <OverflowMenu
                            buttonClass="!p-1 text-zinc-500 hover:text-white"
                            buttonTitle="Track actions"
                            icon={Film}
                            sections={[
                              {
                                items: [
                                  {
                                    label: "Download music video",
                                    icon: Film,
                                    title: "Search YouTube for this track's music video and save it next to the album",
                                    disabled: !tr.tags.TITLE,
                                    onClick: () => downloadVideo(tr),
                                  },
                                ],
                              },
                            ]}
                          />
                        </span>
                      )}
                    </TrackTitleCell>
                  </td>
                )}
                {trackCols.includes("genre") && <td className="td text-zinc-500 break-words">{tr.tags.GENRE ?? "—"}</td>}
                {trackCols.includes("dur") && (
                  <td className="td text-zinc-500 cell-nowrap">{fmtDuration(tr.tech.length)}</td>
                )}
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
                {/* user-added tag columns, in the order they were created —
                    the same order their headers render in above */}
                {trackCustom.map((c) =>
                  trackCols.includes(c.id) ? (
                    <td key={c.id} className="td text-zinc-500 break-words" title={c.label}>
                      {customColValue(tr, c.tag) || "—"}
                    </td>
                  ) : null
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

      {coverSearch && (
        <CoverSearchModal
          albumPath={data.path}
          artist={data.meta?.ALBUMARTIST ?? data.meta?.ARTIST ?? ""}
          album={data.meta?.ALBUM ?? ""}
          releaseGroupMbid={data.meta?.MUSICBRAINZ_RELEASEGROUPID ?? undefined}
          releaseMbid={data.meta?.MUSICBRAINZ_ALBUMID ?? undefined}
          initialResults={coverSearch.results}
          initialProvider={coverSearch.provider}
          initialChosen={stagedCovers.data?.chosen ?? null}
          initialNotes={stagedCovers.data?.notes ?? []}
          onClose={() => setCoverSearch(null)}
          onApplied={() => {
            qc.invalidateQueries({ queryKey: ["library"] });
            qc.invalidateQueries({ queryKey: ["coverColor", decoded] });
            qc.invalidateQueries({ queryKey: ["album", decoded] });
          }}
        />
      )}

      {/* TWO shelves side by side, each saying where its rows came from: what
          the LOCAL scorer ranks closest to this album (this library's own
          tags), and what the online providers suggest for it. The online shelf
          is its own request, so the page is usable before it lands. */}
      <div className="grid gap-4 lg:grid-cols-2 items-start">
        <MoreLikeThis kind="album" id={decoded} />
        <OnlineRecommendations
          kind="albums"
          seedKind="album"
          seedMbid={data.meta?.MUSICBRAINZ_RELEASEGROUPID ?? data.meta?.MUSICBRAINZ_ALBUMID ?? ""}
          seedName={albumTitle}
          seedArtist={albumArtist}
        />
      </div>
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
    <Modal
      onClose={onClose}
      title="Cover info"
      subtitle="Image details for this album's artwork"
      icon={InfoIcon}
      width="max-w-sm"
    >
      <div className="flex flex-col gap-3">
        <div className="rounded-lg overflow-hidden">
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
    </Modal>
  );
}
