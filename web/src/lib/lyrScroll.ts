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

export interface LyricsGlider {
  /** The scroller this glider drives. */
  readonly el: HTMLElement;
  /** Bring `el` to the pane's lyric anchor line. `snap` jumps immediately
   * (seeks, track changes); default glides from here. */
  center(el: HTMLElement, snap?: boolean): void;
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
    center(el, snap = false) {
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
      if (snap || instant) {
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
 * before following picks back up (native players do the same — being yanked
 * back mid-read is what reads as "auto-scroll is broken"). */
const HOLD_MS = 6000;
/** A clock jump this large between frames can only be a seek. */
const SEEK_JUMP = 1.2;
/** Window around a seek in which a move SNAPS instead of gliding (scrubbing
 * should land where you dropped the needle, not sail there). */
const SEEK_SNAP = 600;
/** Window after a click-to-seek in which moves still glide — navigating by
 * lyric line stays animated, and the click outranks its own clock jump. */
const CLICK_GLIDE = 1500;

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
 * snaps on seeks, glides on line steps, and gets out of the reader's way
 * for `HOLD_MS` after a wheel / touch. Shared by every pane so they can't
 * drift apart. */
export function useLyricsFollow({
  active, time, playing, scroll, rows, reset = null, anchor = LYRICS_ANCHOR,
}: LyricsFollowOptions): {
  /** Centre row `i` right now (click-to-seek) — outranks a reader hold. */
  centerLine: (i: number) => void;
  /** The reader wheeled / touched the pane. */
  takeOver: () => void;
} {
  const glider = useRef<LyricsGlider | null>(null);
  const holdUntil = useRef(0);
  const holdTimer = useRef(0);
  const seekAt = useRef(0);
  const glideAt = useRef(0);
  const prevTime = useRef(-1);
  const playingRef = useRef(playing);
  const [kick, setKick] = useState(0);

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

  const centerLine = useCallback((i: number) => {
    const el = rows.current[i];
    if (!el) return;
    glideAt.current = Date.now();
    releaseHold();
    forPane()?.center(el, false);
  }, [rows, forPane, releaseHold]);

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
  }, []);

  // Seek: a jump the reader did not ask for by line. Re-centre even when the
  // jump lands inside the line that was already active, which the active-line
  // effect below would never see.
  useEffect(() => {
    const prev = prevTime.current;
    prevTime.current = time;
    if (prev < 0 || Math.abs(time - prev) <= SEEK_JUMP) return;
    seekAt.current = Date.now();
    releaseHold();
    setKick((k) => k + 1);
  }, [time, releaseHold]);

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
    const now = Date.now();
    const glide = now - glideAt.current < CLICK_GLIDE || now - seekAt.current > SEEK_SNAP;
    forPane()?.center(el, !glide);
    // Deps: the active line and the kicks only. Never the clock — the pane
    // moves one step per line, not one per frame.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, kick, forPane, rows]);

  return { centerLine, takeOver };
}
