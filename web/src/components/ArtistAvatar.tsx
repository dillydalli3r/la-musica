import { useState } from "react";
import { api } from "../api";
import CoverImg from "./CoverImg";

/** An artist's avatar: the artist's own picture first, then a representative
 *  album cover, then CoverImg's placeholder.
 *
 *  The artist picture is `GET /api/artist/image` — the endpoint the artist
 *  page, every Discover row and Home's shelf read. It answers 404 for a folder
 *  that holds no picture, and a URL that 404s paints the browser's
 *  broken-image glyph before anything can replace it, so the request is only
 *  made when the payload says the folder has one (`has_image`, never a probe
 *  of our own). The URL that failed is remembered as ITSELF — the rule
 *  CoverImg and DiscoverRow keep — so a row recycled onto another artist never
 *  inherits a failure.
 *
 *  Shared by Home's artist shelf and the Library's Artists view: one rule for
 *  what an artist's face is, at whatever size the caller's box asks for. */
export default function ArtistAvatar({
  path,
  hasImage,
  coverPath = "",
  coverFile = null,
  className = "h-9 w-9 rounded-full bg-raise overflow-hidden shrink-0",
  title,
}: {
  path: string;
  hasImage?: boolean;
  /** A representative album of the artist — where the fallback cover lives. */
  coverPath?: string;
  coverFile?: string | null;
  className?: string;
  title?: string;
}) {
  const [failed, setFailed] = useState<string[]>([]);
  const picture = hasImage && path ? api.artistImageUrl(path) : null;
  if (picture && !failed.includes(picture)) {
    return (
      <div className={`${className} bg-raise overflow-hidden`}>
        <img
          src={picture}
          alt=""
          loading="lazy"
          decoding="async"
          title={title}
          onError={() => setFailed((seen) => (seen.includes(picture) ? seen : [...seen, picture]))}
          className="h-full w-full object-cover"
        />
      </div>
    );
  }
  return <CoverImg albumPath={coverPath || path} coverFile={coverFile} wrapperClass={className} />;
}
