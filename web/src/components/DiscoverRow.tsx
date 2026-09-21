import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Disc3, ExternalLink, Loader2, Plus } from "lucide-react";
import { api, getToken, serverUrl, type DiscoverItem, type DiscoverNotes } from "../api";
import { toast } from "../store";

/** What a disabled add button says when the server has no add route at all —
 *  the whole point of the state: a button that looks live and does nothing is
 *  worse than one that says why it cannot work. */
export const ADD_UNAVAILABLE =
  "add-to-library endpoint not available on this server";

/** Provider labels for a source id that arrived WITHOUT one. The routes report
 *  `source_label` on every row, so this only fills a gap (a provider added
 *  server-side before its label reaches this build); an id nobody knows is
 *  title-cased as itself rather than guessed at. */
const SOURCE_LABELS: Record<string, string> = {
  library: "Library",
  musicbrainz: "MusicBrainz",
  lastfm: "Last.fm",
  listenbrainz: "ListenBrainz",
  deezer: "Deezer",
  itunes: "Apple Music",
  audiodb: "TheAudioDB",
  wikipedia: "Wikipedia",
  wikidata: "Wikidata",
  discogs: "Discogs",
  rateyourmusic: "RateYourMusic",
  bandcamp: "Bandcamp",
};

/** The name to print for a source: what the server called it, else the table
 *  above, else the id. */
export function sourceLabel(id: string, reported?: string | null): string {
  const said = (reported ?? "").trim();
  if (said) return said;
  const key = (id ?? "").trim();
  if (!key) return "";
  return SOURCE_LABELS[key.toLowerCase()] ?? key.charAt(0).toUpperCase() + key.slice(1);
}

/** The one notes key that is NOT a source id: the recommendation shelf's own
 *  verdict on the whole seed ("no recommendation source had anything to
 *  suggest for this seed"). It is printed as its own sentence, without a
 *  provider name in front of it. */
const RECOMMENDED_NOTE = "recommended";

/** The sources that said nothing, one chip each, carrying the server's own
 *  reason ("skipped: no lastfm_api_key"). A silence the server explained is
 *  shown; one it did not explain is not invented. `sources` is the asked list,
 *  printed quietly so a short answer is never mistaken for a whole one. */
export function NotesChips({ notes, sources }: { notes?: DiscoverNotes | null; sources?: string[] }) {
  const rows = Object.entries(notes ?? {});
  const asked = sources ?? [];
  if (!rows.length && !asked.length) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {asked.length > 0 && (
        <span className="text-[10px] text-zinc-600" title="Every source this answer asked">
          asked: {asked.join(", ")}
        </span>
      )}
      {rows.map(([id, note]) => {
        // "partial: N of 2202 genres …" is a source answering with part of a
        // long list — information, not a failure, so it is not coloured like
        // one. Anything else on a source id is the reason it said nothing.
        const partial = /^partial:/i.test(note);
        return (
          <span
            key={id}
            className={`chip max-w-[24rem] truncate ${
              partial
                ? "bg-raise border border-border text-zinc-400"
                : "bg-amber-950/30 border border-amber-900/60 text-amber-300/90"
            }`}
            title={note}
          >
            {id === RECOMMENDED_NOTE ? note : `${sourceLabel(id)} — ${note}`}
          </span>
        );
      })}
    </div>
  );
}

/** What POST /api/library/add answered with, or why it could not answer.
 *  `unavailable` is a server without the route — an older build — and is the
 *  one failure the row must keep out of the retry loop. */
type AddOutcome =
  | { state: "unavailable" }
  | { state: "answered"; already: boolean; background: boolean; added: number; note: string; why: string }
  | { state: "error"; message: string };

/** The `POST /api/library/add` reply (server/api_add.py). */
type AddReply = {
  ok?: boolean;
  /** An artist's albums arrive one by one (`album_pending` per album), so the
   *  call returns while the work goes on. */
  background?: boolean;
  /** The server's own summary of what it just queued. */
  note?: string;
  albums?: { already_in_library?: boolean }[];
  skipped?: { reason?: string }[];
  errors?: { reason?: string }[];
  detail?: string;
};

/** Ask the server to add one row.
 *
 *  Sent here rather than through `api.libraryAdd` because the row has to tell
 *  a server WITHOUT the route (404/405) apart from one that refused the item:
 *  `api.ts`'s `json()` folds every failure into one message, and only the
 *  status carries that difference. Same credentials and token as any `api.ts`
 *  call. */
async function requestAdd(item: DiscoverItem): Promise<AddOutcome> {
  // The row's release group IS the thing to add, and saying so skips the
  // server's own id resolution; a row with only its own id (a recording, an
  // artist) hands that over and lets the server decide what it names.
  const rg = (item.release_group_mbid || "").trim();
  const mbid = (rg || item.mbid || "").trim();
  const token = getToken();
  let r: Response;
  try {
    r = await fetch(`${serverUrl()}/api/library/add`, {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(rg ? { mbid, kind: "release_group" } : { mbid, kind: "auto" }),
    });
  } catch (e) {
    return { state: "error", message: e instanceof Error ? e.message : String(e) };
  }
  if (r.status === 404 || r.status === 405 || r.status === 501) return { state: "unavailable" };
  let body: AddReply = {};
  try {
    body = (await r.json()) as AddReply;
  } catch {
    /* the status is the whole answer */
  }
  if (!r.ok) return { state: "error", message: String(body.detail || `HTTP ${r.status}`) };
  const albums = body.albums ?? [];
  return {
    state: "answered",
    already: albums.length > 0 && albums.every((a) => a?.already_in_library),
    background: !!body.background,
    added: albums.filter((a) => a && !a.already_in_library).length,
    note: String(body.note || ""),
    why: [...(body.skipped ?? []), ...(body.errors ?? [])].map((s) => String(s?.reason || "")).find(Boolean) ?? "",
  };
}

/** What the row's action currently IS. The queued/pending states are the
 *  point: an added release is a download job, so "Add to library" becomes
 *  "queued, pending" IN THE ROW and stays there — a toast would be gone while
 *  the album is still on its way. */
type AddPhase = "idle" | "busy" | "unavailable" | "queued" | "adding" | "already" | "nothing";

/** The unowned row's action. Disabled — with the reason in its tooltip — when
 *  the row carries no MusicBrainz id, and permanently disabled once the server
 *  has answered 404: a route this build does not have will not appear by
 *  pressing the button again. */
function AddButton({ item }: { item: DiscoverItem }) {
  const qc = useQueryClient();
  const [phase, setPhase] = useState<AddPhase>("idle");
  const [detail, setDetail] = useState("");
  const mbid = (item.release_group_mbid || item.mbid || "").trim();

  if (!mbid) {
    return (
      <span
        className="chip bg-raise border border-border text-zinc-600 shrink-0"
        title="This row states no MusicBrainz id, so there is nothing to ask the server to add"
      >
        no MBID
      </span>
    );
  }

  const tip = `Add ${item.title || "this"} to the library — the server resolves it on MusicBrainz and queues the download`;

  const run = async () => {
    setPhase("busy");
    try {
      const out = await requestAdd(item);
      if (out.state === "unavailable") {
        setPhase("unavailable");
        setDetail(ADD_UNAVAILABLE);
        toast.error(ADD_UNAVAILABLE);
        return;
      }
      if (out.state === "error") {
        setPhase("idle");
        toast.error(`Could not add ${item.title || "this"}: ${out.message}`);
        return;
      }
      setDetail(out.note || out.why);
      if (out.already) {
        setPhase("already");
        toast(`${item.title || "It"} is already in the library`);
      } else if (out.background) {
        // An artist's reply carries no albums yet — they arrive as the server
        // resolves each release group, so "background" outranks "nothing".
        setPhase("adding");
        toast.success(`Adding ${item.title || "it"} — the albums arrive as MusicBrainz resolves them`);
      } else if (!out.added) {
        setPhase("nothing");
        toast(`Nothing to add for ${item.title || "this row"}${out.why ? ` — ${out.why}` : ""}`);
      } else {
        setPhase("queued");
        toast.success(`Queued ${item.title || "it"} into the library — it appears when the download lands`);
      }
      // The add is a background download: the library payload and every
      // Discover answer (what counts as owned) are stale from here on.
      qc.invalidateQueries({ queryKey: ["library"] });
      qc.invalidateQueries({ queryKey: ["discoverGenres"] });
      qc.invalidateQueries({ queryKey: ["discoverGenre"] });
      qc.invalidateQueries({ queryKey: ["discoverRecommended"] });
    } catch (e) {
      // requestAdd turns its own failures into an outcome; this is only a
      // surprise (a bug above it), and the row returns to its button.
      setPhase("idle");
      toast.error(`Could not add ${item.title || "this"}: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  if (phase === "unavailable") {
    return (
      <span className="shrink-0" title={ADD_UNAVAILABLE}>
        <button className="btn-ghost !py-1.5 text-xs" disabled title={ADD_UNAVAILABLE}>
          <Plus className="h-3.5 w-3.5" />
          Add to library
        </button>
      </span>
    );
  }
  if (phase === "queued" || phase === "adding") {
    const label = phase === "queued" ? "Queued — pending" : "Adding — pending";
    const why = phase === "queued"
      ? "Queued into the library: the download is running, and the album appears there when it lands"
      : "Queued into the library: MusicBrainz is being read one release group at a time, and each album appears as it resolves";
    return (
      <span className="chip bg-sky-950/40 border border-sky-900/60 text-sky-300 shrink-0" title={detail ? `${why} — ${detail}` : why}>
        {label}
      </span>
    );
  }
  if (phase === "already") {
    return (
      <span className="chip bg-emerald-950/40 border border-emerald-900/60 text-emerald-300 shrink-0" title="The server found this in the library already">
        Already in library
      </span>
    );
  }
  if (phase === "nothing") {
    return (
      <span className="chip bg-amber-950/40 border border-amber-900/60 text-amber-300 shrink-0" title={detail || "The server resolved it but had nothing to add"}>
        Nothing to add
      </span>
    );
  }

  return (
    <span className="shrink-0" title={tip}>
      <button className="btn-ghost !py-1.5 text-xs" disabled={phase === "busy"} title={tip} onClick={run}>
        {phase === "busy" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
        Add to library
      </button>
    </span>
  );
}

/** In-app route for a row the library owns. `path` is set exactly when it
 *  does, so an owned row links into the library and an unowned one never
 *  pretends to have a page. */
function libraryRef(item: DiscoverItem): string | null {
  const path = (item.path ?? "").trim();
  if (!path) return null;
  const enc = encodeURIComponent(path);
  if (item.kind === "artist") return `/artist/${enc}`;
  if (item.kind === "track") return `/track/${enc}`;
  return `/album/${enc}`;
}

/** The row's cover: the library's own image when the item is owned (the file
 *  on disk is the authority), otherwise the provider's — proxied through
 *  `/api/art`, because several cover CDNs refuse a browser outright. A URL
 *  that fails collapses to the same placeholder as a row that has none. */
function RowCover({ item }: { item: DiscoverItem }) {
  const [failed, setFailed] = useState(false);
  // `owned`, NOT `in_library`: that flag only says the library holds something
  // from this artist, so this exact item has no library art to show.
  const owned = !!item.owned;
  const path = owned ? (item.path ?? "").trim() : "";
  let src: string | null = null;
  if (path) {
    if (item.kind === "artist") src = api.artistImageUrl(item.artist || path);
    else if (item.kind === "track") {
      const cut = path.lastIndexOf("/");
      src = api.coverUrl(cut > 0 ? path.slice(0, cut) : path, cut > 0 ? path.slice(cut + 1) : undefined);
    } else src = api.coverUrl(path);
  }
  if (!src && item.cover_url) {
    src = api.artUrl(item.cover_url, { artist: item.artist, album: item.title, rg: item.release_group_mbid });
  }
  const box = "h-11 w-11 rounded bg-raise border border-border overflow-hidden shrink-0";
  if (!src || failed) {
    return (
      <div className={`${box} flex items-center justify-center text-zinc-700`} title={item.cover_url ? "The provider image could not be shown" : "No cover"}>
        <Disc3 className="h-5 w-5" />
      </div>
    );
  }
  return (
    <div className={box}>
      <img
        src={src}
        alt=""
        loading="lazy"
        decoding="async"
        referrerPolicy="no-referrer"
        onError={() => setFailed(true)}
        className="h-full w-full object-cover"
      />
    </div>
  );
}

/** One Discover row: cover, title, artist and year, the provider it came from,
 *  why it is here, and — the part that matters — either a link into the
 *  library for what the user already owns or the add action for what they do
 *  not. Rows arrive from `/api/discover/genre` and `/api/discover/recommended`
 *  and are the same object whatever produced them.
 *
 *  `owned` and `in_library` are NOT the same statement: `owned` means the
 *  library holds this exact item (and `path` is set), while `in_library` only
 *  means it holds something by that artist. Only ownership earns the library
 *  link; a row the user merely collects the artist of still gets the add
 *  action, with the collection noted beside it. */
export default function DiscoverRow({ item }: { item: DiscoverItem }) {
  const owned = !!item.owned;
  const ref = owned ? libraryRef(item) : null;
  const label = sourceLabel(item.source, item.source_label);
  const alsoFrom = (item.also_from ?? []).filter((id) => id && id !== item.source);
  const sub = [item.artist, item.year].filter(Boolean).join(" · ");
  return (
    <li className="flex items-center gap-3 px-3 py-2">
      <RowCover item={item} />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2 min-w-0">
          {ref ? (
            <Link
              to={ref}
              className="text-sm font-medium truncate hover:text-accent-soft"
              title={`Open in the library: ${item.title}`}
            >
              {item.title || "—"}
            </Link>
          ) : item.page_url ? (
            <a
              href={item.page_url}
              target="_blank"
              rel="noreferrer"
              className="text-sm font-medium truncate hover:text-accent-soft"
              title={item.page_url}
            >
              {item.title || "—"}
            </a>
          ) : (
            <span className="text-sm font-medium truncate">{item.title || "—"}</span>
          )}
          {item.tracks?.length ? (
            <span className="chip bg-raise border border-border text-zinc-500 shrink-0" title={item.tracks.join(" · ")}>
              {item.tracks.length} tracks
            </span>
          ) : null}
        </div>
        <div className="text-[11px] text-zinc-500 truncate">{sub || label}</div>
        {item.reason && (
          <div className="text-[10px] text-zinc-600 truncate" title={item.reason}>
            {item.reason}
          </div>
        )}
      </div>
      <span
        className="chip bg-raise border border-border text-zinc-400 shrink-0 hidden sm:inline-flex"
        title={alsoFrom.length
          ? `This row came from ${label} (and from ${alsoFrom.map((id) => sourceLabel(id)).join(", ")})`
          : `This row came from ${label}`}
      >
        {label}
        {alsoFrom.length > 0 && <span className="text-zinc-600">+{alsoFrom.length}</span>}
      </span>
      {!owned && item.in_library ? (
        <span
          className="chip bg-raise border border-border text-zinc-500 shrink-0 hidden md:inline-flex"
          title={`The library already holds something by ${item.artist || "this artist"} — not this release`}
        >
          artist in library
        </span>
      ) : null}
      {owned ? (
        ref ? (
          <Link className="btn-ghost !py-1.5 text-xs shrink-0 tap" to={ref} title="Open it in the library">
            In library
          </Link>
        ) : (
          <span
            className="chip bg-emerald-950/40 border border-emerald-900/60 text-emerald-300 shrink-0"
            title="The library holds this, but the row carries no path for it"
          >
            owned
          </span>
        )
      ) : (
        <AddButton item={item} />
      )}
      {item.page_url && (
        <a
          href={item.page_url}
          target="_blank"
          rel="noreferrer"
          className="p-1 rounded-lg text-zinc-600 hover:text-white hover:bg-raise transition-colors shrink-0"
          title="Open the source page"
        >
          <ExternalLink className="h-3.5 w-3.5" />
        </a>
      )}
    </li>
  );
}
