import { useEffect, useState } from "react";
import { Image, Loader2, RefreshCw, ExternalLink, Check } from "lucide-react";
import { api } from "../api";
import { toast } from "../store";
import type { CoverResult, CoverSourceCatalog } from "../types";
import Modal from "./Modal";

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
  coverartarchive: "Cover Art Archive",
  amazonmusic: "Amazon Music",
  fanarttv: "Fanart.tv",
  lastfm: "Last.fm",
  soundcloud: "SoundCloud",
};

/** Pixel width parsed out of a CDN cover URL — a cheap HINT only; the result
 *  actually chosen is measured from the loaded image (`naturalWidth`). */
function urlWidth(url: string | null): number | null {
  if (!url) return null;
  const m = url.match(/(\d{2,5})x/);
  return m ? parseInt(m[1], 10) : null;
}

/** Fallback when the backend config has no usable `cover_target_size`. */
const DEFAULT_TARGET = 1200;

/** A result's pixel size — best source first: the backend's own probe of the
 *  image file, then the image the browser measured, and only then the CDN
 *  URL's own hint. That hint is a REQUEST width ("…/500x0w.jpg"), not the
 *  size the URL answers with, so it is marked as an estimate. `null` = no
 *  size known at all. */
function resultSize(
  r: CoverResult,
  measured?: { w: number; h: number }
): { w: number; h: number; real: boolean } | null {
  if (r.width && r.height) return { w: r.width, h: r.height, real: true };
  if (measured) return { w: measured.w, h: measured.h, real: true };
  const hint = urlWidth(r.big || r.small);
  return hint != null ? { w: hint, h: hint, real: false } : null;
}

interface Props {
  albumPath: string;
  artist: string;
  album: string;
  onClose: () => void;
  onApplied?: () => void;
  /** Audio filenames in the album folder: apply the chosen image to THESE
   *  tracks (one file, many tracks) instead of the album cover. */
  tracks?: string[];
  /** The album's MusicBrainz release-group MBID, when the page knows it — the
   *  identity the Cover Art Archive fallback is asked about. Without it that
   *  fallback can only answer for artist/album. */
  releaseGroupMbid?: string;
  /** Candidates the import already fetched and staged (`cover_review` on):
   *  the modal opens showing these instead of searching, which is what turns
   *  "review" into a single pick. Absent → search as before. */
  initialResults?: CoverResult[];
  /** Who answered that staged fetch (the badge next to the grid). */
  initialProvider?: string | null;
}

export default function CoverSearchModal({ albumPath, artist, album, onClose, onApplied, tracks, releaseGroupMbid, initialResults, initialProvider }: Props) {
  const [qArtist, setQArtist] = useState(artist);
  const [qAlbum, setQAlbum] = useState(album);
  const [results, setResults] = useState<CoverResult[] | null>(initialResults ?? null);
  // Who answered the last search: "cov" for the meta-search, a fallback id
  // ("deezer", "itunes", "coverartarchive") when it had nothing, null when
  // nobody did. Shown so a fallback answer is never silently passed off as
  // the meta-search's.
  const [provider, setProvider] = useState<string | null>(initialProvider ?? null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<CoverResult | null>(null);
  const [applying, setApplying] = useState(false);
  // The app's cover target (as graded by the backend) — read once.
  const [target, setTarget] = useState(DEFAULT_TARGET);
  // Real dimensions of the selected result's image, measured on load and keyed
  // by URL so a stale measurement can never describe a different pick.
  const [measured, setMeasured] = useState<{ url: string; w: number; h: number } | null>(null);
  // Per-result real dimensions, keyed by the image URL. COV's event carries no
  // size, so each result's BIG image is measured by loading it once — the
  // browser caches it, and the same image is what "apply" then downloads.
  const [sizes, setSizes] = useState<Record<string, { w: number; h: number }>>({});
  // Source/region overrides for THIS search; seeded from the saved defaults.
  const [cat, setCat] = useState<CoverSourceCatalog | null>(null);
  const [srcSel, setSrcSel] = useState<string[]>([]);
  const [country, setCountry] = useState("");
  const [pickerOpen, setPickerOpen] = useState(false);
  // A CAA reference URL that 404'd — kept as the URL, not a boolean, so a new
  // album's own 404 can never hide the next album's cover.
  const [refDead, setRefDead] = useState<string | null>(null);

  /** Every provider image on this screen goes through the app (`api.artUrl`):
   *  several cover CDNs — Deezer's above all — refuse the browser on some
   *  networks, and the backend both gets past that and falls back to a
   *  provider that answers. A result's OWN artist/title is the identity that
   *  fallback is asked about (this album's, when a result states none). */
  const artUrl = (u: string | null | undefined, own?: { artist?: string | null; album?: string | null }) =>
    api.artUrl(u, {
      artist: own?.artist || qArtist.trim() || artist,
      album: own?.album || qAlbum.trim() || album,
      rg: releaseGroupMbid,
    });

  /** Cover Art Archive's front cover for the album's release group — the one
   *  image the candidates are judged against. Asked about through the app's
   *  cached art proxy with NO artist/album identity on purpose: the proxy
   *  falls back to Deezer/Apple when the URL it is given fails, and a Deezer
   *  cover labelled "MusicBrainz reference" would be a lie. Without an MBID,
   *  or when the proxied fetch 404s, there is no reference at all. */
  const caaRef = releaseGroupMbid
    ? `https://coverartarchive.org/release-group/${releaseGroupMbid}/front-500`
    : null;
  const showRef = caaRef != null && refDead !== caaRef;

  useEffect(() => {
    api
      .coverSources()
      .then((c) => {
        setCat(c);
        setSrcSel((cur) => (cur.length ? cur : c.default_sources));
        setCountry((cur) => cur || c.default_country);
      })
      .catch(() => {});
  }, []);

  /** Measure each result's big image (the one "apply" would download). */
  useEffect(() => {
    if (!results?.length) return;
    let dead = false;
    for (const r of results) {
      const url = r.big || r.small;
      if (!url || sizes[url]) continue;
      const img = new window.Image();
      img.onload = () => {
        if (dead) return;
        setSizes((s) => (s[url] ? s : { ...s, [url]: { w: img.naturalWidth, h: img.naturalHeight } }));
      };
      img.src = artUrl(url, { artist: r.artist, album: r.title });
    }
    return () => {
      dead = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [results]);

  const search = async (a = qArtist, al = qAlbum) => {
    setLoading(true);
    setError(null);
    setSelected(null);
    try {
      const r = await api.coverSearch(a.trim(), al.trim(), {
        sources: srcSel.length ? srcSel : undefined,
        country: country || undefined,
        releaseGroupMbid,
      });
      setResults(r.results ?? []);
      setProvider(r.provider ?? null);
    } catch (e) {
      setError(String(e));
      setResults(null);
      setProvider(null);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    // Staged candidates are already the answer to this query — searching again
    // would throw the user's own fetched set away. Same condition as the state
    // seed: no `initialResults` prop at all keeps the old auto-search.
    if (initialResults) return;
    search(artist, album);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    api
      .config()
      .then((cfg) => {
        const n = Number(cfg.cover_target_size);
        if (Number.isFinite(n) && n > 0) setTarget(n);
      })
      .catch(() => {});
  }, []);

  const apply = async (r: CoverResult) => {
    if (!r.big && !r.small) return;
    setApplying(true);
    setError(null);
    try {
      const perTrack = tracks?.length ? tracks : undefined;
      const res = await api.coverFromUrl(albumPath, r.big || r.small!, undefined, perTrack, {
        artist: r.artist ?? qArtist,
        album: r.title ?? qAlbum,
        rg: releaseGroupMbid,
      });
      const name = res.path.split("/").pop();
      toast(res.warning ? `Cover saved as ${name} — ${res.warning}` : `Cover saved as ${name}`);
      onApplied?.();
      onClose();
    } catch (e) {
      setError(String(e));
    } finally {
      setApplying(false);
    }
  };

  return (
    <Modal
      onClose={onClose}
      icon={Image}
      title={
        <>
          Find cover{" "}
          <a
            href="https://covers.musichoarders.xyz"
            target="_blank"
            rel="noreferrer"
            className="text-xs text-zinc-500 font-normal hover:text-accent-soft hover:underline inline-flex items-center gap-1"
          >
            covers.musichoarders.xyz <ExternalLink className="h-3 w-3" />
          </a>
        </>
      }
      width="max-w-4xl"
      bodyClass="!px-0 !py-0"
      footer={
        selected && (
          <div className="flex items-center gap-4">
            <img
              key={selected.big || selected.small || ""}
              src={artUrl(selected.big || selected.small, { artist: selected.artist, album: selected.title })}
              alt="preview"
              className="h-24 w-24 rounded-lg border border-border object-cover"
              referrerPolicy="no-referrer"
              onLoad={(e) => {
                const el = e.currentTarget;
                if (el.naturalWidth > 0 && el.naturalHeight > 0) {
                  setMeasured({
                    url: selected.big || selected.small || "",
                    w: el.naturalWidth,
                    h: el.naturalHeight,
                  });
                }
              }}
            />
            <div className="flex-1 min-w-0 text-sm">
              <div className="font-medium truncate">{selected.title ?? "—"}</div>
              <div className="text-zinc-400 truncate">{selected.artist ?? "—"}</div>
              {(() => {
                const m = measured?.url === (selected.big || selected.small) ? measured : undefined;
                const size = resultSize(selected, m);
                if (!size) return null;
                const low = size.w < target;
                return (
                  <div className={`text-xs mt-0.5 ${low ? "text-amber-400" : "text-zinc-500"}`}>
                    {size.real ? `${size.w}×${size.h}px` : `~${size.w}px (estimated)`}
                    {low && ` · low resolution (target ${target}px)`}
                  </div>
                );
              })()}
              {tracks && tracks.length > 0 && (
                <div className="text-xs text-zinc-500 mt-0.5">
                  Applies to {tracks.length} selected track{tracks.length === 1 ? "" : "s"} — one image, no copies
                </div>
              )}
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
        )
      }
    >

      <div className="px-4 py-2 border-b border-border text-[11px] text-zinc-500 flex items-center gap-3">
        {showRef && (
          <div
            className="group relative shrink-0 flex items-center gap-2"
            title="MusicBrainz reference — the album's own Cover Art Archive front cover"
          >
            <img
              src={api.artUrl(caaRef)}
              alt="MusicBrainz reference"
              className="h-11 w-11 rounded border border-border object-cover bg-zinc-950"
              referrerPolicy="no-referrer"
              onError={() => setRefDead(caaRef)}
            />
            <span className="text-[10px] font-semibold uppercase tracking-wider text-zinc-400 leading-tight">
              MusicBrainz
              <br />
              reference
            </span>
            {/* The enlarged copy — hover only, and purely a CSS one so no
                state, no popover lib, and no layout shift for the grid. */}
            <div className="pointer-events-none absolute left-0 top-full mt-2 z-50 hidden group-hover:block">
              <img
                src={api.artUrl(caaRef)}
                alt=""
                className="h-80 w-80 max-w-[70vw] rounded-lg border border-border object-contain bg-zinc-950 shadow-2xl"
                referrerPolicy="no-referrer"
              />
              <div className="text-[10px] text-zinc-400 mt-1">Cover Art Archive front cover</div>
            </div>
          </div>
        )}
        <span>
          The MusicBrainz cover shown on the album may be wrong — open covers.musichoarders.xyz
          above and pick the correct one there. Covers below {target}px are flagged.
        </span>
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
        {cat && (
          <button
            className={`btn-ghost !py-1.5 text-xs ml-auto ${pickerOpen ? "!text-accent" : ""}`}
            onClick={() => setPickerOpen((v) => !v)}
            title="Choose which cover sources and which region to search"
          >
            {srcSel.length}/{cat.active_source_limit} sources · {country.toUpperCase()}
          </button>
        )}
      </div>

      {/* Per-search overrides. The saved defaults come from Settings; this
          picker changes ONE search without touching them. */}
      {pickerOpen && cat && (
        <div className="px-4 py-3 border-b border-border bg-panel/50 space-y-2">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-[11px] font-semibold text-zinc-400">Region</span>
            <select
              className="input !w-auto !py-1 text-xs"
              value={country}
              onChange={(e) => setCountry(e.target.value)}
            >
              {cat.countries.map((c) => (
                <option key={c} value={c}>{c.toUpperCase()}</option>
              ))}
            </select>
            <span className="text-[10px] text-zinc-600">
              The storefront the sources are asked about — it decides which
              releases and artwork exist for a region.
            </span>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-[11px] font-semibold text-zinc-400">Sources</span>
            <button
              className="btn-ghost !py-0.5 !px-1.5 text-[10px]"
              onClick={() => setSrcSel(cat.default_sources)}
            >
              Defaults
            </button>
            <button
              className="btn-ghost !py-0.5 !px-1.5 text-[10px]"
              onClick={() => setSrcSel(cat.sources.filter((s) => s.enabled).map((s) => s.id))}
            >
              All
            </button>
            <button className="btn-ghost !py-0.5 !px-1.5 text-[10px]" onClick={() => setSrcSel([])}>
              None
            </button>
            <span className="text-[10px] text-zinc-500">
              at most {cat.active_source_limit} per search
            </span>
          </div>
          <div className="flex flex-wrap gap-x-3 gap-y-1">
            {cat.sources.map((s) => {
              const on = srcSel.includes(s.id);
              const full = !on && srcSel.length >= cat.active_source_limit;
              return (
                <label
                  key={s.id}
                  className={`flex items-center gap-1.5 text-[11px] select-none ${
                    full ? "text-zinc-600" : "text-zinc-300 cursor-pointer"
                  }`}
                  title={full ? `Already at the ${cat.active_source_limit}-source limit` : s.name}
                >
                  <input
                    type="checkbox"
                    checked={on}
                    disabled={full}
                    onChange={() =>
                      setSrcSel((cur) =>
                        cur.includes(s.id) ? cur.filter((x) => x !== s.id) : [...cur, s.id]
                      )
                    }
                  />
                  {s.color && (
                    <span className="h-2 w-2 rounded-full shrink-0" style={{ background: s.color }} />
                  )}
                  {SOURCE_NAMES[s.id] ?? s.name}
                  {!s.enabled && <span className="text-[9px] text-amber-400/80">off</span>}
                </label>
              );
            })}
          </div>
          <div className="flex items-center gap-2 text-[10px] text-zinc-600 flex-wrap">
            <span>Changing these applies to the next search.</span>
            <button
              className="btn-ghost !py-0.5 !px-1.5 text-[10px]"
              onClick={async () => {
                try {
                  await api.saveConfig({ cover_sources: srcSel, cover_country: country });
                  toast.success(`Saved as the default cover search: ${srcSel.length} source(s), ${country.toUpperCase()}`);
                  setCat((c) => (c ? { ...c, saved_sources: srcSel, saved_country: country } : c));
                } catch (e) {
                  toast.error(String(e));
                }
              }}
              title="Make this source list + region the default for every future cover search"
            >
              <Check className="h-3 w-3" /> Save as default
            </button>
          </div>
        </div>
      )}

      <div className="p-4">
        {error && <div className="text-red-400 text-sm p-3 bg-red-950/40 rounded-lg border border-red-900">{error}</div>}
        {loading && (
          <div className="text-zinc-500 text-sm flex items-center gap-2 p-3">
            <Loader2 className="h-4 w-4 animate-spin" /> Searching cover sources…
          </div>
        )}
        {!loading && results && results.length === 0 && !error && (
          <div className="text-zinc-500 text-sm p-3">No covers found for this query.</div>
        )}
        {results && results.length > 0 && provider && provider !== "cov" && (
          <div className="text-[11px] text-amber-400/90 pb-2">
            covers.musichoarders.xyz had nothing for this query — via{" "}
            {SOURCE_NAMES[provider] ?? provider}
          </div>
        )}
        {results && results.length > 0 && (
          <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
            {results.map((r, i) => {
              // The backend's own probe of the image wins; the image the
              // browser loaded is the fallback for the rows past its probe
              // limit, and the CDN URL's hint is the last resort (marked as an
              // estimate — sources like Apple carry no size in the URL at all).
              const big = r.big || r.small;
              const size = resultSize(r, big ? sizes[big] : undefined);
              const low = size != null && size.w < target;
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
                        src={artUrl(r.small, { artist: r.artist, album: r.title })}
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
                      <span
                        className={`text-[10px] tabular-nums ${low ? "text-amber-400" : "text-zinc-500"}`}
                        title={
                          size?.real
                            ? "Measured from the full-size image"
                            : size
                              ? "Estimated from the image URL — the real size is unknown"
                              : "Size not known yet"
                        }
                      >
                        {size ? (size.real ? `${size.w}×${size.h}` : `~${size.w}px`) : "…"}
                        {low ? " · low" : ""}
                      </span>
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
    </Modal>
  );
}
