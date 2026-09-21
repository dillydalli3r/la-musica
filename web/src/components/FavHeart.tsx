import { Heart } from "lucide-react";
import { useFav, type FavKind } from "../lib/favs";

/** Heart toggle bound to the favorites/likes store. Stops propagation so it
 * can live inside clickable cards, rows and links. Pass `mbid` when the
 * entity has one — the backend then keeps the favorite across file moves. */
export default function FavHeart({
  kind,
  id,
  mbid,
  className = "",
  iconClass = "h-4 w-4",
  title,
  revealOnHover = false,
}: {
  kind: FavKind | "track";
  id?: string | null;
  mbid?: string | null;
  className?: string;
  iconClass?: string;
  title?: string;
  /** Row hearts keep their space and appear when the row is hovered (the
   *  caller's `.group`), so an action column costs no layout. A FAVOURITED
   *  heart is a STATE, not an action: it is drawn whether or not the pointer
   *  is anywhere near, or the row reads as unfavourited until the mouse lands
   *  on it. */
  revealOnHover?: boolean;
}) {
  const { fav, toggle } = useFav(kind, id, mbid);
  return (
    // `type="button"` explicitly: a favourite toggle must never inherit a
    // form's submit behaviour, wherever a future caller drops it.
    <button
      type="button"
      className={`tap-hit p-1.5 rounded-md hover:bg-raise shrink-0 transition-colors ${
        fav
          ? "text-accent"
          : `text-zinc-500 hover:text-zinc-200${revealOnHover ? " row-hover" : ""}`
      } ${className}`}
      onClick={(e) => {
        e.preventDefault();
        e.stopPropagation();
        toggle();
      }}
      title={title ?? (fav ? "Remove from favorites" : "Add to favorites")}
    >
      <Heart className={`${iconClass} ${fav ? "fill-current" : ""}`} />
    </button>
  );
}
