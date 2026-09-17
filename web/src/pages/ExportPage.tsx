import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, HardDriveDownload, Library, Search } from "lucide-react";
import { api } from "../api";
import { toast, useStore } from "../store";
import CoverImg, { TrackCover } from "../components/CoverImg";
import { fmtDuration } from "../lib/fmt";
import Segmented from "../components/Segmented";

/** Quality presets per codec — mirrors server/exporter.py CODECS tables.
 * Every transcode codec additionally offers "custom" (raw kbps / Vorbis q),
 * which the backend accepts as a plain number. */
const QUALITY: Record<string, { v: string; label: string }[]> = {
  copy: [],
  flac: Array.from({ length: 9 }, (_, i) => 8 - i).map((lv) => ({
    v: String(lv),
    label: `level ${lv}${lv === 8 ? " (smallest, slowest)" : lv === 5 ? " (balanced)" : lv === 0 ? " (fastest)" : ""}`,
  })),
  mp3: [
    { v: "V0", label: "V0 (~245 kbps VBR, best)" },
    { v: "V1", label: "V1 (~225 kbps VBR)" },
    { v: "V2", label: "V2 (~175 kbps VBR)" },
    { v: "V3", label: "V3 (~155 kbps VBR)" },
    { v: "V4", label: "V4 (~135 kbps VBR)" },
    { v: "V5", label: "V5 (~115 kbps VBR)" },
    { v: "320", label: "320 kbps CBR" },
    { v: "256", label: "256 kbps CBR" },
    { v: "192", label: "192 kbps CBR" },
    { v: "128", label: "128 kbps CBR" },
    { v: "custom", label: "Custom bitrate…" },
  ],
  aac: [
    { v: "320", label: "320 kbps" },
    { v: "256", label: "256 kbps" },
    { v: "192", label: "192 kbps" },
    { v: "128", label: "128 kbps" },
    { v: "custom", label: "Custom bitrate…" },
  ],
  opus: [
    { v: "320", label: "320 kbps" },
    { v: "256", label: "256 kbps" },
    { v: "224", label: "224 kbps" },
    { v: "192", label: "192 kbps" },
    { v: "160", label: "160 kbps" },
    { v: "128", label: "128 kbps" },
    { v: "112", label: "112 kbps" },
    { v: "96", label: "96 kbps" },
    { v: "80", label: "80 kbps" },
    { v: "64", label: "64 kbps" },
    { v: "custom", label: "Custom bitrate…" },
  ],
  vorbis: [
    { v: "q10", label: "q10 (~320 kbps, best)" },
    { v: "q9", label: "q9 (~280 kbps)" },
    { v: "q8", label: "q8 (~256 kbps)" },
    { v: "q7", label: "q7 (~224 kbps)" },
    { v: "q6", label: "q6 (~192 kbps)" },
    { v: "q5", label: "q5 (~160 kbps)" },
    { v: "q4", label: "q4 (~128 kbps)" },
    { v: "q3", label: "q3 (~112 kbps)" },
    { v: "q2", label: "q2 (~96 kbps)" },
    { v: "q1", label: "q1 (~80 kbps)" },
    { v: "q0", label: "q0 (~64 kbps, smallest)" },
    { v: "custom", label: "Custom q…" },
  ],
  wav: [
    { v: "24", label: "24-bit" },
    { v: "16", label: "16-bit (CD)" },
  ],
};

const FALLBACK_CODEC_LABELS: Record<string, string> = {
  copy: "Copy (original codec)",
  flac: "FLAC (lossless)",
  mp3: "MP3",
  aac: "AAC / M4A",
  opus: "Opus",
  vorbis: "Ogg Vorbis",
  wav: "WAV (PCM, uncompressed)",
};

const STRUCTURES = [
  { v: "artist_album", label: "Artist / Album / 01 - Title" },
  { v: "flat", label: "Flat — one folder" },
  { v: "mirror", label: "Mirror library layout" },
];

const SOURCE_KINDS = [
  { id: "playlist", label: "Playlist" },
  { id: "albums", label: "Albums" },
  { id: "artists", label: "Artists" },
  { id: "tracks", label: "Tracks" },
  { id: "library", label: "Entire library" },
] as const;
type SourceKind = (typeof SOURCE_KINDS)[number]["id"];

function fmtGB(n: number | null): string {
  return n === null ? "—" : `${(n / 1024 ** 3).toFixed(1)} GB`;
}

/** Rough constant-bitrate output size for the drive-fit estimate; null =
 * size unknown (copy / FLAC, whose compressed size can't be predicted). */
function estimateKbps(codec: string, quality: string, custom: string): number | null {
  if (codec === "copy" || codec === "flac") return null;
  if (codec === "wav") return quality === "24" ? 2117 : 1411;
  const q = quality === "custom" ? custom : quality;
  const n = parseInt(q.replace(/^[Vq]/i, ""), 10);
  if (codec === "mp3") {
    if (/^V/i.test(quality === "custom" ? "" : quality)) return { 0: 245, 1: 225, 2: 175, 3: 155, 4: 135, 5: 115 }[n] ?? 175;
    return n || 190;
  }
  if (codec === "vorbis") return { 10: 320, 9: 280, 8: 256, 7: 224, 6: 192, 5: 160, 4: 128, 3: 112, 2: 96 }[n] ?? 160;
  return n || 220; // aac / opus
}

/** Export any slice of the library — playlists, albums, artists, single
 * tracks or everything — to a target drive with a codec / bitrate
 * configurator and folder-structure choices. The "put music on my MP3
 * player" feature. */
export default function ExportPage() {
  const { setToast } = useStore();
  const { data: lib } = useQuery({ queryKey: ["library"], queryFn: api.library });
  const { data: playlists } = useQuery({ queryKey: ["playlists"], queryFn: api.playlists });
  const { data: drivesData } = useQuery({ queryKey: ["exportDrives"], queryFn: api.exportDrives });
  const { data: codecLabels } = useQuery({ queryKey: ["exportCodecs"], queryFn: api.exportCodecs });

  const [sourceKind, setSourceKind] = useState<SourceKind>("playlist");
  const [playlistId, setPlaylistId] = useState<number | null>(null);
  const [albumPaths, setAlbumPaths] = useState<Set<string>>(new Set());
  const [artistPaths, setArtistPaths] = useState<Set<string>>(new Set());
  const [trackPaths, setTrackPaths] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState("");
  const [drive, setDrive] = useState("");
  const [subfolder, setSubfolder] = useState("Music");
  const [codec, setCodec] = useState("copy");
  const [quality, setQuality] = useState("");
  const [customKbps, setCustomKbps] = useState("192");
  const [structure, setStructure] = useState("artist_album");
  const [busy, setBusy] = useState(false);

  const artists = useMemo(() => lib?.artists ?? [], [lib]);
  const albums = useMemo(() => artists.flatMap((a: any) => a.albums ?? []), [artists]);

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
      (a) =>
        !q ||
        (a.meta?.ALBUM ?? "").toLowerCase().includes(q) ||
        (a.album_artist ?? a.meta?.ALBUMARTIST ?? a.meta?.ARTIST ?? "").toLowerCase().includes(q)
    );
  }, [albums, filter]);
  const filteredArtists = useMemo(() => {
    const q = filter.toLowerCase();
    return artists.filter((a: any) => !q || (a.display_name || a.name || "").toLowerCase().includes(q));
  }, [artists, filter]);
  const filteredTracks = useMemo(() => {
    const q = filter.toLowerCase();
    return trackRows.filter(
      (t) => !q || t.title.toLowerCase().includes(q) || t.artist.toLowerCase().includes(q) || t.album.toLowerCase().includes(q)
    );
  }, [trackRows, filter]);

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
  const kbps = estimateKbps(codec, quality || QUALITY[codec]?.[0]?.v || "", customKbps);
  const estBytes = kbps !== null ? (totalSeconds * kbps * 1000) / 8 : null;
  const selectedDrive = drivesData?.drives.find((d) => d.root === drive) ?? null;
  const overCapacity = estBytes !== null && selectedDrive?.free != null && estBytes > selectedDrive.free;

  const toggle = (set: Set<string>, path: string, apply: (s: Set<string>) => void) => {
    const next = new Set(set);
    if (next.has(path)) next.delete(path);
    else next.add(path);
    apply(next);
  };

  const run = async () => {
    if (!paths.length) return toast("Select something to export first");
    const destRoot = drivesData?.drives.find((d) => d.root === drive)?.root;
    if (!destRoot) return toast("Choose a destination drive");
    setBusy(true);
    setToast(`Exporting ${paths.length} track(s)…`);
    try {
      const r = await api.exportRun({
        paths, dest: destRoot, subfolder, codec,
        quality: quality === "custom" ? customKbps : quality || QUALITY[codec]?.[0]?.v || "",
        structure,
      });
      const gb = (r.bytes / 1024 ** 3).toFixed(2);
      setToast(
        r.failed
          ? `Export finished with ${r.failed} failure(s): ${r.errors[0] ?? ""}`
          : `Exported ${r.exported} track(s)${r.skipped ? ` (${r.skipped} already there)` : ""} · ${gb} GB`
      );
    } catch (e) {
      toast(String(e));
    } finally {
      setBusy(false);
    }
  };

  const codecLabel = codecLabels?.codecs?.[codec] ?? FALLBACK_CODEC_LABELS[codec] ?? codec;

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
        <HardDriveDownload className="h-6 w-6" /> Export
      </h1>
      <p className="text-xs text-zinc-500 mt-1">
        Copy or convert any part of the library — playlists, albums, artists, single tracks or
        everything — onto a drive. Tags and artwork ride along; already-exported tracks are skipped
        on re-runs.
      </p>

      <div className="grid lg:grid-cols-2 gap-4 mt-5">
        {/* ---- source -------------------------------------------------- */}
        <div className="bg-card rounded-lg border border-border p-4">
          <div className="text-xs font-bold text-zinc-300 mb-2">Source</div>
          <Segmented value={sourceKind} onChange={setSourceKind} options={SOURCE_KINDS} className="mb-3" />

          {sourceKind === "playlist" && (
            <select
              className="input !py-1 text-xs w-full"
              value={playlistId ?? ""}
              onChange={(e) => setPlaylistId(e.target.value ? Number(e.target.value) : null)}
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
                  className="input !py-1 !pl-7 text-xs w-full"
                  placeholder={
                    sourceKind === "albums" ? "Filter albums / artists…"
                    : sourceKind === "artists" ? "Filter artists…"
                    : "Filter tracks / artists / albums…"
                  }
                  value={filter}
                  onChange={(e) => setFilter(e.target.value)}
                />
              </div>
              <div className="max-h-64 overflow-y-auto border border-border rounded-md divide-y divide-border/60">
                {sourceKind === "albums" && filteredAlbums.map((a: any) => (
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
                    <span className="text-zinc-600 shrink-0 break-words">{a.artist}</span>
                  </label>
                ))}
                {sourceKind === "artists" && filteredArtists.map((a: any) => (
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
                    <span className="text-zinc-600 shrink-0">
                      {a.albums?.length ?? 0} albums · {(a.albums ?? []).reduce((n: number, al: any) => n + (al.tracks?.length ?? 0), 0)} tracks
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
                    <span className="text-zinc-600 shrink-0 break-words">{t.artist}</span>
                  </label>
                ))}
                {!((sourceKind === "albums" && filteredAlbums.length) || (sourceKind === "artists" && filteredArtists.length) || (sourceKind === "tracks" && filteredTracks.length)) && (
                  <div className="px-2 py-3 text-[11px] text-zinc-600">Nothing matches.</div>
                )}
              </div>
            </>
          )}

          <div className="text-[11px] text-zinc-500 mt-2 flex items-center gap-2 flex-wrap">
            <span>
              {paths.length} track{paths.length === 1 ? "" : "s"} selected
              {totalSeconds > 0 ? ` · ${fmtDuration(totalSeconds)}` : ""}
            </span>
            {estBytes !== null && totalSeconds > 0 && (
              <span className="text-zinc-600">· ~{(estBytes / 1024 ** 3).toFixed(2)} GB after export</span>
            )}
          </div>

          {/* track preview — same table language as the library views */}
          {paths.length > 0 && (
            <div className="mt-2 max-h-64 overflow-y-auto border border-border rounded-md">
              <table className="w-full text-xs">
                <thead className="border-b border-border sticky top-0 bg-card">
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

        {/* ---- destination + format ------------------------------------ */}
        <div className="bg-card rounded-lg border border-border p-4">
          <div className="text-xs font-bold text-zinc-300 mb-2">Destination</div>
          <select
            className="input !py-1 text-xs w-full"
            value={drive}
            onChange={(e) => setDrive(e.target.value)}
          >
            <option value="">Choose a drive…</option>
            {(drivesData?.drives ?? []).map((d) => (
              <option key={d.root} value={d.root}>
                {d.letter} {d.type !== "fixed" ? `(${d.type})` : ""} — {fmtGB(d.free)} free
              </option>
            ))}
          </select>
          <label className="flex items-center gap-2 mt-2 text-xs text-zinc-300">
            <span className="shrink-0">Subfolder</span>
            <input className="input !py-1 text-xs flex-1" value={subfolder} onChange={(e) => setSubfolder(e.target.value)} />
          </label>
          {overCapacity && (
            <div className="flex items-start gap-2 mt-2 text-[11px] text-amber-300 border border-amber-500/30 bg-amber-500/10 rounded-md p-2">
              <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
              <span>
                Estimated output (~{(estBytes! / 1024 ** 3).toFixed(2)} GB) may not fit this drive
                ({fmtGB(selectedDrive?.free ?? null)} free). Pick fewer tracks or a lower bitrate.
              </span>
            </div>
          )}

          <div className="text-xs font-bold text-zinc-300 mt-4 mb-2">Format</div>
          <div className="grid grid-cols-2 gap-2">
            <label className="text-[10px] text-zinc-500 flex flex-col gap-1">
              Codec
              <select
                className="input !py-1 text-xs"
                value={codec}
                onChange={(e) => {
                  setCodec(e.target.value);
                  setQuality("");
                }}
              >
                {Object.entries(codecLabels?.codecs ?? FALLBACK_CODEC_LABELS).map(([v, l]) => (
                  <option key={v} value={v}>{l}</option>
                ))}
              </select>
            </label>
            <label className="text-[10px] text-zinc-500 flex flex-col gap-1">
              Quality
              <select
                className="input !py-1 text-xs"
                value={quality || QUALITY[codec]?.[0]?.v || ""}
                onChange={(e) => setQuality(e.target.value)}
                disabled={codec === "copy"}
              >
                {/* copy has no knobs — a disabled placeholder keeps the box legible */}
                {(QUALITY[codec] ?? []).length === 0 ? (
                  <option value="">—</option>
                ) : (
                  (QUALITY[codec] ?? []).map((q) => (
                    <option key={q.v} value={q.v}>{q.label}</option>
                  ))
                )}
              </select>
            </label>
          </div>
          {/* custom bitrate / q — per-codec ranges clamp server-side too */}
          {(QUALITY[codec] ?? []).some((q) => q.v === "custom") && quality === "custom" && (
            <label className="flex items-center gap-2 mt-2 text-[10px] text-zinc-500">
              {codec === "vorbis" ? "Custom q (0–10)" : "Custom bitrate (kbps)"}
              <input
                className="input !py-1 text-xs w-24"
                type="number"
                min={codec === "vorbis" ? 0 : 32}
                max={codec === "mp3" ? 320 : codec === "vorbis" ? 10 : 510}
                value={customKbps}
                onChange={(e) => setCustomKbps(e.target.value)}
              />
              {kbps !== null && <span className="text-zinc-600">~{kbps} kbps effective</span>}
            </label>
          )}
          <label className="text-[10px] text-zinc-500 flex flex-col gap-1 mt-2">
            Folder structure
            <select
              className="input !py-1 text-xs w-full"
              value={structure}
              onChange={(e) => setStructure(e.target.value)}
            >
              {STRUCTURES.map((s) => (
                <option key={s.v} value={s.v}>{s.label}</option>
              ))}
            </select>
          </label>

          <button className="btn-primary w-full mt-4 text-xs" disabled={busy || !paths.length} onClick={run}>
            <HardDriveDownload className="h-3.5 w-3.5" />
            {busy ? "Exporting…" : `Export ${paths.length || ""} track${paths.length === 1 ? "" : "s"}`}
          </button>
          <div className="text-[10px] text-zinc-600 mt-2">
            {codec === "copy"
              ? "Copy keeps the original files bit-exact."
              : codec === "flac"
                ? "FLAC → FLAC exports are bit-copies; anything else is re-encoded with ffmpeg and fully re-tagged."
                : `Exporting as ${codecLabel} — files are re-encoded with ffmpeg and fully re-tagged.`}
          </div>
        </div>
      </div>
    </div>
  );
}
