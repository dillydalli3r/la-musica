import { useState } from "react";
import { Disc3, Fingerprint, Loader2, Search } from "lucide-react";
import Modal from "./Modal";
import { api } from "../api";
import { toast } from "../store";
import type { AcoustidAlbumMatch } from "../types";

/** One album the import dialog asks about: the release it names (prefilled in
 *  its row, and what a row left alone keeps) or nothing, when its files name
 *  no release at all. */
export interface IdentifyAlbum {
  path: string;
  artist: string;
  album: string;
  /** The release id the album's files already carry, when they carry one.
   *  Prefills the row and is what an untouched row keeps. */
  current?: string;
  /** The album is NOT in the music folder yet — a finished download, a staged
   *  batch. The AcoustID pass fingerprints the files, so the server must be
   *  told the path is an opt-in staged one (`_allow_staged`), exactly as the
   *  wizard's own calls do. */
  staged?: boolean;
  /** How many files of the group this press is taking. The downloads picker
   *  imports a picked SUBSET of a folder, so its row says "12 of 14 files"
   *  rather than pretending the whole entry is coming. */
  fileCount?: number;
  /** How many files the group holds in all — what `fileCount` is OF. */
  fileTotal?: number;
  /** The tags the group's own files state (from the picker's payload), an MB
   *  id included. They PREFILL the row's release input and its search box when
   *  nothing else names a release — a download's files often say who and what
   *  they are even when their release id is missing. */
  tags?: Record<string, string | null>;
}

/** The release the row's own tags name, when the caller passed no `current`:
 *  the release-GROUP id first (it is the level AcoustID answers at, and the
 *  release id the caller already folded into `current` when it had one), else
 *  the release id — either is an id the MBID input resolves. */
function tagRelease(tags: IdentifyAlbum["tags"]): string {
  return (tags?.MUSICBRAINZ_RELEASEGROUPID || tags?.MUSICBRAINZ_ALBUMID || "").trim();
}

/** What a row's three ways open on: the release the files already name — the
 *  caller's `current`, else the one their tags state. A row left alone keeps
 *  it; a row re-pointed is a pin. */
function seedOf(al: IdentifyAlbum): string {
  return (al.current || "").trim() || tagRelease(al.tags);
}

/** The free search's starting point — who and what the files say they are. */
function defaultQuery(al: IdentifyAlbum): string {
  const t = al.tags ?? {};
  const artist = (t.ALBUMARTIST || t.ARTIST || al.artist || "").trim();
  const album = (t.ALBUM || al.album || "").trim();
  return [artist, album].filter(Boolean).join(" ");
}

/** "Which release is this?" — the one question the import pipeline cannot
 *  answer for itself.
 *
 *  The owner's ask: an import must not be a guess. That is what it was — the
 *  audio is matched by name (or not at all), so an album like Toxicity could
 *  take another pressing's identity and cover and quietly import the wrong
 *  thing. Every album in the selection gets a row here, and each row offers
 *  the three ways a person actually knows which release it is:
 *
 *  * paste a MusicBrainz link (or the bare ID) — a release URL, a release-group
 *    URL, either is accepted and the server resolves it;
 *  * **Detect** — fingerprint the album with AcoustID and let it say which
 *    release GROUP the audio is, with its own score and the tracks it matched
 *    (the app's own AcoustID path: `POST /api/import/acoustid`);
 *  * **a MusicBrainz search, two ways** — a free query (artist + album), which
 *    the row PREFILLS from the files' own tags, and the CD's catalogue number
 *    (`catno:`), which is the one search free text cannot do for you; each hit
 *    is one click from being the pin.
 *
 *  A batch of rows also offers **Detect all** — ONE AcoustID call fingerprints
 *  every album at once, so a multi-album import costs one request rather than a
 *  press per row. Every row that has an edit is a PIN, the release it names;
 *  each row's release-group match lands under it as a "use this" link.
 *
 *  The row IS the release change: a row opens on the release the album's files
 *  name now — the caller's `current`, or, when it has none, the id the files'
 *  tags state (marked "from tags", so "I pinned this" and "the files said so"
 *  never look alike) — and editing it re-points that album: the pipeline stamps
 *  a pin OVER an existing id (`force_ids`, `server.imports._stamp_release`), so
 *  the release id, the metadata and the tracklist are rewritten from what the
 *  row names. Only a row the user actually edits is sent as a pin, so a
 *  selection with nothing changed imports exactly as it would have. An album
 *  left blank is imported the way it always was (matched by name): the dialog
 *  is a chance to be sure, never a wall. */
export default function ImportIdentifyDialog({
  albums,
  onClose,
  onStart,
  cancelNote,
}: {
  albums: IdentifyAlbum[];
  onClose: () => void;
  onStart: (pins: Record<string, string>) => void | Promise<void>;
  /** What Cancel DOES, for the caller whose staging placed these albums in
   *  the library before this dialog opened (the import wizard removes them
   *  again). The Library and Soulseek callers own nothing, so they pass
   *  nothing and Cancel is the plain close it always was. */
  cancelNote?: string;
}) {
  const [pins, setPins] = useState<Record<string, string>>({});
  const [catno, setCatno] = useState<Record<string, string>>({});
  /** The free MusicBrainz query per row (artist + album). Unset falls back to
   *  what the files' tags say, so an untouched row searches by itself. */
  const [query, setQuery] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<Record<string, "detect" | "search" | "find" | "start">>({});
  const [detected, setDetected] = useState<Record<string, AcoustidAlbumMatch>>({});
  const [found, setFound] = useState<Record<string, any[]>>({});

  const setPin = (path: string, value: string) =>
    setPins((cur) => ({ ...cur, [path]: value }));

  const detect = async (al: IdentifyAlbum) => {
    setBusy((b) => ({ ...b, [al.path]: "detect" }));
    try {
      const res = await api.importAcoustid([al.path], false, !!al.staged);
      const row = res.albums?.[0];
      if (!row) {
        toast("AcoustID: nothing came back for that album", "error");
        return;
      }
      setDetected((cur) => ({ ...cur, [al.path]: row }));
      if (!res.available) toast(res.note || "AcoustID is not configured", "error");
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(({ [al.path]: _gone, ...rest }) => rest);
    }
  };

  /** Fingerprint EVERY row at once: one AcoustID call takes the whole album
   *  list, so a batch costs one request and one fpcalc pass per album instead
   *  of a press per row. Each row's own result lands under its path, so the
   *  per-row "use this" links still work. */
  const detectAll = async () => {
    const paths = albums.map((a) => a.path);
    if (!paths.length) return;
    setBusy((b) => {
      const next = { ...b };
      for (const p of paths) next[p] = "detect";
      return next;
    });
    try {
      const res = await api.importAcoustid(paths, false, albums.some((a) => a.staged));
      const rows = res.albums || [];
      setDetected((cur) => {
        const next = { ...cur };
        for (const row of rows) next[row.path] = row;
        return next;
      });
      const matched = rows.filter((r) => r.status === "matched").length;
      if (!res.available) toast(res.note || "AcoustID is not configured", "error");
      else toast(`AcoustID: ${matched}/${rows.length} album(s) matched`);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy((b) => {
        const next = { ...b };
        for (const p of paths) if (next[p] === "detect") delete next[p];
        return next;
      });
    }
  };

  /** MusicBrainz search, two ways over one row: a free query (artist + album,
   *  which the row prefills from the files' own tags) and the catalogue number,
   *  the one search a free-text release search cannot do for you. */
  const find = async (al: IdentifyAlbum, mode: "release" | "catno") => {
    const q = (mode === "catno" ? catno[al.path] : query[al.path] ?? defaultQuery(al)).trim();
    if (!q) {
      toast(mode === "catno" ? "Type the release's catalogue number first" : "Type an artist and album to search for");
      return;
    }
    setBusy((b) => ({ ...b, [al.path]: mode === "catno" ? "find" : "search" }));
    try {
      const rows = await api.mbSearchReleases(q, mode);
      setFound((cur) => ({ ...cur, [al.path]: rows || [] }));
      if (!rows || !rows.length) {
        toast(mode === "catno" ? `No MusicBrainz release carries "${q}"` : `No MusicBrainz release matches "${q}"`);
      }
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(({ [al.path]: _gone, ...rest }) => rest);
    }
  };

  const start = async () => {
    const pinsOut: Record<string, string> = {};
    for (const [path, value] of Object.entries(pins)) {
      const v = (value || "").trim();
      // A row left on the release it already names (its `current`, or the one
      // its tags state) sends NO pin: an unchanged album re-imports the way it
      // always did, and a pin — which the pipeline stamps over an existing id —
      // is reserved for the row the user actually re-pointed.
      const row = albums.find((a) => a.path === path);
      const base = row ? seedOf(row) : "";
      if (v && v.toLowerCase() !== base.toLowerCase()) pinsOut[path] = v;
    }
    setBusy({ start: "start" });
    try {
      await onStart(pinsOut);
    } finally {
      setBusy({});
    }
  };

  // "pinned" is what the press will SEND: a row still holding the release it
  // already names is not a pin (see `start`), so the count the subtitle and
  // the button print is the rows the user actually re-pointed.
  const pinned = albums.filter((a) => {
    const v = (pins[a.path] ?? seedOf(a)).trim();
    return !!v && v.toLowerCase() !== seedOf(a).toLowerCase();
  }).length;
  // How many rows open on a release of their own: the wording below turns on
  // it, because "keeps the release it names" says nothing to a user whose
  // rows are all blank (nothing named yet).
  const named = albums.filter((a) => !!seedOf(a)).length;
  const keptNote = named
    ? "A row left alone keeps the release it already names."
    : "An album left blank is imported the way it always was — matched by name.";
  const pending = Object.keys(busy).length > 0;

  return (
    <Modal
      icon={Disc3}
      title={albums.length === 1 ? "Which release is this?" : `Releases — ${albums.length} albums`}
      subtitle={
        `${named
          ? `${named} of these already name their release in the files, and each row opens on it — change a link and that album is re-pointed at the release you name (its release id, metadata and tracklist are rewritten from it). `
          : ""}${keptNote}`
        + (pinned ? ` · ${pinned} re-pointed` : "")
      }
      onClose={onClose}
      width="max-w-3xl"
      bodyClass="px-5 py-4 space-y-3"
      footer={
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[11px] text-zinc-500">{keptNote}</span>
          <span className="ml-auto inline-flex items-center gap-2">
            <button className="btn-ghost !py-1.5 text-xs tap" onClick={onClose}
              disabled={pending} title={cancelNote}>
              Cancel
            </button>
            <button className="btn-primary !py-1.5 text-xs tap" onClick={() => void start()} disabled={pending}>
              {busy.start ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
              Import {albums.length} album{albums.length === 1 ? "" : "s"}
              {pinned ? ` — ${pinned} ${named ? "re-pointed" : "pinned"}` : ""}
            </button>
          </span>
        </div>
      }
    >
      {albums.length > 1 && (
        <div className="flex items-center gap-2 flex-wrap">
          <button
            className="btn-ghost !py-1 text-xs tap"
            onClick={() => void detectAll()}
            disabled={pending}
            title="Fingerprint every album with AcoustID in ONE request and let it say which release group each one is"
          >
            {busy[albums[0].path] === "detect" && albums.every((a) => busy[a.path] === "detect")
              ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
              : <Fingerprint className="h-3.5 w-3.5" />}
            Detect all (AcoustID)
          </button>
          <span className="text-[11px] text-zinc-500">
            One fingerprint pass for the whole batch — each row's own match and release-group link appears below it.
          </span>
        </div>
      )}
      {albums.map((al) => {
        const det = detected[al.path];
        const rows = found[al.path];
        const seed = seedOf(al);
        const tagSeed = tagRelease(al.tags);
        const value = (pins[al.path] ?? seed).trim();
        // Where the row OPENED: the release the files themselves name, either
        // in their own id (`current`) or in their tags. A row the user then
        // re-pointed carries neither chip — that state is "pinned by me".
        const changed = !!value && value.toLowerCase() !== seed.toLowerCase();
        const fromCurrent = !!al.current && !changed && value.toLowerCase() === al.current.trim().toLowerCase();
        const fromTags = !al.current && !!tagSeed && !changed;
        return (
          <div key={al.path} className="rounded-lg border border-border bg-panel/60 p-3 space-y-2">
            <div className="flex items-baseline gap-2 flex-wrap">
              <Disc3 className="h-3.5 w-3.5 text-zinc-500" />
              <span className="text-sm text-zinc-100">{al.album}</span>
              <span className="text-xs text-zinc-500">{al.artist}</span>
              {fromCurrent && (
                <span className="chip text-[9px] text-emerald-300/90" title="This is the release id the album's own files already carry — a row left alone keeps it">
                  in files
                </span>
              )}
              {fromTags && (
                <span className="chip bg-amber-950/40 text-amber-300 border border-amber-900 text-[9px]" title="The files name no release id, but their own tags name this one — a row left alone keeps it">
                  from tags
                </span>
              )}
              {al.fileCount !== undefined && (
                <span className="chip text-[9px] text-zinc-400" title="How many of the group's files this import takes">
                  {al.fileTotal && al.fileTotal !== al.fileCount
                    ? `${al.fileCount} of ${al.fileTotal} files`
                    : `${al.fileCount} file${al.fileCount === 1 ? "" : "s"}`}
                </span>
              )}
              {changed && (
                <span className="chip bg-accent/15 text-accent-soft border border-accent/30 text-[9px]" title="You re-pointed this row — the import is pinned to your release">
                  your pick
                </span>
              )}
              {al.path && (
                <span className="text-[10px] text-zinc-600 font-mono truncate max-w-[18rem]" title={al.path}>
                  {al.path.split(/[\\/]/).slice(-2).join("/")}
                </span>
              )}
            </div>
            <input
              className="input !py-1.5 text-xs font-mono"
              placeholder="MusicBrainz release or release-group URL, or the bare ID"
              value={pins[al.path] ?? seed}
              onChange={(e) => setPin(al.path, e.target.value)}
            />
            <div className="flex flex-wrap items-center gap-2">
              <button
                className="btn-ghost !py-1 text-xs tap"
                onClick={() => void detect(al)}
                disabled={pending}
                title="Fingerprint this album with AcoustID and let it say which release group the audio is"
              >
                {busy[al.path] === "detect"
                  ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  : <Fingerprint className="h-3.5 w-3.5" />}
                Detect with AcoustID
              </button>
              <span className="inline-flex items-center gap-1.5">
                <input
                  className="input !py-1 !w-44 text-xs"
                  placeholder="Artist + album"
                  value={query[al.path] ?? defaultQuery(al)}
                  onChange={(e) => setQuery((c) => ({ ...c, [al.path]: e.target.value }))}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      void find(al, "release");
                    }
                  }}
                />
                <button
                  className="btn-ghost !py-1 text-xs tap"
                  onClick={() => void find(al, "release")}
                  disabled={pending}
                  title="Search MusicBrainz for a release by artist and album"
                >
                  {busy[al.path] === "search"
                    ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    : <Search className="h-3.5 w-3.5" />}
                  Search
                </button>
              </span>
              <span className="inline-flex items-center gap-1.5">
                <input
                  className="input !py-1 !w-40 text-xs"
                  placeholder="Catalogue number"
                  value={catno[al.path] ?? ""}
                  onChange={(e) => setCatno((c) => ({ ...c, [al.path]: e.target.value }))}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      void find(al, "catno");
                    }
                  }}
                />
                <button
                  className="btn-ghost !py-1 text-xs tap"
                  onClick={() => void find(al, "catno")}
                  disabled={pending}
                  title="Search MusicBrainz for the release with this catalogue number"
                >
                  {busy[al.path] === "find"
                    ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    : <Search className="h-3.5 w-3.5" />}
                  Cat no
                </button>
              </span>
            </div>
            {det && (
              <p className="text-[11px] leading-relaxed text-zinc-400">
                {det.status === "matched" && det.release_group_id ? (
                  <>
                    AcoustID: <b className="text-zinc-200">{det.release_group_title || "a release group"}</b>
                    {det.artists?.length ? ` — ${det.artists.join(", ")}` : ""}
                    {typeof det.score === "number" ? ` · score ${det.score.toFixed(2)}` : ""}
                    {det.total ? ` · ${det.matched ?? 0}/${det.total} tracks` : ""}
                    {" · "}
                    <button
                      className="text-accent-soft hover:text-white underline underline-offset-2"
                      onClick={() =>
                        setPin(al.path, `https://musicbrainz.org/release-group/${det.release_group_id}`)
                      }
                    >
                      use this
                    </button>
                  </>
                ) : (
                  <>AcoustID: {det.reason || det.status || "no match"}</>
                )}
              </p>
            )}
            {rows && rows.length > 0 && (
              <ul className="space-y-1">
                {rows.slice(0, 5).map((r) => (
                  <li key={String(r.id)} className="flex items-baseline gap-2 text-[11px]">
                    <button
                      className="text-left text-zinc-300 hover:text-white underline-offset-2 hover:underline truncate"
                      onClick={() => setPin(al.path, `https://musicbrainz.org/release/${r.id}`)}
                      title="Use this release"
                    >
                      {r.title || r.id}
                    </button>
                    <span className="text-zinc-500 truncate">
                      {[r.artist, r.date, r.country].filter(Boolean).join(" · ")}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        );
      })}
    </Modal>
  );
}
