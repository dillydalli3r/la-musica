import { Link } from "react-router-dom";
import { Play } from "lucide-react";
import { useStore } from "../store";
import { statusFor } from "../lib/status";
import { mediaShort } from "./Badges";
import { albumTech } from "../lib/fmt";
import CoverImg from "./CoverImg";
import FavHeart from "./FavHeart";
import { albumRef } from "../lib/refs";
import { originalYear } from "../lib/fmt";
import type { ReactNode } from "react";
import type { Album } from "../types";

/** The library's album grid card, shared by the Library and Favorites pages
 * so favorites render with exactly the same layout. The library payload
 * enriches albums with an `artist` display name; elsewhere it falls back to
 * the album-artist tag. */
export default function AlbumCard({ al, artistName, selectable, selected, onSelect, href, actions, extraMeta }: {
  al: Album & { artist?: string };
  artistName?: string;
  selectable?: boolean;
  selected?: boolean;
  onSelect?: (path: string) => void;
  /** Album page target; pass `null` to render the card without links
   * (e.g. entries that have no album page, like trashed folders). */
  href?: string | null;
  /** Overrides the play button (top-left overlay); defaults to today's button. */
  actions?: ReactNode;
  /** Extra bits for the caption's meta row (after the artist) — for pages
   *  that state more than the shared grid does. The library grid passes
   *  nothing and renders exactly as before. */
  extraMeta?: ReactNode;
}) {
  const st = statusFor(!!al.pass, al.audit_summary);
  const ref = href === undefined ? albumRef(al) : href;
  const artist = artistName ?? al.artist ?? al.album_artist ?? al.path.split(/[\\/]/).slice(0, -1).pop() ?? "";
  const ms = mediaShort(al.media || al.meta?.MEDIA);
  return (
    <div
      className={`group relative rounded-xl p-2 transition-colors hover:bg-panel/70 ${selected ? "bg-accent/10 ring-1 ring-accent/30" : ""} ${selectable ? "cursor-pointer" : ""}`}
      onClick={selectable ? () => onSelect?.(al.path) : undefined}
    >
      <div className="relative">
        {ref ? (
          <Link
            to={ref}
            onClick={selectable ? (e) => e.preventDefault() : undefined}
            title={selectable ? "Click to select" : "Open album page"}
            className="block"
          >
            <CoverImg
              albumPath={al.path}
              coverFile={al.cover_file}
              wrapperClass="aspect-square w-full rounded-xl shadow-lg ring-1 ring-black/40 overflow-hidden"
            />
          </Link>
        ) : (
          <CoverImg
            albumPath={al.path}
            coverFile={al.cover_file}
            wrapperClass="aspect-square w-full rounded-xl shadow-lg ring-1 ring-black/40 overflow-hidden"
          />
        )}
        {selectable && (
          <div
            className="absolute top-1.5 right-10 row-hover transition-opacity bg-black/60 rounded-md px-1 py-0.5"
            onClick={(e) => e.stopPropagation()}
          >
            <input type="checkbox" checked={!!selected} onChange={() => onSelect?.(al.path)} title="Select album" />
          </div>
        )}
        {(() => {
          const tech = albumTech(al.tracks, true);
          const dr = al.meta?.["ALBUM DYNAMIC RANGE"] ?? null;
          return (
            <>
              {/* quality bottom-left · media type bottom-right · DR top-left.
                  The play button lives top-left below the DR chip so it can
                  never cover the bitrate readout. */}
              {tech && (
                <span
                  className="absolute bottom-1.5 left-1.5 bg-black/65 text-zinc-300 text-[9px] font-mono tracking-wide rounded px-1 py-0.5 border border-white/10"
                  title={`Formats: ${albumTech(al.tracks)}`}
                >
                  {tech}
                </span>
              )}
              {ms ? (
                <span
                  className="absolute bottom-1.5 right-1.5 bg-black/65 text-zinc-200 text-[9px] font-semibold tracking-wide rounded px-1 py-0.5 border border-white/10"
                  title={`Media: ${al.media || al.meta?.MEDIA}`}
                >
                  {ms}
                </span>
              ) : null}
              {dr ? (
                <span
                  className="absolute top-1.5 left-1.5 bg-black/65 text-zinc-200 text-[9px] font-mono tracking-wide rounded px-1 py-0.5 border border-white/10"
                  title="Album dynamic range (DR meter)"
                >
                  DR{dr}
                </span>
              ) : null}
            </>
          );
        })()}
        <div className="absolute top-1.5 right-1.5 row-hover transition-opacity">
          <FavHeart kind="album" id={al.path} mbid={al.meta?.MUSICBRAINZ_ALBUMID} className="!p-1.5 bg-black/60" iconClass="h-4 w-4" />
        </div>
        {actions ?? (
          <button
            className="tap-hit btn-primary absolute left-2 top-9 !rounded-lg !p-3 row-hover transition-opacity shadow-2xl"
            title="Play album"
            onClick={(e) => {
              e.stopPropagation();
              useStore.getState().playNow(
                (al.tracks ?? []).map((t) => ({
                  path: t.path, file: t.file, albumPath: al.path,
                  artist, album: al.meta?.ALBUM ?? undefined, title: t.tags.TITLE || undefined,
                }))
              );
            }}
          >
            <Play className="h-4 w-4 fill-current" />
          </button>
        )}
      </div>
      <div className="mt-2 px-0.5">
        {ref ? (
          <Link
            to={ref}
            className="text-sm font-medium truncate block hover:text-accent-soft"
            title={al.meta?.ALBUM ?? al.path}
          >
            {al.meta?.ALBUM ?? al.path.split("/").pop()}
          </Link>
        ) : (
          <span className="text-sm font-medium truncate block" title={al.meta?.ALBUM ?? al.path}>
            {al.meta?.ALBUM ?? al.path.split("/").pop()}
          </span>
        )}
        {/* artist left · ORIGINAL release year bottom-right (no separator —
            the two ends read as their own columns) */}
        <div className="text-[11px] text-zinc-500 truncate flex items-center gap-1.5 mt-0.5">
          <span className={`h-1.5 w-1.5 rounded-full ${st.edge} inline-block shrink-0`} title={st.label} />
          <span className="truncate" title={artist}>{artist}</span>
          {extraMeta}
          {(() => {
            const y = originalYear(al.meta);
            return y ? (
              <span
                className="ml-auto shrink-0 tabular-nums"
                title={al.meta?.ORIGINALDATE && al.meta.ORIGINALDATE !== al.meta?.DATE
                  ? `Original release ${al.meta.ORIGINALDATE}`
                  : `Released ${al.meta?.DATE}`}
              >
                {y}
              </span>
            ) : null;
          })()}
        </div>
      </div>
    </div>
  );
}
