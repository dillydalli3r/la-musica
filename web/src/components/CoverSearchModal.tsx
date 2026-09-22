import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Image, Loader2, RefreshCw, ExternalLink, Check } from "lucide-react";
import { api, offlineFallback } from "../api";
import { useI18n } from "../lib/i18n";
import {
  autoCoverSearch,
  coverAlbumTrackCount,
  coverCheckedAgainst,
  coverIdentity,
  coverIdentityKey,
  coverQuery,
  coverSearchPath,
  coverSearchPhase,
  coverTerms,
  type CoverAnswer,
  type CoverQuery,
} from "../lib/coverSearch";
import { toast } from "../store";
import type { CoverResult, CoverSourceCatalog, TrackTags } from "../types";
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

/** The sentence that put a candidate where it is: the one the backend's policy
 *  ended its reasons on — for the winner, why it won; for a loser, why it lost
 *  (see mlo/cover_choice). A candidate the policy REJECTED says why it cannot
 *  be the automatic pick instead. "" when the backend sent neither (an older
 *  server, or a row that was never ranked). */
function candidateReason(r: CoverResult): string {
  if (r.rejected) return r.rejected;
  const reasons = r.reasons ?? [];
  return reasons.length ? reasons[reasons.length - 1]! : "";
}

interface Props {
  albumPath: string;
  artist: string;
  album: string;
  onClose: () => void;
  onApplied?: () => void;
  /** Audio filenames in the album folder: apply the chosen image to THESE
   *  tracks (one file, many tracks) instead of the album cover. A selection,
   *  so it is never the album's own track count. */
  tracks?: string[];
  /** How many tracks the album has, when the caller knows — the third fact the
   *  server checks a candidate's own release against (`tracks=` in the search;
   *  a karaoke or other-album row can carry the same artist and title). Omit it
   *  and nothing is verified against a count. */
  trackCount?: number;
  /** The album's MusicBrainz release-group MBID, when the page knows it — the
   *  identity the Cover Art Archive is asked about for the group's stand-in.
   *  Without it that fallback can only answer for artist/album. */
  releaseGroupMbid?: string;
  /** The album's own MusicBrainz RELEASE id, when the page knows it: with it
   *  the Cover Art Archive is asked for the release's own front cover, which
   *  the policy prefers above every other candidate. */
  releaseMbid?: string;
  /** The import wizard's album — a folder the library does not list yet, whose
   *  cover the server writes only to a request that says so (the same opt-in
   *  every other call the wizard makes passes). Left out, "Use this cover" is
   *  refused with "album outside music folder" and the folder keeps the art it
   *  arrived with, however well the row was picked. */
  staged?: boolean;
  /** Candidates the import already fetched and staged (`cover_review` on):
   *  the modal opens showing these instead of searching, which is what turns
   *  "review" into a single pick. Absent → search as before. */
  initialResults?: CoverResult[];
  /** Who answered that staged fetch (the badge next to the grid). */
  initialProvider?: string | null;
  /** The staged fetch's own pick and its source notes (`covers.chosen` /
   *  `covers.notes` in the review entry), so the pick the import already made
   *  is shown as such without re-searching. */
  initialChosen?: CoverResult | null;
  initialNotes?: string[];
}

export default function CoverSearchModal({ albumPath, artist, album, onClose, onApplied, tracks, trackCount, releaseGroupMbid, releaseMbid, staged = false, initialResults, initialProvider, initialChosen, initialNotes }: Props) {
  const { t } = useI18n();
  const qc = useQueryClient();
  // The album's own track count for the identity check. The wizard states the
  // release it is importing; an album page passes none, but its own payload is
  // already in the app's cache under the folder this finder was opened for —
  // reading it costs no request, and it is read the way the SERVER reads it
  // (the recorded manifest, else the files' TRACKTOTAL tag; never the folder's
  // file count, which a partial import would make contradict the right cover).
  // A miss leaves the count unknown and nothing is verified against one.
  const cachedAlbum = qc.getQueryData<{
    expected_tracks?: readonly unknown[];
    tracks?: readonly { tags?: TrackTags | null }[];
  }>(["album", albumPath]);
  const albumTracks = trackCount ?? coverAlbumTrackCount(cachedAlbum);
  // The album's identity as the page that opened the finder knows it: the
  // artist and album tags it holds, plus whichever MusicBrainz ids it was
  // given. A release id when there is one, its release group otherwise.
  const identity = coverIdentity({ artist, album, releaseGroupMbid, releaseMbid, tracks: albumTracks });
  const [qArtist, setQArtist] = useState(identity.artist);
  const [qAlbum, setQAlbum] = useState(identity.album);
  // The fields follow the album's own tags until the user edits them: a page
  // whose data lands after the finder opens must not leave the finder
  // searching for blanks, and an edit must never be overwritten.
  const edited = useRef(false);
  // The answer AND the query it is the answer to (`CoverAnswer`). The pick,
  // the notes and the "what was searched" line all come from it, so a stored
  // answer can never be shown as the answer to a different question. Seeded by
  // the candidates the import already staged — those ARE the answer.
  const [answer, setAnswer] = useState<CoverAnswer | null>(
    initialResults?.length
      ? {
          query: coverQuery(identity),
          results: initialResults,
          provider: initialProvider ?? null,
          chosen: initialChosen ?? null,
          notes: initialNotes ?? [],
          // Staged candidates never went through a search of ours, so what the
          // import checked them against is not in this reply: said to be
          // unknown rather than restated as if we had verified it.
          identity: null,
          cached: null,
        }
      : null
  );
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // What the last attempt asked (the in-flight one included). A spinner and an
  // error are ABOUT a query — never about whatever the fields say now.
  const [lastQuery, setLastQuery] = useState<CoverQuery | null>(null);
  // The identity the automatic search has already asked (`coverIdentityKey`).
  const asked = useRef<string | null>(null);
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
  // False until `/api/cover/sources` has answered (or failed). The automatic
  // search waits for it: a request sent before the source list is known
  // carries no `sources` and no `country`, i.e. a DIFFERENT question from the
  // one the Search button asks with the picker's own values.
  const [sourcesReady, setSourcesReady] = useState(false);
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
      .catch(() => {})
      // Either way the automatic search may go: an unanswerable catalogue must
      // not leave the finder waiting, and with no override the server resolves
      // the same saved defaults itself.
      .finally(() => setSourcesReady(true));
  }, []);

  // The album's own tags reach the fields when they arrive after the finder
  // opened (a page still loading its album), and stop the moment the user
  // types: an edit is the user's, and it is what the Search button sends.
  useEffect(() => {
    if (edited.current) return;
    setQArtist(identity.artist);
    setQAlbum(identity.album);
  }, [identity.artist, identity.album]);

  // What the finder is showing, decided in ONE place (lib/coverSearch): the
  // states are mutually exclusive, so "none found" can only ever be a real
  // zero-candidate answer, and a spinner or an error is never dressed as one.
  const queryNow = coverQuery(
    coverIdentity({ artist: qArtist, album: qAlbum, releaseGroupMbid, releaseMbid, tracks: albumTracks }),
    { sources: srcSel, country }
  );
  const phase = coverSearchPhase({ query: queryNow, loading, error, answer, lastQuery });
  const outcome = phase.kind === "ready" ? phase.answer : null;
  const rows = outcome ? outcome.results : null;
  /** The terms a query asked for — the "what was searched" line. Built from
   *  the ANSWER's own query, never from the fields as they stand now: after an
   *  edit those describe a different question. */
  const termsOf = (q: CoverQuery) =>
    coverTerms(q)
      .map((term) => t(`cover.term.${term.kind}`, { value: term.value }))
      .join(" · ");

  /** Measure each result's big image (the one "apply" would download). */
  useEffect(() => {
    if (!rows?.length) return;
    let dead = false;
    for (const r of rows) {
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
  }, [rows]);

  /** Ask ONE question and file the answer under the query it answers. The
   *  automatic search and the Search button both come through here with a
   *  query built by `coverQuery`, so the two paths cannot send different
   *  parameters. A click always re-asks — the finder keeps no answers of its
   *  own, so a re-search is never short-circuited by a stored one; the only
   *  stored answer in the app is the offline copy, keyed by the query and
   *  consulted only when a request gets no answer at all. */
  const runQuery = async (q: CoverQuery) => {
    setLoading(true);
    setError(null);
    setSelected(null);
    setLastQuery(q);
    try {
      const r = await api.coverSearch(q);
      const cached = offlineFallback();
      const key = coverSearchPath(q);
      setAnswer({
        query: q,
        results: r.results ?? [],
        provider: r.provider ?? null,
        chosen: r.chosen ?? null,
        notes: r.notes ?? [],
        // What the server says it verified the rows against — its own words,
        // not ours restated: it is the answer to "why was this row rejected".
        identity: r.identity ?? null,
        // An answer the offline copy supplied is recorded as such: a real
        // answer, but not a fresh one, and the finder says which.
        cached: cached && (cached.key === key || cached.key.endsWith(key)) ? { at: cached.at } : null,
      });
    } catch (e) {
      // The server's or the provider's own words, verbatim. Nothing here may
      // become "no covers found": a request that did not answer is not an
      // answer.
      setError(String(e));
      setAnswer(null);
    } finally {
      setLoading(false);
    }
  };

  // The finder's own search — fired as soon as it can ask exactly what the
  // Search button would ask: the album's identity AND the source list. One
  // search per identity (the ref makes a re-render, or StrictMode's doubled
  // effect, a no-op), none when the caller brought staged candidates (they ARE
  // the answer), and none when the album has no identity to ask about — the
  // blocked state says what is missing instead.
  const auto = autoCoverSearch({
    identity,
    sources: srcSel,
    country,
    sourcesReady,
    staged: Boolean(initialResults?.length),
    asked: asked.current,
  });
  useEffect(() => {
    if (!auto) return;
    if (asked.current === coverIdentityKey(identity)) return;
    asked.current = coverIdentityKey(identity);
    void runQuery(auto);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [auto ? coverSearchPath(auto) : ""]);

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
      }, staged);
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
              className="h-24 w-24 rounded-lg object-cover"
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
              className="h-11 w-11 rounded object-cover bg-zinc-950"
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
          onChange={(e) => {
            edited.current = true;
            setQArtist(e.target.value);
          }}
          onKeyDown={(e) => e.key === "Enter" && runQuery(queryNow)}
        />
        <input
          className="input !w-52"
          placeholder="Album"
          value={qAlbum}
          onChange={(e) => {
            edited.current = true;
            setQAlbum(e.target.value);
          }}
          onKeyDown={(e) => e.key === "Enter" && runQuery(queryNow)}
        />
        <button className="btn-primary !py-1.5" onClick={() => runQuery(queryNow)} disabled={loading}>
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
        {/* ONE state at a time, decided by lib/coverSearch: what is missing,
            what failed (in the server's or the provider's own words), what is
            being asked, a REAL answer that held nothing, or a real answer with
            candidates. "No covers found" is only ever the fourth. */}
        {phase.kind === "blocked" && (
          <div className="text-sm p-3 rounded-lg border border-amber-900/60 bg-amber-950/20 text-amber-200 space-y-1">
            <div className="font-medium">{t("cover.blocked")}</div>
            <div className="text-[11px] text-amber-200/80">{t("cover.blocked_missing")}</div>
            <ul className="text-[11px] text-amber-200/80 list-disc pl-5 space-y-0.5">
              {phase.gaps.map((gap) => (
                <li key={gap}>{t(`cover.missing.${gap}`)}</li>
              ))}
            </ul>
            <div className="text-[11px] text-amber-200/70">{t("cover.blocked_hint")}</div>
          </div>
        )}
        {phase.kind === "error" && (
          <div className="text-sm p-3 rounded-lg border border-red-900 bg-red-950/40 text-red-400 space-y-2">
            <div className="font-medium">{t("cover.failed")}</div>
            {/* The server's or the provider's own words — never paraphrased
                into "none found". */}
            <div className="text-[12px] break-words">{phase.message}</div>
            {phase.query && (
              <div className="text-[11px] text-red-300/70">
                {t("cover.answered", { terms: termsOf(phase.query) })}
              </div>
            )}
            <button className="btn-ghost !py-1 !px-2 text-xs" onClick={() => runQuery(phase.query ?? queryNow)}>
              <RefreshCw className="h-3 w-3" /> {t("cover.retry")}
            </button>
          </div>
        )}
        {phase.kind === "searching" && (
          <div className="text-sm p-3 space-y-1">
            <div className="text-zinc-500 flex items-center gap-2">
              <Loader2 className="h-4 w-4 animate-spin" /> {t("cover.searching")}
            </div>
            <div className="text-[11px] text-zinc-600">
              {t("cover.answered", { terms: termsOf(phase.query) })}
            </div>
          </div>
        )}
        {phase.kind === "empty" && (
          <div className="text-sm p-3 rounded-lg border border-border bg-raise/40 space-y-1">
            <div className="text-zinc-300 font-medium">{t("cover.empty")}</div>
            {/* WHAT was searched: the terms the answer's own query asked for,
                not the fields as they stand now. */}
            <div className="text-[11px] text-zinc-500">
              {t("cover.answered", { terms: termsOf(phase.query) })}
            </div>
            <div className="text-[11px] text-zinc-500">{t("cover.empty_hint")}</div>
            {/* An answer that came off disk is said to be one: a stale zero
                must not read as a fresh "there is nothing". */}
            {phase.cached && (
              <div className="text-[11px] text-amber-400/90">
                {t("cover.offline")}
                {phase.cached.at ? ` · ${new Date(phase.cached.at).toLocaleString()}` : ""}
              </div>
            )}
            {phase.notes.length > 0 && (
              <details className="text-[11px] text-zinc-500">
                <summary className="cursor-pointer select-none">{t("cover.source_notes")}</summary>
                <ul className="mt-1 space-y-0.5">
                  {phase.notes.map((n, i) => (
                    <li key={i} className="truncate" title={n}>
                      {n}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        )}
        {outcome && outcome.cached && (
          <div className="text-[11px] text-amber-400/90 pb-2">
            {t("cover.offline")}
            {outcome.cached.at ? ` · ${new Date(outcome.cached.at).toLocaleString()}` : ""}
          </div>
        )}
        {outcome && (
          <div className="text-[11px] text-zinc-500 pb-2">
            {t("cover.answered", { terms: termsOf(outcome.query) })}
          </div>
        )}
        {outcome && outcome.provider && outcome.provider !== "cov" && (
          <div className="text-[11px] text-amber-400/90 pb-2">
            covers.musichoarders.xyz had nothing for this query — via{" "}
            {SOURCE_NAMES[outcome.provider] ?? outcome.provider}
          </div>
        )}
        {outcome && (
          <div className="mb-3 rounded-lg border border-border bg-raise/60 p-3 space-y-1">
            <div className="flex items-center gap-2 flex-wrap text-sm">
              <span className="text-[10px] font-semibold uppercase tracking-wider text-accent-soft bg-accent/10 border border-accent/25 rounded px-1.5 py-0.5">
                {t("cover.best_pick")}
              </span>
              {outcome.chosen ? (
                <>
                  <span className="font-medium">
                    {SOURCE_NAMES[outcome.chosen.source] ?? outcome.chosen.source}
                  </span>
                  <span className="text-zinc-400 tabular-nums">
                    {outcome.chosen.width && outcome.chosen.height
                      ? `${outcome.chosen.width}×${outcome.chosen.height}px`
                      : ""}
                  </span>
                  <button
                    className="btn-ghost !py-0.5 !px-2 text-[11px] tap ml-auto"
                    onClick={() => setSelected(outcome.chosen)}
                    title={candidateReason(outcome.chosen)}
                  >
                    <Check className="h-3 w-3" /> {t("cover.best_pick_use")}
                  </button>
                </>
              ) : (
                <span className="text-amber-400">{t("cover.no_pick")}</span>
              )}
            </div>
            {outcome.chosen && (
              <div className="text-[11px] text-zinc-400">
                {t("cover.pick_reason")}: {candidateReason(outcome.chosen)}
              </div>
            )}
            {/* What every row on this screen was VERIFIED against — the
                server's own echo, so a row rejected two lines below ("a
                different artist's release") can be read against the identity
                it contradicts rather than taken on faith. */}
            {outcome.identity && (outcome.identity.artist || outcome.identity.album) && (
              <div className="text-[11px] text-zinc-500">
                {t("cover.checked_against", { identity: coverCheckedAgainst(outcome.identity) })}
              </div>
            )}
            {/* Every row rejected: the pick is empty and the rows are still
                listed (each carrying why), which without this line reads as
                "the search is still deciding". They stay selectable — the
                manual apply is warning-only on purpose. */}
            {!outcome.chosen && outcome.results.length > 0 && (
              <div className="text-[11px] text-amber-400/90">
                {t("cover.all_rejected", { count: outcome.results.length })}
              </div>
            )}
            {outcome.notes.length > 0 && (
              <details className="text-[11px] text-zinc-500">
                <summary className="cursor-pointer select-none">{t("cover.source_notes")}</summary>
                <ul className="mt-1 space-y-0.5">
                  {outcome.notes.map((n, i) => (
                    <li key={i} className="truncate" title={n}>
                      {n}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        )}
        {outcome && (
          <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
            {outcome.results.map((r, i) => {
              // The backend's own probe of the image wins; the image the
              // browser loaded is the fallback for the rows past its probe
              // limit, and the CDN URL's hint is the last resort (marked as an
              // estimate — sources like Apple carry no size in the URL at all).
              const big = r.big || r.small;
              const size = resultSize(r, big ? sizes[big] : undefined);
              const low = size != null && size.w < target;
              const isPick = !!outcome.chosen && !!(outcome.chosen.big || outcome.chosen.small) &&
                (outcome.chosen.big || outcome.chosen.small) === big;
              const why = candidateReason(r);
              return (
                <button
                  key={`${r.source}-${i}`}
                  className={`group text-left rounded-lg overflow-hidden border transition-colors ${
                    selected === r
                      ? "border-accent ring-1 ring-accent"
                      : isPick
                        ? "border-accent/50"
                        : "border-transparent hover:border-zinc-600"
                  } ${r.rejected ? "opacity-70" : ""} bg-raise`}
                  onClick={() => setSelected(r)}
                  title={why || r.title || undefined}
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
                      {isPick && (
                        <span className="text-[9px] font-semibold uppercase tracking-wider text-accent-soft">
                          {t("cover.best_pick")}
                        </span>
                      )}
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
                    {/* WHY this row is where it is: the policy's own sentence —
                        the deciding reason for the pick, the losing reason for
                        the rest, the rejection for one that cannot be the
                        automatic pick (it can still be applied by hand). */}
                    {why && (
                      <div
                        className={`text-[10px] leading-snug line-clamp-2 ${
                          r.rejected ? "text-amber-500/80" : "text-zinc-500"
                        }`}
                        title={why}
                      >
                        {why}
                      </div>
                    )}
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
