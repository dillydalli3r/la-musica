import { api } from "../api";
import { toast } from "../store";

/** One track's music video, downloaded and then tagged as THAT track's.
 *
 *  The two server calls (`POST /api/videos/download-youtube`, then
 *  `POST /api/videos/match`) and every word the user hears about them live
 *  HERE, because the app offers this action from three places — the album
 *  page's release-wide film button ("fetch what this album is missing"), the
 *  video overlay's own button while a video plays, and the details menu's
 *  entry on a single track. Three copies of the flow would be three chances
 *  for them to ask something slightly different or report it differently. */
export interface TrackVideoRequest {
  /** The track the video is FOR. The file lands in this track's own folder
   *  (the server resolves a track path to its folder) and the tag write
   *  assigns the downloaded video to this track's title, which is what makes
   *  it this track's video — a download named after the YouTube upload says
   *  nothing about the release. */
  path: string;
  artist?: string;
  title?: string;
  /** Expected length in seconds: the candidate search uses it to reject
   *  live / tribute / cover uploads. */
  duration?: number;
  tracknumber?: number | null;
  discnumber?: number | null;
}

/** The folder a track lives in — the `album_path` the tag write takes. A path
 *  with no separator is its own folder (the API answers with the server's
 *  own error if that is not somewhere under the music folder). */
export function trackFolder(path: string): string {
  const cut = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  return cut > 0 ? path.slice(0, cut) : path;
}

/** Download one track's music video: YouTube first, else Soulseek, then tag
 *  the file as this track's. Returns whether a video was fetched — a Soulseek
 *  QUEUE counts: the transfer runs in the app's own downloads and cannot be
 *  waited on here.
 *
 *  `hooks` says what the caller has to refresh: `onQueued` when a network
 *  transfer was queued (the Downloads page shows it) and `onSaved` when a file
 *  was downloaded and tagged (the surface's own video list is stale from that
 *  moment on). Both are called AFTER the toast that explains what happened. */
export async function downloadTrackVideo(
  req: TrackVideoRequest,
  hooks?: { onQueued?: () => void; onSaved?: () => void },
): Promise<boolean> {
  const title = (req.title ?? "").trim();
  if (!title) {
    // A title is what the search IS: without one there is nothing to look for,
    // and the tag write would have nothing to assign the file to.
    toast("This track has no TITLE tag — there is nothing to search for");
    return false;
  }
  try {
    const r = await api.videosDownloadYoutube({
      path: req.path,
      artist: req.artist ?? "",
      title,
      duration: req.duration || undefined,
    });
    if (r.ok && r.queued) {
      const what = String(r.candidate?.filename ?? title);
      toast(`Queued from Soulseek: ${what} — it downloads into Downloads`);
      hooks?.onQueued?.();
      return true;
    }
    if (!r.ok || !r.file) {
      toast(r.error ? `No music video: ${r.error}` : "No matching music video found");
      return false;
    }
    toast(`Music video saved: ${r.file.split(/[\\/]/).pop()}`);
    try {
      await api.videosMatch(trackFolder(req.path), [
        {
          path: r.file,
          title,
          tracknumber: req.tracknumber ?? undefined,
          discnumber: req.discnumber ?? undefined,
        },
      ]);
    } catch (e) {
      // The file IS downloaded; only the tag write failed, and the album
      // page's matching panel can still record the assignment by hand — so
      // say that rather than reporting the whole download as failed.
      toast.error(`Downloaded, but tagging it as "${title}" failed: ${e}`);
    }
    hooks?.onSaved?.();
    return true;
  } catch (e) {
    toast.error(String(e));
    return false;
  }
}
