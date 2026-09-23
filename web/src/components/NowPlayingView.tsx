import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AudioLines, Captions, ChevronDown, Heart, Info, ListMusic, ListPlus, Mic2, Pause, Play, Repeat, Settings2, Shuffle,
  SkipBack, SkipForward, Volume1, Volume2, VolumeX, X,
} from "lucide-react";
import { api } from "../api";
import VolumePct from "./VolumePct";
import LyricZoom from "./LyricZoom";
import LyricOffset from "./LyricOffset";
import { DetailsDialog } from "./AlbumDetails";
import { toast, useStore } from "../store";
import { fmtTech, fmtPair, isVideoFile } from "../lib/fmt";
import { AdvisoryMark } from "./Badges";
import StarRating from "./StarRating";
import { ratingOf, useRatings, useSetRating } from "../lib/ratings";
import CoverImg from "./CoverImg";
import Popover, { MenuItem } from "./Popover";
import ScrubSeek from "./ScrubSeek";
import { MAX_DB, MIN_DB, activeAnalyser } from "../lib/analyser";
import Visualizer from "./Visualizer";
import { parsePlayerLrc, parseLrc, splitStoredLines, hasLyricsText, activeLineRange, KaraokeWords, type LrcLine } from "./LyricsViewer";
import type { Playlist } from "../types";
import { useLyricsFollow, LYRICS_PAD_BOTTOM, LYRICS_PAD_TOP } from "../lib/lyrScroll";
import { nextSpeed, fmtSpeed } from "../lib/playback";
import { fmtDuration } from "../lib/fmt";
import useSubtitleTracks from "./SubtitledVideo";
import type { VideoAspect } from "../lib/video";

const XLIT_KEY = "mlo.np.xlit";
const TRANS_KEY = "mlo.np.trans";
const SIZE_KEY = "mlo.np.size"; // sm | md | lg
const KARAOKE_KEY = "mlo.np.karaoke"; // "1" = word-level karaoke, "0" = line highlight (default)
const ORBS_KEY = "mlo.np.orbs"; // "1" = animated background
const VIS_KEY = "mlo.np.vis"; // "1" = background pulses with the beat
const VIZ_KEY = "mlo.np.viz"; // "1" = frequency-bar visualizer visible
// "1" = the lyrics pane is shown (default). The pane is a control the reader
// owns, not a side effect of the track carrying lyrics: it used to appear
// whenever lyrics existed and could not be dismissed, so a track whose lyrics
// you did not want took half the screen with no way back. Persisted like the
// other display picks, and the pane stays MOUNTED while hidden — that is what
// keeps its scroll position and zoom across a toggle instead of rebuilding it
// (and re-finding the sung line) on the next open.
const LYRICS_KEY = "mlo.np.lyrics";
const ZOOM_KEY = "mlo.np.lyrzoom.v2"; // lyrics zoom multiplier (persisted)
// 150 % is the new 100 %: the multiplier 1.5 (the shipped default) is what the
// box now calls 100 %, because that is the size the pane was always read at.
const LYRIC_ZOOM_BASE = 1.5;

/** The ambience window, in dB, measured RELATIVE to this track's own rolling
 * loud reference — a fixed window cannot work across masters. On a real
 * track the bass-weighted mix below swings inside ~4 dB, so the old fixed
 * −82..−57 window (25 dB wide) moved --amb by about 0.15 and the whole
 * backdrop barely breathed. The reference follows the loudest mix level
 * heard recently (~0.9 dB/s fall), and AMB_DYN_DB is the span beneath it
 * that maps to closed → open: quiet passages close the glow, the loud ones
 * open it, whatever the master's own level happens to be.
 * See the ambience tick for why the level is taken in dB, not raw bytes. */
const AMB_DYN_DB = 8;
const AMB_REF_FALL_DB = 0.07; // per tick (~0.9 dB/s at AMB_TICK_MS)
const AMB_REF_START_DB = -60;

/** How often the ambience reads the analyser and updates --amb, in ms. This
 * has to keep up with `.amb-glow`'s own transitions in index.css: with the
 * old 2.6/4 s easing a 120 ms tick was more than enough, but the two low-
 * passes stacked into a layer that barely moved, so the CSS now eases in
 * under a second and the tick follows it. Still a stepped write, never a
 * per-frame paint — the smoothing below is what keeps a kick off the screen. */
const AMB_TICK_MS = 70;

/** How far --amb has to move before it is written again. The eased value
 *  sits within a hair of the last write through a steady or silent passage,
 *  and re-writing the same number 14x/s restarts a transition on a
 *  full-viewport layer for nothing. Below this the glow simply stays where
 *  the last write put it. */
const AMB_WRITE_EPS = 0.002;

/** How the player applies ReplayGain — mirrors the `replaygain_mode` config
 * options (mlo/config.py). */
type RgMode = "track" | "album" | "off";

interface Props {
  current: { path: string; file: string; albumPath: string; artist?: string; album?: string; title?: string; coverFile?: string | null; albumCover?: string | null; advisory?: string | null };
  queuePos: string;
  playing: boolean;
  time: number;
  duration: number;
  shuffle: boolean;
  loop: boolean;
  liked: boolean;
  onTogglePlay: () => void;
  onSeek: (t: number) => void;
  onStep: (d: 1 | -1) => void;
  onToggleShuffle: () => void;
  onToggleLoop: () => void;
  onToggleLike: () => void;
  onClose: () => void;
  getAudioTime?: () => number;
  /** Shared with the player bar — the bar owns the decoders, the fullscreen
   * view mirrors and edits the rate through these. */
  speed: number;
  onSpeedChange: (s: number) => void;
  /** Music-video presentation — the bar owns the single <video>, so the
   *  fullscreen pickers drive it through these. */
  video: {
    aspect: VideoAspect;
    /** null = as tagged (the default track), -1 = off, else that track index. */
    captions: number | null;
    onAspect: (a: VideoAspect) => void;
    onCaptions: (i: number | null) => void;
  };
  /** ReplayGain is stored in the config and applied by the player bar (it owns
   * the decoders); the fullscreen options menu is where it gets edited. */
  rg: {
    mode: RgMode;
    preamp: number;
    /** What the bar is applying to this track — null at unity. `album` is
     *  false while the mode is "album" when the album tag was missing, so the
     *  line can name the per-track fallback instead of implying album gain. */
    applied: { gain: number; source: string | null; analyzed: boolean; album: boolean } | null;
    onMode: (m: RgMode) => void;
    onPreamp: (db: number) => void;
  };
}

function hexToRgbTriplet(hex?: string | null): [number, number, number] | null {
  if (!hex) return null;
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return null;
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

/** The fullscreen player's ink — TWO tables, ONE choice, and the scrim that
 *  makes the choice true.
 *
 *  There used to be one table (light, pinned) and a full-bleed GREY wash under
 *  it, which is what kept white legible on a white cover. Both are gone: a grey
 *  layer between the artwork's ambience and its text is exactly the "added
 *  stuff behind the text" the owner reported, and pinning one ink is what made
 *  it necessary. The ink is now derived from the cover — the ask, verbatim:
 *  a text colour that answers to the background's own brightness.
 *
 *  What decides it (`npInk`), in one place:
 *
 *  * the cover's AVERAGE colour is what the whole ambience is built from (the
 *    server's `tagcache.cover_color` is that average, and every layer in the
 *    markup below paints `rgb` of it). Its relative luminance is therefore the
 *    one number that predicts the field;
 *  * below `NP_INK_FLIP` the ambience is a dark field: the white table is what
 *    reads on it, and NO SCRIM IS DRAWN AT ALL — the background is the cover's
 *    own colour, nothing added;
 *  * above the flip the field is bright (a white cover's bloom cores go to
 *    white), so the table flips to near-black and the field is lifted by a
 *    LIGHT scrim built from the cover's own colour — `rgb` mixed toward white,
 *    never grey — with a strength that grows with the cover's brightness. That
 *    is the mirror of the old wash and it exists for the same reason: one ink
 *    cannot be AA on a field that spans rgb(96) to rgb(255) within one screen,
 *    so the field is bounded instead of the ink being guessed per pixel.
 *
 *  Every lyric surface reads its colour from the chosen table, so the polarity
 *  is decided once instead of separately for the active line, the faded ones,
 *  the karaoke syllables and the metadata block. The steps are the zinc
 *  ladder's near-white and near-black rungs — a saturated cover gets white or
 *  near-black ink, never a tint of its own colour — and the glyph SHADOW flips
 *  with them: a dark halo under light ink, a light halo under dark ink, so the
 *  edge of a glyph always separates from the busier mid-tones the ambience
 *  drifts through. `dim` sits high on either ladder because an inactive line is
 *  ALSO drawn at 80 % opacity behind a 1px blur (LINE_BLUR), which pulls it
 *  back toward the backdrop.
 *
 *  The numbers — measured field patches per cover, per tier — are in
 *  tools/check_np_metadata_contrast.cjs, which renders the real component. */
interface LyricInk {
  /** The line being sung — full-strength ink. */
  active: string;
  /** A synced line that is not the current one: the same ink faded toward
   *  the backdrop, so the active line still reads as the one playing. */
  dim: string;
  /** Unsynced lyrics have no active line to stand out against, so every
   *  line keeps full ink instead of two tones of grey. */
  plain: string;
  /** The glyph shadow. text-shadow inherits, so the pane sets it once for
   *  the whole reading surface: a tight near-opaque core (the glyph's own
   *  edge) plus a wide soft halo (the surround a line sits in). Its POLARITY
   *  is the ink's: dark under light glyphs, light under dark ones. */
  shade: string;
  /** Karaoke syllables: under the playhead, already sung, still to come. The
   *  emphasis is the scale + glow; the colour follows the ink, because the
   *  default theme's `--accent` IS white and would put one white word back
   *  on the line it is meant to stand out from. */
  wordNow: string;
  wordSung: string;
  wordNext: string;
  /** The transport and top-bar CHROME: the icon buttons, the time readouts and
   *  the small labels. Their muted grey has to follow the ink's polarity too —
   *  a zinc-500 glyph on a white cover is the same grey-on-grey failure the
   *  lyrics had before the flip, and it is why these three live in the table
   *  instead of being spelled out at each button. */
  chromeStrong: string;
  chromeButton: string;
  chromeText: string;
  /** The full-bleed field lift this table needs, or "" for none. Built from
   *  the cover's own colour (`rgb`) rather than a grey, and drawn with no
   *  edge, rounding or blur: a scrim, never a panel. */
  scrim: string;
}

const INK_ON_DARK: LyricInk = {
  active: "text-white",
  dim: "text-zinc-200",
  plain: "text-zinc-100",
  shade: "np-shade",
  wordNow: "text-accent scale-110 [text-shadow:0_0_16px_rgba(255,255,255,0.4)]",
  wordSung: "text-white",
  wordNext: "text-white/75",
  chromeStrong: "text-white",
  chromeButton: "text-zinc-300 hover:text-white hover:bg-white/10",
  chromeText: "text-zinc-300",
  scrim: "",
};

const INK_ON_LIGHT: LyricInk = {
  active: "text-zinc-950",
  dim: "text-zinc-800",
  plain: "text-zinc-900",
  shade: "np-shade-light",
  wordNow: "text-zinc-950 scale-110 [text-shadow:0_0_16px_rgba(0,0,0,0.35)]",
  wordSung: "text-zinc-950",
  wordNext: "text-zinc-950/75",
  chromeStrong: "text-zinc-950",
  chromeButton: "text-zinc-950/75 hover:text-zinc-950 hover:bg-black/5",
  chromeText: "text-zinc-950/75",
  scrim: "",
};

/** Relative luminance (WCAG) of one sRGB colour. */
export function npLuminance(rgb: [number, number, number]): number {
  const lin = (v: number) => {
    const s = Math.max(0, Math.min(255, v)) / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(rgb[0]) + 0.7152 * lin(rgb[1]) + 0.0722 * lin(rgb[2]);
}

/** Where the two tables swap: the cover's own average luminance, in relative
 *  terms. Chosen from the measured fields (`check_np_metadata_contrast.cjs`),
 *  not from taste — a cover at or below this leaves the ambience dark enough
 *  that white ink clears AA on EVERY patch the text sits on (so nothing is
 *  drawn behind it), and a brighter one pushes the bloom cores past it. */
export const NP_INK_FLIP = 0.42;

/** The luminance at or below which the field needs NO wash at all: white ink on
 *  a flat field clears AA on its own down here (1.05 / (L + 0.05) >= 4.5 is
 *  L <= 0.18), and every cover below it measured holding AA on every patch the
 *  text covers. */
export const NP_FIELD_AS_IS = 0.20;

/** The ink and the field lift ONE cover asks for.
 *
 *  `scrim` is empty for a dark cover — the ask is that nothing at all sits
 *  behind the text there — and a cover-tinted lift above the flip, strong
 *  enough to hold the DARK table's contrast on the dimmest patch the text
 *  covers (the metadata block at the bottom, where the vignette bites) while
 *  leaving the bright cores untouched. */
export function npInk(rgb: [number, number, number]): LyricInk & { scrim: string } {
  const lum = npLuminance(rgb);
  if (lum <= NP_INK_FLIP) {
    if (lum <= NP_FIELD_AS_IS) return INK_ON_DARK;
    // The white table, and a field drop under it. A cover in this band has a
    // dark AVERAGE while the ambience painted from it is not: the orbs and the
    // bloom are screen-blended, so they ADD light on top of that average, and
    // the patch the lyrics and the metadata block sit on measured roughly
    // twice the cover's own luminance. The Bends cover (rgb 168 126 104, L
    // 0.24) put the field at ~0.45 — white ink read about 2:1 there, which is
    // the "text mixes into the background" report — while a cover just under
    // the ink flip measured the same failure at 3.6:1 on a flat field.
    //
    // So the field is dropped instead of the ink being flipped: a wash built
    // from the cover's OWN colour mixed toward near-black (never a grey),
    // full-bleed, no edge, rounding or blur — a scrim, not a panel. The
    // strength is one number because the band is narrow and the composition
    // that lifts the field is the same on every cover; it is measured, per
    // cover, in tools/check_np_metadata_contrast.cjs.
    const dark = rgb.map((v) => Math.round(v * 0.30));
    const strength = 0.62;
    return {
      ...INK_ON_DARK,
      scrim: `linear-gradient(to bottom, rgb(${dark.join(" ")} / ${strength.toFixed(3)}), `
        + `rgb(${dark.join(" ")} / ${strength.toFixed(3)}))`,
    };
  }
  // The strength has a FLOOR, and the floor is the point: what the dark table
  // has to clear is the DIMMEST patch the text covers — the metadata block at
  // the bottom, where the vignette bites — and that patch is dark no matter how
  // bright the cover's AVERAGE is (a #b4b4b4 cover put it at rgb(80): 2.5:1 for
  // near-black ink, measured). A curve that starts at zero at the flip left
  // exactly that band unreadable, so the lift starts where the ink's floor is
  // met (0.46, which takes rgb(80) past rgb(150) — 0.38 was measured at
  // rgb(132), where the dark table's muted zinc-800 tier sat at 4.00:1, just
  // short of its 4.5 floor) and grows to 0.62 on a pure-white cover. Numbers
  // from tools/check_np_metadata_contrast.cjs, which covers dark, mid-grey,
  // bright-grey and white covers.
  const strength = Math.min(0.62, Math.max(0.46, (lum - NP_INK_FLIP) * 1.05));
  const lift = rgb.map((v) => Math.round(v + (255 - v) * 0.72));
  return {
    ...INK_ON_LIGHT,
    scrim: `linear-gradient(to bottom, rgb(${lift.join(" ")} / ${(strength * 0.85).toFixed(3)}), `
      + `rgb(${lift.join(" ")} / ${strength.toFixed(3)}) 52%, `
      + `rgb(${lift.join(" ")} / ${Math.min(0.75, strength * 1.08).toFixed(3)}))`,
  };
}

// Size steps are deliberately close together: the active line reads slightly
// larger than the rest, and the translation/transliteration sub-lines sit
// just under the main line instead of shrinking into fine print.
const LYRIC_SIZES = {
  sm: { line: "text-[15px]", active: "text-[17px]", word: "text-[15px]", xlit: "text-[13px]" },
  md: { line: "text-xl", active: "text-[1.5rem]", word: "text-xl", xlit: "text-[17px]" },
  lg: { line: "text-2xl", active: "text-[2.1rem]", word: "text-2xl", xlit: "text-xl" },
} as const;

/** Ease for the active line's growth — slow out, no snap. */
const LINE_EASE = "transition-[transform,color] duration-motion-slow ease-motion";

/** How much smaller an inactive line renders next to the active one. The
 * layout is ALWAYS the active size — inactive lines shrink via transform
 * scale, which is GPU-composited and never re-wraps text. Animating
 * font-size instead re-flows and re-wraps the line every frame (the janky,
 * jumpy growth this replaced). */
const INACTIVE_SCALE = { sm: 0.88, md: 0.84, lg: 0.8 } as const;

/** Non-current synced lines read greyed-out (a slight blur + dim grey);
 * hover or keyboard focus reveals full detail. Plain-text lyrics are never
 * styled — only synced lines get the active/inactive treatment. The dim is
 * deliberately mild (80 %, 1px): over the light additive ambience a 2px blur
 * at 60 % made the line genuinely unreadable on a white cover. */
const LINE_BLUR = "np-line-blur blur-[1px] opacity-90 hover:blur-none hover:opacity-100 focus-within:blur-none focus-within:opacity-100 transition-[opacity,filter] duration-motion-base ease-motion";

/** The volume cluster is its own component because dragging the slider writes
 *  `vol` once per pointer step. Subscribed here, where the value is actually
 *  rendered, a drag repaints three small controls instead of the whole pane —
 *  the fullscreen view used to re-render its entire lyrics subtree once per
 *  frame of a volume drag. */
function VolumeControl() {
  const vol = useStore((s) => s.vol);
  const setVol = useStore((s) => s.setVol);
  const VolIcon = vol <= 0 ? VolumeX : vol < 0.5 ? Volume1 : Volume2;
  return (
    <div className="hidden md:flex items-center gap-1.5 shrink-0" title={`Volume — ${Math.round(vol * 100)}%`}>
      <VolIcon className="h-4 w-4" />
      <input
        type="range"
        min={0}
        max={1}
        step={0.05}
        value={vol}
        onChange={(e) => setVol(Number(e.target.value))}
        className="w-24 max-w-full seek-fat"
        title="Volume"
        aria-label="Volume"
      />
      <VolumePct value={vol} onChange={setVol} />
    </div>
  );
}

export default function NowPlayingView(p: Props) {
  // Subscribed field by field, never as a selector-less `useStore()`: this is
  // the fullscreen pane, and the store is written many times a second while
  // playing (a relay progress frame among them). One wholesale subscription
  // re-rendered the whole lyrics subtree on each of those writes. `vol` is not
  // read here at all — the volume cluster subscribes for itself, so a drag
  // frame never reaches this component (see VolumeControl).
  // Stored transliteration + translation default ON — they arrive with the
  // track's tags (or a .romaji.lrc / .<lang>.lrc sidecar) and render as
  // sub-lines under each line; nothing is generated on the fly.
  const [showXlit, setShowXlit] = useState(() => localStorage.getItem(XLIT_KEY) !== "0");
  const [showTrans, setShowTrans] = useState(() => localStorage.getItem(TRANS_KEY) !== "0");
  const [lyricSize, setLyricSize] = useState<keyof typeof LYRIC_SIZES>(
    () => (localStorage.getItem(SIZE_KEY) as keyof typeof LYRIC_SIZES) || "md"
  );
  // Extra zoom multiplier on top of the size preset, persisted — the default
  // is 1.5× (the user's preferred reading size).
  const [lyricZoom, setLyricZoom] = useState<number>(
    () => Number(localStorage.getItem(ZOOM_KEY)) || 1.5
  );
  const [karaoke, setKaraoke] = useState(() => localStorage.getItem(KARAOKE_KEY) === "1");
  // The lyric offset the reader is dialling in, in ms (LyricOffset): a
  // PREVIEW — the parsed line times below move with it, and Save writes the
  // shift into the track's own lyrics. Not persisted per device like the size
  // and zoom beside it, because it is not a display preference: it ends up in
  // the FILE, and a value restored on the next visit would silently re-shift
  // lyrics that were already corrected.
  const [offsetMs, setOffsetMs] = useState(0);
  // Track details & credits for whatever is playing (the options menu's entry;
  // the player bar carries the same one as an ⓘ button).
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [orbs, setOrbs] = useState(() => localStorage.getItem(ORBS_KEY) !== "0");
  const [vis, setVis] = useState(() => localStorage.getItem(VIS_KEY) !== "0");
  // Frequency-bar visualizer (fullscreen + sidebar), default on.
  const [viz, setViz] = useState(() => localStorage.getItem(VIZ_KEY) !== "0");
  // Lyrics pane, default on; the toggle sits beside the visualizer's in the
  // top bar (LYRICS_KEY carries the why).
  const [showLyrics, setShowLyrics] = useState(() => localStorage.getItem(LYRICS_KEY) !== "0");
  // ReplayGain preamp: the slider drags locally and commits to the config on a
  // short debounce, so one drag is one config write (and one re-fetch of the
  // track's gain), not one per 0.5 dB step. The commit goes through a ref —
  // the player bar re-renders several times a second, which would otherwise
  // re-arm the timer from a stale closure forever.
  const [preampDraft, setPreampDraft] = useState(p.rg.preamp);
  const rgCommit = useRef(p.rg.onPreamp);
  useEffect(() => {
    rgCommit.current = p.rg.onPreamp;
  });
  useEffect(() => setPreampDraft(p.rg.preamp), [p.rg.preamp]);
  useEffect(() => {
    if (preampDraft === p.rg.preamp) return;
    const t = setTimeout(() => rgCommit.current(preampDraft), 400);
    return () => clearTimeout(t);
  }, [preampDraft, p.rg.preamp]);
  const [options, setOptions] = useState(false);
  const [queueOpen, setQueueOpen] = useState(false);
  const [plOpen, setPlOpen] = useState(false);
  const [newPlName, setNewPlName] = useState("");
  const [tags, setTags] = useState<Record<string, string> | null>(null);
  const [tech, setTech] = useState<{ bitrate?: number; sample_rate?: number; bits_per_sample?: number; codec?: string } | null>(null);
  const [lyricsText, setLyricsText] = useState<string | null>(null);
  const [lyricsFor, setLyricsFor] = useState<string | null>(null);
  // Which track `tags`/`tech` belong to. They are deliberately NOT cleared on
  // track change: blanking them collapsed the tech line under the title and
  // everything below jumped vertically on next/previous (the "title shake").
  // Stale values render until the fresh payload lands; freshness is gated
  // where wrong info would matter (title fallback, instrumental, BPM).
  const [tagsFor, setTagsFor] = useState<string | null>(null);
  const [transforms, setTransforms] = useState<Record<string, string[]>>({});
  // The PRIMARY text line of each block — with translations/romanization the
  // outer block also carries sub-lines, and centering the block would push
  // the sung line off the middle. The scroller centers this element.
  const primaryRefs = useRef<Record<number, HTMLDivElement | null>>({});
  const lyricsScrollRef = useRef<HTMLDivElement>(null);
  const qc = useQueryClient();

  const { time, duration } = p;
  // Same per-field treatment: the drawer renders `queue`/`index`, and the
  // action selectors are stable references — so this pane repaints on a queue
  // or track change, not on every unrelated store write.
  const queue = useStore((s) => s.queue);
  const index = useStore((s) => s.index);
  const setQueue = useStore((s) => s.setQueue);
  const queueRemoveAt = useStore((s) => s.queueRemoveAt);
  const queueMove = useStore((s) => s.queueMove);
  const queueListRef = useRef<HTMLDivElement>(null);
  const queueTriggerRef = useRef<HTMLButtonElement>(null);
  const queueCloseRef = useRef<HTMLButtonElement>(null);
  // drag-reorder state for the queue drawer (absolute queue indexes)
  const [dragIdx, setDragIdx] = useState<number | null>(null);
  const [overIdx, setOverIdx] = useState<number | null>(null);

  // Keep the playing row visible when the queue drawer opens.
  useEffect(() => {
    if (!queueOpen) return;
    queueListRef.current
      ?.querySelector(`[data-queue-index="${index}"]`)
      ?.scrollIntoView({ block: "center" });
  }, [queueOpen, index]);

  // Keyboard-operable drawer: Esc closes, focus moves in on open and
  // returns to the trigger on close. Separate from the scroll above so a
  // track change while open doesn't yank focus back to the close button.
  useEffect(() => {
    if (!queueOpen) return;
    queueCloseRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setQueueOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      queueTriggerRef.current?.focus();
    };
  }, [queueOpen]);

  // ~60 fps lyric clock: while playing, rAF reads the shared <audio> element
  // directly so syllable highlighting isn't stepped at timeupdate's ~4 Hz.
  // A backgrounded pane PAUSES rAF, so freshness is tracked — when ticks
  // stop arriving the clock falls back to the event-driven `time` prop
  // (which always advances), keeping auto-scroll alive for every sync type
  // even while the pane is throttled.
  const [smoothTime, setSmoothTime] = useState(0);
  const smoothTickRef = useRef(0);
  // True while a word / syllable sweep is on screen — the only consumer that
  // needs 60 fps. Line changes just have to flip on the beat, so everything
  // else ticks at 20 Hz and skips ~2/3 of the full-view re-renders the pane
  // has to compete with.
  const sweepRef = useRef(false);
  useEffect(() => {
    if (!p.playing) return;
    let raf = 0;
    const tick = () => {
      const t = p.getAudioTime?.();
      if (typeof t === "number" && isFinite(t) && t >= 0) {
        setSmoothTime(sweepRef.current ? t : Math.round(t * 20) / 20);
        smoothTickRef.current = performance.now();
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [p.playing, p.getAudioTime]);
  const smoothFresh = performance.now() - smoothTickRef.current < 250;
  const dispTime = p.playing && smoothTime > 0 && smoothFresh ? smoothTime : time;

  // cover file + dominant color for the ambient background — the album
  // payload also supplies the canonical album artist / album name shown
  // under the cover.
  const { data: album } = useQuery({
    queryKey: ["album", p.current.albumPath],
    queryFn: () => api.album(p.current.albumPath),
    staleTime: 5 * 60 * 1000,
  });
  // Per-track sidecar art wins; the album cover is the fallback — the
  // now-playing art must match the track, not just the album.
  const coverFile = p.current.coverFile ?? album?.cover_file ?? p.current.albumCover ?? null;
  const { data: colorData } = useQuery({
    queryKey: ["coverColor", p.current.albumPath],
    queryFn: () => api.coverColor(p.current.albumPath),
    retry: false,
    staleTime: 10 * 60 * 1000,
  });
  const coverHex = colorData?.color ?? null;
  const rgb = useMemo<[number, number, number]>(
    () => hexToRgbTriplet(coverHex) ?? [113, 113, 122],
    [coverHex]
  );
  // The ONE polarity decision for the whole player, off the cover's own average
  // colour — the value every ambience layer below is painted from. A dark cover
  // takes the white table and draws NOTHING behind the text; a bright one flips
  // to near-black ink and lifts the field with a scrim built from the cover's
  // own colour (`npInk` carries both tables and the rule).
  const ink = useMemo(() => npInk(rgb), [rgb]);

  // ---- background ambience (Apple Music-style, layered) --------------------
  // One value comes from the audio — the shared WebAudio analyser's bass-
  // weighted level, smoothed — and it is applied as a breathing swell to ONE
  // glow layer. Everything else (cover breathing, aurora sweep, drifting color
  // fields) is CSS animation on its own long clock. The level is written as a
  // stepped custom property, not painted per frame: an instant attack on this
  // layer is what read as strobing, because a kick could brighten one frame and
  // the next took it back. The layers themselves are described at the markup
  // below.
  const ambRef = useRef<HTMLDivElement>(null);
  const eased = useRef({ energy: 0 });
  // Read through a ref, exactly like the bars do: the ambience loop already
  // handles silence internally (energy eases to 0), so `p.playing` must not be
  // an effect dependency. It was, and the teardown/rebuild on every pause
  // reset the phase to 0 — the whole background snapped to another scale in a
  // single frame on every pause and resume.
  const playingRef = useRef(p.playing);
  useEffect(() => {
    playingRef.current = p.playing;
  }, [p.playing]);
  useEffect(() => {
    const el = ambRef.current;
    if (!el) return;
    const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!vis || calm) {
      // One steady value: the layers keep their colors and composition, and
      // nothing moves on its own.
      el.style.setProperty("--amb", "0.45");
      return;
    }
    let timer = 0;
    let freq: Uint8Array | null = null;
    // Starts at the CSS fallback, so the first tick only writes if the
    // audio actually asks for something else.
    let written = 0.45;
    let refDb = AMB_REF_START_DB;
    const read = () => {
      let energy = 0;
      if (playingRef.current) {
        try {
          const an = activeAnalyser();
          if (an) {
            if (!freq || freq.length !== an.frequencyBinCount) {
              freq = new Uint8Array(an.frequencyBinCount);
            }
            an.getByteFrequencyData(freq as Uint8Array<ArrayBuffer>);
            let sum = 0;
            for (let i = 0; i < freq.length; i++) sum += freq[i];
            // Bytes are the analyser's dB window (lib/analyser.ts) squeezed
            // into 0..255, so the level is read back in dB — averaging raw
            // bytes saturated on any loud master.
            const meanDb = MIN_DB + (sum / freq.length / 255) * (MAX_DB - MIN_DB);
            // The LOW bands carry the beat: 60% bass, 40% overall keeps the
            // glow following the music instead of sitting flat.
            const lowBins = Math.max(1, freq.length >> 3);
            let low = 0;
            for (let i = 0; i < lowBins; i++) low += freq[i];
            const lowDb = MIN_DB + (low / lowBins / 255) * (MAX_DB - MIN_DB);
            const mixDb = 0.6 * lowDb + 0.4 * meanDb;
            // Loud reference for THIS material: rises to a new peak at once,
            // falls slowly, so the window below it keeps working on a quiet
            // master and on a loud one alike.
            refDb = Math.max(mixDb, refDb - AMB_REF_FALL_DB);
            energy = Math.min(1, Math.max(0, (mixDb - (refDb - AMB_DYN_DB)) / AMB_DYN_DB));
          }
        } catch {
          energy = 0;
        }
      }
      // Asymmetric smoothing here, CSS easing on the other side: this writes
      // ONE custom property every AMB_TICK_MS and .amb-glow eases its own
      // opacity/scale from it. The attack is ~0.2 s (three ticks) so a kick
      // lands while it is still a kick, and the release is a little over a
      // second so the glow falls back instead of flickering — the old
      // 0.12/0.03 per 120 ms stacked a ~1 s attack on top of multi-second CSS
      // transitions and flattened the whole layer.
      const prev = eased.current.energy;
      const next = prev + (energy - prev) * (energy > prev ? 0.45 : 0.1);
      eased.current.energy = next;
      if (Math.abs(next - written) > AMB_WRITE_EPS) {
        written = next;
        el.style.setProperty("--amb", next.toFixed(3));
      }
      timer = window.setTimeout(read, AMB_TICK_MS);
    };
    read();
    return () => window.clearTimeout(timer);
  }, [vis]);

  // Persisted-toggle helper shared by the options menu and inline buttons.
  const persist = (key: string, v: string) => localStorage.setItem(key, v);

  // ---- video presentation (music videos in the queue) ----------------------
  // The player bar owns the popout <video> (the only decoder) and portals it
  // to <body>, restyling it to fill the viewport while this overlay is open.
  // Nothing is moved or mirrored here: a second element would double-decode,
  // and a DOM move would put React's own node under a parent its fiber tree
  // doesn't know about (NotFoundError on the next track change). All this
  // overlay adds is the click surface and the control bar painted above it.
  const videoPath = isVideoFile(p.current.file || p.current.path) ? p.current.path : null;
  // Same query key as the <video>'s own hook, so the <track> list the picker
  // shows is the one the element actually carries — no second fetch.
  const captionTracks = useSubtitleTracks(videoPath);

  // ---- auto-hiding chrome (video mode) ------------------------------------
  // Like every serious video player: any mouse movement / key / touch shows
  // the top bar, the control overlay and the cursor, then ~2.5s of stillness
  // hides them again over the playing picture. Paused or with a popover open
  // the chrome stays put — hidden controls must never be a surprise.
  const [chromeVisible, setChromeVisible] = useState(true);
  const chromeTimer = useRef<number | null>(null);
  useEffect(() => {
    if (!videoPath) {
      setChromeVisible(true);
      return;
    }
    const arm = () => {
      if (chromeTimer.current !== null) window.clearTimeout(chromeTimer.current);
      chromeTimer.current = window.setTimeout(() => {
        if (p.playing && !queueOpen && !options && !plOpen) setChromeVisible(false);
      }, 2500);
    };
    const poke = () => {
      setChromeVisible(true);
      arm();
    };
    arm();
    const evts: (keyof WindowEventMap)[] = ["mousemove", "mousedown", "keydown", "touchstart"];
    evts.forEach((e) => window.addEventListener(e, poke, { passive: true }));
    return () => {
      evts.forEach((e) => window.removeEventListener(e, poke));
      if (chromeTimer.current !== null) window.clearTimeout(chromeTimer.current);
    };
  }, [videoPath, p.playing, queueOpen, options, plOpen]);
  // Pausing always brings the chrome back.
  useEffect(() => {
    if (!p.playing) setChromeVisible(true);
  }, [p.playing]);

  // ---- lyrics for the current track --------------------------------------
  // Stored translations / transliterations (TRANSLATION-<lang> /
  // TRANSLITERATION-<lang>-LATN tags, or .romaji.lrc / .<lang>.lrc sidecars)
  // arrive with the same payload and are parsed exactly like the original
  // lyrics, so their lines stay 1:1 with `plainLines` without re-alignment.
  // While the next track's payload loads, the PREVIOUS track's lyrics stay
  // rendered (dimmed, no highlight): resetting to empty first is what made
  // the cover jump sizes / flash to the middle on next / previous.
  useEffect(() => {
    let dead = false;
    setSmoothTime(0);
    api
      .tags(p.current.path)
      .then((t) => {
        if (dead) return;
        setTags(t.tags ?? {});
        setTech((t.tech as typeof tech) ?? null);
        setTagsFor(p.current.path);
        setLyricsText(typeof t.lyrics === "string" ? t.lyrics : null);
        setLyricsFor(p.current.path);
        const seeded: Record<string, string[]> = {};
        const main = typeof t.lyrics === "string" ? t.lyrics : null;
        const withLeader = !!main && parsePlayerLrc(main).length > parseLrc(main).length;
        if (typeof t.lyrics_xlit === "string" && t.lyrics_xlit.trim())
          seeded.transliterate = splitStoredLines(t.lyrics_xlit, withLeader);
        if (typeof t.lyrics_trans === "string" && t.lyrics_trans.trim())
          seeded.translate = splitStoredLines(t.lyrics_trans, withLeader);
        setTransforms(seeded);
      })
      .catch(() => {
        if (!dead) {
          setTags({});
          // A failed read must not leave the PREVIOUS track's lines on
          // screen un-gated: `staleLyrics` compares paths, so marking the
          // path fresh would make them look current — highlightable, and
          // clickable into a seek the new track never had.
          setLyricsText(null);
          setTransforms({});
          setTagsFor(p.current.path);
          setLyricsFor(p.current.path);
        }
      });
    return () => {
      dead = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.current.path]);

  const instrumental = (tags?.INSTRUMENTAL ?? "").toString().trim() === "1";
  // The lyrics on screen belong to `lyricsFor`; until the new track's
  // payload arrives they are stale — kept for layout stability, dimmed and
  // never highlighted.
  const staleLyrics = lyricsFor !== p.current.path;
  const tagsStale = tagsFor !== p.current.path;
  const lines: LrcLine[] = useMemo(
    () => (lyricsText && !instrumental ? parsePlayerLrc(lyricsText, offsetMs) : []),
    [lyricsText, instrumental, offsetMs]
  );
  // Layout (cover sizing, pane presence) follows the on-screen lyrics even
  // while stale so next/previous never reflows the whole view.
  const layoutHasLyrics = hasLyricsText(lyricsText) && !instrumental;
  // Whether the pane is actually on screen: the track has lyrics AND the
  // reader has not dismissed them. The track's lyrics still decide the
  // toggle's presence (below) — a button that cannot do anything is hidden,
  // not rendered inert.
  const paneOpen = layoutHasLyrics && showLyrics;
  const hasLyrics = layoutHasLyrics && !staleLyrics;
  const plainLines = useMemo(() => {
    if (!hasLyrics) return [];
    if (lines.length) return lines.map((l) => l.text);
    return (lyricsText ?? "").split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  }, [hasLyrics, lines, lyricsText]);
  // Synced lyrics carry timestamps; unsynced ones are plain text — both must
  // flow through renderLine so transliteration/translation applies to each.
  const synced = lines.length > 0;
  const displayLines: LrcLine[] = useMemo(
    () => (synced ? lines : plainLines.map((text) => ({ ts: "", time: 0, text }))),
    [synced, lines, plainLines]
  );

  // ---- active line ---------------------------------------------------------
  // A RANGE, not a single line: lines stamped at the same moment (duets,
  // backing vocals) form one cluster and highlight together. aStart is the
  // scroll target (first line of the cluster).
  const { aStart, aEnd } = useMemo(() => {
    if (staleLyrics) return { aStart: -1, aEnd: -1 }; // old lyrics vs new clock
    const [s, e] = activeLineRange(lines, dispTime);
    return { aStart: s, aEnd: e };
  }, [lines, dispTime, staleLyrics]);
  const activeLine = aStart;

  // Auto-follow owns the pane — the shared controller, so this view and the
  // sidebar can't drift apart. Nothing pauses it implicitly, not even the
  // pointer resting on the lyrics (that hover-pause read as "auto-scroll
  // stopped working" whenever the cursor was parked over the pane). A wheel /
  // touch hands the pane to the reader for a few seconds; a seek, a click on
  // a line, or pressing play takes it straight back.
  //
  // While the pane is put away it follows NOTHING: `active` goes back to the
  // controller's own "nothing sung yet" (-1), which it skips. A collapsed pane
  // has no width, so every row's offsetTop is measured against a
  // one-character-wide layout — a write from there would park the pane deep in
  // the song, and it is the reopen below that re-centres it instead.
  const { centerLine, takeOver } = useLyricsFollow({
    active: paneOpen ? activeLine : -1,
    time: dispTime,
    playing: p.playing,
    scroll: lyricsScrollRef,
    rows: primaryRefs,
    reset: p.current.path,
  });

  // A word / syllable sweep on screen is the one consumer that needs the
  // clock at full rate; line changes ride the 20 Hz tick.
  const sweeping = karaoke && !!lines[aStart]?.words?.length;
  useEffect(() => {
    sweepRef.current = sweeping;
  }, [sweeping]);

  // The viewer's close handler is recreated on every parent render, so the
  // fullscreenchange listener below reads it through a ref to stay correct.
  const closeRef = useRef(p.onClose);
  closeRef.current = p.onClose;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      // An inner surface gets first refusal: something that already consumed
      // the key (a capture field), or the add-to-playlist popover.
      if (e.defaultPrevented || document.querySelector("[data-lrc-editor]")) return;
      if (plOpen) {
        setPlOpen(false);
        return;
      }
      // ONE press leaves the player for good. Esc used to be spent stepping
      // out of the browser fullscreen, so closing the viewer took two presses.
      closeRef.current();
      if (document.fullscreenElement) document.exitFullscreen().catch(() => { /* gone */ });
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [plOpen]);

  // Some browsers swallow Esc themselves while in native fullscreen — the page
  // never sees a keydown, only the resulting fullscreenchange. Treating that
  // transition as "the user is done" is what makes a single Esc enough
  // everywhere. The listener only exists while the viewer is mounted.
  useEffect(() => {
    const onFs = () => {
      if (!document.fullscreenElement) closeRef.current();
    };
    document.addEventListener("fullscreenchange", onFs);
    return () => document.removeEventListener("fullscreenchange", onFs);
  }, []);

  const toggleOpt = (which: "xlit" | "trans") => {
    if (which === "xlit") {
      const v = !showXlit;
      setShowXlit(v);
      persist(XLIT_KEY, v ? "1" : "0");
    } else {
      const v = !showTrans;
      setShowTrans(v);
      persist(TRANS_KEY, v ? "1" : "0");
    }
  };

  // ---- add to playlist -----------------------------------------------------
  const { data: playlists } = useQuery({
    queryKey: ["playlists"],
    queryFn: api.playlists,
    enabled: plOpen,
  });
  const addToPlaylist = async (pl: Playlist) => {
    try {
      await api.playlistAdd(pl.id, [p.current.path]);
      toast.success(`Added "${title}" to ${pl.name}`);
      setPlOpen(false);
      qc.invalidateQueries({ queryKey: ["playlist", pl.id] });
    } catch (e) {
      toast.error(String(e));
    }
  };
  const createPlaylistAndAdd = async () => {
    const name = newPlName.trim();
    if (!name) return;
    try {
      const pl = await api.createPlaylist(name, "manual");
      await api.playlistAdd(pl.id, [p.current.path]);
      toast.success(`Added "${title}" to ${pl.name}`);
      setNewPlName("");
      setPlOpen(false);
      qc.invalidateQueries({ queryKey: ["playlists"] });
    } catch (e) {
      toast.error(String(e));
    }
  };

  const size = LYRIC_SIZES[lyricSize];

  const renderLine = (l: LrcLine, i: number) => {
    // Every line of the current same-time cluster (duets / backing vocals)
    // reads as active; the anchor is the cluster's first line.
    const isActive = synced && aEnd >= aStart && i >= aStart && i <= aEnd;
    // Clickable only once the rows belong to the CURRENT track: while the
    // previous track's payload is still up (staleLyrics — kept on purpose for
    // layout stability) a click would seek the new track to the OLD track's
    // timestamp, and a past-the-duration target drives it straight into
    // `ended`, advancing the queue again.
    const seekable = synced && !staleLyrics;
    const xlit = showXlit ? transforms.transliterate?.[i] : undefined;
    const trans = showTrans ? transforms.translate?.[i] : undefined;
    // When transliteration is on, the romanized text IS the primary line —
    // for Latin-script originals it equals the original, so hiding the
    // original loses nothing. Karaoke word timing only fits the original
    // wording, so it applies only when the original is displayed (and only
    // for synced lyrics, which is where word timings exist).
    const replaced = !!xlit && xlit.trim() !== "" && xlit.trim() !== l.text.trim();
    const primary = replaced ? xlit : l.text;
    // A "translation" that repeats the line it sits under (English lyrics
    // "translated" to English) adds nothing — skip those sub-lines.
    const essence = (s: string) => s.toLowerCase().replace(/[\W_]+/g, "");
    const transDup = !!trans && (
      essence(trans) === essence(l.text) ||
      (replaced && essence(trans) === essence(xlit))
    );
    return (
      <div
        key={i}
        className={`py-2 ${seekable ? "cursor-pointer" : ""} ${
          isActive ? "opacity-100" : synced ? LINE_BLUR : ""
        }`}
        onClick={
          seekable
            ? () => {
                p.onSeek(l.time);
                // Center the clicked line NOW — the active-line step alone
                // misses clicks inside the line already playing.
                centerLine(i);
              }
            : undefined
        }
        title={seekable ? "Click to seek" : undefined}
      >
        {/* One layout for every state: the line block is laid out at the
            active size and scaled down when inactive — a compositor-only
            animation, so text never re-wraps mid-growth and the scroll
            target never shifts under it. Weight is constant for the same
            reason (weight changes re-flow glyph widths). */}
        <div
          ref={(el) => {
            primaryRefs.current[i] = el;
          }}
          className={`${size.active} leading-snug ${synced ? `${LINE_EASE} font-semibold` : ""} ${
            isActive ? ink.active : synced ? ink.dim : ink.plain
          }`}
          style={
            synced
              ? { transform: `scale(${isActive ? 1 : INACTIVE_SCALE[lyricSize]})`, transformOrigin: "0 50%" }
              : { transform: `scale(${INACTIVE_SCALE[lyricSize]})`, transformOrigin: "0 50%" }
          }
        >
          {!replaced && isActive && karaoke && l.words?.length ? (
            <KaraokeWords
              words={l.words}
              time={dispTime}
              currentClass={ink.wordNow}
              sungClass={ink.wordSung}
              upcomingClass={ink.wordNext}
            />
          ) : primary}
          {trans && !transDup && (
            <div className={`${size.xlit} font-normal text-accent-soft/70 mt-0.5 leading-snug`}>{trans}</div>
          )}
        </div>
      </div>
    );
  };

  // Freshness-gated tag fallbacks: while the next track's payload loads,
  // never substitute the PREVIOUS track's tag values into these strings —
  // the queue already knows title/artist/album, so prefer those.
  const freshTags = tagsStale ? undefined : tags;
  const title = p.current.title || freshTags?.TITLE || p.current.file.replace(/\.[^.]+$/, "");
  // Same vertical order and formatting as the player bar's text block:
  // title, then ALBUM, then ARTIST — the two sub-lines render identically.
  const albumLine = p.current.album || freshTags?.ALBUM || album?.meta?.ALBUM || "—";
  const artistLine = p.current.artist || freshTags?.ARTIST || p.current.albumPath.split("/").pop() || "";
  const upNext = !p.shuffle ? queue[index + 1] as
    | { title?: string; artist?: string; file: string }
    | undefined : undefined;
  const upNextLabel = upNext
    ? `${upNext.title || upNext.file.replace(/\.[^.]+$/, "")}${upNext.artist ? ` — ${upNext.artist}` : ""}`
    : "";
  // Ultra-condensed readout under the title: "16/44.1" (same as the player
  // bar); the tooltip carries the full codec/bitrate detail.
  const techStr = fmtPair(tech);
  const techTip = fmtTech(tech);

  // What the options menu says about the gain the player is applying right now
  // — the same three cases the bar's chip covers: tags, measured on demand,
  // and unity (nothing shown in the bar).
  const rgDb = (db: number) => `${db >= 0 ? "+" : ""}${db.toFixed(1)} dB`;
  const rgLine =
    p.rg.mode === "off"
      ? "Off — every file plays at its own level."
      : p.rg.applied
        ? `${rgDb(p.rg.applied.gain)} — ${
            p.rg.applied.analyzed
              ? "measured on demand: this file has no ReplayGain tags"
              : "from ReplayGain tags"
          }${
            p.rg.mode === "album" && !p.rg.applied.album
              ? "; this album has no album gain, so its track value was used"
              : ""
          }${p.rg.applied.source?.endsWith("+clamp") ? "; reduced to stop clipping" : ""}`
        : "Unity — no ReplayGain for this file.";

  // Shared control blocks — the audio layout shows them under the cover;
  // the fullscreen-video layout overlays them at the bottom of the picture.
  //
  // Every tier of the metadata block reads its colour from the player's ONE
  // ink table (`INK`), and the block draws NOTHING of its own: no fill, no
  // border, no blur, no halo. It used to carry `np-veil np-veil-pill` — a
  // tinted, blurred, rounded box spread past its edges — and the owner rejected
  // it outright: a grey button pasted under the artwork. The tint was there to
  // move the FIELD so a fixed ink table could stay legible on the cover's
  // polarity, which is the wrong lever. The ink is one table now (white,
  // zinc-100, zinc-300) and the field is what gets darkened, once, for every
  // cover — the full-bleed wash over the ambience (see the wash below).
  // Measured by tools/check_np_metadata_contrast.cjs, which reads the pixels
  // just inside the block's edges — the widest rows' own field: the title
  // and the secondary tiers clear AA on the mid-grey and the white cover with
  // no panel under them, only the wash and the glyph shadow.
  // The track's own rating, editable here exactly as in the bar: one map and
  // one optimistic setter (`lib/ratings`), so a star clicked in the fullscreen
  // player lights up in the row behind it — and vice versa.
  const { data: ratingsData } = useRatings();
  const { setRating, pending } = useSetRating();
  const textBlock = (
    /* Every text row keeps a fixed height and is ALWAYS rendered —
       blanking a row while the next track's tags load is what made
       the block (and the title itself) shake on next/previous. */
    <div className={`text-center w-[26rem] max-w-full min-w-0 ${ink.shade}`}>
      <div className="h-8 flex items-center justify-center gap-2" title={title}>
        <div className={`text-2xl font-bold truncate ${ink.active}`}>{title}</div>
        <AdvisoryMark value={freshTags?.ITUNESADVISORY ?? p.current.advisory} />
        {/* bit depth/sample rate rides beside the title, same as the
            player bar; tooltip carries the full codec/bitrate detail */}
        {techStr && (
          <span className={`text-[11px] font-mono shrink-0 ${ink.dim}`} title={techTip || undefined}>
            {techStr}
          </span>
        )}
      </div>
      <div className="h-5 mt-1 flex items-center justify-center" title={albumLine}>
        <div className={`text-sm truncate ${ink.dim}`}>{albumLine}</div>
      </div>
      <div className="h-5 mt-0.5 flex items-center justify-center" title={artistLine}>
        <div className={`text-sm truncate ${ink.dim}`}>{artistLine}</div>
      </div>
      {/* Fixed height and always rendered, like the rows above, so the block
          never jumps on next/previous. The control's own tooltip carries the
          rest: half stars on a star's left half, the value already set clears
          it, and the keyboard works (← / →, Delete). */}
      <div className="h-7 mt-1 flex items-center justify-center">
        <StarRating
          size="md"
          label="Track rating"
          value={ratingOf(ratingsData?.ratings, p.current.path)}
          onChange={(v) => setRating(p.current.path, v)}
          pending={pending(p.current.path)}
        />
      </div>
    </div>
  );
  /** The like toggle, in the one shape both layouts draw.
   *
   *  `className` carries the size and the PLACEMENT, because the two homes are
   *  different rooms: above lg it is one control in the transport row, and on a
   *  phone the same control sits alone at the player's bottom-left — the spot
   *  Apple Music keeps its favourite in, which is what the owner asked for. One
   *  definition, so the two can never drift into two different affordances. */
  const likeButton = (className: string) => (
    <button
      aria-label={p.liked ? "Unlike" : "Like this track"}
      aria-pressed={p.liked}
      className={`tap-hit rounded-lg transition-colors ${p.liked ? "text-accent" : ink.chromeButton} ${className}`}
      onClick={p.onToggleLike}
      title={p.liked ? "Unlike" : "Like this track"}
    >
      <Heart className={`h-[18px] w-[18px] ${p.liked ? "fill-current" : ""}`} />
    </button>
  );

  const transportRow = (
    <div className={`flex items-center justify-center gap-2.5 flex-wrap ${ink.shade}`}>
      <button aria-label="Shuffle" aria-pressed={p.shuffle} className={`p-2 rounded-lg transition-colors ${p.shuffle ? "text-accent" : ink.chromeButton}`} onClick={p.onToggleShuffle} title="Shuffle">
        <Shuffle className="h-4 w-4" />
      </button>
      <button aria-label="Previous track" className={`p-2.5 rounded-lg transition-colors ${ink.chromeStrong} hover:bg-black/5`} onClick={() => p.onStep(-1)} title="Previous track">
        <SkipBack className="h-5 w-5" />
      </button>
      <button
        aria-label={p.playing ? "Pause" : "Play"}
        aria-pressed={p.playing}
        className="p-4 rounded-lg bg-accent on-accent hover:bg-accent-soft shadow-lg transition-colors"
        onClick={p.onTogglePlay}
        title="Play / pause (Space)"
      >
        {p.playing ? <Pause className="h-6 w-6" /> : <Play className="h-6 w-6 ml-0.5" />}
      </button>
      <button aria-label="Next track" className={`p-2.5 rounded-lg transition-colors ${ink.chromeStrong} hover:bg-black/5`} onClick={() => p.onStep(1)} title="Next track">
        <SkipForward className="h-5 w-5" />
      </button>
      <button aria-label="Repeat one" aria-pressed={p.loop} className={`p-2 rounded-lg transition-colors ${p.loop ? "text-accent" : ink.chromeButton}`} onClick={p.onToggleLoop} title="Repeat one">
        <Repeat className="h-4 w-4" />
      </button>
      <button
        className={`p-2 rounded-lg transition-colors text-xs font-mono min-w-[46px] ${ink.chromeButton}`}
        onClick={() => p.onSpeedChange(nextSpeed(p.speed, 1))}
        title="Playback speed — [ slower · ] faster · 0 reset to 1×"
      >
        {fmtSpeed(p.speed)}
      </button>
      {/* the divider belongs to the row's CONTROL line, not the row box:
          self-center + a fixed height keep it on the same axis as the icons
          either side of it, whatever heights they have */}
      <span className="w-px h-6 bg-white/15 mx-1 self-center shrink-0" />
      {/* Desktop's home for the favourite. On a phone this control lives at the
          player's bottom-left instead (`likeButton`'s other call site) — one
          home per width, never two buttons for one flag. */}
      {likeButton("p-2 hidden lg:inline-flex items-center justify-center")}
      <div className="relative">
        <button
          aria-label="Add this track to a playlist"
          aria-expanded={plOpen}
          className={`p-2 rounded-lg transition-colors ${plOpen ? "text-accent" : ink.chromeButton}`}
          onClick={() => setPlOpen(!plOpen)}
          title="Add this track to a playlist"
        >
          <ListPlus className="h-[18px] w-[18px]" />
        </button>
        <Popover
          open={plOpen}
          onClose={() => setPlOpen(false)}
          align="center"
          placement="top"
          frost
          panelClass="w-60 p-1.5 max-h-72 flex flex-col"
        >
            <div className="text-[10px] uppercase tracking-wider text-zinc-500 px-2 pt-1 pb-1">Playlists</div>
            <div className="overflow-y-auto min-h-0">
              {(playlists ?? []).filter((pl) => pl.kind === "manual").map((pl) => (
                <MenuItem key={pl.id} label={pl.name} title={`Add to ${pl.name}`} onClick={() => addToPlaylist(pl)} />
              ))}
              {!(playlists ?? []).some((pl) => pl.kind === "manual") && (
                <div className="px-2 py-1.5 text-[11px] text-zinc-600">No manual playlists yet</div>
              )}
            </div>
              <div className="flex items-center gap-1.5 pt-1.5 mt-1 border-t border-white/10">
                <input
                  className="input !py-1 !px-2 text-[11px] flex-1 min-w-0"
                  placeholder="New playlist name"
                  value={newPlName}
                  onChange={(e) => setNewPlName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") createPlaylistAndAdd();
                  }}
                  autoFocus
                />
                <button className="btn-primary !py-1 !px-2 text-[11px] shrink-0" onClick={createPlaylistAndAdd} disabled={!newPlName.trim()}>
                  Create
                </button>
              </div>
        </Popover>
      </div>
    </div>
  );
  const seekRow = (
    <div className={`flex items-center gap-2 text-xs w-[26rem] max-w-full px-2 ${ink.shade} ${ink.chromeText}`}>
      <span className="w-10 text-right font-mono tabular-nums">{fmtDuration(dispTime)}</span>
      <ScrubSeek
        videoPath={videoPath}
        value={dispTime}
        max={duration || 0}
        onChange={p.onSeek}
        className="flex-1 min-w-0"
      />
      {/* the divider sits dead-center between the duration and volume
          groups, on the same optical axis as the sliders — same height and
          margins as the transport row's, so both read as one separator. The
          duration hugs it (text-right in its fixed box): the box's trailing
          slack would otherwise push the divider visibly off-centre towards
          the volume group. */}
      <span className="w-10 text-right font-mono tabular-nums">{fmtDuration(duration)}</span>
      <span className="w-px h-6 bg-white/15 self-center shrink-0 mx-2" />
      <VolumeControl />
    </div>
  );

  // Music-video controls: caption track + how the picture fills the screen.
  // Both live in the bottom overlay with the transport, so they are one click
  // away while the picture plays (and ride the same auto-hide).
  const taggedCaption = captionTracks.find((t) => t.default);
  const videoRow = (
    <div className="flex items-center justify-center gap-4 flex-wrap">
      <label className="flex items-center gap-1.5 text-xs text-zinc-300" title="Subtitle / caption track">
        <Captions className="h-4 w-4 text-zinc-400 shrink-0" />
        <select
          className="chip text-[11px] bg-white/10 border border-white/15 text-zinc-200 px-1.5 py-1 rounded-md"
          value={p.video.captions === null ? "tagged" : String(p.video.captions)}
          onChange={(e) =>
            p.video.onCaptions(e.target.value === "tagged" ? null : Number(e.target.value))
          }
        >
          <option value="tagged">As tagged{taggedCaption ? ` — ${taggedCaption.label}` : ""}</option>
          <option value="-1">Off</option>
          {captionTracks.map((t, i) => (
            <option key={t.key} value={i}>
              {t.label}
            </option>
          ))}
        </select>
      </label>
      <div className="flex items-center gap-1 text-xs text-zinc-300" title="How the picture fills the screen">
        <span className="text-[11px] text-zinc-400 mr-0.5">Fit</span>
        {(["contain", "cover", "stretch"] as const).map((a) => (
          <button
            key={a}
            className={`chip text-[10px] border ${
              p.video.aspect === a ? "bg-accent on-accent border-accent" : "bg-white/5 border-white/15 text-zinc-400 hover:text-white"
            }`}
            onClick={() => p.video.onAspect(a)}
            title={
              a === "contain"
                ? "Whole picture, letterboxed (default)"
                : a === "cover"
                  ? "Fill the screen, cropping the overflow"
                  : "Fill the screen, ignoring the aspect ratio"
            }
          >
            {a.toUpperCase()}
          </button>
        ))}
      </div>
    </div>
  );

  return (
    /* No polarity tint on the root anymore: the whole player draws its text on
       the ambience through the wash below, and the only surfaces that still
       frost anything are the floating menus, which pin their own tint
       (`np-veil-dark np-veil-panel`, index.css). */
    <div className={`fixed inset-0 z-50 overflow-clip ${videoPath ? "bg-transparent" : "bg-zinc-950"} ${videoPath && !chromeVisible ? "cursor-none" : ""}`}>
      {/* overflow-clip (not hidden): a hidden box is still a scroll container,
          so wheel / scrollIntoView can silently scroll the whole overlay and
          leave the view "stuck" half-rendered. Clip can never be scrolled. */}
      {/* Music videos own the whole screen: the picture fills the viewport
          (object-contain over black), controls overlay the bottom edge. The
          ambience below is skipped — the video IS the background. */}
      {!videoPath && (
      <>
      {/* ---- ambient background -------------------------------------------
          Five layers: the cover's own colors blurred underneath, an aurora
          sweep, drifting color fields, one glow the music swells (--amb,
          written every 70 ms), then grain and a vignette to settle it. Only
          the glow reacts to the audio, and only through that stepped value —
          per-frame reactions were what strobed. The rest run on their own
          CSS clocks (24-55 s), fast enough to read as movement and slow
          enough to stay a backdrop. */}
      <div ref={ambRef} className="absolute inset-0 overflow-clip" aria-hidden>
        <div className="amb-cover absolute inset-0 blur-3xl opacity-[0.34]">
          <CoverImg albumPath={p.current.albumPath} coverFile={coverFile} wrapperClass="w-full h-full" />
        </div>
        <div className="amb-sweep absolute -inset-1/2">
          <div
            className="absolute inset-0"
            style={{
              background: `conic-gradient(from 0deg at 50% 50%, transparent 0deg, rgb(${rgb.map((v) => Math.min(255, v + 30)).join(" ")} / 0.30) 70deg, transparent 150deg, rgb(${rgb.map((v) => Math.max(0, v - 25)).join(" ")} / 0.22) 250deg, transparent 330deg)`,
            }}
          />
        </div>
        {orbs && (
          /* .amb-orbs carries the beat: the whole color field swells with
             --amb, so the music moves the backdrop itself and not just one
             edge of it */
          <div className="amb-orbs absolute inset-0">
            {/* mix-blend-mode: screen adds light instead of turning muddy,
                which is what keeps overlapping fields colorful; each field
                also carries its own hue rotation so the backdrop is not one
                flat tint of the cover */}
            <div
              className="amb-orb amb-orb-a w-[62vw] h-[62vw] -top-[18vw] -left-[12vw]"
              style={{ background: `radial-gradient(circle at 38% 34%, rgb(${rgb.join(" ")} / 0.9), transparent 66%)` }}
            />
            <div
              className="amb-orb amb-orb-b w-[54vw] h-[54vw] bottom-[-16vw] right-[-10vw]"
              style={{ background: `radial-gradient(circle at 40% 30%, rgb(${rgb.map((v) => Math.min(255, v + 34)).join(" ")} / 0.8), transparent 64%)` }}
            />
            <div
              className="amb-orb amb-orb-c w-[44vw] h-[44vw] top-[26%] left-[34%]"
              style={{ background: `radial-gradient(circle at 60% 40%, rgb(${rgb.map((v) => Math.max(0, v - 28)).join(" ")} / 0.75), transparent 62%)` }}
            />
          </div>
        )}
        {/* the two music-driven layers: a wide glow behind the artwork and a
            hue-shifted bloom over the color fields. Both read the same
            --amb, so a kick is visible across the whole background, and both
            are opacity/transform only — never repainted per frame */}
        <div
          className="amb-glow absolute inset-0"
          style={{ background: `radial-gradient(ellipse 58% 46% at 50% 52%, rgb(${rgb.join(" ")} / 0.5), transparent 72%)` }}
        />
        <div
          className="amb-bloom absolute inset-0"
          style={{ background: `radial-gradient(ellipse 72% 62% at 50% 46%, rgb(${rgb.join(" ")} / 0.55), transparent 66%)` }}
        />
        <div className="amb-grain absolute inset-0" />
        <div className="amb-vignette absolute inset-0" />
      </div>
      {/* The field lift the chosen ink asks for — and NOTHING when it asks for
          none, which is every cover dark enough for the white table: the
          background is then the artwork's own ambience with nothing added, the
          way the owner asked for it. A bright cover gets one full-bleed scrim
          built from ITS OWN colour (never a grey), with no edge, rounding or
          blur — a scrim, not a panel. `npInk` computes it, and
          tools/check_np_metadata_contrast.cjs measures the result per cover. */}
      {ink.scrim && <div className="absolute inset-0" style={{ background: ink.scrim }} />}
      </>
      )}

      {videoPath && (
        /* the picture itself is the player bar's portaled <video>, filling
           the viewport one z-step below this overlay — this layer only
           carries the click surface and the control bar */
        <div className="absolute inset-0">
          {/* click surface for play/pause — sits above the video, below
              the bottom controls, and keeps working while the chrome is
              hidden */}
          <div
            className="absolute inset-0 z-[1] cursor-pointer"
            onClick={p.onTogglePlay}
            title="Play / pause (Space)"
          />
          {/* bottom control overlay — same blocks as the audio layout; eases
              away (with the cursor) while the video plays untouched */}
          <div
            className={`safe-np-video absolute inset-x-0 bottom-0 z-10 bg-gradient-to-t from-black/85 via-black/45 to-transparent pt-24 transition-[opacity,transform] duration-300 ease-out ${
              chromeVisible ? "" : "pointer-events-none opacity-0 translate-y-6"
            }`}
          >
            <div className="max-w-3xl mx-auto flex flex-col items-center gap-4">
              {videoRow}
              {textBlock}
              {transportRow}
              {seekRow}
            </div>
          </div>
        </div>
      )}

      {/* pointer-events pass through to the fullscreen video; the top bar
          opts back in so its buttons still work. z-[2]: the video layer's
          click-catcher is z-[1] in the same (root) stacking context — the
          bar must paint and hit-test above it, while the bottom controls
          (z-10) and queue drawer (z-20) stay above the bar. */}
      <div className={`relative z-[2] h-full flex flex-col ${videoPath ? "pointer-events-none" : ""}`}>
        {/* top bar — exit button top-left, queue/options cluster top-right;
            eases away with the bottom overlay while the video plays.
            `safe-np-top` carries the base padding AND the notch/status-bar
            inset: the overlay is `fixed inset-0`, so without it the system
            clock, the queue readout and the options button share one line. */}
        <div
          className={`safe-np-top flex items-center justify-between transition-[opacity,transform] duration-300 ease-out ${
            videoPath ? (chromeVisible ? "pointer-events-auto" : "pointer-events-none opacity-0 -translate-y-3") : ""
          } ${videoPath ? "text-zinc-300" : `${ink.shade} ${ink.chromeText}`}`}
        >
          <button
            className="p-2 rounded-lg transition-colors hover:bg-white/10 text-current hover:text-white"
            onClick={p.onClose}
            title="Exit fullscreen (Esc)"
            aria-label="Exit fullscreen"
          >
            <ChevronDown className="h-5 w-5" />
          </button>
          <div className="flex items-center gap-1 min-w-0">
            {p.queuePos && (
              <span className="text-[10px] font-mono text-current opacity-80 mr-1 tabular-nums" title="Queue position">
                {p.queuePos}
              </span>
            )}
            {/* up next — lives beside the queue it describes; click opens it.
                Always on the bar: inert when nothing is queued. */}
            <button
              className={`max-w-[15rem] min-w-0 items-center gap-1.5 px-1.5 py-1 rounded-md text-[10px] font-mono hidden sm:flex ${
                upNextLabel
                  ? "text-current hover:text-white hover:bg-white/10 transition-colors"
                  : "text-current opacity-60 pointer-events-none"
              }`}
              onClick={() => setQueueOpen(true)}
              title={upNextLabel ? `Up next — ${upNextLabel} · click to view the queue` : "Up next — nothing queued"}
            >
              <span className="uppercase tracking-widest opacity-70 shrink-0">Up next</span>
              <span className="truncate">{upNextLabel || "—"}</span>
            </button>
            <button
              ref={queueTriggerRef}
              className={`p-2 rounded-lg transition-colors hover:bg-white/10 ${queueOpen ? "text-white bg-white/10" : "text-current hover:text-white"}`}
              onClick={() => setQueueOpen(!queueOpen)}
              title="Up next (queue)"
              aria-label="Up next queue"
              aria-expanded={queueOpen}
            >
              <ListMusic className="h-5 w-5" />
            </button>
            {/* inert over a music video: <Visualizer> only renders in the
                audio layout, so the toggle is hidden rather than a no-op */}
            {!videoPath && (
              <button
                className={`p-2 rounded-lg transition-colors hover:bg-white/10 ${viz ? "text-accent" : "text-current hover:text-white"}`}
                onClick={() => {
                  const v = !viz;
                  setViz(v);
                  persist(VIZ_KEY, v ? "1" : "0");
                }}
                title="Toggle visualizer bars"
                aria-label="Toggle visualizer bars"
                aria-pressed={viz}
              >
                <AudioLines className="h-5 w-5" />
              </button>
            )}
            {/* the lyrics pane's ONE control, built exactly like the
                visualizer toggle beside it (same box, same active treatment,
                same `aria-pressed`) and the same idea as the player bar's
                lyrics button (Mic2 on the bar toggles the docked pane).
                Hidden when the track has no lyrics rather than shown inert —
                the same rule the visualizer follows over a music video: a
                control that cannot do anything is not drawn. It used to be
                nothing at all: the pane appeared whenever lyrics existed and
                could not be put away, which is what made it feel like an
                accident of the layout instead of something you own. */}
            {!videoPath && layoutHasLyrics && (
              <button
                className={`p-2 rounded-lg transition-colors hover:bg-white/10 ${showLyrics ? "text-accent" : "text-current hover:text-white"}`}
                onClick={() => {
                  const v = !showLyrics;
                  setShowLyrics(v);
                  persist(LYRICS_KEY, v ? "1" : "0");
                }}
                title="Toggle the lyrics pane"
                aria-label="Toggle the lyrics pane"
                aria-pressed={showLyrics}
              >
                <Mic2 className="h-5 w-5" />
              </button>
            )}
            <div className="relative">
              <button
                className={`p-2 rounded-lg transition-colors hover:bg-white/10 ${options ? "text-white bg-white/10" : "text-current hover:text-white"}`}
                onClick={() => setOptions(!options)}
                title="Lyrics & display options"
                aria-label="Lyrics and display options"
                aria-expanded={options}
              >
                <Settings2 className="h-5 w-5" />
              </button>
              <Popover
                open={options}
                onClose={() => setOptions(false)}
                frost
                panelClass="w-72 max-w-[calc(100vw-1.5rem)] p-1.5"
              >
                  <div className="text-[10px] uppercase tracking-wider text-zinc-500 px-2 pt-1 pb-1">Lyrics</div>
                {[
                    { id: "xlit" as const, label: "Transliteration", on: showXlit, act: () => toggleOpt("xlit") },
                    { id: "trans" as const, label: "Translation", on: showTrans, act: () => toggleOpt("trans") },
                    {
                      id: "karaoke" as const,
                      label: "Karaoke syllable sweep",
                      on: karaoke,
                      act: () => {
                        const v = !karaoke;
                        setKaraoke(v);
                        persist(KARAOKE_KEY, v ? "1" : "0");
                      },
                    },
                  ].map((o) => (
                    <label key={o.id} className="flex items-center gap-2 px-2 py-1.5 rounded-md hover:bg-white/10 cursor-pointer text-xs text-zinc-300">
                      <input type="checkbox" className="accent-[var(--accent)]" checked={o.on} onChange={o.act} />
                      {o.label}
                    </label>
                  ))}
                  <div className="flex items-center gap-2 px-2 py-1.5 text-xs text-zinc-300">
                    <span className="flex-1">Lyrics size</span>
                    {(["sm", "md", "lg"] as const).map((s) => (
                      <button
                        key={s}
                        className={`chip text-[10px] border ${lyricSize === s ? "bg-accent on-accent border-accent" : "bg-white/5 border-white/15 text-zinc-400 hover:text-white"}`}
                        onClick={() => {
                          setLyricSize(s);
                          persist(SIZE_KEY, s);
                        }}
                      >
                        {s.toUpperCase()}
                      </button>
                    ))}
                  </div>
                  <div className="flex items-center gap-2 px-2 py-1.5 text-xs text-zinc-300">
                    <span className="flex-1" title="Size of the lyrics pane — saved for every future visit">Zoom</span>
                    {/* The stored value is a MULTIPLIER; the box speaks the new
                        scale, where 100 % is what 150 % used to render (the
                        owner's rule). The default 1.5 therefore SHOWS as 100 %,
                        and nobody's saved size changes under them. */}
                    <LyricZoom
                      pct={Math.round((lyricZoom / LYRIC_ZOOM_BASE) * 100)}
                      onChange={(p) => {
                        const next = (p / 100) * LYRIC_ZOOM_BASE;
                        setLyricZoom(next);
                        persist(ZOOM_KEY, String(next));
                      }}
                    />
                  </div>
                  {/* The lyric offset sits with the size and zoom: all three
                      are "how the lyrics are read". Save writes the shift into
                      the track's own lyrics (tags / .lrc) — see LyricOffset. */}
                  <div className="flex items-center gap-2 px-2 py-1.5 text-xs text-zinc-300">
                    <span
                      className="flex-1"
                      title="Move every lyric line's timestamp until it lands with the track, then Save to write it into the file's lyrics. Untimed lines are never touched."
                    >
                      Offset
                    </span>
                    <LyricOffset
                      path={p.current.path}
                      ms={offsetMs}
                      onChange={setOffsetMs}
                      onSaved={(lrc) => setLyricsText(lrc)}
                    />
                  </div>
                  <div className="text-[10px] uppercase tracking-wider text-zinc-500 px-1 pt-2 pb-1">This track</div>
                  {/* Details and credits, one click from the player itself —
                      the same modal the library row's ⓘ opens, over the
                      fullscreen player (Modal is z-[60] against the player's
                      z-50, which is exactly why it can be opened from here). */}
                  <button
                    className="w-full text-left px-2 py-1.5 rounded-md hover:bg-white/10 text-xs text-zinc-300 flex items-center gap-2"
                    onClick={() => {
                      setOptions(false);
                      setDetailsOpen(true);
                    }}
                  >
                    <Info className="h-3.5 w-3.5 text-zinc-500" />
                    Track details &amp; credits…
                  </button>
                  <div className="text-[10px] uppercase tracking-wider text-zinc-500 px-1 pt-2 pb-1">Background</div>
                  <label className="flex items-center gap-2 px-2 py-1.5 rounded-md hover:bg-white/10 cursor-pointer text-xs text-zinc-300">
                    <input
                      type="checkbox"
                      className="accent-[var(--accent)]"
                      checked={orbs}
                      onChange={() => {
                        const v = !orbs;
                        setOrbs(v);
                        persist(ORBS_KEY, v ? "1" : "0");
                      }}
                    />
                    Animated color drift
                  </label>
                  <label className="flex items-center gap-2 px-2 py-1.5 rounded-md hover:bg-white/10 cursor-pointer text-xs text-zinc-300">
                    <input
                      type="checkbox"
                      className="accent-[var(--accent)]"
                      checked={vis}
                      onChange={() => {
                        const v = !vis;
                        setVis(v);
                        persist(VIS_KEY, v ? "1" : "0");
                      }}
                    />
                    Background glow follows the music
                  </label>
                  {/* inert over a music video: <Visualizer> only renders in the
                      audio layout, so the toggle is hidden rather than a no-op */}
                  {!videoPath && (
                    <label className="flex items-center gap-2 px-2 py-1.5 rounded-md hover:bg-white/10 cursor-pointer text-xs text-zinc-300">
                      <input
                        type="checkbox"
                        className="accent-[var(--accent)]"
                        checked={viz}
                        onChange={() => {
                          const v = !viz;
                          setViz(v);
                          persist(VIZ_KEY, v ? "1" : "0");
                        }}
                      />
                      Visualizer bars
                    </label>
                  )}
                  {/* loudness matching — stored in the config (Settings → DR /
                      ReplayGain) but felt right here, so it is edited here */}
                  <div className="text-[10px] uppercase tracking-wider text-zinc-500 px-1 pt-2 pb-1">Loudness · ReplayGain</div>
                  <div className="flex items-center gap-2 px-2 py-1.5 text-xs text-zinc-300">
                    <span className="flex-1" title="Per track, per album, or no loudness matching at all">Gain</span>
                    {(["track", "album", "off"] as const).map((m) => (
                      <button
                        key={m}
                        className={`chip text-[10px] border ${p.rg.mode === m ? "bg-accent on-accent border-accent" : "bg-white/5 border-white/15 text-zinc-400 hover:text-white"}`}
                        onClick={() => p.rg.onMode(m)}
                        title={
                          m === "album"
                            ? "Album gain — one loudness offset for the whole album"
                            : m === "track"
                              ? "Track gain — each track matched on its own"
                              : "Off — play files at their own level"
                        }
                      >
                        {m.toUpperCase()}
                      </button>
                    ))}
                  </div>
                  <div className="flex items-center gap-2 px-2 py-1.5 text-xs text-zinc-300">
                    <span className="flex-1" title="Extra gain on top of the ReplayGain value, applied to every track">Preamp</span>
                    <input
                      type="range"
                      min={-24}
                      max={24}
                      step={0.5}
                      value={preampDraft}
                      onChange={(e) => setPreampDraft(Number(e.target.value))}
                      disabled={p.rg.mode === "off"}
                      className="w-28 disabled:opacity-40"
                      title="Preamp — ±24 dB"
                    />
                    <span className="w-14 text-right text-[10px] text-zinc-500 tabular-nums">{rgDb(preampDraft)}</span>
                  </div>
                  <div className="text-[10px] text-zinc-600 px-2 pb-1">{rgLine}</div>
              </Popover>
            </div>
          </div>
        </div>

        {/* main area — music videos never reach this branch: their picture
            fills the screen behind the top bar (see the video layer above)
            with the same controls overlaid at the bottom edge.

            Alignment when the pane is put away is `start`, not `center`: the
            collapsed pane is still a (zero-width) flex item, so the row's gap
            still counts and the line overflows by that gap — centring splits
            the overflow across both edges and the cover lands half a gap left
            of the middle. Packed from the start, the cover column is the whole
            row and the art sits dead centre, which is also where the
            no-lyrics layout puts it. */}
        {!videoPath && (
        <>
        <div className={`safe-np-body flex-1 min-h-0 flex flex-col lg:flex-row items-center gap-4 sm:gap-8 ${
          paneOpen
            ? "overflow-y-auto [@media(min-height:560px)]:overflow-clip"
            : "overflow-y-auto"
        } lg:overflow-clip ${paneOpen ? "" : "lg:justify-start"}`}>
          {/* left column: cover, track/album/artist, all playback controls —
              centered as a group inside the full column height.
              `w-full`: this is a flex item in a column whose `items-center`
              sizes it to its CONTENT, so the fixed-width rows below (the
              title block, the seek row, the visualizer, all `w-[26rem]`)
              made the column 416px wide inside a 358px parent — the title
              and the visualizer then hung off both edges of a phone, past
              their own `max-w-full` (which measures against a parent that
              had already overflowed, so it clamped nothing).
              max-h-full + overflow-y-auto: on a short window this column is
              taller than the clipped row above it, which used to silently cut
              the bottom controls (and the visualizer strip with them) off with
              no way to reach them. Capping it makes it scroll instead, and the
              strip sticks to the bottom of that scrollport so the bars are
              always visible when enabled.
              ponytail: `justify-center` in a scroll container leaves the TOP
              overflow unreachable on very short windows; move to a safe-center
              layout if anyone ever uses the player that small. */}
          <div
            className={`w-full flex flex-col items-center justify-center gap-4 shrink-0 min-w-0 transition-[width] duration-300 ease-out ${
              paneOpen
                ? "lg:w-[42%] lg:h-full lg:max-h-full lg:min-h-0 lg:overflow-y-auto"
                : "max-h-full min-h-0 overflow-x-clip overflow-y-auto"
            }`}
          >
            <div className="relative">
              {orbs && (
                <div
                  className="artwork-glow absolute -inset-6 rounded-[2rem] blur-2xl"
                  style={{ background: `radial-gradient(circle, rgb(${rgb.join(" ")} / 0.55), transparent 70%)` }}
                />
              )}
              <CoverImg
                albumPath={p.current.albumPath}
                coverFile={coverFile}
                // One size per breakpoint per LAYOUT: the art never jumps when a
                // track's lyrics load or finish, and above lg the pane sits
                // beside it so the size is the same either way. On a phone the
                // pane's toggle is the one moment it changes — that IS the
                // request: the cover, title and controls collapse to a header
                // and the lyrics take the rest of the screen.
                wrapperClass={`relative rounded-2xl shadow-2xl bg-raise overflow-hidden lg:w-[min(28rem,48vh)] lg:h-[min(28rem,48vh)] ${
                  paneOpen ? "w-32 h-32" : "w-72 h-72"
                }`}
              />
            </div>
            {textBlock}
            {transportRow}
            {/* seek + volume — a single line under the transport */}
            {seekRow}
            {/* frequency-bar visualizer — the same one the fullscreen view
                uses; toggle via the button in the top bar or options menu.
                shrink-0 + explicit min height: this is the LAST child of a
                clipped column, and a shrinking flex item there collapsed to
                nothing (bars rendered, box clipped away). */}
            {viz && (
              <div className="sticky bottom-0 z-10 shrink-0 min-h-12 w-[26rem] max-w-full px-2">
                <Visualizer playing={p.playing} className="block h-12 w-full" />
              </div>
            )}
          </div>

          {/* lyrics column — plain, no panel, hugging the right edge; flex-1
              below lg so it can't overflow the viewport (h-full there would
              double-count with the cover block and clip the bottom half
              outside the scroll pane). While the next track's lyrics load,
              the previous ones stay on screen dimmed instead of collapsing
              the layout (which flashed the cover to the middle).

              `min-h-[45vh]` below lg is what keeps them READABLE there: the
              cover block is content-sized and claimed the whole height of a
              short (zoomed) window, which left this column 60 px tall at the
              very bottom of a clipped body — lyrics that were rendered and
              unreachable. With the floor the body scrolls (overflow-y-auto
              below lg) and the pane is a real reading surface.

              Shown and hidden by the toggle in the top bar, and the BOX is the
              same one in both states — only its size changes. Collapsing
              (w-0 / max-h-0 / overflow-clip) is what makes it take no room, so
              the cover column can widen to the whole row and the art glides
              back to the middle over 300 ms instead of snapping; the scroll
              container itself is never removed, so the reader's scroll
              position, the zoom and the auto-follow refs all survive a toggle.
              The lyric lines are divs, not controls, so hiding the subtree
              from assistive tech costs no tab stop. */}
          {!videoPath && layoutHasLyrics && (
            <div
              className={`relative flex flex-col max-w-3xl overflow-clip transition-[width,max-height,opacity,transform] duration-300 ease-out ${
                paneOpen
                  ? "flex-1 min-h-[45vh] [@media(min-height:560px)]:min-h-0 w-full lg:min-h-0 lg:h-full lg:max-w-none lg:flex-none lg:w-[56%] lg:ml-auto opacity-100 translate-x-0"
                  : "flex-none min-h-0 max-h-0 w-0 max-w-0 opacity-0 translate-x-6 pointer-events-none"
              }`}
              aria-hidden={!paneOpen}
              /* While the pane is collapsed its width is zero, so every row's
                 offsetTop is measured against a one-character-wide layout and
                 any follow write made there is meaningless. Re-open is the one
                 moment that matters: the sung line is re-centred once the box
                 has finished growing (a `transitionend` on the width, which is
                 the property that moves it at both breakpoints — a timeout
                 would drift off the 300 ms in the class). Nothing is written
                 while closed, so the reader's own scroll position is what the
                 pane shows in between. */
              onTransitionEnd={(e) => {
                if (e.target !== e.currentTarget || !paneOpen) return;
                if (e.propertyName !== "width") return;
                if (aStart >= 0) centerLine(aStart);
                else if (lyricsScrollRef.current) lyricsScrollRef.current.scrollTop = 0;
              }}
            >
              <div
                ref={lyricsScrollRef}
                /* No panel and no tint on the reading surface: what separates
                   the glyphs from the cover is the player's full-bleed wash
                   (deep enough for white ink on every cover) plus the glyph
                   shadow the scroller sets once here — text-shadow inherits, so
                   every line reads it. A `bg-white/35` rectangle was the grey
                   slab in the owner's screenshot, and the blurred tint that
                   replaced it still read as a box over the artwork; both are
                   gone. */
                className={`relative flex-1 min-h-0 overflow-y-auto overscroll-contain px-6 py-5 no-scrollbar ${ink.shade} transition-opacity duration-300 ${
                  staleLyrics ? "opacity-50" : "opacity-100"
                }`}
                style={{ zoom: lyricZoom }}
                onWheel={(e) => {
                  if (e.deltaY !== 0) takeOver();
                }}
                onTouchStart={takeOver}
              >
                {/* Symmetric pads so the first and last line can both reach
                    the anchor line: with a fixed tail spacer the pane ran out
                    of travel at the end of the song and looked frozen. */}
                {displayLines.length > 0 ? (
                  <>
                    <div style={{ height: LYRICS_PAD_TOP }} />
                    {displayLines.map(renderLine)}
                    <div style={{ height: LYRICS_PAD_BOTTOM }} />
                  </>
                ) : (
                  <div className={`text-sm whitespace-pre-wrap leading-relaxed ${ink.plain}`}>
                    {lyricsText}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>

        <div className="lg:hidden w-full shrink-0 flex items-center px-1 sm:px-2 pb-1">
          {/* The phone's favourite: bottom-left of the player, where a thumb
              looks for it and where Apple Music keeps its own. Outside the
              scrolling body on purpose — a control that can scroll off a
              phone's screen is not the control the owner asked for. The heart
              is the app's own favourite (a STAR in this app means a rating, a
              different store — see lib/ratings), and this row draws it only
              below lg, where the transport row does not. */}
          {likeButton("p-2.5 flex items-center justify-center")}
        </div>
        </>
        )}
      </div>

      {/* up-next queue drawer — same features as the player bar's queue
          popover: CLEAR upcoming, per-track ✕, drag to reorder */}
      {queueOpen && (
        <div role="dialog" aria-modal="true" aria-label="Up next queue" className="safe-np-queue absolute right-0 bottom-0 w-80 max-w-[85vw] z-20 np-veil np-veil-dark np-veil-panel flex flex-col rounded-l-2xl border-l border-t border-border">
          <div className="flex items-center justify-between px-4 py-3 border-b border-white/10 gap-2">
            <div className="text-[11px] uppercase tracking-widest text-zinc-400 min-w-0 truncate">
              Queue · {queue.length} track{queue.length === 1 ? "" : "s"}
              {queue.length > index + 1 ? ` · ${queue.length - index - 1} up next` : ""}
            </div>
            <div className="flex items-center shrink-0">
              {queue.length > index + 1 && (
                <button
                  className="px-1.5 py-1 rounded-md text-[10px] font-mono tracking-widest text-zinc-500 hover:text-white hover:bg-white/10"
                  onClick={() => setQueue(queue.slice(0, index + 1))}
                  title="Remove upcoming tracks"
                >
                  CLEAR
                </button>
              )}
              <button ref={queueCloseRef} className="p-2 rounded-lg hover:bg-white/10 text-zinc-400 hover:text-white" onClick={() => setQueueOpen(false)} title="Close queue" aria-label="Close queue">
                <X className="h-5 w-5" />
              </button>
            </div>
          </div>
          <div className="flex-1 min-h-0 overflow-y-auto p-2" ref={queueListRef}>
            {queue.map((t, i) => {
              const isCurrent = i === index;
              const isDragging = dragIdx === i;
              const isOver = overIdx === i && dragIdx !== null && dragIdx !== i;
              return (
                <div
                  key={t.path + i}
                  data-queue-index={i}
                  draggable
                  onDragStart={(e) => {
                    setDragIdx(i);
                    e.dataTransfer.effectAllowed = "move";
                    e.dataTransfer.setData("text/plain", String(i));
                  }}
                  onDragOver={(e) => {
                    e.preventDefault();
                    e.dataTransfer.dropEffect = "move";
                    if (overIdx !== i) setOverIdx(i);
                  }}
                  onDrop={(e) => {
                    e.preventDefault();
                    const from = dragIdx ?? Number(e.dataTransfer.getData("text/plain"));
                    if (Number.isFinite(from) && overIdx !== null && from !== overIdx) queueMove(from, overIdx);
                    setDragIdx(null);
                    setOverIdx(null);
                  }}
                  onDragEnd={() => {
                    setDragIdx(null);
                    setOverIdx(null);
                  }}
                  className={`group/qr w-full text-left px-2.5 py-2 rounded-lg flex items-center gap-3 transition-colors border-t-2 ${
                    isCurrent ? "bg-accent/15" : "hover:bg-white/10"
                  } ${isOver ? "border-accent" : "border-transparent"} ${isDragging ? "opacity-40" : ""}`}
                  title="Drag to reorder · click to play now"
                >
                  <button
                    className="min-w-0 flex-1 flex items-center gap-3 text-left"
                    onClick={() => {
                      // A queue row press is a PLAY press: the same action the
                      // play buttons use, so the track changes, `playing` stays
                      // in step, and pressing the row that is already playing
                      // restarts it instead of doing nothing.
                      useStore.getState().playNow(queue, i);
                    }}
                    title="Play this track now"
                  >
                    <span className={`text-[10px] font-mono w-5 text-right shrink-0 ${isCurrent ? "text-accent" : "text-zinc-600"}`}>
                      {isCurrent && p.playing ? "▶" : i + 1}
                    </span>
                    <span className="flex-1 min-w-0">
                      <span className={`block text-xs truncate ${isCurrent ? "text-white font-medium" : "text-zinc-300"}`}>
                        {t.title || t.file.replace(/\.[^.]+$/, "")}
                      </span>
                      <span className="block text-[10px] text-zinc-500 truncate">
                        {t.artist ?? t.albumPath.split("/").pop()}
                      </span>
                    </span>
                    {isCurrent && <span className="text-[10px] text-zinc-500 shrink-0">playing</span>}
                  </button>
                  <button
                    className="p-1 rounded text-zinc-600 hover:text-red-300 hover:bg-white/5 opacity-0 group-hover/qr:opacity-100 [@media(hover:none)]:opacity-100 transition-opacity shrink-0"
                    onClick={() => queueRemoveAt(i)}
                    title="Remove from queue"
                    aria-label={`Remove ${t.title || t.file} from queue`}
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Track details & credits, opened from the options menu. Rendered from
          inside the fullscreen player because that is where the click is: the
          Modal layer is z-[60] against this view's z-50, so it lands on top
          (the same reason the credits dialog could already be opened over the
          player from a library row). */}
      {detailsOpen && p.current && (
        <DetailsDialog
          albumPath={p.current.albumPath}
          trackPath={p.current.path}
          onClose={() => setDetailsOpen(false)}
        />
      )}

    </div>
  );
}
