import { cachedFlags, downloadTracks, type DownloadProgress } from "./mediaCache";
import { toast } from "../store";

/** Put `paths` in the browser's offline cache, reporting the run the way every
 *  caller wants it: one toast for the outcome, per-file reasons included, and
 *  nothing said twice ("already downloaded" is its own answer, not a silent
 *  no-op). Extracted from the download BUTTON because the track/album menu
 *  offers the same action and a second implementation would drift from this
 *  one — the button keeps its own progress state through `onProgress`.
 *
 *  Callers own the react-query invalidation afterwards: this module has no
 *  query client (it is not a component), and the two callers already hold one
 *  for their own reasons. */
export async function downloadForOffline(
  paths: string[],
  opts: { onProgress?: (p: DownloadProgress) => void } = {}
): Promise<void> {
  if (!paths.length) return;
  let hits: boolean[];
  try {
    hits = await cachedFlags(paths);
  } catch (e) {
    // The cache probe failing IS the run failing (no Cache Storage, or no
    // answer from the server) — same sentence the download itself would use.
    toast.error(`Download failed: ${e instanceof Error ? e.message : String(e)}`);
    return;
  }
  const todo = paths.filter((_, i) => !hits[i]);
  if (!todo.length) {
    toast("Already downloaded");
    return;
  }
  try {
    // One bounded, cancellable run: N tracks at a time (server config
    // `download_concurrency`), with a bulk request per chunk when the server
    // offers one. Nothing here fires a promise per track.
    const report = await downloadTracks(todo, { onProgress: opts.onProgress });
    if (report.failures.length) {
      // One dead file must not abandon the rest — but a bare count ("2 of 2
      // could not be downloaded") leaves nothing to act on, so the reason
      // travels out with the file it belongs to.
      const rows = report.failures.map((f) => {
        const name = f.path ? f.path.split(/[\\/]/).pop() || f.path : "";
        return name ? `${name} — ${f.message}` : f.message;
      });
      const head = rows.slice(0, 2).join(" · ");
      toast.error(
        `${report.failures.length} of ${todo.length} track(s) could not be downloaded: ${head}` +
          (rows.length > 2 ? ` · +${rows.length - 2} more` : "")
      );
    } else if (report.cancelled) {
      toast(`Download stopped — ${report.done} of ${todo.length} track(s) are cached`);
    } else {
      toast.success(`Downloaded ${report.done} track${report.done === 1 ? "" : "s"} for offline playback`);
    }
  } catch (e) {
    // downloadTracks reports per-track failures itself; this is the run
    // failing outright (no Cache Storage, or no answer from the server).
    toast.error(`Download failed: ${e instanceof Error ? e.message : String(e)}`);
  }
}
