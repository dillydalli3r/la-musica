import { useEffect, useState } from "react";
import { Image, Loader2, RefreshCw, ExternalLink, Check, X } from "lucide-react";
import { api } from "../api";
import { toast } from "../store";
import type { CoverResult } from "../types";

const SOURCE_NAMES: Record<string, string> = {
  qobuz: "Qobuz",
  applemusic: "Apple Music",
  tidal: "Tidal",
  bandcamp: "Bandcamp",
  deezer: "Deezer",
  spotify: "Spotify",
  itunes: "iTunes",
  discogs: "Discogs",
  musicbrainz: "Cover Art Archive",
  amazonmusic: "Amazon Music",
  fanarttv: "Fanart.tv",
  lastfm: "Last.fm",
  soundcloud: "SoundCloud",
};

/** Try to read a pixel width out of a CDN cover URL (best effort). */
function urlWidth(url: string | null): number | null {
  if (!url) return null;
  const m = url.match(/(\d{2,5})x/);
  return m ? parseInt(m[1], 10) : null;
}

interface Props {
  albumPath: string;
  artist: string;
  album: string;
  onClose: () => void;
  onApplied?: () => void;
}

export default function CoverSearchModal({ albumPath, artist, album, onClose, onApplied }: Props) {
  const [qArtist, setQArtist] = useState(artist);
  const [qAlbum, setQAlbum] = useState(album);
  const [results, setResults] = useState<CoverResult[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<CoverResult | null>(null);
  const [applying, setApplying] = useState(false);

  const search = async (a = qArtist, al = qAlbum) => {
    setLoading(true);
    setError(null);
    setSelected(null);
    try {
      const r = await api.coverSearch(a.trim(), al.trim());
      setResults(r.results ?? []);
    } catch (e) {
      setError(String(e));
      setResults(null);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    search(artist, album);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const apply = async (r: CoverResult) => {
    if (!r.big && !r.small) return;
    setApplying(true);
    try {
      const res = await api.coverFromUrl(albumPath, r.big || r.small!);
      toast(`Cover saved as ${res.path.split("/").pop()}`);
      onApplied?.();
      onClose();
    } catch (e) {
      toast(String(e));
    } finally {
      setApplying(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-40 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4 sm:p-6"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="bg-card border border-border rounded-xl w-full max-w-4xl max-h-[90vh] flex flex-col shadow-2xl">
        <div className="flex items-center justify-between gap-3 p-4 border-b border-border">
          <h2 className="font-semibold flex items-center gap-2">
            <Image className="h-4 w-4 text-accent" /> Find cover
            <span className="text-xs text-zinc-500 font-normal">covers.musichoarders.xyz</span>
          </h2>
          <button className="btn-ghost !p-1.5" onClick={onClose} title="Close">
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="p-4 flex flex-wrap gap-2 items-center border-b border-border">
          <input
            className="input !w-52"
            placeholder="Artist"
            value={qArtist}
            onChange={(e) => setQArtist(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && search()}
          />
          <input
            className="input !w-52"
            placeholder="Album"
            value={qAlbum}
            onChange={(e) => setQAlbum(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && search()}
          />
          <button className="btn-primary !py-1.5" onClick={() => search()} disabled={loading}>
            {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
            Search
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4">
          {error && <div className="text-red-400 text-sm p-3 bg-red-950/40 rounded-lg border border-red-900">{error}</div>}
          {loading && (
            <div className="text-zinc-500 text-sm flex items-center gap-2 p-3">
              <Loader2 className="h-4 w-4 animate-spin" /> Searching cover sources…
            </div>
          )}
          {!loading && results && results.length === 0 && !error && (
            <div className="text-zinc-500 text-sm p-3">No covers found for this query.</div>
          )}
          {results && results.length > 0 && (
            <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
              {results.map((r, i) => {
                const px = urlWidth(r.big || r.small);
                return (
                  <button
                    key={`${r.source}-${i}`}
                    className={`group text-left rounded-lg overflow-hidden border transition-colors ${
                      selected === r
                        ? "border-accent ring-1 ring-accent"
                        : "border-border hover:border-zinc-600"
                    } bg-raise`}
                    onClick={() => setSelected(r)}
                    title={r.title ?? undefined}
                  >
                    <div className="aspect-square bg-zinc-950 overflow-hidden">
                      {r.small && (
                        <img
                          src={r.small}
                          alt={r.title ?? "cover"}
                          className="w-full h-full object-cover group-hover:scale-105 transition-transform"
                          loading="lazy"
                          referrerPolicy="no-referrer"
                        />
                      )}
                    </div>
                    <div className="p-2 space-y-0.5">
                      <div className="flex items-center justify-between gap-1">
                        <span className="text-[10px] font-semibold uppercase tracking-wider text-accent-soft bg-accent/10 border border-accent/25 rounded px-1 py-px">
                          {SOURCE_NAMES[r.source] ?? r.source}
                        </span>
                        {px != null && <span className="text-[10px] text-zinc-500">{px}px</span>}
                      </div>
                      <div className="text-xs font-medium truncate">{r.title ?? "—"}</div>
                      <div className="text-[11px] text-zinc-500 truncate">
                        {r.artist ?? "—"}
                        {r.tracks ? ` · ${r.tracks} tracks` : ""}
                      </div>
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </div>

        {selected && (
          <div className="border-t border-border p-4 flex items-center gap-4 bg-zinc-950/50">
            <img
              src={selected.big || selected.small || ""}
              alt="preview"
              className="h-24 w-24 rounded-lg border border-border object-cover"
              referrerPolicy="no-referrer"
            />
            <div className="flex-1 min-w-0 text-sm">
              <div className="font-medium truncate">{selected.title ?? "—"}</div>
              <div className="text-zinc-400 truncate">{selected.artist ?? "—"}</div>
              {selected.url && (
                <a
                  href={selected.url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-xs text-accent-soft hover:underline inline-flex items-center gap-1 mt-0.5"
                >
                  <ExternalLink className="h-3 w-3" /> open release page
                </a>
              )}
            </div>
            <button className="btn-primary" onClick={() => apply(selected)} disabled={applying}>
              {applying ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
              Use this cover
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
