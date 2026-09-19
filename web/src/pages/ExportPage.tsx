import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, HardDriveDownload, Library, RotateCcw, Save, Search } from "lucide-react";
import { api } from "../api";
import type { ExportCodecSpec, ExportForm } from "../api";
import { toast } from "../store";
import CoverImg, { TrackCover } from "../components/CoverImg";
import { fmtDuration } from "../lib/fmt";
import Segmented from "../components/Segmented";
import PageHeader from "../components/PageHeader";
import { EmptyState } from "../components/Badges";

/** The dropdown's synthetic entry for a codec's "custom value" field; the
 * backend takes the plain number the field holds (kbps, or 0-10 for Vorbis),
 * so nothing but the form needs to know the word. */
const CUSTOM = "custom";

const STRUCTURES = [
  { v: "artist_album", label: "Artist / Album / 01 - Title" },
  { v: "album", label: "Album / 01 - Title" },
  { v: "flat", label: "Flat — one folder" },
  { v: "mirror", label: "Mirror library layout" },
];

/** Rendered while the saved defaults are still loading; the same shape and the
 * same first-run values the backend ships. */
const BLANK_FORM: ExportForm = {
  dest: "",
  subfolder: "Music",
  codec: "copy",
  quality: "",
  structure: "artist_album",
  embed_covers: true,
  embed_cover_jpeg_quality: 90,
  embed_cover_resolution: 1200,
  id3v2: "2.3",
  id3v1: false,
  replaygain: false,
  clean_tags: true,
  playlists: true,
  sidecars: true,
  verify: true,
  prune: false,
  workers: 0,
};

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

/** Effective kbps for the drive-fit estimate, from the server's own preset
 * hints (server/exporter.py CODECS). null = unpredictable: a bit-exact copy,
 * a lossless re-encode, or a custom Vorbis q. */
function effectiveKbps(spec: ExportCodecSpec | undefined, quality: string, custom: string): number | null {
  if (!spec) return null;
  const preset = spec.presets.find((p) => p.v === quality);
  if (preset) return preset.kbps;
  if (spec.custom && spec.custom.mode === "kbps") {
    const n = parseInt(quality === CUSTOM ? custom : quality, 10);
    if (!Number.isFinite(n)) return null;
    return Math.min(spec.custom.max, Math.max(spec.custom.min, n));
  }
  return null;
}

/** One option row: the checkbox plus its one-line explanation. The
 * compatibility switches are numerous enough that bare checkbox rows would
 * not say what they do. */
function Opt({ checked, onChange, label, hint, danger }: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  hint: string;
  danger?: boolean;
}) {
  return (
    <label className="flex items-start gap-2 mt-2 text-xs text-zinc-300 cursor-pointer">
      <input
        type="checkbox"
        className="mt-0.5"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="min-w-0">
        <span className={"block " + (danger ? "text-amber-300" : "")}>{label}</span>
        <span className="block text-[10px] text-zinc-600">{hint}</span>
      </span>
    </label>
  );
}

/** Export any slice of the library — playlists, albums, artists, single
 * tracks or everything — to a target drive with a codec / bitrate
 * configurator and folder-structure choices. The "put music on my MP3
 * player" feature. */
export default function ExportPage() {
  const { data: lib } = useQuery({ queryKey: ["library"], queryFn: api.library });
  const { data: playlists } = useQuery({ queryKey: ["playlists"], queryFn: api.playlists });
  const { data: drivesData } = useQuery({ queryKey: ["exportDrives"], queryFn: api.exportDrives });
  const { data: specs } = useQuery({ queryKey: ["exportCodecs"], queryFn: api.exportCodecs });
  const { data: savedDefaults } = useQuery({ queryKey: ["exportDefaults"], queryFn: api.exportDefaults });
  const queryClient = useQueryClient();

  const [sourceKind, setSourceKind] = useState<SourceKind>("playlist");
  const [playlistId, setPlaylistId] = useState<number | null>(null);
  const [albumPaths, setAlbumPaths] = useState<Set<string>>(new Set());
  const [artistPaths, setArtistPaths] = useState<Set<string>>(new Set());
  const [trackPaths, setTrackPaths] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState("");
  const [form, setForm] = useState<ExportForm | null>(null);
  const [customValue, setCustomValue] = useState("192");
  const [busy, setBusy] = useState(false);

  // What the form shows (and what a run sends): the user's edits, else the
  // saved `export_*` config values, else the shipped defaults. An export uses
  // exactly what is displayed; nothing is written back to config until "Save
  // as default" is pressed.
  const f = form ?? savedDefaults ?? BLANK_FORM;
  const set = <K extends keyof ExportForm>(key: K, value: ExportForm[K]) =>
    setForm({ ...f, [key]: value });

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

  // Bulk selection over the FILTERED list: "All" means "everything the filter
  // shows", the only reading that cannot surprise after a search.
  const listKeys = useMemo(() => {
    if (sourceKind === "albums") return filteredAlbums.map((a: any) => a.path);
    if (sourceKind === "artists") return filteredArtists.map((a: any) => a.path);
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
  const spec = specs?.codecs?.[f.codec];
  const quality = f.quality;
  const kbps = effectiveKbps(spec, quality, customValue);
  const estBytes = kbps !== null ? (totalSeconds * kbps * 1000) / 8 : null;
  const selectedDrive = drivesData?.drives.find((d) => d.root === f.dest) ?? null;
  const overCapacity = estBytes !== null && selectedDrive?.free != null && estBytes > selectedDrive.free;

  const toggle = (set: Set<string>, path: string, apply: (s: Set<string>) => void) => {
    const next = new Set(set);
    if (next.has(path)) next.delete(path);
    else next.add(path);
    apply(next);
  };

  const run = async () => {
    if (!paths.length) return toast("Select something to export first");
    const destRoot = drivesData?.drives.find((d) => d.root === f.dest)?.root;
    if (!destRoot) return toast("Choose a destination drive");
    setBusy(true);
    toast(`Exporting ${paths.length} track(s)…`);
    try {
      const r = await api.exportRun({
        ...f,
        dest: destRoot,
        quality: quality === CUSTOM ? customValue : quality || spec?.default || "",
        paths,
      });
      const gb = (r.bytes / 1024 ** 3).toFixed(2);
      const extras = [
        r.skipped ? `${r.skipped} already there` : "",
        r.playlists ? `${r.playlists} playlist(s)` : "",
        r.sidecars ? `${r.sidecars} sidecar file(s)` : "",
        r.pruned ? `${r.pruned} removed from the device` : "",
      ].filter(Boolean).join(" · ");
      if (r.failed) toast.error(`Export finished with ${r.failed} failure(s): ${r.errors[0] ?? ""}`);
      else toast.success(`Exported ${r.exported} track(s)${extras ? ` (${extras})` : ""} · ${gb} GB`);
      if (r.warnings?.length) toast(r.warnings[0]);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const saveDefaults = async () => {
    try {
      await api.exportSaveDefaults(f);
      queryClient.invalidateQueries({ queryKey: ["exportDefaults"] });
      queryClient.invalidateQueries({ queryKey: ["config"] });
      toast.success("Export defaults saved");
    } catch (e) {
      toast.error(String(e));
    }
  };

  const resetDefaults = () => {
    if (savedDefaults) setForm(savedDefaults);
    toast("Form reset to the saved defaults");
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
        <div className="panel">
          <div className="text-xs font-bold text-zinc-300 mb-2">Source</div>
          {/* Five options are wider than a phone: the strip scrolls in its own
              box instead of pushing the page sideways. */}
          <div className="overflow-x-auto">
            <Segmented value={sourceKind} onChange={setSourceKind} options={SOURCE_KINDS} className="mb-3" />
          </div>

          {sourceKind === "playlist" && (
            <select
              className="input !py-1 text-xs w-full min-h-8 sm:min-h-0"
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
                  className="input !py-1 !pl-7 text-xs w-full min-h-8 sm:min-h-0"
                  placeholder={
                    sourceKind === "albums" ? "Filter albums / artists…"
                    : sourceKind === "artists" ? "Filter artists…"
                    : "Filter tracks / artists / albums…"
                  }
                  value={filter}
                  onChange={(e) => setFilter(e.target.value)}
                />
              </div>
              <div className="flex items-center gap-2 mb-2 text-[11px]">
                <button className="btn !py-0.5 !px-2 text-[11px] min-h-8 sm:min-h-0" onClick={() => bulkSelect(true)}>
                  Select all{filter ? " matching" : ""} ({listKeys.length})
                </button>
                <button className="btn !py-0.5 !px-2 text-[11px] min-h-8 sm:min-h-0" onClick={() => bulkSelect(false)}>
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
                    <span className="max-w-[45%] truncate text-zinc-600 shrink-0">{a.artist}</span>
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
                    <span className="hidden sm:block text-zinc-600 shrink-0">
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
                    <span className="max-w-[45%] truncate text-zinc-600 shrink-0">{t.artist}</span>
                  </label>
                ))}
              </div>
              )}
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

        {/* ---- destination + format ------------------------------------ */}
        <div className="panel">
          <div className="flex items-center justify-between mb-2">
            <div className="text-xs font-bold text-zinc-300">Destination</div>
            <button
              className="btn !py-0.5 !px-2 text-[11px] min-h-8 sm:min-h-0"
              onClick={() => queryClient.invalidateQueries({ queryKey: ["exportDrives"] })}
              title="Rescan the drives (a device plugged in after the page opened)"
            >
              <RotateCcw className="h-3 w-3" />
              Rescan
            </button>
          </div>
          <select
            className="input !py-1 text-xs w-full min-h-8 sm:min-h-0"
            value={f.dest}
            onChange={(e) => set("dest", e.target.value)}
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
            <input className="input !py-1 text-xs flex-1 min-h-8 sm:min-h-0" value={f.subfolder} onChange={(e) => set("subfolder", e.target.value)} />
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
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            <label className="text-[10px] text-zinc-500 flex flex-col gap-1">
              Codec
              <select
                className="input !py-1 text-xs min-h-8 sm:min-h-0"
                value={f.codec}
                onChange={(e) => {
                  set("codec", e.target.value);
                  set("quality", "");
                }}
              >
                {Object.entries(specs?.codecs ?? {}).map(([v, cs]) => (
                  <option key={v} value={v}>{cs.label}</option>
                ))}
              </select>
            </label>
            <label className="text-[10px] text-zinc-500 flex flex-col gap-1">
              Quality
              <select
                className="input !py-1 text-xs min-h-8 sm:min-h-0"
                value={quality || spec?.default || ""}
                onChange={(e) => set("quality", e.target.value)}
                disabled={!spec?.presets.length && !spec?.custom}
              >
                {/* copy has no knobs — a disabled placeholder keeps the box legible */}
                {!spec?.presets.length && !spec?.custom ? (
                  <option value="">—</option>
                ) : (
                  <>
                    {spec.presets.map((q) => (
                      <option key={q.v} value={q.v}>{q.label}</option>
                    ))}
                    {spec.custom && (
                      <option value={CUSTOM}>
                        {spec.custom.mode === "q" ? "Custom q…" : "Custom bitrate…"}
                      </option>
                    )}
                  </>
                )}
              </select>
            </label>
          </div>
          {/* custom bitrate / q — the server clamps to the same range again */}
          {spec?.custom && quality === CUSTOM && (
            <label className="flex flex-wrap items-center gap-2 mt-2 text-[10px] text-zinc-500">
              {spec.custom.mode === "q"
                ? `Custom q (${spec.custom.min}–${spec.custom.max})`
                : `Custom bitrate (${spec.custom.min}–${spec.custom.max} kbps)`}
              <input
                className="input !py-1 text-xs w-24 min-h-8 sm:min-h-0"
                type="number"
                min={spec.custom.min}
                max={spec.custom.max}
                value={customValue}
                onChange={(e) => setCustomValue(e.target.value)}
              />
              {kbps !== null && <span className="text-zinc-600">~{kbps} kbps effective</span>}
            </label>
          )}
          <label className="text-[10px] text-zinc-500 flex flex-col gap-1 mt-2">
            Folder structure
            <select
              className="input !py-1 text-xs w-full min-h-8 sm:min-h-0"
              value={f.structure}
              onChange={(e) => set("structure", e.target.value)}
            >
              {STRUCTURES.map((st) => (
                <option key={st.v} value={st.v}>{st.label}</option>
              ))}
            </select>
          </label>
          <div className="text-[10px] text-zinc-600 mt-1">
            A multi-disc album gets a &quot;1-01 - Title&quot; file name, so the two discs
            cannot collide.
          </div>

          {/* ---- artwork, tags, extras -------------------------------- */}
          <div className="text-xs font-bold text-zinc-300 mt-4 mb-2">Artwork &amp; tags</div>
          <Opt
            checked={f.embed_covers}
            onChange={(v) => set("embed_covers", v)}
            label="Embed cover art into the exported files"
            hint="The album's cover.* (or the file's own art when the folder has none) is embedded, re-encoded at the quality below. Off leaves art exactly as the source had it."
          />
          {f.embed_covers && (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 mt-1 pl-6">
              <label className="text-[10px] text-zinc-500 flex flex-col gap-1">
                Embedded JPEG quality — {f.embed_cover_jpeg_quality}
                <input
                  type="range"
                  min={60}
                  max={100}
                  value={f.embed_cover_jpeg_quality}
                  onChange={(e) => set("embed_cover_jpeg_quality", Number(e.target.value))}
                />
              </label>
              <label className="text-[10px] text-zinc-500 flex flex-col gap-1">
                Max resolution (px, 0 = original)
                <input
                  className="input !py-1 text-xs min-h-8 sm:min-h-0"
                  type="number"
                  min={0}
                  max={4000}
                  value={f.embed_cover_resolution}
                  onChange={(e) => set("embed_cover_resolution", Number(e.target.value))}
                />
              </label>
            </div>
          )}
          <Opt
            checked={f.clean_tags}
            onChange={(v) => set("clean_tags", v)}
            label="Write only the canonical tag set"
            hint="Transcodes drop the source's leftover frames instead of carrying them along beside the tags this app writes."
          />
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 mt-2 items-end">
            <label className="text-[10px] text-zinc-500 flex flex-col gap-1">
              ID3 version (MP3)
              <select
                className="input !py-1 text-xs min-h-8 sm:min-h-0"
                value={f.id3v2}
                onChange={(e) => set("id3v2", e.target.value)}
              >
                <option value="2.3">2.3 — older players, car stereos</option>
                <option value="2.4">2.4 — newest frames</option>
              </select>
            </label>
            <Opt
              checked={f.id3v1}
              onChange={(v) => set("id3v1", v)}
              label="Also write ID3v1"
              hint="For players that read nothing else (short, latin-1 fields)."
            />
          </div>
          <Opt
            checked={f.replaygain}
            onChange={(v) => set("replaygain", v)}
            label="Write ReplayGain tags"
            hint="Measures each track (ffmpeg EBU R128, one pass that rides along with the transcode) and stores track + album gain/peak, so the player matches your library's loudness."
          />
          <Opt
            checked={f.playlists}
            onChange={(v) => set("playlists", v)}
            label="Write .m3u8 playlists"
            hint="One per exported album, plus all.m3u8 for the whole export — UTF-8 with relative paths and durations."
          />
          <Opt
            checked={f.sidecars}
            onChange={(v) => set("sidecars", v)}
            label="Copy covers, lyrics, cue, log and descriptions"
            hint="cover.*, description.txt, .lrc, .cue, .log and the artist image travel with the tracks."
          />
          <Opt
            checked={f.verify}
            onChange={(v) => set("verify", v)}
            label="Verify every written file"
            hint="Re-opens each export and proves it parses with the source's duration before reporting success."
          />
          <label className="flex flex-wrap items-center gap-2 mt-2 text-xs text-zinc-300">
            <span className="shrink-0">Parallel workers</span>
            <select
              className="input !py-1 text-xs min-h-8 sm:min-h-0"
              value={f.workers}
              onChange={(e) => set("workers", Number(e.target.value))}
            >
              <option value={0}>Auto (half the cores, max 8)</option>
              {[1, 2, 3, 4, 6, 8, 12, 16].map((n) => (
                <option key={n} value={n}>{n}</option>
              ))}
            </select>
          </label>
          <Opt
            checked={f.prune}
            onChange={(v) => set("prune", v)}
            danger
            label="Sync mode — remove audio the export does not write"
            hint="Deletes audio files under the export folder that this run did not produce. Meant for mirroring a player: leave it off unless you want the destination to match this selection exactly."
          />

          <div className="grid grid-cols-2 sm:grid-cols-[2fr_1fr_auto] gap-2 mt-4">
            <button className="btn-primary text-xs col-span-2 sm:col-span-1 min-h-10 sm:min-h-0" disabled={busy || !paths.length} onClick={run}>
              <HardDriveDownload className="h-3.5 w-3.5" />
              {busy ? "Exporting…" : `Export ${paths.length || ""} track${paths.length === 1 ? "" : "s"}`}
            </button>
            <button className="btn text-xs min-h-8 sm:min-h-0" disabled={busy} onClick={saveDefaults} title="Save these choices as the defaults for the next export">
              <Save className="h-3.5 w-3.5" />
              Save as default
            </button>
            <button className="btn text-xs !px-2 min-h-8 sm:min-h-0" disabled={busy} onClick={resetDefaults} title="Reload the saved defaults">
              <RotateCcw className="h-3.5 w-3.5" />
            </button>
          </div>
          <div className="text-[10px] text-zinc-600 mt-2">
            {f.codec === "copy"
              ? "Copy keeps the original files bit-exact (an embed-cover pass still rewrites tags when art must change)."
              : f.codec === "flac"
                ? "FLAC → FLAC exports are bit-copies; anything else is re-encoded with ffmpeg and fully re-tagged."
                : f.codec === "wav" || f.codec === "aiff"
                  ? `${spec?.label ?? f.codec} carries no tag set this app can write — the export keeps the audio only.`
                  : `Exporting as ${spec?.label ?? f.codec} — files are re-encoded with ffmpeg and fully re-tagged.`}
          </div>
        </div>
      </div>
    </div>
  );
}
