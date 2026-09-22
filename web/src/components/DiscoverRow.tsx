import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Disc3, ExternalLink, Loader2, Plus } from "lucide-react";
import { api, getToken, serverUrl, type DiscoverItem, type DiscoverNotes, type DiscoverNotApplicable } from "../api";
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
  rym: "RateYourMusic",
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

/** The notes keys that are NOT source ids — the whole request's own verdict
 *  ("no recommendation source had anything to suggest for this seed", "no
 *  chart source had anything to rank for this week"). They are printed as
 *  their own sentences, without a provider name in front of them. */
const PLAIN_NOTES: Record<string, true> = { recommended: true, charts: true };

/** How long a chip's label may be before the outcome word stands in for it.
 *  A label is NEVER clipped to fit: it is the note's lead clause when that is
 *  short enough to read as one, and otherwise the outcome word alone (`failed`
 *  — the provider's own words are a hover away), so no pill ever ends
 *  mid-sentence or mid-word. */
const MAX_LABEL = 48;

/** The SHORT label for one source note — the chip's entire visible text, with
 *  the server's own sentence in its `title`.
 *
 *  A pill holds a few words, and a sentence cut off where the pill ended reads
 *  as a bug in the app rather than as a fact about a provider, so the label is
 *  the note's own FIRST CLAUSE — up to its first ` — `, ` (` or full stop, which
 *  is a complete phrase by construction — or, when even that is too long to be
 *  a label, the outcome word (`failed`, `skipped`). A reason that is one clause
 *  long is the label whole (`no lastfm_api_key`, `unknown source`). */
function shortNote(note: string): string {
  const text = note.trim();
  const head = /^([a-z]+):\s*/i.exec(text);
  const word = (head?.[1] ?? "").toLowerCase();
  const rest = text.slice(head?.[0].length ?? 0).trim();
  const lead = rest.split(/\s+—\s+|\s+\(|\.\s+/)[0].replace(/[.;,]\s*$/, "").trim();
  const label = lead && lead.length <= MAX_LABEL ? lead : word || lead;
  return word === "partial" && label !== "partial" ? `partial: ${label}` : label || text;
}

/** The sources an ENTITY shelf cannot use at all, on ONE quiet line: who they
 *  are, the short marker saying what they CAN answer, and the provider's own
 *  full sentence in the tooltip. Never amber — this is a fact about the
 *  provider, not a request that failed, and a page that has three such sources
 *  must not look like a page with three errors. */
function CapabilityLine({ notApplicable }: { notApplicable: DiscoverNotApplicable[] }) {
  if (!notApplicable.length) return null;
  return (
    <span
      className="text-[10px] text-zinc-600"
      title={notApplicable.map((one) => `${one.label} — ${one.why}`).join("\n")}
    >
      cannot answer for this page:{" "}
      {notApplicable.map((one) => `${one.label} (${one.short})`).join(" · ")}
    </span>
  );
}

/** What each source said, and who was asked.
 *
 *  THE RENDERING RULE — which silence is information and which is an error:
 *
 *  * amber, and only amber, is a source the request actually lost something to:
 *    `failed: …` (the provider refused, timed out or answered with an error),
 *    and `skipped: …` — an answer it did not have (an empty result from a feed
 *    that should have had one) or a missing credential, which keeps its chip
 *    exactly as documented because it is the ONE silence the reader can end, by
 *    adding that key in Settings → Discovery;
 *  * quiet grey is information: `partial: …` (a source that answered with part
 *    of a long list), the `nothing to search for` chip on a row that names
 *    neither an artist nor a title (see `AddButton` — a row with no name is not
 *    a failure of anything), and `CapabilityLine` above for a source whose feed
 *    does not exist for this KIND of page at all (Last.fm's similar-tracks feed
 *    cannot be asked about an album);
 *  * `sources` is the asked list, printed quietly beside them, so a short
 *    answer is never mistaken for a whole one. A silence the server explained
 *    is shown; one it did not explain is not invented. */
export function NotesChips({
  notes,
  sources,
  notApplicable,
}: {
  notes?: DiscoverNotes | null;
  sources?: string[];
  notApplicable?: DiscoverNotApplicable[] | null;
}) {
  const rows = Object.entries(notes ?? {});
  const asked = sources ?? [];
  const cannot = notApplicable ?? [];
  if (!rows.length && !asked.length && !cannot.length) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {asked.length > 0 && (
        <span className="text-[10px] text-zinc-600" title="Every source this answer asked">
          asked: {asked.join(", ")}
        </span>
      )}
      <CapabilityLine notApplicable={cannot} />
      {rows.map(([id, note]) => {
        // "partial: N of 2202 genres …" is a source answering with part of a
        // long list — information, not a failure, so it is not coloured like
        // one. Anything else on a source id is a real outcome: the reason it
        // said nothing, in the provider's own words when it refused.
        const partial = /^partial:/i.test(note);
        return (
          <span
            key={id}
            className={`chip ${
              partial
                ? "bg-raise border border-border text-zinc-400"
                : "bg-amber-950/30 border border-amber-900/60 text-amber-300/90"
            }`}
            title={note}
          >
            {PLAIN_NOTES[id] ? note : `${sourceLabel(id)} — ${shortNote(note)}`}
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
  | {
      state: "answered";
      already: boolean;
      background: boolean;
      added: number;
      /** The request was made BY NAME (the row carries no id). */
      nameOnly: boolean;
      /** …and the server found a MusicBrainz match for it. */
      matched: boolean;
      note: string;
      why: string;
    }
  | { state: "error"; message: string };

/** The `POST /api/library/add` reply (server/api_add.py). */
type AddReply = {
  ok?: boolean;
  /** An artist's albums arrive one by one (`album_pending` per album), so the
   *  call returns while the work goes on. */
  background?: boolean;
  /** A name-only add that MATCHED: the framework album exists now and
   *  MusicBrainz is being asked the rest of the way on a thread. Id-given
   *  replies never state either flag. */
  matched?: boolean;
  resolving?: boolean;
  /** A name-only add that matched NOTHING: the wish the search was queued
   *  under (nothing is on disk yet, and the row must not read as "added"). */
  by_name?: boolean;
  wish_id?: number;
  queued?: number;
  /** The server's own summary of what it just queued. */
  note?: string;
  albums?: { already_in_library?: boolean }[];
  skipped?: { reason?: string }[];
  errors?: { reason?: string }[];
  detail?: string;
};

/** The row's add request: the body the server wants, and whether that body is
 *  a NAME (the row states no MusicBrainz id at all).
 *
 *  A row WITH an id is added by id — its release group when it has one (that
 *  skips the server's own resolution), else the id itself with `kind: "auto"`,
 *  which means "look up what this id names". A row WITHOUT one is still
 *  addable: it is sent by name, and the server searches MusicBrainz for
 *  artist+title, adopts the id it finds, and — finding none — records a
 *  name-keyed wish so the auto-import's own search can run. `kind: "auto"` is
 *  NEVER sent for a name-only row: with no id there is nothing to look up, and
 *  the row's own kind is what the search and the wish are made of.
 *
 *  `null` when the row names neither a title nor an artist: there is nothing
 *  for the server to search, which is the one case it answers 400 to. */
function addRequest(item: DiscoverItem): { body: Record<string, unknown>; nameOnly: boolean } | null {
  const rg = (item.release_group_mbid || "").trim();
  const mbid = (rg || item.mbid || "").trim();
  if (mbid) return { body: rg ? { mbid, kind: "release_group" } : { mbid, kind: "auto" }, nameOnly: false };
  const title = (item.title || "").trim();
  const artist = (item.artist || "").trim();
  if (!title && !artist) return null;
  return {
    nameOnly: true,
    body: {
      kind: item.kind,
      title,
      artist,
      // A soft preference for the search, never a filter, and the provider's
      // own page for the row — the wish's note keeps it as the source link.
      year: item.year,
      source: item.source_label || item.source || "",
      page_url: item.page_url || "",
    },
  };
}

/** Ask the server to add one row.
 *
 *  Sent here rather than through `api.libraryAdd` because the row has to tell
 *  a server WITHOUT the route (404/405) apart from one that refused the item:
 *  `api.ts`'s `json()` folds every failure into one message, and only the
 *  status carries that difference. Same credentials and token as any `api.ts`
 *  call. */
async function requestAdd(req: { body: Record<string, unknown>; nameOnly: boolean }): Promise<AddOutcome> {
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
      body: JSON.stringify(req.body),
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
    // `matched` is stated by the server for a name-only add; an id-given reply
    // has resolved its id already, so it counts as matched by construction.
    nameOnly: req.nameOnly,
    matched: !req.nameOnly || !!body.matched,
    note: String(body.note || ""),
    why: [...(body.skipped ?? []), ...(body.errors ?? [])].map((s) => String(s?.reason || "")).find(Boolean) ?? "",
  };
}

/** What the row's action currently IS. The queued/pending states are the
 *  point: an added release is a download job, so "Add to library" becomes
 *  "queued, pending" IN THE ROW and stays there — a toast would be gone while
 *  the album is still on its way. */
type AddPhase = "idle" | "busy" | "unavailable" | "queued" | "adding" | "byname" | "already" | "nothing";

/** The unowned row's action.
 *
 *  A row the server CAN add offers the button however little the row knows: an
 *  id when it has one, else artist+title, which the server searches
 *  MusicBrainz for and — finding nothing — turns into a name-keyed wish. The
 *  one row that gets no button is the one with no name to search AT ALL (not
 *  even an artist), because there the add could only ever fail; that chip is
 *  informational grey, not an error. Permanently disabled once the server has
 *  answered 404: a route this build does not have will not appear by pressing
 *  the button again. */
function AddButton({ item }: { item: DiscoverItem }) {
  const qc = useQueryClient();
  const [phase, setPhase] = useState<AddPhase>("idle");
  const [detail, setDetail] = useState("");
  const req = addRequest(item);

  if (!req) {
    return (
      <span
        className="chip bg-raise border border-border text-zinc-600 shrink-0"
        title="This row names neither an artist nor a title, so there is nothing for the server to search for"
      >
        nothing to search for
      </span>
    );
  }

  const tip = req.nameOnly
    ? `Add ${item.title || "this"} to the library — the server searches MusicBrainz for it by artist and title, and queues a name search when it finds no match`
    : `Add ${item.title || "this"} to the library — the server resolves it on MusicBrainz and queues the download`;

  const run = async () => {
    setPhase("busy");
    try {
      const out = await requestAdd(req);
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
      } else if (out.nameOnly && !out.matched) {
        // The server found no MusicBrainz match and queued a name-keyed wish:
        // nothing is on disk, but a search IS running, so this is neither
        // "nothing to add" nor a plain queue — the server's own note says it.
        setPhase("byname");
        toast.success(out.note || `Added ${item.title || "it"} — no MusicBrainz match, searching by name`);
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
  if (phase === "queued" || phase === "adding" || phase === "byname") {
    const label =
      phase === "queued" ? "Queued — pending" : phase === "adding" ? "Adding — pending" : "Queued — searching by name";
    const why =
      phase === "queued"
        ? "Queued into the library: the download is running, and the album appears there when it lands"
        : phase === "adding"
          ? "Queued into the library: MusicBrainz is being read one release group at a time, and each album appears as it resolves"
          : "Queued into the library by NAME: MusicBrainz had no match for this row, so a name-keyed wish is searching for it by artist and title";
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

/** The row's cover.
 *
 *  The library's own image comes first when the item is owned — the file on
 *  disk is the authority — and the provider's cover is the FALLBACK, not a
 *  different path for unowned rows: an owned row whose art cannot be served
 *  (a folder with no cover, a track with no sidecar art) still has a cover the
 *  app can fetch, and the provider URL is already on the row. Both go through
 *  the app: `/api/cover` reads the file, `/api/art` proxies the provider (see
 *  `api.artUrl`) and walks Cover Art Archive → Apple → Deezer by the row's own
 *  identity when that CDN refuses us. Only a row with no candidate at all, or
 *  one where every candidate failed, keeps the placeholder.
 *
 *  A track row asks for the ALBUM FOLDER's cover: its `path` is the audio file,
 *  and `/api/cover?file=` serves a FILE by name, so passing the track's own
 *  name back fetched the audio bytes under an image content type — an image the
 *  browser cannot decode, which is how an owned track row ended up empty while
 *  its album's cover sat on disk. */
function RowCover({ item }: { item: DiscoverItem }) {
  // Remembered per URL, not as a bare flag: a row recycled onto another item
  // must not inherit the previous cover's failure (the same rule CoverImg
  // keeps), and it is what lets the provider's cover be tried after the
  // library's own URL has already failed.
  const [failed, setFailed] = useState<string[]>([]);
  // `owned`, NOT `in_library`: that flag only says the library holds something
  // from this artist, so this exact item has no library art to show.
  const owned = !!item.owned;
  const path = owned ? (item.path ?? "").trim() : "";
  let local: string | null = null;
  if (path) {
    if (item.kind === "artist") local = api.artistImageUrl(item.artist || path);
    else {
      const cut = path.lastIndexOf("/");
      local = api.coverUrl(cut > 0 ? path.slice(0, cut) : path);
    }
  }
  const provider = item.cover_url
    ? api.artUrl(item.cover_url, {
        artist: item.artist,
        album: item.title,
        rg: item.release_group_mbid,
      })
    : null;
  const candidates = [local, provider].filter((url): url is string => !!url);
  const src = candidates.find((url) => !failed.includes(url)) ?? null;
  const box = "h-10 w-10 rounded bg-raise overflow-hidden shrink-0";
  if (!src) {
    return (
      <div
        className={`${box} flex items-center justify-center text-zinc-700`}
        title={
          candidates.length
            ? "The cover could not be shown — neither the library's own art nor the provider's image loaded"
            : "No cover"
        }
      >
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
        onError={() => setFailed((seen) => (seen.includes(src) ? seen : [...seen, src]))}
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
    <li className="flex items-center gap-3 px-3 py-1.5">
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
        title={
          (alsoFrom.length
            ? `This row came from ${label} (and from ${alsoFrom.map((id) => sourceLabel(id)).join(", ")})`
            : `This row came from ${label}`)
          // The provider's OWN number, when it stated one (Last.fm's match,
          // Deezer's fans/rank, ListenBrainz's score). It is that provider's
          // scale, so it is stated here as provenance and never printed as a
          // percentage comparable across sources.
          + (typeof item.score === "number" ? ` — the provider's own score: ${item.score}` : "")
        }
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
