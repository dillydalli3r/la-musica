import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useState, type MouseEvent } from "react";
import {
  BarChart3, BookmarkPlus, ChevronDown, ChevronRight, Disc3, Download, FileDown, ImagePlus,
  ListChecks, Loader2, Music2, Pencil, Play, RefreshCw, Square, SquareCheck, Trash2,
} from "lucide-react";
import { api } from "../api";
import { LinkChips, LinkEditorButton } from "../components/Links";
import { EmptyState, GradeBadge, GradeBar, PageLoading } from "../components/Badges";
import CoverImg from "../components/CoverImg";
import FavHeart from "../components/FavHeart";
import ArtistImageModal from "../components/ArtistImageModal";
import MetadataReviewModal from "../components/MetadataReviewModal";
import MoreLikeThis from "../components/MoreLikeThis";
import PageHeader from "../components/PageHeader";
import TagActionsMenu from "../components/TagActionsMenu";
import type { Album } from "../types";
import { albumRef, artistMbid } from "../lib/refs";
import { auditFails } from "../lib/status";
import { invalidateLibrary } from "../lib/invalidate";
import StatsPanel from "../components/StatsPanel";
import { toast, useStore } from "../store";

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

/** The album sections the list groups by, in display order. */
const TYPE_ORDER = ["Album", "EP", "Single", "Live", "Compilation", "Other"] as const;
type ReleaseType = (typeof TYPE_ORDER)[number];

/** AlbumMeta carries no RELEASETYPE, so the type comes from the first track
 *  that tags one (the importer stamps the same value on every track). A
 *  combined type ("Album + Compilation") buckets by the first match in
 *  releaseType's precedence. */
function releaseType(al: Album): ReleaseType {
  const raw = (al.tracks.find((t) => t.tags?.RELEASETYPE)?.tags?.RELEASETYPE ?? "").toLowerCase();
  if (raw.includes("compilation")) return "Compilation";
  if (raw.includes("live")) return "Live";
  if (raw.includes("ep")) return "EP";
  if (raw.includes("single")) return "Single";
  if (raw.includes("album")) return "Album";
  return "Other";
}

export default function ArtistPage() {
  const { path = "" } = useParams();
  const decoded = decodeURIComponent(path);
  const qc = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: ["artist", decoded],
    queryFn: () => api.artist(decoded),
  });
  const { playNow } = useStore();
  const [statsOpen, setStatsOpen] = useState(false);
  const [imageOpen, setImageOpen] = useState(false);
  const [descEditing, setDescEditing] = useState(false);
  const [descDraft, setDescDraft] = useState("");
  // One flag for every in-flight artwork/description/batch write — the buttons
  // in a block share a target, so two at once is always a mistake.
  const [busy, setBusy] = useState<string | null>(null);
  // Album-list UI: collapsed RELEASETYPE sections, and the select-mode set of
  // album paths the batch bar acts on.
  const [collapsed, setCollapsed] = useState<Set<ReleaseType>>(new Set());
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [reviewOpen, setReviewOpen] = useState(false);
  const navigate = useNavigate();

  if (error)
    return (
      <EmptyState
        title="Artist not found"
        hint="The artist folder may have been renamed, moved or deleted."
        action={{ label: "Back to the library", to: "/library" }}
      />
    );
  if (isLoading || !data) return <PageLoading label="Loading artist…" />;

  const name = data.display_name || data.name;
  const allTracks = data.albums.flatMap((a) =>
    a.tracks.map((t) => ({
      path: t.path, file: t.file, albumPath: a.path,
      artist: a.album_artist || data.display_name || data.name,
      album: a.meta?.ALBUM ?? undefined, title: t.tags.TITLE || undefined,
      coverFile: t.cover_file ?? null, albumCover: a.cover_file ?? null,
    }))
  );
  // Identity links for this artist: MBID from any album's album-artist tag,
  // RYM artist URL from any track that carries one.
  const artistTags = {
    MUSICBRAINZ_ARTISTID: artistMbid(data) ?? "",
    RATEYOURMUSIC_ARTIST:
      data.albums.flatMap((a) => a.tracks.map((t) => t.tags?.RATEYOURMUSIC_ARTIST ?? ""))
        .find((v) => v) ?? "",
  };

  // Stored artwork (artist.jpg + description.txt, see mlo/artistdata) and the
  // artist-level grade that watches for both. The image URL the payload hands
  // over is authoritative; the endpoint URL is the fallback when it is absent.
  const art = data.artwork;
  const imageUrl = art?.image_url ?? (art?.image ? api.artistImageUrl(decoded) : null);
  const descText = art?.description ?? "";
  const grade = data.grade;
  const gradeIssues = grade?.issues ?? [];
  const monogram = name.trim().split(/\s+/).map((w) => w[0] ?? "").join("").slice(0, 2).toUpperCase();
  const artistGradeTitle = gradeIssues.length
    ? gradeIssues.map((i) => i.label).join(" · ")
    : "Artist image and description — both present";

  /** Every artwork/description write invalidates the SAME queries the rest of
   *  the app refreshes after a library write: the artist payload this page
   *  renders, plus library/home/album/track-tags (see lib/invalidate). */
  const refresh = () => invalidateLibrary(qc);

  const fetchDescription = async () => {
    setBusy("desc-fetch");
    try {
      await api.artistDescriptionSave(decoded);
      toast("Description fetched");
      refresh();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(null);
    }
  };

  const saveDescription = async () => {
    setBusy("desc-save");
    try {
      await api.artistDescriptionSave(decoded, descDraft);
      toast("Description saved");
      setDescEditing(false);
      refresh();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(null);
    }
  };

  const clearDescription = async () => {
    if (!window.confirm("Remove the stored artist description?")) return;
    setBusy("desc-clear");
    try {
      await api.artistDescriptionClear(decoded);
      toast("Description removed");
      refresh();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(null);
    }
  };

  const removeImage = async () => {
    if (!window.confirm(`Remove the stored image of ${name}?\nThe artist folder keeps no artist.jpg afterwards.`)) return;
    setBusy("image-clear");
    try {
      await api.artistImageClear(decoded);
      toast("Artist image removed");
      refresh();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(null);
    }
  };

  const artistMb = artistMbid(data);

  /** One section per non-empty RELEASETYPE bucket, in TYPE_ORDER. */
  const sections = TYPE_ORDER.map((type) => ({
    type,
    albums: data.albums.filter((al) => releaseType(al) === type),
  })).filter((s) => s.albums.length > 0);

  const selectedAlbums = data.albums.filter((al) => selected.has(al.path));
  const selectedTrackPaths = selectedAlbums.flatMap((al) => al.tracks.map((t) => t.path));

  const toggleSel = (albumPath: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(albumPath)) next.delete(albumPath);
      else next.add(albumPath);
      return next;
    });
  };

  const toggleSelectMode = () => {
    setSelectMode((on) => !on);
    setSelected(new Set<string>()); // leaving (or re-entering) select mode drops the selection
  };

  /** "Auto-import best release" over the selection: one queue call per album,
   *  summed into a single toast so a 20-album batch is not 20 toasts. Albums
   *  with no MusicBrainz id at all count as skipped. */
  const autoImportBest = async () => {
    setBusy("batch-import");
    let queued = 0;
    let skipped = 0;
    try {
      for (const al of selectedAlbums) {
        // The release group when the tags carry one, else the release itself:
        // the release-group endpoint 502s when handed a release id.
        const rg = al.tracks[0]?.tags?.MUSICBRAINZ_RELEASEGROUPID ?? al.meta?.MUSICBRAINZ_RELEASEGROUPID;
        const release = al.meta?.MUSICBRAINZ_ALBUMID;
        const mbid = rg ?? release;
        if (!mbid) {
          skipped += 1;
          continue;
        }
        const r = await api.mbAutoImport({
          mbid,
          kind: rg ? "release_group" : "release",
          mode: "best",
        });
        queued += r.queued;
        skipped += r.skipped.length;
      }
      toast(`Queued ${queued} release${queued === 1 ? "" : "s"}${skipped ? ` · ${skipped} skipped` : ""}`);
      refresh();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(null);
    }
  };

  /** "Add to wishes" over the selection. A wish is filled by looking a RELEASE
   *  up, so the release id wins over the release group when both are tagged. */
  const addWishes = async () => {
    setBusy("batch-wish");
    let added = 0;
    let skipped = 0;
    try {
      for (const al of selectedAlbums) {
        const rg = al.tracks[0]?.tags?.MUSICBRAINZ_RELEASEGROUPID ?? al.meta?.MUSICBRAINZ_RELEASEGROUPID;
        const mbid = al.meta?.MUSICBRAINZ_ALBUMID ?? rg;
        if (!mbid) {
          skipped += 1;
          continue;
        }
        await api.wishAdd({
          release_mbid: mbid,
          title: al.meta?.ALBUM ?? al.path.split("/").pop() ?? "",
          artist: name,
        });
        added += 1;
      }
      toast(`Added ${added} wish${added === 1 ? "" : "es"}${skipped ? ` · ${skipped} skipped (no MBID)` : ""}`);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(null);
    }
  };

  /** "Download best per group" / "Download all": the artist's whole discography
   *  through the MusicBrainz artist id, one queue call for the lot. */
  const downloadArtist = async (mode: "best" | "all") => {
    if (!artistMb) return;
    setBusy(`download-${mode}`);
    try {
      const r = await api.mbAutoImport({ mbid: artistMb, kind: "artist", mode });
      toast(
        `Queued ${r.queued} release${r.queued === 1 ? "" : "s"}${r.skipped.length ? ` · ${r.skipped.length} skipped` : ""}`
      );
      refresh();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="p-6 space-y-5 mx-auto max-w-6xl">
      <div className="hero-flat relative overflow-hidden">
        {/* the stored image doubles as the hero backdrop, blurred behind the
            identity block so the name stays readable */}
        {imageUrl && (
          <img
            src={imageUrl}
            alt=""
            aria-hidden
            className="absolute inset-0 h-full w-full object-cover opacity-25 blur-2xl scale-110"
          />
        )}
        <div className="absolute inset-0 bg-gradient-to-t from-bg via-bg/70 to-bg/30" />
        <div className="relative flex flex-col sm:flex-row items-start gap-5">
          <div className="h-40 w-40 rounded-xl overflow-hidden border border-border bg-gradient-to-br from-accent/30 to-accent/5 flex items-center justify-center shrink-0 shadow-2xl ring-1 ring-black/40">
            {imageUrl ? (
              <img src={imageUrl} alt={`${name} artist image`} className="h-full w-full object-cover" />
            ) : monogram ? (
              <span className="text-4xl font-bold tracking-tight text-zinc-300 select-none" title="No artist image stored yet">
                {monogram}
              </span>
            ) : (
              <Music2 className="h-10 w-10 text-zinc-500" />
            )}
          </div>

          <div className="flex-1 min-w-0 w-full">
            <PageHeader
              overline="Artist"
              title={name}
              subtitle={
                <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-zinc-400">
                  <span>
                    {data.aggregate.album_count} album{data.aggregate.album_count === 1 ? "" : "s"} · {data.aggregate.track_count} track{data.aggregate.track_count === 1 ? "" : "s"}
                  </span>
                  {/* two different verdicts, labeled: the aggregate of the album
                      checks vs. the artist folder's own image/description checks */}
                  <span className="inline-flex items-center gap-1.5" title="Grading of this artist's albums">
                    <GradeBadge
                      pass={(data.aggregate.grade_pct ?? 0) >= 100 && !auditFails(data.aggregate.audit_summary)}
                      score={data.aggregate.grade_pct}
                      audit={data.aggregate.audit_summary}
                    />
                    <span className="text-xs text-zinc-500">albums</span>
                  </span>
                  {grade?.error ? (
                    <span className="text-xs text-zinc-600" title={grade.error}>artist grade unavailable</span>
                  ) : grade && (grade.checks ?? 0) === 0 ? (
                    <span className="text-xs text-zinc-600" title="Both artist checks are switched off in Settings → Grading">
                      artist checks off
                    </span>
                  ) : grade ? (
                    <span className="inline-flex items-center gap-1.5" title={artistGradeTitle}>
                      <GradeBadge pass={!!grade.pass} score={grade.pct ?? null} size="sm" />
                      <span className="text-xs text-zinc-500">
                        artist artwork{grade.checks ? ` ${grade.pass_count}/${grade.checks}` : ""}
                      </span>
                    </span>
                  ) : null}
                  {gradeIssues.map((i) => (
                    <span
                      key={i.code}
                      className="text-[10px] text-red-300/90 bg-red-950/30 border border-red-900/40 rounded px-1.5 py-0.5"
                      title={i.where ? `${i.label} — ${i.where}` : i.label}
                    >
                      {i.label}
                    </span>
                  ))}
                </span>
              }
              actions={
                <>
                  {/* beside the title, but out of its truncating span so a long
                      name can never clip the heart */}
                  <FavHeart kind="artist" id={data.path} mbid={artistMb} />
                  <button className="btn-primary" onClick={() => playNow(allTracks)} title={`Play all ${allTracks.length} tracks`}>
                    <Play className="h-4 w-4 fill-current" /> Play all
                  </button>
                  <TagActionsMenu
                    paths={allTracks.map((t) => t.path)}
                    artist={decoded}
                    onDone={refresh}
                    buttonTitle="Tag actions on every track of this artist"
                  />
                  <button
                    className="btn-ghost"
                    onClick={() => setReviewOpen(true)}
                    title="Review candidate artist images and descriptions"
                  >
                    Metadata review
                  </button>
                  <button
                    className="btn-ghost"
                    onClick={() => downloadArtist("best")}
                    disabled={!artistMb || !!busy}
                    title={
                      artistMb
                        ? "Queue the best edition of every release group not in the library yet"
                        : "No MusicBrainz artist ID on this folder — match an album first"
                    }
                  >
                    {busy === "download-best" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}
                    Download best per group
                  </button>
                  <button
                    className="btn-ghost"
                    onClick={() => downloadArtist("all")}
                    disabled={!artistMb || !!busy}
                    title={
                      artistMb
                        ? "Queue every eligible edition of every release group not in the library yet"
                        : "No MusicBrainz artist ID on this folder — match an album first"
                    }
                  >
                    {busy === "download-all" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}
                    Download all
                  </button>
                  <button
                    className="btn-ghost"
                    onClick={() => setImageOpen(true)}
                    title={imageUrl ? "Pick a different artist image" : "Find an artist image online, or upload your own"}
                  >
                    <ImagePlus className="h-4 w-4" /> {imageUrl ? "Change image" : "Find image"}
                  </button>
                  {imageUrl && (
                    <button
                      className="btn-ghost !px-2.5 text-red-300/80 hover:text-red-200"
                      onClick={removeImage}
                      disabled={busy === "image-clear"}
                      title="Delete artist.jpg from the artist folder"
                    >
                      {busy === "image-clear" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
                    </button>
                  )}
                  <LinkEditorButton mode="artist" paths={allTracks.map((t) => t.path)} current={artistTags} artist={data.name} />
                  <button className="btn-ghost" onClick={() => setStatsOpen(true)}>
                    <BarChart3 className="h-4 w-4" /> Stats
                  </button>
                </>
              }
            >
              <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
                {data.display_name && data.display_name !== data.name && (
                  <span className="text-xs text-zinc-600" title={data.name}>{data.name}</span>
                )}
                <LinkChips tags={artistTags} />
                {!imageUrl && (
                  <span className="text-xs text-zinc-500">
                    No artist image yet —{" "}
                    <button className="text-accent-soft hover:underline" onClick={() => setImageOpen(true)}>
                      look for one or upload your own
                    </button>
                    .
                  </span>
                )}
              </span>
            </PageHeader>
          </div>
        </div>
      </div>

      {statsOpen && (
        <StatsPanel
          title={data.name}
          albums={data.albums}
          tracks={data.albums.flatMap((a) => a.tracks)}
          onClose={() => setStatsOpen(false)}
        />
      )}

      <div className="section">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Description</span>
          {art?.description_source && (
            <span className="text-[10px] text-zinc-600">
              {art.description_url ? (
                <a
                  href={art.description_url}
                  target="_blank"
                  rel="noreferrer"
                  className="hover:text-accent-soft underline decoration-dotted"
                  title={art.description_url}
                >
                  {SOURCE_NAMES[art.description_source] ?? art.description_source}
                </a>
              ) : (
                SOURCE_NAMES[art.description_source] ?? art.description_source
              )}
            </span>
          )}
          <div className="ml-auto flex items-center gap-1.5">
            <button
              className="btn-ghost !py-1 text-xs"
              onClick={fetchDescription}
              disabled={!!busy}
              title="Fetch the biography from the configured sources (Wikipedia first)"
            >
              {busy === "desc-fetch" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
              Fetch
            </button>
            <button
              className="btn-ghost !py-1 text-xs"
              onClick={() => {
                setDescDraft(descText);
                setDescEditing(true);
              }}
              title="Write or edit the description yourself"
            >
              <Pencil className="h-3.5 w-3.5" /> Edit
            </button>
            {descText && (
              <button
                className="btn-ghost !py-1 text-xs text-red-300/80 hover:text-red-200"
                onClick={clearDescription}
                disabled={!!busy}
                title="Remove the stored description"
              >
                {busy === "desc-clear" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
                Clear
              </button>
            )}
          </div>
        </div>
        {descEditing ? (
          <div className="mt-2.5 space-y-2">
            <textarea
              className="input w-full h-40 text-sm leading-relaxed"
              value={descDraft}
              onChange={(e) => setDescDraft(e.target.value)}
              placeholder="Artist biography…"
            />
            <div className="flex items-center gap-2 flex-wrap">
              <button
                className="btn-primary !py-1 text-xs"
                onClick={saveDescription}
                disabled={busy === "desc-save" || !descDraft.trim()}
              >
                {busy === "desc-save" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null} Save
              </button>
              <button className="btn-ghost !py-1 text-xs" onClick={() => setDescEditing(false)}>
                Cancel
              </button>
              <span className="text-[10px] text-zinc-600">
                Stored as description.txt in the artist folder — counts for the artist grade.
              </span>
            </div>
          </div>
        ) : descText ? (
          <p className="mt-2 text-sm text-zinc-300 leading-relaxed whitespace-pre-line max-h-72 overflow-y-auto" title={`${descText.length} characters`}>
            {descText}
          </p>
        ) : (
          <div className="mt-2 text-xs text-zinc-500">
            No description yet —{" "}
            <button className="text-accent-soft hover:underline" onClick={fetchDescription} disabled={!!busy}>
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

      <section className="space-y-3">
        <div className="flex items-center gap-2 flex-wrap">
          <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Albums</h2>
          <span className="text-[10px] text-zinc-600">{data.albums.length}</span>
          {data.albums.length > 0 && (
            <button
              className={`btn-ghost !py-1 !px-2 text-xs ml-auto ${selectMode ? "!text-accent-soft !border-accent" : ""}`}
              onClick={toggleSelectMode}
              title={selectMode ? "Leave select mode" : "Pick albums for a batch action"}
              aria-pressed={selectMode}
            >
              {selectMode ? <SquareCheck className="h-3.5 w-3.5" /> : <Square className="h-3.5 w-3.5" />}
              Select
            </button>
          )}
        </div>

        {/* Batch bar: only while something is picked. Each button re-runs on the
            CURRENT selection (the per-album TagActionsMenu lives on the albums).
            Same accent bar the album page uses for its own selection. */}
        {selected.size > 0 && (
          <div className="flex items-center gap-2 flex-wrap bg-accent/15 border border-accent/40 rounded-lg px-3 py-2">
            <span className="text-xs text-zinc-300 inline-flex items-center gap-1.5">
              <ListChecks className="h-3.5 w-3.5 text-accent-soft" />
              {selected.size} album{selected.size === 1 ? "" : "s"} selected
            </span>
            <button className="btn-ghost !py-1 !px-2 text-xs" onClick={() => setSelected(new Set<string>())}>
              Clear
            </button>
            <span className="ml-auto flex items-center gap-1.5 flex-wrap">
              <button
                className="btn-primary !py-1 text-xs"
                onClick={autoImportBest}
                disabled={!!busy}
                title="Queue the best Soulseek edition of each selected album (release groups already in the library are skipped)"
              >
                {busy === "batch-import" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
                Auto-import best release
              </button>
              <button
                className="btn-ghost !py-1 text-xs"
                onClick={addWishes}
                disabled={!!busy}
                title="Save each selected album on the wishlist — the background worker fills them from Soulseek"
              >
                {busy === "batch-wish" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <BookmarkPlus className="h-3.5 w-3.5" />}
                Add to wishes
              </button>
              <TagActionsMenu
                paths={selectedTrackPaths}
                artist={decoded}
                onDone={refresh}
                buttonClass="btn-ghost !py-1 text-xs"
                buttonTitle="Tag actions on the selected albums"
              />
              <button
                className="btn-ghost !py-1 text-xs"
                onClick={() => navigate("/export")}
                title="Open the export page"
              >
                <FileDown className="h-3.5 w-3.5" /> Export…
              </button>
            </span>
          </div>
        )}

        {data.albums.length === 0 ? (
          <EmptyState
            title="No albums yet"
            hint="Albums show up here once a folder is imported under this artist."
          />
        ) : (
          sections.map(({ type, albums }) => {
            const isCollapsed = collapsed.has(type);
            return (
              <section key={type} className="section space-y-1">
                <button
                  type="button"
                  className="flex w-full items-center gap-1.5 pb-2 text-left text-xs font-bold uppercase tracking-wider text-zinc-400 select-none"
                  onClick={() => setCollapsed((prev) => {
                    const next = new Set(prev);
                    if (next.has(type)) next.delete(type);
                    else next.add(type);
                    return next;
                  })}
                  aria-expanded={!isCollapsed}
                >
                  {isCollapsed ? <ChevronRight className="h-3.5 w-3.5 text-zinc-500" /> : <ChevronDown className="h-3.5 w-3.5 text-zinc-500" />}
                  <Disc3 className="h-3.5 w-3.5 text-zinc-500" />
                  {type}
                  <span className="font-normal text-zinc-600 normal-case">
                    {albums.length} album{albums.length === 1 ? "" : "s"}
                  </span>
                </button>
                {!isCollapsed && (
                  <div className="stagger space-y-1">
                    {albums.map((al) => {
                      const albumName = al.meta?.ALBUM ?? al.path.split("/").pop() ?? "";
                      const href = albumRef(al);
                      const isSel = selected.has(al.path);
                      // In select mode a link selects instead of navigating —
                      // otherwise the album page opens instead of the checkbox.
                      const linkClick = (e: MouseEvent) => {
                        if (!selectMode) return;
                        e.preventDefault();
                        toggleSel(al.path);
                      };
                      return (
                        <div
                          key={al.path}
                          className={`px-3 py-3 rounded-lg transition-colors flex items-center gap-4 group ${
                            isSel ? "bg-accent/15" : "hover:bg-white/[0.06]"
                          } ${selectMode ? "cursor-pointer" : ""}`}
                          onClick={selectMode ? () => toggleSel(al.path) : undefined}
                        >
                          {selectMode && (
                            <input
                              type="checkbox"
                              className="shrink-0"
                              checked={isSel}
                              onChange={() => toggleSel(al.path)}
                              onClick={(e) => e.stopPropagation()}
                              aria-label={`Select ${albumName}`}
                            />
                          )}
                          <Link to={href} className="shrink-0" title={`Open ${albumName}`} onClick={linkClick}>
                            <CoverImg
                              albumPath={al.path}
                              coverFile={al.cover_file}
                              wrapperClass="h-14 w-14 rounded-md bg-raise border border-border overflow-hidden shrink-0 group-hover:border-zinc-600 transition-colors"
                            />
                          </Link>
                          <GradeBar pct={al.grade_pct} />
                          <div className="flex-1 min-w-0">
                            <Link
                              to={href}
                              className="font-semibold hover:text-accent-soft transition-colors break-words"
                              title={albumName}
                              onClick={linkClick}
                            >
                              {albumName}
                            </Link>
                            <div className="text-xs text-zinc-500 mt-0.5 break-words">
                              {al.meta?.DATE ?? "—"} · {al.media} · {al.track_count} tracks
                              {al.meta?.["ALBUM DYNAMIC RANGE"] ? ` · DR${al.meta["ALBUM DYNAMIC RANGE"]}` : ""}
                            </div>
                          </div>
                          <GradeBadge pass={!!al.pass && !auditFails(al.audit_summary)} score={al.grade_pct} audit={al.audit_summary} />
                        </div>
                      );
                    })}
                  </div>
                )}
              </section>
            );
          })
        )}
      </section>

      <MoreLikeThis kind="artist" artist={name} mbid={artistMb} />

      {imageOpen && (
        <ArtistImageModal
          artist={decoded}
          onClose={() => setImageOpen(false)}
          onSaved={refresh}
        />
      )}

      {reviewOpen && (
        <MetadataReviewModal
          artist={decoded}
          title="Artist metadata"
          onClose={() => setReviewOpen(false)}
          onSaved={refresh}
        />
      )}
    </div>
  );
}
