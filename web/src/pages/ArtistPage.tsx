import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { useState } from "react";
import {
  BarChart3, Disc3, ImagePlus, Loader2, Music2, Pencil, Play, RefreshCw, Trash2,
} from "lucide-react";
import { api } from "../api";
import { LinkChips, LinkEditorButton } from "../components/Links";
import { EmptyState, GradeBadge, GradeBar, PageLoading } from "../components/Badges";
import CoverImg from "../components/CoverImg";
import FavHeart from "../components/FavHeart";
import ArtistImageModal from "../components/ArtistImageModal";
import MoreLikeThis from "../components/MoreLikeThis";
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
  // One flag for every in-flight artwork/description write — the buttons in a
  // block share a target, so two at once is always a mistake.
  const [busy, setBusy] = useState<string | null>(null);

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
      toast(String(e));
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
      toast(String(e));
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
      toast(String(e));
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
      toast(String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="p-6 space-y-6">
      <div className="relative rounded-xl border border-border overflow-hidden">
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
        <div className="relative p-5 flex items-start gap-5">
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

          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 min-w-0">
              <h1 className="text-3xl font-bold tracking-tight truncate" title={name}>{name}</h1>
              <FavHeart kind="artist" id={data.path} mbid={artistMbid(data)} />
            </div>
            {data.display_name && data.display_name !== data.name && (
              <div className="text-xs text-zinc-600 mt-0.5" title={data.name}>{data.name}</div>
            )}
            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-zinc-400">
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
            </div>
            {gradeIssues.length > 0 && (
              <ul className="mt-1.5 flex flex-wrap gap-1.5">
                {gradeIssues.map((i) => (
                  <li
                    key={i.code}
                    className="text-[10px] text-red-300/90 bg-red-950/30 border border-red-900/40 rounded px-1.5 py-0.5"
                    title={i.where ? `${i.label} — ${i.where}` : i.label}
                  >
                    {i.label}
                  </li>
                ))}
              </ul>
            )}
            <div className="mt-2 flex items-center gap-1.5">
              <LinkChips tags={artistTags} />
            </div>
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <button className="btn-primary" onClick={() => playNow(allTracks)} title={`Play all ${allTracks.length} tracks`}>
                <Play className="h-4 w-4 fill-current" /> Play all
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
              <LinkEditorButton mode="artist" paths={allTracks.map((t) => t.path)} current={artistTags} />
              <button className="btn-ghost" onClick={() => setStatsOpen(true)}>
                <BarChart3 className="h-4 w-4" /> Stats
              </button>
            </div>
            {!imageUrl && (
              <div className="mt-2 text-xs text-zinc-500">
                No artist image yet —{" "}
                <button className="text-accent-soft hover:underline" onClick={() => setImageOpen(true)}>
                  look for one or upload your own
                </button>
                .
              </div>
            )}
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

      <div className="bg-card rounded-lg border border-border p-4">
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

      <section className="space-y-2">
        <div className="flex items-baseline gap-2">
          <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Albums</h2>
          <span className="text-[10px] text-zinc-600">{data.albums.length}</span>
        </div>
        {data.albums.length === 0 ? (
          <div className="flex items-center gap-2 px-3 py-6 text-xs text-zinc-500 bg-card border border-dashed border-border rounded-lg">
            <Disc3 className="h-4 w-4 text-zinc-600" /> No albums under this artist folder yet.
          </div>
        ) : (
          <div className="space-y-1">
            {data.albums.map((al) => {
              const albumName = al.meta?.ALBUM ?? al.path.split("/").pop() ?? "";
              const href = albumRef(al);
              return (
                <div
                  key={al.path}
                  className="px-3 py-3 rounded-lg hover:bg-white/[0.06] transition-colors flex items-center gap-4 group"
                >
                  <Link to={href} className="shrink-0" title={`Open ${albumName}`}>
                    <CoverImg
                      albumPath={al.path}
                      coverFile={al.cover_file}
                      wrapperClass="h-14 w-14 rounded-md bg-raise border border-border overflow-hidden shrink-0 group-hover:border-zinc-600 transition-colors"
                    />
                  </Link>
                  <GradeBar pct={al.grade_pct} />
                  <div className="flex-1 min-w-0">
                    <Link to={href} className="font-semibold hover:text-accent-soft transition-colors" title={albumName}>
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

      <MoreLikeThis kind="artist" artist={name} mbid={artistMbid(data)} />

      {imageOpen && (
        <ArtistImageModal
          artist={decoded}
          onClose={() => setImageOpen(false)}
          onSaved={refresh}
        />
      )}
    </div>
  );
}
