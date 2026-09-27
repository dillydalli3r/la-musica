/** Shared lyrics-pane scrolling for the sidebar, the fullscreen player and
 * the editor preview.
 *
 * Centering uses ONLY offset layout values (offsetTop / offsetHeight /
 * clientHeight) — never getBoundingClientRect. The fullscreen pane scales
 * its content with CSS `zoom`, and inactive lines carry a transform scale;
 * gBCR reports visual pixels while scrollTop is written in layout units, so
 * mixing the two landed every scroll target off by the zoom factor. Offset
 * math is blind to both, which also makes the centering independent of
 * screen / pane size.
 *
 * The glide is a retargetable rAF ease instead of scrollTo({smooth}): a
 * native smooth scroll interrupted by the next line change restarts its
 * animation and reads as a jerk; this loop simply re-aims each frame, so
 * any number of rapid retargets stays one continuous motion. It also keeps
 * the scroll inside the pane — scrollIntoView walks EVERY scrollable
 * ancestor and yanks the surrounding page along. */

import { useCallback, useEffect, useRef, useState, type RefObject } from "react";

/** Which way a retarget lands. `snap` = the pane arrives in the same frame;
 *  `glide` = the rAF ease carries it there. */
export type LyricMove = "glide" | "snap";

export interface LyricsGlider {
  /** The scroller this glider drives. */
  readonly el: HTMLElement;
  /** Bring `el` to the pane's lyric anchor line. `"snap"` lands in the same
   *  frame — a scrub, an offset / zoom change, anything the reader did that is
   *  a jump; `"glide"` (the default) carries there with the shared ease — the
   *  clock's own advance to the next line, and a press on a line. */
  center(el: HTMLElement, move?: LyricMove): void;
  /** Stop gliding and adopt the current position as resting (user took
   * over the pane with the wheel / touch). */
  stop(): void;
  /** Stop for good — the pane is going away. */
  destroy(): void;
}

/** Where the currently-sung line sits vertically, as a fraction of the
 * pane height: the upper third — ahead of the reader's eye, with the
 * upcoming lines filling the space below. */
export const LYRICS_ANCHOR = 0.33;

/** Leading / trailing space a pane needs for the first and last line to
 * reach the anchor line. Without it the final lines have nowhere to go and
 * the pane reads as frozen for the whole outro. */
export const LYRICS_PAD_TOP = `${Math.round(LYRICS_ANCHOR * 1000) / 10}%`;
export const LYRICS_PAD_BOTTOM = `${Math.round((1 - LYRICS_ANCHOR) * 1000) / 10}%`;

/** Fraction of the remaining distance covered per 60 Hz frame. */
const EASE = 0.16;
const FRAME_MS = 1000 / 60;

export function createLyricsGlider(c: HTMLElement, anchor: number = LYRICS_ANCHOR): LyricsGlider {
  let target = c.scrollTop;
  let raf = 0;
  let active = false;
  let dead = false;
  let last = 0;
  // Reduced motion: the pane still follows, it just arrives instead of
  // travelling.
  const instant = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;

  const step = () => {
    // A detached or collapsed pane pins scrollTop to 0, so the target is
    // unreachable and the loop would spin a frame callback forever.
    if (dead || !c.isConnected || c.scrollHeight === 0) {
      active = false;
      return;
    }
    // Wall clock, never the frame timestamp: a throttled pane (background tab,
    // hidden window) can hand out stalled or repeated timestamps, and a
    // negative delta clamps to a 1 ms step — the glide then crawls forever
    // instead of arriving.
    const now = performance.now();
    const dt = Math.min(64, Math.max(1, now - last));
    last = now;
    const diff = target - c.scrollTop;
    if (Math.abs(diff) < 0.5) {
      c.scrollTop = target;
      active = false;
      return;
    }
    // dt-normalised: the same 0.16-per-frame feel on a 144 Hz display as on
    // a 60 Hz one (a fixed step glided 2.4x faster on high-refresh screens).
    c.scrollTop += diff * (1 - Math.pow(1 - EASE, dt / FRAME_MS));
    raf = requestAnimationFrame(step);
  };

  return {
    el: c,
    center(el, move = "glide") {
      // Walk the offsetParent chain up to the scroller (it is the nearest
      // positioned ancestor — every pane marks it `relative`).
      let top = 0;
      let n: HTMLElement | null = el;
      while (n && n !== c) {
        top += n.offsetTop;
        n = n.offsetParent as HTMLElement | null;
      }
      if (n !== c) return; // el is not inside the scroller — refuse to guess
      const maxTop = Math.max(0, c.scrollHeight - c.clientHeight);
      // Anchor: the line's vertical middle lands at `anchor` × height
      // (the upper third) instead of the pane's exact middle.
      target = Math.min(maxTop, Math.max(0, top + el.offsetHeight / 2 - c.clientHeight * anchor));
      // A glide in flight needs NO cancel: the pending frame reads `target`
      // when it runs, so re-aiming is all a retarget takes. Cancelling here
      // (and then skipping the restart because the loop is already `active`)
      // killed the loop outright — the pane froze mid-flight on the next line
      // change and stayed frozen for the rest of the track.
      //
      // A SNAP has to cancel: the reader's own move must not be carried on by
      // whatever frame was already in flight towards the old line.
      if (move === "snap" || instant) {
        cancelAnimationFrame(raf);
        c.scrollTop = target;
        active = false;
        return;
      }
      if (!active) {
        active = true;
        last = performance.now();
        raf = requestAnimationFrame(step);
      }
    },
    stop() {
      cancelAnimationFrame(raf);
      active = false;
      target = c.scrollTop;
    },
    destroy() {
      dead = true;
      cancelAnimationFrame(raf);
      active = false;
    },
  };
}

/** How long an explicit wheel / touch keeps the pane in the reader's hands
 * before following picks back up.
 *
 * Short on purpose. The 6 s this started as made the pane look BROKEN: a
 * reader who nudged the wheel and then waited watched the song's line change
 * three times while the pane sat still (the hold also suppressed the
 * line-change step, so nothing moved at all). A wheel's own momentum is a few
 * hundred milliseconds and a finger drag re-arms this on every event, so a
 * little over a second is enough to never fight a gesture in progress — and
 * short enough that the pane feels alive again the moment the reader stops.
 * The line-change step and the resume-after-pause kick both go through the same
 * clock, so a long line still picks itself up without waiting for the next one. */
const HOLD_MS = 1200;
/** A clock jump this large between frames can only be a seek. */
const SEEK_JUMP = 1.2;
/** How long a reader-made move keeps the pane SNAPPING — the line's own
 *  emphasis drops its transition for the window (`snapping`, which the
 *  surfaces render with), so the highlight lands at once instead of easing
 *  across the lines between the one left behind and the one arrived at. It
 *  covers the re-parse the offset buttons trigger and the re-sync a seek
 *  causes: skipping to a line used to show the transit from the line before
 *  it.
 *
 *  The PANE's half of the move is not decided here: a scrub still lands on its
 *  target in the same frame (`lyricMove` below), while a press on a line —
 *  the one move where the reader asked to be BROUGHT somewhere — glides
 *  (`centerLine`).
 *
 *  Long enough to span the commit that carries the move (a starved renderer
 *  commits late) and the emphasis' own `duration-motion-slow`, short enough
 *  that the next line the CLOCK advances to animates again. */
export const LYRIC_JUMP_MS = 600;
/** How long after a press its own seek is still recognised as that press.
 *  `centerLine` runs in the click handler and the clock jump it caused arrives
 *  with the next commit; anything past a few hundred ms later is a seek the
 *  reader made elsewhere (the scrub bar) and is treated as one. */
const PRESS_SEEK_MS = 250;

/** Which way a retarget lands: `snap` for the reader moves that are a jump —
 *  an offset step, a zoom change, a scrub — and `glide` for the clock's own
 *  advance to the next line and for a press on a line (whose move `centerLine`
 *  passes explicitly). `jumpAt` is the timestamp of the last SNAPPING
 *  reader-made move (0 = none this session). Pure on purpose: the rule is the
 *  behaviour the owner reported, so tools/check_lyrscroll.cjs pins both halves
 *  of it without a DOM. */
export function lyricMove(now: number, jumpAt: number, window = LYRIC_JUMP_MS): LyricMove {
  return now - jumpAt < window ? "snap" : "glide";
}

export interface LyricsFollowOptions {
  /** First line of the active cluster (row index); -1 = nothing sung yet. */
  active: number;
  /** Playback position — read only to spot seeks. */
  time: number;
  playing: boolean;
  /** The scroll pane. */
  scroll: RefObject<HTMLElement | null>;
  /** Primary TEXT element of each row. Rows carrying translation /
  * transliteration sub-lines must register the text line, not the block, or
  * the sub-line pushes the sung line off the anchor. */
  rows: RefObject<Record<number, HTMLElement | null>>;
  /** Rewind the pane when this changes — the track path. */
  reset?: string | null;
  /** Vertical anchor, as a fraction of the pane height. */
  anchor?: number;
}

/** Auto-follow for one lyrics pane: keeps the sung line on the anchor line,
 * snaps on the reader moves that are jumps (a scrub, an offset step, a zoom
 * change), carries the reader on a press, glides on the clock's own line
 * steps, and gets out of the reader's way for `HOLD_MS` after a wheel / touch.
 * Shared by every pane so they can't drift apart. */
export function useLyricsFollow({
  active, time, playing, scroll, rows, reset = null, anchor = LYRICS_ANCHOR,
}: LyricsFollowOptions): {
  /** Centre row `i` (a press on a line, or a pane re-opening). Outranks a
   *  reader hold, and the emphasis lands with the call either way; `move`
   *  decides the PANE's half — the default "snap" for a re-open that must not
   *  drift, "glide" for a press, which is a request to be brought there. */
  centerLine: (i: number, move?: LyricMove) => void;
  /** The reader moved the words themselves without naming a line — an offset
   * step, a zoom change, a seek. The pane re-centres on the sung line
   * instantly instead of gliding to it. */
  jump: () => void;
  /** True for `LYRIC_JUMP_MS` after any of the above: the surfaces drop their
   * line transition for the window, so the emphasis lands with the words
   * rather than easing across them. */
  snapping: boolean;
  /** The reader wheeled / touched the pane. */
  takeOver: () => void;
} {
  const glider = useRef<LyricsGlider | null>(null);
  const holdUntil = useRef(0);
  const holdTimer = useRef(0);
  const jumpAt = useRef(0);
  // When the pane last started carrying the reader to a line they pressed
  // (`centerLine(i, "glide")`) — the seek that press causes must not be read
  // as a scrub (see the seek effect below).
  const pressAt = useRef(0);
  const snapTimer = useRef(0);
  const prevTime = useRef(-1);
  const playingRef = useRef(playing);
  const [kick, setKick] = useState(0);
  const [snapping, setSnapping] = useState(false);

  // One glider per pane ELEMENT, made on demand: the pane unmounts and
  // remounts around lyrics presence and video mode, and a glider left
  // holding the old node would keep driving a detached scroller.
  const forPane = useCallback(() => {
    const c = scroll.current;
    if (!c) return null;
    if (!glider.current || glider.current.el !== c) {
      glider.current?.destroy();
      glider.current = createLyricsGlider(c, anchor);
    }
    return glider.current;
  }, [scroll, anchor]);

  const releaseHold = useCallback(() => {
    holdUntil.current = 0;
    window.clearTimeout(holdTimer.current);
  }, []);

  // Every reader-made move goes through here: it marks the jump window the
  // retarget rule reads (`lyricMove`), holds the emphasis transition off for
  // the same window, and kicks the follow effect so the pane re-centres even
  // when the active line itself did not change (an offset step inside one
  // line, a zoom change).
  //
  // `snapMove` is false for a press on a line, whose pane move is a glide: the
  // emphasis still snaps (that is the half a jump always wants), but nothing
  // carries a time stamp into the retarget rule, so the follow effect a press
  // also kicks glides to the same line instead of landing on it.
  const markJump = useCallback((snapMove = true) => {
    if (snapMove) jumpAt.current = Date.now();
    releaseHold();
    setSnapping(true);
    window.clearTimeout(snapTimer.current);
    snapTimer.current = window.setTimeout(() => setSnapping(false), LYRIC_JUMP_MS);
    setKick((k) => k + 1);
  }, [releaseHold]);

  const takeOver = useCallback(() => {
    forPane()?.stop();
    holdUntil.current = Date.now() + HOLD_MS;
    window.clearTimeout(holdTimer.current);
    holdTimer.current = window.setTimeout(() => {
      // The reader stopped scrolling and the song kept going: pick the pane
      // back up instead of leaving it parked until the next line change.
      if (playingRef.current) setKick((k) => k + 1);
    }, HOLD_MS + 50);
  }, [forPane]);

  const centerLine = useCallback((i: number, move: LyricMove = "snap") => {
    const el = rows.current[i];
    if (!el) return;
    // A press on a line is the one reader-made move that CARRIES the reader:
    // the emphasis lands at once (`snapping`, so nothing eases across the
    // lines between), and the pane glides there instead of teleporting.
    // `pressAt` marks that jump for the seek effect below — a seek it can see
    // but did not ask for is a scrub, and a scrub snaps.
    if (move === "glide") pressAt.current = Date.now();
    markJump(move === "snap");
    forPane()?.center(el, move);
  }, [rows, forPane, markJump]);

  // New track: rewind, and drop any hold or glide left over from the one
  // that just ended. Declared before the follow effect so the rewind wins
  // the commit.
  useEffect(() => {
    glider.current?.stop();
    if (scroll.current) scroll.current.scrollTop = 0;
    holdUntil.current = 0;
    window.clearTimeout(holdTimer.current);
    prevTime.current = -1;
  }, [reset, scroll]);

  // Unmount: a pane that disappears mid-glide can never reach its target, and
  // a pending hold timer would fire into a dead component.
  useEffect(() => () => {
    glider.current?.destroy();
    glider.current = null;
    window.clearTimeout(holdTimer.current);
    window.clearTimeout(snapTimer.current);
  }, []);

  // Seek: the clock jumped by more than a line, so the reader did it (the
  // scrub bar, or a seek from anywhere else in the app) — the re-sync that
  // follows is a jump, not a line step. Re-centre even when the jump lands
  // inside the line that was already active, which the active-line effect
  // below would never see.
  //
  // A press on a line seeks too, and its own centerLine already did all of
  // that; the only thing left for this effect to do would be to snap the pane
  // out from under the glide it just started, which is the teleport this
  // guard exists to stop.
  useEffect(() => {
    const prev = prevTime.current;
    prevTime.current = time;
    if (prev < 0 || Math.abs(time - prev) <= SEEK_JUMP) return;
    if (Date.now() - pressAt.current < PRESS_SEEK_MS) return;
    markJump();
  }, [time, markJump]);

  // Play resumes: the pane was left wherever the reader parked it, and the
  // rest of the line can be half a minute long — waiting for the NEXT line
  // change is what made following look dead.
  useEffect(() => {
    const was = playingRef.current;
    playingRef.current = playing;
    if (playing && !was) {
      releaseHold();
      setKick((k) => k + 1);
    }
  }, [playing, releaseHold]);

  useEffect(() => {
    if (active < 0 || Date.now() < holdUntil.current) return;
    const el = rows.current[active];
    if (!el) return;
    forPane()?.center(el, lyricMove(Date.now(), jumpAt.current));
    // Deps: the active line and the kicks only. Never the clock — the pane
    // moves one step per line, not one per frame.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, kick, forPane, rows]);

  return { centerLine, jump: markJump, snapping, takeOver };
}
