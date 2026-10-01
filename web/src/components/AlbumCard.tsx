import { Link } from "react-router-dom";
import { Play } from "lucide-react";
import { useStore } from "../store";
import { statusFor } from "../lib/status";
import { AdvisoryMark, albumAdvisory, mediaSourceLabel, releaseCountries } from "./Badges";
import { albumTech } from "../lib/fmt";
import CoverImg from "./CoverImg";
import FavHeart from "./FavHeart";
import LockedChip from "./LockedChip";
import { PendingMark, pendingSummary } from "./Badges";
import { useI18n } from "../lib/i18n";
import { albumArtistRef, albumRef } from "../lib/refs";
import { originalYear } from "../lib/fmt";
import { ratingOf, useRatings, albumWebRating, webStarProps } from "../lib/ratings";
import StarRating from "./StarRating";
import type { ReactNode } from "react";
import type { GridSize } from "../lib/libraryView";
import type { Album } from "../types";

/** The server thumbnail bucket each cover size asks for (the buckets the
 *  server keeps are 160/320/640/1200 — `artcache.THUMB_SIZES`).
 *
 *  A card DRAWS its cover at `GRID_SIZE_MIN[size]` px (126/164/214 — lib/fmt),
 *  but a grid column is `minmax(min, 1fr)`: it stretches past that minimum
 *  whenever the row has room to spare, and a hi-DPI display draws the box in
 *  twice the pixels. The bucket is therefore the next one up from the box
 *  rather than the box itself — big enough that the card is not upscaled in
 *  ordinary use, small enough that a shelf is not downloading the 1200–3000 px
 *  MASTER (which the server answers with `no-cache` and no thumbnail bucket)
 *  for a 126 px square. The master stays what a surface that really draws it
 *  large asks for: an album page's hero, the fullscreen player's pane. */
export const GRID_COVER_W: Record<GridSize, number> = { s: 160, m: 320, l: 640 };

/** The cover size the stored setting names, for the pages that draw a card
 *  WITHOUT owning the cover-size control (Favorites, Artist, Playlists,
 *  Podcast, Trash all read `mlo.gridSize` — lib/libraryView's own key for it —
 *  and a card that ignored it would ask the wrong bucket there). A page that
 *  DOES own the control passes the size it picked, so the click re-renders the
 *  page and its cards in the same pass. */
function storedGridSize(): GridSize {
  try {
    const v = localStorage.getItem("mlo.gridSize");
    return v === "s" || v === "l" ? v : "m";
  } catch {
    return "m"; // no localStorage (private mode): the default size
  }
}

/** The app's album grid card — the Library, Favorites, Artist, Trash,
 * "more like this" and every Home shelf draw an album with this one card, so
 * one album can never look like two. The library payload enriches albums with
 * an `artist` display name; elsewhere it falls back to the album-artist tag. */
export default function AlbumCard({ al, artistName, selectable, selected, onSelect, href, actions, extraMeta, size }: {
  /** `owned: false` marks a row the library does not hold (a favourite whose
   *  folder moved away): nothing has graded it and
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
  /** Overrides the play button. The CARD places it — the button's own band
   * (top-left, under the ADR chip) is part of the overlay's one column — so
   * pass the button itself, without a position of its own. */
  actions?: ReactNode;
  /** The caller's own bits for the row, drawn on their own line under the
   *  caption — for pages that state more than the shared grid does (Home
   *  passes its shelf's reason chip here). Only rendered when given, so the
   *  library grid passes nothing and renders exactly as before. */
  extraMeta?: ReactNode;
  /** The cover size this card is drawn at (lib/libraryView's shared setting).
   *  It is what the card asks the server for (`GRID_COVER_W`): a page that
   *  offers the control passes what the user picked, and a page that does not
   *  is answered from the stored setting. Omitted, the card still never asks
   *  for the master. */
  size?: GridSize;
}) {
  const st = statusFor(!!al.pass, al.audit_summary);
  const ref = href === undefined ? albumRef(al) : href;
  // A row outside the library has no verdict, no audio and nothing to
  // favourite: the three controls that would claim otherwise are left off.
  const inLibrary = al.owned !== false;
  const artist = artistName ?? al.artist ?? al.album_artist ?? al.path.split(/[\\/]/).slice(0, -1).pop() ?? "";
  const media = al.media || al.meta?.MEDIA;
  /* The medium AND, for a download, its provenance ("Digital · Bandcamp") —
   * the same words the album page's own chip prints, built by the same
   * helper, so a card and the page it opens cannot name one release two ways.
   * The countries stay their own badge below (one long list wraps inside its
   * own line instead of stretching the medium). */
  const mediaLabel = mediaSourceLabel(media, al.source_summary);
  const countries = releaseCountries(al.meta?.RELEASECOUNTRY);
  // The two cover facts that are not the release country: the release's own ADR
  // and the codec/bitrate summary. Read once here because the overlay draws the
  // ADR chip, the button and the chips as ONE column, in that order.
  const dr = al.meta?.["ALBUM DYNAMIC RANGE"] ?? null;
  const tech = albumTech(al.tracks, true);
  // The server thumbnail this card's cover asks for: the caller's size when it
  // owns the control, else the stored setting (see storedGridSize).
  const coverW = GRID_COVER_W[size ?? storedGridSize()];
  const { t } = useI18n();
  // The album's OWN rating (the user's verdict on the album, never the average
  // of its tracks — lib/ratings owns that distinction), read here rather than
  // passed in: this card draws the same album on six pages, and one of them
  // enriching its own rows would leave the other five without stars. ONE
  // request per page and scope, shared by every card through the query cache.
  const { data: albumRatings } = useRatings("album");
  const rating = ratingOf(albumRatings?.ratings, al.path);
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
              w={coverW}
              wrapperClass="aspect-square w-full rounded-xl shadow-lg overflow-hidden"
            />
          </Link>
        ) : (
          <CoverImg
            albumPath={al.path}
            coverFile={al.cover_file}
            w={coverW}
            wrapperClass="aspect-square w-full rounded-xl shadow-lg overflow-hidden"
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
        {inLibrary && (
          <div className="absolute top-1.5 right-1.5">
            <FavHeart kind="album" id={al.path} mbid={al.meta?.MUSICBRAINZ_ALBUMID} className="!p-1.5 bg-black/60" iconClass="h-4 w-4" revealOnHover />
          </div>
        )}
        {/* The cover's chrome is ONE flow column, not three overlays that each
            guess where the others end. It used to be a `top-9` play button plus
            a `bottom-1.5` chip column that grew UPWARD, so a card too small for
            both (a phone at cover size S, or an album carrying a long release
            country list) drew a country chip straight over the play circle. In
            one column nothing can land on anything else whatever the card's
            size: the chips take the room left under the button, and a card with
            less room than that clips its own last chip rather than covering the
            control (`min-h-0` + `overflow-hidden` below).

            The ADR chip's row is reserved whether or not this release has one,
            so the button below it sits at the same offset on every card and a
            shelf's buttons stay on one line — that is what `top-9` used to pin,
            except it only pinned it until the chip column grew into it. */}
        <div className="pointer-events-none absolute inset-x-1.5 top-1.5 bottom-1.5 flex flex-col items-start min-h-0">
          <div className="h-5 shrink-0 flex items-center">
            {dr ? (
              <span
                className="pointer-events-auto bg-black/65 text-zinc-200 text-[9px] font-mono tracking-wide rounded px-1 py-0.5 border border-white/10"
                title="Album dynamic range (ADR): the release's own DR, one value for every track on it"
              >
                ADR{dr}
              </span>
            ) : null}
          </div>
          {inLibrary && (
            /* The button's band, placed by the card: the caller's `actions`
               button lands here too, so it carries no offset of its own. A
               framework album has no audio to play, so the default button is
               OFF rather than a control that starts nothing — the band carries
               the sentence (a disabled button does not get its own tooltips).
               `pointer-events-auto`: the column itself must let the cover's
               own link and the heart through. */
            <div
              className="ml-0.5 mt-2.5 shrink-0 pointer-events-auto"
              title={actions || !pending ? undefined : pending.full}
            >
              {actions ?? (
                <button
                  className={`tap-hit btn-primary !rounded-lg !p-3 row-hover transition-opacity shadow-2xl${pending ? " opacity-60 cursor-not-allowed" : ""}`}
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
              )}
            </div>
          )}
          {/* One LEFT-aligned column of the three facts a reader scans a shelf
              for: which pressing it is, where it came from, what is inside.
              They used to be two corner chips with the release countries fused
              onto the medium and pushed to the right edge, so the same album's
              format chip moved with the length of its country list (#38).
              Bitrate sits at the BOTTOM, the line a reader's eye lands on first
              in a grid, and each row is its own badge so one long country list
              wraps inside its own line instead of stretching its neighbours.
              `mt-auto` keeps the column on the cover's bottom edge while there
              is room; with none, it shrinks and clips instead of riding up into
              the button. */}
          {(mediaLabel || countries.length > 0 || tech) && (
            <div className="mt-auto min-h-0 max-w-full overflow-hidden flex flex-col items-start justify-end gap-1 pointer-events-none">
              {mediaLabel && (
                <span
                  className="pointer-events-auto max-w-full break-words text-[9px] font-semibold tracking-wide leading-snug rounded px-1 py-0.5 border border-white/10 bg-black/65 text-zinc-200"
                  title={`Media: ${media}${al.source_summary ? ` · Source: ${al.source_summary}` : ""}`}
                >
                  {mediaLabel}
                </span>
              )}
              {countries.length > 0 && (
                <span
                  className="pointer-events-auto max-w-full break-words text-[9px] tracking-wide leading-snug rounded px-1 py-0.5 border border-white/10 bg-black/65 text-zinc-300"
                  title={`Released in ${countries.join(", ")}`}
                >
                  {countries.join(", ")}
                </span>
              )}
              {tech && (
                <span
                  className="pointer-events-auto max-w-full break-words bg-black/65 text-zinc-300 text-[9px] font-mono tracking-wide rounded px-1 py-0.5 border border-white/10"
                  title={`Formats: ${albumTech(al.tracks)}`}
                >
                  {tech}
                </span>
              )}
            </div>
          )}
        </div>
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
          {/* The album's advisory, RIGHT beside the title: the card showed the
              pressing, the countries, the bitrate and the rating while the one
              thing a reader looks for next to a name — whether the album is
              explicit — was missing, even for albums whose own tracks say so
              (see `albumAdvisory`: the album tag can lag its tracks). */}
          <AdvisoryMark value={albumAdvisory(al)} />
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
          {/* The artist OPENS its page: the caption named an artist a reader
              could not follow, while the title beside it was already a link.
              Same rule as the title (MBID when the album carries one, the
              containing folder otherwise — lib/refs). */}
          <Link
            to={albumArtistRef(al)}
            className="truncate min-w-0 hover:text-accent-soft"
            title={artist}
          >
            {artist}
          </Link>
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
        {/* The album's rating, UNDER the artist/year caption: the grid showed
            covers and captions while the same albums' ratings were only visible
            in the table view's own column, so a shelf of grid cards said
            nothing about how the user had judged them. Read-only here — the
            table row and the album page are where a rating is EDITED — and
            drawn for library albums only, like the status dot above it: a row
            the library does not hold (`owned: false`) has no album to
            have a verdict about. */}
        {inLibrary && (
          <div className="mt-0.5">
            {/* The value rides WITH the stars. It used to be missing here and
                added by one shelf on top (the Home page's "Your ratings" drew
                its own row under this one), which is how a card ended up with
                two rows of stars under it — the same rating twice, and the
                reader left to work out which one was theirs. One row, on the
                card, with the number in it: every surface that draws a card
                gets the same thing. */}
            <StarRating size="sm" readOnly showValue label="Album rating" value={rating} {...webStarProps(albumWebRating(al.tracks))} webKind="album" />
          </div>
        )}
        {/* The caller's own bits sit on a line of their OWN: sharing this one
            with the artist and the year cost a 6-column shelf its artist
            ("Syst…" beside a reason chip), and the chip is the shelf's own
            sentence, not a caption. */}
        {extraMeta && <div className="mt-1 flex items-center gap-1.5 min-w-0">{extraMeta}</div>}
      </div>
    </div>
  );
}
