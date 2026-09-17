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
import CoverSearchModal from "../components/CoverSearchModal";
import CoverImg, { TrackCover } from "../components/CoverImg";
import PageHeader from "../components/PageHeader";
import MetadataReviewModal from "../components/MetadataReviewModal";
import type {
  AcoustidAlbumMatch, AcoustidMatch, ImportBulkJob, ImportScriptsPreview,
  LyricsAutoResult, MBRelease, MatchSuggestion, Track,
} from "../types";
import { SCRIPTS } from "../lib/scripts";

const STEPS = ["Select & separate", "Links", "Match", "Covers", "Genres", "Lyrics", "Advisory", "Finish"];

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
  // relPaths the user unticked: PARTIAL import. Excluded files are never
  // uploaded or moved, and the release tracklist recorded at match time lets
  // the album page grey out exactly those tracks.
  const [excluded, setExcluded] = useState<Set<string>>(new Set());

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
  // Covers step: album/per-track cover feedback + the per-track selection.
  const [coverNotice, setCoverNotice] = useState<string | null>(null);
  const [lyricsNotice, setLyricsNotice] = useState<string | null>(null);
  const [coverSel, setCoverSel] = useState<Set<string>>(new Set());
  const [coverUrl, setCoverUrl] = useState("");
  const [trackCoverUrl, setTrackCoverUrl] = useState("");
  const [coverSearchOpen, setCoverSearchOpen] = useState(false);
  const albumCoverInput = useRef<HTMLInputElement>(null);
  const trackCoverInput = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [fetchStatus, setFetchStatus] = useState<string | null>(null);
  const qc = useQueryClient();

  // ---- Bulk queue (several albums at once) ------------------------------
  // More than one album selected/dropped switches the wizard into queue mode:
  // the 8 steps below keep working on the album picked in the dropdown, the
  // queue itself is imported and post-processed through api.importBulk.
  const [bulkJob, setBulkJob] = useState<ImportBulkJob | null>(null);
  const queueMode = uploaded.length > 1 || albums.length > 1;
  const [acoustid, setAcoustid] = useState<AcoustidMatch | null>(null);
  const [acoustidBusy, setAcoustidBusy] = useState(false);
  const [matchAllBusy, setMatchAllBusy] = useState(false);
  // Per-track results of the last lyrics auto-import (provider per track).
  const [lyrResults, setLyrResults] = useState<Record<string, LyricsAutoResult>>({});

  // Exactly which scripts the import chain runs (Settings → Import).
  const { data: scriptChain } = useQuery({
    queryKey: ["importScripts"],
    queryFn: () => api.importScriptsPreview(),
  });

  // Poll the bulk queue while it runs; done/failed stops the poll.
  useEffect(() => {
    if (bulkJob?.status !== "running") return;
    const timer = setInterval(() => {
      api.importBulkStatus()
        .then((job) => {
          setBulkJob(job);
          if (job.status !== "running") qc.invalidateQueries({ queryKey: ["library"] });
        })
        .catch(() => setBulkJob(null)); // server restarted: nothing to poll
    }, 1500);
    return () => clearInterval(timer);
  }, [bulkJob?.status, qc]);

  const { data: lib } = useQuery({ queryKey: ["library"], queryFn: api.library });

  // Metadata review is off unless the config explicitly turns it on.
  const { data: cfg } = useQuery({ queryKey: ["config"], queryFn: api.config });
  /** Album folder whose metadata review modal is open (metadata_review only). */
  const [reviewPath, setReviewPath] = useState<string | null>(null);

  // Real dimensions of the album cover, re-read whenever a cover changes.
  const { data: coverInfo } = useQuery({
    queryKey: ["coverInfo", albumPath],
    queryFn: () => api.coverInfo(albumPath!),
    enabled: !!albumPath && step >= 3,
  });

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
      toast.error(String(e));
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
    // A fresh selection starts with nothing excluded: the tick state is keyed
    // by relPath, so a second drop of a same-named folder would otherwise
    // silently carry the previous album's exclusions into it.
    setExcluded(new Set());
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
      if ((e as Error).name !== "AbortError") toast.error(String(e));
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
      toast.error(String(e));
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
    // Excluded files are dropped here, before anything is uploaded or moved:
    // that is what makes a PARTIAL album import (one track of twelve) work.
    // The native/folder import MOVES the source directory, so there is no
    // subset to move — exclusion only exists on the upload path.
    const skip = source === "web" ? excluded : new Set<string>();
    const included = (g: AlbumGroup) => g.files.filter((f) => !skip.has(f.relPath));
    const groups = albums.filter((g) => g.name.trim() && included(g).length);
    if (!groups.length) {
      toast("Nothing to import — add files first");
      return;
    }
    setUploading(true);
    try {
      const results: { name: string; path: string }[] = [];
      const failed: { name: string; error: unknown }[] = [];
      for (const g of groups) {
        const name = g.name.trim();
        const keep = included(g);
        if (source === "web") {
          const filesToSend = keep.filter((f) => f.file) as { file: File; relPath: string }[];
          if (!filesToSend.length) continue;
          try {
            const res = await api.importUpload(name, filesToSend);
            results.push({ name, path: res.album_path });
          } catch (e) {
            failed.push({ name, error: e });
          }
        } else {
          const src = nativeRoot ? (g.root ? `${nativeRoot}/${g.root}` : nativeRoot) : "";
          if (!src) continue;
          try {
            const res = await api.importIngest(src, name);
            results.push({ name, path: res.path });
          } catch (e) {
            failed.push({ name, error: e });
          }
        }
      }
      if (!results.length) {
        toast(failed.length ? String(failed[0].error) : "Nothing to import");
        return;
      }
      setUploaded(results);
      setAlbumIndex(0);
      setAlbumPath(results[0].path);
      setStep(1);
      qc.invalidateQueries({ queryKey: ["library"] });
      // Committed. With metadata_review on (Settings → Metadata) the review is
      // offered right here, while the album is fresh, instead of on a later
      // visit to its page.
      if (cfg?.metadata_review === true) setReviewPath(results[0].path);
      // Several albums: hand the staged queue to the bulk job, which moves
      // whatever is still outside the library and runs the import chain per
      // album. The queue panel polls api.importBulkStatus for progress.
      if (results.length > 1) {
        try {
          const job = await api.importBulk(results.map((r) => ({ path: r.path })));
          if (job.ok && job.job) setBulkJob(job.job);
          else toast(`Queue import: ${job.error ?? "could not start"}`);
        } catch (e) {
          toast(`Queue import: ${e}`);
        }
      }
      toast(
        failed.length
          ? `Imported ${results.length} album${results.length > 1 ? "s" : ""} — failed: ${failed.map((f) => f.name).join(", ")}`
          : `Imported ${results.length} album${results.length > 1 ? "s" : ""}`
      );
    } catch (e) {
      toast.error(String(e));
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
      toast.error(String(e));
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
      toast.error(String(e));
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

  const pickRelease = async (id: string, target: string | null = albumPath) => {
    if (!id) return;
    setBusy(true);
    setFetchStatus("Fetching release from MusicBrainz…");
    try {
      // MusicBrainz rate-limits and blips — retry the remote calls.
      const rel = await withRetry(() => api.mbRelease(id));
      setRelease(rel);
      setReleaseId(id);
      setFetchStatus("Matching local tracks to the release…");
      const matched = await api.mbMatch(target!, id);
      setSuggestions(matched.suggestions);
      if (!matched.suggestions.length) {
        toast("No audio tracks found in this folder — check the album folder contains the music files");
      } else {
        toast(`Matched ${matched.suggestions.filter((s) => s.matched).length}/${matched.suggestions.length} tracks`);
      }
    } catch (e) {
      toast.error(String(e));
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
      toast.error(String(e));
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
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  // ---------------- AcoustID: fingerprint the staged audio ----------------
  /** Stage 2 of the AcoustID step: what the audio actually is. Says so and
   *  leaves the manual MusicBrainz search alone when it cannot run. */
  const runAcoustid = async () => {
    // Everything staged, else the single album the wizard is on (?album=).
    const paths = uploaded.length ? uploaded.map((a) => a.path) : albumPath ? [albumPath] : [];
    if (!paths.length) {
      toast("Import the files first — AcoustID fingerprints the staged album");
      return;
    }
    setAcoustidBusy(true);
    try {
      const res = await api.importAcoustid(paths);
      setAcoustid(res);
      if (!res.available) toast(`Fingerprinting unavailable — ${res.note}`);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setAcoustidBusy(false);
    }
  };

  /** Which edition of a matched release group to use: the release with the
   *  track count AcoustID heard, else the group's earliest. */
  const releaseForRow = async (row: AcoustidAlbumMatch): Promise<string | null> => {
    if (!row.release_group_id) return null;
    const group: { releases?: { id?: string; track_count?: number }[] } = await withRetry(() =>
      api.mbReleaseGroup(row.release_group_id!)
    );
    const releases = group?.releases ?? [];
    const pick = releases.find((r) => r.track_count === row.total) ?? releases[0];
    return pick?.id ?? null;
  };

  /** "Use this release" — resolves the matched release group to a release and
   *  hands it to the SAME fetch + auto-match flow the wizard already uses
   *  (api.mbReleaseGroup → api.mbMatch), so tags are written by one writer.
   *  Accepting the match also writes its AcoustID identity into the files
   *  (apply=true: ACOUSTID_ID + ACOUSTID_FINGERPRINT). In queue mode the
   *  wizard follows the album the release is for. */
  const useAcoustidRelease = async (row: AcoustidAlbumMatch) => {
    const index = uploaded.findIndex((a) => a.path === row.path);
    if (index >= 0 && index !== albumIndex) switchAlbum(index);
    setAcoustidBusy(true);
    setFetchStatus("Resolving the release group on MusicBrainz…");
    try {
      const rid = await releaseForRow(row);
      if (!rid) {
        toast("That release group has no releases on MusicBrainz — search manually below");
        return;
      }
      setDetectedFromTags(false);
      setMbLink(`https://musicbrainz.org/release/${rid}`);
      setReleaseId(rid);
      await pickRelease(rid, row.path);
      // Accepting the match: keep the fingerprint on the album, so the
      // on-disk check agrees with what was just matched.
      try {
        const applied = await api.importAcoustid([row.path], true);
        const tagged = applied.albums?.find((a) => a.path === row.path)?.tagged ?? 0;
        toast(
          tagged
            ? `Identity tags written to ${tagged} track(s)`
            : "AcoustID identity tags not written — the files carry none of the tag families it targets"
        );
      } catch (e) {
        toast(`Matched, but the AcoustID identity tags failed: ${e}`);
      }
    } catch (e) {
      toast.error(String(e));
    } finally {
      setAcoustidBusy(false);
      setFetchStatus(null);
    }
  };

  /** Queue mode "match to release": the release chosen above is applied to
   *  EVERY staged album — one release across the queue (a box set's discs, a
   *  rip split over several folders). Per-album failures are reported, never
   *  fatal: the albums are already in the library. */
  const matchQueueToRelease = async () => {
    const rid = releaseId || extractMbid(mbLink) || "";
    if (!uploaded.length || !rid) {
      toast("Pick a MusicBrainz release first (Links step, or AcoustID above)");
      return;
    }
    setMatchAllBusy(true);
    const failed: string[] = [];
    let matched = 0;
    try {
      for (const a of uploaded) {
        setFetchStatus(`Matching ${a.name} to the release…`);
        try {
          const res = await api.mbMatch(a.path, rid);
          if (!res.suggestions.length) {
            failed.push(`${a.name}: no audio files`);
            continue;
          }
          await assignTracks(a.path, res.release, res.suggestions);
          matched += 1;
        } catch (e) {
          failed.push(`${a.name}: ${e}`);
        }
      }
      toast(
        failed.length
          ? `Matched ${matched}/${uploaded.length} album(s) — ${failed.slice(0, 3).join("; ")}`
          : `Matched and tagged ${matched} album(s) to the release`
      );
      qc.invalidateQueries({ queryKey: ["library"] });
    } finally {
      setMatchAllBusy(false);
      setFetchStatus(null);
    }
  };

  // ---------------- Step 2: matching ----------------
  const setSuggestion = (path: string, disc: number, position: number) => {
    const m = release?.media.find((x) => x.disc === disc && x.position === position);
    setSuggestions((ss) => ss.map((s) => (s.local === path ? { ...s, matched: !!m, confidence: 1, release_track: m ?? null } : s)));
  };

  /** Write ONE album's matched rows to its files. The wizard's only tag
   *  writer: the single-album Confirm calls it for the album on screen, the
   *  queue's "match to release" calls it once per queued album. */
  const assignTracks = async (path: string, release: MBRelease, suggestions: MatchSuggestion[]) => {
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
        // UNPADDED: a file tag stores "1", never "01". Zero-padding is a
        // FILENAME/display convention (the naming script's pattern), not a
        // metadata one — MusicBrainz, Picard, beets and every player write
        // the bare integer, and a padded tag sorts and diffs wrong against
        // them.
        TRACKNUMBER: t ? String(t.position) : null,
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
    // Record the RELEASE's own tracklist on the folder. A partial import
    // (some of these tracks never brought in) leaves no other trace of what
    // is absent, so the album page diffs against this list and greys out
    // the missing rows. A failure here must not lose the tag writes that
    // just succeeded, so it is reported and the caller moves on.
    try {
      await api.importExpected(
        path,
        release.id,
        (release.media ?? []).map((m) => ({
          disc: m.disc,
          position: m.position,
          title: m.title,
          recording_mbid: m.recording_mbid ?? null,
        }))
      );
    } catch (e) {
      toast(`Metadata saved, but the release tracklist could not be recorded: ${e}`);
    }
  };

  const confirmMatch = async () => {
    if (!albumPath || !release) {
      toast("Fetch the MusicBrainz release first");
      return;
    }
    setBusy(true);
    setFetchStatus("Writing MusicBrainz metadata to files…");
    try {
      await assignTracks(albumPath, release, suggestions);
      toast("MusicBrainz metadata written to files (titles, artists, album, dates, MBIDs)");
      setStep(3);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
      setFetchStatus(null);
    }
  };

  // ---------------- Step 3: covers ----------------
  /** How many tracks point at the same image (manifest art or sidecar). */
  const coverShares = useMemo(() => {
    const m = new Map<string, number>();
    for (const t of stepTracks) if (t.cover_file) m.set(t.cover_file, (m.get(t.cover_file) ?? 0) + 1);
    return m;
  }, [stepTracks]);

  /** The release picked in the Links step, via the Cover Art Archive. */
  const mbCoverUrl = releaseId ? `https://coverartarchive.org/release/${releaseId}/front-500` : null;

  const refreshCovers = () => {
    qc.invalidateQueries({ queryKey: ["library"] });
    qc.invalidateQueries({ queryKey: ["coverInfo", albumPath] });
  };

  /** Cover results: amber banner when the image is under the minimum size
   *  (the backend writes it anyway and says so), toast for the outcome.
   *  The backend compresses on write, so the toast reports the RESULTING
   *  size and what it was shrunk from. */
  const reportCover = (
    res: {
      warning?: string | null;
      width?: number; height?: number;
      below_target?: boolean;
      compressed?: boolean;
      original_width?: number | null;
      original_height?: number | null;
    },
    what: string
  ) => {
    setCoverNotice(res.warning ?? null);
    const dims = res.width && res.height ? ` (${res.width}×${res.height})` : "";
    const shrunk =
      res.compressed && res.original_width && res.width && res.original_width > res.width
        ? ` — compressed from ${res.original_width}×${res.original_height}`
        : res.compressed
          ? " — compressed"
          : "";
    toast(
      res.warning
        ? `${what} applied${dims}${shrunk} — ${res.warning}`
        : `${what} applied${dims}${shrunk}`
    );
  };

  const uploadCover = async (file: File, tracks?: string[]) => {
    if (!albumPath) return;
    if (!file.type.startsWith("image/")) {
      toast(`${file.name} is not an image — use a jpg, png or webp`);
      return;
    }
    setBusy(true);
    try {
      const res = tracks?.length
        ? await api.cover(albumPath, file, undefined, tracks)
        : await api.cover(albumPath, file);
      reportCover(res, tracks?.length ? `Cover assigned to ${tracks.length} track(s)` : "Album cover");
      refreshCovers();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const applyCoverUrl = async (url: string, tracks?: string[]) => {
    const u = url.trim();
    if (!albumPath || !u) return;
    setBusy(true);
    try {
      const res = tracks?.length
        ? await api.coverFromUrl(albumPath, u, undefined, tracks)
        : await api.coverFromUrl(albumPath, u);
      reportCover(res, tracks?.length ? `Cover assigned to ${tracks.length} track(s)` : "Album cover");
      refreshCovers();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const clearTrackCovers = async () => {
    if (!albumPath || !coverSel.size) return;
    setBusy(true);
    try {
      await api.coverClear(albumPath, [...coverSel]);
      toast(`Per-track cover cleared for ${coverSel.size} track(s)`);
      refreshCovers();
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const toggleCoverSel = (path: string) =>
    setCoverSel((s) => {
      const next = new Set(s);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });

  const setCoverSelFor = (paths: string[], on: boolean) =>
    setCoverSel((s) => {
      const next = new Set(s);
      for (const p of paths) {
        if (on) next.add(p);
        else next.delete(p);
      }
      return next;
    });

  /** The cover endpoints take audio FILENAMES, the selection holds paths. */
  const selectedCoverFiles = () => [...coverSel].map((p) => p.split("/").pop()!);

  const saveCovers = () => {
    setCoverNotice(null);
    setStep(4); // Genres
  };

  // ---------------- Step 4: genres ----------------
  // When the Genres step opens, prefill untouched tracks with their existing
  // GENRE tags so they are visible and editable right away.
  useEffect(() => {
    if (step !== 4) return;
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

  /** Every genre currently on ANY track, with how many tracks carry it, most
   *  common first — the album-wide cleanup control renders one chip per entry. */
  const allGenres = useMemo(() => {
    const m = new Map<string, number>();
    for (const t of stepTracks) {
      for (const gen of new Set(genreList(t.path))) m.set(gen, (m.get(gen) ?? 0) + 1);
    }
    return [...m.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stepTracks, genres]);

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

  /** Drop one genre from EVERY track — album-wide cleanup of a bad genre. */
  const removeGenreEverywhere = (genre: string) =>
    setGenres((g) => {
      const next = { ...g };
      for (const t of stepTracks) {
        const list = (next[t.path] ?? "").split(";").map((x) => x.trim()).filter(Boolean);
        if (list.includes(genre)) next[t.path] = list.filter((x) => x !== genre).join("; ");
      }
      return next;
    });

  /** Drop ALL genres from every track, leaving the step empty to re-import. */
  const removeAllGenres = () =>
    setGenres((g) => {
      const next = { ...g };
      for (const t of stepTracks) next[t.path] = "";
      return next;
    });

  const saveGenres = async () => {
    setBusy(true);
    try {
      const writes: Record<string, Record<string, string | null>> = {};
      for (const [p, g] of Object.entries(genres)) writes[p] = { GENRE: g || null };
      await api.mbAssign(writes);
      toast("Genres saved");
      setStep(5);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  // ---------------- Step 5: lyrics ----------------
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

  /** The richest row for a path: the library payload first, the step's own
   *  rows (suggestions / folder scan) second. */
  const findTrack = (p: string): Track | undefined =>
    trackList.find((x) => x.path === p) ?? stepTracks.find((x) => x.path === p);

  /** (disc, track) digits from a leading "D-TT" / "TT" file name — the same
   *  fallback the library payload derives for untagged files. */
  const numsFromName = (file: string): { disc: number | null; track: number | null } => {
    const stem = (file.split("/").pop() ?? "").replace(/\.[^.]+$/, "");
    const dd = stem.match(/^\s*(\d+)\s*-\s*(\d+)/);
    if (dd) return { disc: Number(dd[1]), track: Number(dd[2]) };
    const tt = stem.match(/^\s*(\d+)/);
    return { disc: null, track: tt ? Number(tt[1]) : null };
  };

  /** Track number shown on EVERY step: the matched MusicBrainz position when
   *  there is one (that is the number written to the file), else the local tag,
   *  else the number derived from the file name. */
  const trackNoOf = (p: string): number | null => {
    const m = suggByPath.get(p)?.release_track;
    if (m) return m.position;
    const t = findTrack(p);
    if (!t) return null;
    return parseDisc(t.tags?.TRACKNUMBER) ?? t.tracknumber ?? numsFromName(t.file || p).track;
  };

  /** Disc number shown on EVERY step, same precedence as {@link trackNoOf}. */
  const discNoOf = (p: string): number | null => {
    const m = suggByPath.get(p)?.release_track;
    if (m) return m.disc;
    const t = findTrack(p);
    if (!t) return null;
    return parseDisc(t.tags?.DISCNUMBER) ?? t.discnumber ?? numsFromName(t.file || p).disc;
  };

  /** Whether this track will actually carry lyrics once the step is saved.
   *  A checkmark used to appear for any track whose .lrc sidecar merely
   *  EXISTED, so empty sidecars from an aborted run claimed lyrics they did
   *  not have. Instrumentals never count. */
  const hasLyrics = (t: Track): boolean => {
    if ((instrumental[t.path] ?? t.tags.INSTRUMENTAL) === "1") return false;
    const draft = lyricsDrafts[t.path];
    if (draft && draft.trim()) return true;   // what this step is about to write
    return !!(t.lyrics_embedded || t.lyrics_lrc); // what is already on disk
  };

  /** Auto-import lyrics through the configured provider chain (script 13's own
   *  engine): one track from a track row, the whole step from the header.
   *  Reports the winning provider per track plus skipped/failed counts — the
   *  chain order lives in Settings → Lyrics. */
  const autoImportLyrics = async (paths?: string[]) => {
    const targets = paths ?? stepTracks.map((t) => t.path);
    if (!targets.length) {
      toast("No tracks to fetch lyrics for");
      return;
    }
    setBusy(true);
    try {
      const res = await api.lyricsAuto(targets);
      setLyrResults((m) => {
        const next = { ...m };
        for (const r of res.results) next[r.path] = r;
        return next;
      });
      const byProvider = new Map<string, number>();
      for (const r of res.results) {
        if (r.status === "ok" && r.provider_label)
          byProvider.set(r.provider_label, (byProvider.get(r.provider_label) ?? 0) + 1);
      }
      const got = [...byProvider].map(([label, n]) => `${label} ${n}`).join(", ");
      const rest = [
        res.skipped ? `${res.skipped} skipped` : "",
        res.failed ? `${res.failed} failed` : "",
      ].filter(Boolean).join(", ");
      toast(
        res.ok
          ? `Lyrics written for ${res.ok} track(s)${got ? ` — ${got}` : ""}${rest ? ` (${rest})` : ""}`
          : `No new lyrics found${rest ? ` — ${rest}` : ""}`
      );
      qc.invalidateQueries({ queryKey: ["library"] });
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  const saveLyricsStep = async () => {
    setBusy(true);
    try {
      // Write what the config asks for — the wizard used to always write .lrc.
      const cfg = await api.config();
      const fmt = String(cfg.lyrics_format ?? "EMBEDDED").toUpperCase(); // EMBEDDED | LRC | BOTH
      const writes: Record<string, Record<string, string | null>> = {};
      let embedded = 0;
      let sidecars = 0;
      let untimed = 0;
      for (const t of stepTracks) {
        const inst = instrumental[t.path] ?? (t.tags.INSTRUMENTAL === "1" ? "1" : "0");
        writes[t.path] = { INSTRUMENTAL: inst };
        if (inst === "1") continue; // instrumentals never carry lyrics
        const lrc = lyricsDrafts[t.path];
        if (!lrc || !lrc.trim()) continue;
        if (!parseLrc(lrc).length) {
          untimed++; // plain text: nothing to timestamp, nothing written
          continue;
        }
        if (fmt === "LRC" || fmt === "BOTH") {
          await api.lyricsWrite(t.path, lrc);
          sidecars++;
        }
        if (fmt === "EMBEDDED" || fmt === "BOTH") {
          await api.lyricsEmbed(t.path, lrc);
          embedded++;
        }
      }
      await api.mbAssign(writes);
      setLyricsNotice(
        untimed ? `${untimed} track(s) have lyrics without timestamps — not saved as .lrc` : null
      );
      toast(`lyrics_format=${fmt}: ${embedded} embedded, ${sidecars} .lrc — INSTRUMENTAL saved`);
      setStep(6);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  // ---------------- Step 6: advisory ----------------
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
        setStep(7);
        return;
      }
      await api.mbAssign(writes);
      toast("Advisory ratings saved");
      setStep(7);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(false);
    }
  };

  // The post-import chain comes from lib/scripts.ts — the single source of
  // truth every other script menu in the app already uses. This was an
  // 8-entry list hardcoded here, and it silently drifted: AccurateRip, Format
  // all, Remux videos, Key & BPM, Fetch lyrics and Beets were all
  // missing, so "Run all scripts" ran 8 of the 14 that exist.
  const POST_IMPORT_DEFAULT_ON = new Set([1, 2, 5, 7, 4]);
  const POST_IMPORT_SCRIPTS = SCRIPTS.map((s) => ({
    id: s.ids[0],
    label: s.label,
    defaultOn: POST_IMPORT_DEFAULT_ON.has(s.ids[0]),
  }));
const [runAfterImport, setRunAfterImport] = useState<number[]>(
  POST_IMPORT_SCRIPTS.filter((s) => s.defaultOn).map((s) => s.id)
);
const [scriptsRunning, setScriptsRunning] = useState(false);

/** Run every configured post-import script on the new album(s) right now,
 *  without leaving the wizard: api.importFinish is the same chain the bulk
 *  queue and the Soulseek import run, and reports per-script errors. The
 *  checkboxes stay the "on Done" shortcut for a chosen subset. */
const runAllScripts = async () => {
  if (!uploaded.length) {
    toast("Nothing imported yet");
    return;
  }
  setScriptsRunning(true);
  try {
    const res = await api.importFinish(uploaded.map((a) => a.path));
    const errors = res.albums.flatMap((a) =>
      a.errors.map((e) => `${baseName(a.path) || a.path}: ${String(e)}`)
    );
    toast(
      errors.length
        ? `Import chain finished with ${errors.length} script error(s): ${errors.slice(0, 3).join("; ")}`
        : `Import chain finished on ${uploaded.length} album(s) — progress shows at the top of the window`
    );
    qc.invalidateQueries({ queryKey: ["library"] });
  } catch (e) {
    toast.error(String(e));
  } finally {
    setScriptsRunning(false);
  }
};

const finish = async () => {
  try {
    if (runAfterImport.length && uploaded.length) {
      await api.run(runAfterImport, uploaded.map((a) => a.path));
    }
  } catch (e) {
    toast.error(String(e));
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
    setCoverNotice(null);
    setLyricsNotice(null);
    setLyrResults({});
    setCoverSel(new Set());
    setCoverUrl("");
    setTrackCoverUrl("");
    setCoverSearchOpen(false);
  };

  const totalFiles = albums.reduce((n, g) => n + g.files.length, 0);
  // Queue panel rows: the staged albums, else what step 0 is about to import.
  const queueItems: { name: string; path: string }[] = uploaded.length
    ? uploaded
    : albums.filter((g) => g.files.length).map((g) => ({ name: g.name.trim() || "Album", path: "" }));
  const hasRipFiles = albums.some((g) => g.files.some((f) => /\.(cue|log|accurip)$/i.test(f.relPath)));
  const canNext =
    step === 0
      ? totalFiles > 0 && albums.length > 0 && albums.every((g) => g.name.trim() || g.files.length === 0)
      : step === 1
        ? !!(releaseId || extractMbid(mbLink))
        : true;
  // Why Continue is disabled — shown next to the button instead of leaving
  // the user with a dead button.
  const nextBlock = (() => {
    if (canNext) return null;
    if (step === 0)
      return totalFiles === 0
        ? "Add files or pick a folder first"
        : !albums.length
          ? "Nothing to import"
          : "Every album needs a name";
    if (step === 1) return "Enter a MusicBrainz release URL or ID first";
    return null;
  })();

  return (
    <div className="p-6 max-w-6xl mx-auto space-y-5">
      <PageHeader
        icon={UploadCloud}
        title="Import"
        subtitle={
          uploaded.length > 1 || albumPath ? (
            <>
              {uploaded.length > 1 && (
                <select className="input !w-auto text-sm" value={albumIndex} onChange={(e) => switchAlbum(Number(e.target.value))}>
                  {uploaded.map((a, i) => (
                    <option key={a.path} value={i}>{a.name}</option>
                  ))}
                </select>
              )}
              {albumPath && (
                <span className="truncate">
                  <Link to={`/album/${encodeURIComponent(albumPath)}`} className="hover:text-accent-soft">
                    {albumPath.split("/").pop()}
                  </Link>
                </span>
              )}
            </>
          ) : undefined
        }
      />

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

      {/* ---------------- Queue mode: several albums at once ---------------- */}
      {queueMode && (
        <div className="panel p-3 space-y-2">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-semibold text-zinc-200 flex items-center gap-1.5">
              <Disc3 className="h-4 w-4 text-accent" /> Queue mode — {queueItems.length} album(s)
            </span>
            {bulkJob?.status === "running" && (
              <span className="text-xs text-zinc-400 flex items-center gap-1.5">
                <span className="h-3 w-3 rounded-full border-2 border-zinc-700 border-t-accent-soft animate-spin shrink-0" />
                <span className="truncate max-w-[16rem]" title={bulkJob.label}>{bulkJob.label || "Importing…"}</span>
                <span className="font-mono tabular-nums shrink-0">
                  {bulkJob.done ?? 0}/{bulkJob.total ?? queueItems.length}
                </span>
              </span>
            )}
            {bulkJob?.status === "done" && (
              <span className="chip bg-emerald-900/50 text-emerald-300 border border-emerald-800">
                <Check className="h-3 w-3" /> queue done — {bulkJob.done ?? 0}/{bulkJob.total ?? queueItems.length}
              </span>
            )}
            {bulkJob?.status === "failed" && (
              <span className="chip bg-red-900/40 text-red-300 border border-red-900">queue failed</span>
            )}
            <span className="text-xs text-zinc-500 ml-auto">
              {uploaded.length
                ? "The 8 steps below run on the album picked in the dropdown; shared actions cover the whole queue."
                : "Importing runs the queue; each album can then be walked through the 8 steps."}
            </span>
          </div>
          <ScriptChainNote preview={scriptChain} />
          <div className="space-y-1">
            {queueItems.map((it, i) => {
              const row = it.path ? bulkJob?.items?.find((r) => r.path === it.path) : undefined;
              const state = row?.status ?? "queued";
              return (
                <div
                  key={it.path || `new-${i}`}
                  className="flex items-center gap-2 bg-panel rounded border border-border px-3 py-1.5 text-xs"
                >
                  <Disc3 className="h-3.5 w-3.5 text-zinc-500 shrink-0" />
                  {uploaded.length ? (
                    <button
                      className={`flex-1 min-w-0 text-left truncate ${
                        i === albumIndex ? "text-accent-soft" : "text-zinc-300 hover:text-accent-soft"
                      }`}
                      onClick={() => switchAlbum(i)}
                      title="Work this album through the steps below"
                    >
                      {it.name}
                    </button>
                  ) : (
                    <span className="flex-1 min-w-0 truncate text-zinc-300">{it.name}</span>
                  )}
                  {row?.error && (
                    <span className="text-[10px] text-red-300/90 truncate max-w-[18rem]" title={row.error}>
                      {row.error}
                    </span>
                  )}
                  <span
                    className={`chip bg-raise border border-border shrink-0 ${
                      state === "imported"
                        ? "text-emerald-300"
                        : state === "failed"
                          ? "text-red-300"
                          : state === "skipped"
                            ? "text-amber-300"
                            : "text-zinc-500"
                    }`}
                  >
                    {state}
                  </span>
                </div>
              );
            })}
          </div>
          {bulkJob?.status === "failed" && bulkJob.error && (
            <div className="text-xs text-red-300">Queue import failed: {bulkJob.error}</div>
          )}
          <AcoustidBlock
            match={acoustid}
            busy={acoustidBusy}
            queue
            canMatchAll={!!(releaseId || extractMbid(mbLink))}
            matchAllBusy={matchAllBusy}
            onRun={runAcoustid}
            onUse={useAcoustidRelease}
            onMatchAll={matchQueueToRelease}
          />
        </div>
      )}

      {/* ---------------- Step 0: select & separate ---------------- */}
      {step === 0 && (
        <div className="space-y-4">
          <div
            className="panel-hero border-2 border-dashed p-10 text-center hover:border-accent/60 transition-colors cursor-pointer"
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
                <div key={gi} className="panel p-3">
                  <div className="flex items-center gap-2 mb-2">
                    <Disc3 className="h-4 w-4 text-zinc-500 shrink-0" />
                    <input
                      className="input !w-auto min-w-[200px] font-medium"
                      value={g.name}
                      placeholder="Album name"
                      onChange={(e) => renameGroup(gi, e.target.value)}
                    />
                    <span className="text-xs text-zinc-500">
                      {g.files.length - g.files.filter((f) => excluded.has(f.relPath)).length} of {g.files.length} file(s)
                      {g.files.some((f) => excluded.has(f.relPath)) && (
                        <span className="text-amber-300/90"> — partial album</span>
                      )}
                    </span>
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
                        selectable={source === "web"}
                        excluded={excluded.has(f.relPath)}
                        onToggleExcluded={() =>
                          setExcluded((s) => {
                            const next = new Set(s);
                            if (next.has(f.relPath)) next.delete(f.relPath);
                            else next.add(f.relPath);
                            return next;
                          })
                        }
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
          {/* Queue mode carries this block in the queue panel above. */}
          {!queueMode && (
            <AcoustidBlock
              match={acoustid}
              busy={acoustidBusy}
              queue={false}
              canMatchAll={false}
              matchAllBusy={false}
              onRun={runAcoustid}
              onUse={useAcoustidRelease}
              onMatchAll={matchQueueToRelease}
            />
          )}
          <div className="panel p-4 space-y-3">
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
                return (
                  <div key={s.local} className="flex items-center gap-3 panel px-3 py-2">
                    <TrackNoBadge disc={discNoOf(s.local)} track={trackNoOf(s.local)} />
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

      {/* ---------------- Step 3: covers ---------------- */}
      {step === 3 && albumPath && (
        <div className="space-y-4">
          {coverNotice && (
            <div className="rounded-lg border border-amber-900/60 bg-amber-950/30 px-3 py-2 text-xs text-amber-200">
              {coverNotice}
            </div>
          )}

          <div className="grid md:grid-cols-2 gap-3">
            <div className="panel p-4 space-y-2">
              <div className="text-sm font-semibold text-zinc-300">Current album cover</div>
              <CoverImg
                albumPath={albumPath}
                coverFile={coverInfo?.file}
                wrapperClass="h-40 w-40 rounded-lg bg-raise border border-border overflow-hidden"
              />
              <div className="text-xs text-zinc-500">
                {coverInfo?.file
                  ? `${coverInfo.file} — ${coverInfo.width ?? "?"}×${coverInfo.height ?? "?"} px · ${Math.max(1, Math.round(coverInfo.bytes / 1024))} KB`
                  : "No album cover on disk yet."}
              </div>
              <div className="flex items-center gap-2 flex-wrap">
                <button className="btn-ghost !py-1 text-xs" onClick={() => albumCoverInput.current?.click()} disabled={busy}>
                  <UploadCloud className="h-3.5 w-3.5" /> Upload image
                </button>
                <button className="btn-ghost !py-1 text-xs" onClick={() => setCoverSearchOpen(true)} disabled={busy}>
                  Search covers
                </button>
              </div>
            </div>

            <div className="panel p-4 space-y-2">
              <div className="text-sm font-semibold text-zinc-300">MusicBrainz release cover</div>
              <div className="text-xs text-zinc-500">
                The release's own cover — compare it with musichoarders before you accept it.
              </div>
              {mbCoverUrl ? (
                <img
                  src={mbCoverUrl}
                  alt="MusicBrainz release cover"
                  referrerPolicy="no-referrer"
                  className="h-40 w-40 rounded-lg bg-raise border border-border object-cover"
                />
              ) : (
                <div className="h-40 w-40 rounded-lg bg-raise border border-border flex items-center justify-center text-center px-3 text-xs text-zinc-600">
                  No release picked yet — fetch it in the Links step.
                </div>
              )}
              <div className="flex items-center gap-2 flex-wrap">
                <button
                  className="btn-ghost !py-1 text-xs"
                  onClick={() => mbCoverUrl && applyCoverUrl(mbCoverUrl)}
                  disabled={busy || !mbCoverUrl}
                >
                  <CloudDownloadIcon /> Use the MusicBrainz cover
                </button>
                {mbCoverUrl && (
                  <a
                    href={mbCoverUrl}
                    target="_blank"
                    rel="noreferrer"
                    className="text-xs text-accent-soft underline underline-offset-2"
                  >
                    open on coverartarchive.org
                  </a>
                )}
              </div>
            </div>
          </div>

          <div className="panel p-3 space-y-2">
            <div className="text-xs text-zinc-400">
              MusicBrainz cover wrong? Find the correct one on{" "}
              <a
                href="https://covers.musichoarders.xyz"
                target="_blank"
                rel="noreferrer"
                className="text-accent-soft underline underline-offset-2"
              >
                covers.musichoarders.xyz <ExternalLink className="inline h-3 w-3" />
              </a>
              , then apply it here with Search covers or an image URL.
            </div>
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-xs font-semibold text-zinc-400">Album cover from URL</span>
              <input
                className="input flex-1 min-w-[240px] !py-1 text-xs"
                placeholder="https://…/cover.jpg"
                value={coverUrl}
                onChange={(e) => setCoverUrl(e.target.value)}
              />
              <button
                className="btn-ghost !py-1 text-xs"
                onClick={() => applyCoverUrl(coverUrl)}
                disabled={busy || !coverUrl.trim()}
              >
                <ExternalLink className="h-3.5 w-3.5" /> Apply to album
              </button>
            </div>
          </div>

          <div className="panel p-3 space-y-2">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-sm font-semibold text-zinc-300">Per-track covers</span>
              <span className="text-xs text-zinc-500">
                {coverSel.size} selected — one image applied to several tracks is stored once and shared between them (that is how
                tracks 7 and 8 get the same art).
              </span>
              <button
                className="btn-ghost !py-1 text-xs ml-auto"
                onClick={() =>
                  setCoverSel(
                    coverSel.size && coverSel.size === stepTracks.length
                      ? new Set()
                      : new Set(stepTracks.map((t) => t.path))
                  )
                }
              >
                {coverSel.size && coverSel.size === stepTracks.length ? "Select none" : "Select all"}
              </button>
            </div>
            <div className="flex items-center gap-2 flex-wrap">
              <button
                className="btn-ghost !py-1 text-xs"
                onClick={() => trackCoverInput.current?.click()}
                disabled={busy || !coverSel.size}
              >
                <UploadCloud className="h-3.5 w-3.5" /> Upload to selected
              </button>
              <input
                className="input !w-64 !py-1 text-xs"
                placeholder="Cover image URL for the selection…"
                value={trackCoverUrl}
                onChange={(e) => setTrackCoverUrl(e.target.value)}
              />
              <button
                className="btn-ghost !py-1 text-xs"
                onClick={() => applyCoverUrl(trackCoverUrl, selectedCoverFiles())}
                disabled={busy || !coverSel.size || !trackCoverUrl.trim()}
              >
                <ExternalLink className="h-3.5 w-3.5" /> Use URL
              </button>
              <button
                className="btn-ghost !py-1 text-xs"
                onClick={() => mbCoverUrl && applyCoverUrl(mbCoverUrl, selectedCoverFiles())}
                disabled={busy || !coverSel.size || !mbCoverUrl}
              >
                <CloudDownloadIcon /> MusicBrainz cover
              </button>
              <button className="btn-danger !py-1 text-xs" onClick={clearTrackCovers} disabled={busy || !coverSel.size}>
                <Trash2 className="h-3.5 w-3.5" /> Clear per-track cover
              </button>
            </div>
            {stepTracks.length === 0 && (
              <div className="text-xs text-zinc-500">No tracks — go back and fetch the release.</div>
            )}
            {groupByDisc(stepTracks, discOfTrack).map((g) => (
              <DiscSection
                key={g.disc ?? "unmatched"}
                disc={g.disc}
                count={g.rows.length}
                collapsed={collapsedDiscs.has(g.disc ?? null)}
                onToggle={() => toggleDisc(g.disc ?? null)}
                extra={
                  <>
                    <button
                      className="btn-ghost !py-0.5 !px-1.5 text-[11px]"
                      onClick={() => setCoverSelFor(g.rows.map((t) => t.path), true)}
                    >
                      All
                    </button>
                    <button
                      className="btn-ghost !py-0.5 !px-1.5 text-[11px]"
                      onClick={() => setCoverSelFor(g.rows.map((t) => t.path), false)}
                    >
                      None
                    </button>
                  </>
                }
              >
                {g.rows.map((t) => {
                  const shared = t.cover_file ? (coverShares.get(t.cover_file) ?? 0) - 1 : 0;
                  return (
                    <label
                      key={t.path}
                      className={`flex items-center gap-3 panel px-3 py-2 cursor-pointer ${
                        coverSel.has(t.path) ? "border-accent/60" : "border-border"
                      }`}
                    >
                      <input type="checkbox" checked={coverSel.has(t.path)} onChange={() => toggleCoverSel(t.path)} />
                      <TrackCover albumPath={albumPath} trackCover={t.cover_file} albumCover={coverInfo?.file} />
                      <TrackNoBadge disc={discNoOf(t.path)} track={trackNoOf(t.path)} />
                      <span className="flex-1 truncate text-sm">{displayTitle(t.path)}</span>
                      {t.cover_file ? (
                        <span
                          className="chip bg-accent/10 text-accent-soft border border-accent/25 shrink-0"
                          title={t.cover_file}
                        >
                          own art{shared > 0 ? ` · shared with ${shared}` : ""}
                        </span>
                      ) : (
                        <span className="chip bg-raise border border-border text-zinc-500 shrink-0">album cover</span>
                      )}
                    </label>
                  );
                })}
              </DiscSection>
            ))}
          </div>

          <div className="flex items-center gap-2 justify-end">
            <span className="text-xs text-zinc-500">Covers are written as you apply them — Continue just moves on.</span>
            <button className="btn-primary" onClick={saveCovers}>Continue to genres</button>
          </div>

          <input
            ref={albumCoverInput}
            type="file"
            accept="image/*"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              e.target.value = "";
              if (f) uploadCover(f);
            }}
          />
          <input
            ref={trackCoverInput}
            type="file"
            accept="image/*"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              const files = selectedCoverFiles();
              e.target.value = "";
              if (f && files.length) uploadCover(f, files);
            }}
          />

          {coverSearchOpen && (
            <CoverSearchModal
              albumPath={albumPath}
              artist={release?.artists.map((a) => a.name).join(", ") || trackArtist(stepTracks[0]?.path ?? "")}
              album={trackAlbum || currentAlbumName}
              releaseGroupMbid={release?.release_group_id ?? undefined}
              tracks={coverSel.size ? selectedCoverFiles() : undefined}
              onClose={() => setCoverSearchOpen(false)}
              onApplied={refreshCovers}
            />
          )}
        </div>
      )}

      {/* ---------------- Step 4: genres ---------------- */}
      {step === 4 && (
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
          {/* Album-wide cleanup: every genre currently on any track, one click
              to strip it from ALL of them, plus a clear-everything button.
              A wrong genre lands on a whole album at once, so removing it
              everywhere is the common correction — and until now it could only
              be done one track at a time. */}
          {allGenres.length > 0 && (
            <div className="panel px-3 py-2 flex items-center gap-1.5 flex-wrap">
              <span className="text-xs font-semibold text-zinc-400 shrink-0">Remove a genre everywhere:</span>
              {allGenres.map(([gen, n]) => (
                <button
                  key={gen}
                  className="chip bg-raise border border-border text-zinc-300 hover:border-red-800 hover:text-red-200"
                  onClick={() => removeGenreEverywhere(gen)}
                  title={`Remove “${gen}” from all ${n} track(s)`}
                >
                  {gen}
                  <span className="text-[10px] text-zinc-500 tabular-nums">{n}</span>
                  <X className="h-3 w-3" />
                </button>
              ))}
              <button
                className="btn-danger !py-1 text-xs ml-auto"
                onClick={removeAllGenres}
                title="Clear the genre field on every track in this step"
              >
                <Trash2 className="h-3.5 w-3.5" /> Remove all genres
              </button>
            </div>
          )}
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
                <div key={t.path} className="flex items-center gap-3 panel px-3 py-2">
                  <TrackNoBadge disc={discNoOf(t.path)} track={trackNoOf(t.path)} />
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

      {/* ---------------- Step 5: lyrics ---------------- */}
      {step === 5 && (
        <div className="space-y-4">
          {lyricsNotice && (
            <div className="rounded-lg border border-amber-900/60 bg-amber-950/30 px-3 py-2 text-xs text-amber-200">
              {lyricsNotice}
            </div>
          )}
          <div className="flex items-center gap-2">
            <button className="btn-primary text-xs" onClick={() => autoImportLyrics()} disabled={busy}>
              <CloudDownloadIcon /> Auto-import lyrics
            </button>
            <span className="text-xs text-zinc-500">
              Tries every provider in the saved order (Settings → Lyrics) and writes them straight to the
              files. Review below — Space stamps time while previewing; INSTRUMENTAL=1 skips lyrics.
            </span>
          </div>
          {Object.keys(lyrResults).length > 0 && (
            <div className="panel px-3 py-2 text-xs space-y-0.5">
              <div className="flex items-center gap-3 text-zinc-400 flex-wrap">
                {(["ok", "skipped", "failed"] as const).map((status) => {
                  const rows = Object.values(lyrResults).filter((r) => r.status === status);
                  if (!rows.length) return null;
                  const labels = [...new Set(rows.map((r) => r.provider_label).filter(Boolean))];
                  return (
                    <span key={status}>
                      <b className="text-zinc-200">{rows.length}</b> {status}
                      {status === "ok" && labels.length > 0 && ` — ${labels.join(", ")}`}
                    </span>
                  );
                })}
              </div>
              <div className="text-[10px] text-zinc-600">
                Per track below — nothing found is normal for instrumentals and unreleased tracks.
              </div>
            </div>
          )}
          {stepTracks.map((t) => {
            const inst = instrumental[t.path] ?? t.tags.INSTRUMENTAL;
            const hasDraft = hasLyrics(t);
            return (
              <details key={t.path} className="panel open:pb-3">
                <summary className="px-3 py-2 text-sm font-medium cursor-pointer flex items-center gap-2">
                  <TrackNoBadge disc={discNoOf(t.path)} track={trackNoOf(t.path)} />
                  <span className="flex-1 truncate">{displayTitle(t.path)}</span>
                  {hasDraft && (
                    <span className="chip bg-emerald-900/60 text-emerald-300 border border-emerald-800">
                      <Check className="h-3 w-3" /> Lyrics
                    </span>
                  )}
                  {lyrResults[t.path] && (
                    <span
                      className={`chip border ${
                        lyrResults[t.path].status === "ok"
                          ? "bg-emerald-900/50 text-emerald-300 border-emerald-800"
                          : lyrResults[t.path].status === "failed"
                            ? "bg-red-900/40 text-red-300 border-red-900"
                            : "bg-raise text-zinc-500 border-border"
                      }`}
                      title={lyrResults[t.path].reason || lyrResults[t.path].error || ""}
                    >
                      {lyrResults[t.path].status === "ok" ? lyrResults[t.path].provider_label : lyrResults[t.path].status}
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
                  <div className="px-3 space-y-1.5">
                    <button
                      className="btn-ghost !py-0.5 text-[11px]"
                      onClick={() => autoImportLyrics([t.path])}
                      disabled={busy}
                      title="Fetch this track's lyrics through the provider chain and write them to the file"
                    >
                      <CloudDownloadIcon /> Auto-import lyrics
                    </button>
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

      {/* ---------------- Step 6: advisory ---------------- */}
      {step === 6 && (
        <div className="space-y-3">
          <div className="text-sm text-zinc-400">Set iTunes advisory per track: <b className="text-zinc-200">0</b> unrated/clean, <b className="text-zinc-200">1</b> explicit, <b className="text-zinc-200">2</b> safe edited version.</div>
          <div className="flex items-center gap-2 panel px-3 py-2 flex-wrap">
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
            <div key={t.path} className="flex items-center gap-3 panel px-3 py-2">
              <TrackNoBadge disc={discNoOf(t.path)} track={trackNoOf(t.path)} />
              <span className="flex-1 truncate text-sm">{displayTitle(t.path)}</span>
              {!!t.tags.ITUNESADVISORY && !["0", "1", "2"].includes(t.tags.ITUNESADVISORY.trim()) && (
                <span
                  className="chip bg-amber-950/40 text-amber-300 border border-amber-900 shrink-0"
                  title="ITUNESADVISORY must be 0, 1 or 2 — pick a value below to fix it"
                >
                  invalid existing value “{t.tags.ITUNESADVISORY}”
                </span>
              )}
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

      {/* ---------------- Step 7: finish ---------------- */}
      {step === 7 && (
        <div className="panel p-6">
          <div className="text-center">
            <Check className="h-10 w-10 text-emerald-400 mx-auto mb-3" />
            <div className="font-semibold text-lg">Import complete</div>
            <div className="text-sm text-zinc-500 mt-1">
              {uploaded.length > 1
                ? `${uploaded.length} albums were added to your library. Use the dropdown above to finish linking, matching and tagging each one.`
                : "Links, MBIDs, metadata, genres, lyrics and advisory ratings are written to the files."}
            </div>
          </div>
          <div className="mt-4">
            <ScriptChainNote preview={scriptChain} />
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
            <div className="flex items-center gap-2 mt-3 flex-wrap">
              <button
                className="btn-primary !py-1.5 text-xs"
                onClick={runAllScripts}
                disabled={scriptsRunning || !uploaded.length}
                title="Run the configured import chain — the same scripts a bulk or Soulseek import runs"
              >
                <Wand2 className={`h-3.5 w-3.5 ${scriptsRunning ? "animate-spin" : ""}`} />
                {scriptsRunning ? "Running…" : "Run the import chain"}
              </button>
              <span className="text-[10px] text-zinc-500">
                Runs the whole chain in its configured order; tick boxes above to run just those on Done.
              </span>
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
      {step > 0 && step < 7 && (
        <div className="flex justify-between pt-2">
          <button className="btn-ghost" onClick={() => setStep(step - 1)}>
            <ChevronLeft className="h-4 w-4" /> Back
          </button>
          <div className="flex items-center gap-2">
            {nextBlock && <span className="text-xs text-amber-300/90">{nextBlock}</span>}
            <button
              className="btn-primary"
              disabled={!canNext || busy}
              onClick={() =>
                step === 1
                  ? nextFromLinks()
                  : step === 2
                    ? confirmMatch()
                    : step === 3
                      ? saveCovers()
                      : step === 4
                        ? saveGenres()
                        : step === 5
                          ? saveLyricsStep()
                          : saveAdvisory()
              }
            >
              {step === 6 ? "Save & finish" : "Continue"} <ChevronRight className="h-4 w-4" />
            </button>
          </div>
        </div>
      )}

      {/* Metadata review for the album just committed. The artist is its
          parent folder — the images and descriptions the modal edits live
          there. Dismissing it never touches the wizard's own step state. */}
      {reviewPath && (
        <MetadataReviewModal
          artist={baseName(reviewPath.split("/").slice(0, -1).join("/"))}
          albumPath={reviewPath}
          onClose={() => setReviewPath(null)}
        />
      )}
    </div>
  );
}

function CloudDownloadIcon() {
  return <ExternalLink className="h-3.5 w-3.5" />;
}

/** Exactly which optimizer scripts the import chain runs — the same chain the
 *  bulk queue and the Soulseek import use, configured in Settings → Import. */
function ScriptChainNote({ preview }: { preview?: ImportScriptsPreview }) {
  if (!preview) return <div className="text-[11px] text-zinc-600">Reading the import chain…</div>;
  const chain = preview.chain ?? [];
  return (
    <div className="text-[11px] text-zinc-500">
      {chain.length ? (
        <>
          Runs automatically after import:{" "}
          <span className="text-zinc-300">
            {chain.map((id) => preview.labels?.[String(id)] ?? `script ${id}`).join(" → ")}
          </span>{" "}
          — the chain is configurable in Settings → Import.
        </>
      ) : (
        <>The import chain is empty — albums are imported without post-processing (Settings → Import).</>
      )}
    </div>
  );
}

/** AcoustID stage: fingerprint the staged audio and name the release group it
 *  really is. "Use this release" hands the result back to the wizard's own
 *  release fetch + auto-match flow — there is no second tag writer. */
function AcoustidBlock({
  match, busy, queue, canMatchAll, matchAllBusy, onRun, onUse, onMatchAll,
}: {
  match: AcoustidMatch | null;
  busy: boolean;
  /** Queue mode: offer the shared "apply the chosen release to the queue". */
  queue: boolean;
  canMatchAll: boolean;
  matchAllBusy: boolean;
  onRun: () => void;
  onUse: (row: AcoustidAlbumMatch) => void;
  onMatchAll: () => void;
}) {
  return (
    <div className="panel p-4 space-y-2">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-sm font-semibold text-zinc-300">AcoustID fingerprint</span>
        <span className="text-xs text-zinc-500">
          — names the release group the audio actually is, before matching by hand.
        </span>
        {queue && (
          <button
            className="btn-primary !py-1 text-xs ml-auto"
            onClick={onMatchAll}
            disabled={matchAllBusy || !canMatchAll}
            title="Match every queued album to the release chosen below and write its metadata"
          >
            <Check className="h-3.5 w-3.5" />
            {matchAllBusy ? "Matching the queue…" : "Match the queue to the chosen release"}
          </button>
        )}
        <button
          className={`btn-ghost !py-1 text-xs ${queue ? "" : "ml-auto"}`}
          onClick={onRun}
          disabled={busy}
        >
          <Wand2 className={`h-3.5 w-3.5 ${busy ? "animate-spin" : ""}`} />
          {busy ? "Fingerprinting…" : "Fingerprint & match"}
        </button>
      </div>
      {match && !match.available && (
        <div className="rounded-lg border border-amber-900/60 bg-amber-950/30 px-3 py-2 text-xs text-amber-200">
          Fingerprinting is unavailable — {match.note}. Add an AcoustID API key (or install fpcalc) in{" "}
          <b className="text-amber-100">Settings → Import</b>; until then search by title or paste a release
          link below.
        </div>
      )}
      {match?.available && (
        <div className="space-y-1">
          {match.albums?.map((row) => (
            <div
              key={row.path}
              className="flex items-center gap-2 bg-panel rounded border border-border px-3 py-2 text-xs flex-wrap"
            >
              <Disc3 className="h-3.5 w-3.5 text-zinc-500 shrink-0" />
              <span className="text-zinc-400 truncate max-w-[14rem]" title={row.path}>
                {row.path.split(/[\\/]/).pop()}
              </span>
              {row.release_group_id ? (
                <>
                  <span className="text-zinc-200">{row.release_group_title}</span>
                  {row.artists?.length ? <span className="text-zinc-500">{row.artists.join(", ")}</span> : null}
                  {row.release_group_type && (
                    <span className="chip bg-raise border border-border text-zinc-400">{row.release_group_type}</span>
                  )}
                  <span className="chip bg-emerald-900/50 text-emerald-300 border border-emerald-800">
                    {row.matched}/{row.total} tracks
                  </span>
                  {row.score != null && (
                    <span className="text-zinc-600 font-mono">score {Math.round(row.score * 100)}%</span>
                  )}
                  <button
                    className="btn-ghost !py-0.5 text-[11px] ml-auto"
                    onClick={() => onUse(row)}
                    disabled={busy}
                    title="Fetch this release and auto-match the album's tracks"
                  >
                    Use this release
                  </button>
                </>
              ) : (
                <span className="text-zinc-500">
                  No release group matched {row.total} track(s) — search by title or paste a release link below.
                </span>
              )}
            </div>
          ))}
          {match.albums?.length === 0 && (
            <div className="text-xs text-zinc-500">Nothing staged to fingerprint yet.</div>
          )}
        </div>
      )}
    </div>
  );
}

/** Disc/track number badge shown on EVERY import step: "2.07" when the disc is
 *  known, "07" when it is not. The width is fixed so the column lines up, and
 *  the title spells the pair out for hover text. */
function TrackNoBadge({ disc, track }: { disc: number | null; track: number | null }) {
  const tt = track != null ? String(track).padStart(2, "0") : "—";
  const label =
    disc != null && track != null
      ? `Disc ${disc}, track ${track}`
      : disc != null
        ? `Disc ${disc}, track unknown`
        : track != null
          ? `Track ${track} (disc unknown)`
          : "No track number";
  return (
    <span className="text-xs text-zinc-600 w-12 shrink-0 tabular-nums" title={label}>
      {disc != null ? `${disc}.` : ""}
      {tt}
    </span>
  );
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
function ImportFileRow({ f, gi, albums, groupFiles, selectable, excluded, onToggleExcluded, onMove }: {
  f: ImportFile;
  gi: number;
  albums: AlbumGroup[];
  groupFiles: ImportFile[];
  selectable: boolean;
  excluded: boolean;
  onToggleExcluded: () => void;
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
    <div className={`flex items-center gap-2 text-xs px-2 py-1 ${excluded ? "opacity-45" : ""}`}>
      {selectable && (
        <input
          type="checkbox"
          checked={!excluded}
          onChange={onToggleExcluded}
          title={excluded ? "Excluded — click to include this file again" : "Untick to leave this file out (partial album import)"}
          aria-label={`Include ${f.relPath}`}
        />
      )}
      {isImage && thumb ? (
        <img src={thumb} alt="" className="h-8 w-8 rounded bg-raise border border-border object-cover shrink-0" />
      ) : isImage ? (
        <span className="h-8 w-8 rounded bg-raise border border-border shrink-0 flex items-center justify-center text-zinc-600 text-[9px]">IMG</span>
      ) : null}
      <span className="flex-1 min-w-0" title={f.relPath}>
        <span className={`block break-words ${excluded ? "text-zinc-500 line-through" : "text-zinc-400"}`}>{f.relPath}</span>
        {excluded && <span className="block text-[10px] text-amber-300/80">left out — will show greyed out on the album page</span>}
        {!excluded && coverFor && <span className="block text-[10px] text-accent-soft">→ {coverFor}</span>}
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
