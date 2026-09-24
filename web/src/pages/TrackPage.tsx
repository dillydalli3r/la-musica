import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams, useNavigate, Link } from "react-router-dom";
import { Save, Play, Disc3, ListPlus, ListStart, ListMusic, ShieldCheck, ImageUp, Clapperboard, Search, FolderOpen, Users, Info } from "lucide-react";
import { api } from "../api";
import { fmtTech, fmtDuration, isVideoFile } from "../lib/fmt";
import { uncacheTrack } from "../lib/mediaCache";
import { LinkEditorButton, MbIcon, RymIcon } from "../components/Links";
import { SubtitledVideo } from "../components/SubtitledVideo";
import { useStore, toast } from "../store";
import { AuditBadge, GradeBadge, IssueList, EmptyState, PageLoading, LyricsKindChip, allowPlainOf } from "../components/Badges";
import CoverImg from "../components/CoverImg";
import DownloadButton from "../components/DownloadButton";
import { ExportButton } from "../components/ExportDialog";
import LyricsViewer from "../components/LyricsViewer";
import LyricsManagerModal from "../components/LyricsManagerModal";
import LyricsEditorModal from "./../components/LyricsEditorModal";
import OverflowMenu from "../components/OverflowMenu";
import PageHeader from "../components/PageHeader";
import StarRating from "../components/StarRating";
import { ratingOf, useRatings, useSetRating } from "../lib/ratings";
import TagActionsMenu from "../components/TagActionsMenu";
import Modal from "../components/Modal";
import MoreLikeThis from "../components/MoreLikeThis";
import OnlineRecommendations from "../components/OnlineRecommendations";
import LockedChip from "../components/LockedChip";
import { useLockWhy } from "../lib/locks";
import TrackDetails, { CreditsPanel, creditTagsFrom } from "../components/TrackDetails";
import { failedChecksOf, invalidValueReason, isExcessTag, tagInfoOf, tagLabel, tagTooltip, useTagRegistry } from "../lib/tags";

export default function TrackPage() {
  const { path = "" } = useParams();
  const decoded = decodeURIComponent(path);
  const qc = useQueryClient();
  const playNow = useStore((s) => s.playNow);
  const queue = useStore((s) => s.queue);
  const queueAdd = useStore((s) => s.queueAdd);
  // Held by a job right now? Then there is no stream to play and the page says
  // why up front, in the server's own words.
  const lockWhy = useLockWhy(decoded);

  const { data, isLoading, error } = useQuery({
    queryKey: ["track-tags", decoded],
    queryFn: () => api.tags(decoded),
  });

  // What the app knows about each tag, from the server's single source of
  // truth (server/tags_registry.py) — labels, families, writers and checks.
  const reg = useTagRegistry();

  // Full grading/audit context from the album payload (issues, checks,
  // audit verdict, log grade, AccurateRip status, tech). With "mb:<id>"
  // references the album folder comes from the resolved tags payload.
  const albumDir = decoded.startsWith("mb:")
    ? (data?.path ?? "").split("/").slice(0, -1).join("/")
    : decoded.split("/").slice(0, -1).join("/");
  const { data: album, error: albumError } = useQuery({
    queryKey: ["album", albumDir],
    queryFn: () => api.album(albumDir),
    retry: false,
    enabled: !!albumDir,
  });
  // The user's own answer on plain lyrics — whether a plain lyric is shown as
  // the failing state or as the plain fact it is (the config is already in the
  // app-wide cache, so this costs no request of its own).
  const { data: config } = useQuery({ queryKey: ["config"], queryFn: api.config });
  const allowPlain = allowPlainOf(config);
  const realPath = data?.path ?? decoded;
  const track = (album?.tracks ?? []).find((t) => t.path === decoded || t.path === realPath);

  const [lyrics, setLyrics] = useState("");
  const [dirty, setDirty] = useState(false);
  const [managerOpen, setManagerOpen] = useState(false);
  const [editorOpen, setEditorOpen] = useState(false);
  const [coverBusy, setCoverBusy] = useState(false);
  const [videoOpen, setVideoOpen] = useState(false);
  const [creditsOpen, setCreditsOpen] = useState(false);
  // This track's own details modal — TrackDetails renders the stored readout.
  const [detailsOpen, setDetailsOpen] = useState(false);
  const coverInput = useRef<HTMLInputElement>(null);
  // One GET /api/ratings for the page (react-query dedupes it across rows) and
  // the optimistic setter — declared with the other hooks, ABOVE the early
  // returns below: a hook after a conditional return is a crash on the second
  // render, which is exactly how this page broke once already.
  const { data: ratingsData } = useRatings();
  const { setRating, pending } = useSetRating();

  const tags: Record<string, string> = {};
  for (const [k, v] of Object.entries(data?.tags ?? {})) {
    if (typeof v === "string" && v !== "") tags[k] = v;
  }

  useEffect(() => {
    if (!data) return;
    setLyrics(data.lyrics ?? "");
    setDirty(false);
  }, [data]);

  if (error)
    return (
      <EmptyState
        title="Track not found"
        hint={String(error)}
        action={{ label: "Back to the library", to: "/library" }}
      />
    );

  if (isLoading || !data) return <PageLoading label="Loading track…" />;

  const fileName = realPath.split("/").pop() ?? realPath;
  const isVideo = isVideoFile(realPath);
  const tech = track?.tech ?? data.tech ?? {};
  const issues: string[] = track?.issues ?? [];
  const audit = track?.audit ?? null;
  const logGrade = track?.log_grade ?? null;
  const arStatus = track?.accuraterip_status ?? null;
  const csStatus = track?.checksum_status ?? null;

  // The album payload carries this track's grading (issues, audit, log);
  // until that query resolves AND lists the track the verdict is unknown —
  // an empty issue list there means "not graded", not "clean".
  const graded = !!track;
  const gradeText = !graded
    ? (albumError ? "unavailable" : "—")
    : issues.length
      ? `FAILED (${issues.length} check${issues.length === 1 ? "" : "s"})`
      : "PASS";
  const gradeTone = !graded ? "text-zinc-600" : issues.length ? "text-red-300" : "text-emerald-300";

  const saveLyrics = async () => {
    // Save target chosen in the lyrics editor toolbar (embedded tag / .lrc
    // sidecar / both). Default keeps the historical embed-only behavior.
    const target = localStorage.getItem("mlo.lyricsSaveTarget") ?? "embedded";
    try {
      if (target === "embedded" || target === "both") await api.lyricsEmbed(decoded, lyrics);
      if (target === "sidecar" || target === "both") await api.lyricsWrite(decoded, lyrics);
      toast(target === "sidecar" ? "Lyrics saved to .lrc sidecar" : target === "both" ? "Lyrics saved (tag + .lrc sidecar)" : "Lyrics saved");
      setDirty(false);
      qc.invalidateQueries({ queryKey: ["library"] });
      qc.invalidateQueries({ queryKey: ["track-tags", decoded] });
      qc.invalidateQueries({ queryKey: ["album", albumDir] });
    } catch (e) {
      toast.error(String(e));
    }
  };

  /** LyricsManager hands lyrics to the editor; the normal Save flow writes
   * them per the chosen save target. */
  const applyFoundLyrics = (lrc: string, source: string) => {
    setLyrics(lrc);
    setDirty(true);
    setManagerOpen(false);
    toast(`Lyrics loaded — ${source}. Review, then Save.`);
  };

  /** The enhanced editor saves through the API itself; refresh afterwards. */
  const refreshAfterEditor = () => {
    qc.invalidateQueries({ queryKey: ["library"] });
    qc.invalidateQueries({ queryKey: ["track-tags", decoded] });
    qc.invalidateQueries({ queryKey: ["album", albumDir] });
  };

  // The page's own rating state: one GET /api/ratings for the whole page,
  // and the same optimistic setter the rows use.
  const ratings = ratingsData?.ratings;

  const queueTrack = {
    path: realPath, file: fileName, albumPath: albumDir,
    artist: tags.ALBUMARTIST ?? tags.ARTIST, album: tags.ALBUM, title: tags.TITLE || undefined,
    advisory: tags.ITUNESADVISORY ?? null,
  };
  const enqueue = (position: "next" | "end") => {
    if (!queue.length) {
      playNow([queueTrack]);
      return;
    }
    queueAdd([queueTrack], position);
    toast(position === "next" ? "Playing next" : "Added to the queue");
  };
  const addToPlaylist = async () => {
    const pls = await api.playlists();
    const manual = pls.find((p) => p.kind === "manual");
    if (!manual) {
      const created = await api.createPlaylist("Library selection", "manual");
      await api.playlistAdd(created.id, [decoded]);
    } else {
      await api.playlistAdd(manual.id, [decoded]);
    }
    toast.success("Added to playlist");
  };
  const openFolder = async () => {
    try {
      await api.openFolder(albumDir);
    } catch (e) {
      toast.error(String(e));
    }
  };

  const uploadCover = async (file: File) => {
    setCoverBusy(true);
    try {
      await api.cover(albumDir, file, fileName);
      toast(`Per-track cover saved for ${fileName}`);
      qc.invalidateQueries({ queryKey: ["album", albumDir] });
      qc.invalidateQueries({ queryKey: ["library"] });
    } catch (e) {
      toast.error(String(e));
    } finally {
      setCoverBusy(false);
      if (coverInput.current) coverInput.current.value = "";
    }
  };

  // The identity links a tag value can be opened at. The label shown for each
  // comes from the registry like every other tag's (tagLabel below), so only
  // the URL shape lives here.
  const linkTags: Record<string, { kind: "mb" | "rym"; url?: (v: string) => string }> = {
    MUSICBRAINZ_ALBUMID: { kind: "mb", url: (v) => `https://musicbrainz.org/release/${v}` },
    MUSICBRAINZ_TRACKID: { kind: "mb", url: (v) => `https://musicbrainz.org/recording/${v}` },
    MUSICBRAINZ_ARTISTID: { kind: "mb", url: (v) => `https://musicbrainz.org/artist/${v}` },
    MUSICBRAINZ_RELEASEGROUPID: { kind: "mb", url: (v) => `https://musicbrainz.org/release-group/${v}` },
    RATEYOURMUSIC_ALBUM: { kind: "rym" },
    RATEYOURMUSIC_TRACK: { kind: "rym" },
    RATEYOURMUSIC_ARTIST: { kind: "rym" },
  };

  // The grader puts the full problem text on the ALBUM and only the check code
  // on the track, appended in the same order — so a code can be shown with the
  // sentence a person acts on.
  const issueMessages = Object.entries(album?.issues ?? {})
    .filter(([, files]) => files.includes(fileName))
    .map(([text]) => text);
  const messageFor = (code: string) => {
    const i = issues.findIndex((c) => c.toUpperCase() === code.toUpperCase());
    return i >= 0 && issueMessages[i] ? issueMessages[i] : code;
  };

  // Every tag on the file, resolved through the registry: which family it
  // belongs to, whether the strip pass would call it excess, and which of the
  // grader's own issues (plus a closed value set's verdict) it fails on.
  const tagRows = Object.entries(tags).map(([tag, value]) => {
    const invalid = invalidValueReason(reg, tag, value);
    return {
      tag,
      value,
      excess: isExcessTag(reg, tag),
      anomalies: [
        ...failedChecksOf(reg, tag, issues).map((code) => ({ code, title: messageFor(code) })),
        ...(invalid ? [{ code: "bad value", title: invalid }] : []),
      ],
    };
  });
  const grouped = new Map<string, typeof tagRows>();
  for (const row of tagRows) {
    const id = tagInfoOf(reg, row.tag)?.family ?? "other";
    grouped.set(id, [...(grouped.get(id) ?? []), row]);
  }
  // Registry family order, then whatever the registry has no entry for: a tag
  // beets wrote under its own spelling, or one a vendor left behind.
  const tagGroups = [...(reg?.families.map((f) => ({ id: f.id, label: f.label })) ?? []), { id: "other", label: "Other" }]
    .map((f) => ({ ...f, rows: grouped.get(f.id) ?? [] }))
    .filter((g) => g.rows.length > 0);

  return (
    <div className="p-6 space-y-5">
      {/* A music video in a dialog, not a hand-rolled overlay: the portal,
          backdrop click, Escape, focus trap and the phone sheet all come from
          `Modal` (#53) — the old `fixed inset-0 z-50` box had none of them, so
          on iOS the only way out was the one Close button, and the file name
          scrolled under the video on a phone. The name is the dialog title and
          the Close sits in the pinned footer, both always reachable. */}
      {videoOpen && isVideo && (
        <Modal
          onClose={() => setVideoOpen(false)}
          title={fileName}
          icon={Clapperboard}
          width="max-w-4xl"
          bodyClass="p-2"
          footer={
            <div className="flex justify-end">
              <button className="btn-ghost !py-1.5 tap" onClick={() => setVideoOpen(false)}>
                Close
              </button>
            </div>
          }
        >
          <SubtitledVideo path={decoded} className="w-full max-h-[70vh] rounded-lg border border-border bg-black" />
        </Modal>
      )}

      <PageHeader
        overline="Track"
        title={
          <span className="inline-flex items-center gap-2 min-w-0">
            <span className="truncate">{tags.TITLE ?? fileName}</span>
            <LockedChip path={decoded} />
          </span>
        }
        subtitle={
          <>
        {/* the track's own star rating — large, with the numeric value
            beside it, so it reads as a fact about this file */}
        <StarRating
          size="lg"
          showValue
          label="Track rating"
          value={ratingOf(ratings, realPath)}
          onChange={(v) => setRating(realPath, v)}
          pending={pending(realPath)}
        />
            <Link to={tags.MUSICBRAINZ_ALBUMID ? `/album/mb:${tags.MUSICBRAINZ_ALBUMID}` : `/album/${encodeURIComponent(albumDir)}`} className="hover:text-accent-soft">
              {tags.ALBUM || albumDir.split("/").pop()}
            </Link>
            {" · "}
            <span className="inline-flex items-center gap-1"><Disc3 className="h-3 w-3" /> {fileName}</span>
          </>
        }
        actions={
          <>
            <button
              className={`btn-ghost${lockWhy ? " opacity-60" : ""}`}
              onClick={() => playNow([queueTrack])}
              title={lockWhy || "Play this track"}
            >
              <Play className="h-4 w-4 fill-current" /> Play
            </button>
            <DownloadButton
              paths={realPath ? [realPath] : []}
              size="md"
              emptyReason="Nothing to download — this track has no file"
            />
            {/* one track in, one file out — the server never archives a
                single-track export */}
            <ExportButton
              paths={realPath ? [realPath] : []}
              seconds={track?.tech?.length ?? 0}
              size="md"
              title="Export this track to a drive"
              emptyReason="Nothing to export — this track has no file"
              dialogSubtitle="This track — one file on the device, not an archive"
            />
            <LinkEditorButton mode="track" paths={[decoded]} current={tags} />
            {/* cover search for this track = the per-track cover upload below */}
            <TagActionsMenu
              paths={[track?.path ?? realPath]}
              artist={tags.ALBUMARTIST ?? tags.ARTIST}
              albumPath={albumDir}
              // The page holds the album folder for its own panels, but what
              // this menu acts on is ONE track: the album-shaped scripts (a
              // .cue rewrite, a per-album grade, the layout of the subtree)
              // are the album page's, not this row's.
              kind="track"
              releaseMbid={tags.MUSICBRAINZ_ALBUMID}
              covers={() => coverInput.current?.click()}
              onDone={refreshAfterEditor}
            />
            <button className="btn-primary" onClick={saveLyrics} disabled={!dirty} title="Save lyrics per the chosen save target">
              <Save className="h-4 w-4" /> Save lyrics
            </button>
            <OverflowMenu
              buttonTitle="All track actions"
              sections={[
                {
                  items: [
                    { label: "Find lyrics", icon: Search, onClick: () => setManagerOpen(true) },
                    { label: "Add to playlist", icon: ListPlus, onClick: addToPlaylist },
                    { label: "Play next", icon: ListStart, onClick: () => enqueue("next") },
                    { label: "Add to queue", icon: ListMusic, onClick: () => enqueue("end") },
                  ],
                },
                {
                  title: "Track",
                  items: [
                    { label: "Details", icon: Info, onClick: () => setDetailsOpen(true), disabled: !track,
                      title: "Stored tags, technical readout, failed checks and the AudioAuditor verdict for this track" },
                    { label: "Credits", icon: Users, onClick: () => setCreditsOpen(true) },
                    { label: "Watch video", icon: Clapperboard, hidden: !isVideo, onClick: () => setVideoOpen(true) },
                    { label: "Open album folder", icon: FolderOpen, onClick: openFolder },
                  ],
                },
              ]}
            />
          </>
        }
      >
        <div className="flex flex-wrap items-center gap-2">
          {graded ? (
            <>
              <GradeBadge pass={!issues.length} score={issues.length ? 0 : 100} />
              <AuditBadge audit={audit} />
              <IssueList issues={issues} />
            </>
          ) : (
            <span
              className="text-xs text-zinc-600"
              title={albumError ? String(albumError) : "This track's album payload has not loaded yet"}
            >
              {albumError ? "Grading data unavailable" : "Grading —"}
            </span>
          )}
          {/* WHICH KIND the stored lyrics are, beside the verdicts: a synced
              lyric is a fact, and a plain one is a failing state while the
              user's `lyrics_allow_plain` says plain is not acceptable — never
              for a track with no lyrics at all (nothing is rendered then). */}
          <LyricsKindChip kind={track?.lyrics_kind} allowPlain={allowPlain} showReason />
        </div>
      </PageHeader>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-5">
        <div className="space-y-4">
          {/* Every tag the file carries, grouped the way the registry says.
              The label, the meaning, the writer and the grading checks all
              come from server/tags_registry.py, so this page cannot hold a
              different idea of a tag than the engine does — and the two
              anomaly marks are verdicts, not guesses: `excess` is what the
              strip pass would remove (the grader's own allow-list), and a
              failing check is a code the grader already reported here. */}
          <div className="section space-y-3">
            <div className="flex items-baseline gap-2">
              <span className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Tags</span>
              <span className="text-[10px] text-zinc-600">hover a name for what it means, who writes it and what grades it</span>
            </div>
            {tagGroups.map((group) => (
              <div key={group.id} className="space-y-1.5">
                <div className="text-[10px] font-semibold uppercase tracking-wider text-zinc-600 border-b border-border/60 pb-1">
                  {group.label}
                </div>
                <div className="stagger grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-2">
                  {group.rows.map((row) => (
                    <div key={row.tag} className="min-w-0">
                      <div className="text-[10px] text-zinc-500 uppercase truncate" title={tagTooltip(reg, row.tag)}>
                        {tagLabel(reg, row.tag)}
                      </div>
                      <div className="flex items-center gap-1.5 min-w-0">
                        <span
                          className={`text-sm truncate ${row.excess || row.anomalies.length ? "text-amber-200" : "text-zinc-200"}`}
                          title={row.value}
                        >
                          {row.value}
                        </span>
                        {/* the identity-link button rides on its own tag row:
                            MB rows carry the MusicBrainz mark, RYM rows the RYM mark */}
                        {linkTags[row.tag] && (
                          <a
                            href={linkTags[row.tag].url ? linkTags[row.tag].url!(row.value) : row.value}
                            target="_blank"
                            rel="noreferrer"
                            title={`Open ${tagLabel(reg, row.tag)}`}
                            className="p-1 rounded hover:bg-raise transition-transform hover:scale-110 inline-flex items-center shrink-0"
                          >
                            {linkTags[row.tag].kind === "mb" ? <MbIcon className="h-3.5 w-3.5" /> : <RymIcon className="h-3.5 w-3.5" />}
                          </a>
                        )}
                        {row.excess && (
                          <span
                            className="chip shrink-0 text-[10px] bg-amber-950/40 text-amber-300 border border-amber-900"
                            title="No script or tagger this app knows writes it — Optimize FLACs (3) or Format all (10) strips it"
                          >
                            excess
                          </span>
                        )}
                        {row.anomalies.map((a) => (
                          <span
                            key={a.code}
                            className="chip shrink-0 text-[10px] bg-red-950/40 text-red-300 border border-red-900"
                            title={a.title}
                          >
                            {a.code}
                          </span>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>

          <div className="section">
            <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500 mb-2">Audio</div>
            <div className="grid grid-cols-2 gap-2 text-sm text-zinc-400">
              <div>Duration <span className="text-zinc-200">{fmtDuration(tech.length)}</span></div>
              <div>Bitrate <span className="text-zinc-200">{fmtTech(tech) || "—"}</span></div>
              <div>Bit depth <span className="text-zinc-200">{tech.bits_per_sample ?? "—"}</span></div>
              <div>Sample rate <span className="text-zinc-200">{tech.sample_rate ? `${(tech.sample_rate / 1000).toFixed(1).replace(/\.0$/, "")} kHz` : "—"}</span></div>
              <div>Channels <span className="text-zinc-200">{tech.channels ?? "—"}</span></div>
              {typeof tech.width === "number" && typeof tech.height === "number" && (
                <div>Video <span className="text-zinc-200">{tech.width}×{tech.height}{tech.codec ? ` · ${tech.codec}` : ""}</span></div>
              )}
            </div>
          </div>

          {isVideo && (
            <VideoTagCard
              path={realPath}
              tags={tags}
              onSaved={() => {
                qc.invalidateQueries({ queryKey: ["track-tags", decoded] });
                qc.invalidateQueries({ queryKey: ["album", albumDir] });
                qc.invalidateQueries({ queryKey: ["library"] });
              }}
            />
          )}

          <div className="section">
            <div className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-zinc-500 mb-2">
              <ShieldCheck className="h-3.5 w-3.5" /> Grading & AUDIT details
            </div>
            <div className="grid grid-cols-2 gap-2 text-sm text-zinc-400">
              <div>Grade <span className={gradeTone}>{gradeText}</span></div>
              <div>AUDIT <span className="text-zinc-200">{audit ?? "not audited"}</span></div>
              <div>Log score <span className="text-zinc-200">{logGrade != null ? `${logGrade}/100` : "—"}</span></div>
              <div>AccurateRip <span className="text-zinc-200">{arStatus ?? "—"}</span></div>
              <div>Checksum <span className="text-zinc-200">{csStatus ?? "—"}</span></div>
              {["AUDIO_MD5", "INTEGRITY", "LOG_CRC", "REPLAYGAIN_TRACK_GAIN", "DYNAMIC RANGE"].map((k) => (
                tags[k] ? (
                  <div key={k}>{k.replace(/_/g, " ")} <span className="text-zinc-200 break-all">{tags[k]}</span></div>
                ) : null
              ))}
            </div>
            {issues.length > 0 && (
              <>
                <div className="text-[10px] text-zinc-500 uppercase tracking-wider mt-3 mb-1">Failed checks</div>
                <ul className="stagger space-y-1">
                  {issues.map((iss, i) => (
                    <li key={i} className="text-xs text-red-300/90 bg-red-950/30 border border-red-900/40 rounded px-2 py-1">{issueMessages[i] ?? iss}</li>
                  ))}
                </ul>
              </>
            )}
          </div>
        </div>

        <div className="space-y-4">
          <div className="section space-y-2">
            <div className="flex items-center gap-3">
              {track?.cover_file && (
                <CoverImg albumPath={albumDir} coverFile={track.cover_file} wrapperClass="h-16 w-16 rounded-lg bg-raise overflow-hidden shrink-0" />
              )}
              <div className="text-xs text-zinc-500 flex-1">
                {track?.cover_file ? `Per-track cover: ${track.cover_file}` : "No per-track cover — the album cover is used."}
              </div>
            </div>
            <input
              ref={coverInput}
              type="file"
              accept="image/*"
              className="hidden"
              onChange={(e) => e.target.files?.[0] && uploadCover(e.target.files[0])}
            />
            <button
              className="btn-ghost !py-1 text-xs w-full"
              onClick={() => coverInput.current?.click()}
              disabled={coverBusy}
              title={`Upload an image saved next to the track as "${fileName.replace(/\.[^.]+$/, "")}.jpg"`}
            >
              <ImageUp className="h-3.5 w-3.5" /> {coverBusy ? "Uploading…" : "Upload per-track cover"}
            </button>
          </div>
          <LyricsViewer
            path={decoded}
            initialLyrics={lyrics}
            onChange={(v) => { setLyrics(v); setDirty(true); }}
            onSave={saveLyrics}
            artist={tags.ARTIST}
            track={tags.TITLE}
            album={tags.ALBUM}
            duration={tech.length ? Math.round(tech.length) : undefined}
            allowPlain={allowPlain}
            onEnhancedEditor={() => setEditorOpen(true)}
          />
        </div>
      </div>

      {/* Two shelves, side by side: tracks the LOCAL scorer ranks closest to
          this one from elsewhere in the library (its own album excluded), and
          what the online providers suggest for this track. The online shelf
          has its own loading state, so it never delays the page. */}
      <div className="grid gap-4 lg:grid-cols-2 items-start">
        <MoreLikeThis kind="track" id={decoded} />
        <OnlineRecommendations
          kind="tracks"
          seedKind="track"
          seedMbid={tags.MUSICBRAINZ_TRACKID ?? ""}
          seedName={tags.TITLE ?? fileName}
          seedArtist={tags.ARTIST ?? ""}
        />
      </div>

      {managerOpen && (
        <LyricsManagerModal
          path={decoded}
          artist={tags.ARTIST ?? ""}
          track={tags.TITLE ?? ""}
          album={tags.ALBUM || undefined}
          duration={tech.length ? Math.round(tech.length) : undefined}
          currentText={lyrics}
          allowPlain={allowPlain}
          onApplied={applyFoundLyrics}
          onSaved={refreshAfterEditor}
          onClose={() => setManagerOpen(false)}
        />
      )}

      {creditsOpen && (
        <Modal
          onClose={() => setCreditsOpen(false)}
          icon={Users}
          title="Credits"
          subtitle={tags.TITLE ?? fileName}
          width="max-w-lg"
          bodyClass="px-5 py-5"
        >
          {/* The file's own credit tags are the fallback the panel shows when
              MusicBrainz has no relations — pass them, or that line is dead. */}
          <CreditsPanel
            path={track?.path ?? realPath}
            tags={creditTagsFrom(tags as Record<string, unknown>)}
          />
        </Modal>
      )}

      {detailsOpen && track && (
        <TrackDetails track={track} albumPath={albumDir} onClose={() => setDetailsOpen(false)} />
      )}

      {editorOpen && (
        <LyricsEditorModal
          path={decoded}
          artist={tags.ARTIST}
          track={tags.TITLE}
          album={tags.ALBUM || undefined}
          duration={tech.length ? Math.round(tech.length) : undefined}
          initialLyrics={lyrics}
          onClose={() => setEditorOpen(false)}
          onSaved={refreshAfterEditor}
        />
      )}
    </div>
  );
}

const VIDEO_TAG_FIELDS = ["TITLE", "ARTIST", "ALBUM", "GENRE", "DATE", "DISCNUMBER", "TRACKNUMBER"];

/** Tag editor for music-video files. Saving writes the tags via ffmpeg —
 * for containers that can't carry them (VOB, MPEG-PS…) the file is remuxed
 * to MKV with every stream stream-copied, so nothing is re-encoded and
 * captions / audio / video quality are untouched. */
function VideoTagCard({
  path,
  tags,
  onSaved,
}: {
  path: string;
  tags: Record<string, string>;
  onSaved: () => void;
}) {
  const [form, setForm] = useState<Record<string, string>>({});
  const [advisory, setAdvisory] = useState("0");
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();
  const reg = useTagRegistry();

  useEffect(() => {
    setForm(Object.fromEntries(VIDEO_TAG_FIELDS.map((k) => [k, tags[k] ?? ""])));
    setAdvisory(tags.ITUNESADVISORY ?? "0");
  }, [path]);

  const set = (k: string, v: string) => setForm((m) => ({ ...m, [k]: v }));

  const save = async () => {
    setBusy(true);
    try {
      const clean: Record<string, string> = {};
      for (const [k, v] of Object.entries(form)) if (v.trim()) clean[k] = v.trim();
      if (advisory.trim()) clean.ITUNESADVISORY = advisory.trim();
      const r = await api.videoTag(path, clean);
      if (r.renamed) {
        // the file moved (remux rename) — the old stream URL's offline cache
        // entry would serve the stale file forever, and this page's own tags
        // query would 404 on the path that no longer exists. The new path
        // itself is announced by api.videoTag's container-swap toast.
        await uncacheTrack(path);
        navigate(`/track/${encodeURIComponent(r.path)}`, { replace: true });
      }
      toast("Tags written");
      onSaved();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="section space-y-2.5">
      <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500 flex items-center gap-1.5">
        <Clapperboard className="h-3.5 w-3.5" /> Tag this music video
      </div>
      <div className="grid grid-cols-2 gap-2.5">
        {VIDEO_TAG_FIELDS.map((k) => (
          <label key={k} className="text-[10px] text-zinc-500 uppercase block">
            {tagLabel(reg, k)}
            <input
              className="input !py-1 !px-2 text-xs mt-0.5 w-full"
              value={form[k] ?? ""}
              onChange={(e) => set(k, e.target.value)}
            />
          </label>
        ))}
        <label className="text-[10px] text-zinc-500 uppercase block">
          Advisory
          <select
            className="input !py-1 !px-2 text-xs mt-0.5 w-full"
            value={advisory}
            onChange={(e) => setAdvisory(e.target.value)}
          >
            <option value="0">Clean (0)</option>
            <option value="1">Explicit (1)</option>
            <option value="2">Cleaned (2)</option>
          </select>
        </label>
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        <button className="btn-primary !py-1 text-xs" disabled={busy} onClick={save}>
          <Save className="h-3.5 w-3.5" /> Save video tags
        </button>
        <span className="text-[10px] text-zinc-600">
          Video / audio / captions are stream-copied — containers that can't hold tags are remuxed to MKV losslessly.
        </span>
      </div>
    </div>
  );
}
