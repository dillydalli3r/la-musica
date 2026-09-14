import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useSearchParams, Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  UploadCloud, ExternalLink, Check, ChevronLeft, ChevronRight, ChevronDown, Wand2,
  Plus, Trash2, Disc3, FolderOpen, X,
} from "lucide-react";
import { api } from "../api";
import { toast } from "../store";
import LyricsViewer, { parseLrc } from "../components/LyricsViewer";
import type { MBRelease, MatchSuggestion, Track } from "../types";

const STEPS = ["Select & separate", "Links", "Match", "Genres", "Lyrics", "Advisory", "Finish"];

// Everything the importer accepts: audio, all common image formats, and the
// sidecars the optimizer understands (.lrc, .cue, .log, .accurip).
const ALLOWED = /\.(flac|mp3|m4a|mp4|ogg|opus|wav|aac|wv|ape|alac|aiff|aif|dsf|dff|mka)$|\.(jpg|jpeg|png|webp|bmp|gif|tiff|tif|avif|heic|heif|jxl|svg)$|\.(lrc|cue|log|accurip)$/i;
const AUDIO_RE = /\.(flac|mp3|m4a|mp4|ogg|opus|wav|aac|wv|ape|alac|aiff|aif|dsf|dff|mka)$/i;
const DISC_RE = /^(cd|disc|disk)\s*\d+$/i;

interface ImportFile {
  file: File | null; // null = native pick (already on disk)
  relPath: string;
}

interface AlbumGroup {
  name: string;
  root: string; // rel path of the album dir under the drop/native root ("" = root itself)
  files: ImportFile[];
}

function dirOf(relPath: string): string {
  const i = relPath.lastIndexOf("/");
  return i === -1 ? "" : relPath.slice(0, i);
}

function baseName(p: string): string {
  const parts = p.split("/").filter(Boolean);
  return parts[parts.length - 1] ?? "";
}

/** Extract a MusicBrainz ID from an ID or a musicbrainz.org URL. */
function extractMbid(value: string): string | null {
  const m = value.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
  return m ? m[0].toLowerCase() : null;
}

/** Retry a transient network failure (MusicBrainz rate limits / blips). */
async function withRetry<T>(fn: () => Promise<T>, tries = 3, baseDelay = 1200): Promise<T> {
  let lastErr: unknown;
  for (let i = 0; i < tries; i++) {
    try {
      return await fn();
    } catch (e) {
      lastErr = e;
      if (i < tries - 1) await new Promise((r) => setTimeout(r, baseDelay * (i + 1)));
    }
  }
  throw lastErr;
}

/** Parse a disc number from a tag like "1", "1/2" or "Disc 2". */
function parseDisc(v: string | null | undefined): number | null {
  if (!v) return null;
  const m = String(v).trim().match(/(\d+)/);
  const n = m ? parseInt(m[1], 10) : NaN;
  return Number.isNaN(n) ? null : n;
}

/** Group rows into per-disc sections (unmatched rows last). */
function groupByDisc<T>(rows: T[], discOf: (r: T) => number | null): { disc: number | null; rows: T[] }[] {
  const groups = new Map<number | null, T[]>();
  for (const r of rows) {
    const d = discOf(r);
    if (!groups.has(d)) groups.set(d, []);
    groups.get(d)!.push(r);
  }
  return [...groups.keys()]
    .sort((a, b) => {
      if (a === null) return 1;
      if (b === null) return -1;
      return a - b;
    })
    .map((d) => ({ disc: d, rows: groups.get(d)! }));
}

/** Detect album boundaries inside an import set.
 *  An album = any dir with immediate audio children (disc-like dirs such as
 *  CD1/Disc 2 merge into their parent). Group names fall back to
 *  "Parent - Album" when albums sit inside artist folders. */
function detectAlbums(imports: ImportFile[], fallbackName: string): AlbumGroup[] {
  const audio = imports.filter((f) => AUDIO_RE.test(f.relPath));
  if (!audio.length) {
    return [{ name: fallbackName || "New Album", root: "", files: [...imports] }];
  }
  const dirsWithAudio = new Set(audio.map((f) => dirOf(f.relPath)));

  const isDiscDir = (d: string) => d !== "" && DISC_RE.test(baseName(d));
  const roots = new Set<string>();
  for (const d of dirsWithAudio) {
    if (!d) {
      roots.add("");
      continue;
    }
    if (isDiscDir(d)) {
      const parent = dirOf(d);
      const siblings = [...dirsWithAudio].filter((x) => x && dirOf(x) === parent);
      if (siblings.length && siblings.every((s) => isDiscDir(s))) {
        roots.add(parent || "");
      } else {
        roots.add(d);
      }
    } else {
      roots.add(d);
    }
  }
  if (!roots.size) roots.add("");

  const ordered = [...roots].sort((a, b) => {
    const da = a ? a.split("/").length : 0;
    const db = b ? b.split("/").length : 0;
    return da - db || a.localeCompare(b);
  });

  // The wrapper folder the user dropped/picked (e.g. "Rips/"). Albums sitting
  // directly inside it are named by their own folder only.
  const firstSeg = imports[0]?.relPath.split("/")[0] ?? "";
  const allShare = imports.every((f) => f.relPath.split("/")[0] === firstSeg);
  const dropRoot = firstSeg && allShare ? firstSeg : "";

  const nameFor = (d: string): string => {
    if (!d) return fallbackName || "New Album";
    const parent = dirOf(d);
    const base = baseName(d);
    if (!parent || parent === dropRoot) return base;
    return `${baseName(parent)} - ${base}`;
  };

  const groups = new Map<string, ImportFile[]>();
  const rootsDeep = [...roots].sort((a, b) => b.split("/").length - a.split("/").length);
  for (const f of imports) {
    const fd = dirOf(f.relPath);
    const root = rootsDeep.find((r) => r === "" || fd === r || fd.startsWith(`${r}/`)) ?? ordered[0];
    if (!groups.has(root)) groups.set(root, []);
    groups.get(root)!.push(f);
  }

  return ordered.map((r) => ({
    name: nameFor(r),
    root: r,
    files: groups.get(r) ?? [],
  }));
}

export default function ImportWizard() {
  const [params, setParams] = useSearchParams();
  const albumParam = params.get("album");
  const initialAlbum = albumParam ?? null;
  const [step, setStep] = useState(initialAlbum ? 1 : 0);

  const [albumPath, setAlbumPath] = useState<string | null>(initialAlbum);
  const [albumName, setAlbumName] = useState("");
  const [source, setSource] = useState<"web" | "native">("web");
  const [nativeRoot, setNativeRoot] = useState<string | null>(null);
  const [albums, setAlbums] = useState<AlbumGroup[]>([]);
  const [mediaType, setMediaType] = useState<string>(""); // "" unsure | "CD" | "Digital Media"
  const [uploaded, setUploaded] = useState<{ name: string; path: string }[]>([]);
  const [albumIndex, setAlbumIndex] = useState(0);
  const [uploading, setUploading] = useState(false);

  const [mbLink, setMbLink] = useState("");
  const [rymLink, setRymLink] = useState("");
  const [rymValid, setRymValid] = useState<boolean | null>(null);
  const [detectedFromTags, setDetectedFromTags] = useState(false);
  const [mbSearch, setMbSearch] = useState("");
  const [searchHits, setSearchHits] = useState<any[]>([]);
  const [release, setRelease] = useState<MBRelease | null>(null);
  const [releaseId, setReleaseId] = useState("");
  const [suggestions, setSuggestions] = useState<MatchSuggestion[]>([]);
  const [genres, setGenres] = useState<Record<string, string>>({});
  const [discGenres, setDiscGenres] = useState<Record<number, string>>({});
  const [genreAddValues, setGenreAddValues] = useState<Record<string, string>>({});
  const [genreLimit, setGenreLimit] = useState<number | null>(null); // null = all
  const [genreSource, setGenreSource] = useState<string | null>(null);
  const [collapsedDiscs, setCollapsedDiscs] = useState<Set<number | null>>(new Set());
  const [advisory, setAdvisory] = useState<Record<string, string>>({});
  const [instrumental, setInstrumental] = useState<Record<string, string>>({});
  const [lyricsDrafts, setLyricsDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [fetchStatus, setFetchStatus] = useState<string | null>(null);
  const qc = useQueryClient();

  const { data: lib } = useQuery({ queryKey: ["library"], queryFn: api.library });

  const trackList = useMemo(() => {
    if (!albumPath || !lib) return [];
    for (const a of lib.artists)
      for (const al of a.albums)
        if (al.path === albumPath) return al.tracks;
    return [];
  }, [albumPath, lib]);

  // Refresh the library when the wizard opens so tags written externally
  // (e.g. Picard) since the last fetch are picked up.
  useEffect(() => {
    qc.refetchQueries({ queryKey: ["library"] });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Auto-recognize a pasted MusicBrainz release URL/ID.
  useEffect(() => {
    const id = extractMbid(mbLink);
    if (id && id !== releaseId) setReleaseId(id);
  }, [mbLink, releaseId]);

  // Detection: payload tags first, then a live on-disk scan (catches fresh
  // tags and non-standard key variants such as MUSICBRAINZ_ALBUM_ID).
  const detectReleaseId = async (): Promise<string | null> => {
    const tagged = trackList.find((t) => t.tags?.MUSICBRAINZ_ALBUMID);
    const fromPayload = tagged ? extractMbid(tagged.tags.MUSICBRAINZ_ALBUMID ?? "") : null;
    if (fromPayload) return fromPayload;
    if (!albumPath) return null;
    try {
      const r = await api.mbDetect(albumPath);
      return r.mbid ?? null;
    } catch {
      return null;
    }
  };

  // Auto-detect a Picard-tagged MusicBrainz release and fetch it
  // automatically when the Links step opens.
  const autoDetected = useRef(false);
  const [detectStatus, setDetectStatus] = useState<"idle" | "scanning" | "found" | "none">("idle");
  useEffect(() => {
    if (step !== 1 || !albumPath || releaseId || autoDetected.current) return;
    let cancelled = false;
    autoDetected.current = true;
    setDetectStatus("scanning");
    (async () => {
      const id = await detectReleaseId();
      if (cancelled) return;
      if (!id) {
        // not found now — allow retry when the library refreshes
        autoDetected.current = false;
        setDetectStatus("none");
        return;
      }
      setDetectStatus("found");
      setDetectedFromTags(true);
      setMbLink(`https://musicbrainz.org/release/${id}`);
      setReleaseId(id);
      pickRelease(id);
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step, albumPath, trackList, releaseId]);

  // Manual "Detect from tags" — always available, one click.
  const detectFromTags = async () => {
    if (!albumPath) {
      toast("No album selected");
      return;
    }
    setDetectStatus("scanning");
    try {
      const id = await detectReleaseId();
      if (!id) {
        setDetectStatus("none");
        toast("No MusicBrainz release ID found in the track tags");
        return;
      }
      setDetectStatus("found");
      setDetectedFromTags(true);
      setMbLink(`https://musicbrainz.org/release/${id}`);
      setReleaseId(id);
      pickRelease(id);
    } catch (e) {
      setDetectStatus("none");
      toast(String(e));
    }
  };

  // Fetch button: falls back to tag detection when the field is empty.
  const handleFetch = async () => {
    let id = releaseId || extractMbid(mbLink) || "";
    if (!id && albumPath) {
      const detected = await detectReleaseId();
      if (detected) {
        setDetectedFromTags(true);
        setMbLink(`https://musicbrainz.org/release/${detected}`);
        id = detected;
      }
    }
    pickRelease(id);
  };

  // Debounced RYM link validation.
  useEffect(() => {
    if (!rymLink.trim()) {
      setRymValid(null);
      return;
    }
    const t = setTimeout(async () => {
      try {
        const r = await api.rymValidate(rymLink.trim());
        setRymValid(r.valid);
      } catch {
        setRymValid(false);
      }
    }, 400);
    return () => clearTimeout(t);
  }, [rymLink]);

  const defaultTrackName = (p: string) => {
    const base = p.split("/").pop() ?? "";
    return base.replace(/^\d+\s*[-._]\s*/, "").replace(/\.[^.]+$/, "").trim();
  };

  const currentAlbumName = uploaded[albumIndex]?.name ?? albumPath?.split("/").pop() ?? "";

  // Tracks for the per-track steps (genres/lyrics/advisory). Prefer the
  // library payload, but fall back to the matched suggestions — the library
  // only contains the exact album path, while matching scans the folder
  // directly (nested/multi-album structures).
  // Third fallback: direct folder scan — always reflects what's on disk.
  const [scannedTracks, setScannedTracks] = useState<Track[]>([]);
  useEffect(() => {
    setScannedTracks([]);
    if (trackList.length || suggestions.length || !albumPath) return;
    let cancelled = false;
    api.scanTracks(albumPath)
      .then((r) => {
        if (cancelled) return;
        setScannedTracks(
          r.tracks.map((t) => ({
            path: t.path,
            file: t.file,
            issues: [],
            values: {},
            audit: null,
            log_grade: null,
            lyrics_embedded: false,
            lyrics_lrc: false,
            unreadable: false,
            tech: t.tech ?? {},
            tags: t.tags ?? {},
            grade_pass: false,
            lyrics_present: false,
          })) as Track[]
        );
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [albumPath, trackList.length, suggestions.length]);

  const stepTracks: Track[] = useMemo(() => {
    if (trackList.length) return trackList;
    if (suggestions.length) {
      return suggestions.map((s) => ({
        path: s.local,
        file: s.file,
        issues: [],
        values: {},
        audit: null,
        log_grade: null,
        lyrics_embedded: false,
        lyrics_lrc: false,
        unreadable: false,
        tech: {},
        tags: {},
        grade_pass: false,
        lyrics_present: false,
      }));
    }
    return scannedTracks;
  }, [trackList, suggestions, scannedTracks]);

  // ---- per-disc grouping helpers (shared by Match and Genres steps) ----
  const suggByPath = useMemo(() => new Map(suggestions.map((s) => [s.local, s])), [suggestions]);

  const discOfTrack = (t: { path: string; tags?: { DISCNUMBER?: string | null } }): number | null => {
    const m = suggByPath.get(t.path)?.release_track;
    if (m) return m.disc;
    return parseDisc(t.tags?.DISCNUMBER);
  };

  const discOfSuggestion = (s: MatchSuggestion): number | null => {
    if (s.release_track) return s.release_track.disc;
    const t = trackList.find((x) => x.path === s.local);
    return parseDisc(t?.tags?.DISCNUMBER);
  };

  const toggleDisc = (d: number | null) =>
    setCollapsedDiscs((s) => {
      const next = new Set(s);
      if (next.has(d)) next.delete(d);
      else next.add(d);
      return next;
    });

  // ---------------- Step 0: selection + separation ----------------
  const adoptImports = (list: ImportFile[], fallback: string) => {
    if (list.length && fallback && !albumName) setAlbumName(fallback);
    setAlbums(detectAlbums(list, fallback || albumName || "New Album"));
  };

  const handleFiles = (list: FileList | File[]) => {
    const arr: ImportFile[] = [];
    for (const f of Array.from(list)) {
      const rel = (f as any).webkitRelativePath
        ? (f as any).webkitRelativePath.replace(/\\/g, "/")
        : f.name.replace(/\\/g, "/");
      if (ALLOWED.test(rel)) arr.push({ file: f, relPath: rel });
    }
    adoptImports(arr, arr[0]?.relPath.split("/")[0] ?? "");
  };

  const walkEntry = async (entry: any, prefix: string, out: ImportFile[]) => {
    if (entry.isFile) {
      const f = await new Promise<File>((resolve, reject) => entry.file(resolve, reject));
      out.push({ file: f, relPath: prefix ? `${prefix}/${f.name}` : f.name });
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      const entries: any[] = await new Promise((resolve, reject) => {
        const all: any[] = [];
        const readBatch = () =>
          reader.readEntries((batch: any[]) => {
            if (!batch.length) return resolve(all);
            all.push(...batch);
            readBatch();
          }, reject);
        readBatch();
      });
      for (const e of entries) await walkEntry(e, prefix ? `${prefix}/${entry.name}` : entry.name, out);
    }
  };

  const handleDrop = async (items: DataTransferItemList) => {
    const out: ImportFile[] = [];
    const roots: any[] = [];
    for (const item of Array.from(items)) {
      const entry = item.webkitGetAsEntry?.();
      if (entry) roots.push(entry);
    }
    if (roots.some((r) => r.isDirectory)) {
      for (const root of roots) {
        await walkEntry(root, root.isDirectory ? root.name : "", out);
      }
      adoptImports(out.filter((o) => ALLOWED.test(o.relPath)), roots.find((r) => r.isDirectory)?.name ?? "");
    } else {
      const files = roots.filter((r) => r.isFile).map((r) => r.file) as File[];
      handleFiles(files);
    }
  };

  const pickFolderBrowser = async () => {
    try {
      // Preferred: File System Access API (Chromium/Edge).
      const picker = (window as any).showDirectoryPicker;
      if (picker) {
        const dir = await picker({ mode: "read" });
        const out: ImportFile[] = [];
        const walk = async (entry: any, prefix: string) => {
          for await (const e of entry.values()) {
            if (e.kind === "file") {
              const f = await e.getFile();
              out.push({ file: f, relPath: prefix ? `${prefix}/${f.name}` : f.name });
            } else if (e.kind === "directory") {
              await walk(e, prefix ? `${prefix}/${e.name}` : e.name);
            }
          }
        };
        await walk(dir, "");
        adoptImports(out.filter((o) => ALLOWED.test(o.relPath)), dir.name || "");
        return;
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError") toast(String(e));
      return;
    }
    // Fallback: webkitdirectory input (Chrome/Edge/Firefox/WebView2).
    document.getElementById("import-folder")?.click();
  };

  const pickFolderNative = async () => {
    const inTauri = !!(window as any).__TAURI_INTERNALS__;
    if (!inTauri) {
      // no native dialog in a plain browser — fall back to the folder input
      document.getElementById("import-folder")?.click();
      return;
    }
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const picked = await invoke<string | null>("pick_folder");
      if (!picked) return;
      const scan = await api.importScan(picked);
      setSource("native");
      setNativeRoot(picked);
      const list: ImportFile[] = scan.files
        .filter((f) => ALLOWED.test(f.relPath))
        .map((f) => ({ file: null, relPath: f.relPath }));
      adoptImports(list, picked.split(/[\\/]/).pop() ?? "");
    } catch (e) {
      toast(String(e));
    }
  };

  const renameGroup = (i: number, name: string) =>
    setAlbums((gs) => gs.map((g, j) => (j === i ? { ...g, name } : g)));

  const moveFile = (from: number, to: number, f: ImportFile) => {
    if (from === to) return;
    setAlbums((gs) =>
      gs.map((g, i) => {
        if (i === from) return { ...g, files: g.files.filter((x) => x !== f) };
        if (i === to) return { ...g, files: [...g.files, f] };
        return g;
      })
    );
  };

  const addAlbum = () =>
    setAlbums((gs) => [...gs, { name: `Album ${gs.length + 1}`, root: "", files: [] }]);

  const removeAlbum = (i: number) => {
    setAlbums((gs) => {
      if (gs.length <= 1) return gs;
      const removed = gs[i];
      const rest = gs.filter((_, j) => j !== i);
      if (removed.files.length) {
        rest[0] = { ...rest[0], files: [...removed.files, ...rest[0].files] };
      }
      return rest;
    });
  };

  const doImport = async () => {
    const groups = albums.filter((g) => g.name.trim() && g.files.length);
    if (!groups.length) {
      toast("Nothing to import — add files first");
      return;
    }
    setUploading(true);
    try {
      const results: { name: string; path: string }[] = [];
      for (const g of groups) {
        const name = g.name.trim();
        if (source === "web") {
          const filesToSend = g.files.filter((f) => f.file) as { file: File; relPath: string }[];
          if (!filesToSend.length) continue;
          const res = await api.importUpload(name, filesToSend);
          results.push({ name, path: res.album_path });
        } else {
          const src = nativeRoot ? (g.root ? `${nativeRoot}/${g.root}` : nativeRoot) : "";
          if (!src) continue;
          const res = await api.importIngest(src, name);
          results.push({ name, path: res.path });
        }
      }
      if (!results.length) {
        toast("Nothing to import");
        return;
      }
      setUploaded(results);
      setAlbumIndex(0);
      setAlbumPath(results[0].path);
      setStep(1);
      qc.invalidateQueries({ queryKey: ["library"] });
      toast(`Imported ${results.length} album${results.length > 1 ? "s" : ""}`);
    } catch (e) {
      toast(String(e));
    } finally {
      setUploading(false);
    }
  };

  // ---------------- Step 1: links ----------------
  const [searchMode, setSearchMode] = useState<"release" | "track" | "catno" | "barcode">("release");
  const doSearch = async (q?: string) => {
    const query = q ?? mbSearch;
    if (!query.trim()) return;
    setBusy(true);
    try {
      setSearchHits(await api.mbSearchReleases(query.trim(), searchMode));
    } catch (e) {
      toast(String(e));
    } finally {
      setBusy(false);
    }
  };

  const [artistQuery, setArtistQuery] = useState("");
  const [artistHits, setArtistHits] = useState<{ id: string; name: string; type?: string }[]>([]);
  const doArtistSearch = async () => {
    if (!artistQuery.trim()) return;
    setBusy(true);
    try {
      const hits = await api.mbSearchArtists(artistQuery.trim());
      setArtistHits(Array.isArray(hits) ? hits : []);
    } catch (e) {
      toast(String(e));
    } finally {
      setBusy(false);
    }
  };
  const applyArtist = (name: string) => {
    setArtistHits([]);
    setArtistQuery("");
    setMbSearch(`artist:"${name}"`);
    setSearchMode("release");
    doSearch(`artist:"${name}"`);
  };

  const pickRelease = async (id: string) => {
    if (!id) return;
    setBusy(true);
    setFetchStatus("Fetching release from MusicBrainz…");
    try {
      // MusicBrainz rate-limits and blips — retry the remote calls.
      const rel = await withRetry(() => api.mbRelease(id));
      setRelease(rel);
      setReleaseId(id);
      setFetchStatus("Matching local tracks to the release…");
      const matched = await api.mbMatch(albumPath!, id);
      setSuggestions(matched.suggestions);
      if (!matched.suggestions.length) {
        toast("No audio tracks found in this folder — check the album folder contains the music files");
      } else {
        toast(`Matched ${matched.suggestions.filter((s) => s.matched).length}/${matched.suggestions.length} tracks`);
      }
    } catch (e) {
      toast(String(e));
    } finally {
      setBusy(false);
      setFetchStatus(null);
    }
  };

  // Genres are imported manually in the Genres step — never auto-fetched.
  const importGenres = async () => {
    const rid = releaseId || extractMbid(mbLink) || "";
    if (!rid) {
      toast("Fetch the MusicBrainz release first (Links step)");
      return;
    }
    setBusy(true);
    setFetchStatus("Importing genres from MusicBrainz…");
    try {
      const cascade = await withRetry(() => api.mbGenres(rid, genreLimit ?? undefined));
      const byPos = new Map(cascade.per_track.map((t) => [`${t.disc}-${t.position}`, t.genres.join("; ")]));
      const g: Record<string, string> = {};
      let src: string | null = null;
      for (const s of suggestions) {
        const m = s.release_track;
        const key = m ? `${m.disc}-${m.position}` : "";
        g[s.local] = byPos.get(key) ?? "";
        const s2 = cascade.per_track.find((t) => t.title === m?.title)?.source;
        if (s2) src = s2;
      }
      setGenres(g);
      setGenreSource(src ?? "MusicBrainz");
      toast("Genres imported — review and edit below");
    } catch (e) {
      toast(String(e));
    } finally {
      setBusy(false);
      setFetchStatus(null);
    }
  };

  const nextFromLinks = async () => {
    const rid = releaseId || extractMbid(mbLink) || "";
    if (!albumPath || !rid) {
      toast("Enter a valid MusicBrainz release URL or ID first");
      return;
    }
    setBusy(true);
    try {
      await api.importCommit(albumPath, mbLink || `https://musicbrainz.org/release/${rid}`, rymValid ? rymLink : undefined);
      toast("Links saved to album");
      setStep(2);
    } catch (e) {
      toast(String(e));
    } finally {
      setBusy(false);
    }
  };

  // ---------------- Step 2: matching ----------------
  const setSuggestion = (path: string, disc: number, position: number) => {
    const m = release?.media.find((x) => x.disc === disc && x.position === position);
    setSuggestions((ss) => ss.map((s) => (s.local === path ? { ...s, matched: !!m, confidence: 1, release_track: m ?? null } : s)));
  };

  const confirmMatch = async () => {
    setBusy(true);
    setFetchStatus("Writing MusicBrainz metadata to files…");
    try {
      const writes: Record<string, Record<string, string | null>> = {};
      const albumArtist = (release?.artists ?? []).map((a) => a.name).join(", ") || null;
      const mediumFormat = release?.medium_formats?.[0] || mediaType || null;
      for (const s of suggestions) {
        const t = s.release_track;
        writes[s.local] = {
          // links / IDs
          MUSICBRAINZ_TRACKID: t?.recording_mbid ?? null,
          MUSICBRAINZ_ARTISTID: t?.artist_mbids?.[0] ?? null,
          MUSICBRAINZ_ALBUMID: release?.id ?? null,
          MUSICBRAINZ_RELEASEGROUPID: release?.release_group_id ?? null,
          MUSICBRAINZ_RELEASEID: release?.id ?? null,
          MUSICBRAINZ_ALBUMARTISTID: release?.artists?.[0]?.mbid ?? null,
          // metadata
          TITLE: t?.title ?? null,
          ARTIST: t?.artist_credit || (release?.artists ?? [])[0]?.name || null,
          ALBUM: release?.title ?? null,
          ALBUMARTIST: albumArtist,
          DATE: release?.date || null,
          TRACKNUMBER: t ? String(t.position).padStart(2, "0") : null,
          TRACKTOTAL: release?.media?.length ? String(release.media.length) : null,
          DISCNUMBER: t ? String(t.disc) : null,
          DISCTOTAL: release?.medium_count ? String(release.medium_count) : null,
          MEDIA: mediumFormat,
          RELEASETYPE: release?.release_type || null,
          RELEASECOUNTRY: release?.country || null,
          CATALOGNUMBER: release?.catalog_number || null,
          LABEL: release?.label || null,
        };
      }
      await api.mbAssign(writes);
      toast("MusicBrainz metadata written to files (titles, artists, album, dates, MBIDs)");
      setStep(3);
    } catch (e) {
      toast(String(e));
    } finally {
      setBusy(false);
      setFetchStatus(null);
    }
  };

  // ---------------- Step 3: genres ----------------
  // When the Genres step opens, prefill untouched tracks with their existing
  // GENRE tags so they are visible and editable right away.
  useEffect(() => {
    if (step !== 3) return;
    setGenres((g) => {
      let changed = false;
      const next = { ...g };
      for (const t of stepTracks) {
        if (next[t.path] === undefined && t.tags?.GENRE) {
          next[t.path] = t.tags.GENRE;
          changed = true;
        }
      }
      return changed ? next : g;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step, stepTracks]);

  const genreList = (path: string): string[] =>
    (genres[path] ?? "").split(";").map((g) => g.trim()).filter(Boolean);

  const setGenreList = (path: string, list: string[]) =>
    setGenres((g) => ({ ...g, [path]: list.join("; ") }));

  const addGenre = (path: string, value: string) => {
    const v = value.trim();
    if (!v) return;
    const list = genreList(path);
    if (!list.includes(v)) setGenreList(path, [...list, v]);
  };

  const removeGenre = (path: string, genre: string) =>
    setGenreList(path, genreList(path).filter((g) => g !== genre));

  const applyGenresToDisc = (disc: number, value: string) => {
    const paths = stepTracks.filter((t) => discOfTrack(t) === disc).map((t) => t.path);
    if (!paths.length) return;
    setGenres((g) => {
      const next = { ...g };
      for (const p of paths) next[p] = value;
      return next;
    });
    setDiscGenres((m) => ({ ...m, [disc]: "" }));
  };

  const saveGenres = async () => {
    setBusy(true);
    try {
      const writes: Record<string, Record<string, string | null>> = {};
      for (const [p, g] of Object.entries(genres)) writes[p] = { GENRE: g || null };
      await api.mbAssign(writes);
      toast("Genres saved");
      setStep(4);
    } catch (e) {
      toast(String(e));
    } finally {
      setBusy(false);
    }
  };

  // ---------------- Step 4: lyrics ----------------
  // Display name for a track ANYWHERE in the wizard: MusicBrainz release
  // title > existing tag > filename-derived fallback.
  const displayTitle = (p: string) => {
    const rt = suggByPath.get(p)?.release_track;
    if (rt?.title) return rt.title;
    const t = trackList.find((x) => x.path === p) ?? stepTracks.find((x) => x.path === p);
    return t?.tags?.TITLE ?? defaultTrackName(p);
  };

  const trackArtist = (p: string) => {
    const rt = suggByPath.get(p)?.release_track;
    if (rt?.artist_credit) return rt.artist_credit;
    const t = trackList.find((x) => x.path === p) ?? stepTracks.find((x) => x.path === p);
    return t?.tags?.ARTIST ?? "";
  };
  const trackTitle = (p: string) => {
    const rt = suggByPath.get(p)?.release_track;
    if (rt?.title) return rt.title;
    const t = trackList.find((x) => x.path === p) ?? stepTracks.find((x) => x.path === p);
    return t?.tags?.TITLE ?? defaultTrackName(p);
  };
  const trackDuration = (p: string): number | undefined => {
    const t = trackList.find((x) => x.path === p) ?? stepTracks.find((x) => x.path === p);
    return t?.tech?.length ? Math.round(t.tech.length) : undefined;
  };
  const trackAlbum = release?.title || stepTracks[0]?.tags.ALBUM || undefined;

  const importLyricsForAll = async () => {
    setBusy(true);
    let done = 0;
    let skipped = 0;
    let missing = 0;
    try {
      for (const t of stepTracks) {
        if (instrumental[t.path] === "1") continue;
        const artist = trackArtist(t.path);
        const title = trackTitle(t.path);
        if (!artist || !title) {
          skipped++;
          continue;
        }
        try {
          const res = await api.lyricsGet(artist, title, trackAlbum, trackDuration(t.path));
          const lrc = res?.syncedLyrics ?? res?.plainLyrics;
          if (lrc) {
            setLyricsDrafts((d) => ({ ...d, [t.path]: lrc }));
            done++;
          } else {
            missing++;
          }
        } catch {
          missing++;
        }
        await new Promise((r) => setTimeout(r, 350)); // gentle pacing for LRCLIB
      }
      if (done) {
        toast(`Imported lyrics for ${done} track(s)${missing ? ` — ${missing} not found on LRCLIB` : ""}`);
      } else if (skipped) {
        toast("No artist/title available for some tracks — matching the MusicBrainz release first improves lyrics results");
      } else {
        toast("No lyrics found on LRCLIB for these tracks (some songs genuinely have none)");
      }
    } finally {
      setBusy(false);
    }
  };

  const saveLyricsStep = async () => {
    setBusy(true);
    try {
      const writes: Record<string, Record<string, string | null>> = {};
      for (const t of stepTracks) {
        const inst = instrumental[t.path] ?? (t.tags.INSTRUMENTAL === "1" ? "1" : "0");
        writes[t.path] = { INSTRUMENTAL: inst };
        if (inst === "1") continue;
        const lrc = lyricsDrafts[t.path];
        if (lrc && parseLrc(lrc).length) {
          await api.lyricsWrite(t.path, lrc);
        }
      }
      await api.mbAssign(writes);
      toast("Lyrics + INSTRUMENTAL saved");
      setStep(5);
    } catch (e) {
      toast(String(e));
    } finally {
      setBusy(false);
    }
  };

  // ---------------- Step 5: advisory ----------------
  const applyAdvisoryToAll = (v: string) => {
    setAdvisory((a) => {
      const next = { ...a };
      for (const t of stepTracks) next[t.path] = v;
      return next;
    });
  };

  const saveAdvisory = async () => {
    setBusy(true);
    try {
      // Only write tracks the user explicitly changed — untouched tracks keep
      // their existing ITUNESADVISORY tags instead of being wiped.
      const writes: Record<string, Record<string, string | null>> = {};
      for (const t of stepTracks) {
        const picked = advisory[t.path];
        if (picked === undefined) continue;
        const existing = t.tags?.ITUNESADVISORY ?? undefined;
        if (picked !== existing) writes[t.path] = { ITUNESADVISORY: picked };
      }
      if (!Object.keys(writes).length) {
        toast("No advisory changes — pick values or use Apply to all");
        setStep(6);
        return;
      }
      await api.mbAssign(writes);
      toast("Advisory ratings saved");
      setStep(6);
    } catch (e) {
      toast(String(e));
    } finally {
      setBusy(false);
    }
  };

  const POST_IMPORT_SCRIPTS = [
  { id: 1, label: "Lyrics", defaultOn: true },
  { id: 2, label: "CUEs", defaultOn: true },
  { id: 3, label: "FLACs (re-encode)", defaultOn: false },
  { id: 5, label: "Images", defaultOn: true },
  { id: 7, label: "DR / ReplayGain", defaultOn: true },
  { id: 6, label: "Audit", defaultOn: false },
  { id: 8, label: "AutoTag", defaultOn: false },
  { id: 4, label: "Grade", defaultOn: true },
];
const [runAfterImport, setRunAfterImport] = useState<number[]>(
  POST_IMPORT_SCRIPTS.filter((s) => s.defaultOn).map((s) => s.id)
);

const finish = async () => {
  try {
    if (runAfterImport.length && uploaded.length) {
      await api.run(runAfterImport, uploaded.map((a) => a.path));
    }
  } catch (e) {
    toast(String(e));
  }
  qc.invalidateQueries({ queryKey: ["library"] });
  setParams({});
  toast(uploaded.length > 1 ? `Imported ${uploaded.length} albums — enrich each from its album page` : "Import complete — album graded");
};

  const switchAlbum = (i: number) => {
    setAlbumIndex(i);
    setAlbumPath(uploaded[i].path);
    autoDetected.current = false;
    setDetectedFromTags(false);
    setRelease(null);
    setReleaseId("");
    setSuggestions([]);
    setGenres({});
    setDiscGenres({});
    setGenreSource(null);
    setMbLink("");
    setRymLink("");
    setSearchHits([]);
    // reset per-track drafts so album B never inherits album A's data
    setLyricsDrafts({});
    setInstrumental({});
    setAdvisory({});
  };

  const totalFiles = albums.reduce((n, g) => n + g.files.length, 0);
  const hasRipFiles = albums.some((g) => g.files.some((f) => /\.(cue|log|accurip)$/i.test(f.relPath)));
  const canNext =
    step === 0
      ? totalFiles > 0 && albums.length > 0 && albums.every((g) => g.name.trim() || g.files.length === 0)
      : step === 1
        ? !!(releaseId || extractMbid(mbLink))
        : true;

  return (
    <div className="p-6 max-w-6xl mx-auto space-y-5">
      <div className="flex items-center gap-3 flex-wrap">
        <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
          <UploadCloud className="h-6 w-6 text-accent" /> Import
        </h1>
        {uploaded.length > 1 && (
          <select className="input !w-auto text-sm" value={albumIndex} onChange={(e) => switchAlbum(Number(e.target.value))}>
            {uploaded.map((a, i) => (
              <option key={a.path} value={i}>{a.name}</option>
            ))}
          </select>
        )}
        {albumPath && (
          <span className="text-xs text-zinc-500 truncate">
            <Link to={`/album/${encodeURIComponent(albumPath)}`} className="hover:text-accent-soft">
              {albumPath.split("/").pop()}
            </Link>
          </span>
        )}
      </div>

      {/* step indicator */}
      <div className="flex items-center gap-1.5 overflow-x-auto">
        {STEPS.map((s, i) => (
          <div key={s} className="flex items-center gap-1.5 shrink-0">
            <button
              onClick={() => i < step && setStep(i)}
              className={`flex items-center gap-1.5 rounded-lg px-3 py-1 text-xs transition-colors ${
                i === step
                  ? "bg-accent on-accent"
                  : i < step
                    ? "bg-accent/10 text-accent-soft hover:bg-accent/20"
                    : "bg-raise text-zinc-500 border border-border"
              }`}
            >
              {i < step ? <Check className="h-3 w-3" /> : <span>{i + 1}</span>}
              {s}
            </button>
            {i < STEPS.length - 1 && <div className="h-px w-3 bg-border" />}
          </div>
        ))}
      </div>

      {/* ---------------- Step 0: select & separate ---------------- */}
      {step === 0 && (
        <div className="space-y-4">
          <div
            className="rounded-xl border-2 border-dashed border-border bg-card p-10 text-center hover:border-accent/60 transition-colors cursor-pointer"
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault();
              handleDrop(e.dataTransfer.items);
            }}
            onClick={() => pickFolderBrowser()}
          >
            <UploadCloud className="h-10 w-10 text-zinc-600 mx-auto mb-3" />
            <div className="font-medium text-zinc-300">Drop albums or files here</div>
            <div className="text-xs text-zinc-600 mt-1">
              drop one or more folders (even from different artists) — they are separated into albums below
            </div>
            <div className="text-xs text-zinc-600">
              multi-disc folders (<b className="text-zinc-500">CD1/</b>, <b className="text-zinc-500">Disc 2/</b>) merge into one album
            </div>
            <div className="text-[11px] text-zinc-600 mt-1">
              audio (flac, mp3, m4a, ogg, opus, wav, …) · images (jpg, png, webp, tiff, avif, heic, …) · .lrc .cue .log .accurip
            </div>
            <div className="text-xs text-zinc-600 mt-2">
              click to <b className="text-zinc-400">pick a folder</b> · or{" "}
              <span
                className="text-accent-soft underline underline-offset-2 cursor-pointer"
                onClick={(e) => {
                  e.stopPropagation();
                  document.getElementById("import-files")?.click();
                }}
              >
                browse individual files
              </span>
            </div>
            <input id="import-files" type="file" multiple className="hidden" onChange={(e) => e.target.files && handleFiles(e.target.files)} />
            <input
              id="import-folder"
              type="file"
              multiple
              className="hidden"
              ref={(el) => {
                if (el) {
                  el.setAttribute("webkitdirectory", "");
                  el.setAttribute("directory", "");
                }
              }}
              onChange={(e) => e.target.files && handleFiles(e.target.files)}
            />
          </div>

          <div className="flex items-center gap-2 flex-wrap">
            <button className="btn-ghost" onClick={pickFolderNative}>
              <FolderOpen className="h-4 w-4" /> Choose folder
            </button>
            <input
              className="input max-w-xs"
              placeholder="Default album name"
              value={albumName}
              onChange={(e) => setAlbumName(e.target.value)}
            />
            <label className="flex items-center gap-1.5 text-xs text-zinc-400">
              Media type:
              <select className="input !w-auto !py-1 text-xs" value={mediaType} onChange={(e) => setMediaType(e.target.value)}>
                <option value="">Not sure</option>
                <option value="CD">CD rip</option>
                <option value="Digital Media">Digital Media</option>
              </select>
            </label>
            {source === "native" && (
              <span className="text-xs text-zinc-500">importing from disk — files move into your library</span>
            )}
          </div>

          {(mediaType === "" || mediaType === "CD") && totalFiles > 0 && (
            <div className="rounded-lg border border-amber-900/60 bg-amber-950/30 px-3 py-2 text-xs text-amber-200">
              {mediaType === "CD"
                ? "CD rip: include the .cue, .log and .accurip files alongside the audio so grading and audit can verify the rip."
                : "CD rip? If these files came from a CD, set Media type to “CD rip” and include the .cue and .log files (drop them here or browse them alongside the audio)."}
              {!hasRipFiles && " No .cue/.log files detected in the current selection."}
            </div>
          )}

          {totalFiles > 0 && (
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <span className="text-sm font-semibold">Separate into albums</span>
                <span className="text-xs text-zinc-500">
                  {totalFiles} file(s) → {albums.length} album(s) — rename, or move files between albums with the dropdown
                </span>
                <button className="btn-ghost !py-1 text-xs ml-auto" onClick={addAlbum}>
                  <Plus className="h-3.5 w-3.5" /> Add album
                </button>
              </div>
              {albums.map((g, gi) => (
                <div key={gi} className="bg-card rounded-lg border border-border p-3">
                  <div className="flex items-center gap-2 mb-2">
                    <Disc3 className="h-4 w-4 text-zinc-500 shrink-0" />
                    <input
                      className="input !w-auto min-w-[200px] font-medium"
                      value={g.name}
                      placeholder="Album name"
                      onChange={(e) => renameGroup(gi, e.target.value)}
                    />
                    <span className="text-xs text-zinc-500">{g.files.length} file(s)</span>
                    <button className="btn-danger !px-2 !py-1 ml-auto" onClick={() => removeAlbum(gi)} disabled={albums.length <= 1} title="Remove (files move to first album)">
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                  <div className="max-h-52 overflow-auto space-y-1">
                    {g.files.length === 0 && <div className="text-xs text-zinc-600 px-2 py-1">Empty — move files here from other albums.</div>}
                    {g.files.map((f) => (
                      <ImportFileRow
                        key={f.relPath}
                        f={f}
                        gi={gi}
                        albums={albums}
                        groupFiles={g.files}
                        onMove={(to) => moveFile(gi, to, f)}
                      />
                    ))}
                    {(() => {
                      const covers = g.files.filter((x) => /\.(jpg|jpeg|png|webp|bmp|gif|tiff|tif|avif|heic|heif|jxl|svg)$/i.test(x.relPath));
                      if (!covers.length) return null;
                      return (
                        <div className="text-[11px] text-zinc-500 px-2 pt-1">
                          {covers.length} image{covers.length === 1 ? "" : "s"} — “cover.jpg” becomes the album cover; an image named like a track (“01 - Song.jpg” next to “01 - Song.flac”) becomes that track's own cover.
                        </div>
                      );
                    })()}
                  </div>
                </div>
              ))}
              <button
                className="btn-primary"
                onClick={doImport}
                disabled={uploading || !albums.some((g) => g.name.trim() && g.files.length)}
              >
                {uploading ? "Importing…" : `Import ${albums.filter((g) => g.files.length).length} album(s) into library`}
              </button>
            </div>
          )}
        </div>
      )}

      {/* ---------------- Step 1: links ---------------- */}
      {step === 1 && (
        <div className="space-y-4">
          <div className="bg-card rounded-lg border border-border p-4 space-y-3">
            <div className="text-sm font-semibold text-zinc-300">
              MusicBrainz release <span className="text-zinc-500 font-normal">— {currentAlbumName}</span>
            </div>
            <div className="flex items-center gap-2">
              <input
                className={`input flex-1 ${releaseId ? "!border-emerald-700" : ""}`}
                placeholder="MusicBrainz release URL or ID (e.g. https://musicbrainz.org/release/…)"
                value={mbLink}
                onChange={(e) => setMbLink(e.target.value)}
              />
              {releaseId ? (
                <span className="chip bg-emerald-900/60 text-emerald-300 border border-emerald-800 shrink-0">
                  <Check className="h-3 w-3" /> {detectedFromTags ? "detected from track tags" : "recognized"}
                </span>
              ) : mbLink.trim() ? (
                <span className="chip bg-amber-900/50 text-amber-300 border border-amber-900 shrink-0">no MBID found</span>
              ) : null}
            </div>
            {detectStatus !== "idle" && !releaseId && (
              <div className="text-xs text-zinc-500 flex items-center gap-1.5">
                {detectStatus === "scanning" && (
                  <span className="animate-pulse">Scanning track tags for a MusicBrainz release ID…</span>
                )}
                {detectStatus === "none" && (
                  <span>
                    No MusicBrainz release ID found in the track tags — paste a link, search, or{" "}
                    <button className="text-accent-soft underline underline-offset-2" onClick={detectFromTags}>rescan</button>
                  </span>
                )}
              </div>
            )}
            {detectStatus === "found" && releaseId && (
              <div className="text-xs text-emerald-400 flex items-center gap-1.5">
                <Check className="h-3 w-3" /> Release detected in track tags — fetched automatically
              </div>
            )}
            <div className="text-xs text-zinc-600">or search:</div>
            <div className="flex gap-2">
              <div className="flex gap-2">
                <select
                  className="input !w-auto text-xs shrink-0"
                  value={searchMode}
                  onChange={(e) => setSearchMode(e.target.value as any)}
                  title="Search MusicBrainz by"
                >
                  <option value="release">Title / artist</option>
                  <option value="track">Track title</option>
                  <option value="catno">Catalog number</option>
                  <option value="barcode">Barcode</option>
                </select>
                <input className="input" placeholder="Search MusicBrainz…" value={mbSearch} onChange={(e) => setMbSearch(e.target.value)} onKeyDown={(e) => e.key === "Enter" && doSearch()} />
                <button className="btn-ghost shrink-0" onClick={() => doSearch()} disabled={busy}>Search</button>
              </div>
            </div>
            <div className="text-xs text-zinc-600">or find an artist:</div>
            <div className="flex gap-2">
              <input
                className="input"
                placeholder="Artist name…"
                value={artistQuery}
                onChange={(e) => setArtistQuery(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && doArtistSearch()}
              />
              <button className="btn-ghost shrink-0" onClick={doArtistSearch} disabled={busy}>Find artist</button>
            </div>
            {artistHits.length > 0 && (
              <div className="max-h-40 overflow-auto space-y-1">
                {artistHits.map((a) => (
                  <button key={a.id} className="w-full text-left px-3 py-2 rounded bg-panel hover:bg-raise text-sm flex items-center gap-2"
                    onClick={() => applyArtist(a.name)} title={`Search releases by ${a.name}`}>
                    <span className="flex-1 truncate min-w-0 text-zinc-200">{a.name}</span>
                    {a.type && <span className="chip bg-zinc-800 text-zinc-500 border border-border text-[10px]">{a.type}</span>}
                  </button>
                ))}
              </div>
            )}
            {searchHits.length > 0 && (
              <div className="max-h-48 overflow-auto space-y-1">
                {searchHits.map((h) => (
                  <button key={h.id} className="w-full text-left px-3 py-2 rounded bg-panel hover:bg-raise text-sm flex items-center gap-2"
                    onClick={() => { setMbLink(`https://musicbrainz.org/release/${h.id}`); setReleaseId(h.id); }}>
                    <span className="flex-1 truncate min-w-0">
                      <span className="text-zinc-200">{h.title}</span>
                      <span className="text-zinc-500"> — {h.artist} ({h.date})</span>
                    </span>
                    {h.catalog_number && (
                      <span className="chip bg-raise border border-border text-zinc-400 shrink-0">catno {h.catalog_number}</span>
                    )}
                    {h.barcode && (
                      <span className="chip bg-raise border border-border text-zinc-500 font-mono shrink-0">{h.barcode}</span>
                    )}
                    {releaseId === h.id && <Check className="h-4 w-4 text-accent shrink-0" />}
                  </button>
                ))}
              </div>
            )}
            {release && (
              <div className="text-xs text-zinc-400 pt-2 border-t border-border">
                <span className="font-semibold text-zinc-200">{release.title}</span> · {release.artists.map((a) => a.name).join(", ")} · {release.date} · {release.medium_count} disc(s) · {release.media.length} tracks
              </div>
            )}
            <div className="text-sm font-semibold text-zinc-300 pt-2">RateYourMusic album link (optional)</div>
            <div className="flex items-center gap-2">
              <input
                className={`input flex-1 ${rymValid === true ? "!border-emerald-700" : rymValid === false ? "!border-red-800" : ""}`}
                placeholder="https://rateyourmusic.com/release/…"
                value={rymLink}
                onChange={(e) => setRymLink(e.target.value)}
              />
              {rymValid === true && (
                <span className="chip bg-emerald-900/60 text-emerald-300 border border-emerald-800 shrink-0">
                  <Check className="h-3 w-3" /> valid
                </span>
              )}
              {rymValid === false && (
                <span className="chip bg-red-900/50 text-red-300 border border-red-900 shrink-0">not a RYM URL</span>
              )}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button className="btn-primary" onClick={handleFetch} disabled={busy}>
              <Wand2 className="h-4 w-4" /> Fetch release & auto-match
            </button>
            <button className="btn-ghost" onClick={detectFromTags} disabled={busy || !albumPath}>
              Detect from tags
            </button>
            {busy && fetchStatus && (
              <span className="text-xs text-accent-soft animate-pulse flex items-center gap-1.5">
                {fetchStatus}
              </span>
            )}
          </div>
        </div>
      )}

      {/* ---------------- Step 2: matching ---------------- */}
      {step === 2 && (
        <div className="space-y-3">
          <div className="text-sm text-zinc-400">
            Confirm each local track's MusicBrainz track/disc. Unmatched rows stay blank — you can also fix them manually later on the track page.
          </div>
          {suggestions.length === 0 && <div className="text-xs text-zinc-500">No data — go back and fetch the release.</div>}
          {groupByDisc(suggestions, discOfSuggestion).map((g) => (
            <DiscSection
              key={g.disc ?? "unmatched"}
              disc={g.disc}
              count={g.rows.length}
              collapsed={collapsedDiscs.has(g.disc ?? null)}
              onToggle={() => toggleDisc(g.disc ?? null)}
            >
              {g.rows.map((s) => {
                const local = trackList.find((t) => t.path === s.local);
                return (
                  <div key={s.local} className="flex items-center gap-3 bg-card rounded-lg border border-border px-3 py-2">
                    <span className="text-xs text-zinc-600 w-8">{local?.tags.TRACKNUMBER ?? s.file.split("/").pop()?.slice(0, 2)}</span>
                    <span className="flex-1 truncate text-sm">{displayTitle(s.local)}</span>
                    <span className="text-xs text-zinc-500">
                      {s.matched ? `${s.release_track!.disc}.${s.release_track!.position} ${s.release_track!.title}` : "no match"}
                    </span>
                    <select
                      className="input !w-auto text-xs"
                      value={s.release_track ? `${s.release_track.disc}-${s.release_track.position}` : ""}
                      onChange={(e) => {
                        const [d, p] = e.target.value.split("-").map(Number);
                        setSuggestion(s.local, d, p);
                      }}
                    >
                      <option value="">— none —</option>
                      {release?.media.map((m) => (
                        <option key={`${m.disc}-${m.position}`} value={`${m.disc}-${m.position}`}>
                          {m.disc}.{m.position} {m.title}
                        </option>
                      ))}
                    </select>
                  </div>
                );
              })}
            </DiscSection>
          ))}
          <div className="flex justify-end">
            <button className="btn-primary" onClick={confirmMatch} disabled={busy}>
              Save matching
            </button>
          </div>
        </div>
      )}

      {/* ---------------- Step 3: genres ---------------- */}
      {step === 3 && (
        <div className="space-y-3">
          <div className="flex items-center gap-2 flex-wrap">
            <button className="btn-ghost" onClick={importGenres} disabled={busy || !(releaseId || extractMbid(mbLink))}>
              <CloudDownloadIcon /> Import genres from MusicBrainz
            </button>
            {busy && fetchStatus && (
              <span className="text-xs text-accent-soft animate-pulse">{fetchStatus}</span>
            )}
            <label className="flex items-center gap-1.5 text-xs text-zinc-400 ml-auto">
              Max genres / track
              <select
                className="input !w-auto !py-1 text-xs"
                value={genreLimit ?? 0}
                onChange={(e) => setGenreLimit(e.target.value === "0" ? null : Number(e.target.value))}
              >
                <option value={0}>All</option>
                <option value={1}>1 (primary)</option>
                <option value={2}>2</option>
                <option value={3}>3</option>
                <option value={4}>4</option>
                <option value={5}>5</option>
                <option value={10}>10</option>
              </select>
            </label>
          </div>
          <span className="text-xs text-zinc-500 -mt-1 block">
            {genreSource
              ? `Fetched via ${genreSource} fallback (track → release → release-group → artist). Edit freely.`
              : "Genres are not fetched automatically — set the per-track limit, then click to import."}
          </span>
          {stepTracks.length === 0 && <div className="text-xs text-zinc-500">No tracks — go back and fetch the release.</div>}
          {groupByDisc(stepTracks, discOfTrack).map((g) => (
            <DiscSection
              key={g.disc ?? "unmatched"}
              disc={g.disc}
              count={g.rows.length}
              collapsed={collapsedDiscs.has(g.disc ?? null)}
              onToggle={() => toggleDisc(g.disc ?? null)}
              extra={
                g.disc
                  ? [
                      <input
                        key="in"
                        className="input !w-52 !py-1 text-xs"
                        placeholder="Apply genre to whole disc…"
                        value={discGenres[g.disc!] ?? ""}
                        onChange={(e) => setDiscGenres((m) => ({ ...m, [g.disc!]: e.target.value }))}
                        onKeyDown={(e) => e.key === "Enter" && applyGenresToDisc(g.disc!, (discGenres[g.disc!] ?? "").trim())}
                      />,
                      <button
                        key="btn"
                        className="btn-ghost !py-1 text-xs"
                        onClick={() => applyGenresToDisc(g.disc!, (discGenres[g.disc!] ?? "").trim())}
                      >
                        Apply to all
                      </button>,
                    ]
                  : undefined
              }
            >
              {g.rows.map((t) => (
                <div key={t.path} className="flex items-center gap-3 bg-card rounded-lg border border-border px-3 py-2">
                  <span className="text-xs text-zinc-600 w-8">{t.tags.TRACKNUMBER ?? "—"}</span>
                  <span className="flex-1 truncate text-sm">{displayTitle(t.path)}</span>
                  <div className="flex items-center gap-1.5 flex-wrap justify-end">
                    {genreList(t.path).map((gen) => (
                      <span key={gen} className="chip bg-accent/10 text-accent-soft border border-accent/25">
                        {gen}
                        <button
                          className="hover:text-white transition-colors"
                          onClick={() => removeGenre(t.path, gen)}
                          title={`Remove ${gen}`}
                        >
                          <X className="h-3 w-3" />
                        </button>
                      </span>
                    ))}
                    <input
                      className="input !w-36 !py-1 text-xs"
                      placeholder={genreList(t.path).length ? "+ add genre…" : "Add genre…"}
                      value={genreAddValues[t.path] ?? ""}
                      onChange={(e) => setGenreAddValues((v) => ({ ...v, [t.path]: e.target.value }))}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") {
                          addGenre(t.path, genreAddValues[t.path] ?? "");
                          setGenreAddValues((v) => ({ ...v, [t.path]: "" }));
                        }
                      }}
                    />
                  </div>
                </div>
              ))}
            </DiscSection>
          ))}
          <div className="flex justify-end">
            <button className="btn-primary" onClick={saveGenres} disabled={busy}>Save genres</button>
          </div>
        </div>
      )}

      {/* ---------------- Step 4: lyrics ---------------- */}
      {step === 4 && (
        <div className="space-y-4">
          <div className="flex items-center gap-2">
            <button className="btn-primary text-xs" onClick={importLyricsForAll} disabled={busy}>
              <CloudDownloadIcon /> Auto-import from LRCLIB
            </button>
            <span className="text-xs text-zinc-500">Review below — Space stamps time while previewing; INSTRUMENTAL=1 skips lyrics.</span>
          </div>
          {stepTracks.map((t) => {
            const inst = instrumental[t.path] ?? t.tags.INSTRUMENTAL;
            const hasDraft = !!(lyricsDrafts[t.path] && parseLrc(lyricsDrafts[t.path]).length) || !!t.lyrics_present;
            return (
              <details key={t.path} className="bg-card rounded-lg border border-border open:pb-3">
                <summary className="px-3 py-2 text-sm font-medium cursor-pointer flex items-center gap-2">
                  <span className="text-xs text-zinc-600 w-8">{t.tags.TRACKNUMBER ?? "—"}</span>
                  <span className="flex-1 truncate">{displayTitle(t.path)}</span>
                  {hasDraft && (
                    <span className="chip bg-emerald-900/60 text-emerald-300 border border-emerald-800">
                      <Check className="h-3 w-3" /> lyrics
                    </span>
                  )}
                  <label className="flex items-center gap-1.5 text-xs text-zinc-400 select-none" onClick={(e) => e.stopPropagation()}>
                    <input
                      type="checkbox"
                      checked={inst === "1"}
                      onChange={(e) => setInstrumental((m) => ({ ...m, [t.path]: e.target.checked ? "1" : "0" }))}
                      className=""
                    />
                    INSTRUMENTAL
                  </label>
                </summary>
                {inst !== "1" ? (
                  <div className="px-3">
                    <LyricsViewer
                      path={t.path}
                      initialLyrics={lyricsDrafts[t.path] ?? ""}
                      onChange={(lrc) => setLyricsDrafts((d) => ({ ...d, [t.path]: lrc }))}
                      artist={trackArtist(t.path)}
                      track={trackTitle(t.path)}
                      album={trackAlbum}
                      duration={trackDuration(t.path)}
                    />
                  </div>
                ) : (
                  <div className="px-3 text-xs text-zinc-500">Marked instrumental — lyrics skipped.</div>
                )}
              </details>
            );
          })}
          <div className="flex justify-end">
            <button className="btn-primary" onClick={saveLyricsStep} disabled={busy}>Save lyrics & instrumental</button>
          </div>
        </div>
      )}

      {/* ---------------- Step 5: advisory ---------------- */}
      {step === 5 && (
        <div className="space-y-3">
          <div className="text-sm text-zinc-400">Set iTunes advisory per track: <b className="text-zinc-200">0</b> unrated/clean, <b className="text-zinc-200">1</b> explicit, <b className="text-zinc-200">2</b> safe edited version.</div>
          <div className="flex items-center gap-2 bg-card rounded-lg border border-border px-3 py-2 flex-wrap">
            <span className="text-xs font-semibold text-zinc-400">Apply to all tracks:</span>
            {["0", "1", "2"].map((v) => (
              <button
                key={v}
                onClick={() => applyAdvisoryToAll(v)}
                className="btn-ghost !py-1 text-xs"
                title={`Set every track to ${v === "0" ? "clean" : v === "1" ? "explicit" : "safe"}`}
              >
                {v === "0" ? "0 · clean" : v === "1" ? "1 · explicit" : "2 · safe"}
              </button>
            ))}
          </div>
          {stepTracks.map((t) => (
            <div key={t.path} className="flex items-center gap-3 bg-card rounded-lg border border-border px-3 py-2">
              <span className="flex-1 truncate text-sm">{displayTitle(t.path)}</span>
              <div className="flex gap-1">
                {["0", "1", "2"].map((v) => (
                  <button
                    key={v}
                    onClick={() => setAdvisory((a) => ({ ...a, [t.path]: v }))}
                    className={`px-3 py-1 rounded text-xs border ${
                      (advisory[t.path] ?? t.tags.ITUNESADVISORY) === v
                        ? "bg-accent on-accent border-accent"
                        : "bg-panel text-zinc-400 border-border hover:border-accent/50"
                    }`}
                  >
                    {v === "0" ? "0 · clean" : v === "1" ? "1 · explicit" : "2 · safe"}
                  </button>
                ))}
              </div>
            </div>
          ))}
          <div className="flex justify-end">
            <button className="btn-primary" onClick={saveAdvisory} disabled={busy}>Save advisory</button>
          </div>
        </div>
      )}

      {/* ---------------- Step 6: finish ---------------- */}
      {step === 6 && (
        <div className="bg-card rounded-lg border border-border p-6">
          <div className="text-center">
            <Check className="h-10 w-10 text-emerald-400 mx-auto mb-3" />
            <div className="font-semibold text-lg">Import complete</div>
            <div className="text-sm text-zinc-500 mt-1">
              {uploaded.length > 1
                ? `${uploaded.length} albums were added to your library. Use the dropdown above to finish linking, matching and tagging each one.`
                : "Links, MBIDs, metadata, genres, lyrics and advisory ratings are written to the files."}
            </div>
          </div>
          <div className="mt-5 bg-panel rounded-lg border border-border p-4">
            <div className="text-xs font-semibold uppercase tracking-wider text-zinc-400 mb-2">
              Run scripts after import (on the new album{uploaded.length > 1 ? "s" : ""})
            </div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
              {POST_IMPORT_SCRIPTS.map((s) => (
                <label key={s.id} className="flex items-center gap-2 text-xs text-zinc-300 cursor-pointer select-none">
                  <input
                    type="checkbox"
                    checked={runAfterImport.includes(s.id)}
                    onChange={(e) =>
                      setRunAfterImport((ids) => (e.target.checked ? [...ids, s.id] : ids.filter((i) => i !== s.id)))
                    }
                  />
                  {s.label}
                </label>
              ))}
            </div>
            <div className="text-[10px] text-zinc-600 mt-2">
              Progress shows at the top of the window. Scripts can also be run individually anytime from the album page.
            </div>
          </div>
          <div className="flex justify-center gap-2 mt-5">
            {albumPath && (
              <Link to={`/album/${encodeURIComponent(albumPath)}`} className="btn-ghost" onClick={finish}>
                Open album
              </Link>
            )}
            <button className="btn-primary" onClick={finish}>Done</button>
          </div>
        </div>
      )}

      {/* nav buttons */}
      {step > 0 && step < 6 && (
        <div className="flex justify-between pt-2">
          <button className="btn-ghost" onClick={() => setStep(step - 1)}>
            <ChevronLeft className="h-4 w-4" /> Back
          </button>
          <button
            className="btn-primary"
            disabled={!canNext || busy}
            onClick={() => (step === 1 ? nextFromLinks() : step === 2 ? confirmMatch() : step === 3 ? saveGenres() : step === 4 ? saveLyricsStep() : saveAdvisory())}
          >
            {step === 5 ? "Save & finish" : "Continue"} <ChevronRight className="h-4 w-4" />
          </button>
        </div>
      )}
    </div>
  );
}

function CloudDownloadIcon() {
  return <ExternalLink className="h-3.5 w-3.5" />;
}

/** Collapsible per-disc section header + body, shared by Match and Genres. */
function DiscSection({
  disc,
  count,
  collapsed,
  onToggle,
  extra,
  children,
}: {
  disc: number | null;
  count: number;
  collapsed: boolean;
  onToggle: () => void;
  extra?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <div
        className="flex items-center gap-1.5 px-1 pt-2 text-xs font-bold uppercase tracking-wider text-zinc-400 cursor-pointer select-none"
        onClick={onToggle}
      >
        <button className="p-0.5 text-zinc-500 hover:text-white">
          {collapsed ? <ChevronRight className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
        </button>
        <Disc3 className="h-3.5 w-3.5 text-zinc-500" />
        {disc ? `Disc ${disc}` : "Unmatched"}
        <span className="font-normal text-zinc-600 normal-case">
          {count} track{count === 1 ? "" : "s"}
        </span>
        {extra && (
          <span className="ml-auto flex items-center gap-1.5 normal-case font-normal" onClick={(e) => e.stopPropagation()}>
            {extra}
          </span>
        )}
      </div>
      {!collapsed && children}
    </div>
  );
}

/** One row in the import file list: image thumbnails for artwork, and a
 * "track cover" hint when an image shares its stem with an audio file in
 * the same album group (that is how per-track covers are added). */
function ImportFileRow({ f, gi, albums, groupFiles, onMove }: {
  f: ImportFile;
  gi: number;
  albums: AlbumGroup[];
  groupFiles: ImportFile[];
  onMove: (to: number) => void;
}) {
  const isImage = /\.(jpg|jpeg|png|webp|bmp|gif|tiff|tif|avif|heic|heif|jxl|svg)$/i.test(f.relPath);
  const [thumb, setThumb] = useState<string | null>(null);
  useEffect(() => {
    if (!isImage || !f.file) return;
    const url = URL.createObjectURL(f.file);
    setThumb(url);
    return () => URL.revokeObjectURL(url);
  }, [isImage, f.file]);
  const coverFor = (() => {
    if (!isImage) return null;
    const stem = f.relPath.split("/").pop()!.replace(/\.[^.]+$/, "").toLowerCase();
    if (["cover", "front", "folder"].includes(stem)) return "album cover";
    const hit = groupFiles.find((x) => x !== f && AUDIO_RE.test(x.relPath) && x.relPath.split("/").pop()!.replace(/\.[^.]+$/, "").toLowerCase() === stem);
    return hit ? `track cover for ${hit.relPath.split("/").pop()}` : null;
  })();
  return (
    <div className="flex items-center gap-2 text-xs px-2 py-1">
      {isImage && thumb ? (
        <img src={thumb} alt="" className="h-8 w-8 rounded bg-raise border border-border object-cover shrink-0" />
      ) : isImage ? (
        <span className="h-8 w-8 rounded bg-raise border border-border shrink-0 flex items-center justify-center text-zinc-600 text-[9px]">IMG</span>
      ) : null}
      <span className="flex-1 min-w-0" title={f.relPath}>
        <span className="block break-words text-zinc-400">{f.relPath}</span>
        {coverFor && <span className="block text-[10px] text-accent-soft">→ {coverFor}</span>}
      </span>
      <select
        className="input !w-auto !py-0.5 text-[11px] shrink-0"
        value={gi}
        onChange={(e) => onMove(Number(e.target.value))}
      >
        {albums.map((a, i) => (
          <option key={i} value={i}>{a.name.trim() || `Album ${i + 1}`}</option>
        ))}
      </select>
    </div>
  );
}
