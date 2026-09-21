import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { HardDriveDownload, Library, Search } from "lucide-react";
import { api } from "../api";
import CoverImg, { TrackCover } from "../components/CoverImg";
import { fmtDuration } from "../lib/fmt";
import Segmented from "../components/Segmented";
import PageHeader from "../components/PageHeader";
import { EmptyState } from "../components/Badges";
import { ExportOptionsPanel, useExportOptions } from "../components/ExportDialog";
import type { Album, Artist } from "../types";

const SOURCE_KINDS = [
  { id: "playlist", label: "Playlist" },
  { id: "albums", label: "Albums" },
  { id: "artists", label: "Artists" },
  { id: "tracks", label: "Tracks" },
  { id: "library", label: "Entire library" },
] as const;
type SourceKind = (typeof SOURCE_KINDS)[number]["id"];

/** Export any slice of the library — playlists, albums, artists, single
 * tracks or everything — to a target drive with a codec / bitrate
 * configurator and folder-structure choices. The "put music on my MP3
 * player" feature.
 *
 * This page owns the SOURCE half (what to export); the destination, format
 * and tag options are the shared ExportOptionsPanel, so the per-page Export
 * dialog offers exactly the same surface. */
export default function ExportPage() {
  const { data: lib } = useQuery({ queryKey: ["library"], queryFn: api.library });
  const { data: playlists } = useQuery({ queryKey: ["playlists"], queryFn: api.playlists });

  const [sourceKind, setSourceKind] = useState<SourceKind>("playlist");
  const [playlistId, setPlaylistId] = useState<number | null>(null);
  const [albumPaths, setAlbumPaths] = useState<Set<string>>(new Set());
  const [artistPaths, setArtistPaths] = useState<Set<string>>(new Set());
  const [trackPaths, setTrackPaths] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState("");

  const artists = useMemo<Artist[]>(() => lib?.artists ?? [], [lib]);
  const albums = useMemo(() => artists.flatMap((a) => a.albums ?? []), [artists]);

  // One flat track metadata table for every source kind / the preview.
  const trackRows = useMemo(() => {
    const rows: {
      path: string; title: string; artist: string; album: string;
      albumPath: string; dur?: number; num?: number | null;
      coverFile?: string | null; albumCover?: string | null;
    }[] = [];
    for (const a of lib?.artists ?? [])
      for (const al of a.albums)
        for (const t of al.tracks)
          rows.push({
            path: t.path,
            title: t.tags.TITLE ?? t.file,
            artist: al.album_artist || a.name || "",
            album: al.meta?.ALBUM ?? al.path.split(/[\\/]/).pop() ?? "",
            albumPath: al.path,
            dur: t.tech?.length ?? undefined,
            num: t.tracknumber ?? null,
            coverFile: t.cover_file ?? null,
            albumCover: al.cover_file ?? null,
          });
    return rows;
  }, [lib]);
  const trackByPath = useMemo(() => new Map(trackRows.map((r) => [r.path, r])), [trackRows]);

  const filteredAlbums = useMemo(() => {
    const q = filter.toLowerCase();
    return albums.filter(
      (a: Album) =>
        !q ||
        (a.meta?.ALBUM ?? "").toLowerCase().includes(q) ||
        (a.album_artist ?? a.meta?.ALBUMARTIST ?? a.meta?.ARTIST ?? "").toLowerCase().includes(q)
    );
  }, [albums, filter]);
  const filteredArtists = useMemo(() => {
    const q = filter.toLowerCase();
    return artists.filter((a: Artist) => !q || (a.display_name || a.name || "").toLowerCase().includes(q));
  }, [artists, filter]);
  const filteredTracks = useMemo(() => {
    const q = filter.toLowerCase();
    return trackRows.filter(
      (t) => !q || t.title.toLowerCase().includes(q) || t.artist.toLowerCase().includes(q) || t.album.toLowerCase().includes(q)
    );
  }, [trackRows, filter]);

  // Bulk selection over the FILTERED list: "All" means "everything the filter
  // shows", the only reading that cannot surprise after a search.
  const listKeys = useMemo(() => {
    if (sourceKind === "albums") return filteredAlbums.map((a) => a.path);
    if (sourceKind === "artists") return filteredArtists.map((a) => a.path);
    if (sourceKind === "tracks") return filteredTracks.map((t) => t.path);
    return [];
  }, [sourceKind, filteredAlbums, filteredArtists, filteredTracks]);

  const bulkSelect = (on: boolean) => {
    const apply = sourceKind === "albums" ? setAlbumPaths
      : sourceKind === "artists" ? setArtistPaths : setTrackPaths;
    apply((prev) => {
      const next = new Set(prev);
      for (const key of listKeys) {
        if (on) next.add(key);
        else next.delete(key);
      }
      return next;
    });
  };

  const { data: playlistDetail } = useQuery({
    queryKey: ["playlist", playlistId],
    queryFn: () => api.playlist(playlistId!),
    enabled: sourceKind === "playlist" && playlistId !== null,
  });

  // Resolve the exact track paths for the chosen source.
  const paths = useMemo(() => {
    if (sourceKind === "playlist") return playlistDetail?.tracks ?? [];
    if (sourceKind === "library") return trackRows.map((t) => t.path);
    if (sourceKind === "albums") {
      const out: string[] = [];
      for (const a of albums) if (albumPaths.has(a.path)) for (const t of a.tracks ?? []) out.push(t.path);
      return out;
    }
    if (sourceKind === "artists") {
      const out: string[] = [];
      for (const a of artists) if (artistPaths.has(a.path)) for (const al of a.albums ?? []) for (const t of al.tracks ?? []) out.push(t.path);
      return out;
    }
    return trackRows.filter((t) => trackPaths.has(t.path)).map((t) => t.path);
  }, [sourceKind, playlistDetail, albums, albumPaths, artists, artistPaths, trackPaths, trackRows]);

  const totalSeconds = useMemo(
    () => paths.reduce((sum, p) => sum + (trackByPath.get(p)?.dur ?? 0), 0),
    [paths, trackByPath]
  );

  // The destination/format half lives in the shared panel, driven by this
  // exact selection.
  const e = useExportOptions(paths, totalSeconds);

  const toggle = (set: Set<string>, path: string, apply: (s: Set<string>) => void) => {
    const next = new Set(set);
    if (next.has(path)) next.delete(path);
    else next.add(path);
    apply(next);
  };

  return (
    <div className="p-6 space-y-5 mx-auto max-w-6xl">
      <PageHeader
        icon={HardDriveDownload}
        title="Export"
        subtitle="Copy or convert any part of the library — playlists, albums, artists, single tracks or everything — onto a drive. Tags and artwork ride along; already-exported tracks are skipped on re-runs."
      />

      <div className="grid lg:grid-cols-2 gap-4">
        {/* ---- source -------------------------------------------------- */}
        {/* `min-w-0`: as a grid item the panel's automatic minimum is its
            content's min-content, which the tab strip below sets — and then
            the strip grows the panel instead of scrolling inside its own box
            on a phone. */}
        <div className="panel min-w-0">
          <div className="text-xs font-bold text-zinc-300 mb-2">Source</div>
          {/* Five options are wider than a phone: the strip scrolls in its own
              box instead of pushing the page sideways. */}
          <div className="overflow-x-auto">
            <Segmented value={sourceKind} onChange={setSourceKind} options={SOURCE_KINDS} className="mb-3" />
          </div>

          {sourceKind === "playlist" && (
            <select
              className="input !py-1 text-xs w-full min-w-0 tap"
              value={playlistId ?? ""}
              onChange={(ev) => setPlaylistId(ev.target.value ? Number(ev.target.value) : null)}
            >
              <option value="">Choose a playlist…</option>
              {(playlists ?? []).map((pl) => (
                <option key={pl.id} value={pl.id}>
                  {pl.name} ({pl.track_count})
                </option>
              ))}
            </select>
          )}

          {sourceKind === "library" && (
            <div className="flex items-start gap-2 text-xs text-zinc-400 border border-border rounded-md p-3 bg-panel/50">
              <Library className="h-4 w-4 text-accent shrink-0 mt-0.5" />
              <span>
                Every playable track in the library — <b className="text-zinc-200">{trackRows.length} tracks</b>{" "}
                across {albums.length} albums by {artists.length} artists.
              </span>
            </div>
          )}

          {(sourceKind === "albums" || sourceKind === "artists" || sourceKind === "tracks") && (
            <>
              <div className="relative mb-2">
                <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-zinc-500" />
                <input
                  className="input !py-1 !pl-7 text-xs w-full min-w-0 tap"
                  placeholder={
                    sourceKind === "albums" ? "Filter albums / artists…"
                    : sourceKind === "artists" ? "Filter artists…"
                    : "Filter tracks / artists / albums…"
                  }
                  value={filter}
                  onChange={(ev) => setFilter(ev.target.value)}
                />
              </div>
              <div className="flex items-center gap-2 mb-2 text-[11px]">
                <button className="btn !py-0.5 !px-2 text-[11px] tap" onClick={() => bulkSelect(true)}>
                  Select all{filter ? " matching" : ""} ({listKeys.length})
                </button>
                <button className="btn !py-0.5 !px-2 text-[11px] tap" onClick={() => bulkSelect(false)}>
                  Clear
                </button>
              </div>
              {listKeys.length === 0 && (
                <EmptyState
                  title="Nothing matches"
                  hint={
                    filter.trim()
                      ? `No library entry matches "${filter.trim()}".`
                      : "The library holds nothing to export yet."
                  }
                />
              )}
              {listKeys.length > 0 && (
              <div className="stagger max-h-64 overflow-y-auto border border-border rounded-md divide-y divide-border/60">
                {sourceKind === "albums" && filteredAlbums.map((a) => (
                  <label
                    key={a.path}
                    className="flex items-center gap-2 px-2 py-1.5 text-xs text-zinc-300 hover:bg-panel cursor-pointer"
                  >
                    <input
                      type="checkbox"
                      checked={albumPaths.has(a.path)}
                      onChange={() => toggle(albumPaths, a.path, setAlbumPaths)}
                    />
                    <CoverImg albumPath={a.path} coverFile={a.cover_file} wrapperClass="h-6 w-6 rounded bg-raise border border-border overflow-hidden shrink-0" />
                    <span className="break-words flex-1 min-w-0">{a.meta?.ALBUM ?? a.path}</span>
                    <span className="max-w-[45%] truncate text-zinc-600 shrink-0">{a.album_artist ?? a.meta?.ALBUMARTIST ?? a.meta?.ARTIST ?? ""}</span>
                  </label>
                ))}
                {sourceKind === "artists" && filteredArtists.map((a) => (
                  <label
                    key={a.path}
                    className="flex items-center gap-2 px-2 py-1.5 text-xs text-zinc-300 hover:bg-panel cursor-pointer"
                  >
                    <input
                      type="checkbox"
                      checked={artistPaths.has(a.path)}
                      onChange={() => toggle(artistPaths, a.path, setArtistPaths)}
                    />
                    <span className="break-words flex-1 min-w-0">{a.display_name || a.name}</span>
                    <span className="hidden sm:block text-zinc-600 shrink-0">
                      {a.albums?.length ?? 0} albums · {(a.albums ?? []).reduce((n, al) => n + (al.tracks?.length ?? 0), 0)} tracks
                    </span>
                  </label>
                ))}
                {sourceKind === "tracks" && filteredTracks.map((t) => (
                  <label
                    key={t.path}
                    className="flex items-center gap-2 px-2 py-1.5 text-xs text-zinc-300 hover:bg-panel cursor-pointer"
                  >
                    <input
                      type="checkbox"
                      checked={trackPaths.has(t.path)}
                      onChange={() => toggle(trackPaths, t.path, setTrackPaths)}
                    />
                    <span className="break-words flex-1 min-w-0">{t.title}</span>
                    <span className="max-w-[45%] truncate text-zinc-600 shrink-0">{t.artist}</span>
                  </label>
                ))}
              </div>
              )}
            </>
          )}

          {/* track preview — same table language as the library views */}
          {paths.length > 0 && (
            <div className="mt-2 max-h-64 overflow-y-auto border border-border rounded-md table-scroll">
              <table className="w-full text-xs">
                <thead className="border-b border-border sticky top-12 bg-card">
                  <tr>
                    <th className="th !py-1 w-8">#</th>
                    <th className="th !py-1 w-10"><span className="sr-only">Cover</span></th>
                    <th className="th !py-1">Title</th>
                    <th className="th !py-1 hidden sm:table-cell">Artist</th>
                    <th className="th !py-1 hidden md:table-cell">Album</th>
                    <th className="th !py-1 w-14 text-right">Dur</th>
                  </tr>
                </thead>
                <tbody>
                  {paths.slice(0, 200).map((p, i) => {
                    const m = trackByPath.get(p);
                    return (
                      <tr key={`${p}-${i}`} className="table-row">
                        <td className="td !py-1 text-zinc-600 tabular-nums">{m?.num ?? i + 1}</td>
                        <td className="td !py-1 pl-0">
                          <TrackCover
                            albumPath={m?.albumPath ?? p.split(/[\\/]/).slice(0, -1).join("/")}
                            trackCover={m?.coverFile}
                            albumCover={m?.albumCover}
                            wrapperClass="h-7 w-7 rounded bg-raise border border-border overflow-hidden shrink-0"
                          />
                        </td>
                        <td className="td !py-1 break-words min-w-0">{m?.title ?? p.split(/[\\/]/).pop()}</td>
                        <td className="td !py-1 text-zinc-500 break-words hidden sm:table-cell">{m?.artist}</td>
                        <td className="td !py-1 text-zinc-500 break-words hidden md:table-cell">{m?.album}</td>
                        <td className="td !py-1 text-zinc-500 tabular-nums text-right">{m?.dur ? fmtDuration(m.dur) : "—"}</td>
                      </tr>
                    );
                  })}
                  {paths.length > 200 && (
                    <tr className="table-row">
                      <td colSpan={6} className="td !py-1.5 text-[11px] text-zinc-600">+{paths.length - 200} more…</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* ---- destination + format (the shared option surface) --------- */}
        <div className="panel min-w-0">
          <ExportOptionsPanel e={e} />
        </div>
      </div>
    </div>
  );
}
