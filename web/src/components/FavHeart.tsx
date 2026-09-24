import { Heart } from "lucide-react";
import { useFav, type FavKind } from "../lib/favs";

/** The app's ONE heart.
 *
 *  Every favourite toggle in the UI is this component: the rows and cards that
 *  used to call `FavHeart` directly (library, browse, album, artist, playlist,
 *  favorites pages), and the player's two bar hearts plus the fullscreen
 *  player's — which used to be three separate copies of the button, each
 *  writing likes its own way (two of them without the optimistic update or the
 *  `aria-pressed` the other one had). `useFav` in `lib/favs.ts` is the single
 *  writer; this is the single button, so state, the optimistic patch, the
 *  invalidation, `aria-pressed` and "idle means unpressable" cannot drift
 *  between the four places the heart now appears.
 *
 *  What is allowed to differ per site is only what genuinely differs — the
 *  box's padding and radius (`boxClass`), the unliked colour (`unlikedClass`,
 *  because the fullscreen player draws its chrome in ink, not in zinc), the
 *  glyph's size (`iconClass`), the placement (`className`), the idle state
 *  (`disabled`) and the wording (`likeLabels`, plus an explicit `title`). No
 *  site draws a heart of its own.
 *
 *  The click stops propagating: the heart lives inside clickable cards, rows
 *  and links all over the app, where a bare toggle would also open whatever
 *  the heart sits in. */
export default function FavHeart({
  kind,
  id,
  mbid,
  className = "",
  iconClass = "h-4 w-4",
  title,
  revealOnHover = false,
  boxClass = "tap-hit p-1.5 rounded-md hover:bg-raise shrink-0 transition-colors",
  unlikedClass = "text-zinc-500 hover:text-zinc-200",
  likeLabels = false,
  disabled = false,
  onToggled,
  onError,
}: {
  kind: FavKind | "track";
  id?: string | null;
  mbid?: string | null;
  className?: string;
  iconClass?: string;
  /** Overrides both the tooltip and the screen-reader label. */
  title?: string;
  /** Row hearts keep their space and appear when the row is hovered (the
   *  caller's `.group`), so an action column costs no layout. A FAVOURITED
   *  heart is a STATE, not an action: it is drawn whether or not the pointer
   *  is anywhere near, or the row reads as unfavourited until the mouse lands
   *  on it. */
  revealOnHover?: boolean;
  /** The button's own box: padding, radius, hover wash. The one part of the
   *  look that really is per-site (a 3.5-px row heart and the fullscreen
   *  player's 18-px one are not the same object). */
  boxClass?: string;
  /** The colour the heart wears while the entity is NOT favourited. The
   *  favourited colour is deliberately not configurable: a lit heart is
   *  `text-accent` everywhere, which is what makes the lit state readable at a
   *  glance across the app. */
  unlikedClass?: string;
  /** The player's hearts talk about LIKING THIS TRACK, the library's about
   *  favorites — the two vocabularies the app already used at each site, kept
   *  so this de-duplication changes no user-visible string. */
  likeLabels?: boolean;
  /** Idle (nothing loaded / nothing playing) — drawn faded and unpressable,
   *  exactly as the player bar has always shown it. */
  disabled?: boolean;
  /** Called with the entity's new state once the server agreed: what a player
   *  toasts, and what a row says nothing about. */
  onToggled?: (liked: boolean) => void;
  /** The write failed; the site decides how loudly to say so. */
  onError?: (e: unknown) => void;
}) {
  const { fav, toggle } = useFav(kind, id, mbid, { onToggled, onError });
  const label =
    title ?? (likeLabels ? (fav ? "Unlike" : "Like this track") : fav ? "Remove from favorites" : "Add to favorites");
  return (
    // `type="button"` explicitly: a favourite toggle must never inherit a
    // form's submit behaviour, wherever a future caller drops it.
    <button
      type="button"
      aria-label={label}
      aria-pressed={fav}
      disabled={disabled}
      className={`${boxClass} ${
        fav ? "text-accent" : `${unlikedClass}${revealOnHover ? " row-hover" : ""}`
      } ${className}${disabled ? " opacity-40 pointer-events-none" : ""}`}
      onClick={(e) => {
        e.preventDefault();
        e.stopPropagation();
        toggle();
      }}
      title={label}
    >
      <Heart className={`${iconClass} ${fav ? "fill-current" : ""}`} />
    </button>
  );
}
