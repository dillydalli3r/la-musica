import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Disc3, FileText, Gauge, Info, Loader2 } from "lucide-react";
import { useMemo, useState } from "react";
import { api } from "../api";
import type { Album, Track } from "../types";
import { toast } from "../store";
import Modal from "./Modal";
import LogReport from "./LogReport";
import DownloadButton from "./DownloadButton";
import { ExportButton } from "./ExportDialog";
import TrackDetails, { DetailRows, DetailSection, type DetailItem } from "./TrackDetails";
import { albumTech, fmtDuration, fmtTech } from "../lib/fmt";
import { tagLabel, tagTooltip, useTagRegistry } from "../lib/tags";
import { pendingSummary } from "./Badges";
import { useI18n } from "../lib/i18n";

/** The album-level tags the readout lists FIRST, in this order — the ones a
 *  reader looks for. Everything else the folder stores follows alphabetically,
 *  so the readout stays complete without a second place to update.
 *
 *  A display order only: every LABEL comes from the tag registry
 *  (server/tags_registry.py), so a key cannot be renamed here. */
const ALBUM_INFO_KEYS = [
  "ALBUM", "ALBUMARTIST", "ARTIST", "DATE", "ORIGINALDATE", "ORIGINALYEAR",
  "RELEASETYPE", "RELEASESTATUS", "RELEASECOUNTRY", "LABEL", "CATALOGNUMBER",
  "BARCODE", "MEDIA", "SCRIPT", "ITUNESADVISORY", "ALBUMITUNESADVISORY",
  "MUSICBRAINZ_ALBUMID", "MUSICBRAINZ_RELEASEGROUPID",
  "MUSICBRAINZ_ALBUMARTISTID", "RATEYOURMUSIC_ALBUM", "ALBUM DYNAMIC RANGE",
];

const yesNo = (v: boolean | null | undefined) => (v ? "yes" : "no");

/** The album-wide AudioAuditor verdict: REAL / FAKE / Auto, written to EVERY
 *  track's `AUDIOAUDITOR_OVERRIDE` tag in ONE request (`api.mbAssign` takes a
 *  path→tags map, so a 20-track album costs one write, not twenty modals).
 *
 *  Read back from the album payload's own `values`, so the buttons show what
 *  the grade sees: a value is "current" only when EVERY track carries it —
 *  a mixed album reads as Auto rather than claiming a verdict half its tracks
 *  do not have. Clearing writes the empty value, which is how the tag is
 *  deleted (see api_mb's assign: a blank value is a delete). */
function AlbumAuditOverride({ paths, tracks }: { paths: string[]; tracks: Track[] }) {
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);
  const stored = useMemo(() => {
    const vals = tracks.map((tr) =>
      String(tr.values?.AUDIOAUDITOR_OVERRIDE ?? "").trim().toUpperCase());
    if (!vals.length) return null;
    return vals.every((v) => v === vals[0]) && (vals[0] === "REAL" || vals[0] === "FAKE")
      ? vals[0]
      : null;
  }, [tracks]);
  const set = async (v: "REAL" | "FAKE" | null) => {
    if (!paths.length) return;
    setBusy(true);
    try {
      const patch: Record<string, Record<string, string | null>> = {};
      for (const p of paths) patch[p] = { AUDIOAUDITOR_OVERRIDE: v };
      await api.mbAssign(patch);
      toast(v
        ? `AudioAuditor forced to ${v} for ${paths.length} track${paths.length === 1 ? "" : "s"} — it survives forced re-audits`
        : "Album override cleared — AudioAuditor decides again");
      qc.invalidateQueries({ queryKey: ["album"] });
      qc.invalidateQueries({ queryKey: ["library"] });
      qc.invalidateQueries({ queryKey: ["tags"] });
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="mt-2 space-y-1">
      <div className="flex flex-wrap items-center gap-1">
        <span className="text-xs text-zinc-400">Audit override — all {paths.length || 0} tracks</span>
        {(["REAL", "FAKE"] as const).map((v) => (
          <button
            key={v}
            className={`px-2 py-0.5 rounded text-[10px] border ${
              stored === v
                ? v === "REAL"
                  ? "bg-emerald-900/60 text-emerald-300 border-emerald-800"
                  : "bg-red-900/60 text-red-300 border-red-800"
                : "bg-panel text-zinc-400 border-border hover:border-accent/50"
            }`}
            disabled={busy}
            onClick={() => set(stored === v ? null : v)}
            title={stored === v
              ? "Click to clear the override on every track"
              : `Force every track of this album to report ${v}`}
          >
            {v}
          </button>
        ))}
        <button
          className={`px-2 py-0.5 rounded text-[10px] border ${
            !stored ? "bg-accent on-accent border-accent" : "bg-panel text-zinc-400 border-border hover:border-accent/50"
          }`}
          disabled={busy}
          onClick={() => set(null)}
          title="Let AudioAuditor decide for every track"
        >
          Auto
        </button>
      </div>
      <div className="text-[10px] text-zinc-500">
        {stored
          ? `Every track is forced ${stored} — this beats the audit verdict, and Audit Library keeps it.`
          : "AudioAuditor decides. Force REAL when you have verified a download yourself — a digital-media album has no rip log for it to read."}
      </div>
    </div>
  );
}

/** Album details: the whole stored readout for one album, built from the ALBUM
 *  PAGE'S OWN PAYLOAD (`GET /api/album`) — metadata, technical summary,
 *  grading, the stored tags, and the per-track technical list. The track modal
 *  (TrackDetails) is the same readout one level down: both render through the
 *  same DetailRows/DetailSection, so a row looks the same in either. */
export function AlbumDetails({ album, onClose }: { album: Album; onClose: () => void }) {
  // The rip log panel, opened on demand: most albums are fine, and the log
  // itself is only ever wanted when the grading has something to answer for.
  const [logOpen, setLogOpen] = useState(false);
  const reg = useTagRegistry();
  const { t } = useI18n();
  const meta = (album.meta ?? {}) as Record<string, string | null | undefined>;
  const extraKeys = Object.keys(meta)
    .filter((k) => meta[k] && !ALBUM_INFO_KEYS.includes(k))
    .sort();
  const tech = albumTech(album.tracks);
  const title = meta.ALBUM || album.path.split("/").pop() || "album";
  const pending = pendingSummary(album, t);
  const issues = Object.entries(album.issues ?? {});
  // The grader's informational half: said, never failed. An album can
  // pass with both lists filled — a disc AccurateRip has no entry for is
  // not this rip's fault (mlo.grader, the CD-leg block).
  const notes = album.notes ?? [];
  const missing = album.expected_tracks?.filter((t) => t.missing).length ?? 0;
  // The album page's own pair (AlbumPage's header row): an album readout opened
  // from a track row's menu has the same two actions, with the same props, so
  // "download / export this album" never means leaving the modal for a page the
  // reader may not have been on.
  const paths = album.tracks.map((t) => t.path);
  const seconds = album.tracks.reduce((s, t) => s + (t.tech?.length ?? 0), 0);

  const infoRows: DetailItem[] = [
    // A framework album is added but its audio has not arrived: that is the
    // first fact about it, so the empty track count below is never read as an
    // album that is simply empty. Same marker, same sentence as every row.
    ...(pending ? [{ label: "State", value: pending.full, title: pending.full }] : []),
    { label: "Album", value: title },
    { label: "Album artist", value: album.album_artist ?? meta.ALBUMARTIST ?? "—" },
    { label: "Year", value: (meta.ORIGINALDATE || meta.DATE || "").slice(0, 4) || "—" },
    { label: "Format", value: tech || album.media || "—", title: "Measured from the tracks' own technical data" },
    { label: "Source", value: album.source_summary || "—" },
    { label: "Tracks", value: `${album.track_count}${missing ? ` (+${missing} missing)` : ""}` },
    { label: "Cover", value: album.cover_file || "—" },
    { label: "Log / CUE", value: `${yesNo(album.has_log)} / ${yesNo(album.has_cue)}` },
    { label: "Lyrics", value: `${album.lyrics_present} of ${album.lyrics_expected} tracks` },
    { label: "Instrumental", value: `${album.instrumental_count}` },
    { label: "Path", value: album.path },
  ];

  const gradeRows: DetailItem[] = [
    {
      label: "Grade",
      value: album.grade_pct == null
        ? (album.pass ? "PASS (no checks enabled)" : "—")
        : `${album.grade_pct}% · ${album.pass_count}/${album.total_checks} checks · ${album.pass ? "PASS" : "FAIL"}`,
    },
    { label: "Audit", value: album.audit_summary || "—", title: "AccurateRip / AudioAuditor verdict for the whole album" },
    { label: "Checksum", value: album.checksum_status || "—" },
    { label: "AccurateRip", value: album.accuraterip_status || "—" },
    { label: "Media", value: album.media || "—" },
    {
      label: "Release MBID",
      value: album.expected_release_id || meta.MUSICBRAINZ_ALBUMID || "—",
      title: "The MusicBrainz release this album was imported as",
    },
  ];

  return (
    <Modal
      onClose={onClose}
      icon={Disc3}
      title={title}
      subtitle={[album.album_artist, (meta.DATE || "").slice(0, 4), album.partial ? "partial import" : ""]
        .filter(Boolean)
        .join(" · ") || album.path.split("/").pop()}
      width="max-w-2xl"
      bodyClass="px-5 py-5 space-y-4"
    >
      <DetailSection icon={Info} title="Release">
        <DetailRows rows={infoRows} />
      </DetailSection>

      {album.tracks.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          <DownloadButton
            paths={paths}
            label="Download for offline playback"
            emptyReason="Nothing to download — this album has no tracks"
          />
          <ExportButton
            paths={paths}
            seconds={seconds}
            title="Export this album to a drive"
            emptyReason="Nothing to export — this album has no tracks"
            dialogSubtitle={`${title} · ${album.tracks.length} track${album.tracks.length === 1 ? "" : "s"}`}
          />
        </div>
      )}

      <DetailSection icon={Gauge} title="Grading & audit">
        <DetailRows rows={gradeRows} />
        {/* One verdict for the whole album. A DIGITAL-MEDIA album has no rip
            log, so "Missing AUDIT tag" has no other way out than the user's
            own call — and doing that track by track, one modal at a time, is
            the wait this removes. It writes AUDIOAUDITOR_OVERRIDE, which the
            grader applies after every derived verdict (mlo.grader, spec R25)
            and which Audit Library now writes back instead of overruling. */}
        <AlbumAuditOverride paths={paths} tracks={album.tracks} />
      </DetailSection>

      {/* The rip log the grading above was read off. Scoring a log and never
          letting anyone read it is what made "score 60 is below the required
          100" a dead end for the owner; the panel opens on demand so an album
          that is fine costs nothing to look at. */}
      {album.has_log && (
        <DetailSection icon={FileText} title="Rip log">
          {logOpen ? (
            <LogReport albumPath={album.path} onClose={() => setLogOpen(false)} />
          ) : (
            <button
              className="btn-ghost !py-1.5 text-xs tap"
              onClick={() => setLogOpen(true)}
              title="Read the log's own text and what Logchecker made of it (score, checksum, its own notes)"
            >
              <FileText className="h-3.5 w-3.5" /> View the log
            </button>
          )}
        </DetailSection>
      )}

      <DetailSection icon={Disc3} title="Stored tags">
        <DetailRows
          rows={[...ALBUM_INFO_KEYS, ...extraKeys].map((key) => ({
            label: tagLabel(reg, key),
            title: tagTooltip(reg, key),
            value: meta[key] ?? "—",
          }))}
        />
      </DetailSection>

      {issues.length > 0 && (
        <DetailSection icon={Gauge} title={`Failed checks (${issues.length})`}>
          <ul className="space-y-1">
            {issues.map(([text, files]) => (
              <li
                key={text}
                className="text-xs text-red-300/90 bg-red-950/30 border border-red-900/40 rounded px-2 py-1 break-all"
              >
                {text}
                {files?.length ? <span className="text-red-400/70"> — {files.join(", ")}</span> : null}
              </li>
            ))}
          </ul>
        </DetailSection>
      )}

      {notes.length > 0 && (
        <DetailSection icon={Info} title={`Not checked (${notes.length})`}>
          <ul className="space-y-1">
            {notes.map((text) => (
              <li
                key={text}
                className="text-xs text-zinc-300/90 bg-zinc-800/40 border border-zinc-700/50 rounded px-2 py-1 break-all"
              >
                {text}
              </li>
            ))}
          </ul>
        </DetailSection>
      )}

      {album.tracks.length > 0 && (
        <DetailSection icon={Info} title={`Tracks (${album.tracks.length})`}>
          <DetailRows
            rows={album.tracks.map((t) => ({
              label: `${t.discnumber && t.discnumber > 1 ? `${t.discnumber}.` : ""}${t.tracknumber ?? ""} ${t.tags?.TITLE ?? t.file}`.trim(),
              title: t.path,
              value: (
                <span className="inline-flex items-center gap-2">
                  <span className="font-mono text-[11px] text-zinc-400">
                    {[fmtTech(t.tech), t.tech.length ? fmtDuration(t.tech.length) : ""]
                      .filter(Boolean).join(" · ") || "—"}
                  </span>
                  {t.issues?.length ? <span className="text-red-400">{t.issues.length} failed</span> : null}
                </span>
              ),
            }))}
          />
        </DetailSection>
      )}
    </Modal>
  );
}

/** The Details menu's loader for the row context menu, which has paths and no
 *  payload: `albumPath` opens the album readout, `trackPath` the file's own
 *  TrackDetails. Both come from the album payload the album page itself asks
 *  for (`["album", dir]`), so a menu opened on an album page costs no request
 *  and the two surfaces can never show different data. */
export function DetailsDialog({ albumPath, trackPath, onClose }: {
  albumPath?: string; trackPath?: string; onClose: () => void;
}) {
  const dir = albumPath ?? (trackPath ?? "").replace(/\\/g, "/").split("/").slice(0, -1).join("/");
  const { data, isLoading, error } = useQuery({
    queryKey: ["album", dir],
    queryFn: () => api.album(dir),
    enabled: !!dir,
    retry: false,
  });
  if (isLoading) {
    return (
      <Modal onClose={onClose} icon={Info} title="Details" width="max-w-lg" bodyClass="px-5 py-5">
        <div className="flex items-center gap-2 text-xs text-zinc-500">
          <Loader2 className="h-3.5 w-3.5 animate-spin" /> Reading the album…
        </div>
      </Modal>
    );
  }
  if (error || !data) {
    return (
      <Modal onClose={onClose} icon={Info} title="Details" width="max-w-lg" bodyClass="px-5 py-5">
        <div className="text-xs text-zinc-500">
          {error ? `Could not read this album — ${String(error)}` : "This folder holds no album."}
        </div>
      </Modal>
    );
  }
  if (!trackPath) return <AlbumDetails album={data} onClose={onClose} />;
  const found = data.tracks.find((t) => t.path === trackPath);
  if (!found) {
    return (
      <Modal onClose={onClose} icon={Info} title="Details" width="max-w-lg" bodyClass="px-5 py-5">
        <div className="text-xs text-zinc-500">
          This file is not in the album any more — it may have moved or been renamed.
        </div>
      </Modal>
    );
  }
  return <TrackDetails track={found} albumPath={dir} onClose={onClose} />;
}
