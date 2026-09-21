import { Link } from "react-router-dom";
import { Play } from "lucide-react";
import { useStore } from "../store";
import { statusFor } from "../lib/status";
import { mediaCountryLabel, releaseCountries } from "./Badges";
import { albumTech } from "../lib/fmt";
import CoverImg from "./CoverImg";
import FavHeart from "./FavHeart";
import LockedChip from "./LockedChip";
import { PendingMark, pendingSummary } from "./Badges";
import { useI18n } from "../lib/i18n";
import { albumRef } from "../lib/refs";
import { originalYear } from "../lib/fmt";
import type { ReactNode } from "react";
import type { Album } from "../types";

/** The app's album grid card — the Library, Favorites, Artist, Trash,
 * "more like this" and every Home shelf draw an album with this one card, so
 * one album can never look like two. The library payload enriches albums with
 * an `artist` display name; elsewhere it falls back to the album-artist tag. */
export default function AlbumCard({ al, artistName, selectable, selected, onSelect, href, actions, extraMeta }: {
  /** `owned: false` marks a row the library does not hold (Home's Soulseek
   *  wishes, a favourite whose folder moved away): nothing has graded it and
   *  there is no audio to play or favourite yet, so the card draws identity
   *  only — no status dot, no play button, no heart — instead of reading its
   *  own missing fields as verdicts. */
  al: Album & { artist?: string; owned?: boolean };
  artistName?: string;
  selectable?: boolean;
  selected?: boolean;
  onSelect?: (path: string) => void;
  /** Album page target; pass `null` to render the card without links
   * (e.g. entries that have no album page, like trashed folders). */
  href?: string | null;
  /** Overrides the play button (top-left overlay); defaults to today's button. */
  actions?: ReactNode;
  /** The caller's own bits for the row, drawn on their own line under the
   *  caption — for pages that state more than the shared grid does (Home
   *  passes its shelf's reason chip here). Only rendered when given, so the
   *  library grid passes nothing and renders exactly as before. */
  extraMeta?: ReactNode;
}) {
  const st = statusFor(!!al.pass, al.audit_summary);
  const ref = href === undefined ? albumRef(al) : href;
  // A row outside the library has no verdict, no audio and nothing to
  // favourite: the three controls that would claim otherwise are left off.
  const inLibrary = al.owned !== false;
  const artist = artistName ?? al.artist ?? al.album_artist ?? al.path.split(/[\\/]/).slice(0, -1).pop() ?? "";
  const media = al.media || al.meta?.MEDIA;
  const countries = releaseCountries(al.meta?.RELEASECOUNTRY);
  const mediaBadge = mediaCountryLabel(media, al.meta?.RELEASECOUNTRY);
  const { t } = useI18n();
  // The pending mark's own sentence (null for a complete album), used both for
  // the dot and for the reason the play button is off.
  const pending = pendingSummary(al, t);
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
              {/* quality bottom-left · media + release countries bottom-right ·
                  DR top-left. The play button lives top-left below the DR chip
                  so it can never cover the bitrate readout.

                  The two bottom badges share ONE wrapping row: the media badge
                  carries the release countries too, and a release tagged for
                  several ("CD · US, CA, JP") needs the width to say so instead
                  of a fixed corner chip it would overflow. The row itself is
                  not clickable — only the badges are — so the cover link
                  underneath still takes a click between them. */}
              {(tech || mediaBadge) && (
                <div className="absolute inset-x-1.5 bottom-1.5 flex flex-wrap items-end gap-1.5 pointer-events-none">
                  {tech && (
                    <span
                      className="pointer-events-auto shrink-0 bg-black/65 text-zinc-300 text-[9px] font-mono tracking-wide rounded px-1 py-0.5 border border-white/10"
                      title={`Formats: ${albumTech(al.tracks)}`}
                    >
                      {tech}
                    </span>
                  )}
                  {mediaBadge && (
                    <span
                      className="pointer-events-auto ml-auto shrink-0 max-w-full text-right text-[9px] font-semibold tracking-wide leading-snug break-words rounded px-1 py-0.5 border border-white/10 bg-black/65 text-zinc-200"
                      title={[
                        media ? `Media: ${media}` : "",
                        countries.length ? `Released in ${countries.join(", ")}` : "",
                      ]
                        .filter(Boolean)
                        .join(" · ")}
                    >
                      {mediaBadge}
                    </span>
                  )}
                </div>
              )}
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
        {inLibrary && (
          <div className="absolute top-1.5 right-1.5">
            <FavHeart kind="album" id={al.path} mbid={al.meta?.MUSICBRAINZ_ALBUMID} className="!p-1.5 bg-black/60" iconClass="h-4 w-4" revealOnHover />
          </div>
        )}
        {inLibrary && (actions ?? (
          /* A framework album has no audio to play, so the button is OFF
             rather than a control that starts nothing: the wrapper carries the
             sentence (a disabled button does not get its own tooltips). */
          <span title={pending ? pending.full : undefined}>
            <button
              className={`tap-hit btn-primary absolute left-2 top-9 !rounded-lg !p-3 row-hover transition-opacity shadow-2xl${pending ? " opacity-60 cursor-not-allowed" : ""}`}
              title={pending ? pending.full : "Play album"}
              aria-label={pending ? pending.full : "Play album"}
              disabled={!!pending}
              onClick={(e) => {
                e.stopPropagation();
                useStore.getState().playNow(
                  (al.tracks ?? []).map((t) => ({
                    path: t.path, file: t.file, albumPath: al.path,
                    artist, album: al.meta?.ALBUM ?? undefined, title: t.tags.TITLE || undefined,
                    advisory: t.tags.ITUNESADVISORY ?? null,
                  }))
                );
              }}
            >
              <Play className="h-4 w-4 fill-current" />
            </button>
          </span>
        ))}
      </div>
      <div className="mt-2 px-0.5">
        <div className="flex items-center gap-1.5 min-w-0">
        {ref ? (
          <Link
            to={ref}
            className="text-sm font-medium truncate min-w-0 hover:text-accent-soft"
            title={al.meta?.ALBUM ?? al.path}
          >
            {al.meta?.ALBUM ?? al.path.split("/").pop()}
          </Link>
        ) : (
          <span className="text-sm font-medium truncate min-w-0" title={al.meta?.ALBUM ?? al.path}>
            {al.meta?.ALBUM ?? al.path.split("/").pop()}
          </span>
        )}
          {/* the folder is held by a job right now: its files cannot be played
              until that job finishes (the chip's tooltip is the server's own
              refusal sentence) */}
          <LockedChip path={al.path} />
          {/* added, not downloaded yet. A different fact from the lock above:
              there is no audio to hold yet, and the folder can be both. */}
          <PendingMark album={al} />
        </div>
        {/* artist left · ORIGINAL release year bottom-right (no separator —
            the two ends read as their own columns) */}
        <div className="text-[11px] text-zinc-500 truncate flex items-center gap-1.5 mt-0.5">
          {inLibrary && (
            <span className={`h-1.5 w-1.5 rounded-full ${st.edge} inline-block shrink-0`} title={st.label} />
          )}
          <span className="truncate" title={artist}>{artist}</span>
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
        {/* The caller's own bits sit on a line of their OWN: sharing this one
            with the artist and the year cost a 6-column shelf its artist
            ("Syst…" beside a reason chip), and the chip is the shelf's own
            sentence, not a caption. */}
        {extraMeta && <div className="mt-1 flex items-center gap-1.5 min-w-0">{extraMeta}</div>}
      </div>
    </div>
  );
}
