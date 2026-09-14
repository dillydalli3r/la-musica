import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { useState } from "react";
import { Music2, BarChart3 } from "lucide-react";
import { api } from "../api";
import { LinkChips, LinkEditorButton } from "../components/Links";
import { EmptyState, GradeBadge, GradeBar, PageLoading } from "../components/Badges";
import CoverImg from "../components/CoverImg";
import FavHeart from "../components/FavHeart";
import { albumRef, artistMbid } from "../lib/refs";
import { auditFails } from "../lib/status";
import StatsPanel from "../components/StatsPanel";
import { useStore } from "../store";

export default function ArtistPage() {
  const { path = "" } = useParams();
  const decoded = decodeURIComponent(path);
  const { data, isLoading, error } = useQuery({
    queryKey: ["artist", decoded],
    queryFn: () => api.artist(decoded),
  });
  const { playNow } = useStore();
  const [statsOpen, setStatsOpen] = useState(false);

  if (error)
    return (
      <EmptyState
        title="Artist not found"
        hint="The artist folder may have been renamed, moved or deleted."
        action={{ label: "Back to the library", to: "/library" }}
      />
    );
  if (isLoading || !data) return <PageLoading label="Loading artist…" />;

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

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-start gap-5">
        <div className="h-24 w-24 rounded-xl bg-gradient-to-br from-accent/40 to-accent/10 border border-border flex items-center justify-center shrink-0">
          <Music2 className="h-10 w-10 text-zinc-400" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 min-w-0">
            <h1 className="text-3xl font-bold tracking-tight truncate">{data.display_name || data.name}</h1>
            <FavHeart kind="artist" id={data.path} mbid={artistMbid(data)} />
          </div>
          {data.display_name && data.display_name !== data.name && (
            <div className="text-xs text-zinc-600 mt-0.5">{data.name}</div>
          )}
          <div className="mt-1 flex items-center gap-3 text-sm text-zinc-400">
            <span>
              {data.aggregate.album_count} album{data.aggregate.album_count === 1 ? "" : "s"} · {data.aggregate.track_count} track{data.aggregate.track_count === 1 ? "" : "s"}
            </span>
            <GradeBadge
              pass={(data.aggregate.grade_pct ?? 0) >= 100 && !auditFails(data.aggregate.audit_summary)}
              score={data.aggregate.grade_pct}
              audit={data.aggregate.audit_summary}
            />
          </div>
          <div className="mt-2 flex items-center gap-1.5">
            <LinkChips tags={artistTags} />
          </div>
        </div>
        <button className="btn-primary" onClick={() => playNow(allTracks)}>
          Play all
        </button>
        <LinkEditorButton mode="artist" paths={allTracks.map((t) => t.path)} current={artistTags} />
        <button className="btn-ghost" onClick={() => setStatsOpen(true)}>
          <BarChart3 className="h-4 w-4" /> Stats
        </button>
      </div>

      {statsOpen && (
        <StatsPanel
          title={data.name}
          albums={data.albums}
          tracks={allTracks.map((t) => {
            const al = data.albums.find((a) => a.tracks.some((x) => x.path === t.path));
            const tr = al?.tracks.find((x) => x.path === t.path);
            return { ...tr, tags: tr?.tags ?? {} } as any;
          })}
          onClose={() => setStatsOpen(false)}
        />
      )}

      <div className="space-y-3">
        {data.albums.map((al) => (
          <div key={al.path} className="px-3 py-3 rounded-lg hover:bg-white/[0.06] transition-colors flex items-center gap-4">
            <CoverImg
              albumPath={al.path}
              coverFile={al.cover_file}
              wrapperClass="h-14 w-14 rounded-md bg-raise border border-border overflow-hidden shrink-0"
            />
            <GradeBar pct={al.grade_pct} />
            <div className="flex-1 min-w-0">
              <Link to={albumRef(al)} className="font-semibold hover:text-accent-soft">
                {al.meta?.ALBUM ?? al.path.split("/").pop()}
              </Link>
              <div className="text-xs text-zinc-500 mt-0.5 break-words">
                {al.meta?.DATE ?? "—"} · {al.media} · {al.track_count} tracks
                {al.meta?.["ALBUM DYNAMIC RANGE"] ? ` · DR${al.meta["ALBUM DYNAMIC RANGE"]}` : ""}
              </div>
            </div>
            <GradeBadge pass={!!al.pass && !auditFails(al.audit_summary)} score={al.grade_pct} audit={al.audit_summary} />
          </div>
        ))}
      </div>
    </div>
  );
}