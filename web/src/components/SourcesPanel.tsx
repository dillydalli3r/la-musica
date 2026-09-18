import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, RotateCcw } from "lucide-react";
import { api } from "../api";
import { toast } from "../store";
import type { SourceHealth, SourceKind, SourcesHealth } from "../types";

/** The provider families, in the order the panel lists them. The rows
 *  themselves come from `/api/sources/health` — nothing here is a source
 *  list, so a provider added on the backend shows up on its own. */
const KIND_LABEL: Record<SourceKind, string> = {
  lyrics: "Lyrics providers — synced, free",
  advisory: "Advisory sources — release ratings & parental flags",
  genre: "Genre sources",
  metadata: "Metadata providers — images & descriptions",
  links: "Link sources — where the album's rating links come from",
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

/** The config keys this panel can prompt for, in a stable order. */
const KEY_NAMES = Object.keys(KEY_INFO);

/** `needs` mixes config keys with installed tools (yt-dlp): only the keys get
 *  an input, the tools are a dependency note. */
const promptKeysOf = (row: SourceHealth) => row.needs.filter((k) => k in KEY_INFO);

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
 *  its own and carrying the key fields it needs. Shared by the setup wizard
 *  (step 3, and its RateYourMusic card) and Settings → Sources.
 *
 *  `only` narrows the panel to one family — the wizard's RateYourMusic card
 *  shows just the link rows instead of the whole source list. */
export default function SourcesPanel({ only }: { only?: SourceKind } = {}) {
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
  const rows = (data?.sources ?? []).filter((r) => !only || r.kind === only);
  const groups = (Object.keys(KIND_LABEL) as SourceKind[])
    .filter((kind) => !only || kind === only)
    .map((kind) => [kind, rows.filter((r) => r.kind === kind)] as const)
    .filter(([, list]) => list.length > 0);

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div>
          <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">
            Sources ({rows.length})
          </div>
          <div className="text-[11px] text-zinc-600 mt-0.5">
            Every provider the app can ask. Testing runs one live lookup per source against a fixed sample;
            a source that cannot run here is skipped, never fatal.
          </div>
        </div>
        <button className="btn-ghost !py-1 text-xs" onClick={testAll} disabled={busy !== null}>
          <RotateCcw className={`h-3 w-3 ${busy === "all" ? "animate-spin" : ""}`} />
          {busy === "all" ? "Testing…" : "Test all"}
        </button>
      </div>

      {groups.map(([kind, list]) => (
        <div key={kind} className="space-y-1">
          <div className="text-[10px] uppercase tracking-widest text-zinc-500">{KIND_LABEL[kind]}</div>
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
              return (
              <div key={busyId(row)} className="px-3 py-2 space-y-1.5">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-[13px] text-zinc-200" title={row.notes}>
                    {row.rank ? `${row.rank}. ` : ""}
                    {row.label}
                  </span>
                  <StatusChip row={row} />
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
                        needs {k}
                      </span>
                    ))}
                  <span className="flex-1" />
                  <span className="text-[10px] font-mono text-zinc-600">
                    {row.id}
                    {row.ms ? ` · ${row.ms} ms` : ""}
                  </span>
                  <button
                    className="btn-ghost !py-0.5 !px-2 text-[11px]"
                    onClick={() => testOne(row)}
                    disabled={busy !== null}
                  >
                    {busy === busyId(row) ? "Testing…" : "Test"}
                  </button>
                </div>

                {row.detail && <div className="text-[11px] text-zinc-500">{row.detail}</div>}
                {/* Why RYM said no comes from the response itself: the status
                    code, whether Cloudflare's challenge marker was in the
                    body, the URL that was asked for and when. The sentence
                    above says what to do about it; this says what came back —
                    a stale cookie and a blocked network both read as "403"
                    without it. */}
                {rymLast && (
                  <div className="text-[11px] text-zinc-600">
                    Last RYM reply:{" "}
                    {typeof rymLast.status === "number"
                      ? `HTTP ${rymLast.status}`
                      : "no answer"}{" "}
                    · {rymLast.challenge ? "challenge marker seen" : "no challenge marker"}
                    {rymLast.url ? ` · ${rymLast.url}` : ""}
                    {rymLast.at_iso ? ` · ${rymLast.at_iso}` : ""}
                  </div>
                )}
                {row.notes && <div className="text-[11px] text-zinc-600">{row.notes}</div>}

                {promptKeys.length > 0 && (
                  <div className="space-y-1.5 pt-0.5">
                    <div className="flex flex-wrap items-end gap-2">
                      {promptKeys.map((k) => (
                        <label key={k} className="flex-1 min-w-[200px]">
                          <span className="text-[10px] uppercase tracking-wider text-zinc-500">
                            {KEY_INFO[k].label}
                          </span>
                          <input
                            className="input !py-1 text-[11px] mt-0.5"
                            type={KEY_INFO[k].secret ? "password" : "text"}
                            value={draft[k] ?? ""}
                            placeholder={KEY_INFO[k].label}
                            onChange={(e) => setDraft((d) => ({ ...d, [k]: e.target.value }))}
                          />
                        </label>
                      ))}
                      <button
                        className="btn-primary !py-1 text-xs shrink-0"
                        disabled={busy !== null || !promptKeys.some((k) => (draft[k] ?? "") !== String(config?.[k] ?? ""))}
                        onClick={() => saveKeys(row)}
                        title="Save these keys and test this source again"
                      >
                        Save &amp; test
                      </button>
                    </div>
                    {promptKeys.map((k) => {
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
                    })}
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
