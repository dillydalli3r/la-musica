/** The library codec targets (config `library_codec`) in ONE declarative list.
 *
 *  Settings and the setup wizard render the same controls from here, and
 *  `mlo/containers.py`'s CODECS table is the Python half of the same list —
 *  the encoder arguments, the extension and the lossless/lossy answer all come
 *  from that one, so a value added on either side has to be added on the other
 *  (`tools/test_codec_policy.py` asserts the Python half).
 *
 *  `CODEC_CHOICES` is the plain (value, label) list a select takes; the labels
 *  are exactly the strings `configMeta.ts` and `SettingsPage.tsx` render, so
 *  importing it changes nothing on screen. Use `CODEC_META` when the form also
 *  has to say WHICH field the codec's rate/quality setting feeds and what that
 *  codec's own default is — both settings exist for every target, and only one
 *  of them applies to any given codec. */

export type CodecChoice = {
  /** The `library_codec` value. */
  value: string;
  /** Select label (also the label in `CODEC_CHOICES`). */
  label: string;
  /** False when encoding to this codec throws samples away. */
  lossy: boolean;
  /** The extension a converted file gets. */
  ext: string;
  /**
   * Which encoder setting this codec reads:
   *   "bitrate" — `library_codec_bitrate`. kbps for MP3/AAC/Opus, and
   *               libvorbis' own 0-10 quality scale for Ogg.
   *   "level"   — `library_codec_quality`, the lossless compression level.
   *   null      — neither: the codec exposes no such control to the encoder,
   *               so both settings are ignored for it.
   */
  setting: "bitrate" | "level" | null;
  /** The value that setting resolves to when the user leaves it at 0/empty —
   *  i.e. the codec's own default, or the shipped `library_codec_quality`. */
  defaultRate: number | null;
  /** One line of help: what choosing this codec means. */
  help: string;
};

export const CODEC_META: CodecChoice[] = [
  {
    value: "flac",
    label: "FLAC (.flac) — lossless",
    lossy: false,
    ext: ".flac",
    setting: "level",
    defaultRate: 5,
    help: "The shipped default: losslessly compressed and what the verification tools (metaflac, flac -t, the ENCODER tags) are built around.",
  },
  {
    value: "alac",
    label: "ALAC (.m4a) — compressed lossless",
    lossy: false,
    ext: ".m4a",
    setting: null,
    defaultRate: null,
    help: "Lossless in an Apple container: the same audio as FLAC, for libraries that live in iTunes/Music. Tags are MP4 atoms.",
  },
  {
    value: "wav",
    label: "WAV (.wav) — uncompressed",
    lossy: false,
    ext: ".wav",
    setting: null,
    defaultRate: null,
    help: "Uncompressed PCM: no quality is lost but a library is roughly three times the size of FLAC and carries ID3 tags only.",
  },
  {
    value: "aiff",
    label: "AIFF (.aiff) — uncompressed",
    lossy: false,
    ext: ".aiff",
    setting: null,
    defaultRate: null,
    help: "Uncompressed PCM in Apple's container (big-endian), the same trade as WAV.",
  },
  {
    value: "mp3",
    label: "MP3 (.mp3) — lossy, CBR",
    lossy: true,
    ext: ".mp3",
    setting: "bitrate",
    defaultRate: 320,
    help: "Lossy and universally playable. The bitrate setting is kbps; 320 is transparent for most material, 128-192 much smaller.",
  },
  {
    value: "aac",
    label: "AAC (.m4a) — lossy, CBR",
    lossy: true,
    ext: ".m4a",
    setting: "bitrate",
    defaultRate: 256,
    help: "Lossy, in an MP4 container: better quality than MP3 at the same bitrate (kbps). Note .m4a also carries ALAC — the codec, not the extension, is what the pass compares.",
  },
  {
    value: "ogg",
    label: "Ogg Vorbis (.ogg) — lossy",
    lossy: true,
    ext: ".ogg",
    setting: "bitrate",
    defaultRate: 6,
    help: "Lossy, open and unencumbered. The setting is libvorbis' own 0-10 quality scale (6 ≈ 192 kbps), NOT kbps.",
  },
  {
    value: "opus",
    label: "Opus (.opus) — lossy",
    lossy: true,
    ext: ".opus",
    setting: "bitrate",
    defaultRate: 128,
    help: "The best quality per kbps of the lossy targets, always resampled to 48 kHz. 96-128 kbps is transparent for most material.",
  },
  {
    value: "keep",
    label: "Keep — never convert",
    lossy: false,
    ext: "",
    setting: null,
    defaultRate: null,
    help: "No target at all: the pass converts nothing and only the existing format/naming work runs. Script 3 still re-compresses FLACs, which stays in the same codec.",
  },
];

/** `[value, label]` for a select — the codec list in one place. */
export const CODEC_CHOICES: [string, string][] =
  CODEC_META.map((c) => [c.value, c.label]);
