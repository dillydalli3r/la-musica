import { useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CloudDownload, ListFilter, ListMusic, Play, Plus, Upload } from "lucide-react";
import { api } from "../api";
import { toast, useStore } from "../store";
import { EmptyState, PageLoading } from "../components/Badges";
import PageHeader from "../components/PageHeader";
import Modal from "../components/Modal";
import DownloadButton from "../components/DownloadButton";
import { ExportButton, usePlaylistTracks } from "../components/ExportDialog";
import { TrackCover } from "../components/CoverImg";
import FavHeart from "../components/FavHeart";
import { useI18n } from "../lib/i18n";
import { fmtDuration, GRID_SIZE_MIN } from "../lib/fmt";
import type { Playlist, StreamingImportResult } from "../types";

export default function PlaylistsPage() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const { playNow } = useStore();
  const { t } = useI18n();
  const { data: playlists, isLoading } = useQuery({ queryKey: ["playlists"], queryFn: api.playlists });
  const { data: lib } = useQuery({ queryKey: ["library"], queryFn: () => api.library() });
  const gridSize = (localStorage.getItem("mlo.gridSize") as "s" | "m" | "l" | null) ?? "m";
  const [newName, setNewName] = useState("");
  const [importOpen, setImportOpen] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  // path -> tag-derived info (title/artist/album/duration/cover) shared by
  // the cards' duration chips and the expanded viewer
  const trackMeta = useMemo(() => {
    const map = new Map<string, { artist?: string; album?: string; title?: string; coverFile?: string | null; albumCover?: string | null; albumPath?: string; dur?: number; audit?: string | null; pass?: boolean; advisory?: string | null }>();
    for (const a of lib?.artists ?? [])
      for (const al of a.albums)
        for (const t of al.tracks)
          map.set(t.path, {
            artist: al.album_artist || a.name,
            album: al.meta?.ALBUM ?? undefined,
            title: t.tags.TITLE || undefined,
            coverFile: t.cover_file ?? null,
            albumCover: al.cover_file ?? null,
            albumPath: al.path,
            dur: t.tech?.length ?? undefined,
            audit: t.audit,
            pass: t.grade_pass,
            advisory: t.tags.ITUNESADVISORY ?? null,
          });
    return map;
  }, [lib]);

  const refresh = () => qc.invalidateQueries({ queryKey: ["playlists"] });

  // Every playlist's tracks, deduplicated and in playlist order — what this
  // page's Download/Export header actions cover (the page IS the playlists
  // collection, and no card is "selected"). The hook is one query keyed by
  // the id list, so the fetches dedupe and cache.
  const playlistIds = useMemo(() => (playlists ?? []).map((p) => p.id), [playlists]);
  const { data: allPaths = [] } = usePlaylistTracks(playlistIds);
  const allSeconds = useMemo(
    () => allPaths.reduce((s, p) => s + (trackMeta.get(p)?.dur ?? 0), 0),
    [allPaths, trackMeta]
  );

  // One name field, two buttons: a manual playlist is a list you fill by hand,
  // a smart one is a RULE you write — so creating a smart playlist drops the
  // user straight into its rule editor (`?rules=1`) instead of an empty page
  // with nothing to look at. The starter filter is deliberately empty: no rule
  // means "everything", and the first rule the user adds is what makes it
  // theirs.
  const create = useMutation({
    mutationFn: async (kind: "manual" | "smart") => {
      const name = newName.trim();
      if (!name) return undefined;
      const p = await api.createPlaylist(
        name,
        kind,
        kind === "smart" ? { conditions: [], match: "all" } : undefined
      );
      setNewName("");
      return p;
    },
    onSuccess: (p) => {
      refresh();
      if (p?.kind === "smart") navigate(`/playlist/${p.id}?rules=1`);
    },
  });

  const importM3u8 = async (file: File) => {
    try {
      await api.playlistImport(file.name.replace(/\.m3u8?$/i, ""), file);
      refresh();
      toast("Playlist imported");
    } catch (e) {
      toast.error(String(e));
    } finally {
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const queueFor = (paths: string[]) =>
    playNow(
      paths.map((path) => ({
        path,
        file: path.split("/").pop()!,
        albumPath: path.split("/").slice(0, -1).join("/"),
        artist: trackMeta.get(path)?.artist,
        album: trackMeta.get(path)?.album,
        title: trackMeta.get(path)?.title,
        coverFile: trackMeta.get(path)?.coverFile ?? null,
        albumCover: trackMeta.get(path)?.albumCover ?? null,
        advisory: trackMeta.get(path)?.advisory ?? null,
      }))
    );

  if (isLoading) return <PageLoading label="Loading playlists…" />;

  const manual = (playlists ?? []).filter((p) => p.kind === "manual");
  const smart = (playlists ?? []).filter((p) => p.kind === "smart");

  const cardGrid = (list: Playlist[]) => (
    <div
      className="grid gap-x-4 gap-y-5 stagger"
      style={{ gridTemplateColumns: `repeat(auto-fill, minmax(${GRID_SIZE_MIN[gridSize]}px, 1fr))` }}
    >
      {list.map((p) => (
        <PlaylistGridCard
          key={p.id}
          playlist={p}
          trackMeta={trackMeta}
          onPlay={(paths) => paths.length && queueFor(paths.slice(0))}
        />
      ))}
    </div>
  );

  return (
    <div className="p-6 space-y-5 mx-auto max-w-[1600px]">
      <PageHeader
        icon={ListMusic}
        title="Playlists"
        actions={
          <>
            <input
              className="input max-w-xs tap"
              placeholder="New playlist name…"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && create.mutate("manual")}
            />
            <button className="btn-primary tap" onClick={() => create.mutate("manual")} disabled={!newName.trim()}>
              <Plus className="h-4 w-4" /> Create
            </button>
            <button
              className="btn-ghost tap"
              onClick={() => create.mutate("smart")}
              disabled={!newName.trim()}
              title="Create a playlist from rules (genre, year, grade, advisory, …) instead of a hand-picked list"
            >
              <ListFilter className="h-4 w-4" /> Smart
            </button>
            <input
              ref={fileRef}
              type="file"
              accept=".m3u8,.m3u"
              className="hidden"
              onChange={(e) => e.target.files?.[0] && importM3u8(e.target.files[0])}
            />
            <button className="btn-ghost tap" onClick={() => fileRef.current?.click()}>
              <Upload className="h-4 w-4" /> Import .m3u8
            </button>
            <button
              className="btn-ghost tap"
              onClick={() => setImportOpen(true)}
              title="Read a public playlist from Deezer, Spotify, YouTube Music or Apple Music and make it a playlist here"
            >
              <CloudDownload className="h-4 w-4" /> {t("plimport.action")}
            </button>
            {/* bulk actions on every playlist's tracks: nothing here is
                "selected", so the page covers all of them */}
            <DownloadButton
              paths={allPaths}
              size="md"
              label="Download all"
              emptyReason="Nothing to download — the playlists hold no tracks"
            />
            <ExportButton
              paths={allPaths}
              seconds={allSeconds}
              size="md"
              label="Export all"
              emptyReason="Nothing to export — the playlists hold no tracks"
              title="Export every playlist's tracks to a drive"
              dialogSubtitle={`${allPaths.length} track${allPaths.length === 1 ? "" : "s"} across ${(playlists ?? []).length} playlist${(playlists ?? []).length === 1 ? "" : "s"}`}
            />
          </>
        }
      />

      {manual.length === 0 && smart.length === 0 && (
        <EmptyState
          title="No playlists yet"
          hint="Create a manual playlist, create a smart one from rules, or import an .m3u8 file."
        />
      )}

      {/* playlists render exactly like albums in the library grid: same card
          anatomy, same cover sizing, same hover actions */}
      {manual.length > 0 && cardGrid(manual)}

      {smart.length > 0 && (
        <div className="space-y-3">
          <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Smart playlists</h2>
          {cardGrid(smart)}
        </div>
      )}

      {importOpen && (
        <StreamingImportDialog
          onClose={() => setImportOpen(false)}
          onImported={() => refresh()}
        />
      )}
    </div>
  );
}

/** Deterministic per-playlist cover gradient (golden-angle hue spread). */
function coverGradient(p: Playlist): string {
  const hue = Math.round((p.id * 137.5) % 360);
  return `linear-gradient(135deg, hsl(${hue} 42% 32%), hsl(${(hue + 40) % 360} 48% 15%))`;
}

/** Album-card-style grid card for a playlist: 2x2 cover mosaic, completion
 * chip (its "grade"), track/duration chips, hover play + heart. Clicking
 * opens the dedicated playlist viewer, exactly like album cards do. */
function PlaylistGridCard({ playlist, trackMeta, onPlay }: {
  playlist: Playlist;
  trackMeta: Map<string, { artist?: string; album?: string; title?: string; coverFile?: string | null; albumCover?: string | null; albumPath?: string; dur?: number; audit?: string | null; pass?: boolean; advisory?: string | null }>;
  onPlay: (paths: string[]) => void;
}) {
  const { data: detail } = useQuery({ queryKey: ["playlist", playlist.id], queryFn: () => api.playlist(playlist.id) });
  const tracks = useMemo(() => detail?.tracks ?? [], [detail]);
  const totalDur = useMemo(() => tracks.reduce((s, t) => s + (trackMeta.get(t)?.dur ?? 0), 0), [tracks, trackMeta]);

  return (
    <div
      className="group relative rounded-xl p-2 transition-colors hover:bg-panel/70"
    >
      <div className="relative">
        <Link to={`/playlist/${playlist.id}`} title="Open the playlist page" className="block">
        <div
          className="aspect-square w-full rounded-xl shadow-lg overflow-hidden relative"
          style={{ background: coverGradient(playlist) }}
        >
          {/* cover mosaic: the first four tracks' artwork in a 2x2 grid —
              empty slots fall back to the playlist gradient */}
          <div className="absolute inset-0 grid grid-cols-2 grid-rows-2">
            {tracks.slice(0, 4).map((t, i) => (
              <div key={i} className="overflow-hidden">
                <TrackCover
                  albumPath={trackMeta.get(t)?.albumPath ?? t.split("/").slice(0, -1).join("/")}
                  trackCover={trackMeta.get(t)?.coverFile}
                  albumCover={trackMeta.get(t)?.albumCover}
                  wrapperClass="w-full h-full"
                />
              </div>
            ))}
            {Array.from({ length: Math.max(0, 4 - Math.min(4, tracks.length)) }).map((_, i) => (
              <div key={`empty-${i}`} className="flex items-center justify-center opacity-25">
                <ListMusic className="h-5 w-5 text-white/60" />
              </div>
            ))}
          </div>
        </div>
        </Link>
        <div className="absolute top-1.5 right-1.5">
          <FavHeart kind="playlist" id={String(playlist.id)} className="!p-1.5 bg-black/60 tap-hit" iconClass="h-4 w-4" revealOnHover />
        </div>
        {/* ONE flow column, the shape the library's album card uses: the play
            button owns the top band, the chips the bottom edge. The button was
            `absolute left-2 top-9` beside independently anchored corner chips —
            and `.tap-hit { position: relative }` (the phone/coarse-pointer rule
            that grows a 44px hit area) OVERRODE that `absolute`, so on a phone
            the button fell out of the overlay and into the flow below the
            cover, on top of the caption. In flow it needs no position of its
            own, and no chip can be drawn over it. The `h-5` spacer stands in
            for the album card's ADR chip row, so the two grids' play buttons
            sit on the same line. */}
        <div className="pointer-events-none absolute inset-x-1.5 top-1.5 bottom-1.5 flex flex-col items-start min-h-0">
          <div className="h-5 shrink-0" />
          <div className="ml-0.5 mt-2.5 shrink-0 pointer-events-auto">
            <button
              className="btn-primary tap-hit !rounded-lg !p-3 row-hover transition-opacity shadow-2xl"
              title="Play playlist"
              onClick={(e) => {
                e.stopPropagation();
                onPlay(tracks);
              }}
            >
              <Play className="h-4 w-4 fill-current" />
            </button>
          </div>
          {/* The chips stay over the cover's own link: the column lets clicks
              through, so tapping "12 tracks" still opens the playlist. */}
          <div className="mt-auto w-full min-h-0 overflow-hidden flex items-center justify-between gap-2">
            <span className="min-w-0 truncate bg-black/65 text-zinc-300 text-[9px] font-mono tracking-wide rounded px-1 py-0.5 border border-white/10">
              {playlist.track_count} track{playlist.track_count === 1 ? "" : "s"}
            </span>
            {totalDur > 0 && (
              <span className="shrink-0 whitespace-nowrap bg-black/65 text-zinc-200 text-[9px] font-mono tracking-wide rounded px-1 py-0.5 border border-white/10">
                {fmtDuration(totalDur)}
              </span>
            )}
          </div>
        </div>
      </div>
      <div className="mt-2 px-0.5">
        <div className="text-sm font-medium truncate block" title={playlist.name}>
          {playlist.name}
        </div>
        <div className="text-[11px] text-zinc-500 truncate flex items-center gap-1.5 mt-0.5">
          <span className="truncate">{playlist.kind === "smart" ? "Smart playlist" : "Manual playlist"}</span>
          {playlist.kind === "smart" && (
            <span className="chip bg-accent/10 text-accent-soft border border-accent/25 text-[10px] shrink-0">SMART</span>
          )}
        </div>
      </div>
    </div>
  );
}

/** Import a playlist from a streaming service — the form, then the report.
 *
 * One call, two actions: **Check** is a dry run (`dry_run: true`: the same
 * read and the same match, nothing created and nothing queued) and **Import**
 * makes the playlist. The report is the point of both — how many rows the
 * library has, and every row it does not with the reason it does not — and it
 * stays on screen with a link to the created playlist.
 *
 * The parent-albums checkbox starts at the configured default
 * (`playlist_import_parent_albums`, Settings → Streaming playlist import) and
 * overrides it for this one import only. The other two settings that decide
 * what an import does with a track the library lacks are shown as the notes
 * they are, so what is about to happen is read here rather than guessed. */
function StreamingImportDialog({ onClose, onImported }: {
  onClose: () => void;
  onImported: () => void;
}) {
  const { t } = useI18n();
  const { data: cfg } = useQuery({ queryKey: ["config"], queryFn: api.config });
  const [url, setUrl] = useState("");
  const [name, setName] = useState("");
  // null = "the configured default", so a config that arrives after the first
  // render still decides the box — and the user's own click always wins.
  const [parentAlbums, setParentAlbums] = useState<boolean | null>(null);
  const [result, setResult] = useState<StreamingImportResult | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<"" | "check" | "import">("");
  const effectiveParentAlbums = parentAlbums ?? !!cfg?.playlist_import_parent_albums;
  const queueTrackWishes = cfg?.playlist_import_unmatched === "wish";
  const createEmpty = cfg?.playlist_import_create_empty !== false;

  const run = async (dryRun: boolean) => {
    if (!url.trim() || busy) return;
    setBusy(dryRun ? "check" : "import");
    setError("");
    try {
      const res = await api.playlistImportStreaming({
        url: url.trim(),
        name: name.trim(),
        parentAlbums: effectiveParentAlbums,
        dryRun,
      });
      setResult(res);
      if (!dryRun) onImported();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy("");
    }
  };

  const report = result?.report;
  const unmatched = (report?.tracks ?? []).filter((r) => !r.matched);
  const queuedAlbums = report?.parent_albums.queued ?? [];
  const queuedTracks = report?.unmatched_tracks.queued ?? [];

  return (
    <Modal
      onClose={onClose}
      icon={CloudDownload}
      title={t("plimport.title")}
      subtitle={t("plimport.subtitle")}
      width="max-w-lg"
      footer={
        <div className="flex flex-wrap justify-end gap-2">
          <button className="btn-ghost" onClick={onClose}>
            {t("plimport.close")}
          </button>
          <button
            className="btn-ghost tap"
            onClick={() => run(true)}
            disabled={!url.trim() || !!busy}
            title={t("plimport.check_help")}
          >
            {busy === "check" ? t("plimport.working") : t("plimport.check")}
          </button>
          <button className="btn-primary tap" onClick={() => run(false)} disabled={!url.trim() || !!busy}>
            {busy === "import" ? t("plimport.working") : t("plimport.import")}
          </button>
        </div>
      }
    >
      {/* One column, phone first: the dialog is the mobile path into this
          feature, so nothing here is a wide row that has to scroll. */}
      <div className="space-y-3">
        <div>
          <label className="label">{t("plimport.url")}</label>
          <input
            className="input"
            autoFocus
            inputMode="url"
            autoComplete="off"
            placeholder={t("plimport.url_placeholder")}
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && url.trim() && !busy) run(false);
            }}
          />
        </div>
        <div>
          <label className="label">{t("plimport.name")}</label>
          <input
            className="input"
            placeholder={t("plimport.name_placeholder")}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </div>
        <label className="flex items-start gap-2 cursor-pointer">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={effectiveParentAlbums}
            onChange={(e) => setParentAlbums(e.target.checked)}
          />
          <span className="text-xs text-zinc-300">
            {t("plimport.parent_albums")}
            <span className="block text-[11px] text-zinc-500 mt-0.5">{t("plimport.parent_albums_help")}</span>
          </span>
        </label>
        {queueTrackWishes && (
          <div className="text-[11px] text-zinc-500">{t("plimport.hint.wish")}</div>
        )}
        {!createEmpty && (
          <div className="text-[11px] text-zinc-500">{t("plimport.hint.create_empty")}</div>
        )}

        {error && (
          <div className="rounded-lg border border-red-500/40 bg-red-500/10 p-3 text-xs text-red-300 break-words">
            <div className="font-medium">{t("plimport.failed")}</div>
            <div className="mt-1">{error}</div>
          </div>
        )}

        {report && (
          <div className="space-y-3 border-t border-border pt-3">
            <div className="rounded-lg border border-border bg-raise/40 p-3 text-xs space-y-1">
              <div className="font-medium text-zinc-200">
                {t("plimport.report_counts", { matched: report.matched, total: report.total })}
              </div>
              <div className="text-zinc-400">
                {t("plimport.report_unmatched", { n: report.unmatched })}
                {report.duplicates > 0 && ` · ${t("plimport.report_duplicates", { n: report.duplicates })}`}
              </div>
              <div className="text-zinc-400 break-words">{report.note}</div>
            </div>

            {unmatched.length > 0 && (
              <div>
                <div className="text-[10px] uppercase tracking-widest text-zinc-500">
                  {t("plimport.report_unmatched_title")}
                </div>
                <ul className="mt-1 divide-y divide-border/60 rounded-lg border border-border overflow-hidden">
                  {unmatched.map((r) => (
                    <li key={r.index} className="px-3 py-2 text-xs">
                      <div className="truncate text-zinc-200">
                        {r.title || "—"}
                        {r.artist && <span className="text-zinc-500"> · {r.artist}</span>}
                      </div>
                      <div className="truncate text-[11px] text-zinc-500" title={r.reason}>
                        {r.album ? `${r.album} — ` : ""}
                        {r.reason}
                      </div>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {queuedAlbums.length > 0 && (
              <div>
                <div className="text-[10px] uppercase tracking-widest text-zinc-500">
                  {t("plimport.report_queued_albums", { n: queuedAlbums.filter((q) => q.queued).length })}
                </div>
                <ul className="mt-1 divide-y divide-border/60 rounded-lg border border-border overflow-hidden">
                  {queuedAlbums.map((q, i) => (
                    <li key={i} className="px-3 py-2 text-xs">
                      <div className="truncate text-zinc-200">
                        {q.title}
                        {q.artist && <span className="text-zinc-500"> · {q.artist}</span>}
                      </div>
                      <div className="truncate text-[11px] text-zinc-500" title={q.reason || q.note || q.error}>
                        {q.error || q.reason || q.note || (q.queued ? t("plimport.queued") : "")}
                      </div>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {queuedTracks.length > 0 && (
              <div>
                <div className="text-[10px] uppercase tracking-widest text-zinc-500">
                  {t("plimport.report_queued_tracks", { n: queuedTracks.filter((q) => q.queued).length })}
                </div>
                <ul className="mt-1 divide-y divide-border/60 rounded-lg border border-border overflow-hidden">
                  {queuedTracks.map((q, i) => (
                    <li key={i} className="px-3 py-2 text-xs">
                      <div className="truncate text-zinc-200">
                        {q.title}
                        {q.artist && <span className="text-zinc-500"> · {q.artist}</span>}
                      </div>
                      <div className="truncate text-[11px] text-zinc-500" title={q.note || q.error}>
                        {q.error || q.note || (q.queued ? t("plimport.queued") : "")}
                      </div>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {result?.playlist ? (
              <Link
                to={`/playlist/${result.playlist.id}`}
                className="btn-primary tap inline-flex"
                onClick={onClose}
              >
                {t("plimport.open_playlist")} · {result.playlist.name}
              </Link>
            ) : null}
          </div>
        )}
      </div>
    </Modal>
  );
}
