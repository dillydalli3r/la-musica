/** Shared compact audio-format readout: "FLAC 16/44.1 · 1022 kbps" —
 * codec with its bit depth/sample rate first, bitrate last. */
export interface TechInfo {
  codec?: string;
  bitrate?: number;
  bits_per_sample?: number;
  sample_rate?: number;
  width?: number;
  height?: number;
}

/** Music-video containers the app plays with <video> (mirrors the
 * backend's LIB_VIDEO_EXTS). */
const VIDEO_EXTS = new Set([
  ".mp4", ".m4v", ".mkv", ".webm", ".mov", ".vob", ".mpg", ".mpeg",
  ".m2v", ".ts", ".m2ts", ".mts", ".avi", ".wmv", ".flv", ".ogv",
  ".3gp", ".3g2",
]);

const VIDEO_CODEC_RE = /^(h ?264|h ?265|hevc|av1|vp[89]|mpeg|vc-?1|theora|prores|divx|xvid|wmv|flv|rawvideo|png|mjpeg)/i;

/** True when this tech payload describes a VIDEO stream (its bitrate is
 * the container's total, its codec a video one) — bitrate readouts must
 * only ever speak about the AUDIO stream, so callers skip these. */
export function isVideoTech(t?: TechInfo | null): boolean {
  if (!t) return false;
  if (t.width && t.height) return true;
  return !!t.codec && VIDEO_CODEC_RE.test(String(t.codec));
}

export function isVideoFile(fileOrPath: string | null | undefined): boolean {
  if (!fileOrPath) return false;
  const m = (fileOrPath.match(/\.([a-z0-9]+)$/i) ?? [])[0];
  return !!m && VIDEO_EXTS.has(m.toLowerCase());
}

/** Human bitrate — always kilobits per second, never Mbps. */
export function fmtBitrate(bps?: number | null): string {
  if (!bps) return "";
  return `${Math.round(bps / 1000)} kbps`;
}

/** Ultra-condensed depth/rate readout for beside the title: "16/44.1".
 * Video files fall back to their resolution — the one figure that
 * identifies them. */
export function fmtPair(t?: TechInfo | null): string {
  if (!t) return "";
  if (t.width && t.height) return `${t.width}×${t.height}`;
  if (t.bits_per_sample && t.sample_rate)
    return `${Math.round(t.bits_per_sample)}/${(t.sample_rate / 1000).toFixed(1).replace(/\.0$/, "")}`;
  if (t.bits_per_sample) return `${Math.round(t.bits_per_sample)} bit`;
  if (t.sample_rate) return `${(t.sample_rate / 1000).toFixed(1).replace(/\.0$/, "")} kHz`;
  return "";
}

export function fmtTech(t?: TechInfo | null): string {
  if (!t) return "";
  // Video files: resolution + the AUDIO stream's depth/rate only — video
  // codec names (MPEG2VIDEO…) are noise and the container's total bitrate
  // says nothing about the music.
  if (isVideoTech(t)) {
    const parts: string[] = [];
    if (t.width && t.height) parts.push(`${t.width}×${t.height}`);
    if (t.bits_per_sample && t.sample_rate)
      parts.push(`${Math.round(t.bits_per_sample)}/${(t.sample_rate / 1000).toFixed(1).replace(/\.0$/, "")}`);
    else if (t.sample_rate)
      parts.push(`${(t.sample_rate / 1000).toFixed(1).replace(/\.0$/, "")} kHz`);
    return parts.join(" · ");
  }
  const pair =
    t.bits_per_sample && t.sample_rate
      ? `${Math.round(t.bits_per_sample)}/${(t.sample_rate / 1000).toFixed(1).replace(/\.0$/, "")}`
      : t.bits_per_sample
        ? `${Math.round(t.bits_per_sample)} bit`
        : t.sample_rate
          ? `${(t.sample_rate / 1000).toFixed(1).replace(/\.0$/, "")} kHz`
          : "";
  const parts: string[] = [];
  if (t.codec) parts.push(`${t.codec}${pair ? ` ${pair}` : ""}`);
  else if (pair) parts.push(pair);
  if (t.bitrate) parts.push(fmtBitrate(t.bitrate));
  return parts.join(" · ");
}

/** Aggregated album-level readout from the album's tracks — codecs with
 * their depth/rate pair first, bitrate figure last (mean when the tracks
 * are close, min–max range when they drift): "FLAC 16/44.1 · 904–1079 kbps".
 * `short` drops the bitrate (for badges): "FLAC 16/44.1". */
export function albumTech(
  tracks?: { tech?: TechInfo & { length?: number; channels?: number } }[] | null,
  short = false
): string {
  const techs = (tracks ?? [])
    .map((t) => t?.tech)
    // video streams carry container bitrates and video codecs — the album
    // format chip speaks ONLY about the audio
    .filter((t): t is TechInfo => !!t && !isVideoTech(t) && !!(t.codec || t.sample_rate));
  if (!techs.length) return "";
  const codecs = [...new Set(techs.map((t) => String(t.codec).toUpperCase()).filter(Boolean))];
  const pairs = [
    ...new Set(
      techs
        .filter((t) => t.bits_per_sample && t.sample_rate)
        .map((t) => `${Math.round(t.bits_per_sample!)}/${(t.sample_rate! / 1000).toFixed(1).replace(/\.0$/, "")}`)
    ),
  ];
  const pair = pairs.length === 1 ? pairs[0] : pairs.length > 1 ? "mixed" : "";
  const parts: string[] = [];
  if (codecs.length) parts.push(`${codecs.join("/")}${pair ? ` ${pair}` : ""}`);
  else if (pair) parts.push(pair);
  if (!short) {
    const brs = techs.map((t) => t.bitrate).filter((b): b is number => !!b);
    if (brs.length) {
      const min = Math.min(...brs);
      const max = Math.max(...brs);
      parts.push(
        max - min <= Math.max(0.05 * max, 20000)
          ? `${Math.round(brs.reduce((a, b) => a + b, 0) / brs.length / 1000)} kbps`
          : `${Math.round(min / 1000)}–${Math.round(max / 1000)} kbps`
      );
    }
  }
  return parts.join(" · ");
}

/** Grid cover sizes (small / medium / large) → grid-template min column,
 * shared by the library and favourites album grids. */
export const GRID_SIZE_MIN: Record<"s" | "m" | "l", number> = { s: 126, m: 164, l: 214 };

/** The year shown on cards/cells: the ORIGINAL release year when tagged
 * (a remaster keeps its original year), the release year otherwise. */
export function originalYear(meta?: { ORIGINALDATE?: string | null; DATE?: string | null } | null): string {
  const src = meta?.ORIGINALDATE || meta?.DATE || "";
  const m = String(src).match(/^(\d{4})/);
  return m ? m[1] : "";
}

/** Year by default ("2010-12-15" -> "2010"); full value when the user
 *  enables Show full dates. The raw date is always the tooltip. */
export function fmtDateCell(value: string | null | undefined, full: boolean): string {
  if (!value) return "—";
  if (full) return value;
  const m = String(value).match(/^(\d{4})/);
  return m ? m[1] : value;
}

/** mm:ss (h:mm:ss past an hour). Non-finite (Infinity / NaN) comes from
 * live-transcoded video streams — callers fall back to the probed duration,
 * and "—" keeps Infinity:NaN off the screen in the meantime. */
export function fmtDuration(sec: number | undefined): string {
  if (sec === undefined || !Number.isFinite(sec) || sec < 0) return "—";
  const s = Math.floor(sec);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  return h
    ? `${h}:${String(m).padStart(2, "0")}:${String(ss).padStart(2, "0")}`
    : `${m}:${String(ss).padStart(2, "0")}`;
}
