import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams, useSearchParams, Link } from "react-router-dom";
import {
  ChevronDown, ChevronUp, Download, ListMusic, ListPlus, ListStart, Pencil, Play, Plus, Trash2,
} from "lucide-react";
import { api } from "../api";
import { toast, useStore } from "../store";
import { EmptyState, PageLoading } from "../components/Badges";
import PageHeader from "../components/PageHeader";
import Modal from "../components/Modal";
import MoreLikeThis from "../components/MoreLikeThis";
import { TrackCover } from "../components/CoverImg";
import DownloadButton from "../components/DownloadButton";
import { ExportButton } from "../components/ExportDialog";
import { TrackActionsMenu } from "../components/TagActionsMenu";
import FavHeart from "../components/FavHeart";
import OverflowMenu from "../components/OverflowMenu";
import { trackRef, entityLinkClick } from "../lib/refs";
import { useI18n } from "../lib/i18n";
import { fmtDuration } from "../lib/fmt";
import {
  fieldIndex, newCondition, opsFor, useLibraryFields, ValueControl,
} from "../components/QueryBuilder";
import type { LibraryCondition } from "../api";
import type { Playlist } from "../types";

/** The rule editor renders the app's ONE field catalogue
 *  (`GET /api/library/fields`) — the same list the Browse page's builder and
 *  facet rail render — so a field or operator added on the server shows up
 *  here too instead of drifting behind a second hardcoded list.
 *
 *  A spec written before that catalogue existed may name a field it no longer
 *  lists (`grade_pass`, `tech.length`): the row keeps the raw key as a
 *  selectable option and the engine still resolves it, so opening an older
 *  smart playlist can never silently rewrite or drop its rules. */

/** The streaming services an imported playlist can name. The ids and labels
 *  are the server's own (`server/streaming_playlists.py` SERVICE_LABELS); an id
 *  no map here lists is shown as it is rather than hidden. */
const SERVICE_LABELS: Record<string, string> = {
  deezer: "Deezer",
  spotify: "Spotify",
  youtube: "YouTube Music",
  apple: "Apple Music",
};

/** Deterministic per-playlist cover gradient (same recipe as the cards). */
function coverGradient(p: Playlist): string {
  const hue = Math.round((p.id * 137.5) % 360);
  return `linear-gradient(135deg, hsl(${hue} 42% 32%), hsl(${(hue + 40) % 360} 48% 15%))`;
}

/** The playlist detail viewer — laid out exactly like the album page:
 * ambient tinted header (mosaic cover, big title, meta chips, action row),
 * then the tracklist table. Position numbers are the track's order in the
 * playlist. */
export default function PlaylistDetailPage() {
  const { id } = useParams();
  const pid = Number(id);
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { playNow, queue, queueAdd } = useStore();
  const { t } = useI18n();

  const { data: playlist, isLoading, error } = useQuery({
    queryKey: ["playlists"],
    queryFn: api.playlists,
    select: (rows: Playlist[]) => rows.find((p) => p.id === pid),
  });
  const { data: detail, error: detailError } = useQuery({ queryKey: ["playlist", pid], queryFn: () => api.playlist(pid) });
  const { data: lib } = useQuery({ queryKey: ["library"], queryFn: () => api.library() });
  const { data: catalogue } = useLibraryFields();
  const fields = useMemo(() => fieldIndex(catalogue), [catalogue]);

  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(playlist?.name ?? "");
  const [filterOpen, setFilterOpen] = useState(false);
  // `?rules=1` is how a just-created smart playlist lands the user in the rule
  // editor instead of an empty page: the create button in the playlists list
  // sends it, and the param is stripped once the editor is open so a refresh
  // or a back-navigation does not reopen a modal the user closed.
  const [searchParams, setSearchParams] = useSearchParams();
  const [conditions, setConditions] = useState<LibraryCondition[]>([]);
  const [matchAll, setMatchAll] = useState(true);
  const [dragIdx, setDragIdx] = useState<number | null>(null);
  const [overIdx, setOverIdx] = useState<number | null>(null);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["playlists"] });
    qc.invalidateQueries({ queryKey: ["playlist", pid] });
  };

  const tracks = useMemo(() => detail?.tracks ?? [], [detail]);
  const reorderable = playlist?.kind === "manual";

  // shared per-track metadata for the table + duration totals
  const trackMeta = useMemo(() => {
    const map = new Map<string, {
      title: string; artist?: string; album?: string; dur?: number;
      coverFile?: string | null; albumCover?: string | null; albumPath?: string;
      audit?: string | null; pass?: boolean; advisory?: string | null;
      mbid?: string | null;
    }>();
    for (const a of lib?.artists ?? [])
      for (const al of a.albums)
        for (const t of al.tracks)
          map.set(t.path, {
            title: t.tags.TITLE ?? t.file,
            artist: al.album_artist || a.name,
            album: al.meta?.ALBUM ?? undefined,
            dur: t.tech?.length ?? undefined,
            coverFile: t.cover_file ?? null,
            albumCover: al.cover_file ?? null,
            albumPath: al.path,
            audit: t.audit,
            pass: t.grade_pass,
            advisory: t.tags.ITUNESADVISORY ?? null,
            mbid: t.tags.MUSICBRAINZ_TRACKID ?? null,
          });
    return map;
  }, [lib]);

  const totalDur = useMemo(() => tracks.reduce((s, t) => s + (trackMeta.get(t)?.dur ?? 0), 0), [tracks, trackMeta]);

  const queueTracks = useMemo(
    () =>
      tracks.map((path) => ({
        path,
        file: path.split("/").pop()!,
        albumPath: trackMeta.get(path)?.albumPath ?? path.split("/").slice(0, -1).join("/"),
        artist: trackMeta.get(path)?.artist,
        album: trackMeta.get(path)?.album,
        title: trackMeta.get(path)?.title,
        coverFile: trackMeta.get(path)?.coverFile ?? null,
        albumCover: trackMeta.get(path)?.albumCover ?? null,
        advisory: trackMeta.get(path)?.advisory ?? null,
      })),
    [tracks, trackMeta]
  );

  // A just-created smart playlist links here with `?rules=1`: the editor opens
  // on arrival and the param is dropped straight away, so a refresh or a back
  // navigation does not reopen a modal the user has since closed.
  //
  // This effect MUST stay above the loading/not-found returns below: a hook
  // that only runs on the renders that got past them changes the hook order
  // between one render and the next, which is a crash — and it does happen on
  // a real path, because a deep link or a reload renders once with no playlist
  // yet. So it opens the editor from the playlist itself rather than through
  // `openFilterEditor`, which is defined below the returns.
  useEffect(() => {
    if (searchParams.get("rules") !== "1" || !playlist) return;
    setConditions(playlist.filter?.conditions ?? []);
    setMatchAll(playlist.filter?.match !== "any");
    setFilterOpen(true);
    setSearchParams({}, { replace: true });
  }, [playlist, searchParams, setSearchParams]);

  const enqueue = (position: "next" | "end") => {
    if (!queueTracks.length) return;
    if (!queue.length) {
      playNow(queueTracks);
      return;
    }
    queueAdd(queueTracks, position);
    toast(position === "next" ? `Playing ${queueTracks.length} track(s) next` : `Added ${queueTracks.length} track(s) to the queue`);
  };

  const update = useMutation({
    mutationFn: (patch: { name?: string }) => api.playlistUpdate(pid, patch),
    onSuccess: () => {
      invalidate();
      qc.invalidateQueries({ queryKey: ["library"] });
    },
    onError: (e) => toast.error(String(e)),
  });
  const del = useMutation({
    mutationFn: () => api.deletePlaylist(pid),
    onSuccess: () => {
      invalidate();
      navigate("/playlists");
    },
    onError: (e) => toast.error(String(e)),
  });

  const move = async (i: number, dir: -1 | 1) => {
    const j = i + dir;
    if (j < 0 || j >= tracks.length) return;
    const next = [...tracks];
    [next[i], next[j]] = [next[j], next[i]];
    try {
      await api.playlistOrder(pid, next);
      invalidate();
    } catch (e) {
      toast.error(String(e));
    }
  };

  const moveTo = async (from: number, to: number) => {
    if (from === to || from < 0 || to < 0 || from >= tracks.length || to >= tracks.length) return;
    const next = [...tracks];
    const [moved] = next.splice(from, 1);
    next.splice(to, 0, moved);
    try {
      await api.playlistOrder(pid, next);
      invalidate();
    } catch (e) {
      toast.error(String(e));
    }
  };

  const removeTrack = async (path: string) => {
    try {
      await api.playlistRemove(pid, [path]);
      invalidate();
    } catch (e) {
      toast.error(String(e));
    }
  };

  if (error || detailError || (!isLoading && !playlist))
    return (
      <EmptyState
        title="Playlist not found"
        hint="It may have been deleted."
        action={{ label: "Back to playlists", to: "/playlists" }}
      />
    );
  if (isLoading || !playlist) return <PageLoading label="Loading playlist…" />;

  // first four covers for the mosaic header art
  const mosaic = tracks.slice(0, 4).map((t) => ({
    albumPath: trackMeta.get(t)?.albumPath ?? t.split("/").slice(0, -1).join("/"),
    trackCover: trackMeta.get(t)?.coverFile ?? null,
    albumCover: trackMeta.get(t)?.albumCover ?? null,
  }));

  // The action row is one size: `.btn-icon` in index.css is the same 36px
  // square for play, download, like, filter and the overflow menu.
  const openFilterEditor = () => {
    setConditions(playlist?.filter?.conditions ?? []);
    setMatchAll(playlist?.filter?.match !== "any");
    setFilterOpen(true);
  };

  const saveSmart = async () => {
    try {
      await api.playlistFilter(pid, { conditions, match: matchAll ? "all" : "any" });
      const ev = await api.playlistEvaluate(pid);
      await api.playlistOrder(pid, ev.paths);
      setFilterOpen(false);
      invalidate();
      toast("Smart playlist updated");
    } catch (e) {
      toast.error(String(e));
    }
  };

  const rename = async () => {
    if (!name.trim() || name.trim() === playlist.name) return setRenaming(false);
    await update.mutateAsync({ name: name.trim() });
    setRenaming(false);
    toast("Playlist renamed");
  };

  return (
    <>
      {/* ambient tint behind the whole page, from the playlist's hue */}
      <div className="fixed inset-0 z-0 pointer-events-none overflow-hidden" aria-hidden>
        <div className="w-full h-full blur-[90px] opacity-25 scale-125" style={{ background: coverGradient(playlist) }} />
        <div className="absolute inset-0 bg-bg/50" />
      </div>
      <div className="relative z-10 p-6 space-y-5 mx-auto max-w-6xl">
        <div
          className="hero-flat relative"
          style={{ background: `linear-gradient(135deg, hsl(${Math.round((pid * 137.5) % 360)} 42% 32% / 0.15) 0%, transparent 60%)` }}
        >
          {/* The album and artist heroes' own fold, reused verbatim: a column
              on a phone (the 224 px mosaic used to squeeze the title block to
              nothing beside it) and the cover-beside-identity row from `sm`
              up. */}
          <div className="flex flex-col sm:flex-row items-start gap-5">
            {/* mosaic cover — the first four tracks' artwork in a 2x2 grid */}
            <div className="shrink-0 mx-auto sm:mx-0">
              <div
                className="h-40 w-40 sm:h-56 sm:w-56 rounded-md bg-raise overflow-hidden shadow-2xl relative grid grid-cols-2 grid-rows-2"
                style={{ background: coverGradient(playlist) }}
              >
                {mosaic.map((m, i) => (
                  <div key={i} className="overflow-hidden">
                    <TrackCover albumPath={m.albumPath} trackCover={m.trackCover} albumCover={m.albumCover} wrapperClass="w-full h-full" />
                  </div>
                ))}
                {Array.from({ length: Math.max(0, 4 - Math.min(4, tracks.length)) }).map((_, i) => (
                  <div key={`empty-${i}`} className="flex items-center justify-center opacity-25">
                    <ListMusic className="h-6 w-6 text-white/60" />
                  </div>
                ))}
              </div>
            </div>
            {/* `w-full` is the fold's other half, exactly as on the album and
                artist heroes: a flex child in a column is only as wide as its
                content, so the title block would otherwise sit narrow. */}
            <div className="flex-1 min-w-0 w-full">
              <PageHeader
                icon={ListMusic}
                overline={playlist.kind === "smart" ? "Smart playlist" : "Manual playlist"}
                title={
                  renaming ? (
                    <input
                      className="input max-w-md"
                      value={name}
                      autoFocus
                      onChange={(e) => setName(e.target.value)}
                      onKeyDown={(e) => e.key === "Enter" && rename()}
                    />
                  ) : (
                    playlist.name
                  )
                }
                chips={playlist.kind === "smart" ? ["SMART"] : undefined}
                subtitle={
                  <>
                    {tracks.length} track{tracks.length === 1 ? "" : "s"} ·{" "}
                    {totalDur > 0 ? fmtDuration(totalDur) : "—"}
                    {/* Where an imported playlist came from: the service, and a
                        link to that service's own playlist page. */}
                    {playlist.origin && (
                      <>
                        {" · "}
                        {t("plimport.origin", {
                          service: SERVICE_LABELS[playlist.origin] ?? playlist.origin,
                        })}
                        {playlist.origin_url && (
                          <>
                            {" "}
                            <a
                              href={playlist.origin_url}
                              target="_blank"
                              rel="noreferrer"
                              className="underline hover:text-zinc-300"
                            >
                              {t("plimport.origin_link")}
                            </a>
                          </>
                        )}
                      </>
                    )}
                  </>
                }
                actions={
                  <>
                    {renaming && (
                      <button className="btn-primary text-xs" onClick={rename}>Save</button>
                    )}
                    <button
                      className="btn-icon-primary"
                      onClick={() => queueTracks.length && playNow(queueTracks)}
                      title="Play the playlist from the top"
                      aria-label="Play playlist"
                    >
                      <Play className="h-4 w-4 fill-current" />
                    </button>
                    <FavHeart
                      kind="playlist"
                      id={String(pid)}
                      className="btn-icon"
                      iconClass="h-4 w-4"
                    />
                    <DownloadButton
                      paths={tracks}
                      size="md"
                      emptyReason="Nothing to download — this playlist is empty"
                    />
                    <ExportButton
                      paths={tracks}
                      seconds={totalDur}
                      iconOnly
                      title="Export this playlist to a drive"
                      emptyReason="Nothing to export — this playlist is empty"
                      dialogSubtitle={`${playlist.name} · ${tracks.length} track${tracks.length === 1 ? "" : "s"}`}
                    />
                    {playlist.kind === "smart" && (
                      <button className="btn-icon" onClick={openFilterEditor} title="Edit smart filter">
                        <Pencil className="h-4 w-4" />
                      </button>
                    )}
                    <OverflowMenu
                      buttonClass="btn-icon"
                      buttonTitle="All playlist actions"
                      sections={[
                        {
                          items: [
                            { label: "Play next", icon: ListStart, onClick: () => enqueue("next") },
                            { label: "Add to queue", icon: ListPlus, onClick: () => enqueue("end") },
                          ],
                        },
                        {
                          title: "Playlist",
                          items: [
                            { label: "Rename", icon: Pencil, onClick: () => { setName(playlist.name); setRenaming(true); } },
                            { label: "Download .m3u8", icon: Download, onClick: () => { window.location.href = api.playlistExportUrl(pid); } },
                          ],
                        },
                        {
                          items: [
                            { label: "Delete playlist", icon: Trash2, danger: true, onClick: () => { if (window.confirm(`Delete "${playlist.name}"?`)) del.mutate(); } },
                          ],
                        },
                      ]}
                    />
                  </>
                }
              />
            </div>
          </div>
        </div>

        {/* ---------------- tracklist table (album-page style) ------------- */}
        {tracks.length > 0 ? (
          <div className="overflow-x-auto">
            {/* Its floor, from its own columns: the fixed # and cover take 96 px
                and the reorder handle 96 more, artist + album + duration take
                40% of what is left, and Duration (8%) has to hold the 56 px a
                "3:45" needs — 56 / 0.08 = 700. There the Title still has
                0.6 × 700 − 192 = 228 px. The table has no phone fold, so unlike
                the library's tables this floor applies at every width. */}
            <table className="w-full text-sm min-w-[700px]">
              <thead className="border-b border-border">
                <tr>
                  <th className="th w-12">#</th>
                  <th className="th w-12" title="Cover art"><span className="sr-only">Cover</span></th>
                  <th className="th">Title</th>
                  <th className="th w-[16%]">Artist</th>
                  <th className="th w-[16%]">Album</th>
                  <th className="th w-[8%]">Duration</th>
                  {reorderable && <th className="th w-24"><span className="sr-only">Actions</span></th>}
                </tr>
              </thead>
              <tbody>
                {tracks.map((t, i) => {
                  const m = trackMeta.get(t);
                  const isDragging = dragIdx === i;
                  const isOver = overIdx === i && dragIdx !== null && dragIdx !== i;
                  return (
                    <tr
                      key={t}
                      draggable={reorderable}
                      onDragStart={(e) => {
                        if (!reorderable) return;
                        setDragIdx(i);
                        e.dataTransfer.effectAllowed = "move";
                        e.dataTransfer.setData("text/plain", String(i));
                      }}
                      onDragOver={(e) => {
                        if (!reorderable) return;
                        e.preventDefault();
                        e.dataTransfer.dropEffect = "move";
                        if (overIdx !== i) setOverIdx(i);
                      }}
                      onDrop={(e) => {
                        if (!reorderable) return;
                        e.preventDefault();
                        const from = dragIdx ?? Number(e.dataTransfer.getData("text/plain"));
                        if (Number.isFinite(from)) moveTo(from, i);
                        setDragIdx(null);
                        setOverIdx(null);
                      }}
                      onDragEnd={() => {
                        setDragIdx(null);
                        setOverIdx(null);
                      }}
                      className={`table-row group cursor-pointer ${isOver ? "outline outline-1 outline-accent" : ""} ${isDragging ? "opacity-40" : ""}`}
                      title="Click to play · Ctrl-click to open track page"
                      onClick={() => queueTracks.length && playNow(queueTracks, Math.min(i, queueTracks.length - 1))}
                    >
                      {/* position in the PLAYLIST (1, 2, 3…) */}
                      <td className="td cell-nowrap text-zinc-600 tabular-nums">{i + 1}</td>
                      <td className="td cell-cover pr-0">
                        <TrackCover
                          albumPath={m?.albumPath ?? t.split("/").slice(0, -1).join("/")}
                          trackCover={m?.coverFile}
                          albumCover={m?.albumCover}
                          wrapperClass="h-9 w-9 rounded bg-raise overflow-hidden shrink-0"
                        />
                      </td>
                      <td className="td">
                        <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 min-w-0">
                          <Link
                            to={trackRef({ path: t, tags: { MUSICBRAINZ_TRACKID: m?.mbid ?? undefined } })}
                            className="break-words flex-1 min-w-[8rem] hover:text-accent-soft"
                            title="Click to play · Ctrl-click to open track page"
                            onClick={(e) => entityLinkClick(e, () => navigate(trackRef({ path: t, tags: { MUSICBRAINZ_TRACKID: m?.mbid ?? undefined } })))}
                          >
                            {m?.title ?? t.split("/").pop()}
                          </Link>
                          <span className="shrink-0" onClick={(e) => e.stopPropagation()}>
                            <FavHeart kind="track" id={t} iconClass="h-3.5 w-3.5" revealOnHover />
                          </span>
                          <span className="row-hover shrink-0" onClick={(e) => e.stopPropagation()}>
                            <TrackActionsMenu path={t} />
                          </span>
                        </div>
                      </td>
                      <td className="td text-zinc-400 break-words">{m?.artist ?? "—"}</td>
                      <td className="td text-zinc-500 break-words">{m?.album ?? "—"}</td>
                      <td className="td text-zinc-500">{m?.dur ? fmtDuration(m.dur) : "—"}</td>
                      {reorderable && (
                        <td className="td pr-0" onClick={(e) => e.stopPropagation()}>
                          <div className="flex gap-0.5 items-center">
                            <button className="text-zinc-600 hover:text-zinc-300 disabled:opacity-30" title="Move up" onClick={() => move(i, -1)} disabled={i === 0}><ChevronUp className="h-4 w-4" /></button>
                            <button className="text-zinc-600 hover:text-zinc-300 disabled:opacity-30" title="Move down" onClick={() => move(i, 1)} disabled={i === tracks.length - 1}><ChevronDown className="h-4 w-4" /></button>
                            <button className="text-zinc-600 hover:text-red-400 ml-1" title="Remove from playlist" onClick={() => removeTrack(t)}>
                              <Trash2 className="h-3.5 w-3.5" />
                            </button>
                          </div>
                        </td>
                      )}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState title="Empty playlist" hint="Add tracks from any album or track page." />
        )}

        {/* Library tracks the local scorer ranks closest to what this playlist
            already holds — the playlist's own tracks are excluded. */}
        <MoreLikeThis kind="playlist" id={String(pid)} />
      </div>

      {filterOpen && (
        <Modal
          onClose={() => setFilterOpen(false)}
          icon={Pencil}
          title={`Smart playlist: ${playlist.name}`}
          width="max-w-[560px]"
          bodyClass="px-5 py-4 space-y-2"
          footer={
            <div className="flex justify-end gap-2">
              <button className="btn-ghost" onClick={() => setFilterOpen(false)}>Cancel</button>
              <button className="btn-primary" onClick={saveSmart}>Save &amp; evaluate</button>
            </div>
          }
        >
          <label className="flex items-center gap-2 text-sm text-zinc-400">
            <input type="checkbox" checked={matchAll} onChange={(e) => setMatchAll(e.target.checked)} />
            Match all conditions (AND)
          </label>
          {conditions.map((c, i) => {
            const def = fields.get(c.field);
            const ops = opsFor(def);
            return (
              <div key={i} className="flex flex-wrap items-center gap-2">
                <select
                  className="input !py-1 text-xs flex-1 min-w-[150px]"
                  aria-label="Field"
                  value={c.field}
                  onChange={(e) => {
                    // A different field means a different operator set and a
                    // different kind of value: start that row over from the
                    // new field's own defaults rather than carrying an op the
                    // new field does not offer.
                    const next = fields.get(e.target.value);
                    setConditions((cs) =>
                      cs.map((x, j) => (j === i ? (next ? newCondition(next) : { ...x, field: e.target.value }) : x))
                    );
                  }}
                >
                  {/* A field the catalogue no longer lists (an older saved
                      spec) stays selectable instead of being rewritten. */}
                  {!def && <option value={c.field}>{c.field} (older field)</option>}
                  {(catalogue?.groups ?? []).map((g) => (
                    <optgroup key={g.id} label={g.label}>
                      {g.fields.map((f) => (
                        <option key={f.field} value={f.field}>
                          {f.label}
                        </option>
                      ))}
                    </optgroup>
                  ))}
                </select>
                <select
                  className="input !py-1 text-xs w-32"
                  aria-label="Operator"
                  value={c.op}
                  onChange={(e) => setConditions((cs) => cs.map((x, j) => (j === i ? { ...x, op: e.target.value } : x)))}
                >
                  {ops.some((o) => o.op === c.op) ? null : <option value={c.op}>{c.op}</option>}
                  {ops.map((o) => (
                    <option key={o.op} value={o.op}>
                      {o.label}
                    </option>
                  ))}
                </select>
                <ValueControl
                  def={def}
                  op={c.op}
                  value={c.value ?? null}
                  onChange={(v) => setConditions((cs) => cs.map((x, j) => (j === i ? { ...x, value: v } : x)))}
                  className="w-36"
                />
                <button className="btn-danger !px-2 ml-auto" onClick={() => setConditions((cs) => cs.filter((_, j) => j !== i))} title="Remove condition" aria-label="Remove condition">
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
            );
          })}
          <div>
            <button
              className="btn-ghost text-xs"
              disabled={!catalogue}
              onClick={() => {
                const first = catalogue?.groups.flatMap((g) => g.fields)[0];
                if (first) setConditions((cs) => [...cs, newCondition(first)]);
              }}
            >
              <Plus className="h-3.5 w-3.5" /> Add condition
            </button>
          </div>
        </Modal>
      )}
    </>
  );
}
