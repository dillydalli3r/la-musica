import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { useState } from "react";
import {
  BarChart3, ChevronDown, ChevronRight, Disc3, FileDown,
  ListChecks, Music2, Square, SquareCheck,
} from "lucide-react";
import { api } from "../api";
import { LinkChips, LinkEditorButton } from "../components/Links";
import { EmptyState, PageLoading } from "../components/Badges";
import AlbumCard from "../components/AlbumCard";
import { ExportButton } from "../components/ExportDialog";
import ArtistName from "../components/ArtistName";
import PageHeader from "../components/PageHeader";
import SelectAllButton from "../components/SelectAllButton";
import TagActionsMenu from "../components/TagActionsMenu";
import type { Album } from "../types";
import { artistMbid } from "../lib/refs";
import { GRID_SIZE_MIN, releaseCount } from "../lib/fmt";
import { invalidateLibrary } from "../lib/invalidate";
import StatsPanel from "../components/StatsPanel";

/** The album sections the list groups by, in display order. */
const TYPE_ORDER = ["Album", "EP", "Single", "Live", "Compilation", "Other"] as const;
type ReleaseType = (typeof TYPE_ORDER)[number];

/** AlbumMeta carries no RELEASETYPE, so the type comes from the first track
 *  that tags one (the importer stamps the same value on every track). A
 *  combined type ("Album + Compilation") buckets by the first match in
 *  releaseType's precedence. */
function releaseType(al: Album): ReleaseType {
  const tags = al.tracks.find((t) => t.tags?.RELEASETYPE)?.tags;
  const raw = (tags?.RELEASETYPE ?? "").toLowerCase();
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
  const [statsOpen, setStatsOpen] = useState(false);
  // Album-list UI: collapsed RELEASETYPE sections, and the select-mode set of
  // album paths the batch bar acts on.
  const [collapsed, setCollapsed] = useState<Set<ReleaseType>>(new Set());
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
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
  // The releases grid follows the library's own cover-size setting, so the
  // two grids read as one viewer.
  const gridSize = (localStorage.getItem("mlo.gridSize") as "s" | "m" | "l" | null) ?? "m";
  const allTracks = data.albums.flatMap((a) =>
    a.tracks.map((t) => ({
      path: t.path, file: t.file, albumPath: a.path,
      artist: a.album_artist || data.display_name || data.name,
      album: a.meta?.ALBUM ?? undefined, title: t.tags.TITLE || undefined,
      coverFile: t.cover_file ?? null, albumCover: a.cover_file ?? null,
      advisory: t.tags.ITUNESADVISORY ?? null,
    }))
  );
  // The catalogue's total runtime — the export dialog's drive-fit estimate.
  const allSeconds = data.albums.reduce(
    (s, a) => s + a.tracks.reduce((n, t) => n + (t.tech?.length ?? 0), 0),
    0
  );
  // Identity links for this artist: the MusicBrainz artist id, from any
  // album's album-artist tag.
  const artistTags = {
    MUSICBRAINZ_ARTISTID: artistMbid(data) ?? "",
  };

  // The artist-level grade, watched by the identity dot and the chips below.
  const grade = data.grade;
  const gradeIssues = grade?.issues ?? [];
  // Notes inform without failing: they are shown muted, next to the chips the
  // failures use.
  const gradeNotes = grade?.notes ?? [];
  const monogram = name.trim().split(/\s+/).map((w) => w[0] ?? "").join("").slice(0, 2).toUpperCase();

  /** Every library write invalidates the SAME queries the rest of the app
   *  refreshes after one: the artist payload this page renders, plus
   *  library/home/album/track-tags (see lib/invalidate). */
  const refresh = () => invalidateLibrary(qc);

  /** One section per non-empty RELEASETYPE bucket, in TYPE_ORDER. */
  const sections = TYPE_ORDER.map((type) => ({
    type,
    albums: data.albums.filter((al) => releaseType(al) === type),
  })).filter((s) => s.albums.length > 0);

  const selectedAlbums = data.albums.filter((al) => selected.has(al.path));
  const selectedTrackPaths = selectedAlbums.flatMap((al) => al.tracks.map((t) => t.path));
  /* Every album this page lists — the same set the batch bar above acts on
   * (its buttons re-run on whatever is ticked, and this is the largest tick a
   * reader can ask for here). */
  const allAlbumsSelected = data.albums.length > 0 && data.albums.every((al) => selected.has(al.path));

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

  return (
    <div className="p-6 space-y-5 mx-auto max-w-[1600px]">
      <div className="hero-flat relative overflow-hidden">
        <div className="absolute inset-0 bg-gradient-to-t from-bg via-bg/70 to-bg/30" />
        <div className="relative flex flex-col sm:flex-row items-start gap-5">
          {/* Same cover geometry as the album hero: one square tile, a plain
              generic avatar (a monogram, or a music glyph when the name has
              no usable initials). */}
          <div className="h-40 w-40 shrink-0 mx-auto sm:mx-0 rounded-xl overflow-hidden bg-gradient-to-br from-accent/30 to-accent/5 flex items-center justify-center shadow-2xl">
            {monogram ? (
              <span className="text-4xl font-semibold tracking-tight text-zinc-500 select-none">
                {monogram}
              </span>
            ) : (
              <Music2 className="h-10 w-10 text-zinc-500" />
            )}
          </div>

          <div className="flex-1 min-w-0 w-full">
            <PageHeader
              overline="Artist"
              /* The name carries its OWN verdict: one green dot when the
                 artist folder passes its checks. The chips that spelled those
                 checks out are gone — the same dot sits beside the name in
                 every list an artist is listed in (components/ArtistName),
                 and it reads from the same payload field this page holds. */
              title={<ArtistName name={name} pass={grade?.pass} issues={grade?.issues} disambiguation={data.disambiguation} nameClassName="truncate" />}
              subtitle={
                <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-zinc-400">
                  <span className="whitespace-nowrap">
                    {releaseCount(data.aggregate.album_count)} · {data.aggregate.track_count} track{data.aggregate.track_count === 1 ? "" : "s"}
                  </span>
                  {grade?.error ? (
                    <span className="text-xs text-zinc-600" title={grade.error}>artist grade unavailable</span>
                  ) : grade && (grade.checks ?? 0) === 0 ? (
                    <span className="text-xs text-zinc-600" title="Both artist checks are switched off in Settings → Grading — the dot only says nothing failed">
                      artist checks off
                    </span>
                  ) : null}
                  {/* the failing artist checks, in the app's standard chip —
                      inside the row, so they can never spill out of the hero */}
                  {gradeIssues.map((i) => (
                    <span
                      key={i.code}
                      className="chip bg-red-950/40 text-red-300/90 border border-red-900/50"
                      title={[i.label, i.where, i.reason].filter(Boolean).join(" — ")}
                    >
                      {i.label}
                    </span>
                  ))}
                  {gradeNotes.map((i) => (
                    <span
                      key={i.code}
                      className="chip bg-raise/60 text-zinc-400 border border-border"
                      title={[i.label, i.where, i.reason].filter(Boolean).join(" — ")}
                    >
                      {i.label}
                    </span>
                  ))}
                </span>
              }
            >
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <LinkChips tags={artistTags} />
              </div>
              {/* The actions sit in their OWN wrapping row under the title: a
                  header row of buttons sharing a line with the identity block
                  squeezes the title and the counts into a column at
                  1024-1568px. Out of that row they just wrap.

                  Every button is the same 36px `.btn-icon` square — the set
                  the album page's action row uses — with no visible label:
                  the glyph names the action, and `title`/`aria-label` (kept by
                  each component from `title`/`buttonTitle`) name it on hover
                  and to a screen reader. */}
              <div className="flex items-center gap-1.5 flex-wrap">
                <ExportButton
                  paths={allTracks.map((t) => t.path)}
                  seconds={allSeconds}
                  iconOnly
                  emptyReason="Nothing to export — this artist has no tracks"
                  title="Export this artist's tracks to a drive"
                  dialogSubtitle={`Every track by ${name}`}
                />
                <TagActionsMenu
                  paths={allTracks.map((t) => t.path)}
                  artist={decoded}
                  // The artist's own folder, so the folder-scoped scripts run on
                  // the artist (the layout of its subtrees) instead of being
                  // derived from the tracks one album at a time.
                  artistPath={data.path}
                  onDone={refresh}
                  buttonClass="btn-icon"
                  buttonTitle="Tag actions on every track of this artist"
                />
                <LinkEditorButton
                  mode="artist"
                  paths={allTracks.map((t) => t.path)}
                  current={artistTags}
                  iconOnly
                />
                <button
                  className="btn-icon"
                  onClick={() => setStatsOpen(true)}
                  title="Stats for this artist"
                  aria-label="Stats for this artist"
                >
                  <BarChart3 className="h-4 w-4" />
                </button>
              </div>
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

      <section className="space-y-3">
        <div className="flex items-center gap-2 flex-wrap">
          <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Releases</h2>
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
          {/* Where the reader just pressed Select: every album this page lists,
              in one click, instead of one per card. */}
          {selectMode && (
            <SelectAllButton
              count={data.albums.length}
              noun="albums"
              all={allAlbumsSelected}
              onSelectAll={() => setSelected(new Set(data.albums.map((al) => al.path)))}
              onClear={() => setSelected(new Set<string>())}
            />
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
              <TagActionsMenu
                paths={selectedTrackPaths}
                artist={decoded}
                artistPath={data.path}
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
            hint="Releases show up here once a folder is imported under this artist."
          />
        ) : (
          sections.map(({ type, albums }) => {
            const isCollapsed = collapsed.has(type);
            return (
              <section key={type} className="section space-y-1">
                <button
                  type="button"
                  className="tap flex w-full items-center gap-1.5 pb-2 text-left text-xs font-bold uppercase tracking-wider text-zinc-400 select-none"
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
                  <div
                    className="grid gap-x-4 gap-y-5 stagger"
                    style={{ gridTemplateColumns: `repeat(auto-fill, minmax(${GRID_SIZE_MIN[gridSize] ?? GRID_SIZE_MIN.m}px, 1fr))` }}
                  >
                    {albums.map((al) => (
                      // The library's own album card (cover, title, artist,
                      // year, verdict dot). The per-release grade
                      // verdict is NOT repeated here any more: the card's own
                      // status dot IS that verdict (`statusFor(al.pass,
                      // al.audit_summary)`), so a failing album wore a red dot
                      // and a red cross two lines apart, and a passing one a
                      // green dot and a tick — one fact, twice, under every
                      // cover. The dot's tooltip names it in words, and the
                      // album page and the Library table state the score. The
                      // folder's track count is deliberately not printed
                      // either: noise on a grid of albums, one click away.
                      <AlbumCard
                        key={al.path}
                        al={al}
                        selectable={selectMode}
                        selected={selected.has(al.path)}
                        onSelect={toggleSel}
                      />
                    ))}
                  </div>
                )}
              </section>
            );
          })
        )}
      </section>
    </div>
  );
}
