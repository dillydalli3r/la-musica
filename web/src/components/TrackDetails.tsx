import { X, ShieldCheck, CircleAlert, Info, ExternalLink } from "lucide-react";
import { Link } from "react-router-dom";
import type { Track } from "../types";
import { isVideoTech } from "../lib/fmt";
import { trackRef } from "../lib/refs";
import { AuditBadge, GradeBadge } from "./Badges";
import TrackDownloadExport from "./TrackDownloadExport";

function fmtTech(tech: Track["tech"]): string {
  const video = isVideoTech(tech);
  const parts: string[] = [];
  // Video files: resolution only, plus the audio stream's shape — video
  // codecs and container bitrates are never shown.
  if (tech.width && tech.height) parts.push(`${tech.width}×${tech.height}`);
  else if (tech.width) parts.push(`${tech.width}p`);
  if (!video && tech.codec) parts.push(`${tech.codec}`);
  const pair =
    tech.bits_per_sample && tech.sample_rate
      ? `${Math.round(tech.bits_per_sample)}/${(tech.sample_rate / 1000).toFixed(1).replace(/\.0$/, "")}`
      : tech.bits_per_sample
        ? `${Math.round(tech.bits_per_sample)} bit`
        : tech.sample_rate
          ? `${(tech.sample_rate / 1000).toFixed(1).replace(/\.0$/, "")} kHz`
          : "";
  if (pair) parts.push(pair);
  if (tech.length) {
    const m = Math.floor(tech.length / 60);
    const s = Math.round(tech.length % 60);
    parts.push(`${m}:${String(s).padStart(2, "0")}`);
  }
  if (!video && tech.bitrate) parts.push(`${Math.round(tech.bitrate / 1000)} kbps`);
  if (tech.channels) parts.push(tech.channels === 1 ? "mono" : tech.channels === 2 ? "stereo" : `${tech.channels} ch`);
  return parts.join(" · ");
}

const INFO_ROWS: { key: string; label: string }[] = [
  { key: "ARTIST", label: "Artist" },
  { key: "ALBUMARTIST", label: "Album artist" },
  { key: "ALBUM", label: "Album" },
  { key: "TRACKNUMBER", label: "Track #" },
  { key: "DISCNUMBER", label: "Disc #" },
  { key: "DATE", label: "Date" },
  { key: "ORIGINALDATE", label: "Original date" },
  { key: "GENRE", label: "Genre" },
  { key: "MEDIA", label: "Media" },
  { key: "SOURCE", label: "Source" },
  { key: "RELEASETYPE", label: "Release type" },
  { key: "RELEASECOUNTRY", label: "Country" },
  { key: "LABEL", label: "Label" },
  { key: "CATALOGNUMBER", label: "Catalog #" },
  { key: "COMPOSER", label: "Composer" },
  { key: "LYRICIST", label: "Lyricist" },
  { key: "REMIXER", label: "Remixer" },
  { key: "COPYRIGHT", label: "Copyright" },
  { key: "ISRC", label: "ISRC" },
  { key: "MUSICBRAINZ_TRACKID", label: "MB recording" },
  { key: "MUSICBRAINZ_ALBUMID", label: "MB release" },
];

/** Per-track song info + grading/audit detail modal (metadata, credits,
 * tech, lyrics, checks, verdicts). */
export default function TrackDetails({
  track,
  albumPath,
  onClose,
}: {
  track: Track;
  albumPath: string;
  onClose: () => void;
}) {
  const issues: string[] = track.issues ?? [];
  const values = track.values ?? {};
  const tags = track.tags ?? {};
  const checkRows = Object.entries(values).filter(([k]) => !["GENRE", "ITUNESADVISORY", "INSTRUMENTAL", "MEDIA", "SOURCE"].includes(k));
  const failKeys = new Set(issues.map((i) => i.toUpperCase()));
  const infoRows = INFO_ROWS.filter(({ key }) => tags[key as keyof typeof tags]);
  const tech = fmtTech(track.tech ?? {});
  const lyricsState = track.lyrics_embedded ? "embedded" : track.lyrics_lrc ? ".lrc sidecar" : "missing";

  return (
    <div className="fixed inset-0 z-40 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4 sm:p-6" onClick={onClose}>
      <div
        className="bg-card border border-border rounded-xl w-full max-w-lg max-h-[85vh] overflow-auto shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 px-5 py-3 border-b border-border sticky top-0 bg-card z-10">
          <ShieldCheck className="h-4 w-4 text-accent" />
          <span className="font-semibold text-sm truncate">{track.tags?.TITLE ?? track.file}</span>
          <button className="ml-auto p-1 text-zinc-500 hover:text-white" onClick={onClose}>
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="p-5 space-y-4">
          {/* ---- song info: metadata & credits ---- */}
          <div>
            <div className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-zinc-400 mb-1.5">
              <Info className="h-3.5 w-3.5" /> Song info
            </div>
            {tech && <div className="text-[11px] font-mono text-zinc-500 mb-1.5">{tech}</div>}
            <div className="rounded-md border border-border overflow-hidden">
              <table className="w-full text-xs">
                <tbody>
                  <tr>
                    <td className="px-2 py-1 text-zinc-500 w-28 align-top">Title</td>
                    <td className="px-2 py-1 text-zinc-200">{track.tags?.TITLE ?? track.file}</td>
                  </tr>
                  {infoRows.map(({ key, label }) => (
                    <tr key={key}>
                      <td className="px-2 py-1 text-zinc-500 align-top">{label}</td>
                      <td className="px-2 py-1 text-zinc-200 break-all">{String(tags[key as keyof typeof tags])}</td>
                    </tr>
                  ))}
                  <tr>
                    <td className="px-2 py-1 text-zinc-500">Lyrics</td>
                    <td className="px-2 py-1 text-zinc-200">{lyricsState}</td>
                  </tr>
                  <tr>
                    <td className="px-2 py-1 text-zinc-500 align-top">Path</td>
                    <td className="px-2 py-1 text-zinc-200 break-all">{track.path ?? albumPath}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <GradeBadge pass={!issues.length} score={issues.length ? 0 : 100} />
            <AuditBadge audit={track.audit} />
            {track.log_grade != null && track.log_grade !== "" && (
              <span className="chip bg-raise border border-border text-zinc-300">log {track.log_grade}/100</span>
            )}
            {track.accuraterip_status && (
              <span className="chip bg-raise border border-border text-zinc-300">AR {track.accuraterip_status}</span>
            )}
            {track.checksum_status && (
              <span className="chip bg-raise border border-border text-zinc-300">CS {track.checksum_status}</span>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <TrackDownloadExport path={track.path} title={track.tags?.TITLE ?? track.file} />
            {/* jump to the track's own page for full tag editing */}
            <Link
              to={trackRef({ path: track.path, tags: { MUSICBRAINZ_TRACKID: track.tags?.MUSICBRAINZ_TRACKID } })}
              className="btn-ghost !py-1.5 text-xs"
              title="Open the track page"
            >
              <ExternalLink className="h-3.5 w-3.5" /> Track page
            </Link>
          </div>

          {issues.length > 0 && (
            <div>
              <div className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-zinc-400 mb-1.5">
                <CircleAlert className="h-3.5 w-3.5 text-red-400" /> Failed checks ({issues.length})
              </div>
              <ul className="space-y-1">
                {issues.map((iss, i) => (
                  <li key={i} className="text-xs text-red-300/90 bg-red-950/30 border border-red-900/40 rounded px-2 py-1">
                    {iss}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {checkRows.length > 0 && (
            <div>
              <div className="text-xs font-semibold uppercase tracking-wider text-zinc-400 mb-1.5">Check results</div>
              <div className="rounded-md border border-border overflow-hidden">
                <table className="w-full text-xs">
                  <tbody>
                    {checkRows.map(([k, v]) => {
                      const failed = failKeys.has(k.toUpperCase());
                      return (
                        <tr key={k} className={failed ? "bg-red-950/20" : ""}>
                          <td className="px-2 py-1 text-zinc-500">{k}</td>
                          <td className={`px-2 py-1 text-right ${failed ? "text-red-400" : "text-emerald-400"}`}>
                            {failed ? "FAIL" : v === null || v === "" ? "—" : "OK"}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          <div className="grid grid-cols-2 gap-2 text-xs text-zinc-400">
            <div>Sidecar cover <span className="text-zinc-200">{track.sidecar_cover ? "yes" : "no"}</span></div>
            <div>Unreadable <span className="text-zinc-200">{track.unreadable ? "yes" : "no"}</span></div>
          </div>
        </div>
      </div>
    </div>
  );
}
