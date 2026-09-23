import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, RotateCcw } from "lucide-react";
import { api } from "../api";
import { toast } from "../store";
import type { SourceHealth, SourceKind, SourcesHealth } from "../types";

/** The provider families, in the order the panel lists them. The rows
 *  themselves come from `/api/sources/health` — nothing here is a source
 *  list, so a provider added on the backend shows up on its own.
 *
 *  `credentials` is not a family of sources: those rows are the saved logins
 *  the families above need, and they answer the narrower question a source row
 *  cannot ("is this key ACCEPTED" — Discogs browses anonymously, so its own
 *  row stays green with a discarded token). `SourceKind` has to name it or the
 *  group would not be rendered at all. */
const KIND_LABEL: Record<SourceKind, string> = {
  lyrics: "Lyrics providers — synced, free",
  advisory: "Advisory sources — release ratings & parental flags",
  genre: "Genre sources",
  metadata: "Metadata providers — images & descriptions",
  links: "Link sources — where the album's rating links come from",
  discover: "Discover sources — genre browse & recommendations",
  credentials: "API keys & logins — is each saved credential accepted?",
};

/** What each config key a source may need is called and where it is issued.
 *  The KEYS come from the endpoint's `needs`; this is only their wording. */
const KEY_INFO: Record<string, { label: string; hint: string; url?: string; link?: string; secret?: boolean }> = {
  spotify_client_id: {
    label: "Spotify client ID",
    hint: "Create an app, then copy its Client ID from the dashboard.",
    url: "https://developer.spotify.com/dashboard",
    link: "Spotify developer dashboard",
  },
  spotify_client_secret: {
    label: "Spotify client secret",
    hint: "Same app page → Show client secret. Free, no card.",
    url: "https://developer.spotify.com/dashboard",
    link: "Spotify developer dashboard",
    secret: true,
  },
  discogs_token: {
    label: "Discogs personal access token",
    hint: "Generate a token (free account) and paste it here.",
    url: "https://www.discogs.com/settings/developers",
    link: "Discogs developers",
    secret: true,
  },
  lastfm_api_key: {
    label: "Last.fm API key",
    hint: "API account → the key is shown immediately.",
    url: "https://www.last.fm/api/account/create",
    link: "last.fm API account",
  },
  rym_cookie: {
    label: "RateYourMusic cookie",
    hint:
      "How to get it: sign in to rateyourmusic.com in your browser → F12 (dev tools) → Network → reload the page → click any request to rateyourmusic.com → Headers → Request Headers → copy everything after \"Cookie:\". " +
      "Paste the WHOLE header value — every name=value pair it shows, not just one token like cf_clearance: RYM checks the session cookies together, and the app normalises the paste for you (newlines, a stray \"Cookie:\" label). " +
      "It is a session credential: keep it to yourself, and paste a fresh one when RYM starts refusing — signing out or clearing cookies invalidates it, and Test asks RYM again even after a refusal. " +
      "MusicBrainz already states the RYM page for many releases, so this is only needed for the rest.",
    url: "https://rateyourmusic.com",
    link: "rateyourmusic.com",
    secret: true,
  },
  // The AcoustID pair belongs HERE, in the wizard's Keys step: the application
  // key is what makes fingerprint matching work at all, and the user key is
  // what publishing a match needs — telling someone to go to Settings → Import
  // in the middle of a first run is how an install ends up with neither.
  acoustid_api_key: {
    label: "AcoustID application key",
    hint:
      "Register an application (free) and copy its API key. It is what fingerprint matching uses to identify which release the AUDIO is — MusicBrainz and the wizard's AcoustID step both need it.",
    url: "https://acoustid.org/new-application",
    link: "acoustid.org — register an application",
  },
  acoustid_user_key: {
    label: "AcoustID user key",
    hint:
      "Sign in at acoustid.org → your account → API keys, and copy YOUR user key (a different key from the application one). Only needed to publish a matched fingerprint pair back to AcoustID's database; lookups never use it.",
    url: "https://acoustid.org/account",
    link: "acoustid.org — your account",
    secret: true,
  },
};

/** The RYM rows carry the last response RYM gave the backend (`rym_last`):
 *  `detail` is the sentence the probe built, and this is the response behind
 *  it. "403" and "403 with no challenge marker" are a stale cookie and a
 *  blocked network — the panel shows which one it was. Only those rows have
 *  it, and it is not part of the shared `SourceHealth` shape. */
type RymLast = {
  status?: number | null;
  challenge?: boolean;
  url?: string;
  at_iso?: string;
  reason?: string;
};

const rymLastOf = (row: SourceHealth): RymLast | undefined =>
  (row as SourceHealth & { rym_last?: RymLast }).rym_last;

/** The config keys this panel can prompt for, in a stable order — every one of
 *  those settings has exactly one control, and it is this panel, wherever the
 *  panel is mounted (the wizard's Keys step and Settings → Sources draw the
 *  same component). */
const KEY_NAMES = Object.keys(KEY_INFO);

/** Keys a row needs that are NOT edited here — each setting has exactly one
 *  control, and these live on the tab that owns them (the credential rows
 *  name keys the panel never had to prompt for before). The chip says where
 *  to set it, so a row that cannot be filled in here is not a dead end. */
const KEY_HOME: Record<string, string> = {
  soulseek_username: "the Soulseek tab",
  soulseek_password: "the Soulseek tab",
  ai_base_url: "Settings → AI",
  ai_model: "Settings → AI",
  ai_api_key: "Settings → AI",
  auth_password_hash: "first-run setup, or Sign-in & security",
};

/** `needs` mixes config keys with installed tools (yt-dlp): only the keys get
 *  an input, the tools are a dependency note. */
const promptKeysOf = (row: SourceHealth) => row.needs.filter((k) => k in KEY_INFO);

/** A panel row: one PROVIDER, whatever number of roles it serves. */
type PanelRow = SourceHealth & { kinds: SourceKind[] };

/** Fold the endpoint's one-row-per-ROLE list into one row per PROVIDER.
 *
 *  The same service is often reachable in more than one role — RateYourMusic
 *  is a genre source AND the album-link source, Deezer and iTunes are genre
 *  and metadata providers — and each role is its own row with its own `needs`.
 *  Listed as-is, that is the SAME key asked for twice on one screen (and the
 *  section reads as a duplicate). The first role that lists a provider owns the
 *  row; the others become role chips on it, the needs are the union, and the
 *  state is the worst of them (a role that failed is the row's state — hiding a
 *  failure behind a sibling's OK is the one thing this must not do).
 *
 *  Testing stays per provider: every role of a credential reads the same key,
 *  so one probe answers for all of them. */
function mergeRoles(rows: SourceHealth[]): PanelRow[] {
  const roles = new Map<string, SourceHealth[]>();
  for (const r of rows) {
    const list = roles.get(r.id);
    if (list) list.push(r);
    else roles.set(r.id, [r]);
  }
  const out: PanelRow[] = [];
  const done = new Set<string>();
  for (const r of rows) {
    if (done.has(r.id)) continue;
    done.add(r.id);
    const all = roles.get(r.id)!;
    const rank = { fail: 2, skipped: 1, ok: 0 } as const;
    out.push({
      ...r,
      kinds: all.map((x) => x.kind),
      needs: [...new Set(all.flatMap((x) => x.needs))],
      configured: all.every((x) => x.configured),
      status: all.reduce((worst, x) => (rank[x.status] > rank[worst] ? x.status : worst), "ok" as SourceHealth["status"]),
      detail: [...new Set(all.map((x) => x.detail).filter(Boolean))].join(" · "),
      ms: Math.max(...all.map((x) => x.ms || 0)),
    });
  }
  return out;
}

/** Deezer and iTunes are a genre source AND a metadata provider — two rows
 *  share one id, so the row key (and the in-flight marker) carries the kind. */
const busyId = (row: SourceHealth) => `${row.kind}:${row.id}`;

/** Status chip: `ok` ran, `skipped` cannot run here, `fail` ran and had no
 *  answer. A failure is a state to look at, never an error to retry. */
function StatusChip({ row }: { row: SourceHealth }) {
  const cls =
    row.status === "ok"
      ? "bg-emerald-900/50 text-emerald-300 border-emerald-800"
      : row.status === "skipped"
        ? "bg-white/5 text-zinc-400 border-white/15"
        : "bg-red-900/50 text-red-300 border-red-900";
  // Capitalised for display: the payload's enum value is `fail`, the label
  // the user reads is "Failed".
  const label = { ok: "OK", skipped: "Skipped", fail: "Failed" }[row.status] ?? row.status;
  return <span className={`chip border ${cls}`}>{label}</span>;
}

/** Every source the app can talk to, grouped by kind, each row testable on
 *  its own and carrying the key fields it needs. Shared by the setup wizard's
 *  Keys step and Settings → Sources.
 *
 *  `only` narrows the panel to one family (or a list of them) — the wizard
 *  shows the credential rows plus the links row that carries the RYM cookie,
 *  instead of the whole source list.
 *
 *  `askKeys` is that wizard view: the key FIELDS, with each row's status
 *  chips, failure detail, provider notes, live-test report and Test button
 *  folded into the row's own disclosure. A first run pastes the keys the app
 *  uses; triaging eleven providers is what Settings → Sources is for, and the
 *  panel was 4,800 px of exactly that on the step that exists to ask for keys. */
export default function SourcesPanel({ only, askKeys }: { only?: SourceKind | SourceKind[]; askKeys?: boolean } = {}) {
  const qc = useQueryClient();
  const { data: config } = useQuery({ queryKey: ["config"], queryFn: api.config });
  const { data, isLoading, error } = useQuery({
    queryKey: ["sourcesHealth"],
    queryFn: () => api.sourcesHealth(false),
    staleTime: 30000,
  });
  // id of the row being tested, or "all"
  const [busy, setBusy] = useState<string | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});

  useEffect(() => {
    if (!config) return;
    setDraft(Object.fromEntries(KEY_NAMES.map((k) => [k, String(config[k] ?? "")])));
  }, [config]);

  /** Probe one source live and fold its row into the cached list. Matched on
   *  id AND kind — deezer and itunes exist as both a genre source and a
   *  metadata provider, so an id alone would overwrite the wrong row. */
  const probeInto = async (row: SourceHealth) => {
    const fresh = await api.sourceHealth(row.id, true, row.kind);
    qc.setQueryData<SourcesHealth | undefined>(["sourcesHealth"], (d) =>
      d
        ? { ...d, sources: d.sources.map((s) => (s.id === fresh.id && s.kind === fresh.kind ? fresh : s)) }
        : d
    );
  };

  const testAll = async () => {
    setBusy("all");
    try {
      qc.setQueryData(["sourcesHealth"], await api.sourcesHealth(true));
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(null);
    }
  };

  const testOne = async (row: SourceHealth) => {
    setBusy(busyId(row));
    try {
      await probeInto(row);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(null);
    }
  };

  /** Save this row's keys, then re-test just that source — the point of
   *  entering a key is finding out whether it works. */
  const saveKeys = async (row: SourceHealth) => {
    setBusy(busyId(row));
    try {
      await api.saveConfig({
        ...config,
        ...Object.fromEntries(promptKeysOf(row).map((k) => [k, draft[k] ?? ""])),
      });
      qc.invalidateQueries({ queryKey: ["config"] });
      await probeInto(row);
      toast.success(`${row.label} re-tested`);
    } catch (e) {
      toast.error(String(e));
    } finally {
      setBusy(null);
    }
  };

  if (isLoading) return <div className="text-xs text-zinc-500">Checking sources…</div>;
  if (error) return <div className="text-xs text-red-300">{String(error)}</div>;
  const wanted = only === undefined ? null : Array.isArray(only) ? only : [only];
  const rows = mergeRoles((data?.sources ?? []).filter((r) => !wanted || wanted.includes(r.kind)))
    // The Keys step wants the rows that ASK for something: the credential list
    // also carries rows whose keys live elsewhere (the Soulseek account, the AI
    // provider, this server's login), and a row with no field is a status line
    // — those belong to Settings → Sources, not to the step that asks for keys.
    .filter((r) => !askKeys || promptKeysOf(r).length > 0);
  const groups = (Object.keys(KIND_LABEL) as SourceKind[])
    .filter((kind) => !wanted || wanted.includes(kind))
    .map((kind) => [kind, rows.filter((r) => r.kind === kind)] as const)
    .filter(([, list]) => list.length > 0);

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div>
          <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">
            {askKeys ? `API keys & logins (${rows.length})` : `Sources (${rows.length})`}
          </div>
          <div className="text-[11px] text-zinc-600 mt-0.5">
            {askKeys ? (
              <>
                Optional: a source whose key is missing is simply skipped, and every one of these is editable
                later in Settings → Sources. Saving a key re-tests that source.
              </>
            ) : (
              <>
                Every provider the app can ask. Testing runs one live lookup per source against a fixed sample;
                a source that cannot run here is skipped, never fatal.
              </>
            )}
          </div>
        </div>
        {/* The live-test sweep is triage, so the Keys step does not offer it —
            each row still re-tests itself when its keys are saved. */}
        {!askKeys && (
          <button className="btn-ghost !py-1 text-xs tap" onClick={testAll} disabled={busy !== null}>
            <RotateCcw className={`h-3 w-3 ${busy === "all" ? "animate-spin" : ""}`} />
            {busy === "all" ? "Testing…" : "Test all"}
          </button>
        )}
      </div>

      {groups.map(([kind, list]) => (
        <div key={kind} className="space-y-1">
          {!askKeys && <div className="text-[10px] uppercase tracking-widest text-zinc-500">{KIND_LABEL[kind]}</div>}
          <div className="rounded-md border border-border divide-y divide-border/60">
            {list.map((row) => {
              const promptKeys = promptKeysOf(row);
              // The RYM rows need exactly one key — the cookie — so the chip
              // names it: "cookie missing" is what the fix (paste a logged-in
              // Cookie header) hangs off. The live failure reason arrives in
              // `row.detail` from the probe below.
              const stateLabel = promptKeys.includes("rym_cookie")
                ? row.configured ? "cookie set" : "cookie missing"
                : row.configured ? "configured" : "not configured";
              const rymLast = rymLastOf(row);
              const chips = (
                <>
                  <span className="text-[13px] text-zinc-200" title={row.notes}>
                    {row.rank ? `${row.rank}. ` : ""}
                    {row.label}
                  </span>
                  <StatusChip row={row} />
                  {/* Every ROLE this one provider serves (see mergeRoles): the
                      same service can be a genre source and a links source, and
                      naming both is what keeps a second row from being needed
                      for the second role. */}
                  {row.kinds.length > 1 && (
                    <span className="chip border border-white/15 bg-white/5 text-zinc-400" title="Roles this provider serves in this app">
                      {row.kinds.join(" · ")}
                    </span>
                  )}
                  {row.needs.length > 0 && (
                    <span
                      className={`chip border ${
                        row.configured
                          ? "bg-emerald-900/40 text-emerald-300 border-emerald-800"
                          : "bg-amber-900/40 text-amber-300 border-amber-900"
                      }`}
                    >
                      {stateLabel}
                    </span>
                  )}
                  {row.synced && <span className="chip border border-white/15 bg-white/5 text-zinc-400">synced</span>}
                  {row.rank !== undefined && (
                    <span
                      className="chip border border-accent/25 bg-accent/10 text-accent-soft"
                      title="Rank in the BUILT-IN chain. A saved order in Settings → Lyrics & CUEs replaces it without changing it."
                    >
                      #{row.rank} preferred
                    </span>
                  )}
                  {row.needs
                    .filter((k) => !(k in KEY_INFO))
                    .map((k) => (
                      <span key={k} className="chip border border-white/15 bg-white/5 text-zinc-400">
                        {KEY_HOME[k] ? `set ${k} in ${KEY_HOME[k]}` : `needs ${k}`}
                      </span>
                    ))}
                  <span className="flex-1" />
                  <span className="text-[10px] font-mono text-zinc-600">
                    {row.id}
                    {row.ms ? ` · ${row.ms} ms` : ""}
                  </span>
                  <button
                    className="btn-ghost !py-0.5 !px-2 text-[11px] tap"
                    onClick={() => testOne(row)}
                    disabled={busy !== null}
                  >
                    {busy === busyId(row) ? "Testing…" : "Test"}
                  </button>
                </>
              );
              // What a key BUYS: the provider's own line from the backend
              // registry, plus why RYM said no — the status code, whether
              // Cloudflare's challenge marker was in the body, the URL that was
              // asked for and when. A stale cookie and a blocked network both
              // read as "403" without it.
              const report = (
                <>
                  {row.detail && <div className="text-[11px] text-zinc-500 break-words">{row.detail}</div>}
                  {row.provides && <div className="text-[11px] text-zinc-400 break-words">{row.provides}</div>}
                  {rymLast && (
                    <div className="text-[11px] text-zinc-600 break-all">
                      Last RYM reply:{" "}
                      {typeof rymLast.status === "number" ? `HTTP ${rymLast.status}` : "no answer"}
                      {" · "}
                      {rymLast.challenge ? "challenge marker seen" : "no challenge marker"}
                      {rymLast.url ? ` · ${rymLast.url}` : ""}
                      {rymLast.at_iso ? ` · ${rymLast.at_iso}` : ""}
                    </div>
                  )}
                  {row.notes && <div className="text-[11px] text-zinc-600">{row.notes}</div>}
                </>
              );
              const keyFields = (
                <div className="flex flex-wrap items-end gap-2">
                  {promptKeys.map((k) => (
                    <label key={k} className="flex-1 min-w-[200px]">
                      <span className="text-[10px] uppercase tracking-wider text-zinc-500">{KEY_INFO[k].label}</span>
                      <input
                        className="input !py-1 text-[11px] mt-0.5 tap"
                        type={KEY_INFO[k].secret ? "password" : "text"}
                        value={draft[k] ?? ""}
                        placeholder={KEY_INFO[k].label}
                        onChange={(e) => setDraft((d) => ({ ...d, [k]: e.target.value }))}
                      />
                    </label>
                  ))}
                  <button
                    className="btn-primary !py-1 text-xs shrink-0 min-h-10 md:min-h-0"
                    disabled={busy !== null || !promptKeys.some((k) => (draft[k] ?? "") !== String(config?.[k] ?? ""))}
                    onClick={() => saveKeys(row)}
                    title="Save these keys and test this source again"
                  >
                    Save &amp; test
                  </button>
                </div>
              );
              // Where the key is issued, in the panel's own words. The Keys
              // step keeps these: a hint that says where to get the value is
              // part of asking for it, and it is one line per key.
              const hints = promptKeys.map((k) => {
                const info = KEY_INFO[k];
                return (
                  <div key={k} className="text-[10px] text-zinc-600 flex items-center gap-1 flex-wrap">
                    <span className="text-zinc-500">{info.label}:</span>
                    <span>{info.hint}</span>
                    {info.url && (
                      <a
                        className="text-accent-soft hover:underline inline-flex items-center gap-0.5"
                        href={info.url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        {info.link ?? info.url}
                        <ExternalLink className="h-2.5 w-2.5" />
                      </a>
                    )}
                  </div>
                );
              });

              if (askKeys) {
                return (
                  <div key={busyId(row)} className="px-3 py-2 space-y-1.5">
                    {keyFields}
                    <div className="space-y-1">{hints}</div>
                    <details className="rounded border border-border/60 bg-bg/40 px-2 py-1.5">
                      <summary className="text-[11px] text-zinc-500 cursor-pointer select-none">
                        {row.label} — {row.needs.length > 0 ? stateLabel : "no key"} · status, notes and Test
                      </summary>
                      <div className="mt-1.5 space-y-1">
                        <div className="flex items-center gap-2 flex-wrap">{chips}</div>
                        {report}
                      </div>
                    </details>
                  </div>
                );
              }

              return (
                <div key={busyId(row)} className="px-3 py-2 space-y-1.5">
                  <div className="flex items-center gap-2 flex-wrap">{chips}</div>
                  {report}
                  {promptKeys.length > 0 && (
                    <div className="space-y-1.5 pt-0.5">
                      {keyFields}
                      <div className="space-y-1">{hints}</div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}
