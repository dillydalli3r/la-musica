import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AudioLines, Captions, ChevronDown, Info, ListMusic, ListPlus, Maximize2, Mic2, Minimize2, Pause, Play,
  Repeat, Settings2, Shuffle, SkipBack, SkipForward, Volume1, Volume2, VolumeX, X,
} from "lucide-react";
import { api } from "../api";
import VolumePct from "./VolumePct";
import FavHeart from "./FavHeart";
import { likeToasts } from "../lib/favs";
import LyricZoom from "./LyricZoom";
import LyricOffset from "./LyricOffset";
import { DetailsDialog } from "./AlbumDetails";
import { parseHexColor } from "../lib/accent";
import { toast, useStore } from "../store";
import { fmtTech, fmtPair, isVideoFile } from "../lib/fmt";
import { albumRef, artistRef, libraryRow, trackRef } from "../lib/refs";
import { AdvisoryMark, LyricsKindChip, allowPlainOf } from "./Badges";
import StarRating from "./StarRating";
import ScrollingText from "./ScrollingText";
import { ratingOf, useRatings, useSetRating } from "../lib/ratings";
import CoverImg from "./CoverImg";
import Popover, { MenuItem } from "./Popover";
import ScrubSeek from "./ScrubSeek";
import { MAX_DB, MIN_DB, activeAnalyser } from "../lib/analyser";
import Visualizer from "./Visualizer";
import { parsePlayerLrc, parseLrc, splitStoredLines, lyricsKindOf, activeLineRange, KaraokeWords, type LrcLine } from "./LyricsViewer";
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

/** How long the pointer may sit still before the fullscreen pane drops it
 *  (≈3 s). That window is the media-pane one: aim-then-reach between the
 *  artwork and the transport row costs ~1-2 s, so three seconds never punishes
 *  a hand that is on its way somewhere, and past it the arrow is only in the
 *  way of the picture — short enough that it is gone by the time the viewer
 *  has settled in to listen, long enough that it cannot flicker while a line
 *  of lyrics is read. The video overlay's own 2.5 s chrome timer (below) is a
 *  separate, older decision about the CONTROLS; this one is about the arrow. */
const IDLE_CURSOR_MS = 3000;
/** Whether there is a pointer to hide at all: a finger has no arrow, so a
 *  device whose primary input is coarse / cannot hover never arms the timer. */
const FINE_POINTER = "(hover: hover) and (pointer: fine)";
/** Surfaces that must never sit under a hidden arrow — any open modal or menu
 *  over the pane, whoever opened it. The player's own menus are known from
 *  state; this is for the ones it does not own (the shortcut sheet arrives on
 *  a keystroke, so "the pointer moved recently" is not a safe proxy for
 *  "nothing on screen is waiting for it"). */
const OPEN_OVER_PANE = '[aria-modal="true"], [role="menu"], dialog[open]';

/** The ambience window, in dB, measured RELATIVE to this track's own rolling
 * loud reference — a fixed window cannot work across masters. On a real
 * track the bass-weighted mix below swings inside ~4 dB, so the old fixed
 * −82..−57 window (25 dB wide) moved --amb by about 0.15 and the whole
 * backdrop barely breathed. The reference follows the loudest mix level
 * heard recently (~0.9 dB/s fall), and AMB_DYN_DB is the span beneath it
 * that maps to closed → open: quiet passages close the glow, the loud ones
 * open it, whatever the master's own level happens to be.
 * See the ambience tick for why the level is taken in dB, not raw bytes. */
const AMB_DYN_DB = 12;
const AMB_REF_FALL_DB = 0.07; // per tick (~0.9 dB/s at AMB_TICK_MS)
const AMB_REF_START_DB = -60;

/** The beat, as its own number. `--amb` is the SMOOTHED level (a swell that
 *  follows the music's dynamics); this is the part of a tick that arrives
 *  ABOVE that swell — a kick, a snare, a hit — so the backdrop can punch on
 *  the beat while the swell underneath keeps breathing. It is written as
 *  `--amb-pulse`, and the CSS layers that use it transition in ~0.1 s instead
 *  of ~0.3 s, which is the difference between "the background follows the
 *  music" and "the background pulses with it".
 *
 *  Decay rather than a second smoother: a hit has to be gone by the next tick
 *  it is not re-armed on, or a busy passage turns the whole layer into a
 *  flicker (the strobe the smoothing below exists to prevent). */
const AMB_PULSE_GAIN = 2.4;
const AMB_PULSE_MIN = 0.05;   // below this the pulse is simply not written
const AMB_PULSE_FALL = 0.78;  // per tick (~0.25 s to nothing)

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
  onTogglePlay: () => void;
  onSeek: (t: number) => void;
  onStep: (d: 1 | -1) => void;
  onToggleShuffle: () => void;
  onToggleLoop: () => void;
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
   *  the whole reading surface: a wide soft drop under the line, with no
   *  tight layer — an edge hugging the glyph is what reads as a border around
   *  the text instead of depth under it (see `.np-shade` in index.css). Its
   *  POLARITY is the ink's: dark under light glyphs, light under dark ones. */
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
  /** The TOP BAR's icon buttons — every one of them, in one pair of states:
   *  the exit button top-left and the queue, visualizer, lyrics, fullscreen
   *  and options buttons top-right.
   *
   *  ON is what the app lights a pressed control with: `text-accent` on the
   *  player bar's own lyrics button and on the sidebar's visualizer button,
   *  and the accent here as well. NOT on the LIGHT table: the default theme's
   *  `--accent` IS white, and a white glyph on a bright cover is nothing at
   *  all — there the table's full-strength ink carries the state instead,
   *  which is the same substitution `wordNow` makes.
   *
   *  OFF is this table's muted chrome ink DIMMED. That dim is the whole
   *  point: the bar's own grey (zinc-300 on the dark table) sits a hair under
   *  the white accent, so a toggle that swapped one for the other read as
   *  neither on nor off — the state was in the class list and nowhere on the
   *  screen. The same gesture mutes the queue readout and the lyric controls,
   *  and hover takes it back, so the row still answers the pointer.
   *
   *  Both halves belong to the WHOLE bar, not to the two toggles it started
   *  with (that is what the earlier version of this comment said, and the
   *  pair was only used on them): three of the buttons beside them spelled
   *  out `text-current hover:text-white` at full ink, the fullscreen button a
   *  bare `text-accent`, and the options button an open-state `text-white
   *  bg-white/10` — three different brightnesses in one row, two of which the
   *  LIGHT table could not honour at all (a fixed `hover:text-white` on a
   *  white cover is invisible). NowPlayingView's `barOn` / `barOff` are the
   *  only place either half is read, and every icon button in the bar uses
   *  them, so the row cannot drift apart again. The queue readout and the
   *  "Up next" chip's own label are text: the readout keeps `chromeText`. */
  chromeOn: string;
  chromeOff: string;
  /** The SLIDERS' ink — the seek bar and the volume bar, the two controls
   *  that paint a track rather than a glyph. They are four values because a
   *  track is four surfaces (the unplayed run, the played run, the thumb and
   *  the thumb's ring) and one number cannot carry them: a single zinc
   *  `#3a3a42` track measured 1.70:1 on a near-black cover and 1.01:1 on the
   *  mid-grey one, and a white accent thumb measured 1.64:1 on a white one —
   *  the bars the owner could not see. They live HERE, in the same table as
   *  the text, because a slider and the label above it disagreeing about the
   *  polarity is the same bug twice; the player writes them onto the slider's
   *  container as the `--seek-*` custom properties index.css paints with.
   *  (`--seek-pct`, the played run's end, is geometry rather than ink and is
   *  set by the controls themselves.) */
  seekTrack: string;
  seekFill: string;
  seekThumb: string;
  seekRing: string;
  /** The frequency strip's ink, when the visualizer is shown over the artwork.
   *  Same rule as the chrome above, and the same table: a canvas cannot wear a
   *  Tailwind class, so it takes the polarity itself and picks its own
   *  near-white / near-black tones (see Visualizer's `ink`). */
  viz: "light" | "dark";
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
  chromeOn: "text-accent hover:bg-white/10",
  chromeOff: "text-zinc-300 hover:text-white hover:bg-white/10 opacity-60 hover:opacity-100",
  // White at two strengths for the two runs, so the played run is told from
  // the unplayed one by more than the thumb's position, and a solid white
  // thumb with a dark ring so the dot reads as a knob on both runs.
  seekTrack: "rgb(255 255 255 / 0.42)",
  seekFill: "rgb(255 255 255)",
  seekThumb: "rgb(255 255 255)",
  seekRing: "rgb(0 0 0 / 0.45)",
  viz: "light",
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
  chromeOn: "text-zinc-950 hover:bg-black/5",
  // One ink, one opacity — never a translucent ink AND an opacity on top:
  // `text-zinc-950/75` at `opacity-60` (0.45 alpha over the field) measured
  // 2.9:1 on the white cover, i.e. the dimmed glyphs the owner read as
  // "not the same brightness" AND as blending into the artwork. 0.65 of the
  // solid ink clears the 3:1 non-text floor on every measured field.
  chromeOff: "text-zinc-950 opacity-65 hover:opacity-100 hover:bg-black/5",
  // Near-black at two strengths, the same rule as the dark table above: the
  // unplayed run at 0.50 alpha (3.8:1 on the white cover's washed field) and
  // the played run solid, with a white ring so the dark dot reads as a knob.
  seekTrack: "rgb(0 0 0 / 0.50)",
  seekFill: "rgb(9 9 11)",
  seekThumb: "rgb(9 9 11)",
  seekRing: "rgb(255 255 255 / 0.5)",
  viz: "dark",
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
        /* the level as the played run's end — the same `--seek-pct` the seek
           bar sets, so the two bars in this row read the same way */
        style={{ "--seek-pct": `${Math.round(vol * 100)}%` } as CSSProperties}
        title="Volume"
        aria-label="Volume"
      />
      <VolumePct value={vol} onChange={setVol} />
    </div>
  );
}

/** Tailwind's own `md`, written as a query in the SAME unit (48rem), so this
 *  line and the `md:` classes the layout switches on cannot drift apart. */
const MD_UP = "(min-width: 48rem)";

/** True at `md` and up, live. The compact phone header (see `npLyricsMode`'s
 *  `compactHeader`) is the one piece of this player that asks JavaScript for
 *  the width instead of letting a breakpoint class do the switching: the block
 *  below `md` is not a narrower version of the desktop one but a different
 *  composition of it (the thumbnail row, the block it stands in for), and
 *  which of the two the VOICE-OVER reads — and whether the pane is a sibling
 *  of the phone's header or the desktop's right-hand column — cannot be
 *  expressed as a `md:` utility on one subtree. Read on mount so the first
 *  paint already knows, and on `change` so a rotation or a dragged window
 *  re-decides instead of leaving the phone's layout on a desktop-width
 *  screen. */
function useMdUp() {
  const [up, setUp] = useState(() => window.matchMedia?.(MD_UP).matches ?? true);
  useEffect(() => {
    const mq = window.matchMedia?.(MD_UP);
    if (!mq) return;
    const on = (e: MediaQueryListEvent) => setUp(e.matches);
    setUp(mq.matches); // the width can move between render and this effect
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return up;
}

/** What the lyrics ON SCREEN are, as the player must draw them. Five states,
 *  because the pane and its control answer to five different facts:
 *
 *  * `"synced"` — timed text: the pane follows the sung line;
 *  * `"plain"` — untimed text the reader's own `lyrics_allow_plain` accepts;
 *  * `"plain-refused"` — untimed text while that setting is OFF, which is
 *    this app's failing state, not a reading state: the same red cross every
 *    other surface wears for it (`components/Badges`, `LyricsKindChip`);
 *  * `"instrumental"` — `INSTRUMENTAL=1`: stored lyrics, if any, stay hidden
 *    (the rule the docked sidebar states in words);
 *  * `"none"` — no lyrics at all.
 *
 *  `"plain-refused"` and `"none"` are exactly why this is one function instead
 *  of the `hasLyricsText(...) && !instrumental` it replaces: the pane may only
 *  be drawn or OFFERED where there are words this install will show, so a
 *  control cannot promise a pane that cannot be filled. */
export type NpLyricsState = "synced" | "plain" | "plain-refused" | "instrumental" | "none";

/** The fullscreen player's ONE layout decision, taken in one place (and
 *  pinned by tools/check_lyrics_kind.mjs): what the lyrics on screen ARE,
 *  whether the pane is offered at all, whether it is open, and whether the
 *  phone wears its compact header.
 *
 *  `lyrics` is the text the view is HOLDING — deliberately the stale one while
 *  the next track's payload is in flight, so next/previous never reflows the
 *  whole view (see `staleLyrics`); `instrumental` comes from that track's own
 *  tags; `allowPlain` is the user's setting, and `undefined` (config not read
 *  yet) must NOT claim a refusal the app has not verified.
 *
 *  The phone's header row is the LYRICS composition, not the phone's mode: the
 *  thumbnail-and-one-line header only stands in for the cover and the metadata
 *  block while the words they make room for are actually on screen. With no
 *  pane (no lyrics, an instrumental, refused plain lyrics, or the reader's own
 *  "lyrics off") a phone gets the full composition instead — cover, title,
 *  album · artist, transport, seek — centred, which is what the owner's report
 *  was about: the compact row was the phone's layout whatever the track held,
 *  so a track with no lyrics drew a bare strip on a whole screen. */
export function npLyricsMode({
  lyrics, instrumental, allowPlain, showLyrics, mdUp,
}: {
  lyrics: string | null | undefined;
  instrumental: boolean;
  allowPlain: boolean | undefined;
  showLyrics: boolean;
  mdUp: boolean;
}): {
  state: NpLyricsState;
  drawable: boolean;
  paneOpen: boolean;
  compactHeader: boolean;
} {
  const kind = instrumental ? null : lyricsKindOf(lyrics);
  const state: NpLyricsState = instrumental
    ? "instrumental"
    : kind === null
      ? "none"
      : kind === "plain" && allowPlain === false
        ? "plain-refused"
        : kind;
  // The two states with words to put in the pane: timed text, and untimed text
  // this install accepts.
  const drawable = state === "synced" || state === "plain";
  const paneOpen = drawable && showLyrics;
  return { state, drawable, paneOpen, compactHeader: !mdUp && paneOpen };
}

/** One metadata line of the fullscreen player — the title, and either half of
 *  the album · artist pair: a link to that entity's own page when the library
 *  lists the folder the track came from, plain text when it does not (a
 *  download being previewed has no page to open, and a route that cannot
 *  resolve is worse than none). A click STOPS there and closes the viewer:
 *  this pane covers the whole app, so navigating behind it would look like
 *  nothing happened.
 *
 *  `scroll` is the drifting line the block layout uses (components/
 *  ScrollingText), the same component the title beside it uses — one marquee,
 *  not a second. The phone's compact header passes `scroll={false}`: its title
 *  has always been a truncating span, so these lines keep that layout's own
 *  form and only gain the link.
 *
 *  The hover affordance is an underline rather than the bar's
 *  `text-accent-soft`: this surface draws its ink from the polarity table
 *  above, and `--accent-soft` is a near-white grey that would erase a line on
 *  the LIGHT table's near-black ink. An underline is the same affordance on
 *  both, and the two surfaces keep their own idiom. */
function MetaLink({
  href,
  text,
  className,
  scroll = true,
  onOpen,
}: {
  href: string | null;
  text: string;
  className?: string;
  scroll?: boolean;
  onOpen: () => void;
}) {
  const inner = scroll ? (
    <ScrollingText text={text} className={className} />
  ) : (
    <span className={`block min-w-0 truncate ${className ?? ""}`}>{text}</span>
  );
  if (!href) return inner;
  return (
    <Link
      to={href}
      className="min-w-0 hover:underline underline-offset-2"
      onClick={(e) => {
        e.stopPropagation();
        onOpen();
      }}
    >
      {inner}
    </Link>
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
  // The reader's own `lyrics_allow_plain` (mlo/config.py, OFF by default) —
  // the same ["config"] cache every settings surface reads and the Settings
  // page invalidates on save, so flipping that switch re-decides this player's
  // mode without a reload. `undefined` while it is in flight: an unread
  // setting must not claim a refusal the app has not verified (`allowPlainOf`).
  const { data: cfg } = useQuery({ queryKey: ["config"], queryFn: api.config, staleTime: 5 * 60 * 1000 });
  const allowPlain = allowPlainOf(cfg);
  // ---- the phone's compact header ------------------------------------------
  // Reported on a phone: the cover art, the title + format readout, the
  // album · artist row and the star row together took the whole screen before
  // the first control, so below `md` the overlay wears the header row (thumbnail,
  // title, artist · album) with the transport and the seek bar under it while
  // the lyrics are up. It was a MODE the lyrics button flipped, which is what
  // broke the phone: below `md` that press moved the block and the pane
  // together, so the reader's own "lyrics on" pick meant the pane at `md` and
  // up and the whole block below it — the same control, two different things,
  // and a phone came up showing no lyrics at all (the owner's report) with the
  // zoom / offset controls the pane carries nowhere on screen. It is now the
  // LYRICS composition alone (`npLyricsMode`'s `compactHeader`): the header
  // stands in for the block while words are on screen, and a track without any
  // gets the full composition back, cover and all. One button, one meaning,
  // every width (see the toggle).
  const mdUp = useMdUp();
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
  // The title/album/artist lines open the library's own pages, but a queue row
  // carries only the folder it came from while the routes prefer a
  // MusicBrainz ID (lib/refs) — so the rows come from the `/api/library`
  // payload the player bar already holds under this key: the same cache entry,
  // never a second fetch. A folder the library does not list has no page to
  // open, and that line stays plain text.
  const { data: lib } = useQuery({ queryKey: ["library"], queryFn: () => api.library(), staleTime: 5 * 60 * 1000 });
  const libRow = useMemo(() => libraryRow(lib, p.current.albumPath), [lib, p.current.albumPath]);
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
    () => parseHexColor(coverHex) ?? [113, 113, 122],
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
  const eased = useRef({ energy: 0, pulse: 0, slow: 0 });
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
      el.style.setProperty("--amb-pulse", "0");
      return;
    }
    let timer = 0;
    let freq: Uint8Array | null = null;
    // Starts at the CSS fallback, so the first tick only writes if the
    // audio actually asks for something else.
    let written = 0.45;
    let writtenPulse = 0;
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
      // The beat: whatever this tick has that the SLOW follower below has not
      // caught up to. The follower is deliberately slow (~1.5 s at 70 ms) and
      // deliberately not the swell above: a fast chase eats a transient inside
      // a tick or two, which is exactly why the first version of this barely
      // moved — the analyser's own `smoothingTimeConstant` means the level
      // arriving here is already softened, so what is left of a kick is the gap
      // to a SLOW average, not to a fast one.
      const slow = eased.current.slow;
      const nextSlow = slow + (energy - slow) * 0.05;
      eased.current.slow = nextSlow;
      const rise = Math.max(0, energy - nextSlow) * AMB_PULSE_GAIN;
      const pulse = Math.max(Math.min(1, rise), eased.current.pulse * AMB_PULSE_FALL);
      const shown = pulse < AMB_PULSE_MIN ? 0 : pulse;
      if (Math.abs(shown - writtenPulse) > 0.001) {
        writtenPulse = shown;
        el.style.setProperty("--amb-pulse", shown.toFixed(3));
      }
      eased.current.pulse = shown;
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

  // ---- idle cursor ---------------------------------------------------------
  // A bright arrow parked over the picture is the one piece of chrome nobody
  // asked for: after a beat of stillness the pane drops it, and the next move,
  // click or key brings it straight back. Same shape as the video chrome above
  // — ONE piece of state, one class on the pane, one effect with the listeners
  // — but its own clock, because that one owns the controls (and waits while
  // the picture is paused) while this one owns only the arrow.
  //
  // The arrow is only ever withheld while nothing on screen is waiting for it:
  //   * an open surface over the pane — the queue drawer, the options menu, the
  //     playlist popover, the track details, or a dialog this pane does not own
  //     (looked up in the DOM at hide time, since a keystroke can open one) —
  //     restores it when it opens and keeps it while it is up;
  //   * any press in progress — the seek bar being scrubbed, a queue row being
  //     reordered: the button is held and the pointer can sit perfectly still
  //     through the gesture (and a scrub can be dragged right off the seek
  //     row), so a held button is never idleness wherever the pointer has gone;
  //   * the player's own controls: those rows opt out of the pane's
  //     `cursor-none` in CSS (`cursor-auto`), so the arrow is there whenever it
  //     hovers anything clickable — the seek bar's hover preview included —
  //     without a second listener measuring hover.
  // A touch screen has no arrow to drop: with no fine pointer the timer is
  // never armed, and a tap cancels it instead of re-arming, so nothing here can
  // fight a finger-scroll or leave the pane in `cursor: none`.
  const [idleCursor, setIdleCursor] = useState(false);
  const idleRef = useRef(false);
  const cursorBusy = queueOpen || options || plOpen || detailsOpen || dragIdx !== null;
  useEffect(() => {
    if (videoPath || cursorBusy) {
      // The video overlay hands the arrow to its own chrome timer, and an open
      // surface or a drag always has it.
      idleRef.current = false;
      setIdleCursor(false);
      return;
    }
    if (!window.matchMedia?.(FINE_POINTER).matches) return; // nothing to hide
    let timer: number | null = null;
    // A held button — a scrub, a queue reorder, any press — is a gesture in
    // progress, never idleness: the arrow stays for as long as the button is
    // down, wherever the drag has taken the pointer (a scrub can legitimately
    // leave the seek row, and a hidden cursor mid-drag is the bug).
    let held = false;
    const disarm = () => {
      if (timer !== null) window.clearTimeout(timer);
      timer = null;
    };
    const arm = () => {
      disarm();
      timer = window.setTimeout(() => {
        timer = null;
        // Something opened over the pane while the pointer sat still (a
        // keystroke is enough): keep the arrow, and look again in a window
        // rather than drop it under a dialog. Same for a button still down.
        if (held || document.querySelector(OPEN_OVER_PANE)) arm();
        else {
          idleRef.current = true;
          setIdleCursor(true);
        }
      }, IDLE_CURSOR_MS);
    };
    const poke = (e: Event) => {
      if (e.type === "pointerdown") held = true;
      // `blur` too: a release outside the window is never delivered, and a
      // stuck "held" would only ever cost a hidden arrow that never comes back.
      else if (e.type === "pointerup" || e.type === "pointercancel" || e.type === "blur") held = false;
      if (idleRef.current) {
        idleRef.current = false;
        setIdleCursor(false);
      }
      // A finger owns the glass while it is down: cancel, and let the next real
      // pointer event start the clock again.
      if (e.type === "touchstart") disarm();
      else arm();
    };
    arm();
    // `wheel` because scrolling a long lyric while the pointer rests still is
    // not idleness; `touchstart` only ever cancels (above); `pointerup` /
    // `blur` only ever release a hold.
    const evts: (keyof WindowEventMap)[] = [
      "pointermove", "pointerdown", "pointerup", "pointercancel", "keydown", "wheel", "touchstart", "blur",
    ];
    evts.forEach((e) => window.addEventListener(e, poke, { passive: true }));
    return () => {
      evts.forEach((e) => window.removeEventListener(e, poke));
      disarm();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [videoPath, cursorBusy]);

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
  // The ONE derivation both layouts read: what those lyrics ARE (five states,
  // refused plain ones and instrumentals included), whether the pane may be
  // drawn or offered at all, whether it is open, and whether the phone wears
  // its compact header. It follows the text on screen — stale included — so
  // next/previous never reflows the view, and it reads the reader's
  // `lyrics_allow_plain` so a pane is never offered for words this install
  // refuses (`npLyricsMode` above carries the whole rule). It replaced a
  // `hasLyricsText(...) && !instrumental` here plus a width branch in the
  // pane and toggle conditions below, which gave the phone a second, hidden
  // owner of the same pane — the reason the persisted pick was ignored below
  // `md` and the offset / zoom controls never mounted there.
  //
  // Memoized like `lines` beside it, and for the same reason: the clock ticks
  // this component 20 times a second while a track plays, and asking the
  // question parses the whole lyric text (`lyricsKindOf`). The answer only
  // moves when the lyrics, the track's own INSTRUMENTAL, the setting, the
  // reader's pick or the width does.
  const { state: lyricsState, drawable: lyricsDrawable, paneOpen, compactHeader } = useMemo(
    () => npLyricsMode({ lyrics: lyricsText, instrumental, allowPlain, showLyrics, mdUp }),
    [lyricsText, instrumental, allowPlain, showLyrics, mdUp]
  );
  const hasLyrics = lyricsDrawable && !staleLyrics;
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
  // Native (browser-window) fullscreen: off unless the top-bar button asks for
  // it, and `fsOwn` marks the transitions this pane caused itself.
  const [nativeFs, setNativeFs] = useState(() => !!document.fullscreenElement);
  const fsOwn = useRef(false);

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
  //
  // Native fullscreen is now the viewer's own OPT-IN (the button in the top
  // bar), so a transition this pane asked for must not close it: `fsOwn` says
  // "we pressed it", and a windowed pane is what that button wanted.
  useEffect(() => {
    const onFs = () => {
      setNativeFs(!!document.fullscreenElement);
      if (!document.fullscreenElement && !fsOwn.current) closeRef.current();
      fsOwn.current = false;
    };
    document.addEventListener("fullscreenchange", onFs);
    return () => document.removeEventListener("fullscreenchange", onFs);
  }, []);

  const toggleNativeFs = () => {
    fsOwn.current = true;
    if (document.fullscreenElement) {
      document.exitFullscreen().catch(() => { /* gone */ });
      return;
    }
    document.documentElement.requestFullscreen?.().catch(() => { fsOwn.current = false; });
  };

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
  // The three lines' own pages. The track route always resolves (the path form
  // is the fallback), while album/artist come from the library row above — no
  // row, no link, which is what keeps a previewed download's lines from
  // pointing at a page that cannot exist.
  const trackHref = trackRef({
    path: p.current.path,
    tags: { MUSICBRAINZ_TRACKID: freshTags?.MUSICBRAINZ_TRACKID },
  });
  const albumHref = libRow ? albumRef(libRow.album) : null;
  const artistHref = libRow ? artistRef(libRow.artist) : null;
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
      <div className="h-8 flex items-center justify-center gap-2 min-w-0" title={title}>
        {/* The title DRIFTS when it does not fit — the same marquee the player
            bar's own title uses (components/ScrollingText), so a long track
            name is READ here instead of cut at "…" (reported). A short one
            never moves: the shift is measured, not guessed. It also opens the
            track's own page (see MetaLink). */}
        <MetaLink href={trackHref} text={title} className={`text-2xl font-bold ${ink.active}`} onOpen={p.onClose} />
        <AdvisoryMark value={freshTags?.ITUNESADVISORY ?? p.current.advisory} />
        {/* bit depth/sample rate rides beside the title, same as the
            player bar; tooltip carries the full codec/bitrate detail */}
        {techStr && (
          <span className={`text-[11px] font-mono shrink-0 ${ink.dim}`} title={techTip || undefined}>
            {techStr}
          </span>
        )}
        {/* The refused-plain mark. This install does not accept untimed lyrics
            (`lyrics_allow_plain` off), so the player offers no pane for them —
            and this is where that is SAID, on the row that already carries the
            track's other marks, in the same vocabulary every other surface
            uses (Badges' `LyricsKindChip`: the red cross, the reason on hover,
            which names the setting). One mark, one meaning, no second kind of
            notice invented for the player. Nothing is reserved for it: the
            state is a property of the TRACK, so a track change can take the
            mark away — but the row is the player's fixed-height title row
            either way, so nothing the block is made of moves when it does. */}
        {lyricsState === "plain-refused" && (
          <LyricsKindChip kind="plain" allowPlain={false} size="sm" />
        )}
      </div>
      {/* Album and artist on ONE row — "Hail to the Thief · Radiohead" is one
          fact pair, and the two stacked rows read as two unrelated lines
          (reported). Each half marquees for the same reason the title does
          (its own box, so only the half that does not fit drifts) and opens
          its own page; the row keeps its fixed height either way. */}
      <div
        className="h-5 mt-1 flex items-center justify-center gap-2 min-w-0"
        title={[albumLine, artistLine].filter(Boolean).join(" · ")}
      >
        <MetaLink href={albumHref} text={albumLine} className={`text-sm ${ink.dim}`} onOpen={p.onClose} />
        {albumLine && artistLine ? <span className={`shrink-0 text-sm ${ink.dim}`}>·</span> : null}
        {artistLine ? <MetaLink href={artistHref} text={artistLine} className={`text-sm ${ink.dim}`} onOpen={p.onClose} /> : null}
      </div>
      {/* Fixed height and always rendered, like the rows above, so the block
          never jumps on next/previous. The control's own tooltip carries the
          rest: half stars on a star's left half, the value already set clears
          it, and the keyboard works (← / →, Delete). */}
      <div className={`h-7 mt-1 flex items-center justify-center ${ink.chromeText}`}>
        <StarRating
          size="md"
          label="Track rating"
          value={ratingOf(ratingsData?.ratings, p.current.path)}
          onChange={(v) => setRating(p.current.path, v)}
          pending={pending(p.current.path)}
          /* The two colours are the INK's, not the control's defaults: this row
             sits straight on the artwork, where a zinc-600 outline and a white
             accent fill both blend into a bright cover. The outline keeps a
             little air (the scale should not shout) while the filled half takes
             the ink at full strength — the same polarity rule the lyrics, the
             chrome and the frequency strip follow (R52c). */
          emptyClass="text-current opacity-45"
          fillClass="fill-current"
        />
      </div>
    </div>
  );
  /** The like toggle, in the one shape both layouts draw.
   *
   *  `className` carries the size: one home now (the transport row, R267), so
   *  only the box tightens below `sm` with the controls around it. It used to
   *  be drawn twice, with the phone copy alone in a row of its own at the
   *  bottom-left — the owner asked "where is the like button?" about exactly
   *  that row.
   *
   *  The button itself is the shared `components/FavHeart`, bound to the track
   *  this view is showing: the same writer, the same optimistic update and the
   *  same `aria-pressed` the player bar's hearts and every library row use (it
   *  used to be a fourth copy of the button, reading a `liked` prop the bar
   *  passed down and writing likes its own way). What stays local is the INK:
   *  the fullscreen chrome draws its unlit controls in `ink.chromeButton`, not
   *  in zinc, because this row sits straight on the artwork. */
  const likeButton = (className: string) => (
    <FavHeart
      kind="track"
      id={p.current.path}
      mbid={freshTags?.MUSICBRAINZ_TRACKID}
      boxClass="tap-hit rounded-lg transition-colors"
      unlikedClass={ink.chromeButton}
      iconClass="h-[18px] w-[18px]"
      likeLabels
      className={className}
      {...likeToasts(() => title)}
    />
  );

  const transportRow = (
    /* `cursor-auto`: the transport row keeps the arrow while the pane is
       idling — see the idle-cursor effect.

       ONE line at EVERY width (R267). The row used to be `flex-wrap`, and at
       390 px its last control — the add-to-playlist button — was pushed onto a
       line of its own, so it read as a stray icon under the transport (the
       owner's report: "the playlist button seems placed weirdly"). Nothing
       wraps now: the gaps and the hit boxes tighten below `sm`
       (`gap-1` / `p-1.5` against `sm:gap-2.5` / `sm:p-2`) so the whole row —
       shuffle, previous, play, next, repeat, the speed button, the divider,
       the favourite and add-to-playlist — fits a 360 px phone with room to
       spare, and every control on it keeps its own full-height hit box. */
    <div className={`cursor-auto flex items-center justify-center gap-1 sm:gap-2.5 ${ink.shade}`}>
      <button aria-label="Shuffle" aria-pressed={p.shuffle} className={`p-1.5 sm:p-2 rounded-lg transition-colors ${p.shuffle ? "text-accent" : ink.chromeButton}`} onClick={p.onToggleShuffle} title="Shuffle">
        <Shuffle className="h-4 w-4" />
      </button>
      <button aria-label="Previous track" className={`p-2 sm:p-2.5 rounded-lg transition-colors ${ink.chromeStrong} hover:bg-black/5`} onClick={() => p.onStep(-1)} title="Previous track">
        <SkipBack className="h-5 w-5" />
      </button>
      <button
        aria-label={p.playing ? "Pause" : "Play"}
        aria-pressed={p.playing}
        className="p-3.5 sm:p-4 rounded-lg bg-accent on-accent hover:bg-accent-soft shadow-lg transition-colors"
        onClick={p.onTogglePlay}
        title="Play / pause (Space)"
      >
        {p.playing ? <Pause className="h-6 w-6" /> : <Play className="h-6 w-6 ml-0.5" />}
      </button>
      <button aria-label="Next track" className={`p-2 sm:p-2.5 rounded-lg transition-colors ${ink.chromeStrong} hover:bg-black/5`} onClick={() => p.onStep(1)} title="Next track">
        <SkipForward className="h-5 w-5" />
      </button>
      <button aria-label="Repeat one" aria-pressed={p.loop} className={`p-1.5 sm:p-2 rounded-lg transition-colors ${p.loop ? "text-accent" : ink.chromeButton}`} onClick={p.onToggleLoop} title="Repeat one">
        <Repeat className="h-4 w-4" />
      </button>
      <button
        className={`p-1.5 sm:p-2 rounded-lg transition-colors text-xs font-mono min-w-[38px] sm:min-w-[46px] ${ink.chromeButton}`}
        onClick={() => p.onSpeedChange(nextSpeed(p.speed, 1))}
        title="Playback speed — [ slower · ] faster · 0 reset to 1×"
      >
        {fmtSpeed(p.speed)}
      </button>
      {/* the divider belongs to the row's CONTROL line, not the row box:
          self-center + a fixed height keep it on the same axis as the icons
          either side of it, whatever heights they have */}
      <span className="w-px h-6 bg-white/15 mx-0.5 sm:mx-1 self-center shrink-0" />
      {/* The favourite's ONE home now, at every width (R267): it used to be a
          lone row pinned to the bottom-left of the player below `lg`, drawn
          outside the scrolling body — which is where a reader had to go
          looking for it, and where the owner could not find it ("also where is
          the like button?"). It sits on the MAIN control line instead, beside
          add-to-playlist and the divider that separates the transport from the
          track actions, so the one place the controls live is the place the
          favourite lives. `FavHeart` is untouched: same key, same writer, same
          `aria-pressed`; the app's star stays what it always was — a rating,
          not a favourite (see lib/ratings). */}
      {likeButton("p-1.5 sm:p-2 inline-flex items-center justify-center")}
      <div className="relative">
        <button
          aria-label="Add this track to a playlist"
          aria-expanded={plOpen}
          className={`p-1.5 sm:p-2 rounded-lg transition-colors ${plOpen ? "text-accent" : ink.chromeButton}`}
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
    /* `cursor-auto`: seek bar and volume keep the arrow while the pane is
       idling — the scrubber's hover preview is the moment the arrow is needed
       most, see the idle-cursor effect. */
    <div className={`cursor-auto flex items-center gap-2 text-xs w-[26rem] max-w-full px-2 ${ink.shade} ${ink.chromeText}`}>
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

  // The lyrics button's own wording and lit state — ONE of each, on every
  // width. It used to read the width twice (it was the compact header's
  // expand / collapse control below `md`), which is exactly how "lyrics on"
  // came to mean the pane on a desktop and the whole block on a phone.
  const lyricsTitle = "Toggle the lyrics pane";
  // What the control lights on is the reader's pane pick itself. The button is
  // drawn only for a track that HAS lyrics (below), so on screen `lyricsOn` and
  // `paneOpen` agree; reading the state keeps `aria-pressed` honest about what
  // the press just did.
  const lyricsOn = showLyrics;
  // Who owns the arrow in this mode: over a music video the chrome already
  // carries it away (the older rule above), in the audio pane it is dropped on
  // idle (the effect above). Either way it is ONE class on this root.
  const cursorHidden = videoPath ? !chromeVisible : idleCursor;
  /** The sliders' ink as CSS, for the one subtree that draws them (see
   *  LyricInk's `seek*`). Not the video path: that chrome sits on the player's
   *  own black gradient rather than on the cover's ambience, so it keeps the
   *  fixed dark track and white thumb index.css defaults to — the ink table
   *  answers to the ARTWORK, and there is no artwork behind those controls. */
  const seekInk = videoPath
    ? undefined
    : ({
        "--seek-track": ink.seekTrack,
        "--seek-fill": ink.seekFill,
        "--seek-thumb": ink.seekThumb,
        "--seek-ring": ink.seekRing,
      } as CSSProperties);
  /** The top bar's one icon pair: engaged / idle. Over the artwork it is the
   *  ink table's own pair (see the bar's comment); over a VIDEO the idle half
   *  stays the fixed `text-zinc-300` that bar has always used, because that
   *  chrome sits on the player's black gradient rather than on the cover's
   *  ambience and the table has nothing to say about a field it is not on (a
   *  bright album cover must not paint near-black glyphs onto it). */
  const barOn = videoPath ? "text-accent hover:bg-white/10" : ink.chromeOn;
  const barOff = videoPath ? "text-current hover:text-white hover:bg-white/10" : ink.chromeOff;

  return (
    /* No polarity tint on the root anymore: the whole player draws its text on
       the ambience through the wash below, and the only surfaces that still
       frost anything are the floating menus, which pin their own tint
       (`np-veil-dark np-veil-panel`, index.css). */
    <div className={`fixed inset-0 z-50 overflow-clip ${videoPath ? "bg-transparent" : "bg-zinc-950"} ${cursorHidden ? "cursor-none" : ""}`}>
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
          (z-10) and queue drawer (z-20) stay above the bar.
          `style` carries the slider ink (LyricInk's `seek*`) down to the
          seek row and the volume cluster: the seek bar and the volume bar are
          the two controls that paint a track rather than a glyph, and a
          slider tinted from a fixed zinc while the label above it flips with
          the cover is the same grey-on-grey bug twice. The whole subtree gets
          it, so a future slider in here is covered by the same rule. */}
      <div
        className={`relative z-[2] h-full flex flex-col ${videoPath ? "pointer-events-none" : ""}`}
        style={seekInk}
      >
        {/* top bar — exit button top-left, queue/options cluster top-right;
            eases away with the bottom overlay while the video plays.
            `safe-np-top` carries the base padding AND the notch/status-bar
            inset: the overlay is `fixed inset-0`, so without it the system
            clock, the queue readout and the options button share one line.
            `cursor-auto` is this row opting back OUT of the pane's idle
            `cursor-none`: a row of controls is never idle, so the arrow stays
            for as long as it is over one (see the idle-cursor effect). Over a
            video the class is inert — that bar is pointer-events-none while
            the chrome is away, so it cannot resurrect the arrow.

            Every ICON button in this bar draws its two states from ONE pair
            of ink classes — `chromeOn` while it is engaged, `chromeOff` while
            it is not — and every one of them is the same 36 px box around a
            20 px glyph with the row's `gap-1`. That is the whole point of the
            pair: the toggles already used it, while the three plain buttons
            beside them spelled out `text-current hover:text-white` (full
            ink), the fullscreen button an `text-accent`, and the settings
            button an open-state `text-white bg-white/10`. Measured across the
            row that is three different brightnesses on one bar — the owner's
            "one much brighter, one dimmer than its neighbours" — and the
            `hover:text-white` in the old ones was a fixed white that the
            LIGHT table could not honour at all. `chromeOff` is one dimmed
            ink, `chromeOn` is the accent (or, on the light table, full ink,
            because the default accent IS white), hover takes either back to
            full. The queue readout beside them is a READOUT, not a control:
            it keeps the row's own `chromeText`, like the time readouts. */}
        <div
          className={`safe-np-top cursor-auto flex items-center justify-between transition-[opacity,transform] duration-300 ease-out ${
            videoPath ? (chromeVisible ? "pointer-events-auto" : "pointer-events-none opacity-0 -translate-y-3") : ""
          } ${videoPath ? "text-zinc-300" : `${ink.shade} ${ink.chromeText}`}`}
        >
          <button
            className={`p-2 rounded-lg transition-colors ${barOff}`}
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
                Always on the bar: inert when nothing is queued, and then it
                wears the family's idle ink rather than an opacity of its own
                (`pointer-events-none` already makes the hover half inert). */}
            <button
              className={`max-w-[15rem] min-w-0 items-center gap-1.5 px-1.5 py-1 rounded-md text-[10px] font-mono hidden sm:flex ${
                queueOpen ? barOn : barOff
              } ${upNextLabel ? "transition-colors" : "pointer-events-none"}`}
              onClick={() => setQueueOpen(true)}
              title={upNextLabel ? `Up next — ${upNextLabel} · click to view the queue` : "Up next — nothing queued"}
            >
              <span className="uppercase tracking-widest opacity-70 shrink-0">Up next</span>
              <span className="truncate">{upNextLabel || "—"}</span>
            </button>
            <button
              ref={queueTriggerRef}
              className={`p-2 rounded-lg transition-colors ${queueOpen ? barOn : barOff}`}
              onClick={() => setQueueOpen(!queueOpen)}
              title="Up next (queue)"
              aria-label="Up next queue"
              aria-expanded={queueOpen}
            >
              <ListMusic className="h-5 w-5" />
            </button>
            {/* inert over a music video: <Visualizer> only renders in the
                audio layout, so the toggle is hidden rather than a no-op.
                Its two states come from the bar's own pair (`barOn` /
                `barOff` — the ink table's `chromeOn` / `chromeOff`): a bare
                `text-accent` on/off pair is what made both of these read as
                neither (the table says why). */}
            {!videoPath && (
              <button
                className={`p-2 rounded-lg transition-colors ${viz ? barOn : barOff}`}
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
                accident of the layout instead of something you own.

                ONE press, ONE thing, at EVERY width: it writes the reader's
                pane pick (`showLyrics`, persisted), and `paneOpen` is the one
                derivation both layouts read. Below `md` it used to flip the
                compact MODE instead and write nothing — so a phone could not
                show lyrics without also unfolding the whole block, the
                persisted pick was ignored there, and the offset / zoom
                controls the pane carries never mounted on a phone at all
                (the owner's report). The phone's compact header is now the
                LYRICS composition alone (`npLyricsMode`), never a second owner
                of this pane, so the press does the same thing in both layouts
                and the pick follows the reader between them.

                Drawn exactly where the pane CAN be filled — timed lyrics, or
                untimed ones this install accepts (`npLyricsMode.drawable`) —
                which is the same rule the pane below is drawn under, and the
                one the desktop always used. An instrumental, a lyric-less
                track and a plain text under `lyrics_allow_plain` off wear no
                button at all (the refused text wears the red-cross mark in
                the metadata block instead), so no state of this control ever
                promises words the pane cannot show. It also keeps its OWN
                state: the pick is the reader's, persisted across tracks, and
                what follows the track is the pane. */}
            {!videoPath && lyricsDrawable && (
              <button
                className={`p-2 rounded-lg transition-colors ${lyricsOn ? barOn : barOff}`}
                onClick={() => {
                  const v = !showLyrics;
                  setShowLyrics(v);
                  persist(LYRICS_KEY, v ? "1" : "0");
                }}
                title={lyricsTitle}
                aria-label={lyricsTitle}
                aria-pressed={lyricsOn}
              >
                <Mic2 className="h-5 w-5" />
              </button>
            )}
            <div className="relative">
              <button
                className={`p-2 rounded-lg transition-colors ${nativeFs ? barOn : barOff}`}
                onClick={toggleNativeFs}
                title={nativeFs ? "Leave browser fullscreen (Esc)" : "Browser fullscreen — hide the browser's own chrome"}
                aria-label={nativeFs ? "Leave browser fullscreen" : "Enter browser fullscreen"}
                aria-pressed={nativeFs}
              >
                {nativeFs ? <Minimize2 className="h-5 w-5" /> : <Maximize2 className="h-5 w-5" />}
              </button>
            </div>
            <div className="relative">
              <button
                className={`p-2 rounded-lg transition-colors ${options ? barOn : barOff}`}
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
              layout if anyone ever uses the player that small.
              `my-auto` while the pane is away: with no words this column IS
              the composition (cover, metadata, transport, seek), so it takes
              the free space as margins instead of hugging the top of a phone
              screen. Auto margins and not `justify-center` on the body on
              purpose — they resolve to zero when there is no free space, so a
              window shorter than the composition (a landscape phone) scrolls
              from its own top instead of hiding the cover above the
              scrollport. */}
          <div
            className={`w-full flex flex-col items-center justify-center gap-4 shrink-0 min-w-0 transition-[width] duration-300 ease-out ${
              paneOpen
                ? "lg:w-[42%] lg:h-full lg:max-h-full lg:min-h-0 lg:overflow-y-auto"
                : "my-auto max-h-full min-h-0 overflow-x-clip overflow-y-auto"
            }`}
          >
            {/* the phone's compact header — the whole top block as ONE row,
                drawn only while the lyrics are up (the note on `npLyricsMode`'s
                `compactHeader` carries the report and the rule). `md:hidden`
                as well as the flag: this must never render at md and up, and
                the block below must never show below md while it does. Fixed
                row heights like the block's own rows, so a track change cannot
                make the header jump. */}
            {compactHeader && (
              <div className="md:hidden w-[26rem] max-w-full min-w-0 flex items-center gap-3 px-1">
                <CoverImg
                  albumPath={p.current.albumPath}
                  coverFile={coverFile}
                  wrapperClass="shrink-0 w-12 h-12 rounded-lg shadow-lg bg-raise overflow-hidden"
                />
                {/* min-w-0 + truncate down the whole chain, and the readout is
                    the ONE box allowed to keep its width: at 390px a long
                    title has to give way, but a clipped format readout is the
                    report this surface already has a history of. */}
                <div className="flex-1 min-w-0">
                  <div className={`h-6 flex items-center gap-2 min-w-0 ${ink.shade}`} title={title}>
                    {/* This header keeps the truncating spans it has always
                        used — the drifting line belongs to the block layout,
                        where the title has one — so the three lines become
                        links over those same spans (see MetaLink). */}
                    <MetaLink href={trackHref} text={title} className={`text-base font-bold ${ink.active}`} scroll={false} onOpen={p.onClose} />
                    {techStr && (
                      <span className={`shrink-0 text-[11px] font-mono ${ink.dim}`} title={techTip || undefined}>
                        {techStr}
                      </span>
                    )}
                  </div>
                  <div
                    className={`h-5 flex items-center gap-1 min-w-0 ${ink.shade}`}
                    title={[albumLine, artistLine].filter(Boolean).join(" · ")}
                  >
                    <MetaLink href={albumHref} text={albumLine} className={`text-xs ${ink.dim}`} scroll={false} onOpen={p.onClose} />
                    {albumLine && artistLine ? <span className={`shrink-0 text-xs ${ink.dim}`}>·</span> : null}
                    {artistLine ? <MetaLink href={artistHref} text={artistLine} className={`text-xs ${ink.dim}`} scroll={false} onOpen={p.onClose} /> : null}
                  </div>
                </div>
              </div>
            )}
            <div className={`relative ${compactHeader ? "hidden md:block" : ""}`}>
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
                // beside it so the size is the same either way. Below `md` the
                // art is drawn ONLY when no pane is up — with lyrics the
                // compact header takes its place, and without them (`w-72`,
                // 288 px) the cover is the phone composition's centrepiece, so
                // a track with no lyrics is not a bare strip on a whole screen.
                wrapperClass={`relative rounded-2xl shadow-2xl bg-raise overflow-hidden lg:w-[min(28rem,48vh)] lg:h-[min(28rem,48vh)] ${
                  paneOpen ? "w-32 h-32" : "w-72 h-72"
                }`}
              />
            </div>
            {/* the block the compact header stands in for; `hidden md:block`
                keeps the desktop copy on screen even if the width state is a
                beat behind a resize */}
            {compactHeader ? <div className="hidden md:block">{textBlock}</div> : textBlock}
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
                <Visualizer playing={p.playing} className="block h-12 w-full" ink={ink.viz} />
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
          {/* lyrics column — plain, no panel, hugging the right edge; flex-1
…
              Below `md` this column is the PHONE's reading surface: the
              compact header (`compactHeader`) is a 48 px row plus the
              transport, the seek line and the visualizer, and everything left
              under it belongs to the pane — `flex-1` with `min-h-0`, so it
              scrolls inside the body rather than pushing the chrome off the
              screen, and it stays inside `safe-np-body` so the notch /
              home-indicator insets below `lg` apply to it exactly as they do
              to the desktop column. The offset / zoom controls ride its bottom
              edge there, which is the pair the owner's report asked for and the
              reason the pane has to exist on a phone at all.

              Drawn only where there are words this install will show
              (`npLyricsMode.drawable` — timed, or untimed and accepted): an
              instrumental, a lyric-less track or a refused plain text draws no
              column at all, in either layout, and the phone falls back to its
              full composition instead of a header row with nothing under it.

              Shown and hidden by the toggle in the top bar, and the BOX is the
              same one in both states — only its size changes. Collapsing
              (w-0 / max-h-0 / overflow-clip) is what makes it take no room, so
              the cover column can widen to the whole row and the art glides
              back to the middle over 300 ms instead of snapping; the scroll
              container itself is never removed, so the reader's scroll
              position, the zoom and the auto-follow refs all survive a toggle.
              The lyric lines are divs, not controls, so hiding the subtree
              from assistive tech costs no tab stop. */}
          {!videoPath && lyricsDrawable && (
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
                className={`relative flex-1 min-h-0 overflow-y-auto overscroll-contain px-6 py-5 no-scrollbar lyr-fade ${ink.shade} transition-opacity duration-300 ${
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
              {/* The two lyric controls, ON the lyrics and SUBTLE. Both used
                  to live only in the options popover, so nudging the sync or
                  fitting the size to the room meant leaving the words to go
                  and find them — and neither is a thing a listener should have
                  to hunt for while the song plays. Chrome, not a panel: no
                  background, no border, the ink's own tone (it brightens on
                  hover, and it fades with the pane's own stale state because
                  it rides the same surface), so it reads as part of the words
                  rather than a control strip pasted over the artwork (R52c).
                  Rendered only while the pane is OPEN: collapsed, there is
                  nothing on screen to size or to shift.

                  The ink is the table's FULL strength (`ink.chromeStrong`) and
                  the row rests at it: the controls inside dim their own glyphs
                  (0.7 on the step buttons, 0.8 on the value), and that is the
                  whole of the "subtle" this strip needs. It used to be the
                  muted `ink.chromeText` at 0.6, and on the light table that is
                  a translucent near-black at 0.75 × 0.6 — 0.45 alpha, 2.9:1 on
                  the white cover — and 0.42 alpha after the glyph dim, which
                  is the "they blend into the background" report. The row's own
                  rest opacity is 1 now, so nothing multiplies on top of the
                  values: measured at 11.6:1 on the dark cover and 6.4:1 on the
                  white one, from 2.4:1 and 1.9:1 before
                  (tools/check_np_metadata_contrast.cjs asserts both per cover).
                  The stale fade stays where it was: that is a state, not the
                  resting tone. */}
              {paneOpen && (
                <div className={`shrink-0 flex items-center justify-end gap-4 px-6 pb-2 pt-1 text-[11px] transition-opacity duration-300 ${ink.shade} ${ink.chromeStrong} ${
                  staleLyrics ? "opacity-40" : "opacity-100"
                }`}>
                  <LyricZoom
                    pct={Math.round((lyricZoom / LYRIC_ZOOM_BASE) * 100)}
                    onChange={(p) => {
                      const next = (p / 100) * LYRIC_ZOOM_BASE;
                      setLyricZoom(next);
                      persist(ZOOM_KEY, String(next));
                    }}
                  />
                  <LyricOffset
                    path={p.current.path}
                    ms={offsetMs}
                    onChange={setOffsetMs}
                    onSaved={(lrc) => setLyricsText(lrc)}
                  />
                </div>
              )}
            </div>
          )}
        </div>

        </>
        )}
      </div>

      {/* up-next queue drawer — same features as the player bar's queue
          popover: CLEAR upcoming, per-track ✕, drag to reorder.
          It sits above the player (`z-20` against the chrome's `z-[2]`), and a
          press anywhere else closes it — the rule the nav drawer and every
          Popover already follow. It was the one menu in the app that ignored a
          press outside itself, so on a phone the drawer stayed over the player
          until its own cross was found. The shield is between the two layers,
          so the rows keep their drag/press targets. */}
      {queueOpen && (
        <>
          <div className="absolute inset-0 z-[15]" onClick={() => setQueueOpen(false)} />
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
        </>
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
