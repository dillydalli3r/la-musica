import CoverImg from "./CoverImg";

/** An artist's avatar: a representative album cover, then CoverImg's
 *  placeholder. The app stores no artist pictures, so the artist's own face
 *  here is the artwork of an album they released.
 *
 *  Shared by the Library's Artists view and the artist page: one rule for what
 *  an artist's face is, at whatever size the caller's box asks for. */
export default function ArtistAvatar({
  path,
  coverPath = "",
  coverFile = null,
  className = "h-9 w-9 rounded-full bg-raise overflow-hidden shrink-0",
}: {
  /** The artist folder — where CoverImg falls back when no album cover is
   *  passed (CoverImg reads the folder's own cover.*). */
  path: string;
  /** A representative album of the artist — where the fallback cover lives. */
  coverPath?: string;
  coverFile?: string | null;
  className?: string;
}) {
  return <CoverImg albumPath={coverPath || path} coverFile={coverFile} wrapperClass={className} />;
}