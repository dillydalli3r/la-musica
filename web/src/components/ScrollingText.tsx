import { useEffect, useRef, useState, type CSSProperties } from "react";

/** A one-line text that DRIFTS back and forth when it does not fit its box,
 *  and sits still when it does.
 *
 *  Measured, never guessed: the wrapper and the text are both observed
 *  (ResizeObserver), so a web-font swap, a badge appearing beside the title or
 *  a window resize all re-measure, and `shift` is 0 for anything that fits —
 *  a short title must not wobble. The animation is the shared `title-marquee`
 *  keyframe (index.css) driving a CSS variable, which is what makes one
 *  mechanism serve the player bar and the fullscreen player: a truncating
 *  `…` was the whole story in the fullscreen player, and a title that is cut
 *  mid-word is exactly what the bar's own drifting title exists to avoid. */
export default function ScrollingText({ text, className }: {
  text: string;
  className?: string;
}) {
  const wrapRef = useRef<HTMLSpanElement>(null);
  const textRef = useRef<HTMLSpanElement>(null);
  const [shift, setShift] = useState(0);

  useEffect(() => {
    const wrap = wrapRef.current;
    const el = textRef.current;
    if (!wrap || !el) return;
    let raf = 0;
    const measure = () => {
      // scrollWidth of the inner span vs the box it was given: the overflow
      // is the distance the text has to travel (and 0 means it fits).
      const over = el.scrollWidth - wrap.clientWidth;
      setShift(over > 2 ? over + 6 : 0); // +6 = visible padding at the end
    };
    const schedule = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(measure);
    };
    measure();
    // re-measure whenever the available space changes AND when the text's
    // own width changes (web-font swap, async badge rendering)
    const ro = new ResizeObserver(schedule);
    ro.observe(wrap);
    ro.observe(el);
    document.fonts?.ready.then(schedule).catch(() => {});
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
    };
  }, [text]);

  const dur = Math.max(5, Math.min(24, shift / 12));
  return (
    <span ref={wrapRef} className={`block overflow-hidden min-w-0 ${className ?? ""}`}>
      <span
        ref={textRef}
        className={`block whitespace-nowrap ${shift > 0 ? "title-marquee will-change-transform" : ""}`}
        style={
          shift > 0
            ? ({
                "--title-shift": `-${shift}px`,
                animation: `title-marquee ${dur}s ease-in-out infinite`,
              } as CSSProperties)
            : undefined
        }
      >
        {text}
      </span>
    </span>
  );
}
