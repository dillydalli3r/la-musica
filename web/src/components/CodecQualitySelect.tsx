import type { ExportCodecSpec } from "../api";

/** The dropdown's synthetic entry for a codec's "custom value" field; the
 * backend takes the plain number the field holds (kbps, or 0-10 for Vorbis),
 * so nothing but the form needs to know the word. */
export const CUSTOM = "custom";

/** Effective kbps for the drive-fit estimate, from the server's own preset
 * hints (server/exporter.py CODECS). null = unpredictable: a bit-exact copy,
 * a lossless re-encode, or a custom Vorbis q. */
export function effectiveKbps(spec: ExportCodecSpec | undefined, quality: string, custom: string): number | null {
  if (!spec) return null;
  const preset = spec.presets.find((p) => p.v === quality);
  if (preset) return preset.kbps;
  if (spec.custom && spec.custom.mode === "kbps") {
    const n = parseInt(quality === CUSTOM ? custom : quality, 10);
    if (!Number.isFinite(n)) return null;
    return Math.min(spec.custom.max, Math.max(spec.custom.min, n));
  }
  return null;
}

/** The codec and quality controls of an export — the codec dropdown, its
 * quality presets and, for the codecs that take one, a custom-value field.
 *
 * ONE home for the pair: the Export page and the per-track export panel both
 * render THIS off the same server table (`GET /api/export/codecs` →
 * `server.exporter.CODECS`, preset keys and labels included), so the two
 * cannot offer a codec or a quality the other does not, and both send the same
 * `codec`/`quality` vocabulary the run itself uses. The owner of the state is
 * the caller: it keeps the form, this only draws it.
 *
 * `stacked` lays the two selects out in one column (the narrow per-track
 * popover) instead of the Export page's two-column grid. */
export default function CodecQualitySelect({
  specs, codec, quality, customValue, onCodec, onQuality, onCustomValue, stacked = false,
}: {
  /** The server's codec table; undefined until the query lands. */
  specs: Record<string, ExportCodecSpec> | undefined;
  codec: string;
  quality: string;
  customValue: string;
  /** A codec was picked. The caller writes the quality reset too, in ONE
   *  update: a codec and the quality it invalidates have to land together. */
  onCodec: (codec: string) => void;
  onQuality: (quality: string) => void;
  onCustomValue: (value: string) => void;
  stacked?: boolean;
}) {
  const spec = specs?.[codec];
  const kbps = effectiveKbps(spec, quality, customValue);
  return (
    <>
      <div className={stacked ? "space-y-2" : "grid grid-cols-1 sm:grid-cols-2 gap-2"}>
        <label className="text-[10px] text-zinc-500 flex flex-col gap-1">
          Codec
          <select
            className="input !py-1 text-xs min-w-0 tap"
            value={codec}
            onChange={(ev) => onCodec(ev.target.value)}
          >
            {!specs && <option value={codec}>Loading codecs…</option>}
            {Object.entries(specs ?? {}).map(([v, cs]) => (
              <option key={v} value={v}>{cs.label}</option>
            ))}
          </select>
        </label>
        <label className="text-[10px] text-zinc-500 flex flex-col gap-1">
          Quality
          <select
            className="input !py-1 text-xs min-w-0 tap"
            value={quality || spec?.default || ""}
            onChange={(ev) => onQuality(ev.target.value)}
            /* Disabled for a codec with no knobs (copy) AND while the codec
               table is still on its way: an enabled box with nothing in it
               reads as a control that does not work. */
            disabled={!spec || (!spec.presets.length && !spec.custom)}
          >
            {/* copy has no knobs — a disabled placeholder keeps the box legible */}
            {!spec?.presets.length && !spec?.custom ? (
              <option value="">—</option>
            ) : (
              <>
                {spec.presets.map((q) => (
                  <option key={q.v} value={q.v}>{q.label}</option>
                ))}
                {spec.custom && (
                  <option value={CUSTOM}>
                    {spec.custom.mode === "q" ? "Custom q…" : "Custom bitrate…"}
                  </option>
                )}
              </>
            )}
          </select>
        </label>
      </div>
      {/* custom bitrate / q — the server clamps to the same range again */}
      {spec?.custom && quality === CUSTOM && (
        <label className="flex flex-wrap items-center gap-2 mt-2 text-[10px] text-zinc-500">
          {spec.custom.mode === "q"
            ? `Custom q (${spec.custom.min}–${spec.custom.max})`
            : `Custom bitrate (${spec.custom.min}–${spec.custom.max} kbps)`}
          <input
            className="input !py-1 text-xs w-24 min-w-0 tap"
            type="number"
            min={spec.custom.min}
            max={spec.custom.max}
            value={customValue}
            onChange={(ev) => onCustomValue(ev.target.value)}
          />
          {kbps !== null && <span className="text-zinc-600">~{kbps} kbps effective</span>}
        </label>
      )}
    </>
  );
}