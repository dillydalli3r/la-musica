/** How the fullscreen music-video picture fills the screen. The player bar
 *  owns the single <video> decoder and the fullscreen overlay draws the
 *  pickers, so the vocabulary lives outside both of them. */
export type VideoAspect = "contain" | "cover" | "stretch";

const ASPECT_KEY = "mlo.video.aspect";

/** Tailwind object-fit class per choice — `stretch` really fills (object-fill). */
export const ASPECT_FIT: Record<VideoAspect, string> = {
  contain: "object-contain",
  cover: "object-cover",
  stretch: "object-fill",
};

/** Persisted fit preference; anything unrecognized (or absent) is `contain`,
 *  the letterboxed default. */
export function readAspect(): VideoAspect {
  const v = localStorage.getItem(ASPECT_KEY);
  return v === "cover" || v === "stretch" ? v : "contain";
}

export function writeAspect(a: VideoAspect) {
  localStorage.setItem(ASPECT_KEY, a);
}
