import type { QueryClient } from "@tanstack/react-query";
import { api } from "../api";

/** The query key the STORED layout report lives under — read by the
 *  Library page's own layout warning. */
export const LAYOUT_REPORT_KEY = ["layout-report"] as const;

/** Scan the music folder again and republish the stored report.
 *
 *  The report is a SCAN's output, and the app itself changes the tree: an album
 *  or an artist folder goes to the Trash, a fix is applied, a folder is put
 *  back. A reader who fixed something then had to press a manual rescan before
 *  the Library's own layout warning — which reads the same stored report —
 *  would believe it (owner report: "I need to manually use this section under
 *  rescan for the library to update. It should be done automatically"). Every
 *  action that moves a folder calls this, so the warning describes the folder
 *  as it is now rather than as it was at the last manual press.
 *
 *  The GET is the READ-ONLY half of the layout route (`mlo.layout
 *  .scan_library`, the same walk script 20 runs) — it never moves anything, so
 *  calling it behind a mutation cannot settle a reader's files by surprise; the
 *  fixing half stays behind script 20's own `layout_apply`. A
 *  scan that cannot run (offline, a locked folder) leaves the stored report in
 *  place and stays silent: this is housekeeping, not a job the reader asked
 *  for, and script 20's own run is the manual way to hear about it. */
export async function rescanLayout(qc: QueryClient): Promise<void> {
  try {
    await api.libraryLayout();
  } catch {
    /* the stored report stands */
  }
  qc.invalidateQueries({ queryKey: LAYOUT_REPORT_KEY });
}