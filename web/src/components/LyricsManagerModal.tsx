import { useEffect, useMemo, useState } from "react";
import { CloudDownload, Loader2, PenLine, Search, ChevronDown, ChevronUp, ClipboardPaste, Upload } from "lucide-react";
import { api } from "../api";
import { toast } from "../store";
import LrclibPublishPanel from "./LrclibPublish";
import LyricsEditorModal from "./LyricsEditorModal";
import { LyricsKindChip } from "./Badges";
import { lyricsKindOf } from "./LyricsViewer";
import Modal from "./Modal";

interface Candidate {
  id: number;
  /** Which provider supplied this hit (LRCLIB, NetEase, Kugou, QQ Music,
   *  Kuwo, YouTube captions). */
  provider?: string;
  artistName: string;
  trackName: string;
  albumName?: string;
  duration?: number;
  instrumental?: boolean;
  plain: string;
  synced: string;
}

/** Query variants so fuzzy real-world tags still find a match: strip
 * parenthetical/bracket suffixes and remix markers from the title, try the
 * primary artist before feat./comma separators, and combine them. */
function queryVariants(artist: string, track: string): { artist: string; track: string }[] {
  const cleanTrack = (t: string) =>
    t
      .replace(/\s*[(\[][^)\]]*(feat|ft|version|remaster|live|remix|edit|mix|deluxe|bonus)[^)\]]*[)\]]/gi, "")
      .replace(/\s+-\s+(remaster|live|remix|single|edit|bonus).*$/i, "")
      .trim();
  const cleanArtist = (a: string) =>
    a
      .split(/,| feat\.| ft\.| & | x | × /i)[0]
      .trim();
  const out: { artist: string; track: string }[] = [];
  const seen = new Set<string>();
  for (const [a, t] of [
    [artist, track],
    [artist, cleanTrack(track)],
    [cleanArtist(artist), track],
    [cleanArtist(artist), cleanTrack(track)],
  ] as const) {
    if (!a || !t) continue;
    const key = `${a.toLowerCase()}|${t.toLowerCase()}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ artist: a, track: t });
  }
  return out;
}

const fmtDur = (s?: number) =>
  s ? `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}` : "—";

export default function LyricsManagerModal({
  path,
  artist,
  track,
  album,
  duration,
  currentText,
  allowPlain,
  onApplied,
  onSaved,
  onClose,
}: {
  path: string;
  artist: string;
  track: string;
  album?: string;
  duration?: number;
  currentText?: string;
  /** The user's `lyrics_allow_plain` — the same flag the page header's chip
   *  reads, so the track's current lyrics are marked the same way here. */
  allowPlain?: boolean;
  /** Receives the chosen LRC/plain text plus a short source label. */
  onApplied: (lrc: string, source: string) => void;
  /** Fired after the embedded editor saves — invalidate the host page's
   * track/library/album queries here or they keep serving the old lyrics. */
  onSaved?: () => void;
  onClose: () => void;
}) {
  const [loading, setLoading] = useState(true);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [searchedVariants, setSearchedVariants] = useState(0);
  const [preview, setPreview] = useState<number | null>(null);
  const [manual, setManual] = useState("");
  const [manualOpen, setManualOpen] = useState(false);
  const [pubOpen, setPubOpen] = useState(false);
  const [editorOpen, setEditorOpen] = useState(false);

  const search = useMemo(
    () => () => {
      setLoading(true);
      setCandidates([]);
      setPreview(null);
      const variants = queryVariants(artist, track);
      setSearchedVariants(variants.length);
      (async () => {
        const byId = new Map<number, Candidate>();
        for (const v of variants) {
          try {
            const hits = await api.lyricsSearch(v.artist, v.track, album, duration);
            for (const h of Array.isArray(hits) ? hits : []) {
              const id = Number(h?.id);
              if (!id || byId.has(id)) continue;
              const plain = typeof h?.plainLyrics === "string" ? h.plainLyrics : "";
              const synced = typeof h?.syncedLyrics === "string" ? h.syncedLyrics : "";
              if (!plain && !synced) continue;
              byId.set(id, {
                id,
                artistName: String(h?.artistName ?? ""),
                trackName: String(h?.trackName ?? ""),
                albumName: h?.albumName ? String(h.albumName) : undefined,
                duration: h?.duration ? Number(h.duration) : undefined,
                instrumental: !!h?.instrumental,
                provider: h?.provider_label ? String(h.provider_label) : undefined,
                plain,
                synced,
              });
            }
          } catch {
            /* variant failing is fine — try the next */
          }
        }
        const rows = [...byId.values()];
        // best first: synced candidates, then closest duration
        rows.sort((a, b) => {
          const s = (c: Candidate) =>
            (c.synced ? 0 : 10) + Math.min(Math.abs((c.duration ?? 0) - (duration ?? 0)), 120);
          return s(a) - s(b);
        });
        setCandidates(rows);
        setLoading(false);
      })();
    },
    [artist, track, album, duration]
  );

  useEffect(search, [search]);

  const apply = (lrc: string, source: string) => {
    if (!lrc.trim()) {
      toast("Nothing to apply");
      return;
    }
    onApplied(lrc, source);
  };

  return (
    <>
      <Modal
        onClose={onClose}
        icon={Search}
        title="Find lyrics"
        subtitle={`${artist} — ${track}`}
        width="max-w-2xl"
        bodyClass="px-5 py-4 space-y-3"
        footer={
          <div className="text-[10px] text-zinc-600">
            Applying hands the lyrics to the editor / player — saving still follows your lyrics format &amp; save-target settings.
          </div>
        }
      >
      <div className="flex gap-2 flex-wrap items-center">
        {/* WHICH KIND the track's CURRENT lyrics are, beside the actions that
            replace them: the candidates below wear their own SYNCED/PLAIN
            marks, and this is the text already on the track. */}
        <LyricsKindChip kind={lyricsKindOf(currentText)} allowPlain={allowPlain} showReason />
        <button className="btn-ghost !py-1.5 text-xs" onClick={search} disabled={loading}>
          <CloudDownload className="h-3.5 w-3.5" /> Re-search
        </button>
        <button className="btn-ghost !py-1.5 text-xs" onClick={() => setManualOpen(!manualOpen)}>
          <ClipboardPaste className="h-3.5 w-3.5" /> Paste manually
        </button>
        <button
          className={`btn-ghost !py-1.5 text-xs ${editorOpen ? "!text-accent" : ""}`}
          onClick={() => setEditorOpen(true)}
          title="Full-screen lyrics editor: tap line/word/syllable times along the vocals, speed control"
        >
          <PenLine className="h-3.5 w-3.5" /> Enhanced editor
        </button>
        {currentText?.trim() && (
          <button
            className={`btn-ghost !py-1.5 text-xs ${pubOpen ? "!text-accent" : ""}`}
            onClick={() => setPubOpen(!pubOpen)}
            title="Give these lyrics back to the community database"
          >
            <Upload className="h-3.5 w-3.5" /> Publish to LRCLIB
          </button>
        )}
      </div>

      {pubOpen && currentText?.trim() && (
        <LrclibPublishPanel
          artist={artist}
          track={track}
          album={album}
          duration={duration}
          text={currentText}
          onDone={() => setPubOpen(false)}
        />
      )}

      {manualOpen && (
        <div>
          <textarea
            className="input font-mono text-xs min-h-[120px]"
            placeholder="Paste plain or LRC lyrics here…"
            value={manual}
            onChange={(e) => setManual(e.target.value)}
          />
          <button className="btn-primary !py-1 text-xs mt-2" onClick={() => apply(manual, "manual paste")} disabled={!manual.trim()}>
            Use these lyrics
          </button>
        </div>
      )}

      <div className="space-y-1.5">
        {loading && (
          <div className="flex items-center gap-2 text-xs text-zinc-500 py-8 justify-center">
            <Loader2 className="h-4 w-4 animate-spin" /> Searching lyric providers ({searchedVariants} query variants)…
          </div>
        )}
        {!loading && candidates.length === 0 && (
          <div className="text-xs text-zinc-500 py-8 text-center">
            No candidates found. Paste lyrics manually instead.
          </div>
        )}
        {candidates.map((c) => {
          const near = duration ? Math.abs((c.duration ?? 0) - duration) : null;
          return (
            <div key={c.id} className="rounded-xl border border-white/10 bg-white/[0.03] hover:bg-white/[0.06] transition-colors">
              <div className="flex items-center gap-3 p-3">
                <div className="flex-1 min-w-0">
                  <div className="text-sm text-zinc-100 truncate">
                    {c.trackName} <span className="text-zinc-500">· {c.artistName}</span>
                  </div>
                  <div className="text-[11px] text-zinc-500 flex items-center gap-2 mt-0.5">
                    <span>{fmtDur(c.duration)}</span>
                    {c.albumName && <span className="truncate max-w-[220px]">{c.albumName}</span>}
                    {near !== null && (
                      <span className={near <= 3 ? "text-emerald-400" : near <= 15 ? "text-amber-400" : "text-zinc-600"}>
                        {near <= 2 ? "duration match" : `±${Math.round(near)}s`}
                      </span>
                    )}
                  </div>
                </div>
                <span
                  className={`chip text-[10px] shrink-0 border ${
                    c.synced
                      ? "bg-emerald-900/50 text-emerald-300 border-emerald-800"
                      : "bg-zinc-800 text-zinc-400 border-border"
                  }`}
                >
                  {c.synced ? "SYNCED" : "PLAIN"}
                </span>
                <button
                  className="p-1 rounded hover:bg-white/10 text-zinc-400 hover:text-white shrink-0"
                  onClick={() => setPreview(preview === c.id ? null : c.id)}
                  title="Preview"
                >
                  {preview === c.id ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
                </button>
                <button
                  className="btn-primary !py-1 text-[11px] shrink-0"
                  onClick={() => apply(c.synced || c.plain, `${c.provider ?? "Lyrics"}${c.synced ? " (synced)" : ""}`)}
                >
                  Apply
                </button>
              </div>
              {preview === c.id && (
                <pre className="mx-3 mb-3 mt-0 max-h-44 overflow-auto text-[11px] leading-relaxed text-zinc-400 whitespace-pre-wrap font-mono bg-black/30 rounded-lg p-3">
                  {(c.synced || c.plain).split("\n").slice(0, 40).join("\n")}
                  {(c.synced || c.plain).split("\n").length > 40 ? "\n…" : ""}
                </pre>
              )}
            </div>
          );
        })}
      </div>
      </Modal>

      {editorOpen && (
        <LyricsEditorModal
          path={path}
          artist={artist}
          track={track}
          album={album}
          duration={duration}
          initialLyrics={currentText ?? ""}
          onClose={() => setEditorOpen(false)}
          onSaved={onSaved}
        />
      )}
    </>
  );
}
