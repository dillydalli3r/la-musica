import { Download } from "lucide-react";
import PageHeader from "../components/PageHeader";
import CachedTracksView from "../components/CachedTracksView";

/** The offline downloads: exactly the tracks this browser can play without
 *  the server, laid out as the library's album table. Downloading happens
 *  where the music already is — the album, artist, playlist and player
 *  download buttons — so this page is the one place to see what was
 *  downloaded and to drop it again.
 *
 *  Saving a file to disk is Export's job, and the Soulseek page owns the
 *  staging folder downloads land in; neither is this. */
export default function DownloadsPage() {
  return (
    <div className="p-6 space-y-5">
      <PageHeader
        icon={Download}
        title="Downloads"
        subtitle="Tracks cached in this browser — playable with the server down"
      />
      <CachedTracksView />
    </div>
  );
}
